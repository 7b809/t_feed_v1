# services/execution_mode_service.py
"""Pin STANDARD or EARLY_EXTREME_REVERSAL at isolation time (strategy §6.1)."""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from core.config import (
    TIMEZONE,
    ISOLATION_MODE_CUTOFF_HOUR,
    ISOLATION_MODE_CUTOFF_MINUTE,
    ISOLATION_MODE_PRICE_HIGH,
    ISOLATION_MODE_PRICE_LOW,
)
from core.logger import get_logger

logger = get_logger(__file__)


def pin_execution_mode(touch_time_iso: str, option_ltp: float) -> dict:
    """Return the immutable execution-mode payload for the isolation record.

    Per strategy §6.1:
      EARLY_EXTREME_REVERSAL requires **both**:
        1. touch time strictly before 10:30:00 IST
        2. isolated option LTP strictly > 250 OR strictly < 100
      Anything else → STANDARD.
      Missing/zero price → STANDARD (safe default, no EER without a price).
    """
    dt = datetime.fromisoformat(touch_time_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))

    touch_time_only = dt.timetz().replace(tzinfo=None)
    cutoff = dtime(ISOLATION_MODE_CUTOFF_HOUR, ISOLATION_MODE_CUTOFF_MINUTE)

    price_available = option_ltp is not None and option_ltp > 0
    time_ok = touch_time_only < cutoff
    price_ok = price_available and (
        option_ltp > ISOLATION_MODE_PRICE_HIGH
        or option_ltp < ISOLATION_MODE_PRICE_LOW
    )

    if time_ok and price_ok:
        mode = "EARLY_EXTREME_REVERSAL"
        reason = (
            f"time {touch_time_only} < cutoff {cutoff} AND "
            f"price {option_ltp:.2f} outside [{ISOLATION_MODE_PRICE_LOW}, "
            f"{ISOLATION_MODE_PRICE_HIGH}]"
        )
    else:
        mode = "STANDARD"
        reasons = []
        if not time_ok:
            reasons.append(f"time {touch_time_only} not < cutoff {cutoff}")
        if not price_available:
            reasons.append("option LTP unavailable")
        elif not price_ok:
            reasons.append(
                f"price {option_ltp:.2f} inside [{ISOLATION_MODE_PRICE_LOW}, "
                f"{ISOLATION_MODE_PRICE_HIGH}]"
            )
        reason = " | ".join(reasons)

    logger.info("Execution mode pinned: %s (%s)", mode, reason)

    return {
        "standard_execution_mode": mode,
        "early_extreme_reversal": mode == "EARLY_EXTREME_REVERSAL",
        "execution_mode_price": float(option_ltp) if price_available else None,
        "execution_mode_decided_at": touch_time_iso,
        "execution_mode_reason": reason,
    }