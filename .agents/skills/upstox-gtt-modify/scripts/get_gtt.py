"""Fetch and display one Upstox GTT order without exposing credentials."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
UPSTOX_SCRIPTS = SKILL_ROOT / "upstox" / "scripts"
sys.path.insert(0, str(UPSTOX_SCRIPTS))

from upstox_helpers import order_api_v3  # noqa: E402
from validate_gtt_modify import to_plain, unwrap_order  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch one Upstox GTT order.")
    parser.add_argument("--gtt-order-id", required=True, help="Existing Upstox GTT order ID")
    args = parser.parse_args()

    try:
        response = order_api_v3().get_gtt_order_details(gtt_order_id=args.gtt_order_id)
        order = unwrap_order(response, args.gtt_order_id)
        print(json.dumps(order, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"ERROR: Could not fetch GTT order: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
