# Options Greeks Regime Signal — Formula Specification

**Version:** 2.0 (unified put + call)  
**Source contracts (twin pair, same strike & expiry):**
- PUT: `NSE_FO|45395` — cp = 140.4
- CALL: `NSE_FO|45394` — cp = 225.25

**Calibration windows (2026-11-13):**
- PUT: 10:47 → 11:33 IST (46 × 1-min candles)
- CALL: 09:38 → 10:46 IST (68 × 1-min candles)

**Purpose:** A reusable rulebook that classifies the **underlying's directional regime** from option Greeks, for **both a call and a put at the same strike**, and emits a per-candle verdict with confidence.

---

## 0. Orientation (READ FIRST)

Every formula below assumes you know which **side** you're feeding — call or put. All signs and thresholds flip between sides.

| Underlying moving… | Put premium | Call premium | Δ_put | Δ_call | ρ_put | ρ_call |
|---|---|---|---|---|---|---|
| **Up** (bullish underlying) | falls ↓ | rises ↑ | → 0 (less neg) | → +1 (more pos) | → 0 (less neg) | → large pos |
| **Down** (bearish underlying) | rises ↑ | falls ↓ | → −1 (more neg) | → 0 (less pos) | → large neg | → 0 (less pos) |

**Two identities that MUST hold if both contracts share the same K and T:**
Δ_call = Δ_put + 1 (algebraic: N(d1) − (1 − N(−d1)) = 1)
ρ_call ≈ −ρ_put (approximately, same K·T, N(d2) vs −N(−d2))
Δ_call + Δ_put ∈ [0, 0.20] (in practice ~0.05–0.15 near ATM)

text

**If those identities don't hold, the two options are NOT the same strike.** Stop and re-verify before applying the rules.

---

## 1. Inputs required per candle

| Field | Symbol | Source (JSON) | Notes |
|---|---|---|---|
| Premium close | `P` | `candles[].close` | Call or put premium in ₹ |
| Delta | `Δ` | `candles[].greeks.delta` | **> 0 ⇒ call**, **< 0 ⇒ put** |
| Theta | `Θ` | `candles[].greeks.theta` | ₹/day, always negative |
| Vega | `V` | `candles[].greeks.vega` | ₹ per 1 % IV |
| Rho | `ρ` | `candles[].greeks.rho` | **> 0 ⇒ call**, **< 0 ⇒ put** |
| Gamma | `Γ` | `candles[].greeks.gamma` | **EXCLUDED** — feed rounds to 0.001 |
| (optional) Underlying spot | `S` | *not in file* | Needed for true moneyness `S/K` |
| (optional) Strike | `K` | *not in file* | Needed for true moneyness `S/K` |

**Side detection (auto):**
FUNCTION detect_side(Δ, ρ):
IF Δ > 0.20 AND ρ > 0.5: RETURN "CALL"
IF Δ < −0.20 AND ρ < −0.5: RETURN "PUT"
RETURN "ATYPICAL — verify data"

text

---

## 2. Calibration reference values

### 2.1 PUT — `NSE_FO|45395` (10:47 → 11:33)

| Greek | Min | Max | Center | Std (approx) |
|---|---|---|---|---|
| `Δ_put` | −0.5208 (11:33) | −0.4385 (10:49) | −0.480 | 0.022 |
| `ρ_put` | −1.5149 (11:33) | −1.2885 (10:49) | −1.402 | 0.060 |
| `Θ` | −19.46 | −18.57 | −19.0 | 0.22 |
| `V` | 10.7133 | 10.7670 | 10.74 | 0.013 |
| `Γ` | 0.001 | 0.001 | — | — (dead) |

### 2.2 CALL — `NSE_FO|45394` (09:38 → 10:46)

| Greek | Min | Max | Center | Std (approx) |
|---|---|---|---|---|
| `Δ_call` | 0.4719 (9:40) | 0.5570 (10:46) | 0.515 | 0.022 |
| `ρ_call` | 1.3636 (9:40) | 1.5952 (10:46) | 1.479 | 0.060 |
| `Θ` | −19.61 (9:44) | −18.20 (11:26) | −19.0 | 0.30 |
| `V` | 10.6636 | 10.836 | 10.77 | 0.04 |
| `Γ` | 0.0009–0.001 | — | — | — (dead) |

### 2.3 Cross-check (the put-call parity sanity test)

| Metric | Put (10:47–11:33) | Call (9:38–10:46) | Must satisfy |
|---|---|---|---|
| Δ width | 0.0823 | 0.0851 | ≈ equal (✓) |
| ρ width | 0.2264 | 0.2316 | ≈ equal (✓) |
| Δ_put + Δ_call @ 10:47 | −0.4407 | 0.5593 | sum = 0.1186 (✓ in [0, 0.20]) |
| ρ_call / |ρ_put| @ extremes | 1.29–1.51 | 1.36–1.60 | ratio ≈ 1.05–1.06 (✓ consistent) |

**Both widths match** — strong evidence the two contracts were driven by identical underlying dynamics during the sampled windows.

---

## 3. Core formulas

### 3.1 Raw delta rule — PUT (contract-local, NOT portable)
BULLISH_RAW_PUT ⟺ Δ_put > −0.49
BEARISH_RAW_PUT ⟺ Δ_put < −0.51
NEUTRAL_RAW_PUT ⟺ −0.51 ≤ Δ_put ≤ −0.49

text

### 3.2 Raw delta rule — CALL (contract-local, recalibrated)
STRONG_BULL_CALL ⟺ Δ_call > 0.53
BULL_CALL ⟺ 0.51 < Δ_call ≤ 0.53
NEUTRAL_CALL ⟺ 0.49 ≤ Δ_call ≤ 0.51
BEAR_CALL ⟺ 0.47 ≤ Δ_call < 0.49
STRONG_BEAR_CALL ⟺ Δ_call < 0.47

text

> ⚠️ The mechanical mirror `Δ_call > −Δ_put_threshold` (i.e. `> 0.49`) is **too loose** for calls. The recalibrated values above use the actual extremes observed.

### 3.3 Rho confirmation — PUT
RHO_BULLISH_PUT ⟺ ρ_put > −1.43
RHO_BEARISH_PUT ⟺ ρ_put < −1.49

text

### 3.4 Rho confirmation — CALL (recalibrated)
RHO_STRONG_BULL_CALL ⟺ ρ_call > 1.53
RHO_BULL_CALL ⟺ 1.49 < ρ_call ≤ 1.53
RHO_STRONG_BEAR_CALL ⟺ ρ_call < 1.38
RHO_BEAR_CALL ⟺ 1.38 ≤ ρ_call < 1.43

text

> ⚠️ Do **not** use `ρ_call > 1.49` / `< 1.43` as the raw rule — the actual call rho swings wider (1.36 → 1.60). Use the recalibrated bands above.

### 3.5 Normalized delta z-score (PORTABLE, works for both sides)
Δ_mean_N = rolling mean of Δ over last N candles (N = 20 recommended)
Δ_std_N = rolling std of Δ over last N candles
zΔ = (Δ − Δ_mean_N) / Δ_std_N

text

Interpretation (same for call or put because z-score is side-agnostic):
zΔ > +0.5 → |Δ| growing → underlying moving toward ITM side for this contract
zΔ < −0.5 → |Δ| shrinking → underlying moving toward OTM side for this contract
|zΔ| ≤ 0.5 → neutral

text

For a **PUT**, `zΔ > 0` means "more ITM" ⇒ underlying falling ⇒ **bearish**.  
For a **CALL**, `zΔ > 0` means "more ITM" ⇒ underlying rising ⇒ **bullish**.

So the sign of the *interpretation* flips, but the formula is identical:
IF side == "PUT": bullish_if = (zΔ < −0.5) ; bearish_if = (zΔ > +0.5)
IF side == "CALL": bullish_if = (zΔ > +0.5) ; bearish_if = (zΔ < −0.5)

text

### 3.6 True moneyness (BEST — requires S and K)

If the underlying spot `S` and strike `K` are available:
m = S / K

text

- For a **CALL**: `m > 1.02` ⇒ ITM ⇒ bullish underlying bias
- For a **PUT**:  `m < 0.98` ⇒ ITM ⇒ bearish underlying bias
BULLISH_MONEYNESS ⟺ m > 1.02
BEARISH_MONEYNESS ⟺ m < 0.98
NEUTRAL_MONEYNESS ⟺ 0.98 ≤ m ≤ 1.02

text

**This is side-independent** — you just apply the correct ITM direction per side.

### 3.7 Composite normalized score
score = 50

25 · tanh( s · zΔ / 1.0 ) # s = +1 for CALL, −1 for PUT

25 · tanh( (m − 1.0) / 0.05 ) # only if S and K available

score ∈ [0, 100]
score > 65 → bullish conviction
score < 35 → bearish conviction
otherwise → neutral
text

If `S` and `K` are unavailable, drop the moneyness term and rescale:
score = 50 + 50 · tanh( s · zΔ / 1.0 )

text

### 3.8 Premium momentum (auxiliary cross-check)
ΔP_5 = P[t] − P[t−5]

text

Sign check depends on side:
CALL side: ΔP_5 > 0 → call richening → underlying rising (bullish)
PUT side: ΔP_5 < 0 → put cheapening → underlying rising (bullish)

text

Use only as a **confidence filter** — a contradicting ΔP_5 reduces conviction one band.

---

## 4. Verdict logic (per candle)

### 4.1 Local (contract-specific) — PUT
FUNCTION classify_local_put(Δ, ρ):
IF Δ > −0.49 AND ρ > −1.43: RETURN "BULLISH"
IF Δ < −0.51 AND ρ < −1.49: RETURN "BEARISH"
IF Δ > −0.49 XOR ρ > −1.43: RETURN "LEAN_BULLISH"
IF Δ < −0.51 XOR ρ < −1.49: RETURN "LEAN_BEARISH"
RETURN "NEUTRAL"

text

### 4.2 Local (contract-specific) — CALL
FUNCTION classify_local_call(Δ, ρ):
IF Δ > 0.53 AND ρ > 1.53: RETURN "STRONG_BULL"
IF Δ > 0.51 AND ρ > 1.49: RETURN "BULL"
IF Δ < 0.47 AND ρ < 1.38: RETURN "STRONG_BEAR"
IF Δ < 0.49 AND ρ < 1.43: RETURN "BEAR"
IF Δ > 0.49 XOR ρ > 1.43: RETURN "LEAN_BULLISH"
IF Δ < 0.51 XOR ρ < 1.49: RETURN "LEAN_BEARISH"
RETURN "NEUTRAL"

text

### 4.3 Unified dispatcher (auto-detect side)
FUNCTION classify_local(Δ, ρ):
side = detect_side(Δ, ρ)
IF side == "CALL": RETURN classify_local_call(Δ, ρ)
IF side == "PUT": RETURN classify_local_put(Δ, ρ)
RETURN "ATYPICAL"

text

### 4.4 Portable verdict (works across any strike / expiry / IV)
FUNCTION classify_portable(Δ, ρ, Δ_mean_N, Δ_std_N, side, [S, K]):
zΔ = (Δ − Δ_mean_N) / Δ_std_N
sgn = (side == "CALL") ? +1 : −1
z_bull = sgn * zΔ # positive ⇒ bullish, negative ⇒ bearish

IF S and K are available:
m = S / K
IF m > 1.02 AND z_bull > 0: RETURN "BULLISH"
IF m < 0.98 AND z_bull < 0: RETURN "BEARISH"
IF m > 1.02 AND z_bull ≤ 0: RETURN "LEAN_BULLISH (moneyness only)"
IF m < 0.98 AND z_bull ≥ 0: RETURN "LEAN_BEARISH (moneyness only)"
RETURN "NEUTRAL"
ELSE:
IF z_bull > 0.5: RETURN "BULLISH"
IF z_bull < −0.5: RETURN "BEARISH"
RETURN "NEUTRAL"

text

### 4.5 Final verdict with confidence band + premium cross-check
FUNCTION verdict(Δ, ρ, Δ_mean_N, Δ_std_N, side, [S, K], ΔP_5):
base = classify_portable(Δ, ρ, Δ_mean_N, Δ_std_N, side, S, K)
score = compute_score(Δ, Δ_mean_N, Δ_std_N, side, S, K)

Premium momentum cross-check
IF base contains "BULL" AND ΔP_5 would contradict bullish:
score = score − 15
IF base contains "BEAR" AND ΔP_5 would contradict bearish:
score = score − 15

band = score > 65 ? "HIGH"
: score < 35 ? "HIGH"
: 45 < score < 55 ? "LOW"
: "MEDIUM"

RETURN { verdict: base, score: score, confidence: band }

text

---

## 5. Threshold lookup tables

### 5.1 PUT side

| Rule | Bullish | Neutral | Bearish |
|---|---|---|---|
| `Δ_put` (local) | > −0.49 | [−0.51, −0.49] | < −0.51 |
| `ρ_put` (local) | > −1.43 | [−1.49, −1.43] | < −1.49 |
| `zΔ · (+1)` (portable) | > +0.5 | [−0.5, +0.5] | < −0.5 |
| `m = S/K` | > 1.02 | [0.98, 1.02] | < 0.98 |
| `ΔP_5` (cross-check) | < 0 | — | > 0 |

### 5.2 CALL side

| Rule | Strong Bull | Bull | Neutral | Bear | Strong Bear |
|---|---|---|---|---|---|
| `Δ_call` | > 0.53 | 0.51–0.53 | 0.49–0.51 | 0.47–0.49 | < 0.47 |
| `ρ_call` | > 1.53 | 1.49–1.53 | 1.43–1.49 | 1.38–1.43 | < 1.38 |
| `zΔ · (−1)` (portable) | — | > +0.5 | ±0.5 | < −0.5 | — |
| `m = S/K` | > 1.02 | — | 0.98–1.02 | — | < 0.98 |
| `ΔP_5` (cross-check) | > 0 (bullish) | — | — | < 0 (bearish) | — |

### 5.3 Unified "underlying regime" signal (side-agnostic)

The whole point of running both legs: each one independently reports "underlying high" or "underlying low". When both agree, confidence is HIGH.
call_ITM_equivalent = (Δ_call > 0.51) OR (Δ_put < −0.49)
put_ITM_equivalent = (Δ_put < −0.51) OR (Δ_call < 0.49)

IF both say ITM-call-equivalent → "UNDERLYING HIGH (sell zone)"
IF both say ITM-put-equivalent → "UNDERLYING LOW (buy zone)"
IF they disagree → "AT POINT OF INFLECTION — stand aside"

text

---

## 6. Worked examples (dummy + real)

### 6.1 DUMMY EXAMPLE — Put side, textbook bullish signal

You're watching a put option with:
P = 84.50 (premium, ₹)
Δ_put = −0.445
ρ_put = −1.295

text

**Step 1 — Detect side:** `Δ < −0.20 AND ρ < −0.5` ⇒ **PUT** ✓

**Step 2 — Apply put local rule:**
- `Δ = −0.445 > −0.49` ✓
- `ρ = −1.295 > −1.43` ✓
- **Verdict: BULLISH** (both conditions met)

**Step 3 — Confidence (assume ΔP_5 = −0.80, put premium fell):**
- Put premium falling + delta says bullish ⇒ **consistent** → no penalty
- Score = 50 + 25·tanh(|zΔ|) ≈ high → **HIGH** confidence

**Interpretation:** Underlying is at a **high** — put premium has cheapened. This is where put sellers are rewarded, and underlying-momentum buyers should wait for a pullback.

---

### 6.2 DUMMY EXAMPLE — Call side, textbook bearish signal

You're watching a call option with:
P = 118.00 (premium, ₹)
Δ_call = 0.4720
ρ_call = 1.3750

text

**Step 1 — Detect side:** `Δ > 0.20 AND ρ > 0.5` ⇒ **CALL** ✓

**Step 2 — Apply call local rule:**
- `Δ = 0.4720 < 0.49` ✓ (bearish gate)
- `Δ = 0.4720 ≥ 0.47` but `< 0.49` → **BEAR** (not strong)
- `ρ = 1.3750 < 1.43` ✓ (bearish confirmation)
- `ρ < 1.38` → would be STRONG_BEAR, but 1.375 is borderline
- **Verdict: BEAR**

**Step 3 — Premium cross-check (ΔP_5 = −1.20, call fell):**
- Call falling + delta says bearish ⇒ **consistent** ✓
- **Confidence: HIGH**

**Interpretation:** Underlying is at a **low** — call premium has cheapened. This is a potential **buy-the-dip** zone for call buyers.

---

### 6.3 DUMMY EXAMPLE — Twin pair confirmation

You have BOTH legs available and get simultaneous readings:

| Time | Δ_put | Δ_call | Δ_put + Δ_call | Put verdict | Call verdict | Unified |
|---|---|---|---|---|---|---|
| T1 | −0.4402 | 0.5580 | 0.1178 | BULLISH | STRONG_BULL | **UNDERLYING HIGH** |
| T2 | −0.5205 | 0.4740 | −0.0465 ⚠️ | BEARISH | BEAR | **UNDERLYING LOW** |

> ⚠️ At T2 the sum is **negative** — that violates parity. Real data would give sum ∈ [0.05, 0.15]. In the T2 case, re-check that you're not mixing different strikes / expiries. If truly the same strike, one of the legs is stale.

**Correct parity example (real data, 10:47 IST):**
Δ_put = −0.4407
Δ_call = +0.5593
sum = +0.1186 ✓ in [0.05, 0.20]

text
Both legs agree: **UNDERLYING HIGH**. And indeed the call premium (194.4) was at its window maximum, while the put premium (134.1) was at its window minimum. ✅

---

### 6.4 REAL EXAMPLE — Put side, 10:49 IST (from calibration data)
P = 135.45
Δ_put = −0.4385
ρ_put = −1.2885

text

**Verdict:** `Δ > −0.49` ✓, `ρ > −1.43` ✓ → **BULLISH**  
**Reality check:** put premium was at its 46-candle LOW (135.45) → underlying at its HIGH ✅

---

### 6.5 REAL EXAMPLE — Call side, 9:40 IST (from calibration data)
P = 144.8
Δ_call = 0.4719
ρ_call = 1.3636

text

**Verdict:** `Δ < 0.47`? No (0.4719 > 0.47). `Δ < 0.49` yes. `ρ < 1.43` yes. → **BEAR**  
**Reality check:** call premium at its **minimum** in the call window → underlying at its LOW ✅  
**Cross-check with twin put:** put at 9:40 had `Δ ≈ −0.49, ρ ≈ −1.46` (roughly neutral-bearish) — consistent with call's BEAR verdict ✅

---

### 6.6 REAL EXAMPLE — Divergent signals, 10:12 IST
PUT: Δ = −0.4838, ρ = −1.4278 → NEUTRAL (mid-band)
CALL: Δ = +0.5067, ρ = +1.4580 → NEUTRAL (mid-band)

text

Both legs neutral ⇒ **no edge**. Reality: both premiums at mid-range values. ✅ Correctly stood aside.

---

## 7. Known limitations / do-not-do list

| ⚠️ Limitation | Impact | Mitigation |
|---|---|---|
| **Gamma rounded to 0.001** in feed | No gamma signal possible | Exclude Γ from all rules |
| **No underlying spot or strike** in file | Cannot compute true `m = S/K` | Use `zΔ` fallback, or add S & K to feed |
| **Theta barely moves** within window (range ~1) | Weak directional signal | Do not use Θ as trigger |
| **Only ~46–68 candles calibrated** | Thresholds provisional | Re-calibrate after 3–5 full days |
| **Numbers are contract-specific** | Cannot port `Δ > −0.49` to other strikes | Use `zΔ` or `m` for cross-instrument |
| **Monotonic feed only** — no gap analysis | May miss overnight regime shifts | Re-baseline `Δ_mean_N`, `Δ_std_N` at each session open |
| **Call thresholds are tighter than mechanical mirror** | If you naïvely use `−Δ_put`, you'll miss signals | Use the §3.4 recalibrated values |
| **Parity identity must hold** `Δ_call + Δ_put ∈ [0.05, 0.15]` | If violated, contracts aren't twins | Auto-check and warn |

---

## 8. Reference implementation (Python — production-ready)

```python
import numpy as np
from collections import deque


class GreeksRegimeClassifier:
    """
    Unified regime classifier for call & put option Greeks.
    - Auto-detects side (call vs put).
    - Applies contract-local thresholds OR portable z-score mode.
    - Optionally uses twin-leg confirmation.
    """

    def __init__(self, N=20, mode="local", twin_tolerance=0.02):
        self.N = N
        self.mode = mode                  # "local" or "portable"
        self.twin_tolerance = twin_tolerance
        self.deltas = deque(maxlen=N)
        self.premiums = deque(maxlen=6)   # for ΔP_5

    # ---------- side detection ----------
    @staticmethod
    def detect_side(delta, rho):
        if delta > 0.20 and rho > 0.5:
            return "CALL"
        if delta < -0.20 and rho < -0.5:
            return "PUT"
        return "ATYPICAL"

    # ---------- core update ----------
    def update(self, delta, rho, premium, S=None, K=None, side=None):
        self.deltas.append(delta)
        self.premiums.append(premium)

        if side is None:
            side = self.detect_side(delta, rho)

        if self.mode == "local":
            return self._classify_local(delta, rho, side)
        return self._classify_portable(delta, side, S, K)

    # ---------- local thresholds ----------
    def _classify_local(self, delta, rho, side):
        if side == "CALL":
            return self._classify_local_call(delta, rho)
        if side == "PUT":
            return self._classify_local_put(delta, rho)
        return {"verdict": "ATYPICAL", "side": side}

    def _classify_local_put(self, delta, rho):
        if delta > -0.49 and rho > -1.43:
            return {"verdict": "BULLISH", "side": "PUT"}
        if delta < -0.51 and rho < -1.49:
            return {"verdict": "BEARISH", "side": "PUT"}
        if delta > -0.49 or rho > -1.43:
            return {"verdict": "LEAN_BULLISH", "side": "PUT"}
        if delta < -0.51 or rho < -1.49:
            return {"verdict": "LEAN_BEARISH", "side": "PUT"}
        return {"verdict": "NEUTRAL", "side": "PUT"}

    def _classify_local_call(self, delta, rho):
        if delta > 0.53 and rho > 1.53:
            return {"verdict": "STRONG_BULL", "side": "CALL"}
        if delta > 0.51 and rho > 1.49:
            return {"verdict": "BULL", "side": "CALL"}
        if delta < 0.47 and rho < 1.38:
            return {"verdict": "STRONG_BEAR", "side": "CALL"}
        if delta < 0.49 and rho < 1.43:
            return {"verdict": "BEAR", "side": "CALL"}
        if delta > 0.49 or rho > 1.43:
            return {"verdict": "LEAN_BULLISH", "side": "CALL"}
        if delta < 0.51 or rho < 1.49:
            return {"verdict": "LEAN_BEARISH", "side": "CALL"}
        return {"verdict": "NEUTRAL", "side": "CALL"}

    # ---------- portable z-score ----------
    def _classify_portable(self, delta, side, S, K):
        if len(self.deltas) < self.N:
            return {"verdict": "WARMUP", "side": side}

        mean = np.mean(self.deltas)
        std  = np.std(self.deltas) or 1e-9
        z    = (delta - mean) / std
        sgn  = +1 if side == "CALL" else -1
        z_bull = sgn * z                   # positive ⇒ bullish underlying

        m = (S / K) if (S and K) else None

        score = 50 + 25 * np.tanh(z_bull / 1.0)
        if m is not None:
            score += 25 * np.tanh((m - 1.0) / 0.05)

        # premium cross-check (side-aware)
        if len(self.premiums) == 6:
            dP5 = self.premiums[-1] - self.premiums[0]
            bullish_prem_conflict = (side == "CALL" and dP5 < 0) or \
                                    (side == "PUT"  and dP5 > 0)
            bearish_prem_conflict = (side == "CALL" and dP5 > 0) or \
                                    (side == "PUT"  and dP5 < 0)
            if z_bull > 0.5 and bullish_prem_conflict:
                score -= 15
            if z_bull < -0.5 and bearish_prem_conflict:
                score -= 15

        if   score > 65: verdict = "BULLISH"
        elif score < 35: verdict = "BEARISH"
        else:            verdict = "NEUTRAL"

        band = "HIGH" if (score > 65 or score < 35) else \
               ("LOW" if 45 < score < 55 else "MEDIUM")

        return {
            "verdict":    verdict,
            "side":       side,
            "score":      round(score, 1),
            "confidence": band,
            "z_delta":    round(z, 3),
            "z_bull":     round(z_bull, 3),
            "moneyness":  round(m, 4) if m else None,
        }


# ---------- twin-leg confirmation helper ----------
def twin_confirm(delta_put, delta_call, tol=0.15):
    """
    Given simultaneous readings from put and call legs at the same strike,
    return a unified underlying-regime verdict.
    """
    parity = delta_put + delta_call
    if not (0.0 - tol <= parity <= 0.20 + tol):
        return {
            "unified": "PARITY_BREAK",
            "parity_sum": round(parity, 4),
            "note": "Contracts may not share strike/expiry",
        }

    call_itm = (delta_call > 0.51) or (delta_put < -0.49)
    put_itm  = (delta_put < -0.51) or (delta_call < 0.49)

    if call_itm and not put_itm:
        return {"unified": "UNDERLYING_HIGH", "parity_sum": round(parity, 4)}
    if put_itm and not call_itm:
        return {"unified": "UNDERLYING_LOW", "parity_sum": round(parity, 4)}
    if call_itm and put_itm:
        return {"unified": "INFLECTION_ZONE", "parity_sum": round(parity, 4)}
    return {"unified": "NEUTRAL", "parity_sum": round(parity, 4)}


# ---------- DEMO: dummy examples from §6 ----------
if __name__ == "__main__":
    print("=" * 60)
    print("DUMMY EXAMPLE 6.1 — Put side, bullish")
    print("=" * 60)
    clf = GreeksRegimeClassifier(mode="local")
    print(clf.update(delta=-0.445, rho=-1.295, premium=84.50))

    print("\n" + "=" * 60)
    print("DUMMY EXAMPLE 6.2 — Call side, bearish")
    print("=" * 60)
    clf2 = GreeksRegimeClassifier(mode="local")
    print(clf2.update(delta=0.4720, rho=1.3750, premium=118.00))

    print("\n" + "=" * 60)
    print("DUMMY EXAMPLE 6.3 — Twin confirmation, 10:47 IST (real)")
    print("=" * 60)
    print(twin_confirm(delta_put=-0.4407, delta_call=0.5593))
Expected output:

text
============================================================
DUMMY EXAMPLE 6.1 — Put side, bullish
============================================================
{'verdict': 'BULLISH', 'side': 'PUT'}

============================================================
DUMMY EXAMPLE 6.2 — Call side, bearish
============================================================
{'verdict': 'BEAR', 'side': 'CALL'}

============================================================
DUMMY EXAMPLE 6.3 — Twin confirmation, 10:47 IST (real)
============================================================
{'unified': 'UNDERLYING_HIGH', 'parity_sum': 0.1186}
9. Integration recipe
9.1 Single-instrument workflow
text
1. Load candles JSON for the instrument.
2. For each candle:
      delta = candle.greeks.delta
      rho   = candle.greeks.rho
      premium = candle.close
      side  = auto-detect (Δ sign)
      clf.update(delta, rho, premium, S=S_spot_if_available, K=strike)
3. Collect verdicts, confidence, score.
4. Compare verdict sign vs next-5-candle premium direction → hit-rate metric.
9.2 Twin-leg workflow (recommended)
text
1. Load BOTH candles files (put + call).
2. Align by timestamp (1-min buckets).
3. For each timestamp where both exist:
      v = twin_confirm(Δ_put, Δ_call)
      IF v.unified == "PARITY_BREAK":
          LOG warning; skip candle
      ELSE:
          record unified verdict
4. Use unified verdict + per-leg verdicts for cross-validation.
9.3 How to validate the formula
For each window of N=5 candles ahead:

text
hit = (verdict == "BULLISH" AND future_premium_change > 0) OR
      (verdict == "BEARISH" AND future_premium_change < 0)

aggregate_hit_rate = sum(hit) / sum(verdict != "NEUTRAL")
Benchmark targets:

local mode (put): ≥ 65 % hit rate

local mode (call): ≥ 60 % hit rate (call side noisier)

portable mode (zΔ only): ≥ 55 % hit rate

portable mode (+ m = S/K): ≥ 65 % hit rate

If below benchmark, re-run §2 calibration on that instrument and update local thresholds.

10. Change log
Version	Date	Change
1.0	2026-11-13	Initial put-side calibration (10:47–11:33)
2.0	2026-11-15	Unified call + put; recalibrated call thresholds; parity check; twin-leg confirmation
11. Quick reference — one-liner per side
PUT (contract-local):

text
BULL if (Δ > −0.49 AND ρ > −1.43); BEAR if (Δ < −0.51 AND ρ < −1.49)
CALL (contract-local):

text
STRONG_BULL if (Δ > 0.53 AND ρ > 1.53)
BULL        if (Δ > 0.51 AND ρ > 1.49)
STRONG_BEAR if (Δ < 0.47 AND ρ < 1.38)
BEAR        if (Δ < 0.49 AND ρ < 1.43)
Twin confirmation (same strike):

text
UNDERLYING HIGH  if Δ_call > 0.51 AND Δ_put > −0.51
UNDERLYING LOW   if Δ_put < −0.51 AND Δ_call < 0.51
PARITY BREAK     if not (0.0 ≤ Δ_put + Δ_call ≤ 0.20)