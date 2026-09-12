"""Deterministic profit-protection engine for every open position.

Exit policy:
- No fixed take-profit.
- Velocity panic exit: if price falls SMART_SL_VELOCITY_DROP_PCT within
  SMART_SL_VELOCITY_WINDOW_MINUTES, close immediately -- regardless of PnL
  from entry. This is checked before everything else because it reacts to
  *speed*, which neither of the two checks below do.
- Hard downside protection starts at -30% by default (fixed level, from
  entry price).
- At +5% peak profit, stop moves to break-even.
- At +10% peak profit, stop locks 50% of peak profit.
- Thereafter the stop ratchets upward to 50% of the best profit reached,
  only after the configured ratchet step.
- The stop never moves backward.
- Every completed smart-SL exit is written to permanent trade history.
"""
from __future__ import annotations

import logging
import time
from telegram.ext import ContextTypes

from config import settings
import security  # module import so sitecustomize.py's live-pricing patch is honored — see trading_bot.py for why
from state import Position, store
from trading import sell_token
from wallet import wallet

log = logging.getLogger("smart-sl")

# Per-mint rolling (timestamp, price) history for the velocity panic exit.
# In-memory only and intentionally not persisted to state.json: it is a
# short rolling window (a few minutes), so losing it on a restart just means
# the window rebuilds from the next few ticks -- it never affects hard SL or
# the profit-lock ratchet, which are both computed from pos.entry_price_usd
# / pos.peak_profit_pct in state.json as before.
_price_history: dict[str, list[tuple[float, float]]] = {}


async def _close(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mint: str, pos: Position, reason: str, exit_price: float | None = None):
    try:
        wallet_balance = await wallet.get_token_balance(mint)
        recorded_balance = max(0.0, float(pos.amount_tokens or 0.0))
        balance = min(wallet_balance, recorded_balance)
        if balance <= 0:
            if wallet_balance <= 0:
                await store.remove_position(mint)
            return True

        raw_units = int(balance * (10 ** pos.decimals))
        if raw_units <= 0:
            return False

        result = await sell_token(mint, raw_units, pos.decimals)
        if not result.success:
            await context.bot.send_message(admin_id, f"⚠️ Smart SL sell FAILED for ${pos.symbol}: {result.error}")
            return False

        if exit_price is None:
            exit_price = pos.entry_price_usd
            try:
                overview = await security.get_token_overview(mint)
                if overview.found and overview.price_usd > 0:
                    exit_price = overview.price_usd
            except Exception:
                pass

        entry = float(pos.entry_price_usd or 0.0)
        pnl_pct = ((exit_price - entry) / entry * 100.0) if entry > 0 and exit_price else None
        event = {
            "closed_at": time.time(),
            "tokens": balance,
            "exit_price_usd": exit_price,
            "sell_signature": result.signature,
            "reason": reason,
            "pnl_pct": pnl_pct,
        }
        pos.close_events = list(pos.close_events or []) + [event]
        record = {
            "closed_at": event["closed_at"],
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
            "exit_tokens": balance,
            "take_profit_pct": None,
            "stop_loss_pct": pos.stop_loss_pct,
            "peak_profit_pct": pos.peak_profit_pct,
            "smart_stop_profit_pct": pos.smart_stop_profit_pct,
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
            f"🧠 Smart SL closed *${pos.symbol}* — {reason}\nPnL: `{pnl_text}`\nTx: `{result.signature}`\n📚 Saved to History",
            parse_mode="Markdown",
        )
        return True
    except Exception as exc:
        log.warning("Smart SL close failed: %s", type(exc).__name__)
        return False


async def tick(context: ContextTypes.DEFAULT_TYPE):
    if not wallet.configured or not settings.admin_ids:
        return
    positions = await store.get_positions()
    if not positions:
        _price_history.clear()
        return

    # Drop history for any mint no longer an open position (e.g. closed by a
    # manual sell from trading_bot.py, which this module has no other way of
    # observing) so a later re-buy of the same mint starts a fresh window.
    for stale_mint in list(_price_history.keys()):
        if stale_mint not in positions:
            _price_history.pop(stale_mint, None)

    admin_id = next(iter(settings.admin_ids))
    for mint, pos in positions.items():
        try:
            overview = await security.get_token_overview(mint)

            # Self-heal positions recorded under the old bug where decimals
            # defaulted to a hardcoded 9 and entry price could be recorded as
            # 0 for a token too new to have pricing data at buy time. Decimals
            # is a fixed mint property, so this is a safe, fully-accurate fix
            # whenever real data is available. Entry price can't be recovered
            # retroactively -- backfilling with the current price isn't
            # historically accurate, but it's strictly better than leaving
            # Smart-SL/hard-SL permanently disabled for that position.
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
                        f"ℹ️ ${pos.symbol}: entry price was missing (likely bought before pricing data existed) — "
                        f"backfilled at the current price so Smart-SL/hard-SL are now active. Historical PnL from "
                        f"before this point is not recoverable.",
                    )
                except Exception:
                    pass
            if healed:
                await store.upsert_position(pos)

            if not overview.found or overview.price_usd <= 0 or pos.entry_price_usd <= 0:
                continue

            pnl_pct = (overview.price_usd - pos.entry_price_usd) / pos.entry_price_usd * 100.0
            old_peak = pos.peak_profit_pct or 0.0
            peak = max(old_peak, pnl_pct)
            changed = False
            if peak > old_peak:
                pos.peak_profit_pct = peak
                changed = True

            # Fixed TP is intentionally ignored. Winners stay open until the
            # smart stop or hard protection closes them.
            pos.take_profit_pct = None
            pos.trailing_sl_pct = None

            # 0. VELOCITY PANIC EXIT. Hard SL only fires once PnL from *entry*
            # crosses -30%, and the profit-lock ratchet only protects the
            # specific floor already locked -- so a token that ran to, say,
            # +40% peak and then crashes 20% in a few minutes can slip
            # through both: it's still net-positive from entry (hard SL
            # blind to it) and the crash may not yet have reached the locked
            # floor (ratchet blind to it too). This check reacts to the drop
            # itself, independent of where entry or peak were.
            now = time.time()
            history = _price_history.setdefault(mint, [])
            history.append((now, overview.price_usd))
            window_seconds = settings.smart_sl_velocity_window_minutes * 60.0
            cutoff = now - window_seconds
            while len(history) > 1 and history[0][0] < cutoff:
                history.pop(0)
            # Require at least half the window of real history before
            # evaluating, so a token bought seconds ago can't false-trigger
            # off a single noisy price sample.
            if len(history) >= 2 and (now - history[0][0]) >= window_seconds * 0.5:
                window_high = max(p for _, p in history)
                if window_high > 0:
                    drop_pct = (window_high - overview.price_usd) / window_high * 100.0
                    if drop_pct >= settings.smart_sl_velocity_drop_pct:
                        await store.upsert_position(pos)
                        await _close(
                            context,
                            admin_id,
                            mint,
                            pos,
                            f"Velocity panic exit ({drop_pct:.1f}% drop in "
                            f"{(now - history[0][0]) / 60.0:.1f}m)",
                            overview.price_usd,
                        )
                        _price_history.pop(mint, None)
                        continue

            # Hard downside protection. The default is -30%, but the recorded
            # position value is respected if an explicit positive SL exists.
            hard_sl = float(pos.stop_loss_pct if pos.stop_loss_pct is not None else 30.0)
            if hard_sl <= 0:
                hard_sl = 30.0
            if pnl_pct <= -hard_sl:
                await store.upsert_position(pos)
                await _close(context, admin_id, mint, pos, f"Hard SL hit ({pnl_pct:.1f}%)", overview.price_usd)
                continue

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

            # A smart stop is a profit floor, not a fixed TP. A strong winner
            # can therefore continue running while its protected floor ratchets.
            if pos.smart_stop_profit_pct is not None and pnl_pct <= pos.smart_stop_profit_pct:
                await _close(
                    context,
                    admin_id,
                    mint,
                    pos,
                    f"Profit lock {pos.smart_stop_profit_pct:+.1f}% hit (peak +{pos.peak_profit_pct:.1f}%, now {pnl_pct:+.1f}%)",
                    overview.price_usd,
                )
        except Exception as exc:
            log.warning("Smart SL check failed: %s", type(exc).__name__)
