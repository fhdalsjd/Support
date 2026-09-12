"""Run the Telegram bot, dashboard, exits, and guarded auto-sniper together."""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("launcher")
BASE_DIR = Path(__file__).resolve().parent
BOT_PATH = BASE_DIR / "trading_bot.py"
SITECUSTOMIZE_PATH = BASE_DIR / "sitecustomize.py"


def _load_sitecustomize_by_path() -> None:
    """
    Load this project's sitecustomize.py by explicit file path, unconditionally,
    before anything else in this process imports `security`.

    Two separate problems make relying on Python's automatic sitecustomize
    mechanism unsafe here:

    1. A `sitecustomize.py` sitting next to an entry script is NOT auto-loaded
       just by running `python launcher.py`. The `site` module only tries
       `import sitecustomize` using sys.path as it exists *during interpreter
       startup* -- which happens BEFORE the script's own directory is added
       to sys.path. Without PYTHONPATH pointing at this directory, this
       project's sitecustomize.py was silently never imported at all.
    2. Even where a `sitecustomize` module import *does* succeed automatically
       (e.g. many Debian-based Python images, including python:3.11-slim-bookworm,
       ship their own /usr/.../sitecustomize.py for dist-packages setup), that
       unrelated module gets cached in sys.modules under the same name first --
       so a later bare `import sitecustomize` would silently return THAT
       module instead of this project's, since Python only ever imports one
       module per name.

    Loading by explicit file path sidesteps both: it doesn't depend on
    sys.path timing and can't be shadowed by an unrelated same-named module.

    This must run before `load_bot_module()` (and before importing sniper /
    smart_sl), because `security.get_token_overview` is imported by name
    (`import security`, called as `security.get_token_overview(...)`) in
    trading_bot.py and smart_sl.py. The monkeypatch this file installs
    (`security.get_token_overview = live_overview`) only affects code that
    looks the function up on the `security` module *after* the patch is
    applied -- so ordering here directly determines whether every token paste
    gets the robust multi-source/live-pricing path or silently falls back to
    the bare DexScreener-only lookup (which returns "found=False" for any
    token DexScreener hasn't indexed yet -- i.e. most brand-new pump.fun
    tokens, which is exactly what an auto-sniper pastes/buys).
    """
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def load_bot_module():
    spec = importlib.util.spec_from_file_location("support_bot_app", BOT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load bot module from {BOT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_dashboard():
    dashboard.serve()


def _snapshot(overview, source="manual", allocation_pct=None, trade_amount=None, price_impact=None, risk=None, rug=None):
    return {
        "source": source, "allocation_pct": allocation_pct, "trade_amount_sol": trade_amount,
        "price_impact_pct": price_impact, "risk_score": getattr(risk, "risk_score", None),
        "risk_level": getattr(rug, "risk_level", None), "rug_notes": list(getattr(rug, "notes", []) or []),
        "price_usd": overview.price_usd, "market_cap_usd": overview.market_cap, "fdv_usd": overview.fdv,
        "liquidity_usd": overview.liquidity_usd, "volume_5m_usd": overview.volume_5m,
        "volume_24h_usd": overview.volume_24h, "trades_5m": overview.total_trades_5m,
        "trades_24h": overview.total_trades_24h, "buy_sell_ratio_5m": overview.buy_sell_ratio_5m,
        "change_5m_pct": overview.change_5m, "change_1h_pct": overview.change_1h,
        "pool_age_minutes": overview.age_minutes,
        "primary_pool_age_minutes": getattr(overview, "primary_pool_age_minutes", None),
        "oldest_pool_age_minutes": getattr(overview, "oldest_pool_age_minutes", None),
        "pool_count": overview.pool_count, "dex": overview.dex, "decimals": overview.decimals,
        "total_supply": overview.total_supply, "mint_authority": getattr(overview, "mint_authority", None),
        "freeze_authority": getattr(overview, "freeze_authority", None),
        "top_holder_pct": getattr(overview, "top_holder_pct", None), "data_quality": overview.data_quality,
        "data_warnings": list(getattr(overview, "data_warnings", []) or []),
        "market_cap_source": getattr(overview, "market_cap_source", None),
        "data_source": getattr(overview, "data_source", None), "fetched_at": getattr(overview, "fetched_at", None),
    }


async def _save_closed_trade(bot_app, pos, mint, signature, reason, exit_price=None, exit_tokens=None):
    if exit_price is None:
        try:
            ov = await bot_app.get_token_overview(mint)
            exit_price = ov.price_usd if ov.found else None
        except Exception:
            pass
    entry = float(pos.entry_price_usd or 0)
    pnl_pct = ((exit_price - entry) / entry * 100) if exit_price and entry else None
    entry_sol = float(pos.entry_sol or 0)
    record = {
        "closed_at": time.time(), "opened_at": pos.opened_at or None, "mint": mint, "symbol": pos.symbol,
        "decimals": pos.decimals, "entry_price_usd": entry, "exit_price_usd": exit_price,
        "entry_sol": entry_sol, "pnl_pct": pnl_pct,
        "pnl_sol_estimate": entry_sol * pnl_pct / 100 if pnl_pct is not None else None,
        "amount_tokens": pos.amount_tokens, "exit_tokens": exit_tokens if exit_tokens is not None else pos.amount_tokens,
        "take_profit_pct": pos.take_profit_pct, "stop_loss_pct": pos.stop_loss_pct,
        "peak_profit_pct": pos.peak_profit_pct, "smart_stop_profit_pct": pos.smart_stop_profit_pct,
        "close_reason": reason, "buy_signature": pos.buy_signature, "sell_signature": signature,
        "entry_snapshot": dict(pos.entry_snapshot or {}), "close_events": list(pos.close_events or []),
    }
    await bot_app.store.append_history(record)
    return pnl_pct


if __name__ == "__main__":
    bot_app = load_bot_module()
    import sniper
    import smart_sl
    from solders.pubkey import Pubkey
    from trading import sell_token

    async def safe_auto_close(context, admin_id, mint, pos, reason):
        try:
            wallet_balance = await bot_app.wallet.get_token_balance(mint)
            balance = min(wallet_balance, max(0.0, float(pos.amount_tokens or 0.0)))
            if balance <= 0:
                if wallet_balance <= 0:
                    await bot_app.store.remove_position(mint)
                return
            raw_units = int(balance * (10 ** pos.decimals))
            if raw_units <= 0:
                return
            result = await sell_token(mint, raw_units, pos.decimals)
            if not result.success:
                await context.bot.send_message(admin_id, f"⚠️ Auto-close FAILED for ${pos.symbol}: {result.error}")
                return
            try:
                ov = await bot_app.get_token_overview(mint)
                exit_price = ov.price_usd if ov.found else None
            except Exception:
                exit_price = None
            pnl = await _save_closed_trade(bot_app, pos, mint, result.signature, reason, exit_price, balance)
            await bot_app.store.remove_position(mint)
            pnl_text = f"{pnl:+.2f}%" if pnl is not None else "unavailable"
            await context.bot.send_message(admin_id, f"🤖 Auto-closed *${pos.symbol}* — {reason}\nPnL: `{pnl_text}`\nTx: `{result.signature}`\n📚 Saved to History", parse_mode="Markdown")
        except Exception as exc:
            log.warning("Safe auto-close failed: %s", type(exc).__name__)

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
            pos = (await bot_app.store.get_positions()).get(mint)
            if not pos:
                await notice.edit_text("No recorded position for that token.")
                return
            wallet_balance = await bot_app.wallet.get_token_balance(mint)
            sell_balance = min(wallet_balance, max(0.0, float(pos.amount_tokens or 0.0))) * fraction
            raw_units = int(sell_balance * (10 ** pos.decimals))
            if raw_units <= 0:
                await notice.edit_text("❌ Close amount is too small for the token precision.")
                return
            result = await sell_token(mint, raw_units, pos.decimals)
            if not result.success:
                await notice.edit_text(result.error or "❌ Sell failed")
                return
            try:
                ov = await bot_app.get_token_overview(mint)
                exit_price = ov.price_usd if ov.found else None
            except Exception:
                exit_price = None
            close_event = {"closed_at": time.time(), "fraction": fraction, "tokens": sell_balance, "exit_price_usd": exit_price, "sell_signature": result.signature, "reason": "MANUAL_CLOSE"}
            pos.close_events = list(pos.close_events or []) + [close_event]
            if fraction >= 0.999999:
                pnl = await _save_closed_trade(bot_app, pos, mint, result.signature, "MANUAL_CLOSE", exit_price, sell_balance)
            else:
                pnl = ((exit_price - pos.entry_price_usd) / pos.entry_price_usd * 100) if exit_price and pos.entry_price_usd else None
                await bot_app.store.upsert_position(pos)
            await bot_app.store.reduce_position_amount(mint, sell_balance)
            label = "Closed 100%" if fraction >= 0.999999 else f"Sold {fraction * 100:g}%"
            pnl_text = f"\nPnL at exit: `{pnl:+.2f}%`" if pnl is not None else ""
            await notice.edit_text(f"✅ {label} of ${pos.symbol}{pnl_text}\nTx: `{result.signature}`\n\n📚 Saved/updated in trade history.", parse_mode="Markdown")
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
            if not overview.found or overview.price_usd <= 0:
                await notice.edit_text("❌ No reliable live market price is available for this token. Buy cancelled.")
                return
            before = await bot_app.wallet.get_token_balance(mint)
            result = await bot_app.buy_token(mint, sol_amount)
            if not result.success:
                await notice.edit_text(result.error or "❌ Buy failed")
                return
            after = await bot_app.wallet.get_token_balance(mint)
            token_delta = max(0.0, after - before)
            if token_delta <= 0:
                await notice.edit_text("⚠️ Buy confirmed but token balance did not increase; position was not recorded.")
                return
            try:
                decimals = (await bot_app.wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
            except Exception:
                decimals = overview.decimals or 9
            st = await bot_app.store.get_settings()
            old = (await bot_app.store.get_positions()).get(mint)
            snap = _snapshot(overview, trade_amount=sol_amount)
            if old:
                total = old.amount_tokens + token_delta
                old.entry_price_usd = ((old.entry_price_usd * old.amount_tokens) + (overview.price_usd * token_delta)) / total
                old.amount_tokens = total
                old.entry_sol = (old.entry_sol or 0) + sol_amount
                old.decimals = decimals
                old.entry_snapshot = {**(old.entry_snapshot or {}), "last_add": snap}
                await bot_app.store.upsert_position(old)
            else:
                pos = bot_app.Position(mint=mint, symbol=overview.symbol, entry_price_usd=overview.price_usd, amount_tokens=token_delta, decimals=decimals, take_profit_pct=st["default_tp_pct"], stop_loss_pct=st["default_sl_pct"], entry_sol=sol_amount, opened_at=time.time(), buy_signature=result.signature, entry_snapshot=snap)
                await bot_app.store.upsert_position(pos)
            await notice.edit_text(
                f"✅ *Position OPEN — ${overview.symbol}*\n\nCA / Mint: `{mint}`\nEntry: `${overview.price_usd:.10f}`\nTokens: `{token_delta:.8g}`\nSpend: `{sol_amount:.9f} SOL`\n"
                f"Liquidity: `${overview.liquidity_usd:,.0f}` • MC: `${overview.market_cap:,.0f}` • FDV: `${overview.fdv:,.0f}`\n"
                f"5m volume: `${overview.volume_5m:,.0f}` • 5m trades: `{overview.total_trades_5m}`\nRisk data: `{overview.data_quality}` • Pools: `{overview.pool_count}`\nTP: `+{st['default_tp_pct']}%` • Hard SL: `-{st['default_sl_pct']}%`\nBuy Tx: `{result.signature}`\n\n📚 Full entry snapshot saved.",
                parse_mode="Markdown")
        except Exception as exc:
            log.exception("Buy failed: %s", type(exc).__name__)
            await notice.edit_text("❌ Buy failed due to a temporary error. No wallet credential was exposed.")

    bot_app.do_buy = safe_do_buy

    original_callback_router = bot_app.callback_router

    async def guarded_callback_router(update, context):
        query = update.callback_query
        data = query.data or ""
        user = update.effective_user
        if not user or user.id not in bot_app.settings.admin_ids:
            await query.answer("Not authorized", show_alert=True)
            return
        if data == "toggle_sniper":
            await query.answer()
            st = await bot_app.store.get_settings()
            if st.get("auto_sniper_enabled"):
                await bot_app.store.set_setting("auto_sniper_enabled", False)
                await bot_app.store.set_setting("auto_sniper_allocation_pct", None)
                await query.edit_message_text("🔴 *Auto-Sniper OFF*\n\nNo automatic buys are running.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Dashboard", callback_data="refresh_dashboard")]]))
                return
            await query.edit_message_text("🎯 *Enable Auto-Sniper*\n\nChoose the percentage of spendable SOL for each new trade.\n\nProtected fee reserve + safety buffer are excluded.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("75%", callback_data="sniperpct:75"), InlineKeyboardButton("65%", callback_data="sniperpct:65")], [InlineKeyboardButton("25%", callback_data="sniperpct:25"), InlineKeyboardButton("10%", callback_data="sniperpct:10")], [InlineKeyboardButton("❌ Cancel", callback_data="snipercancel")]]))
            return
        if data.startswith("sniperpct:"):
            await query.answer()
            pct = float(data.split(":", 1)[1])
            balance = await bot_app.wallet.get_sol_balance() if bot_app.wallet.configured else 0
            reserve = bot_app.settings.auto_fee_reserve_sol + bot_app.settings.auto_safety_buffer_sol
            spendable = max(0, balance - reserve)
            estimated = min(spendable * pct / 100, bot_app.settings.max_buy_sol)
            await query.edit_message_text(f"🎯 *Confirm Auto-Sniper*\n\nAllocation: *{pct:g}%*\nWallet: `{balance:.6f} SOL`\nProtected: `{reserve:.6f} SOL`\nSpendable: `{spendable:.6f} SOL`\nEstimated trade: `{estimated:.9f} SOL`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm & Enable", callback_data=f"sniperconfirm:{pct:g}"), InlineKeyboardButton("⬅ Change", callback_data="toggle_sniper")], [InlineKeyboardButton("❌ Cancel", callback_data="snipercancel")]]))
            return
        if data.startswith("sniperconfirm:"):
            await query.answer()
            pct = float(data.split(":", 1)[1])
            if not 0 < pct <= 100:
                await query.edit_message_text("❌ Invalid allocation.")
                return
            await bot_app.store.set_setting("auto_sniper_allocation_pct", pct)
            await bot_app.store.set_setting("auto_sniper_enabled", True)
            await query.edit_message_text(f"🟢 *Auto-Sniper ON*\n\nAllocation: `{pct:g}%` of spendable SOL per trade.\nLive balance and protected reserve are checked before every buy.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔴 Turn Sniper OFF", callback_data="toggle_sniper")], [InlineKeyboardButton("🏠 Dashboard", callback_data="refresh_dashboard")]]))
            return
        if data == "snipercancel":
            await query.answer()
            await query.edit_message_text("Auto-Sniper setup cancelled. It remains OFF.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Dashboard", callback_data="refresh_dashboard")]]))
            return
        await original_callback_router(update, context)

    bot_app.callback_router = guarded_callback_router

    # NOTE: trading_bot.py's own legacy `tp_sl_daemon` (fixed TP / trailing-SL)
    # is intentionally NOT called here anymore. smart_sl.py's docstring states
    # it is the complete, deterministic exit policy (hard SL + break-even +
    # ratcheting profit lock), and its own tick() already reads/enforces
    # `pos.stop_loss_pct` and actively nulls `pos.take_profit_pct` /
    # `pos.trailing_sl_pct` on every position. Running both daemons back to
    # back on the same 20s interval meant they both evaluated the same
    # `stop_loss_pct` breach independently -- if smart_sl's own close attempt
    # failed (e.g. a bad quote), the legacy daemon would immediately retry an
    # independent, redundant close using its own history-less `_auto_close`
    # helper in the same tick. Only smart_sl's exit path writes to permanent
    # trade history, so keeping the legacy daemon in the loop also meant some
    # closes silently never got recorded there.

    async def combined_daemon(context):
        await smart_sl.tick(context)
        await sniper.tick(context)

    bot_app.tp_sl_daemon = combined_daemon
    dashboard.set_bot(bot_app)
    threading.Thread(target=run_dashboard, name="web-dashboard", daemon=True).start()
    log.info("Starting Telegram polling + smart SL + guarded auto-sniper")
    bot_app.main()
