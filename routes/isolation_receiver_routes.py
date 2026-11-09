# routes/isolation_receiver_routes.py
"""Receive isolation notifications from the isolation service.

The isolation service POSTs its complete isolation document to this
endpoint after persisting it to Mongo. This module:

  * verifies the shared-secret header (X-Isolation-Key by default)
  * parses the JSON payload
  * returns IMMEDIATELY with "request received, processing in background"
  * saves the payload to
        data/isolation/<YYYY-MM-DD>/<HHMMSS_ffffff>__<kind>__<symbol>.json
    via FastAPI BackgroundTasks (runs *after* the response is sent)
  * updates data/isolation/latest__<kind>.json as a rolling pointer
  * sends a Telegram notification summarising all three stages
    (received → saved → notified)

The whole 3-stage message is a single short-lines layout that renders
identically on Telegram desktop and mobile — no ASCII columns, no
markdown tables, no monospace alignment.

Failure isolation (why this never stalls the app)
-------------------------------------------------
The background task runs every stage inside its own try/except and
ALWAYS attempts the Telegram notification, even when the save step
failed or threw unexpectedly. The Telegram send itself is bounded by
a hard timeout (`ISOLATION_RECEIVER_TELEGRAM_TIMEOUT_SECONDS`,
default 10s) so a hung network call cannot hold a FastAPI background
worker — the worker is freed as soon as the timeout fires and the
HTTP response has already gone out long before.

A final `DONE` summary log line is emitted for every request with the
per-stage outcomes, so `grep req_id=<id>` yields the complete story in
one shot.

Endpoints
---------
POST /api/isolation
    Receive a notification. Auth via header (X-Isolation-Key).

GET  /api/isolation/received
    List recently saved JSON files (newest first).

GET  /api/isolation/received/{date}/{filename}
    Read a single saved JSON file (path-traversal safe).

Auth
----
The shared secret comes from ISOLATION_RECEIVER_KEY in .env.
The sender's key is ISOLATION_NOTIFY_CLIENT_KEY — both sides MUST be set
to the SAME value.

If ISOLATION_RECEIVER_KEY is empty, the receiver runs in NO-AUTH mode
and logs a one-time warning. Do not run this way in production.

PROD_MODE interaction
---------------------
When the main app runs with PROD_MODE=True, AuthGuardMiddleware runs
FIRST on every request. /api/isolation is on its public whitelist (see
core/auth_guard.py) so requests reach this router. Route-level auth is
still enforced here via the shared-secret header.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core.logger import get_logger

load_dotenv()
logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
def _env_str(name: str, default: str) -> str:
    raw = (os.getenv(name, "") or "").strip()
    return raw or default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw == "true"


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[isolation-receiver] invalid %s=%r; using default %s",
            name, raw, default,
        )
        return default


_KEY = _env_str("ISOLATION_RECEIVER_KEY", "")
_KEY_HEADER = _env_str("ISOLATION_RECEIVER_KEY_HEADER", "X-Isolation-Key")
_DATA_DIR = Path(_env_str("ISOLATION_RECEIVER_DATA_DIR", "data/isolation"))
_TZ = _env_str("ISOLATION_RECEIVER_TIMEZONE", "Asia/Kolkata")
_TELEGRAM_ENABLED = _env_bool("ISOLATION_RECEIVER_TELEGRAM_ENABLED", True)
_TELEGRAM_TIMEOUT_SECONDS = _env_float(
    "ISOLATION_RECEIVER_TELEGRAM_TIMEOUT_SECONDS", 10.0
)

_KEY_WARNING_LOGGED = False


def _warn_missing_key_once() -> None:
    global _KEY_WARNING_LOGGED
    if _KEY_WARNING_LOGGED or _KEY:
        return
    _KEY_WARNING_LOGGED = True
    logger.warning(
        "[isolation-receiver] ISOLATION_RECEIVER_KEY is EMPTY — running "
        "in NO-AUTH mode. Any client can post to this endpoint without a "
        "key. Set ISOLATION_RECEIVER_KEY in .env to enforce authentication."
    )


# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _safe_token(value: str, max_len: int = 80) -> str:
    """Return a filesystem-safe token (no path separators, no traversal)."""
    out = (value or "").strip() or "UNKNOWN"
    for ch in (" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|", "."):
        out = out.replace(ch, "_")
    while "__" in out:
        out = out.replace("__", "_")
    out = out.strip("_") or "UNKNOWN"
    return out[:max_len]


def _build_save_path(payload: dict) -> Path:
    """Return the target path for a received payload."""
    now = datetime.now(ZoneInfo(_TZ))
    trade_date = _safe_token(
        str(payload.get("trade_date_ist") or now.date().isoformat()),
        max_len=32,
    )
    symbol = _safe_token(str(payload.get("trading_symbol") or "unknown"), max_len=80)
    kind = "test" if payload.get("is_test") else "live"
    ts = now.strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{ts}__{kind}__{symbol}.json"
    return _DATA_DIR / trade_date / filename


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically — no half-written files ever observed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Timeout wrapper                                                             #
# --------------------------------------------------------------------------- #
def _run_with_timeout(
    fn: Callable[[], Any], *, timeout: float
) -> tuple[bool, Any, str | None]:
    """Run ``fn()`` with a hard wall-clock timeout.

    Returns ``(ok, result, error)``.

    On timeout, the worker thread is abandoned (daemon) — we do NOT wait
    for it to finish. This is deliberate: a hung Telegram HTTP call must
    never hold a FastAPI background-task worker thread, because that
    worker is drawn from a shared pool used by every request's background
    work across the app. Letting it hang there would eventually starve
    request handling for unrelated endpoints.

    The daemon flag guarantees the abandoned thread also cannot prevent
    interpreter shutdown.
    """
    state: dict[str, Any] = {"ok": False, "value": None, "error": None}
    done = threading.Event()

    def _target() -> None:
        try:
            state["value"] = fn()
            state["ok"] = True
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    thread = threading.Thread(
        target=_target, daemon=True, name="iso-recv-stage"
    )
    thread.start()

    if not done.wait(timeout):
        return False, None, f"timed out after {timeout:.1f}s"
    if state["ok"]:
        return True, state["value"], None
    return False, None, state["error"]


# --------------------------------------------------------------------------- #
# Telegram message                                                            #
# --------------------------------------------------------------------------- #
def _build_telegram_message(
    *,
    payload: dict,
    save_path: Path,
    save_ok: bool,
    save_error: str | None,
    pointer_ok: bool | None,
    pointer_error: str | None,
    request_id: str,
    client_host: str,
) -> tuple[str, str, str]:
    """Build the 3-stage Telegram notification.

    Returns ``(title, body, level)`` where ``level`` is one of the strings
    the project's ``telegram_service`` understands (SUCCESS / ERROR / INFO).

    Layout rules (why it renders the same on PC and mobile):
      * short lines (no ASCII columns)
      * emojis as anchors
      * bullets with ``·`` only — no markdown tables
      * no alignment padding — mobile Telegram uses a proportional font
      * stage block first so the phone's ~3-line preview alone tells the
        operator what happened; details below the fold
    """
    is_test = bool(payload.get("is_test"))
    kind_upper = "TEST" if is_test else "LIVE"

    try:
        rel_path = save_path.relative_to(_DATA_DIR).as_posix()
    except Exception:
        rel_path = str(save_path)

    # ---- Stage 1 — always success (we're inside the background task) ----
    stage1 = "✅ Stage 1/3 · Received"

    # ---- Stage 2 — save (+ optional pointer) status ---------------------
    if save_ok:
        if pointer_ok is True:
            stage2 = "✅ Stage 2/3 · Saved to disk (+ latest pointer)"
        elif pointer_ok is False:
            stage2 = (
                "⚠️ Stage 2/3 · Saved to disk "
                f"(pointer write failed: {pointer_error or 'unknown'})"
            )
        else:
            stage2 = "✅ Stage 2/3 · Saved to disk"
    else:
        stage2 = f"❌ Stage 2/3 · Save failed: {save_error or 'unknown'}"

    # ---- Stage 3 — this message itself ----------------------------------
    stage3 = "✅ Stage 3/3 · Telegram notified (this message)"

    level = "SUCCESS" if save_ok else "ERROR"

    lines = [
        f"📥 Isolation received · {kind_upper}",
        "",
        stage1,
        stage2,
        stage3,
        "",
        f"🎯 {payload.get('trading_symbol') or 'unknown'}",
        f"📊 Mode: {payload.get('standard_execution_mode') or '—'}",
        f"📅 Trade date: {payload.get('trade_date_ist') or '—'}",
        f"👤 Client: {client_host or '—'}",
        f"🔑 Request ID: {request_id}",
        "",
        f"📁 {rel_path}",
    ]

    title = f"Isolation received · {kind_upper}"
    return title, "\n".join(lines), level


def _send_telegram_sync(
    *,
    payload: dict,
    save_path: Path,
    save_ok: bool,
    save_error: str | None,
    pointer_ok: bool | None,
    pointer_error: str | None,
    request_id: str,
    client_host: str,
) -> tuple[bool, str | None]:
    """Send the 3-stage notification, bounded by a timeout.

    Returns ``(ok, error)``. **Never raises** — every failure mode is
    captured and returned so the caller can log it.
    """
    if not _TELEGRAM_ENABLED:
        logger.debug(
            "[isolation-receiver] Telegram notify disabled for req_id=%s "
            "(ISOLATION_RECEIVER_TELEGRAM_ENABLED=false)",
            request_id,
        )
        return False, "disabled by config"

    try:
        from services.telegram_service import telegram_service
    except Exception as exc:
        logger.exception(
            "[isolation-receiver] Telegram service import failed req_id=%s",
            request_id,
        )
        return False, f"import failed: {type(exc).__name__}: {exc}"

    try:
        title, body, level = _build_telegram_message(
            payload=payload,
            save_path=save_path,
            save_ok=save_ok,
            save_error=save_error,
            pointer_ok=pointer_ok,
            pointer_error=pointer_error,
            request_id=request_id,
            client_host=client_host,
        )
    except Exception as exc:
        logger.exception(
            "[isolation-receiver] Telegram message build failed req_id=%s",
            request_id,
        )
        return False, f"build failed: {type(exc).__name__}: {exc}"

    ok, _result, error = _run_with_timeout(
        lambda: telegram_service.send_message(title, body, level=level),
        timeout=_TELEGRAM_TIMEOUT_SECONDS,
    )

    if ok:
        logger.info(
            "[isolation-receiver] Telegram notified req_id=%s level=%s",
            request_id, level,
        )
        return True, None

    logger.error(
        "[isolation-receiver] Telegram send FAILED req_id=%s error=%s",
        request_id, error,
    )
    return False, error


# --------------------------------------------------------------------------- #
# Background task — save + notify                                             #
# --------------------------------------------------------------------------- #
def _save_then_notify_sync(
    payload: dict,
    save_path: Path,
    request_id: str,
    client_host: str,
) -> None:
    """Background worker: save, write pointer, notify — each stage isolated.

    Runs *after* the HTTP response has been sent to the caller, so a slow
    disk or slow Telegram API never delays the ack. Every stage is wrapped
    in its own try/except so a failure in one cannot prevent the next from
    running. **Telegram is always attempted**, even when the save step
    failed or threw unexpectedly — the operator must see an outcome for
    every request.

    Nothing in this function raises. Any failure is logged and reflected in
    the final ``DONE`` summary line, which lets a single
    ``grep req_id=<id>`` yield the whole story for a request.
    """
    kind = "test" if payload.get("is_test") else "live"
    save_ok = False
    save_error: str | None = None
    pointer_ok: bool | None = None
    pointer_error: str | None = None
    tg_ok = False
    tg_error: str | None = None

    try:
        # ---- Stage 2a: save payload ------------------------------------
        try:
            _atomic_write_json(save_path, payload)
            save_ok = True
            logger.info(
                "[isolation-receiver] SAVED req_id=%s kind=%s symbol=%s "
                "mode=%s -> %s",
                request_id, kind,
                payload.get("trading_symbol"),
                payload.get("standard_execution_mode"),
                save_path,
            )
        except Exception as exc:
            save_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "[isolation-receiver] FAILED to save req_id=%s -> %s",
                request_id, save_path,
            )

        # ---- Stage 2b: latest pointer (only on save success) -----------
        # Best-effort. A failed pointer does NOT escalate to ERROR level
        # in the Telegram message, because the primary file is already
        # durable — the pointer is only a convenience alias.
        if save_ok:
            latest_path = _DATA_DIR / f"latest__{kind}.json"
            try:
                _atomic_write_json(latest_path, {
                    "_meta": {
                        "source_file": str(save_path),
                        "request_id": request_id,
                        "written_at": datetime.now(ZoneInfo(_TZ)).isoformat(),
                    },
                    "payload": payload,
                })
                pointer_ok = True
            except Exception as exc:
                pointer_ok = False
                pointer_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "[isolation-receiver] latest pointer write failed "
                    "req_id=%s (non-fatal)",
                    request_id,
                )

        # ---- Stage 3: Telegram (ALWAYS attempted) ----------------------
        # Even if save failed we still notify so the operator sees the
        # failure. This is the whole point of running these stages in the
        # same background task.
        try:
            tg_ok, tg_error = _send_telegram_sync(
                payload=payload,
                save_path=save_path,
                save_ok=save_ok,
                save_error=save_error,
                pointer_ok=pointer_ok,
                pointer_error=pointer_error,
                request_id=request_id,
                client_host=client_host,
            )
        except Exception as exc:
            tg_error = f"unexpected: {type(exc).__name__}: {exc}"
            logger.exception(
                "[isolation-receiver] Telegram stage crashed req_id=%s",
                request_id,
            )
    except Exception as exc:
        # ---- Absolute safety net ---------------------------------------
        # If anything above raised in a way we did not expect, still try
        # one last Telegram attempt so the operator sees *something* for
        # this request. Never re-raise — the response already went out,
        # and stalling a background worker here would only hurt the app.
        logger.exception(
            "[isolation-receiver] UNHANDLED error in background task "
            "req_id=%s",
            request_id,
        )
        try:
            _send_telegram_sync(
                payload=payload,
                save_path=save_path,
                save_ok=save_ok,
                save_error=(
                    save_error
                    or f"unhandled: {type(exc).__name__}: {exc}"
                ),
                pointer_ok=pointer_ok,
                pointer_error=pointer_error,
                request_id=request_id,
                client_host=client_host,
            )
        except Exception:
            logger.exception(
                "[isolation-receiver] last-resort notify failed req_id=%s",
                request_id,
            )
    finally:
        # ---- Final summary ---------------------------------------------
        # One grep finds everything about this request:
        #   grep "req_id=<id>" logs/isolated_instrument_routes.log
        logger.info(
            "[isolation-receiver] DONE req_id=%s save_ok=%s "
            "pointer_ok=%s tg_ok=%s tg_error=%s",
            request_id, save_ok, pointer_ok, tg_ok, tg_error,
        )


# --------------------------------------------------------------------------- #
# Router                                                                      #
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/api/isolation", tags=["isolation-receiver"])


@router.post("")
async def receive_isolation(request: Request, background_tasks: BackgroundTasks):
    """Receive an isolation document and return immediately.

    Authentication: header ISOLATION_RECEIVER_KEY_HEADER (default
    X-Isolation-Key) must equal ISOLATION_RECEIVER_KEY. If the key is
    unset, the endpoint runs in no-auth mode (logs a warning once).

    The response is returned BEFORE the payload is written to disk and
    BEFORE the Telegram message is sent. Save and notify happen in a
    single background task that is fully isolated per stage — see
    ``_save_then_notify_sync`` — so nothing that happens after this
    response can stall the app.

    Use the returned ``saved_to`` path (or poll
    ``GET /api/isolation/received``) to confirm the file landed.
    """
    request_id = uuid.uuid4().hex[:12]
    client_host = request.client.host if request.client else "?"

    # ---- 1. Auth ---------------------------------------------------------
    received_key = request.headers.get(_KEY_HEADER)
    if _KEY:
        if received_key != _KEY:
            logger.warning(
                "[isolation-receiver] REJECT req_id=%s client=%s "
                "— invalid/missing %s",
                request_id, client_host, _KEY_HEADER,
            )
            raise HTTPException(
                status_code=401,
                detail=f"invalid or missing {_KEY_HEADER} header",
            )
    else:
        _warn_missing_key_once()

    # ---- 2. Parse JSON ---------------------------------------------------
    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning(
            "[isolation-receiver] BAD-JSON req_id=%s client=%s error=%s",
            request_id, client_host, exc,
        )
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    # ---- 3. Compute save path -------------------------------------------
    try:
        save_path = _build_save_path(payload)
    except Exception as exc:
        logger.exception(
            "[isolation-receiver] path build failed req_id=%s", request_id,
        )
        raise HTTPException(status_code=500, detail=f"save path error: {exc}")

    # ---- 4. Schedule background save + notify ---------------------------
    # One task does both so the Telegram message is built *after* the
    # save outcome is known — that is what makes the "Stage 2/3" line
    # in the message truthful rather than assumed.
    background_tasks.add_task(
        _save_then_notify_sync,
        payload,
        save_path,
        request_id,
        client_host,
    )

    # ---- 5. Immediate response ------------------------------------------
    is_test = bool(payload.get("is_test"))
    logger.info(
        "[isolation-receiver] RECEIVED req_id=%s client=%s kind=%s "
        "symbol=%s mode=%s (save + notify scheduled)",
        request_id, client_host,
        "test" if is_test else "live",
        payload.get("trading_symbol"),
        payload.get("standard_execution_mode"),
    )
    return {
        "ok": True,
        "status": "received",
        "message": "Request received, processing in background",
        "request_id": request_id,
        "saved_to": str(save_path),
        "received_at": datetime.now(ZoneInfo(_TZ)).isoformat(),
        "payload_summary": {
            "trading_symbol": payload.get("trading_symbol"),
            "standard_execution_mode": payload.get("standard_execution_mode"),
            "is_test": is_test,
            "is_dummy": bool(payload.get("is_dummy")),
            "source": payload.get("source"),
            "trade_date_ist": payload.get("trade_date_ist"),
        },
    }


@router.get("/received")
def list_received(limit: int = 50):
    """List recently saved isolation JSON files (newest first)."""
    if limit < 1 or limit > 500:
        limit = 50
    data_root = _DATA_DIR
    if not data_root.is_dir():
        return {
            "count": 0,
            "data_dir": str(data_root.resolve()),
            "files": [],
        }
    files = []
    for p in data_root.rglob("*.json"):
        try:
            stat = p.stat()
            files.append({
                "path": str(p.relative_to(data_root).as_posix()),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, ZoneInfo(_TZ)
                ).isoformat(),
            })
        except OSError:
            continue
    files.sort(key=lambda f: f["modified_at"], reverse=True)
    files = files[:limit]
    return {
        "count": len(files),
        "data_dir": str(data_root.resolve()),
        "files": files,
    }


@router.get("/received/{date}/{filename}")
def read_received(date: str, filename: str):
    """Read a single saved isolation JSON file. Path-traversal safe."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400, detail="invalid date (expected YYYY-MM-DD)"
        )

    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")

    base = _DATA_DIR.resolve()
    target = (base / date / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")

    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"file not found: {date}/{filename}",
        )
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"read failed: {exc}")

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw_content": content}