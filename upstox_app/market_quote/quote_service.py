"""
upstox_app/market_quote/quote_service.py

Batch orchestration on top of `fetch_quote.fetch_ohlc_raw`.

Default interval is `I1` (1 minute). Human aliases like `1m`, `1minute`,
`1d` are accepted and normalised by `fetch_quote.normalise_interval`.

Response key remapping
----------------------
The MarketQuoteV3 OHLC endpoint returns entries keyed by a trading
symbol form (`NSE_FO:NIFTY26O0624150CE`) rather than the instrument key
form we send (`NSE_FO|40809`). Each returned entry carries an
`instrument_token` field that holds the canonical `NSE_FO|40809`. We use
that field (and a numeric-suffix fallback) to remap every entry back to
the key the caller requested. Callers therefore always read
`result["data"]["NSE_FO|40809"]`.

Never raises. Every failure is logged as a single compact line.
"""
import random
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.config import core_config
from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.market_quote.fetch_quote import (
    DEFAULT_INTERVAL,
    QuoteFetchError,
    fetch_ohlc_raw,
    normalise_interval,
)

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _cfg(name: str, default: Any) -> Any:
    return getattr(upstox_config, name, default)


def _resolve_interval(interval: Optional[str]) -> str:
    if interval:
        return normalise_interval(interval)
    configured = _cfg("QUOTES_INTERVAL", DEFAULT_INTERVAL)
    return normalise_interval(configured)


# ---------------------------------------------------------------------------
# key matching helpers
# ---------------------------------------------------------------------------


def _id_suffix(key: str) -> str:
    """
    Return the trailing identifier of an instrument key, ignoring the
    separator style. Handles both `NSE_FO|40809` and
    `NSE_FO:NIFTY26O0624150CE`.

    For `NSE_FO|40809`          -> "40809"
    For `NSE_FO:NIFTY26O0624150CE` -> "NIFTY26O0624150CE"
    """
    if not key:
        return ""
    for sep in ("|", ":"):
        if sep in key:
            return key.rsplit(sep, 1)[-1]
    return key


def _numeric_suffix(key: str) -> str:
    """Return trailing digits of the last segment, or '' if none."""
    tail = _id_suffix(key)
    digits = ""
    for ch in reversed(tail):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return digits


def _build_key_index(requested: List[str]) -> Dict[str, str]:
    """
    Build a lookup from every plausible form of a requested key to the
    key the caller actually asked for.

    Example:
        requested = ["NSE_FO|40809", "NSE_FO|40823"]
        index = {
            "NSE_FO|40809": "NSE_FO|40809",
            "40809": "NSE_FO|40809",
            "NSE_FO|40823": "NSE_FO|40823",
            "40823": "NSE_FO|40823",
        }
    """
    index: Dict[str, str] = {}
    for want in requested:
        index[want] = want
        suffix = _id_suffix(want)
        if suffix:
            index[suffix] = want
        num = _numeric_suffix(want)
        if num:
            index[num] = want
    return index


def _match_key(returned_key: str, entry: Any, key_index: Dict[str, str]) -> Optional[str]:
    """
    Return the requested key that matches this returned entry, or None.
    Tries, in order:
      1. Exact key match.
      2. `instrument_token` field inside the entry.
      3. Suffix of the returned key (after | or :).
      4. Numeric tail of the returned key.
    """
    # 1) exact
    if returned_key in key_index:
        return key_index[returned_key]

    # 2) instrument_token field
    if isinstance(entry, dict):
        token = entry.get("instrument_token")
        if isinstance(token, str) and token:
            if token in key_index:
                return key_index[token]
            suffix = _id_suffix(token)
            if suffix and suffix in key_index:
                return key_index[suffix]

    # 3) suffix of the returned key
    suffix = _id_suffix(returned_key)
    if suffix and suffix in key_index:
        return key_index[suffix]

    # 4) numeric tail
    num = _numeric_suffix(returned_key)
    if num and num in key_index:
        return key_index[num]

    return None


def _remap_response(
    raw_data: Dict[str, Any], requested: List[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Rekey `raw_data` so every entry is stored under the requested key.

    Returns (remapped_data, failed_keys).
    """
    key_index = _build_key_index(requested)
    remapped: Dict[str, Any] = {}
    matched_requested: set = set()

    for returned_key, entry in (raw_data or {}).items():
        wanted = _match_key(returned_key, entry, key_index)
        if wanted is None:
            # Unmatched: keep it under its returned key so nothing is lost.
            remapped[returned_key] = entry
            continue
        remapped[wanted] = entry
        matched_requested.add(wanted)

    failed = [k for k in requested if k not in matched_requested]
    return remapped, failed


# ---------------------------------------------------------------------------
# rate limiter
# ---------------------------------------------------------------------------


class QuoteRateLimiter:
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
        base = float(_cfg("QUOTES_RATE_LIMIT_BASE_COOLDOWN_SEC", 30.0))
        cap = float(_cfg("QUOTES_RATE_LIMIT_MAX_COOLDOWN_SEC", 300.0))
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
            "Quote rate limit hit | consecutive=%d | total=%d | cooldown=%.1fs "
            "(quote calls paused)",
            attempt, self._total_429s, backoff,
        )
        return backoff

    def note_success(self) -> None:
        with self._lock:
            if self._consecutive_429:
                logger.info(
                    "Quote rate-limit recovered | consecutive_429s_reset=%d",
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


_quote_limiter = QuoteRateLimiter()


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class QuoteService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---- internal ------------------------------------------------------
    def _call_one_batch(
        self,
        keys: List[str],
        interval: str,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        if not keys:
            return {}, []

        _quote_limiter.wait_if_cooling()
        delay = float(_cfg("QUOTES_REQUEST_DELAY_SEC", 0.2))
        if delay > 0:
            time.sleep(delay)

        joined = ",".join(keys)
        try:
            result = fetch_ohlc_raw(joined, interval=interval)
            _quote_limiter.note_success()
        except QuoteFetchError as exc:
            status = getattr(exc, "status_code", None)
            if status == 429:
                retry_after = None
                text = str(exc)
                if "retry_after" in text:
                    try:
                        retry_after = float(
                            text.split("retry_after")[-1].strip(" :=").split()[0]
                        )
                    except Exception:  # noqa: BLE001
                        retry_after = None
                _quote_limiter.note_429(retry_after)
                return {}, list(keys)
            logger.error(
                "quote_service::_call_one_batch failed | step=sdk call "
                "| batch_size=%d | interval=%s | status=%s | reason=%s",
                len(keys), interval, status, _short(exc),
            )
            return {}, list(keys)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "quote_service::_call_one_batch failed | step=sdk call "
                "| batch_size=%d | interval=%s | reason=%s",
                len(keys), interval, _short(exc),
            )
            return {}, list(keys)

        raw_data = result.get("data") or {}
        if not raw_data:
            return {}, list(keys)

        remapped, failed = _remap_response(raw_data, keys)

        if failed:
            logger.info(
                "quote_service::_call_one_batch | remap done | requested=%d "
                "| returned=%d | matched=%d | failed=%d",
                len(keys), len(raw_data), len(remapped), len(failed),
            )

        return remapped, failed

    def _fetch_with_fallback(
        self,
        keys: List[str],
        interval: str,
        depth: int = 0,
        max_depth: int = 4,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        if not keys:
            return {}, []

        data, failed = self._call_one_batch(keys, interval)
        if not failed:
            return data, []

        if depth >= max_depth or len(failed) == 1:
            if failed:
                logger.warning(
                    "quote_service | unrecoverable keys | depth=%d | count=%d | first=%s",
                    depth, len(failed), failed[0],
                )
            return data, failed

        mid = max(1, len(failed) // 2)
        left, right = failed[:mid], failed[mid:]

        data_a, failed_a = self._fetch_with_fallback(left, interval, depth + 1, max_depth)
        data_b, failed_b = self._fetch_with_fallback(right, interval, depth + 1, max_depth)

        merged = {**data, **data_a, **data_b}
        return merged, failed_a + failed_b

    # ---- public API ----------------------------------------------------
    def fetch_ohlc_batch(
        self,
        instrument_keys: Iterable[str],
        interval: Optional[str] = None,
    ) -> Dict[str, Any]:
        interval = _resolve_interval(interval)
        batch_size = max(1, int(_cfg("QUOTES_BATCH_SIZE", 10)))

        unique_keys = []
        seen = set()
        for k in instrument_keys or []:
            if not k:
                continue
            if k in seen:
                continue
            seen.add(k)
            unique_keys.append(k)

        total = len(unique_keys)
        if total == 0:
            return {
                "status": "empty", "requested": 0, "fetched": 0, "failed": 0,
                "batch_size": batch_size, "interval": interval,
                "data": {}, "failed_keys": [], "rl_429s": 0,
            }

        all_data: Dict[str, Dict[str, Any]] = {}
        all_failed: List[str] = []

        for i in range(0, total, batch_size):
            chunk = unique_keys[i:i + batch_size]
            data, failed = self._fetch_with_fallback(chunk, interval)
            all_data.update(data)
            all_failed.extend(failed)

        fetched = len(all_data)
        failed = len(all_failed)
        if fetched == 0 and failed > 0:
            status = "failed"
        elif failed > 0:
            status = "partial"
        else:
            status = "success"

        rl = _quote_limiter.snapshot()
        logger.info(
            "quote_service::fetch_ohlc_batch | status=%s | requested=%d "
            "| fetched=%d | failed=%d | batch_size=%d | interval=%s | rl_429s=%d",
            status, total, fetched, failed, batch_size, interval, rl["total_429"],
        )

        return {
            "status": status,
            "requested": total,
            "fetched": fetched,
            "failed": failed,
            "batch_size": batch_size,
            "interval": interval,
            "data": all_data,
            "failed_keys": all_failed,
            "rl_429s": rl["total_429"],
        }

    def gather_enabled_keys(self) -> Dict[str, List[str]]:
        index_keys: List[str] = []
        for name, meta in core_config.MAIN_INDEXES.items():
            if not meta.get("enabled"):
                continue
            key = meta.get("instrument_key")
            if key:
                index_keys.append(key)

        option_keys: List[str] = []
        try:
            from upstox_app.candle.candle_service import candle_service
            for name, meta in core_config.MAIN_INDEXES.items():
                if not meta.get("enabled"):
                    continue
                contracts = candle_service._load_contracts(name)  # noqa: SLF001
                for c in contracts:
                    key = c.get("instrument_key")
                    if key:
                        option_keys.append(key)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "quote_service::gather_enabled_keys failed | step=load contracts | reason=%s",
                _short(exc),
            )

        return {"index_keys": index_keys, "option_keys": option_keys}

    def fetch_all_enabled(
        self,
        include_indexes: bool = True,
        include_options: bool = True,
        interval: Optional[str] = None,
    ) -> Dict[str, Any]:
        keys: List[str] = []
        grouped = self.gather_enabled_keys()
        if include_indexes:
            keys.extend(grouped["index_keys"])
        if include_options:
            keys.extend(grouped["option_keys"])

        result = self.fetch_ohlc_batch(keys, interval=interval)
        result["groups"] = {
            "index_keys": len(grouped["index_keys"]),
            "option_keys": len(grouped["option_keys"]),
        }
        return result

    def fetch_random_sample(
        self,
        index_name: str,
        count: int = 10,
        interval: Optional[str] = None,
    ) -> Dict[str, Any]:
        index_name = index_name.upper()
        try:
            from upstox_app.candle.candle_service import candle_service
            contracts = candle_service._load_contracts(index_name)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "quote_service::fetch_random_sample failed | step=load contracts "
                "| index=%s | reason=%s",
                index_name, _short(exc),
            )
            return {
                "status": "failed", "index": index_name,
                "requested": 0, "fetched": 0, "failed": 0,
                "data": {}, "failed_keys": [],
            }

        keys = [c.get("instrument_key") for c in contracts if c.get("instrument_key")]
        if not keys:
            return {
                "status": "empty", "index": index_name,
                "requested": 0, "fetched": 0, "failed": 0,
                "data": {}, "failed_keys": [],
            }

        count = max(1, min(count, len(keys)))
        sample = random.sample(keys, count)

        result = self.fetch_ohlc_batch(sample, interval=interval)
        result["index"] = index_name
        result["sampled"] = sample
        return result


quote_service = QuoteService()