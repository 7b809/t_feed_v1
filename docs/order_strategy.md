# BASE-INSTRUMENT ORDER LOGIC — CE 45404 (25350 CE) Reference Text

## Purpose of this document
Use this text as a **reference block** in new chats. It tells the assistant:
- which instrument is the **base instrument** for order placement,
- which instrument is the **paired leg / veto leg**,
- the **completed-15m gate**,
- the **paired-leg veto**,
- the **entry / stop / target** rules,
- the **invalidation** and **late-signal** rules,
- the **incomplete-candle** rule,
- and a **dummy worked example** using real numbers from the 2026-11-12 session.

---

## 1. Instrument roles

```text
Base instrument   : NSE_FO|45404  = NIFTY 25350 CE 17 NOV 26
Paired leg / veto : NSE_FO|45405  = NIFTY 25350 PE 17 NOV 26
Timeframe for decisions : completed 15m candles (built from 1m data)
Isolation time (example) : 2026-11-12 09:26
Rule:

The base instrument (CE) is used for entry price, stop, and target.

The paired leg (PE) is used only as a veto/confirmation, never as the entry trigger.

2. Completed-15m gate
text
Never place an order while the 15m candle containing the signal is still forming.
Always wait for the 15m candle to complete.
Dummy example:

text
Signal time      : 09:31 (CE resistance 216.6 confirmed)
Containing 15m   : 09:30–09:45
Status at 09:31  : WAIT_FOR_15M_CLOSE
Action           : do not place order yet
3. Paired-leg veto
text
On the same completed 15m close:
  CE bearish order is valid only if PE is bullish on that same close.
  CE bullish order is valid only if PE is bearish on that same close.
  If both CE and PE rise, or both fall  -> IV / choppy -> NO directional order.
Dummy example (09:30–09:45 close):

text
CE 15m close = 215.20   (CE rose +9.9 from open 205.30)
PE 15m close = 200.30   (PE fell −16.5 from open 216.80)
CE up + PE down  => underlying bullish read
Bearish CE order => VETOED
4. Entry / stop / target rules (base instrument = CE)
text
Bearish CE entry:
  Condition A : CE completed 15m close below confirmed resistance or below confirmed support
  Condition B : CE candle direction is falling on that same 15m candle
  Condition C : PE is bullish on that same 15m close
  Entry       : CE close of the qualifying 15m candle
  Stop        : above the most recent CE swing high
  Target      : next confirmed CE support (then next, if trend continues)

Bullish CE entry (mirror rule):
  Condition A : CE completed 15m close above confirmed resistance
  Condition B : CE candle direction is rising on that same 15m candle
  Condition C : PE is bearish on that same 15m close
  Entry       : CE close of the qualifying 15m candle
  Stop        : below the most recent CE swing low
  Target      : next confirmed CE resistance
Dummy example — first permissible bearish CE order (after 10:15 close):

text
CE 15m close  = 207.50   (CE fell −22.35 from open 229.85)
PE 15m close  = 191.80   (PE rose +13.75 from open 178.05)
Level check   : CE closed below 216.6 resistance
Paired check  : PE bullish
Entry         : 207.50
Stop          : above most recent CE swing high 218.70  -> risk ≈ 11.2 points
Target 1      : 192.00 CE support
Target 2      : 178.00 CE support
Outcome       : CE reached 174.60 by 11:15 -> T1 hit, T2 approached
5. Invalidation rule
text
If the base instrument's completed 15m close is on the wrong side of the
first confirmed level after isolation, the original assumption is INVALID.
No order is placed in that direction.
Dummy example:

text
First confirmed level after isolation : CE resistance 216.6 (confirmed 09:31)
09:45 CE close = 215.20  -> below 216.6, but CE rose +9.9 and PE fell −16.5
=> paired-leg veto overrides -> NO bearish order
Next 15m close (10:00) CE = 229.00  -> 12.4 above 216.6
=> bearish thesis formally INVALIDATED
6. Late-signal rule
text
If the first valid cross-confirmed signal is far in time or price from the
original idea, treat it as a NEW trade with its own entry/stop/target.
Do not force the original trade.
Dummy example:

text
Original idea        : bearish CE from 09:31 close ≈ 205.95
First clean cross-confirmation : after 11:15 close
CE at 11:15          : 174.60  (≈ 31 points lower than 09:31 close)
PE at 11:15          : 225.05  (above 218.55 resistance)
Status               : VALID but LATE -> treat as new trade
7. Incomplete-candle rule
text
Ignore the last forming candle for order decisions.
Status = WAIT_FOR_15M_CLOSE.
Dummy example (data end):

text
Forming 15m candle : 11:45–12:00
CE move            : 164.50 -> 172.55  (+8.05)
PE move            : 233.20 -> 217.20  (−16.0)
Read               : bullish rebound cross, but candle incomplete
Action             : WAIT for 12:00 close, no order
8. Reference logic in one paragraph (copyable)
text
Use NSE_FO|45404 (25350 CE) as the base instrument and NSE_FO|45405 (25350 PE)
as the paired veto leg. Take the first S/R confirmed after isolation as the
reference level, but never act on it until the containing 15m candle completes.
On the completed 15m close require three things together:
  (1) CE close is on the correct side of the confirmed level,
  (2) CE candle direction agrees with that side (bearish order needs CE falling,
      bullish order needs CE rising),
  (3) PE moves oppositely on the same 15m close.
If all three agree, place the order at the CE close, stop above the most recent
CE swing high for shorts or below the most recent CE swing low for longs, and
target the next confirmed CE S/R. If the CE level and CE candle direction
disagree, or if CE and PE move the same way, place no order. If the first valid
cross-confirmed signal is far in time or price from the original idea, treat it
as a new trade. Ignore any forming 15m candle.
9. Dummy outcome table (real numbers from 2026-11-12)
text
Time of 15m close | CE close | CE dir | PE close | PE dir | Status                     | Order?
------------------+----------+--------+----------+--------+----------------------------+----------------
09:45             | 215.20   | up +9.9| 200.30   | dn −16.5| IV / conflict             | NO
10:00             | 229.00   | up     | 178.15   | dn     | bullish underlying        | NO (bearish invalid)
10:15             | 207.50   | dn −22 | 191.80   | up +13 | valid bearish cross       | YES (entry 207.50, stop >218.70)
11:15             | 174.60   | dn     | 225.05   | up     | valid bearish but LATE    | NEW trade only
11:45–12:00       | forming  | —      | forming  | —      | WAIT_FOR_15M_CLOSE        | NO
10. Quick checklist before every order
text
[ ] 1. Is the 15m candle completed?           -> if NO, WAIT
[ ] 2. Is CE close on the correct side of the confirmed level? -> if NO, NO ORDER
[ ] 3. Does CE candle direction agree?        -> if NO, NO ORDER
[ ] 4. Does PE move oppositely on same close? -> if NO, NO ORDER (IV/choppy)
[ ] 5. Is this the first valid signal, or a late new trade? -> if late, NEW trade
[ ] 6. Stop placed beyond most recent CE swing high/low?    -> required
[ ] 7. Target = next confirmed CE S/R?                      -> required


The plain-Python rule logic (no AI, just comparisons)
You can write deterministic rules like this:

Rule 1 — Delta band for contract selection. Compute delta magnitude. If it is below 0.30 or above 0.75, do not place a directional long on this strike; the premium will not track spot efficiently. In this data, the 0.46–0.55 band from 09:15 to 10:00 was the usable zone.

Rule 2 — Gamma speed check. Compute gamma. If gamma is below about 0.0007, require a larger spot move (say 1.5× your normal trigger distance) or skip the trade. If gamma is above about 0.0012, a normal trigger is fine and you can use a tighter stop. At 10:00 gamma 0.00062 would have said “slow, skip or widen.” At 12:40 gamma 0.00201 would have said “fast, normal.”

Rule 3 — IV reliability filter (the big one). Keep a rolling 20-bar average of IV. If current IV is more than about 1.3× that average and starting to turn down, do not buy premium for direction — IV crush will likely eat the delta gain. At 10:00 IV 0.2106 was a spike high; the filter would have blocked a long-premium directional entry there. Conversely, if IV is below about 0.8× its average and turning up, long premium is favorable. In this data, IV was low and stable around 09:15–09:30 (0.0989) and again near 12:40 (0.0498); those are friendlier long-premium zones.

Rule 4 — Theta hold-cost check. Compute theta magnitude. If it is above about 20 per day and gamma is low, either shorten the intended hold or prefer a short-premium structure. If gamma is high, theta is less relevant because delta/gamma gains dominate.

Rule 5 — Spot–delta consistency check (confirmation only). For puts: if spot is falling and delta magnitude is rising, direction is consistent. If spot is falling but delta magnitude is falling, something is off (model/IV issue) — skip. In this data, 09:15 spot 25229.6 delta -0.6218; 10:00 spot 25378.2 delta -0.4616 (spot up, delta magnitude down — consistent); 12:40 spot 25211.8 delta -0.7755 (spot down, delta magnitude up — consistent). This check never triggers the trade, it only vetoes inconsistent ones.

How this stacks with your existing 15m + paired-leg logic
Your existing sequence is: isolation time → first S/R → completed 15m close → CE/PE directional agreement → entry/stop/target. The Greeks slot in after that sequence, as a final gate:

15m close and paired-leg say “bearish.”

Delta band says “this strike will capture the move” (0.40–0.65).

Gamma says “fast or slow” (adjust stop and target distance).

IV filter says “premium will not be crushed” (IV not at a spike high and falling).

Theta says “hold cost is acceptable.”

If all pass, place the order; if any fails, skip or reduce size.

In this dataset, a bearish put idea around 10:00 would have passed step 1 (spot was near a local top and the paired leg would have agreed), passed delta, but failed the IV filter (IV 0.2106, a spike high). That single filter would have saved about 96 points of loss despite the direction being right. That is the practical value.

Caveats
The Greeks in this feed are model-derived and not perfectly stable. Theta swings from -23.29 at 10:00 to -0.519 at 12:40 to -7.43 at 15:00 — that is IV-driven, not just time-driven, and at the extremes the model’s theta is unreliable. So use theta as a rough cost estimate, not a precise number. IV and delta are the two Greeks you can trust most in this data. Gamma is useful as a speed indicator but also model-sensitive. Vega and rho you can ignore for intraday direction.

