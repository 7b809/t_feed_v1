---
name: upstox-gtt
description: Preview and explicitly confirm a multi-leg GTT order on Upstox.
category: Orders
access: write-capable
execution: direct-python
script: scripts/place_gtt.py
result_schema: schemas/result.schema.json
---

# Upstox GTT Order Skill

## Mandatory two-step safety workflow

The handler is preview-only by default. It does not place a GTT unless both `--confirm` and the exact `--expected-preview-hash` from the reviewed preview are supplied.

1. Verify the instrument key, quantity and strategy inputs. Do not guess option/futures tokens or lot sizes.
2. Run the handler without `--confirm`. Review the entire GTT payload and `preview_hash`.
3. Obtain explicit user approval of the exact instrument, transaction side, quantity, product, ENTRY, STOPLOSS, TARGET and hash.
4. Execute with the same arguments plus `--confirm --expected-preview-hash <exact_hash>`.
5. Report the API response. An accepted GTT request is not proof that its trigger has fired or that a trade has filled.

## JSON execution

Preview only:
```bash
algoai run upstox-gtt --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","<qty>","--entry-trigger","<entry>","--sl-trigger","<stoploss>","--target-trigger","<target>","--product","D","--transaction-type","BUY"]'
```

Execute only after approval of that exact preview:
```bash
algoai run upstox-gtt --allow-write --result-format json --script-args-json '["--instrument","<instrument_key>","--quantity","<qty>","--entry-trigger","<entry>","--sl-trigger","<stoploss>","--target-trigger","<target>","--product","D","--transaction-type","BUY","--confirm","--expected-preview-hash","<preview_hash>"]'
```

Never read `.env`, print tokens, or treat `--skip-permissions` / `--allow-write` as trade approval. The fingerprint-bound confirmation is mandatory.
