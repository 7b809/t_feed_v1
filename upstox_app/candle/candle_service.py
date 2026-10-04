"""
Candle loader.

- Historical + intraday fetch with 7-day batching and thread pool.
- Process-wide RateLimiter for Cloudflare 429s.
- Freshness check: files are treated as stale (and refetched) when
  missing, empty, status!=success, or when their stored `to_date` is
  behind the last market day.
- Freshness is applied on startup (ensure_index) and on daily refresh
  (refresh_if_outdated).
"""
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import upstox_client

from core.config import core_config
from core.logger import get_logger
from upstox_app.candle.candle_storage import candle_storage
from upstox_app.common.config import upstox_config
from upstox_app.common.market_day import last_market_day

logger = get_logger(__name__)

_thread_local = threading.local()


def _cfg(name: str, default: Any) -> Any:
    return getattr(upstox_config, name, default)


# ---- rate limiter ----------------------------------------------------------


def _is_rate_limit(exc: BaseException) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc)
    return (
        "(429)" in text
        or "Too Many Requests" in text
        or "Error 1015" in text
        or "rate_limited" in text
    )


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    headers = getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._consecutive_429 = 0
        self._total_429s = 0

    def wait_if_cooling(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._cooldown_until:
                    return
                remaining = self._cooldown_until - now
            time.sleep(min(remaining, 2.0))

    def note_429(self, retry_after: Optional[float]) -> float:
        base = float(_cfg("CANDLES_RATE_LIMIT_BASE_COOLDOWN_SEC", 30.0))
        cap = float(_cfg("CANDLES_RATE_LIMIT_MAX_COOLDOWN_SEC", 300.0))
        if retry_after is not None and retry_after > 0:
            base = max(base, retry_after)
        with self._lock:
            self._consecutive_429 += 1
            self._total_429s += 1
            backoff = base * (2 ** min(self._consecutive_429 - 1, 4))
            backoff = min(backoff, cap)
            self._cooldown_until = max(
                self._cooldown_until, time.monotonic() + backoff
            )
            attempt = self._consecutive_429
        logger.warning(
            "Rate limit hit | consecutive=%d | total=%d | cooldown=%.1fs (all workers paused)",
            attempt, self._total_429s, backoff,
        )
        return backoff

    def note_success(self) -> None:
        with self._lock:
            if self._consecutive_429:
                logger.info(
                    "Rate-limit recovered | consecutive_429s_reset=%d",
                    self._consecutive_429,
                )
            self._consecutive_429 = 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "consecutive_429": self._consecutive_429,
                "total_429": self._total_429s,
                "cooling_down": time.monotonic() < self._cooldown_until,
            }


_rate_limiter = RateLimiter()


# ---- SDK helpers -----------------------------------------------------------


def _thread_api() -> "upstox_client.HistoryV3Api":
    api = getattr(_thread_local, "api", None)
    if api is None:
        api = upstox_client.HistoryV3Api()
        _thread_local.api = api
    return api


def _extract_candles(response: Any) -> List[List[Any]]:
    if response is None:
        return []
    data = (
        response.get("data")
        if isinstance(response, dict)
        else getattr(response, "data", None)
    )
    if not data:
        return []
    if isinstance(data, dict):
        candles = data.get("candles")
    else:
        candles = getattr(data, "candles", None)
    return list(candles or [])


def _split_windows(from_date: date, to_date: date, max_days: int) -> List[Tuple[date, date]]:
    if max_days <= 0:
        max_days = 7
    windows: List[Tuple[date, date]] = []
    cursor = from_date
    while cursor <= to_date:
        end = min(cursor + timedelta(days=max_days - 1), to_date)
        windows.append((cursor, end))
        cursor = end + timedelta(days=1)
    return windows


def _call_with_retry(label: str, fn, *args, **kwargs) -> Tuple[Optional[Any], Optional[str]]:
    max_retries = int(_cfg("CANDLES_RATE_LIMIT_MAX_RETRIES", 3))
    request_delay = float(_cfg("CANDLES_REQUEST_DELAY_SEC", 0.5))
    attempt = 0
    while True:
        _rate_limiter.wait_if_cooling()
        if request_delay > 0:
            time.sleep(request_delay)
        try:
            result = fn(*args, **kwargs)
            _rate_limiter.note_success()
            return result, None
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc):
                retry_after = _retry_after_seconds(exc)
                cooldown = _rate_limiter.note_429(retry_after)
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "%s | giving up after %d retries | last_cooldown=%.1fs",
                        label, max_retries, cooldown,
                    )
                    return None, f"rate_limit_exhausted after {max_retries} retries"
                logger.info("%s | retry %d/%d queued after cooldown", label, attempt, max_retries)
                continue
            return None, str(exc)


class CandleService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()

    # ---- per-contract --------------------------------------------------
    def _fetch_historical_batched(
        self, instrument_key, unit, interval, from_date, to_date, max_days_per_request
    ) -> Tuple[List[List[Any]], List[str], int, int]:
        candles: List[List[Any]] = []
        errors: List[str] = []
        windows = _split_windows(from_date, to_date, max_days_per_request)
        windows_failed = 0
        api = _thread_api()
        for w_from, w_to in windows:
            resp, err = _call_with_retry(
                f"Historical {instrument_key} {w_from}..{w_to}",
                api.get_historical_candle_data1,
                instrument_key, unit, interval,
                w_to.isoformat(), w_from.isoformat(),
            )
            if err is not None:
                windows_failed += 1
                errors.append(f"historical {w_from}..{w_to}: {err}")
                logger.warning(
                    f"Historical window failed | key={instrument_key} "
                    f"from={w_from} to={w_to} | err={err}"
                )
                continue
            candles.extend(_extract_candles(resp))
        return candles, errors, len(windows), windows_failed

    def _fetch_intraday(self, instrument_key, unit, interval) -> Tuple[List[List[Any]], List[str], bool]:
        api = _thread_api()
        resp, err = _call_with_retry(
            f"Intraday {instrument_key}",
            api.get_intra_day_candle_data,
            instrument_key, unit, interval,
        )
        if err is not None:
            return [], [f"intraday: {err}"], False
        return _extract_candles(resp), [], True

    def _build_payload(self, index_name: str, contract: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        instrument_key = contract.get("instrument_key")
        strike = contract.get("strike_price")
        otype = contract.get("option_type") or contract.get("instrument_type")

        unit = _cfg("CANDLES_UNIT", "minutes")
        interval = _cfg("CANDLES_INTERVAL", "1")
        days = int(_cfg("CANDLES_DAYS", 10))
        include_intraday = bool(_cfg("CANDLES_INCLUDE_INTRADAY", True))
        max_days = int(_cfg("CANDLES_MAX_DAYS_PER_REQUEST", 7))

        today = date.today()
        yesterday = last_market_day(today)
        from_date = yesterday - timedelta(days=days - 1)

        historical, hist_errors, windows_total, windows_failed = self._fetch_historical_batched(
            instrument_key, unit, interval, from_date, yesterday, max_days
        )

        intraday: List[List[Any]] = []
        intra_errors: List[str] = []
        intraday_ok = True
        if include_intraday:
            intraday, intra_errors, intraday_ok = self._fetch_intraday(
                instrument_key, unit, interval
            )

        errors = hist_errors + intra_errors
        has_historical = len(historical) > 0
        has_intraday = len(intraday) > 0

        if has_historical or has_intraday:
            status = "success"
        elif errors:
            status = "error"
        else:
            status = "empty"

        payload = {
            "status": status,
            "index_name": index_name.upper(),
            "instrument_key": instrument_key,
            "strike_price": strike,
            "option_type": otype,
            "expiry": contract.get("expiry"),
            "trading_symbol": contract.get("trading_symbol"),
            "unit": unit,
            "interval": interval,
            "days": days,
            "from_date": from_date.isoformat(),
            "to_date": yesterday.isoformat(),
            "fetched_at": datetime.now().astimezone().isoformat(),
            "historical_count": len(historical),
            "intraday_count": len(intraday),
            "windows_total": windows_total,
            "windows_failed": windows_failed,
            "errors": errors,
            "candles": historical + intraday,
        }

        if has_historical and (not include_intraday or has_intraday):
            outcome = "full"
        elif has_historical or has_intraday:
            outcome = "partial"
        else:
            outcome = "failed"

        return payload, {
            "outcome": outcome,
            "historical_ok": has_historical,
            "intraday_ok": intraday_ok if include_intraday else None,
            "windows_total": windows_total,
            "windows_failed": windows_failed,
        }

    def _process_contract(self, index_name: str, contract: Dict[str, Any]) -> Tuple[Path, str, str, Dict[str, Any]]:
        strike = contract.get("strike_price")
        otype = contract.get("option_type") or contract.get("instrument_type")
        payload, outcome = self._build_payload(index_name, contract)
        path = candle_storage.save(index_name, strike, otype, payload)
        return path, str(strike), str(otype), outcome

    def _process_contract_overwrite(self, index_name: str, contract: Dict[str, Any]) -> Tuple[Path, str, str, Dict[str, Any]]:
        strike = contract.get("strike_price")
        otype = contract.get("option_type") or contract.get("instrument_type")
        payload, outcome = self._build_payload(index_name, contract)
        path = candle_storage.overwrite(index_name, strike, otype, payload)
        return path, str(strike), str(otype), outcome

    # ---- per-index -----------------------------------------------------
    def ensure_index(self, index_name: str, contracts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        index_name = index_name.upper()
        result: Dict[str, Any] = {
            "index_name": index_name,
            "total_contracts": 0,
            "already_present": 0,
            "missing_before": 0,
            "fetched": 0,
            "failed": 0,
            "full_success": 0,
            "partial_success": 0,
            "errors": [],
            "paths": [],
            "contracts_source": "none",
            "contracts_from_module": 0,
            "contracts_from_fallback": 0,
            "fallback_used": False,
        }

        if contracts is None:
            resolved, source = self._load_contracts_with_source(index_name)
            contracts = resolved
            result["contracts_source"] = source
            if source == "module":
                result["contracts_from_module"] = len(contracts)
            elif source == "runtime_file":
                result["contracts_from_fallback"] = len(contracts)
                result["fallback_used"] = True

        result["total_contracts"] = len(contracts or [])
        if not contracts:
            logger.warning(f"No option contracts available for index={index_name}; skipping candle load")
            return result

        if result["contracts_source"] == "module":
            logger.info(f"Contracts resolved from module cache | index={index_name} | count={len(contracts)}")
        elif result["contracts_source"] == "runtime_file":
            logger.warning(
                f"Contracts resolved via runtime-file FALLBACK | index={index_name} "
                f"| count={len(contracts)} | reason=module cache unavailable"
            )

        # Freshness: stale includes missing, empty, old, or non-success files.
        last_md = last_market_day().isoformat()
        stale = candle_storage.stale_contracts(index_name, contracts, last_md)
        result["missing_before"] = len(stale)
        result["already_present"] = len(contracts) - len(stale)

        if not stale:
            logger.info(
                f"All candle files fresh | index={index_name} | contracts={len(contracts)} | up_to={last_md}"
            )
            return result

        max_workers = max(1, int(_cfg("CANDLES_MAX_WORKERS", 4)))
        progress_every = max(1, int(_cfg("CANDLES_PROGRESS_EVERY", 10)))
        request_delay = float(_cfg("CANDLES_REQUEST_DELAY_SEC", 0.5))
        total_missing = len(stale)

        logger.info(
            f"Fetching candles | index={index_name} | stale={total_missing} of {len(contracts)} "
            f"| up_to={last_md} | workers={max_workers} | request_delay={request_delay}s "
            f"| progress_every={progress_every}"
        )

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(self._process_contract, index_name, c): c for c in stale}
            for future in as_completed(future_map):
                contract = future_map[future]
                strike = contract.get("strike_price")
                otype = contract.get("option_type") or contract.get("instrument_type")
                completed += 1
                try:
                    path, _, _, outcome = future.result()
                    result["fetched"] += 1
                    result["paths"].append(str(path))
                    if outcome["outcome"] == "full":
                        result["full_success"] += 1
                    elif outcome["outcome"] == "partial":
                        result["partial_success"] += 1
                        logger.warning(
                            f"Partial candles | index={index_name} | strike={strike} "
                            f"| type={otype} | historical_ok={outcome['historical_ok']} "
                            f"| intraday_ok={outcome['intraday_ok']} "
                            f"| windows_failed={outcome['windows_failed']}/{outcome['windows_total']}"
                        )
                    else:
                        result["failed"] += 1
                        result["errors"].append(f"{strike}_{otype}: no candles returned")
                        logger.error(
                            f"Candle fetch produced no data | index={index_name} "
                            f"| strike={strike} | type={otype} "
                            f"| windows_failed={outcome['windows_failed']}/{outcome['windows_total']}"
                        )
                except Exception as exc:  # noqa: BLE001
                    result["failed"] += 1
                    result["errors"].append(f"{strike}_{otype}: {exc}")
                    logger.error(
                        f"Candle fetch/save failed | index={index_name} "
                        f"| strike={strike} | type={otype} | err={exc}"
                    )

                if completed % progress_every == 0 or completed == total_missing:
                    pct = (completed / total_missing) * 100.0 if total_missing else 100.0
                    rl = _rate_limiter.snapshot()
                    logger.info(
                        "Progress | index=%s | %d/%d (%.1f%%) | full=%d partial=%d failed=%d "
                        "| fetched=%d | rl_429s=%d rl_cooling=%s",
                        index_name, completed, total_missing, pct,
                        result["full_success"], result["partial_success"],
                        result["failed"], result["fetched"],
                        rl["total_429"], rl["cooling_down"],
                    )

        rl = _rate_limiter.snapshot()
        logger.info(
            "Candle summary | index=%s | total=%d | fresh=%d | stale=%d "
            "| fetched=%d | full=%d | partial=%d | failed=%d "
            "| contracts_source=%s | fallback_used=%s | rl_429s=%d",
            index_name, result["total_contracts"], result["already_present"],
            result["missing_before"], result["fetched"], result["full_success"],
            result["partial_success"], result["failed"], result["contracts_source"],
            result["fallback_used"], rl["total_429"],
        )
        return result

    def ensure_all_enabled(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "enabled": bool(_cfg("CANDLES_ENABLED", True)),
            "days": int(_cfg("CANDLES_DAYS", 10)),
            "results": {},
            "totals": {
                "indexes": 0,
                "total_contracts": 0,
                "already_present": 0,
                "missing_before": 0,
                "fetched": 0,
                "full_success": 0,
                "partial_success": 0,
                "failed": 0,
                "fallback_indexes": [],
                "module_indexes": [],
            },
        }
        if not summary["enabled"]:
            return summary

        for name, meta in core_config.MAIN_INDEXES.items():
            if not meta.get("enabled"):
                continue
            with self._lock:
                res = self.ensure_index(name)
                summary["results"][name] = res

            t = summary["totals"]
            t["indexes"] += 1
            t["total_contracts"] += res["total_contracts"]
            t["already_present"] += res["already_present"]
            t["missing_before"] += res["missing_before"]
            t["fetched"] += res["fetched"]
            t["full_success"] += res["full_success"]
            t["partial_success"] += res["partial_success"]
            t["failed"] += res["failed"]
            if res["contracts_source"] == "runtime_file":
                t["fallback_indexes"].append(name)
            elif res["contracts_source"] == "module":
                t["module_indexes"].append(name)

        t = summary["totals"]
        rl = _rate_limiter.snapshot()
        logger.info(
            "Candle GLOBAL summary | indexes=%d | total=%d | fresh=%d | stale=%d "
            "| fetched=%d | full=%d | partial=%d | failed=%d "
            "| module_indexes=%s | fallback_indexes=%s | rl_429s=%d",
            t["indexes"], t["total_contracts"], t["already_present"],
            t["missing_before"], t["fetched"], t["full_success"],
            t["partial_success"], t["failed"], t["module_indexes"],
            t["fallback_indexes"], rl["total_429"],
        )
        return summary

    # ---- daily refresh -------------------------------------------------
    def refresh_if_outdated(self, force_reload_options: bool = True) -> Dict[str, Any]:
        """
        Daily check. Refetches a contract when:
          - its stored `expiry` != the live contract's expiry, OR
          - its candle file is stale (missing / empty / old / status!=success).
        """
        summary: Dict[str, Any] = {
            "enabled": bool(_cfg("CANDLES_DAILY_REFRESH_ENABLED", True)),
            "force_reload_options": force_reload_options,
            "option_reload": None,
            "indexes": {},
            "totals": {
                "indexes_checked": 0,
                "contracts_checked": 0,
                "expiry_changed": 0,
                "stale": 0,
                "outdated": 0,
                "refreshed": 0,
                "failed": 0,
                "stale_indexes": [],
            },
        }
        if not summary["enabled"]:
            logger.info("Candle daily refresh disabled")
            return summary

        with self._refresh_lock:
            if force_reload_options:
                try:
                    from upstox_app.option.option_service import load_enabled_indexes
                    reload_result = load_enabled_indexes()
                    summary["option_reload"] = reload_result
                    logger.info("Daily refresh | option reload | %s", reload_result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Daily refresh | option reload failed | err=%s", exc)
                    summary["option_reload"] = {"error": str(exc)}

            for name, meta in core_config.MAIN_INDEXES.items():
                if not meta.get("enabled"):
                    continue
                res = self._refresh_index_if_outdated(name)
                summary["indexes"][name] = res
                t = summary["totals"]
                t["indexes_checked"] += 1
                t["contracts_checked"] += res["contracts_checked"]
                t["expiry_changed"] += res["expiry_changed"]
                t["stale"] += res["stale"]
                t["outdated"] += res["outdated"]
                t["refreshed"] += res["refreshed"]
                t["failed"] += res["failed"]
                if res["outdated"]:
                    t["stale_indexes"].append(name)

        t = summary["totals"]
        logger.info(
            "Candle daily refresh summary | indexes=%d | checked=%d | expiry_changed=%d "
            "| stale=%d | outdated=%d | refreshed=%d | failed=%d | stale_indexes=%s",
            t["indexes_checked"], t["contracts_checked"], t["expiry_changed"],
            t["stale"], t["outdated"], t["refreshed"], t["failed"], t["stale_indexes"],
        )
        return summary

    def _refresh_index_if_outdated(self, index_name: str) -> Dict[str, Any]:
        index_name = index_name.upper()
        result: Dict[str, Any] = {
            "index_name": index_name,
            "contracts_checked": 0,
            "expiry_changed": 0,
            "stale": 0,
            "outdated": 0,
            "refreshed": 0,
            "failed": 0,
            "stale_contracts": [],
            "errors": [],
        }

        contracts = self._load_contracts(index_name)
        if not contracts:
            logger.warning("Daily refresh | no contracts for index=%s", index_name)
            return result

        last_md = last_market_day().isoformat()
        stale: List[Dict[str, Any]] = []
        for c in contracts:
            strike = c.get("strike_price")
            otype = c.get("option_type") or c.get("instrument_type")
            current_expiry = c.get("expiry")
            if strike is None or not otype:
                continue

            result["contracts_checked"] += 1
            stored_expiry = candle_storage.stored_expiry(index_name, strike, otype)
            expiry_changed = bool(
                stored_expiry is not None
                and current_expiry is not None
                and stored_expiry != str(current_expiry)
            )
            fresh = candle_storage.is_fresh(index_name, strike, otype, last_md)

            if expiry_changed or not fresh:
                stale.append(c)
                result["stale_contracts"].append({
                    "strike": strike,
                    "type": otype,
                    "expiry_changed": expiry_changed,
                    "was_fresh": fresh,
                    "stored_expiry": stored_expiry,
                    "current_expiry": str(current_expiry) if current_expiry else None,
                })
                if expiry_changed:
                    result["expiry_changed"] += 1
                if not fresh:
                    result["stale"] += 1

        result["outdated"] = len(stale)
        if not stale:
            logger.info(
                "Daily refresh | index=%s | checked=%d | all fresh | up_to=%s",
                index_name, result["contracts_checked"], last_md,
            )
            return result

        logger.info(
            "Daily refresh | index=%s | checked=%d | outdated=%d "
            "(expiry_changed=%d stale=%d) | refreshing...",
            index_name, result["contracts_checked"], len(stale),
            result["expiry_changed"], result["stale"],
        )

        max_workers = max(1, int(_cfg("CANDLES_MAX_WORKERS", 4)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(self._process_contract_overwrite, index_name, c): c
                for c in stale
            }
            for future in as_completed(future_map):
                contract = future_map[future]
                strike = contract.get("strike_price")
                otype = contract.get("option_type") or contract.get("instrument_type")
                try:
                    future.result()
                    result["refreshed"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["failed"] += 1
                    result["errors"].append(f"{strike}_{otype}: {exc}")
                    logger.error(
                        "Daily refresh failed | index=%s | strike=%s | type=%s | err=%s",
                        index_name, strike, otype, exc,
                    )
        return result

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _load_contracts_with_source(index_name: str) -> Tuple[List[Dict[str, Any]], str]:
        contracts = CandleService._contracts_from_module(index_name)
        if contracts:
            return contracts, "module"
        contracts = CandleService._contracts_from_runtime_file(index_name)
        if contracts:
            return contracts, "runtime_file"
        return [], "none"

    @staticmethod
    def _load_contracts(index_name: str) -> List[Dict[str, Any]]:
        contracts, _ = CandleService._load_contracts_with_source(index_name)
        return contracts

    @staticmethod
    def _contracts_from_module(index_name: str) -> List[Dict[str, Any]]:
        try:
            module = importlib.import_module("upstox_app.option.option_service")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"option_service module import failed | err={exc}")
            return []

        for fn_name in (
            "get_contracts", "list_contracts", "get_cached_contracts",
            "get_index_contracts", "get_options",
        ):
            fn = getattr(module, fn_name, None)
            if callable(fn):
                try:
                    data = fn(index_name)
                    if data:
                        return list(data)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"option_service.{fn_name} failed | index={index_name} | err={exc}")

        for cache_name in ("options_cache", "_cache", "CACHE"):
            cache = getattr(module, cache_name, None)
            if isinstance(cache, dict):
                data = cache.get(index_name)
                if not data and isinstance(cache.get("by_index"), dict):
                    data = cache["by_index"].get(index_name)
                if isinstance(data, dict):
                    data = data.get("contracts")
                if data:
                    return list(data)
        return []

    @staticmethod
    def _contracts_from_runtime_file(index_name: str) -> List[Dict[str, Any]]:
        try:
            import json
            runtime_dir = _cfg("OPTIONS_RUNTIME_DIR", "data/runtime")
            path = Path(runtime_dir) / "options" / f"{index_name.upper()}.json"
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    return list(json.load(fh).get("contracts") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Runtime fallback for contracts failed | index={index_name} | err={exc}")
        return []


candle_service = CandleService()