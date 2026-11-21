from datetime import date

from services.option_chain_capture_service import select_nearest_expiry_contracts


def contract(key, expiry, strike, side):
    return {
        "instrument_key": key,
        "expiry": expiry,
        "strike_price": strike,
        "instrument_type": side,
    }


def test_selects_nearest_expiry_both_sides_within_inclusive_range():
    today = date(2026, 11, 18)
    contracts = [
        contract("far-ce", "2026-11-26", 25300, "CE"),
        contract("near-ce", "2026-11-19", 25300, "CE"),
        contract("near-pe", "2026-11-19", 25000, "PE"),
        contract("near-outside", "2026-11-19", 25601, "CE"),
        contract("expired", "2026-11-17", 25300, "CE"),
        contract("wrong-side", "2026-11-19", 25300, "XX"),
    ]

    selected, expiry, lower, upper = select_nearest_expiry_contracts(
        contracts, today, average=25300, range_points=300
    )

    assert expiry == "2026-11-19"
    assert lower == 25000
    assert upper == 25600
    assert {row["instrument_key"] for row in selected} == {"near-ce", "near-pe"}


def test_no_contracts_returns_empty_selection():
    selected, expiry, lower, upper = select_nearest_expiry_contracts(
        [], date(2026, 11, 18), average=62300, range_points=500
    )

    assert selected == []
    assert expiry is None
    assert lower == 61800
    assert upper == 62800


def test_expiry_today_remains_valid_for_expiry_day_session():
    selected, expiry, _, _ = select_nearest_expiry_contracts(
        [contract("expiry-day", "2026-11-18", 14200, "PE")],
        date(2026, 11, 18),
        average=14200,
        range_points=200,
    )

    assert expiry == "2026-11-18"
    assert selected[0]["instrument_key"] == "expiry-day"


def test_zero_range_selects_all_strikes_for_nearest_expiry():
    today = date(2026, 11, 18)
    contracts = [
        contract("near-low", "2026-11-19", 24000, "PE"),
        contract("near-atm", "2026-11-19", 25300, "CE"),
        contract("near-high", "2026-11-19", 27000, "CE"),
        contract("far-expiry", "2026-11-26", 25300, "PE"),
    ]

    selected, expiry, lower, upper = select_nearest_expiry_contracts(
        contracts, today, average=25300, range_points=0
    )

    assert expiry == "2026-11-19"
    assert lower is None and upper is None
    assert {row["instrument_key"] for row in selected} == {
        "near-low", "near-atm", "near-high"
    }


def test_all_expiry_mode_selects_all_non_expired_expiries():
    today = date(2026, 11, 18)
    contracts = [
        contract("near", "2026-11-19", 25300, "CE"),
        contract("far", "2026-11-26", 25300, "PE"),
        contract("expired", "2026-11-17", 25300, "CE"),
    ]
    selected, expiry, _, _ = select_nearest_expiry_contracts(
        contracts, today, average=25300, range_points=0, expiry_mode="all"
    )
    assert expiry == "2026-11-19"
    assert {row["instrument_key"] for row in selected} == {"near", "far"}
