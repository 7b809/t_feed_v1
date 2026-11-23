"""Fetch Upstox normal orders, GTT orders, or a filtered/specific order.

Read-only utility: this script never places, modifies, or cancels orders.
Normal order book/details use OrderApi v2; GTT listing/details use OrderApiV3.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[2]
UPSTOX_SCRIPTS = SKILL_ROOT / "upstox" / "scripts"
sys.path.insert(0, str(UPSTOX_SCRIPTS))

from upstox_helpers import order_api, order_api_v3  # noqa: E402
from order_filters import as_records, filter_records, normalize_record, response_data  # noqa: E402


def _normal_orders(order_id: str | None) -> list[dict]:
    api = order_api()
    if order_id:
        # get_order_details returns lifecycle history for the specified order.
        response = api.get_order_details(api_version="2.0", order_id=order_id)
    else:
        response = api.get_order_book(api_version="2.0")
    return [normalize_record(item, "normal") for item in as_records(response_data(response))]


def _gtt_orders(order_id: str | None) -> list[dict]:
    api = order_api_v3()
    response = (
        api.get_gtt_order_details(gtt_order_id=order_id)
        if order_id
        else api.get_gtt_order_details()
    )
    return [normalize_record(item, "gtt") for item in as_records(response_data(response))]


def _fetch(kind: str, order_id: str | None) -> list[dict]:
    if kind == "normal":
        return _normal_orders(order_id)
    if kind == "gtt":
        return _gtt_orders(order_id)

    # For an ID in all-mode, query both namespaces: normal order IDs and GTT IDs
    # are distinct identifier types. Keep successful results if one endpoint
    # reports that the ID does not exist in its namespace.
    records: list[dict] = []
    errors: list[str] = []
    for label, fetch in (("normal", _normal_orders), ("gtt", _gtt_orders)):
        try:
            records.extend(fetch(order_id))
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    if not records and errors:
        raise RuntimeError("No order results returned. " + " | ".join(errors))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch normal Upstox orders, GTT orders, or a specific order."
    )
    parser.add_argument(
        "--kind", choices=["all", "normal", "gtt"], default="all",
        help="Order namespace to fetch (default: all).",
    )
    parser.add_argument(
        "--order-id", help="Optional normal order ID or GTT order ID. In all-mode both APIs are checked."
    )
    parser.add_argument("--status", help="Filter by exact status, e.g. open, complete, cancelled.")
    parser.add_argument("--symbol", help="Filter by partial trading symbol or instrument key.")
    parser.add_argument("--tag", help="Filter by exact order tag (normal orders).")
    parser.add_argument("--segment", help="Filter by exchange/segment prefix, e.g. NSE_FO or NSE.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON (default is already readable).")
    args = parser.parse_args()

    try:
        records = _fetch(args.kind, args.order_id)
        records = filter_records(
            records,
            order_id=args.order_id,
            status=args.status,
            symbol=args.symbol,
            tag=args.tag,
            segment=args.segment,
        )
        result = {
            "status": "success",
            "kind": args.kind,
            "filters": {
                "order_id": args.order_id,
                "status": args.status,
                "symbol": args.symbol,
                "tag": args.tag,
                "segment": args.segment,
            },
            "count": len(records),
            "orders": records,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(f"ERROR: Could not fetch Upstox orders: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
