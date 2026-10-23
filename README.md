# MEGA Archive Service — Job 1

A modular FastAPI service that reads one configured record from `UPSTOX_APP_V2.app_tokens`, downloads its public MEGA ZIP link anonymously, validates it, and safely replaces the managed extracted dataset.

**Scope is Job 1 only.** No candle/EMA processing or future jobs are implemented.

## Requirements

- Python 3.11+
- A reachable MongoDB instance and a valid `MONGO_URL`
- A public MEGA **file** link in the selected document

## Setup

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`. Never commit it. Use a unique MongoDB selector. If `MONGO_DOCUMENT_ID` is non-empty, it takes precedence over `MONGO_QUERY_JSON`. `MONGO_DOCUMENT_ID_TYPE` supports `string`, `objectid`, and `int`. Without an ID, the JSON query must match exactly one document; zero or multiple matches fail safely.

Expected source fields:
- `file_link`: public MEGA file URL
- `archive_name`: optional safe archive filename; falls back to `MEGA_ARCHIVE_FILENAME`
- `archive_size_bytes`: optional exact size check

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
# or
python run.py
```

- `GET /health` — dependency health and current job state
- `GET /api/v1/jobs/mega-archive/status` — current status
- `POST /api/v1/jobs/mega-archive/hard-refresh` — force a fresh download and replace the managed dataset
- `/docs` — OpenAPI UI

When `API_KEY` is non-empty, send `X-API-Key: <value>` to the hard-refresh endpoint. If no key is configured, the endpoint is unauthenticated; do not expose it publicly. Configure a strong key for network deployments.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs/mega-archive/hard-refresh \
  -H 'X-API-Key: replace-with-your-key'
```

## Scheduling and startup

When enabled, the scheduler runs Monday–Friday at 09:00 in `Asia/Kolkata`. Startup refresh is controlled by `REFRESH_ON_STARTUP`. The startup refresh runs in a background thread so the API can become responsive while the refresh proceeds. State is available from the status endpoint.

**Deploy with one Uvicorn worker.** The scheduler and single-flight lock are process-local; multiple workers would each create a scheduler. For multi-worker deployments, move scheduling to a dedicated process or implement a distributed lock.

## Storage and safe replacement

- Downloads: `MEGA_DOWNLOAD_DIR` (default `storage/downloads`)
- Extracted managed tree: `MEGA_DESTINATION_DIR` (default `storage/mega`)

The archive is downloaded to a temporary directory and only promoted after it is non-empty, within size limits, and matches the configured expected size if supplied. ZIP CRC checks run before extraction. Extraction rejects traversal/absolute paths, duplicate entries, symbolic links, excessive file counts and excessive extracted bytes. A staging directory is built first; the previous managed tree is moved to a backup and restored if the new tree cannot be promoted. Extracted content is never executed.

## MEGA SDK compatibility note

The MEGA integration is isolated in `app/services/mega_download_service.py`. It targets the requested `from mega_sdk import Mega`, anonymous `Mega().login()`, and public-link download methods (`download_url` or `download_public_url`). Package names and versions in the MEGA ecosystem have inconsistent APIs; validate the installed package and real public-link behavior in your deployment environment. If the installed `mega-sdk` distribution exposes a different API, update only `_sdk_download`. The normal test suite does not make external MEGA requests. No MEGA account credentials are used.

The legacy `mega.co.nz/#!...!...` URL form is accepted by the URL validator, but actual parsing/decryption remains dependent on the installed SDK. The app intentionally fails clearly rather than silently falling back to another package.

## Configuration

See `.env.example`. Relative paths resolve from the project root. Important limits:
- `MAX_ARCHIVE_SIZE_MB`
- `MAX_EXTRACTED_SIZE_MB`
- `MAX_ARCHIVE_FILES`
- `MONGO_TIMEOUT_MS`
- `REFRESH_RETRIES`
- `RETRY_BACKOFF_SECONDS`

`archive_size_bytes`, when present, is treated as an exact expected download size.

## Tests

```bash
pytest -q
```

Tests use temporary ZIPs and mocks; no real MongoDB or MEGA credentials are needed.

## Operations and recovery

Inspect logs and `/api/v1/jobs/mega-archive/status` after a failure. The current extracted dataset is preserved for failures before replacement. The scheduler does not apply market-holiday logic. The in-memory status and lock reset on process restart. For durable job history or distributed coordination, add a persistent store/lock in a future iteration.
