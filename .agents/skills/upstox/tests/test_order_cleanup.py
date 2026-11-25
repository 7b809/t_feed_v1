"""Unit tests for conservative order-cleanup planning helpers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILLS = ROOT / ".agents" / "skills"
sys.path.insert(0, str(SKILLS / "upstox" / "scripts"))
sys.path.insert(0, str(SKILLS / "upstox-gtt-cleanup" / "scripts"))
sys.path.insert(0, str(SKILLS / "upstox-normal-order-cleanup" / "scripts"))

import cleanup_gtt_orders as gtt_cleanup
import cleanup_normal_orders as normal_cleanup
from order_cleanup_helpers import fingerprint


class GttClassificationTests(unittest.TestCase):
    def test_active_entry_is_untriggered(self):
        self.assertIs(gtt_cleanup._is_triggered({
            "status": "active",
            "rules": [{"strategy": "ENTRY", "status": "pending"}],
        }), False)

    def test_linked_entry_order_marks_triggered(self):
        self.assertIs(gtt_cleanup._is_triggered({
            "status": "active",
            "rules": [{"strategy": "ENTRY", "order_id": "ENTRY-1"}],
        }), True)

    def test_unknown_lifecycle_is_ambiguous(self):
        self.assertIsNone(gtt_cleanup._is_triggered({
            "status": "awaiting_confirmation",
            "rules": [{"strategy": "ENTRY"}],
        }))

    def test_only_entry_order_ids_are_used_for_fill_reconciliation(self):
        ids = gtt_cleanup._linked_order_ids({
            "rules": [
                {"strategy": "ENTRY", "order_id": "ENTRY-1"},
                {"strategy": "TARGET", "order_id": "TARGET-1"},
                {"strategy": "STOPLOSS", "order_id": "SL-1"},
            ]
        })
        self.assertEqual(ids, ["ENTRY-1"])


class NormalCleanupPlanTests(unittest.TestCase):
    def test_empty_account_snapshot_has_no_actions(self):
        plan = normal_cleanup.build_plan({
            "normal_orders": [], "positions": [], "gtt_orders": [],
        }, {})
        self.assertTrue(plan["nothing_to_do"])
        self.assertEqual(plan["cancel"], [])
        self.assertEqual(plan["exit"], [])

    def test_pending_order_is_scheduled_for_cancel(self):
        plan = normal_cleanup.build_plan({
            "normal_orders": [{
                "order_id": "ORDER-1", "status": "open",
                "instrument_token": "NSE_EQ|TEST", "product": "I",
            }],
            "positions": [],
            "gtt_orders": [],
        }, {})
        self.assertEqual([x["order_id"] for x in plan["cancel"]], ["ORDER-1"])

    def test_tuple_trade_keys_can_be_fingerprinted(self):
        value = fingerprint(
            {"positions": []},
            {("NSE_EQ|TEST", "I"): [{"quantity": 1}]},
            {"cancel": [], "exit": []},
        )
        self.assertEqual(len(value), 64)


if __name__ == "__main__":
    unittest.main()
