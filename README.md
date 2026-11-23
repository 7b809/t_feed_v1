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
- Upstox live mode: selected by default (sandbox mode must be explicitly enabled)
- Regular BUY order execution: disabled by default (`TRADING_ENABLED=false`)
- Live GTT placement: enabled by default (`GTT_ENABLED=true`); can be disabled independently

The dashboard order-entry workflow is BUY-only. The API model also rejects SELL order placement; opposite-side SELL requests are created internally only by the existing square-off workflow. Regular BUY order placement remains gated by `TRADING_ENABLED=true`. GTT placement has its own `GTT_ENABLED` switch, which defaults to `true`, and targets the live Upstox account because `UPSTOX_SANDBOX` defaults to `false`. Configure a valid live access token in MongoDB. To intentionally switch the service to sandbox for testing, set `UPSTOX_SANDBOX=true` and use a sandbox token; live GTT placement will then be rejected.

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
- `POST /api/v1/orders/exit-all` — direct official Upstox REST position square-off without order IDs; accepts optional `tag` and `segment` in the JSON body.
- `GET /api/v1/orders/order-book` — live current-trading-day order book fetched directly from Upstox, including orders placed outside this project.
- `POST /api/external/v1/orders/order-book` — authenticated external order-book read; body requires `api_token`.

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

**Live-account operation:** this project defaults to `UPSTOX_SANDBOX=false` and `GTT_ENABLED=true`. Set a valid live access token in the configured MongoDB token document. GTT placement is independently controlled by `GTT_ENABLED` and does not require enabling regular order placement with `TRADING_ENABLED`. It never squares off or cancels unrelated positions. A GTT is a real broker-side order: verify instrument, lot size, quantity, entry, target, stop-loss and product before submitting. Sandbox testing requires explicitly setting `UPSTOX_SANDBOX=true` and using a sandbox token; live GTT requests are blocked in sandbox mode.



## External API authentication

External server-to-server trading integrations use the separate `/api/external/v1` namespace. Existing dashboard routes under `/api/v1` remain unchanged.

Configure one shared secret in the private server `.env` file:

```env
EXTERNAL_API_AUTH_ENABLED=true
EXTERNAL_API_SOURCE_TOKEN=your_exactly_32_character_secret
```

When enabled, every external mutation request must include the same token in its JSON payload using the `api_token` property. The value must match `EXTERNAL_API_SOURCE_TOKEN` exactly and be 32 characters long. The service compares it using constant-time comparison. Missing or incorrect values return HTTP 401; if the server-side token is not exactly 32 characters, the service fails closed with HTTP 503.

Example order placement payload:

```json
{
  "api_token": "your_exactly_32_character_secret",
  "instrument_token": "NSE_FO|12345",
  "quantity": 65,
  "transaction_type": "BUY"
}
```

For single-order cancel/exit endpoints, which otherwise have no request body, send a JSON body containing the token:

```json
{
  "api_token": "your_exactly_32_character_secret"
}
```

External mutation endpoints:

- `POST /api/external/v1/orders/place`
- `POST /api/external/v1/orders/cancel/bulk`
- `POST /api/external/v1/orders/{order_id}/cancel`
- `POST /api/external/v1/orders/exit-by-ids`
- `POST /api/external/v1/orders/{order_id}/exit`
- `POST /api/external/v1/orders/exit-all`
- `POST /api/external/v1/gtt/place`

When `EXTERNAL_API_AUTH_ENABLED=false`, external requests do not require the token. Never commit the real secret, expose it in browser JavaScript, or send it over plain HTTP. Use HTTPS. This authentication gate does not override `TRADING_ENABLED`, `UPSTOX_SANDBOX`, request validation, or broker account restrictions.

## Orderflow GTT lifecycle and broker cleanup

The authenticated server-to-server API exposes:

- `POST /api/external/v1/gtt/place` — place a multi-leg GTT and persist its GTT ID, source order and isolation.
- `POST /api/external/v1/gtt/cancel` — cancel one untriggered GTT by ID.
- `POST /api/external/v1/gtt/cleanup` — reconcile and clean up only the supplied Orderflow GTT IDs.

Enable these independently from normal dashboard order placement:

```env
EXTERNAL_API_AUTH_ENABLED=true
EXTERNAL_API_SOURCE_TOKEN=<same 32-character token configured in orderflow_v1>
ORDERFLOW_CLEANUP_ENABLED=true
GTT_ENABLED=true
TRADING_ENABLED=false
UPSTOX_SANDBOX=false
```

`ORDERFLOW_CLEANUP_ENABLED` permits authenticated, ID-scoped cancellation and square-off for GTTs supplied by Orderflow. It does not enable regular dashboard orders and the cleanup manager never calls account-wide `exit_positions`.

Cleanup behavior:

1. Fetch GTT details and inspect the ENTRY/TARGET/STOPLOSS rule statuses.
2. Cancel an ENTRY that is still `SCHEDULED` and has no generated order ID.
3. For a triggered ENTRY, reconcile its exact generated order ID, cancel pending entry/exit orders, and square off only the remaining quantity tracked against that ENTRY.
4. Treat zero filled entry quantity as `NO_OPEN_POSITION`.
5. Block replacement if a triggered GTT has an active TARGET/STOPLOSS rule without an order ID, if a broker state cannot be reconciled, or if cancellation/square-off is not confirmed.

Upstox does not provide details for completed GTTs. Such IDs cannot be reconstructed from the GTT details API alone; cleanup therefore fails closed and requires manual reconciliation rather than assuming that exposure is gone. The existing account-wide Exit All endpoint remains separate and is not used by this manager.


## Daily portfolio P&L and profit-target lockout

The service reads the current Upstox portfolio from
`GET https://api.upstox.com/v2/portfolio/short-term-positions`.
It aggregates every returned position row, including rows whose net quantity
is zero because they may contain realised P&L from trades already closed today.

- `realised_pnl`: sum of every position's `realised` field.
- `unrealised_pnl`: sum of every position's `unrealised` field.
- `total_pnl`: realised P&L plus unrealised P&L.
- `guard_pnl`: total P&L when `DAILY_PROFIT_INCLUDE_UNREALISED=true`; otherwise realised P&L only.

Dashboard endpoint:
- `GET /api/v1/portfolio/today-pnl`

Authenticated external endpoint (send `api_token` in the JSON body):
- `POST /api/external/v1/portfolio/today-pnl`

The guard is checked before every new BUY order and every new GTT placement.
Once `guard_pnl` reaches `DAILY_PROFIT_TARGET`, new entries are blocked until
the next Asia/Kolkata calendar date. The lock is stored in MongoDB collection
`daily_profit_guard`, keyed by `YYYY-MM-DD`, and survives restarts and
subsequent drops in unrealised P&L. Position exits, cancellations and cleanup
operations are not blocked by this entry guard.

Default environment settings:

```env
DAILY_PROFIT_GUARD_ENABLED=true
DAILY_PROFIT_TARGET=2000
DAILY_PROFIT_INCLUDE_UNREALISED=true
DAILY_PROFIT_GUARD_FAIL_CLOSED=true
DAILY_PROFIT_LOCKOUT_UNTIL_NEXT_DAY=true
DAILY_PROFIT_GUARD_COLLECTION=daily_profit_guard
```

The default target is INR 2,000 and is configurable. If the Upstox positions
response or persisted lock cannot be verified, the default fail-closed behavior
blocks new entries. The guard uses broker-reported realised/unrealised P&L and
does not independently subtract brokerage, taxes or other charges.

The lockout does not cancel GTTs submitted before the target was reached.
Those broker-side GTTs may still trigger and must be managed through the
existing GTT lifecycle/cleanup operations.


## Live broker portfolio dashboard

The dashboard now presents two separate sources of truth:

- **Project order ledger:** local MongoDB/JSON audit history for activity placed or tracked by this service.
- **Broker portfolio:** live Upstox short-term positions and long-term Demat holdings, including trades placed through Upstox mobile or website even when the project did not create the order.

Broker portfolio APIs (the access token is loaded server-side from MongoDB):

- `GET /api/v1/portfolio/overview` — combined dashboard snapshot.
- `GET /api/v1/portfolio/positions` — proxies Upstox `GET /v2/portfolio/short-term-positions`.
- `GET /api/v1/portfolio/holdings` — proxies Upstox `GET /v2/portfolio/long-term-holdings`.
- Existing `GET /api/v1/portfolio/today-pnl` continues to return the daily-profit-guard snapshot.

P&L definitions:
- **Today's P&L** = sum of Upstox short-term positions' `realised` + `unrealised`, including zero-net-quantity rows.
- **Total P&L** in the dashboard = today's short-term positions P&L + Upstox-reported long-term holdings `pnl`.
- Holdings P&L is also shown separately. This combined figure is a current broker snapshot, not a lifetime realised-profit ledger.

The overview response can report partial availability using `status=partial` and an `errors` object when one portfolio endpoint fails. It does not silently replace missing broker data with zero.

## GTT cancellation APIs

GTT cancellation uses the official Upstox REST `DELETE https://api.upstox.com/v3/order/gtt/cancel` endpoint through `requests.delete`, not the SDK cancellation method. The API cancels an untriggered GTT and all its associated legs. A triggered GTT requires lifecycle reconciliation; cancelling a GTT is not equivalent to squaring off an already-created position.

Dashboard routes:
- `POST /api/v1/gtt/cancel` — one GTT, body `{"gtt_order_id":"GTT-..."}`.
- `POST /api/v1/gtt/cancel/bulk` — 1–20 GTT IDs, body `{"gtt_order_ids":["GTT-...","GTT-..."]}`.

Authenticated external routes:
- `POST /api/external/v1/gtt/cancel` — one GTT.
- `POST /api/external/v1/gtt/cancel/bulk` — 1–20 GTT IDs.
- Existing `POST /api/external/v1/gtt/cleanup` remains the Orderflow lifecycle reconciliation and square-off workflow.

Upstox exposes a single-GTT DELETE operation, not an atomic bulk endpoint. Bulk cancellation therefore sends one broker request per ID and returns per-ID results with success/failure counts; partial success is possible.

GTT cancellation has an independent `GTT_CANCEL_ENABLED=true` setting (default true) and no longer depends on `ORDERFLOW_CLEANUP_ENABLED`. Live GTT cancellation requires `UPSTOX_SANDBOX=false` and a valid live access token. `ORDERFLOW_CLEANUP_ENABLED` continues to gate only Orderflow lifecycle cleanup.


## Official Upstox order book and Exit All REST APIs

The server now calls the official Upstox REST APIs directly with `requests`; the Exit All route no longer calls the Upstox Python SDK's `OrderApi.exit_positions()` method.

**Order book**

- Upstox request: `GET https://api.upstox.com/v2/order/retrieve-all`.
- Internal route: `GET /api/v1/orders/order-book`.
- Authenticated external route: `POST /api/external/v1/orders/order-book`, with `{"api_token":"<32-character-secret>"}`.
- Returns the broker's current trading-day orders, including orders submitted from Upstox mobile/web and orders not present in the project's local audit ledger.
- The Upstox order book is a current-day view; it is not lifetime order history.

**Exit All Positions**

- Upstox request: `POST https://api.upstox.com/v2/order/positions/exit`.
- No JSON request body is sent to Upstox. Optional `tag` and `segment` are sent as query parameters.
- Project routes: `POST /api/v1/orders/exit-all` and authenticated `POST /api/external/v1/orders/exit-all`.
- Example body: `{"segment":"NSE_FO","tag":"strategy-a"}`; use `{}` to request all eligible positions.
- The response preserves `data.order_ids`, per-position `errors`, `summary` counts, and the original broker response. HTTP 207 / `partial_success` responses are returned as `exit_all_partial_success` instead of being described as a full success.
- The operation is still gated by `TRADING_ENABLED=true`, requires `UPSTOX_SANDBOX=false`, and records an audit entry before sending the broker request.

Important Upstox constraints: Exit All places opposite-side MARKET orders and applies Market Price Protection. It is available during the relevant segment's market hours, does not support Delivery orders in the EQ segment, and the request is limited to at most 10 positions. Upstox executes BUY positions before SELL positions and automatically slices orders where required. Tag filtering is for intraday positions; do not use a tag filter for carry-forward positions. A successful API acknowledgement means exit orders were submitted, not that every exit order has filled. Refresh the order book and positions to verify final quantities.

Dashboard now includes a separate broker order-book panel. It is not merged into the project's local daily order ledger; the ledger remains the project's own persisted audit history.
