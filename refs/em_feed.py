"""
EMA Live Feed WebSocket client.

Deployed URL : https://domain-url.in
WS base URL  : wss://domain-url.in          (https -> wss, keep the same host)

Install:
    pip install websockets
"""

import asyncio
import json
import urllib.parse

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


# ---------------------------------------------------------------------------
# 1. URL builders — mirror the three channel patterns from README §1
# ---------------------------------------------------------------------------

WS_BASE = "wss://domain-url.in"     # derived from https://domain-url.in


def index_url(underlying: str) -> str:
    """Index channel.  underlying MUST be lowercase (e.g. 'nifty', 'sensex')."""
    return f"{WS_BASE}/ws/index/{underlying.lower()}"


def option_url(underlying: str, strike: int, opt_type: str) -> str:
    """
    Options channel. Contract = '<strike><CE|PE>' or '<strike>_<CE|PE>'.
    Do NOT include expiry — server resolves the current active contract.
    Auto-subscribes the instrument if it isn't already live.
    """
    contract = f"{strike}{opt_type.upper()}"
    return f"{WS_BASE}/ws/ema/options/{underlying.lower()}/{contract}"


def instrument_url(instrument_key: str) -> str:
    """
    Raw instrument channel.  instrument_key comes from the catalog,
    e.g. 'NSE_FO|51330' or 'NSE_INDEX|Nifty 50'.
    The '|' and spaces MUST be URL-encoded.
    """
    encoded = urllib.parse.quote(instrument_key, safe="")
    return f"{WS_BASE}/ws/ema/{encoded}"


# ---------------------------------------------------------------------------
# 2. Event handlers — one per event type documented in README §4
#    Everything arrives as a single JSON object with an "event" field.
# ---------------------------------------------------------------------------

def handle_snapshot(msg: dict) -> None:
    """
    First frame, sent exactly once per connection.  Full runtime state.
    Contains:
        ema_fast / ema_slow        -> current EMA(9) and EMA(21)
        last_ts / last_close       -> candle that produced those EMAs
        ema_fast_prev / ema_slow_prev, last_ts_prev, last_close_prev
        candle_count, cross_count, cross_count_today
        last_cross (object or None), last_cross_time
        meta (instrument_key, trading_symbol, underlying, strike_type, expiry)
    """
    print("\n=== SNAPSHOT ===")
    print(f"  instrument_key : {msg['instrument_key']}")
    print(f"  trading_symbol : {msg['meta']['trading_symbol']}")
    print(f"  ema9           : {msg['ema_fast']:.4f}")
    print(f"  ema21          : {msg['ema_slow']:.4f}")
    print(f"  last_close     : {msg['last_close']}")
    print(f"  last_ts        : {msg['last_ts']}")
    print(f"  candles        : {msg['candle_count']}")
    print(f"  crosses_total  : {msg['cross_count']}  today: {msg['cross_count_today']}")


def handle_last_cross(msg: dict) -> None:
    """
    Replay frame — sent once, only if snapshot.last_cross is not null.
    A compact badge form: close / ema9 / ema21 / last_cross_time.
    This is NOT a live event; live ones arrive as event == "cross".
    """
    print("\n=== LAST_CROSS (replay) ===")
    print(f"  {msg['trading_symbol']}  close={msg['close']}  "
          f"ema9={msg['ema9']}  ema21={msg['ema21']}  at {msg['last_cross_time']}")


def handle_heartbeat(msg: dict) -> None:
    """
    Once per cycle (~every 60 s, aligned to :10 of the minute), sent AFTER
    the cycle has finished processing every instrument.
    status = "ok" | "failed" | "timeout"; on failure `error` is populated.
    Also carries cycle, cycle_started_at, cycle_completed_at, duration_seconds.
    """
    status = msg["status"]
    if status == "ok":
        print(f"[heartbeat] cycle={msg['cycle']} "
              f"dur={msg['duration_seconds']:.2f}s  "
              f"ema9={msg['ema9']:.4f} ema21={msg['ema21']:.4f}")
    else:
        print(f"[heartbeat] cycle={msg['cycle']} status={status} "
              f"error={msg.get('error')}")


def handle_cross(msg: dict) -> None:
    """
    Live crossover — fires inside the cycle that processes the closing
    candle where 9/21 crossed.  One per crossover.
    """
    print(f"\n*** CROSS *** {msg['trading_symbol']}  "
          f"close={msg['close']}  ema9={msg['ema9']}  ema21={msg['ema21']}  "
          f"at {msg['last_cross_time']}")


def handle_no_cross(msg: dict) -> None:
    """Cycle completed with no crossover.  Usually just log/ignore."""
    print(f"[no_cross] {msg['trading_symbol']}  last_cross={msg['last_cross_time']}")


EVENT_HANDLERS = {
    "snapshot":   handle_snapshot,
    "last_cross": handle_last_cross,
    "heartbeat":  handle_heartbeat,
    "cross":      handle_cross,
    "no_cross":   handle_no_cross,
}


# ---------------------------------------------------------------------------
# 3. Consumer — reconnects with exponential backoff (README §7)
#    Never retries on 4400 (bad contract) or 4404 (not found).
# ---------------------------------------------------------------------------

async def consume(url: str) -> None:
    backoff = 1.0

    while True:
        try:
            # ping_interval/ping_timeout are client-level WS pings.
            # The README also recommends sending the text frame "ping"
            # every 30 s (server ignores content).  Both is fine.
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20
            ) as ws:
                print(f"\n>>> connected: {url}")
                backoff = 1.0

                # Background keepalive: README §7 says send any text frame
                # ("ping" is fine) every 30 s to defeat NAT/proxy idle timeouts.
                async def keepalive():
                    while True:
                        await asyncio.sleep(30)
                        try:
                            await ws.send("ping")
                        except Exception:
                            return

                ka = asyncio.create_task(keepalive())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        event = msg.get("event")
                        handler = EVENT_HANDLERS.get(event)
                        if handler:
                            handler(msg)
                        # Unknown event types: ignore (forward-compatible)
                finally:
                    ka.cancel()

        except ConnectionClosed as exc:
            # Deterministic rejections — do NOT retry.
            if exc.code in (4400, 4404):
                print(f"\nPermanent rejection ({exc.code}): {exc.reason}")
                print("  -> 4400 = bad contract format (e.g. 22500XZ)")
                print("  -> 4404 = instrument not in catalog; refresh catalog")
                return
            # 1001 (Going Away / deploy), 1011 (internal), network drops:
            # exponential backoff, capped at 30 s.
            print(f"\nConnection closed ({exc.code} {exc.reason}), "
                  f"retrying in {backoff:.1f}s")

        except InvalidStatus as exc:
            # FastAPI converts pre-accept rejections (4400/4404) into an
            # HTTP 403 during the handshake -> surfaces as InvalidStatus.
            print(f"\nHandshake rejected (HTTP {exc.response.status_code}): {url}")
            print("  -> 403 usually means 4400/4404: fix the contract or "
                  "refresh /api/v1/ws/catalog first.")
            return

        except OSError as exc:
            print(f"\nTransport error ({exc}), retrying in {backoff:.1f}s")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)   # 1 → 2 → 4 → 8 → 16 → 30 → 30 ...


# ---------------------------------------------------------------------------
# 4. Example entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ---- pick ONE of these URLs ----

    # Index (simplest, lowercase underlying)
    url = index_url("nifty")

    # Options (active contract auto-resolved, auto-subscribed if needed)
    # url = option_url("nifty", 22500, "CE")

    # Raw instrument (most explicit; use a key from /api/v1/ws/catalog)
    # url = instrument_url("NSE_FO|51330")

    print(f"Connecting to {url}")
    asyncio.run(consume(url))