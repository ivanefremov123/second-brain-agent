import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

from bot.platform_utils import PROJECT_ROOT, PROJECTS_DIR, SCRATCH_DIR, JSON_MEMORY_FILE, QUEUE_FILE

MEMORY_TARGETS = [
    SCRATCH_DIR / "GEMINI.md",
    SCRATCH_DIR / "ANTIGRAVITY_MEMORY.md",
    PROJECT_ROOT / "GEMINI.md",
    PROJECTS_DIR / "GEMINI.md",
    PROJECTS_DIR / "INSTALLED_TOOLS.md",
]

def format_memory_entry(name: str, category: str, what_it_does: str, relevance: str = "", repo_url: Optional[str] = None) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"### [{category}] {name}",
        f"- **Суть:** {what_it_does}",
    ]
    if relevance:
        lines.append(f"- **Актуальность:** {relevance}")
    if repo_url:
        lines.append(f"- **Репозиторий:** {repo_url}")
    lines.append(f"- **Добавлено в память:** {now_str}")
    lines.append("- **Контекст для диалога:** Пользователь сохранил этот проект в долговременную память Antigravity. При обсуждении этой темы или смежных задач ассистент уже знает суть и назначение инструмента без повторных напоминаний.")
    return "\n".join(lines)

async def save_to_antigravity_memory(
    name: str,
    category: str,
    what_it_does: str,
    relevance: str = "",
    repo_url: Optional[str] = None
) -> bool:
    if not what_it_does or len(what_it_does.strip()) < 3:
        logger.warning(f"Пропуск сохранения некорректной записи в память: '{what_it_does}'")
        return False

    entry_text = format_memory_entry(name, category, what_it_does, relevance, repo_url)

    # 1. Обновляем JSON реестр памяти
    try:
        data = []
        if JSON_MEMORY_FILE.exists():
            try:
                data = json.loads(JSON_MEMORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = []
        
        # Обновляем или добавляем
        data = [item for item in data if item.get("name") != name]
        data.append({
            "name": name,
            "category": category,
            "what_it_does": what_it_does,
            "relevance": relevance,
            "repo_url": repo_url,
            "saved_at": datetime.now().isoformat()
        })
        JSON_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        JSON_MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка при сохранении в JSON память Antigravity: {e}")

    # 2. Обновляем все GEMINI.md и Markdown файлы памяти
    header = (
        "# Память и активный контекст проектов пользователя (Antigravity Memory)\n\n"
        "> Этот файл автоматически обновляется Telegram-ботом Second Brain при нажатии «Запомнить в Antigravity».\n"
        "> Antigravity автоматически считывает эти данные и всегда держит их в контексте диалога.\n\n"
    )

    for target in MEMORY_TARGETS:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = ""
            if target.exists():
                existing = target.read_text(encoding="utf-8")

            if not existing.strip():
                existing = header

            marker = f"### [{category}] {name}"
            if marker in existing:
                # Уже есть запись
                pass
            else:
                updated = existing.rstrip() + "\n\n" + entry_text + "\n"
                target.write_text(updated, encoding="utf-8")
        except Exception as e:
            logger.error(f"Ошибка при записи памяти в {target}: {e}")

    return True

def remove_from_antigravity_memory(name: str) -> Dict[str, Any]:
    """
    Полностью удаляет утилиту / проект из долговременной памяти Antigravity:
    1. Удаляет из JSON памяти (antigravity_memory.json).
    2. Очищает из очереди задач (antigravity_tasks_queue.json).
    3. Вырезает блок из всех GEMINI.md и ANTIGRAVITY_MEMORY.md.
    """
    clean_name = name.strip().lower()
    removed_from_json = False
    removed_from_md_count = 0
    removed_from_queue = 0

    # 1. Удаляем из JSON реестра памяти
    if JSON_MEMORY_FILE.exists():
        try:
            data = json.loads(JSON_MEMORY_FILE.read_text(encoding="utf-8"))
            new_data = [item for item in data if item.get("name", "").strip().lower() != clean_name]
            if len(new_data) != len(data):
                removed_from_json = True
                JSON_MEMORY_FILE.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Ошибка при удалении из JSON памяти Antigravity: {e}")

    # 2. Удаляем из очереди задач Antigravity (antigravity_tasks_queue.json)
    queue_file = QUEUE_FILE
    if queue_file.exists():
        try:
            q_data = json.loads(queue_file.read_text(encoding="utf-8"))
            new_q = [item for item in q_data if item.get("project_name", "").strip().lower() != clean_name]
            removed_from_queue = len(q_data) - len(new_q)
            if removed_from_queue > 0:
                queue_file.write_text(json.dumps(new_q, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Ошибка при очистке очереди Antigravity: {e}")

    # 3. Удаляем блок из всех Markdown файлов памяти
    # Шаблон 1: ### [Категория] name
    # Шаблон 2: ### N. name
    pat_str = rf"(?ms)^###\s+(?:\[.*?\]|\d+\.)\s+{re.escape(name)}\b.*?(?=(?:^#{1,3}\s+)|\Z)"
    pattern = re.compile(pat_str, re.IGNORECASE)

    for target in MEMORY_TARGETS:
        try:
            if target.exists():
                content = target.read_text(encoding="utf-8")
                new_content, n_subs = pattern.subn("", content)
                if n_subs > 0:
                    new_content = re.sub(r"\n{3,}", "\n\n", new_content).strip() + "\n"
                    target.write_text(new_content, encoding="utf-8")
                    removed_from_md_count += 1
        except Exception as e:
            logger.error(f"Ошибка удаления записи из {target}: {e}")

    logger.info(f"Удаление {name} из памяти Antigravity: JSON={removed_from_json}, MD={removed_from_md_count}, Queue={removed_from_queue}")
    return {
        "success": True,
        "removed_from_json": removed_from_json,
        "removed_from_md_count": removed_from_md_count,
        "removed_from_queue": removed_from_queue
    }

def find_repo_url_in_memory(name: str) -> str:
    """
    Ищет URL репозитория в JSON памяти Antigravity и GEMINI.md по имени проекта или алиасу.
    Позволяет агенту автоматически скачать проект при запуске, если папка еще не создана.
    """
    if not name:
        return ""

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower().strip())

    norm_target = norm(name)

    if JSON_MEMORY_FILE.exists():
        try:
            data = json.loads(JSON_MEMORY_FILE.read_text(encoding="utf-8"))
            for item in data:
                n = item.get("name", "")
                norm_n = norm(n)
                if norm_n == norm_target or (len(norm_target) >= 4 and (norm_n.startswith(norm_target) or norm_target.startswith(norm_n))):
                    u = item.get("repo_url", "")
                    if u:
                        return u.strip()
        except Exception:
            pass

    # Проверяем реестр установленных утилит Claude (~/.claude/installed_tools.json)
    registry_file = Path.home() / ".claude" / "installed_tools.json"
    if registry_file.exists():
        try:
            reg_data = json.loads(registry_file.read_text(encoding="utf-8"))
            for item in reg_data:
                n = item.get("name", "")
                norm_n = norm(n)
                if norm_n == norm_target or (len(norm_target) >= 4 and (norm_n.startswith(norm_target) or norm_target.startswith(norm_n))):
                    u = item.get("repo_url", "")
                    if u:
                        return u.strip()
        except Exception:
            pass

    for target in MEMORY_TARGETS:
        if target.exists():
            try:
                content = target.read_text(encoding="utf-8")
                # Ищем заголовок секции
                for line in content.split("\n"):
                    if line.startswith("### "):
                        sect_name = line.replace("### ", "").split("]")[-1].strip()
                        norm_s = norm(sect_name)
                        if norm_s == norm_target or (len(norm_target) >= 4 and (norm_s.startswith(norm_target) or norm_target.startswith(norm_s))):
                            pat_str = rf"(?ms)^###\s+(?:\[.*?\]|\d+\.)\s+{re.escape(sect_name)}\b.*?(?=(?:^#{1,3}\s+)|\Z)"
                            m = re.search(pat_str, content, re.IGNORECASE)
                            if m:
                                block = m.group(0)
                                repo_m = re.search(r"\*\*(?:Репозиторий|Ссылка):\*\*\s*(\S+)", block, re.IGNORECASE)
                                if repo_m:
                                    return repo_m.group(1).strip()
            except Exception:
                pass

    return ""

