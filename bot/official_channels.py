"""Persistent admin-managed official channels used for automatic application decline."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import text
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from .config import settings
from .database import engine
from .keyboards import admin_back_keyboard, official_channel_cancel_keyboard, official_channels_keyboard

WAITING_OFFICIAL_CHANNEL = 40


def _admin_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in settings.admin_ids)


def ensure_official_channels_table() -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS official_channels (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                link TEXT NOT NULL,
                added_by BIGINT,
                added_at DATETIME NOT NULL
            )
        """))


def _channel_username(value: str | None) -> str:
    """Extract a public Telegram channel username from @name or a t.me/telegram.me link."""
    if not value:
        return ""
    value = value.strip()
    if value.startswith("@"):
        username = value[1:].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip().lower()
        return username if re.fullmatch(r"[a-zA-Z0-9_]{4,32}", username) else ""
    if not value.startswith(("https://", "http://")):
        return ""
    parsed = urlparse(value)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    if parts[0].lower() in {"s", "c"}:
        if parts[0].lower() == "c":
            return ""
        parts = parts[1:]
    if not parts or parts[0].startswith("+"):
        return ""
    username = parts[0].lstrip("@").lower()
    return username if re.fullmatch(r"[a-zA-Z0-9_]{4,32}", username) else ""


def is_official_channel(username: str | None) -> bool:
    """Return True only when the channel is explicitly protected by configuration/admin list."""
    key = _channel_username(username)
    if not key:
        return False

    official_bot = str(settings.official_bot_username or "").lstrip("@").lower()
    if official_bot and key == official_bot:
        return True

    ensure_official_channels_table()
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT 1 FROM official_channels WHERE lower(username)=:username LIMIT 1"),
            {"username": key},
        ).first()
    if row:
        return True

    configured = {_channel_username(link) for link in settings.moderation_channel_links}
    return key in configured


def is_official_post_url(post_url: str | None) -> bool:
    """Hard pre-check for a submitted post URL; no Telegram/Telethon verification is performed."""
    key = _channel_username(post_url)
    return bool(key and is_official_channel(key))


def _list_channels() -> list[tuple[int, str]]:
    ensure_official_channels_table()
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, username FROM official_channels ORDER BY lower(username)")).all()
    return [(int(row[0]), str(row[1])) for row in rows]


async def official_channel_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject protected official-channel post URLs before any application/verification work."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id in settings.admin_ids or not message.text:
        return

    value = message.text.strip()
    if not is_official_post_url(value):
        return

    await message.reply_text(
        "🚫 <b>Official Channel Not Allowed</b>\n\n"
        "Please send your <b>real channel</b> post link.\n\n"
        "❌ Please do not send <b>Hf Bot official channels</b> or other protected official channels.\n\n"
        "⚠️ <b>You may be banned for violating this rule.</b>\n\n"
        "Send a post link from your own qualifying channel to continue.",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


async def official_channels_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _admin_allowed(update):
        await query.answer("This area is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    channels = _list_channels()
    if channels:
        channel_lines = "\n".join(f"• <b>@{username}</b>" for _, username in channels)
    else:
        channel_lines = "<i>No official channels have been added yet.</i>"
    text_body = (
        "🛡️ <b>Official Channel Auto-Decline</b>\n\n"
        "Channels saved here are automatically declined when a user submits a post from them.\n\n"
        "<b>Saved channels</b>\n"
        f"{channel_lines}\n\n"
        "Add a public channel using <code>@channelusername</code> or its <code>https://t.me/channelusername</code> link."
    )
    await query.edit_message_text(text_body, parse_mode="HTML", reply_markup=official_channels_keyboard(channels))


async def official_channel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not _admin_allowed(update):
        await query.answer("This area is restricted to administrators.", show_alert=True)
        return ConversationHandler.END
    await query.answer("Send the official channel username or link.")
    await query.message.reply_text(
        "➕ <b>Add Official Channel</b>\n\n"
        "Send the public channel as <code>@channelusername</code> or <code>https://t.me/channelusername</code>.\n\n"
        "Private invite links are not supported for automatic matching.\n\n"
        "Send /cancel to stop.",
        parse_mode="HTML",
        reply_markup=official_channel_cancel_keyboard(),
    )
    return WAITING_OFFICIAL_CHANNEL


async def official_channel_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin_allowed(update):
        return ConversationHandler.END
    value = update.message.text.strip()
    username = _channel_username(value)
    if not username:
        await update.message.reply_text("⚠️ <b>Invalid channel</b>\n\nPlease send a public channel as <code>@channelusername</code> or <code>https://t.me/channelusername</code>.", parse_mode="HTML", reply_markup=official_channel_cancel_keyboard())
        return WAITING_OFFICIAL_CHANNEL
    ensure_official_channels_table()
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO official_channels (username, link, added_by, added_at) VALUES (:username, :link, :added_by, CURRENT_TIMESTAMP)"), {"username": username, "link": value, "added_by": update.effective_user.id})
    except Exception as exc:
        if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
        await update.message.reply_text(f"ℹ️ <b>@{username}</b> is already saved.\n\nAutomatic decline is already active for this channel.", parse_mode="HTML", reply_markup=official_channels_keyboard(_list_channels()))
        return ConversationHandler.END
    await update.message.reply_text(f"✅ <b>Official channel saved</b>\n\n<b>@{username}</b> is now protected by automatic decline.\n\nApplications submitted from this channel will be declined automatically.", parse_mode="HTML", reply_markup=official_channels_keyboard(_list_channels()))
    return ConversationHandler.END


async def official_channel_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _admin_allowed(update):
        await query.answer("This area is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    try:
        channel_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid channel selection.", show_alert=True)
        return
    ensure_official_channels_table()
    with engine.begin() as connection:
        row = connection.execute(text("SELECT username FROM official_channels WHERE id=:id"), {"id": channel_id}).first()
        if not row:
            await query.answer("Channel was already removed.", show_alert=True)
            return
        username = str(row[0])
        connection.execute(text("DELETE FROM official_channels WHERE id=:id"), {"id": channel_id})
    await query.edit_message_text(f"🗑️ <b>@{username} removed</b>\n\nAutomatic decline is no longer active for this saved channel.", parse_mode="HTML", reply_markup=official_channels_keyboard(_list_channels()))


async def official_channel_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not _admin_allowed(update):
        await query.answer("This area is restricted to administrators.", show_alert=True)
        return ConversationHandler.END
    await query.answer("Cancelled")
    await query.edit_message_text("🛡️ <b>Official Channel Auto-Decline</b>\n\nNo changes were made.", parse_mode="HTML", reply_markup=official_channels_keyboard(_list_channels()))
    return ConversationHandler.END


async def official_channel_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _admin_allowed(update):
        return ConversationHandler.END
    await update.message.reply_text("Cancelled. No channel was added.", reply_markup=admin_back_keyboard())
    return ConversationHandler.END


def build_conversation() -> ConversationHandler:
    return ConversationHandler(entry_points=[CallbackQueryHandler(official_channel_add, pattern=r"^official_channel_add$")], states={WAITING_OFFICIAL_CHANNEL: [CallbackQueryHandler(official_channel_cancel, pattern=r"^official_channel_cancel$"), CommandHandler("cancel", official_channel_cancel_command), MessageHandler(filters.TEXT & ~filters.COMMAND, official_channel_save)]}, fallbacks=[CallbackQueryHandler(official_channel_cancel, pattern=r"^official_channel_cancel$"), CommandHandler("cancel", official_channel_cancel_command)], name="official_channel_conversation")


def register(application) -> None:
    ensure_official_channels_table()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, official_channel_guard), group=-3)
    application.add_handler(build_conversation())
    application.add_handler(CallbackQueryHandler(official_channels_page, pattern=r"^admin_official_channels$"))
    application.add_handler(CallbackQueryHandler(official_channel_remove, pattern=r"^official_channel_remove:\d+$"))
