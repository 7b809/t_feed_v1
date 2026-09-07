from pathlib import Path

from core.logger import get_logger

logger = get_logger("token_service")


class TokenService:

    def __init__(self) -> None:
        self.token_file = Path("access_token.txt")

    def get_access_token(self) -> str | None:
        try:
            if not self.token_file.exists():
                logger.error(
                    "Access token file not found: %s",
                    self.token_file,
                )
                return None

            access_token = self.token_file.read_text(encoding="utf-8").strip()

            if not access_token:
                logger.error("Access token file is empty.")
                return None

            logger.info("Access token loaded successfully.")

            return access_token

        except Exception:
            logger.exception("Failed to load access token.")
            return None


token_service = TokenService()
