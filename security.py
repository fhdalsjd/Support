import re
from dataclasses import dataclass
import httpx
from settings import settings

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

@dataclass
class TokenInfo:
    mint: str
    name: str = "Unknown"
    symbol: str = "?"
    price_usd: float = 0.0
    market_cap: float = 0.0
    liquidity: float = 0.0
    change_5m: float = 0.0
    change_1h: float = 0.0
    verdict: str = "UNKNOWN"
    mint_authority: str = "unknown"
    freeze_authority: str = "unknown"
    pair_url: str = ""

async def inspect_token(mint: str) -> TokenInfo:
    if not MINT_RE.fullmatch(mint): raise ValueError("Invalid Solana mint address")
    info = TokenInfo(mint=mint)
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{settings.dexscreener_url}/token-pairs/v1/solana/{mint}", timeout=8); r.raise_for_status()
            pairs = r.json() if isinstance(r.json(), list) else []
            if pairs:
                p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
                base = p.get("baseToken") or {}; ch = p.get("priceChange") or {}
                info.name = base.get("name") or info.name; info.symbol = base.get("symbol") or info.symbol
                info.price_usd = float(p.get("priceUsd") or 0); info.market_cap = float(p.get("marketCap") or p.get("fdv") or 0)
                info.liquidity = float((p.get("liquidity") or {}).get("usd") or 0); info.change_5m = float(ch.get("m5") or 0); info.change_1h = float(ch.get("h1") or 0)
                info.pair_url = p.get("url") or ""
        except Exception: pass
        try:
            r = await client.get(f"{settings.rugcheck_url}/v1/tokens/{mint}/report", timeout=8); r.raise_for_status(); rc=r.json()
            risks = rc.get("risks") or []
            info.verdict = "RISK" if any((x.get("level") or "").lower() in {"danger","critical","high"} for x in risks) else "PASS"
            tm=rc.get("tokenMeta") or {}; info.mint_authority="set" if (tm.get("mintAuthority") or rc.get("mintAuthority")) else "disabled/unknown"; info.freeze_authority="set" if (tm.get("freezeAuthority") or rc.get("freezeAuthority")) else "disabled/unknown"
        except Exception: info.verdict="UNAVAILABLE"
    return info

def format_token(i: TokenInfo) -> str:
    return (f"🪙 <b>{i.name}</b> <code>${i.symbol}</code>\nCA: <code>{i.mint}</code>\nPrice: ${i.price_usd:.8g}\n"
            f"Market Cap: ${i.market_cap:,.0f}\nLiquidity: ${i.liquidity:,.0f}\n5m: {i.change_5m:+.2f}% | 1h: {i.change_1h:+.2f}%\n"
            f"RugCheck: <b>{i.verdict}</b> | Mint: {i.mint_authority} | Freeze: {i.freeze_authority}")
