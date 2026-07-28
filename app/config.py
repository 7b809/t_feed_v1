import os
from typing import List
from dotenv import load_dotenv

# Load environment variables from .env file into os.environ
load_dotenv()


class Settings:
    MONGO_URL: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    MONGO_DB: str = os.getenv("MONGO_DB", "UPSTOX_APP")
    INSTRUMENTS_COLLECTION: str = os.getenv(
        "INSTRUMENTS_COLLECTION", "latest_instruments"
    )
    TOKENS_COLLECTION: str = os.getenv("TOKENS_COLLECTION", "upstox_tokens")

    # Cast numerical string values to integers
    STRIKE_FROM: int = int(os.getenv("STRIKE_FROM", "23000"))
    STRIKE_TO: int = int(os.getenv("STRIKE_TO", "25000"))
    PORT: int = int(os.getenv("PORT", "8000"))

    UPSTOX_MODE: str = os.getenv("UPSTOX_MODE", "ltpc")
    TICK_FILE: str = os.getenv("TICK_FILE", "data/ticks.jsonl")
    LOG_DIR: str = os.getenv("LOG_DIR", "logs")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    HOST: str = os.getenv("HOST", "0.0.0.0")

    CANDLE_INTERVALS: str = os.getenv("CANDLE_INTERVALS", "1,3,5")

    @property
    def parsed_candle_intervals(self) -> List[int]:
        """Parses comma-separated string '1,3,5' into a list of integers [1, 3, 5]."""
        return [int(x.strip()) for x in self.CANDLE_INTERVALS.split(",") if x.strip()]


settings = Settings()
