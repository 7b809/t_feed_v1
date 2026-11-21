"""Send a clearly labelled group-only test using the saved isolation JSON.

Run from the project root:
    python scripts/test_isolation_group_notification.py
    python scripts/test_isolation_group_notification.py /path/to/file.json

This script does not create or modify isolation records, touch MongoDB, or
start market feeds. It never targets the legacy TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.isolation_telegram_service import (  # noqa: E402
    send_existing_isolation_test,
)


def main() -> int:
    # Match IsolatedArchiveService: env override, otherwise data/isolated.
    isolated_data_dir = os.getenv("ISOLATED_DATA_DIR", "data/isolated").strip()
    default_dir = Path(isolated_data_dir).expanduser()
    if not default_dir.is_absolute():
        default_dir = PROJECT_ROOT / default_dir
    default_path = default_dir / "isolated_instrument.json"
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else default_path
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    if not path.is_file():
        print(f"ERROR: isolation JSON not found: {path}")
        print("Create/restore today's isolation first, or pass an existing JSON path.")
        return 2

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read valid JSON from {path}: {exc}")
        return 2

    if not isinstance(record, dict):
        print("ERROR: isolation JSON root must be a JSON object.")
        return 2
    if not isinstance(record.get("source_record"), dict) and not isinstance(
        record.get("instruments"), dict
    ):
        print(
            "ERROR: JSON does not look like isolated_instrument.json "
            "(missing source_record/instruments)."
        )
        return 2

    print(f"Using saved isolation data: {path}")
    print("Sending TEST MESSAGE to TELEGRAM_GROUP_CHAT_ID only...")
    if not send_existing_isolation_test(record):
        print(
            "FAILED: Telegram did not confirm delivery. Check "
            "TELEGRAM_GROUP_ENABLED, TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_GROUP_CHAT_ID and bot group permissions."
        )
        return 1

    print("SUCCESS: Telegram accepted the group-only test message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
