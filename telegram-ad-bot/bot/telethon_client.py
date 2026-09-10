from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import ChannelPrivateError, ChannelInvalidError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, Message, MessageEntityTextUrl
from .config import settings
POST_URL_RE = re.compile(r"^https?://t\.me/(?P<username>[A-Za-z0-9_]{4,32})/(?P<post_id>\d+)/?(?:\?.*)?$")
class PostLinkError(ValueError): pass
class ChannelNotAccessibleError(RuntimeError): pass
@dataclass
class ParsedPostLink: channel_username: str; post_id: int
@dataclass
class ChannelSnapshot: channel_id: int; username: str; title: str; subscriber_count: int
@dataclass
class PostSnapshot: post_id: int; views: int; date: datetime; text: str; referral_link_found: bool

def parse_post_url(url: str) -> ParsedPostLink:
    match = POST_URL_RE.match(url.strip())
    if not match: raise PostLinkError("That doesn't look like a public channel post link. Expected format: https://t.me/channelname/123")
    return ParsedPostLink(match.group("username"), int(match.group("post_id")))

def extract_referral_link(message: Message) -> bool:
    prefix = settings.referral_url_prefix; haystacks = []
    if message.message: haystacks.append(message.message)
    if message.entities:
        for entity in message.entities:
            if isinstance(entity, MessageEntityTextUrl): haystacks.append(entity.url)
    if message.reply_markup and getattr(message.reply_markup, "rows", None):
        for row in message.reply_markup.rows:
            for button in row.buttons:
                if getattr(button, "url", None): haystacks.append(button.url)
    return any(prefix in text for text in haystacks)
class TelethonInspector:
    def __init__(self):
        self._client = TelegramClient(StringSession(settings.telethon_session_string), settings.api_id, settings.api_hash) if settings.telethon_session_string else TelegramClient(settings.telethon_session, settings.api_id, settings.api_hash)
    async def start(self):
        await self._client.connect()
        if not await self._client.is_user_authorized(): raise RuntimeError("Telethon session is not authorized. Run generate_session.py and set TELETHON_SESSION_STRING.")
    async def stop(self): await self._client.disconnect()
    async def get_channel_snapshot(self, username: str) -> ChannelSnapshot:
        try: entity = await self._client.get_entity(username)
        except (UsernameInvalidError, UsernameNotOccupiedError): raise ChannelNotAccessibleError(f"Channel @{username} does not exist.")
        except (ChannelPrivateError, ChannelInvalidError): raise ChannelNotAccessibleError(f"Channel @{username} is private or inaccessible.")
        if not isinstance(entity, Channel): raise ChannelNotAccessibleError(f"@{username} is not a broadcast channel.")
        try: full = await self._client(GetFullChannelRequest(entity))
        except (ChannelPrivateError, ChannelInvalidError): raise ChannelNotAccessibleError(f"Channel @{username} is private or inaccessible.")
        count = getattr(full.full_chat, "participants_count", None)
        if count is None: raise ChannelNotAccessibleError(f"Could not read the subscriber count for @{username}.")
        return ChannelSnapshot(entity.id, entity.username or username, entity.title, count)
    async def get_post_snapshot(self, username: str, post_id: int) -> PostSnapshot:
        try: message = await self._client.get_messages(username, ids=post_id)
        except (ChannelPrivateError, ChannelInvalidError): raise ChannelNotAccessibleError(f"Channel @{username} is private or inaccessible.")
        if message is None: raise ChannelNotAccessibleError("That post is unavailable or has been deleted.")
        return PostSnapshot(post_id, message.views or 0, message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(timezone.utc), message.message or "", extract_referral_link(message))
    async def get_recent_view_counts(self, username: str, limit: int) -> list[int]:
        views=[]
        async for message in self._client.iter_messages(username, limit=limit*2):
            if message is None or message.action is not None or message.views is None: continue
            views.append(message.views)
            if len(views)>=limit: break
        return views
inspector = TelethonInspector()
