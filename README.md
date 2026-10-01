# Option Feed Engine

FastAPI application that loads Upstox option contracts, retrieves historical and intraday candles, calculates Opening Range (OR) levels and EMA values, identifies eligible option instruments, and streams market data and strategy events to APIs, WebSocket clients, dashboards, Telegram, and an optional Algo App integration.

The `multi-index-v1` runtime scopes the strategy work by enabled underlying index. Its central rule is that candle history drives EMA and pre-selection touch discovery, while upstream live streaming is limited to enabled index feeds plus selected option instruments.

## Project overview

The engine operates over configured underlying indexes (initially NIFTY, BANKNIFTY, and SENSEX) and their option chains. For every enabled underlying it builds an isolated `StrategyContext` containing that index's option contracts, OR result, EMA state and events, touch state and events, candidates, selected instrument, alert state, and runtime metadata. Common strategy services are reused across contexts.

Completed one-minute candles are used to update EMA and evaluate pre-selection OR touches. A touch candidate is evaluated against the corresponding underlying's OR reference and option configuration. A successful daily selection gets its own live option subscription, with market ticks used for real-time price/chart updates and downstream live events.

## Architecture overview

```text
Configuration (core/config.py)
             |
             v
      Enabled underlyings
       /       |       \
      v        v        v
   NIFTY   BANKNIFTY   SENSEX
      |        |        |
      v        v        v
 Independent StrategyContext instances
      |        |        |
      +--------+--------+
               |
       per-index option universes
               |
       historical/intraday candles
               |
      +--------+---------+
      |        |         |
      v        v         v
     EMA      OR       Touch/candidates
      |        |         |
      +--------+---------+
               v
       Per-index isolation/selection
               |
               v
   Enabled index feeds + selected options
               |
               v
       Upstox Market Data WebSocket
               |
       +-------+--------+
       |                |
       v                v
  Live broadcaster   Dashboard/charts
```

Mongo strategy state uses one configured collection, with a separate upserted document for each trading date and underlying.

## Previous and current architecture

### Previous model

The earlier strategy runtime centered on NIFTY and global runtime stores: one option cache, one OR cache, one touch queue/duplicate set, and one selected-instrument slot. Live ticks were also available to the OR touch path. This made NIFTY the implicit reference for parts of option filtering and selection and made it difficult to run independent index strategies safely.

```text
NIFTY-oriented global state
  ├── option cache
  ├── OR and EMA
  ├── touch processing (including a live-tick path)
  └── one selected instrument/dashboard state
```

### Current model

```text
Enabled underlying
      |
      v
Independent StrategyContext
  ├── option universe for that underlying
  ├── index and option OR results
  ├── per-instrument EMA state and EMA events
  ├── context-scoped touch state, events, candidates
  ├── selected instrument and isolation state
  └── alert state and runtime metadata
      |
      +--> completed 1-minute candles: EMA and pre-selection touch discovery
      |
      +--> selected instrument: selective live WebSocket feed
```

The shared `options_cache` and `opening_range_cache` still exist as compatibility/raw-data views used by existing application surfaces. For an instrument found in an enabled context, strategy lookup and persistence use that context's option universe, OR state, and selected state.

## Configuration and supported indexes

`core/config.py` builds `STRATEGY_UNDERLYINGS` and `ACTIVE_STRATEGY_UNDERLYINGS`. Configuration resolution is environment variable first, then the application's JSON configuration, then defaults. The JSON file is located using `APP_CONFIG_FILE` or the existing `config/app_config.json` / `config.json` search.

| Environment variable | JSON key | Default | Purpose |
| --- | --- | --- | --- |
| `STRATEGY_NIFTY_ENABLED` | `strategies.nifty.enabled` | `true` | Enables NIFTY strategy context. Its key is `MAIN_NIFTY_SECURITY`. |
| `MAIN_NIFTY_SECURITY` | `market.main_nifty_security` | `NSE_INDEX\|Nifty 50` | NIFTY index instrument key. |
| `STRATEGY_BANKNIFTY_ENABLED` | `strategies.banknifty.enabled` | `false` | Enables BANKNIFTY strategy context. |
| `BANKNIFTY_SECURITY` | `strategies.banknifty.instrument_key` | `NSE_INDEX\|Nifty Bank` | BANKNIFTY index instrument key. |
| `STRATEGY_SENSEX_ENABLED` | `strategies.sensex.enabled` | `false` | Enables SENSEX strategy context. |
| `SENSEX_SECURITY` | `strategies.sensex.instrument_key` | `BSE_INDEX\|SENSEX` | SENSEX index instrument key. |
| `STRATEGY_NIFTY_STRIKE_FROM`, `STRATEGY_NIFTY_STRIKE_TO` | `strategies.nifty.strike_from`, `strategies.nifty.strike_to` | Global `STRIKE_FROM`, `STRIKE_TO` | Optional NIFTY option strike bounds. |
| `STRATEGY_BANKNIFTY_STRIKE_FROM`, `STRATEGY_BANKNIFTY_STRIKE_TO` | `strategies.banknifty.strike_from`, `strategies.banknifty.strike_to` | Unbounded | Optional BANKNIFTY strike bounds. |
| `STRATEGY_SENSEX_STRIKE_FROM`, `STRATEGY_SENSEX_STRIKE_TO` | `strategies.sensex.strike_from`, `strategies.sensex.strike_to` | Unbounded | Optional SENSEX strike bounds. |
| `STRIKE_FROM`, `STRIKE_TO` | `market.strike_from`, `market.strike_to` | `22500`, `25000` | Shared legacy bounds and NIFTY fallback. `.env.example` overrides these. |
| `STRATEGY_STATE_COLLECTION` | `strategy_state.collection` | `strategy_state` | One Mongo collection for date/index strategy snapshots. |

Example matching the checked-in `.env.example` defaults:

```dotenv
STRATEGY_NIFTY_ENABLED=true
STRATEGY_NIFTY_STRIKE_FROM=23000
STRATEGY_NIFTY_STRIKE_TO=25000
STRATEGY_BANKNIFTY_ENABLED=false
BANKNIFTY_SECURITY=NSE_INDEX|Nifty Bank
STRATEGY_SENSEX_ENABLED=false
SENSEX_SECURITY=BSE_INDEX|SENSEX
STRATEGY_STATE_COLLECTION=strategy_state
```

For NIFTY and SENSEX together, set `STRATEGY_SENSEX_ENABLED=true`. Add an optional pair of SENSEX strike variables only if a strike range is desired. BANKNIFTY and SENSEX do not inherit NIFTY's global strike bounds when their own range is absent.

An enabled index gets a context, option-chain load, candle processing, OR state, and an index base WebSocket key. A disabled index is excluded from active contexts, option-chain loading, EMA active keys, strategy processing, and selected-instrument subscriptions. The service may still have static/master metadata for all three configured indexes.

## Strategy context and index/option mapping

`services/strategy_context.py` creates a context from each configured index. Its runtime state includes:

| Context field | Contents |
| --- | --- |
| `underlying`, `index_instrument_key`, `display_name`, `enabled` | Identity and configured activation. |
| `option_config` | Per-index strike range configuration. |
| `option_universe` | Map from option `instrument_key` to normalized provider contract metadata. |
| `opening_range` | Date/status/source plus OR result map keyed by the index or option instrument key. |
| `ema_state`, `ema_events` | Per-instrument EMA snapshots and bounded crossover event history. |
| `touch_state`, `touch_events` | Date/index-scoped duplicate and touch state, plus bounded event history. |
| `candidates` | Touch candidates keyed by underlying, instrument, and level. |
| `selected_instrument`, `isolation_state` | The context's independent daily selection and lock state. |
| `alert_state` | EMA alert history and finalized-minute duplicate guards. |
| `runtime_metadata` | Option expiry/count, update timestamps, underlying LTP, and related runtime values. |

For example, the NIFTY context owns NIFTY CE/PE contracts and the SENSEX context owns SENSEX CE/PE contracts. The option loader iterates active contexts, calls Upstox using each configured index instrument key, filters by provider `underlying_symbol` where that metadata is present, and stamps each normalized contract with `underlying` and `underlying_instrument_key`. Normalized contracts retain `provider_metadata` for inspection. Per-index lookups use the context or an underlying-aware cache index rather than choosing a same-strike contract from another index.

With `filter_nearest=True` (the default), the loader chooses the nearest non-expired expiry and applies that index's configured strike bounds. If no per-index range exists, BANKNIFTY/SENSEX contracts are not filtered using NIFTY's global range. Explicit `expiry_date` requests bypass nearest-expiry selection. The combined legacy cache records per-underlying expiry and contract summaries while each context remains the strategy universe.

## Candle and EMA processing

`InternalEmaEngine.poll_completed_candles()` runs from the APScheduler job `internal_ema_completed_candle_job`, scheduled on weekdays at second 10 of each minute. It requests `1minute` intraday candles for index and option instrument keys in active contexts. A candle is eligible only when its parsed timestamp, truncated to a minute, is earlier than the current minute cutoff. The poller skips an already-polled minute, avoids overlapping cycles, and the per-instrument state rejects timestamps not later than `last_processed_timestamp`.

Historical candle processing warms EMA state before the poller begins. The engine maintains state by instrument key and mirrors each instrument state into its owning context. Incremental EMA is updated from each completed candle close; the current implementation stores `ema_9` and `ema_21`, derives trend and crossover from the prior/current fast-slow difference, and emits a crossover event carrying candle timestamp, instrument key, contract details, and underlying identity when the context is known. Events are broadcast to the EMA WebSocket and saved through the existing intraday EMA history path. The engine's incremental calculation uses fixed 9/21 fields; see [Known limitations](#known-limitations) regarding the separate configurable EMA period settings.

```text
Historical candle warmup
        |
        v
Enabled context instrument keys
        |
        v
Completed 1-minute candle (timestamp < current minute)
        +-------------------------------+
        |                               |
        v                               v
Incremental EMA 9/21              Completed-candle OR touch
        |                               |
        v                               v
EMA crossover event               Candidate/selection evaluation
```

The OR touch call is made only when `process_candle` returns a newly updated EMA state. This means duplicate, not-yet-initialized, or otherwise unprocessed EMA candles do not run this touch path.

## Opening Range and touch rules

The OR service calculates OR data for the subscribed index and option keys using the configured OR candle interval and opening-candle count (`OPENING_RANGE_INTERVAL`, default `1minute`; `OPENING_RANGE_CANDLE_COUNT`, default `1`). The per-index `opening_range` context stores the OR result for the index instrument and that context's option instruments, including calculated range/level data, status, date, interval, source, and calculation time. The shared OR cache is updated for existing consumers as a compatibility snapshot.

Pre-selection discovery is candle-based. `process_completed_candle_for_opening_range()` reads the instrument's OR levels from its owning context and uses the completed candle. The directional business rules are:

```text
R2 / R3: candle high >= level
S2 / S3: candle low  <= level
```

If the selected high/low is not positive, the existing fallback uses candle close and labels the trigger field `close`. Touches are limited to levels in `OPENING_RANGE_ISOLATION_TOUCH_LEVELS` (default `R3`) and the configured option-only rule. Duplicate alert keys include trading date, underlying, instrument key, and level; the default once-per-level rule is enabled. Context touch histories and event arrays are bounded. Touch payloads include `underlying`, `underlying_instrument_key`, `instrument_key`, level/value, trigger price/field, timestamp, source, normalized contract metadata, and candle data.

The prior live-tick OR-touch helper remains callable for compatibility but now records live price state only and returns no touch candidates. A touch or selection decision is not made from a live tick.

## Isolation and selected instruments

Touch events are grouped by underlying before selection. Each group is evaluated using that context's OR average (or its own index LTP fallback), strike configuration, existing touch eligibility, configured touch-level priority, and average-window rules. The selected option and the daily lock are written to that same context. The early-opening EMA selector also maintains one selection window per underlying and filters candidates against that context's option universe and reference value. The date/index key is also the unit of persistence. Thus an existing NIFTY lock does not block a SENSEX selection. Existing replacement behavior remains a daily lock: a context with an already selected instrument does not replace it during that trading day.

```text
Completed candle
      |
      v
Per-instrument OR touch and candidate
      |
      v
Group by underlying -> eligibility/priority/window checks
      |
      v
Context selected_instrument (daily locked)
      |
      v
Subscribe selected option -> live chart/dashboard feed
```

When the context's selected instrument is unavailable, no option is added from that context. The running WebSocket synchronizer computes a desired-key set and applies added/removed key differences; processing one context does not remove another context's still-desired key.

## Selective live WebSocket subscriptions

`get_live_websocket_instrument_keys()` returns the index instrument key for every active context and that context's selected option, if one exists. It does not return every option contract. The option universe remains available to candle polling and strategy calculations without putting all those options on the live streaming path.

The Upstox streamer periodically compares desired keys with its active set and calls subscribe for added keys and unsubscribe for removed keys. On reconnection it rebuilds its subscription set from current active contexts, so current selections are included. Live ticks update instrument LTP state and are broadcast through the existing broadcaster to general, instrument, chart, and related WebSocket clients. They can update a selected chart and live price but do not perform OR level discovery. EMA itself remains candle-based.

## Events, alerts, and dashboard

EMA crossover and OR touch events carry instrument identity and, for context-owned instruments, `underlying` plus `underlying_instrument_key`. Touch events are appended to the owning context; the bounded shared touch list and legacy pending alert queue are retained for existing interfaces. EMA finalized-minute suppression is scoped to the context's alert state. Accepted EMA alert records (including configured Telegram/Algo App delivery results) are attached to their instrument's underlying context. Existing Telegram commands and delivery integrations are retained. NIFTY's existing option-chain alert suggestion path remains in place; for other contexts, nearest CE/PE suggestions are selected from that context's loaded option universe and its own underlying spot/reference. Those non-NIFTY master-data suggestions do not include a live-priced budget list.

The existing isolated EMA dashboard now includes an **Enabled Index Strategy Contexts** view. It fetches `/api/strategies` and displays one card per active underlying with option count, OR average/status, EMA instrument count, and that context's selected instrument. A selected option exposes an `/chart/{instrument_key}` link. Existing dashboard tables and compatibility views remain available; they have not all been rewritten as per-index table filters.

Important REST routes include:

| Route | Purpose |
| --- | --- |
| `GET /api/strategies` | List enabled strategy contexts and compact runtime state for each. |
| `GET /api/strategies/{underlying}` | Return one enabled context snapshot, including its option universe. |
| `GET /opening-range/status?underlying=...` | Existing aggregate OR status; the selected-instrument field can be scoped to the requested underlying. |
| `GET /opening-range/dashboard` | Existing aggregate dashboard summary. |
| `GET /opening-range/cache` | Existing aggregate compatibility OR cache. |
| `GET /opening-range/selected-instrument` | Selected state; accepts optional `underlying`. |
| `GET /opening-range/selected-instrument/ema-alerts?underlying=...` | EMA alert history for a context when an underlying is specified. |
| `GET /opening-range/isolated-instrument` | Existing selected-state view; accepts optional `underlying`. |
| `GET /opening-range/isolated-instrument/ema-alerts?underlying=...` | Selected instrument EMA alerts for the named context. |
| `GET /opening-range/touch-events` | Existing touch event list. |
| `POST /opening-range/fetch` | Manually recalculate OR over subscribed instruments. |
| `POST /opening-range/isolated-instrument/manual?strike=...&striketype=...&underlying=...` | Manually select a loaded option within the requested context. `underlying` is optional for compatibility (legacy lookup remains NIFTY-first). |
| `GET /chart/{instrument_key}` | Render an instrument chart page. |

WebSocket endpoints include `/ws`, `/all-feeds`, `/option`, `/ws/ema`, `/ws/ema-crossover`, `/ws/ema-crossover/instrument`, `/ws/opening-range`, and `/ws/opening-range/instrument`. Instrument-specific routes use instrument keys/option filters as implemented in `ws_feed/websocket_routes.py`. The broadcaster forwards event payload fields, including underlying identity when supplied.

## MongoDB persistence

`services/strategy_state_persistence.py` stores snapshots in the one collection configured by `STRATEGY_STATE_COLLECTION` (default `strategy_state`), using the project's `MONGO_URL`/`MONGO_DB` settings. The repository creates a unique compound index on `(trading_date, underlying)` and upserts by that same identity. `trading_date` is the market date in `MARKET_TIMEZONE` (default `Asia/Kolkata`) formatted as `YYYY-MM-DD`.

Each snapshot includes identity and index key, strategy configuration, option universe, OR, EMA states/events, touch state/events, candidates, selected instrument, isolation state, alerts, and metadata. Writes happen on meaningful context updates and once after an EMA candle batch, not once per incoming WebSocket tick. The repository uses bounded context event histories. Mongo loading/writing is fail-open for the live runtime: when Mongo settings or connectivity are missing, processing can continue and repository warnings/errors are logged.

Example document identity and content shape:

```json
{
  "trading_date": "2026-10-02",
  "underlying": "SENSEX",
  "underlying_instrument_key": "BSE_INDEX|SENSEX",
  "strategy_config": { "enabled": true, "display_name": "SENSEX", "option": {} },
  "opening_range": { "status": "success", "instruments": {} },
  "ema": { "state": {}, "events": [] },
  "touch_state": {},
  "touch_events": [],
  "candidates": {},
  "selected_instrument": {},
  "isolation": {},
  "alerts": {},
  "metadata": { "updated_at": "...", "version": 1 }
}
```

NIFTY, BANKNIFTY, and SENSEX are separate documents in that same collection for a given date. Earlier dates are not overwritten by later trading days.

## Startup, market-day flow, and shutdown

### Startup

1. FastAPI startup initializes runtime configuration and the existing token/Mongo lifecycle.
2. `load_options_for_enabled_underlyings()` loads each active index chain and builds context option universes, then merges a compatibility cache for existing application/history consumers.
3. Startup historical candle processing warms EMA values and mirrors instrument state into contexts.
4. Startup OR catch-up calculates OR state and scans eligible backfill candles using the configured existing backfill rules.
5. The application starts the scheduler and internal EMA poller integration, then starts the Upstox streamer. The live key set contains active index feeds plus selected instruments.

### Market day

Market timings come from `MARKET_OPEN_HOUR`, `MARKET_OPEN_MINUTE`, `MARKET_CLOSE_HOUR`, and `MARKET_CLOSE_MINUTE`; defaults are 09:15–15:30 in `MARKET_TIMEZONE` (default `Asia/Kolkata`). EMA polling is restricted to weekdays and this configured market window, through the minute after the configured close. Completed candle state is processed, touch candidates are evaluated against context-owned OR levels, and daily selections remain locked by underlying. Selected live updates continue to flow to the dashboard/chart. Existing scheduled refresh, token, archive, and cleanup jobs continue through APScheduler.

On a new market date, `StrategyContext.ensure_trading_date()` clears intraday state before restoring only the matching date/underlying Mongo document. On application shutdown, the FastAPI lifespan stops the streamer, scheduler, token bot/runtime services, and closes configured runtime resources according to the existing `main.py` shutdown sequence. Mongo strategy-state writes are snapshots/upserts; no per-tick strategy records are created.

## Multi-index examples and backward compatibility

NIFTY-only mode remains the defaults represented in `.env.example`:

```dotenv
STRATEGY_NIFTY_ENABLED=true
STRATEGY_BANKNIFTY_ENABLED=false
STRATEGY_SENSEX_ENABLED=false
```

Two-index example:

```dotenv
STRATEGY_NIFTY_ENABLED=true
STRATEGY_BANKNIFTY_ENABLED=false
STRATEGY_SENSEX_ENABLED=true
```

In the second example NIFTY and SENSEX get independent candles, EMA states, OR results, option universes, touch/selection state, alert histories, and selected live feeds. BANKNIFTY is omitted from active strategy processing. NIFTY retains its default instrument key and global strike-range fallback, which preserves the NIFTY-only contract-loading behavior while touch discovery uses completed candles.

## Component and file reference

| Path | Responsibility |
| --- | --- |
| `main.py` | FastAPI application/lifespan, startup orchestration, scheduled jobs, and route registration. |
| `core/config.py` | Environment/JSON settings, enabled underlying map, instrument keys, and per-index option ranges. |
| `services/strategy_context.py` | Per-underlying runtime state, IST trading date reset/restore, active keys, and context snapshots. |
| `services/strategy_state_persistence.py` | One Mongo collection, unique date/underlying index, load, and upsert repository. |
| `services/option_service.py` | Upstox option master loading, provider underlying filtering, per-index option universes, cache compatibility indexes, and selected live-key generation. |
| `services/history_service.py` | Historical/intraday candle fetching and historical EMA warmup integration. |
| `services/ema_engine.py` | Completed one-minute candle polling, duplicate protection, incremental EMA updates, context association, crossover event and broadcast. |
| `services/opening_range/service.py` | Opening Range calculation orchestration and context OR synchronization. |
| `services/opening_range/live_touch.py` | Completed-candle touch semantics, event creation, per-context touch state, and live-price-only tick handler. |
| `services/opening_range/isolation.py` | Context-specific reference average/window, event eligibility, priority, and daily selection. |
| `services/opening_range/state.py` | Legacy compatibility cache/queues plus context-aware state access and date/index helper behavior. |
| `services/opening_range/ema_alerts.py` | Selected-context EMA alert construction and existing alert delivery workflow. |
| `services/upstox_websocket.py` | Upstream stream lifecycle and dynamic subscribe/unsubscribe synchronization. |
| `ws_feed/broadcaster.py` | Downstream live tick, EMA crossover, and OR event delivery to WebSocket clients. |
| `ws_feed/websocket_routes.py` | Browser/client WebSocket routes. |
| `api/strategy_routes.py` | Multi-index strategy context list and single-underlying snapshot endpoints. |
| `api/opening_range_routes.py` | Existing OR, touch, selected instrument, alert, and manual fetch APIs. |
| `api/chart_routes.py` | Chart page and chart instrument/data routes. |
| `templates/isolated_ema_dashboard.html` | Existing OR/EMA dashboard plus enabled strategy context cards. |
| `tests/test_strategy_context.py` | Context ownership, disabled context option behavior, selection isolation, and directional candle touch checks. |
| `tests/test_strategy_state_persistence.py` | Same-collection date/underlying upsert and unique compound identity checks. |

## End-to-end data flow

```text
Upstox option master                    Upstox candle history
        |                                         |
        v                                         v
Validate/filter by underlying         1-minute history/warmup
        |                                         |
        v                                         v
Context.option_universe  <---- active instrument keys ----+
        |                                         |
        +---------------------+-------------------+
                              v
                   Completed-candle poll
                    /        |         \
                   v         v          v
                 EMA        OR       Directional touch
                   \         |          /
                    \        |         /
                     v       v        v
                  Context state/events
                              |
                    eligibility + isolation
                              |
                              v
                 Context selected instrument
                              |
                              v
             selective upstream live subscription
                              |
                              v
           tick broadcaster -> chart/dashboard clients

Context changes and candle batches -> date/index Mongo upsert
```

## Rationale and extensibility

The context-based model supports more than one underlying without copying the strategy implementation, prevents one index's lock/OR/touch/alert state from replacing another's, and uses closed candles for stable strategy discovery. Selective upstream subscriptions keep the live tick path focused on configured index feeds and selected instruments while preserving live chart/dashboard updates. No numerical performance improvement is claimed here; the repository does not contain measurements for one.

The initial supported set is explicitly configured in `STRATEGY_UNDERLYINGS`. Enabling BANKNIFTY or SENSEX and setting its instrument key is configuration-driven. Adding an entirely new index currently requires a code/config addition to that supported-index map, a provider-recognized instrument key and option metadata, plus validation that existing feed/chart/order assumptions accept its market. The strategy services can then consume a context without creating a duplicate per-index strategy implementation. It is not currently a zero-code-change plugin system.

## Testing and verification

Run the unit tests from the repository root in an environment with `requirements.txt` installed:

```powershell
python -m unittest discover -s tests
```

The focused context tests cover option ownership, disabled context behavior, independent selection values, and directional candle touch conditions. The persistence tests use an in-memory collection double to assert the unique `(trading_date, underlying)` identity and that updates affect only the matching date/index document.

In the development environment used for this change, Python AST parsing and `git diff --check` were run. The normal focused unittest command could not import the project because the runtime is missing project dependencies (`python-dotenv`, `upstox_client`, and `pymongo`). To exercise the changed strategy/persistence logic, all 5 context tests and all 3 persistence tests were also run with import stubs for those absent external modules; those 8 tests passed. This does not verify provider, Mongo, or full application integration. Install `requirements.txt` and run the normal command for an environment-level verification.

## Known limitations

- The shared option and OR caches and legacy global state accessors remain for compatibility. New per-context paths are used for known enabled-context instruments, but older aggregate dashboard tables are not yet all converted to per-underlying filters.
- Automated coverage currently focuses on context ownership, directional touch behavior, and date/index persistence identity. It does not yet exercise multi-index startup against Upstox, live subscribe/unsubscribe against the provider, the browser dashboard, or Telegram/Algo App delivery end to end.
- The internal incremental EMA engine currently uses `ema_9` and `ema_21` fields/formulas. The repository also defines `EMA_FAST_PERIOD`/`EMA_SLOW_PERIOD` and `LIVE_EMA_FAST_PERIOD`/`LIVE_EMA_SLOW_PERIOD`; changing these settings does not currently change the engine's hard-coded incremental 9/21 recurrence.
- Touch discovery within the candle poll is coupled to an initialized EMA state because the OR touch function is called after a newly processed EMA candle. Uninitialized or duplicate EMA candles do not independently run touch processing.
- Provider contract validation checks `underlying_symbol` when available. If the provider omits it, the returned contracts are accepted from the request's underlying and stamped with that context identity.
- For non-NIFTY selected-instrument EMA alerts, nearest option suggestions come from the context contract master; the current option master does not supply the live option prices needed to reproduce the existing NIFTY budget-price suggestion list.
- MongoDB persistence is fail-open and depends on valid `MONGO_URL`, `MONGO_DB`, connectivity, and the `pymongo` dependency. It is not required for the process to continue strategy calculations.
- The application was not fully runtime-tested in this environment because the installed Python runtime lacks `python-dotenv`; live Upstox, Mongo, Telegram, and browser integration behavior was not exercised here.
