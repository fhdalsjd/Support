"""
Centralized configuration loaded from environment variables.

No secrets or business-rule thresholds are hard-coded anywhere else in the
codebase -- everything tunable lives here and is sourced from the
environment (see .env.example).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[config] Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def _parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            ids.add(int(chunk))
    return ids


def _parse_channel_links(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]

    api_id: int
    api_hash: str
    telethon_session: str
    telethon_session_string: str | None

    database_url: str

    min_subscribers: int
    min_average_views: int
    min_referral_views: int
    verification_hours: int
    official_bot_username: str
    average_views_sample_size: int
    tracking_poll_minutes: int
    apply_rate_limit_seconds: int
    moderation_channel_links: tuple[str, ...]

    referral_url_prefix: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "referral_url_prefix",
            f"https://t.me/{self.official_bot_username}?start=",
        )


def load_settings() -> Settings:
    bot_token = _require("BOT_TOKEN")
    admin_raw = _require("ADMIN_ID")
    api_id = _require("API_ID")
    api_hash = _require("API_HASH")

    session_path = os.environ.get("TELETHON_SESSION", "sessions/ad_bot_session")
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    database_url = os.environ.get("DATABASE_URL", "sqlite:///./data/bot.db")
    if database_url.startswith("sqlite:///./"):
        db_file = database_url.replace("sqlite:///./", "")
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(admin_raw),
        api_id=int(api_id),
        api_hash=api_hash,
        telethon_session=session_path,
        telethon_session_string=os.environ.get("TELETHON_SESSION_STRING") or None,
        database_url=database_url,
        min_subscribers=int(os.environ.get("MIN_SUBSCRIBERS", 1000)),
        min_average_views=int(os.environ.get("MIN_AVERAGE_VIEWS", 200)),
        min_referral_views=int(os.environ.get("MIN_REFERRAL_VIEWS", 100)),
        verification_hours=int(os.environ.get("VERIFICATION_HOURS", 24)),
        official_bot_username=os.environ.get("OFFICIAL_BOT_USERNAME", "Hfearnmoneyv4bot"),
        average_views_sample_size=int(os.environ.get("AVERAGE_VIEWS_SAMPLE_SIZE", 15)),
        tracking_poll_minutes=int(os.environ.get("TRACKING_POLL_MINUTES", 15)),
        apply_rate_limit_seconds=int(os.environ.get("APPLY_RATE_LIMIT_SECONDS", 30)),
        moderation_channel_links=_parse_channel_links(os.environ.get("MODERATION_CHANNEL_LINKS", "")),
    )


settings = load_settings()
