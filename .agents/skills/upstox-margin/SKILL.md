---
name: upstox-margin
description: Fetch Upstox required margin estimates, brokerage charges, or both using a supplied price or one live LTP snapshot. Read-only calculation endpoints.
category: Trading Calculations
access: read-only
execution: direct-python
script: scripts/fetch_margin.py
result_schema: schemas/result.schema.json
---

# Upstox Margin and Brokerage

This skill calls calculation endpoints only. It never places, modifies, or cancels orders.

## Combined live-market-price calculation

Use `--kind both --price market` to fetch the instrument's LTP once, then use that same price for both:

- `ChargeApi.post_margin()` to calculate required/final margin.
- `ChargeApi.get_brokerage()` to calculate brokerage and taxes.

Example:

```bash
algoai run upstox-margin \
  --result-format json \
  --script-args-json '["--kind","both","--instrument","NSE_FO|47838","--quantity","1","--transaction-type","BUY","--product","D","--price","market"]'
```

The response includes `request.price`, `request.price_source` (`LIVE_LTP`), `data.margin`, `data.brokerage`, and complete raw API responses. If one calculation fails while the other succeeds, the status is `partial` and the error identifies the failed component.

## Individual calculations

Required margin:

```bash
algoai run upstox-margin --result-format json --script-args-json '["--kind","required-margin","--instrument","NSE_FO|<token>","--quantity","<qty>","--transaction-type","BUY","--product","I","--price","<price>"]'
```

Brokerage:

```bash
algoai run upstox-margin --result-format json --script-args-json '["--kind","brokerage","--instrument","NSE_EQ|<ISIN>","--quantity","10","--transaction-type","BUY","--product","D","--price","250.00"]'
```

Use a numeric `--price` for a specified price or `market` to fetch LTP. A single LTP snapshot is used for both calculations in combined mode; it is an estimate, not a guaranteed execution price or final exchange/broker charge.

Do not guess instrument keys, quantity, side or product. The response retains complete raw SDK responses. Never read `.env` or expose credentials.
