# ALGOAI CLI v2

The modular CLI retains the existing launch_upstox_agy.py entry point and ~/.local/bin/algoai wrapper contract.

## Commands

    algoai help
    algoai --version
    algoai status
    algoai skills
    algoai skills upstox-funds
    algoai run upstox-funds --result-format json
    algoai run upstox-orders-fetch --result-format json --script-args-json '["--kind","all"]'
    algoai run upstox-portfolio --result-format json --script-args-json '["--kind","holdings"]'
    algoai run upstox-order --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","1","--order-type","LIMIT","--price","100"]'
    algoai run upstox-gtt --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","1","--entry-trigger","100","--sl-trigger","95","--target-trigger","110"]'\n    algoai run upstox-margin --result-format json --script-args-json '["--kind","brokerage","--instrument","NSE_EQ|<ISIN>","--quantity","1","--transaction-type","BUY","--product","D","--price","100"]'
    algoai run upstox-gtt-modify --allow-write --result-format json --script-args-json '["--gtt-order-id","<id>","--update","TARGET=300"]'
    algoai --skip-permissions --prompt "Existing free-form prompt"

## JSON result mode

--output-format controls AGY's output protocol. It does not define a skill's business-data schema.

--result-format json is a separate machine-readable result contract. It is available only for skills that declare a deterministic handler and JSON Schema in SKILL.md:

    execution: direct-python
    script: scripts/handler.py
    result_schema: schemas/result.schema.json

The CLI runs the declared Python script with the same injected Upstox access token and passes only explicit string arguments supplied in --script-args-json (a JSON array). It captures stdout, parses JSON, validates the schema, and emits only the validated JSON object to stdout. Errors and handler stderr go to stderr. Credentials and environment values are never included in output or execution logs.

Read-only JSON skills include funds, order fetch (normal and GTT), portfolio holdings/positions, and margin/brokerage calculations. Normal order placement and GTT placement have preview-first JSON handlers: --allow-write plus the exact preview hash is required for execution. GTT modification also has a direct JSON handler and requires --allow-write, its preview, exact state_fingerprint confirmation, re-fetch and verification.

Normal order placement and GTT placement now use direct JSON handlers that are preview-only by default. Do not use --skip-permissions or --allow-write as a substitute for explicit approval of the exact live order and preview hash.

## Skill discovery

Search precedence:
1. ALGOAI_SKILLS_DIR
2. ~/.config/algoai/skills
3. Application-root .agents/skills
4. Current-workspace .agents/skills

Earlier roots win on duplicate skill names. Relative explicit paths resolve against the application root. ALGOAI_APP_DIR can override the application root.

## Safety and logs

Write-capable skills require --allow-write; --skip-permissions independently bypasses AGY approval prompts and is not trade authorization. Exact action confirmations remain mandatory.

Execution metadata is appended to ~/.local/state/algoai/executions.jsonl (or ALGOAI_LOG_FILE). Prompts, tokens, and environment values are never logged. CLI modules use the standard library; existing token retrieval still requires python-dotenv and pymongo.
