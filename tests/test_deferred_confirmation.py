"""
Отложенные задачи: подтверждение на экране ПК после включения компьютера.

Запуск без интернета и без pytest:
    .venv\\Scripts\\python.exe tests\\test_deferred_confirmation.py
Окно подтверждения подменено заглушкой, которая «нажимает» нужную кнопку.
"""
import asyncio
import json
import logging
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Консоль Windows на серверах сборки — cp1252: без этого падает вывод русских названий проверок
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))

from bot import agent_core as ac  # noqa: E402

_results = []
log = logging.getLogger("test")
log.addHandler(logging.NullHandler())


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


sent, handled = [], []


async def fake_send(chat_id, text, markup=None, msg_id=None):
    sent.append(text)
    return 900


async def fake_handle(task, *a, **k):
    handled.append(dict(task))


ac._tg_send = fake_send
ac.handle_incoming_task = fake_handle


def old_task(action="clone", hours=3, **extra):
    # uuid, а не время: таймер Windows на серверах сборки грубый (~15 мс), и id совпадали
    return {"id": f"t_{action}_{uuid.uuid4().hex}", "action": action, "project_name": "notepad4", "chat_id": 1,
            "message_id": 5, "signed_at": int(time.time() - hours * 3600), **extra}


async def scenario(answer, task):
    sent.clear()
    handled.clear()
    ac._ask_on_pc_blocking = lambda *a, **k: answer
    await ac.confirm_and_run(task, "windows", "Windows ПК", Path("."), log)


async def main():
    ac._CONFIRM_LOCK = asyncio.Lock()
    with tempfile.TemporaryDirectory() as d:
        ac.POSTPONED_FILE = Path(d) / "postponed_tasks.json"

        check("свежая задача (ПК был в сети) выполняется без окна", not ac.needs_pc_confirmation(old_task(hours=0)))
        check("скачивание, ждавшее выключенный ПК, требует подтверждения", ac.needs_pc_confirmation(old_task()))
        check("инструкция без подтверждения (ничего не меняет на ПК)", not ac.needs_pc_confirmation(old_task("get_guide")))
        check("запуск и удаление требуют подтверждения", ac.needs_pc_confirmation(old_task("auto_run")) and ac.needs_pc_confirmation(old_task("uninstall")))

        await scenario("yes", old_task())
        check("«Да»: задача выполняется", len(handled) == 1)
        check("«Да»: прогресс идёт в новом сообщении", handled and handled[0]["message_id"] == 900)
        check("«Да»: в Telegram сначала просьба подтвердить, затем «выполняю»", "появилось окно" in sent[0] and "Подтверждено" in sent[-1])

        await scenario("cancel", old_task())
        check("«Отмена»: ничего не выполняется и не откладывается", not handled and not ac._load_postponed() and "отменена" in sent[-1])

        await scenario("later", old_task())
        items = ac._load_postponed()
        check("«Нет»: задача отложена на 30 минут", len(items) == 1 and abs(items[0]["_remind_at"] - time.time() - ac.REMIND_AFTER_SEC) < 5)
        check("«Нет»: в Telegram «напомню через 30 минут»", "Напомню через 30 минут" in sent[-1] and not handled)

        await scenario("timeout", old_task())
        check("никто не ответил: тоже откладывается", len(ac._load_postponed()) == 2 and "никто не ответил" in sent[-1])

        await scenario("timeout", old_task(_reminders=ac.MAX_REMINDERS - 1))
        check("после 6 напоминаний задача снимается", "снята" in sent[-1] and len(ac._load_postponed()) == 2)

        # напоминание по расписанию
        ac._save_postponed([dict(old_task(), _reminders=1, _remind_at=time.time() - 1), dict(old_task(), _reminders=1, _remind_at=time.time() + 999)])
        spawned = []

        async def fake_confirm(task, *a, **k):
            spawned.append(task)
        orig = ac.confirm_and_run
        ac.confirm_and_run = fake_confirm
        ac.spawn_due_postponed("windows", "Windows ПК", Path("."), log)
        await asyncio.sleep(0.05)
        ac.confirm_and_run = orig
        check("пришло время — окно показывается снова, остальные ждут", len(spawned) == 1 and len(ac._load_postponed()) == 1)
        check("отложенная задача при повторе снова требует подтверждения", ac.needs_pc_confirmation(spawned[0]))


asyncio.run(main())
failed = [n for n, ok in _results if not ok]
print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
sys.exit(1 if failed else 0)
