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
 15. EMA app: wire cross listener + attach tick sink + start session scheduler
     (initial backfill only runs when we are inside the trading session)
 16. Isolation layer: register metadata + hook base-engine listeners +
     start per-index winner selection (session-gated)

Hard refresh (triggered by /refresh, POST /api/instruments/refresh, or
after a successful token save):
  1. token reload + validate
  2. index subscription
  3. option contracts reload
  4. candles ensure (freshness-aware)
  5. crossovers recompute
  6. subscribe-all (enabled indexes + every option contract)
  7. EMA app: ensure started + backfill EMA state from disk (session-gated)
  8. Isolation layer: re-register instrument metadata for the new chain

The EMA app is a live 9/21 crossover service that consumes market-streamer
ticks, aggregates them into per-minute candles, detects crosses on candle
close, persists only the crosses to data/runtime/<INDEX>/<strike>_<TYPE>/
intraday_cross.json, and fans them out to WebSocket clients with rich
client-side filtering (underlying / strike / option_type / expiry / key).

The Isolation layer sits on top of the EMA app. It consumes the finalized
candle stream to detect opening-range touches, picks one option contract
per enabled index per day (the "isolated" instrument), and for that
instrument only, enriches its EMA crosses into a full alert payload
(order suggestion + budget-filtered shortlist), broadcasts them over a
dedicated WebSocket frame, places orders per the config flag, and
persists each alert to MongoDB keyed by YYYY-MM-DD with HH_MM_SS entries.
"""

import asyncio
import importlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

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

# ---- EMA app ---------------------------------------------------------------
from ema_app.scheduler import (
    start_scheduler as start_ema_scheduler,
    stop_scheduler as stop_ema_scheduler,
)
from ema_app.service import ema_service, is_inside_session
from ema_app.ws_manager import ema_ws_manager

# ---- Isolation layer -------------------------------------------------------
from ema_app.isolation.service import isolation_service
from ema_app.isolation.order_storage import isolated_order_storage
from ema_app.isolation.state import isolation_store

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:150]}"


# ---------------------------------------------------------------------------
# EMA app wiring helpers
# ---------------------------------------------------------------------------

# Module-level flag so `_ensure_ema_app_started()` is idempotent across
# startup + any number of hard refreshes.
_ema_app_started: bool = False

# Module-level flag so `_ensure_isolation_started()` is idempotent too.
_isolation_started: bool = False


def _try_attach_sink_on(target: Any, label: str, sink) -> bool:
    """
    Try to register `sink` on `target` using any of the common method names.
    `target` may be a module, a class instance, or a class.

    Returns True on the first successful registration.
    """
    candidates = (
        "register_tick_listener",
        "add_tick_listener",
        "set_tick_listener",
        "register_tick",
        "register_listener",
        "add_listener",
        "on_tick",
    )
    for attr in candidates:
        fn = getattr(target, attr, None)
        if not callable(fn):
            continue
        try:
            fn(sink)
            logger.info("EMA tick sink attached | source=%s.%s", label, attr)
            return True
        except Exception as exc:
            logger.debug("%s.%s failed: %s", label, attr, exc)
    return False


def _attach_ema_tick_sink() -> bool:
    """
    Attach `ema_service.ingest_tick` to the market streamer.

    Probes multiple entry points in this order:
      1. upstox_app.streamer.streamer_manager as a MODULE (top-level function)
      2. A `streamer_manager` singleton inside that module (if exposed)
      3. upstox_app.market.market_streamer as a MODULE (top-level function)
      4. A `market_streamer` singleton inside that module (if exposed)

    Returns True if a hook was successfully registered.
    """
    sink = ema_service.ingest_tick

    # ---- 1. streamer_manager module + optional singleton ------------------
    try:
        module = importlib.import_module("upstox_app.streamer.streamer_manager")

        if _try_attach_sink_on(module, "streamer_manager(module)", sink):
            return True

        singleton = getattr(module, "streamer_manager", None)
        if singleton is not None:
            if _try_attach_sink_on(singleton, "streamer_manager.singleton", sink):
                return True
    except Exception as exc:
        logger.debug("streamer_manager module probe failed: %s", exc)

    # ---- 2. market_streamer module + optional singleton -------------------
    try:
        module = importlib.import_module("upstox_app.market.market_streamer")

        if _try_attach_sink_on(module, "market_streamer(module)", sink):
            return True

        singleton = getattr(module, "market_streamer", None)
        if singleton is not None:
            if _try_attach_sink_on(singleton, "market_streamer.singleton", sink):
                return True
    except Exception as exc:
        logger.debug("market_streamer module probe failed: %s", exc)

    return False


async def _ema_backfill_all() -> int:
    """
    Rebuild EMA state for every tracked instrument from the on-disk candle
    series + today's intraday. Called on startup and after every hard
    refresh — but ONLY when inside the trading session.
    """
    keys = ema_service.instrument_keys()
    if not keys:
        return 0

    logger.info("EMA app: backfilling %d instruments", len(keys))

    done = 0
    for key in keys:
        try:
            n = ema_service.backfill_instrument(key)
            if n:
                done += 1
        except Exception as exc:
            logger.warning("EMA backfill %s failed: %s", key, exc)

    logger.info("EMA app: backfill done | instruments=%d", done)
    return done


async def _ensure_ema_app_started() -> None:
    """
    Idempotently wire the EMA app:

      1. Register the cross listener so crosses broadcast through the WS hub
      2. Attach the tick sink to the market streamer (best-effort)
      3. Start the session scheduler (09:14 → 15:30 IST)
    """
    global _ema_app_started
    if _ema_app_started:
        return

    # ---- 1. cross listener → WebSocket broadcast ------------------------
    try:
        loop = asyncio.get_running_loop()
        ema_service.register_cross_listener(
            lambda cross: ema_ws_manager.broadcast_threadsafe(cross, loop)
        )
    except Exception as exc:
        logger.warning(
            "EMA cross listener registration failed | reason=%s", _short(exc)
        )

    # ---- 2. tick sink ---------------------------------------------------
    try:
        attached = _attach_ema_tick_sink()
        ema_service.mark_streamer_attached(attached)
        if not attached:
            logger.warning(
                "EMA tick sink NOT attached — no live ticks will flow. "
                "Add a top-level register_tick_listener(fn) in "
                "upstox_app/streamer/streamer_manager.py and call every "
                "registered listener from the SDK's on_message callback."
            )
    except Exception as exc:
        logger.warning("EMA tick sink attach failed | reason=%s", _short(exc))

    # ---- 3. session scheduler ------------------------------------------
    try:
        await start_ema_scheduler()
        _ema_app_started = True
        logger.info("EMA app wired and scheduler started")
    except Exception as exc:
        logger.error("EMA scheduler start failed | reason=%s", _short(exc))


async def _stop_ema_app() -> None:
    """Stop the EMA session scheduler and reset the start flag."""
    global _ema_app_started
    try:
        await stop_ema_scheduler()
        _ema_app_started = False
        logger.info("EMA app scheduler stopped")
    except Exception as exc:
        logger.error("EMA scheduler stop failed | reason=%s", _short(exc))


# ---------------------------------------------------------------------------
# Streamer health diagnostics
# ---------------------------------------------------------------------------


def _diagnose_streamer_health() -> None:
    """
    Report the health of the upstream market streamer AND the tick flow.

    >>> FIX: previous version looked for a symbol named `streamer_manager`
    inside upstox_app.streamer.streamer_manager, which doesn't exist — so
    `connected` was always None and every run printed a false "not
    connected" warning during market hours. This version uses the module-
    level `market_streamer_status()` accessor (which is what the module
    actually exports) and also surfaces tick-flow statistics so the log
    tells you at a glance whether ticks are arriving.

    Does not raise.
    """
    try:
        from upstox_app.streamer import streamer_manager as sm_mod  # type: ignore
    except Exception as exc:
        logger.warning(
            "Streamer health: cannot import streamer_manager (%s)", _short(exc)
        )
        return

    in_session = False
    try:
        in_session = bool(is_inside_session())
    except Exception:
        in_session = False

    # ---- Streamer status -------------------------------------------------
    status: Optional[Dict[str, Any]] = None
    for fn_name in ("market_streamer_status", "status_all", "get_status"):
        fn = getattr(sm_mod, fn_name, None)
        if callable(fn):
            try:
                status = fn()
                break
            except Exception as exc:
                logger.debug("Streamer health: %s failed | %s", fn_name, _short(exc))

    connected: Optional[bool] = None
    if isinstance(status, dict):
        connected = status.get("connected")
        if connected is None and isinstance(status.get("market"), dict):
            connected = status["market"].get("connected")

    if connected is True:
        logger.info("Streamer health | connected=True | session=%s", in_session)
    elif not in_session:
        logger.info(
            "Streamer health | connected=%s | session=False (outside market hours)",
            connected,
        )
    else:
        logger.warning(
            "STREAMER NOT CONNECTED DURING MARKET HOURS | connected=%s | "
            "check UPSTOX_AUTO_CONNECT_ON_STARTUP, UPSTOX_CONNECT_TIMEOUT_SEC, "
            "token validity, and upstox_app/streamer/streamer_manager.start_all()",
            connected,
        )

    # ---- Tick flow -------------------------------------------------------
    stats: Optional[Dict[str, Any]] = None
    try:
        getter = getattr(sm_mod, "get_tick_stats", None)
        if callable(getter):
            stats = getter()
    except Exception:
        stats = None

    if isinstance(stats, dict):
        total = stats.get("total_ticks", 0)
        keys_n = stats.get("keys_seen_count", 0)
        last_age = stats.get("last_tick_age_sec")
        listeners_n = stats.get("listeners", 0)
        log_enabled = stats.get("tick_log_enabled", False)
        log_file = stats.get("tick_log_file")

        # If the split log is on, the tick-flow line goes to that file
        # automatically (via _tick_log inside streamer_manager). We only
        # emit here as a top-level boot summary so the operator sees it
        # on the main log once, regardless of the split.
        if total == 0 and in_session:
            logger.warning(
                "TICK FLOW: 0 ticks received during market hours | "
                "listeners=%d | likely cause: market_streamer.on_message "
                "does not call streamer_manager._fanout_tick(msg)",
                listeners_n,
            )
        else:
            logger.info(
                "Tick flow | total=%d | keys_seen=%d | listeners=%d | last_age=%s%s",
                total, keys_n, listeners_n,
                f"{last_age:.1f}s" if last_age is not None else "never",
                f" | detail_log={log_file}" if log_enabled else "",
            )


# ---------------------------------------------------------------------------
# Isolation layer wiring helpers
# ---------------------------------------------------------------------------


def _load_instruments_for_isolation() -> list:
    """
    Load the universe of option contracts that the isolation layer will
    watch for opening-range touches. Uses the same sources as the EMA
    instrument loader, so they stay in lockstep.
    """
    return _load_instruments_for_ema()


async def _ensure_isolation_started() -> None:
    """
    Idempotently wire the isolation layer on top of the EMA app:

      1. Register the contract metadata cache (strike / type / expiry /
         lot_size) for every tracked instrument.
      2. Hook the base engine's cross listener so crosses on the current
         isolated instrument reach `isolation_service.on_cross`.
      3. Hook a broadcast listener so isolated alerts reach the WS hub.
      4. Call `isolation_service.start(ema_service)` which registers a
         candle listener and loads any persisted isolation state for today.

    Safe to call multiple times; only the first call has an effect.
    """
    global _isolation_started
    if _isolation_started:
        return

    # ---- 1. metadata ----------------------------------------------------
    try:
        instruments = _load_instruments_for_isolation()
        if instruments:
            isolation_service.register_instrument_metadata(instruments)
    except Exception as exc:
        logger.warning("Isolation: metadata registration failed: %s", _short(exc))

    # ---- 2. cross listener ---------------------------------------------
    # NOTE: this REPLACES the base engine's cross listener with a chain
    # that notifies the WS hub AND the isolation service. Both must fire.
    try:
        loop = asyncio.get_running_loop()

        def _cross_fanout(cross: Dict[str, Any]) -> None:
            # 1) Broadcast every cross through the base WS hub
            try:
                ema_ws_manager.broadcast_threadsafe(cross, loop)
            except Exception:
                pass

            # 2) Feed the isolation layer (it filters internally)
            try:
                isolation_service.on_cross(cross)
            except Exception as exc:
                logger.warning("Isolation cross handler failed: %s", exc)

        ema_service.register_cross_listener(_cross_fanout)
        logger.info("Isolation: cross fanout registered on EMA engine")
    except Exception as exc:
        logger.warning("Isolation: cross fanout registration failed: %s", _short(exc))

    # ---- 3. broadcast listener for isolated alerts ----------------------
    try:
        loop = asyncio.get_running_loop()

        def _broadcast_isolated(payload: Dict[str, Any]) -> None:
            try:
                ema_ws_manager.broadcast_threadsafe(
                    {"type": "isolated_ema_alert", **payload}, loop
                )
            except Exception as exc:
                logger.warning("Isolation broadcast failed: %s", exc)

        isolation_service.register_broadcast_listener(_broadcast_isolated)
        logger.info("Isolation: broadcast listener registered")
    except Exception as exc:
        logger.warning("Isolation: broadcast listener failed: %s", _short(exc))

    # ---- 4. start (hooks candle listener + loads persisted state) -------
    try:
        isolation_service.start(ema_service)
        _isolation_started = True
        logger.info("Isolation layer wired and started")
    except Exception as exc:
        logger.error("Isolation layer start failed | reason=%s", _short(exc))


async def _stop_isolation() -> None:
    """Stop the isolation layer, flush state, close Mongo."""
    global _isolation_started
    try:
        isolation_service.stop()
    except Exception as exc:
        logger.error("Isolation stop failed | reason=%s", _short(exc))

    try:
        isolation_store.save_all()
    except Exception as exc:
        logger.warning("Isolation store save failed | reason=%s", _short(exc))

    try:
        isolated_order_storage.close()
    except Exception as exc:
        logger.warning("Isolation order storage close failed | reason=%s", _short(exc))

    _isolation_started = False
    logger.info("Isolation layer stopped")


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

    Steps: token -> indexes -> contracts -> candles -> crossovers -> subscribe-all
           -> EMA app (ensure started + backfill, session-gated)
           -> Isolation (re-register metadata).

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

    # 7) EMA app — ensure started, register instruments, session-gated backfill
    try:
        await _ensure_ema_app_started()

        # Re-attach the tick sink after a hard refresh. The SDK streamer may
        # have been rebuilt when the token changed, so the previous listener
        # registration can be stale.
        try:
            _attach_ema_tick_sink()
        except Exception as exc:
            logger.debug("EMA tick sink re-attach failed: %s", _short(exc))

        try:
            instruments = _load_instruments_for_ema()
            if instruments:
                ema_service.register_instruments(instruments)
        except Exception as exc:
            logger.warning("EMA instrument registration failed: %s", _short(exc))

        if is_inside_session():
            backfilled = await _ema_backfill_all()
            summary["steps"]["ema_app"] = f"backfilled={backfilled}"
        else:
            summary["steps"]["ema_app"] = "skipped (outside session)"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh ema_app step failed | reason=%s", _short(exc)
        )
        summary["steps"]["ema_app"] = "error"

    # 8) Isolation — ensure started, then re-register metadata for the new chain
    try:
        await _ensure_isolation_started()

        try:
            instruments = _load_instruments_for_isolation()
            if instruments:
                isolation_service.register_instrument_metadata(instruments)
                summary["steps"]["isolation"] = f"metadata={len(instruments)}"
            else:
                summary["steps"]["isolation"] = "no instruments"
        except Exception as exc:
            logger.warning("Isolation metadata re-register failed: %s", _short(exc))
            summary["steps"]["isolation"] = "metadata error"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "lifespan::hard_refresh isolation step failed | reason=%s", _short(exc)
        )
        summary["steps"]["isolation"] = "error"

    # After a hard refresh, re-check streamer + tick health.
    try:
        _diagnose_streamer_health()
    except Exception:
        pass

    telegram_manager.notify_hard_refresh_done(summary)
    return summary


def _load_instruments_for_ema() -> list:
    """
    Load the full instrument universe for the EMA app.

    Tries the web helper first (which merges every enabled index), then the
    option service, then runtime snapshots.
    """
    # 1) Web helper
    try:
        from web.service import load_all_option_instruments  # type: ignore

        instruments = load_all_option_instruments()
        if instruments:
            return instruments
    except Exception:
        pass

    # 2) Option service — try new API first, then old.
    out: list = []
    try:
        from upstox_app.common.config import MAIN_INDEXES  # type: ignore
        from upstox_app.option.option_service import option_service  # type: ignore

        indexes = (
            list(MAIN_INDEXES.keys())
            if isinstance(MAIN_INDEXES, dict)
            else list(MAIN_INDEXES or [])
        )
        for idx in indexes:
            idx_name = str(idx).upper()
            contracts = None

            getter = getattr(option_service, "get_contracts_for_index", None)
            if callable(getter):
                try:
                    contracts = getter(idx_name)
                except Exception as exc:
                    logger.debug(
                        "get_contracts_for_index(%s) failed: %s", idx_name, exc
                    )

            if not contracts:
                getter = getattr(option_service, "get_contracts", None)
                if callable(getter):
                    try:
                        contracts = getter(idx_name)
                    except Exception as exc:
                        logger.debug(
                            "get_contracts(%s) failed: %s", idx_name, exc
                        )

            if contracts:
                out.extend(contracts)
    except Exception:
        pass

    return out


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
                "EMA app scheduler",
                "Isolation layer",
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
            steps_skipped.append("Streamer connect (error)")
    else:
        logger.warning(
            "Streamer auto-connect DISABLED (UPSTOX_AUTO_CONNECT_ON_STARTUP=false) — "
            "no upstream connection will be established at startup. "
            "Subscriptions will queue until connect() is called explicitly."
        )
        steps_skipped.append("Streamer connect (disabled)")

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

    # EMA app — register instruments, wire listeners, start scheduler.
    try:
        instruments = _load_instruments_for_ema()
        if instruments:
            ema_service.register_instruments(instruments)
            logger.info("EMA app: registered %d instruments", len(instruments))
        await _ensure_ema_app_started()

        if is_inside_session():
            try:
                backfilled = await _ema_backfill_all()
                if backfilled:
                    logger.info(
                        "EMA app: initial backfill done | instruments=%d",
                        backfilled,
                    )
            except Exception as exc:
                logger.warning("EMA initial backfill failed: %s", _short(exc))
        else:
            logger.info(
                "EMA app: skipping initial backfill — outside trading session"
            )

        steps_done.append("EMA app started")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::ema app startup failed | reason=%s", _short(exc))
        steps_skipped.append("EMA app (error)")

    # Isolation layer — sits on top of the EMA app. Registers metadata,
    # hooks the base engine's cross + candle listeners, wires the WS hub
    # for isolated alerts, and rehydrates any persisted per-index state.
    try:
        await _ensure_isolation_started()
        steps_done.append("Isolation layer started")
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::isolation startup failed | reason=%s", _short(exc))
        steps_skipped.append("Isolation layer (error)")

    # Report streamer + tick health right before declaring startup complete.
    try:
        _diagnose_streamer_health()
    except Exception:
        pass

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

    # ---- Isolation layer first (flush state, close order-storage Mongo) --
    try:
        await _stop_isolation()
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::isolation stop failed | reason=%s", _short(exc))

    # ---- EMA app next (before streamers stop, so it stops cleanly) ------
    try:
        await _stop_ema_app()
    except Exception as exc:  # noqa: BLE001
        logger.error("lifespan::ema app stop failed | reason=%s", _short(exc))

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