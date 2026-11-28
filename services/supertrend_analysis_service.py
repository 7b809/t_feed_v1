"""Historical Supertrend replay and next-session range analysis.

Read-only: fetches a bounded Upstox candle window, calculates a standard
Wilder-ATR Supertrend, and reports confirmed target-day signals and the
following calendar day's OHLC range.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import HTTPException

from core import config
from core.logger import get_logger
from services.token_service import token_service
from upstox_app.services.expired_instruments_service import expired_instruments_service

logger = get_logger(__file__)
_MARKET_TZ = ZoneInfo(getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"))
_UPSTOX_BASE = "https://api.upstox.com"
_TIMEOUT_SECONDS = 30
_LOOKBACK_CALENDAR_DAYS = 10


def _market_today() -> date:
    return datetime.now(_MARKET_TZ).date()


def _expired_key_expiry(instrument_key: str) -> date | None:
    parts = [part.strip() for part in str(instrument_key).split("|")]
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(parts[2], "%d-%m-%Y").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Expired instrument key suffix must use DD-MM-YYYY format",
        ) from exc


def _normalise_expiry_date(value: Any) -> date | None:
    """Parse expiry values returned by Upstox (ISO or DD-MM-YYYY)."""
    if isinstance(value, dict):
        value = value.get("expiry_date") or value.get("expiry") or value.get("date") or value.get("expiryDate")
    if value is None:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _contract_matches_key(contract: dict[str, Any], parts: list[str]) -> bool:
    """Match a returned expired contract by its exchange token, not symbol."""
    token, exchange = parts[1], parts[0]
    exchange_token = str(contract.get("exchange_token") or contract.get("exchangeToken") or "").strip()
    returned_key = str(contract.get("instrument_key") or contract.get("instrumentKey") or "").strip()
    if returned_key:
        returned_parts = [part.strip() for part in returned_key.split("|")]
        if len(returned_parts) >= 3 and returned_parts[0] == exchange and returned_parts[1] == token:
            return True
    return exchange_token == token


def _resolve_expired_instrument_key(
    instrument_key: str,
    underlying_key: str,
    target_date: date,
    expiry_date: str | None = None,
) -> tuple[str, date]:
    """Resolve a two-part F&O key through Upstox's expired-contract catalogue."""
    parts = [part.strip() for part in str(instrument_key).split("|")]
    if len(parts) != 2 or parts[0] not in {"NSE_FO", "BSE_FO"}:
        raise HTTPException(status_code=400, detail="Expired-key resolution requires a two-part NSE_FO/BSE_FO key")
    underlying = str(underlying_key or "").strip()
    if not underlying or "|" not in underlying:
        raise HTTPException(
            status_code=422,
            detail="Cannot resolve expired F&O key: underlying_key is missing from the isolation event metadata",
        )

    if expiry_date:
        parsed_expiry = _normalise_expiry_date(expiry_date)
        if parsed_expiry is None:
            raise HTTPException(status_code=400, detail="expiry_date must use YYYY-MM-DD or DD-MM-YYYY")
        expiry_candidates = [parsed_expiry]
    else:
        expiry_result = expired_instruments_service.get_expiries(underlying)
        if expiry_result.get("status") != "ok":
            status = expiry_result.get("status")
            detail = expiry_result.get("error") or status or "Unable to retrieve expired contract expiries"
            raise HTTPException(status_code=503 if status == "no_token" else 502,
                                detail=f"Expired contract expiry lookup failed: {detail}")
        expiry_candidates = [
            parsed for item in (expiry_result.get("expiries") or [])
            if (parsed := _normalise_expiry_date(item)) is not None and parsed >= target_date
        ]
        expiry_candidates.sort()

    for expiry in expiry_candidates:
        contracts_result = expired_instruments_service.get_expired_option_contracts(
            underlying, expiry.isoformat()
        )
        if contracts_result.get("status") != "ok":
            status = contracts_result.get("status")
            detail = contracts_result.get("error") or status or "Unable to retrieve expired contracts"
            raise HTTPException(status_code=503 if status == "no_token" else 502,
                                detail=f"Expired contract lookup failed for {expiry.isoformat()}: {detail}")
        for contract in contracts_result.get("contracts") or []:
            if not isinstance(contract, dict) or not _contract_matches_key(contract, parts):
                continue
            returned_key = str(contract.get("instrument_key") or contract.get("instrumentKey") or "").strip()
            contract_expiry = _normalise_expiry_date(contract) or expiry
            if contract_expiry < target_date:
                continue
            if returned_key:
                returned_parts = [part.strip() for part in returned_key.split("|")]
                if len(returned_parts) >= 3:
                    resolved_key = "|".join(returned_parts[:3])
                else:
                    resolved_key = f"{parts[0]}|{parts[1]}|{contract_expiry.strftime('%d-%m-%Y')}"
            else:
                # Appending the date is safe only after exact token verification
                # against the expired-contract catalogue.
                resolved_key = f"{parts[0]}|{parts[1]}|{contract_expiry.strftime('%d-%m-%Y')}"
            if _expired_key_expiry(resolved_key) != contract_expiry:
                continue
            logger.info("Resolved expired instrument key %s to %s using underlying %s",
                        instrument_key, resolved_key, underlying)
            return resolved_key, contract_expiry

    raise HTTPException(status_code=404,
                        detail=f"No expired contract matched exchange token {parts[1]} for underlying {underlying}")


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_MARKET_TZ)
        return parsed.astimezone(_MARKET_TZ)
    except (TypeError, ValueError):
        return None


def _normalise_candles(raw_candles: Any) -> list[dict[str, Any]]:
    """Accept Upstox list rows and dict rows; return ascending unique bars."""
    by_timestamp: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_candles, list):
        return []
    for row in raw_candles:
        if isinstance(row, dict):
            timestamp = row.get("timestamp") or row.get("time")
            values = {name: row.get(name) for name in ("open", "high", "low", "close")}
            volume = row.get("volume")
            oi = row.get("open_interest", row.get("oi"))
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            timestamp = row[0]
            values = dict(zip(("open", "high", "low", "close"), row[1:5]))
            volume = row[5] if len(row) > 5 else None
            oi = row[6] if len(row) > 6 else None
        else:
            continue
        dt = _parse_timestamp(timestamp)
        if dt is None:
            continue
        try:
            ohlc = {name: float(values[name]) for name in ("open", "high", "low", "close")}
        except (TypeError, ValueError, KeyError):
            continue
        if any(value != value or abs(value) == float("inf") for value in ohlc.values()):
            continue
        if ohlc["high"] < max(ohlc["open"], ohlc["close"], ohlc["low"]):
            continue
        if ohlc["low"] > min(ohlc["open"], ohlc["close"], ohlc["high"]):
            continue
        iso = dt.isoformat()
        by_timestamp[iso] = {
            "timestamp": iso, "date": dt.date().isoformat(), **ohlc,
            "volume": volume, "open_interest": oi,
        }
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _fetch_active_candles(instrument_key: str, from_date: date, to_date: date) -> list[Any]:
    token = token_service.get_access_token()
    if not token:
        raise HTTPException(status_code=503, detail="Upstox access token is not available")
    url = (
        f"{_UPSTOX_BASE}/v3/historical-candle/{instrument_key}/minutes/1/"
        f"{to_date.isoformat()}/{from_date.isoformat()}"
    )
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("Active historical candle request failed for %s", instrument_key)
        raise HTTPException(
            status_code=502,
            detail=f"Upstox historical candle request failed: {type(exc).__name__}",
        ) from exc
    if response.status_code >= 400:
        logger.warning(
            "Upstox historical candle request returned HTTP %s for %s: %s",
            response.status_code, instrument_key, response.text[:250],
        )
        if "UDAPI100011" in response.text:
            raise HTTPException(status_code=502, detail="UPSTOX_INVALID_INSTRUMENT_KEY: Upstox rejected the two-part instrument key")
        raise HTTPException(
            status_code=502,
            detail=f"Upstox historical candle API returned HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="Upstox returned a non-JSON candle response"
        ) from exc
    return ((payload.get("data") or {}).get("candles") or [])


def _fetch_expired_candles(instrument_key: str, from_date: date, to_date: date) -> list[Any]:
    result = expired_instruments_service.get_expired_historical_candles(
        instrument_key, "1minute", from_date.isoformat(), to_date.isoformat()
    )
    if result.get("status") != "ok":
        status = result.get("status")
        detail = result.get("error") or status or "Expired candle request failed"
        raise HTTPException(status_code=503 if status == "no_token" else 502, detail=str(detail))
    return result.get("candles") or []


def fetch_historical_candles(
    instrument_key: str,
    from_date: date,
    to_date: date,
    target_date: date | None = None,
    underlying_key: str | None = None,
    expiry_date: str | None = None,
) -> tuple[list[dict[str, Any]], str, date]:
    """Select active History V3 or resolve and fetch an expired F&O contract."""
    parts = [part.strip() for part in instrument_key.split("|")]
    expiry = _expired_key_expiry(instrument_key)
    if len(parts) >= 3:
        if expiry is None:
            raise HTTPException(status_code=400, detail="Invalid expired instrument key")
        actual_to = min(to_date, expiry)
        if from_date > actual_to:
            return [], "expired", actual_to
        raw = _fetch_expired_candles(instrument_key, from_date, actual_to)
        return _normalise_candles(raw), "expired", actual_to

    reference_date = target_date or from_date
    parsed_expiry = _normalise_expiry_date(expiry_date) if expiry_date else None
    if (len(parts) == 2 and parts[0] in {"NSE_FO", "BSE_FO"}
            and parsed_expiry is not None and parsed_expiry <= _market_today()):
        resolved_key, resolved_expiry = _resolve_expired_instrument_key(
            instrument_key, underlying_key or "", reference_date, expiry_date
        )
        actual_to = min(to_date, resolved_expiry)
        if from_date > actual_to:
            return [], "expired", actual_to
        raw = _fetch_expired_candles(resolved_key, from_date, actual_to)
        return _normalise_candles(raw), "expired", actual_to

    try:
        raw = _fetch_active_candles(instrument_key, from_date, to_date)
        return _normalise_candles(raw), "active", to_date
    except HTTPException as exc:
        if (exc.status_code != 502 or "UPSTOX_INVALID_INSTRUMENT_KEY" not in str(exc.detail)
                or len(parts) != 2 or parts[0] not in {"NSE_FO", "BSE_FO"}):
            raise
        resolved_key, resolved_expiry = _resolve_expired_instrument_key(
            instrument_key, underlying_key or "", reference_date, expiry_date
        )
        actual_to = min(to_date, resolved_expiry)
        if from_date > actual_to:
            return [], "expired", actual_to
        raw = _fetch_expired_candles(resolved_key, from_date, actual_to)
        return _normalise_candles(raw), "expired", actual_to


def calculate_supertrend(
    candles: list[dict[str, Any]], atr_period: int = 10, multiplier: float = 3.0
) -> list[dict[str, Any]]:
    """Calculate Supertrend with Wilder's smoothed ATR and trailing bands."""
    if atr_period < 2:
        raise ValueError("atr_period must be >= 2")
    if multiplier <= 0:
        raise ValueError("multiplier must be > 0")
    if len(candles) < atr_period:
        return []

    true_ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        high, low = candle["high"], candle["low"]
        tr = high - low if previous_close is None else max(
            high - low, abs(high - previous_close), abs(low - previous_close)
        )
        true_ranges.append(tr)
        previous_close = candle["close"]

    atrs: list[float | None] = [None] * len(candles)
    seed_index = atr_period - 1
    atr = sum(true_ranges[:atr_period]) / atr_period
    atrs[seed_index] = atr
    for index in range(seed_index + 1, len(candles)):
        atr = ((atr * (atr_period - 1)) + true_ranges[index]) / atr_period
        atrs[index] = atr

    result: list[dict[str, Any]] = []
    final_upper: float | None = None
    final_lower: float | None = None
    direction = 1
    previous_close = None
    for index, candle in enumerate(candles):
        current_atr = atrs[index]
        if current_atr is None:
            continue
        midpoint = (candle["high"] + candle["low"]) / 2.0
        basic_upper = midpoint + multiplier * current_atr
        basic_lower = midpoint - multiplier * current_atr
        if final_upper is None or final_lower is None:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            prior_upper, prior_lower = final_upper, final_lower
            final_upper = (
                basic_upper if basic_upper < prior_upper
                or (previous_close is not None and previous_close > prior_upper)
                else prior_upper
            )
            final_lower = (
                basic_lower if basic_lower > prior_lower
                or (previous_close is not None and previous_close < prior_lower)
                else prior_lower
            )
        old_direction = direction
        if direction == 1 and candle["close"] < final_lower:
            direction = -1
        elif direction == -1 and candle["close"] > final_upper:
            direction = 1
        signal = ("BUY" if direction == 1 else "SELL") if direction != old_direction else None
        result.append({
            **candle,
            "atr": round(current_atr, 8),
            "supertrend": round(final_lower if direction == 1 else final_upper, 8),
            "upper_band": round(final_upper, 8),
            "lower_band": round(final_lower, 8),
            "direction": "BULLISH" if direction == 1 else "BEARISH",
            "signal": signal,
        })
        previous_close = candle["close"]
    return result


def _bar_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {
            "available": False, "candle_count": 0, "open": None, "high": None,
            "low": None, "close": None, "range": None,
            "first_candle_at": None, "last_candle_at": None,
        }
    high = max(bar["high"] for bar in bars)
    low = min(bar["low"] for bar in bars)
    return {
        "available": True, "candle_count": len(bars),
        "open": bars[0]["open"], "high": high, "low": low,
        "close": bars[-1]["close"], "range": round(high - low, 4),
        "first_candle_at": bars[0]["timestamp"],
        "last_candle_at": bars[-1]["timestamp"],
    }


def analyse_instrument(
    instrument_key: str, target_date: date, atr_period: int = 10, multiplier: float = 3.0,
    underlying_key: str | None = None, expiry_date: str | None = None,
) -> dict[str, Any]:
    key = str(instrument_key or "").strip()
    if not key or "|" not in key or len(key) > 180:
        raise HTTPException(
            status_code=400,
            detail="instrument_key must be a full Upstox key, e.g. NSE_INDEX|Nifty 50",
        )
    if atr_period < 2 or atr_period > 100:
        raise HTTPException(status_code=400, detail="atr_period must be between 2 and 100")
    if multiplier <= 0 or multiplier > 20:
        raise HTTPException(status_code=400, detail="multiplier must be > 0 and <= 20")
    if target_date > _market_today():
        raise HTTPException(status_code=400, detail="target_date cannot be in the future")

    from_date = target_date - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
    requested_to = target_date + timedelta(days=1)
    fetch_to = min(requested_to, _market_today())
    expiry = _expired_key_expiry(key) or (_normalise_expiry_date(expiry_date) if expiry_date else None)
    if expiry is not None and target_date > expiry:
        raise HTTPException(
            status_code=400,
            detail=f"target_date is after instrument expiry ({expiry.isoformat()})",
        )

    candles, mode, actual_to = fetch_historical_candles(
        key, from_date, fetch_to, target_date, underlying_key, expiry_date
    )
    indicator_bars = calculate_supertrend(candles, atr_period, multiplier)
    target_iso = target_date.isoformat()
    next_date = requested_to
    target_bars = [bar for bar in indicator_bars if bar["date"] == target_iso]
    next_bars = [bar for bar in candles if bar["date"] == next_date.isoformat()]

    target_signals = [
        {
            "timestamp": bar["timestamp"], "signal": bar["signal"],
            "direction": bar["direction"], "close": bar["close"],
            "supertrend": bar["supertrend"], "atr": bar["atr"],
        }
        for bar in target_bars if bar["signal"]
    ]
    earlier_and_target_signals = [
        bar for bar in indicator_bars if bar["signal"] and bar["date"] <= target_iso
    ]
    latest_signal = earlier_and_target_signals[-1] if earlier_and_target_signals else None
    last_target_bar = target_bars[-1] if target_bars else None

    return {
        "status": "success",
        "instrument_key": key,
        "instrument_mode": mode,
        "target_date": target_iso,
        "lookback": {
            "calendar_days_before_target": _LOOKBACK_CALENDAR_DAYS,
            "from_date": from_date.isoformat(),
            "requested_to_date": requested_to.isoformat(),
            "fetched_to_date": actual_to.isoformat(),
        },
        "supertrend_parameters": {
            "atr_period": atr_period, "multiplier": multiplier,
            "atr_method": "Wilder smoothing", "candle_interval": "1minute",
        },
        "candles_fetched": len(candles),
        "indicator_candles": len(indicator_bars),
        "target_day": {
            **_bar_summary(target_bars),
            "last_confirmed_signal_on_target_date": (
                target_signals[-1] if target_signals else None
            ),
            "latest_confirmed_signal_as_of_target_close": (
                {
                    "timestamp": latest_signal["timestamp"],
                    "signal": latest_signal["signal"],
                    "direction": latest_signal["direction"],
                    "close": latest_signal["close"],
                    "supertrend": latest_signal["supertrend"],
                    "atr": latest_signal["atr"],
                    "occurred_on_target_date": latest_signal["date"] == target_iso,
                } if latest_signal else None
            ),
            "direction_at_close": last_target_bar["direction"] if last_target_bar else None,
            "supertrend_at_close": last_target_bar["supertrend"] if last_target_bar else None,
            "signals": target_signals,
        },
        "next_day": {"date": next_date.isoformat(), **_bar_summary(next_bars)},
        "notes": [
            "Lookback is 10 calendar days before target_date, not 10 trading sessions.",
            "Signals are confirmed only after the corresponding 1-minute candle closes.",
            "Next-day high/low is aggregated from all next-day candles returned by Upstox.",
            "No next-day candles means data is unavailable, not zero.",
        ],
    }


supertrend_analysis_service = analyse_instrument
