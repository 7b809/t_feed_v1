from datetime import date, timedelta
import logging

import upstox_client

from services.token_service import token_service

logger = logging.getLogger(__name__)


def _create_history_client():
    access_token = token_service.get_access_token()

    if not access_token:
        raise ValueError("Upstox access token not available")

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)

    return upstox_client.HistoryV3Api(api_client)


def fetch_historical_candles(
    instrument_key: str,
    interval: str = "minutes",
    unit: str = "1",
    days: int = 7,
):
    api = _create_history_client()

    to_date = date.today() - timedelta(days=1)
    from_date = to_date - timedelta(days=days)

    response = api.get_historical_candle_data1(
        instrument_key,
        interval,
        unit,
        str(to_date),
        str(from_date),
    )

    data = getattr(response, "data", None)

    if not data:
        return []

    return getattr(data, "candles", []) or []


def fetch_intraday_candles(
    instrument_key: str,
    interval: str = "minutes",
    unit: str = "1",
):
    api = _create_history_client()

    response = api.get_intra_day_candle_data(
        instrument_key,
        interval,
        unit,
    )

    data = getattr(response, "data", None)

    if not data:
        return []

    return getattr(data, "candles", []) or []


def merge_candles(
    historical_candles,
    intraday_candles,
):
    merged = []
    seen = set()

    for candle in historical_candles + intraday_candles:
        timestamp = candle[0]

        if timestamp in seen:
            continue

        seen.add(timestamp)
        merged.append(candle)

    merged.sort(key=lambda x: x[0])

    return merged


def convert_to_chart_data(candles):
    chart_data = []

    for candle in candles:
        try:
            chart_data.append(
                {
                    "time": candle[0],
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]) if len(candle) > 5 else 0,
                }
            )
        except Exception as exc:
            logger.error("Failed to parse candle: %s", exc)

    return chart_data


def get_chart_data(
    instrument_key: str,
    interval: str = "minutes",
    unit: str = "1",
):
    historical_candles = []
    intraday_candles = []

    try:
        historical_candles = fetch_historical_candles(
            instrument_key=instrument_key,
            interval=interval,
            unit=unit,
        )
    except Exception as exc:
        logger.exception(
            "Historical candle fetch failed for %s: %s",
            instrument_key,
            exc,
        )

    try:
        intraday_candles = fetch_intraday_candles(
            instrument_key=instrument_key,
            interval=interval,
            unit=unit,
        )
    except Exception as exc:
        logger.exception(
            "Intraday candle fetch failed for %s: %s",
            instrument_key,
            exc,
        )

    merged_candles = merge_candles(
        historical_candles,
        intraday_candles,
    )

    chart_data = convert_to_chart_data(merged_candles)

    return {
        "instrument_key": instrument_key,
        "total_candles": len(chart_data),
        "candles": chart_data,
    }
