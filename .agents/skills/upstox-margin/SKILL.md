---
name: upstox-margin
description: Fetch Upstox required margin estimates, brokerage charges, or both. Resolves live instrument metadata, validates lot-size multiples, and defaults to one lot and live LTP.
category: Trading Calculations
access: read-only
execution: direct-python
script: scripts/fetch_margin.py
result_schema: schemas/result.schema.json
---

# Upstox Margin and Brokerage

This skill calls calculation and quote/metadata endpoints only. It never places, modifies, or cancels orders.

## Defaults

Only `--instrument` is mandatory. If omitted, the handler applies these defaults:

| Input | Default | Behaviour |
|---|---|---|
| `--kind` | `both` | Calculate margin and brokerage |
| `--lots` / `--quantity` | `1` lot | Resolve exact lot size from Upstox instrument metadata and convert one lot to units |
| `--price` | `market` | Fetch one live LTP snapshot and use it for both calculations |
| `--product` | `I` | Intraday |
| `--transaction-type` | `BUY` | Buy |

These defaults are conveniences, not a substitute for reviewing the request. In particular, BUY and intraday are assumptions: explicitly supply `--transaction-type SELL` or `--product D` when appropriate. The instrument key is never guessed.

## Simplest command (one lot, BUY, intraday, live price)

```bash
algoai run upstox-margin --result-format json --script-args-json '["--instrument","NSE_FO|<instrument-token>"]'
```

## Explicit number of lots

```bash
algoai run upstox-margin --result-format json --script-args-json '["--instrument","NSE_FO|<instrument-token>","--lots","1","--transaction-type","BUY","--product","I","--price","market"]'
```

## Explicit unit quantity

```bash
algoai run upstox-margin --result-format json --script-args-json '["--instrument","NSE_FO|<instrument-token>","--quantity","<units>","--transaction-type","BUY","--product","I","--price","market"]'
```

For F&O, explicit `--quantity` must be a positive multiple of the exact contract lot size returned by Upstox Instrument Search. `--lots N` is converted to `N × lot_size`. Invalid quantities are rejected before margin/brokerage calculation, with the actual lot size and valid nearby quantities shown. Lot sizes are looked up for the exact instrument key; NIFTY and SENSEX lot sizes are not hardcoded because they can change.

## Price behaviour

- Omitted `--price` or `--price market`: use live LTP from Upstox.
- Numeric `--price`: use the supplied price for both calculations.
- One LTP snapshot is reused in combined mode, so margin and brokerage use the same price.

The live LTP is an estimate reference, not a guaranteed fill price or final charge.

## Response

The JSON includes resolved `instrument_metadata` (including `lot_size`), normalized quantity, `quantity_source`, resolved price and `price_source`, both calculation results, complete raw API responses, and component-specific errors. If one calculation succeeds and the other fails, status is `partial`.

Never guess an instrument key. Never read `.env` or expose credentials.
