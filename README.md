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

## Instrument search reliability and Cloudflare blocks

Instrument search uses the Python `requests` HTTP client with a configurable, descriptive application User-Agent (`UPSTOX_USER_AGENT`) and bounded connect/read timeouts. It does not spoof a browser User-Agent.

Successful Upstox search responses are cached as JSON under `data/instruments/search_cache/`, keyed by the complete search parameter set. If the upstream API is unavailable, the service can return the last cached response up to `UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS` old. Responses include a `cache` object indicating whether results came from Upstox or the local cache and whether the result is stale. Cache files are runtime data under `DATA_DIR` and are included in the existing daily data backup.

Cloudflare Error 1010 (`browser_signature_banned`) is treated as a non-retryable upstream access block. The service logs the Cloudflare Ray ID and returns a structured error when no usable cache entry exists; it does not repeatedly retry or disguise the client. This error requires Upstox/API-owner review. Share the Ray ID, timestamp, and the server's actual outbound public IP with Upstox support. A cached result may still be served when available.

Settings:
- `UPSTOX_USER_AGENT=sandbox_v1/1.0 (Upstox instrument-search integration)`
- `UPSTOX_HTTP_TIMEOUT_SECONDS=12`
- `UPSTOX_INSTRUMENT_CACHE_ENABLED=true`
- `UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS=604800` (7 days)

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

Backups include files under `data/` only; credentials and `.env` are never archived. Empty data directories are rejected rather than uploading an empty ZIP.

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


## GTT orders (Upstox v3)

The **/gtt** page provides a BUY-entry, multi-leg GTT ticket. It searches the same Upstox instrument catalogue as the BUY ticket and uses the selected instrument's returned `lot_size` metadata to convert lots to units.

API:
- `GET /api/v1/gtt/config` — GTT capability and default lot-size map.
- `POST /api/v1/gtt/place` — place a multi-leg GTT.
- `GET /gtt` — responsive GTT ticket.

Example request:
```json
{
  "instrument_key": "NSE_FO|<instrument-id>",
  "entry": null,
  "target": 150,
  "stoploss": 130,
  "lot": 1,
  "lot_size": 65,
  "product": "I"
}
```

A blank/null entry fetches the current LTP from Upstox and uses an ENTRY rule with `trigger_type: IMMEDIATE` at that price. A specified entry uses `ABOVE` when above LTP and `BELOW` when below LTP. The BUY-side validation requires `stoploss < entry < target`. Quantity sent to Upstox is `lot × lot_size`; Upstox expects quantity in units, not number of lots. The instrument-search result's lot-size metadata is preferred; the service's static symbol map is only a fallback and may need updating when exchange lot sizes change.

The request is sent to `POST https://api.upstox.com/v3/order/gtt/place` as a `MULTIPLE` GTT with ENTRY, TARGET and STOPLOSS rules. Per Upstox documentation, TARGET and STOPLOSS are exit-side SELL legs for a BUY entry, and SELL GTT legs require EDIS authorization. Complete EDIS authorization on an Upstox platform before placing these GTT orders. GTT legs execute as LIMIT orders when triggered; a GTT can remain active up to one year, subject to Upstox's rules.

**Live-trading safety:** GTT placement is disabled unless `UPSTOX_SANDBOX=false` and `TRADING_ENABLED=true`. The GTT endpoint refuses sandbox mode. Use a valid live token and verify all prices, lot size, product and account before enabling. No live GTT has been submitted as part of deploying this code.


## External API authentication

External server-to-server trading integrations use a separate API namespace:
`/api/external/v1`. The existing `/api/v1` routes remain the dashboard routes and do not require browser-exposed shared secrets.

Configure these values in the private server `.env` file:

- `EXTERNAL_API_AUTH_ENABLED=true` — enables authentication for every external mutation route.
- `EXTERNAL_API_SOURCE_TOKEN` — independently generated secret of exactly 32 characters.
- `EXTERNAL_API_DESTINATION_TOKEN` — independently generated secret of exactly 32 characters.

When authentication is enabled, external requests must include both headers:

- `X-Source-Token: <source token>`
- `X-Destination-Token: <destination token>`

Both values are checked independently using constant-time comparison. Missing or invalid headers return HTTP 401. If authentication is enabled but either server-side token is not exactly 32 characters, the API fails closed with HTTP 503. When `EXTERNAL_API_AUTH_ENABLED=false` (the default), the external endpoints do not require either header.

Authenticated external mutation endpoints:

- `POST /api/external/v1/orders/place`
- `POST /api/external/v1/orders/cancel/bulk`
- `POST /api/external/v1/orders/{order_id}/cancel`
- `POST /api/external/v1/orders/exit-by-ids`
- `POST /api/external/v1/orders/{order_id}/exit`
- `POST /api/external/v1/orders/exit-all`
- `POST /api/external/v1/gtt/place`

Example:

```bash
curl -X POST "https://your-host/api/external/v1/orders/place" \\
  -H "Content-Type: application/json" \\
  -H "X-Source-Token: 32-character-source-secret-here" \\
  -H "X-Destination-Token: 32-character-destination-secret" \\
  -d '{"instrument_token":"NSE_FO|12345","quantity":65,"transaction_type":"BUY"}'
```

Generate two independent random secrets; do not reuse the Upstox access token, commit these secrets, place them in frontend JavaScript, or send them over plain HTTP. Use HTTPS. Authentication is an additional gate and does not override `TRADING_ENABLED`, `UPSTOX_SANDBOX`, model validation, or Upstox account restrictions.
