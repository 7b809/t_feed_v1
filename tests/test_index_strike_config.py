import unittest

from core.instrument_specs import (
    INDEX_INSTRUMENT_SPECS,
    filter_provider_contracts,
    validate_strike_range,
)


class IndexStrikeConfigurationTests(unittest.TestCase):
    def test_static_steps_are_per_underlying(self):
        self.assertEqual(INDEX_INSTRUMENT_SPECS["NIFTY"].strike_step, 50)
        self.assertEqual(INDEX_INSTRUMENT_SPECS["SENSEX"].strike_step, 100)
        self.assertEqual(INDEX_INSTRUMENT_SPECS["BANKNIFTY"].strike_step, 100)

    def test_nifty_range_and_alignment(self):
        self.assertEqual(
            validate_strike_range("NIFTY", 23000, 23200, 50), (23000, 23200)
        )
        with self.assertRaisesRegex(ValueError, "23025.*step 50"):
            validate_strike_range("NIFTY", 23025, 23200, 50)
        with self.assertRaisesRegex(ValueError, "23075.*step 50"):
            validate_strike_range("NIFTY", 23000, 23075, 50)

    def test_sensex_range_and_alignment(self):
        self.assertEqual(
            validate_strike_range("SENSEX", 80000, 80400, 100), (80000, 80400)
        )
        with self.assertRaisesRegex(ValueError, "80050.*step 100"):
            validate_strike_range("SENSEX", 80050, 80400, 100)
        with self.assertRaisesRegex(ValueError, "80150.*step 100"):
            validate_strike_range("SENSEX", 80000, 80150, 100)

    def test_range_filter_is_inclusive_and_uses_per_index_step(self):
        nifty = [{"strike_price": strike} for strike in (23000, 23050, 23100, 23150, 23200, 23225)]
        sensex = [{"strike_price": strike} for strike in (80000, 80100, 80200, 80300, 80400, 80450)]
        self.assertEqual(
            [item["strike_price"] for item in filter_provider_contracts(nifty, 23000, 23200, 50)],
            [23000, 23050, 23100, 23150, 23200],
        )
        self.assertEqual(
            [item["strike_price"] for item in filter_provider_contracts(sensex, 80000, 80400, 100)],
            [80000, 80100, 80200, 80300, 80400],
        )

    def test_filter_only_keeps_actual_provider_contracts(self):
        provider_rows = [
            {"instrument_key": "provider-80000", "strike_price": 80000},
            {"instrument_key": "provider-80200", "strike_price": 80200},
        ]
        filtered = filter_provider_contracts(provider_rows, 80000, 82000, 100)
        self.assertEqual([row["strike_price"] for row in filtered], [80000, 80200])
        self.assertNotIn(80100, [row["strike_price"] for row in filtered])

    def test_range_requires_both_bounds_and_ascending_order(self):
        with self.assertRaisesRegex(ValueError, "both STRIKE_FROM and STRIKE_TO"):
            validate_strike_range("SENSEX", 80000, None, 100)
        with self.assertRaisesRegex(ValueError, "greater than"):
            validate_strike_range("SENSEX", 81000, 80000, 100)


if __name__ == "__main__":
    unittest.main()
