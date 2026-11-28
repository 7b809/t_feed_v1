---
name: upstox-order
description: Place a live MARKET or LIMIT order on Upstox using UPSTOX_ACCESS_TOKEN from the environment. Never read .env or add extra commentary.
category: Orders
access: write-capable
---

# Upstox Order Execution

## Rules
- Never read `.env` or print tokens/secrets.
- Read `UPSTOX_ACCESS_TOKEN` from environment variables.
- Run `.agents/skills/upstox-order/scripts/place_order.py` once and print ONLY its output.
- Do not make extra API calls unless explicitly requested.

## Usage
- MARKET order: `--order-type MARKET --price 0`
- LIMIT order: `--order-type LIMIT --price <price>`
