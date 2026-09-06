# services/instrument_selection_service.py

from __future__ import annotations

from typing import Any, Iterable


def _to_float(value: Any) -> float | None:
    """Safely convert a value to float."""

    if value is None or isinstance(value, bool):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_instrument(
    instrument: Any,
) -> dict[str, Any] | None:
    """
    Normalize an instrument into a dictionary.

    The producer sends rich option-chain instrument dictionaries
    containing fields such as market_data, option_greeks, PCR,
    underlying information, etc.

    We intentionally preserve the complete dictionary so the selected
    instrument returned by this service contains all original fields.
    """

    if isinstance(instrument, dict):
        return instrument

    if hasattr(instrument, "model_dump"):
        try:
            return instrument.model_dump(mode="json")
        except Exception:
            return None

    return None


def _get_live_ltp(
    instrument: dict[str, Any],
) -> float | None:
    """
    Get the current live LTP from an instrument.

    Priority:

    1. live_ltp
    2. ltp
    3. market_data.ltp
    """

    live_ltp = _to_float(instrument.get("live_ltp"))

    if live_ltp is not None:
        return live_ltp

    ltp = _to_float(instrument.get("ltp"))

    if ltp is not None:
        return ltp

    market_data = instrument.get("market_data")

    if isinstance(market_data, dict):
        return _to_float(market_data.get("ltp"))

    return None


def _get_instrument_type(
    instrument: dict[str, Any],
) -> str | None:
    """
    Get the instrument type from the instrument dictionary.

    instrument_type is preferred, with option_type as fallback.
    """

    instrument_type = instrument.get("instrument_type")

    if instrument_type is None:
        instrument_type = instrument.get("option_type")

    if instrument_type is None:
        return None

    return str(instrument_type).upper().strip()


def _is_available(
    instrument: dict[str, Any],
) -> bool:
    """
    Determine whether an instrument is available.

    Missing 'available' is treated as available because the
    producer's option-chain objects do not currently include
    an explicit available field.
    """

    available = instrument.get(
        "available",
        True,
    )

    return available is not False


def get_available_instruments(
    instruments: Iterable[Any],
    instrument_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return normalized available instruments.

    Filtering performed here:

    1. Keep dictionary/model instruments only.
    2. Exclude available=False instruments.
    3. If instrument_type is supplied, keep only matching types.
    4. Require a valid live price through live_ltp, ltp,
       or market_data.ltp.

    The original complete instrument dictionary is preserved.
    """

    normalized_type = str(instrument_type).upper().strip() if instrument_type else None

    result: list[dict[str, Any]] = []

    for raw_instrument in instruments:
        instrument = _normalize_instrument(raw_instrument)

        if instrument is None:
            continue

        if not _is_available(instrument):
            continue

        if normalized_type:
            current_type = _get_instrument_type(instrument)

            if current_type != normalized_type:
                continue

        live_ltp = _get_live_ltp(instrument)

        if live_ltp is None:
            continue

        result.append(instrument)

    return result


def select_nearest_instrument(
    instruments: Iterable[Any],
    target_price: float | int,
    instrument_type: str | None = None,
) -> dict[str, Any] | None:
    """
    Select the complete instrument whose live LTP is closest
    to target_price.

    Example:

        target_price = 132

        available prices:
            120
            135
            154

        selected:
            135

    Selection rules:

        1. Only available instruments are considered.
        2. A valid price must exist.
        3. live_ltp is preferred.
        4. ltp is used as fallback.
        5. market_data.ltp is used as final fallback.
        6. instrument_type is filtered when supplied.
        7. Absolute price difference is used.
        8. First instrument wins equal-price ties.
        9. The complete original instrument dictionary is returned.
    """

    target = _to_float(target_price)

    if target is None:
        return None

    candidates: list[tuple[float, int, dict[str, Any]]] = []

    filtered_instruments = get_available_instruments(
        instruments=instruments,
        instrument_type=instrument_type,
    )

    for index, instrument in enumerate(filtered_instruments):
        live_ltp = _get_live_ltp(instrument)

        if live_ltp is None:
            continue

        price_difference = abs(live_ltp - target)

        candidates.append(
            (
                price_difference,
                index,
                instrument,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )

    return candidates[0][2]


def select_nearest_instrument_with_details(
    instruments: Iterable[Any],
    target_price: float | int,
    instrument_type: str | None = None,
) -> dict[str, Any]:
    """
    Select the nearest instrument and return selection metadata.

    The returned selected_instrument is the complete original
    instrument object supplied by the producer.
    """

    normalized_instruments = [
        instrument
        for instrument in (_normalize_instrument(item) for item in instruments)
        if instrument is not None
    ]

    target = _to_float(target_price)

    available_instruments = get_available_instruments(
        instruments=normalized_instruments,
        instrument_type=instrument_type,
    )

    selected = select_nearest_instrument(
        instruments=available_instruments,
        target_price=target_price,
        instrument_type=instrument_type,
    )

    if selected is None:
        return {
            "performed": bool(normalized_instruments),
            "target_price": target,
            "instrument_type": instrument_type,
            "available_instrument_count": len(available_instruments),
            "selected_instrument": None,
            "selected_live_ltp": None,
            "price_difference": None,
        }

    selected_ltp = _get_live_ltp(selected)

    price_difference = (
        abs(selected_ltp - target)
        if selected_ltp is not None and target is not None
        else None
    )

    return {
        "performed": True,
        "target_price": target,
        "instrument_type": instrument_type,
        "available_instrument_count": len(available_instruments),
        "selected_instrument": selected,
        "selected_live_ltp": selected_ltp,
        "price_difference": price_difference,
    }
