"""
Run this ONCE, locally, on your own machine -- not on Railway.

It logs a regular Telegram user account into Telethon interactively
(phone number + login code, and 2FA password if you have one enabled)
and prints a session string. Paste that string into the TELETHON_SESSION_STRING
environment variable on Railway (or wherever you deploy).

Use a real personal or dedicated account here, NOT the bot's token --
bot accounts cannot read message history for channels they haven't joined,
which is required to verify applicants' channels.

Usage:
    pip install telethon python-dotenv
    python generate_session.py
"""
from telethon import TelegramClient
from telethon.sessions import StringSession
import os

from dotenv import load_dotenv

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
    print("\n" + "=" * 60)
    print("Login successful. Your session string is below.")
    print("Copy it into TELETHON_SESSION_STRING on your deployment platform.")
    print("Treat it like a password -- anyone with it can access this account.")
    print("=" * 60 + "\n")
    print(session_string)
    print()
