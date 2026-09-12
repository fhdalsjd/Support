"""Professional Profit-Protection Engine with Moonbag, Volume-Decay, and Ratchet SL.

Exit policy (checked in this order, per tick, per position):
- 0. Velocity panic exit: if price falls SMART_SL_VELOCITY_DROP_PCT within
  SMART_SL_VELOCITY_WINDOW_MINUTES, close immediately -- regardless of PnL
  from entry. This is checked first because it reacts to *speed*, which none
  of the checks below do: a token that ran to a big peak and then crashes
  hard in a few minutes can still be net-positive from entry (hard SL blind
  to it) and may not yet have reached its locked profit floor (ratchet blind
  to it too).
- 1. Moonbag: at +100% peak, sell 50% to pull the initial SOL back out.
- 2. Emergency dump guard: heavy sell pressure / dev dump detection.
- 3. Time-decay / stagnation exit: dies out on volume after a grace period.
- 4. Hard downside protection, -30% by default (fixed level, from entry).
- 5. Ratcheting smart SL: break-even at activation, then a trailing profit
  floor that only ever moves forward.
- Every completed smart-SL exit is written to permanent trade history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from telegram.ext import ContextTypes

from config import settings
import security
from state import Position, store
from trading import sell_token, to_raw_units
from wallet import wallet

log = logging.getLogger("smart-sl")
_smart_sl_lock = asyncio.Lock()

MOONBAG_TARGET_PNL = 100.0        # At +100% (2x), sell 50% to recover 100% of initial SOL capital
TIME_DECAY_MINUTES = 12.0          # If stagnant after 12 minutes, check volume
TIME_DECAY_MIN_VOL_5M = 1500.0     # If 5m volume drops below $1500, exit early

# Per-mint rolling (timestamp, price) history for the velocity panic exit.
# In-memory only and intentionally not persisted to state.json: it is a
# short rolling window (a few minutes), so losing it on a restart just means
# the window rebuilds from the next few ticks -- it never affects hard SL or
# the profit-lock ratchet, which are both computed from pos.entry_price_usd
# / pos.peak_profit_pct in state.json as before.
_price_history: dict[str, list[tuple[float, float]]] = {}


async def _execute_exit(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    mint: str,
    pos: Position,
    sell_fraction: float,
    reason: str,
    exit_price: float,
) -> bool:
    """Execute partial or full sell and sync with permanent trade history."""
    try:
        # Prevent race with manual close or another task
        current_positions = await store.get_positions()
        if mint not in current_positions:
            return True

        wallet_balance = await wallet.get_token_balance(mint)
        recorded_balance = max(0.0, float(pos.amount_tokens or 0.0))
        available_balance = min(wallet_balance, recorded_balance)
        if available_balance <= 0:
            if wallet_balance <= 0:
                await store.remove_position(mint)
            return True

        sell_tokens = available_balance * sell_fraction
        raw_units = to_raw_units(sell_tokens, pos.decimals)
        if raw_units <= 0:
            return False

        result = await sell_token(mint, raw_units, pos.decimals)
        if not result.success:
            await context.bot.send_message(admin_id, f"⚠️ Sell FAILED for ${pos.symbol}: {result.error}")
            return False

        entry = float(pos.entry_price_usd or 0.0)
        pnl_pct = ((exit_price - entry) / entry * 100.0) if entry > 0 and exit_price else None

        close_event = {
            "closed_at": time.time(),
            "fraction": sell_fraction,
            "tokens": sell_tokens,
            "exit_price_usd": exit_price,
            "sell_signature": result.signature,
            "reason": reason,
            "pnl_pct": pnl_pct,
        }
        pos.close_events = list(pos.close_events or []) + [close_event]

        if sell_fraction >= 0.999999:
            record = {
                "closed_at": close_event["closed_at"],
                "opened_at": pos.opened_at or None,
                "mint": mint,
                "symbol": pos.symbol,
                "decimals": pos.decimals,
                "entry_price_usd": entry,
                "exit_price_usd": exit_price,
                "entry_sol": float(pos.entry_sol or 0.0),
                "pnl_pct": pnl_pct,
                "pnl_sol_estimate": (float(pos.entry_sol or 0.0) * pnl_pct / 100.0) if pnl_pct is not None else None,
                "amount_tokens": pos.amount_tokens,
                "exit_tokens": sell_tokens,
                "close_reason": reason,
                "buy_signature": pos.buy_signature,
                "sell_signature": result.signature,
                "entry_snapshot": dict(pos.entry_snapshot or {}),
                "close_events": list(pos.close_events),
            }
            await store.append_history(record)
            await store.remove_position(mint)
            pnl_text = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "unavailable"
            await context.bot.send_message(
                admin_id,
                f"🧠 Smart SL closed 100% *${pos.symbol}* — {reason}\nPnL: `{pnl_text}`\nTx: `{result.signature}`\n📚 Saved to History",
                parse_mode="Markdown",
            )
        else:
            # Moonbag partial exit
            pos.entry_sol = max(0.0, float(pos.entry_sol or 0.0) * (1.0 - sell_fraction))
            await store.upsert_position(pos)
            await store.reduce_position_amount(mint, sell_tokens)
            await context.bot.send_message(
                admin_id,
                f"🌕 *MOONBAG SECURED — ${pos.symbol}*\n\n"
                f"Sold: `50%` at `{pnl_pct:+.1f}%` (Initial SOL investment pulled out!)\n"
                f"Remaining tokens are now 100% risk-free moonbag.\n"
                f"Tx: `{result.signature}`",
                parse_mode="Markdown",
            )
        return True
    except Exception as exc:
        log.warning("Smart SL execution failed: %s", type(exc).__name__)
        return False


async def tick(context: ContextTypes.DEFAULT_TYPE):
    if not wallet.configured or not settings.admin_ids:
        return

    if _smart_sl_lock.locked():
        log.debug("Smart SL tick skipped: previous tick still active")
        return

    async with _smart_sl_lock:
        positions = await store.get_positions()
        if not positions:
            _price_history.clear()
            return

        # Drop history for any mint no longer an open position (e.g. closed by
        # a manual sell from trading_bot.py, which this module has no other
        # way of observing) so a later re-buy of the same mint starts fresh.
        for stale_mint in list(_price_history.keys()):
            if stale_mint not in positions:
                _price_history.pop(stale_mint, None)

        admin_id = next(iter(settings.admin_ids))
        now = time.time()

        for mint, pos in list(positions.items()):
            try:
                overview = await security.get_token_overview(mint)

                # Self-heal decimals and entry price
                healed = False
                if overview.found and overview.decimals > 0 and overview.decimals != pos.decimals:
                    log.warning("Correcting stored decimals for %s: %s -> %s", mint, pos.decimals, overview.decimals)
                    pos.decimals = overview.decimals
                    healed = True
                if pos.entry_price_usd <= 0 and overview.found and overview.price_usd > 0:
                    pos.entry_price_usd = overview.price_usd
                    healed = True
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"ℹ️ ${pos.symbol}: entry price was missing — backfilled at current price (${overview.price_usd:.8f}) so Smart-SL is active.",
                        )
                    except Exception:
                        pass
                if healed:
                    await store.upsert_position(pos)

                if not overview.found or overview.price_usd <= 0 or pos.entry_price_usd <= 0:
                    continue

                cur_price = overview.price_usd
                pnl_pct = (cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100.0
                peak = max(pos.peak_profit_pct or 0.0, pnl_pct)
                changed = False

                if peak > (pos.peak_profit_pct or 0.0):
                    pos.peak_profit_pct = peak
                    changed = True

                # 0. VELOCITY PANIC EXIT: react to the speed of a crash, not just
                # its size from entry -- see module docstring for why this has
                # to run before everything else.
                history = _price_history.setdefault(mint, [])
                history.append((now, cur_price))
                window_seconds = getattr(settings, "smart_sl_velocity_window_minutes", 3.0) * 60.0
                cutoff = now - window_seconds
                while len(history) > 1 and history[0][0] < cutoff:
                    history.pop(0)
                # Require at least half the window of real history before
                # evaluating, so a token bought seconds ago can't false-trigger
                # off a single noisy price sample.
                if len(history) >= 2 and (now - history[0][0]) >= window_seconds * 0.5:
                    window_high = max(p for _, p in history)
                    velocity_drop_pct = getattr(settings, "smart_sl_velocity_drop_pct", 20.0)
                    if window_high > 0:
                        drop_pct = (window_high - cur_price) / window_high * 100.0
                        if drop_pct >= velocity_drop_pct:
                            await store.upsert_position(pos)
                            await _execute_exit(
                                context,
                                admin_id,
                                mint,
                                pos,
                                1.0,
                                f"Velocity panic exit ({drop_pct:.1f}% drop in "
                                f"{(now - history[0][0]) / 60.0:.1f}m)",
                                cur_price,
                            )
                            _price_history.pop(mint, None)
                            continue

                # 1. MOONBAG STRATEGY: At +100% (2x), sell 50% to withdraw initial investment
                moonbag_taken = (pos.entry_snapshot or {}).get("moonbag_taken", False)
                if pnl_pct >= MOONBAG_TARGET_PNL and not moonbag_taken:
                    pos.entry_snapshot["moonbag_taken"] = True
                    pos.smart_stop_profit_pct = 25.0  # Floor profit for the remaining 50%
                    await store.upsert_position(pos)
                    await _execute_exit(context, admin_id, mint, pos, 0.50, "Moonbag 50% Take-Profit", cur_price)
                    continue

                # 2. EMERGENCY DUMP GUARD: Heavy sell pressure or dev dump
                ratio = overview.buy_sell_ratio_5m
                if overview.sells_5m >= 10 and ratio < 0.25 and pnl_pct < 0:
                    await store.upsert_position(pos)
                    await _execute_exit(context, admin_id, mint, pos, 1.0, f"Emergency Panic Exit (Heavy Sells, Ratio {ratio:.2f})", cur_price)
                    continue

                # 3. TIME-DECAY / STAGNATION EXIT: Early exit if volume dies out
                age_minutes = (now - (pos.opened_at or now)) / 60.0
                if age_minutes >= TIME_DECAY_MINUTES and pnl_pct < 5.0 and overview.volume_5m < TIME_DECAY_MIN_VOL_5M:
                    await store.upsert_position(pos)
                    await _execute_exit(context, admin_id, mint, pos, 1.0, f"Stagnation Exit ({age_minutes:.0f}m stagnant, low volume)", cur_price)
                    continue

                # 4. HARD STOP LOSS (-30%)
                hard_sl = float(pos.stop_loss_pct if pos.stop_loss_pct is not None else 30.0)
                if hard_sl <= 0:
                    hard_sl = 30.0
                if pnl_pct <= -hard_sl:
                    await store.upsert_position(pos)
                    await _execute_exit(context, admin_id, mint, pos, 1.0, f"Hard SL hit ({pnl_pct:.1f}%)", cur_price)
                    continue

                # 5. RATCHETING SMART SL (Break-even and trailing profit floor)
                current_stop = pos.smart_stop_profit_pct
                if peak >= settings.smart_sl_activation_pct and (current_stop is None or current_stop < 0):
                    pos.smart_stop_profit_pct = 0.0
                    current_stop = 0.0
                    changed = True

                if peak >= settings.smart_sl_profit_lock_start_pct:
                    candidate = max(0.0, peak * settings.smart_sl_lock_ratio)
                    if current_stop is None or candidate >= current_stop + settings.smart_sl_ratchet_step_pct:
                        pos.smart_stop_profit_pct = candidate
                        current_stop = candidate
                        changed = True

                if changed:
                    await store.upsert_position(pos)

                if pos.smart_stop_profit_pct is not None and pnl_pct <= pos.smart_stop_profit_pct:
                    await _execute_exit(
                        context,
                        admin_id,
                        mint,
                        pos,
                        1.0,
                        f"Smart Lock {pos.smart_stop_profit_pct:+.1f}% hit (Peak +{pos.peak_profit_pct:.1f}%)",
                        cur_price,
                    )
            except Exception as exc:
                log.warning("Smart SL check failed for %s: %s", mint, type(exc).__name__)
