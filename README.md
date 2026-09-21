# NIFTY EMA Crossover Project

This service refreshes nearest-expiry NIFTY option contracts on weekdays at 09:00 Asia/Kolkata, filters the configured strike range, optionally selects a random test subset, builds EMA 9/21 state from historical plus current-day intraday candles, and then processes the V3 `prev_ohlc` completed one-minute candle once per minute.

## Setup

1. Copy `.env.example` to `.env`.
2. Configure MongoDB and strike range values.
3. Store the Upstox token document:

```json
{"_id": "upstox_access_token", "access_token": "YOUR_TOKEN"}
```

4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Run:

```bash
python main.py
```

## Flow

```text
Start/restart
  -> load settings from core/config.py
  -> load token from MongoDB
  -> restore today's selection/state when valid
  -> otherwise refresh contracts after 09:00
  -> historical + intraday EMA warmup
  -> after each minute boundary plus delay
  -> fetch V3 I1 OHLC in batches
  -> consume prev_ohlc only
  -> skip duplicate timestamp
  -> update EMA state
  -> save crossover only when bullish/bearish cross occurs
  -> repeat until market close
  -> next weekday refresh at 09:00
```

## Data

- `data/nearest_nifty_option_contracts.json`: filtered nearest-expiry contracts
- `data/runtime/selected_contracts.json`: fixed daily selection used by all services
- `data/runtime/service_state.json`: scheduler state
- `data/ema_state/*.json`: incremental EMA continuation state
- `data/crossovers/YYYY-MM-DD/*.json`: crossover-only records
- `data/candles/YYYY-MM-DD/*.json`: optional completed-candle journal

`TEST_FLAG=true` chooses one random daily subset. `TEST_FLAG=false` processes all valid contracts in the configured range. The service uses `MarketQuoteV3Api.get_market_quote_ohlc("I1", instrument_key=...)` and reads `prev_ohlc` as the completed candle.

## Control API

The API listens on `API_HOST:API_PORT`. Administrative endpoints require `X-API-Key`.

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/hard-refresh \
  -H "X-API-Key: change-this-secret"

curl http://localhost:8000/hard-refresh/JOB_ID \
  -H "X-API-Key: change-this-secret"

curl http://localhost:8000/state \
  -H "X-API-Key: change-this-secret"
```

`POST /hard-refresh` returns HTTP 202 and executes contract refresh, selection, historical retrieval, intraday retrieval, EMA rebuild, crossover persistence, and state persistence in a dedicated thread. Only one refresh job can run at a time.

## Telegram

Set `TELEGRAM_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. Notifications are sent for service lifecycle, refresh completion/failure, scheduler errors, and EMA crossovers. Telegram failure is logged and does not stop market processing.
