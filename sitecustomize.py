"""Runtime market-data hardening for the Support bot.

This module is loaded by Python before launcher/bot. It fixes two production
problems: Telegram callback refreshes that appear stuck, and stale/incorrect
token pricing caused by the 5-second overview cache or a bad single-pool quote.
It also adds best-effort fallback discovery for new/pre-graduation tokens.
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
    from telegram import Bot, CallbackQuery
    from config import settings
    import security
    import market_snapshot

    MINT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
    ORIGINAL_OVERVIEW = security.get_token_overview
    ORIGINAL_SEND_MESSAGE = Bot.send_message
    ORIGINAL_EDIT_MESSAGE = CallbackQuery.edit_message_text

    async def _json_get(url: str, timeout: float = 8.0):
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json", "User-Agent": "SupportBot/2.0"}) as http:
            r = await http.get(url)
            r.raise_for_status()
            return r.json()

    async def _jupiter_price(mint: str) -> float | None:
        try:
            payload = await _json_get(settings.jupiter_price_api.rstrip("/") + "?ids=" + mint)
            value = (payload.get(mint) or {}).get("usdPrice")
            price = float(value) if value is not None else 0.0
            return price if price > 0 else None
        except Exception as exc:
            log.debug("Jupiter price unavailable: %s", type(exc).__name__)
            return None

    async def _dex_search(mint: str) -> list[dict]:
        try:
            payload = await _json_get(f"https://api.dexscreener.com/latest/dex/search?q={mint}")
            rows = payload.get("pairs") if isinstance(payload, dict) else []
            return [p for p in (rows or []) if p.get("chainId") == "solana" and p.get("pairAddress")]
        except Exception as exc:
            log.debug("DexScreener search unavailable: %s", type(exc).__name__)
            return []

    async def _gecko_pools(mint: str) -> list[dict]:
        try:
            payload = await _json_get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools", 10.0)
            out = []
            for item in payload.get("data") or []:
                attrs = item.get("attributes") or {}
                reserve = float(attrs.get("reserve_in_usd") or 0)
                if reserve <= 0:
                    continue
                volume = attrs.get("volume_usd") or {}
                pool_id = str(item.get("id") or "")
                out.append({
                    "chainId": "solana",
                    "pairAddress": pool_id.split("_", 1)[-1],
                    "dexId": str(attrs.get("dex_id") or attrs.get("name") or "geckoterminal"),
                    "baseToken": {"name": attrs.get("name") or "Unknown", "symbol": "?"},
                    "priceUsd": attrs.get("base_token_price_usd"),
                    "liquidity": {"usd": reserve},
                    "volume": {"m5": volume.get("m5", 0), "h1": volume.get("h1", 0), "h6": volume.get("h6", 0), "h24": volume.get("h24", 0)},
                    "txns": {}, "priceChange": {}, "pairCreatedAt": 0,
                })
            return out
        except Exception as exc:
            log.debug("GeckoTerminal pools unavailable: %s", type(exc).__name__)
            return []

    async def _pumpfun_coin(mint: str) -> dict | None:
        for url in (f"https://frontend-api-v3.pump.fun/coins-v2/{mint}", f"https://frontend-api-v3.pump.fun/coins/{mint}?sync=false"):
            try:
                payload = await _json_get(url, 8.0)
                if isinstance(payload, dict) and (payload.get("mint") or payload.get("address")):
                    return payload
            except Exception as exc:
                log.debug("pump.fun lookup unavailable: %s", type(exc).__name__)
        return None

    async def _chain_snapshot(mint: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                a, b, c = await asyncio.gather(
                    http.post(settings.rpc_url, json={"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":[mint,{"commitment":"confirmed"}]}),
                    http.post(settings.rpc_url, json={"jsonrpc":"2.0","id":2,"method":"getAccountInfo","params":[mint,{"encoding":"jsonParsed","commitment":"confirmed"}]}),
                    http.post(settings.rpc_url, json={"jsonrpc":"2.0","id":3,"method":"getTokenLargestAccounts","params":[mint,{"commitment":"confirmed"}]}),
                    return_exceptions=True,
                )
            supply_payload = a.json() if not isinstance(a, Exception) else {}
            account_payload = b.json() if not isinstance(b, Exception) else {}
            largest_payload = c.json() if not isinstance(c, Exception) else {}
            supply = ((supply_payload.get("result") or {}).get("value") or {})
            account = ((account_payload.get("result") or {}).get("value") or {})
            info = (((account.get("data") or {}).get("parsed") or {}).get("info") or {})
            decimals = int(supply.get("decimals") or info.get("decimals") or 0)
            amount = float(supply.get("uiAmountString") or 0)
            if amount <= 0 and supply.get("amount"):
                amount = float(supply["amount"]) / (10 ** decimals)
            top_pct = None
            accounts = ((largest_payload.get("result") or {}).get("value") or [])
            if accounts and amount > 0:
                raw_total = amount * (10 ** decimals)
                top_pct = max(float(x.get("amount") or 0) for x in accounts) / raw_total * 100
            return {"available": bool(supply), "total_supply": amount, "decimals": decimals, "mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"), "top_holder_pct": top_pct}
        except Exception as exc:
            log.debug("Solana RPC fallback unavailable: %s", type(exc).__name__)
            return {"available": False, "total_supply": 0.0, "decimals": 0}

    def _f(v, default=0.0):
        try:
            return float(v or default)
        except (TypeError, ValueError):
            return float(default)

    def _i(v, default=0):
        try:
            return int(v or default)
        except (TypeError, ValueError):
            return int(default)

    def _median_price(pairs: list[dict]) -> float | None:
        prices = [_f(p.get("priceUsd")) for p in pairs if _f(p.get("priceUsd")) > 0 and _f((p.get("liquidity") or {}).get("usd")) >= 1000]
        return float(statistics.median(prices)) if prices else None

    async def _trusted_price(mint: str, pairs: list[dict]) -> float | None:
        jup, searched = await asyncio.gather(_jupiter_price(mint), _dex_search(mint))
        median = _median_price(pairs) or _median_price(searched)
        if jup and median:
            if 0.33 <= jup / median <= 3.0:
                return jup
            log.warning("Rejected live-price outlier for %s: jupiter=%s median=%s", mint, jup, median)
            return median
        return jup or median

    async def _fallback_overview(mint: str):
        chain, pump, jup, searched, gecko = await asyncio.gather(_chain_snapshot(mint), _pumpfun_coin(mint), _jupiter_price(mint), _dex_search(mint), _gecko_pools(mint))
        pairs = searched or gecko
        price = jup or _median_price(pairs) or 0.0
        supply = _f(chain.get("total_supply"))
        pump_mcap = _f((pump or {}).get("usd_market_cap"))
        market_cap = supply * price if supply and price else pump_mcap
        if not (pairs or price or supply or pump):
            return security.TokenOverview(mint, "Unknown", "?", 0, 0, 0, 0, 0, "-", found=False)
        base = (pairs[0].get("baseToken") or {}) if pairs else {}
        name = (pump or {}).get("name") or base.get("name") or "Unknown Token"
        symbol = (pump or {}).get("symbol") or base.get("symbol") or "?"
        warnings = ["DexScreener token-pairs returned no usable pool; fallback providers used"]
        if not pairs:
            warnings.append("No indexed AMM pool yet; token may be on a bonding curve or too new")
        if pump:
            warnings.append("pump.fun metadata detected")
        return security.TokenOverview(
            mint=mint, name=name, symbol=symbol, price_usd=price, market_cap=market_cap,
            liquidity_usd=sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs), change_5m=0.0, change_1h=0.0,
            dex=(pairs[0].get("dexId") if pairs else ("pump.fun" if pump else "-")), found=True, fdv=market_cap,
            volume_5m=sum(_f((p.get("volume") or {}).get("m5")) for p in pairs), volume_1h=sum(_f((p.get("volume") or {}).get("h1")) for p in pairs),
            volume_6h=sum(_f((p.get("volume") or {}).get("h6")) for p in pairs), volume_24h=sum(_f((p.get("volume") or {}).get("h24")) for p in pairs),
            pair_address=pairs[0].get("pairAddress", "") if pairs else "", pool_count=len(pairs), total_supply=supply, decimals=_i(chain.get("decimals")),
            mint_authority=chain.get("mint_authority"), freeze_authority=chain.get("freeze_authority"), top_holder_pct=chain.get("top_holder_pct"),
            data_source="DexScreener + Jupiter + GeckoTerminal + Solana RPC + pump.fun", market_cap_source=("Trusted live price × on-chain supply" if supply and price else "pump.fun metadata" if pump_mcap else "Unavailable"),
            fetched_at_ms=int(time.time() * 1000), data_quality="MEDIUM" if pairs else "LOW", data_warnings=warnings,
        )

    async def live_overview(mint: str):
        try:
            security._OVERVIEW_CACHE.pop(mint, None)
        except Exception:
            pass
        overview = await ORIGINAL_OVERVIEW(mint)
        if not getattr(overview, "found", False):
            overview = await _fallback_overview(mint)
        if not getattr(overview, "found", False):
            return overview
        try:
            pairs = await security._fetch_sol_pairs(mint)
        except Exception:
            pairs = []
        trusted = await _trusted_price(mint, pairs)
        if trusted and trusted > 0:
            old = _f(overview.price_usd)
            overview.price_usd = trusted
            if overview.total_supply > 0:
                overview.market_cap = overview.total_supply * trusted
                overview.fdv = overview.market_cap
                overview.market_cap_source = "Trusted live price × on-chain supply"
            if old > 0 and abs(old - trusted) / trusted > 0.20:
                overview.data_warnings.append(f"Rejected stale/outlier pool price ${old:,.6g}; live validated price ${trusted:,.6g} used")
        overview.fetched_at_ms = int(time.time() * 1000)
        return overview

    security.get_token_overview = live_overview

    async def _snapshot_quote(mint: str):
        price = await _trusted_price(mint, [])
        if not price:
            fallback = await _fallback_overview(mint)
            price = fallback.price_usd if getattr(fallback, "found", False) else None
        return price, None, None

    market_snapshot._live_dex_quote = _snapshot_quote

    async def _edit_with_answer(self, *args, **kwargs):
        try:
            await self.answer()
        except Exception:
            pass
        return await ORIGINAL_EDIT_MESSAGE(self, *args, **kwargs)

    CallbackQuery.edit_message_text = _edit_with_answer

    def _snapshot_symbol(text: str) -> str:
        m = re.search(r"\*([^*\n]{1,32})\*\s*\(\s*`\$?([^`\)\n]{1,16})`", text or "")
        return m.group(2).strip().upper() if m else "TOKEN"

    async def _send_message_with_snapshot(self, *args, **kwargs):
        result = await ORIGINAL_SEND_MESSAGE(self, *args, **kwargs)
        try:
            chat_id = kwargs.get("chat_id") if "chat_id" in kwargs else (args[0] if args else None)
            text = kwargs.get("text") if "text" in kwargs else (args[1] if len(args) > 1 else "")
            if chat_id not in settings.admin_ids or not text or "Market Cap:" not in text or "Liquidity:" not in text:
                return result
            match = MINT_RE.search(text)
            if not match:
                return result
            mint = match.group(0)
            symbol = _snapshot_symbol(text)
            overview = await live_overview(mint)
            png = await market_snapshot.build_market_snapshot(mint, symbol, getattr(overview, "price_usd", None), getattr(overview, "change_5m", None), getattr(overview, "liquidity_usd", None))
            if png:
                await self.send_photo(chat_id=chat_id, photo=io.BytesIO(png), caption=f"📸 Live Market Snapshot — ${symbol}\nPrice: validated live Solana providers\nChart: GeckoTerminal")
        except Exception as exc:
            log.warning("Market snapshot failed: %s", type(exc).__name__)
        return result

    Bot.send_message = _send_message_with_snapshot
    log.info("Live refresh + trusted pricing + new-token fallbacks enabled")
except Exception as exc:
    log.warning("Optional startup hook unavailable: %s", type(exc).__name__)
