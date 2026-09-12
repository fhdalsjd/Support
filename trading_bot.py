"""Telegram trading UI with live token views, PnL refresh, and safe wallet flows."""
from __future__ import annotations

import functools
import logging
import re
import time
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from solders.pubkey import Pubkey

from config import settings
from state import Position, store
import security
from trading import buy_token, sell_token, to_raw_units
from wallet import wallet
import sniper
import smart_sl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("membot")

MINT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
_pending_ca: dict[int, str] = {}
_awaiting_custom_amount: dict[int, str] = {}
_awaiting_custom_sell_pct: dict[int, str] = {}
_awaiting_wallet_mnemonic: set[int] = set()
_live_position_messages: dict[int, int] = {}


def _extract_mint(text: str) -> str | None:
    """Extract a Solana mint from arbitrary pasted Telegram text."""
    cleaned = ZERO_WIDTH_RE.sub("", text or "").strip()
    match = MINT_RE.search(cleaned)
    return match.group(0) if match else None


def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id not in settings.admin_ids:
            log.warning("Rejected message from non-admin user_id=%s", user.id if user else None)
            return
        return await handler(update, context, *args, **kwargs)
    return wrapper


def _snapshot(overview, source="manual", allocation_pct=None, trade_amount=None, price_impact=None, risk=None, rug=None):
    return {
        "source": source,
        "allocation_pct": allocation_pct,
        "trade_amount_sol": trade_amount,
        "price_impact_pct": price_impact,
        "risk_score": getattr(risk, "risk_score", None),
        "risk_level": getattr(rug, "risk_level", None),
        "rug_notes": list(getattr(rug, "notes", []) or []),
        "price_usd": overview.price_usd,
        "market_cap_usd": overview.market_cap,
        "fdv_usd": overview.fdv,
        "liquidity_usd": overview.liquidity_usd,
        "volume_5m_usd": overview.volume_5m,
        "volume_24h_usd": overview.volume_24h,
        "trades_5m": overview.total_trades_5m,
        "trades_24h": overview.total_trades_24h,
        "buy_sell_ratio_5m": overview.buy_sell_ratio_5m,
        "change_5m_pct": overview.change_5m,
        "change_1h_pct": overview.change_1h,
        "pool_age_minutes": overview.age_minutes,
        "primary_pool_age_minutes": getattr(overview, "primary_pool_age_minutes", None),
        "oldest_pool_age_minutes": getattr(overview, "oldest_pool_age_minutes", None),
        "pool_count": overview.pool_count,
        "dex": overview.dex,
        "decimals": overview.decimals,
        "total_supply": overview.total_supply,
        "mint_authority": getattr(overview, "mint_authority", None),
        "freeze_authority": getattr(overview, "freeze_authority", None),
        "top_holder_pct": getattr(overview, "top_holder_pct", None),
        "data_quality": overview.data_quality,
        "data_warnings": list(getattr(overview, "data_warnings", []) or []),
        "market_cap_source": getattr(overview, "market_cap_source", None),
        "data_source": getattr(overview, "data_source", None),
        "fetched_at": getattr(overview, "fetched_at", None),
    }


async def _save_closed_trade(pos: Position, mint: str, signature: str | None, reason: str, exit_price: float | None = None, exit_tokens: float | None = None):
    if exit_price is None:
        try:
            ov = await security.get_token_overview(mint)
            exit_price = ov.price_usd if ov.found else None
        except Exception:
            pass
    entry = float(pos.entry_price_usd or 0)
    pnl_pct = ((exit_price - entry) / entry * 100) if exit_price and entry else None
    entry_sol = float(pos.entry_sol or 0)
    record = {
        "closed_at": time.time(),
        "opened_at": pos.opened_at or None,
        "mint": mint,
        "symbol": pos.symbol,
        "decimals": pos.decimals,
        "entry_price_usd": entry,
        "exit_price_usd": exit_price,
        "entry_sol": entry_sol,
        "pnl_pct": pnl_pct,
        "pnl_sol_estimate": (entry_sol * pnl_pct / 100) if pnl_pct is not None else None,
        "amount_tokens": pos.amount_tokens,
        "exit_tokens": exit_tokens if exit_tokens is not None else pos.amount_tokens,
        "take_profit_pct": pos.take_profit_pct,
        "stop_loss_pct": pos.stop_loss_pct,
        "peak_profit_pct": pos.peak_profit_pct,
        "smart_stop_profit_pct": pos.smart_stop_profit_pct,
        "close_reason": reason,
        "buy_signature": pos.buy_signature,
        "sell_signature": signature,
        "entry_snapshot": dict(pos.entry_snapshot or {}),
        "close_events": list(pos.close_events or []),
    }
    await store.append_history(record)
    return pnl_pct


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
    sniper_state = "🟢 ON" if st.get("auto_sniper_enabled") else "🔴 OFF"
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
        f"⚙️ Slippage: {st.get('slippage_bps', settings.default_slippage_bps)/100:.1f}%\n"
        f"📊 Open positions: {len(await store.get_positions())}\n\n"
        "Paste any Solana token mint address to open its live market view."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Live PnL / Positions", callback_data="positions")],
        [InlineKeyboardButton("🔐 Connect Wallet", callback_data="connect_wallet"), InlineKeyboardButton("🔌 Disconnect", callback_data="disconnect_wallet")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"), InlineKeyboardButton(f"🎯 Sniper: {'ON' if st.get('auto_sniper_enabled') else 'OFF'}", callback_data="toggle_sniper")],
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
    overview = await security.get_token_overview(mint)
    if not overview.found:
        return None, None, "❌ No market data found for this mint yet. It may be too new or have no usable liquidity."
    rug = await security.get_rug_verdict(mint)
    return overview, rug, None


async def _render_token_message(message, context: ContextTypes.DEFAULT_TYPE, mint: str):
    try:
        overview, rug, error = await _token_view_data(mint)
        if error:
            await message.edit_text(error, reply_markup=_back_kb())
            return
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "UNKNOWN": "⚪"}.get(rug.risk_level, "⚪")
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
            + ("\n🟢 Wallet ready for trading" if wallet.configured else "\n🔌 Connect wallet to buy/sell")
        )
        _pending_ca[message.chat_id] = mint
        buttons = [
            [InlineKeyboardButton("0.01 SOL", callback_data=f"buy:{mint}:0.01"), InlineKeyboardButton("0.05 SOL", callback_data=f"buy:{mint}:0.05"), InlineKeyboardButton("0.1 SOL", callback_data=f"buy:{mint}:0.1")],
            [InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom:{mint}"), InlineKeyboardButton("🔄 Refresh Token", callback_data=f"tokenrefresh:{mint}")],
        ]
        if pos:
            buttons.append([InlineKeyboardButton("💼 Close Position", callback_data=f"closemenu:{mint}")])
        buttons.append([InlineKeyboardButton("📊 Live PnL", callback_data="positions"), InlineKeyboardButton("❌ Close", callback_data="cancel")])
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Token overview failed: %s", type(exc).__name__)
        await message.edit_text("⚠️ Token live view failed. Press Refresh Token to retry.", reply_markup=_back_kb())


@admin_only
async def show_token_overview(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    msg = await update.message.reply_text("🔎 Loading live token market...")
    await _render_token_message(msg, context, mint)


async def refresh_token_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    await _render_token_message(update.callback_query.message, context, mint)


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = ZERO_WIDTH_RE.sub("", (update.message.text or "")).strip()
    if chat_id in _awaiting_wallet_mnemonic:
        await handle_wallet_mnemonic(update, context)
        return
    if chat_id in _awaiting_custom_sell_pct:
        mint = _awaiting_custom_sell_pct.pop(chat_id)
        try:
            pct = float(text.replace("%", "").strip())
            if not 0 < pct <= 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a close percentage from 0.01% to 100%. Example: 37.5")
            return
        await do_sell(update, context, mint, pct / 100.0)
        return
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

    mint = _extract_mint(text)
    if mint:
        log.info("Detected Solana mint in Telegram message: %s...%s", mint[:6], mint[-4:])
        try:
            await show_token_overview(update, context, mint)
        except Exception as exc:
            log.exception("Token message handler failed: %s", type(exc).__name__)
            await update.message.reply_text("⚠️ I received the token address, but the live token lookup failed. Please try again in a few seconds.")
        return

    await update.message.reply_text(
        "⚠️ I couldn't detect a Solana token address in that message.\n\n"
        "Paste the full mint address (usually 43–44 base58 characters)."
    )


async def do_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str, sol_amount: float):
    chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
    if not wallet.configured:
        await context.bot.send_message(chat_id, "🔌 No wallet connected. Use /connect_wallet first.")
        return
    if sol_amount <= 0 or sol_amount > settings.max_buy_sol:
        await context.bot.send_message(chat_id, f"❌ Buy must be greater than 0 and no more than {settings.max_buy_sol} SOL.")
        return
    notice = await context.bot.send_message(chat_id, f"⏳ Preflighting buy of `{sol_amount:.9f} SOL`...", parse_mode="Markdown")
    try:
        overview = await security.get_token_overview(mint)
        if not overview.found or overview.price_usd <= 0:
            await notice.edit_text("❌ No reliable live market price is available for this token. Buy cancelled.")
            return

        before = await wallet.get_token_balance(mint)
        result = await buy_token(mint, sol_amount)
        if not result.success:
            await notice.edit_text(result.error or "❌ Buy failed")
            return

        after = await wallet.get_token_balance(mint)
        token_delta = max(0.0, after - before)
        if token_delta <= 0:
            await notice.edit_text("⚠️ Buy confirmed but token balance did not increase; position was not recorded.")
            return

        try:
            decimals = (await wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
        except Exception:
            decimals = overview.decimals or 9

        st = await store.get_settings()
        old = (await store.get_positions()).get(mint)
        snap = _snapshot(overview, trade_amount=sol_amount)
        if old:
            total = old.amount_tokens + token_delta
            old.entry_price_usd = ((old.entry_price_usd * old.amount_tokens) + (overview.price_usd * token_delta)) / total
            old.amount_tokens = total
            old.entry_sol = (old.entry_sol or 0) + sol_amount
            old.decimals = decimals
            old.entry_snapshot = {**(old.entry_snapshot or {}), "last_add": snap}
            await store.upsert_position(old)
        else:
            pos = Position(
                mint=mint,
                symbol=overview.symbol,
                entry_price_usd=overview.price_usd,
                amount_tokens=token_delta,
                decimals=decimals,
                take_profit_pct=st.get("default_tp_pct"),
                stop_loss_pct=st.get("default_sl_pct", 30.0),
                entry_sol=sol_amount,
                opened_at=time.time(),
                buy_signature=result.signature,
                entry_snapshot=snap,
            )
            await store.upsert_position(pos)

        await notice.edit_text(
            f"✅ *Position OPEN — ${overview.symbol}*\n\n"
            f"CA / Mint: `{mint}`\nEntry: `${overview.price_usd:.10f}`\nTokens: `{token_delta:.8g}`\nSpend: `{sol_amount:.9f} SOL`\n"
            f"Liquidity: `${overview.liquidity_usd:,.0f}` • MC: `${overview.market_cap:,.0f}` • FDV: `${overview.fdv:,.0f}`\n"
            f"5m volume: `${overview.volume_5m:,.0f}` • 5m trades: `{overview.total_trades_5m}`\nRisk data: `{overview.data_quality}` • Pools: `{overview.pool_count}`\n"
            f"TP: `Managed by Smart-SL` • Hard SL: `-{st.get('default_sl_pct', 30.0)}%`\nBuy Tx: `{result.signature}`\n\n📚 Full entry snapshot saved.",
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
            overview = await security.get_token_overview(mint)
            cur = overview.price_usd or pos.entry_price_usd
            pnl = ((cur - pos.entry_price_usd) / pos.entry_price_usd * 100) if pos.entry_price_usd else 0
            value = cur * pos.amount_tokens
            smart = "—" if pos.smart_stop_profit_pct is None else ("BE" if pos.smart_stop_profit_pct == 0 else f"+{pos.smart_stop_profit_pct:.1f}%")
            chunks.append(
                f"*${pos.symbol}*\nEntry `${pos.entry_price_usd:.8f}` → Now `${cur:.8f}`\n"
                f"PnL *{pnl:+.2f}%* • Value `${value:,.2f}`\n"
                f"Hard SL `-{pos.stop_loss_pct}%` • Smart SL `{smart}`\n"
                f"Mint `{mint[:8]}…{mint[-6:]}`"
            )
            rows.append([
                InlineKeyboardButton("Sell 25%", callback_data=f"sell:{mint}:0.25"),
                InlineKeyboardButton("Sell 50%", callback_data=f"sell:{mint}:0.50"),
                InlineKeyboardButton("Sell 75%", callback_data=f"sell:{mint}:0.75"),
            ])
            rows.append([
                InlineKeyboardButton("Sell 100%", callback_data=f"sell:{mint}:1.00"),
                InlineKeyboardButton("✏️ Close %", callback_data=f"sellcustom:{mint}"),
                InlineKeyboardButton("🔄 Refresh", callback_data="positions"),
            ])
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
    if not 0 < fraction <= 1:
        await context.bot.send_message(chat_id, "❌ Close percentage must be between 0.01% and 100%.")
        return
    pct = fraction * 100.0
    notice = await context.bot.send_message(chat_id, f"⏳ Selling {pct:g}% of the recorded position...")
    try:
        positions = await store.get_positions()
        pos = positions.get(mint)
        if not pos:
            await notice.edit_text("No recorded position for that token.")
            return

        wallet_balance = await wallet.get_token_balance(mint)
        if wallet_balance <= 0:
            await store.remove_position(mint)
            await notice.edit_text("Wallet shows zero token balance already — clearing position.")
            return

        sell_balance = min(wallet_balance, max(0.0, float(pos.amount_tokens or 0.0))) * fraction
        raw_units = to_raw_units(sell_balance, pos.decimals)
        if raw_units <= 0:
            await notice.edit_text("❌ Close amount is too small for the token precision.")
            return

        result = await sell_token(mint, raw_units, pos.decimals)
        if not result.success:
            await notice.edit_text(result.error or "❌ Sell failed")
            return

        try:
            ov = await security.get_token_overview(mint)
            exit_price = ov.price_usd if ov.found else None
        except Exception:
            exit_price = None

        close_event = {
            "closed_at": time.time(),
            "fraction": fraction,
            "tokens": sell_balance,
            "exit_price_usd": exit_price,
            "sell_signature": result.signature,
            "reason": "MANUAL_CLOSE",
        }
        pos.close_events = list(pos.close_events or []) + [close_event]

        if fraction >= 0.999999:
            pnl = await _save_closed_trade(pos, mint, result.signature, "MANUAL_CLOSE", exit_price, sell_balance)
            await store.remove_position(mint)
        else:
            pnl = ((exit_price - pos.entry_price_usd) / pos.entry_price_usd * 100) if exit_price and pos.entry_price_usd else None
            await store.upsert_position(pos)
            await store.reduce_position_amount(mint, sell_balance)

        label = "Closed 100%" if fraction >= 0.999999 else f"Sold {pct:g}%"
        pnl_text = f"\nPnL at exit: `{pnl:+.2f}%`" if pnl is not None else ""
        await notice.edit_text(f"✅ {label} of ${pos.symbol}{pnl_text}\nTx: `{result.signature}`\n\n📚 Saved/updated in trade history.", parse_mode="Markdown")
    except Exception as exc:
        log.exception("Manual sell failed: %s", type(exc).__name__)
        await notice.edit_text("❌ Sell failed due to a temporary error.")


def _close_position_kb(mint: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("25%", callback_data=f"sell:{mint}:0.25"), InlineKeyboardButton("50%", callback_data=f"sell:{mint}:0.50"), InlineKeyboardButton("75%", callback_data=f"sell:{mint}:0.75")],
        [InlineKeyboardButton("100% Close", callback_data=f"sell:{mint}:1.00"), InlineKeyboardButton("✏️ Write %", callback_data=f"sellcustom:{mint}")],
        [InlineKeyboardButton("⬅ Positions", callback_data="positions")],
    ])


def _back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])


async def show_close_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, mint: str):
    positions = await store.get_positions()
    pos = positions.get(mint)
    if not pos:
        await update.callback_query.edit_message_text("No open position for this token.", reply_markup=_back_kb())
        return
    await update.callback_query.edit_message_text(
        f"💼 *Close ${pos.symbol} position*\n\nChoose how much of the current token balance to sell:\n• 25%\n• 50%\n• 75%\n• 100%\n• Write any percentage from 0.01–100%",
        reply_markup=_close_position_kb(mint),
        parse_mode="Markdown",
    )


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await store.get_settings()
    text = f"*Settings*\n\nSlippage: {st.get('slippage_bps', 500)/100:.1f}%\nPriority fee: {st.get('priority_fee_microlamports', 100000)} microlamports\nDefault TP: None (Managed by Smart-SL)\nDefault Hard SL: -{st.get('default_sl_pct', 30.0)}%"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("5%", callback_data="slip:500"), InlineKeyboardButton("15%", callback_data="slip:1500"), InlineKeyboardButton("25%", callback_data="slip:2500")], [InlineKeyboardButton("⬅ Back", callback_data="refresh_dashboard")]])
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


@admin_only
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = update.effective_user
    if not user or user.id not in settings.admin_ids:
        await query.answer("Not authorized", show_alert=True)
        return

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
        if st.get("auto_sniper_enabled"):
            await store.set_setting("auto_sniper_enabled", False)
            await store.set_setting("auto_sniper_allocation_pct", None)
            await query.edit_message_text("🔴 *Auto-Sniper OFF*\n\nNo automatic buys are running.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Dashboard", callback_data="refresh_dashboard")]]))
            return
        await query.edit_message_text("🎯 *Enable Auto-Sniper*\n\nChoose the percentage of spendable SOL for each new trade.\n\nProtected fee reserve + safety buffer are excluded.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("75%", callback_data="sniperpct:75"), InlineKeyboardButton("65%", callback_data="sniperpct:65")], [InlineKeyboardButton("25%", callback_data="sniperpct:25"), InlineKeyboardButton("10%", callback_data="sniperpct:10")], [InlineKeyboardButton("❌ Cancel", callback_data="snipercancel")]]))
    elif data.startswith("sniperpct:"):
        pct = float(data.split(":", 1)[1])
        balance = await wallet.get_sol_balance() if wallet.configured else 0
        reserve = settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol
        spendable = max(0, balance - reserve)
        estimated = min(spendable * pct / 100, settings.max_buy_sol)
        await query.edit_message_text(f"🎯 *Confirm Auto-Sniper*\n\nAllocation: *{pct:g}%*\nWallet: `{balance:.6f} SOL`\nProtected: `{reserve:.6f} SOL`\nSpendable: `{spendable:.6f} SOL`\nEstimated trade: `{estimated:.9f} SOL`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm & Enable", callback_data=f"sniperconfirm:{pct:g}"), InlineKeyboardButton("⬅ Change", callback_data="toggle_sniper")], [InlineKeyboardButton("❌ Cancel", callback_data="snipercancel")]]))
    elif data.startswith("sniperconfirm:"):
        pct = float(data.split(":", 1)[1])
        if not 0 < pct <= 100:
            await query.edit_message_text("❌ Invalid allocation.")
            return
        await store.set_setting("auto_sniper_allocation_pct", pct)
        await store.set_setting("auto_sniper_enabled", True)
        await query.edit_message_text(f"🟢 *Auto-Sniper ON*\n\nAllocation: `{pct:g}%` of spendable SOL per trade.\nLive balance and protected reserve are checked before every buy.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔴 Turn Sniper OFF", callback_data="toggle_sniper")], [InlineKeyboardButton("🏠 Dashboard", callback_data="refresh_dashboard")]]))
    elif data == "snipercancel":
        await query.edit_message_text("Auto-Sniper setup cancelled. It remains OFF.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Dashboard", callback_data="refresh_dashboard")]]))
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
    elif data.startswith("closemenu:"):
        _, mint = data.split(":", 1)
        await show_close_menu(update, context, mint)
    elif data.startswith("sellcustom:"):
        _, mint = data.split(":", 1)
        positions = await store.get_positions()
        if mint not in positions:
            await query.edit_message_text("No open position for that token.", reply_markup=_back_kb())
            return
        _awaiting_custom_sell_pct[query.message.chat_id] = mint
        await query.edit_message_text(
            f"✏️ *Manual position close*\n\nEnter the percentage to sell for `{mint[:8]}...`\n\nAllowed: `0.01` to `100`\nExamples: `25`, `37.5`, `82.25`, `100`",
            parse_mode="Markdown",
        )
    elif data.startswith("sell:"):
        _, mint, fraction = data.split(":")
        await do_sell(update, context, mint, float(fraction))
    elif data.startswith("slip:"):
        _, bps = data.split(":")
        await store.set_setting("slippage_bps", int(bps))
        await show_settings(update, context)


async def combined_daemon(context: ContextTypes.DEFAULT_TYPE):
    await smart_sl.tick(context)
    await sniper.tick(context)


def main():
    settings.validate()
    app = Application.builder().token(settings.telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("connect_wallet", connect_wallet_cmd))
    app.add_handler(CommandHandler("cancel_wallet", cancel_wallet_cmd))
    app.add_handler(CommandHandler("disconnect_wallet", disconnect_wallet_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Single source of truth for background tasks: Smart SL + Sniper
    app.job_queue.run_repeating(combined_daemon, interval=settings.auto_sniper_poll_seconds, first=10)
    app.job_queue.run_repeating(live_pnl_daemon, interval=10, first=15)
    log.info("Bot starting — whitelisted admins: %s", settings.admin_ids)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
