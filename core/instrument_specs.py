"""Authoritative static instrument characteristics for strategy underlyings.

Strategy enablement and strike bounds are runtime settings; exchange/provider
identity and strike intervals belong here so they are not duplicated in
strategy services.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexInstrumentSpec:
    underlying: str
    instrument_key: str
    display_name: str
    exchange: str
    strike_step: int
    provider_underlying_symbols: tuple[str, ...]


INDEX_INSTRUMENT_SPECS = {
    "NIFTY": IndexInstrumentSpec(
        underlying="NIFTY",
        instrument_key="NSE_INDEX|Nifty 50",
        display_name="NIFTY 50",
        exchange="NSE",
        strike_step=50,
        provider_underlying_symbols=("NIFTY", "NIFTY50", "NIFTYINDEX"),
    ),
    "BANKNIFTY": IndexInstrumentSpec(
        underlying="BANKNIFTY",
        instrument_key="NSE_INDEX|Nifty Bank",
        display_name="NIFTY BANK",
        exchange="NSE",
        strike_step=100,
        provider_underlying_symbols=("BANKNIFTY", "NIFTYBANK", "NIFTYBANKINDEX"),
    ),
    "SENSEX": IndexInstrumentSpec(
        underlying="SENSEX",
        instrument_key="BSE_INDEX|SENSEX",
        display_name="SENSEX",
        exchange="BSE",
        strike_step=100,
        provider_underlying_symbols=("SENSEX", "BSESENSEX", "SPBSESENSEX"),
    ),
}


def validate_strike_range(underlying: str, strike_from, strike_to, strike_step: int):
    """Validate inclusive absolute strike bounds against an instrument step.

    An entirely unset range means no bounds. A partially configured range,
    non-positive step, reversed range, or boundary not divisible by the step
    is rejected instead of silently rounded.
    """
    name = str(underlying or "").upper()
    if isinstance(strike_step, bool) or int(strike_step) <= 0:
        raise ValueError(f"Invalid {name} strike step: {strike_step}")
    if strike_from is None and strike_to is None:
        return None, None
    if strike_from is None or strike_to is None:
        raise ValueError(f"Invalid {name} strike range: both STRIKE_FROM and STRIKE_TO are required")

    lower = float(strike_from)
    upper = float(strike_to)
    step = int(strike_step)
    for boundary in (lower, upper):
        if not boundary.is_integer() or int(boundary) % step:
            display_value = int(boundary) if boundary.is_integer() else boundary
            raise ValueError(
                f"Invalid {name} strike range: {display_value} is not aligned to strike step {step}"
            )
    if lower > upper:
        raise ValueError(f"Invalid {name} strike range: {lower:g} is greater than {upper:g}")
    return int(lower), int(upper)


def filter_provider_contracts(contracts, strike_from=None, strike_to=None, strike_step=None):
    """Filter existing provider contracts; never synthesize an instrument."""
    if not isinstance(contracts, (list, tuple)):
        return []
    step = int(strike_step) if strike_step is not None else None
    lower = float(strike_from) if strike_from is not None else None
    upper = float(strike_to) if strike_to is not None else None
    result = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        try:
            strike = float(contract.get("strike_price"))
        except (TypeError, ValueError, OverflowError):
            continue
        if lower is not None and strike < lower:
            continue
        if upper is not None and strike > upper:
            continue
        if step and (not strike.is_integer() or int(strike) % step):
            continue
        result.append(contract)
    return result


__all__ = [
    "INDEX_INSTRUMENT_SPECS", "IndexInstrumentSpec", "filter_provider_contracts",
    "validate_strike_range",
]
