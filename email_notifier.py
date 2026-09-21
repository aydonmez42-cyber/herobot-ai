"""
Transactional email — currently only "şifremi unuttum" reset links.

Uses plain SMTP (stdlib smtplib), not a vendor SDK, so it works with any
provider (Gmail app password, SendGrid/Mailgun/Postmark SMTP relay, a
company mail server, etc.) purely through environment variables:

  SMTP_HOST        e.g. smtp.gmail.com / smtp.sendgrid.net
  SMTP_PORT        default 587 (STARTTLS). Use 465 for implicit TLS.
  SMTP_USER        SMTP auth username
  SMTP_PASSWORD    SMTP auth password / app password / API key
  SMTP_FROM        "From" address shown to the recipient (default: SMTP_USER)
  SMTP_FROM_NAME   display name for the From header (default: "Herobot-ai")
  SMTP_USE_SSL     "true" -> connect with implicit TLS (smtplib.SMTP_SSL)
                    instead of plaintext-then-STARTTLS. Default: false.

If SMTP_HOST/SMTP_USER/SMTP_PASSWORD aren't set, sending is soft-disabled:
callers get (False, 'not configured') instead of a crash, so a missing mail
setup can never take down the login/reset flow or any other route.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

SMTP_HOST = os.environ.get('SMTP_HOST', '').strip()
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '').strip()
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '').strip()
SMTP_FROM = (os.environ.get('SMTP_FROM', '').strip() or SMTP_USER)
SMTP_FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'Herobot-ai').strip()
SMTP_USE_SSL = os.environ.get('SMTP_USE_SSL', '').strip().lower() in ('1', 'true', 'yes')

EMAIL_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM)


def _send(to_email, subject, body_text):
    if not EMAIL_ENABLED:
        return False, 'not configured'
    msg = MIMEText(body_text, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg['To'] = to_email
    try:
        if SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        try:
            if not SMTP_USE_SSL:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        finally:
            server.quit()
        return True, None
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def send_password_reset_email(to_email, reset_url):
    subject = 'Herobot-ai — Şifre sıfırlama'
    body = (
        f'Merhaba,\n\n'
        f'Herobot-ai hesabınız için bir şifre sıfırlama talebi aldık. '
        f'Şifrenizi sıfırlamak için aşağıdaki bağlantıya tıklayın:\n\n'
        f'{reset_url}\n\n'
        f'Bu bağlantı 30 dakika içinde geçerliliğini yitirir ve yalnızca bir kez kullanılabilir.\n\n'
        f'Bu talebi siz oluşturmadıysanız bu e-postayı yok sayabilirsiniz; '
        f'şifreniz değiştirilmeyecektir.\n\n'
        f'— Herobot-ai'
    )
    return _send(to_email, subject, body)
