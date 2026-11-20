# Local Upstox Supertrend recovery and live polling

OrderFlow calculates Supertrend locally using the same calculator as
`ema_v1`. For every active isolation it waits for the configured grace
period, fetches historical and today's intraday one-minute candles for both
option legs using Upstox HistoryV3, merges/deduplicates/sorts them, excludes
unfinished bars, and calculates today's confirmed Supertrend signals.

## Per-isolation workflow

1. Mark recovery `PENDING` so remote WebSocket events cannot race the
   initial backfill.
2. Wait until `isolation_time + SUPERTREND_RECOVERY_BUFFER_SECONDS`
   (default 300 seconds / 5 minutes).
3. Fetch previous candles using
   `HistoryV3Api.get_historical_candle_data1` and today's candles using
   `HistoryV3Api.get_intra_day_candle_data`, for both isolated and opposite
   option instruments.
4. Merge by timestamp, sort chronologically and discard unfinished candles.
5. Calculate Supertrend with ATR period 10, multiplier 3.0, Wilder ATR and
   HL2 source by default. The calculation is ported unchanged from
   `ema_v1/app/services/supertrend_calculator.py`.
6. Recover all confirmed signals from the isolation boundary through the
   actual fetch-completion time. The five-minute buffer delays the recovery
   attempt; it is not a five-minute cap on the signals being recovered.
7. Simulate entry at the first available one-minute candle after each
   confirmed signal, then replay OHLC chronologically for target/stop/reversal.
   If one candle touches both stop and target, stop-loss wins (conservative
   OHLC-only assumption). Include the full candle that confirms an opposite
   Supertrend flip before pricing a reversal at that candle's close.
8. If a persisted dummy order is still open when the process restarts, resume
   it from its last safely knowable candle boundary and replay missed candles
   through the recovery time. For a live LTP fill inside a one-minute bar, skip
   that partial bar because OHLC cannot establish whether its extremes occurred
   before or after entry.
9. If no following entry candle or required target-leg history is available,
   keep recovery pending and retry. Never guess an entry or exit price.
10. Once recovery completes, poll today's intraday candles and forward only
   newly confirmed post-recovery signals to the existing order engine.

## Candle timestamps

Upstox timestamps identify candle-open time. A one-minute candle is
confirmed at open time + one minute. The recovery bounds account for this
close boundary so a candle that closes after isolation is eligible, while a
candle that closed before isolation is excluded.

## Safety

- Both option legs are fetched for correct backfill pricing, but only the
  isolated leg's Supertrend events are routed to the order engine.
- Local live entries still require a fresh current-session LTP, valid risk
  levels, current-day isolation, and the existing entry-window/order-cap
  checks.
- Local candle confirmation gets a bounded 120-second age allowance to
  account for candle-open timestamps. Frame receive time and quote freshness
  retain their stricter checks.
- Historical replay and restart catch-up are simulation/backfill only and never
  submit GTT or broker orders. Existing open dummy positions are replayed rather
  than skipped; historical entries always persist broker_order_id=null and
  real_order_submitted=false.
- The original isolation timestamp is read from durable dummy-order state on
  process restart; refreshed Mongo received_at values do not move the recovery
  boundary.
- Confirmed-time identities are checked against persisted orders to prevent
  duplicate processing.
- API failures and missing candles leave recovery pending and are retried;
  no synthetic candle prices are used.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `SUPERTREND_HISTORY_DAYS` | 10 | Previous calendar days requested for ATR warm-up |
| `SUPERTREND_ATR_PERIOD` | 10 | ATR period |
| `SUPERTREND_MULTIPLIER` | 3.0 | Supertrend multiplier |
| `SUPERTREND_CHANGE_ATR` | true | Wilder ATR when true; SMA(TR) when false |
| `SUPERTREND_POLL_SECONDS` | 15 | Local intraday refresh interval |
| `SUPERTREND_RECOVERY_BUFFER_SECONDS` | 300 | Delay after isolation before first recovery fetch |
| `SUPERTREND_FETCH_TIMEOUT_SECONDS` | 25 | Request timeout; historical walkback allows 4x |

All date filtering and timestamps use Asia/Kolkata (IST). The Upstox client
wrapper mirrors the existing `ema_v1` HistoryV3 service.
