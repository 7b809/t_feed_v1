"""Scheduler entry point for the daily time-window Supertrend scan."""
from services.time_window_supertrend_service import (
    run_time_window_supertrend_cycle,
)


def run_time_window_supertrend_scan():
    """Run one guarded scan cycle; service-level exceptions are logged per leg."""
    return run_time_window_supertrend_cycle()
