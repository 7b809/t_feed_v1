"""
core/config.py

Global project configuration + singleton MongoDB manager.

Responsibilities:
    - Load global application configuration from .env.
    - Define static application timezone configuration.
    - Define static main-index instrument mappings.
    - Load configurable index settings from environment variables.
    - Manage the global MongoDB client / database.

Token-specific settings live in:
    token_tasks/config.py
"""

import os
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

# =============================================================
# Load environment variables
# =============================================================

load_dotenv()


# =============================================================
# Helpers
# =============================================================


def _env_bool(
    name: str,
    default: bool,
) -> bool:
    """
    Read a boolean environment variable.

    Accepted true values:
        1, true, yes, on

    Accepted false values:
        0, false, no, off
    """

    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _env_int(
    name: str,
    default: int,
) -> int:
    """Read an integer environment variable."""

    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)

    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {name}: {value!r}") from exc


# =============================================================
# Core Configuration
# =============================================================


class CoreConfig:
    """Global application configuration."""

    # =========================================================
    # Application
    # =========================================================

    APP_NAME: str = os.getenv(
        "APP_NAME",
        "fastapi-token-app",
    )

    APP_VERSION: str = os.getenv(
        "APP_VERSION",
        "1.0.0",
    )

    APP_HOST: str = os.getenv(
        "APP_HOST",
        "0.0.0.0",
    )

    APP_PORT: int = _env_int(
        "APP_PORT",
        8000,
    )

    DEBUG: bool = _env_bool(
        "DEBUG",
        False,
    )

    # =========================================================
    # Application Timezone
    # =========================================================
    #
    # Static application-wide timezone.
    #
    # India uses:
    #     Asia/Kolkata
    #
    # IST = UTC+05:30
    #
    # This is intentionally NOT loaded from .env because the
    # application is designed to operate in Indian market time.
    #
    # Other modules should use:
    #
    #     core_config.TIMEZONE
    #
    # instead of creating their own timezone objects.
    # =========================================================

    TIMEZONE_NAME: str = "Asia/Kolkata"

    TIMEZONE_ABBR: str = "IST"

    TIMEZONE_OFFSET: str = "+05:30"

    TIMEZONE: ZoneInfo = ZoneInfo(TIMEZONE_NAME)

    # =========================================================
    # Logging
    # =========================================================

    LOG_LEVEL: str = os.getenv(
        "LOG_LEVEL",
        "INFO",
    )

    LOG_DIR: str = os.getenv(
        "LOG_DIR",
        "logs",
    )

    LOG_FILE: str = os.getenv(
        "LOG_FILE",
        "app.log",
    )

    # =========================================================
    # MongoDB
    # =========================================================

    # No fallback credentials / URI.
    #
    # These must come from .env or the deployment environment.

    MONGO_URI: Optional[str] = os.getenv(
        "MONGO_URI",
    )

    MONGO_DB_NAME: Optional[str] = os.getenv(
        "MONGO_DB_NAME",
    )

    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = _env_int(
        "MONGO_SERVER_SELECTION_TIMEOUT_MS",
        5000,
    )

    # =========================================================
    # Main Index Configuration
    # =========================================================
    #
    # Instrument keys are intentionally STATIC.
    #
    # Operational settings are configurable through .env:
    #
    #   - enabled
    #   - start instrument range
    #   - end instrument range
    #   - opening range
    #
    # The start/end values represent the broad configured
    # lifecycle envelope for the index.
    # =========================================================

    # =========================================================
    # NIFTY
    # =========================================================

    NIFTY_ENABLED: bool = _env_bool(
        "NIFTY_ENABLED",
        True,
    )

    NIFTY_START_INSTRUMENT_RANGE: int = _env_int(
        "NIFTY_START_INSTRUMENT_RANGE",
        22500,
    )

    NIFTY_END_INSTRUMENT_RANGE: int = _env_int(
        "NIFTY_END_INSTRUMENT_RANGE",
        24500,
    )

    NIFTY_OPENING_RANGE: int = _env_int(
        "NIFTY_OPENING_RANGE",
        300,
    )

    # =========================================================
    # SENSEX
    # =========================================================

    SENSEX_ENABLED: bool = _env_bool(
        "SENSEX_ENABLED",
        True,
    )

    SENSEX_START_INSTRUMENT_RANGE: int = _env_int(
        "SENSEX_START_INSTRUMENT_RANGE",
        70000,
    )

    SENSEX_END_INSTRUMENT_RANGE: int = _env_int(
        "SENSEX_END_INSTRUMENT_RANGE",
        85000,
    )

    SENSEX_OPENING_RANGE: int = _env_int(
        "SENSEX_OPENING_RANGE",
        500,
    )

    # =========================================================
    # Static Instrument Keys
    # =========================================================

    NIFTY_INSTRUMENT_KEY: str = "NSE_INDEX|Nifty 50"

    SENSEX_INSTRUMENT_KEY: str = "BSE_INDEX|SENSEX"

    # =========================================================
    # Main Index Mapping
    # =========================================================

    MAIN_INDEXES: Dict[str, Dict[str, object]] = {
        "NIFTY": {
            "instrument_key": NIFTY_INSTRUMENT_KEY,
            "enabled": NIFTY_ENABLED,
            "start_instrument_range": NIFTY_START_INSTRUMENT_RANGE,
            "end_instrument_range": NIFTY_END_INSTRUMENT_RANGE,
            "opening_range": NIFTY_OPENING_RANGE,
        },
        "SENSEX": {
            "instrument_key": SENSEX_INSTRUMENT_KEY,
            "enabled": SENSEX_ENABLED,
            "start_instrument_range": SENSEX_START_INSTRUMENT_RANGE,
            "end_instrument_range": SENSEX_END_INSTRUMENT_RANGE,
            "opening_range": SENSEX_OPENING_RANGE,
        },
    }


# =============================================================
# Global Configuration Singleton
# =============================================================

core_config = CoreConfig()


# =============================================================
# MongoDB Manager
# =============================================================


class MongoManager:
    """
    Singleton holder for the global MongoClient / Database.

    Started once during FastAPI lifespan and reused by
    every feature package.
    """

    _client: Optional[MongoClient] = None
    _db: Optional[Database] = None

    @classmethod
    def connect(cls) -> Database:
        """Create MongoClient and return configured database."""

        if not core_config.MONGO_URI:
            raise ValueError("MONGO_URI is not configured")

        if not core_config.MONGO_DB_NAME:
            raise ValueError("MONGO_DB_NAME is not configured")

        if cls._client is None:
            cls._client = MongoClient(
                core_config.MONGO_URI,
                serverSelectionTimeoutMS=(
                    core_config.MONGO_SERVER_SELECTION_TIMEOUT_MS
                ),
            )

        cls._db = cls._client[core_config.MONGO_DB_NAME]

        return cls._db

    @classmethod
    def get_db(cls) -> Database:
        """Return the active MongoDB database."""

        if cls._db is None:
            return cls.connect()

        return cls._db

    @classmethod
    def get_client(cls) -> MongoClient:
        """Return the active MongoDB client."""

        if cls._client is None:
            cls.connect()

        return cls._client

    @classmethod
    def ping(cls) -> bool:
        """Check MongoDB connectivity."""

        try:
            cls.get_client().admin.command("ping")

            return True

        except Exception:
            return False

    @classmethod
    def close(cls) -> None:
        """Close MongoDB connection."""

        if cls._client is not None:
            cls._client.close()

        cls._client = None
        cls._db = None


# =============================================================
# MongoDB Singleton
# =============================================================

mongo_manager = MongoManager()
