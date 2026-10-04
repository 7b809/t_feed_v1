"""HTML page routes: /, /charts, /chart/{key}, /isolated-dashboard, /orders, /logs"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.logger import get_logger
from web import service

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["UI"])


def _ctx(**extra: Any) -> Dict[str, Any]:
    """
    Build a template context.

    Starlette >= 0.29 injects ``request`` into the context automatically,
    so we must NOT put it here anymore.
    """
    base: Dict[str, Any] = {
        "app_name": service.APP_NAME,
        "page_title": "",
    }
    base.update(extra)
    return base


@router.get("/", response_class=HTMLResponse)
async def page_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=_ctx(
            indexes=service.index_meta(),
        ),
    )


@router.get("/charts", response_class=HTMLResponse)
async def page_charts(request: Request) -> HTMLResponse:
    # Every option contract across every enabled index.
    instruments = service.load_all_option_instruments()
    indexes = service.index_meta()
    return templates.TemplateResponse(
        request=request,
        name="instrument_list.html",
        context=_ctx(
            page_title="Chart Instruments",
            instruments=instruments,
            indexes=indexes,
        ),
    )


@router.get("/chart/{instrument_key:path}", response_class=HTMLResponse)
async def page_chart(request: Request, instrument_key: str) -> HTMLResponse:
    instrument = service.find_contract_by_key(instrument_key) or {}

    # Handle index keys like "NSE_INDEX|Nifty 50" or "BSE_INDEX|SENSEX".
    # Those are not option contracts, so we resolve the owning index name
    # from the key itself.
    if "_INDEX|" in instrument_key:
        idx_upper = instrument_key.upper()
        if "NIFTY" in idx_upper and "BANK" not in idx_upper and "FIN" not in idx_upper:
            index_name = "NIFTY"
        elif "SENSEX" in idx_upper:
            index_name = "SENSEX"
        elif "BANKNIFTY" in idx_upper or "BANK" in idx_upper:
            index_name = "BANKNIFTY"
        else:
            index_name = instrument_key.split("|", 1)[1].split()[0].upper()
        instrument_name = instrument_key.split("|", 1)[1]
    else:
        index_name = (
            instrument.get("index")
            or instrument.get("underlying_symbol")
            or instrument.get("underlying")
        )
        instrument_name = (
            instrument.get("trading_symbol")
            or instrument.get("name")
            or instrument_key
        )

    instruments: List[Dict[str, Any]] = (
        service.load_option_contracts(str(index_name)) if index_name else []
    )

    # Candles are NOT rendered server-side anymore; the template fetches
    # them from api.upstox.com during bootstrap so the initial HTML paint
    # is fast even for large candle histories.
    #
    # `indexes` is passed so the new three-tab instrument panel (Index /
    # Options / Search) can render every enabled index without an extra
    # round-trip.
    return templates.TemplateResponse(
        request=request,
        name="chart.html",
        context=_ctx(
            instrument_key=instrument_key,
            instrument=instrument,
            instrument_name=instrument_name,
            instruments=instruments,
            indexes=service.index_meta(),
            total_candles=0,
        ),
    )


@router.get("/isolated-dashboard", response_class=HTMLResponse)
async def page_isolated_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="isolated_ema_dashboard.html",
        context=_ctx(),
    )


@router.get("/orders", response_class=HTMLResponse)
async def page_orders(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="orders.html",
        context=_ctx(),
    )


@router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request) -> HTMLResponse:
    logs = service.list_log_files()
    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context=_ctx(logs=logs),
    )