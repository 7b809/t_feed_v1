# Supertrend GTT execution through independent AlgoCLI

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
AlgoCLI implementation. The algoai executable must already be available on
the host PATH. Authentication and broker access remain inside the CLI and its
skills; this project does not read broker tokens or .env files for execution.

## Activation and safety

`LIVE_GTT_EXECUTION_ENABLED=false` remains the required default. Keep it false
until the separate AlgoCLI team confirms that its unattended sandbox execution path
is implemented and verified. This project intentionally has no sandbox selector,
sandbox token, or broker credential setting.

The kill switch must remain false only when deliberately enabling the worker.
Both `LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR` and
`LIVE_GTT_MAX_RISK_PER_TRADE_INR` must be positive before entries can be planned.
The execution worker fails closed if the algoai executable is unavailable.

## Prompt templates

Templates are fixed files; runtime code substitutes only named values:
- `get_ltp.txt` — latest instrument LTP
- `place_gtt.txt` — immediate live-market entry with stop-loss and target only
- `get_gtt.txt` — inspect one GTT
- `cancel_gtt.txt` — cancel one GTT
- `modify_gtt_stop.txt` — modify one GTT stop-loss
- `get_positions.txt` — read current positions
- `place_market_exit.txt` — market exit expressed in lots
- `get_order_details.txt` — inspect one normal order

Prompts explicitly name the skill from the external skills catalog. The main project
does not generate new operational instructions or copy the external skill implementation.

## Subprocess and response handling

The adapter captures stdout, stderr, exit code and duration. The prompt requests valid JSON;
the adapter extracts a JSON object/array from stdout, allowing CLI status lines around it.
A non-zero exit, invalid JSON, missing response fields or timeout fails closed.

Write timeouts and non-zero exits are ambiguous: the broker may have accepted the write
before the CLI failed. The state is marked `RECONCILIATION_REQUIRED`; the adapter
never retries a write automatically. A deterministic local operation reference is
recorded for traceability, but it is not represented as a broker-enforced idempotency key.

A GTT response ID is required before the service treats placement as submitted.
GTT acceptance is not an exchange fill. The monitor continues to require a confirmed
target/stop rule and linked completed exit order before classifying a fill.

## Strategy retained

- BULLISH buys the isolated option; BEARISH buys the opposite option.
- Signal age, frame age and isolation lookback checks remain active.
- Two separate one-lot GTT brackets are requested. Each prompt identifies the instrument by its unique instrument key and expresses size in lots, not units or quantity.
- Entry uses the current live market price (IMMEDIATE); only stop-loss and target prices are supplied as GTT price values. The live LTP fetched by the worker remains an internal input for risk and target calculations, not an entry trigger value.
- Lot 1 uses the configured premium-band target; lot 2 uses the purchased contract's
  15-minute R3.
- Initial stop uses the configured percentage adjusted by S1/S2/S3 support logic.
- After confirmed lot-1 target fill, runner stop moves to the midpoint.
- Opposite signals cancel existing GTTs, reconcile broker position, convert the exact
  net units into lots using the stored lot size, close that lot count at market and wait
  for zero before a reversal is considered. Non-integral lot positions are blocked for
  manual reconciliation.
- Ambiguous or unresolved entries block subsequent automated actions.

## Audit

The daily local JSON and Mongo audit retain the exact rendered prompt, request,
AlgoCLI JSON response, exit code, operation reference and duration. The private
Telegram audit receives the prompt/request/response; the group receives lifecycle
milestones only. Secrets are not sourced by this project and should not be included
in CLI output.

## Important external dependency

The external skill catalog includes preview/explicit-confirmation requirements for
some trading writes. The CLI permission flag does not replace the skill-level
confirmation rules. Keep execution disabled until the external team confirms the
unattended sandbox workflow and structured JSON responses required by this worker.
