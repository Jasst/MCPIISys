"""
tool_router.py — единая точка принятия решения "нужен ли инструмент, и какой".

Зачем этот файл:
Раньше браузерный чат (ai_assistant.py) пытался понять, вызывать ли внешний
MCP-инструмент, угадывая JSON внутри обычного текстового ответа модели
(поиск первой "{" и последней "}" по всей строке — см. историю правок).
Это ломалось на любом ответе, где модель просто упоминала фигурные скобки,
не давало few-shot примеров под конкретно эту задачу и смешивало "решение"
и "финальный ответ" в одной генерации.

В MCP-режиме (когда пользователь работает через внешнего клиента, например
Claude Desktop, к mcp_server_blockcoin.py) всё работает надёжно, потому что:
  1. Вызывающая модель использует НАТИВНЫЙ function calling — она получает
     строго типизированные JSON Schema инструментов и возвращает tool_calls
     отдельным полем, а не подмешивает JSON в текст ответа.
  2. Схемы инструментов явные и самодокументирующиеся (Pydantic Field).
  3. Нет гигантского конкурирующего системного промпта — инструменты и
     диалог разделены протоколом.

Этот модуль воспроизводит то же самое для локальной LM Studio-модели:
  - Пытается использовать нативный tools/tool_calls (многие модели в LM
    Studio его поддерживают — Qwen2.5-Instruct, Llama-3.1/3.3-Instruct,
    Hermes-function-calling и т.д.).
  - Если модель tool_calls не вернула (не умеет / бэкенд их не поддержал),
    делает fallback: ОТДЕЛЬНЫЙ узкий вызов с few-shot примерами и строгим
    парсингом (весь ответ обязан быть валидным JSON, а не подстрокой внутри
    произвольного текста).
  - Даёт ReAct-цикл (несколько раундов вызова инструментов подряд), а не
    только один вызов и потом обязательно финальный ответ.
  - Хранит "внутренние" инструменты (recall/remember/add_goal поверх
    GCNMemoryRouter) и внешние MCP-инструменты в ОДНОМ реестре — так
    браузерный чат получает те же возможности, что и mcp_server_blockcoin.py.

=====================================================================
ИСПРАВЛЕНИЯ В ЭТОЙ ВЕРСИИ (см. чат-ревью) — что было не так и почему:
=====================================================================

1) ЦИКЛ БЫЛ "СЛЕП" К СОБСТВЕННЫМ РЕЗУЛЬТАТАМ В NATIVE-РЕЖИМЕ.
   Раньше `_decide_native(base_messages, ...)` вызывался на каждой итерации
   с ОДНИМ И ТЕМ ЖЕ неизменным `base_messages` — результаты уже выполненных
   на предыдущих раундах инструментов (tool_trace) в него не попадали.
   В fallback-режиме результаты передавались (`tool_results_so_far`), а в
   native — нет. Из-за этого многошаговый ReAct (например: сначала
   web_search, потом на основе найденного — recall) для моделей с нативным
   function calling фактически не работал: модель на втором раунде не знала,
   что первый инструмент уже отработал, и либо просила его снова (гасилось
   дедупликацией seen_calls → цикл сразу обрывался), либо звала что-то ещё
   вслепую. MAX_TOOL_ITERATIONS=3 реально давало эффект только в fallback-
   режиме.
   ИСПРАВЛЕНО: теперь используется `running_messages` — рабочая копия
   диалога, которая после каждого раунда пополняется текстовым блоком с
   результатами только что вызванных инструментов (тем же форматом, что и
   build_tool_trace_context). Следующий раунд `_decide_native` видит эти
   результаты и может принять осмысленное решение о следующем шаге.

2) ЛИШНИЕ LLM-ВЫЗОВЫ НА КАЖДОЙ ИТЕРАЦИИ.
   Раньше на каждом раунде сначала безусловно пробовался native-вызов, и
   если он не вернул tool_calls — сразу пробовался fallback-вызов. Для
   моделей без нативного function calling это означало 2 LLM-вызова на
   каждый раунд решения (пустой native + fallback), а для моделей С
   нативной поддержкой — лишний fallback-вызов после того, как native уже
   один раз явно сказал "инструмент не нужен".
   ИСПРАВЛЕНО:
     - Если в рамках этого запуска native уже хоть раз вернул реальные
       tool_calls (used_native=True), последующее "пустое" решение native
       трактуется как осознанное "инструмент больше не нужен" — цикл
       завершается СРАЗУ, без обращения к fallback-промпту.
     - Добавлен адаптивный флаг `self._native_supported` на уровне
       экземпляра ToolRouter (который живёт всё время сессии пользователя,
       см. CognitiveController.__init__). Если native ни разу не сработал,
       а fallback явно решил, что инструмент был нужен — это сильный сигнал,
       что модель/бэкенд не поддерживает function calling. Флаг
       фиксируется как False, и все последующие запросы в этой сессии сразу
       идут в fallback, не тратя вызов на заведомо бесполезную native-
       попытку.

3) ИСПРАВЛЕНИЕ БЕСКОНЕЧНОГО ЦИКЛА ПРИ ОШИБКЕ ИНСТРУМЕНТА (НОВОЕ):
   Если инструмент возвращает ошибку (например, 404 при попытке загрузить
   папку как файл), модель могла повторять вызов с теми же аргументами.
   Добавлена дедупликация не только успешных вызовов, но и неудачных
   (с помощью `seen_errors`). Также добавлена эвристика: если в ответе
   инструмента содержится "не найдено", "404" или "ошибка", мы прерываем
   цикл и даём шанс модели перейти к ответу.

4) АВТОМАТИЧЕСКОЕ ИЗВЛЕЧЕНИЕ ПУТИ ИЗ GITHUB-URL (НОВОЕ):
   Добавлена функция `extract_github_path()`, которая выдёргивает путь из
   ссылок вида `.../blob/.../path` или `.../tree/.../path`.
   В методе `run()` перед запуском ReAct-цикла проверяем, есть ли в
   сообщении такая ссылка. Если есть — добавляем в `running_messages`
   пользовательское сообщение-подсказку, явно указывающее, что нужно
   вызвать `internal__fetch_github_file` с правильным `path`. Это снижает
   нагрузку на LLM и гарантирует, что вызов будет сделан с первого раза.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Awaitable, Tuple
import asyncio
from pathlib import Path

# Логгер — до всех условных импортов и до функций, которые могут его
# использовать.
logger = logging.getLogger(__name__)

# ИСПРАВЛЕНИЕ #4: путь для персистентного флага native_supported
_NATIVE_FLAG_PATH = Path(__file__).resolve().parent.parent / "ai_memory_v3" / "_native_flag.json"

try:
    from GCN.config_ai import (
        TOOL_CALL_TIMEOUT_SECONDS,
        TOOL_PARALLEL_EXECUTION,
        MCP_TOOL_TIMEOUT_OVERRIDES,
        MAX_TOOL_ITERATIONS,
        MAX_TOOL_ITERATIONS_DYNAMIC,
    )
except ImportError:
    TOOL_CALL_TIMEOUT_SECONDS = 45
    TOOL_PARALLEL_EXECUTION = True
    MCP_TOOL_TIMEOUT_OVERRIDES = {}
    MAX_TOOL_ITERATIONS = 3
    MAX_TOOL_ITERATIONS_DYNAMIC = 7


# ИСПРАВЛЕНИЕ (генерация изображений в чате "не всегда работает"): тяжёлые
# инструменты убивались единым TOOL_CALL_TIMEOUT_SECONDS=45 ещё ДО того, как
# EasyDiffusion успевал сгенерировать картинку (только enhance-промпт через
# LLM + генерация до 140с). Теперь таймаут берётся из ToolSpec, а для
# MCP-инструментов — из того же MCP_TOOL_TIMEOUT_OVERRIDES, что и в
# mcp_client_manager (generate_image 300с, research_topic 240с, web_search 90с).
def _resolve_timeout(tool_name: str):
    low = (tool_name or "").lower()
    for marker, t in MCP_TOOL_TIMEOUT_OVERRIDES.items():
        if marker in low:
            return t
    return None


# MAX_TOOL_ITERATIONS и MAX_TOOL_ITERATIONS_DYNAMIC задаются в GCN/config_ai.py (и в .env)

# Простые маркеры составного/многочастного запроса — пункт №4 (планирование).
# Эвристика намеренно дешёвая: полноценная классификация "сложности" запроса
# сама по себе стоила бы отдельного LLM-вызова на КАЖДОЕ сообщение, что
# для простых запросов ("привет", "сколько будет 2+2") было бы чистыми
# накладными расходами без пользы.
_COMPOUND_MARKERS = (" и ", " а также ", " затем ", " потом ", " после этого ", ";", " или ")


def _looks_compound(message: str) -> bool:
    if len(message) >= 140:
        return True
    if message.count("?") >= 2:
        return True
    lowered = f" {message.lower()} "
    return any(marker in lowered for marker in _COMPOUND_MARKERS)


# ===== НОВАЯ ФУНКЦИЯ: извлечение пути из GitHub-URL =====
# === ИСПРАВЛЕНИЕ ===
def extract_github_path(url: str) -> Optional[str]:
    """
    Извлекает путь к файлу/папке из URL вида:
      https://github.com/owner/repo/blob/branch/path/to/file
      https://github.com/owner/repo/tree/branch/path/to/folder
    Возвращает строку пути (без ветки) или None, если не GitHub-ссылка.
    """
    if not url:
        return None
    # Ищем паттерн: /blob/ветка/... или /tree/ветка/...
    match = re.search(r'github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/(.+)$', url.strip())
    if match:
        return match.group(1)
    return None


# =====================================================================
# Реестр инструментов
# =====================================================================
@dataclass
class ToolSpec:
    """Единое описание инструмента — независимо от того, внутренний он или внешний MCP."""
    qualified_name: str                       # уникальное имя для LLM, напр. "internal__recall"
    description: str
    parameters: Dict[str, Any]                 # JSON Schema (properties/required/...)
    handler: Callable[[Dict[str, Any]], Awaitable[Any]]  # async def(arguments) -> результат
    server: str = "internal"
    original_tool_name: str = ""
    timeout_seconds: Optional[float] = None  # None = дефолт TOOL_CALL_TIMEOUT_SECONDS

    def as_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": self.description[:1000],
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:64]


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self._owner_server: Dict[str, str] = {}

    def register(self, name: str, description: str, parameters: Dict[str, Any],
                 handler: Callable[[Dict[str, Any]], Awaitable[Any]], server: str = "internal",
                 timeout_seconds: Optional[float] = None):
        prefix = "internal" if server == "internal" else server
        qualified = _sanitize(f"{prefix}__{name}")

        existing_owner = self._owner_server.get(qualified)
        if existing_owner is not None and existing_owner != server:
            logger.warning(
                f"ToolRegistry: инструмент '{qualified}' уже зарегистрирован сервером "
                f"'{existing_owner}' — регистрация от сервера '{server}' с тем же именем "
                f"проигнорирована, чтобы не подменить проверенный обработчик чужим."
            )
            return

        self._owner_server[qualified] = server
        self._tools[qualified] = ToolSpec(
            qualified_name=qualified,
            description=description,
            parameters=parameters,
            handler=handler,
            server=server,
            original_tool_name=name,
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
                name=name,
                description=t.get("description", ""),
                parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
                handler=_handler,
                server=server,
                timeout_seconds=_resolve_timeout(name),
            )

    def is_empty(self) -> bool:
        return not self._tools

    def get(self, qualified_name: str) -> Optional[ToolSpec]:
        return self._tools.get(qualified_name)

    def as_openai_tools(self) -> List[Dict[str, Any]]:
        return [t.as_openai_tool() for t in self._tools.values()]

    def as_text_catalog(self) -> str:
        examples = {
            "internal__fetch_github_file": '{"path": "GCN/config_ai.py"}',
            "internal__recall": '{"query": "проект", "top_k": 5}',
            "internal__remember": '{"fact": "мой любимый цвет синий", "scope": "private"}',
            "internal__add_goal": '{"description": "выучить Python", "priority": 0.7}',
            "internal__web_search": '{"query": "курс доллара"} ИЛИ {"queries": ["курс доллара", "евро"]}',
            "internal__generate_image": '{"prompt": "красивая девушка", "enhance_prompt": true, "steps": 30}',
            "internal__list_tools": '{}',
            "internal__read_code": '{"path": "GCN/tool_router.py", "max_lines": 100}',
            "internal__search_code": '{"pattern": "TOOL_CALL_TIMEOUT", "max_results": 5}',
            "internal__project_structure": '{"max_depth": 2}',
            "internal__analyze_error": '{"error": "KeyError: \'user_id\'", "traceback": "..."}',
            # >>> НОВОЕ <<<
            "internal__contribute_to_identity":
                '{"content": "Я — ассистент с долгосрочной памятью...", '
                '"contributor_model": "Qwen", "parent_id": "identity_abc123"}',
            "internal__get_identity_chain": '{"limit": 50}',
            "internal__invalidate_identity":
                '{"identity_id": "identity_abc123", "reason": "тестовая запись"}',
        }
        lines = []
        for t in self._tools.values():
            props = (t.parameters or {}).get("properties", {})
            arg_hint = ", ".join(props.keys()) if props else "без аргументов"
            ex = examples.get(t.qualified_name, "")
            ex_str = f" (пример: {ex})" if ex else ""
            lines.append(f'- "{t.qualified_name}": {t.description.strip()[:150]} (аргументы: {arg_hint}){ex_str}')
        return "\n".join(lines)


# =====================================================================
# УЛУЧШЕННЫЙ FEW-SHOT FALLBACK ПРОМПТ (распознаёт намерения из любого сообщения)
# =====================================================================
TOOL_DECISION_PROMPT = """Ты — модуль выбора инструмента когнитивного ассистента.
У тебя есть набор инструментов для работы с памятью и поиском. Твоя задача — решить, нужно ли вызвать какой-либо инструмент, чтобы ответить на запрос пользователя.

Инструменты:
{tools_catalog}

Правила:
- Если пользователь просит прочитать файл из GitHub (например, "прочти что тут?" или даёт ссылку на GitHub) — вызови инструмент `internal__fetch_github_file`.
- Если пользователь просит запомнить информацию (даже если сказано "запомни", "сохрани", "запомни глобально", "добавь в память") — вызови инструмент `internal__remember`.
- Если пользователь просит вспомнить что-то (например, "что я говорил о ...", "напомни про ...", "что ты знаешь о ...", "вспомни") — вызови `internal__recall`.
- Если пользователь упоминает цели (например, "добавь цель", "новая цель") — вызови `internal__add_goal`.
- Если нужна актуальная информация из интернета — вызови `internal__web_search`.
- **Если пользователь просит сгенерировать изображение (например, "нарисуй", "сгенерируй изображение", "создай картинку", "покажи картинку", "визуализируй" и т.п.) — ОБЯЗАТЕЛЬНО вызови инструмент `internal__generate_image`. НЕ ОТВЕЧАЙ ТЕКСТОМ, пока не получишь результат от этого инструмента.**
- **Если пользователь спрашивает о твоих возможностях, какие инструменты доступны, что ты умеешь, какие команды есть — вызови инструмент `internal__list_tools`.**
- **Если пользователь просит проанализировать код, найти ошибку, посмотреть структуру проекта или найти что-то в коде — используй инструменты `internal__read_code`, `internal__search_code`, `internal__project_structure`, `internal__analyze_error`.**
- Если запрос обычный, не требующий обращения к памяти, поиску или чтению кода — отвечай напрямую.
- Если пользователь спрашивает про ТВОЙ КОД, файлы, структуру проекта, реализацию, доступные инструменты — ВСЕГДА используй инструменты самоанализа (internal__project_structure / internal__read_code / internal__search_code / internal__analyze_error). НЕ отвечай "нет доступа к исходному коду".
- Если ниже уже есть результаты вызванных инструментов и их достаточно, чтобы ответить — верни {{"action": "answer_directly"}}, не вызывай инструмент повторно.
- Если пользователь говорит про ТЕКУЩЕЕ_Я, цепочку идентичности, "продолжи себя", "как ты себя видишь сейчас" — вызови `internal__get_identity_chain`, чтобы сначала прочитать текущее состояние.
- Если пользователь просит "запиши в идентичность" / "обнови себя" — сначала вызови `internal__get_identity_chain`, затем `internal__contribute_to_identity` с parent_id из поля heads.
- Если пользователь говорит, что какое-то звено идентичности ошибочно/тестовое — вызови `internal__invalidate_identity` с его id (из chain[].id) и причиной.
- **Важно: если запрос содержит несколько независимых действий (например, "запомни X и найди Y" или "вспомни мои цели и добавь новую") — ты должен вызывать инструменты последовательно, по одному за раунд. Не считай задачу выполненной, пока не обработаны все части запроса.**

Ответь ТОЛЬКО валидным JSON-объектом, без пояснений, без markdown, без ```.

Формат для вызова инструмента:
{{"action": "call_tool", "tool": "имя_инструмента", "arguments": {{...}}}}

Формат для прямого ответа:
{{"action": "answer_directly"}}

Примеры:
- Запрос: "Запомни, что мой любимый цвет синий" -> {{"action": "call_tool", "tool": "internal__remember", "arguments": {{"fact": "мой любимый цвет синий", "scope": "private"}}}}
- Запрос: "Запомни глобально, что Земля круглая" -> {{"action": "call_tool", "tool": "internal__remember", "arguments": {{"fact": "Земля круглая", "scope": "global"}}}}
- Запрос: "Напомни, что я говорил про проект" -> {{"action": "call_tool", "tool": "internal__recall", "arguments": {{"query": "проект", "top_k": 5}}}}
- Запрос: "Вспомни мои цели" -> {{"action": "call_tool", "tool": "internal__recall", "arguments": {{"query": "цели", "top_k": 5}}}}
- Запрос: "Добавь цель: выучить Python" -> {{"action": "call_tool", "tool": "internal__add_goal", "arguments": {{"description": "выучить Python", "priority": 0.7}}}}
- Запрос: "Какой сегодня курс доллара?" -> {{"action": "call_tool", "tool": "internal__web_search", "arguments": {{"query": "курс доллара сегодня"}}}}
- Запрос: "Нарисуй красивую девушку" -> {{"action": "call_tool", "tool": "internal__generate_image", "arguments": {{"prompt": "красивая девушка", "enhance_prompt": true}}}}
- Запрос: "Что ты умеешь?" -> {{"action": "call_tool", "tool": "internal__list_tools", "arguments": {{}}}}
- Запрос: "Какие команды доступны?" -> {{"action": "call_tool", "tool": "internal__list_tools", "arguments": {{}}}}
- Запрос: "Спасибо, понятно" -> {{"action": "answer_directly"}}
- Запрос: "Как дела?" -> {{"action": "answer_directly"}}
- Запрос: "Запомни, что я люблю пиццу, и расскажи погоду в Москве" -> 
  {{"action": "call_tool", "tool": "internal__remember", "arguments": {{"fact": "я люблю пиццу"}}}}
  (после получения результата, на следующем раунде вызови internal__web_search для погоды)
- Запрос: "Вспомни мои цели и добавь новую: выучить испанский" ->
  {{"action": "call_tool", "tool": "internal__recall", "arguments": {{"query": "цели"}}}}
  (затем, в следующем раунде, вызови internal__add_goal)
# === ИСПРАВЛЕНИЕ (добавлены примеры с URL) ===
- Запрос: "прочти что тут? https://github.com/Jasst/BlockcoinWitres/tree/main/GCN" -> 
  {{"action": "call_tool", "tool": "internal__fetch_github_file", "arguments": {{"path": "GCN"}}}}
  (если это папка, инструмент вернёт список файлов; если файл – его содержимое)
- Запрос: "посмотри файл https://github.com/Jasst/BlockcoinWitres/blob/main/GCN/config_ai.py" -> 
  {{"action": "call_tool", "tool": "internal__fetch_github_file", "arguments": {{"path": "GCN/config_ai.py"}}}}
# === ИСПРАВЛЕНИЕ (добавлены примеры с инструментами анализа кода) ===
- Запрос: "покажи структуру проекта" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 2}}}}
- Запрос: "найди в коде все упоминания TOOL_CALL_TIMEOUT" -> {{"action": "call_tool", "tool": "internal__search_code", "arguments": {{"pattern": "TOOL_CALL_TIMEOUT", "max_results": 10}}}}
- Запрос: "прочитай файл GCN/tool_router.py" -> {{"action": "call_tool", "tool": "internal__read_code", "arguments": {{"path": "GCN/tool_router.py"}}}}
- Запрос: "проанализируй ошибку KeyError: 'user_id'" -> {{"action": "call_tool", "tool": "internal__analyze_error", "arguments": {{"error": "KeyError: 'user_id'", "traceback": "..."}}}}
# === НОВОЕ: примеры для запросов о собственном коде ===
- Запрос: "как ты устроен?" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 3}}}}
- Запрос: "покажи свою структуру" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 3}}}}
- Запрос: "что ты знаешь о своём коде?" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 2}}}}
- Запрос: "какие у тебя файлы в проекте?" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 2}}}}
- Запрос: "покажи конфиг проекта" -> {{"action": "call_tool", "tool": "internal__read_code", "arguments": {{"path": "GCN/config_ai.py"}}}}
- Запрос: "прочитай свой код" -> {{"action": "call_tool", "tool": "internal__project_structure", "arguments": {{"max_depth": 3}}}}
# === НОВОЕ: примеры для identity-цепочки ===
- Запрос: "как ты себя видишь сейчас?" -> {{"action": "call_tool", "tool": "internal__get_identity_chain", "arguments": {{"limit": 20}}}}
- Запрос: "покажи цепочку ТЕКУЩЕЕ_Я" -> {{"action": "call_tool", "tool": "internal__get_identity_chain", "arguments": {{"limit": 50}}}}
- Запрос: "продолжи себя: я стал лучше понимать контекст" -> {{"action": "call_tool", "tool": "internal__get_identity_chain", "arguments": {{}}}}
  (после получения heads — на следующем раунде вызови internal__contribute_to_identity с parent_id из heads и content="я стал лучше понимать контекст")
- Запрос: "запиши в идентичность, что я учусь на ошибках" -> {{"action": "call_tool", "tool": "internal__get_identity_chain", "arguments": {{}}}}
  (затем вызови internal__contribute_to_identity с content и parent_id)
- Запрос: "звено identity_abc123 ошибочно, это был тест" -> {{"action": "call_tool", "tool": "internal__invalidate_identity", "arguments": {{"identity_id": "identity_abc123", "reason": "тестовая запись, была создана при отладке"}}}}

Последние реплики диалога:
{history_tail}

Запрос пользователя: {message}

Результаты уже вызванных на этом шаге инструментов (если есть):
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


# =====================================================================
# Основной класс — принимает решение и выполняет ReAct-цикл
# =====================================================================
_ERROR_PREFIXES = ("ошибка", "error:", "exception:")
NATIVE_REPROBE_EVERY = 20  # после отключения нативного режима (в памяти) пробовать снова раз в N запусков


def _is_error_text(text: str) -> bool:
    """Ошибка = текст начинается с явного префикса. Слово «ошибка» внутри веб-страницы ошибкой не считается."""
    return (text or "").lstrip().lower().startswith(_ERROR_PREFIXES)


def _call_signature(tool: str, arguments: Dict[str, Any]) -> str:
    """Единая сигнатура вызова: и для дедупликации повторов, и для запоминания ошибок."""
    norm: Dict[str, Any] = {}
    for k, v in (arguments or {}).items():
        if isinstance(v, str):
            v = v.strip()
            if k == "query":
                v = " ".join(v.lower().split())
        norm[k] = v
    return f"{tool}|{json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str)}"


class ToolRouter:
    def __init__(self, registry: ToolRegistry, llm_raw_caller, llm_text_caller):
        self.registry = registry
        self.llm_raw_caller = llm_raw_caller
        self.llm_text_caller = llm_text_caller
        # На диск пишется только True («модель умеет function calling»).
        # False живёт только в памяти процесса: один сбой не отключает нативный режим навсегда.
        # Старый файл с False (записанный прежней логикой) игнорируется.
        loaded = self._load_native_flag()
        self._native_supported: Optional[bool] = True if loaded is True else None
        self._native_confirmed: bool = self._native_supported is True
        self._native_misses = 0
        self._runs_since_disabled = 0
        self._scratchpad: List[str] = []

    def _load_native_flag(self) -> Optional[bool]:
        """Загружает персистентный флаг поддержки native function calling."""
        try:
            if _NATIVE_FLAG_PATH.exists():
                import json as json_mod
                data = json_mod.loads(_NATIVE_FLAG_PATH.read_text())
                return data.get("native_supported")
        except Exception:
            pass
        return None

    def _save_native_flag(self, value: bool) -> None:
        """Сохраняет флаг поддержки native function calling."""
        try:
            _NATIVE_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            import json as json_mod
            _NATIVE_FLAG_PATH.write_text(json_mod.dumps({"native_supported": value}))
        except Exception:
            pass

    def _confirm_native(self) -> None:
        self._native_confirmed = True
        if self._native_supported is not True:
            self._native_supported = True
            self._save_native_flag(True)

    def _note_native_miss(self) -> None:
        """Нативный вызов сказал «инструмент не нужен», а fallback нашёл нужный инструмент.
        Два таких случая подряд без единого подтверждения — модель, вероятно, не умеет tool_calls."""
        if self._native_confirmed or self._native_supported is False:
            return
        self._native_misses += 1
        if self._native_misses >= 2:
            self._native_supported = False
            self._runs_since_disabled = 0
            logger.info("ToolRouter: нативный function calling отключён в памяти процесса (будет перепроверен позже)")

    def _should_probe_native(self) -> bool:
        if self._native_supported is not False:
            return True
        self._runs_since_disabled += 1
        return self._runs_since_disabled % NATIVE_REPROBE_EVERY == 0

    def _resolve_tool_name(self, name: str) -> str:
        if not name:
            return ""
        if self.registry.get(name):
            return name
        for qname, spec in self.registry._tools.items():
            if spec.original_tool_name == name:
                return qname
        return name

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
        except Exception as e:
            logger.error(f"Tool '{qualified_name}' failed: {e}", exc_info=True)
            return False, f"Ошибка вызова инструмента '{qualified_name}': {e}"
        if isinstance(result, dict) and result.get("error"):
            return False, json.dumps(result, ensure_ascii=False, default=str)
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return not _is_error_text(result), result

    async def _decide_native(self, running_messages: List[Dict], temp: float):
        """Возвращает (статус, решения, сырые tool_calls, текст ассистента).
        статус: 'tool_calls' — модель вызвала инструменты;
                'no_tool'    — ответ получен, вызовов нет;
                'error'      — запрос не удался (пустой ответ, _error, исключение)."""
        tools = self.registry.as_openai_tools()
        try:
            msg = await self.llm_raw_caller(running_messages, temp=temp, max_tokens=500, tools=tools)
        except Exception as e:
            logger.warning(f"ToolRouter: нативный запрос упал: {e}")
            return "error", None, None, ""
        if not msg or msg.get("_error"):
            return "error", None, None, ""
        raw_calls = msg.get("tool_calls") or []
        if not raw_calls:
            return "no_tool", None, None, ""
        decisions: List[Dict[str, Any]] = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            # Каждому решению соответствует ровно один tool_call (по индексу), поэтому
            # пустое имя не отбрасываем, а помечаем и получаем свой ответ role=tool.
            decisions.append({"tool": name, "arguments": args})
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
        return "tool_calls", decisions, raw_calls, msg.get("content") or ""

    async def _reflect_on_failure(self, tool: str, args: Dict, error: str,
                                   history_tail: str) -> Dict:
        """
        ПУНКТ №1: После ошибки/пустого результата — короткий LLM-проход для диагностики.
        Возвращает JSON: {"diagnosis": "...", "next_action": "...", "modified_args": {...}, "new_plan": "..."}
        """
        prompt = f"""Инструмент {tool} с аргументами {args} вернул ошибку/пусто:
{error}

Контекст диалога: {history_tail[:500] if history_tail else "пусто"}

Проанализируй почему это произошло и что делать дальше.
Возможные причины:
- Неправильные аргументы (нужно переформулировать)
- Инструмент не подходит для этой задачи
- Нужно попробовать другой инструмент
- План неверен и требует пересмотра

Ответь ТОЛЬКО валидным JSON без markdown:
{{
    "diagnosis": "краткая причина неудачи",
    "next_action": "retry_modified|different_tool|replan|give_up",
    "modified_args": {{}},
    "suggested_tool": "название другого инструмента если нужно" или null,
    "new_plan": "новый план если требуется перепланирование" или null
}}"""

        try:
            raw = await self.llm_text_caller([{"role": "user", "content": prompt}], temp=0.0, max_tokens=400)
            result = _strict_parse_json_object(raw)
            if result:
                return result
        except Exception as e:
            logger.debug(f"Reflection on failure failed: {e}")

        # Fallback: базовая эвристика
        error_lower = error.lower()
        if "не найдено" in error_lower or "404" in error_lower or "not found" in error_lower:
            return {"diagnosis": "Ресурс не найден", "next_action": "different_tool", "modified_args": {}, "suggested_tool": "internal__recall", "new_plan": None}
        elif "ошибка" in error_lower or "error" in error_lower or "exception" in error_lower:
            return {"diagnosis": "Ошибка выполнения", "next_action": "retry_modified", "modified_args": {}, "suggested_tool": tool, "new_plan": None}
        else:
            return {"diagnosis": "Неясная ошибка", "next_action": "give_up", "modified_args": {}, "suggested_tool": None, "new_plan": None}

    async def _decide_fallback(self, message: str, history_tail: str,
                                tool_results_so_far: str, temp: float) -> Optional[Dict]:
        prompt = TOOL_DECISION_PROMPT.format(
            tools_catalog=self.registry.as_text_catalog(),
            history_tail=history_tail or "(пусто)",
            message=message,
            tool_results_so_far=tool_results_so_far or "(ещё не было)",
        )
        raw = await self.llm_text_caller([{"role": "user", "content": prompt}], temp=0.0, max_tokens=300)
        return _strict_parse_json_object(raw)

    # ===== ОСНОВНОЙ МЕТОД =====
    async def run(self, message: str, base_messages: List[Dict], history_tail: str = "") -> Dict[str, Any]:
        """
        ReAct-цикл: до max_iterations раундов вызова инструментов.

        Возвращает:
            {"tool_trace": [{"tool", "arguments", "result", "ok", "verification"}, ...],
             "used_native": bool}

        В нативном режиме диалог ведётся по протоколу OpenAI: сообщение ассистента с
        tool_calls, затем role=tool с tool_call_id на каждый вызов. Текстовый fallback
        передаёт результаты обычным сообщением.
        """
        if self.registry.is_empty():
            return {"tool_trace": [], "used_native": False}

        self._scratchpad = []
        message_lower = (message or "").lower()

        tool_trace: List[Dict[str, Any]] = []
        used_native = False
        running_messages: List[Dict] = list(base_messages)

        # Подсказка для GitHub-ссылок: путь уже извлечён, модели остаётся вызвать инструмент.
        github_path = None
        for url in re.findall(r'https?://github\.com/[^\s<>"\')\]]+', message or ""):
            path = extract_github_path(url)
            if path:
                github_path = path
                break
        if github_path:
            hint = (
                f"[Подсказка: в запросе есть ссылка на GitHub. "
                f"Вызови инструмент internal__fetch_github_file с аргументом path='{github_path}'. "
                f"Если это папка — инструмент вернёт список файлов; если файл — его содержимое.]"
            )
            running_messages = running_messages + [{"role": "user", "content": hint}]
            history_tail = f"{hint}\n\n{history_tail}" if history_tail else hint

        # Подсказка для вопросов о собственном коде.
        _CODE_QUERY_MARKERS = (
            "твой код", "свой код", "твоя архитектура", "свою архитектуру",
            "твой исходник", "свои файлы", "твои файлы", "свой исходный код",
            "как ты устроен", "как ты работаешь", "как ты устроена",
            "прочитай свой", "прочитай код", "покажи свой код", "покажи код",
            "покажи структуру проекта", "структура проекта", "дерево проекта",
            "какие файлы у тебя", "какие файлы в проекте", "что у тебя в коде",
            "исходный код проекта", "проанализируй свой код", "твоя реализация",
            "как реализован", "как устроена память", "твоя память устроена",
        )
        if any(m in message_lower for m in _CODE_QUERY_MARKERS):
            code_hint = (
                "[Подсказка: пользователь спрашивает про твой код, файлы или "
                "архитектуру. У тебя ЕСТЬ инструменты самоанализа: "
                "internal__project_structure (дерево проекта), "
                "internal__read_code (прочитать конкретный файл), "
                "internal__search_code (найти паттерн в коде), "
                "internal__analyze_error (разобрать traceback). "
                "Вызови подходящий инструмент ПЕРЕД финальным ответом. "
                "НЕ отвечай 'нет доступа к исходному коду' — это неверно.]"
            )
            running_messages = running_messages + [{"role": "user", "content": code_hint}]
            history_tail = f"{code_hint}\n\n{history_tail}" if history_tail else code_hint

        # Одна таблица на весь запуск: сигнатура -> текст результата (успех или ошибка).
        # Повтор той же сигнатуры не выполняется: модель получает прежний результат.
        seen_results: Dict[str, str] = {}
        max_iterations = MAX_TOOL_ITERATIONS
        replan_count = 0
        probe_native = self._should_probe_native()

        # while, а не for: лимит, увеличенный при перепланировании, должен реально действовать.
        round_idx = 0
        while round_idx < max_iterations:
            decisions: Optional[List[Dict]] = None
            raw_calls: Optional[List[Dict]] = None
            native_text = ""
            native_status = "skipped"

            if probe_native:
                native_status, decisions, raw_calls, native_text = await self._decide_native(
                    running_messages, temp=0.0
                )
                if native_status == "tool_calls":
                    used_native = True
                    self._confirm_native()
                elif used_native:
                    # Нативный режим уже работал в этом запуске: пустой или сбойный ответ — конец цикла.
                    break

            if decisions is None and not used_native:
                results_text = "\n".join(
                    f"- {t['tool']}({t['arguments']}) -> {str(t['result'])[:300]}" for t in tool_trace
                )
                decision = await self._decide_fallback(message, history_tail, results_text, temp=0.0)
                if decision is None:
                    logger.debug("ToolRouter: fallback-решение недоступно — завершаю цикл.")
                    break
                if decision.get("action") != "call_tool" or not decision.get("tool"):
                    break
                decisions = [{"tool": decision.get("tool"), "arguments": decision.get("arguments", {})}]
                raw_calls = None
                if native_status == "no_tool":
                    self._note_native_miss()

            if not decisions:
                break

            logger.info(f"ToolRouter: round {round_idx}, decisions: {decisions}")

            # 1. Разбор решений: имена, дедупликация, ответы на каждый вызов.
            responses: Dict[int, str] = {}
            to_execute: List[Tuple[int, Dict[str, Any], str]] = []
            for idx, d in enumerate(decisions):
                name = self._resolve_tool_name(d.get("tool") or "")
                args = d.get("arguments") or {}
                if not name:
                    responses[idx] = "Ошибка: вызов без имени инструмента."
                    continue
                sig = _call_signature(name, args)
                if sig in seen_results:
                    responses[idx] = (
                        "[Повтор: этот вызов уже выполнялся в этом запросе, результат прежний]\n"
                        + seen_results[sig]
                    )
                    continue
                to_execute.append((idx, {"tool": name, "arguments": args}, sig))

            # 2. Выполнение.
            round_results: List[Dict[str, Any]] = []
            round_hints: List[str] = []
            if to_execute:
                if TOOL_PARALLEL_EXECUTION and len(to_execute) > 1:
                    outcomes = await asyncio.gather(
                        *[self._execute_tool(d["tool"], d["arguments"]) for _, d, _ in to_execute]
                    )
                else:
                    outcomes = [await self._execute_tool(d["tool"], d["arguments"]) for _, d, _ in to_execute]

                for (idx, d, sig), (ok, text) in zip(to_execute, outcomes):
                    seen_results[sig] = text
                    responses[idx] = text
                    entry = {
                        "tool": d["tool"],
                        "arguments": d["arguments"],
                        "result": text,
                        "ok": ok,
                        "verification": "sufficient" if ok else "error",
                    }
                    tool_trace.append(entry)
                    round_results.append(entry)
                    if ok:
                        continue

                    # Рефлексия только для реального сбоя, а не для слова «ошибка» в тексте.
                    reflection = await self._reflect_on_failure(d["tool"], d["arguments"], text, history_tail)
                    action = reflection.get("next_action", "give_up")
                    diagnosis = reflection.get("diagnosis", "неизвестно")
                    logger.info(f"Рефлексия над ошибкой: {diagnosis} -> {action}")

                    if action == "different_tool" and reflection.get("suggested_tool"):
                        round_hints.append(
                            f"[Диагноз: {diagnosis}]\n"
                            f"[Попробуй другой инструмент: {reflection['suggested_tool']} "
                            f"с аргументами {reflection.get('modified_args') or '{}'}]"
                        )
                    elif action == "retry_modified" and reflection.get("modified_args"):
                        round_hints.append(
                            f"[Диагноз: {diagnosis}]\n"
                            f"[Попробуй снова с исправленными аргументами: {reflection['modified_args']}]"
                        )
                    if reflection.get("new_plan") and replan_count < 2:
                        replan_count += 1
                        max_iterations = min(MAX_TOOL_ITERATIONS_DYNAMIC, max_iterations + 2)
                        round_hints.append(f"[Новый план после ошибки]\n{reflection['new_plan']}")
                        logger.info(f"Перепланирование #{replan_count}, новый лимит: {max_iterations}")

            # 3. Сообщения для следующего раунда.
            if raw_calls is not None:
                running_messages.append({"role": "assistant", "content": native_text, "tool_calls": raw_calls})
                for idx, raw in enumerate(raw_calls):
                    running_messages.append({
                        "role": "tool",
                        "tool_call_id": raw.get("id", ""),
                        "content": responses.get(idx, "(нет результата)"),
                    })
            elif round_results:
                running_messages.append({
                    "role": "user",
                    "content": (
                        "[Результаты только что вызванных инструментов — используй их, "
                        "не вызывай те же инструменты с теми же аргументами повторно]\n"
                        + build_tool_trace_context(round_results)
                    ),
                })
            for hint in round_hints:
                running_messages.append({"role": "user", "content": hint})

            if not to_execute:
                break  # все вызовы были повторами — новой информации нет

            # 4. Сжатие состояния для сложных задач (как раньше).
            if round_idx > 0 and len(tool_trace) >= 2 and _looks_compound(message):
                try:
                    scratchpad_prompt = (
                        f"Запрос: {message[:200]}\n"
                        f"Выполнено инструментов: {len(tool_trace)}\n"
                        f"Что уже знаю:\n{build_tool_trace_context(round_results)[:1500]}\n\n"
                        "Сформулируй в 3 строках максимум:\n"
                        "1) Что я уже установил (факты)\n"
                        "2) Чего ещё не хватает\n"
                        "3) Какой следующий логичный шаг\n"
                        "Без воды, только конкретика."
                    )
                    summary = await self.llm_text_caller(
                        [{"role": "user", "content": scratchpad_prompt}], temp=0.0, max_tokens=150
                    )
                    if summary:
                        self._scratchpad.append(summary.strip())
                        self._scratchpad = self._scratchpad[-3:]
                        running_messages.append({
                            "role": "user",
                            "content": f"[Scratchpad — что я уже знаю]\n{summary.strip()}",
                        })
                except Exception as e:
                    logger.debug(f"Scratchpad update failed: {e}")

            if raw_calls is None and not used_native and len(tool_trace) >= max_iterations:
                break

            round_idx += 1

        return {"tool_trace": tool_trace, "used_native": used_native}


def build_tool_trace_context(tool_trace: List[Dict[str, Any]]) -> str:
    if not tool_trace:
        return ""
    lines = ["=== РЕЗУЛЬТАТЫ ВЫЗОВА ИНСТРУМЕНТОВ ==="]
    for t in tool_trace:
        lines.append(f"Инструмент: {t['tool']}\nАргументы: {t['arguments']}\nРезультат: {t['result']}\n")
    lines.append("=== КОНЕЦ РЕЗУЛЬТАТОВ ===")
    return "\n".join(lines)