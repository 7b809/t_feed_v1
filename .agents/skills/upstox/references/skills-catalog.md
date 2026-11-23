# Upstox Skills Catalog and Prompt Guide

This document describes the Upstox agent skills currently present in this repository, when to use each one, the scripts and reference material associated with them, and copy-ready `algoai` prompt examples.

> **Scope:** This catalog reflects the skill directories and files present on branch `algocli_v1` when this document was created. It is a guide to the repository implementation, not a guarantee that an API request will succeed or that an order will execute.

## 1. Quick skill directory

| Skill | Main purpose | Read / write | Skill entry point |
|---|---|---|---|
| `upstox` | General Upstox API workflows, market data, portfolio, instrument lookup, order operations and streaming | Read and trade-capable; apply safety rules for writes | `.agents/skills/upstox/SKILL.md` |
| `upstox-order` | Place a normal MARKET or LIMIT order | Writes a live order | `.agents/skills/upstox-order/SKILL.md` |
| `upstox-gtt` | Place a standard GTT order with entry, stop-loss and target triggers | Writes a GTT order | `.agents/skills/upstox-gtt/SKILL.md` |
| `upstox-gtt-variants` | Place GTT variants with IMMEDIATE / ABOVE / BELOW entry and optional SL / target legs | Writes a GTT order | `.agents/skills/upstox-gtt-variants/SKILL.md` |
| `upstox-gtt-modify` | Inspect and modify exactly one trigger leg on an existing GTT | Read first; modification requires explicit confirmation | `.agents/skills/upstox-gtt-modify/SKILL.md` |
| `upstox-orders-fetch` | Fetch normal orders, GTT orders, or a specific order; filter results | Read-only | `.agents/skills/upstox-orders-fetch/SKILL.md` |
| `upstox-pnl` | Fetch today's positions and P&L | Read-only | `.agents/skills/upstox-pnl/SKILL.md` |

The six named skills after the general `upstox` skill are focused workflows. The general skill also contains shared API guidance and reference documents; it is not limited to a single script.

## 2. General Upstox skill

### `upstox`

**Use it for:** General Upstox requests that do not fit a narrower dedicated workflow, including instrument resolution, quotes and market data, holdings, positions, funds and margins, historical candles, option-chain analysis, WebSocket streams, and supported order operations.

**Important implementation notes:**
- Uses the official `upstox-python-sdk`.
- Shared client and instrument utilities are under `.agents/skills/upstox/scripts/`.
- `upstox_helpers.py` centralizes client/token setup.
- `instrument_search.py` resolves tradable instruments; resolve an instrument key rather than guessing a derivative token.
- `validate_order.py` provides order validation support.
- The skill distinguishes Upstox v2 and v3 SDK classes and method signatures. Consult the relevant reference before coding or invoking an endpoint.
- Credentials must come from environment variables. Never expose access tokens or hardcode credentials.
- For live order placement, modification, cancellation, GTT placement, or other consequential trading actions, follow the safety guardrails in the main skill, including a clear preview and explicit user confirmation.

**Sample prompts:**

General account and portfolio:
```bash
algoai --skip-permissions --prompt "Use the upstox skill to fetch my current holdings, positions, available funds, and margin information. Keep the operation read-only. Do not read .env or print access tokens. Return the available API data as valid JSON."
```

Market data:
```bash
algoai --skip-permissions --prompt "Use the upstox skill to fetch the latest quote and OHLC data for NSE_INDEX|Nifty 50. Do not place or modify any orders. Do not read .env or expose credentials. Return the response as JSON."
```

Instrument lookup:
```bash
algoai --skip-permissions --prompt "Use the upstox skill and its instrument search utility to resolve the current tradable instrument key and lot size for NIFTY options matching [expiry, strike, CE/PE]. Do not guess tokens. Do not place an order. Do not read .env. Return the resolved instrument details as JSON."
```

## 3. Normal order placement

### `upstox-order`

**Use it for:** Placing one normal Upstox MARKET or LIMIT order using the dedicated `place_order.py` script.

**Script:** `.agents/skills/upstox-order/scripts/place_order.py`

**Supported order modes described by the skill:**
- MARKET: `--order-type MARKET --price 0`
- LIMIT: `--order-type LIMIT --price <price>`

This is a live trading workflow, not a preview-only/read-only workflow. Provide the exact instrument, BUY/SELL side, quantity, product, order type and (for LIMIT) price. Do not infer missing trade parameters. Follow the main Upstox skill's order safety checks and obtain explicit confirmation before a live order is submitted.

**Sample prompts:**

```bash
algoai --skip-permissions --prompt "Use the upstox-order skill to prepare a LIMIT BUY order for [instrument_key], quantity [quantity], product [D/I], price [limit_price]. Do not read .env or print credentials. First show the complete order details and validation result. Do not submit the live order until I explicitly confirm the exact preview."
```

```bash
algoai --skip-permissions --prompt "Use the upstox-order skill to prepare a MARKET SELL order for [instrument_key], quantity [quantity], product [D/I]. Do not assume any missing values. Show the full order preview and wait for my explicit confirmation before submitting. Do not read .env or expose tokens."
```

## 4. Standard GTT placement

### `upstox-gtt`

**Use it for:** Placing a standard GTT order with an instrument, quantity, entry trigger, stop-loss trigger and target trigger.

**Script:** `.agents/skills/upstox-gtt/scripts/place_gtt.py`

**Inputs used by the script:**
- `--instrument "<instrument_key>"`
- `--quantity <qty>`
- `--entry-trigger <entry_price>`
- `--sl-trigger <sl_price>`
- `--target-trigger <target_price>`

A GTT is a conditional order workflow. Treat placement as a live trading action. Confirm the exact instrument, side/strategy semantics, quantity and all trigger prices before submission. Do not read `.env` or expose credentials.

**Sample prompt:**
```bash
algoai --skip-permissions --prompt "Use the upstox-gtt skill to prepare a GTT for instrument [instrument_key], quantity [quantity], entry trigger [entry_price], stop-loss trigger [sl_price], and target trigger [target_price]. Do not read .env or print tokens. Validate the supplied values, show the complete GTT preview, and wait for my explicit confirmation before placing it."
```

## 5. GTT variants

### `upstox-gtt-variants`

**Use it for:** GTT configurations with different entry trigger styles and optional protective/target legs.

**Script:** `.agents/skills/upstox-gtt-variants/scripts/place_gtt_variant.py`

**Supported configuration described by the skill:**
- Immediate / market-style entry: `--entry-trigger-type IMMEDIATE`
- Price-level entry: `--entry-trigger-type ABOVE` or `BELOW`, with `--entry-price <price>`
- Optional stop-loss leg: `--sl-price <sl>`
- Optional target leg: `--target-price <target>`
- Omitted legs are left out automatically; the script uses `SINGLE` for one rule and `MULTIPLE` for two or three rules.

**Sample prompts:**

Immediate entry with both protective legs:
```bash
algoai --skip-permissions --prompt "Use the upstox-gtt-variants skill to prepare an IMMEDIATE-entry GTT for [instrument_key], quantity [quantity], with stop-loss [sl_price] and target [target_price]. Validate and show the complete preview. Do not place it until I explicitly confirm. Do not read .env or expose tokens."
```

ABOVE entry with target only:
```bash
algoai --skip-permissions --prompt "Use the upstox-gtt-variants skill to prepare a GTT for [instrument_key], quantity [quantity], entry trigger type ABOVE at [entry_price], and target [target_price]. Do not add a stop-loss leg. Show the exact configuration and wait for my explicit confirmation before placing it. Do not read .env."
```

BELOW entry with stop-loss only:
```bash
algoai --skip-permissions --prompt "Use the upstox-gtt-variants skill to prepare a GTT for [instrument_key], quantity [quantity], entry trigger type BELOW at [entry_price], and stop-loss [sl_price]. Do not add a target leg. Validate, show the preview, and wait for my explicit confirmation before placing it. Never read .env or print credentials."
```

## 6. Modify an existing GTT

### `upstox-gtt-modify`

**Use it for:** Inspecting an existing GTT and changing exactly one trigger price while retaining the other rules and settings.

**Scripts:**
- `.agents/skills/upstox-gtt-modify/scripts/get_gtt.py`
- `.agents/skills/upstox-gtt-modify/scripts/modify_gtt.py`
- `.agents/skills/upstox-gtt-modify/scripts/validate_gtt_modify.py`

**Supported legs:**
- `ENTRY`
- `STOPLOSS`
- `TARGET`

**Safety workflow:**
1. Run the modification script without `--confirm` to fetch the latest GTT and show a preview.
2. Show the GTT ID, selected leg, old trigger, requested trigger, quantity and rule-preservation details.
3. Wait for the user's explicit approval of that exact preview.
4. Rerun with `--confirm` using the same GTT ID, leg and requested trigger.
5. Fetch again and verify the updated trigger.

Only one leg is changed per request. Other rules, quantity and GTT type are retained. Do not infer a missing GTT ID, leg or trigger price.

**Sample prompts:**

Preview only:
```bash
algoai --skip-permissions --prompt "Use the upstox-gtt-modify skill to preview changing GTT [gtt_order_id], leg STOPLOSS, to trigger price [new_price]. Run without --confirm. Show the current value and proposed value, and confirm that all other rules and settings will remain unchanged. Do not modify anything yet. Do not read .env."
```

After reviewing the preview, explicitly approve:
```bash
algoai --skip-permissions --prompt "I explicitly confirm the exact GTT modification preview just shown: GTT [gtt_order_id], leg STOPLOSS, new trigger price [new_price]. Use the upstox-gtt-modify skill and execute with --confirm. Change only this leg, then fetch the GTT again and report whether the new trigger price was verified. Do not read .env or expose credentials."
```

Replace `STOPLOSS` with `ENTRY` or `TARGET` only when that is the intended leg.

## 7. Fetch normal orders and GTT orders

### `upstox-orders-fetch`

**Use it for:** Read-only retrieval of the normal order book, GTT order list, or a specific normal/GTT order. It also supports filters.

**Scripts:**
- `.agents/skills/upstox-orders-fetch/scripts/fetch_orders.py`
- `.agents/skills/upstox-orders-fetch/scripts/order_filters.py`

**Supported options:**
- `--kind all|normal|gtt` (default: `all`)
- `--order-id <id>` (in `all` mode, checks both normal and GTT order APIs)
- `--status <status>`
- `--symbol <partial-symbol-or-instrument-key>`
- `--tag <tag>` (normal orders)
- `--segment <prefix>` (for example, `NSE_FO`)

**Important data scope:**
- Normal `get_order_book` is today's order book, not an unlimited historical archive.
- GTT listing returns the GTT orders exposed by Upstox.
- A specific normal order lookup may return multiple lifecycle events; preserve the returned history.
- Zero matches for a filter is a valid empty result and does not prove that an order never existed.

**Sample prompts:**

All current normal and GTT orders:
```bash
algoai --skip-permissions --prompt "Use the upstox-orders-fetch skill with --kind all to fetch the current normal order book and available GTT orders. Return the complete available response in valid JSON only. Preserve all original API fields and GTT rules. Do not read .env, expose tokens, place orders, modify orders, or cancel orders."
```

Normal orders only:
```bash
algoai --skip-permissions --prompt "Use the upstox-orders-fetch skill with --kind normal to fetch today's normal order book. Return all available records as valid JSON. Do not read .env or expose credentials. Keep this operation read-only."
```

GTT orders only:
```bash
algoai --skip-permissions --prompt "Use the upstox-orders-fetch skill with --kind gtt to fetch all GTT orders currently exposed by Upstox. Preserve every available rule and field and return valid JSON only. Do not read .env or modify/cancel anything."
```

Specific order ID:
```bash
algoai --skip-permissions --prompt "Use the upstox-orders-fetch skill to look up order ID [order_id]. Check both normal and GTT order APIs as supported by the skill. Preserve lifecycle events and return the complete available response as JSON. Do not read .env or make any changes."
```

Filtered orders:
```bash
algoai --skip-permissions --prompt "Use the upstox-orders-fetch skill with --kind all --symbol NIFTY --segment NSE_FO to fetch matching normal and GTT orders. Return valid JSON only and do not omit available fields. Do not read .env. This is read-only."
```

## 8. Today's P&L and positions

### `upstox-pnl`

**Use it for:** Fetching today's Upstox positions and P&L using the dedicated script.

**Script:** `.agents/skills/upstox-pnl/scripts/fetch_today_pnl.py`

The skill instructs the agent to execute the script once and present its output cleanly. It is a read-only reporting workflow.

**Sample prompts:**

```bash
algoai --skip-permissions --prompt "Use the upstox-pnl skill to fetch today's P&L and positions. Return the complete script output in valid JSON format only. Do not read .env, expose access tokens, or make any trading changes."
```

```bash
algoai --skip-permissions --prompt "Use the upstox-pnl skill to fetch today's positions and P&L. Include all fields returned by the script, including available realised/unrealised P&L and position details. Do not calculate or invent missing values. Return JSON only. Do not read .env."
```

## 9. Core Upstox reference documents

The general `upstox` skill contains reference documents under `.agents/skills/upstox/references/`. These are topic guides loaded by the general skill when relevant; they are not separate standalone skill directories.

| Reference file | Topic |
|---|---|
| `orders.md` | Normal order placement, modification, cancellation, multi-order and exit-position workflows |
| `gtt-orders.md` | GTT order APIs and single-/multi-rule GTT workflows |
| `portfolio.md` | Holdings, positions, conversions and portfolio/P&L information |
| `market-data.md` | LTP, OHLC, quotes, historical candles and market status |
| `option-chain.md` | Option-chain data and related analysis such as Greeks, OI, PCR and max pain |
| `margins.md` | Funds, margin requirements and brokerage/charge checks |
| `kill-switch.md` | Segment-level trading kill switch and its operational implications |
| `websocket.md` | WebSocket connections for live market and order/portfolio updates |
| `instruments.md` | Instrument lookup, instrument keys and lot-size information |
| `errors.md` | HTTP/API errors, Upstox error codes and rate-limit troubleshooting |

## 10. Shared execution and security rules

Apply these rules across the Upstox workflows:

1. **Never read `.env`** when a skill explicitly prohibits it. Use the configured environment-variable mechanism.
2. **Never print or expose secrets**, access tokens, authorization headers or credential values.
3. **Keep read-only tasks read-only.** Fetching orders, positions, P&L, quotes or holdings must not trigger a trade.
4. **Require explicit confirmation for live trading changes.** A prompt that asks to prepare, preview or validate an order is not authorization to submit it.
5. **Do not invent trade inputs.** Ask for missing instrument, side, quantity, product, price, trigger or order ID as applicable.
6. **Resolve instrument keys.** Do not guess option/futures tokens or lot sizes.
7. **Preserve source data.** For JSON requests, retain all fields returned by the API/script; do not invent unavailable values.
8. **Respect API scope.** In particular, the normal order book is a current-day view, not a full historical order archive.
9. **Do not make extra API calls** when a focused skill says to run its script once, unless the user explicitly requests additional retrieval or verification. The GTT modification workflow has its own required re-fetch/verification sequence.
10. **Treat `--skip-permissions` as a CLI execution flag, not as user approval** for a live financial action. Explicit confirmation is still required by the trading safety rules.

## 11. Choosing the right skill

| User's request | Use |
|---|---|
| "Show my holdings, funds, quote, candles, option chain or instrument details" | `upstox` |
| "Place a normal limit/market order" | `upstox-order` (and follow main skill safety guardrails) |
| "Place a GTT with entry, SL and target triggers" | `upstox-gtt` |
| "Place a GTT with immediate/above/below entry or optional legs" | `upstox-gtt-variants` |
| "Change the stop-loss/entry/target trigger on an existing GTT" | `upstox-gtt-modify` |
| "List all normal and GTT orders or fetch a particular order" | `upstox-orders-fetch` |
| "Show today's P&L and positions" | `upstox-pnl` |

---

**Maintenance:** Update this catalog whenever a skill directory, its `SKILL.md`, supported script options, or reference documents are added, removed or changed. Treat each skill's own `SKILL.md` as the authoritative operational instruction; this catalog is the navigation and prompt-example guide.
