from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import zipfile

from app.services.daily_data_backup_service import DailyDataBackupService


class FakeTelegram:
    def __init__(self, document_results=None):
        self.messages = []
        self.documents = []
        self.document_results = list(document_results or [])

    async def send_message(self, title, message, level="INFO"):
        self.messages.append((title, message, level))
        return True

    async def send_document(self, path, caption=""):
        self.documents.append((Path(path).name, caption, Path(path).stat().st_size))
        return self.document_results.pop(0) if self.document_results else True


def config(data_dir, backup_dir, enabled=True):
    return SimpleNamespace(
        daily_data_backup_enabled=enabled,
        daily_data_backup_dir=str(backup_dir),
        data_dir=str(data_dir),
        app_name="orderflow_v1",
    )


def test_backup_creates_zip_and_sends_document(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        (data / "sample.json").write_text('{"ok": true}', encoding="utf-8")
        telegram = FakeTelegram()
        service = DailyDataBackupService(config(data, tmp_path / "backups"), telegram)
        result = await service.run_daily_backup()
        assert result["status"] == "sent"
        assert result["files_added"] == 1
        assert len(telegram.documents) == 1
        archive_path = Path(result["archive"])
        assert archive_path.exists()
        with zipfile.ZipFile(archive_path) as archive:
            assert "data/sample.json" in archive.namelist()
    asyncio.run(run())


def test_large_archive_is_split_into_multiple_telegram_parts(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        (data / "large.bin").write_bytes(b"x" * 100)
        telegram = FakeTelegram()
        service = DailyDataBackupService(
            config(data, tmp_path / "backups"), telegram, part_size_bytes=32
        )
        result = await service.run_daily_backup()
        assert result["parts_total"] >= 2
        assert result["parts_sent"] == result["parts_total"]
        assert all(size <= 32 for _, _, size in telegram.documents)
        assert all(".part" in name for name, _, _ in telegram.documents)
    asyncio.run(run())


def test_disabled_backup_does_nothing(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        telegram = FakeTelegram()
        service = DailyDataBackupService(config(data, tmp_path / "backups", False), telegram)
        result = await service.run_daily_backup()
        assert result["status"] == "disabled"
        assert telegram.documents == []
    asyncio.run(run())


def test_backup_directory_inside_data_is_rejected_without_raising(tmp_path):
    async def run():
        data = tmp_path / "data"
        data.mkdir()
        telegram = FakeTelegram()
        service = DailyDataBackupService(config(data, data / "backups"), telegram)
        result = await service.run_daily_backup()
        assert result["status"] == "failed"
    asyncio.run(run())
