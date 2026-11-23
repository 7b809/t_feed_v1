"""Safely modify exactly one trigger price on an existing Upstox GTT order.

Without --confirm this script only fetches, validates, and previews the change.
With --confirm it re-fetches the order, submits the complete updated rules list,
then fetches again to verify the requested value.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
UPSTOX_SCRIPTS = SKILL_ROOT / "upstox" / "scripts"
sys.path.insert(0, str(UPSTOX_SCRIPTS))

import upstox_client  # noqa: E402
from upstox_helpers import order_api_v3  # noqa: E402
from validate_gtt_modify import (  # noqa: E402
    to_plain,
    unwrap_order,
    validate_gtt_modification,
)


def _fetch(api, order_id: str) -> dict:
    return unwrap_order(api.get_gtt_order_details(gtt_order_id=order_id), order_id)


def _verify(order: dict, leg: str, expected: float) -> bool:
    matches = [
        rule for rule in order.get("rules", [])
        if str(rule.get("strategy", "")).upper() == leg.upper()
    ]
    if len(matches) != 1:
        return False
    try:
        actual = float(matches[0]["trigger_price"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.000001)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Modify one ENTRY, STOPLOSS, or TARGET trigger price on an existing Upstox GTT."
    )
    parser.add_argument("--gtt-order-id", required=True)
    parser.add_argument("--leg", required=True, choices=["ENTRY", "STOPLOSS", "TARGET"])
    parser.add_argument("--trigger-price", required=True, type=float)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually submit the live modification. Without this flag, only preview.",
    )
    args = parser.parse_args()

    try:
        api = order_api_v3()
        existing = _fetch(api, args.gtt_order_id)
        preview = validate_gtt_modification(existing, args.leg, args.trigger_price)

        print("GTT MODIFICATION PREVIEW")
        print("=" * 58)
        print(f"Order ID           : {args.gtt_order_id}")
        print(f"Leg                : {preview['leg']}")
        print(f"Old trigger price  : {preview['old_trigger_price']}")
        print(f"New trigger price  : {preview['new_trigger_price']}")
        print(f"Quantity (retained): {preview['quantity']}")
        print(f"Order type (kept)  : {preview['type']}")
        print("Other GTT rules    : retained from the latest fetched order")
        print("=" * 58)

        if not args.confirm:
            print("PREVIEW ONLY — no modification was submitted.")
            print("Review the values, obtain explicit user approval, then rerun with --confirm.")
            return 0

        # Fetch again immediately before writing to avoid applying a stale preview.
        latest = _fetch(api, args.gtt_order_id)
        latest_preview = validate_gtt_modification(latest, args.leg, args.trigger_price)

        request_data = latest_preview["modify_request"]
        sdk_rules = [
            upstox_client.GttRule(**rule)
            for rule in request_data["rules"]
        ]
        body = upstox_client.GttModifyOrderRequest(
            type=request_data["type"],
            gtt_order_id=request_data["gtt_order_id"] or args.gtt_order_id,
            quantity=request_data["quantity"],
            rules=sdk_rules,
        )

        response = api.modify_gtt_order(body=body)
        print("\nUPSTOX MODIFY RESPONSE")
        print(json.dumps(to_plain(response), indent=2, default=str))

        verified = _fetch(api, args.gtt_order_id)
        if not _verify(verified, args.leg, args.trigger_price):
            print(
                "\nWARNING: Modification request returned, but the follow-up read "
                "does not show the requested trigger price. Check the order in Upstox.",
                file=sys.stderr,
            )
            print(json.dumps(verified, indent=2, default=str), file=sys.stderr)
            return 2

        print("\nVERIFICATION: SUCCESS")
        print(f"{args.leg} trigger price is now {args.trigger_price}.")
        return 0

    except Exception as exc:
        print(f"ERROR: GTT modification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
