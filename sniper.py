"""sniper.py — opt-in Solana token discovery and guarded auto-buy loop.

Discovery uses DEX Screener's latest Solana token profiles. A candidate must
pass liquidity, market-cap, momentum, token-age, RugCheck, and Jupiter quote
checks before a small automatic buy is attempted.
"""
from __future__ import annotations

import logging
import time

import httpx
from solders.pubkey import Pubkey

from config import settings
from security import get_rug_verdict
from state import Position, store
from trading import SOL_MINT, buy_token, get_quote
from wallet import wallet

log = logging.getLogger("sniper")

_seen: set[str] = set()
_last_buy_at = 0.0


async def _latest_profiles() -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get("https://api.dexscreener.com/token-profiles/latest/v1")
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []


async def _pair(mint: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


async def tick(context) -> None:
    """One discovery pass. Called from the Telegram application's event loop."""
    global _last_buy_at

    st = await store.get_settings()
    if not st.get("auto_sniper_enabled") or not wallet.configured:
        return
    if time.time() - _last_buy_at < settings.auto_sniper_cooldown_seconds:
        return

    try:
        profiles = await _latest_profiles()
    except Exception as exc:
        log.warning("Auto-sniper discovery failed: %s", type(exc).__name__)
        return

    # DEX Screener documents this endpoint at 60 requests/minute, so one
    # discovery request per pass with a conservative 20s default is safe.
    for profile in profiles:
        if profile.get("chainId") != "solana":
            continue
        mint = profile.get("tokenAddress")
        if not mint or mint in _seen:
            continue
        _seen.add(mint)

        try:
            pair = await _pair(mint)
            if not pair:
                continue

            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            market_cap = float(pair.get("marketCap") or pair.get("fdv") or 0)
            volume_5m = float((pair.get("volume") or {}).get("m5") or 0)
            change_5m = float((pair.get("priceChange") or {}).get("m5") or 0)
            created_ms = int(pair.get("pairCreatedAt") or 0)
            age_minutes = (time.time() * 1000 - created_ms) / 60000 if created_ms else 999999

            if liquidity < settings.auto_sniper_min_liquidity_usd:
                continue
            if market_cap < settings.auto_sniper_min_market_cap_usd:
                continue
            if volume_5m < settings.auto_sniper_min_volume_5m_usd:
                continue
            if change_5m <= 0 or change_5m > 35:
                continue
            # Focus on newly active pools, but do not buy an instant launch with
            # no trading history. Five minutes to 24 hours is the default window.
            if age_minutes < 5 or age_minutes > 1440:
                continue

            positions = await store.get_positions()
            if mint in positions:
                continue

            rug = await get_rug_verdict(mint)
            if rug.risk_level != "LOW":
                continue

            # Quote before signing: this confirms Jupiter has an executable route
            # and rejects excessive price impact before any SOL is spent.
            quote = await get_quote(
                SOL_MINT,
                mint,
                int(settings.auto_sniper_buy_sol * 1_000_000_000),
                st["slippage_bps"],
            )
            if quote.price_impact_pct > settings.auto_sniper_max_price_impact_pct:
                continue

            balance = await wallet.get_sol_balance()
            if balance < settings.auto_sniper_buy_sol + 0.002:
                await context.bot.send_message(
                    next(iter(settings.admin_ids)),
                    f"⚠️ Auto-sniper skipped: wallet SOL balance too low for the configured {settings.auto_sniper_buy_sol} SOL buy."
                )
                return

            result = await buy_token(mint, settings.auto_sniper_buy_sol, st["slippage_bps"])
            if not result.success:
                log.warning("Auto-sniper buy failed for %s: %s", mint, result.error)
                continue

            try:
                decimals = (await wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
            except Exception:
                decimals = 9
            token_balance = await wallet.get_token_balance(mint)
            base = pair.get("baseToken") or {}
            symbol = base.get("symbol", "?")
            price = float(pair.get("priceUsd") or 0)
            pos = Position(
                mint=mint,
                symbol=symbol,
                entry_price_usd=price,
                amount_tokens=token_balance,
                decimals=decimals,
                take_profit_pct=st["default_tp_pct"],
                stop_loss_pct=st["default_sl_pct"],
            )
            await store.upsert_position(pos)
            _last_buy_at = time.time()
            await context.bot.send_message(
                next(iter(settings.admin_ids)),
                f"🤖 *AUTO-SNIPER BUY*\n\n"
                f"Token: `${symbol}`\n"
                f"Mint: `{mint}`\n"
                f"Liquidity: `${liquidity:,.0f}`\n"
                f"5m: `{change_5m:+.1f}%`\n"
                f"Jupiter impact: `{quote.price_impact_pct:.2f}%`\n"
                f"Amount: `{settings.auto_sniper_buy_sol}` SOL\n"
                f"Tx: `{result.signature}`",
                parse_mode="Markdown",
            )
            return  # one automatic buy per discovery pass
        except Exception as exc:
            log.warning("Auto-sniper candidate check failed: %s", type(exc).__name__)
