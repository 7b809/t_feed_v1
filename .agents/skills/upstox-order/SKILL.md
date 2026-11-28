---
name: upstox-order
description: Place an Upstox normal order immediately by default, or preview it with --preview; AMO is opt-in with is_amo=true.
category: Orders
access: write-capable
execution: direct-python
script: scripts/place_order.py
result_schema: schemas/result.schema.json
---

# Upstox Normal / After-Market Order Placement

## Command behavior

The handler places the order immediately by default. There is no mandatory preview/confirm/hash workflow.

- No AMO argument and no --preview: submit a normal order immediately (is_amo=false).
- is_amo=true and no --preview: submit an AMO order immediately (is_amo=true).
- --preview: build and return the exact order payload without submitting it. It works with either normal or AMO orders.
- is_amo=false: explicitly request a normal order.
- --is-amo true|false is also supported.

**Live order submission is the default.** Use --preview whenever you want to inspect the order before submission. --allow-write is required by the runner, but it does not replace the user's explicit instruction to submit a live order.

## Defaults

| Parameter | Default |
|---|---|
| AMO | Disabled; normal order unless is_amo=true is supplied |
| Side | BUY |
| Product | D (delivery) |
| Order type | LIMIT |
| Price | Live LTP rounded to the instrument's actual tick_size |
| Quantity | One instrument lot (for EQ, normally one unit) |
| Validity | DAY |

The exact instrument key is required and is never guessed. The script retrieves the live quote to resolve the trading symbol, looks up exact instrument metadata, reads the instrument's lot size and tick size, validates the quantity, and rounds the limit price to the tick size. F&O quantity is in units and must be a multiple of the live lot size.

A live LTP used as a LIMIT price is only a reference price. It may change before an AMO is processed or before a regular order executes. A LIMIT order only executes if market conditions meet its limit price.

Upstox determines AMO handling from the market session. AMO requests may be rejected outside supported windows or if the exchange/segment/product combination is not eligible. Check the current Upstox/exchange rules before submitting.

## Examples

### 1. Normal order — submit immediately (default)

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838"]'
```

This submits a normal BUY LIMIT order for one lot at the current LTP, rounded to the instrument tick size, using product D.

### 2. AMO — submit immediately

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","is_amo=true"]'
```

### 3. AMO preview — do not submit

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","is_amo=true","--preview"]'
```

### 4. Normal order preview — do not submit

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","--preview"]'
```

### 5. Override quantity, side, price or product

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","--lots","2","--transaction-type","SELL","--price","250.00","--product","I"]'
```

### 6. Explicit AMO option form

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","NSE_FO|47838","--is-amo","true"]'
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

A command without --preview is a live submission. Before running it, ensure the user has explicitly asked for that exact live action and the instrument, side, quantity, product, order type, price, and AMO setting are clear. Never infer permission to trade from --allow-write alone. An accepted API response is not proof of an execution/fill.
