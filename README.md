# UpstoxAppV2

A FastAPI service that manages Upstox authentication tokens, exposes real-time market and portfolio data via HTTP and WebSocket, handles option-chain loading and subscription for enabled indexes such as NIFTY and SENSEX, and maintains a readonly cache of per-contract historical and intraday candles with a daily refresh scheduler.

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
10. [HTTP API Reference](#10-http-api-reference)
11. [WebSocket Protocol](#11-websocket-protocol)
12. [Data Storage Layout](#12-data-storage-layout)
13. [How Things Work](#13-how-things-work)
14. [Development Notes](#14-development-notes)
15. [Troubleshooting](#15-troubleshooting)

## 1. Overview

UpstoxAppV2 is a long-running service that:

- Loads an Upstox access token from MongoDB into an in-memory cache
- Keeps that cache fresh on startup/restart and every 30 minutes
- Validates tokens via the Upstox User Profile API
- Maintains a WebSocket connection to Upstox's market and portfolio feeds
- Fans out live market messages to downstream WebSocket clients
- Loads option contracts (CE and PE) for every enabled index in a configured strike range
- Loads historical (10-day default, 7-day per request) and intraday candles per option contract
- Refreshes the candle cache daily at 09:00 IST when contract expiries roll over
- Optionally bulk-subscribes every fetched option key to the market streamer
- Exposes a lookup API for contracts and candles by instrument key or strike/type
- Persists option and candle snapshots to disk with a readonly and runtime folder split

The service is designed around one rule: **tokens and user identity never leave the server**. No HTTP endpoint returns the raw access token, user ID, or user name.

## 2. Features

### Token management

- Load, save, and validate Upstox tokens via REST
- In-memory cache loaded from MongoDB on boot
- Auto-refresh every 30 minutes, configurable
- Safe responses: no token or user identity is ever returned
- Validation delegated to `upstox_app/profile/get_profile_status.py`

### Market data streaming

- Upstream connection via `MarketDataStreamerV3`
- Idempotent subscribe, unsubscribe, and change-mode operations
- Out-of-market-hours aware: operations are deferred, not failed
- Wait-for-open handshake so callers can subscribe immediately after connecting
- Ring buffer of recent messages, retrievable over HTTP

### Downstream WebSocket

- `/ws/market` fans out every upstream message to all connected clients
- Optional per-client filter using `?keys=A,B`
- Broadcasts topology events for subscribe, unsubscribe, and change-mode operations
- Thread-safe fanout from SDK callback threads

### Option chain service

- Loads contracts per enabled index, with `MAIN_INDEXES` as the source of truth
- Filters by `start_instrument_range` and `end_instrument_range`
- Keeps CE and PE contracts
- Uses nearest expiry by default, or all expiries when configured
- Persists per-index snapshots to runtime and readonly folders
- Supports startup hydration from fresh disk snapshots
- Can bulk-subscribe every fetched option key to the market streamer

### Candle service

- Historical fetch via `HistoryV3Api.get_historical_candle_data1`, batched into windows of at most seven days
- Intraday fetch via `HistoryV3Api.get_intra_day_candle_data`
- No access token required for either call
- Parallel per-contract fetches through `ThreadPoolExecutor`
- Process-wide `RateLimiter` that pauses every worker on Cloudflare 429 responses, with exponential backoff and retry
- Readonly, write-once per-contract JSON files named `<strike>_<CE|PE>.json`
- Double-check on startup: only missing files are fetched, and existing files are skipped
- Daily refresh scheduler at 09:00 IST that rewrites files whose stored `expiry` no longer matches the live contract
- Progress logging every N completions, with full, partial, and failed roll-up counters

### Instrument API

- Lookup a contract by `instrument_key` or by `strike` and `option_type`, with optional `index` and `expiry`
- Historical candles for a contract, default window `today-7` through `yesterday`
- Intraday candles for a contract, today only
- Combined historical and intraday data in one response
- `source=auto` uses the readonly snapshot as a fast path and falls back to Upstox

### Portfolio streamer

- `PortfolioDataStreamer` wrapper using the same wait-for-open and ring-buffer pattern

### Operational

- Rotating file logger at `logs/app.log`
- Configuration through `.env` only, with no secrets in code
- Graceful startup and shutdown through FastAPI lifespan
- Per-folder access control on POSIX systems, with Windows fallback logging

## 3. Architecture

```text
main.py
   └─ create_app()
        ├─ registers core/health router
        ├─ registers token_tasks/router
        ├─ registers api/ (instruments + candles)
        ├─ registers upstox_app/api/router (streamers HTTP)
        ├─ registers upstox_app/candle/candle_router
        ├─ registers upstox_app/option/option_router
        └─ registers upstox_app/streamer/ws_router (WebSocket fanout)
```

### Lifetime ownership

| Concern | Owner |
|---|---|
| MongoDB client | `core/config.py :: mongo_manager` |
| Logging | `core/logger.py` |
| Startup / shutdown | `core/lifespan.py` |
| Token cache | `token_tasks/service.py :: token_service` |
| Token scheduler | `token_tasks/scheduler.py` |
| Upstox profile validation | `upstox_app/profile/get_profile_status.py` |
| Market streamer | `upstox_app/market/market_streamer.py` |
| Portfolio streamer | `upstox_app/portfolio/portfolio_streamer.py` |
| WebSocket fanout | `upstox_app/streamer/ws_manager.py` and `ws_router.py` |
| Option chains | `upstox_app/option/option_service.py` and `option_storage.py` |
| Candles | `upstox_app/candle/candle_service.py`, `candle_storage.py`, and `candle_scheduler.py` |
| Instrument API | `api/instruments_api.py` |
| Upstox config | `upstox_app/common/config.py :: upstox_config` |
| Market hours | `upstox_app/common/market_hours.py` |

### Threading model

- FastAPI runs an asyncio event loop.
- The Upstox SDK uses background threads for its WebSocket.
- SDK callbacks use `asyncio.run_coroutine_threadsafe` to reach the event loop.
- Every singleton uses `threading.RLock` for internal state.
- Candle fetching uses a `ThreadPoolExecutor` sized by `OPTIONS_CANDLES_MAX_WORKERS`, with a per-thread `HistoryV3Api` client.
- A process-wide `RateLimiter` gates every upstream history call.

## 4. Folder Structure

```text
multi_index_v2/
├── .env
├── .env.example
├── requirements.txt
├── main.py
├── run.bat
├── api/
│   ├── __init__.py
│   ├── instruments_api.py
│   └── schemas.py
├── core/
│   ├── __init__.py
│   ├── config.py
│   ├── logger.py
│   ├── lifespan.py
│   └── health.py
├── token_tasks/
│   ├── __init__.py
│   ├── config.py
│   ├── service.py
│   ├── scheduler.py
│   ├── schemas.py
│   ├── validator.py
│   └── router.py
├── upstox_app/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   └── schemas.py
│   ├── candle/
│   │   ├── __init__.py
│   │   ├── candle_router.py
│   │   ├── candle_scheduler.py
│   │   ├── candle_schemas.py
│   │   ├── candle_service.py
│   │   └── candle_storage.py
│   ├── common/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── market_hours.py
│   ├── market/
│   │   ├── __init__.py
│   │   └── market_streamer.py
│   ├── option/
│   │   ├── __init__.py
│   │   ├── option_router.py
│   │   ├── option_schemas.py
│   │   ├── option_service.py
│   │   └── option_storage.py
│   ├── portfolio/
│   │   ├── __init__.py
│   │   └── portfolio_streamer.py
│   ├── profile/
│   │   ├── __init__.py
│   │   └── get_profile_status.py
│   └── streamer/
│       ├── __init__.py
│       ├── streamer_manager.py
│       ├── ws_manager.py
│       └── ws_router.py
├── data/
│   ├── readonly/
│   │   ├── options/
│   │   │   ├── NIFTY.json
│   │   │   └── SENSEX.json
│   │   ├── NIFTY/candles/
│   │   │   ├── 22500_CE.json
│   │   │   └── 22500_PE.json
│   │   └── SENSEX/candles/
│   │       └── 70000_CE.json
│   └── runtime/options/
│       ├── NIFTY.json
│       ├── SENSEX.json
│       └── _meta.json
├── logs/app.log
├── output/
├── refs/
├── temp/
└── tests/
```

## 5. Requirements

- Python 3.10 or newer, tested on Python 3.12
- MongoDB 4.4 or newer
- An Upstox account with a valid access token
- Network access to Upstox WebSocket and REST endpoints

```text
fastapi
uvicorn[standard]
pymongo
python-dotenv
upstox-python-sdk
```

## 6. Installation

```bash
git clone <repo-url> multi_index_v2
cd multi_index_v2
python -m venv .venv

# Windows
.venv\Scripts\activate

# POSIX
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with `MONGO_URI`, `MONGO_DB_NAME`, and the required application settings.

## 7. Environment Variables

### App, logging, MongoDB, and token task

| Variable | Default | Purpose |
|---|---:|---|
| `APP_NAME` | `UpstoxAppV2` | FastAPI title |
| `APP_VERSION` | `1.0.0` | FastAPI version |
| `APP_HOST` | `0.0.0.0` | Uvicorn host |
| `APP_PORT` | `8000` | Uvicorn port |
| `DEBUG` | `false` | Uvicorn reload |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `LOG_DIR` | `logs` | Log directory |
| `LOG_FILE` | `app.log` | Log file name |
| `MONGO_URI` | none | MongoDB connection string |
| `MONGO_DB_NAME` | none | Database name |
| `MONGO_SERVER_SELECTION_TIMEOUT_MS` | `5000` | Connection timeout |
| `TOKEN_COLLECTION_NAME` | `tokens` | Token collection |
| `TOKEN_DOC_ID` | `upstox_access_token` | Token document `_id` |
| `TOKEN_REFRESH_INTERVAL_SECONDS` | `1800` | Cache refresh cadence |
| `TOKEN_LOAD_ON_STARTUP` | `true` | Load token on boot |

### Indexes and market hours

| Variable | Default | Purpose |
|---|---:|---|
| `NIFTY_ENABLED` | `true` | Enable NIFTY |
| `NIFTY_START_INSTRUMENT_RANGE` | `22500` | NIFTY lower strike |
| `NIFTY_END_INSTRUMENT_RANGE` | `24500` | NIFTY upper strike |
| `NIFTY_OPENING_RANGE` | `300` | NIFTY opening range |
| `SENSEX_ENABLED` | `true` | Enable SENSEX |
| `SENSEX_START_INSTRUMENT_RANGE` | `70000` | SENSEX lower strike |
| `SENSEX_END_INSTRUMENT_RANGE` | `85000` | SENSEX upper strike |
| `SENSEX_OPENING_RANGE` | `500` | SENSEX opening range |
| `MARKET_OPEN_TIME` | `09:00` | Session start in IST |
| `MARKET_CLOSE_TIME` | `15:40` | Session end in IST |

### Streamers and options

| Variable | Default | Purpose |
|---|---:|---|
| `UPSTOX_MSG_BUFFER_SIZE` | `500` | Ring buffer per streamer |
| `UPSTOX_CONNECT_TIMEOUT_SEC` | `15` | Wait-for-open timeout |
| `UPSTOX_AUTO_CONNECT_ON_STARTUP` | `false` | Connect on boot |
| `UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP` | `true` | Subscribe enabled indexes |
| `UPSTOX_DEFAULT_SUBSCRIPTION_MODE` | `full` | Index feed mode |
| `UPSTOX_AUTO_RECONNECT_ENABLED` | `true` | SDK auto-reconnect |
| `UPSTOX_AUTO_RECONNECT_INTERVAL_SEC` | `10` | Reconnect interval |
| `UPSTOX_AUTO_RECONNECT_RETRY_COUNT` | `3` | Reconnect retries |
| `OPTIONS_READONLY_DIR` | `data/readonly` | Readonly root |
| `OPTIONS_RUNTIME_DIR` | `data/runtime` | Runtime cache root |
| `OPTIONS_READONLY_MODE` | `555` | Readonly folder mode, octal |
| `OPTIONS_RUNTIME_MODE` | `755` | Runtime folder mode, octal |
| `OPTIONS_LOAD_ON_STARTUP` | `true` | Fetch option chains on boot |
| `OPTIONS_PERSIST_ON_LOAD` | `true` | Persist option snapshots |
| `OPTIONS_LOAD_RUNTIME_ON_STARTUP` | `true` | Hydrate option cache from disk |
| `OPTIONS_RUNTIME_MAX_AGE_SEC` | `86400` | Hydration freshness window |
| `OPTIONS_LOAD_ALL_EXPIRIES` | `false` | Keep all expiries |
| `OPTIONS_SUBSCRIBE_ALL_ON_STARTUP` | `false` | Subscribe all option keys |
| `OPTIONS_SUBSCRIBE_MODE` | `ltpc` | Option-key feed mode |
| `OPTIONS_SUBSCRIBE_BATCH_SIZE` | `500` | Keys per subscription call |
| `OPTIONS_STRIKE_RANGE_BUFFER` | `0` | Strike-range buffer |

### Candles and daily refresh

| Variable | Default | Purpose |
|---|---:|---|
| `OPTIONS_CANDLES_ENABLED` | `true` | Enable candles |
| `OPTIONS_CANDLES_LOAD_ON_STARTUP` | `true` | Fetch candles on boot |
| `OPTIONS_CANDLES_DAYS` | `10` | Historical window length |
| `OPTIONS_CANDLES_UNIT` | `minutes` | Candle unit |
| `OPTIONS_CANDLES_INTERVAL` | `1` | Candle interval |
| `OPTIONS_CANDLES_INCLUDE_INTRADAY` | `true` | Include today's candles |
| `OPTIONS_CANDLES_MAX_WORKERS` | `4` | Parallel workers |
| `OPTIONS_CANDLES_MAX_DAYS_PER_REQUEST` | `7` | Maximum historical window per call |
| `OPTIONS_CANDLES_REQUEST_DELAY_SEC` | `0.5` | Delay before each upstream call |
| `OPTIONS_CANDLES_PROGRESS_EVERY` | `10` | Progress-log frequency |
| `OPTIONS_CANDLES_RATE_LIMIT_MAX_RETRIES` | `3` | Retries after a 429 |
| `OPTIONS_CANDLES_RATE_LIMIT_BASE_COOLDOWN_SEC` | `30` | Initial cooldown |
| `OPTIONS_CANDLES_RATE_LIMIT_MAX_COOLDOWN_SEC` | `300` | Cooldown cap |
| `OPTIONS_CANDLES_DAILY_REFRESH_ENABLED` | `true` | Enable daily refresh |
| `OPTIONS_CANDLES_DAILY_REFRESH_TIME` | `09:00` | Refresh time in IST |
| `OPTIONS_CANDLES_RUN_IF_MISSED` | `true` | Run on startup if today's slot passed |

### Recommended baseline `.env`

```env
APP_NAME=UpstoxAppV2
APP_VERSION=1.0.0
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=false
LOG_LEVEL=INFO
LOG_DIR=logs
LOG_FILE=app.log
MONGO_URI=mongodb://localhost:27017
MONGO_DB_NAME=trading
MONGO_SERVER_SELECTION_TIMEOUT_MS=5000
TOKEN_COLLECTION_NAME=upstox_tokens
TOKEN_DOC_ID=upstox_access_token
TOKEN_REFRESH_INTERVAL_SECONDS=1800
TOKEN_LOAD_ON_STARTUP=true
NIFTY_ENABLED=true
NIFTY_START_INSTRUMENT_RANGE=22500
NIFTY_END_INSTRUMENT_RANGE=24500
NIFTY_OPENING_RANGE=300
SENSEX_ENABLED=true
SENSEX_START_INSTRUMENT_RANGE=70000
SENSEX_END_INSTRUMENT_RANGE=85000
SENSEX_OPENING_RANGE=500
MARKET_OPEN_TIME=09:00
MARKET_CLOSE_TIME=15:40
UPSTOX_MSG_BUFFER_SIZE=500
UPSTOX_CONNECT_TIMEOUT_SEC=15
UPSTOX_AUTO_CONNECT_ON_STARTUP=true
UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP=true
UPSTOX_DEFAULT_SUBSCRIPTION_MODE=full
UPSTOX_AUTO_RECONNECT_ENABLED=true
UPSTOX_AUTO_RECONNECT_INTERVAL_SEC=10
UPSTOX_AUTO_RECONNECT_RETRY_COUNT=3
OPTIONS_READONLY_DIR=data/readonly
OPTIONS_RUNTIME_DIR=data/runtime
OPTIONS_READONLY_MODE=555
OPTIONS_RUNTIME_MODE=755
OPTIONS_LOAD_ON_STARTUP=true
OPTIONS_PERSIST_ON_LOAD=true
OPTIONS_LOAD_RUNTIME_ON_STARTUP=true
OPTIONS_RUNTIME_MAX_AGE_SEC=86400
OPTIONS_LOAD_ALL_EXPIRIES=false
OPTIONS_SUBSCRIBE_ALL_ON_STARTUP=false
OPTIONS_SUBSCRIBE_MODE=ltpc
OPTIONS_SUBSCRIBE_BATCH_SIZE=500
OPTIONS_STRIKE_RANGE_BUFFER=0
OPTIONS_CANDLES_ENABLED=true
OPTIONS_CANDLES_LOAD_ON_STARTUP=true
OPTIONS_CANDLES_DAYS=10
OPTIONS_CANDLES_UNIT=minutes
OPTIONS_CANDLES_INTERVAL=1
OPTIONS_CANDLES_INCLUDE_INTRADAY=true
OPTIONS_CANDLES_MAX_WORKERS=4
OPTIONS_CANDLES_MAX_DAYS_PER_REQUEST=7
OPTIONS_CANDLES_REQUEST_DELAY_SEC=0.5
OPTIONS_CANDLES_PROGRESS_EVERY=10
OPTIONS_CANDLES_RATE_LIMIT_MAX_RETRIES=3
OPTIONS_CANDLES_RATE_LIMIT_BASE_COOLDOWN_SEC=30
OPTIONS_CANDLES_RATE_LIMIT_MAX_COOLDOWN_SEC=300
OPTIONS_CANDLES_DAILY_REFRESH_ENABLED=true
OPTIONS_CANDLES_DAILY_REFRESH_TIME=09:00
OPTIONS_CANDLES_RUN_IF_MISSED=true
```

## 8. Running the App

```bash
# Development
python main.py

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **Important:** Run with one worker. The token cache, WebSocket registry, streamers, option cache, candle cache, and rate limiter are in-process singletons. Multiple workers create multiple upstream connections, split fanout, and bypass the shared rate limiter.

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 9. Startup Flow

```text
1. Configure logging and create FastAPI app.
2. Start lifespan:
   a. Register event loop with ws_manager.
   b. Connect to MongoDB.
   c. Load token into cache.
   d. Ensure readonly and runtime folders.
   e. Hydrate fresh option snapshots.
   f. Start token scheduler.
   g. Optionally connect market and portfolio streamers.
   h. Optionally subscribe enabled indexes.
   i. Load enabled option chains and persist snapshots.
   j. Load missing per-contract candle files:
      - seven-day historical request windows
      - one intraday call per contract
      - parallel workers with global 429 cooldown
   k. Start daily candle refresh scheduler at 09:00 IST.
   l. Optionally bulk-subscribe all option keys.
3. App is ready.
4. Shutdown reverses ownership: candle scheduler, streamers,
   token scheduler, then MongoDB.
```

| Auto-connect | Indexes | Options | Candles | All options | Result |
|---|---|---|---|---|---|
| false | false | false | false | false | Fully manual |
| false | true | false | false | false | Connect only to subscribe indexes |
| true | true | true | false | false | Streamers and option cache, no candles |
| true | true | true | true | false | Full load pipeline, no bulk option subscribe |
| true | true | true | true | true | Full automatic pipeline |

## 10. HTTP API Reference

### Health and token

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | App name and version |
| `GET` | `/health` | Mongo, scheduler, and cache state |
| `GET` | `/token/status` | Safe cache metadata |
| `GET` | `/token/doc` | Safe token-document projection |
| `POST` | `/token/save` | Validate and save token |
| `POST` | `/token/validate` | Validate explicit or cached token |
| `POST` | `/token/validate-cached` | Validate cached token |
| `POST` | `/token/refresh` | Force MongoDB reload |

No endpoint returns the token, user ID, or user name.

### Streamers

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/upstox/streamers` | Both streamer statuses |
| `POST` | `/upstox/streamers/start` | Start both streamers |
| `POST` | `/upstox/streamers/stop` | Stop both streamers |
| `GET` | `/upstox/market/status` | Market status |
| `POST` | `/upstox/market/connect` | Connect market streamer |
| `POST` | `/upstox/market/disconnect` | Disconnect market streamer |
| `POST` | `/upstox/market/reconnect` | Reconnect with latest token |
| `POST` | `/upstox/market/subscribe` | Subscribe keys |
| `POST` | `/upstox/market/unsubscribe` | Unsubscribe keys |
| `POST` | `/upstox/market/change-mode` | Change mode |
| `GET` | `/upstox/market/messages` | Recent messages |
| `POST` | `/upstox/market/messages/clear` | Clear message buffer |
| `GET` | `/upstox/portfolio/status` | Portfolio status |
| `POST` | `/upstox/portfolio/connect` | Connect portfolio streamer |
| `POST` | `/upstox/portfolio/disconnect` | Disconnect portfolio streamer |
| `POST` | `/upstox/portfolio/reconnect` | Reconnect portfolio streamer |
| `GET` | `/upstox/portfolio/messages` | Recent portfolio messages |
| `POST` | `/upstox/portfolio/messages/clear` | Clear portfolio buffer |
| `GET` | `/upstox/ws/status` | WS clients and upstream keys |

### Options, candles, and instruments

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/upstox/options/cache` | Option cache summary |
| `POST` | `/upstox/options/load` | Load all enabled indexes |
| `POST` | `/upstox/options/load/{index_name}` | Load one index |
| `GET` | `/upstox/options/{index_name}/contracts` | Contracts by index |
| `GET` | `/upstox/options/contract/{instrument_key}` | Contract by key |
| `POST` | `/upstox/options/strike-window` | Contracts in strike window |
| `GET` | `/upstox/candles/status` | Candle configuration |
| `POST` | `/upstox/candles/ensure` | Fetch all missing candle files |
| `POST` | `/upstox/candles/ensure/{index_name}` | Fetch missing files for one index |
| `GET` | `/api/instruments/status` | Enabled indexes and defaults |
| `GET` | `/api/instruments/contract` | Resolve one contract |
| `GET` | `/api/instruments/candles/historical` | Historical candles |
| `GET` | `/api/instruments/candles/intraday` | Today's candles |
| `GET` | `/api/instruments/candles` | Historical and intraday |

Query semantics:

- `instrument_key` alone performs direct lookup.
- `strike` and `option_type` search enabled indexes.
- Add `index` and/or `expiry` to narrow the result.
- `from_date` and `to_date` default to `today-7` and `yesterday`.
- `source=auto|readonly|upstream` selects the candle source.

Subscription operations are idempotent. Existing subscriptions at the requested mode and missing unsubscriptions are returned in `skipped`; actual changes are returned in `applied`.

## 11. WebSocket Protocol

```text
ws://<host>:<port>/ws/market
ws://<host>:<port>/ws/market?keys=NSE_INDEX|Nifty 50,BSE_INDEX|SENSEX
```

### Server to client

```json
{"type":"welcome","conn_id":"ab12...","filter_keys":["NSE_INDEX|Nifty 50"],"clients":1,"upstream":{"connected":true,"subscribed_count":5}}
{"type":"market","instrument_key":"NSE_INDEX|Nifty 50","data":{"feeds":{}}}
{"type":"event","event":"upstream_subscribe","payload":{"keys":["NSE_EQ|INE020B01018"],"mode":"full","by":"http"}}
{"type":"ack","action":"subscribe","requested":["NSE_EQ|INE020B01018"],"applied":["NSE_EQ|INE020B01018"],"skipped":[]}
{"type":"pong"}
{"type":"status","conn_id":"ab12...","filter_keys":null,"upstream":{},"clients":3}
{"type":"error","message":"unknown action: foo"}
```

### Client to server

```json
{"action":"ping"}
{"action":"subscribe","instrument_keys":["NSE_EQ|INE020B01018"],"mode":"full","subscribe_upstream":true}
{"action":"unsubscribe","instrument_keys":["NSE_EQ|INE020B01018"],"unsubscribe_upstream":false}
{"action":"set-filter","instrument_keys":["NSE_INDEX|Nifty 50"]}
{"action":"clear-filter"}
{"action":"status"}
```

`filter_keys = None` receives all upstream data. A set receives only matching keys. Topology events are broadcast regardless of filters.

## 12. Data Storage Layout

`data/readonly/` contains locked option references and per-contract candles. Files are write-once unless daily expiry refresh detects that a stored contract expiry differs from the live contract. POSIX directories use `0555` and files use `0444`. Windows uses the read-only file attribute where possible.

`data/runtime/` contains writable option snapshots. Files use `0644`, directories use `0755`, and fresh snapshots can hydrate the in-memory cache at startup.

```text
data/readonly/
├── options/
│   ├── NIFTY.json
│   └── SENSEX.json
├── NIFTY/candles/
│   ├── 22500_CE.json
│   └── 22500_PE.json
└── SENSEX/candles/
    └── 70000_CE.json
```

### Candle file schema

```json
{
  "status": "success",
  "index_name": "NIFTY",
  "instrument_key": "NSE_FO|40712",
  "strike_price": 22750.0,
  "option_type": "CE",
  "expiry": "2026-10-06",
  "trading_symbol": "NIFTY 22750 CE 06 OCT 26",
  "unit": "minutes",
  "interval": "1",
  "days": 10,
  "from_date": "2026-09-23",
  "to_date": "2026-10-02",
  "fetched_at": "2026-10-04T00:11:22.123456+05:30",
  "historical_count": 2250,
  "intraday_count": 375,
  "windows_total": 2,
  "windows_failed": 0,
  "errors": [],
  "candles": [
    ["2026-09-23T09:15:00+05:30", 120.5, 121.2, 119.8, 120.9, 1500, 0]
  ]
}
```

The `expiry` field is the refresh signal. A mismatch causes the file to be rewritten.

### Runtime metadata

```json
{
  "loaded_indexes": ["NIFTY", "SENSEX"],
  "loaded_at": "2026-10-03T22:47:22.123456+05:30",
  "source": "upstream",
  "total_contracts": 1250
}
```

## 13. How Things Work

### Token cache

```text
Startup → load_token() → MongoDB lookup → populate cache
Every 30 minutes → refresh_token()
POST /token/save → validate → upsert → reload cache
POST /token/refresh → immediate reload
```

All consumers call `token_service.get_access_token()` and never read MongoDB directly.

### Streamer lifecycle

```text
connect() → configure cached token → create SDK streamer
          → attach callbacks → connect → wait for on_open
on_open() → mark connected → release waiters → apply subscription intents
subscribe() → store intent → apply immediately if connected
on_message() → ring buffer → thread-safe asyncio fanout
```

### Option chain lifecycle

```text
load_enabled_indexes()
  → fetch contracts for each MAIN_INDEXES entry
  → normalize fields
  → keep CE/PE in configured strike range
  → keep nearest expiry unless all-expiries is enabled
  → update cache
  → persist runtime and initial readonly snapshots
```

### Candle lifecycle

```text
Startup:
  ensure_all_enabled()
    → resolve contracts from memory or runtime fallback
    → identify missing candle files
    → fetch contracts in parallel
    → split history into seven-day windows
    → fetch intraday data
    → merge and save write-once JSON files

Daily at 09:00 IST:
  refresh_if_outdated(force_reload_options=True)
    → reload live option contracts
    → compare stored and live expiry per strike/type
    → refetch and overwrite only stale files
```

### Rate limiting

Every history call passes through `_call_with_retry`. On a 429, a process-wide cooldown pauses all workers. Consecutive responses increase the cooldown from 30 to 60, 120, 240, then 300 seconds. The affected window is retried up to `OPTIONS_CANDLES_RATE_LIMIT_MAX_RETRIES`. A successful call clears the consecutive-hit counter.

### Market hours

| State | Log level | Behavior |
|---|---|---|
| Monday to Friday, 09:00 to 15:40 IST | `ERROR` | Unexpected closure alert |
| Pre-market or post-market | `WARNING` | Deferred until open |
| Weekend | `WARNING` | Deferred until Monday |

Subscription intent remains stored and is applied by the next `on_open`.

## 14. Development Notes

### Adding an index

```env
BANKNIFTY_ENABLED=true
BANKNIFTY_START_INSTRUMENT_RANGE=50000
BANKNIFTY_END_INSTRUMENT_RANGE=55000
BANKNIFTY_OPENING_RANGE=400
```

Extend `upstox_app/common/config.py` and add the entry to `MAIN_INDEXES`. The index-subscription, option-load, candle-load, and option-subscription pipelines then discover it automatically.

### Logging

```python
from core.logger import get_logger
logger = get_logger(__name__)
```

```text
YYYY-MM-DD HH:MM:SS | LEVEL | logger.name | message
```

The rotating handler uses 5 MB per file, keeps five backups, and writes to `logs/app.log`.

### Manual testing

```bash
curl -X POST http://localhost:8000/token/save -H "Content-Type: application/json" -d '{"access_token":"eyJ...","source":"manual"}'
curl http://localhost:8000/token/status
curl -X POST http://localhost:8000/upstox/options/load
curl http://localhost:8000/upstox/options/cache
curl -X POST http://localhost:8000/upstox/candles/ensure
curl "http://localhost:8000/api/instruments/contract?strike=23000&option_type=CE"
curl "http://localhost:8000/api/instruments/candles/historical?index=NIFTY&strike=23000&option_type=CE"
curl "http://localhost:8000/api/instruments/candles/intraday?index=NIFTY&strike=23000&option_type=CE"
```

```bash
websocat ws://localhost:8000/ws/market
```

```json
{"action":"ping"}
{"action":"status"}
{"action":"subscribe","instrument_keys":["NSE_EQ|INE020B01018"],"mode":"full"}
{"action":"unsubscribe","instrument_keys":["NSE_EQ|INE020B01018"]}
```

## 15. Troubleshooting

### Socket is already closed during startup subscribe

`connect()` waits for `_opened_event`. If the SDK does not call `on_open` within `UPSTOX_CONNECT_TIMEOUT_SEC`, increase the timeout.

### Subscribe deferred: market closed

The intent is stored and applied by the next `on_open`. This is expected outside Monday to Friday, 09:00 to 15:40 IST.

### Token source is `none`

The cache is empty and no explicit token was supplied. Save one through `POST /token/save`, or pass `access_token` to validation.

### Option load fails for an index

Check the index's enabled flag, cached-token validity, Upstox connectivity, and configured strike range.

### `%d format: a real number is required, not tuple`

Old code expects `subscribe()` to return a number. The current function returns `(applied, skipped)`. Update `subscribe_enabled_indexes()` in `streamer_manager.py`.

### Missing `OPTIONS_READONLY_DIR` attribute

Option and candle settings belong to `upstox_app.common.config.upstox_config`, not `core.config.core_config`. Ensure `UpstoxAppConfig` declares:

```python
OPTIONS_READONLY_DIR = _env_str("OPTIONS_READONLY_DIR", "data/readonly")
OPTIONS_RUNTIME_DIR = _env_str("OPTIONS_RUNTIME_DIR", "data/runtime")
```

Candle storage may derive `data/readonly` from `data/runtime` as a defensive fallback and log a warning.

### Candle load receives Cloudflare 429

The shared rate limiter pauses all workers and retries with exponential cooldown. To reduce pressure:

- Halve `OPTIONS_CANDLES_MAX_WORKERS`
- Increase `OPTIONS_CANDLES_REQUEST_DELAY_SEC`
- Reduce `OPTIONS_CANDLES_DAYS` from 10 to 7
- Increase `OPTIONS_CANDLES_RATE_LIMIT_BASE_COOLDOWN_SEC`
- Do not set `OPTIONS_CANDLES_MAX_DAYS_PER_REQUEST` above 7

### Candle file is missing

Verify candle loading is enabled, option loading succeeded, and the summary logs show the contract. Run `POST /upstox/candles/ensure/{index_name}` and check `data/readonly/<INDEX>/candles/<strike>_<CE|PE>.json`.

### Daily refresh did not run at 09:00 IST

Verify the daily scheduler is enabled. When the app starts after 09:00 and `OPTIONS_CANDLES_RUN_IF_MISSED=true`, it should run once on startup. Inspect `Candle daily refresh starting`, `Candle daily refresh done`, and scheduler next-fire logs. Times use fixed IST, UTC+05:30.

### MongoDB connection fails

Verify `MONGO_URI`, `MONGO_DB_NAME`, network access, firewall, and VPN settings. The service logs the failure and continues, but token-dependent streamers remain unavailable.

### Windows storage-permission errors

Windows `chmod` does not enforce directory ACLs. The app logs the limitation and continues. Candle storage temporarily clears a target's readonly attribute before replacement because `os.replace()` cannot overwrite a readonly NTFS target.

### Upstream disconnect storms

Verify auto-reconnect is enabled, retry values are reasonable, and the access token has not expired. `Market streamer auto-reconnect halted` means retries were exhausted.

## Summary of Changes

1. Added the candle subsystem, instrument API, and daily refresh behavior.
2. Updated architecture for the split `upstox_app` package layout.
3. Replaced the folder structure with the current project tree.
4. Added all `OPTIONS_CANDLES_*` and daily-refresh settings.
5. Extended startup flow and behavior matrix with candle loading.
6. Added the candle and instrument HTTP APIs.
7. Documented per-contract candle storage and schemas.
8. Added candle lifecycle and shared rate-limiting behavior.
9. Expanded troubleshooting for configuration, 429 responses, missing candle files, refresh scheduling, and Windows replacement behavior.
