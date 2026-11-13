"""Import only the daily dummy-order snapshot from an extracted backup folder.

This script reads exactly:
  - level_isolated_instrument.json
  - dummy_orders.json

It upserts one document into UPSTOX_APP_V2.dummy_orders. It does not
upload/archive any files, use GridFS, or write to any other collection.
The full project backup remains on MEGA.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

LOGGER = logging.getLogger("daily_dummy_order_import")
DATABASE_DEFAULT = "UPSTOX_APP_V2"
COLLECTION_NAME = "dummy_orders"
ISOLATION_FILENAME = "level_isolated_instrument.json"
ORDERS_FILENAME = "dummy_orders.json"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_environment() -> None:
    if load_dotenv is None:
        LOGGER.warning(
            "python-dotenv is not installed; using OS environment only. "
            "Install with: pip install pymongo python-dotenv"
        )
        return

    cwd_env = Path.cwd() / ".env"
    script_env = Path(__file__).resolve().parent.parent / ".env"
    if cwd_env.is_file():
        load_dotenv(cwd_env, override=False)
        LOGGER.info("Loaded environment: %s", cwd_env)
    if script_env.is_file() and script_env != cwd_env:
        load_dotenv(script_env, override=False)
        LOGGER.info("Loaded environment: %s", script_env)


def infer_backup_date(folder: Path) -> date:
    patterns = (
        r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, folder.name)
        if match:
            try:
                return date(*(int(part) for part in match.groups()))
            except ValueError as exc:
                raise ValueError(
                    f"Invalid date in source folder name: {folder.name}"
                ) from exc
    raise ValueError(
        f"Cannot infer date from '{folder.name}'. Use --date YYYY-MM-DD."
    )


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required file not found: {path}")
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected a JSON object in {path}; got {type(payload).__name__}"
        )
    return payload


def resolve_mongo_settings() -> tuple[str, str]:
    mongo_uri = (
        os.getenv("MONGO_URL")
        or os.getenv("MONGO_URI")
        or os.getenv("MONGODB_URI")
        or ""
    ).strip()
    database_name = (
        os.getenv("LIVE_ORDER_SNAPSHOT_MONGO_DB")
        or os.getenv("MONGO_DB")
        or DATABASE_DEFAULT
    ).strip()

    if not mongo_uri:
        raise RuntimeError("Set MONGO_URL or MONGO_URI in .env/environment.")
    if not database_name:
        raise RuntimeError("MongoDB database name cannot be empty.")
    return mongo_uri, database_name


def upsert_daily_snapshot(
    database: Any,
    snapshot_date: date,
    isolated_data: dict[str, Any],
    orders_data: dict[str, Any],
) -> None:
    # The collection name is intentionally fixed. This importer writes
    # to dummy_orders only; it never creates a backup/archive collection.
    collection = database[COLLECTION_NAME]
    now = datetime.now(timezone.utc)
    document_id = snapshot_date.isoformat()

    result = collection.update_one(
        {"_id": document_id},
        {
            "$set": {
                "isolated_instrument": isolated_data,
                "orders_data": orders_data,
                "last_updated_time": now,
            },
            "$setOnInsert": {"created_datetime": now},
        },
        upsert=True,
    )

    action = "created" if result.upserted_id is not None else "updated"
    LOGGER.info(
        "%s %s.%s document _id=%s",
        action.capitalize(),
        database.name,
        COLLECTION_NAME,
        document_id,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import only level_isolated_instrument.json and dummy_orders.json "
            "into UPSTOX_APP_V2.dummy_orders."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Extracted backup folder, e.g. data_2026-11-13.",
    )
    parser.add_argument(
        "--date",
        help="Override folder date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate both JSON files without connecting to MongoDB.",
    )
    return parser.parse_args()


def main() -> int:
    configure_logging()
    args = parse_args()
    load_environment()

    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        LOGGER.error("Source folder does not exist: %s", source)
        return 2

    client: MongoClient | None = None
    try:
        snapshot_date = (
            date.fromisoformat(args.date) if args.date else infer_backup_date(source)
        )
        isolated_data = read_json_object(source / ISOLATION_FILENAME)
        orders_data = read_json_object(source / ORDERS_FILENAME)

        LOGGER.info("Source folder: %s", source)
        LOGGER.info("Snapshot date: %s", snapshot_date.isoformat())
        LOGGER.info("Isolation JSON keys: %d", len(isolated_data))
        LOGGER.info("Dummy-order JSON keys: %d", len(orders_data))
        LOGGER.info(
            "MongoDB write target: %s.%s (the only collection written)",
            os.getenv("LIVE_ORDER_SNAPSHOT_MONGO_DB") or DATABASE_DEFAULT,
            COLLECTION_NAME,
        )

        if args.dry_run:
            LOGGER.info("DRY RUN: validation passed; no MongoDB writes performed.")
            return 0

        mongo_uri, database_name = resolve_mongo_settings()
        client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
        )
        client.admin.command("ping")
        LOGGER.info("MongoDB connection successful.")

        upsert_daily_snapshot(
            client[database_name],
            snapshot_date,
            isolated_data,
            orders_data,
        )
        LOGGER.info("Daily dummy-order snapshot import completed.")
        return 0

    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 2
    except PyMongoError:
        LOGGER.exception("MongoDB operation failed.")
        return 3
    except Exception:
        LOGGER.exception("Unexpected import failure.")
        return 4
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
