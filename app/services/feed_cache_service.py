import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger("uvicorn")

# ==========================================================
# Full Tick Cache
# ==========================================================
#
# Stores latest FULL Upstox feed packet
# per instrument.
#
# Example:
#
# {
#     "NSE_FO|63935": {
#         "instrument_key": "NSE_FO|63935",
#         "timestamp": "...",
#         "feed": {...}
#     }
# }
#
# ==========================================================

live_feed_cache: Dict[str, Dict[str, Any]] = {}

# ==========================================================
# Candle Caches
# ==========================================================

candle_1m_cache: Dict[str, Dict[str, Any]] = {}

candle_3m_cache: Dict[str, Dict[str, Any]] = {}

candle_5m_cache: Dict[str, Dict[str, Any]] = {}

# ==========================================================
# Cache Helpers
# ==========================================================


def update_live_feed(
    instrument_key: str,
    feed: Dict[str, Any],
    current_ts: str | None = None,
):
    """
    Store latest full feed packet.
    """

    live_feed_cache[instrument_key] = {
        "instrument_key": instrument_key,
        "timestamp": current_ts,
        "received_at": datetime.now().isoformat(),
        "feed": feed,
    }


def update_candle(
    instrument_key: str,
    interval: int,
    candle: Dict[str, Any],
):
    """
    Store latest completed candle.
    """

    if interval == 1:

        candle_1m_cache[instrument_key] = {
            "instrument_key": instrument_key,
            "interval": 1,
            "updated_at": (datetime.now().isoformat()),
            "candle": candle,
        }

    elif interval == 3:

        candle_3m_cache[instrument_key] = {
            "instrument_key": instrument_key,
            "interval": 3,
            "updated_at": (datetime.now().isoformat()),
            "candle": candle,
        }

    elif interval == 5:

        candle_5m_cache[instrument_key] = {
            "instrument_key": instrument_key,
            "interval": 5,
            "updated_at": (datetime.now().isoformat()),
            "candle": candle,
        }


def get_live_feed(
    instrument_key: str,
) -> Dict[str, Any] | None:
    """
    Get live feed for instrument.
    """

    return live_feed_cache.get(instrument_key)


def get_candle_cache(
    interval: int,
):
    """
    Returns requested candle cache.
    """

    if interval == 1:
        return candle_1m_cache

    if interval == 3:
        return candle_3m_cache

    if interval == 5:
        return candle_5m_cache

    return None


def clear_live_feeds():
    """
    Clear tick cache.
    """

    live_feed_cache.clear()

    logger.info("Live feed cache cleared.")


def clear_candle_caches():
    """
    Clear all candle caches.
    """

    candle_1m_cache.clear()
    candle_3m_cache.clear()
    candle_5m_cache.clear()

    logger.info("Candle caches cleared.")


def clear_all_feed_caches():
    """
    Clear every feed cache.
    """

    clear_live_feeds()

    clear_candle_caches()

    logger.info("All feed caches cleared.")


def get_cache_stats():
    """
    Cache diagnostics.
    """

    return {
        "live_feeds": len(live_feed_cache),
        "candle_1m": len(candle_1m_cache),
        "candle_3m": len(candle_3m_cache),
        "candle_5m": len(candle_5m_cache),
        "timestamp": (datetime.now().isoformat()),
    }
