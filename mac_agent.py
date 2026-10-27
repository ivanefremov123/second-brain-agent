"""
mac_agent.py — Локальный агент для macOS (с автоматическим делегированием на WindowsAgent при запуске на Windows).
- Папка проектов: ~/Projects
- Управление питанием: caffeinate
- Подавление спама httpx/httpcore
- Без эмодзи
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from bot.platform_utils import IS_MAC, IS_WINDOWS, LOGS_DIR
from bot.config import DOWNLOADS_DIR
from bot.agent_core import run_agent, send_telegram_card  # noqa: F401

LOGS_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = LOGS_DIR / "agent.pid"

log_handlers = [logging.FileHandler(LOGS_DIR / "agent.log", encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=log_handlers,
)

logger = logging.getLogger("MacAgent")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAC_PROJECTS_DIR = Path.home() / "Projects"
if not MAC_PROJECTS_DIR.exists():
    try:
        MAC_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        MAC_PROJECTS_DIR = DOWNLOADS_DIR


def main():
    if IS_WINDOWS:
        logger.info("Обнаружена Windows: запуск WindowsAgent вместо MacAgent.")
        import win_agent
        win_agent.main()
        return

    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    try:
        asyncio.run(run_agent(
            current_platform="mac",
            device_label="Mac",
            downloads_dir=MAC_PROJECTS_DIR,
            logger=logger
        ))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Агент Mac остановлен.")
    except Exception:
        import traceback
        err = traceback.format_exc()
        logger.critical(f"Критическая ошибка MacAgent: {err}")
        try:
            (LOGS_DIR / "crash.log").write_text(err, encoding="utf-8")
        except Exception:
            pass
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
