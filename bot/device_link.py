"""
device_link.py — компьютер пользователя, подключённый к боту одноразовым кодом из Telegram (Р-16).

У такого агента нет BOT_TOKEN, AGENT_SECRET и адреса очереди kvdb. Он знает только свой device_id и ключ
устройства: задачи забирает у облака подписанным запросом, сообщения в Telegram отправляет через облако,
а облако пересылает их только в чат владельца этого компьютера.
"""
import hashlib
import hmac
import json
import os
import platform
import socket
import time
from typing import Any, Dict, List, Optional

import httpx

from bot.platform_utils import ENV_FILE

CLOUD_URL = os.getenv("CLOUD_URL", "https://second-brain-cloud-eight.vercel.app").strip().rstrip("/")
DEVICE_ID = os.getenv("DEVICE_ID", "").strip()
DEVICE_KEY = os.getenv("DEVICE_KEY", "").strip()
DEVICE_EVENTS_URL = os.getenv("DEVICE_EVENTS_URL", "").strip()
DEVICE_NAME = os.getenv("DEVICE_NAME", "").strip()
POLL_INTERVAL_SEC = 30
# Задача ждёт в очереди, пока компьютер выключен, — как и у агента владельца
TASK_MAX_AGE_SEC = 24 * 3600

_SETTING_KEYS = ("DEVICE_ID", "DEVICE_KEY", "DEVICE_EVENTS_URL", "DEVICE_NAME")

PAIR_ERRORS = {
    "bad_code": "Неверный или просроченный код. Получите новый код в боте: «Подключить компьютер» — «Получить код».",
    "too_many_attempts": "Слишком много неверных попыток. Подождите 10 минут и получите новый код.",
    "too_many_devices": "К вашему аккаунту уже подключено 3 компьютера. Отключите лишний в боте: «Подключить компьютер» — «Забыть».",
    "pairing_disabled": "Подключение компьютеров сейчас недоступно. Попробуйте позже.",
}


class DeviceRevoked(Exception):
    """Компьютер отключён в боте («Забыть»): облако больше не принимает его ключ."""


class PairError(Exception):
    pass


def is_paired() -> bool:
    return bool(DEVICE_ID and DEVICE_KEY)


def default_device_name() -> str:
    return (socket.gethostname() or "Компьютер")[:40]


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_headers(endpoint: str, body: bytes, ts: Optional[int] = None) -> Dict[str, str]:
    """Подпись запроса к облаку: адрес, устройство, время и тело. Перехваченный запрос нельзя отправить на другой адрес."""
    ts_s = str(int(ts if ts is not None else time.time()))
    msg = f"{endpoint}\n{DEVICE_ID}\n{ts_s}\n".encode("utf-8") + body
    sig = hmac.new(DEVICE_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"x-device-id": DEVICE_ID, "x-device-ts": ts_s, "x-device-sig": sig, "content-type": "application/json"}


def task_signature_ok(task: dict) -> bool:
    """Задача подписана ключом именно этого компьютера и не старше суток."""
    sig = task.get("sig")
    signed_at = task.get("signed_at")
    if not is_paired() or not isinstance(sig, str) or not isinstance(signed_at, int):
        return False
    if task.get("device_id") != DEVICE_ID:
        return False
    body = {k: v for k, v in task.items() if k != "sig"}
    expected = hmac.new(DEVICE_KEY.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    age = time.time() - signed_at
    return -300 <= age <= TASK_MAX_AGE_SEC


async def poll(client: httpx.AsyncClient, info: Dict[str, Any]) -> List[dict]:
    """Забирает задачи из своей очереди в облаке. Этот же запрос — сигнал «компьютер в сети»."""
    body = json.dumps(info, ensure_ascii=False).encode("utf-8")
    r = await client.post(f"{CLOUD_URL}/api/agent/poll", content=body, headers=request_headers("poll", body))
    if r.status_code == 401:
        error = ""
        try:
            error = r.json().get("error", "")
        except Exception:
            pass
        if error == "revoked":
            raise DeviceRevoked()
        raise RuntimeError(f"облако отклонило запрос: {error or r.status_code}")
    r.raise_for_status()
    tasks = r.json().get("tasks") or []
    return [t for t in tasks if isinstance(t, dict)]


async def report(client: httpx.AsyncClient, method: str, payload: Dict[str, Any]) -> httpx.Response:
    """Вызов Telegram Bot API через облако. Ответ — как от Telegram (тот же код и JSON)."""
    body = json.dumps({"method": method, "payload": payload}, ensure_ascii=False).encode("utf-8")
    return await client.post(f"{CLOUD_URL}/api/agent/report", content=body, headers=request_headers("report", body))


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_settings(values: Dict[str, Optional[str]]) -> None:
    """Обновляет ключи в agent.env, не трогая остальные строки. None — удалить ключ."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            done.add(key)
            if values[key] is not None:
                out.append(f"{key}={_quote(values[key])}")
            continue
        out.append(line)
    out += [f"{k}={_quote(v)}" for k, v in values.items() if k not in done and v is not None]
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def _apply(values: Dict[str, Optional[str]]) -> None:
    for key in _SETTING_KEYS:
        val = values.get(key) or ""
        globals()[key] = val
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)


def pair(code: str, name: str = "") -> Dict[str, Any]:
    """Обменивает код из бота на ключ устройства и сохраняет его в настройках агента."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        raise PairError("Код состоит из 6 цифр.")
    name = (name or "").strip()[:40] or default_device_name()
    try:
        r = httpx.post(
            f"{CLOUD_URL}/api/agent/pair",
            json={"code": code, "name": name, "os": f"{platform.system()} {platform.release()}".strip()},
            timeout=20.0,
        )
    except httpx.HTTPError:
        raise PairError("Нет связи с сервером бота. Проверьте интернет и попробуйте ещё раз.")
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code != 200 or not data.get("device_id") or not data.get("device_key"):
        raise PairError(PAIR_ERRORS.get(data.get("error", ""), f"Сервер бота ответил ошибкой ({r.status_code}). Попробуйте позже."))
    values = {
        "DEVICE_ID": data["device_id"],
        "DEVICE_KEY": data["device_key"],
        "DEVICE_EVENTS_URL": data.get("events_url", ""),
        "DEVICE_NAME": data.get("name") or name,
    }
    save_settings(values)
    _apply(values)
    return data


def forget_locally() -> None:
    """Сбрасывает привязку на этом компьютере (после «Забыть» в боте или по команде --unpair)."""
    save_settings({k: None for k in _SETTING_KEYS})
    _apply({})
