"""Scheduled wrapper for the daily MEGA backup.

Runs at ``DAILY_BACKUP_HOUR:DAILY_BACKUP_MINUTE`` (default 16:00 IST)
via the shared APScheduler in ``services/scheduler_service.py``.

Sends:
    - Started (INFO)      before zipping
    - Completed (SUCCESS) when every folder uploaded to MEGA
    - Finished (WARNING)  on partial success / no data / disabled
    - Failed (ERROR)      on unexpected exceptions
"""

from __future__ import annotations

import logging
from typing import Any

from services.daily_backup_service import daily_backup_service
from services.telegram_format import build
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

def _completion_message(summary: dict[str, Any]) -> str:
    status = str(summary.get("status") or "unknown")
    date_str = summary.get("date") or "—"
    root = summary.get("root_folder") or "—"
    files = summary.get("files") or []

    if status == "uploaded":
        bullets: list[str] = []
        for f in files:
            if not f.get("uploaded"):
                continue
            bullets.append(
                f"✅ {f.get('remote_path')}  ·  {f.get('size_human')}"
            )
            if f.get("mega_link"):
                bullets.append(f"   🔗 {f.get('mega_link')}")
        return build(
            emoji="📦",
            title="Daily Backup · Uploaded",
            fields=[
                ("Date", date_str),
                ("Root", root),
                ("Total size", summary.get("total_size_human")),
                ("Uploaded", summary.get("uploaded_count")),
            ],
            sections=[("MEGA", "☁️", bullets or ["—"])],
            status="success",
            status_detail=(
                f"{summary.get('uploaded_count')} file(s) uploaded"
            ),
        )

    if status == "partial":
        bullets: list[str] = []
        for f in files:
            if f.get("uploaded"):
                bullets.append(
                    f"✅ {f.get('folder')}: "
                    f"{f.get('size_human')} (uploaded)"
                )
            else:
                bullets.append(
                    f"❌ {f.get('folder')}: {f.get('error')}"
                )
        return build(
            emoji="⚠️",
            title="Daily Backup · Partial",
            fields=[
                ("Date", date_str),
                ("Root", root),
                ("Uploaded", summary.get("uploaded_count")),
                ("Failed", summary.get("failed_count")),
            ],
            sections=[("Per folder", "📁", bullets)],
            status="warning",
            status_detail="some folders failed — see per-folder status",
        )

    if status == "failed":
        return build(
            emoji="❌",
            title="Daily Backup · Failed",
            fields=[
                ("Date", date_str),
                ("Root", root),
                ("Error", summary.get("error") or "unknown error"),
            ],
            status="failed",
        )

    if status == "no_data":
        return build(
            emoji="📭",
            title="Daily Backup · No Data",
            fields=[
                ("Date", date_str),
                ("Note", "No source folders produced a zip."),
            ],
            status="warning",
        )

    if status == "disabled":
        return build(
            emoji="🚫",
            title="Daily Backup · Disabled",
            fields=[("Reason", summary.get("message") or summary.get("reason"))],
            status="info",
        )

    if status == "no_folders":
        return build(
            emoji="📭",
            title="Daily Backup · No Folders",
            fields=[("Reason", summary.get("reason") or "empty list")],
            status="warning",
        )

    if status == "already_running":
        return build(
            emoji="⏳",
            title="Daily Backup · Already Running",
            fields=[("Note", "Another backup run was in progress.")],
            status="info",
        )

    return build(
        emoji="❌",
        title="Daily Backup · Failed",
        fields=[("Error", summary.get("error") or "unknown")],
        status="failed",
    )

def run_daily_backup() -> None:
    logger.info("Daily backup job starting")

    telegram_service.send_message(
        build(
            emoji="📦",
            title="Daily Backup · Started",
            sections=[(
                "Doing",
                "🔍",
                [
                    "Zip configured folders",
                    "Upload to MEGA cloud",
                    "Record progress state in MongoDB",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )

    try:
        summary = daily_backup_service.run(reason="scheduled")
        status = str(summary.get("status") or "unknown")

        if status == "uploaded":
            level = "SUCCESS"
        elif status in {"partial", "no_data"}:
            level = "WARNING"
        elif status == "already_running":
            level = "INFO"
        else:
            level = "ERROR"

        telegram_service.send_message(
            _completion_message(summary), level=level
        )

        logger.info(
            "Daily backup completed: status=%s uploaded=%s size=%s date=%s",
            status,
            summary.get("uploaded_count"),
            summary.get("total_size_human"),
            summary.get("date"),
        )
    except Exception as exc:
        logger.exception("Daily backup failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Daily Backup · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )