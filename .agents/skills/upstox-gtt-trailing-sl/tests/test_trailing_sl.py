import importlib.util
import pathlib
import unittest
from types import SimpleNamespace

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "place_trailing_sl.py"
SPEC = importlib.util.spec_from_file_location("place_trailing_sl", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TrailingStopTests(unittest.TestCase):
    def test_buy_levels_are_relative_to_actual_fill(self):
        self.assertEqual(MODULE.calculate_levels(100, "BUY", 30, 220, 0.05), (70, 320))

    def test_sell_levels_are_relative_to_actual_fill(self):
        self.assertEqual(MODULE.calculate_levels(400, "SELL", 30, 220, 0.05), (430, 180))

    def test_buy_trailing_advances_in_full_jumps_only(self):
        self.assertEqual(MODULE.calculate_trailing_stop(100, 139.9, "BUY", 70, 20, 0.05), 90)
        self.assertEqual(MODULE.calculate_trailing_stop(100, 140, "BUY", 70, 20, 0.05), 110)

    def test_sell_trailing_advances_down_in_full_jumps_only(self):
        self.assertEqual(MODULE.calculate_trailing_stop(400, 360.1, "SELL", 430, 20, 0.05), 410)
        self.assertEqual(MODULE.calculate_trailing_stop(400, 360, "SELL", 430, 20, 0.05), 390)

    def test_stop_never_moves_backwards(self):
        self.assertEqual(MODULE.calculate_trailing_stop(100, 90, "BUY", 70, 20, 0.05), 70)
        self.assertEqual(MODULE.calculate_trailing_stop(400, 410, "SELL", 430, 20, 0.05), 430)

    def test_preview_hash_is_stable_and_tracks_strategy_inputs(self):
        base = dict(instrument="NSE_EQ|INE002A01018", side="BUY", quantity=1,
                    product="I", sl_distance=30.0, target_distance=220.0,
                    trailing_jump=20.0, tick_size=0.05)
        first = MODULE.config_fingerprint(SimpleNamespace(**base))
        changed = {**base, "trailing_jump": 25.0}
        second = MODULE.config_fingerprint(SimpleNamespace(**changed))
        self.assertEqual(first, MODULE.config_fingerprint(SimpleNamespace(**base)))
        self.assertNotEqual(first, second)

    def test_invalid_nonpositive_levels_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.calculate_levels(100, "BUY", 150, 220, 0.05)


if __name__ == "__main__":
    unittest.main()
