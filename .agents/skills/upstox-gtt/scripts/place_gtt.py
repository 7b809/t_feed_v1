import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Place an Upstox GTT order.")
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--entry-trigger", type=float, required=True)
    parser.add_argument("--sl-trigger", type=float, required=True)
    parser.add_argument("--target-trigger", type=float, required=True)
    parser.add_argument("--product", default="D")
    parser.add_argument("--transaction-type", default="BUY")
    args = parser.parse_args()

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: UPSTOX_ACCESS_TOKEN is not set in environment.", file=sys.stderr)
        return 1

    payload = {
        "type": "MULTIPLE",
        "quantity": args.quantity,
        "product": args.product,
        "instrument_token": args.instrument,
        "transaction_type": args.transaction_type,
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": "IMMEDIATE",
                "trigger_price": args.entry_trigger,
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": args.sl_trigger,
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": args.target_trigger,
            },
        ],
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
