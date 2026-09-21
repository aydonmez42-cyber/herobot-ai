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


# Backward-compatible alias. Returns (symbols, source), same contract as get_xutum_symbols().
def get_bist_tum_symbols(force=False):
    return get_xutum_symbols(force=force)
