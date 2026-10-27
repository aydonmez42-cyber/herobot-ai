"""
Supertrend scan (4H, ATR period 10, factor 7.8) - MANUAL ONLY.

LONG  = price closed above the Supertrend line (trend flipped UP) on a closed
        4H candle; SHORT = it closed below (flipped DOWN).
For every coin the scan records the current trend and how many closed candles
ago it last flipped (flip_bars: 0 = on the latest closed candle), so the
dashboard can show "flipped in the last N candles" without scanning again.

It never runs by itself: it starts only from the dashboard button, scans the
SCAN_ST_TOP_N most liquid USDT-M perpetuals (default 300), paces its requests,
stops on HTTP 429/418 (sharing the cooldown with binance_live) and refuses to
start within SCAN_ST_MIN_GAP_S of the last scan.
"""
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import binance_live as blive

PERIOD = int(os.environ.get('SUPERTREND_PERIOD', '10'))
FACTOR = float(os.environ.get('SUPERTREND_FACTOR', '7.8'))
INTERVAL = '4h'
TOP_N = int(os.environ.get('SCAN_ST_TOP_N', '300'))
PACE_S = float(os.environ.get('SCAN_ST_PACE_S', '0.15'))
MIN_GAP_S = float(os.environ.get('SCAN_ST_MIN_GAP_S', '180'))
KLINE_LIMIT = 300
FAPI = os.environ.get('BINANCE_FAPI_URL', 'https://fapi.binance.com')

_lock = threading.Lock()
_state = {'status': 'IDLE', 'results': [], 'started_at': None, 'finished_at': None,
          'progress': 0, 'total': 0, 'last_error': None, 'partial': False}
_last_finish_ts = 0.0


def supertrend(df, period=PERIOD, factor=FACTOR):
    """TradingView-style Supertrend (ta.supertrend: hl2 source, Wilder ATR).
    Returns (trend_up bool array, supertrend line array)."""
    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    close = df['close'].to_numpy(float)
    n = len(df)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(n, np.nan)
    if n > period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    hl2 = (high + low) / 2.0
    upper = hl2 + factor * atr
    lower = hl2 - factor * atr
    trend_up = np.zeros(n, dtype=bool)
    line = np.full(n, np.nan)
    f_upper = np.full(n, np.nan)
    f_lower = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(f_upper[i - 1]):
            f_upper[i], f_lower[i] = upper[i], lower[i]
            trend_up[i] = True if close[i] > hl2[i] else False
        else:
            f_lower[i] = lower[i] if (lower[i] > f_lower[i - 1] or close[i - 1] < f_lower[i - 1]) else f_lower[i - 1]
            f_upper[i] = upper[i] if (upper[i] < f_upper[i - 1] or close[i - 1] > f_upper[i - 1]) else f_upper[i - 1]
            if not trend_up[i - 1]:
                trend_up[i] = close[i] > f_upper[i]
            else:
                trend_up[i] = not (close[i] < f_lower[i])
        line[i] = f_lower[i] if trend_up[i] else f_upper[i]
    return trend_up, line


def _get(url, params=None, timeout=15):
    r = requests.get(url, params=params, timeout=timeout)
    blive._note_rate_limit(r)
    r.raise_for_status()
    return r.json()


def _universe_n(top_n):
    """[(symbol, quote_volume_usd, change_pct)] for the top_n most liquid
    USDT perpetuals, from one exchangeInfo (cached 1h) + one 24h ticker call."""
    info = blive.get_exchange_info() or {}
    ok = {s for s, i in info.items()
          if i.get('quoteAsset') == 'USDT' and i.get('contractType') == 'PERPETUAL' and i.get('status') == 'TRADING'}
    tick = _get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=20)
    rows = []
    for t in tick:
        sym = t.get('symbol')
        if sym in ok:
            try:
                rows.append((sym, float(t.get('quoteVolume') or 0), float(t.get('priceChangePercent') or 0)))
            except Exception:
                continue
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:top_n]


def snapshot():
    with _lock:
        out = dict(_state)
        out['results'] = list(_state['results'])
    out.update({'interval': INTERVAL, 'min_gap_s': MIN_GAP_S})
    wait = MIN_GAP_S - (time.time() - _last_finish_ts) if _last_finish_ts else 0
    out['next_allowed_in_s'] = max(0, int(wait))
    return out


def _klines(symbol):
    data = _get(f'{FAPI}/fapi/v1/klines', {'symbol': symbol, 'interval': INTERVAL, 'limit': KLINE_LIMIT})
    cols = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume',
            'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    return df


def _analyse(symbol, vol, chg):
    df = _klines(symbol)
    closed = df.iloc[:-1].reset_index(drop=True)      # closed candles only
    if len(closed) < PERIOD + 30:
        return None
    up, line = supertrend(closed)
    last = len(closed) - 1
    if np.isnan(line[last]):
        return None
    j = last
    while j > 0 and up[j - 1] == up[last] and not np.isnan(line[j - 1]):
        j -= 1
    # j = first candle of the current trend; the flip happened ON candle j
    flip_bars = last - j
    price = float(df.iloc[-1]['close'])
    return {
        'symbol': symbol, 'price': price, 'change_pct': chg, 'volume': vol,
        'trend': 'UP' if up[last] else 'DOWN',
        'st': float(line[last]),
        'dist_pct': (price - float(line[last])) / price * 100.0 if price else None,
        'flip_bars': int(flip_bars),
        'flip_time': closed.iloc[j]['close_time'].isoformat(),
    }


def _run():
    global _last_finish_ts
    results, partial, err = [], False, None
    try:
        universe = _universe_n(TOP_N)
        with _lock:
            _state['total'] = len(universe)
        for n, (sym, vol, chg) in enumerate(universe, 1):
            if blive.in_rate_limit_cooldown():
                partial, err = True, 'Binance istek sınırı: tarama yarıda durduruldu, birkaç dakika sonra tekrar deneyin.'
                break
            try:
                row = _analyse(sym, vol, chg)
                if row:
                    results.append(row)
            except requests.HTTPError as e:
                code = getattr(e.response, 'status_code', None)
                if code in (418, 429):
                    partial, err = True, f'Binance istek sınırı (HTTP {code}): tarama yarıda durduruldu.'
                    break
            except Exception:
                pass
            with _lock:
                _state['progress'] = n
            time.sleep(PACE_S)
    except Exception as e:
        err = f'{type(e).__name__}: {str(e)[:160]}'
    with _lock:
        _state['results'] = results
        _state['partial'] = partial
        _state['last_error'] = err
        _state['status'] = 'ERROR' if (err and not results) else 'READY'
        _state['finished_at'] = datetime.now(timezone.utc).isoformat()
    _last_finish_ts = time.time()
    flips = [r for r in results if r['flip_bars'] == 0]
    print(f'SCAN-ST | bitti | {len(results)} coin | son mumda: LONG={sum(1 for r in flips if r["trend"]=="UP")} '
          f'SHORT={sum(1 for r in flips if r["trend"]=="DOWN")} | partial={partial} | err={err}', flush=True)


def start_scan():
    """Returns (started, message). Manual trigger only."""
    with _lock:
        if _state['status'] == 'SCANNING':
            return False, 'Tarama zaten çalışıyor.'
    if blive.in_rate_limit_cooldown():
        return False, 'Binance istek sınırı bekleme süresi aktif; birkaç dakika sonra deneyin.'
    wait = MIN_GAP_S - (time.time() - _last_finish_ts) if _last_finish_ts else 0
    if wait > 0:
        return False, f'Son taramanın üzerinden çok az zaman geçti; {int(wait)} sn sonra tekrar deneyin.'
    with _lock:
        _state.update({'status': 'SCANNING', 'progress': 0, 'total': 0, 'last_error': None,
                       'partial': False, 'started_at': datetime.now(timezone.utc).isoformat()})
    threading.Thread(target=_run, daemon=True).start()
    return True, 'Tarama başlatıldı.'
