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
        candidates and updates the existing ones)                 <-- NEW
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

Config keys referenced (may be added to ``core/config.py`` as needed —
all read defensively via ``getattr``):

    * ``DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS``  (default 120)

Admin surfaces mounted by this module:

    Folder API             routes/folder_routes.py
    Market Holiday API     routes/market_holiday_routes.py
    Readiness API          routes/readiness_routes.py
    Level-Isolation API    routes/isolated_instrument_routes.py
    Simulation API         routes/simulation_routes.py
    Dummy-Orders API       routes/dummy_order_routes.py        <-- NEW
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import config
from core.logger import get_logger
from routes.admin_routes import _run_full_refresh_sync
from routes.admin_routes import router as admin_router
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

        # ---- Wait for SR backfill readiness, then dummy-order catch-up #
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

        registry_count = (result.get("registry") or {}).get("count", 0)
        telegram_running = _telegram_bot_running()

        isolation_summary = result.get("level_isolation") or {}
        isolated = isolation_summary.get("isolated") or {}
        isolation_status = isolation_summary.get("status")

        logger.info(
            "Application startup complete. feed_instruments=%d "
            "telegram_polling=%s holiday=%s level_isolation_status=%s "
            "isolated=%s dummy_orders=%s sr_ready=%s sr_wait_s=%.1f",
            registry_count,
            telegram_running,
            holiday_gate.get("is_holiday"),
            isolation_status,
            isolated.get("instrument_key"),
            dummy_summary.get("status"),
            sr_ready_info["ready"],
            sr_ready_info["elapsed_seconds"],
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

        startup_message = (
            f"{_DIV}\n"
            f"🚀 APPLICATION STARTED\n"
            f"{_DIV}\n"
            f"📊 Instruments:     {registry_count}\n"
            f"{holiday_line}"
            f"{isolation_line}"
            f"{sr_line}"
            f"{dummy_line}"
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
app = FastAPI(title="Market Data Foundation", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(page_router)
app.include_router(live_router)
app.include_router(live_sr_router)
app.include_router(live_sr_calc_router)
app.include_router(ema_calc_router)
app.include_router(admin_router)
app.include_router(folder_router)
app.include_router(upstox_router)
app.include_router(expired_instruments_router)
app.include_router(api_explorer_router)
app.include_router(expired_chart_router)
app.include_router(log_router)
app.include_router(market_holiday_router)
# app.include_router(order_router) # commented this for testing order routes (2026-11-04)
app.include_router(margin_router)
app.include_router(daily_backup_router)
app.include_router(layout_router)
app.include_router(readiness_router)
app.include_router(isolated_instrument_router)
app.include_router(simulation_router)
app.include_router(dummy_order_router)