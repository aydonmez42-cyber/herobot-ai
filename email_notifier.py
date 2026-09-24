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

# Shared inbox that gets: trial-expiry notices, "Admin'e soru sor" questions,
# and "tutarı gönderdim" subscription-payment notices. Overridable via env
# var in case the business address ever changes.
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL', 'herobotai.int@gmail.com').strip()
# Mirrors auth.TRIAL_DAYS (same env var) — kept independent rather than
# imported so this module has no dependency on auth.py.
TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', '7'))

EMAIL_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM)


def _send(to_email, subject, body_text, reply_to=None):
    if not EMAIL_ENABLED:
        return False, 'not configured'
    msg = MIMEText(body_text, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg['To'] = to_email
    if reply_to:
        msg['Reply-To'] = reply_to
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


def send_trial_expired_notice(username, user_email):
    """Fired once, by the background trial-expiry loop in dashboard.py, the
    moment a non-admin user's free trial runs out — so the admin knows to
    follow up about a subscription without having to check the admin panel
    proactively."""
    subject = f'Herobot-ai — Deneme süresi doldu: {username}'
    body = (
        f'Merhaba,\n\n'
        f'"{username}" kullanıcısının ({user_email or "e-posta yok"}) '
        f'{TRIAL_DAYS} günlük ücretsiz deneme süresi az önce doldu.\n\n'
        f'Bu kullanıcı artık panele erişemeyecek; abone olması için "Abonelik '
        f'Sistemine Geç" seçeneğini kullanması gerekiyor. Ödeme bildirimi '
        f'geldiğinde ayrıca bir e-posta ile bilgilendirileceksiniz.\n\n'
        f'Admin panelinden kullanıcıyı "Aktif" yaparak erişimini manuel '
        f'olarak da açabilirsiniz.\n\n'
        f'— Herobot-ai sistemi'
    )
    return _send(ADMIN_NOTIFY_EMAIL, subject, body)


def send_admin_question(username, user_email, message):
    """"Admin'e soru sor" formundan gelen soruyu ADMIN_NOTIFY_EMAIL'e iletir.
    Reply-To kullanıcının kendi e-postasına ayarlanır (varsa) ki admin
    doğrudan "yanıtla" diyerek kullanıcıya cevap yazabilsin."""
    subject = f'Herobot-ai — Kullanıcı sorusu: {username}'
    body = (
        f'"{username}" kullanıcısından ({user_email or "e-posta belirtilmemiş"}) '
        f'yeni bir soru geldi:\n\n'
        f'{"-" * 40}\n{message}\n{"-" * 40}\n\n'
        f'Bu kullanıcıya doğrudan yanıt vermek için bu e-postayı yanıtlayabilirsiniz'
        + (f' (Yanıtla adresi otomatik olarak {user_email} olarak ayarlandı).' if user_email else '.')
    )
    return _send(ADMIN_NOTIFY_EMAIL, subject, body, reply_to=(user_email or None))


def send_subscription_payment_notice(username, user_email, plan_label, amount_usd):
    """"Tutarı gönderdim" butonuna basıldığında ADMIN_NOTIFY_EMAIL'e gider —
    gerçek para transferini asla otomatik doğrulamaz, sadece admin'e
    "şu kullanıcı şu tutarı gönderdiğini bildirdi, banka hesabını kontrol et"
    der. Hesabı "aktif" yapmak admin panelinden hâlâ admin'in elindedir."""
    subject = f'Herobot-ai — Abonelik ödeme bildirimi: {username} ({plan_label}, {amount_usd} USD)'
    body = (
        f'"{username}" kullanıcısı ({user_email or "e-posta belirtilmemiş"}) '
        f'{plan_label} abonelik bedeli olan {amount_usd} USD tutarını IBAN\'a '
        f'gönderdiğini bildirdi.\n\n'
        f'Lütfen banka hesabınızı kontrol edin ve tutar/açıklama eşleştiğinde '
        f'admin panelinden bu kullanıcıyı "Aktif (ödedi)" olarak işaretleyin.\n\n'
        f'Not: Bu bildirim yalnızca kullanıcının beyanıdır — ödeme sistem '
        f'tarafından otomatik doğrulanmaz.\n\n'
        f'— Herobot-ai sistemi'
    )
    return _send(ADMIN_NOTIFY_EMAIL, subject, body, reply_to=(user_email or None))
