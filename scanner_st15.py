"""
Supertrend taramasi - 15 DAKIKALIK grafik, her SAAT BASI otomatik (Supertrend ATR 10, carpan 4.4).

Sinyal mantigi supertrend_signal.py icindedir (TradingView "ST Sinyal" Pine kodunun birebir karsiligi):
  LONG  = Supertrend'i yukari kirip ustunde mum kapatti (veya stop sonrasi yeniden giris),
  SHORT = Supertrend'i asagi kirip altinda mum kapatti.
Her saat basinda (hh:00) o ana kadar KAPANMIS son mum kullanilir; son 4 kapanmis 15 dk mumdaki
(= bir onceki saatin tamami) sinyaller listelenir. Olusmakta olan mum her zaman atilir.

Dashboard: /api/scanner_st rotalari bu modulu kullanir (scanner_st.py 4s/1g/1h taramasi DEGISMEDEN kalir; grafik ve hisse taramalari onu kullanir).
Evren: en likit SCAN_ST_TOP_N (varsayilan 100) USDT-M surekli kontrat.
"""
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import binance_live as blive
import supertrend_signal as sts
from indicators import supertrend as _st_df

PERIOD = sts.ST_PERIOD
FACTOR = sts.ST_MULT
INTERVAL = '15m'
TIMEFRAMES = ('15m',)
TOP_N = int(os.environ.get('SCAN_ST_TOP_N', '100'))
_EXCLUDE = {x.strip().upper() for x in os.environ.get('ST_EXCLUDE_SYMBOLS', 'PAXGUSDT,XAUTUSDT,XAUUSDT,XAGUSDT').split(',') if x.strip()}
PACE_S = float(os.environ.get('SCAN_ST_PACE_S', '0.15'))
MIN_GAP_S = float(os.environ.get('SCAN_ST_MIN_GAP_S', '120'))
SCAN_EVERY_MIN = max(5, int(os.environ.get('SCANNER_EVERY_MIN', '60')))
KLINE_LIMIT = 499                      # Binance agirligi 2 (500+ olursa 5)
MIN_CANDLES = 60
FAPI = os.environ.get('BINANCE_FAPI_URL', 'https://fapi.binance.com')

_lock = threading.Lock()
_state = {'status': 'IDLE', 'results': [], 'started_at': None, 'finished_at': None, 'progress': 0, 'total': 0,
          'last_error': None, 'partial': False, 'asof': None, 'interval': INTERVAL,
          'every_min': SCAN_EVERY_MIN}
_last_finish_ts = 0.0


def _get(url, params=None, timeout=15):
    r = requests.get(url, params=params, timeout=timeout)
    blive._note_rate_limit(r)
    r.raise_for_status()
    return r.json()


MIN_LISTING_DAYS = float(os.environ.get('ST_MIN_LISTING_DAYS', '14'))   # bundan yeni listelenen kontratlar taranmaz / islem acilmaz (0 = kapali)
_ONB = {'t': 0.0, 'm': {}}


def _onboard_map():
    """sembol -> Binance'te listelenme zamani (ms). 6 saatte bir tazelenir."""
    if not _ONB['m'] or time.time() - _ONB['t'] > 6 * 3600:
        try:
            data = _get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=25)
            _ONB['m'] = {s['symbol']: s.get('onboardDate') for s in (data.get('symbols') or []) if s.get('onboardDate')}
            _ONB['t'] = time.time()
        except Exception as e:
            print(f'SCAN | listeleme tarihi alinamadi: {type(e).__name__}: {e}', flush=True)
    return _ONB['m']


def listing_age_days(symbol):
    ms = _onboard_map().get(symbol)
    return (time.time() * 1000.0 - float(ms)) / 86400000.0 if ms else None     # bilinmiyorsa None (engelleme)


def is_new_listing(symbol):
    if MIN_LISTING_DAYS <= 0:
        return False
    age = listing_age_days(symbol)
    return age is not None and age < MIN_LISTING_DAYS


def _universe_n(top_n):
    info = blive.get_exchange_info() or {}
    ok = {s for s, i in info.items()
          if i.get('quoteAsset') == 'USDT' and i.get('contractType') == 'PERPETUAL' and i.get('status') == 'TRADING'}
    rows = []
    for t in _get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=20):
        sym = t.get('symbol')
        if sym in ok and sym.upper() not in _EXCLUDE and not is_new_listing(sym):
            try:
                rows.append((sym, float(t.get('quoteVolume') or 0), float(t.get('priceChangePercent') or 0)))
            except Exception:
                continue
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:top_n]


def _klines(symbol):
    data = _get(f'{FAPI}/fapi/v1/klines', {'symbol': symbol, 'interval': INTERVAL, 'limit': KLINE_LIMIT})
    cols = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume',
            'trades', 'tb', 'tq', 'ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    return df


def _wilder_atr(df, n=14):
    pc = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(), (df['low'] - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1.0 / n, adjust=False).mean().iloc[-1])


def _analyse(symbol, vol, chg, asof):
    df = _klines(symbol)
    closed = df[df['close_time'] <= asof].reset_index(drop=True)          # yalnizca kapanmis mumlar
    if len(closed) < MIN_CANDLES:
        return None
    ind = closed.set_index('close_time')
    st = _st_df(ind['high'], ind['low'], ind['close'], PERIOD, FACTOR)
    last = len(ind) - 1
    line = float(st['supertrend'].iloc[last])
    if np.isnan(line):
        return None
    up = int(st['supertrend_direction'].iloc[last]) == 1
    d = st['supertrend_direction'].to_numpy()
    j = last
    while j > 0 and d[j - 1] == d[last]:
        j -= 1
    price = float(df.iloc[-1]['close'])
    sig = sts.latest_entry_signal(ind)
    row = {
        'symbol': symbol, 'price': price, 'change_pct': chg, 'volume': vol,
        'trend': 'UP' if up else 'DOWN', 'st': line,
        'dist_pct': (price - line) / price * 100.0 if price else None,
        'flip_bars': int(last - j), 'flip_time': ind.index[j].isoformat(),
        'atr': _wilder_atr(closed), 'candle_time': ind.index[last].isoformat(),
        'signal': None, 'signal_kind': None, 'signal_age': None, 'level': None, 'signal_close': None,
    }
    if sig:
        row.update({'signal': sig['side'], 'signal_kind': sig['kind'], 'signal_age': int(sig['age_bars']),
                    'level': sig['level'], 'signal_st': sig.get('st'), 'signal_close': sig['signal_close'],
                    'signal_time': pd.Timestamp(sig['signal_time']).isoformat()})
    return row


def snapshot(tf=None):
    with _lock:
        out = dict(_state)
        out['results'] = list(_state['results'])
    out.update({'interval': INTERVAL, 'min_gap_s': MIN_GAP_S})
    wait = MIN_GAP_S - (time.time() - _last_finish_ts) if _last_finish_ts else 0
    out['next_allowed_in_s'] = max(0, int(wait))
    return out


def _run(asof):
    global _last_finish_ts
    results, partial, err = [], False, None
    try:
        universe = _universe_n(TOP_N)
        with _lock:
            _state['total'] = len(universe)
        for n, (sym, vol, chg) in enumerate(universe, 1):
            if blive.in_rate_limit_cooldown():
                partial, err = True, 'Binance istek siniri: tarama yarida durduruldu, birkac dakika sonra tekrar denenecek.'
                break
            try:
                row = _analyse(sym, vol, chg, asof)
                if row:
                    results.append(row)
            except requests.HTTPError as e:
                if getattr(e.response, 'status_code', None) in (418, 429):
                    partial, err = True, f'Binance istek siniri (HTTP {e.response.status_code}): tarama yarida durduruldu.'
                    break
            except Exception:
                pass
            with _lock:
                _state['progress'] = n
            time.sleep(PACE_S)
    except Exception as e:
        err = f'{type(e).__name__}: {str(e)[:160]}'
    with _lock:
        _state.update({'results': results, 'partial': partial, 'last_error': err,
                       'status': 'ERROR' if (err and not results) else 'READY',
                       'finished_at': datetime.now(timezone.utc).isoformat(), 'asof': asof.isoformat()})
    _last_finish_ts = time.time()
    n_l = sum(1 for r in results if r['signal'] == 'LONG'); n_s = sum(1 for r in results if r['signal'] == 'SHORT')
    print(f'SCAN-ST-15M | bitti | {len(results)} coin | sinyal: LONG={n_l} SHORT={n_s} | asof={asof} | partial={partial} | err={err}', flush=True)


def start_scan(tf=None, aligned=False):
    """(started, message). aligned=True: saat basi mumuna gore; False: manuel (son kapanmis 15 dk mum)."""
    with _lock:
        if _state['status'] == 'SCANNING':
            return False, 'Tarama zaten calisiyor.'
    if blive.in_rate_limit_cooldown():
        return False, 'Binance istek siniri bekleme suresi aktif; birkac dakika sonra deneyin.'
    wait = MIN_GAP_S - (time.time() - _last_finish_ts) if _last_finish_ts else 0
    if wait > 0:
        return False, f'Son taramanin uzerinden cok az zaman gecti; {int(wait)} sn sonra tekrar deneyin.'
    now = pd.Timestamp.now(tz='UTC')
    asof = now.floor(f'{SCAN_EVERY_MIN}min') if aligned else now.floor('15min')
    with _lock:
        _state.update({'status': 'SCANNING', 'progress': 0, 'total': 0, 'last_error': None, 'partial': False,
                       'started_at': datetime.now(timezone.utc).isoformat()})
    threading.Thread(target=_run, args=(asof,), daemon=True).start()
    return True, 'Tarama baslatildi.'


def scan_due(now=None):
    """Saat basi sinirini gecti ve son tarama bu sinirdan ONCEki mumla yapildiysa True."""
    with _lock:
        busy = _state['status'] == 'SCANNING'
        last = _state.get('asof')
    if busy:
        return False
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz='UTC')
    cur = now.floor(f'{SCAN_EVERY_MIN}min')
    if last is not None and pd.Timestamp(last) >= cur:
        return False
    return now >= cur + pd.Timedelta(seconds=int(os.environ.get('SCANNER_CANDLE_DELAY_S', '5')))


def background_loop():
    """Her 20 sn'de saat basini kontrol eder; saat basi gecince otomatik tarar (hata olursa 10 dk sonra tekrar)."""
    last_try = 0.0
    while True:
        try:
            if scan_due() and time.time() - last_try > 30:
                last_try = time.time()
                start_scan(aligned=True)
            else:
                with _lock:
                    errored = _state['status'] == 'READY' and _state['partial'] and _state['finished_at']
                if errored and time.time() - _last_finish_ts > 600 and time.time() - last_try > 600:
                    last_try = time.time()
                    start_scan(aligned=True)
        except Exception as e:
            with _lock:
                _state['status'], _state['last_error'] = 'ERROR', str(e)
        time.sleep(20)
