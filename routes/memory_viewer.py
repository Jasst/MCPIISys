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
async def memory_snapshot(max_nodes: int = 800, uid: str = Depends(require_auth)):
    _enabled()
    mem = await _private_memory(uid)
    max_nodes = max(10, min(2000, int(max_nodes)))
    async with mem._lock:
        mem.reload_if_stale()  # если память правил другой процесс (например, MCP-сервер)
        return core.build_snapshot(mem.store, readonly=READONLY, max_nodes=max_nodes)


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


@router.post("/edit")
async def memory_edit(body: EditBody, uid: str = Depends(require_auth)):
    _enabled()
    if READONLY:
        raise HTTPException(status_code=403, detail="режим только чтение")
    mem = await _private_memory(uid)
    async with mem._lock:
        mem.reload_if_stale()
        try:
            result = core.apply_edit(mem.store, body.op, body.args) or {}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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
