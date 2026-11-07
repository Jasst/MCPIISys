"""
API окна «Настройки модели»: список GGUF-моделей из MODELS_DIR и параметры
загрузки llama-server для каждой модели.

Эти эндпоинты только читают и сохраняют параметры. Реальная загрузка идёт
через уже существующий POST /ai/models/select (с busy-проверкой: во время
генерации модель не меняется), поэтому чат этим окном не ломается.
"""
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import require_auth
from GCN import llm_client

router = APIRouter(prefix="/ai/model-settings", tags=["model-settings"])


class ParamsIn(BaseModel):
    ctx: int = Field(8192, ge=512, le=262144)
    gpu_layers: int = Field(-1, ge=-1, le=999)
    flash_attn: bool = True
    kv_cache_type: str = Field("q8_0", max_length=8)
    threads: int = Field(14, ge=1, le=128)
    main_gpu: int = Field(0, ge=0, le=15)
    tensor_split: str = Field("", max_length=64)
    parallel: int = Field(1, ge=1, le=8)


class SaveIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=400)
    params: ParamsIn


@router.get("")
async def get_model_settings(address: str = Depends(require_auth)):
    from GCN import llamacpp_manager as lm
    from GCN.config_ai import LLM_BACKEND, MODELS_DIR

    if LLM_BACKEND != "llamacpp":
        # LM Studio сам управляет загрузкой: параметры здесь не применяются
        models = await llm_client.list_models_cached()
        return {
            "backend": LLM_BACKEND,
            "editable": False,
            "note": "Параметры загрузки для LM Studio задаются в самом LM Studio.",
            "loaded": llm_client.loaded_model_cached(),
            "models": [{"id": m, "size_gb": None, "params": None,
                        "saved": False, "loaded": m == llm_client.loaded_model_cached()}
                       for m in models],
        }

    loaded = lm.manager.loaded_id()
    cur = lm.manager.params()
    items = []
    for m in lm.scan_models():
        try:
            p = asdict(lm.get_params(m["id"]))
        except ValueError:
            # битая запись в model_params.json — показываем значения по умолчанию
            p = asdict(lm.defaults())
        items.append({
            "id": m["id"],
            "size_gb": m["size_gb"],
            "params": p,
            "saved": lm.has_saved_params(m["id"]),
            "loaded": m["id"] == loaded,
        })
    return {
        "backend": LLM_BACKEND,
        "editable": True,
        "models_dir": MODELS_DIR,
        "loaded": loaded,
        "loaded_params": asdict(cur) if cur else None,
        "defaults": asdict(lm.defaults()),
        "kv_types": list(lm.KV_TYPES),
        "models": items,
    }


@router.post("/unload")
async def unload_model_endpoint(address: str = Depends(require_auth)):
    """Выгрузить текущую модель из памяти. Во время генерации — 409."""
    from fastapi.responses import JSONResponse
    from routes.ai_assistant import any_active_generation

    if (await any_active_generation())["active"]:
        return JSONResponse({"detail": "generation_busy",
                             "message": "Сейчас идёт ответ: выгрузка недоступна до его окончания"},
                            status_code=409)
    try:
        return await llm_client.unload_current_model()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось выгрузить: {e}")


@router.put("")
async def save_model_settings(body: SaveIn, address: str = Depends(require_auth)):
    """Сохранить параметры модели. Саму модель не перезагружает."""
    from GCN import llamacpp_manager as lm
    from GCN.config_ai import LLM_BACKEND

    if LLM_BACKEND != "llamacpp":
        raise HTTPException(status_code=400, detail="Параметры доступны только для LLM_BACKEND=llamacpp")
    if not any(m["id"] == body.model for m in lm.scan_models()):
        raise HTTPException(status_code=404, detail="Модель не найдена в MODELS_DIR")
    try:
        p = lm.LoadParams(**body.params.model_dump()).normalized()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    lm.save_params(body.model, p)
    return {"ok": True, "model": body.model, "params": asdict(p)}
