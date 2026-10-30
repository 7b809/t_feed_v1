"""Multi-chart layout page + JSON API.

    GET  /layout_1                       -> HTML page (default layout)
    GET  /layout_1/{layout_id}           -> HTML page (specific layout)

    GET  /admin/layouts                  -> list layouts
    POST /admin/layouts                  -> create layout
    GET  /admin/layouts/{layout_id}      -> one layout
    PATCH /admin/layouts/{layout_id}     -> update one layout
    DELETE /admin/layouts/{layout_id}    -> delete one layout
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from core import config
from services.layout_service import layout_service

router = APIRouter(tags=["layout"])

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))


# --------------------------------------------------------------------------- #
# Page routes                                                                 #
# --------------------------------------------------------------------------- #
@router.get("/layout_1", response_class=HTMLResponse)
def layout_page_default(request: Request) -> HTMLResponse:
    layout = layout_service.get_default_layout() or layout_service.ensure_default_layout()
    return _render_layout(request, layout)


@router.get("/layout_1/{layout_id}", response_class=HTMLResponse)
def layout_page(request: Request, layout_id: str) -> HTMLResponse:
    layout = layout_service.get_layout(layout_id)
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return _render_layout(request, layout)


def _render_layout(request: Request, layout: dict[str, Any]) -> HTMLResponse:
    # Every pane starts on the same chart so multi-chart layouts load
    # quickly and match TradingView behaviour. The default pane is
    # always NSE_INDEX|Nifty 50 unless the saved layout overrides it.
    default_key = "NSE_INDEX|Nifty 50"
    panes = list(layout.get("panes") or [])
    while len(panes) < int(layout.get("grid") or 1):
        panes.append({"instrument_key": default_key, "interval": "5m"})

    return templates.TemplateResponse(
        request=request,
        name="layout.html",
        context={
            "app_name": "Market Data Foundation",
            "layout": layout,
            "panes": panes,
            "default_key": default_key,
            # NEW
            "sr_left_bars":  config.LIVE_SR_LEFT_BARS,
            "sr_right_bars": config.LIVE_SR_RIGHT_BARS,
        },
    )


# --------------------------------------------------------------------------- #
# JSON API                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/admin/layouts")
def list_layouts() -> dict[str, Any]:
    layouts = layout_service.list_layouts()
    return {"count": len(layouts), "layouts": layouts}


@router.post("/admin/layouts")
def create_layout(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = payload.get("name")
    grid = int(payload.get("grid") or 4)
    if grid not in (1, 2, 4):
        raise HTTPException(status_code=400, detail="grid must be 1, 2 or 4")
    panes = payload.get("panes") if isinstance(payload.get("panes"), list) else None
    is_default = bool(payload.get("is_default") or False)
    return layout_service.create_layout(
        name=name, grid=grid, panes=panes, is_default=is_default,
    )


@router.get("/admin/layouts/{layout_id}")
def get_layout(layout_id: str) -> dict[str, Any]:
    layout = layout_service.get_layout(layout_id)
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return layout


@router.patch("/admin/layouts/{layout_id}")
def update_layout(layout_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    layout = layout_service.update_layout(
        layout_id,
        name=payload.get("name"),
        grid=payload.get("grid"),
        panes=payload.get("panes"),
        is_default=payload.get("is_default"),
    )
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return layout


@router.delete("/admin/layouts/{layout_id}")
def delete_layout(layout_id: str) -> dict[str, Any]:
    ok = layout_service.delete_layout(layout_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Layout not found")
    return {"deleted": True, "id": layout_id}