# sandbox_v1 — Upstox Order Service

A standalone FastAPI order service using the official Upstox Python SDK, with a responsive browser dashboard for order history, exits and BUY order entry.

## Dashboard

- `/` — today's order ledger, historical daily ledgers, order status, eligible exit/cancel actions and exit-all positions.
- `/buy` — BUY-only order ticket with live instrument suggestions from Upstox's official Instrument Search API.
- `/docs` — API documentation; `/health` — service health.

The shared responsive layout uses Bootstrap 5 and Bootstrap Icons. Desktop has a left navigation sidebar; mobile has a bottom navigation bar and a floating page launcher.

## Instrument search

The browser calls `GET /api/v1/instruments/search?query=NIFTY&records=20`. The server proxies the official Upstox endpoint `GET https://api.upstox.com/v2/instruments/search` with the access token loaded from MongoDB. The token is never sent to the browser. Suggestions include the official `instrument_key`, trading symbol, segment, expiry, instrument type and lot-size metadata when provided.

Search supports Upstox's `query`, `exchanges`, `segments`, `instrument_types`, `expiry`, `page_number` and `records` parameters. Results are capped at 30 by Upstox.

## Defaults and safety

- API port: **8004**
- MongoDB database: **UPSTOX_APP_V2**
- Token source: **app_tokens**, document **_id=upstox_access_token**, field **access_token**
- Daily order collection: **sandbox_orders**
- Default order: **BUY**, quantity **65**, **MARKET**, product **I (Intraday)**, validity **DAY**
- Upstox sandbox mode: enabled by default
- Order execution: disabled by default

The dashboard order-entry workflow is BUY-only. The API model also rejects SELL order placement; opposite-side SELL requests are created internally only by the existing square-off workflow. The service does not submit orders until `TRADING_ENABLED=true` is configured. Keep `UPSTOX_SANDBOX=true` with a sandbox token for initial tests.

## Install and run

1. Copy `.env.example` to `.env`.
2. Set `MONGO_URI` and ensure the `UPSTOX_APP_V2.app_tokens` document is available.
3. Install dependencies: `pip install -r requirements.txt`.
4. Run `./start.sh` on Linux or `run.bat` on Windows.
5. Open the service at port 8004.

## Telegram notifications

Telegram uses the same environment-variable pattern as the isolation project:

- `TELEGRAM_ENABLED=true` enables the notification pipeline (default true).
- `TELEGRAM_BOT_TOKEN` is the bot token from BotFather.
- `TELEGRAM_CHAT_ID` is the destination chat or group ID.

Set the credentials in the server's private `.env` file; never commit real credentials. If the token or chat ID is missing, notifications are skipped. Telegram HTTP/network errors are logged and never interrupt order execution.

Events include service startup/shutdown, successful and blocked/failed order placement, cancellation, individual exit, exit-all, and project update progress/failures.

## Daily MEGA data backup

The service mirrors the daily data backup flow used by `isolation_v1`. It ZIPs the complete `data/` directory (including `data/orders/YYYY-MM-DD.json` ledgers), verifies the ZIP, calculates SHA-256, uploads it to MEGA and records the result in MongoDB collection `daily_data_backup_state`. The temporary ZIP is removed after upload.

Default schedule: weekdays at **16:05 Asia/Kolkata**. Remote archive naming is `sandbox_v1/YYYYMMDD_data.zip`; configure a different remote folder with `MEGA_BACKUP_ROOT`. Backup staging defaults to `./backups`, outside `DATA_DIR`, and is git-ignored.

Required private `.env` settings: `DAILY_DATA_BACKUP_ENABLED=true`, `MEGA_ENABLED=true`, `MEGA_EMAIL`, `MEGA_PASSWORD`, and `MEGA_BACKUP_ROOT=sandbox_v1`. Configure the schedule with `DAILY_DATA_BACKUP_HOUR`, `DAILY_DATA_BACKUP_MINUTE`, and `DAILY_DATA_BACKUP_DAYS`.

Backup endpoints:

- `GET /api/v1/backups/mega/status` — configuration, next run and latest result
- `GET /api/v1/backups/mega/history?limit=30` — recent daily backup records
- `POST /api/v1/backups/mega/run` — trigger a manual full `data/` backup

Telegram notifications report backup start, completion and failure when Telegram is configured. Missing MEGA credentials do not prevent the order service from starting.

## Order history APIs

- `GET /api/v1/orders/today`
- `GET /api/v1/orders/dates` — available dates from local daily JSON and MongoDB
- `GET /api/v1/orders/history?date=YYYY-MM-DD` — selected historical daily ledger
- `GET /api/v1/config/order-defaults`

History is read from `data/orders/YYYY-MM-DD.json` first, then MongoDB when no local mirror exists. Order lifecycle records remain in the existing `UPSTOX_APP_V2.sandbox_orders` collection.

## Trading actions

- `POST /api/v1/orders/place` — BUY-only order placement
- `POST /api/v1/orders/{order_id}/cancel` and `POST /api/v1/orders/cancel/bulk`
- `POST /api/v1/orders/{order_id}/exit` and `POST /api/v1/orders/exit-by-ids`
- `POST /api/v1/orders/exit-all` — broker-native position square-off without order IDs

The dashboard asks for confirmation before submitting exit or cancel requests. An exit request is an opposite-side MARKET order for the filled quantity remaining after prior exits; it is not a cancellation. Exit-all uses Upstox's native position exit API and may affect every eligible position in the selected account.

## Persistence and audit

Each trading date has one MongoDB document in `UPSTOX_APP_V2.sandbox_orders` with `_id` and `date` set to YYYY-MM-DD, an `orders` array, `created_time`, and `last_modified_time`. The same daily document is mirrored to `data/orders/YYYY-MM-DD.json`. The existing service records order requests, broker responses, exits, cancellations and exceptions.
