"""Compatibility patch for the Telegram token-view flow.

Keeps contract-address discovery independent from wallet connection. Viewing a
mint is read-only; wallet connection is required only when a buy/sell action is
actually executed.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def install(bot_app):
    async def render_token_message(message, context, mint: str):
        try:
            overview, rug, error = await bot_app._token_view_data(mint)
            if error:
                await message.edit_text(error, reply_markup=bot_app._back_kb())
                return

            risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "UNKNOWN": "⚪"}.get(rug.risk_level, "⚪")
            positions = await bot_app.store.get_positions()
            pos = positions.get(mint)
            live_line = ""
            if pos and pos.entry_price_usd:
                pnl = (overview.price_usd - pos.entry_price_usd) / pos.entry_price_usd * 100
                value = overview.price_usd * pos.amount_tokens
                live_line = f"\n📈 *Live PnL:* {pnl:+.2f}%\n💼 Position value: ${value:,.2f}\n"

            wallet_line = "🟢 Wallet ready for trading" if bot_app.wallet.configured else "🔌 Connect wallet to buy this token"
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
                + f"\n{wallet_line}"
            )

            bot_app._pending_ca[message.chat_id] = mint
            buttons = [
                [InlineKeyboardButton("0.01 SOL", callback_data=f"buy:{mint}:0.01"), InlineKeyboardButton("0.05 SOL", callback_data=f"buy:{mint}:0.05"), InlineKeyboardButton("0.1 SOL", callback_data=f"buy:{mint}:0.1")],
                [InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{mint}"), InlineKeyboardButton("🔄 Refresh Token", callback_data=f"tokenrefresh:{mint}")],
                [InlineKeyboardButton("📊 Live PnL", callback_data="positions"), InlineKeyboardButton("❌ Close", callback_data="cancel")],
            ]
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        except Exception as exc:
            bot_app.log.exception("Token view failed: %s", type(exc).__name__)
            await message.edit_text("⚠️ Token live view failed. Press Refresh Token to retry.", reply_markup=bot_app._back_kb())

    async def show_token_overview(update, context, mint: str):
        msg = await update.message.reply_text("🔎 Loading live token market...")
        await render_token_message(msg, context, mint)

    async def refresh_token_cb(update, context, mint: str):
        await render_token_message(update.callback_query.message, context, mint)

    bot_app._render_token_message = render_token_message
    bot_app.show_token_overview = show_token_overview
    bot_app.refresh_token_cb = refresh_token_cb
