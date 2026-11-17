# Supertrend startup recovery and daily target lock

When the application starts after the configured live-feed session has begun, the Supertrend engine attempts a recovery pass when the isolated-leg snapshot arrives.

## Replay inputs
- Confirmed Supertrend signals in the upstream snapshot, filtered to the isolation time through application startup.
- Persisted one-minute OHLC candles for the isolated and opposite option legs, under `LIVE_FEED_STATE_DIR`.

The simulated entry is the open of the first persisted one-minute candle after the signal confirmation time. The same configured stop-loss percentage and entry-price target bands are used as for live dummy orders.

## Conservative handling
- If a candle touches both stop and target, stop-loss is assumed first because OHLC bars do not reveal the intrabar path.
- A reversal closes at the last completed candle close before the opposite signal.
- A still-open recovered order is restored for subsequent live LTP monitoring.
- A target hit during recovery closes that order and sets `supertrend_daily_target_locked=true` for that isolation. Further Supertrend entries are blocked for that isolation for the rest of the session.
- A stop-loss does not activate the daily target lock; later confirmed signals may create another simulated order.

## P&L
P&L is reported in INR as price movement multiplied by configured quantity and lot size when present in isolation metadata. If lot size is absent, it defaults to 1. Estimated charges and slippage are not included; `pnl_cost_model` marks results as gross-only.

## Data limitation
Signals alone cannot reconstruct historical fills. If persisted one-minute candles do not cover the required period, recovery becomes `WAITING_FOR_HISTORY` and new Supertrend entries are paused rather than assigning guessed prices. This repository does not currently include an external historical-candle API client. An already-persisted open dummy order is preserved rather than replayed again; its downtime price gap is not reconciled by this pass.

Simulation only: no broker order is submitted.
