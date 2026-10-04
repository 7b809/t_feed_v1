"""
Web UI package.

Serves the Jinja2 templates (base, index, chart, instrument_list,
isolated_ema_dashboard, orders, show_logs) plus the JSON APIs,
WebSocket endpoints and refresh controls those templates depend on.
"""

from web.page_router import router as web_page_router
from web.api_router import router as web_api_router
from web.ws_router import router as web_ws_router

__all__ = ["web_page_router", "web_api_router", "web_ws_router"]