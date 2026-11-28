---
name: upstox-order
description: Place normal Upstox orders immediately by default, preview with --preview; AMO requests are preview-only because Upstox API rejects AMO submissions.
category: Orders
access: write-capable
execution: direct-python
script: scripts/place_order.py
result_schema: schemas/result.schema.json
---

# Upstox Normal / After-Market Order Placement

## Important AMO API limitation

Upstox returned error UDAPI1162 for an AMO submission: "AMO orders are not supported via the API. Please use the Upstox mobile or web platform to place AMO orders."

Accordingly, this handler **never submits AMO orders**. Supplying is_amo=true returns status UNSUPPORTED and submitted=false. It does not retry as a regular order and does not change is_amo to false. Place AMO orders manually in Upstox mobile/web.

## Command behavior

- No AMO argument and no --preview: submit a normal order immediately (is_amo=false).
- is_amo=true: return a local UNSUPPORTED result; no API order request is sent.
- is_amo=true --preview: show the AMO order details without submission, with a clear API limitation note.
- --preview without AMO: preview a normal order without submission.
- is_amo=false: explicitly request a normal order.
- --is-amo true|false is also supported.

## Defaults

| Parameter | Default |
|---|---|
| AMO | Disabled; normal order unless is_amo=true is supplied |
| Side | BUY |
| Product | D (delivery) |
| Order type | LIMIT |
| Price | Live LTP rounded to the instrument's actual tick size |
| Quantity | One instrument lot (for EQ, normally one unit) |
| Validity | DAY |

The exact instrument key is required and is never guessed. The script retrieves the live quote to resolve the trading symbol, looks up exact instrument metadata, reads the instrument's lot size and tick size, validates quantity, and rounds price to the instrument tick.

For NSE/BSE instrument metadata, the source tick_size is expressed in paise (for example 5.0 means ₹0.05); the handler converts it to rupees before rounding. F&O quantity is in units and must be a multiple of the live lot size.

## Examples

### 1. Normal order — submit immediately

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838"]'
```

### 2. AMO request — safely rejected locally, no submission

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","is_amo=true"]'
```

This returns UNSUPPORTED / UDAPI1162 and does not send an order to Upstox. Place AMO manually in the Upstox app or website.

### 3. AMO preview — informational only

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","is_amo=true","--preview"]'
```

### 4. Normal order preview — no submission

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","--preview"]'
```

### 5. Override quantity, side, price or product

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","--lots","2","--transaction-type","SELL","--price","250.00","--product","I"]'
```

## Supported arguments

- --instrument: required exact Upstox instrument key.
- --quantity: explicit units; F&O quantity must be a multiple of lot size.
- --lots: number of lots; mutually exclusive with --quantity.
- --transaction-type BUY|SELL (default BUY).
- --product D|I|MTF (default D).
- --order-type LIMIT|MARKET (default LIMIT).
- --price market|<number> (default market; MARKET uses price 0).
- --validity DAY|IOC (default DAY).
- is_amo=true or is_amo=false: positional AMO selector (default false).
- --is-amo true|false: alternative AMO selector.
- --preview: preview only; never submits the order.

Metadata auto-resolution supports NSE/BSE equity and F&O instrument keys. The handler uses the project-verified Upstox SDK V3 order API and shared client factory. It reads credentials from the process environment; it never reads .env or prints tokens.

## Execution safety

A command without --preview submits a live normal order, unless AMO was requested, in which case the handler refuses to submit. Before running a normal-order command, ensure the user explicitly requested the live action and instrument, side, quantity, product, order type and price are clear. Never infer permission to trade from --allow-write alone. An accepted API response is not proof of an execution/fill.
