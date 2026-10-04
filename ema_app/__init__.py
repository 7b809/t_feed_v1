"""
Live EMA crossover service.

Streams real-time 9/21 EMA crosses for every enabled index and option
contract, persists them to per-contract intraday_cross.json files, and
fans them out over a WebSocket with rich client-side filtering.

See README section "EMA App" for the full design.
"""

from ema_app.service import ema_service
from ema_app.ws_manager import ema_ws_manager

__all__ = ["ema_service", "ema_ws_manager"]