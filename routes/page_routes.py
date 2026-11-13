# routes/page_routes.py
"""HTML page routes.

Serves the Jinja2-rendered pages that make up the UI:

    GET /                     -> index.html (multi-index dashboard)
    GET /charts               -> instrument_list.html (Upstox search)
    GET /chart/{instrument_key}
                              -> chart.html (candles + EMA + OR levels)
    GET /isolated             -> isolated_instrument.html (level-isolation page)
    GET /positions            -> positions.html (position dashboard)
    GET /favicon.ico          -> 204 No Content

The templates live at <project_root>/templates/. If your project uses a
different location, change the single ``_TEMPLATES_DIR`` line below.

Chart data is fetched directly from Upstox by the browser (via the
``/api/upstox/*`` proxy), so the chart route only needs to pass the
instrument metadata + the side-panel instrument list to the template.

The positions page is a pure shell: every action (list / lock /
unlock / clear / exit) is served by the sibling JSON router
``upstox_app/apis/position_routes.py`` mounted at ``/api/positions``
in ``main.py``. The template's JS talks to that API directly, so this
route does no broker or Mongo work — it just renders the HTML shell.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from core import config  # (likely already imported; add if missing)

from services.instrument_registry import instrument_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _resolve_underlying_name(entry: dict[str, Any]) -> str:
    return str(entry.get("underlying") or "").strip() or "—"


def _build_instrument_context(instrument_key: str) -> dict[str, Any]:
    """Return the ``instrument`` dict the chart template expects."""
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        return {
            "instrument_key": instrument_key,
            "trading_symbol": instrument_key,
            "instrument_type": "",
            "option_type": "",
            "strike_price": None,
            "expiry": None,
            "underlying_symbol": None,
            "underlying": None,
            "exchange": "NSE" if "|" not in instrument_key else instrument_key.split("|", 1)[0],
            "segment": None,
            "lot_size": None,
            "tick_size": None,
            "name": None,
        }

    strike_type = (entry.get("strike_type") or "").upper()
    is_index = bool(entry.get("is_index"))

    return {
        "instrument_key": entry.get("instrument_key"),
        "trading_symbol": entry.get("trading_symbol") or instrument_key,
        "instrument_type": strike_type,
        "option_type": strike_type,
        "strike_price": entry.get("strike_price"),
        "expiry": entry.get("expiry"),
        "underlying_symbol": _resolve_underlying_name(entry),
        "underlying": _resolve_underlying_name(entry),
        "exchange": "NSE",
        "segment": "NSE_INDEX" if is_index else "NSE_FO",
        "lot_size": None,
        "tick_size": None,
        "name": entry.get("trading_symbol"),
    }


def _build_side_panel_instruments() -> list[dict[str, Any]]:
    """Return the flat list the chart side panel renders as option cards."""
    out: list[dict[str, Any]] = []
    for e in instrument_registry.all():
        if e.get("is_index"):
            continue
        strike_type = (e.get("strike_type") or "").upper()
        if strike_type not in ("CE", "PE"):
            continue
        out.append({
            "instrument_key": e.get("instrument_key"),
            "option_type": strike_type,
            "strike_price": e.get("strike_price"),
            "expiry": e.get("expiry"),
        })
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Silence favicon 404s without shipping an asset."""
    return Response(status_code=204)


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """Multi-index dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": "NG7"},
    )


@router.get("/charts", response_class=HTMLResponse)
def charts_page(request: Request) -> HTMLResponse:
    """Instrument search page."""
    return templates.TemplateResponse(
        request=request,
        name="instrument_list.html",
        context={
            "page_title": "Instrument Search",
            "instruments": [],
            "app_name": "NG7",
        },
    )


@router.get("/chart/{instrument_key:path}", response_class=HTMLResponse)
def chart_page(request: Request, instrument_key: str) -> HTMLResponse:
    """Chart page."""
    instrument = _build_instrument_context(instrument_key)
    instrument_name = instrument.get("trading_symbol") or instrument_key

    return templates.TemplateResponse(
        request=request,
        name="chart.html",
        context={
            "app_name": "NG7",
            "instrument_key": instrument_key,
            "instrument_name": instrument_name,
            "instrument": instrument,
            "instruments": _build_side_panel_instruments(),
            "total_candles": 0,
            # NEW — single-source-of-truth for the SR Params card
            "sr_left_bars": config.LIVE_SR_LEFT_BARS,
            "sr_right_bars": config.LIVE_SR_RIGHT_BARS,
        },
    )


@router.get("/isolated", response_class=HTMLResponse)
def isolated_page(request: Request) -> HTMLResponse:
    """Read-only daily performance dashboard.

    The page no longer reads or renders this process's local isolation
    engine state. Its browser reads today's upstream MongoDB snapshot
    through /live/performance/today-snapshot and uses the live index
    feed only while today's document has not yet been published.
    """
    return templates.TemplateResponse(
        request=request,
        name="isolated_instrument.html",
        context={
            "app_name": "NG7",
            "is_member": bool(getattr(request.state, "member", None)),
        },
    )

@router.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request) -> HTMLResponse:
    """Position dashboard shell.

    Renders ``templates/positions.html`` only. Every JSON action
    (list / lock / unlock / clear / exit) is served by
    ``upstox_app/apis/position_routes.py`` mounted at ``/api/positions``
    — the template's JS calls that router directly. Doing no server-
    side fetch here keeps the page loadable even when Upstox or
    MongoDB is degraded; the client-side code surfaces those errors
    in-place rather than via a broken HTTP response.
    """
    is_member = bool(getattr(request.state, "member", None))
    if is_member:
        return templates.TemplateResponse(
            request=request,
            name="coming_soon.html",
            context={
                "app_name": "NG7",
                "feature_name": "Positions",
                "feature_description": "The member positions workspace is being prepared. Position monitoring and order actions will be introduced in a future release.",
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="positions.html",
        context={"app_name": "NG7"},
    )