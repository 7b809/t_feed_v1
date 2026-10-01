import unittest
from unittest.mock import patch

from services.strategy_context import StrategyContext
from services.opening_range.live_touch import detect_touch_from_candle


class StrategyContextTests(unittest.TestCase):
    def test_option_contract_is_owned_by_its_underlying_context(self):
        context = StrategyContext("SENSEX", "BSE_INDEX|SENSEX", "SENSEX", True)
        context.set_option_universe([{"instrument_key": "BSE_FO|call-1", "instrument_type": "CE"}])

        contract = context.option_universe["BSE_FO|call-1"]
        self.assertEqual(contract["underlying"], "SENSEX")
        self.assertEqual(contract["underlying_instrument_key"], "BSE_INDEX|SENSEX")

    def test_disabled_context_does_not_retain_options(self):
        context = StrategyContext("BANKNIFTY", "NSE_INDEX|Nifty Bank", "NIFTY BANK", False)
        context.set_option_universe([{"instrument_key": "NSE_FO|option-1"}])
        self.assertEqual(context.option_universe, {})

    def test_context_selection_state_is_independent(self):
        nifty = StrategyContext("NIFTY", "NSE_INDEX|Nifty 50", "NIFTY 50", True)
        sensex = StrategyContext("SENSEX", "BSE_INDEX|SENSEX", "SENSEX", True)
        nifty.selected_instrument["instrument_key"] = "NSE_FO|nifty-call"
        sensex.selected_instrument["instrument_key"] = "BSE_FO|sensex-put"
        self.assertNotEqual(nifty.selected_instrument, sensex.selected_instrument)

    def test_restore_ignores_a_different_underlying_document(self):
        nifty = StrategyContext("NIFTY", "NSE_INDEX|Nifty 50", "NIFTY 50", True, trading_date="2026-10-02")
        sensex = StrategyContext("SENSEX", "BSE_INDEX|SENSEX", "SENSEX", True, trading_date="2026-10-02")
        nifty.selected_instrument = {"selected": True, "instrument_key": "NSE_FO|nifty-call"}
        sensex.restore(nifty.snapshot())
        self.assertEqual(sensex.selected_instrument, {})

    def test_completed_candle_preserves_directional_touch_semantics(self):
        with patch("services.opening_range.live_touch.DEFAULT_ISOLATION_TOUCH_LEVELS", {"R2"}):
            events = detect_touch_from_candle(
                "NSE_FO|test-option",
                {"timestamp": "2026-10-01T10:00:00+05:30", "high": 102.4, "low": 95.0, "close": 101.0},
                {"r2": 100.0, "r3": 110.0, "s2": 90.0, "s3": 80.0},
                {"instrument_key": "NSE_FO|test-option", "instrument_type": "CE", "strike_price": 25000},
                "completed_1minute_candle",
            )
        self.assertEqual([event["level"] for event in events], ["R2"])
        self.assertEqual(events[0]["trigger_field"], "high")
        self.assertEqual(events[0]["source"], "completed_1minute_candle")


if __name__ == "__main__":
    unittest.main()
