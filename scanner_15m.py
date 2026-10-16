"""
15-minute Binance Futures scan - MANUAL ONLY.

Nothing here runs on a timer. A scan starts only when someone presses the
"15 dk tara" button (dashboard -> /api/scanner15/scan). To keep Binance's
per-IP rate limit safe it:
  * scans only the SCAN15_TOP_N most liquid USDT-M perpetuals (default 200),
    ranked from ONE bulk 24h ticker call,
  * fetches one small klines request per coin, paced (SCAN15_PACE_S apart),
  * stops immediately on HTTP 429/418 and shares the cooldown with
    binance_live (so dashboard price reads back off as well),
  * refuses to start again within SCAN15_MIN_GAP_S seconds of the last scan.

Signals use exactly the same indicators and long/short rules as the 4H bot
(indicators.add_indicators / strategy.long_signal / short_signal) on 15m
CLOSED candles. The daily volatility filter is not applied (it would need an
extra daily-candle request per coin). The bot itself still trades only on its
own 4H signals - this list is for finding coins worth adding to the watchlist.
"""
import os
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import requests

import binance_live as blive

FAPI = os.environ.get('BINANCE_FAPI_URL', 'https://fapi.binance.com')
TOP_N = int(os.environ.get('SCAN15_TOP_N', '200'))
PACE_S = float(os.environ.get('SCAN15_PACE_S', '0.15'))
MIN_GAP_S = float(os.environ.get('SCAN15_MIN_GAP_S', '180'))
KLINE_LIMIT = 300

_lock = threading.Lock()
_state = {
    'status': 'IDLE',          # IDLE | SCANNING | READY | ERROR
    'results': [],
    'started_at': None,
    'finished_at': None,
    'progress': 0,
    'total': 0,
    'last_error': None,
    'partial': False,
}
_last_finish_ts = 0.0


def snapshot():
    with _lock:
        out = dict(_state)
        out['results'] = list(_state['results'])
    out['min_gap_s'] = MIN_GAP_S
    wait = MIN_GAP_S - (time.time() - _last_finish_ts) if _last_finish_ts else 0
    out['next_allowed_in_s'] = max(0, int(wait))
    return out


def _get(url, params=None, timeout=15):
    r = requests.get(url, params=params, timeout=timeout)
    blive._note_rate_limit(r)
    r.raise_for_status()
    return r.json()


def _universe():
    """[(symbol, quote_volume_usd, change_pct)] for the TOP_N most liquid
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
    return rows[:TOP_N]


def _klines(symbol):
    data = _get(f'{FAPI}/fapi/v1/klines', {'symbol': symbol, 'interval': '15m', 'limit': KLINE_LIMIT})
    cols = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume',
            'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    return df


def _fmt(v):
    try:
        return round(float(v), 4)
    except Exception:
        return None


def _analyse(symbol, vol, chg):
    import config as cfg
    from indicators import add_indicators
    from strategy import long_signal, short_signal
    df = _klines(symbol)
    closed = df.iloc[:-1].copy()           # drop the still-forming candle
    if len(closed) < 120:
        return None
    enriched = add_indicators(closed, cfg)
    i = len(enriched) - 1
    latest = enriched.iloc[-1]
    go_long = long_signal(enriched, i, cfg)
    go_short = short_signal(enriched, i, cfg)
    signal = 'LONG' if go_long else ('SHORT' if go_short else 'NO SIGNAL')
    macd_bull = float(latest.get('macd', 0)) > float(latest.get('macd_signal', 0)) and float(latest.get('macd_hist', 0)) > 0
    return {
        'symbol': symbol,
        'price': float(df.iloc[-1]['close']),
        'change_pct': chg,
        'volume': vol,
        'st': 'BULLISH' if bool(latest.get('supertrend_direction', False)) else 'BEARISH',
        'adx': _fmt(latest.get('adx')), 'rsi': _fmt(latest.get('rsi')), 'cci': _fmt(latest.get('cci')),
        'macd': 'BULLISH' if macd_bull else 'BEARISH',
        'atrp_percentile_1d': None, 'score': None,
        'signal': signal,
        'reason': '15 dk kapanış mumu · ' + str(latest['close_time'])[:16] + ' UTC',
    }


def _run():
    global _last_finish_ts
    results, partial, err = [], False, None
    try:
        universe = _universe()
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
                pass   # one bad coin must not stop the scan
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
    print(f'SCAN15 | bitti | {len(results)} coin | LONG={sum(1 for r in results if r["signal"]=="LONG")} | partial={partial} | err={err}', flush=True)


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
