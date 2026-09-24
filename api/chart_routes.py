from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import upstox_client
from fastapi import APIRouter, HTTPException, Query, Request
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.ema import normalize_candle
from utils.common import api_error, call_with_retry, object_to_dict

logger = get_logger(__file__)

router = APIRouter(
    prefix="/candles",
    tags=["Candles"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Upstox typically rejects historical minute requests whose range exceeds
# about one calendar week. We use this as the maximum window per API call.
HISTORICAL_WINDOW_DAYS = 7

# Safety cap so users cannot ask for years of minute data and hammer the API.
HISTORICAL_MAX_LOOKBACK_DAYS = 365

DEFAULT_CONTRACTS_FILE = Path("data") / "nearest_nifty_option_contracts.json"


def _contracts_file_path() -> Path:
    """
    Resolve the contracts JSON file path.

    Prefers config.CONTRACTS_FILE (defined in core/config.py) and falls back
    to the conventional data/nearest_nifty_option_contracts.json location.
    """
    value = getattr(config, "CONTRACTS_FILE", None)

    if value:
        return Path(str(value))

    for name in ("NEAREST_CONTRACTS_FILE", "CONTRACTS_JSON"):
        value = getattr(config, name, None)
        if value:
            return Path(str(value))

    return DEFAULT_CONTRACTS_FILE


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


def _close_history_client(client: Any) -> None:
    """
    Best-effort close for the Upstox ApiClient.

    Different SDK versions expose different internals; some have no explicit
    close at all. We try known attributes and never raise, so callers do not
    need to wrap this in try/except.
    """
    if client is None:
        return

    close_method = getattr(client, "close", None)
    if callable(close_method):
        try:
            close_method()
        except Exception:
            logger.debug("client.close() raised", exc_info=True)
        return

    rest_client = getattr(client, "rest_client", None)
    if rest_client is not None:
        pool = getattr(rest_client, "pool_manager", None)
        if pool is not None:
            clear = getattr(pool, "clear", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    logger.debug("pool_manager.clear() raised", exc_info=True)
            return

    # Nothing to close in this SDK version. Silent no-op on purpose.
    logger.debug(
        "No close path available on history client type=%s",
        type(client).__name__,
    )


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------


def extract_candles(response) -> list:
    data = object_to_dict(response).get("data", {})
    return data.get("candles", []) if isinstance(data, dict) else []


def invalid_range(ex: ApiException) -> bool:
    error = api_error(ex)

    return (
        str(error.get("error_code") or "").upper() == "UDAPI1148"
        or "invalid date range" in str(error.get("message") or "").lower()
    )


def _normalize_history_response(
    response: Any,
    from_date: date,
    to_date: date,
    *,
    log_label: str,
    instrument_key: str,
) -> list:
    """
    Convert a raw Upstox history response into normalized candles and
    restrict the result to the requested [from_date, to_date] window.
    """
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

    window_from = from_date.isoformat()
    window_to = to_date.isoformat()

    filtered = [
        candle
        for candle in normalized
        if window_from <= candle.get("date", "") <= window_to
    ]

    logger.info(
        "Historical window fetch completed label=%s instrument=%s "
        "raw_count=%s filtered_count=%s from_date=%s to_date=%s",
        log_label,
        instrument_key,
        len(normalized),
        len(filtered),
        window_from,
        window_to,
    )

    return filtered


def _fetch_history_window(
    api,
    key: str,
    from_date: date,
    to_date: date,
) -> list:
    """
    Fetch a single historical window (must fit within HISTORICAL_WINDOW_DAYS)
    and return normalized candles.

    On an "invalid date range" response, retries with progressively shorter
    windows down to a single day.
    """
    if from_date > to_date:
        return []

    span_days = (to_date - from_date).days + 1

    logger.info(
        "Historical window fetch started instrument=%s from_date=%s "
        "to_date=%s span_days=%s",
        key,
        from_date.isoformat(),
        to_date.isoformat(),
        span_days,
    )

    # Try the requested span, then progressively shrink it if Upstox
    # rejects the range. This mirrors the original LOOKBACK_ATTEMPTS logic
    # but is anchored to the specific window we were asked for.
    candidate_spans = sorted(
        {span_days, 7, 5, 4, 3, 2, 1},
        reverse=True,
    )

    last_error: Exception | None = None

    for attempt_span in candidate_spans:
        if attempt_span > span_days:
            continue

        attempt_from = to_date - timedelta(days=attempt_span - 1)

        try:
            response = call_with_retry(
                "historical",
                lambda: api.get_historical_candle_data1(
                    key,
                    "minutes",
                    "1",
                    to_date.isoformat(),
                    attempt_from.isoformat(),
                ),
            )

            return _normalize_history_response(
                response,
                from_date=attempt_from,
                to_date=to_date,
                log_label=f"span_{attempt_span}",
                instrument_key=key,
            )

        except ApiException as ex:
            last_error = ex

            if invalid_range(ex):
                logger.warning(
                    "Historical window rejected instrument=%s "
                    "from_date=%s to_date=%s attempt_span=%s "
                    "trying_shorter=true",
                    key,
                    attempt_from.isoformat(),
                    to_date.isoformat(),
                    attempt_span,
                )
                continue

            logger.exception(
                "Historical window fetch failed instrument=%s "
                "from_date=%s to_date=%s",
                key,
                attempt_from.isoformat(),
                to_date.isoformat(),
            )
            raise

    if last_error is not None:
        logger.error(
            "Historical window fetch exhausted attempts instrument=%s "
            "from_date=%s to_date=%s error=%s",
            key,
            from_date.isoformat(),
            to_date.isoformat(),
            last_error,
        )

    return []


def _fetch_history_range(
    api,
    key: str,
    from_date: date,
    to_date: date,
    *,
    window_days: int = HISTORICAL_WINDOW_DAYS,
) -> list:
    """
    Fetch historical minute candles between from_date and to_date inclusive.

    Splits the range into windows of `window_days` calendar days, fetches
    each window, then merges and de-duplicates candles by timestamp.

    This is what allows lookback_days values larger than one week.
    """
    if from_date > to_date:
        return []

    window_days = max(1, min(int(window_days), HISTORICAL_WINDOW_DAYS))

    total_days = (to_date - from_date).days + 1

    logger.info(
        "Historical range fetch started instrument=%s from_date=%s "
        "to_date=%s total_days=%s window_days=%s",
        key,
        from_date.isoformat(),
        to_date.isoformat(),
        total_days,
        window_days,
    )

    if total_days <= window_days:
        candles = _fetch_history_window(api, key, from_date, to_date)

        logger.info(
            "Historical range fetch completed instrument=%s candles=%s "
            "windows=1 from_date=%s to_date=%s",
            key,
            len(candles),
            from_date.isoformat(),
            to_date.isoformat(),
        )
        return candles

    merged: dict[str, dict] = {}

    window_start = from_date
    window_index = 0

    while window_start <= to_date:
        window_end = min(
            window_start + timedelta(days=window_days - 1),
            to_date,
        )

        window_index += 1

        logger.info(
            "Historical range fetch window started instrument=%s "
            "window_index=%s from_date=%s to_date=%s",
            key,
            window_index,
            window_start.isoformat(),
            window_end.isoformat(),
        )

        try:
            window_candles = _fetch_history_window(
                api,
                key,
                window_start,
                window_end,
            )

        except ApiException:
            # A failure in one window must not lose the windows we already
            # collected. Log and continue to the next window.
            logger.exception(
                "Historical range fetch window failed instrument=%s "
                "window_index=%s from_date=%s to_date=%s",
                key,
                window_index,
                window_start.isoformat(),
                window_end.isoformat(),
            )
            window_candles = []

        for candle in window_candles:
            timestamp = str(candle.get("timestamp") or "").strip()
            if not timestamp:
                continue
            merged[timestamp] = candle

        logger.info(
            "Historical range fetch window completed instrument=%s "
            "window_index=%s window_candles=%s total_unique=%s",
            key,
            window_index,
            len(window_candles),
            len(merged),
        )

        window_start = window_end + timedelta(days=1)

    candles = sorted(
        merged.values(),
        key=lambda item: str(item.get("timestamp") or ""),
    )

    logger.info(
        "Historical range fetch completed instrument=%s candles=%s "
        "windows=%s from_date=%s to_date=%s",
        key,
        len(candles),
        window_index,
        from_date.isoformat(),
        to_date.isoformat(),
    )

    return candles


def fetch_history(
    api,
    key: str,
    today: date,
    lookback_days: int | None = None,
) -> list:
    """
    Fetch historical minute candles ending yesterday.

    lookback_days behavior:
      - None -> use config.HISTORICAL_TRADING_DAYS (default 7)
      - <= HISTORICAL_WINDOW_DAYS -> single API call
      - > HISTORICAL_WINDOW_DAYS -> multiple windows merged

    Intraday candles for `today` are handled separately by fetch_intraday().
    """
    to_date = today - timedelta(days=1)

    if lookback_days is None:
        requested_days = int(getattr(config, "HISTORICAL_TRADING_DAYS", 7))
    else:
        try:
            requested_days = int(lookback_days)
        except (TypeError, ValueError):
            requested_days = int(getattr(config, "HISTORICAL_TRADING_DAYS", 7))

    requested_days = max(1, min(requested_days, HISTORICAL_MAX_LOOKBACK_DAYS))

    from_date = to_date - timedelta(days=requested_days - 1)

    logger.info(
        "Historical candle fetch started instrument=%s from_date=%s "
        "to_date=%s requested_days=%s window_days=%s",
        key,
        from_date.isoformat(),
        to_date.isoformat(),
        requested_days,
        HISTORICAL_WINDOW_DAYS,
    )

    candles = _fetch_history_range(
        api,
        key,
        from_date,
        to_date,
        window_days=HISTORICAL_WINDOW_DAYS,
    )

    # Trim to the most recent HISTORICAL_TRADING_DAYS trading dates, matching
    # the original behaviour when the caller did not override lookback_days.
    if candles:
        trading_dates = sorted({candle["date"] for candle in candles})[
            -requested_days :
        ]
        selected_dates = set(trading_dates)
        candles = [
            candle for candle in candles if candle["date"] in selected_dates
        ]

    logger.info(
        "Historical candle fetch completed instrument=%s candle_count=%s "
        "from_date=%s to_date=%s",
        key,
        len(candles),
        from_date.isoformat(),
        to_date.isoformat(),
    )

    return candles


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


# ---------------------------------------------------------------------------
# Contracts loading helpers
# ---------------------------------------------------------------------------


def _load_contracts() -> list[dict[str, Any]]:
    """
    Load and flatten the nearest NIFTY option contracts JSON file.

    Handles the shape produced by services/contracts.fetch_and_select:
        {"status": ..., "data": [ {...}, {...} ], ...}
    as well as a few common alternatives.
    """
    path = _contracts_file_path()

    if not path.exists():
        logger.warning("Contracts file not found path=%s", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read contracts file path=%s", path)
        return []

    records: list[dict[str, Any]] = []

    def _extend(items: Any) -> None:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    records.append(item)

    if isinstance(raw, list):
        _extend(raw)

    elif isinstance(raw, dict):
        for key in ("data", "contracts", "instruments", "nearest"):
            if key in raw:
                _extend(raw[key])
                break
        else:
            for value in raw.values():
                if isinstance(value, dict):
                    if "instrument_key" in value or "instrument_token" in value:
                        records.append(value)
                    else:
                        _extend(value.get("contracts") or value.get("data"))
                elif isinstance(value, list):
                    _extend(value)

    logger.info(
        "Contracts loaded path=%s count=%s",
        path,
        len(records),
    )

    return records


def _normalize_strike(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_option_type(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"CE", "CALL"}:
        return "CE"
    if text in {"PE", "PUT"}:
        return "PE"
    return text or None


def _instrument_key_of(record: dict[str, Any]) -> str | None:
    for name in ("instrument_key", "instrument_token", "key", "token"):
        value = record.get(name)
        if value:
            return str(value)
    return None


def _strike_of(record: dict[str, Any]) -> float | None:
    for name in ("strike_price", "strike", "strikePrice"):
        if name in record:
            value = _normalize_strike(record.get(name))
            if value is not None:
                return value
    return None


def _option_type_of(record: dict[str, Any]) -> str | None:
    for name in (
        "option_type",
        "instrument_type",
        "optionType",
        "type",
    ):
        if name in record:
            value = _normalize_option_type(record.get(name))
            if value is not None:
                return value
    return None


def _find_contract(
    records: list[dict[str, Any]],
    instrument_key: str | None,
    strike: float | None,
    option_type: str | None,
) -> dict[str, Any] | None:
    for record in records:
        if instrument_key is not None:
            if _instrument_key_of(record) != instrument_key:
                continue
        if strike is not None:
            record_strike = _strike_of(record)
            if record_strike is None or abs(record_strike - strike) > 0.001:
                continue
        if option_type is not None:
            if _option_type_of(record) != option_type:
                continue
        return record
    return None


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def _resolve_token(request: Request) -> str:
    """
    Resolve an Upstox access token for the incoming request.

    Priority:
      1. token_service attached to the EMA runtime (normal case)
      2. global core.token_service (module level singleton)
      3. config.ACCESS_TOKEN / config.UPSTOX_ACCESS_TOKEN (fallback)
    """
    runtime = getattr(request.app.state, "ema_runtime", None) or getattr(
        request.app.state, "runtime", None
    )

    if runtime is not None:
        runtime_token_service = getattr(runtime, "token_service", None)

        if runtime_token_service is not None:
            get_token = getattr(runtime_token_service, "get_access_token", None)

            if callable(get_token):
                try:
                    token = get_token()
                    if token:
                        return str(token)
                except Exception:
                    logger.exception(
                        "runtime.token_service.get_access_token failed"
                    )

        direct_token = getattr(runtime, "access_token", None) or getattr(
            runtime, "token", None
        )
        if direct_token:
            return str(direct_token)

    try:
        from core.token_service import token_service as global_token_service

        token = global_token_service.get_access_token()
        if token:
            return str(token)

    except Exception:
        logger.exception("Global token_service.get_access_token failed")

    for name in ("ACCESS_TOKEN", "UPSTOX_ACCESS_TOKEN"):
        token = getattr(config, name, None)
        if token:
            return str(token)

    raise HTTPException(
        status_code=503,
        detail="Upstox access token is not available",
    )


# ---------------------------------------------------------------------------
# Existing candle routes
# ---------------------------------------------------------------------------


@router.get("/history")
def get_history_candles(
    request: Request,
    instrument_key: str = Query(..., description="Upstox instrument key"),
    lookback_days: int | None = Query(
        None,
        ge=1,
        le=HISTORICAL_MAX_LOOKBACK_DAYS,
        description=(
            "Number of calendar days to look back. Defaults to "
            "config.HISTORICAL_TRADING_DAYS (7). Values above "
            f"{HISTORICAL_WINDOW_DAYS} are fetched in windows and merged."
        ),
    ),
) -> dict[str, Any]:
    token = _resolve_token(request)

    client, api = build_history_client(token)

    try:
        today = date.today()
        candles = fetch_history(api, instrument_key, today, lookback_days)

        return {
            "instrument_key": instrument_key,
            "lookback_days": (
                lookback_days
                if lookback_days is not None
                else int(getattr(config, "HISTORICAL_TRADING_DAYS", 7))
            ),
            "window_days": HISTORICAL_WINDOW_DAYS,
            "candle_count": len(candles),
            "candles": candles,
        }

    except ApiException as ex:
        error = api_error(ex)
        logger.exception(
            "History candle request failed instrument=%s",
            instrument_key,
        )
        raise HTTPException(
            status_code=getattr(ex, "status", 502) or 502,
            detail=error or str(ex),
        )

    finally:
        _close_history_client(client)


@router.get("/intraday")
def get_intraday_candles(
    request: Request,
    instrument_key: str = Query(..., description="Upstox instrument key"),
) -> dict[str, Any]:
    token = _resolve_token(request)

    client, api = build_history_client(token)

    try:
        today = date.today()
        candles = fetch_intraday(api, instrument_key, today)

        return {
            "instrument_key": instrument_key,
            "trading_date": today.isoformat(),
            "candle_count": len(candles),
            "candles": candles,
        }

    except ApiException as ex:
        error = api_error(ex)
        logger.exception(
            "Intraday candle request failed instrument=%s",
            instrument_key,
        )
        raise HTTPException(
            status_code=getattr(ex, "status", 502) or 502,
            detail=error or str(ex),
        )

    finally:
        _close_history_client(client)


@router.get("/health")
def candles_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "candle_routes",
        "historical_window_days": HISTORICAL_WINDOW_DAYS,
        "historical_max_lookback_days": HISTORICAL_MAX_LOOKBACK_DAYS,
    }


# ---------------------------------------------------------------------------
# Contract discovery routes
# ---------------------------------------------------------------------------


@router.get("/contracts")
def list_contracts() -> dict[str, Any]:
    """
    Return all available instruments from the nearest NIFTY option contracts file.
    """
    path = _contracts_file_path()
    records = _load_contracts()

    return {
        "source": str(path),
        "exists": path.exists(),
        "count": len(records),
        "contracts": records,
    }


@router.get("/contracts/by-key")
def get_contract_by_key(
    instrument_key: str = Query(..., description="Upstox instrument key"),
) -> dict[str, Any]:
    """
    Return the contract object matching the given instrument key.
    """
    records = _load_contracts()

    record = _find_contract(
        records,
        instrument_key=instrument_key,
        strike=None,
        option_type=None,
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No contract found for instrument_key={instrument_key}",
        )

    return {
        "count": 1,
        "contracts": [record],
    }


@router.get("/contracts/by-strike")
def get_contracts_by_strike(
    strike: float = Query(..., description="Strike price, e.g. 23550"),
    option_type: str | None = Query(
        None,
        description="Option type: CE or PE (case-insensitive)",
    ),
) -> dict[str, Any]:
    """
    Return all contracts matching the given strike (and optional CE/PE type).
    """
    records = _load_contracts()

    normalized_type = _normalize_option_type(option_type) if option_type else None

    matches = [
        record
        for record in records
        if (
            (_strike_of(record) is not None)
            and abs(_strike_of(record) - float(strike)) <= 0.001
            and (
                normalized_type is None
                or _option_type_of(record) == normalized_type
            )
        )
    ]

    if not matches:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No contracts found for strike={strike}"
                + (f" option_type={normalized_type}" if normalized_type else "")
            ),
        )

    return {
        "strike": float(strike),
        "option_type": normalized_type,
        "count": len(matches),
        "contracts": matches,
    }


@router.get("/contracts/resolve")
def resolve_contract(
    instrument_key: str | None = Query(
        None,
        description="Upstox instrument key",
    ),
    strike: float | None = Query(
        None,
        description="Strike price, e.g. 23550",
    ),
    option_type: str | None = Query(
        None,
        description="Option type: CE or PE",
    ),
) -> dict[str, Any]:
    """
    Resolve a single contract by instrument key OR by (strike + option_type).

    Priority: instrument_key, then strike + option_type.
    """
    if instrument_key is None and strike is None:
        raise HTTPException(
            status_code=400,
            detail="Provide instrument_key or strike (+ optional option_type)",
        )

    records = _load_contracts()

    normalized_type = _normalize_option_type(option_type) if option_type else None

    record = _find_contract(
        records,
        instrument_key=instrument_key,
        strike=strike,
        option_type=normalized_type,
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No matching contract found",
        )

    return {
        "count": 1,
        "contracts": [record],
    }


# ---------------------------------------------------------------------------
# Combined routes: resolve a contract + fetch candles
# ---------------------------------------------------------------------------


def _fetch_candles_for_contract(
    request: Request,
    mode: str,
    instrument_key: str | None,
    strike: float | None,
    option_type: str | None,
    lookback_days: int | None,
) -> dict[str, Any]:
    if instrument_key is None and strike is None:
        raise HTTPException(
            status_code=400,
            detail="Provide instrument_key or strike (+ optional option_type)",
        )

    records = _load_contracts()

    normalized_type = _normalize_option_type(option_type) if option_type else None

    record = _find_contract(
        records,
        instrument_key=instrument_key,
        strike=strike,
        option_type=normalized_type,
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No matching contract found in contracts file",
        )

    resolved_key = _instrument_key_of(record)
    if not resolved_key:
        raise HTTPException(
            status_code=422,
            detail="Resolved contract does not contain an instrument key",
        )

    token = _resolve_token(request)
    client, api = build_history_client(token)

    try:
        today = date.today()

        if mode == "intraday":
            candles = fetch_intraday(api, resolved_key, today)
            return {
                "mode": "intraday",
                "trading_date": today.isoformat(),
                "instrument_key": resolved_key,
                "contract": record,
                "candle_count": len(candles),
                "candles": candles,
            }

        candles = fetch_history(api, resolved_key, today, lookback_days)

        return {
            "mode": "historical",
            "instrument_key": resolved_key,
            "contract": record,
            "lookback_days": (
                lookback_days
                if lookback_days is not None
                else int(getattr(config, "HISTORICAL_TRADING_DAYS", 7))
            ),
            "window_days": HISTORICAL_WINDOW_DAYS,
            "candle_count": len(candles),
            "candles": candles,
        }

    except ApiException as ex:
        error = api_error(ex)
        logger.exception(
            "Candle fetch failed mode=%s instrument=%s",
            mode,
            resolved_key,
        )
        raise HTTPException(
            status_code=getattr(ex, "status", 502) or 502,
            detail=error or str(ex),
        )

    finally:
        _close_history_client(client)


@router.get("/contracts/candles")
def get_contract_candles(
    request: Request,
    mode: str = Query(
        "historical",
        description="Candle mode: 'historical' or 'intraday'",
    ),
    instrument_key: str | None = Query(
        None,
        description="Upstox instrument key",
    ),
    strike: float | None = Query(
        None,
        description="Strike price, e.g. 23550",
    ),
    option_type: str | None = Query(
        None,
        description="Option type: CE or PE",
    ),
    lookback_days: int | None = Query(
        None,
        ge=1,
        le=HISTORICAL_MAX_LOOKBACK_DAYS,
        description=(
            "Historical lookback override in calendar days. Defaults to "
            f"config.HISTORICAL_TRADING_DAYS (7). Above "
            f"{HISTORICAL_WINDOW_DAYS} is fetched in windows."
        ),
    ),
) -> dict[str, Any]:
    """
    Resolve a contract (by key OR strike+type) and return its candles.
    """
    normalized_mode = (mode or "historical").strip().lower()
    if normalized_mode not in {"historical", "intraday"}:
        raise HTTPException(
            status_code=400,
            detail="mode must be 'historical' or 'intraday'",
        )

    return _fetch_candles_for_contract(
        request=request,
        mode=normalized_mode,
        instrument_key=instrument_key,
        strike=strike,
        option_type=option_type,
        lookback_days=lookback_days,
    )


@router.get("/contracts/intraday")
def get_contract_intraday(
    request: Request,
    instrument_key: str | None = Query(None),
    strike: float | None = Query(None),
    option_type: str | None = Query(None),
) -> dict[str, Any]:
    """
    Convenience route: resolve a contract and return its intraday candles.
    """
    return _fetch_candles_for_contract(
        request=request,
        mode="intraday",
        instrument_key=instrument_key,
        strike=strike,
        option_type=option_type,
        lookback_days=None,
    )


@router.get("/contracts/history")
def get_contract_history(
    request: Request,
    instrument_key: str | None = Query(None),
    strike: float | None = Query(None),
    option_type: str | None = Query(None),
    lookback_days: int | None = Query(
        None,
        ge=1,
        le=HISTORICAL_MAX_LOOKBACK_DAYS,
        description=(
            "Historical lookback override in calendar days. Defaults to "
            f"config.HISTORICAL_TRADING_DAYS (7). Above "
            f"{HISTORICAL_WINDOW_DAYS} is fetched in windows."
        ),
    ),
) -> dict[str, Any]: 
    """
    Convenience route: resolve a contract and return its historical candles.
    """
    return _fetch_candles_for_contract(
        request=request,
        mode="historical",
        instrument_key=instrument_key,
        strike=strike,
        option_type=option_type,
        lookback_days=lookback_days,
    )