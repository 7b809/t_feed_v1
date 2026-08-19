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

    timezone_name = getattr(
        config,
        "MARKET_TIMEZONE",
        "Asia/Kolkata",
    )

    try:
        return datetime.now(ZoneInfo(timezone_name)).date()

    except Exception:
        logger.warning(
            f"Unable to resolve timezone={timezone_name}. "
            "Falling back to local date."
        )

        return datetime.now().date()


def _parse_date_string(
    value: str | None,
    field_name: str,
) -> date | None:
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
            detail=(
                f"{field_name} must be in yyyy-mm-dd format. " f"Received: {value}"
            ),
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


# ============================================================
# Historical Candle API Helper
# ============================================================


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
                "Historical candle method signature failed. "
                f"method={method_name}, "
                f"error={type(ex).__name__}: {ex}"
            )

            continue

    raise RuntimeError(
        "Historical candle API method exists but no supported call "
        "signature matched. "
        f"Last error: {last_type_error}"
    )


# ============================================================
# Index Instrument Payload
# ============================================================


def _build_index_instrument_payload(
    main_key: str,
) -> dict:
    """
    Builds payload for main index instrument.

    Note:
    - The existing project has option-specific WebSocket route /option.
    - For index live streaming, chart frontend can use /all-feeds
      and filter by instrument_key.
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
                "Use /all-feeds and filter incoming live_tick "
                "payloads by instrument_key for index chart updates."
            ),
        },
    }


# ============================================================
# Option Display Name
# ============================================================


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
            return (
                f"{int(float(strike_price))} " f"{instrument_type} | {trading_symbol}"
            )

        except Exception:
            return f"{strike_price} " f"{instrument_type} | {trading_symbol}"

    return str(trading_symbol or instrument_key)


# ============================================================
# Chart WebSocket Payload
# ============================================================


def _build_chart_websocket_payload(
    item: dict,
) -> dict:
    """
    Builds WebSocket metadata for frontend live chart connection.

    For option instruments:
        /option?strike=<strike>&striketype=<ce|pe>

    For index instruments:
        /all-feeds and filter by instrument_key.

    This does not create a new WebSocket backend route.
    It only tells the frontend which existing WebSocket route to use.
    """

    instrument_key = item.get("instrument_key")

    instrument_type = str(item.get("instrument_type") or "").upper()

    strike_price = item.get("strike_price")

    # --------------------------------------------------------
    # OPTION
    # --------------------------------------------------------

    if instrument_type in ["CE", "PE"] and strike_price is not None:

        strike_int = _safe_int_from_float(strike_price)

        if strike_int is not None:

            strike_type = instrument_type.lower()

            path = f"/option?" f"strike={strike_int}" f"&striketype={strike_type}"

            return {
                "enabled": True,
                "mode": "option",
                "endpoint": "/option",
                "query": {
                    "strike": strike_int,
                    "striketype": strike_type,
                },
                "path": path,
                "description": (
                    "Use this option-specific WebSocket "
                    "path for live selected instrument "
                    "tick updates."
                ),
            }

    # --------------------------------------------------------
    # INDEX
    # --------------------------------------------------------

    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    if instrument_key == main_key or instrument_type == "INDEX":

        return {
            "enabled": True,
            "mode": "all_feeds_filter",
            "endpoint": "/all-feeds",
            "query": None,
            "path": "/all-feeds",
            "description": (
                "Use /all-feeds and filter incoming "
                "live_tick payloads by instrument_key "
                "for index chart updates."
            ),
        }

    # --------------------------------------------------------
    # UNSUPPORTED
    # --------------------------------------------------------

    return {
        "enabled": False,
        "mode": "unsupported",
        "endpoint": None,
        "query": None,
        "path": None,
        "description": (
            "Unable to build chart WebSocket path "
            "because instrument type or strike price "
            "is missing."
        ),
    }


# ============================================================
# Contract Information
# ============================================================


def _get_contract_info_by_key(
    instrument_key: str,
) -> dict:
    """
    Resolves contract metadata from options_cache
    using instrument_key.
    """

    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    if instrument_key == main_key:
        return _build_index_instrument_payload(main_key)

    for item in options_cache.get("data", []) or []:

        if item.get("instrument_key") == instrument_key:

            contract_info = dict(item)

            contract_info["display_name"] = _build_option_display_name(item)

            contract_info["chart_websocket"] = _build_chart_websocket_payload(
                contract_info
            )

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


# ============================================================
# Candle Normalization
# ============================================================


def _normalize_candle(
    raw_candle: list,
) -> dict | None:
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

    volume = (
        _safe_float(
            raw_candle[5],
            0.0,
        )
        if len(raw_candle) > 5
        else 0.0
    )

    oi = (
        _safe_float(
            raw_candle[6],
            0.0,
        )
        if len(raw_candle) > 6
        else 0.0
    )

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
# Candle Merge Helper
# ============================================================


def _merge_candles(
    historical_candles: list,
    intraday_candles: list,
) -> list:
    """
    Combines historical and today's intraday candles.

    De-duplicates candles using candle timestamp.

    Intraday candles overwrite historical candles when the
    same timestamp exists because today's intraday data is
    considered the most current source.
    """

    candle_map = {}

    # --------------------------------------------------------
    # Historical candles first
    # --------------------------------------------------------

    for candle in historical_candles:

        if not candle:
            continue

        candle_time = candle.get("time")

        if candle_time is None:
            continue

        candle_map[str(candle_time)] = candle

    # --------------------------------------------------------
    # Today's candles second
    #
    # If the same timestamp exists, today's intraday
    # candle replaces the historical candle.
    # --------------------------------------------------------

    for candle in intraday_candles:

        if not candle:
            continue

        candle_time = candle.get("time")

        if candle_time is None:
            continue

        candle_map[str(candle_time)] = candle

    # --------------------------------------------------------
    # Sort ascending
    # --------------------------------------------------------

    merged = sorted(
        candle_map.values(),
        key=lambda candle: str(candle.get("time") or ""),
    )

    return merged


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
    - chart_websocket metadata for live selected-instrument
      chart updates.
    """

    cache_data = options_cache.get("data", []) or []

    subscribed_keys = (
        options_cache.get(
            "subscribed_keys",
            [],
        )
        or []
    )

    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    instruments = []

    # --------------------------------------------------------
    # Main index
    # --------------------------------------------------------

    if main_key in subscribed_keys or not subscribed_keys:

        instruments.append(_build_index_instrument_payload(main_key))

    # --------------------------------------------------------
    # Options
    # --------------------------------------------------------

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
            "display_name": (_build_option_display_name(item)),
            "segment": item.get("segment"),
            "underlying_symbol": item.get("underlying_symbol"),
            "underlying_type": item.get("underlying_type"),
        }

        instrument_payload["chart_websocket"] = _build_chart_websocket_payload(
            instrument_payload
        )

        instruments.append(instrument_payload)

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

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
        "message": ("Chart instruments loaded successfully."),
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get("total_contracts"),
        "subscribed_keys_count": len(subscribed_keys),
        "count": len(instruments),
        "websocket_usage": {
            "option_live_chart": ("/option?strike=<strike>" "&striketype=<ce|pe>"),
            "index_live_chart": "/all-feeds",
            "note": (
                "Frontend should connect to "
                "chart_websocket.path from the "
                "selected instrument and update "
                "the latest candle using "
                "live_tick payloads."
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
        description=(
            "Instrument key, for example " "NSE_FO|41012 or NSE_INDEX|Nifty 50."
        ),
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
        description=(
            "historical or intraday. "
            "historical combines historical candles "
            "with today's intraday candles."
        ),
    ),
    days: int = Query(
        default=7,
        ge=1,
        le=30,
        description=(
            "Number of calendar days to load when " "from_date is not provided."
        ),
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

    IMPORTANT:
    Historical API and intraday API are combined.

    Historical API:
        Used only for candles up to yesterday.

    Intraday API:
        Used for today's candles.

    Example:

        Request:
            /chart/candles?
                instrument_key=NSE_FO|41012
                &interval=1minute
                &mode=historical
                &days=7

        Result:
            Previous historical candles
            +
            Today's intraday candles
            =
            One combined sorted candle list.

    Intraday behavior:
        mode=intraday returns today's intraday candles only.

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
    - Initial candles come from this route.
    - Realtime candle updates come from selected instrument WebSocket.
    - WebSocket metadata is returned under live_chart.
    """

    # --------------------------------------------------------
    # Validate instrument
    # --------------------------------------------------------

    if not instrument_key:
        raise HTTPException(
            status_code=400,
            detail="instrument_key is required.",
        )

    # --------------------------------------------------------
    # Validate mode
    # --------------------------------------------------------

    mode_text = str(mode or "historical").lower().strip()

    if mode_text not in [
        "historical",
        "intraday",
    ]:
        raise HTTPException(
            status_code=400,
            detail=("mode must be either " "historical or intraday."),
        )

    # --------------------------------------------------------
    # Resolve interval
    # --------------------------------------------------------

    unit, intra_interval = get_intraday_unit_and_interval(interval)

    if not unit or not intra_interval:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported interval: {interval}. "
                "Allowed examples: "
                "1minute, 3minute, 5minute, "
                "15minute, 30minute."
            ),
        )

    # --------------------------------------------------------
    # Market date
    # --------------------------------------------------------

    market_today = _get_market_today()

    # --------------------------------------------------------
    # Parse requested dates
    # --------------------------------------------------------

    parsed_to_date = _parse_date_string(
        to_date,
        "to_date",
    )

    parsed_from_date = _parse_date_string(
        from_date,
        "from_date",
    )

    # Default end date = today
    if parsed_to_date is None:
        parsed_to_date = market_today

    # Default start date
    if parsed_from_date is None:
        parsed_from_date = parsed_to_date - timedelta(days=days - 1)

    # --------------------------------------------------------
    # Validate date range
    # --------------------------------------------------------

    if parsed_from_date > parsed_to_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "from_date must be less than or equal "
                "to to_date. "
                f"Received "
                f"from_date={parsed_from_date}, "
                f"to_date={parsed_to_date}."
            ),
        )

    resolved_from_date = parsed_from_date.isoformat()

    resolved_to_date = parsed_to_date.isoformat()

    # --------------------------------------------------------
    # Historical API end date
    #
    # Upstox historical candle API should NOT be relied on
    # for today's candles.
    #
    # Therefore, when the requested range includes today,
    # historical API stops at yesterday.
    # --------------------------------------------------------

    yesterday = market_today - timedelta(days=1)

    historical_from_date = parsed_from_date

    historical_to_date = min(
        parsed_to_date,
        yesterday,
    )

    historical_requested = historical_from_date <= historical_to_date

    # --------------------------------------------------------
    # Today is requested?
    # --------------------------------------------------------

    today_requested = parsed_from_date <= market_today <= parsed_to_date

    contract_info = _get_contract_info_by_key(instrument_key)

    try:
        logger.info(
            "Chart candle request started. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"interval={interval}, "
            f"unit={unit}, "
            f"intra_interval={intra_interval}, "
            f"requested_from={resolved_from_date}, "
            f"requested_to={resolved_to_date}, "
            f"historical_from="
            f"{historical_from_date.isoformat()}, "
            f"historical_to="
            f"{historical_to_date.isoformat()}, "
            f"today_requested={today_requested}"
        )

        api_instance = upstox_client.HistoryV3Api()

        # ====================================================
        # 1. INTRADAY MODE
        #
        # Only today's intraday candles.
        # ====================================================

        if mode_text == "intraday":

            logger.info(
                "Fetching today's intraday candles. "
                f"instrument_key={instrument_key}, "
                f"unit={unit}, "
                f"interval={intra_interval}"
            )

            intraday_response = api_instance.get_intra_day_candle_data(
                instrument_key,
                unit,
                intra_interval,
            )

            raw_intraday_candles = extract_candles_from_response(intraday_response)

            intraday_candles = []

            for raw_candle in raw_intraday_candles:

                normalized = _normalize_candle(raw_candle)

                if normalized:
                    intraday_candles.append(normalized)

            candles = _merge_candles(
                historical_candles=[],
                intraday_candles=intraday_candles,
            )

            logger.info(
                "Intraday candle fetch completed. "
                f"instrument_key={instrument_key}, "
                f"intraday_count="
                f"{len(intraday_candles)}, "
                f"final_count={len(candles)}"
            )

        # ====================================================
        # 2. HISTORICAL MODE
        #
        # Historical candles + today's intraday candles.
        # ====================================================

        else:

            historical_candles = []
            intraday_candles = []

            # ------------------------------------------------
            # Historical candles
            #
            # Only fetch if requested date range contains
            # at least one day before today.
            # ------------------------------------------------

            if historical_requested:

                historical_from = historical_from_date.isoformat()

                historical_to = historical_to_date.isoformat()

                logger.info(
                    "Fetching historical candles. "
                    f"instrument_key={instrument_key}, "
                    f"from_date={historical_from}, "
                    f"to_date={historical_to}, "
                    f"unit={unit}, "
                    f"interval={intra_interval}"
                )

                historical_response = _get_historical_candle_response(
                    api_instance=api_instance,
                    instrument_key=instrument_key,
                    unit=unit,
                    interval=intra_interval,
                    from_date=historical_from,
                    to_date=historical_to,
                )

                raw_historical_candles = extract_candles_from_response(
                    historical_response
                )

                for raw_candle in raw_historical_candles:

                    normalized = _normalize_candle(raw_candle)

                    if normalized:
                        historical_candles.append(normalized)

                logger.info(
                    "Historical candle fetch completed. "
                    f"instrument_key={instrument_key}, "
                    f"historical_count="
                    f"{len(historical_candles)}"
                )

            else:

                logger.info(
                    "Historical candle fetch skipped. "
                    f"instrument_key={instrument_key}, "
                    f"requested_from={resolved_from_date}, "
                    f"requested_to={resolved_to_date}"
                )

            # ------------------------------------------------
            # Today's intraday candles
            #
            # Fetch only when today belongs to the requested
            # date range.
            # ------------------------------------------------

            if today_requested:

                logger.info(
                    "Fetching today's intraday candles "
                    "for combined chart response. "
                    f"instrument_key={instrument_key}, "
                    f"unit={unit}, "
                    f"interval={intra_interval}"
                )

                intraday_response = api_instance.get_intra_day_candle_data(
                    instrument_key,
                    unit,
                    intra_interval,
                )

                raw_intraday_candles = extract_candles_from_response(intraday_response)

                for raw_candle in raw_intraday_candles:

                    normalized = _normalize_candle(raw_candle)

                    if normalized:
                        intraday_candles.append(normalized)

                logger.info(
                    "Today's intraday candle fetch "
                    "completed. "
                    f"instrument_key={instrument_key}, "
                    f"intraday_count="
                    f"{len(intraday_candles)}"
                )

            else:

                logger.info(
                    "Today's intraday fetch skipped because "
                    "today is outside requested date range. "
                    f"instrument_key={instrument_key}, "
                    f"requested_from={resolved_from_date}, "
                    f"requested_to={resolved_to_date}"
                )

            # ------------------------------------------------
            # Merge
            # ------------------------------------------------

            candles = _merge_candles(
                historical_candles=historical_candles,
                intraday_candles=intraday_candles,
            )

            logger.info(
                "Historical + intraday candle merge "
                "completed. "
                f"instrument_key={instrument_key}, "
                f"historical_count="
                f"{len(historical_candles)}, "
                f"intraday_count="
                f"{len(intraday_candles)}, "
                f"final_count={len(candles)}"
            )

        # ====================================================
        # Final response
        # ====================================================

        logger.info(
            "Chart candle request completed. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"candles_count={len(candles)}"
        )

        return {
            "status": "success",
            "message": ("Candles loaded successfully."),
            "instrument_key": instrument_key,
            "contract_info": contract_info,
            "mode": mode_text,
            "from_date": resolved_from_date,
            "to_date": resolved_to_date,
            "days": days,
            "interval": interval,
            "unit": unit,
            "intraday_interval": intra_interval,
            # Counts
            "candles_count": len(candles),
            # Source information
            "candle_sources": {
                "historical": (mode_text == "historical" and historical_requested),
                "intraday_today": (
                    today_requested
                    if mode_text == "historical"
                    else mode_text == "intraday"
                ),
                "historical_from_date": (
                    historical_from_date.isoformat() if historical_requested else None
                ),
                "historical_to_date": (
                    historical_to_date.isoformat() if historical_requested else None
                ),
                "intraday_date": (
                    market_today.isoformat() if today_requested else None
                ),
            },
            # Combined candles
            "candles": candles,
            # Live WebSocket configuration
            "live_chart": {
                "enabled": bool(
                    contract_info.get(
                        "chart_websocket",
                        {},
                    ).get("enabled")
                ),
                "websocket": contract_info.get("chart_websocket"),
                "update_source": ("selected_instrument_websocket"),
                "frontend_logic": (
                    "Use candles from this response "
                    "for chart load. Then connect to "
                    "websocket.path and update the latest "
                    "candle using incoming live_tick "
                    "LTP/OHLC values."
                ),
            },
            "generated_at": (datetime.now().isoformat()),
        }

    # ========================================================
    # Upstox API error
    # ========================================================

    except ApiException as ex:

        error_body = getattr(
            ex,
            "body",
            str(ex),
        )

        logger.error(
            "Chart candle Upstox API error. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"error={error_body}"
        )

        raise HTTPException(
            status_code=500,
            detail=(f"Upstox candle API error: " f"{error_body}"),
        )

    # ========================================================
    # General error
    # ========================================================

    except Exception as ex:

        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Chart candle request failed. "
            f"instrument_key={instrument_key}, "
            f"mode={mode_text}, "
            f"from_date={resolved_from_date}, "
            f"to_date={resolved_to_date}, "
            f"error={error_message}"
        )

        raise HTTPException(
            status_code=500,
            detail=(f"Chart candle fetch failed: " f"{error_message}"),
        )
