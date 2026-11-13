import os
import re
import time
import threading
import requests

TV_SCANNER_URL = os.environ.get('TRADINGVIEW_SCANNER_URL', 'https://scanner.tradingview.com/turkey/scan')
CNBC_XUTUM_URL = os.environ.get('CNBC_XUTUM_URL', 'https://www.cnbce.com/borsa/endeksler/bist-tum')
CACHE_SECONDS = int(os.environ.get('BIST_XUTUM_CACHE_SECONDS', '3600'))
TIMEOUT = int(os.environ.get('BIST_XUTUM_TIMEOUT', '30'))

_lock = threading.Lock()
_cache = {'symbols': None, 'ts': 0, 'source': None}


def _headers():
    return {
        'Accept': 'application/json',
        'Content-Type': 'text/plain;charset=UTF-8',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
        'Origin': 'https://www.tradingview.com',
        'Referer': 'https://www.tradingview.com/',
    }


def _tv_payload():
    # BIST Tüm (XUTUM) is the all-shares universe. This used to also filter
    # on is_primary/typespecs=common/type=stock to exclude certificates and
    # warrants, but TradingView silently changed what values those fields
    # accept — with those filters present the screener returns totalCount=0
    # (not an error, just an empty match). Diagnosed 2026-09-21 via a live
    # admin-only debug endpoint that tried several payload variants against
    # the real API: 'exchange'=='BIST' alone (plus the 'markets': ['turkey']
    # restriction below) reliably returns the full ~650-symbol BIST universe
    # with clean common-stock rows, so that's all we filter on now. If this
    # breaks again, re-run that comparison rather than guessing blind.
    return {
        'columns': ['name', 'description'],
        'filter': [
            {'left': 'exchange', 'operation': 'equal', 'right': 'BIST'},
        ],
        'filterOR': [],
        'ignore_unknown_fields': False,
        'options': {'lang': 'tr'},
        'price_conversion': {},
        'range': [0, 2000],
        'sort': {'sortBy': 'name', 'sortOrder': 'asc'},
        'symbols': {'query': {'types': []}, 'tickers': []},
        'markets': ['turkey'],
    }


def _normalize(symbol):
    s = str(symbol or '').upper().strip()
    if ':' in s:
        s = s.split(':', 1)[1]
    # BIST symbols are normally 3-6 chars, but retain legitimate longer ones.
    if re.fullmatch(r'[A-Z0-9]{2,8}', s):
        return s
    return None


def fetch_from_tradingview():
    r = requests.post(TV_SCANNER_URL, json=_tv_payload(), headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    data = payload.get('data') or []
    symbols = []
    for row in data:
        s = _normalize(row.get('s'))
        if s and s not in symbols:
            symbols.append(s)
    total = int(payload.get('totalCount') or len(symbols))
    if len(symbols) < 500:
        raise RuntimeError(f'TradingView XUTUM evreni beklenenden küçük: {len(symbols)} (totalCount={total})')
    return sorted(symbols), f'TradingView Screener XUTUM/BIST common stocks ({len(symbols)})'


def fetch_cnbc_fallback():
    # Confirmed 2026-09-21: CNBC-E's BIST-TUM page now only server-renders a
    # short preview of the table (~10 rows); the rest loads client-side, so
    # this regex-based fallback can never see the full ~650-symbol universe
    # from a plain HTTP GET. It will keep raising below (by design — better
    # to fail loudly than silently scan a partial/wrong universe). Kept as a
    # fallback in case TradingView's API goes down entirely; not a full fix.
    r = requests.get(CNBC_XUTUM_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=TIMEOUT)
    r.raise_for_status()
    # CNBC-E XUTUM page contains /borsa/hisseler/<symbol>-<slug> links.
    found = re.findall(r'/borsa/hisseler/([a-z0-9]+)-', r.text, flags=re.I)
    symbols = []
    seen = set()
    for raw in found:
        s = _normalize(raw)
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)
    if len(symbols) < 500:
        raise RuntimeError(f'CNBC-E XUTUM fallback evreni beklenenden küçük: {len(symbols)}')
    return sorted(symbols), f'CNBC-E XUTUM fallback ({len(symbols)})'


def get_xutum_symbols(force=False):
    now = time.time()
    with _lock:
        if not force and _cache['symbols'] and now - _cache['ts'] < CACHE_SECONDS:
            return list(_cache['symbols']), _cache['source']

    errors = []
    for fn in (fetch_from_tradingview, fetch_cnbc_fallback):
        try:
            symbols, source = fn()
            with _lock:
                _cache.update({'symbols': symbols, 'ts': time.time(), 'source': source})
            return list(symbols), source
        except Exception as exc:
            errors.append(f'{fn.__name__}: {exc}')

    raise RuntimeError('XUTUM evreni alınamadı | ' + ' | '.join(errors))


# ---- BIST 30 / BIST 100 (endeks bilesenleri) ----
# TradingView Screener'da endeks uyeleri 'symbolset' ile istenir ("SYML:BIST;XU100"). Liste bu sekilde her seferinde
# TradingView'den gelir (endeks bilesimi degisince kendiliginden guncellenir). Beklenen boyutta gelmezse YEDEK olarak
# BIST'teki hisseler piyasa degerine gore siralanip ilk N alinir; bu RESMI endeks listesi degildir ve 'approx'=True doner.
INDEXES = {
    'XU030': {'name': 'BIST 30', 'n': 30, 'ok': (25, 35)},
    'XU100': {'name': 'BIST 100', 'n': 100, 'ok': (90, 110)},
}
_idx_cache = {}


def _tv_index_payload(code):
    p = _tv_payload()
    p['symbols'] = {'symbolset': [f'SYML:BIST;{code}'], 'query': {'types': []}, 'tickers': []}
    p['range'] = [0, 300]
    return p


def _tv_rank_payload(n):
    p = _tv_payload()
    p['columns'] = ['name', 'description', 'market_cap_basic']
    p['sort'] = {'sortBy': 'market_cap_basic', 'sortOrder': 'desc'}
    p['range'] = [0, int(n)]
    return p


def _rows_to_symbols(payload):
    out = []
    for row in (payload.get('data') or []):
        s = _normalize(row.get('s'))
        if s and s not in out:
            out.append(s)
    return out


def fetch_index_from_tradingview(code):
    meta = INDEXES[code]
    r = requests.post(TV_SCANNER_URL, json=_tv_index_payload(code), headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    syms = _rows_to_symbols(r.json())
    lo, hi = meta['ok']
    if not (lo <= len(syms) <= hi):
        raise RuntimeError(f"{meta['name']} bilesen sayisi beklenenden farkli: {len(syms)}")
    return sorted(syms), f"TradingView {meta['name']} endeks bilesenleri ({len(syms)})", False


def fetch_index_by_market_cap(code):
    meta = INDEXES[code]
    r = requests.post(TV_SCANNER_URL, json=_tv_rank_payload(meta['n']), headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    syms = _rows_to_symbols(r.json())
    if len(syms) < int(meta['n'] * 0.8):
        raise RuntimeError(f"piyasa degeri siralamasi beklenenden kucuk: {len(syms)}")
    return sorted(syms), f"TradingView piyasa degeri siralamasi (YAKLASIK {meta['name']}, {len(syms)} hisse)", True


def get_index_symbols(code, force=False):
    """BIST 30 / BIST 100 hisseleri. Doner: (semboller, kaynak, approx)."""
    code = str(code).upper()
    if code not in INDEXES:
        raise ValueError(f'bilinmeyen endeks: {code}')
    now = time.time()
    with _lock:
        c = _idx_cache.get(code)
        if c and not force and now - c['ts'] < CACHE_SECONDS * 6:
            return list(c['symbols']), c['source'], c['approx']
    errors = []
    for fn in (fetch_index_from_tradingview, fetch_index_by_market_cap):
        try:
            symbols, source, approx = fn(code)
            with _lock:
                _idx_cache[code] = {'symbols': symbols, 'ts': time.time(), 'source': source, 'approx': approx}
            return list(symbols), source, approx
        except Exception as exc:
            errors.append(f'{fn.__name__}: {exc}')
    raise RuntimeError(f'{code} evreni alinamadi | ' + ' | '.join(errors))


# Backward-compatible alias. Returns (symbols, source), same contract as get_xutum_symbols().
def get_bist_tum_symbols(force=False):
    return get_xutum_symbols(force=force)
