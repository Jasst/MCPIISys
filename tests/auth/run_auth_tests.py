"""
Тесты авторизации: регистрация, коды, вход, блокировка, восстановление, сессии.
Запуск из папки проекта:  python tests/auth/run_auth_tests.py
Письма не отправляются: код перехватывается в словарь SENT.
"""
import os
import sys
import tempfile
import time
import sqlite3

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ['AUTH_DB_PATH'] = os.path.join(_tmp, 'test_auth.db')
os.environ['SECRET_KEY'] = 'x' * 40
os.environ['AUTH_DISABLED'] = '0'
os.environ['AUTH_DEV_MODE'] = '0'

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

import auth_store as store  # noqa: E402
import mailer  # noqa: E402
import routes.auth as auth_routes  # noqa: E402
from config import SECRET_KEY  # noqa: E402
from dependencies import require_auth  # noqa: E402
from setup import RateLimiter  # noqa: E402

SENT = {}  # email -> последний код


def fake_send(to, code, purpose):
    SENT[(to, purpose)] = code


mailer.send_code = fake_send
# Лимиты запросов отдельно проверим в конце, здесь они не мешают сценариям
auth_routes._code_limiter = RateLimiter(max_requests=10_000)
auth_routes._login_limiter = RateLimiter(max_requests=10_000)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie='session')
app.include_router(auth_routes.router)


@app.get('/probe')
async def probe(uid: str = Depends(require_auth)):
    return {'uid': uid}


store.init_db()
results = []


def check(name, ok, extra=''):
    results.append(bool(ok))
    print('PASS' if ok else 'FAIL', name, extra)


def fresh_client():
    return TestClient(app)


def set_expiry_past(email):
    conn = sqlite3.connect(os.environ['AUTH_DB_PATH'])
    conn.execute('UPDATE codes SET expires_at = ? WHERE email = ?', (time.time() - 1, email))
    conn.commit(); conn.close()


def clear_cooldown(email, purpose):
    conn = sqlite3.connect(os.environ['AUTH_DB_PATH'])
    conn.execute('UPDATE codes SET created_at = ? WHERE email = ? AND purpose = ?',
                 (time.time() - 3600, email, purpose))
    conn.commit(); conn.close()


A = 'anna@example.com'
PW = 'correct-horse-1'

# ---------------------------------------------------------------- 1. защита и регистрация
c = fresh_client()
r = c.get('/probe')
check('1 protected route needs login', r.status_code == 401)

r = c.post('/auth/register', json={'email': A, 'password': 'short'})
check('2 short password refused', r.status_code == 400, r.json().get('detail', ''))

r = c.post('/auth/register', json={'email': 'not-an-email', 'password': PW})
check('3 bad email refused', r.status_code == 400)

r = c.post('/auth/register', json={'email': A, 'password': PW})
check('4 register sends code', r.status_code == 200 and (A, 'verify') in SENT)

r = c.post('/auth/login', json={'email': A, 'password': PW})
check('5 unverified account cannot login (403)', r.status_code == 403, r.json().get('detail', ''))

r = c.post('/auth/register/confirm', json={'email': A, 'code': '000000'})
check('6 wrong code refused', r.status_code == 400)

code = SENT[(A, 'verify')]
r = c.post('/auth/register/confirm', json={'email': A, 'code': code})
check('7 right code confirms and logs in', r.status_code == 200)
uid_a = c.get('/probe').json().get('uid')
check('8 protected route works after confirm, user id is u<N>', uid_a and uid_a.startswith('u'), uid_a)

r = c.post('/auth/register/confirm', json={'email': A, 'code': code})
check('9 code cannot be reused', r.status_code == 400)

# ---------------------------------------------------------------- 2. вход и выход
c.post('/auth/logout')
check('10 logout closes access', c.get('/probe').status_code == 401)

r = c.post('/auth/login', json={'email': A, 'password': 'wrong-password'})
check('11 wrong password refused', r.status_code == 401)

r = c.post('/auth/login', json={'email': A, 'password': PW})
check('12 right password logs in', r.status_code == 200)
check('13 same user id after login', c.get('/probe').json().get('uid') == uid_a)

st = c.get('/auth/status').json()
check('14 status shows email when logged in', st['authenticated'] and st['email'] == A, str(st))

# ---------------------------------------------------------------- 3. пользователь не раскрывается
r1 = fresh_client().post('/auth/register', json={'email': A, 'password': 'another-pass-99'})
check('15 existing verified email: same answer, no new code sent',
      r1.status_code == 200 and r1.json()['message'] == auth_routes.SENT_MESSAGE)
SENT.pop((A, 'verify'), None)
fresh_client().post('/auth/register', json={'email': A, 'password': 'another-pass-99'})
check('16 no code sent to existing account', (A, 'verify') not in SENT)

r = fresh_client().post('/auth/login', json={'email': 'nobody@example.com', 'password': PW})
check('17 unknown email gives same 401 as wrong password', r.status_code == 401 and 'Неверный email' in r.json()['detail'])

# ---------------------------------------------------------------- 4. блокировка после ошибок
B = 'boris@example.com'
cb = fresh_client()
cb.post('/auth/register', json={'email': B, 'password': PW})
cb.post('/auth/register/confirm', json={'email': B, 'code': SENT[(B, 'verify')]})
cb.post('/auth/logout')
codes = [cb.post('/auth/login', json={'email': B, 'password': 'bad-password-x'}).status_code for _ in range(5)]
check('18 five wrong passwords -> 401 each', codes == [401] * 5, str(codes))
r = cb.post('/auth/login', json={'email': B, 'password': PW})
check('19 locked: right password refused with 429', r.status_code == 429, r.json().get('detail', ''))

# ---------------------------------------------------------------- 5. коды: срок и попытки
D = 'dina@example.com'
cd = fresh_client()
cd.post('/auth/register', json={'email': D, 'password': PW})
set_expiry_past(D)
r = cd.post('/auth/register/confirm', json={'email': D, 'code': SENT[(D, 'verify')]})
check('20 expired code refused', r.status_code == 400)

clear_cooldown(D, 'verify')
cd.post('/auth/code/resend', json={'email': D, 'purpose': 'verify'})
good = SENT[(D, 'verify')]
bad = [cd.post('/auth/register/confirm', json={'email': D, 'code': '111111'}).status_code for _ in range(5)]
r = cd.post('/auth/register/confirm', json={'email': D, 'code': good})
check('21 after 5 wrong attempts even right code is dead', bad == [400] * 5 and r.status_code == 400)

clear_cooldown(D, 'verify')
cd.post('/auth/code/resend', json={'email': D, 'purpose': 'verify'})
r = cd.post('/auth/code/resend', json={'email': D, 'purpose': 'verify'})
check('22 resend within 60 s refused (429)', r.status_code == 429, r.json().get('detail', ''))

# ---------------------------------------------------------------- 6. восстановление пароля
E = 'egor@example.com'
NEWPW = 'brand-new-pass-7'
ce = fresh_client()
ce.post('/auth/register', json={'email': E, 'password': PW})
ce.post('/auth/register/confirm', json={'email': E, 'code': SENT[(E, 'verify')]})
ce.post('/auth/logout')
old_session = fresh_client()
old_session.post('/auth/login', json={'email': E, 'password': PW})
check('23 second device logged in', old_session.get('/probe').status_code == 200)

SENT.pop((E, 'reset'), None)
r = fresh_client().post('/auth/reset/request', json={'email': E})
check('24 reset request answered and code sent', r.status_code == 200 and (E, 'reset') in SENT)

r = fresh_client().post('/auth/reset/request', json={'email': 'ghost@example.com'})
check('25 reset for unknown email: same answer, nothing sent', r.status_code == 200 and ('ghost@example.com', 'reset') not in SENT)

r = fresh_client().post('/auth/reset/confirm', json={'email': E, 'code': '123456', 'password': NEWPW})
check('26 wrong reset code refused', r.status_code == 400)

r = fresh_client().post('/auth/reset/confirm', json={'email': E, 'code': SENT[(E, 'reset')], 'password': 'short'})
check('27 weak new password refused before code is used', r.status_code == 400)

r = fresh_client().post('/auth/reset/confirm', json={'email': E, 'code': SENT[(E, 'reset')], 'password': NEWPW})
check('28 reset with right code', r.status_code == 200)

check('29 old device session is dead after reset', old_session.get('/probe').status_code == 401)
r = fresh_client().post('/auth/login', json={'email': E, 'password': PW})
check('30 old password no longer works', r.status_code == 401)
r = fresh_client().post('/auth/login', json={'email': E, 'password': NEWPW})
check('31 new password works', r.status_code == 200)

# ---------------------------------------------------------------- 7. лимиты запросов кодов по IP
limited = AUTH_LIMITED = RateLimiter(max_requests=2, window_seconds=600)
auth_routes._code_limiter = limited
rl = fresh_client()
codes = [rl.post('/auth/reset/request', json={'email': 'x@example.com'}).status_code for _ in range(3)]
check('32 code requests limited per address (429 on third)', codes == [200, 200, 429], str(codes))

passed = sum(results)
print(f'\n{passed}/{len(results)} passed')
sys.exit(0 if passed == len(results) else 1)
