from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from core import config
from services.option_service import get_feed_by_instrument_key, get_subscribed_instrument_keys


router = APIRouter()

templates = Jinja2Templates(directory="templates")


@router.get("/api/live-subscriptions")
async def live_subscriptions():
    subscribed_keys = list(dict.fromkeys(get_subscribed_instrument_keys() or []))
    main_index_key = str(getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50"))
    subscriptions = [
        get_feed_by_instrument_key(key) or {"instrument_key": key}
        for key in subscribed_keys
    ]
    if main_index_key not in subscribed_keys:
        subscriptions.append(get_feed_by_instrument_key(main_index_key) or {
            "instrument_key": main_index_key, "instrument_type": "INDEX", "trading_symbol": "NIFTY 50"
        })
    return {"active": subscriptions}


# ============================================================
# HOME PAGE
# ============================================================


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def home_page(request: Request):
    """
    Renders the main live option feed dashboard home page.
    """

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
        },
    )


# ============================================================
# ISOLATED EMA DASHBOARD
# ============================================================


@router.get(
    "/isolated-dashboard",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def isolated_dashboard(request: Request):
    """
    Renders the Isolated EMA Dashboard.

    IMPORTANT:
    This uses Jinja2 TemplateResponse because
    isolated_ema_dashboard.html extends base.html.
    """

    return templates.TemplateResponse(
        request=request,
        name="isolated_ema_dashboard.html",
        context={
            "request": request,
        },
    )


# ============================================================
# ISOLATED EMA DASHBOARD ALIAS
# ============================================================


@router.get(
    "/isolated-ema-dashboard",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def isolated_ema_dashboard_alias(request: Request):
    """
    Alias route for the Isolated EMA Dashboard.
    """

    return templates.TemplateResponse(
        request=request,
        name="isolated_ema_dashboard.html",
        context={
            "request": request,
        },
    )
