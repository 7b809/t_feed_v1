from dotenv import load_dotenv
import os

load_dotenv()


class Settings:
    # ----------------------------------------------------------
    # MongoDB
    # ----------------------------------------------------------
    mongodb_uri: str = os.getenv(
        "MONGODB_URI",
        "mongodb://localhost:27017",
    )

    # Application database — this service reads/writes its own state here.
    mongodb_db: str = os.getenv(
        "MONGODB_DB",
        "UPSTOX_APP_v3",
    )

    # Token source database — read-only. Kept separate because the token
    # is produced by an external process (e.g. Telegram login flow) and
    # must not be coupled to this service's schema.
    token_mongodb_db: str = os.getenv(
        "TOKEN_MONGODB_DB",
        "UPSTOX_APP",
    )

    token_collection: str = os.getenv(
        "TOKEN_COLLECTION",
        "upstox_tokens",
    )

    token_document_id: str = os.getenv(
        "TOKEN_DOCUMENT_ID",
        "upstox_access_token",
    )

    # ----------------------------------------------------------
    # FastAPI
    # ----------------------------------------------------------
    app_host: str = os.getenv(
        "APP_HOST",
        "0.0.0.0",
    )

    app_port: int = int(
        os.getenv(
            "APP_PORT",
            "8001",
        )
    )

    app_api_key: str = os.getenv(
        "APP_API_KEY",
        "change-me",
    )

    # ----------------------------------------------------------
    # Startup subscriptions
    # ----------------------------------------------------------
    startup_index_subscription_mode: str = os.getenv(
        "STARTUP_INDEX_SUBSCRIPTION_MODE",
        "ltpc",
    )

    startup_option_subscription_mode: str = os.getenv(
        "STARTUP_OPTION_SUBSCRIPTION_MODE",
        "ltpc",
    )

    # Either an explicit date (YYYY-MM-DD) or a keyword:
    #   current_week | next_week | current_month | next_month
    startup_option_expiry: str = os.getenv(
        "STARTUP_OPTION_EXPIRY",
        "current_week",
    )

    valid_subscription_modes: tuple[str, ...] = (
        "ltpc",
        "full",
        "option_greeks",
        "full_d30",
    )

    startup_subscribe_list: list[dict] = [
        {
            "instrument_name": "NSE_INDEX|Nifty 50",
            "index_mode": "ltpc",
            "option_mode": "ltpc",
            "start_range": 22000,
            "end_range": 24500,
        },
        {
            "instrument_name": "BSE_INDEX|SENSEX",
            "index_mode": "ltpc",
            "option_mode": "ltpc",
            "start_range": 70000,
            "end_range": 72500,
        },
    ]

    def __init__(self) -> None:
        self._validate_modes()

    def _validate_modes(self) -> None:
        if (
            self.startup_index_subscription_mode
            not in self.valid_subscription_modes
        ):
            raise ValueError(
                "Invalid STARTUP_INDEX_SUBSCRIPTION_MODE: "
                f"{self.startup_index_subscription_mode}"
            )

        if (
            self.startup_option_subscription_mode
            not in self.valid_subscription_modes
        ):
            raise ValueError(
                "Invalid STARTUP_OPTION_SUBSCRIPTION_MODE: "
                f"{self.startup_option_subscription_mode}"
            )

        for item in self.startup_subscribe_list:
            index_mode = item.get(
                "index_mode",
                self.startup_index_subscription_mode,
            )

            option_mode = item.get(
                "option_mode",
                self.startup_option_subscription_mode,
            )

            if index_mode not in self.valid_subscription_modes:
                raise ValueError(
                    "Invalid index subscription mode for "
                    f"{item.get('instrument_name')}: "
                    f"{index_mode}"
                )

            if option_mode not in self.valid_subscription_modes:
                raise ValueError(
                    "Invalid option subscription mode for "
                    f"{item.get('instrument_name')}: "
                    f"{option_mode}"
                )


settings = Settings()