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

        # ------------------------------------------------------------------
        # Upstox order execution settings
        # ------------------------------------------------------------------

        # Master safety switch for actual broker order placement.
        #
        # IMPORTANT:
        # Keep this false until the complete order execution flow has been
        # tested and you explicitly want the application to place real orders.
        self.order_placement_enabled = os.getenv(
            "ORDER_PLACEMENT_ENABLED",
            "false",
        ).strip().lower() in {"true", "1", "yes", "on"}

        # When enabled, all existing Upstox positions are exited before
        # placing the newly selected order.
        #
        # If exit_positions() fails, the new order is NOT placed.
        self.order_exit_previous_positions = os.getenv(
            "ORDER_EXIT_PREVIOUS_POSITIONS",
            "true",
        ).strip().lower() in {"true", "1", "yes", "on"}

        # Upstox order product.
        # I = Intraday.
        self.order_product = (
            os.getenv(
                "ORDER_PRODUCT",
                "I",
            )
            .strip()
            .upper()
        )

        # Upstox order type.
        self.order_type = (
            os.getenv(
                "ORDER_TYPE",
                "MARKET",
            )
            .strip()
            .upper()
        )

        # Upstox order validity.
        self.order_validity = (
            os.getenv(
                "ORDER_VALIDITY",
                "DAY",
            )
            .strip()
            .upper()
        )

        # Transaction side for the newly selected instrument.
        self.order_transaction_type = (
            os.getenv(
                "ORDER_TRANSACTION_TYPE",
                "BUY",
            )
            .strip()
            .upper()
        )

        # Upstox order tag.
        self.order_tag = os.getenv(
            "ORDER_TAG",
            "EMA_ALGO",
        ).strip()

        # AMO flag.
        self.order_is_amo = os.getenv(
            "ORDER_IS_AMO",
            "false",
        ).strip().lower() in {"true", "1", "yes", "on"}

        # Optional order price.
        #
        # For MARKET orders this remains 0.0.
        self.order_price = float(
            os.getenv(
                "ORDER_PRICE",
                "0.0",
            )
        )

        # Optional trigger price.
        #
        # For MARKET orders this remains 0.0.
        self.order_trigger_price = float(
            os.getenv(
                "ORDER_TRIGGER_PRICE",
                "0.0",
            )
        )

        # Optional disclosed quantity.
        #
        # 0 means no disclosed quantity.
        self.order_disclosed_quantity = int(
            os.getenv(
                "ORDER_DISCLOSED_QUANTITY",
                "0",
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
