import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# .env and data.db live here; defaults to the install directory. Tests point it elsewhere.
DATA_DIR = os.getenv("VPS_PANEL_DATA_DIR") or BASE_DIR
ENV_PATH = os.path.join(DATA_DIR, ".env")
load_dotenv(ENV_PATH)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 8088))
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")
INTERFACE = os.getenv("INTERFACE", "")
PANEL_TITLE = os.getenv("PANEL_TITLE", "VPS 監控流量面板")
PANEL_SUBTITLE = os.getenv("PANEL_SUBTITLE", "Tim哥在三更半夜改好的")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "0")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
try:
    TELEGRAM_PUSH_HOUR = int(os.getenv("TELEGRAM_PUSH_HOUR", "20"))
except ValueError:
    TELEGRAM_PUSH_HOUR = 20
TELEGRAM_PUSH_HOUR = max(0, min(23, TELEGRAM_PUSH_HOUR))
try:
    TELEGRAM_PUSH_MINUTE = int(os.getenv("TELEGRAM_PUSH_MINUTE", "0"))
except ValueError:
    TELEGRAM_PUSH_MINUTE = 0
TELEGRAM_PUSH_MINUTE = max(0, min(59, TELEGRAM_PUSH_MINUTE))
try:
    TELEGRAM_TIMEZONE_OFFSET = int(os.getenv("TELEGRAM_TIMEZONE_OFFSET", "8"))
except ValueError:
    TELEGRAM_TIMEZONE_OFFSET = 8
TELEGRAM_TIMEZONE_OFFSET = max(-12, min(14, TELEGRAM_TIMEZONE_OFFSET))
TELEGRAM_TIMEZONE_LABEL = os.getenv("TELEGRAM_TIMEZONE_LABEL", "中國時間")
BANDWAGON_VEID = os.getenv("BANDWAGON_VEID", "")
BANDWAGON_API_KEY = os.getenv("BANDWAGON_API_KEY", "")
try:
    TRAFFIC_QUOTA_BYTES = int(os.getenv("TRAFFIC_QUOTA_BYTES", "0"))
except ValueError:
    TRAFFIC_QUOTA_BYTES = 0
TRAFFIC_QUOTA_BYTES = max(0, TRAFFIC_QUOTA_BYTES)
CLOUDFLARE_DDNS_ENABLED = os.getenv("CLOUDFLARE_DDNS_ENABLED", "0")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID", "")
CLOUDFLARE_RECORD_NAME = os.getenv("CLOUDFLARE_RECORD_NAME", "")
CLOUDFLARE_PROXIED = os.getenv("CLOUDFLARE_PROXIED", "0")
try:
    MONTH_RESET_DAY = int(os.getenv("MONTH_RESET_DAY", "1"))
except ValueError:
    MONTH_RESET_DAY = 1
MONTH_RESET_DAY = max(1, min(31, MONTH_RESET_DAY))
