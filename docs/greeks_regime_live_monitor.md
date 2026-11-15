# Live Greeks Regime Monitor

## Purpose

This is a separate, read-only task attached to the existing Upstox live-feed
callback. It evaluates CE/PE option ticks only. It does not participate in
dummy-order creation, strategy entry signals, sandbox execution, or broker
order placement.

## Evaluation model

- Portable mode is the default. Fixed local delta/rho bands are calibrated to
  specific contracts and are not applied indiscriminately to every strike or expiry.
- Side is checked against registry metadata (CE/PE) and the formula's delta/rho
  sign rules. Atypical or mismatched readings are ignored.
- Each option keeps one latest valid sample per observed minute. The previous
  minute sample is finalized when the next minute's tick arrives.
- The current tick is compared against the preceding 20 completed minute
  samples. A minimum of 20 prior observations is required.
- The directional condition is z_bull > +0.5 (bullish) or z_bull < -0.5
  (bearish), where z_bull = z_delta for calls and -z_delta for puts.
- Premium movement exactly five minutes earlier is an auxiliary confidence
  check when that minute's sample is available. It does not override the
  directional z-score condition.
- Gamma and theta are preserved but are not triggers. Underlying spot/strike
  moneyness is omitted until a reliable spot mapping is wired into the feed.

## Snapshot storage and deduplication

Events are appended as newline-delimited JSON to:

data/greeks_regime_snapshots_YYYYMMDD.jsonl

The date and minute bucket use the configured market timezone (default
Asia/Kolkata). The first qualifying event for an instrument in a minute is
written; later qualifying ticks for that same instrument/minute are
deduplicated. The daily file is scanned on first use after process startup
so the same minute is not written twice after a restart.

The append is flushed immediately. Set GREEKS_REGIME_FSYNC=true if the
deployment requires an fsync for each event (stronger durability, with
additional disk latency).

## Environment settings

| Setting | Default | Meaning |
|---|---:|---|
| GREEKS_REGIME_ENABLED | true | Enable/disable the monitor |
| GREEKS_REGIME_LOOKBACK_MINUTES | 20 | Completed observations required and retained |
| GREEKS_REGIME_Z_THRESHOLD | 0.5 | Absolute directional z-score threshold |
| GREEKS_REGIME_STD_FLOOR | 0.0001 | Ignore near-zero rolling delta variance |
| GREEKS_REGIME_TIMEZONE | MARKET_TIMEZONE | Minute bucketing timezone |
| GREEKS_REGIME_OUTPUT_DIR | data | Snapshot output directory |
| GREEKS_REGIME_FSYNC | false | Force disk sync after each appended row |

## Operational notes

- A cold process start needs 20 completed minute samples per option before
  portable signals can qualify. No synthetic or backfilled Greeks samples are invented.
- Options without valid CE/PE registry metadata or usable optionGreeks
  (delta and rho) are ignored.
- The JSONL event is an audit snapshot, not a trading instruction.
- Validate the deployed Upstox feed version's optionGreeks payload before
  relying on live results. No broker execution is connected.
