"""Generate a lightweight market snapshot image for Telegram token analysis.

The image is built from live GeckoTerminal OHLCV data for the most liquid
Solana pool returned for the mint. It is intentionally a chart snapshot,
not an invented price feed or a claim to be a browser screenshot.
"""
from __future__ import annotations

import io
import time
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont


API = "https://api.geckoterminal.com/api/v2"


def _font(size: int, bold: bool = False):
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


async def _get_json(url: str):
    async with httpx.AsyncClient(timeout=12, headers={"Accept": "application/json"}) as http:
        r = await http.get(url)
        r.raise_for_status()
        return r.json()


async def _best_pool(mint: str) -> tuple[str, str] | None:
    payload = await _get_json(f"{API}/networks/solana/tokens/{mint}/pools")
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
            best = item
            best_liq = liq
    if not best:
        return None
    pool_id = str(best.get("id") or "")
    # GeckoTerminal IDs are normally "solana_<pool_address>".
    pool_address = pool_id.split("_", 1)[1] if "_" in pool_id else pool_id
    attrs = best.get("attributes") or {}
    name = str(attrs.get("name") or "Solana pool")
    return pool_address, name


async def build_market_snapshot(mint: str, symbol: str = "TOKEN") -> bytes | None:
    """Return PNG bytes containing a recent price/volume market snapshot."""
    try:
        best = await _best_pool(mint)
        if not best:
            return None
        pool, pool_name = best
        payload = await _get_json(
            f"{API}/networks/solana/pools/{pool}/ohlcv/minute"
            "?aggregate=5&limit=72&currency=usd&token=base"
        )
        rows = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = list(reversed(rows))
        if len(rows) < 3:
            return None

        closes = []
        volumes = []
        times = []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                times.append(float(row[0]))
                closes.append(float(row[4]))
                volumes.append(float(row[5]))
            except (TypeError, ValueError):
                continue
        if len(closes) < 3:
            return None

        w, h = 1200, 700
        img = Image.new("RGB", (w, h), (18, 22, 29))
        draw = ImageDraw.Draw(img)
        title = _font(34, True)
        sub = _font(20, False)
        label = _font(18, True)
        small = _font(16, False)

        draw.text((42, 28), f"${symbol}  •  LIVE MARKET SNAPSHOT", font=title, fill=(240, 243, 247))
        draw.text((42, 76), "GeckoTerminal • Solana • most-liquid returned pool", font=sub, fill=(164, 174, 188))
        draw.text((42, 108), pool_name[:80], font=small, fill=(133, 145, 160))

        left, right = 60, w - 55
        top, bottom = 160, 515
        vtop, vbottom = 545, 650
        lo, hi = min(closes), max(closes)
        if hi <= lo:
            hi = lo * 1.01 if lo else 1.0
        pad = (hi - lo) * 0.08
        lo -= pad
        hi += pad

        # Grid and price labels.
        for i in range(5):
            y = top + (bottom - top) * i / 4
            draw.line((left, y, right, y), fill=(43, 50, 61), width=1)
            value = hi - (hi - lo) * i / 4
            draw.text((right - 145, y - 11), f"${value:.8g}", font=small, fill=(145, 154, 168))

        # Price line and volume bars.
        pts = []
        for i, price in enumerate(closes):
            x = left + (right - left) * i / max(1, len(closes) - 1)
            y = bottom - (price - lo) / (hi - lo) * (bottom - top)
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            draw.line((a[0], a[1], b[0], b[1]), fill=(70, 190, 130), width=4)
        draw.ellipse((pts[-1][0] - 6, pts[-1][1] - 6, pts[-1][0] + 6, pts[-1][1] + 6), fill=(245, 190, 70))

        vmax = max(volumes) or 1.0
        bw = max(2, (right - left) / max(1, len(volumes)) * 0.7)
        for i, vol in enumerate(volumes):
            x = left + (right - left) * i / max(1, len(volumes) - 1)
            bar_h = vol / vmax * (vbottom - vtop)
            draw.rectangle((x - bw / 2, vbottom - bar_h, x + bw / 2, vbottom), fill=(65, 91, 120))

        change = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0
        now = datetime.fromtimestamp(times[-1], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        draw.text((42, 665), f"Recent move: {change:+.2f}%   •   Last candle: {now}", font=small, fill=(175, 185, 198))
        draw.text((right - 280, 665), "Visual snapshot • not execution data", font=small, fill=(130, 140, 153))

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return None
