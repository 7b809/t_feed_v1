markdown
# UpstoxAppV2

A single-process FastAPI service that:

- Manages Upstox authentication tokens (REST + Telegram)
- Streams live market and portfolio data over WebSocket
- Loads and subscribes CE / PE option contracts for enabled indexes
- Maintains per-contract candle files (readonly) and derives 9/21 EMA crossovers
- Runs a **live** EMA engine (`ema_app`) that aggregates ticks into 1-minute
  bars, detects crosses, persists them, and broadcasts over WebSocket
- Runs an **isolation layer** (`ema_app/isolation`) that picks one option
  contract per index per day from opening-range touches and can place Upstox
  market orders on its EMA crosses
- Serves a full web dashboard: live index cards, option chain, instrument
  search, per-contract charts, isolated-EMA monitoring, orders, logs
- Runs a token health watchdog with Telegram alerts

**Security rule:** tokens and user identity never leave the server. No HTTP
endpoint returns the raw access token, user ID, or user name.

---

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
10. [The Two EMA Engines](#10-the-two-ema-engines)
11. [The Isolation Layer](#11-the-isolation-layer)
12. [Web UI Pages](#12-web-ui-pages)
13. [HTTP API Reference](#13-http-api-reference)
14. [WebSocket Protocol](#14-websocket-protocol)
15. [Data Storage Layout](#15-data-storage-layout)
16. [Telegram Subsystem](#16-telegram-subsystem)
17. [How Things Work](#17-how-things-work)
18. [Development Notes](#18-development-notes)
19. [Troubleshooting](#19-troubleshooting)
20. [Deltas vs. Previous README](#20-deltas-vs-previous-readme)

---

## 1. Overview

UpstoxAppV2 is a **single-worker**, long-running FastAPI service. It owns:

- **Token cache** — loaded from MongoDB on boot, refreshed every 30 minutes,
  validated via User Profile API, watched by a health watchdog.
- **Upstream streamers** — `MarketDataStreamerV3` (market) and
  `PortfolioDataStreamer` (portfolio) with idempotent subscribe /
  unsubscribe / change-mode, wait-for-open handshake, and market-hours
  deferral.
- **Downstream WS fanout** — flat tick fanout on `/all-feeds`, filtered
  fanout on `/ws/market`, plus per-topic hubs.
- **Option chains** — one snapshot per enabled index, filtered by strike
  range and expiry, persisted to `data/runtime/options/`.
- **Candle cache** — readonly per-contract files under
  `data/readonly/<INDEX>/candles/<strike>_<CE|PE>.json`, refreshed daily at
  09:00 IST.
- **Batch EMA engine** — `upstox_app/candle/crossover_service.py` computes
  9/21 crosses from stored candle files on startup, daily refresh, and hard
  refresh. Output: `historic_cross.json` / `intraday_cross.json` under
  `data/runtime/<INDEX>/<strike>_<TYPE>/`.
- **Live EMA engine** — `ema_app/` ingests ticks from the streamer,
  aggregates them into 1-minute bars, detects 9/21 crosses on candle close,
  and broadcasts them on `/ws/ema-app`.
- **Isolation layer** — `ema_app/isolation/` picks **one** option contract
  per index per day from opening-range touches, enriches its crosses into an
  alert payload, optionally places Upstox market orders, and persists placed
  orders to MongoDB.
- **Web dashboard** — six pages served by `web/page_router.py`:
  `/`, `/charts`, `/chart/{key}`, `/isolated-dashboard`, `/orders`, `/logs`.

---

## 2. Features

### Token management
- Load / save / validate tokens via REST or Telegram
- In-memory token cache, refreshed every 30 minutes
- No endpoint returns token, user ID, or user name
- Profile validation through `token_tasks/_profile.py` with SDK fallback
- Token health watchdog with Telegram alerts on invalid / recovered

### Market data streaming
- Upstream connection through `MarketDataStreamerV3`
- Idempotent subscribe / unsubscribe / change-mode
- Defer subscription operations outside market hours; queue intents
- Wait-for-open handshake for immediate subscription
- Ring buffer of recent messages exposed over HTTP
- Bulk-subscribe enabled indexes and option keys after refresh

### Downstream WebSocket
- `/all-feeds` — flat tick fanout used by dashboard and charts
- `/ws/market` — filtered fanout, optional `?keys=A,B`
- `/ws/ema-app` — live EMA crosses and isolated alerts
- Topology events for subscribe / unsubscribe / mode changes
- Thread-safe fanout from SDK callback threads

### Option chain service
- Loads contracts for indexes in `MAIN_INDEXES`
- Filters by configured strike range and expiry
- Retains CE and PE contracts
- Nearest expiry by default, all expiries when configured
- Persists to `data/runtime/options/<INDEX>.json` (single runtime tree —
  **no readonly tree for options**)
- Hydrates cache from fresh disk snapshots at startup
- Bulk-subscribes fetched option keys

### Candle service (batch)
- Historical candles via `HistoryV3Api.get_historical_candle_data1`,
  windows ≤ 7 days
- Intraday via `HistoryV3Api.get_intra_day_candle_data`
- `ThreadPoolExecutor` for parallel per-contract fetches
- Process-wide `RateLimiter` for Cloudflare 429s
- Readonly per-contract files at
  `data/readonly/<INDEX>/candles/<strike>_<CE|PE>.json`
- Stale rules: missing, empty, unsuccessful, outdated `to_date`, or
  different contract expiry
- Daily refresh scheduler at 09:00 IST
- Chart page fetches candles **directly** from the public Upstox v3 candle
  endpoint (no token) — see §12

### Batch EMA crossover service
- Configurable periods (default 9/21)
- Saves only crossover candles
- Outputs `historic_cross.json` and `intraday_cross.json`
- Recomputed on startup, daily refresh, hard refresh
- Histogram-split: historic (before today) vs intraday (today)

### Live EMA engine (`ema_app`)
- Tick ingestion → 1-minute bar aggregation → 9/21 cross detection on close
- Session scheduler: arm at 09:14 IST, start at 09:15, finalize per minute
  at minute + `EMA_APP_FINALIZE_DELAY_SEC`, close at 15:30
- Restart-safe: backfill on boot reconstructs opening-range levels and
  re-detects all intraday crosses from disk + today's intraday
- Persists crosses to per-contract `intraday_cross.json` **including
  `opening_range_levels`**
- Broadcasts `live_ema_cross` on `/ws/ema-app`
- Listener hooks: `register_cross_listener`, `register_candle_listener`

### Isolation layer (`ema_app/isolation`)
- Per-index daily winner from opening-range touches
- Selection priority: `r3/s3` (60) > `r2/s2` (40) > `r1/s1` (20); tiebreak
  by earliest touch, then smallest distance to index LTP
- Richer alert payload (`schema_version 1.0`) on isolated instrument crosses:
  instrument, opening_range, market_snapshot, ema, order_suggestion,
  duplicate_control, delivery
- Optional order placement:
  - `ISOLATED_INSTRUMENT_ORDERS_ONLY=true` → order the isolated instrument
    itself (bullish → BUY; bearish → SELL only if
    `ISOLATED_INSTRUMENT_PLACE_SELL_ON_BEARISH=true`)
  - `false` → BUY up to `EMA_ALERT_MAX_ORDERS_PER_ALERT` budget-filtered
    instruments
- Master switches: `EMA_ALERT_ORDERS_ENABLED` (default `false`),
  `EMA_ALERT_ORDER_DRY_RUN` (default `true`)
- Dedupe window per `(instrument_key, direction)`
- Persists placed orders to MongoDB collection `ema_isolated_orders`
  (one document per day, orders keyed `HH_MM_SS[_N]`), fail-open by default
- Persists daily isolation state to `data/runtime/isolation/<INDEX>.json`

### Market quote service
- Wraps `MarketQuoteV3Api.get_market_quote_ohlc`
- Batches of 10 by default, halves failed batches recursively
- Remaps trading-symbol response keys to requested `NSE_FO|<id>`
- Dedicated quote rate limiter
- Accepts SDK interval codes and human aliases (`1m`, `5m`, `1d`, …)

### Instrument search service
- Wraps Upstox `GET /v2/instruments/search` using `httpx`
- Filters by exchange, segment, instrument type, expiry, ATM offset
- Paginated results (up to 30 per page)
- Convenience endpoints: resolve key, ATM option chain
- Streamer subscribe / unsubscribe helpers with fallbacks

### Order book service
- Wraps `OrderApi.get_order_book`
- Handles v1 and v2 SDK signatures
- Recursively serializes SDK models into JSON-safe dicts
- Normalises camelCase and snake_case field names
- Always returns a structured success / error dict

### Web dashboard
- `/` — live index cards, CE/strike/PE option chain, hard refresh,
  client-side market-hours gate
- `/charts` — search + category pills + expiry filter + reset + pagination
- `/chart/{key}` — candlesticks, EMA 9/21, crossover markers, opening-range
  levels, live ticks, subscribe-on-load / unsubscribe-on-close
- `/isolated-dashboard` — per-index winners, live cross feed, embedded
  isolated-instrument chart, force backfill
- `/orders` — table/card views, filters, sorting, status modal,
  15 s auto-refresh
- `/logs` — file browser, search, zoom, wrap, line numbers, theme toggle

### Telegram subsystem
- Global `TELEGRAM_ENABLED` master switch
- Per-event flags
- Long-polling bot with stale-update filtering
- `/save_token` flow deletes token messages; triggers hard refresh on save
- Commands: `/help`, `/start`, `/status`, `/save_token`, `/refresh`,
  `/cancel`
- Uses `urllib` HTTP transport — no Telegram Python package required

### Operational
- Rotating log at `logs/app.log` (5 MB × 5 backups)
- `.env`-based config; no secrets in source
- Graceful FastAPI lifespan startup/shutdown
- Token-gated feature initialization
- POSIX folder permissions with Windows fallback logging

---

## 3. Architecture

```text
main.py
   └─ create_app()
        ├─ web/page_router                       (HTML pages)
        ├─ core/health router
        ├─ token_tasks/router
        ├─ upstox_app/api/router                 (streamer control)
        ├─ upstox_app/streamer/ws_router         (/ws/market)
        ├─ upstox_app/option/option_router
        ├─ upstox_app/candle/candle_router
        ├─ upstox_app/market_quote/quote_router
        ├─ upstox_app/instruments_search/router
        ├─ api/                                  (instruments, candles,
        │                                         crossovers, refresh)
        ├─ ema_app/router                        (/ema-app/*)
        ├─ ema_app/isolation/router              (/ema-app/isolation/*)
        ├─ ema_app/ws_router                     (/ws/ema-app)
        ├─ web/api_router                        (UI JSON APIs)
        └─ web/ws_router                         (/all-feeds,
                                                  /ws/ema-crossover,
                                                  /ws/opening-range)
Lifetime ownership
Concern	Owner
MongoDB client	core/config.py :: mongo_manager
Logging	core/logger.py
Startup and shutdown	core/lifespan.py
Token cache	token_tasks/service.py :: token_service
Token scheduler	token_tasks/scheduler.py
Token health / watchdog	token_tasks/health_check.py, token_tasks/token_watchdog.py
Profile resolver	token_tasks/_profile.py
Market / portfolio streamers	upstox_app/market/, upstox_app/portfolio/
Streamer lifecycle	upstox_app/streamer/streamer_manager.py
Downstream WS fanout	upstox_app/streamer/ws_manager.py, ws_router.py
Option chains	upstox_app/option/
Candles (batch)	upstox_app/candle/candle_service.py, candle_storage.py
Crossover (batch)	upstox_app/candle/crossover_service.py, crossover_storage.py
Market quotes	upstox_app/market_quote/
Instrument search	upstox_app/instruments_search/
Order book	upstox_app/order_book_service.py
Instrument API	api/instruments_api.py
Live EMA engine	ema_app/service.py :: ema_service
Live EMA scheduler	ema_app/scheduler.py
Live EMA WS hub	ema_app/ws_manager.py, ws_router.py
Isolation	ema_app/isolation/service.py :: isolation_service
Isolation state	ema_app/isolation/state.py :: isolation_store
Isolation orders	ema_app/isolation/order_service.py, place_order.py
Order storage	ema_app/isolation/order_storage.py :: isolated_order_storage
Telegram	telegram_app/
Web templates / APIs	web/
Page routes	web/page_router.py
JSON APIs	web/api_router.py
WebSocket hubs	web/ws_router.py
Template data helpers	web/service.py
Threading model
FastAPI runs an asyncio event loop.

The Upstox SDK uses background threads for WebSocket callbacks.

SDK callbacks reach the event loop via
asyncio.run_coroutine_threadsafe().

Singleton services use threading.RLock.

Candle fetching uses ThreadPoolExecutor.

Candle and quote fetching use independent rate limiters.

The Telegram bot runs on a daemon thread; schedules async work via
telegram_manager.schedule().

The live EMA engine ingests ticks from SDK threads; state mutations are
guarded by an RLock per EmaService.

The isolation layer runs synchronously inside the base engine's
register_candle_listener / register_cross_listener hooks; it is safe
to call from any thread.

Web layer
The web/ package is thin: it serves templates, JSON APIs, and WebSocket
hubs. It never duplicates business logic — every data call routes into
core/, token_tasks/, upstox_app/, api/, or ema_app/.

text
HTTP request ──► web/page_router ──► web/service ──► project services / disk snapshots
              ├─► web/api_router  ──► web/service ──► project services
              └─► web/ws_router   ──► /all-feeds, /ws/ema-crossover, /ws/opening-range
4. Folder Structure
text
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
├── api/                                    (instruments, candles,
│                                            crossovers, refresh)
├── core/                                   (config, logger, lifespan,
│                                            health)
├── token_tasks/                            (cache, scheduler, watchdog,
│                                            profile resolver)
├── telegram_app/                           (long-polling bot)
├── upstox_app/
│   ├── order_book_service.py
│   ├── api/                                (streamer HTTP control)
│   ├── candle/                             (batch candle + crossover)
│   ├── common/                             (config, market_day,
│   │                                        market_hours)
│   ├── instruments_search/                 (Upstox v2 search wrapper)
│   ├── market/                             (market_streamer.py)
│   ├── market_quote/                       (MarketQuoteV3 wrapper)
│   ├── option/                             (option_router, service,
│   │                                        storage, schemas)
│   ├── portfolio/                          (portfolio_streamer.py)
│   ├── profile/                            (get_profile_status.py)
│   └── streamer/                           (streamer_manager,
│                                            ws_manager, ws_router)
├── ema_app/                                (LIVE EMA ENGINE)
│   ├── __init__.py
│   ├── config.py
│   ├── opening_range.py
│   ├── scheduler.py
│   ├── schemas.py
│   ├── service.py
│   ├── state.py
│   ├── ws_manager.py
│   ├── ws_router.py
│   └── isolation/                          (ISOLATION + ORDERS)
│       ├── __init__.py
│       ├── config.py
│       ├── order_service.py
│       ├── order_storage.py
│       ├── payload.py
│       ├── place_order.py
│       ├── router.py
│       ├── selector.py
│       ├── service.py
│       ├── state.py
│       └── touch_detector.py
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
│   ├── readonly/                           (candles only)
│   │   ├── NIFTY/candles/
│   │   ├── SENSEX/candles/
│   │   └── <INDEX>/candles/
│   └── runtime/
│       ├── options/                        (option snapshots)
│       │   ├── NIFTY.json
│       │   ├── SENSEX.json
│       │   └── _meta.json
│       ├── isolation/                      (per-index isolation state)
│       │   ├── NIFTY.json
│       │   └── SENSEX.json
│       ├── refresh_status.json
│       ├── NIFTY/<strike>_<TYPE>/
│       │   ├── historic_cross.json
│       │   └── intraday_cross.json
│       └── SENSEX/<strike>_<TYPE>/
│           ├── historic_cross.json
│           └── intraday_cross.json
├── logs/app.log
├── output/
├── refs/
├── temp/
└── tests/
5. Requirements
Python 3.10+ (tested on 3.12)

MongoDB 4.4+

Upstox account with a valid access token

Network access to Upstox WebSocket and REST endpoints

Optional Telegram bot token and chat ID

text
fastapi
uvicorn[standard]
pymongo
python-dotenv
upstox-python-sdk
jinja2
python-multipart
httpx
6. Installation
bash
git clone <repo-url> multi_index_v2
cd multi_index_v2
python -m venv .venv

# Windows
.venv\Scripts\activate

# POSIX
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
Edit .env and provide at minimum:

MONGO_URI

MONGO_DB_NAME

UPSTOX_ACCESS_TOKEN (or supply via POST /token/save / Telegram)

7. Environment Variables
Configuration is entirely .env-driven. Major groups below.

Global
LOG_DIR, LOG_LEVEL, MARKET_TIMEZONE (default Asia/Kolkata)

MONGO_URI, MONGO_DB_NAME

TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Token tasks
TOKEN_REFRESH_INTERVAL_SEC

TOKEN_HEALTH_INTERVAL_SEC, TOKEN_WATCHDOG_*

Indexes
MAIN_INDEXES (populated in upstox_app/common/config.py)

Streamers
UPSTOX_MSG_BUFFER_SIZE

UPSTOX_CONNECT_TIMEOUT_SEC

UPSTOX_AUTO_CONNECT_ON_STARTUP

UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP

UPSTOX_DEFAULT_SUBSCRIPTION_MODE (ltpc | full | option_greeks | full_d30)

UPSTOX_AUTO_RECONNECT_ENABLED, _INTERVAL_SEC, _RETRY_COUNT

Options
OPTIONS_READONLY_DIR (retained for candle compat)

OPTIONS_RUNTIME_DIR (default data/runtime)

OPTIONS_READONLY_MODE, OPTIONS_RUNTIME_MODE

OPTIONS_LOAD_ON_STARTUP, OPTIONS_PERSIST_ON_LOAD

OPTIONS_LOAD_RUNTIME_ON_STARTUP, OPTIONS_RUNTIME_MAX_AGE_SEC

OPTIONS_LOAD_ALL_EXPIRIES, OPTIONS_DEFAULT_NEAREST_EXPIRY_ONLY

OPTIONS_SUBSCRIBE_ALL_ON_STARTUP, OPTIONS_SUBSCRIBE_MODE,
OPTIONS_SUBSCRIBE_BATCH_SIZE, OPTIONS_STRIKE_RANGE_BUFFER

Candles
OPTIONS_CANDLES_ENABLED, OPTIONS_CANDLES_LOAD_ON_STARTUP

OPTIONS_CANDLES_DAYS (default 10)

OPTIONS_CANDLES_UNIT (default minutes), OPTIONS_CANDLES_INTERVAL
(default 1)

OPTIONS_CANDLES_INCLUDE_INTRADAY

OPTIONS_CANDLES_MAX_WORKERS, OPTIONS_CANDLES_MAX_DAYS_PER_REQUEST

OPTIONS_CANDLES_FETCH_DELAY_SEC

OPTIONS_CANDLES_RATE_LIMIT_BASE_COOLDOWN_SEC (default 30)

OPTIONS_CANDLES_RATE_LIMIT_MAX_COOLDOWN_SEC (default 300)

OPTIONS_CANDLES_RATE_LIMIT_MAX_RETRIES

OPTIONS_CANDLES_DAILY_REFRESH_ENABLED, _TIME (default 09:00)

OPTIONS_CANDLES_PROGRESS_EVERY

Batch crossovers
CROSSOVER_ENABLED

CROSSOVER_EMA_FAST (default 9), CROSSOVER_EMA_SLOW (default 21)

CROSSOVER_CALC_ON_STARTUP, CROSSOVER_CALC_ON_DAILY_REFRESH

Market quotes
QUOTES_ENABLED

QUOTES_BATCH_SIZE (default 10)

QUOTES_INTERVAL (default I1)

QUOTES_REQUEST_DELAY_SEC

QUOTES_RATE_LIMIT_BASE_COOLDOWN_SEC, _MAX_COOLDOWN_SEC

Live EMA engine (ema_app)
EMA_APP_ENABLED

EMA_APP_FAST_PERIOD (default 9), EMA_APP_SLOW_PERIOD (default 21)

EMA_APP_MARKET_OPEN (default 09:15), EMA_APP_MARKET_CLOSE (default
15:30)

EMA_APP_ARM_OFFSET_MIN (default 1)

EMA_APP_FINALIZE_DELAY_SEC (default 15)

EMA_APP_BACKFILL_ON_START

EMA_APP_BACKFILL_COOLDOWN_SEC (default 120)

EMA_APP_BACKFILL_INTER_REQUEST_DELAY_SEC

EMA_APP_INTRADAY_HTTP_TIMEOUT_SEC

EMA_APP_PERSIST_CROSSES

EMA_APP_BROADCAST_ENABLED

EMA_APP_USE_STREAMER

Isolation layer
EMA_ISOLATION_ENABLED (default true)

EMA_ALERT_ORDERS_ENABLED (default false)

ISOLATED_INSTRUMENT_ORDERS_ONLY (default true)

ISOLATED_INSTRUMENT_PLACE_SELL_ON_BEARISH (default false)

EMA_ALERT_MAX_ORDERS_PER_ALERT (default 2)

EMA_ALERT_ORDER_DEFAULT_QUANTITY (default 65)

EMA_ALERT_ORDER_PRODUCT (default D)

EMA_ALERT_ORDER_VALIDITY (default DAY)

EMA_ALERT_ORDER_TAG (default EMA_ISOLATED)

EMA_ALERT_ORDER_TRANSACTION_TYPE (default BUY)

EMA_ALERT_ORDER_DRY_RUN (default true)

EMA_ALERT_ORDER_DEDUPE_WINDOW_SEC (default 60)

EMA_ALERT_ORDER_STORAGE_ENABLED (default true)

EMA_ALERT_ORDER_STORAGE_FAIL_OPEN (default true)

EMA_ALERT_ORDER_STORAGE_COLLECTION (default ema_isolated_orders)

EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED

Market hours
MARKET_OPEN_TIME (default 09:00)

MARKET_CLOSE_TIME (default 15:40)

Quote interval codes
SDK: I1, I1_5, I1_15, I1_30, I1_H, I1_D, I1_W, I1_MO
Aliases: 1m, 5m, 15m, 30m, 1h, 1d, 1w, 1mo

8. Running the App
bash
# Development
python main.py

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Important: use one worker. Token cache, WS registry, streamers,
option cache, candle cache, batch crossover cache, live EMA state,
isolation state, rate limiters, and the Telegram bot are in-process
singletons.

Web dashboard: http://localhost:8000/

Swagger UI: http://localhost:8000/docs

ReDoc: http://localhost:8000/redoc

9. Startup Flow
text
 1. Configure logging and create the FastAPI app.
 2. Initialize Telegram.
 3. Capture the running event loop for thread-safe fanout.
 4. Connect to MongoDB.
 5. Load the token into cache.
 6. Run the token health gate.
 7. Start the Telegram command bot.
 8. Start the token watchdog.
 9. Ensure storage folders; hydrate fresh option snapshots.
10. Start the token refresh scheduler.
11. Optionally connect market and portfolio streamers.
12. Optionally subscribe enabled indexes.
13. Load and persist option chains.
14. Ensure missing or stale candle files.
15. Compute batch historic and intraday crossovers.
16. Start the daily candle refresh scheduler (09:00 IST).
17. Optionally bulk-subscribe option keys.
18. Start the live EMA engine:
       - register instruments from option cache
       - attach isolation listener (register_candle_listener)
       - attach cross listener (register_cross_listener)
       - start ema_app/scheduler.py session loop
19. Mark the app ready.
20. Reverse startup order during shutdown:
       - stop live EMA scheduler, isolation
       - stop candle scheduler
       - stop streamers
       - stop token watchdog / Telegram
       - close MongoDB
10. The Two EMA Engines
The project runs two independent EMA engines. They share the same
concept (9/21 EMA cross on 1-minute close) but differ in inputs, seeding,
output paths, and consumers.

Aspect	Batch engine	Live engine
Package	upstox_app/candle/crossover_service.py	ema_app/service.py
Trigger	Startup, daily refresh 09:00 IST, hard refresh	Tick ingestion + minute finalize
Input	candle_storage readonly files	Upstream streamer ticks + _fetch_intraday backfill
EMA seed	SMA of first period closes	First close value
Output file	historic_cross.json + intraday_cross.json	intraday_cross.json (also writes opening_range_levels)
WS topic	/ws/ema-crossover (README-claimed)	/ws/ema-app (implemented)
Listener hooks	None	register_cross_listener, register_candle_listener
Both engines write to the same intraday_cross.json path. If both run
on the same session, the last writer wins. The live engine adds
opening_range_levels; the batch engine does not.

Known inconsistency: EMA seeding differs (SMA vs first close). The same
input candles can yield different detected crosses between the two engines.

Known inconsistency: the client-side chart templates compute opening
range as
R1 = H, S1 = L, Δ = (H−L)/2, R2 = H + Δ/2, R3 = H + Δ, S2 = L − Δ/2, S3 = L − Δ,
whereas ema_app/opening_range.py uses
A = (H+L)/2, D_H = |H−A|, D_L = |A−L|, R1 = A + D_H/2, S1 = A − D_L/2, R2 = H + D_H, ….
R2/R3/S2/S3 agree; R1/S1 disagree.

11. The Isolation Layer
ema_app/isolation/ is an additive layer that consumes the live EMA
engine's candle and cross events.

Selection
Every finalized candle for a tracked option updates that contract's own
opening range (from its first candle of the day).

A touch is detected when the candle's high reaches a resistance
(prev_close < level <= high) or its low reaches a support
(prev_close > level >= low).

Touches are pooled per index (isolation_store.add_candidate).

The winner (isolated) is chosen by:

Higher level priority (r3/s3=60 > r2/s2=40 > r1/s1=20)
Earliest touch_time
Smallest distance_from_index (|strike − index LTP|)
Alert
When the isolated instrument produces an EMA cross, isolation_service.on_cross:

Builds a rich alert payload (build_payload) with schema_version 1.0

Updates isolated.cross_count / last_cross_*

Calls the registered broadcast listener (usually the EMA WS hub)

Calls place_orders_for_isolated_ema_alert(payload)

Persists each placed order to MongoDB

Saves isolation state to disk

Order modes
ISOLATED_INSTRUMENT_ORDERS_ONLY=true (default)

bullish cross → BUY the isolated instrument

bearish cross → SELL only if
ISOLATED_INSTRUMENT_PLACE_SELL_ON_BEARISH=true, else skip

ISOLATED_INSTRUMENT_ORDERS_ONLY=false

BUY up to EMA_ALERT_MAX_ORDERS_PER_ALERT budget-filtered instruments
(from payload.order_suggestion.budget_filter.instruments)

Safety
Two independent master switches gate real order placement:

EMA_ALERT_ORDERS_ENABLED (default false) — nothing is ever
placed when disabled

EMA_ALERT_ORDER_DRY_RUN (default true) — logs intent but does
not call the Upstox API

Both must be flipped to place live orders.

Dedupe
Per (instrument_key, direction), within
EMA_ALERT_ORDER_DEDUPE_WINDOW_SEC (default 60). Stale entries pruned
periodically.

Persistence
State: data/runtime/isolation/<INDEX>.json (session-date gated,
rehydrates on restart)

Orders: MongoDB ema_isolated_orders, one document per YYYY-MM-DD,
orders keyed HH_MM_SS (with _N suffix on collision), fail-open

12. Web UI Pages
All pages extend web/templates/base.html: dark theme, Bootstrap 5.3.3,
Bootstrap Icons 1.11.3, glassmorphism top nav.

/ — Dashboard home
Header banner with market-status pill + /all-feeds pill + index count

Live index cards (LTP, change, %, OHLC)

CE / Strike / PE option chain (LTP, change, OI, ATM highlight)

Index tabs row with per-tab LTP

Manual hard refresh (POST /refresh/manual) + status console

Client-side market-hours gate (IST virtual clock, Mon–Fri 09:00–15:40,
re-evaluated every 30 s): /all-feeds is only opened when open;
subscriptions are only sent when open

/charts — Chart instruments
Free-text search across symbol, name, ISIN, strike

Category pills: All, Index, Options, Futures, Equities, Commodities, Currency

Expiry filter: current week / next week / current month / next month

Reset button (disabled when state matches defaults)

Server-side pagination (20 per page)

Card navigation to /chart/<instrument_key>

/chart/{instrument_key} — Per-contract chart
Lightweight Charts candlesticks

EMA 9 / EMA 21 lines, BUY/SELL crossover markers (persisted toggles via
localStorage['chart.indicators.prefs'])

Opening-range levels: HIGH, AVG, LOW, R2, R3, S2, S3 (R1/S1 computed
but not drawn)

Label toggles: R2/S2, R3/S3, HIGH/LOW (AVG always on)

Instrument panel: Index / Options / Search tabs, fold state persisted to
sessionStorage['chart.instrumentPanel.collapsed']

Instrument info card, chart settings offcanvas

Historical 7-day windows loaded on scroll-to-end

Candles fetched directly from the public Upstox v3 candle endpoint
(https://api.upstox.com/v3/historical-candle/...) — no token required
from the browser

Subscribe on load, unsubscribe on close (navigator.sendBeacon fallback)

/isolated-dashboard — Isolated EMA dashboard
Per-index cards (SELECTED / WAITING + candidate count)

Embedded isolated-instrument chart (window.IsolatedChart module) with
same EMA / opening-range features

Metrics: connection, isolation engine, isolated status, total candidates

Index tabs → detail (selected instrument, trigger, reference average)

Opening-range touch table + crosses-on-isolated table

Selection history table

Live WebSocket events feed (from /ws/ema-app)

Raw isolation snapshot (collapsed)

Force Backfill → POST /ema-app/backfill?force=true

15 s auto-refresh toggle

/orders — Today's orders
Summary cards: Total, Complete, Rejected, Pending, Buy, Sell

Filters: search (symbol, order ID, token, tag), status, side, product, tag

Quick filter pills: All / Buy / Sell / Complete / Rejected / Pending

Table view and card view toggle

Sortable columns: order_timestamp, quantity, price

Status-message modal (status_message, status_message_raw)

15 s auto-refresh (pauses when tab hidden)

/logs — Log browser
File list with color-coded left borders

File-name filter, content search highlight

Zoom in/out, wrap toggle, line numbers, copy, refresh

Theme toggle persisted to localStorage['logTheme']

Auto-loads first file

13. HTTP API Reference
Health and token
GET /, GET /health, GET /token/status, GET /token/doc,
POST /token/save, POST /token/validate, POST /token/validate-cached,
POST /token/refresh

No endpoint returns the token, user ID, or user name.

Streamers
GET /upstox/streamers, POST /upstox/streamers/start,
POST /upstox/streamers/stop; plus market and portfolio connect /
disconnect / reconnect / message / subscription / status routes under
/upstox/.

Options, candles, crossovers, instruments
Option cache/load/contract lookup, candle status/ensure, instrument
status/contract/candle/crossover lookup,
POST /api/instruments/refresh.

Market quotes
GET /upstox/quotes/status

GET /upstox/quotes/sample?index=NIFTY&count=20

POST /upstox/quotes/batch

POST /upstox/quotes/all

Instrument search
GET|POST /upstox/instruments/search

GET /upstox/instruments/resolve

GET /upstox/instruments/option-chain/atm

POST /upstox/instruments/subscribe

POST /upstox/instruments/unsubscribe

GET /upstox/instruments/search/status

Search params: query, exchanges, segments, instrument_types,
expiry, atm_offset, page_number, records.

Live EMA engine
GET /ema-app/status

GET /ema-app/instruments

GET /ema-app/opening-range/{instrument_key}

GET /ema-app/state/{instrument_key}

GET /ema-app/crosses/today

GET /ema-app/crosses/{instrument_key}

POST /ema-app/backfill?force=true

POST /ema-app/finalize-now

Isolation layer
GET /ema-app/isolation/status

GET /ema-app/isolation/today

GET /ema-app/isolation/today/{index_name}

GET /ema-app/isolation/candidates/{index_name}

UI JSON APIs
GET /api/chart/candles

GET /api/older-candles

GET /api/orders

GET /ui/logs/{filename} (plain text)

GET /refresh/status

POST /refresh/manual

GET /opening-range/dashboard

POST /opening-range/fetch

POST /opening-range/isolated-instrument/manual

GET /api/strategies

14. WebSocket Protocol
/all-feeds
Flat tick fanout used by dashboard, chart, isolated dashboard.

text
ws://<host>:<port>/all-feeds
Server sends a ping every 25 seconds of silence

Client may send {"action":"ping"}

Payload shape varies: index cards expect info.instrument_type === "INDEX"
or key contains _INDEX|; option ticks expect info.instrument_type in
{"CE","PE"}

/ws/market
text
ws://<host>:<port>/ws/market
ws://<host>:<port>/ws/market?keys=NSE_INDEX|Nifty 50,BSE_INDEX|SENSEX
Client actions:

json
{"action":"ping"}
{"action":"subscribe","instrument_keys":["NSE_EQ|INE020B01018"],"mode":"full","subscribe_upstream":true}
{"action":"unsubscribe","instrument_keys":["NSE_EQ|INE020B01018"],"unsubscribe_upstream":false}
{"action":"set-filter","instrument_keys":["NSE_INDEX|Nifty 50"]}
{"action":"clear-filter"}
{"action":"status"}
Server messages: welcome, market, event, ack, pong, status,
error.

/ws/ema-app
Live EMA crosses and isolated alerts.

Client actions:

{"action":"ping"}

{"action":"subscribe","filter":{...}} or filters:[...]

{"action":"subscribe_all"}

{"action":"unsubscribe","filter":{...}}

{"action":"unsubscribe_all"}

{"action":"status"}

Filter fields (all optional, wildcard when omitted):
instrument_key, underlying, strike, option_type, expiry.

Server messages:

connected, subscribed, subscribed_all, unsubscribed,
unsubscribed_all, status, ping, pong

live_ema_cross — {type, instrument_key, underlying, strike, option_type, expiry, trading_symbol, cross_time, cross_time_iso, cross_type ("bullish_cross"|"bearish_cross"), close, ema_fast, ema_slow, ema_calculation_mode}

isolated_ema_alert — full alert payload (schema_version 1.0)

/ws/ema-crossover
README-claimed; emits live_ema_cross for the batch engine.
Not implemented in the current source tree — the live engine uses
/ws/ema-app.

/ws/opening-range
README-claimed; emits opening_range_touch.
Not implemented in the current source tree — opening-range touches are
surfaced via the isolation state and the isolated dashboard, not a dedicated
WS topic.

15. Data Storage Layout
Readonly tree — candles only
text
data/readonly/<INDEX>/candles/<strike>_<CE|PE>.json
Each file:

json
{
  "status": "success|empty|error",
  "index_name": "NIFTY",
  "instrument_key": "NSE_FO|...",
  "strike_price": 24000.0,
  "option_type": "CE",
  "expiry": "2026-10-09",
  "trading_symbol": "NIFTY 24000 CE ...",
  "unit": "minutes",
  "interval": "1",
  "days": 10,
  "from_date": "…",
  "to_date": "…",
  "fetched_at": "…",
  "historical_count": N,
  "intraday_count": M,
  "errors": [],
  "candles": [[ts, o, h, l, c, v], ...]
}
Stale when: missing, status != "success", historical_count == 0,
to_date older than the last market day, or expiry differs from the live
contract.

Runtime tree — options, crossovers, isolation, refresh status
text
data/runtime/options/<INDEX>.json
data/runtime/options/_meta.json
data/runtime/<INDEX>/<strike>_<TYPE>/historic_cross.json
data/runtime/<INDEX>/<strike>_<TYPE>/intraday_cross.json
data/runtime/isolation/<INDEX>.json
data/runtime/refresh_status.json
No readonly tree for options. Every successful option load overwrites
data/runtime/options/<INDEX>.json. The runtime tree is the single source.

MongoDB
tokens collection — token documents (raw token, source, timestamps)

ema_isolated_orders collection — one document per trading day, keyed
_id: "YYYY-MM-DD", orders as sub-docs keyed HH_MM_SS[_N]

16. Telegram Subsystem
TELEGRAM_ENABLED=false — disables sending and polling

TELEGRAM_ENABLED=true — enabled when credentials exist

Unset — active only when both credentials are configured

Commands: /help, /start, /status, /save_token, /refresh,
/cancel.

Pending updates are drained at startup; messages older than the process
start are rejected. /save_token deletes the user's message and triggers a
hard refresh on success.

The watchdog emits invalid-token alerts, cooldown reminders, and recovery
notifications.

Transport is urllib — no Telegram Python package required.

17. How Things Work
Token cache
text
Startup          -> load_token() -> MongoDB lookup -> cache
Every 30 minutes -> refresh_token()
POST /token/save -> validate -> upsert -> reload -> notify -> hard refresh
Telegram /save_token -> validate -> upsert -> reload -> hard refresh
Streamer lifecycle
text
connect() -> configure token -> create SDK streamer -> attach callbacks
          -> connect -> wait for on_open
on_open() -> mark connected -> release waiters -> apply intents
subscribe() -> store intent -> apply immediately when connected
on_message() -> ring buffer -> thread-safe asyncio fanout
Option loading
text
MAIN_INDEXES -> Upstox OptionsApi.get_option_contracts
             -> clean + filter (strike range, nearest expiry)
             -> cache in options_cache
             -> persist to data/runtime/options/<INDEX>.json
             -> bulk subscribe (optional)
Batch candle loading
text
per contract -> fetch historical (<=7-day windows)
             -> fetch intraday
             -> save readonly <strike>_<TYPE>.json
             -> log full/partial/failed
Batch crossover computation
text
per contract -> load candle file
             -> split historic (before today) / intraday (today)
             -> SMA-seeded 9/21 EMA
             -> detect crosses
             -> write historic_cross.json + intraday_cross.json
Live EMA engine
text
tick -> _on_tick -> minute bar aggregation
     -> minute boundary crossed -> _finalize_bar
     -> running first-close-seeded EMA update
     -> cross detection -> _on_cross
     -> persist intraday_cross.json (with opening_range_levels)
     -> notify cross listener (broadcast + isolation)
Isolation
text
candle -> on_candle -> update OR levels (first candle)
                    -> detect touches
                    -> add to candidate pool
                    -> re-evaluate winner (priority → time → distance)
cross  -> on_cross -> if instrument is isolated:
                        build payload
                        broadcast
                        place orders (per config)
                        persist orders to MongoDB
                        save isolation state
Chart candle loading (client)
text
Page load -> subscribe instrument
          -> fetch intraday candles directly from
             https://api.upstox.com/v3/historical-candle/intraday/<key>/minutes/1
          -> fetch 7-day historical window directly
          -> merge + dedupe + sort
          -> render chart
          -> connect /all-feeds
Page close -> unsubscribe beacon (sendBeacon, keepalive fallback)
Market-hours awareness
Server: upstox_app/common/market_hours.py — Mon–Fri 09:00–15:40 IST

Client (/): virtual IST clock, same window, re-evaluated every 30 s

Outside window: no /all-feeds connection, no subscriptions, closed-state
messaging

Rate limiting
Candle and quote calls use separate limiters. HTTP 429 triggers
shared-worker cooldowns growing 30 → 60 → 120 → 240 → 300 s. Successful
request resets the hit counter.

18. Development Notes
Adding an index
Extend upstox_app/common/config.py and MAIN_INDEXES

Ensure web/service.py::index_meta() returns metadata

Create folders: data/readonly/<INDEX>/candles/,
data/runtime/<INDEX>/, data/runtime/isolation/

Verify option strike-range config

Adding a feature package
text
myfeature/
├── __init__.py
├── config.py
├── schemas.py
├── service.py
└── router.py
Adding a web page
Add template under web/templates/

Extend base.html blocks: title, head, content, scripts

Add route in web/page_router.py

Add defensive data helpers to web/service.py

Add JSON APIs to web/api_router.py when needed

Add WebSocket hubs to web/ws_router.py when needed

Adding a live EMA listener
python
from ema_app.service import ema_service
ema_service.register_candle_listener(my_fn)   # every finalized bar
ema_service.register_cross_listener(my_fn)    # every cross
Adding an isolated broadcast hook
python
from ema_app.isolation.service import isolation_service
isolation_service.register_broadcast_listener(my_fn)   # full alert payload
Logging
python
from core.logger import get_logger
logger = get_logger(__name__)
Format: YYYY-MM-DD HH:MM:SS | LEVEL | logger.name | message
Rotating handler: 5 MB per file × 5 backups.

Manual testing
bash
curl "http://localhost:8000/"
curl -X POST http://localhost:8000/token/save \
     -H "Content-Type: application/json" \
     -d '{"access_token":"eyJ...","source":"manual"}'
curl http://localhost:8000/token/status
curl -X POST http://localhost:8000/upstox/options/load
curl -X POST http://localhost:8000/upstox/candles/ensure
curl -X POST http://localhost:8000/api/instruments/refresh
curl "http://localhost:8000/upstox/quotes/sample?index=NIFTY&count=20"
curl "http://localhost:8000/upstox/instruments/search?query=RELIANCE&exchanges=NSE&segments=FO&instrument_types=CE,PE&page_number=1&records=20"
curl "http://localhost:8000/api/orders"
curl "http://localhost:8000/refresh/status"
curl -X POST "http://localhost:8000/refresh/manual"
curl "http://localhost:8000/ema-app/status"
curl "http://localhost:8000/ema-app/isolation/status"
curl "http://localhost:8000/ema-app/isolation/today"
curl -X POST "http://localhost:8000/ema-app/backfill?force=true"
19. Troubleshooting
Socket is already closed during startup subscribe
Increase UPSTOX_CONNECT_TIMEOUT_SEC.

Subscribe deferred — market closed
Expected outside Mon–Fri 09:00–15:40 IST. Intent remains stored and applies
on next open.

Token source is none
Save a token via POST /token/save or Telegram /save_token. Search,
order book, and option chain all require a token.

Candle loading receives Cloudflare 429
Reduce workers, increase request delay, reduce candle days, increase
cooldown, keep request windows ≤ 7 days.

Dashboard pages render but nothing updates
Confirm /all-feeds is connected

Confirm current time is inside IST market window

Confirm a valid token has been saved

/charts shows no enabled indexes
Ensure MAIN_INDEXES is populated and
web/service.py::index_meta() returns configured index metadata.

Chart fails to render
The browser may block direct historical-candle requests through CORS. Add a
server-side proxy and point the chart to it.

Option tab shows no contracts
Load options or run the full instrument refresh. Confirm underlying names
match MAIN_INDEXES. Search fallback may require NIFTY rather than
NIFTY 50.

Orders page shows no orders
Confirm token is valid, Upstox SDK is installed, inspect logs for the
order-book API error.

Logs page is empty
Confirm LOG_DIR points to the correct folder and logs/app.log exists and
is receiving entries.

Isolation dashboard shows no winner
Confirm EMA_ISOLATION_ENABLED=true

Confirm ema_app scheduler is running

Confirm option metadata was registered

Confirm at least one opening-range touch has occurred today

Force backfill: POST /ema-app/backfill?force=true

Orders were not placed on an isolated cross
Confirm EMA_ALERT_ORDERS_ENABLED=true

Confirm EMA_ALERT_ORDER_DRY_RUN=false

Confirm EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED if Telegram is expected

Confirm the dedupe window hasn't swallowed the order

Inspect logs for place_market_order errors

/ws/ema-crossover or /ws/opening-range connect fails
These topics are not implemented in the current tree. Use /ws/ema-app
instead. See §20.

20. Deltas vs. Previous README
This README supersedes the previous one. The most important corrections:

New in this README
ema_app/ — live EMA engine (service, scheduler, WS hub, state,
opening-range, schemas). Not in the old README.

ema_app/isolation/ — isolation selection, alert payload builder,
order placement, order persistence, touch detector, selector. Not in the
old README.

/ws/ema-app WebSocket topic and its full message protocol.

Isolated-order MongoDB collection (ema_isolated_orders).

Daily isolation state files at data/runtime/isolation/<INDEX>.json.

Config groups for EMA_APP_* and EMA_ALERT_* /
ISOLATED_INSTRUMENT_*.

Client-side market-hours gate on /.

Direct-from-browser candle fetch on /chart/{key} and the isolated
dashboard chart (no token).

window.IsolatedChart module inside the isolated dashboard template.

Live EMA vs. batch EMA comparison table (§10).

Isolation algorithm and order safety switches (§11).

Known inconsistencies (EMA seeding, R1/S1 formula).

Corrected
Option storage: only data/runtime/options/<INDEX>.json exists. The
old README's claim of "readonly and runtime folder separation" for
options is wrong. Only candles use a readonly tree.

/ws/ema-crossover and /ws/opening-range are listed in the old
README as implemented. They are not in the current tree. The live
engine uses /ws/ema-app; opening-range touches are surfaced via isolation
state and the isolated dashboard, not a dedicated topic.

upstox_app/option/option_service.py returns a cache summary without
readonly_dir. Only runtime_dir is exposed.

get_profile_status.py always returns raw: None, even on success.

Latent issues worth flagging
These are places where the current source has drift that could cause bugs.
Fix before relying on them:

ema_app/scheduler.py::_load_instruments tries
option_service.option_service.get_contracts(index) — wrong API. Should
use upstox_app.option.option_service.get_contracts_for_index(index) or
the options_cache map.

ema_app/scheduler.py::_arm_session tries
streamer_manager.subscribe / subscribe_instruments — these methods do
not exist on streamer_manager. Use market_streamer.subscribe(keys, mode).

ema_app/isolation/service.py::_budget_instruments reads
cross_record[f"ltp_{key}"] fields the live engine never emits, so the
budget pool is normally empty.

ema_app/isolation/order_storage.py::_get_market_timezone uses
__import__("core.config", fromlist=["config"]).config — fragile; use
core_config directly.

ema_app/isolation/service.py::start() registers only the candle
listener. on_cross must be wired by the app bootstrap via
ema_service.register_cross_listener(isolation_service.on_cross).

intraday_cross.json path collision between the batch and live
engines — last writer wins.

EMA seeding divergence between batch and live engines.

R1/S1 divergence between the JS opening-range formula and
ema_app/opening_range.py.

UI subscribe path is /upstox/instruments/subscribe, not
/upstox/market/subscribe. Confirm both server-side paths call the same
market_streamer.subscribe.

/api/orders, /ui/logs/{filename}, /refresh/status,
/refresh/manual must exist in web/api_router.py with the payload
shapes described in the templates.

Missing pieces (needed to verify end-to-end wiring)
main.py

core/lifespan.py

web/page_router.py

web/api_router.py

web/ws_router.py

web/service.py

End of README.

text

---

## Notes on this rewrite

**What I kept from the old README:** the section structure, the tone, the emphasis on security invariants, the operating constraints (single worker), the troubleshooting format.

**What I added:** the whole `ema_app` and `ema_app/isolation` surface, the two-engine comparison, the isolation algorithm, the order safety switches, the client-side market-hours gate, the direct-from-browser candle path, and a §20 that explicitly reconciles this README with the old one and lists latent bugs.

**What I deliberately left as "flag, don't fix":** items 1–10 in the "Latent issues" list. I don't have `main.py` / `core/lifespan.py` / `web/*.py`, so I can't confirm the correct call sites. When you share those, I can turn §20's latent-issues list into a concrete patch set.

**What is still uncertain and needs your files to resolve:**
- Whether `ema_app.start_scheduler()` is actually called at startup
- Whether `isolation_service.start(ema_service)` and
  `ema_service.register_cross_listener(isolation_service.on_cross)` are wired
- Whether `ema_ws_manager` actually broadcasts `isolated_ema_alert` (the
  template depends on it)
- Whether `/api/orders`, `/ui/logs/{filename}`, `/refresh/*` exist in
  `web/api_router.py` with the exact response shapes the templates expect