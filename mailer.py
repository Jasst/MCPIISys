"""
mailer.py — отправка писем с кодами через SMTP.
Работает с любым провайдером: Gmail, Яндекс, Brevo, Resend, Mailgun и т.п.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from config import APP_NAME, AUTH_DEV_MODE, SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_STARTTLS, SMTP_USER

logger = logging.getLogger(__name__)


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

    if not SMTP_HOST:
        if AUTH_DEV_MODE:
            logger.warning(f'[DEV] код ({purpose}) для {to}: {code}')
            return
        raise MailError('Почта не настроена на сервере')

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM or SMTP_USER
    msg['To'] = to
    msg.set_content(text)

    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with server:
            if SMTP_PORT != 465 and SMTP_STARTTLS:
                server.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f'Ошибка отправки письма: {e}')
        raise MailError('Не удалось отправить письмо') from e
