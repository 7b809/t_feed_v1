import os
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


_TRUE_VALUES = {
    "true",
    "1",
    "yes",
    "on",
}


def _get_bool(
    name: str,
    default: bool = False,
) -> bool:
    default_value = "true" if default else "false"

    return os.getenv(name, default_value).strip().lower() in _TRUE_VALUES


def _get_int(
    name: str,
    default: int,
) -> int:
    raw_value = os.getenv(
        name,
        str(default),
    ).strip()

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid integer") from exc


def _get_float(
    name: str,
    default: float,
) -> float:
    raw_value = os.getenv(
        name,
        str(default),
    ).strip()

    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid number") from exc


class Settings:
    def __init__(self) -> None:
        # Application
        self.app_name = os.getenv(
            "APP_NAME",
            "Upstox Order Request Receiver",
        ).strip()

        self.app_env = os.getenv(
            "APP_ENV",
            "development",
        ).strip()

        self.app_host = os.getenv(
            "APP_HOST",
            "0.0.0.0",
        ).strip()

        self.app_port = _get_int(
            "APP_PORT",
            8000,
        )

        self.app_timezone = os.getenv(
            "APP_TIMEZONE",
            "Asia/Kolkata",
        ).strip()

        self.datetime_format = os.getenv(
            "DATETIME_FORMAT",
            "iso",
        ).strip()

        try:
            self.timezone = ZoneInfo(self.app_timezone)
        except Exception as exc:
            raise ValueError(
                "Invalid APP_TIMEZONE value: " f"{self.app_timezone}"
            ) from exc

        # Runtime flags
        self.tele_flg = _get_bool(
            "TELE_FLG",
            False,
        )

        self.test_flg = _get_bool(
            "TEST_FLG",
            False,
        )

        self.run_tele_bot = _get_bool(
            "RUN_TELE_BOT",
            False,
        )

        self.PLACE_ORDER = _get_bool(
            "PLACE_ORDER",
            False,
        )

        self.print_flag = _get_bool(
            "PRINT_FLAG",
            True,
        )

        # Logging
        self.log_level = (
            os.getenv(
                "LOG_LEVEL",
                "INFO",
            )
            .strip()
            .upper()
        )

        # Telegram
        self.telegram_bot_token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        ).strip()

        self.telegram_chat_id = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        ).strip()

        # Primary MongoDB
        self.mongodb_uri = os.getenv(
            "MONGODB_URI",
            "mongodb://localhost:27017",
        ).strip()

        self.mongodb_database = os.getenv(
            "MONGODB_DATABASE",
            "UPSTOX_ALGO_APP",
        ).strip()

        self.mongodb_collection = os.getenv(
            "MONGODB_COLLECTION",
            "order_reqs",
        ).strip()

        # Upstox token storage
        self.upstox_mongodb_database = os.getenv(
            "UPSTOX_MONGO_DB",
            "UPSTOX_APP",
        ).strip()

        self.upstox_tokens_collection = os.getenv(
            "UPSTOX_TOKENS_COLLECTION",
            "upstox_tokens",
        ).strip()


        self.upstox_access_token_document_id = os.getenv(
            "UPSTOX_ACCESS_TOKEN_DOCUMENT_ID",
            "upstox_access_token",
        ).strip()

        self.upstox_token_refresh_interval_seconds = _get_int(
            "UPSTOX_TOKEN_REFRESH_INTERVAL_SECONDS",
            3600,
        )

        self.upstox_token_required_on_startup = _get_bool(
            "UPSTOX_TOKEN_REQUIRED_ON_STARTUP",
            True,
        )

        self.DEBUG_FLAG = _get_bool(
            "DEBUG_FLAG",
            False,
        )

        self.upstox_token_validation_enabled = _get_bool(
            "UPSTOX_TOKEN_VALIDATION_ENABLED",
            True,
        )

        self.upstox_token_validation_api_version = os.getenv(
            "UPSTOX_TOKEN_VALIDATION_API_VERSION",
            "2.0",
        ).strip()

        self.upstox_token_validation_timeout_seconds = _get_int(
            "UPSTOX_TOKEN_VALIDATION_TIMEOUT_SECONDS",
            20,
        )

        if not self.upstox_mongodb_database:
            raise ValueError("UPSTOX_MONGO_DB must not be empty")

        if not self.upstox_tokens_collection:
            raise ValueError("UPSTOX_TOKENS_COLLECTION must not be empty")

        if not self.upstox_access_token_document_id:
            raise ValueError("UPSTOX_ACCESS_TOKEN_DOCUMENT_ID " "must not be empty")

        if self.upstox_token_refresh_interval_seconds < 60:
            raise ValueError(
                "UPSTOX_TOKEN_REFRESH_INTERVAL_SECONDS " "must be at least 60"
            )

        if self.upstox_token_validation_timeout_seconds < 5:
            raise ValueError(
                "UPSTOX_TOKEN_VALIDATION_TIMEOUT_SECONDS " "must be at least 5"
            )

        if not self.upstox_token_validation_api_version:
            raise ValueError("UPSTOX_TOKEN_VALIDATION_API_VERSION " "must not be empty")

        # Upstox order execution
        self.order_placement_enabled = _get_bool(
            "ORDER_PLACEMENT_ENABLED",
            False,
        )

        self.order_exit_previous_positions = _get_bool(
            "ORDER_EXIT_PREVIOUS_POSITIONS",
            True,
        )

        self.order_product = (
            os.getenv(
                "ORDER_PRODUCT",
                "I",
            )
            .strip()
            .upper()
        )

        self.order_type = (
            os.getenv(
                "ORDER_TYPE",
                "MARKET",
            )
            .strip()
            .upper()
        )

        self.order_validity = (
            os.getenv(
                "ORDER_VALIDITY",
                "DAY",
            )
            .strip()
            .upper()
        )

        self.order_transaction_type = (
            os.getenv(
                "ORDER_TRANSACTION_TYPE",
                "BUY",
            )
            .strip()
            .upper()
        )

        self.order_tag = os.getenv(
            "ORDER_TAG",
            "EMA_ALGO",
        ).strip()

        self.order_is_amo = _get_bool(
            "ORDER_IS_AMO",
            False,
        )

        self.order_price = _get_float(
            "ORDER_PRICE",
            0.0,
        )

        self.order_trigger_price = _get_float(
            "ORDER_TRIGGER_PRICE",
            0.0,
        )

        self.order_disclosed_quantity = _get_int(
            "ORDER_DISCLOSED_QUANTITY",
            0,
        )

        self._validate()

    def _validate(self) -> None:
        if not 1 <= self.app_port <= 65535:
            raise ValueError("APP_PORT must be between 1 and 65535")

        if not self.mongodb_uri:
            raise ValueError("MONGODB_URI must not be empty")

        if not self.mongodb_database:
            raise ValueError("MONGODB_DATABASE must not be empty")

        if not self.mongodb_collection:
            raise ValueError("MONGODB_COLLECTION must not be empty")

        if self.order_disclosed_quantity < 0:
            raise ValueError("ORDER_DISCLOSED_QUANTITY cannot be negative")

        if self.order_price < 0:
            raise ValueError("ORDER_PRICE cannot be negative")

        if self.order_trigger_price < 0:
            raise ValueError("ORDER_TRIGGER_PRICE cannot be negative")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
