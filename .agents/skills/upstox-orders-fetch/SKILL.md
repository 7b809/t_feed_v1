---
name: upstox-orders-fetch
description: Read and filter the user's Upstox normal order book and GTT orders, including lookup of a specific normal order ID or GTT order ID. Read-only; never places, modifies, or cancels orders.
category: Orders
access: read-only
---

# Upstox Order Fetch

Use this skill whenever the user asks to view/list/fetch normal orders, GTT orders, all orders, or a specific order.

## Supported data

- Normal orders: today's order book via `OrderApi.get_order_book(api_version="2.0")`.
- Specific normal order: lifecycle details via `OrderApi.get_order_details(api_version="2.0", order_id=...)`.
- GTT orders: all GTT orders via `OrderApiV3.get_gtt_order_details()`.
- Specific GTT: `OrderApiV3.get_gtt_order_details(gtt_order_id=...)`.

## Filters

- `--kind all|normal|gtt` (default: all).
- `--order-id`: fetch a specific order. In `all` mode both normal and GTT endpoints are checked.
- `--status`: exact status filter (case-insensitive).
- `--symbol`: partial trading-symbol or instrument-key filter.
- `--tag`: exact tag filter for normal orders.
- `--segment`: exchange/segment prefix filter, e.g. `NSE_FO`.

## Execution

Fetch all normal and GTT orders:

```bash
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind all
```

Fetch normal orders only:

```bash
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind normal
```

Fetch GTT orders only:

```bash
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind gtt
```

Fetch a specific order ID (checks both normal and GTT order APIs):

```bash
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --order-id "<order_id>"
```

Examples of filters:

```bash
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind normal --status open
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind all --symbol "NIFTY"
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind normal --tag "intraday"
python3 .agents/skills/upstox-orders-fetch/scripts/fetch_orders.py --kind all --segment NSE_FO
```

## Safety and interpretation

- This module is strictly read-only. It does not place, modify, or cancel orders.
- Never read `.env`, print tokens, or expose credentials.
- Use the shared client in `.agents/skills/upstox/scripts/upstox_helpers.py`.
- Do not claim that "all historical orders" are returned: normal `get_order_book` is the current day's order book; GTT listing returns the GTT orders exposed by Upstox.
- Specific normal order details may return multiple lifecycle events for one order; preserve them rather than flattening away status history.
- A filter returning zero records is a valid empty result, not proof that the order never existed.
- The `all` mode attempts both order namespaces and reports an error only if neither returns usable data.
