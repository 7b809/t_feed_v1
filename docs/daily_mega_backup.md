# Isolation daily MEGA backup

The isolation service can archive its local `DATA_DIR` and upload one
date-keyed ZIP to the configured MEGA folder.

## Remote layout

```text
MEGA account/
└── isolation_app/
    ├── YYYYMMDD_data.zip
    └── ...
```

The default archive is created after the market session at **16:05 IST,
Monday–Friday**. Configure the schedule with
`DAILY_DATA_BACKUP_HOUR`, `DAILY_DATA_BACKUP_MINUTE`, and
`DAILY_DATA_BACKUP_DAYS`.

## Environment

Set these values in the deployment `.env` file:

- `DATA_DIR=./data`: source folder to archive.
- `DAILY_DATA_BACKUP_ENABLED=true`: enable the scheduled backup.
- `DAILY_DATA_BACKUP_DIR=./backups`: local staging directory. It must
  remain outside `DATA_DIR` so the ZIP is not included in itself.
- `DAILY_DATA_BACKUP_HOUR=16`, `DAILY_DATA_BACKUP_MINUTE=5`,
  `DAILY_DATA_BACKUP_DAYS=mon-fri`: schedule in `TIMEZONE` (default IST).
- `DAILY_DATA_BACKUP_COLLECTION=daily_data_backup_state`: MongoDB
  collection for date-keyed upload status.
- `MEGA_ENABLED=true`, `MEGA_EMAIL`, `MEGA_PASSWORD`: MEGA account.
- `MEGA_BACKUP_ROOT=isolation_app`: folder created at the MEGA account root.

Install the dependency after deployment:

```bash
pip install -r requirements.txt
```

## Backup flow

1. Scheduler invokes the backup job at the configured time.
2. The service walks `DATA_DIR`, skips symlinks, temporary files and
   unreadable files, and writes a ZIP into the staging directory.
3. A manifest is added and the ZIP is verified by reading its members.
4. The service logs into MEGA, ensures the `isolation_app` folder exists,
   uploads the date-keyed ZIP, and verifies remote filename and size.
5. A repeat upload for the same date replaces older same-name archive nodes.
6. The service writes status, checksum, file counts, size, remote path and
   optional share link to MongoDB, and sends best-effort Telegram updates.
7. The staged local ZIP is deleted after the upload attempt. Source data is
   never deleted or modified.

A backup failure is logged and recorded when MongoDB is available. It does
not stop the isolation scanner or other scheduled jobs. The service does
not perform a live MEGA upload during ordinary unit tests.
