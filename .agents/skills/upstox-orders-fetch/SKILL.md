---
name: upstox-orders-fetch
description: Read and filter the user's Upstox normal order book and GTT orders, including lookup of a specific order. Read-only; never places, modifies, or cancels orders.
category: Orders
access: read-only
execution: direct-python
script: scripts/fetch_orders.py
result_schema: schemas/result.schema.json
---

# Upstox Order Fetch

This is a read-only JSON handler. It fetches today's normal order book and/or the GTT orders exposed by Upstox. It preserves the complete SDK response in `raw_response` and includes normalized/filterable order records in `orders`.

## JSON execution

Use explicit script arguments through the CLI:

```bash
algoai run upstox-orders-fetch --result-format json --script-args-json '["--kind","all"]'
algoai run upstox-orders-fetch --result-format json --script-args-json '["--kind","normal","--status","open"]'
algoai run upstox-orders-fetch --result-format json --script-args-json '["--kind","gtt"]'
algoai run upstox-orders-fetch --result-format json --script-args-json '["--order-id","<order_id>"]'
```

Supported arguments: `--kind all|normal|gtt`, `--order-id`, `--status`, `--symbol`, `--tag`, and `--segment`.

Normal `get_order_book` is today's order book, not an unlimited historical archive. Specific normal order details may return multiple lifecycle events; these are retained. A filter matching zero records is a valid empty result.

This skill is read-only. It never places, modifies, or cancels orders. Never read `.env`, print tokens, or expose credentials.
