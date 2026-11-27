# Instrument Isolation Service

FastAPI service for index-level isolation detection, option-contract selection, Telegram notifications, and isolated-instrument Supertrend data collection.

## Scope

The application:
1. Refreshes Upstox access tokens, market data, and opening-range levels.
2. Monitors configured indexes for SR/OR level touches.
3. Selects the isolated NIFTY option contract and its opposite CE/PE contract.
4. Persists the isolation record and local isolation archive.
5. Sends the detailed isolation announcement to configured Telegram private and group chats. Chart URLs are built dynamically from `CHART_BASE_URL` and the actual instrument keys.
6. Starts independent Supertrend WebSocket feeds for the isolated and opposite instruments using `SUPERTREND_INTERVALS` (default: `1m,15m`). Snapshots and subsequent frames are appended to local JSONL archives for analysis.

There is no broker order placement, GTT execution, position management, AlgoAI/AlgoCLI integration, SR WebSocket capture, or SR indicator notification pipeline.

## Supertrend archives

Default settings:

```dotenv
EMA_WS_BASE_URL=wss://ema.novag7.in
ISOLATED_SUPERTREND_ENABLED=true
SUPERTREND_INTERVALS=1m,15m
ISOLATED_DATA_DIR=data/isolated
REQUIRE_TODAY_ONLY_SCOPE=true
```

Each isolated and opposite leg receives one feed per configured interval. Files are stored under `data/isolated/supertrend/` with the trade date, role, and instrument key in the filename. The complete upstream frame (including initial snapshots) is retained as JSONL for later analysis.

The 1-minute endpoint is `/ws/supertrend/{instrument_key}`; other intervals use `/ws/supertrend/interval/{interval}/{instrument_key}`. Instrument keys are URL-encoded.

## Sandbox Supertrend signal plans

After an isolation is created, the isolated and opposite instruments' interval-specific
Supertrend WebSocket snapshots are checked independently for `1m` and `15m`.
A signal is eligible when its confirmation timestamp falls in the same IST minute
as isolation or any later minute. Seconds are ignored for this eligibility check, so
a signal at 09:20:15 is accepted when isolation happens at 09:20:40.
Signals from earlier minutes are ignored. The earliest confirmed eligible signal wins;
if both legs have the same confirmation time, the earlier WebSocket arrival wins.
Once selected, that signal is locked for the interval even if the live entry quote
is temporarily unavailable. Later signals cannot replace it.
A `buy` selects the instrument that emitted the signal; a `sell` selects the opposite leg.
Only one plan is saved per isolation and interval. Entry always uses the selected
instrument's live Upstox LTP, never the Supertrend confirmation price.

The selected target instrument's live Upstox market-feed LTP is the entry. The stop
is the lower numeric value of the 30% entry stop and the nearest valid S1/S2/S3 support
below entry; if no valid support is available, the 30% stop is used. Target offsets are
+40 for entry <= 100, +50 for 100 < entry <= 200, and +60 for entry > 200.

Plans are saved under `data/isolated/sandbox_services/{YYYYMMDD}/`, one JSON file
per isolation and interval. Notifications are sent to the enabled isolation Telegram
group/private destinations. `SANDBOX_SUPERTREND_SIGNALS_ENABLED=true` enables this
workflow. The saved payload records the signal source, target-leg decision, live entry,
support candidates, stop/target formulas, and explicitly marks `order_placed=false`.
This is analysis/notification only; it does not place orders or manage positions.

## Daily time-window Supertrend signal scan

A separate scheduler task scans the active isolated and opposite option instruments
during the configured afternoon window. It does not replace the WebSocket-based
sandbox signal workflow.

Defaults are weekdays, 15:00–15:40 IST. The first run is scheduled at 15:00:10;
subsequent runs execute at ten seconds past each minute. Each cycle refreshes
today's intraday candles and reuses date/instrument/interval-keyed historical
batches fetched on the first run. Historical requests are split into three-day
batches and transient failures are retried on later cycles. Only fully completed
candles are evaluated, with a configurable publication grace.

The default intervals are 1m, 3m, 5m, and 15m. 30m, 45m, and 60m can be enabled
through configuration. Each isolated/opposite leg and interval is evaluated
independently. Every trend flip whose candle closes inside the 15:00–15:40 window
is persisted; deterministic signal IDs prevent duplicate records and alerts when
the same candles are reprocessed. One instrument/interval failure does not stop
other scans. Failed Telegram deliveries remain pending for bounded retries, including a five-minute
notification-only retry period after the 15:40 signal window closes.

Daily files are stored under `data/time_window_supertrend/{YYYYMMDD}/`:
- `history/` — historical API batches cached for the day.
- `intraday/` — latest intraday candle snapshots by leg and interval.
- `signals/{interval}/` — one atomic JSON file per unique signal.
- `signals.json` — daily signal index and notification delivery state.
- `state.json` — latest cycle status and errors.

Notifications use the existing `TELEGRAM_GROUP_ENABLED` /
`TELEGRAM_GROUP_CHAT_ID` and `TELEGRAM_ENABLED` / `TELEGRAM_CHAT_ID`
destinations. Group messages contain a concise signal summary; private messages
include the instrument key, candle close, signal ID, and Supertrend parameters.
No orders are placed.

Configuration (all values are read at process startup):

```dotenv
TIME_WINDOW_SUPERTREND_ENABLED=true
TIME_WINDOW_SUPERTREND_START_HOUR=15
TIME_WINDOW_SUPERTREND_START_MINUTE=0
TIME_WINDOW_SUPERTREND_END_HOUR=15
TIME_WINDOW_SUPERTREND_END_MINUTE=40
TIME_WINDOW_SUPERTREND_SCAN_SECOND=10
TIME_WINDOW_SUPERTREND_CANDLE_GRACE_SECONDS=5
TIME_WINDOW_SUPERTREND_INTERVALS=1m,3m,5m,15m
TIME_WINDOW_SUPERTREND_ATR_PERIOD=10
TIME_WINDOW_SUPERTREND_MULTIPLIER=3.0
TIME_WINDOW_SUPERTREND_USE_ATR=true
TIME_WINDOW_SUPERTREND_HISTORY_DAYS=12
TIME_WINDOW_SUPERTREND_HISTORY_BATCH_DAYS=3
TIME_WINDOW_SUPERTREND_MAX_NOTIFICATION_ATTEMPTS=5
TIME_WINDOW_SUPERTREND_NOTIFICATION_RETRY_MINUTES=5
TIME_WINDOW_SUPERTREND_DATA_DIR=data/time_window_supertrend
```

The status is exposed by `GET /status` under `time_window_supertrend`.

## Index isolation thresholds

Index isolation uses the thresholds calculated from the index's own opening-range
pivot levels in MongoDB (`project_info.pivot_levels`). An R3-side isolation is
triggered when index LTP is greater than or equal to `R3_Threshold`; an S3-side
isolation is triggered when index LTP is less than or equal to `S3_Threshold`.
Both thresholds are the midpoint between the corresponding second and third
levels. Raw R3/S3 are retained as reference levels, not trigger boundaries.
If a same-day MongoDB pivot document is missing either threshold, the OR-level
service attempts to repair the index levels before scanning; the detector does
not fall back to raw R3/S3 when a threshold is absent.

## Isolated option pivot levels

The active isolated option and its exact opposite CE/PE contract are processed independently.
The service calculates levels from completed Upstox intraday candles for **1-minute and
15-minute** intervals only. It does not scan or subscribe to the full option chain and
does not restore order placement, GTT, positions, or execution workflows.

Configuration:

```dotenv
ISOLATED_PIVOT_ENABLED=true
ISOLATED_PIVOT_INTERVALS=1,15
ISOLATED_PIVOT_SCHEDULER_INTERVAL_SECONDS=30
ISOLATED_PIVOT_CANDLE_CLOSE_GRACE_SECONDS=5
ISOLATED_PIVOT_FETCH_REQUESTS_PER_SECOND=4
ISOLATED_PIVOT_FETCH_MAX_RETRIES=2
ISOLATED_PIVOT_FETCH_RETRY_BACKOFF_SECONDS=1
```

Files are written atomically under
`data/isolated/pivots/{YYYYMMDD}/{isolated|opposite}/{instrument_key}/`:

- `pivot_levels_1m.json` — latest completed 1-minute candle's levels.
- `pivot_levels_15m.json` — latest completed 15-minute candle's levels.
- `pivot_levels.json` — preserved opening 09:15 one-minute candle levels, when available (legacy-compatible name).

The level document includes the instrument/leg metadata, interval, source candle,
source candle timestamps, calculation inputs, and R3/R2/R1/AVG/S1/S2/S3 levels.
It also stores `R3_Threshold = (R2 + R3) / 2` and
`S3_Threshold = (S2 + S3) / 2` in both `levels` and a dedicated `thresholds` object.
These values are calculated independently for each instrument and each interval;
they do not drive index isolation detection.
Candles are aligned to the 09:15 IST session open; a configurable publication grace
is applied and incomplete candles are never used. A polling worker runs only for the
current isolation pair during the weekday 09:15–15:30 IST session. The worker resumes
for today's persisted isolation after application restart. Status is exposed in
`GET /status` under `isolated_pivots`.

This calculation uses the same existing range-level formula as the project's OR
calculator, applied independently to each completed option candle; it is not a
traditional previous-day floor-pivot formula.

## Telegram

Configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_GROUP_CHAT_ID`, and optionally `TELEGRAM_CHAT_ID`. Group announcements are controlled by `TELEGRAM_GROUP_ENABLED`; private-chat announcements are controlled by `TELEGRAM_ENABLED`.

Set `CHART_BASE_URL` to the current chart application base URL. Isolation notifications include links for the isolated option, opposite option, trigger index, NIFTY, BANKNIFTY, and SENSEX.

## Daily backup and cleanup

The existing daily MEGA backup workflow is retained. It creates and verifies a date-keyed ZIP from `DATA_DIR`, uploads it to the configured MEGA folder, persists a durable backup receipt, and only then performs the configured cleanup of the contents of `DATA_DIR`. If upload verification, archive completeness, or durable receipt persistence fails, cleanup is blocked and local data is retained.

Keep `DAILY_DATA_BACKUP_DIR` outside `DATA_DIR`. The startup cleanup service is also retained.

## Run

Install dependencies and configure the environment:

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8001
```

## API

- `GET /health` — service and isolated Supertrend feed status.
- `GET /status` — scheduler status.
- `GET /startup-state` — startup gate diagnostics.
- `POST /trigger/refresh` — manual daily refresh.
- `GET /api/v1/isolation/current` — current isolation record.
- `GET /api/v1/isolation/history` — recent isolation history.
- `POST /api/v1/isolation/trigger` — manual isolation scan.
- `POST /api/v1/isolation/backfill` — manual isolation backfill.
- `/api/v1/search/instrument` — instrument search endpoints.
- `/api/v1/logs` — log inspection endpoints.

Existing MongoDB isolation records, local isolation archives, and completed backups are not removed by the code cleanup.
