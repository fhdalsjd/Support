"""Market data + deterministic security/risk analysis.

The UI and auto-trader deliberately share this module so they do not use
separate calculations. Market data comes from the live DexScreener pool feed;
Solana RPC supplies on-chain token supply/authorities/top-account concentration;
RugCheck supplies an independent token-security report.

Important: different analytics sites can legitimately disagree on market cap or
liquidity because they use different pools and circulating-supply definitions.
This module therefore reports the source and coverage explicitly and fails
closed for automatic trading when critical data is incomplete.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from config import settings


@dataclass
class TokenOverview:
    mint: str
    name: str
    symbol: str
    price_usd: float
    market_cap: float
    liquidity_usd: float
    change_5m: float
    change_1h: float
    dex: str
    found: bool = True
    fdv: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_6h: float = 0.0
    volume_24h: float = 0.0
    change_6h: float = 0.0
    change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    pair_created_at_ms: int = 0
    pair_address: str = ""
    pool_count: int = 0
    oldest_pair_created_at_ms: int = 0
    total_supply: float = 0.0
    decimals: int = 0
    mint_authority: str | None = None
    freeze_authority: str | None = None
    top_holder_pct: float | None = None
    data_source: str = "DexScreener + Solana RPC"
    market_cap_source: str = "DexScreener pair-reported"
    fetched_at_ms: int = 0
    data_quality: str = "LOW"
    data_warnings: list[str] = field(default_factory=list)

    @property
    def age_minutes(self) -> float | None:
        if not self.pair_created_at_ms:
            return None
        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60000.0)

    @property
    def oldest_pool_age_minutes(self) -> float | None:
        if not self.oldest_pair_created_at_ms:
            return None
        return max(0.0, (time.time() * 1000 - self.oldest_pair_created_at_ms) / 60000.0)

    @property
    def liquidity_mcap_pct(self) -> float:
        return self.liquidity_usd / self.market_cap * 100.0 if self.market_cap > 0 else 0.0

    @property
    def buy_sell_ratio_5m(self) -> float:
        if self.sells_5m <= 0:
            return float(self.buys_5m) if self.buys_5m else 0.0
        return self.buys_5m / self.sells_5m

    @property
    def total_trades_5m(self) -> int:
        return self.buys_5m + self.sells_5m

    @property
    def total_trades_1h(self) -> int:
        return self.buys_1h + self.sells_1h

    @property
    def fdv_onchain(self) -> float:
        if self.total_supply > 0 and self.price_usd > 0:
            return self.total_supply * self.price_usd
        return 0.0


def _f(value, default=0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return float(default)


def _i(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return int(default)


def _sum_nested(pairs: list[dict], parent: str, child: str) -> float:
    return sum(_f((p.get(parent) or {}).get(child)) for p in pairs)


def _sum_txns(pairs: list[dict], window: str, side: str) -> int:
    return sum(_i(((p.get("txns") or {}).get(window) or {}).get(side)) for p in pairs)


def _weighted_change(pairs: list[dict], window: str, volume_key: str) -> float:
    total_volume = sum(_f((p.get("volume") or {}).get(volume_key)) for p in pairs)
    if total_volume <= 0:
        best = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
        return _f(((best.get("priceChange") or {}).get(window)))
    return sum(
        _f(((p.get("priceChange") or {}).get(window)))
        * _f((p.get("volume") or {}).get(volume_key))
        for p in pairs
    ) / total_volume


async def _rpc_call(http: httpx.AsyncClient, method: str, params: list) -> dict | None:
    try:
        r = await http.post(
            settings.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            return None
        return payload.get("result")
    except Exception:
        return None


async def _fetch_chain_snapshot(mint: str) -> dict:
    """Fetch authoritative on-chain supply, authorities and largest accounts."""
    async with httpx.AsyncClient(timeout=10) as http:
        supply_result, account_result, largest_result = await asyncio.gather(
            _rpc_call(http, "getTokenSupply", [mint]),
            _rpc_call(http, "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]),
            _rpc_call(http, "getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]),
        )

    supply_value = (supply_result or {}).get("value") or {}
    total_supply = _f(supply_value.get("uiAmountString"))
    if total_supply <= 0:
        amount = _f(supply_value.get("amount"))
        decimals = _i(supply_value.get("decimals"))
        total_supply = amount / (10 ** decimals) if amount > 0 else 0.0
    decimals = _i(supply_value.get("decimals"))

    mint_authority = None
    freeze_authority = None
    account_value = (account_result or {}).get("value")
    try:
        info = account_value["data"]["parsed"]["info"]
        mint_authority = info.get("mintAuthority")
        freeze_authority = info.get("freezeAuthority")
        if not decimals:
            decimals = _i(info.get("decimals"))
        if total_supply <= 0:
            raw = _f(info.get("supply"))
            total_supply = raw / (10 ** decimals) if raw > 0 else 0.0
    except (KeyError, TypeError):
        pass

    top_holder_pct = None
    accounts = (largest_result or {}).get("value") or []
    if accounts and total_supply > 0:
        raw_total = total_supply * (10 ** decimals)
        largest_raw = max(_f(a.get("amount")) for a in accounts)
        if raw_total > 0:
            top_holder_pct = largest_raw / raw_total * 100.0

    return {
        "available": bool(supply_result and account_result),
        "total_supply": total_supply,
        "decimals": decimals,
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "top_holder_pct": top_holder_pct,
    }


async def _fetch_sol_pairs(mint: str) -> list[dict]:
    url = f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(url)
        r.raise_for_status()
        data = r.json()
    unique: dict[str, dict] = {}
    for p in data or []:
        if p.get("chainId") != "solana":
            continue
        addr = str(p.get("pairAddress") or "")
        if addr:
            unique[addr] = p
    return list(unique.values())


async def get_mint_decimals(mint: str) -> int | None:
    """Dedicated on-chain decimals lookup, for when a full get_token_overview()
    snapshot isn't available/cached yet but we still need a trustworthy decimals
    value before recording a position (getting this wrong causes sells to
    compute the wrong raw token amount -- see do_buy in trading_bot.py)."""
    async with httpx.AsyncClient(timeout=10) as http:
        result = await _rpc_call(http, "getTokenSupply", [mint])
    value = (result or {}).get("value") or {}
    decimals = _i(value.get("decimals"))
    return decimals if decimals > 0 else None


_OVERVIEW_CACHE_TTL_SECONDS = 5.0
_OVERVIEW_CACHE: dict[str, tuple[float, TokenOverview]] = {}


async def get_token_overview(mint: str) -> TokenOverview:
    now = time.time()
    cached = _OVERVIEW_CACHE.get(mint)
    if cached and now - cached[0] <= _OVERVIEW_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        pairs, chain = await asyncio.gather(_fetch_sol_pairs(mint), _fetch_chain_snapshot(mint))
        if not pairs:
            return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)

        liquid_pairs = [p for p in pairs if _f((p.get("liquidity") or {}).get("usd")) > 0] or pairs
        best = max(liquid_pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
        base = best.get("baseToken") or {}
        market_cap = _f(best.get("marketCap"))
        dex_fdv = _f(best.get("fdv"))
        created = [_i(p.get("pairCreatedAt")) for p in pairs if _i(p.get("pairCreatedAt")) > 0]
        oldest_created = min(created) if created else 0

        warnings: list[str] = []
        if len(pairs) >= 30:
            warnings.append("Provider returned 30 pools; total Solana market coverage may be incomplete")
        if not chain.get("available"):
            warnings.append("On-chain supply/authority lookup unavailable")
        if market_cap <= 0:
            warnings.append("Provider-reported market cap unavailable")

        onchain_fdv = _f(chain.get("total_supply")) * _f(best.get("priceUsd"))
        fdv = onchain_fdv if onchain_fdv > 0 else dex_fdv
        valuation_conflict = False
        if onchain_fdv > 0 and market_cap > onchain_fdv * 1.05:
            valuation_conflict = True
            warnings.append("Market cap exceeds on-chain FDV; circulating-supply data is inconsistent")
        if onchain_fdv > 0 and dex_fdv > 0 and abs(onchain_fdv - dex_fdv) / dex_fdv > 0.20:
            valuation_conflict = True
            warnings.append("On-chain FDV differs >20% from provider FDV")

        quality = (
            "HIGH"
            if chain.get("available") and len(pairs) < 30 and not valuation_conflict
            else "MEDIUM"
            if chain.get("available") and pairs
            else "LOW"
        )

        overview = TokenOverview(
            mint=mint,
            name=base.get("name", "Unknown"),
            symbol=base.get("symbol", "?"),
            price_usd=_f(best.get("priceUsd")),
            market_cap=market_cap,
            liquidity_usd=sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs),
            change_5m=_weighted_change(pairs, "m5", "m5"),
            change_1h=_weighted_change(pairs, "h1", "h1"),
            dex=best.get("dexId", "-"),
            fdv=fdv,
            volume_5m=_sum_nested(pairs, "volume", "m5"),
            volume_1h=_sum_nested(pairs, "volume", "h1"),
            volume_6h=_sum_nested(pairs, "volume", "h6"),
            volume_24h=_sum_nested(pairs, "volume", "h24"),
            change_6h=_weighted_change(pairs, "h6", "h6"),
            change_24h=_weighted_change(pairs, "h24", "h24"),
            buys_5m=_sum_txns(pairs, "m5", "buys"),
            sells_5m=_sum_txns(pairs, "m5", "sells"),
            buys_1h=_sum_txns(pairs, "h1", "buys"),
            sells_1h=_sum_txns(pairs, "h1", "sells"),
            pair_created_at_ms=_i(best.get("pairCreatedAt")),
            pair_address=best.get("pairAddress", ""),
            pool_count=len(pairs),
            oldest_pair_created_at_ms=oldest_created,
            total_supply=_f(chain.get("total_supply")),
            decimals=_i(chain.get("decimals")),
            mint_authority=chain.get("mint_authority"),
            freeze_authority=chain.get("freeze_authority"),
            top_holder_pct=chain.get("top_holder_pct"),
            fetched_at_ms=int(time.time() * 1000),
            data_quality=quality,
            data_warnings=warnings,
        )
        _OVERVIEW_CACHE[mint] = (now, overview)
        return overview
    except Exception:
        return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)


@dataclass
class RugVerdict:
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    top_holder_pct: float | None
    risk_level: str
    notes: list[str]
    risk_score: int = 100


@dataclass
class TradeAnalysis:
    risk_score: int
    risk_level: str
    momentum_score: int
    liquidity_score: int
    flow_score: int
    age_score: int
    reasons: list[str]


def _market_risk(o: TokenOverview) -> tuple[int, list[str], int, int, int, int]:
    risk = 0
    reasons: list[str] = []
    if o.liquidity_usd < 10_000:
        risk += 25; reasons.append("Very low liquidity")
    elif o.liquidity_usd < 25_000:
        risk += 12; reasons.append("Low liquidity")

    if o.market_cap <= 0:
        risk += 20; reasons.append("Market cap unavailable")
    elif o.liquidity_mcap_pct < 5:
        risk += 15; reasons.append(f"Thin liquidity ({o.liquidity_mcap_pct:.1f}% of MC)")
    elif o.liquidity_mcap_pct < 8:
        risk += 7; reasons.append(f"Moderate liquidity ({o.liquidity_mcap_pct:.1f}% of MC)")

    if o.volume_5m < 500:
        risk += 15; reasons.append("Very low 5m volume")
    elif o.volume_5m < 1000:
        risk += 7; reasons.append("Low 5m volume")

    ratio = o.buy_sell_ratio_5m
    if o.sells_5m > o.buys_5m and o.sells_5m > 0:
        risk += 8; reasons.append(f"Sell pressure (buy/sell {ratio:.2f})")
    if o.sells_5m > o.buys_5m * 1.5 and o.sells_5m > 0:
        risk += 7

    if o.change_5m > 35:
        risk += 12; reasons.append("5m move is overheated")
    elif o.change_5m < -20:
        risk += 12; reasons.append("5m momentum is strongly negative")
    elif o.change_5m < 0:
        risk += 5; reasons.append("5m momentum is negative")

    age = o.age_minutes
    if age is None:
        risk += 10; reasons.append("Pool age unavailable")
    elif age < 5:
        risk += 15; reasons.append("Pool is extremely new")
    elif age < 10:
        risk += 6; reasons.append("Pool has limited trading history")

    if o.data_quality != "HIGH":
        risk += 8
        reasons.append("Market-data coverage is not fully verified")

    risk = min(100, max(0, int(risk)))
    momentum = int(max(0, min(100, 50 + max(-30, min(30, o.change_5m * 1.2)) + max(-20, min(20, o.change_1h * 0.4)))))
    liquidity_score = int(max(0, min(100, o.liquidity_mcap_pct * 5)))
    flow_score = int(max(0, min(100, 50 + (ratio - 1) * 25))) if o.sells_5m else 60
    age_score = 70 if age is not None and 10 <= age <= 1440 else 40
    return risk, reasons, momentum, liquidity_score, flow_score, age_score


async def get_rug_verdict(mint: str, overview: TokenOverview | None = None) -> RugVerdict:
    try:
        if overview is None:
            overview = await get_token_overview(mint)

        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{settings.rugcheck_api}/tokens/{mint}/report")
            if r.status_code != 200:
                if overview.data_quality == "LOW":
                    return RugVerdict(False, False, overview.top_holder_pct, "UNKNOWN", ["RugCheck unavailable and on-chain security data unavailable — automatic trading blocked"], 100)
                return RugVerdict(
                    overview.mint_authority is None,
                    overview.freeze_authority is None,
                    overview.top_holder_pct,
                    "UNKNOWN",
                    ["RugCheck unavailable — automatic trading blocked"],
                    100,
                )
            data = r.json()

        mint_revoked = overview.mint_authority is None if overview.mint_authority is not None or overview.data_quality != "LOW" else data.get("mintAuthority") is None
        freeze_revoked = overview.freeze_authority is None if overview.freeze_authority is not None or overview.data_quality != "LOW" else data.get("freezeAuthority") is None

        top_pct_candidates: list[float] = []
        if overview.top_holder_pct is not None:
            top_pct_candidates.append(float(overview.top_holder_pct))
        for holder in data.get("topHolders") or []:
            try:
                pct = float(holder.get("pct", 0))
                if pct > 0:
                    top_pct_candidates.append(pct)
                    break
            except (TypeError, ValueError):
                pass
        top_pct = max(top_pct_candidates) if top_pct_candidates else None

        security_risk = 0
        security_notes: list[str] = []
        if not mint_revoked:
            security_risk += 20; security_notes.append("Mint authority NOT revoked")
        if not freeze_revoked:
            security_risk += 20; security_notes.append("Freeze authority NOT revoked")
        if top_pct is not None and top_pct > 20:
            security_risk += 15; security_notes.append(f"Top token account concentration {top_pct:.1f}%")
        elif top_pct is not None and top_pct > 10:
            security_risk += 7; security_notes.append(f"Top token account concentration {top_pct:.1f}%")

        if not overview.found:
            return RugVerdict(mint_revoked, freeze_revoked, top_pct, "UNKNOWN", ["Market data unavailable — automatic trading blocked"], 100)

        market_risk, reasons, _, _, _, _ = _market_risk(overview)
        score = min(100, security_risk + market_risk)
        level = "LOW" if score <= 20 else "MEDIUM" if score <= 45 else "HIGH"

        notes = [f"Risk score: {score}/100 ({level})"]
        notes.append(f"Source: DexScreener + Solana RPC • data quality: {overview.data_quality}")
        notes.append(f"Liquidity: ${overview.liquidity_usd:,.0f} • 24h volume: ${overview.volume_24h:,.0f} • {overview.pool_count} pools returned")
        notes.append(f"5m volume: ${overview.volume_5m:,.0f} • 5m trades: {overview.total_trades_5m:,} • Buy/Sell: {overview.buy_sell_ratio_5m:.2f}")
        if overview.total_supply > 0:
            notes.append(f"On-chain supply: {overview.total_supply:,.6f} • decimals: {overview.decimals} • FDV: ${overview.fdv:,.0f}")
        if overview.data_warnings:
            notes.extend(f"⚠️ {w}" for w in overview.data_warnings[:2])
        notes.append(f"5m: {overview.change_5m:+.1f}% • 1h: {overview.change_1h:+.1f}%")
        if overview.age_minutes is not None:
            notes.append(f"Primary pool age: {overview.age_minutes:.0f} min • oldest: {overview.oldest_pool_age_minutes or overview.age_minutes:.0f} min")
        notes.extend(security_notes)
        notes.extend(reasons[:3])
        return RugVerdict(mint_revoked, freeze_revoked, top_pct, level, notes[:12], score)
    except Exception:
        return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck lookup failed — automatic trading blocked"], 100)


async def analyze_token(mint: str) -> tuple[TokenOverview, RugVerdict, TradeAnalysis]:
    overview = await get_token_overview(mint)
    rug = await get_rug_verdict(mint, overview)
    if not overview.found or rug.risk_level == "UNKNOWN":
        analysis = TradeAnalysis(100, "HIGH", 0, 0, 0, 0, ["Critical market/security data unavailable"])
        return overview, rug, analysis

    market_risk, reasons, momentum, liquidity, flow, age = _market_risk(overview)
    security_component = max(0, rug.risk_score - market_risk)
    total = min(100, security_component + market_risk)
    level = "LOW" if total <= 20 else "MEDIUM" if total <= 45 else "HIGH"
    analysis = TradeAnalysis(total, level, momentum, liquidity, flow, age, reasons[:10] + rug.notes[:4])
    rug.risk_score = total
    rug.risk_level = level
    return overview, rug, analysis
