from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.option_service import options_cache
from services.history_service import (
    extract_candles_from_response,
    get_intraday_unit_and_interval,
)

logger = get_logger(__file__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ============================================================
# Chart Dashboard Page
# ============================================================


@router.get("/chart", response_class=HTMLResponse)
async def chart_dashboard(request: Request):
    """
    Serves the professional live candle chart dashboard.

    Dashboard behavior:
    - Loads available subscribed instruments from /chart/instruments.
    - User selects an instrument.
    - Frontend loads historical candles from /chart/candles.
    - Default candle load is last 7 days.
    - Frontend can lazy-load previous 7 day windows using from_date/to_date.
    - Frontend loads Opening Range levels from existing
      /opening-range/ema-context endpoint.
    - Chart displays candles with Opening Range High, Low, Average,
      R1/R2/R3 and S1/S2/S3 levels.
    - Frontend connects WebSocket for the selected instrument and updates
      the latest candle/live price in real time.
    """
    return templates.TemplateResponse(
        request,
        "chart_dashboard.html",
    )


# ============================================================
# Helpers
# ============================================================


def _safe_float(value, default=None):
    """
    Safely converts value to float.
    """
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int_from_float(value, default=None):
    """
    Safely converts numeric value to int through float.
    Useful for strike display and WebSocket query values.
    """
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _get_market_today() -> date:
    """
    Returns today's date in the configured market timezone.

    Default:
    - Asia/Kolkata
    """
    timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")

    try:
        return datetime.now(ZoneInfo(timezone_name)).date()
    except Exception:
        logger.warning(
            f"Unable to resolve timezone={timezone_name}. Falling back to local date."
        )
        return datetime.now().date()


def _parse_date_string(value: str | None, field_name: str) -> date | None:
    """
    Parses yyyy-mm-dd date string.
    """
    if not value:
        return None

    try:
        return date.fromisoformat(str(value))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be in yyyy-mm-dd format. Received: {value}",
        )


def _to_date_string(value) -> str | None:
    """
    Converts date/datetime/string into yyyy-mm-dd.
    """
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    return str(value)


def _get_historical_candle_response(
    api_instance,
    instrument_key: str,
    unit: str,
    interval: str,
    from_date: str,
    to_date: str,
):
    """
    Calls Upstox historical candle API with compatibility fallback.

    Different SDK versions may expose slightly different method names.
    This helper tries known HistoryV3Api method names.

    Expected common call shape:
        get_historical_candle_data1(
            instrument_key,
            unit,
            interval,
            to_date,
            from_date,
        )
    """
    attempts = []

    if hasattr(api_instance, "get_historical_candle_data1"):
        attempts.append(
            (
                "get_historical_candle_data1",
                api_instance.get_historical_candle_data1,
            )
        )

    if hasattr(api_instance, "get_historical_candle_data"):
        attempts.append(
            (
                "get_historical_candle_data",
                api_instance.get_historical_candle_data,
            )
        )

    if not attempts:
        raise RuntimeError(
            "No supported historical candle method found on "
            "upstox_client.HistoryV3Api."
        )

    last_type_error = None

    for method_name, method in attempts:
        try:
            logger.info(
                f"Trying historical candle API method={method_name}, "
                f"instrument_key={instrument_key}, "
                f"unit={unit}, "
                f"interval={interval}, "
                f"from_date={from_date}, "
                f"to_date={to_date}"
            )

            return method(
                instrument_key,
                unit,
                interval,
                to_date,
                from_date,
            )

        except TypeError as ex:
            last_type_error = ex

            logger.warning(
                f"Historical candle method signature failed. "
                f"method={method_name}, error={type(ex).__name__}: {ex}"
            )

            continue

    raise RuntimeError(
        "Historical candle API method exists but no supported call signature "
        f"matched. Last error: {last_type_error}"
    )


def _build_index_instrument_payload(main_key: str) -> dict:
    """
    Builds payload for main index instrument.

    Note:
    - The existing project has option-specific WebSocket route /option.
    - For index live streaming, chart frontend can use /all-feeds and filter
      by instrument_key, or a future dedicated /ws/chart/instrument route
      can be added.
    """
    return {
        "instrument_key": main_key,
        "instrument_type": "INDEX",
        "strike_price": None,
        "expiry": None,
        "trading_symbol": "NIFTY 50",
        "display_name": "NIFTY 50 Index",
        "segment": "INDEX",
        "underlying_symbol": "NIFTY 50",
        "underlying_type": "INDEX",
        "chart_websocket": {
            "enabled": True,
            "mode": "all_feeds_filter",
            "endpoint": "/all-feeds",
            "query": None,
            "path": "/all-feeds",
            "description": (
                "Use /all-feeds and filter incoming live_tick payloads by "
                "instrument_key for index chart updates."
            ),
        },
    }


def _build_option_display_name(item: dict) -> str:
    """
    Builds readable display text for option instruments.
    """
    instrument_key = item.get("instrument_key")
    instrument_type = str(item.get("instrument_type") or "").upper()
    strike_price = item.get("strike_price")
    trading_symbol = item.get("trading_symbol") or item.get("name") or instrument_key

    if strike_price is not None and instrument_type in ["CE", "PE"]:
        try:
            return f"{int(float(strike_price))} {instrument_type} | {trading_symbol}"
        except Exception:
            return f"{strike_price} {instrument_type} | {trading_symbol}"

    return str(trading_symbol or instrument_key)


def _build_chart_websocket_payload(item: dict) -> dict:
    """
    Builds WebSocket metadata for frontend live chart connection.

    For option instruments:
        /option?strike=<strike>&striketype=<ce|pe>

    For index instruments:
        /all-feeds and filter by instrument_key on frontend.

    This does not create a new WebSocket backend route.
    It only tells the frontend which existing WebSocket route to use.
    """
    instrument_key = item.get("instrument_key")
    instrument_type = str(item.get("instrument_type") or "").upper()
    strike_price = item.get("strike_price")

    if instrument_type in ["CE", "PE"] and strike_price is not None:
        strike_int = _safe_int_from_float(strike_price)

        if strike_int is not None:
            striketype = instrument_type.lower()
            path = f"/option?strike={strike_int}&striketype={striketype}"

            return {
                "enabled": True,
                "mode": "option",
                "endpoint": "/option",
                "query": {
                    "strike": strike_int,
                    "striketype": striketype,
                },
                "path": path,
                "description": (
                    "Use this option-specific WebSocket path for live selected "
                    "instrument tick updates."
                ),
            }

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key or instrument_type == "INDEX":
        return {
            "enabled": True,
            "mode": "all_feeds_filter",
            "endpoint": "/all-feeds",
            "query": None,
            "path": "/all-feeds",
            "description": (
                "Use /all-feeds and filter incoming live_tick payloads by "
                "instrument_key for index chart updates."
            ),
        }

    return {
        "enabled": False,
        "mode": "unsupported",
        "endpoint": None,
        "query": None,
        "path": None,
        "description": (
            "Unable to build chart WebSocket path because instrument type or "
            "strike price is missing."
        ),
    }


def _get_contract_info_by_key(instrument_key: str) -> dict:
    """
    Resolves contract metadata from options_cache using instrument_key.
    """
    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key:
        return _build_index_instrument_payload(main_key)

    for item in options_cache.get("data", []) or []:
        if item.get("instrument_key") == instrument_key:
            contract_info = dict(item)
            contract_info["display_name"] = _build_option_display_name(item)
            contract_info["chart_websocket"] = _build_chart_websocket_payload(item)
            return contract_info

    fallback = {
        "instrument_key": instrument_key,
        "instrument_type": None,
        "strike_price": None,
        "expiry": None,
        "trading_symbol": instrument_key,
        "display_name": instrument_key,
    }

    fallback["chart_websocket"] = _build_chart_websocket_payload(fallback)

    return fallback


def _normalize_candle(raw_candle: list) -> dict | None:
    """
    Normalizes Upstox candle list into frontend chart format.

    Expected Upstox candle format:
    [
        timestamp,
        open,
        high,
        low,
        close,
        volume,
        oi
    ]
    """
    if not isinstance(raw_candle, list) or len(raw_candle) < 5:
        return None

    timestamp = raw_candle[0]
    open_price = _safe_float(raw_candle[1])
    high_price = _safe_float(raw_candle[2])
    low_price = _safe_float(raw_candle[3])
    close_price = _safe_float(raw_candle[4])
    volume = _safe_float(raw_candle[5], 0.0) if len(raw_candle) > 5 else 0.0
    oi = _safe_float(raw_candle[6], 0.0) if len(raw_candle) > 6 else 0.0

    if (
        timestamp is None
        or open_price is None
        or high_price is None
        or low_price is None
        or close_price is None
    ):
        return None

    return {
        "time": timestamp,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "oi": oi,
    }


# ============================================================
# Instrument Dropdown API
# ============================================================


@router.get("/chart/instruments")
async def get_chart_instruments():
    """
    Returns available instruments for the chart dropdown.

    Source:
    - options_cache["subscribed_keys"]
    - options_cache["data"]

    Includes:
    - Main NIFTY index if subscribed.
    - Filtered option contracts from options_cache.
    - chart_websocket metadata for live selected-instrument chart updates.
    """
    cache_data = options_cache.get("data", []) or []
    subscribed_keys = options_cache.get("subscribed_keys", []) or []
    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    instruments = []

    if main_key in subscribed_keys or not subscribed_keys:
        instruments.append(_build_index_instrument_payload(main_key))

    for item in cache_data:
        instrument_key = item.get("instrument_key")

        if not instrument_key:
            continue

        if subscribed_keys and instrument_key not in subscribed_keys:
            continue

        instrument_type = str(item.get("instrument_type") or "").upper()
        strike_price = item.get("strike_price")
        trading_symbol = (
            item.get("trading_symbol") or item.get("name") or instrument_key
        )

        instrument_payload = {
            "instrument_key": instrument_key,
            "instrument_type": instrument_type,
            "strike_price": strike_price,
            "expiry": item.get("expiry"),
            "trading_symbol": trading_symbol,
            "display_name": _build_option_display_name(item),
            "segment": item.get("segment"),
            "underlying_symbol": item.get("underlying_symbol"),
            "underlying_type": item.get("underlying_type"),
        }

        instrument_payload["chart_websocket"] = _build_chart_websocket_payload(
            instrument_payload
        )

        instruments.append(instrument_payload)

    instruments = sorted(
        instruments,
        key=lambda item: (
            0 if item.get("instrument_type") == "INDEX" else 1,
            str(item.get("instrument_type") or ""),
            float(item.get("strike_price") or 0),
            str(item.get("trading_symbol") or ""),
        ),
    )

    return {
        "status": "success",
        "message": "Chart instruments loaded successfully.",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get("total_contracts"),
        "subscribed_keys_count": len(subscribed_keys),
        "count": len(instruments),
        "websocket_usage": {
            "option_live_chart": "/option?strike=<strike>&striketype=<ce|pe>",
            "index_live_chart": "/all-feeds",
            "note": (
                "Frontend should connect to chart_websocket.path from the selected "
                "instrument and update the latest candle using live_tick payloads."
            ),
        },
        "instruments": instruments,
    }


# ============================================================
# Candle Data API
# ============================================================


@router.get("/chart/candles")
async def get_chart_candles(
    instrument_key: str = Query(
        ...,
        description="Instrument key, for example NSE_FO|41012 or NSE_INDEX|Nifty 50.",
    ),
    interval: str = Query(
        default="1minute",
        description=(
            "Candle interval. Supported examples: "
            "1minute, 3minute, 5minute, 15minute, 30minute."
        ),
    ),
    mode: str = Query(
        default="historical",
        description="historical or intraday. historical is used by chart page.",
    ),
    days: int = Query(
        default=7,
        ge=1,
        le=30,
        description="Number of days to load when from_date is not provided.",
    ),
    from_date: str | None = Query(
        default=None,
        description="Start date in yyyy-mm-dd format.",
    ),
    to_date: str | None = Query(
        default=None,
        description="End date in yyyy-mm-dd format.",
    ),
):
    """
    Returns candles for selected instrument.

    Default behavior:
    - mode=historical
    - days=7
    - Loads last 7 days including today, based on market timezone.

    Pagination behavior:
    - Frontend can request older windows:
      /chart/candles?instrument_key=...&interval=1minute&mode=historical
      &from_date=2026-08-06&to_date=2026-08-12

    Intraday behavior:
    - mode=intraday returns today's intraday candles only.

    Response candle format:
    {
        "time": "2026-08-19T09:15:00+05:30",
        "open": 120.0,
        "high": 125.0,
        "low": 118.0,
        "close": 123.5,
        "volume": 10000,
        "oi": 500000
    }

    Live chart updates:
    - Initial/historical candles come from this route.
    - Realtime candle updates should come from selected instrument WebSocket.
    - The required WebSocket path is available in contract_info.chart_websocket.
    """
    if not instrument_key:
        raise HTTPException(
            status_code=400,
            detail="instrument_key is required.",
        )

    mode_text = str(mode or "historical").lower().strip()

    if mode_text not in ["historical", "intraday"]:
        raise HTTPException(
            status_code=400,
            detail="mode must be either historical or intraday.",
        )

    unit, intra_interval = get_intraday_unit_and_interval(interval)

    if not unit or not intra_interval:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported interval: {interval}. "
                "Allowed examples: 1minute, 3minute, 5minute, 15minute, 30minute."
            ),
        )

    market_today = _get_market_today()

    parsed_to_date = _parse_date_string(to_date, "to_date")
    parsed_from_date = _parse_date_string(from_date, "from_date")

    if parsed_to_date is None:
        parsed_to_date = market_today

    if parsed_from_date is None:
        parsed_from_date = parsed_to_date - timedelta(days=days - 1)

    if parsed_from_date > parsed_to_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "from_date must be less than or equal to to_date. "
                f"Received from_date={parsed_from_date}, to_date={parsed_to_date}."
            ),
        )

    resolved_from_date = parsed_from_date.isoformat()
    resolved_to_date = parsed_to_date.isoformat()

    contract_info = _get_contract_info_by_key(instrument_key)

    try:
        logger.info(
            f"Chart candle request started. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"interval={interval}, "
            f"unit={unit}, "
            f"intra_interval={intra_interval}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"days={days}"
        )

        api_instance = upstox_client.HistoryV3Api()

        if mode_text == "intraday":
            api_response = api_instance.get_intra_day_candle_data(
                instrument_key,
                unit,
                intra_interval,
            )
        else:
            api_response = _get_historical_candle_response(
                api_instance=api_instance,
                instrument_key=instrument_key,
                unit=unit,
                interval=intra_interval,
                from_date=resolved_from_date,
                to_date=resolved_to_date,
            )

        raw_candles = extract_candles_from_response(api_response)

        candles = []
        for raw_candle in raw_candles:
            normalized = _normalize_candle(raw_candle)
            if normalized:
                candles.append(normalized)

        candles = sorted(
            candles,
            key=lambda candle: str(candle.get("time") or ""),
        )

        logger.info(
            f"Chart candle request completed. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"candles_count={len(candles)}"
        )

        return {
            "status": "success",
            "message": "Candles loaded successfully.",
            "instrument_key": instrument_key,
            "contract_info": contract_info,
            "mode": mode_text,
            "from_date": resolved_from_date,
            "to_date": resolved_to_date,
            "days": days,
            "interval": interval,
            "unit": unit,
            "intraday_interval": intra_interval,
            "candles_count": len(candles),
            "candles": candles,
            "live_chart": {
                "enabled": bool(
                    contract_info.get("chart_websocket", {}).get("enabled")
                ),
                "websocket": contract_info.get("chart_websocket"),
                "update_source": "selected_instrument_websocket",
                "frontend_logic": (
                    "Use candles from this response for chart load. "
                    "Then connect to websocket.path and update the latest "
                    "candle using incoming live_tick LTP/OHLC values."
                ),
            },
            "generated_at": datetime.now().isoformat(),
        }

    except ApiException as ex:
        error_body = getattr(ex, "body", str(ex))

        logger.error(
            f"Chart candle Upstox API error. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"error={error_body}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Upstox candle API error: {error_body}",
        )

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            f"Chart candle request failed. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"error={error_message}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Chart candle fetch failed: {error_message}",
        )
