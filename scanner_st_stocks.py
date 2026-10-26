"""
Supertrend scan (DAILY, ATR period 10, factor 7.8) for Borsa Istanbul and
Wall Street (S&P 500 + Nasdaq-100) - MANUAL ONLY.

Same Supertrend maths as scanner_st.py (TradingView ta.supertrend), applied to
CLOSED daily candles only (today's still-forming session is dropped).
LONG = price closed above the line (trend flipped UP), SHORT = below.
flip_bars = how many closed daily candles ago the trend last flipped
(0 = on the latest closed day).

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

WORKERS = int(os.environ.get('SCAN_ST_STOCK_WORKERS', '4'))
MIN_GAP_S = float(os.environ.get('SCAN_ST_STOCK_MIN_GAP_S', '120'))
DAILY_BARS = 300

_MARKETS = {
    'bist': {'tz': 'Europe/Istanbul'},
    'us': {'tz': 'America/New_York'},
}


class _Scan:
    def __init__(self, key):
        self.key = key
        self.tz = _MARKETS[key]['tz']
        self.lock = threading.Lock()
        self.last_finish_ts = 0.0
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
            return fetch_tv_bars(f'BIST:{symbol}', interval='1D', bars=DAILY_BARS), None
        from us_scanner import _resolve_tv_bars
        df, ex = _resolve_tv_bars(symbol, '1D', DAILY_BARS)
        return df, ex

    def analyse(self, symbol):
        try:
            df, exchange = self._bars(symbol)
            df = df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
            today = pd.Timestamp.now(tz=self.tz).date()
            local_date = df['timestamp'].dt.tz_convert(self.tz).dt.date
            closed = df.loc[local_date < today].reset_index(drop=True)   # closed sessions only
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
            return None

    def run(self):
        results, err = [], None
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
        with self.lock:
            self.state['results'] = results
            self.state['last_error'] = err
            self.state['status'] = 'ERROR' if (err and not results) else 'READY'
            self.state['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.last_finish_ts = time.time()
        flips = [r for r in results if r['flip_bars'] == 0]
        print(f'SCAN-ST-{self.key.upper()} | bitti | {len(results)} hisse | son mumda: '
              f'LONG={sum(1 for r in flips if r["trend"]=="UP")} '
              f'SHORT={sum(1 for r in flips if r["trend"]=="DOWN")} | err={err}', flush=True)

    def snapshot(self):
        with self.lock:
            out = dict(self.state)
            out['results'] = list(self.state['results'])
        out.update({'period': PERIOD, 'factor': FACTOR, 'interval': '1D', 'min_gap_s': MIN_GAP_S})
        wait = MIN_GAP_S - (time.time() - self.last_finish_ts) if self.last_finish_ts else 0
        out['next_allowed_in_s'] = max(0, int(wait))
        return out

    def start(self):
        with self.lock:
            if self.state['status'] == 'SCANNING':
                return False, 'Tarama zaten çalışıyor.'
        wait = MIN_GAP_S - (time.time() - self.last_finish_ts) if self.last_finish_ts else 0
        if wait > 0:
            return False, f'Son taramanın üzerinden çok az zaman geçti; {int(wait)} sn sonra tekrar deneyin.'
        with self.lock:
            self.state.update({'status': 'SCANNING', 'progress': 0, 'total': 0, 'last_error': None,
                               'started_at': datetime.now(timezone.utc).isoformat()})
        threading.Thread(target=self.run, daemon=True).start()
        return True, 'Tarama başlatıldı.'


_scans = {k: _Scan(k) for k in _MARKETS}


def snapshot(key):
    return _scans[key].snapshot()


def start_scan(key):
    return _scans[key].start()
