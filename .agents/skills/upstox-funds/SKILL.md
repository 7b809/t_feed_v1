---
name: upstox-funds
description: Fetch available Upstox funds and margin balances for all supported segments. Read-only; uses the launcher-injected UPSTOX_ACCESS_TOKEN.
category: Portfolio
access: read-only
execution: direct-python
script: scripts/fetch_funds.py
result_schema: schemas/result.schema.json
---

# Upstox Available Funds Skill

Fetch the current funds/margin balance from Upstox using `UserApi.get_user_fund_margin_v3()`.

## Security and execution
- Read `UPSTOX_ACCESS_TOKEN` only from the process environment.
- Never read `.env`, print credentials, or include authorization headers in output.
- The `algoai` launcher loads the token through the existing MongoDB token loader and injects it into this script's environment.
- This is a read-only API operation. Never place, modify, or cancel orders.
- For `--result-format json`, the launcher executes the declared Python script directly and validates its output against `schemas/result.schema.json`.
- Preserve every segment returned by the API. Do not assume only equity and commodity segments exist.
- Report the API's available/used margin and other returned fund fields without calculating or inventing values.
