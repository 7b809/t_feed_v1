# Dual WebSocket Feed Capture

A FastAPI service that retrieves active instruments from an upstream subscription API, runs one EMA WebSocket and one raw-market WebSocket per instrument during the configured India market window, and durably appends every received message to date-rotated JSON Lines files. It includes exponential reconnects, a connection semaphore, subscription reconciliation, rotating logs, graceful shutdown, and runtime control APIs.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# Windows: copy .env.example .env
```

Review `.env`, especially `SUBSCRIPTIONS_API_URL`. No URL, time, or output path is embedded in runtime logic.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use `./start.sh` on POSIX and `run.bat` on Windows. Open <http://127.0.0.1:8000>; OpenAPI is at <http://127.0.0.1:8000/docs>.

## Market-hours behavior

The supervisor evaluates weekdays and clock time only in `MARKET_TIMEZONE`, defaulting to `Asia/Kolkata`. The default inclusive window is Monday through Friday, 09:00 to 15:40 IST. Outside it, all workers are absent and the supervisor performs an interruptible wait until the next weekday opening. Holidays are intentionally out of scope. Each reconnect rechecks the gate, and active workers poll the gate so they close at market close.

Subscription snapshots refresh every 900 seconds only while the market is open. New instrument keys receive EMA and raw workers, removed keys are cancelled, and unchanged keys continue uninterrupted. `POST /api/refresh` fetches a snapshot immediately; outside market hours it updates the cache but does not open sockets.

## Raw URL derivation

`connect_url` is retained as the EMA URL. If `ws_base_url` is a host such as `wss://feed.novag7.in`, `/ws/market` is appended. Otherwise `RAW_FEED_BASE` is used. The raw URL receives an RFC3986-encoded `instrument_key` query parameter.

## Output files

```text
files/
├── ema/
│   └── NSE_INDEX__Nifty_50__2026-10-12.jsonl
└── raw/
    └── NSE_INDEX__Nifty_50__2026-10-12.jsonl
```

Each line includes `received_at`, `feed`, `instrument_key`, `url`, and the exact text in `raw`. A `parsed` field is added when the text is valid JSON. Files rotate by India market date. They flush and `fsync` after the configured interval or message count, whichever comes first, and always flush on worker close.

Example line:

```json
{"received_at":"2026-10-12T09:15:03.123456+05:30","feed":"ema","instrument_key":"NSE_INDEX|Nifty 50","url":"wss://ema.example/ws","raw":"{\"ltp\":24450.1}","parsed":{"ltp":24450.1}}
```

## API reference

### `GET /api/health`

```json
{"status":"ok","in_market_hours":true,"supervisor_running":true,"instruments":42,"active_workers":84,"connected_workers":80,"total_messages_written":123456,"last_message_at":"2026-10-12T10:31:07.123456+05:30"}
```

### `GET /api/status`

Returns the market gate, next wake-up, instrument count, and each worker's state, timestamps, counters, error, URL, and current file.

```json
{"in_market_hours":false,"next_wakeup_at":"2026-10-13T09:00:00+05:30","instruments":2,"workers":[]}
```

Worker states are `pending`, `connecting`, `connected`, `reconnecting`, `closed`, `error`, or `stopped`.

### `GET /api/subscriptions`

Returns the latest upstream snapshot augmented with `ema_connect_url`, `raw_connect_url`, and `raw_feed_base`.

### `POST /api/refresh`

Forces an upstream fetch and reconciles workers immediately when the market is open.

### `POST /api/start`

Starts the supervisor. It is idempotent and reports `already_running` on a duplicate call.

### `POST /api/stop`

Interrupts supervisor sleep, cancels all workers, closes sockets, flushes files, and stops capture.

### `GET /`

A small live status dashboard. `GET /docs` provides Swagger UI.

## Configuration

See `.env.example`. `WS_MAX_CONCURRENT_CONNECTIONS=0` disables the semaphore. File and log paths are relative to the project working directory unless absolute paths are supplied.

## Tests

```bash
pytest -q
```

Tests cover inclusive market boundaries, Saturday-to-Monday wake-up, filename sanitization, and raw URL construction.

## Troubleshooting

### No files are appearing

Open `/api/status` and verify `in_market_hours` is `true`. Also confirm the snapshot contains valid `instrument_key` and `connect_url` values, workers are connected, and the process can write to `FILES_DIR`.

### Sockets keep reconnecting

Check the upstream hosts are reachable, TLS certificates are valid, and the source `instrument_key` is correct. The service URL-encodes the key automatically. Inspect `last_error` in `/api/status` and `LOG_FILE` for details.

### Supervisor is running but there are no workers

Outside market hours this is expected. During market hours, call `POST /api/refresh` and inspect the subscription endpoint response. Invalid items without both `instrument_key` and EMA `connect_url` are skipped.

### Shutdown appears delayed

The supervisor wait is interruptible. Socket close timeout is two seconds; file `fsync` speed depends on the storage device.
