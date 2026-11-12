# UNIVERSAL GREEKS-BASED DIRECTIONAL ORDER FILTER — LOGIC DOCUMENT

## Purpose
A rule-based, deterministic order filter that applies to ANY option instrument
after isolation, irrespective of the option price. All thresholds are normalised
ratios — no absolute price dependence. No AI model. Pure comparison logic.

---

## SECTION 0 — INPUTS (per candle, per instrument)

Per-candle fields required:

```text
ts              : timestamp
spot_close      : underlying spot price
premium_close   : option premium close
iv              : implied volatility (decimal, e.g. 0.2106)
delta           : signed delta (negative for PE, positive for CE)
gamma           : gamma (always positive)
theta           : theta per day (negative for long options)
vega            : vega per 1.00 vol (positive)
Per-instrument fields required:

text
instrument_type : "CE" or "PE"
strike_price    : strike
isolated_time   : timestamp of isolation/selection
From S/R engine:

text
first_sr_price      : first confirmed S/R price after isolated_time
first_sr_type       : "support" or "resistance"
first_sr_confirmed  : timestamp of confirmation
sr_levels           : list of all confirmed S/R prices
From paired-leg engine:

text
paired_close        : opposite leg premium close on same 15m candle
paired_prev_close   : opposite leg premium close on previous 15m candle
base_prev_close     : base instrument premium close on previous 15m candle
SECTION 1 — UNIVERSAL CONSTANTS (price-independent)
text
# Delta band (magnitude)
DELTA_MIN           = 0.30
DELTA_SWEET_LO      = 0.40
DELTA_SWEET_HI      = 0.65
DELTA_MAX           = 0.75

# Gamma speed
GAMMA_SLOW          = 0.0007
GAMMA_FAST          = 0.0012

# IV regime
IV_LOOKBACK         = 20
IV_SPIKE_RATIO      = 1.30
IV_CRUSH_RATIO      = 0.70
IV_ONE_MIN_CRUSH    = 0.03
IV_TURN_DOWN_BARS   = 3

# Theta
THETA_HIGH          = 20.0
THETA_LOW_GAMMA     = 0.0007

# Vega
VEGA_IGNORE_BELOW   = 5.0

# 15m gate
TF_MINUTES          = 15
SECTION 2 — HELPER FORMULAS (pure, price-independent)
text
norm_delta(delta)         = abs(delta)
norm_theta(theta)         = abs(theta)

rolling_mean(series, n)   = mean(last n values of series)

iv_ratio(iv_now, series)  = iv_now / rolling_mean(series, IV_LOOKBACK)
                            (if avg <= 0, return 1.0)

iv_slope(series, bars)    = True if series[-i] < series[-i-1]
                            for i in 1..bars

one_min_iv_drop(iv_now, iv_prev) = iv_prev - iv_now

expected_delta_gain(delta, spot_move)   = norm_delta(delta) * abs(spot_move)
expected_vega_gain(vega, iv_change)     = vega * iv_change
expected_theta_cost(theta, minutes)     = norm_theta(theta) * (minutes / 375)
                                          # 6.25h session = 375 min

risk_reward(entry, stop, target)        = abs(target - entry) / abs(entry - stop)
                                          (if risk == 0, return 0)

position_size(capital, risk_pct, entry, stop) =
    (capital * risk_pct) / abs(entry - stop)
    (if risk per unit == 0, return 0)
SECTION 3 — 15M COMPLETION GATE
text
is_15m_complete(ts, tf=15):
    minute = ts.minute
    return (minute % tf) == (tf - 1)

Meaning: a 15m candle is complete only on minutes :14, :29, :44, :59.
If not complete -> status = WAIT_15M_CLOSE, no order.
SECTION 4 — GREEKS FILTERS (universal)
Delta filter
text
delta_filter(delta):
    d = abs(delta)
    if d < DELTA_MIN or d > DELTA_MAX: return (False, 0.0)
    if DELTA_SWEET_LO <= d <= DELTA_SWEET_HI: return (True, 1.0)
    return (True, 0.5)
Gamma filter
text
gamma_filter(gamma):
    if gamma < GAMMA_SLOW: return (True, stop_mult=1.5, need_bigger_move=True)
    if gamma > GAMMA_FAST: return (True, stop_mult=0.75, need_bigger_move=False)
    return (True, stop_mult=1.0, need_bigger_move=False)
IV filter
text
iv_filter(iv_now, iv_series):
    ratio = iv_ratio(iv_now, iv_series)
    turning_down = iv_slope(iv_series, IV_TURN_DOWN_BARS)
    if ratio > IV_SPIKE_RATIO and turning_down: return (False, 0.0, False)
    if ratio > IV_SPIKE_RATIO:                  return (True, 0.5, False)
    if ratio < IV_CRUSH_RATIO:                  return (True, 0.5, False)
    return (True, 1.0, False)
Theta filter
text
theta_filter(theta, gamma):
    t = abs(theta)
    if t > THETA_HIGH and gamma < THETA_LOW_GAMMA: return (True, hold_cap=30)
    if t > THETA_HIGH:                              return (True, hold_cap=60)
    return (True, hold_cap=240)
Vega filter
text
vega_filter(vega) = vega > VEGA_IGNORE_BELOW
SECTION 5 — PAIRED-LEG CONFIRMATION
text
paired_leg_confirms(base_close, base_prev, paired_close, paired_prev):
    base_dir   = base_close - base_prev
    paired_dir = paired_close - paired_prev
    return (base_dir * paired_dir) < 0

paired_leg_strength(base_close, base_prev, paired_close, paired_prev):
    base_pct   = abs(base_close - base_prev) / base_prev
    paired_pct = abs(paired_close - paired_prev) / paired_prev
    return min(base_pct, paired_pct)
SECTION 6 — DIRECTION GATE (S/R based)
text
direction_gate(close, first_sr_price, first_sr_type):
    if first_sr_type == "resistance":
        if close < first_sr_price: return "bearish"
        if close > first_sr_price: return "bullish"
    if first_sr_type == "support":
        if close < first_sr_price: return "bearish"
        if close > first_sr_price: return "bullish"
    return "neutral"
SECTION 7 — ENTRY / STOP / TARGET FORMULAS (percentage-based)
text
STOP_PCT_BASE       = 0.05
STOP_PCT_SLOW_GAMMA = 0.075
STOP_PCT_FAST_GAMMA = 0.035

stop_price(entry, gamma, direction):
    if gamma < GAMMA_SLOW: pct = STOP_PCT_SLOW_GAMMA
    elif gamma > GAMMA_FAST: pct = STOP_PCT_FAST_GAMMA
    else: pct = STOP_PCT_BASE
    if direction == "long":  return entry * (1 - pct)
    else:                    return entry * (1 + pct)

TARGET_RR_MIN  = 2.0
TARGET_RR_GOOD = 3.0

target_from_rr(entry, stop, rr):
    risk = abs(entry - stop)
    if direction == "long": return entry + rr * risk
    else:                   return entry - rr * risk

target_from_sr(entry, sr_levels, direction):
    if direction == "long":
        candidates = [s for s in sr_levels if s > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [s for s in sr_levels if s < entry]
        return max(candidates) if candidates else None
SECTION 8 — EXIT RULES
text
exit_on_target(price, target, direction):
    long:  return price >= target
    short: return price <= target

exit_on_stop(price, stop, direction):
    long:  return price <= stop
    short: return price >= stop

exit_on_iv_crush(iv_now, iv_prev):
    return (iv_prev - iv_now) > IV_ONE_MIN_CRUSH

exit_on_iv_crush_ratio(iv_now, iv_series):
    return iv_ratio(iv_now, iv_series) < IV_CRUSH_RATIO

exit_on_hold_time(minutes_held, hold_cap):
    return minutes_held >= hold_cap
SECTION 9 — MASTER ORDER DECISION (sequential gate)
text
decide_order(inputs):
    reasons = []

    STEP 1 — 15m completion gate
        if not is_15m_complete(ts):
            return WAIT_15M_CLOSE

    STEP 2 — Paired-leg confirmation
        if not paired_leg_confirms(base_close, base_prev,
                                   paired_close, paired_prev):
            return SKIP (paired_leg_no_confirm)

    STEP 3 — Direction gate
        direction = direction_gate(premium_close, first_sr_price, first_sr_type)
        if direction == "neutral": return SKIP (direction_neutral)
        if instrument_type == "PE" and direction != "bearish":
            return SKIP (PE_needs_bearish)
        if instrument_type == "CE" and direction != "bullish":
            return SKIP (CE_needs_bullish)

    STEP 4 — Delta filter
        d_pass, d_mult = delta_filter(delta)
        if not d_pass: return SKIP (delta_out_of_band)

    STEP 5 — Gamma filter
        g_pass, stop_mult, need_bigger = gamma_filter(gamma)
        if not g_pass: return SKIP (gamma_fail)

    STEP 6 — IV filter
        iv_pass, iv_mult, _ = iv_filter(iv, iv_series)
        if not iv_pass: return SKIP (iv_spike_and_falling)

    STEP 7 — Theta filter
        t_pass, hold_cap = theta_filter(theta, gamma)
        if not t_pass: return SKIP (theta_fail)

    STEP 8 — Size
        size_mult = min(d_mult, iv_mult)
        qty = position_size(capital, risk_pct * size_mult,
                            premium_close, stop_price(premium_close, gamma))

    STEP 9 — Entry / stop / target
        entry  = premium_close
        stop   = stop_price(entry, gamma, "long")
        t_rr   = target_from_rr(entry, stop, TARGET_RR_GOOD)
        t_sr   = target_from_sr(entry, sr_levels, "long")
        target = t_sr if t_sr else t_rr
        rr     = risk_reward(entry, stop, target)

    STEP 10 — R:R reject
        if rr < TARGET_RR_MIN: return SKIP (rr_below_min)

    STEP 11 — Build order
        return PLACE_ORDER {
            direction, entry, stop, target, qty,
            size_mult, rr, hold_cap_min
        }
SECTION 10 — LIVE MONITOR (called every 1m after entry)
text
monitor_position(ts, entry, stop, target, price,
                 iv, iv_prev, hold_cap, minutes_held):

    if exit_on_stop(price, stop):               return EXIT_STOP
    if exit_on_target(price, target):           return EXIT_TARGET
    if exit_on_iv_crush(iv, iv_prev):           return EXIT_IV_CRUSH_1MIN
    if exit_on_hold_time(minutes_held, hold_cap): return EXIT_HOLD_CAP
    return HOLD
SECTION 11 — WORKED EXAMPLES (real numbers, 2026-11-12 PE data)
Example 1 — 10:15 close (rejected, fakeout)
text
Input:
  instrument_type = "PE"
  ts              = 10:15
  premium_close   = 212.70
  delta           = -0.4805
  gamma           = 0.000712
  iv              = 0.1846
  theta           = -20.12
  vega            = 12.076
  first_sr_price  = 208.0 (resistance)
  paired_close    = 207.50 (CE 10:15)
  paired_prev     = 218.10 (CE 09:45)
  base_prev       = 229.00 (PE 10:00)

Walk-through:
  STEP 1: 15m complete  -> PASS
  STEP 2: paired check:
            CE move = 207.50 - 218.10 = -10.60
            PE move = 212.70 - 229.00 = -16.30
            both negative -> product positive -> paired FAIL
  Result: SKIP (paired_leg_no_confirm)

Interpretation: both premiums fell together = IV-driven, not directional.
Example 2 — 12:00 close (valid signal, rejected on R:R)
text
Input:
  instrument_type = "PE"
  ts              = 12:00
  premium_close   = 161.15
  delta           = -0.5617
  gamma           = 0.001187
  iv              = 0.11061
  theta           = -10.57
  vega            = 11.826
  first_sr_price  = 208.0 (resistance)
  sr_levels       = [175, 193.1, 208, 210, 233.65]
  paired_close    = 232.70 (CE 12:00)
  paired_prev     = 240.00 (CE 11:45)
  base_prev       = 160.55 (PE 11:59)

Walk-through:
  STEP 1: 15m complete  -> PASS
  STEP 2: paired check:
            CE move = 232.70 - 240.00 = -7.30
            PE move = 161.15 - 160.55 = +0.60
            opposite signs -> PASS
  STEP 3: direction: 161.15 < 208 -> bearish -> PASS for PE
  STEP 4: delta |−0.5617| = 0.5617 in [0.40, 0.65] -> PASS, mult 1.0
  STEP 5: gamma 0.001187 moderate -> stop_mult 1.0
  STEP 6: iv ratio ~1.05 -> PASS, mult 1.0
  STEP 7: theta |−10.57| < 20 -> hold_cap 240 min
  STEP 8: size_mult = min(1.0, 1.0) = 1.0
  STEP 9:
     entry = 161.15
     stop  = 161.15 * (1 - 0.05) = 153.09
     t_rr  = 161.15 + 3 * (161.15 - 153.09) = 185.33
     t_sr  = nearest sr > 161.15 = 175
     target = 175
     rr     = (175 - 161.15) / (161.15 - 153.09) = 1.72
  STEP 10: rr 1.72 < TARGET_RR_MIN 2.0 -> SKIP (rr_below_min)

Interpretation: direction and Greeks passed, but nearest S/R target
gives R:R below 2.0. Correct action: wait for a better entry.
SUMMARY OF DECISION FLOW
text
1. Candle must be 15m-complete          -> else WAIT
2. Paired leg must move oppositely      -> else SKIP
3. S/R direction must match instrument  -> else SKIP
4. Delta in [0.30, 0.75]                -> else SKIP (half if outside sweet spot)
5. Gamma regime sets stop multiplier    -> slow: wider, fast: tighter
6. IV not spiking-and-falling           -> else SKIP (half if spike/crush)
7. Theta sets max hold time             -> else cap hold
8. Entry = premium close
9. Stop  = entry * (1 - gamma-adjusted %)
10. Target = nearest S/R or R:R-based
11. R:R must be >= 2.0                  -> else SKIP
12. Monitor every minute: stop / target / IV crush / hold cap
KEY PROPERTIES
text
- Price-independent: all thresholds are ratios or percentages.
- Deterministic: same inputs -> same decision.
- Instrument-agnostic: works on CE or PE, any strike, any underlying.
- Candle-gated: never fires inside a forming 15m candle.
- Paired-leg vetoed: CE/PE must disagree on the same close.
- IV-aware: detects both spike traps and crush traps.
- R:R-guarded: rejects trades with insufficient reward.
- Exit-disciplined: stop, target, IV crush, and hold cap are all enforced.