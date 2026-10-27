"""
win_agent.py — Локальный агент для Windows (Windows 10/11 ПК и ноутбуки).
- Папка проектов: PROJECTS_DIR (C:\\Users\\<user>\\Projects или DOWNLOADS_DIR)
- Низкий приоритет процесса для энергосбережения
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

from bot.platform_utils import IS_WINDOWS, LOGS_DIR, ensure_utf8_console, PROJECTS_DIR
from bot.agent_core import run_agent

LOGS_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = LOGS_DIR / "agent.pid"

if sys.stdout is None:
    try:
        sys.stdout = open(LOGS_DIR / "agent_stdout.log", "a", encoding="utf-8")
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(LOGS_DIR / "agent_stderr.log", "a", encoding="utf-8")
    except Exception:
        pass

ensure_utf8_console()

log_handlers = [logging.FileHandler(LOGS_DIR / "agent.log", encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=log_handlers,
)

logger = logging.getLogger("WindowsAgent")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

WIN_PROJECTS_DIR = PROJECTS_DIR
WIN_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def handle_cli() -> bool:
    """Служебные режимы .exe. True — команда выполнена, агент запускать не нужно."""
    args = set(sys.argv[1:])
    if "--selftest" in args:
        # Проверка сборки: все модули импортируются, каталоги доступны. У .exe без консоли результат — код выхода
        from bot import AGENT_VERSION
        from bot.platform_utils import APP_DIR, ENV_FILE, IS_FROZEN
        try:
            import bot.runner_service  # noqa: F401
            import bot.first_run  # noqa: F401
            import tkinter  # noqa: F401
            import py7zr  # noqa: F401
        except Exception as e:
            logger.error(f"selftest: {e!r}")
            sys.exit(1)
        msg = f"SecondBrainAgent {AGENT_VERSION} | frozen={IS_FROZEN} | data={APP_DIR} | settings={ENV_FILE} exists={ENV_FILE.exists()}"
        logger.info(f"selftest OK: {msg}")
        print(msg)
        sys.exit(0)
    if "--install-autostart" in args:
        from bot.platform_utils import install_autostart
        print("autostart:", "installed" if install_autostart() else "FAILED")
        return True
    if "--remove-autostart" in args:
        from bot.platform_utils import remove_autostart
        print("autostart:", "removed" if remove_autostart() else "FAILED")
        return True
    if "--pair" in args:
        # Подключение без окна: --pair 123456 [--name "Рабочий ПК"]
        from bot import device_link
        argv = sys.argv[1:]
        code = argv[argv.index("--pair") + 1] if argv.index("--pair") + 1 < len(argv) else ""
        name = argv[argv.index("--name") + 1] if "--name" in argv and argv.index("--name") + 1 < len(argv) else ""
        try:
            data = device_link.pair(code, name)
            print(f"paired: {data['device_id']} ({device_link.DEVICE_NAME})")
        except device_link.PairError as e:
            print(f"pair FAILED: {e}")
            sys.exit(1)
        return True
    if "--unpair" in args:
        from bot import device_link
        device_link.forget_locally()
        print("unpaired")
        return True
    return False


def ensure_connected() -> bool:
    """Агенту нужна либо привязка по коду, либо настройки владельца (AGENT_SECRET). Иначе — окно первого запуска."""
    from bot import device_link
    from bot.agent_core import AGENT_SECRET
    if device_link.is_paired() or AGENT_SECRET:
        return True
    if not IS_WINDOWS:
        logger.warning("Компьютер не подключён: запустите агент с параметром --pair <код из бота>.")
        return False
    try:
        from bot.first_run import run_first_run_window
        paired = run_first_run_window()
    except Exception as e:
        logger.error(f"Окно подключения не открылось: {e!r}")
        return False
    if not paired:
        logger.info("Подключение отменено пользователем — агент не запускается.")
    return paired


def main():
    if handle_cli():
        return
    from bot.platform_utils import acquire_single_instance
    instance_lock = acquire_single_instance()
    if instance_lock is None:
        logger.info("Агент уже запущен — второй экземпляр не стартует.")
        return
    if not ensure_connected():
        return
    if IS_WINDOWS:
        try:
            import ctypes
            # Устанавливаем приоритет BELOW_NORMAL_PRIORITY_CLASS (0x00004000) для фонового режима
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                0x00004000
            )
        except Exception:
            pass

    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    try:
        asyncio.run(run_agent(
            current_platform="windows",
            device_label="Windows ПК",
            downloads_dir=WIN_PROJECTS_DIR,
            logger=logger
        ))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Агент Windows остановлен.")
    except Exception:
        import traceback
        err = traceback.format_exc()
        logger.critical(f"Критическая ошибка WindowsAgent: {err}")
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
