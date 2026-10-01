# temp3 Option Feed Engine

`temp3` is a FastAPI application for Upstox option market data. It manages an access token and option contracts, streams live option ticks, calculates EMA 9/EMA 21 from candle data, detects crossovers, processes Opening Range (OR) levels and instrument isolation, and exposes REST and WebSocket interfaces for dashboards and other clients.

EMA calculation now runs inside `temp3`. Normal EMA calculation and crossover delivery do not depend on the separately deployed `ema_v1` service or its WebSocket.

## Run the Application

Copy `.env.example` to `.env`, replace the required MongoDB placeholders, install the packages in `requirements.txt`, then start the FastAPI app from this project directory:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

`run.bat` is the Windows launcher and `Procfile` contains the deployment command. At minimum, configure MongoDB connection/database/token collection values (`MONGO_URL`, `MONGO_DB`, `TOKENS_COLLECTION`) and provide a valid Upstox access token through the configured token document. Market selection and candle behavior are controlled by settings in `core/config.py`, including `STRIKE_FROM`, `STRIKE_TO`, `MARKET_TIMEZONE`, `HISTORICAL_CANDLE_DAYS`, `EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD`, and `LIVE_EMA_ENABLED`. Telegram and Algo App delivery are separately configurable and can be disabled.

## What the Project Does Now

At startup, the FastAPI lifespan initializes runtime configuration, refreshes the Upstox token from MongoDB, loads option contracts and builds the subscription list, then fetches historical and current-day intraday candles. The historical calculation seeds the internal EMA state. The application also catches up Opening Range data when its scheduled time has passed and today's result is unavailable.

The scheduler starts the internal completed-candle poller alongside token checks, instrument recovery, daily refresh, Opening Range refresh, and log archiving. The Upstox market-data WebSocket streams option ticks to `services/upstox_websocket.py`. EMA candle processing uses Upstox's intraday candle API: on a once-per-minute poll during configured market hours, the engine processes returned candles whose minute has completed. It updates state once per instrument and candle timestamp.

When EMA 9 crosses EMA 21, the engine builds one event, adds Opening Range context when available, passes it to the existing selected-instrument alert/isolation workflow, appends the event to a JSONL log, retains a bounded in-memory event history, and schedules a broadcast to connected WebSocket clients. REST clients can read current EMA state and in-memory crossover history.

## Previous Architecture

Before the migration, the calculation and consumer workflows ran in separate applications:

```text
                    PREVIOUS ARCHITECTURE

       Deployed ema_v1 service
       Historical candles → EMA 9/21 → cross detection
                              │
                       EMA WebSocket
                              │
                              ▼
                            temp3
                 external_ema_feed client
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼            ▼
           OR enrichment  Isolation    Telegram
                 └────────────┼────────────┘
                              ▼
                       Dashboard/API
```

`ema_v1` calculated EMA values and crossovers. `temp3` ran `services/external_ema_feed.py`, connected to the configured external EMA WebSocket, synchronized received state, and forwarded crossover messages into its own Opening Range, isolation, Telegram, and broadcast flows. The applications had separate runtimes, and EMA state and events crossed a network boundary. This was a separate-service architecture; the change consolidates the calculation and consumers into the existing `temp3` process.

## Why the Architecture Was Changed

`temp3` already owns the Upstox option feed, contract selection, Opening Range, isolation, dashboard, and notification workflows. Those features consume EMA crossover events directly. Integrating the calculation into that application removes the deployed `ema_v1` WebSocket as a runtime dependency and gives the application one internal EMA event path.

```text
Previous: ema_v1 ── external WebSocket ──► temp3

Current:   temp3
             ├── Option Feed
             ├── EMA Engine
             ├── Opening Range
             ├── Isolation and notifications
             └── Dashboard/API
```

## Current Architecture

```text
                              TEMP3
                ┌───────────────┴────────────────┐
                │                                │
          OPTION WORKFLOW                   EMA ENGINE
       Upstox contracts/ticks         Historical + intraday candles
                │                      EMA 9 / EMA 21 state
         Live tick handling                    │
                │                         Cross detector
                └───────────────┬────────────────┘
                                │
                         EMA crossover event
                 ┌──────────────┼──────────────┐
                 ▼              ▼              ▼
          OR enrichment     Isolation     Telegram/Algo App
                 └──────────────┼──────────────┘
                                ▼
                In-memory state + event JSONL log
                                │
                         ┌──────┴──────┐
                         ▼             ▼
                       REST       WebSocket clients
                         └──────┬──────┘
                                ▼
                            Dashboard
```

The option and EMA inputs are separate: the Upstox market stream supplies live ticks, while EMA candles are retrieved through the existing intraday history service. `ws_feed.broadcaster` routes outgoing events; it does not calculate EMA or own its state.

## Option Feed Flow

1. `services/token_service.py` loads the Upstox access token from the configured MongoDB token document. Startup and refresh workflows call the token service before loading market data.
2. `services/option_service.py` fetches option contracts, selects the nearest expiry and configured strike range, and stores contract metadata and subscription keys in `options_cache`.
3. `services/upstox_websocket.py` starts `MarketDataStreamerV3` for the selected keys and configured feed mode.
4. Incoming ticks are decoded and matched to cached contract information. `services/opening_range_service.py` receives ticks for live touch processing. `ws_feed.broadcaster` sends market ticks to eligible `/all-feeds`, `/ws`, and matching `/option` clients.
5. EMA calculation is performed by the separate internal candle poller described below; tick prices do not trigger repeated EMA calculations.

Relevant implementation files are `services/token_service.py`, `services/option_service.py`, `services/upstox_websocket.py`, `services/history_service.py`, and `ws_feed/broadcaster.py`.
 
## EMA Engine

The internal engine is `services/ema_engine.py` (`InternalEmaEngine`, singleton `internal_ema_engine`). Historical fetching and EMA calculation remain in `services/history_service.py`; its `initialize_live_ema_from_history()` function passes the resulting summary into the engine. EMA periods are configured by `EMA_FAST_PERIOD` and `EMA_SLOW_PERIOD` (defaults 9 and 21).

### Initialization

```text
Startup or refresh
       ↓
Token and option subscriptions available
       ↓
Historical candles plus today's intraday candles when applicable
       ↓
Chronological EMA calculation in history_service
       ↓
Seed each instrument's internal EMA state
```

The existing history service fetches data for subscribed instruments, calculates EMA values and crossover summaries, and supplies each instrument's latest EMA values, timestamp, price, signal, and candle count to the internal engine. Instruments without valid fast and slow EMA values are not initialized. This historical summary seeds current state; live crossover dispatch happens as new completed candles are processed.

### Live Processing

The existing APScheduler instance runs `internal_ema_engine.poll_completed_candles` every 10 seconds. The poller gates work to weekdays and configured market hours, then limits processing to one poll per minute. It requests `1minute` intraday candles for subscribed instruments using `fetch_intraday_candles_for_instrument()` and a worker pool sized from `HISTORICAL_CANDLE_MAX_WORKERS`.

The engine parses and sorts returned candles, skips the candle whose minute is still open, and calls `process_candle()` for completed candles in timestamp order. Each instrument state retains `last_processed_timestamp`. Candles with a timestamp at or before that value are skipped, so repeated API results do not recalculate or re-emit a crossover. A failed instrument poll is logged while other completed futures are still collected.

```text
Intraday 1-minute candles
       ↓
Discard the still-open minute
       ↓
Compare candle timestamp with instrument state
       ├── Already processed → skip
       └── New completed candle → update EMA state
                                  ↓
                              Cross detector
```

The state includes instrument key, EMA 9 and EMA 21 values, their difference, last processed timestamp, last price, current trend, candle count, and initialization/live-processing flags. State is in memory and is re-seeded from history at startup or refresh.

The REST crossover history is the bounded in-memory event list and is not reconstructed from the JSONL file at startup. The JSONL file is an append-only event record; EMA state itself is recalculated/re-seeded from candle history.

## EMA Crossover Detection

For a newly processed candle, the engine applies the standard recursive update with multipliers `2 / (period + 1)` to the previous fast and slow EMA values. It compares the previous and current EMA relationship:

```text
Previous EMA 9 <= EMA 21
Current  EMA 9 >  EMA 21  → BULLISH crossover

Previous EMA 9 >= EMA 21
Current  EMA 9 <  EMA 21  → BEARISH crossover
```

Remaining on the same side produces an updated state but no crossover event. The event is generated only on a side transition. Historical EMA calculation uses full precision between candles, consistent with the `ema_v1` calculation behavior; event display values are rounded when built.

## EMA Event Flow

The engine creates an event with fields used by the consumers: `type`, `event_type`, `source`, `instrument_key`, `trading_symbol` when available, `contract_info`, `cross_type`, uppercase `signal`, `timestamp`, `candle_timestamp`, `ema_fast`, `ema_slow`, `ema_9`, `ema_21`, previous EMA values, `price`, `close`, and `ema_calculation_mode`.

```text
InternalEmaEngine.process_candle()
       ↓ crossover only
Normalized live_ema_cross event
       ├── Opening Range context lookup
       ├── Selected/isolated-instrument alert processing
       ├── Append logs/live_ema_events.jsonl
       ├── Bounded in-memory crossover history
       └── ws_feed.broadcaster.broadcast_ema_cross()
```

Failures during Opening Range enrichment, isolation dispatch, event-log writing, or WebSocket scheduling are logged. Those failures do not roll back the EMA state already updated for the candle.

## Opening Range Integration

`services/ema_engine.py` calls `get_opening_range_levels_for_ema_event()` from `services/opening_range/ema_alerts.py` after detecting a crossover. That method looks up the instrument's cached Opening Range levels and touch status and adds available intraday-close, main-index LTP, processing-time, and isolated-instrument context. If no matching OR result exists, it returns an empty/default context; the EMA event continues through the rest of the flow.

Opening Range is calculated and cached by the existing `services/opening_range` implementation and `services/opening_range_service.py`. Startup catch-up and the scheduled fetch use the same service; live ticks are also considered for configured level-touch handling. EMA calculation itself does not require OR data.

## Instrument Isolation

The OR workflow monitors configured touch levels and uses the existing selection rules to maintain a selected/isolated instrument. EMA events are produced for initialized subscribed instruments. `process_selected_or_ema_cross_alert()` and its detailed implementation in `services/opening_range/ema_alerts.py` evaluate an event against the current isolated-instrument state and configuration. Telegram EMA alerts are scoped to the selected/isolated instrument; WebSocket events can still be broadcast for other subscribed instruments.

## Telegram Notifications

`services/telegram_service.py` is the application's Telegram implementation. Existing startup, shutdown, token, contract/subscription, refresh, and exception notifications remain in use. After successful EMA initialization, `main.py` attempts a “LIVE EMA CALCULATION STARTED” notification with the initialized instrument count, periods, interval, and configured market time. Isolated EMA crossover messages are built and dispatched by the existing EMA alert/Telegram workflow when enabled.

Telegram is configuration-dependent. When it is disabled or not configured, the service logs/skips the send; a notification send failure is handled as a notification failure and does not cause an EMA notification retry loop. The startup EMA notification call is guarded and logs its own error.

## WebSocket Architecture

The WebSocket API is served by `temp3`. `ws_feed.broadcaster` fans out market ticks, EMA crossover events, and Opening Range events to registered clients. EMA state remains in `internal_ema_engine`; the broadcaster is not the state owner.

| Endpoint | Behavior |
| --- | --- |
| `/all-feeds` and `/ws` | All-feed connection for live ticks and eligible EMA/Opening Range events. |
| `/option?strike=…&striketype=ce` or `striketype=pe` | Matching option feed and related eligible events. |
| `/ws/ema` and `/ws/ema-crossover` | EMA events across initialized instruments; connection sends an `initial_state` snapshot. |
| `/ws/ema/{instrument_key}` | Path-style single-instrument EMA stream. |
| `/ws/ema-crossover/instrument?instrument_key=…` | Single-instrument EMA stream; can also resolve by `strike` and `striketype`. |
| `/ws/opening-range` | Opening Range events. |
| `/ws/opening-range/instrument` | Instrument-specific Opening Range events, with instrument query inputs. |

The global and instrument-specific EMA connections send a current state message on connection, then receive crossover broadcasts. A client connected to the all-instrument endpoint receives the instrument list; an instrument-specific connection receives that instrument's current state. The broadcaster routes instrument-specific events to the matching instrument pool and global events to the global EMA pool. The all-feed and option pools also receive events according to the broadcaster's contract matching rules.

## REST API

Representative current routes include:

| Route | Purpose |
| --- | --- |
| `GET /health` | Reports token, subscription-cache, and Upstox-stream status. |
| `POST /refresh/manual` | Manually refreshes token/contracts/history/EMA initialization and the applicable Opening Range state. |
| `GET /refresh/status` | Reports manual refresh and current subscription-cache status. |
| `GET /history/live-ema/state?instrument_key=…&limit=100` | Returns current internal EMA state and recent in-memory crossovers; `instrument_key` and `limit` are optional. |
| `GET /instruments` and `GET /option-chain` | Read the current instrument/option data. |
| `GET /opening-range/status` and `GET /opening-range/dashboard` | Read Opening Range and isolation status/dashboard data. |
| `GET /opening-range/ema-context` | Reads Opening Range context for a resolved instrument. |
| `GET /opening-range/selected-instrument/ema-alerts` | Reads selected-instrument EMA alert records. |

Other routes are defined in `api/`. `/history/live-ema/state` is the dedicated endpoint for current internal EMA state and in-memory crossover history. Historical candle fetching and the historical EMA summary are part of startup/refresh processing; the current implementation does not expose a separate dedicated REST route for raw historical EMA series.

## Previous vs Current Architecture

| Area | Previous architecture | Current architecture |
| --- | --- | --- |
| EMA calculation | Deployed `ema_v1` application | `temp3.services.ema_engine` seeded by `history_service` |
| EMA communication | External WebSocket client in `temp3` | Internal calls and event dispatch within `temp3` |
| EMA runtime | Separate service lifecycle | Shared FastAPI application and APScheduler lifecycle |
| Live EMA input | Events/state from external EMA feed | Completed one-minute candles fetched through Upstox history API |
| EMA state | External state synchronized by consumer | Internal per-instrument state, re-seeded from history |
| Cross delivery | External WebSocket event | Internal event passed to OR/isolation and broadcast locally |
| Opening Range / isolation | `temp3` consumer workflows | Existing `temp3` workflows consume internal events |
| Dashboard and APIs | `temp3` | `temp3` |
| Deployment dependency | Deployed EMA endpoint required for live crossovers | No normal external EMA service dependency |
| Scheduler | Separate EMA/application schedules | EMA poll job added to `temp3`'s scheduler |
| Event persistence | External feed messages were appended by its client | Crossovers are appended to `logs/live_ema_events.jsonl`; selected alert workflows retain their own persistence |

## Changes Implemented

The table lists the migration and immediate integration fixes reflected in the current working tree.

| # | File / component | Previous behavior | New behavior | Change type |
| --- | --- | --- | --- | --- |
| 1 | `services/ema_engine.py` | No authoritative internal live EMA engine | Adds historical state seeding, completed-candle polling, per-instrument timestamp deduplication, crossover detection, event dispatch, state snapshots, and event log/history | Added |
| 2 | `services/external_ema_feed.py` | Connected to deployed EMA WebSocket, cached remote state, persisted messages, and dispatched events | Removed; equivalent downstream dispatch now starts from the internal engine | Removed |
| 3 | `services/history_service.py` | Calculated historical summaries but skipped local live-state initialization; rounded each recursive EMA step | Retains full precision between candles and seeds the internal engine from the historical summary | Modified |
| 4 | `main.py` | Started/stopped external EMA client | Registers the internal poll job with the existing scheduler, wires its event loop, reports startup state, and shuts down the internal engine | Modified |
| 5 | `core/config.py` | Included external EMA URL/output/reconnect settings | Removes external EMA settings; adds configured market-close hour/minute for the poller | Modified |
| 6 | `requirements.txt` | Included the separate `websockets` client dependency | Removes that external-client dependency | Modified |
| 7 | `services/upstox_websocket.py` | Reported external EMA feed status/mode | Reports the internal engine status/mode; retains live tick and OR processing | Modified |
| 8 | `services/ema_alert_simulation_service.py` | Read live EMA state from the external-feed cache | Reads internal EMA state | Modified |
| 9 | `ws_feed/websocket_routes.py` | Described/served the consumer-side external event route only | Exposes `/ws/ema` and instrument path alias, sends initial state, and identifies the internal event source in current route flow | Modified |
| 10 | `api/history_routes.py` | No dedicated internal EMA state route | Adds `GET /history/live-ema/state` for current state and in-memory crossovers | Modified |
| 11 | `architecture.md` | Described the external EMA flow | Describes the internal engine and event flow | Documentation |
| 12 | `README.md` | Contained a startup/OR checklist | Replaced with current system architecture, lifecycle, API, and migration documentation | Documentation |

Existing Opening Range, isolation, Telegram, and broadcaster implementations were reused rather than replaced by parallel services.

## Removed External EMA Dependency

The old runtime relationship was:

```text
temp3 → external_ema_feed → deployed ema_v1 WebSocket
```

It is now:

```text
temp3 → internal_ema_engine → local event consumers
```

The current code no longer defines `EXTERNAL_EMA_WEBSOCKET_URL`, imports `services.external_ema_feed`, or references the former deployed `tfeed.up.railway.app/ws/ema` endpoint. The old client module was deleted and its `websockets` dependency removed. `/ws/ema` is now a local client endpoint served by `temp3`; it is not a connection to the old service.

## Application Lifecycle

### Startup and refresh

```text
FastAPI lifespan
   ↓
Startup cleanup and runtime configuration
   ↓
MongoDB token refresh
   ↓
Option contract fetch and subscription cache
   ↓
Historical/intraday candle fetch and EMA state seeding
   ↓
Opening Range startup catch-up when applicable
   ↓
Existing scheduler + Telegram token bot + Upstox market streamer
```

The daily hard refresh reloads the token and option subscriptions, refreshes historical EMA initialization, and restarts the Upstox streamer with current subscriptions. The scheduler also runs the internal EMA poll every 10 seconds; the engine itself enforces its once-per-minute and market-time gates. The Opening Range fetch has its own configured daily schedule.

### Market processing

Upstox ticks feed option/WebSocket and OR touch processing. Separately, the EMA engine queries completed one-minute intraday candles, updates state and emits only transition events. At weekends or outside the configured market window, the EMA poller returns without fetching candles. The latest completed minute is allowed through the configured close boundary.

### Shutdown

The FastAPI lifespan stops the Telegram token bot when it was started, stops the Upstox streamer, marks the internal EMA engine stopped, shuts down the scheduler, closes runtime configuration, and sends the configured shutdown notification. Cleanup errors are logged and included in shutdown reporting where possible.

## Error Handling

Failures are logged at the component that encounters them. Existing startup/refresh paths send Telegram error messages where configured. During live EMA polling, a failed instrument future is logged and does not prevent the remaining futures from being collected. OR enrichment, isolation dispatch, event-log persistence, and WebSocket scheduling have local exception handling so those failures do not undo the already-updated EMA state. This does not guarantee successful external data retrieval or alert delivery; those still depend on Upstox, MongoDB, Telegram configuration, and runtime availability.

## Data Flow Summary

```text
MongoDB token + Upstox contracts
                ↓
         Subscription cache
                ↓
     ┌──────────┴──────────────┐
     ▼                         ▼
Upstox live ticks       Intraday candle API
     │                         ↓
Option / OR ticks       Completed 1-minute candles
     │                         ↓
     │                    Internal EMA 9/21
     │                         ↓
     │                    Cross detector
     │                         ↓
     │                    EMA cross event
     │              ┌──────────┼──────────┐
     │              ▼          ▼          ▼
     │          OR context  Isolation   Event log
     │              └──────────┼──────────┘
     │                         ▼
     └──────────────► REST / WebSocket clients
                               ↓
                           Dashboard
```

The token enables Upstox access; the contract manager determines which option instruments are available and subscribed. The live streamer supplies ticks. The history service supplies the candle sequences used for EMA warmup and live updates. A crossover event can acquire cached OR context, then the existing isolation alert workflow decides whether the selected-instrument Telegram/Algo notification applies. The event log and internal state support local status and client delivery.

## Current Project Responsibilities

| Component | Responsibility |
| --- | --- |
| Token service | Read and cache the Upstox access token from MongoDB. |
| Contract manager | Fetch, filter, cache, and subscribe selected option contracts. |
| Option feed | Stream and route Upstox market ticks. |
| EMA engine | Own per-instrument live EMA state and coordinate completed-candle processing. |
| Candle processor | Fetch one-minute intraday candles, filter unfinished bars, normalize timestamps, and skip already processed bars. |
| Cross detector | Emit an event only when EMA 9 and EMA 21 change sides. |
| Opening Range | Calculate/cache OR levels and touch state; enrich EMA events when available. |
| Isolation | Apply existing selection rules and process alerts for the selected instrument. |
| Telegram | Deliver configured lifecycle, refresh, and isolated EMA notifications. |
| WebSocket broadcaster | Fan out ticks, EMA crossovers, and OR events to connected clients. |
| REST API | Expose health, refresh, instrument, Opening Range, and internal EMA state endpoints. |
| Persistence | Store existing application data in its configured stores/files; append EMA crossover events to `logs/live_ema_events.jsonl`. |
| Scheduler | Coordinate token checks, recovery, daily refreshes, EMA polling, OR work, and log archiving. |

## What the Previous Project Did

The earlier design used `ema_v1` as an independent calculation service. That service fetched candles, built EMA 9/21 state, detected crosses, and published events over its WebSocket. `temp3` connected to that endpoint using `ExternalEMAFeed`, mirrored remote state, enriched crossovers with its Opening Range cache, processed isolation/Telegram rules, and broadcast events to local clients. The design assigned EMA calculation to a separate service; the migration moves that responsibility into `temp3`.

## EMA Migration Summary

1. EMA calculation and state existed in the dedicated `ema_v1` application.
2. `temp3` consumed remote EMA state and crossover events through an external WebSocket client.
3. `temp3` gained an internal engine seeded by its existing history/candle service.
4. The external client, URL/output/reconnect configuration, and dedicated WebSocket package dependency were removed from the normal runtime.
5. Completed intraday candles now update internal EMA state; crossover events flow to existing local consumers and WebSocket clients.

## Final Conclusion

`temp3` is now one market-processing application containing the Option Feed, EMA Engine, Opening Range, instrument isolation, configured notifications, persistence, REST APIs, and WebSocket delivery. Its normal EMA calculation and crossover flow runs within the `temp3` runtime.

```text
TEMP3
├── Market data and option processing
├── EMA calculation and crossover detection
├── Opening Range and isolation
├── Telegram / configured Algo notifications
├── Persistence and runtime state
└── REST and WebSocket APIs
```

The EMA engine is now an internal component of `temp3` rather than an externally deployed service consumed through a WebSocket.
