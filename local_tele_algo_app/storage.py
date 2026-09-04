import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TELEGRAM_MESSAGES_FILE = DATA_DIR / "telegram_messages.json"
ALGO_EVENTS_FILE = DATA_DIR / "algo_events.json"
MAX_RECORDS_PER_FILE = max(1, int(os.getenv("LOCAL_ALERT_MAX_RECORDS", "5000")))
_storage_lock = RLock()

def get_utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_storage() -> None:
    with _storage_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for file_path in (TELEGRAM_MESSAGES_FILE, ALGO_EVENTS_FILE):
            if not file_path.exists():
                _write_json_file(file_path, [])

def _read_json_file(file_path: Path) -> list:
    if not file_path.exists():
        return []
    try:
        raw_content = file_path.read_text(encoding="utf-8").strip()
        if not raw_content:
            return []
        data = json.loads(raw_content)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []

def _write_json_file(file_path: Path, records: list[dict]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = file_path.with_name(f"{file_path.name}.tmp")
    normalized_records = [deepcopy(item) for item in records if isinstance(item, dict)]
    serialized_content = json.dumps(normalized_records, ensure_ascii=False, indent=2, default=str)
    temporary_file.write_text(serialized_content, encoding="utf-8")
    os.replace(temporary_file, file_path)

def _append_record(file_path: Path, record: dict) -> dict:
    if not isinstance(record, dict):
        raise TypeError("Storage record must be a dictionary.")
    saved_record = deepcopy(record)
    with _storage_lock:
        records = _read_json_file(file_path)
        records.append(saved_record)
        if len(records) > MAX_RECORDS_PER_FILE:
            records = records[-MAX_RECORDS_PER_FILE:]
        _write_json_file(file_path, records)
    return deepcopy(saved_record)

def _get_records(file_path: Path, limit: int = 100, newest_first: bool = True) -> list:
    normalized_limit = max(1, min(int(limit), MAX_RECORDS_PER_FILE))
    with _storage_lock:
        records = _read_json_file(file_path)
    selected_records = records[-normalized_limit:]
    if newest_first:
        selected_records.reverse()
    return deepcopy(selected_records)

def _get_record_count(file_path: Path) -> int:
    with _storage_lock:
        return len(_read_json_file(file_path))

def save_telegram_message(record: dict) -> dict:
    return _append_record(TELEGRAM_MESSAGES_FILE, record)

def save_algo_event(record: dict) -> dict:
    return _append_record(ALGO_EVENTS_FILE, record)

def get_telegram_messages(limit: int = 100, newest_first: bool = True) -> list:
    return _get_records(TELEGRAM_MESSAGES_FILE, limit=limit, newest_first=newest_first)

def get_algo_events(limit: int = 100, newest_first: bool = True) -> list:
    return _get_records(ALGO_EVENTS_FILE, limit=limit, newest_first=newest_first)

def get_telegram_message_count() -> int:
    return _get_record_count(TELEGRAM_MESSAGES_FILE)

def get_algo_event_count() -> int:
    return _get_record_count(ALGO_EVENTS_FILE)

def get_telegram_message(message_id: str) -> dict | None:
    normalized_message_id = str(message_id or "").strip()
    if not normalized_message_id:
        return None
    with _storage_lock:
        records = _read_json_file(TELEGRAM_MESSAGES_FILE)
    for record in reversed(records):
        if str(record.get("message_id") or "").strip() == normalized_message_id:
            return deepcopy(record)
    return None

def get_algo_event(event_id: str) -> dict | None:
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        return None
    with _storage_lock:
        records = _read_json_file(ALGO_EVENTS_FILE)
    for record in reversed(records):
        if str(record.get("event_id") or "").strip() == normalized_event_id:
            return deepcopy(record)
    return None

def delete_telegram_message(message_id: str) -> bool:
    normalized_message_id = str(message_id or "").strip()
    if not normalized_message_id:
        return False
    with _storage_lock:
        records = _read_json_file(TELEGRAM_MESSAGES_FILE)
        remaining_records = [record for record in records if str(record.get("message_id") or "").strip() != normalized_message_id]
        deleted = len(remaining_records) != len(records)
        if deleted:
            _write_json_file(TELEGRAM_MESSAGES_FILE, remaining_records)
    return deleted

def delete_algo_event(event_id: str) -> bool:
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        return False
    with _storage_lock:
        records = _read_json_file(ALGO_EVENTS_FILE)
        remaining_records = [record for record in records if str(record.get("event_id") or "").strip() != normalized_event_id]
        deleted = len(remaining_records) != len(records)
        if deleted:
            _write_json_file(ALGO_EVENTS_FILE, remaining_records)
    return deleted

def clear_telegram_messages() -> None:
    with _storage_lock:
        _write_json_file(TELEGRAM_MESSAGES_FILE, [])

def clear_algo_events() -> None:
    with _storage_lock:
        _write_json_file(ALGO_EVENTS_FILE, [])

def clear_all_records() -> None:
    with _storage_lock:
        _write_json_file(TELEGRAM_MESSAGES_FILE, [])
        _write_json_file(ALGO_EVENTS_FILE, [])

def get_storage_status() -> dict:
    ensure_storage()
    return {
        "status": "ready",
        "data_directory": str(DATA_DIR),
        "telegram_messages_file": str(TELEGRAM_MESSAGES_FILE),
        "algo_events_file": str(ALGO_EVENTS_FILE),
        "telegram_messages_file_exists": TELEGRAM_MESSAGES_FILE.exists(),
        "algo_events_file_exists": ALGO_EVENTS_FILE.exists(),
        "telegram_message_count": get_telegram_message_count(),
        "algo_event_count": get_algo_event_count(),
        "maximum_records_per_file": MAX_RECORDS_PER_FILE,
        "checked_at": get_utc_timestamp(),
    }

def make_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    return str(value)

ensure_storage()

__all__ = [
    "BASE_DIR",
    "DATA_DIR",
    "TELEGRAM_MESSAGES_FILE",
    "ALGO_EVENTS_FILE",
    "MAX_RECORDS_PER_FILE",
    "get_utc_timestamp",
    "ensure_storage",
    "save_telegram_message",
    "save_algo_event",
    "get_telegram_messages",
    "get_algo_events",
    "get_telegram_message_count",
    "get_algo_event_count",
    "get_telegram_message",
    "get_algo_event",
    "delete_telegram_message",
    "delete_algo_event",
    "clear_telegram_messages",
    "clear_algo_events",
    "clear_all_records",
    "get_storage_status",
    "make_json_safe",
]