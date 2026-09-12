"""Run the Telegram bot, dashboard, exits, and guarded auto-sniper together."""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger("launcher")
BASE_DIR = Path(__file__).resolve().parent
BOT_PATH = BASE_DIR / "trading_bot.py"
SITECUSTOMIZE_PATH = BASE_DIR / "sitecustomize.py"


def _load_sitecustomize_by_path() -> None:
    """Load project sitecustomize.py explicitly before anything else imports security."""
    if not SITECUSTOMIZE_PATH.exists():
        log.warning("sitecustomize.py not found at %s -- live-pricing patch not applied", SITECUSTOMIZE_PATH)
        return
    spec = importlib.util.spec_from_file_location("_project_sitecustomize", SITECUSTOMIZE_PATH)
    if spec is None or spec.loader is None:
        log.warning("Could not load sitecustomize.py from %s", SITECUSTOMIZE_PATH)
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log.info("Loaded project sitecustomize.py explicitly (live-pricing patch active)")


_load_sitecustomize_by_path()

import dashboard
import trading_bot


def run_dashboard():
    dashboard.serve()


if __name__ == "__main__":
    dashboard.set_bot(trading_bot)
    threading.Thread(target=run_dashboard, name="web-dashboard", daemon=True).start()
    log.info("Starting Telegram polling + smart SL + guarded auto-sniper")
    trading_bot.main()
