"""Small production hook: attach a live market snapshot to CA analysis messages.

Python imports sitecustomize automatically when this project directory is on
sys.path (as it is for the Railway launcher). The hook is intentionally narrow:
it only reacts to admin token-analysis messages containing the CA plus the
analysis fields "Market Cap" and "Liquidity". Other Telegram messages are
untouched.
"""
from __future__ import annotations

import io
import re

try:
    from telegram import Bot
    from config import settings
    from market_snapshot import build_market_snapshot

    _MINT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
    _ORIGINAL_SEND_MESSAGE = Bot.send_message

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
            png = await build_market_snapshot(mint, symbol)
            if not png:
                return result
            await self.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(png),
                caption=f"📸 *Live Market Snapshot* — ${symbol}\nSource: GeckoTerminal data • most-liquid Solana pool",
                parse_mode="Markdown",
            )
        except Exception:
            # A snapshot must never break the underlying token analysis message.
            pass
        return result

    Bot.send_message = _send_message_with_snapshot
except Exception:
    # Startup must remain safe if an optional image dependency is unavailable.
    pass
