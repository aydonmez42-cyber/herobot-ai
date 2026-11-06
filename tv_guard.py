"""Shared pacing for every TradingView request (stock scans + the Süper Grafik).

TradingView answers a too-fast stream of websocket handshakes with "Handshake status 429
Too Many Requests". The stock scans (4 workers) and the chart used to open connections
independently, so clicking a stock during a scan - or clicking several quickly - failed.

call(fn, ...) runs fn through ONE gate:
  * at least TV_MIN_GAP_S between two starts (default 0.6 s);
  * on a 429 the whole process pauses (4 s, then 10 s, then 20 s) and the call is retried,
    at most TV_RETRIES times (default 3); other errors are raised unchanged.
"""
import os
import threading
import time

MIN_GAP_S = float(os.environ.get('TV_MIN_GAP_S', '0.6'))
RETRIES = int(os.environ.get('TV_RETRIES', '3'))
BACKOFF_S = (4.0, 10.0, 20.0)

_lock = threading.Lock()
_state = {'last': 0.0, 'pause_until': 0.0, 'n429': 0}


def is_rate_limited(exc):
    s = str(exc)
    return '429' in s or 'Too Many Requests' in s


def _gate():
    with _lock:
        now = time.time()
        wait = max(_state['pause_until'] - now, _state['last'] + MIN_GAP_S - now)
        if wait > 0:
            time.sleep(min(wait, 30.0))
        _state['last'] = time.time()


def call(fn, *args, **kwargs):
    attempt = 0
    while True:
        _gate()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not is_rate_limited(e) or attempt >= RETRIES:
                raise
            back = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            with _lock:
                _state['n429'] += 1
                _state['pause_until'] = max(_state['pause_until'], time.time() + back)
            print(f'TV GUARD | 429 -> {back:.0f} sn bekleniyor (deneme {attempt}/{RETRIES})', flush=True)
