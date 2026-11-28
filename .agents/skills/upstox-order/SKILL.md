---
name: upstox-order
description: Preview and explicitly confirm a lot-aware Upstox LIMIT order, AMO by default, using live instrument metadata and UPSTOX_ACCESS_TOKEN.
category: Orders
access: write-capable
execution: direct-python
script: scripts/place_order.py
result_schema: schemas/result.schema.json
---

# Upstox Normal / After-Market Order Placement

## Default behavior

The handler is preview-only by default. Its defaults are:

| Parameter | Default |
|---|---|
| AMO | Enabled (is_amo: true); use --no-amo to disable |
| Side | BUY |
| Product | D (delivery) |
| Order type | LIMIT |
| Price | Live LTP rounded to the instrument's actual tick_size |
| Quantity | One instrument lot (for EQ, normally one unit) |
| Validity | DAY |

The exact instrument key is required and is never guessed. Before previewing, the script retrieves the live quote to resolve the trading symbol, looks up exact instrument metadata, reads the instrument's actual lot size and tick size, validates the quantity, and rounds the limit price to the tick size. F&O quantity is expressed in units and must be a multiple of the live lot size.

A live LTP used as an AMO limit price is only a reference price. It can become stale before the next market session; the limit order will only execute if market conditions meet the limit price.

Upstox documents that AMO handling is inferred from the market session and that the is_amo flag may be ignored during active market hours. It also documents that AMO requests during market hours can be rejected. Confirm the market session before approving an order. API order placement is available only during Upstox's documented API access window (currently 05:30–00:00 IST).

## Mandatory two-step safety workflow

1. Resolve/verify the exact instrument key and inspect the metadata returned in the preview.
2. Run the handler without --confirm. Review the complete order, resolved quantity, reference LTP, rounded price, product, side, AMO flag, and preview_hash.
3. Obtain explicit user approval of that exact instrument, side, quantity, product, order type, price, AMO setting, and hash.
4. Execute the same order using the resolved numeric quantity and numeric price shown in the preview, plus --confirm --expected-preview-hash <exact_hash>. Do not re-use --price market for confirmation because the LTP can change.
5. Report the broker API response; an accepted order request is not proof of a fill.

Neither --allow-write nor --skip-permissions is trade approval. Do not place an order without explicit approval of the exact preview.

## JSON execution

### Default AMO preview (one lot, BUY, delivery, LIMIT at live LTP)

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","<instrument_key>"]'
```

### Example: two lots, SELL, delivery, limit price 250.00

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","<instrument_key>","--lots","2","--transaction-type","SELL","--price","250.00"]'
```

### Example: disable AMO

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","<instrument_key>","--no-amo"]'
```

### Confirm only after explicit approval

Use the exact resolved values from the preview. For example, if it showed quantity 75, price 252.10, and hash <preview_hash>:

```bash
algoai run upstox-order --allow-write --result-format json \
  --script-args-json '["--instrument","<instrument_key>","--quantity","75","--price","252.10","--confirm","--expected-preview-hash","<preview_hash>"]'
```

The quantity, product, side, order type, validity, AMO flag, and price must reproduce the exact preview hash. If the hash differs, the handler stops without placing an order.

## Supported overrides

- --quantity <units>: explicit quantity; F&O must be a multiple of lot size.
- --lots <n>: number of lots; mutually exclusive with --quantity.
- --transaction-type BUY|SELL
- --product D|I|MTF (default D)
- --order-type LIMIT|MARKET (default LIMIT; LIMIT recommended for AMO)
- --price market|<number> (default market; MARKET order uses 0)
- --validity DAY|IOC
- --no-amo: disable the default AMO request

Metadata auto-resolution currently supports NSE/BSE equity and F&O instrument keys. The handler uses the project-verified Upstox SDK V3 order API and shared client factory. It reads credentials from the process environment; it never reads .env or prints tokens.
