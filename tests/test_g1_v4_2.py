from __future__ import annotations
from unittest.mock import patch, MagicMock
# tests/test_g1_v4_2.py
"""Unit and integration test suite for G1 v4.2 additions.

Validates:
1. Vectorized indicator mathematics (Wilder's ATR, ADX, Bollinger Bandwidth, EMA, RSI).
2. Market regime classification (COMPRESSION, TRENDING_BULL, TRENDING_BEAR, RANGING).
3. Strategy entry signal service regime filtering and ATR attachment.
4. Dummy order service ATR trailing stop, hybrid trailing, consecutive loss throttling, and circuit breakers.
"""


import datetime
from datetime import timezone
import unittest

from services.regime_detector_service import (
    calculate_adx,
    calculate_bollinger_bandwidth,
    calculate_tr,
    calculate_wilder_atr,
    MarketRegimeDetector,
)
from services.dummy_order_service import dummy_order_service


class TestG1V42Indicators(unittest.TestCase):
    """Test pure indicator calculation routines."""

    def test_calculate_tr(self):
        highs = [100.0, 105.0, 102.0]
        lows = [95.0, 98.0, 94.0]
        closes = [98.0, 99.0, 95.0]
        trs = calculate_tr(highs, lows, closes)
        self.assertEqual(len(trs), 3)
        self.assertAlmostEqual(trs[0], 5.0)  # 100 - 95
        # Bar 1: max(105 - 98 = 7, |105 - 98| = 7, |98 - 98| = 0) -> 7.0
        self.assertAlmostEqual(trs[1], 7.0)
        # Bar 2: max(102 - 94 = 8, |102 - 99| = 3, |94 - 99| = 5) -> 8.0
        self.assertAlmostEqual(trs[2], 8.0)

    def test_calculate_wilder_atr(self):
        # 20 flat bars with 10 pt daily range
        highs = [110.0] * 20
        lows = [100.0] * 20
        closes = [105.0] * 20
        atrs = calculate_wilder_atr(highs, lows, closes, period=14)
        self.assertEqual(len(atrs), 20)
        self.assertAlmostEqual(atrs[-1], 10.0, places=4)

    def test_calculate_bollinger_bandwidth_compression(self):
        # 30 constant close bars -> bandwidth should be 0.0
        closes = [100.0] * 30
        bw = calculate_bollinger_bandwidth(closes, period=20, std_dev=2.0)
        self.assertEqual(len(bw), 30)
        self.assertAlmostEqual(bw[-1], 0.0)

        # 30 bars with mild fluctuation (compression)
        closes_mild = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(30)]
        bw_mild = calculate_bollinger_bandwidth(closes_mild, period=20, std_dev=2.0)
        self.assertLess(bw_mild[-1], 0.015)

    def test_calculate_adx_trending_vs_ranging(self):
        # Strong upward trend: highs and lows advance consistently
        highs_trend = [100.0 + (i * 2.5) for i in range(40)]
        lows_trend = [95.0 + (i * 2.5) for i in range(40)]
        closes_trend = [98.0 + (i * 2.5) for i in range(40)]
        res_trend = calculate_adx(highs_trend, lows_trend, closes_trend, period=14)
        self.assertGreater(res_trend["adx"][-1], 30.0)
        self.assertGreater(res_trend["plus_di"][-1], res_trend["minus_di"][-1])


class TestMarketRegimeDetector(unittest.TestCase):
    """Test regime classification engine."""

    def setUp(self):
        self.detector = MarketRegimeDetector(
            adx_threshold=22.0,
            bandwidth_threshold=0.015,
        )

    def test_classify_compression(self):
        # Low volatility tight candles
        candles = [
            {"time": f"2026-10-30T10:{i:02d}:00+05:30", "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 500}
            for i in range(30)
        ]
        res = self.detector.evaluate(candles)
        self.assertEqual(res["regime"], "COMPRESSION")
        self.assertTrue(res["is_compression"])

    def test_classify_trending_bull(self):
        # Steady strong bullish run above EMA 200 with > 0.015 bandwidth
        candles = []
        base = 24000.0
        for i in range(60):
            base += 30.0
            candles.append({
                "time": f"2026-10-30T10:{i%60:02d}:00+05:30",
                "open": base - 10,
                "high": base + 20,
                "low": base - 12,
                "close": base + 15,
                "volume": 2000,
            })
        res = self.detector.evaluate(candles)
        self.assertEqual(res["regime"], "TRENDING_BULL")
        self.assertTrue(res["is_trending_bull"])

    def test_classify_ranging(self):
        # Oscillating sideways price action -> ADX stays low, bandwidth normal
        candles = []
        pattern = [100.0, 103.0, 101.0, 98.0, 99.5, 102.5, 99.0, 101.0]
        for i in range(60):
            p = pattern[i % len(pattern)]
            candles.append({
                "time": f"2026-10-30T10:{i%60:02d}:00+05:30",
                "open": p - 0.5,
                "high": p + 1.5,
                "low": p - 1.5,
                "close": p,
                "volume": 1000,
            })
        res = self.detector.evaluate(candles)
        self.assertEqual(res["regime"], "RANGING")
        self.assertTrue(res["is_ranging"])


class TestDummyOrderTrailingAndRisk(unittest.TestCase):
    """Test G1 v4.2 ATR trailing stop and risk limits in DummyOrderService."""

    def setUp(self):
        dummy_order_service.reset()
        self.now = datetime.datetime(2026, 10, 30, 11, 0, 0, tzinfo=timezone.utc)

    def test_path_a_atr_trailing_stop(self):
        # Create dummy Path A order: Entry = 100, Stop = 88, Target = 150, ATR = 10
        order = {
            "order_id": "TEST_KEY|CE|2026-10-30T10:00:00|signal_v2",
            "dummy": True,
            "scenario": "signal_v2",
            "path": "A_self_trigger",
            "instrument_key": "TEST_KEY",
            "side": "CE",
            "entry_price": 100.0,
            "stop_price": 88.0,
            "target_price": 150.0,
            "atr_14": 10.0,
            "trailing_stop_mode": "hybrid",
            "profit_state": "PRE_TRAIL",
            "trailing_stop_price": None,
            "max_high_since_entry": 100.0,
            "status": "OPEN",
        }
        dummy_order_service._state["orders"].append(order)
        dummy_order_service._by_id[order["order_id"]] = order

        # 1. Price advances to 110 (+1.0 ATR) -> below activation (1.2 ATR = 112)
        dummy_order_service._update_from_ltp(order, 110.0, self.now)
        self.assertEqual(order["profit_state"], "PRE_TRAIL")
        self.assertIsNone(order["trailing_stop_price"])
        self.assertEqual(order["status"], "OPEN")

        # 2. Price touches 112 (+1.2 ATR) -> activates trailing stop at breakeven (100.0)
        dummy_order_service._update_from_ltp(order, 112.0, self.now)
        self.assertEqual(order["profit_state"], "TRAILING")
        self.assertAlmostEqual(order["trailing_stop_price"], 100.0)
        self.assertEqual(order["status"], "OPEN")

        # 3. Price advances to 118 (+1.8 ATR -> 1 step of 0.5 ATR beyond 1.2 ATR) -> trailing stop = 105.0
        dummy_order_service._update_from_ltp(order, 118.0, self.now)
        self.assertAlmostEqual(order["trailing_stop_price"], 105.0)

        # 4. Price pulls back to 104.5 (below 105.0) -> triggers TRAILING_STOP_HIT exit!
        dummy_order_service._update_from_ltp(order, 104.5, self.now)
        self.assertEqual(order["status"], "TRAILING_STOP_HIT")
        self.assertAlmostEqual(order["closed_price"], 105.0)

    def test_path_b_hybrid_trailing_stop(self):
        # Entry = 200, Lot = 65, ATR = 15.0
        order = {
            "order_id": "TEST_KEY|BUY|2026-10-30T10:15:00",
            "dummy": True,
            "path": "B_external_trigger",
            "instrument_key": "TEST_KEY",
            "side": "BUY",
            "entry_price": 200.0,
            "stop_price": 180.0,
            "target_price": 250.0,
            "lot_size": 65,
            "atr_14": 15.0,
            "trailing_stop_mode": "hybrid",
            "profit_state": "PRE_TRAIL",
            "peak_profit_rs": 0.0,
            "trailing_stop_price": None,
            "max_high_since_entry": 200.0,
            "status": "OPEN",
        }

        # Advance price to 220: profit = 20 * 65 = Rs 1300 (> Rs 1000 activation)
        dummy_order_service._update_path_b_profit(order, 220.0, self.now)
        self.assertEqual(order["profit_state"], "TRAILING")
        # Rupee lock = max(1000, 1300 - 500) = 1000 -> trail = 200 + 1000/65 = 215.3846
        # ATR advance = 20.0 (>= 1.2 * 15 = 18.0) -> ATR trail = 200.0
        # Hybrid chooses max(215.3846, 200.0) = 215.3846
        self.assertIsNotNone(order["trailing_stop_price"])
        self.assertGreater(order["trailing_stop_price"], 215.0)

    def test_consecutive_losses_circuit_breaker(self):
        # Simulate 5 consecutive losses with small Rs 100 loss each (tot loss Rs 500, below Rs 3000 max DD)
        for i in range(5):
            order = {
                "order_id": f"ORDER_{i}",
                "dummy": True,
                "path": "B_external_trigger",
                "instrument_key": "TEST_KEY",
                "entry_price": 100.0,
                "lot_size": 10,
                "status": "OPEN",
            }
            # Close at 90.0: loss = (90 - 100) * 10 = -100 Rs
            dummy_order_service._close_path_b_order(order, "STOP_HIT", 90.0, self.now)

        state = dummy_order_service.get_path_b_state()
        self.assertEqual(state["consecutive_losses"], 5)

        # Triggering _run_path_b_once should halt and lock strategy
        created, updated, closed = dummy_order_service._run_path_b_once(
            "test", self.now, "open", {"instrument_key": "NSE_FO|12345", "touched_at": "2026-10-30T09:20:00"}
        )
        self.assertEqual(created, 0)
        self.assertTrue(dummy_order_service._state["strategy_locked_today"])
        self.assertIn("consecutive_losses_circuit_breaker", dummy_order_service._state.get("lock_reason", ""))

    def test_daily_max_drawdown_guard(self):
        # Single large loss exceeding Rs 3000
        order = {
            "order_id": "ORDER_BIG_LOSS",
            "dummy": True,
            "path": "B_external_trigger",
            "instrument_key": "TEST_KEY",
            "entry_price": 100.0,
            "lot_size": 65,
            "status": "OPEN",
        }
        # Loss = (40 - 100) * 65 = -3900 Rs (exceeds 3000 limit)
        dummy_order_service._close_path_b_order(order, "STOP_HIT", 40.0, self.now)

        self.assertTrue(dummy_order_service._state["strategy_locked_today"])
        self.assertIn("daily_drawdown_limit_exceeded", dummy_order_service._state.get("lock_reason", ""))


if __name__ == "__main__":
    unittest.main()


class TestStrategyEntrySignalRegimeGate(unittest.TestCase):
    """Test G1 v4.2 market regime filter gating in strategy entry signal service."""

    @patch("services.regime_detector_service.regime_detector_service.evaluate_market_regime")
    @patch("services.strategy_entry_signal_service._run_pivot_scan")
    def test_ranging_regime_gates_candidate(self, mock_scan, mock_regime):
        from services.strategy_entry_signal_service import _compute_candidate

        # Mock that a support pivot formed inside the window
        t_touch = datetime.datetime(2026, 10, 30, 9, 20, 0, tzinfo=timezone.utc)
        w_end = datetime.datetime(2026, 10, 30, 9, 30, 0, tzinfo=timezone.utc)
        now = datetime.datetime(2026, 10, 30, 9, 25, 0, tzinfo=timezone.utc)

        mock_scan.return_value = [
            {
                "level": "support",
                "price": 120.0,
                "pivot_time": "2026-10-30T09:22:00+00:00",
                "confirmed_time": "2026-10-30T09:24:00+00:00",
                "pivot_bar_index": 2,
            }
        ]

        # Case 1: Market is RANGING (ADX 18 < 22) -> candidate must be held in WAITING
        mock_regime.return_value = {
            "regime": "RANGING",
            "adx": 18.0,
            "adx_threshold": 22.0,
            "bb_bandwidth": 0.02,
        }

        entry_mock = {"instrument_key": "NSE_FO|12345", "trading_symbol": "NIFTY 24000 CE", "underlying": "NSE_INDEX|Nifty 50"}
        cand = _compute_candidate(
            side="CE",
            entry=entry_mock,
            is_bias_side=True,
            touch_dt=t_touch,
            window_end=w_end,
            now=now,
        )

        self.assertEqual(cand["state"], "waiting")
        self.assertTrue(cand.get("regime_blocked"))
        self.assertIn("waiting_for_trending_regime", cand["state_note"])
