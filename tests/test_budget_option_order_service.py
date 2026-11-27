from sandbox_services import budget_option_order_service as service
from services.daily_profit_guard_service import DailyProfitGuardError


def _contract(key, strike, ltp, side="CE"):
    return {
        "instrument_key": key,
        "instrument_type": side,
        "trading_symbol": f"TEST{strike}{side}",
        "expiry": "2026-12-01T00:00:00",
        "strike_price": strike,
        "moneyness": "ATM",
        "strike_offset": 0,
        "market_data": {"ltp": ltp, "volume": 1000, "oi": 2000},
        "lot_size": 65,
    }


def _chain():
    return {
        "status": "SUCCESS",
        "underlying_key": "NSE_INDEX|Nifty 50",
        "underlying_symbol": "NIFTY",
        "underlying_ltp": 25000,
        "expiry_keyword": "next_week",
        "expiry_used": "2026-12-01T00:00:00",
        "ce_contracts": [
            _contract("NSE_FO|1", 24900, 99),
            _contract("NSE_FO|2", 24950, 100),
            _contract("NSE_FO|3", 25000, 125),
            _contract("NSE_FO|4", 25050, 150),
            _contract("NSE_FO|5", 25100, 151),
        ],
        "pe_contracts": [
            _contract("NSE_FO|6", 24900, 110, "PE"),
        ],
    }


def test_preview_defaults_to_nifty_and_filters_inclusive_premium(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: captured.update(kwargs) or _chain(),
    )
    monkeypatch.setattr(
        service,
        "place_order_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not place in preview")),
    )

    result = service.place_option_orders_by_budget(
        budget_min=100,
        budget_max=150,
        option_type="CE",
    )

    assert result["status"] == "PREVIEW"
    assert result["underlying_key"] == "NSE_INDEX|Nifty 50"
    assert [row["instrument_key"] for row in result["matching_contracts"]] == [
        "NSE_FO|3", "NSE_FO|2", "NSE_FO|4"
    ]
    assert result["selected_contracts_count"] == 1
    assert result["selected_contracts"][0]["instrument_key"] == "NSE_FO|3"
    assert captured["include_market_data"] is True
    assert captured["include_metadata"] is True


def test_underlying_symbol_alias_is_normalized():
    assert service.normalize_underlying_key("SENSEX") == "BSE_INDEX|SENSEX"
    assert service.normalize_underlying_key("NIFTY") == "NSE_INDEX|Nifty 50"
    assert service.normalize_underlying_key("NSE_INDEX%7CNifty%2050") == "NSE_INDEX|Nifty 50"


def test_execute_calls_existing_place_order_service_with_limit_premium(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: _chain(),
    )
    monkeypatch.setattr(service, "assert_new_entries_allowed", lambda: {"new_entries_allowed": True})
    calls = []

    def fake_place(**kwargs):
        calls.append(kwargs)
        return {"status": "SUCCESS", "response": {"data": {"order_id": "ORDER-1"}}}

    monkeypatch.setattr(service, "place_order_service", fake_place)
    result = service.place_option_orders_by_budget(
        budget_min=100,
        budget_max=150,
        option_type="CE",
        execute=True,
        max_orders=2,
        lot=1,
    )

    assert result["status"] == "SUCCESS"
    assert len(calls) == 2
    assert calls[0]["instrument_token"] == "NSE_FO|3"
    assert calls[0]["entry"] == 125
    assert calls[0]["order_type"] == "LIMIT"
    assert calls[0]["lot_size_override"] == 65
    assert calls[0]["strict_lot_size"] is True


def test_failed_chain_returns_structured_failure(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: {
            "status": "FAILED",
            "step": "CHAIN_FETCH",
            "error": {"error_code": "TOKEN", "message": "Invalid token"},
            "context": {"underlying_key": "NSE_INDEX|Nifty 50"},
        },
    )
    result = service.place_option_orders_by_budget(
        budget_min=100,
        budget_max=150,
        option_type="PE",
    )
    assert result["status"] == "FAILED"
    assert result["step"] == "OPTION_CHAIN"


def test_invalid_budget_is_rejected_without_fetch_or_orders(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not fetch chain")),
    )
    result = service.place_option_orders_by_budget(
        budget_min=150,
        budget_max=100,
        option_type="CE",
    )
    assert result["status"] == "FAILED"
    assert result["step"] == "VALIDATION"


def test_one_order_exception_does_not_block_next_selected_order(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: _chain(),
    )
    monkeypatch.setattr(service, "assert_new_entries_allowed", lambda: {"new_entries_allowed": True})
    calls = []

    def fake_place(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("simulated failure")
        return {"status": "SUCCESS"}

    monkeypatch.setattr(service, "place_order_service", fake_place)
    result = service.place_option_orders_by_budget(
        budget_min=100,
        budget_max=150,
        option_type="CE",
        execute=True,
        max_orders=2,
    )

    assert result["status"] == "PARTIAL_SUCCESS"
    assert result["successful_orders"] == 1
    assert result["failed_orders"] == 1
    assert len(result["placement_results"]) == 2


def test_daily_profit_guard_blocks_before_any_buy_order(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_nearest_expiry_option_contracts",
        lambda **kwargs: _chain(),
    )
    calls = []
    monkeypatch.setattr(service, "place_order_service", lambda **kwargs: calls.append(kwargs))

    def blocked():
        raise DailyProfitGuardError(
            "Daily profit target reached.",
            status_code=403,
            code="DAILY_PROFIT_TARGET_REACHED",
        )

    monkeypatch.setattr(service, "assert_new_entries_allowed", blocked)
    result = service.place_option_orders_by_budget(
        budget_min=100,
        budget_max=150,
        option_type="CE",
        execute=True,
        max_orders=2,
        transaction_type="BUY",
    )

    assert calls == []
    assert result["status"] == "FAILED"
    assert result["step"] == "DAILY_PROFIT_GUARD"
    assert result["error"]["http_status"] == 403
    assert result["failed_orders"] == 2
    assert all(row["status"] == "BLOCKED" for row in result["placement_results"])
