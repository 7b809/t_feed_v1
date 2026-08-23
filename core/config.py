import os
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load environment variables from the .env file.
load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.app_name = os.getenv(
            "APP_NAME",
            "Upstox Order Request Receiver",
        )

        self.tele_flg = os.getenv(
            "TELE_FLG",
            "false",
        ).strip().lower() in {"true", "1", "yes", "on"}

        self.test_flg = os.getenv(
            "TEST_FLG",
            "false",
        ).strip().lower() in {"true", "1", "yes", "on"}

        self.app_env = os.getenv(
            "APP_ENV",
            "development",
        )

        self.app_host = os.getenv(
            "APP_HOST",
            "0.0.0.0",
        )

        self.app_port = int(
            os.getenv(
                "APP_PORT",
                "8000",
            )
        )

        self.telegram_bot_token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        ).strip()

        self.telegram_chat_id = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        ).strip()

        self.log_level = os.getenv(
            "LOG_LEVEL",
            "INFO",
        ).upper()

        self.print_flag = os.getenv(
            "PRINT_FLAG",
            "true",
        ).strip().lower() in {"true", "1", "yes", "on"}

        self.mongodb_uri = os.getenv(
            "MONGODB_URI",
            "mongodb://localhost:27017",
        )

        self.mongodb_database = os.getenv(
            "MONGODB_DATABASE",
            "UPSTOX_ALGO_APP",
        )

        self.mongodb_collection = os.getenv(
            "MONGODB_COLLECTION",
            "order_reqs",
        )

        # Default application timezone.
        self.app_timezone = os.getenv(
            "APP_TIMEZONE",
            "Asia/Kolkata",
        )

        try:
            self.timezone = ZoneInfo(self.app_timezone)
        except Exception as exc:
            raise ValueError(
                f"Invalid APP_TIMEZONE value: {self.app_timezone}"
            ) from exc

        # Timestamp output format.
        # Example: 2026-08-22T14:32:15.123456+05:30
        self.datetime_format = os.getenv(
            "DATETIME_FORMAT",
            "iso",
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
