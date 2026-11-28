"""Unit tests for Supertrend calculation and historical candle routing."""
import unittest
from datetime import date
from unittest.mock import patch

from fastapi import HTTPException

from services.supertrend_analysis_service import (
    calculate_supertrend,
    fetch_historical_candles,
)


class SupertrendCalculationTests(unittest.TestCase):
    def _candles(self, closes):
        return [{
            "timestamp": f"2026-11-{i + 1:02d}T09:15:00+05:30",
            "date": f"2026-11-{i + 1:02d}",
            "open": float(value), "high": float(value) + 1,
            "low": float(value) - 1, "close": float(value),
            "volume": 1, "open_interest": None,
        } for i, value in enumerate(closes)]

    def test_no_values_before_atr_warmup(self):
        self.assertEqual(
            calculate_supertrend(self._candles([100, 101]), atr_period=3), []
        )

    def test_indicator_values_after_atr_warmup(self):
        result = calculate_supertrend(
            self._candles([100, 101, 102, 103, 104]), atr_period=3, multiplier=2
        )
        self.assertEqual(len(result), 3)
        self.assertIn(result[-1]["direction"], {"BULLISH", "BEARISH"})
        self.assertIsInstance(result[-1]["atr"], float)

    def test_down_move_can_confirm_sell_signal(self):
        result = calculate_supertrend(
            self._candles([100, 101, 102, 103, 104, 105, 106, 80, 79, 78, 77]),
            atr_period=3, multiplier=1,
        )
        self.assertIn("SELL", [bar["signal"] for bar in result if bar["signal"]])


class SupertrendHistoricalRoutingTests(unittest.TestCase):
    FROM_DATE = date(2026, 11, 13)
    TO_DATE = date(2026, 11, 24)
    TARGET_DATE = date(2026, 11, 23)

    @patch("services.supertrend_analysis_service._fetch_active_candles", return_value=[])
    def test_two_part_active_instrument_uses_history_v3(self, active_fetch):
        candles, mode, actual_to = fetch_historical_candles(
            "NSE_FO|47855", self.FROM_DATE, self.TO_DATE, self.TARGET_DATE
        )
        self.assertEqual(candles, [])
        self.assertEqual(mode, "active")
        self.assertEqual(actual_to, self.TO_DATE)
        active_fetch.assert_called_once()

    @patch("services.supertrend_analysis_service._fetch_expired_candles", return_value=[])
    def test_already_qualified_expired_key_uses_expired_endpoint(self, expired_fetch):
        key = "NSE_FO|54452|24-11-2026"
        candles, mode, actual_to = fetch_historical_candles(
            key, self.FROM_DATE, self.TO_DATE, self.TARGET_DATE
        )
        self.assertEqual(candles, [])
        self.assertEqual(mode, "expired")
        self.assertEqual(actual_to, date(2026, 11, 24))
        expired_fetch.assert_called_once_with(key, self.FROM_DATE, self.TO_DATE)

    @patch("services.supertrend_analysis_service._market_today", return_value=date(2026, 11, 28))
    @patch("services.supertrend_analysis_service._fetch_expired_candles", return_value=[])
    @patch("services.supertrend_analysis_service.expired_instruments_service.get_expired_option_contracts")
    @patch("services.supertrend_analysis_service.expired_instruments_service.get_expiries")
    @patch(
        "services.supertrend_analysis_service._fetch_active_candles",
        side_effect=HTTPException(
            status_code=502,
            detail="UPSTOX_INVALID_INSTRUMENT_KEY: Upstox rejected the two-part instrument key",
        ),
    )
    def test_two_part_expired_key_is_resolved_by_exact_exchange_token(
        self, active_fetch, get_expiries, get_contracts, expired_fetch, _today
    ):
        get_expiries.side_effect = [
            {"status": "ok", "expiries": []},
            {"status": "ok", "expiries": []},
            {"status": "ok", "expiries": ["2026-11-27"]},
            {"status": "ok", "expiries": []},
        ]
        get_contracts.return_value = {
            "status": "ok",
            "contracts": [{
                "exchange_token": "64492",
                "instrument_key": "NSE_FO|64492|27-11-2026",
            }],
        }

        candles, mode, actual_to = fetch_historical_candles(
            "NSE_FO|64492", self.FROM_DATE, self.TO_DATE, self.TARGET_DATE,
        )

        self.assertEqual(candles, [])
        self.assertEqual(mode, "expired")
        self.assertEqual(actual_to, self.TO_DATE)
        self.assertEqual(get_expiries.call_count, 3)
        get_contracts.assert_called_once_with("NSE_INDEX|NIFTY MID SELECT", "2026-11-27")
        expired_fetch.assert_called_once_with(
            "NSE_FO|64492|27-11-2026", self.FROM_DATE, self.TO_DATE
        )

    @patch(
        "services.supertrend_analysis_service.expired_instruments_service.get_expiries",
        return_value={"status": "ok", "expiries": []},
    )
    @patch(
        "services.supertrend_analysis_service._fetch_active_candles",
        side_effect=HTTPException(
            status_code=502,
            detail="UPSTOX_INVALID_INSTRUMENT_KEY: Upstox rejected the two-part instrument key",
        ),
    )
    def test_expired_key_without_underlying_metadata_fails_clearly(
        self, _active_fetch, _get_expiries
    ):
        with self.assertRaises(HTTPException) as raised:
            fetch_historical_candles(
                "NSE_FO|64492", self.FROM_DATE, self.TO_DATE, self.TARGET_DATE
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("No expired contract matched", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
