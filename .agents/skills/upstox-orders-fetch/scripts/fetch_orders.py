"""Fetch Upstox normal/GTT orders and emit a complete JSON contract."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[2]
UPSTOX_SCRIPTS = SKILL_ROOT / "upstox" / "scripts"
sys.path.insert(0, str(UPSTOX_SCRIPTS))

from upstox_helpers import order_api, order_api_v3  # noqa: E402
from order_filters import as_records, filter_records, normalize_record, response_data, to_plain  # noqa: E402


def _fetch_normal(order_id: str | None) -> tuple[list[dict], Any]:
    api = order_api()
    response = (api.get_order_details(api_version="2.0", order_id=order_id)
                if order_id else api.get_order_book(api_version="2.0"))
    payload = response_data(response)
    return [normalize_record(item, "normal") for item in as_records(payload)], to_plain(response)


def _fetch_gtt(order_id: str | None) -> tuple[list[dict], Any]:
    api = order_api_v3()
    response = (api.get_gtt_order_details(gtt_order_id=order_id)
                if order_id else api.get_gtt_order_details())
    payload = response_data(response)
    return [normalize_record(item, "gtt") for item in as_records(payload)], to_plain(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Upstox normal and GTT orders.")
    parser.add_argument("--kind", choices=["all", "normal", "gtt"], default="all")
    parser.add_argument("--order-id")
    parser.add_argument("--status")
    parser.add_argument("--symbol")
    parser.add_argument("--tag")
    parser.add_argument("--segment")
    args = parser.parse_args()

    records: list[dict] = []
    raw: dict[str, Any] = {}
    errors: list[dict] = []
    fetchers = (("normal", _fetch_normal), ("gtt", _fetch_gtt))
    selected = [item for item in fetchers if args.kind == "all" or item[0] == args.kind]
    for label, fetcher in selected:
        try:
            items, response = fetcher(args.order_id)
            records.extend(items)
            raw[label] = response
        except Exception as exc:
            errors.append({"source": label, "type": type(exc).__name__, "message": str(exc)})

    records = filter_records(records, order_id=args.order_id, status=args.status,
                             symbol=args.symbol, tag=args.tag, segment=args.segment)
    if errors and not records and len(errors) == len(selected):
        status = "error"
    elif errors:
        status = "partial"
    else:
        status = "success"
    result = {
        "status": status,
        "kind": args.kind,
        "filters": {"order_id": args.order_id, "status": args.status,
                    "symbol": args.symbol, "tag": args.tag, "segment": args.segment},
        "count": len(records),
        "orders": records,
        "raw_response": raw,
        "errors": errors,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
