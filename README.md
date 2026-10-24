<!-- README_WEBSOCKETS.md -->
# EMA Live Feed — WebSocket API Guide

Live EMA(9/21) streaming service for indexes and options. This document
explains how to connect, what events you will receive, and how to handle
reconnects and errors.

- **Production base URL (WS):** `wss://ema.novag7.in`
- **Production base URL (HTTP):** `https://ema.novag7.in`
- **Local base URL (WS):** `ws://127.0.0.1:8002`
- **Local base URL (HTTP):** `http://127.0.0.1:8002`

All timestamps in payloads are ISO-8601 with an explicit `+05:30` offset (IST).

---

## 1. Channel patterns

There are three ways to subscribe to a live feed. All of them are **WebSocket
GET** requests — no auth, no request body.

| Kind        | URL template                                     | Example                                              |
|-------------|--------------------------------------------------|------------------------------------------------------|
| Index       | `/ws/index/{underlying}`                         | `wss://ema.novag7.in/ws/index/nifty`                 |
| Options     | `/ws/ema/options/{underlying}/{contract}`        | `wss://ema.novag7.in/ws/ema/options/nifty/22500CE`   |
| Instrument  | `/ws/ema/{instrument_key}`                       | `wss://ema.novag7.in/ws/ema/NSE_FO%7C51330`          |

**Notes:**

- `{underlying}` is always **lowercased** (`nifty`, `sensex`, `banknifty`, …).
- `{contract}` is `{strike}{CE|PE}` — an underscore between strike and type
  is also accepted (`22500_CE`). Do not include expiry; the server resolves
  the current active contract.
- For the raw-instrument URL, URL-encode `|` as `%7C` and spaces as `%20`.
  For example `NSE_INDEX|Nifty 50` becomes `NSE_INDEX%7CNifty%2050`.

### Which one should I use?

- **Index** — simplest; works even if the index is not yet in
  `all_instruments.json` (a default metadata record is used).
- **Options** — resolves the active CE/PE contract from the catalog and
  **auto-subscribes** it on connect if it is not already live.
- **Instrument** — most explicit. Use when you already know the raw
  `instrument_key` from the catalog or a previous snapshot.

---

## 2. Discovering what is available

Two REST endpoints feed the WebSocket layer:

| Endpoint                                  | Purpose                                                                 |
|-------------------------------------------|-------------------------------------------------------------------------|
| `GET /api/v1/ws/catalog`                  | Full catalog: patterns, event schemas, subscribed list, available list. |
| `GET /api/v1/ws/catalog/{instrument_key}` | Single-instrument lookup with its recommended WS URLs.                  |

Useful query parameters on `/api/v1/ws/catalog`:

- `available_limit` (default 20, max 500)
- `available_offset` (for pagination)
- `underlying` (e.g. `NIFTY`)
- `strike_type` (`CE` / `PE` / `INDEX`)

Example:

```bash
curl -s "https://ema.novag7.in/api/v1/ws/catalog?underlying=NIFTY&strike_type=CE&available_limit=20"
Every item in the response contains a ws_urls object with the exact URLs
to use for that instrument. Use those directly — they are already
URL-encoded.

3. Connection handshake
Open a plain WebSocket to one of the URLs above. No headers, no token,
no subprotocol negotiation.

http
GET /ws/index/nifty HTTP/1.1
Host: ema.novag7.in
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Version: 13
Sec-WebSocket-Key: <random>
The server accepts immediately. The first frame you receive is always a
snapshot event (see below). If a cross already exists for the instrument,
a last_cross replay follows the snapshot. Live events then stream in
once per cycle (default cycle = 1 minute, aligned to :10 of the minute).

4. Event types
Every frame is a single JSON object with an "event" field. There are
exactly five event types.

4.1 snapshot — first frame, sent once per connection
Full runtime state at the moment of connection.

json
{
  "event": "snapshot",
  "instrument_key": "NSE_INDEX|Nifty 50",
  "meta": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "trading_symbol": "NIFTY",
    "underlying": "NIFTY",
    "strike_price": null,
    "strike_type": "INDEX",
    "expiry": null
  },
  "ema_fast": 22545.965722366633,
  "ema_slow": 22526.43445530132,
  "last_ts": "2026-10-23T15:29:00+05:30",
  "last_close": 22600.0,
  "ema_fast_prev": 22512.6,
  "ema_slow_prev": 22495.78,
  "last_ts_prev": "2026-10-23T15:12:00+05:30",
  "last_close_prev": 22497.85,
  "prev_snapshot_time": "2026-10-23T15:13:10+05:30",
  "cycle_time": "2026-10-23T15:14:10+05:30",
  "cycle_number": 42,
  "candle_count": 3375,
  "cross_count": 141,
  "cross_count_today": 12,
  "today_ist_date": "2026-10-23",
  "last_cross": {
    "timestamp": "2026-10-23T15:13:00+05:30",
    "type": "bullish",
    "fast_ema": 22497.41,
    "slow_ema": 22495.78,
    "close": 22512.6,
    "ema_diff": 1.63,
    "valid": true
  },
  "last_cross_time": "2026-10-23T15:13:00+05:30",
  "last_payload": null
}
Key fields:

Field	Meaning
ema_fast	Current EMA(9) value after the last closed candle.
ema_slow	Current EMA(21) value after the last closed candle.
last_ts	Timestamp of the candle that produced these EMAs.
last_close	Close price of that candle.
ema_fast_prev	EMA(9) from the previous candle (useful for arrow/diff rendering).
candle_count	Number of historical candles loaded for this instrument.
cross_count	Total crossovers detected since the archive loaded.
cross_count_today	Crossovers detected today (IST).
last_cross	Full object for the most recent crossover, or null.
4.2 last_cross — replay frame, sent once after snapshot (conditional)
Only sent if last_cross is non-null in the snapshot. A compact form of the
same data, convenient for UI badges.

json
{
  "event": "last_cross",
  "instrument_key": "NSE_INDEX|Nifty 50",
  "trading_symbol": "NIFTY",
  "close": 22512.6,
  "ema9": 22497.4108,
  "ema21": 22495.7792,
  "last_cross_time": "2026-10-23T15:13:00+05:30"
}
4.3 heartbeat — once per cycle
Emitted after the cycle has finished processing every instrument. This
is your "the feed is alive and the minute is complete" signal. It carries
enough state to reconstruct the same numbers the dashboard shows.

json
{
  "event": "heartbeat",
  "cycle": 8,
  "cycle_started_at": "2026-10-24T12:54:10.000603+05:30",
  "cycle_completed_at": "2026-10-24T12:54:10.031913+05:30",
  "duration_seconds": 0.03,
  "instrument_key": "NSE_INDEX|Nifty 50",
  "trading_symbol": "NIFTY",
  "status": "ok",
  "error": null,
  "ema9": 22545.9657,
  "ema21": 22526.4345,
  "last_close": 22600.0,
  "last_ts": "2026-10-23T15:29:00+05:30",
  "last_cross_time": "2026-10-23T15:13:00+05:30"
}
status is "ok" on success; on failure it is "failed" or "timeout"
and the error field will be populated.

4.4 cross — live crossover
Emitted inside the cycle that processes the closing candle in which a
9/21 crossover occurred. One per crossover.

json
{
  "event": "cross",
  "instrument_key": "NSE_FO|51330",
  "trading_symbol": "NIFTY 22500 CE 27 OCT 26",
  "close": 190.0,
  "ema9": 189.24,
  "ema21": 182.99,
  "last_cross_time": "2026-10-23T15:12:00+05:30"
}
4.5 no_cross — cycle with no crossover
Emitted when a cycle completes with no crossover. Fires once per candle per
subscription. Usually suppressed by the logger unless --all-events is on.

json
{
  "event": "no_cross",
  "instrument_key": "NSE_FO|51330",
  "trading_symbol": "NIFTY 22500 CE 27 OCT 26",
  "last_cross_time": "2026-10-23T15:12:00+05:30"
}
5. Message sequence per connection
text
connect
  └─ snapshot           (always, exactly once)
  └─ last_cross         (only if a cross exists)
  └─ heartbeat          (once per cycle, ~every 60s)
  └─ cross | no_cross   (when a cycle processes a candle)
  └─ …
There are no other frame types. Unknown event values must be ignored
forward-compatibly.

6. Close codes
Code	Name	Meaning
1000	Normal Closure	Client disconnected normally.
1001	Going Away	Server shutting down (restart/deploy). Reconnect with backoff.
1011	Internal Error	Unexpected server-side error. Reconnect with backoff.
4400	Bad Request	Invalid options contract format (e.g. 22500XZ). Do not retry.
4404	Not Found	Instrument not in catalog and auto-subscribe failed. Do not retry.
Note: FastAPI turns pre-accept() rejections into HTTP 403 during the
handshake, which most WebSocket clients surface as an InvalidStatus
exception. The Python client below handles both.

7. Client behaviour
Keepalive
Send any text frame (the string "ping" is fine) every 30 seconds.
The server reads via receive_text() and ignores content. This prevents
NAT/proxy idle timeouts.

Reconnect
Use exponential backoff capped at 30 seconds:

text
1s → 2s → 4s → 8s → 16s → 30s → 30s → …
But: never retry on 4400 or 4404. These are deterministic failures —
fix the contract format or pick a different instrument.

On 1001 / 1011 / network errors, reset the backoff and reconnect.

8. Client examples
Python (websockets)
python
import asyncio
import json

import websockets


async def consume(url: str):
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                print(f"connected: {url}")
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    event = msg.get("event")
                    if event == "snapshot":
                        print("SNAPSHOT", msg["instrument_key"],
                              msg["ema_fast"], msg["ema_slow"])
                    elif event == "heartbeat":
                        print(f"HEARTBEAT cycle={msg['cycle']} "
                              f"ema9={msg['ema9']:.2f} ema21={msg['ema21']:.2f}")
                    elif event == "cross":
                        print(f"CROSS {msg['trading_symbol']} "
                              f"close={msg['close']} ema9={msg['ema9']} ema21={msg['ema21']}")
                    elif event == "last_cross":
                        print("LAST_CROSS", msg["last_cross_time"])

        except websockets.exceptions.ConnectionClosed as exc:
            if exc.code in (4400, 4404):
                print(f"Permanent rejection ({exc.code}): {exc.reason}")
                return
            print(f"Connection closed ({exc.code}), retrying in {backoff:.1f}s")
        except (OSError, websockets.exceptions.InvalidStatus) as exc:
            print(f"Transport error ({exc}), retrying in {backoff:.1f}s")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    asyncio.run(consume("wss://ema.novag7.in/ws/index/nifty"))
JavaScript (browser)
js
function connect(url) {
  const ws = new WebSocket(url);
  let backoff = 1000;

  ws.onopen = () => {
    backoff = 1000;
    setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 30000);
  };

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    switch (msg.event) {
      case "snapshot":
        console.log("snapshot", msg.instrument_key, msg.ema_fast, msg.ema_slow);
        break;
      case "last_cross":
        console.log("last cross at", msg.last_cross_time);
        break;
      case "heartbeat":
        console.log(`heartbeat cycle=${msg.cycle} ema9=${msg.ema9} ema21=${msg.ema21}`);
        break;
      case "cross":
        console.log(`cross ${msg.trading_symbol} close=${msg.close}`);
        break;
      case "no_cross":
        // usually ignore
        break;
    }
  };

  ws.onclose = (evt) => {
    if (evt.code === 4400 || evt.code === 4404) {
      console.warn("Permanent rejection:", evt.code, evt.reason);
      return;
    }
    setTimeout(() => connect(url), backoff);
    backoff = Math.min(backoff * 2, 30000);
  };

  ws.onerror = (err) => console.error("ws error", err);
}

connect("wss://ema.novag7.in/ws/index/nifty");
CLI (websocat)
bash
websocat wss://ema.novag7.in/ws/index/nifty
Python logger (scripts/ws_logger.py)
The repo ships a ready-to-use multi-subscription logger that writes one log
file per subscription:

bash
# Local feed, built-in TARGETS from the top of the script
python scripts/ws_logger.py

# Production feed, follow everything the server already streams
python scripts/ws_logger.py --env prod --from-subscribed

# Production feed, only NIFTY CE options in the catalog
python scripts/ws_logger.py --env prod --from-catalog \
    --catalog-underlying NIFTY --catalog-strike-type CE

# Explicit targets
python scripts/ws_logger.py --env prod \
    --index NIFTY --option NIFTY:22500:PE --instrument "NSE_FO|51330"
Log files land in ./logs/ (or --log-dir). Each line is one event:

text
2026-10-24 12:53:38 INFO    Subscription: index/nifty
2026-10-24 12:53:38 INFO    URL:          wss://ema.novag7.in/ws/index/nifty
2026-10-24 12:53:38 INFO    ===== connected url=wss://ema.novag7.in/ws/index/nifty =====
2026-10-24 12:53:38 INFO    [SNAPSHOT] NSE_INDEX|Nifty 50 (NIFTY) ema9=22545.965722366633 ema21=22526.43445530132 candles=3375 crosses=141 last_close=22600.0 last_cross_time=2026-10-23T15:13:00+05:30
2026-10-24 12:53:38 INFO    [LAST_CROSS] NSE_INDEX|Nifty 50 (NIFTY) close=22512.6 ema9=22497.4108 ema21=22495.7792 last_cross_time=2026-10-23T15:13:00+05:30
2026-10-24 12:54:10 INFO    [HEARTBEAT] cycle=8 duration=0.03s NSE_INDEX|Nifty 50 (NIFTY) ema9=22545.9657 ema21=22526.4345 close=22600.0000 last_cross_time=2026-10-23T15:13:00+05:30 ...
You can browse these logs in the web UI at /ui/logs, which reads from the
JSON API at GET /api/v1/logs and GET /api/v1/logs/{filename}.

9. Common pitfalls
4404 Not Found on options — the strike you requested does not exist
in all_instruments.json. Refresh the catalog first:

bash
curl -X POST https://ema.novag7.in/api/v1/jobs/mega-archive/hard-refresh \
     -H "X-API-Key: $API_KEY"
Then re-list /api/v1/ws/catalog?underlying=NIFTY&strike_type=CE and
pick a strike that is actually in the response.

Lowercase underlying — /ws/index/NIFTY returns 404. Use
/ws/index/nifty.

| in the raw instrument URL — you must URL-encode it as %7C.
Do not pass the literal pipe in the path.

ema_fast / ema_slow are float — they can be large. Round on
display (toFixed(2)), don't round the wire values.

Heartbeat is cycle-level, not per-instrument — one heartbeat frame
per subscription per cycle, sent after the cycle completes. If you
need to know "how long did this cycle take", read duration_seconds.

cross_count_today resets at 00:00 IST — use today_ist_date to
detect the rollover if you cache.

last_cross is a replay, not a live event — treat it as a hint for
the UI badge. Live crossovers arrive as event: "cross".

10. End-to-end quickstart
Discover instruments in production:

bash
curl -s "https://ema.novag7.in/api/v1/ws/catalog?available_limit=5" | jq
Subscribe to a NIFTY CE option (this auto-adds it to the live feed):

bash
curl -X POST "https://ema.novag7.in/api/v1/ema/subscribe/NSE_FO%7C51330"
Connect to the options WebSocket:

bash
websocat "wss://ema.novag7.in/ws/ema/options/nifty/22500CE"
You will see a snapshot, optionally a last_cross, then a
heartbeat approximately every 60 s.

Or run the logger to capture everything to files:

bash
python scripts/ws_logger.py --env prod --from-subscribed
Browse the logs at https://ema.novag7.in/ui/logs.

11. Reference
Catalog: GET /api/v1/ws/catalog

Subscribe (REST prewarm): POST /api/v1/ema/subscribe/{instrument_key}

Snapshot (REST): GET /api/v1/ema/{instrument_key}/latest

Status (all subscriptions): GET /api/v1/ema/status

Live channels + client counts: GET /api/v1/ema/channels

Logs (JSON): GET /api/v1/logs, GET /api/v1/logs/{filename}

Logs (UI): GET /ui/logs

Hard-refresh archive (with API key): POST /api/v1/jobs/mega-archive/hard-refresh

text

### What this README covers and why

- **Three URL patterns** — index, options, raw instrument — with the exact
  casing and encoding rules the server enforces, so clients don't get a 404
  on the first try.
- **All five event types** — with the fields you actually need for a UI
  (arrow direction, diff, cross badge, cycle duration). The `snapshot` payload
  is copied from your catalog JSON, and the `heartbeat`/`last_cross` samples
  are taken from the log you provided so the field names match production.
- **Message sequence** — snapshot → optional last_cross → heartbeat → cross/no_cross —
  so integrators know what to expect on connect and don't dismiss the first
  frame as noise.
- **Close codes 4400/4404** — explicitly called out as "do not retry"
  deterministic failures, with the actionable fix (refresh catalog / fix
  contract format).
- **Copy-paste clients** — Python `websockets`, browser JS, `websocat`, and
  your existing `ws_logger.py --env prod ...` all working against the same
  server, so people can pick the one that matches their environment.
- **Quickstart** — a five-step discover → subscribe → connect → log → browse
  path that ties all the endpoints (`/ws/catalog`, `/api/v1/ema/subscribe`,
  the WS URLs, `/ui/logs`) together.