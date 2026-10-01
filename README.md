# Option Feed Engine

FastAPI market-data application using Upstox for option contracts, candles, and market streaming. The application maintains option instrument metadata, calculates Opening Range (OR) levels, warms and updates EMA state from candles, detects OR touches, applies existing isolation and alert rules, and exposes REST, HTML dashboard, and WebSocket interfaces.

The current branch adds configuration metadata for NIFTY, BANKNIFTY, and SENSEX, a per-underlying context container, completed-candle OR touch processing, and a narrower upstream WebSocket subscription list. These additions are not yet a complete multi-index strategy runtime: the existing option, EMA, OR, and isolation services still use shared NIFTY-centered state. See [Known limitations](#known-limitations) before enabling non-NIFTY indexes in a live deployment.

## Run the application

Use Python with the packages listed in `requirements.txt`:

```powershell
Copy-Item .env.example .env
# Edit .env with MongoDB and Upstox token-store settings.
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

The Upstox access token is obtained through the existing MongoDB token lifecycle; this README does not assume an unauthenticated candle API. `run.bat`, `start.sh`, and the `Procfile` are also present as launch/deployment helpers. Keep secrets in `.env` or the configured secret store; do not commit them.

## Project overview

The main runtime components are:

- **Market inputs:** Upstox option-contract metadata, historical candles, current-day intraday candles, and a Market Data Stream V3 connection.
- **Strategy calculations:** an internal EMA engine and Opening Range calculation/touch services.
- **Discovery and selection:** touch events pass through the existing isolation eligibility, priority, and daily-selection rules.
- **Delivery:** EMA and OR events, live ticks, REST APIs, dashboard pages, Telegram, and configured Algo App integrations.

The application has one FastAPI process and one set of legacy strategy singletons. It is not currently running three fully independent strategy instances.

## Architecture at a glance

```text
Configuration
  ├── enabled index keys → index feed metadata and base WS keys
  └── one shared option configuration/cache (currently NIFTY default)

Upstox option contracts ──→ shared options_cache ──→ candle polling
Upstox History API ────────→ completed 1-minute candles
                                  ├──→ per-instrument EMA state/cross events
                                  └──→ OR touch check against shared OR result
                                                     │
                                                     ▼
                                      shared isolation selection slot
                                                     │
Enabled index feeds ────────────────────────────────┤
Selected instrument ────────────────────────────────┘
             │
             ▼
Upstox Market Data Stream V3 → tick broadcaster → REST/WS clients and charts
```

### Historical and current behavior

The application began with a NIFTY-oriented option cache and one Opening Range/isolation state. Live Upstox ticks were used for pre-isolation OR touch evaluation. EMA has already been moved to an internal completed-candle engine and remains candle-based.

This branch changes the touch path: the EMA candle poller now calls the OR touch processor for each newly processed candle. Upstream WS keys are now computed from enabled index feeds plus the currently selected instrument instead of the entire option cache. However, the contract cache and isolation state were not converted to per-underlying stores; see below for the exact scope.

## Index configuration and current scope

`core/config.py` builds `STRATEGY_UNDERLYINGS` and `ACTIVE_STRATEGY_UNDERLYINGS`. Configuration values come from environment variables first, then the existing JSON config file (`APP_CONFIG_FILE`, `config/app_config.json`, or `config.json`), then defaults.

| Environment key | JSON key | Default | Current effect |
| --- | --- | --- | --- |
| `STRATEGY_NIFTY_ENABLED` | `strategies.nifty.enabled` | `true` | Includes NIFTY in active index metadata and the live WS base-key list. Its security key is `MAIN_NIFTY_SECURITY`. |
| `STRATEGY_BANKNIFTY_ENABLED` | `strategies.banknifty.enabled` | `false` | Includes BANKNIFTY in active index metadata and the live WS base-key list when enabled. |
| `BANKNIFTY_SECURITY` | `strategies.banknifty.instrument_key` | `NSE_INDEX\|Nifty Bank` | BANKNIFTY index instrument key. |
| `STRATEGY_SENSEX_ENABLED` | `strategies.sensex.enabled` | `false` | Includes SENSEX in active index metadata and the live WS base-key list when enabled. |
| `SENSEX_SECURITY` | `strategies.sensex.instrument_key` | `BSE_INDEX\|SENSEX` | SENSEX index instrument key. |
| `MAIN_NIFTY_SECURITY` | `market.main_nifty_security` | `NSE_INDEX\|Nifty 50` | NIFTY index instrument key and default for option-contract loading. |

Example environment configuration:

```dotenv
STRATEGY_NIFTY_ENABLED=true
STRATEGY_BANKNIFTY_ENABLED=false
BANKNIFTY_SECURITY=NSE_INDEX|Nifty Bank
STRATEGY_SENSEX_ENABLED=true
SENSEX_SECURITY=BSE_INDEX|SENSEX
```

The current behavior of these flags is narrower than a full strategy enable/disable switch. They control active index feed metadata, chart instrument listings, interval classification, and base upstream WS index keys. They do **not** yet gate contract loading, EMA warmup/polling, OR calculation, touch state, alerts, or isolation by underlying. The regular startup contract loader still calls `get_options_contracts()` without an index argument, which defaults to NIFTY.

Other relevant existing settings include:

| Key | Purpose / implementation detail |
| --- | --- |
| `STRIKE_FROM`, `STRIKE_TO` | One shared strike filter used when choosing nearest-expiry contracts. Defaults are `22500` and `25000` in `core/config.py`; `.env.example` sets `23000` and `25000`. |
| `MARKET_TIMEZONE`, `MARKET_OPEN_HOUR`, `MARKET_OPEN_MINUTE`, `MARKET_CLOSE_HOUR`, `MARKET_CLOSE_MINUTE` | Timezone and EMA polling window. Defaults: `Asia/Kolkata`, 09:15–15:30. |
| `LIVE_EMA_ENABLED` | Enables/disables internal completed-candle EMA polling. |
| `EMA_FAST_PERIOD`, `EMA_SLOW_PERIOD` | General EMA configuration defaults to 9/21 and is used by other EMA/history paths. The internal incremental engine currently stores and updates `ema_9` and `ema_21` directly; its incremental alpha values are fixed at 2/10 and 2/22. |
| `LIVE_EMA_FAST_PERIOD`, `LIVE_EMA_SLOW_PERIOD`, `LIVE_EMA_INTERVAL_MINUTES` | Defined in configuration, but the internal poller currently requests `1minute` and the incremental update uses fixed 9/21 fields. These keys do not currently reconfigure that path. |
| `HISTORICAL_CANDLE_MAX_WORKERS` | Caps concurrent per-instrument candle requests. |
| `OPENING_RANGE_ENABLED`, `OPENING_RANGE_CANDLE_COUNT`, `OPENING_RANGE_FETCH_HOUR`, `OPENING_RANGE_FETCH_MINUTE` | Enables OR, sets opening candle count (default one), and sets its daily fetch time (defaults to 09:18). |
| `OPENING_RANGE_TOUCH_CHECK_MODE` | Legacy live-touch mode value (`high_low` or `ltp`). The new completed-candle processor evaluates candle high/low using the existing directional thresholds. |
| `OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL` | Enables once-per-instrument/level duplicate suppression; default true. |
| `OPENING_RANGE_ISOLATION_TOUCH_LEVELS`, `OPENING_RANGE_ISOLATION_PRIORITY_LEVELS` | Configured isolation levels and priority. Defaults are `R3`. |
| `WEBSOCKET_FEED_MODE` | Upstox stream mode; default `full`. |

See `core/config.py` and `.env.example` for the complete settings catalogue. Many additional alert, order, Telegram, runtime-config, and service-control settings are intentionally omitted from this architecture-focused table.

## Strategy context and index/option relationship

`services/strategy_context.py` defines a `StrategyContext` dataclass with:

- `underlying`, `index_instrument_key`, `display_name`, and `enabled`;
- `option_universe`, keyed by option instrument key;
- dictionaries/lists for `opening_range`, `ema_state`, `ema_events`, `touch_state`, `candidates`, `selected_instrument`, and `alert_state`;
- `set_option_universe()`, which copies contracts and stamps `underlying` plus `underlying_instrument_key` metadata;
- `snapshot()`, which returns a deep-copied representation.

`get_strategy_contexts()` lazily creates one in-memory context for each configured supported index and filters disabled contexts by default. This is a **state-container foundation**, not the state store currently used by the OR or EMA engines. Existing processing code does not populate these contexts or route events through them yet.

The option master response contains each contract's instrument key, CE/PE type, strike, expiry, and provider-supplied underlying metadata. `get_options_contracts(instrument_key=...)` can request contracts for a specified index, but it writes the response to the single global `options_cache`. The application startup path requests only the default NIFTY security. The current strike filter is global, not per index. Consequently, the intended mapping:

```text
NIFTY      → NIFTY CE/PE contracts
BANKNIFTY  → BANKNIFTY CE/PE contracts
SENSEX     → SENSEX CE/PE contracts
```

is not yet maintained as three independent runtime universes. Do not treat the context helper's metadata stamping as proof that contracts have been loaded or processed for all three indexes.

## Candle processing and EMA

`InternalEmaEngine` in `services/ema_engine.py` owns per-instrument EMA state keyed by instrument key. `initialize_from_history()` seeds each state's latest fast/slow values, timestamp, close, trend, and valid-candle count from the historical initialization summary. The app's startup and hard-refresh flows use the existing candle/history services to produce that summary.

The scheduler invokes `poll_completed_candles()` on weekdays at second 10 of each minute. The engine has its own guards: it only polls between configured market open and one minute after close, avoids a second poll in the same market minute, refuses overlapping poll cycles, and checks returned timestamps against the current minute cutoff. The API requests are made through the existing authenticated Upstox history service with the `1minute` interval. Per-instrument requests run in a bounded thread pool; an individual future error is logged while other instruments can finish.

For each newly processed candle timestamp, the engine updates EMA state once. Timestamps at or before `last_processed_timestamp` are skipped. The incremental update is:

```text
fast = old_fast + (2 / 10) × (close - old_fast)
slow = old_slow + (2 / 22) × (close - old_slow)
```

A crossover is emitted when the fast/slow difference changes sign. The event includes instrument key, contract metadata when found, cross direction, candle timestamp, close, prior/current EMA values, and calculation mode. Events are retained in bounded memory, appended to `logs/live_ema_events.jsonl`, enriched with OR context when available, dispatched to existing selected-instrument alert logic, and broadcast to connected local EMA WebSocket clients. Persistence of the crossover is also attempted through the existing history service. This is not a durable checkpoint of all in-memory EMA state; restart warmup reconstructs it from history.

```text
Authenticated intraday candle request
             ↓
Only timestamped, completed 1-minute candles
             ├──→ per-instrument EMA update → crossover event
             └──→ completed-candle OR touch check
```

EMA is not calculated from incoming WebSocket tick prices in this engine. The engine's instrument list currently comes from the shared `options_cache["subscribed_keys"]`, so the candle path is not yet filtered by the per-underlying enable flags.

## Opening Range and touch detection

`services/opening_range/service.py` fetches current-day intraday candles for the shared subscribed-key list and selects the configured number of opening candles (default one). The range calculator derives high, low, average, R1/S1, R2/S2, R3/S3, and the R3/S3 thresholds using the formula in `services/opening_range/range_calculator.py`. Results and per-instrument levels are written into one shared `opening_range_cache` in `services/opening_range/state.py`. OR fetch is scheduled for weekdays at the configured fetch time; startup catch-up can run if that time has passed and today's result is missing.

The new `process_completed_candle_for_opening_range()` path is called by the internal EMA poller after it accepts a newer candle for that instrument. It looks up the instrument in the shared OR result data, requires a successful OR calculation, and passes the candle to the existing touch detector. The detector preserves the established directional rule:

```text
R2 / R3: candle high >= level (uses close as fallback if high is invalid)
S2 / S3: candle low  <= level (uses close as fallback if low is invalid)
```

This is not a generic `low <= level <= high` containment check. Only levels in `OPENING_RANGE_ISOLATION_TOUCH_LEVELS` are considered by this detector (default `R3`). Touch alerts can be restricted to option contracts and are suppressed after an instrument/level key has been recorded when once-per-level is enabled. A touch event records `instrument_key`, level, level value, trigger price/field, parsed touch timestamp/date, source, contract metadata, candle, and alert key. It does not currently add an explicit underlying identifier.

Touch events update shared touch status and event queues, then go through existing isolation rules. Historical OR backfill scans post-opening candles separately when enabled. The completed-candle touch event source is `completed_1minute_candle`. The old live-tick touch function remains in the code for compatibility, but the Upstox stream handler no longer invokes it for pre-isolation discovery.

The OR cache, touch queues, duplicate keys, latest-main-index LTP, and selected-instrument state are all single shared runtime objects. OR calculation over option candles also does not create distinct OR state per index. The event's `main_index_ltp` / distance metadata is NIFTY-oriented.

## Isolation and alerts

Touch events are submitted to `try_isolate_from_touch_events()`. The existing isolation service filters eligible events using option-only configuration, configured level/priority, and the reference average/window rules; it chooses a best eligible event and commits it only if the daily shared selection slot permits. `should_replace_isolated_instrument()` currently blocks replacement once an instrument is selected for the market day. Selection state is reset according to the existing market-day reset path.

An isolated-instrument notification may be delivered through the configured Telegram integration. EMA cross events use the existing selected-instrument EMA alert path, which can also dispatch to the configured Algo App integration. Touch event batches may be flushed through the legacy Telegram path when enabled. Delivery options and filters are configured in `core/config.py` and the Opening Range constants/runtime-config service.

These flows currently have one selection slot and shared alert counters. They do not support a simultaneous NIFTY selection and SENSEX selection as independent strategies.

## Upstream WebSocket and selective subscriptions

`get_live_websocket_instrument_keys()` returns the configured active index instrument keys plus the instrument in the current shared selected-OR state, if one exists. The Upstox streamer starts with that list. While running, `services/upstox_websocket.py` re-evaluates the list every five seconds, subscribes newly added keys and unsubscribes removed keys through the existing `MarketDataStreamerV3` instance. On stream failure, the outer connection loop retries after a delay and builds a fresh initial list. This is the upstream Upstox subscription policy; it is distinct from local client WebSocket routes.

As a result, the full option universe is no longer passed as the upstream stream's initial subscription set solely for touch discovery. Strategy discovery uses candles. The stream carries configured index feed ticks and the selected instrument's live ticks. Tick messages are decoded and broadcast through `ws_feed.broadcaster`; tick handling does not call the OR live-touch detector. EMA calculations continue to use candles.

The selected instrument lookup is still the single shared selection state. Dynamic subscribe/unsubscribe recovery depends on the Upstox SDK's stream methods and a live connection; it has not been exercised by the automated tests in this repository/runtime. Also, other code still maintains the broader `options_cache["subscribed_keys"]` list for candle/history work, so “not subscribed to Upstox stream” does not mean “excluded from all application processing.”

## Frontend, API, and local WebSocket interfaces

The existing HTML pages and APIs remain instrument-centric. Active configured index feeds can appear in chart instrument listings, and index feed metadata identifies the configured underlying. The isolated dashboard and OR status APIs read the single shared selected instrument and shared OR cache; they do not yet expose a collection of independent per-index strategy selections.

Notable routes (routers are registered in `main.py`):

| Route | Purpose |
| --- | --- |
| `/` | Main live option-feed dashboard. |
| `/health` | Health and service/cache status. |
| `/api/instruments` | Loaded options cache instruments. |
| `/api/option-chain` | Existing option-chain view. |
| `/charts` and `/chart/{instrument_key}` | Chart instrument listing and chart page. |
| `/chart/api/{instrument_key}` | Chart JSON data. |
| `/candles/candles` | Historical plus intraday candle API (the router prefix is `/candles`, handler route is `/candles`). |
| `/opening-range/status`, `/opening-range/dashboard`, `/opening-range/cache` | OR summary/dashboard/shared cache. |
| `/opening-range/selected-instrument`, `/opening-range/isolated-instrument` | Current single selected instrument views. |
| `/history/live-ema/state`, `/history/ema-crosses` | Internal EMA state and crossover history. |
| `/isolated-dashboard` and `/isolated-ema-dashboard` | HTML isolated EMA dashboard and alias. |

Local WebSocket client endpoints include `/ws` and `/all-feeds` for broad tick/event delivery, `/option` for option-specific filtering, `/ws/ema` and `/ws/ema-crossover` for EMA crossover messages, `/ws/ema-crossover/instrument` and `/ws/ema/{instrument_key}` for instrument-filtered EMA, and `/ws/opening-range` plus `/ws/opening-range/instrument` for OR updates. These are local FastAPI client endpoints, not the upstream Upstox connection.

## Events and data delivery

An EMA crossover event uses `type: live_ema_cross` and `event_type: ema.crossover`, and carries its instrument, timestamp/candle timestamp, cross direction, close, EMA values, and contract metadata. OR enrichment is attached where available. It is persisted/buffered and broadcast via the local broadcaster.

An OR touch event uses `type: opening_range_touch`; it includes the instrument key, level, threshold, trigger field/price, touch timestamp, source, contract information, candle payload, and duplicate-control alert key. The current event schema does not promise a separate underlying field. Consequently, a consumer should use instrument metadata to determine the contract's underlying, and should not assume that the event is already isolated into an independent per-index stream.

```text
Completed candle ──┬──→ EMA state → EMA crossover → alert/persistence/local WS
                   └──→ OR levels → touch event → isolation → shared selection
                                                              │
Enabled index feed + shared selected key → Upstox WS → tick broadcast/dashboard
```

## Startup, market-day, and shutdown lifecycle

At FastAPI lifespan startup, the application performs startup cleanup, initializes runtime configuration, and runs its existing startup workflow. That workflow refreshes the token, loads the option contracts into the shared cache (NIFTY by default), fetches history/current-day candles and initializes EMA state, and performs OR catch-up when required. It then connects the EMA engine to the app event loop, starts the APScheduler, starts the Telegram token bot, and starts the Upstox streamer.

Relevant scheduler jobs include token refresh and validity checks, instrument recovery, weekday daily market hard refresh at 09:00, EMA candle polling at second 10 each minute, the configured weekday OR fetch (default 09:18), and daily archive jobs. The EMA poller applies its own configured market-open/close checks (defaults 09:15–15:30 in `Asia/Kolkata`) and skips weekends. The OR fetch schedule is independently configured. Current market processing is not orchestrated as one lifecycle per configured index.

During shutdown, the lifespan stops the Telegram bot if started, stops the Upstox streamer, marks the EMA engine stopped, shuts down the scheduler without waiting for all jobs, closes runtime configuration, and reports shutdown status through logging/Telegram where configured.

## Component reference

| Path | Responsibility |
| --- | --- |
| `main.py` | FastAPI lifespan, router registration, startup/recovery workflows, scheduler jobs, shutdown. |
| `core/config.py` | Environment/JSON configuration parsing, market settings, and supported underlying metadata. |
| `services/strategy_context.py` | Per-index state-container dataclass and lazy registry; not yet the runtime source of strategy state. |
| `services/token_service.py` | Upstox access-token retrieval and cache. |
| `services/option_service.py` | Contract API, nearest expiry/global strike filtering, shared options cache, feed metadata, and upstream live-key selection. |
| `services/history_service.py` | Existing historical/intraday candle workflows, EMA warmup/persistence helpers. |
| `services/ema_engine.py` | Completed-candle polling, per-instrument EMA state, duplicate guard, crossover events and delivery. |
| `services/opening_range/` | OR formula, intraday calculation, touch event handling, shared state, isolation and alert logic. |
| `services/opening_range/live_touch.py` | Legacy tick path plus completed-candle OR touch detection. The live stream no longer invokes the tick touch path. |
| `services/upstox_websocket.py` | Upstream Upstox stream lifecycle, selected-key subscription sync, tick handling and local client broadcast. |
| `ws_feed/broadcaster.py` | Local client connection registry and broadcast fan-out. |
| `ws_feed/websocket_routes.py` | Local FastAPI WebSocket client endpoints. |
| `api/opening_range_routes.py`, `api/history_routes.py` | OR dashboard/status/selection and EMA/history APIs. |
| `api/instrument_routes.py`, `api/chart_routes.py`, `api/candles_routes.py` | Instrument listing, charts, and candle endpoints. |
| `templates/` | HTML pages, including `isolated_ema_dashboard.html`, `chart.html`, and instrument/order views. |

## Testing and verification

The repository contains `tests/test_strategy_context.py`, with unit cases for context contract metadata, disabled context option clearing, independent context selection dictionaries, and directional candle-touch semantics. The test command is:

```powershell
python -m unittest discover -s tests -v
```

In the implementation environment, this command was attempted with the available bundled Python and could not import the test module because `python-dotenv` was unavailable there (`ModuleNotFoundError: dotenv`). No live Upstox, NIFTY-only end-to-end, multi-index independence, disabled-index lifecycle, dynamic upstream subscribe/unsubscribe, or dashboard integration scenario is documented as passing. Install the project requirements in the intended environment before using the command above.

The touch unit test currently asserts an R2 event, while the checked-in example configuration selects R3 by default. Since the detector only evaluates configured touch levels, that assertion should be reconciled with the test environment before treating the test module as a passing suite.

## Known limitations

- NIFTY remains the only index whose option contracts are loaded automatically by the application startup workflow.
- `STRATEGY_*_ENABLED` flags do not gate the complete strategy lifecycle; at present, they primarily affect index metadata/listings and the base upstream WebSocket key set.
- `StrategyContext` exists as a helper, but OR, EMA, touch, isolation, alert, and frontend code still uses legacy shared/global runtime structures.
- There is one global option cache, one OR cache/event queue set, one selected-instrument slot, and one NIFTY-oriented latest-index-LTP/distance path. Two indexes cannot be independently isolated at the same time.
- The option strike bounds are shared. `get_options_contracts()` replaces the shared cache for each call instead of maintaining an index-keyed cache.
- EMA polling reads all keys in the shared subscription cache and is not filtered by `ACTIVE_STRATEGY_UNDERLYINGS`. Its live incremental update uses fixed 9/21 alphas even though several period settings exist.
- OR event payloads do not contain an explicit underlying field. UI/API selection and dashboard state remain shared rather than keyed by underlying.
- Automated coverage is limited to the added unit module; the attempted run was blocked by the missing dependency described above. Upstream subscription behavior and market-provider integration have not been verified here.

## Backward compatibility and future extension

The configuration below is the intended NIFTY-only switch setting:

```dotenv
STRATEGY_NIFTY_ENABLED=true
STRATEGY_BANKNIFTY_ENABLED=false
STRATEGY_SENSEX_ENABLED=false
```

It retains the default NIFTY contract loader and its current shared strategy flow. Since the enable flag does not gate every subsystem, disabling NIFTY should not be interpreted as proving that all NIFTY strategy work has been disabled.

The context and configuration layers provide a starting point for extension: add or configure an index key/display name and instantiate the same context shape. A complete new index still requires wiring its option response into an index-keyed contract cache; routing candle/EMA/OR/touch work by that association; making selection, event, alert, and frontend state per underlying; and testing that disabled contexts produce no processing or subscriptions. Configuration alone is not sufficient in the current implementation.
