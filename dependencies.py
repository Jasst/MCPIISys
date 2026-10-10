"""
dependencies.py — авторизация для AI-роутов.
"""
import os

from fastapi import HTTPException, Request, status

from config import AUTH_MODE, LOCAL_USER_ID


def require_auth(request: Request) -> str:
    """Возвращает user_id. В режиме local — фиксированный, иначе из сессии."""
    if AUTH_MODE == 'local':
        return LOCAL_USER_ID
    address = request.session.get('address')
    if not address:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    return address
