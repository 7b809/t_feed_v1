"""
upstox_app/streamer/streamer_manager.py

Lifecycle + subscription orchestrator for the market and portfolio
streamers.

Every helper that reaches into `option_service` or `market_hours` uses a
defensive resolver, because those modules have shipped with several
different public names over time. Nothing here raises on attribute drift.
"""
import importlib
import threading
from typing import Any, Dict, List

from core.config import core_config
from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.market.market_streamer import market_streamer
from upstox_app.portfolio.portfolio_streamer import portfolio_streamer

logger = get_logger(__name__)

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _cfg(name: str, default: Any) -> Any:
    return getattr(upstox_config, name, default)


def _flag(name: str, default: bool) -> bool:
    return bool(_cfg(name, default))


def _index_subscription_mode() -> str:
    mode = _cfg("UPSTOX_INDEX_SUBSCRIPTION_MODE", None)
    if not mode:
        mode = _cfg("UPSTOX_DEFAULT_SUBSCRIPTION_MODE", "ltpc")
    return str(mode) if mode else "ltpc"


def _option_subscription_mode() -> str:
    return str(_cfg("OPTIONS_SUBSCRIBE_MODE", "ltpc"))


def _safe_call(obj: Any, method: str, default: Any = None) -> Any:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return default
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "streamer_manager::_safe_call failed | method=%s | reason=%s",
            method, _short(exc),
        )
        return default


# ---- market_hours probing --------------------------------------------------

# Candidate names for "human-readable market state".
# `market_state_text` is the current name in
# upstox_app/common/market_hours.py. The others are kept as fallbacks
# for older or alternative implementations of that module.
_MARKET_HOURS_CANDIDATE_FUNCS = (
    "market_state_text",       # current
    "market_state",
    "get_market_state",
    "current_market_state",
    "session_state",
    "state",
)

# Candidate names for "is the market open right now?".
_MARKET_OPEN_CANDIDATE_FUNCS = (
    "is_market_open",          # current
    "market_open",
    "is_open",
    "session_open",
)


def _import_market_hours():
    try:
        return importlib.import_module("upstox_app.common.market_hours")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "streamer_manager | market_hours import failed | reason=%s",
            _short(exc),
        )
        return None


def _market_state_str() -> str:
    module = _import_market_hours()
    if module is None:
        return "unknown"
    for name in _MARKET_HOURS_CANDIDATE_FUNCS:
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "streamer_manager | market_hours.%s failed | reason=%s",
                    name, _short(exc),
                )
    return "unknown"


def _is_market_open() -> bool:
    module = _import_market_hours()
    if module is None:
        return True
    for name in _MARKET_OPEN_CANDIDATE_FUNCS:
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "streamer_manager | market_hours.%s failed | reason=%s",
                    name, _short(exc),
                )
    return True


# ---- option_service probing ------------------------------------------------


def _collect_option_keys() -> List[str]:
    """
    Collect every option instrument key currently known to the app.

    Order:
      1. A callable on upstox_app.option.option_service:
         get_all_contracts, get_contracts, list_contracts, get_options, ...
      2. A module-level cache dict: options_cache, _cache, CACHE, with
         optional `by_index` sub-dict.
      3. Nothing -> returns [].

    Deduplicates while preserving order. Never raises.
    """
    seen = set()
    keys: List[str] = []

    try:
        module = importlib.import_module("upstox_app.option.option_service")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "streamer_manager::_collect_option_keys failed | step=import | reason=%s",
            _short(exc),
        )
        return []

    contracts: List[Dict[str, Any]] = []

    # 1) accessor functions
    for fn_name in (
        "get_all_contracts",
        "get_contracts",
        "list_contracts",
        "get_cached_contracts",
        "get_index_contracts",
        "get_options",
    ):
        fn = getattr(module, fn_name, None)
        if not callable(fn):
            continue
        try:
            # Try no-arg call first (get_all_contracts), then per-index.
            try:
                data = fn()
                if isinstance(data, list) and data:
                    contracts = list(data)
                    break
            except TypeError:
                pass
            # Per-index variant
            aggregated: List[Dict[str, Any]] = []
            for index_name, meta in core_config.MAIN_INDEXES.items():
                if not meta.get("enabled"):
                    continue
                try:
                    rows = fn(index_name)
                    if isinstance(rows, list):
                        aggregated.extend(rows)
                except Exception:  # noqa: BLE001
                    continue
            if aggregated:
                contracts = aggregated
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "streamer_manager | option_service.%s failed | reason=%s",
                fn_name, _short(exc),
            )

    # 2) module-level cache dict
    if not contracts:
        for cache_name in ("options_cache", "_cache", "CACHE"):
            cache = getattr(module, cache_name, None)
            if not isinstance(cache, dict):
                continue
            by_index = cache.get("by_index") if isinstance(cache.get("by_index"), dict) else cache
            for _, entry in by_index.items():
                if isinstance(entry, dict):
                    contracts.extend(entry.get("contracts") or [])

    for c in contracts:
        key = c.get("instrument_key") if isinstance(c, dict) else None
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)

    return keys


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


def auto_connect_enabled() -> bool:
    return _flag("UPSTOX_AUTO_CONNECT_ON_STARTUP", False)


def index_subscription_enabled() -> bool:
    return _flag("UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP", True)


def option_subscription_enabled() -> bool:
    return _flag("OPTIONS_SUBSCRIBE_ALL_ON_STARTUP", False)


# ---------------------------------------------------------------------------
# index resolution
# ---------------------------------------------------------------------------


def get_enabled_indexes() -> List[str]:
    names: List[str] = []
    try:
        for name, meta in core_config.MAIN_INDEXES.items():
            if meta.get("enabled"):
                names.append(name.upper())
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "streamer_manager::get_enabled_indexes failed | step=read config | reason=%s",
            _short(exc),
        )
        return []
    logger.info("Enabled indexes resolved | names=%s", names)
    return names


def _index_instrument_keys() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    try:
        for name, meta in core_config.MAIN_INDEXES.items():
            if not meta.get("enabled"):
                continue
            key = meta.get("instrument_key")
            if key:
                mapping[name.upper()] = str(key)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "streamer_manager::_index_instrument_keys failed | step=read config | reason=%s",
            _short(exc),
        )
    return mapping


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def start_all() -> Dict[str, Any]:
    result: Dict[str, Any] = {"market": False, "portfolio": False}
    with _lock:
        logger.info("Starting all streamers")
        try:
            result["market"] = bool(market_streamer.connect())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "streamer_manager::start_all failed | step=market.connect | reason=%s",
                _short(exc),
            )
        try:
            result["portfolio"] = bool(portfolio_streamer.connect())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "streamer_manager::start_all failed | step=portfolio.connect | reason=%s",
                _short(exc),
            )
    return result


def stop_all() -> Dict[str, Any]:
    result: Dict[str, Any] = {"market": False, "portfolio": False}
    with _lock:
        try:
            result["market"] = bool(market_streamer.disconnect())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "streamer_manager::stop_all failed | step=market.disconnect | reason=%s",
                _short(exc),
            )
        try:
            result["portfolio"] = bool(portfolio_streamer.disconnect())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "streamer_manager::stop_all failed | step=portfolio.disconnect | reason=%s",
                _short(exc),
            )
    return result


def status_all() -> Dict[str, Any]:
    with _lock:
        market_connected = bool(
            _safe_call(market_streamer, "is_connected", False)
            or _safe_call(market_streamer, "connected", False)
        )
        market_subs = _safe_call(market_streamer, "subscription_count", None)
        if market_subs is None:
            subs_map = _safe_call(market_streamer, "subscriptions", None)
            if isinstance(subs_map, dict):
                market_subs = len(subs_map)
        market_buffer = _safe_call(market_streamer, "buffer_size", None)

        portfolio_connected = bool(
            _safe_call(portfolio_streamer, "is_connected", False)
            or _safe_call(portfolio_streamer, "connected", False)
        )
        portfolio_buffer = _safe_call(portfolio_streamer, "buffer_size", None)

        return {
            "market": {
                "connected": market_connected,
                "subscriptions": market_subs,
                "buffer_size": market_buffer,
                "mode": _index_subscription_mode(),
            },
            "portfolio": {
                "connected": portfolio_connected,
                "buffer_size": portfolio_buffer,
            },
            "auto_connect": auto_connect_enabled(),
            "index_subscription": index_subscription_enabled(),
            "option_subscription": option_subscription_enabled(),
        }


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------


def subscribe_enabled_indexes() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "mode": None, "keys": [], "indexes": [],
        "market_open": None, "market_state": None,
        "connected": False, "deferred": False,
        "subscribed_count": 0, "applied": [], "skipped": [], "error": None,
    }

    mapping = _index_instrument_keys()
    if not mapping:
        result["error"] = "no enabled indexes"
        return result

    names = list(mapping.keys())
    keys = list(mapping.values())
    mode = _index_subscription_mode()
    market_open = _is_market_open()
    market_state = _market_state_str()

    result.update({
        "mode": mode, "keys": keys, "indexes": names,
        "market_open": market_open, "market_state": market_state,
    })

    logger.info(
        "Startup index subscription | indexes=%s | keys=%s | mode=%s | market_state=%s",
        names, keys, mode, market_state,
    )

    try:
        connected = bool(getattr(market_streamer, "is_connected", lambda: False)())
    except Exception:  # noqa: BLE001
        connected = False
    result["connected"] = connected

    try:
        applied, skipped = market_streamer.subscribe(keys, mode)
        result["applied"] = list(applied or [])
        result["skipped"] = list(skipped or [])
        result["subscribed_count"] = len(result["applied"]) + len(result["skipped"])
    except Exception as exc:  # noqa: BLE001
        result["error"] = _short(exc)
        logger.error(
            "streamer_manager::subscribe_enabled_indexes failed | step=subscribe | reason=%s",
            _short(exc),
        )
        return result

    if not market_open:
        result["deferred"] = True
        logger.warning(
            "Startup index subscription deferred — market closed | state=%s "
            "| registered=%d | mode=%s | will apply on next open",
            market_state, result["subscribed_count"], mode,
        )
    return result


def subscribe_option_contracts() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "mode": _option_subscription_mode(),
        "batch_size": int(_cfg("OPTIONS_SUBSCRIBE_BATCH_SIZE", 500)),
        "total_keys": 0, "batches": 0,
        "applied": 0, "skipped": 0, "errors": 0,
    }

    keys = _collect_option_keys()
    result["total_keys"] = len(keys)
    if not keys:
        logger.info("Option subscription | no keys to subscribe")
        return result

    mode = result["mode"]
    batch_size = max(1, result["batch_size"])
    logger.info(
        "Option subscription | keys=%d | batch_size=%d | mode=%s",
        len(keys), batch_size, mode,
    )

    for i in range(0, len(keys), batch_size):
        chunk = keys[i:i + batch_size]
        result["batches"] += 1
        try:
            applied, skipped = market_streamer.subscribe(chunk, mode)
            result["applied"] += len(applied or [])
            result["skipped"] += len(skipped or [])
        except Exception as exc:  # noqa: BLE001
            result["errors"] += 1
            logger.error(
                "streamer_manager::subscribe_option_contracts failed "
                "| step=subscribe batch | batch=%d | size=%d | reason=%s",
                result["batches"], len(chunk), _short(exc),
            )

    logger.info(
        "Option subscription result | total=%d | batches=%d | applied=%d "
        "| skipped=%d | errors=%d | mode=%s",
        result["total_keys"], result["batches"], result["applied"],
        result["skipped"], result["errors"], mode,
    )
    return result


def subscribe_all_on_refresh() -> Dict[str, Any]:
    result: Dict[str, Any] = {"indexes": None, "options": None}

    try:
        result["indexes"] = subscribe_enabled_indexes()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "streamer_manager::subscribe_all_on_refresh failed | step=indexes | reason=%s",
            _short(exc),
        )
        result["indexes"] = {"error": _short(exc)}

    try:
        result["options"] = subscribe_option_contracts()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "streamer_manager::subscribe_all_on_refresh failed | step=options | reason=%s",
            _short(exc),
        )
        result["options"] = {"error": _short(exc)}

    logger.info(
        "streamer_manager::subscribe_all_on_refresh | done | indexes=%s | options=%s",
        result["indexes"], result["options"],
    )
    return result