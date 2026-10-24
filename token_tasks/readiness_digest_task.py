# File: token_tasks/readiness_digest_task.py
"""Scheduled readiness digest (08:45) and pre-flight recheck (09:05).

08:45 digest
------------
Short, glanceable Telegram message showing GREEN / YELLOW / RED and
exactly which subsystem is degraded. Replaces the dense 08:30
completion message as the "one glance" heartbeat of the pre-open
window.

09:05 pre-flight recheck
------------------------
Re-run the readiness checks ~10 min before cash open, compare with
the 08:45 snapshot, and if any check regressed from ``ok`` to
``fail`` send an INTERVENTION REQUIRED alert with the specific
remediation hint for each regression.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core import config
from core.logger import get_logger
from services.readiness_service import (
    readiness_service,
    remediation_for,
)
from services.telegram_format import build, divider, THIN
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
_CHECK_EMOJI = {"ok": "✅", "warn": "⚠️", "fail": "❌"}

# Snapshot captured at 08:45 so the 09:05 recheck can compare.
_LAST_DIGEST_SNAPSHOT: dict[str, Any] | None = None


def _minutes_to_open() -> int:
    """Return minutes between now and 09:15 IST."""
    now = datetime.now().astimezone()
    open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    delta = (open_dt - now).total_seconds() / 60.0
    return max(0, int(round(delta)))


def _check_lines(checks: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for name, check in checks.items():
        icon = _CHECK_EMOJI.get(check.get("status"), "•")
        lines.append(f"{icon} {name}: {check.get('detail', '')}")
    return lines


def _build_digest_message(snapshot: dict[str, Any]) -> str:
    status = snapshot.get("status", "red")
    emoji = _STATUS_EMOJI.get(status, "🔴")
    checks = snapshot.get("checks") or {}
    body = "\n".join(_check_lines(checks))

    if status == "green":
        footer = "✅ No action required"
    elif status == "yellow":
        footer = "⚠️ Review required — see /admin/readiness"
    else:
        footer = "🔴 INTERVENTION REQUIRED — see /admin/ops/runbook"

    return (
        f"{divider()}\n"
        f"{emoji} PRE-OPEN READINESS · {status.upper()}\n"
        f"{divider()}\n"
        f"⏰ {datetime.now().astimezone().strftime('%H:%M:%S IST')}\n"
        f"🎯 Market opens in {_minutes_to_open()} minutes\n"
        f"{THIN}\n"
        f"{body}\n"
        f"{THIN}\n"
        f"{footer}"
    )


def run_readiness_digest() -> None:
    """08:45 — short, glanceable Telegram digest."""
    global _LAST_DIGEST_SNAPSHOT

    logger.info("[readiness:digest] starting")
    try:
        snapshot = readiness_service.check()
        # Augment with the smoke-test probes (active verification).
        snapshot["smoke_test"] = readiness_service.smoke_test()
        _LAST_DIGEST_SNAPSHOT = snapshot

        status = snapshot.get("status", "red")
        level = {
            "green": "SUCCESS",
            "yellow": "WARNING",
            "red": "ERROR",
        }.get(status, "INFO")

        telegram_service.send_message(
            f"Pre-Open Readiness · {status.upper()}",
            _build_digest_message(snapshot),
            level=level,
        )
        logger.info("[readiness:digest] sent (status=%s)", status)
    except Exception as exc:
        logger.exception("[readiness:digest] failed")
        telegram_service.send_message(
            "Readiness Digest Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )


def run_preflight_recheck() -> None:
    """09:05 — re-check and alert on any regression vs. the 08:45 digest."""
    global _LAST_DIGEST_SNAPSHOT

    logger.info("[readiness:preflight] starting")
    try:
        current = readiness_service.check()

        previous = _LAST_DIGEST_SNAPSHOT or {"checks": {}}
        prev_checks = previous.get("checks") or {}
        curr_checks = current.get("checks") or {}

        regressions: list[tuple[str, dict[str, Any]]] = []
        for name, curr in curr_checks.items():
            prev = prev_checks.get(name) or {}
            if (
                prev.get("status") == "ok"
                and curr.get("status") == "fail"
            ):
                regressions.append((name, curr))

        if not regressions:
            logger.info(
                "[readiness:preflight] no regressions (status=%s)",
                current.get("status"),
            )
            # Optionally update the baseline so a later digest has the
            # most recent snapshot.
            _LAST_DIGEST_SNAPSHOT = current
            return

        bullets: list[str] = []
        for name, curr in regressions:
            bullets.append(
                f"❌ {name}: {curr.get('detail', '')}"
            )
            hint = remediation_for(name)
            if hint:
                bullets.append(f"   🔧 {hint}")

        msg = (
            f"{divider()}\n"
            f"🔴 PRE-FLIGHT INTERVENTION REQUIRED\n"
            f"{divider()}\n"
            f"⏰ {datetime.now().astimezone().strftime('%H:%M:%S IST')}\n"
            f"🎯 {_minutes_to_open()} min to cash open\n"
            f"{THIN}\n"
            f"Degraded since 08:45:\n"
            + "\n".join(bullets)
            + f"\n{THIN}\n"
            f"📖 /admin/ops/runbook"
        )
        telegram_service.send_message(
            "Pre-Flight Intervention Required",
            msg,
            level="ERROR",
        )
        logger.warning(
            "[readiness:preflight] %d regression(s): %s",
            len(regressions),
            [n for n, _ in regressions],
        )
    except Exception as exc:
        logger.exception("[readiness:preflight] failed")
        telegram_service.send_message(
            "Pre-Flight Recheck Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )