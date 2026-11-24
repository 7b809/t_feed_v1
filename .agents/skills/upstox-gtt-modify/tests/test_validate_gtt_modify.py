"""Unit tests for multi-leg GTT modification validation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from validate_gtt_modify import (  # noqa: E402
    order_fingerprint,
    validate_gtt_modifications,
)


class MultiLegGttValidationTests(unittest.TestCase):
    def setUp(self):
        self.order = {
            "gtt_order_id": "test-gtt-123",
            "type": "MULTIPLE",
            "quantity": 25,
            "rules": [
                {"strategy": "ENTRY", "trigger_type": "ABOVE", "trigger_price": 250.0},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 220.0},
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": 280.0},
            ],
        }

    def test_updates_stoploss_and_target_together(self):
        result = validate_gtt_modifications(
            self.order, {"TARGET": 300, "STOPLOSS": 230}
        )
        self.assertEqual(
            result["changes"],
            [
                {"leg": "TARGET", "old_trigger_price": 280.0, "new_trigger_price": 300.0},
                {"leg": "STOPLOSS", "old_trigger_price": 220.0, "new_trigger_price": 230.0},
            ],
        )
        rules = result["modify_request"]["rules"]
        self.assertEqual(rules[0]["trigger_price"], 250.0)
        self.assertEqual(rules[1]["trigger_price"], 230.0)
        self.assertEqual(rules[2]["trigger_price"], 300.0)
        self.assertEqual(result["modify_request"]["quantity"], 25)
        self.assertEqual(result["modify_request"]["type"], "MULTIPLE")

    def test_rejects_missing_or_duplicated_selected_rule(self):
        self.order["rules"].append(dict(self.order["rules"][1]))
        with self.assertRaisesRegex(ValueError, "exactly one STOPLOSS"):
            validate_gtt_modifications(self.order, {"STOPLOSS": 231, "TARGET": 301})

    def test_rejects_non_positive_or_non_finite_price(self):
        for price in (0, -1, float("nan"), float("inf")):
            with self.subTest(price=price), self.assertRaises(ValueError):
                validate_gtt_modifications(self.order, {"TARGET": price})

    def test_rejects_duplicate_case_insensitive_legs(self):
        with self.assertRaisesRegex(ValueError, "Duplicate update"):
            validate_gtt_modifications(self.order, {"TARGET": 300, "target": 301})

    def test_fingerprint_is_stable_and_tracks_order_changes(self):
        first = order_fingerprint(self.order)
        self.assertEqual(first, order_fingerprint(dict(self.order)))
        changed = dict(self.order)
        changed["quantity"] = 50
        self.assertNotEqual(first, order_fingerprint(changed))


if __name__ == "__main__":
    unittest.main()
