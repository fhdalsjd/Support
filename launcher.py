"""Run the Telegram bot, live dashboard, smart exits, and auto-sniper in one Railway process."""
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

    # Import after bot.py so all trading checks share the same module instances
    # and asyncio event loop as Telegram polling.
    import sniper
    import smart_sl
    from trading import sell_token

    # Hard TP/SL must never sell unrelated tokens that happen to be in the same
    # wallet. Replace the bot helper with a recorded-position bounded exit.
    async def safe_auto_close(context, admin_id, mint, pos, reason):
        try:
            wallet_balance = await bot_app.wallet.get_token_balance(mint)
            recorded_balance = max(0.0, float(pos.amount_tokens or 0.0))
            balance = min(wallet_balance, recorded_balance)
            if balance <= 0:
                if wallet_balance <= 0:
                    await bot_app.store.remove_position(mint)
                return
            raw_units = int(balance * (10 ** pos.decimals))
            if raw_units <= 0:
                return
            result = await sell_token(mint, raw_units, pos.decimals)
            if result.success:
                await bot_app.store.remove_position(mint)
                await context.bot.send_message(
                    admin_id,
                    f"🤖 Auto-closed *${pos.symbol}* — {reason}\nTx: `{result.signature}`",
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(admin_id, f"⚠️ Auto-close FAILED for ${pos.symbol}: {result.error}")
        except Exception as exc:
            log.warning("Safe auto-close failed: %s", type(exc).__name__)

    bot_app._auto_close = safe_auto_close
    original_tp_sl_daemon = bot_app.tp_sl_daemon

    async def combined_daemon(context):
        # Tighten smart protection first, then evaluate hard TP/SL, then scan
        # for a new auto-trade. All three execute sequentially in one loop.
        await smart_sl.tick(context)
        await original_tp_sl_daemon(context)
        await sniper.tick(context)

    bot_app.tp_sl_daemon = combined_daemon

    dashboard.set_bot(bot_app)
    dashboard_thread = threading.Thread(
        target=run_dashboard,
        name="web-dashboard",
        daemon=True,
    )
    dashboard_thread.start()

    log.info("Starting Telegram polling + smart SL + TP/SL + guarded auto-sniper")
    bot_app.main()
