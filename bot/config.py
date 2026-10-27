import os
import logging
from pathlib import Path

# .env загружает platform_utils (первым при любом запуске): из папки проекта или, у собранного .exe, из профиля пользователя
from bot.platform_utils import APP_DIR

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

raw_allowed_users = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = set()
for uid in raw_allowed_users.split(","):
    cleaned = uid.strip()
    if cleaned.isdigit():
        ALLOWED_USER_IDS.add(int(cleaned))

if not ALLOWED_USER_IDS:
    logger.warning("ALLOWED_USER_IDS пуст! Бот запущен в режиме fail-closed (все действия заблокированы).")

DIGEST_DAY_OF_WEEK = os.getenv("DIGEST_DAY_OF_WEEK", "sun").strip().lower()
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", 20))
DIGEST_MINUTE = int(os.getenv("DIGEST_MINUTE", 0))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/second_brain.db").strip()
db_dir = Path(DATABASE_PATH).parent
if db_dir and not db_dir.is_absolute():
    db_full_path = APP_DIR / DATABASE_PATH
    db_full_path.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_PATH = str(db_full_path)

# Каталог для автоматического сохранения скачанных проектов (Windows: C:\Users\<user>\Projects)
from bot.platform_utils import PROJECTS_DIR, SCRATCH_DIR, QUEUE_FILE

DOWNLOADS_DIR = PROJECTS_DIR
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
