"""App settings. Values come from .env so secrets never live in code."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "restaurant.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
RESTAURANT_NAME = os.getenv("RESTAURANT_NAME", "Demo Restaurant")
GST_PERCENT = int(os.getenv("GST_PERCENT", "5"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
# A business day runs from this hour to the same hour next day, so late-night service
# (orders after midnight) belongs to the evening it started in
BUSINESS_DAY_START_HOUR = int(os.getenv("BUSINESS_DAY_START_HOUR", "4"))

# Floor timers: minutes before a table/item turns red
WARN_NO_ORDER_MIN = int(os.getenv("WARN_NO_ORDER_MIN", "10"))
WARN_FOOD_WAITING_MIN = int(os.getenv("WARN_FOOD_WAITING_MIN", "5"))
WARN_KITCHEN_MIN = int(os.getenv("WARN_KITCHEN_MIN", "20"))
