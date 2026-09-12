"""Live market snapshot for Telegram CA analysis.

The headline price uses Jupiter Price V3 when available, with a robust
validated-pool fallback. GeckoTerminal supplies the historical chart.
"""
from __future__ import annotations

import io
import statistics
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont

GECKO_API = "https://api.geckoterminal.com/api/v2"
DEX_API = "https://api.dexscreener.com"
JUPITER_PRICE_API = "https://lite-api.jup.ag/price/v3"


def _font(size: int, bold: bool = False):
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


async def _get_json(url: str):
    async with httpx.AsyncClient(timeout=12, headers={"Accept": "application/json", "User-Agent": "SupportBot/1.0"}) as http:
        r = await http.get(url)
        r.raise_for_status()
        return r.json()


async def _best_pool(mint: str) -> tuple[str, str] | None:
    payload = await _get_json(f"{GECKO_API}/networks/solana/tokens/{mint}/pools")
    pools = payload.get("data") or []
    best = None
    best_liq = -1.0
    for item in pools:
        attrs = item.get("attributes") or {}
        try:
            liq = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        if liq > best_liq:
            best, best_liq = item, liq
    if not best:
        return None
    pool_id = str(best.get("id") or "")
    pool_address = pool_id.split("_", 1)[1] if "_" in pool_id else pool_id
    return pool_address, str((best.get("attributes") or {}).get("name") or "Solana pool")


async def _jupiter_price(mint: str) -> float | None:
    try:
        payload = await _get_json(f"{JUPITER_PRICE_API}?ids={mint}")
        value = ((payload.get(mint) or {}).get("usdPrice"))
        price = float(value) if value is not None else 0.0
        return price if price > 0 else None
    except Exception:
        return None


async def _validated_pool_price(mint: str) -> float | None:
    try:
        payload = await _get_json(f"{DEX_API}/token-pairs/v1/solana/{mint}")
        pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [])
        prices = []
        for pair in pairs:
            try:
                price = float(pair.get("priceUsd") or 0)
                liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
            except (TypeError, ValueError):
                continue
            if price > 0 and liq >= 1_000:
                prices.append(price)
        return float(statistics.median(prices)) if prices else None
    except Exception:
        return None


async def _live_dex_quote(mint: str):
    """Get a trusted live price; the second/third values are optional chart metadata."""
    jup, median = await _jupiter_price(mint), await _validated_pool_price(mint)
    if jup and median:
        ratio = jup / median if median > 0 else 1.0
        price = jup if 0.33 <= ratio <= 3.0 else median
    else:
        price = jup or median
    return price, None, None


async def build_market_snapshot(
    mint: str,
    symbol: str = "TOKEN",
    live_price_usd: float | None = None,
    live_change_5m: float | None = None,
    live_liquidity_usd: float | None = None,
) -> bytes | None:
    """Return a PNG snapshot with a trusted live headline price."""
    try:
        live_price, _, _ = await _live_dex_quote(mint)
        current_price = live_price or live_price_usd
        current_change = live_change_5m
        current_liq = live_liquidity_usd
        if not current_price or current_price <= 0:
            return None

        best = await _best_pool(mint)
        if not best:
            return None
        pool, pool_name = best
        payload = await _get_json(
            f"{GECKO_API}/networks/solana/pools/{pool}/ohlcv/minute"
            "?aggregate=5&limit=72&currency=usd&token=base"
        )
        rows = list(reversed(((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []))
        closes, volumes, times = [], [], []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                times.append(float(row[0])); closes.append(float(row[4])); volumes.append(float(row[5]))
            except (TypeError, ValueError):
                continue
        if len(closes) < 3:
            return None

        w, h = 1200, 700
        img = Image.new("RGB", (w, h), (18, 22, 29))
        draw = ImageDraw.Draw(img)
        title, sub, small = _font(34, True), _font(20), _font(16)
        price_font = _font(28, True)

        draw.text((42, 25), f"${symbol}  •  LIVE MARKET SNAPSHOT", font=title, fill=(240, 243, 247))
        draw.text((42, 72), f"LIVE PRICE  ${current_price:.10g}", font=price_font, fill=(245, 195, 80))
        change_text = "n/a" if current_change is None else f"{current_change:+.2f}%"
        liq_text = "n/a" if current_liq is None else f"${current_liq:,.0f}"
        draw.text((42, 112), f"5m {change_text}   •   Liquidity {liq_text}   •   Jupiter Price V3", font=sub, fill=(170, 181, 194))
        draw.text((42, 142), f"Chart: GeckoTerminal • most-liquid pool • {pool_name[:70]}", font=small, fill=(133, 145, 160))

        left, right, top, bottom = 60, w - 55, 180, 515
        lo, hi = min(closes), max(closes)
        if hi <= lo:
            hi = lo * 1.01 if lo else 1.0
        pad = (hi - lo) * 0.08
        lo -= pad; hi += pad
        for i in range(5):
            y = top + (bottom - top) * i / 4
            draw.line((left, y, right, y), fill=(43, 50, 61), width=1)
            value = hi - (hi - lo) * i / 4
            draw.text((right - 150, y - 10), f"${value:.8g}", font=small, fill=(145, 154, 168))

        pts = []
        for i, price in enumerate(closes):
            x = left + (right - left) * i / max(1, len(closes) - 1)
            y = bottom - (price - lo) / (hi - lo) * (bottom - top)
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            draw.line((a[0], a[1], b[0], b[1]), fill=(70, 190, 130), width=4)

        vmax = max(volumes) or 1.0
        vtop, vbottom = 545, 650
        bw = max(2, (right - left) / max(1, len(volumes)) * 0.7)
        for i, vol in enumerate(volumes):
            x = left + (right - left) * i / max(1, len(volumes) - 1)
            bar_h = vol / vmax * (vbottom - vtop)
            draw.rectangle((x - bw / 2, vbottom - bar_h, x + bw / 2, vbottom), fill=(65, 91, 120))

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        draw.text((42, 665), f"Generated {now} • price source: Jupiter Price V3 / validated pools", font=small, fill=(175, 185, 198))
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out.getvalue()
    except Exception:
        return None
