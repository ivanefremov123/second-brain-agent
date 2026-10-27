import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Awaitable, Tuple
import zipfile
import tarfile
import time

import httpx
import certifi

from bot.config import DOWNLOADS_DIR, QUEUE_FILE
from bot.platform_utils import IS_MAC, IS_WINDOWS, keep_awake, wrap_no_sleep, extract_archive

logger = logging.getLogger(__name__)

ACTIVE_CLONES: Dict[str, Dict[str, Any]] = {}

def make_progress_bar(percent: int, length: int = 10) -> str:
    """Создает визуальную текстовую полосу загрузки: [██████░░░░]."""
    clamped = max(0, min(100, percent))
    filled = int(length * clamped / 100)
    return "█" * filled + "░" * (length - filled)

def parse_git_progress(line: str) -> Optional[Tuple[int, str]]:
    """
    Парсит строки вывода git clone --progress:
    Counting objects, Compressing objects, Receiving objects, Resolving deltas.
    Возвращает (percent, human_readable_description).
    """
    m = re.search(
        r'(Counting|Compressing|Receiving|Resolving)\s+(?:objects|deltas)?:\s+(\d+)%(?:[^(]*\(([^)]+)\))?(?:,\s*([^|\r\n]+))?(?:\|\s*([^\r\n]+))?',
        line
    )
    if m:
        stage = m.group(1)
        percent = int(m.group(2))
        counts = (m.group(3) or "").strip()
        size = (m.group(4) or "").replace("done.", "").strip()
        speed = (m.group(5) or "").strip()

        stage_names = {
            "Counting": "Подсчет объектов",
            "Compressing": "Сжатие объектов",
            "Receiving": "Загрузка файлов",
            "Resolving": "Обработка дельт"
        }
        stage_ru = stage_names.get(stage, stage)
        details = []
        if counts:
            details.append(counts)
        if size:
            details.append(size)
        if speed:
            details.append(speed)
        detail_str = f" ({', '.join(details)})" if details else ""
        return percent, f"{stage_ru}: {percent}%{detail_str}"
    return None

CANCELLED_CLONE_IDS: set = set()

def cancel_clone(clone_id: str) -> bool:
    """Останавливает процесс git clone и удаляет частичные файлы с диска."""
    clean_id = (clone_id or "").strip().lower()
    if clean_id:
        CANCELLED_CLONE_IDS.add(clean_id)

    info = None
    target_key = None
    for k, v in list(ACTIVE_CLONES.items()):
        if k.lower() == clean_id or clean_id in k.lower():
            info = v
            target_key = k
            break

    if not info:
        logger.info(f"cancel_clone: активная загрузка для '{clone_id}' не найдена в ACTIVE_CLONES")
        return False

    info["cancelled"] = True
    proc = info.get("proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    target_dir = info.get("target_dir")
    if target_dir and isinstance(target_dir, Path) and target_dir.exists():
        try:
            shutil.rmtree(target_dir, ignore_errors=True)
            logger.info(f"Удалена частично скачанная папка: {target_dir}")
        except Exception as e:
            logger.warning(f"Ошибка при очистке {target_dir}: {e}")

    logger.info(f"cancel_clone: процесс для {clone_id} успешно остановлен и помечен отмененным.")
    return True

@contextlib.asynccontextmanager
async def prevent_mac_sleep(timeout_seconds: int = 180):
    """
    Предотвращает засыпание устройства на время выполнения загрузки.
    macOS: системный caffeinate (даже при закрытой крышке и без питания).
    Windows: SetThreadExecutionState через platform_utils.keep_awake().
    """
    if not IS_MAC:
        with keep_awake():
            yield
        return

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/caffeinate", "-dims", "-t", str(timeout_seconds),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
    except Exception as e:
        logger.warning(f"Не удалось запустить caffeinate: {e}")
        proc = None

    try:
        yield
    finally:
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

def is_repo_relevant(query: str, repo_name: str, description: str) -> bool:
    """
    Проверяет, действительно ли найденный репозиторий относится к теме запроса.
    """
    q_words = set(w.lower() for w in query.replace("-", " ").replace("_", " ").split() if len(w) > 2)
    stop_words = {"mcp", "server", "tools", "tool", "and", "the", "for", "with", "скачать", "установить"}
    keywords = q_words - stop_words
    if not keywords:
        keywords = q_words

    text_to_search = f"{repo_name.lower()} {description.lower()}"
    return any(kw in text_to_search for kw in keywords)

async def search_best_github_repo(queries: List[str]) -> Optional[Dict[str, Any]]:
    """
    Ищет лучший репозиторий по списку запросов ПАРАЛЛЕЛЬНО,
    выбирая самый популярный и проверенный проект.
    """
    filtered_queries = [q for q in queries[:2] if q and len(q.strip()) > 2]
    if not filtered_queries:
        return None

    best_candidate = None

    async def fetch_query(client: httpx.AsyncClient, q: str):
        encoded_query = urllib.parse.quote(q)
        url = f"https://api.github.com/search/repositories?q={encoded_query}&sort=stars&order=desc"
        try:
            resp = await client.get(url, headers={"User-Agent": "SecondBrainBot"}, timeout=3.5)
            if resp.status_code == 200:
                return q, resp.json().get("items", [])
        except Exception as e:
            logger.warning(f"Ошибка параллельного поиска GitHub '{q}': {e}")
        return q, []

    async with httpx.AsyncClient(verify=certifi.where()) as client:
        results = await asyncio.gather(*[fetch_query(client, q) for q in filtered_queries], return_exceptions=True)

    for item in results:
        if isinstance(item, tuple):
            q, repo_items = item
            for r in repo_items[:5]:
                stars = r.get("stargazers_count", 0)
                repo_name = r.get("name", "")
                desc = r.get("description") or ""

                if not is_repo_relevant(q, repo_name, desc):
                    continue

                if stars >= 5:
                    candidate = {
                        "name": repo_name,
                        "full_name": r["full_name"],
                        "html_url": r["html_url"],
                        "clone_url": r["clone_url"],
                        "description": desc or "Без описания",
                        "stars": stars,
                        "is_verified": True
                    }
                    if not best_candidate or candidate["stars"] > best_candidate["stars"]:
                        best_candidate = candidate

    return best_candidate

async def fetch_github_readme(full_name: str) -> Optional[str]:
    """
    Скачивает текст README.md из GitHub репозитория без авторизации.
    Проверяет ветки HEAD, main, master.
    """
    if not full_name or "/" not in full_name:
        return None

    branches = ["HEAD", "main", "master"]
    async with httpx.AsyncClient(verify=certifi.where(), follow_redirects=True, timeout=4.0) as client:
        for branch in branches:
            try:
                url = f"https://raw.githubusercontent.com/{full_name}/{branch}/README.md"
                resp = await client.get(url, headers={"User-Agent": "SecondBrainBot"})
                if resp.status_code == 200 and len(resp.text.strip()) > 30:
                    return resp.text.strip()
            except Exception:
                pass
    return None

async def clone_repository(
    repo_url: str,
    project_name: str,
    clone_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
) -> Dict[str, Any]:
    """
    Клонирует git-репозиторий в каталог ~/Projects/<project_name>.
    Использует caffeinate, чтобы Mac не уснул в процессе скачивания.
    Отслеживает прогресс в реальном времени и поддерживает прерывание.
    """
    clean_name = "".join(c for c in project_name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name:
        clean_name = "downloaded_project"

    target_dir = DOWNLOADS_DIR / clean_name

    if target_dir.exists():
        # Если папка существует, но там уже лежит готовый .exe
        if list(target_dir.glob("*.exe")) or list(target_dir.glob("**/*.exe")):
            return {
                "success": True,
                "path": str(target_dir),
                "already_existed": True,
                "message": f"Папка {target_dir.name} с готовой программой уже существует в {target_dir}."
            }

    # Для проектов с готовыми релизами под Windows (Spotifast и др.) качаем готовый .exe вместо сырого кода
    if IS_WINDOWS:
        if clean_name.lower() in ("spotifast", "fastpotify") or "spotifast" in (repo_url or "").lower():
            target_dir.mkdir(parents=True, exist_ok=True)
            spotifast_zip_url = "https://github.com/crmne/spotifast/releases/download/v0.13.2/spotifast-v0.13.2-x86_64-pc-windows-msvc.zip"
            if progress_callback:
                await progress_callback(30, "Скачивание готового релиза spotifast.exe...")
            try:
                import urllib.request, zipfile
                zip_dest = target_dir / "spotifast_temp.zip"
                await asyncio.to_thread(urllib.request.urlretrieve, spotifast_zip_url, zip_dest)
                if zipfile.is_zipfile(zip_dest):
                    with zipfile.ZipFile(zip_dest, "r") as zf:
                        zf.extractall(target_dir)
                    zip_dest.unlink(missing_ok=True)
                    for exe in target_dir.glob("**/spotifast.exe"):
                        if exe.parent != target_dir:
                            try:
                                shutil.copy2(str(exe), str(target_dir / "spotifast.exe"))
                            except Exception:
                                pass
                    logger.info(f"Официальный релиз Spotifast распакован в {target_dir}")
                    if progress_callback:
                        await progress_callback(100, "Готово! Скачан spotifast.exe")
                    return {
                        "success": True,
                        "path": str(target_dir),
                        "is_release_binary": True,
                        "message": f"Готовый релиз spotifast.exe успешно сохранен в {target_dir}."
                    }
            except Exception as se:
                logger.warning(f"Ошибка скачивания релиза Spotifast: {se}")

        if "github.com" in (repo_url or ""):
            try:
                from bot.runner_service import download_github_release_binary
                target_dir.mkdir(parents=True, exist_ok=True)
                if progress_callback:
                    await progress_callback(20, "Поиск официального Windows релиза (.exe)...")
                rel_bin = await download_github_release_binary(repo_url, target_dir)
                if rel_bin and rel_bin.exists():
                    logger.info(f"Успешно скачан готовый Windows релиз вместо сырого кода: {rel_bin}")
                    if progress_callback:
                        await progress_callback(100, f"Готово! Скачан {rel_bin.name}")
                    return {
                        "success": True,
                        "path": str(target_dir),
                        "is_release_binary": True,
                        "message": f"Готовый релиз {rel_bin.name} успешно загружен в {target_dir}."
                    }
                # Если релизов нет, удаляем пустую папку, чтобы git clone мог продолжить
                if not any(target_dir.iterdir()):
                    target_dir.rmdir()
            except Exception as re_err:
                logger.warning(f"Ошибка проверки GitHub релизов: {re_err}")
                if target_dir.exists() and not any(target_dir.iterdir()):
                    target_dir.rmdir()

    async with prevent_mac_sleep(timeout_seconds=300):
        cmd = wrap_no_sleep(["git", "clone", "--progress", repo_url, str(target_dir)])
        git_env = os.environ.copy()
        for k in ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"]:
            git_env.pop(k, None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            if clone_id:
                ACTIVE_CLONES[clone_id] = {
                    "proc": proc,
                    "target_dir": target_dir,
                    "cancelled": False
                }

            stderr_chunks = []
            buf = ""
            while True:
                chunk = await proc.stderr.read(128)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
                if progress_callback:
                    buf += chunk.decode(errors="ignore")
                    if "\r" in buf or "\n" in buf:
                        parts = re.split(r"[\r\n]+", buf)
                        buf = parts[-1]
                        for p in parts[:-1]:
                            parsed = parse_git_progress(p)
                            if parsed:
                                pct, txt = parsed
                                try:
                                    await progress_callback(pct, txt)
                                except Exception:
                                    pass

            await proc.wait()

            # Проверяем, не была ли задача отменена пользователем
            clean_cid = (clone_id or "").strip().lower()
            is_cancelled = (clean_cid in CANCELLED_CLONE_IDS) or (
                clone_id and clone_id in ACTIVE_CLONES and ACTIVE_CLONES[clone_id].get("cancelled")
            )

            if clone_id:
                ACTIVE_CLONES.pop(clone_id, None)

            if is_cancelled:
                shutil.rmtree(target_dir, ignore_errors=True)
                return {
                    "success": False,
                    "cancelled": True,
                    "path": str(target_dir),
                    "message": "Клонирование остановлено пользователем."
                }

            if proc.returncode == 0:
                if progress_callback:
                    try:
                        await progress_callback(100, "Завершено: 100%")
                    except Exception:
                        pass
                return {
                    "success": True,
                    "path": str(target_dir),
                    "already_existed": False,
                    "message": f"Репозиторий успешно склонирован в ~/Projects/{target_dir.name}."
                }
            else:
                err_msg = b"".join(stderr_chunks).decode(errors="ignore")
                shutil.rmtree(target_dir, ignore_errors=True)
                return {
                    "success": False,
                    "path": str(target_dir),
                    "message": f"Ошибка git clone: {err_msg}"
                }
        except asyncio.CancelledError:
            if clone_id:
                cancel_clone(clone_id)
            raise
        except Exception as e:
            if clone_id:
                cancel_clone(clone_id)
            logger.error(f"Исключение при git clone: {e}")
            return {
                "success": False,
                "path": str(target_dir),
                "message": str(e)
            }

async def handle_app_store_download(
    clean_url: str,
    project_name: str,
    progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
) -> Dict[str, Any]:
    """
    Обрабатывает загрузку приложения из Mac App Store:
    1. Извлекает ID приложения и slug из URL.
    2. Проверяет наличие приложения в /Applications или mas list.
    3. Пытается автоматически скачать через mas install (если доступно).
    4. Если требуется Touch ID/пароль Apple ID: открывает Mac App Store прямо на странице приложения.
    5. Создает в ~/Projects/<project_name> структурированное описание вместо мусорного HTML.
    """
    clean_name = "".join(c for c in project_name if c.isalnum() or c in ("-", "_")).strip() or "app_store_app"
    target_dir = DOWNLOADS_DIR / clean_name
    target_dir.mkdir(parents=True, exist_ok=True)

    app_id = ""
    m_id = re.search(r"id(\d+)", clean_url)
    if m_id:
        app_id = m_id.group(1)

    app_slug = clean_name.replace("_", "-")
    m_slug = re.search(r"/app/([^/]+)/id\d+", clean_url)
    if m_slug:
        app_slug = m_slug.group(1)

    if progress_callback:
        await progress_callback(20, "Проверка приложения в Mac App Store...")

    # Проверяем, установлено ли уже в /Applications
    is_installed = False
    for a_dir in [Path("/Applications"), Path.home() / "Applications"]:
        if a_dir.exists():
            for a in a_dir.glob("*.app"):
                a_low = a.stem.lower()
                clean_target = clean_name.lower().replace("_", "").replace("-", "")
                clean_a = a_low.replace(" ", "").replace("_", "").replace("-", "")
                if clean_target in clean_a or clean_a in clean_target:
                    is_installed = True
                    break
        if is_installed:
            break

    # Пытаемся проверить mas list
    mas_bin = shutil.which("mas") or str(Path.home() / ".local/bin/mas")
    if not is_installed and Path(mas_bin).exists() and app_id:
        try:
            mas_proc = await asyncio.create_subprocess_exec(
                str(mas_bin), "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await mas_proc.communicate()
            if app_id in out.decode(errors="ignore"):
                is_installed = True
        except Exception:
            pass

    # Если не установлено, пытаемся установить через mas CLI с таймаутом
    auto_installed = False
    if not is_installed and Path(mas_bin).exists() and app_id:
        if progress_callback:
            await progress_callback(50, "Попытка автоматической установки из App Store...")
        try:
            inst_proc = await asyncio.create_subprocess_exec(
                str(mas_bin), "install", app_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(inst_proc.communicate(), timeout=8.0)
            if inst_proc.returncode == 0:
                is_installed = True
                auto_installed = True
        except Exception:
            try:
                inst_proc.kill()
            except Exception:
                pass

    # Если приложение не установлено автоматически — открываем Mac App Store на Mac
    if not is_installed:
        if progress_callback:
            await progress_callback(80, "Открытие страницы в Mac App Store...")
        store_url = f"macappstore://apps.apple.com/app/id{app_id}" if app_id else clean_url
        subprocess.run(["open", store_url], check=False)
        subprocess.run(["osascript", "-e", 'tell application "App Store" to activate'], check=False)

    if progress_callback:
        await progress_callback(100, "Готово!")

    # Удаляем любой старый мусорный HTML txt файл если был
    for old_txt in target_dir.glob("*.txt"):
        try:
            head = old_txt.read_text(encoding="utf-8", errors="ignore")[:300].lower()
            if "<!doctype html" in head or "<html" in head:
                old_txt.unlink(missing_ok=True)
        except Exception:
            pass

    # Создаем понятный README.md и метаданные
    meta_readme = target_dir / "README.md"
    readme_content = f"""# {project_name}

- **Тип:** Официальное приложение macOS из Mac App Store
- **ID в App Store:** {app_id or 'н/д'}
- **Ссылка:** {clean_url}
- **Статус на Mac:** {'Установлено в /Applications' if is_installed else 'Открыто в Mac App Store для установки'}

### Как запустить
Нажмите кнопку « Запустить на Mac» в Telegram — бот запустит программу из /Applications или откроет её страницу в App Store.
"""
    meta_readme.write_text(readme_content, encoding="utf-8")

    meta_json = target_dir / "APP_STORE.json"
    meta_json.write_text(json.dumps({
        "name": project_name,
        "app_id": app_id,
        "url": clean_url,
        "is_installed": is_installed,
        "type": "app_store"
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "success": True,
        "path": str(target_dir),
        "type": "app_store",
        "app_id": app_id,
        "is_installed": is_installed,
        "auto_installed": auto_installed,
        "message": "Приложение установлено из App Store!" if auto_installed else (
            "Приложение уже установлено на Mac!" if is_installed else
            "Страница приложения открыта в Mac App Store на вашем Mac."
        )
    }

async def download_resource(
    url: str,
    project_name: str,
    clone_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
) -> Dict[str, Any]:
    """
    Универсальная загрузка ресурсов:
    - Если git-репозиторий (github, gitlab, bitbucket, .git): клонирует через git clone.
    - Если ссылка из Mac App Store (apps.apple.com): обрабатывает через Mac App Store / mas CLI.
    - Если прямая ссылка, архив (.zip, .tar.gz) или файл: скачивает curl/httpx и распаковывает в ~/Projects/<project_name>.
    """
    clean_url = (url or "").strip()
    is_git = (
        clean_url.endswith(".git") or
        "github.com" in clean_url or
        "gitlab.com" in clean_url or
        "bitbucket.org" in clean_url or
        "codeberg.org" in clean_url
    )
    if is_git:
        return await clone_repository(
            repo_url=clean_url,
            project_name=project_name,
            clone_id=clone_id,
            progress_callback=progress_callback
        )

    is_app_store = (
        "apps.apple.com" in clean_url or
        "itunes.apple.com" in clean_url or
        clean_url.startswith("macappstore://")
    )
    if is_app_store:
        return await handle_app_store_download(
            clean_url=clean_url,
            project_name=project_name,
            progress_callback=progress_callback
        )

    clean_name = "".join(c for c in project_name if c.isalnum() or c in ("-", "_")).strip()
    if not clean_name:
        clean_name = "downloaded_tool"

    target_dir = DOWNLOADS_DIR / clean_name
    if target_dir.exists():
        return {
            "success": True,
            "path": str(target_dir),
            "already_existed": True,
            "message": f"Папка {target_dir.name} уже существует в ~/Projects."
        }

    target_dir.mkdir(parents=True, exist_ok=True)
    temp_file = target_dir / f"download_temp_{int(time.time())}"

    async with prevent_mac_sleep(timeout_seconds=300):
        if progress_callback:
            await progress_callback(10, "Загрузка файла по ссылке...")

        cmd = wrap_no_sleep(["curl", "-L", "-sS", "--max-time", "180", "-o", str(temp_file), clean_url])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            if clone_id:
                ACTIVE_CLONES[clone_id] = {
                    "proc": proc,
                    "target_dir": target_dir,
                    "cancelled": False
                }

            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                shutil.rmtree(target_dir, ignore_errors=True)
                err_text = stderr.decode(errors="ignore") if stderr else f"код возврата {proc.returncode}"
                return {"success": False, "message": f"Ошибка скачивания файла: {err_text}"}

            if progress_callback:
                await progress_callback(60, "Анализ и подготовка файлов...")

            # zip / 7z / tar.* — с защитой от путей вида ../ внутри архива
            is_archive = await asyncio.to_thread(extract_archive, temp_file, target_dir)
            if is_archive:
                temp_file.unlink(missing_ok=True)

            if is_archive:
                # 1. Если внутри архива лежит одна корневая папка - поднимаем всё на уровень выше
                try:
                    subdirs = [d for d in target_dir.iterdir() if d.is_dir()]
                    subfiles = [f for f in target_dir.iterdir() if f.is_file()]
                    if len(subdirs) == 1 and not subfiles:
                        single_child = subdirs[0]
                        for item in single_child.iterdir():
                            dest = target_dir / item.name
                            if dest.exists():
                                if dest.is_dir():
                                    shutil.rmtree(dest, ignore_errors=True)
                                else:
                                    dest.unlink(missing_ok=True)
                            shutil.move(str(item), str(dest))
                        shutil.rmtree(single_child, ignore_errors=True)

                    # 2. Если исполняемый файл Windows лежит во вложенной папке - копируем в корень
                    for exe in list(target_dir.glob("**/*.exe")) + list(target_dir.glob("**/*.msi")):
                        if exe.parent != target_dir and ".venv" not in exe.parts and "node_modules" not in exe.parts:
                            dest_exe = target_dir / exe.name
                            if not dest_exe.exists():
                                shutil.copy2(str(exe), str(dest_exe))
                except Exception as fe:
                    logger.warning(f"Ошибка выравнивания структуры архива: {fe}")

            if not is_archive:
                # Одиночный файл или скрипт
                parsed_path = urllib.parse.urlparse(clean_url).path
                ext = Path(parsed_path).suffix
                is_html_content = False
                sample = ""
                if temp_file.exists():
                    try:
                        with open(temp_file, "r", encoding="utf-8", errors="ignore") as f:
                            sample = f.read(65536)
                            sample_lower = sample.lower()
                            if "<!doctype html" in sample_lower or "<html" in sample_lower or "<head" in sample_lower or ext.lower() in (".html", ".htm"):
                                is_html_content = True
                            elif not ext and (sample_lower.strip().startswith("{") or sample_lower.strip().startswith("[")):
                                ext = ".json"
                    except Exception as e:
                        logger.warning(f"Ошибка проверки содержимого файла: {e}")

                if is_html_content:
                    # Интеллектуальный анализ скачанного HTML на случай, если ссылка была на сайт-лендинг
                    gh_m = re.findall(r"github\.com/([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+)", sample)
                    ignored = {"topics", "features", "pricing", "sponsors", "site", "about", "security", "enterprise", "customer-stories", "readme", "pulls", "issues"}
                    valid_gh = []
                    for o, r in gh_m:
                        r_clean = r.removesuffix(".git").rstrip("/").split("/")[0].split("#")[0].split("?")[0]
                        if o.lower() not in ignored and r_clean.lower() not in ignored and not r_clean.startswith("."):
                            valid_gh.append(f"https://github.com/{o}/{r_clean}")
                    if valid_gh:
                        real_gh_repo = valid_gh[0]
                        logger.info(f"В скачанном HTML найден GitHub репозиторий {real_gh_repo}. Переключаемся на git clone...")
                        temp_file.unlink(missing_ok=True)
                        shutil.rmtree(target_dir, ignore_errors=True)
                        return await clone_repository(
                            repo_url=real_gh_repo,
                            project_name=project_name,
                            clone_id=clone_id,
                            progress_callback=progress_callback
                        )

                    # Ищем установщик Windows (.exe, .msi, .zip)
                    inst_matches = re.findall(r'href=[\'"](https?://[^\'"]+\.(?:exe|msi|zip))[\'"]', sample, re.IGNORECASE)
                    if inst_matches:
                        win_inst = inst_matches[0]
                        for link in inst_matches:
                            if any(k in link.lower() for k in ["win", "windows", "setup", "x64", "x86_64"]):
                                win_inst = link
                                break
                        logger.info(f"В скачанном HTML найдена ссылка на установщик: {win_inst}. Загружаем...")
                        temp_file.unlink(missing_ok=True)
                        shutil.rmtree(target_dir, ignore_errors=True)
                        return await download_resource(
                            url=win_inst,
                            project_name=project_name,
                            clone_id=clone_id,
                            progress_callback=progress_callback
                        )

                    # Чистый веб-сайт (SaaS), сохранять мертвый HTML на диск пользователю НЕ НУЖНО!
                    temp_file.unlink(missing_ok=True)
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return {
                        "success": False,
                        "is_pure_saas": True,
                        "message": f"{project_name} является онлайн веб-сервисом и работает прямо в браузере. Локальная установка не требуется."
                    }
                if not ext:
                    ext = ".txt"
                final_file = target_dir / f"{clean_name}{ext}"
                temp_file.rename(final_file)
                if ext in (".sh", ".py", ".rb", ".pl"):
                    try:
                        os.chmod(final_file, 0o755)
                    except Exception:
                        pass

            # Если в архиве была одна вложенная директория, поднимаем её содержимое вверх
            subdirs = [p for p in target_dir.iterdir() if p.is_dir()]
            files = [p for p in target_dir.iterdir() if p.is_file()]
            if len(subdirs) == 1 and not files:
                single_sub = subdirs[0]
                for item in single_sub.iterdir():
                    shutil.move(str(item), str(target_dir))
                shutil.rmtree(single_sub, ignore_errors=True)

            if progress_callback:
                await progress_callback(100, "Завершено: 100%")

            return {
                "success": True,
                "path": str(target_dir),
                "already_existed": False,
                "message": f"Файлы успешно загружены в ~/Projects/{target_dir.name}"
            }
        except asyncio.CancelledError:
            if clone_id:
                cancel_clone(clone_id)
            raise
        except Exception as e:
            if clone_id:
                cancel_clone(clone_id)
            logger.error(f"Исключение при скачивании по ссылке: {e}")
            return {
                "success": False,
                "path": str(target_dir),
                "message": str(e)
            }

def queue_task_for_antigravity(
    project_name: str,
    repo_url: Optional[str],
    local_path: str,
    note_text: str,
    action: str = "setup_and_analyze",
    item_type: str = "Проект"
) -> Dict[str, Any]:
    """
    Записывает задачу в общий файл очереди для Antigravity.
    """
    task = {
        "id": f"task_{int(datetime.now().timestamp())}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project_name": project_name,
        "repo_url": repo_url,
        "local_path": local_path,
        "action": action,
        "item_type": item_type,
        "status": "ready_for_agent",
        "user_note": note_text
    }

    current_tasks = []
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                current_tasks = json.load(f)
        except Exception:
            current_tasks = []

    current_tasks.append(task)

    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(current_tasks, f, ensure_ascii=False, indent=2)

    logger.info(f"Задача сохранена в {QUEUE_FILE}: {task['id']}")
    return task
