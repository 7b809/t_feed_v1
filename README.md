# Live Option Feed Engine

A FastAPI-based live market feed engine for NIFTY option contracts using the Upstox WebSocket feed. The application fetches NIFTY option contracts, subscribes to the NIFTY index and filtered option instruments, receives live ticks from Upstox, normalizes incoming feed data, and broadcasts updates to browser clients over local FastAPI WebSocket endpoints.

The project has now evolved from a static single-option dashboard into a route-segregated FastAPI application with:

- A FastAPI-rendered dashboard served from `templates/index.html`
- Live `/all-feeds` WebSocket streaming
- A NIFTY index header card
- Dynamic nearest CE and PE option cards based on live NIFTY LTP
- Scheduled daily hard refresh at 09:00 AM IST, Monday to Friday
- Manual hard refresh API and dashboard button
- Telegram notifications for startup, refresh, subscription, token, instruments, shutdown, and errors

---

## Project Purpose

This project acts as a local live option feed gateway.

It handles:

1. Loading Upstox access tokens from MongoDB.
2. Fetching nearest NIFTY option contracts using the Upstox Options API.
3. Filtering option contracts by configured strike range.
4. Subscribing to the NIFTY index plus filtered option contracts.
5. Connecting to Upstox `MarketDataStreamerV3`.
6. Parsing raw live feed ticks into a normalized JSON format.
7. Broadcasting normalized ticks to connected FastAPI WebSocket clients.
8. Rendering a browser dashboard through FastAPI templates.
9. Showing live NIFTY index data in the dashboard header.
10. Showing nearest CE and PE option cards based on live NIFTY LTP.
11. Supporting automatic scheduled hard refresh.
12. Supporting manual hard refresh through an API and UI button.
13. Sending Telegram alerts for important lifecycle events and failures.
14. Providing debug and health endpoints for local testing.

---

## Current Project Structure

```text
t_feed_v1-ws-feed/
│
├── main.py
├── requirements.txt
├── Procfile
├── run.bat
├── .env
├── README.md
│
├── api/
│   ├── __init__.py
│   ├── home_routes.py
│   ├── health_routes.py
│   ├── debug_routes.py
│   └── refresh_routes.py
│
├── core/
│   ├── config.py
│   └── logger.py
│
├── data/
│   └── nearest_nifty_option_contracts.json
│
├── services/
│   ├── option_service.py
│   ├── telegram_service.py
│   ├── token_service.py
│   └── upstox_websocket.py
│
├── templates/
│   └── index.html
│
└── ws_feed/
    ├── __init__.py
    ├── broadcaster.py
    └── websocket_routes.py
```

---

## Main Components

### `main.py`

Main FastAPI application entry point.

Responsibilities:

- Creates the FastAPI app.
- Registers CORS middleware.
- Registers all HTTP and WebSocket route modules.
- Runs startup lifecycle logic.
- Starts APScheduler background jobs.
- Starts the Upstox WebSocket streamer.
- Performs daily hard refresh scheduling.
- Sends Telegram notifications for startup, scheduler, refresh, subscription, errors, and shutdown.
- Stops streamer and scheduler during shutdown.

Registered route modules:

```python
app.include_router(home_router)
app.include_router(health_router)
app.include_router(debug_router)
app.include_router(refresh_router)
app.include_router(websocket_router)
```

Startup lifecycle flow:

```text
Application startup
    -> token_service.refresh_tokens()
    -> get_options_contracts(save_data=True)
    -> update options_cache and subscribed_keys
    -> start APScheduler
    -> start Upstox streamer
    -> send Telegram startup/subscription notifications
```

Daily hard refresh flow:

```text
Mon-Fri 09:00 AM IST
    -> refresh token from MongoDB
    -> fetch latest option contracts
    -> filter instruments by configured strike range
    -> update options_cache subscribed_keys
    -> restart Upstox streamer
    -> send Telegram refresh/subscription notifications
```

---

## API Routes

### `api/home_routes.py`

Serves the dashboard home page.

#### Endpoint

```text
GET /
```

Renders:

```text
templates/index.html
```

This is now the main browser dashboard route.

---

### `api/health_routes.py`

Contains the health check route.

#### Endpoint

```text
GET /health
```

Returns:

- Application health status
- Token availability
- Options cache status
- Upstox WebSocket streamer status
- Subscribed instrument count
- Connected WebSocket client count

Example response:

```json
{
  "status": "healthy",
  "timestamp": "2026-08-04T00:00:00+00:00",
  "services": {
    "token_service": "active",
    "options_cache": "loaded (83 keys)",
    "websocket_feed": "active"
  },
  "metrics": {
    "subscribed_instruments": 83,
    "connected_ws_clients": 1
  }
}
```

---

### `api/debug_routes.py`

Contains local testing and debugging routes.

#### Endpoints

```text
GET /test-broadcast
GET /test-broadcast-option
GET /debug/cache
GET /debug/find-option?strike=24500&striketype=ce
```

#### `/test-broadcast`

Sends a sample NIFTY index tick to connected `/ws` and `/all-feeds` clients.

#### `/test-broadcast-option`

Sends a sample `NIFTY 24500 CE` option tick to connected `/option` clients.

#### `/debug/cache`

Returns the current in-memory options cache summary.

#### `/debug/find-option`

Searches the current options cache for a matching strike and CE/PE type.

Example:

```text
http://127.0.0.1:8000/debug/find-option?strike=24500&striketype=ce
```

---

### `api/refresh_routes.py`

Contains manual market hard refresh APIs.

#### Endpoints

```text
POST /refresh/manual
GET  /refresh/status
```

#### `POST /refresh/manual`

Manually triggers the same hard refresh workflow used by the daily scheduler.

Steps performed:

1. Refresh token document from MongoDB.
2. Load latest token into memory.
3. Fetch latest NIFTY option contracts.
4. Filter instruments by configured strike range.
5. Update `options_cache` and `subscribed_keys`.
6. Restart Upstox streamer so latest subscription keys are applied.
7. Send Telegram notifications for success/failure.

Example response:

```json
{
  "status": "success",
  "message": "Manual market hard refresh completed successfully.",
  "started_at": "2026-08-04T03:30:00+00:00",
  "completed_at": "2026-08-04T03:30:08+00:00",
  "nearest_expiry": "2026-08-04",
  "total_contracts": 82,
  "subscribed_instruments": 83,
  "feed_mode": "full"
}
```

#### `GET /refresh/status`

Returns latest manual refresh status and current cache summary.

Example response:

```json
{
  "manual_refresh_running": false,
  "last_manual_refresh": {
    "status": "success",
    "timestamp": "2026-08-04T03:30:08+00:00",
    "message": "Manual market hard refresh completed successfully.",
    "subscribed_instruments": 83,
    "nearest_expiry": "2026-08-04"
  },
  "current_cache": {
    "nearest_expiry": "2026-08-04",
    "total_contracts": 82,
    "subscribed_keys_count": 83
  }
}
```

---

## WebSocket Routes

### `ws_feed/websocket_routes.py`

Contains FastAPI WebSocket endpoints.

#### Endpoints

```text
WS /ws
WS /all-feeds
WS /option?strike=24500&striketype=ce
```

#### `/ws` and `/all-feeds`

Both routes connect a client to all subscribed instruments.

Current dashboard uses only:

```text
WS /all-feeds
```

The dashboard no longer opens a separate fixed `/option` connection. It derives nearest CE and PE option cards from the all-feeds stream.

#### `/option`

Connects a client to a specific option based on strike and option type.

Example:

```text
ws://127.0.0.1:8000/option?strike=24500&striketype=ce
```

Behavior:

- Validates `striketype` as `CE` or `PE`.
- Registers client using `broadcaster.connect_option()`.
- Uses a key format like:

```text
24500.0_CE_0
```

Here, the final `0` represents live tick interval.

This endpoint remains available for future focused option consumers even though the current UI mainly uses `/all-feeds`.

---

## Dashboard

### `templates/index.html`

FastAPI-rendered dashboard page served at:

```text
http://127.0.0.1:8000/
```

Current dashboard features:

- Header with live NIFTY 50 LTP.
- Header with NIFTY change, percent change, OHLC, and update time.
- Hard Refresh button.
- Manual refresh status panel.
- `/all-feeds` connection status badge.
- Dynamic nearest CE option cards.
- Dynamic nearest PE option cards.
- Nearest CE/PE selection based on live NIFTY LTP.
- No separate fixed target option WebSocket connection.

Dashboard feed behavior:

```text
Browser
    -> connects to /all-feeds
    -> receives NIFTY index ticks and option ticks
    -> updates NIFTY header
    -> stores CE/PE option ticks in memory
    -> sorts options by distance from live NIFTY LTP
    -> displays nearest CE and PE cards
```

The dashboard uses:

```javascript
const wsProtocol = window.location.protocol === "https:" ? "wss" : "ws";
const wsHost = `${wsProtocol}://${window.location.host}`;
const apiHost = `${window.location.protocol}//${window.location.host}`;
```

This allows it to work locally and in deployment without hardcoding port `8000`.

---

## Core Configuration

### `core/config.py`

Central configuration file.

Expected configuration:

```python
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")
REFRESH_INTERVAL_MINUTES = 60

MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"
STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0

WEBSOCKET_FEED_MODE = "full"

MARKET_TIMEZONE = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))
```

Key settings:

- `MAIN_NIFTY_SECURITY`: NIFTY index instrument key.
- `STRIKE_FROM`: Lower strike filter.
- `STRIKE_TO`: Upper strike filter.
- `WEBSOCKET_FEED_MODE`: Upstox feed mode. Current value is `full`.
- `REFRESH_INTERVAL_MINUTES`: Token refresh interval.
- `MARKET_TIMEZONE`: Market timezone, default `Asia/Kolkata`.
- `TELEGRAM_ENABLED`: Enables or disables Telegram alerts.
- `TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `TELEGRAM_CHAT_ID`: Telegram chat ID.
- `TELEGRAM_TIMEOUT_SECONDS`: Telegram request timeout.

---

## Services

### `services/token_service.py`

Handles access token retrieval from MongoDB.

Responsibilities:

- Connects to MongoDB.
- Reads token document with `_id = "upstox_access_token"`.
- Stores token data in thread-safe memory cache.
- Provides access token to other services.

Expected MongoDB token document:

```json
{
  "_id": "upstox_access_token",
  "access_token": "your_upstox_access_token",
  "updated_at": "timestamp"
}
```

---

### `services/option_service.py`

Fetches and manages NIFTY option contracts.

Responsibilities:

- Calls Upstox Options API.
- Gets option contracts for `NSE_INDEX|Nifty 50`.
- Finds nearest valid expiry date.
- Filters contracts by configured strike range.
- Standardizes option contract fields.
- Updates global `options_cache`.
- Saves contracts to `data/nearest_nifty_option_contracts.json`.

Runtime cache:

```python
options_cache = {
    "nearest_expiry": None,
    "total_contracts": 0,
    "subscribed_keys": [],
    "data": [],
}
```

Subscription keys include:

1. NIFTY index key.
2. Filtered option contract keys.

---

### `services/upstox_websocket.py`

Maintains the connection to Upstox live market feed.

Responsibilities:

- Reads access token from `token_service`.
- Reads subscribed instrument keys from `options_cache`.
- Creates `upstox_client.MarketDataStreamerV3`.
- Connects to Upstox WebSocket in a background thread.
- Receives tick messages.
- Sends decoded feed ticks to `broadcaster.broadcast_tick()`.
- Reconnects on failure.
- Supports `restart()` so refreshed subscription keys are applied after daily or manual hard refresh.
- Logs market time using `MARKET_TIMEZONE` from config.

Important optimization:

If no local WebSocket clients are connected, incoming Upstox ticks are not scheduled for processing. This prevents FastAPI event-loop overload.

---

### `services/telegram_service.py`

Sends Telegram notifications using bot token and chat ID from config.

Telegram events include:

- Application startup started
- Application startup success/warnings
- Token refresh success/failure
- Instrument fetch success/failure
- Scheduler started
- Daily hard refresh started/success/failure
- Manual hard refresh started/success/failure
- Upstox subscription active/failure
- Application shutdown started/completed
- Exceptions and runtime failures

Configuration comes from `.env` through `core/config.py`.

Required `.env` values:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_TIMEOUT_SECONDS=10
```

---

## Broadcaster

### `ws_feed/broadcaster.py`

Core local WebSocket broadcasting engine.

Responsibilities:

- Tracks connected local WebSocket clients.
- Maintains separate connection pools for:
  - generic connections
  - all-feeds connections
  - option-specific connections
- Parses raw Upstox feed data into normalized payloads.
- Broadcasts live ticks to matching clients.
- Removes dead WebSocket clients.
- Logs raw feeds to `logs/feeds.log`.

Connection pools:

```python
self.active_connections = set()
self.all_feeds_connections = {}
self.option_connections = {}
```

Option connection key format:

```text
{strike_price}_{instrument_type}_{interval}
```

Example:

```text
24500.0_CE_0
```

Currently, interval `0` represents live ticks.

---

## WebSocket Payload Format

### Live Tick Payload

The broadcaster normalizes Upstox feed into this structure:

```json
{
  "type": "live_tick",
  "interval": 0,
  "instrument_key": "NSE_FO|65860",
  "ltp": 120.5,
  "close": 115.0,
  "change": 5.5,
  "change_pct": 4.78,
  "ltt": 0,
  "ltq": 75,
  "open": 110.0,
  "high": 130.0,
  "low": 100.0,
  "volume": 250000,
  "atp": 118.5,
  "oi": 500000,
  "upper_circuit": 0.0,
  "lower_circuit": 0.0,
  "greeks": {
    "iv": 12.5,
    "delta": 0.52,
    "theta": -8.2,
    "gamma": 0.001,
    "vega": 10.4,
    "rho": 1.25
  },
  "depth": [],
  "info": {
    "instrument_key": "NSE_FO|65860",
    "instrument_type": "CE",
    "strike_price": 24500.0,
    "expiry": "2026-08-04",
    "trading_symbol": "NIFTY 24500 CE 04 AUG 26",
    "underlying_symbol": "NIFTY"
  }
}
```

---

## Scheduled Refresh

The application performs a daily hard refresh:

```text
Monday to Friday at 09:00 AM Asia/Kolkata
```

Scheduled hard refresh does:

```text
1. Refresh token document from MongoDB
2. Load latest token into memory
3. Fetch latest NIFTY option instruments
4. Filter instruments by configured strike range
5. Update options_cache and subscribed_keys
6. Restart Upstox streamer
7. Subscribe with the latest instrument keys
8. Send Telegram notifications
```

Scheduler configuration is in `main.py`:

```python
CronTrigger(
    day_of_week="mon-fri",
    hour=9,
    minute=0,
    timezone=getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
)
```

---

## Manual Refresh

Manual refresh is available from both API and dashboard.

### API

```text
POST /refresh/manual
GET  /refresh/status
```

### Dashboard

The dashboard includes a `Hard Refresh` button in the header. It calls:

```text
POST /refresh/manual
```

The UI shows:

- Refresh running
- Refresh success
- Refresh failed
- Last refresh details

---

## Setup Instructions

### 1. Create Virtual Environment

```bat
python -m venv myenv
myenv\Scripts\activate
```

### 2. Install Dependencies

```bat
pip install -r requirements.txt
```

### 3. Configure `.env`

Example `.env`:

```env
MONGO_URL=mongodb://localhost:27017
MONGO_DB=your_database_name
TOKENS_COLLECTION=your_tokens_collection_name

MARKET_TIMEZONE=Asia/Kolkata

TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TELEGRAM_TIMEOUT_SECONDS=10
```

Your MongoDB collection should contain a token document with `_id` as `upstox_access_token`.

---

## Requirements

```text
fastapi
uvicorn
upstox-python-sdk
pymongo
python-dotenv
pydantic
pydantic-settings
websockets
websocket-client
requests
pandas
numpy
apscheduler
jinja2
```

---

## Run the Application

Using batch file:

```bat
run.bat
```

Or directly:

```bat
python main.py
```

Server starts at:

```text
http://127.0.0.1:8000
```

Dashboard:

```text
http://127.0.0.1:8000/
```

Health endpoint:

```text
http://127.0.0.1:8000/health
```

---

## Testing the Application

### Health Check

```text
http://127.0.0.1:8000/health
```

### Cache Debug

```text
http://127.0.0.1:8000/debug/cache
```

### Find Option Contract

```text
http://127.0.0.1:8000/debug/find-option?strike=24500&striketype=ce
```

### Test All Feeds Broadcast

First connect a WebSocket client to:

```text
ws://127.0.0.1:8000/all-feeds
```

Then call:

```text
http://127.0.0.1:8000/test-broadcast
```

### Test Option Broadcast

First connect a WebSocket client to:

```text
ws://127.0.0.1:8000/option?strike=24500&striketype=ce
```

Then call:

```text
http://127.0.0.1:8000/test-broadcast-option
```

### Manual Refresh

```text
POST http://127.0.0.1:8000/refresh/manual
GET  http://127.0.0.1:8000/refresh/status
```

---

## Logs

Logs are written to the `logs/` folder.

Examples:

```text
logs/main.log
logs/option_service.log
logs/upstox_websocket.log
logs/broadcaster.log
logs/telegram_service.log
logs/feeds.log
```

`logs/feeds.log` stores raw feed JSON lines and is capped to the latest 2,000 lines.

---

## Deployment Note

If `main.py` is located directly in the project root, the recommended `Procfile` is:

```text
web: uvicorn main:app --host 0.0.0.0 --port 8000
```

Avoid this if you do not have an `app/` package:

```text
web : uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Also remove the extra space before the colon.

---

## Important Notes

- This project depends on a valid Upstox access token.
- Token is expected to be stored in MongoDB.
- If token is missing, option contract loading will fail.
- If options cache is empty, Upstox subscription keys will not be available.
- Updating `options_cache` alone is not enough to apply new subscriptions.
- The Upstox streamer must be restarted after refresh so it subscribes with the latest keys.
- If no local WebSocket clients are connected, incoming Upstox ticks are skipped for processing to reduce event-loop load.
- The dashboard is now served by FastAPI from `templates/index.html`.
- The dashboard currently uses `/all-feeds` and calculates nearest CE/PE from received ticks.
- Telegram notifications are optional and controlled by `TELEGRAM_ENABLED`.

---

## Quick Start Summary

```bat
python -m venv myenv
myenv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Then open:

```text
http://127.0.0.1:8000/
```

---

## Owner Notes

This README reflects the current version of the project where:

- HTTP routes are moved into `api/`.
- WebSocket routes are moved into `ws_feed/websocket_routes.py`.
- Dashboard is served from `templates/index.html`.
- Manual refresh is available through `/refresh/manual`.
- Daily refresh runs Mon-Fri at 09:00 AM IST.
- Telegram notifications are integrated through `services/telegram_service.py`.
- Dashboard displays live NIFTY 50 and nearest CE/PE option cards from `/all-feeds`.
