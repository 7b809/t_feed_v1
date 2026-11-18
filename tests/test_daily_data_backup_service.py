from __future__ import annotations

import zipfile

from services.daily_data_backup_service import DailyDataBackupService


def make_service(data_dir, staging_dir):
    return DailyDataBackupService(
        data_dir=str(data_dir),
        staging_dir=str(staging_dir),
        app_name="isolation_app",
        mega_enabled=True,
        mega_email="",
        mega_password="",
        mega_backup_root="isolation_app",
        telegram_sender=lambda message: False,
        state_collection=None,
    )


def test_archive_contains_data_root_and_manifest(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "isolated").mkdir()
    (data / "isolated" / "isolated_instrument.json").write_text(
        '{"instrument_key": "NSE_FO|123"}', encoding="utf-8"
    )
    staging = tmp_path / "backups"
    service = make_service(data, staging)

    archive, stats = service._create_archive("20261118")
    try:
        assert stats["files_added"] == 1
        with zipfile.ZipFile(archive) as zipped:
            assert "data/isolated/isolated_instrument.json" in zipped.namelist()
            assert "data/BACKUP_MANIFEST.txt" in zipped.namelist()
            assert zipped.testzip() is None
    finally:
        archive.unlink(missing_ok=True)


def test_staging_directory_inside_data_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    service = make_service(data, data / "backups")

    try:
        service._create_archive("20261118")
    except ValueError as exc:
        assert "outside DATA_DIR" in str(exc)
    else:
        raise AssertionError("backup staging inside DATA_DIR must be rejected")


def test_empty_data_directory_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    service = make_service(data, tmp_path / "backups")

    try:
        service._create_archive("20261118")
    except ValueError as exc:
        assert "no readable files" in str(exc)
    else:
        raise AssertionError("empty backup must be rejected")
