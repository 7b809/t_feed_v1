# G1 v4.2 Strategy Specification: Cross-Index Relational Isolation with Regime-Gated Multi-Path Execution & Adaptive ATR/Rupee Trailing Engine

**Document Version:** 4.2.0  
**Status:** Approved for Implementation  
**Supersedes:** G1 v4.1, G1 v4.0, G1 v3.2  
**Scope:** Multi-Index Opening Range Boundary Surveillance (`MIDCPNIFTY`, `BANKNIFTY`, `NIFTY 50`, `SENSEX`), Relational NIFTY Strike Isolation, Source-Aware Branching Execution (Path A: Internal NIFTY Self-Trigger vs. Path B: External Leading Index Trigger), Continuous SR Multi-Order Placement, Vectorized Market Regime Classification (ADX Trend Filtering, Bollinger Bandwidth Volatility Compression, EMA Alignment, and RSI Exhaustion Bands), Adaptive Dynamic Trailing Stop Engine (ATR Mode, Rupee Mode, Hybrid Maximum Protection), and Multi-Tier Capital Protection (Consecutive Loss Sizing Throttles and Daily Max Drawdown Circuit Breakers).

---

## 0. Version Evolution Matrix (v4.0 vs. v4.1 vs. v4.2)

| Feature / Area | G1 v4.0 Behaviour | G1 v4.1 Behaviour | G1 v4.2 Reconciled Behaviour |
| :--- | :--- | :--- | :--- |
| **Strike Isolation** | NIFTY ATM CE ($R3$) / PE ($S3$) | Unchanged ($R3 \rightarrow \text{CE}$, $S3 \rightarrow \text{PE}$) | Unchanged: Multi-index touch establishes relational bias; execution isolates to nearest round-50 NIFTY strike. |
| **Execution Paths** | Single path: 10-min fast pivot scan + auto-reverse | Split into Path A (Self-NIFTY) and Path B (External Index) | Retained & Enhanced: Path A (single-order pivot bounce/auto-reverse) and Path B (continuous SR multi-order watch). |
| **Market Regime Filter** | None: blind entry on boundary touch | None: flat/choppy markets suffer whipsaws | **Integrated Market Regime Detector**: Evaluates ADX(14) $\ge 22.0$, Bollinger Bandwidth(20, 2) compression, EMA 9/21/200 alignment, and RSI(14) bounds. Flat `RANGING` markets are strictly gated. |
| **Volatility Squeeze Filter** | None | None | **Compression Detection**: Identifies volatility squeeze ($\text{BB Bandwidth} < 0.015$) as early breakout warning before momentum expansion. |
| **Path A Trailing Stop** | Fixed LTP-tier stop-loss only; no trailing | Fixed LTP-tier stop-loss only; no trailing | **Dynamic ATR Trailing Stop**: Activates at $+1.2 \times \text{ATR}$ (ratchets stop to break-even), and trails at $+0.5 \times \text{ATR}$ step increments. |
| **Path B Trailing Stop** | N/A | Rupee-based trailing ($₹1,000$ activation, $₹500$ trail gap, $₹1,000$ floor) | **Hybrid Trailing Stop**: Evaluates both Rupee-ladder and ATR-ladder stops simultaneously, ratcheting to whichever provides the higher locked-in floor. |
| **Consecutive Loss Guard** | None | None | **2-Tier Circuit Breaker**: $\ge 3$ consecutive stop-outs throttles position size by $50\%$; $\ge 5$ consecutive stop-outs halts all strategy operations for the day. |
| **Daily Drawdown Guard** | None | None | **Capital Safety Gate**: Automatically locks the strategy and freezes new order creation if cumulative session drawdown exceeds $₹3,000$ (or $4.0\%$ equity limit). |
| **Bearish Regime Exit** | None | 2 of 3 criteria during `TRAILING` mode | Retained & Enhanced: In active trailing mode, exits early if 2 consecutive bearish candles, NIFTY below support, or lower close sequence confirm. |
| **Strategy Lockdown** | On promotion | On target hit (Path B) or promotion (Path A) | Comprehensive Lockdown: Target hit, daily drawdown limit, or 5 consecutive losses immediately triggers session freeze. |

---

## 1. Executive Summary & Core Architectural Premise

The G1 strategy architecture operates on the principle of **cross-index lead-lag momentum** combined with **centralized liquid execution** in Indian equity derivatives. High-beta and benchmark sectoral indices (`MIDCPNIFTY`, `BANKNIFTY`, `NIFTY 50`, `SENSEX`) frequently lead directional breakouts. 

When any monitored index breaches its Opening Range extreme boundary ($R3$ or $S3$), the market's directional posture is established. To eliminate liquidity friction, execution is relationally mapped exclusively to NIFTY 50 options contracts (nearest round-50 strike to NIFTY spot LTP).

G1 v4.2 introduces a critical supervisory layer: **The Market Regime Gatekeeper and Dynamic ATR Trailing Engine**. While boundary touches signal potential momentum, entering during flat or sideways consolidation (`RANGING` regime where $\text{ADX} < 22$) produces catastrophic whipsaws. G1 v4.2 guarantees that:
1. Entries are validated by directional trend strength ($\text{ADX} \ge 22$) or confirmed breakout from volatility squeeze (`COMPRESSION`).
2. Open positions lock in accrued gains via an adaptive trailing stop (moving to break-even at $+1.2 \times \text{ATR}$ and stepping by $+0.5 \times \text{ATR}$).
3. Downside risk is clamped by consecutive loss throttling and a hard daily drawdown circuit breaker.

```
                           ┌─────────────────────────────────────────────────────────┐
                           │       MULTI-INDEX BOUNDARY SURVEILLANCE (Step 4)        │
                           │   • MIDCPNIFTY (Rank 1)      • BANKNIFTY (Rank 2)       │
                           │   • NIFTY 50   (Rank 3)      • SENSEX    (Rank 4)       │
                           └────────────────────────────┬────────────────────────────┘
                                                        │
                                      First Boundary Touch Detected (R3 / S3)
                                                        │
                                                        ▼
                           ┌─────────────────────────────────────────────────────────┐
                           │          RELATIONAL NIFTY STRIKE ISOLATION              │
                           │  • R3 Touch  ──> Bias CE ──> NIFTY ATM CE (Primary)     │
                           │                              NIFTY ATM PE (Counter)    │
                           │  • S3 Touch  ──> Bias PE ──> NIFTY ATM PE (Primary)     │
                           │                              NIFTY ATM CE (Counter)    │
                           └────────────────────────────┬────────────────────────────┘
                                                        │
                                                        ▼
                           ┌─────────────────────────────────────────────────────────┐
                           │          MARKET REGIME DETECTOR & ADX GATE (v4.2)       │
                           │  • ADX(14) >= 22.0  &  EMA 9/21/200 Trend Alignment     │
                           │  • Bollinger Bandwidth Volatility Compression (squeeze) │
                           │  • RSI(14) Boundary Checks (CE: 45–68, PE: 32–55)       │
                           │                                                         │
                           │  [ If RANGING (ADX < 22) ──> GATE / SUPPRESS ENTRY ]    │
                           └────────────────────────────┬────────────────────────────┘
                                                        │
                               ┌────────────────────────┴────────────────────────┐
                               │                                                 │
                    ┌──────────▼──────────┐                           ┌──────────▼──────────┐
                    │ TRIGGER = NIFTY 50  │                           │ TRIGGER = EXTERNAL  │
                    │      (Path A)       │                           │      (Path B)       │
                    │                     │                           │                     │
                    │ 10-min fast scan    │                           │ ❌ 10-min scan OFF  │
                    │ left=15 right=3     │                           │ ❌ auto-reverse OFF │
                    │ Auto-reverse ON     │                           │                     │
                    │ Support: BUY bias   │                           │ Continuous SR watch │
                    │ Resist : BUY counter│                           │ on bias instrument  │
                    │ Single order / day  │                           │                     │
                    │                     │                           │ Support  → BUY bias │
                    │ ATR Trailing Stop:  │                           │ Resist   → BUY count│
                    │ • +1.2 ATR -> B/E   │                           │ Multi-order setup   │
                    │ • +0.5 ATR trail    │                           │                     │
                    │                     │                           │ Hybrid Trailing:    │
                    │                     │                           │ max(Rupee, ATR stop)│
                    └──────────┬──────────┘                           └──────────┬──────────┘
                               │                                                 │
                               └─────────────────────┬───────────────────────────┘
                                                     ▼
                           ┌─────────────────────────────────────────────────────────┐
                           │           CIRCUIT BREAKERS & CAPITAL DEFENSE (v4.2)     │
                           │  • 3 Consecutive Losses ──> 50% Position Size Throttle  │
                           │  • 5 Consecutive Losses ──> Session Trading Halt        │
                           │  • Cumulative Loss >= ₹3,000 ──> Immediate Lockdown     │
                           └─────────────────────────────────────────────────────────┘
```

---

## 2. Multi-Index Priority Arbitration & Relational Mapping

### 2.1 Index Surveillance Universe
The engine samples 2-second tick snapshots across four indices during market hours:

| Index Symbol | Instrument Key | Exchange | Segment | Priority Rank | Strike Step |
| :--- | :--- | :--- | :--- | :---: | :---: |
| `MIDCPNIFTY` | `NSE_INDEX\|NIFTY MID SELECT` | NSE | `NSE_INDEX` | **1** | 25 |
| `BANKNIFTY` | `NSE_INDEX\|Nifty Bank` | NSE | `NSE_INDEX` | **2** | 100 |
| `NIFTY` | `NSE_INDEX\|Nifty 50` | NSE | `NSE_INDEX` | **3** | 50 |
| `SENSEX` | `BSE_INDEX\|SENSEX` | BSE | `BSE_INDEX` | **4** | 100 |

### 2.2 Boundary Arbitration Rule
Priority resolution on simultaneous boundary hits:
$$\text{MIDCPNIFTY} \succ \text{BANKNIFTY} \succ \text{NIFTY} \succ \text{SENSEX}$$

Strike Selection Formula:
$$\text{NIFTY Strike} = \text{round}\left(\frac{\text{Current NIFTY Spot LTP}}{50}\right) \times 50$$

Path Branching Determination:
$$\text{trigger\_is\_self\_nifty} = (\text{trigger\_index} == \text{"NIFTY"})$$

---

## 3. Market Regime Detection Engine (G1 v4.2 Core)

### 3.1 Mathematical Formulations
1. **Wilder's Average True Range ($\text{ATR}_{14}$)**:
   $$\text{TR}_t = \max\left(H_t - L_t,\ |H_t - C_{t-1}|,\ |L_t - C_{t-1}|\right)$$
   $$\text{ATR}_t = \frac{\text{ATR}_{t-1} \times 13 + \text{TR}_t}{14}$$

2. **Average Directional Movement Index ($\text{ADX}_{14}$)**:
   $$+DM_t = \begin{cases} H_t - H_{t-1} & \text{if } H_t - H_{t-1} > L_{t-1} - L_t \text{ and } H_t - H_{t-1} > 0 \\ 0 & \text{otherwise} \end{cases}$$
   $$-DM_t = \begin{cases} L_{t-1} - L_t & \text{if } L_{t-1} - L_t > H_t - H_{t-1} \text{ and } L_{t-1} - L_t > 0 \\ 0 & \text{otherwise} \end{cases}$$
   $$\text{SmoothTR}_t = \text{SmoothTR}_{t-1} - \frac{\text{SmoothTR}_{t-1}}{14} + \text{TR}_t$$
   $$+DI_t = 100 \times \frac{\text{SmoothPlusDM}_t}{\text{SmoothTR}_t},\quad -DI_t = 100 \times \frac{\text{SmoothMinusDM}_t}{\text{SmoothTR}_t}$$
   $$DX_t = 100 \times \frac{|+DI_t - -DI_t|}{+DI_t + -DI_t}$$
   $$\text{ADX}_t = \frac{\text{ADX}_{t-1} \times 13 + DX_t}{14}$$

3. **Bollinger Bandwidth ($\text{BB Bandwidth}_{20, 2}$)**:
   $$\mu_{20} = \frac{1}{20}\sum_{i=0}^{19} C_{t-i},\quad \sigma_{20} = \sqrt{\frac{1}{20}\sum_{i=0}^{19} (C_{t-i} - \mu_{20})^2}$$
   $$\text{Bandwidth} = \frac{\text{Upper Band} - \text{Lower Band}}{\mu_{20}} = \frac{4 \times \sigma_{20}}{\mu_{20}}$$

### 3.2 Regime Classification State Machine
```python
if bb_bandwidth < STRATEGY_REGIME_BB_BANDWIDTH_MIN:  # 0.015
    regime = "COMPRESSION"
elif adx >= STRATEGY_REGIME_MIN_ADX:                # 22.0
    if fast_ema > slow_ema and close >= trend_ema:
        regime = "TRENDING_BULL"
    elif fast_ema < slow_ema and close <= trend_ema:
        regime = "TRENDING_BEAR"
    else:
        regime = "RANGING"
else:
    regime = "RANGING"
```

### 3.3 Flat Market Protection Rule
When `STRATEGY_REGIME_GATE_ENTRIES == True`:
* If regime evaluates to `RANGING`, signal generation is held in `waiting` state (`waiting_for_trending_regime: adx < 22`).
* Zero long/short orders are placed on Path A or Path B while `RANGING` persists.
* Breakout from `COMPRESSION` is permitted once directional expansion occurs.

---

## 4. Path A Execution Pipeline (Self-NIFTY Trigger)

Executes when `trigger_is_self_nifty == True`:
* **Scan Window:** $[T_{\text{touch}},\ T_{\text{touch}} + 10\text{ min}]$.
* **Pivot Structure:** Fast `left=15, right=3` confirmation on primary bias instrument.
* **Support Confirmation ($S$):** Indicates continuation $\rightarrow$ BUY primary bias instrument.
* **Resistance Confirmation ($R$):** Indicates rejection $\rightarrow$ BUY counter instrument (auto-reverse).
* **Initial Stop:** Dynamic LTP-tier bracket ($5\%\text{--}15\%$, capped at $35\%$).
* **Target:** Nearest confirmed resistance above entry on target contract.
* **G1 v4.2 Trailing Stop:**
  $$\text{Activation Advance} = 1.2 \times \text{ATR}_{14}$$
  $$\text{Step Size} = 0.5 \times \text{ATR}_{14}$$
  1. Once price reaches $\text{Entry} + 1.2 \times \text{ATR}_{14}$, stop moves to break-even ($\text{Entry}$).
  2. For every additional $+0.5 \times \text{ATR}_{14}$ advance, stop ratchets up by $+0.5 \times \text{ATR}_{14}$.
  3. Pullback to trailing stop executes `TRAILING_STOP_HIT` exit.

---

## 5. Path B Execution Pipeline (External Index Trigger)

Executes when `trigger_is_self_nifty == False`:
* 10-minute scan window and auto-reverse are **DISABLED**.
* The bias-side instrument remains isolated for the complete trading day.
* Continuous SR engine consumes confirmed levels on the bias instrument:
  * **Support confirmed on bias side $\rightarrow$ BUY bias-side instrument**.
  * **Resistance confirmed on bias side $\rightarrow$ BUY counter-side instrument**.
* **Lot Sizing:** NIFTY = 65, SENSEX = 20. Other symbols are telemetry-only.

### 5.1 Hybrid Trailing Stop Engine (G1 v4.2)
Orders evaluate both Rupee-based and ATR-based trailing levels simultaneously:
1. **Rupee Ladder**:
   $$\text{lock\_rs} = \max(1000,\ \text{peak\_profit\_rs} - 500)$$
   $$\text{Stop}_{\text{rupee}} = \text{Entry} + \frac{\text{lock\_rs}}{\text{Lot Size}}$$
2. **ATR Ladder**:
   $$\text{Stop}_{\text{atr}} = \text{Entry} + \left\lfloor\frac{\text{Advance} - 1.2 \times \text{ATR}}{0.5 \times \text{ATR}}\right\rfloor \times (0.5 \times \text{ATR})$$
3. **Hybrid Execution Floor**:
   $$\text{Trailing Stop Price} = \max(\text{Stop}_{\text{rupee}},\ \text{Stop}_{\text{atr}})$$

The trailing stop never declines.

### 5.2 Bearish Regime Early Exit
During `TRAILING` mode, order exits immediately if at least 2 of 3 conditions hold:
1. Two consecutive completed bearish candles on traded instrument.
2. NIFTY 50 LTP drops below the originating support pivot level.
3. NIFTY 50 forms two consecutive lower closes.

---

## 6. Dynamic Risk & Capital Protection Engine

### 6.1 Consecutive Loss Circuit Breakers
To prevent capital decay during prolonged choppy sessions:
* **Level 1 Throttle (3 Consecutive Losses):**
  When 3 consecutive stop-losses occur in a rolling 24-hour window, position size is reduced by $50\%$ ($\text{lot\_size} = \max(1,\ \text{lot\_size} // 2)$).
* **Level 2 Halt (5 Consecutive Losses):**
  When 5 consecutive stop-losses occur, the engine triggers an automatic session halt (`strategy_locked_today = True`, `lock_reason = "consecutive_losses_circuit_breaker"`).

### 6.2 Daily Max Drawdown Circuit Breaker
* Evaluates total net realized PnL across all closed trades in the session.
* If net realized loss breaches the hard threshold:
  $$\text{Daily Net Realized Loss} \le -₹3,000 \quad (\text{or } -4.0\% \text{ nominal})$$
* Strategy is immediately frozen (`strategy_locked_today = True`). All pending orders are cancelled and no new SR levels produce orders.

---

## 7. Configuration Dictionary Reference (`core/config.py`)

```python
# Market Regime Detection
STRATEGY_REGIME_FILTER_ENABLED = True      # Master regime toggle
STRATEGY_REGIME_MIN_ADX = 22.0             # ADX threshold for trending qualification
STRATEGY_REGIME_BB_PERIOD = 20             # Bollinger Band lookback
STRATEGY_REGIME_BB_STD = 2.0               # Bollinger Band standard deviations
STRATEGY_REGIME_BB_BANDWIDTH_MIN = 0.015   # Volatility compression squeeze threshold
STRATEGY_REGIME_FAST_EMA = 9               # Fast EMA period
STRATEGY_REGIME_SLOW_EMA = 21              # Slow EMA period
STRATEGY_REGIME_TREND_EMA = 200            # Macro trend EMA period
STRATEGY_REGIME_RSI_PERIOD = 14            # RSI lookback
STRATEGY_REGIME_RSI_OVERBOUGHT = 68.0      # CE bullish exhaustion ceiling
STRATEGY_REGIME_RSI_OVERSOLD = 32.0        # PE bearish exhaustion floor
STRATEGY_REGIME_GATE_ENTRIES = True        # Gate entries during RANGING markets

# Dynamic Trailing Stop Engine
STRATEGY_TRAILING_STOP_ENABLED = True      # Trailing stop master toggle
STRATEGY_TRAILING_STOP_MODE = "hybrid"     # "atr" | "rupee" | "hybrid"
STRATEGY_TRAILING_ACTIVATION_PROFIT_ATR = 1.2  # ATR activation multiple (+1.2 ATR -> Breakeven)
STRATEGY_TRAILING_TRAIL_STEP_ATR = 0.5     # Trailing ratchet step (+0.5 ATR)
STRATEGY_ATR_PERIOD = 14                   # Wilder's ATR period

# Circuit Breakers & Risk Governance
STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_THROTTLE = 3  # Sizing throttle threshold (50% lot)
STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_HALT = 5      # Full session trading halt threshold
STRATEGY_RISK_DAILY_MAX_DRAWDOWN_RS = 3000.0       # Daily net loss limit in Rupees
STRATEGY_RISK_DAILY_MAX_DRAWDOWN_PCT = 0.04        # Daily net loss limit in % equity
```

---

## 8. Verification & Test Suite Summary

The G1 v4.2 implementation is validated by automated unit tests in `tests/test_g1_v4_2.py`:
* `TestG1V42Indicators`: Validates True Range, Wilder's ATR(14), Bollinger Bandwidth, and ADX directional movement index against analytical baselines.
* `TestMarketRegimeDetector`: Confirms deterministic classification of `COMPRESSION`, `TRENDING_BULL`, `TRENDING_BEAR`, and `RANGING`.
* `TestDummyOrderTrailingAndRisk`: Validates Path A ATR trailing stop breakeven activation and ratcheting, Path B hybrid trailing stop, 3-loss position sizing throttle, 5-loss circuit breaker, and ₹3,000 daily max drawdown lockdown.
* `TestStrategyEntrySignalRegimeGate`: Verifies that `_compute_candidate()` holds signals in `waiting` (`waiting_for_trending_regime`) when ADX < 22 during flat market noise.
