import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Place Upstox GTT orders across IMMEDIATE or ABOVE/BELOW entry and SL/Target leg combinations."
    )
    parser.add_argument("--instrument", required=True, help="e.g. NSE_FO|64473")
    parser.add_argument("--quantity", type=int, required=True, help="Lot-aligned quantity, e.g. 65")
    parser.add_argument(
        "--entry-trigger-type",
        choices=["IMMEDIATE", "ABOVE", "BELOW"],
        required=True,
        help="IMMEDIATE for live/immediate trigger, or ABOVE/BELOW for price-level trigger",
    )
    parser.add_argument(
        "--entry-price",
        type=float,
        default=None,
        help="Entry trigger price (required for ABOVE/BELOW; optional reference/trigger for IMMEDIATE)",
    )
    parser.add_argument(
        "--sl-price",
        type=float,
        default=None,
        help="Optional stop-loss trigger price",
    )
    parser.add_argument(
        "--target-price",
        type=float,
        default=None,
        help="Optional target trigger price",
    )
    parser.add_argument(
        "--product",
        choices=["I", "D", "MTF"],
        default="D",
    )
    parser.add_argument(
        "--transaction-type",
        choices=["BUY", "SELL"],
        default="BUY",
    )
    args = parser.parse_args()

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: UPSTOX_ACCESS_TOKEN is not set in environment.", file=sys.stderr)
        return 1

    if args.quantity <= 0:
        print("ERROR: --quantity must be greater than 0.", file=sys.stderr)
        return 1

    if args.entry_trigger_type in ("ABOVE", "BELOW") and (args.entry_price is None or args.entry_price <= 0):
        print("ERROR: --entry-price (> 0) is required when --entry-trigger-type is ABOVE or BELOW.", file=sys.stderr)
        return 1

    if args.entry_trigger_type == "IMMEDIATE" and (args.entry_price is None or args.entry_price <= 0):
        print("ERROR: --entry-price (> 0) is required for ENTRY trigger_price.", file=sys.stderr)
        return 1

    exit_tx = "SELL" if args.transaction_type == "BUY" else "BUY"

    rules = [
        {
            "strategy": "ENTRY",
            "trigger_type": args.entry_trigger_type,
            "trigger_price": round_to_tick(args.entry_price, 0.05),
            "transaction_type": args.transaction_type,
        }
    ]

    if args.sl_price is not None:
        if args.sl_price <= 0:
            print("ERROR: --sl-price must be > 0.", file=sys.stderr)
            return 1
        rules.append(
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round_to_tick(args.sl_price, 0.05),
                "transaction_type": exit_tx,
            }
        )

    if args.target_price is not None:
        if args.target_price <= 0:
            print("ERROR: --target-price must be > 0.", file=sys.stderr)
            return 1
        rules.append(
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round_to_tick(args.target_price, 0.05),
                "transaction_type": exit_tx,
            }
        )

    gtt_type = "SINGLE" if len(rules) == 1 else "MULTIPLE"

    payload = {
        "type": gtt_type,
        "quantity": args.quantity,
        "product": args.product,
        "instrument_token": args.instrument,
        "transaction_type": args.transaction_type,
        "rules": rules,
    }

    req = urllib.request.Request(
        "https://api.upstox.com/v3/order/gtt/place",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(resp.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
