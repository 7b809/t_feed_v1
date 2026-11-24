---
name: upstox-gtt-trailing-sl
description: Enter at live market price and manage fixed-target plus step-trailing stop-loss exits on Upstox. Use when the user requests a market entry with SL distance, target distance and trailing jump.
---

# Upstox Market Entry + Trailing Stop Skill

## Important execution model
- This workflow intentionally uses a normal MARKET entry, not a GTT ENTRY rule.
- Upstox GTT requires an ENTRY rule. Its IMMEDIATE trigger sends a LIMIT order at the supplied trigger price; it is not a market order. Do not use GTT IMMEDIATE as a substitute for the requested live market entry.
- After the market entry fills, this script places a normal protective SL-M order and a fixed TARGET LIMIT order. It then monitors LTP and modifies the SL-M trigger in favourable, discrete trailing-jump increments.
- This is a locally managed bracket, not a broker-native OCO/GTT. The manager process must remain running. The broker-side initial SL and target remain active if the process stops, but trailing adjustments and sibling cancellation will stop. Never imply that trailing continues after the manager exits.

## Required inputs
- Instrument key (resolve it; never guess derivative tokens)
- BUY or SELL
- Quantity (F&O quantity must satisfy exchange lot-size rules)
- Product: I, D or MTF
- Stop-loss distance (> 0)
- Target distance (> 0)
- Trailing jump (> 0)
- Tick size for the instrument

Distances and trailing jump are price points, not absolute trigger prices.

## Mandatory safety workflow
1. Validate all inputs and resolve/verify instrument and lot size before execution.
2. Run the script without `--confirm`. It fetches the current LTP and prints the full strategy preview plus a SHA-256 preview hash. The displayed LTP is informational only; actual levels are calculated from the market order's average fill price.
3. Show the preview to the user and obtain explicit approval for the exact instrument, side, quantity, product, distances, trailing jump, and preview hash.
4. Only then rerun with `--confirm --expected-preview-hash <hash>`. A mismatch aborts before placing the entry.
5. The script places one MARKET entry, waits for its terminal state, uses actual filled quantity and average fill price, and places the initial protective SL-M and fixed target LIMIT exits.
6. It monitors order status and LTP. On each full trailing-jump increment in the favourable direction, it moves the stop trigger by the same number of points. The stop must never move backwards. The target remains fixed.
7. When either exit completes, it cancels the sibling and verifies both statuses. Partial exit fills or API errors stop automated management and are reported for immediate manual reconciliation.
8. Never place a live order during tests. Tests must use pure calculations/mocks only.

## Execution
Preview:
```bash
python3 .agents/skills/upstox-gtt-trailing-sl/scripts/place_trailing_sl.py \
  --instrument "<instrument_key>" --side BUY --quantity <qty> --product I \
  --sl-distance <points> --target-distance <points> --trailing-jump <points> \
  --tick-size <tick_size>
```

After the exact preview is approved:
```bash
python3 .agents/skills/upstox-gtt-trailing-sl/scripts/place_trailing_sl.py \
  --instrument "<instrument_key>" --side BUY --quantity <qty> --product I \
  --sl-distance <points> --target-distance <points> --trailing-jump <points> \
  --tick-size <tick_size> --confirm --expected-preview-hash "<preview_hash>"
```

Use `--side SELL` for a short entry. The script reads only `UPSTOX_ACCESS_TOKEN` from the environment. Never read `.env`, print tokens, or submit an order without the exact confirmation hash.
