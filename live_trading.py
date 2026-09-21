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

_lock = threading.Lock()


def _notify_user(username, text):
    """Best-effort personal Telegram message — never allowed to raise, since
    this is called from inside the order-placement path."""
    try:
        chat_id = auth.get_telegram_chat_id(username)
        if chat_id:
            tg.send_to(chat_id, text)
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


def on_entry_signal(symbol, side, price, source='eth_bot'):
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
                    _notify_user(username, tg.live_risk_alert(f'{symbol} {side} canlı emri başarısız oldu:\n{result}\n\nBakiye/marj yetersizliği, API izin sorunu vb. olabilir — Binance hesabınızı kontrol edin.'))
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
            _notify_user(username, tg.live_entry_message(side, symbol, qty, fill_price, leverage, position_usd))
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
                    _notify_user(username, tg.live_risk_alert(f'{symbol} {side} pozisyonunu kapatma emri BAŞARISIZ OLDU:\n{result}\n\nPozisyon Binance hesabınızda AÇIK kalmış olabilir — lütfen hemen manuel kontrol edin.'))
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
            _notify_user(username, tg.live_exit_message(side, symbol, qty, entry_price, fill_price, gross, reason))
            if just_paused:
                _notify_user(username, tg.live_risk_alert(
                    f'Günlük maksimum kayıp limitinize ulaşıldı (bugünkü tahmini kayıp: {urec["realized_loss_usd"]:.2f} USD, limit: {daily_limit:.2f} USD).\n'
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
            try:
                ok = tg.send_to(chat_id, text)
            except Exception as e:
                print(f'LIVE | TELEGRAM DAILY REPORT ERROR | {username} | {type(e).__name__}: {e}', flush=True)
                ok = False
            if ok:
                urec['last_daily_report_date'] = report_date
                changed = True
        if changed:
            _save_runtime(runtime)
