# Global Option Chain Breakout Formula (Call & Put Scanner)

This document defines a standardized, scale-independent quantitative framework for scanning an entire option chain daily to detect high-momentum directional moves ("Big Moves") using Option Greeks and Order Book/Volume confirmation.

---

## 1. Core Concept: Why Universal Rules Beat Static Numbers

Because **Theta**, **Vega**, and **Rho** scale dynamically based on the underlying index/stock price and Days to Expiry (DTE), hardcoding static numbers (e.g., `Vega = 10.6`) will fail across different strikes and expiry cycles. 

To build a **Global Daily Scanner**, we use **Normalized Delta Zones**, **Rate-of-Change (Velocity)**, and **Relative Greek Ratios** that work universally across NIFTY, BANKNIFTY, FINNIFTY, and individual F&O stocks.

---

## 2. Universal Greeks Range Matrix (Breakout Zone)

When an option transitions from Out-of-The-Money (OTM) / At-The-Money (ATM) into In-The-Money (ITM), it hits the **Acceleration Sweet Spot**. 

| Metric | Call Option (CE) Target | Put Option (PE) Target | Condition / Behavior |
| :--- | :--- | :--- | :--- |
| **Entry Trigger Delta** | $+0.42 \text{ to } +0.48$ | $-0.42 \text{ to } -0.48$ | Pre-breakout coil (ATM/OTM boundary) |
| **Momentum Sweet Spot** | **$+0.50 \text{ to } +0.62$** | **$-0.50 \text{ to } -0.62$** | **Confirmed Big Move Zone (ITM Cross)** |
| **Gamma ($\Gamma$)** | $\ge 0.0008$ (Stable/Rising) | $\ge 0.0008$ (Stable/Rising) | Ensures steady price acceleration without whip-saw |
| **Theta Decay Ratio** | $\frac{|\Theta|}{\text{Option Price}} < 0.15$ | $\frac{|\Theta|}{\text{Option Price}} < 0.15$ | Ensures time decay isn't eating the premium faster than Delta gains |
| **Vega / IV Trend** | Stable or Rising ($\Delta \text{IV} \ge 0$) | Stable or Rising ($\Delta \text{IV} \ge 0$) | Confirms institutional buying rather than short covering alone |

---

## 3. The Global Scanner Logic (Pseudo-Code / Algo Rules)

Run this evaluation every **1-minute or 3-minute candle** across all active strikes within $\pm 5$ strikes of the Spot Price.

### A. Bullish Big Move Formula (Scanning Call Options - CE)
Flag a **CE Strike** as `PRICE_INCREASING_MOMENTUM` when **ALL** conditions are met:

1. **Delta Breakout Cross:**
   $$\Delta_{t-1} < 0.50 \quad \text{AND} \quad 0.50 \le \Delta_{t} \le 0.62$$
2. **Delta Velocity (Momentum Slope):**
   $$\Delta_{t} - \Delta_{t-3} \ge +0.03 \quad \text{(Delta gained at least 0.03 in last 3 candles)}$$
3. **Theta-to-Premium Safety Filter:**
   $$\frac{|\Theta_{t}|}{\text{Close}_{t}} \le 0.15$$
4. **Order Flow & Price Confirmation:**
   * $\text{Close}_{t} > \text{ATP}_{t}$ (Price trading above Average Traded Price / VWAP)
   * $\text{TBQ}_{t} > \text{TSQ}_{t} \times 1.5$ (Total Buy Quantity is at least $1.5\times$ Total Sell Quantity)

---

### B. Bearish Big Move Formula (Scanning Put Options - PE)
Flag a **PE Strike** as `PRICE_INCREASING_MOMENTUM` when **ALL** conditions are met:

1. **Delta Breakout Cross:**
   $$\Delta_{t-1} > -0.50 \quad \text{AND} \quad -0.62 \le \Delta_{t} \le -0.50$$
2. **Delta Velocity (Momentum Slope):**
   $$|\Delta_{t}| - |\Delta_{t-3}| \ge +0.03 \quad \text{(Absolute Delta expanded by at least 0.03 in last 3 candles)}$$
3. **Theta-to-Premium Safety Filter:**
   $$\frac{|\Theta_{t}|}{\text{Close}_{t}} \le 0.15$$
4. **Order Flow & Price Confirmation:**
   * $\text{Close}_{t} > \text{ATP}_{t}$ (Put premium trading above its VWAP/ATP)
   * $\text{TBQ}_{t} > \text{TSQ}_{t} \times 1.5$ (Strong buyer dominance on the Put order book)

---

## 4. Combined "Big Move Score" (0 to 100 Ranking Formula)

If multiple strikes trigger at the same time during an option chain scan, rank them using this composite score to pick the single best contract:

$$\text{Move Score} = (W_1 \times \text{Delta\_Score}) + (W_2 \times \text{OrderBook\_Ratio}) + (W_3 \times \text{OI\_Shift})$$

* **$\text{Delta\_Score}$ (40% Weight):** $100$ if $|\Delta|$ is between $0.50$ and $0.58$; scales down to $0$ outside $[0.45, 0.65]$.
* **$\text{OrderBook\_Ratio}$ (35% Weight):** $\min\left(\frac{\text{TBQ}}{\text{TSQ}} \times 25, 100\right)$
* **$\text{OI\_Shift}$ (25% Weight):** 
  * **Long Buildup (Fresh Buying):** Price $\uparrow$ and Open Interest ($\text{OI}$) $\uparrow$ $\rightarrow +100$ points.
  * **Short Covering (Squeeze):** Price $\uparrow$ and Open Interest ($\text{OI}$) $\downarrow$ $\rightarrow +80$ points *(Note: Short covering rallies, like the 11:55 AM – 12:17 PM phase in your sample data where OI dropped from 14.9M to 10.2M, often produce the fastest vertical spikes)*.

---

## 5. Risk & Exit Triggers (When the Big Move Ends)

Exit or trail stop-loss tightly when any of the following occur:
1. **Delta Exhaustion:** $|\Delta|$ exceeds **$0.65$** (option becomes deep ITM; risk-to-reward diminishes).
2. **Theta Spike / IV Crush:** Implied Volatility ($\text{IV}$) drops sharply while price stalls ($\text{Close}_{t} < \text{Open}_{t}$ for 2 consecutive candles).
3. **Order Book Flip:** $\text{TBQ}$ drops below $\text{TSQ}$.