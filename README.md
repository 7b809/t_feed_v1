# Upstox Order Request Receiver

A FastAPI service that receives isolated EMA alert payloads and stores them in MongoDB.

## Storage

- **Database:** `UPSTOX_ALGO_APP`
- **Collection:** `order_reqs`
- Both values can be overridden through `.env`.
- Each document includes the validated payload plus `received_at` and `schema_version`.

## Project structure

```text
upstox_order_receiver/
├── api/routes/                 # HTTP endpoints
├── core/config.py              # .env-based configuration
├── core/database.py            # MongoDB lifecycle and indexes
├── core/logger.py              # logs/{filename}.log logger factory
├── models/alert.py             # Pydantic payload models
├── services/                   # Persistence logic
├── tests/                      # Validation tests
├── logs/                       # Runtime log files
├── main.py                     # FastAPI application
├── run.py                      # Development runner
├── sample_payload.json
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

## Run locally

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

On Windows, use:

```powershell
Copy-Item .env.example .env
```

Update `MONGODB_URI` if MongoDB is not running locally.

### 4. Start the API

```bash
python run.py
```

API URLs:

- Swagger UI: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>
- POST endpoint: <http://localhost:8000/api/v1/order-requests>

## Run using Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

When using the bundled MongoDB container, change this value in `.env`:

```dotenv
MONGODB_URI=mongodb://mongodb:27017
```

## Send the included sample payload

```bash
curl -X POST "http://localhost:8000/api/v1/order-requests" \
  -H "Content-Type: application/json" \
  --data-binary "@sample_payload.json"
```

Expected response:

```json
{
  "status": "success",
  "message": "Order request saved",
  "id": "MongoDB ObjectId as a string"
}
```

Invalid payloads receive FastAPI's standard HTTP `422` validation response. A database failure returns HTTP `503`.

## Logger usage

```python
from core.logger import get_logger

logger = get_logger("my_worker")
logger.info("Worker started")
```

This writes to `logs/my_worker.log`. Passing `"my_worker.log"` also works. File names are sanitized to prevent writing outside the `logs` directory. Logs rotate at 10 MB and keep five backups.

## Configuration usage

```python
from core.config import settings

print(settings.mongodb_database)
```

`core/config.py` loads values from `.env` and environment variables. Do not commit your actual `.env` file.

## Tests

```bash
pytest -q
```

The models allow additional JSON fields so future payload additions will not be rejected, while required fields and key business consistency checks remain validated.
