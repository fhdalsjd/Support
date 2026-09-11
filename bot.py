"""
bot.py — Telegram handlers and inline-button UI.

Run with: python bot.py
"""
import re
import logging
import functools
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

from config import settings
from wallet import wallet
from trading import buy_token, sell_token, LAMPORTS_PER_SOL
from security import get_token_overview, get_rug_verdict
from state import store, Position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("membot")

MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")  # base58, Solana mint length range

# in-memory cache: last token shown per chat, so "Custom Amount" flow knows what to buy
_pending_ca: dict[int, str] = {}
_awaiting_custom_amount: dict[int, str] = {}


# ---------------------------------------------------------------------------
# ACCESS CONTROL — hard whitelist, silent drop for anyone else
# ---------------------------------------------------------------------------
def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in settings.admin_ids:
            log.warning("Rejected message from non-admin user_id=%s", user.id if user else None)
            return  # silent drop — no reply, no error, no trace back to the user
        return await handler(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
async def build_dashboard_text_and_kb():
    bal = await wallet.get_sol_balance()
    try:
        usd_price = await wallet.get_sol_usd_price()
    except Exception:
        usd_price = 0.0
    usd_val = bal * usd_price
    st = await store.get_settings()
    sniper_state = "🟢 ON" if st["auto_sniper_enabled"] else "🔴 OFF"

    text = (
        f"*Solana Trading Bot*\n\n"
        f"💰 Balance: `{bal:.4f} SOL` (${usd_val:,.2f})\n"
        f"🔑 Wallet: `{wallet.short_address()}`\n"
        f"🎯 Auto-Sniper: {sniper_state}\n"
        f"⚙️ Slippage: {st['slippage_bps']/100:.1f}%\n\n"
        f"Paste any Solana token mint address to trade it."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Open Positions", callback_data="positions")],
        [InlineKeyboardButton("⚙️ Wallet Settings", callback_data="settings"),
         InlineKeyboardButton(f"🎯 Sniper: {'ON' if st['auto_sniper_enabled'] else 'OFF'}", callback_data="toggle_sniper")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_dashboard")],
    ])
    return text, kb


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await build_dashboard_text_and_kb()
    await update.message.reply_markdown(text, reply_markup=kb)


async def refresh_dashboard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = await build_dashboard_text_and_kb()
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# CONTRACT ADDRESS INGESTION
# ---------------------------------------------------------------------------
@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Custom amount flow takes priority if we're waiting on one
    if chat_id in _awaiting_custom_amount:
        mint = _awaiting_custom_amount.pop(chat_id)
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("That's not a number. Buy cancelled.")
            return
        await do_buy(update, context, mint, amount)
        return

    match = MINT_RE.search(text)
    if not match:
        return  # not something we recognize — ignore silently
    mint = match.group(0)
    await show_token_overview(update, context, mint)


async def show_token_overview(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    msg = await update.message.reply_text("🔎 Looking up token...")
    overview = await get_token_overview(mint)
    if not overview.found:
        await msg.edit_text("Couldn't find market data for that mint (too new / no liquidity yet).")
        return
    rug = await get_rug_verdict(mint)

    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "UNKNOWN": "⚪"}[rug.risk_level]

    text = (
        f"*{overview.name}* (`${overview.symbol}`)\n"
        f"`{mint}`\n\n"
        f"💵 Price: ${overview.price_usd:.8f}\n"
        f"🏦 Market Cap: ${overview.market_cap:,.0f}\n"
        f"💧 Liquidity: ${overview.liquidity_usd:,.0f}\n"
        f"📈 5m: {overview.change_5m:+.1f}%   1h: {overview.change_1h:+.1f}%\n"
        f"🏛 DEX: {overview.dex}\n\n"
        f"{risk_emoji} *RugCheck: {rug.risk_level}*\n"
        + "\n".join(f"• {n}" for n in rug.notes)
    )

    _pending_ca[update.effective_chat.id] = mint
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0.1 SOL", callback_data=f"buy:{mint}:0.1"),
            InlineKeyboardButton("0.5 SOL", callback_data=f"buy:{mint}:0.5"),
            InlineKeyboardButton("1.0 SOL", callback_data=f"buy:{mint}:1.0"),
        ],
        [
            InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{mint}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ],
    ])
    await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# BUY FLOW
# ---------------------------------------------------------------------------
async def do_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str, sol_amount: float):
    chat_id = update.effective_chat.id
    notice = await context.bot.send_message(chat_id, f"⏳ Buying {sol_amount} SOL of `{mint[:6]}...`", parse_mode="Markdown")

    overview = await get_token_overview(mint)
    result = await buy_token(mint, sol_amount)

    if not result.success:
        await notice.edit_text(f"❌ Buy failed: {result.error}")
        return

    # record position (best-effort price bookkeeping — for precise entry price,
    # parse the swap's actual out_amount from the quote in trading.py and pass it through)
    st = await store.get_settings()
    pos = Position(
        mint=mint,
        symbol=overview.symbol,
        entry_price_usd=overview.price_usd,
        amount_tokens=await wallet.get_token_balance(mint),
        decimals=9,
        take_profit_pct=st["default_tp_pct"],
        stop_loss_pct=st["default_sl_pct"],
    )
    await store.upsert_position(pos)

    await notice.edit_text(
        f"✅ Bought `${overview.symbol}`\n"
        f"Tx: `{result.signature}`\n"
        f"https://solscan.io/tx/{result.signature}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------
async def show_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = await store.get_positions()
    query = update.callback_query

    if not positions:
        await query.edit_message_text("No open positions.", reply_markup=_back_kb())
        return

    for mint, pos in positions.items():
        overview = await get_token_overview(mint)
        cur_price = overview.price_usd or pos.entry_price_usd
        pnl_pct = ((cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100) if pos.entry_price_usd else 0
        value_usd = cur_price * pos.amount_tokens

        text = (
            f"*${pos.symbol}*\n"
            f"Entry: ${pos.entry_price_usd:.8f}  Now: ${cur_price:.8f}\n"
            f"PnL: {pnl_pct:+.1f}%\n"
            f"Value: ${value_usd:,.2f}\n"
            f"TP: +{pos.take_profit_pct}%  SL: -{pos.stop_loss_pct}%"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Sell 25%", callback_data=f"sell:{mint}:0.25"),
                InlineKeyboardButton("Sell 50%", callback_data=f"sell:{mint}:0.5"),
                InlineKeyboardButton("Sell 100%", callback_data=f"sell:{mint}:1.0"),
            ],
            [InlineKeyboardButton("📉 Set Trailing SL", callback_data=f"trailsl:{mint}")],
        ])
        await context.bot.send_message(query.message.chat_id, text, reply_markup=kb, parse_mode="Markdown")

    await query.edit_message_text("Positions listed below 👇", reply_markup=_back_kb())


async def do_sell(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str, fraction: float):
    chat_id = update.effective_chat.id
    notice = await context.bot.send_message(chat_id, f"⏳ Selling {int(fraction*100)}%...")

    positions = await store.get_positions()
    pos = positions.get(mint)
    if not pos:
        await notice.edit_text("No recorded position for that token.")
        return

    balance_raw = await wallet.get_token_balance(mint)
    if balance_raw <= 0:
        await store.remove_position(mint)
        await notice.edit_text("Wallet shows zero balance already — clearing position.")
        return

    amount_to_sell = balance_raw * fraction
    # NOTE: token_amount_raw expects raw (non-decimal-adjusted) units for
    # the swap API in production — convert using the token's actual decimals
    # (fetch via get_account_info / mint metadata) rather than assuming 9.
    raw_units = int(amount_to_sell * (10 ** pos.decimals))

    result = await sell_token(mint, raw_units, pos.decimals)
    if not result.success:
        await notice.edit_text(f"❌ Sell failed: {result.error}")
        return

    await store.reduce_position(mint, fraction)
    await notice.edit_text(
        f"✅ Sold {int(fraction*100)}% of ${pos.symbol}\n"
        f"Tx: `{result.signature}`",
        parse_mode="Markdown",
    )


def _back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await store.get_settings()
    text = (
        f"*Settings*\n\n"
        f"Slippage: {st['slippage_bps']/100:.1f}%\n"
        f"Priority fee: {st['priority_fee_microlamports']} microlamports\n"
        f"Default TP: +{st['default_tp_pct']}%\n"
        f"Default SL: -{st['default_sl_pct']}%"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("5%", callback_data="slip:500"),
         InlineKeyboardButton("15%", callback_data="slip:1500"),
         InlineKeyboardButton("25%", callback_data="slip:2500")],
        [InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")],
    ])
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# CALLBACK ROUTER
# ---------------------------------------------------------------------------
@admin_only
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "refresh_dashboard":
        await refresh_dashboard_cb(update, context)
    elif data == "positions":
        await show_positions(update, context)
    elif data == "settings":
        await show_settings(update, context)
    elif data == "toggle_sniper":
        st = await store.get_settings()
        await store.set_setting("auto_sniper_enabled", not st["auto_sniper_enabled"])
        await refresh_dashboard_cb(update, context)
    elif data == "cancel":
        await query.edit_message_text("Cancelled.")
    elif data.startswith("buy:"):
        _, mint, amount = data.split(":")
        await do_buy(update, context, mint, float(amount))
    elif data.startswith("custom:"):
        _, mint = data.split(":")
        _awaiting_custom_amount[query.message.chat_id] = mint
        await query.edit_message_text(f"Enter the SOL amount to buy for `{mint[:6]}...`", parse_mode="Markdown")
    elif data.startswith("sell:"):
        _, mint, fraction = data.split(":")
        await do_sell(update, context, mint, float(fraction))
    elif data.startswith("slip:"):
        _, bps = data.split(":")
        await store.set_setting("slippage_bps", int(bps))
        await show_settings(update, context)
    elif data.startswith("trailsl:"):
        _, mint = data.split(":")
        await query.edit_message_text(
            f"Trailing SL setup for `{mint[:6]}...` — reply with a percent (e.g. `15` for 15% trail).",
            parse_mode="Markdown",
        )
        # In a full build, capture the next text message into a dedicated
        # "awaiting trailing sl" dict the same way _awaiting_custom_amount works.


# ---------------------------------------------------------------------------
# TP / SL / TRAILING-SL BACKGROUND DAEMON
# ---------------------------------------------------------------------------
async def tp_sl_daemon(context: ContextTypes.DEFAULT_TYPE):
    positions = await store.get_positions()
    if not positions:
        return

    admin_id = next(iter(settings.admin_ids))  # notify the primary admin

    for mint, pos in positions.items():
        overview = await get_token_overview(mint)
        if not overview.found or overview.price_usd <= 0:
            continue
        cur_price = overview.price_usd
        pnl_pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100

        # trailing stop-loss bookkeeping
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


async def _auto_close(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mint: str, pos: Position, reason: str):
    balance_raw = await wallet.get_token_balance(mint)
    if balance_raw <= 0:
        await store.remove_position(mint)
        return
    raw_units = int(balance_raw * (10 ** pos.decimals))
    result = await sell_token(mint, raw_units, pos.decimals)
    if result.success:
        await store.remove_position(mint)
        await context.bot.send_message(
            admin_id,
            f"🤖 Auto-closed *${pos.symbol}* — {reason}\nTx: `{result.signature}`",
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(admin_id, f"⚠️ Auto-close FAILED for ${pos.symbol}: {result.error}")


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
def main():
    settings.validate()

    app = Application.builder().token(settings.telegram_token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # TP/SL/trailing-SL checker — every 20s. Tighten/loosen based on your RPC's rate limits.
    app.job_queue.run_repeating(tp_sl_daemon, interval=20, first=10)

    log.info("Bot starting — whitelisted admins: %s", settings.admin_ids)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
