# File: services/readiness_service.py
"""Aggregate readiness engine for the pre-open window.

Produces ONE aggregate GREEN / YELLOW / RED status from ~10
independent subsystem checks, each of which independently reports
``ok`` / ``warn`` / ``fail`` with a short human-readable detail.

The output is consumed by:

    * ``GET /admin/readiness``                (monitoring script / dashboard)
    * ``token_tasks/readiness_digest_task.py`` (08:45 Telegram digest)
    * ``token_tasks/readiness_digest_task.py`` (09:05 pre-flight recheck)
    * ``GET /admin/ops/runbook``              (operator cheat sheet)

Design notes
------------
* The service is a pure reporter — it never mutates application state.
  The only optional side effect is the smoke-test probe, which is a
  read-only HTTP GET against the local FastAPI app.
* Every check is wrapped in its own try/except so a single
  misbehaving subsystem cannot blank the entire readiness report.
* The clock-drift check is NTP-based (``ntplib``) with a 2-second
  timeout and a 60-second result cache, so it never blocks the report
  for more than ~2 s even when the NTP server is unreachable.
* The ``smoke_test`` probes are separated from the passive checks so
  the same routine can be reused by ``POST /admin/ops/smoke-test``
  without triggering the full readiness pass.
* No credentials, tokens, or account identifiers are ever read or
  emitted by this module.

Strategy-layer removal note
---------------------------
The EMA cross engine (``services/live_ema_engine.py``) has been removed
from the codebase. The ``nifty_snapshot`` smoke-test probe therefore
checks ``ltp > 0`` on the returned snapshot payload — the ``ema`` block
that used to carry ``candle_count`` is no longer populated by any live
producer.

Status aggregation
------------------
    Any ``fail``  -> RED
    Any ``warn``  -> YELLOW
    Otherwise     -> GREEN
"""

from __future__ import annotations

import shutil
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #
_NTP_HOST = "pool.ntp.org"
_NTP_TIMEOUT = 2.0
_NTP_CACHE_SECONDS = 60.0
_SMOKE_HTTP_TIMEOUT = 5.0

# --------------------------------------------------------------------------- #
# Rolling 7-day health-log directory (JSON per day)                           #
# --------------------------------------------------------------------------- #
_HEALTH_DIR = config.PROJECT_ROOT / "meta_data" / "health"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class ReadinessService:
    """Aggregate readiness checks for the pre-open window."""

    def __init__(self) -> None:
        self._last_snapshot: dict[str, Any] | None = None
        self._ntp_cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ #
    # Public entry points                                                #
    # ------------------------------------------------------------------ #
    def check(self) -> dict[str, Any]:
        """Run every check and return the aggregate readiness payload.

        The payload is JSON-serialisable and safe to log or return
        directly from ``GET /admin/readiness``.
        """
        checks = {
            "token_valid":            self._check_token(),
            "token_age_minutes":      self._check_token_age(),
            "registry_count":         self._check_registry(),
            "data_dir":               self._check_data_dir(),
            "mongo_reachable":        self._check_mongo(),
            "live_feed_running":      self._check_live_feed(),
            "scheduler_jobs_count":   self._check_scheduler(),
            "clock_drift_seconds":    self._check_clock_drift(),
            "disk_free_gb":           self._check_disk(),
            "order_toggle":           self._check_order_runtime(),
        }

        fails = [k for k, v in checks.items() if v["status"] == "fail"]
        warns = [k for k, v in checks.items() if v["status"] == "warn"]

        if fails:
            status = "red"
        elif warns:
            status = "yellow"
        else:
            status = "green"

        snapshot = {
            "status":     status,
            "checks":     checks,
            "checked_at": _iso_now(),
            "day":        _today_iso(),
            "failed":     fails,
            "warned":     warns,
        }

        self._last_snapshot = snapshot
        self._append_health_log(snapshot)
        logger.info(
            "[readiness] aggregate=%s ok=%d warn=%d fail=%d",
            status.upper(),
            len(checks) - len(fails) - len(warns),
            len(warns),
            len(fails),
        )
        return snapshot

    def last_snapshot(self) -> dict[str, Any] | None:
        """Return the most recent check result (or None before the first call)."""
        return self._last_snapshot

    def smoke_test(self) -> dict[str, Any]:
        """Actively probe key endpoints for a known-good response.

        Runs in ~2 s when everything is healthy. Reused by the 08:45
        digest and by ``POST /admin/ops/smoke-test`` (no side effects).

        Probe 1 (Nifty snapshot) checks ``ltp > 0`` — the ``ema`` block
        is no longer populated after the strategy layer was removed.
        """
        base = f"http://127.0.0.1:{config.PORT}"
        probes: dict[str, Any] = {}

        # 1) Nifty snapshot — assert ltp > 0
        try:
            r = requests.get(
                f"{base}/live/snapshot/{quote('NSE_INDEX|Nifty 50')}",
                timeout=_SMOKE_HTTP_TIMEOUT,
            )
            d = r.json()
            ltp = d.get("ltp")
            try:
                ltp_ok = float(ltp) > 0
            except (TypeError, ValueError):
                ltp_ok = False
            probes["nifty_snapshot"] = (
                "ok" if r.status_code == 200 and ltp_ok
                else f"empty (status={r.status_code}, ltp={ltp})"
            )
        except Exception as exc:
            probes["nifty_snapshot"] = f"fail: {type(exc).__name__}"

        # 2) Registry — assert count > 0
        try:
            r = requests.get(
                f"{base}/live/instruments",
                timeout=_SMOKE_HTTP_TIMEOUT,
            )
            d = r.json()
            count = int(d.get("count", 0))
            probes["registry_api"] = (
                "ok" if r.status_code == 200 and count > 0
                else f"empty (status={r.status_code}, count={count})"
            )
        except Exception as exc:
            probes["registry_api"] = f"fail: {type(exc).__name__}"

        # 3) Token endpoint — assert available=true
        try:
            r = requests.get(
                f"{base}/api/upstox/token",
                timeout=_SMOKE_HTTP_TIMEOUT,
            )
            d = r.json()
            probes["token_api"] = (
                "ok" if r.status_code == 200 and d.get("available")
                else f"unavailable (status={r.status_code})"
            )
        except Exception as exc:
            probes["token_api"] = f"fail: {type(exc).__name__}"

        # 4) Backup status — surface last extraction result
        try:
            from services.backup_data_service import backup_data_service
            st = backup_data_service.last_status()
            probes["backup"] = str(st.get("status") or "unknown")
        except Exception as exc:
            probes["backup"] = f"fail: {type(exc).__name__}"

        # 5) Scheduler job count
        try:
            from services.scheduler_service import scheduler_service
            sched = scheduler_service.scheduler
            probes["scheduler"] = (
                f"{len(sched.get_jobs())} jobs"
                if sched else "not_started"
            )
        except Exception as exc:
            probes["scheduler"] = f"fail: {type(exc).__name__}"

        return probes

    # ------------------------------------------------------------------ #
    # Individual checks                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ok(detail: str, **extra: Any) -> dict[str, Any]:
        return {"status": "ok", "detail": detail, **extra}

    @staticmethod
    def _warn(detail: str, **extra: Any) -> dict[str, Any]:
        return {"status": "warn", "detail": detail, **extra}

    @staticmethod
    def _fail(detail: str, **extra: Any) -> dict[str, Any]:
        return {"status": "fail", "detail": detail, **extra}

    # ---- token ------------------------------------------------------- #
    def _check_token(self) -> dict[str, Any]:
        try:
            from services.token_service import token_service
            token = token_service.get_access_token()
            if not token:
                return self._fail("no access token in MongoDB")
            return self._ok(f"present ({len(token)} chars)")
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    def _check_token_age(self) -> dict[str, Any]:
        try:
            from token_tasks.token_store import token_store
            doc = token_store.get_token_document() or {}
            last_validated = doc.get("last_validated_at")
            if not last_validated:
                return self._warn("never validated")
            try:
                dt = datetime.fromisoformat(
                    str(last_validated).replace("Z", "+00:00")
                )
            except ValueError:
                return self._warn(f"unparseable timestamp {last_validated!r}")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            max_age = float(config.READINESS_TOKEN_MAX_AGE_MINUTES)
            if age_min > max_age:
                return self._fail(
                    f"last validated {age_min:.1f} min ago "
                    f"(max {max_age:.0f} min)"
                )
            if age_min > max_age * 0.75:
                return self._warn(f"last validated {age_min:.1f} min ago")
            return self._ok(f"age {age_min:.1f} min")
        except Exception as exc:
            return self._warn(f"{type(exc).__name__}: {exc}")

    # ---- registry / data --------------------------------------------- #
    def _check_registry(self) -> dict[str, Any]:
        try:
            from services.instrument_registry import instrument_registry
            count = len(instrument_registry.all())
            min_count = int(config.READINESS_REGISTRY_MIN_COUNT)
            if count == 0:
                return self._fail("registry is empty")
            if count < min_count:
                return self._warn(
                    f"{count} instruments (expected >= {min_count})"
                )
            return self._ok(f"{count} instruments")
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    def _check_data_dir(self) -> dict[str, Any]:
        try:
            path = config.LIVE_DATA_DIR
            if not path.exists():
                return self._fail(f"missing: {path}")
            if not any(path.iterdir()):
                return self._fail(f"empty: {path}")
            return self._ok(str(path))
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    # ---- mongo ------------------------------------------------------- #
    def _check_mongo(self) -> dict[str, Any]:
        try:
            from pymongo import MongoClient
            client = MongoClient(
                config.MONGO_URI,
                serverSelectionTimeoutMS=2000,
            )
            t0 = time.monotonic()
            client.admin.command("ping")
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return self._ok(f"{elapsed_ms:.1f} ms")
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    # ---- live feed --------------------------------------------------- #
    def _check_live_feed(self) -> dict[str, Any]:
        try:
            from services.live_feed_service import live_feed_service
            status = live_feed_service.status()
            if not status.get("running"):
                return self._fail("WS thread is not alive")
            count = int(status.get("instrument_count", 0))
            if count == 0:
                return self._warn("running with 0 instruments")
            return self._ok(f"running, {count} instruments")
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    # ---- scheduler --------------------------------------------------- #
    def _check_scheduler(self) -> dict[str, Any]:
        try:
            from services.scheduler_service import scheduler_service
            sched = scheduler_service.scheduler
            if sched is None:
                return self._fail("scheduler not started")
            jobs = sched.get_jobs()
            if len(jobs) == 0:
                return self._fail("scheduler has 0 jobs")
            return self._ok(f"{len(jobs)} jobs registered")
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}")

    # ---- clock drift ------------------------------------------------- #
    def _check_clock_drift(self) -> dict[str, Any]:
        # cache the result for 60 s so the digest + preflight don't both
        # pay the NTP round-trip.
        if self._ntp_cache is not None:
            age = time.monotonic() - self._ntp_cache[0]
            if age < _NTP_CACHE_SECONDS:
                return dict(self._ntp_cache[1])

        result = self._fetch_ntp_drift()
        self._ntp_cache = (time.monotonic(), dict(result))
        return result

    def _fetch_ntp_drift(self) -> dict[str, Any]:
        try:
            import ntplib  # type: ignore
        except ImportError:
            return self._warn("ntplib not installed; drift unchecked")

        try:
            client = ntplib.NTPClient()
            response = client.request(_NTP_HOST, version=3, timeout=_NTP_TIMEOUT)
            drift = abs(float(response.tx_time) - time.time())
            max_drift = float(config.READINESS_CLOCK_DRIFT_MAX_SECONDS)
            if drift > max_drift:
                return self._fail(
                    f"{drift:.1f}s drift (> {max_drift:.0f}s) — "
                    f"run `sudo ntpdate -s {_NTP_HOST}`"
                )
            if drift > max_drift / 3.0:
                return self._warn(f"{drift:.1f}s drift")
            return self._ok(f"{drift:.2f}s drift")
        except Exception as exc:
            return self._warn(f"ntp_unreachable: {type(exc).__name__}")

    # ---- disk -------------------------------------------------------- #
    def _check_disk(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(config.PROJECT_ROOT)
            free_gb = usage.free / (1024 ** 3)
            min_gb = float(config.READINESS_DISK_FREE_MIN_GB)
            if free_gb < min_gb:
                return self._fail(f"{free_gb:.1f} GB free (< {min_gb:.0f} GB)")
            if free_gb < min_gb * 3:
                return self._warn(f"{free_gb:.1f} GB free")
            return self._ok(f"{free_gb:.1f} GB free")
        except Exception as exc:
            return self._warn(f"{type(exc).__name__}: {exc}")

    # ---- order runtime toggle ---------------------------------------- #
    def _check_order_runtime(self) -> dict[str, Any]:
        try:
            from services.order_runtime_service import order_runtime_service
            st = order_runtime_service.status()
            enabled = bool(st.get("enabled"))
            # Either state is acceptable — the check exists so the digest
            # surfaces it in one line.
            return self._ok(
                f"enabled={enabled}"
                + (f" (changed_by={st.get('changed_by')})"
                   if st.get("changed_by") else "")
            )
        except Exception as exc:
            return self._warn(f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 7-day health log                                                   #
    # ------------------------------------------------------------------ #
    def _append_health_log(self, snapshot: dict[str, Any]) -> None:
        """Append a compact record to ``meta_data/health/readiness_<day>.json``.

        Best-effort — any failure here must not affect the caller.
        """
        try:
            _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
            path = _HEALTH_DIR / f"readiness_{snapshot['day']}.json"
            existing: list[dict[str, Any]] = []
            if path.exists():
                try:
                    import json
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({
                "at":         snapshot["checked_at"],
                "status":     snapshot["status"],
                "failed":     snapshot["failed"],
                "warned":     snapshot["warned"],
                "checks":     {
                    k: v["status"]
                    for k, v in snapshot["checks"].items()
                },
            })
            # 7-day retention
            existing = existing[-200:]
            import json
            path.write_text(
                json.dumps(existing, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[readiness] health log write failed")


readiness_service = ReadinessService()


# --------------------------------------------------------------------------- #
# Convenience: remediation hint per failing check                              #
# --------------------------------------------------------------------------- #
_REMEDIATION = {
    "token_valid":          "send /save_token in Telegram",
    "token_age_minutes":    "run /token_check in Telegram",
    "registry_count":       "POST /admin/hard-refresh",
    "data_dir":             "POST /admin/hard-refresh",
    "mongo_reachable":      "check Atlas status page",
    "live_feed_running":    "POST /admin/ops/resubscribe-feed",
    "scheduler_jobs_count": "restart the application",
    "clock_drift_seconds":  f"sudo ntpdate -s {_NTP_HOST}",
    "disk_free_gb":         "clear data/ or logs/ old files",
    "order_toggle":         "POST /admin/orders/runtime-enable",
}


def remediation_for(check_name: str) -> str:
    """Return a one-line operator hint for a failing/warning check."""
    return _REMEDIATION.get(check_name, "POST /admin/hard-refresh")