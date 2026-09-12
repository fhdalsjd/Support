"""Production startup hooks for resilient token pricing and market snapshots.

The normal analysis uses DexScreener + Solana RPC. This hook adds provider
fallbacks so a newly-created or pre-graduation Solana token is still recognized
instead of being reported as an unknown token, while keeping automatic trading
fail-closed when market data is insufficient.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import statistics
import time

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

    async def _json_get(url: str, timeout: float = 8.0):
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json", "User-Agent": "SupportBot/1.0"}) as http:
            r = await http.get(url)
            r.raise_for_status()
            return r.json()

    async def _jupiter_price(mint: str) -> float | None:
        try:
            url = settings.jupiter_price_api.rstrip("/") + "?ids=" + mint
            payload = await _json_get(url)
            value = (payload.get(mint) or {}).get("usdPrice")
            price = float(value) if value is not None else 0.0
            return price if price > 0 else None
        except Exception as exc:
            log.debug("Jupiter price unavailable for %s: %s", mint, type(exc).__name__)
            return None

    async def _dex_pairs_search(mint: str) -> list[dict]:
        """Fallback to DexScreener search when token-pairs indexing is delayed."""
        try:
            payload = await _json_get(f"https://api.dexscreener.com/latest/dex/search?q={mint}")
            pairs = payload.get("pairs") if isinstance(payload, dict) else []
            return [p for p in (pairs or []) if p.get("chainId") == "solana" and p.get("pairAddress")]
        except Exception as exc:
            log.debug("DexScreener search unavailable for %s: %s", mint, type(exc).__name__)
            return []

    async def _gecko_pools(mint: str) -> list[dict]:
        """Fallback pool source; useful when DexScreener has not indexed a new pool yet."""
        try:
            payload = await _json_get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools", 10.0)
            rows = payload.get("data") or []
            out = []
            for item in rows:
                attrs = item.get("attributes") or {}
                reserve = float(attrs.get("reserve_in_usd") or 0)
                if reserve <= 0:
                    continue
                # Normalize the fields security.py expects where possible.
                name = str(attrs.get("name") or "")
                out.append({
                    "chainId": "solana",
                    "pairAddress": str(item.get("id") or "").split("_", 1)[-1],
                    "dexId": name.split(" / ")[-1] if name else "geckoterminal",
                    "baseToken": {"name": attrs.get("name") or "Unknown", "symbol": "?"},
                    "priceUsd": attrs.get("base_token_price_usd"),
                    "liquidity": {"usd": reserve},
                    "volume": {"m5": attrs.get("volume_usd", {}).get("m5", 0) if isinstance(attrs.get("volume_usd"), dict) else 0,
                               "h1": attrs.get("volume_usd", {}).get("h1", 0) if isinstance(attrs.get("volume_usd"), dict) else 0,
                               "h6": attrs.get("volume_usd", {}).get("h6", 0) if isinstance(attrs.get("volume_usd"), dict) else 0,
                               "h24": attrs.get("volume_usd", {}).get("h24", 0) if isinstance(attrs.get("volume_usd"), dict) else 0},
                    "txns": {},
                    "priceChange": {},
                    "pairCreatedAt": int(float(attrs.get("pool_created_at") or 0) * 1000) if attrs.get("pool_created_at") else 0,
                })
            return out
        except Exception as exc:
            log.debug("GeckoTerminal pools unavailable for %s: %s", mint, type(exc).__name__)
            return []

    async def _pumpfun_coin(mint: str) -> dict | None:
        """Best-effort metadata fallback for pump.fun tokens before graduation."""
        urls = (
            f"https://frontend-api-v3.pump.fun/coins-v2/{mint}",
            f"https://frontend-api-v3.pump.fun/coins/{mint}?sync=false",
        )
        for url in urls:
            try:
                payload = await _json_get(url, 8.0)
                if isinstance(payload, dict) and (payload.get("mint") or payload.get("address")):
                    return payload
            except Exception as exc:
                log.debug("pump.fun metadata unavailable for %s: %s", mint, type(exc).__name__)
        return None

    async def _chain_fallback(mint: str) -> dict:
        """Read enough authoritative on-chain data to identify a token without pools."""
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                responses = await asyncio.gather(
                    http.post(settings.rpc_url, json={"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":[mint,{"commitment":"confirmed"}]}),
                    http.post(settings.rpc_url, json={"jsonrpc":"2.0","id":2,"method":"getAccountInfo","params":[mint,{"encoding":"jsonParsed","commitment":"confirmed"}]}),
                    return_exceptions=True,
                )
            supply_payload = responses[0].json() if not isinstance(responses[0], Exception) else {}
            account_payload = responses[1].json() if not isinstance(responses[1], Exception) else {}
            supply = ((supply_payload.get("result") or {}).get("value") or {})
            account = ((account_payload.get("result") or {}).get("value") or {})
            info = (((account.get("data") or {}).get("parsed") or {}).get("info") or {})
            decimals = int(supply.get("decimals") or info.get("decimals") or 0)
            amount = float(supply.get("uiAmountString") or 0)
            if amount <= 0:
                raw = float(supply.get("amount") or info.get("supply") or 0)
                amount = raw / (10 ** decimals) if decimals >= 0 else 0
            return {
                "available": bool((supply_payload.get("result") or {}).get("value")),
                "total_supply": amount,
                "decimals": decimals,
                "mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"),
            }
        except Exception as exc:
            log.debug("On-chain fallback unavailable for %s: %s", mint, type(exc).__name__)
            return {"available": False, "total_supply": 0.0, "decimals": 0}

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

    def _pool_median_price(pairs: list[dict]) -> float | None:
        prices = []
        for pair in pairs:
            price = _f(pair.get("priceUsd"))
            liq = _f((pair.get("liquidity") or {}).get("usd"))
            if price > 0 and liq >= 1_000:
                prices.append(price)
        return float(statistics.median(prices)) if prices else None

    async def _trusted_price(mint: str, pairs: list[dict] | None = None) -> float | None:
        pairs = pairs or []
        jup = await _jupiter_price(mint)
        median = _pool_median_price(pairs)
        if not median:
            search_pairs = await _dex_pairs_search(mint)
            median = _pool_median_price(search_pairs)
        if jup and median:
            ratio = jup / median
            if 0.33 <= ratio <= 3.0:
                return jup
            log.warning("Rejecting Jupiter outlier for %s: jupiter=%s median=%s", mint, jup, median)
            return median
        return jup or median

    def _sanitize_overview_with_pairs(overview, pairs: list[dict], trusted: float):
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
        if overview.total_supply > 0:
            overview.market_cap = overview.total_supply * trusted
            overview.fdv = overview.market_cap
        overview.market_cap_source = "Trusted live price × on-chain supply"
        if abs(old_price - trusted) / trusted > 0.20 if trusted > 0 else False:
            overview.data_warnings.append(f"Rejected outlier pool price ${old_price:,.6g}; trusted live price ${trusted:,.6g} used")
        overview.data_quality = "MEDIUM" if overview.data_quality == "HIGH" else overview.data_quality
        return overview

    async def _fallback_overview(mint: str):
        """Construct a real token overview when DexScreener has not indexed the mint."""
        chain, pump, jup, search_pairs, gecko_pairs = await asyncio.gather(
            _chain_fallback(mint), _pumpfun_coin(mint), _jupiter_price(mint), _dex_pairs_search(mint), _gecko_pools(mint)
        )
        pairs = search_pairs or gecko_pairs
        price = jup or _pool_median_price(pairs) or 0.0
        name = (pump or {}).get("name") or (pairs[0].get("baseToken") or {}).get("name") or "Unknown Token"
        symbol = (pump or {}).get("symbol") or (pairs[0].get("baseToken") or {}).get("symbol") or "?"
        supply = _f(chain.get("total_supply"))
        pump_mcap = _f((pump or {}).get("usd_market_cap"))
        market_cap = supply * price if supply > 0 and price > 0 else pump_mcap
        if not pairs and not price and not supply and not pump:
            return security.TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)
        warnings = ["DexScreener token-pairs endpoint returned no pools; fallback providers used"]
        if not pairs:
            warnings.append("No indexed AMM pool yet; token may still be on a bonding curve or too new")
        if pump:
            warnings.append("pump.fun metadata detected")
        quality = "LOW" if not pairs else "MEDIUM"
        return security.TokenOverview(
            mint=mint, name=name, symbol=symbol, price_usd=price,
            market_cap=market_cap, liquidity_usd=sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs),
            change_5m=0.0, change_1h=0.0,
            dex=(pairs[0].get("dexId") if pairs else ("pump.fun" if pump else "-")),
            found=True, fdv=market_cap,
            volume_5m=sum(_f((p.get("volume") or {}).get("m5")) for p in pairs),
            volume_1h=sum(_f((p.get("volume") or {}).get("h1")) for p in pairs),
            volume_6h=sum(_f((p.get("volume") or {}).get("h6")) for p in pairs),
            volume_24h=sum(_f((p.get("volume") or {}).get("h24")) for p in pairs),
            pair_address=(pairs[0].get("pairAddress") if pairs else ""),
            pool_count=len(pairs), total_supply=supply, decimals=_i(chain.get("decimals")),
            mint_authority=chain.get("mint_authority"), freeze_authority=chain.get("freeze_authority"),
            data_source="DexScreener + Jupiter + GeckoTerminal + Solana RPC + pump.fun fallback",
            market_cap_source="Trusted live price × on-chain supply" if supply and price else "pump.fun metadata" if pump_mcap else "Unavailable",
            fetched_at_ms=int(time.time() * 1000), data_quality=quality, data_warnings=warnings,
        )

    async def _safe_get_token_overview(mint: str):
        overview = await _ORIGINAL_OVERVIEW(mint)
        if not getattr(overview, "found", False):
            overview = await _fallback_overview(mint)
        if not getattr(overview, "found", False):
            return overview
        pairs = []
        try:
            pairs = await security._fetch_sol_pairs(mint)
        except Exception:
            pass
        trusted = await _trusted_price(mint, pairs)
        if trusted and trusted > 0 and pairs:
            return _sanitize_overview_with_pairs(overview, pairs, trusted)
        if trusted and trusted > 0:
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
            fallback = await _fallback_overview(mint)
            price = fallback.price_usd if getattr(fallback, "found", False) else None
        return price, None, None

    market_snapshot._live_dex_quote = _trusted_snapshot_quote

    def _snapshot_symbol(text: str) -> str:
        match = re.search(r"\*([^*\n]{1,32})\*\s*\(\s*`\$?([^`\)\n]{1,16})`\s*\)", text or "")
        return match.group(2).strip().upper() if match else "TOKEN"

    async def _send_message_with_snapshot(self, *args, **kwargs):
        result = await _ORIGINAL_SEND_MESSAGE(self, *args, **kwargs)
        try:
            chat_id = kwargs.get("chat_id") if "chat_id" in kwargs else (args[0] if args else None)
            text = kwargs.get("text") if "text" in kwargs else (args[1] if len(args) > 1 else "")
            if chat_id not in settings.admin_ids or not text or "Market Cap:" not in text or "Liquidity:" not in text:
                return result
            match = _MINT_RE.search(text)
            if not match:
                return result
            mint = match.group(0)
            symbol = _snapshot_symbol(text)
            overview = await _safe_get_token_overview(mint)
            png = await market_snapshot.build_market_snapshot(mint, symbol, getattr(overview, "price_usd", None), getattr(overview, "change_5m", None), getattr(overview, "liquidity_usd", None))
            if png:
                await self.send_photo(chat_id=chat_id, photo=io.BytesIO(png), caption=f"📸 *Live Market Snapshot* — ${symbol}\nPrice source: validated live Solana providers\nChart: GeckoTerminal", parse_mode="Markdown")
        except Exception as exc:
            log.warning("Market snapshot failed: %s", type(exc).__name__)
        return result

    Bot.send_message = _send_message_with_snapshot
    log.info("Resilient token lookup + trusted price guard enabled")
except Exception as exc:
    log.warning("Optional startup hook unavailable: %s", type(exc).__name__)
