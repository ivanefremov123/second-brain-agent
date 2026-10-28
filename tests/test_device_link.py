"""Агент на компьютере, подключённом по коду (Р-16): настройки, подписи, задачи и сообщения через облако.
Без интернета: облако подменено через httpx.MockTransport."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Отдельная папка данных: тест не читает и не портит настройки агента на этом компьютере
TMP = Path(tempfile.mkdtemp(prefix="sb_device_test_"))
os.environ["SB_APP_DIR"] = str(TMP)
for k in ("DEVICE_ID", "DEVICE_KEY", "DEVICE_EVENTS_URL", "DEVICE_NAME", "AGENT_SECRET"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from bot import device_link as dl
from bot import agent_core as ac
from bot import platform_utils as pu

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)

log = logging.getLogger("t")
log.addHandler(logging.NullHandler())

DEV_ID, DEV_KEY = "d0123456789abcdef", "k" * 64

def sign_task(task, key=DEV_KEY, device_id=DEV_ID, signed_at=None):
    task = dict(task, device_id=device_id, signed_at=signed_at if signed_at is not None else int(time.time()))
    body = json.dumps(task, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    task["sig"] = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return task

check("data dir override (SB_APP_DIR)", pu.APP_DIR == TMP and pu.ENV_FILE.parent == TMP)
check("not paired initially", not dl.is_paired())

# --- Сохранение настроек ---
pu.ENV_FILE.write_text("# комментарий\nDOWNLOADS_DIR=D:\\Projects\nDEVICE_ID=old\n", encoding="utf-8")
dl.save_settings({"DEVICE_ID": DEV_ID, "DEVICE_NAME": 'Дом "ПК"'})
env_text = pu.ENV_FILE.read_text(encoding="utf-8")
check("settings: other lines kept", "# комментарий" in env_text and "DOWNLOADS_DIR=D:\\Projects" in env_text)
check("settings: key replaced, not duplicated", env_text.count("DEVICE_ID=") == 1 and DEV_ID in env_text)
try:
    from dotenv import dotenv_values
    check("settings: quoted name reads back", dotenv_values(pu.ENV_FILE).get("DEVICE_NAME") == 'Дом "ПК"')
except ImportError:
    pass
dl.save_settings({"DEVICE_ID": None, "DEVICE_NAME": None})
check("settings: None removes key", "DEVICE_" not in pu.ENV_FILE.read_text(encoding="utf-8"))

# --- Подключение кодом ---
calls = []
def fake_post(url, json=None, timeout=None):
    calls.append((url, json))
    req = httpx.Request("POST", url)
    if json["code"] == "123456":
        return httpx.Response(200, json={"device_id": DEV_ID, "device_key": DEV_KEY,
                                         "events_url": "https://smee.io/sbd-x", "name": json["name"]}, request=req)
    if json["code"] == "222222":
        return httpx.Response(429, json={"error": "too_many_attempts"}, request=req)
    return httpx.Response(400, json={"error": "bad_code"}, request=req)
real_post = httpx.post
httpx.post = fake_post

for bad, expected in (("12345", "6 цифр"), ("654321", "Неверный"), ("222222", "10 минут")):
    try:
        dl.pair(bad, "PC")
        check(f"pair {bad}: error expected", False)
    except dl.PairError as e:
        check(f"pair {bad}: understandable error", expected in str(e))
check("pair: not paired after errors", not dl.is_paired())

dl.pair("123 456", "Рабочий ПК")
check("pair: code normalized, sent to cloud", calls[-1][1]["code"] == "123456" and calls[-1][0].endswith("/api/agent/pair"))
check("pair: paired in memory", dl.is_paired() and dl.DEVICE_ID == DEV_ID and dl.DEVICE_NAME == "Рабочий ПК")
env_text = pu.ENV_FILE.read_text(encoding="utf-8")
check("pair: saved to settings file", DEV_ID in env_text and DEV_KEY in env_text and "sbd-x" in env_text)
httpx.post = real_post

# --- Подпись задач ключом устройства ---
os.environ["AGENT_SECRET"] = ""
check("task: valid device signature accepted", ac.is_task_authentic(sign_task({"action": "hello", "chat_id": 5}), log))
check("task: other device's key rejected", not ac.is_task_authentic(sign_task({"action": "hello"}, key="x" * 64), log))
check("task: addressed to another device rejected", not ac.is_task_authentic(sign_task({"action": "hello"}, device_id="dffffffffffffffff"), log))
t = sign_task({"action": "clone", "project_name": "lazygit"}); t["project_name"] = "evil"
check("task: tampered task rejected", not ac.is_task_authentic(t, log))
check("task: older than a day rejected", not ac.is_task_authentic(sign_task({"action": "hello"}, signed_at=int(time.time()) - 2 * 86400), log))
ac.AGENT_SECRET = "owner-secret"
owner_body = {"action": "hello", "signed_at": int(time.time())}
owner_task = dict(owner_body, sig=hmac.new(b"owner-secret", json.dumps(owner_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(), hashlib.sha256).hexdigest())
check("task: owner-signed task not accepted on a user's PC", not ac.is_task_authentic(owner_task, log))
ac.AGENT_SECRET = ""

# --- Запросы к облаку ---
seen = []
mode = {"poll": "ok"}
def cloud(request: httpx.Request) -> httpx.Response:
    body = request.content
    endpoint = request.url.path.rsplit("/", 1)[-1]
    ts = request.headers["x-device-ts"]
    expected = hmac.new(DEV_KEY.encode(), f"{endpoint}\n{DEV_ID}\n{ts}\n".encode() + body, hashlib.sha256).hexdigest()
    seen.append((endpoint, json.loads(body), request.headers["x-device-sig"] == expected and request.headers["x-device-id"] == DEV_ID))
    if endpoint == "poll":
        if mode["poll"] == "revoked":
            return httpx.Response(401, json={"error": "revoked"})
        return httpx.Response(200, json={"tasks": [sign_task({"id": f"hello_{time.time_ns()}", "action": "hello", "chat_id": 5, "platform": "windows"})]})
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

real_client = httpx.AsyncClient
class MockClient(real_client):
    def __init__(self, *a, **k):
        k.pop("transport", None)
        super().__init__(*a, transport=httpx.MockTransport(cloud), **k)
httpx.AsyncClient = MockClient
ac.httpx.AsyncClient = MockClient

async def scenario():
    msg_id = await ac._tg_send(5, "<b>Прогресс</b>")
    check("telegram: sent via cloud /report, signed", seen and seen[-1][0] == "report" and seen[-1][2])
    check("telegram: message_id returned from cloud", msg_id == 42)
    check("telegram: no direct call to api.telegram.org needed", seen[-1][1]["method"] == "sendMessage")

    seen.clear()
    await ac._take_tasks_from_cloud("windows", "Компьютер «Рабочий ПК»", TMP, log)
    poll = [s for s in seen if s[0] == "poll"]
    check("poll: signed, carries version and tools", poll and poll[0][2] and poll[0][1]["version"] == ac.AGENT_VERSION and "tools" in poll[0][1])
    replies = [s for s in seen if s[0] == "report"]
    check("poll: 'hello' task answered in Telegram", replies and "на связи" in replies[-1][1]["payload"]["text"])

    mode["poll"] = "revoked"
    try:
        async with httpx.AsyncClient() as client:
            await dl.poll(client, {})
        check("poll: revoked raises DeviceRevoked", False)
    except dl.DeviceRevoked:
        check("poll: revoked raises DeviceRevoked", True)

asyncio.run(scenario())

# --- Этап 4: подтверждение на экране вместо 2FA ---
proj_dir = TMP / "Projects"
(proj_dir / "lazygit").mkdir(parents=True, exist_ok=True)
check("confirm: download needs 'Да' on screen", ac.needs_device_confirmation({"action": "clone"}, proj_dir))
check("confirm: uninstall and runtime install too",
      ac.needs_device_confirmation({"action": "uninstall"}, proj_dir) and ac.needs_device_confirmation({"action": "install_runtime"}, proj_dir))
check("confirm: running a downloaded program — no window",
      not ac.needs_device_confirmation({"action": "auto_run", "project_name": "lazygit"}, proj_dir))
check("confirm: 'run' of a missing program means download — window",
      ac.needs_device_confirmation({"action": "auto_run", "project_name": "notdownloaded"}, proj_dir))
check("confirm: check-connection needs no window", not ac.needs_device_confirmation({"action": "hello"}, proj_dir))

asked, ran = [], []
real_ask, real_process = ac._ask_on_pc_blocking, ac.process_task
async def fake_process(task, *a, **k):
    ran.append(task)
    if task.get("action") == "install_runtime":
        return await real_process(task, *a, **k)
ac.process_task = fake_process

async def confirm_scenario():
    ac._CONFIRM_LOCK = asyncio.Lock()
    answer = {"v": "cancel"}
    def fake_ask(title, text, ok, timeout, allow_later=True):
        asked.append((text, allow_later)); return answer["v"]
    ac._ask_on_pc_blocking = fake_ask
    task = sign_task({"id": f"c_{time.time_ns()}", "action": "clone", "project_name": "fzf", "chat_id": 5, "message_id": 9})
    await ac.handle_incoming_task(dict(task), "windows", "Компьютер «Рабочий ПК»", proj_dir, log)
    await asyncio.sleep(0.2)
    check("confirm: fresh task asks Да/Нет (no 'later')", asked and asked[-1][1] is False and "просит скачать" in asked[-1][0])
    check("confirm: 'Нет' — nothing downloaded", not ran)
    answer["v"] = "yes"
    await ac.handle_incoming_task(dict(task), "windows", "Компьютер «Рабочий ПК»", proj_dir, log)
    await asyncio.sleep(0.3)
    check("confirm: 'Да' — download runs once", len(ran) == 1 and ran[0]["action"] == "clone" and ran[0].get("_confirmed"))
    ran.clear()
    run_task = sign_task({"id": f"r_{time.time_ns()}", "action": "auto_run", "project_name": "lazygit", "chat_id": 5})
    n_asked = len(asked)
    await ac.handle_incoming_task(run_task, "windows", "Компьютер «Рабочий ПК»", proj_dir, log)
    await asyncio.sleep(0.2)
    check("confirm: run of downloaded program starts without a window", len(asked) == n_asked and ran and ran[0]["action"] == "auto_run")

    # Git: после установки продолжается скачивание, а не запуск
    ran.clear()
    async def fake_install(rt):
        return {"success": True}
    ac.install_runtime = fake_install
    await real_process({"action": "install_runtime", "runtime": "git", "project_name": "fzf", "repo_url": "https://github.com/junegunn/fzf.git", "chat_id": 5},
                       "windows", "Компьютер «Рабочий ПК»", proj_dir, log)
    check("git: after install the download continues", ran and ran[-1]["action"] == "clone" and ran[-1]["repo_url"].endswith("fzf.git"))

asyncio.run(confirm_scenario())
ac._ask_on_pc_blocking, ac.process_task = real_ask, real_process

from bot import github_service as gs
from bot import runner_service as rs
check("git: in the winget whitelist", rs.RUNTIMES["git"]["winget"] == "Git.Git")
real_avail = rs.is_runtime_available
rs.is_runtime_available = lambda key: key != "git"
res = asyncio.run(gs.clone_repository("https://gitlab.com/example/sb-no-git-test.git", f"sb_no_git_{time.time_ns()}"))
rs.is_runtime_available = real_avail
check("git: missing Git -> offer to install instead of an error", res.get("type") == "missing_runtime" and res.get("runtime") == "git")

# --- Окружение Python — только Python-проектам ---
for name, files in {"py_req": ["requirements.txt"], "py_src": ["main.py"], "go_proj": ["go.mod", "main.go"], "exe_build": ["tool.exe"]}.items():
    d = TMP / "venvcheck" / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("", encoding="utf-8")
check("venv: Python projects get an environment", rs.is_python_project(TMP / "venvcheck" / "py_req") and rs.is_python_project(TMP / "venvcheck" / "py_src"))
check("venv: Go project and ready .exe do not", not rs.is_python_project(TMP / "venvcheck" / "go_proj") and not rs.is_python_project(TMP / "venvcheck" / "exe_build"))
check("venv: skipped without creating .venv", asyncio.run(rs.prepare_python_env(str(TMP / "venvcheck" / "go_proj"))).get("skipped")
      and not (TMP / "venvcheck" / "go_proj" / ".venv").exists())

# --- Нет обращений к kvdb владельца ---
check("no kvdb heartbeat/sync when paired",
      asyncio.run(ac.send_platform_heartbeat("windows", "x", "Windows", TMP, [], log)) is None
      and not any(s[0] not in ("poll", "report") for s in seen))

# --- Сброс привязки ---
dl.forget_locally()
check("forget: not paired anymore", not dl.is_paired())
check("forget: removed from settings file", DEV_KEY not in pu.ENV_FILE.read_text(encoding="utf-8"))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
