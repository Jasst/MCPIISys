"""
Проверка многозапросного recall и пакетного remember на фиктивной памяти.
Запуск из папки проекта: python tests/eval/test_internal_memory_tools.py
"""
import asyncio
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)

# Тяжёлые зависимости не нужны для проверки логики: подставляем заглушки
ws = types.ModuleType('GCN.web_search'); ws.deep_search = None
sys.modules.setdefault('GCN.web_search', ws)

from GCN import internal_tools as it  # noqa: E402
from GCN.tool_router import ToolRegistry  # noqa: E402

class FakeMemory:
    def __init__(self):
        self.store = {
            'кофе': [{'text': 'любит кофе без сахара', 'confidence': 0.9}, {'text': 'пьёт кофе утром', 'confidence': 0.6}],
            'утро': [{'text': 'пьёт кофе утром', 'confidence': 0.8}, {'text': 'встаёт в 7', 'confidence': 0.7}],
            'здоровье': [],
        }
        self.saved = []
        self.reject_words = {'пустяк'}
    async def recall(self, query, top_k=5, scope=None):
        return self.store.get(query, [])
    async def remember(self, fact, scope=None, user_explicit=False):
        if fact in self.reject_words:
            return {'reason': 'not_factual'}
        self.saved.append(fact)
        return {'id': f'id{len(self.saved)}', 'fact': fact, 'scope': 'private'}

class FakeController:
    def __init__(self):
        self.memory_service = FakeMemory()

registry = ToolRegistry()
ctrl = FakeController()
it._register_memory(registry, ctrl)
handlers = {}
for name in ('recall', 'remember'):
    handlers[name] = registry._tools[f'internal__{name}'].handler if hasattr(registry, '_tools') else None

res = []
def check(name, ok, extra=''):
    res.append(bool(ok)); print('PASS' if ok else 'FAIL', name, extra)

async def main():
    recall = handlers['recall']
    remember = handlers['remember']
    if recall is None:
        raise SystemExit('не удалось получить обработчики из ToolRegistry')

    one = await recall({'query': 'кофе'})
    check('1 single query still works', 'любит кофе' in one and 'пьёт кофе' in one, one)

    many = await recall({'queries': ['кофе', 'утро']})
    lines = [l for l in many.splitlines() if l.startswith('-')]
    texts = [l.split(' (уверенность')[0][2:] for l in lines]
    check('2 several queries merged, duplicates removed', len(texts) == len(set(texts)) == 3, many)
    check('3 merged results ranked by confidence', texts[0] == 'любит кофе без сахара', many)

    empty = await recall({'queries': ['здоровье']})
    check('4 nothing found is reported', empty == 'Ничего не найдено.', empty)

    none = await recall({})
    check('5 no query gives a clear message', 'Укажите запрос' in none, none)

    batch = await remember({'facts': ['живёт в Казани', 'пустяк', 'работает инженером']})
    check('6 batch saves valid facts', ctrl.memory_service.saved == ['живёт в Казани', 'работает инженером'], batch)
    check('7 batch reports rejected facts with reason', 'Запомнил 2 из 3' in batch and 'not_factual' in batch, batch)

    single = await remember({'fact': 'любит чай'})
    check('8 single fact still works', 'Запомнил:' in single or 'любит чай' in single, single)

asyncio.run(main())
passed = sum(res); print(f'\n{passed}/{len(res)} passed')
sys.exit(0 if passed == len(res) else 1)
