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


def _fnum(v, fmt: str = ".1f") -> str:
    """Format a possibly-None numeric value for a check detail string."""
    return "?" if v is None else format(v, fmt)


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


async def _pumpfun_about_to_graduate() -> list[dict]:
    """Secondary pump.fun source: tokens nearing/at graduation to Raydium.

    The newest-launch feed above catches mints seconds old -- almost all of
    them still on the bonding curve with no real pool yet, so they get
    SKIPPED for "Data quality LOW". pump.fun's own "king of the hill" feed
    lists the coins with the most real trading activity on their bonding
    curve, which is exactly the population closest to (or already past)
    graduating to an actual Raydium pool with real liquidity. Same
    unofficial-endpoint caveats as _pumpfun_newest_coins: any failure just
    yields an empty list.
    """
    url = "https://frontend-api-v3.pump.fun/coins/king-of-the-hill"
    params = {"offset": 0, "limit": 50, "includeNsfw": "false"}
    try:
        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS) as http:
            r = await http.get(url, params=params)
            if r.status_code != 200:
                log.warning(
                    "pump.fun king-of-the-hill feed returned HTTP %s: %s",
                    r.status_code, r.text[:200].replace("\n", " "),
                )
                return []
            data = r.json()
    except Exception as exc:
        log.warning("pump.fun king-of-the-hill feed unavailable: %s: %s", type(exc).__name__, exc)
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


async def _geckoterminal_trending() -> list[dict]:
    """Fifth discovery source, and a genuinely different population from
    the pump.fun feeds above: tokens that already have a real, established
    pool and are *currently trending* on GeckoTerminal -- i.e. the "other
    side" of the strategy the user asked for. pump.fun's feeds are biased
    toward brand-new mints; this one surfaces tokens that have already
    been trading for a while and are seeing rising interest right now,
    which is what the new 1h-trend gate below (auto_sniper_min_1h_change_pct)
    is meant to confirm before buying.
    """
    url = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
    try:
        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS) as http:
            r = await http.get(url, params={"include": "base_token"})
            if r.status_code != 200:
                log.warning(
                    "GeckoTerminal trending-pools feed returned HTTP %s: %s",
                    r.status_code, r.text[:200].replace("\n", " "),
                )
                return []
            data = r.json()
    except Exception as exc:
        log.warning("GeckoTerminal trending-pools feed unavailable: %s: %s", type(exc).__name__, exc)
        return []

    if not isinstance(data, dict):
        return []

    included = {
        item.get("id"): item
        for item in (data.get("included") or [])
        if isinstance(item, dict)
    }

    out: list[dict] = []
    for pool in data.get("data") or []:
        if not isinstance(pool, dict):
            continue
        try:
            token_ref = pool["relationships"]["base_token"]["data"]
            token_obj = included.get(token_ref.get("id"))
            address = (token_obj or {}).get("attributes", {}).get("address")
        except (KeyError, TypeError, AttributeError):
            address = None
        if address:
            out.append({"chainId": "solana", "tokenAddress": address})
    return out


async def _dexscreener_boosted() -> list[dict]:
    """Tertiary source: DexScreener's boosted-token feed. Projects pay to
    boost, which in practice means they already have a real pool and are
    pushing for visibility -- a different population again from brand-new
    or about-to-graduate pump.fun mints, and a cheap way to widen coverage
    beyond pump.fun specifically."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=_BROWSER_HEADERS) as http:
            r = await http.get("https://api.dexscreener.com/token-boosts/latest/v1")
            if r.status_code != 200:
                log.warning(
                    "DexScreener boosts feed returned HTTP %s: %s",
                    r.status_code, r.text[:200].replace("\n", " "),
                )
                return []
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("DexScreener boosts feed unavailable: %s: %s", type(exc).__name__, exc)
        return []


async def _discover_candidates() -> list[dict]:
    """Merge every discovery source and de-duplicate by mint. Each source
    fails independently -- one going down (or an API changing shape) never
    blocks the others from surfacing candidates.

    Five sources, deliberately covering different populations:
      - pump.fun newest: seconds-old mints (mostly pre-liquidity, filtered
        out downstream by the data-quality gate -- but this is the only
        source that ever sees a token in its first minutes at all)
      - pump.fun king-of-the-hill: bonding-curve coins with heavy trading,
        closest to (or already past) graduating to a real Raydium pool
      - DexScreener profiles: tokens with a creator-submitted profile
      - DexScreener boosts: tokens whose project paid to boost visibility
      - GeckoTerminal trending: already-established pools currently seeing
        rising interest -- the "hold what's trending up" side of the
        strategy, gated below by the 1h-trend check rather than treated
        like a brand-new mint
    """
    pumpfun_new, pumpfun_koth, dex_profiles, dex_boosts, gt_trending = await asyncio.gather(
        _pumpfun_newest_coins(), _pumpfun_about_to_graduate(),
        _latest_profiles(), _dexscreener_boosted(), _geckoterminal_trending(),
        return_exceptions=True,
    )
    seen: set[str] = set()
    merged: list[dict] = []
    sources = (
        ("pump.fun-newest", pumpfun_new),
        ("pump.fun-koth", pumpfun_koth),
        ("dexscreener-profiles", dex_profiles),
        ("dexscreener-boosts", dex_boosts),
        ("geckoterminal-trending", gt_trending),
    )
    for source_name, batch in sources:
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
        "Auto-sniper discovery: pump.fun-newest=%d pump.fun-koth=%d dexscreener-profiles=%d dexscreener-boosts=%d geckoterminal-trending=%d merged_unique=%d",
        *(len(b) if isinstance(b, list) else -1 for _, b in sources),
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
        # Cap to whatever exposure room is left, rather than aborting the
        # whole tick outright. AUTO_SNIPER_MAX_EXPOSURE_SOL is a small hard
        # safety ceiling (e.g. 0.03 SOL) while trade_amount here comes from
        # allocation_pct of the wallet balance -- for any real balance,
        # allocation-based sizing will almost always be *larger* than that
        # ceiling. Previously this returned outright whenever that happened,
        # which meant a normal allocation % (e.g. 65%) silently blocked
        # every single tick before discovery ever ran, no matter how many
        # good candidates existed.
        remaining_exposure = settings.auto_sniper_max_exposure_sol - await _auto_exposure_sol(positions)
        if remaining_exposure <= 0:
            return
        trade_amount = min(trade_amount, remaining_exposure)
        if balance - trade_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
            return

        profiles = await _discover_candidates()

        now = time.time()
        _last_tick_candidates_seen = len(profiles)
        analyzed_count = 0
        for profile in profiles:
            if profile.get("chainId") != "solana":
                continue
            mint = profile.get("tokenAddress")
            if not mint or now - _last_checked.get(mint, 0) < _RECHECK_SECONDS:
                continue
            if analyzed_count >= settings.auto_sniper_max_analyses_per_tick:
                # Discovery now regularly returns 100+ candidates a pass
                # across four sources. Analyzing every one of them in a
                # single tick means 3-5 HTTP calls each (DexScreener,
                # Jupiter, RugCheck, Solana RPC) fired back-to-back --
                # hundreds of requests in a few seconds, which is exactly
                # what was tripping "429 Too Many Requests" on every
                # provider and making ticks run so long the next
                # scheduled tick got skipped ("maximum number of running
                # instances reached"). Capping per tick and leaving the
                # rest for the next pass (the _last_checked cache below
                # remembers what's already queued) keeps request volume
                # sane without permanently skipping anything.
                break
            _last_checked[mint] = now
            analyzed_count += 1
            if analyzed_count > 1:
                # Space requests out instead of firing analyze_token() for
                # every candidate back-to-back -- this is what actually
                # avoids tripping DexScreener/Jupiter/RPC rate limits,
                # regardless of which check below a given candidate exits
                # on.
                await asyncio.sleep(settings.auto_sniper_analysis_delay_seconds)

            try:
                overview, rug, analysis = await analyze_token(mint)
                symbol = overview.symbol or None

                # Evaluate every gate up front instead of stopping at the
                # first failure, so the dashboard can show which checks
                # passed (green) and which failed (red) for every single
                # candidate -- not just the one reason it happened to trip
                # on first.
                checks: list[dict] = []

                def add_check(label: str, passed: bool, detail: str):
                    checks.append({"label": label, "passed": bool(passed), "detail": detail})

                found_ok = bool(overview.found) and overview.price_usd > 0
                add_check("Market data found", found_ok, "found" if found_ok else "No market data / price found")

                data_quality_ok = overview.data_quality == "HIGH"
                add_check(
                    "Data quality", data_quality_ok,
                    f"{overview.data_quality}" + (f" (warnings: {', '.join(overview.data_warnings)})" if overview.data_warnings else ""),
                )

                mcap_data_ok = (overview.market_cap or 0) > 0 and (overview.total_supply or 0) > 0 and (overview.fdv or 0) > 0
                add_check("Market cap / supply / FDV present", mcap_data_ok, "present" if mcap_data_ok else "missing")

                authority_ok = bool(rug.mint_authority_revoked) and bool(rug.freeze_authority_revoked)
                add_check("Mint/Freeze authority revoked", authority_ok, "revoked" if authority_ok else "NOT revoked (rug risk)")

                holder_ok = rug.top_holder_pct is not None and rug.top_holder_pct <= 10
                add_check("Top holder concentration", holder_ok, f"{_fnum(rug.top_holder_pct)}% (max 10%)")

                liquidity = overview.liquidity_usd or 0
                liquidity_ok = liquidity >= settings.auto_sniper_min_liquidity_usd
                add_check("Liquidity", liquidity_ok, f"${liquidity:,.0f} (min ${settings.auto_sniper_min_liquidity_usd:,.0f})")

                market_cap = overview.market_cap or 0
                mc_ok = market_cap >= settings.auto_sniper_min_market_cap_usd
                add_check("Market cap", mc_ok, f"${market_cap:,.0f} (min ${settings.auto_sniper_min_market_cap_usd:,.0f})")

                volume_5m = overview.volume_5m or 0
                vol_ok = volume_5m >= settings.auto_sniper_min_volume_5m_usd
                add_check("5m volume", vol_ok, f"${volume_5m:,.0f} (min ${settings.auto_sniper_min_volume_5m_usd:,.0f})")

                liq_mcap_pct = overview.liquidity_mcap_pct or 0
                liq_mcap_ok = liq_mcap_pct >= settings.auto_sniper_min_liquidity_mcap_pct
                add_check("Liquidity/MC ratio", liq_mcap_ok, f"{liq_mcap_pct:.1f}% (min {settings.auto_sniper_min_liquidity_mcap_pct:.1f}%)")

                change_5m = overview.change_5m
                change_5m_ok = change_5m is not None and 0 < change_5m <= settings.auto_sniper_max_5m_change_pct
                add_check("5m momentum", change_5m_ok, f"{_fnum(change_5m, '+.1f')}% (allowed 0–{settings.auto_sniper_max_5m_change_pct:.0f}%)")

                age = overview.age_minutes
                age_ok = age is not None and settings.auto_sniper_min_age_minutes <= age <= settings.auto_sniper_max_age_minutes
                add_check("Pool age", age_ok, f"{_fnum(age, '.0f')}m (window {settings.auto_sniper_min_age_minutes:g}–{settings.auto_sniper_max_age_minutes:g}m)")

                # "Other side" of the strategy: only applies once a token is
                # old enough to have a real 1h history -- not applicable
                # (so not added to the list at all) for a fresh mint still
                # being judged on 5m momentum alone.
                if age is not None and age >= settings.auto_sniper_trend_check_age_minutes:
                    change_1h = overview.change_1h
                    trend_ok = change_1h is not None and change_1h >= settings.auto_sniper_min_1h_change_pct
                    add_check("1h trend (established token)", trend_ok, f"{_fnum(change_1h, '+.1f')}% (min +{settings.auto_sniper_min_1h_change_pct:.0f}%)")

                trades_5m = overview.total_trades_5m or 0
                trades_ok = trades_5m >= 5
                add_check("5m trade count", trades_ok, f"{trades_5m} (min 5)")

                ratio = overview.buy_sell_ratio_5m
                ratio_ok = not overview.sells_5m or (ratio is not None and ratio >= settings.auto_sniper_min_buy_sell_ratio)
                add_check("Buy/Sell ratio", ratio_ok, f"{_fnum(ratio, '.2f')} (min {settings.auto_sniper_min_buy_sell_ratio:.2f})")

                risk_ok = analysis.risk_score <= settings.auto_sniper_max_risk_score and rug.risk_level == "LOW"
                add_check("Risk score / RugCheck", risk_ok, f"{analysis.risk_score}/100 (max {settings.auto_sniper_max_risk_score}), RugCheck {rug.risk_level}")

                all_passed = all(c["passed"] for c in checks)
                if not all_passed:
                    first_fail = next(c for c in checks if not c["passed"])
                    _record(
                        mint, symbol, "SKIPPED",
                        f"{first_fail['label']}: {first_fail['detail']}",
                        checks=checks,
                        **_analysis_snapshot(overview, rug, analysis),
                    )
                    continue

                current_positions = await store.get_positions()
                if mint in current_positions or len(current_positions) >= settings.auto_sniper_max_positions:
                    _record(mint, symbol, "SKIPPED", "Already holding this mint, or max concurrent positions reached", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue

                # Recalculate immediately before quoting.
                available_sol, balance = await _available_trade_sol()
                trade_amount = min(available_sol * allocation_pct / 100.0, settings.max_buy_sol)
                remaining_exposure = settings.auto_sniper_max_exposure_sol - await _auto_exposure_sol(current_positions)
                trade_amount = min(trade_amount, remaining_exposure)
                if trade_amount <= 0 or balance - trade_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                    _record(mint, symbol, "SKIPPED", "Insufficient spendable SOL after fee reserve / exposure cap", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue

                # Safe exact lamports conversion
                quote_lamports = to_raw_units(trade_amount, 9)
                quote = await get_quote(SOL_MINT, mint, quote_lamports, settings.auto_sniper_slippage_bps)
                if quote.price_impact_pct > settings.auto_sniper_max_price_impact_pct:
                    _record(mint, symbol, "SKIPPED", f"Jupiter price impact {quote.price_impact_pct:.1f}% > max {settings.auto_sniper_max_price_impact_pct:.1f}%", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue

                # One last balance check
                available_sol_now, balance_now = await _available_trade_sol()
                final_amount = min(available_sol_now * allocation_pct / 100.0, settings.max_buy_sol, remaining_exposure)
                if final_amount <= 0 or balance_now - final_amount < settings.auto_fee_reserve_sol + settings.auto_safety_buffer_sol:
                    _record(mint, symbol, "SKIPPED", "Balance changed before final check -- amount no longer affordable", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue
                if abs(final_amount - trade_amount) > 1e-9:
                    _record(mint, symbol, "SKIPPED", "Balance shifted between checks (safety abort, will retry)", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue

                before_balance = await wallet.get_token_balance(mint)
                result = await buy_token(mint, final_amount, settings.auto_sniper_slippage_bps)
                if not result.success:
                    log.warning("Auto-sniper buy failed for %s: %s", mint, result.error)
                    _record(mint, symbol, "BUY_FAILED", result.error or "Unknown execution error", checks=checks, **_analysis_snapshot(overview, rug, analysis))
                    continue

                try:
                    decimals = (await wallet.client.get_token_supply(Pubkey.from_string(mint))).value.decimals
                except Exception:
                    decimals = overview.decimals or 9
                after_balance = await wallet.get_token_balance(mint)
                token_delta = max(0.0, after_balance - before_balance)
                if token_delta <= 0:
                    log.error("Auto-buy confirmed but token balance delta is zero for %s", mint)
                    _record(mint, symbol, "ERROR", "Buy confirmed but resulting balance could not be measured", checks=checks, **_analysis_snapshot(overview, rug, analysis))
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
                _record(mint, symbol, "BOUGHT", f"Spent {final_amount:.4f} SOL @ ${overview.price_usd:.10f}", checks=checks, **_analysis_snapshot(overview, rug, analysis))
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
