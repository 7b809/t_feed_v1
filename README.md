# Market Data Foundation

`market-engine-v1` is the foundational FastAPI application for general market history, candlestick charts, and Upstox token lifecycle management. It retains the existing home and chart pages and the historical candle service. The chart page accepts an Upstox instrument key directly, so chart access does not rely on strategy-selected instruments.

## Retained functionality

- FastAPI home page, health endpoint, read-only log viewer, chart index, instrument-key chart pages, chart JSON, and historical/intraday candle APIs.
- Historical candle pagination through `GET /api/older-candles` and a chart-only `/ws/chart` feed that refreshes the current one-minute candle at a configurable interval.
- MongoDB token storage, safe token status, Upstox token validation, and scheduled token lifecycle checks.
- Authorized Telegram long polling with `/save_token`, `/token_status`, `/token_check`, `/cancel`, and `/help`.
- Shared logging and application startup/shutdown lifecycle.

The chart renders historical candlesticks from Upstox history data. Its WebSocket accepts the chart's instrument key and refreshes the current intraday candle directly, without strategy-selected option subscriptions. Strategy event WebSockets have been removed.

## Removed scope

This branch no longer includes EMA calculation or alerts, Opening Range calculations, instrument isolation, strategy-driven option subscriptions, signal selection, order placement or simulations, trade/order dashboards, strategy persistence, or strategy background jobs and WebSocket events.

## Requirements

- Python 3.10 or later.
- MongoDB for token persistence and lifecycle status.
- A valid Upstox access token stored in the configured MongoDB token document for chart history requests and token checks.
- A Telegram bot and authorized chat for interactive token saving; Telegram is optional if token management is performed elsewhere.

Copy `.env.example` to `.env` and fill in local values. Do not commit `.env` or real credentials.

| Variable | Purpose |
| --- | --- |
| `MONGO_URL` | MongoDB connection string |
| `MONGO_DB` | Database containing the token document |
| `TOKENS_COLLECTION` | Collection containing the token document |
| `UPSTOX_TOKEN_DOC_ID` | Token document `_id` (defaults to `upstox_access_token`) |
| `UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES` | Scheduled token validation interval |
| `MARKET_TIMEZONE` | Timezone used for token timestamps |
| `TELEGRAM_ENABLED` | Enables Telegram notifications and polling |
| `TELEGRAM_BOT_TOKEN` | Telegram bot credential |
| `TELEGRAM_CHAT_ID` | Authorized chat for token commands |
| `TELEGRAM_TOKEN_BOT_ENABLED` | Enables the token polling worker |
| `TELEGRAM_TOKEN_BOT_POLL_SECONDS` | Delay between polling retries |
| `TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT` | Telegram long-poll timeout |
| `TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT` | Restricts commands to `TELEGRAM_CHAT_ID` |
| `CHART_MAX_BATCH_WORKERS` | Maximum parallel history requests |
| `CHART_LIVE_POLL_SECONDS` | Candle refresh interval for chart WebSocket clients (minimum 15 seconds) |

## Install and run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the required values in `.env`, then launch from the repository root:

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

The home page is `/`, chart picker `/charts`, read-only logs `/logs`, health check `/health`, and token status `/token/status`. To open a chart, enter an Upstox instrument key on the home/chart page or visit `/chart/{URL-encoded-instrument-key}`. Candle APIs are under `/candles`.

## Token management

In the authorized Telegram chat, send `/save_token`, then send the new Upstox token in a private message. The bot attempts to delete the token message, validates the token against Upstox before saving, and never echoes the raw value. `/token_status` reports masked token metadata; `/token_check` validates the stored token. The application starts the existing polling worker and schedules token checks at the configured interval. The worker is stopped during application shutdown.

The token status API never returns the access token. Keep MongoDB and Telegram credentials in local environment configuration. Do not paste secrets into logs or issue reports.

## Basic verification

With MongoDB and Upstox configured, check `/health`, `/token/status`, `/charts`, and a chart page. `GET /candles?instrument_key=...` retrieves merged history and intraday candles; `GET /api/older-candles?instrument_key=...&before_date=YYYY-MM-DD` retrieves an earlier history window. Telegram commands require working bot credentials and the configured authorized chat.

Install the test dependency with `python -m pip install -r requirements-dev.txt`, then run `python -m pytest -q`. The regression tests mock MongoDB, Upstox, and Telegram calls.
