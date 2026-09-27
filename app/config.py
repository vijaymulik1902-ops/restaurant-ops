"""App settings. Values come from .env so secrets never live in code."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "restaurant.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
# Send the session cookie over HTTPS only. Keep false for plain-HTTP LAN use; true in production.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")
RESTAURANT_NAME = os.getenv("RESTAURANT_NAME", "Demo Restaurant")
GST_PERCENT = int(os.getenv("GST_PERCENT", "5"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
# A business day runs from this hour to the same hour next day, so late-night service
# (orders after midnight) belongs to the evening it started in
BUSINESS_DAY_START_HOUR = int(os.getenv("BUSINESS_DAY_START_HOUR", "4"))

# AI chat on /insights (Google Gemini REST API). Empty key = chat disabled, insight cards still work.
# Default model: gemini-3.8-flash, the latest stable Flash model; free tier; supports function calling.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
# Used for the same step when the primary model is overloaded (503) or rate-limited (429):
# gemini-3.5-flash-lite is a lighter stable model that supports function calling.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()

# Floor timers: minutes before a table/item turns red
WARN_NO_ORDER_MIN = int(os.getenv("WARN_NO_ORDER_MIN", "10"))
WARN_FOOD_WAITING_MIN = int(os.getenv("WARN_FOOD_WAITING_MIN", "5"))
WARN_KITCHEN_MIN = int(os.getenv("WARN_KITCHEN_MIN", "20"))
