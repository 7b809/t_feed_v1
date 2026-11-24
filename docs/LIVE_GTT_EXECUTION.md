# Supertrend two-GTT execution through AlgoCLI

## Execution boundary

The isolation project owns signal qualification, instrument selection, strategy calculations,
risk checks, entry limits, local/Mongo state, monitoring decisions and audit records.
It does not create an Upstox SDK client and does not retrieve broker credentials.

All broker operations are sent through the installed command-line executable using
exactly this invocation pattern:

```bash
algoai --skip-permissions --prompt "<rendered prompt template>"
```

The isolation project does not define or depend on an AlgoCLI project directory,
launcher script, Python interpreter, custom working directory, or internal
AlgoCLI implementation. Authentication and broker access remain inside the CLI
and its skills.

## Activation and safety

`LIVE_GTT_EXECUTION_ENABLED=false` remains the required default. Keep it false
until the separate AlgoCLI team confirms the unattended sandbox execution path,
structured JSON responses, and support for modifying an existing GTT's STOPLOSS
rule without cancelling/recreating the GTT. The CLI permission flag does not
replace skill-level confirmation requirements.

Both `LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR` and
`LIVE_GTT_MAX_RISK_PER_TRADE_INR` must be positive before entries can be planned.
The worker fails closed if the algoai executable is unavailable. Risk caps are
evaluated against the combined exposure of both one-lot orders.

## Strategy

Every qualified signal creates two independent GTTs for the same selected option:

| Feature | GTT 1 | GTT 2 |
|---|---|---|
| Quantity | 1 lot | 1 lot |
| Entry | Immediate live-market entry | Immediate live-market entry |
| Initial SL | Existing support-below-percentage-stop rule | Lower of 30% stop and nearest S1/S2/S3 below entry |
| Target | Existing premium-band target: 60/50/40 points | 15-minute R3, otherwise calculated GTT 1 target PRICE × 5 |
| Trailing | Disabled | Enabled |
| Trailing step | — | Fixed from initial Entry, SL and Target |

GTT 1 retains the original stop calculation: calculate the configured percentage
stop first, then select the highest S1/S2/S3 support strictly below that stop.
If none qualifies, retain the percentage stop.

GTT 2 selects the nearest S1/S2/S3 support below entry, then uses the lower of
that support and the 30% stop. It therefore never widens the 30% stop. If no
support below entry exists, it uses the 30% stop.

The GTT 2 target is the current contract's completed 15-minute R3 when a valid
same-day pivot file is available. If the 15-minute R3 is unavailable, the fallback
is the calculated GTT 1 target PRICE multiplied by five. If R3 exists but is not
above the live entry, planning fails closed rather than creating an invalid
long-position target.

The initial trailing step is calculated once and never recalculated:

```text
trail_step = (initial_entry - initial_stop) + ((initial_target - initial_entry) / 2)
```

For every complete favourable move of `trail_step` points from the original
entry, GTT 2's STOPLOSS trigger is raised by the same number of `trail_step`
points. Example: entry 100, initial SL 70, target 140 gives a 50-point step;
at LTP 150 the SL becomes 120, at 200 it becomes 170, and at 250 it becomes 220.
The SL only moves upward. The monitor persists each modification intent, sends
the change through the installed `upstox-gtt` skill, fetches the GTT again, and
accepts the change only after the broker's STOPLOSS trigger price is verified.

If the modification response is ambiguous, the broker state cannot be verified,
or the skill does not support GTT modification, the entry is marked
`RECONCILIATION_REQUIRED`; no blind retry or cancel/recreate fallback is used.

## Placement, persistence and notifications

- GTT 1 and GTT 2 are submitted separately, sequentially, with one lot each.
- A separate deterministic operation reference and persisted write intent are
  recorded for each GTT before its broker write.
- If either write is ambiguous or returns no GTT ID, the entry is blocked for
  manual reconciliation; the other GTT is not duplicated or automatically retried.
- The state document records each GTT ID, strategy, initial/current SL, target,
  target source, trailing step, completed trail steps and verified trail history.
- The append-only daily audit stores each rendered prompt, request, response,
  operation reference and result locally and in MongoDB.
- Detailed execution/audit notifications go to the configured private Telegram
  chat. Entry, trailing and reconciliation lifecycle summaries go to the official
  Telegram group. Both routes log delivery failures.
- GTT acceptance is not proof of exchange fill. Each GTT is independently
  monitored and its linked exit order must be reconciled before a fill is confirmed.
- Opposite signals cancel every active GTT leg, reconcile actual net position,
  close the exact reconciled quantity at market and wait for zero before reversal.

## AlgoCLI prompt templates

- `get_ltp.txt` — latest instrument LTP
- `place_gtt.txt` — one normal GTT with calculated SL/target, product I
- `modify_gtt_stop_loss.txt` — modify only the STOPLOSS rule on an existing GTT
- `get_gtt.txt` — inspect a GTT
- `cancel_gtt.txt` — cancel a GTT
- `get_positions.txt` — read current positions
- `place_market_exit.txt` — market exit expressed in lots
- `get_order_details.txt` — inspect a normal order

## External dependency

The current external skill contract must be checked before enabling execution.
The repository does not implement or bypass AlgoCLI internals or skill-level
confirmation. Keep `LIVE_GTT_EXECUTION_ENABLED=false` until unattended
sandbox execution and safe in-place GTT STOPLOSS modification are confirmed.
