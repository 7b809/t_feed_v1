---
name: upstox-margin
description: Fetch Upstox required margin estimates or brokerage charges. Read-only calculation endpoints; does not place orders.
category: Trading Calculations
access: read-only
execution: direct-python
script: scripts/fetch_margin.py
result_schema: schemas/result.schema.json
---

# Upstox Margin and Brokerage

This skill calls calculation endpoints only. `required-margin` uses ChargeApi.post_margin with the exact supplied instrument, quantity, side, product and price. Although the HTTP method is POST, it is a margin calculation and does not place an order. `brokerage` estimates brokerage and taxes. No order placement/modification/cancellation is performed.

```bash
algoai run upstox-margin --result-format json --script-args-json '["--kind","required-margin","--instrument","NSE_FO|<token>","--quantity","<qty>","--transaction-type","BUY","--product","I","--price","<price>"]'
algoai run upstox-margin --result-format json --script-args-json '["--kind","brokerage","--instrument","NSE_EQ|<ISIN>","--quantity","10","--transaction-type","BUY","--product","D","--price","250.00"]'
```

Do not guess instrument keys, quantity, side, product or price. The response retains the complete raw SDK response. Never read `.env` or expose credentials.
