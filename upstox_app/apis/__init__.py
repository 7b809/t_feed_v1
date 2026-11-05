# upstox_app/apis/__init__.py
"""API routers packaged under ``upstox_app.apis``.

Import the routers here so ``main.py`` can pull them all from one place
without touching individual modules. Each import is wrapped in a
``try/except ImportError`` so a partially-migrated tree still boots —
missing routers simply don't get mounted.
"""

from __future__ import annotations

__all__ = [
    "expired_instruments_router",
    "margin_calc_router",
]


# ---- expired instruments API (pre-existing) -------------------------------- #
try:
    from upstox_app.apis.expired_instruments_api import (
        router as expired_instruments_router,
    )
except ImportError:  # pragma: no cover — optional during migration
    expired_instruments_router = None  # type: ignore[assignment]

