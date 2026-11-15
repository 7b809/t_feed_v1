# Daily MEGA data backup

## Remote layout

The application uploads one date-stamped archive per scheduled day:

```text
MEGA/
└── orderflow/
    ├── 20261115_data.zip
    ├── 20261116_data.zip
    └── 20261117_data.zip
```

The ZIP contains the complete `DATA_DIR` tree (default `./data`) and a
`data/BACKUP_MANIFEST.txt`. Temporary files and symlinks are skipped. The
archive is verified before upload and staged outside `DATA_DIR`; the local
staged ZIP is removed after the upload attempt.

## Schedule and configuration

Defaults are 16:05 Asia/Kolkata, Monday-Friday, five minutes after the
default live-feed end time. Configure these values in `.env`:

- `DAILY_DATA_BACKUP_ENABLED`
- `DAILY_DATA_BACKUP_HOUR` / `DAILY_DATA_BACKUP_MINUTE`
- `DAILY_DATA_BACKUP_DAYS`
- `DAILY_DATA_BACKUP_DIR` (temporary staging, outside DATA_DIR)
- `DAILY_DATA_BACKUP_COLLECTION`
- `MEGA_ENABLED`
- `MEGA_EMAIL` / `MEGA_PASSWORD`
- `MEGA_BACKUP_ROOT` (default `orderflow`)

The MEGA SDK is imported only when a backup runs. Install dependencies
with `pip install -r requirements.txt`.

## Tracking and notifications

Each run upserts one MongoDB document into
`MONGO_DATABASE.DAILY_DATA_BACKUP_COLLECTION`, keyed by the IST date
(`YYYYMMDD`). It records status, remote path, MEGA link, byte size,
SHA-256, included/skipped file counts, reason, timestamp and error details.
Telegram sends started and completed/failed notifications using the
application's existing Telegram configuration. Notification failure does
not interrupt the backup.

## Safe replacement

The service uploads under a unique temporary remote name, verifies the
uploaded node's size, promotes it to the date-based filename, then removes
older same-name nodes. A previous archive is not removed before the new
upload is verified.

## Manual trigger

From an application shell with the service running:

```python
import asyncio
from app.main import app

service = app.state.daily_backup_service
asyncio.run(service.run_daily_backup(reason="manual"))
```

The manual trigger shares the same non-overlapping lock as the scheduled run.
