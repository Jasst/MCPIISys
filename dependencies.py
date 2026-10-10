"""
dependencies.py — кто делает запрос.

AUTH_DISABLED=1: все запросы идут как LOCAL_USER_ID.
Иначе: пользователь берётся из сессии. Сессия действует, пока не сменился
session_version пользователя (смена пароля отключает все старые входы).
"""
from fastapi import HTTPException, Request, status

import auth_store
from config import AUTH_ENABLED, LOCAL_USER_ID


def current_user(request: Request):
    """Строка пользователя из БД или None, если сессии нет или она устарела."""
    uid = request.session.get('uid')
    version = request.session.get('v')
    if not uid:
        return None
    user = auth_store.get_user_by_id(uid)
    if user is None or not user['verified'] or user['session_version'] != version:
        return None
    return user


def current_user_id(request: Request):
    if not AUTH_ENABLED:
        return LOCAL_USER_ID
    user = current_user(request)
    return f"u{user['id']}" if user is not None else None


def require_auth(request: Request) -> str:
    """Возвращает идентификатор для папки памяти, например 'u1'. Иначе 401."""
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    return uid
