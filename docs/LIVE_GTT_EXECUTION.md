# Live Supertrend GTT execution

## Safety and activation

Live execution is disabled by default. The application does not place orders
unless LIVE_GTT_EXECUTION_ENABLED=true and LIVE_GTT_KILL_SWITCH=false.
The two INR risk caps must also be configured to positive values:
LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR and LIVE_GTT_MAX_RISK_PER_TRADE_INR.

The strategy worker is a dedicated single-thread executor. WebSocket callbacks
only enqueue a copied event; they do not wait for Upstox, MongoDB or Telegram.

## Strategy

- BULLISH buys the isolated option; BEARISH buys the opposite option.
- Signal time must be at least 60 seconds before isolation time, inclusive.
- Signal age and frame receive age are independently checked.
- Two separate one-lot MULTIPLE GTT brackets are created.
- Lot 1 uses the configured premium-band target: entry above 200 gets +60;
  100 through 200 gets +50; below 100 gets +40.
- Lot 2 uses 15-minute R3 of the actual purchased contract.
- Both lots start with the configured percentage stop, adjusted by the
  contract's S1/S2/S3 support rule.
- After lot 1 target execution is confirmed by a linked normal order with
  COMPLETE/FILLED status, the runner GTT stop is modified to the midpoint
  between entry and target 1. All other GTT rules are retained.
- Default entry limits are two entries before 09:40 and one at/after 09:40.
  Reversal consumes another entry. After 09:40 the default is close-only,
  without reopening the opposite leg.
- Missing R3, missing lot/tick metadata, stale quotes, unrelated open positions,
  and missing risk caps fail closed.

## Audit

Every GTT placement, cancellation, modification and close request gets a
JSON-only deterministic execution contract, request payload, actual response,
timestamp, action and isolation ID.

Local journal: data/live_gtt_audit/YYYY-MM-DD.json.
Mongo collection: live_gtt_execution_audit, one document per IST date with an
events array. The array is capped by LIVE_GTT_AUDIT_MAX_EVENTS to avoid the
MongoDB 16 MB document limit. State is stored separately per isolation in
live_gtt_execution_state and in data/live_gtt_state.

The prompt is an audit contract, not an LLM instruction used to authorize or
calculate trades. Python strategy code calculates every order parameter.
The API response stored is the actual Upstox response; it is not fabricated.

## Telegram routing

Normal chat receives the detailed prompt, request and JSON response.
The official group receives only concise order lifecycle milestones.
Raw diagnostics, prompt text and full broker payloads are never sent to group.

## Broker lifecycle limitation

GTT acceptance is not an exchange fill. The monitor only changes the runner
stop after it can identify a linked normal order with COMPLETE/FILLED status
and an explicit TARGET rule outcome. If the SDK/API response does not expose
those identifiers/statuses, the service does not infer a fill and leaves the
entry in reconciliation-required state.

Reversal requests GTT cancellation, confirms terminal GTT states, reads the
actual broker position, closes the observed net long quantity and waits for
position zero before submitting the reverse entry. Ambiguous broker writes
are not retried automatically.

The Upstox SDK response schema/status vocabulary must be verified in the
deployed environment and sandbox-tested before live activation. No live broker
test or successful fill is claimed by this implementation.
