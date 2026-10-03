"""
upstox_app/streamer_manager.py

Coordinates both streamers, startup index subscriptions, and bulk
option-contract subscriptions.

Security:
    The access token is never logged or returned.
"""
import time
from typing import Any, Dict, List

from core.config import core_config
from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.common.market_hours import is_market_open, market_state_text
from upstox_app.market.market_streamer import market_streamer
from upstox_app.option.option_service import get_subscribable_option_keys
from upstox_app.portfolio.portfolio_streamer import portfolio_streamer

logger = get_logger(__name__)


# ── enabled-index helpers ────────────────────────────────────────
def get_enabled_indexes() -> Dict[str, Dict[str, Any]]:
    """Return the indexes marked `enabled=True` in core_config.MAIN_INDEXES."""
    indexes = core_config.MAIN_INDEXES or {}
    enabled = {
        name: cfg
        for name, cfg in indexes.items()
        if isinstance(cfg, dict) and cfg.get("enabled")
    }
    logger.info("Enabled indexes resolved | names=%s", list(enabled.keys()))
    return enabled


def get_enabled_index_keys() -> List[str]:
    """Return the instrument keys for all enabled indexes (order preserved)."""
    keys: List[str] = []
    for name, cfg in get_enabled_indexes().items():
        key = cfg.get("instrument_key")
        if key and str(key).strip():
            keys.append(str(key).strip())
        else:
            logger.warning("Enabled index has no instrument_key | name=%s", name)
    return keys


# ── lifecycle ────────────────────────────────────────────────────
def start_all() -> Dict[str, bool]:
    logger.info("Starting all streamers")
    return {
        "market": market_streamer.connect(),
        "portfolio": portfolio_streamer.connect(),
    }


def stop_all() -> Dict[str, bool]:
    logger.info("Stopping all streamers")
    return {
        "market": market_streamer.disconnect(),
        "portfolio": portfolio_streamer.disconnect(),
    }


def status_all() -> Dict[str, Dict]:
    return {
        "market": market_streamer.status(),
        "portfolio": portfolio_streamer.status(),
    }


def auto_connect_enabled() -> bool:
    return upstox_config.AUTO_CONNECT_ON_STARTUP


def index_subscription_enabled() -> bool:
    return upstox_config.SUBSCRIBE_INDEXES_ON_STARTUP


def option_subscription_enabled() -> bool:
    return upstox_config.OPTIONS_SUBSCRIBE_ALL_ON_STARTUP


# ── startup index subscription ───────────────────────────────────
def subscribe_enabled_indexes(mode: str | None = None) -> Dict[str, Any]:
    """Connect the market streamer and subscribe every enabled index key."""
    effective_mode = (mode or upstox_config.DEFAULT_SUBSCRIPTION_MODE or "full").strip().lower()
    market_open = is_market_open()
    market_text = market_state_text()

    enabled = get_enabled_indexes()
    index_names = list(enabled.keys())
    keys = get_enabled_index_keys()

    base: Dict[str, Any] = {
        "mode": effective_mode,
        "keys": keys,
        "indexes": index_names,
        "market_open": market_open,
        "market_state": market_text,
    }

    if not keys:
        logger.warning("Startup index subscription skipped — no enabled indexes")
        return {**base, "connected": market_streamer.status()["connected"],
                "deferred": False, "subscribed_count": 0,
                "applied": [], "skipped": [], "error": "no_enabled_indexes"}

    logger.info(
        "Startup index subscription | indexes=%s | keys=%s | mode=%s | market_state=%s",
        index_names, keys, effective_mode, market_text,
    )

    if not market_streamer.status()["connected"]:
        logger.info("Market streamer not connected — connecting for startup subscription")
        if not market_streamer.connect():
            logger.error("Market streamer failed to connect during startup subscription")
            return {**base, "connected": False, "deferred": False,
                    "subscribed_count": 0, "applied": [], "skipped": [],
                    "error": "streamer_connect_failed"}

    try:
        applied, skipped = market_streamer.subscribe(keys, effective_mode)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Startup index subscription failed: %s", exc)
        return {**base, "connected": market_streamer.status()["connected"],
                "deferred": False, "subscribed_count": 0,
                "applied": [], "skipped": [], "error": str(exc)}

    subscribed_count = len(applied)
    deferred = (not market_open) and subscribed_count > 0

    if deferred:
        logger.warning(
            "Startup index subscription deferred — market closed | "
            "state=%s | registered=%d | mode=%s | will apply on next open",
            market_text, subscribed_count, effective_mode,
        )
    else:
        logger.info(
            "Startup index subscription done | indexes=%s | applied=%d | skipped=%d | mode=%s",
            index_names, subscribed_count, len(skipped), effective_mode,
        )

    return {**base, "connected": market_streamer.status()["connected"],
            "deferred": deferred, "subscribed_count": subscribed_count,
            "applied": applied, "skipped": skipped, "error": None}


# ── startup option-contract subscription (bulk) ──────────────────
def subscribe_option_contracts() -> Dict[str, Any]:
    """
    Subscribe every cached CE/PE option key from enabled indexes to the
    market streamer, using OPTIONS_SUBSCRIBE_MODE.

    Chunks into batches of OPTIONS_SUBSCRIBE_BATCH_SIZE for safety.

    Returns a summary dict. Never raises — errors are captured per batch.
    """
    market_open = is_market_open()
    market_text = market_state_text()
    mode = (upstox_config.OPTIONS_SUBSCRIBE_MODE or "ltpc").strip().lower()
    batch_size = max(1, upstox_config.OPTIONS_SUBSCRIBE_BATCH_SIZE)

    keys, per_index = get_subscribable_option_keys()
    if not keys:
        logger.warning(
            "Option subscription skipped — no cached CE/PE contracts | per_index=%s",
            per_index,
        )
        return {
            "ok": True,
            "requested": 0,
            "applied": 0,
            "skipped": 0,
            "batches": 0,
            "mode": mode,
            "per_index": per_index,
            "market_open": market_open,
            "market_state": market_text,
            "error": "no_option_keys",
        }

    logger.info(
        "Startup option subscription | total_keys=%d | mode=%s | batch_size=%d | "
        "per_index=%s | market_state=%s",
        len(keys), mode, batch_size, per_index, market_text,
    )

    if not market_streamer.status()["connected"]:
        logger.info("Market streamer not connected — connecting for option subscription")
        if not market_streamer.connect():
            logger.error("Market streamer failed to connect during option subscription")
            return {
                "ok": False, "requested": len(keys), "applied": 0, "skipped": 0,
                "batches": 0, "mode": mode, "per_index": per_index,
                "market_open": market_open, "market_state": market_text,
                "error": "streamer_connect_failed",
            }

    total_applied = 0
    total_skipped = 0
    batches = 0
    errors: List[str] = []
    started = time.time()

    for i in range(0, len(keys), batch_size):
        chunk = keys[i:i + batch_size]
        batches += 1
        try:
            applied, skipped = market_streamer.subscribe(chunk, mode)
            total_applied += len(applied)
            total_skipped += len(skipped)
            logger.info(
                "Option subscribe batch done | batch=%d | offset=%d | size=%d | "
                "applied=%d | skipped=%d",
                batches, i, len(chunk), len(applied), len(skipped),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Option subscribe batch failed | batch=%d | err=%s", batches, exc)
            errors.append(f"batch {batches}: {exc}")

    elapsed = time.time() - started
    logger.info(
        "Startup option subscription done | total=%d | applied=%d | skipped=%d | "
        "batches=%d | elapsed=%.2fs | errors=%d",
        len(keys), total_applied, total_skipped, batches, elapsed, len(errors),
    )

    return {
        "ok": not errors,
        "requested": len(keys),
        "applied": total_applied,
        "skipped": total_skipped,
        "batches": batches,
        "elapsed_sec": round(elapsed, 2),
        "mode": mode,
        "per_index": per_index,
        "market_open": market_open,
        "market_state": market_text,
        "errors": errors,
    }