---
name: upstox-pnl
description: Fetch today's Upstox positions and P&L using UPSTOX_ACCESS_TOKEN from the environment. Never read .env or print tokens.
category: Portfolio
access: read-only
execution: direct-python
script: scripts/fetch_today_pnl.py
result_schema: schemas/result.schema.json
---

# Upstox Today P&L Skill

## Rules
- Never read .env or print secrets/tokens.
- Read UPSTOX_ACCESS_TOKEN directly from environment variables.
- The launcher injects UPSTOX_ACCESS_TOKEN from the existing MongoDB token loader.
- For normal text execution, run the declared script through AGY.
- For --result-format json, the launcher runs the declared script directly, validates its JSON against schemas/result.schema.json, and prints only the validated JSON payload to stdout.
- Do not make extra API calls unless explicitly asked.
