import queue
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

        # -------------------------------------------------
        # Background Telegram message sender
        # -------------------------------------------------
        #
        # Telegram messages are queued here instead of
        # performing the HTTP request directly from the
        # polling / command handling thread.
        #
        self._message_queue: queue.Queue[str | None] = queue.Queue()
        self._sender_running = False
        self._sender_thread: threading.Thread | None = None

        self._sender_lock = threading.Lock()

    # =====================================================
    # STARTUP MESSAGE
    # =====================================================

    def send_startup_message(self) -> bool:
        """
        Queue application startup status for background sending.

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

    # =====================================================
    # BOT START
    # =====================================================

    def start(self) -> None:
        if not settings.run_tele_bot:
            logger.info(
                "Telegram bot is disabled via RUN_TELE_BOT configuration."
            )
            return

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Telegram bot not started. "
                "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
            )
            return

        if self.running:
            logger.warning("Telegram bot is already running.")
            return

        # -------------------------------------------------
        # Start background message sender first
        # -------------------------------------------------
        self._start_sender()

        self.running = True

        self.thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-bot",
            daemon=True,
        )

        self.thread.start()

        logger.info("Telegram bot started.")

    # =====================================================
    # BOT STOP
    # =====================================================

    def stop(self) -> None:
        """
        Stop Telegram bot polling.

        The background sender is intentionally allowed to
        finish queued messages so messages already accepted
        by the application are not unnecessarily discarded.
        """

        if not self.running:
            logger.info("Telegram bot is already stopped.")
            return

        self.running = False

        logger.info("Telegram bot stopped.")

    # =====================================================
    # BACKGROUND MESSAGE SENDER
    # =====================================================

    def _start_sender(self) -> None:
        """
        Start the background Telegram message sender.

        Only one sender thread is created.
        """

        with self._sender_lock:
            if self._sender_running:
                return

            self._sender_running = True

            self._sender_thread = threading.Thread(
                target=self._message_sender_loop,
                name="telegram-message-sender",
                daemon=True,
            )

            self._sender_thread.start()

            logger.info("Telegram background message sender started.")

    def _message_sender_loop(self) -> None:
        """
        Continuously process queued Telegram messages.

        This runs independently from the Telegram polling thread,
        so a slow Telegram API request cannot block command polling.
        """

        while self._sender_running:
            try:
                message = self._message_queue.get()

                # None is reserved as a shutdown sentinel.
                if message is None:
                    self._message_queue.task_done()
                    break

                try:
                    self._send_message_http(message)

                except Exception:
                    logger.exception(
                        "Unexpected error in background Telegram "
                        "message sender."
                    )

                finally:
                    self._message_queue.task_done()

            except Exception:
                logger.exception(
                    "Unexpected error in Telegram message queue."
                )

        logger.info("Telegram background message sender stopped.")

    def _send_message_http(self, message: str) -> bool:
        """
        Perform the actual blocking HTTP request to Telegram.

        IMPORTANT:
        This method is only called by the background sender thread.
        """

        if not settings.run_tele_bot:
            logger.info(
                "Telegram bot disabled. Message ignored."
            )
            return False

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Cannot send Telegram bot message. "
                "Bot token or chat ID is missing."
            )
            return False

        url = (
            f"https://api.telegram.org/bot"
            f"{self.bot_token}/sendMessage"
        )

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

            logger.info(
                "Telegram bot response sent successfully."
            )

            return True

        except requests.RequestException:
            logger.exception(
                "Failed to send Telegram bot response."
            )
            return False

        except Exception:
            logger.exception(
                "Unexpected error sending Telegram bot response."
            )
            return False

    # =====================================================
    # TELEGRAM POLLING
    # =====================================================

    def _poll_loop(self) -> None:
        """
        Continuously poll Telegram for new messages.

        IMPORTANT:
        No blocking Telegram send request is performed here.
        Messages are only added to the background queue.
        """

        url = (
            f"https://api.telegram.org/bot"
            f"{self.bot_token}/getUpdates"
        )

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
                logger.exception(
                    "Telegram polling request failed."
                )

                time.sleep(5)

            except Exception:
                logger.exception(
                    "Unexpected Telegram bot error."
                )

                time.sleep(5)

    # =====================================================
    # UPDATE HANDLER
    # =====================================================

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
                "Ignoring Telegram command from "
                "unauthorized chat_id=%s",
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
            logger.info(
                "Received Telegram command: /start"
            )

            self._send_status_message()

        # -------------------------------------------------
        # /enable_telegram
        # -------------------------------------------------
        elif command == "/enable_telegram":

            logger.info(
                "Received Telegram command: "
                "/enable_telegram"
            )

            if settings.tele_flg:
                logger.info(
                    "Telegram sending is already enabled."
                )

                self._send_direct_message(
                    "ℹ️ "
                    "<b>Telegram sending is already ENABLED.</b>"
                )

                return

            settings.tele_flg = True

            logger.info(
                "Telegram sending ENABLED."
            )

            self._send_direct_message(
                "✅ "
                "<b>Telegram sending ENABLED.</b>"
            )

        # -------------------------------------------------
        # /disable_telegram
        # -------------------------------------------------
        elif command == "/disable_telegram":

            logger.info(
                "Received Telegram command: "
                "/disable_telegram"
            )

            if not settings.tele_flg:
                logger.info(
                    "Telegram sending is already disabled."
                )

                self._send_direct_message(
                    "ℹ️ "
                    "<b>Telegram sending is already DISABLED.</b>"
                )

                return

            # Change the flag first.
            settings.tele_flg = False

            logger.info(
                "Telegram sending DISABLED."
            )

            # Direct sender is used because normal
            # Telegram sending now sees TELE_FLG=False.
            self._send_direct_message(
                "🔕 "
                "<b>Telegram sending DISABLED.</b>"
            )

        # -------------------------------------------------
        # /enable_test
        # -------------------------------------------------
        elif command == "/enable_test":

            logger.info(
                "Received Telegram command: "
                "/enable_test"
            )

            if settings.test_flg:
                logger.info(
                    "Test mode is already enabled."
                )

                self._send_direct_message(
                    "ℹ️ "
                    "<b>Test mode is already ENABLED.</b>"
                )

                return

            settings.test_flg = True

            logger.info(
                "Test mode ENABLED."
            )

            self._send_direct_message(
                "🧪 <b>TEST MODE ENABLED.</b>\n\n"
                "Any JSON payload will now be accepted "
                "and stored as received."
            )

        # -------------------------------------------------
        # /disable_test
        # -------------------------------------------------
        elif command == "/disable_test":

            logger.info(
                "Received Telegram command: "
                "/disable_test"
            )

            if not settings.test_flg:
                logger.info(
                    "Test mode is already disabled."
                )

                self._send_direct_message(
                    "ℹ️ "
                    "<b>Test mode is already DISABLED.</b>"
                )

                return

            settings.test_flg = False

            logger.info(
                "Test mode DISABLED."
            )

            self._send_direct_message(
                "🛡️ <b>TEST MODE DISABLED.</b>\n\n"
                "Payloads must now match the expected schema."
            )

        # -------------------------------------------------
        # /status
        # -------------------------------------------------
        elif command == "/status":

            logger.info(
                "Received Telegram command: /status"
            )

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

    # =====================================================
    # STATUS MESSAGE
    # =====================================================

    def _send_status_message(self) -> None:
        """
        Queue current application control status
        for background sending.
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

    # =====================================================
    # HELP MESSAGE
    # =====================================================

    def _send_help_message(self) -> None:
        """
        Queue available Telegram commands
        for background sending.
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

    # =====================================================
    # PUBLIC / NON-BLOCKING MESSAGE SENDER
    # =====================================================

    def _send_direct_message(
        self,
        message: str,
    ) -> bool:
        """
        Queue a Telegram message for background delivery.

        IMPORTANT:
        This method NO LONGER performs the HTTP request directly.

        It only places the message into the queue and returns
        immediately.

        Therefore:
            - Telegram API delays do not block command processing.
            - Other application actions can continue.
            - Multiple messages are processed by the background
              sender independently from the polling loop.

        Return value:
            True  = message successfully queued.
            False = message could not be queued because the bot
                    configuration is invalid/disabled.
        """

        if not settings.run_tele_bot:
            logger.info(
                "Telegram bot disabled. Message ignored."
            )
            return False

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Cannot queue Telegram bot message. "
                "Bot token or chat ID is missing."
            )
            return False

        # Make sure the background sender exists.
        self._start_sender()

        try:
            self._message_queue.put_nowait(message)

            logger.info(
                "Telegram message queued for background sending."
            )

            return True

        except queue.Full:
            logger.error(
                "Telegram message queue is full. "
                "Message was not queued."
            )
            return False

        except Exception:
            logger.exception(
                "Unexpected error queueing Telegram message."
            )
            return False


# =========================================================
# GLOBAL TELEGRAM BOT SERVICE
# =========================================================

telegram_bot_service = TelegramBotService()