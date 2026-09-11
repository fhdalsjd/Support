"""Telegram trading UI with live token views, PnL refresh, and safe wallet flows."""
import re
import logging
import functools
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from config import settings
from wallet import wallet
from trading import buy_token, sell_token
from security import get_token_overview, get_rug_verdict
from state import store, Position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("membot")
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_pending_ca: dict[int, str] = {}
_awaiting_custom_amount: dict[int, str] = {}
_awaiting_wallet_mnemonic: set[int] = set()
_live_position_messages: dict[int, int] = {}


def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in settings.admin_ids:
            log.warning("Rejected message from non-admin user_id=%s", user.id if user else None)
            return
        return await handler(update, context)
    return wrapper


@admin_only
async def connect_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _awaiting_wallet_mnemonic.add(update.effective_chat.id)
    await update.message.reply_text(
        "🔐 Wallet connection\n\nSend your Solana recovery phrase in your next message. "
        "I will validate it, derive the wallet address, check the wallet on-chain, and delete the phrase message when possible.\n\n"
        "⚠️ The phrase is kept in memory only and is never written to bot state or logs. Use a dedicated trading wallet.\n\n"
        "Send the phrase now, or /cancel_wallet to stop."
    )


@admin_only
async def cancel_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _awaiting_wallet_mnemonic.discard(update.effective_chat.id)
    await update.message.reply_text("Wallet connection cancelled.")


async def _delete_secret_message(message):
    try:
        await message.delete()
        return True
    except Exception as exc:
        log.warning("Could not delete wallet credential message: %s", type(exc).__name__)
        return False


@admin_only
async def handle_wallet_mnemonic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _awaiting_wallet_mnemonic.discard(chat_id)
    phrase = update.message.text.strip()
    deleted = await _delete_secret_message(update.message)
    try:
        address = wallet.connect_mnemonic(phrase)
        balance = await wallet.get_sol_balance()
    except Exception as exc:
        log.warning("Wallet connection rejected: %s", type(exc).__name__)
        await context.bot.send_message(chat_id, "❌ Wallet verification failed. The recovery phrase is invalid or unsupported. Nothing was connected. Please use /connect_wallet to try again.")
        return
    delete_note = "🗑 Recovery phrase message deleted." if deleted else "⚠️ Telegram did not allow deletion; delete that message manually now."
    await context.bot.send_message(
        chat_id,
        "✅ *Wallet verified and connected*\n\n"
        f"Address: `{address}`\nBalance: `{balance:.6f} SOL`\n\n{delete_note}\n"
        "The recovery phrase is not saved to bot state or logs. It remains only in this running process.",
        parse_mode="Markdown",
    )


@admin_only
async def disconnect_wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet.disconnect()
    await update.message.reply_text("🔌 Wallet disconnected from this bot process. The recovery phrase was not persisted.")


async def build_dashboard_text_and_kb():
    st = await store.get_settings()
    sniper_state = "🟢 ON" if st["auto_sniper_enabled"] else "🔴 OFF"
    if wallet.configured:
        try:
            bal = await wallet.get_sol_balance()
            usd_price = await wallet.get_sol_usd_price()
            wallet_line = f"💰 Balance: `{bal:.6f} SOL` (${bal * usd_price:,.2f})\n🔑 Wallet: `{wallet.short_address()}`"
        except Exception:
            wallet_line = f"🔑 Wallet: `{wallet.short_address()}` (balance unavailable)"
    else:
        wallet_line = "🔌 Wallet: `Not connected`"
    text = (
        "*Solana Trading Bot • Pro Dashboard*\n\n"
        f"{wallet_line}\n"
        f"🎯 Auto-Sniper: {sniper_state}\n"
        f"⚙️ Slippage: {st['slippage_bps']/100:.1f}%\n"
        f"📊 Open positions: {len(await store.get_positions())}\n\n"
        "Paste any Solana token mint address to open its live market view."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live PnL / Positions", callback_data="positions")],
        [InlineKeyboardButton("🔐 Connect Wallet", callback_data="connect_wallet"), InlineKeyboardButton("🔌 Disconnect", callback_data="disconnect_wallet")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"), InlineKeyboardButton(f"🎯 Sniper: {'ON' if st['auto_sniper_enabled'] else 'OFF'}", callback_data="toggle_sniper")],
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="refresh_dashboard")],
    ])
    return text, kb


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await build_dashboard_text_and_kb()
    await update.message.reply_markdown(text, reply_markup=kb)


async def refresh_dashboard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await build_dashboard_text_and_kb()
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def _token_view_data(mint: str):
    overview = await get_token_overview(mint)
    if not overview.found:
        return None, None, "❌ No market data found for this mint yet. It may be too new or have no usable liquidity."
    rug = await get_rug_verdict(mint)
    return overview, rug, None


async def _render_token_message(message, context: ContextTypes.DEFAULT_TYPE, mint: str):
    if not wallet.configured:
        await message.edit_text("🔌 No wallet connected. Use /connect_wallet before trading.")
        return
    try:
        overview, rug, error = await _token_view_data(mint)
        if error:
            await message.edit_text(error, reply_markup=_back_kb())
            return
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "UNKNOWN": "⚪"}[rug.risk_level]
        positions = await store.get_positions()
        pos = positions.get(mint)
        live_line = ""
        if pos and pos.entry_price_usd:
            pnl = (overview.price_usd - pos.entry_price_usd) / pos.entry_price_usd * 100
            value = overview.price_usd * pos.amount_tokens
            live_line = f"\n📈 *Live PnL:* {pnl:+.2f}%\n💼 Position value: ${value:,.2f}\n"
        text = (
            f"*{overview.name}* (`${overview.symbol}`)\n`{mint}`\n\n"
            f"💵 Price: `${overview.price_usd:.8f}`\n"
            f"🏦 Market Cap: `${overview.market_cap:,.0f}`\n"
            f"💧 Liquidity: `${overview.liquidity_usd:,.0f}`\n"
            f"📈 Momentum: 5m `{overview.change_5m:+.1f}%` • 1h `{overview.change_1h:+.1f}%`\n"
            f"🏛 DEX: `{overview.dex}`\n"
            f"{risk_emoji} *RugCheck:* `{rug.risk_level}`\n"
            + ("\n".join(f"• {n}" for n in rug.notes[:5]) if rug.notes else "• No additional notes")
            + live_line
        )
        _pending_ca[message.chat_id] = mint
        buttons = [
            [InlineKeyboardButton("0.01 SOL", callback_data=f"buy:{mint}:0.01"), InlineKeyboardButton("0.05 SOL", callback_data=f"buy:{mint}:0.05"), InlineKeyboardButton("0.1 SOL", callback_data=f"buy:{mint}:0.1")],
            [InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{mint}"), InlineKeyboardButton("🔄 Refresh Token", callback_data=f"tokenrefresh:{mint}")],
            [InlineKeyboardButton("📊 Live PnL", callback_data="positions"), InlineKeyboardButton("❌ Close", callback_data="cancel")],
        ]
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Token overview failed: %s", type(exc).__name__)
        await message.edit_text("⚠️ Token live view failed. Press Refresh Token to retry.", reply_markup=_back_kb())


@admin_only
async def show_token_overview(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    if not wallet.configured:
        await update.message.reply_text("🔌 No wallet connected. Use /connect_wallet before trading.")
        return
    msg = await update.message.reply_text("🔎 Loading live token market...")
    await _render_token_message(msg, context, mint)


async def refresh_token_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    await _render_token_message(update.callback_query.message, context, mint)


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in _awaiting_wallet_mnemonic:
        await handle_wallet_mnemonic(update, context)
        return
    text = update.message.text.strip()
    if chat_id in _awaiting_custom_amount:
        mint = _awaiting_custom_amount.pop(chat_id)
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("That's not a valid positive SOL amount. Buy cancelled.")
            return
        await do_buy(update, context, mint, amount)
        return
    match = MINT_RE.search(text)
    if match:
        await show_token_overview(update, context, match.group(0))


async def do_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str, sol_amount: float):
    chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
    if not wallet.configured:
        await context.bot.send_message(chat_id, "🔌 No wallet connected. Use /connect_wallet first.")
        return
    if sol_amount > settings.max_buy_sol:
        await context.bot.send_message(chat_id, f"❌ Maximum buy is {settings.max_buy_sol} SOL.")
        return
    notice = await context.bot.send_message(chat_id, f"⏳ Preflighting buy of `{sol_amount:.9f} SOL`...", parse_mode="Markdown")
    try:
        overview = await get_token_overview(mint)
        result = await buy_token(mint, sol_amount)
        if not result.success:
            await notice.edit_text(result.error or "❌ Buy failed")
            return
        st = await store.get_settings()
        token_balance = await wallet.get_token_balance(mint)
        pos = Position(mint=mint, symbol=overview.symbol, entry_price_usd=overview.price_usd, amount_tokens=token_balance, decimals=9, take_profit_pct=st["default_tp_pct"], stop_loss_pct=st["default_sl_pct"])
        await store.upsert_position(pos)
        await notice.edit_text(
            f"✅ *Bought ${overview.symbol}*\nAmount: `{sol_amount:.9f} SOL`\nTx: `{result.signature}`\n\n📊 Live PnL is now available from the Live PnL button.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.exception("Buy failed: %s", type(exc).__name__)
        await notice.edit_text("❌ Buy failed due to a temporary error. No wallet credential was exposed.")


async def _positions_text_and_kb():
    if not wallet.configured:
        return "🔌 No wallet connected. Use /connect_wallet first.", _back_kb()
    positions = await store.get_positions()
    if not positions:
        return "📊 *Live PnL*\n\nNo open positions.", InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="positions")], [InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])
    chunks = ["📊 *LIVE PnL • positions*", ""]
    rows = []
    for mint, pos in positions.items():
        try:
            overview = await get_token_overview(mint)
            cur = overview.price_usd or pos.entry_price_usd
            pnl = ((cur - pos.entry_price_usd) / pos.entry_price_usd * 100) if pos.entry_price_usd else 0
            value = cur * pos.amount_tokens
            smart = "—" if pos.smart_stop_profit_pct is None else ("BE" if pos.smart_stop_profit_pct == 0 else f"+{pos.smart_stop_profit_pct:.1f}%")
            chunks.append(
                f"*${pos.symbol}*\nEntry `${pos.entry_price_usd:.8f}` → Now `${cur:.8f}`\n"
                f"PnL *{pnl:+.2f}%* • Value `${value:,.2f}`\n"
                f"TP `+{pos.take_profit_pct}%` • Hard SL `-{pos.stop_loss_pct}%` • Smart SL `{smart}`\n"
                f"Mint `{mint[:8]}…{mint[-6:]}`"
            )
            rows.append([InlineKeyboardButton("Sell 25%", callback_data=f"sell:{mint}:0.25"), InlineKeyboardButton("Sell 50%", callback_data=f"sell:{mint}:0.5"), InlineKeyboardButton("Sell 100%", callback_data=f"sell:{mint}:1.0")])
        except Exception as exc:
            log.warning("Position display failed: %s", type(exc).__name__)
            chunks.append(f"*${pos.symbol}*\n⚠️ Price temporarily unavailable")
    rows.append([InlineKeyboardButton("🔄 Refresh Live PnL", callback_data="positions")])
    rows.append([InlineKeyboardButton("🏠 Dashboard", callback_data="refresh_dashboard")])
    return "\n\n".join(chunks), InlineKeyboardMarkup(rows)


async def show_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await _positions_text_and_kb()
    query = update.callback_query
    _live_position_messages[query.message.chat_id] = query.message.message_id
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def live_pnl_daemon(context: ContextTypes.DEFAULT_TYPE):
    if not _live_position_messages or not wallet.configured:
        return
    text, kb = await _positions_text_and_kb()
    for chat_id, message_id in list(_live_position_messages.items()):
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            pass


async def do_sell(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str, fraction: float):
    chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
    if not wallet.configured:
        await context.bot.send_message(chat_id, "🔌 No wallet connected. Use /connect_wallet first.")
        return
    notice = await context.bot.send_message(chat_id, f"⏳ Selling {int(fraction*100)}%...")
    try:
        positions = await store.get_positions()
        pos = positions.get(mint)
        if not pos:
            await notice.edit_text("No recorded position for that token.")
            return
        balance = await wallet.get_token_balance(mint)
        if balance <= 0:
            await store.remove_position(mint)
            await notice.edit_text("Wallet shows zero token balance already — clearing position.")
            return
        raw_units = int(balance * fraction * (10 ** pos.decimals))
        result = await sell_token(mint, raw_units, pos.decimals)
        if not result.success:
            await notice.edit_text(result.error or "❌ Sell failed")
            return
        await store.reduce_position(mint, fraction)
        await notice.edit_text(f"✅ Sold {int(fraction*100)}% of ${pos.symbol}\nTx: `{result.signature}`", parse_mode="Markdown")
    except Exception as exc:
        log.exception("Sell failed: %s", type(exc).__name__)
        await notice.edit_text("❌ Sell failed due to a temporary error.")


def _back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await store.get_settings()
    text = f"*Settings*\n\nSlippage: {st['slippage_bps']/100:.1f}%\nPriority fee: {st['priority_fee_microlamports']} microlamports\nDefault TP: +{st['default_tp_pct']}%\nDefault SL: -{st['default_sl_pct']}%"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("5%", callback_data="slip:500"), InlineKeyboardButton("15%", callback_data="slip:1500"), InlineKeyboardButton("25%", callback_data="slip:2500")], [InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


@admin_only
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    if data == "refresh_dashboard":
        await refresh_dashboard_cb(update, context)
    elif data == "connect_wallet":
        _awaiting_wallet_mnemonic.add(query.message.chat_id)
        await query.edit_message_text("🔐 Send your recovery phrase in your next message. It will be validated and deleted when possible. Use /cancel_wallet to cancel.")
    elif data == "disconnect_wallet":
        wallet.disconnect()
        await query.edit_message_text("🔌 Wallet disconnected.", reply_markup=_back_kb())
    elif data == "positions":
        await show_positions(update, context)
    elif data == "settings":
        await show_settings(update, context)
    elif data == "toggle_sniper":
        st = await store.get_settings()
        await store.set_setting("auto_sniper_enabled", not st["auto_sniper_enabled"])
        await refresh_dashboard_cb(update, context)
    elif data == "cancel":
        await query.edit_message_text("Cancelled.", reply_markup=_back_kb())
    elif data.startswith("tokenrefresh:"):
        _, mint = data.split(":", 1)
        await refresh_token_cb(update, context, mint)
    elif data.startswith("buy:"):
        _, mint, amount = data.split(":")
        await do_buy(update, context, mint, float(amount))
    elif data.startswith("custom:"):
        _, mint = data.split(":", 1)
        _awaiting_custom_amount[query.message.chat_id] = mint
        await query.edit_message_text(f"Enter the SOL amount to buy for `{mint[:8]}...`\n\nThe bot will preflight the transaction and show the exact SOL balance/top-up required if funds are insufficient.", parse_mode="Markdown")
    elif data.startswith("sell:"):
        _, mint, fraction = data.split(":")
        await do_sell(update, context, mint, float(fraction))
    elif data.startswith("slip:"):
        _, bps = data.split(":")
        await store.set_setting("slippage_bps", int(bps))
        await show_settings(update, context)
    elif data.startswith("trailsl:"):
        _, mint = data.split(":", 1)
        await query.edit_message_text(f"Trailing SL setup for `{mint[:8]}...` — reply with a percent (e.g. `15` for 15% trail).", parse_mode="Markdown")


async def tp_sl_daemon(context: ContextTypes.DEFAULT_TYPE):
    if not wallet.configured:
        return
    positions = await store.get_positions()
    if not positions or not settings.admin_ids:
        return
    admin_id = next(iter(settings.admin_ids))
    for mint, pos in positions.items():
        try:
            overview = await get_token_overview(mint)
            if not overview.found or overview.price_usd <= 0:
                continue
            cur_price = overview.price_usd
            pnl_pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100
            if pos.trailing_sl_pct:
                if not pos.trailing_high_price or cur_price > pos.trailing_high_price:
                    pos.trailing_high_price = cur_price
                    await store.upsert_position(pos)
                drawdown = (pos.trailing_high_price - cur_price) / pos.trailing_high_price * 100
                if drawdown >= pos.trailing_sl_pct:
                    await _auto_close(context, admin_id, mint, pos, f"Trailing SL hit (-{drawdown:.1f}% from high)")
                    continue
            if pos.take_profit_pct and pnl_pct >= pos.take_profit_pct:
                await _auto_close(context, admin_id, mint, pos, f"Take-Profit hit (+{pnl_pct:.1f}%)")
            elif pos.stop_loss_pct and pnl_pct <= -pos.stop_loss_pct:
                await _auto_close(context, admin_id, mint, pos, f"Stop-Loss hit ({pnl_pct:.1f}%)")
        except Exception as exc:
            log.warning("TP/SL check failed: %s", type(exc).__name__)


async def _auto_close(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mint: str, pos: Position, reason: str):
    try:
        balance = await wallet.get_token_balance(mint)
        if balance <= 0:
            await store.remove_position(mint)
            return
        raw_units = int(balance * (10 ** pos.decimals))
        result = await sell_token(mint, raw_units, pos.decimals)
        if result.success:
            await store.remove_position(mint)
            await context.bot.send_message(admin_id, f"🤖 Auto-closed *${pos.symbol}* — {reason}\nTx: `{result.signature}`", parse_mode="Markdown")
        else:
            await context.bot.send_message(admin_id, f"⚠️ Auto-close FAILED for ${pos.symbol}: {result.error}")
    except Exception as exc:
        log.warning("Auto-close failed: %s", type(exc).__name__)


def main():
    settings.validate()
    app = Application.builder().token(settings.telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("connect_wallet", connect_wallet_cmd))
    app.add_handler(CommandHandler("cancel_wallet", cancel_wallet_cmd))
    app.add_handler(CommandHandler("disconnect_wallet", disconnect_wallet_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.job_queue.run_repeating(tp_sl_daemon, interval=20, first=10)
    app.job_queue.run_repeating(live_pnl_daemon, interval=10, first=15)
    log.info("Bot starting — whitelisted admins: %s", settings.admin_ids)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
