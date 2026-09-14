import asyncio
import json
import urllib.parse
import urllib.request

from core.config import Settings
from core.logger import get_logger


logger = get_logger("telegram_bot")


class ProjectUpdateBot:
    def __init__(self, settings: Settings, manager):
        self.settings = settings
        self.manager = manager

        self.token = settings.telegram_bot_token
        self.base_url = (
            f"https://api.telegram.org/bot{self.token}"
            if self.token
            else None
        )

        self.running = False
        self.polling_task = None
        self.offset = None

    # ============================================================
    # Telegram API
    # ============================================================

    async def telegram_api(self, method: str, data: dict | None = None):
        if not self.base_url:
            return None

        url = f"{self.base_url}/{method}"

        if data:
            encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        else:
            encoded_data = None

        def request():
            req = urllib.request.Request(
                url,
                data=encoded_data,
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(request)

    # ============================================================
    # Authorization
    # ============================================================

    def authorized(self, chat_id) -> bool:
        return bool(
            chat_id
            and self.settings.allowed_chat_id is not None
            and int(chat_id) == int(self.settings.allowed_chat_id)
        )

    # ============================================================
    # Send Message
    # ============================================================

    async def send_message(
        self,
        chat_id,
        text: str,
        reply_markup: dict | None = None,
    ):
        data = {
            "chat_id": chat_id,
            "text": text,
        }

        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        return await self.telegram_api("sendMessage", data)

    # ============================================================
    # Answer Callback Query
    # ============================================================

    async def answer_callback(self, callback_id, text=None, show_alert=False):
        data = {
            "callback_query_id": callback_id,
            "show_alert": str(show_alert).lower(),
        }

        if text:
            data["text"] = text

        return await self.telegram_api("answerCallbackQuery", data)

    # ============================================================
    # Edit Message
    # ============================================================

    async def edit_message(
        self,
        chat_id,
        message_id,
        text: str,
    ):
        return await self.telegram_api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            },
        )

    # ============================================================
    # Project Menu
    # ============================================================

    async def menu(self, chat_id):
        if not self.authorized(chat_id):
            await self.send_message(
                chat_id,
                "Unauthorized chat.",
            )
            return

        keyboard = [
            [
                {
                    "text": f"Update {name}",
                    "callback_data": f"update:{name}",
                }
            ]
            for name in sorted(self.settings.projects)
        ]

        reply_markup = {
            "inline_keyboard": keyboard
        }

        await self.send_message(
            chat_id,
            "Select a project to update:",
            reply_markup=reply_markup,
        )

    # ============================================================
    # Handle Update Button
    # ============================================================

    async def on_update(self, callback_query):
        callback_id = callback_query["id"]

        message = callback_query.get("message", {})
        chat = message.get("chat", {})

        chat_id = chat.get("id")

        if not self.authorized(chat_id):
            await self.answer_callback(
                callback_id,
                "Unauthorized",
                show_alert=True,
            )
            return

        await self.answer_callback(callback_id)

        data = callback_query.get("data", "")

        if not data.startswith("update:"):
            return

        project = data.split(":", 1)[1]

        try:
            job = self.manager.submit(
                project,
                requested_by=f"telegram:{chat_id}",
                chat_id=chat_id,
            )

            await self.edit_message(
                chat_id,
                message.get("message_id"),
                f"Queued {project}\nJob: {job.job_id}",
            )

        except KeyError:
            await self.edit_message(
                chat_id,
                message.get("message_id"),
                "Unknown project.",
            )

        except Exception:
            logger.exception(
                "Failed to submit project update: %s",
                project,
            )

            await self.edit_message(
                chat_id,
                message.get("message_id"),
                "Failed to queue project update.",
            )

    # ============================================================
    # Handle Incoming Telegram Update
    # ============================================================

    async def handle_update(self, update):
        # --------------------------------------------------------
        # Normal message
        # --------------------------------------------------------

        message = update.get("message")

        if message:
            chat = message.get("chat", {})
            chat_id = chat.get("id")

            text = message.get("text", "")

            if text in ("/start", "/projects"):
                await self.menu(chat_id)

            return

        # --------------------------------------------------------
        # Callback query from inline button
        # --------------------------------------------------------

        callback_query = update.get("callback_query")

        if callback_query:
            await self.on_update(callback_query)

    # ============================================================
    # Poll Telegram
    # ============================================================

    async def poll(self):
        logger.info("Telegram polling started")

        while self.running:
            try:
                data = {
                    "timeout": 30,
                }

                if self.offset is not None:
                    data["offset"] = self.offset

                response = await self.telegram_api(
                    "getUpdates",
                    data,
                )

                if not response or not response.get("ok"):
                    logger.warning(
                        "Telegram getUpdates failed: %s",
                        response,
                    )

                    await asyncio.sleep(5)
                    continue

                updates = response.get("result", [])

                for update in updates:
                    update_id = update.get("update_id")

                    if update_id is not None:
                        self.offset = update_id + 1

                    try:
                        await self.handle_update(update)

                    except Exception:
                        logger.exception(
                            "Error handling Telegram update"
                        )

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception(
                    "Telegram polling error"
                )

                await asyncio.sleep(5)

        logger.info("Telegram polling stopped")

    # ============================================================
    # Start
    # ============================================================

    async def start(self):
        if not self.token:
            logger.warning(
                "Telegram bot disabled because "
                "TELEGRAM_BOT_TOKEN is empty"
            )
            return

        # --------------------------------------------------------
        # Verify bot token
        # --------------------------------------------------------

        response = await self.telegram_api("getMe")

        if not response or not response.get("ok"):
            logger.error(
                "Unable to connect to Telegram API: %s",
                response,
            )
            return

        bot_info = response.get("result", {})

        logger.info(
            "Telegram bot connected: @%s",
            bot_info.get("username"),
        )

        self.running = True

        self.polling_task = asyncio.create_task(
            self.poll()
        )

    # ============================================================
    # Stop
    # ============================================================

    async def stop(self):
        if not self.running:
            return

        logger.info("Stopping Telegram bot...")

        self.running = False

        if self.polling_task:
            self.polling_task.cancel()

            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass

            self.polling_task = None

        logger.info("Telegram bot stopped")
