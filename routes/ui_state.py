"""
routes/ui_state.py — список чатов и сообщения интерфейса, общие для аккаунта.

Один и тот же аккаунт видит одни и те же чаты на компьютере, телефоне и в PWA.
Хранится в ui_state/<user_id>.json рядом с проектом. Запись защищена версией:
если состояние уже изменено с другого устройства, сервер отвечает 409, и клиент
сначала подтягивает свежую версию.
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
_SAFE_ID = re.compile(r'^[A-Za-z0-9_]{1,40}$')


class StateBody(BaseModel):
    base: int                      # версия, на которой основано это изменение
    sessions: list
    messages: dict


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


@router.get('/ui-state')
async def get_state(user_id: str = Depends(require_auth)):
    return _read(user_id)


@router.put('/ui-state')
async def put_state(body: StateBody, user_id: str = Depends(require_auth)):
    current = _read(user_id)
    if body.base != current.get('updated', 0):
        raise HTTPException(409, detail='Состояние изменено на другом устройстве')

    payload = {
        'updated': int(time.time() * 1000),
        'sessions': body.sessions,
        'messages': body.messages,
    }
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
