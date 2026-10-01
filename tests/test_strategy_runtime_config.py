import unittest

from core import config
from services.runtime_config_service import (
    RuntimeConfigService,
    RuntimeConfigValidationError,
)
from services.strategy_context import StrategyContext


class StrategyRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_runtime_enabled = config.RUNTIME_CONFIG_ENABLED
        self.original_cache_enabled = config.RUNTIME_CONFIG_CACHE_ENABLED
        config.RUNTIME_CONFIG_ENABLED = True
        config.RUNTIME_CONFIG_CACHE_ENABLED = True
        self.service = RuntimeConfigService()
        self.service._initialized = True

    def tearDown(self):
        config.RUNTIME_CONFIG_ENABLED = self.original_runtime_enabled
        config.RUNTIME_CONFIG_CACHE_ENABLED = self.original_cache_enabled

    def test_runtime_range_refresh_is_used_without_source_changes(self):
        self.service._cache.update({
            "STRATEGY_SENSEX_ENABLED": True,
            "STRATEGY_SENSEX_STRIKE_FROM": 80000,
            "STRATEGY_SENSEX_STRIKE_TO": 82000,
        })
        initial = self.service.get_strategy_settings("SENSEX")
        self.assertEqual(initial["option"]["strike_from"], 80000)
        self.assertEqual(initial["option"]["strike_to"], 82000)
        self.assertEqual(initial["option"]["strike_step"], 100)

        self.service._cache["STRATEGY_SENSEX_STRIKE_TO"] = 83000
        updated = self.service.get_strategy_settings("SENSEX")
        self.assertEqual(updated["option"]["strike_to"], 83000)

    def test_runtime_alignment_uses_each_underlyings_static_step(self):
        with self.assertRaisesRegex(RuntimeConfigValidationError, "step 100"):
            self.service._validate_related_values("STRATEGY_SENSEX_STRIKE_FROM", 80050)
        with self.assertRaisesRegex(RuntimeConfigValidationError, "step 50"):
            self.service._validate_related_values("STRATEGY_NIFTY_STRIKE_FROM", 23025)

    def test_disabled_banknifty_can_be_configured_for_future_enablement(self):
        self.service._cache.update({
            "STRATEGY_BANKNIFTY_ENABLED": False,
            "STRATEGY_BANKNIFTY_STRIKE_FROM": 45000,
            "STRATEGY_BANKNIFTY_STRIKE_TO": 50000,
        })
        settings = self.service.get_strategy_settings("BANKNIFTY")
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["option"]["strike_step"], 100)
        self.assertEqual(settings["option"]["strike_from"], 45000)

    def test_day_snapshot_keeps_the_initial_effective_range(self):
        context = StrategyContext(
            underlying="SENSEX",
            index_instrument_key="BSE_INDEX|SENSEX",
            display_name="SENSEX",
            enabled=True,
            option_config={"strike_from": 80000, "strike_to": 82000, "strike_step": 100},
            trading_date="2026-10-02",
        )
        context.strategy_config_snapshot = context._current_strategy_config()
        context.option_config["strike_to"] = 83000
        stored_snapshot = context.snapshot()["strategy_config"]
        self.assertEqual(stored_snapshot["option"]["strike_to"], 82000)


if __name__ == "__main__":
    unittest.main()
