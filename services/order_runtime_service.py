"""Runtime order-enable toggle shared by API routes and the Telegram bot.

Default: ENABLED at process start (as requested).

Any code path that would place, exit, or cancel an Upstox order MUST
check ``order_runtime_service.is_enabled()`` first and return a
``disabled`` result when the flag is off. The flag is process-local
and thread-safe. It resets to ENABLED on every restart — by design, so
a crash / deploy does not leave the engine silently unable to trade.

Public API
----------
    order_runtime_service.is_enabled() -> bool
    order_runtime_service.enable(source="api") -> dict
    order_runtime_service.disable(source="telegram") -> dict
    order_runtime_service.set(enabled, source="api") -> dict
    order_runtime_service.status() -> dict

Status payload::

    {
      "enabled": bool,
      "changed_at": ISO | None,
      "changed_by": str | None,
      "default_on_startup": True,
    }
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from core.logger import get_logger

logger = get_logger(__file__)


class OrderRuntimeService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled: bool = True                 # default ON at startup
        self._changed_at: str | None = None
        self._changed_by: str | None = None

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def enable(self, source: str = "api") -> dict[str, Any]:
        return self.set(True, source=source)

    def disable(self, source: str = "api") -> dict[str, Any]:
        return self.set(False, source=source)

    def set(self, enabled: bool, source: str = "api") -> dict[str, Any]:
        with self._lock:
            previous = self._enabled
            self._enabled = bool(enabled)
            if previous != self._enabled:
                self._changed_at = datetime.now(timezone.utc).isoformat()
                self._changed_by = str(source or "api")
                logger.info(
                    "[order-runtime] enabled=%s changed_by=%s (was %s)",
                    self._enabled, self._changed_by, previous,
                )
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "changed_at": self._changed_at,
                "changed_by": self._changed_by,
                "default_on_startup": True,
            }


order_runtime_service = OrderRuntimeService()