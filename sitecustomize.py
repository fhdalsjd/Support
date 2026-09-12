"""Production startup hooks for trusted token pricing and market snapshots.

The bot's original analysis uses DexScreener pool data, but a single malformed
or stale pool can report an impossible token price. This hook adds a second
price check using Jupiter Price V3 when available, with a robust pool-median
fallback, then sanitizes the shared TokenOverview used by both the UI and the
auto-trader.
"""
from __future__ import annotations

import io
import logging
import re
import statistics

import httpx

log = logging.getLogger("support.sitecustomize")

try:
    from telegram import Bot
    from config import settings
    import security
    import market_snapshot

    _MINT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
    _ORIGINAL_SEND_MESSAGE = Bot.send_message
    _ORIGINAL_OVERVIEW = security.get_token_overview

    async def _jupiter_price(mint: str) -> float | None:
        """Return Jupiter's current USD price when the configured endpoint supplies one."""
        try:
            url = settings.jupiter_price_api.rstrip("/") + "?ids=" + mint
            async with httpx.AsyncClient(timeout=8, headers={"Accept": "application/json", "User-Agent": "SupportBot/1.0"}) as http:
                r = await http.get(url)
                r.raise_for_status()
                payload = r.json() or {}
            row = payload.get(mint) or {}
            value = row.get("usdPrice")
            value = float(value) if value is not None else 0.0
            return value if value > 0 else None
        except Exception as exc:
            log.debug("Jupiter price unavailable for %s: %s", mint, type(exc).__name__)
            return None

    async def _pool_median_price(mint: str) -> float | None:
        """Robust fallback that ignores zero-price and dust pools."""
        try:
            pairs = await security._fetch_sol_pairs(mint)
            prices = []
            for pair in pairs:
                try:
                    price = float(pair.get("priceUsd") or 0)
                    liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
                except (TypeError, ValueError):
                    continue
                if price > 0 and liq >= 1_000:
                    prices.append(price)
            if not prices:
                return None
            return float(statistics.median(prices))
        except Exception as exc:
            log.debug("Pool median unavailable for %s: %s", mint, type(exc).__name__)
            return None

    async def _trusted_price(mint: str) -> float | None:
        """Choose a current price with a sanity check against live pool prices."""
        jup = await _jupiter_price(mint)
        median = await _pool_median_price(mint)
        if jup and median:
            ratio = jup / median if median > 0 else 1.0
            if 0.33 <= ratio <= 3.0:
                return jup
            log.warning("Rejecting Jupiter outlier for %s: jupiter=%s median=%s", mint, jup, median)
            return median
        return jup or median

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

    def _sanitize_overview_with_pairs(overview, pairs: list[dict], trusted: float):
        """Remove pools whose quoted price is an extreme outlier before aggregation."""
        valid = []
        for pair in pairs:
            price = _f(pair.get("priceUsd"))
            if price <= 0:
                continue
            ratio = price / trusted if trusted > 0 else 1.0
            if 0.5 <= ratio <= 2.0:
                valid.append(pair)
        if not valid:
            return overview

        def vol(p, key):
            return _f((p.get("volume") or {}).get(key))

        def tx(p, window, side):
            return _i(((p.get("txns") or {}).get(window) or {}).get(side))

        def weighted_change(window, volume_key):
            total = sum(vol(p, volume_key) for p in valid)
            if total <= 0:
                return overview.change_5m if window == "m5" else overview.change_1h
            return sum(_f(((p.get("priceChange") or {}).get(window))) * vol(p, volume_key) for p in valid) / total

        old_price = overview.price_usd
        old_liquidity = overview.liquidity_usd
        overview.price_usd = trusted
        overview.liquidity_usd = sum(_f((p.get("liquidity") or {}).get("usd")) for p in valid)
        overview.volume_5m = sum(vol(p, "m5") for p in valid)
        overview.volume_1h = sum(vol(p, "h1") for p in valid)
        overview.volume_6h = sum(vol(p, "h6") for p in valid)
        overview.volume_24h = sum(vol(p, "h24") for p in valid)
        overview.buys_5m = sum(tx(p, "m5", "buys") for p in valid)
        overview.sells_5m = sum(tx(p, "m5", "sells") for p in valid)
        overview.buys_1h = sum(tx(p, "h1", "buys") for p in valid)
        overview.sells_1h = sum(tx(p, "h1", "sells") for p in valid)
        overview.change_5m = weighted_change("m5", "m5")
        overview.change_1h = weighted_change("h1", "h1")
        overview.change_6h = weighted_change("h6", "h6")
        overview.change_24h = weighted_change("h24", "h24")

        best = max(valid, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
        overview.dex = best.get("dexId", overview.dex)
        overview.pair_address = best.get("pairAddress", overview.pair_address)
        overview.pair_created_at_ms = _i(best.get("pairCreatedAt"), overview.pair_created_at_ms)
        overview.market_cap = overview.total_supply * trusted if overview.total_supply > 0 else overview.market_cap
        overview.fdv = overview.total_supply * trusted if overview.total_supply > 0 else overview.fdv
        overview.market_cap_source = "Trusted live price × on-chain supply"

        if abs(old_price - trusted) / trusted > 0.20 if trusted > 0 else False:
            overview.data_warnings.append(
                f"Rejected outlier pool price ${old_price:,.6g}; trusted live price ${trusted:,.6g} used"
            )
        if old_liquidity > overview.liquidity_usd * 3 and overview.liquidity_usd > 0:
            overview.data_warnings.append("Excluded extreme-price pool(s) from liquidity/volume aggregation")
        overview.data_quality = "MEDIUM" if overview.data_quality == "HIGH" else overview.data_quality
        return overview

    async def _safe_get_token_overview(mint: str):
        overview = await _ORIGINAL_OVERVIEW(mint)
        if not getattr(overview, "found", False):
            return overview
        trusted = await _trusted_price(mint)
        if not trusted or trusted <= 0:
            return overview
        try:
            pairs = await security._fetch_sol_pairs(mint)
            return _sanitize_overview_with_pairs(overview, pairs, trusted)
        except Exception as exc:
            log.debug("Price sanitization failed for %s: %s", mint, type(exc).__name__)
            overview.price_usd = trusted
            if overview.total_supply > 0:
                overview.market_cap = overview.total_supply * trusted
                overview.fdv = overview.market_cap
            overview.market_cap_source = "Trusted live price × on-chain supply"
            return overview

    security.get_token_overview = _safe_get_token_overview

    async def _trusted_snapshot_quote(mint: str):
        price = await _trusted_price(mint)
        if not price:
            return None, None, None
        return price, None, None

    # Prevent the snapshot module from replacing the trusted headline price
    # with a single anomalous DEX Screener pair.
    market_snapshot._live_dex_quote = _trusted_snapshot_quote

    def _snapshot_symbol(text: str) -> str:
        match = re.search(r"\*([^*\n]{1,32})\*\s*\(\s*`\$?([^`\)\n]{1,16})`\s*\)", text or "")
        if match:
            return match.group(2).strip().upper()
        return "TOKEN"

    async def _send_message_with_snapshot(self, *args, **kwargs):
        result = await _ORIGINAL_SEND_MESSAGE(self, *args, **kwargs)
        try:
            chat_id = kwargs.get("chat_id") if "chat_id" in kwargs else (args[0] if args else None)
            text = kwargs.get("text") if "text" in kwargs else (args[1] if len(args) > 1 else "")
            if chat_id not in settings.admin_ids or not text:
                return result
            if "Market Cap:" not in text or "Liquidity:" not in text:
                return result
            match = _MINT_RE.search(text)
            if not match:
                return result
            mint = match.group(0)
            symbol = _snapshot_symbol(text)
            overview = await _safe_get_token_overview(mint)
            png = await market_snapshot.build_market_snapshot(
                mint,
                symbol,
                getattr(overview, "price_usd", None),
                getattr(overview, "change_5m", None),
                getattr(overview, "liquidity_usd", None),
            )
            if not png:
                return result
            await self.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(png),
                caption=f"📸 *Live Market Snapshot* — ${symbol}\nPrice source: Jupiter Price V3 / validated Solana pools\nChart: GeckoTerminal",
                parse_mode="Markdown",
            )
        except Exception as exc:
            log.warning("Market snapshot failed: %s", type(exc).__name__)
        return result

    Bot.send_message = _send_message_with_snapshot
    log.info("Trusted token price guard enabled")
except Exception as exc:
    log.warning("Optional startup hook unavailable: %s", type(exc).__name__)
