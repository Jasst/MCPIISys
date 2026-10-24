"""
auth_store.py — пользователи, пароли и коды подтверждения (SQLite, стандартная библиотека).

Пароли: scrypt с солью. Коды: 6 цифр, в базе хранится только HMAC-хеш,
действуют 10 минут, после 5 неверных попыток сгорают, новый код — не раньше чем через 60 с.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import base64
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config import AUTH_DB_PATH, SECRET_KEY

CODE_TTL = 600
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SEC = 60
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 900
MIN_PASSWORD = 8
MAX_PASSWORD = 128
_EMAIL_RE = re.compile(r'^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$')

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_email ON codes(email, purpose);
CREATE TABLE IF NOT EXISTS mcp_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    code_enc TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    last_used_ip TEXT,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mcp_codes_uid ON mcp_codes(uid);
"""


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400, retry_after: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _run(sql, args=(), *, one=False, commit=False):
    with _lock:
        conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, args)
            result = cur.fetchone() if one else cur.fetchall()
            last_id = cur.lastrowid
            if commit:
                conn.commit()
            return result, last_id
        finally:
            conn.close()


def init_db():
    with _lock:
        conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False, timeout=10)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- email и пароли

def normalize_email(raw: str) -> str:
    e = (raw or '').strip().lower()
    if len(e) > 254 or not _EMAIL_RE.match(e):
        raise AuthError('Некорректный email', 400)
    return e


def check_password_policy(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD:
        raise AuthError(f'Пароль должен быть не короче {MIN_PASSWORD} символов', 400)
    if len(password) > MAX_PASSWORD:
        raise AuthError('Пароль слишком длинный', 400)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f'scrypt${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split('$')
        digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt_hex),
                                n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# Выполняется при входе на несуществующий email, чтобы время ответа не выдавало,
# есть ли аккаунт.
_DUMMY_HASH = hash_password('dummy-password-for-timing')


def dummy_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


# ---------------------------------------------------------------- пользователи

def get_user(email: str):
    row, _ = _run('SELECT * FROM users WHERE email = ?', (email,), one=True)
    return row


def get_user_by_id(uid: int):
    row, _ = _run('SELECT * FROM users WHERE id = ?', (uid,), one=True)
    return row


def create_pending_user(email: str, password: str) -> None:
    _run('INSERT INTO users (email, password_hash, verified, created_at) VALUES (?, ?, 0, ?)',
         (email, hash_password(password), time.time()), commit=True)


def set_pending_password(uid: int, password: str) -> None:
    _run('UPDATE users SET password_hash = ? WHERE id = ? AND verified = 0',
         (hash_password(password), uid), commit=True)


def mark_verified(uid: int) -> None:
    _run('UPDATE users SET verified = 1 WHERE id = ?', (uid,), commit=True)


def set_password(uid: int, password: str) -> None:
    # session_version + 1: все старые сессии этого пользователя становятся недействительными
    _run('UPDATE users SET password_hash = ?, session_version = session_version + 1, '
         'failed_logins = 0, locked_until = 0 WHERE id = ?',
         (hash_password(password), uid), commit=True)


def register_login_failure(uid: int) -> None:
    user = get_user_by_id(uid)
    fails = (user['failed_logins'] or 0) + 1
    if fails >= LOGIN_MAX_FAILS:
        _run('UPDATE users SET failed_logins = 0, locked_until = ? WHERE id = ?',
             (time.time() + LOGIN_LOCK_SEC, uid), commit=True)
    else:
        _run('UPDATE users SET failed_logins = ? WHERE id = ?', (fails, uid), commit=True)


def reset_login_failures(uid: int) -> None:
    _run('UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?', (uid,), commit=True)


def locked_seconds(user) -> int:
    left = (user['locked_until'] or 0) - time.time()
    return int(left) + 1 if left > 0 else 0


# ---------------------------------------------------------------- коды

def _code_hash(email: str, purpose: str, code: str) -> str:
    msg = f'{purpose}|{email}|{code}'.encode('utf-8')
    return hmac.new(SECRET_KEY.encode('utf-8'), msg, hashlib.sha256).hexdigest()


def new_code(email: str, purpose: str) -> str:
    """Создаёт код и сохраняет его хеш. Возвращает код для отправки письмом."""
    last, _ = _run('SELECT created_at FROM codes WHERE email = ? AND purpose = ? '
                   'ORDER BY id DESC LIMIT 1', (email, purpose), one=True)
    if last is not None:
        wait = int(CODE_RESEND_SEC - (time.time() - last['created_at']))
        if wait > 0:
            raise AuthError(f'Код уже отправлен. Повторно можно запросить через {wait} с',
                            429, retry_after=wait)
    _run('DELETE FROM codes WHERE email = ? AND purpose = ?', (email, purpose), commit=True)
    code = f'{secrets.randbelow(10 ** 6):06d}'
    _run('INSERT INTO codes (email, purpose, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)',
         (email, purpose, _code_hash(email, purpose, code), time.time() + CODE_TTL, time.time()),
         commit=True)
    return code


def drop_codes(email: str, purpose: str) -> None:
    _run('DELETE FROM codes WHERE email = ? AND purpose = ?', (email, purpose), commit=True)


def check_code(email: str, purpose: str, code: str) -> bool:
    row, _ = _run('SELECT * FROM codes WHERE email = ? AND purpose = ? ORDER BY id DESC LIMIT 1',
                  (email, purpose), one=True)
    if row is None:
        return False
    if time.time() > row['expires_at'] or row['attempts'] >= CODE_MAX_ATTEMPTS:
        drop_codes(email, purpose)
        return False
    _run('UPDATE codes SET attempts = attempts + 1 WHERE id = ?', (row['id'],), commit=True)
    if hmac.compare_digest(row['code_hash'], _code_hash(email, purpose, (code or '').strip())):
        drop_codes(email, purpose)
        return True
    return False


# ---------------------------------------------------------------- постоянные MCP-коды
# Код входа для внешнего ИИ живёт до ручного отзыва и переживает перезапуск.
# В БД лежат только HMAC-хеш (для проверки) и шифротекст (чтобы показать код
# в профиле на любом устройстве). Ключ — отдельный файл рядом с auth.db, а не
# SECRET_KEY: тот без .env генерируется заново при каждом запуске.

_mcp_log = logging.getLogger("auth_store.mcp")
_MCP_KEY_PATH = Path(os.getenv("MCP_CODE_KEY_PATH", str(Path(AUTH_DB_PATH).parent / "mcp_code.key")))
_mcp_secret_cache: Optional[bytes] = None


def _mcp_secret() -> bytes:
    global _mcp_secret_cache
    if _mcp_secret_cache is None:
        env = os.getenv("MCP_CODE_SECRET", "").strip()
        if env:
            _mcp_secret_cache = env.encode("utf-8")
        else:
            with _lock:
                if not _MCP_KEY_PATH.exists():
                    _MCP_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        fd = os.open(_MCP_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "w") as f:
                            f.write(secrets.token_hex(32))
                    except FileExistsError:
                        pass
                _mcp_secret_cache = _MCP_KEY_PATH.read_text(encoding="utf-8").strip().encode("utf-8")
    return _mcp_secret_cache


def _mcp_hash(code: str) -> str:
    return hmac.new(_mcp_secret(), code.encode("utf-8"), hashlib.sha256).hexdigest()


def _mcp_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(_mcp_secret() + b"|enc").digest())
    return Fernet(key)


def mcp_code_store(uid: str, code: str) -> None:
    """Сохраняет новый постоянный код. Прежние активные коды uid отзываются."""
    now = time.time()
    _run("UPDATE mcp_codes SET revoked_at=? WHERE uid=? AND revoked_at IS NULL",
         (now, uid), commit=True)
    enc = _mcp_fernet().encrypt(code.encode("utf-8")).decode("ascii")
    _run("INSERT INTO mcp_codes (uid, code_hash, code_enc, created_at) VALUES (?,?,?,?)",
         (uid, _mcp_hash(code), enc, now), commit=True)


def mcp_code_get(uid: str) -> Optional[str]:
    """Текущий активный код uid (расшифрованный) или None."""
    row, _ = _run(
        "SELECT code_enc FROM mcp_codes WHERE uid=? AND revoked_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (uid,), one=True)
    if row is None:
        return None
    try:
        return _mcp_fernet().decrypt(row["code_enc"].encode("ascii")).decode("utf-8")
    except InvalidToken:
        _mcp_log.error("Не удалось расшифровать MCP-код: ключ изменён. Выпустите новый код.")
        return None


def mcp_code_consume(code: str, ip: str = "") -> Optional[str]:
    """Проверяет код (уже нормализованный). Возвращает uid или None. Код не расходуется."""
    row, _ = _run(
        "SELECT id, uid FROM mcp_codes WHERE code_hash=? AND revoked_at IS NULL",
        (_mcp_hash(code),), one=True)
    if row is None:
        return None
    _run("UPDATE mcp_codes SET last_used_at=?, last_used_ip=? WHERE id=?",
         (time.time(), ip or None, row["id"]), commit=True)
    return row["uid"]


def mcp_code_revoke(uid: str) -> int:
    """Отзывает все активные коды uid. Возвращает количество отозванных."""
    rows, _ = _run("SELECT id FROM mcp_codes WHERE uid=? AND revoked_at IS NULL", (uid,))
    if not rows:
        return 0
    _run("UPDATE mcp_codes SET revoked_at=? WHERE uid=? AND revoked_at IS NULL",
         (time.time(), uid), commit=True)
    return len(rows)