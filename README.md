# UpstoxAppV2

A FastAPI service that manages Upstox authentication tokens, exposes real-time market and portfolio data over HTTP and WebSocket, handles option-chain loading and subscription for indexes such as NIFTY and SENSEX, maintains a readonly per-contract candle cache with 9/21 EMA crossover computation, provides batch market-quote fetching, and serves a full web dashboard (live index cards, option chains, instrument search, per-contract charts, order book, logs, and an isolated-EMA monitoring page). Telegram notifications and a token health watchdog run alongside the FastAPI process.

## Table of Contents

1. [Overview](#1-overview)
2. [Features](#2-features)
3. [Architecture](#3-architecture)
4. [Folder Structure](#4-folder-structure)
5. [Requirements](#5-requirements)
6. [Installation](#6-installation)
7. [Environment Variables](#7-environment-variables)
8. [Running the App](#8-running-the-app)
9. [Startup Flow](#9-startup-flow)
10. [Web UI Pages](#10-web-ui-pages)
11. [HTTP API Reference](#11-http-api-reference)
12. [WebSocket Protocol](#12-websocket-protocol)
13. [Data Storage Layout](#13-data-storage-layout)
14. [Telegram Subsystem](#14-telegram-subsystem)
15. [How Things Work](#15-how-things-work)
16. [Development Notes](#16-development-notes)
17. [Troubleshooting](#17-troubleshooting)

## 1. Overview

UpstoxAppV2 is a long-running service that:

- Loads an Upstox access token from MongoDB into an in-memory cache
- Keeps that cache fresh on startup/restart and every 30 minutes
- Validates tokens through the Upstox User Profile API and a defensive resolver
- Runs a token health watchdog that alerts through Telegram when the token expires or becomes invalid
- Maintains WebSocket connections to Upstox market and portfolio feeds
- Fans out live market messages to downstream WebSocket clients
- Loads CE and PE option contracts for every enabled index within a configured strike range
- Loads historical and intraday candles per option contract
- Computes 9/21 EMA crossovers for historic and intraday candles
- Refreshes the candle cache daily at 09:00 IST when contract expiries roll over
- Bulk-subscribes enabled indexes and option contracts after refresh
- Fetches MarketQuoteV3 OHLC quotes with batching, halving fallback, and rate-limit protection
- Fetches today's order book through the Upstox SDK
- Persists option and candle snapshots with readonly and runtime folder separation
- Delivers Telegram notifications for lifecycle, refresh, token, and error events
- Serves a full web dashboard with live index cards, option-chain tables, instrument search, per-contract charts, order book, logs, and an isolated-EMA monitoring page

The service follows one rule: **tokens and user identity never leave the server**. No HTTP endpoint returns the raw access token, user ID, or user name.

## 2. Features

### Token management

- Load, save, and validate Upstox tokens using REST or Telegram
- Load the in-memory token cache from MongoDB on boot
- Refresh the token cache every 30 minutes by default
- Never return the token, user ID, or user name in API responses
- Resolve profile validation through `token_tasks/_profile.py`, with raw SDK fallback
- Poll token health and notify Telegram when the token becomes invalid or recovers

### Market data streaming

- Upstream connection through `MarketDataStreamerV3`
- Idempotent subscribe, unsubscribe, and change-mode operations
- Defer subscription operations outside market hours instead of failing them
- Wait-for-open handshake for immediate subscription requests
- Ring buffer of recent messages exposed over HTTP
- Subscribe every enabled index and option key after refresh

### Downstream WebSocket

- `/ws/market` fans out upstream messages to connected clients
- `/all-feeds` flat tick fanout used by the web dashboard, charts, index cards, and option chain
- Optional client filtering with `?keys=A,B` on `/ws/market`
- Broadcast topology events for subscribe, unsubscribe, and mode changes
- Thread-safe fanout from SDK callback threads
- Per-topic hubs for isolated-EMA crossover and Opening Range touch events

### Option chain service

- Loads contracts for indexes defined by `MAIN_INDEXES`
- Filters contracts using configured strike ranges
- Retains CE and PE contracts
- Uses nearest expiry by default, or all expiries when configured
- Persists snapshots to runtime and readonly folders
- Hydrates the cache from fresh disk snapshots during startup
- Bulk-subscribes fetched option instrument keys

### Candle service

- Fetches historical candles through `HistoryV3Api.get_historical_candle_data1`
- Splits historical requests into windows of at most seven days
- Fetches intraday candles through `HistoryV3Api.get_intra_day_candle_data`
- Uses `ThreadPoolExecutor` for parallel per-contract requests
- Uses a process-wide `RateLimiter` for Cloudflare 429 responses
- Stores readonly per-contract files as `<strike>_<CE|PE>.json`
- Treats files as stale when missing, empty, unsuccessful, outdated, or tied to an expired contract
- Runs a daily refresh scheduler at 09:00 IST
- Logs progress and full, partial, and failed result counters
- Chart page fetches intraday and 7-day historical windows directly from `api.upstox.com/v3/historical-candle`, with no access token required

### EMA crossover service

- Computes configurable 9/21 EMAs from stored candle closes
- Saves only crossover candles
- Produces `historic_cross.json` and `intraday_cross.json`
- Uses the weekend-aware last market day for historic splitting
- Recomputes on startup, daily refresh, and hard refresh
- Broadcasts live EMA-cross events to `/ws/ema-crossover`

### Market quote service

- Wraps `MarketQuoteV3Api.get_market_quote_ohlc`
- Defaults to batches of 10 instruments
- Halves failed batches recursively until single-instrument requests
- Remaps trading-symbol response keys to requested `NSE_FO|<id>` keys
- Uses a dedicated quote rate limiter
- Accepts SDK interval codes and human aliases
- Provides status, sample, batch, and full-universe HTTP endpoints

### Instrument search service

- Wraps Upstox `GET /v2/instruments/search` using `httpx`
- Supports free-text search across exchanges and segments
- Filters by exchange, segment, instrument type, expiry, and ATM offset
- Provides paginated results with up to 30 records per page
- Provides convenience endpoints to resolve keys and fetch ATM option chains
- Includes streamer subscribe and unsubscribe helpers with multiple fallbacks
- Available through GET query parameters and POST JSON bodies

### Order book service

- Wraps `OrderApi.get_order_book`
- Handles v1 and v2 SDK method signatures
- Serializes SDK model objects recursively into JSON-safe dictionaries
- Normalizes camelCase and snake_case field names
- Always returns a structured success or error dictionary

### Instrument API

- Resolves contracts by instrument key or strike and option type
- Supports optional index and expiry filters
- Returns historical candles, intraday candles, or both
- Returns historic and intraday crossovers
- Uses readonly snapshots first when `source=auto`
- Provides a full orchestrated refresh endpoint

### Web dashboard

- Dashboard home (`/`) with live index cards, option-chain tables, hard refresh, and market-hours awareness
- Charts list (`/charts`) with search, category pills, expiry filters, reset, and pagination
- Per-contract chart (`/chart/{key}`) with candlesticks, EMA lines, crossover markers, Opening Range levels, live ticks, and subscription lifecycle
- Isolated EMA dashboard (`/isolated-dashboard`) for opening-range and EMA-touch monitoring
- Orders (`/orders`) with table/card views, filters, sorting, search, and status modal
- Logs (`/logs`) with file browser, search, zoom, wrapping, line numbers, and theme toggle

### Telegram subsystem

- Global `TELEGRAM_ENABLED` master switch
- Per-event notification flags
- Long-polling bot with stale-update filtering
- Secure `/save_token` prompt flow that deletes token messages
- Automatic hard refresh after a successful token save
- Commands: `/status`, `/save_token`, `/refresh`, `/cancel`, `/help`
- Uses urllib HTTP transport without a Telegram Python package

### Operational

- Rotating file log at `logs/app.log`
- `.env`-based configuration with no secrets in source code
- Graceful FastAPI lifespan startup and shutdown
- Token-gated feature initialization
- POSIX folder permissions with Windows fallback logging

## 3. Architecture

```text
main.py
   └─ create_app()
        ├─ web/page_router            (HTML pages)
        ├─ core/health router
        ├─ token_tasks/router
        ├─ upstox_app/api/router
        ├─ upstox_app/streamer/ws_router
        ├─ upstox_app/option/option_router
        ├─ upstox_app/candle/candle_router
        ├─ upstox_app/market_quote/quote_router
        ├─ upstox_app/instruments_search/router
        ├─ api/ instruments, candles, crossovers, refresh
        ├─ web/api_router             (JSON APIs)
        └─ web/ws_router              (WebSocket hubs)
```

### Lifetime ownership

| Concern | Owner |
|---|---|
| MongoDB client | `core/config.py :: mongo_manager` |
| Logging | `core/logger.py` |
| Startup and shutdown | `core/lifespan.py` |
| Token cache | `token_tasks/service.py :: token_service` |
| Token scheduler | `token_tasks/scheduler.py` |
| Token health and watchdog | `token_tasks/health_check.py`, `token_tasks/token_watchdog.py` |
| Profile resolver | `token_tasks/_profile.py` |
| Market and portfolio streamers | `upstox_app/market/`, `upstox_app/portfolio/` |
| Streamer lifecycle | `upstox_app/streamer/streamer_manager.py` |
| WebSocket fanout | `upstox_app/streamer/ws_manager.py`, `ws_router.py` |
| Option chains | `upstox_app/option/` |
| Candles and crossovers | `upstox_app/candle/` |
| Market quotes | `upstox_app/market_quote/` |
| Instrument search | `upstox_app/instruments_search/` |
| Order book | `upstox_app/order_book_service.py` |
| Instrument API | `api/instruments_api.py` |
| Telegram | `telegram_app/` |
| Upstox configuration | `upstox_app/common/config.py :: upstox_config` |
| Web templates and APIs | `web/` |
| Page routes | `web/page_router.py` |
| JSON APIs | `web/api_router.py` |
| WebSocket hubs | `web/ws_router.py` |
| Template data helpers | `web/service.py` |

### Threading model

FastAPI runs an asyncio event loop. The Upstox SDK uses background threads for WebSocket callbacks. SDK callbacks use `asyncio.run_coroutine_threadsafe()` to reach the event loop. Singleton services use `threading.RLock`. Candle fetching uses a `ThreadPoolExecutor`, and candle and quote fetching have independent rate limiters. The Telegram bot runs on a daemon thread and schedules async work through `telegram_manager.schedule()`.

### Web layer

The `web/` package is intentionally thin. It serves templates, JSON APIs, and WebSocket fanout hubs. It never duplicates business logic. Every data call routes into `core/`, `token_tasks/`, `upstox_app/`, or `api/`.

```text
HTTP request ──► web/page_router ──► web/service ──► project services / disk snapshots
              ├─► web/api_router  ──► web/service ──► project services
              └─► web/ws_router   ──► /all-feeds, /ws/ema-crossover, /ws/opening-range
```

## 4. Folder Structure

```text
multi_index_v2/
├── .env
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── main.py
├── run.bat
├── start.sh
├── stop.sh
├── create_file.py
├── update_project.py
├── api/
├── core/
├── token_tasks/
├── telegram_app/
├── upstox_app/
│   ├── order_book_service.py
│   ├── api/
│   ├── candle/
│   ├── common/
│   ├── instruments_search/
│   ├── market/
│   ├── market_quote/
│   ├── option/
│   ├── portfolio/
│   ├── profile/
│   └── streamer/
├── web/
│   ├── api_router.py
│   ├── config.py
│   ├── page_router.py
│   ├── service.py
│   ├── ws_router.py
│   ├── __init__.py
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── chart.html
│       ├── instrument_list.html
│       ├── isolated_ema_dashboard.html
│       ├── orders.html
│       └── show_logs.html
├── data/
│   ├── readonly/
│   │   ├── options/
│   │   ├── NIFTY/candles/
│   │   └── SENSEX/candles/
│   └── runtime/
│       ├── options/
│       ├── refresh_status.json
│       ├── NIFTY/
│       └── SENSEX/
├── logs/app.log
├── output/
├── refs/
├── temp/
└── tests/
```

## 5. Requirements

- Python 3.10 or newer, tested on Python 3.12
- MongoDB 4.4 or newer
- Upstox account with a valid access token
- Network access to Upstox WebSocket and REST endpoints
- Optional Telegram bot token and chat ID

```text
fastapi
uvicorn[standard]
pymongo
python-dotenv
upstox-python-sdk
jinja2
python-multipart
httpx
```

## 6. Installation

```bash
git clone <repo-url> multi_index_v2
cd multi_index_v2
python -m venv .venv

# Windows
.venv\Scriptsctivate

# POSIX
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and provide `MONGO_URI`, `MONGO_DB_NAME`, and the required application settings.

## 7. Environment Variables

The application is configured entirely through `.env`. Major groups include app/logging, MongoDB, token tasks, watchdog, Telegram, indexes, streamers, options, candles, crossovers, and quotes.

Accepted quote interval values are `I1`, `I1_5`, `I1_15`, `I1_30`, `I1_H`, `I1_D`, `I1_W`, and `I1_MO`. Aliases `1m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w`, and `1mo` are normalized.

## 8. Running the App

```bash
# Development
python main.py

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **Important:** Use one worker. Token cache, WebSocket registry, streamers, option cache, candle cache, crossover cache, rate limiters, and the Telegram bot are in-process singletons.

- Web dashboard: `http://localhost:8000/`
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 9. Startup Flow

```text
1. Configure logging and create the FastAPI app.
2. Initialize Telegram.
3. Capture the running event loop for Telegram scheduling.
4. Connect to MongoDB.
5. Load the token into cache.
6. Run the token health gate.
7. Start the Telegram command bot.
8. Start the token watchdog.
9. Ensure storage folders and hydrate fresh option snapshots.
10. Start the token refresh scheduler.
11. Optionally connect market and portfolio streamers.
12. Optionally subscribe enabled indexes.
13. Load and persist option chains.
14. Ensure missing or stale candle files.
15. Compute historic and intraday crossovers.
16. Start the daily candle refresh scheduler.
17. Optionally bulk-subscribe option keys.
18. Mark the app ready.
19. Reverse startup order during shutdown.
```

## 10. Web UI Pages

All pages extend `web/templates/base.html`, which provides the top navigation bar, dark theme, Bootstrap 5.3, and Bootstrap Icons.

### `/` — Dashboard home

- Live cards for every enabled index with LTP, change, percentage change, and OHLC
- CE / Strike / PE option chain with LTP, change, OI, ATM highlighting, and index tabs
- IST market-hours awareness and next-session messaging
- Manual hard refresh through `POST /refresh/manual`

### `/charts` — Chart instruments

- Free-text search across symbol, name, ISIN, and strike
- Category filters for Index, Options, Futures, Equities, Commodities, and Currency
- Expiry filters, reset button, and server-side pagination
- Card navigation to `/chart/<instrument_key>`

### `/chart/{instrument_key}` — Per-contract chart

- Lightweight Charts candlesticks
- EMA 9 and EMA 21 lines with BUY/SELL crossover markers
- Opening Range HIGH, AVG, LOW, R2, R3, S2, and S3 levels
- Live subscription, LTP, and WebSocket state
- Index, Options, and Search tabs
- Instrument metadata card and chart-settings offcanvas
- Previous 7-day history loading
- Subscribe on load and idempotent unsubscribe on close

### `/isolated-dashboard` — Isolated EMA dashboard

- API, EMA WebSocket, and Opening Range WebSocket connection status
- EMA mode, isolated state, and alert metrics
- Current isolated instrument and Opening Range status
- Backfill evaluation and diagnostic table
- Alert history, touch events, live event feed, and raw JSON snapshot

### `/orders` — Today's orders

- Summary cards for totals, completed, rejected, pending, buy, and sell
- Search and filters for status, side, product, and tag
- Sortable table and card views
- Optional 15-second auto-refresh
- Status-message modal

### `/logs` — Log browser

- Searchable log file list with size and color badges
- Colorized timestamps and log levels
- Search highlighting, zoom, wrapping, line numbers, copy, and refresh
- Persisted dark/light theme toggle

## 11. HTTP API Reference

### Health and token

`GET /`, `GET /health`, `GET /token/status`, `GET /token/doc`, `POST /token/save`, `POST /token/validate`, `POST /token/validate-cached`, `POST /token/refresh`

No endpoint returns the token, user ID, or user name.

### Streamers

`GET /upstox/streamers`, `POST /upstox/streamers/start`, `POST /upstox/streamers/stop`, plus market and portfolio connect, disconnect, reconnect, message, subscription, and status routes under `/upstox/`.

### Options, candles, crossovers, and instruments

Routes include option cache/load/contract lookup, candle status/ensure, instrument status/contract/candle/crossover lookup, and `POST /api/instruments/refresh`.

### Market quotes

- `GET /upstox/quotes/status`
- `GET /upstox/quotes/sample`
- `POST /upstox/quotes/batch`
- `POST /upstox/quotes/all`

### Instrument search

- `GET|POST /upstox/instruments/search`
- `GET /upstox/instruments/resolve`
- `GET /upstox/instruments/option-chain/atm`
- `POST /upstox/instruments/subscribe`
- `POST /upstox/instruments/unsubscribe`
- `GET /upstox/instruments/search/status`

Search supports `query`, `exchanges`, `segments`, `instrument_types`, `expiry`, `atm_offset`, `page_number`, and `records`.

### UI JSON APIs

- `GET /api/chart/candles`
- `GET /api/older-candles`
- `GET /api/orders`
- `GET /ui/logs/{filename}`
- `GET /refresh/status`
- `POST /refresh/manual`
- `GET /opening-range/dashboard`
- `POST /opening-range/fetch`
- `POST /opening-range/isolated-instrument/manual`
- `GET /api/strategies`

## 12. WebSocket Protocol

### `/all-feeds`

Flat tick fanout used by dashboard and chart pages.

```text
ws://<host>:<port>/all-feeds
```

The client may send `{"action":"ping"}`. The server sends a ping message every 25 seconds of silence.

### `/ws/market`

```text
ws://<host>:<port>/ws/market
ws://<host>:<port>/ws/market?keys=NSE_INDEX|Nifty 50,BSE_INDEX|SENSEX
```

```json
{"action":"ping"}
{"action":"subscribe","instrument_keys":["NSE_EQ|INE020B01018"],"mode":"full","subscribe_upstream":true}
{"action":"unsubscribe","instrument_keys":["NSE_EQ|INE020B01018"],"unsubscribe_upstream":false}
{"action":"set-filter","instrument_keys":["NSE_INDEX|Nifty 50"]}
{"action":"clear-filter"}
{"action":"status"}
```

### `/ws/ema-crossover`

Emits `live_ema_cross` messages containing instrument key, cross type, close, fast EMA, slow EMA, and calculation mode.

### `/ws/opening-range`

Emits `opening_range_touch` messages containing instrument key, level, trigger field, trigger price, and main-index LTP.

## 13. Data Storage Layout

Readonly storage contains option references and per-contract candle files. Runtime storage contains writable snapshots, crossovers, and refresh state.

A candle file is stale when it is missing, unsuccessful, has no historical candles, has an outdated `to_date`, or references a different contract expiry.

```text
data/runtime/<INDEX>/<strike>_<TYPE>/historic_cross.json
data/runtime/<INDEX>/<strike>_<TYPE>/intraday_cross.json
data/runtime/refresh_status.json
```

## 14. Telegram Subsystem

`TELEGRAM_ENABLED=false` disables sending and polling. `true` enables the subsystem when credentials exist. When unset, Telegram is active only when both credentials are configured.

Commands include `/help`, `/start`, `/status`, `/save_token`, `/refresh`, and `/cancel`. Pending updates are drained at startup, and messages older than the process start are rejected. The watchdog sends invalid-token alerts, cooldown reminders, and recovery notifications.

## 15. How Things Work

### Token cache

```text
Startup -> load_token() -> MongoDB lookup -> cache
Every 30 minutes -> refresh_token()
POST /token/save -> validate -> upsert -> reload -> notify -> hard refresh
Telegram /save_token -> validate -> upsert -> reload -> hard refresh
```

### Streamer lifecycle

```text
connect() -> configure token -> create SDK streamer -> attach callbacks
          -> connect -> wait for on_open
on_open() -> mark connected -> release waiters -> apply intents
subscribe() -> store intent -> apply immediately when connected
on_message() -> ring buffer -> thread-safe asyncio fanout
```

### Instrument search

Search requests use `httpx` against the Upstox instruments search API with the current token. Responses are normalized across naming conventions and errors are returned as structured dictionaries rather than raised.

### Chart candle loading

The chart loads intraday one-minute candles and historical one-minute candles in windows of at most seven days. Results are merged and deduplicated by timestamp, and zero-volume placeholders are removed.

### Chart subscription lifecycle

```text
Page load  -> subscribe instrument
           -> fetch intraday and historical candles
           -> render chart
           -> connect /all-feeds
Page close -> send unsubscribe beacon
           -> fall back to keepalive fetch
```

### Market-hours awareness

The dashboard and chart use IST, Monday through Friday, 09:00 to 15:40. Outside that window, `/all-feeds` is disconnected, subscriptions are not sent, closed-state messaging is shown, and the state is reevaluated every 30 seconds.

### Rate limiting

Candle and quote calls use separate limiters. HTTP 429 responses trigger shared-worker cooldowns that grow from 30 to 60, 120, 240, and 300 seconds. A successful request resets the hit counter.

## 16. Development Notes

### Adding an index

Extend `upstox_app/common/config.py` and `MAIN_INDEXES`. The data pipelines and web dashboard discover the new index through `web/service.py::index_meta()`.

### Adding a feature package

```text
myfeature/
├── __init__.py
├── config.py
├── schemas.py
├── service.py
└── router.py
```

### Adding a web page

1. Add a template under `web/templates/`.
2. Extend `base.html` and its title, head, content, and scripts blocks.
3. Add a route in `web/page_router.py`.
4. Add defensive data helpers to `web/service.py`.
5. Add JSON APIs to `web/api_router.py` when needed.
6. Add WebSocket hubs to `web/ws_router.py` when needed.

### Logging

```python
from core.logger import get_logger
logger = get_logger(__name__)
```

```text
YYYY-MM-DD HH:MM:SS | LEVEL | logger.name | message
```

The rotating handler uses 5 MB per file and retains five backups.

### Manual testing

```bash
curl "http://localhost:8000/"
curl -X POST http://localhost:8000/token/save -H "Content-Type: application/json" -d '{"access_token":"eyJ...","source":"manual"}'
curl http://localhost:8000/token/status
curl -X POST http://localhost:8000/upstox/options/load
curl -X POST http://localhost:8000/upstox/candles/ensure
curl -X POST http://localhost:8000/api/instruments/refresh
curl "http://localhost:8000/upstox/quotes/sample?index=NIFTY&count=20"
curl "http://localhost:8000/upstox/instruments/search?query=RELIANCE&exchanges=NSE&segments=FO&instrument_types=CE,PE&expiry=current_month&atm_offset=0&page_number=1&records=20"
curl "http://localhost:8000/api/orders"
curl "http://localhost:8000/refresh/status"
curl -X POST "http://localhost:8000/refresh/manual"
```

## 17. Troubleshooting

### Socket is already closed during startup subscribe

Increase `UPSTOX_CONNECT_TIMEOUT_SEC` when the SDK does not invoke `on_open` before the timeout.

### Subscribe deferred: market closed

This is expected outside Monday to Friday, 09:00 to 15:40 IST. The intent remains stored and is applied on the next open connection.

### Token source is none

Save a token through `POST /token/save` or Telegram `/save_token`. Search, order book, and option-chain operations require a token.

### Candle loading receives Cloudflare 429

Reduce workers, increase request delay, reduce candle days, increase cooldown, and keep request windows at seven days or less.

### Web dashboard pages render but nothing updates

Confirm `/all-feeds` is connected, the current time is inside the IST market window, and a valid token has been saved.

### `/charts` shows no enabled indexes

Ensure `MAIN_INDEXES` is populated and `web/service.py::index_meta()` returns configured index metadata.

### Chart fails to render

The browser may block direct historical-candle requests through CORS. Add a server-side proxy route and point the chart page to it when required.

### Option tab shows no contracts

Load options or run the full instrument refresh. Confirm underlying names match `MAIN_INDEXES`. The search fallback may also require a different query such as `NIFTY` instead of `NIFTY 50`.

### Orders page shows no orders

Confirm the token is valid, the Upstox SDK is installed, and inspect logs for the order-book API error message.

### Logs page is empty

Confirm `LOG_DIR` points to the correct folder and `logs/app.log` exists and is receiving entries.
