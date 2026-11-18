"""
main.py — FastAPI-приложение локального AI-ассистента.

Маршруты:
  /              — веб-чат (нужен вход)
  /login, /register, /forgot — страница входа с вкладками
  /auth/*        — регистрация, вход, коды, восстановление, выход
  /ai/*          — API ассистента
  /mcp/          — MCP-сервер для внешних ИИ (вход по коду из чата)
  /health        — проверка работы
"""
import asyncio
import contextlib
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import auth_store
from config import AUTH_ENABLED, CONFIG, SECRET_KEY, STATIC_FOLDER
from dependencies import current_user_id
from GCN.config_ai import GENERATED_IMAGES_DIR, LLM_BACKEND, LLAMACPP_AUTOSTART, LM_STUDIO_MODEL
from setup import setup_logging, get_rate_limit_stats
from routes.ai_assistant import (
    router as ai_router,
    start_global_merge_task,
    stop_global_merge_task,
    init_global_mcp,
    shutdown_all,
)
from routes.auth import router as auth_router
from routes.ui_state import router as ui_state_router
from routes.user_files import router as user_files_router
from routes.memory_viewer import router as memory_viewer_router
from routes.model_settings import router as model_settings_router
from mcp_server_blockcoin import mcp

setup_logging()
logger = logging.getLogger(__name__)
startup_logger = logging.getLogger("startup")  # Для отслеживания старта
logging.getLogger("routes.ai_assistant").setLevel(logging.INFO)
logging.getLogger("GCN.mcp_client_manager").setLevel(logging.INFO)

# Запуск через `uvicorn main:app` минует run.py — ставим Windows-фикс и здесь.
if sys.platform == 'win32':
    from win_asyncio_accept_fix import install as _install_win_asyncio_accept_fix
    _install_win_asyncio_accept_fix()


async def _autostart_llamacpp():
    """LLM_BACKEND=llamacpp и LLAMACPP_AUTOSTART=1: поднять модель по умолчанию при старте."""
    if LLM_BACKEND != "llamacpp" or not LLAMACPP_AUTOSTART:
        return
    await asyncio.sleep(2)
    from GCN.llm_client import ensure_model_loaded, list_models_cached
    from GCN.llamacpp_manager import normalize_model_id
    models = await list_models_cached(force=True)
    wanted = normalize_model_id(LM_STUDIO_MODEL)
    if wanted in models:
        target = wanted
    else:
        # Раньше тихий откат на models[0]: сервер грузил не ту модель, и страница потом
        # переключала её на нужную — выглядело как двойная загрузка. Теперь — явное предупреждение.
        target = models[0] if models else None
        logger.warning(f"llama-server autostart: модель {LM_STUDIO_MODEL!r} не найдена в MODELS_DIR; "
                       f"берём {target!r}. Доступные: {models[:10]}")
    if not target:
        logger.warning("llama-server autostart: в MODELS_DIR нет .gguf моделей")
        return
    startup_logger.info(f"[LLAMA] Загрузка модели по умолчанию: {target!r}")
    logger.info(f"llama-server autostart: стартовая загрузка {target!r} (инициатор: автозапуск)")
    res = await ensure_model_loaded(target)
    startup_logger.info(f"[LLAMA] Автозагрузка завершена: {res}")
    logger.info(f"llama-server autostart: {res}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Контекстный менеджер для жизненного цикла приложения.
    
    Запускает фоновые задачи при старте и останавливает их при завершении.
    Все события логируются в startup_logger для полного трекинга загрузки.
    """
    startup_logger.info("=" * 60)
    startup_logger.info("🚀  FastAPI Application Starting")
    startup_logger.info("=" * 60)
    
    logger.info("Starting AI assistant server...")
    startup_logger.info("[LIFESPAN] Началась инициализация приложения")

    if AUTH_ENABLED:
        auth_store.init_db()
        startup_logger.info("[AUTH] Инициализация базы данных пользователей завершена")

    start_global_merge_task()
    startup_logger.info("[MERGE] Запущена глобальная задача слияния памяти")

    async def _deferred_init_mcp():
        """Инициализация MCP-менеджера с небольшой задержкой."""
        await asyncio.sleep(1.5)
        try:
            await init_global_mcp()
            startup_logger.info("[MCP] MCP менеджер инициализирован")
            logger.info("MCP manager initialized")
        except Exception as e:
            startup_logger.error(f"[MCP] Ошибка инициализации MCP: {e}", exc_info=True)
            logger.error(f"Failed to initialize MCP: {e}", exc_info=True)

    async def _warm_models():
        """Прогрев моделей в отдельном потоке, чтобы не блокировать первый запрос."""
        await asyncio.sleep(0.5)
        try:
            startup_logger.info("[WARMUP] Начало прогрева моделей")
            await asyncio.to_thread(_warm_models_sync)
            startup_logger.info("[WARMUP] Модели прогреты")
            logger.info("Модели прогреты")
        except Exception as e:  # noqa: BLE001
            startup_logger.warning(f"[WARMUP] Прогрев моделей не выполнен (загрузятся по требованию): {e}")
            logger.warning(f"Прогрев моделей не выполнен (загрузятся по требованию): {e}")

    def _warm_models_sync():
        """Синхронная часть прогрева моделей."""
        from GCN.config_ai import MEMORY_USE_EMBEDDINGS, EMBEDDING_MODEL, NLI_MODEL
        startup_logger.info("[WARMUP] Проверка embedder: %s", EMBEDDING_MODEL)
        if MEMORY_USE_EMBEDDINGS:
            from GCN.memory_graph import CognitiveMemory
            CognitiveMemory._get_shared_embedder(EMBEDDING_MODEL)
            startup_logger.info("[WARMUP] Embedder загружен")
        startup_logger.info("[WARMUP] Проверка NLI модель: %s", NLI_MODEL)
        if NLI_MODEL:
            from GCN.nli import get_cross_encoder
            get_cross_encoder(NLI_MODEL)
            startup_logger.info("[WARMUP] Cross-encoder загружен")
        startup_logger.info("[WARMUP] Проверка Reranker")
        from GCN.web_search import Reranker
        Reranker._get()
        startup_logger.info("[WARMUP] Reranker загружен")

    async with mcp.session_manager.run():
        # Создаём задачи с именами для отслеживания в logs
        init_task = asyncio.create_task(_deferred_init_mcp(), name="mcp-init")
        autostart_task = asyncio.create_task(_autostart_llamacpp(), name="llama-autostart")
        warmup_task = asyncio.create_task(_warm_models(), name="model-warmup")

        startup_logger.info("=" * 60)
        startup_logger.info("📋  Background Tasks Started:")
        startup_logger.info(f"   [MCP]    _deferred_init_mcp() — инициализация MCP-менеджера")
        startup_logger.info(f"   [LLAMA]  _autostart_llamacpp() — автозагрузка модели по умолчанию")
        startup_logger.info(f"   [WARMUP] _warm_models()      — прогрев embedder, NLI и reranker")
        startup_logger.info("=" * 60)

        try:
            yield
        finally:
            # Отменяем фоновые задачи при завершении приложения
            init_task.cancel()
            autostart_task.cancel()
            warmup_task.cancel()
            
            startup_logger.info("=" * 60)
            startup_logger.info("🛑  FastAPI Application Shutting Down")
            startup_logger.info("=" * 60)

            await stop_global_merge_task()
            startup_logger.info("[MERGE] Остановлена глобальная задача слияния памяти")

            with contextlib.suppress(asyncio.CancelledError):
                await init_task
            with contextlib.suppress(asyncio.CancelledError):
                await autostart_task
            with contextlib.suppress(asyncio.CancelledError):
                await warmup_task

            await shutdown_all()
            startup_logger.info("[SHUTDOWN] Завершена остановка всех компонентов")

            # llama-server — наш дочерний процесс: останавливаем вместе с приложением
            from GCN.llamacpp_manager import shutdown_llamacpp
            await shutdown_llamacpp()
            startup_logger.info("[LLAMA] Остановлен llama-server, освобождён VRAM")

            logger.info("Shutdown complete")
            startup_logger.info("=" * 60)
            startup_logger.info("✅  FastAPI Application Shutdown Complete")
            startup_logger.info("=" * 60)


app = FastAPI(
    title='Local AI Assistant API',
    version='1.0.0',
    lifespan=lifespan,
    docs_url='/api/docs',
    redoc_url='/api/redoc',
    openapi_url='/api/openapi.json',
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost,http://127.0.0.1').split(','),
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
    # MCP Streamable HTTP возвращает идентификатор сессии в этом заголовке.
    # По умолчанию браузер скрывает от JS нестандартные заголовки ответа;
    # без явного expose_headers BlockChat не сможет прочитать Mcp-Session-Id
    # и не сможет отправить его обратно на следующем запросе (Missing session ID).
    expose_headers=['Mcp-Session-Id'],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie='session',
    max_age=CONFIG['SESSION_LIFETIME'],
    https_only=os.getenv('FLASK_ENV') == 'production',
    same_site='lax',
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={'error': 'Internal server error'})


class NoCacheStaticFiles(StaticFiles):
    """JS/CSS без кеша, чтобы после обновления не запускался старый скрипт."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith(('.js', '.css')):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response


os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)
app.mount('/generated_images', StaticFiles(directory=str(GENERATED_IMAGES_DIR)), name='generated_images')
if os.path.isdir(STATIC_FOLDER):
    app.mount('/static', NoCacheStaticFiles(directory=STATIC_FOLDER), name='static')
app.mount('/mcp', mcp.streamable_http_app())

app.include_router(ai_router)
app.include_router(auth_router)
app.include_router(ui_state_router)
app.include_router(user_files_router)
app.include_router(memory_viewer_router)
app.include_router(model_settings_router)


def _page(name: str):
    return FileResponse(os.path.join(STATIC_FOLDER, name), headers={'Cache-Control': 'no-cache, must-revalidate'})


@app.get('/', include_in_schema=False)
async def index(request: Request):
    if current_user_id(request) is None:
        return RedirectResponse(url='/login', status_code=302)
    return _page('ai.html')


@app.get('/login', include_in_schema=False)
@app.get('/register', include_in_schema=False)
@app.get('/forgot', include_in_schema=False)
async def auth_page(request: Request):
    if current_user_id(request) is not None:
        return RedirectResponse(url='/', status_code=302)
    return _page('auth.html')


@app.get('/manifest.json', include_in_schema=False)
async def manifest():
    return FileResponse(os.path.join(STATIC_FOLDER, 'manifest.json'),
                        media_type='application/manifest+json',
                        headers={'Cache-Control': 'no-cache, must-revalidate'})


@app.get('/sw.js', include_in_schema=False)
async def service_worker():
    # service worker должен отдаваться из корня, чтобы его область действия была весь сайт
    return FileResponse(os.path.join(STATIC_FOLDER, 'sw.js'),
                        media_type='application/javascript',
                        headers={'Cache-Control': 'no-cache, no-store, must-revalidate',
                                 'Service-Worker-Allowed': '/'})


@app.get('/offline', include_in_schema=False)
async def offline_page():
    return _page('offline.html')


@app.get('/health', tags=['health'])
async def health_check():
    return {'status': 'ok', 'rate_limits': get_rate_limit_stats()}