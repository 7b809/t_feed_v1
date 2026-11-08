# services/touch_arbitration_service.py
"""Priority-based arbitration for simultaneous index touches (strategy §4.3)."""

from __future__ import annotations

from typing import Optional

from core.config import STRATEGY_INDEX_PRIORITY
from core.logger import get_logger

logger = get_logger(__file__)


def arbitrate_touches(touches: list[dict]) -> Optional[dict]:
    """Return the highest-priority touch, annotated with metadata.

    The returned dict gains:
        priority                 : index in STRATEGY_INDEX_PRIORITY
        simultaneous_touches     : the remaining touches (diagnostic)
    """
    if not touches:
        return None

    priority_map = {k: i for i, k in enumerate(STRATEGY_INDEX_PRIORITY)}

    sorted_touches = sorted(
        touches, key=lambda t: priority_map.get(t["index_key"], 999)
    )
    winner = dict(sorted_touches[0])
    winner["priority"] = priority_map.get(winner["index_key"], 999)
    winner["simultaneous_touches"] = [
        {
            "index_key": t["index_key"],
            "side": t["side"],
            "trigger_level": t["trigger_level"],
            "trigger_value": t["trigger_value"],
            "priority": priority_map.get(t["index_key"], 999),
        }
        for t in sorted_touches[1:]
    ]

    logger.info(
        "Arbitration: winner=%s/%s (priority=%d), simultaneous=%d",
        winner["index_key"], winner["side"],
        winner["priority"], len(winner["simultaneous_touches"]),
    )
    return winner