"""Entry point: wires up the database, Telethon client, bot handlers, and scheduler."""
from __future__ import annotations
import logging
from telegram.ext import Application as TelegramApplication
from .config import settings
from .database import init_db
from .handlers import admin, user
from .scheduler import start_scheduler
from .telethon_client import inspector
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger=logging.getLogger(__name__)
async def _post_init(telegram_app:TelegramApplication)->None:
    await inspector.start(); start_scheduler(telegram_app); logger.info("Bot fully initialized. Admin ids: %s",settings.admin_ids)
async def _post_shutdown(telegram_app:TelegramApplication)->None: await inspector.stop()
def main()->None:
    init_db(); telegram_app=(TelegramApplication.builder().token(settings.bot_token).post_init(_post_init).post_shutdown(_post_shutdown).build()); user.register(telegram_app); admin.register(telegram_app); logger.info("Starting bot (polling mode)..."); telegram_app.run_polling(allowed_updates=["message","callback_query"])
if __name__=="__main__": main()
