# services/isolated_instrument_sr_export_service.py
"""
Isolated-instrument SR export service (live + expired).

Live mode (expired=False / None)
--------------------------------
  run("NSE_FO|53749")
    - loads data/all_instruments.json
    - resolves metadata + CE<->PE sibling
    - fetches LOOKBACK_DAYS historical 1-min + today's intraday 1-min
    - merges by minute (intraday wins), drops zero/invalid candles
    - computes strict pivot SR levels (left=15, right=1)

Expired mode (expired=True, end_date required)
----------------------------------------------
  run("NSE_FO|51356|27-10-2026", expired=True, end_date="2026-10-21")
    - metadata is resolved by the SAME chain used by
      routes/expired_chart_routes._resolve_contract_meta:

        (1) direct method on expired_instruments_service
            (get_contract_by_key / get_contract_meta / get_contract)
        (2) any dict-shaped cache on the service
        (3) get_expired_option_contracts(index, expiry) for every
            known index whose exchange matches the key's segment

      …so trading_symbol, strike_price, lot_size, tick_size, etc.
      come from the real expired-contract registry, not from guesses.

    - strike_type fallback: if resolution fails to return an
      instrument_type, derive from the numeric id parity:
          even id -> CE      odd id -> PE

    - opposite key is derived by flipping parity:
          CE (even id) -> id + 1  (odd, PE)
          PE (odd  id) -> id - 1  (even, CE)

Early-session SR check (both modes)
-----------------------------------
  After writing isolated_instrument.json, we look at its `sr_levels`
  and find levels whose:

      pivot_time      >= 09:15:00 and < 09:30:00 (IST, check_date)
      confirmed_time  >= 09:15:00 and < 09:30:00 (IST, check_date)

  i.e. an SR level whose pivot AND confirmation both happen inside
  the first 15 minutes of the market day on `check_date`.

  - Live mode:     check_date defaults to today (IST), caller can
                   override with check_date="DD-MM-YYYY" or "YYYY-MM-DD".
  - Expired mode:  check_date is FORCED to end_date (caller-supplied
                   check_date is ignored with a warning log).

  If multiple levels qualify, the OLDEST one wins
  (earliest pivot_time, then earliest confirmed_time).

Both modes write:
    <LIVE_DATA_DIR>/isolation/isolated_instrument.json
    <LIVE_DATA_DIR>/isolation/opposite_instrument.json
    <LIVE_DATA_DIR>/isolation/isolated_instrument_sr_check.json

    Per-instrument SR snapshots (2026-11-06):
    <LIVE_DATA_DIR>/isolation/{isolated_trading_symbol}_live_sr.json
    <LIVE_DATA_DIR>/isolation/{opposite_trading_symbol}_live_sr.json
      — atomic, thread-safe writes (tmp + os.replace); filenames
        sanitized via _safe_filename_for_sr() so the promotion
        service's backfill
        (isolated_instrument_promotion_service._backfill_isolation_sr_baseline)
        and this export service land on identical paths for the same
        instrument.

Public API:
    run(instrument_key, expired=None, end_date=None, check_date=None) -> dict

    - expired    : None/False -> live; True -> expired (end_date required)
    - end_date   : None for live; "YYYY-MM-DD" or "DD-MM-YYYY" for expired
    - check_date : None -> defaults to today (IST) for live;
                   forced to end_date when expired=True
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import config
from core.config import PROJECT_ROOT
from core.logger import get_logger
from services.token_service import token_service
from upstox_app.services.expired_instruments_service import (
    expired_instruments_service,
)

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Tunables                                                                    #
# --------------------------------------------------------------------------- #
LOOKBACK_DAYS: int = 11
LEFT_BARS: int = config.LIVE_SR_LEFT_BARS if hasattr(config, "LIVE_SR_LEFT_BARS") else 15
RIGHT_BARS: int = config.LIVE_SR_RIGHT_BARS if hasattr(config, "LIVE_SR_RIGHT_BARS") else 1

IST = timezone(timedelta(hours=5, minutes=30))

ALL_INSTRUMENTS_PATH = PROJECT_ROOT / "data" / "all_instruments.json"

# --------------------------------------------------------------------------- #
# Isolation directory resolution                                              #
# --------------------------------------------------------------------------- #
# The promotion service writes its per-instrument SR baseline to
# <LIVE_DATA_DIR>/isolation/<safe>_live_sr.json. Prefer
# config.LIVE_DATA_DIR here so the export service lands on the same
# physical path; fall back to PROJECT_ROOT/data (the historical
# default) when the attribute is absent.
try:
    _LIVE_DATA_DIR_PATH: Path = Path(config.LIVE_DATA_DIR)
except Exception:
    _LIVE_DATA_DIR_PATH = PROJECT_ROOT / "data"

ISOLATION_DIR: Path = _LIVE_DATA_DIR_PATH / "isolation"

# 2026-11-06 — per-instrument `_live_sr.json` snapshots.
# Alias of ISOLATION_DIR: same physical directory, so the export
# service and the promotion service's backfill
# (_backfill_isolation_sr_baseline) always write to the same path.
ISOLATION_SR_DIR: Path = ISOLATION_DIR

ISOLATED_FILENAME = "isolated_instrument.json"
OPPOSITE_FILENAME = "opposite_instrument.json"
EARLY_CHECK_FILENAME = "isolated_instrument_sr_check.json"

# Same index set the expired chart routes use — needed so that when we
# try to resolve an expired contract we query the *right* index expiry.
KNOWN_INDEXES: list[dict[str, str]] = [
    {"key": "NSE_INDEX|Nifty 50",          "label": "NIFTY 50"},
    {"key": "NSE_INDEX|Nifty Bank",        "label": "BANKNIFTY"},
    {"key": "NSE_INDEX|NIFTY MID SELECT",  "label": "MIDCPNIFTY"},
    {"key": "NSE_INDEX|NIFTY FIN SERVICE", "label": "FINNIFTY"},
    {"key": "BSE_INDEX|SENSEX",            "label": "SENSEX"},
]


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class IsolatedInstrumentSRExportService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token_source: str | None = None

        # Per-run caches for the expired contract resolver — identical
        # behaviour to routes/expired_chart_routes.py, scoped to one run
        # so we never serve stale data across separate invocations.
        self._expired_meta_cache: dict[str, dict | None] = {}
        self._expired_contracts_cache: dict[tuple[str, str], list] = {}

    # ================================================================== #
    # ATOMIC PER-INSTRUMENT LIVE-SR SNAPSHOTS (2026-11-06)                #
    # ================================================================== #
    @staticmethod
    def _safe_filename_for_sr(trading_symbol: Any) -> str:
        """Return a filesystem-safe version of ``trading_symbol``.

        Mirrors ``isolated_instrument_promotion_service._safe_filename()``
        exactly so the export service and the promotion backfill produce
        identical filenames for the same instrument. Any character that
        is not alphanumeric or one of ``._-`` is replaced with ``_``.
        """
        raw = str(trading_symbol or "").strip() or "unknown"
        safe = "".join(
            ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw
        )
        return safe.strip("_") or "unknown"

    @classmethod
    def _live_sr_path(cls, trading_symbol: Any) -> Path:
        """Return ``<ISOLATION_SR_DIR>/<safe>_live_sr.json``.

        Same physical path the promotion service's
        ``_backfill_isolation_sr_baseline()`` writes to, so dashboards
        and other consumers see one canonical file per instrument.
        """
        return (
            ISOLATION_SR_DIR
            / f"{cls._safe_filename_for_sr(trading_symbol)}_live_sr.json"
        )

    @staticmethod
    def _atomic_write_json(path: Path, payload: Any) -> None:
        """Write ``payload`` to ``path`` atomically.

        Writes to ``<path>.tmp`` and then ``os.replace()``s it over the
        target. Concurrent readers therefore see either the previous
        complete file or the new complete file — never a partial write.

        ``os.replace()`` is atomic on POSIX and on Windows when the
        source and destination share a volume, which is always true
        here (both live in the same parent directory).

        ``fsync`` is best-effort: some filesystems (e.g. overlayfs in
        containers) raise ``OSError`` on ``fileno()`` fsync and we
        deliberately swallow that so the write still succeeds.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)

    def _build_live_sr_payload(self, instrument_payload: dict) -> dict:
        """Build the per-instrument ``_live_sr.json`` content.

        Deliberately excludes the full candle series (that lives in
        ``isolated_instrument.json`` / ``opposite_instrument.json``) so
        the per-instrument snapshot stays small for dashboards. Counts
        and candle timestamps are included so consumers can detect
        staleness without loading the candle arrays.
        """
        md = instrument_payload.get("metadata") or {}
        params = instrument_payload.get("params") or {}
        candles = instrument_payload.get("candles") or []
        levels = instrument_payload.get("sr_levels") or []

        return {
            "instrument_key": md.get("instrument_key"),
            "trading_symbol": md.get("trading_symbol"),
            "underlying": md.get("underlying"),
            "strike_price": md.get("strike_price"),
            "strike_type": md.get("strike_type"),
            "expiry": md.get("expiry"),
            "relative_path": md.get("relative_path"),
            "role": md.get("role"),
            "is_expired": bool(md.get("is_expired", False)),
            "base_instrument_key": md.get("base_instrument_key"),
            "expired_instrument_key": md.get("expired_instrument_key"),
            "expiry_suffix": md.get("expiry_suffix"),
            "generated_at": instrument_payload.get("generated_at"),
            "params": params,
            "candle_count": instrument_payload.get(
                "candles_count", len(candles),
            ),
            "first_candle": candles[0]["time"] if candles else None,
            "last_candle": candles[-1]["time"] if candles else None,
            "level_count": instrument_payload.get(
                "sr_levels_count", len(levels),
            ),
            "counts": {
                "resistance": sum(
                    1 for l in levels if l.get("level") == "resistance"
                ),
                "support": sum(
                    1 for l in levels if l.get("level") == "support"
                ),
            },
            "levels": levels,
            "source": "isolated_instrument_sr_export_service",
        }

    def _write_live_sr_snapshot(self, instrument_payload: dict) -> dict:
        """Write one per-instrument ``_live_sr.json`` atomically.

        Never raises. Returns ``{"path": str | None, "error": str | None}``.
        A failed write does **not** abort the overall ``run()`` — the
        canonical ``isolated_instrument.json`` / ``opposite_instrument.json``
        files are still produced and the error is surfaced in the
        response payload for the caller to act on.
        """
        try:
            trading_symbol = (
                (instrument_payload.get("metadata") or {})
                .get("trading_symbol")
            )
            path = self._live_sr_path(trading_symbol)
            payload = self._build_live_sr_payload(instrument_payload)
            self._atomic_write_json(path, payload)
            logger.info(
                "[sr-export] wrote live_sr snapshot %s "
                "(instrument=%s levels=%d candles=%d)",
                path,
                payload.get("instrument_key"),
                payload.get("level_count", 0),
                payload.get("candle_count", 0),
            )
            return {"path": str(path), "error": None}
        except Exception as exc:
            logger.exception(
                "[sr-export] live_sr snapshot write failed for %s",
                (instrument_payload.get("metadata") or {}).get(
                    "instrument_key"
                ),
            )
            return {
                "path": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # ================================================================== #
    # TOKEN RESOLUTION                                                   #
    # ================================================================== #
    def _try_value(self, obj: Any, name: str) -> str | None:
        if not hasattr(obj, name):
            return None
        try:
            val = getattr(obj, name)
        except Exception:
            return None
        try:
            if callable(val):
                val = val()
        except Exception:
            return None
        if isinstance(val, str):
            s = val.strip()
            return s or None
        if isinstance(val, dict):
            for k in ("access_token", "token", "value", "accessToken"):
                v = val.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    def _resolve_token(self) -> str:
        candidates = (
            "token",
            "access_token",
            "cached_token",
            "current_token",
            "value",
            "get_token",
            "get",
            "load",
            "load_token",
            "get_access_token",
            "get_cached_token",
            "get_valid_token",
            "current",
            "fetch",
            "read",
        )

        for name in candidates:
            tok = self._try_value(token_service, name)
            if tok:
                self._token_source = f"token_service.{name}"
                return tok

        for refresh_name in (
            "refresh_tokens",
            "refresh",
            "reload",
            "load_from_db",
        ):
            fn = getattr(token_service, refresh_name, None)
            if callable(fn):
                try:
                    fn()
                    logger.info(
                        "[sr-export] token_service.%s() called",
                        refresh_name,
                    )
                except Exception:
                    logger.exception(
                        "[sr-export] token_service.%s() raised",
                        refresh_name,
                    )
                for name in candidates:
                    tok = self._try_value(token_service, name)
                    if tok:
                        self._token_source = (
                            f"token_service.{name} (after {refresh_name}())"
                        )
                        return tok

        try:
            from token_tasks import token_store  # type: ignore

            for name in (
                "get_token",
                "load_token",
                "get_access_token",
                "get",
                "read_token",
            ):
                tok = self._try_value(token_store, name)
                if tok:
                    self._token_source = f"token_store.{name}"
                    return tok
        except Exception:
            pass

        try:
            from core import config as cfg

            for name in (
                "UPSTOX_ACCESS_TOKEN",
                "ACCESS_TOKEN",
                "UPSTOX_TOKEN",
            ):
                v = getattr(cfg, name, None)
                if isinstance(v, str) and v.strip():
                    self._token_source = f"core.config.{name}"
                    return v.strip()
        except Exception:
            pass

        try:
            attrs = [
                n for n in dir(token_service) if not n.startswith("__")
            ]
            logger.error(
                "[sr-export] could not resolve Upstox token. "
                "token_service attrs=%s",
                attrs,
            )
        except Exception:
            pass

        raise RuntimeError(
            "Could not resolve Upstox access token. "
            "Add a small adapter if your token_service variant differs."
        )

    # ================================================================== #
    # UPSTOX CLIENT (live path only)                                     #
    # ================================================================== #
    def _upstox_client(self) -> Any:
        from upstox_client import ApiClient, Configuration

        token = self._resolve_token()
        logger.info(
            "[sr-export] token resolved via %s", self._token_source
        )
        try:
            cfg = Configuration(access_token=token)
        except TypeError:
            cfg = Configuration()
            cfg.access_token = token
        except Exception:
            cfg = Configuration()
            cfg.access_token = token
        return ApiClient(cfg)

    # ================================================================== #
    # REGISTRY                                                           #
    # ================================================================== #
    def _load_all_instruments(self) -> dict[str, dict]:
        if not ALL_INSTRUMENTS_PATH.exists():
            raise FileNotFoundError(
                f"all_instruments.json not found at {ALL_INSTRUMENTS_PATH}"
            )
        with open(ALL_INSTRUMENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return {
                item["instrument_key"]: item
                for item in data
                if isinstance(item, dict) and item.get("instrument_key")
            }
        if isinstance(data, dict):
            return data
        raise ValueError(
            f"Unexpected all_instruments.json shape: {type(data).__name__}"
        )

    def _load_all_instruments_safe(self) -> dict[str, dict]:
        try:
            if not ALL_INSTRUMENTS_PATH.exists():
                return {}
            return self._load_all_instruments()
        except Exception as exc:
            logger.warning(
                "[sr-export] could not load all_instruments.json: %s", exc
            )
            return {}

    # ================================================================== #
    # EXPIRED KEY PARSING + PARITY HELPERS                              #
    # ================================================================== #
    @staticmethod
    def _parse_expired_key(instrument_key: str) -> tuple[str, int, str]:
        """
        'NSE_FO|51356|27-10-2026' -> ('NSE_FO|51356', 51356, '27-10-2026')
        """
        parts = instrument_key.split("|")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid expired instrument key '{instrument_key}'. "
                f"Expected 'NSE_FO|<id>|DD-MM-YYYY' "
                f"(e.g. 'NSE_FO|51356|27-10-2026'). "
                f"Got {len(parts)} pipe-separated part(s)."
            )
        exchange, id_str, expiry_suffix = parts
        try:
            id_int = int(id_str.strip())
        except ValueError:
            raise ValueError(
                f"Invalid numeric id in expired key "
                f"'{instrument_key}': '{id_str}' is not an integer."
            )
        if id_int <= 0:
            raise ValueError(
                f"Invalid id in expired key '{instrument_key}': "
                f"id must be positive, got {id_int}."
            )
        return f"{exchange.strip()}|{id_int}", id_int, expiry_suffix.strip()

    @staticmethod
    def _expiry_suffix_to_iso(expiry_suffix: str) -> str | None:
        """'27-10-2026' -> '2026-10-27'; None on parse failure."""
        try:
            dt = datetime.strptime(expiry_suffix.strip(), "%d-%m-%Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    @staticmethod
    def _strike_type_from_id(id_int: int) -> str:
        """Even id -> CE, odd id -> PE."""
        return "CE" if id_int % 2 == 0 else "PE"

    @staticmethod
    def _opposite_id(id_int: int) -> int:
        """CE (even) -> +1 (PE); PE (odd) -> -1 (CE)."""
        return id_int + 1 if id_int % 2 == 0 else id_int - 1

    # ================================================================== #
    # EXPIRED CONTRACT RESOLVER (mirrors expired_chart_routes)          #
    # ================================================================== #
    @staticmethod
    def _safe_dict(raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if hasattr(raw, "to_dict"):
            try:
                d = raw.to_dict()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        try:
            return dict(raw)
        except Exception:
            return {}

    @staticmethod
    def _extract_contracts(resp: Any) -> list:
        if isinstance(resp, list):
            return resp
        if not isinstance(resp, dict):
            return []
        for top in ("data", "contracts", "instruments", "items"):
            val = resp.get(top)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for sub in ("contracts", "instruments", "items"):
                    subval = val.get(sub)
                    if isinstance(subval, list):
                        return subval
        return []

    @staticmethod
    def _index_candidates_for_segment(segment: str) -> list[dict[str, str]]:
        if segment.startswith("BSE"):
            return [
                i for i in KNOWN_INDEXES if i["key"].startswith("BSE_INDEX")
            ]
        if segment.startswith("NSE"):
            return [
                i for i in KNOWN_INDEXES if i["key"].startswith("NSE_INDEX")
            ]
        return list(KNOWN_INDEXES)

    def _get_contracts_for_index_expiry(
        self, index_key: str, expiry_iso: str
    ) -> list:
        cache_key = (index_key, expiry_iso)
        if cache_key in self._expired_contracts_cache:
            return self._expired_contracts_cache[cache_key]
        try:
            resp = expired_instruments_service.get_expired_option_contracts(
                index_key, expiry_iso
            )
            contracts = self._extract_contracts(resp)
        except Exception:
            logger.warning(
                "[sr-export] get_expired_option_contracts failed for %s @ %s",
                index_key,
                expiry_iso,
                exc_info=True,
            )
            contracts = []
        self._expired_contracts_cache[cache_key] = contracts
        return contracts

    def _resolve_expired_contract_meta(
        self, expired_key: str
    ) -> dict | None:
        """
        Best-effort lookup of the full expired-contract metadata.
        Mirrors routes/expired_chart_routes._resolve_contract_meta.

        Returns the resolved contract dict (with trading_symbol,
        strike_price, instrument_type, lot_size, tick_size, ...) or
        None when nothing could be resolved.
        """
        # Cache
        if expired_key in self._expired_meta_cache:
            cached = self._expired_meta_cache[expired_key]
            return dict(cached) if cached else None

        # ---- 1. Direct method on the service ---------------------- #
        for method_name in (
            "get_contract_by_key",
            "get_contract_meta",
            "get_contract",
        ):
            method = getattr(
                expired_instruments_service, method_name, None
            )
            if not callable(method):
                continue
            try:
                result = method(expired_key)
            except Exception:
                logger.debug(
                    "[sr-export] %s failed for %s",
                    method_name,
                    expired_key,
                    exc_info=True,
                )
                continue
            d = self._safe_dict(result)
            if d and d.get("instrument_key") in (None, expired_key):
                self._expired_meta_cache[expired_key] = dict(d)
                return d

        # ---- 2. Any dict-shaped cache on the service -------------- #
        for attr in (
            "_contracts_cache", "contracts_cache",
            "_cache",          "cache",
            "_contracts",      "_by_key",
            "_meta_cache",     "meta_cache",
        ):
            cache = getattr(expired_instruments_service, attr, None)
            if not isinstance(cache, dict):
                continue
            candidate = cache.get(expired_key)
            if not candidate:
                continue
            d = self._safe_dict(candidate)
            if d:
                self._expired_meta_cache[expired_key] = dict(d)
                return d

        # ---- 3. Fetch contracts for each index candidate ---------- #
        parts = expired_key.split("|")
        if len(parts) < 3:
            self._expired_meta_cache[expired_key] = None
            return None

        segment = parts[0]
        id_str = parts[1]
        expiry_suffix = parts[2]
        expiry_iso = self._expiry_suffix_to_iso(expiry_suffix)
        if not expiry_iso:
            self._expired_meta_cache[expired_key] = None
            return None

        for idx in self._index_candidates_for_segment(segment):
            contracts = self._get_contracts_for_index_expiry(
                idx["key"], expiry_iso
            )
            for c in contracts:
                cd = self._safe_dict(c)
                if not cd:
                    continue
                ck = str(cd.get("instrument_key") or "")
                cet = str(cd.get("exchange_token") or "")
                cex = str(cd.get("expiry") or "")
                # Exact key match wins outright
                if ck == expired_key:
                    self._expired_meta_cache[expired_key] = dict(cd)
                    return cd
                # Match on exchange_token (id) + optionally expiry
                if cet and cet == id_str:
                    if not cex or cex.startswith(expiry_iso):
                        self._expired_meta_cache[expired_key] = dict(cd)
                        return cd

        self._expired_meta_cache[expired_key] = None
        return None

    # ================================================================== #
    # EXPIRED META BUILDER                                              #
    # ================================================================== #
    def _build_expired_meta(
        self,
        exchange: str,
        id_int: int,
        expiry_suffix: str,
        role: str,
        all_map: dict[str, dict] | None = None,
    ) -> dict:
        base_key = f"{exchange}|{id_int}"
        expired_key = f"{base_key}|{expiry_suffix}"
        expiry_iso = self._expiry_suffix_to_iso(expiry_suffix)
        parity_type = self._strike_type_from_id(id_int)

        resolved = self._resolve_expired_contract_meta(expired_key)

        # ---- Determine strike_type: prefer resolved, else parity -- #
        strike_type = parity_type
        if resolved:
            resolved_type = str(
                resolved.get("instrument_type")
                or resolved.get("strike_type")
                or ""
            ).strip().upper()
            if resolved_type in ("CE", "PE"):
                if resolved_type != parity_type:
                    logger.warning(
                        "[sr-export] type conflict for %s: parity=%s "
                        "resolved=%s — using resolved instrument_type",
                        expired_key,
                        parity_type,
                        resolved_type,
                    )
                strike_type = resolved_type

        # ---- Extract enriched fields from resolved ---------------- #
        underlying: str | None = None
        strike_price: Any = None
        trading_symbol: str | None = None
        relative_path: str | None = None
        lot_size: Any = None
        tick_size: Any = None

        if resolved:
            underlying = (
                resolved.get("underlying_symbol")
                or resolved.get("underlying")
                or resolved.get("name")
            )
            strike_price = resolved.get("strike_price")
            trading_symbol = resolved.get("trading_symbol")
            relative_path = resolved.get("relative_path")
            lot_size = resolved.get("lot_size")
            tick_size = resolved.get("tick_size")

        # ---- Secondary enrichment from all_instruments.json ------- #
        if all_map and base_key in all_map:
            entry = all_map[base_key]
            if not underlying and entry.get("underlying"):
                underlying = entry["underlying"]
            if strike_price is None and entry.get("strike_price") is not None:
                strike_price = entry["strike_price"]
            if not trading_symbol and entry.get("trading_symbol"):
                trading_symbol = entry["trading_symbol"]
            if not relative_path and entry.get("relative_path"):
                relative_path = entry["relative_path"]

        # ---- Derive relative_path if we have enough info ---------- #
        if (
            not relative_path
            and underlying
            and strike_price is not None
            and strike_type
        ):
            try:
                ul = str(underlying).strip().lower()
                sp = float(strike_price)
                sp_str = str(int(sp)) if sp == int(sp) else str(sp)
                relative_path = (
                    f"{ul}/{sp_str}_{strike_type}/all_instruments.json"
                )
            except Exception:
                pass

        # ---- Last resort trading_symbol --------------------------- #
        if not trading_symbol:
            trading_symbol = expired_key

        return {
            "instrument_key": expired_key,
            "base_instrument_key": base_key,
            "expired_instrument_key": expired_key,
            "strike_type": strike_type,
            "expiry": expiry_iso,
            "expiry_suffix": expiry_suffix,
            "underlying": underlying,
            "strike_price": strike_price,
            "trading_symbol": trading_symbol,
            "relative_path": relative_path,
            "lot_size": lot_size,
            "tick_size": tick_size,
            "is_expired": True,
            "role": role,
        }

    # ================================================================== #
    # LIVE METADATA                                                     #
    # ================================================================== #
    def _resolve_meta_live(
        self, all_map: dict[str, dict], instrument_key: str
    ) -> dict:
        meta = all_map.get(instrument_key)
        if meta is None:
            raise ValueError(
                f"instrument_key {instrument_key} not found in "
                f"{ALL_INSTRUMENTS_PATH}"
            )
        return dict(meta)

    def _find_opposite_live(
        self, meta: dict, all_map: dict[str, dict]
    ) -> dict | None:
        underlying = str(meta.get("underlying") or "").strip().upper()
        strike = meta.get("strike_price")
        expiry = str(meta.get("expiry") or "").strip()
        strike_type = str(meta.get("strike_type") or "").strip().upper()
        if not (underlying and strike is not None and expiry and strike_type):
            return None
        opposite_type = "PE" if strike_type == "CE" else "CE"

        for entry in all_map.values():
            if not isinstance(entry, dict):
                continue
            if (
                str(entry.get("underlying") or "").strip().upper()
                == underlying
                and entry.get("strike_price") == strike
                and str(entry.get("expiry") or "").strip() == expiry
                and str(entry.get("strike_type") or "").strip().upper()
                == opposite_type
            ):
                return dict(entry)
        return None

    # ================================================================== #
    # LIVE CANDLE FETCHERS                                              #
    # ================================================================== #
    def _fetch_historical_live(
        self, instrument_key: str, days: int = LOOKBACK_DAYS
    ) -> list[dict]:
        from upstox_client.api.history_v3_api import HistoryV3Api

        api = HistoryV3Api(self._upstox_client())
        today = datetime.now(IST).date()
        to_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")

        for method_name in (
            "get_historical_candle_data1",
            "get_historical_candle_data",
        ):
            fn = getattr(api, method_name, None)
            if fn is None:
                continue
            try:
                resp = fn(
                    instrument_key=instrument_key,
                    unit="minutes",
                    interval="1",
                    to_date=to_date,
                    from_date=from_date,
                )
                return self._normalise_candles(resp)
            except TypeError:
                try:
                    resp = fn(
                        instrument_key,
                        "minutes",
                        "1",
                        to_date=to_date,
                        from_date=from_date,
                    )
                    return self._normalise_candles(resp)
                except Exception as exc:
                    logger.warning(
                        "[sr-export] %s (positional) failed for %s: %s",
                        method_name,
                        instrument_key,
                        exc,
                    )
            except Exception as exc:
                logger.warning(
                    "[sr-export] %s failed for %s: %s",
                    method_name,
                    instrument_key,
                    exc,
                )

        logger.error(
            "[sr-export] all historical methods failed for %s",
            instrument_key,
        )
        return []

    def _fetch_intraday_live(self, instrument_key: str) -> list[dict]:
        from upstox_client.api.history_v3_api import HistoryV3Api

        api = HistoryV3Api(self._upstox_client())
        for method_name in (
            "get_intra_day_candle_data",
            "get_intraday_candle_data",
        ):
            fn = getattr(api, method_name, None)
            if fn is None:
                continue
            try:
                resp = fn(
                    instrument_key=instrument_key,
                    unit="minutes",
                    interval="1",
                )
                return self._normalise_candles(resp)
            except TypeError:
                try:
                    resp = fn(instrument_key, "minutes", "1")
                    return self._normalise_candles(resp)
                except Exception as exc:
                    logger.warning(
                        "[sr-export] %s (positional) failed for %s: %s",
                        method_name,
                        instrument_key,
                        exc,
                    )
            except Exception as exc:
                logger.warning(
                    "[sr-export] %s failed for %s: %s",
                    method_name,
                    instrument_key,
                    exc,
                )
        logger.error(
            "[sr-export] all intraday methods failed for %s",
            instrument_key,
        )
        return []

    # ================================================================== #
    # EXPIRED CANDLE FETCHER                                            #
    # ================================================================== #
    def _fetch_historical_expired(
        self,
        expired_instrument_key: str,
        days: int,
        end_date: date,
    ) -> list[dict]:
        to_date_str = end_date.strftime("%Y-%m-%d")
        from_date_str = (end_date - timedelta(days=days)).strftime(
            "%Y-%m-%d"
        )

        for method_name in (
            "get_expired_historical_candles",
            "get_expired_historical_candle_data",
        ):
            fn = getattr(expired_instruments_service, method_name, None)
            if fn is None:
                continue

            # kwargs shape
            try:
                resp = fn(
                    instrument_key=expired_instrument_key,
                    interval="1minute",
                    from_date=from_date_str,
                    to_date=to_date_str,
                )
                candles = self._normalise_expired_candles(resp)
                if candles:
                    return candles
            except TypeError:
                pass
            except Exception as exc:
                logger.warning(
                    "[sr-export] %s (kwargs) failed for %s: %s",
                    method_name,
                    expired_instrument_key,
                    exc,
                )

            # positional shape
            try:
                resp = fn(
                    expired_instrument_key,
                    "1minute",
                    from_date_str,
                    to_date_str,
                )
                candles = self._normalise_expired_candles(resp)
                if candles:
                    return candles
            except TypeError:
                pass
            except Exception as exc:
                logger.warning(
                    "[sr-export] %s (positional) failed for %s: %s",
                    method_name,
                    expired_instrument_key,
                    exc,
                )

        logger.error(
            "[sr-export] all expired historical methods failed for %s",
            expired_instrument_key,
        )
        return []

    # ================================================================== #
    # NORMALISERS                                                        #
    # ================================================================== #
    @staticmethod
    def _tuple_to_candle(row: Any) -> dict | None:
        try:
            ts = str(row[0])
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
            v = (
                float(row[5])
                if len(row) > 5 and row[5] is not None
                else 0.0
            )
            return {
                "time": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        except Exception:
            return None

    @staticmethod
    def _dict_to_candle(row: dict) -> dict | None:
        try:
            ts = (
                row.get("timestamp")
                or row.get("time")
                or row.get("date")
                or row.get("datetime")
            )
            if ts is None:
                return None
            return {
                "time": str(ts),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0) or 0),
            }
        except Exception:
            return None

    def _normalise_candles(self, response: Any) -> list[dict]:
        raw: list = []
        try:
            data = getattr(response, "data", response)
            if isinstance(data, list):
                raw = data
            elif hasattr(data, "candles"):
                raw = list(getattr(data, "candles") or [])
            elif isinstance(data, dict):
                raw = list(data.get("candles") or [])
            elif hasattr(data, "to_dict"):
                raw = list((data.to_dict() or {}).get("candles") or [])
        except Exception:
            raw = []

        out: list[dict] = []
        for row in raw:
            if isinstance(row, dict):
                candle = self._dict_to_candle(row)
            else:
                candle = self._tuple_to_candle(row)
            if candle is not None:
                out.append(candle)
        return out

    def _normalise_expired_candles(self, response: Any) -> list[dict]:
        raw: list = []
        try:
            if isinstance(response, list):
                raw = response
            elif isinstance(response, dict):
                raw = (
                    response.get("candles")
                    or (response.get("data") or {}).get("candles")
                    or []
                )
            else:
                data = getattr(response, "data", response)
                if isinstance(data, list):
                    raw = data
                elif hasattr(data, "candles"):
                    raw = list(getattr(data, "candles") or [])
                elif isinstance(data, dict):
                    raw = list(data.get("candles") or [])
                elif hasattr(data, "to_dict"):
                    raw = list(
                        (data.to_dict() or {}).get("candles") or []
                    )
        except Exception:
            raw = []

        out: list[dict] = []
        for row in raw:
            if isinstance(row, dict):
                candle = self._dict_to_candle(row)
            else:
                candle = self._tuple_to_candle(row)
            if candle is not None:
                out.append(candle)
        return out

    # ================================================================== #
    # MERGE                                                             #
    # ================================================================== #
    @staticmethod
    def _minute_key(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            dt = dt.astimezone(IST)
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return ts[:16]

    def _merge(
        self, historical: list[dict], intraday: list[dict]
    ) -> list[dict]:
        by_minute: dict[str, dict] = {}
        for c in historical:
            by_minute[self._minute_key(c["time"])] = c
        for c in intraday:
            by_minute[self._minute_key(c["time"])] = c

        merged = [by_minute[k] for k in sorted(by_minute.keys())]
        return [
            c
            for c in merged
            if c["high"] > 0
            and c["low"] > 0
            and not (
                c["open"] == 0
                and c["high"] == 0
                and c["low"] == 0
                and c["close"] == 0
            )
        ]

    # ================================================================== #
    # PIVOT DETECTION                                                   #
    # ================================================================== #
    def _compute_sr_levels(self, candles: list[dict]) -> list[dict]:
        levels: list[dict] = []
        n = len(candles)
        left, right = LEFT_BARS, RIGHT_BARS
        if n < left + right + 1:
            return levels

        for i in range(left, n - right):
            cur = candles[i]
            is_res = True
            is_sup = True

            for j in range(i - left, i):
                if is_res and cur["high"] <= candles[j]["high"]:
                    is_res = False
                if is_sup and cur["low"] >= candles[j]["low"]:
                    is_sup = False
                if not is_res and not is_sup:
                    break

            if is_res or is_sup:
                for j in range(i + 1, i + right + 1):
                    if is_res and cur["high"] <= candles[j]["high"]:
                        is_res = False
                    if is_sup and cur["low"] >= candles[j]["low"]:
                        is_sup = False
                    if not is_res and not is_sup:
                        break

            if not (is_res or is_sup):
                continue

            confirmed_idx = i + right
            confirmed_candle = (
                candles[confirmed_idx] if confirmed_idx < n else None
            )

            if is_res:
                levels.append(
                    {
                        "level": "resistance",
                        "price": round(cur["high"], 2),
                        "pivot_bar_index": i,
                        "pivot_time": cur["time"],
                        "confirmed_bar_index": confirmed_idx,
                        "confirmed_time": (
                            confirmed_candle["time"]
                            if confirmed_candle
                            else None
                        ),
                        "left_bars": left,
                        "right_bars": right,
                    }
                )
            if is_sup:
                levels.append(
                    {
                        "level": "support",
                        "price": round(cur["low"], 2),
                        "pivot_bar_index": i,
                        "pivot_time": cur["time"],
                        "confirmed_bar_index": confirmed_idx,
                        "confirmed_time": (
                            confirmed_candle["time"]
                            if confirmed_candle
                            else None
                        ),
                        "left_bars": left,
                        "right_bars": right,
                    }
                )

        levels.sort(key=lambda x: (x["pivot_time"] or "", x["level"]))
        return levels

    # ================================================================== #
    # EARLY SESSION SR CHECK (09:15 - 09:30 IST)                        #
    # ================================================================== #
    @staticmethod
    def _parse_ist_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    def _check_early_sr_level(
        self,
        payload: dict,
        check_date: date,
    ) -> dict:
        """
        Check SR levels for the first 15 minutes of the market day:

            09:15:00 <= pivot_time     < 09:30:00 (IST, on check_date)
            09:15:00 <= confirmed_time < 09:30:00 (IST, on check_date)

        Both pivot and confirmation must fall on `check_date`.

        If multiple levels qualify, the OLDEST wins
        (earliest pivot_time, then earliest confirmed_time).
        """
        start_dt = datetime(
            check_date.year,
            check_date.month,
            check_date.day,
            9,
            15,
            tzinfo=IST,
        )
        end_dt = datetime(
            check_date.year,
            check_date.month,
            check_date.day,
            9,
            30,
            tzinfo=IST,
        )

        candidates: list[tuple[datetime, datetime, dict]] = []

        for level in payload.get("sr_levels") or []:
            if not isinstance(level, dict):
                continue

            pivot_dt = self._parse_ist_datetime(level.get("pivot_time"))
            confirmed_dt = self._parse_ist_datetime(
                level.get("confirmed_time")
            )

            if pivot_dt is None or confirmed_dt is None:
                continue

            # Both must be on the check_date.
            if pivot_dt.date() != check_date:
                continue
            if confirmed_dt.date() != check_date:
                continue

            # Both must be inside the first 15 minutes of the session.
            if pivot_dt < start_dt or confirmed_dt < start_dt:
                continue
            if pivot_dt >= end_dt or confirmed_dt >= end_dt:
                continue

            candidates.append((pivot_dt, confirmed_dt, level))

        # Oldest first.
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
                str(item[2].get("level") or ""),
                float(item[2].get("price") or 0),
            )
        )

        base_result = {
            "check_date": check_date.strftime("%Y-%m-%d"),
            "session_start": "09:15:00+05:30",
            "session_end": "09:30:00+05:30",
        }

        if not candidates:
            return {
                **base_result,
                "sr_level_present": False,
                "level": None,
                "level_type": None,
                "price": None,
                "pivot_time": None,
                "confirmation_time": None,
                "pivot_bar_index": None,
                "confirmed_bar_index": None,
                "valid_levels_count": 0,
            }

        _, _, selected = candidates[0]

        return {
            **base_result,
            "sr_level_present": True,
            "level": selected,
            "level_type": selected.get("level"),
            "price": selected.get("price"),
            "pivot_time": selected.get("pivot_time"),
            "confirmation_time": selected.get("confirmed_time"),
            "pivot_bar_index": selected.get("pivot_bar_index"),
            "confirmed_bar_index": selected.get("confirmed_bar_index"),
            "valid_levels_count": len(candidates),
        }

    # ================================================================== #
    # PAYLOAD                                                           #
    # ================================================================== #
    def _build_payload(
        self,
        meta: dict,
        candles: list[dict],
        sr_levels: list[dict],
        expired: bool,
        end_date: date | None,
    ) -> dict:
        metadata: dict = {
            "instrument_key": meta.get("instrument_key"),
            "trading_symbol": meta.get("trading_symbol"),
            "underlying": meta.get("underlying"),
            "strike_price": meta.get("strike_price"),
            "strike_type": meta.get("strike_type"),
            "expiry": meta.get("expiry"),
            "relative_path": meta.get("relative_path"),
        }

        # Expired-only enrichment
        if meta.get("role"):
            metadata["role"] = meta["role"]
        if meta.get("lot_size") is not None:
            metadata["lot_size"] = meta["lot_size"]
        if meta.get("tick_size") is not None:
            metadata["tick_size"] = meta["tick_size"]

        if expired:
            metadata.update(
                {
                    "is_expired": True,
                    "base_instrument_key": meta.get(
                        "base_instrument_key"
                    ),
                    "expired_instrument_key": meta.get(
                        "expired_instrument_key"
                    ),
                    "expiry_suffix": meta.get("expiry_suffix"),
                }
            )

        params: dict = {
            "lookback_days": LOOKBACK_DAYS,
            "left_bars": LEFT_BARS,
            "right_bars": RIGHT_BARS,
            "mode": "expired" if expired else "live",
        }
        if expired and end_date is not None:
            params["end_date"] = end_date.strftime("%Y-%m-%d")
            params["from_date"] = (
                end_date - timedelta(days=LOOKBACK_DAYS)
            ).strftime("%Y-%m-%d")

        return {
            "metadata": metadata,
            "candles": candles,
            "candles_count": len(candles),
            "sr_levels": sr_levels,
            "sr_levels_count": len(sr_levels),
            "generated_at": datetime.now(IST).isoformat(),
            "params": params,
        }

    # ================================================================== #
    # PER-INSTRUMENT PIPELINE                                           #
    # ================================================================== #
    def _process_one(
        self,
        meta: dict,
        expired: bool,
        end_date: date | None,
    ) -> dict:
        if expired:
            fetch_key = (
                meta.get("expired_instrument_key")
                or meta.get("instrument_key")
            )
            logger.info(
                "[sr-export] EXPIRED fetch=%s strike_type=%s expiry=%s "
                "trading_symbol=%s",
                fetch_key,
                meta.get("strike_type"),
                meta.get("expiry"),
                meta.get("trading_symbol"),
            )
            if end_date is None:
                raise ValueError(
                    "end_date is required when expired=True"
                )
            historical = self._fetch_historical_expired(
                fetch_key, LOOKBACK_DAYS, end_date
            )
            intraday: list[dict] = []
        else:
            fetch_key = meta["instrument_key"]
            logger.info(
                "[sr-export] LIVE fetch=%s strike_type=%s",
                fetch_key,
                meta.get("strike_type"),
            )
            historical = self._fetch_historical_live(
                fetch_key, LOOKBACK_DAYS
            )
            intraday = self._fetch_intraday_live(fetch_key)

        merged = self._merge(historical, intraday)
        levels = self._compute_sr_levels(merged)

        logger.info(
            "[sr-export] %s candles: hist=%d intra=%d merged=%d levels=%d",
            fetch_key,
            len(historical),
            len(intraday),
            len(merged),
            len(levels),
        )
        return self._build_payload(
            meta, merged, levels, expired, end_date
        )

    # ================================================================== #
    # PUBLIC                                                            #
    # ================================================================== #
    @staticmethod
    def _parse_end_date(value: str | date | datetime) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        s = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(
            f"Invalid end_date '{value}'. Expected YYYY-MM-DD "
            f"(e.g. 2026-10-21) or DD-MM-YYYY (e.g. 21-10-2026)."
        )

    def run(
        self,
        instrument_key: str,
        expired: bool | None = None,
        end_date: str | date | datetime | None = None,
        check_date: str | date | datetime | None = None,
    ) -> dict:
        """
        Parameters
        ----------
        instrument_key : str
            Live key "NSE_FO|<id>" or expired key "NSE_FO|<id>|DD-MM-YYYY".
        expired : bool | None, default None
            None / False -> live mode. True -> expired mode (end_date
            becomes mandatory).
        end_date : str | date | datetime | None, default None
            None for live mode. For expired mode: "YYYY-MM-DD" or
            "DD-MM-YYYY". Mandatory when expired=True.
        check_date : str | date | datetime | None, default None
            Early-session check date. None -> today (IST).
            When expired=True, this is FORCED to end_date
            (any passed value is ignored with a warning log).
        """
        end_date_obj: date | None = None
        if end_date is not None:
            end_date_obj = self._parse_end_date(end_date)

        if expired and end_date_obj is None:
            raise ValueError(
                "expired=True requires end_date (YYYY-MM-DD)"
            )

        # ------------------------------------------------------------------
        # Resolve check_date:
        #   - expired : FORCED to end_date (ignore caller-supplied check_date)
        #   - live    : caller-supplied check_date, else today (IST)
        # check_date is "mandatory" in the sense that it is always
        # resolved to a real date — never left as None.
        # ------------------------------------------------------------------
        if expired:
            check_date_obj: date = end_date_obj  # type: ignore[assignment]
            if check_date is not None:
                logger.info(
                    "[sr-export] expired=True: ignoring check_date=%s "
                    "and using end_date=%s",
                    check_date,
                    end_date_obj.strftime("%Y-%m-%d"),
                )
        else:
            check_date_obj = (
                self._parse_end_date(check_date)
                if check_date is not None
                else datetime.now(IST).date()
            )

        if not expired:
            parts = instrument_key.split("|")
            if len(parts) > 2:
                raise ValueError(
                    f"'{instrument_key}' looks like an expired key "
                    f"({len(parts)} pipe-separated parts). Pass "
                    f"expired=True and end_date='YYYY-MM-DD'."
                )

        with self._lock:
            ISOLATION_DIR.mkdir(parents=True, exist_ok=True)
            # Defensive: keep ISOLATION_SR_DIR alive even if an operator
            # aliases it to a distinct directory in future.
            if str(ISOLATION_SR_DIR) != str(ISOLATION_DIR):
                ISOLATION_SR_DIR.mkdir(parents=True, exist_ok=True)

            # Reset per-run caches — fresh resolution each invocation
            self._expired_meta_cache = {}
            self._expired_contracts_cache = {}

            if expired:
                # -------- EXPIRED PATH (no registry lookup) -------- #
                (
                    base_key,
                    id_int,
                    expiry_suffix,
                ) = self._parse_expired_key(instrument_key)
                exchange = base_key.split("|")[0]

                # Enrichment source — optional, never authoritative
                all_map = self._load_all_instruments_safe()

                isolated_meta = self._build_expired_meta(
                    exchange,
                    id_int,
                    expiry_suffix,
                    role="isolated",
                    all_map=all_map,
                )

                opposite_id_int = self._opposite_id(id_int)
                opposite_meta = self._build_expired_meta(
                    exchange,
                    opposite_id_int,
                    expiry_suffix,
                    role="opposite",
                    all_map=all_map,
                )

                logger.info(
                    "[sr-export] EXPIRED PAIR: isolated=%s (%s | %s) "
                    "opposite=%s (%s | %s) end_date=%s",
                    isolated_meta["expired_instrument_key"],
                    isolated_meta["strike_type"],
                    isolated_meta["trading_symbol"],
                    opposite_meta["expired_instrument_key"],
                    opposite_meta["strike_type"],
                    opposite_meta["trading_symbol"],
                    end_date_obj.strftime("%Y-%m-%d"),
                )

                isolated_payload = self._process_one(
                    isolated_meta, expired=True, end_date=end_date_obj
                )
                opposite_payload: dict | None = self._process_one(
                    opposite_meta, expired=True, end_date=end_date_obj
                )
            else:
                # -------- LIVE PATH -------- #
                all_map = self._load_all_instruments()
                meta = self._resolve_meta_live(all_map, instrument_key)
                opposite = self._find_opposite_live(meta, all_map)

                if opposite is None:
                    logger.warning(
                        "[sr-export] no opposite key for %s "
                        "(same underlying/strike/expiry, flipped CE<->PE)",
                        instrument_key,
                    )

                isolated_payload = self._process_one(
                    meta, expired=False, end_date=None
                )
                opposite_payload = (
                    self._process_one(
                        opposite, expired=False, end_date=None
                    )
                    if opposite is not None
                    else None
                )

            # -------- WRITE ISOLATED (canonical) -------- #
            isolated_path = ISOLATION_DIR / ISOLATED_FILENAME
            with open(isolated_path, "w", encoding="utf-8") as f:
                json.dump(isolated_payload, f, indent=2)
            logger.info("[sr-export] wrote %s", isolated_path)

            # -------- WRITE OPPOSITE (canonical) -------- #
            opposite_path: str | None = None
            if opposite_payload is not None:
                op_path = ISOLATION_DIR / OPPOSITE_FILENAME
                with open(op_path, "w", encoding="utf-8") as f:
                    json.dump(opposite_payload, f, indent=2)
                opposite_path = str(op_path)
                logger.info("[sr-export] wrote %s", op_path)

            # -------- WRITE PER-INSTRUMENT LIVE SR SNAPSHOTS -------- #
            # 2026-11-06 — atomic, thread-safe. Filenames match the
            # promotion service's _backfill_isolation_sr_baseline(), so
            # a promotion-triggered backfill and an export-triggered
            # snapshot overwrite each other cleanly for the same
            # instrument, and consumers only ever see a complete file.
            isolated_sr_write = self._write_live_sr_snapshot(
                isolated_payload
            )
            opposite_sr_write = (
                self._write_live_sr_snapshot(opposite_payload)
                if opposite_payload is not None
                else {"path": None, "error": None}
            )

            # -------- EARLY 09:15-09:30 SR CHECK -------- #
            # Read back from disk so the check always reflects exactly
            # what was just persisted to isolated_instrument.json.
            with open(isolated_path, "r", encoding="utf-8") as f:
                isolated_for_check = json.load(f)

            early_sr_check = self._check_early_sr_level(
                isolated_for_check,
                check_date_obj,
            )

            early_check_path = ISOLATION_DIR / EARLY_CHECK_FILENAME
            with open(early_check_path, "w", encoding="utf-8") as f:
                json.dump(early_sr_check, f, indent=2)
            logger.info(
                "[sr-export] wrote %s (sr_level_present=%s, "
                "valid_levels=%d, check_date=%s)",
                early_check_path,
                early_sr_check["sr_level_present"],
                early_sr_check["valid_levels_count"],
                early_sr_check["check_date"],
            )

            return {
                "status": "ok",
                "instrument_key": instrument_key,
                "expired": expired,
                "end_date": (
                    end_date_obj.strftime("%Y-%m-%d")
                    if end_date_obj
                    else None
                ),
                "check_date": check_date_obj.strftime("%Y-%m-%d"),
                "token_source": self._token_source,
                "early_sr_check": {
                    "path": str(early_check_path),
                    **early_sr_check,
                },
                "isolated": {
                    "path": str(isolated_path),
                    "live_sr_path": isolated_sr_write["path"],
                    "live_sr_error": isolated_sr_write["error"],
                    "instrument_key": isolated_payload["metadata"].get(
                        "instrument_key"
                    ),
                    "base_instrument_key": isolated_payload["metadata"].get(
                        "base_instrument_key"
                    ),
                    "strike_type": isolated_payload["metadata"].get(
                        "strike_type"
                    ),
                    "expiry": isolated_payload["metadata"].get("expiry"),
                    "trading_symbol": isolated_payload["metadata"].get(
                        "trading_symbol"
                    ),
                    "candles": isolated_payload["candles_count"],
                    "sr_levels": isolated_payload["sr_levels_count"],
                },
                "opposite": (
                    None
                    if opposite_payload is None
                    else {
                        "path": opposite_path,
                        "live_sr_path": opposite_sr_write["path"],
                        "live_sr_error": opposite_sr_write["error"],
                        "instrument_key": opposite_payload[
                            "metadata"
                        ].get("instrument_key"),
                        "base_instrument_key": opposite_payload[
                            "metadata"
                        ].get("base_instrument_key"),
                        "strike_type": opposite_payload["metadata"].get(
                            "strike_type"
                        ),
                        "expiry": opposite_payload["metadata"].get(
                            "expiry"
                        ),
                        "trading_symbol": opposite_payload[
                            "metadata"
                        ].get("trading_symbol"),
                        "candles": opposite_payload["candles_count"],
                        "sr_levels": opposite_payload["sr_levels_count"],
                    }
                ),
            }


# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
isolated_instrument_sr_export_service = IsolatedInstrumentSRExportService()


# # --------------------------------------------------------------------------- #
# # CLI (static — edit the four constants below to run)                         #
# # --------------------------------------------------------------------------- #
# if __name__ == "__main__":
#     import sys

#     # ============================================================ #
#     # EDIT THESE FOUR LINES TO RUN                                 #
#     # ============================================================ #
#     INSTRUMENT_KEY = "NSE_FO|53871"   # live or expired key
#     EXPIRED = None                    # None -> live; True -> expired
#     END_DATE = None                   # None for live; required if EXPIRED=True
#     CHECK_DATE = None         # None -> today (IST); forced to END_DATE if EXPIRED=True

#     # ------------------------------------------------------------ #
#     # Examples                                                     #
#     # ------------------------------------------------------------ #
#     #
#     # Live (uses all_instruments.json, no end_date):
#     #   INSTRUMENT_KEY = "NSE_FO|53749"
#     #   EXPIRED = None                 # or False
#     #   END_DATE = None
#     #   CHECK_DATE = None              # defaults to today (IST)
#     #   CHECK_DATE = "30-10-2026"      # explicit check_date
#     #
#     # Expired — CE isolated (even id 51356 -> CE), PE opposite (51357):
#     #   INSTRUMENT_KEY = "NSE_FO|51356|27-10-2026"
#     #   EXPIRED = True
#     #   END_DATE = "2026-10-21"
#     #   CHECK_DATE = None              # ignored, forced to END_DATE
#     #
#     # Expired — PE isolated (odd id 51357 -> PE), CE opposite (51356):
#     #   INSTRUMENT_KEY = "NSE_FO|51357|27-10-2026"
#     #   EXPIRED = True
#     #   END_DATE = "2026-10-21"
#     #   CHECK_DATE = None              # ignored, forced to END_DATE
#     # ============================================================ #

#     try:
#         result = isolated_instrument_sr_export_service.run(
#             INSTRUMENT_KEY,
#             expired=EXPIRED,
#             end_date=END_DATE,
#             check_date=CHECK_DATE,
#         )
#         print(json.dumps(result, indent=2, default=str))
#     except Exception as exc:
#         logger.exception(
#             "sr-export failed for %s", INSTRUMENT_KEY
#         )
#         print(
#             json.dumps(
#                 {
#                     "status": "error",
#                     "instrument_key": INSTRUMENT_KEY,
#                     "expired": EXPIRED,
#                     "end_date": END_DATE,
#                     "check_date": CHECK_DATE,
#                     "error": f"{type(exc).__name__}: {exc}",
#                 },
#                 indent=2,
#             )
#         )
#         sys.exit(1)