"""
run.py — Production runner для FastAPI (self-hosted).

ВАЖНО: принудительно 1 воркер. Причина:
  - routes/mcp_auth._sessions — in-memory
  - ai_assistant._assistants — in-memory (CognitiveController)
  - memory_service._services — in-memory
  - MCPToolManager._tool_cache/_rate_limits — in-memory
  - каждый CognitiveController держит свой SSE к MCP под X-User-Id

Multi-worker ломает сессии (401 через раз) и создаёт дубли MCP-подключений
под одним user_id. Масштабирование — отдельная задача (Redis + sticky),
а не подъём workers.

Наружу пускает только nginx — host всегда 127.0.0.1.
"""
import atexit
from pathlib import Path

# .env читаем до os.getenv: иначе SERVER_HOST/SERVER_PORT из .env игнорируются
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / '.env', override=False)
except ImportError:
    pass
import logging
import os
import signal
import sys

# Если stdout/stderr перенаправлены (Task Scheduler, CI), Windows использует
# ANSI-кодировку, и эмодзи в баннере роняют запуск с UnicodeEncodeError.
# errors='replace' оставляет консольный вывод как есть, но не даёт упасть.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

if sys.platform == 'win32':
    # uvicorn >= 0.36 выбирает loop через loop_factory и больше не смотрит на
    # WindowsSelectorEventLoopPolicy. Оставляем ProactorEventLoop (он нужен
    # uvicorn) и ставим backport фикса CPython gh-93821: иначе один reset
    # соединения (WinError 64) закрывает слушающий сокет :8000, процесс
    # продолжает жить, но сервер перестаёт принимать запросы.
    from win_asyncio_accept_fix import install as install_win_asyncio_accept_fix
else:
    install_win_asyncio_accept_fix = None

logger = logging.getLogger(__name__)

PID_FILE = os.path.join(os.path.dirname(__file__), 'app.pid')


def save_pid():
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        print(f"PID saved: {os.getpid()}")
    except Exception as e:
        print(f"Could not save PID: {e}")


def remove_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def graceful_shutdown(signum, frame):
    print("\nReceived shutdown signal, stopping gracefully...")
    remove_pid()
    sys.exit(0)


if __name__ == '__main__':
    import uvicorn

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    atexit.register(remove_pid)
    save_pid()

    mode = os.getenv('UVICORN_MODE', 'auto')

    # === ИЗМЕНЕНИЕ 1: читаем SERVER_HOST/SERVER_PORT, не HOST/PORT ===
    # В .env уже есть SERVER_HOST=127.0.0.1 — используем его.
    # Если SERVER_HOST не задан — всё равно 127.0.0.1 (наружу пускает nginx).
    host = os.getenv('SERVER_HOST') or os.getenv('HOST') or '127.0.0.1'
    port = int(os.getenv('SERVER_PORT') or os.getenv('PORT', 8000))

    is_prod = os.getenv('FLASK_ENV') == 'production'

    # === ИЗМЕНЕНИЕ 2: workers всегда 1 ===
    # Не читаем SERVER_WORKERS — она тут не применима.
    workers = 1
    reload = (mode == 'dev')

    # httptools быстрее, но на Windows его иногда не удаётся установить.
    # Если его нет, используем h11 — он входит в uvicorn и работает так же.
    try:
        import httptools  # noqa: F401
        http_impl = 'httptools'
    except ImportError:
        http_impl = 'h11'

    # === ИЗМЕНЕНИЕ 3: uvloop только если реально установлен ===
    try:
        import uvloop  # noqa: F401
        loop_impl = 'uvloop'
    except ImportError:
        loop_impl = 'asyncio'

    # === ИЗМЕНЕНИЕ 4: страховка от случайного 0.0.0.0 ===
    if host == '0.0.0.0' and not os.getenv('ALLOW_PUBLIC_BIND'):
        print("!! WARNING: SERVER_HOST=0.0.0.0 отключён, ставлю 127.0.0.1.")
        print("!! Если это осознанно — запусти с ALLOW_PUBLIC_BIND=1")
        host = '127.0.0.1'

    # === ДОБАВЛЕНО: подробный лог старта программы ===
    from setup import setup_logging
    _, startup_logger = setup_logging()

    if install_win_asyncio_accept_fix is not None:
        if install_win_asyncio_accept_fix():
            startup_logger.info(
                "[WIN] Установлен backport asyncio-accept (gh-93821, WinError 64)"
            )
        else:
            startup_logger.info(
                "[WIN] Backport asyncio-accept не нужен: исправление уже есть в Python"
            )
    
    print("=" * 80)
    print("🚀  Local AI Assistant (FastAPI + Uvicorn)")
    print("=" * 80)
    print(f"   Mode:    {mode}")
    print(f"   Host:    {host}:{port}   (behind nginx)")
    print(f"   Workers: {workers}  (forced 1: in-memory state)")
    print(f"   Loop:    {loop_impl}")
    print(f"   Reload:  {reload}")
    print(f"   Docs:    http://{host}:{port}/api/docs")
    print(f"   PID:     {os.getpid()}")
    print("=" * 80)

    # Записываем конфигурацию в лог
    startup_logger.info("=" * 60)
    startup_logger.info("🚀  Local AI Assistant (FastAPI + Uvicorn)")
    startup_logger.info("=" * 60)
    startup_logger.info(f"   Mode:    {mode}")
    startup_logger.info(f"   Host:    {host}:{port}   (behind nginx)")
    startup_logger.info(f"   Workers: {workers}  (forced 1: in-memory state)")
    startup_logger.info(f"   Loop:    {loop_impl}")
    startup_logger.info(f"   Reload:  {reload}")
    startup_logger.info(f"   Docs:    http://{host}:{port}/api/docs")
    startup_logger.info(f"   PID:     {os.getpid()}")
    startup_logger.info("=" * 60)

    # Записываем окружение в лог
    startup_logger.info("📋  Environment Configuration:")
    startup_logger.info(f"   FLASK_ENV={os.getenv('FLASK_ENV', 'not set')}")
    startup_logger.info(f"   UVICORN_MODE={mode}")
    startup_logger.info(f"   SERVER_HOST={host}")
    startup_logger.info(f"   SERVER_PORT={port}")
    startup_logger.info(f"   ALLOW_PUBLIC_BIND={os.getenv('ALLOW_PUBLIC_BIND', 'False')}")

    # Записываем Python окружение в лог
    startup_logger.info("🐍  Python Environment:")
    startup_logger.info(f"   PYTHON_VERSION={sys.version}")
    startup_logger.info(f"   PLATFORM={sys.platform}")
    startup_logger.info(f"   EXECUTABLE={sys.executable}")

    # Записываем ключевые пути в лог
    startup_logger.info("📁  Key Paths:")
    startup_logger.info(f"   PROJECT_ROOT={os.path.dirname(os.path.abspath(__file__))}")
    startup_logger.info(f"   PID_FILE={PID_FILE}")

    print("\n📋 Запуск сервера...")
    
    # Устанавливаем уровень логирования для uvicorn (чтобы не было слишком много шума)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    
    startup_logger.info("=" * 60)
    startup_logger.info("🚀  Starting Uvicorn Server...")
    startup_logger.info("=" * 60)

    uvicorn.run(
        'main:app',
        host=host,
        port=port,
        workers=workers,
        reload=reload,
        loop=loop_impl,
        http=http_impl,
        access_log=not is_prod,
        server_header=False,
        date_header=False,
        timeout_keep_alive=300,
        timeout_graceful_shutdown=30,
    )

    # После завершения uvicorn (или при аварийном завершении)
    startup_logger.info("=" * 60)
    startup_logger.info("🛑  Uvicorn Server Stopped")
    startup_logger.info("=" * 60)
