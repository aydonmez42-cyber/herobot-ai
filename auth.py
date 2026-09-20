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


def _valid_username(u):
    return bool(u) and USERNAME_MIN <= len(u) <= USERNAME_MAX and all(c.isalnum() or c in '_.-' for c in u)


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
        users[key] = {
            'username': username,
            'email': email,
            'salt': salt_hex,
            'password_hash': hash_hex,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'binance_api_key_encrypted': None,
            'binance_api_secret_encrypted': None,
            'binance_key_masked': None,
            'binance_verified_at': None,
            'binance_verify_error': None,
            'live_trading_enabled': False,
            'risk_ack_at': None,
        }
        _write_json(USERS_FILE, users)
    return True, None


def authenticate(username, password):
    users = _read_json(USERS_FILE, {})
    rec = users.get((username or '').strip().lower())
    if not rec:
        return False, 'Kullanıcı adı veya şifre hatalı.'
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


def save_binance_credentials(username, api_key, api_secret):
    f = _fernet()
    if f is None:
        return False, 'Sunucuda CREDENTIAL_ENCRYPTION_KEY tanımlı değil — API anahtarları güvenle şifrelenemediği için kaydedilmedi.'
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
    return {
        'username': rec.get('username'),
        'email': rec.get('email'),
        'binance_connected': bool(rec.get('binance_api_key_encrypted')),
        'binance_key_masked': rec.get('binance_key_masked'),
        'binance_verified_at': rec.get('binance_verified_at'),
        'binance_verify_error': rec.get('binance_verify_error'),
        'live_trading_enabled': bool(rec.get('live_trading_enabled')),
        'credential_encryption_ready': credential_encryption_ready(),
    }
