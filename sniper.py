"""Opt-in Solana discovery and guarded auto-buy loop.

Auto-trading is fail-closed: the same deterministic snapshot used for the
analysis view is used for every gate immediately before the buy.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from solders.pubkey import Pubkey

from config import settings
from security import analyze_token
from state import Position, store
from trading import SOL_MINT, buy_token, get_quote, to_raw_units
from wallet import wallet

log = logging.getLogger("sniper")
_last_checked: dict[str, float] = {}
_last_buy_at = 0.0
_RECHECK_SECONDS = 300
_tick_lock = asyncio.Lock()

# In-memory activity feed for the web dashboard (same process, see
# launcher.py). Not persisted -- it only needs to show recent behaviour
# while the process is up, and losing it on a restart is harmless.
_ACTIVITY_LIMIT = 80
_activity_log: list[dict] = []
_last_tick_at: float | None = None
_last_tick_candidates_seen = 0


def _record(mint: str, symbol: str | None, verdict: str, reason: str, **extra):
    entry = {"time": time.time(), "mint": mint, "symbol": symbol, "verdict": verdict, "reason": reason}
    if extra:
        entry.update(extra)
    _activity_log.append(entry)
    if len(_activity_log) > _ACTIVITY_LIMIT:
        del _activity_log[: len(_activity_log) - _ACTIVITY_LIMIT]


def _analysis_snapshot(overview, rug=None, analysis=None) -> dict:
    """The actual analyze_token() output for one candidate, in the same
    shape shown to the dashboard -- so the activity feed shows WHAT was
    analyzed, not just the one-line pass/fail reason."""
    return {
        "price_usd": getattr(overview, "price_usd", None),
        "liquidity_usd": getattr(overview, "liquidity_usd", None),
        "market_cap_usd": getattr(overview, "market_cap", None),
        "fdv_usd": getattr(overview, "fdv", None),
        "volume_5m_usd": getattr(overview, "volume_5m", None),
        "volume_24h_usd": getattr(overview, "volume_24h", None),
        "change_5m_pct": getattr(overview, "change_5m", None),
        "change_1h_pct": getattr(overview, "change_1h", None),
        "age_minutes": getattr(overview, "age_minutes", None),
        "trades_5m": getattr(overview, "total_trades_5m", None),
        "buy_sell_ratio_5m": getattr(overview, "buy_sell_ratio_5m", None),
        "pool_count": getattr(overview, "pool_count", None),
        "dex": getattr(overview, "dex", None),
        "data_quality": getattr(overview, "data_quality", None),
        "risk_score": getattr(analysis, "risk_score", None) if analysis is not None else None,
        "rug_level": getattr(rug, "risk_level", None) if rug is not None else None,
        "top_holder_pct": getattr(rug, "top_holder_pct", None) if rug is not None else None,
    }


def get_status() -> dict:
    """Read-only snapshot for the dashboard: is the sniper loop alive, and
    what has it looked at / decided most recently."""
    return {
        "last_tick_at": _last_tick_at,
        "last_tick_candidates_seen": _last_tick_candidates_seen,
        "activity": list(reversed(_activity_log)),
    }


async def _latest_profiles() -> list[dict]:
    """Secondary discovery source: tokens whose creator submitted a
    DexScreener profile. Kept as a fallback, but most brand-new pump.fun
    mints never have a profile in their first hour, so this alone almost
    always returns an empty/near-empty list -- see _pumpfun_newest_coins."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS) as http:
            r = await http.get("https://api.dexscreener.com/token-profiles/latest/v1")
            if r.status_code != 200:
                log.warning(
                    "DexScreener profiles feed returned HTTP %s: %s",
                    r.status_code, r.text[:200].replace("\n", " "),
                )
                return []
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("DexScreener profiles feed unavailable: %s: %s", type(exc).__name__, exc)
        return []


_BROWSER_HEADERS = {
    # pump.fun's frontend API sits behind Cloudflare and commonly 403s
    # plain server-side requests with no browser-like headers -- this is
    # very likely why the feed was silently empty even after switching
    # sources.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://pump.fun/",
    "Origin": "https://pump.fun",
}


async def _pumpfun_newest_coins() -> list[dict]:
    """Primary discovery source: pump.fun's own newest-launch feed.

    Unlike DexScreener's token-profiles feed, this has no requirement that
    the creator filled in a profile/socials -- it lists every new mint,
    which is exactly the population a sniper needs to see. This hits an
    unofficial/undocumented pump.fun endpoint, so it's wrapped defensively:
    any failure or shape change just yields an empty list and the tick
    falls back to whatever DexScreener returned. Failures are logged at
    WARNING (not DEBUG) so they actually surface in the dashboard's Live
    Logs panel, which only mirrors INFO and above.
    """
    url = "https://frontend-api-v3.pump.fun/coins"
    params = {
        "offset": 0,
        "limit": 60,
        "sort": "created_timestamp",
        "order": "DESC",
        "includeNsfw": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS) as http:
            r = await http.get(url, params=params)
            if r.status_code != 200:
                log.warning(
                    "pump.fun newest-coins feed returned HTTP %s: %s",
                    r.status_code, r.text[:200].replace("\n", " "),
                )
                return []
            data = r.json()
    except Exception as exc:
        log.warning("pump.fun newest-coins feed unavailable: %s: %s", type(exc).__name__, exc)
        return []

    if isinstance(data, list):
        coins = data
    elif isinstance(data, dict):
        coins = data.get("coins") or data.get("data") or []
    else:
        coins = []

    out: list[dict] = []
    for c in coins:
        if not isinstance(c, dict):
            continue
        mint = c.get("mint") or c.get("tokenAddress") or c.get("address")
        if mint:
            out.append({"chainId": "solana", "tokenAddress": mint})
    return out


async def _discover_candidates() -> list[dict]:
    """Merge every discovery source and de-duplicate by mint. Each source
    fails independently -- one going down (or pump.fun changing its API)
    never blocks the other from surfacing candidates."""
    pumpfun_batch, dexscreener_batch = await asyncio.gather(
        _pumpfun_newest_coins(), _latest_profiles(), return_exceptions=True
    )
    seen: set[str] = set()
    merged: list[dict] = []
    for source_name, batch in (("pump.fun", pumpfun_batch), ("dexscreener", dexscreener_batch)):
        if isinstance(batch, Exception):
            log.warning("Auto-sniper discovery source (%s) failed: %s: %s", source_name, type(batch).__name__, batch)
            continue
        for profile in batch:
            mint = profile.get("tokenAddress")
            if not mint or mint in seen:
                continue
            seen.add(mint)
            merged.append(profile)
    log.info(
        "Auto-sniper discovery: pump.fun=%d dexscreener=%d merged_unique=%d",
        len(pumpfun_batch) if isinstance(pumpfun_batch, list) else -1,
        len(dexscreener_batch) if isinstance(dexscreener_batch, list) else -1,
        len(merged),
    )
    return merged


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
    global _last_buy_at, _last_tick_at, _last_tick_candidates_seen

    if _tick_lock.locked():
        log.debug("Auto-sniper tick skipped: previous tick still active")
        return

    async with _tick_lock:
        _last_tick_at = time.time()
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

        profiles = await _discover_candidates()

        now = time.time()
        _last_tick_candidates_seen = len(profiles)
        for profile in profiles:
            if profile.get("chainId") != "solana":
                continue
            mint = profile.get("tokenAddress")
            if not mint or now - _last_checked.get(mint, 0) < _RECHECK_SECONDS:
                continue
            _last_checked[mint] = now

            try:
                overview, rug, analysis = await analyze_token(mint)
                symbol = overview.symbol or None
                if not overview.found or overview.price_usd <= 0:
                    _record(mint, symbol, "SKIPPED", "No market data / price found", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if overview.data_quality != "HIGH":
                    log.info("Auto-sniper blocked %s: data quality=%s warnings=%s", mint, overview.data_quality, overview.data_warnings)
                    _record(mint, symbol, "SKIPPED", f"Data quality {overview.data_quality} (warnings: {', '.join(overview.data_warnings) or 'none'})", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if overview.market_cap <= 0 or overview.total_supply <= 0 or overview.fdv <= 0:
                    _record(mint, symbol, "SKIPPED", "Missing market cap / supply / FDV data", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if not rug.mint_authority_revoked or not rug.freeze_authority_revoked:
                    _record(mint, symbol, "SKIPPED", "Mint or freeze authority not revoked (rug risk)", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if rug.top_holder_pct is None or rug.top_holder_pct > 10:
                    _record(mint, symbol, "SKIPPED", f"Top holder concentration {rug.top_holder_pct if rug.top_holder_pct is not None else '?'}% (max 10%)", **_analysis_snapshot(overview, rug, analysis))
                    continue

                liquidity = overview.liquidity_usd
                market_cap = overview.market_cap
                volume_5m = overview.volume_5m
                change_5m = overview.change_5m
                age = overview.age_minutes
                ratio = overview.buy_sell_ratio_5m
                trades_5m = overview.total_trades_5m

                if liquidity < settings.auto_sniper_min_liquidity_usd:
                    _record(mint, symbol, "SKIPPED", f"Liquidity ${liquidity:,.0f} < min ${settings.auto_sniper_min_liquidity_usd:,.0f}", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if market_cap < settings.auto_sniper_min_market_cap_usd:
                    _record(mint, symbol, "SKIPPED", f"Market cap ${market_cap:,.0f} < min ${settings.auto_sniper_min_market_cap_usd:,.0f}", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if volume_5m < settings.auto_sniper_min_volume_5m_usd:
                    _record(mint, symbol, "SKIPPED", f"5m volume ${volume_5m:,.0f} < min ${settings.auto_sniper_min_volume_5m_usd:,.0f}", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if overview.liquidity_mcap_pct < settings.auto_sniper_min_liquidity_mcap_pct:
                    _record(mint, symbol, "SKIPPED", f"Liquidity/MC {overview.liquidity_mcap_pct:.1f}% < min {settings.auto_sniper_min_liquidity_mcap_pct:.1f}%", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if change_5m <= 0 or change_5m > settings.auto_sniper_max_5m_change_pct:
                    _record(mint, symbol, "SKIPPED", f"5m momentum {change_5m:+.1f}% outside allowed 0–{settings.auto_sniper_max_5m_change_pct:.0f}%", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if age is None or age < settings.auto_sniper_min_age_minutes or age > settings.auto_sniper_max_age_minutes:
                    _record(mint, symbol, "SKIPPED", f"Pool age {age if age is not None else '?'}m outside {settings.auto_sniper_min_age_minutes:g}–{settings.auto_sniper_max_age_minutes:g}m window", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if trades_5m < 5:
                    _record(mint, symbol, "SKIPPED", f"Only {trades_5m} trades in the last 5m (min 5)", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if overview.sells_5m > 0 and ratio < settings.auto_sniper_min_buy_sell_ratio:
                    _record(mint, symbol, "SKIPPED", f"Buy/Sell ratio {ratio:.2f} < min {settings.auto_sniper_min_buy_sell_ratio:.2f}", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if analysis.risk_score > settings.auto_sniper_max_risk_score or rug.risk_level != "LOW":
                    _record(mint, symbol, "SKIPPED", f"Risk score {analysis.risk_score}/100 (max {settings.auto_sniper_max_risk_score}), RugCheck {rug.risk_level}", **_analysis_snapshot(overview, rug, analysis))
                    continue

                current_positions = await store.get_positions()
                if mint in current_positions or len(current_positions) >= settings.auto_sniper_max_positions:
                    _record(mint, symbol, "SKIPPED", "Already holding this mint, or max concurrent positions reached", **_analysis_snapshot(overview, rug, analysis))
                    continue

                # Recalculate immediately before quoting.
                available_sol, balance = await _available_trade_sol()
                trade_amount = min(available_sol * allocation_pct / 100.0, settings.max_buy_sol)
                remaining_exposure = settings.auto_sniper_max_exposure_sol - await _auto_exposure_sol(current_positions)
                trade_amount = min(trade_amount, remaining_exposure)
                if trade_amount <= 0 or balance - trade_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                    _record(mint, symbol, "SKIPPED", "Insufficient spendable SOL after fee reserve / exposure cap", **_analysis_snapshot(overview, rug, analysis))
                    continue

                # Safe exact lamports conversion
                quote_lamports = to_raw_units(trade_amount, 9)
                quote = await get_quote(SOL_MINT, mint, quote_lamports, settings.auto_sniper_slippage_bps)
                if quote.price_impact_pct > settings.auto_sniper_max_price_impact_pct:
                    _record(mint, symbol, "SKIPPED", f"Jupiter price impact {quote.price_impact_pct:.1f}% > max {settings.auto_sniper_max_price_impact_pct:.1f}%", **_analysis_snapshot(overview, rug, analysis))
                    continue

                # One last balance check
                available_sol_now, balance_now = await _available_trade_sol()
                final_amount = min(available_sol_now * allocation_pct / 100.0, settings.max_buy_sol, remaining_exposure)
                if final_amount <= 0 or balance_now - final_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                    _record(mint, symbol, "SKIPPED", "Balance changed before final check -- amount no longer affordable", **_analysis_snapshot(overview, rug, analysis))
                    continue
                if abs(final_amount - trade_amount) > 1e-9:
                    _record(mint, symbol, "SKIPPED", "Balance shifted between checks (safety abort, will retry)", **_analysis_snapshot(overview, rug, analysis))
                    continue

                before_balance = await wallet.get_token_balance(mint)
                result = await buy_token(mint, final_amount, settings.auto_sniper_slippage_bps)
                if not result.success:
                    log.warning("Auto-sniper buy failed for %s: %s", mint, result.error)
                    _record(mint, symbol, "BUY_FAILED", result.error or "Unknown execution error", **_analysis_snapshot(overview, rug, analysis))
                    continue

                try:
                    decimals = (await wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
                except Exception:
                    decimals = overview.decimals or 9
                after_balance = await wallet.get_token_balance(mint)
                token_delta = max(0.0, after_balance - before_balance)
                if token_delta <= 0:
                    log.error("Auto-buy confirmed but token balance delta is zero for %s", mint)
                    _record(mint, symbol, "ERROR", "Buy confirmed but resulting balance could not be measured", **_analysis_snapshot(overview, rug, analysis))
                    await context.bot.send_message(next(iter(settings.admin_ids)), f"⚠️ Auto-buy confirmed for ${overview.symbol}, but position balance could not be measured. Tx: `{result.signature}`", parse_mode="Markdown")
                    return

                symbol = overview.symbol or "?"
                pos = Position(
                    mint=mint,
                    symbol=symbol,
                    entry_price_usd=overview.price_usd,
                    amount_tokens=token_delta,
                    decimals=decimals,
                    take_profit_pct=st.get("default_tp_pct"),
                    stop_loss_pct=st.get("default_sl_pct", 30.0),
                    entry_sol=final_amount,
                    opened_at=time.time(),
                    buy_signature=result.signature,
                    entry_snapshot=_entry_snapshot(overview, rug, analysis, allocation_pct, final_amount, quote.price_impact_pct),
                )
                await store.upsert_position(pos)
                _last_buy_at = time.time()
                _record(mint, symbol, "BOUGHT", f"Spent {final_amount:.4f} SOL @ ${overview.price_usd:.10f}", **_analysis_snapshot(overview, rug, analysis))
                await context.bot.send_message(
                    next(iter(settings.admin_ids)),
                    f"🤖 *AUTO-SNIPER BUY — POSITION OPEN*\n\n"
                    f"Token: `${symbol}`\nName: `{overview.name}`\nMint / CA: `{mint}`\n\n"
                    f"Entry: `${overview.price_usd:.10f}`\nTokens: `{token_delta:.8g}`\nSpend: `{final_amount:.9f} SOL`\n"
                    f"Allocation: `{allocation_pct:g}%` of spendable SOL\nTP: `Managed by Smart-SL` • Hard SL: `-{st.get('default_sl_pct', 30.0)}%`\n\n"
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
                _record(mint, None, "ERROR", f"{type(exc).__name__}: {exc}")
