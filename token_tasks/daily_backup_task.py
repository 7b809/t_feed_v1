"""Scheduled wrapper for the daily MongoDB data backup.

Runs at ``DAILY_BACKUP_HOUR:DAILY_BACKUP_MINUTE`` (default 16:00 IST)
via the shared APScheduler in ``services/scheduler_service.py``.

Sends:
    - Started (INFO) before zipping
    - Completed (SUCCESS) when the archive was saved inline
    - Finished (WARNING) when the archive exceeded the size limit and
      only metadata was saved
    - Failed (ERROR) on an unexpected exception
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

    if status == "saved":
        fields = [
            ("Status", "saved"),
            ("Archive", summary.get("archive_name")),
            ("Size", summary.get("archive_size_human")),
            ("Files", summary.get("file_count")),
            ("Doc ID", summary.get("doc_id")),
            ("DB", summary.get("database")),
            ("Collection", summary.get("collection")),
        ]
        section_title = "Result"
        section_emoji = "✅"
        bullets = [
            f"Archive {summary.get('archive_size_human')} stored inline.",
            f"Under the {summary.get('limit_human')} limit.",
        ]
        return build(
            emoji="📦",
            title="Daily Backup · Completed",
            fields=fields,
            sections=[(section_title, section_emoji, bullets)],
            status="success",
            status_detail=f"files={summary.get('file_count')}",
        )

    if status == "skipped_size_limit":
        fields = [
            ("Status", "skipped (size)"),
            ("Archive", summary.get("archive_name")),
            ("Size", summary.get("archive_size_human")),
            ("Limit", summary.get("limit_human")),
            ("Files", summary.get("file_count")),
            ("Doc ID", summary.get("doc_id")),
        ]
        bullets = [
            f"Archive exceeds the {summary.get('limit_human')} inline limit.",
            "Only metadata was saved to MongoDB.",
            "Raise DAILY_BACKUP_MAX_BLOB_MB, trim DAILY_BACKUP_FOLDERS, "
            "or archive the folder manually before the next refresh.",
        ]
        return build(
            emoji="⚠️",
            title="Daily Backup · Finished",
            fields=fields,
            sections=[("Reason", "⚠️", bullets)],
            status="warning",
            status_detail="metadata only — size limit hit",
        )

    if status == "disabled":
        return build(
            emoji="🚫",
            title="Daily Backup · Disabled",
            fields=[("Reason", summary.get("message") or "config")],
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
        fields=[
            ("Status", status),
            ("Error", summary.get("error") or summary.get("reason")),
        ],
        status="failed",
    )


def run_daily_backup() -> None:
    logger.info("Daily backup job starting")

    telegram_service.send_message(
        build(
            emoji="📦",
            title="Daily Backup · Started",
            sections=[
                (
                    "Doing",
                    "🔍",
                    [
                        "Zip configured folders",
                        "Measure final archive size",
                        "Save inline if within limit, else metadata only",
                    ],
                )
            ],
            status="started",
        ),
        level="INFO",
    )

    try:
        summary = daily_backup_service.run(reason="scheduled")
        status = str(summary.get("status") or "unknown")

        if status == "saved":
            level = "SUCCESS"
        elif status in {"skipped_size_limit", "no_folders"}:
            level = "WARNING"
        elif status == "already_running":
            level = "INFO"
        else:
            level = "ERROR"

        telegram_service.send_message(
            _completion_message(summary), level=level
        )

        logger.info(
            "Daily backup completed: status=%s size=%s files=%s",
            status,
            summary.get("archive_size_human"),
            summary.get("file_count"),
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