# Instrument Isolation Service & Live Market Feed Gateway

FastAPI application integrating the Upstox MarketDataStreamerV3 feed with an instrument-isolation workflow. It exposes live market updates to downstream WebSocket clients, supports dynamic instrument subscriptions, provides instrument-search and isolation APIs, and runs scheduled market-session jobs.

**Repository:** `7b809/t_feed_v1`  
**Branch:** `isolation_v1`  
**Application version:** `0.7.1`  
**Default port:** `8001`  
**Timezone:** `Asia/Kolkata` (IST)

---

## Contents

- [Overview and architecture](#overview-and-architecture)
- [Requirements and installation](#requirements-and-installation)
- [Configuration](#configuration)
- [MongoDB and Upstox token](#mongodb-and-upstox-token)
- [Run the application](#run-the-application)
- [HTTP API reference](#http-api-reference)
- [Live WebSocket API](#live-websocket-api)
- [WebSocket examples](#websocket-examples)
- [Dynamic subscriptions](#dynamic-subscriptions)
- [Feed modes](#feed-modes)
- [Monitoring](#monitoring)
- [Nginx HTTPS/WSS deployment](#nginx-httpswss-deployment)
- [Operational behaviour](#operational-behaviour)
- [Troubleshooting and security](#troubleshooting-and-security)
- [Project structure](#project-structure)

## Overview and architecture

### Live market-data gateway

- Connects to Upstox using `MarketDataStreamerV3`.
- Exposes downstream market data at **`/ws/market`**.
- Supports a single instrument or multiple instrument keys.
- Defaults to **`full`** mode when no mode is specified.
- Subscribes requested instruments upstream when they are not already subscribed to the selected mode's feed.
- Supports `ltpc`, `full`, `full_d30`, and `option_greeks`.
- Accepts subscribe/unsubscribe commands without reconnecting.
- Shares upstream feeds between clients using the same mode and tracks per-client subscription references.

### Instrument-isolation workflow

- Fetches and validates the daily Upstox token.
- Starts the primary market feed and prepares opening-range levels.
- Runs scheduled isolation scans and startup backfill.
- Exposes isolation current/history/trigger/backfill APIs.
- Exposes instrument search and log-inspection APIs.

The gateway reuses the primary application feed when its mode matches the requested WebSocket mode and the feed is running. Different modes use separate upstream streams because Upstox selects the mode when the streamer is created.

### Architecture at a glance

```text
MongoDB app_tokens
       |
       v
Upstox access token
       |
       v
FastAPI application
       |
       +---- Daily / isolation MarketFeedService
       |
       +---- WebSocket Feed Manager (one managed stream per mode)
                         |
                  Upstox MarketDataStreamerV3
                         |
                   Incoming instrument data
                         |
                    Message listeners
                         |
                  Per-client bounded queues
                         |
                  Downstream WebSocket clients
```

Upstox is the upstream provider. This service relays provider payloads; it does not generate or simulate market prices.

## Requirements and installation

Requirements:

- Python 3.10 or later, compatible with the installed Upstox SDK.
- MongoDB reachable by the application.
- Valid Upstox access token stored in MongoDB.
- Upstox Python SDK access to `MarketDataStreamerV3`.
- Outbound network access to MongoDB and Upstox.
- For public deployment: a domain, TLS certificate, and reverse proxy configured for WebSocket upgrades.

Clone the target branch:

```bash
git clone --branch isolation_v1 https://github.com/7b809/t_feed_v1.git
cd t_feed_v1
```

Create and activate a virtual environment.

**Linux**

```bash
python3 -m venv myenv
source myenv/bin/activate
```

**Windows PowerShell**

```powershell
python -m venv myenv
.\myenv\Scripts\Activate.ps1
```

Install dependencies and create the environment file:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

On Windows, use `Copy-Item .env.example .env`. Edit `.env` with your own MongoDB connection and application settings. Never commit real tokens, passwords, or API keys.

## Configuration

The complete set of strategy and scheduling options is documented in `.env.example` and `core/config.py`. Principal settings:

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection |
| `MONGO_DB_NAME` | `UPSTOX_APP_V2` | MongoDB database |
| `FEED_MODE` | `full` | Primary application feed mode |
| `LOG_RAW_FEED` | `false` | Enable verbose raw feed logging |
| `FEED_HEALTH_INTERVAL_SECONDS` | `300` | Feed health log interval |
| `FEED_STALE_AFTER_SECONDS` | `120` | Stale-data threshold |
| `TIMEZONE` | `Asia/Kolkata` | Application timezone |
| `TEST_FLAG` | `false` | Immediate/deferred startup control |
| `DEFERRED_STARTUP_HOUR` | `9` | Deferred startup hour |
| `DEFERRED_STARTUP_MINUTE` | `18` | Deferred startup minute |

## MongoDB and Upstox token

The application reads the Upstox access token from MongoDB. Downstream WebSocket clients do not receive or provide this token; the server uses it for the upstream Upstox connection:

- Database: `MONGO_DB_NAME` (default `UPSTOX_APP_V2`)
- Collection: `app_tokens`
- Document ID: `upstox_access_token`
- Field: `access_token`

Expected document shape:

```json
{
  "_id": "upstox_access_token",
  "access_token": "<UPSTOX_ACCESS_TOKEN>",
  "updated_at": "<timestamp>"
}
```

The token must be valid for the Upstox account and have the required market-data permissions. The daily refresh validates the token before starting the primary feed.

## Run the application

Run from the project root:

```bash
uvicorn main:app --host 0.0.0.0 --port 8001
```

For local-only access, bind to `127.0.0.1` instead.

The existing Linux start script can also be used:

```bash
chmod +x start.sh
./start.sh
```

The script creates/uses the `myenv` virtual environment, installs requirements, and starts Uvicorn on port 8001. Startup output is written to `logs/fastapi_startup.log`.

When running:

- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`
- OpenAPI schema: `http://localhost:8001/openapi.json`

WebSocket routes are not ordinary HTTP operations in Swagger UI; use the examples below.

## HTTP API reference

All paths are relative to the application base URL, for example `https://your-domain.example`.

### Application and feed

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Basic liveness response |
| GET | `/status` | Scheduler status and registered jobs |
| GET | `/startup-state` | Startup mode, deferred cutoff, last daily refresh date |
| POST | `/trigger/refresh` | Manually run daily refresh |
| GET | `/api/v1/feed/status` | WebSocket-managed feeds, subscriptions and health |

Force a daily refresh:

```bash
curl -X POST "http://localhost:8001/trigger/refresh?force=true"
```

Without `force=true`, daily refresh is guarded against duplicate successful runs on the same day.

### Isolation

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/isolation/current` | Current persisted isolation record |
| GET | `/api/v1/isolation/history?limit=20` | Recent history; limit 1–200 |
| POST | `/api/v1/isolation/trigger` | Run a manual isolation scan |
| POST | `/api/v1/isolation/backfill` | Run startup backfill |
| POST | `/api/isolation-test` | Send a test isolation notification |

Examples:

```bash
curl "http://localhost:8001/api/v1/isolation/current"
curl "http://localhost:8001/api/v1/isolation/history?limit=20"

curl -X POST "http://localhost:8001/api/v1/isolation/trigger" \
  -H "Content-Type: application/json" \
  -d '{"force": false}'

curl -X POST "http://localhost:8001/api/v1/isolation/backfill"
```

The current-isolation endpoint returns HTTP 404 when no isolation record has been persisted.

### Instrument search

| Method | Endpoint | Required query parameters | Description |
|---|---|---|---|
| GET | `/api/v1/search/instrument` | `strike`, `option_type` | First matching option at a strike |
| GET | `/api/v1/search/instrument/all` | `strike`, `option_type` | Matching instruments at a strike |
| GET | `/api/v1/search/instrument/atm` | `nifty_ltp`, `option_type` | Calculate ATM strike and search |
| GET | `/api/v1/search/instrument/raw` | `query` | Raw Upstox instrument-search response |
| GET | `/api/v1/search/instrument/dummy-isolation` | `strike`, `option_type` | Build diagnostic isolation-shaped response |

Examples:

```bash
# First match for NIFTY 25050 CE
curl "http://localhost:8001/api/v1/search/instrument?strike=25050&option_type=CE"

# All matches at a strike
curl "http://localhost:8001/api/v1/search/instrument/all?strike=25050&option_type=PE&max_records=30"

# ATM option lookup from a supplied NIFTY spot LTP
curl "http://localhost:8001/api/v1/search/instrument/atm?nifty_ltp=25038.75&option_type=CE&atm_offset=0"

# Raw instrument search
curl "http://localhost:8001/api/v1/search/instrument/raw?query=NIFTY%2025050%20CE"

# Preview a dummy isolation response; persist defaults to false
curl "http://localhost:8001/api/v1/search/instrument/dummy-isolation?strike=25050&option_type=CE&option_ltp=150"
```

Search endpoints fetch the current Upstox token from MongoDB and call the Upstox instrument-search API. The dummy-isolation endpoint is diagnostic. Do not use `persist=true` on a production trading day unless intentionally replacing today's persisted isolation record.

### Logs

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/logs` | List available log files |
| GET | `/api/v1/logs/{category}/{filename}?lines=100` | Read/tail a log file |
| GET | `/api/v1/logs/{category}/{filename}/download` | Download a log file as text |

Use category and filename values returned by the log-list API.

## Live WebSocket API

**Public access:** downstream WebSocket connections require no API key, JWT, login, or Upstox token. Connect directly with the instrument key. The server retrieves and uses its own Upstox access token internally.

### Endpoint

```text
/ws/market
```

Use `wss://` through HTTPS in production and `ws://` for local/insecure development.

### Query parameters

| Parameter | Required | Description |
|---|---:|---|
| `instrument_key` | One instrument parameter is required | One instrument key; may be repeated |
| `instrument_keys` | One instrument parameter is required | Comma-separated instrument keys |
| `mode` | No | Upstox feed mode; defaults to `full` |

Instrument keys contain a pipe character (`|`). URL-encode it as `%7C` when building a URL. Multiple instrument keys may be separated by commas.

### Single instrument

```text
ws://localhost:8001/ws/market?instrument_key=NSE_FO%7C45405
```

### Multiple instruments

```text
ws://localhost:8001/ws/market?instrument_keys=NSE_FO%7C45405,NSE_FO%7C45404
```

### Repeated query parameter

```text
ws://localhost:8001/ws/market?instrument_key=NSE_FO%7C45405&instrument_key=NSE_FO%7C45404
```

### Explicit mode

```text
ws://localhost:8001/ws/market?instrument_key=NSE_FO%7C45405&mode=ltpc
```

Supported modes: `ltpc`, `full`, `full_d30`, `option_greeks`.

### Connection acknowledgement

After accepting the WebSocket, the server ensures the selected upstream feed is connected and attempts to subscribe the requested instruments. It then sends:

```json
{
  "type": "subscribed",
  "mode": "full",
  "instrument_keys": [
    "NSE_FO|45405",
    "NSE_FO|45404"
  ],
  "message": "Live market data stream ready"
}
```

If the upstream connection does not become ready within 10 seconds, or a requested subscription cannot be established, the server reports an error and ends the failed connection.

### Live market-data message

Each incoming instrument update is sent as a separate JSON message:

```json
{
  "type": "market_data",
  "mode": "full",
  "instrument_key": "NSE_FO|45405",
  "data": {
    "...": "Original Upstox feed payload for this instrument"
  }
}
```

The `data` property contains the original Upstox instrument payload, not a normalized candle. Fields depend on the selected Upstox mode and the provider message.

### Error message

```json
{
  "type": "error",
  "message": "Description of the validation or subscription error"
}
```

## WebSocket examples

### JavaScript / browser

Single instrument, default full mode:

```javascript
const ws = new WebSocket(
  "wss://your-domain.example/ws/market?instrument_key=NSE_FO%7C45405"
);

ws.onopen = () => console.log("Connected to market feed");

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);

  if (message.type === "subscribed") {
    console.log("Subscription confirmed:", message.instrument_keys);
  } else if (message.type === "market_data") {
    console.log(message.instrument_key, message.mode, message.data);
  } else if (message.type === "error") {
    console.error("Feed error:", message.message);
  }
};

ws.onerror = (event) => console.error("WebSocket error:", event);
ws.onclose = (event) => console.log("WebSocket closed:", event.code, event.reason);
```

### Python / websockets

Install the client package:

```bash
pip install websockets
```

Connect and receive two instruments:

```python
import asyncio
import json
import websockets

URL = (
    "wss://your-domain.example/ws/market"
    "?instrument_keys=NSE_FO%7C45405,NSE_FO%7C45404"
)

async def main():
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        async for raw_message in ws:
            message = json.loads(raw_message)
            kind = message.get("type")

            if kind == "subscribed":
                print("Subscribed:", message["instrument_keys"])
            elif kind == "market_data":
                print(message["instrument_key"], message["data"])
            elif kind == "error":
                print("Feed error:", message["message"])

asyncio.run(main())
```

For local testing, use:

```text
ws://127.0.0.1:8001/ws/market?instrument_key=NSE_FO%7C45405
```

## Dynamic subscriptions

A connected client can modify its instrument list without opening another WebSocket connection. Commands are JSON text messages sent from the client to the server.

### Subscribe multiple instruments

```json
{
  "action": "subscribe",
  "instrument_keys": [
    "NSE_FO|45406",
    "NSE_FO|45407"
  ]
}
```

Single-instrument form:

```json
{
  "action": "subscribe",
  "instrument_key": "NSE_FO|45406"
}
```

Confirmation:

```json
{
  "type": "subscribed",
  "mode": "full",
  "instrument_keys": [
    "NSE_FO|45406",
    "NSE_FO|45407"
  ]
}
```

### Unsubscribe

```json
{
  "action": "unsubscribe",
  "instrument_keys": ["NSE_FO|45406"]
}
```

Confirmation:

```json
{
  "type": "unsubscribed",
  "mode": "full",
  "instrument_keys": ["NSE_FO|45406"]
}
```

The confirmation lists instruments changed for that client. If another client still uses an instrument, the upstream subscription is retained. When the last client releases a dynamically added instrument, the manager removes it upstream. Initial baseline instruments belonging to the primary application feed are not removed by a downstream client unsubscribe.

JavaScript example (send after the socket opens):

```javascript
ws.send(JSON.stringify({
  action: "subscribe",
  instrument_keys: ["NSE_FO|45406", "NSE_FO|45407"]
}));

ws.send(JSON.stringify({
  action: "unsubscribe",
  instrument_key: "NSE_FO|45406"
}));
```

## Feed modes

| Mode | Description |
|---|---|
| `ltpc` | Last traded price and close-related feed mode |
| `full` | Full market feed mode; default for downstream WebSocket requests |
| `full_d30` | Full depth-30 feed mode |
| `option_greeks` | Option Greeks feed mode |

The selected mode is fixed for a WebSocket session. To change mode, close the connection and reconnect with the desired `mode` query parameter.

Upstox selects a mode when an upstream streamer is created. The gateway maintains separate upstream streams for different modes instead of changing the mode of an existing stream. Clients using the same mode share the same managed upstream stream.

The primary isolation feed is configured independently through `FEED_MODE`. When its mode matches a WebSocket request and it is running, the gateway can reuse it.

## Monitoring

### WebSocket-managed feed status

```bash
curl "http://localhost:8001/api/v1/feed/status"
```

This reports managed modes, running state, subscribed instrument keys, client count, and the underlying feed health snapshot.

### Application health

```bash
curl "http://localhost:8001/health"
curl "http://localhost:8001/status"
curl "http://localhost:8001/startup-state"
```

Useful checks:

- Confirm MongoDB is reachable and the Upstox token document exists.
- Confirm the Upstox token validates during daily refresh.
- Confirm the upstream feed reaches its connected state.
- Confirm requested instrument keys appear in `/api/v1/feed/status`.
- Confirm market updates arrive during an active market session.
- Check application logs for upstream errors, subscription failures, and stale feed data.

No-data or stale states can be normal outside market hours or when an instrument is not publishing updates.

## Nginx HTTPS/WSS deployment

For public deployment, terminate TLS at Nginx and proxy WebSocket requests with HTTP/1.1 upgrade headers. Replace the domain and certificate paths:

```nginx
server {
    listen 443 ssl;
    server_name feed.example.com;

    ssl_certificate     /etc/letsencrypt/live/feed.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/feed.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Validate and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

The public WebSocket URL then uses `wss://feed.example.com/ws/market?...`. Ensure the firewall/security group allows inbound HTTPS and port 8001 is not unnecessarily exposed to the public internet.

## Operational behaviour

- The primary daily refresh validates the Upstox token and starts the primary market feed.
- The scheduler runs daily refresh, pivot checks, periodic isolation scans, and the configured end-of-day primary-feed disconnect.
- Startup may be immediate or deferred according to `TEST_FLAG` and the deferred-startup time.
- WebSocket clients request subscriptions independently. The manager tracks each client's keys and releases references on disconnect.
- A client's unsubscribe/disconnect does not remove an instrument still used by another client.
- A bounded per-client queue limits memory growth. If a client cannot consume messages quickly enough, older queued updates may be discarded in favour of newer updates.
- Different feed modes may require additional upstream connections and may be subject to Upstox account limits.
- A subscription acknowledgement means the upstream subscription was accepted; it does not guarantee the instrument is currently publishing ticks.

## Troubleshooting and security

### WebSocket closes immediately

Check:

1. At least one `instrument_key` or `instrument_keys` parameter was supplied.
2. Instrument keys are URL-encoded correctly; encode `|1. At least one `instrument_key` or `instrument_keys` parameter was supplied.
2. Instrument keys are URL-encoded correctly; encode `|` as `%7C`.
3. Mode is one of `ltpc`, `full`, `full_d30`, or `option_greeks`.
4. Nginx forwards WebSocket upgrade headers.

### Upstream feed does not connect

- Check MongoDB connectivity and the `app_tokens` document.
- Validate the Upstox access token.
- Check outbound connectivity and Upstox service availability.
- Inspect application logs for SDK errors.
- Confirm the account permits the selected mode and requested subscriptions.

### Subscription fails

- Confirm the instrument key is valid and active.
- Confirm the upstream connection is already open.
- Check Upstox subscription limits and SDK errors.
- Inspect `/api/v1/feed/status` and server logs.

### Connection succeeds but no market updates arrive

- Confirm the instrument is subscribed in the requested mode.
- Check whether the exchange is open and the instrument is trading.
- Check the feed health snapshot for no-data or stale status.
- A subscription acknowledgement does not guarantee that the instrument is currently producing ticks.

### HTTP works but WSS does not

- Verify TLS certificate and DNS.
- Verify Nginx WebSocket upgrade headers.
- Check Nginx and application logs.
- Confirm reverse-proxy timeouts are suitable for persistent connections.

### Security recommendations

1. Use HTTPS/WSS for every public deployment.
2. This WebSocket endpoint is intentionally public and does not authenticate downstream clients. Anyone who knows the URL and a valid instrument key can connect.
3. Apply firewall/security-group rules, reverse-proxy rate limits, connection limits, and subscription caps if public usage needs to be controlled.
4. Do not expose MongoDB publicly.
5. Never return or log the Upstox access token to downstream clients.
6. Do not commit `.env`, credentials, tokens, or production API keys.
7. Monitor client counts, upstream connection counts, subscription totals, and provider limits.

## Project structure

```text
t_feed_v1/
├── api/
│   └── routes/
│       ├── isolation.py
│       ├── market_feed.py
│       └── search.py
├── core/
│   ├── config.py
│   ├── market_feed_service.py
│   ├── mongo_client.py
│   └── token_service.py
├── jobs/
│   ├── daily_refresh.py
│   ├── deferred_startup.py
│   └── isolation_scan.py
├── services/
├── repositories/
├── .env.example
├── main.py
├── requirements.txt
├── scheduler.py
└── start.sh
```

This tree highlights the main runtime components; additional service, repository, model, utility, and data files are part of the project.

## Provider notes

This repository integrates with Upstox services and the Upstox Python SDK. Provider authentication, market-data availability, instrument validity, feed-mode availability, and subscription limits remain subject to the applicable Upstox account and API terms.
