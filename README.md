# Ordered Instrument Jobs FastAPI

A FastAPI application that keeps a rolling local cache of 1-minute candles
for every subscribed instrument, derives 9/21 EMA crossings from them,
and detects new crosses live during market hours.

Five chained jobs run on startup and on demand:

1. **Subscriptions** — fetch the upstream `active` list, de-duplicate by
   `instrument_key`, preserve upstream order.
2. **Historical candles** — ensure the last `HISTORY_LOOKBACK_DAYS` (default
   **10**) of 1-minute candles are cached locally. Missing windows are pulled
   from Upstox `HistoryV3Api` in 7-day chunks.
3. **Intraday candles** — during the market window (`MARKET_OPEN_TIME` to
   `MARKET_CLOSE_TIME` in `MARKET_TIMEZONE`), fetch today's 1-minute candles
   and merge them into the same file.
4. **Batch EMA crosses** — compute 9/21 EMA crossings from each candle file
   and rewrite `ema_crosses.json`.
5. **Live EMA crosses** — while inside the market window, tick every minute
   at `minute boundary + LIVE_EMA_TICK_OFFSET_SECONDS`, fetch fresh intraday
   candles, and append only *new* crosses to `ema_crosses.json`.

An optional **bootstrap job (Job 0)** runs before Job 1 and can seed `data/`
from a MongoDB/MEGA backup produced by the separate `candle-backup` project.

## File layout

```
data/<underlying>/<strike>_<instrument_type>/historical.json
data/<underlying>/<strike>_<instrument_type>/ema_crosses.json
data/index/<underlying>/historical.json
data/index/<underlying>/ema_crosses.json
```

Examples:

```
data/nifty/25000_CE/historical.json
data/nifty/25000_CE/ema_crosses.json
data/sensex/70100_PE/historical.json
data/index/nifty/historical.json            # index rows
data/nifty/spot/historical.json             # non-option non-index rows
```

`historical.json` is the single source of truth for candles.
`ema_crosses.json` is derived from it (batch job) and appended to live.

## Project behavior

- **Application start / restart**: runs Job 0 (bootstrap, optional), then
  jobs 1–4 in order, then starts job 5 as a background task.
- **Normal browser refresh**: reads the current in-memory ordered list.
- **Hard refresh** (`POST /api/hard-refresh`): runs jobs 1–4; job 5 keeps
  running in the background.
- **Manual jobs**: `/api/historical-refresh`, `/api/intraday-refresh`,
  `/api/ema-refresh`, `/api/live-ema/tick`.
- **Failed refresh**: previously loaded good data remains available.
- **Concurrency**: bounded by `HISTORY_MAX_CONCURRENCY` for every job.
- **Configuration**: `app/core/config.py` loads `.env` via `python-dotenv`.
- **Logging**: console + rotating file (`LOG_FILE`).
- **Timezone**: all market-window logic uses `MARKET_TIMEZONE`
  (default `Asia/Kolkata`).
- **Startup gate**: while startup is running, every non-whitelisted route
  returns HTTP 503 with a `"Project still loading"` payload. See
  [Startup gate](#startup-gate) below.

## EMA cross semantics

- Standard SMA-seeded EMA, `k = 2 / (period + 1)`.
- Fast/slow periods from `EMA_FAST_PERIOD` (default 9) and `EMA_SLOW_PERIOD`
  (default 21).
- **Bullish cross**: `fast − slow` goes from `<= 0` to `> 0`.
- **Bearish cross**: `fast − slow` goes from `>= 0` to `< 0`.
- Live-detected events are tagged with `"detected_by": "live"` (REST) or
  `"detected_by": "live_ltp"` (LTP feed); batch events have no such tag.

## Bootstrap from MongoDB / MEGA backup

On startup, before jobs 1–4, the app can seed `data/` from a MongoDB
document written by the `candle-backup` project. This avoids a fresh
historical fetch when a recent backup exists.

Priority order:

1. **Inline Mongo blob** — if the blob document contains the archive
   bytes (`data` field, BSON `Binary`), they are extracted into `data/`
   directly. Always preferred when present.
2. **MEGA file link** — if the blob is absent but the document carries a
   `file_link` and its `updated_at` / `updated_at_utc` is **today** in
   `MARKET_TIMEZONE`, the archive is downloaded and extracted.
3. **Fall through** — if neither path yields data, Job 2 (historical
   candle fetch) runs normally against Upstox.

Notes:

- Extraction is **incremental** by default: files already on disk are
  left alone, so a restart never clobbers fresher data. Set
  `BOOTSTRAP_OVERWRITE=true` to replace existing files.
- Standard MEGA `#!` links are not directly downloadable via HTTP — the
  bootstrap tries a direct `requests` GET first, then falls back to the
  vendored `mega_local` client if it's importable. Copy `mega_local/`
  from the `candle-backup` project into this project root to enable the
  fallback. Without it, the MEGA path is skipped and the app fetches
  normally.
- The bootstrap never raises. Any failure is logged and control falls
  through to the standard fetch.

## Live EMA test mode

Set `EMA_TEST_MODE=true` to run the live EMA job outside the market window.

While test mode is on:

- The market-hours gate is bypassed — the job ticks **every minute, 24×7**,
  using the same `LIVE_EMA_TICK_OFFSET_SECONDS` offset.
- Every tick emits a per-instrument outcome line, e.g.
  ```
  Live EMA tick check; instrument=NSE_FO|44694 result=no-cross
  ```
  even when `INSTRUMENT_ERRORS_ONLY=true`.
- Every tick emits a compact summary:
  ```
  Live EMA cross tick #42 completed; checked=156 new_crosses=0 no_data=2
  insufficient=0 warmup=0 no_cross=152 duplicate=2 failed=0 elapsed=1.83s
  ```

Per-instrument `result=` values:

| Result | Meaning |
|---|---|
| `invalid-key` | Instrument row is missing a usable `instrument_key`. |
| `no-intraday` | Upstream intraday endpoint returned no candles. |
| `insufficient-candles` | Merged history is shorter than the slow EMA needs. |
| `ema-warmup` | EMA series isn't populated at the last two indices yet. |
| `no-cross` | EMAs ready, no bullish/bearish transition this minute. |
| `duplicate-cross` | Cross detected but already recorded for this timestamp. |
| `cross` | New cross written to `ema_crosses.json`. |

Test mode is meant to be temporary; leave it off in production so the job
keeps respecting the market window.

## Startup gate

Until `lifespan` finishes jobs 1–5 and sets `app.state.startup_complete`
to `True`, every non-whitelisted route is answered with **HTTP 503**:

```json
{
  "status": "loading",
  "message": "Project is still loading. Jobs are being initialised (...)",
  "phase": "historical",
  "started_at": "2026-10-13T03:35:22.104321+00:00",
  "retry_after_seconds": 5
}
```

Endpoints that always respond, even during startup:

- `GET /api/health`
- `GET /api/startup-status`
- `GET /docs`, `/redoc`, `/openapi.json`
- Anything under `/static/*`

This is a safety net for the case where the app sits behind a reverse
proxy, or a load balancer sends health checks while the process is
booting. With a plain `uvicorn` server the socket isn't bound until the
lifespan yields, so under normal operation clients never see the 503.

## Telegram notifications

When `TELEGRAM_ENABLED=true` and both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are set, the app sends formatted startup and shutdown
messages via the Telegram Bot HTTP API. No Telegram package is required —
only `requests`, already a project dependency.

The startup message includes the instrument count, live-EMA state, WS
recorder state, market window, daily-refresh time, and any bootstrap
result. The shutdown message includes uptime, live tick and cross counts,
and any recorder statistics.

Diagnostic endpoints:

```bash
curl http://127.0.0.1:8002/api/telegram/status
curl -X POST http://127.0.0.1:8002/api/telegram/test
```

## WebSocket recorder (optional)

When `WS_RECORDER_ENABLED=true`, a background service opens one WebSocket
connection to our own live-EMA endpoint for `NSE_INDEX|Nifty 50`, waits
for the first tick to capture the Nifty LTP, then opens a second
connection to a randomly chosen instrument whose `strike_price` is
nearest to that LTP. Every received frame is appended as one JSON line to:

```
<WS_RECORDER_DIR>/<safe_instrument_key>/<YYYYMMDD_HHMMSS>.jsonl
```

A fresh file is created for every session and every reconnect. The
recorder is a WS *client* of our own server, so it only sees live data
when `USE_LIVE_LTP_FEED=true` and `LIVE_EMA_ENABLED=true`.

## WebSocket samples API

A read-only catalog of the live-EMA WebSocket message shapes. Sample
payloads match the field names and value types the server emits on real
subscriptions.

```text
GET /api/ws-samples                     Full catalog
GET /api/ws-samples/categories          Lightweight index
GET /api/ws-samples/main-index          Index samples (Nifty 50, SENSEX)
GET /api/ws-samples/options             F&O samples (CE / PE / FUT)
GET /api/ws-samples/sample/{id}         A single sample by id
GET /api/ws-samples/{category}          Generic lookup
```

Each sample carries a resolved `connect_url` (and `connect_url_by_parts`
for F&O) built from `PUBLIC_WS_BASE_URL`, `X-Forwarded-*` headers, or the
request itself.

## Live-EMA WebSocket

Our own WebSocket endpoints accept one subscription per socket and emit:

- `snapshot` — sent immediately after connect.
- `tick` — every upstream LTP tick; includes the forming candle.
- `candle_completed` — a bucket closed. Carries the completed OHLC and
  recomputed fast/slow EMA values.
- `ema_cross` — only when a *completed* candle produces a new cross.
  Never fires on a partial candle.
- `error` — human-readable error (e.g. unknown instrument).

```text
WS /ws/live-ema/by-instrument/{url-encoded instrument_key}
WS /ws/live-ema/by-parts?underlying=nifty&strike=25000&type=CE
```

Discovery endpoints:

```bash
curl http://127.0.0.1:8002/api/live-ema/ws-endpoints
curl http://127.0.0.1:8002/api/live-ema/ws-samples
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

On Windows the packaged `run.bat` starts the app on port **8002**:

```cmd
run.bat
```

## Configuration

All settings are read from `.env` via `python-dotenv`.

### Core

| Variable | Default | Purpose |
|---|---|---|
| `SUBSCRIPTIONS_API_URL` | `https://feed.novag7.in/api/subscriptions` | Upstream subscriptions endpoint. |
| `REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout for outbound requests. |
| `LOG_LEVEL` | `INFO` | Root log level. |
| `LOG_FILE` | `logs/app.log` | Rotating log file path. |
| `MODULE_LOGS_ENABLED` | `true` | Write one rotating log file per module. |
| `MODULE_LOG_DIR` | `logs/modules` | Directory for per-module log files. |
| `DATA_DIR` | `data` | Root directory for cached files. |
| `HISTORY_LOOKBACK_DAYS` | `10` | Rolling window of candles to keep. |
| `HISTORY_MAX_CONCURRENCY` | `8` | Max in-flight instruments per job. |
| `MARKET_TIMEZONE` | `Asia/Kolkata` | Timezone used to evaluate the market window. |
| `MARKET_OPEN_TIME` | `09:15` | Intraday window start. |
| `MARKET_CLOSE_TIME` | `15:40` | Intraday window end. |
| `EMA_FAST_PERIOD` | `9` | Fast EMA period. |
| `EMA_SLOW_PERIOD` | `21` | Slow EMA period. |

### Live EMA

| Variable | Default | Purpose |
|---|---|---|
| `LIVE_EMA_ENABLED` | `true` | Start the live job at startup. |
| `LIVE_EMA_TICK_OFFSET_SECONDS` | `10` | Seconds after each minute boundary to fire the tick. |
| `LIVE_EMA_IDLE_SLEEP_SECONDS` | `30` | Sleep outside market hours before re-checking. |
| `LIVE_EMA_INTERVAL_SECONDS` | `60` | Bucket interval for LTP-based live EMA. |
| `EMA_TEST_MODE` | `false` | When `true`, the live EMA job ignores market hours and ticks 24×7. |
| `INSTRUMENT_ERRORS_ONLY` | `false` | Suppress per-instrument progress lines; log errors only. |
| `USE_LIVE_LTP_FEED` | `false` | `true` = LTP WebSocket backend; `false` = REST polling. |

### LTP feed

| Variable | Default | Purpose |
|---|---|---|
| `LIVE_FEED_URL` | `wss://feed.novag7.in/ws/market` | Upstream LTP WebSocket URL. |
| `LIVE_FEED_RECONNECT_SECONDS` | `5` | Reconnect backoff. |
| `LIVE_FEED_PING_INTERVAL_SECONDS` | `20` | Keepalive ping interval. |
| `LIVE_FEED_MAX_CONNECTIONS` | `200` | Cap on concurrent upstream connections. `0` disables the cap. |

### Daily refresh

| Variable | Default | Purpose |
|---|---|---|
| `DAILY_REFRESH_ENABLED` | `true` | Run jobs 1–4 once per market day. |
| `DAILY_REFRESH_TIME` | `08:40` | Time (in `MARKET_TIMEZONE`) to run it. |

### CORS and public URLs

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | `*` | Comma-separated allowed origins, or `*`. |
| `CORS_ALLOW_CREDENTIALS` | `false` | Only honoured when origins are explicit. |
| `PUBLIC_WS_BASE_URL` | *(empty)* | Hard-code the external WS host when behind a proxy. |

### Telegram

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_ENABLED` | `false` | Master switch. |
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | *(empty)* | Target chat id or `@channel`. |
| `TELEGRAM_TIMEOUT_SECONDS` | `10` | Per-request timeout. |

### WebSocket recorder

| Variable | Default | Purpose |
|---|---|---|
| `WS_RECORDER_ENABLED` | `false` | Enable the self-connect recorder. |
| `WS_RECORDER_BASE_URL` | *(empty)* | Override the recorder's self-connect base URL. |
| `WS_RECORDER_DIR` | `ws_records` | Root folder for recorded `.jsonl` files. |
| `WS_RECORDER_NIFTY_KEY` | `NSE_INDEX\|Nifty 50` | Index instrument recorded first. |
| `WS_RECORDER_RECONNECT_SECONDS` | `5` | Reconnect backoff. |

### Bootstrap from backup

| Variable | Default | Purpose |
|---|---|---|
| `BOOTSTRAP_ENABLED` | `true` | Master switch. |
| `BOOTSTRAP_OVERWRITE` | `false` | Replace existing files when extracting. |
| `BOOTSTRAP_MONGO_URL` | *(empty)* | Mongo connection string for the backup DB. |
| `BOOTSTRAP_MONGO_DB` | `UPSTOX_APP_V2` | Database name. |
| `BOOTSTRAP_MONGO_BLOB_COLLECTION` | `backup_collection` | Collection holding the blob doc. |
| `BOOTSTRAP_MONGO_BLOB_DOC_ID` | `backup_historical_candles` | Fixed `_id` of the blob doc. |
| `BOOTSTRAP_MONGO_TIMEOUT_MS` | `10000` | Server-selection / connect timeout. |

## API routes

```text
GET  /api/instruments
POST /api/hard-refresh
POST /api/historical-refresh
POST /api/intraday-refresh
POST /api/ema-refresh
GET  /api/live-ema/status
GET  /api/live-ema/feed-status
POST /api/live-ema/start
POST /api/live-ema/stop
POST /api/live-ema/tick
GET  /api/live-ema/ws-endpoints
GET  /api/live-ema/ws-samples
GET  /api/candles
GET  /api/ema-crosses
GET  /api/logs
GET  /api/logs/modules
GET  /api/ws-samples
GET  /api/ws-samples/categories
GET  /api/ws-samples/main-index
GET  /api/ws-samples/options
GET  /api/ws-samples/sample/{sample_id}
GET  /api/telegram/status
POST /api/telegram/test
GET  /api/startup-status
GET  /api/health
GET  /docs
```

WS routes:

```text
WS /ws/live-ema/by-instrument/{instrument_key}
WS /ws/live-ema/by-parts?underlying=...&strike=...&type=...
```

Manual jobs:

```bash
curl -X POST http://127.0.0.1:8000/api/hard-refresh
curl -X POST http://127.0.0.1:8000/api/ema-refresh
curl -X POST http://127.0.0.1:8000/api/live-ema/tick
curl http://127.0.0.1:8000/api/live-ema/status
curl http://127.0.0.1:8000/api/live-ema/feed-status
```

## Git push

```bash
git init
git add .
git commit -m "Ordered Instrument Jobs FastAPI"
git branch -M main
git remote add origin <YOUR_GITHUB_REPOSITORY_URL>
git push -u origin main
```