---
name: upstox-gtt-modify
description: Safely inspect and modify one trigger price (ENTRY, STOPLOSS, or TARGET) on an existing Upstox GTT order while retaining all other GTT rules and settings. Use when the user asks to change an existing GTT order.
---

# Upstox GTT Modification

Modify one selected trigger price on an existing GTT order. This skill supports ENTRY, STOPLOSS, and TARGET.

## Safety rules

- Never read `.env`, print credentials, or expose access tokens.
- Use the shared Upstox client from `.agents/skills/upstox/scripts/upstox_helpers.py`.
- Never place, cancel, or modify an order without explicit user confirmation.
- First run the script WITHOUT `--confirm`. This fetches the latest order and prints a preview only.
- Show the user the GTT order ID, selected leg, old value, requested value, quantity, and confirmation that other rules will be retained.
- Only after the user explicitly approves the exact preview, rerun the same command with `--confirm`.
- Do not infer a GTT order ID, leg, or trigger price. Ask for any missing value.
- Never silently change quantity, product, order type, transaction direction, or any other rule.
- If the selected leg is missing or duplicated, stop without modifying the order.
- After modification, the script fetches the order again and verifies the requested trigger price. Report verification failure clearly.

## Supported operations

- `ENTRY`: change only the ENTRY trigger price.
- `STOPLOSS`: change only the STOPLOSS trigger price.
- `TARGET`: change only the TARGET trigger price.

Each request changes exactly one leg. Existing other rules, quantity, and GTT type are retained.

## Preview command

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/modify_gtt.py \
  --gtt-order-id "<gtt_order_id>" \
  --leg STOPLOSS \
  --trigger-price 235
```

## Execute after explicit confirmation

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/modify_gtt.py \
  --gtt-order-id "<gtt_order_id>" \
  --leg STOPLOSS \
  --trigger-price 235 \
  --confirm
```

Replace STOPLOSS with ENTRY or TARGET as requested. The preview and execution must use the same order ID, leg, and trigger price.

## Inspect an existing GTT

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/get_gtt.py \
  --gtt-order-id "<gtt_order_id>"
```

## Validation helper

`scripts/validate_gtt_modify.py` contains pure validation and rule-preservation helpers. It does not call the API or handle credentials.
