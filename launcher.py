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

    import sniper
    import smart_sl
    from trading import sell_token

    # Never let an exit sell unrelated tokens held in the same wallet.
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
                await context.bot.send_message(admin_id, f"🤖 Auto-closed *${pos.symbol}* — {reason}\nTx: `{result.signature}`", parse_mode="Markdown")
            else:
                await context.bot.send_message(admin_id, f"⚠️ Auto-close FAILED for ${pos.symbol}: {result.error}")
        except Exception as exc:
            log.warning("Safe auto-close failed: %s", type(exc).__name__)

    # Replace the original helpers before Telegram starts; callback handlers
    # resolve these globals at runtime, so both manual and daemon exits use them.
    bot_app._auto_close = safe_auto_close

    async def safe_do_sell(update, context, mint, fraction):
        chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
        if not bot_app.wallet.configured:
            await context.bot.send_message(chat_id, "🔌 No wallet connected. Use /connect_wallet first.")
            return
        if not 0 < fraction <= 1:
            await context.bot.send_message(chat_id, "❌ Close percentage must be between 0.01% and 100%.")
            return
        notice = await context.bot.send_message(chat_id, f"⏳ Selling {fraction * 100:g}% of the recorded position...")
        try:
            positions = await bot_app.store.get_positions()
            pos = positions.get(mint)
            if not pos:
                await notice.edit_text("No recorded position for that token.")
                return
            wallet_balance = await bot_app.wallet.get_token_balance(mint)
            recorded_balance = max(0.0, float(pos.amount_tokens or 0.0))
            sell_balance = min(wallet_balance, recorded_balance) * fraction
            raw_units = int(sell_balance * (10 ** pos.decimals))
            if raw_units <= 0:
                await notice.edit_text("❌ Close amount is too small for the token precision.")
                return
            result = await sell_token(mint, raw_units, pos.decimals)
            if not result.success:
                await notice.edit_text(result.error or "❌ Sell failed")
                return
            await bot_app.store.reduce_position_amount(mint, raw_units / (10 ** pos.decimals))
            label = "Closed 100%" if fraction >= 0.999999 else f"Sold {fraction * 100:g}%"
            await notice.edit_text(f"✅ {label} of ${pos.symbol}\nTx: `{result.signature}`", parse_mode="Markdown")
        except Exception as exc:
            log.exception("Manual sell failed: %s", type(exc).__name__)
            await notice.edit_text("❌ Sell failed due to a temporary error.")

    bot_app.do_sell = safe_do_sell

    async def safe_do_buy(update, context, mint, sol_amount):
        chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
        if not bot_app.wallet.configured:
            await context.bot.send_message(chat_id, "🔌 No wallet connected. Use /connect_wallet first.")
            return
        if sol_amount <= 0 or sol_amount > bot_app.settings.max_buy_sol:
            await context.bot.send_message(chat_id, f"❌ Buy must be greater than 0 and no more than {bot_app.settings.max_buy_sol} SOL.")
            return
        notice = await context.bot.send_message(chat_id, f"⏳ Preflighting buy of `{sol_amount:.9f} SOL`...", parse_mode="Markdown")
        try:
            overview = await bot_app.get_token_overview(mint)
            before = await bot_app.wallet.get_token_balance(mint)
            result = await bot_app.buy_token(mint, sol_amount)
            if not result.success:
                await notice.edit_text(result.error or "❌ Buy failed")
                return
            after = await bot_app.wallet.get_token_balance(mint)
            token_delta = max(0.0, after - before)
            if token_delta <= 0:
                await notice.edit_text("⚠️ Buy confirmed but token balance did not increase; position was not recorded. Check the transaction before trading it.")
                return
            try:
                decimals = (await bot_app.wallet.client.get_token_supply(bot_app.Pubkey.from_string(mint))).value.decimals
            except Exception:
                decimals = 9
            st = await bot_app.store.get_settings()
            old_positions = await bot_app.store.get_positions()
            old = old_positions.get(mint)
            if old:
                total_tokens = old.amount_tokens + token_delta
                weighted_entry = ((old.entry_price_usd * old.amount_tokens) + (overview.price_usd * token_delta)) / total_tokens
                old.amount_tokens = total_tokens
                old.entry_price_usd = weighted_entry
                old.entry_sol = (old.entry_sol or 0.0) + sol_amount
                old.decimals = decimals
                await bot_app.store.upsert_position(old)
            else:
                pos = bot_app.Position(
                    mint=mint,
                    symbol=overview.symbol,
                    entry_price_usd=overview.price_usd,
                    amount_tokens=token_delta,
                    decimals=decimals,
                    take_profit_pct=st["default_tp_pct"],
                    stop_loss_pct=st["default_sl_pct"],
                    entry_sol=sol_amount,
                )
                await bot_app.store.upsert_position(pos)
            await notice.edit_text(f"✅ *Bought ${overview.symbol}*\nAmount: `{sol_amount:.9f} SOL`\nTokens received: `{token_delta:.8g}`\nTx: `{result.signature}`\n\n📊 Position accounting is based on the actual token balance delta.", parse_mode="Markdown")
        except Exception as exc:
            log.exception("Buy failed: %s", type(exc).__name__)
            await notice.edit_text("❌ Buy failed due to a temporary error. No wallet credential was exposed.")

    bot_app.do_buy = safe_do_buy

    original_tp_sl_daemon = bot_app.tp_sl_daemon

    async def combined_daemon(context):
        # Tighten smart protection first, then hard TP/SL, then scan for a new
        # auto-trade. They execute sequentially in the Telegram event loop.
        await smart_sl.tick(context)
        await original_tp_sl_daemon(context)
        await sniper.tick(context)

    bot_app.tp_sl_daemon = combined_daemon

    dashboard.set_bot(bot_app)
    dashboard_thread = threading.Thread(target=run_dashboard, name="web-dashboard", daemon=True)
    dashboard_thread.start()

    log.info("Starting Telegram polling + smart SL + TP/SL + guarded auto-sniper")
    bot_app.main()
