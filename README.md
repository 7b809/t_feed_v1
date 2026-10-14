# Upstox FastAPI Market Stream Gateway

A FastAPI service that keeps one Upstox MarketDataStreamerV3 connection, persists active subscriptions in MongoDB, and republishes each instrument feed through your own WebSocket endpoint.

## Features

- Loads the access token from `UPSTOX_APP.upstox_tokens` document `_id=upstox_access_token` at startup.
- Reloads the token and reconnects on a configurable schedule. Default is **08:00 Asia/Kolkata, Monday–Friday**, and the schedule is fully environment-driven.
- Manual hard refresh API.
- Subscribe, unsubscribe, change mode, list active subscriptions, and retain seven-day subscription event history.
- Restores active subscriptions after restart or token refresh.
- Proxies the Upstox Instrument Search API.
- Browser UI in `app/templates/index.html`.
- One client WebSocket route per selected instrument via query parameter.
- **Interval-based OHLCVOI candle stream** on the same WebSocket route (see below).

## MongoDB token document

```json
{
  "_id": "upstox_access_token",
  "access_token": "YOUR_TOKEN"
}
The application only reads this document. Your existing Telegram/token process remains responsible for updating it.

Run locally
bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
Open http://localhost:8000 and enter the APP_API_KEY value.

Docker
bash
cp .env.example .env
docker compose up --build
When using Docker Compose, set MONGODB_URI=mongodb://mongodb:27017 in .env.

Configuration — token refresh schedule
The scheduled token reload and streamer reconnect are fully environment-driven, so the time and cadence can be tuned without touching code.

Variable	Default	Description
TOKEN_REFRESH_ENABLED	true	Master switch for the scheduled refresh. Set to false to disable the scheduler job entirely.
TOKEN_REFRESH_DAY_OF_WEEK	mon-fri	APScheduler day_of_week expression. Use * for every day, mon-fri for weekdays, or a list like mon,wed,fri.
TOKEN_REFRESH_HOUR	8	24-hour clock (0–23).
TOKEN_REFRESH_MINUTE	0	Minute of the hour (0–59).
TOKEN_REFRESH_TIMEZONE	Asia/Kolkata	IANA timezone name used by the cron trigger.
Examples:

env
# Every day at 08:00 IST (default behaviour)
TOKEN_REFRESH_ENABLED=true
TOKEN_REFRESH_DAY_OF_WEEK=*
TOKEN_REFRESH_HOUR=8
TOKEN_REFRESH_MINUTE=0
TOKEN_REFRESH_TIMEZONE=Asia/Kolkata
env
# Weekdays at 07:45 IST
TOKEN_REFRESH_DAY_OF_WEEK=mon-fri
TOKEN_REFRESH_HOUR=7
TOKEN_REFRESH_MINUTE=45
env
# Disable the scheduled refresh entirely (manual endpoint still works)
TOKEN_REFRESH_ENABLED=false
Notes:

Invalid values for TOKEN_REFRESH_HOUR, TOKEN_REFRESH_MINUTE, or TOKEN_REFRESH_TIMEZONE cause a fast startup failure with a clear error message.

The scheduler's own timezone is taken from TOKEN_REFRESH_TIMEZONE, so the job fires in the intended local time regardless of the host OS timezone.

The manual refresh endpoint POST /api/token/hard-refresh is always available, even when the scheduled job is disabled.

Candle / interval feed
The `/ws/market` endpoint accepts an optional `interval` query parameter. When it is present, the server streams OHLCVOI candles on that interval to the client.

Allowed intervals (minutes): **1, 3, 5, 15, 30, 60**.

Example:
text
# Raw full-feed ticks (default, backwards-compatible)
ws://localhost:8001/ws/market?instrument_key=NSE_FO%7C47142

# 5-minute candles for the same instrument
ws://localhost:8001/ws/market?instrument_key=NSE_FO%7C47142&interval=5
Each message on an interval connection is a JSON array:

json
[
  "2026-08-12T15:39:00+05:30",
  61.0,
  61.1,
  60.3,
  60.3,
  157040,
  7173530
]
Fields: `[timestamp_ist_iso8601, open, high, low, close, volume, oi]`.

### How candles are built

The server does **not** reconstruct candles by accumulating individual LTP / VTT / OI values from the tick stream. Instead it uses the **official 1-minute candle** that Upstox itself embeds in every `full`-mode tick, inside `marketOHLC.ohlc`, tagged with `"interval": "I1"`. That entry is the exchange-derived 1-minute OHLC for the instrument — the same numbers you see on the Upstox terminal, with the same volume and (for tradable instruments) the same open interest.

Aggregation rules:

- On every tick the server inspects the `I1` entry's `ts` field (epoch milliseconds).
- When `I1.ts` changes, the previous minute is considered **fully closed**, and that closed `I1` is merged into the running state for every interval a client is subscribed to:
  - `interval=1` → the closed `I1` **is** the candle; it is emitted immediately, verbatim.
  - `interval=3/5/15/30/60` → the closed `I1` is merged into the current interval bucket: `open` from the first I1 in the bucket, `high` = max, `low` = min, `close` from the last I1, `volume` = sum of the 1-minute volumes, `oi` from the last I1. The bucket is emitted only when a later I1 crosses the interval boundary.
- A gap of more than 5 minutes between two consecutive `I1` timestamps is treated as a session break or outage: the stale minute is dropped silently and a fresh state is started from the new `I1`.

Because candles are derived from Upstox's own `I1` feed, their OHLCVOI values match the exchange's official candles exactly — including volume on instruments where `vtt` was previously unavailable.

Notes:

- Timestamps are always IST (`Asia/Kolkata`) and aligned to the start of the interval bucket. For `interval=60`, buckets start on hour boundaries.
- **Completed candles only.** The in-progress interval candle is held server-side and emitted exactly once, at the moment the next interval bucket begins. Clients therefore receive one message per interval per instrument (one per minute on `interval=1`, one every three minutes on `interval=3`, one every five minutes on `interval=5`, etc.). The current, partially formed candle is never pushed.
- Index instruments (`NSE_INDEX|Nifty 50`, `BSE_INDEX|SENSEX`) have no open interest; the `oi` field is emitted as `0`. Volume is still reported correctly via the `I1` entry.
- Multiple clients may connect to the same instrument with **different intervals** at the same time — each `(instrument_key, interval)` pair is tracked independently.
- When a client connects with an interval, the server **auto-subscribes** the instrument upstream in `full` mode if it is not already subscribed. `I1` candles are only available in `full` / `full_d30` mode; if the instrument is currently subscribed in `ltpc` or `option_greeks`, the server will not receive `marketOHLC` and no candle will be emitted until the subscription mode is upgraded.
- Because a candle is only emitted when the next bucket starts, the very last candle of a trading session is not flushed when the market closes. It will be emitted as soon as the first `I1` of the next session arrives, provided the gap is under 5 minutes — which it will not be across an overnight break, so the final minute of the day is intentionally dropped.
- Invalid intervals (non-integer, or not in {1,3,5,15,30,60}) cause the WebSocket to be closed immediately with code `1008`.

API
All management endpoints require X-API-Key.

bash
curl -X POST http://localhost:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"full"}'
bash
curl -X PATCH http://localhost:8000/api/subscriptions/mode \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"ltpc"}'
bash
curl -X DELETE http://localhost:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"full"}'
bash
curl -H 'X-API-Key: change-me' http://localhost:8000/api/subscriptions
curl -X POST -H 'X-API-Key: change-me' http://localhost:8000/api/token/hard-refresh
Client WebSocket:

text
ws://localhost:8000/ws/market?instrument_key=NSE_INDEX%7CNifty%2050
ws://localhost:8000/ws/market?instrument_key=NSE_INDEX%7CNifty%2050&interval=5
Important deployment notes
Run one Uvicorn worker. The Upstox streamer and WebSocket client registry are in process memory. Multiple workers would create duplicate upstream connections and separate client registries.

For horizontal scaling, move fan-out to Redis pub/sub and elect one upstream-stream owner.

Protect the WebSocket endpoint with authentication before exposing it publicly. The sample keeps the browser flow simple and only protects management APIs.

A TTL index expires event records after seven days. MongoDB TTL deletion is asynchronous, so an expired row may remain briefly before MongoDB removes it.

Subscribe operations are idempotent: an already-subscribed key with the same mode is returned in already_subscribed; a different mode is changed instead of duplicated.

If an Upstox subscribe call succeeds but MongoDB fails immediately afterward, upstream state and persisted state may briefly differ. A reconnect restores MongoDB as the source of truth. Production systems can add a reconciliation job and operation IDs.