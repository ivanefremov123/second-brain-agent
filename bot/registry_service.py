import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from bot.platform_utils import PROJECTS_DIR, OS_NAME

logger = logging.getLogger(__name__)

HOME_DIR = Path.home()
CLAUDE_DIR = HOME_DIR / ".claude"
CATALOG_FILE = PROJECTS_DIR / "INSTALLED_TOOLS.md"
GLOBAL_CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
REGISTRY_JSON = CLAUDE_DIR / "installed_tools.json"

def init_registry_files():
    """Создает базовые каталоги и файлы реестра, если они не существуют."""
    try:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        (CLAUDE_DIR / "skills").mkdir(parents=True, exist_ok=True)
        (CLAUDE_DIR / "commands").mkdir(parents=True, exist_ok=True)
        (CLAUDE_DIR / "agents").mkdir(parents=True, exist_ok=True)
        (CLAUDE_DIR / "rules").mkdir(parents=True, exist_ok=True)

        if not CATALOG_FILE.exists():
            CATALOG_FILE.write_text(
                f"# Реестр установленных инструментов и скиллов на {OS_NAME}\n\n"
                "Этот файл автоматически обновляется Telegram-ботом Second Brain при установке новых проектов.\n\n",
                encoding="utf-8"
            )

        if not REGISTRY_JSON.exists():
            REGISTRY_JSON.write_text("[]", encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка при инициализации файлов реестра: {e}")

async def register_installed_tool(
    project_name: str,
    local_path: str,
    category: str,
    what_it_does: str,
    repo_url: Optional[str] = None,
    launch_command: Optional[str] = None
) -> Dict[str, Any]:
    """
    Регистрирует установленный инструмент или скилл:
    1. Обновляет INSTALLED_TOOLS.md в ~/Projects.
    2. Добавляет запись в ~/.claude/installed_tools.json.
    3. Обновляет глобальный ~/.claude/CLAUDE.md, чтобы Claude Code и терминальные LLM видели скиллы.
    4. Если проект содержит скиллы/команды, экспортирует их в ~/.claude/skills/ и ~/.claude/commands/.
    """
    init_registry_files()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    p = Path(local_path)

    # 1. Проверяем наличие скиллов и команд внутри установленного проекта
    exported_skills: List[str] = []
    exported_commands: List[str] = []

    if p.exists() and p.is_dir():
        # Скиллы
        skills_dir = p / "skills"
        if skills_dir.exists() and skills_dir.is_dir():
            target_skills_dir = CLAUDE_DIR / "skills"
            for item in skills_dir.iterdir():
                if item.name.startswith("."):
                    continue
                dest = target_skills_dir / item.name
                if not dest.exists():
                    try:
                        import shutil
                        if item.is_dir():
                            shutil.copytree(item, dest)
                        else:
                            shutil.copy2(item, dest)
                        exported_skills.append(item.name)
                    except Exception as e:
                        logger.warning(f"Не удалось скопировать скилл {item.name}: {e}")

        # Команды
        commands_dir = p / "commands"
        if commands_dir.exists() and commands_dir.is_dir():
            target_cmd_dir = CLAUDE_DIR / "commands"
            for item in commands_dir.glob("*.md"):
                dest = target_cmd_dir / item.name
                if not dest.exists():
                    try:
                        import shutil
                        shutil.copy2(item, dest)
                        exported_commands.append(item.stem)
                    except Exception as e:
                        logger.warning(f"Не удалось скопировать команду {item.name}: {e}")

        # Отдельный SKILL.md в корне
        skill_file = p / "SKILL.md"
        if skill_file.exists():
            dest = CLAUDE_DIR / "skills" / f"{project_name}.md"
            if not dest.exists():
                try:
                    import shutil
                    shutil.copy2(skill_file, dest)
                    exported_skills.append(project_name)
                except Exception as e:
                    logger.warning(f"Не удалось скопировать SKILL.md: {e}")

    # 2. Обновляем JSON реестр
    registry_data = []
    try:
        if REGISTRY_JSON.exists():
            registry_data = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
    except Exception:
        registry_data = []

    # Удаляем старую запись, если уже была
    registry_data = [t for t in registry_data if t.get("name") != project_name]

    entry = {
        "name": project_name,
        "category": category,
        "path": local_path,
        "what_it_does": what_it_does,
        "repo_url": repo_url,
        "launch_command": launch_command,
        "installed_at": now_str,
        "exported_skills": exported_skills,
        "exported_commands": exported_commands
    }
    registry_data.append(entry)

    try:
        REGISTRY_JSON.write_text(json.dumps(registry_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка сохранения JSON реестра: {e}")

    # 3. Пересоздаем ~/Projects/INSTALLED_TOOLS.md
    try:
        md_lines = [
            f"# Реестр установленных инструментов и скиллов на {OS_NAME}\n",
            "> Этот каталог обновляется автоматически при установке инструментов через Second Brain.\n"
        ]
        for t in reversed(registry_data):
            md_lines.append(f"## {t['name']}")
            md_lines.append(f"- **Категория:** {t['category']}")
            md_lines.append(f"- **Путь:** `{t['path']}`")
            md_lines.append(f"- **Назначение:** {t['what_it_does']}")
            if t.get("repo_url"):
                md_lines.append(f"- **Репозиторий:** {t['repo_url']}")
            if t.get("launch_command"):
                md_lines.append(f"- **Команда запуска:** `{t['launch_command']}`")
            if t.get("exported_commands"):
                cmds = ", ".join(f"/{c}" for c in t["exported_commands"][:10])
                md_lines.append(f"- **Команды для Claude Code:** {cmds}")
            if t.get("exported_skills"):
                sks = ", ".join(t["exported_skills"][:10])
                md_lines.append(f"- **Скиллы для агентов:** {sks}")
            md_lines.append(f"- **Установлено:** {t['installed_at']}\n")

        CATALOG_FILE.write_text("\n".join(md_lines), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка сохранения INSTALLED_TOOLS.md: {e}")

    # 4. Обновляем ~/.claude/CLAUDE.md для чтения всеми нейросетями в Claude Code
    try:
        claude_md_lines = [
            "# Global Claude Code Context and Installed Skills\n",
            "This file is automatically generated by Second Brain. It lists all active tools and skills available on this system:\n"
        ]
        for t in registry_data:
            claude_md_lines.append(f"- **{t['name']}** ({t['category']}): {t['what_it_does']}")
            if t.get("exported_commands"):
                claude_md_lines.append(f"  Slash commands: {', '.join('/' + c for c in t['exported_commands'][:8])}")
            if t.get("exported_skills"):
                claude_md_lines.append(f"  Skills: {', '.join(t['exported_skills'][:8])}")
            claude_md_lines.append(f"  Location: {t['path']}")

        claude_md_lines.append("\n## Instructions for AI:")
        claude_md_lines.append("You have access to the above installed tools and skills. Check ~/.claude/skills and ~/.claude/commands when relevant.\n")

        GLOBAL_CLAUDE_MD.write_text("\n".join(claude_md_lines), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка обновления CLAUDE.md: {e}")

    return {
        "success": True,
        "exported_skills": exported_skills,
        "exported_commands": exported_commands,
        "catalog_path": str(CATALOG_FILE)
    }

def unregister_installed_tool(project_name: str) -> Dict[str, Any]:
    """
    Удаляет установленный инструмент из реестров:
    1. Удаляет экспортированные скиллы и команды из ~/.claude/skills/ и ~/.claude/commands/
    2. Удаляет запись из ~/.claude/installed_tools.json
    3. Пересоздает ~/Projects/INSTALLED_TOOLS.md
    4. Пересоздает ~/.claude/CLAUDE.md
    """
    init_registry_files()
    removed_skills = []
    removed_commands = []

    registry_data = []
    try:
        if REGISTRY_JSON.exists():
            registry_data = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
    except Exception:
        registry_data = []

    to_remove = [t for t in registry_data if t.get("name") == project_name]
    for item in to_remove:
        # Удаляем экспортированные скиллы
        for sk in item.get("exported_skills", []):
            sk_path = CLAUDE_DIR / "skills" / sk
            if sk_path.exists():
                try:
                    import shutil
                    if sk_path.is_dir():
                        shutil.rmtree(sk_path, ignore_errors=True)
                    else:
                        sk_path.unlink()
                    removed_skills.append(sk)
                except Exception as e:
                    logger.warning(f"Ошибка удаления скилла {sk}: {e}")

        single_sk = CLAUDE_DIR / "skills" / f"{project_name}.md"
        if single_sk.exists():
            try:
                single_sk.unlink()
                removed_skills.append(f"{project_name}.md")
            except Exception:
                pass

        # Удаляем экспортированные команды
        for cmd in item.get("exported_commands", []):
            cmd_path = CLAUDE_DIR / "commands" / f"{cmd}.md"
            if cmd_path.exists():
                try:
                    cmd_path.unlink()
                    removed_commands.append(cmd)
                except Exception as e:
                    logger.warning(f"Ошибка удаления команды {cmd}: {e}")

    registry_data = [t for t in registry_data if t.get("name") != project_name]

    try:
        REGISTRY_JSON.write_text(json.dumps(registry_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка обновления JSON реестра: {e}")

    # Пересоздаем ~/Projects/INSTALLED_TOOLS.md
    try:
        md_lines = [
            f"# Реестр установленных инструментов и скиллов на {OS_NAME}\n",
            "> Этот каталог обновляется автоматически при установке инструментов через Second Brain.\n"
        ]
        for t in reversed(registry_data):
            md_lines.append(f"## {t['name']}")
            md_lines.append(f"- **Категория:** {t['category']}")
            md_lines.append(f"- **Путь:** `{t['path']}`")
            md_lines.append(f"- **Назначение:** {t['what_it_does']}")
            if t.get("repo_url"):
                md_lines.append(f"- **Репозиторий:** {t['repo_url']}")
            if t.get("launch_command"):
                md_lines.append(f"- **Команда запуска:** `{t['launch_command']}`")
            if t.get("exported_commands"):
                cmds = ", ".join(f"/{c}" for c in t["exported_commands"][:10])
                md_lines.append(f"- **Команды для Claude Code:** {cmds}")
            if t.get("exported_skills"):
                sks = ", ".join(t["exported_skills"][:10])
                md_lines.append(f"- **Скиллы для агентов:** {sks}")
            md_lines.append(f"- **Установлено:** {t['installed_at']}\n")

        CATALOG_FILE.write_text("\n".join(md_lines), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка сохранения INSTALLED_TOOLS.md: {e}")

    # Пересоздаем CLAUDE.md
    try:
        claude_md_lines = [
            "# Global Claude Code Context and Installed Skills\n",
            "This file is automatically generated by Second Brain. It lists all active tools and skills available on this system:\n"
        ]
        for t in registry_data:
            claude_md_lines.append(f"- **{t['name']}** ({t['category']}): {t['what_it_does']}")
            if t.get("exported_commands"):
                claude_md_lines.append(f"  Slash commands: {', '.join('/' + c for c in t['exported_commands'][:8])}")
            if t.get("exported_skills"):
                claude_md_lines.append(f"  Skills: {', '.join(t['exported_skills'][:8])}")
            claude_md_lines.append(f"  Location: {t['path']}")

        claude_md_lines.append("\n## Instructions for AI:")
        claude_md_lines.append("You have access to the above installed tools and skills. Check ~/.claude/skills and ~/.claude/commands when relevant.\n")

        GLOBAL_CLAUDE_MD.write_text("\n".join(claude_md_lines), encoding="utf-8")
    except Exception as e:
        logger.error(f"Ошибка обновления CLAUDE.md: {e}")

    return {
        "success": True,
        "removed_skills": removed_skills,
        "removed_commands": removed_commands
    }
