"""Foundational FastAPI app for home, charts, market history, token
lifecycle, backup extraction, live feed / EMA-cross engine, Opening
Range levels, and the multi-stage index strategy pipeline
(Strategy 1 and Strategy 2).

Project startup delegates to the shared ``_run_full_refresh_sync``
routine so that project startup, the scheduled daily refresh, and the
manual hard-refresh endpoint all execute the exact same task list in
the same order:

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core + strategy indexes
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  EMA backfill seed (backup + intraday)
    10. Opening Range (OR) level calculation
    10b. Strategy 1 · Stage 1 (buffer filter + last-day-cross)
    10c. Strategy 1 · Stage 2 (EMA-cross isolation, paired CE/PE)
    10d. Strategy 1 · Stage 3 tracker start (R2 activation watcher)
    10e. Strategy 1 · Stage 3 catch-up scan (if past window)
    10f. Strategy 1 · Stage 4 catch-up order (if past 09:18)
    10g. Strategy 2 · Stage 3 tracker start (S3 touch watcher)
    10h. Strategy 2 · Stage 3 snapshot (isolation-level touch filter)
    10i. Strategy 2 · Stage 4 order (next-cross order, if isolated)
    11. Start / restart live feed
    12. Start scheduler (idempotent)
    13. Start Telegram bot (idempotent)
    14. Telegram completion notification
    15. Return structured result

Market holiday gate
-------------------
Before running the unified refresh, startup asks the new
``services.market_holiday_service`` (Upstox Market Holidays & Timings
API) whether the current market date is a trading holiday. Behaviour:

    * ``is_holiday = True``   -> pipeline skipped; Telegram message
                                 says "APPLICATION STARTED · HOLIDAY";
                                 scheduler + Telegram bot still start
                                 so token checks keep working.
    * ``is_holiday = False``  -> full pipeline runs as before.
    * ``status = "error"``    -> treated as **not a holiday** so a
                                 provider outage can never silently
                                 skip the pipeline. The error is
                                 recorded in ``holiday_gate`` for
                                 debugging.

The same gate is exposed to other callers via
``GET /admin/market-holiday`` and can be reused by the scheduled daily
refresh / OR / Stage 1 tasks if you want them to skip holidays too.

Shutdown handling is owned by the lifespan only.

Admin surfaces mounted by this module:

    Folder API            routes/folder_routes.py
    Opening Range API     routes/opening_range_routes.py
    Strategy 1 API        routes/strategy_routes.py
    Strategy 2 API        routes/strategy_2_routes.py
    Strategy Dashboard    routes/dashboard_routes.py
    Market Holiday API    routes/market_holiday_routes.py
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logger import get_logger
from routes.admin_routes import _run_full_refresh_sync
from routes.admin_routes import router as admin_router
from routes.dashboard_routes import router as dashboard_router
from routes.folder_routes import router as folder_router
from routes.live_routes import router as live_router
from routes.log_routes import router as log_router
from routes.market_holiday_routes import router as market_holiday_router
from routes.opening_range_routes import router as opening_range_router
from routes.page_routes import router as page_router
from routes.strategy_2_routes import router as strategy_2_router
from routes.strategy_routes import router as strategy_router
from routes.upstox_routes import router as upstox_router
from services.live_feed_service import live_feed_service
from services.market_holiday_service import market_holiday_service
from services.scheduler_service import scheduler_service
from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.strategy_2_live_tracker_service import (
    strategy_2_live_tracker_service,
)
from services.telegram_service import telegram_service
from token_tasks.telegram_token_bot import telegram_token_bot
from routes.order_routes import router as order_router
from routes.margin_routes import router as margin_router
from routes.margin_routes import router as margin_router
from routes.layout_routes import router as layout_router
from routes.daily_backup_routes import router as daily_backup_router
from routes.prochart_routes import router as prochart_router

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Decorative constants (pure Unicode — no markdown needed)                    #
# --------------------------------------------------------------------------- #
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


def _is_strategy2_enabled() -> bool:
    """Safe read of STRATEGY_2_ENABLED without a hard import-time dependency."""
    try:
        from core import config
        return bool(getattr(config, "STRATEGY_2_ENABLED", False))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Market holiday gate                                                          #
# --------------------------------------------------------------------------- #
def _holiday_gate_for_startup() -> dict[str, Any]:
    """Ask the holiday service whether today is a trading holiday.

    Returns a decision dict that is safe to log and serialise:

        {
          "date":           "YYYY-MM-DD",
          "status":         "holiday" | "not_holiday" | "error",
          "is_holiday":     bool,
          "should_run":     bool,   # True when the pipeline should run
          "holiday_count":  int,
          "description":    str | None,
          "error":          str | None,
          "cached":         bool,
        }

    Failure semantics: an ``error`` status is treated as **not a
    holiday** (``should_run=True``) so a provider outage never causes
    the pipeline to be silently skipped on a real trading day.
    """
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

    should_run = not is_holiday  # errors fall through to should_run=True

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
# Lifespan                                                                     #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    try:
        # ---- Holiday gate --------------------------------------------- #
        holiday_gate = _holiday_gate_for_startup()

        if not holiday_gate["should_run"]:
            # Trading holiday: skip the heavy pipeline (cleanup, backup,
            # OR, S1/S2 stages, feed start) but still start the
            # scheduler + Telegram token bot so that a token refresh
            # through Telegram keeps working on the holiday.
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
                    f"{'✓' if getattr(telegram_token_bot, 'enabled', False) else '✗'}\n"
                    f"{_THIN}\n"
                    f"⏭️ Strategy pipeline skipped — no market data expected.\n"
                    f"{_DIV}\n"
                    f"✅ Ready (holiday mode)"
                ),
                level="INFO",
            )
            yield
            return

        # ---- Unified refresh (Strategy 1 + Strategy 2) ----------------- #
        result = await asyncio.to_thread(
            _run_full_refresh_sync, "startup", loop
        )
        result["holiday_gate"] = holiday_gate

        registry_count = (result.get("registry") or {}).get("count", 0)
        or_summary = result.get("opening_range") or {}
        strategy_summary = result.get("strategy") or {}
        strategy2_summary = result.get("strategy2_stage3") or {}
        strategy2_order_summary = result.get("strategy2_stage4") or {}

        logger.info(
            "Application startup complete. feed_instruments=%d "
            "telegram_polling=%s strategy_indexes=%d tracker=%s "
            "s2_enabled=%s s2_tracker=%s s2_isolated=%s holiday=%s",
            registry_count,
            result.get("telegram_started"),
            len(strategy_summary.get("indexes", [])),
            result.get("tracker_started"),
            _is_strategy2_enabled(),
            result.get("strategy2_tracker_started"),
            bool(strategy2_summary.get("isolated")),
            holiday_gate.get("is_holiday"),
        )

        # ---- Startup Telegram ------------------------------------------ #
        s2_isolated = strategy2_summary.get("isolated")
        if s2_isolated:
            s2_isolated_line = (
                f"{s2_isolated.get('ticker')} "
                f"{s2_isolated.get('strike_price')} "
                f"{s2_isolated.get('strike_type')}"
            )
        else:
            s2_isolated_line = "none yet"

        holiday_line = (
            f"🏖️ Holiday:       "
            f"{holiday_gate.get('description') or 'yes'}\n"
            if holiday_gate.get("is_holiday")
            else f"🏖️ Holiday:       no\n"
        )

        startup_message = (
            f"{_DIV}\n"
            f"🚀 APPLICATION STARTED\n"
            f"{_DIV}\n"
            f"📊 Instruments:     {registry_count}\n"
            f"🎯 OR levels:       "
            f"{or_summary.get('calculated')}/{or_summary.get('total')}\n"
            f"{holiday_line}"
            f"📡 Live feed:       "
            f"{'✓' if result.get('feed_restarted') else '✗'}\n"
            f"⏰ Scheduler:       "
            f"{'✓' if result.get('scheduler_started') else '✗'}\n"
            f"🤖 Telegram bot:    "
            f"{'✓' if result.get('telegram_started') else '✗'}\n"
            f"{_THIN}\n"
            f"📊 STRATEGY 1\n"
            f"   📈 Indexes:      {len(strategy_summary.get('indexes', []))}\n"
            f"   🎯 Stage 3:      tracker="
            f"{'✓' if result.get('tracker_started') else '✗'}\n"
            f"{_THIN}\n"
            f"🔒 STRATEGY 2\n"
            f"   🎯 Stage 3:      tracker="
            f"{'✓' if result.get('strategy2_tracker_started') else '✗'}\n"
            f"   🔒 Isolated:     {s2_isolated_line}\n"
            f"   💰 Stage 4:      placed="
            f"{strategy2_order_summary.get('placed', 0)}\n"
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
                f"Shutting down live feed, trackers, scheduler and "
                f"Telegram bot…"
            ),
            level="INFO",
        )

        # Stop every background component in a safe order. Each stop is
        # isolated so one failure cannot prevent the rest from running.
        stop_targets = [
            ("Live feed", live_feed_service.stop),
            ("Strategy 1 tracker", strategy_live_tracker_service.stop),
            ("Strategy 2 tracker", strategy_2_live_tracker_service.stop),
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
app.include_router(admin_router)
app.include_router(folder_router)
app.include_router(opening_range_router)
app.include_router(strategy_router)
app.include_router(strategy_2_router)
app.include_router(dashboard_router)
app.include_router(upstox_router)
app.include_router(log_router)
app.include_router(market_holiday_router)
app.include_router(order_router)
app.include_router(margin_router)
app.include_router(margin_router)
app.include_router(daily_backup_router)

# near the other app.include_router(...) calls
app.include_router(layout_router)
app.include_router(prochart_router)