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
    return sum(
        max(float(getattr(p, "entry_sol", 0.0) or 0.0), settings.auto_sniper_buy_sol)
        for p in positions.values()
    )


async def _buy_is_funded_safely() -> tuple[bool, float, float]:
    """Return whether a new auto-buy leaves the protected SOL reserve intact."""
    balance = await wallet.get_sol_balance()
    required = (
        settings.auto_sniper_buy_sol
        + settings.auto_fee_reserve_sol
        + settings.auto_safety_buffer_sol
    )
    return balance >= required, balance, required


async def tick(context) -> None:
    global _last_buy_at

    st = await store.get_settings()
    if not st.get("auto_sniper_enabled") or not wallet.configured:
        return
    if time.time() - _last_buy_at < settings.auto_sniper_cooldown_seconds:
        return

    positions = await store.get_positions()
    if len(positions) >= settings.auto_sniper_max_positions:
        return
    if await _auto_exposure_sol(positions) + settings.auto_sniper_buy_sol > settings.auto_sniper_max_exposure_sol:
        return

    # Protect the SOL needed to execute future TP/SL/Smart-SL sells before
    # doing any market discovery or spending. Never use the full wallet balance.
    funded, balance, required = await _buy_is_funded_safely()
    if not funded:
        log.info(
            "Auto-sniper waiting for fee reserve: balance=%.6f required=%.6f",
            balance,
            required,
        )
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
            # One deterministic snapshot feeds both the analysis/risk numbers
            # and every execution gate. No second market lookup is used.
            overview, rug, analysis = await analyze_token(mint)
            if not overview.found or overview.price_usd <= 0:
                continue

            if overview.data_quality != "HIGH":
                log.info(
                    "Auto-sniper blocked %s: data quality=%s warnings=%s",
                    mint,
                    overview.data_quality,
                    overview.data_warnings,
                )
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
            if await _auto_exposure_sol(current_positions) + settings.auto_sniper_buy_sol > settings.auto_sniper_max_exposure_sol:
                continue

            quote = await get_quote(
                SOL_MINT,
                mint,
                int(settings.auto_sniper_buy_sol * 1_000_000_000),
                settings.auto_sniper_slippage_bps,
            )
            if quote.price_impact_pct > settings.auto_sniper_max_price_impact_pct:
                continue

            # Re-check the wallet immediately before execution. This closes
            # the race where another transaction spends SOL after the first
            # balance check. The protected reserve is never spendable by the
            # auto-buyer.
            funded, balance, required = await _buy_is_funded_safely()
            if not funded:
                log.info(
                    "Auto-sniper skipped %s: balance=%.6f required=%.6f",
                    mint,
                    balance,
                    required,
                )
                continue

            before_balance = await wallet.get_token_balance(mint)
            result = await buy_token(mint, settings.auto_sniper_buy_sol, settings.auto_sniper_slippage_bps)
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
                await context.bot.send_message(
                    next(iter(settings.admin_ids)),
                    f"⚠️ Auto-buy confirmed for ${overview.symbol}, but position balance could not be measured. Tx: `{result.signature}`",
                    parse_mode="Markdown",
                )
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
                entry_sol=settings.auto_sniper_buy_sol,
            )
            await store.upsert_position(pos)
            _last_buy_at = time.time()
            await context.bot.send_message(
                next(iter(settings.admin_ids)),
                f"🤖 *AUTO-SNIPER BUY*\n\n"
                f"Token: `${symbol}`\nMint: `{mint}`\n"
                f"Risk: `{analysis.risk_score}/100`\n"
                f"Data: `{overview.data_quality}` • Pools: `{overview.pool_count}`\n"
                f"Liquidity: `${liquidity:,.0f}` ({overview.liquidity_mcap_pct:.1f}% of MC)\n"
                f"5m: `{change_5m:+.1f}%` • Buy/Sell: `{ratio:.2f}` • Trades: `{trades_5m}`\n"
                f"Jupiter impact: `{quote.price_impact_pct:.2f}%`\n"
                f"Amount: `{settings.auto_sniper_buy_sol}` SOL\n"
                f"Protected reserve: `{settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:.3f} SOL`\n"
                f"Tx: `{result.signature}`",
                parse_mode="Markdown",
            )
            return
        except Exception as exc:
            log.warning("Auto-sniper candidate check failed: %s", type(exc).__name__)
