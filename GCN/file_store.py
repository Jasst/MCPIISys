"""Файлы пользователя: хранение, чтение, поиск, разбор и обработка.

Модель оперирует только file_id. Путь на диске строится здесь из проверенного
id и папки пользователя, поэтому инструменты не могут выйти за пределы своих файлов.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(os.environ.get("USER_FILES_DIR")
            or Path(__file__).resolve().parent.parent / "user_files")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_READ_CHARS = 60_000
PREVIEW_CHARS = 4_000
SEARCH_LINE_LIMIT = 5_000          # длинные строки не гоняем через regex (защита от зависаний)
COMPRESS_MIN_BYTES = 600_000       # картинки меньше этого размера для модели не трогаем

_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXT = {
    ".txt", ".md", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".html", ".htm",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".log",
    ".sql", ".sh", ".bat", ".ps1", ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".r", ".env", ".gitignore", ".dockerfile", ".srt", ".vtt",
}


class FileError(Exception):
    """Ошибка, понятная пользователю и модели (уходит в ответ инструмента/HTTP)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ хранение

def _uid_dir(uid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(uid))[:64] or "anon"
    d = ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _check_id(fid: Any) -> str:
    if not isinstance(fid, str) or not _ID_RE.match(fid):
        raise FileError("Некорректный id файла")
    return fid


def _safe_name(name: Any) -> str:
    base = os.path.basename(str(name or "file")).strip() or "file"
    base = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "_", base)
    return base[:120]


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def is_text_bytes(raw: bytes) -> bool:
    return b"\x00" not in raw[:8192]


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def kind_of(name: str, raw: bytes) -> str:
    if _ext(name) in _IMAGE_EXT:
        return "image"
    return "text" if is_text_bytes(raw) else "binary"


def save_file(uid: str, name: str, raw: bytes) -> Dict[str, Any]:
    if not raw:
        raise FileError("Пустой файл")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise FileError(f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ", 413)
    name = _safe_name(name)
    fid = hashlib.sha256(raw).hexdigest()[:32]        # дедупликация по содержимому
    d = _uid_dir(uid)
    blob = d / fid
    if not blob.exists():
        _atomic_write(blob, raw)
    meta = {
        "id": fid,
        "name": name,
        "size": len(raw),
        "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "kind": kind_of(name, raw),
        "created": int(time.time()),
    }
    _atomic_write(d / f"{fid}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    return meta


def get_meta(uid: str, fid: str) -> Optional[Dict[str, Any]]:
    _check_id(fid)
    p = _uid_dir(uid) / f"{fid}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def read_bytes(uid: str, fid: str) -> Tuple[Dict[str, Any], bytes]:
    meta = get_meta(uid, fid)
    if meta is None:
        raise FileError("Файл не найден", 404)
    return meta, (_uid_dir(uid) / fid).read_bytes()


def list_files(uid: str) -> List[Dict[str, Any]]:
    out = []
    for p in _uid_dir(uid).glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda m: m.get("created", 0), reverse=True)
    return out[:200]


def delete_file(uid: str, fid: str) -> None:
    if get_meta(uid, fid) is None:
        raise FileError("Файл не найден", 404)
    d = _uid_dir(uid)
    (d / fid).unlink(missing_ok=True)
    (d / f"{fid}.json").unlink(missing_ok=True)


def _text(uid: str, fid: str) -> Tuple[Dict[str, Any], str]:
    meta, raw = read_bytes(uid, fid)
    if meta["kind"] != "text":
        raise FileError(f"«{meta['name']}» — не текстовый файл (тип: {meta['kind']})")
    return meta, decode_text(raw)


# ------------------------------------------------------------- чтение и поиск

def read_text(uid: str, fid: str, offset: int = 0, limit: int = 8000) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    limit = max(200, min(int(limit), MAX_READ_CHARS))
    offset = max(0, min(int(offset), len(text)))
    chunk = text[offset:offset + limit]
    end = offset + len(chunk)
    return {
        "id": fid, "name": meta["name"],
        "total_chars": len(text), "total_lines": text.count("\n") + 1,
        "offset": offset, "next_offset": end if end < len(text) else None,
        "text": chunk,
    }


def search_text(uid: str, fid: str, pattern: str, context: int = 2,
                max_matches: int = 30, regex: bool = True) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    if not pattern:
        raise FileError("Пустой шаблон")
    if len(pattern) > 200:
        raise FileError("Шаблон слишком длинный (макс. 200 символов)")
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    except re.error as e:
        raise FileError(f"Некорректное регулярное выражение: {e}")
    context = max(0, min(int(context), 10))
    max_matches = max(1, min(int(max_matches), 100))
    lines = text.splitlines()
    matches = []
    for i, line in enumerate(lines):
        if len(line) > SEARCH_LINE_LIMIT:
            continue
        if rx.search(line):
            lo, hi = max(0, i - context), min(len(lines), i + context + 1)
            matches.append({
                "line": i + 1,
                "text": line[:400],
                "context": [f"{j + 1}: {lines[j][:400]}" for j in range(lo, hi)],
            })
            if len(matches) >= max_matches:
                break
    return {"id": fid, "name": meta["name"], "pattern": pattern,
            "matches": matches, "truncated": len(matches) >= max_matches}


# --------------------------------------------------------------- JSON и Python

_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[-?\d+\]")


def _json_tokens(path: str) -> List[Any]:
    out: List[Any] = []
    for part in _PATH_TOKEN.findall(path or ""):
        out.append(int(part[1:-1]) if part.startswith("[") else part)
    return out


def _jtype(v: Any) -> str:
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if v is None:
        return "null"
    return "string"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… (обрезано, всего {len(s)} символов)"


def json_query(uid: str, fid: str, path: str = "", max_chars: int = 6000) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise FileError(f"Это не JSON: строка {e.lineno}, позиция {e.colno}: {e.msg}")
    cur = data
    for tok in _json_tokens(path):
        if isinstance(tok, int):
            if not isinstance(cur, list) or not (-len(cur) <= tok < len(cur)):
                raise FileError(f"Нет элемента с индексом {tok}")
            cur = cur[tok]
        else:
            if not isinstance(cur, dict) or tok not in cur:
                raise FileError(f"Нет ключа «{tok}»")
            cur = cur[tok]
    if isinstance(cur, dict):
        summary: Dict[str, Any] = {"keys": list(cur.keys())[:80], "count": len(cur)}
    elif isinstance(cur, list):
        summary = {"length": len(cur), "item_types": sorted({_jtype(x) for x in cur[:50]})}
    else:
        summary = {}
    value = json.dumps(cur, ensure_ascii=False, indent=2)
    return {"id": fid, "name": meta["name"], "path": path or "$",
            "type": _jtype(cur), "summary": summary,
            "value": _clip(value, max(200, min(int(max_chars), MAX_READ_CHARS)))}


def json_format(uid: str, fid: str, minify: bool = False) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise FileError(f"Это не JSON: строка {e.lineno}: {e.msg}")
    if minify:
        out = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        suffix = ".min.json"
    else:
        out = json.dumps(data, ensure_ascii=False, indent=2)
        suffix = ".pretty.json"
    new = save_file(uid, os.path.splitext(meta["name"])[0] + suffix, out.encode("utf-8"))
    return {"id": new["id"], "name": new["name"], "before_bytes": meta["size"],
            "after_bytes": new["size"], "url": f"/ai/files/{new['id']}"}


def _fn_info(node: ast.AST) -> Dict[str, Any]:
    return {"name": node.name, "line": node.lineno,
            "async": isinstance(node, ast.AsyncFunctionDef),
            "args": [a.arg for a in node.args.args][:12],
            "doc": (ast.get_docstring(node) or "")[:160]}


def python_outline(uid: str, fid: str) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    if _ext(meta["name"]) != ".py":
        raise FileError(f"«{meta['name']}» — не Python-файл; структуру строит только для .py")
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise FileError(f"Синтаксическая ошибка: строка {e.lineno}: {e.msg}")
    imports: List[str] = []
    classes: List[Dict[str, Any]] = []
    functions: List[Dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append("." * node.level + (node.module or ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_fn_info(node))
        elif isinstance(node, ast.ClassDef):
            methods = [_fn_info(n) for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append({"name": node.name, "line": node.lineno,
                            "bases": [ast.unparse(b) for b in node.bases][:5],
                            "methods": methods[:100]})
    return {"id": fid, "name": meta["name"], "lines": text.count("\n") + 1,
            "imports": sorted(set(imports))[:100], "classes": classes[:200],
            "functions": functions[:300]}


def text_stats(uid: str, fid: str) -> Dict[str, Any]:
    meta, text = _text(uid, fid)
    lines = text.splitlines()
    out: Dict[str, Any] = {
        "id": fid, "name": meta["name"], "bytes": meta["size"], "chars": len(text),
        "words": len(text.split()), "lines": len(lines),
        "blank_lines": sum(1 for line in lines if not line.strip()),
        "longest_line": max((len(line) for line in lines), default=0),
    }
    ext = _ext(meta["name"])
    if ext in (".csv", ".tsv") and lines:
        delim = "\t" if ext == ".tsv" else ","
        out["csv_columns"] = lines[0].split(delim)[:100]
        out["csv_rows"] = max(0, len(lines) - 1)
    return out


# -------------------------------------------------------------------- изображения

def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise FileError("Обработка изображений недоступна: на сервере не установлен Pillow "
                        "(pip install pillow)", 501)
    return Image, ImageOps


def image_info(uid: str, fid: str) -> Dict[str, Any]:
    meta, raw = read_bytes(uid, fid)
    if meta["kind"] != "image":
        raise FileError(f"«{meta['name']}» — не изображение")
    Image, _ = _pil()
    with Image.open(io.BytesIO(raw)) as im:
        return {"id": fid, "name": meta["name"], "width": im.width, "height": im.height,
                "format": im.format, "mode": im.mode, "bytes": meta["size"],
                "animated": bool(getattr(im, "is_animated", False))}


def _encode_image(raw: bytes, max_side: int, fmt: str, quality: int) -> Tuple[bytes, str, Dict[str, int]]:
    """Уменьшает по большей стороне, поворачивает по EXIF, убирает метаданные и кодирует."""
    Image, ImageOps = _pil()
    fmt = fmt.lower()
    with Image.open(io.BytesIO(raw)) as src:
        im = ImageOps.exif_transpose(src)
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt in ("jpeg", "jpg"):
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            mime = "image/jpeg"
        elif fmt == "png":
            im.save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if ("A" in im.getbands() or im.mode == "P") else "RGB")
            im.save(buf, "WEBP", quality=quality, method=4)
            mime = "image/webp"
        return buf.getvalue(), mime, {"width": im.width, "height": im.height}


_EXT_FOR_MIME = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png"}


def process_image(uid: str, fid: str, max_side: int = 1600, fmt: str = "webp",
                  quality: int = 85) -> Dict[str, Any]:
    meta, raw = read_bytes(uid, fid)
    if meta["kind"] != "image":
        raise FileError(f"«{meta['name']}» — не изображение")
    fmt = (fmt or "webp").lower()
    if fmt not in ("webp", "jpeg", "jpg", "png"):
        raise FileError("format: webp, jpeg или png")
    max_side = max(64, min(int(max_side), 4096))
    quality = max(30, min(int(quality), 95))
    data, mime, dims = _encode_image(raw, max_side, fmt, quality)
    base = os.path.splitext(meta["name"])[0]
    new = save_file(uid, f"{base}_{dims['width']}x{dims['height']}{_EXT_FOR_MIME[mime]}", data)
    return {"id": new["id"], "name": new["name"], "mime": mime, "width": dims["width"],
            "height": dims["height"], "before_bytes": meta["size"], "after_bytes": new["size"],
            "url": f"/ai/files/{new['id']}", "source_id": fid}


def compress_for_model(image_b64: str, mime: Optional[str]) -> Tuple[str, Optional[str]]:
    """Уменьшает картинку перед отправкой vision-модели: быстрее и дешевле по токенам.
    Без Pillow, для маленьких картинок или при ошибке — возвращает как было."""
    try:
        payload = image_b64.partition(",")[2] if image_b64.startswith("data:") else image_b64
        raw = base64.b64decode(payload)
        if len(raw) < COMPRESS_MIN_BYTES:
            return image_b64, mime
        data, out_mime, _ = _encode_image(raw, 1536, "webp", 85)
        if len(data) >= len(raw):
            return image_b64, mime
        return base64.b64encode(data).decode("ascii"), out_mime
    except Exception as e:  # noqa: BLE001 — сжатие не должно ломать ответ
        logger.debug("compress_for_model: пропуск (%s)", e)
        return image_b64, mime


# ------------------------------------------------------------------ контекст чата

def write_text(uid: str, name: str, content: str) -> Dict[str, Any]:
    return save_file(uid, name, content.encode("utf-8"))


def context_block(uid: str, meta: Dict[str, Any]) -> str:
    """Кусок промпта про прикреплённый файл: превью текста + подсказка, каким инструментом читать дальше."""
    head = f"[{meta['name']} · id={meta['id']} · {meta['size']} байт · {meta['kind']}]"
    if meta["kind"] == "text":
        _, raw = read_bytes(uid, meta["id"])
        text = decode_text(raw)
        if len(text) > PREVIEW_CHARS:
            body = (text[:PREVIEW_CHARS]
                    + f"\n… (показано {PREVIEW_CHARS} из {len(text)} символов; "
                      f"остальное — files_read с file_id={meta['id']})")
        else:
            body = text
        return f"{head}\n```\n{body}\n```"
    if meta["kind"] == "image":
        return f"{head} — изображение; для уменьшения или конвертации: files_image_compress"
    return f"{head} — бинарный файл: доступны только метаданные"
