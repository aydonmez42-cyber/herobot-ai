import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import config as cfg
from indicators import add_indicators, atr
from tradingview_data import fetch_tv_bars, add_close_time
from strategy import long_signal, short_signal
from us_stocks_universe import get_us_symbols

US_SCANNER_WORKERS = int(os.environ.get('US_SCANNER_WORKERS', '6'))
US_SCANNER_CACHE_SECONDS = int(os.environ.get('US_SCANNER_CACHE_SECONDS', '900'))
US_MIN_4H_BARS = int(os.environ.get('US_MIN_4H_BARS', '230'))

# US listings are split across NASDAQ/NYSE/AMEX and this universe list
# doesn't carry that metadata, so each symbol is tried against the three
# exchanges in order and the one that works is cached for the rest of the
# process's lifetime — avoids re-guessing on every scan cycle.
_EXCHANGES = ('NASDAQ', 'NYSE', 'AMEX')
_exchange_cache = {}
_exchange_lock = threading.Lock()


def _resolve_tv_bars(symbol, interval, bars):
    with _exchange_lock:
        known = _exchange_cache.get(symbol)
    if known:
        try:
            return fetch_tv_bars(f"{known}:{symbol}", interval=interval, bars=bars), known
        except Exception:
            pass  # fall through and re-probe in case the cached guess was wrong
    last_err = None
    for ex in _EXCHANGES:
        try:
            df = fetch_tv_bars(f"{ex}:{symbol}", interval=interval, bars=bars)
            if len(df):
                with _exchange_lock:
                    _exchange_cache[symbol] = ex
                return df, ex
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f'{symbol}: no exchange matched (tried {_EXCHANGES})')


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
    'universe_source': None,
}


def daily_atrp_percentile(symbol):
    try:
        d, _ = _resolve_tv_bars(symbol, "1D", max(400, cfg.VOLATILITY_PERCENTILE_LENGTH + 40))
        if len(d) < cfg.VOLATILITY_PERCENTILE_LENGTH + 30:
            return None
        d["atr"] = atr(d["high"], d["low"], d["close"], cfg.VOLATILITY_ATR_LENGTH)
        d["atrp"] = d["atr"] / d["close"] * 100
        s = d["atrp"].dropna()
        if len(s) < cfg.VOLATILITY_PERCENTILE_LENGTH:
            return None
        latest = float(s.iloc[-1])
        window = s.iloc[-cfg.VOLATILITY_PERCENTILE_LENGTH:]
        return float((window <= latest).mean() * 100)
    except Exception:
        return None


def _reason_map(row):
    long_checks = {
        'EMA100 trend': bool(row.close > row.ema100),
        'EMA50 > EMA100': bool(row.ema50 > row.ema100),
        'ADX > threshold': bool(row.adx > cfg.ADX_THRESHOLD),
        'Supertrend bullish': bool(row.supertrend_bullish),
        'RSI 55–72': bool(row.rsi > cfg.RSI_LONG_THRESHOLD and row.rsi <= cfg.RSI_LONG_MAX),
        'MACD bullish': bool(row.macd_long_ok) if cfg.USE_MACD_LONG_FILTER else True,
        'CCI > 100': bool(row.cci > cfg.CCI_LONG_THRESHOLD),
        'Stoch cross': bool(row.stoch_bull_cross),
        'Stoch D > 30': bool(row.stoch_d > cfg.STOCH_LONG_D_THRESHOLD),
    }
    short_checks = {
        'EMA100 trend': bool(row.close < row.ema100),
        'EMA50 < EMA100': bool(row.ema50 < row.ema100),
        'ADX > threshold': bool(row.adx > cfg.ADX_THRESHOLD),
        'Supertrend bearish': bool(row.supertrend_bearish),
        'RSI < 30': bool(row.rsi < cfg.RSI_SHORT_THRESHOLD),
        'CCI < -50': bool(row.cci < cfg.CCI_SHORT_THRESHOLD),
        'Stoch cross': bool(row.stoch_bear_cross),
        'Stoch K < 80': bool(row.stoch_k < cfg.STOCH_SHORT_THRESHOLD),
    }
    return long_checks, short_checks


def scan_symbol(symbol):
    try:
        df, exchange = _resolve_tv_bars(symbol, "240", max(300, US_MIN_4H_BARS + 40))
        df = add_close_time(df, hours=4)
        if len(df) < US_MIN_4H_BARS + 5:
            return {'symbol': symbol, 'market': 'US_STOCK', 'signal': 'DATA', 'reason': f'Yetersiz TradingView 4H veri ({len(df)})'}

        # Same reasoning as the BIST scanner: US equities also trade in
        # sessions, not 24/7, so a bar only counts once its 4H window has
        # actually closed in the exchange's own timezone.
        now_ny = pd.Timestamp.now(tz='America/New_York')
        closed = df[df['close_time'].dt.tz_convert('America/New_York') <= now_ny].copy()
        if closed.empty:
            return {'symbol': symbol, 'market': 'US_STOCK', 'signal': 'DATA', 'reason': 'Kapalı 4H mum yok'}

        enriched = add_indicators(closed, cfg)
        i = len(enriched) - 1
        row = enriched.iloc[i]
        long_ok = long_signal(enriched, i, cfg)
        short_ok = short_signal(enriched, i, cfg)
        atrp_pct = daily_atrp_percentile(symbol) if (long_ok or short_ok) else None
        volatile = False
        if cfg.USE_VOLATILE_FILTER and atrp_pct is not None:
            volatile = bool(atrp_pct > cfg.VOLATILITY_HIGH_PERCENTILE or atrp_pct < cfg.VOLATILITY_LOW_PERCENTILE)
            if volatile:
                long_ok = short_ok = False

        sig = 'LONG' if long_ok else ('SHORT' if short_ok else 'NO SIGNAL')
        long_checks, short_checks = _reason_map(row)
        if volatile:
            reason = f'1D VOLATILE BLOCK | ATRP percentile={atrp_pct:.1f}'
        elif sig == 'LONG':
            reason = 'TEST32 RSI72 LONG koşulları sağlandı'
        elif sig == 'SHORT':
            reason = 'TEST32 SHORT koşulları sağlandı'
        else:
            fl = [k for k, v in long_checks.items() if not v]
            fs = [k for k, v in short_checks.items() if not v]
            reason = 'LONG eksik: ' + ', '.join(fl[:3]) + ' | SHORT eksik: ' + ', '.join(fs[:3])

        last = closed.iloc[-1]
        try:
            daily, _ = _resolve_tv_bars(symbol, "1D", 12)
            daily = daily.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            now_ny2 = pd.Timestamp.now(tz='America/New_York')
            daily_local_date = daily['timestamp'].dt.tz_convert('America/New_York').dt.date
            closed_daily = daily.loc[daily_local_date < now_ny2.date()]
            if len(closed_daily) >= 2:
                day_before_close = float(closed_daily.iloc[-2]['close'])
                yesterday_close = float(closed_daily.iloc[-1]['close'])
                change = (yesterday_close / day_before_close - 1) * 100 if day_before_close else 0
            else:
                change = 0
        except Exception:
            change = 0
        return {
            'symbol': symbol,
            'market': 'US_STOCK',
            'exchange': exchange,
            'signal': sig,
            'price': round(float(last['close']), 4),
            'change_pct': round(float(change), 2),
            'volume': round(float(last.get('volume', 0) or 0), 0),
            'rsi': round(float(row.rsi), 2),
            'adx': round(float(row.adx), 2),
            'cci': round(float(row.cci), 2),
            'st': 'BULL' if row.supertrend_bullish else 'BEAR',
            'macd': 'BULL' if row.macd_long_ok else 'BEAR',
            'stoch_k': round(float(row.stoch_k), 2),
            'stoch_d': round(float(row.stoch_d), 2),
            'atr': round(float(row.atr), 6),
            'atrp_percentile_1d': round(float(atrp_pct), 2) if atrp_pct is not None else None,
            'candle_time': pd.Timestamp(row.close_time).isoformat(),
            'reason': reason,
            'short_note': 'Paper SHORT simülasyonudur; gerçek açığa satışın ödünç/marj kısıtlarını modellemez.',
        }
    except Exception as e:
        return {'symbol': symbol, 'market': 'US_STOCK', 'signal': 'ERROR', 'reason': str(e)[:180]}


def _scan_worker(symbols):
    with _lock:
        _state.update(status='SCANNING', started_at=datetime.now(timezone.utc).isoformat(), finished_at=None,
                      last_error=None, symbols_total=len(symbols), symbols_done=0)
    try:
        results = []
        with ThreadPoolExecutor(max_workers=US_SCANNER_WORKERS) as ex:
            futs = {ex.submit(scan_symbol, s): s for s in symbols}
            for fut in as_completed(futs):
                results.append(fut.result())
                with _lock:
                    _state['symbols_done'] += 1
        results.sort(key=lambda x: (0 if x.get('signal') == 'LONG' else 1 if x.get('signal') == 'SHORT' else 2, x.get('symbol', '')))
        candle_times = [r.get('candle_time') for r in results if r.get('candle_time')]
        with _lock:
            _state['results'] = results
            _state['status'] = 'READY'
            _state['finished_at'] = datetime.now(timezone.utc).isoformat()
            _state['last_scan_candle'] = max(candle_times) if candle_times else None
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
                if age < US_SCANNER_CACHE_SECONDS:
                    return False
            except Exception:
                pass
    symbols, source = get_us_symbols(force=force)
    with _lock:
        _state['universe_source'] = source
    threading.Thread(target=_scan_worker, args=(symbols,), daemon=True).start()
    return True


def snapshot():
    with _lock:
        return dict(_state)


def background_loop():
    while True:
        try:
            start_scan(force=False)
        except Exception as e:
            with _lock:
                _state['status'] = 'ERROR'
                _state['last_error'] = str(e)
        time.sleep(max(60, US_SCANNER_CACHE_SECONDS))
