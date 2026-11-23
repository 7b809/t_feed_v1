---
name: upstox-pnl
description: Fetch today's Upstox positions and P&L using UPSTOX_ACCESS_TOKEN from the environment. Never read .env or print tokens.
---

# Upstox Today P&L Skill

## Rules
- Never read `.env` or print secrets/tokens.
- Read `UPSTOX_ACCESS_TOKEN` directly from environment variables.
- Run `.agents/skills/upstox-pnl/scripts/fetch_today_pnl.py` once and print its output cleanly.
- Do not make extra API calls unless explicitly asked.
