import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: UPSTOX_ACCESS_TOKEN is not set in environment.", file=sys.stderr)
        return 1

    url = "https://api.upstox.com/v2/portfolio/short-term-positions"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    positions = raw.get("data") or []
    total_pnl = 0.0
    rows = []

    for pos in positions:
        pnl_val = float(pos.get("pnl") or 0.0)
        total_pnl += pnl_val
        rows.append(
            {
                "symbol": pos.get("trading_symbol") or pos.get("instrument_token"),
                "instrument_token": pos.get("instrument_token"),
                "product": pos.get("product"),
                "net_quantity": pos.get("quantity"),
                "pnl": round(pnl_val, 2),
            }
        )

    summary = {
        "status": raw.get("status", "success"),
        "positions_count": len(rows),
        "total_pnl": round(total_pnl, 2),
        "positions": rows,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
