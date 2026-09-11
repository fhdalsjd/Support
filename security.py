"""
security.py — market data + deterministic risk analysis shared by the UI and
auto-trader. The bot never presents the score as a guarantee; it is a hard
filter built from observable market/security signals.
"""
from dataclasses import dataclass
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
        import time
        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60000.0)

    @property
    def liquidity_mcap_pct(self) -> float:
        if self.market_cap <= 0:
            return 0.0
        return self.liquidity_usd / self.market_cap * 100.0

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

            # One canonical pair prevents the UI and auto-trader from using
            # different prices for the same token. Prefer deepest liquidity.
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


def _risk_from_market(o: TokenOverview) -> tuple[int, list[str], int, int, int, int]:
    risk = 0
    reasons: list[str] = []

    if o.liquidity_usd < 10_000:
        risk += 25; reasons.append("Very low liquidity")
    elif o.liquidity_usd < 25_000:
        risk += 12; reasons.append("Low liquidity")

    if o.market_cap <= 0:
        risk += 20; reasons.append("Market cap unavailable")
    elif o.liquidity_mcap_pct < 5:
        risk += 15; reasons.append(f"Thin liquidity ({o.liquidity_mcap_pct:.1f}% of market cap)")
    elif o.liquidity_mcap_pct < 8:
        risk += 7; reasons.append(f"Moderate liquidity ({o.liquidity_mcap_pct:.1f}% of market cap)")

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
    risk_level = "LOW" if risk <= 20 else "MEDIUM" if risk <= 45 else "HIGH"

    momentum = 50
    momentum += max(-30, min(30, o.change_5m * 1.2))
    momentum += max(-20, min(20, o.change_1h * 0.4))
    momentum = int(max(0, min(100, momentum)))

    liquidity_score = int(max(0, min(100, o.liquidity_mcap_pct * 5)))
    flow_score = int(max(0, min(100, 50 + (ratio - 1) * 25))) if o.sells_5m else 60
    age_score = 70 if age is not None and 10 <= age <= 1440 else 40
    return risk, reasons, momentum, liquidity_score, flow_score, age_score


async def analyze_token(mint: str) -> tuple[TokenOverview, RugVerdict, TradeAnalysis]:
    overview = await get_token_overview(mint)
    rug = await get_rug_verdict(mint)
    market_risk, reasons, momentum, liquidity, flow, age = _risk_from_market(overview)

    security_risk = 0
    if not rug.mint_authority_revoked:
        security_risk += 20; reasons.append("Mint authority is active")
    if not rug.freeze_authority_revoked:
        security_risk += 20; reasons.append("Freeze authority is active")
    if rug.top_holder_pct is not None:
        if rug.top_holder_pct > 20:
            security_risk += 15; reasons.append(f"Top holder concentration {rug.top_holder_pct:.1f}%")
        elif rug.top_holder_pct > 10:
            security_risk += 7; reasons.append(f"Top holder concentration {rug.top_holder_pct:.1f}%")
    if rug.risk_level == "UNKNOWN":
        security_risk += 30; reasons.append("RugCheck unavailable")

    total = min(100, market_risk + security_risk)
    level = "LOW" if total <= 20 else "MEDIUM" if total <= 45 else "HIGH"
    analysis = TradeAnalysis(total, level, momentum, liquidity, flow, age, reasons[:10])
    rug.risk_score = total
    rug.risk_level = level
    if not rug.notes:
        rug.notes = []
    rug.notes = [f"Risk score: {total}/100 ({level})"] + reasons[:4] + rug.notes[:2]
    return overview, rug, analysis


async def get_rug_verdict(mint: str) -> RugVerdict:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{settings.rugcheck_api}/tokens/{mint}/report")
            if r.status_code != 200:
                return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck data unavailable — verify manually"], 100)
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
                top_pct = None

        notes = []
        if not mint_revoked:
            notes.append("⚠️ Mint authority NOT revoked — supply can be inflated")
        if not freeze_revoked:
            notes.append("⚠️ Freeze authority NOT revoked — tokens could be frozen")
        if top_pct and top_pct > 20:
            notes.append(f"⚠️ Top holder owns {top_pct:.1f}% of supply")
        if not notes:
            notes.append("No major authority/holder red flags found in automated check")

        score = 0
        if not mint_revoked: score += 20
        if not freeze_revoked: score += 20
        if top_pct and top_pct > 20: score += 15
        elif top_pct and top_pct > 10: score += 7
        level = "LOW" if score <= 20 else "MEDIUM" if score <= 45 else "HIGH"
        return RugVerdict(mint_revoked, freeze_revoked, top_pct, level, notes, score)
    except Exception:
        return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck lookup failed — verify manually"], 100)
