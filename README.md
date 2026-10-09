# Ordered Instrument Jobs FastAPI

A FastAPI application that keeps a rolling local cache of 1-minute candles
for every subscribed instrument, derives 9/21 EMA crossings from them, and
also detects new crosses live during market hours.

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

## File layout

```
data/<underlying>/<strike>_<instrument_type>/historical.json
data/<underlying>/<strike>_<instrument_type>/ema_crosses.json
```

Examples:

```
data/nifty/25000_CE/historical.json
data/nifty/25000_CE/ema_crosses.json
data/sensex/70100_PE/historical.json
data/nifty/spot/historical.json            # non-option rows
```

`historical.json` is the single source of truth for candles.
`ema_crosses.json` is derived from it (batch job) and appended to live.

## Project behavior

- **Application start / restart**: runs jobs 1–4 in order, then starts job 5
  as a background task.
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

## EMA cross semantics

- Standard SMA-seeded EMA, `k = 2 / (period + 1)`.
- Fast/slow periods from `EMA_FAST_PERIOD` (default 9) and `EMA_SLOW_PERIOD`
  (default 21).
- **Bullish cross**: `fast − slow` goes from `<= 0` to `> 0`.
- **Bearish cross**: `fast − slow` goes from `>= 0` to `< 0`.
- Live-detected events are tagged with `"detected_by": "live"`; batch events
  have no such tag.

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

## Configuration

All settings are read from `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `SUBSCRIPTIONS_API_URL` | `https://feed.novag7.in/api/subscriptions` | Upstream subscriptions endpoint. |
| `REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout for the subscriptions fetch. |
| `LOG_LEVEL` | `INFO` | Root log level. |
| `LOG_FILE` | `logs/app.log` | Rotating log file path. |
| `DATA_DIR` | `data` | Root directory for cached files. |
| `HISTORY_LOOKBACK_DAYS` | `10` | Rolling window of candles to keep. |
| `HISTORY_MAX_CONCURRENCY` | `8` | Max in-flight instruments per job. |
| `MARKET_TIMEZONE` | `Asia/Kolkata` | Timezone used to evaluate the market window. |
| `MARKET_OPEN_TIME` | `09:15` | Intraday window start. |
| `MARKET_CLOSE_TIME` | `15:40` | Intraday window end. |
| `EMA_FAST_PERIOD` | `9` | Fast EMA period. |
| `EMA_SLOW_PERIOD` | `21` | Slow EMA period. |
| `LIVE_EMA_ENABLED` | `true` | Start the live polling job at startup. |
| `LIVE_EMA_TICK_OFFSET_SECONDS` | `10` | Seconds after each minute boundary to fire the tick. |
| `LIVE_EMA_IDLE_SLEEP_SECONDS` | `30` | Sleep when outside market hours before re-checking. |

## API routes

```text
GET  /api/instruments
POST /api/hard-refresh
POST /api/historical-refresh
POST /api/intraday-refresh
POST /api/ema-refresh
GET  /api/live-ema/status
POST /api/live-ema/start
POST /api/live-ema/stop
POST /api/live-ema/tick
GET  /api/health
GET  /docs
```

Manual jobs:

```bash
curl -X POST http://127.0.0.1:8000/api/hard-refresh
curl -X POST http://127.0.0.1:8000/api/ema-refresh
curl -X POST http://127.0.0.1:8000/api/live-ema/tick
curl http://127.0.0.1:8000/api/live-ema/status
```

## Git push

```bash
git init
git add .
git commit -m "Add live EMA cross polling job"
git branch -M main
git remote add origin <YOUR_GITHUB_REPOSITORY_URL>
git push -u origin main
