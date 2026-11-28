# ALGOAI CLI v2

The modular CLI retains the existing launch_upstox_agy.py entry point and ~/.local/bin/algoai wrapper contract.

## Commands

    algoai help
    algoai --version
    algoai status
    algoai skills
    algoai skills upstox-pnl
    algoai skills --search "order fetch"
    algoai skills --json
    algoai run upstox-pnl
    algoai run upstox-pnl --result-format json
    algoai run upstox-orders-fetch "Show today's pending orders"
    algoai --skip-permissions --prompt "Existing free-form prompt"

## JSON result mode

--output-format controls AGY's output protocol. It does not define a skill's business-data schema.

--result-format json is a separate machine-readable result contract. It is available only for skills that declare a deterministic handler and JSON Schema in SKILL.md:

    execution: direct-python
    script: scripts/handler.py
    result_schema: schemas/result.schema.json

The CLI runs the declared Python script with the same injected Upstox access token, captures stdout, parses JSON, validates the schema, and emits only the validated JSON object to stdout. No AGY prose, timing lines, progress text, or logging metadata are written to stdout. Errors go to stderr and the process exits non-zero. This mode fails closed when a skill has no declared handler/schema.

The P&L skill defines status, positions_count, total_pnl, and positions, including typed position fields, in .agents/skills/upstox-pnl/schemas/result.schema.json.

## Skill discovery

Search precedence:
1. ALGOAI_SKILLS_DIR
2. ~/.config/algoai/skills
3. Application-root .agents/skills
4. Current-workspace .agents/skills

Earlier roots win on duplicate skill names. Relative explicit paths resolve against the application root. ALGOAI_APP_DIR can override the application root.

## Safety and logs

Write-capable skills require --allow-write; --skip-permissions independently bypasses AGY approval prompts and is not trade authorization. Exact action confirmations remain mandatory.

Execution metadata is appended to ~/.local/state/algoai/executions.jsonl (or ALGOAI_LOG_FILE). Prompts, tokens, and environment values are never logged. The CLI modules use the standard library; existing token retrieval still requires python-dotenv and pymongo.
