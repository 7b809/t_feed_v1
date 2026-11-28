---
name: upstox-gtt
description: Place a GTT order on Upstox using UPSTOX_ACCESS_TOKEN from the environment. Use when the user asks to place a GTT order.
category: Orders
access: write-capable
---

# Upstox GTT Order Skill

## Rules
- Never read `.env` or print secrets.
- Read `UPSTOX_ACCESS_TOKEN` directly from environment variables.
- Run a single script execution to place the GTT order and print only the API response.

## Execution
Run:
```bash
python3 .agents/skills/upstox-gtt/scripts/place_gtt.py \
  --instrument "<instrument_key>" \
  --quantity <qty> \
  --entry-trigger <entry_price> \
  --sl-trigger <sl_price> \
  --target-trigger <target_price>
