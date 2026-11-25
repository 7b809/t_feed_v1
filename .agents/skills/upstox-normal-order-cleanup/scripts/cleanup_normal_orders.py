"""Preview and safely clean up today's Upstox normal orders and positions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "upstox" / "scripts"))

from order_cleanup_helpers import (
    cancel_normal_order, fingerprint, get_snapshot, market_exit, plain,
    position_index, position_quantity, signed_trade_quantity, data_records,
)
from upstox_helpers import order_api


PENDING = {"open", "trigger pending", "pending"}
TERMINAL = {"complete", "completed", "cancelled", "canceled", "rejected", "expired"}


def _status(order: dict) -> str:
    return str(order.get("status") or "").strip().lower()


def _trades_by_key() -> dict[tuple[str, str], list[dict]]:
    rows = data_records(order_api().get_trade_history(api_version="2.0"))
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("instrument_token") or ""), str(row.get("product") or "").upper())
        if key[0]:
            grouped.setdefault(key, []).append(row)
    return grouped


def build_plan(snapshot: dict, grouped_trades: dict[tuple[str, str], list[dict]]) -> dict:
    plan = {"cancel": [], "exit": [], "skip": [], "nothing_to_do": False}
    pending_keys: set[tuple[str, str]] = set()
    for order in snapshot["normal_orders"]:
        status = _status(order)
        oid = str(order.get("order_id") or "")
        instrument = str(order.get("instrument_token") or "")
        product = str(order.get("product") or "").upper()
        key = (instrument, product)
        if status in PENDING:
            if not oid:
                plan["skip"].append({"order_id": None, "reason": "Pending normal order has no order ID."})
                if instrument:
                    pending_keys.add(key)
            else:
                plan["cancel"].append({"order_id": oid, "status": status, "instrument_token": instrument, "product": product})
                if instrument:
                    pending_keys.add(key)
        elif status not in TERMINAL:
            plan["skip"].append({"order_id": oid or None, "reason": f"Unrecognized order status {status!r}; no action taken."})

    positions = position_index(snapshot["positions"])
    active_gtt_keys = set()
    for gtt in snapshot["gtt_orders"]:
        status = _status(gtt)
        if status in {"active", "open", "pending", "trigger pending", "triggered"}:
            active_gtt_keys.add((str(gtt.get("instrument_token") or ""), str(gtt.get("product") or "").upper()))

    for key, position in positions.items():
        net_qty = position_quantity(position)
        if net_qty == 0:
            continue
        instrument, product = key
        if key in pending_keys:
            plan["skip"].append({"instrument_token": instrument, "product": product, "position_quantity": net_qty, "reason": "Pending normal order exists for this instrument/product; cancel must settle before any exit."})
            continue
        if key in active_gtt_keys:
            plan["skip"].append({"instrument_token": instrument, "product": product, "position_quantity": net_qty, "reason": "An active GTT exists for this instrument/product; use GTT cleanup first to prevent a later trigger."})
            continue
        trades = grouped_trades.get(key, [])
        signed = signed_trade_quantity(trades)
        if signed != net_qty:
            plan["skip"].append({
                "instrument_token": instrument, "product": product,
                "position_quantity": net_qty, "today_signed_trade_quantity": signed,
                "reason": "Today's trade history does not exactly reconcile to the live position (e.g. carry-forward or external activity); no exit submitted.",
            })
            continue
        plan["exit"].append({
            "instrument_token": instrument, "product": product,
            "quantity": abs(net_qty), "net_position_quantity": net_qty,
            "side": "SELL" if net_qty > 0 else "BUY",
        })
    plan["nothing_to_do"] = not plan["cancel"] and not plan["exit"]
    return plan


def execute(plan: dict) -> dict:
    result = {"cancelled": [], "exited": [], "failed": [], "skipped": list(plan["skip"])}
    for item in plan["cancel"]:
        oid = item["order_id"]
        try:
            response = cancel_normal_order(oid)
            result["cancelled"].append({"order_id": oid, "api_response": plain(response)})
        except Exception as exc:
            result["failed"].append({"order_id": oid, "action": "cancel_normal_order", "error": str(exc)})
    # Re-fetch after cancellations; only exit if no still-live pending order and
    # the current position still reconciles with today's trade history.
    fresh = get_snapshot(include_gtt=True)
    grouped = _trades_by_key()
    fresh_plan = build_plan(fresh, grouped)
    cancelled_ids = {x["order_id"] for x in result["cancelled"]}
    failed_ids = {x["order_id"] for x in result["failed"] if x.get("order_id")}
    for item in plan["exit"]:
        key = (item["instrument_token"], item["product"])
        still_planned = next((x for x in fresh_plan["exit"] if (x["instrument_token"], x["product"]) == key), None)
        if not still_planned:
            result["skipped"].append({"instrument_token": key[0], "product": key[1], "reason": "Position no longer passes fresh post-cancellation reconciliation; no exit submitted."})
            continue
        # Do not square off if a cancellation for this key failed.
        if any(x.get("order_id") in failed_ids and (x.get("instrument_token"), x.get("product")) == key for x in plan["cancel"]):
            result["skipped"].append({"instrument_token": key[0], "product": key[1], "reason": "A pending order cancellation failed; no position exit submitted."})
            continue
        try:
            response = market_exit(key[0], key[1], still_planned["quantity"], still_planned["net_position_quantity"], "normal-order-cleanup")
            ids = list(getattr(getattr(response, "data", None), "order_ids", []) or [])
            result["exited"].append({"instrument_token": key[0], "product": key[1], "exit_order_ids": ids, "api_response": plain(response)})
        except Exception as exc:
            result["failed"].append({"instrument_token": key[0], "product": key[1], "action": "market_exit", "error": str(exc)})
    result["status"] = "success" if not result["failed"] else "partial"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview/execute cancellation of pending normal orders and exit of reconciled positions.")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-state-hash")
    args = parser.parse_args()
    try:
        snapshot = get_snapshot(include_gtt=True)
        trades = _trades_by_key()
        plan = build_plan(snapshot, trades)
        state_hash = fingerprint(snapshot, trades, plan)
        if not args.confirm:
            print(json.dumps({
                "status": "preview", "operation": "normal_order_cleanup",
                "normal_order_count": len(snapshot["normal_orders"]), "plan": plan,
                "state_fingerprint": state_hash, "confirmation_required": bool(plan["cancel"] or plan["exit"]),
                "message": "No changes submitted. Review every cancellation and position exit before confirming.",
            }, indent=2, ensure_ascii=False, default=str))
            return 0
        if not args.expected_state_hash or args.expected_state_hash != state_hash:
            raise RuntimeError("Current order/position state differs from the approved preview. No action taken; create a fresh preview.")
        latest_snapshot = get_snapshot(include_gtt=True)
        latest_trades = _trades_by_key()
        latest_plan = build_plan(latest_snapshot, latest_trades)
        if fingerprint(latest_snapshot, latest_trades, latest_plan) != args.expected_state_hash:
            raise RuntimeError("State changed during preflight. No action taken; create a fresh preview.")
        if latest_plan["nothing_to_do"]:
            print(json.dumps({"status": "no_action", "operation": "normal_order_cleanup", "message": "No pending normal orders to cancel and no reconciled open positions to exit.", "cancelled": [], "exited": [], "skipped": latest_plan["skip"]}, indent=2))
            return 0
        result = execute(latest_plan)
        result["operation"] = "normal_order_cleanup"
        result["state_fingerprint"] = args.expected_state_hash
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result["status"] == "success" else 2
    except Exception as exc:
        print(json.dumps({"status": "error", "operation": "normal_order_cleanup", "error": str(exc), "action_taken": False}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
