import json
import os
import threading
from datetime import datetime, timezone

DATA_DIR = os.environ.get('PAPER_DATA_DIR', '')
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.environ.get('PAPER_STATE_FILE', os.path.join(DATA_DIR, 'paper_state.json') if DATA_DIR else 'paper_state.json')
TRADES_FILE = os.environ.get('PAPER_TRADES_FILE', os.path.join(DATA_DIR, 'paper_trades.csv') if DATA_DIR else 'paper_trades.csv')

# One process-wide lock guards every read-modify-write of STATE_FILE. The main
# bot loop (paper_trading.py) and the dashboard's watchlist API (dashboard.py,
# running on its own thread) both write to this same file, so without a shared
# lock a watchlist add/remove from the dashboard could be silently overwritten
# by the bot loop's next save (or vice versa).
_lock = threading.Lock()


def _defaults(starting_equity=10000.0):
    return {
        'equity': starting_equity,
        'position': None,
        'positions': {},            # symbol -> open paper position (watchlist crypto symbols)
        'watchlist': {},            # symbol -> {market, added_at, added_signal, note}
        'watchlist_signals': {},    # symbol -> {price, final, updated_at}
        'symbol_last_closed': {},   # symbol -> last processed closed-candle ISO time
        'last_closed_time': None,
        'last_processed_entry_time': None,
        'market_prices': {},
        'signals': {},
        'last_heartbeat': None,
        'last_daily_report_date': None,
        'ai_analysis': None,           # last AI Trade Analyst result dict (read-only)
        'ai_analyst_last_run': None,   # {'at': iso, 'total_trades': int} gating info
    }


def _read_raw(starting_equity=10000.0):
    if not os.path.exists(STATE_FILE):
        return _defaults(starting_equity)
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        s = json.load(f)
    d = _defaults(starting_equity)
    d.update(s)
    # Make sure keys added after this file was first written are always present.
    for k, v in _defaults(starting_equity).items():
        s.setdefault(k, v)
    return s


def load_state(starting_equity=10000.0):
    with _lock:
        return _read_raw(starting_equity)


def save_state(s):
    with _lock:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


def update_state(mutate_fn, starting_equity=10000.0):
    """Read-modify-write the state file atomically under the shared lock.
    mutate_fn(state) mutates the dict in place; the result is persisted and returned."""
    with _lock:
        s = _read_raw(starting_equity)
        mutate_fn(s)
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        return s


def add_to_watchlist(symbol, market, signal=None, note=''):
    symbol = symbol.strip().upper()

    def m(s):
        s['watchlist'][symbol] = {
            'symbol': symbol,
            'market': market,
            'added_signal': signal,
            'added_at': datetime.now(timezone.utc).isoformat(),
            'note': note,
        }
    return update_state(m)


def remove_from_watchlist(symbol):
    symbol = symbol.strip().upper()

    def m(s):
        s['watchlist'].pop(symbol, None)
        s.get('positions', {}).pop(symbol, None)
        s.get('watchlist_signals', {}).pop(symbol, None)
        s.get('symbol_last_closed', {}).pop(symbol, None)
    return update_state(m)
