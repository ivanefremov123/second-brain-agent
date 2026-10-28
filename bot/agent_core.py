"""
agent_core.py — Общее кроссплатформенное ядро для локальных агентов (Windows и Mac).
Без эмодзи, с изоляцией задач по платформе и моноширинным выводом путей.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, Set, Callable

import httpx

from bot import AGENT_VERSION, device_link
from bot.config import BOT_TOKEN, QUEUE_FILE
from bot.github_service import cancel_clone, make_progress_bar, download_resource, queue_task_for_antigravity
from bot.runner_service import (
    generate_usage_guide, uninstall_service,
    stop_process, detect_offline_launch_command, auto_run_project_on_mac,
    get_mac_local_ip, prepare_python_env, ACTIVE_PROCESS_INFO,
    RUNTIMES, install_runtime
)
from bot.registry_service import register_installed_tool, REGISTRY_JSON
from bot.memory_service import (
    save_to_antigravity_memory, remove_from_antigravity_memory,
    JSON_MEMORY_FILE, find_repo_url_in_memory
)
from bot.platform_utils import (
    APP_DIR, IS_MAC, IS_WINDOWS, OS_NAME, LOGS_DIR, open_url, launch_in_terminal, keep_awake,
    open_directory, kill_process_by_name, launch_gui_executable
)

# Заглушаем спам от сетевых библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
# Адреса очереди задач и канала событий — в настройках (.env / agent.env), не в коде: код агента публичный
KVDB_BUCKET = os.getenv("KVDB_BUCKET", "").strip()
KVDB_BASE = f"https://kvdb.io/{KVDB_BUCKET}"
SMEE_CHANNEL = os.getenv("SMEE_CHANNEL", "").strip()
PROCESSED_TASKS_FILE = APP_DIR / "processed_tasks.json"
# Общий секрет с облаком (Vercel AGENT_SECRET): облако подписывает задачи, агент выполняет только подписанные
AGENT_SECRET = os.getenv("AGENT_SECRET", "").strip()
# Задачи ждут в очереди, пока ПК выключен, поэтому окно больше пары минут
TASK_MAX_AGE_SEC = 24 * 3600

# Проект -> время отмены: отменяются только задачи, подписанные до неё (иначе повторный запрос молча терялся)
CANCELLED_PROJECTS: Dict[str, float] = {}
ACTIVE_TASKS: Dict[str, asyncio.Task] = {}

def sanitize_keyboard(markup: Optional[Dict[str, Any]], project_name: str = "") -> Optional[Dict[str, Any]]:
    """Гарантирует, что ни одна кнопка не содержит url с localhost или 127.0.0.1, предотвращая ошибку Telegram 400."""
    if not markup or not isinstance(markup, dict):
        return markup
    rows = markup.get("inline_keyboard", [])
    new_rows = []
    for row in rows:
        new_row = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            b = dict(btn)
            u = b.get("url")
            if u:
                u_str = str(u).strip().lower()
                if "localhost" in u_str or "127.0.0.1" in u_str or u_str.startswith("file://"):
                    b.pop("url", None)
                    p_tgt = project_name or "tool"
                    b["callback_data"] = f"open_browser:{p_tgt}"
            new_row.append(b)
        new_rows.append(new_row)
    return {"inline_keyboard": new_rows}

def is_task_authentic(task: dict, logger: logging.Logger) -> bool:
    """Очередь kvdb и канал smee.io публичны: принимаем только задачи с валидной подписью облака."""
    action = task.get("action", "?")
    if device_link.is_paired():
        # Компьютер пользователя: задачи подписаны ключом этого устройства, а не общим AGENT_SECRET
        if not device_link.task_signature_ok(task):
            logger.warning(f"Отклонена задача с неверной или устаревшей подписью устройства: {action}")
            return False
        return True
    if not AGENT_SECRET:
        logger.warning(f"Задача {action} отклонена: в .env не задан AGENT_SECRET")
        return False
    sig = task.get("sig")
    signed_at = task.get("signed_at")
    if not isinstance(sig, str) or not isinstance(signed_at, int):
        logger.warning(f"Отклонена неподписанная задача: {action}")
        return False
    body = {k: v for k, v in task.items() if k != "sig"}
    message = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(AGENT_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        logger.warning(f"Отклонена задача с неверной подписью: {action}")
        return False
    age = time.time() - signed_at
    if age > TASK_MAX_AGE_SEC or age < -300:
        logger.warning(f"Отклонена устаревшая задача: {action}")
        return False
    return True

async def tg_post(client: httpx.AsyncClient, method: str, payload: dict) -> httpx.Response:
    """Вызов Telegram Bot API. У подключённого по коду компьютера нет BOT_TOKEN — сообщения идут через облако."""
    if device_link.is_paired():
        return await device_link.report(client, method, payload)
    return await client.post(f"{TELEGRAM_API_URL}/{method}", json=payload)

async def _tg_send(chat_id, text: str, markup: Optional[dict] = None, msg_id: Optional[int] = None) -> Optional[int]:
    """Правит сообщение msg_id или отправляет новое; возвращает id итогового сообщения."""
    if not chat_id:
        return None
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if msg_id:
                r = await tg_post(client, "editMessageText", {**payload, "message_id": msg_id})
                if r.status_code == 200:
                    return msg_id
            r = await tg_post(client, "sendMessage", payload)
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
    except Exception:
        pass
    return None

def get_processed_ids() -> Set[str]:
    if PROCESSED_TASKS_FILE.exists():
        try:
            with open(PROCESSED_TASKS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def record_processed_id(task_id: str):
    processed = get_processed_ids()
    processed.add(task_id)
    try:
        with open(PROCESSED_TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(processed)[-500:], f)
    except Exception:
        pass

def get_installed_tool_names(downloads_dir: Path) -> list:
    """Возвращает список установленных инструментов, сканируя директории проектов."""
    tools = {}
    dirs_to_check = [downloads_dir]
    if IS_WINDOWS:
        for extra_p in [Path.home() / "Projects", Path("C:/Projects")]:
            if extra_p not in dirs_to_check and extra_p.exists():
                dirs_to_check.append(extra_p)
    elif IS_MAC:
        for extra_p in [Path.home() / "Projects", Path.home() / "Downloads"]:
            if extra_p not in dirs_to_check and extra_p.exists():
                dirs_to_check.append(extra_p)

    for d_dir in dirs_to_check:
        if d_dir.exists():
            try:
                for d in d_dir.iterdir():
                    if d.is_dir() and not d.name.startswith((".", "_")) and d.name.lower() not in ("logs", "venv", ".venv", "__pycache__"):
                        norm_k = re.sub(r"[^a-z0-9]", "", d.name.lower())
                        if norm_k:
                            tools[norm_k] = d.name
            except Exception:
                pass

    if JSON_MEMORY_FILE.exists():
        try:
            mem = json.loads(JSON_MEMORY_FILE.read_text(encoding="utf-8"))
            for item in mem:
                n = item.get("name")
                if n:
                    for d_dir in dirs_to_check:
                        if (d_dir / n).exists():
                            norm_k = re.sub(r"[^a-z0-9]", "", n.lower())
                            if norm_k and norm_k not in tools:
                                tools[norm_k] = n
                            break
        except Exception:
            pass

    if "bento" in tools and "bentopresentations" in tools:
        tools.pop("bento", None)

    return sorted(list(tools.values()))

get_local_installed_tools = get_installed_tool_names

async def sync_installed_tools_to_cloud(downloads_dir: Path, logger: logging.Logger, platform: Optional[str] = None):
    """Синхронизирует актуальный список утилит и метаданные в kvdb под изолированным ключом платформы."""
    if device_link.is_paired():
        return  # компьютер пользователя не знает kvdb: список утилит уходит в облако вместе с опросом очереди
    try:
        tools = get_installed_tool_names(downloads_dir)
        clean_plat = (platform or (OS_NAME.lower() if hasattr(OS_NAME, "lower") else "windows")).strip().lower()
        if clean_plat not in ("mac", "windows"):
            clean_plat = "windows" if IS_WINDOWS else "mac"
            
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{KVDB_BASE}/installed_tools_{clean_plat}", content=json.dumps(tools))

            meta_list = []
            if REGISTRY_JSON.exists():
                try:
                    raw_meta = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
                    for item in raw_meta:
                        if isinstance(item, dict) and item.get("name"):
                            meta_list.append({
                                "name": item.get("name"),
                                "repo_url": item.get("repo_url", ""),
                                "path": item.get("path", "")
                            })
                except Exception:
                    pass
            if meta_list:
                await client.post(f"{KVDB_BASE}/installed_tools_meta_{clean_plat}", content=json.dumps(meta_list))

            if JSON_MEMORY_FILE.exists():
                try:
                    mem_data = json.loads(JSON_MEMORY_FILE.read_text(encoding="utf-8"))
                    if isinstance(mem_data, list):
                        await client.post(f"{KVDB_BASE}/antigravity_memory_{clean_plat}", content=json.dumps(mem_data))
                        await client.post(f"{KVDB_BASE}/antigravity_memory", content=json.dumps(mem_data))
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Ошибка синхронизации установленных утилит: {e}")

async def send_platform_heartbeat(
    platform: str,
    device_name: str,
    os_name: str,
    downloads_dir: Path,
    active_tasks: list,
    logger: logging.Logger
):
    """Отправляет heartbeat конкретной платформы (heartbeat_windows или heartbeat_mac)."""
    if device_link.is_paired():
        return  # «в сети» у компьютера пользователя — это его опрос очереди в облаке
    try:
        payload = {
            "timestamp": time.time(),
            "status": "online",
            "active_tasks": active_tasks,
            "tools_count": len(get_installed_tool_names(downloads_dir)),
            "device_id": platform,
            "device_name": device_name,
            "os": os_name,
            "platform": platform
        }
        body = json.dumps(payload)
        async with httpx.AsyncClient(timeout=4.0) as client:
            await client.post(f"{KVDB_BASE}/heartbeat_{platform}", content=body)
            # Обратная совместимость
            if platform == "mac":
                await client.post(f"{KVDB_BASE}/mac_heartbeat", content=body)
            elif platform == "windows":
                await client.post(f"{KVDB_BASE}/heartbeat_win-laptop", content=body)
    except Exception:
        pass

async def send_telegram_card(
    chat_id: int,
    project_name: str,
    local_path: str,
    repo_url: str,
    guide: dict,
    platform: str = "",
    device_label: str = ""
):
    """Отправляет карточку проекта без эмодзи, с моноширинным путем и кнопками под платформу и тип проекта."""
    if IS_WINDOWS:
        platform = "windows"
        device_label = "Windows ПК"
    else:
        platform = platform or "mac"
        device_label = device_label or "MacBook"
    summary = guide.get("summary") or guide.get("how_to_use") or "Готово к работе."
    p_type = guide.get("project_type") or "cli"
    is_web = guide.get("is_web", False) or p_type == "web"
    web_port = guide.get("web_port", 0)
    cmd = guide.get("launch_command") or "python main.py"

    type_labels = {
        "web": f"Веб-сервис (порт {web_port or 3000})",
        "cli": "Консольная утилита (CLI)",
        "gui": "Оконное приложение (GUI)",
        "library": "Библиотека / Пакет",
        "docs": "База знаний / Документация"
    }

    parts = [
        f"<b>Утилита {html.escape(project_name)} скачана на {device_label}!</b>",
        f"Проект лежит здесь: <code>{html.escape(local_path)}</code>",
        f"Тип: {type_labels.get(p_type, 'Консольная утилита')}",
        f"Что делает:\n{html.escape(summary)}"
    ]
    if p_type in ("web", "cli", "gui"):
        parts.append(f"Команда запуска:\n<code>{html.escape(cmd)}</code>")
    elif p_type == "library":
        snippet = guide.get("import_snippet") or f"import {project_name.lower().replace('-', '_')}"
        parts.append(f"Пример импорта:\n<code>{html.escape(snippet)}</code>")

    row3 = []
    if p_type == "web":
        from bot.runner_service import is_online_web_service
        is_online = is_online_web_service(repo_url)
        if is_online:
            row1 = [
                {"text": "Открыть в браузере", "url": repo_url}
            ]
            row2 = [
                {"text": "Как пользоваться", "callback_data": f"tool_guide:{project_name}"},
                {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
            ]
        else:
            row1 = [
                {"text": "Запустить веб-сервис", "callback_data": f"auto_run:{project_name}"}
            ]
            row2 = [
                {"text": "Как пользоваться", "callback_data": f"tool_guide:{project_name}"},
                {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
            ]
    elif p_type == "gui":
        row1 = [
            {"text": "Открыть приложение", "callback_data": f"auto_run:{project_name}"}
        ]
        row2 = [
            {"text": "Как пользоваться", "callback_data": f"tool_guide:{project_name}"},
            {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
        ]
    elif p_type == "library":
        row1 = [
            {"text": "Как подключить", "callback_data": f"tool_guide:{project_name}"},
            {"text": "Скопировать импорт", "callback_data": f"copy_import:{project_name}"}
        ]
        row2 = [
            {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
        ]
    elif p_type == "docs":
        row1 = [
            {"text": "Показать главное", "callback_data": f"show_summary:{project_name}"},
            {"text": f"Запустить на {device_label}", "callback_data": f"auto_run:{project_name}"}
        ]
        row2 = [
            {"text": "Как пользоваться", "callback_data": f"tool_guide:{project_name}"},
            {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
        ]
    else: # cli
        row1 = [
            {"text": "Открыть в терминале", "callback_data": f"auto_run:{project_name}"}
        ]
        row2 = [
            {"text": "Как пользоваться", "callback_data": f"tool_guide:{project_name}"},
            {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
        ]

    rows = [row1, row2]
    if row3:
        rows.append(row3)
    elif repo_url:
        rows.append([{"text": "GitHub / Источник", "url": repo_url}])

    markup = sanitize_keyboard({"inline_keyboard": rows}, project_name)

    payload = {
        "chat_id": chat_id,
        "text": "\n\n".join(parts),
        "parse_mode": "HTML",
        "reply_markup": markup
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await tg_post(client, "sendMessage", payload)
    except Exception:
        pass

def is_task_for_platform(task: dict, current_platform: str) -> bool:
    """Проверяет соответствие задачи текущей платформе агента."""
    t_plat = (task.get("platform") or task.get("device") or "").strip().lower()
    if not t_plat or t_plat in ("any", ""):
        return True
    if current_platform == "windows" and t_plat in ("windows", "win", "win-laptop", "win-pc"):
        return True
    if current_platform == "mac" and t_plat in ("mac", "macos"):
        return True
    return t_plat == current_platform

def find_project_dir(downloads_dir: Path, project_name: str, local_path: Optional[str] = None) -> Optional[Path]:
    r"""
    Глубокий поиск директории проекта на диске:
    1. По точному пути local_path (если передан и существует).
    2. По записям в registry.json и antigravity_memory.json (сохранённый path).
    3. По точному имени в downloads_dir, C:\Users\<User>\Projects, C:\Projects.
    4. По нормализованному названию и алиасам (fastpotify <-> spotifast).
    5. По глубокому рекурсивному поиску папки с готовым .exe / .bat / скриптом.
    """
    if local_path:
        lp = Path(local_path)
        if lp.exists() and lp.is_dir():
            return lp

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower().strip())

    norm_target = norm(project_name)
    KNOWN_ALIASES = {
        "fastpotify": "spotifast",
        "spotifast": "fastpotify"
    }
    norm_alias = norm(KNOWN_ALIASES.get(norm_target, ""))

    # 1. Проверяем сохраненные пути в реестре и памяти Antigravity
    for meta_file in (REGISTRY_JSON, JSON_MEMORY_FILE):
        if meta_file.exists():
            try:
                raw_entries = json.loads(meta_file.read_text(encoding="utf-8", errors="ignore"))
                for entry in raw_entries:
                    if isinstance(entry, dict):
                        e_name = entry.get("name", "")
                        e_path = entry.get("path", "")
                        n_e = norm(e_name)
                        if (n_e in (norm_target, norm_alias) or e_name.lower() == project_name.lower()) and e_path:
                            p_e = Path(e_path)
                            if p_e.exists() and p_e.is_dir():
                                return p_e
            except Exception:
                pass

    check_roots = [downloads_dir]
    if IS_WINDOWS:
        for p in [Path.home() / "Projects", Path("C:/Projects")]:
            if p not in check_roots and p.exists():
                check_roots.append(p)
    elif IS_MAC:
        for p in [Path.home() / "Projects", Path.home() / "Downloads"]:
            if p not in check_roots and p.exists():
                check_roots.append(p)

    # 2. Точное совпадение папки
    for root in check_roots:
        exact = root / project_name
        if exact.exists() and exact.is_dir():
            return exact
        if norm_alias:
            alias_path = root / KNOWN_ALIASES[norm_target]
            if alias_path.exists() and alias_path.is_dir():
                return alias_path

    # 3. Совпадение после нормализации имени папки в корнях
    if norm_target:
        for root in check_roots:
            if root.exists():
                try:
                    for d in root.iterdir():
                        if d.is_dir() and not d.name.startswith((".", "_")):
                            n_d = norm(d.name)
                            if n_d in (norm_target, norm_alias):
                                return d
                except Exception:
                    pass

    # 4. Префиксное или частичное совпадение
    if norm_target and len(norm_target) >= 4:
        for root in check_roots:
            if root.exists():
                try:
                    for d in root.iterdir():
                        if d.is_dir() and not d.name.startswith((".", "_")):
                            norm_d = norm(d.name)
                            if norm_d.startswith(norm_target) or norm_target.startswith(norm_d):
                                return d
                            if norm_alias and (norm_d.startswith(norm_alias) or norm_alias.startswith(norm_d)):
                                return d
                except Exception:
                    pass

    # 5. Глубокий рекурсивный поиск в check_roots (глубина до 3 уровней) на случай вложенных папок
    for root in check_roots:
        if root.exists():
            try:
                for sub in root.glob("*/*"):
                    if sub.is_dir() and not sub.name.startswith((".", "_")):
                        n_sub = norm(sub.name)
                        if n_sub in (norm_target, norm_alias):
                            return sub
            except Exception:
                pass

    return None

async def process_task(task: dict, current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    """Выполняет задачу: скачивание, автозапуск, удаление или инструкцию."""
    if IS_WINDOWS:
        current_platform = "windows"
        device_label = "Windows ПК"
    project_name = task.get("project_name", "downloaded_project")
    repo_url = task.get("repo_url", "")
    chat_id = task.get("chat_id")
    action = task.get("action")

    if action == "save_memory":
        what_it_does = task.get("what_it_does", "")
        await save_to_antigravity_memory(
            name=project_name,
            category="Утилита",
            what_it_does=what_it_does,
            repo_url=repo_url
        )
        logger.info(f"Инструмент {project_name} сохранён в память Antigravity.")
        return

    if action == "open_folder":
        logger.info(f"Запрос на открытие папки {project_name} на {device_label}...")
        found_dir = find_project_dir(downloads_dir, project_name, task.get("local_path"))
        p = found_dir or (downloads_dir / project_name)
        from bot.platform_utils import open_directory
        open_directory(p)
        if chat_id:
            try:
                fm_name = "Проводнике" if current_platform == "windows" else "Finder"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await tg_post(client, "sendMessage", {
                        "chat_id": chat_id,
                        "text": f"Папка <b>{html.escape(project_name)}</b> открыта в {fm_name} на {device_label}:\n<code>{html.escape(str(p))}</code>",
                        "parse_mode": "HTML"
                    })
            except Exception:
                pass
        return

    if action in ("open_browser", "open_url"):
        logger.info(f"Запрос на открытие страницы/сервиса {project_name} в браузере на {device_label}...")
        url_to_open = task.get("url")
        if not url_to_open:
            for p_info in ACTIVE_PROCESS_INFO.values():
                if isinstance(p_info, dict) and p_info.get("project_name") == project_name and p_info.get("port"):
                    url_to_open = f"http://localhost:{p_info['port']}"
                    break
        if not url_to_open:
            from bot.memory_service import find_repo_url_in_memory
            url_to_open = find_repo_url_in_memory(project_name)
        if not url_to_open:
            url_to_open = task.get("repo_url")
        if not url_to_open:
            url_to_open = "http://localhost:8899"
        import webbrowser
        try:
            webbrowser.open(url_to_open)
        except Exception as e:
            logger.error(f"Ошибка открытия браузера: {e}")
        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await tg_post(client, "sendMessage", {
                        "chat_id": chat_id,
                        "text": f"Страница <b>{html.escape(project_name)}</b> открыта в браузере на вашем {device_label}!\n<code>{html.escape(url_to_open)}</code>",
                        "parse_mode": "HTML"
                    })
            except Exception:
                pass
        return

    if action == "install_runtime":
        rt = str(task.get("runtime", ""))
        if rt not in RUNTIMES:
            logger.warning(f"Отклонена установка неизвестной среды: {rt!r}")
            return
        label = RUNTIMES[rt]["label"]
        tool = "winget" if IS_WINDOWS else "Homebrew"
        # Git нужен для скачивания: после него продолжаем скачивание, а не запуск
        next_action, next_verb = ("clone", "скачивание") if rt == "git" else ("auto_run", "запуск")
        logger.info(f"Установка среды {label} для {project_name}...")
        msg_id = await _tg_send(
            chat_id,
            f"Устанавливаю <b>{html.escape(label)}</b> на {device_label} через {tool}...\n\n"
            f"Это может занять несколько минут. После установки {next_verb} {html.escape(project_name)} продолжится автоматически.",
            msg_id=task.get("message_id"),
        )
        res = await install_runtime(rt)
        if res.get("success"):
            logger.info(f"Среда {label} установлена, продолжаю: {next_action} {project_name}")
            await _tg_send(chat_id, f"<b>{html.escape(label)} установлена.</b> Продолжаю: {next_verb} {html.escape(project_name)}...", msg_id=msg_id)
            next_task = {**task, "action": next_action, "message_id": msg_id}
            if next_action == "clone" and not next_task.get("repo_url"):
                next_task["repo_url"] = find_repo_url_in_memory(project_name) or ""
            await process_task(next_task, current_platform, device_label, downloads_dir, logger)
        else:
            logger.warning(f"Установка {label} не удалась: {res.get('message', '')[:200]}")
            await _tg_send(
                chat_id,
                f"<b>Не удалось установить {html.escape(label)} автоматически.</b>\n\n"
                f"<pre>{html.escape(res.get('message', '')[:800])}</pre>\n\n"
                f"Установите вручную и нажмите «Попробовать снова».",
                markup={"inline_keyboard": [[{"text": "Попробовать снова", "callback_data": (f"install_rt:git:{project_name}" if rt == "git" else f"auto_run:{project_name}")[:64]}]]},
                msg_id=msg_id,
            )
        return

    if action in ("auto_run", "launch"):
        logger.info(f"Запрос на запуск утилиты {project_name} на {device_label}...")
        if not repo_url:
            repo_url = find_repo_url_in_memory(project_name)
        found_dir = find_project_dir(downloads_dir, project_name, task.get("local_path"))
        local_path = str(found_dir) if found_dir else (task.get("local_path") or str(downloads_dir / project_name))
        cmd = task.get("launch_command")
        msg_id = task.get("message_id")

        p = found_dir or Path(local_path)
        if not p.exists():
            target_url = repo_url or find_repo_url_in_memory(project_name)
            if target_url:
                logger.info(f"Директория {local_path} не найдена. Автоматически скачиваем {project_name} из {target_url}...")
                cancel_markup = {
                    "inline_keyboard": [
                        [{"text": "Отменить загрузку", "callback_data": f"cancel_task:{project_name}"}]
                    ]
                }
                if chat_id:
                    try:
                        async with httpx.AsyncClient(timeout=8.0) as client:
                            auto_msg = (
                                f"Утилита {html.escape(project_name)} еще не скачана на {device_label}.\n\n"
                                f"Автоматически скачиваем репозиторий и настраиваем окружение..."
                            )
                            if msg_id:
                                await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": auto_msg, "parse_mode": "HTML", "reply_markup": cancel_markup})
                            else:
                                await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": auto_msg, "parse_mode": "HTML", "reply_markup": cancel_markup})
                    except Exception:
                        pass

                dl_res = await download_resource(target_url, project_name)
                if dl_res.get("success"):
                    local_path = dl_res.get("path") or str(downloads_dir / project_name)
                    p = Path(local_path)
                    await prepare_python_env(local_path)
                    try:
                        await register_installed_tool(
                            project_name=project_name,
                            local_path=local_path,
                            what_it_does=dl_res.get("description", "Утилита скачана и настроена."),
                            category="Утилита",
                            repo_url=target_url
                        )
                    except Exception:
                        pass
                else:
                    err_msg = dl_res.get("error", dl_res.get("message", "Не удалось автоматически скачать репозиторий."))
                    text = (
                        f"Не удалось запустить {html.escape(project_name)} на {device_label}:\n\n"
                        f"Проект не найден на диске, а при попытке скачивания произошла ошибка:\n"
                        f"<pre>{html.escape(err_msg[:1000])}</pre>"
                    )
                    markup = {
                        "inline_keyboard": [
                            [{"text": "Попробовать скачать снова", "callback_data": f"cloud_clone:{project_name}"}]
                        ]
                    }
                    if chat_id:
                        try:
                            async with httpx.AsyncClient(timeout=8.0) as client:
                                if msg_id:
                                    await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                                else:
                                    await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                        except Exception:
                            pass
                    return
            else:
                text = (
                    f"Утилита {html.escape(project_name)} еще не скачана на {device_label} (папка <code>{html.escape(local_path)}</code> не найдена).\n\n"
                    f"Отправьте боту ссылку на GitHub-репозиторий, чтобы установить её."
                )
                markup = {
                    "inline_keyboard": [
                        [{"text": f"Скачать на {device_label}", "callback_data": f"cloud_clone:{project_name}"}]
                    ]
                }
                if chat_id:
                    try:
                        async with httpx.AsyncClient(timeout=8.0) as client:
                            if msg_id:
                                await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                            else:
                                await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                    except Exception:
                        pass
                return

        res = await auto_run_project_on_mac(local_path, project_name, cmd, repo_url=repo_url)
        res_type = res.get("type")

        if res_type == "online_web":
            target_url = res.get("url") or res.get("online_url") or repo_url or find_repo_url_in_memory(project_name)
            is_local = any(k in (target_url or "").lower() for k in ["localhost", "127.0.0.1", "file://"])
            browser_btn = {"text": "Открыть в браузере ПК", "callback_data": f"open_browser:{project_name}"} if is_local else {"text": "Открыть в браузере", "url": target_url}
            text = (
                f"<b>Онлайн-сервис {html.escape(project_name)} открыт в браузере!</b>\n\n"
                f"• <b>На {device_label}:</b> страница открыта в вашем браузере.\n"
                f"• <b>С телефона или ПК:</b> нажмите кнопку «Открыть в браузере» ниже, чтобы работать прямо сейчас.\n\n"
                f"<b>Адрес сервиса:</b> <code>{html.escape(target_url)}</code>\n"
                f"<b>Папка проекта:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        browser_btn
                    ],
                    [
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type == "web":
            pid = res.get("pid")
            local_url = res.get("local_url")
            mobile_url = res.get("mobile_url")
            cmd_used = res.get("command", "")
            text = (
                f"<b>Веб-интерфейс {html.escape(project_name)} запущен на {device_label}!</b>\n\n"
                f"<b>На {device_label}:</b>\n"
                f"Страница открыта в браузере (или перейдите: <code>{local_url}</code>)\n\n"
                f"<b>С телефона (в одной Wi-Fi сети):</b>\n"
                f"<code>{mobile_url}</code>\n"
                f"<i>(Убедитесь, что смартфон подключен к тому же Wi-Fi, что и компьютер)</i>\n\n"
                f"<b>Команда:</b> <code>{html.escape(cmd_used)}</code> | <b>PID:</b> <code>{pid}</code>\n"
                f"<b>Папка проекта:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Открыть в браузере ПК", "callback_data": f"open_browser:{project_name}"}
                    ],
                    [
                        {"text": "Остановить сервер", "callback_data": f"stop_proc:{pid}:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type == "terminal":
            cmd_used = res.get("command", cmd or "")
            text = (
                f"<b>Консольная утилита {html.escape(project_name)} запущена на экране {device_label}!</b>\n\n"
                f"Окно консоли открыто на мониторе и выполняет:\n<code>{html.escape(cmd_used)}</code>\n\n"
                f"<i>Это утилита командной строки (CLI) — она не имеет веб-интерфейса в браузере и работает в терминале.</i>\n\n"
                f"<b>Папка проекта:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Запустить снова", "callback_data": f"auto_run:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type in ("web_page", "html_page"):
            html_p = res.get("html_path") or local_path
            msg_desc = res.get("message") or f"Веб-страница {html.escape(project_name)} открыта в браузере на вашем {device_label}!"
            text = (
                f"<b>{msg_desc}</b>\n\n"
                f"• Локальный HTML-файл открыт в браузере на экране {device_label}.\n"
                f"• <b>Путь:</b> <code>{html.escape(html_p)}</code>\n\n"
                f"<i>Примечание: это локальный файл на диске компьютера, поэтому открыть его напрямую с телефона через интернет нельзя.</i>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": f"Открыть на {device_label}", "callback_data": f"auto_run:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type in ("browser_extension", "vscode_extension"):
            msg_desc = res.get("message") or f"Расширение {html.escape(project_name)} открыто на вашем {device_label}!"
            details = res.get("details", "")
            text = (
                f"{msg_desc}\n\n"
                f"{details}\n\n"
                f"<b>Путь:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type in ("gui", "windows_app", "mac_app", "installer"):
            msg_desc = res.get("message") or f"Приложение {html.escape(project_name)} открыто на экране {device_label}!"
            text = (
                f"<b>{msg_desc}</b>\n\n"
                f"Окно программы активно на рабочем столе {device_label}.\n"
                f"<i>Это десктопное приложение для компьютера.</i>\n\n"
                f"<b>Путь:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Запустить снова", "callback_data": f"auto_run:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type == "finder":
            term_app = "Проводнике" if IS_WINDOWS else "Finder"
            text = (
                f"Папка проекта {html.escape(project_name)} открыта в {term_app} на {device_label}!\n\n"
                f"Путь: <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        elif res_type == "no_entrypoint":
            text = (
                f"<b>{html.escape(project_name)}: запускать нечего</b>\n\n"
                f"{html.escape(res.get('message', ''))}\n\n"
                f"<b>Папка проекта:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"[:64]}]
                ]
            }
        elif res_type == "missing_runtime":
            rt = res.get("runtime", "")
            label = res.get("runtime_label") or rt
            tool = "winget" if IS_WINDOWS else "Homebrew"
            text = (
                f"<b>Для запуска {html.escape(project_name)} нужно установить {html.escape(label)}</b>\n\n"
                f"На {device_label} не найдено подходящей версии. Могу поставить её автоматически через {tool} "
                f"(официальный пакет) и сразу запустить {html.escape(project_name)}.\n\n"
                f"<b>Команда запуска:</b> <code>{html.escape(res.get('command', ''))}</code>\n"
                f"<b>Папка проекта:</b> <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"Установить {label}", "callback_data": f"install_rt:{rt}:{project_name}"[:64]}],
                    [{"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"[:64]}]
                ]
            }
        elif res.get("success"):
            out = res.get("output", "Команда выполнена успешно.")
            cmd_used = res.get("command", cmd or "")
            text = (
                f"Утилита {html.escape(project_name)} успешно выполнена на {device_label}!\n\n"
                f"Проект лежит здесь: <code>{html.escape(local_path)}</code>\n"
                f"Команда: <code>{html.escape(cmd_used)}</code>\n\n"
                f"Результат:\n<pre>{html.escape(out[:1500])}</pre>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Запустить снова", "callback_data": f"auto_run:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }
        else:
            err = res.get("output", res.get("message", "Ошибка выполнения"))
            cmd_used = res.get("command", cmd or "")
            text = (
                f"Ошибка при запуске {html.escape(project_name)} на {device_label}:\n\n"
                f"Команда: <code>{html.escape(cmd_used)}</code>\n\n"
                f"Лог:\n<pre>{html.escape(err[:1500])}</pre>\n\n"
                f"Проект лежит здесь: <code>{html.escape(local_path)}</code>"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "Попробовать снова", "callback_data": f"auto_run:{project_name}"},
                        {"text": "Инструкция", "callback_data": f"tool_guide:{project_name}"}
                    ]
                ]
            }

        markup = sanitize_keyboard(markup, project_name)

        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    if msg_id:
                        r = await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                        if r.status_code != 200:
                            logger.info(f"editMessageText non-200 ({r.status_code}), sending fresh message: {r.text[:200]}")
                            await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": markup})
            except Exception as e:
                logger.error(f"Error sending/editing message in agent_core: {e}")
        return

    if action == "stop_proc":
        pid = task.get("pid")
        stopped = stop_process(int(pid)) if pid else False
        text = f"Процесс {html.escape(project_name)} (PID: {pid}) {'успешно остановлен' if stopped else 'не найден или уже завершен'}."
        msg_id = task.get("message_id")
        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    if msg_id:
                        await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            except Exception:
                pass
        return

    if action == "get_guide":
        found_dir = find_project_dir(downloads_dir, project_name, task.get("local_path"))
        local_path = str(found_dir) if found_dir else (task.get("local_path") or str(downloads_dir / project_name))
        p = found_dir or Path(local_path)

        target_url = repo_url or find_repo_url_in_memory(project_name)
        guide = await generate_usage_guide(local_path, project_name, repo_url=target_url)
        summary = guide.get("summary") or guide.get("how_to_use") or "Готово к работе."
        quick_start = guide.get("quick_start") or "Запустите утилиту через кнопку ниже."
        cmd = guide.get("launch_command") or "python main.py"
        examples = guide.get("examples")

        if not p.exists():
            guide_text = (
                f"Инструкция к утилите {html.escape(project_name)}\n\n"
                f"Что делает:\n{html.escape(summary)}\n\n"
                f"Утилита еще не скачана на {device_label}.\n"
                f"Нажмите кнопку ниже, чтобы скачать и подготовить её к запуску."
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": f"Скачать на {device_label}", "callback_data": f"cloud_clone:{project_name}"}
                    ]
                ]
            }
        else:
            guide_text = (
                f"Инструкция к утилите {html.escape(project_name)}\n"
                f"Проект лежит здесь: <code>{html.escape(local_path)}</code>\n\n"
                f"Что делает:\n{html.escape(summary)}\n\n"
                f"Пошаговый сценарий:\n{html.escape(quick_start)}\n\n"
                f"Команда запуска:\n<code>{html.escape(cmd)}</code>"
            )
            if examples and examples != cmd:
                guide_text += f"\n\nПримеры:\n<code>{html.escape(examples)}</code>"

            markup = {
                "inline_keyboard": [
                    [
                        {"text": f"Запустить на {device_label}", "callback_data": f"auto_run:{project_name}"},
                        {"text": f"Удалить с {device_label}", "callback_data": f"cloud_uninst:{project_name}"}
                    ]
                ]
            }
        msg_id = task.get("message_id")
        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    if msg_id:
                        await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": guide_text, "parse_mode": "HTML", "reply_markup": markup})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": guide_text, "parse_mode": "HTML", "reply_markup": markup})
            except Exception:
                pass
        return

    if action == "uninstall":
        logger.info(f"Запрос на удаление {project_name} с {device_label}...")
        found_dir = find_project_dir(downloads_dir, project_name, task.get("local_path"))
        target_path_str = str(found_dir) if found_dir else (task.get("local_path") or str(downloads_dir / project_name))
        res = uninstall_service(local_path=target_path_str, project_name=project_name)
        remove_from_antigravity_memory(project_name)
        await sync_installed_tools_to_cloud(downloads_dir, logger, platform=current_platform)
        result_text = (
            f"Проект {html.escape(project_name)} успешно удалён с {device_label}.\n\n"
            f"• Файлы по пути <code>{html.escape(target_path_str)}</code> удалены\n"
            f"• Запись удалена из памяти Antigravity"
        )
        msg_id = task.get("message_id")
        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    if msg_id:
                        await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": result_text, "parse_mode": "HTML"})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": result_text, "parse_mode": "HTML"})
            except Exception:
                pass
        return

    # Скачивание репозитория
    logger.info(f"Начало скачивания задачи {task.get('id')}: {project_name} ({repo_url})")

    cancel_markup = {
        "inline_keyboard": [
            [{"text": "Отменить загрузку", "callback_data": f"cancel_task:{project_name}"}]
        ]
    }

    progress_msg_id = task.get("message_id")
    if chat_id:
        try:
            initial_bar = make_progress_bar(0, length=12)
            init_text = (
                f"Скачивание утилиты на {device_label}\n"
                f"Проект: {html.escape(project_name)}\n\n"
                f"<code>[{initial_bar}] 0%</code>\n"
                f"Инициализация git clone..."
            )
            async with httpx.AsyncClient(timeout=8.0) as client:
                if progress_msg_id:
                    await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": progress_msg_id, "text": init_text, "parse_mode": "HTML", "reply_markup": cancel_markup})
                else:
                    resp = await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": init_text, "parse_mode": "HTML", "reply_markup": cancel_markup})
                    if resp.status_code == 200:
                        progress_msg_id = resp.json().get("result", {}).get("message_id")
        except Exception:
            pass

    last_edit_time = 0.0
    last_rendered_pct = -1

    async def on_clone_progress(percent: int, stage_text: str):
        nonlocal last_edit_time, last_rendered_pct
        if not progress_msg_id or not chat_id:
            return
        now = time.time()
        if (now - last_edit_time >= 1.2 and percent != last_rendered_pct) or percent == 100:
            last_edit_time = now
            last_rendered_pct = percent
            bar = make_progress_bar(percent, length=12)
            progress_msg = (
                f"Скачивание утилиты на {device_label}\n"
                f"Проект: {html.escape(project_name)}\n\n"
                f"<code>[{bar}] {percent}%</code>\n"
                f"{html.escape(stage_text)}"
            )
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": progress_msg_id, "text": progress_msg, "parse_mode": "HTML", "reply_markup": cancel_markup})
            except Exception:
                pass

    clone_res = await download_resource(
        url=repo_url,
        project_name=project_name,
        clone_id=project_name,
        progress_callback=on_clone_progress
    )

    if clone_res.get("cancelled"):
        logger.info(f"Скачивание {project_name} прервано.")
        if chat_id:
            cancel_text = f"Скачивание {html.escape(project_name)} остановлено.\nВременные файлы удалены."
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    if progress_msg_id:
                        await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": progress_msg_id, "text": cancel_text, "parse_mode": "HTML"})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": cancel_text, "parse_mode": "HTML"})
            except Exception:
                pass
        return

    if not clone_res.get("success") and clone_res.get("type") == "missing_runtime":
        rt = clone_res.get("runtime", "")
        label = clone_res.get("runtime_label", rt)
        logger.info(f"Для скачивания {project_name} не хватает {label} — предлагаю установить")
        await _tg_send(
            chat_id,
            f"<b>{html.escape(project_name)}: нужен {html.escape(label)}.</b>\n\n"
            f"{html.escape(clone_res.get('message', ''))}\n\n"
            f"Установить {html.escape(label)} автоматически (через winget)? После установки скачивание продолжится само.",
            markup={"inline_keyboard": [[{"text": f"Установить {label}", "callback_data": f"install_rt:{rt}:{project_name}"[:64]}]]},
            msg_id=progress_msg_id,
        )
        return

    if not clone_res.get("success"):
        err_msg = clone_res.get("message", "Неизвестная ошибка")
        logger.error(f"Ошибка скачивания {project_name}: {err_msg}")
        if chat_id:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    err_text = f"Ошибка скачивания {html.escape(project_name)} на {device_label}:\n{html.escape(err_msg)}"
                    if progress_msg_id:
                        await tg_post(client, "editMessageText", {"chat_id": chat_id, "message_id": progress_msg_id, "text": err_text, "parse_mode": "HTML"})
                    else:
                        await tg_post(client, "sendMessage", {"chat_id": chat_id, "text": err_text, "parse_mode": "HTML"})
            except Exception:
                pass
        return

    local_path = clone_res["path"]
    guide = await generate_usage_guide(local_path, project_name, repo_url=repo_url)

    await register_installed_tool(
        project_name=project_name,
        local_path=local_path,
        category="Утилита",
        what_it_does=guide.get("how_to_use", ""),
        repo_url=repo_url,
        launch_command=guide.get("launch_command")
    )

    await save_to_antigravity_memory(
        name=project_name,
        category="Утилита",
        what_it_does=guide.get("how_to_use", ""),
        repo_url=repo_url
    )

    queue_task_for_antigravity(
        project_name=project_name,
        repo_url=repo_url,
        local_path=local_path,
        note_text=f"Скачано через бота: {project_name}",
        item_type="Утилита"
    )

    if chat_id:
        await send_telegram_card(chat_id, project_name, local_path, repo_url, guide, current_platform, device_label)
        if progress_msg_id:
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    await tg_post(client, "deleteMessage", {"chat_id": chat_id, "message_id": progress_msg_id})
            except Exception:
                pass

    await sync_installed_tools_to_cloud(downloads_dir, logger, platform=current_platform)
    logger.info(f"Задача для {project_name} успешно выполнена на {device_label}!")

async def run_task_wrapper(task: dict, current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    p_key = task.get("project_name", "").lower().strip()
    try:
        await process_task(task, current_platform, device_label, downloads_dir, logger)
    except asyncio.CancelledError:
        logger.info(f"Фоновая задача для {p_key} отменена.")
    except Exception as e:
        logger.error(f"Ошибка выполнения задачи {p_key}: {e}")
    finally:
        ACTIVE_TASKS.pop(p_key, None)

async def handle_incoming_task(task: dict, current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    if not is_task_for_platform(task, current_platform):
        return

    action = task.get("action", "")
    p_name = task.get("project_name", "")
    p_key = p_name.lower().strip()

    if action in ("ping", "check_status", "ping_queue"):
        await send_platform_heartbeat(current_platform, device_label, OS_NAME, downloads_dir, list(ACTIVE_TASKS.keys()), logger)
        logger.info(f"Получен {action} от облака, отправлен ответный heartbeat.")
        return

    if action == "hello":
        # «Проверить связь» в боте: ответ доказывает всю цепочку — очередь, подпись, сообщения через облако
        await _tg_send(task.get("chat_id"), f"<b>{html.escape(device_label)}</b> на связи. Агент {AGENT_VERSION} готов к работе.")
        logger.info("Проверка связи из бота: ответ отправлен.")
        return

    if action in ("cancel_task", "cancel_active_clone"):
        logger.info(f"Получена команда отмены для '{p_name}'.")
        if p_key:
            CANCELLED_PROJECTS[p_key] = float(task.get("signed_at") or time.time())
        cancel_clone(p_name)
        if p_key in ACTIVE_TASKS and not ACTIVE_TASKS[p_key].done():
            ACTIVE_TASKS[p_key].cancel()
        return

    cancelled_at = CANCELLED_PROJECTS.get(p_key) if p_key else None
    if cancelled_at is not None and float(task.get("signed_at") or 0) <= cancelled_at:
        logger.info(f"Задача {action} для '{p_name}' пропущена: отменена пользователем")
        return

    if device_link.is_paired() and not task.get("_confirmed") and needs_device_confirmation(task, downloads_dir):
        # Компьютер пользователя: вместо 2FA — «Да» в окне на экране (злоумышленник с доступом к Telegram её не нажмёт)
        asyncio.create_task(confirm_and_run(task, current_platform, device_label, downloads_dir, logger))
        return

    t = asyncio.create_task(run_task_wrapper(task, current_platform, device_label, downloads_dir, logger))
    if p_key:
        ACTIVE_TASKS[p_key] = t

async def _remove_task_from_kvdb_queue(task_id: str):
    """Удаляет задачу по ID из очереди kvdb (если она была перехвачена через SSE быстрее опроса)."""
    if not task_id or device_link.is_paired():
        return  # очередь компьютера пользователя в облаке; повтор задачи отсекает список выполненных id
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{KVDB_BASE}/tasks_queue")
            if r.status_code == 200:
                tasks = r.json()
                if isinstance(tasks, list):
                    filtered = [t for t in tasks if isinstance(t, dict) and str(t.get("id")) != str(task_id)]
                    if len(filtered) != len(tasks):
                        await client.post(f"{KVDB_BASE}/tasks_queue", content=json.dumps(filtered))
    except Exception:
        pass


_CONTROL_ACTIONS = ("cancel_task", "cancel_active_clone", "ping", "check_status", "ping_queue")
# Сигналы цикла событий: выход из сна -> сразу опросить очередь и переподключить SSE
_WAKE_EVENT: Optional[asyncio.Event] = None
_SSE_RECONNECT: Optional[asyncio.Event] = None
# Задача старше этого значения ждала, пока ПК был выключен/спал — о ней пишем отдельным сообщением
DEFERRED_TASK_AGE_SEC = 60
_DEFERRED_LABELS = {
    "clone": "скачивание", "download_url": "скачивание", "auto_run": "запуск", "launch": "запуск",
    "uninstall": "удаление", "get_guide": "инструкция", "install_runtime": "установка среды",
}


# Отложенные задачи, которые выполняются только после подтверждения на экране ПК
_CONFIRM_VERBS = {
    "clone": "скачать", "download_url": "скачать", "auto_run": "запустить", "launch": "запустить",
    "uninstall": "удалить", "install_runtime": "установить среду выполнения для",
}
REMIND_AFTER_SEC = 30 * 60
CONFIRM_TIMEOUT_SEC = 15 * 60
MAX_REMINDERS = 6
POSTPONED_FILE = APP_DIR / "data" / "postponed_tasks.json"
_CONFIRM_LOCK: Optional[asyncio.Lock] = None


def _waited_text(task: dict) -> str:
    age = max(0.0, time.time() - float(task.get("signed_at") or time.time()))
    return f"{int(age // 3600)} ч {int(age % 3600 // 60)} мин" if age >= 3600 else f"{max(1, int(age // 60))} мин"


DEVICE_CONFIRM_ACTIONS = ("clone", "download_url", "uninstall", "install_runtime")


def needs_device_confirmation(task: dict, downloads_dir: Path) -> bool:
    """На компьютере пользователя подтверждаются скачивание, удаление и установка сред (Р-17).
    Запуск уже скачанной программы — без окна; «Запустить» для нескачанной программы означает скачивание."""
    action = task.get("action")
    if action in DEVICE_CONFIRM_ACTIONS:
        return True
    if action in ("auto_run", "launch"):
        return find_project_dir(downloads_dir, task.get("project_name", ""), task.get("local_path")) is None
    return False


def needs_pc_confirmation(task: dict) -> bool:
    """Задача ждала, пока ПК был выключен/спал (или уже откладывалась) и что-то меняет на компьютере."""
    if task.get("action") not in _CONFIRM_VERBS:
        return False
    if task.get("_reminders"):
        return True
    return time.time() - float(task.get("signed_at") or time.time()) >= DEFERRED_TASK_AGE_SEC


def _ask_on_pc_blocking(title: str, text: str, ok_label: str, timeout_s: int, allow_later: bool = True) -> str:
    """Окно поверх всех окон. Возвращает yes / later / cancel / timeout. Без allow_later — только Да / Нет (Нет = cancel)."""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        MB_YESNOCANCEL, MB_YESNO, MB_ICONQUESTION, MB_SETFOREGROUND, MB_TOPMOST = 0x3, 0x4, 0x20, 0x10000, 0x40000
        flags = (MB_YESNOCANCEL if allow_later else MB_YESNO) | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST
        user32 = ctypes.windll.user32
        try:
            fn = user32.MessageBoxTimeoutW  # недокументированная, но есть во всех Windows начиная с XP
            fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD, wintypes.DWORD]
            fn.restype = ctypes.c_int
            res = fn(None, text, title, flags, 0, timeout_s * 1000)
        except AttributeError:
            res = user32.MessageBoxW(None, text, title, flags)
        return {6: "yes", 7: "later" if allow_later else "cancel", 2: "cancel", 32000: "timeout"}.get(res, "later")
    if IS_MAC:
        import subprocess as _sp
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display dialog "{esc(text)}" with title "{esc(title)}" '
            f'buttons {{"Отменить", "Позже", "{esc(ok_label)}"}} default button "{esc(ok_label)}" giving up after {timeout_s}'
        )
        try:
            out = _sp.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout_s + 30).stdout
        except Exception:
            return "later"
        if "gave up:true" in out:
            return "timeout"
        if ok_label in out:
            return "yes"
        if "Отменить" in out:
            return "cancel"
        return "later"
    return "yes"


def _load_postponed() -> list:
    try:
        data = json.loads(POSTPONED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_postponed(items: list) -> None:
    try:
        POSTPONED_FILE.parent.mkdir(parents=True, exist_ok=True)
        POSTPONED_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


async def confirm_and_run(task: dict, current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger) -> None:
    """Спрашивает на экране ПК, выполнять ли отложенную задачу; «позже» — напомнит через 30 минут."""
    action = task.get("action", "")
    verb = _CONFIRM_VERBS.get(action, "выполнить")
    name = task.get("project_name", "")
    if action in ("auto_run", "launch") and find_project_dir(downloads_dir, name, task.get("local_path")) is None:
        verb = "скачать и запустить"
    chat_id = task.get("chat_id")
    reminders = int(task.get("_reminders") or 0)
    # Свежая задача (компьютер в сети) — подтверждение вместо 2FA на компьютере пользователя: Да / Нет
    fresh = not reminders and time.time() - float(task.get("signed_at") or time.time()) < DEFERRED_TASK_AGE_SEC
    async with _CONFIRM_LOCK:
        if fresh:
            msg_id = await _tg_send(
                chat_id,
                f"На экране «{html.escape(device_label)}» появилось окно: подтвердите, что нужно {verb} <b>{html.escape(name)}</b>.",
                msg_id=task.get("message_id"),
            )
            question = f"Бот Second Brain просит {verb} «{name}».\n\nДа — {verb} сейчас\nНет — отменить"
        else:
            msg_id = await _tg_send(
                chat_id,
                f"<b>{device_label} снова в сети.</b>\n"
                f"На экране компьютера появилось окно: подтвердите, что нужно {verb} <b>{html.escape(name)}</b>.\n"
                f"<i>Задача ждёт {_waited_text(task)}.</i>",
            )
            question = (
                f"Пока компьютер был выключен, вы просили {verb} «{name}».\n\n"
                f"Да — {verb} сейчас\nНет — напомнить через 30 минут\nОтмена — отменить задачу"
            )
        logger.info(f"Подтверждение на ПК: {verb} {name} (напоминание {reminders})")
        decision = await asyncio.to_thread(
            _ask_on_pc_blocking, "Second Brain", question, verb.split()[0].capitalize(), CONFIRM_TIMEOUT_SEC, not fresh,
        )
    logger.info(f"Решение на ПК для {name}: {decision}")
    if decision == "yes":
        await _tg_send(chat_id, f"Подтверждено на компьютере. Выполняю: {verb} <b>{html.escape(name)}</b>...", msg_id=msg_id)
        task.pop("_reminders", None)
        task.pop("_remind_at", None)
        task["_confirmed"] = True
        if msg_id:
            task["message_id"] = msg_id
        await handle_incoming_task(task, current_platform, device_label, downloads_dir, logger)
        return
    if decision == "cancel":
        await _tg_send(chat_id, f"Задача отменена на компьютере: {verb} <b>{html.escape(name)}</b>.", msg_id=msg_id)
        return
    if reminders + 1 >= MAX_REMINDERS:
        await _tg_send(
            chat_id,
            f"Задача снята: {verb} <b>{html.escape(name)}</b> так и не подтвердили на компьютере. "
            f"Отправьте ссылку снова, когда будет удобно.",
            msg_id=msg_id,
        )
        return
    task["_reminders"] = reminders + 1
    task["_remind_at"] = time.time() + REMIND_AFTER_SEC
    items = [t for t in _load_postponed() if t.get("id") != task.get("id")]
    items.append(task)
    _save_postponed(items)
    reason = "На компьютере никто не ответил." if decision == "timeout" else "Отложено на компьютере."
    await _tg_send(chat_id, f"{reason} Напомню через 30 минут: {verb} <b>{html.escape(name)}</b>.", msg_id=msg_id)


def spawn_due_postponed(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger) -> None:
    items = _load_postponed()
    if not items:
        return
    now = time.time()
    due = [t for t in items if float(t.get("_remind_at") or 0) <= now]
    if not due:
        return
    _save_postponed([t for t in items if t not in due])
    for t in due:
        asyncio.create_task(confirm_and_run(t, current_platform, device_label, downloads_dir, logger))


async def announce_deferred_task(task: dict, device_label: str) -> None:
    """Новое сообщение (с уведомлением) вместо правки старого «Задача сохранена в очередь» далеко вверху чата."""
    label = _DEFERRED_LABELS.get(task.get("action", ""))
    if not label or not task.get("chat_id"):
        return
    age = time.time() - float(task.get("signed_at") or time.time())
    if age < DEFERRED_TASK_AGE_SEC:
        return
    waited = f"{int(age // 3600)} ч {int(age % 3600 // 60)} мин" if age >= 3600 else f"{max(1, int(age // 60))} мин"
    new_id = await _tg_send(
        task["chat_id"],
        f"<b>{device_label} снова в сети.</b>\n"
        f"Выполняю отложенную задачу ({label}): <b>{html.escape(task.get('project_name', ''))}</b>\n"
        f"<i>Задача ждала в очереди {waited}.</i>",
    )
    if new_id:
        task["message_id"] = new_id


async def sse_listener(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    if not (device_link.DEVICE_EVENTS_URL if device_link.is_paired() else SMEE_CHANNEL):
        logger.warning("Канал мгновенных событий не задан: задачи придут только при опросе очереди.")
        return
    logger.info("Запуск SSE слушателя событий.")

    async def _consume():
        # read-таймаут: после сна TCP-соединение может зависнуть навсегда без единой ошибки
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=None)) as client:
            events_url = device_link.DEVICE_EVENTS_URL if device_link.is_paired() else SMEE_CHANNEL
            async with client.stream("GET", events_url, headers={"Accept": "text/event-stream"}) as response:
                logger.info("SSE канал подключен.")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw_data = line[5:].strip()
                    if not raw_data or raw_data == "{}":
                        continue
                    try:
                        evt = json.loads(raw_data)
                        payload = evt.get("body", evt)
                        if not isinstance(payload, dict) or not payload.get("action"):
                            continue
                        if not is_task_authentic(payload, logger):
                            continue
                        if not is_task_for_platform(payload, current_platform):
                            continue
                        action = payload.get("action", "")
                        if action in _CONTROL_ACTIONS:
                            await handle_incoming_task(payload, current_platform, device_label, downloads_dir, logger)
                            continue
                        task_id = payload.get("id") or f"{action}_{payload.get('project_name')}_{payload.get('timestamp', int(time.time() * 1000))}"
                        if task_id and task_id not in get_processed_ids():
                            logger.info(f"Получена задача через SSE: {payload.get('project_name')} ({action})")
                            record_processed_id(task_id)
                            if payload.get("id"):
                                asyncio.create_task(_remove_task_from_kvdb_queue(str(payload["id"])))
                            await handle_incoming_task(payload, current_platform, device_label, downloads_dir, logger)
                    except Exception as e:
                        logger.error(f"Ошибка обработки SSE сообщения: {e}")

    while True:
        consume_task = asyncio.create_task(_consume())
        reconnect_task = asyncio.create_task(_SSE_RECONNECT.wait())
        done, pending = await asyncio.wait({consume_task, reconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if reconnect_task in done:
            _SSE_RECONNECT.clear()
            logger.info("Переподключение SSE после выхода из сна.")
            continue
        await asyncio.sleep(3.0)


async def heartbeat_loop(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    tick = 0
    last_wall = time.time()
    while True:
        now = time.time()
        # Цикл идёт раз в ~15 с; большой скачок системного времени означает сон/гибернацию
        if now - last_wall > 75:
            logger.info(f"Выход из сна: агент простаивал {int(now - last_wall)} с, опрашиваю очередь.")
            _WAKE_EVENT.set()
            _SSE_RECONNECT.set()
        last_wall = now
        await send_platform_heartbeat(current_platform, device_label, OS_NAME, downloads_dir, list(ACTIVE_TASKS.keys()), logger)
        tick += 1
        if tick % 2 == 0:
            await sync_installed_tools_to_cloud(downloads_dir, logger, platform=current_platform)
        await asyncio.sleep(15.0)


async def _take_task_from_kvdb(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger) -> bool:
    """Забирает одну задачу своей платформы из постоянной очереди kvdb. True — задача была."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{KVDB_BASE}/tasks_queue")
        if r.status_code != 200:
            return False
        tasks = r.json()
        if not tasks or not isinstance(tasks, list):
            return False
        idx = next((i for i, tk in enumerate(tasks) if isinstance(tk, dict) and is_task_for_platform(tk, current_platform)), None)
        if idx is None:
            return False
        task = tasks.pop(idx)
        await client.post(f"{KVDB_BASE}/tasks_queue", content=json.dumps(tasks))
    await _dispatch_queued_task(task, current_platform, device_label, downloads_dir, logger)
    return True


async def _dispatch_queued_task(task: dict, current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger) -> None:
    """Задача из постоянной очереди (kvdb владельца или очередь компьютера в облаке)."""
    action = task.get("action", "")
    if not is_task_authentic(task, logger):
        return
    if action in _CONTROL_ACTIONS:
        await handle_incoming_task(task, current_platform, device_label, downloads_dir, logger)
        return
    task_id = task.get("id") or f"{action}_{task.get('project_name')}_{task.get('timestamp', int(time.time() * 1000))}"
    if task_id not in get_processed_ids():
        logger.info(f"Получена задача из очереди: {task.get('project_name')} ({action})")
        record_processed_id(task_id)
        if needs_pc_confirmation(task):
            # Окно ждёт ответа до 15 минут — не блокируем разбор остальной очереди
            asyncio.create_task(confirm_and_run(task, current_platform, device_label, downloads_dir, logger))
        else:
            await announce_deferred_task(task, device_label)
            await handle_incoming_task(task, current_platform, device_label, downloads_dir, logger)


async def _take_tasks_from_cloud(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger) -> None:
    """Компьютер пользователя: забирает задачи из своей очереди в облаке (этот же запрос — сигнал «в сети»)."""
    info = {
        "version": AGENT_VERSION,
        "active_tasks": list(ACTIVE_TASKS.keys()),
        "tools": get_installed_tool_names(downloads_dir),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        tasks = await device_link.poll(client, info)
    for task in tasks:
        if is_task_for_platform(task, current_platform):
            await _dispatch_queued_task(task, current_platform, device_label, downloads_dir, logger)


def _notify_unpaired_blocking() -> None:
    if not IS_WINDOWS:
        return  # подключение по коду пока только для Windows
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None,
        "Этот компьютер отключён от бота (кнопка «Забыть» в Telegram).\n\n"
        "Агент остановлен. Чтобы подключить компьютер снова, запустите агент и введите новый код из бота.",
        "Second Brain",
        0x40 | 0x10000 | 0x40000,  # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
    )


async def run_agent(current_platform: str, device_label: str, downloads_dir: Path, logger: logging.Logger):
    """Главный цикл работы любого локального агента."""
    global _WAKE_EVENT, _SSE_RECONNECT, _CONFIRM_LOCK
    if IS_WINDOWS:
        current_platform = "windows"
        device_label = "Windows ПК"
    paired = device_link.is_paired()
    if paired:
        device_label = f"Компьютер «{device_link.DEVICE_NAME or 'без названия'}»"
    _WAKE_EVENT = asyncio.Event()
    _SSE_RECONNECT = asyncio.Event()
    _CONFIRM_LOCK = asyncio.Lock()
    logger.info(f"Демон {device_label} ({OS_NAME}) запущен. Опрос очереди и SSE активны.")
    if paired:
        logger.info(f"Компьютер подключён к боту по коду (устройство {device_link.DEVICE_ID}), задачи — через {device_link.CLOUD_URL}")
    else:
        if not AGENT_SECRET:
            logger.warning("В .env не задан AGENT_SECRET: агент не будет выполнять задачи из облака")
        if not KVDB_BUCKET or not SMEE_CHANNEL:
            logger.warning("В .env не заданы KVDB_BUCKET / SMEE_CHANNEL: агент не получит задачи из облака")
    await sync_installed_tools_to_cloud(downloads_dir, logger, platform=current_platform)
    asyncio.create_task(sse_listener(current_platform, device_label, downloads_dir, logger))
    asyncio.create_task(heartbeat_loop(current_platform, device_label, downloads_dir, logger))
    await send_platform_heartbeat(current_platform, device_label, OS_NAME, downloads_dir, list(ACTIVE_TASKS.keys()), logger)

    last_cloud_error = ""
    while True:
        spawn_due_postponed(current_platform, device_label, downloads_dir, logger)
        if paired:
            try:
                await _take_tasks_from_cloud(current_platform, device_label, downloads_dir, logger)
                last_cloud_error = ""
            except device_link.DeviceRevoked:
                logger.warning("Компьютер отключён в боте («Забыть»): привязка сброшена, агент останавливается.")
                device_link.forget_locally()
                await asyncio.to_thread(_notify_unpaired_blocking)
                return
            except Exception as e:
                if str(e) != last_cloud_error:  # не засоряем журнал одной и той же ошибкой каждые 30 с
                    logger.warning(f"Опрос облака не удался: {e}")
                    last_cloud_error = str(e)
            try:
                await asyncio.wait_for(_WAKE_EVENT.wait(), timeout=device_link.POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            _WAKE_EVENT.clear()
            continue

        got_task = False
        try:
            got_task = await _take_task_from_kvdb(current_platform, device_label, downloads_dir, logger)
        except Exception:
            pass
        if got_task:
            await asyncio.sleep(0.5)
            continue  # в очереди могут ждать ещё задачи — забираем подряд, без 6-секундной паузы
        try:
            await asyncio.wait_for(_WAKE_EVENT.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            pass
        _WAKE_EVENT.clear()
