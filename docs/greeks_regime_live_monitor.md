# Live Greeks Regime Monitor

## Purpose and safety boundary

This is a separate, read-only task attached to the existing Upstox live-feed
callback. It evaluates option contracts independently and appends audit
snapshots. It does not participate in strategy signals, dummy-order creation,
sandbox execution, or broker order placement.

**Required input: Option Full Feed only.** A monitor-eligible tick must be
parsed from the `oc.ltpc` and `oc.optionGreeks` objects in the option feed.
LTPC-only updates are rejected by an explicit `option_full_feed` marker.
Index feeds and any alternate Greeks locations are not eligible.

Twin-leg confirmation is not part of this change. Each CE or PE is evaluated
independently. Same-strike/same-expiry CE/PE alignment and put-call delta
parity validation remain a separate follow-up enhancement.

## Option Full Feed fields

The parser retains these fields from `oc.ltpc`:
- `ltp`: option last traded price
- `cp`: previous close
- `ltt`: exchange last-traded timestamp

The parser retains these fields from `oc.optionGreeks`:
- Core Greeks: `delta`, `theta`, `gamma`, `vega`, `rho`
- `up`: underlying spot price (S)
- `iv`: implied volatility
- `op`: option price from the Greeks payload

The instrument registry supplies strike price (K), strike type, expiry and
instrument identity. True moneyness is calculated as `m = up / strike_price`
when S and K are finite and positive.

## Single-option evaluation

- Portable mode is used; contract-local delta/rho thresholds are not applied
  across all strikes and expiries.
- Side is detected from delta/rho and checked against registry CE/PE metadata:
  CALL when delta > 0.20 and rho > 0.5; PUT when delta < -0.20 and rho < -0.5.
- Each option maintains its own rolling baseline of the last 20 completed
  minute observations by default. The preceding minute is finalized when a
  tick from the next minute arrives.
- `z_delta = (delta - rolling_mean) / rolling_std`.
- `z_bull = z_delta` for CALL and `-z_delta` for PUT.
- When S/K is available, verdict classification follows formula §4.4:
  * `m > 1.02` and `z_bull > 0`: BULLISH
  * `m < 0.98` and `z_bull < 0`: BEARISH
  * `m > 1.02` without positive z_bull: LEAN_BULLISH
  * `m < 0.98` without negative z_bull: LEAN_BEARISH
  * Otherwise: NEUTRAL
- Without S/K, the portable fallback is BULLISH when `z_bull > +0.5`,
  BEARISH when `z_bull < -0.5`, otherwise NEUTRAL.
- Composite score with moneyness:
  `50 + 25*tanh(z_bull / 1.0) + 25*tanh((m - 1.0) / 0.05)`.
  Without moneyness:
  `50 + 50*tanh(z_bull / 1.0)`.
- Score above 65 or below 35 is HIGH confidence; 45–55 is LOW; otherwise
  MEDIUM. The score is a confidence measure and does not replace the §4.4
  portable verdict.
- Premium movement five minutes earlier is an auxiliary cross-check.
  A contradictory non-flat premium move subtracts 15 score points. It does
  not change the verdict.
- Gamma and theta are retained for audit but are not triggers.

## Snapshot persistence

Events are appended as newline-delimited JSON to:

`data/greeks_regime_snapshots_YYYYMMDD.jsonl`

The configured output directory defaults to `LIVE_DATA_DIR`, which is
`data` under the project root unless overridden by
`GREEKS_REGIME_OUTPUT_DIR`. The configured market timezone (default
Asia/Kolkata) determines the date and minute bucket. The first qualifying
event for an instrument/minute is written; later events for that option/minute
are deduplicated. The daily file is scanned after startup to restore the
deduplication index.

Each append is flushed immediately. Set `GREEKS_REGIME_FSYNC=true` to request
an fsync after each appended row.

## Environment settings

| Setting | Default | Meaning |
|---|---:|---|
| GREEKS_REGIME_ENABLED | true | Enable/disable the monitor |
| GREEKS_REGIME_LOOKBACK_MINUTES | 20 | Completed observations required and retained |
| GREEKS_REGIME_Z_THRESHOLD | 0.5 | Delta-only directional threshold |
| GREEKS_REGIME_STD_FLOOR | 0.0001 | Ignore near-zero rolling delta variance |
| GREEKS_REGIME_TIMEZONE | MARKET_TIMEZONE | Minute bucketing timezone |
| GREEKS_REGIME_OUTPUT_DIR | LIVE_DATA_DIR | Snapshot output directory |
| GREEKS_REGIME_FSYNC | false | Force disk sync after each appended row |

## Operational notes

- A cold process start needs 20 completed minute samples per option before
  portable evaluation can qualify.
- Options without the Option Full Feed shape, CE/PE registry metadata, valid
  delta/rho, or a positive option premium are ignored.
- Monitor status reports `required_feed_source=OPTION_FULL` and
  `twin_leg_confirmation=false`.
- This change does not pair CE/PE legs or check delta parity.
- A JSONL event is an audit snapshot, not a trading instruction. No broker
  execution is connected.
