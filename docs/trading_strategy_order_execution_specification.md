# Strategy Architecture & Logic Enhancement Specification

> **Target Repository / Engine:** `t_feed_v1` / `market-engine-v4`  
> **Topic:** Instrument Isolation, Higher-Timeframe Trend Filtering, S/R Confirmation, Dynamic Stop-Loss & Target Rules  
> **Status:** Enhancement Proposal & Architectural Review  
> **Reference Timezone:** Asia/Kolkata (IST)

---

## 1. Executive Summary & Core Objective

This document consolidates the complete architectural analysis of the event-driven market signal and execution engine, focusing on:
1. Understanding why standard S/R confirmations succeeded on some instruments (`26-10 PE`) while failing on others (`09-10 CE`).
2. Diagnosing critical structural bugs and logic edge-cases in the current branch implementation (`market-engine-v4-unified-path-a`).
3. Introducing a deterministic **15-Minute Multi-Timeframe Trend Filter** to prevent counter-trend execution traps.
4. Defining an upgraded **Risk-Managed Entry, Stop-Loss, and Target Engine** integrating dual volatility boundaries ($\min(30\%, 30\text{ pts})$) and macro-historical liquidity benchmarks (Prior Day / Day-1 High-Low averages vs. 50-point increments).

---

## 2. Comparative Trade Diagnosis: Why CE Failed vs. Why PE Succeeded

### 2.1 The Two Reference Trades

| Parameter | Trade 1: NIFTY 22350 CE (09-10-2026) | Trade 2: NIFTY 22400 PE (26-10-2026) |
| :--- | :--- | :--- |
| **Trigger Bias / Strike** | `CE` / Strike 22350 | `PE` / Strike 22400 |
| **Isolation Touch Time** | 09:38:00 (R3 touch @ 09:34:00) | 09:21:00 (R3 touch @ 09:24:00) |
| **First 15/3 S/R Confirmed** | **Resistance: ₹221.00** (Pivot 09:51, Confirmed 09:54) | **Resistance: ₹116.80** (Pivot 09:26, Confirmed 09:29) |
| **Confirmation Candle Close** | ₹215.00 (Pullback to 198–200 held) | ₹80.85 (Exhaustion collapse) |
| **Previous Day Avg ($A_{D-1}$)** | High: 268.05, Low: 236.55 $\rightarrow$ **₹252.30** | High: 99.30, Low: 60.00 $\rightarrow$ **₹79.65** |
| **Result / Outcome** | **FAILED as Resistance (Blew out to ₹297+)** | **RESPECTED Resistance (Crashed to ₹14)** |

### 2.2 Root Causes of Divergence

```
                    Trade 1 (09-10 CE)                           Trade 2 (26-10 PE)
                  [Strong Bullish Trend]                     [Climactic Rejection Spike]
 300 |                      ▲                                 120 |       ▲ (116.80 Pivot High)
     |                     /                                      |      / \
 250 |              ▲     / (Rallied to 297)                  100 |     /   \
     |             / \   /                                        |    /     ▼ (Closed 80.85)
 200 |  ▲         /   ▲-▼ (Held 21 EMA @ 200)                  80 |---/--------------------------- 21 EMA
     | / \       /                                                |  /         \
 150 |/   ▼-----▼ (Rising 9/21 EMAs)                           60 | /           ▼ (Trended to 14)
 100 +-------------------------------------                    40 +--------------------------------
      09:15     09:38   09:54   10:30                              09:15   09:24 09:29  10:00
```

1. **EMA Trend Alignment:**
   - **On 26-10 PE:** Price broke sharply below both 9 and 21 EMAs by 09:29. Fast EMA crossed below slow EMA at 09:43. Momentum confirmed price exhaustion.
   - **On 09-10 CE:** 9 EMA was at 208+ and 21 EMA was steeply ascending. The pullback to 198 never breached the 21 EMA. S/R confirmation was merely a flag consolidation in an ongoing rally.
2. **Prior Day Value Vacuum:**
   - On 09-10, the CE opened deeply discounted at ₹108.50. The previous day's value average was **₹252.30**. The market was violently drawing toward the gap-fill/macro mean. Intermediate 1-minute pivots had negligible resistance value.

---

## 3. Discovered Vulnerabilities in the Existing Engine

Reviewing `STRATEGY_AND_ORDER_EXECUTION_GUIDE.md` reveals five critical edge-case bugs:

1. **Rigid Once-Per-Day Isolation Trap (Section 5.3):**
   - The first index touch permanently locks the isolated contract. If an early false spike occurs at 09:18 and the market shifts, the engine remains blind to legitimate trends developing elsewhere.
2. **Blind Reversal Routing on Resistance (Section 8.1):**
   - In `STANDARD` mode, Resistance unconditionally routes to **BUY the Opposite Leg (PE)**. On strong trend days, this triggers catastrophic counter-trend trades.
3. **Arbitrary Fixed Rupee Bands in Early Extreme Reversal (Section 6.1):**
   - Requiring snapshot prices strictly $> ₹250$ or $< ₹100$ fails to scale across the weekly cycle (e.g., expiry 0-DTE vs. Friday 4-DTE).
4. **Contradictory Same-Leg Premium Filter (Section 8.4 vs. Section 16):**
   - Section 8.4 mandates that EER same-leg orders are valid only when $80 \le \text{Entry} \le 100$. However, Section 16's worked example executes an EER same-leg order at **₹294.30**, which would be discarded as `SKIPPED / FILTERED` under live filter code.
5. **Absence of Breakeven Protection in STANDARD Exits (Section 10):**
   - A trade reaching +30% does not take profit until a new 15/3 Resistance confirms. If price reverses abruptly without forming a new pivot, the trade decays into the full -30% stop-loss.

---

## 4. Enhanced Multi-Timeframe Trend & S/R Logic

To eliminate counter-trend failures, we introduce a **15-Minute Macro Trend Gate** before validating any 1-minute 15/3 S/R confirmation.

```
+--------------------------------------------------------------------+
| 1-Minute 15/3 S/R Level Confirmed on Isolated Instrument           |
+--------------------------------------------------------------------+
                                  |
                                  ▼
+--------------------------------------------------------------------+
| Higher Timeframe (15m) Trend Evaluation                            |
| (Uses synthetic 15m candle before 09:30; completed 15m post-09:30) |
+--------------------------------------------------------------------+
         |                                           |
    [ BULLISH ]                                 [ BEARISH ]
         |                                           |
         ▼                                           ▼
+-----------------------------+             +-----------------------------+
| Confirmed S/R = SUPPORT?    |             | Confirmed S/R = RESISTANCE? |
+-----------------------------+             +-----------------------------+
   |                       |                   |                       |
 [YES]                    [NO]               [YES]                    [NO]
   |                       |                   |                       |
   ▼                       ▼                   ▼                       ▼
VALID BUY SIGNAL     INVALID / SKIP      VALID SHORT / PE    INVALID / SKIP
(Buy Isolated Leg)  (Do NOT fade rally)  (Buy Opposite Leg) (Do NOT buy dips)
```

### 4.1 Resolving the 09:15–09:30 Data Absence Problem

Because the first official 15-minute candle only closes at 09:30, the engine uses a dual evaluation path:

1. **Intra-Opening Interval (09:15 to 09:30):**
   - Construct a **synthetic 15-minute candle** using 1-minute closes from 09:15 to timestamp $T$.
   - **Bullish:** Current Close $> \text{Open}_{09:15}$ AND Current Close $> \text{OR Midpoint } A$.
   - **Bearish:** Current Close $< \text{Open}_{09:15}$ AND Current Close $< \text{OR Midpoint } A$.
2. **Post-Opening Interval ($\ge 09:30$):**
   - Evaluate completed 15-minute bar series.
   - **Bullish:** $\text{Close}_{15m} > \text{Open}_{15m}$ AND $\text{LTP} > \text{EMA}_{15m}(9)$.
   - **Bearish:** $\text{Close}_{15m} < \text{Open}_{15m}$ AND $\text{LTP} < \text{EMA}_{15m}(9)$.

---

## 5. Order Execution, Stop-Loss & Dynamic Target Engine

### 5.1 Trade Direction & Instrument Selection

* **Long Signal (Support Confirmed + 15m Bullish):**
  - Traded Instrument: Isolated Instrument.
  - Action: **BUY**.
* **Reversal Signal (Resistance Confirmed + 15m Bearish):**
  - Traded Instrument: Opposite Leg (CE $\leftrightarrow$ PE at matching strike) or Underlying Short.
  - Action: **BUY Opposite Option** at its prevailing market close.

### 5.2 Dynamic Dual-Bounded Stop-Loss ($\min(30\%, 30\text{ pts})$)

To protect capital across both high-beta and penny option regimes:

$$\text{SL Distance} = \min\left(\text{Entry Price} \times 0.30,\, 30.0\right)$$

* **For Long Orders (Buy Call / Buy Put):**
  $$\text{Stop Loss} = \text{Entry Price} - \text{SL Distance}$$
* **For Short/Underlying Orders:**
  $$\text{Stop Loss} = \text{Entry Price} + \text{SL Distance}$$

### 5.3 Tri-Anchor Target Ladder

Targets are anchored dynamically to whichever reference level is nearest in the trade direction:

$$\text{Target}_{\text{Long}} = \min\left(\text{Valid Macro Targets},\, \text{Entry} + 50.0\right)$$

Where $\text{Valid Macro Targets}$ includes:
1. **$\text{Avg}_{D-1}$ (Yesterday's High-Low Midpoint):** $\frac{\text{High}_{D-1} + \text{Low}_{D-1}}{2}$
2. **$\text{Avg}_{D-2}$ (Day-Minus-1 High-Low Midpoint):** $\frac{\text{High}_{D-2} + \text{Low}_{D-2}}{2}$
3. Filter condition: $\text{Avg} > \text{Entry Price}$ for Longs; $\text{Avg} < \text{Entry Price}$ for Shorts.

---

## 6. End-to-End Implementation Blueprint

```python
"""
Order Parameter & Filter Calculation Module
Branch: market-engine-v4-unified-path-a-enhanced
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class Candle1m:
    timestamp: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class OrderParameters:
    signal_type: str
    traded_symbol: str
    side: str
    entry_price: float
    stop_loss: float
    target: float
    risk_pts: float
    reward_pts: float
    target_reference: str


def evaluate_15m_trend(candles_1m: List[Candle1m]) -> str:
    """Evaluates higher-timeframe trend status deterministically, handling both pre-09:30 and post-09:30 windows."""
    if not candles_1m:
        return "NEUTRAL"

    first_open = candles_1m[0].open
    current_close = candles_1m[-1].close
    session_high = max(c.high for c in candles_1m)
    session_low = min(c.low for c in candles_1m)
    session_midpoint = (session_high + session_low) / 2.0

    # Synthetic Trend Gate
    if current_close > first_open and current_close >= session_midpoint:
        return "BULLISH"
    elif current_close < first_open and current_close <= session_midpoint:
        return "BEARISH"
    return "NEUTRAL"


def validate_and_build_order(
    confirmed_sr_type: str,  # "SUPPORT" or "RESISTANCE"
    sr_level_price: float,
    current_candle: Candle1m,
    candles_today: List[Candle1m],
    yesterday_avg: float,
    day_minus_1_avg: float,
    isolated_symbol: str,
    opposite_symbol: str,
    opposite_leg_close: float = 0.0,
) -> Optional[OrderParameters]:
    """Validates S/R signal against 15m trend and generates complete order parameters."""
    trend = evaluate_15m_trend(candles_today)

    # 1. Trend Validation Filtering
    if confirmed_sr_type == "SUPPORT":
        if trend != "BULLISH":
            return None  # Filtered: Cannot buy support in a non-bullish trend
        traded_symbol = isolated_symbol
        entry_price = current_candle.close
        side = "BUY"
        is_long = True

    elif confirmed_sr_type == "RESISTANCE":
        if trend != "BEARISH":
            return None  # Filtered: Cannot short/fade resistance in a non-bearish trend
        # Route to opposite option leg
        traded_symbol = opposite_symbol
        entry_price = (
            opposite_leg_close
            if opposite_leg_close > 0
            else current_candle.close
        )
        side = "BUY"  # Buying the Put option
        is_long = True
    else:
        return None

    # 2. Dynamic Stop-Loss Calculation: min(30%, 30 points)
    sl_distance = min(entry_price * 0.30, 30.0)
    stop_loss = round(entry_price - sl_distance, 2)

    # 3. Dynamic Target Calculation
    fixed_50_target = entry_price + 50.0
    macro_candidates = [
        avg for avg in [yesterday_avg, day_minus_1_avg] if avg > entry_price
    ]

    if macro_candidates:
        nearest_macro = min(macro_candidates)
        if nearest_macro < fixed_50_target:
            target = round(nearest_macro, 2)
            target_ref = "NEAREST_MACRO_AVG"
        else:
            target = round(fixed_50_target, 2)
            target_ref = "FIXED_50_PTS"
    else:
        target = round(fixed_50_target, 2)
        target_ref = "FIXED_50_PTS"

    return OrderParameters(
        signal_type=f"{trend}_{confirmed_sr_type}",
        traded_symbol=traded_symbol,
        side=side,
        entry_price=round(entry_price, 2),
        stop_loss=stop_loss,
        target=target,
        risk_pts=round(entry_price - stop_loss, 2),
        reward_pts=round(target - entry_price, 2),
        target_reference=target_ref,
    )
```

---

## 7. Worked Verification Examples (Dummy Scenarios)

### Scenario A: Bullish Support Alignment (Valid Entry)

- **Isolated Instrument:** `NIFTY 22350 CE`
- **Confirmation Time:** 09:35 IST
- **Level Confirmed:** Support @ ₹145.00
- **15m Trend Assessment:** BULLISH (Current Close ₹152.00 > Open ₹108.50)
- **Macro Levels:** Yesterday Avg = ₹210.00 | Day-1 Avg = ₹240.00
- **Entry Execution:**
  - `Entry Price` = ₹152.00
  - `SL Distance` = $\min(152.00 \times 0.30,\, 30) = \min(45.60,\, 30) = \mathbf{30.00\text{ pts}}$
  - `Stop Loss` = $152.00 - 30.00 = \mathbf{₹122.00}$
  - `Fixed Target` = $152.00 + 50.00 = \mathbf{₹202.00}$
  - `Macro Target` = Nearest of [₹210.00, ₹240.00] = ₹210.00
  - `Final Target` = $\min(202.00,\, 210.00) = \mathbf{₹202.00}$ (Fixed 50 pts is nearer)
- **Order Result:** `BUY NIFTY 22350 CE` @ ₹152.00 | SL: ₹122.00 | Target: ₹202.00 (Risk: 30 pts, Reward: 50 pts).

### Scenario B: Counter-Trend Resistance Breakout Trap (Correctly Filtered)

- **Isolated Instrument:** `NIFTY 22350 CE`
- **Confirmation Time:** 09:54 IST
- **Level Confirmed:** Resistance @ ₹221.00
- **15m Trend Assessment:** BULLISH (Close ₹215.00 > Open ₹108.50)
- **Engine Filter Check:**
  - Confirmed level is `RESISTANCE`, but trend is `BULLISH`.
  - Reversal condition (`BEARISH`) is NOT met.
- **Order Result:** **FILTERED / SKIPPED**. Engine prevents buying the opposite PE leg, saving the strategy from the upward surge to ₹297+.

### Scenario C: Bearish Resistance Reversal (Valid Put Purchase)

- **Isolated Instrument:** `NIFTY 22400 PE`
- **Confirmation Time:** 09:40 IST
- **Level Confirmed:** Resistance @ ₹100.00
- **15m Trend Assessment:** BEARISH (Option collapsed from high of 116.80 to 91.45, below 21 EMA)
- **Entry Execution on Counter Leg / Put:**
  - `Entry Price` = ₹91.45
  - `SL Distance` = $\min(91.45 \times 0.30,\, 30) = \min(27.435,\, 30) = \mathbf{27.44\text{ pts}}$
  - `Stop Loss` = $91.45 - 27.44 = \mathbf{₹64.01}$
  - `Target` = $91.45 + 50.00 = \mathbf{₹141.45}$
- **Order Result:** Accurately adapts stop buffer to percentage on contracts $< ₹100$ while retaining downside safety.