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

@router.get("/files/{fid}/preview")
async def preview_file(fid: str, user_id: str = Depends(require_auth)):
    """Содержимое файла для модального просмотра в чате (JSON, не бинарь).

    Отдаёт ровно то, что нужно клиенту для отрисовки:
      • image  → url для <img>
      • text   → text + language (markdown/code/text) + truncated
      • binary → download_only=true (только скачивание)
    Отдельный эндпоинт нужен потому, что GET /ai/files/{fid} принудительно
    отдаёт attachment для не-картинок (защита от XSS из .html/.svg) — браузер
    такое скачивает, а не открывает.
    """
    try:
        meta = await asyncio.to_thread(fs.get_meta, user_id, fid)
    except fs.FileError as e:
        raise HTTPException(e.status, detail=str(e))
    if meta is None:
        raise HTTPException(404, detail="Файл не найден")

    kind = meta.get("kind")
    if kind == "image":
        return {"kind": "image", "name": meta["name"],
                "url": f"/ai/files/{fid}", "size": meta["size"]}
    if kind == "binary":
        return {"kind": "binary", "name": meta["name"],
                "size": meta["size"], "download_only": True}

    # text
    try:
        info = await asyncio.to_thread(fs.read_text, user_id, fid, 0, fs.MAX_READ_CHARS)
    except fs.FileError as e:
        raise HTTPException(e.status, detail=str(e))

    # Язык по расширению — для hljs на клиенте. markdown выделен отдельно,
    # потому что для него клиент использует marked+DOMPurify, а не <pre><code>.
    import os
    ext = os.path.splitext(meta["name"])[1].lower()
    lang_map = {
        ".md": "markdown", ".markdown": "markdown", ".mdx": "markdown",
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".json": "json",
        ".html": "html", ".htm": "html",
        ".css": "css", ".scss": "scss", ".sass": "scss",
        ".xml": "xml", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
        ".bat": "dos", ".ps1": "powershell",
        ".sql": "sql",
        ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
        ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
        ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
        ".r": "r", ".lua": "lua", ".pl": "perl", ".dart": "dart",
        ".csv": "csv", ".tsv": "tsv", ".log": "text",
    }
    return {
        "kind": "text",
        "name": meta["name"],
        "language": lang_map.get(ext, "text"),
        "text": info["text"],
        "total_chars": info["total_chars"],
        "truncated": info["next_offset"] is not None,
        "mime": meta.get("mime") or "text/plain",
    }
