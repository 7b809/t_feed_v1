from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

from app.services.daily_data_backup_service import DailyDataBackupService


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


class FakeMega:
    def __init__(self):
        self.root_id = "root"
        self.nodes = {}
        self.next_id = 0

    def login(self, email, password):
        assert email == "test@example.com"
        assert password == "secret"

    def get_files(self):
        return dict(self.nodes)

    def create_folder_at(self, name, parent):
        self.next_id += 1
        handle = f"folder-{self.next_id}"
        self.nodes[handle] = {"p": parent, "t": 1, "a": {"n": name}}
        return handle

    def upload(self, path, dest, dest_filename):
        self.next_id += 1
        handle = f"file-{self.next_id}"
        self.nodes[handle] = {
            "p": dest, "t": 0, "s": Path(path).stat().st_size,
            "a": {"n": dest_filename},
        }

    def rename(self, handle, name):
        self.nodes[handle]["a"]["n"] = name

    def destroy(self, handle):
        self.nodes.pop(handle, None)

    def get_link(self, node):
        return f"https://mega.nz/file/{node[0]}"


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


def test_daily_archive_uploads_to_orderflow_folder_and_tracks_mongo(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        (data / "live_feed.jsonl").write_text('{"ltp": 100}\n', encoding="utf-8")
        telegram = FakeTelegram()
        collection = FakeCollection()
        mega = FakeMega()
        service = DailyDataBackupService(
            make_config(data, tmp_path / "staging"),
            telegram,
            collection,
            mega_factory=lambda: mega,
        )

        result = await service.run_daily_backup(reason="manual")

        assert result["status"] == "uploaded"
        assert result["filename"].endswith("_data.zip")
        assert result["remote_path"] == f"orderflow/{result['filename']}"
        assert result["sha256"] == hashlib.sha256(b"").hexdigest() or len(result["sha256"]) == 64
        assert result["files_added"] == 1
        assert len(collection.documents) == 1
        assert list(collection.documents.values())[0]["status"] == "uploaded"
        assert not list((tmp_path / "staging").glob("*.zip"))
        assert any(message[0] == "Daily data backup uploaded" for message in telegram.messages)

        remote_names = [node.get("a", {}).get("n") for node in mega.nodes.values()]
        assert "orderflow" in remote_names
        assert result["filename"] in remote_names

    asyncio.run(run())


def test_disabled_backup_does_not_login_or_write_state(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        collection = FakeCollection()
        service = DailyDataBackupService(
            make_config(data, tmp_path / "staging", False),
            FakeTelegram(),
            collection,
            mega_factory=lambda: (_ for _ in ()).throw(AssertionError("must not login")),
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
            mega_factory=FakeMega,
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
        mega_factory=FakeMega,
    )
    archive, stats = service._create_archive("20261115")
    try:
        assert stats["files_added"] == 1
        with zipfile.ZipFile(archive) as zipped:
            assert "data/sample.json" in zipped.namelist()
            assert "data/BACKUP_MANIFEST.txt" in zipped.namelist()
    finally:
        archive.unlink(missing_ok=True)
