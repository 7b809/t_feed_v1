# main.py
"""Foundational FastAPI app for home, charts, market history, token
lifecycle, backup extraction, live feed, and admin surfaces.

Project startup delegates to the shared ``_run_full_refresh_sync``
routine. The pipeline is **infrastructure-only** — strategy stages
(EMA backfill, Opening Range) are NOT part of startup and are not
mounted by this module either.

Startup task list (infrastructure only):

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core indexes (Nifty 50, SENSEX)
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  Start / restart live feed
    9b. Level-isolation backfill (replays intraday candles to
        reconstruct today's "would-have-isolated" instrument)
    9c. **Wait for SR backfill readiness (bounded)**, then dummy-order
        catch-up (creates any missing dummy orders for today's armed
        candidates and updates the existing ones)
    9d. **Register live sandbox executor on dummy_order_service**
        and run its startup reconciliation (ledger vs positions vs
        catch-up result)                              <-- NEW (2026-11-04)
    10. Start scheduler (idempotent)
    11. Start Telegram bot (idempotent)
    12. Telegram completion notification
    13. Return structured result

Dummy-order catch-up gating (2026-11-04)
----------------------------------------
``_run_full_refresh_sync`` returns as soon as
``live_feed_service.start()`` has been *scheduled* — the SR backfill
runs asynchronously inside feed start. Evidence (2026-10-29): level
isolation completed at 16:10:35, dummy-order catch-up fired at
16:10:36 while the SR backfill was already re-seeding (16:12:36).

The catch-up is now gated on ``sr_backfill_service.is_ready()`` (or
``is_ready_for_today()`` if that alias exists) with a bounded wait of
``DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS`` (default 120 s).
If the latch is not set in time, the catch-up is **skipped** — the
scheduler's next tick will retry — and the skip reason is written into
the startup result dict so the Telegram summary reflects what actually
happened. The catch-up is called exactly once per startup.

Live sandbox execution — registration ordering (2026-11-04)
-----------------------------------------------------------
The dummy-order engine is the *signal generator*; ``sandbox_execution_service``
mirrors its transitions onto real broker orders **only for** the
scheduled (``reason="scheduled"``) path. Startup catch-up runs with
``reason="startup"`` and is hard-rejected by the executor's gate chain,
which is why registration MUST happen **after** ``_dummy_order_catchup()``
returns — otherwise the catch-up's transitions would be emitted to a
registered executor that has no way to tell them apart from real events.

Correct order of operations in ``app_lifespan``:

    wait_for_sr_backfill_ready()
      → _dummy_order_catchup()          (reason="startup"; executor not yet registered)
      → register_with_dummy_order_service()
      → sandbox_execution_service.reconcile_startup()
      → scheduler ticks fire              (reason="scheduled"; executor fires)

The scheduler's ``_dummy_order_tick`` itself started inside
``_run_full_refresh_sync``, so it *may* have already run its first
tick by the time we get here — that is safe: (a) on builds without the
``is_ready_for_today`` probe it short-circuits every tick
(``missing_probe``), (b) even on builds that do tick, the executor is
not registered yet, so no real order is placed. Registration then
closes the window for all subsequent ticks.

Config keys referenced (may be added to ``core/config.py`` as needed —
all read defensively via ``getattr``):

    * ``DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS``  (default 120)
    * ``SANDBOX_EXECUTION_ENABLED``                          (default false)
    * ``SANDBOX_EXECUTION_MODE``                             (default "observe")
    * ``ADMIN_API_KEY``                                      (default "")
    * ``ADMIN_BASIC_USER``                                   (default "admin")
    * ``ADMIN_BASIC_PASSWORD``                               (default "")
    * ``ADMIN_SESSION_SECRET``                               (default "")
    * ``ADMIN_SESSION_TTL_SECONDS``                          (default 8h)
    * ``ADMIN_COOKIE_NAME``                                  (default "novag_admin")
    * ``ADMIN_COOKIE_SECURE``                                (default true)
    * ``CORS_ALLOW_ORIGINS``                                 (default "*")

Authentication (2026-11-05)
---------------------------
The scanner traffic observed on 2026-11-05 (400+ probes for .env /
.git / wp-config / terraform.tfstate / etc. from 45.138.12.172 and
others) proved that this box is directly reachable from the public
internet. ``/openapi.json`` and ``/docs`` were returning 200, i.e. the
whole API surface was public.

This module now:

  * disables ``docs_url`` / ``redoc_url`` / ``openapi_url`` entirely
    unless at least one credential is configured (see
    ``_DOCS_ENABLED`` below), so the default deployment is closed;
  * when docs are enabled (because ``ADMIN_API_KEY`` or
    ``ADMIN_BASIC_PASSWORD`` is set), gates them behind the
    ``_gate_docs`` HTTP middleware, which requires the same auth as
    the admin routers;
  * requires auth (Bearer / X-API-Key / Basic / session-cookie) on
    every sensitive router: admin, folders, logs, isolated-instrument,
    simulation, dummy-orders, sandbox, margin, daily-backup,
    readiness, api-explorer;
  * replaces the ``allow_origins=["*"] + allow_credentials=True``
    combination (rejected by browsers, CORS spec violation) with a
    real allow-list from ``CORS_ALLOW_ORIGINS``.

Global auth guard (2026-11-05)
------------------------------
``AuthGuardMiddleware`` (``core/auth_guard.py``) is installed on the
app as the outermost auth layer. For every request it:

  * lets the auth-bypass paths through (``/login``, ``/logout``,
    ``/auth/whoami``);
  * lets the docs paths through (they are on the guard's public
    whitelist, and the ``_gate_docs`` middleware below performs the
    actual auth check on them);
  * lets static asset paths through (``/static/``, ``/assets/``,
    ``/_health``);
  * validates the request using ``core.auth.is_authenticated()``,
    which accepts Bearer / X-API-Key / Basic / session-cookie;
  * on failure, distinguishes browser navigations (``Accept:
    text/html``) — which are redirected to ``/login?next=<path>`` —
    from API / XHR / curl callers, which get a plain JSON 401 with
    no ``WWW-Authenticate`` challenge (the login page is the intended
    recovery path).

The previous pair of hand-rolled middlewares
(``_redirect_html_to_login`` and a docs-only ``_gate_docs`` that
returned a Basic challenge) has been replaced by this single guard.
``_gate_docs`` is retained only because docs paths appear on the
guard's public whitelist and therefore still need a focused auth
check with the same redirect/401 behaviour as the guard itself.

Browser login flow (2026-11-05)
-------------------------------
The header schemes above are designed for machines (curl / CI /
internal tools). A browser, however, cannot attach ``X-API-Key`` or
``Authorization`` headers to a plain ``<a href>`` navigation, so
every click on ``/logs``, ``/admin/...``, ``/isolated`` etc. was
returning a raw JSON 401. To fix this, two pieces were added:

  * ``routes/auth_routes.py`` (public, NOT behind ``_AUTH``) serves
    ``GET /login`` / ``POST /login`` / ``GET /logout`` /
    ``GET /auth/whoami``. A successful login sets an HMAC-signed
    cookie (name ``ADMIN_COOKIE_NAME``, HttpOnly, Secure, SameSite=Lax,
    TTL ``ADMIN_SESSION_TTL_SECONDS``).
  * ``core/auth.py`` accepts that cookie anywhere the header schemes
    are accepted — ``require_any_auth``, ``docs_request_is_authenticated``,
    and ``is_authenticated`` (used by ``AuthGuardMiddleware``).

The frontend (``templates/base.html``) additionally:

  * installs a global ``fetch()`` interceptor that redirects to
    ``/login?next=<path>`` on any 401, so an expired cookie mid-page
    is caught immediately;
  * renders a red-tinted "Logout" button in the nav / mobile drawer /
    FAB pill when ``/auth/whoami`` returns 200, and re-checks on
    ``visibilitychange`` / ``pageshow`` so a session expiring in the
    background flips the button state without a hard refresh;
  * calls ``GET /logout`` (browser navigation path, so the backend
    returns a 303 to ``/login`` and clears the cookie).

Note on ``WWW-Authenticate``: RFC 7235 §4.1 does not permit combining
two challenge schemes in one header value. The previous
``'Basic realm="admin", Bearer'`` was malformed and browsers silently
ignored it, which is why no Basic prompt ever appeared. The header is
no longer emitted by the new redirect-based flow — the browser is sent
to ``/login`` instead, so no challenge prompt is needed.

Admin surfaces mounted by this module:

    Folder API             routes/folder_routes.py
    Market Holiday API     routes/market_holiday_routes.py
    Readiness API          routes/readiness_routes.py
    Level-Isolation API    routes/isolated_instrument_routes.py
    Simulation API         routes/simulation_routes.py
    Dummy-Orders API       routes/dummy_order_routes.py
    Sandbox-Orders API     routes/admin_orders.py (sandbox-status)   <-- NEW
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from core import config
from core.auth import docs_request_is_authenticated, require_any_auth
from core.auth_guard import AuthGuardMiddleware
from core.logger import get_logger
from routes.admin_routes import _run_full_refresh_sync
from routes.admin_routes import router as admin_router
from routes.auth_routes import router as auth_router
from routes.daily_backup_routes import router as daily_backup_router
from routes.dummy_order_routes import router as dummy_order_router
from routes.ema_calc_routes import router as ema_calc_router
from routes.folder_routes import router as folder_router
from routes.isolated_instrument_routes import router as isolated_instrument_router
from routes.layout_routes import router as layout_router
from routes.live_routes import router as live_router
from routes.log_routes import router as log_router
from routes.margin_routes import router as margin_router
from routes.market_holiday_routes import router as market_holiday_router
from routes.order_routes import router as order_router
from routes.page_routes import router as page_router
from routes.readiness_routes import router as readiness_router
from routes.simulation_routes import router as simulation_router
from routes.upstox_routes import router as upstox_router
from routes.live_sr_routes import router as live_sr_router
from routes.live_sr_calc_routes import router as live_sr_calc_router
from routes.api_explorer_route import router as api_explorer_router
from routes.sandbox_routes import router as sandbox_router

from upstox_app.apis.expired_instruments_api import (
    router as expired_instruments_router,
)

from routes.expired_chart_routes import router as expired_chart_router


from services.config_validation import validate_config
from services.live_feed_service import live_feed_service
from services.market_holiday_service import market_holiday_service
from services.scheduler_service import scheduler_service
from services.telegram_service import telegram_service
from token_tasks.telegram_token_bot import telegram_token_bot

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Decorative constants (pure Unicode — no markdown needed)                    #
# --------------------------------------------------------------------------- #
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

# Poll interval while waiting for the SR backfill readiness latch.
# Kept small (2 s) so the typical case — backfill already complete by
# the time ``_run_full_refresh_sync`` returns — costs a single check.
_DUMMY_ORDER_SR_BACKFILL_POLL_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Auth / docs gating                                                          #
# --------------------------------------------------------------------------- #
# Docs are only enabled when at least one credential is configured.
# On an unconfigured box (no ADMIN_API_KEY, no ADMIN_BASIC_PASSWORD)
# FastAPI is built with docs_url=None, redoc_url=None, openapi_url=None
# so /docs, /redoc and /openapi.json all return 404 — the scanner
# cannot map the API surface.
_HAVE_HEADER_AUTH = bool((getattr(config, "ADMIN_API_KEY", "") or "").strip())
_HAVE_BASIC_AUTH = bool(
    (getattr(config, "ADMIN_BASIC_USER", "") or "").strip()
    and (getattr(config, "ADMIN_BASIC_PASSWORD", "") or "")
)
_DOCS_ENABLED = _HAVE_HEADER_AUTH or _HAVE_BASIC_AUTH

# The three FastAPI-served docs surfaces. All other auth is enforced
# by ``AuthGuardMiddleware``; these three are on that guard's public
# whitelist (so the guard passes them through) and are separately
# gated by ``_gate_docs`` below.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


# --------------------------------------------------------------------------- #
# Telegram bot state helper                                                    #
# --------------------------------------------------------------------------- #
def _telegram_bot_running() -> bool:
    """Return the bot's **actual** polling state."""
    running = getattr(telegram_token_bot, "is_running", None)
    if isinstance(running, bool):
        return running
    return bool(getattr(telegram_token_bot, "enabled", False))


# --------------------------------------------------------------------------- #
# Startup configuration validation                                            #
# --------------------------------------------------------------------------- #
def _run_config_validation() -> tuple[list[str], list[str]]:
    """Validate config and surface the result on Telegram + logs."""
    try:
        errors, warnings = validate_config()
    except Exception as exc:
        logger.exception("Config validation raised")
        errors = [
            f"validate_config() raised {type(exc).__name__}: {exc}"
        ]
        warnings = []

    if warnings:
        logger.warning(
            "Config validation produced %d warning(s):\n%s",
            len(warnings),
            "\n".join(f"  • {w}" for w in warnings),
        )

    if errors:
        logger.error(
            "Config validation FAILED with %d error(s):\n%s",
            len(errors),
            "\n".join(f"  • {e}" for e in errors),
        )
        try:
            telegram_service.send_message(
                "❌ Config Validation Failed",
                (
                    "The following configuration errors were detected "
                    "at startup. The process is running, but one or "
                    "more subsystems are likely disabled until this "
                    "is fixed.\n\n"
                    + "\n".join(f"• {e}" for e in errors)
                ),
                level="ERROR",
            )
        except Exception:
            logger.exception(
                "Failed to send config validation Telegram alert"
            )
    else:
        logger.info(
            "Config validation OK (%d warning(s))", len(warnings)
        )

    return errors, warnings


# --------------------------------------------------------------------------- #
# Market holiday gate                                                          #
# --------------------------------------------------------------------------- #
def _holiday_gate_for_startup() -> dict[str, Any]:
    """Ask the holiday service whether today is a trading holiday."""
    try:
        payload = market_holiday_service.get_today()
    except Exception as exc:
        logger.exception(
            "Holiday gate: market_holiday_service.get_today() raised"
        )
        return {
            "date": None,
            "status": "error",
            "is_holiday": False,
            "should_run": True,
            "holiday_count": 0,
            "description": None,
            "error": f"{type(exc).__name__}: {exc}",
            "cached": False,
        }

    status = str(payload.get("status") or "error").lower()
    is_holiday = bool(payload.get("is_holiday"))
    description: str | None = None
    holidays = payload.get("holidays") or []
    if holidays and isinstance(holidays[0], dict):
        description = holidays[0].get("description") or None

    should_run = not is_holiday

    logger.info(
        "Holiday gate: date=%s status=%s is_holiday=%s should_run=%s "
        "description=%s error=%s cached=%s",
        payload.get("date"),
        status,
        is_holiday,
        should_run,
        description,
        payload.get("error"),
        payload.get("cached"),
    )

    return {
        "date": payload.get("date"),
        "status": status,
        "is_holiday": is_holiday,
        "should_run": should_run,
        "holiday_count": int(payload.get("holiday_count") or 0),
        "description": description,
        "error": payload.get("error"),
        "cached": bool(payload.get("cached")),
    }


# --------------------------------------------------------------------------- #
# SR backfill readiness wait (2026-11-04)                                     #
# --------------------------------------------------------------------------- #
def _sr_backfill_is_ready() -> bool:
    """Best-effort readiness probe for the SR backfill latch.

    Prefers ``sr_backfill_service.is_ready_for_today()`` when the
    service exposes that name (the documented gate), and falls back to
    ``is_ready()`` — which is what the current ``sr_backfill_service``
    implementation exports. Returns ``False`` if neither method exists
    (older service build), so the caller's timeout path handles it
    defensively instead of crashing startup.
    """
    try:
        from services.sr_backfill_service import sr_backfill_service
    except Exception:
        logger.exception(
            "[startup] sr_backfill_service import failed during "
            "readiness probe"
        )
        return False

    for method_name in ("is_ready_for_today", "is_ready"):
        method = getattr(sr_backfill_service, method_name, None)
        if callable(method):
            try:
                return bool(method())
            except Exception:
                logger.exception(
                    "[startup] sr_backfill_service.%s() raised",
                    method_name,
                )
                return False
    return False


async def _wait_for_sr_backfill_ready(
    timeout_seconds: float | None = None,
    poll_seconds: float = _DUMMY_ORDER_SR_BACKFILL_POLL_SECONDS,
) -> dict[str, Any]:
    """Await the SR backfill readiness latch, bounded by a timeout.

    Returns a dict with:

        ready            — True iff the latch was set within the timeout
        elapsed_seconds  — how long we waited
        timeout_seconds  — configured timeout (for logging / display)
        reason           — "already_ready" | "became_ready" | "timeout"

    The immediate check is cheap — in the common case where SR backfill
    has already completed by the time ``_run_full_refresh_sync``
    returns, this function returns without ever entering the poll loop.
    """
    if timeout_seconds is None:
        timeout_seconds = float(
            getattr(
                config,
                "DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS",
                120.0,
            )
        )

    # ---- Immediate check (fast path) ----------------------------- #
    if _sr_backfill_is_ready():
        return {
            "ready": True,
            "elapsed_seconds": 0.0,
            "timeout_seconds": float(timeout_seconds),
            "reason": "already_ready",
        }

    start = time.monotonic()
    deadline = start + float(timeout_seconds)

    while time.monotonic() < deadline:
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            raise
        if _sr_backfill_is_ready():
            return {
                "ready": True,
                "elapsed_seconds": time.monotonic() - start,
                "timeout_seconds": float(timeout_seconds),
                "reason": "became_ready",
            }

    return {
        "ready": False,
        "elapsed_seconds": time.monotonic() - start,
        "timeout_seconds": float(timeout_seconds),
        "reason": "timeout",
    }


# --------------------------------------------------------------------------- #
# Dummy-order catch-up                                                         #
# --------------------------------------------------------------------------- #
def _dummy_order_catchup() -> dict[str, Any]:
    """Run one dummy-order scan right after startup so today's already-
    armed candidates get their dummy orders created (or reconciled)
    immediately — without waiting for the first scheduler tick.

    **Called exactly once per startup** — the single call site in
    ``app_lifespan`` gates it on ``_wait_for_sr_backfill_ready()`` and
    writes the summary (or a skip record) into the startup result dict.
    """
    try:
        from services.dummy_order_service import dummy_order_service
        summary = dummy_order_service.run_once(reason="startup")
        logger.info(
            "[startup] dummy-order catch-up — status=%s created=%s "
            "updated=%s closed=%s open=%s total=%s mode=%s",
            summary.get("status"),
            summary.get("created"),
            summary.get("updated"),
            summary.get("closed"),
            summary.get("open_count"),
            summary.get("total_today"),
            summary.get("mode"),
        )
        return summary
    except Exception as exc:
        logger.exception("[startup] dummy-order catch-up failed")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


# --------------------------------------------------------------------------- #
# Live sandbox executor — registration + startup reconciliation (2026-11-04)  #
# --------------------------------------------------------------------------- #
def _register_and_reconcile_sandbox(
    dummy_summary: dict[str, Any],
) -> dict[str, Any]:
    """Wire the sandbox executor to dummy_order_service and reconcile.

    Order matters:

        1. Catch-up already ran with ``reason="startup"`` (executor not
           yet registered → no events captured).
        2. Register the callback — subsequent scheduler ticks emit
           ``reason="scheduled"`` transitions the executor will act on.
        3. Reconcile ledger vs positions vs catch-up result, so a
           restart mid-day does not leave the sandbox in an inconsistent
           state.

    Always returns a dict for ``result["sandbox_execution"]``. Never
    raises.
    """
    try:
        from services.sandbox_execution_service import (
            sandbox_execution_service,
            register_with_dummy_order_service,
        )
    except Exception as exc:
        logger.exception(
            "[startup] sandbox_execution_service import failed"
        )
        return {
            "status": "error",
            "registered": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    registered = False
    try:
        registered = bool(register_with_dummy_order_service())
    except Exception as exc:
        logger.exception(
            "[startup] sandbox executor registration failed"
        )
        return {
            "status": "error",
            "registered": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    reconcile_summary: dict[str, Any] = {}
    try:
        reconcile_summary = sandbox_execution_service.reconcile_startup(
            dummy_summary=dummy_summary,
        ) or {}
    except Exception as exc:
        logger.exception(
            "[startup] sandbox reconcile_startup failed"
        )
        reconcile_summary = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # Single canonical summary line so the operator can answer
    # "did the sandbox come up cleanly?" from one grep.
    logger.info(
        "[sandbox_exec] startup reconcile entered=%d exited_late=%d "
        "missed_entry=%d stuck=%d mode=%s enabled=%s registered=%s",
        int(reconcile_summary.get("entered", 0) or 0),
        int(reconcile_summary.get("exited_late", 0) or 0),
        int(reconcile_summary.get("missed_entry", 0) or 0),
        int(reconcile_summary.get("stuck", 0) or 0),
        str(reconcile_summary.get("mode", "?")),
        bool(reconcile_summary.get("execution_enabled", False)),
        registered,
    )

    return {
        "status": "ok" if registered else "warn",
        "registered": registered,
        **reconcile_summary,
    }


# --------------------------------------------------------------------------- #
# Lifespan                                                                     #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    # ---- Startup config validation (fail loud, not fast) ----------- #
    _run_config_validation()

    try:
        # ---- Holiday gate --------------------------------------------- #
        holiday_gate = _holiday_gate_for_startup()

        if not holiday_gate["should_run"]:
            logger.info(
                "Holiday gate: skipping unified refresh for %s (%s)",
                holiday_gate.get("date"),
                holiday_gate.get("description") or "trading holiday",
            )
            try:
                scheduler_service.start()
            except Exception:
                logger.exception("Holiday gate: scheduler start failed")
            try:
                telegram_token_bot.start()
            except Exception:
                logger.exception("Holiday gate: telegram bot start failed")

            telegram_service.send_message(
                "Application Started · Holiday",
                (
                    f"{_DIV}\n"
                    f"🏖️ APPLICATION STARTED · HOLIDAY\n"
                    f"{_DIV}\n"
                    f"📅 Date:        {holiday_gate.get('date')}\n"
                    f"🎯 Reason:      "
                    f"{holiday_gate.get('description') or 'trading holiday'}\n"
                    f"⏰ Scheduler:   "
                    f"{'✓' if scheduler_service.scheduler else '✗'}\n"
                    f"🤖 Telegram bot: "
                    f"{'✓' if _telegram_bot_running() else '✗'}\n"
                    f"{_THIN}\n"
                    f"⏭️ Pipeline skipped — no market data expected.\n"
                    f"{_DIV}\n"
                    f"✅ Ready (holiday mode)"
                ),
                level="INFO",
            )
            yield
            return

        # ---- Unified refresh (infrastructure only) ------------------- #
        result = await asyncio.to_thread(
            _run_full_refresh_sync, "startup", loop
        )
        result["holiday_gate"] = holiday_gate

        # ---- Wait for SR backfill readiness, then dummy-order catch-up # #
        # NOTE: ``_run_full_refresh_sync`` returns as soon as
        # ``live_feed_service.start()`` has been scheduled — the SR
        # backfill runs asynchronously inside feed start. Wait for the
        # readiness latch (bounded by
        # ``DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS``) before
        # running the catch-up so it doesn't fire against a half-built
        # SR state. If the latch does not set in time, SKIP the
        # catch-up — the scheduler's next tick will retry — and record
        # the skip reason in ``result["dummy_orders"]`` so the startup
        # Telegram summary reflects what actually happened.
        #
        # This is the ONLY call site of ``_dummy_order_catchup()`` in
        # the startup path. It runs exactly once per startup.
        sr_ready_info = await _wait_for_sr_backfill_ready()
        if sr_ready_info["ready"]:
            if sr_ready_info["reason"] != "already_ready":
                logger.info(
                    "[startup] SR backfill readiness achieved after "
                    "%.1fs (%s) — running dummy-order catch-up",
                    sr_ready_info["elapsed_seconds"],
                    sr_ready_info["reason"],
                )
            dummy_summary = await asyncio.to_thread(_dummy_order_catchup)
            dummy_summary["sr_backfill_wait"] = sr_ready_info
        else:
            dummy_summary = {
                "status": "skipped",
                "reason": (
                    f"sr_backfill_not_ready:{sr_ready_info['reason']}"
                ),
                "error": None,
                "sr_backfill_wait": sr_ready_info,
            }
            logger.warning(
                "[startup] dummy-order catch-up skipped — SR backfill "
                "readiness not achieved within %.1fs (%s); "
                "scheduler will retry on next tick",
                sr_ready_info["elapsed_seconds"],
                sr_ready_info["reason"],
            )
        result["dummy_orders"] = dummy_summary

        # ---- Live sandbox executor: register + reconcile ------------ #
        # Runs AFTER the catch-up (which used reason="startup" and is
        # ignored by the executor) and BEFORE the next scheduled tick
        # can act on new transitions. See module docstring step 9d.
        sandbox_summary = await asyncio.to_thread(
            _register_and_reconcile_sandbox, dummy_summary,
        )
        result["sandbox_execution"] = sandbox_summary

        registry_count = (result.get("registry") or {}).get("count", 0)
        telegram_running = _telegram_bot_running()

        isolation_summary = result.get("level_isolation") or {}
        isolated = isolation_summary.get("isolated") or {}
        isolation_status = isolation_summary.get("status")

        logger.info(
            "Application startup complete. feed_instruments=%d "
            "telegram_polling=%s holiday=%s level_isolation_status=%s "
            "isolated=%s dummy_orders=%s sr_ready=%s sr_wait_s=%.1f "
            "sandbox_registered=%s sandbox_mode=%s docs_enabled=%s",
            registry_count,
            telegram_running,
            holiday_gate.get("is_holiday"),
            isolation_status,
            isolated.get("instrument_key"),
            dummy_summary.get("status"),
            sr_ready_info["ready"],
            sr_ready_info["elapsed_seconds"],
            sandbox_summary.get("registered"),
            sandbox_summary.get("mode"),
            _DOCS_ENABLED,
        )

        # ---- Startup Telegram ------------------------------------------ #
        holiday_line = (
            f"🏖️ Holiday:       "
            f"{holiday_gate.get('description') or 'yes'}\n"
            if holiday_gate.get("is_holiday")
            else f"🏖️ Holiday:       no\n"
        )

        isolation_line = ""
        if isolated:
            isolation_line = (
                f"🎯 Isolated:      "
                f"{isolated.get('trading_symbol') or isolated.get('instrument_key')} "
                f"({isolated.get('reason')})\n"
            )
        elif isolation_status:
            isolation_line = (
                f"🎯 Isolation:     {isolation_status}\n"
            )

        # SR backfill wait line — surfaces the readiness outcome so the
        # operator can tell at a glance whether the dummy-order catch-up
        # actually ran or was deferred to the scheduler.
        if sr_ready_info["ready"]:
            if sr_ready_info["reason"] == "already_ready":
                sr_line = "🎯 SR backfill:    ✓ (ready)"
            else:
                sr_line = (
                    f"🎯 SR backfill:    ✓ (waited "
                    f"{sr_ready_info['elapsed_seconds']:.1f}s)"
                )
            sr_line += "\n"
        else:
            sr_line = (
                f"🎯 SR backfill:    ✗ timeout "
                f"({sr_ready_info['elapsed_seconds']:.0f}s / "
                f"{sr_ready_info['timeout_seconds']:.0f}s)\n"
            )

        dummy_line = ""
        if dummy_summary.get("status") == "ok":
            dummy_line = (
                f"📋 Dummy orders:  "
                f"created={dummy_summary.get('created')} "
                f"open={dummy_summary.get('open_count')} "
                f"closed={dummy_summary.get('closed_count')}\n"
            )
        elif dummy_summary.get("status") in {"skipped", "no_signal"}:
            skip_reason = str(dummy_summary.get("reason") or "").strip()
            dummy_line = (
                f"📋 Dummy orders:  {dummy_summary.get('status')}"
                f"{f' ({skip_reason})' if skip_reason else ''}\n"
            )
        else:
            dummy_line = (
                f"📋 Dummy orders:  {dummy_summary.get('status')} "
                f"({dummy_summary.get('error') or dummy_summary.get('detail') or '—'})\n"
            )

        # Sandbox execution line — shows the mode and, when a stale
        # ledger was reconciled on restart, the counts so the operator
        # knows the executor is not starting from a clean slate.
        sandbox_line = ""
        if sandbox_summary.get("registered"):
            sandbox_mode = str(sandbox_summary.get("mode") or "?")
            sandbox_enabled = bool(sandbox_summary.get("execution_enabled"))
            extra_bits: list[str] = []
            if sandbox_summary.get("open_position"):
                extra_bits.append(
                    f"open={sandbox_summary.get('open_position')}"
                )
            if int(sandbox_summary.get("exited_late", 0) or 0) > 0:
                extra_bits.append(
                    f"exited_late={sandbox_summary.get('exited_late')}"
                )
            if int(sandbox_summary.get("missed_entry", 0) or 0) > 0:
                extra_bits.append(
                    f"missed_entry={sandbox_summary.get('missed_entry')}"
                )
            if sandbox_summary.get("stuck"):
                extra_bits.append("STUCK")
            extra = (" · " + " · ".join(extra_bits)) if extra_bits else ""
            sandbox_line = (
                f"🎬 Sandbox exec:  {sandbox_mode} · "
                f"{'enabled' if sandbox_enabled else 'disabled'}{extra}\n"
            )
        else:
            sandbox_line = (
                f"🎬 Sandbox exec:  ✗ not registered "
                f"({sandbox_summary.get('error') or 'unavailable'})\n"
            )

        # Auth line — one-glance confirmation of the security posture.
        auth_line = (
            f"🔐 Auth:          "
            f"{'header' if _HAVE_HEADER_AUTH else ''}"
            f"{'/' if _HAVE_HEADER_AUTH and _HAVE_BASIC_AUTH else ''}"
            f"{'basic' if _HAVE_BASIC_AUTH else ''}"
            f"{' · docs=on' if _DOCS_ENABLED else ' · docs=off'}\n"
            if (_HAVE_HEADER_AUTH or _HAVE_BASIC_AUTH)
            else "🔐 Auth:          ⚠️ DISABLED — all admin routes are public\n"
        )

        startup_message = (
            f"{_DIV}\n"
            f"🚀 APPLICATION STARTED\n"
            f"{_DIV}\n"
            f"📊 Instruments:     {registry_count}\n"
            f"{holiday_line}"
            f"{isolation_line}"
            f"{sr_line}"
            f"{dummy_line}"
            f"{sandbox_line}"
            f"{auth_line}"
            f"📡 Live feed:       "
            f"{'✓' if result.get('feed_restarted') else '✗'}\n"
            f"⏰ Scheduler:       "
            f"{'✓' if result.get('scheduler_started') else '✗'}\n"
            f"🤖 Telegram bot:    "
            f"{'✓' if telegram_running else '✗'}\n"
            f"{_DIV}\n"
            f"✅ Ready"
        )

        telegram_service.send_message(
            "Application Started",
            startup_message,
            level="SUCCESS",
        )
        yield

    finally:
        # ---- Shutdown handling ----------------------------------------- #
        telegram_service.send_message(
            "Application Stopping",
            (
                f"{_THIN}\n"
                f"🛑 APPLICATION STOPPING\n"
                f"{_THIN}\n"
                f"Shutting down live feed, scheduler and Telegram bot…"
            ),
            level="INFO",
        )

        stop_targets = [
            ("Live feed", live_feed_service.stop),
            ("Telegram token bot", telegram_token_bot.stop),
            ("Scheduler", scheduler_service.shutdown),
        ]
        for name, fn in stop_targets:
            try:
                fn()
                logger.info("%s stopped", name)
            except Exception:
                logger.exception("%s stop failed", name)

        telegram_service.send_message(
            "Application Stopped",
            (
                f"{_THIN}\n"
                f"🏁 APPLICATION STOPPED\n"
                f"{_THIN}\n"
                f"Market engine shutdown complete."
            ),
            level="INFO",
        )
        logger.info("Application shutdown complete.")


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #
# Build FastAPI with docs CLOSED by default. When at least one credential
# is configured (ADMIN_API_KEY or ADMIN_BASIC_PASSWORD), docs are
# re-enabled — and then protected by ``_gate_docs`` below.
app = FastAPI(
    title="Market Data Foundation",
    lifespan=app_lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


# ---- CORS ------------------------------------------------------------------ #
# ``allow_origins=["*"] + allow_credentials=True`` is rejected by
# browsers (CORS spec) and was the previous configuration. Use the
# allow-list from CORS_ALLOW_ORIGINS instead. When the list is exactly
# ``["*"]`` we set allow_credentials=False so the browser does not
# silently drop the CORS headers.
_cors_origins = list(getattr(config, "CORS_ALLOW_ORIGINS", ["*"]) or ["*"])
_cors_wildcard = _cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- /docs, /redoc, /openapi.json auth gate -------------------------------- #
# ``AuthGuardMiddleware`` (below) covers every path that is not on its
# public whitelist — but the docs paths ARE on that whitelist (so the
# guard passes them through untouched). This middleware performs the
# actual auth check on those three paths and mirrors the guard's
# redirect-or-401 behaviour:

#   * browser (Accept: text/html) → 303 to /login?next=<path>
#   * API / curl / XHR             → 401 JSON with no Basic challenge
#                                    (the login page is the recovery path)
#
# No-op when docs are disabled (the FastAPI app was created with
# docs_url=None so /docs never resolves anyway).
@app.middleware("http")
async def _gate_docs(request: Request, call_next):
    if not _DOCS_ENABLED:
        return await call_next(request)

    path = request.url.path
    is_docs_path = any(
        path == p or path.startswith(p + "/") for p in _DOCS_PATHS
    )
    if not is_docs_path:
        return await call_next(request)

    if docs_request_is_authenticated(request):
        return await call_next(request)

    logger.warning(
        "[auth] rejected docs request path=%s client=%s",
        path,
        request.client.host if request.client else "?",
    )

    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return RedirectResponse(
            f"/login?next={path}",
            status_code=303,
        )

    return JSONResponse(
        {"detail": "Authentication required", "login_url": "/login"},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


# ---- Global auth guard ----------------------------------------------------- #
# ``AuthGuardMiddleware`` is the outer auth layer. It is added LAST so it
# runs FIRST (Starlette middleware stack is LIFO), intercepting every
# unauth request before it reaches CORS, ``_gate_docs``, or the router.
#
# Behaviour (see ``core/auth_guard.py``):
#   * auth-bypass paths       → pass through (/login, /logout, /auth/whoami)
#   * docs paths              → pass through (handled by ``_gate_docs`` above)
#   * static asset paths      → pass through
#   * authenticated requests  → pass through
#   * unauth HTML navigation  → 303 redirect to /login?next=<path>
#   * unauth API / curl / XHR → 401 JSON {detail, login_url}
#
# This replaces the previous hand-rolled pair
# (``_redirect_html_to_login`` + a docs-only ``_gate_docs`` that returned
# a Basic challenge). One guard, one consistent behaviour, no
# hand-maintained route prefix list.
app.add_middleware(AuthGuardMiddleware)


# --------------------------------------------------------------------------- #
# Routers                                                                      #
# --------------------------------------------------------------------------- #
# ---- Public surfaces (no auth) -------------------------------------------- #
# The auth router MUST be public — /login is how a browser obtains the
# session cookie that the gated routers below will accept. Everything
# else in this block is the front-end surface: UI pages, live REST,
# live-SR, EMA-calc, upstox proxy, expired instrument/chart, market
# holiday, layout. If you decide any of these must be private, add
# ``dependencies=[Depends(require_any_auth)]`` to the include below.
#
# NOTE: AuthGuardMiddleware (above) is the authoritative gate — this
# block is simply "these routers don't add their own per-route auth".
# Any path not on the guard's whitelist will still be redirected /
# 401'd, even if the router itself is mounted here.
app.include_router(auth_router)          # /login, /logout, /auth/whoami
app.include_router(page_router)
app.include_router(live_router)
app.include_router(live_sr_router)
app.include_router(live_sr_calc_router)
app.include_router(ema_calc_router)
app.include_router(upstox_router)
app.include_router(expired_instruments_router)
app.include_router(expired_chart_router)
app.include_router(market_holiday_router)
app.include_router(layout_router)


# ---- Sensitive surfaces (require auth) ----------------------------------- #
# Admin, folders, logs, isolated-instrument, simulation, dummy-orders,
# sandbox, margin, daily-backup, readiness, api-explorer.
#
# All of these were reachable by the scanner on 2026-11-05. They are now
# gated on ``require_any_auth`` (Bearer / X-API-Key / Basic / session-
# cookie). The cookie path is what lets a browser reach these after a
# successful /login.
_AUTH = [Depends(require_any_auth)]

app.include_router(admin_router, dependencies=_AUTH)
app.include_router(folder_router, dependencies=_AUTH)
app.include_router(log_router, dependencies=_AUTH)
app.include_router(isolated_instrument_router, dependencies=_AUTH)
app.include_router(simulation_router, dependencies=_AUTH)
app.include_router(dummy_order_router, dependencies=_AUTH)
app.include_router(sandbox_router, dependencies=_AUTH)
app.include_router(margin_router, dependencies=_AUTH)
app.include_router(daily_backup_router, dependencies=_AUTH)
app.include_router(readiness_router, dependencies=_AUTH)
app.include_router(api_explorer_router, dependencies=_AUTH)

# The order router was already commented out in the original file:
# app.include_router(order_router)   # commented 2026-11-04 for testing