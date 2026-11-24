# Supertrend single-GTT execution through AlgoCLI

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
until the separate AlgoCLI team confirms that its unattended sandbox execution path
is implemented and verified.

Both `LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR` and
`LIVE_GTT_MAX_RISK_PER_TRADE_INR` must be positive before entries can be planned.
The worker fails closed if the algoai executable is unavailable.

## Prompt templates

- `get_ltp.txt` — latest instrument LTP
- `place_gtt.txt` — one normal GTT with calculated stop-loss and target, product I
- `get_gtt.txt` — inspect the GTT
- `cancel_gtt.txt` — cancel the GTT
- `get_positions.txt` — read current positions
- `place_market_exit.txt` — market exit expressed in lots
- `get_order_details.txt` — inspect a normal order

The trailing-SL prompt and its adapter method have been removed. The project
places one normal GTT only; it does not place a second GTT or perform runner
stop modifications.

## Subprocess and response handling

The adapter captures stdout, stderr, exit code and duration. The prompt requests valid
JSON; the adapter extracts a JSON object/array from stdout, allowing CLI status lines
around it. A non-zero exit, invalid JSON or timeout fails closed.

Write timeouts and non-zero exits are ambiguous: the broker or skill workflow may have
accepted or staged the write before the CLI failed. The state is marked
`RECONCILIATION_REQUIRED`; the adapter never retries a write automatically. A
deterministic local operation reference is recorded for traceability, but it is not
represented as a broker-enforced idempotency key. Skill-level preview/confirmation
remains inside AlgoCLI; the application stores the JSON returned by the subprocess
and does not create a separate confirmation workflow.

A GTT response ID is required before the service treats placement as submitted.
GTT acceptance is not an exchange fill. The monitor requires a confirmed target/stop
rule and linked completed exit order before classifying a fill.

## Strategy

- BULLISH buys the isolated option; BEARISH buys the opposite option.
- Signal age, frame age and isolation lookback checks remain active.
- Exactly one normal GTT is placed per qualified signal.
- The GTT quantity is the configured `DUMMY_ORDER_QUANTITY` in lots, converted
  using the verified contract lot size. The default remains two lots, now in one GTT.
- Entry uses the current live market price; the fetched LTP is an internal input
  for stop, target and risk calculations, not an entry trigger price.
- The target remains the existing premium-band target: 60, 50 or 40 points
  according to the configured premium thresholds.
- The initial stop is first calculated using the configured percentage below entry.
  The service then checks S1, S2 and S3. If support levels are strictly below the
  calculated stop, it selects the highest such level (the nearest support below
  the calculated stop). If none qualify, it retains the percentage-based stop.
  A support above the calculated stop is never used to widen the stop-loss.
- Both the selected stop and target are tick-rounded before the single GTT prompt
  is rendered. Product `I` (Intraday) is specified in the placement prompt.
- Opposite signals cancel the active GTT, reconcile broker position, convert exact
  net units into lots using the stored lot size, close that lot count at market and
  wait for zero before a reversal is considered. Non-integral lot positions are
  blocked for manual reconciliation.
- Ambiguous or unresolved entries block subsequent automated actions.

## Audit

The daily local JSON and Mongo audit retain the exact rendered prompt, request,
AlgoCLI JSON response, exit code, operation reference and duration. The private
Telegram audit receives the prompt/request/response; the group receives lifecycle
milestones only. Secrets should not be included in CLI output.

## External dependency

The external skill catalog may impose preview/explicit-confirmation requirements.
The CLI permission flag does not replace skill-level confirmation rules. Keep
execution disabled until the external team confirms the unattended sandbox workflow
and structured JSON responses required by this worker.
