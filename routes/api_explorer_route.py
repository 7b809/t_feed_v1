# routes/api_explorer_route.py
"""API Explorer page.

Renders ``templates/api_explorer.html`` — a self-contained UI that reads
``/openapi.json`` at runtime and lets the operator browse + invoke every
route exposed by this service. No state, no DB, no auth side-effects.

Templates are resolved with the same ``Path(__file__).resolve().parents[1]
/ "templates"`` pattern used by every other page router in this project
(``routes/page_routes.py``, ``routes/log_routes.py``,
``routes/expired_chart_routes.py``, ``routes/layout_routes.py``). This
avoids depending on a ``config.TEMPLATES_DIR`` constant that does not
exist in ``core/config.py``.

Mount with::

    from routes.api_explorer_route import router as api_explorer_router
    app.include_router(api_explorer_router)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])

# --------------------------------------------------------------------------- #
# Template discovery — matches routes/page_routes.py, log_routes.py, etc.     #
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get(
    "/api-explorer",
    response_class=HTMLResponse,
    summary="API Explorer Page",
    description=(
        "Interactive browser for every route exposed by this service. "
        "Fetches `/openapi.json` at runtime, groups routes by tag, "
        "and provides a live `Try it` panel."
    ),
)
async def api_explorer_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="api_explorer.html",
        context={"title": "API Explorer"},
    )