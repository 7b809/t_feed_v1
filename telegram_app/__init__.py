"""telegram_app — standalone notification + command bot for UpstoxAppV2."""
from telegram_app.manager import telegram_manager  # noqa: F401
from telegram_app.config import telegram_config  # noqa: F401

__all__ = ["telegram_manager", "telegram_config"]