Strategy Specification (v2.2)
First-Candle OR Ladder · Isolation-Buffered Entry · 30%/30pt Initial SL · S2..AVG Entry-Zone R3 Trigger · Previous-Close Trailing SL · 15/30 → 15/3 Exit Flip · Single Order

1. Configuration & Parameters
Parameter	Value	Meaning
LOT	65	Contract lot size
MAX_ORDERS	1	Single order per day
TICK	5.0	Tick size
EARLY_ISOLATION_CUTOFF	09:30	Post-cutoff = normal SR path
SR_LEFT / SR_RIGHT	15 / 3	Entry-signal pivot scan (post-09:30)
EARLY_SR_WINDOW_START / END	09:15 / 09:30	Early-isolation scan window
EARLY_SR_LEFT / RIGHT	15 / 3	Early-isolation pivot scan
EARLY_SR_ALLOW_PRIOR_DAY	True	Early scan may use prior-day candles
EARLY_SR_ALLOW_POST_WINDOW_CONFIRM	True	Early scan may confirm after 09:30
EXIT_LEFT_INITIAL / RIGHT_INITIAL	15 / 30	Initial exit-resistance scanner
EXIT_LEFT_POST_MOVE / RIGHT_POST_MOVE	15 / 3	Exit scanner after R3 milestone
SL_PCT_OF_ENTRY	0.30	30 % of entry as SL offset
SL_MIN_POINTS	30	Minimum SL offset
TRIGGER_REQUIRES_ENTRY_ZONE	True	R3 trigger only valid if entry ∈ [S2, AVG]
ENTRY_ZONE_LOW_LEVEL	S2	Lower bound of entry zone
ENTRY_ZONE_HIGH_LEVEL	AVG	Upper bound of entry zone
R3_POINTS_TRIGGER	50	Minimum move from reference
TRIGGER_POINTS_FROM	OR_OPEN	Reference price (OR_OPEN / OR_LOW / ENTRY)
ISOLATION_BUFFER_ENABLED	True	(new) Enforce the isolation-time entry buffer
ISOLATION_BUFFER_ENTRY_MODE	NEXT_CANDLE_OPEN	(new) NEXT_CANDLE_OPEN or LIVE_PRICE_AT_ISOLATION
2. Opening Range (OR) Ladder — First-Candle Formulas
The OR candle is the very first candle of the trading day (sorted by time ascending). If the data is 1-minute, it is exactly the 09:15 candle; for coarser data it is the first bar of the session.

text
OR candle → Open  = first.open
            High  = first.high
            Low   = first.low
            Close = first.close   (carried but not used in level math)
2.1 Formulas in plain English
Range = High − Low

Half-step = Range / 2 = (High − Low) / 2

Midpoint = AVG = (High + Low) / 2

Resistance ladder (above High):

R1 = High

R2 = High + Step

R3 = High + 2 × Step = High + (High − Low)

Support ladder (below Low):

S1 = Low

S2 = Low − Step

S3 = Low − 2 × Step = Low − (High − Low)

Note: The Close of the OR candle is not used. The old v2 used P = (H + L + C) / 3, which pulled every level toward the close. The new ladder depends only on H and L, so the levels are identical regardless of where the first candle closes inside its range.

2.2 Compact reference table
Level	Formula
R3	High + (High − Low)
R2	High + (High − Low) / 2
R1	High
AVG	(High + Low) / 2
S1	Low
S2	Low − (High − Low) / 2
S3	Low − (High − Low)
2.3 Worked example
OR candle: O = 100 · H = 130 · L = 90 · C = 120

Range = 40, Step = 20, AVG = 110

Level	Value
R3	170
R2	150
R1	130
AVG	110
S1	90
S2	70
S3	50
Entry zone width = AVG − S2 = exactly Range = 40. Unlike v2, where the zone width drifted with C.

3. Initial Stop-Loss (unchanged)
Rule in plain English:

pct_offset = entry × 0.30

pts_offset = 30

offset = max(pct_offset, pts_offset)

SL_initial = entry − offset

3.1 Examples
Entry	30 %	Floor	Offset	Initial SL	Basis
80	24.0	30	30	50	MIN_30
100	30.0	30	30	70	MIN_30 (tie → floor wins)
145	43.5	30	43.5	101.5	PCT_30
250	75.0	30	75.0	175.0	PCT_30
4. Entry-Zone Check
Rule in plain English: an entry is in-zone if its price is between S2 and AVG (both ends inclusive).

Using the §2.3 ladder (zone = [70, 110]):

Entry	In zone?
65	❌
70	✅
95	✅
110	✅
120	❌
5. Isolation Buffer — New in v2.2
5.1 Why this rule exists
In real time, you cannot execute an order on an instrument you do not yet have on your isolated list. The isolation list is populated at a specific moment (T_iso) for each instrument. If a valid SR pivot confirmed at 09:19 but the instrument only reaches your isolation list at 09:22, the earliest possible execution is 09:22 onward — not the historical 09:19 signal price.

The buffer rule accepts the SR signal but replaces the entry price with a realistically executable one, evaluated at or after T_iso. The initial SL, the in-zone check, and the R3 milestone then all use this buffered entry price.

5.2 Rule in plain English
Given:

T_sig — the SR confirmation bar's timestamp

T_iso — the per-instrument isolation time (supplied to the simulation)

isolation_enabled — the toggle ISOLATION_BUFFER_ENABLED

Do:

If isolation_enabled is False → behave exactly as v2.1 (entry at sr.confirmed_idx open).

If isolation_enabled is True and T_sig >= T_iso → SR came at or after isolation; entry as normal at sr.confirmed_idx open. No buffer.

If isolation_enabled is True and T_sig < T_iso → SR came before isolation; delay the entry to the isolation moment using the mode selector below.

5.3 Entry-mode selector (ISOLATION_BUFFER_ENTRY_MODE)
Mode	Entry price	Entry index
NEXT_CANDLE_OPEN (default)	Open of the first bar whose timestamp is ≥ T_iso	that bar's index
LIVE_PRICE_AT_ISOLATION	Close of the last bar whose timestamp is strictly < T_iso	next bar's index (monitoring starts here)
In backtest with 1-minute candles, LIVE_PRICE_AT_ISOLATION yields exactly the 09:22:00 tick if T_iso = 09:22:00 (the 09:21 bar's close), which is the realistic proxy for the live price at isolation.

5.4 Logged fields (new)
Field	Description
signal_ts	Original SR confirmation timestamp
isolation_ts	Per-instrument isolation time
buffer_applied	True if entry was delayed, False otherwise
buffer_delay_seconds	T_entry − T_sig in seconds
buffer_mode	NEXT_CANDLE_OPEN or LIVE_PRICE_AT_ISOLATION
5.5 Worked example — the 09:19 / 09:22 case
Setup

OR ladder from §2.3: R3 = 170, AVG = 110, S2 = 70

SR support pivot confirmed at 09:19 — confirmation price = 100

Instrument lands on the isolation list at 09:22

Candle data is 1-minute

Prices at / near isolation

Time	Candle O	Candle C
09:18	98	99
09:19	99	100 ← SR confirmed here
09:20	100	103
09:21	103	106 ← last close before 09:22
09:22	106	108
09:23	108	105
Path A — no buffer (v2.1) → entry at 100 (09:19 confirmation open). Zone check: 100 ∈ [70, 110] ✅. Initial SL at max(0.30 × 100, 30) = 30 below → 70.

Path B — buffer on, NEXT_CANDLE_OPEN (v2.2 default) → entry = open of 09:22 bar = 106. Zone check: 106 ∈ [70, 110] ✅. Initial SL = 106 − max(0.30 × 106, 30) = 106 − 31.8 = 74.2.

Path C — buffer on, LIVE_PRICE_AT_ISOLATION → entry = close of 09:21 bar = 106. Same SL as Path B.

Path D — buffer on, but price gapped out of zone — suppose the 09:22 open is 125 (> AVG = 110). Zone check → ❌. R3 milestone is now inhibited (per §8 rule 3). Initial SL = 125 − max(37.5, 30) = 125 − 37.5 = 87.5. Trade still runs; exit scanner stays on 15/30.

Log entry (Path B)

text
signal_ts           : 09:19
isolation_ts        : 09:22
buffer_applied      : True
buffer_delay_seconds: 180
buffer_mode         : NEXT_CANDLE_OPEN
entry               : 106.00     ← actual execution price
entry_in_zone       : True
sl_initial          : 74.20
5.6 Interaction with the rest of the strategy
SL basis is computed from the buffered entry, not the signal price.

In-zone check uses the buffered entry. If the buffer moves the price out of [S2, AVG], the R3 trigger is disabled for the rest of the day (per §8).

R3 milestone reference price — TRIGGER_POINTS_FROM = ENTRY uses the buffered entry; OR_OPEN / OR_LOW are unchanged.

Resistance-scanner eligibility — the scanner runs from entry_idx + 1 onward, where entry_idx is the buffered entry index. So any pivot that confirmed between T_sig and T_iso is not eligible to become the exit target; only pivots confirmed at or after the buffered entry are.

6. R3 Milestone Trigger
Rule in plain English. The R3 milestone fires on the first bar where all four are true:

side == BUY

bar.high ≥ R3

entry_in_zone == True (skipped if TRIGGER_REQUIRES_ENTRY_ZONE = False; uses the buffered entry price from §5)

bar.high − reference_price > 50 (strict)

The reference price is chosen by TRIGGER_POINTS_FROM:

Setting	Reference
OR_OPEN (default)	OR candle open
OR_LOW	OR candle low
ENTRY	The buffered entry price
Because (2) and (4) must both hold, the bar's high must reach max(R3, reference + 50) (strict on the +50 leg).

Worked example (continuing §2.3)
O = 100, R3 = 170, reference = O = 100 → threshold = max(170, 150) = 170

bar.high	Hits R3?	Move > 50?	Trigger?
165	❌	✅	❌
170	✅	✅	✅
172	✅	✅	✅
On the trigger bar, before the resistance scanner runs:

SL_current = previous bar's close

Exit scanner flips 15/30 → 15/3

Trigger fields are logged

7. Live Resistance Scanner
Rule in plain English. Walk forward from entry_idx + 1. At each index i, ask: is candle i a pivot high? It is a pivot high if its high is strictly greater than the left bars before it and the right bars after it. If so, and if that pivot's high is above the entry price, return it — together with the pivot bar's timestamp, price and the confirmation bar index i + right. The exit fires only when the simulation reaches i + right.

Phase	left	right
Before R3 trigger	15	30
After R3 trigger	15	3
So before the milestone, the strategy is patient (needs 30 future bars); after the milestone, it's aggressive (needs only 3).

8. Order Execution Flow (step-by-step)
Called once per simulated day. Steps in order:

Compute both OR ladders — one for the isolated leg, one for the opposite leg.

Resolve the entry-signal SR — using the post-09:30 path or the early-isolation path as configured. If none, stop with "No SR → no orders."

Pick the trade leg — support → trade the isolated leg; otherwise trade the opposite leg.

Determine the effective entry index using the isolation buffer (§5):

entry_idx = sr.confirmed_idx if no buffer, or if T_sig ≥ T_iso

entry_idx = isolation_bar_idx if buffer fires

Read the entry price:

NEXT_CANDLE_OPEN → candles[entry_idx].open

LIVE_PRICE_AT_ISOLATION → candles[entry_idx − 1].close

Compute initial SL from the buffered entry price (§3).

Evaluate in-zone with the buffered entry price (§4).

Run the bar-by-bar simulator starting at entry_idx + 1. For each bar j, in this priority order:

SL check first. bar.low ≤ SL_current → exit at SL_current, reason SL, stop.
R3 trigger. If not yet fired and all four conditions of §6 hold → set SL_current = candles[j−1].close, flip scanner to 15/3, log the trigger. No exit.
Resistance confirmation. Run the scanner from j with the current left / right. If it finds a pivot high > entry with confirmed_idx == j → exit at bar.close, reason RESISTANCE_CONFIRM_{left}_{right}@{level}, stop.
End of day. If no exit fired, exit at the last bar's close, reason EOD.

PnL = (exit − entry) × LOT, where entry is the buffered entry price.

9. Operational Notes & Edge Cases
Low vs high premium SL — same as v2.1. Premium < 100 → floor wins; premium ≥ 100 → percentage wins.

Entry outside [S2, AVG] — if the buffered entry lands above AVG or below S2, entry_in_zone = False. Consequences:

R3 milestone is inhibited (because TRIGGER_REQUIRES_ENTRY_ZONE = True)

SL stays at the initial level for the entire trade

Exit scanner never flips — stays 15/30 until resistance or EOD

Shared-bar priority (unchanged from v2.1):

SL first, then R3 trigger, then resistance confirmation.

Close no longer affects levels — the OR ladder is C-independent.

Single order — no re-entry after SL, no re-entry after target, no scaling in.

Buffer edge cases

T_iso before the session's first candle → buffer effectively fires on entry bar 0 (no historical delay); SR still requires its normal confirmation bars.

T_iso after the session's last candle → no bars at or after isolation; the trade is skipped, reason NO_EXECUTABLE_BAR_AFTER_ISOLATION.

T_sig == T_iso → treated as "signal at isolation"; no buffer applied.

LIVE_PRICE_AT_ISOLATION with T_iso at the very first candle → the "last close before" does not exist; fallback to NEXT_CANDLE_OPEN.

Logging — the strategy emits signal_ts, isolation_ts, buffer_applied, buffer_delay_seconds, buffer_mode so downstream analytics can separate "buffered" from "unbuffered" trades.

10. Worked Example — End-to-End
Setup

OR ladder from §2.3: O = 100, H = 130, L = 90, C = 120 → R3 = 170, AVG = 110, S2 = 70

SR support pivot confirmed at 09:19 (pivot bar close = 100, confirm bar = 09:19)

T_iso = 09:22

Buffer mode = NEXT_CANDLE_OPEN

1-minute candles; 09:22 bar's open = 106

Step 1 — Buffer decision

T_sig = 09:19 < T_iso = 09:22 → buffer fires. entry_idx = 09:22 bar's index. entry = 106.

Step 2 — Initial SL

30 % × 106 = 31.8 > 30 → offset = 31.8 → SL_initial = 74.2.

Step 3 — In-zone check

70 ≤ 106 ≤ 110 → in_zone = True → R3 milestone is armed.

Step 4 — Walk-forward

Bar	H	L	C	SL	Trigger?	Confirm?
09:23	112	104	110	✓	✗	—
09:24	125	110	120	✓	✗	—
…	…	…	…	…	…	…
11:04	172	160	168	✓	✅	—
11:05	175	165	172	✓ (SL=168)	—	pivot of 172 confirmed here at 15/3 → EXIT at 172
R3 trigger on 11:04 (H = 172 ≥ R3 = 170, and 172 − 100 = 72 > 50).

SL moves to 11:03 close = 168. Scanner flips to 15/3.

Resistance confirmation on 11:05: pivot at 11:04, confirmation bar = 11:05 → exit at 11:05 close = 172.

Step 5 — PnL

(172 − 106) × 65 = 4 290 (before costs).

Step 6 — Logged fields

text
signal_ts            : 09:19
isolation_ts         : 09:22
buffer_applied       : True
buffer_delay_seconds : 180
buffer_mode          : NEXT_CANDLE_OPEN
entry                : 106.00
entry_in_zone        : True
sl_initial           : 74.20
sl_basis             : PCT_30
trigger_ts           : 11:04
trigger_prev_close   : 168.00
exit                 : 172.00
reason               : RESISTANCE_CONFIRM_15_3@172
pnl_total            : 4290
11. Summary of Changes (v2.1 → v2.2)
Component	v2.1	v2.2 (current)
OR ladder	First-candle range ladder	Same
AVG	(H + L) / 2	Same
R1 / R2 / R3	H, H + Step, H + 2·Step	Same
S1 / S2 / S3	L, L − Step, L − 2·Step	Same
Initial SL	max(30 %, 30 pts)	Same
Entry gating	S2 ≤ entry ≤ AVG	Same
R3 milestone	Prev-close trail, flip 15/30 → 15/3	Same
Exit scanner	Dynamic 15/30 → 15/3	Same
Order flow	Single order	Same
Entry timing	Entry at sr.confirmed_idx open	Buffered entry at/after T_iso when signal precedes isolation
New config	—	ISOLATION_BUFFER_ENABLED, ISOLATION_BUFFER_ENTRY_MODE
New log fields	—	signal_ts, isolation_ts, buffer_applied, buffer_delay_seconds, buffer_mode
The core SL rule, entry-zone gate, R3 milestone, exit-flip behaviour, and single-order flow remain identical to v2.1. Only the OR ladder (already updated in v2.1) and the new isolation-buffer entry rule differ.