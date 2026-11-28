---
name: upstox-gtt-variants
description: Place Upstox GTT orders with IMMEDIATE or ABOVE/BELOW entry triggers and optional SL only, Target only, or both SL and Target using UPSTOX_ACCESS_TOKEN from the environment. Never read .env.
category: Orders
access: write-capable
---

# Upstox GTT Variants Skill

## Rules
- Never read `.env` or print secrets/tokens.
- Read `UPSTOX_ACCESS_TOKEN` directly from environment variables.
- Run `.agents/skills/upstox-gtt-variants/scripts/place_gtt_variant.py` once and print ONLY its output.
- Do not make extra API calls unless explicitly asked.

## Trigger & Leg Mapping
- Immediate / market-style entry: `--entry-trigger-type IMMEDIATE`
- Price-level entry: `--entry-trigger-type ABOVE` or `BELOW` with `--entry-price <price>`
- Optional SL leg: `--sl-price <sl>`
- Optional Target leg: `--target-price <target>`
- Omits omitted legs automatically (`SINGLE` for 1 rule, `MULTIPLE` for 2–3 rules).
