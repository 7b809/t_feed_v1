"""
core/lifespan.py

Owns the FastAPI startup / shutdown sequence.

Startup is token-gated:
  - Telegram is initialised first and receives a "starting" message.
  - Token health is checked (cache -> mongo -> profile).
  - If the token is invalid, feature steps are skipped, the telegram
    command bot still starts, and FastAPI yields so the token endpoints
    remain live.
  - If the token is valid, the normal pipeline runs.

Pipeline (valid-token path):
  1. Mongo connect
  2. Token load
  3. Token health gate
  4. Telegram bot start
  5. Token watchdog start
  6. Option storage init + hydrate
  7. Token refresh scheduler
  8. Streamers connect
  9. Index subscription
 10. Option contracts load
 11. Candles load (freshness-aware: missing/empty/old/non-success)
 12. Crossover compute (historic + intraday)
 13. Daily candle refresh scheduler
 14. Bulk option subscribe (startup-only, gated)

Hard refresh (triggered by /refresh, POST /api/instruments/refresh, or
after a successful token save):
  1. token reload + validate
  2. index subscription
  3. option contracts reload
  4. candles ensure (freshness-aware)
  5. crossovers recompute
  6. subscribe-all (enabled indexes + every option contract)
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from core.config import core_config, mongo_manager
from core.logger import get_logger
from telegram_app.manager import telegram_manager
from token_tasks import scheduler
from token_tasks.config import token_config
from token_tasks.health_check import check_token_health
from token_tasks.service import token_service
from token_tasks.token_watchdog import token_watchdog
from upstox_app.candle.candle_scheduler import candle_scheduler
from upstox_app.candle.candle_service import candle_service
from upstox_app.candle.crossover_service import crossover_service
from upstox_app.common.config import upstox_config
from upstox_app.option.option_service import hydrate_from_runtime, load_enabled_indexes
from upstox_app.option.option_storage import option_storage
from upstox_app.streamer.streamer_manager import (
    auto_connect_enabled,
    index_subscription_enabled,
    option_subscription_enabled,
    start_all,
    stop_all,
    subscribe_all_on_refresh,
    subscribe_enabled_indexes,
    subscribe_option_contracts,
)
from upstox_app.streamer.ws_manager import set_event_loop

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:150]}"


# ---------------------------------------------------------------------------
# Command handlers exposed to the telegram bot
# ---------------------------------------------------------------------------


def _handle_save_token(access_token: str) -> Dict[str, Any]:
    try:
        result = token_service.save_and_reload(access_token, source="telegram")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::save_token failed | step=save_and_reload | reason=%s",
            _short(exc),
        )
        return {"saved": False, "error": _short(exc)}
    return result


def _handle_status() -> Dict[str, Any]:
    health = check_token_health()
    return {
        "token_valid": health.get("valid"),
        "source": health.get("token_source"),
        "validation": health.get("validation_status"),
        "streamer_auto": auto_connect_enabled(),
    }


async def _run_hard_refresh(trigger: str) -> Dict[str, Any]:
    """
    Full pipeline refresh used by /refresh and post-save flow.

    Steps: token -> indexes -> contracts -> candles -> crossovers -> subscribe-all.
    Every step is isolated so a failure in one does not abort the rest.
    """
    telegram_manager.notify_hard_refresh_started(trigger=trigger)
    summary: Dict[str, Any] = {"trigger": trigger, "steps": {}}

    # 1) token reload + validate
    try:
        await asyncio.to_thread(token_service.load_token)
        health = await asyncio.to_thread(check_token_health)
        summary["steps"]["token"] = health.get("validation_status", "unknown")
        if not health.get("valid"):
            telegram_manager.notify_hard_refresh_failed(
                {"reason": "token invalid", "source": health.get("token_source")}
            )
            return summary
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh token step failed | reason=%s", _short(exc)
        )
        summary["steps"]["token"] = "error"
        telegram_manager.notify_hard_refresh_failed({"reason": _short(exc)})
        return summary

    # 2) index subscription
    try:
        res = await asyncio.to_thread(subscribe_enabled_indexes)
        summary["steps"]["indexes"] = res.get("subscribed_count", 0)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh index step failed | reason=%s", _short(exc)
        )
        summary["steps"]["indexes"] = "error"

    # 3) option contracts
    try:
        await asyncio.to_thread(load_enabled_indexes)
        summary["steps"]["contracts"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh contracts step failed | reason=%s", _short(exc)
        )
        summary["steps"]["contracts"] = "error"

    # 4) candles (freshness-aware: missing/empty/old/non-success)
    try:
        cres = await asyncio.to_thread(candle_service.ensure_all_enabled)
        t = cres.get("totals", {})
        summary["steps"][
            "candles"
        ] = f"{t.get('fetched', 0)}/{t.get('missing_before', 0)}"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh candles step failed | reason=%s", _short(exc)
        )
        summary["steps"]["candles"] = "error"

    # 5) crossovers recompute
    try:
        xres = await asyncio.to_thread(crossover_service.calculate_all_enabled)
        t = xres.get("totals", {})
        summary["steps"][
            "crossovers"
        ] = f"h_ok={t.get('historic_success', 0)} i_ok={t.get('intraday_success', 0)}"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh crossovers step failed | reason=%s", _short(exc)
        )
        summary["steps"]["crossovers"] = "error"

    # 6) subscribe-all: enabled indexes + every option contract
    try:
        sres = await asyncio.to_thread(subscribe_all_on_refresh)
        summary["steps"]["subscribe_all"] = "ok"
        logger.info("Hard refresh | subscribe-all | %s", sres)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh subscribe-all failed | reason=%s", _short(exc)
        )
        summary["steps"]["subscribe_all"] = "error"

    telegram_manager.notify_hard_refresh_done(summary)
    return summary


def _handle_refresh_sync(trigger: str) -> None:
    """
    Bot handler runs on the telegram thread, not the event loop. Schedule
    the async refresh onto the captured main loop.
    """
    ok = telegram_manager.schedule(_run_hard_refresh(trigger))
    if not ok:
        logger.error(
            "lifespan::_handle_refresh_sync failed | step=schedule | reason=main loop unavailable"
        )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(_app) -> AsyncIterator[None]:
    logger.info("=" * 60)
    logger.info("Starting %s v%s", core_config.APP_NAME, core_config.APP_VERSION)

    steps_done: list[str] = []
    steps_skipped: list[str] = []
    token_valid = False

    # ---- Telegram init (independent of FastAPI) ----
    telegram_manager.notify_project_starting(core_config.APP_VERSION)

    # 0) Capture main event loop + register with ws_manager
    running_loop = asyncio.get_running_loop()
    set_event_loop(running_loop)
    telegram_manager.set_main_loop(running_loop)

    # 1) MongoDB
    try:
        mongo_manager.connect()
        if mongo_manager.ping():
            logger.info("MongoDB connected successfully")
            steps_done.append("MongoDB connected")
        else:
            logger.error("MongoDB ping failed")
            steps_done.append("MongoDB connected (ping failed)")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::mongo connect failed | reason=%s", _short(exc))
        steps_done.append("MongoDB connect failed")

    # 2) Token load
    if token_config.LOAD_ON_STARTUP:
        try:
            await asyncio.to_thread(token_service.load_token)
            logger.info("Initial token load completed")
            steps_done.append("Token loaded")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::token load failed | reason=%s", _short(exc))
            steps_done.append("Token load failed")
    else:
        logger.info("Skipping startup token load")

    # 3) Token health gate
    try:
        health = await asyncio.to_thread(check_token_health)
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::token health failed | reason=%s", _short(exc))
        health = {
            "valid": False,
            "token_source": "none",
            "validation_status": "error",
            "error": _short(exc),
        }

    token_valid = bool(health.get("valid"))
    telegram_manager.mark_token_healthy(token_valid)
    telegram_manager.notify_token_health(health)

    # Wire the telegram command bot in both branches.
    telegram_manager.start_bot(
        save_token_fn=_handle_save_token,
        refresh_fn=_handle_refresh_sync,
        status_fn=_handle_status,
    )

    # ---- Token watchdog start (runs in both branches) ----
    try:
        token_watchdog.start(initial_valid=token_valid)
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::token watchdog start failed | reason=%s", _short(exc))

    if not token_valid:
        # ---------- BLOCKED STARTUP ----------
        steps_skipped.extend(
            [
                "Streamer connect",
                "Index subscription",
                "Option load",
                "Candle load",
                "Crossover compute",
                "Daily refresh scheduler",
                "Bulk option subscribe",
            ]
        )
        telegram_manager.notify_token_invalid(health)
        telegram_manager.notify_project_started(
            steps_done=steps_done, steps_skipped=steps_skipped, token_valid=False
        )
        logger.warning(
            "Startup blocked: token invalid | source=%s | validation=%s",
            health.get("token_source"),
            health.get("validation_status"),
        )
        logger.info("FastAPI is live. Save a token to resume features.")
        logger.info("=" * 60)

        try:
            yield
        finally:
            await _shutdown()
        return

    # ---------- NORMAL STARTUP ----------
    steps_done.append("Token valid")

    # Option runtime folder
    try:
        option_storage.ensure_dirs()
        steps_done.append("Option storage ready")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::option storage init failed | reason=%s", _short(exc))

    # Hydrate option cache
    try:
        hydrated = await asyncio.to_thread(hydrate_from_runtime)
        logger.info("Option cache hydrated from disk | indexes=%d", hydrated)
        steps_done.append(f"Option cache hydrated ({hydrated})")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::runtime hydration failed | reason=%s", _short(exc))

    # Token refresh scheduler
    try:
        scheduler.start_scheduler()
        steps_done.append("Token scheduler started")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::token scheduler failed | reason=%s", _short(exc))

    # Streamers
    if auto_connect_enabled():
        try:
            start_result = await asyncio.to_thread(start_all)
            logger.info("Streamer start result | %s", start_result)
            steps_done.append("Streamers connected")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::streamer start failed | reason=%s", _short(exc))

    # Index subscription
    if index_subscription_enabled():
        try:
            sub_result = await asyncio.to_thread(subscribe_enabled_indexes)
            logger.info("Index subscription result | %s", sub_result)
            steps_done.append("Indexes subscribed")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::index subscribe failed | reason=%s", _short(exc))

    # Option contracts
    if upstox_config.OPTIONS_LOAD_ON_STARTUP:
        try:
            result = await asyncio.to_thread(load_enabled_indexes)
            logger.info("Option load result | %s", result)
            steps_done.append("Options loaded")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::option load failed | reason=%s", _short(exc))
    else:
        steps_skipped.append("Option load (disabled)")

    # Candles (freshness-aware)
    if upstox_config.CANDLES_ENABLED and upstox_config.CANDLES_LOAD_ON_STARTUP:
        try:
            candle_summary = await asyncio.to_thread(candle_service.ensure_all_enabled)
            t = candle_summary.get("totals", {})
            logger.info(
                "Candles startup | fetched=%d failed=%d",
                t.get("fetched", 0),
                t.get("failed", 0),
            )
            steps_done.append(f"Candles loaded ({t.get('fetched', 0)})")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::candle load failed | reason=%s", _short(exc))
    else:
        steps_skipped.append("Candle load (disabled)")

    # Crossovers
    if bool(getattr(upstox_config, "CROSSOVER_CALC_ON_STARTUP", True)):
        try:
            cross = await asyncio.to_thread(crossover_service.calculate_all_enabled)
            t = cross.get("totals", {})
            logger.info(
                "Crossover startup | historic_ok=%d intraday_ok=%d",
                t.get("historic_success", 0),
                t.get("intraday_success", 0),
            )
            steps_done.append("Crossovers computed")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::crossover startup failed | reason=%s", _short(exc))

    # Daily candle refresh scheduler
    try:
        candle_scheduler.start()
        steps_done.append("Daily refresh scheduler started")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::candle scheduler failed | reason=%s", _short(exc))

    # Bulk option subscribe (startup-only, gated by OPTIONS_SUBSCRIBE_ALL_ON_STARTUP)
    if option_subscription_enabled():
        try:
            sub_result = await asyncio.to_thread(subscribe_option_contracts)
            logger.info("Option subscription result | %s", sub_result)
            steps_done.append("Option keys subscribed")
        except Exception as exc:  # noqa: BLE001
            logger.error("lifespan::option subscribe failed | reason=%s", _short(exc))
    else:
        steps_skipped.append("Option bulk-subscribe (disabled)")

    telegram_manager.notify_project_started(
        steps_done=steps_done, steps_skipped=steps_skipped, token_valid=True
    )
    logger.info("Startup complete — app is ready")
    logger.info("=" * 60)

    try:
        yield
    finally:
        await _shutdown()


async def _shutdown() -> None:
    logger.info("Shutting down application...")
    telegram_manager.notify_project_stopping()

    try:
        await token_watchdog.stop()
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::token watchdog stop failed | reason=%s", _short(exc))

    try:
        telegram_manager.stop_bot()
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::telegram bot stop failed | reason=%s", _short(exc))

    try:
        await candle_scheduler.stop()
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::candle scheduler stop failed | reason=%s", _short(exc))

    try:
        stop_result = await asyncio.to_thread(stop_all)
        logger.info("Streamers stopped | %s", stop_result)
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::streamer stop failed | reason=%s", _short(exc))

    try:
        await scheduler.stop_scheduler()
        logger.info("Token refresh scheduler stopped")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::token scheduler stop failed | reason=%s", _short(exc))

    try:
        mongo_manager.close()
        logger.info("MongoDB connection closed")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::mongo close failed | reason=%s", _short(exc))

    logger.info("Shutdown complete")
