"""
Pick the best candidate from a list of touch events.

Priority rules:
    1. Higher level priority wins.
       r3/s3 > r2/s2 > r1/s1
    2. Earliest touch_time wins (first signal of the day).
    3. Smallest distance from the main index LTP wins (closest to ATM).

Level priority values (higher = stronger signal):
    r3: 60  s3: 60
    r2: 40  s2: 40
    r1: 20  s1: 20
"""

from __future__ import annotations

from typing import List, Optional

from ema_app.isolation.state import TouchEvent


LEVEL_PRIORITY = {
    "r3": 60,
    "s3": 60,
    "r2": 40,
    "s2": 40,
    "r1": 20,
    "s1": 20,
}


def get_level_priority(level: str) -> int:
    return LEVEL_PRIORITY.get(str(level or "").lower(), 0)


def _sort_key(event: TouchEvent) -> tuple:
    """
    Smaller tuple = better.
    Tuple layout: (-priority, touch_time, distance_or_inf)
    """
    priority = get_level_priority(event.level)
    distance = (
        event.distance_from_index
        if event.distance_from_index is not None
        else float("inf")
    )
    return (-priority, event.touch_time, distance)


def choose_best_candidate(
    candidates: List[TouchEvent],
) -> Optional[TouchEvent]:
    """Return the best touch event, or None when the list is empty."""
    if not candidates:
        return None
    return sorted(candidates, key=_sort_key)[0]


def is_better_than_current(
    candidate: TouchEvent,
    current: Optional[TouchEvent],
) -> bool:
    """
    Decide whether `candidate` should replace `current` as the
    isolated instrument's source touch.

    A candidate is better only when it beats the current one on the
    sort key — i.e. strictly stronger priority, or equal priority and
    strictly earlier time, or equal on both and strictly closer.
    """
    if current is None:
        return True

    cand_priority = get_level_priority(candidate.level)
    curr_priority = get_level_priority(current.level)
    if cand_priority != curr_priority:
        return cand_priority > curr_priority

    if candidate.touch_time != current.touch_time:
        return candidate.touch_time < current.touch_time

    cand_dist = (
        candidate.distance_from_index
        if candidate.distance_from_index is not None
        else float("inf")
    )
    curr_dist = (
        current.distance_from_index
        if current.distance_from_index is not None
        else float("inf")
    )
    return cand_dist < curr_dist