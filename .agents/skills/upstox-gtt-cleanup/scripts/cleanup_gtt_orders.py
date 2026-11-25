"""Preview and safely clean up Upstox GTT orders.

Triggered entries are exited only when linked fills reconcile exactly to a live
position. Untriggered active GTTs are cancelled. Ambiguous states are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "upstox" / "scripts"))

from upstox_helpers import order_api_v3
from order_cleanup_helpers import (
    cancel_gtt_order, data_records, fingerprint, get_snapshot, market_exit,
    plain, position_index, position_quantity, signed_trade_quantity,
    trades_for_order,
)


TERMINAL = {"cancelled", "canceled", "expired", "rejected", "deleted"}
PENDING = {"active", "open", "pending", "created"}
TRIGGERED = {"triggered", "complete", "completed", "partially_filled", "partially filled"}


def _status(record: dict) -> str:
    return str(record.get("status") or record.get("order_status") or "").strip().lower()


def _is_triggered(gtt: dict) -> bool | None:
    rules = gtt.get("rules") or []
    entry_rules = [r for r in rules if str(r.get("strategy") or "").upper() == "ENTRY"]
    if len(entry_rules) > 1:
        return None
    if entry_rules:
        entry = entry_rules[0]
        if any(entry.get(k) for k in ("order_id", "triggered_order_id", "exchange_order_id")):
            return True
        state = _status(entry)
        if state in TRIGGERED:
            return True
        if state in TERMINAL:
            return False
    state = _status(gtt)
    if state in TRIGGERED:
        return True
    if state in PENDING:
        return False
    if state in TERMINAL:
        return False
    return None


def _linked_order_ids(gtt: dict) -> list[str]:
    # Only entry fills define the position opened by this GTT. Do not aggregate
    # TARGET/STOPLOSS order IDs into the entry fill quantity.
    found: list[str] = []
    for key in ("entry_order_id", "triggered_order_id", "order_id"):
        value = gtt.get(key)
        if value:
            found.append(str(value))
    entry_rules = [
        r for r in (gtt.get("rules") or [])
        if str(r.get("strategy") or "").upper() == "ENTRY"
    ]
    for rule in entry_rules:
        for key in ("order_id", "triggered_order_id", "exchange_order_id"):
            value = rule.get(key)
            if value:
                found.append(str(value))
    return list(dict.fromkeys(found))


def build_plan(snapshot: dict) -> tuple[dict, dict[str, list[dict]]]:
    gtts = snapshot["gtt_orders"]
    positions = position_index(snapshot["positions"])
    plan = {"cancel": [], "exit": [], "skip": [], "nothing_to_do": False}
    trade_cache: dict[str, list[dict]] = {}

    for gtt in gtts:
        gid = str(gtt.get("gtt_order_id") or gtt.get("id") or "")
        state = _is_triggered(gtt)
        if not gid:
            plan["skip"].append({"gtt_order_id": None, "reason": "GTT record has no identifiable GTT order ID."})
            continue
        if state is False:
            status = _status(gtt)
            if status in TERMINAL:
                continue
            plan["cancel"].append({"gtt_order_id": gid, "status": status or "unknown"})
            continue
        if state is None:
            plan["skip"].append({"gtt_order_id": gid, "reason": "GTT lifecycle state is ambiguous; no action taken."})
            continue

        instrument = str(gtt.get("instrument_token") or gtt.get("instrument_key") or "")
        product = str(gtt.get("product") or "").upper()
        side = str(gtt.get("transaction_type") or "").upper()
        ids = _linked_order_ids(gtt)
        if not instrument or not product or side not in {"BUY", "SELL"} or not ids:
            plan["skip"].append({
                "gtt_order_id": gid,
                "reason": "Triggered GTT lacks instrument/product/side or linked triggered order ID; cannot safely reconcile its fill.",
            })
            continue

        # All linked fill quantities must be retrievable and agree on one signed fill.
        signed_fill = 0
        linked_ok = True
        for oid in ids:
            try:
                rows = trades_for_order(oid)
            except Exception:
                linked_ok = False
                break
            trade_cache[oid] = rows
            qty = signed_trade_quantity(rows)
            if qty == 0:
                linked_ok = False
                break
            signed_fill += qty
        if not linked_ok or signed_fill == 0:
            plan["skip"].append({"gtt_order_id": gid, "reason": "Linked order trade fills are unavailable or ambiguous; no exit submitted."})
            continue

        expected_sign = 1 if side == "BUY" else -1
        if signed_fill * expected_sign <= 0:
            plan["skip"].append({"gtt_order_id": gid, "reason": "Linked trade direction does not match GTT entry direction."})
            continue
        pos = positions.get((instrument, product))
        if pos and pos.get("_ambiguous"):
            plan["skip"].append({"gtt_order_id": gid, "reason": "Multiple position records share this instrument/product; refusing an ambiguous exit."})
            continue
        net_qty = position_quantity(pos)
        if net_qty != signed_fill:
            plan["skip"].append({
                "gtt_order_id": gid, "instrument_token": instrument, "product": product,
                "linked_signed_fill_quantity": signed_fill, "current_position_quantity": net_qty,
                "reason": "Current position does not exactly match this GTT's linked fills; manual review required.",
            })
            continue
        # A separate active GTT exit leg can still fire after a market exit.
        active_exit_rules = [
            r for r in (gtt.get("rules") or [])
            if str(r.get("strategy") or "").upper() in {"STOPLOSS", "TARGET"}
            and _status(r) not in TERMINAL
        ]
        plan["exit"].append({
            "gtt_order_id": gid, "instrument_token": instrument, "product": product,
            "quantity": abs(net_qty), "net_position_quantity": net_qty,
            "side": "SELL" if net_qty > 0 else "BUY",
            "linked_order_ids": ids,
            "cancel_gtt_before_exit": bool(active_exit_rules),
        })

    # If more than one GTT maps to the same live position, do not close either.
    counts: dict[tuple[str, str], int] = {}
    for item in plan["exit"]:
        key = (item["instrument_token"], item["product"])
        counts[key] = counts.get(key, 0) + 1
    duplicates = {key for key, count in counts.items() if count > 1}
    if duplicates:
        retained = []
        for item in plan["exit"]:
            key = (item["instrument_token"], item["product"])
            if key in duplicates:
                plan["skip"].append({"gtt_order_id": item["gtt_order_id"], "reason": "Multiple triggered GTTs map to the same position; refusing ambiguous duplicate exits."})
            else:
                retained.append(item)
        plan["exit"] = retained

    plan["nothing_to_do"] = not plan["cancel"] and not plan["exit"]
    return plan, trade_cache


def execute(plan: dict) -> dict:
    result = {"cancelled": [], "exited": [], "failed": [], "skipped": list(plan["skip"])}
    api = order_api_v3()
    for item in plan["cancel"]:
        gid = item["gtt_order_id"]
        try:
            response = cancel_gtt_order(gid)
            latest = data_records(api.get_gtt_order_details(gtt_order_id=gid))
            if not latest or _status(latest[0]) not in TERMINAL:
                raise RuntimeError("GTT cancellation was not verified as terminal.")
            result["cancelled"].append({"gtt_order_id": gid, "verified_status": _status(latest[0]), "api_response": plain(response)})
        except Exception as exc:
            result["failed"].append({"gtt_order_id": gid, "action": "cancel_gtt", "error": str(exc)})
    # For triggered GTTs, first cancel the GTT so its remaining TARGET/STOPLOSS
    # legs cannot reverse a manual square-off. Verify cancellation before exit.
    for item in plan["exit"]:
        gid = item["gtt_order_id"]
        try:
            if item["cancel_gtt_before_exit"]:
                cancel_gtt_order(gid)
                latest = data_records(api.get_gtt_order_details(gtt_order_id=gid))
                if not latest:
                    raise RuntimeError("Could not verify GTT state after cancellation.")
                state = _status(latest[0])
                if state not in TERMINAL:
                    raise RuntimeError(f"GTT is not verified terminal after cancellation (status={state or 'unknown'}); position not exited.")
            response = market_exit(
                item["instrument_token"], item["product"], item["quantity"],
                item["net_position_quantity"], f"gtt-cleanup-{gid}",
            )
            ids = list(getattr(getattr(response, "data", None), "order_ids", []) or [])
            verified = []
            for oid in ids:
                from order_cleanup_helpers import verify_order
                verified.append(verify_order(str(oid)))
            result["exited"].append({
                "gtt_order_id": gid, "exit_order_ids": ids,
                "verification": verified, "api_response": plain(response),
            })
        except Exception as exc:
            result["failed"].append({"gtt_order_id": gid, "action": "cancel_then_exit", "error": str(exc)})
    result["status"] = "success" if not result["failed"] else "partial"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview/execute GTT cancellation and triggered-position exit.")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-state-hash")
    args = parser.parse_args()
    try:
        snapshot = get_snapshot(include_gtt=True)
        plan, trades = build_plan(snapshot)
        state_hash = fingerprint(snapshot, trades, plan)
        if not args.confirm:
            print(json.dumps({
                "status": "preview", "operation": "gtt_cleanup",
                "gtt_count": len(snapshot["gtt_orders"]), "plan": plan,
                "state_fingerprint": state_hash, "confirmation_required": bool(plan["cancel"] or plan["exit"]),
                "message": "No changes submitted. Review every cancellation and exit before confirming.",
            }, indent=2, ensure_ascii=False, default=str))
            return 0
        if not args.expected_state_hash or args.expected_state_hash != state_hash:
            raise RuntimeError("Current GTT/order/position state differs from the approved preview. No action taken; create a fresh preview.")
        # Fetch once more immediately before the first write.
        latest_snapshot = get_snapshot(include_gtt=True)
        latest_plan, latest_trades = build_plan(latest_snapshot)
        if fingerprint(latest_snapshot, latest_trades, latest_plan) != args.expected_state_hash:
            raise RuntimeError("State changed during preflight. No action taken; create a fresh preview.")
        if latest_plan["nothing_to_do"]:
            print(json.dumps({"status": "no_action", "operation": "gtt_cleanup", "message": "No triggered GTT positions to exit and no untriggered active GTT orders to cancel.", "cancelled": [], "exited": [], "skipped": latest_plan["skip"]}, indent=2))
            return 0
        result = execute(latest_plan)
        result["operation"] = "gtt_cleanup"
        result["state_fingerprint"] = args.expected_state_hash
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result["status"] == "success" else 2
    except Exception as exc:
        print(json.dumps({"status": "error", "operation": "gtt_cleanup", "error": str(exc), "action_taken": False}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
