import json, os, time
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

import config as cfg
from indicators import add_indicators
from regime import is_volatile_daily
from tradingview_data import add_close_time
from strategy import long_signal, short_signal
from dashboard import start_dashboard
from telegram_notifier import send_message, entry_message, exit_message, daily_report, verify_connection
from state_store import load_state as _load_state, save_state, STATE_FILE, TRADES_FILE
import ai_analyst
import auth
import live_trading
import threading

POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '30'))
# The dashboard's own paper account starts at PAPER_ACCOUNT_INITIAL_CAPITAL
# (not the backtest-anchored INITIAL_CAPITAL); a state file that already
# exists on disk keeps its own persisted equity regardless of this value
# (see state_store._defaults) — an admin can reset it from /admin.
STARTING_EQUITY = float(os.environ.get('PAPER_INITIAL_CAPITAL', str(cfg.PAPER_ACCOUNT_INITIAL_CAPITAL)))

# ---------------------------------------------------------------------------
# ENTRY GUARDS — protective locks on NEW entries. They now apply ONLY to LIVE
# (real-money) copies, PER MEMBER (see live_trading.py); the demo/paper account
# trades every signal unprotected (set DEMO_ENTRY_GUARDS=on to bring the old
# demo behaviour back). The BTC lock state itself is still computed here and
# handed to live_trading with every entry signal. Neither touches exits: open
# positions keep being managed by their own SL/TP/trailing logic.
#
#  1) BTC LOCK  — blocks new LONG entries while Bitcoin is falling. Altcoins
#     follow BTC, so a BTC drop turns many "independent" longs into one
#     correlated bet. Triggers on a fast 1h drop or a 4h drop; releases only
#     after a minimum hold time AND once the 4h move has stabilised.
#  2) LOSS BRAKE — if N stop-losses hit within a short window, ALL new
#     entries pause for a few hours (a cluster of stops usually means the
#     market just turned against the whole book).
#  3) DIRECTION LIMIT — cap on how many crypto/stock watchlist positions can
#     be open in the SAME direction at once (generous by default).
#  4) LATE-ENTRY FILTER — skips an entry when price has already run away from
#     the signal (chasing) or moved against it (stale signal).
#  5) BREAK-EVEN STOP — once a position is +BREAKEVEN_ATR in profit its stop
#     moves to entry (+ a small fee buffer) so it can't turn into a full loss.
#  6) EARLY EXIT (default 'log' = observe only) — while the BTC lock is
#     active, closes LONG positions that are already well under water.
#
# Every setting is an environment variable (Railway -> Variables). Each guard
# has a MODE: 'on' (enforce), 'log' (only log what it WOULD have blocked —
# good for trying thresholds safely) or 'off'.
# ---------------------------------------------------------------------------
def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return float(default)

def _env_mode(name, default='on'):
    v = os.environ.get(name, default).strip().lower()
    return v if v in ('on', 'log', 'off') else default

BTC_LOCK_MODE = _env_mode('BTC_LOCK_MODE', 'on')
BTC_LOCK_1H_DROP_PCT = _env_float('BTC_LOCK_1H_DROP_PCT', 1.0)     # lock if BTC fell >= this % in ~1h
BTC_LOCK_4H_DROP_PCT = _env_float('BTC_LOCK_4H_DROP_PCT', 2.0)     # ...or >= this % in ~4h
BTC_LOCK_RELEASE_PCT = _env_float('BTC_LOCK_RELEASE_PCT', 0.5)     # release when 4h change is back above -this %
BTC_LOCK_MIN_HOURS = _env_float('BTC_LOCK_MIN_HOURS', 2.0)         # never release before this many hours

LOSS_BRAKE_MODE = _env_mode('LOSS_BRAKE_MODE', 'on')
LOSS_BRAKE_STOP_COUNT = int(_env_float('LOSS_BRAKE_STOP_COUNT', 3))      # this many stops...
LOSS_BRAKE_WINDOW_MIN = _env_float('LOSS_BRAKE_WINDOW_MIN', 60)          # ...within this many minutes
LOSS_BRAKE_PAUSE_HOURS = _env_float('LOSS_BRAKE_PAUSE_HOURS', 12)        # ...pauses new entries this long

DIRECTION_LIMIT_MODE = _env_mode('DIRECTION_LIMIT_MODE', 'on')
MAX_SAME_DIRECTION = int(_env_float('MAX_SAME_DIRECTION', 10))     # max simultaneous LONG (and, separately, SHORT) watchlist positions

LATE_ENTRY_MODE = _env_mode('LATE_ENTRY_MODE', 'on')
LATE_ENTRY_CHASE_ATR = _env_float('LATE_ENTRY_CHASE_ATR', 1.5)      # skip if price already ran this many ATR in the signal's favour
LATE_ENTRY_ADVERSE_ATR = _env_float('LATE_ENTRY_ADVERSE_ATR', 1.0)  # skip if price moved this many ATR AGAINST the signal

# Risk/reward tuning ------------------------------------------------------
# Optional Railway overrides of the strategy multipliers (unset = config.py values).
for _n in ('ATR_SL_MULTIPLIER', 'ATR_SHORT_SL_MULTIPLIER', 'ATR_LONG_TP_MULTIPLIER', 'ATR_SHORT_TP_MULTIPLIER'):
    if os.environ.get(_n):
        setattr(cfg, _n, _env_float(_n, getattr(cfg, _n)))
# Per-trade loss cap in USD (demo position size; members' losses scale with their size).
# The stop distance is the smaller of "ATR x multiplier" and "MAX_LOSS_USD / quantity",
# but never tighter than MIN_SL_ATR x ATR (a stop inside normal noise only gets hit at random).
# MAX_LOSS_USD=0 turns the cap off.
MAX_LOSS_USD = _env_float('MAX_LOSS_USD', 70.0)
MIN_SL_ATR = _env_float('MIN_SL_ATR', 1.0)


def _sl_distance(mult, atr, qty):
    d = mult * atr
    if MAX_LOSS_USD > 0 and qty and qty > 0:
        d = min(d, max(MAX_LOSS_USD / qty, MIN_SL_ATR * atr))
    return d


BREAKEVEN_MODE = _env_mode('BREAKEVEN_MODE', 'on')
BREAKEVEN_ATR = _env_float('BREAKEVEN_ATR', 1.5)                    # move stop to entry once this many ATR in profit
BREAKEVEN_BUFFER_ATR = _env_float('BREAKEVEN_BUFFER_ATR', 0.1)      # ...plus this buffer (covers fees)

# Tiered profit lock (USD, measured on the demo position size).
# Tier 1 is a "double touch" (filters the first noisy spike):
#   1) profit reaches ARM_USD            -> first touch (nothing is closed)
#   2) profit then falls below LOCK_USD  -> dip tolerated, normal SL keeps protecting
#   3) profit reaches ARM_USD again      -> lock armed
#   4) profit falls back to LOCK_USD     -> position closed (reason PROFIT_LOCK)
# Higher tiers ("arm:lock", single touch) engage as soon as the peak profit
# has reached their arm level, and ratchet the lock up: once a position has
# seen 100 it closes at 75, once it has seen 150 it closes at 125.
PROFIT_LOCK_MODE = _env_mode('PROFIT_LOCK_MODE', 'on')
PROFIT_LOCK_ARM_USD = _env_float('PROFIT_LOCK_ARM_USD', 50.0)
PROFIT_LOCK_USD = _env_float('PROFIT_LOCK_USD', 30.0)


def _parse_lock_tiers(raw):
    tiers = []
    for part in str(raw or '').split(','):
        try:
            arm, lock = part.strip().split(':')
            arm, lock = float(arm), float(lock)
            if arm > 0 and 0 < lock < arm:
                tiers.append((arm, lock))
        except Exception:
            continue
    return sorted(tiers)


PROFIT_LOCK_TIERS = _parse_lock_tiers(os.environ.get('PROFIT_LOCK_TIERS', '100:75,150:125'))

EARLY_EXIT_MODE = _env_mode('EARLY_EXIT_MODE', 'log')              # 'log' = only report what it would close
EARLY_EXIT_LOSS_ATR = _env_float('EARLY_EXIT_LOSS_ATR', 1.5)        # LONG already down this many ATR while BTC lock is active

# 15-minute reversal exit. The position is opened on a 4H signal, but once it is
# open the 15m chart is watched on CLOSED 15m candles: a LONG is closed when the
# 15m strategy gives SHORT_COUNT SHORT signals in a row (no LONG signal in
# between; a LONG signal resets the count). A SHORT is closed symmetrically on
# LONG signals. Candles without any signal neither count nor reset. The exit is
# taken only after the confirming candle has CLOSED (never on a forming candle),
# and ONLY while the position is at a loss (price beyond the entry).
# 'log' = only report, 'off' = disabled.
M15_EXIT_MODE = _env_mode('M15_EXIT_MODE', 'on')
M15_EXIT_COUNT = max(1, int(_env_float('M15_EXIT_COUNT', 2)))

# Trend stop (4H, closed candles): a LONG is stopped out when a 4H candle CLOSES
# below EMA(TREND_SL_EMA) while the Supertrend (scanner_st PERIOD/FACTOR, 10/7.8)
# is DOWN and flipped down AFTER the entry; a SHORT symmetrically (close above
# the EMA + Supertrend flipped UP after entry). Works as an extra stop in front
# of the far ATR stop. 'log' = only report, 'off' = disabled.
TREND_SL_MODE = _env_mode('TREND_SL_MODE', 'on')
TREND_SL_EMA = int(_env_float('TREND_SL_EMA', 50))

# Entry wait ("Ekle" lock). A coin added from the scanner does NOT open a position
# at once: the 4H signal may be hours old. The bot waits until the last CLOSED
# 15m, 1h and 4h candles all point the trade's way (LONG: close > open), then
# enters (the normal entry guards still apply). The coin is dropped from the
# watchlist when the last ENTRY_WAIT_RED_COUNT closed 1h candles are all against
# the trade, when the 4H signal is gone, or after ENTRY_WAIT_MAX_HOURS.
# 'log' = only report what it would do and enter as before, 'off' = disabled.
ENTRY_WAIT_MODE = _env_mode('ENTRY_WAIT_MODE', 'on')
ENTRY_WAIT_MAX_HOURS = _env_float('ENTRY_WAIT_MAX_HOURS', 24)
ENTRY_WAIT_RED_COUNT = max(1, int(_env_float('ENTRY_WAIT_RED_COUNT', 3)))

# Auto add: after every 4H scan the coins that give a LONG signal are added to the
# watchlist by the bot itself (what the "+ Ekle" button did by hand). Each new
# coin then goes through the entry wait above, so a position opens only when the
# 15m / 1h / 4h candles agree. At most AUTO_ADD_MAX_PER_SCAN coins are added per
# 4H candle (best scanner score first, then volume; the quota counts across the
# repeated scans of that candle) and never beyond the watchlist
# limit. Auto-added coins that have no position, are not waiting and no longer
# show a LONG signal are removed again so the list does not fill up.
# 'log' = only report what it would add, 'off' = disabled.
AUTO_ADD_MODE = _env_mode('AUTO_ADD_MODE', 'on')
AUTO_ADD_MAX_PER_SCAN = max(1, int(_env_float('AUTO_ADD_MAX_PER_SCAN', 10)))
AUTO_ADD_MAX_SCAN_AGE_H = _env_float('AUTO_ADD_MAX_SCAN_AGE_H', 6)

# A watchlist coin whose position was closed at a LOSS by a stop (ATR stop,
# break-even stop, trailing stop, trend stop) is removed from the watchlist
# ("Deneme İşlemleri") instead of waiting there. The closed trade stays in the
# history. Auto add will not bring the coin back for STOP_REMOVE_COOLDOWN_H hours.
# Skipped while a member still holds a real position in the coin.
STOP_REMOVE_MODE = _env_mode('STOP_REMOVE_MODE', 'on')
STOP_REMOVE_COOLDOWN_H = _env_float('STOP_REMOVE_COOLDOWN_H', 24)
STOP_REMOVE_REASONS = ('ATR_SL', 'BREAKEVEN_SL', 'ATR_TRAILING_SL', 'TREND_SL')

# Demo/paper account: guards OFF by default (all locks and limits are live-only,
# per member). 'on' restores the previous behaviour for the demo as well.
DEMO_ENTRY_GUARDS = os.environ.get('DEMO_ENTRY_GUARDS', 'off').strip().lower() in ('1', 'true', 'yes', 'on')

_btc_cache = {'ts': 0.0, 'data': None}


def watchlist_position_usd():
    """The USD notional sized into each watchlist ('+ Ekle') paper trade.
    Admin-editable from /admin (persisted in auth.WATCHLIST_SETTINGS_FILE);
    falls back to cfg.WATCHLIST_POSITION_USD if never set."""
    try:
        v = auth.get_watchlist_settings().get('position_usd')
        return float(v) if v else cfg.WATCHLIST_POSITION_USD
    except Exception:
        return cfg.WATCHLIST_POSITION_USD

USD_M_URL = 'https://fapi.binance.com/fapi/v1/klines'
COIN_M_URL = 'https://dapi.binance.com/dapi/v1/klines'


def load_state():
    return _load_state(STARTING_EQUITY)


def fetch(symbol, market_type, limit=250, interval=None):
    url = COIN_M_URL if market_type == 'COIN_M' else USD_M_URL
    use_interval = interval or cfg.INTERVAL
    r = requests.get(url, params={'symbol': symbol, 'interval': use_interval, 'limit': limit}, timeout=20)
    r.raise_for_status()
    data = r.json()
    cols = ['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open','high','low','close','volume','quote_volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True)
    return df


_kline_cache = {}


def fetch_cached(symbol, market_type, interval, limit):
    """Binance klines with far fewer requests (the bot was hitting HTTP 429).
    - Daily ('1d') candles: fetched once per UTC day per symbol; the strategy
      only uses fully closed daily candles, which cannot change intraday. If a
      refresh fails, the previous copy is used instead of skipping the coin.
    - Intraday candles: the full history is fetched only when a new candle has
      started; in between, only the last 2 candles are requested (the forming
      candle drives stops / current price) and merged into the cached history.
    Signals and indicators are computed on exactly the same data as before."""
    key = (symbol, market_type, interval)
    now = pd.Timestamp.now(tz='UTC')
    c = _kline_cache.get(key)
    if interval == '1d':
        today = now.date()
        if c and c['day'] == today:
            return c['df'].copy()
        try:
            df = fetch(symbol, market_type, limit, interval=interval)
        except Exception:
            if c:
                return c['df'].copy()
            raise
        _kline_cache[key] = {'day': today, 'df': df}
        return df.copy()
    if c is not None and now < c['df'].iloc[-1]['close_time']:
        tail = fetch(symbol, market_type, 2, interval=interval)
        df = c['df']
        df = df[df['open_time'] < tail.iloc[0]['open_time']]
        df = pd.concat([df, tail], ignore_index=True)
        if len(df) > limit:
            df = df.iloc[-limit:].reset_index(drop=True)
        c['df'] = df
        return df.copy()
    df = fetch(symbol, market_type, limit, interval=interval)
    _kline_cache[key] = {'df': df}
    return df.copy()


def log_trade(t):
    exists = os.path.exists(TRADES_FILE)
    pd.DataFrame([t]).to_csv(TRADES_FILE, mode='a', header=not exists, index=False)


def exec_price(raw, side):
    return raw * (1 + cfg.SLIPPAGE_RATE) if side == 'BUY' else raw * (1 - cfg.SLIPPAGE_RATE)


def close_price(raw, side):
    # side is the position side being closed: LONG sells, SHORT buys
    return exec_price(raw, 'SELL' if side == 'LONG' else 'BUY')


def fee(notional):
    return abs(notional) * cfg.FEE_RATE


# ---------------------------------------------------------------------------
# Entry guards (BTC lock + loss brake)
# ---------------------------------------------------------------------------

def _parse_iso(s):
    dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def btc_snapshot(force=False):
    """BTCUSDT price + ~1h and ~4h % change from 15-minute klines (the last
    candle is still forming, so its close is the current price). Cached for
    60s so the 30s polling loop makes at most one extra request per minute.
    Returns None if Binance can't be reached — callers must fail OPEN."""
    now = time.time()
    if not force and _btc_cache['data'] and now - _btc_cache['ts'] < 60:
        return _btc_cache['data']
    try:
        df = fetch('BTCUSDT', 'USD_M', 20, interval='15m')
        closes = df['close'].astype(float).tolist()
        price = closes[-1]
        data = {
            'price': price,
            'change_1h': (price / closes[-5] - 1) * 100,    # 4 candles back ~ 1h
            'change_4h': (price / closes[-17] - 1) * 100,   # 16 candles back ~ 4h
        }
    except Exception as e:
        print(f'BTC LOCK | fetch ERROR | {type(e).__name__}: {e}', flush=True)
        return None
    _btc_cache['ts'] = now
    _btc_cache['data'] = data
    return data


def update_btc_lock(state, now):
    """Re-evaluates the BTC lock once per polling cycle and stores it in
    state['btc_lock']. Admin Telegram message on every lock/release."""
    if BTC_LOCK_MODE == 'off':
        state.pop('btc_lock', None)
        return
    snap = btc_snapshot()
    lock = state.get('btc_lock') or {'active': False}
    if snap is None:
        return  # keep the last known state; never block (or release) on missing data
    c1, c4, px = snap['change_1h'], snap['change_4h'], snap['price']
    lock['change_1h'] = round(c1, 2); lock['change_4h'] = round(c4, 2); lock['price'] = round(px, 2)
    lock['checked_at'] = now.isoformat()
    lock['enforced'] = (BTC_LOCK_MODE == 'on')
    triggered = c1 <= -BTC_LOCK_1H_DROP_PCT or c4 <= -BTC_LOCK_4H_DROP_PCT
    if not lock.get('active'):
        if triggered:
            lock['active'] = True
            lock['since'] = now.isoformat()
            lock['reason'] = f'BTC 1s {c1:+.2f}% / 4s {c4:+.2f}%'
            print(f"BTC LOCK | ACTIVATED ({BTC_LOCK_MODE}) | {lock['reason']} | BTC={px:,.0f}", flush=True)
            try:
                send_message(f"🔒 BTC KİLİDİ AÇILDI\n\n{lock['reason']} | BTC {px:,.0f}\n"
                             + ('Yeni LONG girişleri durduruldu (açık pozisyonlar yönetilmeye devam eder).' if BTC_LOCK_MODE == 'on'
                                else 'TEST MODU: sadece loglanıyor, giriş engellenmiyor.'))
            except Exception:
                pass
    else:
        held_h = (now - _parse_iso(lock.get('since', now.isoformat()))).total_seconds() / 3600
        if held_h >= BTC_LOCK_MIN_HOURS and c4 > -BTC_LOCK_RELEASE_PCT and not triggered:
            lock['active'] = False
            lock['released_at'] = now.isoformat()
            print(f"BTC LOCK | RELEASED | BTC 1s {c1:+.2f}% / 4s {c4:+.2f}% | held {held_h:.1f}h", flush=True)
            try:
                send_message(f"🔓 BTC KİLİDİ KALKTI\n\nBTC 1s {c1:+.2f}% / 4s {c4:+.2f}% | {held_h:.1f} saat sürdü.\nYeni LONG girişleri tekrar serbest.")
            except Exception:
                pass
    state['btc_lock'] = lock


def brake_active(state, now):
    lb = state.get('loss_brake') or {}
    try:
        return bool(lb.get('until')) and _parse_iso(lb['until']) > now
    except Exception:
        return False


def record_stop_and_maybe_brake(state, now):
    """Called when a crypto watchlist position closes at a LOSS via a stop.
    N such stops within the window start a pause on all new entries."""
    if LOSS_BRAKE_MODE == 'off' or not DEMO_ENTRY_GUARDS:
        return  # the loss brake is per-member and live-only now (live_trading.py)
    cutoff = now - timedelta(hours=24)
    stops = [t for t in state.get('recent_stops', []) if _parse_iso(t) >= cutoff]
    stops.append(now.isoformat())
    state['recent_stops'] = stops
    window_start = now - timedelta(minutes=LOSS_BRAKE_WINDOW_MIN)
    n = sum(1 for t in stops if _parse_iso(t) >= window_start)
    if n >= LOSS_BRAKE_STOP_COUNT and not brake_active(state, now):
        until = now + timedelta(hours=LOSS_BRAKE_PAUSE_HOURS)
        state['loss_brake'] = {'until': until.isoformat(), 'triggered_at': now.isoformat(),
                               'stops': n, 'enforced': LOSS_BRAKE_MODE == 'on'}
        print(f'LOSS BRAKE | TRIGGERED ({LOSS_BRAKE_MODE}) | {n} stops in {LOSS_BRAKE_WINDOW_MIN:.0f} min | new entries paused until {until.isoformat()}', flush=True)
        try:
            send_message(f"⛔ ZARAR FRENİ DEVREDE\n\n{LOSS_BRAKE_WINDOW_MIN:.0f} dakika içinde {n} stop oldu.\n"
                         + (f"Yeni girişler {LOSS_BRAKE_PAUSE_HOURS:g} saat durduruldu." if LOSS_BRAKE_MODE == 'on'
                            else 'TEST MODU: sadece loglanıyor, giriş engellenmiyor.'))
        except Exception:
            pass


LAST_BLOCK_CODES = []   # guard names behind the most recent block (read right after entry_guard_check)


def entry_guard_check(state, symbol, side, now, signal_close=None, atr=None, current_price=None, skip_direction_limit=False, quiet=False):
    """Returns (allowed, reason). Fails OPEN on any internal error so a bug
    here can never stop the trading loop."""
    global LAST_BLOCK_CODES
    LAST_BLOCK_CODES = []
    if not DEMO_ENTRY_GUARDS:
        return True, None  # demo trades every signal; guards are enforced per member on the live copy
    try:
        hits = []  # (guard_name, mode, text)
        if LOSS_BRAKE_MODE != 'off' and brake_active(state, now):
            until = (state.get('loss_brake') or {}).get('until', '')
            hits.append(('LOSS_BRAKE', LOSS_BRAKE_MODE, f'zarar freni aktif (bitiş {until})'))
        if side == 'LONG' and BTC_LOCK_MODE != 'off':
            lock = state.get('btc_lock') or {}
            if lock.get('active'):
                hits.append(('BTC_LOCK', BTC_LOCK_MODE, f"BTC kilidi aktif ({lock.get('reason', '')})"))
        if DIRECTION_LIMIT_MODE != 'off' and not skip_direction_limit:
            same = sum(1 for p in (state.get('positions') or {}).values() if p.get('side') == side)
            if same >= MAX_SAME_DIRECTION:
                hits.append(('DIRECTION_LIMIT', DIRECTION_LIMIT_MODE, f'aynı yönde {same} açık {side} var (limit {MAX_SAME_DIRECTION})'))
        if LATE_ENTRY_MODE != 'off' and signal_close and atr and current_price:
            move = (current_price - signal_close) / atr if side == 'LONG' else (signal_close - current_price) / atr
            if move > LATE_ENTRY_CHASE_ATR:
                hits.append(('LATE_ENTRY', LATE_ENTRY_MODE, f'fiyat sinyalden {move:.1f} ATR uzaklaştı (kovalama)'))
            elif move < -LATE_ENTRY_ADVERSE_ATR:
                hits.append(('LATE_ENTRY', LATE_ENTRY_MODE, f'fiyat sinyalin tersine {abs(move):.1f} ATR gitti (sinyal bayatladı)'))
        if not hits:
            return True, None
        enforcing = [h for h in hits if h[1] == 'on']
        text = ' + '.join(h[2] for h in hits)
        if enforcing:
            LAST_BLOCK_CODES = [h[0] for h in enforcing]
            if not quiet:
                print(f'ENTRY BLOCKED | {side} {symbol} | {text}', flush=True)
            return False, text
        if not quiet:
            print(f'ENTRY GUARD (log modu, engellenmedi) | {side} {symbol} | {text}', flush=True)
        return True, None
    except Exception as e:
        print(f'ENTRY GUARD | ERROR (fail-open) | {type(e).__name__}: {e}', flush=True)
        return True, None


def check_early_exit(state, symbol, price, now):
    """While the BTC lock is active, closes a LONG watchlist position that is
    already down EARLY_EXIT_LOSS_ATR (well before its far-away ATR stop).
    'log' mode only reports it (once per position). Returns True if closed."""
    if EARLY_EXIT_MODE == 'off':
        return False
    p = (state.get('positions') or {}).get(symbol)
    if not p or p.get('side') != 'LONG':
        return False
    if not (state.get('btc_lock') or {}).get('active'):
        return False
    atr = float(p.get('atr') or 0)
    if atr <= 0:
        return False
    loss_atr = (float(p['entry_price']) - price) / atr
    if loss_atr < EARLY_EXIT_LOSS_ATR:
        return False
    if EARLY_EXIT_MODE == 'log':
        if not p.get('early_exit_logged'):
            p['early_exit_logged'] = True
            print(f'EARLY EXIT (log modu, kapatılmadı) | LONG {symbol} | BTC kilidi aktif, pozisyon {loss_atr:.1f} ATR zararda', flush=True)
        return False
    print(f'EARLY EXIT | LONG {symbol} | BTC kilidi aktif, pozisyon {loss_atr:.1f} ATR zararda -> kapatılıyor', flush=True)
    exit_symbol_position(state, symbol, price, 'EARLY_EXIT_BTC', now, live_eligible=True)
    return True


def check_m15_reversal_exit(state, symbol, now):
    """Closes a watchlist crypto position when the 15m chart reverses against
    it (see M15_EXIT_MODE). Looks at 15m data only once per closed 15m candle
    (one klines request per open position per 15 minutes). Returns True if the
    position was closed."""
    if M15_EXIT_MODE == 'off':
        return False
    p = (state.get('positions') or {}).get(symbol)
    if not p or p.get('side') not in ('LONG', 'SHORT') or p.get('source') == 'restored_live':
        return False
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize('UTC')
    boundary = now_ts.floor('15min').isoformat()
    if p.get('m15_boundary') == boundary:
        return False                       # this 15m candle was already evaluated
    df = fetch(symbol, 'USD_M', 300, interval='15m')
    closed = df[df['close_time'] < now_ts].copy()
    if len(closed) < 120:
        return False
    entry_ts = pd.Timestamp(p['entry_time'])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize('UTC')
    last_seen = pd.Timestamp(p['m15_last_close']) if p.get('m15_last_close') else None
    enriched = add_indicators(closed, cfg)
    against = 'SHORT' if p['side'] == 'LONG' else 'LONG'
    count = int(p.get('m15_against') or 0)
    for i in range(max(0, len(enriched) - 8), len(enriched)):
        ct = enriched.iloc[i]['close_time']
        if ct <= entry_ts or (last_seen is not None and ct <= last_seen):
            continue                       # before the entry, or already counted
        gl, gs = long_signal(enriched, i, cfg), short_signal(enriched, i, cfg)
        sig = 'LONG' if (gl and not gs) else ('SHORT' if (gs and not gl) else None)
        if sig == against:
            count += 1
            print(f'M15 | {p["side"]} {symbol} | {ct.isoformat()[:16]} UTC kapanışında 15dk {against} sinyali ({count}/{M15_EXIT_COUNT})', flush=True)
        elif sig == p['side']:
            if count:
                print(f'M15 | {p["side"]} {symbol} | {ct.isoformat()[:16]} UTC kapanışında 15dk {sig} sinyali, sayaç sıfırlandı', flush=True)
            count = 0
            p.pop('m15_logged', None)
        p['m15_last_close'] = ct.isoformat()
    p['m15_against'] = count
    p['m15_boundary'] = boundary
    if count < M15_EXIT_COUNT:
        return False
    # Armed: enough opposite signals. Only a position that is currently at a
    # LOSS is closed; a profitable one is left to TP / trailing / profit lock
    # and is closed here as soon as it slips into loss while still armed.
    px = float(df.iloc[-1]['close'])
    entry = float(p['entry_price'])
    in_loss = px < entry if p['side'] == 'LONG' else px > entry
    if not in_loss:
        return False
    if M15_EXIT_MODE == 'log':
        if not p.get('m15_logged'):
            p['m15_logged'] = True
            print(f'M15 EXIT (log modu, kapatılmadı) | {p["side"]} {symbol} | {count} ters 15dk sinyal + zararda ({px:.6f} / giriş {entry:.6f})', flush=True)
        return False
    print(f'M15 EXIT | {p["side"]} {symbol} | {count} ters 15dk sinyal (mum kapanışı) + pozisyon zararda -> kapatılıyor @ {px:.6f} (giriş {entry:.6f})', flush=True)
    exit_symbol_position(state, symbol, px, 'EXIT_15M_SIGNAL', now, live_eligible=True)
    return True


def check_trend_stop(state, symbol, closed, now, price):
    """See TREND_SL_MODE. `closed` = closed candles of the strategy timeframe
    (4H). Evaluated once per newly closed candle. Returns True if closed."""
    if TREND_SL_MODE == 'off':
        return False
    p = (state.get('positions') or {}).get(symbol)
    if not p or p.get('side') not in ('LONG', 'SHORT') or p.get('source') == 'restored_live':
        return False
    if len(closed) < TREND_SL_EMA + 40:
        return False
    last_ct = closed.iloc[-1]['close_time']
    if p.get('trend_sl_last') == last_ct.isoformat():
        return False                       # this candle was already evaluated
    p['trend_sl_last'] = last_ct.isoformat()
    entry_ts = pd.Timestamp(p['entry_time'])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize('UTC')
    if last_ct <= entry_ts:
        return False                       # candle closed before the entry
    import scanner_st
    c = closed.reset_index(drop=True)
    up, line = scanner_st.supertrend(c, scanner_st.PERIOD, scanner_st.FACTOR)
    last = len(c) - 1
    if pd.isna(line[last]):
        return False
    j = last
    while j > 0 and up[j - 1] == up[last] and not pd.isna(line[j - 1]):
        j -= 1
    flip_ct = c.iloc[j]['close_time']      # candle on which the current Supertrend trend began
    ema = c['close'].astype(float).ewm(span=TREND_SL_EMA, adjust=False).mean().iloc[-1]
    close = float(c.iloc[-1]['close'])
    if p['side'] == 'LONG':
        hit = (not up[last]) and close < ema
    else:
        hit = bool(up[last]) and close > ema
    if not (hit and flip_ct > entry_ts):
        return False
    msg = (f'{p["side"]} {symbol} | 4s mum kapanışı {close:.6f} {"<" if p["side"]=="LONG" else ">"} EMA{TREND_SL_EMA} ({ema:.6f}) '
           f've Supertrend({scanner_st.PERIOD}-{scanner_st.FACTOR:g}) {"aşağı" if p["side"]=="LONG" else "yukarı"} kırıldı')
    if TREND_SL_MODE == 'log':
        if not p.get('trend_sl_logged'):
            p['trend_sl_logged'] = True
            print(f'TREND SL (log modu, kapatılmadı) | {msg}', flush=True)
        return False
    print(f'TREND SL | {msg} -> kapatılıyor @ {price:.6f}', flush=True)
    exit_symbol_position(state, symbol, price, 'TREND_SL', now, live_eligible=True)
    return True


def _entry_wait_eval(symbol, side, closed4, now):
    """({'15m': bool, '1h': bool, '4h': bool}, trailing count of closed 1h
    candles against the trade) from the last CLOSED candles."""
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize('UTC')

    def with_trade(row):
        o, c = float(row['open']), float(row['close'])
        return c > o if side == 'LONG' else c < o

    tf = {}
    d15 = fetch(symbol, 'USD_M', 5, interval='15m')
    d15 = d15[d15['close_time'] < now_ts]
    tf['15m'] = bool(len(d15)) and with_trade(d15.iloc[-1])
    d1h = fetch(symbol, 'USD_M', 8, interval='1h')
    d1h = d1h[d1h['close_time'] < now_ts].reset_index(drop=True)
    tf['1h'] = bool(len(d1h)) and with_trade(d1h.iloc[-1])
    tf['4h'] = bool(len(closed4)) and with_trade(closed4.iloc[-1])
    streak = 0
    for k in range(len(d1h) - 1, -1, -1):
        row = d1h.iloc[k]
        o, c = float(row['open']), float(row['close'])
        against = c < o if side == 'LONG' else c > o
        if not against:
            break
        streak += 1
    return tf, streak


def _drop_from_watchlist(state, symbol, why):
    for k in ('watchlist', 'watchlist_signals', 'entry_blocked', 'symbol_last_closed', 'entry_wait'):
        (state.get(k) or {}).pop(symbol, None)
    # auto add must not bring it straight back within the same 4H candle
    try:
        until = pd.Timestamp.now(tz='UTC').floor('4h') + pd.Timedelta(hours=4)
        state.setdefault('auto_add_block', {})[symbol] = until.isoformat()
    except Exception:
        pass
    print(f'ENTRY WAIT | {symbol} izleme listesinden çıkarıldı: {why}', flush=True)
    try:
        send_message(f'⏳ {symbol} giriş beklemesi sona erdi, izleme listesinden çıkarıldı: {why}')
    except Exception:
        pass


def process_entry_wait(state, symbol, rec, closed4, latest, df, go_long, go_short, blocked, now):
    """One evaluation of a coin waiting for entry (see ENTRY_WAIT_MODE).
    Candle data is re-read once per 15 minutes. Returns nothing; the coin ends up
    entered (position opened), dropped from the watchlist, or still waiting."""
    side = rec['side']
    wanted = None if blocked else ('LONG' if (go_long and not go_short) else ('SHORT' if (go_short and not go_long) else None))
    if wanted != side:
        return _drop_from_watchlist(state, symbol, f'4s {side} sinyali artık geçerli değil')
    since = pd.Timestamp(rec['since'])
    if since.tzinfo is None:
        since = since.tz_localize('UTC')
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize('UTC')
    if (now_ts - since).total_seconds() / 3600.0 > ENTRY_WAIT_MAX_HOURS:
        return _drop_from_watchlist(state, symbol, f'{ENTRY_WAIT_MAX_HOURS:g} saat içinde uygun giriş oluşmadı')
    boundary = now_ts.floor('15min').isoformat()
    if rec.get('boundary') == boundary:
        return
    tf, streak = _entry_wait_eval(symbol, side, closed4, now)
    rec['boundary'] = boundary
    rec['tf'] = tf
    rec['streak'] = streak
    if streak >= ENTRY_WAIT_RED_COUNT:
        return _drop_from_watchlist(state, symbol, f'üst üste {streak} adet 1s mum ters yönde kapandı')
    if not all(tf.values()):
        if not rec.get('logged_wait'):
            rec['logged_wait'] = True
            print(f'ENTRY WAIT | {side} {symbol} | giriş bekliyor: 15dk={tf["15m"]} 1s={tf["1h"]} 4s={tf["4h"]}', flush=True)
        return
    sig_close = float(latest['close']); sig_atr = float(latest['atr']); cur_px = float(df.iloc[-1]['close'])
    allowed, why = entry_guard_check(state, symbol, side, now, sig_close, sig_atr, cur_px, quiet=True)
    if not allowed:
        rec['blocked'] = why
        return
    px = exec_price(cur_px, 'BUY' if side == 'LONG' else 'SELL')
    state.get('entry_wait', {}).pop(symbol, None)
    print(f'ENTRY WAIT | {side} {symbol} | 15dk/1s/4s uyumlu, pozisyon açılıyor', flush=True)
    enter_symbol(state, symbol, side, px, latest, now, live_eligible=True)


def auto_add_from_scanner(state, now):
    """See AUTO_ADD_MODE. Called once per bot cycle; acts once per finished 4H scan."""
    if AUTO_ADD_MODE == 'off':
        return
    wl = state.setdefault('watchlist', {})
    positions = state.get('positions') or {}
    waits = state.get('entry_wait') or {}
    # 1) prune auto-added coins that have nothing to wait for any more
    if AUTO_ADD_MODE == 'on':
        try:
            holders = live_trading.get_open_live_holders()
        except Exception:
            holders = None
        if holders is not None:
            for sym, w in list(wl.items()):
                if not w.get('auto_added') or sym in positions or sym in waits or sym in holders:
                    continue
                try:
                    added = pd.Timestamp(w.get('added_at'))
                    if added.tzinfo is None:
                        added = added.tz_localize('UTC')
                    if (pd.Timestamp(now) - added).total_seconds() < 3600:
                        continue           # too fresh to judge
                except Exception:
                    continue
                known = ((state.get('watchlist_signals') or {}).get(sym) or {}).get('final')
                if known and known != 'LONG':
                    for k in ('watchlist', 'watchlist_signals', 'entry_blocked', 'symbol_last_closed', 'entry_wait'):
                        (state.get(k) or {}).pop(sym, None)
                    state.setdefault('auto_add_block', {})[sym] = (pd.Timestamp(now).floor('4h') + pd.Timedelta(hours=4)).isoformat()
                    print(f'AUTO ADD | {sym} otomatik listeden çıkarıldı (pozisyon yok, LONG sinyali sürmüyor)', flush=True)
    # 2) add LONG coins of a newly finished scan
    import scanner as _scanner
    snap = _scanner.snapshot()
    if snap.get('status') != 'READY':
        return
    scan_id = str(snap.get('last_scan_candle') or '')
    if not scan_id:
        return
    first_look = state.get('auto_add_scan') != scan_id
    if first_look:
        state['auto_add_scan'] = scan_id
        state['auto_add_count'] = 0
        try:
            ts = pd.Timestamp(snap['last_scan_candle'], unit='ms', tz='UTC') if scan_id.isdigit() else pd.Timestamp(scan_id)
            if ts.tzinfo is None:
                ts = ts.tz_localize('UTC')
            if (pd.Timestamp(now) - ts).total_seconds() / 3600.0 > AUTO_ADD_MAX_SCAN_AGE_H:
                state['auto_add_count'] = AUTO_ADD_MAX_PER_SCAN   # stale scan (e.g. after a restart): wait for the next candle
                return
        except Exception:
            pass
    remaining = AUTO_ADD_MAX_PER_SCAN - int(state.get('auto_add_count') or 0)
    if remaining <= 0 and not first_look:
        return
    # Expired cooldowns are dropped; the scan of one candle is re-read every cycle
    # (the scanner repeats it, and a first pass can be partial after Binance 429s),
    # so coins missed the first time are picked up by a later pass.
    blocked_until = state.setdefault('auto_add_block', {})
    for k, v in list(blocked_until.items()):
        try:
            if pd.Timestamp(now) > pd.Timestamp(v).tz_convert('UTC'):
                blocked_until.pop(k, None)
        except Exception:
            blocked_until.pop(k, None)
    # Only rows computed on the latest candle count: after a 429 the scanner keeps the
    # PREVIOUS result of a coin it could not refresh (an old candle's LONG).
    longs = [r for r in (snap.get('results') or [])
             if r.get('signal') == 'LONG' and (not r.get('candle_time') or str(r.get('candle_time')) == scan_id)]
    longs.sort(key=lambda r: (float(r.get('score') or 0), float(r.get('volume') or 0)), reverse=True)
    free = max(0, int(cfg.WATCHLIST_MAX_SYMBOLS) - len(wl))
    picked = [r['symbol'] for r in longs if r.get('symbol') and r['symbol'] not in wl and r['symbol'] not in positions
              and r['symbol'] not in blocked_until]
    take = picked[:max(0, min(remaining, free))]
    if first_look or take:
        print(f'AUTO ADD | tarama {scan_id}: {len(longs)} LONG, {len(picked)} yeni aday, {free} boş yer, kota {max(0, remaining)} -> {len(take)} eklenecek'
              f'{" (log modu, eklenmedi)" if AUTO_ADD_MODE == "log" else ""}: {", ".join(take)}', flush=True)
    if AUTO_ADD_MODE != 'on' or not take:
        return
    for sym in take:
        wl[sym] = {'market': 'crypto', 'added_at': now.isoformat(), 'added_signal': 'LONG', 'auto_added': True}
    state['auto_add_count'] = int(state.get('auto_add_count') or 0) + len(take)
    try:
        send_message('🤖 4s taramadan otomatik eklenen LONG coinler (giriş bekleme kilidiyle): ' + ', '.join(take))
    except Exception:
        pass


def live_guard_ctx(state, signal_row, price):
    """Dynamic inputs the PER-MEMBER live guards need (BTC lock state, the
    signal bar's close and ATR for the late-entry filter). Thresholds/modes are
    read from the same environment variables inside live_trading.py."""
    try:
        lock = state.get('btc_lock') or {}
        return {
            'btc_lock_active': bool(lock.get('active')), 'btc_lock_reason': lock.get('reason'),
            'signal_close': float(signal_row['close']), 'atr': float(signal_row['atr']), 'price': float(price),
        }
    except Exception:
        return None


def enter(state, position, price, signal_row, now):
    side = position
    atr = float(signal_row['atr'])
    if side == 'LONG':
        symbol = cfg.LONG_SYMBOL
        sl = price - cfg.ATR_SL_MULTIPLIER * atr
        tp = price + cfg.ATR_LONG_TP_MULTIPLIER * atr
    else:
        symbol = cfg.SHORT_SYMBOL
        sl = price + cfg.ATR_SHORT_SL_MULTIPLIER * atr
        tp = price - cfg.ATR_SHORT_TP_MULTIPLIER * atr
    state['position'] = {
        'side': side, 'symbol': symbol, 'qty_eth': cfg.POSITION_QTY_ETH,
        'entry_price': price, 'entry_time': now.isoformat(),
        'signal_time': signal_row['close_time'].isoformat(), 'atr': atr,
        'sl': sl, 'tp': tp, 'trail_active': False, 'trail_stop': None,
        'highest_high': price, 'lowest_low': price,
        'entry_fee': fee(price * cfg.POSITION_QTY_ETH),
    }
    state['equity'] -= state['position']['entry_fee']
    save_state(state)
    send_message(entry_message(state['position']))
    print(f"PAPER ENTRY | {side} {symbol} | price={price:.4f} | ATR={atr:.4f} | SL={sl:.4f} | TP={tp:.4f}", flush=True)
    # PHASE 2 — mirror this exact signal as a real order for every user who
    # has live trading turned on for their own Binance account. Always uses
    # the single USDT-margined main symbol (cfg.SIGNAL_SYMBOL) for live,
    # regardless of which contract (COIN-M/USD-M) the paper engine itself
    # used for this side — see binance_live.py's module docstring for why.
    # Wrapped so a live-trading failure can never break the paper engine.
    try:
        live_trading.on_entry_signal(cfg.SIGNAL_SYMBOL, side, price, source='eth_bot', guard_ctx=live_guard_ctx(state, signal_row, price))
    except Exception as e:
        print(f'LIVE | ERROR | {type(e).__name__}: {e}', flush=True)


def exit_position(state, raw_price, reason, event_time):
    p = state['position']
    side = p['side']
    price = close_price(raw_price, side)
    qty = p['qty_eth']
    gross = (price - p['entry_price']) * qty if side == 'LONG' else (p['entry_price'] - price) * qty
    exit_fee = fee(price * qty)
    net = gross - exit_fee - p['entry_fee']
    state['equity'] += gross - exit_fee
    trade = {
        'signal_time': p['signal_time'], 'entry_time': p['entry_time'], 'exit_time': event_time.isoformat(),
        'side': side, 'symbol': p['symbol'], 'qty_eth': qty, 'entry_price': p['entry_price'],
        'exit_price': price, 'atr': p['atr'], 'sl': p['sl'], 'tp': p['tp'],
        'trail_active': p['trail_active'], 'trail_stop': p['trail_stop'],
        'gross_pnl': gross, 'fees': p['entry_fee'] + exit_fee, 'net_pnl': net, 'reason': reason,
        'equity_after': state['equity']
    }
    log_trade(trade)
    print(f"PAPER EXIT  | {side} {p['symbol']} | reason={reason} | price={price:.4f} | net={net:.2f} | equity={state['equity']:.2f}", flush=True)
    send_message(exit_message(trade))
    state['position'] = None
    save_state(state)
    try:
        live_trading.on_exit_signal(cfg.SIGNAL_SYMBOL, side, price, reason, source='eth_bot')
    except Exception as e:
        print(f'LIVE | ERROR | {type(e).__name__}: {e}', flush=True)


def process_intrabar(state, long_candle, short_candle, now):
    p = state['position']
    if not p:
        return False
    candle = long_candle if p['side'] == 'LONG' else short_candle
    high, low = float(candle['high']), float(candle['low'])
    entry, atr = p['entry_price'], p['atr']

    # Existing stops/TP have priority. If both hit in the same candle, SL first.
    if p['side'] == 'LONG':
        stop = p['trail_stop'] if p['trail_active'] and p['trail_stop'] is not None else p['sl']
        if low <= stop:
            exit_position(state, stop, 'ATR_TRAILING_SL' if p['trail_active'] else 'ATR_SL', now)
            return True
        if cfg.USE_ATR_TP and high >= p['tp']:
            exit_position(state, p['tp'], 'ATR_TP', now)
            return True
        if cfg.USE_ATR_TRAILING:
            if high >= entry + cfg.ATR_TRAIL_ACTIVATION * atr:
                if not p['trail_active']:
                    p['trail_active'] = True
                    p['highest_high'] = high
                    p['trail_stop'] = high - cfg.ATR_TRAIL_MULTIPLIER * atr
                else:
                    p['highest_high'] = max(p['highest_high'], high)
                    p['trail_stop'] = max(p['trail_stop'], p['highest_high'] - cfg.ATR_TRAIL_MULTIPLIER * atr)
    else:
        stop = p['trail_stop'] if p['trail_active'] and p['trail_stop'] is not None else p['sl']
        if high >= stop:
            exit_position(state, stop, 'ATR_TRAILING_SL' if p['trail_active'] else 'ATR_SL', now)
            return True
        if cfg.USE_ATR_TP and low <= p['tp']:
            exit_position(state, p['tp'], 'ATR_TP', now)
            return True
        if cfg.USE_ATR_TRAILING:
            if low <= entry - cfg.ATR_TRAIL_ACTIVATION * atr:
                if not p['trail_active']:
                    p['trail_active'] = True
                    p['lowest_low'] = low
                    p['trail_stop'] = low + cfg.ATR_TRAIL_MULTIPLIER * atr
                else:
                    p['lowest_low'] = min(p['lowest_low'], low)
                    p['trail_stop'] = min(p['trail_stop'], p['lowest_low'] + cfg.ATR_TRAIL_MULTIPLIER * atr)
    save_state(state)
    return False


def enter_symbol(state, symbol, side, price, signal_row, now, live_eligible=False):
    """Same entry logic as enter(), generalized to any watchlist symbol and
    sized in USD notional (watchlist_position_usd(), admin-editable — falls
    back to cfg.WATCHLIST_POSITION_USD) instead of a fixed coin quantity,
    since watchlist symbols can have wildly different prices.

    live_eligible=True only for Binance-tradable crypto watchlist symbols
    (see run_watchlist_symbol); US-stock/BIST watchlist symbols are never
    live-tradable through this bot and must never reach live_trading.py."""
    atr_val = float(signal_row['atr'])
    qty = (watchlist_position_usd() / price) if price else 0.0
    if side == 'LONG':
        sl = price - _sl_distance(cfg.ATR_SL_MULTIPLIER, atr_val, qty)
        tp = price + cfg.ATR_LONG_TP_MULTIPLIER * atr_val
    else:
        sl = price + _sl_distance(cfg.ATR_SHORT_SL_MULTIPLIER, atr_val, qty)
        tp = price - cfg.ATR_SHORT_TP_MULTIPLIER * atr_val
    pos = {
        'side': side, 'symbol': symbol, 'qty_eth': qty,
        'entry_price': price, 'entry_time': now.isoformat(),
        'signal_time': signal_row['close_time'].isoformat(), 'atr': atr_val,
        'sl': sl, 'tp': tp, 'trail_active': False, 'trail_stop': None,
        'highest_high': price, 'lowest_low': price,
        'entry_fee': fee(price * qty), 'source': 'watchlist',
    }
    state.setdefault('positions', {})[symbol] = pos
    state['equity'] -= pos['entry_fee']
    save_state(state)
    send_message(entry_message(pos))
    print(f"WATCHLIST ENTRY | {side} {symbol} | price={price:.6f} | ATR={atr_val:.6f} | SL={sl:.6f} | TP={tp:.6f}", flush=True)
    if live_eligible:
        try:
            live_trading.on_entry_signal(symbol, side, price, source='watchlist', guard_ctx=live_guard_ctx(state, signal_row, price))
        except Exception as e:
            print(f'LIVE | ERROR | {type(e).__name__}: {e}', flush=True)


def exit_symbol_position(state, symbol, raw_price, reason, event_time, live_eligible=False):
    p = state['positions'][symbol]
    side = p['side']
    price = close_price(raw_price, side)
    qty = p['qty_eth']
    gross = (price - p['entry_price']) * qty if side == 'LONG' else (p['entry_price'] - price) * qty
    exit_fee = fee(price * qty)
    net = gross - exit_fee - p['entry_fee']
    state['equity'] += gross - exit_fee
    trade = {
        'signal_time': p['signal_time'], 'entry_time': p['entry_time'], 'exit_time': event_time.isoformat(),
        'side': side, 'symbol': symbol, 'qty_eth': qty, 'entry_price': p['entry_price'],
        'exit_price': price, 'atr': p['atr'], 'sl': p['sl'], 'tp': p['tp'],
        'trail_active': p['trail_active'], 'trail_stop': p['trail_stop'],
        'gross_pnl': gross, 'fees': p['entry_fee'] + exit_fee, 'net_pnl': net, 'reason': reason,
        'equity_after': state['equity'],
    }
    log_trade(trade)
    print(f"WATCHLIST EXIT  | {side} {symbol} | reason={reason} | price={price:.6f} | net={net:.2f} | equity={state['equity']:.2f}", flush=True)
    send_message(exit_message(trade))
    state['positions'].pop(symbol, None)
    # Loss brake bookkeeping: a crypto watchlist position that closed at a
    # LOSS via a stop counts toward the "N stops in a short window" trigger.
    if live_eligible and net < 0 and reason in ('ATR_SL', 'ATR_TRAILING_SL'):
        try:
            record_stop_and_maybe_brake(state, event_time)
        except Exception as e:
            print(f'LOSS BRAKE | ERROR | {type(e).__name__}: {e}', flush=True)
    save_state(state)
    if live_eligible:
        try:
            live_trading.on_exit_signal(symbol, side, price, reason, source='watchlist')
        except Exception as e:
            print(f'LIVE | ERROR | {type(e).__name__}: {e}', flush=True)
    if live_eligible and STOP_REMOVE_MODE != 'off' and net < 0 and reason in STOP_REMOVE_REASONS:
        try:
            if live_trading.get_open_live_holders().get(symbol):
                print(f'STOP REMOVE | {symbol} listede bırakıldı: bir üyenin canlı pozisyonu hâlâ açık', flush=True)
            elif STOP_REMOVE_MODE == 'log':
                print(f'STOP REMOVE (log modu, silinmedi) | {symbol} zararla {reason} ile kapandı', flush=True)
            else:
                for k in ('watchlist', 'watchlist_signals', 'entry_blocked', 'symbol_last_closed', 'entry_wait'):
                    (state.get(k) or {}).pop(symbol, None)
                state.setdefault('auto_add_block', {})[symbol] = (pd.Timestamp(event_time) + pd.Timedelta(hours=STOP_REMOVE_COOLDOWN_H)).isoformat()
                save_state(state)
                print(f'STOP REMOVE | {symbol} zararla {reason} ile kapandı, izleme listesinden silindi', flush=True)
        except Exception as e:
            print(f'STOP REMOVE | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)


def _stop_reason(p):
    if p['trail_active']:
        return 'ATR_TRAILING_SL'
    return 'BREAKEVEN_SL' if p.get('be_active') else 'ATR_SL'


def _profit_lock_step(state, symbol, p, last, now, live_eligible):
    """Tiered profit lock. Uses the CURRENT price (not candle high/low, whose
    order inside the candle is unknown). Returns True if it closed."""
    if PROFIT_LOCK_MODE != 'on' or p.get('source') == 'restored_live':
        return False
    qty = p.get('qty_eth') or 0.0
    if qty <= 0:
        return False
    entry = p['entry_price']
    pnl = (last - entry) * qty if p['side'] == 'LONG' else (entry - last) * qty
    peak = max(float(p.get('pl_peak') or 0.0), pnl)
    p['pl_peak'] = peak
    # Higher tiers: lock = highest tier already reached (single touch).
    tier_lock = None
    for arm, lock in PROFIT_LOCK_TIERS:
        if peak >= arm:
            tier_lock = lock
    if tier_lock is not None:
        if pnl <= tier_lock:
            p['pl_stage'] = 4
            print(f"PROFIT LOCK | {symbol} | tepe ${peak:.1f}, ${tier_lock:.0f} kilidine düştü (${pnl:.1f}) -> kapat", flush=True)
            exit_symbol_position(state, symbol, last, 'PROFIT_LOCK', now, live_eligible=live_eligible)
            return True
        return False   # never True here: True means "position was closed"
    # Tier 1: double touch.
    stage = int(p.get('pl_stage') or 0)
    if stage == 0 and pnl >= PROFIT_LOCK_ARM_USD:
        stage = 1
        print(f"PROFIT LOCK | {symbol} | 1. dokunuş (${pnl:.1f})", flush=True)
    elif stage == 1 and pnl < PROFIT_LOCK_USD:
        stage = 2
        print(f"PROFIT LOCK | {symbol} | ${PROFIT_LOCK_USD:.0f} altına düştü (${pnl:.1f}), normal SL beklenir", flush=True)
    elif stage == 2 and pnl >= PROFIT_LOCK_ARM_USD:
        stage = 3
        print(f"PROFIT LOCK | {symbol} | 2. dokunuş (${pnl:.1f}), kilit aktif", flush=True)
    elif stage == 3 and pnl <= PROFIT_LOCK_USD:
        p['pl_stage'] = 4
        exit_symbol_position(state, symbol, last, 'PROFIT_LOCK', now, live_eligible=live_eligible)
        return True
    if stage != int(p.get('pl_stage') or 0):
        p['pl_stage'] = stage
    return False


def process_intrabar_symbol(state, symbol, candle, now, live_eligible=False):
    p = state.get('positions', {}).get(symbol)
    if not p:
        return False
    high, low = float(candle['high']), float(candle['low'])
    last = float(candle['close'])
    entry, atr_val = p['entry_price'], p['atr']
    # The forming candle's high/low may have printed BEFORE this position was
    # opened (e.g. a coin added mid-candle after a pump: the candle low is far
    # below the entry). Those extremes are not part of the trade and used to
    # trigger phantom stop-outs seconds after entry. While the candle started
    # before the entry, only prices actually seen since entry count.
    try:
        _opened = pd.Timestamp(p['entry_time'])
        if _opened.tzinfo is None:
            _opened = _opened.tz_localize('UTC')
        if pd.Timestamp(candle['open_time']) < _opened:
            p['seen_high'] = max(float(p.get('seen_high') or last), last)
            p['seen_low'] = min(float(p.get('seen_low') or last), last)
            high, low = p['seen_high'], p['seen_low']
    except Exception:
        pass
    # Safety net: a take-profit must sit on the PROFIT side of the entry. A TP on
    # the losing side would "take profit" at a loss (labelled ATR_TP), so repair it.
    if p['side'] == 'LONG' and p.get('tp') is not None and p['tp'] <= entry:
        p['tp'] = entry + cfg.ATR_LONG_TP_MULTIPLIER * atr_val
        print(f"TP REPAIR | LONG {symbol} | TP girişin altındaydı, yeniden hesaplandı -> {p['tp']:.6f}", flush=True)
    elif p['side'] == 'SHORT' and p.get('tp') is not None and p['tp'] >= entry:
        p['tp'] = entry - cfg.ATR_SHORT_TP_MULTIPLIER * atr_val
        print(f"TP REPAIR | SHORT {symbol} | TP girişin üstündeydi, yeniden hesaplandı -> {p['tp']:.6f}", flush=True)
    # Once the break-even stop is armed it is checked against the CURRENT
    # price, not the forming candle's low: that low may have printed before
    # the move that armed the stop, and a phantom exit would close a winner.
    be_on = bool(p.get('be_active')) and not p['trail_active']
    if p['side'] == 'LONG':
        stop = p['trail_stop'] if p['trail_active'] and p['trail_stop'] is not None else p['sl']
        if (last if be_on else low) <= stop:
            exit_symbol_position(state, symbol, stop, _stop_reason(p), now, live_eligible=live_eligible)
            return True
        if cfg.USE_ATR_TP and high >= p['tp']:
            exit_symbol_position(state, symbol, p['tp'], 'ATR_TP', now, live_eligible=live_eligible)
            return True
        if BREAKEVEN_MODE == 'on' and not p['trail_active'] and not p.get('be_active') and high >= entry + BREAKEVEN_ATR * atr_val:
            p['be_active'] = True
            p['sl'] = max(p['sl'], entry + BREAKEVEN_BUFFER_ATR * atr_val)
            print(f"BREAKEVEN | LONG {symbol} | stop girişe çekildi -> {p['sl']:.6f}", flush=True)
        if cfg.USE_ATR_TRAILING and high >= entry + cfg.ATR_TRAIL_ACTIVATION * atr_val:
            if not p['trail_active']:
                p['trail_active'] = True
                p['highest_high'] = high
                p['trail_stop'] = high - cfg.ATR_TRAIL_MULTIPLIER * atr_val
            else:
                p['highest_high'] = max(p['highest_high'], high)
                p['trail_stop'] = max(p['trail_stop'], p['highest_high'] - cfg.ATR_TRAIL_MULTIPLIER * atr_val)
    else:
        stop = p['trail_stop'] if p['trail_active'] and p['trail_stop'] is not None else p['sl']
        if (last if be_on else high) >= stop:
            exit_symbol_position(state, symbol, stop, _stop_reason(p), now, live_eligible=live_eligible)
            return True
        if cfg.USE_ATR_TP and low <= p['tp']:
            exit_symbol_position(state, symbol, p['tp'], 'ATR_TP', now, live_eligible=live_eligible)
            return True
        if BREAKEVEN_MODE == 'on' and not p['trail_active'] and not p.get('be_active') and low <= entry - BREAKEVEN_ATR * atr_val:
            p['be_active'] = True
            p['sl'] = min(p['sl'], entry - BREAKEVEN_BUFFER_ATR * atr_val)
            print(f"BREAKEVEN | SHORT {symbol} | stop girişe çekildi -> {p['sl']:.6f}", flush=True)
        if cfg.USE_ATR_TRAILING and low <= entry - cfg.ATR_TRAIL_ACTIVATION * atr_val:
            if not p['trail_active']:
                p['trail_active'] = True
                p['lowest_low'] = low
                p['trail_stop'] = low + cfg.ATR_TRAIL_MULTIPLIER * atr_val
            else:
                p['lowest_low'] = min(p['lowest_low'], low)
                p['trail_stop'] = min(p['trail_stop'], p['lowest_low'] + cfg.ATR_TRAIL_MULTIPLIER * atr_val)
    if _profit_lock_step(state, symbol, p, last, now, live_eligible):
        return True
    save_state(state)
    return False


def restore_orphaned_live_positions(state, now):
    """A member's REAL Binance position is only ever managed (stop / take
    profit / trailing / exit signal) through the demo position of the same
    coin. If that demo position is gone (coin removed from the watchlist,
    state reset...), or lost its SL/TP, the real position would sit on
    Binance unprotected. Re-create the demo position so protection resumes:
    side and entry come from the real position, ATR / SL / TP are recomputed
    with the normal strategy multipliers. If price already ran through the
    entry-based stop, the stop is re-based on the current price instead of
    closing a position that was left unprotected through no fault of its own.
    Entry fee is 0 and equity untouched: this is bookkeeping, not a new trade."""
    try:
        holders_by_symbol = live_trading.get_open_live_holders()
    except Exception as e:
        print(f'LIVE RESTORE | ERROR | {type(e).__name__}: {e}', flush=True)
        return
    for symbol, holders in holders_by_symbol.items():
        if symbol == cfg.SIGNAL_SYMBOL:
            continue  # the main ETH bot manages its own position
        existing = state.get('positions', {}).get(symbol)
        if existing and existing.get('sl') is not None and existing.get('tp') is not None:
            continue  # protected already
        sides = [h['side'] for h in holders if h.get('side') in ('LONG', 'SHORT')]
        if not sides:
            continue
        side = max(set(sides), key=sides.count)
        same = [h for h in holders if h.get('side') == side and h.get('entry_price', 0) > 0]
        if not same:
            continue
        if existing and existing.get('side') != side:
            continue  # conflicting demo position: do not touch it
        entry = sum(h['entry_price'] for h in same) / len(same)
        entry_time = min((h.get('entry_time') or now.isoformat()) for h in same)
        try:
            df = fetch_cached(symbol, 'USD_M', cfg.INTERVAL, 300)
            enriched = add_indicators(df.iloc[:-1].copy(), cfg)
            atr_val = float(enriched.iloc[-1]['atr'])
            sig_time = enriched.iloc[-1]['close_time'].isoformat()
            cur = float(df.iloc[-1]['close'])
        except Exception as e:
            if type(e).__name__ == 'BinanceCooldown':
                print('LIVE RESTORE | Binance bekleme süresi aktif, geri yükleme sonraki tura bırakıldı', flush=True)
                return
            print(f'LIVE RESTORE | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
            continue
        if not atr_val or atr_val != atr_val:
            continue
        r_qty = (existing or {}).get('qty_eth') or ((watchlist_position_usd() / entry) if entry else 0.0)
        if side == 'LONG':
            d = _sl_distance(cfg.ATR_SL_MULTIPLIER, atr_val, r_qty)
            sl = entry - d
            tp = entry + cfg.ATR_LONG_TP_MULTIPLIER * atr_val
            if cur <= sl:
                sl = cur - d   # only the STOP is re-based; TP stays entry-based (never on the losing side)
        else:
            d = _sl_distance(cfg.ATR_SHORT_SL_MULTIPLIER, atr_val, r_qty)
            sl = entry + d
            tp = entry - cfg.ATR_SHORT_TP_MULTIPLIER * atr_val
            if cur >= sl:
                sl = cur + d
        pos = dict(existing or {})
        pos.update({
            'side': side, 'symbol': symbol,
            'qty_eth': pos.get('qty_eth') or ((watchlist_position_usd() / entry) if entry else 0.0),
            'entry_price': pos.get('entry_price') or entry,
            'entry_time': pos.get('entry_time') or entry_time,
            'signal_time': pos.get('signal_time') or sig_time,
            'atr': atr_val, 'sl': sl, 'tp': tp,
            'trail_active': bool(pos.get('trail_active')) and pos.get('trail_stop') is not None,
            'trail_stop': pos.get('trail_stop') if pos.get('trail_active') else None,
            'highest_high': max(float(pos.get('highest_high') or entry), cur, entry),
            'lowest_low': min(float(pos.get('lowest_low') or entry), cur, entry),
            'entry_fee': pos.get('entry_fee') or 0.0,
            'source': pos.get('source') or 'restored_live',
        })
        state.setdefault('positions', {})[symbol] = pos
        wl = state.setdefault('watchlist', {})
        if symbol not in wl:
            wl[symbol] = {'market': 'crypto', 'added_at': now.isoformat(), 'added_signal': side, 'restored_for_live': True}
        save_state(state)
        print(f'LIVE RESTORE | {side} {symbol} | entry={entry:.6f} | ATR={atr_val:.6f} | SL={sl:.6f} | TP={tp:.6f} | '
              f'{len(same)} üyenin canlı pozisyonu için SL/TP yeniden kuruldu', flush=True)


def run_watchlist_symbol(state, symbol, now):
    """Fetch data, refresh the signal snapshot, and manage a paper position for
    one manually-added crypto watchlist symbol using the exact same strategy
    and indicators as the main ETH engine (USD-M Binance Futures klines)."""
    df = fetch_cached(symbol, 'USD_M', cfg.INTERVAL, 300)
    daily_df = fetch_cached(symbol, 'USD_M', '1d', 400)
    daily_closed = daily_df.iloc[:-1].copy()
    volatile_now, atrp_pct, _ = is_volatile_daily(
        daily_closed, cfg.VOLATILITY_ATR_LENGTH, cfg.VOLATILITY_PERCENTILE_LENGTH,
        cfg.VOLATILITY_LOW_PERCENTILE, cfg.VOLATILITY_HIGH_PERCENTILE,
    )
    closed = df.iloc[:-1].copy()
    enriched = add_indicators(closed, cfg)
    latest = enriched.iloc[-1]
    latest_closed_time = latest['close_time']
    i = len(enriched) - 1
    go_long = long_signal(enriched, i, cfg)
    go_short = short_signal(enriched, i, cfg)
    blocked = cfg.USE_VOLATILE_FILTER and volatile_now

    def _fmt(v):
        try:
            return round(float(v), 4)
        except Exception:
            return None

    final_signal = 'VOLATILE_BLOCK' if blocked else ('LONG' if go_long else ('SHORT' if go_short else 'NONE'))
    state.setdefault('watchlist_signals', {})[symbol] = {
        'price': float(df.iloc[-1]['close']),
        'final': final_signal,
        'atrp_percentile_1d': atrp_pct,
        'updated_at': now.isoformat(),
        # Same shape as the main ETH engine's state['signals'], so the dashboard
        # can show an identical detail card for any watchlist crypto symbol.
        'indicators': {
            'ema': 'BULLISH' if float(latest.get('ema_fast', 0)) > float(latest.get('ema_slow', 0)) else 'BEARISH',
            'supertrend': 'BULLISH' if bool(latest.get('supertrend_direction', False)) else 'BEARISH',
            'adx': _fmt(latest.get('adx')), 'rsi': _fmt(latest.get('rsi')),
            'cci': _fmt(latest.get('cci')), 'stoch': 'K/D loaded',
            'macd': 'BULLISH' if float(latest.get('macd', 0)) > float(latest.get('macd_signal', 0)) and float(latest.get('macd_hist', 0)) > 0 else 'BEARISH',
            'volatility': 'BLOCKED' if blocked else 'OK',
            'atrp_percentile_1d': _fmt(atrp_pct),
            'final': final_signal,
        },
    }

    # Manage an existing paper position first, on the live (still-forming) candle.
    if symbol in state.get('positions', {}):
        process_intrabar_symbol(state, symbol, df.iloc[-1], now, live_eligible=True)
    if symbol in state.get('positions', {}):
        try:
            check_early_exit(state, symbol, float(df.iloc[-1]['close']), now)
        except Exception as e:
            print(f'EARLY EXIT | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
    if symbol in state.get('positions', {}):
        try:
            check_m15_reversal_exit(state, symbol, now)
        except Exception as e:
            print(f'M15 EXIT | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
    if symbol in state.get('positions', {}):
        try:
            check_trend_stop(state, symbol, closed, now, float(df.iloc[-1]['close']))
        except Exception as e:
            print(f'TREND SL | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
    if symbol not in state.get('watchlist', {}):
        save_state(state)
        return                              # removed after a stop-out: nothing more to do for it

    waits = state.setdefault('entry_wait', {})
    if symbol in state.get('positions', {}):
        waits.pop(symbol, None)
    elif symbol in waits:
        try:
            process_entry_wait(state, symbol, waits[symbol], closed, latest, df, go_long, go_short, blocked, now)
        except Exception as e:
            if type(e).__name__ == 'BinanceCooldown':
                raise
            print(f'ENTRY WAIT | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
        save_state(state)
        return

    last_map = state.setdefault('symbol_last_closed', {})
    last_ts = pd.Timestamp(last_map.get(symbol)) if last_map.get(symbol) else None
    is_new_candle = last_ts is None or latest_closed_time > last_ts
    blocked_map = state.setdefault('entry_blocked', {})
    pend = blocked_map.get(symbol)
    # A signal that was refused by an entry guard is re-tried every cycle for
    # the rest of that candle (guards can lift: BTC lock released, a slot frees
    # up, price comes back), instead of waiting for the next 4H close.
    is_retry = (not is_new_candle) and bool(pend) and pend.get('candle') == latest_closed_time.isoformat()
    if is_new_candle or is_retry:
        last_map[symbol] = latest_closed_time.isoformat()
        if symbol in state.get('positions', {}):
            blocked_map.pop(symbol, None)
        else:
            if blocked:
                go_long = go_short = False
            sig_close = float(latest['close']); sig_atr = float(latest['atr']); cur_px = float(df.iloc[-1]['close'])
            side_wanted = 'LONG' if (go_long and not go_short) else ('SHORT' if (go_short and not go_long) else None)
            if side_wanted is None:
                blocked_map.pop(symbol, None)
            elif ENTRY_WAIT_MODE != 'off' and last_ts is None and is_new_candle:
                # Just added from the scanner: the signal may be hours old.
                if ENTRY_WAIT_MODE == 'on':
                    waits[symbol] = {'side': side_wanted, 'since': now.isoformat(), 'candle': latest_closed_time.isoformat()}
                    print(f'ENTRY WAIT | {side_wanted} {symbol} | eklendi, 15dk/1s/4s mumları uyumlu olunca pozisyon açılacak', flush=True)
                    try:
                        process_entry_wait(state, symbol, waits[symbol], closed, latest, df, go_long, go_short, blocked, now)
                    except Exception as e:
                        if type(e).__name__ == 'BinanceCooldown':
                            raise
                        print(f'ENTRY WAIT | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
                    save_state(state)
                    return
                try:
                    tf, streak = _entry_wait_eval(symbol, side_wanted, closed, now)
                    print(f'ENTRY WAIT (log modu, engellenmedi) | {side_wanted} {symbol} | 15dk={tf["15m"]} 1s={tf["1h"]} 4s={tf["4h"]} ters 1s mum={streak}', flush=True)
                except Exception as e:
                    print(f'ENTRY WAIT | ERROR | {symbol} | {type(e).__name__}: {e}', flush=True)
                allowed, why = entry_guard_check(state, symbol, side_wanted, now, sig_close, sig_atr, cur_px, quiet=is_retry)
                if allowed:
                    px = exec_price(cur_px, 'BUY' if side_wanted == 'LONG' else 'SELL')
                    blocked_map.pop(symbol, None)
                    enter_symbol(state, symbol, side_wanted, px, latest, now, live_eligible=True)
            else:
                allowed, why = entry_guard_check(state, symbol, side_wanted, now, sig_close, sig_atr, cur_px, quiet=is_retry)
                if allowed:
                    # Fill at the CURRENT price (the moment of entry), not the forming 4H
                    # candle's open, which can be hours old when a coin is added mid-candle.
                    px = exec_price(cur_px, 'BUY' if side_wanted == 'LONG' else 'SELL')
                    blocked_map.pop(symbol, None)
                    enter_symbol(state, symbol, side_wanted, px, latest, now, live_eligible=True)
                else:
                    rec = {'candle': latest_closed_time.isoformat(), 'side': side_wanted,
                           'codes': list(LAST_BLOCK_CODES), 'reason': why, 'at': now.isoformat()}
                    if is_new_candle:
                        print(f"ENTRY RETRY | {symbol} | {side_wanted} engelli, aynı mum içinde her turda yeniden denenecek", flush=True)
                    blocked_map[symbol] = rec
    save_state(state)


def run_us_stock_watchlist_symbol(state, symbol, now):
    """Manage a paper position for one manually-added US stock (S&P 500 /
    Nasdaq-100) watchlist symbol, using the exact same strategy/indicators as
    the main ETH engine, sourced from TradingView (NASDAQ/NYSE/AMEX
    auto-detected per symbol) instead of Binance klines.

    US equities trade in sessions, not 24/7, so a 4H bar only counts once it
    has actually closed in America/New_York time — same closed-candle
    handling us_scanner.py uses for the standalone scanner tab.

    Execution-model note (paper only): unlike the crypto engine, which fills
    on the *next* candle's open (always minutes away on a 24/7 market), this
    fills at the signal bar's own close. Waiting for "the next bar's open" on
    an equity can mean waiting through a weekend/holiday gap, so filling at
    the just-closed bar's close is the simpler, still-conservative choice for
    a simulation. This is disclosed to the user, not hidden."""
    from us_scanner import _resolve_tv_bars

    raw_df, _exchange = _resolve_tv_bars(symbol, "240", 300)
    df = add_close_time(raw_df, hours=4)
    now_ny = pd.Timestamp.now(tz='America/New_York')
    closed = df[df['close_time'].dt.tz_convert('America/New_York') <= now_ny].copy()
    if len(closed) < 60:
        return  # not enough closed history yet to compute indicators safely

    daily_raw, _ = _resolve_tv_bars(symbol, "1D", 400)
    daily_local_date = daily_raw['timestamp'].dt.tz_convert('America/New_York').dt.date
    daily_closed = daily_raw.loc[daily_local_date < now_ny.date()].copy()
    volatile_now, atrp_pct, _ = is_volatile_daily(
        daily_closed, cfg.VOLATILITY_ATR_LENGTH, cfg.VOLATILITY_PERCENTILE_LENGTH,
        cfg.VOLATILITY_LOW_PERCENTILE, cfg.VOLATILITY_HIGH_PERCENTILE,
    )

    enriched = add_indicators(closed, cfg)
    latest = enriched.iloc[-1]
    latest_closed_time = latest['close_time']
    i = len(enriched) - 1
    go_long = long_signal(enriched, i, cfg)
    go_short = short_signal(enriched, i, cfg)
    blocked = cfg.USE_VOLATILE_FILTER and volatile_now

    def _fmt(v):
        try:
            return round(float(v), 4)
        except Exception:
            return None

    final_signal = 'VOLATILE_BLOCK' if blocked else ('LONG' if go_long else ('SHORT' if go_short else 'NONE'))
    state.setdefault('watchlist_signals', {})[symbol] = {
        'price': float(closed.iloc[-1]['close']),
        'final': final_signal,
        'atrp_percentile_1d': atrp_pct,
        'updated_at': now.isoformat(),
        'indicators': {
            'ema': 'BULLISH' if float(latest.get('ema_fast', 0)) > float(latest.get('ema_slow', 0)) else 'BEARISH',
            'supertrend': 'BULLISH' if bool(latest.get('supertrend_direction', False)) else 'BEARISH',
            'adx': _fmt(latest.get('adx')), 'rsi': _fmt(latest.get('rsi')),
            'cci': _fmt(latest.get('cci')), 'stoch': 'K/D loaded',
            'macd': 'BULLISH' if float(latest.get('macd', 0)) > float(latest.get('macd_signal', 0)) and float(latest.get('macd_hist', 0)) > 0 else 'BEARISH',
            'volatility': 'BLOCKED' if blocked else 'OK',
            'atrp_percentile_1d': _fmt(atrp_pct),
            'final': final_signal,
        },
    }

    # Manage an existing paper position first, using the most recent known price.
    if symbol in state.get('positions', {}):
        process_intrabar_symbol(state, symbol, closed.iloc[-1], now)

    last_map = state.setdefault('symbol_last_closed', {})
    last_ts = pd.Timestamp(last_map.get(symbol)) if last_map.get(symbol) else None
    if last_ts is None or latest_closed_time > last_ts:
        last_map[symbol] = latest_closed_time.isoformat()
        if symbol not in state.get('positions', {}):
            if blocked:
                go_long = go_short = False
            if go_long and not go_short:
                px = exec_price(float(closed.iloc[-1]['close']), 'BUY')
                enter_symbol(state, symbol, 'LONG', px, latest, now)
            elif go_short and not go_long:
                px = exec_price(float(closed.iloc[-1]['close']), 'SELL')
                enter_symbol(state, symbol, 'SHORT', px, latest, now)
    save_state(state)


def sync_bist_watchlist_signals(state, now):
    """BIST symbols are observation-only (no paper trading, per bist_scanner's
    design) — just mirror the latest cached scan result for any watched symbol
    so the dashboard can show a live signal without extra TradingView calls."""
    try:
        from bist_scanner import snapshot as bist_snapshot
        results = {r.get('symbol'): r for r in bist_snapshot().get('results', [])}
    except Exception:
        return
    changed = False
    for symbol, w in state.get('watchlist', {}).items():
        if w.get('market') != 'bist':
            continue
        r = results.get(symbol)
        if not r:
            continue
        state.setdefault('watchlist_signals', {})[symbol] = {
            'price': r.get('price'), 'final': r.get('signal'),
            'atrp_percentile_1d': r.get('atrp_percentile_1d'), 'updated_at': now.isoformat(),
            # BIST scanner doesn't compute EMA/volatility-veto fields, so those two
            # keys are simply absent here; the dashboard shows '—' for them.
            'indicators': {
                'supertrend': 'BULLISH' if r.get('st') == 'BULL' else ('BEARISH' if r.get('st') == 'BEAR' else None),
                'adx': r.get('adx'), 'rsi': r.get('rsi'), 'cci': r.get('cci'),
                'stoch': f"{r.get('stoch_k','—')} / {r.get('stoch_d','—')}",
                'macd': 'BULLISH' if r.get('macd') == 'BULL' else ('BEARISH' if r.get('macd') == 'BEAR' else None),
                'atrp_percentile_1d': r.get('atrp_percentile_1d'),
                'final': r.get('signal'),
            },
        }
        changed = True
    if changed:
        save_state(state)


def main():
    print('TEST32 PAPER TRADING | REAL MARKET DATA | NO REAL ORDERS', flush=True)
    print(f'ENTRY GUARDS | BTC_LOCK={BTC_LOCK_MODE} (1h>={BTC_LOCK_1H_DROP_PCT}% / 4h>={BTC_LOCK_4H_DROP_PCT}% drop, release>-{BTC_LOCK_RELEASE_PCT}% after {BTC_LOCK_MIN_HOURS}h) | '
          f'LOSS_BRAKE={LOSS_BRAKE_MODE} ({LOSS_BRAKE_STOP_COUNT} stops/{LOSS_BRAKE_WINDOW_MIN:.0f}min -> pause {LOSS_BRAKE_PAUSE_HOURS:g}h)', flush=True)
    print(f'ENTRY GUARDS | DIRECTION_LIMIT={DIRECTION_LIMIT_MODE} (max {MAX_SAME_DIRECTION}) | LATE_ENTRY={LATE_ENTRY_MODE} (chase>{LATE_ENTRY_CHASE_ATR} / adverse>{LATE_ENTRY_ADVERSE_ATR} ATR) | '
          f'STOP_REMOVE={STOP_REMOVE_MODE} | AUTO_ADD={AUTO_ADD_MODE} (en fazla {AUTO_ADD_MAX_PER_SCAN}/tarama) | ENTRY_WAIT={ENTRY_WAIT_MODE} | TREND_SL={TREND_SL_MODE} (4s EMA{TREND_SL_EMA}+Supertrend) | M15_EXIT={M15_EXIT_MODE} ({M15_EXIT_COUNT} ters 15dk sinyal) | BREAKEVEN={BREAKEVEN_MODE} (+{BREAKEVEN_ATR} ATR, buffer {BREAKEVEN_BUFFER_ATR}) | EARLY_EXIT={EARLY_EXIT_MODE} (loss>{EARLY_EXIT_LOSS_ATR} ATR under BTC lock)', flush=True)
    threading.Thread(target=start_dashboard, daemon=True).start()
    state = load_state()
    if state.get('position'):
        p = state['position']
        print(f'STATE RESTORED | position={p.get("side")} {p.get("symbol")} | entry={float(p.get("entry_price", 0)):.4f} | equity={float(state.get("equity", STARTING_EQUITY)):.2f}', flush=True)
    else:
        print(f'STATE RESTORED | position=FLAT | equity={float(state.get("equity", STARTING_EQUITY)):.2f}', flush=True)
    # Verify Telegram at startup without sending a message. This is safe across
    # Railway restarts and cannot spam the chat.
    if os.environ.get('TELEGRAM_VERIFY_ON_START', 'true').strip().lower() in ('1','true','yes','on'):
        if verify_connection():
            state['telegram_verified_once'] = True
            save_state(state)
    while True:
        try:
            # Fresh reload every cycle (instead of reusing the in-memory dict):
            # the dashboard's watchlist add/remove endpoints write to the same
            # file from another thread, and without a reload here this loop's
            # next save would silently overwrite those changes.
            state = load_state()
            signal_df = fetch_cached(cfg.SIGNAL_SYMBOL, 'USD_M', cfg.INTERVAL, 300)
            long_df = fetch(cfg.LONG_SYMBOL, 'COIN_M', 2)     # only the latest candle is used
            short_df = fetch(cfg.SHORT_SYMBOL, 'USD_M', 2)
            # Daily volatility veto uses only the latest fully closed 1D candle.
            daily_df = fetch_cached(cfg.SIGNAL_SYMBOL, 'USD_M', '1d', 400)
            daily_closed = daily_df.iloc[:-1].copy()
            volatile_now, atrp_pct, atrp_value = is_volatile_daily(
                daily_closed,
                cfg.VOLATILITY_ATR_LENGTH,
                cfg.VOLATILITY_PERCENTILE_LENGTH,
                cfg.VOLATILITY_LOW_PERCENTILE,
                cfg.VOLATILITY_HIGH_PERCENTILE,
            )
            # Only fully closed 4H candles are eligible for signals.
            closed = signal_df.iloc[:-1].copy()
            enriched = add_indicators(closed, cfg)
            latest = enriched.iloc[-1]
            latest_closed_time = latest['close_time']
            now = datetime.now(timezone.utc)
            state['last_heartbeat'] = now.isoformat()
            # Daily Telegram report at 09:00 Europe/Istanbul. If the service restarts after 09:00,
            # send the missed report immediately, but never more than once per local calendar day.
            from zoneinfo import ZoneInfo
            tr_now = now.astimezone(ZoneInfo('Europe/Istanbul'))
            report_date = tr_now.date().isoformat()
            if tr_now.hour >= 9 and state.get('last_daily_report_date') != report_date:
                # Read current trade history before sending the report.
                report_trades = []
                if os.path.exists(TRADES_FILE):
                    try:
                        report_trades = pd.read_csv(TRADES_FILE).fillna('').to_dict('records')
                    except Exception:
                        report_trades = []
                msg = daily_report(state, report_trades, (tr_now.date()).isoformat())
                if send_message(msg):
                    state['last_daily_report_date'] = report_date
                    save_state(state)
                    print(f'TELEGRAM | daily report sent | {report_date} 09:00 Europe/Istanbul', flush=True)
            # Per-user LIVE trading daily reports (Telegram) — same 09:00
            # Europe/Istanbul trigger as the shared report above, but sent
            # individually to each linked user about their own real trades.
            # Wrapped so a failure here can never affect the trading loop.
            try:
                live_trading.maybe_send_daily_reports(now)
            except Exception as e:
                print(f'LIVE | ERROR | daily report | {type(e).__name__}: {e}', flush=True)
            # AI Trade Analyst — read-only, gated internally (weekly + min new trades).
            # Never touches position/config state; wrapped so a failure here can never
            # affect the trading loop.
            try:
                ai_analyst.maybe_run_analysis(state, now)
            except Exception as e:
                print(f'AI_ANALYST | ERROR | {type(e).__name__}: {e}', flush=True)
            state['market_prices'] = {
                'ETHUSDT': float(short_df.iloc[-1]['close']),
                'ETHUSD_PERP': float(long_df.iloc[-1]['close'])
            }
            state['market_price_time'] = now.isoformat()
            def fmt(v):
                try:
                    x=float(v)
                    return round(x,4)
                except Exception:
                    return None
            state['signals'] = {
                'ema': 'BULLISH' if float(latest.get('ema_fast',0)) > float(latest.get('ema_slow',0)) else 'BEARISH',
                'supertrend': 'BULLISH' if bool(latest.get('supertrend_direction', False)) else 'BEARISH',
                'adx': fmt(latest.get('adx')), 'rsi': fmt(latest.get('rsi')),
                'cci': fmt(latest.get('cci')), 'stoch': 'K/D loaded',
                'macd': 'BULLISH' if float(latest.get('macd',0)) > float(latest.get('macd_signal',0)) and float(latest.get('macd_hist',0)) > 0 else 'BEARISH',
                'volatility': 'BLOCKED' if (cfg.USE_VOLATILE_FILTER and volatile_now) else 'OK',
                'atrp_percentile_1d': fmt(atrp_pct),
                'final': 'VOLATILE_BLOCK' if (cfg.USE_VOLATILE_FILTER and volatile_now) else ('LONG' if long_signal(enriched, len(enriched)-1, cfg) else ('SHORT' if short_signal(enriched, len(enriched)-1, cfg) else 'NONE'))
            }
            # Refresh the BTC lock once per cycle (cheap: cached 60s). Wrapped
            # so a problem here can never stop the trading loop.
            try:
                update_btc_lock(state, now)
            except Exception as e:
                print(f'BTC LOCK | ERROR | {type(e).__name__}: {e}', flush=True)
            save_state(state)

            # Manage current position using the live execution candle first.
            if state['position']:
                long_live = long_df.iloc[-1]
                short_live = short_df.iloc[-1]
                if process_intrabar(state, long_live, short_live, now):
                    pass

            # Process each newly closed signal candle exactly once.
            last = state.get('last_closed_time')
            last_ts = pd.Timestamp(last) if last else None
            if last_ts is None or latest_closed_time > last_ts:
                # Use the new candle's close to generate a signal; enter at the current execution candle open.
                state['last_closed_time'] = latest_closed_time.isoformat()
                if state['position'] is None:
                    go_long = long_signal(enriched, len(enriched)-1, cfg)
                    go_short = short_signal(enriched, len(enriched)-1, cfg)
                    if cfg.USE_VOLATILE_FILTER and volatile_now:
                        go_long = False
                        go_short = False
                        print(f'CANDLE {latest_closed_time} | VOLATILE BLOCK | 1D ATRP percentile={atrp_pct}', flush=True)
                    # Main ETH engine: the same BTC lock / loss brake / late-entry
                    # checks as the watchlist apply to NEW entries only (the
                    # watchlist-wide direction limit does not apply to ETH, and
                    # ETH's own exits are untouched). Fails open on any error.
                    g_close = float(latest['close']); g_atr = float(latest['atr']); g_px = float(short_df.iloc[-1]['close'])
                    if go_long and not go_short:
                        allowed, _why = entry_guard_check(state, cfg.SIGNAL_SYMBOL, 'LONG', now, g_close, g_atr, g_px, skip_direction_limit=True)
                        if allowed:
                            px = exec_price(float(long_df.iloc[-1]['open']), 'BUY')
                            enter(state, 'LONG', px, latest, now)
                        else:
                            print(f"CANDLE {latest_closed_time} | LONG signal blocked by entry guard", flush=True)
                    elif go_short and not go_long:
                        allowed, _why = entry_guard_check(state, cfg.SIGNAL_SYMBOL, 'SHORT', now, g_close, g_atr, g_px, skip_direction_limit=True)
                        if allowed:
                            px = exec_price(float(short_df.iloc[-1]['open']), 'SELL')
                            enter(state, 'SHORT', px, latest, now)
                        else:
                            print(f"CANDLE {latest_closed_time} | SHORT signal blocked by entry guard", flush=True)
                    else:
                        print(f"CANDLE {latest_closed_time} | no signal", flush=True)
                else:
                    print(f"CANDLE {latest_closed_time} | position already open: {state['position']['side']}", flush=True)
                save_state(state)

            # Manually-added watchlist symbols (from the scanner's "+ Ekle" button).
            # Crypto and US stock symbols each get a full, independent paper
            # position using the same strategy; BIST symbols are signal-only
            # (bist_scanner never trades).
            try:
                restore_orphaned_live_positions(state, now)
            except Exception as e:
                print(f'LIVE RESTORE | ERROR | {type(e).__name__}: {e}', flush=True)
            try:
                auto_add_from_scanner(state, now)
                save_state(state)
            except Exception as e:
                print(f'AUTO ADD | ERROR | {type(e).__name__}: {e}', flush=True)
            for wsym, w in list(state.get('watchlist', {}).items()):
                market = w.get('market')
                try:
                    if market == 'crypto':
                        run_watchlist_symbol(state, wsym, now)
                    elif market == 'us_stock':
                        run_us_stock_watchlist_symbol(state, wsym, now)
                except Exception as e:
                    if type(e).__name__ == 'BinanceCooldown':
                        # Binance asked us to slow down: skip the rest of this cycle
                        # (one log line instead of one per coin).
                        print(f'WATCHLIST | Binance bekleme süresi aktif, kalan semboller bu turda atlandı ({wsym} ve sonrası)', flush=True)
                        break
                    print(f'WATCHLIST ERROR | {wsym} | {type(e).__name__}: {e}', flush=True)
            sync_bist_watchlist_signals(state, now)

            time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f'ERROR | {type(e).__name__}: {e}', flush=True)
            time.sleep(POLL_SECONDS)

if __name__ == '__main__':
    main()
