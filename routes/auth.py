"""
routes/auth.py — регистрация, вход, подтверждение, восстановление пароля, выход.

Ответы на регистрацию и восстановление одинаковые независимо от того,
есть ли такой аккаунт, чтобы по форме нельзя было узнать чужие адреса.
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth_store as store
import mailer
from config import AUTH_ENABLED, LOCAL_USER_ID
from setup import RateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/auth', tags=['auth'])

_code_limiter = RateLimiter(max_requests=10, window_seconds=600)   # запросы кодов с одного адреса
_login_limiter = RateLimiter(max_requests=30, window_seconds=600)  # попытки входа с одного адреса

SENT_MESSAGE = 'Если адрес можно использовать, мы отправили на него код.'


class RegisterBody(BaseModel):
    email: str
    password: str


class EmailBody(BaseModel):
    email: str


class ResendBody(BaseModel):
    email: str
    purpose: str   # 'verify' или 'reset'


class ConfirmBody(BaseModel):
    email: str
    code: str


class ResetConfirmBody(BaseModel):
    email: str
    code: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


def _ip(request: Request) -> str:
    return request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')


def _limit(limiter: RateLimiter, request: Request) -> None:
    allowed, _ = limiter.is_allowed(_ip(request))
    if not allowed:
        raise HTTPException(429, detail='Слишком много запросов. Попробуйте позже.',
                            headers={'Retry-After': '600'})


def _fail(e: store.AuthError):
    headers = {'Retry-After': str(e.retry_after)} if e.retry_after else None
    raise HTTPException(e.status, detail=e.message, headers=headers)


def _log_in(request: Request, user) -> None:
    request.session['uid'] = user['id']
    request.session['v'] = user['session_version']


async def _send(email: str, code: str, purpose: str) -> None:
    try:
        await asyncio.to_thread(mailer.send_code, email, code, purpose)
    except mailer.MailError as e:
        store.drop_codes(email, purpose)
        raise HTTPException(503, detail=str(e) + '. Попробуйте позже.')


# ---------------------------------------------------------------- регистрация

@router.post('/register')
async def register(body: RegisterBody, request: Request):
    _limit(_code_limiter, request)
    try:
        email = store.normalize_email(body.email)
        store.check_password_policy(body.password)
        user = store.get_user(email)
        if user is not None and user['verified']:
            # Аккаунт уже есть — отвечаем так же, письма не шлём
            return {'ok': True, 'message': SENT_MESSAGE}
        if user is not None:
            store.set_pending_password(user['id'], body.password)
        else:
            store.create_pending_user(email, body.password)
        code = store.new_code(email, 'verify')
    except store.AuthError as e:
        _fail(e)
    await _send(email, code, 'verify')
    return {'ok': True, 'message': SENT_MESSAGE}


@router.post('/register/confirm')
async def register_confirm(body: ConfirmBody, request: Request):
    try:
        email = store.normalize_email(body.email)
    except store.AuthError as e:
        _fail(e)
    user = store.get_user(email)
    if user is None or not store.check_code(email, 'verify', body.code):
        raise HTTPException(400, detail='Неверный или просроченный код')
    store.mark_verified(user['id'])
    user = store.get_user_by_id(user['id'])
    _log_in(request, user)
    return {'ok': True}


@router.post('/code/resend')
async def resend_code(body: ResendBody, request: Request):
    _limit(_code_limiter, request)
    if body.purpose not in ('verify', 'reset'):
        raise HTTPException(400, detail='Неизвестный тип кода')
    try:
        email = store.normalize_email(body.email)
    except store.AuthError:
        return {'ok': True, 'message': SENT_MESSAGE}
    user = store.get_user(email)
    relevant = user is not None and (
        (body.purpose == 'verify' and not user['verified']) or
        (body.purpose == 'reset' and user['verified'])
    )
    if relevant:
        try:
            code = store.new_code(email, body.purpose)
        except store.AuthError as e:
            _fail(e)
        await _send(email, code, body.purpose)
    return {'ok': True, 'message': SENT_MESSAGE}


# ---------------------------------------------------------------- вход и выход

@router.post('/login')
async def login(body: LoginBody, request: Request):
    _limit(_login_limiter, request)
    try:
        email = store.normalize_email(body.email)
    except store.AuthError:
        store.dummy_verify(body.password)
        raise HTTPException(401, detail='Неверный email или пароль')

    user = store.get_user(email)
    if user is None:
        store.dummy_verify(body.password)
        raise HTTPException(401, detail='Неверный email или пароль')

    left = store.locked_seconds(user)
    if left:
        raise HTTPException(429, detail=f'Слишком много неудачных попыток. Подождите {left // 60 + 1} мин.',
                            headers={'Retry-After': str(left)})

    if not store.verify_password(body.password, user['password_hash']):
        store.register_login_failure(user['id'])
        raise HTTPException(401, detail='Неверный email или пароль')

    if not user['verified']:
        raise HTTPException(403, detail='Email не подтверждён. Введите код из письма.')

    store.reset_login_failures(user['id'])
    _log_in(request, user)
    return {'ok': True}


@router.post('/logout')
async def logout(request: Request):
    request.session.clear()
    return {'ok': True}


@router.get('/status')
async def auth_status(request: Request):
    if not AUTH_ENABLED:
        return {'required': False, 'authenticated': True, 'email': None, 'uid': LOCAL_USER_ID}
    from dependencies import current_user
    user = current_user(request)
    return {
        'required': True,
        'authenticated': user is not None,
        'email': user['email'] if user is not None else None,
        'uid': f"u{user['id']}" if user is not None else None,
    }


# ---------------------------------------------------------------- восстановление

@router.post('/reset/request')
async def reset_request(body: EmailBody, request: Request):
    _limit(_code_limiter, request)
    try:
        email = store.normalize_email(body.email)
    except store.AuthError:
        return {'ok': True, 'message': SENT_MESSAGE}
    user = store.get_user(email)
    if user is not None and user['verified']:
        try:
            code = store.new_code(email, 'reset')
        except store.AuthError as e:
            _fail(e)
        try:
            await asyncio.to_thread(mailer.send_code, email, code, 'reset')
        except mailer.MailError:
            store.drop_codes(email, 'reset')
            logger.error('Не удалось отправить код восстановления')
    return {'ok': True, 'message': SENT_MESSAGE}


@router.post('/reset/confirm')
async def reset_confirm(body: ResetConfirmBody, request: Request):
    _limit(_login_limiter, request)
    try:
        email = store.normalize_email(body.email)
        store.check_password_policy(body.password)
    except store.AuthError as e:
        _fail(e)
    user = store.get_user(email)
    if user is None or not user['verified'] or not store.check_code(email, 'reset', body.code):
        raise HTTPException(400, detail='Неверный или просроченный код')
    store.set_password(user['id'], body.password)
    # Старые сессии уже недействительны (session_version изменилась), входим заново
    return {'ok': True}
