# services/index_touch_detector.py
"""Detect R3-side / S3-side touches from an index LTP and pivot levels.

Strategy §4.2 semantics:
  • R3-side: LTP >= R3_Threshold → touch
  • S3-side: LTP <= S3_Threshold → touch

Raw R3/S3 remain reference values; isolation is triggered exclusively
by the midpoint thresholds in the index OR-level data.

Returns a touch dict describing the triggered level (name + value) or None.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core.config import TIMEZONE
from core.logger import get_logger

logger = get_logger(__file__)


class IndexTouchDetector:
    """Stateless touch detector — safe to reuse across ticks."""

    def detect(
        self,
        index_key: str,
        ltp: Optional[float],
        levels: dict,
    ) -> Optional[dict]:
        if ltp is None or not levels:
            return None

        r3_thr = levels.get("R3_Threshold")
        s3_thr = levels.get("S3_Threshold")

        # Isolation is triggered at the midpoint threshold, not at the
        # outer R3/S3 level. These are calculated from the index's own
        # OR pivot levels, independently of isolated option pivots.
        if r3_thr is not None and ltp >= float(r3_thr):
            return self._touch(
                index_key, "R3",
                trigger_level=float(r3_thr),
                trigger_level_name="R3_Threshold",
                trigger_value=ltp,
                level_ref={
                    "R3": levels.get("R3"),
                    "R2": levels.get("R2"),
                    "R3_Threshold": float(r3_thr),
                },
            )

        if s3_thr is not None and ltp <= float(s3_thr):
            return self._touch(
                index_key, "S3",
                trigger_level=float(s3_thr),
                trigger_level_name="S3_Threshold",
                trigger_value=ltp,
                level_ref={
                    "S3": levels.get("S3"),
                    "S2": levels.get("S2"),
                    "S3_Threshold": float(s3_thr),
                },
            )

        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _touch(
        index_key: str,
        side: str,
        *,
        trigger_level: float,
        trigger_level_name: str,
        trigger_value: float,
        level_ref: dict,
    ) -> dict:
        return {
            "index_key": index_key,
            "side": side,
            "trigger_level": trigger_level,
            "trigger_level_name": trigger_level_name,
            "trigger_value": trigger_value,
            "detected_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            "level_ref": level_ref,
        }