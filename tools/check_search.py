"""
Проверка поиска: какие поисковики реально отвечают из твоей сети.
Запуск из папки проекта:  python tools/check_search.py

Ключи и адреса не печатаются, только результат каждого поисковика.
"""
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

# .env читаем до импорта web_search: он берёт SEARXNG_URL и порядок поисковиков при загрузке,
# так же как run.py. Без этого скрипт проверял бы не те настройки, что использует приложение.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(ROOT, '.env'), override=False)
except ImportError:
    pass

import GCN.web_search as ws  # noqa: E402

QUERY = 'погода москва сегодня'


async def check(name, fn):
    try:
        results = await fn(QUERY, 3)
    except Exception as e:
        print(f'  {name:<10} ОШИБКА: {type(e).__name__}: {e}')
        return
    if results:
        print(f'  {name:<10} OK, результатов: {len(results)}  пример: {results[0].get("url", "")[:60]}')
    else:
        print(f'  {name:<10} пусто (нет настройки, блокировка или нет результатов)')


async def main():
    print('Поисковики в порядке использования:', ', '.join(ws.SEARCH_BACKENDS_ORDER))
    print('SearXNG адрес:', ws.SEARXNG_URL or '(не задан)')
    print()
    for name in ws.SEARCH_BACKENDS_ORDER:
        fn = ws._BACKENDS.get(name)
        if fn is None:
            print(f'  {name:<10} неизвестный поисковик')
            continue
        await check(name, fn)
    await ws.close_search_resources()


if __name__ == '__main__':
    asyncio.run(main())