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
import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
from collections import OrderedDict
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
_TOMB_TTL_MS = 30 * 86400 * 1000     # сервер помнит удаления чатов 30 дней: дольше любого
                                     # «засыпания» устройства без синхронизации
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


# ------------------------------------------------------------------ генерационный лок
# Единый механизм генерации для всех устройств аккаунта. Пока один клиент пишет
# ответ (фоновая SSE-задача на сервере живёт даже после закрытия вкладки),
# PUT /ui-state от других устройств отклоняется с 429 — иначе они перезатёрли бы
# чат черновиком/обрывком, и итоговый ответ превратился бы в «кашу».
# Лок держится в памяти процесса (TTL-страховка на случай зависшей задачи);
# GET /gen-status — тот же ключ, что использует /ai/chat/attach, поэтому статус
# корректен даже после рестарта сервера (просто активные генерации при этом сбросятся).

_ACTIVE_TTL_MS = 10 * 60 * 1000        # предельная длительность одной генерации
_ACTIVE_MAX = 500                       # защита от разрастания словаря
_active_generations: "OrderedDict[str, float]" = OrderedDict()   # uid -> deadline_ms
_active_lock = asyncio.Lock()


def _active_deadline(uid: str) -> float:
    """ms до снятия лока; <=0 — активной генерации нет (просроченный ключ чистим)."""
    d = _active_generations.get(uid, 0.0) - time.time() * 1000
    if d <= 0:
        _active_generations.pop(uid, None)
    return d


def _active_touch(uid: str) -> None:
    now = time.time() * 1000
    _active_generations[uid] = now + _ACTIVE_TTL_MS
    _active_generations.move_to_end(uid)
    while len(_active_generations) > _ACTIVE_MAX:
        _active_generations.popitem(last=False)


def _active_release(uid: str) -> None:
    _active_generations.pop(uid, None)


@router.get('/gen-status')
async def gen_status(user_id: str = Depends(require_auth)):
    """Есть ли у аккаунта активная генерация и можно ли данному устройству
    писать состояние. active — по серверному локу; may_write — активна ли
    генерация ИМЕННО этого устройства (совпадает с флагом X-Client-Gen его PUT).
    Клиенты, у которых may_write=false и active=true, ждут окончания ответа."""
    async with _active_lock:
        remaining = _active_deadline(user_id)
    return {'active': remaining > 0, 'may_write': remaining > 0, 'ttl_ms': max(0, int(remaining))}


@router.post('/gen-lock')
async def gen_lock(body: dict, user_id: str = Depends(require_auth)):
    """Занять/продлить генерационный лок перед стартом POST /ai/chat.
    {client: true} — лок берёт это же устройство (его PUT разрешён);
    {client: false} — генерацию запустило другое устройство, свой лок не трогаем.
    Ответ: {ok, held_by_me}. ok=false — чужая генерация ещё пишется, новый
    запрос начинать нельзя (клиент показывает «дождитесь окончания»)."""
    mine = isinstance(body, dict) and bool(body.get('client'))
    async with _active_lock:
        remaining = _active_deadline(user_id)
        if remaining > 0 and not mine:
            return {'ok': False, 'held_by_me': False, 'ttl_ms': int(remaining)}
        if mine:
            _active_touch(user_id)
        return {'ok': True, 'held_by_me': mine, 'ttl_ms': _ACTIVE_TTL_MS if mine else int(remaining)}


@router.delete('/gen-lock')
async def gen_unlock(body: dict, user_id: str = Depends(require_auth)):
    """Снять лок по окончании генерации. client:false (чужая генерация) — no-op,
    чтобы «умный» клиент не снял чужую защиту."""
    if isinstance(body, dict) and body.get('client'):
        async with _active_lock:
            _active_release(user_id)
    return {'released': True}


def _path(user_id: str):
    if not _SAFE_ID.match(user_id):
        raise HTTPException(400, detail='Некорректный идентификатор')
    return STATE_DIR / f'{user_id}.json'


def _read(user_id: str) -> dict:
    path = _path(user_id)
    if not path.exists():
        return {'updated': 0, 'sessions': [], 'messages': {}, 'deleted_chats': []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            st = json.load(f)
        # снапшоты до tombstone-реестра: добавляем пустой — merge-логика сама
        # создаёт ключ при первом удалении
        if not isinstance(st.get('deleted_chats'), list):
            st['deleted_chats'] = []
        return st
    except (OSError, ValueError):
        logger.error(f'Повреждён файл состояния: {path}')
        return {'updated': 0, 'sessions': [], 'messages': {}, 'deleted_chats': []}


def _msg_key(m) -> str | None:
    """Устойчивый идентификатор сообщения для идемпотентного слияния.

    Клиент задаёт id каждому новому сообщению (uid на устройстве-авторе), и
    при переключении/повторном открытии чата история строится из тех же объектов
    — дубль одного и того же сообщения физически получает один ключ, поэтому
    слияние схлопывает его в одну запись вместо второй копии. Сообщения без id
    (старые данные, legacy-снимки) получают детерминированный content-ключ
    role+time+text: два устройства, создавшие одинаковый объект, дают одинаковый
    ключ и тоже схлопываются."""
    if not isinstance(m, dict):
        return None
    mid = m.get('id')
    if isinstance(mid, str) and mid:
        return f'id:{mid}'
    t = m.get('time')
    if isinstance(t, (int, float)):
        return f'ct:{m.get("role", "")}|{int(t)}|{(m.get("text") or "")[:80]}'
    return None


def _merge_msgs(base: list, incoming: list) -> list:
    """Слияние полного снимка чата поверх серверной истории без дублей.

    Используется PUT с полным состоянием (первый sync клиента или legacy):
    серверная история НЕ затирается молча — сообщения сопоставляются по ключам,
    совпадения мержатся полями (пришедшее приоритетнее), новые вставляются в
    свою позицию со сдвигом при коллизии. Идемпотентно: повторный тот же снимок
    ничего не добавляет. Это закрывает «чат копируется сам себя» — раньше
    полный снимок перезаписывал историю как есть, а дельты без ключей плодили
    вторые копии тех же сообщений."""
    result = [dict(x) if isinstance(x, dict) else x for x in base if x is not None]
    key_to_idx: dict[str, int] = {}
    used_keys: set[str] = set()
    for i, msg in enumerate(result):
        k = _msg_key(msg)
        if k and k not in key_to_idx:
            key_to_idx[k] = i
            used_keys.add(k)

    for pos, item in enumerate(incoming):
        if not isinstance(item, dict):
            continue
        key = _msg_key(item)
        if key and key in key_to_idx:
            cur = result[key_to_idx[key]]
            if isinstance(cur, dict):
                cur.update(item)
            continue
        j = min(pos, len(result))
        while j < len(result):
            ek = _msg_key(result[j])
            if (not key and not ek) or (key and ek and key != ek and ek not in used_keys):
                break
            j += 1
        result.insert(j, dict(item))
        if key:
            key_to_idx[key] = j
            used_keys.add(key)
        for p in range(j + 1, len(result)):
            pk = _msg_key(result[p])
            if pk:
                key_to_idx[pk] = p
                if not key or pk != key:
                    used_keys.add(pk)
    return result[-MAX_MSGS_PER_CHAT:]


def _merge_sessions(base: list, incoming: list) -> list:
    """Слияние списков чатов по id: last-write-wins по полю updated.

    Пришедший список замещает записи тех же id (переименование, удаление из
    списка), но НЕ затирает чаты, о которых в дельте ничего нет — иначе любое
    устройство со старым списком «воскрешало» бы удалённые на другом чаты или,
    наоборот, теряло новые. Порядок — по updated (свежие сверху)."""
    by_id = {s.get('id'): s for s in base if isinstance(s, dict) and s.get('id')}
    for s in incoming:
        if not isinstance(s, dict) or not s.get('id'):
            continue
        by_id[s['id']] = s
    merged = list(by_id.values())
    merged.sort(key=lambda x: x.get('updated') or x.get('created') or 0, reverse=True)
    return merged


def _apply_delta(current: dict, delta: dict) -> tuple[dict, list]:
    """Точечно применяет изменения поверх снапшота: только присланные сообщения.

    Формат дельты (ai-store.js): {'messages': {sid: [{'i': idx0, 'm': msg|null}]},
    'deleted_chats': [sid], 'sessions': [...] | отсутствует}. Ключевое отличие от
    полного снимка — перезаписывается только история изменённых чатов, остальные
    чаты и сообщения в запросе не участвуют.

    Каждое пришедшее сообщение сопоставляется с серверной историей по устойчивому
    id (или детерминированному content-ключу role+time+text). Совпадение — патч
    существующего сообщения; нового ключа нет — вставка в исходную позицию со
    сдвигом при коллизии. Это делает слияние идемпотентным: повторная доставка
    той же дельты не создаёт дублей, а параллельные записи разных устройств не
    превращают историю в кашу. Полностью удалённые чаты (tombstone) игнорируются,
    если на сервере уже есть более свежая история — так «припозднившаяся» запись
    не затирает финальный ответ другого устройства.

    Возвращает (снапшот, conflicts). conflicts — позиции, которые не удалось
    безопасно применить; сервер НИКОГДА не подгоняет историю под клиента
    (раньше extend([None]) создавал null-дыры при рассинхроне позиций) —
    клиент получает конфликты и перестраивает свою историю по серверной.
    """
    messages = current.get('messages') or {}
    conflicts: list = []
    # tombstone-ы из дельты вырезаем ДО применения сообщений: пришедшая следом
    # история того же чата не должна воскресить удалённую сессию (история без
    # записи в sessions клиенту не видна — «воскресить» её merge уже не сможет)
    deleted = [sid for sid in (delta.get('deleted_chats') or []) if isinstance(sid, str)]
    for sid in set(deleted) | {d for d in (delta.get('_allDeleted') or []) if isinstance(d, str)}:
        messages.pop(sid, None)
    for sid, items in (delta.get('messages') or {}).items():
        if not isinstance(sid, str) or not isinstance(items, list):
            continue
        if sid in deleted:                                  # удалённый в этой же дельте
            continue                                        # чат не пересоздаём
        hist = [x for x in (messages.get(sid) or []) if x is not None]
        changed = False
        for it in items:
            if not isinstance(it, dict):
                continue
            i = it.get('i')
            if not isinstance(i, int) or i < 1:             # i — позиция с 1
                continue
            m = it.get('m')
            if m is None:                                   # tombstone — удалить
                key = _msg_key(it)                          # id удалённого сообщения
                if key:
                    before = len(hist)
                    hist = [x for x in hist if _msg_key(x) != key]
                    changed = len(hist) != before
                elif i <= len(hist):
                    hist[i - 1] = None
                    changed = True
                continue
            key = _msg_key(m)
            at = next((p for p, x in enumerate(hist) if x is not None and _msg_key(x) == key),
                      None) if key else None
            if at is not None:                              # тот же объект — патч поверх
                old = hist[at]
                hist[at] = {**old, **m} if isinstance(old, dict) and isinstance(m, dict) else m
                changed = True
            elif key is None and 1 <= i <= len(hist) and hist[i - 1] is None:
                hist[i - 1] = m                           # заполнение ранее удалённой позиции
                changed = True
            elif key is None and i > len(hist):
                conflicts.append({'sid': sid, 'i': i})      # позиции нет — не дописываем вслепую
                continue
            else:                                           # новое сообщение — вставка по ключу
                j = min(i - 1, len(hist))
                while j < len(hist):
                    ek = _msg_key(hist[j])
                    if (not key and not ek) or (key and ek and ek != key):
                        break
                    j += 1
                hist.insert(j, m)
                changed = True
        cleaned = [x for x in hist if x is not None][-MAX_MSGS_PER_CHAT:]
        if cleaned:
            messages[sid] = cleaned
        elif changed:
            messages.pop(sid, None)

    deleted = [sid for sid in (delta.get('deleted_chats') or []) if isinstance(sid, str)]
    # реестр удалений аккаунта: живёт в снапшоте и пополняется каждым PUT.
    # Нужен потому, что у каждого устройства свой 7-дневный окно tombstone'ов;
    # сервер помнит все удаления и вырезает чат из ЛЮБОГО merge — иначе
    # устаревший снимок третьего устройства воскрешает удалённый чат.
    reg = current.get('deleted_chats') or []
    if not isinstance(reg, list):
        reg = []
    now_ms = int(time.time() * 1000)
    reg = [r for r in reg if isinstance(r, dict) and isinstance(r.get('sid'), str)
           and now_ms - int(r.get('at') or 0) < _TOMB_TTL_MS]
    known = {r['sid'] for r in reg}
    for sid in set(deleted) | {d for d in (delta.get('_allDeleted') or []) if isinstance(d, str)}:
        messages.pop(sid, None)                             # историю удаляем всегда:
        if sid not in known:                                # без записи в sessions она
            reg.append({'sid': sid, 'at': now_ms})          # не видима никому и merge
            known.add(sid)                                  # уже не «воскресит»
    out = {'updated': current.get('updated', 0),
           'messages': messages,
           'sessions': current.get('sessions') or [],
           'deleted_chats': reg}
    gone = known                                            # tombstone побеждает stale-снимок:
    if isinstance(delta.get('sessions'), list):             # список чатов менялся
        incoming_ids = {s.get('id') for s in delta['sessions']
                        if isinstance(s, dict) and s.get('id')}
        merged = _merge_sessions(current.get('sessions') or [], delta['sessions'])
        # удаление — факт: исключаем tombstone-ы, отсутствующие в пришедшем
        # списке явно. Раньше приоритет updated из дельты над удалением
        # позволял любому устройству со чуть более свеzym updated воскресить
        # чат (погрешность часов между ПК/телефоном это гарантирует).
        # Повторное создание чата даёт новый id — старые tombstone его не касаются.
        merged = [s for s in merged if s.get('id') not in gone or s.get('id') in incoming_ids]
        out['sessions'] = merged
    else:                                                   # списка в дельте нет —
        out['sessions'] = [s for s in out['sessions'] if s.get('id') not in gone]
    return out, conflicts


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
async def put_state(body: StateBody, request: Request, user_id: str = Depends(require_auth)):
    # Генерационный лок: пока ответ пишется, состояние аккаунта имеет право
    # менять только то устройство, которое эту генерацию ведёт (оно же и видит
    # финальный текст через SSE/attach). Остальные получают 429 и ждут — иначе
    # их локальные черновики долетели бы до сервера раньше финального commit'а
    # генерирующего клиента и «каша» была бы неизбежна. Флаг X-Client-Gen
    # ставит клиент перед POST /ai/chat; если он не прислан (старый клиент),
    # считаем генерацию чужой — защита работает по умолчанию.
    client_gen = request.headers.get('x-client-gen', '') in ('1', 'true')
    async with _active_lock:
        remaining = _active_deadline(user_id)
        if remaining > 0 and not client_gen:
            raise HTTPException(429, detail='На другом устройстве идёт генерация — синхронизация приостановлена')

    current = _read(user_id)
    if body.base != current.get('updated', 0):
        raise HTTPException(409, detail='Состояние изменено на другом устройстве')

    conflicts: list = []
    if body.delta is not None:
        payload, conflicts = _apply_delta(current, body.delta)
    else:
        # Полный снимок (первый sync / legacy-клиент): НЕ затираем серверную
        # историю молча — мержим по устойчивым ключам сообщений. Если клиент
        # прислал ту же историю, что уже на сервере (например, открыл и закрыл
        # чат повторно), результат идентичен исходному — без дублей.
        merged_msgs = {}
        server_msgs = current.get('messages') or {}
        # tombstone-реестр клиента: чаты, удалённые на любом из его устройств.
        # Полный снимок без них перезаписывал историю «удалённого» чата и он
        # возвращался в список при первом же mergeFull/409-разрешении.
        dels_raw = (body.delta or {}).get('deleted_chats') if isinstance(body.delta, dict) else None
        dels = {d for d in (dels_raw or []) if isinstance(d, str)}
        for sid, hist in (body.messages or {}).items():
            if sid in dels:                                 # удалённый чат — не оживляем
                continue
            if isinstance(hist, list):
                merged_msgs[sid] = _merge_msgs(server_msgs.get(sid) or [], hist)
        for sid, hist in server_msgs.items():
            if sid not in merged_msgs and sid not in dels:
                merged_msgs[sid] = hist
        sess_merged = _merge_sessions(current.get('sessions') or [], body.sessions)
        dels |= {r['sid'] for r in (current.get('deleted_chats') or [])
                 if isinstance(r, dict) and isinstance(r.get('sid'), str)}
        reg = [r for r in (current.get('deleted_chats') or [])
               if isinstance(r, dict) and isinstance(r.get('sid'), str)]
        known = {r['sid'] for r in reg}
        now_ms = int(time.time() * 1000)
        for d in sorted(dels):                              # новые tombstone-ы в реестр:
            if d not in known:                              # сервер помнит удаления и для
                reg.append({'sid': d, 'at': now_ms})        # mergeFull других устройств
        payload = {
            'updated': current.get('updated', 0),
            'sessions': [s for s in sess_merged if s.get('id') not in dels],
            'messages': merged_msgs,
            'deleted_chats': reg,
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
    out = {'updated': payload['updated']}
    if conflicts:
        out['conflicts'] = conflicts   # клиент перестроит историю чата по серверной версии
    return out
