# AlgoCLI test API

## Endpoint

`POST /api/algocli-test/place-gtt`

The endpoint generates the dedicated `prompts/algocli/test_place_gtt.txt`
prompt and stores one isolated test run under `data/algocli_tests/place_gtt/`.

## Server configuration

```dotenv
ALGOCLI_TEST_API_ENABLED=true
ALGOCLI_TEST_API_KEY=replace-with-a-long-random-secret
ALGOCLI_TEST_LIVE_WRITES_ENABLED=false
ALGOCLI_TEST_DATA_DIR=data/algocli_tests
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
data/algocli_tests/place_gtt/20261125_092000_123456_test-order-001/
    request.json
    prompt.txt
    response.json
    metadata.json
```

Credential-like fields are removed before persistence. The response includes
the artifact directory and individual file names.

## Read-only AlgoAI health check (asynchronous)

### Queue a check

`GET /api/algocli-test/get-positions` requires no request body. Send only
`X-API-Key`. The API creates the job folder and prompt files, queues the
AlgoAI process as a FastAPI background task, and immediately returns HTTP
`202 Accepted`. It does not wait for AlgoAI to finish.

```bash
curl -i "http://127.0.0.1:8001/api/algocli-test/get-positions" \
  -H "X-API-Key: $ALGOCLI_TEST_API_KEY"
```

Example immediate response:

```json
{
  "status": "ACCEPTED",
  "operation_id": "generated-32-character-id",
  "artifact_directory": "data/algocli_tests/get_positions/...",
  "result_path": "data/algocli_tests/get_positions/.../response.json",
  "status_url": "/api/algocli-test/get-positions/generated-32-character-id",
  "files": ["request.json", "prompt.txt", "response.json", "metadata.json"]
}
```

### Poll for the result

Call the returned `status_url` with the same API key. It returns `ACCEPTED`
or `RUNNING` while the CLI is working, then `HEALTHY` with the parsed AlgoAI
response or `UNHEALTHY` with error details. Polling never starts another CLI run.

### Artifact layout

All AlgoAI test artifacts are stored separately under `data/algocli_tests/`:

```text
data/algocli_tests/
    place_gtt/<timestamp>_<operation-id>/{request.json,prompt.txt,response.json,metadata.json}
    get_positions/<timestamp>_<operation-id>/{request.json,prompt.txt,response.json,metadata.json}
```

The initial `response.json` is written as `ACCEPTED` before the HTTP response.
The background worker updates it to `RUNNING`, then writes the final result.
Credential-like values are redacted before persistence.

This check validates the configured AlgoAI executable and its ability to
complete the positions prompt at that moment. It also depends on configured
AlgoAI/Upstox authentication and provider availability; it is not a guarantee
that trading endpoints or order placement are healthy.
