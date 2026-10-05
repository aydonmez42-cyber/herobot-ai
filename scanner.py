import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests

import binance_guard
binance_guard.install()  # Binance'e giden TUM istekleri (bu process'te) hiz sinirina ve 429/418 beklemesine tabi tutar

import config as cfg
from indicators import add_indicators, atr
from strategy import long_signal, short_signal, long_conditions, short_conditions

BASE_URL = 'https://fapi.binance.com'
KLINES_URL = BASE_URL + '/fapi/v1/klines'
EXCHANGE_INFO_URL = BASE_URL + '/fapi/v1/exchangeInfo'
TICKER_URL = BASE_URL + '/fapi/v1/ticker/24hr'

# Hiz sinirini binance_guard uyguladigi icin worker sayisi dusuruldu (8 -> 3).
SCANNER_WORKERS = int(os.environ.get('SCANNER_WORKERS', '3'))
SCANNER_CACHE_SECONDS = int(os.environ.get('SCANNER_CACHE_SECONDS', '900'))
SCANNER_KLINE_LIMIT = int(os.environ.get('SCANNER_KLINE_LIMIT', '280'))
SCANNER_DAILY_LIMIT = int(os.environ.get('SCANNER_DAILY_LIMIT', '400'))

_lock = threading.Lock()
_state = {
    'status': 'IDLE',
    'started_at': None,
    'finished_at': None,
    'last_error': None,
    'symbols_total': 0,
    'symbols_done': 0,
    'results': [],
    'last_scan_candle': None,
}

# Mum onbellegi: (sembol, aralik, limit) -> (DataFrame, gecerlilik_bitis_epoch)
# Indikatorler yalnizca KAPANMIS mumlardan hesaplanir. Son satir (olusmakta olan
# mum) kapaninca yeni bir mum olusana kadar kapanmis mumlar degismez; bu yuzden
# veri o ana kadar yeniden indirilmez. 4 saatlik mum icin 15 dakikalik her
# taramada ~500 istek yerine 4 saatte bir ~500 istek atilir.
_kline_cache = {}
_cache_lock = threading.Lock()


def _get_json(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_symbols():
    data = _get_json(EXCHANGE_INFO_URL)
    return [
        s['symbol'] for s in data.get('symbols', [])
        if s.get('status') == 'TRADING'
        and s.get('contractType') == 'PERPETUAL'
        and s.get('quoteAsset') == 'USDT'
    ]


def get_tickers():
    data = _get_json(TICKER_URL, timeout=20)
    out = {}
    for x in data:
        sym = x.get('symbol')
        if sym:
            out[sym] = {
                'price': float(x.get('lastPrice', 0) or 0),
                'change_pct': float(x.get('priceChangePercent', 0) or 0),
                'volume': float(x.get('quoteVolume', 0) or 0),
            }
    return out


def fetch_klines(symbol, interval='4h', limit=280):
    data = _get_json(KLINES_URL, {'symbol': symbol, 'interval': interval, 'limit': limit})
    cols = ['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    return df


def fetch_klines_cached(symbol, interval='4h', limit=280):
    """fetch_klines ile ayni sonucu verir; olusmakta olan mum kapanana kadar
    ayni DataFrame'i onbellekten dondurur (kapanmis mumlar degismez)."""
    key = (symbol, interval, limit)
    now = time.time()
    with _cache_lock:
        hit = _kline_cache.get(key)
    if hit and now < hit[1]:
        return hit[0]
    df = fetch_klines(symbol, interval, limit)
    if len(df):
        valid_until = df['close_time'].iloc[-1].timestamp() + 2  # kapanisin 2 sn sonrasi
    else:
        valid_until = now + 60
    with _cache_lock:
        _kline_cache[key] = (df, valid_until)
    return df


def daily_volatile(symbol):
    """Use only fully closed daily bars for the FINAL V1 volatility veto."""
    try:
        df = fetch_klines_cached(symbol, '1d', SCANNER_DAILY_LIMIT)
        if len(df) < cfg.VOLATILITY_PERCENTILE_LENGTH + cfg.VOLATILITY_ATR_LENGTH + 2:
            return False, None
        df = df.iloc[:-1].copy()  # forming day excluded
        df['atr'] = atr(df['high'], df['low'], df['close'], cfg.VOLATILITY_ATR_LENGTH)
        df['atrp'] = df['atr'] / df['close'] * 100.0
        s = df['atrp'].dropna()
        if len(s) < cfg.VOLATILITY_PERCENTILE_LENGTH:
            return False, None
        window = s.iloc[-cfg.VOLATILITY_PERCENTILE_LENGTH:]
        latest = float(s.iloc[-1])
        pct = float((window <= latest).mean() * 100.0)
        return bool(pct > cfg.VOLATILITY_HIGH_PERCENTILE or pct < cfg.VOLATILITY_LOW_PERCENTILE), pct
    except Exception:
        return False, None


def _reason_map(row):
    long_checks = {
        'EMA200 trend': bool(row.close > row.ema100),
        'EMA50 > EMA200': bool(row.ema50 > row.ema100),
        'ADX > threshold': bool(row.adx > cfg.ADX_THRESHOLD),
        'Supertrend bullish': bool(row.supertrend_bullish),
        'RSI > minimum': bool(row.rsi > cfg.RSI_LONG_THRESHOLD),
        'MACD bullish': bool(row.macd_long_ok) if cfg.USE_MACD_LONG_FILTER else True,
        'CCI': bool(row.cci > cfg.CCI_LONG_THRESHOLD),
        'Stoch cross': bool(row.stoch_bull_cross),
        'Stoch D > threshold': bool(row.stoch_d > cfg.STOCH_LONG_D_THRESHOLD),
    }
    short_checks = {
        'EMA200 trend': bool(row.close < row.ema100),
        'EMA50 < EMA200': bool(row.ema50 < row.ema100),
        'ADX > threshold': bool(row.adx > cfg.ADX_THRESHOLD),
        'Supertrend bearish': bool(row.supertrend_bearish),
        'RSI < threshold': bool(row.rsi < cfg.RSI_SHORT_THRESHOLD),
        'CCI': bool(row.cci < cfg.CCI_SHORT_THRESHOLD),
        'Stoch cross': bool(row.stoch_bear_cross),
        'Stoch K < threshold': bool(row.stoch_k < cfg.STOCH_SHORT_THRESHOLD),
    }
    return long_checks, short_checks


def scan_symbol(symbol, ticker):
    try:
        df = fetch_klines_cached(symbol, '4h', SCANNER_KLINE_LIMIT)
        if len(df) < 250:
            return {'symbol': symbol, 'signal': 'DATA', 'reason': 'Yetersiz 4H veri'}
        closed = df.iloc[:-1].copy()
        enriched = add_indicators(closed, cfg)
        row = enriched.iloc[-1]
        # Match the bot: only the latest fully closed 4H candle can trigger.
        long_ok = long_signal(enriched, len(enriched)-1, cfg)
        short_ok = short_signal(enriched, len(enriched)-1, cfg)

        volatile, atrp_pct = (False, None)
        if cfg.USE_VOLATILE_FILTER and (long_ok or short_ok):
            volatile, atrp_pct = daily_volatile(symbol)
            if volatile:
                long_ok = short_ok = False

        sig = 'LONG' if long_ok else ('SHORT' if short_ok else 'NO SIGNAL')
        long_checks, short_checks = _reason_map(row)
        i_last = len(enriched) - 1
        long_score = sum(1 for _, ok in long_conditions(enriched, i_last, cfg) if ok)
        short_score = sum(1 for _, ok in short_conditions(enriched, i_last, cfg) if ok)
        score = long_score if sig == 'LONG' else (short_score if sig == 'SHORT' else max(long_score, short_score))
        if volatile:
            reason = f'1D VOLATILE BLOCK | ATRP percentile={atrp_pct:.1f}'
        elif sig == 'LONG':
            reason = 'Tüm LONG koşulları sağlandı'
        elif sig == 'SHORT':
            reason = 'Tüm SHORT koşulları sağlandı'
        else:
            # Give the most useful missing reasons first.
            failed_l = [k for k,v in long_checks.items() if not v]
            failed_s = [k for k,v in short_checks.items() if not v]
            reason = 'LONG eksik: ' + ', '.join(failed_l[:3])
            if failed_s:
                reason += ' | SHORT eksik: ' + ', '.join(failed_s[:3])

        return {
            'symbol': symbol,
            'signal': sig,
            'price': ticker.get('price', float(row.close)),
            'change_pct': ticker.get('change_pct', 0),
            'volume': ticker.get('volume', 0),
            'rsi': round(float(row.rsi), 2),
            'adx': round(float(row.adx), 2),
            'cci': round(float(row.cci), 2),
            'st': 'BULL' if row.supertrend_bullish else 'BEAR',
            'macd': 'BULL' if row.macd_long_ok else 'BEAR',
            'stoch_k': round(float(row.stoch_k), 2),
            'stoch_d': round(float(row.stoch_d), 2),
            'atr': round(float(row.atr), 6),
            'atrp_percentile_1d': round(float(atrp_pct), 2) if atrp_pct is not None else None,
            'score': score,
            'score_max': 8,
            'candle_time': row.close_time.isoformat(),
            'reason': reason,
        }
    except Exception as e:
        return {'symbol': symbol, 'signal': 'ERROR', 'reason': str(e)[:180]}


def _scan_worker(symbols):
    with _lock:
        _state.update(status='SCANNING', started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                      last_error=None, symbols_total=len(symbols), symbols_done=0)
        # Onceki basarili sonuclar: bu taramada hata alan semboller icin korunur,
        # boylece 429/ban yuzunden tablo ERROR satirlariyla dolup bosalmaz.
        prev = {r['symbol']: r for r in _state.get('results', []) if r.get('symbol')}
    try:
        tickers = get_tickers()
        results = []
        errors = 0
        with ThreadPoolExecutor(max_workers=SCANNER_WORKERS) as ex:
            futs = {ex.submit(scan_symbol, s, tickers.get(s, {})): s for s in symbols}
            for fut in as_completed(futs):
                r = fut.result()
                if r.get('signal') == 'ERROR':
                    errors += 1
                    old = prev.get(r.get('symbol'))
                    if old and old.get('signal') != 'ERROR':
                        r = old
                results.append(r)
                with _lock:
                    _state['symbols_done'] += 1
        results.sort(key=lambda x: (0 if x.get('signal') == 'LONG' else 1 if x.get('signal') == 'SHORT' else 2, -(x.get('volume') or 0)))
        candle_times = [r.get('candle_time') for r in results if r.get('candle_time')]
        with _lock:
            _state['results'] = results
            _state['status'] = 'READY'
            _state['finished_at'] = datetime.now(timezone.utc).isoformat()
            _state['last_scan_candle'] = max(candle_times) if candle_times else None
            if binance_guard.in_cooldown():
                _state['last_error'] = f'Binance bekleme suresi aktif ({binance_guard.cooldown_remaining()} sn); eski sonuclar korundu'
        print(f'SCANNER | tarama bitti | {len(symbols)} sembol | hata={errors}', flush=True)
    except Exception as e:
        with _lock:
            _state['status'] = 'ERROR'
            _state['last_error'] = str(e)
            _state['finished_at'] = datetime.now(timezone.utc).isoformat()


def start_scan(force=False):
    with _lock:
        if _state['status'] == 'SCANNING':
            return False
        if not force and _state['finished_at']:
            try:
                age = time.time() - datetime.fromisoformat(_state['finished_at']).timestamp()
                if age < SCANNER_CACHE_SECONDS:
                    return False
            except Exception:
                pass
    # Binance bekleme suresindeyken yeni tarama baslatma (ban uzamasin)
    if binance_guard.in_cooldown():
        return False
    symbols = get_symbols()
    threading.Thread(target=_scan_worker, args=(symbols,), daemon=True).start()
    return True


def snapshot():
    with _lock:
        return dict(_state)


def ensure_background_scan():
    try:
        start_scan(force=False)
    except Exception as e:
        with _lock:
            _state['status'] = 'ERROR'
            _state['last_error'] = str(e)


def background_loop():
    # Re-check periodically. The signal engine itself uses only the latest closed 4H candle.
    while True:
        try:
            start_scan(force=False)
        except Exception as e:
            with _lock:
                _state['status'] = 'ERROR'
                _state['last_error'] = str(e)
        time.sleep(max(60, SCANNER_CACHE_SECONDS))
