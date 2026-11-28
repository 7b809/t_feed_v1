---
name: upstox-portfolio
description: Fetch Upstox holdings and current positions with complete raw API data. Read-only.
category: Portfolio
access: read-only
execution: direct-python
script: scripts/fetch_portfolio.py
result_schema: schemas/result.schema.json
---

# Upstox Portfolio Data

Fetch holdings or current positions. This skill is read-only and preserves the complete SDK response in `raw_response`, alongside the record array.

```bash
algoai run upstox-portfolio --result-format json --script-args-json '["--kind","holdings"]'
algoai run upstox-portfolio --result-format json --script-args-json '["--kind","positions"]'
```

Supported `--kind holdings|positions`. Holdings and positions use the Upstox PortfolioApi v2 endpoints. No order is placed, modified, converted, or exited. Never read `.env` or expose credentials.
