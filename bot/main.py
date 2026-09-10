"""Entry point: wires up the database, Telethon client, bot handlers, and scheduler."""
from __future__ import annotations
import logging
from telegram import Update
from telegram.ext import Application as TelegramApplication, ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from .config import settings
from .database import init_db, is_user_blocked, session_scope
from .handlers import admin, user
from .scheduler import start_scheduler
from .telethon_client import inspector
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger=logging.getLogger(__name__)

async def _moderation_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    if not telegram_user or telegram_user.id in settings.admin_ids:
        return
    with session_scope() as session:
        blocked = is_user_blocked(session, telegram_user.id)
    if blocked:
        if update.callback_query:
            await update.callback_query.answer("Your access to this bot is currently restricted.", show_alert=True)
        elif update.message:
            await update.message.reply_text("🚫 <b>Access restricted</b>\n\nYour access to this bot is currently restricted by the moderation team.", parse_mode="HTML")
        raise ApplicationHandlerStop

async def _post_init(telegram_app:TelegramApplication)->None:
    await inspector.start(); start_scheduler(telegram_app); logger.info("Bot fully initialized. Admin ids: %s",settings.admin_ids)
async def _post_shutdown(telegram_app:TelegramApplication)->None: await inspector.stop()
def main()->None:
    init_db(); telegram_app=(TelegramApplication.builder().token(settings.bot_token).post_init(_post_init).post_shutdown(_post_shutdown).build()); telegram_app.add_handler(MessageHandler(filters.ALL,_moderation_guard),group=-1); telegram_app.add_handler(CallbackQueryHandler(_moderation_guard),group=-1); user.register(telegram_app); admin.register(telegram_app); logger.info("Starting bot (polling mode)..."); telegram_app.run_polling(allowed_updates=["message","callback_query"])
if __name__=="__main__": main()
