"""Файлы пользователя для чата: загрузка, список, скачивание, удаление."""
import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from dependencies import require_auth
from GCN import file_store as fs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai")


@router.post("/files")
async def upload_file(file: UploadFile = File(...), user_id: str = Depends(require_auth)):
    raw = await file.read(fs.MAX_UPLOAD_BYTES + 1)
    try:
        meta = await asyncio.to_thread(fs.save_file, user_id, file.filename or "file", raw)
    except fs.FileError as e:
        raise HTTPException(e.status, detail=str(e))
    return {**meta, "url": f"/ai/files/{meta['id']}"}


@router.get("/files")
async def list_user_files(user_id: str = Depends(require_auth)):
    return {"files": await asyncio.to_thread(fs.list_files, user_id)}


@router.get("/files/{fid}")
async def download_file(fid: str, user_id: str = Depends(require_auth)):
    try:
        meta, raw = await asyncio.to_thread(fs.read_bytes, user_id, fid)
    except fs.FileError as e:
        raise HTTPException(e.status, detail=str(e))
    # Картинки показываем inline; остальное — только как скачивание, чтобы html/svg
    # из чужих файлов не выполнялись в origin приложения (защита от XSS).
    is_image = meta["kind"] == "image" and meta["mime"].startswith("image/")
    disposition = "inline" if is_image else "attachment"
    mime = meta["mime"] if is_image else "application/octet-stream"
    return Response(
        content=raw,
        media_type=mime,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(meta['name'])}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete("/files/{fid}")
async def delete_user_file(fid: str, user_id: str = Depends(require_auth)):
    try:
        await asyncio.to_thread(fs.delete_file, user_id, fid)
    except fs.FileError as e:
        raise HTTPException(e.status, detail=str(e))
    return {"ok": True}
