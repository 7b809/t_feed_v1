# services/first_candle_service.py
"""First-candle OR + EMA-cross detection service.

Runs at 09:16:10 IST Mon–Fri (after the 09:15 first candle closes).

After the per-instrument OR + EMA-cross pass completes, this service
also triggers ``level_instrument_filter_service.run(...)`` which
converts the index OR levels into the CE/PE option instruments that
the live SR engine should track. That filter writes
``data/index/<nifty|sensex>/level_instruments.json`` and registers the
filtered instruments with ``live_sr_levels_service``.

Backfill mode (2026-10-26)
--------------------------
When the project is started post-close (e.g. 19:08 IST) we still need
today's ``first_candle.json`` files so that ``level_isolated_instrument_service``
can reconstruct the "would-have-isolated" instrument from the intraday
API. The gate is now bypassable via ``backfill=True``:

    first_candle_service.run(reason="startup_backfill", backfill=True)

The post-close rejection is bypassed in backfill mode, but weekend,
holiday, and pre-open are still rejected — those are genuinely
non-trading conditions, not just "the data hasn't finished yet".

Index retry (2026-10-27)
------------------------
The 09:16:10 scheduled run was observed to fail for the two index
keys (``NSE_INDEX|Nifty 50``, ``BSE_INDEX|SENSEX``) while the same
run succeeded for ~95 option instruments.  The Upstox intraday API
finalises the 09:15 index bar ~20–40 s after the 09:16:00 minute
close, so a single fetch at 09:16:10 misses it.  The same API works
cleanly by 09:16:40 (see the SR-backfill run at that timestamp in the
2026-10-27 log).

``_fetch_first_candle_upstox`` now retries **index keys only** with a
short backoff so the scheduled run collects the index bar before it
returns.  Options are not retried because a genuinely missing option
bar usually means the option has not traded at all in the first
minute, and retrying ~75 thin options would waste 30+ s of API calls
on every run.  For defence in depth, the level-isolation 5-minute
recheck self-heals by re-running this service if either index file
is missing (see ``services/level_isolated_instrument_service.py``).

Same-series OR computation (G1 v4.1, 2026-10-29)
------------------------------------------------
``level_isolated_instrument_service._replay()`` used to mix the OR
levels from the on-disk ``first_candle.json`` with candles freshly
fetched from the intraday API. That decoupling let a stale OR file
(yesterday's values, or values computed from a different window)
record "touches" that never happened — e.g. R3 = 13329.65 against a
replay series whose today-high was 13298.75. The fix is to recompute
OR from the exact series being replayed::

    or_result = first_candle_service.compute_for_series(underlying, candles)

``compute_for_series()`` returns ``{"first_candle": {...},
"or_levels": {...}}`` (or ``None`` when no 09:15 bar is present in
the supplied candles) so the caller can validate the trigger against
OR values that are guaranteed consistent with the candles it is
iterating.

Targeted recompute (G1 v4.1, 2026-10-29)
----------------------------------------
``level_isolated_instrument_service._maybe_self_heal()`` now needs to
verify that ``first_candle.json["first_candle"]["high"|"low"]`` match
today's real first intraday bar, and force a fresh OR computation
when they disagree — not just when the file is missing or has a
stale ``date``. To make that cheap, this module exposes
``recompute_for_underlying(underlying, backfill=True)`` which runs
``_one()`` only for the registry entries of the given underlying
(typically 1–2 instruments) instead of the full ~100-instrument
sweep that ``run()`` performs. Also exposes the read helper
``_read_first_candle_file(underlying)`` so the caller does not have
to duplicate the path layout.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.market_holiday_service import market_holiday_service

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")
_MAX_WORKERS = max(1, int(getattr(config, "FIRST_CANDLE_WORKERS", 8)))

# --------------------------------------------------------------------------- #
# Index retry policy                                                          #
# --------------------------------------------------------------------------- #
# 09:16:10 -> 09:16:45 window.  Empirically the index bar is available
# by 09:16:40 (see the 2026-10-27 SR-backfill run at that second).  5
# attempts × 7 s delay = 28 s of backoff, so the last fetch lands at
# ~09:16:45 which is comfortably after the empirical cutoff.
_INDEX_FETCH_ATTEMPTS = 5
_INDEX_FETCH_DELAY_SECONDS = 7.0
_INDEX_KEY_PREFIXES = ("NSE_INDEX|", "BSE_INDEX|")

# Options are NOT retried — a missing option candle at 09:16:10 usually
# means the option has not traded in the first minute at all.  If the
# index file ends up missing, the level-isolation 5-minute recheck
# self-heals by re-running this service.
_OPTION_FETCH_ATTEMPTS = 1

_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
# The 09:15 1-min candle is only guaranteed to exist from ~09:15:30
# onwards; Upstox usually finalises it by 09:16:00, occasionally by
# 09:16:40.  09:16:10 matches the scheduled task time and empirically
# succeeds on the first fetch.  Any caller invoking `_market_gate_check`
# before this time (e.g. the level-isolation 5-minute recheck firing
# at exactly 09:15:00) is rejected up front rather than burning 5 x 7 s
# = 28 s on retries that the API cannot satisfy yet.
_OR_READY = dtime(9, 16, 10)
_VALID_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri

# --------------------------------------------------------------------------- #
# Market-gate helpers                                                         #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)

def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() not in _VALID_WEEKDAYS

def _is_holiday_today() -> tuple[bool, str | None]:
    try:
        payload = market_holiday_service.get_today()
        if str(payload.get("status") or "").lower() == "holiday":
            holidays = payload.get("holidays") or []
            desc = None
            if holidays and isinstance(holidays[0], dict):
                desc = holidays[0].get("description") or None
            return (True, desc)
    except Exception:
        logger.exception("[first-candle] holiday gate raised; assuming not holiday")
    return (False, None)

def _next_market_open(after: datetime) -> datetime:
    candidate = after.replace(hour=9, minute=15, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    for _ in range(14):
        if candidate.weekday() in _VALID_WEEKDAYS:
            try:
                date_str = candidate.date().isoformat()
                payload = market_holiday_service.get_holiday(date_str)
                is_hol = str(payload.get("status") or "").lower() == "holiday"
            except Exception:
                is_hol = False
            if not is_hol:
                return candidate
        candidate += timedelta(days=1)
    return candidate

def _format_countdown(delta: timedelta) -> str:
    total_sec = max(0, int(delta.total_seconds()))
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    hms = f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{days}d {hms}" if days > 0 else hms

def _market_gate_check(backfill: bool = False) -> dict[str, Any]:
    """Return the trading-time gate result.

    ``backfill=True`` bypasses the post-close rejection: the Upstox
    intraday API still serves today's 1-min candles after 15:30, so
    the 09:15 OR candle can be reconstructed at any point in the
    extended backfill window (09:16 -> 23:59 IST) on a trading day.

    Weekend, holiday, and pre-open are still rejected in backfill
    mode — those are genuine non-trading conditions, not just
    "the data isn't ready yet".

    The 09:15:00 -> 09:16:09 window is also rejected (``or_warming_up``)
    because the 09:15 1-min candle is not yet finalised by the Upstox
    intraday API.  Without this gate, any caller that fires at exactly
    09:15:00 (e.g. the level-isolation 5-minute recheck) wastes the
    full ``_INDEX_FETCH_ATTEMPTS`` x ``_INDEX_FETCH_DELAY_SECONDS`` =
    28 s retry window on a candle that cannot exist yet, then
    returns ``no_first_candle`` for every instrument.  Rejecting
    up front lets the scheduled 09:16:10 run pick the OR cleanly.
    """
    now = _now_market()

    if _is_weekend(now):
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "weekend",
            "skip_message": f"Non-trading day ({now.strftime('%A')})",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
            "backfill_mode": backfill,
        }

    is_hol, desc = _is_holiday_today()
    if is_hol:
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "holiday",
            "skip_message": f"Market holiday: {desc or 'unknown'}",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
            "backfill_mode": backfill,
        }

    now_t = now.time()

    if now_t < _MARKET_OPEN:
        today_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        return {
            "allow": False,
            "skip_reason": "pre_open",
            "skip_message": "Before market open (09:15 IST)",
            "next_open": today_open,
            "countdown_str": _format_countdown(today_open - now),
            "countdown_label": today_open.strftime("%a %d %b 09:15 IST"),
            "backfill_mode": backfill,
        }

    # Market is technically open but the 09:15 OR candle has not been
    # finalised by the Upstox intraday API yet.  Reject immediately so
    # callers do not burn the full 28 s retry window on a candle that
    # cannot exist yet (see module docstring and inline comment above
    # _OR_READY).
    if now_t < _OR_READY:
        today_or_ready = now.replace(
            hour=_OR_READY.hour,
            minute=_OR_READY.minute,
            second=_OR_READY.second,
            microsecond=0,
        )
        return {
            "allow": False,
            "skip_reason": "or_warming_up",
            "skip_message": (
                "OR warming up (first candle closes at 09:16:00 IST; "
                "retry at 09:16:10 IST)"
            ),
            "next_open": today_or_ready,
            "countdown_str": _format_countdown(today_or_ready - now),
            "countdown_label": today_or_ready.strftime("%a %d %b 09:16:10 IST"),
            "backfill_mode": backfill,
        }

    if now_t > _MARKET_CLOSE:
        if backfill:
            # Post-close backfill mode: allow. The intraday API still
            # serves today's full 1-min candle series, so the 09:15
            # OR candle can be reconstructed. This is the path that
            # makes /isolated work when the project starts at e.g.
            # 19:08 IST.
            return {
                "allow": True,
                "skip_reason": None,
                "skip_message": None,
                "next_open": None,
                "countdown_str": None,
                "countdown_label": None,
                "backfill_mode": True,
            }
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "post_close",
            "skip_message": "After market close (15:30 IST)",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
            "backfill_mode": backfill,
        }

    return {
        "allow": True,
        "skip_reason": None,
        "skip_message": None,
        "next_open": None,
        "countdown_str": None,
        "countdown_label": None,
        "backfill_mode": backfill,
    }

# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"

def _folder_for_entry(entry: dict[str, Any]) -> Path:
    underlying = str(entry.get("underlying") or "").strip().lower()
    strike = entry.get("strike_price")
    strike_type = str(entry.get("strike_type") or "").upper()
    is_index = bool(entry.get("is_index")) or strike is None

    if is_index:
        idx_name = underlying or _safe_folder(entry.get("instrument_key"))
        return Path(config.LIVE_DATA_DIR) / "index" / idx_name

    if underlying and strike is not None and strike_type:
        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            strike_int = str(strike)
        return (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
        )

    return (
        Path(config.LIVE_DATA_DIR)
        / "index"
        / _safe_folder(entry.get("instrument_key"))
    )

def _read_first_candle_file(underlying: str) -> dict[str, Any] | None:
    """Read today's ``first_candle.json`` for an index underlying.

    Returns the parsed dict, or ``None`` when the file is missing,
    unreadable, or not a dict.

    Used by ``level_isolated_instrument_service._maybe_self_heal()``
    as the "cached" side of the H/L comparison (G1 v4.1 fix §5.D):
    the caller compares ``payload["first_candle"]["high"|"low"]``
    against ``live_sr_calc_service.get_first_intraday_candle()`` and
    forces a recompute when they disagree.
    """
    p = (
        Path(config.LIVE_DATA_DIR)
        / "index"
        / _safe_folder(underlying)
        / "first_candle.json"
    )
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[first-candle] cannot parse %s", p)
        return None
    if not isinstance(payload, dict):
        return None
    return payload

# --------------------------------------------------------------------------- #
# Upstox intraday fetch                                                       #
# --------------------------------------------------------------------------- #
def _load_sdk():
    try:
        import upstox_client  # type: ignore
        return upstox_client
    except ImportError:
        return None

def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:
            d = None
        if isinstance(d, dict):
            return d
    data_attr = getattr(raw, "data", None)
    if isinstance(data_attr, dict):
        return data_attr
    return None

def _is_index_key(instrument_key: str) -> bool:
    return str(instrument_key or "").startswith(_INDEX_KEY_PREFIXES)

def _fetch_first_candle_once(instrument_key: str) -> dict[str, Any] | None:
    """Single-shot fetch of the 09:15 candle for ``instrument_key``.

    Returns the first-candle dict, or ``None`` when the API does not
    yet expose a 09:15 bar. Never raises.
    """
    sdk = _load_sdk()
    if sdk is None:
        logger.error("upstox_client not installed")
        return None

    try:
        cfg = sdk.Configuration()
        api = sdk.HistoryV3Api(sdk.ApiClient(cfg))
        response = api.get_intra_day_candle_data(
            instrument_key, "minutes", "1",
        )
    except Exception as exc:
        logger.warning(
            "[first-candle] Upstox fetch failed for %s: %s: %s",
            instrument_key, type(exc).__name__, exc,
        )
        return None

    body = _coerce_dict(response)
    if body is None:
        return None

    candles = (body.get("data") or {}).get("candles") or []
    if not isinstance(candles, list) or not candles:
        return None

    for raw in candles:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        try:
            ts_str = str(raw[0])
            dt = datetime.fromisoformat(ts_str)
            if dt.hour != 9 or dt.minute != 15:
                continue
            return {
                "time_iso": ts_str,
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low":  float(raw[3]),
                "close": float(raw[4]),
            }
        except Exception:
            continue
    return None

def _fetch_first_candle_upstox(instrument_key: str) -> dict[str, Any] | None:
    """Fetch the 09:15 candle, retrying index keys with a short backoff.

    Index keys (NSE_INDEX|*, BSE_INDEX|*) are retried up to
    ``_INDEX_FETCH_ATTEMPTS`` times with a ``_INDEX_FETCH_DELAY_SECONDS``
    delay between attempts. Options are single-shot.
    """
    if _is_index_key(instrument_key):
        attempts = _INDEX_FETCH_ATTEMPTS
        delay = _INDEX_FETCH_DELAY_SECONDS
    else:
        attempts = _OPTION_FETCH_ATTEMPTS
        delay = 0.0

    result: dict[str, Any] | None = None
    for attempt in range(attempts):
        result = _fetch_first_candle_once(instrument_key)
        if result is not None:
            if attempt > 0:
                logger.info(
                    "[first-candle] %s: succeeded on attempt %d/%d "
                    "after %.1fs of retries",
                    instrument_key, attempt + 1, attempts,
                    attempt * delay,
                )
            return result
        if attempt < attempts - 1:
            logger.info(
                "[first-candle] %s: 09:15 candle not yet available "
                "(attempt %d/%d) — retrying in %.1fs",
                instrument_key, attempt + 1, attempts, delay,
            )
            time.sleep(delay)

    if _is_index_key(instrument_key) and attempts > 1:
        logger.warning(
            "[first-candle] %s: no 09:15 candle after %d attempts "
            "(%.1fs total) — level filter / isolation may be affected",
            instrument_key, attempts, (attempts - 1) * delay,
        )
    return None

# --------------------------------------------------------------------------- #
# OR level computation                                                        #
# --------------------------------------------------------------------------- #
def _compute_or_levels(fc: dict[str, Any]) -> dict[str, float]:
    H = float(fc["high"])
    L = float(fc["low"])
    A = (H + L) / 2.0

    D_H = abs(H - A)
    D_L = abs(A - L)

    R1 = A + D_H / 2.0
    S1 = A - D_L / 2.0

    R2 = H + D_H
    S2 = L - D_L

    R3 = R2 + D_H
    S3 = S2 - D_L

    return {
        "H": H, "L": L, "A": A,
        "D_H": D_H, "D_L": D_L,
        "R1": R1, "R2": R2, "R3": R3,
        "S1": S1, "S2": S2, "S3": S3,
        "R3_Threshold": (R2 + R3) / 2.0,
        "S3_Threshold": (S2 + S3) / 2.0,
    }

# --------------------------------------------------------------------------- #
# EMA-cross detection                                                         #
# --------------------------------------------------------------------------- #
def _read_historical_crosses(entry: dict[str, Any]) -> dict[str, Any] | None:
    folder = _folder_for_entry(entry)
    p = folder / "ema_crosses.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[first-candle] cannot parse %s", p)
        return None

def _detect_new_ema_cross(
    entry: dict[str, Any],
    historical: dict[str, Any] | None,
    fc: dict[str, Any],
) -> dict[str, Any]:
    if not historical:
        return {"detected": False, "type": None,
                "reason": "no_historical_crosses", "compared_against": None}

    crosses = historical.get("crosses") or []
    if not crosses:
        return {"detected": False, "type": None,
                "reason": "empty_crosses_list", "compared_against": None}

    last = crosses[-1]
    if not isinstance(last, dict):
        return {"detected": False, "type": None,
                "reason": "malformed_last_cross", "compared_against": None}

    last_ts = str(last.get("timestamp") or "")
    last_type = str(last.get("type") or "").lower()
    try:
        last_fast = float(last.get("fast_ema") or 0.0)
        last_slow = float(last.get("slow_ema") or 0.0)
    except (TypeError, ValueError):
        last_fast = 0.0
        last_slow = 0.0

    try:
        fc_close = float(fc["close"])
        fc_open = float(fc["open"])
    except (TypeError, ValueError):
        fc_close = 0.0
        fc_open = 0.0

    new_type: str | None = None
    if last_type == "bullish" and last_slow > 0 and fc_close < last_slow:
        new_type = "bearish"
    elif last_type == "bearish" and last_slow > 0 and fc_close > last_slow:
        new_type = "bullish"

    return {
        "detected": new_type is not None,
        "type": new_type,
        "reason": "reverse_cross_against_last_slow_ema",
        "last_cross_type": last_type,
        "last_cross_fast_ema": last_fast,
        "last_cross_slow_ema": last_slow,
        "first_candle_open": fc_open,
        "first_candle_close": fc_close,
        "compared_against": last_ts or None,
    }

# --------------------------------------------------------------------------- #
# Public service                                                              #
# --------------------------------------------------------------------------- #
class FirstCandleService:
    """Runs the OR + EMA-cross task, then triggers the level filter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_run: dict[str, Any] = {}

    def _one(self, entry: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        key = str(entry.get("instrument_key") or "")
        if not key:
            return ("", None, "missing_instrument_key")
        try:
            fc = _fetch_first_candle_upstox(key)
            if fc is None:
                return (key, None, "no_first_candle")
            levels = _compute_or_levels(fc)
            historical = _read_historical_crosses(entry)
            ema = _detect_new_ema_cross(entry, historical, fc)
            payload = {
                "instrument_key": key,
                "trading_symbol": entry.get("trading_symbol"),
                "underlying": entry.get("underlying"),
                "strike_price": entry.get("strike_price"),
                "strike_type": entry.get("strike_type"),
                "date": datetime.now(_MARKET_TZ).date().isoformat(),
                "first_candle": fc,
                "or_levels": levels,
                "ema_cross_at_first_candle": ema,
                "computed_at": datetime.now(_MARKET_TZ).isoformat(),
            }
            folder = _folder_for_entry(entry)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "first_candle.json").write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            return (key, payload, None)
        except Exception as exc:
            return (key, None, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # Same-series OR computation (G1 v4.1, §5.D)                         #
    # ------------------------------------------------------------------ #
    def compute_for_series(
        self,
        underlying: str,
        candles: list[dict[str, Any]],
        *,
        date: str | None = None,
    ) -> dict[str, Any] | None:
        """Compute OR levels from an arbitrary candle series.

        Used by ``level_isolated_instrument_service._replay()`` to
        ensure the OR values used for the trigger check are derived
        from the **same series** the replay is iterating, rather than
        from the on-disk ``first_candle.json`` — mixing the two was
        the root cause of the phantom MIDCPNIFTY "R3 touch"
        (analysis §4.1 / §4.5): R3 = 13329.65 from a stale file was
        checked against a replay whose series never exceeded
        13298.75.

        ``candles`` must be a list of dicts with at least ``time``
        (ISO-8601), ``open``, ``high``, ``low``, ``close`` — the
        shape produced by ``live_sr_calc_service._merge_by_minute``.
        ``date`` defaults to today's IST date; override when
        replaying a non-today session.

        Returns ``{"first_candle": {...}, "or_levels": {...}}`` or
        ``None`` when no 09:15 bar for ``date`` is present in
        ``candles``.
        """
        target_date = date or datetime.now(_MARKET_TZ).date().isoformat()

        first_candle: dict[str, Any] | None = None
        for c in candles:
            try:
                dt = datetime.fromisoformat(
                    str(c.get("time") or "").replace("Z", "+00:00")
                )
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_MARKET_TZ)
            dt_market = dt.astimezone(_MARKET_TZ)
            if dt_market.date().isoformat() != target_date:
                continue
            if dt_market.hour == 9 and dt_market.minute == 15:
                try:
                    first_candle = {
                        "time_iso": dt_market.isoformat(),
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low":  float(c["low"]),
                        "close": float(c["close"]),
                    }
                except (KeyError, TypeError, ValueError):
                    return None
                break

        if first_candle is None:
            return None

        return {
            "first_candle": first_candle,
            "or_levels": _compute_or_levels(first_candle),
        }

    # ------------------------------------------------------------------ #
    # Targeted recompute (G1 v4.1, §5.D)                                 #
    # ------------------------------------------------------------------ #
    def recompute_for_underlying(
        self,
        underlying: str,
        *,
        reason: str = "self_heal",
        backfill: bool = False,
    ) -> dict[str, Any]:
        """Recompute the OR file for a single underlying.

        Used by ``level_isolated_instrument_service._maybe_self_heal()``
        when ``first_candle.json["first_candle"]["high"|"low"]``
        disagree with today's real first intraday bar — the H/L check
        that catches a *stale-but-today-dated* OR file surviving a
        restart (analysis §4.1 / §5.D).

        Runs the same market gate as ``run()``, then invokes ``_one()``
        only for registry entries whose ``underlying`` matches the
        target (~1–2 instruments) instead of the full ~100-instrument
        sweep. Returns a summary dict shaped like ``run()``'s but with
        ``underlying`` added and ``total`` counting only matching
        instruments.
        """
        gate = _market_gate_check(backfill=backfill)
        if not gate["allow"]:
            summary = {
                "reason": reason,
                "backfill": backfill,
                "skipped": True,
                "skip_reason": gate["skip_reason"],
                "skip_message": gate["skip_message"],
                "next_open": gate["next_open"].isoformat() if gate["next_open"] else None,
                "countdown_str": gate["countdown_str"],
                "countdown_label": gate["countdown_label"],
                "ran_at": _now_market().isoformat(),
                "underlying": underlying,
                "total": 0, "computed": 0, "errors": 0,
            }
            with self._lock:
                self._last_run = summary
            logger.info(
                "[first-candle] RECOMPUTE SKIPPED underlying=%s reason=%s (%s)",
                underlying, gate["skip_reason"], gate["skip_message"],
            )
            return summary

        u = str(underlying or "").strip().lower()
        matching = [
            e for e in instrument_registry.all()
            if str(e.get("underlying") or "").strip().lower() == u
        ]

        results: dict[str, Any] = {}
        errors: list[str] = []
        for entry in matching:
            key, payload, err = self._one(entry)
            if err:
                errors.append(f"{key}: {err}")
            elif payload is not None:
                results[key] = payload

        summary = {
            "reason": reason,
            "backfill": backfill,
            "skipped": False,
            "skip_reason": None,
            "skip_message": None,
            "next_open": None,
            "countdown_str": None,
            "countdown_label": None,
            "underlying": underlying,
            "total": len(matching),
            "computed": len(results),
            "errors": len(errors),
            "error_list": errors[:10],
            "ran_at": _now_market().isoformat(),
        }
        with self._lock:
            self._last_run = summary
        logger.info(
            "[first-candle] RECOMPUTE COMPLETE underlying=%s total=%d "
            "computed=%d errors=%d backfill=%s",
            underlying, len(matching), len(results), len(errors), backfill,
        )
        return summary

    def run(
        self,
        reason: str = "scheduled",
        backfill: bool = False,
    ) -> dict[str, Any]:
        """Compute OR levels for every registry instrument.

        ``backfill=True`` bypasses the post-close rejection so today's
        OR levels can be reconstructed from the intraday API when the
        project is started after 15:30 (e.g. at 19:08 IST). Weekend,
        holiday, and pre-open are still rejected in backfill mode.

        Both modes reject the 09:15:00 -> 09:16:09 window (``or_warming_up``)
        because the 09:15 1-min candle does not exist in the Upstox
        intraday API yet.
        """
        gate = _market_gate_check(backfill=backfill)
        if not gate["allow"]:
            summary = {
                "reason": reason,
                "backfill": backfill,
                "skipped": True,
                "skip_reason": gate["skip_reason"],
                "skip_message": gate["skip_message"],
                "next_open": gate["next_open"].isoformat() if gate["next_open"] else None,
                "countdown_str": gate["countdown_str"],
                "countdown_label": gate["countdown_label"],
                "ran_at": _now_market().isoformat(),
                "total": 0, "computed": 0, "errors": 0,
                "crosses_detected": 0, "elapsed_seconds": 0.0,
            }
            with self._lock:
                self._last_run = summary
            logger.info(
                "[first-candle] SKIPPED reason=%s (%s) backfill=%s — "
                "next open in %s (%s)",
                gate["skip_reason"], gate["skip_message"], backfill,
                gate["countdown_str"], gate["countdown_label"],
            )
            return summary

        if gate.get("backfill_mode"):
            phase = (
                "post-close "
                if _now_market().time() > _MARKET_CLOSE
                else ""
            )
            logger.info(
                "[first-candle] %sBACKFILL MODE — reconstructing "
                "today's OR from intraday API",
                phase,
            )

        entries = list(instrument_registry.all())
        total = len(entries)
        logger.info(
            "[first-candle] START reason=%s instruments=%d workers=%d "
            "backfill=%s",
            reason, total, _MAX_WORKERS, backfill,
        )

        results: dict[str, Any] = {}
        errors: list[str] = []
        skipped: list[str] = []
        crosses_detected: list[dict[str, Any]] = []

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = [pool.submit(self._one, e) for e in entries]
            for fut in as_completed(futures):
                key, payload, err = fut.result()
                if err:
                    if err == "no_first_candle":
                        skipped.append(key)
                    else:
                        errors.append(f"{key}: {err}")
                    continue
                if payload is not None:
                    results[key] = payload
                    ema = payload.get("ema_cross_at_first_candle") or {}
                    if ema.get("detected"):
                        crosses_detected.append({
                            "instrument_key": key,
                            "trading_symbol": payload.get("trading_symbol"),
                            "type": ema.get("type"),
                        })

        elapsed = round(time.monotonic() - t0, 2)
        summary = {
            "reason": reason,
            "backfill": backfill,
            "skipped": False,
            "skip_reason": None,
            "skip_message": None,
            "next_open": None,
            "countdown_str": None,
            "countdown_label": None,
            "total": total,
            "computed": len(results),
            "instrument_skipped": len(skipped),
            "errors": len(errors),
            "crosses_detected": len(crosses_detected),
            "elapsed_seconds": elapsed,
            "crosses_list": crosses_detected[:20],
            "error_list": errors[:10],
            "skipped_list": skipped[:10],
            "ran_at": _now_market().isoformat(),
        }
        with self._lock:
            self._last_run = summary
        logger.info(
            "[first-candle] COMPLETE total=%d computed=%d skipped=%d "
            "errors=%d crosses=%d elapsed=%ss backfill=%s",
            total, len(results), len(skipped),
            len(errors), len(crosses_detected), elapsed, backfill,
        )

        # ---- Level-instrument filter -------------------------------- #
        # Convert the freshly written OR levels into the CE/PE
        # instruments that the live SR engine should track.
        try:
            from services.level_instrument_filter_service import (
                level_instrument_filter_service,
            )
            filter_result = level_instrument_filter_service.run(
                reason=f"first_candle:{reason}"
            )
            summary["level_filter"] = {
                "status": filter_result.get("status"),
                "indexes": {
                    k: {
                        "status": v.get("status"),
                        "ce_count": v.get("ce_count"),
                        "pe_count": v.get("pe_count"),
                    }
                    for k, v in (filter_result.get("indexes") or {}).items()
                },
            }
            logger.info(
                "[first-candle] level filter: %s",
                summary["level_filter"],
            )
        except Exception as exc:
            logger.exception("[first-candle] level filter failed")
            summary["level_filter"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

        return summary

    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_run)

first_candle_service = FirstCandleService()