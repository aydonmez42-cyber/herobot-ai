"""
Multi-user authentication + per-user Binance API credential storage.

This is PHASE 1 of "let each connected user log into their own dashboard"
(the user's request). It covers accounts, sessions, and safely storing a
user's Binance API key/secret. It deliberately does NOT place any order —
that is PHASE 2 (a live-trading execution engine), kept separate on purpose
because it handles real money and deserves its own careful build/verify
pass rather than being bundled into an auth system.

Security model
---------------
- Passwords: PBKDF2-HMAC-SHA256, 260k iterations, random 16-byte salt per
  user. Stdlib only (hashlib), no extra dependency, no plaintext password
  ever stored.
- Sessions: a random 32-byte token is the only thing sent to the browser
  (as an HttpOnly cookie). The token itself is looked up server-side in
  SESSIONS_FILE, so a session can be revoked (logout) without needing any
  cryptographic trick — deleting the server-side entry is enough.
- Binance credentials: encrypted at rest with Fernet (AES128-CBC + HMAC,
  from the `cryptography` package) using a server-side master key read from
  the CREDENTIAL_ENCRYPTION_KEY environment variable. If that env var is
  missing, credential saving is refused outright rather than silently
  falling back to plaintext storage — a missing key must fail loudly, not
  quietly weaken security.
- The decrypted API secret is only ever handed to server-side code (the
  future live-trading engine); every function that returns account info to
  the dashboard/browser returns a MASKED key only (e.g. "AbCd...WxYz"),
  never the full key and never the secret.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import requests

DATA_DIR = os.environ.get('PAPER_DATA_DIR', '')
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.environ.get('USERS_FILE', os.path.join(DATA_DIR, 'users.json') if DATA_DIR else 'users.json')
SESSIONS_FILE = os.environ.get('SESSIONS_FILE', os.path.join(DATA_DIR, 'sessions.json') if DATA_DIR else 'sessions.json')

SESSION_TTL_SECONDS = int(os.environ.get('SESSION_TTL_SECONDS', str(30 * 24 * 3600)))  # 30 days
PBKDF2_ITERATIONS = 260_000

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level JSON store helpers (mirrors state_store.py's read/write pattern)
# ---------------------------------------------------------------------------

def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def _hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def _verify_password(password, salt_hex, hash_hex):
    _, computed = _hash_password(password, salt_hex)
    return hmac.compare_digest(computed, hash_hex)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN = 8

# ---------------------------------------------------------------------------
# Subscription / trial / admin
# ---------------------------------------------------------------------------
TRIAL_DAYS = int(os.environ.get('TRIAL_DAYS', '7'))
# Comma-separated list of usernames/emails (case-insensitive) that are always
# admins, e.g. "aydonmez42@gmail.com,ops". The very first account ever
# registered on a fresh install is also made admin automatically, so there is
# always at least one admin without needing to set this env var up front.
ADMIN_USERNAMES = {u.strip().lower() for u in os.environ.get('ADMIN_USERNAMES', '').split(',') if u.strip()}


def _valid_username(u):
    return bool(u) and USERNAME_MIN <= len(u) <= USERNAME_MAX and all(c.isalnum() or c in '_.-' for c in u)


def _should_be_admin(key, users_before):
    return key in ADMIN_USERNAMES or len(users_before) == 0


def _new_trial_fields():
    now = datetime.now(timezone.utc)
    trial_end = now.timestamp() + TRIAL_DAYS * 24 * 3600
    return {
        'trial_ends_at': datetime.fromtimestamp(trial_end, tz=timezone.utc).isoformat(),
        'payment_status': 'trial',  # 'trial' | 'active' | 'expired' | 'inactive'
    }


def register_user(username, password, email=''):
    username = (username or '').strip()
    email = (email or '').strip()
    if not _valid_username(username):
        return False, f'Kullanıcı adı {USERNAME_MIN}-{USERNAME_MAX} karakter olmalı, sadece harf/rakam/._- içerebilir.'
    if not password or len(password) < PASSWORD_MIN:
        return False, f'Şifre en az {PASSWORD_MIN} karakter olmalı.'
    with _lock:
        users = _read_json(USERS_FILE, {})
        key = username.lower()
        if key in users:
            return False, 'Bu kullanıcı adı zaten alınmış.'
        salt_hex, hash_hex = _hash_password(password)
        is_admin = _should_be_admin(key, users)
        users[key] = {
            'username': username,
            'email': email,
            'salt': salt_hex,
            'password_hash': hash_hex,
            'auth_provider': 'local',
            'google_sub': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'binance_api_key_encrypted': None,
            'binance_api_secret_encrypted': None,
            'binance_key_masked': None,
            'binance_verified_at': None,
            'binance_verify_error': None,
            'live_trading_enabled': False,
            'risk_ack_at': None,
            'is_admin': is_admin,
            **_new_trial_fields(),
        }
        _write_json(USERS_FILE, users)
    return True, None


def authenticate(username, password):
    users = _read_json(USERS_FILE, {})
    rec = users.get((username or '').strip().lower())
    if not rec:
        return False, 'Kullanıcı adı veya şifre hatalı.'
    if not rec.get('password_hash'):
        return False, 'Bu hesap "Auth0 ile devam et" ile oluşturuldu. Lütfen o seçenekle giriş yapın.'
    if not _verify_password(password, rec['salt'], rec['password_hash']):
        return False, 'Kullanıcı adı veya şifre hatalı.'
    return True, None


def get_user(username):
    users = _read_json(USERS_FILE, {})
    return users.get((username or '').strip().lower())


def _update_user(username, mutate_fn):
    with _lock:
        users = _read_json(USERS_FILE, {})
        key = (username or '').strip().lower()
        if key not in users:
            return None
        mutate_fn(users[key])
        _write_json(USERS_FILE, users)
        return users[key]


# ---------------------------------------------------------------------------
# Auth0 sign-in ("Auth0 ile devam et")
# ---------------------------------------------------------------------------
# Standard server-side OAuth2 "Authorization Code" flow against an Auth0
# tenant. Auth0 hosts the actual login screen (Universal Login) — including,
# if you enable it in the Auth0 dashboard, a "Sign in with Google" button —
# so this app never touches a Google/Facebook/etc. consent screen directly
# and never sees the person's password for any of those. We only exchange
# the one-time authorization code for tokens and read the verified profile
# (email, sub) from Auth0's own /userinfo endpoint.
AUTH0_DOMAIN = os.environ.get('AUTH0_DOMAIN', '').strip().rstrip('/').replace('https://', '').replace('http://', '')
AUTH0_CLIENT_ID = os.environ.get('AUTH0_CLIENT_ID', '').strip()
AUTH0_CLIENT_SECRET = os.environ.get('AUTH0_CLIENT_SECRET', '').strip()
AUTH0_CALLBACK_URL = os.environ.get('AUTH0_CALLBACK_URL', '').strip()  # e.g. https://www.herobot-ai.com/callback


def auth0_enabled():
    return bool(AUTH0_DOMAIN and AUTH0_CLIENT_ID and AUTH0_CLIENT_SECRET and AUTH0_CALLBACK_URL)


def build_auth0_authorize_url(state):
    from urllib.parse import urlencode
    params = {
        'response_type': 'code',
        'client_id': AUTH0_CLIENT_ID,
        'redirect_uri': AUTH0_CALLBACK_URL,
        'scope': 'openid profile email',
        'state': state,
    }
    return f'https://{AUTH0_DOMAIN}/authorize?' + urlencode(params)


def _exchange_auth0_code(code):
    try:
        r = requests.post(
            f'https://{AUTH0_DOMAIN}/oauth/token',
            json={
                'grant_type': 'authorization_code',
                'client_id': AUTH0_CLIENT_ID,
                'client_secret': AUTH0_CLIENT_SECRET,
                'code': code,
                'redirect_uri': AUTH0_CALLBACK_URL,
            },
            timeout=10,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json().get('access_token')
    except Exception:
        return None


def _auth0_userinfo(access_token):
    if not access_token:
        return None
    try:
        r = requests.get(f'https://{AUTH0_DOMAIN}/userinfo', headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    email = (data.get('email') or '').strip()
    sub = (data.get('sub') or '').strip()
    if not email or not sub:
        return None
    if data.get('email_verified') is False:
        return None
    return {'email': email, 'sub': sub, 'name': data.get('name') or email}


def login_or_register_auth0(code):
    """Exchanges an Auth0 authorization code for the person's verified
    profile and logs them in, creating an account on first sign-in. Returns
    (ok, username, error)."""
    if not auth0_enabled():
        return False, None, 'Sunucuda Auth0 tanımlı değil — bu giriş yöntemi şu an kapalı.'
    access_token = _exchange_auth0_code(code)
    if not access_token:
        return False, None, 'Auth0 girişi doğrulanamadı. Lütfen tekrar deneyin.'
    info = _auth0_userinfo(access_token)
    if not info:
        return False, None, 'Auth0 profili okunamadı. Lütfen tekrar deneyin.'
    key = info['email'].lower()
    with _lock:
        users = _read_json(USERS_FILE, {})
        if key in users:
            # Existing account (created via Auth0 or otherwise) — just log in.
            if users[key].get('auth0_sub') != info['sub']:
                users[key]['auth0_sub'] = info['sub']
                _write_json(USERS_FILE, users)
            return True, users[key]['username'], None
        is_admin = _should_be_admin(key, users)
        users[key] = {
            'username': info['email'],
            'email': info['email'],
            'salt': None,
            'password_hash': None,
            'auth_provider': 'auth0',
            'google_sub': None,
            'auth0_sub': info['sub'],
            'created_at': datetime.now(timezone.utc).isoformat(),
            'binance_api_key_encrypted': None,
            'binance_api_secret_encrypted': None,
            'binance_key_masked': None,
            'binance_verified_at': None,
            'binance_verify_error': None,
            'live_trading_enabled': False,
            'risk_ack_at': None,
            'is_admin': is_admin,
            **_new_trial_fields(),
        }
        _write_json(USERS_FILE, users)
    return True, info['email'], None


# ---------------------------------------------------------------------------
# Subscription status (trial / paid / expired) — payment collection itself is
# manual for now (admin marks a user active after receiving payment outside
# the app), this just tracks and enforces the resulting state.
# ---------------------------------------------------------------------------

def subscription_status(rec):
    if not rec:
        return {'status': 'expired', 'trial_ends_at': None, 'days_left': 0}
    if rec.get('is_admin'):
        return {'status': 'active', 'trial_ends_at': rec.get('trial_ends_at'), 'days_left': None}
    if rec.get('payment_status') == 'active':
        return {'status': 'active', 'trial_ends_at': rec.get('trial_ends_at'), 'days_left': None}
    trial_ends_at = rec.get('trial_ends_at')
    if trial_ends_at:
        try:
            ends = datetime.fromisoformat(trial_ends_at)
            days_left = (ends - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            days_left = -1
    else:
        days_left = -1
    if days_left > 0:
        return {'status': 'trial', 'trial_ends_at': trial_ends_at, 'days_left': round(days_left, 1)}
    return {'status': 'expired', 'trial_ends_at': trial_ends_at, 'days_left': 0}


def has_active_access(username):
    rec = get_user(username)
    return subscription_status(rec)['status'] in ('active', 'trial')


def ack_risk(username):
    _update_user(username, lambda u: u.update(risk_ack_at=datetime.now(timezone.utc).isoformat()))


# ---------------------------------------------------------------------------
# Admin — list users and manually flip payment status once you've confirmed
# a bank transfer/payment outside the app.
# ---------------------------------------------------------------------------

def is_admin(username):
    rec = get_user(username)
    return bool(rec and rec.get('is_admin'))


def list_users_admin():
    users = _read_json(USERS_FILE, {})
    out = []
    for key, rec in users.items():
        sub = subscription_status(rec)
        out.append({
            'username': rec.get('username'),
            'email': rec.get('email'),
            'auth_provider': rec.get('auth_provider', 'local'),
            'created_at': rec.get('created_at'),
            'is_admin': bool(rec.get('is_admin')),
            'binance_connected': bool(rec.get('binance_api_key_encrypted')),
            'binance_verified_at': rec.get('binance_verified_at'),
            'payment_status': rec.get('payment_status'),
            'subscription_status': sub['status'],
            'trial_ends_at': sub['trial_ends_at'],
            'days_left': sub['days_left'],
        })
    out.sort(key=lambda x: x.get('created_at') or '', reverse=True)
    return out


def set_payment_status(username, status):
    if status not in ('trial', 'active', 'expired', 'inactive'):
        return False, 'Geçersiz durum.'
    if _update_user(username, lambda u: u.update(payment_status=status)) is None:
        return False, 'Kullanıcı bulunamadı.'
    return True, None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(username):
    token = secrets.token_urlsafe(32)
    with _lock:
        sessions = _read_json(SESSIONS_FILE, {})
        sessions[token] = {
            'username': (username or '').strip().lower(),
            'created_at': time.time(),
            'expires_at': time.time() + SESSION_TTL_SECONDS,
        }
        _write_json(SESSIONS_FILE, sessions)
    return token


def get_session_user(token):
    """Returns the username for a valid, non-expired session token, else None."""
    if not token:
        return None
    sessions = _read_json(SESSIONS_FILE, {})
    rec = sessions.get(token)
    if not rec:
        return None
    if time.time() > rec.get('expires_at', 0):
        delete_session(token)
        return None
    return rec.get('username')


def delete_session(token):
    if not token:
        return
    with _lock:
        sessions = _read_json(SESSIONS_FILE, {})
        sessions.pop(token, None)
        _write_json(SESSIONS_FILE, sessions)


# ---------------------------------------------------------------------------
# Binance credential encryption
# ---------------------------------------------------------------------------

def _fernet():
    key = os.environ.get('CREDENTIAL_ENCRYPTION_KEY', '').strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode('utf-8'))
    except Exception:
        return None


def credential_encryption_ready():
    return _fernet() is not None


def _mask_key(api_key):
    if not api_key or len(api_key) < 8:
        return '****'
    return f'{api_key[:4]}...{api_key[-4:]}'


def save_binance_credentials(username, api_key, api_secret, risk_ack=False):
    f = _fernet()
    if f is None:
        return False, 'Sunucuda CREDENTIAL_ENCRYPTION_KEY tanımlı değil — API anahtarları güvenle şifrelenemediği için kaydedilmedi.'
    rec = get_user(username)
    already_acked = bool(rec and rec.get('risk_ack_at'))
    if not already_acked and not risk_ack:
        return False, 'Devam etmeden önce risk onayı kutusunu işaretlemeniz gerekiyor.'
    api_key = (api_key or '').strip()
    api_secret = (api_secret or '').strip()
    if not api_key or not api_secret:
        return False, 'API key ve secret gerekli.'
    enc_key = f.encrypt(api_key.encode('utf-8')).decode('utf-8')
    enc_secret = f.encrypt(api_secret.encode('utf-8')).decode('utf-8')

    def m(u):
        u['binance_api_key_encrypted'] = enc_key
        u['binance_api_secret_encrypted'] = enc_secret
        u['binance_key_masked'] = _mask_key(api_key)
        u['binance_verified_at'] = None
        u['binance_verify_error'] = None
        u['live_trading_enabled'] = False  # re-verify required after any key change
        if not u.get('risk_ack_at'):
            u['risk_ack_at'] = datetime.now(timezone.utc).isoformat()

    if _update_user(username, m) is None:
        return False, 'Kullanıcı bulunamadı.'
    return True, None


def get_decrypted_binance_credentials(username):
    """Server-side only — the live-trading engine calls this, the dashboard
    API never does. Returns (api_key, api_secret) or (None, None)."""
    f = _fernet()
    if f is None:
        return None, None
    rec = get_user(username)
    if not rec or not rec.get('binance_api_key_encrypted'):
        return None, None
    try:
        api_key = f.decrypt(rec['binance_api_key_encrypted'].encode('utf-8')).decode('utf-8')
        api_secret = f.decrypt(rec['binance_api_secret_encrypted'].encode('utf-8')).decode('utf-8')
        return api_key, api_secret
    except Exception:
        return None, None


def clear_binance_credentials(username):
    def m(u):
        u['binance_api_key_encrypted'] = None
        u['binance_api_secret_encrypted'] = None
        u['binance_key_masked'] = None
        u['binance_verified_at'] = None
        u['binance_verify_error'] = None
        u['live_trading_enabled'] = False
    _update_user(username, m)


# ---------------------------------------------------------------------------
# Read-only Binance key verification — confirms the key/secret work and can
# read futures account info. Deliberately calls only a read-only endpoint
# (account balance); it never places, cancels or modifies any order.
# ---------------------------------------------------------------------------

BINANCE_FAPI_URL = os.environ.get('BINANCE_FAPI_URL', 'https://fapi.binance.com')


def _binance_signed_get(path, api_key, api_secret, params=None, timeout=15):
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    signature = hmac.new(api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f'{BINANCE_FAPI_URL}{path}?{query}&signature={signature}'
    headers = {'X-MBX-APIKEY': api_key}
    return requests.get(url, headers=headers, timeout=timeout)


def verify_binance_key(username):
    """Calls Binance Futures' read-only balance endpoint with the user's
    stored, decrypted credentials to confirm they actually work. Updates the
    user record with the result. Returns (ok, error)."""
    api_key, api_secret = get_decrypted_binance_credentials(username)
    if not api_key or not api_secret:
        return False, 'Önce bir API key/secret kaydedin.'
    try:
        r = _binance_signed_get('/fapi/v2/balance', api_key, api_secret)
    except requests.RequestException as e:
        err = f'Binance bağlantı hatası: {type(e).__name__}'
        _update_user(username, lambda u: u.update(binance_verify_error=err))
        return False, err
    if r.status_code != 200:
        try:
            msg = r.json().get('msg', r.text[:200])
        except Exception:
            msg = r.text[:200]
        err = f'Binance HTTP {r.status_code}: {msg}'
        _update_user(username, lambda u: u.update(binance_verify_error=err))
        return False, err

    def m(u):
        u['binance_verified_at'] = datetime.now(timezone.utc).isoformat()
        u['binance_verify_error'] = None
    _update_user(username, m)
    return True, None


def get_account_status(username):
    """Read-only snapshot for the dashboard's account panel — never includes
    the actual key or secret, only whether one is connected/verified."""
    rec = get_user(username) or {}
    sub = subscription_status(rec)
    return {
        'username': rec.get('username'),
        'email': rec.get('email'),
        'auth_provider': rec.get('auth_provider', 'local'),
        'binance_connected': bool(rec.get('binance_api_key_encrypted')),
        'binance_key_masked': rec.get('binance_key_masked'),
        'binance_verified_at': rec.get('binance_verified_at'),
        'binance_verify_error': rec.get('binance_verify_error'),
        'live_trading_enabled': bool(rec.get('live_trading_enabled')),
        'credential_encryption_ready': credential_encryption_ready(),
        'risk_ack_at': rec.get('risk_ack_at'),
        'is_admin': bool(rec.get('is_admin')),
        'subscription_status': sub['status'],
        'trial_ends_at': sub['trial_ends_at'],
        'days_left': sub['days_left'],
        'auth0_login_enabled': auth0_enabled(),
    }
