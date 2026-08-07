# Live Option Feed Engine

A FastAPI-based live market feed engine for NIFTY option contracts using the Upstox WebSocket feed. The application fetches NIFTY option contracts, subscribes to the NIFTY index and filtered option instruments, receives live ticks from Upstox, normalizes incoming feed data, calculates historical and live EMA crossover signals, calculates Opening Range levels for all subscribed instruments, monitors R3/S3 touches, and broadcasts EMA crossover events through WebSockets with the matching instrument's Opening Range levels when available.

The project has evolved from a static single-option dashboard into a route-segregated FastAPI application with:

- A FastAPI-rendered dashboard served from `templates/index.html`
- Live `/all-feeds` WebSocket streaming
- A NIFTY index header card
- Dynamic nearest CE and PE option cards based on live NIFTY LTP
- Historical candle loading for EMA 9/21 crossover initialization
- Live EMA continuation from historical EMA state
- Global EMA crossover WebSocket stream
- Individual instrument-level EMA crossover WebSocket stream
- Opening Range calculation from Upstox intraday candles at 09:18 AM IST
- Opening Range R1/S1, R2/S2, R3/S3 level generation for all subscribed instruments
- Opening Range backfill scan to catch R3/S3 touches that happened before 09:18 AM
- Live Opening Range R3/S3 touch monitoring from Upstox ticks after levels are available
- EMA crossover WebSocket payloads enriched with Opening Range range and levels for the same instrument
- No first touched instrument selection
- No selected Opening Range EMA Telegram alerts
- Optional legacy Telegram alert for grouped R3/S3 touch events, disabled by default
- Scheduled daily hard refresh at 09:00 AM IST, Monday to Friday
- Manual hard refresh API and dashboard button
- Telegram notifications for startup, refresh, subscription, token, instruments, historical EMA, Opening Range job status, shutdown, and errors

---

## Project Purpose

This project acts as a local live option feed gateway, EMA crossover signal engine, and Opening Range WebSocket enrichment engine.

It handles:

1. Loading Upstox access tokens from MongoDB.
2. Fetching nearest NIFTY option contracts using the Upstox Options API.
3. Filtering option contracts by configured strike range.
4. Subscribing to the NIFTY index plus filtered option contracts.
5. Connecting to Upstox `MarketDataStreamerV3`.
6. Parsing raw live feed ticks into a normalized JSON format.
7. Broadcasting normalized ticks to connected FastAPI WebSocket clients.
8. Fetching historical candles for all subscribed instruments.
9. Calculating EMA 9/21 crossover summary from historical candles.
10. Initializing live EMA state from historical EMA values.
11. Continuing EMA calculation using live Upstox full-feed I1 candles.
12. Broadcasting live EMA crossover events globally and per instrument.
13. Calculating Opening Range levels using Upstox intraday candles.
14. Backfill-scanning post-Opening Range candles for already touched R3/S3 levels.
15. Monitoring live ticks after Opening Range generation for R3/S3 touches.
16. Enriching each EMA crossover WebSocket event with the same instrument's Opening Range context.
17. Rendering a browser dashboard through FastAPI templates.
18. Showing live NIFTY index data in the dashboard header.
19. Showing nearest CE and PE option cards based on live NIFTY LTP.
20. Supporting automatic scheduled hard refresh.
21. Supporting manual hard refresh through an API and UI button.
22. Sending Telegram alerts for important lifecycle events and failures.
23. Providing debug, health, history, live EMA, Opening Range, and WebSocket documentation endpoints for local testing.

---

## Current Project Structure

```text
t_feed_v1-ws-feed/
|
├── main.py
├── requirements.txt
├── Procfile
├── run.bat
├── .env
├── README.md
|
├── api/
│   ├── __init__.py
│   ├── home_routes.py
│   ├── health_routes.py
│   ├── debug_routes.py
│   ├── refresh_routes.py
│   ├── history_routes.py
│   ├── opening_range_routes.py
│   └── ws_docs_routes.py
|
├── core/
│   ├── config.py
│   └── logger.py
|
├── data/
│   ├── nearest_nifty_option_contracts.json
│   ├── ema_cross_results.json
│   ├── live_ema_cross_results.json
│   ├── opening_range_results.json
│   └── opening_range_touch_events.json
|
├── services/
│   ├── option_service.py
│   ├── telegram_service.py
│   ├── token_service.py
│   ├── history_service.py
│   ├── live_ema_service.py
│   ├── opening_range_service.py
│   └── upstox_websocket.py
|
├── templates/
│   └── index.html
|
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
- Refreshes token from MongoDB during startup.
- Loads option contracts and subscription keys.
- Fetches historical candles and calculates EMA crossover summary during startup.
- Initializes live EMA state from historical EMA summary.
- Starts APScheduler background jobs.
- Starts the Upstox WebSocket streamer.
- Performs daily hard refresh scheduling.
- Recalculates historical EMA during daily hard refresh.
- Schedules Opening Range calculation at 09:18 AM IST.
- Restarts Upstox streamer after refresh.
- Sends Telegram notifications for startup, scheduler, refresh, historical EMA, Opening Range job status, subscription, errors, and shutdown.
- Stops streamer and scheduler during shutdown.

Registered route modules:

```python
app.include_router(home_router)
app.include_router(health_router)
app.include_router(debug_router)
app.include_router(refresh_router)
app.include_router(history_router)
app.include_router(opening_range_router)
app.include_router(websocket_router)
app.include_router(ws_docs_router)
```

Startup lifecycle flow:

```text
Application startup
    -> token_service.refresh_tokens()
    -> get_options_contracts(save_data=True)
    -> update options_cache and subscribed_keys
    -> fetch historical candles for subscribed instruments
    -> calculate EMA 9/21 crossover summary
    -> initialize live EMA service state
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
    -> fetch historical candles and calculate EMA 9/21 crossover
    -> reinitialize live EMA service state
    -> restart Upstox streamer
    -> send Telegram refresh/subscription notifications
```

Opening Range scheduled flow:

```text
Mon-Fri 09:18 AM IST
    -> read subscribed instruments from options_cache
    -> fetch intraday candles for all subscribed instruments
    -> calculate Opening Range levels from first N candles from 09:15
    -> scan post-OR intraday candles for already touched R3/S3
    -> save Opening Range results locally
    -> store results in memory per instrument
    -> keep R3/S3 touch events in memory
    -> use cached Opening Range levels to enrich EMA crossover WebSocket payloads
```

EMA + Opening Range WebSocket flow:

```text
After historical EMA state and Opening Range cache are ready:
    live tick arrives
    -> live_ema_service processes completed I1 candle
    -> if EMA 9/21 crossover happens, event is created
    -> upstox_websocket enriches event with Opening Range context using instrument_key
    -> broadcaster sends enriched EMA event to connected WebSocket clients
```

---

## API Routes

### `api/home_routes.py`

Serves the dashboard home page.

```text
GET /
```

Renders:

```text
templates/index.html
```

---

### `api/health_routes.py`

Contains the health check route.

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
- Live EMA status
- Opening Range status

---

### `api/debug_routes.py`

Contains local testing and debugging routes.

```text
GET /test-broadcast
GET /test-broadcast-option
GET /debug/cache
GET /debug/find-option?strike=24500&striketype=ce
```

---

### `api/refresh_routes.py`

Contains manual market hard refresh APIs.

```text
POST /refresh/manual
GET  /refresh/status
```

---

## Historical EMA Routes

### `api/history_routes.py`

Contains historical candle, historical EMA, and live EMA status APIs.

```text
GET  /history/status
POST /history/fetch
GET  /history/instrument
GET  /history/cache
GET  /history/ema-results-file
GET  /history/config
GET  /history/live-ema/status
GET  /history/live-ema/events
GET  /history/live-ema/instrument
GET  /history/live-ema/instruments
GET  /history/live-ema/file
```

### `GET /history/live-ema/events`

Returns recent live EMA crossover events.

Example:

```text
GET http://127.0.0.1:8000/history/live-ema/events?limit=100&include_opening_range=true
```

When `include_opening_range=true`, each EMA event is enriched with Opening Range context for the same instrument.

---

## Opening Range Routes

### `api/opening_range_routes.py`

Contains Opening Range calculation, cache, file, R3/S3 touch event APIs, and compatibility routes for the disabled selected OR flow.

```text
GET  /opening-range/status
POST /opening-range/fetch
GET  /opening-range/cache
GET  /opening-range/instrument
GET  /opening-range/ema-context
POST /opening-range/instrument/fetch
GET  /opening-range/selected-instrument
GET  /opening-range/selected-instrument/ema-alerts
GET  /opening-range/touch-events
GET  /opening-range/touch-events/pending
POST /opening-range/touch-events/flush
GET  /opening-range/file
GET  /opening-range/config
```

### `GET /opening-range/status`

Returns latest Opening Range calculation status, latest main index LTP, touch event counts, and new flow details.

### `POST /opening-range/fetch`

Manually triggers Opening Range calculation for all subscribed instruments.

Example:

```text
POST http://127.0.0.1:8000/opening-range/fetch?candle_count=1&save_results=true&max_workers=8
```

Behavior:

- Fetches today's intraday candles using Upstox HistoryV3Api.
- Selects first N candles from market open.
- Calculates open, high, low, close, average.
- Calculates R1/S1, R2/S2, R3/S3 and thresholds.
- Scans post-OR candles for already touched R3/S3.
- Stores summary in memory.
- Saves to `data/opening_range_results.json` if enabled.
- Tracks backfill touch events.
- Does not select or lock any instrument.

### `GET /opening-range/cache`

Returns full Opening Range cache.

### `GET /opening-range/instrument`

Returns Opening Range result for one instrument from memory cache.

Examples:

```text
http://127.0.0.1:8000/opening-range/instrument?instrument_key=NSE_INDEX%7CNifty%2050
http://127.0.0.1:8000/opening-range/instrument?instrument_key=NSE_FO%7C41012
http://127.0.0.1:8000/opening-range/instrument?strike=24500&striketype=ce
```

### `GET /opening-range/ema-context`

Returns the Opening Range context that will be attached to an EMA crossover WebSocket event for the given instrument.

Example:

```text
GET http://127.0.0.1:8000/opening-range/ema-context?strike=24500&striketype=ce
```

### `POST /opening-range/instrument/fetch`

Fetches intraday candles and calculates Opening Range for one instrument for testing/debugging.

### `GET /opening-range/selected-instrument`

Backward-compatible route.

New behavior:

```text
Selected Opening Range instrument flow is disabled.
No instrument is permanently locked.
EMA crossovers are broadcast for all instruments with Opening Range levels when available.
```

### `GET /opening-range/selected-instrument/ema-alerts`

Backward-compatible route.

New behavior:

```text
Selected OR EMA Telegram alerts are disabled.
Use /ws/ema-crossover or /ws/ema-crossover/instrument for live EMA crossovers enriched with Opening Range levels.
```

### `GET /opening-range/touch-events`

Returns recent Opening Range R3/S3 touch events.

Example:

```text
http://127.0.0.1:8000/opening-range/touch-events?limit=100
```

Events can come from:

```text
intraday_backfill_scan
live_tick
```

Touch events are tracked for diagnostics and optional WebSocket broadcasting. They do not select a permanent instrument.

### `GET /opening-range/touch-events/pending`

Returns pending Opening Range touch events waiting for legacy Telegram batch flush.

Pending events are used only if:

```python
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = True
```

### `POST /opening-range/touch-events/flush`

Manually flushes pending touch events to Telegram for legacy touch alert flow.

Example:

```text
POST http://127.0.0.1:8000/opening-range/touch-events/flush?force=true
```

Recommended behavior keeps legacy touch Telegram disabled.

---

## WebSocket Routes

### `ws_feed/websocket_routes.py`

Contains FastAPI WebSocket endpoints.

```text
WS /ws
WS /all-feeds
WS /option?strike=24500&striketype=ce
WS /ws/ema-crossover
WS /ws/ema-crossover/instrument?instrument_key=NSE_FO%7C41012
WS /ws/ema-crossover/instrument?strike=24500&striketype=ce
WS /ws/opening-range
WS /ws/opening-range/instrument?instrument_key=NSE_FO%7C41012
WS /ws/opening-range/instrument?strike=24500&striketype=ce
```

### `/ws` and `/all-feeds`

Both routes connect a client to all subscribed instruments.

They can receive:

- Live ticks
- EMA crossover events
- Opening Range touch events

### `/option`

Connects a client to a specific option based on strike and option type.

Example:

```text
ws://127.0.0.1:8000/option?strike=24500&striketype=ce
```

### `/ws/ema-crossover`

Dedicated global WebSocket endpoint for live EMA crossover events for all instruments.

Each EMA crossover event may include:

```json
"opening_range": {
  "available": true,
  "range": {},
  "levels": {},
  "touch_status": {}
}
```

### `/ws/ema-crossover/instrument`

Dedicated instrument-specific WebSocket endpoint for live EMA crossover events.

Examples:

```text
ws://127.0.0.1:8000/ws/ema-crossover/instrument?instrument_key=NSE_INDEX%7CNifty%2050
ws://127.0.0.1:8000/ws/ema-crossover/instrument?strike=24500&striketype=ce
```

### `/ws/opening-range`

Dedicated global WebSocket endpoint for Opening Range events.

Example:

```text
ws://127.0.0.1:8000/ws/opening-range
```

### `/ws/opening-range/instrument`

Dedicated instrument-specific WebSocket endpoint for Opening Range events.

Examples:

```text
ws://127.0.0.1:8000/ws/opening-range/instrument?instrument_key=NSE_INDEX%7CNifty%2050
ws://127.0.0.1:8000/ws/opening-range/instrument?instrument_key=NSE_FO%7C41012
ws://127.0.0.1:8000/ws/opening-range/instrument?strike=24500&striketype=ce
ws://127.0.0.1:8000/ws/opening-range/instrument?strike=24500&striketype=pe
```

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
    -> receives EMA crossover events if present
    -> receives Opening Range touch events if present
    -> updates NIFTY header
    -> stores CE/PE option ticks in memory
    -> sorts options by distance from live NIFTY LTP
    -> displays nearest CE and PE cards
```

---

## Core Configuration

### `core/config.py`

Central configuration file.

Important settings include:

```python
MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"
STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0
WEBSOCKET_FEED_MODE = "full"

MARKET_TIMEZONE = "Asia/Kolkata"
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15

HISTORICAL_CANDLE_ENABLED = True
HISTORICAL_CANDLE_DAYS = 10
HISTORICAL_CANDLE_INTERVAL = "1minute"
HISTORICAL_CANDLE_MAX_WORKERS = 8

EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21

LIVE_EMA_ENABLED = True
LIVE_EMA_INTERVAL_MINUTES = 1
LIVE_EMA_FAST_PERIOD = 9
LIVE_EMA_SLOW_PERIOD = 21

OPENING_RANGE_ENABLED = True
OPENING_RANGE_INTERVAL = "1minute"
OPENING_RANGE_CANDLE_COUNT = 1
OPENING_RANGE_MARKET_OPEN_HOUR = 9
OPENING_RANGE_MARKET_OPEN_MINUTE = 15
OPENING_RANGE_FETCH_HOUR = 9
OPENING_RANGE_FETCH_MINUTE = 18
OPENING_RANGE_INTRADAY_UNIT = "minutes"
OPENING_RANGE_INTRADAY_INTERVAL = "1"
OPENING_RANGE_OUTPUT_FILE = "data/opening_range_results.json"

OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED = True
OPENING_RANGE_TOUCH_ALERT_ENABLED = True
OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = True
OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = True
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = MAIN_NIFTY_SECURITY
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = True
OPENING_RANGE_TOUCH_CHECK_MODE = "high_low"
OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = "data/opening_range_touch_events.json"

OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = False
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "disabled"
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = False
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = False
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = False

EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = True
EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = True
```

---

## Services

### `services/token_service.py`

Handles access token retrieval from MongoDB.

### `services/option_service.py`

Fetches and manages NIFTY option contracts.

### `services/history_service.py`

Fetches historical and intraday candles and calculates EMA crossover summaries.

### `services/live_ema_service.py`

Continues EMA calculation using live Upstox full-feed candles.

Responsibilities:

- Receives live Upstox full-feed tick data.
- Reads latest I1 candle from `marketOHLC` or `optionOHLC`.
- Waits for candle timestamp change to treat previous candle as completed.
- Continues EMA from historical EMA state.
- Detects bullish and bearish EMA 9/21 crossovers.
- Stores live EMA cross events in memory.
- Optionally saves events to `data/live_ema_cross_results.json` when `TEST_FLAG=True`.
- Adds event details such as previous signal, current signal, previous EMA values, and candle data.
- Does not select an Opening Range instrument.
- Does not send Telegram alerts.

### `services/opening_range_service.py`

Calculates Opening Range levels, monitors R3/S3 touches, and provides Opening Range context for EMA WebSocket enrichment.

Responsibilities:

- Fetches intraday candles for subscribed instruments.
- Selects first N candles from market open.
- Calculates open, high, low, close, average.
- Calculates R1/S1, R2/S2, R3/S3 and thresholds.
- Scans post-OR intraday candles for R3/S3 touches that happened before 09:18.
- Stores Opening Range results in memory per instrument.
- Saves Opening Range summary to `data/opening_range_results.json`.
- Tracks latest NIFTY index LTP.
- Processes live ticks after 09:18 for R3/S3 touches.
- Tracks touch events for diagnostics and optional WebSocket use.
- Provides `get_opening_range_levels_for_ema_event(instrument_key)` for EMA payload enrichment.
- Does not select a permanent OR instrument.
- Does not send selected OR EMA Telegram alerts.
- Optionally saves touch events to `data/opening_range_touch_events.json` when `TEST_FLAG=True`.

Opening Range formula:

```text
rangeAvg = (rangeHigh + rangeLow) / 2

highAvgDiff = abs(rangeHigh - rangeAvg)
lowAvgDiff  = abs(rangeAvg - rangeLow)

R1 = rangeAvg + (highAvgDiff / 2)
S1 = rangeAvg - (lowAvgDiff / 2)

R2 = rangeHigh + highAvgDiff
S2 = rangeLow - lowAvgDiff

R3 = R2 + highAvgDiff
S3 = S2 - lowAvgDiff

R3 Threshold = (R2 + R3) / 2
S3 Threshold = (S2 + S3) / 2
```

Backfill edge case:

```text
If R3/S3 touched between OR completion and 09:18 AM,
the 09:18 intraday scan detects it and stores the event.

Backfill touch does not select or lock the instrument.
```

Live touch flow:

```text
After OR levels are available:
    live tick arrives
    -> update NIFTY LTP if index tick
    -> check option tick against cached R3/S3
    -> store touch event if R3/S3 is touched
    -> do not lock or select any instrument
```

EMA enrichment flow:

```text
Live EMA crossover event generated
    -> upstox_websocket calls get_opening_range_levels_for_ema_event(instrument_key)
    -> opening_range is attached to EMA event
    -> broadcaster sends enriched event through WebSocket
```

### `services/upstox_websocket.py`

Maintains the connection to Upstox live market feed.

Responsibilities:

- Reads access token from `token_service`.
- Reads subscribed instrument keys from `options_cache`.
- Creates `upstox_client.MarketDataStreamerV3`.
- Connects to Upstox WebSocket in a background thread.
- Receives tick messages.
- Sends decoded feed ticks to `broadcaster.broadcast_tick()`.
- Sends full-feed candle data to `live_ema_service.process_live_feed()`.
- Enriches live EMA cross events with Opening Range context.
- Sends live ticks to `opening_range_service.process_live_tick_for_opening_range()`.
- Broadcasts EMA crossover events through `broadcaster.broadcast_ema_cross()`.
- Broadcasts Opening Range touch events through `broadcaster.broadcast_opening_range()`.
- Flushes pending Opening Range touch Telegram alerts only if legacy touch Telegram is enabled.
- Reconnects on failure.

Important behavior:

- Normal live tick broadcasting is skipped when no local clients are connected.
- Live EMA processing can continue even when no UI clients are connected if `LIVE_EMA_ENABLED=True`.
- Opening Range touch monitoring can continue even when no UI clients are connected if `OPENING_RANGE_TOUCH_ALERT_ENABLED=True`.
- EMA Opening Range enrichment can continue even when no UI clients are connected if enabled.

### `services/telegram_service.py`

Sends Telegram notifications using bot token and chat ID from config.

Telegram events include:

- Application startup started
- Token refresh success or failure
- Instrument fetch success or failure
- Historical EMA fetch success or failure
- Opening Range calculation success or failure
- Scheduler started
- Daily hard refresh started, success, or failure
- Manual hard refresh started, success, or failure
- Upstox subscription active or failure
- Application shutdown started or completed
- Exceptions and runtime failures

Disabled Telegram events:

```text
Selected Opening Range instrument notification
Selected OR instrument EMA crossover alerts
```

---

## Broadcaster

### `ws_feed/broadcaster.py`

Core local WebSocket broadcasting engine.

Connection pools:

```python
self.active_connections = set()
self.ema_crossover_connections = set()
self.ema_instrument_connections = {}
self.opening_range_connections = set()
self.opening_range_instrument_connections = {}
self.all_feeds_connections = {}
self.option_connections = {}
```

Broadcast methods:

```python
broadcast_tick()
broadcast_ema_cross()
broadcast_opening_range()
broadcast_candle()
```

The broadcaster preserves and forwards complete EMA event payloads, including `opening_range` when it is attached upstream.

---

## WebSocket Payload Format

### Live Tick Payload

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
  "greeks": {},
  "depth": [],
  "info": {}
}
```

### Live EMA Crossover Payload With Opening Range

```json
{
  "type": "live_ema_cross",
  "instrument_key": "NSE_FO|41012",
  "timestamp": "2026-08-05T09:16:00+05:30",
  "cross_type": "bullish_cross",
  "interval_minutes": 1,
  "close": 124.5,
  "ema_fast_period": 9,
  "ema_slow_period": 21,
  "ema_fast": 123.8,
  "ema_slow": 122.9,
  "previous_ema_fast": 122.1,
  "previous_ema_slow": 122.4,
  "previous_signal": "bearish",
  "current_signal": "bullish",
  "source": "live_feed",
  "created_at": "2026-08-06T09:21:05+05:30",
  "candle": {
    "timestamp": "2026-08-06T09:21:00+05:30",
    "timestamp_ms": 1785988260000,
    "open": 121.5,
    "high": 126.0,
    "low": 120.0,
    "close": 124.5,
    "volume": 2000
  },
  "info": {
    "instrument_type": "CE",
    "strike_price": 24500.0,
    "trading_symbol": "NIFTY 24500 CE"
  },
  "opening_range": {
    "available": true,
    "instrument_key": "NSE_FO|41012",
    "date": "2026-08-06",
    "source": "intraday_api",
    "interval": "1minute",
    "range": {
      "open": 121.5,
      "high": 126.0,
      "low": 120.0,
      "close": 124.0,
      "average": 123.0
    },
    "levels": {
      "r1": 124.5,
      "s1": 121.5,
      "r2": 129.0,
      "s2": 117.0,
      "r3": 132.0,
      "s3": 114.0,
      "r3_threshold": 130.5,
      "s3_threshold": 115.5
    },
    "touch_status": {
      "r3_touched": false,
      "s3_touched": false,
      "first_touch_level": null,
      "first_touch_source": null,
      "first_touch_time": null,
      "events": []
    }
  }
}
```

### Live EMA Crossover Payload Without Opening Range

```json
{
  "type": "live_ema_cross",
  "instrument_key": "NSE_FO|41012",
  "timestamp": "2026-08-05T09:16:00+05:30",
  "cross_type": "bullish_cross",
  "interval_minutes": 1,
  "close": 124.5,
  "ema_fast": 123.8,
  "ema_slow": 122.9,
  "opening_range": {
    "available": false,
    "instrument_key": "NSE_FO|41012",
    "message": "Opening Range levels are not available for this instrument.",
    "range": null,
    "levels": null,
    "touch_status": null
  }
}
```

### Opening Range Touch Payload

```json
{
  "type": "opening_range_touch",
  "instrument_key": "NSE_FO|41012",
  "level": "R3",
  "level_value": 126.75,
  "trigger_price": 128.5,
  "trigger_field": "high",
  "touch_time": "2026-08-06T09:16:00+05:30",
  "source": "live_tick",
  "date": "2026-08-06",
  "main_index_ltp": 24580.25,
  "distance_from_index": 19.75,
  "alert_key": "NSE_FO|41012_R3",
  "contract_info": {
    "instrument_key": "NSE_FO|41012",
    "instrument_type": "CE",
    "strike_price": 24600.0,
    "trading_symbol": "NIFTY 24600 CE"
  },
  "created_at": "2026-08-06T09:18:05+05:30"
}
```

Possible Opening Range touch sources:

```text
intraday_backfill_scan
live_tick
```

---

## Scheduled Jobs

### Daily Hard Refresh

```text
Monday to Friday at 09:00 AM Asia/Kolkata
```

Performs:

```text
1. Refresh token document from MongoDB
2. Load latest token into memory
3. Fetch latest NIFTY option instruments
4. Filter instruments by configured strike range
5. Update options_cache and subscribed_keys
6. Fetch historical candles and calculate EMA crossover summary
7. Initialize live EMA state from historical EMA summary
8. Restart Upstox streamer
9. Subscribe with latest instrument keys
10. Send Telegram notifications
```

### Daily Opening Range Fetch

```text
Monday to Friday at 09:18 AM Asia/Kolkata
```

Performs:

```text
1. Fetch intraday candles for all subscribed instruments
2. Select first N candles from 09:15 AM
3. Calculate Opening Range levels
4. Scan post-OR candles for R3/S3 touches before 09:18
5. Save Opening Range results to data/opening_range_results.json
6. Store touch events in memory
7. Keep Opening Range cache available for EMA WebSocket enrichment
```

---

## Telegram Alert Behavior

### Active Telegram behavior

Telegram is used for lifecycle and operational notifications:

```text
startup
shutdown
token refresh
instrument fetch
subscription status
historical EMA fetch status
Opening Range fetch status
daily hard refresh
manual hard refresh
exceptions and runtime failures
```

### Disabled Telegram behavior

The following are disabled in the new flow:

```python
OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = False
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = False
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = False
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False
```

Meaning:

```text
No first touched instrument is selected.
No selected OR instrument Telegram notification is sent.
No selected OR EMA crossover Telegram alert is sent.
Legacy grouped R3/S3 touch Telegram alert is disabled by default.
```

---

## Manual Refresh

Manual refresh is available from both API and dashboard.

```text
POST /refresh/manual
GET  /refresh/status
```

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

```text
ws://127.0.0.1:8000/all-feeds
http://127.0.0.1:8000/test-broadcast
```

### Test Option Broadcast

```text
ws://127.0.0.1:8000/option?strike=24500&striketype=ce
http://127.0.0.1:8000/test-broadcast-option
```

### Test Global EMA Crossover WebSocket

```text
ws://127.0.0.1:8000/ws/ema-crossover
```

### Test Instrument-Specific EMA Crossover WebSocket

```text
ws://127.0.0.1:8000/ws/ema-crossover/instrument?instrument_key=NSE_INDEX%7CNifty%2050
ws://127.0.0.1:8000/ws/ema-crossover/instrument?strike=24500&striketype=ce
```

### Manual Historical EMA Fetch

```text
POST http://127.0.0.1:8000/history/fetch?interval=1minute&history_days=10&save_results=true&max_workers=5
```

### Live EMA Status

```text
GET http://127.0.0.1:8000/history/live-ema/status
```

### Live EMA Events With Opening Range

```text
GET http://127.0.0.1:8000/history/live-ema/events?limit=100&include_opening_range=true
```

### Opening Range Manual Fetch

```text
POST http://127.0.0.1:8000/opening-range/fetch?candle_count=1&save_results=true&max_workers=8
```

### Opening Range Status

```text
GET http://127.0.0.1:8000/opening-range/status
```

### Opening Range EMA Context

```text
GET http://127.0.0.1:8000/opening-range/ema-context?strike=24500&striketype=ce
```

### Opening Range Touch Events

```text
GET http://127.0.0.1:8000/opening-range/touch-events?limit=100
```

### Flush Pending Opening Range Touch Alerts

This works only if legacy touch Telegram is enabled.

```text
POST http://127.0.0.1:8000/opening-range/touch-events/flush?force=true
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
logs/history_service.log
logs/live_ema_service.log
logs/opening_range_service.log
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

For production deployments, avoid using `--reload`.

---

## Important Notes

- This project depends on a valid Upstox access token.
- Token is expected to be stored in MongoDB.
- If token is missing, option contract loading will fail.
- If options cache is empty, Upstox subscription keys will not be available.
- Updating `options_cache` alone is not enough to apply new subscriptions.
- The Upstox streamer must be restarted after refresh so it subscribes with the latest keys.
- Historical candle batches are sequential inside each instrument.
- Multiple instruments can still run in parallel depending on `HISTORICAL_CANDLE_MAX_WORKERS`.
- Use `HISTORICAL_CANDLE_MAX_WORKERS = 1` for strict one-by-one processing across all instruments.
- Live EMA continuation depends on successful historical EMA initialization.
- Opening Range levels are generated from intraday API candles, not from raw live ticks.
- Opening Range backfill scan covers R3/S3 touches that happen before 09:18.
- Backfill touch events do not permanently select any OR instrument.
- Live touch events do not permanently select any OR instrument.
- EMA Telegram alerts are disabled in the new flow.
- Legacy Opening Range touch Telegram alerts are disabled by default.
- Global EMA WebSocket receives all crossover events.
- Instrument-specific EMA WebSocket receives only the resolved instrument's crossover events.
- EMA WebSocket payloads include Opening Range levels when available.
- Global Opening Range WebSocket receives all Opening Range events.
- Instrument-specific Opening Range WebSocket receives only resolved instrument events.
- If no local WebSocket clients are connected, normal tick broadcasting is skipped to reduce event-loop load.
- If `LIVE_EMA_ENABLED=True`, live EMA processing can continue even when no dashboard client is connected.
- If `OPENING_RANGE_TOUCH_ALERT_ENABLED=True`, Opening Range live touch monitoring can continue even when no dashboard client is connected.
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
- Daily Opening Range fetch runs Mon-Fri at 09:18 AM IST.
- Telegram notifications are integrated through `services/telegram_service.py` for lifecycle and operational status.
- Historical EMA 9/21 crossover calculation is integrated through `services/history_service.py`.
- Live EMA continuation is integrated through `services/live_ema_service.py`.
- Opening Range calculation, R3/S3 touch tracking, and EMA Opening Range context provider are integrated through `services/opening_range_service.py`.
- Upstox live tick processing enriches EMA cross events with Opening Range levels through `services/upstox_websocket.py`.
- Global EMA crossover streaming is available through `/ws/ema-crossover`.
- Instrument-specific EMA crossover streaming is available through `/ws/ema-crossover/instrument`.
- Global Opening Range streaming is available through `/ws/opening-range`.
- Instrument-specific Opening Range streaming is available through `/ws/opening-range/instrument`.
- Selected OR instrument routes are retained only for backward compatibility and return disabled state.
- Dashboard displays live NIFTY 50 and nearest CE/PE option cards from `/all-feeds`.
