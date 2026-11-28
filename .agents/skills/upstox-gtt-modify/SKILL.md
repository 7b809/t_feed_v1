---
name: upstox-gtt-modify
description: Safely inspect and modify one or more trigger prices (ENTRY, STOPLOSS, and/or TARGET) on an existing Upstox GTT in one modification request while retaining all other GTT rules and settings. Use when the user asks to change an existing GTT order.
category: Orders
access: write-capable
execution: direct-python
script: scripts/modify_gtt.py
result_schema: schemas/result.schema.json
---

# Upstox GTT Modification

Modify one or more selected trigger prices on the same existing GTT order. The skill supports ENTRY, STOPLOSS, and TARGET. Multiple requested legs are included in one complete rules payload and submitted through one GTT modification API call.

## Safety rules

- Never read `.env`, print credentials, or expose access tokens.
- Use the shared Upstox client from `.agents/skills/upstox/scripts/upstox_helpers.py`.
- Never place, cancel, or modify an order without explicit user confirmation.
- First run the script WITHOUT `--confirm`. This fetches the latest order and prints a JSON preview only.
- The preview must show the GTT order ID, every selected leg, each old and requested trigger price, quantity, GTT type, and a full-order `state_fingerprint`.
- Wait for explicit user approval of the exact preview, including the GTT ID, every requested leg/value, and the state fingerprint.
- Only after approval, rerun the same command with `--confirm --expected-state-hash "<state_fingerprint_from_preview>"`.
- Do not infer a GTT order ID, leg, or trigger price. Ask for any missing value.
- Never silently change quantity, product, order type, transaction direction, or any other rule.
- If a selected leg is missing or duplicated, stop without modifying the order.
- If the GTT state differs from the reviewed preview when re-fetched, abort and require a fresh preview.
- Submit all requested trigger changes together in one `modify_gtt_order` API request. Do not split a multi-leg request into separate writes.
- After modification, fetch the order again and verify every requested trigger price. Report verification failure clearly.
- Return valid JSON when requested. Do not claim success unless every requested trigger is verified.

## Supported operations

- `ENTRY`: change the ENTRY trigger price.
- `STOPLOSS`: change the STOPLOSS trigger price.
- `TARGET`: change the TARGET trigger price.
- Any combination of distinct supported legs can be changed together, such as STOPLOSS and TARGET.
- Each selected leg must occur exactly once in the existing GTT.
- Existing unselected rules, quantity, GTT type, and supported per-rule settings are retained.

## Validated JSON execution

The modification handler emits a machine-readable preview/result and is available through:

```bash
algoai run upstox-gtt-modify --allow-write --result-format json --script-args-json '["--gtt-order-id","<gtt_order_id>","--update","TARGET=300"]'
```

The first invocation is preview-only. After reviewing the exact preview, explicitly approve it and rerun with `--confirm --expected-state-hash <state_fingerprint>`. The CLI write permission does not replace this fingerprint-bound confirmation.

## Multi-leg preview

Use one repeatable `--update LEG=PRICE` argument per requested leg:

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/modify_gtt.py \
  --gtt-order-id "<gtt_order_id>" \
  --update TARGET=300 \
  --update STOPLOSS=230 \
  --json
```

The preview returns a `state_fingerprint`. No modification is submitted.

## Execute after explicit confirmation

After the user approves the exact preview, rerun the same command with the exact fingerprint returned by that preview:

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/modify_gtt.py \
  --gtt-order-id "<gtt_order_id>" \
  --update TARGET=300 \
  --update STOPLOSS=230 \
  --confirm \
  --expected-state-hash "<state_fingerprint_from_preview>" \
  --json
```

The script verifies that the current complete GTT state matches the reviewed fingerprint, re-fetches immediately before writing to detect a concurrent change, submits all requested changes in one API call, then re-fetches and verifies every requested leg.

## Backwards-compatible single-leg usage

Existing single-leg syntax remains supported for previews:

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/modify_gtt.py \
  --gtt-order-id "<gtt_order_id>" \
  --leg STOPLOSS \
  --trigger-price 235 \
  --json
```

For confirmed execution, also pass `--confirm` and the `--expected-state-hash` returned by the preview.

## algoai prompt examples

Preview both STOPLOSS and TARGET together:

```bash
algoai --skip-permissions --prompt "Use the upstox-gtt-modify skill to preview modifying the existing GTT order ID [GTT_ORDER_ID]. Change TARGET to [NEW_TARGET_PRICE] and STOPLOSS to [NEW_STOPLOSS_PRICE] together in one GTT modification request. Fetch the latest GTT, validate that both rules exist exactly once, show both old and requested prices, preserve ENTRY, quantity, GTT type, and every other rule, and return valid JSON including the state_fingerprint. Do not execute yet. Do not read .env or expose access tokens."
```

Execute only after approving that exact preview. Use the exact fingerprint returned by the preview:

```bash
algoai --skip-permissions --prompt "I explicitly approve the exact preview for GTT [GTT_ORDER_ID]: TARGET trigger price [NEW_TARGET_PRICE], STOPLOSS trigger price [NEW_STOPLOSS_PRICE], and state_fingerprint [STATE_FINGERPRINT_FROM_PREVIEW]. Use the upstox-gtt-modify skill to submit both changes together in one API modification request using --confirm and --expected-state-hash with that exact fingerprint. Re-fetch immediately before writing and abort if the GTT state changed. Preserve ENTRY, quantity, GTT type, and all other rules. Re-fetch afterward and verify both prices. Return valid JSON only. Do not read .env or expose access tokens."
```

## Inspect an existing GTT

```bash
python3 .agents/skills/upstox-gtt-modify/scripts/get_gtt.py \
  --gtt-order-id "<gtt_order_id>"
```

## Validation helper

`scripts/validate_gtt_modify.py` contains pure validation, multi-leg normalization, rule-preservation, and SHA-256 state-fingerprint helpers. It does not call the API or handle credentials.
