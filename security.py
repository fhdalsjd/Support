"""
security.py — Token metadata/market data (DexScreener) + rug/security
verdict (RugCheck) for any pasted Solana mint address.
"""
import httpx
from dataclasses import dataclass

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


@dataclass
class RugVerdict:
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    top_holder_pct: float | None
    risk_level: str          # "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
    notes: list[str]


async def get_token_overview(mint: str) -> TokenOverview:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)

        # Pick the highest-liquidity pair as the canonical price source
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
        base = best.get("baseToken", {})
        return TokenOverview(
            mint=mint,
            name=base.get("name", "Unknown"),
            symbol=base.get("symbol", "?"),
            price_usd=float(best.get("priceUsd") or 0),
            market_cap=float(best.get("fdv") or 0),
            liquidity_usd=float(best.get("liquidity", {}).get("usd") or 0),
            change_5m=float(best.get("priceChange", {}).get("m5") or 0),
            change_1h=float(best.get("priceChange", {}).get("h1") or 0),
            dex=best.get("dexId", "-"),
        )


async def get_rug_verdict(mint: str) -> RugVerdict:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{settings.rugcheck_api}/tokens/{mint}/report")
            if r.status_code != 200:
                return RugVerdict(False, False, None, "UNKNOWN", ["RugCheck data unavailable — verify manually"])
            data = r.json()

        mint_auth = data.get("mintAuthority")
        freeze_auth = data.get("freezeAuthority")
        mint_revoked = mint_auth is None
        freeze_revoked = freeze_auth is None

        top_pct = None
        holders = data.get("topHolders") or []
        if holders:
            top_pct = sum(h.get("pct", 0) for h in holders[:1])

        notes = []
        risk_score = 0
        if not mint_revoked:
            notes.append("⚠️ Mint authority NOT revoked — supply can be inflated")
            risk_score += 2
        if not freeze_revoked:
            notes.append("⚠️ Freeze authority NOT revoked — your tokens could be frozen")
            risk_score += 2
        if top_pct and top_pct > 20:
            notes.append(f"⚠️ Top holder owns {top_pct:.1f}% of supply")
            risk_score += 1
        if not notes:
            notes.append("No major red flags found in automated check")

        risk_level = "HIGH" if risk_score >= 3 else "MEDIUM" if risk_score >= 1 else "LOW"

        return RugVerdict(
            mint_authority_revoked=mint_revoked,
            freeze_authority_revoked=freeze_revoked,
            top_holder_pct=top_pct,
            risk_level=risk_level,
            notes=notes,
        )
    except Exception as e:
        return RugVerdict(False, False, None, "UNKNOWN", [f"RugCheck lookup failed: {e}"])
