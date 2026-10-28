import asyncio
import json
import logging
import os
import psutil
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, List

import httpx

from bot.ai_service import call_gemini_with_fallback
from bot.config import DOWNLOADS_DIR, QUEUE_FILE
from bot.platform_utils import (
    IS_MAC, IS_WINDOWS, OS_NAME, LOGS_DIR,
    open_directory, open_url, launch_in_terminal,
    kill_process_by_name, launch_gui_executable, extract_archive
)
from bot.github_service import prevent_mac_sleep
from bot.registry_service import unregister_installed_tool
from bot.memory_service import remove_from_antigravity_memory

logger = logging.getLogger(__name__)


def is_safe_project_path(target_path: Path, base_dir: Path = DOWNLOADS_DIR) -> bool:
    """Проверяет, что целевой путь находится строго внутри допустимых каталогов проектов и не равен им."""
    try:
        resolved_target = Path(target_path).resolve()
        allowed_bases = {Path(base_dir).resolve(), (Path.home() / "Projects").resolve()}
        if IS_WINDOWS:
            allowed_bases.add(Path("C:/Projects").resolve())
        for resolved_base in allowed_bases:
            if resolved_target.is_relative_to(resolved_base) and resolved_target != resolved_base:
                return True
        return False
    except Exception:
        return False


def is_windows_console_exe(exe_path: Path) -> bool:
    """
    Определяет по PE-заголовку Windows (.exe), является ли бинарник консольной утилитой (CUI, Subsystem == 3)
    или оконным GUI-приложением (GUI, Subsystem == 2).
    """
    try:
        with open(exe_path, "rb") as f:
            dos = f.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return False
            e_lfanew = struct.unpack("<I", dos[0x3C:0x40])[0]
            f.seek(e_lfanew)
            pe_hdr = f.read(0x60)
            if len(pe_hdr) < 0x5E or pe_hdr[:4] != b"PE\x00\x00":
                return False
            subsystem = struct.unpack("<H", pe_hdr[0x5C:0x5E])[0]
            return subsystem == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI
    except Exception:
        return False

_PE_X86, _PE_AMD64, _PE_ARM64, _PE_ARMNT = 0x014C, 0x8664, 0xAA64, 0x01C4


def exe_runs_here(exe_path: Path) -> bool:
    """По полю Machine PE-заголовка: ARM-сборки на обычном x64 ПК не запускаются («не является приложением Win32»)."""
    import platform as _platform
    if _platform.machine().lower() not in ("amd64", "x86_64", "x86"):
        return True  # на ARM64 Windows x64/x86 работают через эмуляцию
    try:
        with open(exe_path, "rb") as f:
            dos = f.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return True
            f.seek(struct.unpack("<I", dos[0x3C:0x40])[0])
            hdr = f.read(6)
            if len(hdr) < 6 or hdr[:4] != b"PE\x00\x00":
                return True
            return struct.unpack("<H", hdr[4:6])[0] not in (_PE_ARM64, _PE_ARMNT)
    except Exception:
        return True


_FOREIGN_LOCALE_TOKENS = {
    "zh", "hans", "hant", "chs", "cht", "cn", "tw", "chinese",
    "ja", "jp", "japanese", "ko", "kr", "korean",
    "de", "fr", "es", "it", "pt", "br", "pl", "tr",
}

_OTHER_OS_RE = re.compile(r"(linux|ubuntu|debian|fedora|appimage|flatpak|macos|darwin|osx|android|freebsd|openbsd|choco)")
# Короткие слова сравниваем только целиком: «snap» не должно совпадать с «snapshot»
_OTHER_OS_TOKENS = {"snap", "mac", "deb", "rpm", "apple", "ios", "dmg", "apk", "musl"}
_X64_RE = re.compile(r"(x86[-_]?64|x64|amd64|win64|64[-_]?bit)")
_ARM_RE = re.compile(r"(arm[-_]?64|aarch[-_]?64|armhf|armv7)")
_ARM_TOKENS = {"arm", "armel"}
_X86_32_TOKENS = {"x86", "i386", "i686", "win32", "ia32", "32bit"}


def score_release_asset(filename: str, is_x64: bool = True) -> Optional[int]:
    """Насколько файл релиза подходит для запуска на этом Windows ПК. None — не подходит вовсе."""
    name = filename.lower()
    # Слова имени: «_» тоже разделитель (yt-dlp_x86.exe, bottom_i686-pc-windows-msvc.zip)
    tokens = set(re.split(r"[^a-z0-9]+", name))
    is_tar = name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2"))
    if not (name.endswith((".exe", ".msi", ".zip", ".7z")) or is_tar) or _OTHER_OS_RE.search(name) or tokens & _OTHER_OS_TOKENS:
        return None
    # tar-архивы почти всегда для Linux/macOS — берём только с явной пометкой Windows
    if is_tar and not ("windows" in name or any(t.startswith("win") for t in tokens)):
        return None
    score = 0
    if _X64_RE.search(name):
        score += 60 if is_x64 else -40
    elif _ARM_RE.search(name) or tokens & _ARM_TOKENS:
        score += -80 if is_x64 else 60
    elif tokens & _X86_32_TOKENS or re.search(r"32[-_]bit", name):
        score -= 20  # 32-битная сборка запустится, но 64-битная предпочтительнее
    if "windows" in name or any(t.startswith("win") for t in tokens):
        score += 20
    if name.endswith(".7z.exe") or "sfx" in tokens:
        score -= 25  # самораспаковывающийся архив: спрашивает, куда распаковать
    elif name.endswith(".exe"):
        score += 20
    elif name.endswith(".zip"):
        score += 15  # обычно portable-версия: запускается сразу, без мастера установки
    elif name.endswith(".7z"):
        score += 12  # тоже portable, агент распаковывает сам
    elif is_tar:
        score += 8
    if "setup" in name or "installer" in name or "install" in tokens:
        score -= 15  # установщик требует ручного прохождения мастера — берём, только если нет portable
    if "portable" in tokens:
        score += 10
    # Отладочные символы, исходники, заголовки — не программа
    if tokens & {"pdb", "symbols", "dbg", "debuginfo", "src", "source", "sources", "headers", "uninstall", "uninstaller"}:
        return None
    if "debug" in tokens:
        score -= 40
    if "cli" in tokens:
        score -= 15  # при наличии версии с окном предпочитаем её
    if "unsigned" in tokens:
        score -= 10
    if "msvc" in tokens:
        score += 5
    # Локализованные сборки (Notepad4_zh-Hant_x64.zip и т.п.): предпочитаем английскую/нейтральную
    if tokens & _FOREIGN_LOCALE_TOKENS:
        score -= 50
    if tokens & {"en", "english"}:
        score += 15
    if "avx512" in tokens:
        score -= 10
    return score


def rank_release_assets(items: List[str], is_x64: bool = True) -> List[str]:
    """Подходящие файлы релиза (URL или имена), лучшие первыми. При равных баллах — самое короткое,
    каноничное имя: yt-dlp.exe, а не yt-dlp_x86.exe."""
    scored = []
    for item in items:
        score = score_release_asset(item.rsplit("/", 1)[-1], is_x64)
        if score is not None and score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], len(x[1].rsplit("/", 1)[-1]), x[1]))
    return [item for _, item in scored]


_GH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


async def list_release_assets(owner: str, rname: str, client: httpx.AsyncClient) -> List[str]:
    """URL файлов последнего стабильного релиза; если его нет (только пре-релизы) — из списка всех релизов."""
    for page in (f"https://github.com/{owner}/{rname}/releases/latest", f"https://github.com/{owner}/{rname}/releases"):
        try:
            resp = await client.get(page)
        except Exception:
            continue
        if resp.status_code != 200 or (page.endswith("/latest") and "/releases/tag/" not in str(resp.url)):
            continue
        raw_html = resp.text
        # Ассеты GitHub подгружает отдельными фрагментами expanded_assets; для /latest берём только его фрагмент
        exp_links = re.findall(r'src=["\'](https://github\.com/[^/]+/[^/]+/releases/expanded_assets/[^"\']+)["\']', raw_html)
        for exp in exp_links[: (1 if page.endswith("/latest") else 3)]:
            try:
                r_exp = await client.get(exp)
                if r_exp.status_code == 200:
                    raw_html += "\n" + r_exp.text
            except Exception:
                pass
        links = re.findall(rf'href=["\'](/{re.escape(owner)}/{re.escape(rname)}/releases/download/[^"\']+)["\']', raw_html, re.IGNORECASE)
        if not links:
            links = re.findall(r'href=["\'](/[^"\']+/releases/download/[^"\']+)["\']', raw_html)
        if links:
            return list(dict.fromkeys(urllib.parse.urljoin("https://github.com", l) for l in links))
    return []


def _best_exe_in(root: Path, project_name: str) -> Optional[Path]:
    exes = [f for f in _iter_files(root, "*.exe", max_depth=4) if exe_runs_here(f) and not _has_marker(f.name, _JUNK_EXE_MARKERS, project_name)]
    if not exes:
        return None
    exes.sort(key=lambda f: _rank_exe(f, root, project_name), reverse=True)
    return exes[0]


async def download_github_release_binary(repo_url: str, target_dir: Path) -> Optional[Path]:
    """
    Скачивает готовую сборку под этот ПК из GitHub Releases (.exe / .msi / portable .zip).
    Архив принимается, только если внутри есть программа, которая запустится на этом процессоре;
    иначе пробуется следующий кандидат, а если подходящих нет — None (дальше будет git clone).
    """
    if not repo_url or "github.com" not in repo_url:
        return None
    m = re.search(r"github\.com/([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+)", repo_url)
    if not m:
        return None
    owner, rname = m.group(1), m.group(2).removesuffix(".git").rstrip("/")
    import platform as _platform
    is_x64 = _platform.machine().lower() in ("amd64", "x86_64", "x86")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True, headers=_GH_HEADERS) as client:
            assets = await list_release_assets(owner, rname, client)
            ranked = rank_release_assets(assets, is_x64)
            if ranked:
                logger.info(f"Кандидаты релиза {owner}/{rname}: " + ", ".join(u.rsplit("/", 1)[-1] for u in ranked[:4]))

            for url in ranked[:3]:
                fname = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0])
                local_dest = target_dir / fname
                if not (local_dest.exists() and local_dest.stat().st_size > 1000):
                    logger.info(f"Скачиваем официальный релиз {fname} из {url}...")
                    async with client.stream("GET", url) as stream_resp:
                        if stream_resp.status_code != 200:
                            continue
                        with open(local_dest, "wb") as f:
                            async for chunk in stream_resp.aiter_bytes(chunk_size=65536):
                                f.write(chunk)
                    logger.info(f"Релиз {fname} успешно сохранен в {local_dest}!")

                low = fname.lower()
                if low.endswith(".msi"):
                    return local_dest
                if low.endswith(".exe"):
                    if exe_runs_here(local_dest):
                        return local_dest
                    logger.info(f"{fname} собран для другого процессора, пробуем следующий файл")
                    local_dest.unlink(missing_ok=True)
                    continue

                # Архив (.zip / .7z / .tar.*): распаковываем во временную папку и проверяем, что там программа для этого ПК
                staging = target_dir / f".sb_unpack_{int(time.time() * 1000)}"
                if not await asyncio.to_thread(extract_archive, local_dest, staging):
                    logger.info(f"Архив {fname} не распаковался, пробуем следующий файл")
                    shutil.rmtree(staging, ignore_errors=True)
                    local_dest.unlink(missing_ok=True)
                    continue
                if not _best_exe_in(staging, rname):
                    logger.info(f"В архиве {fname} нет программы для этого ПК (данные/исходники?), пробуем следующий файл")
                    shutil.rmtree(staging, ignore_errors=True)
                    local_dest.unlink(missing_ok=True)
                    continue
                # Архив с единственной корневой папкой «разворачиваем», чтобы программа лежала прямо в папке проекта.
                # В непустую папку (там уже исходники проекта) — только в отдельную подпапку, ничего не перезаписывая.
                entries = list(staging.iterdir())
                src_root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
                occupied = [x for x in target_dir.iterdir() if x not in (local_dest, staging)]
                dest_root = target_dir
                if occupied:
                    dest_root = target_dir / Path(fname).stem
                    n = 1
                    while dest_root.exists():
                        n += 1
                        dest_root = target_dir / f"{Path(fname).stem}_{n}"
                    dest_root.mkdir()
                for item in src_root.iterdir():
                    shutil.move(str(item), str(dest_root / item.name))
                shutil.rmtree(staging, ignore_errors=True)
                local_dest.unlink(missing_ok=True)
                return _best_exe_in(dest_root, rname)
    except Exception as e:
        logger.warning(f"Ошибка при поиске/скачивании GitHub release: {e}")
    return None

# Хранилище запущенных пользователем фоновых процессов {pid: proc} и их метаданных
ACTIVE_PROCESSES: Dict[int, asyncio.subprocess.Process] = {}
ACTIVE_PROCESS_INFO: Dict[int, Dict[str, Any]] = {}

def get_mac_local_ip() -> str:
    """Возвращает локальный IP-адрес Mac в домашней сети Wi-Fi."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_process_listening_port(pid: int) -> int:
    """Определяет сетевой порт, который открыл запущенный процесс или его дочерние процессы."""
    try:
        if pid:
            main_proc = psutil.Process(pid)
            all_procs = [main_proc]
            try:
                all_procs += main_proc.children(recursive=True)
            except Exception:
                pass

            for proc_item in all_procs:
                try:
                    for conn in proc_item.connections(kind='inet'):
                        if conn.status == psutil.CONN_LISTEN and conn.laddr:
                            return int(conn.laddr.port)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    except Exception:
        pass

    # Fallback на системные утилиты netstat / lsof
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "LISTENING" in line and str(pid) in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        m = re.search(r":(\d+)$", parts[1])
                        if m:
                            return int(m.group(1))
        else:
            out = subprocess.check_output(["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL)
            matches = re.findall(r":(\d+)\s+\(LISTEN\)", out)
            if matches:
                return int(matches[0])
    except Exception:
        pass
    return 0

def is_online_web_service(url: str) -> bool:
    """Определяет, является ли URL ссылкой на живой онлайн веб-сервис/SaaS/демо в браузере."""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False
    low = url.lower().strip()
    if low.endswith(".git"):
        return False
    if "github.com" in low or "gitlab.com" in low:
        return ".github.io" in low
    return True

def classify_repository_type(local_path: str, readme_text: str = "", repo_url: str = "") -> str:
    """
    Классифицирует репозиторий на один из 5 типов:
    - 'docs': документация, базы знаний, промпты, датасеты
    - 'gui': десктопные оконные программы (.app, .dmg, .exe, tkinter, pyqt, pyside, electron)
    - 'web': веб-сервисы и приложения (онлайн-сервисы, streamlit, gradio, fastapi, flask, django, nextjs, vite, react)
    - 'cli': консольные утилиты (argparse, click, typer, entry points, исполняемые скрипты)
    - 'library': пакеты, библиотеки, SDK, MCP-серверы
    """
    p = Path(local_path)
    readme_l = (readme_text or "").lower()
    u_l = (repo_url or "").lower()

    # 0.1 Автоматическая проверка и конвертация .txt с HTML содержимым
    if p.exists() and p.is_dir():
        for txt in p.glob("*.txt"):
            try:
                head_sample = txt.read_text(encoding="utf-8", errors="ignore")[:300].lower()
                if "<!doctype html" in head_sample or "<html" in head_sample:
                    renamed_html = txt.with_suffix(".html")
                    txt.rename(renamed_html)
                    idx_html = p / "index.html"
                    if not idx_html.exists():
                        try:
                            shutil.copyfile(renamed_html, idx_html)
                        except Exception:
                            pass
                    break
            except Exception:
                pass

    # 0. Проверка на наличие исполняемых файлов или оконных приложений (.exe, .msi, .app, .dmg) вне .venv/node_modules
    skip_dirs = {".venv", "venv", "env", "node_modules", ".git", "__pycache__", "site-packages"}
    if list(p.glob("*.app")) or list(p.glob("**/*.app")) or list(p.glob("*.dmg")) or list(p.glob("*.msi")):
        return "gui"
    exe_candidates = [
        f for f in (list(p.glob("*.exe")) + list(p.glob("*/*.exe")) + list(p.glob("*/*/*.exe")))
        if not any(part.lower() in skip_dirs for part in f.relative_to(p).parts[:-1])
    ] if p.exists() else []
    if exe_candidates:
        if all(is_windows_console_exe(f) and not any(m in f.name.lower() for m in ("setup", "install", "installer")) for f in exe_candidates):
            return "cli"
        return "gui"

    # 0.1 Проверка на живой онлайн веб-сервис (например Bento, Excalidraw и др.)
    if is_online_web_service(repo_url):
        return "web"

    # 1. Проверка на Docs
    code_exts = {".py", ".js", ".ts", ".go", ".rs", ".cpp", ".c", ".java", ".sh", ".ps1", ".rb", ".html"}
    has_code = False
    if p.exists():
        for root, _, files in os.walk(p):
            if any(Path(f).suffix.lower() in code_exts for f in files):
                has_code = True
                break
    if not has_code and not (p / "package.json").exists() and not (p / "pyproject.toml").exists() and not (p / "requirements.txt").exists():
        if list(p.glob("*.md")) or list(p.glob("*.txt")) or any(w in readme_l for w in ["awesome-", "curated list", "collection of prompts", "prompts for", "cheat sheet"]):
            return "docs"

    # 2. Проверка на GUI
    if "apps.apple.com" in u_l or u_l.startswith("macappstore://"):
        return "gui"
    if (p / "Cargo.toml").exists():
        try:
            cargo_raw = (p / "Cargo.toml").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in cargo_raw for k in ["egui", "eframe", "iced", "winit", "tauri", "slint", "druid", "gtk", "fltk"]):
                return "gui"
            if any(k in readme_l for k in ["desktop", "gui", "player", "window", "client"]):
                return "gui"
        except Exception:
            pass
    if (p / "go.mod").exists():
        try:
            go_raw = (p / "go.mod").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in go_raw for k in ["fyne", "gioui", "wails", "webview"]):
                return "gui"
        except Exception:
            pass
    if any(k in readme_l for k in ["tkinter", "pyqt", "pyside", "wxpython", "electron", "tauri", "desktop app", "gui app", "gui application", "spotify client", "spotify player"]):
        return "gui"

    # 3. Проверка на Web
    non_web_code = (p / "Cargo.toml").exists() or (p / "go.mod").exists() or (p / "pyproject.toml").exists() or (p / "requirements.txt").exists() or list(p.glob("*.py")) or list(p.glob("*.rs")) or list(p.glob("*.go"))
    html_files = list(p.glob("*.html")) + list(p.glob("*.htm"))
    if html_files and not non_web_code:
        return "web"
    manifests = list(p.glob("manifest.json")) + list(p.glob("*/manifest.json")) + list(p.glob("dist/manifest.json")) + list(p.glob("src/manifest.json"))
    if manifests:
        return "web"
    if (p / "docker-compose.yml").exists() or (p / "docker-compose.yaml").exists():
        return "web"
    if (p / "package.json").exists():
        try:
            pkg_raw = (p / "package.json").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in pkg_raw for k in ["vite", "next", "react", "vue", "express", "fastify", "svelte", "nuxt"]):
                return "web"
        except Exception:
            pass
    if (p / "requirements.txt").exists():
        try:
            req_raw = (p / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in req_raw for k in ["streamlit", "gradio", "fastapi", "uvicorn", "flask", "django", "tornado", "aiohttp"]):
                return "web"
        except Exception:
            pass
    if any(k in readme_l for k in ["streamlit run", "uvicorn", "localhost:", "127.0.0.1:", "http://localhost", "web ui", "web interface", "dashboard", "fastapi", "flask run"]):
        return "web"

    # 4. Проверка на CLI
    if (p / "Cargo.toml").exists():
        return "cli"
    if (p / "go.mod").exists():
        return "cli"
    if (p / "pyproject.toml").exists():
        try:
            pyproj = (p / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "[project.scripts]" in pyproj or "[tool.poetry.scripts]" in pyproj:
                return "cli"
        except Exception:
            pass
    IGNORED_FILES = {"noxfile.py", "setup.py", "conftest.py", "tasks.py", "fabfile.py", "wsgi.py", "conf.py", "test.py", "tests.py", "build.py", "version.py"}
    candidates = ["main.py", "app.py", "cli.py", "run.py", "organize.py", "sort.py", "index.py", "server.py"]
    for c in candidates:
        if (p / c).exists():
            return "cli"
    py_files = [f for f in p.glob("*.py") if not f.name.startswith((".", "_")) and f.name not in IGNORED_FILES]
    if py_files:
        return "cli"
    if any(k in readme_l for k in ["usage:", "command line", "cli", "flags", "options:", "arguments:"]):
        return "cli"

    # 5. Проверка на Library / MCP
    if (p / "setup.py").exists() or (p / "pyproject.toml").exists() or any(k in readme_l for k in ["pip install", "import ", "sdk", "api client", "mcp server", "modelcontextprotocol"]):
        return "library"

    return "cli"

def detect_offline_launch_command(local_path: str, readme_text: str = "", repo_url: str = "") -> Dict[str, Any]:
    """Определяет команду запуска, тип приложения (web/cli/library/gui/docs) и пошаговую инструкцию по структуре файлов и README."""
    p = Path(local_path)
    p_type = classify_repository_type(local_path, readme_text, repo_url)
    py_cmd_prefix = "python" if IS_WINDOWS else "python3"
    
    # 0. Проверка на Mac App Store ссылку
    if repo_url and ("apps.apple.com" in repo_url or repo_url.startswith("macappstore://")):
        return {
            "project_type": "gui",
            "summary": "Официальное приложение из Mac App Store.",
            "is_web": False,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Открыть приложение» ниже.\n2. Приложение откроется на экране {OS_NAME}.",
            "how_to_use": f"Приложение для {OS_NAME}.",
            "launch_command": f"open '{repo_url}'" if IS_MAC else f"start '{repo_url}'",
            "examples": "open",
            "how_to_open_web": "",
            "auto_description": f"Запустит приложение или откроет магазин приложений на экране {OS_NAME}"
        }

    # 1. Проверка на расширение для браузера (Chrome, Edge, Brave, Safari)
    manifests = list(p.glob("manifest.json")) + list(p.glob("*/manifest.json")) + list(p.glob("dist/manifest.json")) + list(p.glob("src/manifest.json"))
    for m in manifests:
        try:
            m_data = json.loads(m.read_text(encoding="utf-8", errors="ignore"))
            if "manifest_version" in m_data:
                fm_name = "Finder" if IS_MAC else "Проводнике"
                return {
                    "project_type": "web",
                    "summary": "Расширение для браузера (Chrome, Edge, Brave, Safari).",
                    "is_web": False,
                    "web_port": 0,
                    "quick_start": f"1. Нажмите «Запустить на {OS_NAME}» ниже.\n2. На {OS_NAME} откроется страница chrome://extensions и папка в {fm_name}.\n3. Включите режим разработчика и перетащите папку в окно браузера.",
                    "how_to_use": "Браузерное расширение. Устанавливается через chrome://extensions (Режим разработчика -> Загрузить распакованное).",
                    "launch_command": "open chrome://extensions" if IS_MAC else "start chrome://extensions",
                    "examples": "chrome://extensions",
                    "how_to_open_web": "",
                    "auto_description": f"Откроет страницу расширений в браузере и покажет готовую папку в {fm_name}"
                }
        except Exception:
            pass

    # 2. Проверка на расширение VS Code
    vsix_files = list(p.glob("*.vsix"))
    if vsix_files:
        return {
            "project_type": "gui",
            "summary": f"Расширение для редактора VS Code ({vsix_files[0].name}).",
            "is_web": False,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Запустить на {OS_NAME}» ниже.\n2. Расширение автоматически установится в VS Code.",
            "how_to_use": "Расширение для редактора VS Code.",
            "launch_command": f"code --install-extension '{vsix_files[0].name}'",
            "examples": "code",
            "how_to_open_web": "",
            "auto_description": f"Установит расширение в VS Code на вашем {OS_NAME}"
        }

    # 3. Проверка на нативный .app бандл
    app_bundles = list(p.glob("*.app")) + list(p.glob("**/*.app"))
    if app_bundles:
        return {
            "project_type": "gui",
            "summary": f"Нативное приложение macOS ({app_bundles[0].name}).",
            "is_web": False,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Запустить на {OS_NAME}» ниже.\n2. Программа запустится на экране {OS_NAME}.",
            "how_to_use": f"Нативное приложение macOS {app_bundles[0].name}.",
            "launch_command": f"open '{app_bundles[0]}'",
            "examples": "open",
            "how_to_open_web": "",
            "auto_description": f"Запустит {app_bundles[0].name} на экране {OS_NAME}"
        }

    # 3.1. Проверка на готовые Windows бинарники (.exe / .msi) вне .venv/node_modules
    if IS_WINDOWS and p.exists():
        skip_dirs = {".venv", "venv", "env", "node_modules", ".git", "__pycache__", "site-packages"}
        win_bins = [
            f for f in (list(p.glob("*.exe")) + list(p.glob("*.msi")) + list(p.glob("*/*.exe")))
            if not any(part.lower() in skip_dirs for part in f.relative_to(p).parts[:-1])
        ]
        if win_bins:
            target_bin = win_bins[0]
            is_cli_bin = target_bin.suffix.lower() == ".exe" and is_windows_console_exe(target_bin) and not any(m in target_bin.name.lower() for m in ("setup", "install", "installer"))
            bin_type = "cli" if is_cli_bin else "gui"
            return {
                "project_type": bin_type,
                "summary": f"Готовая консольная утилита Windows ({target_bin.name})." if is_cli_bin else f"Готовое приложение Windows ({target_bin.name}).",
                "is_web": False,
                "web_port": 0,
                "quick_start": f"1. Нажмите кнопку запуска ниже.\n2. {target_bin.name} откроется на экране {OS_NAME}.",
                "how_to_use": f"Готовый исполняемый файл Windows: {target_bin.name}.",
                "launch_command": f".\\{target_bin.name}",
                "examples": f".\\{target_bin.name} --help" if is_cli_bin else f".\\{target_bin.name}",
                "how_to_open_web": "",
                "auto_description": f"Запустит {target_bin.name} на {OS_NAME}"
            }

    # 3.4. Проверка на живой онлайн веб-сервис
    effective_online_url = repo_url
    if not effective_online_url:
        try:
            from bot.memory_service import find_repo_url_in_memory
            effective_online_url = find_repo_url_in_memory(p.name)
        except Exception:
            pass
    if is_online_web_service(effective_online_url):
        return {
            "project_type": "web",
            "summary": f"Онлайн веб-сервис {p.name}. Работает напрямую в браузере.",
            "is_web": True,
            "is_online_web": True,
            "online_url": effective_online_url,
            "web_port": 0,
            "quick_start": f"1. Откройте сервис онлайн в браузере (работает с любого устройства).\n2. Нажмите «Запустить на {OS_NAME}», чтобы открыть страницу на экране компьютера.",
            "how_to_use": f"Онлайн веб-сервис: {effective_online_url}",
            "launch_command": f"start {effective_online_url}" if IS_WINDOWS else f"open '{effective_online_url}'",
            "examples": effective_online_url,
            "how_to_open_web": f"Открывается в браузере: {effective_online_url}",
            "auto_description": f"Откроет {effective_online_url} в браузере на экране {OS_NAME}"
        }

    # 3.5. Проверка на локальную HTML веб-страницу или презентацию (если нет исполняемого кода)
    has_real_code = (p / "Cargo.toml").exists() or (p / "go.mod").exists() or (p / "pyproject.toml").exists() or (p / "requirements.txt").exists() or (p / "package.json").exists() or list(p.glob("*.py")) or list(p.glob("*.rs")) or list(p.glob("*.go"))
    html_files = list(p.glob("*.html")) + list(p.glob("*.htm"))
    if html_files and not has_real_code:
        target_html = html_files[0]
        return {
            "project_type": "web",
            "summary": f"Интерактивная HTML-страница / презентация {target_html.name}.",
            "is_web": True,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Запустить веб-сервис» ниже.\n2. Страница {target_html.name} откроется в браузере на {OS_NAME}.",
            "how_to_use": "Веб-страница / презентация открывается напрямую в системном браузере.",
            "launch_command": f"start {target_html.name}" if IS_WINDOWS else f"open {target_html.name}",
            "examples": target_html.name,
            "how_to_open_web": f"Открывается в браузере: {target_html.name}",
            "auto_description": f"Откроет {target_html.name} в браузере на {OS_NAME}"
        }

    # 4. Попытка вытянуть готовые команды прямо из README.md
    extracted_cmd = ""
    if readme_text:
        matches = re.findall(r"```(?:bash|sh|shell|zsh|powershell|cmd)?\s*\n([^`]+)\n```", readme_text, re.IGNORECASE)
        for block in matches:
            for line in block.split("\n"):
                line_clean = line.strip().lstrip("$ ").strip()
                if IS_WINDOWS and line_clean.startswith(("bash ", "sh ", "./")):
                    continue
                if any(line_clean.startswith(prefix) for prefix in [
                    "python3 ", "python ", "npm start", "npm run", "cargo run", "go run", "./", "bash ", "uv run", "streamlit run"
                ]):
                    if IS_WINDOWS and line_clean.startswith("python3 "):
                        line_clean = "python " + line_clean[8:]
                    extracted_cmd = line_clean
                    break
            if extracted_cmd:
                break

    # Проверка на наличие признаков веб-интерфейса в README
    is_web_in_readme = False
    readme_port = 0
    if readme_text:
        low_readme = readme_text.lower()
        if any(w in low_readme for w in ["localhost", "127.0.0.1", "web ui", "web interface", "браузер", "browser", "dashboard"]):
            is_web_in_readme = True
            m_port = re.search(r"localhost:(\d+)", low_readme) or re.search(r":(\d{4,5})", low_readme)
            if m_port:
                readme_port = int(m_port.group(1))

    # 5. Определение по структуре каталога
    if IS_MAC and (p / "macos" / "install.sh").exists():
        cmd = extracted_cmd or "bash macos/install.sh"
        return {
            "project_type": p_type,
            "summary": "Нативное приложение для macOS с автоматическим скриптом установки.",
            "is_web": False,
            "web_port": 0,
            "quick_start": "1. Запустите команду установки ниже.\n2. Приложение появится в /Applications.\n3. Откройте приложение на Mac.",
            "how_to_use": "Нативное приложение macOS. Скрипт установит релиз в /Applications и откроет программу.",
            "launch_command": cmd,
            "examples": cmd,
            "how_to_open_web": "",
            "auto_description": "Запустит установщик и откроет программу на Mac"
        }
    if (p / "package.json").exists():
        cmd = extracted_cmd or "npm install && npm start"
        is_node_web = True
        node_port = 3000
        try:
            pkg_raw = (p / "package.json").read_text(encoding="utf-8", errors="ignore")
            if "vite" in pkg_raw:
                node_port = 5173
            elif "next" in pkg_raw or "react" in pkg_raw:
                node_port = 3000
        except Exception:
            pass

        return {
            "project_type": "web" if is_node_web else p_type,
            "summary": "Веб-интерфейс / программа на Node.js.",
            "is_web": is_node_web,
            "web_port": readme_port or node_port,
            "quick_start": f"1. Нажмите «Запустить веб-сервис» ниже.\n2. Сервер запустится и автоматически откроет страницу в браузере на {OS_NAME}.\n3. С телефона можно открыть по ссылке в домашнем Wi-Fi.",
            "how_to_use": "Node.js проект. Запускается локальный сервер разработки.",
            "launch_command": cmd,
            "examples": "npm start",
            "how_to_open_web": f"Страница автоматически откроется в браузере по адресу http://localhost:{readme_port or node_port}. Для доступа с телефона перейдите по локальному адресу в одной сети Wi-Fi.",
            "auto_description": f"Установит зависимости, поднимет веб-сервер и откроет сайт в браузере на {OS_NAME}"
        }

    # 5. Проверка Python пакета через pyproject.toml / setup.py
    cli_cmd = None
    if (p / "pyproject.toml").exists():
        try:
            pyproj_raw = (p / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            m_sec = re.search(r"\[project\.scripts\]\s*\n([^\[]+)", pyproj_raw)
            if m_sec:
                for line in m_sec.group(1).split("\n"):
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        s_name = line.split("=")[0].strip().strip('"\'')
                        if s_name:
                            cli_cmd = s_name
                            break
        except Exception:
            pass

    if (p / "requirements.txt").exists() or (p / "pyproject.toml").exists() or (p / "setup.py").exists() or list(p.glob("*.py")):
        req_raw = ""
        if (p / "requirements.txt").exists():
            try:
                req_raw = (p / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                pass

        IGNORED_PY_FILES = {
            "noxfile.py", "setup.py", "conftest.py", "tasks.py", "fabfile.py",
            "wsgi.py", "conf.py", "test.py", "tests.py", "build.py", "version.py"
        }

        candidates = ["main.py", "app.py", "cli.py", "run.py", "organize.py", "sort.py", "sort_files.py", "index.py", "server.py"]
        entry_file = None
        for c in candidates:
            if (p / c).exists():
                entry_file = c
                break
        if not entry_file:
            py_files = [f.name for f in p.glob("*.py") if not f.name.startswith((".", "_")) and f.name not in IGNORED_PY_FILES]
            if py_files:
                entry_file = py_files[0]

        # Проверяем Python на веб-фреймворки
        py_is_web = False
        py_port = 0
        if entry_file:
            if "streamlit" in req_raw:
                py_is_web = True
                py_port = 8501
                cmd = extracted_cmd or f"streamlit run {entry_file}"
            elif "gradio" in req_raw:
                py_is_web = True
                py_port = 7860
                cmd = extracted_cmd or f"{py_cmd_prefix} {entry_file}"
            elif "fastapi" in req_raw or "uvicorn" in req_raw:
                py_is_web = True
                py_port = 8000
                cmd = extracted_cmd or f"uvicorn {entry_file.replace('.py', '')}:app --reload"
            elif "flask" in req_raw:
                py_is_web = True
                py_port = 5000
                cmd = extracted_cmd or f"{py_cmd_prefix} {entry_file}"
            else:
                py_is_web = is_web_in_readme
                py_port = readme_port or (8080 if is_web_in_readme else 0)
                cmd = extracted_cmd or f"{py_cmd_prefix} {entry_file}"
            summary_text = "Веб-интерфейс на Python." if py_is_web else f"Python-утилита. Выполняет автоматизацию или обработку данных через скрипт {entry_file}."
            how_to_use_text = f"Python проект. Запускается через скрипт {entry_file}."
        elif cli_cmd:
            cmd = extracted_cmd or f"{cli_cmd} --help"
            summary_text = f"Python CLI библиотека / утилита ({cli_cmd})."
            how_to_use_text = f"Python CLI инструмент {cli_cmd}. Доступен через команду `{cli_cmd}`."
        else:
            cmd = extracted_cmd or f"{py_cmd_prefix} -m {p.name.lower().replace('-', '_')} --help"
            summary_text = f"Python модуль / библиотека {p.name}."
            how_to_use_text = f"Python проект {p.name}."

        open_web_text = f"Веб-интерфейс автоматически откроется в браузере по адресу http://localhost:{py_port or 8000}." if py_is_web else ""

        computed_type = "web" if py_is_web else ("cli" if entry_file or cli_cmd else p_type)

        return {
            "project_type": computed_type,
            "summary": summary_text,
            "is_web": py_is_web,
            "web_port": py_port,
            "quick_start": f"1. Нажмите «Открыть в терминале» ниже.\n2. Утилита откроется в окне терминала на {OS_NAME}." if not py_is_web else f"1. Нажмите «Запустить веб-сервис» ниже.\n2. Веб-интерфейс откроется в браузере на {OS_NAME}.",
            "how_to_use": how_to_use_text,
            "launch_command": cmd,
            "examples": f"{cmd}",
            "how_to_open_web": open_web_text,
            "auto_description": f"Запустит веб-сервер и откроет страницу в браузере на {OS_NAME}" if py_is_web else f"Запустит {cmd} в терминале на {OS_NAME}"
        }

    # 5.1. Проверка Rust проекта (Cargo.toml)
    if (p / "Cargo.toml").exists():
        is_rust_gui = False
        try:
            cargo_content = (p / "Cargo.toml").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in cargo_content for k in ["egui", "eframe", "iced", "winit", "tauri", "slint", "druid", "gtk", "fltk"]):
                is_rust_gui = True
        except Exception:
            pass
        if any(k in (readme_text or "").lower() for k in ["desktop", "gui", "player", "window", "client"]):
            is_rust_gui = True

        cargo_cmd = extracted_cmd or "cargo run --release"
        r_type = "gui" if is_rust_gui else "cli"
        return {
            "project_type": r_type,
            "summary": f"Быстрое Rust-приложение {p.name}.",
            "is_web": False,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Запустить на {OS_NAME}» ниже.\n2. Приложение запустится на экране {OS_NAME}.",
            "how_to_use": f"Rust проект {p.name}. Запуск через: `{cargo_cmd}`.",
            "launch_command": cargo_cmd,
            "examples": cargo_cmd,
            "how_to_open_web": "",
            "auto_description": f"Запустит приложение {p.name} на {OS_NAME}"
        }

    # 5.2. Проверка Go проекта (go.mod)
    if (p / "go.mod").exists():
        go_cmd = extracted_cmd or "go run ."
        return {
            "project_type": "cli",
            "summary": f"Быстрая Go-утилита {p.name}.",
            "is_web": False,
            "web_port": 0,
            "quick_start": f"1. Нажмите «Запустить на {OS_NAME}» ниже.\n2. Утилита запустится в окне консоли на {OS_NAME}.",
            "how_to_use": f"Go проект {p.name}. Запуск через: `{go_cmd}`.",
            "launch_command": go_cmd,
            "examples": go_cmd,
            "how_to_open_web": "",
            "auto_description": f"Запустит {p.name} в терминале на {OS_NAME}"
        }

    if (p / "docker-compose.yml").exists() or (p / "docker-compose.yaml").exists():
        cmd = extracted_cmd or "docker compose up -d"
        return {
            "project_type": "web",
            "summary": "Контейнеризированный сервис Docker.",
            "is_web": True,
            "web_port": readme_port or 8080,
            "quick_start": f"1. Нажмите «Запустить веб-сервис» ниже.\n2. Контейнеры поднимутся в фоне на {OS_NAME}.",
            "how_to_use": "Docker контейнер. Разворачивается в фоновом режиме через docker compose.",
            "launch_command": cmd,
            "examples": "docker compose logs -f",
            "how_to_open_web": f"Сервис доступен по адресу http://localhost:{readme_port or 8080}",
            "auto_description": f"Развернет контейнеры в фоне и откроет веб-интерфейс на {OS_NAME}"
        }

    if IS_MAC and (p / "install.sh").exists():
        cmd = extracted_cmd or "bash install.sh"
        return {
            "project_type": p_type,
            "summary": "Утилита со встроенным установочным скриптом для Mac.",
            "is_web": False,
            "web_port": 0,
            "quick_start": "1. Запустите установочный скрипт одной кнопкой ниже.\n2. Дождитесь завершения настройки окружения.",
            "how_to_use": "Проект содержит скрипт автоматической установки. Запустите его для инсталляции на Mac.",
            "launch_command": cmd,
            "examples": cmd,
            "how_to_open_web": "",
            "auto_description": "Выполнит установку и настройку окружения на Mac"
        }

    cmd = extracted_cmd or ("dir" if IS_WINDOWS else "ls -la")
    return {
        "project_type": p_type,
        "summary": f"Файлы проекта {p.name} сохранены в папку на {OS_NAME}.",
        "is_web": is_web_in_readme,
        "web_port": readme_port,
        "quick_start": f"1. Нажмите «Открыть в терминале» ниже.\n2. Результат появится на экране {OS_NAME}.",
        "how_to_use": f"Файлы проекта скачаны на {OS_NAME}.",
        "launch_command": cmd,
        "examples": cmd,
        "how_to_open_web": f"Доступен по адресу http://localhost:{readme_port}" if readme_port else "",
        "auto_description": f"Откроет проект на {OS_NAME}"
    }

async def generate_usage_guide(local_path: str, project_name: str, repo_url: str = "") -> Dict[str, str]:
    """
    Анализирует README и структуру скачанного проекта:
    возвращает понятную инструкцию на простом языке (что делает, пошаговый план, примеры, запуск).
    """
    p = Path(local_path)
    if not p.exists():
        return {
            "project_type": "cli",
            "summary": "Папка проекта не найдена на диске.",
            "quick_start": "Проверьте путь к проекту.",
            "how_to_use": "Папка проекта не найдена на диске.",
            "launch_command": "dir" if IS_WINDOWS else "ls",
            "examples": ""
        }

    # Ищем README файл
    readme_text = ""
    for candidate in ["README.md", "README", "readme.md", "Readme.md", "SKILL.md"]:
        r_file = p / candidate
        if r_file.exists():
            try:
                readme_text = r_file.read_text(encoding="utf-8", errors="ignore")[:4000]
                break
            except Exception:
                pass

    fallback_data = detect_offline_launch_command(local_path, readme_text, repo_url=repo_url)

    # Если README нет, используем оффлайн детекцию
    if not readme_text:
        return fallback_data

    prompt = f"""
Ты — персональный ассистент разработчика.
Пользователь только что скачал утилиту «{project_name}» на свой компьютер ({OS_NAME}) и хочет либо сразу всё запустить в 1 клик, либо быстро понять, как ей пользоваться.
Вот текст из файла README:
{readme_text}

Составь предельно практичное руководство на простом русском языке (без сложного жаргона).
Ответь СТРОГО в формате валидного JSON:
{{
  "summary": "1-2 простых предложения: что именно делает этот инструмент на компьютере.",
  "is_web": true или false (является ли программа веб-интерфейсом, сайтом, дашбордом или локальным сервером),
  "web_port": номер порта числом (например: 3000, 8000, 8501, 7860, 5000) или null,
  "how_to_open_web": "Инструкция как открыть веб-интерфейс в браузере (на {OS_NAME} и с телефона)",
  "auto_description": "Что произойдет при нажатии кнопки 'Запустить на {OS_NAME}' (например: 'Запустит веб-сервер и откроет страницу в браузере' или 'Настроит окружение и автоматически выполнит задачу')",
  "quick_start": "Пошаговый план (3 коротких шага): 1. Положите файлы... 2. Запустите команду... 3. Получите результат...",
  "launch_command": "Точная команда запуска одной строкой из корня проекта (например: python main.py, go run ., npm start, streamlit run app.py)",
  "examples": "1-2 наглядных примера вызова с популярными флагами/параметрами (если утилита консольная)",
  "how_to_use": "Краткая выжимка для карточки Telegram (2-3 понятных предложения)."
}}
Только валидный JSON без markdown-блоков (без ```json).
"""

    resp = await call_gemini_with_fallback(prompt)
    if resp:
        try:
            if resp.startswith("```"):
                resp = re.sub(r"^```(?:json)?\s*", "", resp)
                resp = re.sub(r"\s*```$", "", resp)
            data = json.loads(resp.strip())
            is_web_flag = data.get("is_web", fallback_data.get("is_web", False))
            resolved_type = data.get("project_type") or fallback_data.get("project_type") or ("web" if is_web_flag else "cli")
            raw_cmd = data.get("launch_command") or fallback_data["launch_command"]
            if IS_WINDOWS and raw_cmd.startswith("python3 "):
                raw_cmd = "python " + raw_cmd[8:]
            return {
                "project_type": resolved_type,
                "summary": data.get("summary") or fallback_data["summary"],
                "is_web": is_web_flag,
                "web_port": data.get("web_port") or fallback_data.get("web_port", 0),
                "how_to_open_web": data.get("how_to_open_web") or fallback_data.get("how_to_open_web", ""),
                "auto_description": data.get("auto_description") or fallback_data.get("auto_description", f"Выполнит утилиту на {OS_NAME}"),
                "quick_start": data.get("quick_start") or fallback_data["quick_start"],
                "launch_command": raw_cmd,
                "examples": data.get("examples") or fallback_data.get("examples", ""),
                "how_to_use": data.get("how_to_use") or data.get("summary") or fallback_data["how_to_use"]
            }
        except Exception as e:
            logger.warning(f"Ошибка парсинга ответа Gemini для гайда {project_name}: {e}")

    return fallback_data

PYTHON_PROJECT_MARKERS = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "environment.yml")


def is_python_project(p: Path) -> bool:
    """Признаки Python-проекта в корне: файлы зависимостей/сборки или исходники .py (без вложенных папок)."""
    try:
        if any((p / m).exists() for m in PYTHON_PROJECT_MARKERS):
            return True
        return any(f.suffix == ".py" for f in p.iterdir() if f.is_file())
    except OSError:
        return False


async def prepare_python_env(local_path: str) -> Dict[str, Any]:
    """
    Создает изолированное окружение .venv и устанавливает зависимости из requirements.txt.
    """
    p = Path(local_path)
    if not is_python_project(p):
        # Готовая сборка, Go, Rust, Node.js — окружение Python им не нужно, а создаётся оно десятки секунд
        return {"success": True, "skipped": True, "message": "Не Python-проект: окружение не нужно"}
    venv_dir = p / ".venv"
    if IS_WINDOWS:
        pip_bin = venv_dir / "Scripts" / "pip.exe"
        venv_py_bin = venv_dir / "Scripts" / "python.exe"
    else:
        pip_bin = venv_dir / "bin" / "pip"
        venv_py_bin = venv_dir / "bin" / "python"
    # requires-python проекта (например, khoj: >=3.10,<3.13) может не подходить под Python самого агента
    python_exec = find_python_for_project(p) or sys.executable or ("python" if IS_WINDOWS else "python3")

    spec = _requires_python(p)
    venv_ver = _venv_python_version(venv_dir) if venv_dir.exists() else None
    if venv_ver and not python_version_ok(spec, venv_ver):
        logger.info(f"Пересоздание .venv в {local_path}: Python {venv_ver[0]}.{venv_ver[1]} не подходит под {spec}")
        shutil.rmtree(venv_dir, onerror=_handle_remove_readonly)

    if not venv_py_bin.exists():
        logger.info(f"Создание виртуального окружения в {local_path}...")
        proc = await asyncio.create_subprocess_exec(
            python_exec, "-m", "venv", ".venv",
            cwd=str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "message": "Не удалось создать .venv"}

    req_file = p / "requirements.txt"
    env = os.environ.copy()
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        env.pop(k, None)

    if req_file.exists():
        logger.info(f"Установка requirements.txt в {local_path}...")
        if pip_bin.exists():
            pip_cmd = [str(pip_bin), "install", "-r", "requirements.txt"]
        else:
            pip_cmd = [str(venv_py_bin), "-m", "pip", "install", "-r", "requirements.txt"]

        proc = await asyncio.create_subprocess_exec(
            *pip_cmd,
            cwd=str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {
                "success": False,
                "message": f"Ошибка при pip install: {stderr.decode(errors='ignore')[:300]}"
            }
    elif (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        logger.info(f"Установка пакета из pyproject.toml / setup.py в {local_path}...")
        if pip_bin.exists():
            pip_cmd = [str(pip_bin), "install", "-e", "."]
        else:
            pip_cmd = [str(venv_py_bin), "-m", "pip", "install", "-e", "."]

        proc = await asyncio.create_subprocess_exec(
            *pip_cmd,
            cwd=str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate()

    return {"success": True, "message": "Окружение .venv готово."}


async def _drain_pipe_to_log(stream: Optional[asyncio.StreamReader], log_file: Path) -> None:
    """Фоново вычитывает stdout/stderr долгоживущего процесса в лог-файл, предотвращая переполнение пайпа ОС (Deadlock)."""
    if stream is None:
        return
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as f:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                try:
                    f.write(chunk)
                    f.flush()
                except Exception:
                    pass
    except Exception:
        pass


async def launch_project_on_mac(local_path: str, command: str) -> Dict[str, Any]:
    """
    Запускает команду проекта в указанной директории.
    Автоматически активирует .venv и перенаправляет python3 на виртуальное окружение.
    Если процесс завершается за 3 секунды — возвращает результат.
    Если процесс продолжает работать (сервер/демон) — сохраняет его PID в фоне и запускает дренаж пайпов.
    """
    p = Path(local_path)
    if not p.exists():
        return {"success": False, "message": f"Папка {local_path} не существует."}

    # Настраиваем окружение процесса
    env = os.environ.copy()
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        env.pop(k, None)

    # На Windows: если команда пытается выполнить .sh скрипт, перенаправляем на прямой запуск Python
    if IS_WINDOWS and (command.endswith(".sh") or command.startswith(("./", "bash ", "sh "))):
        for c in ["app.py", "main.py", "run.py", "cli.py", "index.py", "server.py"]:
            if (p / c).exists():
                command = f"python {c}"
                break

    venv_dir = p / ".venv"
    venv_bin = venv_dir / ("Scripts" if IS_WINDOWS else "bin")
    venv_python = venv_bin / ("python.exe" if IS_WINDOWS else "python")

    if venv_bin.exists():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(venv_dir)
        py_q = f'"{venv_python}"' if IS_WINDOWS else str(venv_python)
        tokens = command.split()
        first_token = tokens[0] if tokens else ""
        if first_token in ("python3", "python"):
            command = f"{py_q} {command[len(first_token)+1:]}"
        else:
            exe_candidate = venv_bin / (f"{first_token}.exe" if IS_WINDOWS else first_token)
            if exe_candidate.exists():
                command = f'"{exe_candidate}" {command[len(first_token)+1:]}'
    elif IS_WINDOWS and command.startswith("python3 "):
        command = f"python {command[8:]}"  # на Windows нет python3

    full_cmd = f"/usr/bin/caffeinate -s -i {command}" if IS_MAC else command
    try:
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            cwd=str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        # Ждем 3 секунды, чтобы понять: команда завершилась сразу или процесс фоновый
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            out_str = stdout.decode(errors="ignore").strip()
            err_str = stderr.decode(errors="ignore").strip()

            if proc.returncode == 0:
                return {
                    "success": True,
                    "is_running": False,
                    "pid": proc.pid,
                    "message": "Команда успешно выполнена.",
                    "output": (out_str or "Выполнено без вывода")[:500]
                }
            else:
                return {
                    "success": False,
                    "is_running": False,
                    "pid": proc.pid,
                    "message": f"Команда завершилась с ошибкой (код {proc.returncode}).",
                    "output": (err_str or out_str or "Нет данных")[:500]
                }
        except asyncio.TimeoutError:
            # Процесс продолжает работать в фоне (сервер / демон / процесс)
            ACTIVE_PROCESSES[proc.pid] = proc
            ACTIVE_PROCESS_INFO[proc.pid] = {
                "project_name": p.name,
                "path": str(p),
                "command": command,
                "port": 0
            }
            safe_log_name = re.sub(r"[^a-zA-Z0-9_-]", "_", p.name)
            log_file = LOGS_DIR / f"proc_{safe_log_name}_{proc.pid}.log"
            asyncio.create_task(_drain_pipe_to_log(proc.stdout, log_file))
            asyncio.create_task(_drain_pipe_to_log(proc.stderr, log_file))
            return {
                "success": True,
                "is_running": True,
                "pid": proc.pid,
                "message": f"Процесс запущен и работает в фоне на {OS_NAME}.",
                "output": f"Команда: {command}\nPID: {proc.pid}"
            }
    except Exception as e:
        logger.error(f"Ошибка запуска команды {command}: {e}")
        return {"success": False, "message": f"Исключение при запуске: {e}"}

_LAUNCH_SKIP_DIRS = {".venv", "venv", "env", "node_modules", ".git", "__pycache__", "site-packages"}

# Скрипты сборки/обслуживания и папки вспомогательных клиентов — не точка входа приложения
# (например, у khoj единственный .bat — src/interface/android/gradlew.bat).
_BUILD_SCRIPT_NAMES = {
    "gradlew", "mvnw", "build", "make", "compile", "clean", "test", "tests", "setup", "install", "uninstall",
    "bootstrap", "configure", "deploy", "release", "package", "lint", "format", "publish",
}
_NON_APP_DIRS = {
    "android", "ios", "docs", "doc", "test", "tests", "examples", "example", "ci", ".github", "benchmarks",
    "bench", "tools", "packaging", "installer", "docker",
}

# Среды выполнения, которые агент умеет доставить сам. Ключ приходит из подписанной задачи,
# но установка идёт только по этому белому списку — произвольный пакет поставить нельзя.
RUNTIMES: Dict[str, Dict[str, Any]] = {
    "java": {"label": "Java (JDK 21)", "bins": ["java"], "winget": "EclipseAdoptium.Temurin.21.JDK", "brew": "openjdk@21"},
    "node": {"label": "Node.js LTS", "bins": ["node", "npm"], "winget": "OpenJS.NodeJS.LTS", "brew": "node"},
    "python": {"label": "Python 3.12", "bins": ["python", "py", "python3"], "winget": "Python.Python.3.12", "brew": "python@3.12"},
    "go": {"label": "Go", "bins": ["go"], "winget": "GoLang.Go", "brew": "go"},
    "rust": {"label": "Rust (cargo)", "bins": ["cargo"], "winget": "Rustlang.Rustup", "brew": "rustup"},
    # Не среда запуска, а инструмент скачивания: без него не скачать исходники (у гостей Git часто нет)
    "git": {"label": "Git", "bins": ["git"], "winget": "Git.Git", "brew": "git"},
}


def refresh_env_from_system() -> None:
    """После установки программ PATH текущего процесса устаревает: подтягиваем свежий из реестра Windows."""
    if not IS_WINDOWS:
        return
    try:
        import winreg
        values = {}
        for hive, key_path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ):
            try:
                with winreg.OpenKey(hive, key_path) as k:
                    for name in ("Path", "JAVA_HOME"):
                        try:
                            val, _ = winreg.QueryValueEx(k, name)
                            values.setdefault(name, []).append(os.path.expandvars(val))
                        except OSError:
                            pass
            except OSError:
                pass
        if values.get("Path"):
            merged = []
            for chunk in values["Path"] + [os.environ.get("PATH", "")]:
                for part in chunk.split(os.pathsep):
                    if part and part not in merged:
                        merged.append(part)
            os.environ["PATH"] = os.pathsep.join(merged)
        if values.get("JAVA_HOME") and not os.environ.get("JAVA_HOME"):
            os.environ["JAVA_HOME"] = values["JAVA_HOME"][-1]
    except Exception as e:
        logger.warning(f"Не удалось обновить PATH из реестра: {e}")


def is_runtime_available(key: str) -> bool:
    spec = RUNTIMES.get(key)
    if not spec:
        return True
    if key == "python" and sys.executable:
        return True  # агент сам работает на Python, venv создаётся от него
    refresh_env_from_system()
    if key == "java":
        java_home = os.environ.get("JAVA_HOME")
        if java_home and (Path(java_home) / "bin" / ("java.exe" if IS_WINDOWS else "java")).exists():
            return True
    return any(shutil.which(b) for b in spec["bins"])


def detect_required_runtime(command: str = "", script: Optional[Path] = None) -> Optional[str]:
    """Какая среда нужна команде или скрипту запуска (по первому слову команды / содержимому .bat)."""
    parts = (command or "").strip().lstrip("&").strip().split()
    name = Path(parts[0].strip("'\"")).stem.lower() if parts else ""
    if name in ("gradlew", "mvnw", "java", "javaw", "mvn", "gradle"):
        return "java"
    if name in ("npm", "npx", "node", "yarn", "pnpm"):
        return "node"
    if name == "cargo":
        return "rust"
    if name == "go":
        return "go"
    if script is not None:
        if script.stem.lower() in ("gradlew", "mvnw"):
            return "java"
        try:
            body = script.read_text(encoding="utf-8", errors="ignore").lower()[:20000]
        except Exception:
            body = ""
        if "java_home" in body or re.search(r"\bjava(w)?(\.exe)?\b", body):
            return "java"
        if re.search(r"\b(npm|npx|node|yarn|pnpm)(\.cmd|\.exe)?\b", body):
            return "node"
        if re.search(r"\bcargo\b", body):
            return "rust"
        if re.search(r"\bgo (run|build)\b", body):
            return "go"
    return None


def missing_runtime_result(key: str, command: str) -> Dict[str, Any]:
    label = RUNTIMES[key]["label"]
    return {
        "success": False,
        "type": "missing_runtime",
        "runtime": key,
        "runtime_label": label,
        "command": command,
        "message": f"Для запуска нужно установить {label}: на компьютере не найдено подходящей версии.",
    }


async def install_runtime(key: str) -> Dict[str, Any]:
    """Ставит среду из белого списка через winget (Windows) или Homebrew (Mac)."""
    spec = RUNTIMES.get(key)
    if not spec:
        return {"success": False, "message": "Неизвестная среда выполнения."}
    if IS_WINDOWS:
        tool = shutil.which("winget")
        if not tool:
            return {"success": False, "message": "На компьютере нет winget (App Installer из Microsoft Store)."}
        cmd = [tool, "install", "--id", spec["winget"], "-e", "--silent",
               "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    else:
        tool = shutil.which("brew")
        if not tool:
            return {"success": False, "message": "На Mac не установлен Homebrew (https://brew.sh)."}
        cmd = [tool, "install", spec["brew"]]
    logger.info(f"Установка среды {spec['label']}: {' '.join(cmd[1:4])}")
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20 * 60)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Установка не завершилась за 20 минут."}
    except Exception as e:
        return {"success": False, "message": f"Не удалось запустить установщик: {e}"}
    output = (out or b"").decode(errors="ignore")
    refresh_env_from_system()
    if key == "python" and IS_WINDOWS:
        installed = bool(shutil.which("py")) and "3.12" in subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=15).stdout
    elif key == "python":
        installed = bool(shutil.which("python3.12"))
    else:
        installed = is_runtime_available(key)
    if installed:
        return {"success": True, "message": f"{spec['label']} установлена."}
    tail = "\n".join(line for line in output.splitlines() if line.strip())[-600:]
    return {"success": False, "message": f"Установщик завершился (код {proc.returncode}), но {spec['label']} не найдена.\n{tail}"}


_PY_NOT_ENTRY = {"setup.py", "noxfile.py", "conftest.py", "fabfile.py", "tasks.py", "build.py", "test.py", "tests.py", "manage_dev.py"}


def _python_entry(p: Path, project_name: str) -> Optional[str]:
    """Настоящая точка входа Python-проекта: аргумент для python ("main.py" или "-m пакет") или None (библиотека/документация)."""
    for n in ("main.py", "app.py", "run.py", "server.py", "cli.py", "__main__.py", "manage.py"):
        if (p / n).exists():
            return f"{n} runserver" if n == "manage.py" else n
    skip = _NON_APP_DIRS | _LAUNCH_SKIP_DIRS | {"scripts", "script", "benchmark", "examples", "samples", "site", "build", "dist"}
    packages = []
    for base in (p, p / "src"):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name.lower() not in skip and (d / "__main__.py").exists():
                packages.append(d.name)
    if packages:
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        best = next((pkg for pkg in packages if norm(pkg) in (norm(project_name), norm(p.name))), packages[0])
        return f"-m {best}"
    scripts = []
    for f in sorted(p.glob("*.py")):
        if f.name.lower() in _PY_NOT_ENTRY:
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", body):
            scripts.append(f.name)
    if len(scripts) == 1:
        return scripts[0]
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return next((s for s in scripts if norm(Path(s).stem) in (norm(project_name), norm(p.name))), None)


def _requires_python(p: Path) -> str:
    py_file = p / "pyproject.toml"
    if not py_file.exists():
        return ""
    try:
        import tomllib
        data = tomllib.loads(py_file.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""
    spec = (data.get("project") or {}).get("requires-python") or ""
    if not spec:
        spec = (((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}).get("python") or ""
    return str(spec)


def python_version_ok(spec: str, version: tuple) -> bool:
    """Проверка версии (major, minor) по requires-python: >=, >, <=, <, ==, !=, ~=, ^ (poetry)."""
    if not spec.strip():
        return True
    v = tuple(version[:2])
    for clause in re.split(r"[,|]", spec):
        m = re.match(r"\s*(~=|==|!=|>=|<=|>|<|\^)?\s*(\d+)(?:\.(\d+|\*))?", clause)
        if not m:
            continue
        op = m.group(1) or "=="
        major = int(m.group(2))
        minor_raw = m.group(3)
        minor = 0 if minor_raw in (None, "*") else int(minor_raw)
        target = (major, minor)
        if op == "==" and (minor_raw in (None, "*")) and v[0] != major:
            return False
        if op == "==" and minor_raw not in (None, "*") and v != target:
            return False
        if op == "!=" and v == target:
            return False
        if op == ">=" and v < target:
            return False
        if op == ">" and v <= target:
            return False
        if op == "<=" and v > target:
            return False
        if op == "<" and v >= target:
            return False
        if op in ("~=", "^") and (v < target or v[0] != major):
            return False
    return True


def find_python_for_project(p: Path) -> Optional[str]:
    """Интерпретатор, подходящий под requires-python проекта: текущий или один из установленных (py -0p)."""
    spec = _requires_python(p)
    if python_version_ok(spec, sys.version_info):
        return sys.executable
    if not IS_WINDOWS:
        for minor in range(14, 8, -1):
            exe = shutil.which(f"python3.{minor}")
            if exe and python_version_ok(spec, (3, minor)):
                return exe
        return None
    if not shutil.which("py"):
        return None
    try:
        out = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    found = []
    for line in out.splitlines():
        m = re.search(r"-V:(\d+)\.(\d+)\S*\s+\*?\s*(\S.*python(?:w)?\.exe)", line, re.IGNORECASE)
        if m:
            found.append(((int(m.group(1)), int(m.group(2))), m.group(3).strip()))
    for ver, path in sorted(found, reverse=True):
        if python_version_ok(spec, ver) and Path(path).exists():
            return path
    return None


def _venv_python_version(venv_dir: Path) -> Optional[tuple]:
    try:
        cfg = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", cfg, re.MULTILINE)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


def _pyproject_console_script(p: Path, project_name: str) -> Optional[str]:
    """Имя консольной команды пакета: pyproject.toml ([project.scripts], poetry), setup.cfg или setup.py."""
    scripts: Dict[str, Any] = {}
    py_file = p / "pyproject.toml"
    if py_file.exists():
        try:
            import tomllib
            data = tomllib.loads(py_file.read_text(encoding="utf-8", errors="ignore"))
            scripts.update((data.get("project") or {}).get("scripts") or {})
            scripts.update(((data.get("tool") or {}).get("poetry") or {}).get("scripts") or {})
        except Exception:
            pass
    if not scripts and (p / "setup.cfg").exists():
        try:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(p / "setup.cfg", encoding="utf-8")
            raw = cfg.get("options.entry_points", "console_scripts", fallback="")
            for line in raw.splitlines():
                if "=" in line:
                    scripts[line.split("=", 1)[0].strip()] = line.split("=", 1)[1].strip()
        except Exception:
            pass
    if not scripts and (p / "setup.py").exists():
        try:
            body = (p / "setup.py").read_text(encoding="utf-8", errors="ignore")
            block = re.search(r"console_scripts['\"]?\s*[:=]\s*\[([^\]]*)\]", body)
            for name in re.findall(r"['\"]\s*([\w.-]+)\s*=", block.group(1) if block else ""):
                scripts[name] = ""
        except Exception:
            pass
    if not scripts:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for name in scripts:
        if norm(name) in (norm(project_name), norm(p.name)):
            return name
    return next(iter(scripts))
_INSTALLER_MARKERS = ("setup", "install", "installer")
_JUNK_EXE_MARKERS = (
    "unins", "uninst", "crash", "update", "updater", "helper", "elevate",
    # служебные файлы, которые приносят с собой рантаймы и упаковщики (.NET, Chromium, Squirrel, VC++)
    "createdump", "squirrel", "vc_redist", "vcredist", "dotnet-", "notification_helper", "7za",
)


def _has_marker(filename: str, markers, project_name: str = "") -> bool:
    """Служебное слово в имени файла, если только оно не часть названия самого проекта
    (у Bulk Crap Uninstaller главный файл — BCUninstaller.exe, это не деинсталлятор)."""
    low = filename.lower()
    proj = re.sub(r"[^a-z0-9]", "", (project_name or "").lower())
    return any(m in low and m not in proj for m in markers)


def _iter_files(root: Path, pattern: str, max_depth: int = 4) -> List[Path]:
    """Рекурсивный glob с ограничением глубины и пропуском служебных папок (.venv, node_modules, .git)."""
    found: List[Path] = []
    try:
        for f in root.rglob(pattern):
            try:
                rel_parts = f.relative_to(root).parts
            except Exception:
                continue
            if len(rel_parts) > max_depth:
                continue
            if any(part.lower() in _LAUNCH_SKIP_DIRS for part in rel_parts[:-1]):
                continue
            if f.is_file():
                found.append(f)
    except Exception:
        pass
    return found


def _rank_exe(f: Path, root: Path, project_name: str) -> int:
    """Оценивает, насколько .exe похож на главный исполняемый файл проекта (универсально, без хардкода имён)."""
    fn = f.stem.lower()
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    n_fn, n_root, n_proj = norm(fn), norm(root.name), norm(project_name)
    score = 0
    if n_fn and (n_fn == n_root or n_fn == n_proj):
        score += 100
    elif n_fn and ((n_root and (n_root in n_fn or n_fn in n_root)) or (n_proj and (n_proj in n_fn or n_fn in n_proj))):
        score += 50
    try:
        score -= (len(f.relative_to(root).parts) - 1) * 5
    except Exception:
        pass
    try:
        score += min(int(f.stat().st_size / (1024 * 1024)), 20)  # крупный бинарник скорее главный
    except Exception:
        pass
    # Если рядом есть версия с окном, консольный вариант (BCU-console.exe, app-cli.exe) — не главный
    if set(re.split(r"[^a-z0-9]+", fn)) & {"console", "cli", "cmd"}:
        score -= 30
    # Признаки главной программы, не зависящие от имени (у PowerShell главный файл — pwsh.exe):
    # .NET-приложение (<имя>.runtimeconfig.json / <имя>.exe.config) или Electron (resources/app.asar)
    if (f.parent / f"{f.stem}.runtimeconfig.json").exists() or (f.parent / f"{f.name}.config").exists():
        score += 40
    if (f.parent / "resources" / "app.asar").exists():
        score += 40
    return score


async def universal_windows_launch(p: Path, project_name: str, repo_url: str = "") -> Optional[Dict[str, Any]]:
    """
    Универсальная воронка запуска любого проекта на Windows (сверху вниз):
      1. Готовый .exe (с защитой от зависших копий taskkill и интерактивным запуском os.startfile / DETACHED_PROCESS)
      2. Установщик .msi / *setup*.exe -> os.startfile / launch_gui_executable
      3. Скрипты запуска .bat / .cmd -> запуск в окне консоли
      4. Локальный .html без бэкенда -> os.startfile (браузер)
      5. Скрипты: Python (.venv + main/app/run/server.py), Rust (cargo run), Node (npm start), Go (go run .)
      6. Резерв: видимое окно PowerShell с активированным окружением (никогда не молчим)
    Возвращает None только для реальных локальных веб-серверов, чтобы их поднял штатный пайплайн (порт + браузер).
    """
    if not IS_WINDOWS or not p.exists():
        return None

    # Глубокий рекурсивный поиск исполняемых файлов
    all_exe = [f for f in _iter_files(p, "*.exe") if exe_runs_here(f)]
    installers = [f for f in all_exe if _has_marker(f.name, _INSTALLER_MARKERS, project_name)] + _iter_files(p, "*.msi")
    apps = [f for f in all_exe if f not in installers and not _has_marker(f.name, _JUNK_EXE_MARKERS, project_name)]

    # Если ни программы, ни установщика нет, но есть GitHub — пробуем скачать готовый релиз
    if not apps and not installers and repo_url and "github.com" in repo_url:
        rel_bin = await download_github_release_binary(repo_url, p)
        if rel_bin and rel_bin.exists():
            low = rel_bin.name.lower()
            if low.endswith(".msi") or _has_marker(low, _INSTALLER_MARKERS, project_name):
                installers = [rel_bin]
            elif low.endswith(".exe"):
                apps = [rel_bin]
            else:
                apps = [f for f in _iter_files(p, "*.exe") if exe_runs_here(f) and not _has_marker(f.name, _INSTALLER_MARKERS + _JUNK_EXE_MARKERS, project_name)]

    # ШАГ 1: готовая программа (.exe) — различаем консольную (CUI) и оконную (GUI)
    if apps:
        apps.sort(key=lambda f: _rank_exe(f, p, project_name), reverse=True)
        target = apps[0]
        if is_windows_console_exe(target):
            logger.info(f"[Воронка 1/6] Консольный .exe {target} -> запуск в окне PowerShell")
            escaped_t = str(target).replace("'", "''")
            if launch_in_terminal(f"& '{escaped_t}'", p):
                return {
                    "success": True,
                    "type": "terminal",
                    "command": target.name,
                    "path": str(target),
                    "message": f"Консольная утилита {target.name} запущена в окне терминала на вашем Windows ПК!"
                }
        else:
            logger.info(f"[Воронка 1/6] Запуск GUI программы {target}")
            try:
                kill_process_by_name(target.name)
                if launch_gui_executable(target):
                    return {
                        "success": True,
                        "type": "windows_app",
                        "app_name": target.name,
                        "path": str(target),
                        "message": f"Программа {target.name} запущена на вашем Windows ПК!"
                    }
            except Exception as e:
                logger.error(f"Ошибка запуска {target}: {e}")

    # ШАГ 2: установщик (.msi / setup.exe)
    if installers:
        installers.sort(key=lambda f: (f.suffix.lower() != ".msi", -_rank_exe(f, p, project_name)))
        target = installers[0]
        logger.info(f"[Воронка 2/6] Запуск установщика {target}")
        try:
            if target.suffix.lower() == ".exe":
                kill_process_by_name(target.name)
            if launch_gui_executable(target):
                return {
                    "success": True,
                    "type": "installer",
                    "app_name": target.name,
                    "path": str(target),
                    "message": f"Установщик {target.name} запущен на вашем Windows ПК. Следуйте шагам мастера установки."
                }
        except Exception as e:
            logger.error(f"Ошибка запуска установщика {target}: {e}")

    # ШАГ 3: готовые пакетные скрипты запуска (.bat / .cmd)
    bat_candidates = [
        f for f in (_iter_files(p, "*.bat", max_depth=2) + _iter_files(p, "*.cmd", max_depth=2))
        if not _has_marker(f.name, _JUNK_EXE_MARKERS, project_name)
        and f.stem.lower() not in _BUILD_SCRIPT_NAMES
        and not any(part.lower() in _NON_APP_DIRS for part in f.relative_to(p).parts[:-1])
    ]
    if bat_candidates:
        def _rank_bat(f: Path) -> int:
            fn = f.stem.lower()
            if fn in ("run", "start", "launch", "app", "main"):
                return 100
            if project_name.lower() in fn:
                return 80
            return 10 - len(f.relative_to(p).parts)
        bat_candidates.sort(key=_rank_bat, reverse=True)
        target_bat = bat_candidates[0]
        rel_cmd = f".\\{target_bat.relative_to(p)}"
        needed = detect_required_runtime(rel_cmd, target_bat)
        if needed and not is_runtime_available(needed):
            logger.info(f"[Воронка 3/6] Для {rel_cmd} не хватает среды: {needed}")
            return missing_runtime_result(needed, rel_cmd)
        logger.info(f"[Воронка 3/6] Запуск .bat/.cmd скрипта: {rel_cmd} в {p}")
        if launch_in_terminal(rel_cmd, p):
            return {
                "success": True,
                "type": "terminal",
                "command": target_bat.name,
                "message": f"Скрипт {target_bat.name} запущен в окне консоли на вашем Windows ПК!"
            }

    has_backend = any([
        (p / "package.json").exists(), (p / "Cargo.toml").exists(), (p / "go.mod").exists(),
        (p / "requirements.txt").exists(), (p / "pyproject.toml").exists(),
        list(p.glob("*.py")), list(p.glob("*.rs")), list(p.glob("*.go")),
    ])

    # ШАГ 4: локальный HTML без бэкенда
    if not has_backend:
        html_files = _iter_files(p, "*.html", max_depth=3) + _iter_files(p, "*.htm", max_depth=3)
        if html_files:
            html_files.sort(key=lambda f: (f.name.lower() != "index.html", len(f.relative_to(p).parts)))
            target = html_files[0]
            logger.info(f"[Воронка 4/6] Открытие HTML {target}")
            try:
                if hasattr(os, "startfile"):
                    os.startfile(str(target))
                else:
                    open_url(str(target))
                return {
                    "success": True,
                    "type": "web_page",
                    "html_path": str(target),
                    "message": f"Веб-страница {target.name} открыта в браузере на вашем Windows ПК!"
                }
            except Exception as e:
                logger.error(f"Ошибка открытия {target}: {e}")

    # Проверяем, является ли проект локальным веб-сервером (только реальные локальные фреймворки)
    is_local_web = False
    if (p / "requirements.txt").exists():
        try:
            req_l = (p / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in req_l for k in ["streamlit", "gradio", "fastapi", "uvicorn", "flask"]):
                is_local_web = True
        except Exception:
            pass
    if (p / "package.json").exists():
        try:
            pkg_l = (p / "package.json").read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in pkg_l for k in ["express", "fastify", "next", "vite", "nuxt"]):
                is_local_web = True
        except Exception:
            pass

    if is_local_web:
        if (p / "package.json").exists() and not is_runtime_available("node"):
            return missing_runtime_result("node", "npm install")
        return None  # веб-сервер поднимает штатный пайплайн (определение порта + открытие браузера)

    # ШАГ 5: скрипты на Python / Rust / Node / Go
    run_cmd = ""
    console_script = _pyproject_console_script(p, project_name)
    is_python_project = (p / "pyproject.toml").exists() or (p / "requirements.txt").exists() or (p / "setup.py").exists()
    if is_python_project and not find_python_for_project(p):
        logger.info(f"[Воронка 5/6] Нет Python под requires-python={_requires_python(p)}")
        return missing_runtime_result("python", console_script or "pip install")
    if console_script:
        script_exe = p / ".venv" / "Scripts" / f"{console_script}.exe"
        if not script_exe.exists():
            await prepare_python_env(str(p))
        if script_exe.exists():
            escaped = str(script_exe).replace("'", "''")
            logger.info(f"[Воронка 5/6] Консольная команда из pyproject.toml: {console_script}")
            if launch_in_terminal(f"& '{escaped}'", p):
                return {
                    "success": True,
                    "type": "terminal",
                    "command": console_script,
                    "message": f"Проект {project_name} запущен командой {console_script}"
                }
    py_entry = _python_entry(p, project_name)

    if py_entry:
        if not (p / ".venv" / "Scripts" / "python.exe").exists() and is_python_project:
            await prepare_python_env(str(p))
        venv_py = p / ".venv" / "Scripts" / "python.exe"
        if not venv_py.exists():
            venv_py = p / "venv" / "Scripts" / "python.exe"
        run_cmd = f"python {py_entry}" if venv_py.exists() else f'"{find_python_for_project(p) or sys.executable}" {py_entry}'
    elif (p / "Cargo.toml").exists():
        run_cmd = "cargo run --release"
    elif (p / "package.json").exists():
        try:
            scripts = json.loads((p / "package.json").read_text(encoding="utf-8", errors="ignore")).get("scripts", {})
            if "start" in scripts:
                run_cmd = "npm start"
            elif "dev" in scripts:
                run_cmd = "npm run dev"
        except Exception:
            pass
    elif (p / "go.mod").exists():
        # main-пакет часто лежит не в корне, а в cmd/<имя>/main.go
        root_main = any("package main" in f.read_text(encoding="utf-8", errors="ignore")[:2000] for f in p.glob("*.go"))
        cmd_mains = sorted(d.name for d in (p / "cmd").iterdir() if d.is_dir() and (d / "main.go").exists()) if (p / "cmd").is_dir() else []
        if root_main:
            run_cmd = "go run ."
        elif cmd_mains:
            norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
            # служебные команды (cmd/gen-docs, cmd/tools…) не главные; из остальных — совпадающая по имени или самая короткая
            real = [c for c in cmd_mains if not re.search(r"^gen|doc|test|tool|script|bench|example|migrat", c.lower())] or cmd_mains
            pick = next((c for c in real if norm(c) in (norm(project_name), norm(p.name))), min(real, key=len))
            run_cmd = f"go run ./cmd/{pick}"

    if run_cmd:
        needed = detect_required_runtime(run_cmd)
        if needed and not is_runtime_available(needed):
            logger.info(f"[Воронка 5/6] Для {run_cmd} не хватает среды: {needed}")
            return missing_runtime_result(needed, run_cmd)
        logger.info(f"[Воронка 5/6] PowerShell: {run_cmd} в {p}")
        if launch_in_terminal(run_cmd, p):
            return {
                "success": True,
                "type": "terminal",
                "command": run_cmd,
                "message": f"Проект {project_name} запущен в окне PowerShell: {run_cmd}"
            }

    # ШАГ 6: точки входа нет (библиотека, документация, проект со сборкой через Makefile) — не запускаем
    # случайный файл, а открываем консоль в папке проекта
    logger.info(f"[Воронка 6/6] Точка входа не найдена, открытие консоли в {p}")
    launch_in_terminal("", p)
    return {
        "success": True,
        "type": "no_entrypoint",
        "command": "PowerShell в папке проекта",
        "message": (
            f"У {project_name} нет готовой программы для запуска: похоже, это библиотека, документация "
            f"или проект, который нужно собрать вручную. Консоль открыта в папке проекта — команды есть в README."
        )
    }


async def auto_run_project_on_mac(
    local_path: str,
    project_name: str,
    custom_cmd: Optional[str] = None,
    repo_url: str = ""
) -> Dict[str, Any]:
    """
    Выполняет утилиту 'под ключ' в один клик:
    - Для App Store / Mac приложений: открывает в Mac App Store или запускает установленное приложение.
    - Для расширений браузера (Chrome/Edge/Safari): открывает chrome://extensions и папку в Finder/Проводнике.
    - Для расширений VS Code (.vsix): устанавливает в редактор.
    - Для нативных .app бандлов и .dmg/.pkg / .exe/.msi: открывает программу/установщик на экране.
    - Для веб-интерфейсов (Node.js, Python FastAPI/Streamlit/Flask): поднимает сервер, открывает в браузере и дает ссылку для телефона.
    - Для консольных/скриптовых утилит: запускает в активном окне терминала.
    """
    p = Path(local_path)
    if not p.exists():
        return {"success": False, "message": f"Директория {local_path} не найдена."}

    # Windows: универсальная воронка запуска для любого проекта
    if IS_WINDOWS and not custom_cmd:
        uni_res = await universal_windows_launch(p, project_name, repo_url=repo_url)
        if uni_res is not None:
            return uni_res

    clean_app_name = project_name.replace("_", " ").replace("-", " ")

    # 1. Проверка на Mac App Store ссылку
    if repo_url and ("apps.apple.com" in repo_url or repo_url.startswith("macappstore://")):
        installed_app = None
        for a_dir in [Path("/Applications"), Path.home() / "Applications"]:
            if a_dir.exists():
                for a in a_dir.glob("*.app"):
                    if clean_app_name.lower() in a.name.lower() or a.stem.lower() in clean_app_name.lower():
                        installed_app = a
                        break
            if installed_app:
                break
        
        if installed_app:
            logger.info(f"Найдено установленное приложение {installed_app}, запускаем...")
            await asyncio.to_thread(open_directory, installed_app)
            return {
                "success": True,
                "type": "mac_app",
                "app_name": installed_app.name,
                "message": f"Приложение {installed_app.name} успешно запущено на вашем Mac!"
            }
        else:
            logger.info(f"Открываем страницу в Mac App Store: {repo_url}")
            await asyncio.to_thread(open_url, repo_url)
            return {
                "success": True,
                "type": "app_store",
                "app_name": clean_app_name,
                "url": repo_url,
                "message": f"Страница {clean_app_name} открыта в Mac App Store на вашем Mac!"
            }

    # 2. Проверка на наличие готового .app бандла внутри папки проекта
    app_bundles = list(p.glob("*.app")) + list(p.glob("**/*.app"))
    if app_bundles:
        target_app = app_bundles[0]
        logger.info(f"Найден .app бандл: {target_app}, запускаем...")
        await asyncio.to_thread(open_directory, target_app)
        return {
            "success": True,
            "type": "mac_app",
            "app_name": target_app.name,
            "message": f"Приложение {target_app.name} запущено на экране Mac!"
        }

    # 3. Проверка на Windows исполняемые файлы (.exe, .msi)
    if IS_WINDOWS:
        exe_files = [f for f in _iter_files(p, "*.exe") if exe_runs_here(f)] + _iter_files(p, "*.msi")
        if not exe_files and repo_url and "github.com" in repo_url:
            rel_bin = await download_github_release_binary(repo_url, p)
            if rel_bin and rel_bin.exists():
                exe_files = [rel_bin]

        if exe_files:
            def _exe_score(f: Path) -> int:
                fn = f.name.lower()
                pn = p.name.lower()
                proj_n = (project_name or "").lower()
                if any(x in fn for x in ("unins", "uninst", "crash", "update")):
                    return -100
                score = 0
                if fn == f"{pn}.exe" or fn == f"{proj_n}.exe":
                    score += 100
                elif pn in fn or proj_n in fn:
                    score += 50
                if fn.endswith(".exe"):
                    score += 20
                elif fn.endswith(".msi"):
                    score += 10
                try:
                    depth = len(f.relative_to(p).parts)
                    score -= depth * 5
                except Exception:
                    pass
                return score

            exe_files.sort(key=_exe_score, reverse=True)
            target_exe = exe_files[0]
            logger.info(f"Найден исполняемый файл Windows: {target_exe}, запускаем...")
            if target_exe.suffix.lower() == ".exe" and is_windows_console_exe(target_exe) and not any(m in target_exe.name.lower() for m in _INSTALLER_MARKERS):
                escaped_t = str(target_exe).replace("'", "''")
                if launch_in_terminal(f"& '{escaped_t}'", p):
                    return {
                        "success": True,
                        "type": "terminal",
                        "command": target_exe.name,
                        "message": f"Консольная утилита {target_exe.name} запущена в окне терминала на вашем Windows ПК!"
                    }
            try:
                kill_process_by_name(target_exe.name)
                if launch_gui_executable(target_exe):
                    return {
                        "success": True,
                        "type": "windows_app",
                        "app_name": target_exe.name,
                        "message": f"Программа {target_exe.name} успешно запущена на вашем Windows ПК!"
                    }
                return {"success": False, "message": f"Не удалось запустить {target_exe.name}"}
            except Exception as e:
                logger.error(f"Ошибка запуска {target_exe}: {e}")
                return {"success": False, "message": f"Ошибка запуска {target_exe.name}: {e}"}

    # 3.1 Проверка на .dmg или .pkg установщики (macOS)
    installers = list(p.glob("*.dmg")) + list(p.glob("*.pkg"))
    if installers:
        target_inst = installers[0]
        logger.info(f"Найден установщик: {target_inst}, открываем...")
        await asyncio.to_thread(open_directory, target_inst)
        return {
            "success": True,
            "type": "installer",
            "message": f"Установочный файл {target_inst.name} открыт на {OS_NAME}."
        }

    # 4. Проверка на расширение для браузера (Chrome / Edge / Brave / Safari / Firefox)
    manifests = list(p.glob("manifest.json")) + list(p.glob("*/manifest.json")) + list(p.glob("dist/manifest.json")) + list(p.glob("src/manifest.json"))
    is_extension = False
    extension_dir = p
    for m in manifests:
        try:
            m_data = json.loads(m.read_text(encoding="utf-8", errors="ignore"))
            if "manifest_version" in m_data:
                is_extension = True
                extension_dir = m.parent
                break
        except Exception:
            pass

    if is_extension:
        logger.info(f"Обнаружено браузерное расширение в {extension_dir}!")
        if (p / "package.json").exists() and not (p / "dist").exists() and not (p / "build").exists():
            try:
                pkg_json = json.loads((p / "package.json").read_text(encoding="utf-8", errors="ignore"))
                if "build" in pkg_json.get("scripts", {}):
                    logger.info("Сборка расширения через npm run build...")
                    npm_bin = shutil.which("npm") or ("npm.cmd" if IS_WINDOWS else "npm")
                    proc_i = await asyncio.create_subprocess_exec(npm_bin, "install", cwd=str(p), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        await asyncio.wait_for(proc_i.communicate(), timeout=60)
                    except asyncio.TimeoutError:
                        proc_i.kill()
                    proc_b = await asyncio.create_subprocess_exec(npm_bin, "run", "build", cwd=str(p), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        await asyncio.wait_for(proc_b.communicate(), timeout=60)
                    except asyncio.TimeoutError:
                        proc_b.kill()
                    if (p / "dist" / "manifest.json").exists():
                        extension_dir = p / "dist"
                    elif (p / "build" / "manifest.json").exists():
                        extension_dir = p / "build"
            except Exception as e:
                logger.warning(f"Ошибка автоматической сборки расширения: {e}")

        # Открываем страницу расширений в Google Chrome или системном браузере
        await asyncio.to_thread(open_url, "chrome://extensions")

        # Открываем папку с расширением в Finder/Проводнике
        await asyncio.to_thread(open_directory, extension_dir)

        fm_name = "Finder" if IS_MAC else "Проводнике"
        return {
            "success": True,
            "type": "browser_extension",
            "message": f"Расширение для браузера {project_name} открыто на {OS_NAME}!",
            "details": (
                f"• На экране {OS_NAME} открыта страница <code>chrome://extensions</code>\n"
                f"• Папка с готовым расширением открыта в {fm_name}\n\n"
                "<b>Как подключить за 5 секунд:</b>\n"
                "1. Включите тумблер <b>«Режим разработчика»</b> (в правом верхнем углу)\n"
                "2. Нажмите кнопку <b>«Загрузить распакованное»</b> и выберите открытую папку (или просто перетащите папку в окно браузера)."
            )
        }

    # 5. Проверка на расширение VS Code (.vsix)
    vsix_files = list(p.glob("*.vsix"))
    if vsix_files:
        v_file = vsix_files[0]
        try:
            code_bin = shutil.which("code") or ("code.cmd" if IS_WINDOWS else "code")
            proc_code = await asyncio.create_subprocess_exec(code_bin, "--install-extension", str(v_file), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc_code.communicate()
            return {
                "success": True,
                "type": "vscode_extension",
                "message": f"Расширение {v_file.name} установлено в VS Code на вашем {OS_NAME}!"
            }
        except Exception:
            pass

    # 6. Проверка на живой онлайн веб-сервис (ТОЛЬКО если в папке нет исполняемых файлов или исходного кода)
    effective_run_url = repo_url
    if not effective_run_url:
        try:
            from bot.memory_service import find_repo_url_in_memory
            effective_run_url = find_repo_url_in_memory(project_name)
        except Exception:
            pass

    has_local_code_or_executables = bool(
        _iter_files(p, "*.exe") or _iter_files(p, "*.msi") or
        _iter_files(p, "*.bat") or _iter_files(p, "*.cmd") or
        _iter_files(p, "*.py") or _iter_files(p, "*.rs") or _iter_files(p, "*.go") or
        _iter_files(p, "*.js") or _iter_files(p, "*.ts") or _iter_files(p, "*.html") or
        (p / "Cargo.toml").exists() or (p / "package.json").exists() or (p / "requirements.txt").exists() or (p / "pyproject.toml").exists()
    )

    if not has_local_code_or_executables and is_online_web_service(effective_run_url):
        logger.info(f"Обнаружен онлайн веб-сервис: {effective_run_url}, открываем в браузере...")
        await asyncio.to_thread(open_url, effective_run_url)
        await asyncio.to_thread(open_directory, p)
        return {
            "success": True,
            "type": "online_web",
            "url": effective_run_url,
            "online_url": effective_run_url,
            "project_name": project_name,
            "message": f"Онлайн-сервис {project_name} открыт в браузере на вашем {'Windows ПК' if IS_WINDOWS else OS_NAME}!"
        }

    # 6.1 Проверка на локальную HTML / Web страницу или сохраненный веб-документ (только при отсутствии кода/приложений)
    html_files = list(p.glob("*.html")) + list(p.glob("*.htm"))
    for txt in p.glob("*.txt"):
        try:
            head_sample = txt.read_text(encoding="utf-8", errors="ignore")[:300].lower()
            if "<!doctype html" in head_sample or "<html" in head_sample:
                renamed_html = txt.with_suffix(".html")
                txt.rename(renamed_html)
                html_files.append(renamed_html)
                break
        except Exception:
            pass

    code_files = list(p.glob("*.py")) + list(p.glob("*.js")) + list(p.glob("*.ts")) + list(p.glob("*.go")) + list(p.glob("*.rs"))
    if not has_local_code_or_executables and not code_files and html_files:
        target_html = html_files[0]
        html_abs = str(target_html.resolve())
        logger.info(f"Обнаружена веб-страница/презентация: {html_abs}, открываем...")
        await asyncio.to_thread(open_url, html_abs)
        await asyncio.to_thread(open_directory, p)
        return {
            "success": True,
            "type": "web_page",
            "html_path": html_abs,
            "message": f"Веб-страница {project_name} открыта в браузере на вашем {'Windows ПК' if IS_WINDOWS else OS_NAME}!"
        }

    # 7. Анализ через генератор гайдов (Node.js, Python, Docker, Web frameworks)
    guide = await generate_usage_guide(local_path, project_name, repo_url=repo_url)
    cmd = custom_cmd or guide.get("launch_command") or ("dir" if IS_WINDOWS else "ls -la")
    is_web = guide.get("is_web", False)
    expected_port = guide.get("web_port") or 0
    p_type = guide.get("project_type") or classify_repository_type(local_path, "", repo_url)

    # Автоматическая подготовка окружения
    if (p / "requirements.txt").exists() and not (p / ".venv").exists():
        logger.info(f"Автоматическая подготовка Python окружения для {project_name}...")
        await prepare_python_env(local_path)
    elif ((p / "pyproject.toml").exists() or (p / "setup.py").exists()) and not (p / ".venv").exists():
        logger.info(f"Автоматическая подготовка Python окружения (setup/pyproject) для {project_name}...")
        await prepare_python_env(local_path)
    
    if (p / "package.json").exists() and not (p / "node_modules").exists():
        logger.info(f"Автоматический npm install для {project_name}...")
        try:
            p_npm = await asyncio.create_subprocess_shell("npm install", cwd=str(p))
            await asyncio.wait_for(p_npm.communicate(), timeout=45.0)
        except Exception as e:
            logger.warning(f"Ошибка npm install: {e}")

    # 8. Если это библиотека / MCP сервер (library)
    if p_type == "library":
        import_mod = project_name.lower().replace("-", "_")
        import_code = f"import {import_mod}"
        return {
            "success": True,
            "type": "library",
            "import_code": import_code,
            "path": str(p),
            "command": "pip install -e .",
            "guide": guide,
            "message": f"Библиотека {project_name} готова к подключению на {OS_NAME}!",
            "details": f"Для использования импортируйте модуль в коде:\n<code>{import_code}</code>"
        }

    # 9. Если это документация / база знаний (docs)
    if p_type == "docs":
        open_directory(p)
        return {
            "success": True,
            "type": "docs",
            "path": str(p),
            "summary": guide.get("summary", ""),
            "guide": guide,
            "message": f"Документация {project_name} открыта на {OS_NAME}."
        }

    # 10. Если это веб-сервис
    if is_web or p_type == "web":
        res = await launch_project_on_mac(local_path, cmd)
        pid = res.get("pid")

        # Если команда завершилась с ошибкой, пробуем запасные варианты (app.py, main.py, etc.)
        if not res.get("is_running") and not res.get("success"):
            for c in ["app.py", "main.py", "run.py", "server.py", "index.py"]:
                if (p / c).exists() and not cmd.endswith(c):
                    logger.info(f"Команда {cmd} не запустилась. Пробуем fallback: python {c}...")
                    cmd = f"python {c}"
                    res = await launch_project_on_mac(local_path, cmd)
                    pid = res.get("pid")
                    if res.get("is_running") or res.get("success"):
                        break

        if not res.get("is_running") and not res.get("success"):
            return {
                "success": False,
                "type": "error",
                "message": res.get("message", "Не удалось запустить веб-сервер."),
                "output": res.get("output", "")
            }

        local_ip = get_mac_local_ip()
        port = 0

        # Даем серверу до 4 секунд на привязку порта и проверяем сокетом / psutil
        for _ in range(8):
            await asyncio.sleep(0.5)
            if pid:
                port = get_process_listening_port(pid)
                if port:
                    break
            if expected_port:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.2)
                if sock.connect_ex(('127.0.0.1', int(expected_port))) == 0:
                    port = int(expected_port)
                    sock.close()
                    break
                sock.close()

        if not port:
            port = int(expected_port or 8899 or 5000 or 3000)

        if pid and pid in ACTIVE_PROCESS_INFO:
            ACTIVE_PROCESS_INFO[pid]["port"] = port

        local_web_url = f"http://localhost:{port}"
        mobile_web_url = f"http://{local_ip}:{port}"

        open_url(local_web_url)

        return {
            "success": True,
            "type": "web",
            "is_running": True,
            "pid": pid,
            "port": port,
            "local_url": local_web_url,
            "mobile_url": mobile_web_url,
            "command": cmd,
            "guide": guide,
            "message": f"Веб-интерфейс запущен и открыт в браузере на {OS_NAME}!"
        }

    # 11. Если это десктопное приложение (gui)
    if p_type == "gui":
        exe_files = _iter_files(p, "*.exe") + _iter_files(p, "*.msi")
        if IS_WINDOWS and not exe_files and repo_url and "github.com" in repo_url:
            rel_bin = await download_github_release_binary(repo_url, p)
            if rel_bin and rel_bin.exists():
                exe_files = [rel_bin]

        if IS_WINDOWS and exe_files:
            try:
                target_exe = exe_files[0]
                if target_exe.suffix.lower() == ".exe" and is_windows_console_exe(target_exe) and not any(m in target_exe.name.lower() for m in _INSTALLER_MARKERS):
                    escaped_t = str(target_exe).replace("'", "''")
                    launch_in_terminal(f"& '{escaped_t}'", p)
                    return {
                        "success": True,
                        "type": "terminal",
                        "command": target_exe.name,
                        "message": f"Консольная утилита {target_exe.name} запущена в окне терминала на вашем Windows ПК!"
                    }
                kill_process_by_name(target_exe.name)
                if launch_gui_executable(target_exe):
                    return {
                        "success": True,
                        "type": "windows_app",
                        "app_name": target_exe.name,
                        "path": str(target_exe),
                        "message": f"Программа {target_exe.name} запущена на вашем Windows ПК!"
                    }
            except Exception as e:
                logger.error(f"Ошибка запуска exe {exe_files[0]}: {e}")

        term_ok = launch_in_terminal(cmd, p.resolve())
        return {
            "success": True,
            "type": "gui",
            "command": cmd,
            "message": f"Приложение {project_name} запущено на {OS_NAME}!"
        }

    # 12. Если это консольная программа или CLI утилита (cli) или любая другая команда:
    term_ok = launch_in_terminal(cmd, p.resolve())
    open_directory(p)
    return {
        "success": True,
        "type": "terminal",
        "command": cmd,
        "message": f"Утилита {project_name} запущена на {OS_NAME}!",
        "details": f"Окно терминала и папка проекта открыты на экране {OS_NAME}."
    }

def stop_process(pid: int) -> bool:
    """Останавливает процесс по PID."""
    ACTIVE_PROCESS_INFO.pop(pid, None)
    proc = ACTIVE_PROCESSES.pop(pid, None)
    if proc:
        try:
            proc.terminate()
            return True
        except Exception:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False

def stop_project_processes_by_path(project_path: str) -> int:
    """Останавливает любые фоновые процессы, запущенные из указанной директории."""
    stopped_count = 0
    clean_path = str(Path(project_path).resolve()).lower()
    my_pid = os.getpid()

    # 1. Поиск процессов через psutil
    procs_to_stop = []
    for proc in psutil.process_iter(['pid', 'name', 'cwd', 'cmdline']):
        try:
            if proc.pid == my_pid:
                continue
            proc_cwd = ""
            try:
                proc_cwd = str(proc.cwd() or "").lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
                pass

            proc_cmdline = ""
            try:
                cmdline = proc.cmdline() or []
                proc_cmdline = " ".join(cmdline).lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
                pass

            if (proc_cwd and clean_path in proc_cwd) or (proc_cmdline and clean_path in proc_cmdline):
                procs_to_stop.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue

    # 2. Мягко останавливаем proc.terminate()
    for proc in procs_to_stop:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            pass

    # Ждем через psutil.wait_procs
    gone, alive = psutil.wait_procs(procs_to_stop, timeout=3.0)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            pass

    stopped_count = len(procs_to_stop)

    # 3. Очищаем только соответствующие PID из ACTIVE_PROCESSES и ACTIVE_PROCESS_INFO
    stopped_pids = {p.pid for p in procs_to_stop}
    for pid in list(ACTIVE_PROCESSES.keys()):
        if pid in stopped_pids:
            ACTIVE_PROCESSES.pop(pid, None)
            ACTIVE_PROCESS_INFO.pop(pid, None)

    return stopped_count


def _handle_remove_readonly(func, path, _excinfo):
    """Снимает флаг Read-Only (например, с файлов .git/objects/pack на Windows) перед удалением."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def uninstall_service(project_name: str, local_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Полностью удаляет проект / сервис:
    1. Останавливает все запущенные процессы сервиса.
    2. Удаляет директорию проекта из DOWNLOADS_DIR/<project_name>.
    3. Очищает очередь задач Antigravity (QUEUE_FILE).
    4. Удаляет инструмент из реестров (INSTALLED_TOOLS.md, CLAUDE.md, JSON).
    """
    target_path = Path(local_path) if local_path else (DOWNLOADS_DIR / project_name)
    if not is_safe_project_path(target_path):
        logger.error(f"Небезопасный путь для удаления: {target_path}")
        return {
            "success": False,
            "error": f"Небезопасный путь проекта: {target_path}",
            "project_name": project_name,
            "path": str(target_path),
            "dir_deleted": False,
            "stopped_procs": 0,
            "tasks_removed": 0,
            "removed_skills": [],
            "removed_commands": [],
            "memory_cleaned": {}
        }

    stopped_procs = stop_project_processes_by_path(str(target_path))

    dir_deleted = False
    if target_path.exists() and target_path.is_dir():
        try:
            shutil.rmtree(target_path, onerror=_handle_remove_readonly)
            dir_deleted = not target_path.exists()
            logger.info(f"Директория проекта {target_path} успешно удалена.")
        except Exception as e:
            logger.error(f"Ошибка при удалении директории {target_path}: {e}")
            try:
                shutil.rmtree(target_path, ignore_errors=True)
                dir_deleted = not target_path.exists()
            except Exception:
                pass

    # Очищаем очередь задач Antigravity
    tasks_removed = 0
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            new_tasks = [t for t in tasks if t.get("project_name") != project_name]
            tasks_removed = len(tasks) - len(new_tasks)
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(new_tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка очистки QUEUE_FILE: {e}")

    # Удаляем из реестров
    reg_res = unregister_installed_tool(project_name)

    # Удаляем из долговременной памяти Antigravity (GEMINI.md, ANTIGRAVITY_MEMORY.md, antigravity_memory.json)
    mem_res = remove_from_antigravity_memory(project_name)

    return {
        "success": True,
        "project_name": project_name,
        "path": str(target_path),
        "dir_deleted": dir_deleted,
        "stopped_procs": stopped_procs,
        "tasks_removed": tasks_removed,
        "removed_skills": reg_res.get("removed_skills", []),
        "removed_commands": reg_res.get("removed_commands", []),
        "memory_cleaned": mem_res
    }
