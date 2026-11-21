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


def test_verified_upload_cleans_contents_but_keeps_data_directory(tmp_path):
    data = tmp_path / "data"
    nested = data / "isolated" / "live_feed"
    nested.mkdir(parents=True)
    (nested / "ticks.jsonl").write_text('{"ltp": 100}\n', encoding="utf-8")
    (data / "or_levels.json").write_text("{}", encoding="utf-8")
    service = make_service(data, tmp_path / "backups")
    service._upload_archive = lambda archive_path, filename: "https://mega.example/verified"

    result = service._run_sync("test")

    assert result["status"] == "uploaded"
    assert result["cleanup_status"] == "complete"
    assert data.is_dir()
    assert list(data.iterdir()) == []
    state_file = tmp_path / "backups" / f".{result['date']}_data_backup_state.json"
    assert state_file.is_file()


def test_failed_upload_never_deletes_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    original = data / "important.json"
    original.write_text('{"keep": true}', encoding="utf-8")
    service = make_service(data, tmp_path / "backups")

    def fail_upload(archive_path, filename):
        raise RuntimeError("MEGA unavailable")

    service._upload_archive = fail_upload
    try:
        service._run_sync("test")
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed MEGA upload must raise")

    assert original.read_text(encoding="utf-8") == '{"keep": true}'


def test_cleanup_retry_rearchives_remaining_data_before_retry(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "ticks.jsonl").write_text('{"ltp": 100}\n', encoding="utf-8")
    service = make_service(data, tmp_path / "backups")
    uploads = []
    service._upload_archive = lambda archive_path, filename: uploads.append(filename) or "verified"
    original_cleanup = service._cleanup_data_dir
    attempts = {"count": 0}

    def fail_once():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("temporary filesystem error")
        return original_cleanup()

    service._cleanup_data_dir = fail_once
    first = service._run_sync("test")
    assert first["status"] == "uploaded_cleanup_failed"
    assert data.exists() and list(data.iterdir())

    second = service._run_sync("retry")
    assert second["status"] == "uploaded"
    assert second["cleanup_status"] == "complete"
    assert uploads == [first["filename"], second["filename"]]
    assert list(data.iterdir()) == []


def test_skipped_entries_block_cleanup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "included.json").write_text("{}", encoding="utf-8")
    (data / "unfinished.tmp").write_text("partial", encoding="utf-8")
    service = make_service(data, tmp_path / "backups")
    service._upload_archive = lambda archive_path, filename: "verified"

    result = service._run_sync("test")

    assert result["status"] == "uploaded_cleanup_failed"
    assert result["cleanup_status"] == "blocked_incomplete_archive"
    assert result["files_skipped"] == 1
    assert (data / "included.json").is_file()
    assert (data / "unfinished.tmp").is_file()
