# Real-Time Trading Signal — Strategy & Order Execution Guide

> **Repository:** `7b809/t_feed_v1`  
> **Branch documented:** `market-engine-v4-unified-path-a`  
> **Timezone:** Asia/Kolkata (IST)  
> **Document purpose:** Explain the isolation pipeline, instrument selection, S/R confirmation, order routing, entry/stop/target calculations, exit rules, replay behaviour and persisted dummy-order records.
>
> This guide describes the code and strategy currently represented by this branch. It is an engineering description, not a recommendation to trade. Sample values are illustrative; actual prices, instrument availability, fills, costs and market behaviour will differ.

---

## 1. System overview

The project is an event-driven market signal and dummy-order engine. Its standard-session flow is:

1. Load today's opening-range (OR) levels and filtered option-instrument universe.
2. Monitor four index instruments for an R3/R3-threshold or S3/S3-threshold touch.
3. Resolve simultaneous touches using configured index priority.
4. Select a NIFTY option candidate and isolate it.
5. Capture and persist the isolation snapshot, including the immutable execution mode.
6. Monitor the isolated option for a new post-isolation 15/3 Support or Resistance confirmation.
7. Route the first qualifying confirmation to the same or opposite option leg.
8. Resolve the entry from the routed instrument's 1-minute confirmation candle close.
9. Create one dummy order and manage its stop, target and exit lifecycle.
10. Persist order state and publish transitions to the optional downstream sandbox executor.

The standard pre-11:30 strategy is called **unified Path A**. The index that caused isolation is retained as diagnostic metadata; it does not select a different standard-session order strategy.

POST-11:30 is a separate strategy branch. It is not the same as the standard Path A or Early Extreme Reversal target ladder.

### Main code areas

| Area | Main responsibility |
|---|---|
| `services/level_isolated_instrument_service.py` | Index touch detection, arbitration, candidate selection, isolation snapshot and replay |
| `services/live_sr_levels_service.py` | Instrument candle stream and confirmed S/R levels |
| `services/isolated_sr_tracker_service.py` | Pre-/post-isolation S/R tracking and sidecar state |
| `services/dummy_order_service.py` | Standard dummy-order creation, entry pricing, stop/target replay, closing and persistence |
| `services/strategy_entry_signal_service.py` | Strategy signal calculation, including the separate POST-11:30 signal path |
| `services/order_runtime_service.py` | Runtime tracking used by the POST-11:30 execution branch |
| `services/sandbox_execution_service.py` | Optional downstream sandbox execution from persisted order transitions |
| `core/config.py` | Environment-backed strategy and service configuration |

---

## 2. Market session and time conventions

The isolation service uses IST (`Asia/Kolkata`) and defines:

- Market open: **09:15 IST**
- Market close for isolation touch scanning: **15:30 IST**
- Valid weekdays: **Monday–Friday**
- OR readiness gate: **09:17:10 IST**

The dummy-order service has a separate EOD close default of **15:35 IST**, controlled by `DUMMY_ORDER_EOD_HOUR` and `DUMMY_ORDER_EOD_MINUTE`. Do not confuse the 15:30 isolation/market-close boundary with the 15:35 dummy-order EOD cutoff.

Timestamps used for strategy decisions are market-local and are persisted as ISO timestamps. In particular, the isolation touch time—not the process startup time—is the reference for deciding whether a later S/R pivot and confirmation are post-isolation.

---

## 3. Opening Range (OR) levels

The opening candle is the 09:15 one-minute candle. Its high and low define the first structural range.

Let:

- `H` = first candle high
- `L` = first candle low
- `D = H - L`
- `A = (H + L) / 2`

The current OR level calculation is:

| Level | Calculation |
|---|---|
| R1 | `H` |
| S1 | `L` |
| A | `(H + L) / 2` |
| R2 | `H + D/2` |
| R3 | `H + D` |
| R3 threshold | `(R2 + R3) / 2` |
| S2 | `L - D/2` |
| S3 | `L - D` |
| S3 threshold | `(S2 + S3) / 2` |

The OR grid is used by the **index isolation trigger**. It is not used as an entry eligibility filter or as a target/stop grid for the current unified standard order engine. An option entry is not rejected simply because its price is above R2, below S2, or outside a former OR zone.

### Illustrative OR calculation

Suppose the 09:15 candle has:

- High (H=200)
- Low (L=160)
- Range (D=40)
- Midpoint (A=180)

Then:

- R2 = 200 + 20 = 220
- R3 = 200 + 40 = 240
- R3 threshold = 230
- S2 = 160 − 20 = 140
- S3 = 160 − 40 = 120
- S3 threshold = 130

These are example numbers only.

---

## 4. Isolation trigger detection

### 4.1 Monitored indices and priority

The service monitors these index instruments in configured priority order:

| Priority order | Canonical index | Instrument key |
|---:|---|---|
| 1 | MIDCPNIFTY | `NSE_INDEX|NIFTY MID SELECT` |
| 2 | BANKNIFTY | `NSE_INDEX|Nifty Bank` |
| 3 | NIFTY | `NSE_INDEX|Nifty 50` |
| 4 | SENSEX | `BSE_INDEX|SENSEX` |

The priority values are read from `STRATEGY_INDEX_PRIORITY` in configuration; the table shows the branch's documented default ordering.

### 4.2 Trigger sides

The service checks the following OR levels:

- **R3-side trigger:** R3 or R3 threshold. A touch occurs when index LTP is greater than or equal to the selected level.
- **S3-side trigger:** S3 or S3 threshold. A touch occurs when index LTP is less than or equal to the selected level.

Each canonical-index/side combination is guarded by a touch-state key, for example `NIFTY_R3` or `SENSEX_S3`, so the same touch is not repeatedly processed.

### 4.3 Simultaneous touch arbitration

If multiple monitored indices touch during one poll, the service sorts them by configured priority and chooses the highest-priority touch as the winner. The other touches are retained as simultaneous-touch diagnostic metadata. The first winning isolation locks the daily isolation state; subsequent polling does not create a second isolation while one is already active.

### 4.4 Live versus backfill detection

The service has both a live touch path and a historical backfill/replay path:

- Live detection uses the latest LTP cache.
- Backfill replay walks the intraday index candles in chronological minute order. R3 touches are checked against candle high; S3 touches are checked against candle low.
- The same winning-touch handler is used so the core selection and routing metadata are consistent.
- Fields that describe capture origin or live quote versus replay candle are diagnostic and must not be used as the strategy mode selector.

---

## 5. Option candidate selection and isolation

### 5.1 NIFTY option universe

After an index wins the touch arbitration, the service resolves the routed NIFTY spot reference and selects from the filtered NIFTY option universe.

The default directional bias is:

| Winning index trigger | Default bias side |
|---|---|
| R3 / R3 threshold | CE |
| S3 / S3 threshold | PE |

The corresponding `STRATEGY_RELATIONAL_R3_BIAS` and `STRATEGY_RELATIONAL_S3_BIAS` configuration values can override these defaults when valid.

The candidate list is filtered toward the option instruments associated with the matching trigger-level group when those candidates are available. The selected bias candidate is the one whose strike is nearest to the routed NIFTY LTP. The service can also collect counter-side candidates for visibility and later routing when the relevant crossover/register flags are enabled. The candidate display list does not itself determine the final order route.

### 5.2 What gets isolated

The selected candidate becomes the official isolated instrument. The isolation record stores, among other fields:

- Instrument key, trading symbol, strike, option type and expiry.
- Trigger index, trigger side, trigger level and trigger value.
- Trigger LTP, trigger priority and simultaneous touches.
- Routed index and routed NIFTY reference LTP/source.
- Isolation touch time.
- Isolated option LTP snapshot and quote source.
- First-candle/OR snapshot metadata.
- Pinned standard execution mode and the price/time used to decide it.

The isolation service registers the bias instrument with the live S/R engine and, where available, also resolves/registers the counter-side instrument. It seeds today's candles so later confirmation-candle prices can be resolved consistently. Feed subscription requests are best-effort.

### 5.3 Isolation is a once-per-day decision

The isolation service will not keep switching the selected instrument every time another index level is touched. The first winning isolation is persisted and used by the order engine for that session/day. Resetting isolation is an explicit state operation, not a normal market update.

---

## 6. Execution mode pinned at isolation

The execution mode is decided once from the official isolation timestamp and the isolated option's price snapshot. It is not recalculated from a later pivot, confirmation candle, or current LTP.

### 6.1 Early Extreme Reversal activation

The isolation is marked `EARLY_EXTREME_REVERSAL` only when **both** conditions are true:

1. Isolation touch time is strictly before **10:30:00 IST**.
2. The isolated option price snapshot is strictly greater than **₹250** or strictly less than **₹100**.

Boundary examples:

| Isolation time | Snapshot price | Mode |
|---|---:|---|
| 09:50 | ₹308.35 | EARLY_EXTREME_REVERSAL |
| 09:50 | ₹99.50 | EARLY_EXTREME_REVERSAL |
| 09:50 | ₹250.00 | STANDARD |
| 09:50 | ₹100.00 | STANDARD |
| 10:30:00 | ₹308.35 | STANDARD |
| 10:31 | ₹80.00 | STANDARD |

The mode and source values are persisted as:

- `standard_execution_mode`
- `early_extreme_reversal`
- `execution_mode_price`
- `execution_mode_decided_at`

Existing isolation records without these fields are treated as STANDARD by the order router.

---

## 7. Post-isolation Support/Resistance confirmation

The standard order engine waits for the first qualified 15/3 S/R level on the isolated instrument.

A level qualifies only when both timestamps are strictly later than the isolation timestamp:

[
pivot\_time > T_{iso}
\quad\text{and}\quad
confirmed\_time > T_{iso}
]

The service sorts confirmed levels by confirmation time and selects the first qualifying one. The pivot time and confirmation time are both retained in the trigger record.

The S/R confirmation belongs to the **isolated instrument**. The actual traded instrument can be the isolated instrument or its opposite option leg depending on the pinned mode and trigger side.

The order engine creates at most one standard order from this first qualifying trigger for the active isolation. Later S/R confirmations do not create additional standard orders for the same isolation.

---

## 8. Order routing rules

### 8.1 Standard routing matrix

| Pinned mode | First qualified S/R | Instrument to BUY |
|---|---|---|
| STANDARD | Support | Same isolated option |
| STANDARD | Resistance | Opposite option leg |
| EARLY_EXTREME_REVERSAL | Support | Opposite option leg |
| EARLY_EXTREME_REVERSAL | Resistance | Same isolated option |

Opposite leg means CE ↔ PE at the same underlying and strike, resolved through the instrument registry. If the required opposite instrument cannot be resolved, the router does not create the order on a guessed instrument; it logs and retries on a later scan.

### 8.2 Standard 09:30 backup reversal

For STANDARD mode, the existing backup reversal rule remains available:

- The first post-isolation confirmation must be a Resistance no later than the configured 09:30 cutoff.
- A Support must already have been confirmed on or before the isolation timestamp.
- When both conditions hold, the Resistance is routed to the same isolated instrument.

This backup rule does not override the pinned Early Extreme Reversal routing matrix.

### 8.3 Routing examples

Assume the isolated instrument is NIFTY 24400 CE:

| Mode | Confirmed trigger | Traded instrument |
|---|---|---|
| STANDARD | Support | NIFTY 24400 CE |
| STANDARD | Resistance | NIFTY 24400 PE |
| EARLY_EXTREME_REVERSAL | Support | NIFTY 24400 PE |
| EARLY_EXTREME_REVERSAL | Resistance | NIFTY 24400 CE |

These examples assume the same-strike opposite contract exists and is resolvable in the instrument registry.

---

## 9. Entry-price calculation and dummy-order creation

### 9.1 Entry timestamp

The order's entry timestamp is the S/R level's confirmation timestamp, not its pivot timestamp and not the isolation timestamp.

### 9.2 Entry price

The default entry price is the **routed/traded instrument's one-minute candle close at the confirmation timestamp**.

It is not:

- The isolated option's LTP snapshot.
- The S/R pivot price.
- The trigger index's LTP.
- The traded instrument's later/current LTP.

The service builds a fixed candle series for the traded instrument and resolves the close at the confirmation minute. If the close cannot be found, the order is retried on a later scan. A live-LTP fallback is permitted only when `DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK=true`; this fallback is explicitly non-deterministic across restarts and is logged as such.

### 9.3 Initial stop-loss

For standard Path A orders, the default configured stop percentage is 30% below entry:

[
SL_0 = Entry \times (1 - StopPct)
]

With the default (StopPct=0.30):

[
SL_0 = Entry \times 0.70
]

The configured value is read from `STRATEGY_ORDER_STOP_LOSS_PCT`.

### 9.4 Initial dynamic target

The initial target depends on the entry-price band:

[
Target_1 =
\begin{cases}
Entry + Entry/3, & 0 < Entry \le 100 \\
Entry + Entry/4, & 100 < Entry \le 200 \\
Entry + Entry/5, & Entry > 200
\end{cases}
]

The order stores the target divisor used for the calculation. This is the initial target for both STANDARD and EARLY_EXTREME_REVERSAL orders; the exit handling diverges after this point.

### 9.5 Illustrative order calculation

Assume:

- Isolated option: NIFTY 24400 CE
- Mode: EARLY_EXTREME_REVERSAL
- First qualifying trigger: Resistance confirmed at 09:57 IST
- Traded instrument: same isolated CE
- 09:57 traded-instrument candle close: ₹294.30
- Stop percentage: 30%

Because entry is greater than ₹200:

`Target_1 = 294.30 + (294.30 / 5) = 353.16`

[
SL_0 = 294.30 \times 0.70 = 206.01
]

Illustrative initial order:

| Field | Value |
|---|---|
| Side | BUY |
| Entry time | 09:57 IST |
| Entry price | ₹294.30 |
| Initial stop | ₹206.01 |
| Initial target | ₹353.16 |
| Target divisor | 5 |
| Status | OPEN |

The values above are a worked example, not a hardcoded strategy constant.

---

## 10. STANDARD order management and exits

For STANDARD unified Path A orders:

1. The initial stop remains active after entry.
2. When a later one-minute candle high reaches the initial dynamic target, the target milestone is marked.
3. Reaching the target is **not** an immediate exit.
4. The engine waits for a NEW 15/3 Resistance confirmation strictly after the target milestone timestamp.
5. The exit price is the traded instrument's one-minute candle close at that Resistance confirmation timestamp.
6. If the stop is hit first, the order closes at the configured stop price under the candle-based fill assumption.
7. If neither stop nor the post-target Resistance exit occurs, the configured EOD close applies.

The Resistance confirmation must be later than the milestone; a Resistance confirmed at or before the target-touch time is not eligible.

The STANDARD order's post-target exit reason is `TARGET_MILESTONE_RESISTANCE_EXIT`. The sandbox transition maps this to the existing Stage 2 Resistance exit transition vocabulary.

---

## 11. EARLY_EXTREME_REVERSAL target and stop ladder

This ladder applies **only** when the order carries the pinned Early Extreme Reversal mode. It does not replace STANDARD Path A exits or POST-11:30 exits.

### 11.1 Ladder rules

1. Start with the normal initial stop (SL_0) and dynamic Target 1.
2. Before Target 1 is reached, (SL_0) remains active.
3. When Target 1 is reached:
   - Move stop-loss to the original entry price.
   - Calculate Target 2 as Target 1 × 1.30.
4. When Target 2 is reached:
   - Move stop-loss to Target 1.
   - Calculate Target 3 as Target 2 × 1.30.
5. When Target 3 is reached:
   - Move stop-loss to Target 2.
   - Calculate Target 4 as Target 3 × 1.30.
6. Continue the same pattern for every further target.

For target stage `n >= 1`:

```text
next_target = current_target * 1.30

if stage == 1:
    protective_stop = entry_price
else:
    protective_stop = previous_target
```

This means the stop moves to breakeven after Target 1 and then locks the previous target level after each later target. It is **not** a fixed 15% trailing stop.

### 11.2 Worked example using the illustrative order

Entry = ₹294.30, initial stop = ₹206.01, Target 1 = ₹353.16.

| Event | Target reached | New protective stop | Next target |
|---|---:|---:|---:|
| Entry | — | ₹206.01 | ₹353.16 |
| Target 1 hit | ₹353.16 | ₹294.30 (entry) | ₹459.11 |
| Target 2 hit | ₹459.11 | ₹353.16 (Target 1) | ₹596.84 |
| Target 3 hit | ₹596.84 | ₹459.11 (Target 2) | ₹775.89 |
| Target 4 hit | ₹775.89 | ₹596.84 (Target 3) | ₹1,008.66 |

Prices are rounded to two decimals for display. The engine stores calculation values to four decimal places.

### 11.3 Stop evaluation and OHLC candle ambiguity

The engine uses one-minute OHLC candles. It cannot know the exact intraminute sequence of the high and low.

The deterministic replay rule is:

- At the start of each candle, check the currently active stop against that candle's low.
- If low is at or below the active stop, close at the active stop price.
- Otherwise, check whether candle high reaches the active target.
- When a target is reached, update the stop and next target; the newly calculated stop becomes active from the **next candle**.

If one candle's high crosses more than one successive target, the loop records each crossed target in order and advances the ladder. The newly computed stop is not retroactively tested against the same candle's low.

The candle-based exit fill assumes the active stop price. Real fills can differ because of gaps, slippage, spread, liquidity and execution latency.

### 11.4 Exit conditions

An Early Extreme Reversal order closes when:

- The initial stop is hit before Target 1; or
- The current ladder protective stop is hit after a target milestone; or
- The configured EOD close is reached.

The post-target 15/3 Resistance-confirmation exit is disabled for Early Extreme Reversal orders. S/R confirmations continue to be tracked as market structure, but they do not liquidate this order after entry.

The ladder uses close reason `TARGET_TRAIL_STOP_HIT` after at least one target has been reached. Before Target 1, the original `STOP_HIT` reason is used.

---

## 12. POST-11:30 strategy branch

POST-11:30 is a separate time-based execution regime. The dummy-order service calls `strategy_entry_signal_service.compute()` and creates a POST-11:30 order only when the returned signal is successful and its `execution_path` is `POST_1130`.

The order is created on the instrument and side supplied by the signal payload. Entry, stop and target are copied from the enriched signal entry data rather than re-derived by the unified Path A formula. The configured 15/15 exit S/R configuration is stored on the order, and the trade is registered with `order_runtime_service`.

The POST-11:30 runtime exit logic is responsible for its own stop, target and 15/15 Resistance conditions. It is not subject to the Early Extreme Reversal 15/3 target ladder.

This guide intentionally does not restate every POST-11:30 signal qualification threshold: those conditions belong to the signal service and its configuration, and should be read from that implementation rather than inferred from the standard Path A rules.

---

## 13. EOD and after-market replay

### 13.1 EOD

The dummy-order EOD cutoff defaults to 15:35 IST. Any surviving OPEN order is closed using the final available candle close (or the order's current LTP fallback if no candle close is available). The EOD close reason is `END_OF_DAY`.

### 13.2 Retroactive startup replay

The dummy-order engine supports an after-market startup catch-up:

- The live/retroactive classification boundary defaults to 15:30 IST and is configurable with `DUMMY_ORDER_RETROACTIVE_START_HOUR` and `DUMMY_ORDER_RETROACTIVE_START_MINUTE`.
- When the app starts at or after that boundary and before midnight on the same trading date, catch-up runs after upstream S/R backfill readiness.
- The same dummy-order engine and the same persisted state file are used.
- Orders created through this path are marked `created_retroactively=true`.
- Retroactive classification does not move the EOD cutoff; the separate EOD default remains 15:35 IST.

This is a deterministic order-state replay path. It does not, by itself, mean a real broker order was placed.

---

## 14. Dummy-order record and persistence

The standard order is persisted in the dummy-order state file (default state location is resolved by the service's `_state_path()`; current project deployments use `data/dummy_orders.json`). The service writes through a temporary file and atomically replaces the state file.

Important order fields include:

| Field | Meaning |
|---|---|
| `order_id` | Traded instrument key plus entry timestamp |
| `status` | OPEN or a terminal close reason |
| `instrument_key` | Actual traded contract |
| `isolated_key` | Contract selected by isolation |
| `bias_type` | Isolated option type |
| `routing_mode` | Same-leg/opposite-leg route explanation |
| `standard_execution_mode` | STANDARD or EARLY_EXTREME_REVERSAL |
| `early_extreme_reversal` | Boolean mode flag |
| `trigger` | First qualifying S/R pivot and confirmation details |
| `entry_time` / `entry_price` | Actual routed-contract entry timestamp and price |
| `entry_price_source` | Normally confirmation candle close |
| `initial_stop_price` | Stop calculated at order creation |
| `initial_target_price` | Target calculated at order creation |
| `stop_price` / `active_stop_price` | Current active protective stop |
| `target_price` / `active_target_price` | Current target being monitored |
| `target_ladder_enabled` | Whether the Early Extreme ladder applies |
| `target_ladder_stage` | Number of targets reached |
| `target_ladder_history` | Ordered record of each target touch and resulting levels |
| `current_ltp` | Latest one-minute candle close used by this service |
| `max_high_since_entry` / `min_low_since_entry` | Candle-derived high/low watermarks |
| `mfe` / `mae` | Maximum favourable/adverse excursion in points |
| `closed_time` / `closed_price` | Exit candle timestamp and assumed exit price |
| `closed_profit_pts` / `closed_pnl_pct` | Gross point and percentage result |
| `created_retroactively` | Whether order was created by after-market replay |

For an Early Extreme Reversal order, `target_ladder_history` contains one entry per reached target, including stage, target price, hit timestamp, protective stop and next target.

The state contains the standard `order` and the separate `post_1130_order`; a compatibility `orders` list is rebuilt from those records during save.

---

## 15. P&L calculation

For a BUY order:

```text
pnl_points = exit_price - entry_price
gross_pnl = pnl_points * quantity
return_pct = ((exit_price - entry_price) / entry_price) * 100
```

Example: entry ₹294.30, exit ₹353.16, quantity 65:

- Points = ₹58.86
- Gross P&L = ₹58.86 × 65 = ₹3,825.90
- Return ≈ 20.00%

This is gross arithmetic only. Brokerage, exchange charges, taxes, slippage, spread and actual fill differences are not deducted by these simple formulas unless a separate accounting layer does so.

---

## 16. Worked end-to-end example

The following example uses the previously analysed NIFTY 24400 CE sample values to demonstrate the strategy path. It is an illustrative replay example, not a statement that a live broker trade occurred.

### Isolation and entry

- Isolated contract: NIFTY 24400 CE.
- Isolation time: 09:50 IST.
- Isolation option snapshot: ₹308.35.
- Since 09:50 is before 10:30 and ₹308.35 is strictly above ₹250, mode is EARLY_EXTREME_REVERSAL.
- First qualifying post-isolation Resistance confirmation: 09:57.
- Resistance routing in Early Extreme Reversal buys the same isolated CE.
- Confirmation candle close / entry: ₹294.30.
- Initial stop: ₹206.01.
- Initial target: ₹353.16.

### Target and stop replay

- At 10:29, candle high ₹358.50 reaches Target 1 ₹353.16.
- New stop becomes entry ₹294.30.
- Target 2 becomes ₹459.11.
- At 10:44, candle low ₹291.60 is below the active ₹294.30 stop.
- Under the conservative candle replay rule, the order exits at the assumed stop price ₹294.30.
- Gross P&L before costs: ₹0 per unit.

Later highs in the same day's data may have crossed Target 2 and Target 3, but they occur after the order was already closed and must not be counted as achieved targets for that trade. This is why a trailing-stop replay must stop processing once the exit condition occurs.

---

## 17. Safety, idempotency and execution boundary

- The dummy-order service is the strategy/order-state source; it does not itself submit an exchange order.
- The optional sandbox executor consumes persisted transitions after the state file has been saved.
- A transition callback is invoked after persistence, so downstream consumers should not see a transition before the corresponding order state is durable.
- The sandbox executor has its own idempotency and exit-pending handling.
- No broker execution flag is enabled by this strategy documentation.
- Do not treat a dummy order, a retroactively reconstructed order, a sandbox order request, and a broker-confirmed fill as interchangeable states.

---

## 18. Practical verification checklist

Before enabling or relying on this strategy in a live environment, verify:

- [ ] Today's 09:15 candles and OR levels are correct for all monitored indices.
- [ ] The index priority and bias configuration match the intended deployment.
- [ ] The selected option contract and its opposite leg resolve correctly in the registry.
- [ ] The isolated option snapshot and pinned mode fields are present and correct.
- [ ] The isolated and routed instruments are registered and have seeded 1-minute candles.
- [ ] S/R pivot and confirmation timestamps are both strictly after isolation.
- [ ] Entry price comes from the routed instrument's confirmation-minute close.
- [ ] Early Extreme target 1 uses the correct entry-price band.
- [ ] Early Extreme target 1 moves SL to entry; target 2 moves SL to target 1; later targets repeat the prior-target rule.
- [ ] Standard orders still use their post-target new-Resistance exit.
- [ ] POST-11:30 remains on its own signal/runtime exit path.
- [ ] EOD and retroactive replay cutoffs are configured and understood.
- [ ] Sandbox/broker execution permissions and idempotency have been separately reviewed.
- [ ] Unit tests, replay tests and controlled paper/sandbox tests pass before any production deployment.

---

## 19. Strategy summary

| Strategy / mode | Entry trigger | Routing | Initial stop/target | Post-target exit |
|---|---|---|---|---|
| STANDARD unified Path A | First post-isolation 15/3 S/R confirmation | Support same leg; Resistance opposite leg, subject to 09:30 backup reversal | Configured stop (default 30%); dynamic target by entry band | First new 15/3 Resistance confirmation close after target |
| EARLY_EXTREME_REVERSAL | Same first post-isolation 15/3 S/R confirmation | Support opposite leg; Resistance same leg | Same initial stop and dynamic target | Repeating 30% target ladder; SL to entry after T1, then to previous target after each later target |
| POST-11:30 | Successful `POST_1130` signal from strategy signal service | Instrument/side supplied by signal | Stop and target supplied by signal | Separate runtime stop/target/15/15 Resistance logic |

**Current scope:** This guide documents the branch's strategy and dummy-order lifecycle. It does not certify profitability, live-feed reliability, broker execution, test coverage or production readiness.
