# AlgoCLI test API

## Endpoint

`POST /api/algocli-test/place-gtt`

The endpoint generates the dedicated `prompts/algocli/test_place_gtt.txt`
prompt and stores one isolated test run under `data/tests/`.

## Server configuration

```dotenv
ALGOCLI_TEST_API_ENABLED=true
ALGOCLI_TEST_API_KEY=replace-with-a-long-random-secret
ALGOCLI_TEST_LIVE_WRITES_ENABLED=false
ALGOCLI_TEST_DATA_DIR=data/tests
```

Keep `ALGOCLI_TEST_LIVE_WRITES_ENABLED=false` for prompt-generation tests.
The API key is sent as the `X-API-Key` header.

## Dry-run request

```bash
curl -X POST "http://127.0.0.1:8001/api/algocli-test/place-gtt" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ALGOCLI_TEST_API_KEY" \
  -d '{
    "instrument_key": "NSE_FO|64473",
    "trading_symbol": "NIFTY 25500 CE 23 NOV 26",
    "entry": null,
    "target": 170,
    "stoploss": 84
  }'
```

Without `confirm_live_order=true`, the service only renders the prompt and
writes a `DRY_RUN` artifact. It does not invoke AlgoCLI.

## Explicit live execution

A real GTT placement is a broker write. It requires BOTH:
1. Request field `confirm_live_order=true`.
2. Server setting `ALGOCLI_TEST_LIVE_WRITES_ENABLED=true`.

Only enable the second setting after confirming the intended broker account,
instrument, quantity and order parameters. An ambiguous CLI write is saved as
`AMBIGUOUS`; do not automatically retry it. Reconcile broker GTT state first.

The current prompt supports immediate market entry only. A non-null `entry`
is rejected. The `api_token` field may be sent for payload compatibility but
is intentionally ignored and never written to artifacts; AlgoCLI uses its own
authentication.

## Saved artifacts

Each request creates a unique directory such as:

```text
data/tests/20261125_092000_123456_test-order-001/
    request.json
    prompt.txt
    response.json
    metadata.json
```

Credential-like fields are removed before persistence. The response includes
the artifact directory and individual file names.