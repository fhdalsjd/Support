"""Smart stop-loss manager for open Solana positions.

Policy:
- Keep the original hard SL while the trade is not proven.
- At +SMART_SL_ACTIVATION_PCT, move the stop to break-even.
- Once the trade reaches +SMART_SL_PROFIT_LOCK_START_PCT, lock
  SMART_SL_LOCK_RATIO (default 50%) of the best profit seen.
- Ratchet the locked-profit level only after another configured profit step.
- The stop can only move toward profit; it is never widened.

This manager closes at market when the current PnL reaches the smart stop.
It runs in the same asyncio loop as the Telegram bot so wallet/RPC clients are
not shared across threads.
"""
from __future__ import annotations

import logging
from telegram.ext import ContextTypes

from config import settings
from security import get_token_overview
from state import Position, store
from trading import sell_token
from wallet import wallet

log = logging.getLogger("smart-sl")


async def _close(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mint: str, pos: Position, reason: str):
    try:
        balance = await wallet.get_token_balance(mint)
        if balance <= 0:
            await store.remove_position(mint)
            return True

        raw_units = int(balance * (10 ** pos.decimals))
        result = await sell_token(mint, raw_units, pos.decimals)
        if result.success:
            await store.remove_position(mint)
            await context.bot.send_message(
                admin_id,
                f"🧠 Smart SL closed *${pos.symbol}* — {reason}\nTx: `{result.signature}`",
                parse_mode="Markdown",
            )
            return True

        await context.bot.send_message(
            admin_id,
            f"⚠️ Smart SL sell FAILED for ${pos.symbol}: {result.error}",
        )
        return False
    except Exception as exc:
        log.warning("Smart SL close failed: %s", type(exc).__name__)
        return False


async def tick(context: ContextTypes.DEFAULT_TYPE):
    """Update and enforce the smart SL for every open position."""
    if not wallet.configured or not settings.admin_ids:
        return

    positions = await store.get_positions()
    if not positions:
        return

    admin_id = next(iter(settings.admin_ids))

    for mint, pos in positions.items():
        try:
            overview = await get_token_overview(mint)
            if not overview.found or overview.price_usd <= 0 or pos.entry_price_usd <= 0:
                continue

            pnl_pct = (overview.price_usd - pos.entry_price_usd) / pos.entry_price_usd * 100.0
            old_peak = pos.peak_profit_pct or 0.0
            peak = max(old_peak, pnl_pct)

            changed = False
            if peak > old_peak:
                pos.peak_profit_pct = peak
                changed = True

            current_stop = pos.smart_stop_profit_pct

            # First protection: once the trade proves itself, eliminate downside.
            if peak >= settings.smart_sl_activation_pct:
                if current_stop is None or current_stop < 0:
                    pos.smart_stop_profit_pct = 0.0
                    current_stop = 0.0
                    changed = True

            # Profit lock: after the stronger threshold, lock 50% (by default) of
            # the best profit reached. Ratcheting prevents tiny quote noise from
            # constantly rewriting state and keeps the stop from moving backward.
            if peak >= settings.smart_sl_profit_lock_start_pct:
                candidate = max(0.0, peak * settings.smart_sl_lock_ratio)
                if current_stop is None or candidate >= current_stop + settings.smart_sl_ratchet_step_pct:
                    pos.smart_stop_profit_pct = candidate
                    current_stop = candidate
                    changed = True

            if changed:
                await store.upsert_position(pos)
                log.info(
                    "Smart SL %s: pnl=%+.2f%% peak=%+.2f%% locked=%s",
                    mint[:8],
                    pnl_pct,
                    pos.peak_profit_pct,
                    f"{pos.smart_stop_profit_pct:+.2f}%" if pos.smart_stop_profit_pct is not None else "off",
                )

            # Enforce the smart stop immediately. The original TP/SL daemon still
            # handles the hard SL, TP, and any manually configured trailing SL.
            if pos.smart_stop_profit_pct is not None and pnl_pct <= pos.smart_stop_profit_pct:
                await _close(
                    context,
                    admin_id,
                    mint,
                    pos,
                    f"profit lock {pos.smart_stop_profit_pct:+.1f}% hit (peak +{pos.peak_profit_pct:.1f}%, now {pnl_pct:+.1f}%)",
                )

        except Exception as exc:
            log.warning("Smart SL check failed: %s", type(exc).__name__)
