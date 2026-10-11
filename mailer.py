"""
mailer.py — отправка писем с кодами.

MAIL_PROVIDER:
  smtp   — по SMTP (порты 465/587). Может не работать, если провайдер блокирует порты.
  resend — HTTPS API сервиса Resend (порт 443). Нужен RESEND_API_KEY и подтверждённый домен.
  brevo  — HTTPS API сервиса Brevo (порт 443). Нужен BREVO_API_KEY и подтверждённый отправитель.
  unisender_go — HTTPS API российского сервиса Unisender Go. Нужен UNISENDER_GO_API_KEY и домен отправителя.
"""
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

from config import (APP_NAME, AUTH_DEV_MODE, BREVO_API_KEY, MAIL_PROVIDER, RESEND_API_KEY,
                    SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_STARTTLS, SMTP_USER,
                    UNISENDER_GO_API_KEY, UNISENDER_GO_API_URL)

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15
USER_AGENT = 'ai-assistant/1.0'   # без него Resend отклоняет запросы из Python


class MailError(Exception):
    pass


SUBJECTS = {
    'verify': 'Подтверждение регистрации',
    'reset': 'Восстановление пароля',
}


def send_code(to: str, code: str, purpose: str) -> None:
    subject = f'{APP_NAME}: {SUBJECTS[purpose]}'
    text = (
        f'Ваш код: {code}\n\n'
        'Код действует 10 минут. Никому его не сообщайте.\n'
        'Если вы не запрашивали код, просто проигнорируйте письмо.'
    )
    sender = SMTP_FROM or SMTP_USER

    if MAIL_PROVIDER == 'resend':
        _send_resend(sender, to, subject, text)
    elif MAIL_PROVIDER == 'brevo':
        _send_brevo(sender, to, subject, text)
    elif MAIL_PROVIDER == 'unisender_go':
        _send_unisender_go(sender, to, subject, text)
    elif SMTP_HOST:
        _send_smtp(sender, to, subject, text)
    elif AUTH_DEV_MODE:
        logger.warning(f'[DEV] код ({purpose}) для {to}: {code}')
    else:
        raise MailError('Почта не настроена на сервере')


def _post_json(url: str, headers: dict, payload: dict, provider: str) -> None:
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        logger.error(f'{provider}: ошибка {e.code}: {body!r}')
        raise MailError('Сервис почты отклонил письмо. Проверьте ключ и отправителя') from e
    except (urllib.error.URLError, OSError) as e:
        logger.error(f'{provider}: нет соединения: {e}')
        raise MailError('Не удалось отправить письмо') from e


def _send_resend(sender: str, to: str, subject: str, text: str) -> None:
    if not RESEND_API_KEY:
        raise MailError('Не задан RESEND_API_KEY')
    _post_json(
        'https://api.resend.com/emails',
        {'Authorization': f'Bearer {RESEND_API_KEY}'},
        {'from': sender, 'to': [to], 'subject': subject, 'text': text},
        'Resend',
    )


def _send_brevo(sender: str, to: str, subject: str, text: str) -> None:
    if not BREVO_API_KEY:
        raise MailError('Не задан BREVO_API_KEY')
    _post_json(
        'https://api.brevo.com/v3/smtp/email',
        {'api-key': BREVO_API_KEY},
        {'sender': {'email': sender}, 'to': [{'email': to}], 'subject': subject, 'textContent': text},
        'Brevo',
    )


def _send_unisender_go(sender: str, to: str, subject: str, text: str) -> None:
    if not UNISENDER_GO_API_KEY:
        raise MailError('Не задан UNISENDER_GO_API_KEY')
    _post_json(
        f'{UNISENDER_GO_API_URL}/email/send.json',
        {'X-API-KEY': UNISENDER_GO_API_KEY},
        {'message': {
            'recipients': [{'email': to}],
            'body': {'plaintext': text},
            'subject': subject,
            'from_email': sender,
        }},
        'Unisender Go',
    )


def _send_smtp(sender: str, to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to
    msg.set_content(text)
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
        with server:
            if SMTP_PORT != 465 and SMTP_STARTTLS:
                server.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f'Ошибка отправки письма: {e}')
        raise MailError('Не удалось отправить письмо') from e
