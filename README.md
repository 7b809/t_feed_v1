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
