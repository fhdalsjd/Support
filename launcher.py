"""Run the Telegram bot and live web dashboard in one Railway process."""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

import dashboard


log = logging.getLogger("launcher")
BASE_DIR = Path(__file__).resolve().parent
BOT_PATH = BASE_DIR / "bot.py"


def load_bot_module():
    """Load this project's bot.py explicitly, avoiding a possible package named 'bot'."""
    spec = importlib.util.spec_from_file_location("support_bot_app", BOT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load bot module from {BOT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_dashboard() -> None:
    try:
        dashboard.serve()
    except Exception:
        log.exception("Web dashboard stopped unexpectedly")
        raise


if __name__ == "__main__":
    bot_app = load_bot_module()
    log.info("Loaded Telegram bot from %s; main=%s", BOT_PATH, hasattr(bot_app, "main"))

    # Dashboard runs in the background; Telegram polling stays in the Railway
    # main thread so python-telegram-bot can manage signals normally.
    dashboard.set_bot(bot_app)
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        name="web-dashboard",
        daemon=True,
    )
    dashboard_thread.start()

    log.info("Starting Telegram bot polling in the Railway main thread")
    bot_app.main()
