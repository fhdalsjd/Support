"""Opt-in Solana discovery and guarded auto-buy loop.

Auto-trading is fail-closed: the same deterministic snapshot used for the
analysis view is used for every gate immediately before the buy.
"""
from __future__ import annotations

import logging
import time

import httpx
from solders.pubkey import Pubkey

from config import settings
from security import analyze_token
from state import Position, store
from trading import SOL_MINT, buy_token, get_quote
from wallet import wallet

log = logging.getLogger("sniper")
_last_checked: dict[str, float] = {}
_last_buy_at = 0.0
_RECHECK_SECONDS = 300


async def _latest_profiles() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get("https://api.dexscreener.com/token-profiles/latest/v1")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []


async def _auto_exposure_sol(positions: dict[str, Position]) -> float:
    return sum(max(float(getattr(p, "entry_sol", 0.0) or 0.0), 0.0) for p in positions.values())


async def _available_trade_sol() -> tuple[float, float]:
    """Return spendable SOL after the protected sell-fee reserve and buffer."""
    balance = await wallet.get_sol_balance()
    protected = settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol
    return max(0.0, balance - protected), balance


def _entry_snapshot(overview, rug, analysis, allocation_pct: float, trade_amount: float, quote_impact: float) -> dict:
    """Freeze the exact token intelligence used to approve an automatic entry."""
    return {
        "source": "auto_sniper",
        "allocation_pct": allocation_pct,
        "trade_amount_sol": trade_amount,
        "price_impact_pct": quote_impact,
        "risk_score": getattr(analysis, "risk_score", None),
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


async def tick(context) -> None:
    global _last_buy_at

    st = await store.get_settings()
    if not st.get("auto_sniper_enabled") or not wallet.configured:
        return

    allocation_pct = st.get("auto_sniper_allocation_pct")
    if allocation_pct is None:
        return
    try:
        allocation_pct = float(allocation_pct)
    except (TypeError, ValueError):
        return
    if not 0 < allocation_pct <= 100:
        return

    if time.time() - _last_buy_at < settings.auto_sniper_cooldown_seconds:
        return

    positions = await store.get_positions()
    if len(positions) >= settings.auto_sniper_max_positions:
        return

    available_sol, balance = await _available_trade_sol()
    trade_amount = min(available_sol * allocation_pct / 100.0, settings.max_buy_sol)
    if trade_amount <= 0:
        return
    if await _auto_exposure_sol(positions) + trade_amount > settings.auto_sniper_max_exposure_sol:
        return
    if balance - trade_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
        return

    try:
        profiles = await _latest_profiles()
    except Exception as exc:
        log.warning("Auto-sniper discovery failed: %s", type(exc).__name__)
        return

    now = time.time()
    for profile in profiles:
        if profile.get("chainId") != "solana":
            continue
        mint = profile.get("tokenAddress")
        if not mint or now - _last_checked.get(mint, 0) < _RECHECK_SECONDS:
            continue
        _last_checked[mint] = now

        try:
            overview, rug, analysis = await analyze_token(mint)
            if not overview.found or overview.price_usd <= 0:
                continue
            if overview.data_quality != "HIGH":
                log.info("Auto-sniper blocked %s: data quality=%s warnings=%s", mint, overview.data_quality, overview.data_warnings)
                continue
            if overview.market_cap <= 0 or overview.total_supply <= 0 or overview.fdv <= 0:
                continue
            if not rug.mint_authority_revoked or not rug.freeze_authority_revoked:
                continue
            if rug.top_holder_pct is None or rug.top_holder_pct > 10:
                continue

            liquidity = overview.liquidity_usd
            market_cap = overview.market_cap
            volume_5m = overview.volume_5m
            change_5m = overview.change_5m
            age = overview.age_minutes
            ratio = overview.buy_sell_ratio_5m
            trades_5m = overview.total_trades_5m

            if liquidity < settings.auto_sniper_min_liquidity_usd:
                continue
            if market_cap < settings.auto_sniper_min_market_cap_usd:
                continue
            if volume_5m < settings.auto_sniper_min_volume_5m_usd:
                continue
            if overview.liquidity_mcap_pct < settings.auto_sniper_min_liquidity_mcap_pct:
                continue
            if change_5m <= 0 or change_5m > settings.auto_sniper_max_5m_change_pct:
                continue
            if age is None or age < settings.auto_sniper_min_age_minutes or age > settings.auto_sniper_max_age_minutes:
                continue
            if trades_5m < 5:
                continue
            if overview.sells_5m > 0 and ratio < settings.auto_sniper_min_buy_sell_ratio:
                continue
            if analysis.risk_score > settings.auto_sniper_max_risk_score or rug.risk_level != "LOW":
                continue

            current_positions = await store.get_positions()
            if mint in current_positions or len(current_positions) >= settings.auto_sniper_max_positions:
                continue

            # Recalculate immediately before quoting.
            available_sol, balance = await _available_trade_sol()
            trade_amount = min(available_sol * allocation_pct / 100.0, settings.max_buy_sol)
            remaining_exposure = settings.auto_sniper_max_exposure_sol - await _auto_exposure_sol(current_positions)
            trade_amount = min(trade_amount, remaining_exposure)
            if trade_amount <= 0 or balance - trade_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                continue

            # Quote the exact final amount that will be submitted. This keeps
            # the Jupiter price-impact gate consistent with the actual spend.
            quote = await get_quote(SOL_MINT, mint, int(trade_amount * 1_000_000_000), settings.auto_sniper_slippage_bps)
            if quote.price_impact_pct > settings.auto_sniper_max_price_impact_pct:
                continue

            # One last balance check; if balance changed, skip this candidate
            # rather than reusing a quote for a different amount.
            available_sol_now, balance_now = await _available_trade_sol()
            final_amount = min(available_sol_now * allocation_pct / 100.0, settings.max_buy_sol, remaining_exposure)
            if final_amount <= 0 or balance_now - final_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                continue
            if abs(final_amount - trade_amount) > 1e-12:
                continue

            before_balance = await wallet.get_token_balance(mint)
            result = await buy_token(mint, final_amount, settings.auto_sniper_slippage_bps)
            if not result.success:
                log.warning("Auto-sniper buy failed for %s: %s", mint, result.error)
                continue

            try:
                decimals = (await wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
            except Exception:
                decimals = overview.decimals or 9
            after_balance = await wallet.get_token_balance(mint)
            token_delta = max(0.0, after_balance - before_balance)
            if token_delta <= 0:
                log.error("Auto-buy confirmed but token balance delta is zero for %s", mint)
                await context.bot.send_message(next(iter(settings.admin_ids)), f"⚠️ Auto-buy confirmed for ${overview.symbol}, but position balance could not be measured. Tx: `{result.signature}`", parse_mode="Markdown")
                return

            symbol = overview.symbol or "?"
            pos = Position(
                mint=mint,
                symbol=symbol,
                entry_price_usd=overview.price_usd,
                amount_tokens=token_delta,
                decimals=decimals,
                take_profit_pct=st["default_tp_pct"],
                stop_loss_pct=st["default_sl_pct"],
                entry_sol=final_amount,
                opened_at=time.time(),
                buy_signature=result.signature,
                entry_snapshot=_entry_snapshot(overview, rug, analysis, allocation_pct, final_amount, quote.price_impact_pct),
            )
            await store.upsert_position(pos)
            _last_buy_at = time.time()
            await context.bot.send_message(
                next(iter(settings.admin_ids)),
                f"🤖 *AUTO-SNIPER BUY — POSITION OPEN*\n\n"
                f"Token: `${symbol}`\nName: `{overview.name}`\nMint / CA: `{mint}`\n\n"
                f"Entry: `${overview.price_usd:.10f}`\nTokens: `{token_delta:.8g}`\nSpend: `{final_amount:.9f} SOL`\n"
                f"Allocation: `{allocation_pct:g}%` of spendable SOL\nTP: `+{st['default_tp_pct']}%` • Hard SL: `-{st['default_sl_pct']}%`\n\n"
                f"Risk: `{analysis.risk_score}/100` • RugCheck: `{rug.risk_level}`\n"
                f"Liquidity: `${liquidity:,.0f}` • MC: `${market_cap:,.0f}` • FDV: `${overview.fdv:,.0f}`\n"
                f"5m volume: `${volume_5m:,.0f}` • 5m trades: `{trades_5m}` • Buy/Sell: `{ratio:.2f}`\n"
                f"Momentum: 5m `{change_5m:+.1f}%` • 1h `{overview.change_1h:+.1f}%`\n"
                f"Pools: `{overview.pool_count}` • DEX: `{overview.dex}` • Data: `{overview.data_quality}`\n"
                f"Jupiter impact: `{quote.price_impact_pct:.2f}%`\n"
                f"Protected reserve: `{settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:.3f} SOL`\n"
                f"Buy Tx: `{result.signature}`",
                parse_mode="Markdown",
            )
            return
        except Exception as exc:
            log.warning("Auto-sniper candidate check failed: %s", type(exc).__name__)
