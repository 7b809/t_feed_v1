# services/index_touch_detector.py
"""Detect R3-side / S3-side touches from an index LTP and pivot levels.

Strategy §4.2 semantics:
  • R3-side:  LTP >= min(R3, R3_Threshold)   → touch
             (R3_Threshold is strictly below R3, so it triggers first)
  • S3-side:  LTP <= max(S3, S3_Threshold)   → touch
             (S3_Threshold is strictly above S3, so it triggers first)

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

        r3 = levels.get("R3")
        r3_thr = levels.get("R3_Threshold")
        s3 = levels.get("S3")
        s3_thr = levels.get("S3_Threshold")

        # --- R3-side ---
        if r3 is not None and r3_thr is not None:
            trigger_level = min(r3, r3_thr)  # == r3_thr
            if ltp >= trigger_level:
                return self._touch(
                    index_key, "R3",
                    trigger_level=trigger_level,
                    trigger_level_name="R3_Threshold" if trigger_level == r3_thr else "R3",
                    trigger_value=ltp,
                    level_ref={"R3": r3, "R3_Threshold": r3_thr},
                )

        # --- S3-side ---
        if s3 is not None and s3_thr is not None:
            trigger_level = max(s3, s3_thr)  # == s3_thr
            if ltp <= trigger_level:
                return self._touch(
                    index_key, "S3",
                    trigger_level=trigger_level,
                    trigger_level_name="S3_Threshold" if trigger_level == s3_thr else "S3",
                    trigger_value=ltp,
                    level_ref={"S3": s3, "S3_Threshold": s3_thr},
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