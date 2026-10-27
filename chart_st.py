"""Data for the dashboard's own candle chart with Supertrend (10, 7.8).

Same Supertrend maths as the scanners (scanner_st.supertrend), so the line the
user sees is exactly the one the scans and the bot use. Crypto candles come
from Binance Futures (through binance_live's rate-limit bookkeeping), BIST and
US stocks from TradingView (same helper the stock scanners use).
"""
import re
import threading
import time

import numpy as np
import pandas as pd

import scanner_st
from scanner_st import supertrend, PERIOD, FACTOR

TF_BINANCE = {'15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}
TF_TV = {'15m': '15', '1h': '60', '4h': '240', '1d': '1D'}
BARS = 500
CACHE_S = 20
_cache = {}
_lock = threading.Lock()
_TV_RE = re.compile(r'^[A-Z0-9_.\-]{1,30}:[A-Z0-9_.\-]{1,30}$')


class ChartError(Exception):
    pass


def _binance_df(symbol, tf):
    import binance_live as blive
    if blive.in_rate_limit_cooldown():
        raise ChartError('Binance istek sınırı bekleme süresi aktif, biraz sonra deneyin.')
    data = scanner_st._get(f'{scanner_st.FAPI}/fapi/v1/klines',
                           {'symbol': symbol, 'interval': TF_BINANCE[tf], 'limit': BARS})
    df = pd.DataFrame(data, columns=['t', 'open', 'high', 'low', 'close', 'volume', 'ct', 'qv',
                                     'n', 'tb', 'tq', 'ig'])
    for c in ('open', 'high', 'low', 'close'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['timestamp'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    return df


def _tv_df(tv, tf):
    from tradingview_data import fetch_tv_bars
    df = fetch_tv_bars(tv, interval=TF_TV[tf], bars=BARS)
    return df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)


def build(tv, tf):
    tv = (tv or '').strip().upper()
    tf = (tf or '4h').strip().lower()
    if tf not in TF_BINANCE:
        raise ChartError('geçersiz zaman dilimi')
    if not _TV_RE.match(tv):
        raise ChartError('geçersiz sembol')
    key = (tv, tf)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    exch, sym = tv.split(':', 1)
    if exch == 'BINANCE':
        df = _binance_df(sym[:-2] if sym.endswith('.P') else sym, tf)
    else:
        df = _tv_df(tv, tf)
    df = df.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
    if len(df) < PERIOD + 5:
        raise ChartError('yeterli mum verisi yok')
    up, line = supertrend(df)
    times = [int(t.timestamp()) for t in df['timestamp']]
    candles = [{'time': times[i], 'open': float(df['open'][i]), 'high': float(df['high'][i]),
                'low': float(df['low'][i]), 'close': float(df['close'][i])} for i in range(len(df))]
    up_pts, dn_pts, flips = [], [], []
    for i in range(len(df)):
        if np.isnan(line[i]):
            continue
        v = round(float(line[i]), 8)
        if up[i]:
            up_pts.append({'time': times[i], 'value': v}); dn_pts.append({'time': times[i]})
        else:
            dn_pts.append({'time': times[i], 'value': v}); up_pts.append({'time': times[i]})
        if i > 0 and not np.isnan(line[i - 1]) and bool(up[i]) != bool(up[i - 1]):
            flips.append({'time': times[i], 'dir': 'UP' if up[i] else 'DOWN'})
    out = {'symbol': tv, 'tf': tf, 'period': PERIOD, 'factor': FACTOR,
           'candles': candles, 'st_up': up_pts, 'st_down': dn_pts, 'flips': flips,
           'trend': 'UP' if up[-1] else 'DOWN'}
    with _lock:
        _cache[key] = (time.time(), out)
        if len(_cache) > 60:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:20]:
                _cache.pop(k, None)
    return out
