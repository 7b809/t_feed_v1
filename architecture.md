# Application architecture

```text
FastAPI app (main.py)
├── Home, health, and read-only log routes
├── Chart routes
│   └── Chart history service → Upstox History API → candlestick pages
├── Candle API
│   └── Historical and intraday retrieval, merge, and pagination
│       └── Chart WebSocket refreshes the current candle on demand
└── Token lifecycle
    ├── MongoDB token store
    ├── Upstox profile validation
    ├── Scheduled token monitor
    └── Telegram long-polling token commands
```

`main.py` owns only application lifecycle setup: refresh the cached token, perform a guarded startup validity check, start periodic token monitoring, and start/stop the Telegram token command worker. The chart layer accepts instrument keys as general inputs and fetches candles on demand. It does not read a strategy-selected instrument cache. `/ws/chart` refreshes one current intraday candle per connected chart at the configured polling interval.

Token data is stored in MongoDB under the configured token document ID. The token store masks the access token in status summaries. The Telegram worker restricts commands to the configured chat, validates a submitted token before persistence, deletes the submitted Telegram message when possible, and does not log token contents.

Strategy calculations and their routes, persistence, UI, and background work have been removed. Strategy event WebSockets are gone; retained chart live updates use the chart-specific candle endpoint.
