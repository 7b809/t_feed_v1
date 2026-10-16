"""Shared helpers for writing human-readable "meta test" files.

A meta test file documents *why* a strategy stage made the decision it
did, so an operator can audit the pipeline after a catch-up execution
(project started mid-day, after the scheduled stage time had passed).

Each stage writes one meta file next to its output:

    data/strategy/stage_1_initial/<TICKER>/<INDEX>/stage_1_meta_test.json
    data/strategy/stage_2_isolation/<TICKER>/<INDEX>/stage_2_meta_test.json
    data/strategy/stage_3_r2_activated/stage_3_meta_test.json
    data/strategy/stage_4_order/<TICKER>/<INDEX>/order_meta_test.json

The meta file always contains:
    - stage name
    - execution_mode (startup / daily / manual / scheduled)
    - catchup (bool — true when the pipeline ran late)
    - scheduled_time_ist (HH:MM)
    - actual_time_ist   (HH:MM:SS)
    - explanation       (human-readable "why this decision was made")
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config


def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def market_now() -> datetime:
    return datetime.now(_market_tz())


def market_hhmmss() -> str:
    return market_now().strftime("%H:%M:%S")


def market_iso() -> str:
    return market_now().isoformat()


def write_meta(
    path: Path,
    *,
    stage: str,
    mode: str,
    catchup: bool,
    scheduled_time_ist: str,
    explanation: str,
    payload: dict[str, Any],
) -> None:
    """Write a single meta test file (JSON). Never raises."""
    meta = {
        "stage": stage,
        "execution_mode": mode,
        "catchup": catchup,
        "scheduled_time_ist": scheduled_time_ist,
        "actual_time_ist": market_hhmmss(),
        "actual_timestamp": market_iso(),
        "day": market_now().date().isoformat(),
        "explanation": explanation,
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(meta, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        # Meta files are advisory — never fail the pipeline over them.
        import logging
        logging.getLogger(__name__).exception(
            "Failed writing meta file %s", path,
        )