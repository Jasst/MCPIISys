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
check('1 empty state on first read', r.json() == {'updated': 0, 'sessions': [], 'messages': {}})

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

big = {'base': c.get('/ai/ui-state').json()['updated'], 'sessions': [], 'messages': {'x': ['a' * 9_000_000]}}
r = c.put('/ai/ui-state', json=big)
check('8 oversized state refused (413)', r.status_code == 413)

ui._read  # noqa
bad = TestClient(FastAPI())
bad.app.include_router(ui.router)
bad.app.dependency_overrides[require_auth] = lambda: '../etc'
check('7 unsafe user id refused (400)', bad.get('/ai/ui-state').status_code == 400)

passed = sum(res); print(f'\n{passed}/{len(res)} passed')
sys.exit(0 if passed == len(res) else 1)
