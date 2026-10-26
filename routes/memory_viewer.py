"""
routes/memory_viewer.py — просмотр и правка СОБСТВЕННОЙ памяти внутри приложения.

Безопасность:
  • владелец памяти берётся только из сессии (require_auth); параметра памяти нет,
    поэтому подменить пользователя в запросе нельзя;
  • все операции идут через сервис памяти этого пользователя (get_memory_service),
    то есть тем же объектом, которым пользуется чат; правки видны сразу;
  • правки выполняются под блокировкой памяти и сохраняются штатным _save_async();
  • LLM только предлагает изменения, применяет их пользователь отдельным запросом.

Настройки окружения:
  MEMORY_VIEWER_ENABLED=1   (0 — маршруты отвечают 404)
  MEMORY_VIEWER_READONLY=0  (1 — только просмотр)
"""
import asyncio
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import STATIC_FOLDER
from dependencies import require_auth
from GCN import memory_viewer_core as core
from GCN.llm_client import call_llm
from GCN.memory_graph import get_memory_service

ENABLED = os.getenv("MEMORY_VIEWER_ENABLED", "1") == "1"
READONLY = os.getenv("MEMORY_VIEWER_READONLY", "0") == "1"
PAGE = Path(STATIC_FOLDER) / "memory_viewer.html"

router = APIRouter(prefix="/ai/memory", tags=["memory-viewer"])


class EditBody(BaseModel):
    op: str = Field(..., max_length=40)
    args: Dict[str, Any] = Field(default_factory=dict)


class ReviewBody(BaseModel):
    id: str = Field(..., max_length=128)


class EdgeReviewBody(BaseModel):
    source: str = Field(..., max_length=128)
    relation: str = Field(..., max_length=40)
    target: str = Field(..., max_length=128)


class AutoScanBody(BaseModel):
    limit: int = Field(default=core.AUTO_CANDIDATES, ge=5, le=60)
    # False — только повторы и связи без LLM (бесплатно, открывается сразу)
    use_llm: bool = False


class SuggestLinksBody(BaseModel):
    id: str = Field(..., max_length=128)
    k: int = Field(default=10, ge=3, le=20)


class EdgesBatchBody(BaseModel):
    items: list[EdgeReviewBody] = Field(..., min_length=1, max_length=12)


def _enabled():
    if not ENABLED:
        raise HTTPException(status_code=404, detail="not found")


def _check_id(oid: str):
    if not core.ID_RE.match(oid or ""):
        raise HTTPException(status_code=400, detail="некорректный идентификатор")


async def _private_memory(uid: str):
    """Приватная память текущего пользователя — тот же объект, что использует чат."""
    service = await get_memory_service(uid)
    return service.private_memory


@router.get("/page", include_in_schema=False)
async def memory_page(uid: str = Depends(require_auth)):
    _enabled()
    return FileResponse(PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@router.get("/status")
async def memory_status(uid: str = Depends(require_auth)):
    _enabled()
    return {"enabled": ENABLED, "readonly": READONLY}


@router.get("/snapshot")
async def memory_snapshot(max_nodes: int = 800, since: str = "", pca: int = 0,
                          uid: str = Depends(require_auth)):
    """Полный снимок или дельта относительно версии `since`, которую клиент уже знает.
    Если версия не менялась — только {"unchanged": true}. pca=1 добавляет PCA-проекцию."""
    _enabled()
    mem = await _private_memory(uid)
    max_nodes = max(10, min(2000, int(max_nodes)))
    since = (since or "")[:64]
    async with mem._lock:
        # reload_if_stale() делает stat(); файл читается только если его изменил другой процесс
        await asyncio.to_thread(mem.reload_if_stale)
        version = core.store_version(mem.store)
        if since and since == version:
            return {"version": version, "unchanged": True}

        snap = await asyncio.to_thread(core.build_snapshot, mem.store, readonly=READONLY,
                                       max_nodes=max_nodes, with_pca=(pca == 1))
        # история последних снимков пользователя: дельта работает и для нескольких вкладок
        hist = getattr(mem, "_viewer_hist", [])
        base = next((s for s, mn in hist if since and s["version"] == since and mn == max_nodes), None)
        mem._viewer_hist = ([(snap, max_nodes)] + hist)[:4]
        if base is not None:
            return core.diff_snapshots(base, snap)
        return snap


@router.get("/object")
async def memory_object(id: str, uid: str = Depends(require_auth)):
    _enabled()
    _check_id(id)
    mem = await _private_memory(uid)
    async with mem._lock:
        try:
            return core.object_detail(mem.store, id)
        except LookupError:
            raise HTTPException(status_code=404, detail="запись не найдена")


async def _refresh_embedding(mem, oid: str, text: str):
    """Смена текста меняет смысл записи: пересчитываем эмбеддинг тем же путём, что и чат.
    Иначе семантический поиск продолжит находить запись по старому тексту."""
    vec = await asyncio.to_thread(mem.embed_text, text.strip()[:1000])
    if vec:
        mem.store.set_embedding(oid, vec)
    else:  # эмбеддинги выключены: старый вектор уже не соответствует тексту
        mem.store._embedding_index.pop(oid, None)
        mem.store._faiss_dirty = True


@router.post("/edit")
async def memory_edit(body: EditBody, uid: str = Depends(require_auth)):
    _enabled()
    if READONLY:
        raise HTTPException(status_code=403, detail="режим только чтение")
    mem = await _private_memory(uid)
    async with mem._lock:
        await asyncio.to_thread(mem.reload_if_stale)
        try:
            result = core.apply_edit(mem.store, body.op, body.args) or {}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if body.op == "set_subject":
            await _refresh_embedding(mem, body.args["id"], str(body.args["text"]))
        mem._rebuild_caches_from_gcn()  # индексы фактов и кэши соответствуют новому состоянию
    await mem._save_async()
    return {"ok": True, "version": core.store_version(mem.store), **result}


@router.post("/llm/review")
async def memory_llm_review(body: ReviewBody, uid: str = Depends(require_auth)):
    _enabled()
    _check_id(body.id)
    mem = await _private_memory(uid)
    async with mem._lock:
        try:
            detail = core.object_detail(mem.store, body.id)
        except LookupError:
            raise HTTPException(status_code=404, detail="запись не найдена")
    try:
        raw = await call_llm(core.review_messages(detail), temp=0.0, max_tokens=400)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    return {"proposal": core.sanitize_proposal(core.extract_json(raw)), "raw": raw[:1500]}


@router.get("/impact")
async def memory_impact(id: str, uid: str = Depends(require_auth)):
    """Что затронет отзыв записи: от чего она зависит и на чём держатся другие записи."""
    _enabled()
    _check_id(id)
    mem = await _private_memory(uid)
    async with mem._lock:
        try:
            return core.impact(mem.store, id)
        except LookupError:
            raise HTTPException(status_code=404, detail="запись не найдена")


@router.post("/llm/check-edge")
async def memory_llm_check_edge(body: EdgeReviewBody, uid: str = Depends(require_auth)):
    """ИИ оценивает одну связь: оставить, удалить или сменить тип. Ничего не применяет."""
    _enabled()
    _check_id(body.source)
    _check_id(body.target)
    if not core.REL_RE.match(body.relation):
        raise HTTPException(status_code=400, detail="некорректный тип связи")
    mem = await _private_memory(uid)
    async with mem._lock:
        if body.source not in mem.store._objects or body.target not in mem.store._objects:
            raise HTTPException(status_code=404, detail="запись не найдена")
        messages = core.edge_review_messages(mem.store, body.source, body.relation, body.target)
    try:
        raw = await call_llm(messages, temp=0.0, max_tokens=300)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    return {"proposal": core.sanitize_edge(core.extract_json(raw), body.relation), "raw": raw[:1000]}


@router.post("/llm/check-edges")
async def memory_llm_check_edges(body: EdgesBatchBody, uid: str = Depends(require_auth)):
    """ИИ оценивает все связи записи одним запросом. Ничего не применяет."""
    _enabled()
    items = []
    for it in body.items:
        _check_id(it.source)
        _check_id(it.target)
        if not core.REL_RE.match(it.relation):
            raise HTTPException(status_code=400, detail="некорректный тип связи")
        items.append({"source": it.source, "relation": it.relation, "target": it.target})
    mem = await _private_memory(uid)
    async with mem._lock:
        for it in items:
            if it["source"] not in mem.store._objects or it["target"] not in mem.store._objects:
                raise HTTPException(status_code=404, detail="запись не найдена")
        messages = core.edges_batch_messages(mem.store, items)
    try:
        raw = await call_llm(messages, temp=0.0, max_tokens=120 * len(items) + 100)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    return {"proposals": core.sanitize_edges_batch(core.extract_json(raw), items), "raw": raw[:1000]}


@router.post("/llm/suggest-links")
async def memory_llm_suggest_links(body: SuggestLinksBody, uid: str = Depends(require_auth)):
    """ИИ предлагает новые связи для записи. Кандидаты отбираются по эмбеддингам без LLM,
    модель выбирает из них (один запрос). Ничего не применяет."""
    _enabled()
    _check_id(body.id)
    mem = await _private_memory(uid)
    async with mem._lock:
        if body.id not in mem.store._objects:
            raise HTTPException(status_code=404, detail="запись не найдена")
        candidates = core.link_candidates(mem.store, body.id, k=body.k)
        messages = core.link_suggest_messages(mem.store, body.id, candidates) if candidates else None
    if not candidates:
        return {"proposals": [], "candidates": 0, "llm": False}
    try:
        raw = await call_llm(messages, temp=0.0, max_tokens=700)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    return {"proposals": core.sanitize_link_suggestions(core.extract_json(raw), body.id, candidates),
            "candidates": len(candidates), "llm": True, "raw": raw[:1000]}


@router.post("/llm/regenerate")
async def memory_llm_regenerate(body: ReviewBody, uid: str = Depends(require_auth)):
    """ИИ переписывает текст записи, сохраняя смысл. Ничего не применяет."""
    _enabled()
    _check_id(body.id)
    mem = await _private_memory(uid)
    async with mem._lock:
        try:
            detail = core.object_detail(mem.store, body.id)
        except LookupError:
            raise HTTPException(status_code=404, detail="запись не найдена")
    try:
        raw = await call_llm(core.regenerate_messages(detail), temp=0.0, max_tokens=400)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    return {"proposal": core.sanitize_regen(core.extract_json(raw), detail["subject"]), "raw": raw[:1000]}


@router.post("/llm/auto-scan")
async def memory_llm_auto_scan(body: AutoScanBody, uid: str = Depends(require_auth)):
    """Пакетная автокоррекция: ИИ ищет шум/дубли среди подозрительных записей и предлагает действия.

    Применяются только штатные операции через /edit после подтверждения пользователем.
    Дополнительно возвращаются эвристические предложения связей для изолированных записей
    (без LLM) и группы текстовых повторов — их тоже видно до всякого сканирования.
    """
    _enabled()
    mem = await _private_memory(uid)
    async with mem._lock:
        await asyncio.to_thread(mem.reload_if_stale)
        candidates = await asyncio.to_thread(core.auto_candidates, mem.store, body.limit)
        dups = await asyncio.to_thread(core.auto_dups, mem.store)
        link_suggestions = await asyncio.to_thread(
            core.auto_suggest_links, mem.store, [c["id"] for c in candidates])
    if not candidates:
        return {"proposals": [], "candidates": 0, "dups": dups,
                "link_suggestions": link_suggestions, "llm": False,
                "note": "подозрительных записей не найдено"}
    if not body.use_llm:
        return {"proposals": [], "candidates": len(candidates), "dups": dups,
                "link_suggestions": link_suggestions, "llm": False}
    try:
        raw = await call_llm(core.auto_scan_messages(candidates), temp=0.0, max_tokens=1200)
    except Exception:
        raise HTTPException(status_code=503, detail="LM Studio недоступна")
    raw = raw or ""
    proposals = core.sanitize_auto_actions(core.extract_json(raw), candidates)
    return {"proposals": proposals, "candidates": len(candidates), "llm": True,
            "dups": dups, "link_suggestions": link_suggestions, "raw_len": len(raw)}