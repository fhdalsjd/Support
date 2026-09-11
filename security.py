"""Market data and deterministic security/risk analysis shared by the bot UI and auto-trader.

Market values are aggregated across all Solana pools returned for the mint.
The most liquid pool supplies the displayed executable price/DEX, while
liquidity, volume and trade counts are summed across pools. This avoids the
single-pool mismatch where a token page can show much more liquidity/volume.
"""
from dataclasses import dataclass
import time
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
    """Volume-weight price change across pools instead of picking one pool's change."""
    total_volume = sum(_f((p.get("volume") or {}).get(volume_key)) for p in pairs)
    if total_volume <= 0:
        # Fall back to the most liquid pool when there is no usable volume.
        return _f(((max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd"))).get("priceChange") or {}).get(window)))
    weighted = 0.0
    for p in pairs:
        weighted += _f(((p.get("priceChange") or {}).get(window))) * _f((p.get("volume") or {}).get(volume_key))
    return weighted / total_volume


async def _fetch_sol_pairs(mint: str) -> list[dict]:
    # Use the token-pairs endpoint explicitly: it returns the complete set of
    # pools for the token, rather than relying on a single selected pair.
    url = f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(url)
        r.raise_for_status()
        data = r.json()
    return [p for p in (data or []) if p.get("chainId") == "solana"]


async def get_token_overview(mint: str) -> TokenOverview:
    try:
        pairs = await _fetch_sol_pairs(mint)
        if not pairs:
            return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)

        liquid_pairs = [p for p in pairs if _f((p.get("liquidity") or {}).get("usd")) > 0]
        if not liquid_pairs:
            liquid_pairs = pairs
        best = max(liquid_pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
        base = best.get("baseToken") or {}

        market_caps = [_f(p.get("marketCap")) for p in pairs if _f(p.get("marketCap")) > 0]
        fdvs = [_f(p.get("fdv")) for p in pairs if _f(p.get("fdv")) > 0]
        market_cap = max(market_caps) if market_caps else (max(fdvs) if fdvs else 0.0)
        fdv = max(fdvs) if fdvs else market_cap

        created = [_i(p.get("pairCreatedAt")) for p in pairs if _i(p.get("pairCreatedAt")) > 0]
        oldest_created = min(created) if created else 0

        return TokenOverview(
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
            # age_minutes intentionally refers to the most-liquid/executable
            # pool, because auto-sniper uses it as a launch/liquidity-age gate.
            pair_created_at_ms=_i(best.get("pairCreatedAt")),
            pair_address=best.get("pairAddress", ""),
            pool_count=len(pairs),
            oldest_pair_created_at_ms=oldest_created,
        )
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

    risk = min(100, max(0, int(risk)))
    momentum = int(max(0, min(100, 50 + max(-30, min(30, o.change_5m * 1.2)) + max(-20, min(20, o.change_1h * 0.4)))))
    liquidity_score = int(max(0, min(100, o.liquidity_mcap_pct * 5)))
    flow_score = int(max(0, min(100, 50 + (ratio - 1) * 25))) if o.sells_5m else 60
    age_score = 70 if age is not None and 10 <= age <= 1440 else 40
    return risk, reasons, momentum, liquidity_score, flow_score, age_score


async def get_rug_verdict(mint: str) -> RugVerdict:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{settings.rugcheck_api}/tokens/{mint}/report")
            if r.status_code != 200:
                return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck unavailable — automatic trading blocked"], 100)
            data = r.json()

        mint_auth = data.get("mintAuthority")
        freeze_auth = data.get("freezeAuthority")
        mint_revoked = mint_auth is None
        freeze_revoked = freeze_auth is None
        top_pct = None
        holders = data.get("topHolders") or []
        if holders:
            try:
                top_pct = float(holders[0].get("pct", 0))
            except (TypeError, ValueError):
                pass

        notes: list[str] = []
        security_risk = 0
        if not mint_revoked:
            security_risk += 20; notes.append("⚠️ Mint authority NOT revoked")
        if not freeze_revoked:
            security_risk += 20; notes.append("⚠️ Freeze authority NOT revoked")
        if top_pct and top_pct > 20:
            security_risk += 15; notes.append(f"⚠️ Top holder concentration {top_pct:.1f}%")
        elif top_pct and top_pct > 10:
            security_risk += 7; notes.append(f"⚠️ Top holder concentration {top_pct:.1f}%")

        overview = await get_token_overview(mint)
        if not overview.found:
            return RugVerdict(mint_revoked, freeze_revoked, top_pct, "UNKNOWN", notes + ["Market data unavailable — automatic trading blocked"], 100)
        market_risk, reasons, _, _, _, _ = _market_risk(overview)
        score = min(100, security_risk + market_risk)
        level = "LOW" if score <= 20 else "MEDIUM" if score <= 45 else "HIGH"
        notes = [f"Risk score: {score}/100 ({level})"]
        notes.append(f"Liquidity/MC: {overview.liquidity_mcap_pct:.1f}% • 5m volume: ${overview.volume_5m:,.0f}")
        notes.append(f"5m: {overview.change_5m:+.1f}% • Buy/Sell: {overview.buy_sell_ratio_5m:.2f}")
        if overview.age_minutes is not None:
            notes.append(f"Primary pool age: {overview.age_minutes:.0f} min • {overview.pool_count} Solana pools")
        if overview.oldest_pool_age_minutes is not None and overview.oldest_pool_age_minutes != overview.age_minutes:
            notes.append(f"Oldest pool age: {overview.oldest_pool_age_minutes:.0f} min")
        notes.extend(reasons[:4])
        return RugVerdict(mint_revoked, freeze_revoked, top_pct, level, notes, score)
    except Exception:
        return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck lookup failed — automatic trading blocked"], 100)


async def analyze_token(mint: str) -> tuple[TokenOverview, RugVerdict, TradeAnalysis]:
    overview = await get_token_overview(mint)
    rug = await get_rug_verdict(mint)
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
