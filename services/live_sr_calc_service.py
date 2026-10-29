# services/live_sr_calc_service.py
"""On-demand, stateless SR calculation — LIVE mode, never reads saved data.

This service is fundamentally different from
``services.live_sr_levels_service``:

    * NO per-instrument state.
    * NEVER reads ``data/<...>/live_sr_levels.json`` **by default**.
    * NEVER writes anything to disk.
    * Every call recomputes from fresh data and returns the result.

Opt-in `live_sr` block
----------------------
Setting ``include_live_sr=True`` (default ``False``) asks the service to
ALSO read the persisted ``data/<...>/live_sr_levels.json`` — the file
maintained by ``live_sr_levels_service`` — and attach it to the response
under a **new top-level ``live_sr`` key**.

The persisted levels are deliberately **NOT merged into ``levels``**:

    * ``levels`` is always the "fresh from historical + intraday"
      calculation — the same live-data-only guarantee as when
      ``include_live_sr`` is off.
    * ``live_sr`` is a separate, parallel view of what the running
      engine has actually ticked and persisted today.

Callers who want both views side-by-side get them without cross-
contamination; callers who want to merge can do so in their own code
based on their own semantics (fresh != live).

Public loader helpers
---------------------
Public methods exist specifically so that other services can reuse this
service's candle loaders without duplication:

    ``load_historical_candles_for_entry(entry, lookback_days)``
        → historical candles (list only)
    ``fetch_intraday_candles_for_entry(instrument_key)``
        → today's intraday candles (list only)
    ``merge_candles(historical, intraday)``
        → merged list (intraday wins on minute collision)
    ``get_candle_close_at(instrument_key, iso_time)``
        → close of the 1-min candle at that minute (or None)
    ``get_first_intraday_candle(instrument_key)``
        → first intraday candle (or None)

The first three exist so the periodic refresh job
(``live_sr_refresh_from_api``, see
``services/scheduler_service.py``) can produce exactly the same
historical + intraday merge the calc endpoint uses, and feed it into
``live_sr_levels_service.seed_candles``.

The last two exist so the entry-signal / isolation engines can resolve
an historical candle close without re-implementing the minute-bucket
matching logic (see ``strategy_entry_signal_service._entry_price_for``
and ``level_isolated_instrument_service._maybe_self_heal``).

INTRAday by default
-------------------
The endpoint is an "intraday SR" API — its primary use case is "what
SR levels have been confirmed on this instrument TODAY?". Therefore
``only_today`` defaults to **True**. The response is filtered to levels
whose ``confirmed_time`` falls on the current IST calendar day.

To inspect the full merged window (historical + today), pass
``only_today=false`` explicitly. The response then carries
``mode = "full"`` instead of ``"intraday"``.

Historical candles are still fetched in intraday mode — a pivot that
forms at 09:15 needs 15 bars *before* it to be confirmed, and those
bars come from yesterday's session. The filter is on the OUTPUT, not
the INPUT.

Data sources (merged by minute; intraday wins on collision):

    1. Historical 1-min candles from the backup tree::

           data/index/<underlying>/candles.json          (indexes)
           data/<underlying>/<strike>_<TYPE>/candles.json (options)

       Looked up via ``entry["candles_path"]`` (set by
       ``services.instrument_registry``); falls back to the canonical
       layout above when the registry entry has no path.

    2. Today's intraday 1-min candles from Upstox::

           upstox_client.HistoryV3Api.get_intra_day_candle_data(
               instrument_key, "minutes", "1"
           )

    (3. OPTIONAL, only when ``include_live_sr=true``) the persisted
        ``data/<...>/live_sr_levels.json`` — attached under ``live_sr``,
        never merged into ``levels``.)

Historical-file format
----------------------
The candle-backup writer produces files shaped like::

    {
      "updated_at":     "2026-10-25T17:17:24.284028+00:00",
      "relative_path":  "nifty/22400_CE/candles.json",
      "count":          2695,
      "candles": [
        {
          "timestamp": "2026-10-14T09:15:00+05:30",
          "open":      471.65,
          "high":      471.8,
          "low":       444.65,
          "close":     446.45,
          "volume":    12740,
          "oi":        1412970
        },
        ...
      ]
    }

Candle normalisation
--------------------
Historical files in the backup tree come in several shapes across
deployments. ``_normalise_candle`` accepts all of these:

    * list-of-lists   : ``[ts, o, h, l, c, v?]``  (Upstox native)
    * dict-per-candle : ``{"time"|"ts"|"timestamp"|"datetime"|
                            "start_time"|"t": ...,
                            "open"|"o": ...,
                            "high"|"h": ...,
                            "low"|"l": ...,
                            "close"|"c": ...,
                            "volume"|"v"|"vol"|"vtt": ...}``
    * dict-per-candle with split date + time
                      : ``{"date": "YYYY-MM-DD", "time": "HH:MM:SS", ...}``

Wrapper unwrapping (``_extract_candles_from_payload``) accepts:

    * bare ``[...]``
    * ``{"candles": [...]}``               <-- candle-backup standard
    * ``{"data": {"candles": [...]}}``
    * ``{"data": [...]}``

Diagnostics
-----------
``_load_historical_candles`` returns a ``meta`` dict that always carries:

    ``path``, ``candles``, ``candles_raw``,
    ``candles_rejected_format``, ``candles_rejected_cutoff``,
    ``declared_count`` (from the file's own ``count`` field),
    ``count_mismatch`` (true when declared_count != len(candles)),
    ``file_updated_at``, ``relative_path``,
    ``first_candle``, ``last_candle``, ``error``

``_load_live_sr_state`` returns a ``live_sr`` dict that carries:

    ``path``, ``available``, ``date``, ``is_today``,
    ``candle_count``, ``level_count``, ``counts``,
    ``first_level_time``, ``last_level_time``, ``last_candle``,
    ``updated_at``, ``levels``, ``error``

Pivot detection mirrors ``LiveSRLevelsService._scan_all_pivots``
exactly — the strict TradingView-compatible rule:

    resistance pivot at i  iff  C[i].high > C[j].high
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]

    support    pivot at i  iff  C[i].low  < C[j].low
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]

A pivot at index ``i`` is confirmed at bar ``i + right_bars``.
Every confirmed level is retained for the day — there is no "broken" /
"intact" distinction; a level that price has crossed stays on the
chart, exactly as TradingView renders pivot lines.

Levels are returned **sorted by price ascending** (supports and
resistances intermixed), matching TradingView.

Response is deterministic given the same underlying data. The caller
can therefore poll this endpoint without risk of state drift.

See ``routes/live_sr_calc_routes.py`` for the HTTP surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Accepted field-name variants                                                #
# --------------------------------------------------------------------------- #
_TIME_FIELDS = ("timestamp", "datetime", "ts", "start_time", "t", "time")
_OPEN_FIELDS = ("open", "o")
_HIGH_FIELDS = ("high", "h")
_LOW_FIELDS = ("low", "l")
_CLOSE_FIELDS = ("close", "c")
_VOLUME_FIELDS = ("volume", "v", "vol", "vtt")

# --------------------------------------------------------------------------- #
# Time / path helpers                                                         #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return datetime.now(_market_tz()).date().isoformat()

def _parse_time(ts_val: Any) -> datetime | None:
    """Parse a timestamp value into a tz-aware datetime, or return None."""
    if ts_val is None or ts_val == "":
        return None

    try:
        n = float(ts_val)
        if n > 1e10:  # millis
            return datetime.fromtimestamp(n / 1000.0, tz=_market_tz())
        if n > 1e9:  # seconds
            return datetime.fromtimestamp(n, tz=_market_tz())
    except (TypeError, ValueError):
        pass

    try:
        dt = datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt

def _first_present(raw: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for f in fields:
        if f in raw and raw[f] is not None:
            return raw[f]
    return None

def _extract_timestamp_from_dict(raw: dict[str, Any]) -> Any:
    """Extract a timestamp from a dict-per-candle entry."""
    for field in _TIME_FIELDS:
        v = raw.get(field)
        if v is not None:
            if field == "time":
                date_v = raw.get("date")
                if date_v is not None:
                    date_s = str(date_v)
                    if "T" not in date_s and " " not in date_s:
                        return f"{date_s}T{str(v)}"
            return v

    date_v = raw.get("date")
    time_v = raw.get("time")
    if date_v is not None and time_v is not None:
        date_s = str(date_v)
        if "T" in date_s or " " in date_s:
            return date_s
        return f"{date_s}T{str(time_v)}"

    if date_v is not None:
        return date_v
    if time_v is not None:
        return time_v
    return None

def _normalise_candle(raw: Any) -> dict[str, Any] | None:
    """Return ``{time, ts_ms, bucket, open, high, low, close, volume}``."""
    # ---- List-of-lists form ----------------------------------------- #
    if isinstance(raw, (list, tuple)) and len(raw) >= 5:
        dt = _parse_time(raw[0])
        if dt is None:
            return None
        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]) if len(raw) > 5 else 0.0,
            }
        except (IndexError, TypeError, ValueError):
            return None

    # ---- Dict form -------------------------------------------------- #
    if isinstance(raw, dict):
        ts_val = _extract_timestamp_from_dict(raw)
        dt = _parse_time(ts_val)
        if dt is None:
            return None

        o = _first_present(raw, _OPEN_FIELDS)
        h = _first_present(raw, _HIGH_FIELDS)
        l = _first_present(raw, _LOW_FIELDS)
        c = _first_present(raw, _CLOSE_FIELDS)
        v = _first_present(raw, _VOLUME_FIELDS)
        if v is None:
            v = 0.0

        if any(x is None for x in (o, h, l, c)):
            return None

        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            }
        except (TypeError, ValueError):
            return None

    return None

def _describe_raw(raw: Any) -> dict[str, Any]:
    try:
        if isinstance(raw, dict):
            return {
                "type": "dict",
                "keys": [str(k) for k in list(raw.keys())[:20]],
            }
        if isinstance(raw, (list, tuple)):
            return {"type": "list", "length": len(raw)}
        return {"type": type(raw).__name__}
    except Exception:
        return {"type": "unknown"}

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class LiveSRCalcService:
    """On-demand SR calculator. Stateless and safe to call concurrently."""

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        *,
        instrument_key: str | None = None,
        underlying: str | None = None,
        strike: str | int | float | None = None,
        strike_type: str | None = None,
        left_bars: int | None = None,
        right_bars: int | None = None,
        lookback_days: int | None = None,
        only_today: bool = True,       # <-- intraday by default
        include_candles: bool = False,
        include_live_sr: bool = False, # <-- opt-in live_sr block
        debug: bool = False,
    ) -> dict[str, Any]:
        # ---- 1. Resolve entry --------------------------------------- #
        entry, selector_source = self._resolve_entry(
            instrument_key, underlying, strike, strike_type,
        )
        if entry is None:
            return {
                "status": "not_found",
                "error": "instrument_not_found",
                "selector": {
                    "instrument_key": str(instrument_key) if instrument_key else None,
                    "underlying": normalize_underlying(underlying) or underlying,
                    "strike": normalize_strike(strike) or strike,
                    "type": normalize_strike_type(strike_type) or strike_type,
                },
                "generated_at": _now_iso(),
            }

        # ---- 2. Resolve params -------------------------------------- #
        left = max(1, min(100, left_bars if left_bars is not None else config.LIVE_SR_LEFT_BARS))
        right = max(1, min(100, right_bars if right_bars is not None else config.LIVE_SR_RIGHT_BARS))
        lb = max(1, min(
            30,
            lookback_days if lookback_days is not None
            else config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS,
        ))

        if left + right + 1 > config.LIVE_SR_MAX_CANDLES:
            return {
                "status": "invalid_params",
                "error": (
                    f"left_bars + right_bars + 1 = {left + right + 1} "
                    f"exceeds LIVE_SR_MAX_CANDLES ({config.LIVE_SR_MAX_CANDLES})"
                ),
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "max_candles": config.LIVE_SR_MAX_CANDLES,
                },
                "generated_at": _now_iso(),
            }

        # ---- 3. Fetch both sources ---------------------------------- #
        historical, historical_meta = self._load_historical_candles(
            entry, lb, debug=debug,
        )
        intraday, intraday_meta = self._fetch_intraday_candles(entry["instrument_key"])

        # ---- 4. Merge ----------------------------------------------- #
        merged = self._merge_by_minute(historical, intraday)
        if not merged:
            return {
                "status": "no_data",
                "error": "no_candles_from_either_source",
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "include_candles": include_candles,
                    "include_live_sr": include_live_sr,
                    "debug": debug,
                    "max_candles": config.LIVE_SR_CALC_MAX_CANDLES,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "live_sr": (
                    self._load_live_sr_state(entry)
                    if include_live_sr else None
                ),
                "generated_at": _now_iso(),
            }

        # ---- 5. Cap ------------------------------------------------- #
        merged_before_cap = len(merged)
        max_candles = config.LIVE_SR_CALC_MAX_CANDLES
        if len(merged) > max_candles:
            merged = merged[-max_candles:]

        # ---- 6. Enough data for a pivot? ---------------------------- #
        if len(merged) < left + right + 1:
            return {
                "status": "insufficient_data",
                "error": (
                    f"not enough candles to detect any pivots — "
                    f"need {left + right + 1}, have {len(merged)}"
                ),
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "candle_count": len(merged),
                "candles_needed": left + right + 1,
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "include_candles": include_candles,
                    "include_live_sr": include_live_sr,
                    "debug": debug,
                    "max_candles": max_candles,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "live_sr": (
                    self._load_live_sr_state(entry)
                    if include_live_sr else None
                ),
                "generated_at": _now_iso(),
            }

        # ---- 7. Compute pivots -------------------------------------- #
        all_levels = self._compute_pivots(merged, left, right)

        # ---- 8. Mark is_today + sort ascending by price ------------- #
        self._mark_is_today(all_levels)
        all_levels.sort(key=lambda l: l["price"])

        total_before_filters = len(all_levels)

        # ---- 9. Apply caller filters -------------------------------- #
        levels = all_levels
        if only_today:
            levels = [l for l in levels if l["is_today"]]
        # Ascending price order — TradingView behaviour, supports and
        # resistances intermixed.
        levels.sort(key=lambda l: l["price"])

        # ---- 10. Current price + nearest hints ---------------------- #
        current_price = merged[-1]["close"] if merged else None

        nearest_resistance: dict[str, Any] | None = None
        nearest_support: dict[str, Any] | None = None
        if current_price is not None:
            resistances_above = sorted(
                (
                    l for l in all_levels
                    if l["level"] == "resistance" and l["price"] > current_price
                ),
                key=lambda x: x["price"],
            )
            supports_below = sorted(
                (
                    l for l in all_levels
                    if l["level"] == "support" and l["price"] < current_price
                ),
                key=lambda x: x["price"],
                reverse=True,
            )
            nearest_resistance = resistances_above[0] if resistances_above else None
            nearest_support = supports_below[0] if supports_below else None

        # ---- 11. Optional live_sr block ----------------------------- #
        live_sr_state: dict[str, Any] | None = None
        if include_live_sr:
            live_sr_state = self._load_live_sr_state(entry)

        # ---- 12. Warnings ------------------------------------------- #
        warnings: list[str] = []
        if historical_meta.get("error"):
            warnings.append(f"historical: {historical_meta['error']}")
        if intraday_meta.get("error"):
            warnings.append(f"intraday: {intraday_meta['error']}")
        if merged_before_cap > max_candles:
            warnings.append(
                f"merged candles capped from {merged_before_cap} to {max_candles}"
            )
        if historical_meta.get("count_mismatch"):
            warnings.append(
                f"historical: declared_count={historical_meta.get('declared_count')} "
                f"but file contains {historical_meta.get('candles_raw')} entries"
            )

        # Intraday-mode specific hints — make "0 today" obvious.
        if only_today and len(levels) == 0:
            if total_before_filters == 0:
                warnings.append(
                    "intraday mode: no pivots detected in the merged window"
                )
            else:
                warnings.append(
                    f"intraday mode: 0 levels confirmed today, "
                    f"{total_before_filters} historical level(s) available "
                    f"(pass only_today=false to include them)"
                )
        elif only_today and len(levels) < total_before_filters:
            warnings.append(
                f"intraday mode: filtered out "
                f"{total_before_filters - len(levels)} historical level(s) "
                f"(pass only_today=false to include them)"
            )

        # live_sr specific hints.
        if include_live_sr and isinstance(live_sr_state, dict):
            if live_sr_state.get("error"):
                warnings.append(f"live_sr: {live_sr_state['error']}")
            elif not live_sr_state.get("is_today"):
                warnings.append(
                    f"live_sr: file is dated {live_sr_state.get('date')} "
                    f"(not today — {live_sr_state.get('today_iso')})"
                )
            elif not config.LIVE_SR_ENABLED:
                warnings.append(
                    "live_sr: LIVE_SR_ENABLED is false — file may be stale"
                )

        # ---- 13. Build response ------------------------------------- #
        return {
            "status": "ok",
            "mode": "intraday" if only_today else "full",
            "today_iso": _today_iso(),
            "instrument_key": entry["instrument_key"],
            "trading_symbol": entry.get("trading_symbol"),
            "underlying": entry.get("underlying"),
            "strike": entry.get("strike_price"),
            "type": entry.get("strike_type"),
            "expiry": entry.get("expiry"),
            "is_index": bool(entry.get("is_index")),
            "selector_source": selector_source,
            "params": {
                "left_bars": left,
                "right_bars": right,
                "lookback_days": lb,
                "only_today": only_today,
                "include_candles": include_candles,
                "include_live_sr": include_live_sr,
                "debug": debug,
                "max_candles": max_candles,
            },
            "sources": {
                "historical": historical_meta,
                "intraday": intraday_meta,
            },
            "merged": {
                "candle_count": len(merged),
                "candle_count_before_cap": merged_before_cap,
                "first_candle": merged[0]["time"] if merged else None,
                "last_candle": merged[-1]["time"] if merged else None,
                "current_price": current_price,
            },
            "levels": levels,
            "counts": {
                "total": len(levels),
                "total_unfiltered": len(all_levels),
                "resistance": sum(1 for l in levels if l["level"] == "resistance"),
                "support": sum(1 for l in levels if l["level"] == "support"),
                "today": sum(1 for l in levels if l["is_today"]),
            },
            "nearest": {
                "resistance_above": nearest_resistance,
                "support_below": nearest_support,
            },
            "live_sr": live_sr_state,
            "warnings": warnings,
            "candles": merged if include_candles else None,
            "generated_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Public loader helpers (used by sr_backfill_service)                #
    # ------------------------------------------------------------------ #
    def load_historical_candles_for_entry(
        self,
        entry: dict[str, Any],
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_load_historical_candles``.

        Returns just the candle list (diagnostics are dropped). Callers
        who need the ``meta`` dict should call the private method
        directly.

        Used by ``services/sr_backfill_service`` so the periodic
        refresh-from-API job (``live_sr_refresh_from_api``) can produce
        the same historical tail the calc endpoint uses. That's what
        makes ``live_sr_levels.json`` catch pivots that formed near the
        start of the session (they need 15 bars *before* today's open).
        """
        candles, _meta = self._load_historical_candles(entry, lookback_days)
        return candles

    def fetch_intraday_candles_for_entry(
        self,
        instrument_key: str,
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_fetch_intraday_candles``.

        Returns just the candle list. Never raises — a failed Upstox
        call returns ``[]``.
        """
        candles, _meta = self._fetch_intraday_candles(instrument_key)
        return candles

    def merge_candles(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_merge_by_minute``.

        Merges by minute bucket; intraday wins on collision.
        """
        return self._merge_by_minute(historical, intraday)

    # ------------------------------------------------------------------ #
    # Candle-close helpers (used by entry-signal / isolation engines)   #
    # ------------------------------------------------------------------ #
    def get_candle_close_at(
        self,
        instrument_key: str,
        iso_time: str,
    ) -> float | None:
        """Return the close price of the 1-min candle at ``iso_time``.

        Convenience wrapper around ``fetch_intraday_candles_for_entry()``
        that handles the minute-bucket matching (seconds/microseconds
        zeroed) so callers don't have to re-implement the comparison.

        Used by ``strategy_entry_signal_service._entry_price_for()`` as
        the fallback path when ``live_sr_levels_service._states`` is
        missing or doesn't yet contain the instrument (see G1 v4.1 fix
        §5.B). This makes the historical entry price available in
        simulate mode even when the counter-side instrument was never
        registered with the live SR engine.

        Returns ``None`` when:
          * ``iso_time`` cannot be parsed
          * no intraday candle exists for that minute
          * the Upstox fetch fails (never raises)
        """
        target = _parse_time(iso_time)
        if target is None:
            return None
        target_minute = target.replace(second=0, microsecond=0)

        candles = self.fetch_intraday_candles_for_entry(instrument_key)
        for c in candles:
            c_dt = _parse_time(c.get("time"))
            if c_dt is None:
                continue
            if c_dt.replace(second=0, microsecond=0) == target_minute:
                try:
                    return float(c["close"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    def get_first_intraday_candle(
        self,
        instrument_key: str,
    ) -> dict[str, Any] | None:
        """Return the first 1-min intraday candle for ``instrument_key``.

        Used by ``level_isolated_instrument_service._maybe_self_heal()``
        to verify that ``first_candle.json`` matches today's real first
        bar (see G1 v4.1 fix §5.D). The first candle's H / L must agree
        with the cached OR file — otherwise the cached OR is stale and
        must be recomputed.

        Returns ``None`` when the fetch fails or returns no candles.
        """
        candles = self.fetch_intraday_candles_for_entry(instrument_key)
        if not candles:
            return None
        return candles[0]

    # ------------------------------------------------------------------ #
    # Selector resolution                                                #
    # ------------------------------------------------------------------ #
    def _resolve_entry(
        self,
        instrument_key: str | None,
        underlying: str | None,
        strike: str | int | float | None,
        strike_type: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        key = str(instrument_key).strip() if instrument_key else ""
        if key:
            entry = instrument_registry.get(key)
            if entry is None:
                entry = instrument_registry.register_adhoc(key)
            return entry, "instrument_key"

        u = normalize_underlying(underlying)
        s = normalize_strike(strike)
        t = normalize_strike_type(strike_type)
        if not (u and s and t):
            return None, None

        entry = instrument_registry.resolve(u, s, t)
        if entry is not None:
            return entry, "triple"

        for alt in (f"{u} 50", f"{u}50", u.split(" ", 1)[0]):
            alt_norm = normalize_underlying(alt)
            if not alt_norm or alt_norm == u:
                continue
            entry = instrument_registry.resolve(alt_norm, s, t)
            if entry is not None:
                return entry, "triple"

        return None, None

    # ------------------------------------------------------------------ #
    # Historical candles                                                 #
    # ------------------------------------------------------------------ #
    def _historical_paths(self, entry: dict[str, Any]) -> list[Path]:
        candidates: list[Path] = []

        raw = str(entry.get("candles_path") or "").strip()
        if raw:
            candidates.append(Path(raw))

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        is_index = bool(entry.get("is_index")) or strike is None

        if is_index and underlying:
            candidates.append(
                Path(config.LIVE_DATA_DIR) / "index" / underlying / "candles.json"
            )
        elif underlying and strike is not None and strike_type:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = str(strike)
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / underlying
                / f"{strike_int}_{strike_type}"
                / "candles.json"
            )

        seen: set[str] = set()
        out: list[Path] = []
        for p in candidates:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _extract_candles_from_payload(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                candles = data.get("candles")
                if isinstance(candles, list):
                    return candles
            if isinstance(data, list):
                return data
            candles = payload.get("candles")
            if isinstance(candles, list):
                return candles
        return []

    def _load_historical_candles(
        self,
        entry: dict[str, Any],
        lookback_days: int,
        debug: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "path": None,
            "candles": 0,
            "candles_raw": 0,
            "candles_rejected_format": 0,
            "candles_rejected_cutoff": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        chosen: Path | None = None
        for candidate in self._historical_paths(entry):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break

        if chosen is None:
            meta["error"] = "file_missing"
            return [], meta

        meta["path"] = str(chosen)

        try:
            payload = json.loads(chosen.read_text(encoding="utf-8"))
        except Exception as exc:
            meta["error"] = f"unreadable: {type(exc).__name__}"
            logger.warning(
                "[live-sr-calc] cannot parse %s: %s: %s",
                chosen, type(exc).__name__, exc,
            )
            return [], meta

        if isinstance(payload, dict):
            if payload.get("updated_at") is not None:
                meta["file_updated_at"] = str(payload["updated_at"])
            if payload.get("relative_path") is not None:
                meta["relative_path"] = str(payload["relative_path"])
            if payload.get("count") is not None:
                try:
                    meta["declared_count"] = int(payload["count"])
                except (TypeError, ValueError):
                    meta["declared_count"] = payload["count"]

        raw_list = self._extract_candles_from_payload(payload)
        meta["candles_raw"] = len(raw_list)

        if not raw_list:
            meta["error"] = "empty_candles_list"
            return [], meta

        declared = meta.get("declared_count")
        if isinstance(declared, int) and declared != len(raw_list):
            meta["count_mismatch"] = True

        cutoff = datetime.now(_market_tz()).timestamp() - (lookback_days * 86400)

        out: list[dict[str, Any]] = []
        rejected_format = 0
        rejected_cutoff = 0

        for raw in raw_list:
            c = _normalise_candle(raw)
            if c is None:
                rejected_format += 1
                continue
            if c["ts_ms"] / 1000.0 < cutoff:
                rejected_cutoff += 1
                continue
            out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["candles_rejected_format"] = rejected_format
        meta["candles_rejected_cutoff"] = rejected_cutoff
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None

        if not out:
            meta["sample_structure"] = [
                _describe_raw(r) for r in raw_list[:3]
            ]
            if rejected_format == len(raw_list):
                meta["error"] = (
                    f"all_{rejected_format}_entries_failed_format_parsing"
                )
            elif rejected_cutoff == len(raw_list):
                meta["error"] = (
                    f"all_{rejected_cutoff}_entries_older_than_"
                    f"{lookback_days}d_cutoff"
                )
            elif rejected_format + rejected_cutoff == len(raw_list):
                meta["error"] = (
                    f"no_entries_passed_"
                    f"(format={rejected_format},cutoff={rejected_cutoff})"
                )
        elif debug:
            meta["sample_structure"] = [
                _describe_raw(r) for r in raw_list[:3]
            ]

        return out, meta

    # ------------------------------------------------------------------ #
    # Intraday candles                                                   #
    # ------------------------------------------------------------------ #
    def _fetch_intraday_candles(
        self,
        instrument_key: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "api": "HistoryV3Api.get_intra_day_candle_data",
            "candles": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        try:
            import upstox_client  # type: ignore
        except ImportError:
            meta["error"] = "upstox_client_not_installed"
            return [], meta

        try:
            cfg = upstox_client.Configuration()
            try:
                from services.token_service import token_service
                token = token_service.get_access_token()
                if token:
                    cfg.access_token = token
            except Exception:
                pass

            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(cfg))
            response = api.get_intra_day_candle_data(
                instrument_key, "minutes", "1",
            )
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[live-sr-calc] intraday fetch failed for %s: %s: %s",
                instrument_key, type(exc).__name__, exc,
            )
            return [], meta

        body: Any = response
        if hasattr(response, "to_dict"):
            try:
                body = response.to_dict()
            except Exception:
                body = response
        elif hasattr(response, "data"):
            body = response.data

        if not isinstance(body, dict):
            meta["error"] = "uncoercible_response"
            return [], meta

        raw_candles = None
        data = body.get("data")
        if isinstance(data, dict):
            maybe = data.get("candles")
            if isinstance(maybe, list):
                raw_candles = maybe
        if not isinstance(raw_candles, list):
            top = body.get("candles")
            if isinstance(top, list):
                raw_candles = top
        if not isinstance(raw_candles, list):
            raw_candles = []

        out: list[dict[str, Any]] = []
        for raw in raw_candles:
            c = _normalise_candle(raw)
            if c is not None:
                out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None
        return out, meta

    # ------------------------------------------------------------------ #
    # Optional live_sr block (opt-in, default false)                     #
    # ------------------------------------------------------------------ #
    def _live_sr_paths(self, entry: dict[str, Any]) -> list[Path]:
        """Return candidate live_sr_levels.json paths.

        The live-SR file lives next to the instrument's candles.json /
        ema_crosses.json in the same folder. Derive from
        ``entry["candles_path"]`` first, then fall back to the canonical
        layout (index vs option).
        """
        candidates: list[Path] = []

        raw = str(entry.get("candles_path") or "").strip()
        if raw:
            try:
                p = Path(raw)
                if p.name:
                    candidates.append(p.with_name(config.LIVE_SR_FILENAME))
            except Exception:
                pass

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        is_index = bool(entry.get("is_index")) or strike is None

        if is_index and underlying:
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / "index" / underlying / config.LIVE_SR_FILENAME
            )
        elif underlying and strike is not None and strike_type:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = str(strike)
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / underlying
                / f"{strike_int}_{strike_type}"
                / config.LIVE_SR_FILENAME
            )

        seen: set[str] = set()
        out: list[Path] = []
        for p in candidates:
            k = str(p)
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    def _load_live_sr_state(
        self,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Load the persisted ``live_sr_levels.json`` for this instrument.

        Never raises. Every failure mode lands in ``error`` while the rest
        of the block stays at its default "unavailable" values.

        The returned levels are the raw persisted records. Levels are
        sorted by price ascending so the block matches the ``levels``
        array order.
        """
        today = _today_iso()
        meta: dict[str, Any] = {
            "path": None,
            "available": False,
            "today_iso": today,
            "date": None,
            "is_today": False,
            "candle_count": 0,
            "level_count": 0,
            "counts": {
                "resistance": 0,
                "support": 0,
            },
            "first_level_time": None,
            "last_level_time": None,
            "last_candle": None,
            "updated_at": None,
            "levels": [],
            "error": None,
        }

        chosen: Path | None = None
        for candidate in self._live_sr_paths(entry):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break

        if chosen is None:
            meta["error"] = "file_missing"
            return meta

        meta["path"] = str(chosen)

        try:
            payload = json.loads(chosen.read_text(encoding="utf-8"))
        except Exception as exc:
            meta["error"] = f"unreadable: {type(exc).__name__}"
            logger.warning(
                "[live-sr-calc] cannot parse %s: %s: %s",
                chosen, type(exc).__name__, exc,
            )
            return meta

        if not isinstance(payload, dict):
            meta["error"] = "not_a_dict"
            return meta

        meta["available"] = True
        meta["date"] = payload.get("date")
        meta["is_today"] = (str(payload.get("date") or "") == today)
        try:
            meta["candle_count"] = int(payload.get("candle_count") or 0)
        except (TypeError, ValueError):
            meta["candle_count"] = 0
        meta["updated_at"] = payload.get("updated_at")
        meta["last_candle"] = payload.get("last_candle")

        raw_levels = payload.get("levels") or []
        if isinstance(raw_levels, list):
            levels_copy = [dict(l) for l in raw_levels if isinstance(l, dict)]
            # Remove any residual broken fields so consumers see a clean shape.
            for lvl in levels_copy:
                lvl.pop("broken", None)
                lvl.pop("broken_at", None)
                lvl.pop("broken_price", None)
                lvl.pop("broken_bar_index", None)

            # Sort ascending by price to match the calc's own ordering.
            levels_copy.sort(key=lambda l: float(l.get("price") or 0.0))

            meta["levels"] = levels_copy
            meta["level_count"] = len(levels_copy)

            counts = {"resistance": 0, "support": 0}
            for l in levels_copy:
                t = str(l.get("level") or "").lower()
                if t == "resistance":
                    counts["resistance"] += 1
                elif t == "support":
                    counts["support"] += 1
            meta["counts"] = counts

            times: list[str] = []
            for l in levels_copy:
                t = str(l.get("pivot_time") or "")
                if t:
                    times.append(t)
            if times:
                meta["first_level_time"] = min(times)
                meta["last_level_time"] = max(times)

        return meta

    # ------------------------------------------------------------------ #
    # Merge                                                              #
    # ------------------------------------------------------------------ #
    def _merge_by_minute(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_bucket: dict[int, dict[str, Any]] = {}
        for c in historical:
            by_bucket[c["bucket"]] = c
        for c in intraday:
            by_bucket[c["bucket"]] = c
        return [by_bucket[b] for b in sorted(by_bucket.keys())]

    # ------------------------------------------------------------------ #
    # Pivot detection                                                    #
    # ------------------------------------------------------------------ #
    def _compute_pivots(
        self,
        candles: list[dict[str, Any]],
        left: int,
        right: int,
    ) -> list[dict[str, Any]]:
        levels: list[dict[str, Any]] = []
        n = len(candles)
        if n < left + right + 1:
            return levels

        for i in range(left, n - right):
            c = candles[i]

            left_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(i - left, i)
            )
            right_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(i + 1, i + right + 1)
            )
            left_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(i - left, i)
            )
            right_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(i + 1, i + right + 1)
            )

            confirmed_idx = i + right
            confirmed_candle = candles[confirmed_idx]
            confirmed_time_price = float(
                confirmed_candle.get("close")
                if confirmed_candle.get("close") is not None
                else confirmed_candle.get("ltp", 0.0)
            )

            if left_ok_res and right_ok_res:
                levels.append({
                    "level": "resistance",
                    "price": float(c["high"]),
                    "pivot_bar_index": i,
                    "pivot_time": c["time"],
                    "confirmed_bar_index": confirmed_idx,
                    "confirmed_time": confirmed_candle["time"],
                    "confirmed_time_price": confirmed_time_price,
                    "left_bars": left,
                    "right_bars": right,
                    "is_today": False,
                })

            if left_ok_sup and right_ok_sup:
                levels.append({
                    "level": "support",
                    "price": float(c["low"]),
                    "pivot_bar_index": i,
                    "pivot_time": c["time"],
                    "confirmed_bar_index": confirmed_idx,
                    "confirmed_time": confirmed_candle["time"],
                    "confirmed_time_price": confirmed_time_price,
                    "left_bars": left,
                    "right_bars": right,
                    "is_today": False,
                })

        return levels

    # ------------------------------------------------------------------ #
    # is_today marking                                                   #
    # ------------------------------------------------------------------ #
    def _mark_is_today(self, levels: list[dict[str, Any]]) -> None:
        today = datetime.now(_market_tz()).date()
        for level in levels:
            try:
                dt = datetime.fromisoformat(
                    str(level["confirmed_time"]).replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_market_tz())
                level["is_today"] = (dt.date() == today)
            except Exception:
                level["is_today"] = False

# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
live_sr_calc_service = LiveSRCalcService()