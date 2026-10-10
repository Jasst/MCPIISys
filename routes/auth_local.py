"""
routes/auth_local.py — вход по паролю (включается заданием APP_PASSWORD).
"""
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import APP_PASSWORD, LOCAL_USER_ID
from setup import RateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/auth', tags=['auth'])

# 10 попыток входа за 10 минут с одного адреса
_login_limiter = RateLimiter(max_requests=10, window_seconds=600)


class LoginBody(BaseModel):
    password: str


def _client_ip(request: Request) -> str:
    # За nginx реальный адрес приходит в X-Real-IP
    return request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')


@router.get('/status')
async def auth_status(request: Request):
    required = bool(APP_PASSWORD)
    authed = (not required) or request.session.get('auth') == LOCAL_USER_ID
    return {'required': required, 'authenticated': authed}


@router.post('/login')
async def login(body: LoginBody, request: Request):
    if not APP_PASSWORD:
        return {'ok': True}
    allowed, _ = _login_limiter.is_allowed(_client_ip(request))
    if not allowed:
        raise HTTPException(status_code=429, detail='Слишком много попыток. Подождите 10 минут.')
    if not hmac.compare_digest(body.password.encode('utf-8'), APP_PASSWORD.encode('utf-8')):
        logger.warning(f'Неудачная попытка входа с {_client_ip(request)}')
        raise HTTPException(status_code=401, detail='Неверный пароль')
    request.session['auth'] = LOCAL_USER_ID
    return {'ok': True}


@router.post('/logout')
async def logout(request: Request):
    request.session.clear()
    return {'ok': True}
