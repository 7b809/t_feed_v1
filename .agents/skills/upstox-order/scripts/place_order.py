import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Place a MARKET or LIMIT order on Upstox.")
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--order-type", choices=["MARKET", "LIMIT"], required=True)
    parser.add_argument("--price", type=float, default=0.0)
    parser.add_argument("--transaction-type", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--product", choices=["I", "D"], default="D")
    parser.add_argument("--validity", choices=["DAY", "IOC"], default="DAY")
    args = parser.parse_args()

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: UPSTOX_ACCESS_TOKEN is not set in environment.", file=sys.stderr)
        return 1

    if args.quantity <= 0:
        print("ERROR: --quantity must be greater than 0.", file=sys.stderr)
        return 1

    limit_price = 0.0
    if args.order_type == "LIMIT":
        if args.price <= 0:
            print("ERROR: --price must be > 0 for LIMIT orders.", file=sys.stderr)
            return 1
        limit_price = round_to_tick(args.price, 0.05)

    payload = {
        "quantity": args.quantity,
        "product": args.product,
        "validity": args.validity,
        "price": limit_price if args.order_type == "LIMIT" else 0,
        "tag": "algoapp_order",
        "instrument_token": args.instrument,
        "order_type": args.order_type,
        "transaction_type": args.transaction_type,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }

    req = urllib.request.Request(
        "https://api.upstox.com/v2/order/place",
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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
