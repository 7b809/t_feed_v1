import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "place_trailing_sl.py"
SPEC = importlib.util.spec_from_file_location("place_trailing_sl", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TrailingStopTests(unittest.TestCase):
    def test_buy_levels_are_relative_to_actual_fill(self):
        self.assertEqual(MODULE.calculate_levels(100, "BUY", 30, 220, 0.05), (70, 320))

    def test_sell_levels_are_relative_to_actual_fill(self):
        self.assertEqual(MODULE.calculate_levels(100, "SELL", 30, 220, 0.05), (130, 0) if False else (130, 0))
