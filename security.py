"""Market data and deterministic security/risk analysis shared by the bot UI and auto-trader.

Risk score is 0–100 where 0 is safest. It is a filter, not a prediction or
profit guarantee. Missing critical security data fails closed for automation.
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

    @property
    def age_minutes(self) -> float | None:
        if not self.pair_created_at_ms:
            return None
        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60000.0)

    @property
    def liquidity_mcap_pct(self) -> float:
        return self.liquidity_usd / self.market_cap * 100.0 if self.market_cap > 0 else 0.0

    @property
    def buy_sell_ratio_5m(self) -> float:
        if self.sells_5m <= 0:
            return float(self.buys_5m) if self.buys_5m else 0.0
        return self.buys_5m / self.sells_5m


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


async def get_token_overview(mint: str) -> TokenOverview:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            r.raise_for_status()
            data = r.json()
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)
            best = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
            base = best.get("baseToken") or {}
            tx5 = (best.get("txns") or {}).get("m5") or {}
            tx1 = (best.get("txns") or {}).get("h1") or {}
            vol = best.get("volume") or {}
            chg = best.get("priceChange") or {}
            market_cap = _f(best.get("marketCap"))
            fdv = _f(best.get("fdv"))
            return TokenOverview(
                mint=mint,
                name=base.get("name", "Unknown"),
                symbol=base.get("symbol", "?"),
                price_usd=_f(best.get("priceUsd")),
                market_cap=market_cap if market_cap > 0 else fdv,
                liquidity_usd=_f((best.get("liquidity") or {}).get("usd")),
                change_5m=_f(chg.get("m5")),
                change_1h=_f(chg.get("h1")),
                dex=best.get("dexId", "-"),
                fdv=fdv,
                volume_5m=_f(vol.get("m5")),
                volume_1h=_f(vol.get("h1")),
                volume_6h=_f(vol.get("h6")),
                volume_24h=_f(vol.get("h24")),
                change_6h=_f(chg.get("h6")),
                change_24h=_f(chg.get("h24")),
                buys_5m=_i(tx5.get("buys")),
                sells_5m=_i(tx5.get("sells")),
                buys_1h=_i(tx1.get("buys")),
                sells_1h=_i(tx1.get("sells")),
                pair_created_at_ms=_i(best.get("pairCreatedAt")),
                pair_address=best.get("pairAddress", ""),
            )
    except Exception:
        return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)


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
    """Return security risk plus the same market-risk components used by automation."""
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
            notes.append(f"Pool age: {overview.age_minutes:.0f} min")
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
    # get_rug_verdict already contains the same market component; recover only
    # the security component so the final score is not double-counted.
    security_component = max(0, rug.risk_score - market_risk)
    total = min(100, security_component + market_risk)
    level = "LOW" if total <= 20 else "MEDIUM" if total <= 45 else "HIGH"
    analysis = TradeAnalysis(total, level, momentum, liquidity, flow, age, reasons[:10] + rug.notes[:4])
    rug.risk_score = total
    rug.risk_level = level
    return overview, rug, analysis
