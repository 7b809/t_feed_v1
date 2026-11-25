---
name: upstox-gtt-cleanup
description: Safely inspect, cancel untriggered Upstox GTT orders, and exit positions created by triggered GTT entries. Preview and exact-state confirmation required; emits JSON only.
---

# Upstox GTT Cleanup Skill

Use this skill when asked to cancel untriggered GTT orders and square off positions created by already-triggered GTT entries.

## Safety contract

- Never read `.env`, print credentials, or expose access tokens.
- Use the shared Upstox client and `UPSTOX_ACCESS_TOKEN` environment configuration.
- **Preview first.** The first invocation is read-only and emits a JSON plan plus `state_fingerprint`.
- Execute only after the user explicitly approves the exact preview and fingerprint. A general request to create this skill is not approval to trade.
- Re-fetch order book, GTT book, positions and linked trade details before writing. Abort if the fingerprint changes.
- Cancel untriggered active GTT orders.
- For a triggered GTT, cancel its remaining GTT rules first and verify the GTT is terminal before sending a market exit. This prevents a leftover target/stop rule from opening a reverse position.
- Exit only when linked trade fills can be retrieved and their signed net quantity exactly matches the current position for the same instrument and product.
- If a GTT lacks a linked order ID, the fill cannot be retrieved, multiple GTTs map to one position, the position does not reconcile, or GTT cancellation cannot be verified, skip it and explain why in JSON. Never guess.
- Market exits use the opposite transaction side of the signed position. A market order is consequential and requires explicit confirmation.
- Never claim an exit completed merely because an order was accepted. Report order IDs and available verification. Any unresolved/partial state is reported as such.
- This skill does not guarantee cleanup of historical GTTs not returned by Upstox.

## Run

Preview (read-only):
```bash
python3 .agents/skills/upstox-gtt-cleanup/scripts/cleanup_gtt_orders.py
```

Execute only after reviewing the JSON preview and receiving explicit approval of the exact plan:
```bash
python3 .agents/skills/upstox-gtt-cleanup/scripts/cleanup_gtt_orders.py \
  --confirm --expected-state-hash "<state_fingerprint_from_approved_preview>"
```

## JSON contract

Always emit one valid JSON object on stdout. No prose, markdown fences, logging, or credential values. Errors go into JSON and return a non-zero exit code.

- `status`: `preview`, `success`, `partial`, `no_action`, or `error`.
- `operation`: `gtt_cleanup`.
- Preview includes counts, `plan.cancel`, `plan.exit`, `plan.skip`, `state_fingerprint`, and `confirmation_required`.
- Execution includes `cancelled`, `exited`, `failed`, and `skipped`.
- If there are no eligible GTT orders/positions: `status="no_action"`, empty `cancelled` and `exited` arrays, and a clear message.
- Skipped/ambiguous records remain visible; never silently discard them.

Example empty result:
```json
{
  "status": "no_action",
  "operation": "gtt_cleanup",
  "message": "No triggered GTT positions to exit and no untriggered active GTT orders to cancel.",
  "cancelled": [],
  "exited": [],
  "skipped": []
}
```
