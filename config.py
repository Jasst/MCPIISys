"""
config.py — настройки сервера. Настройки LLM, памяти и генерации изображений
находятся в GCN/config_ai.py.
"""
import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / '.env', override=False)
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
STATIC_FOLDER = str(BASE_DIR / 'static')

# Режим авторизации:
#   local   — один пользователь, без входа (по умолчанию, для self-hosted)
#   session — сессия с адресом, который выставляет свой механизм входа
AUTH_MODE = os.getenv('AUTH_MODE', 'local').strip().lower()
LOCAL_USER_ID = os.getenv('LOCAL_USER_ID', 'local_user').strip()

# Если SECRET_KEY не задан, генерируем случайный на каждый запуск.
# Для режима local это безопасно: сессии нужны только для совместимости.
SECRET_KEY = os.getenv('SECRET_KEY') or secrets.token_hex(32)
if len(SECRET_KEY) < 32:
    raise ValueError(f'SECRET_KEY must be at least 32 characters, got {len(SECRET_KEY)}')

CONFIG = {
    'SESSION_LIFETIME':              int(os.getenv('SESSION_LIFETIME', 60 * 60 * 24 * 7)),
    'LOG_MAX_BYTES':                 10 * 1024 * 1024,
    'LOG_BACKUP_COUNT':              5,
    'RATE_LIMIT_PER_MINUTE':         int(os.getenv('RATE_LIMIT_PER_MINUTE', 60)),
    'RATE_LIMIT_MESSAGE_PER_MINUTE': int(os.getenv('RATE_LIMIT_MESSAGE_PER_MINUTE', 30)),
}
