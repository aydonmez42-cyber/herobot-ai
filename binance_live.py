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


def _signed_request(method, path, api_key, api_secret, params=None, timeout=15):
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    query = urlencode(params)
    signature = hmac.new(api_secret.encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f'{BINANCE_FAPI_URL}{path}?{query}&signature={signature}'
    headers = {'X-MBX-APIKEY': api_key}
    if method == 'GET':
        return requests.get(url, headers=headers, timeout=timeout)
    if method == 'POST':
        return requests.post(url, headers=headers, timeout=timeout)
    if method == 'DELETE':
        return requests.delete(url, headers=headers, timeout=timeout)
    raise ValueError(f'unsupported method {method}')


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
