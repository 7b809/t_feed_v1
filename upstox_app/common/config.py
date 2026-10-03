"""
upstox_app/common/config.py

Streamer + option-service + candle configuration.

Token settings live in:
    token_tasks/config.py

Global application settings live in:
    core/config.py

This module contains settings specific to the Upstox
streaming, option, and candle services.
"""

import os

from dotenv import load_dotenv


# =============================================================
# Load environment variables
# =============================================================

load_dotenv()


# =============================================================
# Environment helpers
# =============================================================


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable."""

    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable."""

    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)

    except ValueError:
        return default


def _env_octal(name: str, default: int) -> int:
    """Read an octal environment variable such as 755."""

    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value, 8)

    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    """Read a string environment variable."""

    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    """Read a floating-point environment variable."""

    value = os.getenv(name)

    if value is None:
        return default

    try:
        return float(value)

    except (TypeError, ValueError):
        return default


# =============================================================
# Upstox Application Configuration
# =============================================================


class UpstoxAppConfig:
    """
    Configuration for:

        - Upstox market streamer
        - Portfolio streamer
        - Option loader
        - Option storage
        - Candle loader
        - Candle storage

    Global application configuration belongs in:
        core/config.py
    """

    # =========================================================
    # STREAMER
    # =========================================================

    MESSAGE_BUFFER_SIZE: int = _env_int(
        "UPSTOX_MSG_BUFFER_SIZE",
        500,
    )

    CONNECT_TIMEOUT_SEC: float = _env_float(
        "UPSTOX_CONNECT_TIMEOUT_SEC",
        15.0,
    )

    AUTO_CONNECT_ON_STARTUP: bool = _env_bool(
        "UPSTOX_AUTO_CONNECT_ON_STARTUP",
        False,
    )

    SUBSCRIBE_INDEXES_ON_STARTUP: bool = _env_bool(
        "UPSTOX_SUBSCRIBE_INDEXES_ON_STARTUP",
        True,
    )

    DEFAULT_SUBSCRIPTION_MODE: str = _env_str(
        "UPSTOX_DEFAULT_SUBSCRIPTION_MODE",
        "full",
    )

    AUTO_RECONNECT_ENABLED: bool = _env_bool(
        "UPSTOX_AUTO_RECONNECT_ENABLED",
        True,
    )

    AUTO_RECONNECT_INTERVAL_SEC: int = _env_int(
        "UPSTOX_AUTO_RECONNECT_INTERVAL_SEC",
        10,
    )

    AUTO_RECONNECT_RETRY_COUNT: int = _env_int(
        "UPSTOX_AUTO_RECONNECT_RETRY_COUNT",
        3,
    )

    # =========================================================
    # OPTION STORAGE
    # =========================================================

    OPTIONS_READONLY_DIR: str = _env_str(
        "OPTIONS_READONLY_DIR",
        "data/readonly",
    )

    OPTIONS_RUNTIME_DIR: str = _env_str(
        "OPTIONS_RUNTIME_DIR",
        "data/runtime",
    )

    OPTIONS_READONLY_MODE: int = _env_octal(
        "OPTIONS_READONLY_MODE",
        0o555,
    )

    OPTIONS_RUNTIME_MODE: int = _env_octal(
        "OPTIONS_RUNTIME_MODE",
        0o755,
    )

    # =========================================================
    # OPTION LOADER
    # =========================================================

    OPTIONS_LOAD_ON_STARTUP: bool = _env_bool(
        "OPTIONS_LOAD_ON_STARTUP",
        True,
    )

    OPTIONS_PERSIST_ON_LOAD: bool = _env_bool(
        "OPTIONS_PERSIST_ON_LOAD",
        True,
    )

    OPTIONS_LOAD_RUNTIME_ON_STARTUP: bool = _env_bool(
        "OPTIONS_LOAD_RUNTIME_ON_STARTUP",
        True,
    )

    OPTIONS_RUNTIME_MAX_AGE_SEC: int = _env_int(
        "OPTIONS_RUNTIME_MAX_AGE_SEC",
        86400,
    )

    # =========================================================
    # OPTION CHAIN SCOPE
    # =========================================================

    OPTIONS_LOAD_ALL_EXPIRIES: bool = _env_bool(
        "OPTIONS_LOAD_ALL_EXPIRIES",
        False,
    )

    OPTIONS_DEFAULT_NEAREST_EXPIRY_ONLY: bool = _env_bool(
        "OPTIONS_DEFAULT_NEAREST_EXPIRY_ONLY",
        True,
    )

    # =========================================================
    # OPTION BULK SUBSCRIPTION
    # =========================================================

    OPTIONS_SUBSCRIBE_ALL_ON_STARTUP: bool = _env_bool(
        "OPTIONS_SUBSCRIBE_ALL_ON_STARTUP",
        False,
    )

    OPTIONS_SUBSCRIBE_MODE: str = _env_str(
        "OPTIONS_SUBSCRIBE_MODE",
        "ltpc",
    )

    OPTIONS_SUBSCRIBE_BATCH_SIZE: int = _env_int(
        "OPTIONS_SUBSCRIBE_BATCH_SIZE",
        500,
    )

    OPTIONS_STRIKE_RANGE_BUFFER: int = _env_int(
        "OPTIONS_STRIKE_RANGE_BUFFER",
        0,
    )

    # =========================================================
    # CANDLE LOADING
    # =========================================================

    CANDLES_ENABLED: bool = _env_bool(
        "OPTIONS_CANDLES_ENABLED",
        True,
    )

    CANDLES_LOAD_ON_STARTUP: bool = _env_bool(
        "OPTIONS_CANDLES_LOAD_ON_STARTUP",
        True,
    )

    CANDLES_DAYS: int = _env_int(
        "OPTIONS_CANDLES_DAYS",
        10,
    )

    CANDLES_UNIT: str = _env_str(
        "OPTIONS_CANDLES_UNIT",
        "minutes",
    )

    CANDLES_INTERVAL: str = _env_str(
        "OPTIONS_CANDLES_INTERVAL",
        "1",
    )

    CANDLES_FETCH_DELAY_SEC: float = _env_float(
        "OPTIONS_CANDLES_FETCH_DELAY_SEC",
        0.0,
    )

    CANDLES_INCLUDE_INTRADAY: bool = _env_bool(
        "OPTIONS_CANDLES_INCLUDE_INTRADAY",
        True,
    )

    CANDLES_MAX_WORKERS: int = _env_int(
        "OPTIONS_CANDLES_MAX_WORKERS",
        8,
    )

    CANDLES_MAX_DAYS_PER_REQUEST: int = _env_int(
        "OPTIONS_CANDLES_MAX_DAYS_PER_REQUEST",
        7,
    )

    # ---- Candle daily refresh -------------------------------------------
    CANDLES_DAILY_REFRESH_ENABLED = _env_bool("OPTIONS_CANDLES_DAILY_REFRESH_ENABLED", True)
    CANDLES_DAILY_REFRESH_TIME = _env_str("OPTIONS_CANDLES_DAILY_REFRESH_TIME", "09:00")

# =============================================================
# Singleton
# =============================================================

upstox_config = UpstoxAppConfig()