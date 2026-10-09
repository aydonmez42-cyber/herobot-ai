"""
PHASE 2 — Binance Futures LIVE (real-money) order helpers.

This module is the only place in the codebase that ever sends a signed
POST to Binance (place/cancel a real order, change leverage). Everything
else that touches Binance (auth.py's verify_binance_key) is read-only.

Scope decision (disclosed to the user): the paper-trading engine trades the
main ETH strategy's LONG leg on the COIN-margined contract (ETHUSD_PERP)
and its SHORT leg on the USDT-margined contract (ETHUSDT) — two different
wallets/margin types. For LIVE trading this module deliberately collapses
both directions onto a single USDT-margined (USD-M) symbol (ETHUSDT for the
main bot; the watchlist symbol itself for watchlist entries), because:
  - almost every retail user only funds a USD-M futures wallet,
  - mixing margin types would require the bot to also automate wallet
    transfers, which is a much larger and riskier surface area,
  - the price difference between ETHUSD_PERP and ETHUSDT is normally a
    small fraction of a percent, so this is a safe simplification for
    sizing/signal purposes.
Real fills always happen at Binance's actual live market price via a
MARKET order — the strategy's own computed price is only used to size the
order (USD notional -> quantity) and to estimate P&L for the daily loss
limit. The user's Binance account is always the source of truth for actual
money; this bot's own bookkeeping here is a best-effort estimate.
"""
import hashlib
import hmac
import math
import os
import threading
import time
from urllib.parse import urlencode

import requests

BINANCE_FAPI_URL = os.environ.get('BINANCE_FAPI_URL', 'https://fapi.binance.com')

_exchange_info_cache = {'data': None, 'ts': 0.0}
_cache_lock = threading.Lock()

_price_cache = {'prices': {}, 'ts': 0.0}   # ONE bulk call covers every symbol
_price_cache_lock = threading.Lock()
PRICE_CACHE_SECONDS = 5

# Shared Binance rate-limit state. The limit is per server IP (all members
# share it, together with the scanner and the bot loop), so once Binance says
# 429/418 every READ call in this module backs off until it passes — hammering
# on is exactly what escalates a 429 into a 418 IP ban. Order placement is
# never blocked by this: closing a real position always gets tried.
_rl_until = 0.0
_rl_lock = threading.Lock()


def _note_rate_limit(r):
    global _rl_until
    try:
        code = r.status_code
    except Exception:
        return
    if code in (429, 418):
        try:
            wait = float(r.headers.get('Retry-After') or 0)
        except Exception:
            wait = 0
        wait = wait if wait > 0 else (300 if code == 418 else 60)
        with _rl_lock:
            _rl_until = max(_rl_until, time.time() + min(wait, 600))


def in_rate_limit_cooldown():
    with _rl_lock:
        return time.time() < _rl_until

_income_cache = {}  # sha256(api_key) -> {'ts': float, 'data': dict}
INCOME_CACHE_SECONDS = 300


def _signed_request(method, path, api_key, api_secret, params=None, timeout=15):
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    query = urlencode(params)
    signature = hmac.new(api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f'{BINANCE_FAPI_URL}{path}?{query}&signature={signature}'
    headers = {'X-MBX-APIKEY': api_key}
    if method == 'GET':
        r = requests.get(url, headers=headers, timeout=timeout)
    elif method == 'POST':
        r = requests.post(url, headers=headers, timeout=timeout)
    elif method == 'DELETE':
        r = requests.delete(url, headers=headers, timeout=timeout)
    else:
        raise ValueError(f'unsupported method {method}')
    _note_rate_limit(r)
    return r


def get_mark_price(symbol):
    """Public (unsigned) last-price lookup for a USD-M futures symbol, used
    only to show a user their own live position's current unrealized P&L —
    never to size or price an order (real fills always happen at Binance's
    own live market price via the MARKET order itself).

    One call WITHOUT a symbol returns every price (weight 2), so the whole
    dashboard — any number of members and positions — costs one request per
    PRICE_CACHE_SECONDS instead of one per position. Returns None on any
    failure (or during a rate-limit cooldown) — callers must fall back to the
    position's entry price rather than showing a wrong number."""
    now = time.time()
    with _price_cache_lock:
        if now - _price_cache['ts'] < PRICE_CACHE_SECONDS and symbol in _price_cache['prices']:
            return _price_cache['prices'][symbol]
        stale = _price_cache['prices'].get(symbol)
    if in_rate_limit_cooldown():
        return stale
    try:
        r = requests.get(f'{BINANCE_FAPI_URL}/fapi/v1/ticker/price', timeout=10)
        _note_rate_limit(r)
        r.raise_for_status()
        prices = {row['symbol']: float(row['price']) for row in r.json()}
    except Exception:
        return stale
    with _price_cache_lock:
        _price_cache['prices'] = prices
        _price_cache['ts'] = now
    return prices.get(symbol)


def get_exchange_info(force=False):
    """Cached for 1h. Returns {symbol: symbolInfoDict} or None on failure
    (callers must treat None/missing-symbol as "cannot safely size this
    order" and skip the trade rather than guess)."""
    with _cache_lock:
        if not force and _exchange_info_cache['data'] and (time.time() - _exchange_info_cache['ts'] < 3600):
            return _exchange_info_cache['data']
    try:
        r = requests.get(f'{BINANCE_FAPI_URL}/fapi/v1/exchangeInfo', timeout=15)
        r.raise_for_status()
        data = r.json()
        symbols = {s['symbol']: s for s in data.get('symbols', [])}
    except Exception:
        with _cache_lock:
            return _exchange_info_cache['data']
    with _cache_lock:
        _exchange_info_cache['data'] = symbols
        _exchange_info_cache['ts'] = time.time()
    return symbols


def _symbol_filters(symbol):
    info = get_exchange_info()
    if not info or symbol not in info:
        return None
    return {f['filterType']: f for f in info[symbol].get('filters', [])}


def _decimals_from_step(step_size):
    s = f'{step_size:.10f}'.rstrip('0')
    if '.' not in s:
        return 0
    return len(s.split('.')[1])


def compute_quantity(symbol, usd_notional, price):
    """Returns (qty, error). qty is None on any failure — callers must skip
    the trade rather than fall back to a guessed size."""
    if not price or price <= 0 or not usd_notional or usd_notional <= 0:
        return None, 'geçersiz fiyat/tutar'
    filters = _symbol_filters(symbol)
    if not filters:
        return None, f'{symbol} için borsa bilgisi alınamadı'
    lot = filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE')
    if not lot:
        return None, f'{symbol} için lot bilgisi alınamadı'
    step_size = float(lot['stepSize'])
    min_qty = float(lot['minQty'])
    raw_qty = usd_notional / price
    steps = math.floor(raw_qty / step_size) * step_size
    qty = round(steps, _decimals_from_step(step_size))
    if qty <= 0 or qty < min_qty:
        return None, f'{symbol} için minimum miktar karşılanamıyor (hesaplanan={qty})'
    min_notional_filter = filters.get('MIN_NOTIONAL') or filters.get('NOTIONAL')
    if min_notional_filter:
        min_notional = float(min_notional_filter.get('notional', min_notional_filter.get('minNotional', 0)) or 0)
        if min_notional and qty * price < min_notional:
            return None, f'{symbol} için minimum işlem tutarı karşılanamıyor (min ~{min_notional} USDT)'
    return qty, None


def set_leverage(api_key, api_secret, symbol, leverage):
    try:
        r = _signed_request('POST', '/fapi/v1/leverage', api_key, api_secret,
                             {'symbol': symbol, 'leverage': int(leverage)})
        return r.status_code == 200
    except requests.RequestException:
        return False


def place_market_order(api_key, api_secret, symbol, side, quantity, reduce_only=False):
    """side: 'BUY' or 'SELL'. Returns (ok, result_dict_or_error_string)."""
    params = {'symbol': symbol, 'side': side, 'type': 'MARKET', 'quantity': quantity}
    if reduce_only:
        params['reduceOnly'] = 'true'
    try:
        r = _signed_request('POST', '/fapi/v1/order', api_key, api_secret, params)
    except requests.RequestException as e:
        return False, f'Bağlantı hatası: {type(e).__name__}'
    if r.status_code != 200:
        try:
            msg = r.json().get('msg', r.text[:200])
        except Exception:
            msg = r.text[:200]
        return False, f'Binance HTTP {r.status_code}: {msg}'
    try:
        return True, r.json()
    except Exception:
        return True, {}


def fill_price_from_order(order, fallback):
    """Best-effort average fill price from a MARKET order response; falls
    back to the strategy's own estimated price if Binance didn't report one
    (this only affects this bot's own P&L bookkeeping/loss-limit tracking,
    never the actual order that was already placed)."""
    try:
        avg = float(order.get('avgPrice') or 0)
        if avg > 0:
            return avg
    except Exception:
        pass
    return fallback


def _json_or_error(r):
    """Returns (data, error) for a signed GET response."""
    if r.status_code != 200:
        try:
            msg = r.json().get('msg', r.text[:200])
        except Exception:
            msg = r.text[:200]
        return None, f'Binance HTTP {r.status_code}: {msg}'
    try:
        return r.json(), None
    except Exception:
        return None, 'Binance yanıtı okunamadı'


def _get_income_summary(api_key, api_secret):
    """Realized P&L / fees+funding for today (UTC), 7d, 30d. Heavy endpoint
    (weight 30 per call, 3 calls), so the result is cached for 5 minutes per
    API key. Returns (dict, None) or (None, error)."""
    ckey = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
    now = time.time()
    c = _income_cache.get(ckey)
    if c and now - c['ts'] < INCOME_CACHE_SECONDS:
        return c['data'], None
    if in_rate_limit_cooldown():
        return (c['data'], None) if c else (None, 'rate-limit cooldown')
    now_ms = int(now * 1000)
    start_ms = now_ms - 30 * 86400 * 1000
    today_start_ms = int((now_ms // 86400000) * 86400000)
    week_start_ms = now_ms - 7 * 86400 * 1000
    realized = {'today': 0.0, 'week': 0.0, 'month': 0.0}
    fees = {'today': 0.0, 'week': 0.0, 'month': 0.0}
    try:
        for kind, bucket in (('REALIZED_PNL', realized), ('COMMISSION', fees), ('FUNDING_FEE', fees)):
            cursor = start_ms
            for _ in range(5):  # at most 5 pages x 1000 rows per kind
                r = _signed_request('GET', '/fapi/v1/income', api_key, api_secret,
                                    {'incomeType': kind, 'startTime': cursor, 'limit': 1000})
                rows, err = _json_or_error(r)
                if err or not isinstance(rows, list):
                    raise RuntimeError(err or 'income')
                for row in rows:
                    t_ms = int(row.get('time') or 0)
                    amt = float(row.get('income') or 0)
                    bucket['month'] += amt
                    if t_ms >= week_start_ms:
                        bucket['week'] += amt
                    if t_ms >= today_start_ms:
                        bucket['today'] += amt
                if len(rows) < 1000:
                    break
                cursor = int(rows[-1].get('time') or cursor) + 1
    except Exception as e:
        return (c['data'], None) if c else (None, str(e)[:160])
    data = {'realized_pnl': realized, 'fees_funding': fees}
    _income_cache[ckey] = {'ts': now, 'data': data}
    return data, None


def get_account_overview(api_key, api_secret):
    """READ-ONLY. The user's real USD-M futures wallet numbers straight from
    Binance (GET /fapi/v2/account, weight 5) plus the cached income summary.
    Returns (dict, None) or (None, error_string). Never places or changes
    anything. Backs off completely while Binance rate-limits this server."""
    if in_rate_limit_cooldown():
        return None, 'Binance HTTP 429: istek sınırı doldu, otomatik bekleniyor'
    try:
        r = _signed_request('GET', '/fapi/v2/account', api_key, api_secret)
    except requests.RequestException as e:
        return None, f'Bağlantı hatası: {type(e).__name__}'
    acc, err = _json_or_error(r)
    if err:
        return None, err

    def f(key):
        try:
            return float(acc.get(key) or 0)
        except Exception:
            return 0.0

    out = {
        'wallet_balance': f('totalWalletBalance'),
        'margin_balance': f('totalMarginBalance'),
        'available_balance': f('availableBalance'),
        'unrealized_pnl': f('totalUnrealizedProfit'),
        'position_margin': f('totalPositionInitialMargin'),
        'open_positions_exchange': sum(1 for p in acc.get('positions', []) if abs(float(p.get('positionAmt') or 0)) > 0),
    }
    inc, ierr = _get_income_summary(api_key, api_secret)
    if inc:
        out.update(inc)
    elif ierr:
        out['income_error'] = ierr
    return out, None
