# bot/config.py
import os
from dotenv import load_dotenv

# Load env from .env + systemd
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "change-me")

# Channels (IDs preferred)
SERVERCONTROL_CHANNEL_ID = os.getenv("SERVERCONTROL_CHANNEL_ID")
HEARTBEAT_CHANNEL_ID = os.getenv("HEARTBEAT_CHANNEL_ID")

# Optional fallback names
SERVERCONTROL_CHANNEL_NAME = os.getenv("SERVERCONTROL_CHANNEL_NAME", "servercontrol")
HEARTBEAT_CHANNEL_NAME = os.getenv("HEARTBEAT_CHANNEL_NAME", "heartbeat")

# Guild for fast slash-command sync
PRIMARY_GUILD_ID = os.getenv("PRIMARY_GUILD_ID")

# How long the bot waits for /chat. MUST exceed the API's GEN_TIMEOUT, or the
# bot gives up first and the player sees a raw connection error instead of the
# API's in-world stall message.
#
# Local generation on this host is slow: a canon-sized prompt costs ~57s of
# prompt evaluation before the first output token, plus ~47s for 512 tokens.
# Raise both this and GEN_TIMEOUT together, or lower MAX_OUTPUT_TOKENS.
CHAT_TIMEOUT = int(os.getenv("BOT_CHAT_TIMEOUT", "240"))

# Mature Data Tracking
MATURE_ROLE_NAME = os.getenv("MATURE_ROLE_NAME", "18plus")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in environment or .env")


def get_headers() -> dict:
    return {"Authorization": f"Bearer {BACKEND_API_KEY}"}
