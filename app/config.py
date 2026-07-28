import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Settings:

    # ============================================================
    # MongoDB
    # ============================================================

    MONGO_URL: str = os.getenv(
        "MONGO_URL",
        "mongodb://localhost:27017",
    )

    MONGO_DB: str = os.getenv(
        "MONGO_DB",
        "UPSTOX_APP",
    )

    INSTRUMENTS_COLLECTION: str = os.getenv(
        "INSTRUMENTS_COLLECTION",
        "latest_instruments",
    )

    TOKENS_COLLECTION: str = os.getenv(
        "TOKENS_COLLECTION",
        "upstox_tokens",
    )

    # ============================================================
    # FastAPI
    # ============================================================

    HOST: str = os.getenv(
        "HOST",
        "0.0.0.0",
    )

    PORT: int = int(
        os.getenv(
            "PORT",
            "8000",
        )
    )

    # ============================================================
    # Logging
    # ============================================================

    LOG_DIR: str = os.getenv(
        "LOG_DIR",
        "logs",
    )

    LOG_LEVEL: str = os.getenv(
        "LOG_LEVEL",
        "INFO",
    )

    # ============================================================
    # Upstox
    # ============================================================

    UPSTOX_MODE: str = os.getenv(
        "UPSTOX_MODE",
        "full",
    )

    OPTION_INSTRUMENT_KEY: str = os.getenv(
        "OPTION_INSTRUMENT_KEY",
        "NSE_INDEX|Nifty 50",
    )

    # ============================================================
    # Strike Filters
    # ============================================================

    STRIKE_FROM: int = int(
        os.getenv(
            "STRIKE_FROM",
            "23000",
        )
    )

    STRIKE_TO: int = int(
        os.getenv(
            "STRIKE_TO",
            "25000",
        )
    )

    # ============================================================
    # EMA Settings
    # ============================================================

    EMA_SHORT_PERIOD: int = int(
        os.getenv(
            "EMA_SHORT_PERIOD",
            "9",
        )
    )

    EMA_LONG_PERIOD: int = int(
        os.getenv(
            "EMA_LONG_PERIOD",
            "21",
        )
    )

    # ============================================================
    # Historical Data
    # ============================================================

    HISTORICAL_DAYS: int = int(
        os.getenv(
            "HISTORICAL_DAYS",
            "7",
        )
    )

    CANDLE_INTERVALS: str = os.getenv(
        "CANDLE_INTERVALS",
        "1,3,5",
    )

    SAVE_OPTIONS_DATA: bool = (
        os.getenv(
            "SAVE_OPTIONS_DATA",
            "false",
        ).lower()
        == "true"
    )

    # ============================================================
    # Live Feed Settings
    # ============================================================

    LIVE_CANDLE_INTERVAL: int = int(
        os.getenv(
            "LIVE_CANDLE_INTERVAL",
            "1",
        )
    )

    MAX_CROSSOVER_EVENTS: int = int(
        os.getenv(
            "MAX_CROSSOVER_EVENTS",
            "500",
        )
    )

    # ============================================================
    # Scheduler Settings
    # ============================================================

    DAILY_REFRESH_TIME: str = os.getenv(
        "DAILY_REFRESH_TIME",
        "08:50",
    )

    WEBSOCKET_CONNECT_TIME: str = os.getenv(
        "WEBSOCKET_CONNECT_TIME",
        "09:10",
    )

    MARKET_OPEN_TIME: str = os.getenv(
        "MARKET_OPEN_TIME",
        "09:15",
    )

    MARKET_CLOSE_TIME: str = os.getenv(
        "MARKET_CLOSE_TIME",
        "15:30",
    )

    # ============================================================
    # File Storage
    # ============================================================

    TICK_FILE: str = os.getenv(
        "TICK_FILE",
        "data/ticks.jsonl",
    )


settings = Settings()