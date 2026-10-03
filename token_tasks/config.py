"""
token_tasks/config.py
Feature-scoped configuration for the token tasks module.
Only things related to the token collection live here.
"""
import os

from dotenv import load_dotenv

load_dotenv()


class TokenConfig:
    """Token-task specific configuration."""

    # Which collection and which document inside it
    COLLECTION_NAME: str = os.getenv("TOKEN_COLLECTION_NAME", "tokens")
    DOC_ID: str = os.getenv("TOKEN_DOC_ID", "upstox_access_token")

    # Cache refresh interval (default 30 min = 1800s)
    REFRESH_INTERVAL_SECONDS: int = int(
        os.getenv("TOKEN_REFRESH_INTERVAL_SECONDS", "1800")
    )

    # Load into cache immediately on startup / restart
    LOAD_ON_STARTUP: bool = os.getenv("TOKEN_LOAD_ON_STARTUP", "true").lower() in (
        "1",
        "true",
        "yes",
    )


token_config = TokenConfig()