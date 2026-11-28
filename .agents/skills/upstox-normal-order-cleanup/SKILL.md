---
name: upstox-normal-order-cleanup
description: Safely cancel pending Upstox normal orders and exit open positions exactly reconciled to today's normal order trades. Preview and exact-state confirmation required; emits JSON only.
category: Risk Management
access: write-capable
---

# Upstox Normal Order Cleanup Skill

Use this skill when asked to cancel pending normal orders and square off positions resulting from normal orders.

## Safety contract

- Never read `.env`, print credentials, or expose access tokens.
- Use the shared Upstox client and configured environment token.
- The normal order book is today's order book, not an unlimited historical archive.
- **Preview first.** Preview is read-only and emits a JSON plan plus `state_fingerprint`.
- Execute only after explicit approval of the exact preview and fingerprint.
- Re-fetch normal orders, GTT orders, positions and trade history before writing; abort if the state changed.
- Cancel only recognized pending normal order states (`open`, `trigger pending`, `pending`). Terminal orders are left alone; unknown states are skipped.
- Exit only positions whose signed today's trade quantity exactly equals the current signed position quantity for the same instrument and product.
- Do not exit a position while an active GTT or pending normal order exists for the same instrument/product. Use the GTT cleanup skill first where relevant.
- After cancellation, refresh state and reconcile again before any exit.
- Carry-forward positions, external/manual trades, incomplete trade history, partial fills, and ambiguous product/instrument matches are skipped rather than guessed.
- Opposite market exit side is derived from signed net position. Require explicit confirmation for the preview.
- Report market order IDs and status as accepted/unverified unless a subsequent order-status check confirms the lifecycle state. Never describe order acceptance as a completed square-off.

## Run

Preview (read-only):
```bash
python3 .agents/skills/upstox-normal-order-cleanup/scripts/cleanup_normal_orders.py
```

Execute only after explicit approval of the exact preview:
```bash
python3 .agents/skills/upstox-normal-order-cleanup/scripts/cleanup_normal_orders.py \
  --confirm --expected-state-hash "<state_fingerprint_from_approved_preview>"
```

## JSON contract

Always emit one valid JSON object on stdout, without prose or markdown fences. Errors are represented as JSON.

- `status`: `preview`, `success`, `partial`, `no_action`, or `error`.
- `operation`: `normal_order_cleanup`.
- Preview contains order count, plan, fingerprint and confirmation requirement.
- Execution contains `cancelled`, `exited`, `failed`, and `skipped`.
- When there is nothing eligible, return `status="no_action"` with empty action arrays and a clear message.

Example empty result:
```json
{
  "status": "no_action",
  "operation": "normal_order_cleanup",
  "message": "No pending normal orders to cancel and no reconciled open positions to exit.",
  "cancelled": [],
  "exited": [],
  "skipped": []
}
```
