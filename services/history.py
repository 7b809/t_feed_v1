from datetime import date, timedelta

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.ema import normalize_candle
from utils.common import api_error, call_with_retry, object_to_dict

logger = get_logger(__file__)


def extract_candles(response) -> list:
    data = object_to_dict(response).get("data", {})
    return data.get("candles", []) if isinstance(data, dict) else []


def invalid_range(ex: ApiException) -> bool:
    error = api_error(ex)

    return (
        str(error.get("error_code") or "").upper() == "UDAPI1148"
        or "invalid date range" in str(error.get("message") or "").lower()
    )


def fetch_history(
    api,
    key: str,
    today: date,
) -> list:
    to_date = today - timedelta(days=1)

    logger.info(
        "Historical candle fetch started instrument=%s to_date=%s",
        key,
        to_date.isoformat(),
    )

    for days in config.LOOKBACK_ATTEMPTS:
        from_date = to_date - timedelta(days=days - 1)

        try:
            response = call_with_retry(
                "historical",
                lambda: api.get_historical_candle_data1(
                    key,
                    "minutes",
                    "1",
                    to_date.isoformat(),
                    from_date.isoformat(),
                ),
            )

            normalized = [
                candle
                for raw_candle in extract_candles(response)
                if (
                    candle := normalize_candle(
                        raw_candle,
                        "historical",
                    )
                )
            ]

            trading_dates = sorted({candle["date"] for candle in normalized})[
                -config.HISTORICAL_TRADING_DAYS :
            ]

            selected_dates = set(trading_dates)

            candles = [
                candle for candle in normalized if candle["date"] in selected_dates
            ]

            logger.info(
                "Historical candle fetch completed "
                "instrument=%s candle_count=%s "
                "from_date=%s to_date=%s trading_days=%s",
                key,
                len(candles),
                from_date.isoformat(),
                to_date.isoformat(),
                len(selected_dates),
            )

            return candles

        except ApiException as ex:
            if invalid_range(ex):
                logger.warning(
                    "Historical date range rejected "
                    "instrument=%s from_date=%s to_date=%s "
                    "lookback_days=%s trying_shorter_range=true",
                    key,
                    from_date.isoformat(),
                    to_date.isoformat(),
                    days,
                )
                continue

            logger.exception(
                "Historical candle fetch failed "
                "instrument=%s from_date=%s to_date=%s",
                key,
                from_date.isoformat(),
                to_date.isoformat(),
            )
            raise

    logger.warning(
        "Historical candle fetch completed with no candles "
        "instrument=%s candle_count=0 to_date=%s",
        key,
        to_date.isoformat(),
    )

    return []


def fetch_intraday(
    api,
    key: str,
    today: date,
) -> list:
    logger.info(
        "Intraday candle fetch started instrument=%s trading_date=%s",
        key,
        today.isoformat(),
    )

    try:
        response = call_with_retry(
            "intraday",
            lambda: api.get_intra_day_candle_data(
                key,
                "minutes",
                "1",
            ),
        )

        candles = [
            candle
            for raw_candle in extract_candles(response)
            if (
                candle := normalize_candle(
                    raw_candle,
                    "intraday",
                )
            )
            and candle["date"] == today.isoformat()
        ]

        logger.info(
            "Intraday candle fetch completed "
            "instrument=%s candle_count=%s trading_date=%s",
            key,
            len(candles),
            today.isoformat(),
        )

        return candles

    except Exception:
        logger.exception(
            "Intraday candle fetch failed " "instrument=%s trading_date=%s",
            key,
            today.isoformat(),
        )
        raise


def build_history_client(token: str):
    configuration = upstox_client.Configuration()
    configuration.access_token = token

    client = upstox_client.ApiClient(configuration)
    api = upstox_client.HistoryV3Api(client)

    return client, api
