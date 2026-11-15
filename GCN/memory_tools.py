"""memory_tools.py — редактирование памяти как агентная цепочка.

Схема работы (модель проходит её в несколько раундов роутера):
    memory_search  -> находит факт и его fact_id
    memory_explain -> (опционально) показывает происхождение и противоречия
    memory_update / memory_forget без confirm -> ПРЕДПРОСМОТР, ничего не меняет
    memory_update / memory_forget с confirm=true -> применяет изменение

Защита от необратимых ошибок:
  - удаление и замена без предпросмотра в этом же ходе запрещены;
  - предпросмотр действует 10 минут и привязан к (ключ, скоуп);
  - запрос-подстрока короче 4 символов не принимается (чтобы не стереть лишнее);
  - правка по id принимается только для gcn-идентификаторов (fct_/fact_/...).
"""
import json
import logging
import time
from typing import Any, Dict

from .tool_router import ToolRegistry

logger = logging.getLogger(__name__)

_ID_PREFIXES = ("fct_", "fact_", "concept_", "goal_", "episode_")
_PREVIEW_TTL = 600  # секунд
_MIN_QUERY_LEN = 4


def _is_gcn_id(value: str) -> bool:
    return bool(value) and value.startswith(_ID_PREFIXES)


def _fmt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def register_memory_edit_tools(registry: ToolRegistry, controller) -> None:
    service = controller.memory_service
    # (ключ, скоуп) -> время предпросмотра. Живёт в замыкании: один контроллер = один пользователь.
    previews: Dict[tuple, float] = {}

    def _has_preview(key: str, scope: str) -> bool:
        ts = previews.get((key, scope))
        return ts is not None and (time.time() - ts) <= _PREVIEW_TTL

    async def _search(args: Dict) -> str:
        query = (args.get("query") or "").strip()
        if len(query) < 2:
            return "Укажи запрос (query) — хотя бы несколько символов."
        top_k = int(args.get("top_k", 8))
        scope = args.get("scope")
        rows = await service.recall(query, top_k=top_k, scope=scope)
        if not rows:
            return f"По запросу «{query}» ничего не найдено."
        lines = []
        for r in rows:
            fid = r.get("id") or "нет_id"
            lines.append(f"- id={fid} scope={r.get('scope')} "
                         f"conf={float(r.get('confidence', 0.5)):.2f} :: {str(r.get('text', ''))[:300]}")
        return "Найдено:\n" + "\n".join(lines)

    async def _explain(args: Dict) -> Dict:
        fid = (args.get("fact_id") or "").strip()
        if not _is_gcn_id(fid):
            return {"status": "error", "message": "fact_id должен быть идентификатором из memory_search."}
        info = await service.explain_fact(fid)
        if info.get("error"):
            return {"status": "error", "message": info["error"]}
        return {"status": "ok", **{k: info.get(k) for k in (
            "id", "scope", "text", "confidence", "version", "author", "source_type", "created",
            "confirming_authors", "contradicts")}}

    async def _forget(args: Dict) -> Dict:
        fact_id = (args.get("fact_id") or "").strip()
        query = (args.get("query") or "").strip()
        confirm = bool(args.get("confirm", False))
        if not fact_id and not query:
            return {"status": "error", "message": "Нужен fact_id (предпочтительно) или query."}
        if fact_id:
            if not _is_gcn_id(fact_id):
                return {"status": "error", "message": "fact_id должен быть идентификатором из memory_search."}
            info = await service.explain_fact(fact_id)
            if info.get("error"):
                return {"status": "error", "message": info["error"]}
            scope, key = info["scope"], fact_id
        else:
            if len(query) < _MIN_QUERY_LEN:
                return {"status": "error",
                        "message": f"Запрос короче {_MIN_QUERY_LEN} символов — удаление слишком широкое. "
                                   "Уточни формулировку или используй fact_id."}
            scope, key = (args.get("scope") or "private").lower(), query

        if not confirm:
            preview = await service.forget(key, scope=scope, dry_run=True)
            previews[(key, scope)] = time.time()
            return {"status": "preview", "scope": scope, "would_remove": preview.get("would_remove", 0),
                    "candidates": preview.get("candidates", []),
                    "hint": "Чтобы удалить, повтори memory_forget с теми же аргументами и confirm=true."}

        if not _has_preview(key, scope):
            return {"status": "error",
                    "message": "Сначала выполни предпросмотр: вызови memory_forget без confirm, "
                               "покажи кандидатов и получи подтверждение пользователя."}
        result = await service.forget(key, scope=scope, dry_run=False)
        previews.pop((key, scope), None)
        return {"status": "ok", "removed": result.get("removed", 0), "scope": scope}

    async def _update(args: Dict) -> Dict:
        fid = (args.get("fact_id") or "").strip()
        new_text = (args.get("new_text") or "").strip()
        confirm = bool(args.get("confirm", False))
        if not _is_gcn_id(fid):
            return {"status": "error", "message": "fact_id должен быть идентификатором из memory_search."}
        if len(new_text) < 3:
            return {"status": "error", "message": "new_text слишком короткий."}
        info = await service.explain_fact(fid)
        if info.get("error"):
            return {"status": "error", "message": info["error"]}
        scope = str(info.get("scope") or "private").lower()

        if not confirm:
            previews[(fid, scope)] = time.time()
            return {"status": "preview", "scope": scope, "old_text": info.get("text"),
                    "new_text": new_text,
                    "hint": "Чтобы заменить, повтори memory_update с confirm=true."}

        if not _has_preview(fid, scope):
            return {"status": "error",
                    "message": "Сначала предпросмотр: вызови memory_update без confirm и покажи пользователю "
                               "старый и новый текст."}
        await service.forget(fid, scope=scope, dry_run=False)
        saved = await service.remember(new_text, scope, user_explicit=True)
        previews.pop((fid, scope), None)
        if saved.get("id"):
            return {"status": "ok", "replaced": fid, "new_id": saved.get("id"),
                    "scope": saved.get("scope", scope), "fact": saved.get("fact", new_text)}
        return {"status": "error", "message": f"Старый факт удалён, но новый не сохранён: "
                                             f"{saved.get('reason', 'unknown')}"}

    registry.register(
        name="memory_search",
        description=("Ищет факты в памяти и возвращает их id, скоуп и текст. Нужен перед "
                     "memory_update, memory_forget и memory_explain. Аргументы: query (str), "
                     "top_k (int, опц.), scope (private/shared/global, опц.)."),
        parameters={"type": "object", "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 8},
            "scope": {"type": "string", "enum": ["private", "shared", "global"]}},
            "required": ["query"]},
        handler=_search, server="internal", timeout_seconds=30)

    registry.register(
        name="memory_explain",
        description=("Показывает происхождение факта по fact_id: кто подтвердил, версию, "
                     "противоречащие факты. Аргумент: fact_id (str)."),
        parameters={"type": "object", "properties": {"fact_id": {"type": "string"}}, "required": ["fact_id"]},
        handler=_explain, server="internal", timeout_seconds=30)

    registry.register(
        name="memory_forget",
        description=("Удаляет факт из памяти. ДВА ШАГА: сначала без confirm (предпросмотр, ничего не "
                     "удаляется), затем с confirm=true после подтверждения пользователя. "
                     "Аргументы: fact_id (str, предпочтительно) или query (str, от 4 символов) + scope, "
                     "confirm (bool)."),
        parameters={"type": "object", "properties": {
            "fact_id": {"type": "string"},
            "query": {"type": "string"},
            "scope": {"type": "string", "enum": ["private", "shared", "global"]},
            "confirm": {"type": "boolean", "default": False}},
            "required": []},
        handler=_forget, server="internal", timeout_seconds=30)

    registry.register(
        name="memory_update",
        description=("Исправляет факт: заменяет его текст новым (старая запись удаляется, новая сохраняется "
                     "в том же скоупе). ДВА ШАГА: сначала без confirm (покажет старый и новый текст), затем "
                     "с confirm=true. Аргументы: fact_id (str), new_text (str), confirm (bool)."),
        parameters={"type": "object", "properties": {
            "fact_id": {"type": "string"},
            "new_text": {"type": "string"},
            "confirm": {"type": "boolean", "default": False}},
            "required": ["fact_id", "new_text"]},
        handler=_update, server="internal", timeout_seconds=60)

    logger.info("memory_tools: зарегистрированы memory_search, memory_explain, memory_forget, memory_update")
