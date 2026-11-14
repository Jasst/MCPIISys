"""
tool_router.py — агентный цикл выбора и вызова инструментов.

Что изменилось относительно прежней версии (и почему):

1. Модель решает сама, а не правила по подстрокам.
   Раньше `preselect_tools` по подстрокам выбирал подмножество инструментов, и всё
   остальное модель не видела вовсе. Теперь ранжирование (`rank_tools`) лишь
   упорядочивает инструменты и ограничивает их число в схеме (TOOL_NATIVE_MAX).
   Нерелевантные инструменты не отбрасываются жёстко: core-набор (recall, remember,
   web_search) всегда доступен, остальные выбираются по близости к запросу.

2. Настоящий цикл вместо «один успешный раунд и стоп».
   Прежний код завершал цикл после первого успешного раунда для «простых» запросов
   (`_looks_compound`). Из-за этого цепочка «найди в памяти → исправь факт» обрывалась.
   Теперь цикл продолжается, пока модель вызывает инструменты. Остановки:
   - модель ответила без tool_calls (штатное завершение);
   - исчерпан лимит раундов или вызовов (AGENT_MAX_ROUNDS / AGENT_MAX_CALLS);
   - обнаружено зацикливание (AGENT_STUCK_LIMIT повторов одного и того же);
   - серия сбоев подряд (AGENT_MAX_CONSECUTIVE_FAILURES);
   - пользователь нажал «пропустить шаг» (request_skip()).

3. Обнаружение зацикливания по смыслу, а не только по точной сигнатуре.
   Повтор одного и того же вызова (с учётом регистра и пробелов) не выполняется,
   а модель получает прежний результат с указанием на повтор. Два таких повтора
   завершают цикл с подсказкой отвечать по уже собранным данным.

4. Пропуск шага. request_skip() прерывает ожидание текущего инструмента и
   останавливает цикл. Ответ строится по тому, что уже получено.

5. Выбор «нужны ли инструменты вообще». might_need_tools() расширяет набор маркеров,
   чтобы запрос не уходил в прямой ответ, когда инструмент действительно нужен.

6. Нативный function calling остаётся основным путём; текстовый JSON-fallback
   сохранён для моделей без tool_calls. Протокол OpenAI (assistant.tool_calls +
   role=tool) сохранён без изменений.
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Персистентный флаг поддержки native function calling (пишется только True).
_NATIVE_FLAG_PATH = Path(__file__).resolve().parent.parent / "ai_memory_v3" / "_native_flag.json"

try:
    from . import config_ai as _cfg
except Exception:  # noqa: BLE001 — роутер должен импортироваться и без полного конфига
    _cfg = None


def _cfg_get(name: str, default: Any) -> Any:
    return getattr(_cfg, name, default) if _cfg is not None else default


TOOL_CALL_TIMEOUT_SECONDS = _cfg_get("TOOL_CALL_TIMEOUT_SECONDS", 45)
TOOL_PARALLEL_EXECUTION = _cfg_get("TOOL_PARALLEL_EXECUTION", True)
MCP_TOOL_TIMEOUT_OVERRIDES = _cfg_get("MCP_TOOL_TIMEOUT_OVERRIDES", {})
MAX_TOOL_ITERATIONS = _cfg_get("MAX_TOOL_ITERATIONS", 3)          # совместимость: используется как минимум
MAX_TOOL_ITERATIONS_DYNAMIC = _cfg_get("MAX_TOOL_ITERATIONS_DYNAMIC", 7)
TOOL_RESULT_MAX_CHARS = _cfg_get("TOOL_RESULT_MAX_CHARS", 3000)

# Новые настройки агентного цикла (все необязательные, есть значения по умолчанию).
AGENT_MAX_ROUNDS = max(1, int(_cfg_get("AGENT_MAX_ROUNDS", 6)))
AGENT_MAX_CALLS = max(1, int(_cfg_get("AGENT_MAX_CALLS", 12)))
AGENT_STUCK_LIMIT = max(1, int(_cfg_get("AGENT_STUCK_LIMIT", 2)))
AGENT_MAX_CONSECUTIVE_FAILURES = max(1, int(_cfg_get("AGENT_MAX_CONSECUTIVE_FAILURES", 4)))
AGENT_REFLECTION_BUDGET = max(0, int(_cfg_get("AGENT_REFLECTION_BUDGET", 2)))  # LLM-рефлексий на один ход
TOOL_NATIVE_MAX = max(3, int(_cfg_get("TOOL_NATIVE_MAX", 14)))   # сколько инструментов видит модель в одном запросе
TOOL_PRESELECT_MAX = 8
TOOL_RESULT_CONTEXT_CHARS = _cfg_get("TOOL_RESULT_CONTEXT_CHARS", 1200)

# Инструменты, которые всегда доступны модели (даже при сильном отборе).
CORE_TOOLS = ("internal__recall", "internal__remember", "internal__web_search")
# Инструмент уточнения: успешный вызов завершает цикл и отдаёт вопрос пользователю.
ASK_USER_TOOL = "internal__ask_user"

# ---------------------------------------------------------------------------
# Ранжирование: ПОДСКАЗКИ, а не фильтр. Маркеры — регулярные выражения, начало слова.
# ---------------------------------------------------------------------------
_WB = r"(?<![а-яёa-z])"

_INTENT_GROUPS: List[Tuple[Tuple[str, ...], List[str]]] = [
    ((r"запомн", r"запиш", r"сохрани", r"добавь в памят"), ["internal__remember"]),
    ((r"вспомн", r"напомн", r"помнишь", r"что я говорил", r"что ты знаешь", r"что я писал",
      r"найди в памят", r"что у меня"), ["internal__recall", "internal__memory_search"]),
    ((r"забуд", r"удали", r"удалит", r"убери из памят", r"стер", r"сотри", r"forget"),
     ["internal__memory_search", "internal__memory_forget"]),
    ((r"исправ", r"измени", r"обнови", r"поправ", r"замени", r"отредактир", r"поменяй", r"переименуй"),
     ["internal__memory_search", "internal__memory_update"]),
    ((r"откуда ты знаешь", r"почему ты так считаешь", r"источник факт", r"объясни факт"),
     ["internal__memory_explain", "internal__memory_search"]),
    ((r"цел(?:ь|и|ей|ям)\b",), ["internal__add_goal"]),
    ((r"поищ", r"найди", r"новост", r"курс", r"погод", r"в интернет", r"актуальн", r"сегодня",
      r"сейчас", r"последн"), ["internal__web_search"]),
    ((r"нарису", r"сгенерир", r"картинк", r"изображени", r"создай картин"), ["internal__generate_image"]),
    ((r"на фото", r"на картинк", r"на изображени", r"на снимк", r"распознай", r"что изображено",
      r"что написано", r"что на "), ["internal__inspect_image"]),
    ((r"код", r"файл", r"функци", r"модул", r"архитектур", r"структур", r"проект", r"класс"),
     ["internal__project_structure", "internal__read_code", "internal__search_code"]),
    ((r"ошибк", r"traceback", r"exception"), ["internal__analyze_error"]),
    ((r"идентичн", r"цепочк", r"текущее_я", r"продолжи себя", r"как ты себя"),
     ["internal__get_identity_chain", "internal__contribute_to_identity"]),
    ((r"инструмент", r"что ты умеешь", r"твои возможност"), ["internal__list_tools"]),
    ((r"github", r"гитхаб"), ["internal__fetch_github_file"]),
]


def _match_markers(msg: str, markers) -> Optional[str]:
    for mk in markers:
        m = re.search(_WB + mk, msg)
        if m:
            return mk
    return None


def _stems(text: str) -> set:
    return {w[:5] for w in re.findall(r"[a-zа-яё_]{4,}", (text or "").lower())}


def rank_tools(message: str, specs) -> Tuple[List[str], str]:
    """Упорядочивает инструменты по близости к запросу. Возвращает (имена, пояснение)."""
    msg = (message or "").lower()
    names = {s.qualified_name for s in specs}
    score: Dict[str, float] = {}
    reasons: List[str] = []
    for markers, tools in _INTENT_GROUPS:
        hit = _match_markers(msg, markers)
        if hit is None:
            continue
        reasons.append(hit)
        for i, n in enumerate(tools):
            if n in names:
                score[n] = score.get(n, 0.0) + (10 - i)
    msg_stems = _stems(msg)
    for s in specs:
        overlap = len(msg_stems & _stems(f"{s.original_tool_name} {s.description or ''}"))
        if overlap:
            score[s.qualified_name] = score.get(s.qualified_name, 0.0) + min(overlap, 3) * 0.5
    ranked = sorted(score, key=lambda n: -score[n])
    return ranked, ", ".join(reasons[:4])


def might_need_tools(message: str) -> bool:
    """Дешёвая проверка «инструмент может понадобиться». Нужна для маршрутизации в чате:
    лучше лишний раз дать модели возможность вызвать инструмент, чем молча ответить без него."""
    msg = (message or "").lower()
    if re.search(r"https?://github\.com/", msg):
        return True
    return any(_match_markers(msg, markers) for markers, _ in _INTENT_GROUPS)


def preselect_tools(message: str, specs) -> Tuple[List[str], str]:
    """Совместимость со старым API: возвращает топ ранжирования (без жёсткого отбора)."""
    ranked, why = rank_tools(message, specs)
    return ranked[:TOOL_PRESELECT_MAX], why


def _clip_result(text) -> str:
    text = str(text or "")
    if len(text) <= TOOL_RESULT_CONTEXT_CHARS:
        return text
    return text[:TOOL_RESULT_CONTEXT_CHARS] + "\n[…результат обрезан для контекста модели]"


def _short_args(args) -> str:
    try:
        s = json.dumps(args or {}, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(args)
    return s if len(s) <= 120 else s[:120] + "…"


def _resolve_timeout(tool_name: str):
    low = (tool_name or "").lower()
    for marker, t in MCP_TOOL_TIMEOUT_OVERRIDES.items():
        if marker in low:
            return t
    return None


# Простой маркер сложного запроса: используется только для сжатия состояния (scratchpad)
# в текстовом fallback-режиме, а не для остановки цикла.
_COMPOUND_MARKERS = (" и ", " а также ", " затем ", " потом ", " после этого ", ";", " или ")


def _looks_compound(message: str) -> bool:
    if len(message or "") >= 140:
        return True
    if (message or "").count("?") >= 2:
        return True
    lowered = f" {(message or '').lower()} "
    return any(marker in lowered for marker in _COMPOUND_MARKERS)


def extract_github_path(url: str) -> Optional[str]:
    """Путь к файлу/папке из ссылок вида .../blob/<ветка>/<путь> или .../tree/<ветка>/<путь>."""
    if not url:
        return None
    match = re.search(r"github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/(.+)$", url.strip())
    return match.group(1) if match else None


# ===========================================================================
# Реестр инструментов (интерфейс не изменён)
# ===========================================================================
@dataclass
class ToolSpec:
    """Единое описание инструмента — независимо от того, внутренний он или внешний MCP."""
    qualified_name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Awaitable[Any]]
    server: str = "internal"
    original_tool_name: str = ""
    timeout_seconds: Optional[float] = None

    def as_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": self.description[:300],
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:64]


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self._owner_server: Dict[str, str] = {}
        self._hidden_prefixes: set = set()

    def set_hidden_prefix(self, prefix: str, hidden: bool) -> None:
        if hidden:
            self._hidden_prefixes.add(prefix)
        else:
            self._hidden_prefixes.discard(prefix)

    def _visible(self) -> List[ToolSpec]:
        hidden = tuple(self._hidden_prefixes)
        return [t for t in self._tools.values()
                if not (hidden and t.qualified_name.startswith(hidden))]

    def register(self, name: str, description: str, parameters: Dict[str, Any],
                 handler: Callable[[Dict[str, Any]], Awaitable[Any]], server: str = "internal",
                 timeout_seconds: Optional[float] = None):
        prefix = "internal" if server == "internal" else server
        qualified = _sanitize(f"{prefix}__{name}")
        existing_owner = self._owner_server.get(qualified)
        if existing_owner is not None and existing_owner != server:
            logger.warning(
                f"ToolRegistry: инструмент '{qualified}' уже зарегистрирован сервером "
                f"'{existing_owner}' — регистрация от '{server}' проигнорирована."
            )
            return
        self._owner_server[qualified] = server
        self._tools[qualified] = ToolSpec(
            qualified_name=qualified, description=description, parameters=parameters,
            handler=handler, server=server, original_tool_name=name,
            timeout_seconds=timeout_seconds,
        )

    def register_mcp_tools(self, mcp_manager, mcp_call: Callable[[str, str, Dict], Awaitable[str]]):
        if not (hasattr(mcp_manager, "_initialized") and mcp_manager._initialized):
            return
        for t in mcp_manager.get_all_tools():
            server = t.get("server", "unknown")
            name = t["name"]

            async def _handler(arguments: Dict[str, Any], _server=server, _name=name) -> str:
                return await mcp_call(_server, _name, arguments)

            self.register(
                name=name, description=t.get("description", ""),
                parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
                handler=_handler, server=server, timeout_seconds=_resolve_timeout(name),
            )

    def is_empty(self) -> bool:
        return not self._tools

    def get(self, qualified_name: str) -> Optional[ToolSpec]:
        return self._tools.get(qualified_name)

    def as_openai_tools(self) -> List[Dict[str, Any]]:
        return [t.as_openai_tool() for t in self._visible()]

    def as_text_catalog(self, only=None) -> str:
        examples = {
            "internal__recall": '{"query": "проект", "top_k": 5}',
            "internal__memory_search": '{"query": "любимый цвет", "top_k": 8}',
            "internal__memory_forget": '{"fact_id": "fct_abc123"} → затем с "confirm": true',
            "internal__memory_update": '{"fact_id": "fct_abc123", "new_text": "любимый цвет — зелёный"}',
            "internal__remember": '{"fact": "мой любимый цвет синий", "scope": "private"}',
            "internal__web_search": '{"query": "курс доллара"} ИЛИ {"queries": ["курс доллара", "евро"]}',
            "internal__inspect_image": '{"question": "что написано на табличке", "region": [0.1, 0.6, 0.5, 0.9]}',
            "internal__generate_image": '{"prompt": "красивая девушка", "enhance_prompt": true}',
            "internal__fetch_github_file": '{"path": "GCN/config_ai.py"}',
        }
        lines = []
        for t in self._visible():
            if only is not None and t.qualified_name not in only:
                continue
            props = (t.parameters or {}).get("properties", {})
            arg_hint = ", ".join(props.keys()) if props else "без аргументов"
            ex = examples.get(t.qualified_name, "")
            ex_str = f" (пример: {ex})" if ex else ""
            lines.append(f'- "{t.qualified_name}": {t.description.strip()[:150]} (аргументы: {arg_hint}){ex_str}')
        return "\n".join(lines)


# ===========================================================================
# Текстовый fallback (для моделей без tool_calls)
# ===========================================================================
TOOL_DECISION_PROMPT = """Ты — модуль выбора инструмента когнитивного ассистента.
Реши, нужен ли один инструмент, чтобы продвинуться к ответу на запрос пользователя.

Инструменты:
{tools_catalog}

Правила:
- Выбирай инструмент, только если без него нельзя ответить точно.
- Если результаты уже вызванных инструментов достаточны — ответь {{"action": "answer_directly"}}.
- Не повторяй вызов с теми же аргументами.
- Для изменения или удаления факта сначала найди его через memory_search (нужен fact_id).

Ответь ТОЛЬКО валидным JSON-объектом, без пояснений и без markdown.
Вызов инструмента: {{"action": "call_tool", "tool": "имя", "arguments": {{...}}}}
Прямой ответ: {{"action": "answer_directly"}}

Последние реплики диалога:
{history_tail}

Запрос пользователя: {message}

Результаты уже вызванных инструментов:
{tool_results_so_far}
"""


def _strict_parse_json_object(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


_ERROR_PREFIXES = ("ошибка", "error:", "exception:")
NATIVE_REPROBE_EVERY = 20


def _is_error_text(text: str) -> bool:
    """Ошибка = текст начинается с явного префикса. Слово «ошибка» внутри текста ошибкой не считается."""
    return (text or "").lstrip().lower().startswith(_ERROR_PREFIXES)


def _call_signature(tool: str, arguments: Dict[str, Any]) -> str:
    """Единая сигнатура вызова: для дедупликации повторов."""
    norm: Dict[str, Any] = {}
    for k, v in (arguments or {}).items():
        if isinstance(v, str):
            v = v.strip()
            if k in ("query", "fact_id", "question"):
                v = " ".join(v.lower().split())
        norm[k] = v
    return f"{tool}|{json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str)}"


def build_tool_trace_context(tool_trace: List[Dict[str, Any]]) -> str:
    if not tool_trace:
        return ""
    lines = ["=== РЕЗУЛЬТАТЫ ВЫЗОВА ИНСТРУМЕНТОВ ==="]
    for t in tool_trace:
        lines.append(f"Инструмент: {t['tool']}\nАргументы: {t['arguments']}\nРезультат: {t['result']}\n")
    lines.append("=== КОНЕЦ РЕЗУЛЬТАТОВ ===")
    return "\n".join(lines)


# ===========================================================================
# Агентный цикл
# ===========================================================================
class ToolRouter:
    def __init__(self, registry: ToolRegistry, llm_raw_caller, llm_text_caller):
        self.registry = registry
        self.llm_raw_caller = llm_raw_caller
        self.llm_text_caller = llm_text_caller
        loaded = self._load_native_flag()
        self._native_supported: Optional[bool] = True if loaded is True else None
        self._native_confirmed: bool = self._native_supported is True
        self._native_misses = 0
        self._runs_since_disabled = 0
        self._scratchpad: List[str] = []
        self._only: Optional[set] = None
        # Пропуск шага: пользователь просит ответить по уже собранному.
        self._skip: asyncio.Event = asyncio.Event()
        self.last_run: Dict[str, Any] = {}

    # ----- управление пользователем -----
    def request_skip(self) -> None:
        """Прерывает текущий инструмент (если выполняется) и завершает цикл. Ответ строится по собранному."""
        self._skip.set()

    def is_skip_requested(self) -> bool:
        return self._skip.is_set()

    # ----- флаг native function calling -----
    def _load_native_flag(self) -> Optional[bool]:
        try:
            if _NATIVE_FLAG_PATH.exists():
                data = json.loads(_NATIVE_FLAG_PATH.read_text())
                return data.get("native_supported")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _save_native_flag(self, value: bool) -> None:
        try:
            _NATIVE_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _NATIVE_FLAG_PATH.write_text(json.dumps({"native_supported": value}))
        except Exception:  # noqa: BLE001
            pass

    def _confirm_native(self) -> None:
        self._native_confirmed = True
        if self._native_supported is not True:
            self._native_supported = True
            self._save_native_flag(True)

    def _note_native_miss(self) -> None:
        if self._native_confirmed or self._native_supported is False:
            return
        self._native_misses += 1
        if self._native_misses >= 2:
            self._native_supported = False
            self._runs_since_disabled = 0
            logger.info("ToolRouter: нативный function calling отключён в памяти процесса")

    def _should_probe_native(self) -> bool:
        if self._native_supported is not False:
            return True
        self._runs_since_disabled += 1
        return self._runs_since_disabled % NATIVE_REPROBE_EVERY == 0

    # ----- вспомогательные -----
    def _resolve_tool_name(self, name: str) -> str:
        if not name:
            return ""
        if self.registry.get(name):
            return name
        for qname, spec in self.registry._tools.items():
            if spec.original_tool_name == name:
                return qname
        return name

    def _offered_tools(self, message: str, specs: List[ToolSpec]) -> List[str]:
        """Какие инструменты увидит модель в этом запросе. Ранжирование, а не жёсткий отбор."""
        names = [s.qualified_name for s in specs]
        if len(names) <= TOOL_NATIVE_MAX:
            return names
        core = [c for c in CORE_TOOLS if c in names]
        ranked, _ = rank_tools(message, specs)
        out: List[str] = list(core)
        for n in ranked:
            if len(out) >= TOOL_NATIVE_MAX:
                break
            if n not in out:
                out.append(n)
        return out

    async def _execute_tool(self, qualified_name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        """Возвращает (успех, текст). Успех определяется типом сбоя, а не поиском слов в тексте."""
        spec = self.registry.get(qualified_name)
        if not spec:
            return False, f"Ошибка: инструмент '{qualified_name}' не найден."
        timeout = spec.timeout_seconds or TOOL_CALL_TIMEOUT_SECONDS
        try:
            result = await asyncio.wait_for(spec.handler(arguments), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Tool '{qualified_name}' timed out after {timeout}s")
            return False, f"Ошибка: инструмент '{qualified_name}' не ответил за {timeout}с."
        except Exception as e:  # noqa: BLE001
            logger.error(f"Tool '{qualified_name}' failed: {e}", exc_info=True)
            return False, f"Ошибка вызова инструмента '{qualified_name}': {e}"
        if isinstance(result, dict) and result.get("error"):
            return False, json.dumps(result, ensure_ascii=False, default=str)
        if isinstance(result, dict) and result.get("status") == "error":
            return False, json.dumps(result, ensure_ascii=False, default=str)
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        ok = not _is_error_text(result)
        if len(result) > TOOL_RESULT_MAX_CHARS:
            result = result[:TOOL_RESULT_MAX_CHARS] + f"\n[...обрезано, всего {len(result)} симв.]"
        return ok, result

    async def _run_guarded(self, name: str, args: Dict[str, Any]) -> Tuple[bool, str, bool]:
        """Выполняет инструмент, но прерывается по пропуску. Возвращает (ok, текст, пропущено)."""
        exec_task = asyncio.ensure_future(self._execute_tool(name, args))
        skip_task = asyncio.ensure_future(self._skip.wait())
        try:
            done, _ = await asyncio.wait({exec_task, skip_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            skip_task.cancel()
        if exec_task in done:
            ok, text = exec_task.result()
            return ok, text, False
        exec_task.cancel()
        return False, "Пропущено пользователем: результат не получен.", True

    async def _decide_native(self, running_messages: List[Dict], temp: float, offered: List[str]):
        """Возвращает (статус, решения, сырые tool_calls, текст ассистента).
        статус: 'tool_calls' | 'no_tool' | 'error'."""
        tools = [t for t in self.registry.as_openai_tools() if t["function"]["name"] in offered]
        if not tools:
            return "no_tool", None, None, ""
        try:
            msg = await self.llm_raw_caller(running_messages, temp=temp, max_tokens=600, tools=tools)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ToolRouter: нативный запрос упал: {e}")
            return "error", None, None, ""
        if not msg or msg.get("_error"):
            return "error", None, None, ""
        raw_calls = msg.get("tool_calls") or []
        if not raw_calls:
            return "no_tool", None, None, msg.get("content") or ""
        decisions: List[Dict[str, Any]] = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            decisions.append({"tool": name, "arguments": args})
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
        return "tool_calls", decisions, raw_calls, msg.get("content") or ""

    async def _reflect_on_failure(self, tool: str, args: Dict, error: str, history_tail: str,
                                  allow_llm: bool = True) -> Dict:
        """Диагностика после сбоя. Однозначные случаи и исчерпанный бюджет решаются без LLM."""
        error_lower = (error or "").lower()
        if "не найдено" in error_lower or "404" in error_lower or "not found" in error_lower:
            return {"diagnosis": "Ресурс не найден", "next_action": "different_tool",
                    "modified_args": {}, "suggested_tool": "internal__memory_search", "new_plan": None}
        if not allow_llm:
            if "ошибка" in error_lower or "error" in error_lower or "exception" in error_lower:
                return {"diagnosis": "Ошибка выполнения (бюджет рефлексии исчерпан)",
                        "next_action": "give_up", "modified_args": {}, "suggested_tool": None, "new_plan": None}
            return {"diagnosis": "Неясная ошибка", "next_action": "give_up",
                    "modified_args": {}, "suggested_tool": None, "new_plan": None}
        prompt = (
            f"Инструмент {tool} с аргументами {args} вернул ошибку:\n{error}\n\n"
            f"Контекст: {history_tail[:500] if history_tail else 'пусто'}\n\n"
            'Ответь ТОЛЬКО валидным JSON без markdown: {"diagnosis": "...", '
            '"next_action": "retry_modified|different_tool|replan|give_up", '
            '"modified_args": {}, "suggested_tool": null, "new_plan": null}'
        )
        try:
            raw = await self.llm_text_caller([{"role": "user", "content": prompt}], temp=0.0, max_tokens=300)
            result = _strict_parse_json_object(raw)
            if result:
                return result
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Reflection on failure failed: {e}")
        if "ошибка" in error_lower or "error" in error_lower or "exception" in error_lower:
            return {"diagnosis": "Ошибка выполнения", "next_action": "retry_modified",
                    "modified_args": {}, "suggested_tool": tool, "new_plan": None}
        return {"diagnosis": "Неясная ошибка", "next_action": "give_up",
                "modified_args": {}, "suggested_tool": None, "new_plan": None}

    async def _decide_fallback(self, message: str, history_tail: str,
                               tool_results_so_far: str, temp: float) -> Optional[Dict]:
        prompt = (TOOL_DECISION_PROMPT
                  .replace("{tools_catalog}", self.registry.as_text_catalog(self._only))
                  .replace("{history_tail}", history_tail or "(пусто)")
                  .replace("{message}", message or "")
                  .replace("{tool_results_so_far}", tool_results_so_far or "(ещё не было)"))
        raw = await self.llm_text_caller([{"role": "user", "content": prompt}], temp=0.0, max_tokens=300)
        return _strict_parse_json_object(raw)

    # ===== ОСНОВНОЙ МЕТОД =====
    async def run(self, message: str, base_messages: List[Dict], history_tail: str = "",
                  on_event=None, context_notes: Optional[List[str]] = None) -> Dict[str, Any]:
        """Агентный цикл. Возвращает:
            {"tool_trace": [...], "used_native": bool, "plan": [...], "rounds": int,
             "stop_reason": str}
        Каждая запись трассы: tool, arguments, result, ok, verification, skipped."""
        if self.registry.is_empty():
            return {"tool_trace": [], "used_native": False, "plan": [], "rounds": 0, "stop_reason": "no_tools"}

        self._skip.clear()
        self._scratchpad = []

        async def emit(kind: str, text: str, **extra) -> None:
            if on_event is None:
                return
            try:
                await on_event({"kind": kind, "text": text, **extra})
            except Exception as e:  # noqa: BLE001
                logger.debug(f"ToolRouter: trace emit failed: {e}")

        specs = self.registry._visible()
        offered = self._offered_tools(message, specs)
        self._only = set(offered)
        if not offered:
            await emit("understand", "инструменты не нужны — отвечаю напрямую")
            self.last_run = {"stop_reason": "no_tools"}
            return {"tool_trace": [], "used_native": False, "plan": [], "rounds": 0, "stop_reason": "no_tools"}

        _, why = rank_tools(message, specs)
        await emit("understand",
                   f"понял запрос ({why or 'без явных маркеров, модель решает сама'}); "
                   f"доступно инструментов: {len(offered)}", tools=offered)

        running: List[Dict] = list(base_messages)
        hints: List[str] = []
        github_path = None
        for url in re.findall(r"https?://github\.com/[^\s<>\"')\]]+", message or ""):
            github_path = extract_github_path(url)
            if github_path:
                break
        if github_path:
            hints.append(f"[Подсказка: в запросе ссылка на GitHub. Вызови internal__fetch_github_file "
                         f"с path='{github_path}'.]")
        for h in hints:
            running.append({"role": "user", "content": h})
        # Краткая память о вызовах прошлых ходов (например, fact_id из memory_search).
        # Передаётся только роутеру, в историю диалога не попадает.
        for note in (context_notes or [])[-4:]:
            running.append({"role": "user", "content": f"[Из прошлых ходов] {note}"})

        tool_trace: List[Dict[str, Any]] = []
        used_native = False
        seen_results: Dict[str, Tuple[bool, str]] = {}
        stuck_events = 0
        consecutive_failures = 0
        replan_count = 0
        reflections_left = AGENT_REFLECTION_BUDGET
        max_rounds = AGENT_MAX_ROUNDS
        hard_round_cap = AGENT_MAX_ROUNDS + 4
        probe_native = self._should_probe_native()
        stop_reason = "model_done"
        pending_question: Optional[str] = None
        round_idx = 0

        while True:
            if round_idx >= max_rounds:
                stop_reason = "round_limit"
                break
            if self._skip.is_set():
                stop_reason = "user_skip"
                await emit("skip", "шаг пропущен — отвечаю по уже собранным данным")
                break
            if len(tool_trace) >= AGENT_MAX_CALLS:
                stop_reason = "call_limit"
                break

            decisions: Optional[List[Dict]] = None
            raw_calls: Optional[List[Dict]] = None
            native_text = ""
            native_status = "skipped"

            if probe_native:
                native_status, decisions, raw_calls, native_text = await self._decide_native(
                    running, 0.0, offered)
                if native_status == "tool_calls":
                    used_native = True
                    self._confirm_native()
                elif native_status == "no_tool" and (used_native or self._native_confirmed):
                    # Модель уже доказала, что понимает протокол, и сама решила, что инструменты не нужны.
                    stop_reason = "model_done"
                    break
                elif used_native:
                    stop_reason = "native_error"
                    break

            if decisions is None and not used_native:
                results_text = "\n".join(
                    f"- {t['tool']}({t['arguments']}) -> {str(t['result'])[:300]}" for t in tool_trace[-6:]
                )
                decision = await self._decide_fallback(message, history_tail, results_text, temp=0.0)
                if decision is None or decision.get("action") != "call_tool" or not decision.get("tool"):
                    stop_reason = "model_done"
                    break
                decisions = [{"tool": decision.get("tool"), "arguments": decision.get("arguments", {})}]
                raw_calls = None
                if native_status == "no_tool":
                    self._note_native_miss()

            if not decisions:
                stop_reason = "model_done"
                break

            await emit("round", f"раунд {round_idx + 1}: " + "; ".join(
                f"{d.get('tool') or '?'}({_short_args(d.get('arguments'))})" for d in decisions))

            # 1. Разбор решений: имена, дедупликация, ответы на каждый вызов.
            responses: Dict[int, str] = {}
            to_execute: List[Tuple[int, Dict[str, Any], str]] = []
            repeats = 0
            for idx, d in enumerate(decisions):
                name = self._resolve_tool_name(d.get("tool") or "")
                args = d.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                if not name or self.registry.get(name) is None:
                    responses[idx] = (f"Ошибка: инструмента '{d.get('tool')}' нет. "
                                      f"Доступны: {', '.join(offered[:12])}")
                    consecutive_failures += 1
                    continue
                sig = _call_signature(name, args)
                if sig in seen_results:
                    repeats += 1
                    responses[idx] = ("[Повтор: этот вызов уже выполнялся, результат прежний]\n"
                                      + seen_results[sig][1])
                    continue
                to_execute.append((idx, {"tool": name, "arguments": args}, sig))

            # 2. Выполнение (параллельно при нескольких вызовах; прерывается пропуском).
            round_results: List[Dict[str, Any]] = []
            round_hints: List[str] = []
            if to_execute:
                if TOOL_PARALLEL_EXECUTION and len(to_execute) > 1:
                    outcomes = await asyncio.gather(
                        *[self._run_guarded(d["tool"], d["arguments"]) for _, d, _ in to_execute])
                else:
                    outcomes = [await self._run_guarded(d["tool"], d["arguments"]) for _, d, _ in to_execute]

                for (idx, d, sig), (ok, text, skipped) in zip(to_execute, outcomes):
                    responses[idx] = text
                    entry = {
                        "tool": d["tool"], "arguments": d["arguments"], "result": text,
                        "ok": ok, "verification": "sufficient" if ok else "error", "skipped": skipped,
                    }
                    if not skipped:
                        seen_results[sig] = (ok, text)
                    tool_trace.append(entry)
                    round_results.append(entry)
                    await emit("tool", ("✓ " if ok else ("⏭ " if skipped else "✗ ")) + d["tool"] + ": "
                               + str(text)[:140].replace("\n", " "), ok=ok)
                    if skipped:
                        continue
                    if ok:
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    # Рефлексия — только для реального сбоя, а не для слова «ошибка» в тексте.
                    allow = reflections_left > 0
                    reflections_left -= 1 if allow else 0
                    reflection = await self._reflect_on_failure(d["tool"], d["arguments"], text, history_tail,
                                                                allow_llm=allow)
                    action = reflection.get("next_action", "give_up")
                    diagnosis = reflection.get("diagnosis", "неизвестно")
                    logger.info(f"Рефлексия над ошибкой: {diagnosis} -> {action}")
                    if action == "different_tool" and reflection.get("suggested_tool"):
                        round_hints.append(f"[Диагноз: {diagnosis}] [Попробуй инструмент "
                                           f"{reflection['suggested_tool']} с аргументами "
                                           f"{reflection.get('modified_args') or '{}'}]")
                    elif action == "retry_modified" and reflection.get("modified_args"):
                        round_hints.append(f"[Диагноз: {diagnosis}] [Повтори с исправленными аргументами: "
                                           f"{reflection['modified_args']}]")
                    if reflection.get("new_plan") and replan_count < 2:
                        replan_count += 1
                        max_rounds = min(hard_round_cap, max_rounds + 2)
                        round_hints.append(f"[Новый план после ошибки]\n{reflection['new_plan']}")

            # 3. Сообщения для следующего раунда (протокол OpenAI при нативном режиме).
            if raw_calls is not None:
                running.append({"role": "assistant", "content": native_text, "tool_calls": raw_calls})
                for idx, raw in enumerate(raw_calls):
                    running.append({"role": "tool", "tool_call_id": raw.get("id", ""),
                                    "content": _clip_result(responses.get(idx, "(нет результата)"))})
            elif round_results:
                running.append({"role": "user", "content": (
                    "[Результаты только что вызванных инструментов — используй их. Если данных достаточно, "
                    "ответь пользователю, иначе вызови следующий инструмент (не повторяя прежние вызовы)]\n"
                    + build_tool_trace_context(round_results))})
            for hint in round_hints:
                running.append({"role": "user", "content": hint})
            round_idx += 1

            # 4. Условия остановки, которые не зависят от модели.
            asked = [e for e in round_results if e["tool"] == ASK_USER_TOOL and e["ok"]]
            if asked:
                # Уточняющий вопрос пользователю: цикл отдаёт управление, ответ строит модель.
                pending_question = asked[-1]["result"]
                stop_reason = "ask_user"
                break
            if not to_execute:
                # Все вызовы — повторы: новой информации нет.
                stop_reason = "no_progress"
                break
            if repeats:
                stuck_events += 1
            if stuck_events >= AGENT_STUCK_LIMIT:
                stop_reason = "stuck"
                await emit("stuck", "модель ходит по кругу — завершаю и отвечаю по собранному")
                running.append({"role": "user", "content": (
                    "[Цикл остановлен: инструменты повторяются. Ответь пользователю на основе уже полученных данных.]")})
                break
            if consecutive_failures >= AGENT_MAX_CONSECUTIVE_FAILURES:
                stop_reason = "failures"
                await emit("stuck", "несколько вызовов подряд не удались — отвечаю по тому, что есть")
                running.append({"role": "user", "content": (
                    "[Несколько вызовов подряд не удались. Ответь, что удалось выяснить, и что не получилось.]")})
                break

            # 5. Сжатие состояния для длинных цепочек в текстовом режиме (малый контекст).
            if not used_native and round_idx >= 2 and len(tool_trace) >= 3:
                try:
                    summary = await self.llm_text_caller([{"role": "user", "content": (
                        f"Запрос: {message[:200]}\nВыполнено инструментов: {len(tool_trace)}\n"
                        f"Результаты:\n{build_tool_trace_context(tool_trace[-4:])[:1500]}\n\n"
                        "В 3 строках: что установлено, чего не хватает, следующий шаг.")}],
                        temp=0.0, max_tokens=150)
                    if summary:
                        running.append({"role": "user", "content": f"[Что уже известно]\n{summary.strip()}"})
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Scratchpad update failed: {e}")

        await emit("done", f"инструментов: {len(tool_trace)}, раундов: {round_idx}; "
                           f"причина остановки: {stop_reason}; перехожу к ответу")
        self.last_run = {"stop_reason": stop_reason, "rounds": round_idx, "calls": len(tool_trace)}
        return {"tool_trace": tool_trace, "used_native": used_native, "plan": offered,
                "rounds": round_idx, "stop_reason": stop_reason, "pending_question": pending_question}
