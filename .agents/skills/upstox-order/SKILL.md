---
name: upstox-order
description: Preview and explicitly confirm a live MARKET or LIMIT order on Upstox using UPSTOX_ACCESS_TOKEN from the environment.
category: Orders
access: write-capable
execution: direct-python
script: scripts/place_order.py
result_schema: schemas/result.schema.json
---

# Upstox Normal Order Placement

## Mandatory two-step safety workflow

The handler is preview-only by default. It never sends an order unless both `--confirm` and the exact `--expected-preview-hash` from the reviewed preview are supplied.

1. Resolve/verify the instrument key and lot size. Do not guess derivative tokens.
2. Run the handler without `--confirm`. Review the full order and `preview_hash`.
3. Obtain explicit user approval of the exact instrument, side, quantity, product, order type, price, and hash.
4. Execute the same arguments with `--confirm --expected-preview-hash <exact_hash>`.
5. Report the broker API response; do not claim a fill from an order-accepted response.

## JSON execution

Preview:
```bash
algoai run upstox-order --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","<qty>","--order-type","LIMIT","--price","<price>","--transaction-type","BUY","--product","D"]'
```

After explicit approval of that exact preview:
```bash
algoai run upstox-order --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","<qty>","--order-type","LIMIT","--price","<price>","--transaction-type","BUY","--product","D","--confirm","--expected-preview-hash","<preview_hash>"]'
```

MARKET orders use `--order-type MARKET --price 0`. LIMIT orders require a positive price. The script validates inputs and rounds LIMIT price to the configured 0.05 tick; inspect the rounded value in the preview.

Never read `.env`, print tokens, or treat `--skip-permissions` / `--allow-write` as trade approval. The fingerprint-bound confirmation is mandatory.
