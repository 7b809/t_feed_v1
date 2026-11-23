# Upstox Trading Pipeline & Antigravity CLI (`agy`) Handoff Guide

**Environment:** AWS Ubuntu EC2 (`ubuntu@ip-172-31-37-46:~/TheProjects/novag7/algoapp`)  
**Last Updated:** November 23, 2026  
**Purpose:** Continuation context for future chats, debugging, and adding Telegram confirmation + live risk/exit guardrails.

---

## 1. What Is Happening in This System

You have migrated from local Windows testing to a dedicated AWS Ubuntu EC2 trading workspace (`~/TheProjects/novag7/algoapp`) running inside a Python virtual environment (`venv`).

Instead of hardcoding broker tokens into `.env` or passing secrets to an LLM agent, your wrapper script (`launch_upstox_agy.py`) fetches the live Upstox access token at runtime from MongoDB Atlas (`UPSTOX_APP_V2.app_tokens`) via `fetch_token.py`, injects it as `UPSTOX_ACCESS_TOKEN` into the child process environment, and invokes Antigravity CLI (`agy` v1.5.14) against narrow, task-specific skills under `.agents/skills/`.

### Current Architecture Flow

```text
[MongoDB Atlas: UPSTOX_APP_V2.app_tokens]
                  │
                  ▼ (runtime fetch via fetch_token.py)
[launch_upstox_agy.py] ── sets UPSTOX_ACCESS_TOKEN in child env
                  │
                  ▼ (spawns `agy` with optional --dangerously-skip-permissions)
[Antigravity CLI (`agy`)] ── routes to focused `.agents/skills/<name>/SKILL.md`
                  │
                  ▼ (single script execution)
[Upstox REST APIs (v2 / v3)] ── returns order IDs, GTT IDs, margins, or P&L
```

---

## 2. Infrastructure, Auth & Quota Context

* **EC2 Workspace:** `~/TheProjects/novag7/algoapp` using Python `venv` with `upstox-python-sdk`, `pymongo`, and `python-dotenv`.
* **Network & Security:**
  * EC2 origin IP is allowlisted with Upstox.
  * `.env` is gitignored; prompts explicitly instruct the agent not to read `.env` or print tokens/secrets.
* **CLI & Quota Profile:**
  * Migrated to Antigravity CLI (`agy` v1.5.14) using Google OAuth (Google AI Pro account).
  * Observed rate ceilings: **Antigravity** (`60 RPM`, `100K TPM`, `100 RPD`) vs. **Gemini 3.5 Flash Lite** (`15 RPM`, `250K TPM`, `500 RPD`) and **Gemini 3.8 Flash** (`5 RPM`, `250K TPM`, `20 RPD`).
  * Because Antigravity has a `100K TPM` and `100 RPD` ceiling, keeping prompts short and single-step prevents wasted retries and token bloat.

---

## 3. Changes & Components Built So Far

### A. Core Launcher (`launch_upstox_agy.py`)
* Added `--skip-permissions` to pass `--dangerously-skip-permissions` to `agy` for unattended runs.
* Added `--output-format` (`text`, `json`, `stream-json`).
* Placed `--model` before `-p` so model selection applies reliably in print/non-interactive mode.
* Logs start time, end time, timezone, elapsed duration, and exit code (`0` / `1`).

### B. Manually Created Skills (`.agents/skills/`)
All custom skills were created/edited using `nano` and verified with `python3 -m py_compile`:

1. **`upstox-gtt`** (`.agents/skills/upstox-gtt/SKILL.md` & `scripts/place_gtt.py`)
   * Places GTT orders via `https://api.upstox.com/v3/order/gtt/place` using `UPSTOX_ACCESS_TOKEN`.
   * Reduced GTT run duration from `4m 23.26s` (broad `upstox` skill) to `1m 8.45s` (`GTT-C26231100146558`).

2. **`upstox-order`** (`.agents/skills/upstox-order/SKILL.md` & `scripts/place_order.py`)
   * Places direct `MARKET` or `LIMIT` orders via `https://api.upstox.com/v2/order/place`.
   * Validates positive quantity, sets `price: 0` for `MARKET`, and requires `price > 0` with `0.05` tick rounding for `LIMIT`.
   * Verified with a live `MARKET BUY` order (`261123000291937`) in `52.95s` (`exit code: 0`).

3. **`upstox-gtt-variants`** (`.agents/skills/upstox-gtt-variants/SKILL.md` & `scripts/place_gtt_variant.py`)
   * Supports GTT entry modes (`IMMEDIATE`, `ABOVE`, `BELOW`) with optional `STOPLOSS` only, `TARGET` only, or both `STOPLOSS` and `TARGET`.
   * Automatically rounds trigger prices to `0.05` ticks, sets `SINGLE` (1 rule) vs. `MULTIPLE` (2–3 rules), and sets opposite exit transaction side (`SELL` for `BUY`, `BUY` for `SELL`).

4. **`upstox-pnl`** (`.agents/skills/upstox-pnl/SKILL.md` & `scripts/fetch_today_pnl.py`)
   * Queries `https://api.upstox.com/v2/portfolio/short-term-positions` and aggregates total P&L, position count, and per-position details (`symbol`, `instrument_token`, `product`, `net_quantity`, `pnl`).
   * Verified in `24.96s` (`exit code: 0`), showing 1 open position (`NIFTY26NOV26900CE`, qty `65`, `NSE_FO|64545`, P&L `₹-65.00`).

---

## 4. Verified Runs & Operational Observations

| Run / Check | Purpose | Outcome | Approx. Duration |
| :--- | :--- | :--- | :--- |
| Fund & Margin Check | Read-only check via `launch_upstox_agy.py` | Succeeded (`exit code: 0`), Equity margin `₹1,400.00` | ~37–40 s |
| Broad Skill GTT (`upstox`) | Multi-leg GTT (`GTT-C26231100131059`) | GTT placed; `ENTRY` leg `FAILED` on margin shortfall (`₹3,917.00` needed); SL/Target `INACTIVE` | `4m 23.26s` |
| Custom Skill GTT (`upstox-gtt`) | Focused GTT placement (`GTT-C26231100146558`) | Succeeded (`exit code: 0`), ~3.8x faster than broad skill loop | `1m 8.45s` |
| Direct Order (`upstox-order`) | Live `MARKET BUY` (`NSE_FO\|64473`, qty `65`, `D`) | Succeeded (`exit code: 0`), order ID `261123000291937` | `52.95s` |
| Today P&L (`upstox-pnl`) | Short-term positions & net P&L | Succeeded (`exit code: 0`), 1 position (`NIFTY26NOV26900CE`, qty `65`, P&L `₹-65.00`) | `24.96s` |

### Key Practical Rules
* **Latency vs. Speed:** Narrow skills with strict `"Print only script output."` instructions brought runs down to **~25–68 seconds**. For sub-second entries/exits on 5–7 daily trades, call Python scripts directly rather than routing through `agy`.
* **Disambiguate Skill Names:** Because `upstox-gtt` and `upstox-gtt-variants` both deal with GTT orders, explicitly name the skill in prompts (`Use upstox-gtt-variants...`, `Use upstox-order...`, `Use upstox-pnl...`).
* **Safety Guardrail:** Reserve `--skip-permissions` for controlled testing; require explicit confirmation (such as via Telegram) before placing live money orders.

---