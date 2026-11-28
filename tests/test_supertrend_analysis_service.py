"""Unit tests for the Supertrend calculation."""
import unittest

from services.supertrend_analysis_service import calculate_supertrend


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


if __name__ == "__main__":
    unittest.main()
