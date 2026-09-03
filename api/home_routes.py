from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()

templates = Jinja2Templates(directory="templates")


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