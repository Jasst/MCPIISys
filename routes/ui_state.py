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
import json
import logging
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import BASE_DIR
from dependencies import require_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/ai', tags=['ui-state'])

STATE_DIR = BASE_DIR / 'ui_state'
MAX_BYTES = 8 * 1024 * 1024
MAX_MSGS_PER_CHAT = 500          # защита от бесконечного роста снапшота
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
