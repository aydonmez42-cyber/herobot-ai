"""
PHASE 2 — real-money order execution supervisor.

This module never decides WHEN to trade — that is still entirely the
existing strategy/signal logic in paper_trading.py (the ETH 4H engine and
the shared watchlist). It only decides, once the strategy has already
opened or closed a PAPER position, whether to mirror that exact same
entry/exit as a REAL Binance Futures order for each user who has
explicitly turned on live trading for their own account.

Every call in here is wrapped by the caller (paper_trading.py) in a
try/except, so a bug or a Binance outage here can never break the paper
engine or the dashboard for anyone.

Safety rules enforced here, per the architecture the user chose:
  - Position size: each user's own fixed USD amount (auth.py:
    live_position_usd), never a % of balance and never a shared default.
  - Daily max-loss limit: once a user's estimated realized loss for the
    current UTC day reaches their configured limit, new entries are paused
    for that user until the next UTC day. Existing open positions are still
    actively managed/closed by the bot's own SL/TP/trailing logic — pausing
    "new entries" does not mean abandoning a position already open with
    real money on it.
  - Max leverage: capped per-user, and additionally hard-capped in this
    module (HARD_MAX_LEVERAGE) regardless of what a user configures.
  - Max open positions: per-user cap on how many symbols can be live at once.
  - Global emergency stop: an admin-controlled kill switch (auth.py) blocks
    ALL new entries for ALL users the instant it's turned on. It does NOT
    block exits — halting new trades but leaving already-open real
    positions completely unmanaged would be more dangerous than letting the
    bot keep closing them on its own stop-loss/take-profit/trailing logic.
"""
import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import auth
import binance_live as blive
import telegram_notifier as tg

DATA_DIR = os.environ.get('PAPER_DATA_DIR', '')
LIVE_RUNTIME_FILE = os.environ.get(
    'LIVE_RUNTIME_FILE',
    os.path.join(DATA_DIR, 'live_runtime.json') if DATA_DIR else 'live_runtime.json'
)
LIVE_TRADES_FILE = os.environ.get(
    'LIVE_TRADES_FILE',
    os.path.join(DATA_DIR, 'live_trades.csv') if DATA_DIR else 'live_trades.csv'
)
LIVE_TRADES_FIELDS = ['username', 'side', 'symbol', 'qty', 'entry_price', 'exit_price',
                       'pnl', 'reason', 'entry_time', 'exit_time', 'source']

HARD_MAX_LEVERAGE = 10  # absolute ceiling regardless of any per-user setting


# ---------------------------------------------------------------------------
# PER-MEMBER ENTRY GUARDS (live only). The demo/paper account trades every
# signal; before a signal is copied to a member's real Binance account these
# checks run FOR THAT MEMBER:
#   BTC lock      - no new LONG copies while Bitcoin is falling (state computed
#                   by paper_trading.update_btc_lock, passed in guard_ctx)
#   loss brake    - N of THIS member's own live stop-outs inside a short window
#                   pause THIS member's new copies for a few hours
#   direction cap - at most LIVE_MAX_SAME_DIRECTION open live positions in the
#                   same direction (0 = off), counted on THIS member's account
#   late entry    - skip when price already ran away from / against the signal
# Same environment variables (Railway -> Variables) as before; each mode is
# 'on' | 'log' | 'off'.
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
LOSS_BRAKE_MODE = _env_mode('LOSS_BRAKE_MODE', 'on')
LOSS_BRAKE_STOP_COUNT = int(_env_float('LOSS_BRAKE_STOP_COUNT', 3))
LOSS_BRAKE_WINDOW_MIN = _env_float('LOSS_BRAKE_WINDOW_MIN', 60)
LOSS_BRAKE_PAUSE_HOURS = _env_float('LOSS_BRAKE_PAUSE_HOURS', 12)
LIVE_MAX_SAME_DIRECTION = int(_env_float('LIVE_MAX_SAME_DIRECTION', _env_float('MAX_SAME_DIRECTION', 10)))
LATE_ENTRY_MODE = _env_mode('LATE_ENTRY_MODE', 'on')
LATE_ENTRY_CHASE_ATR = _env_float('LATE_ENTRY_CHASE_ATR', 1.5)
LATE_ENTRY_ADVERSE_ATR = _env_float('LATE_ENTRY_ADVERSE_ATR', 1.0)
MAX_RECENT_SKIPS = 20


def _parse_iso_utc(v):
    d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _brake_until(urec, now):
    """Active loss-brake end time for this member, or None."""
    try:
        u = urec.get('brake_until')
        if u and _parse_iso_utc(u) > now:
            return _parse_iso_utc(u)
    except Exception:
        pass
    return None


def _entry_guard_for_user(urec, symbol, side, ctx, now):
    """Returns a list of blocking guard codes for this member (empty = allowed).
    Fails OPEN on any error so a bug here can never stop live copying."""
    try:
        codes = []
        if LOSS_BRAKE_MODE == 'on' and _brake_until(urec, now):
            codes.append('LOSS_BRAKE')
        if side == 'LONG' and BTC_LOCK_MODE == 'on' and ctx and ctx.get('btc_lock_active'):
            codes.append('BTC_LOCK')
        if LIVE_MAX_SAME_DIRECTION > 0:
            same = sum(1 for p in urec['positions'].values() if p.get('side') == side)
            if same >= LIVE_MAX_SAME_DIRECTION:
                codes.append('DIRECTION_LIMIT')
        if LATE_ENTRY_MODE == 'on' and ctx and ctx.get('signal_close') and ctx.get('atr') and ctx.get('price'):
            sc, atr, px = float(ctx['signal_close']), float(ctx['atr']), float(ctx['price'])
            move = (px - sc) / atr if side == 'LONG' else (sc - px) / atr
            if move > LATE_ENTRY_CHASE_ATR or move < -LATE_ENTRY_ADVERSE_ATR:
                codes.append('LATE_ENTRY')
        return codes
    except Exception as e:
        print(f'LIVE GUARD | ERROR (fail-open) | {type(e).__name__}: {e}', flush=True)
        return []


def _record_skip(urec, symbol, side, codes, now):
    skips = urec.setdefault('skips', [])
    skips.append({'symbol': symbol, 'side': side, 'codes': codes, 'at': now.isoformat()})
    del skips[:-MAX_RECENT_SKIPS]


def _record_user_stop(username, urec, now):
    """A live position of THIS member just closed at a loss via a stop. N such
    stops inside the window pause this member's new live copies."""
    if LOSS_BRAKE_MODE == 'off':
        return
    cutoff = now - timedelta(hours=24)
    stops = [t for t in urec.get('recent_stops', []) if _parse_iso_utc(t) >= cutoff]
    stops.append(now.isoformat())
    urec['recent_stops'] = stops
    window_start = now - timedelta(minutes=LOSS_BRAKE_WINDOW_MIN)
    n = sum(1 for t in stops if _parse_iso_utc(t) >= window_start)
    if n >= LOSS_BRAKE_STOP_COUNT and not _brake_until(urec, now):
        until = now + timedelta(hours=LOSS_BRAKE_PAUSE_HOURS)
        print(f'LIVE | {username} | LOSS BRAKE TRIGGERED ({LOSS_BRAKE_MODE}) | {n} stops in {LOSS_BRAKE_WINDOW_MIN:.0f} min | new live entries paused until {until.isoformat()}', flush=True)
        if LOSS_BRAKE_MODE == 'on':
            urec['brake_until'] = until.isoformat()
            _notify_user(username, lambda n=n: tg.live_risk_alert(
                f'⛔ Zarar freni devrede: {LOSS_BRAKE_WINDOW_MIN:.0f} dakika içinde {n} stop oldu.\n'
                f'Hesabınızda yeni canlı girişler {LOSS_BRAKE_PAUSE_HOURS:g} saat durduruldu. Açık pozisyonlar yönetilmeye devam eder.'))

_lock = threading.Lock()


def _notify_user(username, text):
    """Best-effort personal Telegram message — never allowed to raise, since
    this is called from inside the order-placement path.

    `text` may be a plain string OR a zero-argument callable that builds the
    string. Passing a callable means a bug while BUILDING the message (bad
    number format, missing field...) is caught and logged here too, instead
    of escaping and silently skipping the notification. Every skip/failure
    path prints a log line so a missing Telegram message can be diagnosed."""
    try:
        chat_id = auth.get_telegram_chat_id(username)
        if not chat_id:
            print(f'LIVE | TELEGRAM SKIP | {username} | Telegram bağlı değil (chat_id kayıtlı değil)', flush=True)
            return
        message = text() if callable(text) else text
        if tg.send_to(chat_id, message):
            print(f'LIVE | TELEGRAM SENT | {username} | chat_id={chat_id}', flush=True)
        else:
            print(f'LIVE | TELEGRAM SEND FAILED | {username} | chat_id={chat_id} | ayrıntı için yukarıdaki TELEGRAM | send satırlarına bakın', flush=True)
    except Exception as e:
        print(f'LIVE | TELEGRAM NOTIFY ERROR | {username} | {type(e).__name__}: {e}', flush=True)


def _log_live_trade(row):
    try:
        exists = os.path.exists(LIVE_TRADES_FILE)
        with open(LIVE_TRADES_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=LIVE_TRADES_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        print(f'LIVE | TRADE LOG ERROR | {type(e).__name__}: {e}', flush=True)


def _read_live_trades():
    if not os.path.exists(LIVE_TRADES_FILE):
        return []
    try:
        with open(LIVE_TRADES_FILE, 'r', encoding='utf-8', newline='') as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _load_runtime():
    return _read_json(LIVE_RUNTIME_FILE, {})


def _save_runtime(data):
    _write_json(LIVE_RUNTIME_FILE, data)


def _get_user_runtime(runtime, username):
    rec = runtime.setdefault(username, {
        'day': _today(), 'realized_loss_usd': 0.0, 'realized_pnl_usd': 0.0,
        'positions': {}, 'paused_today': False, 'last_error': None,
        'last_error_notified': None, 'last_daily_report_date': None,
    })
    if rec.get('day') != _today():
        rec['day'] = _today()
        rec['realized_loss_usd'] = 0.0
        rec['paused_today'] = False
    rec.setdefault('positions', {})
    rec.setdefault('realized_pnl_usd', 0.0)
    rec.setdefault('last_error_notified', None)
    rec.setdefault('last_daily_report_date', None)
    return rec


def get_runtime_status(username):
    """Read-only snapshot for the dashboard's account panel."""
    with _lock:
        runtime = _load_runtime()
        rec = _get_user_runtime(runtime, username)
        _save_runtime(runtime)
    return {
        'paused_today': bool(rec.get('paused_today')),
        'realized_loss_usd': round(rec.get('realized_loss_usd', 0.0), 2),
        'realized_pnl_usd': round(rec.get('realized_pnl_usd', 0.0), 2),
        'open_positions': rec.get('positions', {}),
        'open_position_count': len(rec.get('positions', {})),
        'last_error': rec.get('last_error'),
    }


def clear_last_error(username):
    """Canlı işlem açılıp/kapatıldığında eski hata yazısını temizler.
    Yalnızca last_error alanlarına dokunur; pozisyon ve P&L kayıtları korunur."""
    with _lock:
        runtime = _load_runtime()
        rec = _get_user_runtime(runtime, username)
        rec['last_error'] = None
        rec['last_error_notified'] = None
        _save_runtime(runtime)


# ---------------------------------------------------------------------------
# Per-user read-only views for the dashboard's "Canlı İşlemlerim" panel.
# Deliberately separate from get_runtime_status()'s small account-panel
# summary: this returns full position/trade detail, scoped to ONE user's
# own data only — never another user's, and never the shared paper engine.
# ---------------------------------------------------------------------------

def get_user_open_positions_detailed(username):
    with _lock:
        runtime = _load_runtime()
        rec = _get_user_runtime(runtime, username)
        positions = dict(rec.get('positions', {}))
        _save_runtime(runtime)
    out = []
    for symbol, p in positions.items():
        entry = float(p.get('entry_price') or 0)
        qty = float(p.get('qty') or 0)
        side = p.get('side')
        current = blive.get_mark_price(symbol)
        if current is None:
            current = entry  # public price lookup failed — show entry rather than a wrong number
        unreal = (current - entry) * qty if side == 'LONG' else (entry - current) * qty
        out.append({
            'symbol': symbol, 'side': side, 'qty': qty, 'entry_price': entry,
            'current_price': current, 'unrealized_pnl': unreal,
            'leverage': p.get('leverage'), 'entry_time': p.get('entry_time'),
        })
    out.sort(key=lambda x: x.get('entry_time') or '', reverse=True)
    return out


def get_user_closed_trades(username, limit=20):
    if not os.path.exists(LIVE_TRADES_FILE):
        return []
    rows = []
    try:
        with open(LIVE_TRADES_FILE, 'r', encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                if row.get('username') == username:
                    rows.append(row)
    except Exception:
        return []
    rows.sort(key=lambda r: r.get('exit_time') or '', reverse=True)
    return rows[:limit]


_account_cache = {}  # username -> {'ts': float, 'data': dict}
_account_cache_lock = threading.Lock()
ACCOUNT_CACHE_SECONDS = 45


def get_binance_account_summary(username):
    """The user's real Binance Futures wallet numbers (balance, unrealized
    P&L, realized P&L today/7d/30d, fees). Read-only and cached for a few
    seconds per user so a dashboard that refreshes often does not hammer
    Binance (which is already rate limited). Always returns a dict; on any
    problem it carries an 'error' string instead of numbers."""
    now = time.time()
    with _account_cache_lock:
        c = _account_cache.get(username)
        if c and now - c['ts'] < ACCOUNT_CACHE_SECONDS:
            return c['data']
    try:
        api_key, api_secret = auth.get_decrypted_binance_credentials(username)
    except Exception:
        api_key = api_secret = None
    if not api_key or not api_secret:
        data = {'error': 'Binance API anahtarı bağlı değil.'}
    else:
        try:
            result, err = blive.get_account_overview(api_key, api_secret)
        except Exception as e:
            result, err = None, f'{type(e).__name__}'
        data = result if result else {'error': err or 'Bilinmeyen hata'}
        data['fetched_at'] = datetime.now(timezone.utc).isoformat()
    with _account_cache_lock:
        old = _account_cache.get(username)
        # keep the last good numbers visible if a refresh fails (e.g. 429)
        if 'error' in data and old and 'error' not in old['data']:
            stale = dict(old['data']); stale['stale_error'] = data['error']
            data = stale
        _account_cache[username] = {'ts': now, 'data': data}
    return data


def get_open_live_holders():
    """Every REAL open position across all members, grouped by symbol:
    {symbol: [{'username','side','entry_price','qty','entry_time'}, ...]}.
    Used to (a) refuse removing a watchlist coin somebody is still live in and
    (b) re-create the demo position that carries SL/TP/trailing for it."""
    with _lock:
        runtime = _load_runtime()
    out = {}
    for username, rec in runtime.items():
        if not isinstance(rec, dict):
            continue
        for symbol, p in (rec.get('positions') or {}).items():
            out.setdefault(symbol, []).append({
                'username': username, 'side': p.get('side'),
                'entry_price': float(p.get('entry_price') or 0),
                'qty': float(p.get('qty') or 0), 'entry_time': p.get('entry_time'),
            })
    return out


def get_my_pnl_series(username, max_points=1000):
    """Cumulative realized P&L of THIS member's closed live trades, oldest to
    newest, as [[exit_time_iso, cumulative_pnl, trade_pnl], ...] for the
    dashboard's line chart (only this member's own rows, never anyone else's)."""
    rows = []
    for r in _read_live_trades():
        if r.get('username') != username:
            continue
        try:
            rows.append((r.get('exit_time') or '', float(r.get('pnl') or 0)))
        except ValueError:
            continue
    rows.sort(key=lambda x: x[0])
    out, cum = [], 0.0
    for t, pnl in rows:
        cum += pnl
        out.append([t, round(cum, 4), round(pnl, 4)])
    return out[-max_points:]


def get_my_guard_status(username):
    """This member's own guard state for the dashboard: active loss brake and
    the most recent skipped copies. (BTC lock is global and added by the
    dashboard.)"""
    now = datetime.now(timezone.utc)
    with _lock:
        runtime = _load_runtime()
        urec = _get_user_runtime(runtime, username)
        until = _brake_until(urec, now)
        skips = list(urec.get('skips', []))[-8:]
    return {
        'brake_until': until.isoformat() if until else None,
        'skips': list(reversed(skips)),
        'max_same_direction': LIVE_MAX_SAME_DIRECTION,
    }


def get_my_live_summary(username):
    """Everything the dashboard's per-user live panel needs in one call."""
    return {
        'open': get_user_open_positions_detailed(username),
        'closed': get_user_closed_trades(username),
        'binance': get_binance_account_summary(username),
        'guards': get_my_guard_status(username),
        'pnl_series': get_my_pnl_series(username),
    }


def get_my_open_symbols(username):
    """Symbols this member currently holds a REAL position in (light call for
    the Paper Trades page buttons) plus whether live trading is switched on."""
    with _lock:
        runtime = _load_runtime()
        rec = (runtime.get(username) or {})
        symbols = sorted((rec.get('positions') or {}).keys())
    live_on = any(u['username'] == username for u in auth.list_live_enabled_users())
    return {'symbols': symbols, 'live_enabled': live_on}


def open_position_now(username, symbol, side, demo_price=None):
    """Member-initiated 'Open live' for a coin whose DEMO position is already
    open: copies that demo position onto THIS member's own Binance account at
    the current market price. From then on it is managed exactly like any
    other live copy (the demo position's SL / TP / trailing / exit signal close
    it, because exits match on symbol + side).

    This is an explicit decision by the member, so the market guards (BTC lock,
    loss brake, late-entry) are skipped; the hard risk limits are NOT: global
    emergency stop, live switch + verified key + active subscription, daily
    loss limit pause, max open positions, same-direction cap and position size
    all still apply. Returns (ok, message_or_error)."""
    kill = auth.get_global_kill_switch()
    if kill.get('active'):
        return False, 'Yönetici tüm canlı işlemleri acil olarak durdurdu.'
    rec = next((u for u in auth.list_live_enabled_users() if u['username'] == username), None)
    if not rec:
        return False, 'Canlı işlem sizin için kapalı. Hesabım → Canlı İşlem bölümünden açın (Binance bağlı ve doğrulanmış olmalı).'
    if side not in ('LONG', 'SHORT'):
        return False, 'Geçersiz yön.'
    with _lock:
        runtime = _load_runtime()
        urec = _get_user_runtime(runtime, username)
        if urec.get('paused_today'):
            return False, 'Günlük maksimum kayıp limitine ulaşıldığı için canlı işlem bugün durduruldu.'
        if symbol in urec['positions']:
            return False, f'{symbol} için zaten açık bir canlı pozisyonunuz var.'
        max_positions = int(rec.get('live_max_open_positions') or 1)
        if len(urec['positions']) >= max_positions:
            return False, f'Maksimum açık pozisyon sayınıza ulaştınız ({max_positions}).'
        if LIVE_MAX_SAME_DIRECTION > 0 and sum(1 for p in urec['positions'].values() if p.get('side') == side) >= LIVE_MAX_SAME_DIRECTION:
            return False, f'Aynı yönde en fazla {LIVE_MAX_SAME_DIRECTION} canlı pozisyon açılabilir.'
        position_usd = float(rec.get('live_position_usd') or 0)
        if position_usd <= 0:
            return False, 'Canlı işlem tutarınız ayarlanmamış.'
        api_key, api_secret = auth.get_decrypted_binance_credentials(username)
        if not api_key or not api_secret:
            return False, 'Binance anahtarınız okunamadı — Binance bağlantınızı kontrol edin.'
        price = blive.get_mark_price(symbol) or demo_price
        qty, err = blive.compute_quantity(symbol, position_usd, price)
        if err:
            return False, err
        leverage = max(1, min(int(rec.get('live_max_leverage') or 1), HARD_MAX_LEVERAGE))
        blive.set_leverage(api_key, api_secret, symbol, leverage)
        ok, result = blive.place_market_order(api_key, api_secret, symbol, 'BUY' if side == 'LONG' else 'SELL', qty, reduce_only=False)
        if not ok:
            urec['last_error'] = result
            _save_runtime(runtime)
            print(f'LIVE | {username} | MANUAL OPEN FAILED | {symbol} {side} | {result}', flush=True)
            return False, f'Emir başarısız oldu: {result}'
        fill_price = blive.fill_price_from_order(result, price)
        urec['positions'][symbol] = {
            'side': side, 'qty': qty, 'entry_price': fill_price,
            'entry_time': datetime.now(timezone.utc).isoformat(), 'source': 'manual_join',
            'leverage': leverage,
        }
        urec['last_error'] = None
        urec['last_error_notified'] = None
        _save_runtime(runtime)
        print(f'LIVE ENTRY | {username} | {side} {symbol} | qty={qty} | price~{fill_price} | leverage={leverage}x | manual_join', flush=True)
        _notify_user(username, lambda: tg.live_entry_message(side, symbol, qty, fill_price, leverage, position_usd))
    return True, f'{symbol} {side} canlı pozisyonu açıldı (~{fill_price}).'


def close_position_now(username, symbol):
    """User-initiated manual close (the dashboard's 'Pozisyonu Şimdi Kapat'
    button) — the one case where an open real position is closed WITHOUT
    waiting for the shared strategy's own exit signal. Mirrors
    on_exit_signal()'s bookkeeping exactly (same trade log, same realized
    P&L / daily-loss-limit accounting, same Telegram message), but scoped to
    one user's one symbol and called synchronously so the dashboard can show
    success/failure immediately instead of finding out later. Deliberately
    NOT gated by live_trading_enabled or the global kill switch, for the
    same reason on_exit_signal isn't — closing a position down is always
    allowed, only opening new ones is ever blocked."""
    with _lock:
        runtime = _load_runtime()
        urec = _get_user_runtime(runtime, username)
        pos = urec['positions'].get(symbol)
        if not pos:
            return False, 'Bu sembolde açık canlı pozisyonunuz yok.'
        rec = auth.get_user(username)
        if not rec:
            return False, 'Hesap bulunamadı.'
        api_key, api_secret = auth.get_decrypted_binance_credentials(username)
        if not api_key or not api_secret:
            return False, 'Binance anahtarınız okunamadı — lütfen Binance bağlantınızı kontrol edin.'

        side = pos['side']
        qty = pos['qty']
        order_side = 'SELL' if side == 'LONG' else 'BUY'
        ok, result = blive.place_market_order(api_key, api_secret, symbol, order_side, qty, reduce_only=True)
        if not ok:
            urec['last_error'] = result
            _save_runtime(runtime)
            print(f'LIVE | {username} | MANUAL CLOSE FAILED | {symbol} {side} | {result}', flush=True)
            return False, f'Kapatma emri başarısız oldu: {result}'

        fallback_price = blive.get_mark_price(symbol) or pos['entry_price']
        fill_price = blive.fill_price_from_order(result, fallback_price)
        entry_price = pos['entry_price']
        gross = (fill_price - entry_price) * qty if side == 'LONG' else (entry_price - fill_price) * qty
        urec['realized_pnl_usd'] += gross
        if gross < 0:
            urec['realized_loss_usd'] += gross
        urec['positions'].pop(symbol, None)
        _log_live_trade({
            'username': username, 'side': side, 'symbol': symbol, 'qty': qty,
            'entry_price': entry_price, 'exit_price': fill_price, 'pnl': gross,
            'reason': 'manual_close_by_user', 'entry_time': pos.get('entry_time'),
            'exit_time': datetime.now(timezone.utc).isoformat(), 'source': 'dashboard_manual',
        })
        daily_limit = float(rec.get('live_daily_loss_limit_usd') or 0)
        just_paused = False
        if daily_limit > 0 and urec['realized_loss_usd'] <= -abs(daily_limit) and not urec.get('paused_today'):
            urec['paused_today'] = True
            just_paused = True
        urec['last_error'] = None
        urec['last_error_notified'] = None
        _save_runtime(runtime)

    print(f'LIVE EXIT | {username} | {side} {symbol} | reason=manual_close_by_user | qty={qty} | price~{fill_price} | pnl~{gross:.2f}', flush=True)
    _notify_user(username, lambda: tg.live_exit_message(side, symbol, qty, entry_price, fill_price, gross, 'Kullanıcı panelden manuel kapattı'))
    if just_paused:
        _notify_user(username, lambda: tg.live_risk_alert(
            f'Günlük maksimum kayıp limitinize ulaşıldı (bugünkü tahmini kayıp: {urec["realized_loss_usd"]:.2f} USD, limit: {daily_limit:.2f} USD).\n'
            f'Yeni canlı işlem bugün (UTC) için durduruldu; yarın otomatik olarak tekrar açılacak.'
        ))
    return True, None


def on_entry_signal(symbol, side, price, source='eth_bot', guard_ctx=None):
    """side: 'LONG' or 'SHORT'. Called right after the shared strategy opens
    a paper position, at that same signal price, so live orders mirror the
    exact signal the paper account is trading."""
    kill = auth.get_global_kill_switch()
    if kill.get('active'):
        print(f'LIVE | ENTRY SKIPPED | global emergency stop active | {symbol} {side}', flush=True)
        return
    users = auth.list_live_enabled_users()
    if not users:
        return
    with _lock:
        runtime = _load_runtime()
        for rec in users:
            username = rec['username']
            urec = _get_user_runtime(runtime, username)
            if urec.get('paused_today'):
                continue
            if symbol in urec['positions']:
                continue  # already have a live position open on this symbol
            max_positions = int(rec.get('live_max_open_positions') or 1)
            if len(urec['positions']) >= max_positions:
                continue
            now_utc = datetime.now(timezone.utc)
            codes = _entry_guard_for_user(urec, symbol, side, guard_ctx, now_utc)
            if codes:
                _record_skip(urec, symbol, side, codes, now_utc)
                print(f'LIVE | {username} | ENTRY SKIPPED BY GUARD | {side} {symbol} | {",".join(codes)}', flush=True)
                continue
            position_usd = float(rec.get('live_position_usd') or 0)
            if position_usd <= 0:
                continue
            api_key, api_secret = auth.get_decrypted_binance_credentials(username)
            if not api_key or not api_secret:
                urec['last_error'] = 'Binance anahtarı okunamadı'
                continue
            qty, err = blive.compute_quantity(symbol, position_usd, price)
            if err:
                urec['last_error'] = err
                print(f'LIVE | {username} | ENTRY SKIPPED | {symbol} | {err}', flush=True)
                continue
            leverage = max(1, min(int(rec.get('live_max_leverage') or 1), HARD_MAX_LEVERAGE))
            blive.set_leverage(api_key, api_secret, symbol, leverage)
            order_side = 'BUY' if side == 'LONG' else 'SELL'
            ok, result = blive.place_market_order(api_key, api_secret, symbol, order_side, qty, reduce_only=False)
            if not ok:
                urec['last_error'] = result
                print(f'LIVE | {username} | ENTRY FAILED | {symbol} {side} | {result}', flush=True)
                if urec.get('last_error_notified') != str(result):
                    urec['last_error_notified'] = str(result)
                    _notify_user(username, lambda s=symbol, sd=side, r=result: tg.live_risk_alert(f'{s} {sd} canlı emri başarısız oldu:\n{r}\n\nBakiye/marj yetersizliği, API izin sorunu vb. olabilir — Binance hesabınızı kontrol edin.'))
                continue
            fill_price = blive.fill_price_from_order(result, price)
            urec['positions'][symbol] = {
                'side': side, 'qty': qty, 'entry_price': fill_price,
                'entry_time': datetime.now(timezone.utc).isoformat(), 'source': source,
                'leverage': leverage,
            }
            urec['last_error'] = None
            urec['last_error_notified'] = None
            print(f'LIVE ENTRY | {username} | {side} {symbol} | qty={qty} | price~{fill_price} | leverage={leverage}x', flush=True)
            _notify_user(username, lambda sd=side, s=symbol, q=qty, fp=fill_price, lv=leverage, pu=position_usd: tg.live_entry_message(sd, s, q, fp, lv, pu))
        _save_runtime(runtime)


def on_exit_signal(symbol, side, price, reason, source='eth_bot'):
    """side: the position side being closed (LONG/SHORT), matching
    paper_trading.py's own exit_position()/exit_symbol_position(). Deliberately
    NOT gated by the global kill switch or a user's live_trading_enabled flag
    — see module docstring: an already-open real position keeps being
    managed/closed until it's flat, regardless of those switches."""
    with _lock:
        runtime = _load_runtime()
        changed = False
        for username in list(runtime.keys()):
            urec = _get_user_runtime(runtime, username)
            pos = urec['positions'].get(symbol)
            if not pos or pos.get('side') != side:
                continue
            rec = auth.get_user(username)
            if not rec:
                continue
            api_key, api_secret = auth.get_decrypted_binance_credentials(username)
            if not api_key or not api_secret:
                urec['last_error'] = 'Binance anahtarı okunamadı — pozisyon Binance hesabında AÇIK kalmış olabilir, lütfen manuel kontrol edin.'
                print(f'LIVE | {username} | EXIT FAILED (no credentials) | {symbol} {side}', flush=True)
                continue
            qty = pos['qty']
            order_side = 'SELL' if side == 'LONG' else 'BUY'
            ok, result = blive.place_market_order(api_key, api_secret, symbol, order_side, qty, reduce_only=True)
            if not ok:
                urec['last_error'] = result
                print(f'LIVE | {username} | EXIT FAILED | {symbol} {side} | {result} | pozisyon Binance hesabında AÇIK kalmış olabilir, lütfen manuel kontrol edin.', flush=True)
                if urec.get('last_error_notified') != str(result):
                    urec['last_error_notified'] = str(result)
                    _notify_user(username, lambda s=symbol, sd=side, r=result: tg.live_risk_alert(f'{s} {sd} pozisyonunu kapatma emri BAŞARISIZ OLDU:\n{r}\n\nPozisyon Binance hesabınızda AÇIK kalmış olabilir — lütfen hemen manuel kontrol edin.'))
                changed = True
                continue
            fill_price = blive.fill_price_from_order(result, price)
            entry_price = pos['entry_price']
            gross = (fill_price - entry_price) * qty if side == 'LONG' else (entry_price - fill_price) * qty
            urec['realized_pnl_usd'] += gross
            if gross < 0:
                urec['realized_loss_usd'] += gross
            urec['positions'].pop(symbol, None)
            _log_live_trade({
                'username': username, 'side': side, 'symbol': symbol, 'qty': qty,
                'entry_price': entry_price, 'exit_price': fill_price, 'pnl': gross,
                'reason': reason, 'entry_time': pos.get('entry_time'),
                'exit_time': datetime.now(timezone.utc).isoformat(), 'source': source,
            })
            if gross < 0 and reason in ('ATR_SL', 'ATR_TRAILING_SL'):
                try:
                    _record_user_stop(username, urec, datetime.now(timezone.utc))
                except Exception as e:
                    print(f'LIVE | {username} | LOSS BRAKE ERROR | {type(e).__name__}: {e}', flush=True)
            daily_limit = float(rec.get('live_daily_loss_limit_usd') or 0)
            just_paused = False
            if daily_limit > 0 and urec['realized_loss_usd'] <= -abs(daily_limit) and not urec.get('paused_today'):
                urec['paused_today'] = True
                just_paused = True
                print(f'LIVE | {username} | GÜNLÜK MAKSİMUM KAYIP LİMİTİNE ULAŞILDI ({urec["realized_loss_usd"]:.2f} USD) | canlı işlem bugün için durduruldu', flush=True)
            urec['last_error'] = None
            urec['last_error_notified'] = None
            changed = True
            print(f'LIVE EXIT | {username} | {side} {symbol} | reason={reason} | qty={qty} | price~{fill_price} | pnl~{gross:.2f}', flush=True)
            _notify_user(username, lambda sd=side, s=symbol, q=qty, ep=entry_price, fp=fill_price, g=gross, rs=reason: tg.live_exit_message(sd, s, q, ep, fp, g, rs))
            if just_paused:
                _notify_user(username, lambda rl=urec["realized_loss_usd"], dl=daily_limit: tg.live_risk_alert(
                    f'Günlük maksimum kayıp limitinize ulaşıldı (bugünkü tahmini kayıp: {rl:.2f} USD, limit: {dl:.2f} USD).\n'
                    f'Yeni canlı işlem bugün (UTC) için durduruldu; yarın otomatik olarak tekrar açılacak. Ayarları hesap panelinizden istediğiniz an değiştirebilirsiniz.'
                ))
        if changed:
            _save_runtime(runtime)


def notify_kill_switch_change(active, by_username):
    """Called by dashboard.py right after an admin flips the global
    emergency stop. Best-effort — wrapped by the caller."""
    if not tg.TELEGRAM_BOT_ENABLED:
        return
    linked = {u['username']: u['chat_id'] for u in auth.list_telegram_linked_users()}
    if not linked:
        return
    interested = {rec['username'] for rec in auth.list_live_enabled_users()}
    with _lock:
        runtime = _load_runtime()
    for username, urec in runtime.items():
        if urec.get('positions'):
            interested.add(username)
    text = tg.live_risk_alert(
        f'Yönetici ({by_username}) tüm kullanıcılar için canlı işlemi '
        + ('DURDURDU 🛑 (yeni pozisyon açılmayacak, açık pozisyonlar bot tarafından yönetilmeye devam eder).' if active
           else 'TEKRAR AÇTI ✅.')
    )
    for username in interested:
        chat_id = linked.get(username)
        if chat_id:
            try:
                tg.send_to(chat_id, text)
            except Exception:
                pass


def maybe_send_daily_reports(now):
    """Sends each Telegram-linked user their own previous-day live-trading
    summary, once per Istanbul calendar day, at/after 09:00 Europe/Istanbul
    — the same trigger time as the shared paper-trading daily report.
    Wrapped by the caller (paper_trading.py) so a failure here never
    affects the trading loop."""
    if not tg.TELEGRAM_BOT_ENABLED:
        return
    from zoneinfo import ZoneInfo
    tr_now = now.astimezone(ZoneInfo('Europe/Istanbul'))
    if tr_now.hour < 9:
        return
    report_date = tr_now.date().isoformat()
    previous_date = (tr_now.date() - timedelta(days=1)).isoformat()
    linked = auth.list_telegram_linked_users()
    if not linked:
        return
    all_trades = _read_live_trades()

    def _in_previous_istanbul_day(exit_time_str):
        try:
            dt = datetime.fromisoformat(str(exit_time_str).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat() == previous_date
        except Exception:
            return False

    with _lock:
        runtime = _load_runtime()
        changed = False
        for u in linked:
            username, chat_id = u['username'], u['chat_id']
            try:
                urec = _get_user_runtime(runtime, username)
                if urec.get('last_daily_report_date') == report_date:
                    continue
                day_trades = [t for t in all_trades if t.get('username') == username and _in_previous_istanbul_day(t.get('exit_time'))]
                if not day_trades and not urec.get('positions') and not (auth.get_user(username) or {}).get('live_trading_enabled'):
                    # Never enabled live trading and nothing to report — skip silently, don't spam.
                    urec['last_daily_report_date'] = report_date
                    changed = True
                    continue
                text = tg.live_daily_report(username, previous_date, day_trades, urec.get('realized_pnl_usd', 0.0), urec.get('positions', {}))
                ok = tg.send_to(chat_id, text)
                if ok:
                    urec['last_daily_report_date'] = report_date
                    changed = True
                    print(f'LIVE | DAILY REPORT SENT | {username} | {previous_date}', flush=True)
                else:
                    print(f'LIVE | DAILY REPORT FAILED | {username} | chat_id={chat_id}', flush=True)
            except Exception as e:
                # One user's problem must not stop the report for everyone else.
                print(f'LIVE | DAILY REPORT ERROR | {username} | {type(e).__name__}: {e}', flush=True)
        if changed:
            _save_runtime(runtime)
