import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from bot.config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

MODEL_TIMEOUTS = [
    (GEMINI_MODEL, 5.0),
    ("gemini-2.5-flash", 5.0),
    ("gemini-2.0-flash", 3.0)
]

_genai_client = None

def get_genai_client():
    global _genai_client
    if _genai_client is None and GEMINI_API_KEY:
        try:
            from google import genai
            _genai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            logger.error(f"Не удалось инициализировать Google GenAI клиент: {e}")
            _genai_client = None
    return _genai_client

async def call_gemini_with_fallback(prompt: str) -> Optional[str]:
    client = get_genai_client()
    if not client:
        return None

    for model_name, timeout_sec in MODEL_TIMEOUTS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                ),
                timeout=timeout_sec
            )
            if response and response.text:
                return response.text.strip()
        except asyncio.TimeoutError:
            logger.warning(f"Модель {model_name} превысила таймаут {timeout_sec} сек. Пробуем следующую...")
        except Exception as e:
            err_str = str(e)
            if "User location is not supported" in err_str:
                logger.warning("Google Gemini заблокирован в текущей геолокации (нужен VPN: US/UK/CH).")
                break
            logger.warning(f"Модель {model_name} вернула ошибку: {e}. Пробуем следующую...")

    return None

def extract_plain_description_from_post(text: str) -> str:
    """
    Извлекает понятное человеческое описание инструмента из текста пересланного поста.
    Удаляет ссылки, хештеги и служебные призывы каналов.
    """
    clean = re.sub(r'https?://\S+', '', text)
    clean = re.sub(r'(@[a-zA-Z0-9_]+|#[a-zA-Z0-9_\u0400-\u04FF]+)', '', clean)
    clean = re.sub(r'(?i)\b(забирайте|ссылка|скачать|подробнее|источник|репозиторий|github|гитхаб)\s*:?', '', clean)
    lines = [l.strip(' -*•\t') for l in clean.split('\n') if l.strip(' -*•\t')]
    meaningful = [l for l in lines if len(l) > 15]
    if meaningful:
        res = ' '.join(meaningful[:2]).strip()
        if len(res) > 250:
            res = res[:247] + '...'
        return res
    return ''

def extract_desc_from_readme(readme_text: Optional[str]) -> str:
    """
    Извлекает первое ключевое описание инструмента из текста README.md,
    пропуская HTML-теги, картинки, бейджи и заголовки.
    """
    if not readme_text:
        return ""
    lines = readme_text.split('\n')
    candidates = []
    for line in lines:
        l = line.strip()
        if not l or l.startswith(('<', '#', '![', '[![', '<!--', '---')):
            continue
        clean = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', l)
        clean = re.sub(r'[*_`]', '', clean).strip()
        if len(clean) > 20 and not clean.startswith(('http', 'Copyright', 'License', 'MIT', 'Apache')):
            candidates.append(clean)
            if len(candidates) >= 2:
                break
    if candidates:
        res = ' '.join(candidates)
        if len(res) > 250:
            res = res[:247] + '...'
        return res
    return ''

async def summarize_readme_purpose(readme_text: str, fallback_desc: str = "") -> str:
    """
    Генерирует краткое и простое объяснение назначения инструмента по README через Gemini
    с надежным оффлайн-fallback.
    """
    clean_snippet = extract_desc_from_readme(readme_text)
    prompt = f"""
На основе фрагмента документации проекта:
{clean_snippet or readme_text[:1200]}

Объясни в 1-2 кратких предложениях на русском языке: ЧТО ИМЕННО ДЕЛАЕТ этот инструмент и для чего он нужен.
Требования:
- Кратко и простым языком для обычного человека (без сложного жаргона).
- Строго без эмодзи.
"""
    res = await call_gemini_with_fallback(prompt)
    if res and len(res.strip()) > 15:
        return res.strip()
    return clean_snippet or fallback_desc or "Инструмент для решения прикладных задач."

def expand_text_with_dictionary(text: str, user_dict: Optional[Dict[str, str]] = None) -> str:
    """
    Подставляет расшифровки аббревиатур и сленга пользователя.
    Сортирует ключи по убыванию длины, чтобы фразы заменялись раньше отдельных слов.
    """
    if not text or not user_dict:
        return text

    result = text
    sorted_terms = sorted(user_dict.items(), key=lambda x: len(x[0]), reverse=True)
    for term, meaning in sorted_terms:
        clean_term = term.strip()
        if not clean_term:
            continue
        pattern = rf"(?i)(?<![\w\u0400-\u04FF]){re.escape(clean_term)}(?![\w\u0400-\u04FF])"
        result = re.sub(pattern, meaning, result)
    return result

def parse_event_and_reminder_time(text: str) -> Dict[str, Any]:
    """
    Определяет дату и время события, а также рассчитывает время напоминания.
    Правило:
    - Если указано конкретное время (например 'в 16', 'в 15:30', 'репет по физике в 16'):
      Рассчитывает время события (event_at).
      Напоминание (reminder_at) ставится СТРОГО за 15 минут до события.
      НО только если событие создано заранее (remind_dt > now).
    - Если указано относительное время ('через 30 минут', 'через 2 часа'):
      Ставит напоминание на указанный срок.
    """
    now = datetime.now()
    lower = text.lower().strip()

    # 1. Проверка относительного времени ("через 15 минут", "через 2 часа")
    rel_match = re.search(r'\bчерез\s+(\d+)\s*(мин(?:ут[а-я]*)?|ч(?:ас(?:[а-я]*)?)?)\b', lower)
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2)
        if unit.startswith('ч'):
            delta = timedelta(hours=val)
        else:
            delta = timedelta(minutes=val)
        rem_dt = now + delta
        return {
            "is_event": False,
            "event_at": None,
            "reminder_at": rem_dt.strftime("%Y-%m-%d %H:%M:00"),
            "advance_minutes": 0,
            "advance_possible": True
        }

    # 2. Определение дня (сегодня, завтра, послезавтра)
    target_date = now.date()
    is_explicit_tomorrow = False
    if "послезавтра" in lower:
        target_date = now.date() + timedelta(days=2)
    elif "завтра" in lower:
        target_date = now.date() + timedelta(days=1)
        is_explicit_tomorrow = True
    elif "сегодня" in lower:
        target_date = now.date()

    # 3. Поиск времени: "в 16", "в 16:30", "в 16.00", "в 16 30", "к 15:00", "16:00"
    hour = None
    minute = 0
    is_event = False

    m_full = re.search(r'\b(?:в|к)\s+(\d{1,2})[:\.\s](\d{2})\b', lower)
    if not m_full:
        m_full = re.search(r'\b(\d{1,2})[:\.](\d{2})\b', lower)

    if m_full:
        h = int(m_full.group(1))
        m = int(m_full.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            hour = h
            minute = m
            is_event = True
    else:
        m_hour = re.search(r'\b(?:в|к)\s+(\d{1,2})\b(?!\s*[:\.]\d)', lower)
        if m_hour:
            h = int(m_hour.group(1))
            if 0 <= h <= 23:
                hour = h
                minute = 0
                is_event = True
                if "вечера" in lower and 1 <= hour <= 11:
                    hour += 12
                elif "дня" in lower and 1 <= hour <= 5:
                    hour += 12

    if is_event and hour is not None:
        event_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute, second=0)

        # Если время уже прошло сегодня и не было указано 'сегодня'/'завтра' явно
        if event_dt <= now and not ("сегодня" in lower or is_explicit_tomorrow):
            event_dt += timedelta(days=1)

        event_at_str = event_dt.strftime("%Y-%m-%d %H:%M:00")
        advance_min = 15
        remind_dt = event_dt - timedelta(minutes=advance_min)

        advance_possible = (remind_dt > now)
        reminder_at_str = remind_dt.strftime("%Y-%m-%d %H:%M:00") if advance_possible else None

        return {
            "is_event": True,
            "event_at": event_at_str,
            "reminder_at": reminder_at_str,
            "advance_minutes": advance_min,
            "advance_possible": advance_possible
        }

    return {
        "is_event": False,
        "event_at": None,
        "reminder_at": None,
        "advance_minutes": 15,
        "advance_possible": False
    }

def clean_event_title(text: str) -> str:
    """Очищает текст события от временных конструкций, оставляя саму суть."""
    clean = re.sub(r'\b(?:в|к)\s+\d{1,2}(?:[:\.\s]\d{2})?\b', '', text, flags=re.IGNORECASE)
    clean = re.sub(r'\b\d{1,2}[:\.]\d{2}\b', '', clean)
    clean = re.sub(r'\b(сегодня|завтра|послезавтра|вечера|дня|утра|утром|вечером)\b', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\s+', ' ', clean).strip(' ,.-')
    if clean:
        return clean[0].upper() + clean[1:]
    return "Событие"

async def analyze_message(
    text: str,
    readme_context: Optional[str] = None,
    user_dictionary: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Интеллектуальный семантический анализ сообщения:
    1. Расшифровка персональных аббревиатур и сленга пользователя.
    2. Определение событий со временем и расчет напоминания строго за 15 минут.
    3. Понимание сути, назначения и пользы инструмента простым языком.
    4. Генерация поисковых запросов для GitHub API.
    5. Умный автономный fallback.
    """
    expanded_text = expand_text_with_dictionary(text, user_dictionary)
    time_info = parse_event_and_reminder_time(expanded_text)

    # Если обнаружено конкретное событие / занятие по расписанию
    if time_info["is_event"]:
        clean_title = clean_event_title(expanded_text)
        return {
            "is_meaningful": True,
            "is_installable": False,
            "item_type": "Событие",
            "name": clean_title,
            "matched_by_description": False,
            "what_it_does": clean_title,
            "relevance": "Высокая (расписание)",
            "relevance_reason": "Личное занятие / событие по расписанию.",
            "search_queries": [],
            "repo_url": None,
            "event_at": time_info["event_at"],
            "reminder_at": time_info["reminder_at"],
            "advance_minutes": time_info["advance_minutes"],
            "advance_possible": time_info["advance_possible"]
        }

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    readme_part = ""
    if readme_context:
        clean_rm = extract_desc_from_readme(readme_context) or readme_context[:1000]
        readme_part = f"\nФРАГМЕНТ ИЗ README.MD РЕПОЗИТОРИЯ:\n{clean_rm}\n"

    user_dict_part = ""
    if user_dictionary:
        dict_lines = "\n".join(f"- {k}: {v}" for k, v in user_dictionary.items())
        user_dict_part = f"\nСЛОВАРЬ СОКРАЩЕНИЙ ПОЛЬЗОВАТЕЛЯ:\n{dict_lines}\nЕсли во входном тексте есть эти сокращения, расшифровывай их!\n"

    prompt = f"""
Ты — персональный аналитический ИИ-ассистент разработчика («второй мозг»).
Текущее время: {now_str}.
ВАЖНОЕ ПРАВИЛО: НЕ ИСПОЛЬЗУЙ НИКАКИХ ЭМОДЗИ. Текст должен быть строгим.

Пользователь отправляет описание, функционал, пересланный пост из Telegram или ссылку на инструмент/скилл/MCP/софт.

ВХОДНОЙ ТЕКСТ ПОЛЬЗОВАТЕЛЯ:
"{expanded_text}"
{readme_part}
{user_dict_part}
Твоя задача:
1. Оценить смысл: is_meaningful = false, если это чистый случайный шум (например: 'ывапрол', '123123').
2. Понять, какой РЕАЛЬНЫЙ популярный open-source проект, MCP-сервер, скилл или утилиту имеет в виду пользователь.
3. Назвать КАНОНИЧЕСКОЕ имя настоящего инструмента (canonical_name).
4. Сформировать список из 2-3 лучших поисковых запросов для GitHub API (search_queries) на английском.
5. ЧЕТКО И ПРОСТЫМ ЧЕЛОВЕЧЕСКИМ ЯЗЫКОМ объяснить в 1-2 предложениях: ЧТО ИМЕННО ДЕЛАЕТ этот инструмент и в чем польза (what_it_does):
   - ИСТОЧНИК 1 (ПРИОРИТЕТ): Если в тексте сообщения или пересланном посте уже описана суть инструмента — обязательно используй эту информацию из поста!
   - ИСТОЧНИК 2: Если информации в посте мало, но есть README — объясни пользу простыми словами по README.
   - СТРОГО ЗАПРЕЩЕНО писать сухие отписки вроде "Open-source проект на GitHub" или "Инструмент для интеграции". Объясни конкретный функционал.
6. ОЦЕНИТЬ АКТУАЛЬНОСТЬ (relevance) — СТРОГО КРАТКО (2-4 слова):
   - "Высокая (нужно для AI/кодинга)"
   - "Средняя (по необходимости)"
   - "Низкая (косметика/обои)"
   - relevance_reason: краткое пояснение в 1 коротком предложении.

Верни строго JSON объект следующей структуры:
{{
  "is_meaningful": true или false,
  "is_installable": true или false (true если это софт, MCP-сервер, скилл, расширение, библиотека или репозиторий),
  "item_type": "один из вариантов: MCP-сервер, Скилл / Расширение, Проект / Репозиторий, Задача, Идея, Ссылка, Заметка",
  "canonical_name": "Точное официальное название реального инструмента",
  "matched_by_description": true или false,
  "what_it_does": "Краткое и простое объяснение в 1-2 предложениях: ЧТО ДЕЛАЕТ и для чего нужен",
  "relevance": "Высокая (нужно для AI/кодинга)" или "Средняя (по необходимости)" или "Низкая (косметика/обои)",
  "relevance_reason": "Краткое обоснование полезности",
  "search_queries": ["первый точный запрос для GitHub", "второй альтернативный запрос"],
  "repo_url": "прямой URL если он есть в тексте" или null,
  "reminder_at": "YYYY-MM-DD HH:MM:SS" если есть просьба напомнить или null
}}

Верни ТОЛЬКО валидный JSON без markdown-разметки (без ```json ... ```).
"""

    resp_text = await call_gemini_with_fallback(prompt)
    if not resp_text:
        return _fallback_analyze(text, readme_context, user_dictionary)

    try:
        if resp_text.startswith("```"):
            resp_text = re.sub(r"^```(?:json)?\n", "", resp_text)
            resp_text = re.sub(r"\n```$", "", resp_text)

        data = json.loads(resp_text)
        rem_at = time_info.get("reminder_at") or data.get("reminder_at")
        return {
            "is_meaningful": bool(data.get("is_meaningful", True)),
            "is_installable": bool(data.get("is_installable", False)),
            "item_type": data.get("item_type", "Заметка"),
            "name": data.get("canonical_name") or expanded_text[:30],
            "matched_by_description": bool(data.get("matched_by_description", False)),
            "what_it_does": data.get("what_it_does", ""),
            "relevance": data.get("relevance", "Средняя (по необходимости)"),
            "relevance_reason": data.get("relevance_reason", ""),
            "search_queries": data.get("search_queries") or [expanded_text],
            "repo_url": data.get("repo_url"),
            "event_at": time_info.get("event_at"),
            "reminder_at": rem_at,
            "advance_minutes": time_info.get("advance_minutes", 15),
            "advance_possible": time_info.get("advance_possible", False)
        }
    except Exception as e:
        logger.warning(f"Ошибка парсинга JSON: {e}. Используем fallback.")
        return _fallback_analyze(text, readme_context, user_dictionary)

def build_offline_detailed_explanation(name: str, what_it_does: str, content: str, relevance: str = "", readme_text: Optional[str] = None) -> str:
    """Формирует подробное объяснение сервиса без внешнего API."""
    import html
    parts = [
        f"<b>Подробный разбор:</b> {html.escape(name)}",
        f"<b>Что это такое:</b>\n{html.escape(what_it_does or 'Утилита для решения специализированных прикладных задач на macOS.')}"
    ]

    clean_content = re.sub(r"https?://\S+", "", content)
    content_paragraphs = [p.strip() for p in clean_content.split("\n\n") if len(p.strip()) > 25]

    if len(content_paragraphs) > 1:
        scenarios = "\n".join(f"- {html.escape(p)}" for p in content_paragraphs[1:3])
        parts.append(f"<b>Сценарии использования:</b>\n{scenarios}")
    elif readme_text:
        readme_lines = [l.strip("-* \t") for l in readme_text.split("\n") if len(l.strip("-* \t")) > 25 and not l.startswith(("#", "<", "!", "[!["))]
        if readme_lines:
            scenarios = "\n".join(f"- {html.escape(l)}" for l in readme_lines[:3])
            parts.append(f"<b>Возможности из документации:</b>\n{scenarios}")

    if relevance:
        parts.append(f"<b>Актуальность для Mac:</b>\n{html.escape(relevance)}")

    return "\n\n".join(parts)

async def generate_detailed_explanation(name: str, what_it_does: str, content: str, relevance: str = "", readme_text: Optional[str] = None) -> str:
    """Генерирует развернутое, но простое объяснение проекта для кнопки 'Подробнее'."""
    readme_snippet = extract_desc_from_readme(readme_text) if readme_text else ""
    prompt = f"""
Ты — персональный аналитический ассистент разработчика.
Пользователь нажал кнопку «Подробнее», чтобы понять, про что этот сервис и для чего он нужен.

Инструмент: {name}
Краткая суть: {what_it_does}
Текст сообщения/поста:
{content[:1500]}
{f"Фрагмент README: {readme_snippet}" if readme_snippet else ""}

Ответь строго по следующему шаблону, разделяя каждый блок пустой строкой:

<b>Что это такое:</b>
(Простыми словами объясни суть сервиса)

<b>Сценарии использования:</b>
- (Первый пример использования)
- (Второй пример использования)

<b>Актуальность для Mac:</b>
(Кому подойдет и стоит ли ставить на Mac, с учетом: {relevance})

Требования к оформлению:
- Между темами (блоками) обязательно делай отступ в одну пустую строку, чтобы текст не слипался.
- Заголовки тем выделяй тегами <b>...</b>
- Строго без эмодзи.
- Понятным языком без избыточного жаргона.
"""
    res = await call_gemini_with_fallback(prompt)
    if res and len(res.strip()) > 30:
        return res.strip()
    return build_offline_detailed_explanation(name, what_it_does, content, relevance, readme_text)

def _fallback_analyze(
    text: str,
    readme_context: Optional[str] = None,
    user_dictionary: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    Умный оффлайн-анализатор на случай недоступности внешнего API.
    Понимает персональные сокращения, события с расчетом 15 минут, MCP-серверы памяти, скиллы, GitHub ссылки.
    """
    expanded_text = expand_text_with_dictionary(text, user_dictionary)
    time_info = parse_event_and_reminder_time(expanded_text)

    if time_info["is_event"]:
        clean_title = clean_event_title(expanded_text)
        return {
            "is_meaningful": True,
            "is_installable": False,
            "item_type": "Событие",
            "name": clean_title,
            "matched_by_description": False,
            "what_it_does": clean_title,
            "relevance": "Высокая (расписание)",
            "relevance_reason": "Личное занятие / событие по расписанию.",
            "search_queries": [],
            "repo_url": None,
            "event_at": time_info["event_at"],
            "reminder_at": time_info["reminder_at"],
            "advance_minutes": time_info["advance_minutes"],
            "advance_possible": time_info["advance_possible"]
        }

    post_desc = extract_plain_description_from_post(expanded_text)
    readme_desc = extract_desc_from_readme(readme_context) if readme_context else ""
    best_extracted_desc = post_desc or readme_desc

    lower = expanded_text.lower().strip()
    if len(lower) < 3 or lower in ["привет", "хай", "test", "тест", "ку"]:
        return {
            "is_meaningful": False,
            "is_installable": False,
            "item_type": "Заметка",
            "name": None,
            "matched_by_description": False,
            "what_it_does": "Слишком короткое или неинформативное сообщение",
            "relevance": "Низкая",
            "relevance_reason": "Неинформативный текст.",
            "search_queries": [],
            "repo_url": None,
            "event_at": None,
            "reminder_at": None
        }

    # Оценка актуальности по ключевым словам
    relevance = "Средняя (по необходимости)"
    relevance_reason = "Прикладная утилита для macOS."
    if any(w in lower for w in ["обои", "wallpaper", "spreadpaper", "скринсейвер", "фон", "тема"]):
        relevance = "Низкая (косметика/обои)"
        relevance_reason = "Косметическая утилита, не влияет на продуктивность."
    elif any(w in lower for w in ["claude", "mcp", "agent", "агент", "память", "memory", "тест", "test", "tdd", "review", "кодинг", "devtools"]):
        relevance = "Высокая (нужно для AI/кодинга)"
        relevance_reason = "Инструмент для AI-разработки и автоматизации в терминале."

    # Проверка на MCP сервер памяти / сжатия контекста
    if "mcp" in lower and any(w in lower for w in ["memory", "memori", "condense", "codebase", "память", "контекст"]):
        return {
            "is_meaningful": True,
            "is_installable": True,
            "item_type": "MCP-сервер",
            "name": "codebase-memory-mcp",
            "matched_by_description": True,
            "what_it_does": best_extracted_desc or "Высокопроизводительный MCP-сервер для индексации кодовой базы и организации постоянной памяти LLM, снижающий расход токенов до 99%.",
            "relevance": "Высокая (рекомендуется)",
            "relevance_reason": "Критично для работы с LLM: снижает расход токенов и держит контекст между сессиями.",
            "search_queries": ["codebase-memory-mcp", "mcp codebase memory", "mcp memory context"],
            "repo_url": "https://github.com/DeusData/codebase-memory-mcp",
            "reminder_at": None
        }

    # Общий MCP сервер
    if "mcp" in lower:
        clean_name = re.sub(r"\b(скачать|установить|сервер|mcp)\b", "", lower).strip() or "mcp-server"
        desc = best_extracted_desc or f"MCP-сервер для интеграции внешних инструментов и контекста с языковыми моделями ({clean_name})."
        return {
            "is_meaningful": True,
            "is_installable": True,
            "item_type": "MCP-сервер",
            "name": clean_name,
            "matched_by_description": False,
            "what_it_does": desc,
            "relevance": relevance,
            "relevance_reason": relevance_reason,
            "search_queries": [f"{clean_name} mcp", f"mcp {clean_name}"],
            "repo_url": None,
            "reminder_at": None
        }

    # Скилл или расширение
    if "скилл" in lower or "skill" in lower or "claude" in lower:
        clean_name = re.sub(r"\b(скилл|skill|установить|создать|claude)\b", "", lower).strip() or "skill"
        desc = best_extracted_desc or f"Скилл для автоматизации задач и выполнения сценариев ассистентом ({clean_name})."
        return {
            "is_meaningful": True,
            "is_installable": True,
            "item_type": "Скилл / Расширение",
            "name": clean_name,
            "matched_by_description": False,
            "what_it_does": desc,
            "relevance": relevance,
            "relevance_reason": relevance_reason,
            "search_queries": [f"skill {clean_name}", clean_name],
            "repo_url": None,
            "reminder_at": None
        }

    # Репозиторий или GitHub ссылка
    if "github.com/" in text:
        match = re.search(r"https?://github\.com/([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)", text)
        repo_path = match.group(1) if match else text
        repo_name = repo_path.split("/")[-1]
        desc = best_extracted_desc or f"Утилита {repo_name} для решения прикладных задач."
        return {
            "is_meaningful": True,
            "is_installable": True,
            "item_type": "Проект / Репозиторий",
            "name": repo_name,
            "matched_by_description": False,
            "what_it_does": desc,
            "relevance": relevance,
            "relevance_reason": relevance_reason,
            "search_queries": [repo_name],
            "repo_url": f"https://github.com/{repo_path}",
            "reminder_at": None
        }

    # Задача
    if any(w in lower for w in ["сделать", "задача", "todo", "купить", "позвонить", "надо", "проверить"]) or time_info.get("reminder_at"):
        return {
            "is_meaningful": True,
            "is_installable": False,
            "item_type": "Задача",
            "name": None,
            "matched_by_description": False,
            "what_it_does": expanded_text,
            "relevance": "Средняя",
            "relevance_reason": "Личная задача.",
            "search_queries": [],
            "repo_url": None,
            "event_at": None,
            "reminder_at": time_info.get("reminder_at")
        }

    # Обычная заметка
    return {
        "is_meaningful": True,
        "is_installable": False,
        "item_type": "Заметка",
        "name": None,
        "matched_by_description": False,
        "what_it_does": expanded_text,
        "relevance": "Средняя",
        "relevance_reason": "Заметка для контекста.",
        "search_queries": [],
        "repo_url": None,
        "event_at": None,
        "reminder_at": time_info.get("reminder_at")
    }

async def generate_weekly_digest(notes: List[Dict[str, Any]]) -> str:
    """Генерирует еженедельный дайджест без эмодзи."""
    if not notes:
        return "За прошедшую неделю нет сохраненных заметок."

    formatted_notes = []
    for idx, n in enumerate(notes, 1):
        dt = n.get("created_at", "")
        cat = n.get("category", "Заметка")
        content = n.get("what_it_does") or n.get("content", "")
        proj = f" [Проект: {n.get('project_name')}]" if n.get('project_name') else ""
        formatted_notes.append(f"{idx}. [{dt}] ({cat}){proj}: {content}")
    notes_blob = "\n".join(formatted_notes)

    prompt = f"""
Ты — персональный аналитический ассистент.
Ниже приведен список заметок пользователя за последнюю неделю:

{notes_blob}

Составь строго структурированную еженедельную сводку на русском языке.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать любые эмодзи.
Используй чистую разметку Markdown (жирный шрифт, списки).

Структура:
Еженедельный дайджест
Сводка за последние 7 дней

1. Главные события недели
2. Идеи и инсайты
3. Инструменты, MCP-серверы и скиллы
(для каждого укажи: что делает и статус установки)
4. Выполненные задачи и статус
5. Приоритеты на следующую неделю

Текст должен быть лаконичным, информативным и без воды.
"""
    digest_text = await call_gemini_with_fallback(prompt)
    if digest_text:
        return digest_text
    return _fallback_digest(notes)

def _fallback_digest(notes: List[Dict[str, Any]]) -> str:
    by_category: Dict[str, List[str]] = {}
    for n in notes:
        cat = n.get("category", "Заметка")
        desc = n.get("what_it_does") or n.get("content", "")
        by_category.setdefault(cat, []).append(f"- {desc} ({n.get('created_at')})")

    lines = ["Еженедельный дайджест заметок\n"]
    for cat, items in by_category.items():
        lines.append(f"{cat} ({len(items)}):")
        lines.extend(items)
        lines.append("")

    return "\n".join(lines)
