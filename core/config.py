import os
from pathlib import Path


class Settings:
    # ============================================================
    # Static Project Configuration
    #
    # Keep project names and their absolute directories here.
    # This does NOT come from .env.
    # ============================================================

    PROJECTS = {
        "algo_app_v1": {
            "folder": "/home/ubuntu/TheProjects/algo_app_v1",
            "command": ["python3", "update_project.py"],
        },
        "t_feed_v1": {
            "folder": "/home/ubuntu/TheProjects/t_feed_v1_temp2",
            "command": ["python3", "update_project.py"],
        },
    }

    # ============================================================
    # Default Configuration
    # ============================================================

    DEFAULT_APP_NAME = "Project Update Service"
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_PORT = 8005
    DEFAULT_LOG_LEVEL = "INFO"

    DEFAULT_UPDATE_PYTHON = "python3"
    DEFAULT_UPDATE_SCRIPT = "update_project.py"

    DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800
    DEFAULT_TELEGRAM_OUTPUT_MAX_CHARS = 3000

    # ============================================================
    # Constructor
    # ============================================================

    def __init__(self):
        # --------------------------------------------------------
        # Defaults
        # --------------------------------------------------------

        self.app_name = self.DEFAULT_APP_NAME
        self.host = self.DEFAULT_HOST
        self.port = self.DEFAULT_PORT
        self.log_level = self.DEFAULT_LOG_LEVEL

        self.api_key = ""

        self.telegram_bot_token = ""
        self.telegram_chat_id = ""

        self.update_python = self.DEFAULT_UPDATE_PYTHON
        self.update_script = self.DEFAULT_UPDATE_SCRIPT

        self.command_timeout_seconds = self.DEFAULT_COMMAND_TIMEOUT_SECONDS

        self.telegram_output_max_chars = self.DEFAULT_TELEGRAM_OUTPUT_MAX_CHARS

        # --------------------------------------------------------
        # Load values from .env
        # --------------------------------------------------------

        self.load_env()

        # --------------------------------------------------------
        # Validate configuration
        # --------------------------------------------------------

        self.validate()

    # ============================================================
    # Load Environment
    # ============================================================

    def load_env(self):
        """
        Load configuration from .env.

        Project mapping is intentionally NOT loaded from .env.
        PROJECTS is defined statically above.
        """

        try:
            from dotenv import load_dotenv

            load_dotenv()

        except ImportError:
            raise RuntimeError(
                "python-dotenv is required. "
                "Install it with: pip install python-dotenv"
            )

        # --------------------------------------------------------
        # Application
        # --------------------------------------------------------

        self.app_name = os.getenv(
            "APP_NAME",
            self.DEFAULT_APP_NAME,
        )

        self.host = os.getenv(
            "HOST",
            self.DEFAULT_HOST,
        )

        self.port = int(
            os.getenv(
                "PORT",
                str(self.DEFAULT_PORT),
            )
        )

        self.log_level = os.getenv(
            "LOG_LEVEL",
            self.DEFAULT_LOG_LEVEL,
        )

        # --------------------------------------------------------
        # API
        # --------------------------------------------------------

        self.api_key = os.getenv(
            "API_KEY",
            "",
        )

        # --------------------------------------------------------
        # Telegram
        # --------------------------------------------------------

        self.telegram_bot_token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        )

        self.telegram_chat_id = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        )

        # --------------------------------------------------------
        # Update Runner
        # --------------------------------------------------------

        self.update_python = os.getenv(
            "UPDATE_PYTHON",
            self.DEFAULT_UPDATE_PYTHON,
        )

        self.update_script = os.getenv(
            "UPDATE_SCRIPT",
            self.DEFAULT_UPDATE_SCRIPT,
        )

        self.command_timeout_seconds = int(
            os.getenv(
                "COMMAND_TIMEOUT_SECONDS",
                str(self.DEFAULT_COMMAND_TIMEOUT_SECONDS),
            )
        )

        self.telegram_output_max_chars = int(
            os.getenv(
                "TELEGRAM_OUTPUT_MAX_CHARS",
                str(self.DEFAULT_TELEGRAM_OUTPUT_MAX_CHARS),
            )
        )

    # ============================================================
    # Validation
    # ============================================================

    def validate(self):
        # --------------------------------------------------------
        # API key
        # --------------------------------------------------------

        if len(self.api_key) < 16:
            raise ValueError("API_KEY must be at least 16 characters")

        # --------------------------------------------------------
        # Port
        # --------------------------------------------------------

        if not 1 <= self.port <= 65535:
            raise ValueError(f"PORT must be between 1 and 65535: {self.port}")

        # --------------------------------------------------------
        # Timeout
        # --------------------------------------------------------

        if self.command_timeout_seconds <= 0:
            raise ValueError("COMMAND_TIMEOUT_SECONDS must be greater than 0")

        # --------------------------------------------------------
        # Telegram output size
        # --------------------------------------------------------

        if self.telegram_output_max_chars <= 0:
            raise ValueError("TELEGRAM_OUTPUT_MAX_CHARS must be greater than 0")

        # --------------------------------------------------------
        # Project configuration
        # --------------------------------------------------------

        if not self.PROJECTS:
            raise ValueError("PROJECTS must not be empty")

        for name, path in self.PROJECTS.items():

            if not isinstance(name, str):
                raise ValueError("Project name must be a string")

            if not isinstance(path, str):
                raise ValueError(f"Project path must be a string: {name}")

            project_path = Path(path)

            if not project_path.is_absolute():
                raise ValueError(f"Project path must be absolute: {path}")

    # ============================================================
    # Project Mapping
    # ============================================================

    @property
    def projects(self) -> dict[str, Path]:
        """
        Return project names mapped to resolved Path objects.
        """

        return {name: Path(path).resolve() for name, path in self.PROJECTS.items()}

    # ============================================================
    # Telegram Allowed Chat ID
    # ============================================================

    @property
    def allowed_chat_id(self) -> int | None:
        """
        Return configured Telegram chat ID as an integer.

        Returns None when Telegram chat ID is not configured.
        """

        if not self.telegram_chat_id.strip():
            return None

        try:
            return int(self.telegram_chat_id)

        except ValueError:
            raise ValueError("TELEGRAM_CHAT_ID must be a valid integer")


# ================================================================
# Singleton Settings
# ================================================================

_settings = None


def get_settings() -> Settings:
    global _settings

    if _settings is None:
        _settings = Settings()

    return _settings
