import threading
import time

import requests

from core.config import settings
from core.logger import get_logger

logger = get_logger("telegram_bot")


class TelegramBotService:
    def __init__(self) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = str(settings.telegram_chat_id)

        self.running = False
        self.thread: threading.Thread | None = None
        self.offset = 0

    def send_startup_message(self) -> bool:
        """
        Send application startup status to the configured Telegram chat.

        This is an administrative message and does not depend on
        TELE_FLG.
        """

        message = (
            "🚀 <b>Upstox Order Request Receiver Started</b>\n\n"
            f"📡 Telegram: "
            f"{'ENABLED' if settings.tele_flg else 'DISABLED'}\n"
            f"🧪 Test Mode: "
            f"{'ENABLED' if settings.test_flg else 'DISABLED'}\n\n"
            "<b>Available Commands</b>\n"
            "/enable_telegram\n"
            "/disable_telegram\n"
            "/enable_test\n"
            "/disable_test\n"
            "/status"
        )

        return self._send_direct_message(message)

    def start(self) -> None:
        """
        Start Telegram bot polling in a background thread.
        """

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Telegram bot not started. "
                "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
            )
            return

        if self.running:
            logger.warning("Telegram bot is already running.")
            return

        self.running = True

        self.thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-bot",
            daemon=True,
        )

        self.thread.start()

        logger.info("Telegram bot started.")

    def stop(self) -> None:
        """
        Stop Telegram bot polling.
        """

        if not self.running:
            logger.info("Telegram bot is already stopped.")
            return

        self.running = False

        logger.info("Telegram bot stopped.")

    def _poll_loop(self) -> None:
        """
        Continuously poll Telegram for new messages.
        """

        url = f"https://api.telegram.org/bot" f"{self.bot_token}/getUpdates"

        while self.running:
            try:
                response = requests.get(
                    url,
                    params={
                        "offset": self.offset,
                        "timeout": 20,
                    },
                    timeout=30,
                )

                response.raise_for_status()

                data = response.json()

                if not data.get("ok"):
                    logger.error(
                        "Telegram getUpdates failed: %s",
                        data,
                    )

                    time.sleep(3)
                    continue

                for update in data.get("result", []):
                    self.offset = update["update_id"] + 1

                    self._handle_update(update)

            except requests.RequestException:
                logger.exception("Telegram polling request failed.")

                time.sleep(5)

            except Exception:
                logger.exception("Unexpected Telegram bot error.")

                time.sleep(5)

    def _handle_update(
        self,
        update: dict,
    ) -> None:
        """
        Handle one Telegram update.
        """

        message = update.get("message")

        if not message:
            return

        chat = message.get(
            "chat",
            {},
        )

        chat_id = str(chat.get("id"))

        # Only the configured Telegram chat is allowed
        # to control the application.
        if chat_id != self.chat_id:
            logger.warning(
                "Ignoring Telegram command from " "unauthorized chat_id=%s",
                chat_id,
            )
            return

        text = message.get(
            "text",
            "",
        ).strip()

        if not text:
            return

        command = text.split()[0].lower()

        # -------------------------------------------------
        # /start
        # -------------------------------------------------
        if command == "/start":
            logger.info("Received Telegram command: /start")

            self._send_status_message()

        # -------------------------------------------------
        # /enable_telegram
        # -------------------------------------------------
        elif command == "/enable_telegram":

            logger.info("Received Telegram command: " "/enable_telegram")

            if settings.tele_flg:
                logger.info("Telegram sending is already enabled.")

                self._send_direct_message(
                    "ℹ️ " "<b>Telegram sending is already ENABLED.</b>"
                )

                return

            settings.tele_flg = True

            logger.info("Telegram sending ENABLED.")

            self._send_direct_message("✅ " "<b>Telegram sending ENABLED.</b>")

        # -------------------------------------------------
        # /disable_telegram
        # -------------------------------------------------
        elif command == "/disable_telegram":

            logger.info("Received Telegram command: " "/disable_telegram")

            if not settings.tele_flg:
                logger.info("Telegram sending is already disabled.")

                self._send_direct_message(
                    "ℹ️ " "<b>Telegram sending is already DISABLED.</b>"
                )

                return

            # Change the flag first.
            settings.tele_flg = False

            logger.info("Telegram sending DISABLED.")

            # Direct sender is used because normal
            # Telegram sending now sees TELE_FLG=False.
            self._send_direct_message("🔕 " "<b>Telegram sending DISABLED.</b>")

        # -------------------------------------------------
        # /enable_test
        # -------------------------------------------------
        elif command == "/enable_test":

            logger.info("Received Telegram command: " "/enable_test")

            if settings.test_flg:
                logger.info("Test mode is already enabled.")

                self._send_direct_message("ℹ️ " "<b>Test mode is already ENABLED.</b>")

                return

            settings.test_flg = True

            logger.info("Test mode ENABLED.")

            self._send_direct_message(
                "🧪 <b>TEST MODE ENABLED.</b>\n\n"
                "Any JSON payload will now be accepted "
                "and stored as received."
            )

        # -------------------------------------------------
        # /disable_test
        # -------------------------------------------------
        elif command == "/disable_test":

            logger.info("Received Telegram command: " "/disable_test")

            if not settings.test_flg:
                logger.info("Test mode is already disabled.")

                self._send_direct_message("ℹ️ " "<b>Test mode is already DISABLED.</b>")

                return

            settings.test_flg = False

            logger.info("Test mode DISABLED.")

            self._send_direct_message(
                "🛡️ <b>TEST MODE DISABLED.</b>\n\n"
                "Payloads must now match the expected schema."
            )

        # -------------------------------------------------
        # /status
        # -------------------------------------------------
        elif command == "/status":

            logger.info("Received Telegram command: /status")

            self._send_status_message()

        # -------------------------------------------------
        # Unknown command
        # -------------------------------------------------
        else:

            logger.info(
                "Unknown Telegram command received: %s",
                command,
            )

            self._send_help_message()

    def _send_status_message(self) -> None:
        """
        Send current application control status.
        """

        message = (
            "📊 <b>Upstox Order Request Receiver</b>\n\n"
            f"📡 Telegram: "
            f"{'ENABLED' if settings.tele_flg else 'DISABLED'}\n"
            f"🧪 Test Mode: "
            f"{'ENABLED' if settings.test_flg else 'DISABLED'}\n\n"
            "<b>Available Commands</b>\n"
            "/enable_telegram\n"
            "/disable_telegram\n"
            "/enable_test\n"
            "/disable_test\n"
            "/status"
        )

        self._send_direct_message(message)

    def _send_help_message(self) -> None:
        """
        Send available Telegram commands.
        """

        message = (
            "🤖 <b>Available Commands</b>\n\n"
            "/enable_telegram - "
            "Enable Telegram messages\n"
            "/disable_telegram - "
            "Disable Telegram messages\n"
            "/enable_test - "
            "Accept any JSON payload\n"
            "/disable_test - "
            "Use normal payload validation\n"
            "/status - "
            "Show current application status"
        )

        self._send_direct_message(message)

    def _send_direct_message(
        self,
        message: str,
    ) -> bool:
        """
        Send an administrative Telegram message.

        This intentionally does NOT check TELE_FLG.

        This is required for bot control messages such as:
            /disable_telegram

        because TELE_FLG becomes False before the confirmation
        message is sent.
        """

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Cannot send Telegram bot message. " "Bot token or chat ID is missing."
            )
            return False

        url = f"https://api.telegram.org/bot" f"{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )

            response.raise_for_status()

            data = response.json()

            if not data.get("ok"):
                logger.error(
                    "Telegram bot response failed: %s",
                    data,
                )
                return False

            logger.info("Telegram bot response sent successfully.")

            return True

        except requests.RequestException:
            logger.exception("Failed to send Telegram bot response.")
            return False

        except Exception:
            logger.exception("Unexpected error sending Telegram bot response.")
            return False


telegram_bot_service = TelegramBotService()
