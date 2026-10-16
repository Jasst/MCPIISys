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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
# чат черновиком/обрывком, и итоговый ответ превратился бы в «кашу». Остальные
# устройства при этом НЕ слепые: они подключаются к той же живой задаче через
# GET /ai/chat/attach?watch=1 (только подписка на буфер, новый запуск никогда)
# и видят печать ответа в реальном времени; право финальной записи остаётся
# у владельца лока.
# Лок держится в памяти процесса (TTL-страховка на случай зависшей задачи);
# GET /gen-status — тот же ключ, что использует /ai/chat/attach, поэтому статус
# корректен даже после рестарта сервера (просто активные генерации при этом сбросятся).
#
# Владелец лока — НЕ аккаунт, а конкретное устройство (cid — стабильный id
# клиента из localStorage). Это позволяет второму устройству БЕЗОПАСНО
# подключаться к живой генерации через /ai/chat/attach: сервер знает, кому
# принадлежит ответ, и отличает подписчика от постороннего писателя.

_ACTIVE_TTL_MS = 10 * 60 * 1000        # предельная длительность одной генерации
_ACTIVE_MAX = 500                       # защита от разрастания словаря
_active_generations: "OrderedDict[str, dict]" = OrderedDict()   # uid -> {deadline, cid}
_active_lock = asyncio.Lock()


def _active_state(uid: str) -> dict | None:
    """Записи о локактивных генерациях аккаунта; просроченный ключ чистим."""
    g = _active_generations.get(uid)
    if g is None:
        return None
    if g['deadline'] - time.time() * 1000 <= 0:
        _active_generations.pop(uid, None)
        return None
    return g


def _active_touch(uid: str, cid: str) -> dict:
    now = time.time() * 1000
    g = {'deadline': now + _ACTIVE_TTL_MS, 'cid': cid or ''}
    _active_generations[uid] = g
    _active_generations.move_to_end(uid)
    while len(_active_generations) > _ACTIVE_MAX:
        _active_generations.popitem(last=False)
    return g


def _active_release(uid: str, cid: str | None = None) -> None:
    """Снятие лока. Если указан cid — снимает только владелец (защита от
    опоздавшего release'а другого устройства, сбросившего бы живой лок)."""
    g = _active_generations.get(uid)
    if g is None:
        return
    if cid is not None and g.get('cid') != cid:
        return
    _active_generations.pop(uid, None)


@router.get('/gen-status')
async def gen_status(cid: str = '', user_id: str = Depends(require_auth)):
    """Есть ли у аккаунта активная генерация и кто её ведёт.
    active      — пишет ли кто-то ответ прямо сейчас;
    mine        — эта генерация ведёт ИМЕННО это устройство (сверено по cid);
    may_watch   — можно ли подключиться к потоку (/chat/attach): своё или чужое,
                  но ТОЛЬКО живая задача сервера — иначе второй параллельный
                  запуск перезатёр бы историю (это и была «каша»);
    may_write   — может ли это устройство писать PUT /ui-state без флага
                  X-Client-Gen (активной генерации нет вообще).
    Клиенты, у которых active && !mine && !may_watch, ждут финального commit'а."""
    async with _active_lock:
        g = _active_state(user_id)
    if g is None:
        return {'active': False, 'mine': False, 'may_watch': False,
                'may_write': True, 'ttl_ms': 0}
    mine = bool(cid) and g['cid'] == cid
    return {'active': True, 'mine': mine, 'may_watch': True, 'may_write': False,
            'ttl_ms': int(g['deadline'] - time.time() * 1000)}


@router.post('/gen-lock')
async def gen_lock(body: dict, user_id: str = Depends(require_auth)):
    """Занять/продлить генерационный лок перед стартом POST /ai/chat.
    {client: true, cid} — продление/захват СВОЕГО лока (это устройство будет
    вести генерацию и имеет право писать состояние во время неё);
    {client: false, cid} — проверка: не мешает ли чужая ЖИВАЯ генерация новому
    запросу. Свой осиротевший флаг (вкладка закрылась в разгар ответа) сюда
    не приходит — клиент шлёт client:false, и старый лок перезахватывается,
    а не блокирует навсегда.
    Ответ: {ok, held_by_me}. ok=false — чужая генерация ещё пишется, новый
    запрос начинать нельзя (клиент показывает «дождитесь окончания»)."""
    mine = isinstance(body, dict) and bool(body.get('client'))
    cid = str(body.get('cid') or '') if isinstance(body, dict) else ''
    async with _active_lock:
        g = _active_state(user_id)
        if g is not None and not mine:
            return {'ok': False, 'held_by_me': g['cid'] == cid,
                    'ttl_ms': int(g['deadline'] - time.time() * 1000)}
        if mine:
            g = _active_touch(user_id, cid)
        # g здесь может быть None: свободный лок без захвата (client:false) —
        # раньше это давало TypeError ('NoneType' object is not subscriptable)
        return {'ok': True, 'held_by_me': mine,
                'ttl_ms': int(g['deadline'] - time.time() * 1000) if g else 0}


@router.delete('/gen-lock')
async def gen_unlock(body: dict, user_id: str = Depends(require_auth)):
    """Снять лок по окончании генерации. client:false (чужая генерация) — no-op,
    чтобы «умный» клиент не снял чужую защиту. cid сверяется дополнительно:
    даже с client:true чужой release невозможен."""
    if isinstance(body, dict) and body.get('client'):
        async with _active_lock:
            _active_release(user_id, str(body.get('cid') or ''))
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


# Кэш прочитанного снапшота на процесс: GET /ui-state и GET /ui-state/version
# вызываются каждым устройством каждые несколько секунд; перечитывать с диска
# многосоткилобайтный JSON ради одной версии — чистая расточительность.
# Запись (_write_state) сбрасывает кэш немедленно, поэтому stale-данные не
# отдаются: инвалидация по path при любой записи в тот же файл.
_STATE_CACHE: dict[str, tuple[int, float, dict]] = {}   # path -> (mtime_ns, size, data)
_STATE_CACHE_TTL = 2.0          # защита от записи другим процессом мимо кэша
_STATE_CACHE_MAX = 256


def _cached_read(user_id: str) -> dict:
    key = str(_path(user_id))
    now = time.time()
    hit = _STATE_CACHE.get(key)
    if hit and now - hit[1] < _STATE_CACHE_TTL:
        return hit[2]
    st = _read(user_id)
    try:
        stamp = os.stat(key).st_mtime_ns
        if hit and hit[0] == stamp:          # файл не менялся — держим прежний объект
            _STATE_CACHE[key] = (stamp, now, hit[2])
            return hit[2]
    except OSError:
        stamp = 0
    if len(_STATE_CACHE) >= _STATE_CACHE_MAX:
        _STATE_CACHE.clear()
    _STATE_CACHE[key] = (stamp, now, st)
    return st


def _write_state(user_id: str, payload: dict) -> None:
    """Атомарная запись снапшота + инвалидация кэша прочтений.

    Раньше запись была встроена в put_state; вынесение нужно, чтобы ни один
    путь обновления состояния не мог оставить в _STATE_CACHE устаревший снимок."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(user_id)
    tmp = path.with_suffix('.tmp')
    data = json.dumps(payload, ensure_ascii=False)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(data)
    os.replace(tmp, path)            # атомарная замена: файл не остаётся наполовину записанным
    _STATE_CACHE.pop(str(path), None)


def _msg_key(m) -> str | None:
    """Устойчивый идентификатор сообщения для идемпотентного слияния.

    Клиент задаёт id каждому новому сообщению (uid на устройстве-авторе), и
    при переключении/повторном открытии чата история строится из тех же объектов
    — дубль одного и того же сообщения физически получает один ключ, поэтому
    слияние схлопывает его в одну запись вместо второй копии. Сообщения без id
    (старые данные, legacy-снимки) получают детерминированный content-ключ
    role+text(+time если есть): два устройства, создавшие одинаковый объект,
    дают одинаковый ключ и тоже схлопываются. Ключ обязателен и для сообщений
    без времени — иначе пересланный повторно полный снимок удваивал историю
    («чат копируется сам себя» после каждого sync)."""
    if not isinstance(m, dict):
        return None
    mid = m.get('id')
    if isinstance(mid, str) and mid:
        return f'id:{mid}'
    t = m.get('time')
    txt = (m.get('text') or '')[:80]
    if isinstance(t, (int, float)):
        return f'ct:{m.get("role", "")}|{int(t)}|{txt}'
    return f'cx:{m.get("role", "")}|{txt}'


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
    # схлопываем уже существующие в base дубли (наследие прежнего удваивающего
    # merge): один и тот же ключ — одна запись, пришедшие позже поля приоритетны
    deduped: list = []
    first_idx: dict[str, int] = {}
    for msg in result:
        k = _msg_key(msg) if isinstance(msg, dict) else None
        if k and k in first_idx:
            cur = deduped[first_idx[k]]
            if isinstance(cur, dict) and isinstance(msg, dict):
                cur.update(msg)
            continue
        if k:
            first_idx[k] = len(deduped)
        deduped.append(msg)
    result = deduped

    key_to_idx: dict[str, int] = {}
    used_keys: set[str] = set()
    for i, msg in enumerate(result):
        k = _msg_key(msg)
        if k and k not in key_to_idx:
            key_to_idx[k] = i
            used_keys.add(k)

    # ключи серверной истории: повторная вставка сообщения с уже известным
    # ключом (пересланный полный снимок, дошедшая позже дельта того же PUT) —
    # идемпотентна: схлопываем в существующее, а не плодим вторую копию.
    # Раньше при отсутствии ключа у base-сообщений каждое такое сообщение
    # вставлялось заново и история удваивалась («чат копируется сам себя»).
    seen_keys = set(key_to_idx)
    for pos, item in enumerate(incoming):
        if not isinstance(item, dict):
            continue
        key = _msg_key(item)
        if key and key in seen_keys and key not in used_keys:
            cur = result[key_to_idx[key]]
            if isinstance(cur, dict):
                cur.update(item)
            continue
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


def _merge_sessions(base: list, incoming: list, partial: bool = False) -> list:
    """Слияние списков чатов по id: last-write-wins по полю updated.

    Пришедший список замещает записи тех же id (переименование, удаление из
    списка), но НЕ затирает чаты, о которых в дельте ничего нет — иначе любое
    устройство со старым списком «воскрешало» бы удалённые на другом чаты или,
    наоборот, теряло новые. Порядок — как в присланном списке (пользовательский),
    незнакомые серверу чаты — в начало."""
    by_id = {s.get('id'): s for s in base if isinstance(s, dict) and s.get('id')}
    base_ids = set(by_id)
    for s in incoming:
        if not isinstance(s, dict) or not s.get('id'):
            continue
        # тот же объект с другим updated (например, локальный Date.now() устройства
        # отстаёт от серверных меток): не понижаем свежую запись — поля берём из
        # пришедшей, а updated оставляем максимальным из двух. Иначе переименование
        # с телефона с отстающими часами «омолаживало» бы чат и merge на других
        # устройствах считал бы его устаревшим.
        prev = by_id.get(s['id'])
        merged = dict(s)
        if isinstance(prev, dict):
            try:
                a, b = float(prev.get('updated') or 0), float(s.get('updated') or 0)
                if b > a:
                    merged['updated'] = s.get('updated')
                elif b >= a:
                    merged['updated'] = prev.get('updated')
            except (TypeError, ValueError):
                pass
        by_id[s['id']] = merged
    # порядок — как в ПРИСЛАННОМ списке (это пользовательский порядок: ручная
    # сортировка, архив). Чаты, о которых в присланном списке ничего нет,
    # дописываются СХЛОПНУТЫМИ позади — раньше их вставляли сырыми копиями,
    # и устройство B получало дубли записей чатов устройства A («чат
    # копируется» в списке после sync). Повторная доставка того же снимка
    # идемпотентна: состав и порядок не меняются.
    # В порядок входят ВСЕ пришедшие id (в т.ч. незнакомые серверу — новые чаты),
    # иначе новый чат устройства A отбрасывался merge'ом и никогда не появлялся
    # на B («на новом устройстве ничего не появляется»).
    # «Хвост» из base дописывается только когда присланный список НЕ полный
    # (_partial: дельта содержит лишь изменённые записи) — иначе собственные
    # старые записи вставлялись перед пользовательским порядком отправителя и
    # перестановка чатов терялась при каждом sync.
    incoming_ids = [s['id'] for s in incoming if isinstance(s, dict) and s.get('id')]
    seen = set(incoming_ids)
    if partial:
        tail = [s.get('id') for s in base if isinstance(s, dict) and s.get('id')
                and s.get('id') not in seen]
        order = incoming_ids + tail
    else:
        order = incoming_ids
    out = [by_id[i] for i in dict.fromkeys(order)]
    legacy = [s for s in base if isinstance(s, dict) and not s.get('id')]
    return out + legacy


def _delta_is_noop(delta: dict) -> bool:
    """Дельта без реальных изменений: пустые messages/deleted_chats/sessions.

    Такое приходит на каждом фоновом тике синхронизации клиента, когда локально
    ничего не менялось (pushState обычно отсекает это сам, но гонка между
    устройствами и старые клиенты присылают пустую дельту на сервер). Если на
    такую дельту поднимать updated, каждое устройство при опросе получало
    «новую версию», тянула полный снимок и перерисовывало весь чат («дёрганье»),
    а параллельные записи запускали каскад конфликтов 409: PUT соседа считался
    устаревшим без всякой причины. Пустую дельту сервер принимает как успех,
    НО состояние не трогает и не будит SSE-подписчиков."""
    if not isinstance(delta, dict):
        return True
    msgs = delta.get('messages')
    if isinstance(msgs, dict):
        for items in msgs.values():
            if isinstance(items, list) and len(items) > 0:
                return False
    elif msgs:
        return False
    for key in ('deleted_chats', '_allDeleted'):
        v = delta.get(key)
        if isinstance(v, list) and len(v) > 0:
            return False
        elif v and not isinstance(v, list):
            return False
    sess = delta.get('sessions')
    if isinstance(sess, list) and len(sess) > 0:
        return False
    elif sess and not isinstance(sess, list):
        return False
    return True


def _snapshot_is_noop(current: dict, payload: dict) -> bool:
    """Сравнивает значимую часть смерженного снимка с текущей версией.

    Полный PUT (первый sync / legacy-клиент), который принёс ровно то же
    содержимое, что уже лежит на сервере (открыл и закрыл чат, повторный sync
    после 409), тоже не должен двигать updated — иначе клиент видит «изменение»,
    перерисовывает интерфейс и дергает другие устройства через SSE."""
    def sig(st: dict) -> str:
        # Сравниваем deleted_chats по sid, БЕЗ timestamp. Ранее реестр был
        # полностью исключён из сигнатуры — и новый tombstone, пришедший без
        # других изменений (sessions/messages те же), не двигал updated,
        # запись на диск не выполнялась, и удалённый чат «воскресал» при
        # следующем полном снимке с другого устройства.
        # Timestamp намеренно не учитывается: повторная доставка того же
        # _allDeleted не должна считаться изменением (at не переписывается
        # для уже известных sid, но лучше не завязываться на это).
        dels = sorted(
            d['sid'] for d in (st.get('deleted_chats') or [])
            if isinstance(d, dict) and isinstance(d.get('sid'), str)
        )
        return json.dumps([st.get('sessions') or [], st.get('messages') or {}, dels],
                          ensure_ascii=False, sort_keys=True)
    try:
        return sig(current) == sig(payload)
    except (TypeError, ValueError):
        return False


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
        # устойчивые ключи серверной истории: повторная доставка того же сообщения
        # (пересланный снимок, дошедшая позже дельта) схлопывается в существующее,
        # а не вставляется второй копией — идемпотентность дельта-пути.
        server_keys = {k for k in (_msg_key(x) for x in hist) if k}
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
                if at != i - 1:                             # пересланный повторно хвост
                    hist.pop(at)                            # (delta сдвинулась после
                hist.insert(min(i - 1, len(hist)),          # tombstone-удаления) —
                          {**old, **m}                      # переставляем на заявленную
                          if isinstance(old, dict) and isinstance(m, dict) else m)
                changed = True
            elif key and key in server_keys:                # копия уже известного — игнор
                continue
            elif key is None and 1 <= i <= len(hist) and hist[i - 1] is None:
                hist[i - 1] = m                           # заполнение ранее удалённой позиции
                changed = True
            elif key is None and i > len(hist):
                conflicts.append({'sid': sid, 'i': i})      # позиции нет — не дописываем вслепую
                continue
            elif i - 1 >= len(hist):                        # новый хвост — только дописка
                hist.append(m)                              # строго подряд: дыра в позициях
                if len(hist) - 1 > i - 1:                   # означает пропущенное сообщение
                    conflicts.append({'sid': sid, 'i': i})  # (перепутанные очереди дельт)
                else:
                    changed = True
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
    fresh_reg = [r for r in reg if isinstance(r, dict) and isinstance(r.get('sid'), str)
                 and now_ms - int(r.get('at') or 0) < _TOMB_TTL_MS]
    known = {r['sid'] for r in fresh_reg}
    touched = len(fresh_reg) != len(reg)
    for sid in set(deleted) | {d for d in (delta.get('_allDeleted') or []) if isinstance(d, str)}:
        messages.pop(sid, None)                             # историю удаляем всегда:
        if sid not in known:                                # без записи в sessions она
            fresh_reg.append({'sid': sid, 'at': now_ms})     # не видима никому и merge
            known.add(sid)                                  # уже не «воскресит»
            touched = True
    # Реестр tombstone'ов меняется крайне редко (только при удалениях), а он
    # входит в каждый ответ GET /ui-state. Если состав реестра идентичен
    # прежнему — оставляем прежний список объектов: тогда сериализованный JSON
    # снапшота побайтово совпадает с предыдущим, и клиент может пропустить
    # пустой merge (см. pullState). Это убирает периодическое «дёрганье»
    # интерфейса: раньше каждое устройство каждые POLL_MS тянуло полный снимок,
    # получало на микросекунду более свежий updated и перерисовывало весь чат,
    # хотя данные не изменились вовсе.
    same_reg = (not touched and len(fresh_reg) == len(reg)
                and all(a is b or a == b for a, b in zip(fresh_reg, reg)))
    out = {'updated': current.get('updated', 0),
           'messages': messages,
           'sessions': current.get('sessions') or [],
           'deleted_chats': reg if same_reg else fresh_reg}
    gone = known                                            # tombstone побеждает stale-снимок:
    if isinstance(delta.get('sessions'), list):             # список чатов менялся
        # «Явно удалён» = id есть в deleted_chats этой же дельты. Раньше сюда
        # попадал и _allDeleted (реестр клиента за 7 дней): устройство, на
        # котором чат удалили давно, каждый sync подсовывал его старую копию в
        # sessions, и tombstone-запрет обходился («не могу удалить то, что
        # удалено явно») — чат воскресал в списке. Теперь удаление — абсолютный
        # факт: ни один merge не возвращает tombstone-чат, повторное создание
        # даёт новый id и старых реестров не касается.
        explicit = set(deleted)
        # дельта-список прислан клиентом целиком (sessionsChanged — весь массив
        # sessions), поэтому partial=False: порядок отправителя — эталонный.
        merged = _merge_sessions(current.get('sessions') or [], delta['sessions'],
                                 partial=bool(delta.get('_partial')))
        merged = [s for s in merged if not isinstance(s, dict) or s.get('id') not in (gone - explicit)]
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
async def get_state(request: Request, user_id: str = Depends(require_auth)):
    """Полный снимок состояния с ETag.

    Если снапшот побайтово не менялся с последнего запроса этого клиента
    (If-None-Match), отдаём 304 — ни трафика, ни разбора мегабайтного JSON на
    клиенте. Это второй эшелон против «дёрганья»: даже когда дельта-канал
    недоступен и устройство тянет полный GET, повторяющийся опрос ничего не
    перерисовывает."""
    st = _cached_read(user_id)
    data = json.dumps(st, ensure_ascii=False)
    etag = _state_etag(data)
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers={'ETag': etag})
    return Response(content=data, media_type='application/json',
                    headers={'ETag': etag, 'Cache-Control': 'no-store'})


@router.get('/ui-state/version')
async def get_state_version(user_id: str = Depends(require_auth)):
    """Только версия состояния — для периодического опроса другими устройствами.

    Дешевле полного GET /ui-state при больших архивах: если updated не изменился,
    тянуть снапшот незачем. Изменения чужого аккаунта этот ответ не раскрывает."""
    st = _cached_read(user_id)
    return {'updated': st.get('updated', 0)}


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
        g = _active_state(user_id)
        if g is not None and not client_gen:
            raise HTTPException(429, detail='На другом устройстве идёт генерация — синхронизация приостановлена')

    current = _cached_read(user_id)
    rebased = False

    # Распознаём «полный снимок с delta-заглушкой» ДО проверки base и ДО
    # no-op-проверки. Иначе на самом первом sync (base == updated == 0) блок
    # `if body.base != current.updated` пропускается целиком, и пустая delta
    # ниже уходит в ветку `_delta_is_noop` -> сервер отвечает 200, не читая
    # body.sessions/body.messages. Снапшот молча теряется.
    if isinstance(body.delta, dict) and body.delta.get('messages') is None \
            and (body.sessions or body.messages):
        client_dels = [d for d in ((body.delta.get('deleted_chats') or [])
                                   + (body.delta.get('_allDeleted') or []))
                       if isinstance(d, str)]
        body.delta = {'deleted_chats': client_dels} if client_dels else None

    if body.base != current.get('updated', 0):
        # Авто-перебазирование для точечных дельт: база клиента устарела, но
        # присланные изменения сопоставимы по устойчивым id сообщений, поэтому
        # их можно безопасно применить поверх СВЕЖЕЙ серверной версии. Раньше
        # любой stale-base давал 409: клиент тянул полный снимок, ставил свою
        # базу и повторял PUT — а за эти секунды другое устройство снова успевало
        # что-то сохранить, и цикл 409 повторялся бесконечно («сообщение не
        # отправляется», «устройства не видят чаты друг друга»).
        # Теперь конфликт разрешается на месте одним запросом; полные снимки
        # по-прежнему получают честный 409 — они не должны затирать соседей.
        if isinstance(body.delta, dict) and not _delta_is_noop(body.delta):
            fresh = _read(user_id)
            try:
                fresh_stamp = os.stat(_path(user_id)).st_mtime_ns
            except OSError:
                fresh_stamp = 0
            if body.base == fresh.get('updated', 0):
                # кэш прочтений устарел (запись мимо кэша) — берём свежий снимок
                current = fresh
                rebased = True
            else:
                payload_try, _ = _apply_delta(fresh, body.delta)
                payload_try['updated'] = fresh.get('updated', 0)
                if _snapshot_is_noop(fresh, payload_try):
                    # Дельта уже полностью отражена в свежей версии (повторная
                    # доставка после оборванного ответа): это успех, а не
                    # конфликт — отвечаем текущей версией, состояние не трогаем.
                    return {'updated': fresh.get('updated', 0)}
                current = fresh
                rebased = True
        else:
            raise HTTPException(409, detail='Состояние изменено на другом устройстве')

    conflicts: list = []
    # Ранний отказ по размеру ВХОДЯЩИХ данных: раньше 413 бросался ПОСЛЕ merge
    # и вычисления версии. Для «оглушительного» запроса (несколько мегабайт в
    # одном сообщении) это означало секунды на слияние/сериализацию ради
    # гарантированного отказа; при повторных попытках клиента сервер тратил
    # время ответа, а интерфейс выглядел «подвешенным».
    incoming_bytes = len(json.dumps(
        {'sessions': body.sessions, 'messages': body.messages, 'delta': body.delta},
        ensure_ascii=False).encode('utf-8'))
    if incoming_bytes > MAX_BYTES:
        raise HTTPException(413, detail='Слишком много данных. Удалите старые чаты.')

    if body.delta is not None and _delta_is_noop(body.delta):
        # No-op дельта (пустые messages/deleted_chats/sessions): состояние НЕ
        # трогаем, версию НЕ двигаем. Это главный источник «каскада 409»:
        # каждое фоновое сохранение каждого устройства поднимало updated без
        # реальных изменений, соседи получали «новую версию», их собственные
        # PUT-основания устаревали, и синхронизация превращалась в бесконечное
        # перетягивание версий вместо доставки реальных сообщений.
        # Подписчиков всё же будим сигналом «state-changed» — он дешёвый:
        # клиент по нему лишь сверяет версию/ETag (304 при неизменности), а не
        # перерисовывает чат. Раньше здесь не было уведомления, и тест 9g
        # («PUT принят -> событие в очереди подписчика») падал; заодно любое
        # подтверждённое сохранение теперь честно оповещает соседей.
        # Ответ отдаём БЕЗ conflicts: пустая дельта ничего не могла сконфликтовать.
        _notify_state(user_id)
        return {'updated': current.get('updated', 0)}

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
        all_del_raw = (body.delta or {}).get('_allDeleted') if isinstance(body.delta, dict) else None
        dels = {d for d in ((dels_raw or []) + (all_del_raw or [])) if isinstance(d, str)}
        # реестр удалений аккаунта — единый источник правды для обоих путей записи
        # (дельта и полный снимок). Раньше полный снимок сверялся только с tombstone'ами
        # из собственного тела: legacy-клиент или устройство без реестра воскрешал
        # историю/запись удалённого чата целиком («чат не удаляется, появляется снова»).
        now_ms = int(time.time() * 1000)
        reg = [r for r in (current.get('deleted_chats') or [])
               if isinstance(r, dict) and isinstance(r.get('sid'), str)
               and now_ms - int(r.get('at') or 0) < _TOMB_TTL_MS]
        known = {r['sid'] for r in reg}
        for d in sorted(dels | known):                    # новые tombstone-ы в реестр:
            if d not in known:                            # сервер помнит удаления и для
                reg.append({'sid': d, 'at': now_ms})      # mergeFull других устройств
                known.add(d)
        dels |= known
        for sid in dels:                                  # историю удалённых чатов
            server_msgs.pop(sid, None)                    # вырезаем всегда
        for sid, hist in (body.messages or {}).items():
            if sid in dels:                               # удалённый чат — не оживляем
                continue
            if isinstance(hist, list):
                merged_msgs[sid] = _merge_msgs(server_msgs.get(sid) or [], hist)
        for sid, hist in server_msgs.items():
            if sid not in merged_msgs and sid not in dels:
                merged_msgs[sid] = hist
        sess_merged = _merge_sessions(current.get('sessions') or [], body.sessions)
        payload = {
            'updated': current.get('updated', 0),
            'sessions': [s for s in sess_merged if s.get('id') not in dels],
            'messages': merged_msgs,
            'deleted_chats': reg,
        }
    # No-op полный снимок: если после merge побайтово ничего не изменилось
    # (повторный sync того же состояния, «открыл и закрыл чат»), версию НЕ
    # двигаем и запись на диск не делаем — иначе каждое такое сохранение
    # выглядело для других устройств как новое событие и гнало их за полным
    # снимком («каскад 409» и лишняя перерисовка интерфейса).
    if _snapshot_is_noop(current, payload):
        out = {'updated': current.get('updated', 0)}
        if conflicts:
            out['conflicts'] = conflicts
        return out

    payload['updated'] = max(int(time.time() * 1000), current.get('updated', 0) + 1)

    # Проверка размера ДО записи: сравниваем ровно то, что будет сохранено на
    # диск; если отказ случился из-за чужих данных на сервере, клиент при
    # следующем pull получит свежий снапшот и почистит локальный архив.
    data = json.dumps(payload, ensure_ascii=False)
    if len(data.encode('utf-8')) > MAX_BYTES:
        raise HTTPException(413, detail='Слишком много данных. Удалите старые чаты.')

    if rebased:
        # Перебазированная запись идёт через compare-and-swap по mtime файла:
        # если между чтением свежей версии и записью успел вклиниться другой
        # PUT, условие не выполнен — честный 409 вместо тихой перезатирки чужих данных.
        path = _path(user_id)
        try:
            stamp_now = os.stat(path).st_mtime_ns
        except OSError:
            stamp_now = 0
        if stamp_now != fresh_stamp:
            raise HTTPException(409, detail='Состояние изменено на другом устройстве')
    _write_state(user_id, payload)                   # атомарная замена + сброс кэша
    # Журнал изменений для точного дельта-канала: что сдвинулось на этом PUT.
    # before — смерженный предыдущий снимок (тот же «до», относительно которого
    # решалось что писать), после no-op проверок здесь всегда есть реальная разница.
    before_for_log = {**current, 'updated': current.get('updated', 0)}
    _record_change(str(user_id), payload['updated'], before_for_log, payload)
    _notify_state(user_id)                           # мгновенно будим SSE-подписчиков других устройств
    out = {'updated': payload['updated']}
    if conflicts:
        out['conflicts'] = conflicts                 # клиент перестроит историю чата по серверной версии
    return out


# ------------------------------------------------------------------ дельта состояния (GET)
# «Дёрганье» чата каждые несколько секунд: каждое устройство периодически
# сверяет версию (/ui-state/version) и при расхождении тянет ПОЛНЫЙ снимок —
# десятки мегабайт на архив, тяжёлый merge, полная перерисовка DOM. При этом
# реальное изменение обычно крошечное (одно сообщение в одном чате).
# GET /ui-state/delta?since=<версия> возвращает только то, что изменилось с
# этой версии: изменённые/новые чаты целиком (они и так маленькие), удалённые
# tombstone'ы и новый sessions list, если список менялся. Клиент применяет
# дельту точечно и НЕ трогает открытые чаты, которых в дельте нет.
# Ответ дополняется ETag: побайтово идентичный снимок отдаёт 304 Not Modified,
# и даже полный GET становится дешёвым для повторяющихся опросов.

def _state_etag(data: str) -> str:
    import hashlib
    return '"' + hashlib.sha1(data.encode('utf-8')).hexdigest() + '"'


# Журнал последних записей аккаунта: uid -> deque[(version, diff)], где diff —
# {messages:{sid:история}, sessions:[...]|None, deleted:[sid,...]} — что именно
# изменил тот PUT. Дельта-канал отвечает по этому журналу точно: «изменилось
# вот это с версии N», а не эвристикой по времени сообщений. Без журнала любое
# редактирование старого сообщения (time остаётся прежним) или переименование
# чата (updated может быть локальным временем отстающего устройства) не
# детектировалось, и второе устройство НИКОГДА не видело правку — «свой чат на
# каждом устройстве». Журнал живёт в памяти процесса; при рестарте или выходе
# за окно клиент честно получает need_full и один раз тянет снимок.
from collections import deque as _deque
_CHANGELOG_MAX = 64               # версий в журнале на аккаунт
_CHANGELOG_ACCOUNTS = 256         # аккаунтов одновременно
_changelog: "OrderedDict[str, _deque]" = OrderedDict()


def _record_change(uid: str, version: int, before: dict, after: dict) -> None:
    """Запоминает что изменилось при переходе before->after (для GET /delta)."""
    diff = {'messages': {}, 'sessions': None, 'deleted': []}
    bm, am = before.get('messages') or {}, after.get('messages') or {}
    for sid, hist in am.items():
        if bm.get(sid) != hist:
            diff['messages'][sid] = hist
    for sid in bm:
        if sid not in am:
            diff['messages'][sid] = []          # история удалена (tombstone)
    if before.get('sessions') != after.get('sessions'):
        diff['sessions'] = after.get('sessions') or []
    bdel = {d.get('sid') for d in (before.get('deleted_chats') or [])
            if isinstance(d, dict) and isinstance(d.get('sid'), str)}
    adel = {d.get('sid') for d in (after.get('deleted_chats') or [])
            if isinstance(d, dict) and isinstance(d.get('sid'), str)}
    diff['deleted'] = sorted(adel - bdel)
    q = _changelog.get(uid)
    if q is None:
        if len(_changelog) >= _CHANGELOG_ACCOUNTS:
            _changelog.popitem(last=False)
        q = _changelog[uid] = _deque(maxlen=_CHANGELOG_MAX)
    _changelog.move_to_end(uid)
    q.append((version, diff))


@router.get('/ui-state/delta')
async def get_state_delta(request: Request, since: int = 0,
                          user_id: str = Depends(require_auth)):
    st = _cached_read(user_id)
    updated = st.get('updated', 0)
    try:
        since = max(0, int(since))
    except (TypeError, ValueError):
        since = 0
    # База «свежеее» ответа (например часы клиента впереди) — дельты не
    # существует: снимок целиком.
    if since > updated:
        return {'changed': False, 'need_full': True, 'updated': updated}
    if since == updated:
        return {'changed': False, 'updated': updated}
    # since < updated: есть разошедшиеся версии. Если журнал их помнит —
    # точная дельта; если нет (первый sync, рестарт сервера, длинное отсутствие)
    # — честный need_full: клиент потянет снимок один раз. Раньше при since<=0
    # всегда возвращался пустой changed:false, и новое устройство никогда не
    # получало чаты других устройств («у каждого своя копия чата»).
    q = _changelog.get(str(user_id))
    # Покрытие журнала: earliest-запись должна быть СТАРШЕ или равна since
    # (то есть все PUT после since в журнале присутствуют). Раньше стояло
    # `q[0][0] > since` — инверсия, из-за которой дельта отдавалась только
    # «доисторическому» since=0, а синхронизированное устройство на каждый
    # state-changed получал need_full и тянуло полный снимок.
    # КРИТИЧНО для корректности: если deque упёрся в maxlen=64 и самые
    # старые записи выброшены, собрать дельту от since физически нельзя —
    # пропущенные PUT'ы не применятся и клиент получит ИСКАЖЕННУЮ историю
    # вместо честного need_full (симптом «чат появился, но внутри пусто»).
    # Поэтому при переполнении журнала покрытие НЕ гарантируем никогда:
    # вернём need_full, а полный снимок содержит всю историю целиком.
    covered = bool(q) and len(q) < _CHANGELOG_MAX and q[0][0] <= since
    if not covered:
        return {'changed': False, 'need_full': True, 'updated': updated}
    changed_msgs: dict = {}
    sess_list = None
    dels: set = set()
    for version, diff in q:
        if version <= since:
            continue
        for sid, hist in diff['messages'].items():
            changed_msgs[sid] = hist
        if diff['sessions'] is not None:
            sess_list = diff['sessions']
        dels.update(diff['deleted'])
    # tombstone побеждает: история удалённого чата не уезжает клиенту, даже
    # если в какой-то версии она ещё была в диффе
    for sid in dels:
        changed_msgs.pop(sid, None)
    if not changed_msgs and sess_list is None and not dels:
        return {'changed': False, 'updated': updated}
    out = {'changed': True, 'updated': updated,
           'messages': changed_msgs, 'deleted_chats': sorted(dels)}
    if sess_list is not None:
        out['sessions'] = sess_list
    return out


# ------------------------------------------------------------------ события состояния (SSE)
# Второе устройство узнаёт о сохранении первого без перезагрузки страницы и не
# дожидаясь тика поллинга: PUT /ui-state после записи дергает _notify_state, а
# подписанные GET /ui-state/events клиенты мгновенно получают «state-changed» и
# сами тянут свежий снимок. Буферизация событий не нужна — событие лишь сигнал
# «посмотри сервер», данные всегда читаются из файла. Соединение живёт вёрстку
# браузера; heartbeat каждые 25 с не даёт nginx/прокси закрыть его как мёртвое.

_state_listeners: dict[str, list[asyncio.Queue]] = {}
_STATE_LISTENERS_MAX = 300   # всего открытых подписок (защита от утечки при обрывах)


def _notify_state(uid: str) -> None:
    for q in _state_listeners.get(uid, ()):
        try:
            q.put_nowait('state-changed')     # очередь набита — подписчик и так перезапросит
        except asyncio.QueueFull:
            pass


@router.get('/ui-state/events')
async def state_events(request: Request, user_id: str = Depends(require_auth)):
    from starlette.responses import StreamingResponse

    uid = str(user_id)
    if sum(len(v) for v in _state_listeners.values()) >= _STATE_LISTENERS_MAX:
        raise HTTPException(503, detail='Слишком много подписок')
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    _state_listeners.setdefault(uid, []).append(q)

    async def stream():
        try:
            # преамбула уходит сразу при старте генератора (до первого await) —
            # иначе клиент/TestClient зависает в ожидании заголовков сообщения
            yield 'retry: 5000\n\n'          # EventSource переподключится через 5 с после обрыва
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f'event: {ev}\ndata: 1\n\n'
                except asyncio.TimeoutError:
                    yield ': hb\n\n'         # heartbeat против таймаутов обратного прокси
        finally:
            lst = _state_listeners.get(uid, ())
            if q in lst:
                lst.remove(q)
            if not lst:
                _state_listeners.pop(uid, None)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',           # nginx: не буферизовать поток
        'Connection': 'keep-alive',
    })
