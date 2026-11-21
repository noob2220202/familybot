import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = int(os.getenv("GROUP_ID") or 0)

MINIAPP_URL = os.getenv("MINIAPP_URL", "").rstrip("/")
ADMIN_URL = os.getenv("ADMIN_URL", "").rstrip("/")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = hashlib.sha256(f"{BOT_TOKEN}:{ADMIN_PASSWORD}".encode()).digest()

SPORTS_API_KEY = os.getenv("SPORTS_API_KEY", "")

HOST = os.getenv("HOST", "127.0.0.1")
MINIAPP_PORT = int(os.getenv("MINIAPP_PORT") or 9200)
ADMIN_PORT = int(os.getenv("ADMIN_PORT") or 9201)

DB_PATH = Path(os.getenv("DB_PATH") or ROOT / "data" / "familybot.db")
STATIC = Path(__file__).resolve().parent / "static"
TZ = ZoneInfo("Asia/Seoul")
