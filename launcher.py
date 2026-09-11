"""Run the Telegram bot and the live web dashboard in one Railway process."""
from __future__ import annotations

import threading

from telegram.ext import Application

import bot
import dashboard


# python-telegram-bot normally installs OS signal handlers from the thread
# running run_polling(). Railway's main process owns the signal handlers, so
# disable PTB's optional handlers when the bot runs in a background thread.
_original_run_polling = Application.run_polling


def _run_polling_without_thread_signals(self, *args, **kwargs):
    kwargs["stop_signals"] = None
    return _original_run_polling(self, *args, **kwargs)


Application.run_polling = _run_polling_without_thread_signals


def run_bot():
    try:
        bot.main()
    except Exception:
        bot.log.exception("Telegram bot stopped unexpectedly")


if __name__ == "__main__":
    dashboard.set_bot(bot)
    thread = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
    thread.start()
    dashboard.serve()
