# Strategy Specification (v2.3)
**First-Candle OR Ladder · Isolation-Buffered Entry · 30%/30pt Initial SL · S2..AVG Entry-Zone R3 Trigger · Sub-S2 Recovery Logic (Low Touch + 50pt Move → Breakeven SL & R3 Touch Cap) · Dynamic 15/30 / 15/3 Exit · Single Order**

---

## 1. Configuration & Parameters

| Parameter | Value | Meaning |
| :--- | :--- | :--- |
| `LOT` | 65 | Contract lot size |
| `MAX_ORDERS` | 1 | Single order per day |
| `TICK` | 5.0 | Tick size |
| `EARLY_ISOLATION_CUTOFF` | 09:30 | Post-cutoff = normal SR path |
| `SR_LEFT` / `SR_RIGHT` | 15 / 3 | Entry-signal pivot scan (post-09:30) |
| `EARLY_SR_WINDOW_START` / `END` | 09:15 / 09:30 | Early-isolation scan window |
| `EARLY_SR_LEFT` / `RIGHT` | 15 / 3 | Early-isolation pivot scan |
| `EARLY_SR_ALLOW_PRIOR_DAY` | True | Early scan may use prior-day candles |
| `EARLY_SR_ALLOW_POST_WINDOW_CONFIRM` | True | Early scan may confirm after 09:30 |
| `EXIT_LEFT_INITIAL` / `RIGHT_INITIAL` | 15 / 30 | Initial exit-resistance scanner |
| `EXIT_LEFT_POST_MOVE` / `RIGHT_POST_MOVE` | 15 / 3 | Exit scanner after standard R3 milestone |
| `SL_PCT_OF_ENTRY` | 0.30 | 30% of entry as SL offset |
| `SL_MIN_POINTS` | 30.0 | Minimum SL offset floor |
| `TRIGGER_REQUIRES_ENTRY_ZONE` | True | Standard R3 trigger only valid if entry $\in [\text{S2}, \text{AVG}]$ |
| `ENTRY_ZONE_LOW_LEVEL` | S2 | Lower bound of normal entry zone |
| `ENTRY_ZONE_HIGH_LEVEL` | AVG | Upper bound of normal entry zone |
| `R3_POINTS_TRIGGER` | 50.0 | Minimum move from reference |
| `TRIGGER_POINTS_FROM` | OR_OPEN | Reference price (`OR_OPEN` / `OR_LOW` / `ENTRY`) |
| `ISOLATION_BUFFER_ENABLED` | True | Enforce the isolation-time entry buffer |
| `ISOLATION_BUFFER_ENTRY_MODE` | NEXT_CANDLE_OPEN | `NEXT_CANDLE_OPEN` or `LIVE_PRICE_AT_ISOLATION` |
| `SUB_S2_RECOVERY_ENABLED` | True | **(New in v2.3)** Enables recovery handling for orders below S2 |
| `SUB_S2_MOVE_POINTS` | 50.0 | **(New in v2.3)** Minimum point expansion from entry required to trigger Sub-S2 recovery |

---

## 2. Opening Range (OR) Ladder — First-Candle Formulas
The OR candle is the very first candle of the trading day (sorted ascending by timestamp). For 1-minute candle bars, it is the `09:15` candle.

```text
OR candle → Open  = first.open
            High  = first.high
            Low   = first.low
            Close = first.close   (carried but not used in level math)
```

### 2.1 Mathematical Formulas
$$\text{Range} = \text{High} - \text{Low}$$
$$\text{Step} = \frac{\text{Range}}{2} = \frac{\text{High} - \text{Low}}{2}$$
$$\text{AVG} = \frac{\text{High} + \text{Low}}{2}$$

**Resistance Ladder (Above High):**
$$\text{R1} = \text{High}$$
$$\text{R2} = \text{High} + \text{Step}$$
$$\text{R3} = \text{High} + \text{Range} = \text{High} + (\text{High} - \text{Low})$$

**Support Ladder (Below Low):**
$$\text{S1} = \text{Low}$$
$$\text{S2} = \text{Low} - \text{Step}$$
$$\text{S3} = \text{Low} - \text{Range} = \text{Low} - (\text{High} - \text{Low})$$

---

## 3. Initial Stop-Loss
$$\text{pct\_offset} = \text{entry} \times 0.30$$
$$\text{min\_offset} = 30.0$$
$$\text{offset} = \max(\text{pct\_offset}, \text{min\_offset})$$
$$\text{SL}_{\text{initial}} = \text{entry} - \text{offset}$$

*(Note: On an exact tie, e.g., entry $= 100$, floor wins $\rightarrow \text{MIN\_30}$)*.

---

## 4. Entry Classification & Zones

* **In-Zone Entry:** An entry is considered in-zone when:
  $$\text{S2} \le \text{entry} \le \text{AVG}$$
* **Sub-S2 Entry (New in v2.3):** Any entry where:
  $$\text{entry} < \text{S2}$$

---

## 5. Isolation Buffer Mechanics
When `ISOLATION_BUFFER_ENABLED = True`:
* If $T_{\text{sig}} \ge T_{\text{iso}}$: No buffer applied; entry executes at `sr.confirmed_idx` open.
* If $T_{\text{sig}} < T_{\text{iso}}$: Entry is delayed to $T_{\text{iso}}$.
  * **`NEXT_CANDLE_OPEN`**: Open of first bar where $\text{timestamp} \ge T_{\text{iso}}$.
  * **`LIVE_PRICE_AT_ISOLATION`**: Close of the last bar where $\text{timestamp} < T_{\text{iso}}$.

---

## 6. Sub-S2 Recovery Logic (New in v2.3)

### 6.1 Purpose
When an order executes below $\text{S2}$, the entry was previously deemed invalid for milestone tracking, freezing the stop loss at maximum risk and locking the exit to passive 15/30 confirmation. The **Sub-S2 Recovery Logic** provides an active risk-reduction and profit-securing mechanism for deep discount entries.

### 6.2 Trigger Conditions
For any trade where $\text{entry} < \text{S2}$:
1. **Low Touch:** The market must touch or breach the OR Low of that leg:
   $$\text{bar.low} \le \text{ladder}[\text{"low"}]$$
2. **50-Point Expansion:** The market must expand upwards by more than 50 points from the entry price:
   $$\text{bar.high} - \text{entry} > 50.0$$

### 6.3 Actions Upon Trigger
Once both conditions are fulfilled on bar $j$:
1. **Breakeven Stop-Loss:** Move stop-loss immediately to the exact entry price:
   $$\text{SL}_{\text{current}} = \text{entry}$$
2. **Target Mode Set:** Target is fixed to the next resistance confirmation using the **15/30** scanner (`left = 15, right = 30`).
3. **Early R3 Touch Exit:** If at any point before the 15/30 pivot confirmation occurs, price touches or exceeds the $\text{R3}$ level:
   $$\text{bar.high} \ge \text{ladder}[\text{"R3"}]$$
   The strategy exits **immediately** at that touch price:
   $$\text{Exit Price} = \max(\text{R3}, \text{bar.open})$$
   $$\text{Reason} = \text{"SUB\_S2\_R3\_TOUCH\_EXIT@R3"}$$

---

## 7. Standard In-Zone R3 Milestone Trigger
Applicable strictly when $\text{entry\_in\_zone} == \text{True}$:
1. $\text{bar.high} \ge \text{R3}$
2. $\text{bar.high} - \text{reference\_price} > 50.0$ (where reference price is determined by `TRIGGER_POINTS_FROM`)

**Actions:**
* Trailing SL updates to the previous candle's close: $\text{SL}_{\text{current}} = \text{candles}[j-1].\text{close}$
* Scanner flips from $15/30 \rightarrow 15/3$.

---

## 8. Bar-by-Bar Order Execution Flow

For each subsequent candle $j$ after entry:

1. **Stop-Loss Check First:**
   * If $\text{bar.low} \le \text{SL}_{\text{current}}$:
     * Exit immediately at $\text{SL}_{\text{current}}$.
     * Reason: `SL_BREAKEVEN` if Sub-S2 triggered, else `SL`. Stop execution.

2. **Sub-S2 Path (if $\text{entry} < \text{S2}$):**
   * Track if $\text{bar.low} \le \text{ladder}[\text{"low"}]$.
   * If low touched and $(\text{bar.high} - \text{entry}) > 50.0$:
     * Set $\text{SL}_{\text{current}} = \text{entry}$ (Breakeven).
     * Arm 15/30 resistance scanner and R3 touch monitor.
   * If Sub-S2 is triggered:
     * Check if $\text{bar.high} \ge \text{R3}$. If true $\rightarrow$ Exit immediately at $\max(\text{R3}, \text{bar.open})$, Reason: `SUB_S2_R3_TOUCH_EXIT@R3`. Stop execution.
     * Check 15/30 resistance confirmation. If confirmed $\rightarrow$ Exit at $\text{bar.close}$, Reason: `RESISTANCE_CONFIRM_15_30@pivot_high`. Stop execution.

3. **Standard In-Zone Path (if $\text{S2} \le \text{entry} \le \text{AVG}$):**
   * Check standard R3 Milestone ($\text{high} \ge \text{R3}$ and expansion $> 50$).
   * If met: Trail SL to previous close, flip scanner to 15/3.
   * Check resistance confirmation (15/30 before trigger, 15/3 post-trigger). If confirmed $\rightarrow$ Exit at $\text{bar.close}$. Stop execution.

4. **End of Day (EOD):**
   * If no exit triggered prior to session end, exit at final bar's close. Reason: `EOD`.

---

## 9. Comprehensive Output Schema

```json
{
  "status": "TRADE",
  "trade_leg": "opposite",
  "signal": {
    "type": "resistance",
    "pivot_ts": "2026-10-21T09:36:00",
    "confirmed_ts": "2026-10-21T09:39:00",
    "pivot_price": 187.9,
    "confirmed_idx": 24
  },
  "signal_ts": "2026-10-21T09:39:00",
  "isolation_ts": "2026-10-21T09:31:00",
  "entry_idx": 24,
  "entry_ts": "2026-10-21T09:39:00",
  "entry": 160.9,
  "entry_in_zone": false,
  "sub_s2_recovery": {
    "eligible": true,
    "low_touched": true,
    "triggered": true,
    "trigger_details": {
      "trigger_idx": 48,
      "trigger_ts": "2026-10-21T10:03:00",
      "trigger_bar_high": 216.7,
      "new_sl": 160.9,
      "target_scanner": "15/30",
      "r3_cap": 252.4
    }
  },
  "sl_initial": 112.63,
  "sl_basis": "PCT_30",
  "exit": {
    "exit_idx": 57,
    "exit_ts": "2026-10-21T10:12:00",
    "exit_price": 252.4,
    "reason": "SUB_S2_R3_TOUCH_EXIT@252.40000000000003"
  },
  "pnl": 5947.5
}
```

---

## 10. Summary of Changes (v2.2 → v2.3)

| Component | Strategy v2.2 | Strategy v2.3 (Current) |
| :--- | :--- | :--- |
| **Out-of-Zone Entries (< S2)** | SL remains static; 15/30 scanner runs passively until confirmation or EOD. | **Sub-S2 Recovery Active**: Tracks OR Low touch + 50pt move from entry. |
| **Stop-Loss Below S2** | Kept at initial 30%/30pt level throughout trade lifetime. | **Moves to Breakeven (`entry`)** once 50pt expansion criteria is reached. |
| **Milestone / Profit Taking Below S2** | R3 Milestone inhibited. Price forced to wait for 30 bars confirmation. | **Early R3 Touch Exit**: Exits immediately at R3 upon touch before 15/30 confirmation. |
| **Batch Capabilities** | Single-file runner only. | **Automated Batch Processing** with folder traversal and combined aggregate metrics. |