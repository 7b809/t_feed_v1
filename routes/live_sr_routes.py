# routes/live_sr_routes.py
"""Dashboard data endpoints for level-filtered instruments + SR levels.

    GET  /live-sr/market-status                -> today's trading status + next open
    GET  /live-sr/index/{underlying}           -> filtered instruments + OR + SR
    GET  /live-sr/summary                      -> all configured indexes at once
    POST /live-sr/backfill                     -> manual SR backfill (gated)

2026-11-06 — isolation-store-first reads
----------------------------------------
Route handlers now prefer the **isolation store** (``<LIVE_DATA_DIR>/isolation``)
over the legacy canonical tree when both contain SR data for an
instrument. The isolation store is the freshest source:

  * ``isolation/{symbol}_live_sr.json`` — rewritten **atomically** every
    minute by ``scheduler_service._isolated_pair_sr_minute_tick`` and on
    every ``isolated_instrument_sr_export_service.run()``.
  * ``isolation/isolated_instrument.json`` / ``opposite_instrument.json``
    — the full merged candle series + SR levels written by the export
    service at isolation time.
  * ``isolation/isolated_instrument_sr_check.json`` — the early-session
    (09:15–09:30) SR verdict.
  * ``level_isolated_instrument_promotion.json`` — the promotion sidecar
    (frozen entry, promotion metadata).

Reading from these files instead of recomputing SR from candles (or
walking the entire ``all_instruments.json`` registry) is dramatically
cheaper and semantically equivalent: the scheduler tick recomputes from
fresh intraday candles every minute, so the store is at most 60s stale.

The canonical tree (``data/<underlying>/<strike>_<TYPE>/live_sr_levels.json``)
is retained as a fallback — it's what the live SR engine persists and
remains correct for indexes/options that have not been promoted to
isolation. Every instrument row carries ``sr_source`` =
``"isolation" | "canonical" | "none"`` so dashboards can display which
source served the data.

New endpoints
-------------
    GET  /live-sr/active                       -> current active tracked set
    GET  /live-sr/isolation                    -> isolation store contents
    GET  /live-sr/isolation/{trading_symbol}   -> one instrument's snapshot

``/live-sr/active`` aggregates three sources:

  1. ``config.LEVEL_FILTER_INDEXES`` — statically-configured indexes
  2. ``live_sr_calc_service.active_sr_instruments`` — dynamically
     registered keys (isolated + opposite pairs, added by
     ``isolated_instrument_promotion_service``)
  3. Isolation-store metadata (isolated/opposite/promoted instrument
     keys + trading symbols)

``/live-sr/isolation`` returns the full isolation store (candles
stripped from the payload to keep it small), so an operator can see
exactly what the sched/export/promotion services have written.

The manual backfill endpoint (unchanged) respects the same
trading-day / time-window gate as the automatic backfill:

    * SR_BACKFILL_ENABLED must be True
    * LIVE_SR_ENABLED must be True
    * current time must be in [09:15, 23:59] IST
    * today must be Mon–Fri
    * today must NOT be a trading holiday
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

router = APIRouter(prefix="/live-sr", tags=["live-sr"])

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

_ISOLATION_LIVE_SR_SUFFIX = "_live_sr.json"


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[live-sr-routes] unreadable %s", path)
        return None


def _next_open_dt(now: datetime) -> datetime:
    """Next 09:15 IST on a weekday that is not a holiday."""
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    from services.market_holiday_service import market_holiday_service
    for _ in range(14):
        if candidate.weekday() <= 4:
            try:
                payload = market_holiday_service.get_holiday(
                    candidate.date().isoformat()
                )
                if str(payload.get("status") or "").lower() != "holiday":
                    return candidate
            except Exception:
                return candidate
        candidate += timedelta(days=1)
    return candidate


def _trading_status() -> dict[str, Any]:
    now = _now_market()
    is_weekend = now.weekday() > 4
    is_holiday = False
    holiday_desc: str | None = None
    holiday_error: str | None = None
    try:
        from services.market_holiday_service import market_holiday_service
        payload = market_holiday_service.get_today()
        status = str(payload.get("status") or "").lower()
        is_holiday = status == "holiday"
        holidays = payload.get("holidays") or []
        if holidays and isinstance(holidays[0], dict):
            holiday_desc = holidays[0].get("description") or None
        if status == "error":
            holiday_error = payload.get("error")
    except Exception as exc:
        holiday_error = f"{type(exc).__name__}: {exc}"

    is_trading_day = not is_weekend and not is_holiday
    in_session = (
        is_trading_day
        and dtime(9, 15) <= now.time() <= dtime(15, 30)
    )

    if is_weekend:
        reason = f"weekend ({now.strftime('%A')})"
    elif is_holiday:
        reason = f"holiday ({holiday_desc or 'unknown'})"
    else:
        reason = "trading_day"

    next_open = _next_open_dt(now)
    countdown_seconds = int((next_open - now).total_seconds())
    message = (
        f"Today is a non-trading day ({now.strftime('%A')}). "
        f"Next trading session: {next_open.strftime('%a %d %b %Y 09:15 IST')}."
        if not is_trading_day
        else f"Trading session active. Market closes at 15:30 IST."
    )

    return {
        "today": now.date().isoformat(),
        "day_of_week": now.strftime("%A"),
        "current_time_ist": now.isoformat(),
        "is_trading_day": is_trading_day,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "holiday_description": holiday_desc,
        "holiday_error": holiday_error,
        "in_session": in_session,
        "reason": reason,
        "session_open_time_ist": "09:15 IST",
        "session_close_time_ist": "15:30 IST",
        "next_open_iso": next_open.isoformat(),
        "next_open_label": next_open.strftime("%a %d %b %Y 09:15 IST"),
        "countdown_seconds": max(0, countdown_seconds),
        "message": message,
        "or_sr_available_after": "09:16:10 IST",
        "generated_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Isolation store helpers (2026-11-06)                                        #
# --------------------------------------------------------------------------- #
# The isolation store is the canonical location for the isolated /
# opposite pair's fresh SR data. Files are written atomically (tmp +
# os.replace) by the scheduler minute tick and by the export service, so
# reading a partial file is impossible.
#
# Filename sanitization is identical to
#   * IsolatedInstrumentSRExportService._safe_filename_for_sr()
#   * isolated_instrument_promotion_service._safe_filename()
# so all three writers/readers land on the same physical file per
# instrument.
# --------------------------------------------------------------------------- #
def _isolation_dir() -> Path:
    try:
        return Path(config.LIVE_DATA_DIR) / "isolation"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "isolation"


def _safe_filename_for_sr(trading_symbol: Any) -> str:
    raw = str(trading_symbol or "").strip() or "unknown"
    safe = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw
    )
    return safe.strip("_") or "unknown"


def _isolation_live_sr_path(trading_symbol: Any) -> Path:
    return _isolation_dir() / (
        f"{_safe_filename_for_sr(trading_symbol)}{_ISOLATION_LIVE_SR_SUFFIX}"
    )


def _read_isolation_live_sr(trading_symbol: Any) -> dict[str, Any] | None:
    if not trading_symbol:
        return None
    return _read_json(_isolation_live_sr_path(trading_symbol))


def _read_promotion_sidecar() -> dict[str, Any] | None:
    try:
        path = Path(config.LIVE_DATA_DIR) / (
            "level_isolated_instrument_promotion.json"
        )
    except Exception:
        return None
    return _read_json(path)


def _strip_candles(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if k != "candles"}


def _isolation_state_for_underlying(underlying_upper: str) -> dict[str, Any]:
    """Return the isolation store state restricted to one underlying.

    Used by ``/index/{underlying}`` so the dashboard can surface the
    isolated/opposite pair inline.
    """
    iso_dir = _isolation_dir()
    isolated = _read_json(iso_dir / "isolated_instrument.json")
    opposite = _read_json(iso_dir / "opposite_instrument.json")
    early_check = _read_json(
        iso_dir / "isolated_instrument_sr_check.json"
    )
    promotion = _read_promotion_sidecar()

    iso_meta = (isolated or {}).get("metadata") or {}
    opp_meta = (opposite or {}).get("metadata") or {}

    iso_underlying = str(iso_meta.get("underlying") or "").strip().upper()
    opp_underlying = str(opp_meta.get("underlying") or "").strip().upper()

    same_underlying = (
        iso_underlying == underlying_upper
        or opp_underlying == underlying_upper
    )
    if not same_underlying:
        return {
            "active": False,
            "isolated": None,
            "opposite": None,
            "promotion": None,
            "early_check": None,
        }

    def _summ(meta: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not meta:
            return None
        return {
            "instrument_key": meta.get("instrument_key"),
            "trading_symbol": meta.get("trading_symbol"),
            "strike_price": meta.get("strike_price"),
            "strike_type": meta.get("strike_type"),
            "expiry": meta.get("expiry"),
            "role": meta.get("role"),
            "candle_count": (payload or {}).get("candles_count"),
            "level_count": (payload or {}).get("sr_levels_count"),
        }

    return {
        "active": True,
        "isolated": _summ(iso_meta, isolated),
        "opposite": _summ(opp_meta, opposite),
        "promotion": promotion,
        "early_check": early_check,
    }


def _get_active_tracked_set() -> dict[str, Any]:
    """Return the union of statically-configured and dynamically-tracked keys.

    Sources:
      1. ``config.LEVEL_FILTER_INDEXES`` — statically-configured indexes.
      2. ``live_sr_calc_service.active_sr_instruments`` — dynamically
         registered keys (isolated + opposite pairs, added by
         ``isolated_instrument_promotion_service``).
      3. Isolation store metadata (``isolated_instrument.json`` /
         ``opposite_instrument.json``) and the promotion sidecar.
    """
    config_indexes: list[str] = []
    try:
        raw = getattr(config, "LEVEL_FILTER_INDEXES", None)
        if isinstance(raw, str):
            config_indexes = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            config_indexes = sorted({str(x).strip() for x in raw if x})
    except Exception:
        logger.exception(
            "[live-sr-routes] could not parse config.LEVEL_FILTER_INDEXES"
        )

    dynamic_active: list[str] = []
    try:
        from services.live_sr_calc_service import live_sr_calc_service
        active = getattr(live_sr_calc_service, "active_sr_instruments", None)
        if active is not None:
            dynamic_active = sorted({str(k).strip() for k in active if k})
    except Exception:
        logger.warning(
            "[live-sr-routes] could not read "
            "live_sr_calc_service.active_sr_instruments",
            exc_info=True,
        )

    iso_dir = _isolation_dir()
    isolated_payload = _read_json(iso_dir / "isolated_instrument.json")
    opposite_payload = _read_json(iso_dir / "opposite_instrument.json")
    early_check = _read_json(iso_dir / "isolated_instrument_sr_check.json")
    promotion = _read_promotion_sidecar()

    iso_meta = (isolated_payload or {}).get("metadata") or {}
    opp_meta = (opposite_payload or {}).get("metadata") or {}
    iso_key = iso_meta.get("instrument_key")
    opp_key = opp_meta.get("instrument_key")

    union = sorted({
        k for k in [
            *config_indexes,
            *dynamic_active,
            iso_key,
            opp_key,
            (promotion or {}).get("to_instrument_key"),
        ] if k
    })

    per_instrument_snapshots: list[dict[str, Any]] = []
    if iso_dir.exists():
        for f in sorted(iso_dir.glob(f"*{_ISOLATION_LIVE_SR_SUFFIX}")):
            data = _read_json(f)
            if data is None:
                continue
            per_instrument_snapshots.append({
                "path": str(f),
                "trading_symbol": data.get("trading_symbol"),
                "instrument_key": data.get("instrument_key"),
                "level_count": data.get("level_count", 0),
                "candle_count": data.get("candle_count", 0),
                "counts": data.get("counts"),
                "last_candle": data.get("last_candle"),
                "refreshed_at": data.get("refreshed_at")
                                or data.get("generated_at"),
                "source": data.get("source"),
            })

    return {
        "config_indexes": config_indexes,
        "dynamic_active": dynamic_active,
        "isolated_instrument_key": iso_key,
        "isolated_trading_symbol": iso_meta.get("trading_symbol"),
        "opposite_instrument_key": opp_key,
        "opposite_trading_symbol": opp_meta.get("trading_symbol"),
        "promoted_instrument_key": (promotion or {}).get("to_instrument_key"),
        "promoted_trading_symbol": (promotion or {}).get("to_trading_symbol"),
        "union_keys": union,
        "per_instrument_snapshots": per_instrument_snapshots,
        "promotion": promotion,
        "early_check": early_check,
        "isolated_meta": iso_meta or None,
        "opposite_meta": opp_meta or None,
    }


# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _index_folder(underlying: str) -> Path:
    return Path(config.LIVE_DATA_DIR) / "index" / underlying.lower()


def _index_sr_path(underlying: str) -> Path:
    return _index_folder(underlying) / config.LIVE_SR_FILENAME


def _option_sr_path(underlying: str, strike: Any, strike_type: str) -> Path:
    try:
        strike_int = int(float(strike))
    except (TypeError, ValueError):
        strike_int = str(strike)
    return (
        Path(config.LIVE_DATA_DIR)
        / underlying.lower()
        / f"{strike_int}_{str(strike_type).upper()}"
        / config.LIVE_SR_FILENAME
    )


def _build_instrument_row(
    inst: dict[str, Any],
    underlying: str,
    or_levels: dict[str, Any],
) -> dict[str, Any]:
    """Build one instrument row for the dashboard.

    2026-11-06: the SR payload is now read from the **isolation store**
    first (``isolation/{trading_symbol}_live_sr.json`` — refreshed
    every minute by the scheduler tick), falling back to the canonical
    tree (``data/.../live_sr_levels.json``). The ``sr_source`` field on
    the row tells the dashboard which source served the data.
    """
    key = str(inst.get("instrument_key") or "").strip()
    strike = inst.get("strike")
    strike_type = str(inst.get("type") or "").upper()
    levels = list(inst.get("levels") or [])
    reasons = list(inst.get("reasons") or [])
    trading_symbol = inst.get("trading_symbol")

    level_price = None
    for name in levels:
        raw = or_levels.get(name)
        if raw is not None:
            try:
                level_price = float(raw)
                break
            except (TypeError, ValueError):
                pass

    # ---- Isolation store first (fresh minute-tick data) ------------- #
    sr_source = "none"
    sr_payload: dict[str, Any] | None = None
    if trading_symbol:
        iso_payload = _read_isolation_live_sr(trading_symbol)
        if iso_payload is not None:
            sr_payload = iso_payload
            sr_source = "isolation"

    # ---- Fall back to canonical tree -------------------------------- #
    if sr_payload is None:
        canonical = _read_json(
            _option_sr_path(underlying, strike, strike_type)
        )
        if canonical is not None:
            sr_payload = canonical
            sr_source = "canonical"

    sr_levels = (sr_payload or {}).get("levels") or []
    sr_count = len(sr_levels)
    sr_latest = None
    sr_latest_confirmed_price = None
    if sr_levels:
        last_lvl = sr_levels[-1]
        try:
            sr_latest = float(last_lvl.get("price"))
        except (TypeError, ValueError):
            sr_latest = None
        if last_lvl.get("confirmed_time_price") is not None:
            try:
                sr_latest_confirmed_price = float(
                    last_lvl["confirmed_time_price"]
                )
            except (TypeError, ValueError):
                sr_latest_confirmed_price = None

    ltp_snapshot = None
    try:
        from services.live_feed_service import live_feed_service
        ltp_snapshot = live_feed_service.get_latest_ltp(key)
    except Exception:
        ltp_snapshot = None

    return {
        "instrument_key": key,
        "trading_symbol": trading_symbol,
        "strike": strike,
        "type": strike_type,
        "expiry": inst.get("expiry"),
        "levels": levels,
        "reasons": reasons,
        "level_price": level_price,
        "sr_levels_count": sr_count,
        "sr_latest_level": sr_latest,
        "sr_latest_confirmed_price": sr_latest_confirmed_price,
        "sr_available": bool(sr_payload),
        "sr_source": sr_source,
        "sr_last_refreshed": (
            (sr_payload or {}).get("refreshed_at")
            or (sr_payload or {}).get("updated_at")
            or (sr_payload or {}).get("generated_at")
        ),
        "ltp_server_snapshot": ltp_snapshot,
        "chart_url": f"/chart/{key.replace('|', '%7C').replace(' ', '%20')}",
    }


def _build_index_payload(underlying: str) -> dict[str, Any]:
    u = str(underlying or "").strip().upper()
    if not u:
        raise HTTPException(
            status_code=400,
            detail={"error": "underlying_required"},
        )

    folder = _index_folder(u)
    first_candle = _read_json(folder / "first_candle.json")
    level_instruments = _read_json(folder / "level_instruments.json")

    if level_instruments is None:
        return {
            "underlying": u,
            "status": "not_generated",
            "path": str(folder / "level_instruments.json"),
            "hint": (
                "level_instruments.json not yet written for today. "
                "POST /admin/ops/level-instruments/recalc to force."
            ),
            "isolation": _isolation_state_for_underlying(u),
            "generated_at": _now_iso(),
        }

    or_levels = (first_candle or {}).get("or_levels") or {}
    fc = (first_candle or {}).get("first_candle") or {}

    ce = [
        _build_instrument_row(i, u, or_levels)
        for i in (level_instruments.get("ce_instruments") or [])
    ]
    pe = [
        _build_instrument_row(i, u, or_levels)
        for i in (level_instruments.get("pe_instruments") or [])
    ]

    index_key = level_instruments.get("index_instrument_key")
    index_sr = _read_json(_index_sr_path(u))
    index_sr_count = len((index_sr or {}).get("levels") or [])
    index_ltp = None
    if index_key:
        try:
            from services.live_feed_service import live_feed_service
            index_ltp = live_feed_service.get_latest_ltp(index_key)
        except Exception:
            index_ltp = None

    return {
        "underlying": u,
        "status": "ok",
        "date": level_instruments.get("date"),
        "strike_step": level_instruments.get("strike_step"),
        "range_filter": level_instruments.get("range_filter"),
        "first_candle": fc,
        "or_levels": or_levels,
        "index_instrument_key": index_key,
        "index_sr_levels_count": index_sr_count,
        "index_ltp": index_ltp,
        "ce_instruments": ce,
        "pe_instruments": pe,
        "isolation": _isolation_state_for_underlying(u),
        "generated_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/market-status")
def market_status() -> dict[str, Any]:
    """Return the current trading-day status + next session info.

    Used by the dashboard to show the non-market-day banner and to gate
    the level-filtered section (which stays empty on non-trading days).
    """
    return _trading_status()


@router.get("/index/{underlying}")
def index_sr(underlying: str) -> dict[str, Any]:
    """Return the level-filtered CE/PE instruments for one index.

    Each instrument row carries its OR level(s), the raw level price,
    the count of live SR levels detected so far, the ``sr_source``
    (``"isolation"`` or ``"canonical"``), and a chart URL.

    The response also carries an ``isolation`` block describing any
    isolated/opposite pair active today for this underlying.
    """
    return _build_index_payload(underlying)


@router.get("/summary")
def summary() -> dict[str, Any]:
    """Return the market status + every configured index's payload.

    2026-11-06: the response now also carries the full
    ``active_tracked_set`` block (config indexes + dynamically
    registered isolated/opposite pairs) so a dashboard can render
    the complete SR universe from one call.
    """
    status = _trading_status()
    indexes: dict[str, Any] = {}
    for u in config.LEVEL_FILTER_INDEXES:
        uu = str(u or "").strip().upper()
        if not uu:
            continue
        try:
            indexes[uu] = _build_index_payload(uu)
        except HTTPException:
            raise
        except Exception as exc:
            indexes[uu] = {
                "underlying": uu,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "market_status": status,
        "indexes": indexes,
        "active_tracked_set": _get_active_tracked_set(),
        "generated_at": _now_iso(),
    }


@router.post("/backfill")
def trigger_backfill(send_telegram: bool = False) -> dict[str, Any]:
    """Force a manual SR backfill (runs the intraday seed).

    GATE
    ----
    This endpoint honours the same trading-day / time-window gate as
    the automatic feed-start backfill:

      * ``SR_BACKFILL_ENABLED`` must be True
      * ``LIVE_SR_ENABLED`` must be True
      * current time must be in ``[09:15, 23:59]`` IST
      * today must be Mon–Fri
      * today must NOT be a trading holiday

    The response always includes ``allowed`` (the gate result) and
    ``skip_reason`` (the human-readable reason when skipped) so a
    caller can distinguish a "skipped" response from a run that
    simply produced no candles.
    """
    try:
        from services.sr_backfill_service import sr_backfill_service
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )

    # Gate check (same logic the service uses internally).
    allowed, skip_reason = sr_backfill_service.should_run()

    result = sr_backfill_service.run(
        reason="manual", send_telegram=send_telegram,
    )

    # Annotate the response so callers can tell the two cases apart.
    result["allowed"] = bool(allowed)
    result["skip_reason"] = (
        skip_reason if not allowed else result.get("skip_reason")
    )
    result["window_ist"] = (
        f"{int(config.SR_BACKFILL_START_HOUR):02d}:"
        f"{int(config.SR_BACKFILL_START_MINUTE):02d}"
        f"-"
        f"{int(config.SR_BACKFILL_END_HOUR):02d}:"
        f"{int(config.SR_BACKFILL_END_MINUTE):02d}"
    )
    return result


# --------------------------------------------------------------------------- #
# Active tracked set + isolation store inspection (2026-11-06)                #
# --------------------------------------------------------------------------- #
@router.get("/active")
def active_tracked_set() -> dict[str, Any]:
    """Return the current active tracked SR universe.

    Aggregates:

      * ``config.LEVEL_FILTER_INDEXES`` — statically-configured indexes.
      * ``live_sr_calc_service.active_sr_instruments`` — dynamically
        registered keys (isolated + opposite pairs, added by
        ``isolated_instrument_promotion_service``).
      * Isolation store metadata (``isolated_instrument.json`` /
        ``opposite_instrument.json``) and the promotion sidecar
        (``level_isolated_instrument_promotion.json``).

    Also returns ``per_instrument_snapshots`` — one entry per
    ``isolation/*_live_sr.json`` file currently on disk — so a caller
    can see exactly which instruments have a fresh (≤60s-old) SR
    snapshot and how many levels each has.
    """
    return {
        "status": "ok",
        **_get_active_tracked_set(),
        "generated_at": _now_iso(),
    }


@router.get("/isolation")
def isolation_snapshot() -> dict[str, Any]:
    """Return the full contents of the isolation store.

    Candle arrays are stripped from the top-level ``isolated`` /
    ``opposite`` blocks to keep the payload small — a caller that needs
    the full candle series should read
    ``isolation/isolated_instrument.json`` directly. The
    ``per_instrument_snapshots`` array summarises every
    ``*_live_sr.json`` file in the directory.
    """
    iso_dir = _isolation_dir()
    isolated = _read_json(iso_dir / "isolated_instrument.json")
    opposite = _read_json(iso_dir / "opposite_instrument.json")
    early_check = _read_json(iso_dir / "isolated_instrument_sr_check.json")
    promotion = _read_promotion_sidecar()

    per_instrument: list[dict[str, Any]] = []
    if iso_dir.exists():
        for f in sorted(iso_dir.glob(f"*{_ISOLATION_LIVE_SR_SUFFIX}")):
            data = _read_json(f)
            if data is None:
                continue
            per_instrument.append({
                "path": str(f),
                "trading_symbol": data.get("trading_symbol"),
                "instrument_key": data.get("instrument_key"),
                "level_count": data.get("level_count", 0),
                "candle_count": data.get("candle_count", 0),
                "counts": data.get("counts"),
                "first_candle": data.get("first_candle"),
                "last_candle": data.get("last_candle"),
                "refreshed_at": data.get("refreshed_at")
                                or data.get("generated_at"),
                "source": data.get("source"),
            })

    return {
        "status": "ok",
        "isolation_dir": str(iso_dir),
        "isolated": _strip_candles(isolated),
        "opposite": _strip_candles(opposite),
        "early_check": early_check,
        "promotion": promotion,
        "per_instrument_snapshots": per_instrument,
        "generated_at": _now_iso(),
    }


@router.get("/isolation/{trading_symbol}")
def isolation_instrument_snapshot(trading_symbol: str) -> dict[str, Any]:
    """Return the full per-instrument isolation snapshot.

    Reads ``isolation/{safe(trading_symbol)}_live_sr.json`` — the file
    refreshed every minute by the scheduler tick and on every export.
    Returns the full snapshot including the level list (candles are
    not present in this file — see ``/isolation`` for the full merged
    series).
    """
    path = _isolation_live_sr_path(trading_symbol)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "trading_symbol": trading_symbol,
                "resolved_path": str(path),
            },
        )
    data = _read_json(path)
    if data is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "unreadable",
                "resolved_path": str(path),
            },
        )
    return data