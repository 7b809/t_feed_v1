"""
upstox_app/streamer/streamer_manager.py

Lifecycle + subscription orchestrator for the market and portfolio
streamers.

Tick listener registry
----------------------
Any component that wants to receive raw ticks registers a callable via
`register_tick_listener(fn)`. The market streamer's on_message callback
must call `streamer_manager._fanout_tick(msg)` for those listeners to
fire.

Tick flow diagnostics
---------------------
Flag-driven instrumentation. By default they write to the main app
logger. Set `STREAMER_TICK_LOG_ENABLED=true` to route all tick-flow
diagnostics into a dedicated rotating file instead.

    STREAMER_DEBUG_TICKS                log every tick (very noisy)
    STREAMER_LOG_FIRST_TICK_PER_KEY     log the first tick per instrument
    STREAMER_TICK_STATS_ENABLED         periodic aggregate log
    STREAMER_TICK_STATS_INTERVAL_SEC    aggregate interval, default 60s

    STREAMER_TICK_LOG_ENABLED           send tick diagnostics to file
    STREAMER_TICK_LOG_FILE              default logs/streamer_test.log

`get_tick_stats()` returns a snapshot for HTTP / diagnostic use.
"""
import importlib
import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.config import core_config
from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.market.market_streamer import market_streamer
from upstox_app.portfolio.portfolio_streamer import portfolio_streamer

logger = get_logger(__name__)

_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Tick listener registry
# ---------------------------------------------------------------------------
_tick_listeners: List[Callable[[Dict[str, Any]], None]] = []
_tick_listeners_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Tick flow statistics
# ---------------------------------------------------------------------------
_tick_stats_lock = threading.RLock()
_tick_stats: Dict[str, Any] = {
    "total_ticks": 0,
    "total_fanouts": 0,
    "listener_errors": 0,
    "first_tick_at": None,        # epoch seconds
    "last_tick_at": None,         # epoch seconds
    "keys_seen": {},              # instrument_key -> first epoch
    "ticks_per_key": {},          # instrument_key -> cumulative count
    "message_shapes_seen": {},    # top-level keys -> count
}
_tick_stats_thread_started = False
_tick_stats_thread_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Dedicated tick-diagnostic logger
# ---------------------------------------------------------------------------
# Lazily created on first diagnostic when STREAMER_TICK_LOG_ENABLED=true.
# When the flag is false, `_tick_log()` falls through to the main logger.
_tick_diag_logger: Optional[logging.Logger] = None
_tick_diag_logger_lock = threading.Lock()


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


# ---- dedicated tick diagnostics file --------------------------------------

def _get_tick_diag_logger() -> Optional[logging.Logger]:
    """
    Return the dedicated tick-diagnostic logger if the split is enabled,
    else None. Thread-safe. Failure to open the file downgrades to the
    main logger rather than raising.
    """
    global _tick_diag_logger

    if not _flag("STREAMER_TICK_LOG_ENABLED", False):
        return None

    if _tick_diag_logger is not None:
        return _tick_diag_logger

    with _tick_diag_logger_lock:
        if _tick_diag_logger is not None:
            return _tick_diag_logger

        path_str = str(_cfg("STREAMER_TICK_LOG_FILE", "logs/streamer_test.log"))
        try:
            path = Path(path_str)
            path.parent.mkdir(parents=True, exist_ok=True)

            diag = logging.getLogger("upstox_app.streamer.tick_diag")
            diag.setLevel(logging.DEBUG)
            diag.propagate = False

            # Hot-reload safety: drop any stale handlers.
            for h in list(diag.handlers):
                diag.removeHandler(h)

            handler = RotatingFileHandler(
                str(path),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            diag.addHandler(handler)

            _tick_diag_logger = diag
            # Announce the split once, on the main logger, so it's discoverable.
            logger.info("Streamer tick diagnostics routed to | path=%s", path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Streamer tick diagnostic log could not be opened | path=%s | reason=%s",
                path_str, _short(exc),
            )
            _tick_diag_logger = None

        return _tick_diag_logger


def _tick_log(level: int, msg: str, *args: Any) -> None:
    """
    Emit a tick-diagnostic message.

    When STREAMER_TICK_LOG_ENABLED=true, writes to the dedicated file and
    suppresses the main app logger. Otherwise, writes to the main logger.
    """
    diag = _get_tick_diag_logger()
    if diag is not None:
        try:
            diag.log(level, msg, *args)
            return
        except Exception:  # noqa: BLE001
            # Fall through to main logger if the diag logger misbehaves.
            pass
    logger.log(level, msg, *args)


# ---- market_hours probing --------------------------------------------------

_MARKET_HOURS_CANDIDATE_FUNCS = (
    "market_state_text", "market_state", "get_market_state",
    "current_market_state", "session_state", "state",
)
_MARKET_OPEN_CANDIDATE_FUNCS = (
    "is_market_open", "market_open", "is_open", "session_open",
)


def _import_market_hours():
    try:
        return importlib.import_module("upstox_app.common.market_hours")
    except Exception as exc:  # noqa: BLE001
        logger.debug("streamer_manager | market_hours import failed | reason=%s", _short(exc))
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
                logger.debug("streamer_manager | market_hours.%s failed | reason=%s", name, _short(exc))
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
                logger.debug("streamer_manager | market_hours.%s failed | reason=%s", name, _short(exc))
    return True


# ---- option_service probing ------------------------------------------------


def _collect_option_keys() -> List[str]:
    """
    Collect every option instrument key currently known to the app.
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

    for fn_name in (
        "get_all_contracts", "get_contracts", "list_contracts",
        "get_cached_contracts", "get_index_contracts", "get_options",
    ):
        fn = getattr(module, fn_name, None)
        if not callable(fn):
            continue
        try:
            try:
                data = fn()
                if isinstance(data, list) and data:
                    contracts = list(data)
                    break
            except TypeError:
                pass
            aggregated: List[Dict[str, Any]] = []
            for index_name, meta in core_config.MAIN_INDEXES.items():
                if not meta.get("enabled"):
                    continue
                try:
                    rows = fn(index_name)
                    if isinstance(rows, list):
                        aggregated.extend(rows)
                except Exception:
                    continue
            if aggregated:
                contracts = aggregated
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "streamer_manager | option_service.%s failed | reason=%s",
                fn_name, _short(exc),
            )

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
# Tick listener registry API
# ---------------------------------------------------------------------------


def _name_of(fn: Callable[..., Any]) -> str:
    return (
        getattr(fn, "__qualname__", None)
        or getattr(fn, "__name__", None)
        or repr(fn)
    )


def register_tick_listener(fn: Callable[[Dict[str, Any]], None]) -> None:
    if not callable(fn):
        logger.warning("register_tick_listener: not callable | fn=%r", fn)
        return
    with _tick_listeners_lock:
        if fn in _tick_listeners:
            return
        _tick_listeners.append(fn)
        count = len(_tick_listeners)
    # Structural event — always to the main logger.
    logger.info("Tick listener registered | count=%d | fn=%s", count, _name_of(fn))
    _maybe_start_stats_thread()


def unregister_tick_listener(fn: Callable[[Dict[str, Any]], None]) -> bool:
    with _tick_listeners_lock:
        try:
            _tick_listeners.remove(fn)
        except ValueError:
            return False
        count = len(_tick_listeners)
    logger.info("Tick listener unregistered | count=%d | fn=%s", count, _name_of(fn))
    return True


def tick_listener_count() -> int:
    with _tick_listeners_lock:
        return len(_tick_listeners)


# ---------------------------------------------------------------------------
# Tick flow statistics API
# ---------------------------------------------------------------------------


def _extract_keys(message: Dict[str, Any]) -> List[str]:
    """
    Extract instrument keys from a raw message. Handles the Upstox v3
    shape {"feeds": {"KEY": {...}}} and a flat {"instrument_key": "KEY"}.
    """
    if not isinstance(message, dict):
        return []
    feeds = message.get("feeds")
    if isinstance(feeds, dict) and feeds:
        return [str(k) for k in feeds.keys()]
    for k in ("instrument_key", "instrumentKey", "key"):
        v = message.get(k)
        if isinstance(v, str) and v:
            return [v]
    return []


def _maybe_start_stats_thread() -> None:
    global _tick_stats_thread_started
    if _tick_stats_thread_started:
        return
    if not _flag("STREAMER_TICK_STATS_ENABLED", True):
        return
    with _tick_stats_thread_lock:
        if _tick_stats_thread_started:
            return
        _tick_stats_thread_started = True
    t = threading.Thread(target=_tick_stats_loop, name="tick-stats", daemon=True)
    t.start()
    # Structural — main logger.
    logger.info("Tick stats thread started")


def _tick_stats_loop() -> None:
    interval = max(5, int(_cfg("STREAMER_TICK_STATS_INTERVAL_SEC", 60)))
    while True:
        time.sleep(interval)
        try:
            with _tick_stats_lock:
                total = _tick_stats["total_ticks"]
                last = _tick_stats["last_tick_at"]
                keys_n = len(_tick_stats["keys_seen"])
                errs = _tick_stats["listener_errors"]
            last_age = (time.time() - last) if last else None
            in_session = _is_market_open()
            if total == 0 and in_session:
                _tick_log(
                    logging.WARNING,
                    "Tick flow | total=0 | in_session=True — NO ticks received. "
                    "Check that market_streamer.on_message calls "
                    "streamer_manager._fanout_tick(msg).",
                )
            else:
                _tick_log(
                    logging.INFO,
                    "Tick flow | total=%d | keys_seen=%d | last_age=%s | listener_errors=%d",
                    total, keys_n,
                    f"{last_age:.1f}s" if last_age is not None else "never",
                    errs,
                )
        except Exception:
            _tick_log(logging.ERROR, "tick stats loop error")


def get_tick_stats() -> Dict[str, Any]:
    """Return a snapshot of tick flow statistics."""
    with _tick_stats_lock:
        last = _tick_stats["last_tick_at"]
        first = _tick_stats["first_tick_at"]
        return {
            "total_ticks": _tick_stats["total_ticks"],
            "total_fanouts": _tick_stats["total_fanouts"],
            "listener_errors": _tick_stats["listener_errors"],
            "first_tick_at": first,
            "last_tick_at": last,
            "first_tick_age_sec": (time.time() - first) if first else None,
            "last_tick_age_sec": (time.time() - last) if last else None,
            "keys_seen_count": len(_tick_stats["keys_seen"]),
            "keys_seen": list(_tick_stats["keys_seen"].keys()),
            "listeners": tick_listener_count(),
            "message_shapes_seen": dict(_tick_stats["message_shapes_seen"]),
            "tick_log_enabled": _flag("STREAMER_TICK_LOG_ENABLED", False),
            "tick_log_file": str(_cfg("STREAMER_TICK_LOG_FILE", "logs/streamer_test.log")),
        }


# ---------------------------------------------------------------------------
# Tick fanout
# ---------------------------------------------------------------------------


def _fanout_tick(message: Dict[str, Any]) -> None:
    """
    Fan a raw tick out to every registered listener. Must be called from
    the market streamer's SDK on_message callback.

    Exception-isolated per listener. Never raises. Updates tick stats.
    """
    if not message:
        return

    now = time.time()
    keys = _extract_keys(message)

    debug_each = _flag("STREAMER_DEBUG_TICKS", False)
    log_first = _flag("STREAMER_LOG_FIRST_TICK_PER_KEY", True)

    with _tick_stats_lock:
        _tick_stats["total_ticks"] += 1
        if _tick_stats["first_tick_at"] is None:
            _tick_stats["first_tick_at"] = now
        _tick_stats["last_tick_at"] = now

        shape_key = ",".join(sorted(message.keys()))[:120] if isinstance(message, dict) else "?"
        _tick_stats["message_shapes_seen"][shape_key] = (
            _tick_stats["message_shapes_seen"].get(shape_key, 0) + 1
        )

        for k in keys:
            if k not in _tick_stats["keys_seen"]:
                _tick_stats["keys_seen"][k] = now
                if log_first:
                    _tick_log(logging.INFO, "First tick seen | key=%s", k)
            _tick_stats["ticks_per_key"][k] = _tick_stats["ticks_per_key"].get(k, 0) + 1

    if debug_each:
        _tick_log(
            logging.INFO,
            "Tick | keys=%s | shape_keys=%s",
            keys, list(message.keys())[:6],
        )

    with _tick_listeners_lock:
        listeners = tuple(_tick_listeners)

    if listeners:
        _tick_stats["total_fanouts"] += len(listeners)

    for fn in listeners:
        try:
            fn(message)
        except Exception as exc:  # noqa: BLE001
            _tick_stats["listener_errors"] += 1
            _tick_log(
                logging.ERROR,
                "Tick listener raised | listener=%s | reason=%s",
                _name_of(fn), _short(exc),
            )


# ---------------------------------------------------------------------------
# Optional auto-wire probe (still attempted; non-fatal)
# ---------------------------------------------------------------------------

_MARKET_STREAMER_HOOK_NAMES = (
    "register_message_listener",
    "add_message_listener",
    "register_on_message",
    "set_on_message",
    "register_tick_listener",
    "add_tick_listener",
    "register_listener",
    "add_listener",
    "on_message",
)


def _auto_wire_market_streamer() -> bool:
    try:
        module = importlib.import_module("upstox_app.market.market_streamer")
    except Exception as exc:  # noqa: BLE001
        logger.debug("auto-wire: market_streamer import failed | %s", _short(exc))
        return False

    for name in _MARKET_STREAMER_HOOK_NAMES:
        hook = getattr(module, name, None)
        if not callable(hook):
            hook = getattr(market_streamer, name, None)
            if not callable(hook):
                continue
        try:
            hook(_fanout_tick)
            # Structural — main logger.
            logger.info(
                "Tick fanout auto-wired | target=market_streamer | hook=%s", name
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-wire: %s failed | %s", name, _short(exc))

    # Structural — main logger (this is a boot-time misconfiguration).
    logger.warning(
        "Tick fanout NOT auto-wired — market_streamer exposes no known "
        "callback hook. Add to upstox_app/market/market_streamer.py:\n"
        "    _message_listeners = []\n"
        "    def register_message_listener(fn): _message_listeners.append(fn)\n"
        "and inside on_message():\n"
        "    from upstox_app.streamer.streamer_manager import _fanout_tick\n"
        "    _fanout_tick(message)"
    )
    return False


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------


def auto_connect_enabled() -> bool:
    return _flag("UPSTOX_AUTO_CONNECT_ON_STARTUP", True)


def index_subscription_enabled() -> bool:
    return _flag("UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP", True)


def option_subscription_enabled() -> bool:
    return _flag("OPTIONS_SUBSCRIBE_ALL_ON_STARTUP", True)


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
    _maybe_start_stats_thread()
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
            "tick_listeners": tick_listener_count(),
            "tick_stats": get_tick_stats(),
        }


def market_streamer_status() -> Dict[str, Any]:
    with _lock:
        connected = bool(
            _safe_call(market_streamer, "is_connected", False)
            or _safe_call(market_streamer, "connected", False)
        )
        return {
            "connected": connected,
            "subscriptions": _safe_call(market_streamer, "subscription_count", None),
            "buffer_size": _safe_call(market_streamer, "buffer_size", None),
            "mode": _index_subscription_mode(),
            "tick_listeners": tick_listener_count(),
            "tick_stats": get_tick_stats(),
            "auto_connect": auto_connect_enabled(),
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
    except Exception:
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

    try:
        drain = getattr(market_streamer, "drain_intents", None)
        if callable(drain) and connected:
            try:
                drain()
            except Exception as exc:  # noqa: BLE001
                logger.debug("market_streamer.drain_intents failed | %s", _short(exc))
    except Exception:
        pass

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


# ---------------------------------------------------------------------------
# import-time auto-wiring (best-effort)
# ---------------------------------------------------------------------------

try:
    _auto_wire_market_streamer()
except Exception as _exc:  # noqa: BLE001
    logger.debug("auto-wire market streamer failed at import: %s", _short(_exc))