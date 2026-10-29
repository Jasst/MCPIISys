import asyncio
import aiohttp
import logging
import time
from typing import List, Dict, Optional
import contextvars
from GCN.config_ai import LM_STUDIO_URL, LM_STUDIO_API_KEY, LM_STUDIO_TIMEOUT, LM_STUDIO_STREAM_TIMEOUT, DEFAULT_MAX_TOKENS, LM_STUDIO_MODEL

# Модель, выбранная пользователем в чате. Ставится на время запроса (см. ai_assistant),
# вне запроса — LM_STUDIO_MODEL.
llm_model_var: contextvars.ContextVar = contextvars.ContextVar("llm_model", default=None)


def _model_name() -> str:
    return llm_model_var.get() or LM_STUDIO_MODEL


def _api_base_url() -> str:
    """http://host:port/v1 из LM_STUDIO_URL (.../v1/chat/completions)."""
    return LM_STUDIO_URL.replace("/chat/completions", "")


import json

logger = logging.getLogger(__name__)

try:
    from GCN.config_ai import LLM_NO_THINK_SUFFIX
except ImportError:  # старый config_ai без константы — поведение как раньше
    LLM_NO_THINK_SUFFIX = ""


def _no_think(messages: List[Dict]) -> List[Dict]:
    """Добавляет soft-switch отключения thinking к последнему текстовому user-сообщению
    (копия, исходный список не мутируется). Мультимодальные content-списки не трогаем."""
    if not LLM_NO_THINK_SUFFIX:
        return messages
    msgs = [dict(m) for m in messages]
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            if not m["content"].rstrip().endswith(LLM_NO_THINK_SUFFIX.strip()):
                m["content"] += LLM_NO_THINK_SUFFIX
            break
    return msgs

# Переиспользуемая сессия с пулом соединений (keep-alive) к локальному LM
# Studio вместо нового TCP-хендшейка на каждый вызов — это самый горячий
# путь в проекте (каждое сообщение чата, каждый tool-call реранкинг,
# verify_response). Закрывается через close_session() при остановке процесса.
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()

# Статусы, при которых имеет смысл повторить попытку (временная перегрузка/
# рейт-лимит бэкенда), а не только 5xx.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=30)
                )
    return _session


async def close_session() -> None:
    """Вызывать при штатном завершении процесса, чтобы не оставлять открытый пул соединений."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def call_llm_raw(
    messages: List[Dict[str, str]],
    temp: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tools: Optional[List[Dict]] = None,
    retries: int = 3,
    think: bool = False,
) -> Dict:
    """
    Возвращает сырой объект message от LLM целиком (не только content), чтобы
    вызывающий код (GCN.tool_router) мог прочитать tool_calls при нативном
    function calling — тем же механизмом, что использует внешний MCP-клиент.
    При исчерпании retry возвращает {"_error": "..."} вместо {}.
    """
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"}
    payload = {
        "model": _model_name(),
        "messages": messages if think else _no_think(messages),
        "temperature": temp,
        "max_tokens": max_tokens
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    last_error = None
    for attempt in range(retries):
        try:
            session = await _get_session()
            async with session.post(LM_STUDIO_URL, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=LM_STUDIO_TIMEOUT)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("choices", [{}])[0].get("message", {}) or {}

                error_text = await resp.text()
                logger.error(f"LLM error {resp.status}: {error_text[:200]}")
                # tools может быть не поддержан бэкендом (400) — пробуем без tools один раз
                if tools and resp.status == 400 and attempt == 0:
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    continue
                if resp.status in _RETRYABLE_STATUSES and attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                last_error = f"LLM returned status {resp.status}: {error_text[:100]}"
                break
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            last_error = str(e)
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            break
    # Все retry исчерпаны — возвращаем ошибку явно
    return {"_error": last_error or "LLM call failed after all retries"}


async def call_llm(
    messages: List[Dict[str, str]],
    temp: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    retries: int = 3,
    include_reasoning: bool = False,
) -> str:
    """Универсальный вызов локальной LLM (LM Studio) — только текст ответа.

    include_reasoning=True — если модель прислала reasoning_content (нативный
    thinking Qwen3/DeepSeek-R1 и т.п.), обернуть его в <thought>...</thought>
    и склеить с content. Используется ТОЛЬКО в главном ответе чата; для
    служебных вызовов (планирование, верификация, JSON-парсинг) — False,
    чтобы reasoning не путал парсеры.
    """
    msg = await call_llm_raw(messages, temp=temp, max_tokens=max_tokens,
                             tools=None, retries=retries, think=include_reasoning)
    content = msg.get("content", "") or ""
    if not include_reasoning:
        # служебный вызов: рассуждения, которые модель вывела в content, не должны
        # попадать в гипотезы, JSON-парсеры и поисковые запросы
        from GCN.llm_text import strip_thinking
        cleaned = strip_thinking(content)
        if cleaned != content.strip():
            logger.info("call_llm: вырезаны рассуждения из ответа (%d -> %d символов)",
                        len(content), len(cleaned))
        return cleaned

    reasoning = (
        msg.get("reasoning_content")
        or msg.get("reasoning")
        or ""
    ).strip()

    if not reasoning:
        return content
    if not content:
        # Модель думала, но не уложилась в лимит токенов.
        return f"<thought>{reasoning}</thought>"
    return f"<thought>{reasoning}</thought>\n\n{content}"


async def _call_llm_stream_once(
    messages: List[Dict[str, str]],
    temp: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stop: Optional[List[str]] = None,
    include_reasoning: bool = True,
):
    """Потоковый вызов LLM (LM Studio).

    Yield-ит dict-события, чтобы разделить reasoning и content в потоке:
      {"kind": "reasoning", "text": "..."} — reasoning-токен (нативный thinking)
      {"kind": "content",   "text": "..."} — токен финального ответа
      {"kind": "truncated", "reason": "no_done"|"length"} — поток закончился
                                              без [DONE] или по лимиту токенов.

    include_reasoning=False — reasoning-события не эмитятся (модель может
    тратить токены на thinking, но UI его не увидит).

    Обратная несовместимость: раньше функция yield-ила строки, теперь dict.
    Единственный потребитель — _stream_response_worker в ai_assistant.py.
    """
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"}
    payload = {
        "model": _model_name(),
        # Ход без режима рассуждений — модель не должна «думать» скрыто (экономия TTFT).
        "messages": messages if include_reasoning else _no_think(messages),
        "temperature": temp,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if stop:
        payload["stop"] = stop
    timeout = aiohttp.ClientTimeout(total=LM_STUDIO_STREAM_TIMEOUT)
    stream_did_complete = False
    finish_reason = None
    try:
        session = await _get_session()
        async with session.post(LM_STUDIO_URL, json=payload,
                                headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"Stream error {resp.status}: {error_text[:200]}")
                yield {"kind": "content", "text": "[Ошибка LLM]"}
                return
            async for line in resp.content:
                if not line:
                    break
                line = line.decode('utf-8').strip()
                if not line or not line.startswith('data: '):
                    continue
                data = line[6:]
                if data == '[DONE]':
                    stream_did_complete = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = chunk.get('choices', [{}])[0]
                delta = choice.get('delta', {})

                if 'finish_reason' in choice:
                    finish_reason = choice['finish_reason']

                # Reasoning-токен — эмитим сразу, без накопления.
                if include_reasoning:
                    r_chunk = (
                        delta.get('reasoning_content')
                        or delta.get('reasoning')
                        or ''
                    )
                    if r_chunk:
                        yield {"kind": "reasoning", "text": r_chunk}

                # Content-токен — тоже сразу.
                content = delta.get('content', '')
                if content:
                    yield {"kind": "content", "text": content}

            if not stream_did_complete:
                logger.warning(
                    f"LLM stream ended without [DONE] marker "
                    f"(finish_reason={finish_reason})"
                )
                yield {"kind": "truncated", "reason": "no_done"}
                return
            if finish_reason == "length":
                logger.warning("LLM stream truncated by token limit (finish_reason=length)")
                yield {"kind": "truncated", "reason": "length"}
                return
            logger.debug(f"LLM stream completed with finish_reason: {finish_reason}")
    except asyncio.CancelledError:
        logger.debug("Stream cancelled")
        raise
    except Exception as e:
        logger.error(f"Stream error: {e}")
        yield {"kind": "content", "text": f"[Ошибка: {e}]"}


async def call_llm_stream(
    messages: List[Dict[str, str]],
    temp: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stop: Optional[List[str]] = None,
    include_reasoning: bool = True,
):
    """Потоковый вызов с защитой от зацикливания рассуждений.

    Если модель думает дольше REASONING_MAX_CHARS и ещё не начала ответ,
    поток прерывается и запрос повторяется без режима рассуждений.
    Так длинная самопроверка не съедает весь лимит токенов и не оставляет
    пользователя без ответа.
    """
    from GCN.config_ai import REASONING_MAX_CHARS

    if not include_reasoning or REASONING_MAX_CHARS <= 0:
        async for ev in _call_llm_stream_once(messages, temp, max_tokens, stop, include_reasoning):
            yield ev
        return

    reasoning_chars = 0
    content_started = False
    overflow = False
    gen = _call_llm_stream_once(messages, temp, max_tokens, stop, True)
    try:
        async for ev in gen:
            kind = ev.get("kind")
            if kind == "reasoning":
                reasoning_chars += len(ev.get("text", ""))
                if reasoning_chars > REASONING_MAX_CHARS and not content_started:
                    overflow = True
                    break
            elif kind == "content":
                content_started = True
            yield ev
    finally:
        await gen.aclose()

    if overflow:
        logger.warning(
            f"Рассуждения превысили {REASONING_MAX_CHARS} символов без ответа — повтор без рассуждений"
        )
        async for ev in _call_llm_stream_once(messages, temp, max_tokens, stop, False):
            yield ev


async def list_models() -> List[str]:
    """Идентификаторы моделей из LM Studio (/v1/models). Эмбеддинг-модели не нужны для чата."""
    url = LM_STUDIO_URL.replace("/chat/completions", "/models")
    headers = {"Authorization": f"Bearer {LM_STUDIO_API_KEY}"}
    session = await _get_session()
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"LM Studio /models: HTTP {resp.status}")
        data = await resp.json()
    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    return [i for i in ids if "embed" not in i.lower()]


# ===== Кэш списка моделей =====
# /ai/models опрашивается каждым клиентом при открытии страницы; список в
# LM Studio меняется редко, поэтому отдаём закешированное значение (TTL ниже),
# а фоновые операции с моделью (проверка загрузки, выгрузка) бьют в кассу.
_MODELS_CACHE_TTL_S = 60.0
_models_cache: Dict = {"ids": None, "ts": 0.0}
_models_cache_lock = asyncio.Lock()


async def list_models_cached(force: bool = False) -> List[str]:
    """Как list_models(), но с коротким кэшем. Ошибку гасит в пустой список —
    вызывающий код сам решает, что делать с недоступной LM Studio."""
    now = time.monotonic()
    if not force and _models_cache["ids"] is not None and now - _models_cache["ts"] < _MODELS_CACHE_TTL_S:
        return _models_cache["ids"]
    async with _models_cache_lock:
        # двойная проверка: пока ждали лок, кэш мог обновить сосед
        now = time.monotonic()
        if not force and _models_cache["ids"] is not None and now - _models_cache["ts"] < _MODELS_CACHE_TTL_S:
            return _models_cache["ids"]
        try:
            ids = await list_models()
        except Exception as e:
            logger.warning(f"list_models_cached: {e}")
            # устаревший кэш лучше пустого списка: UI не «сломается» на минуту
            return _models_cache["ids"] or []
        _models_cache["ids"] = ids
        _models_cache["ts"] = time.monotonic()
        return ids


def invalidate_models_cache() -> None:
    _models_cache["ids"] = None
    _models_cache["ts"] = 0.0


# ===== Управление загрузкой моделей в LM Studio (нативный API /api/v0) =====
# Выбор модели в чате должен не просто подставлять id в запрос, а реально
# выгружать прежнюю и загружать выбранную модель: иначе первая же отправка
# упирается в медленную «холодную» загрузкуWeight'ов посреди генерации.
_LMS_API_BASE = LM_STUDIO_URL.replace("/v1/chat/completions", "") + "/api/v0"
_LMS_HEADERS = {"Authorization": f"Bearer {LM_STUDIO_API_KEY}"}

# id модели, которая по нашему учёту сейчас в VRAM (None — неизвестно);
# timestamp последней успешной загрузки — для блокировки смены модели
# сразу после старта/финала генерации.
# ts_aware: результат актуален ровно столько, сколько живёт кэш состояния
# /ai/models (_loaded_state_ttl_s). Без отметки времени «учётный кэш»
# залипал навсегда: LM Studio перезапустили / модель выгрузили вручную —
# а сервер продолжал считать, что она в памяти, и UI показывал ложную
# метку «в памяти».
_loaded_model_id: Optional[str] = None
_loaded_model_ts: float = 0.0
_loaded_state_ttl_s: float = 10.0   # см. _MODELS_STATE_TTL_S в routes/ai_assistant
_load_ts: float = 0.0
# момент последней операции с весами (load/unload/reconcile) — по нему
# быстрая сверка состояния понимает, можно ли доверять учётному кэшу
_last_ops_ts: float = 0.0
# «Намерение пользователя» — какая модель выбрана в интерфейсе (перезапись при
# каждом выборе). Сервер использует её как ориентир после перезагрузки страницы
# или рестарта процесса: если LM Studio уже держит эту модель в VRAM — заново
# ничего НЕ загружается (см. reconcile_loaded_model / GET /ai/models).
_desired_model_id: Optional[str] = None
# глобальная блокировка: выгрузка/загрузка weights — эксклюзивная операция,
# во время неё нельзя запускать генерацию и вторую смену модели
model_ops_lock = asyncio.Lock()
# фоновая задача дозарядки модели после смены (отменяется при новой смене)
_preload_task: Optional[asyncio.Task] = None
# фоновая сверка «выбранная модель vs VRAM» (одна на процесс, не пересоздаётся)
_reconcile_task: Optional[asyncio.Task] = None


def set_desired_model(model_id: Optional[str]) -> None:
    """Запомнить выбор пользователя (для сверки после перезагрузки страницы)."""
    global _desired_model_id
    if model_id:
        _desired_model_id = model_id


def get_desired_model() -> Optional[str]:
    return _desired_model_id


def _remember_loaded(model_id: Optional[str]) -> None:
    """Положить/обновить учётный кэш «что в VRAM» со свежей отметкой времени."""
    global _loaded_model_id, _loaded_model_ts
    _loaded_model_id = model_id
    _loaded_model_ts = time.monotonic()


def loaded_model_cached() -> Optional[str]:
    """Учётный кэш состояния VRAM, если он ещё не протух (TTL = TTL /ai/models).

    Используется горячим путём /models/select: подтверждать выбор модели,
    которая по нашему учёту уже в памяти, можно БЕЗ опроса LM Studio — но
    только пока отметка времени свежая. Протухший кэш hot-путь игнорирует
    и честно идёт проверять VRAM, чтобы UI не выдавал ложвое «уже в памяти».
    """
    if _loaded_model_id and time.monotonic() - _loaded_model_ts < _loaded_state_ttl_s:
        return _loaded_model_id
    return None


_state_listeners = []


def on_model_state_changed(fn) -> None:
    """Регистрация колбэка инвалидации внешних кэшей состояния (см. /ai/models)."""
    _state_listeners.append(fn)


def _notify_state_changed() -> None:
    for fn in list(_state_listeners):
        try:
            fn()
        except Exception:
            pass


async def reconcile_loaded_model(target: Optional[str]) -> Dict:
    """Проверить, какая модель ФАКТИЧЕСКИ в VRAM, и загрузить target только
    если её там нет.

    Ключевой сценарий: пользователь перезагружает страницу — фронтенд снова
    вызывает /ai/models/select с сохранённой моделью. Раньше это каждый раз
    запускало unload+load (тяжёлая выгрузка/загрузка весов), даже когда модель
    уже висела в памяти. Здесь мы сначала спрашиваем LM Studio
    (перебор реальных id через GET /api/v0/models/<id>): если целевая модель
    уже загружена — возвращаем changed=False и НЕ трогаем VRAM. Идемпотентно.

    ВАЖНО: ensure_model_loaded под этим же model_ops_lock вызывать нельзя —
    asyncio.Lock не реентерабелен, будет взаимоблокировка. Поэтому логика
    unload+load продублирована здесь инлайн (лок уже удерживается).
    """
    global _loaded_model_id, _last_ops_ts
    target = target or LM_STUDIO_MODEL
    async with model_ops_lock:
        current = await get_loaded_model()
        _last_ops_ts = time.monotonic()
        if current == target:
            _remember_loaded(target)
            return {"ok": True, "loaded": target, "changed": False, "error": None}
        # ensure_model_loaded НЕ вызывать под этим локом — лок не реентерабелен.
        # Инлайн та же идемпотентная логика (лок уже удерживается): unload старой
        # + load новой модели.
        known_ids = set(await list_models_cached())
        try:
            if current and current != target and current in known_ids:
                await unload_model(current)
            await load_model(target)
            _remember_loaded(target)
            invalidate_models_cache()
            _notify_state_changed()
            logger.info(f"reconcile: модель переключена {current!r} -> {target!r}")
            return {"ok": True, "loaded": target, "changed": True, "error": None}
        except Exception as e:
            logger.error(f"reconcile '{target}': {e}")
            _remember_loaded(None)
            invalidate_models_cache()
            _notify_state_changed()
            return {"ok": False, "loaded": current, "changed": False, "error": str(e)}


async def _reconcile_worker(target: str) -> None:
    """Фоновая сверка: дождаться окончания генерации/операций с весами, затем
    проверить VRAM и загрузить target ТОЛЬКО если он реально отсутствует."""
    try:
        for _ in range(900):              # максимум ~15 минут ожидания покоя
            await asyncio.sleep(1.0)
            if model_ops_lock.locked():
                continue
            st = await get_generation_status()
            if not st.get("active"):
                break
        else:
            return
        # за время ожидания пользователь мог выбрать другую модель — берём свежее намерение
        fresh = _desired_model_id or target
        res = await reconcile_loaded_model(fresh)
        if res["ok"] and res["changed"]:
            logger.info(f"reconcile: модель {fresh!r} отсутствовала в VRAM — загружена")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"reconcile '{target}': {e}")


def schedule_reconcile(target: Optional[str]) -> None:
    """Одна фоновая задача сверки на весь процесс: если target уже в VRAM —
    ничего не делает; если нет (например, сервер рестартовал) — дозагрузит
    в момент покоя. Повторные вызовы для той же модели игнорируются."""
    global _reconcile_task
    target = target or LM_STUDIO_MODEL
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if (_reconcile_task and not _reconcile_task.done()
            and (_reconcile_task.get_name() or "").endswith(target)):
        return
    if _reconcile_task and not _reconcile_task.done():
        _reconcile_task.cancel()
    _reconcile_task = loop.create_task(_reconcile_worker(target),
                                       name=f"model-reconcile:{target}")


async def get_loaded_model() -> Optional[str]:
    """id модели, фактически стоящей в VRAM LM Studio (по данным самого LMS).

    ВАЖНО: при любой нестандартной ситуации (старая версия LMS без этого
    эндпоинта, прокси, отдающий HTML-страницу 404, таймаут) возвращаем
    УЧЁТНЫЙ КЭШ _loaded_model_id, а НЕ выдуманные значения вроде 'current' —
    иначе это «id» уходит в unload/load и LM Studio отвечает
    `Model with identifier 'current' not found`.

    ИСТОРИЯ БАГА: раньше здесь использовался путь `/models/current`, которого
    в REST API LM Studio (/api/v0) НЕ СУЩЕСТВУЕТ. Логика внутри LMS разобрала
    сегмент "current" как идентификатор модели и писала в свой лог ERROR
    `Model with identifier 'current' not found` при КАЖДОМ запросе списка
    моделей. Сейчас состояние VRAM определяется методом перебора реальных id:
    `GET /api/v0/models/<id>` — для загруженной модели LMS отвечает 200,
    для выгруженной — 404. Никаких выдуманных id не отправляем.

    ОПТИМИЗАЦИЯ: если по нашему учёту модель недавно (в пределах TTL) точно
    была в VRAM, проверяем ТОЛЬКО её — один запрос вместо волны probe'ов
    по всему списку. Подтверждение «модель X уже в памяти» при клике
    по X становится мгновенным и дешёвым для LM Studio.
    """
    global _loaded_model_id
    try:
        ids = await list_models_cached()
    except Exception:
        ids = []
    if not ids:
        return _loaded_model_id

    session = await _get_session()
    sem = asyncio.Semaphore(6)  # не заваливать локальный сервер одновременными запросами

    async def _probe(model_id: str) -> Optional[str]:
        url = f"{_LMS_API_BASE}/models/{model_id}"
        async with sem:
            try:
                async with session.get(url, headers=_LMS_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        return model_id
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        return None

    # Быстрая сверка: ожидаем увидеть в VRAM ту же модель, что и в прошлый раз.
    # Актуально только пока учётный кэш не протух (см. loaded_model_cached).
    expected = loaded_model_cached()
    fast = False
    if expected and expected in ids:
        # Если с момента последней операции с весами (наша загрузка/выгрузка)
        # шла генерация, LM Studio могла сама выгрузить/подменить модель —
        # быстрой сверке не доверяем, идём полным перебором.
        last_touch = max(_load_ts, _last_ops_ts)
        st = await get_generation_status()
        fast = not st.get("active") and (time.monotonic() - last_touch) < 300.0
    if fast:
        try:
            if await _probe(expected):
                _remember_loaded(expected)
                return expected
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"get_loaded_model(fast {expected!r}): {e}")
        # Ожидаемая модель НЕ подтверждена → возможно, LM Studio сама
        # переключила/выгрузила веса. Полная проверка ниже; чтобы фоновая
        # дозарядка не грузила старое намерение, смещаем «намерение» на
        # реальное состояние (если оно найдётся) — иначе reconcile будет
        # бесполезно пытаться вернуть модель, которую пользователь уже
        # перестал выбирать в другом окне.

    try:
        results = await asyncio.gather(*(_probe(i) for i in ids))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"get_loaded_model: {e} (использую учётный кэш)")
        return _loaded_model_id
    loaded = next((r for r in results if r), None)
    if loaded:
        _remember_loaded(loaded)
        if expected and loaded != expected:
            set_desired_model(loaded)
            logger.info(f"VRAM: модель сменилась вне нашего контроля "
                        f"{expected!r} -> {loaded!r}, следим за новой")
        return loaded
    # Ни одна модель не отвечает 200 → в VRAM пусто (или LMS недоступна).
    # Если учётный кэш указывал на загруженную модель — сбрасываем его,
    # чтобы следующий ensure_model_loaded корректно загрузил целевую.
    _remember_loaded(None)
    return None


async def unload_model(model_id: str) -> None:
    url = f"{_LMS_API_BASE}/models/unload"
    session = await _get_session()
    async with session.post(url, headers=_LMS_HEADERS, json={"key": model_id},
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"LM Studio unload '{model_id}': HTTP {resp.status}: {text[:150]}")


async def load_model(model_id: str) -> None:
    url = f"{_LMS_API_BASE}/models/load"
    session = await _get_session()
    async with session.post(url, headers=_LMS_HEADERS, json={"key": model_id},
                            timeout=aiohttp.ClientTimeout(total=180)) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"LM Studio load '{model_id}': HTTP {resp.status}: {text[:150]}")


async def ensure_model_loaded(target: Optional[str]) -> Dict:
    """Выгрузить текущую модель и загрузить выбранную (target=None — дефолт).

    Возвращает {"ok": bool, "loaded": id, "changed": bool, "error": str|None}.
    Идемпотентна: если целевая модель уже в VRAM — ничего не делаем.
    Вызывать ТОЛЬКО когда генерация не идёт (см. /ai/models/select).
    """
    global _loaded_model_id, _load_ts, _last_ops_ts, _preload_task
    target = target or LM_STUDIO_MODEL
    set_desired_model(target)
    if _preload_task and not _preload_task.done():
        _preload_task.cancel()
        _preload_task = None
    async with model_ops_lock:
        current = await get_loaded_model()
        _last_ops_ts = time.monotonic()
        if current == target:
            # Модель УЖЕ в VRAM (например, страница перезагрузилась и фронтенд
            # повторно прислал тот же выбор) — НЕ трогаем веса: никаких
            # unload/load, только сверка учёта. Это и есть защита от повторной
            # загрузки модели при каждом обновлении страницы.
            _remember_loaded(target)
            _notify_state_changed()
            return {"ok": True, "loaded": target, "changed": False, "error": None}
        # Реальные id моделей LM Studio — по ним сверяем «current» перед unload,
        # чтобы мусорное значение не ушло в API (ошибка «identifier not found»).
        known_ids = set(await list_models_cached())
        # Whitelist и для целевой модели: load_model('какой-то мусор') LMS тоже
        # не примет; раньше сюда мог долететь id, удалённый из LMS за время жизни
        # кэша списка — проверяем свежим списком только если whitelist пустел.
        if not known_ids:
            known_ids = set(await list_models_cached(force=True))
        if target not in known_ids:
            return {"ok": False, "loaded": current, "changed": False,
                    "error": f"Модель {target!r} не найдена в LM Studio"}
        try:
            # Страховка: в unload уходит ТОЛЬКО реальный id модели. Пустые или
            # служебные значения ('current', 'default' и т.п.) LM Studio не
            # понимает и отвечает «Model with identifier 'current' not found».
            if current and current != target and current in known_ids:
                await unload_model(current)
            await load_model(target)
            _remember_loaded(target)
            _load_ts = time.monotonic()
            invalidate_models_cache()
            _notify_state_changed()
            logger.info(f"Модель переключена: {current!r} -> {target!r}")
            return {"ok": True, "loaded": target, "changed": True, "error": None}
        except Exception as e:
            logger.error(f"Не удалось переключить модель на {target!r}: {e}")
            # состояние VRAM после сбоя неизвестно — пусть следующим шагом
            # разберётся LM Studio (загрузит по запросу сама)
            _remember_loaded(None)
            invalidate_models_cache()
            _notify_state_changed()
            return {"ok": False, "loaded": current, "changed": False, "error": str(e)}


def schedule_preload(target: Optional[str]) -> None:
    """Фоновая дозарядка выбранной модели (после того как генерация закончится).

    Смена модели во время печати запрещена, поэтому новая модель грузится
    сразу, как только поток освободился: следующий запрос не тратит секунды
    на холодную загрузку. Старая задача дозарядки отменяется — актуальна
    только последняя выбранная модель.
    """
    global _preload_task
    target = target or LM_STUDIO_MODEL
    if _preload_task and not _preload_task.done():
        _preload_task.cancel()

    async def _worker():
        try:
            # дождаться завершения текущей генерации (см. /gen-status)
            for _ in range(900):          # максимум ~15 минут
                await asyncio.sleep(1.0)
                if model_ops_lock.locked():
                    continue
                st = await get_generation_status()
                if not st.get("active"):
                    break
            else:
                return
            # Лок НЕ берём заранее: ensure_model_loaded идемпотентна и сама
            # захватит model_ops_lock. Прежний «async with model_ops_lock» +
            # вызов внутри давал взаимоблокировку (Lock не реентерабелен) —
            # дозарядка зависала навсегда, модель так и не загружалась.
            await ensure_model_loaded(target)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"preload '{target}': {e}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _preload_task = loop.create_task(_worker(), name=f"model-preload:{target}")


async def get_generation_status() -> Dict:
    """Есть ли активная генерация хотя бы у одного пользователя?

    Используется дозарядкой и эндпоинтом смены модели: менять модель можно
    только когда LM Studio простаивает. Реализовано ленивым импортом, чтобы
    не создавать цикл llm_client <-> routes.ai_assistant.
    """
    try:
        from routes.ai_assistant import any_active_generation
        return await any_active_generation()
    except Exception:
        return {"active": False}
