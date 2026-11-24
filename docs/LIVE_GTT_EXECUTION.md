# Supertrend GTT execution through independent AlgoCLI

## Execution boundary

The isolation project owns signal qualification, instrument selection, strategy calculations,
risk checks, entry limits, local/Mongo state, monitoring decisions and audit records.
It does not create an Upstox SDK client and does not retrieve broker credentials.

All broker operations are sent as short, predefined prompt templates under
`prompts/algocli/` to a separate AlgoCLI project through the Python launcher.
The external AlgoCLI project owns its authentication, token retrieval, skill execution
and broker API access. This repository does not modify that external project.

Configure `ALGOCLI_PROJECT_DIR` to the independent checkout. The default launcher
filename is `launch_upstox_agy.py`; override it with `ALGOCLI_LAUNCHER_PATH`
if the external project uses a different launcher. Set `ALGOCLI_PYTHON` to the
external project virtualenv interpreter when it differs from the isolation app
interpreter (default: `python3`). The launcher is invoked with
`--output-format json --prompt <rendered-template>`. `ALGOCLI_SKIP_PERMISSIONS`
defaults to false and must not be treated as trade authorization.

## Activation and safety

`LIVE_GTT_EXECUTION_ENABLED=false` remains the required default. Keep it false
until the separate AlgoCLI team confirms that its unattended sandbox execution path
is implemented and verified. This project intentionally has no sandbox selector,
sandbox token, or broker credential setting.

The kill switch must remain false only when deliberately enabling the worker.
Both `LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR` and
`LIVE_GTT_MAX_RISK_PER_TRADE_INR` must be positive before entries can be planned.
The execution worker also fails closed when the independent AlgoCLI project path
or launcher is missing.

## Prompt templates

Templates are fixed files; runtime code substitutes only named values:
- `get_ltp.txt` — latest instrument LTP
- `place_gtt.txt` — standard entry + stop-loss + target GTT
- `get_gtt.txt` — inspect one GTT
- `cancel_gtt.txt` — cancel one GTT
- `modify_gtt_stop.txt` — modify one GTT stop-loss
- `get_positions.txt` — read current positions
- `place_market_exit.txt` — market exit for a reconciled quantity
- `get_order_details.txt` — inspect one normal order

Prompts explicitly name the skill from the external skills catalog. The main project
does not generate new operational instructions or copy the external skill implementation.

## Subprocess and response handling

The adapter captures stdout, stderr, exit code and duration. It requests JSON output
and extracts a JSON object/array from stdout, allowing launcher timing lines around it.
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
- Two separate one-lot GTT brackets are requested.
- Lot 1 uses the configured premium-band target; lot 2 uses the purchased contract's
  15-minute R3.
- Initial stop uses the configured percentage adjusted by S1/S2/S3 support logic.
- After confirmed lot-1 target fill, runner stop moves to the midpoint.
- Opposite signals cancel existing GTTs, reconcile broker position, close the observed
  net quantity and wait for zero before a reversal is considered.
- Ambiguous or unresolved entries block subsequent automated actions.

## Audit

The daily local JSON and Mongo audit retain the exact rendered prompt, request,
AlgoCLI JSON response, exit code, operation reference and duration. The private
Telegram audit receives the prompt/request/response; the group receives lifecycle
milestones only. Secrets are not sourced by this project and should not be included
in CLI output.

## Important external dependency

The external skill catalog includes preview/explicit-confirmation requirements
for trading writes, including GTT placement/modification and normal orders. This
project does not bypass those rules or treat --skip-permissions as trade approval.
Before unattended operation is enabled, the external AlgoCLI team must provide a
compatible, approved sandbox workflow and structured JSON responses for each
required operation. No sandbox execution, broker submission or successful fill
is claimed by this integration change.
