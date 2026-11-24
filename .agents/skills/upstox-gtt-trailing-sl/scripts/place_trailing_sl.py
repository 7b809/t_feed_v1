#!/usr/bin/env python3
"""Live market entry with broker-side SL-M and a locally managed fixed target/step trail.

The only broker-side exit kept active is the stop order. The fixed target is
monitored locally and exits at market after the stop is cancelled and verified.
This avoids leaving two independent exit orders live (a non-OCO over-exit risk).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.upstox.com"
ORDER_API = "https://api-hft.upstox.com"


def round_tick(value: float, tick: float) -> float:
    return round(round(value / tick) * tick, 8)


def calculate_levels(entry: float, side: str, sl_distance: float,
                     target_distance: float, tick: float) -> tuple[float, float]:
    if entry <= 0 or tick <= 0:
        raise ValueError("entry and tick size must be greater than zero")
    if side == "BUY":
        sl, target = entry - sl_distance, entry + target_distance
    elif side == "SELL":
        sl, target = entry + sl_distance, entry - target_distance
    else:
        raise ValueError("side must be BUY or SELL")
    sl, target = round_tick(sl, tick), round_tick(target, tick)
    if sl <= 0 or target <= 0:
        raise ValueError("Calculated stop-loss and target must both be above zero.")
    return sl, target


def calculate_trailing_stop(entry: float, ltp: float, side: str,
                            initial_sl: float, jump: float, tick: float) -> float:
    favourable = ltp - entry if side == "BUY" else entry - ltp
    steps = max(0, math.floor((favourable + 1e-10) / jump))
    candidate = initial_sl + steps * jump if side == "BUY" else initial_sl - steps * jump
    candidate = round_tick(candidate, tick)
    return max(initial_sl, candidate) if side == "BUY" else min(initial_sl, candidate)


def config_fingerprint(args: argparse.Namespace) -> str:
    # Exclude live LTP: a preview must remain confirmable if the quote ticks.
    config = {
        "instrument": args.instrument, "side": args.side, "quantity": args.quantity,
        "product": args.product, "sl_distance": args.sl_distance,
        "target_distance": args.target_distance, "trailing_jump": args.trailing_jump,
        "tick_size": args.tick_size,
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_inputs(args: argparse.Namespace) -> None:
    if args.quantity <= 0:
        raise ValueError("quantity must be greater than zero")
    for name in ("sl_distance", "target_distance", "trailing_jump", "tick_size"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name.replace('_', ' ')} must be greater than zero")
    if args.product not in {"I", "D", "MTF"}:
        raise ValueError("product must be I, D or MTF")
    if args.side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")


def request_json(method: str, url: str, token: str,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Upstox HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Upstox request failed: {exc}") from exc
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(f"Upstox API error: {result}")
    return result


def get_ltp(token: str, instrument: str) -> float:
    query = urllib.parse.urlencode({"instrument_key": instrument})
    result = request_json("GET", f"{API}/v3/market-quote/ltp?{query}", token)
    data = result.get("data") or {}
    quote = data.get(instrument.replace("|", ":"))
    if quote is None:
        quote = next((v for v in data.values()
                      if isinstance(v, dict) and v.get("instrument_token") == instrument), None)
    if not quote or float(quote.get("last_price") or 0) <= 0:
        raise RuntimeError(f"No valid LTP returned for {instrument}")
    return float(quote["last_price"])


def order_status(token: str, order_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"order_id": order_id, "api_version": "2.0"})
    result = request_json("GET", f"{API}/v2/order/details?{query}", token)
    data = result.get("data")
    if isinstance(data, list):
        if not data:
            raise RuntimeError(f"No order details returned for {order_id}")
        return data[-1]
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected order details response for {order_id}: {result}")
    return data


def place_order(token: str, *, instrument: str, quantity: int, product: str,
                side: str, order_type: str, price: float = 0.0,
                trigger_price: float = 0.0, tag: str) -> str:
    payload = {
        "quantity": quantity, "product": product, "validity": "DAY",
        "price": price, "tag": tag[:40], "instrument_token": instrument,
        "order_type": order_type, "transaction_type": side,
        "disclosed_quantity": 0, "trigger_price": trigger_price,
        "is_amo": False, "market_protection": 0,
    }
    result = request_json("POST", f"{ORDER_API}/v3/order/place", token, payload)
    data = result.get("data") or {}
    ids = data.get("order_ids") or ([data["order_id"]] if data.get("order_id") else [])
    if not ids:
        raise RuntimeError(f"Order placement returned no order ID: {result}")
    return str(ids[0])


def modify_stop(token: str, order_id: str, quantity: int, trigger: float) -> None:
    payload = {
        "order_id": order_id, "quantity": quantity, "validity": "DAY",
        "price": 0, "order_type": "SL-M", "disclosed_quantity": 0,
        "trigger_price": trigger, "market_protection": 0,
    }
    request_json("PUT", f"{ORDER_API}/v3/order/modify", token, payload)


def cancel_order(token: str, order_id: str) -> None:
    query = urllib.parse.urlencode({"order_id": order_id})
    request_json("DELETE", f"{ORDER_API}/v3/order/cancel?{query}", token)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str), flush=True)


def wait_for_terminal(token: str, order_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = order_status(token, order_id)
        if str(latest.get("status", "")).lower() in {"complete", "rejected", "cancelled"}:
            return latest
        time.sleep(1)
    return latest


def wait_for_entry(token: str, order_id: str, timeout: int) -> dict[str, Any]:
    latest = wait_for_terminal(token, order_id, timeout)
    if str(latest.get("status", "")).lower() not in {"complete", "rejected", "cancelled"}:
        # Cancel any unfilled remainder, then re-read the broker state.
        cancel_order(token, order_id)
        time.sleep(1)
        latest = order_status(token, order_id)
    return latest


def exit_on_target(token: str, args: argparse.Namespace, stop_id: str,
                   filled_qty: int, entry_id: str, target: float) -> int:
    cancel_order(token, stop_id)
    state = order_status(token, stop_id)
    stop_status = str(state.get("status", "")).lower()
    if stop_status == "complete":
        emit({"event": "stoploss_filled_before_target_exit", "stop_order": state,
              "entry_order_id": entry_id})
        return 0
    if stop_status != "cancelled":
        emit({"critical": "Could not verify stop cancellation; no target market exit was sent.",
              "stop_order": state, "manual_reconciliation_required": True})
        return 2

    exit_side = "SELL" if args.side == "BUY" else "BUY"
    exit_id = place_order(
        token, instrument=args.instrument, quantity=filled_qty, product=args.product,
        side=exit_side, order_type="MARKET", tag=f"trail-target-{entry_id}",
    )
    emit({"event": "target_reached_market_exit_submitted", "target": target,
          "exit_order_id": exit_id, "quantity": filled_qty})
    final = wait_for_terminal(token, exit_id, args.exit_wait)
    exited = int(final.get("filled_quantity") or 0)
    if str(final.get("status", "")).lower() == "complete" and exited >= filled_qty:
        emit({"event": "target_exit_complete", "exit_order": final})
        return 0
    emit({"critical": "Target market exit did not fully complete. Check remaining position and order immediately.",
          "exit_order": final, "expected_quantity": filled_qty,
          "filled_quantity": exited, "manual_reconciliation_required": True})
    return 2


def run_manager(token: str, args: argparse.Namespace, entry_price: float,
                filled_qty: int, entry_order_id: str) -> int:
    initial_sl, target = calculate_levels(
        entry_price, args.side, args.sl_distance, args.target_distance, args.tick_size
    )
    exit_side = "SELL" if args.side == "BUY" else "BUY"
    stop_id = place_order(
        token, instrument=args.instrument, quantity=filled_qty, product=args.product,
        side=exit_side, order_type="SL-M", trigger_price=initial_sl,
        tag=f"trail-sl-{entry_order_id}",
    )
    current_sl = initial_sl
    emit({
        "event": "protection_active", "entry_order_id": entry_order_id,
        "filled_quantity": filled_qty, "average_entry_price": entry_price,
        "stop_order_id": stop_id, "initial_stop": initial_sl, "fixed_target": target,
        "trailing_jump": args.trailing_jump,
        "notice": "Only the SL-M is broker-side. The fixed target is monitored locally; keep this manager running. If it stops, the last SL remains active but target monitoring/trailing stop.",
    })

    while True:
        time.sleep(args.monitor_interval)
        stop_state = order_status(token, stop_id)
        stop_status = str(stop_state.get("status", "")).lower()
        stop_filled = int(stop_state.get("filled_quantity") or 0)
        if stop_status == "complete":
            emit({"event": "stoploss_exit_complete", "stop_order": stop_state,
                  "entry_order_id": entry_order_id})
            return 0
        if stop_filled > 0:
            try:
                cancel_order(token, stop_id)
            except Exception:
                pass
            emit({"critical": "Partial stop execution detected. Automated management stopped; reconcile the remaining position and order immediately.",
                  "stop_order": stop_state})
            return 2
        if stop_status in {"rejected", "cancelled"}:
            emit({"critical": "Protective stop is no longer active. Automated management stopped.",
                  "stop_order": stop_state, "manual_reconciliation_required": True})
            return 2

        ltp = get_ltp(token, args.instrument)
        target_hit = ltp >= target if args.side == "BUY" else ltp <= target
        if target_hit:
            return exit_on_target(token, args, stop_id, filled_qty, entry_order_id, target)

        next_sl = calculate_trailing_stop(
            entry_price, ltp, args.side, initial_sl, args.trailing_jump, args.tick_size
        )
        improves = next_sl > current_sl if args.side == "BUY" else next_sl < current_sl
        if improves:
            modify_stop(token, stop_id, filled_qty, next_sl)
            current_sl = next_sl
            emit({"event": "stop_trailed", "ltp": ltp, "new_stop": current_sl,
                  "entry": entry_price, "target_unchanged": target})


def main() -> int:
    parser = argparse.ArgumentParser(description="Live market entry with step-trailing SL and fixed target.")
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--side", choices=["BUY", "SELL"], required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--product", choices=["I", "D", "MTF"], required=True)
    parser.add_argument("--sl-distance", type=float, required=True)
    parser.add_argument("--target-distance", type=float, required=True)
    parser.add_argument("--trailing-jump", type=float, required=True)
    parser.add_argument("--tick-size", type=float, required=True)
    parser.add_argument("--entry-wait", type=int, default=45)
    parser.add_argument("--exit-wait", type=int, default=45)
    parser.add_argument("--monitor-interval", type=float, default=1.0)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-preview-hash")
    args = parser.parse_args()

    try:
        validate_inputs(args)
        if args.entry_wait <= 0 or args.exit_wait <= 0 or args.monitor_interval < 0.5:
            raise ValueError("entry-wait/exit-wait must be > 0 and monitor-interval must be >= 0.5 seconds")
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            raise ValueError("UPSTOX_ACCESS_TOKEN is not set in the environment.")

        ltp = get_ltp(token, args.instrument)
        fingerprint = config_fingerprint(args)
        preview = {
            "instrument": args.instrument, "side": args.side, "quantity": args.quantity,
            "product": args.product, "entry_type": "MARKET",
            "reference_ltp_only_not_fill_price": ltp,
            "sl_distance": args.sl_distance, "target_distance": args.target_distance,
            "trailing_jump": args.trailing_jump, "tick_size": args.tick_size,
            "exit_model": "broker-side SL-M + locally monitored fixed target + step trailing",
            "manager_must_remain_running": True, "preview_hash": fingerprint,
        }
        if not args.confirm:
            emit({"mode": "PREVIEW_ONLY", **preview,
                  "instruction": "Review this exact configuration and hash. No order has been placed."})
            return 0
        if args.expected_preview_hash != fingerprint:
            raise ValueError("Preview hash missing or changed. No order was placed; generate and review a fresh preview.")

        entry_id = place_order(
            token, instrument=args.instrument, quantity=args.quantity, product=args.product,
            side=args.side, order_type="MARKET", tag="trail-entry",
        )
        emit({"event": "market_entry_submitted", "entry_order_id": entry_id})
        entry = wait_for_entry(token, entry_id, args.entry_wait)
        filled_qty = int(entry.get("filled_quantity") or 0)
        average_price = float(entry.get("average_price") or 0)
        status = str(entry.get("status", "")).lower()
        if filled_qty <= 0 or average_price <= 0:
            emit({"critical": "Market entry has no confirmed fill. Check the order before retrying.",
                  "entry_order": entry, "manual_reconciliation_required": True})
            return 2
        if status not in {"complete", "cancelled", "rejected"}:
            emit({"critical": "Entry did not reach a terminal state. Check remaining pending quantity immediately.",
                  "entry_order": entry, "manual_reconciliation_required": True})
            return 2
        return run_manager(token, args, average_price, filled_qty, entry_id)
    except KeyboardInterrupt:
        emit({"warning": "Manager interrupted. The last broker-side SL-M may remain active, but target monitoring and trailing have stopped. Reconcile immediately."})
        return 130
    except Exception as exc:
        emit({"error": str(exc), "live_order_may_exist": bool(args.confirm),
              "manual_reconciliation_required": bool(args.confirm)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
