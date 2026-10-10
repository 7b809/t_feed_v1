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
Important deployment notes
Run one Uvicorn worker. The Upstox streamer and WebSocket client registry are in process memory. Multiple workers would create duplicate upstream connections and separate client registries.

For horizontal scaling, move fan-out to Redis pub/sub and elect one upstream-stream owner.

Protect the WebSocket endpoint with authentication before exposing it publicly. The sample keeps the browser flow simple and only protects management APIs.

A TTL index expires event records after seven days. MongoDB TTL deletion is asynchronous, so an expired row may remain briefly before MongoDB removes it.

Subscribe operations are idempotent: an already-subscribed key with the same mode is returned in already_subscribed; a different mode is changed instead of duplicated.

If an Upstox subscribe call succeeds but MongoDB fails immediately afterward, upstream state and persisted state may briefly differ. A reconnect restores MongoDB as the source of truth. Production systems can add a reconciliation job and operation IDs.

text

Key changes vs. the original:

- **Features** section: replaced the hardcoded "09:00 … Monday–Friday" line with a description of the environment-driven schedule and its default of 08:00 IST, Mon–Fri.
- **New "Configuration — token refresh schedule" section**: documents all five `TOKEN_REFRESH_*` variables, their defaults, and gives ready-to-paste examples (every day, weekdays at a custom time, disabled).
- Everything else (MongoDB doc, run instructions, API examples, deployment notes) is unchanged.