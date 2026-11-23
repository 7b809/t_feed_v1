from services import portfolio_service


def test_portfolio_overview_aggregates_positions_and_holdings(monkeypatch):
    def fake_fetch(url, resource):
        if resource == "positions":
            return [
                {"quantity": 0, "realised": 250.0, "unrealised": 0.0},
                {"quantity": 10, "realised": 50.0, "unrealised": -30.0},
            ]
        return [
            {"quantity": 5, "pnl": 400.0, "day_change": 12.0},
            {"quantity": 2, "pnl": -25.0, "day_change": -3.0},
        ]

    monkeypatch.setattr(portfolio_service, "_fetch_rows", fake_fetch)
    result = portfolio_service.get_portfolio_overview()
    assert result["today_pnl"] == 270.0
    assert result["realised_pnl"] == 300.0
    assert result["unrealised_pnl"] == -30.0
    assert result["holdings_pnl"] == 375.0
    assert result["holdings_day_change"] == 9.0
    assert result["total_pnl"] == 645.0
    assert result["positions_count"] == 2
    assert result["holdings_count"] == 2


def test_portfolio_overview_reports_partial_broker_data(monkeypatch):
    def fake_fetch(url, resource):
        if resource == "positions":
            return [{"realised": 20.0, "unrealised": 5.0}]
        raise portfolio_service.PortfolioServiceError("Holdings endpoint unavailable")

    monkeypatch.setattr(portfolio_service, "_fetch_rows", fake_fetch)
    result = portfolio_service.get_portfolio_overview()
    assert result["status"] == "partial"
    assert result["today_pnl"] == 25.0
    assert result["holdings_pnl"] is None
    assert result["total_pnl"] is None
    assert "holdings" in result["errors"]
