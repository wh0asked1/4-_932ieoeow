import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot Configuration ─────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# Slash command sync
GUILD_ID = os.getenv("GUILD_ID") or None

# 🔓 Gate removed — empty list means everyone can use it
OWNER_IDS = [] 

# ── Quest Defaults ────────────────────────────────────────────────────────────
DEFAULT_USER_TOKEN = os.getenv("DEFAULT_USER_TOKEN", "")
DEFAULT_BUILD_NUMBER = int(os.getenv("DEFAULT_BUILD_NUMBER", "504649"))

API_BASE = "https://discord.com/api/v9"
POLL_INTERVAL = 60
HEARTBEAT_INTERVAL = 20
AUTO_ACCEPT = True
DEBUG = False

SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE",
]
