"""Тест: модель не может читать данные пользователей через инструменты кода.
Запуск из папки проекта: python tests/sync/run_code_access_tests.py
"""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
os.environ.setdefault('SECRET_KEY', 'x' * 40)

from GCN.code_analyzer import CodeAnalyzer  # noqa: E402

tmp = Path(tempfile.mkdtemp())
(tmp / 'GCN').mkdir(); (tmp / 'routes').mkdir()
(tmp / 'GCN' / 'memory_graph.py').write_text('def recall(): pass\\n', encoding='utf-8')
(tmp / 'routes' / 'ai_assistant.py').write_text('router = None\\n', encoding='utf-8')
(tmp / 'config.py').write_text('X = 1\\n', encoding='utf-8')
(tmp / 'ai_memory_v3' / 'u2').mkdir(parents=True)
(tmp / 'ai_memory_v3' / 'u2' / 'history.json').write_text('{"secret": "переписка другого аккаунта"}', encoding='utf-8')
(tmp / 'ui_state').mkdir(); (tmp / 'ui_state' / 'u2.json').write_text('{"chats": "приватные чаты"}', encoding='utf-8')
(tmp / '.env').write_text('SECRET_KEY=abc', encoding='utf-8')
(tmp / 'auth.db').write_bytes(b'SQLite format 3\x00')

ca = CodeAnalyzer(root_path=tmp)
res = []
def check(n, ok, extra=''):
    res.append(bool(ok)); print('PASS' if ok else 'FAIL', n, extra[:120])

async def main():
    r = await ca.read_file('ai_memory_v3/u2/history.json')
    check('1 other user history is not readable', 'переписка' not in r and ('запрещ' in r or 'Ошибка' in r or 'не' in r.lower()), r)
    r = await ca.read_file('ui_state/u2.json')
    check('2 other user chats are not readable', 'приватные' not in r, r)
    r = await ca.read_file('.env')
    check('3 .env is not readable', 'SECRET_KEY' not in r, r)
    r = await ca.read_file('auth.db')
    check('4 auth database is not readable', 'SQLite' not in r, r)
    r = await ca.read_file('GCN/memory_graph.py')
    check('5 code files are still readable', 'def recall' in r, r)
    r = await ca.search_in_code('переписка')
    check('6 search does not find user data', 'другого аккаунта' not in r and 'Ничего не найдено' in r, r)
    r = await ca.get_project_structure(max_depth=3)
    check('7 project tree hides data folders', 'ai_memory_v3' not in r and 'ui_state' not in r and '.env' not in r, r)
    check('8 project tree shows code folders', 'GCN' in r and 'routes' in r, r)

asyncio.run(main())
passed = sum(res); print(f'\n{passed}/{len(res)} passed')
sys.exit(0 if passed == len(res) else 1)
