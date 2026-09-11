"""Run the Telegram bot and live web dashboard in one Railway process."""
from __future__ import annotations

import logging
import threading

import bot
import dashboard


log = logging.getLogger("launcher")


def run_dashboard() -> None:
    try:
        dashboard.serve()
    except Exception:
        log.exception("Web dashboard stopped unexpectedly")
        raise


if __name__ == "__main__":
    # Keep the dashboard in a background thread. Telegram polling stays in the
    # Railway main thread so python-telegram-bot can install/manage signal
    # handlers normally and receive updates reliably.
    dashboard.set_bot(bot)
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        name="web-dashboard",
        daemon=True,
    )
    dashboard_thread.start()

    log.info("Starting Telegram bot polling in the Railway main thread")
    bot.main()
