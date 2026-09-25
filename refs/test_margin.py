"""
Standalone manual test for margin_service.

Run from the PROJECT ROOT:

    cd D:\\files\\temp_ticks\\t_feed_v1-temp
    python temp\\test_margin.py

Do NOT run this from inside temp\\ — imports will fail.
"""

from __future__ import annotations

import json
import sys

# Make sure the project root is on sys.path so that
# "upstox_services" and "services" are importable, even if the
# script is launched from a different working directory.
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def main() -> None:
    # Import AFTER sys.path is fixed.
    from upstox_services.margin_service import margin_service

    instrument_key = "NSE_FO|65899"
    quantity = 65
    product = "D"
    transaction_type = "BUY"

    print("=" * 70)
    print("Margin Service Manual Test")
    print("=" * 70)
    print(f"instrument_key   : {instrument_key}")
    print(f"quantity         : {quantity}")
    print(f"product          : {product}")
    print(f"transaction_type : {transaction_type}")
    print("-" * 70)

    result = margin_service.calculate_margin(
        instrument_key=instrument_key,
        quantity=quantity,
        product=product,
        transaction_type=transaction_type,
    )

    print(json.dumps(result, indent=2, default=str))
    print("-" * 70)

    if result.get("success"):
        print("RESULT : SUCCESS")
        print(f"required_margin  : {result.get('required_margin')}")
        print(f"available_margin : {result.get('available_margin')}")
    else:
        print("RESULT : FAILURE")
        print(f"error            : {result.get('error')}")

    print("=" * 70)


if __name__ == "__main__":
    main()