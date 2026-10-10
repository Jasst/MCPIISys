"""
main.py — FastAPI-приложение локального AI-ассистента.
"""
import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config import CONFIG, SECRET_KEY, STATIC_FOLDER
from GCN.config_ai import GENERATED_IMAGES_DIR
from setup import setup_logging, get_rate_limit_stats
from routes.ai_assistant import (
    router as ai_router,
    start_global_merge_task,
    init_global_mcp,
    shutdown_all,
)

setup_logging()
logger = logging.getLogger(__name__)
logging.getLogger("routes.ai_assistant").setLevel(logging.INFO)
logging.getLogger("GCN.mcp_client_manager").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI assistant server...")
    start_global_merge_task()

    async def _deferred_init_mcp():
        await asyncio.sleep(1.5)  # uvicorn успевает открыть порт
        try:
            await init_global_mcp()
            logger.info("MCP manager initialized")
        except Exception as e:
            logger.error(f"Failed to initialize MCP: {e}", exc_info=True)

    init_task = asyncio.create_task(_deferred_init_mcp(), name="mcp-init")
    try:
        yield
    finally:
        init_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await init_task
        # Сохраняем память всех ассистентов, иначе изменения при остановке теряются
        await shutdown_all()
        logger.info("Shutdown complete")


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

app.include_router(ai_router)


@app.get('/', include_in_schema=False)
async def index():
    return FileResponse(
        os.path.join(STATIC_FOLDER, 'ai.html'),
        headers={'Cache-Control': 'no-cache, must-revalidate'},
    )


@app.get('/health', tags=['health'])
async def health_check():
    return {'status': 'ok', 'rate_limits': get_rate_limit_stats()}
