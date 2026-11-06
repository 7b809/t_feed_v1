# Isolated Instrument Order Strategy & Execution Engine Specification
**Engine Version:** `t_feed_v1-market-engine-v3`  
**Revision:** Post-Isolation S/R (15/3) Dynamic Routing & Two-Stage Trailing Protocol  
**Document Type:** Technical Execution Architecture & Simulation Audit  

---

## Current implementation — unified Path A (2026-11-06)

This section supersedes the older Path A/Path B split and the Stage 1 OR-grid / +50-point target rules described in earlier revisions.

### Standard session (pre-11:30 IST)

1. Isolation detection is unchanged. The index that caused the isolation is retained as metadata only.
2. Every standard-session isolation uses one execution mode named **Path A**. It uses the former Path B-style standard mechanics: keep the selected bias-side instrument isolated and route entries using its post-isolation 15/3 Support/Resistance confirmations.
3. **Early-session extreme-price reversal mode is pinned at isolation.** It activates only when BOTH conditions hold at the official isolation timestamp (the persisted touch/isolation time used by Path A):
   - isolation time is strictly before 10:30:00 IST; and
   - the isolated option price snapshot is strictly greater than ₹250 or strictly less than ₹100.
   The decision and its source price/timestamp are persisted in the isolation record as `standard_execution_mode`, `early_extreme_reversal`, `execution_mode_price`, and `execution_mode_decided_at`. It is not recalculated from later pivots, confirmation prices, or live LTP. Existing isolation records without these fields default to `STANDARD`.
4. In `EARLY_EXTREME_REVERSAL`, every subsequent standard-session S/R confirmation uses inverted routing for the lifetime of that isolation:
   - Support confirmation → buy the opposite option leg.
   - Resistance confirmation → buy the isolated (same-side) option leg.
   This overrides the normal 09:30 backup routing behavior while the pinned mode is active.
5. Boundary values are strict: exactly ₹100, exactly ₹250, or exactly 10:30:00 IST do not activate reversal mode. All three cases use `STANDARD` routing.
6. If the mode is `STANDARD`, the existing 09:30 backup reversal and same-leg/opposite-leg routing rules remain unchanged.
7. Existing traded-instrument OR validation remains unchanged, including skipping an entry above R2. The OR grid no longer supplies profit-stage milestones.
8. Stop-loss calculation remains the existing configured percentage (default 30% below entry).
9. One target milestone is calculated from entry price:
   - 0 < entry <= 100: target = entry + entry / 3
   - 100 < entry <= 200: target = entry + entry / 4
   - entry > 200: target = entry + entry / 5
10. Reaching the target only marks the milestone. Stop-loss stays active. The system ignores Resistance confirmations at or before the milestone timestamp and exits at the 1-minute candle close of the first newly confirmed 15/3 Resistance strictly after the milestone.
11. If neither stop-loss nor the post-target Resistance exit occurs, the existing EOD close remains active.

### POST-11:30 IST

POST-11:30 remains a separate time-based strategy. Its existing entry confirmation, fixed target, stop-loss, 15/15 Resistance exit and EOD behavior are not changed by the standard Path A update.

### Execution and safety

Dummy orders remain the signal/order-state source. The optional sandbox executor continues to consume persisted create/close transitions; the new standard Resistance exit maps to the existing Stage 2 Resistance exit transition so its exit-pending/idempotency handling is reused. No broker execution flag is enabled by this change.

---

## Historical strategy specification (pre-unified Path A)

Sections below retain the earlier OR-grid milestone and simulation examples for historical reference. They are not the current standard-session target or exit rules.

## 1. Core Architecture & Opening Range (OR) Formulas

All dynamic price structural boundaries are resolved directly from the first minute bar of the session ($09:15:00$ IST):

$$\begin{aligned}
\text{Open} &= \text{first.open} \\
\text{High} &= \text{first.high} \\
\text{Low} &= \text{first.low} \\
\text{Close} &= \text{first.close} \\
\text{R1} &= \text{High} \\
\text{S1} &= \text{Low} \\
\text{AVG} &= \frac{\text{R1} + \text{S1}}{2} = \frac{\text{High} + \text{Low}}{2} \\
\text{Diff} &= \frac{\text{High} - \text{Low}}{2} \\
\text{R2} &= \text{R1} + \text{Diff} = \text{High} + \text{Diff} \\
\text{R3} &= \text{R2} + \text{Diff} = \text{High} + (\text{High} - \text{Low}) \\
\text{R3\_Threshold} &= \frac{\text{R2} + \text{R3}}{2} \\
\text{S2} &= \text{S1} - \text{Diff} = \text{Low} - \text{Diff} \\
\text{S3} &= \text{S2} - \text{Diff} = \text{Low} - (\text{High} - \text{Low}) \\
\text{S3\_Threshold} &= \frac{\text{S2} + \text{S3}}{2}
\end{aligned}$$

---

## 2. Order Placement & Routing Rules

### 2.1 Post-Isolation Event Trigger
* An instrument is marked `ISOLATED` at timestamp $T_{\text{iso}}$.
* The execution engine monitors for the **first qualified Support or Resistance level** on the contract on the $15/3$ timeframe satisfying:
  $$\text{pivot\_time} > T_{\text{iso}} \quad \text{AND} \quad \text{confirmed\_time} > T_{\text{iso}}$$

### 2.2 Time-Windowed Backup Reversal Check ($09:30$ AM Cutoff)
Before executing standard directional leg routing, inspect any S/R level confirmed before the post-isolation confirmation time:
1. **Window Limit:** The post-isolation S/R confirmation must occur **on or before $09:30:00$ IST**.
2. **Reversal Criterion:**
   * If the level confirmed *prior* to $T_{\text{iso}}$ is a **Support** level AND the post-isolation level is a **Resistance** level:
     $$\mathbf{Action:} \ \text{Reverse routing} \longrightarrow \text{Place order on the } \mathbf{ISOLATED\ INSTRUMENT\ ITSELF}.$$
3. **Normal Flow (After $09:30:00$ IST or without prior opposite confirmation):**
   * If post-isolation confirmed level is **Support** $\rightarrow$ Buy **Same Isolated Instrument**.
   * If post-isolation confirmed level is **Resistance** $\rightarrow$ Buy **Opposite Leg Instrument** (CE $\leftrightarrow$ PE).

### 2.3 Pre-Order Regime Validation (Grid Boundaries)
Evaluate the proposed entry price $P_{\text{entry}}$ (live LTP or confirmation bar close):

* **Above R2 ($P_{\text{entry}} > \text{R2}$):**  
  **Skip order.** Abort placement with log: `"No valid logic present above R2 level"`. Send alert notification.
* **Between S2 and R2 ($\text{S2} \le P_{\text{entry}} \le \text{R2}$):**  
  **Valid Zone 2.** Target milestones: `R3_Threshold` and `R3`.
* **Below S2 / Below R1:**  
  * If $P_{\text{entry}} < \text{S2}$: Priority milestone is contract Opening Range Low (ORL) or points gain $> 50$.
  * If $P_{\text{entry}} < \text{R1}$: Arm sequential milestones `R2`, `R3_Threshold`, and `R3`.

### 2.4 Stop-Loss & Risk Management
$$\text{Stop-Loss Price} = P_{\text{entry}} \times (1 - 0.30) = 0.70 \times P_{\text{entry}}$$
* Fixed risk limit: $30\%$ below trade fill price.

---

## 3. Dynamic Milestone Trailing & Exit Logic

Positions transition through a two-stage lifecycle:

```
               [ Trade Entry: P_entry ]
                          │
         ┌────────────────┴────────────────┐
         ▼                                 ▼
   Zone 1: Below S2 / R1             Zone 2: S2 to R2
   Milestones: OR Low / R2-R3        Milestones: R3_Threshold / R3
         │                                 │
         └────────────────┬────────────────┘
                          ▼
            [ Milestone Touch Reached ]
                          OR
            [ Point Gain > 50 Points ]
                          │
                          ▼
          [ Stage 2: Switch Config to 15/3 ]
                          │
                          ▼
      [ Exit at Next 15/3 Resistance Confirmation Close ]
```

1. **Stage 1 (Milestone Armed):** Price action must touch the defined structural grid milestone OR net profit must surpass $+50$ points from entry.
2. **Stage 2 (Shift & Liquidation):** Once triggered, the engine arms the tracker for the **next 15/3 Resistance confirmation**. The position is squared off at the confirmation bar close.

---

## 4. End-to-End Simulation Test Results

### Simulation Case 1: Post-09:30 Resistance $\rightarrow$ Opposite Leg & $+50$ Pt Gain Exit
* **Instrument:** `NSE_FO|53750` (`NIFTY 22650 CE 03 NOV 26`)
* **Isolation Touch:** $09:48:00$ IST
* **First Post-Isolation S/R:** Resistance confirmed at $09:54:00$ ($203.60$)
* **Time Cutoff Evaluation:** $09:54:00 > 09:30:00$ (Reversal rule inactive; standard flow routed to opposite PE leg `NSE_FO|53781`)
* **Execution:** Buy at $209.10$ | Stop-Loss at $146.37$
* **Milestone Hit:** Price touches $279.50$ at $10:06:00$ ($> 50$ pt gain trigger active)
* **Exit:** Next 15/3 Resistance confirmed at $10:09:00$, exited at candle close $257.20$
* **Result (1 Lot = 65 Qty):** $+48.10$ pts | Net Realized P&L: $+₹3,126.50$ ($+23.00\%$)

```json
{
  "simulation_id": "SIM_CASE_1_OPP_LEG_POINT_GAIN",
  "isolated_instrument": "NSE_FO|53750",
  "traded_instrument": "NSE_FO|53781",
  "trading_symbol": "NIFTY 22650 PE 03 NOV 26",
  "side": "BUY",
  "quantity": 65,
  "entry_time": "2026-10-28T09:54:00+05:30",
  "entry_price": 209.10,
  "stop_loss_price": 146.37,
  "capital_invested": 13591.50,
  "point_gain_reached_time": "2026-10-28T10:06:00+05:30",
  "exit_time": "2026-10-28T10:09:00+05:30",
  "exit_reason": "POINTS_EXCEEDED_50_NEXT_15_3_RESISTANCE_CLOSE",
  "exit_price": 257.20,
  "pnl_points": 48.10,
  "realized_pnl_inr": 3126.50,
  "return_pct": 23.00
}
```

---

### Simulation Case 2: Pre-09:30 Backup Reversal $\rightarrow$ Same Leg Execution
* **Instrument:** `NSE_FO|53792` (`NIFTY 22750 CE 03 NOV 26`)
* **Isolation Touch:** $09:18:00$ IST
* **First Post-Isolation S/R:** Resistance confirmed at $09:23:00$ ($162.00$)
* **Time Cutoff Evaluation:** $09:23:00 \le 09:30:00$ (Active)
* **Prior Backup Check:** Support confirmed at $09:18:00$ ($115.10$). Prior Support + Current Resistance = **Reversal Triggered**
* **Execution:** Reverted back to same isolated CE leg (`NSE_FO|53792`)
* **Entry:** Buy at $152.00$ | Stop-Loss at $106.40$
* **Grid Milestones:** `R3_Threshold` ($176.35$), `R3` ($185.10$)
* **Result (1 Lot = 65 Qty):** Trade armed in Zone 2 targeting $185.10$

```json
{
  "simulation_id": "SIM_CASE_2_PRE_930_REVERSED_SAME_LEG",
  "isolated_instrument": "NSE_FO|53792",
  "traded_instrument": "NSE_FO|53792",
  "trading_symbol": "NIFTY 22750 CE 03 NOV 26",
  "side": "BUY",
  "quantity": 65,
  "entry_time": "2026-10-29T09:23:00+05:30",
  "entry_price": 152.00,
  "stop_loss_price": 106.40,
  "capital_invested": 9880.00,
  "regime_zone": "BETWEEN_S2_AND_R2",
  "active_milestones": {
    "tier_1_r3_threshold": 176.35,
    "tier_2_r3": 185.10
  },
  "status": "OPEN_MONITORING_R3_TOUCH"
}
```

---

### Simulation Case 3: Structural Boundary Exhaustion ($P_{\text{entry}} > \text{R2}$) $\rightarrow$ Order Skipped
* **Instrument:** `NSE_FO|53942` (`NIFTY 23350 CE 03 NOV 26`)
* **Isolation Timestamp:** $10:58:01$ IST
* **First Post-Isolation S/R:** Support confirmed at $11:02:00$ ($69.35$)
* **Confirmation Close Price:** $75.90$
* **Grid Boundaries:** $\text{R2} = 71.975$
* **Boundary Evaluation:** $75.90 > 71.975$ ($P_{\text{entry}} > \text{R2}$)
* **Action:** Order skipped. Abort order dispatch and broadcast alert.

```json
{
  "simulation_id": "SIM_CASE_3_EXHAUSTION_SKIPPED",
  "isolated_instrument": "NSE_FO|53942",
  "trading_symbol": "NIFTY 23350 CE 03 NOV 26",
  "attempted_entry_price": 75.90,
  "r2_boundary": 71.975,
  "order_status": "SKIPPED",
  "reason": "Proposed entry price 75.90 is above R2 (71.975). No valid logic present above R2 level.",
  "alert_dispatched": true
}
```