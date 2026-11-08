# services/dummy_isolation_builder.py
"""Build a dummy isolation document from a search-result instrument.

Used by ``GET /api/v1/search/instrument/dummy-isolation`` so an operator
can preview — or persist — an isolation record **without waiting for a
real touch**.

The resulting document matches the shape produced by
``IsolationService._build_record`` (same keys, same nesting) so any
downstream consumer of ``project_info.instrument_isolation`` can be
exercised against a realistic dummy without code changes.

Honest labelling
----------------
Every dummy record carries:

  * ``is_dummy = True``
  * ``source = "dummy_search_endpoint"``
  * ``explanation.is_dummy = True``
  * ``trigger_*`` fields marked as SYNTHETIC in the explanation
  * ``isolated_option_ltp_source = "dummy_param"``
  * ``nifty_ltp_source`` = ``"dummy_param"`` (if user supplied) or
    ``"strike_proxy"`` (if derived from strike)

Downstream consumers that gate on ``is_dummy`` will always be able to
distinguish these records from real isolation events.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import (
    TIMEZONE,
    ISOLATION_DOC_ID,
    ISOLATION_BIAS_SOURCE,
    NIFTY_STRIKE_STEP,
)
from core.logger import get_logger
from services.execution_mode_service import pin_execution_mode

logger = get_logger(__file__)


def build_dummy_isolation_record(
    *,
    instrument: dict,
    strike: int,
    bias: str,
    nifty_ltp: float,
    option_ltp: float,
    touch_time_iso: str,
    trade_date_ist: str | None = None,
    nifty_ltp_source: str = "dummy_param",
) -> dict:
    """Build a full isolation-shaped record using the given instrument.

    Parameters
    ----------
    instrument
        One entry from ``/v2/instruments/search`` (see
        ``InstrumentSearchService.search_nifty_option_at_strike_all``).
    strike
        The strike passed to the search, echoed for completeness.
    bias
        ``"CE"`` or ``"PE"`` — used to derive a synthetic trigger side.
    nifty_ltp
        NIFTY spot used for the strike-generation explanation. Does not
        need to be exact; only affects diagnostic fields.
    option_ltp
        Isolated option LTP snapshot. Feeds ``pin_execution_mode`` to
        decide STANDARD vs EARLY_EXTREME_REVERSAL using the real strategy
        rule (touch time < 10:30 AND price outside [100, 250]).
    touch_time_iso
        ISO timestamp for ``isolation_touch_time`` and mode decision.
        Naive strings are interpreted as IST by ``pin_execution_mode``.
    trade_date_ist
        Override ``trade_date_ist`` (default: today's IST date).
    nifty_ltp_source
        Diagnostic string; usually ``"dummy_param"`` or
        ``"strike_proxy"``.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    if trade_date_ist is None:
        trade_date_ist = now.date().isoformat()

    # Real strategy rule — mode is pinned using the actual formula, not a
    # hard-coded default. This is the one field downstream routing relies
    # on most, so we don't want to hand-wave it.
    mode_payload = pin_execution_mode(touch_time_iso, option_ltp)

    # Synthetic trigger — purely diagnostic in dummy mode. Derived from
    # nifty_ltp so the numbers are internally consistent with the strike.
    is_ce = (bias or "").upper() == "CE"
    synthetic_trigger_side = "R3" if is_ce else "S3"
    synthetic_level_name = "R3_Threshold" if is_ce else "S3_Threshold"
    synthetic_trigger_level = (
        float(nifty_ltp) - (NIFTY_STRIKE_STEP / 2.0)
        if is_ce
        else float(nifty_ltp) + (NIFTY_STRIKE_STEP / 2.0)
    )

    record = {
        "_id": ISOLATION_DOC_ID,
        "trade_date_ist": trade_date_ist,

        # ---- Instrument (from search response) ----
        "instrument_key": instrument.get("instrument_key"),
        "exchange_token": instrument.get("exchange_token"),
        "trading_symbol": instrument.get("trading_symbol"),
        "short_name": instrument.get("short_name"),
        "name": instrument.get("name"),
        "strike": instrument.get("strike_price", strike),
        "option_type": instrument.get("instrument_type", bias),
        "expiry": instrument.get("expiry"),
        "lot_size": instrument.get("lot_size"),
        "tick_size": instrument.get("tick_size"),
        "segment": instrument.get("segment"),
        "exchange": instrument.get("exchange"),
        "underlying": instrument.get("underlying_symbol") or "NIFTY",
        "bias": bias,
        "strike_computed": strike,
        "nifty_ltp_at_isolation": float(nifty_ltp),
        "nifty_ltp_source": nifty_ltp_source,

        # ---- Trigger (SYNTHETIC in dummy mode) ----
        "trigger_index": "NSE_INDEX|Nifty 50",
        "trigger_index_canonical": "NIFTY",
        "trigger_side": synthetic_trigger_side,
        "trigger_level": round(float(synthetic_trigger_level), 2),
        "trigger_level_name": synthetic_level_name,
        "trigger_value": float(nifty_ltp),
        "trigger_priority": 0,
        "simultaneous_touches": [],
        "level_ref": None,

        # ---- Routed reference ----
        "routed_index": "NIFTY",
        "routed_ltp": float(nifty_ltp),

        # ---- Time ----
        "isolation_touch_time": touch_time_iso,
        "isolation_created_at": now.isoformat(),

        # ---- Option snapshot ----
        "isolated_option_ltp": float(option_ltp),
        "isolated_option_ltp_source": "dummy_param",

        # ---- Mode (immutable per strategy §6) ----
        **mode_payload,

        # ---- Status / provenance ----
        "status": "ACTIVE",
        "source": "dummy_search_endpoint",
        "isolation_backfilled": False,
        "is_dummy": True,
    }

    # ---- Explanation block (same shape as real isolation records) ----
    record["explanation"] = {
        "summary": (
            f"DUMMY isolation built from search API: "
            f"{instrument.get('trading_symbol')} "
            f"(strike={strike}, bias={bias}, "
            f"nifty_ltp={nifty_ltp:.2f}, option_ltp={option_ltp:.2f})"
        ),
        "is_dummy": True,
        "backfilled": False,

        "or_levels_source": {
            "first_candle": None,
            "computed_levels": None,
            "note": (
                "Dummy isolation — OR levels are not part of this record. "
                "Real isolation records always populate this block."
            ),
        },

        "trigger_context": {
            "index_key": "NSE_INDEX|Nifty 50",
            "canonical": "NIFTY",
            "side": synthetic_trigger_side,
            "level_name": synthetic_level_name,
            "level_value": round(float(synthetic_trigger_level), 2),
            "index_ltp_at_touch": float(nifty_ltp),
            "detected_at": touch_time_iso,
            "priority": 0,
            "simultaneous_touches": [],
            "note": (
                "SYNTHETIC trigger — this record was not produced by a "
                "real index touch. Do not treat it as a live signal."
            ),
        },

        "nifty_routing": {
            "nifty_ltp": float(nifty_ltp),
            "source": nifty_ltp_source,
            "bias_source": ISOLATION_BIAS_SOURCE,
        },

        "trend": {
            "trend": "DUMMY",
            "bias": bias,
            "reason": (
                "Dummy endpoint — NiftyTrendService not invoked. "
                "Bias was supplied directly as the option_type query param."
            ),
        },

        "strike_generation": {
            "step": NIFTY_STRIKE_STEP,
            "nifty_ltp": float(nifty_ltp),
            "formula": (
                f"round({nifty_ltp:.2f} / {NIFTY_STRIKE_STEP}) * "
                f"{NIFTY_STRIKE_STEP}"
            ),
            "strike": strike,
            "option_type": bias,
            "note": (
                "Strike was supplied directly in the query — the formula "
                "shown here is what would have been used at real isolation."
            ),
        },

        "option_snapshot": {
            "instrument_key": instrument.get("instrument_key"),
            "trading_symbol": instrument.get("trading_symbol"),
            "option_ltp": float(option_ltp),
            "option_ltp_source": "dummy_param",
        },

        "mode_decision": {
            "standard_execution_mode": mode_payload["standard_execution_mode"],
            "early_extreme_reversal": mode_payload["early_extreme_reversal"],
            "reason": mode_payload.get("execution_mode_reason"),
        },
    }

    logger.info(
        "Dummy isolation built: %s | mode=%s | dummy=%s | strike=%d | bias=%s",
        record["trading_symbol"],
        record["standard_execution_mode"],
        record["is_dummy"],
        strike,
        bias,
    )
    return record