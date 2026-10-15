"""Тесты общего состояния интерфейса: запись, чтение, версии, размер.
Запуск из папки проекта: python tests/sync/run_ui_state_tests.py
"""
import os, sys, tempfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
os.environ.setdefault('SECRET_KEY', 'x' * 40)
os.environ.setdefault('AUTH_DB_PATH', os.path.join(tempfile.mkdtemp(), 'a.db'))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import routes.ui_state as ui  # noqa: E402
from dependencies import require_auth  # noqa: E402

ui.STATE_DIR = __import__('pathlib').Path(tempfile.mkdtemp())   # не трогаем настоящие данные

app = FastAPI()
app.include_router(ui.router)
app.dependency_overrides[require_auth] = lambda: 'u1'
c = TestClient(app)
res = []
def check(n, ok, extra=''):
    res.append(bool(ok)); print('PASS' if ok else 'FAIL', n, extra)

r = c.get('/ai/ui-state')
check('1 empty state on first read', r.json() == {'updated': 0, 'sessions': [], 'messages': {}, 'deleted_chats': []})

body = {'base': 0, 'sessions': [{'id': 's1', 'name': 'Чат'}], 'messages': {'s1': [{'role': 'user', 'text': 'привет'}]}}
r = c.put('/ai/ui-state', json=body)
check('2 first write accepted', r.status_code == 200, r.text)
v1 = r.json()['updated']

r = c.get('/ai/ui-state').json()
check('3 state readable with same content', r['sessions'][0]['name'] == 'Чат' and r['messages']['s1'][0]['text'] == 'привет')

r = c.put('/ai/ui-state', json={'base': 0, 'sessions': [], 'messages': {}})
check('4 stale base refused (409): another device wrote first', r.status_code == 409)

r = c.put('/ai/ui-state', json={'base': v1, 'sessions': [{'id': 's2', 'name': 'Второй'}], 'messages': {'s1': [{'role': 'user', 'text': 'привет'}]}})
check('5 write with current base accepted', r.status_code == 200)

v2 = c.get('/ai/ui-state').json()['updated']

# --- дельта-протокол: передаются только изменённые сообщения, не весь контент
# формат i — позиция в истории с 1 (как версии сообщений на клиенте)
r = c.put('/ai/ui-state', json={'base': v2, 'rev': 1, 'delta': {
    'messages': {'s1': [{'i': 2, 'm': {'role': 'assistant', 'text': 'ответ'}}]},
    'deleted_chats': [], 'sessions': [{'id': 's1', 'name': 'Чат'}, {'id': 's3', 'name': 'Третий'}]}})
check('6 delta append accepted', r.status_code == 200, r.text)
st = c.get('/ai/ui-state').json()
check('6a delta message present, old content kept',
      len(st['messages']['s1']) == 2 and st['messages']['s1'][1]['text'] == 'ответ')
check('6b delta sessions list applied', [s['id'] for s in st['sessions']] == ['s1', 's3'])

v3 = st['updated']
r = c.put('/ai/ui-state', json={'base': v3, 'rev': 2, 'delta': {
    'messages': {'s1': [{'i': 1, 'm': {'role': 'user', 'text': 'правк'}}]},
    'deleted_chats': ['s3']}})
check('7 delta patch + chat deletion', r.status_code == 200)
st = c.get('/ai/ui-state').json()
check('7a patched in place (merge fields)', st['messages']['s1'][0]['text'] == 'правк' and st['messages']['s1'][0]['role'] == 'user')
check('7b deleted chat history removed', 's3' not in st['messages'])

r = c.put('/ai/ui-state', json={'base': v3, 'delta': {'messages': {}, 'deleted_chats': []}})
check('7c stale delta refused (409)', r.status_code == 409)

# --- tombstone-реестр: удалённый чат не должен «воскресать» ни при каком merge
v4 = c.get('/ai/ui-state').json()['updated']
st = c.get('/ai/ui-state').json()
check('9a deletion registered in tombstones', any(d['sid'] == 's3' for d in st.get('deleted_chats', [])))
# устройство B (устаревший снимок списка, где s3 ещё есть) — tombstone побеждает
r = c.put('/ai/ui-state', json={'base': v4, 'rev': 5, 'delta': {
    'messages': {}, 'deleted_chats': [], '_allDeleted': ['s3'],
    'sessions': [{'id': 's1', 'name': 'Чат'}, {'id': 's3', 'name': 'Третий', 'updated': v4 + 1}]}})
check('9b resurrection via stale sessions list refused', r.status_code == 200)
st = c.get('/ai/ui-state').json()
check('9c deleted chat absent from merged sessions', all(s['id'] != 's3' for s in st['sessions']))
# полный снимок с историей s3 (старый клиент/первый sync) — историю не оживляем
r = c.put('/ai/ui-state', json={'base': st['updated'], 'sessions': [{'id': 's1', 'name': 'Чат'}],
                                'messages': {'s3': [{'role': 'user', 'text': 'призрак'}]}})
st = c.get('/ai/ui-state').json()
check('9d full snapshot cannot resurrect deleted chat history', 's3' not in st['messages'])

# --- мгновенная доставка изменений другим устройствам (SSE /ui-state/events).
# TestClient не умеет читать бесконечный стрим в фоне, поэтому проверяем
# механизм без открытия потока: PUT после записи должен поставить событие
# в очередь подписчика; обработчик канала существует и смонтирован в роутере.
q_evt = __import__('asyncio').Queue(maxsize=4)
ui._state_listeners.setdefault('u1', []).append(q_evt)
r = c.put('/ai/ui-state', json={'base': st['updated'], 'sessions': [{'id': 's1', 'name': 'Чат'}],
                                'messages': {}})
check('9g SSE notified on state write', r.status_code == 200 and q_evt.qsize() == 1
      and q_evt.get_nowait() == 'state-changed')
routes_paths = {(rt.path, frozenset(getattr(rt, 'methods', None) or ())) for rt in ui.router.routes}
check('9h events channel registered', ('/ai/ui-state/events', frozenset({'GET'})) in routes_paths)
# tombstone из дельты без sessions-списка тоже вырезает запись из sessions
st = c.get('/ai/ui-state').json()
r = c.put('/ai/ui-state', json={'base': st['updated'], 'rev': 6, 'delta': {
    'messages': {}, 'deleted_chats': ['s2']}})
st = c.get('/ai/ui-state').json()
check('9e tombstone without sessions list removes session entry',
      all(s['id'] != 's2' for s in st['sessions']) and 's2' not in st['messages'])
# GET отдаёт реестр — mergeFull клиента обязан вырезать эти чаты локально
check('9f gen-status/get exposes deleted_chats to clients', isinstance(st.get('deleted_chats'), list))

before = c.get('/ai/ui-state').json()
big = {'base': before['updated'], 'sessions': [], 'messages': {'x': ['a' * 9_000_000]}}
r = c.put('/ai/ui-state', json=big)
check('8 oversized state refused (413)', r.status_code == 413)
# 413 должен срабатывать ДО записи на диск и быть идемпотентным: повторный
# отказ не должен двигать updated и менять содержимое («чат копируется сам себя»)
after = c.get('/ai/ui-state').json()
check('8a rejected write left no trace (state byte-identical)',
      after == before)

# Регресс боевого лога: POST /gen-lock {client:false} при СВОБОДНОМ локе
# давал TypeError ('NoneType' object is not subscriptable) -> nginx 502,
# и новый чат на втором устройстве не стартовал.
r = c.post('/ai/gen-lock', json={'client': False, 'cid': 'cA'})
check('10a gen-lock check with free lock returns ok (no 500)',
      r.status_code == 200 and r.json().get('ok') is True)
r = c.post('/ai/gen-lock', json={'client': True, 'cid': 'cA'})
check('10b gen-lock capture returns ttl_ms > 0',
      r.status_code == 200 and r.json().get('held_by_me') and r.json()['ttl_ms'] > 0)
r = c.post('/ai/gen-lock', json={'client': False, 'cid': 'cB'})
check('10c second device blocked while lock held',
      r.status_code == 200 and r.json().get('ok') is False)
r = c.request('DELETE', '/ai/gen-lock', json={'client': True, 'cid': 'cB'})
check('10d foreign release cannot drop live lock',
      r.status_code == 200 and c.post('/ai/gen-lock', json={'client': False, 'cid': 'cB'}).json().get('ok') is False)
r = c.request('DELETE', '/ai/gen-lock', json={'client': True, 'cid': 'cA'})
check('10e owner release frees the lock',
      r.status_code == 200 and c.post('/ai/gen-lock', json={'client': False, 'cid': 'cB'}).json().get('ok') is True)

ui._read  # noqa
bad = TestClient(FastAPI())
bad.app.include_router(ui.router)
bad.app.dependency_overrides[require_auth] = lambda: '../etc'
check('7 unsafe user id refused (400)', bad.get('/ai/ui-state').status_code == 400)

passed = sum(res); print(f'\n{passed}/{len(res)} passed')
sys.exit(0 if passed == len(res) else 1)
