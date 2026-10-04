"""Configuration for the live EMA crossover service."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _b(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EmaAppConfig:
    # Master switch
    enabled: bool = _b("EMA_APP_ENABLED", True)

    # Crossover periods
    fast_period: int = _i("EMA_APP_FAST_PERIOD", 9)
    slow_period: int = _i("EMA_APP_SLOW_PERIOD", 21)

    # Session window (IST, HH:MM)
    market_open: str = os.getenv("EMA_APP_MARKET_OPEN", "09:15")
    market_close: str = os.getenv("EMA_APP_MARKET_CLOSE", "15:30")

    # Arm subscriptions slightly before open
    arm_offset_min: int = _i("EMA_APP_ARM_OFFSET_MIN", 1)

    # Finalization: close a minute M at M + N seconds
    finalize_delay_sec: int = _i("EMA_APP_FINALIZE_DELAY_SEC", 15)

    # Backfill on startup / arm / hard refresh
    backfill_on_start: bool = _b("EMA_APP_BACKFILL_ON_START", True)

    # Cooldown between successive backfill runs (seconds). Prevents the
    # startup backfill and the 09:14 arm backfill from doing double work.
    backfill_cooldown_sec: int = _i("EMA_APP_BACKFILL_COOLDOWN_SEC", 120)

    # Inter-request delay during backfill (seconds, per instrument)
    backfill_inter_request_delay_sec: float = _f(
        "EMA_APP_BACKFILL_INTER_REQUEST_DELAY_SEC", 0.05
    )

    # HTTP timeout for the intraday fetch during backfill
    intraday_http_timeout_sec: float = _f("EMA_APP_INTRADAY_HTTP_TIMEOUT_SEC", 15.0)

    # Persist only crosses (never per-minute snapshots)
    persist_crosses: bool = _b("EMA_APP_PERSIST_CROSSES", True)

    # WebSocket broadcast
    broadcast_enabled: bool = _b("EMA_APP_BROADCAST_ENABLED", True)

    # Streamer hook (best-effort)
    use_streamer: bool = _b("EMA_APP_USE_STREAMER", True)


ema_config = EmaAppConfig()