# Upstox Trading Pipeline & Global `algoai` CLI Handoff Guide

**Environment:** AWS Ubuntu EC2 (`ubuntu@ip-172-31-37-46`, public IP noted as `13.205.170.171` during session checks; workspace `~/TheProjects/novag7/algoapp`)  
**Last Updated:** November 23, 2026  
**Purpose:** Continuation context for future chats, debugging, cross-project execution via `algoai`, and adding Telegram confirmation + live risk/exit guardrails.

---

## 1. System Overview & What Changed

You migrated from local Windows testing to a dedicated AWS Ubuntu EC2 trading environment running inside a Python virtual environment (`venv`), built custom skills under `~/TheProjects/novag7/algoapp/.agents/skills/`, and added a **global `algoai` command** (`~/.local/bin/algoai`) so you can invoke your `algoapp` agent workflow from **any directory** on the EC2 instance (such as `~/TheProjects/novag7/isolation_app` or `~`) without manually activating `venv` or changing directories first.

Instead of storing static broker tokens inside `.env` or exposing secrets to an LLM agent, `launch_upstox_agy.py` retrieves the live Upstox access token at runtime from MongoDB Atlas (`UPSTOX_APP_V2.app_tokens`) via `fetch_token.py`, injects it as `UPSTOX_ACCESS_TOKEN` into the child process environment, and runs Antigravity CLI (`agy`, observed at `1.5.14` and updated to `1.5.15`) against focused skills.

### Updated Architecture Flow

```text
[Any Working Directory: e.g., ~/TheProjects/novag7/isolation_app or ~]
                  │
                  ▼ (invokes `algoai --prompt "..."` or `algoai -p "..."`)
[~/.local/bin/algoai] ── cd /home/ubuntu/TheProjects/novag7/algoapp & uses venv python3
                  │
                  ▼ (fetches token via fetch_token.py from MongoDB Atlas: UPSTOX_APP_V2.app_tokens)
[launch_upstox_agy.py] ── sets UPSTOX_ACCESS_TOKEN in child process env
                  │
                  ▼ (spawns `agy` with optional --dangerously-skip-permissions, --model, --output-format)
[Antigravity CLI (`agy`)] ── routes to focused `.agents/skills/<name>/SKILL.md`
                  │
                  ▼ (single deterministic script execution)
[Upstox REST APIs (v2 / v3)] ── returns order IDs, GTT IDs, margins, or P&L
```

---

## 2. Infrastructure, Schedule & Quota Context

- **EC2 Workspace & Virtual Environment:** `~/TheProjects/novag7/algoapp` using Python `venv` with `upstox-python-sdk`, `pymongo`, and `python-dotenv`.
- **Global Wrapper Location:** `~/.local/bin/algoai` (executable `chmod +x` and added to `~/.bashrc` `PATH`).
- **Daily Instance Schedule (`07:00–17:00` IST):**
  - The EC2 instance follows a daily stop/start schedule (`7:00 AM` to `5:00 PM` IST).
  - Stopping the instance does **not** consume Antigravity quota or credits while stopped, and files on disk (`~/.local/bin/algoai`, `.agents/skills/...`, `launch_upstox_agy.py`, `fetch_token.py`) persist across restarts.
  - Leaving an idle `agy` prompt open does not burn quota/credits, though exiting interactive sessions cleanly (`/quit` or `Ctrl+D`) avoids keeping background watchers/processes running unnecessarily on Linux.
  - Give network, DNS, and time sync a moment after the `07:00` IST boot before firing automated runs, and remember in-memory sessions end at `17:00` IST.
- **Network & Security:**
  - EC2 origin IP is allowlisted with Upstox.
  - `.env` is excluded via `.gitignore`; prompts instruct agents not to read `.env` or print tokens/secrets.
- **CLI, Model & Quota Profile:**
  - Antigravity CLI (`agy` `1.5.15`) authenticated via Google OAuth (`afg154022@gmail.com`, Google AI Pro).
  - Active model observed in `/usage` and `/model`: **Gemini 3.8 Flash** (`auto · Gemini 3.8 Flash · high` / `low` selectable via `/model` or `--model`).
  - `/usage` displays rolling **5-hour** and **weekly** quota bars per model group (`GEMINI MODELS` vs. `CLAUDE AND GPT MODELS` such as Claude Sonnet 4.6, Claude Opus 4.6, and GPT-OSS 120B).
  - Rate ceiling reference: **Antigravity** (`60 RPM`, `100K TPM`, `100 RPD`) vs. **Gemini 3.5 Flash Lite** (`15 RPM`, `250K TPM`, `500 RPD`) and **Gemini 3.8 Flash** (`5 RPM`, `250K TPM`, `20 RPD`). Keep prompts concise and single-step to stay well inside `100K TPM` and `100 RPD`.

---

## 3. Changes & Components Built So Far

### A. Global CLI Wrapper (`~/.local/bin/algoai`)
- Shell wrapper (`#!/usr/bin/env bash`) that changes into `/home/ubuntu/TheProjects/novag7/algoapp` and executes `launch_upstox_agy.py` using `/home/ubuntu/TheProjects/novag7/algoapp/venv/bin/python3` (falling back to system `python3` if needed), forwarding all CLI arguments (`"$@"`).
- Ensured `~/.local/bin` is exported in `~/.bashrc`.

### B. Core Launcher (`~/TheProjects/novag7/algoapp/launch_upstox_agy.py`)
- Supports both `--prompt` / `-p` and a positional prompt argument (`positional_prompt`), resolving `flag_prompt` first and falling back to `positional_prompt`.
- Supports `--skip-permissions` (passes `--dangerously-skip-permissions` to `agy`), `--model` / `-m` (placed before `-p` so model selection applies reliably in print mode), `--output-format` (`text`, `json`, `stream-json`), and `--env-file` (default `.env`).
- Logs start/end timestamps with timezone labels, elapsed duration, and exit code (`0` / `1`).

### C. Manually Created Skills (`~/TheProjects/novag7/algoapp/.agents/skills/`)
Created/edited via `nano` and verified with `python3 -m py_compile`:

1. **`upstox-gtt`** (`.agents/skills/upstox-gtt/SKILL.md` & `scripts/place_gtt.py`)
   - Places GTT orders via `https://api.upstox.com/v3/order/gtt/place` using `UPSTOX_ACCESS_TOKEN`.
   - Reduced GTT runtime from `4m 23.26s` (broad `upstox` skill) to `1m 8.45s` (`GTT-C26231100146558`).

2. **`upstox-order`** (`.agents/skills/upstox-order/SKILL.md` & `scripts/place_order.py`)
   - Places `MARKET` or `LIMIT` orders via `https://api.upstox.com/v2/order/place`.
   - Enforces positive quantity, sets `price: 0` for `MARKET`, and requires `price > 0` with `0.05` tick rounding for `LIMIT`.
   - Verified with a live `MARKET BUY` order (`261123000291937`) in `52.95s` (`exit code: 0`).

3. **`upstox-gtt-variants`** (`.agents/skills/upstox-gtt-variants/SKILL.md` & `scripts/place_gtt_variant.py`)
   - Supports GTT entry trigger types (`IMMEDIATE`, `ABOVE`, `BELOW`) with optional `STOPLOSS` only, `TARGET` only, or both `STOPLOSS` and `TARGET`.
   - Rounds trigger prices to `0.05` ticks, selects `SINGLE` (1 rule) vs. `MULTIPLE` (2–3 rules), and sets opposite exit side (`SELL` for `BUY`, `BUY` for `SELL`).

4. **`upstox-pnl`** (`.agents/skills/upstox-pnl/SKILL.md` & `scripts/fetch_today_pnl.py`)
   - Queries `https://api.upstox.com/v2/portfolio/short-term-positions` and aggregates `total_pnl`, `positions_count`, and per-position fields (`symbol`, `instrument_token`, `product`, `net_quantity`, `pnl`).
   - Verified both inside `algoapp` (`24.96s` and `26.41s`, `exit code: 0`) and from `isolation_app` via global `algoai --prompt ...` (`25.50s`, `exit code: 0`).

---

## 4. Command Reference (`algoai`)

```bash
# Run from any directory (e.g., ~/TheProjects/novag7/isolation_app) using --prompt
algoai --skip-permissions --prompt "Use upstox-pnl skill to fetch today's P&L and positions. Do not read .env."

# Run using short -p flag
algoai --skip-permissions -p "Use upstox-order skill. Place LIMIT BUY order for NSE_FO|64473 qty 65 price 43.00 product D. Print only script output."

# Run a GTT variant
algoai --skip-permissions --prompt "Use upstox-gtt-variants. Place IMMEDIATE BUY GTT for NSE_FO|64473 qty 65 entry 43.00 SL 30.10 target 83.00. Print only script output."

# Open interactive agy inside algoapp with token loaded
algoai
```

---

## 5. Verified Runs & Operational Observations

| Run / Check | Command Style | Outcome | Approx. Duration |
| :--- | :--- | :--- | :--- |
| Fund & Margin Check | `launch_upstox_agy.py` | Succeeded (`exit code: 0`), Equity margin `₹1,400.00` | ~37–40 s |
| Broad Skill GTT (`upstox`) | `launch_upstox_agy.py` | GTT `GTT-C26231100131059` placed; `ENTRY` leg `FAILED` on margin shortfall (`₹3,917.00` needed); SL/Target `INACTIVE` | `4m 23.26s` |
| Custom Skill GTT (`upstox-gtt`) | `launch_upstox_agy.py` | GTT `GTT-C26231100146558` placed (`exit code: 0`), ~3.8x faster than broad skill loop | `1m 8.45s` |
| Direct Order (`upstox-order`) | `launch_upstox_agy.py` | Live `MARKET BUY` (`NSE_FO\|64473`, qty `65`, `D`) succeeded (`exit code: 0`), order ID `261123000291937` | `52.95s` |
| Today P&L (`upstox-pnl`) | `launch_upstox_agy.py` | Succeeded (`exit code: 0`), `NIFTY26NOV26900CE` (qty `65`, `D`, `NSE_FO\|64545`), P&L `₹-65.00` then `₹-139.75` | `24.96s` / `26.41s` |
| Cross-Directory P&L via `algoai` | `algoai --skip-permissions --prompt ...` from `isolation_app` | Succeeded (`exit code: 0`), `NIFTY26NOV26900CE` (qty `65`, `D`, `NSE_FO\|64545`), P&L `₹-139.75` | `25.50s` |

### Key Practical Rules
- **Run `algoai` in Bash, Not Inside Interactive `agy`:** Typing `python3 launch_upstox_agy.py ...` or `algoai ...` inside an interactive `agy` session runs it as a shell tool inside `agy` (and can miss `UPSTOX_ACCESS_TOKEN` if `agy` was launched directly without `launch_upstox_agy.py`).
- **Keep Prompts Focused & Name the Skill:** Explicitly name the skill (`Use upstox-pnl skill...`, `Use upstox-order skill...`, `Use upstox-gtt-variants...`) and ask to print only script output where appropriate to keep runs around `~25–68s`.
- **Direct Python for Sub-Second Latency:** LLM-routed `algoai` calls take tens of seconds due to CLI startup, skill loading, and model turns; call `.py` scripts directly when sub-second execution is required.
- **Permission & Confirmation Guardrail:** Reserve `--skip-permissions` for controlled testing; wire explicit confirmation (such as via Telegram) before live money execution.

---

## 6. Planned Next Steps

1. **Telegram Confirmation Workflow:** Build a structured Telegram approval handler (`Approve` / `Reject` callbacks, chat/user ID check, 64-byte-safe callback IDs) before placing or exiting live orders.
2. **Dynamic SL/Target & Exit/GTT Helpers:** Add helpers for dynamic stop-loss (`Entry * 0.70`), point targets rounded to `0.05` ticks, position square-off/exit, and GTT listing/cancellation.
3. **Position & Margin Awareness:** Monitor open positions (`NIFTY26NOV26900CE`, qty `65`) and available Equity margin (`₹1,400.00`) before placing additional F&O orders.