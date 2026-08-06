# Live Option Feed Engine

A FastAPI-based live market feed engine for NIFTY option contracts using the Upstox WebSocket feed. The application fetches NIFTY option contracts, subscribes to the NIFTY index and filtered option instruments, receives live ticks from Upstox, normalizes incoming feed data, calculates historical and live EMA crossover signals, calculates Opening Range levels, monitors R3/S3 touches, selects the first live R3/S3 touched option instrument permanently for the current run, and sends Telegram EMA crossover alerts only for that selected instrument.

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
- First live option instrument touching/crossing R3 or S3 is selected permanently for the current run/day
- EMA crossover Telegram alerts are sent only for the selected Opening Range instrument
- Optional legacy Telegram alert for grouped R3/S3 touch events, disabled by default for the new flow
- Scheduled daily hard refresh at 09:00 AM IST, Monday to Friday
- Manual hard refresh API and dashboard button
- Telegram notifications for startup, refresh, subscription, token, instruments, historical EMA, Opening Range, selected OR instrument, selected OR EMA cross, shutdown, and errors

---

## Project Purpose

This project acts as a local live option feed gateway, EMA crossover signal engine, and Opening Range selected-instrument alert engine.

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
16. Selecting the first live option instrument that touches/crosses R3 or S3.
17. Ignoring all other instruments after the first selected OR instrument is locked.
18. Sending Telegram messages for EMA crossovers only for the selected OR instrument.
19. Including EMA cross data, current NIFTY LTP, selected instrument live data, and OR levels in Telegram.
20. Rendering a browser dashboard through FastAPI templates.
21. Showing live NIFTY index data in the dashboard header.
22. Showing nearest CE and PE option cards based on live NIFTY LTP.
23. Supporting automatic scheduled hard refresh.
24. Supporting manual hard refresh through an API and UI button.
25. Sending Telegram alerts for important lifecycle events and failures.
26. Providing debug, health, history, live EMA, Opening Range, selected OR instrument, and selected OR EMA alert status endpoints for local testing.

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
│   ├── refresh_routes.py
│   ├── history_routes.py
│   ├── opening_range_routes.py
│   └── ws_docs_routes.py
│
├── core/
│   ├── config.py
│   └── logger.py
│
├── data/
│   ├── nearest_nifty_option_contracts.json
│   ├── ema_cross_results.json
│   ├── live_ema_cross_results.json
│   ├── opening_range_results.json
│   └── opening_range_touch_events.json
│
├── services/
│   ├── option_service.py
│   ├── telegram_service.py
│   ├── token_service.py
│   ├── history_service.py
│   ├── live_ema_service.py
│   ├── opening_range_service.py
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
- Sends Telegram notifications for startup, scheduler, refresh, historical EMA, Opening Range, selected OR instrument, selected OR EMA cross, subscription, errors, and shutdown.
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
    -> store results in memory
    -> keep R3/S3 touch events in memory
    -> wait for live_tick to select first R3/S3 touched option instrument by default
```

Selected OR + EMA alert flow:

```text
After Opening Range levels are ready:
    live tick arrives
    -> update NIFTY LTP if tick is NIFTY index
    -> check option tick against cached R3/S3
    -> first live option instrument touching/crossing R3 or S3 is locked permanently
    -> optional Telegram: selected OR instrument locked
    -> ignore all other instruments after lock
    -> continue live EMA processing for all subscribed instruments
    -> if EMA crossover belongs to selected OR instrument
        -> send Telegram message with EMA cross data, NIFTY LTP, instrument live data, and OR levels
    -> if EMA crossover belongs to any other instrument
        -> ignore for Telegram selected OR alert
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

---

## Opening Range Routes

### `api/opening_range_routes.py`

Contains Opening Range calculation, cache, file, R3/S3 touch event APIs, and selected OR instrument APIs.

```text
GET  /opening-range/status
POST /opening-range/fetch
GET  /opening-range/cache
GET  /opening-range/instrument
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

Returns latest Opening Range calculation status, latest main index LTP, touch event counts, selected OR instrument state, and selected OR EMA alert counts.

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
- By default, backfill events do not permanently select the OR instrument because selection source is `live_tick`.

### `GET /opening-range/cache`

Returns full Opening Range cache including selected OR instrument state and selected OR EMA alert records.

### `GET /opening-range/instrument`

Returns Opening Range result for one instrument from memory cache.

Examples:

```text
http://127.0.0.1:8000/opening-range/instrument?instrument_key=NSE_INDEX%7CNifty%2050
http://127.0.0.1:8000/opening-range/instrument?instrument_key=NSE_FO%7C41012
http://127.0.0.1:8000/opening-range/instrument?strike=24500&striketype=ce
```

### `POST /opening-range/instrument/fetch`

Fetches intraday candles and calculates Opening Range for one instrument for testing/debugging.

### `GET /opening-range/selected-instrument`

Returns the permanently selected Opening Range instrument.

Selection rule:

```text
First live option instrument that touches/crosses R3 or S3 is selected permanently.
All other instruments are ignored after selection.
```

### `GET /opening-range/selected-instrument/ema-alerts`

Returns selected OR instrument EMA alert records.

Example:

```text
http://127.0.0.1:8000/opening-range/selected-instrument/ema-alerts?limit=100
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

### `GET /opening-range/touch-events/pending`

Returns pending Opening Range touch events waiting for legacy Telegram batch flush.

In the new selected OR + EMA flow, pending events are used only if:

```python
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = True
```

### `POST /opening-range/touch-events/flush`

Manually flushes pending touch events to Telegram for legacy touch alert flow.

Example:

```text
POST http://127.0.0.1:8000/opening-range/touch-events/flush?force=true
```

Recommended new behavior keeps legacy touch Telegram disabled.

### `GET /opening-range/file`

Checks whether Opening Range output file and touch events output file exist.

### `GET /opening-range/config`

Returns Opening Range, backfill scan, live touch alert, selected OR instrument, and selected OR EMA alert configuration.

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

### `/option`

Connects a client to a specific option based on strike and option type.

Example:

```text
ws://127.0.0.1:8000/option?strike=24500&striketype=ce
```

### `/ws/ema-crossover`

Dedicated global WebSocket endpoint for live EMA crossover events for all instruments.

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

OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = True
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "live_tick"
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = True
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = True
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = True
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
- Broadcasts EMA cross events if callbacks or WebSocket clients are available.
- Adds event details such as previous signal, current signal, previous EMA values, and candle data.

### `services/opening_range_service.py`

Calculates Opening Range levels, monitors R3/S3 touches, selects the first live touched instrument, and sends EMA-based Telegram alerts for the selected instrument.

Responsibilities:

- Fetches intraday candles for subscribed instruments.
- Selects first N candles from market open.
- Calculates open, high, low, close, average.
- Calculates R1/S1, R2/S2, R3/S3 and thresholds.
- Scans post-OR intraday candles for R3/S3 touches that happened before 09:18.
- Stores Opening Range results in memory.
- Saves Opening Range summary to `data/opening_range_results.json`.
- Tracks latest NIFTY index LTP.
- Processes live ticks after 09:18 for R3/S3 touches.
- Permanently selects the first live option instrument that touches/crosses R3 or S3.
- Ignores all other instruments after selected OR instrument is locked.
- Stores latest live data for the selected instrument.
- Processes EMA crossover events and sends Telegram only when EMA cross belongs to selected OR instrument.
- Provides selected OR instrument state and selected OR EMA alert records through APIs.
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

By default:
    OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "live_tick"

So backfill touch does not permanently select the instrument unless selection source is changed to:
    "intraday_backfill_scan"
    "all"
    "any"
```

Live selected OR flow:

```text
After OR levels are available:
    live tick arrives
    -> update NIFTY LTP if index tick
    -> check option tick against cached R3/S3
    -> if no selected instrument exists and option touches/crosses R3 or S3
        -> select this instrument permanently
        -> send optional selected-instrument Telegram notification
    -> if selected instrument already exists and this tick belongs to another instrument
        -> ignore for OR selection
    -> if selected instrument tick arrives
        -> keep latest live data updated
```

Selected OR EMA flow:

```text
Live EMA crossover event generated
    -> check selected OR instrument state
    -> if selected instrument is not available, ignore
    -> if EMA event instrument is not selected instrument, ignore
    -> if same EMA cross was already alerted, ignore
    -> send Telegram message containing:
        - selected instrument details
        - selected R3/S3 touch details
        - EMA cross data
        - current NIFTY LTP
        - selected instrument live LTP/high/low/close
        - Opening Range R1/S1/R2/S2/R3/S3 levels
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
- Sends live ticks to `opening_range_service.process_live_tick_for_opening_range()`.
- Sends live EMA cross events to `opening_range_service.process_selected_or_ema_cross_alert()`.
- Broadcasts EMA crossover events through `broadcaster.broadcast_ema_cross()`.
- Broadcasts Opening Range touch events through `broadcaster.broadcast_opening_range()`.
- Flushes pending Opening Range touch Telegram alerts only if legacy touch Telegram is enabled.
- Reconnects on failure.

Important behavior:

- Normal live tick broadcasting is skipped when no local clients are connected.
- Live EMA processing can continue even when no UI clients are connected if `LIVE_EMA_ENABLED=True`.
- Opening Range touch monitoring can continue even when no UI clients are connected if `OPENING_RANGE_TOUCH_ALERT_ENABLED=True`.
- Selected OR EMA alert processing can continue even when no UI clients are connected if `OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED=True`.

### `services/telegram_service.py`

Sends Telegram notifications using bot token and chat ID from config.

Telegram events include:

- Application startup started
- Token refresh success or failure
- Instrument fetch success or failure
- Historical EMA fetch success or failure
- Opening Range calculation success or failure
- Selected Opening Range instrument notification
- Selected OR instrument EMA crossover alerts
- Optional legacy Opening Range R3/S3 touch alerts
- Scheduler started
- Daily hard refresh started, success, or failure
- Manual hard refresh started, success, or failure
- Upstox subscription active or failure
- Application shutdown started or completed
- Exceptions and runtime failures

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

### Live EMA Crossover Payload

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

### Selected OR Instrument State

```json
{
  "selected": true,
  "instrument_key": "NSE_FO|41012",
  "selected_level": "R3",
  "level_value": 126.75,
  "trigger_price": 128.5,
  "trigger_field": "high",
  "touch_time": "2026-08-06T09:18:05+05:30",
  "touch_source": "live_tick",
  "selected_at": "2026-08-06T09:18:06+05:30",
  "contract_info": {
    "instrument_type": "CE",
    "strike_price": 24600.0,
    "trading_symbol": "NIFTY 24600 CE"
  },
  "range": {},
  "levels": {},
  "latest_live_data": {
    "ltp": 128.5,
    "high": 128.5,
    "low": 121.5,
    "close": 127.0,
    "timestamp": "2026-08-06T09:18:00+05:30"
  },
  "latest_main_index_ltp": 24580.25,
  "ema_alerts_count": 1,
  "last_ema_alert": {}
}
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
7. Wait for live tick to select first R3/S3 touched option instrument by default
```

---

## Telegram Alert Behavior

### New default behavior: Selected OR instrument + EMA cross

Rules:

```text
1. First live option instrument that touches/crosses R3 or S3 is selected permanently.
2. After selection, all other instruments are ignored for selected OR Telegram flow.
3. EMA crossover alerts are sent only for the selected instrument.
4. Each EMA cross event is protected from duplicate sends using:
   instrument_key + timestamp + cross_type
5. Telegram message includes:
   - selected instrument details
   - selected R3/S3 touch details
   - EMA cross data
   - current NIFTY LTP
   - selected instrument live LTP/high/low/close
   - Opening Range levels
```

Example:

```text
09:18:05 - 24500 CE touches R3
09:18:10 - 24600 CE touches R3
09:19:00 - 24600 CE EMA bullish cross
09:19:30 - 24500 CE EMA bullish cross
09:24:00 - 24500 CE EMA bearish cross
```

Expected behavior:

```text
24500 CE is selected permanently because it touched R3 first.
24600 CE is ignored even though its EMA crossed first.
Telegram sent at 09:19:30 for 24500 CE bullish cross.
Telegram sent at 09:24:00 for 24500 CE bearish cross.
```

Example selected OR EMA Telegram content:

```text
Selected OR Instrument EMA Cross

EMA crossover detected for the permanently selected Opening Range instrument.

Selected Instrument:
Symbol: NIFTY 24500 CE
Instrument Key: NSE_FO|41012
Selected Level: R3
Level Value: 126.75
Trigger high: 128.5
Touch Time: 2026-08-06T09:18:05+05:30
Touch Source: live_tick

EMA Cross Data:
Cross Type: bullish_cross
Cross Time: 2026-08-06T09:21:00+05:30
Close: 124.5
EMA Fast Period: 9
EMA Slow Period: 21
EMA Fast: 123.8
EMA Slow: 122.9
Previous EMA Fast: 122.1
Previous EMA Slow: 122.4
Previous Signal: bearish
Current Signal: bullish

Current Live Data:
Current NIFTY LTP: 24580.25
Instrument LTP: 128.5
Instrument High: 129.0
Instrument Low: 121.4
Instrument Close: 127.0
Live Data Time: 2026-08-06T09:21:00+05:30

Opening Range Levels:
R1: ...
R2: ...
R3: ...
S1: ...
S2: ...
S3: ...
R3 Threshold: ...
S3 Threshold: ...
```

### Legacy touch alert behavior

Legacy R3/S3 touch batch alerts are disabled by default for the new flow.

```python
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False
```

If enabled, the old grouped Telegram behavior can still send max nearest touched instruments:

```text
If 15 instruments touch R3/S3 together:
    -> rank touched option instruments by abs(strike_price - NIFTY_LTP)
    -> select nearest 5 only
    -> send one Telegram message
```

Duplicate prevention:

```text
Same instrument + same level touch event is tracked once per day/run.
Example:
    24500 CE R3 -> tracked once
    24500 CE R3 again -> skipped
    24500 CE S3 later -> allowed once
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

### Opening Range Manual Fetch

```text
POST http://127.0.0.1:8000/opening-range/fetch?candle_count=1&save_results=true&max_workers=8
```

### Opening Range Status

```text
GET http://127.0.0.1:8000/opening-range/status
```

### Selected OR Instrument Status

```text
GET http://127.0.0.1:8000/opening-range/selected-instrument
```

### Selected OR Instrument EMA Alerts

```text
GET http://127.0.0.1:8000/opening-range/selected-instrument/ema-alerts?limit=100
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
- By default, backfill touch events do not permanently select the OR instrument.
- By default, only `live_tick` can permanently select the first R3/S3 touched option instrument.
- After one selected OR instrument is locked, other instruments are ignored for selected OR Telegram flow.
- EMA Telegram alerts are sent only for selected OR instrument.
- The same EMA cross is protected from duplicate Telegram sends using instrument key, timestamp, and cross type.
- Legacy Opening Range touch Telegram alerts are disabled by default.
- Global EMA WebSocket receives all crossover events.
- Instrument-specific EMA WebSocket receives only the selected instrument's crossover events.
- Global Opening Range WebSocket receives all Opening Range events.
- Instrument-specific Opening Range WebSocket receives only selected instrument events.
- If no local WebSocket clients are connected, normal tick broadcasting is skipped to reduce event-loop load.
- If `LIVE_EMA_ENABLED=True`, live EMA processing can continue even when no dashboard client is connected.
- If `OPENING_RANGE_TOUCH_ALERT_ENABLED=True`, Opening Range live touch monitoring can continue even when no dashboard client is connected.
- If `OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED=True`, selected OR EMA Telegram alert processing can continue even when no dashboard client is connected.
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
- Telegram notifications are integrated through `services/telegram_service.py`.
- Historical EMA 9/21 crossover calculation is integrated through `services/history_service.py`.
- Live EMA continuation is integrated through `services/live_ema_service.py`.
- Opening Range calculation, R3/S3 touch tracking, first touched instrument selection, and selected OR EMA alerting are integrated through `services/opening_range_service.py`.
- Upstox live tick processing bridges EMA cross events into selected OR EMA Telegram alerts through `services/upstox_websocket.py`.
- Global EMA crossover streaming is available through `/ws/ema-crossover`.
- Instrument-specific EMA crossover streaming is available through `/ws/ema-crossover/instrument`.
- Global Opening Range streaming is available through `/ws/opening-range`.
- Instrument-specific Opening Range streaming is available through `/ws/opening-range/instrument`.
- Selected OR instrument status is available through `/opening-range/selected-instrument`.
- Selected OR EMA alert records are available through `/opening-range/selected-instrument/ema-alerts`.
- Dashboard displays live NIFTY 50 and nearest CE/PE option cards from `/all-feeds`.
