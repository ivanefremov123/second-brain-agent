"""
Кроссплатформенный слой: определение ОС и единые пути (Windows / macOS / Linux).

Все модули должны брать пути отсюда, а не хардкодить /Users/<name>/...
Переопределяются через переменные окружения (.env):
  DOWNLOADS_DIR  - куда скачиваются проекты (по умолчанию ~/Projects)
  SCRATCH_DIR    - где лежат antigravity_memory.json и очередь задач
"""
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../telegram-second-brain
HOME = Path.home()

# Собранный агент (PyInstaller .exe) не может писать рядом со своим кодом — его данные живут в профиле пользователя
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _user_app_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.getenv("APPDATA") or (HOME / "AppData" / "Roaming"))
    elif IS_MAC:
        base = HOME / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or (HOME / ".config"))
    return base / "SecondBrain"


# Настройки, логи и служебные файлы агента
APP_DIR = _user_app_dir() if IS_FROZEN else PROJECT_ROOT
APP_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = APP_DIR / ("agent.env" if IS_FROZEN else ".env")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ENV_FILE)
except ImportError:
    pass

# Каталог скачанных проектов: Windows -> C:\Users\<user>\Projects, Mac -> ~/Projects
PROJECTS_DIR = Path(os.getenv("DOWNLOADS_DIR") or (HOME / "Projects")).expanduser()

# Каталог, где лежат antigravity_memory.json / antigravity_tasks_queue.json
SCRATCH_DIR = Path(os.getenv("SCRATCH_DIR") or (APP_DIR if IS_FROZEN else PROJECT_ROOT.parent)).expanduser()

JSON_MEMORY_FILE = SCRATCH_DIR / "antigravity_memory.json"
QUEUE_FILE = SCRATCH_DIR / "antigravity_tasks_queue.json"

# Папка с логами агента
LOGS_DIR = APP_DIR / "logs"


ARCHIVE_SUFFIXES = (".zip", ".7z", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")
_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def extract_archive(path: Path, dest: Path) -> bool:
    """Распаковка zip / 7z / tar.* без выхода за пределы dest. False — не архив или повреждён."""
    import tarfile
    import zipfile
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def _inside(member: str) -> bool:
        target = (root / member).resolve()
        return target == root or root in target.parents

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                if not all(_inside(n) for n in zf.namelist()):
                    return False
                zf.extractall(dest)
            return True
        with open(path, "rb") as f:
            is_7z = f.read(6) == _7Z_MAGIC
        if is_7z:
            import py7zr
            with py7zr.SevenZipFile(path, "r") as z:
                if not all(_inside(n) for n in z.getnames()):
                    return False
                z.extractall(path=dest)
            return True
        if tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as tf:
                tf.extractall(dest, filter="data")  # отбрасывает абсолютные пути, ../ и опасные ссылки
            return True
    except Exception:
        return False
    return False


def project_path(name: str) -> str:
    """Локальный путь проекта по имени (строка, для совместимости с прежним кодом)."""
    return str(PROJECTS_DIR / name)


def ensure_utf8_console() -> None:
    """Windows-консоль по умолчанию cp1251/cp866 - принудительно UTF-8, чтобы не падал print с эмодзи."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Устройство (для выбора машины в Telegram: Mac / ПК / Ноутбук)
# ---------------------------------------------------------------------------
import platform as _platform
import re as _re

OS_NAME = "Windows" if IS_WINDOWS else ("macOS" if IS_MAC else "Linux")
PLATFORM_LABEL = "Windows ПК" if IS_WINDOWS else "Mac"
OS_ICON = "" if IS_WINDOWS else ("" if IS_MAC else "")


def _default_device_id() -> str:
    raw = os.getenv("DEVICE_ID") or _platform.node() or OS_NAME
    return _re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower() or "device"


DEVICE_ID = _default_device_id()                      # технический id: ключ в kvdb
DEVICE_NAME = os.getenv("DEVICE_NAME") or _platform.node() or DEVICE_ID  # имя для кнопок


def venv_python(root: Path = PROJECT_ROOT) -> Path:
    """Путь к python внутри .venv: Windows -> .venv\\Scripts\\python.exe, Mac/Linux -> .venv/bin/python."""
    if IS_WINDOWS:
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# Не давать системе засыпать (caffeinate на Mac / SetThreadExecutionState на Windows)
# ---------------------------------------------------------------------------
import contextlib as _contextlib
import threading as _threading

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_awake_lock = _threading.Lock()
_awake_count = 0
_awake_stop = None


def _win_awake_worker(stop_evt):
    """SetThreadExecutionState действует на поток - держим его живым в отдельном потоке."""
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    stop_evt.wait()
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


@_contextlib.contextmanager
def keep_awake():
    """Контекст: пока внутри - ПК не уснёт (Windows). На Mac используйте wrap_no_sleep()/caffeinate."""
    global _awake_count, _awake_stop
    if not IS_WINDOWS:
        yield
        return
    with _awake_lock:
        _awake_count += 1
        if _awake_count == 1:
            _awake_stop = _threading.Event()
            _threading.Thread(target=_win_awake_worker, args=(_awake_stop,), daemon=True).start()
    try:
        yield
    finally:
        with _awake_lock:
            _awake_count -= 1
            if _awake_count == 0 and _awake_stop:
                _awake_stop.set()
                _awake_stop = None


def wrap_no_sleep(cmd: list, flags: str = "-dims") -> list:
    """Mac: добавляет префикс caffeinate. Windows/Linux: команда без изменений (сон держит keep_awake())."""
    if IS_MAC:
        return ["/usr/bin/caffeinate", flags] + list(cmd)
    return list(cmd)


# ---------------------------------------------------------------------------
# Кроссплатформенное управление экраном и приложениями (Explorer / Finder, Browser, Terminal)
# ---------------------------------------------------------------------------
import subprocess as _subprocess


def get_windows_browser_path() -> str | None:
    """Возвращает путь к реальному браузеру (Chrome, Edge, Firefox, Brave) на Windows."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def kill_process_by_name(exe_name: str) -> bool:
    """
    Принудительно завершает старые зависшие копии процесса перед новым запуском
    через taskkill /F /IM <name>.exe, чтобы не блокировался single-instance.
    """
    if not IS_WINDOWS or not exe_name:
        return False
    name = exe_name if exe_name.lower().endswith(".exe") else f"{exe_name}.exe"
    try:
        res = _subprocess.run(
            ["taskkill", "/F", "/T", "/IM", name],
            capture_output=True,
            check=False,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        return res.returncode == 0
    except Exception:
        return False


def launch_gui_executable(exe_path: Path | str) -> bool:
    """
    Интерактивный запуск десктопных GUI-программ (.exe) на физическом рабочем столе Windows (WinSta0\\Default):
    1. Завершает старые зависшие копии этого процесса через taskkill /F /IM <name>.exe.
    2. Запускает .exe через subprocess.Popen с startupinfo.lpDesktop = WinSta0\\Default и флагом DETACHED_PROCESS.
    Гарантирует отрисовку окна на реальном мониторе пользователя, а не на виртуальном столе IDE.
    """
    p = Path(exe_path)
    if not p.exists():
        return False

    if IS_WINDOWS:
        # 1. Защита от дублей и зависших копий
        kill_process_by_name(p.name)

        # 2. Интерактивный запуск на физическом экране WinSta0\Default
        try:
            si = _subprocess.STARTUPINFO()
            si.lpDesktop = r"WinSta0\Default"
            detached = getattr(_subprocess, "DETACHED_PROCESS", 0x00000008)
            _subprocess.Popen(
                [str(p)],
                cwd=str(p.parent),
                startupinfo=si,
                creationflags=detached
            )
            return True
        except Exception:
            pass

        # 3. Резерв: os.startfile
        try:
            os.startfile(str(p))
            return True
        except Exception:
            pass
        return False

    elif IS_MAC:
        try:
            _subprocess.run(["open", str(p)], check=False)
            return True
        except Exception:
            return False
    else:
        try:
            _subprocess.Popen([str(p)], cwd=str(p.parent))
            return True
        except Exception:
            return False


def open_directory(path: Path | str) -> bool:
    """
    Открывает папку в системном проводнике на физическом рабочем столе пользователя WinSta0\\Default.
    Запускает explorer.exe через subprocess.Popen с startupinfo (lpDesktop = r"WinSta0\\Default"),
    чтобы папка мгновенно всплывала на экране.
    """
    target = str(path)
    if IS_WINDOWS:
        # 1. Запуск explorer.exe с принудительным выводом на физический рабочий стол WinSta0\Default
        try:
            si = _subprocess.STARTUPINFO()
            si.lpDesktop = r"WinSta0\Default"
            detached = getattr(_subprocess, "DETACHED_PROCESS", 0x00000008)
            _subprocess.Popen(["explorer.exe", target], startupinfo=si, creationflags=detached)
            return True
        except Exception:
            pass

        # 2. Резерв: os.startfile
        try:
            os.startfile(target)
            return True
        except Exception:
            pass
    elif IS_MAC:
        try:
            _subprocess.run(["open", target], check=False)
            return True
        except Exception:
            pass
    else:
        try:
            _subprocess.run(["xdg-open", target], check=False)
            return True
        except Exception:
            pass
    return False


def open_url(url: str) -> bool:
    """Открывает веб-страницу, ссылку или локальный HTML файл на рабочем столе пользователя."""
    target = str(url).strip()
    if not target:
        return False

    if IS_WINDOWS:
        # 1. os.startfile — нативный системный вызов Windows ShellExecute.
        # Для http/https и локальных .html файлов гарантированно открывает браузер по умолчанию и выводит его на передний план.
        try:
            import os
            os.startfile(target)
            return True
        except Exception:
            pass

        # 2. Если это локальный файл на диске, формируем file:/// URI
        file_target = target
        try:
            p_obj = Path(target)
            if p_obj.exists():
                file_target = p_obj.resolve().as_uri()
        except Exception:
            pass

        # 3. Запуск через реальный установленный браузер (Chrome / Edge / Firefox)
        browser = get_windows_browser_path()
        if browser:
            try:
                _subprocess.Popen([browser, file_target])
                return True
            except Exception:
                pass

        # 4. powershell Start-Process
        try:
            escaped = file_target.replace("'", "''")
            _subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{escaped}'"],
                creationflags=0x08000000
            )
            return True
        except Exception:
            pass

        # 5. webbrowser.open
        try:
            import webbrowser
            if webbrowser.open(file_target):
                return True
        except Exception:
            pass

        # 6. Fallback cmd start
        try:
            _subprocess.Popen(
                f'cmd.exe /c start "" "{file_target}"',
                shell=True,
                creationflags=0x08000000
            )
            return True
        except Exception:
            pass

    try:
        import webbrowser
        if webbrowser.open(target):
            return True
    except Exception:
        pass
    if IS_MAC:
        try:
            _subprocess.run(["open", target], check=False)
            return True
        except Exception:
            pass
    return False


def launch_in_terminal(cmd: str, working_dir: Path | str) -> bool:
    """
    Запускает команду в отдельном интерактивном окне терминала на экране:
    Windows: Новое видимое окно PowerShell с флагом CREATE_NEW_CONSOLE (0x00000010) и авто-активацией .venv.
    macOS: AppleScript открывает новое окно Terminal.app с авто-активацией .venv.
    """
    w_dir = str(working_dir)
    p = Path(w_dir)
    venv_dir = p / ".venv"
    venv_bin = venv_dir / ("Scripts" if IS_WINDOWS else "bin")
    title = p.name or "Terminal"

    clean_cmd = (cmd or "").strip()
    is_interactive_prompt = not clean_cmd or clean_cmd in ("ls -la", "ls", "dir")

    try:
        if IS_WINDOWS:
            create_new_console = 0x00000010
            escaped_dir = w_dir.replace("'", "''")
            
            if is_interactive_prompt:
                ps_cmd = (
                    f"Set-Location -LiteralPath '{escaped_dir}'; "
                    f"if (Test-Path .\\.venv\\Scripts\\Activate.ps1) {{ & .\\.venv\\Scripts\\Activate.ps1 }}; "
                    f"Write-Host 'Окружение активировано. Проект готов к работе.' -ForegroundColor Green; "
                    f"Write-Host '[Second Brain] Папка: {escaped_dir}' -ForegroundColor Cyan; "
                    f"Get-ChildItem"
                )
            else:
                escaped_run = clean_cmd.replace("'", "''")
                ps_cmd = (
                    f"Set-Location -LiteralPath '{escaped_dir}'; "
                    f"if (Test-Path .\\.venv\\Scripts\\Activate.ps1) {{ & .\\.venv\\Scripts\\Activate.ps1 }}; "
                    f"Write-Host '[Second Brain] Запуск: {clean_cmd}' -ForegroundColor Cyan; "
                    f"{clean_cmd}"
                )

            si = _subprocess.STARTUPINFO()
            si.lpDesktop = r"WinSta0\Default"
            _subprocess.Popen(
                ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                cwd=w_dir,
                startupinfo=si,
                creationflags=create_new_console
            )
            return True
        elif IS_MAC:
            cmd_to_run = clean_cmd if not is_interactive_prompt else "ls -la"
            act_sh = venv_bin / "activate"
            if act_sh.exists():
                full_script = f'cd \\"{w_dir}\\" && source \\"{act_sh}\\" && {cmd_to_run}'
            else:
                full_script = f'cd \\"{w_dir}\\" && {cmd_to_run}'
            applescript = f'''
tell application "Terminal"
    activate
    do script "{full_script}"
end tell
'''
            _subprocess.run(["osascript", "-e", applescript], check=False)
            return True
        return False
    except Exception:
        return False




# --- Автозапуск собранного агента (SecondBrainAgent.exe) ---
STARTUP_SHORTCUT_NAME = "SecondBrainAgent.lnk"


def _startup_dir() -> Path:
    return Path(os.getenv("APPDATA") or (HOME / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_autostart() -> bool:
    """Ярлык в «Автозагрузке» Windows: агент стартует вместе с системой. Только с согласия пользователя."""
    if not IS_WINDOWS:
        return False
    if IS_FROZEN:
        target, args, workdir = Path(sys.executable), "", Path(sys.executable).parent
    else:
        target, args, workdir = venv_python(PROJECT_ROOT).with_name("pythonw.exe"), "win_agent.py", PROJECT_ROOT
    env = dict(os.environ, SB_LNK=str(_startup_dir() / STARTUP_SHORTCUT_NAME), SB_TARGET=str(target), SB_ARGS=args, SB_DIR=str(workdir))
    # Пути передаются через переменные окружения, а не подставляются в текст команды
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SB_LNK);"
        "$s.TargetPath=$env:SB_TARGET;$s.Arguments=$env:SB_ARGS;$s.WorkingDirectory=$env:SB_DIR;$s.Save()"
    )
    try:
        _subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, capture_output=True, timeout=30, check=True)
        return (_startup_dir() / STARTUP_SHORTCUT_NAME).exists()
    except Exception:
        return False


def remove_autostart() -> bool:
    try:
        (_startup_dir() / STARTUP_SHORTCUT_NAME).unlink(missing_ok=True)
        return True
    except Exception:
        return False


def acquire_single_instance(name: str = "SecondBrainAgent"):
    """Именованный мьютекс Windows: второй экземпляр агента не запускается. None — агент уже работает."""
    if not IS_WINDOWS:
        return object()
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle
