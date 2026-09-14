from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from core.config import Settings
from core.logger import get_logger

logger = get_logger("telegram_bot")

class ProjectUpdateBot:
    def __init__(self, settings: Settings, manager):
        self.settings, self.manager = settings, manager
        self.application = Application.builder().token(settings.telegram_bot_token).build() if settings.telegram_bot_token else None
        if self.application:
            self.application.add_handler(CommandHandler("start", self.menu))
            self.application.add_handler(CommandHandler("projects", self.menu))
            self.application.add_handler(CallbackQueryHandler(self.on_update, pattern=r"^update:"))

    def authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and self.settings.allowed_chat_id is not None and chat.id == self.settings.allowed_chat_id)

    async def reject(self, update: Update):
        if update.callback_query:
            await update.callback_query.answer("Unauthorized", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Unauthorized chat.")

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update): return await self.reject(update)
        keyboard = [[InlineKeyboardButton(f"Update {name}", callback_data=f"update:{name}")] for name in sorted(self.settings.projects)]
        await update.effective_message.reply_text("Select a project to update:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def on_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update): return await self.reject(update)
        query = update.callback_query
        await query.answer()
        project = query.data.split(":", 1)[1]
        try:
            job = self.manager.submit(project, requested_by=f"telegram:{update.effective_chat.id}", chat_id=update.effective_chat.id)
            await query.edit_message_text(f"Queued {project}\nJob: {job.job_id}")
        except KeyError:
            await query.edit_message_text("Unknown project.")

    async def start(self):
        if not self.application:
            logger.warning("Telegram bot disabled because TELEGRAM_BOT_TOKEN is empty")
            return
        await self.application.initialize(); await self.application.start(); await self.application.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling started")

    async def stop(self):
        if self.application:
            await self.application.updater.stop(); await self.application.stop(); await self.application.shutdown()
