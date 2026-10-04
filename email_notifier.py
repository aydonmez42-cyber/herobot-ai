"""
Transactional email — şifre sıfırlama, admin soruları, deneme süresi ve
abonelik ödeme bildirimleri.

Resend HTTP API kullanır (SMTP DEĞİL). Railway SMTP portlarını engelleyebildiği
için HTTP tabanlı gönderim daha güvenilirdir. Ek paket gerekmez, yalnızca
standart kütüphane kullanılır. Ayarlar ortam değişkenleriyle yapılır:

  RESEND_API_KEY      Resend panelinden alınan API anahtarı (zorunlu)
  RESEND_FROM         Gönderen. Varsayılan: "Herobot-ai <onboarding@resend.dev>"
                      (kendi domaininizi Resend'de doğruladıysanız
                      "Herobot-ai <info@alanadiniz.com>" yapın)
  ADMIN_NOTIFY_EMAIL  Admin bildirimlerinin gideceği adres. Yoksa NOTIFY_EMAIL,
                      o da yoksa herobotai.int@gmail.com kullanılır.
  TRIAL_DAYS          Ücretsiz deneme günü (varsayılan 7)

NOT: Resend'de domain doğrulanmadıysa, onboarding@resend.dev ile yalnızca
Resend hesabını açtığınız e-posta adresine mail gönderilebilir. Kullanıcılara
giden mailler (ör. şifre sıfırlama) için domain doğrulaması gerekir.

RESEND_API_KEY tanımlı değilse gönderim devre dışı kalır: çağıranlar
(False, 'not configured') alır, uygulama çökmez.
"""
import json
import os
import urllib.error
import urllib.request

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
RESEND_FROM = os.environ.get(
    'RESEND_FROM', 'Herobot-ai <onboarding@resend.dev>').strip()

# Shared inbox that gets: trial-expiry notices, "Admin'e soru sor" questions,
# and "tutarı gönderdim" subscription-payment notices.
ADMIN_NOTIFY_EMAIL = (os.environ.get('ADMIN_NOTIFY_EMAIL')
                      or os.environ.get('NOTIFY_EMAIL')
                      or 'herobotai.int@gmail.com').strip()
# Mirrors auth.TRIAL_DAYS (same env var) — kept independent rather than
# imported so this module has no dependency on auth.py.
TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', '7'))

EMAIL_ENABLED = bool(RESEND_API_KEY)


def _send(to_email, subject, body_text, reply_to=None):
    """Resend API ile düz metin mail gönderir. (başarılı_mı, hata_metni) döner."""
    if not EMAIL_ENABLED:
        print('MAIL | RESEND_API_KEY tanimli degil', flush=True)
        return False, 'not configured'

    payload = {
        'from': RESEND_FROM,
        'to': [to_email],
        'subject': subject,
        'text': body_text,
    }
    if reply_to:
        payload['reply_to'] = reply_to

    req = urllib.request.Request(
        'https://api.resend.com/emails',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {RESEND_API_KEY}',
            'Content-Type': 'application/json',
            'User-Agent': 'herobot-ai/1.0',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            print(f'MAIL | gonderildi -> {to_email} | {subject}', flush=True)
            return True, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        print(f'MAIL | Resend hata {e.code}: {detail}', flush=True)
        return False, f'HTTP {e.code}: {detail}'
    except Exception as e:
        print(f'MAIL | istisna: {type(e).__name__}: {e}', flush=True)
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
    "şu kullanıcı şu tutarı gönderdiğini bildirdi, cüzdanını kontrol et"
    der. Hesabı "aktif" yapmak admin panelinden hâlâ admin'in elindedir."""
    subject = f'Herobot-ai — Abonelik ödeme bildirimi: {username} ({plan_label}, {amount_usd} USD)'
    body = (
        f'"{username}" kullanıcısı ({user_email or "e-posta belirtilmemiş"}) '
        f'{plan_label} abonelik bedeli olan {amount_usd} USD tutarını '
        f'gönderdiğini bildirdi.\n\n'
        f'Lütfen cüzdanınızı/hesabınızı kontrol edin ve tutar eşleştiğinde '
        f'admin panelinden bu kullanıcıyı "Aktif (ödedi)" olarak işaretleyin.\n\n'
        f'Not: Bu bildirim yalnızca kullanıcının beyanıdır — ödeme sistem '
        f'tarafından otomatik doğrulanmaz.\n\n'
        f'— Herobot-ai sistemi'
    )
    return _send(ADMIN_NOTIFY_EMAIL, subject, body, reply_to=(user_email or None))
