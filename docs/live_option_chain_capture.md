# Live option-chain and Supertrend capture

Capture starts at application startup once today's OR levels and the primary
Upstox market feed are ready. A 60-second scheduler retry covers deferred
startup, pre-market starts and temporary upstream failures.

## Index and contract selection

Set `OPTION_CHAIN_INDEXES` to a comma-separated list of Upstox index
instrument keys. The default is only `NSE_INDEX|Nifty 50`. Add BANKNIFTY,
MIDCPNIFTY or SENSEX keys later without changing the service code.

For each index, the service reads its OR average, requests contracts through
Upstox `OptionsApi.get_option_contracts`, filters expired/non-option rows,
then selects both CE and PE contracts. `OPTION_CHAIN_EXPIRY_MODE=all` keeps
all non-expired expiries returned by Upstox; `nearest` keeps only the nearest
valid expiry. A positive strike-range setting limits strikes to the OR-average
window; zero means all strikes. The default is all expiries and all NIFTY strikes.

The selected instruments are subscribed in batches of 100 on the existing
primary `MarketFeedService`; no second Upstox market-feed connection is
created.

## Per-instrument storage

```text
data/option_chain/nifty/YYYYMMDD/25100_ce_NSE_FO_64443/
├── meta.json
├── live_feed.jsonl
└── supertrend_feed.jsonl
```

Each contract folder contains its metadata, raw live Upstox market frames,
and raw Supertrend WebSocket events. The Supertrend URL is
`/ws/supertrend/{URL-encoded instrument_key}`. It runs on a dedicated
asyncio loop with reconnect/backoff and a configurable concurrency cap
(default 120). The existing isolation/opposite-leg capture is unchanged.

The 60-second retry is idempotent by IST date and instrument key. Both feed
types run during Mon-Fri 09:15-15:30 IST. With
`REQUIRE_TODAY_ONLY_SCOPE=true`, frames lacking `data_scope=today_only`
are rejected. `OPTION_CHAIN_FEED_MODE` must match the primary `FEED_MODE`.

## Environment

```env
OPTION_CHAIN_CAPTURE_ENABLED=true
OPTION_CHAIN_INDEXES=NSE_INDEX|Nifty 50
OPTION_CHAIN_EXPIRY_MODE=all
OPTION_CHAIN_DATA_DIR=data/option_chain
OPTION_CHAIN_FEED_MODE=full
OPTION_CHAIN_WRITE_QUEUE_SIZE=20000
OPTION_CHAIN_NIFTY_RANGE_POINTS=0
OPTION_CHAIN_BANKNIFTY_RANGE_POINTS=500
OPTION_CHAIN_MIDCPNIFTY_RANGE_POINTS=200
OPTION_CHAIN_SENSEX_RANGE_POINTS=500
EMA_WS_BASE_URL=wss://ema.novag7.in
OPTION_CHAIN_SUPERTREND_ENABLED=true
OPTION_CHAIN_SUPERTREND_MAX_CONNECTIONS=120
REQUIRE_TODAY_ONLY_SCOPE=true
```
