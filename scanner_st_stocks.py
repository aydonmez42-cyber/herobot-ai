"""
Supertrend scan (DAILY or WEEKLY, ATR period 10) for Borsa Istanbul and
Wall Street (S&P 500 + Nasdaq-100) - MANUAL ONLY.

Same Supertrend maths as scanner_st.py (TradingView ta.supertrend), applied to
CLOSED candles only: daily = today's still-forming session is dropped; weekly = the
current week counts as closed from its Saturday on (stocks do not trade on weekends).
LONG = price closed above the line (trend flipped UP), SHORT = below.
flip_bars = how many closed candles (days or weeks) ago the trend last flipped
(0 = on the latest closed candle). Each (market, timeframe) keeps its own last result.

Data comes from TradingView (same helpers the existing BIST / US scanners use),
never from Binance, so it cannot touch the Binance rate limit. Runs only when
the dashboard button is pressed.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from scanner_st import supertrend, PERIOD, FACTOR
from tradingview_data import fetch_tv_bars
import tv_guard

WORKERS = int(os.environ.get('SCAN_ST_STOCK_WORKERS', '4'))
MIN_GAP_S = float(os.environ.get('SCAN_ST_STOCK_MIN_GAP_S', '120'))
DAILY_BARS = 300
WEEKLY_BARS = 260
TIMEFRAMES = {'1D': DAILY_BARS, '1W': WEEKLY_BARS}

_MARKETS = {
    'bist': {'tz': 'Europe/Istanbul'},
    'us': {'tz': 'America/New_York'},
}


class _Scan:
    def __init__(self, key, tf='1D'):
        self.key = key
        self.tf = tf
        self.bars = TIMEFRAMES[tf]
        self.tz = _MARKETS[key]['tz']
        self.lock = threading.Lock()
        self.last_finish_ts = 0.0
        self.failed = 0
        self.state = {'status': 'IDLE', 'results': [], 'started_at': None, 'finished_at': None,
                      'progress': 0, 'total': 0, 'last_error': None, 'partial': False}

    def _symbols(self):
        if self.key == 'bist':
            from bist_xutum_universe import get_xutum_symbols
            return get_xutum_symbols(force=False)[0]
        from us_stocks_universe import get_us_symbols
        return get_us_symbols(force=False)[0]

    def _bars(self, symbol):
        if self.key == 'bist':
            return tv_guard.call(fetch_tv_bars, f'BIST:{symbol}', interval=self.tf, bars=self.bars), None
        from us_scanner import _resolve_tv_bars
        df, ex = tv_guard.call(_resolve_tv_bars, symbol, self.tf, self.bars)
        return df, ex

    def _today(self):
        return pd.Timestamp.now(tz=self.tz).date()

    def _closed(self, df):
        """Only finished candles. Daily: sessions before today. Weekly: weeks whose Monday + 5 days
        (the Saturday) is today or earlier."""
        today = self._today()
        local_date = df['timestamp'].dt.tz_convert(self.tz).dt.date
        if self.tf == '1D':
            return df.loc[local_date < today].reset_index(drop=True)
        ld = pd.to_datetime(local_date)
        monday = ld - pd.to_timedelta(ld.dt.weekday, unit='D')
        sat = (monday + pd.Timedelta(days=5)).dt.date
        return df.loc[sat <= today].reset_index(drop=True)

    def analyse(self, symbol):
        try:
            df, exchange = self._bars(symbol)
            df = df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            closed = self._closed(df)                                   # closed candles only
            if len(closed) < PERIOD + 30:
                return None
            up, line = supertrend(closed)
            last = len(closed) - 1
            if np.isnan(line[last]):
                return None
            j = last
            while j > 0 and up[j - 1] == up[last] and not np.isnan(line[j - 1]):
                j -= 1
            price = float(closed.iloc[last]['close'])
            prev = float(closed.iloc[last - 1]['close'])
            return {
                'symbol': symbol, 'exchange': exchange, 'price': price,
                'change_pct': round((price / prev - 1) * 100, 2) if prev else 0,
                'volume': float(closed.iloc[last].get('volume', 0) or 0),
                'trend': 'UP' if up[last] else 'DOWN',
                'st': float(line[last]),
                'dist_pct': (price - float(line[last])) / price * 100.0 if price else None,
                'flip_bars': int(last - j),
                'flip_time': closed.iloc[j]['timestamp'].isoformat(),
            }
        except Exception:
            with self.lock:
                self.failed += 1          # data could not be fetched (not "too little history")
            return None

    def run(self):
        results, err = [], None
        self.failed = 0
        try:
            symbols = self._symbols()
            with self.lock:
                self.state['total'] = len(symbols)
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = [ex.submit(self.analyse, s) for s in symbols]
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        results.append(r)
                    with self.lock:
                        self.state['progress'] += 1
        except Exception as e:
            err = f'{type(e).__name__}: {str(e)[:160]}'
        if not err and self.failed:
            err = (f'{self.failed} hisse için veri alınamadı (TradingView istek sınırı olabilir); '
                   f'birkaç dakika sonra tekrar tarayın.')
        with self.lock:
            self.state['results'] = results
            self.state['last_error'] = err
            self.state['status'] = 'ERROR' if (err and not results) else 'READY'
            self.state['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.last_finish_ts = time.time()
        flips = [r for r in results if r['flip_bars'] == 0]
        print(f'SCAN-ST-{self.key.upper()}-{self.tf} | bitti | {len(results)} hisse | son mumda: '
              f'LONG={sum(1 for r in flips if r["trend"]=="UP")} '
              f'SHORT={sum(1 for r in flips if r["trend"]=="DOWN")} | err={err}', flush=True)

    def snapshot(self):
        with self.lock:
            out = dict(self.state)
            out['results'] = list(self.state['results'])
        out.update({'interval': self.tf, 'min_gap_s': MIN_GAP_S})
        wait = MIN_GAP_S - (time.time() - self.last_finish_ts) if self.last_finish_ts else 0
        out['next_allowed_in_s'] = max(0, int(wait))
        return out

    def start(self):
        with self.lock:
            if self.state['status'] == 'SCANNING':
                return False, 'Tarama zaten çalışıyor.'
        for other_tf, other in ((t, _scans[(self.key, t)]) for t in TIMEFRAMES if t != self.tf):
            if other.state.get('status') == 'SCANNING':
                return False, 'Bu piyasada başka bir zaman diliminin taraması çalışıyor, bitmesini bekleyin.'
        wait = MIN_GAP_S - (time.time() - self.last_finish_ts) if self.last_finish_ts else 0
        if wait > 0:
            return False, f'Son taramanın üzerinden çok az zaman geçti; {int(wait)} sn sonra tekrar deneyin.'
        with self.lock:
            self.state.update({'status': 'SCANNING', 'progress': 0, 'total': 0, 'last_error': None,
                               'started_at': datetime.now(timezone.utc).isoformat()})
        threading.Thread(target=self.run, daemon=True).start()
        return True, 'Tarama başlatıldı.'


_scans = {(k, tf): _Scan(k, tf) for k in _MARKETS for tf in TIMEFRAMES}


def _tf(tf):
    tf = str(tf or '1D').upper()
    return tf if tf in TIMEFRAMES else '1D'


def snapshot(key, tf='1D'):
    return _scans[(key, _tf(tf))].snapshot()


def start_scan(key, tf='1D'):
    return _scans[(key, _tf(tf))].start()
