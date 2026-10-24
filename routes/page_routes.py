# File: routes/page_routes.py
"""HTML page routes.

Serves the Jinja2-rendered pages that make up the UI:

    GET /                     -> index.html (multi-index dashboard)
    GET /charts               -> instrument_list.html (Upstox search)
    GET /chart/{instrument_key}
                              -> chart.html (candles + live feed)
    GET /favicon.ico          -> 204 No Content

The templates live at <project_root>/templates/. If your project uses a
different location, change the single ``_TEMPLATES_DIR`` line below.

Chart data is fetched directly from Upstox by the browser (via the
``/api/upstox/*`` proxy), so the chart route only needs to pass the
instrument metadata + the side-panel instrument list to the template.

ProChart / Opening Range have been removed from this codebase. The
chart page is now candles + live feed only — the previous OR-level
overlay has been dropped along with the strategy layer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

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
    """Return the ``instrument`` dict the chart template expects.

    Falls back to a minimal stub when the key is not in the registry —
    the frontend still works because it fetches candles directly from
    Upstox.
    """
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
        context={"app_name": "Market Data Foundation"},
    )


@router.get("/charts", response_class=HTMLResponse)
def charts_page(request: Request) -> HTMLResponse:
    """Instrument search page.

    The search itself is performed against ``/api/upstox/search`` from
    the browser, so this page only needs the title.
    """
    return templates.TemplateResponse(
        request=request,
        name="instrument_list.html",
        context={
            "page_title": "Instrument Search",
            "instruments": [],
            "app_name": "Market Data Foundation",
        },
    )


@router.get("/chart/{instrument_key:path}", response_class=HTMLResponse)
def chart_page(request: Request, instrument_key: str) -> HTMLResponse:
    """Chart page.

    ``instrument_key`` arrives URL-encoded from the browser
    (e.g. ``NSE_FO%7C50201`` -> ``NSE_FO|50201``).
    """
    instrument = _build_instrument_context(instrument_key)
    instrument_name = instrument.get("trading_symbol") or instrument_key

    return templates.TemplateResponse(
        request=request,
        name="chart.html",
        context={
            "app_name": "Market Data Foundation",
            "instrument_key": instrument_key,
            "instrument_name": instrument_name,
            "instrument": instrument,
            "instruments": _build_side_panel_instruments(),
            "total_candles": 0,
        },
    )