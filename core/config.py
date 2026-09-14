
import os
from pathlib import Path


class Settings:
    # ============================================================
    # Static Project Configuration
    #
    # Project configuration is intentionally kept in code.
    # It does NOT come from .env.
    #
    # Each project contains:
    #
    #   folder  -> Absolute project directory
    #   command -> Command to execute inside that directory
    #
    # Example:
    #
    # "algo_app_v1": {
    #     "folder": "/home/ubuntu/TheProjects/algo_app_v1",
    #     "command": ["python3", "update_project.py"],
    # }
    # ============================================================

    PROJECTS = {
        "algo_app_v1": {
            "command ": ["python3", "/home/ubuntu/TheProjects/algo_app_v1/update_project.py"],
        },

        "t_feed_v1": {
            "command": ["python3", "/home/ubuntu/TheProjects/t_feed_v1_temp2/update_project.py"],
        },
    }

    # ============================================================
    # Default Configuration
    # ============================================================

    DEFAULT_APP_NAME = "Project Update Service"
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_PORT = 8005
    DEFAULT_LOG_LEVEL = "INFO"

    DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800
    DEFAULT_TELEGRAM_OUTPUT_MAX_CHARS = 3000

    # ============================================================
    # Constructor
    # ============================================================

    def __init__(self):
        # --------------------------------------------------------
        # Application
        # --------------------------------------------------------

        self.app_name = self.DEFAULT_APP_NAME
        self.host = self.DEFAULT_HOST
        self.port = self.DEFAULT_PORT
        self.log_level = self.DEFAULT_LOG_LEVEL

        # --------------------------------------------------------
        # API
        # --------------------------------------------------------

        self.api_key = ""

        # --------------------------------------------------------
        # Telegram
        # --------------------------------------------------------

        self.telegram_bot_token = ""
        self.telegram_chat_id = ""

        # --------------------------------------------------------
        # Update Runner
        # --------------------------------------------------------

        self.command_timeout_seconds = (
            self.DEFAULT_COMMAND_TIMEOUT_SECONDS
        )

        self.telegram_output_max_chars = (
            self.DEFAULT_TELEGRAM_OUTPUT_MAX_CHARS
        )

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
        Load environment configuration from .env.

        Project configuration is intentionally NOT loaded from .env.
        PROJECTS is defined statically in this class.
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
            raise ValueError(
                "API_KEY must be at least 16 characters"
            )

        # --------------------------------------------------------
        # Port
        # --------------------------------------------------------

        if not 1 <= self.port <= 65535:
            raise ValueError(
                f"PORT must be between 1 and 65535: {self.port}"
            )

        # --------------------------------------------------------
        # Timeout
        # --------------------------------------------------------

        if self.command_timeout_seconds <= 0:
            raise ValueError(
                "COMMAND_TIMEOUT_SECONDS must be greater than 0"
            )

        # --------------------------------------------------------
        # Telegram output size
        # --------------------------------------------------------

        if self.telegram_output_max_chars <= 0:
            raise ValueError(
                "TELEGRAM_OUTPUT_MAX_CHARS must be greater than 0"
            )

        # --------------------------------------------------------
        # Project configuration
        # --------------------------------------------------------

        if not self.PROJECTS:
            raise ValueError(
                "PROJECTS must not be empty"
            )

        for name, project in self.PROJECTS.items():

            # ----------------------------------------------------
            # Project name
            # ----------------------------------------------------

            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    "Project name must be a non-empty string"
                )

            # ----------------------------------------------------
            # Project object
            # ----------------------------------------------------

            if not isinstance(project, dict):
                raise ValueError(
                    f"Project configuration must be a dictionary: {name}"
                )

            # ----------------------------------------------------
            # Folder
            # ----------------------------------------------------

            folder = project.get("folder")

            if not isinstance(folder, str) or not folder.strip():
                raise ValueError(
                    f"Project folder must be a non-empty string: {name}"
                )

            project_path = Path(folder)

            if not project_path.is_absolute():
                raise ValueError(
                    f"Project folder must be absolute: {name}: {folder}"
                )

            # ----------------------------------------------------
            # Command
            # ----------------------------------------------------

            command = project.get("command")

            if not isinstance(command, list) or not command:
                raise ValueError(
                    f"Project command must be a non-empty list: {name}"
                )

            for argument in command:

                if not isinstance(argument, str):
                    raise ValueError(
                        f"Every command argument must be a string: {name}"
                    )

                if not argument.strip():
                    raise ValueError(
                        f"Command arguments cannot be empty: {name}"
                    )

    # ============================================================
    # Project Mapping
    # ============================================================

    @property
    def projects(self) -> dict:
        """
        Return the configured projects.

        Structure:

        {
            "project_name": {
                "folder": Path(...),
                "command": [...]
            }
        }

        The folder is converted to an absolute resolved Path.
        The command is returned as a separate list.
        """

        result = {}

        for name, project in self.PROJECTS.items():

            result[name] = {
                "folder": Path(
                    project["folder"]
                ).resolve(),

                "command": list(
                    project["command"]
                ),
            }

        return result

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
            raise ValueError(
                "TELEGRAM_CHAT_ID must be a valid integer"
            )


# ================================================================
# Singleton Settings
# ================================================================

_settings = None


def get_settings() -> Settings:
    global _settings

    if _settings is None:
        _settings = Settings()

    return _settings