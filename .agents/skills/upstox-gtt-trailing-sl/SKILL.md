---
name: upstox-gtt-trailing-sl
description: Enter at live market price and manage a fixed target plus step-trailing stop-loss on Upstox. Use when the user requests market entry with SL distance, target distance and trailing jump.
category: Risk Management
access: write-capable
---

# Upstox Market Entry + Trailing Stop Skill

## Execution model
- Entry is a normal MARKET order. Do not use a GTT ENTRY rule: GTT `IMMEDIATE` is not a market order.
- After the market entry fills, place one broker-side protective SL-M exit.
- Monitor LTP locally for the fixed target. When target is reached, cancel the SL-M, verify it is cancelled, then submit a MARKET exit. Never keep independent stop and target orders live together; they are not assumed to be OCO.
- On each full favourable trailing-jump increment, modify the SL-M trigger by the same number of points. Never loosen the stop.
- This is a locally managed strategy, not broker-native OCO/GTT. The manager must remain running. If it stops, the last SL-M remains at the broker, but target monitoring and further trailing stop.

## Inputs
Instrument key, BUY/SELL, quantity, product (I/D/MTF), SL distance, target distance, trailing jump and instrument tick size. Distances are price points relative to the actual average fill. Resolve the instrument and verify F&O lot size; never guess tokens or lot sizes.

## Safety workflow
1. Validate inputs and resolve the instrument/tick/lot size.
2. Run without `--confirm`. It fetches a reference LTP and displays the complete configuration and a stable SHA-256 hash. The reference LTP is informational; all exit prices use actual average fill.
3. Obtain explicit user approval for the exact instrument, side, quantity, product, distances, tick size and hash.
4. Only then rerun with `--confirm --expected-preview-hash <hash>`. The hash covers the approved inputs and is stable across LTP ticks.
5. Place one MARKET entry. Wait for terminal order status; manage only the actually filled quantity using actual average fill price. If no reliable fill is confirmed, stop and require manual reconciliation.
6. Place SL-M first. The fixed target is monitored locally; when reached, cancel and verify the SL-M before submitting a market exit. If cancellation cannot be verified, do not submit the target exit.
7. Stop management and report immediately on partial exit fills, rejected/cancelled protective SL, API errors, or ambiguous order states. Verify final order status and require manual reconciliation when needed.
8. Never execute live orders during tests. Never read `.env` or print credentials.

## Preview
```bash
python3 .agents/skills/upstox-gtt-trailing-sl/scripts/place_trailing_sl.py \
  --instrument "<instrument_key>" --side BUY --quantity <qty> --product I \
  --sl-distance <points> --target-distance <points> --trailing-jump <points> \
  --tick-size <tick_size>
```

## Execute only after approval of the exact preview hash
```bash
python3 .agents/skills/upstox-gtt-trailing-sl/scripts/place_trailing_sl.py \
  --instrument "<instrument_key>" --side BUY --quantity <qty> --product I \
  --sl-distance <points> --target-distance <points> --trailing-jump <points> \
  --tick-size <tick_size> --confirm --expected-preview-hash "<preview_hash>"
```

Use `--side SELL` for short entries. The script reads `UPSTOX_ACCESS_TOKEN` from the environment. Keep the manager process running throughout the trade.
