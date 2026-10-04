"""
Binance rate-limit koruyucusu.

Neden var: Binance, IP basina dakikalik istek agirligi limitini asinca 429 doner;
429 sonrasi istek gondermeye devam eden IP'leri 418 ile (2 dakikadan gunlere kadar)
banlar. Bu modul sunlari yapar:

  1. Herkese acik piyasa verisi (klines, ticker, exchangeInfo...) isteklerini
     saniyede en fazla BINANCE_MAX_REQ_PER_SEC (varsayilan 5) hizla gonderir.
  2. 429/418 gelince Retry-After suresi kadar TUM piyasa verisi isteklerini durdurur
     (istek gondermeden BinanceCooldown hatasi firlatir).
  3. X-MBX-USED-WEIGHT-1M basligini izler; limite yaklasinca otomatik yavaslar.

GUVENLIK: Emir/hesap/pozisyon gibi imzali ve islem uc noktalarina (POST, DELETE,
signature iceren istekler, /order, /account, /positionRisk vb.) DOKUNMAZ; onlar
hic engellenmez, sadece cevaplari izlenir. Boylece gercek pozisyon kapatma
istekleri bu koruma yuzunden asla beklemez.

Kullanim: bir kez `import binance_guard; binance_guard.install()` cagrilmasi yeter.
requests kutuphanesini yamadigi icin Binance'e giden tum modullerde (scanner,
paper_trading, live_trading...) otomatik devreye girer, onlarin kodunu degistirmez.

Ortam degiskenleri:
  BINANCE_MAX_REQ_PER_SEC     varsayilan 5   (piyasa verisi istek hizi)
  BINANCE_WEIGHT_SOFT_LIMIT   varsayilan 1500 (bu agirligin ustunde 4x yavaslar)
"""
import os
import threading
import time
from urllib.parse import urlparse

import requests

MAX_REQ_PER_SEC = max(0.5, float(os.environ.get('BINANCE_MAX_REQ_PER_SEC', '5')))
WEIGHT_SOFT_LIMIT = int(os.environ.get('BINANCE_WEIGHT_SOFT_LIMIT', '1500'))
MIN_429_COOLDOWN = 60       # Retry-After yoksa / cok kisaysa en az bu kadar bekle
DEFAULT_418_COOLDOWN = 600  # 418'de Retry-After yoksa
COOLDOWN_MARGIN = 5         # Retry-After uzerine guvenlik payi (sn)

# Sadece bu on eklerle baslayan, imzasiz GET istekleri sinirlanir/engellenir.
PUBLIC_DATA_PREFIXES = (
    '/fapi/v1/klines', '/fapi/v1/continuousKlines', '/fapi/v1/ticker',
    '/fapi/v1/exchangeInfo', '/fapi/v1/premiumIndex', '/fapi/v1/depth',
    '/fapi/v1/trades', '/fapi/v1/openInterest', '/fapi/v1/fundingRate',
    '/dapi/v1/klines', '/dapi/v1/continuousKlines', '/dapi/v1/ticker',
    '/dapi/v1/exchangeInfo', '/dapi/v1/premiumIndex',
    '/api/v3/klines', '/api/v3/ticker', '/api/v3/exchangeInfo',
)

_lock = threading.Lock()
_cooldown_until = 0.0
_next_slot = 0.0
_used_weight = 0
_used_weight_at = 0.0
_installed = False


class BinanceCooldown(requests.exceptions.RequestException):
    """Binance 429/418 sonrasi bekleme suresi dolmadi; istek gonderilmedi."""


def in_cooldown():
    return time.time() < _cooldown_until


def cooldown_remaining():
    return max(0, int(_cooldown_until - time.time()))


def _is_binance(url):
    try:
        host = (urlparse(str(url)).hostname or '').lower()
    except Exception:
        return False
    return host.endswith('binance.com')


def _is_public_data(method, url, params):
    if str(method).upper() != 'GET' or not _is_binance(url):
        return False
    if 'signature' in str(url).lower() or 'signature' in str(params or '').lower():
        return False
    try:
        path = urlparse(str(url)).path
    except Exception:
        return False
    return path.startswith(PUBLIC_DATA_PREFIXES)


def _check_cooldown():
    remaining = _cooldown_until - time.time()
    if remaining > 0:
        raise BinanceCooldown(f'Binance bekleme suresi aktif ({int(remaining)} sn kaldi), istek gonderilmedi')


def _before_public():
    global _next_slot
    with _lock:
        _check_cooldown()
        now = time.time()
        interval = 1.0 / MAX_REQ_PER_SEC
        if _used_weight >= WEIGHT_SOFT_LIMIT and now - _used_weight_at < 60:
            interval *= 4  # limite yaklasildi, belirgin yavasla
        slot = max(now, _next_slot)
        _next_slot = slot + interval
    wait = slot - now
    if wait > 0:
        time.sleep(wait)
    # Beklerken baska bir istek 429/418 almis olabilir
    with _lock:
        _check_cooldown()


def _after(resp):
    global _cooldown_until, _used_weight, _used_weight_at
    try:
        w = resp.headers.get('X-MBX-USED-WEIGHT-1M')
        if w:
            with _lock:
                _used_weight = int(w)
                _used_weight_at = time.time()
        code = resp.status_code
        if code in (418, 429):
            ra = resp.headers.get('Retry-After')
            try:
                secs = float(ra)
            except (TypeError, ValueError):
                secs = DEFAULT_418_COOLDOWN if code == 418 else MIN_429_COOLDOWN
            if code == 429:
                secs = max(secs, MIN_429_COOLDOWN)
            secs += COOLDOWN_MARGIN
            until = time.time() + secs
            with _lock:
                if until > _cooldown_until:
                    _cooldown_until = until
                    print(f'BINANCE GUARD | HTTP {code} | piyasa verisi istekleri {int(secs)} sn durduruldu', flush=True)
    except Exception:
        pass


def install():
    """requests'i bir kez yamalar. Birden fazla cagrilirsa zararsizdir."""
    global _installed
    if _installed:
        return
    original = requests.sessions.Session.request

    def guarded(self, method, url, *args, **kwargs):
        public = _is_public_data(method, url, kwargs.get('params'))
        if public:
            _before_public()
        resp = original(self, method, url, *args, **kwargs)
        if _is_binance(url):
            _after(resp)
        return resp

    requests.sessions.Session.request = guarded
    _installed = True
    print(f'BINANCE GUARD | aktif | max {MAX_REQ_PER_SEC}/sn, yumusak agirlik limiti {WEIGHT_SOFT_LIMIT}', flush=True)
