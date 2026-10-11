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

# ---------- Авторизация ----------
# По умолчанию вход по email и паролю. AUTH_DISABLED=1 — локальный режим без входа
# (только для своей машины, все запросы идут как LOCAL_USER_ID).
AUTH_DISABLED = os.getenv('AUTH_DISABLED', '0') == '1'
AUTH_ENABLED = not AUTH_DISABLED
AUTH_DB_PATH = os.getenv('AUTH_DB_PATH', str(BASE_DIR / 'auth.db'))
LOCAL_USER_ID = os.getenv('LOCAL_USER_ID', 'local_user').strip()
# В dev-режиме коды подтверждения пишутся в лог вместо отправки письма.
# В продакшене всегда 0: коды в логах — это утечка.
AUTH_DEV_MODE = os.getenv('AUTH_DEV_MODE', '0') == '1'
APP_NAME = os.getenv('APP_NAME', 'AI-ассистент').strip()

# ---------- Почта (SMTP) ----------
SMTP_HOST = os.getenv('SMTP_HOST', '').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '').strip()
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_FROM = os.getenv('SMTP_FROM', '').strip()
SMTP_STARTTLS = os.getenv('SMTP_STARTTLS', '1') == '1'   # для порта 587

# Способ отправки: smtp (по умолчанию), resend или brevo.
# resend/brevo работают через HTTPS (порт 443), его обычно не блокируют,
# в отличие от SMTP-портов 465/587.
MAIL_PROVIDER = os.getenv('MAIL_PROVIDER', 'smtp').strip().lower()   # smtp | resend | brevo | unisender_go
RESEND_API_KEY = os.getenv('RESEND_API_KEY', '').strip()
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '').strip()
UNISENDER_GO_API_KEY = os.getenv('UNISENDER_GO_API_KEY', '').strip()
# Адрес зависит от дата-центра, в котором зарегистрирован аккаунт: go1 или go2
UNISENDER_GO_API_URL = os.getenv('UNISENDER_GO_API_URL',
    'https://go1.unisender.ru/ru/transactional/api/v1').strip().rstrip('/')
# порт 465 использует SSL с самого начала (SMTP_SSL), это определяется по порту

# Если SECRET_KEY не задан, генерируем случайный на каждый запуск.
# Тогда при перезапуске все сессии сбрасываются — для продакшена задайте постоянный ключ.
SECRET_KEY = os.getenv('SECRET_KEY') or secrets.token_hex(32)
if len(SECRET_KEY) < 32:
    raise ValueError(f'SECRET_KEY must be at least 32 characters, got {len(SECRET_KEY)}')

CONFIG = {
    'SESSION_LIFETIME':              int(os.getenv('SESSION_LIFETIME', 60 * 60 * 24 * 14)),
    'LOG_MAX_BYTES':                 10 * 1024 * 1024,
    'LOG_BACKUP_COUNT':              5,
    'RATE_LIMIT_PER_MINUTE':         int(os.getenv('RATE_LIMIT_PER_MINUTE', 60)),
    'RATE_LIMIT_MESSAGE_PER_MINUTE': int(os.getenv('RATE_LIMIT_MESSAGE_PER_MINUTE', 30)),
}
