# Полный лог запуска программы

## Что было изменено

### 1. `setup.py` — Настройка логирования
- Установлен уровень DEBUG для отладочных сообщений при старте
- Добавлен отдельный логгер `"startup"` для отслеживания событий старта
- Формат логов в файле: `%(asctime)s [%(levelname)-7s] %(name)s:%(lineno)d - %(message)s`

### 2. `run.py` — Точка входа программы
Добавлено подробное логирование перед запуском uvicorn:

**Конфигурация приложения:**
- Режим работы (mode)
- Хост и порт сервера
- Количество воркеров (всегда 1 для in-memory состояния)
- Loop implementation (uvloop/asyncio)
- PID процесса

**Окружение (Environment):**
- FLASK_ENV, UVICORN_MODE
- SERVER_HOST, SERVER_PORT
- ALLOW_PUBLIC_BIND

**Python окружение:**
- PYTHON_VERSION
- PLATFORM
- EXECUTABLE

**Ключевые пути:**
- PROJECT_ROOT
- PID_FILE

### 3. `main.py` — FastAPI приложение
Добавлено логирование в lifespan контекстном менеджере:

**При старте (start):**
- `[LIFESPAN] Началась инициализация приложения`
- `[AUTH] Инициализация базы данных пользователей завершена` (если AUTH_ENABLED)
- `[MERGE] Запущена глобальная задача слияния памяти`

**Фоновые задачи:**
- `[MCP] _deferred_init_mcp() — инициализация MCP-менеджера`
- `[LLAMA]  _autostart_llamacpp() — автозагрузка модели по умолчанию`
- `[WARMUP] _warm_models()      — прогрев embedder, NLI и reranker`

**При завершении (shutdown):**
- `[MERGE] Остановлена глобальная задача слияния памяти`
- `[SHUTDOWN] Завершена остановка всех компонентов`
- `[LLAMA] Остановлен llama-server, освобождён VRAM`

### 4. `GCN/llamacpp_manager.py` — Управление llama-server
Добавлено детальное логирование загрузки и остановки модели:

**При загрузке (load):**
- `[LLAMA] Модель {id}: mmproj={путь или 'не найден'}`
- `[LLAMA] Команда запуска: {полная команда с аргументами}`
- `[LLAMA] Параметры: ctx=X, gpu_layers=Y, flash_attn=Z, kv_cache=W, threads=T, parallel=P, main_gpu=M`
- `[LLAMA] Запуск llama-server: PID={pending или pid}`
- `[LLAMA] llama-server запущен с PID={pid}`

**При ожидании готовности (_wait_ready):**
- `[LLAMA] Ожидание готовности llama-server: {url} (дедлайн через X.0с)`
- Каждую секунду проверяется статус процесса и HTTP /health
- При успехе: `[LLAMA] llama-server готов! Ответ /health: 200`

**При ошибке:**
- `[LLAMA] Ошибка при ожидании готовности: {error}`, exc_info=True (полный стек)
- `[LLAMA] Ошибка: llama-server не ответил за X.0с`, exc_info=True

**При остановке (unload):**
- `[LLAMA] Завершение llama-server (PID={pid})...`
- `[LLAMA] graceful terminate не сработал, force kill (PID={pid})` — если нужно принудительное завершение
- `[LLAMA] llama-server уже остановлен (PID=N/A)` — если процесс уже умер

## Как посмотреть логи

### Консольный вывод
При запуске программы (`python run.py`) вы увидите подробные сообщения в консоли с временными метками.

### Файл логов
Полный лог сохраняется в файле `assistant.log` в корне проекта.

Формат файла:
```
2026-11-18 12:04:15 [INFO   ] startup - ============================================================
2026-11-18 12:04:15 [INFO   ] startup - 🚀  Local AI Assistant (FastAPI + Uvicorn)
2026-11-18 12:04:15 [INFO   ] startup - ============================================================
2026-11-18 12:04:15 [INFO   ] startup -    Mode:    test
...
```

### Отдельный лог llama-server
Лог самого llama-server сохраняется в файле `LLAMACPP_LOG` (путь задаётся в `.env` через переменную `LLAMACPP_LOG`).

## Пример полного лога запуска

```
2026-11-18 12:04:15 [INFO   ] startup - ============================================================
2026-11-18 12:04:15 [INFO   ] startup - 🚀  Local AI Assistant (FastAPI + Uvicorn)
2026-11-18 12:04:15 [INFO   ] startup - ============================================================
2026-11-18 12:04:15 [INFO   ] startup -    Mode:    production
2026-11-18 12:04:15 [INFO   ] startup -    Host:    127.0.0.1:8000   (behind nginx)
2026-11-18 12:04:15 [INFO   ] startup -    Workers: 1  (forced 1: in-memory state)
2026-11-18 12:04:15 [INFO   ] startup -    Loop:    uvloop
2026-11-18 12:04:15 [INFO   ] startup -    Reload:  False
2026-11-18 12:04:15 [INFO   ] startup -    Docs:    http://127.0.0.1:8000/api/docs
2026-11-18 12:04:15 [INFO   ] startup -    PID:     7656
2026-11-18 12:04:15 [INFO   ] startup - ============================================================

2026-11-18 12:04:15 [INFO   ] startup - 📋  Environment Configuration:
2026-11-18 12:04:15 [INFO   ] startup -    FLASK_ENV=production
2026-11-18 12:04:15 [INFO   ] startup -    UVICORN_MODE=production
2026-11-18 12:04:15 [INFO   ] startup -    SERVER_HOST=127.0.0.1
2026-11-18 12:04:15 [INFO   ] startup -    SERVER_PORT=8000
2026-11-18 12:04:15 [INFO   ] startup -    ALLOW_PUBLIC_BIND=False

2026-11-18 12:04:15 [INFO   ] startup - 🐍  Python Environment:
2026-11-18 12:04:15 [INFO   ] startup -    PYTHON_VERSION=3.13.5 (tags/v3.13.5:6cb20a2, Jun 11 2025, 16:15:46) [MSC v.1943 64 bit (AMD64)]
2026-11-18 12:04:15 [INFO   ] startup -    PLATFORM=win32
2026-11-18 12:04:15 [INFO   ] startup -    EXECUTABLE=C:\Users\PC\AppData\Local\Programs\Python\Python313\python.exe

2026-11-18 12:04:15 [INFO   ] startup - 📁  Key Paths:
2026-11-18 12:04:15 [INFO   ] startup -    PROJECT_ROOT=C:\Users\PC\Documents\GitHub\MCPIISys
2026-11-18 12:04:15 [INFO   ] startup -    PID_FILE=C:\Users\PC\Documents\GitHub\MCPIISys\app.pid

2026-11-18 12:04:15 [INFO   ] startup - 🚀  Starting Uvicorn Server...
2026-11-18 12:04:15 [INFO   ] startup - ============================================================
```

## Преимущества

1. **Полная трассировка** — видно каждый шаг от запуска Python до готовности llama-server
2. **Детализация параметров** — все важные конфигурационные параметры записываются в лог
3. **Отслеживание процессов** — PID llama-server и его статус на каждом этапе
4. **Ошибки с полным стеком** — при ошибках показывается полный traceback для отладки
5. **Разделение логики** — отдельный логгер `"startup"` позволяет легко фильтровать события старта

## Настройка уровня детализации

Если нужно больше/меньше логов, измените уровень в `setup.py`:

```python
# Для отладочных логов (DEBUG)
logging.basicConfig(level=logging.DEBUG, ...)

# Для производственного режима (INFO)
logging.basicConfig(level=logging.INFO, ...)
```
