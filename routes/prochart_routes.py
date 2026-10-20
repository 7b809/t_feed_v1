"""ProChart routes.

    GET  /prochart                       -> HTML page
    GET  /api/prochart/settings          -> current settings
    POST /api/prochart/settings          -> save settings (partial merge)
    POST /api/prochart/alert             -> save + buffer an alert
    GET  /api/prochart/alerts            -> last 100 alerts (in-memory)
    GET  /api/prochart/alerts/<key>      -> persisted alerts for an instrument

URL parameters
--------------
Every parameter is also accepted from the URL, so a chart can be
shared or bookmarked with a fully-specified configuration:

    /prochart?instrument_key=NSE_INDEX|Nifty 50
        -> instrument set; alerts OFF; trigger=after_r3_or_s3_touch
           (defaults). Saved to settings.

    /prochart?instrument_key=X&alerts=true
        -> instrument set; alerts ON; trigger=default. Saved.

    /prochart?instrument_key=X&alerts=true&trigger=after_s3_touch
        -> all three set from URL. Saved.

    /prochart
        -> no URL params; saved settings (or defaults) used. NOT saved.

Acknowledged rule (from the spec): when a URL carries ANY of these
parameters, the request becomes the new source of truth — omitted
parameters take their DEFAULT value (not the previously-saved value),
and the resulting configuration is persisted so the next visit
without URL params gets the same config.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services.instrument_registry import instrument_registry
from services.prochart_alerts_service import prochart_alerts_service
from services.prochart_settings_service import (
    DEFAULT_INSTRUMENT_KEY,
    DEFAULT_TRIGGER,
    VALID_TRIGGERS,
    prochart_settings_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prochart"])

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))

# --------------------------------------------------------------------------- #
# In-memory alert buffer (last 100)                                           #
# --------------------------------------------------------------------------- #
_ALERT_BUFFER: "deque[dict[str, Any]]" = deque(maxlen=100)

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _coerce_bool_param(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default

def _resolve_instrument_context(instrument_key: str) -> dict[str, Any]:
    """Return the ``instrument`` dict the template expects."""
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        try:
            entry = instrument_registry.register_adhoc(instrument_key)
        except Exception:
            logger.exception(
                "[prochart] register_adhoc failed for %s", instrument_key
            )
            entry = None

    if not entry:
        return {
            "instrument_key": instrument_key,
            "trading_symbol": instrument_key,
            "instrument_type": "",
            "option_type": "",
            "strike_price": None,
            "expiry": None,
            "underlying_symbol": None,
            "underlying": None,
            "exchange": (
                "NSE" if "|" not in instrument_key
                else instrument_key.split("|", 1)[0]
            ),
            "segment": None,
            "lot_size": None,
            "tick_size": None,
            "name": None,
        }

    strike_type = (entry.get("strike_type") or "").upper()
    is_index = bool(entry.get("is_index"))
    underlying = entry.get("underlying") or ""

    return {
        "instrument_key": entry.get("instrument_key"),
        "trading_symbol": entry.get("trading_symbol") or instrument_key,
        "instrument_type": strike_type,
        "option_type": strike_type,
        "strike_price": entry.get("strike_price"),
        "expiry": entry.get("expiry"),
        "underlying_symbol": underlying,
        "underlying": underlying,
        "exchange": "NSE",
        "segment": "NSE_INDEX" if is_index else "NSE_FO",
        "lot_size": None,
        "tick_size": None,
        "name": entry.get("trading_symbol"),
    }

# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #
@router.get("/prochart", response_class=HTMLResponse)
def prochart_page(
    request: Request,
    instrument_key: str | None = Query(default=None),
    alerts: str | None = Query(default=None),
    trigger: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the ProChart page.

    If ANY of the URL parameters is present, the request becomes the
    new saved configuration (unspecified parameters take their DEFAULT
    value, not the previously-saved value). Otherwise, the saved
    settings are used verbatim.
    """
    has_url_params = (
        bool(instrument_key)
        or (alerts is not None)
        or bool(trigger)
    )

    if has_url_params:
        # Build a fresh config from DEFAULTS, then apply URL overrides.
        new_settings: dict[str, Any] = {
            "chart_instrument_key": DEFAULT_INSTRUMENT_KEY,
            "alerts_enabled": False,
            "alert_config": {"trigger": DEFAULT_TRIGGER},
        }
        if instrument_key:
            new_settings["chart_instrument_key"] = instrument_key.strip()
        if alerts is not None:
            new_settings["alerts_enabled"] = _coerce_bool_param(alerts, False)
        if trigger:
            trigger_norm = str(trigger).strip()
            if trigger_norm in VALID_TRIGGERS:
                new_settings["alert_config"]["trigger"] = trigger_norm
            else:
                logger.warning(
                    "[prochart] invalid trigger in URL: %r — using default %s",
                    trigger_norm,
                    DEFAULT_TRIGGER,
                )

        settings = prochart_settings_service.save_settings(new_settings)
        logger.info(
            "[prochart] page loaded with URL overrides — instrument=%s "
            "alerts=%s trigger=%s (saved)",
            settings.get("chart_instrument_key"),
            settings.get("alerts_enabled"),
            settings.get("alert_config", {}).get("trigger"),
        )
    else:
        settings = prochart_settings_service.get_settings()

    resolved_key = (
        settings.get("chart_instrument_key") or DEFAULT_INSTRUMENT_KEY
    )
    instrument = _resolve_instrument_context(resolved_key)
    instrument_name = instrument.get("trading_symbol") or resolved_key

    return templates.TemplateResponse(
        request=request,
        name="prochart.html",
        context={
            "app_name": "Market Data Foundation",
            "instrument_key": resolved_key,
            "instrument_name": instrument_name,
            "instrument": instrument,
            "prochart_settings": settings,
            "valid_triggers": sorted(VALID_TRIGGERS),
            "default_trigger": DEFAULT_TRIGGER,
        },
    )

# --------------------------------------------------------------------------- #
# Settings API                                                                #
# --------------------------------------------------------------------------- #
@router.get("/api/prochart/settings")
def get_prochart_settings() -> dict[str, Any]:
    return prochart_settings_service.get_settings()

@router.post("/api/prochart/settings")
def save_prochart_settings(
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Merge-update the single ProChart settings document."""
    if isinstance(payload.get("alert_config"), dict):
        trigger = str(payload["alert_config"].get("trigger") or "").strip()
        if trigger and trigger not in VALID_TRIGGERS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_trigger",
                    "allowed": sorted(VALID_TRIGGERS),
                },
            )

    if "chart_instrument_key" in payload:
        key = str(payload.get("chart_instrument_key") or "").strip()
        if not key:
            raise HTTPException(
                status_code=400,
                detail={"error": "chart_instrument_key must not be empty"},
            )

    return prochart_settings_service.save_settings(payload)

# --------------------------------------------------------------------------- #
# Alert ingestion                                                             #
# --------------------------------------------------------------------------- #
@router.post("/api/prochart/alert")
def receive_prochart_alert(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Receive a live EMA-cross alert from the browser.

    Persists it to MongoDB + data folder (via
    ``prochart_alerts_service``), appends it to the in-memory ring
    buffer, and returns a small ack dict.
    """
    received_at = datetime.now(timezone.utc).isoformat()

    record = {
        "received_at": received_at,
        "instrument_key": payload.get("instrument_key"),
        "alert_type": payload.get("alert_type"),
        "cross_type": payload.get("cross_type"),
        "cross_time": payload.get("cross_time"),
        "cross_price": payload.get("cross_price"),
        "trigger_config": payload.get("trigger_config"),
        "trigger_context": payload.get("trigger_context"),
    }
    _ALERT_BUFFER.appendleft(record)

    # Persist (MongoDB + data folder)
    persisted = prochart_alerts_service.save_alert(payload)

    logger.info(
        "[prochart][ALERT] instrument=%s type=%s cross=%s price=%s "
        "trigger=%s at=%s persisted=%s",
        record["instrument_key"],
        record["alert_type"],
        record["cross_type"],
        record["cross_price"],
        record["trigger_config"],
        record["cross_time"],
        persisted.get("status"),
    )

    return {
        "status": "ok",
        "received_at": received_at,
        "persisted": persisted,
    }

@router.get("/api/prochart/alerts")
def list_prochart_alerts(limit: int = 50) -> dict[str, Any]:
    """Return the last ``limit`` alerts received by the test endpoint."""
    n = max(1, min(int(limit or 50), 100))
    items = list(_ALERT_BUFFER)[:n]
    return {"count": len(items), "alerts": items}

@router.get("/api/prochart/alerts/{instrument_key:path}")
def list_persisted_alerts(instrument_key: str) -> dict[str, Any]:
    """Return persisted alerts for one instrument (from MongoDB)."""
    return prochart_alerts_service.get_alerts(instrument_key)