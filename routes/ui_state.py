"""
routes/ui_state.py — список чатов и сообщения интерфейса, общие для аккаунта.

Один и тот же аккаунт видит одни и те же чаты на компьютере, телефоне и в PWA.
Хранится в ui_state/<user_id>.json рядом с проектом. Запись защищена версией:
если состояние уже изменено с другого устройства, сервер отвечает 409, и клиент
сначала подтягивает свежую версию.

Два вида PUT-запросов:
  - полный снимок {base, sessions, messages} — при первой синхронизации;
  - дельта      {base, rev, delta:{messages:{sid:[{i,m}]}, deleted_chats, sessions}}
    — передаются только изменённые сообщения (m:null — удаление), а не весь
    контент. Дельта применяется к снапшоту точечно, поэтому сетевой трафик и
    время обработки не зависят от размера архива чатов.
"""
import base64
import binascii
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config import BASE_DIR
from dependencies import require_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/ai', tags=['ui-state'])

STATE_DIR = BASE_DIR / 'ui_state'
MEDIA_DIR = BASE_DIR / 'ui_media'   # изображения чатов: вне снапшота, по content hash
MAX_BYTES = 8 * 1024 * 1024
MAX_MSGS_PER_CHAT = 500          # защита от бесконечного роста снапшота
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # одно изображение в медиатеке (с запасом от клиентских 8 МБ)
IMAGE_EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp',
             'image/gif': '.gif'}
_SAFE_HASH = re.compile(r'^[a-f0-9]{64}$')
_SAFE_ID = re.compile(r'^[A-Za-z0-9_]{1,40}$')


class StateBody(BaseModel):
    base: int                      # версия, на которой основано это изменение
    sessions: list = []            # полный снимок (первый sync)
    messages: dict = {}            # полный снимок (первый sync)
    rev: int = 0                   # локальная версия клиента (для дельты)
    delta: dict | None = None      # частичное обновление, если не пустой снимок


def _path(user_id: str):
    if not _SAFE_ID.match(user_id):
        raise HTTPException(400, detail='Некорректный идентификатор')
    return STATE_DIR / f'{user_id}.json'


def _read(user_id: str) -> dict:
    path = _path(user_id)
    if not path.exists():
        return {'updated': 0, 'sessions': [], 'messages': {}}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        logger.error(f'Повреждён файл состояния: {path}')
        return {'updated': 0, 'sessions': [], 'messages': {}}


def _apply_delta(current: dict, delta: dict) -> dict:
    """Точечно применяет изменения поверх снапшота: только присланные сообщения.

    Формат дельты (ai-store.js): {'messages': {sid: [{'i': idx0, 'm': msg|null}]},
    'deleted_chats': [sid], 'sessions': [...] | отсутствует}. Ключевое отличие от
    полного снимка — перезаписывается только история изменённых чатов, остальные
    чаты и сообщения в запросе не участвуют.
    """
    messages = current.get('messages') or {}
    for sid, items in (delta.get('messages') or {}).items():
        if not isinstance(sid, str) or not isinstance(items, list):
            continue
        hist = list(messages.get(sid) or [])
        for it in items:
            if not isinstance(it, dict):
                continue
            i = it.get('i')
            if not isinstance(i, int) or i < 1:             # i — версия/позиция с 1
                continue
            i -= 1                                          # индекс внутри истории
            m = it.get('m')
            if m is None:                                   # tombstone — удалить
                if i < len(hist):
                    hist[i] = None
            elif i < len(hist):
                if hist[i] is None:
                    hist[i] = m
                else:
                    hist[i] = {**hist[i], **m} if isinstance(hist[i], dict) and isinstance(m, dict) else m
            else:
                hist.extend([None] * (i - len(hist)))
                hist.append(m)
        cleaned = [x for x in hist if x is not None][-MAX_MSGS_PER_CHAT:]
        if cleaned:
            messages[sid] = cleaned
        else:
            messages.pop(sid, None)

    for sid in delta.get('deleted_chats') or []:
        if isinstance(sid, str):
            messages.pop(sid, None)

    out = {'updated': current.get('updated', 0),
           'messages': messages,
           'sessions': current.get('sessions') or []}
    if isinstance(delta.get('sessions'), list):             # список чатов менялся
        out['sessions'] = delta['sessions']
    return out


# ---------------------------------------------------------------- медиатека изображений
# base64-картинки внутри сообщений раздували снапшот ui_state (и лимит 8 МБ,
# и квоты браузеров). Тяжёлые изображения хранятся отдельными файлами по
# content hash; в сообщении остаётся короткая ссылка /ai/ui-media/<hash>.png.

@router.post('/ui-media')
async def put_media(request: Request, user_id: str = Depends(require_auth)):
    """Сохраняет изображение чата, возвращает {url, reused}. Повторная загрузка
    того же файла (тот же sha256) ничего не пишет — дедупликация по контенту.
    Принимает JSON {data(base64 или data URL), mime} либо multipart с полем file."""
    import hashlib

    # вариант «сырой файл»: multipart POST с полем file (фронт читает File как ArrayBuffer)
    ctype = request.headers.get('content-type', '')
    if 'multipart/form-data' in ctype:
        form = await request.form()
        up = form.get('file')
        if up is None or not hasattr(up, 'read'):
            raise HTTPException(400, detail='Нет файла')
        raw = await up.read()
        mime = up.content_type or None
        if mime and mime.lower().startswith('image/jpg'):
            mime = 'image/jpeg'          # нестандартный, но частый MIME от камер
    else:
        try:
            body = json.loads((await request.body()).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(400, detail='Некорректный JSON')
        if not isinstance(body, dict):
            raise HTTPException(400, detail='Нет данных изображения')
        data_b64 = body.get('data')
        mime = body.get('mime')
        if not isinstance(data_b64, str) or not data_b64:
            raise HTTPException(400, detail='Нет данных изображения')
        if data_b64.startswith('data:'):
            # принимаем и data URL целиком: data:[<mime>];base64,<payload>
            head, _, data_b64 = data_b64.partition(',')
            if mime is None and ';' in head:
                mime = head.split(':', 1)[1].split(';')[0] or None
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, detail='Некорректный base64')

    if mime not in IMAGE_EXT:
        raise HTTPException(415, detail='Можно загружать только PNG/JPEG/WebP/GIF')
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(413, detail='Изображение слишком большое (макс. 10 МБ)')
    # магические байты: не довераем заявленному mime
    if not (raw[:8] == b'\x89PNG\r\n\x1a\n' or raw[:3] == b'\xff\xd8\xff'
            or raw[:4] == b'RIFF' or raw[:6] in (b'GIF87a', b'GIF89a')):
        raise HTTPException(400, detail='Файл не является изображением')

    h = hashlib.sha256(raw).hexdigest()
    ext = IMAGE_EXT[mime]
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / (h + ext)
    reused = path.exists()
    if not reused:
        tmp = path.with_suffix(path.suffix + '.tmp')
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, path)   # атомарно: полуталых файлов нет
    return {'url': f'/ai/ui-media/{h}{ext}', 'reused': reused}


@router.get('/ui-media/{name}')
async def get_media(name: str, user_id: str = Depends(require_auth)):
    from fastapi.responses import Response

    stem, dot, suffix = name.partition('.')
    # имя вида <sha256>.png; расширение сверяется с IMAGE_EXT, путь собирается
    # только из проверенных символов — directory traversal исключён structurally
    if not dot or not _SAFE_HASH.match(stem):
        raise HTTPException(400, detail='Некорректный идентификатор изображения')
    allowed = {v[1:] for v in IMAGE_EXT.values()}
    if suffix not in allowed:
        raise HTTPException(400, detail='Некорректный идентификатор изображения')
    path = MEDIA_DIR / (stem + '.' + suffix)
    if not path.is_file():
        raise HTTPException(404, detail='Изображение не найдено')
    mime = next((k for k, v in IMAGE_EXT.items() if v == '.' + suffix), 'application/octet-stream')
    data = path.read_bytes()
    # immutable: имя = sha256 контента, содержимое по этому имени не меняется
    return Response(content=data, media_type=mime,
                    headers={'Cache-Control': 'public, max-age=31536000, immutable'})


@router.get('/ui-state')
async def get_state(user_id: str = Depends(require_auth)):
    return _read(user_id)


@router.put('/ui-state')
async def put_state(body: StateBody, user_id: str = Depends(require_auth)):
    current = _read(user_id)
    if body.base != current.get('updated', 0):
        raise HTTPException(409, detail='Состояние изменено на другом устройстве')

    if body.delta is not None:
        payload = _apply_delta(current, body.delta)
    else:
        payload = {
            'updated': current.get('updated', 0),
            'sessions': body.sessions,
            'messages': body.messages,
        }
    payload['updated'] = max(int(time.time() * 1000), current.get('updated', 0) + 1)

    data = json.dumps(payload, ensure_ascii=False)
    if len(data.encode('utf-8')) > MAX_BYTES:
        raise HTTPException(413, detail='Слишком много данных. Удалите старые чаты.')

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(user_id)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(data)
    os.replace(tmp, path)            # атомарная замена: файл не остаётся наполовину записанным
    return {'updated': payload['updated']}
