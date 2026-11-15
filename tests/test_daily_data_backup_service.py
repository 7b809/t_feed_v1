# tests/test_daily_data_backup_service.py
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.services.daily_data_backup_service import DailyDataBackupService
from app.services.mongodb_service import MongoDbService
from app.services.telegram_service import TelegramService


# ------------------------------------------------------------
# Fakes (unchanged)
# ------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, title, message, level="INFO"):
        self.messages.append((title, message, level))
        return True


class FakeCollection:
    def __init__(self):
        self.documents = {}

    def replace_one(self, query, document, upsert=False):
        self.documents[query["_id"]] = dict(document)


def make_config(data_dir, staging_dir, enabled=True):
    return SimpleNamespace(
        daily_data_backup_enabled=enabled,
        daily_data_backup_dir=str(staging_dir),
        data_dir=str(data_dir),
        app_name="orderflow",
        mega_enabled=True,
        mega_email="test@example.com",
        mega_password="secret",
        mega_backup_root="orderflow",
    )


# ------------------------------------------------------------
# Fast unit tests (unchanged)
# ------------------------------------------------------------
def test_disabled_backup_does_not_login_or_write_state(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        collection = FakeCollection()
        service = DailyDataBackupService(
            make_config(data, tmp_path / "staging", False),
            FakeTelegram(),
            collection,
            mega_factory=lambda: (_ for _ in ()).throw(
                AssertionError("must not login when disabled")
            ),
        )
        result = await service.run_daily_backup()
        assert result["status"] == "disabled"
        assert collection.documents == {}

    asyncio.run(run())


def test_backup_directory_inside_data_is_rejected(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        service = DailyDataBackupService(
            make_config(data, data / "backups"),
            FakeTelegram(),
            FakeCollection(),
        )
        result = await service.run_daily_backup()
        assert result["status"] == "failed"

    asyncio.run(run())


def test_archive_contains_data_root_and_manifest(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sample.json").write_text('{"ok": true}', encoding="utf-8")
    service = DailyDataBackupService(
        make_config(data, tmp_path / "staging"),
        FakeTelegram(),
        FakeCollection(),
    )
    archive, stats = service._create_archive("20261115")
    try:
        assert stats["files_added"] == 1
        with zipfile.ZipFile(archive) as zipped:
            assert "data/sample.json" in zipped.namelist()
            assert "data/BACKUP_MANIFEST.txt" in zipped.namelist()
    finally:
        archive.unlink(missing_ok=True)


# ------------------------------------------------------------
# Live MEGA test — checks are inside the body, not the decorator
# ------------------------------------------------------------
def test_daily_archive_uploads_to_mega_and_tracks_mongo(tmp_path):
    """data folder → ZIP → real MEGA upload → Mongo state doc.

    Runs only when explicitly enabled:
        * python -m tests.test_daily_data_backup_service   (auto)
        * RUN_MEGA_BACKUP_TEST=1 pytest ...                (CI)
    """
    live = os.getenv("RUN_MEGA_BACKUP_TEST", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # `python -m tests...` sets pytest's rootdir differently; check sys.argv
    if not live and any("test_daily_data_backup_service" in a for a in sys.argv):
        live = True
    if not live:
        pytest.skip(
            "live test skipped — run `python -m tests.test_daily_data_backup_service` "
            "or set RUN_MEGA_BACKUP_TEST=1"
        )

    config = load_settings()
    if not config.daily_data_backup_enabled:
        pytest.skip("DAILY_DATA_BACKUP_ENABLED=false")
    if not config.mega_enabled:
        pytest.skip("MEGA_ENABLED=false")
    if not (config.mega_email and config.mega_password):
        pytest.skip("MEGA_EMAIL / MEGA_PASSWORD missing from .env")

    mongo = MongoDbService(config)
    mongo.connect()
    try:
        async def run():
            data = tmp_path / "data"
            data.mkdir()
            (data / "live_feed.jsonl").write_text('{"ltp": 100}\n', encoding="utf-8")
            (data / "sr_state").mkdir()
            (data / "sr_state" / "today_live_sr.json").write_text(
                '{"current_price": 197.35}\n', encoding="utf-8"
            )

            staging = tmp_path / "backups"
            staging.mkdir()

            test_root = f"{config.mega_backup_root}_test"
            test_collection = f"{config.daily_data_backup_collection}_test"
            cfg = dataclasses.replace(
                config,
                data_dir=str(data),
                daily_data_backup_dir=str(staging),
                mega_backup_root=test_root,
                daily_data_backup_collection=test_collection,
            )

            telegram = TelegramService(cfg)
            collection = mongo.db[test_collection]
            collection.delete_many({})

            service = DailyDataBackupService(cfg, telegram, collection)
            result = await service.run_daily_backup(reason="python-module")

            assert result["status"] == "uploaded", result
            assert result["filename"].endswith("_data.zip")
            assert result["remote_path"] == f"{test_root}/{result['filename']}"
            assert len(result["sha256"]) == 64
            assert result["files_added"] == 2
            assert result["size_bytes"] > 0
            assert not list(staging.glob("*.zip"))

            state = collection.find_one({"_id": result["date"]})
            assert state is not None
            assert state["status"] == "uploaded"
            assert state["remote_path"] == f"{test_root}/{result['filename']}"

            from mega_sdk import Mega
            mega = Mega()
            mega.login(cfg.mega_email, cfg.mega_password)
            files = mega.get_files()

            folder_handles = {
                h for h, n in files.items()
                if n.get("t") == 1 and (n.get("a") or {}).get("n") == test_root
            }
            assert folder_handles, f"MEGA folder '{test_root}' was not created"

            uploaded_handles = [
                h for h, n in files.items()
                if n.get("p") in folder_handles
                and (n.get("a") or {}).get("n") == result["filename"]
            ]
            assert uploaded_handles, f"{result['filename']} not found on MEGA"

            try:
                for handle in uploaded_handles:
                    try:
                        mega.destroy(handle)
                    except Exception:
                        pass
                for handle in folder_handles:
                    try:
                        mega.destroy(handle)
                    except Exception:
                        pass
            finally:
                collection.delete_many({})

        asyncio.run(run())
    finally:
        mongo.close()


# ------------------------------------------------------------
# Direct runner
# ------------------------------------------------------------
if __name__ == "__main__":
    this_file = str(Path(__file__).resolve())
    print(f"\n>>> running live MEGA backup test: {this_file}\n")
    sys.exit(
        pytest.main(
            [this_file, "-v", "-s", "--tb=long", "--no-header",
             "-p", "no:cacheprovider"]
        )
    )