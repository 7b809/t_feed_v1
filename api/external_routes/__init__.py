"""External order-mutation API package.

Assembles the five sub-routers into a single ``router`` that the main FastAPI
app mounts at ``/api/external/v1``:

    from api.external_routes import router as external_router
    app.include_router(external_router)

Structure
---------
    _common.py     shared helpers (step→HTTP mapping, error translation)
    orders.py      /orders/*    regular + new place-order-service
    gtt.py         /gtt/*       GTT place / place-dual / modify / cancel / cleanup
    portfolio.py   /portfolio/* today-pnl

The router-level ``require_external_api_auth`` dependency is applied once
here so individual route modules do not need to repeat it. It reads
``body.api_token`` from the JSON body when enabled.
"""

from fastapi import APIRouter, Depends

from core.external_api_auth import require_external_api_auth

from . import gtt, orders, portfolio

router = APIRouter(
    prefix="/api/external/v1",
    tags=["external-orders"],
    dependencies=[Depends(require_external_api_auth)],
)

router.include_router(orders.router)
router.include_router(gtt.router)
router.include_router(portfolio.router)

__all__ = ["router"]