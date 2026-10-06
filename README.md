# Upstox FastAPI Market Stream Gateway

A FastAPI service that keeps one Upstox MarketDataStreamerV3 connection, persists active subscriptions in MongoDB, and republishes each instrument feed through your own WebSocket endpoint.

## Features

- Loads the access token from `UPSTOX_APP.upstox_tokens` document `_id=upstox_access_token` at startup.
- Reloads the token and reconnects at 09:00 Asia/Kolkata every Monday-Friday.
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
```

The application only reads this document. Your existing Telegram/token process remains responsible for updating it.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000` and enter the `APP_API_KEY` value.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

When using Docker Compose, set `MONGODB_URI=mongodb://mongodb:27017` in `.env`.

## API

All management endpoints require `X-API-Key`.

```bash
curl -X POST http://localhost:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"full"}'
```

```bash
curl -X PATCH http://localhost:8000/api/subscriptions/mode \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"ltpc"}'
```

```bash
curl -X DELETE http://localhost:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"instrument_keys":["NSE_INDEX|Nifty 50"],"mode":"full"}'
```

```bash
curl -H 'X-API-Key: change-me' http://localhost:8000/api/subscriptions
curl -X POST -H 'X-API-Key: change-me' http://localhost:8000/api/token/hard-refresh
```

Client WebSocket:

```text
ws://localhost:8000/ws/market?instrument_key=NSE_INDEX%7CNifty%2050
```

## Important deployment notes

- Run one Uvicorn worker. The Upstox streamer and WebSocket client registry are in process memory. Multiple workers would create duplicate upstream connections and separate client registries.
- For horizontal scaling, move fan-out to Redis pub/sub and elect one upstream-stream owner.
- Protect the WebSocket endpoint with authentication before exposing it publicly. The sample keeps the browser flow simple and only protects management APIs.
- A TTL index expires event records after seven days. MongoDB TTL deletion is asynchronous, so an expired row may remain briefly before MongoDB removes it.
- Subscribe operations are idempotent: an already-subscribed key with the same mode is returned in `already_subscribed`; a different mode is changed instead of duplicated.
- If an Upstox subscribe call succeeds but MongoDB fails immediately afterward, upstream state and persisted state may briefly differ. A reconnect restores MongoDB as the source of truth. Production systems can add a reconciliation job and operation IDs.
