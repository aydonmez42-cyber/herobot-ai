import html
import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
# TELEGRAM_ENABLED: the original single admin-broadcast channel (unchanged
# behavior — token + your own chat id). TELEGRAM_BOT_ENABLED: just the bot
# token, which is all that's needed to (a) message any per-user chat_id once
# a user has linked their own Telegram (see telegram_link.py), and (b) poll
# for incoming /start messages to do that linking in the first place.
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_BOT_ENABLED = bool(TELEGRAM_BOT_TOKEN)

_bot_username = None


def _api(method: str, payload=None, timeout=15):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}'
    return requests.post(url, json=payload or {}, timeout=timeout)


def get_bot_username():
    """Cached @handle of the configured bot, e.g. 'MyTradingBot' (no leading
    @). Returns None if no token is configured or getMe hasn't succeeded
    yet (verify_connection() populates this on startup)."""
    return _bot_username


def ensure_bot_username():
    """Lazily fetches/caches the bot's @handle if it isn't known yet, for
    UIs that need a 't.me/<handle>' link before verify_connection() has run.
    Safe to call often — only hits the network once."""
    global _bot_username
    if _bot_username or not TELEGRAM_BOT_TOKEN:
        return _bot_username
    try:
        r = requests.get(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe', timeout=10)
        if r.status_code == 200 and r.json().get('ok'):
            _bot_username = r.json().get('result', {}).get('username')
    except requests.RequestException:
        pass
    return _bot_username


def send_to(chat_id, text: str) -> bool:
    """Sends a message to an arbitrary chat_id (a per-user linked Telegram
    chat, or the admin's own). Only needs TELEGRAM_BOT_TOKEN — unlike the
    original send_message(), it does not require TELEGRAM_CHAT_ID."""
    if not TELEGRAM_BOT_ENABLED or not chat_id:
        return False
    try:
        r = _api('sendMessage', {'chat_id': chat_id, 'text': text}, timeout=15)
        if r.status_code != 200:
            print(f'TELEGRAM | send ERROR | chat_id={chat_id} | HTTP {r.status_code} | {r.text[:300]}', flush=True)
            return False
        data = r.json()
        if not data.get('ok'):
            print(f'TELEGRAM | send ERROR | chat_id={chat_id} | {data}', flush=True)
            return False
        return True
    except requests.RequestException as e:
        print(f'TELEGRAM | send ERROR | chat_id={chat_id} | {type(e).__name__} | {e}', flush=True)
        return False
    except Exception as e:
        print(f'TELEGRAM | send ERROR | chat_id={chat_id} | {type(e).__name__} | {e}', flush=True)
        return False


def verify_connection() -> bool:
    """Verify the bot token and chat ID without sending a startup message."""
    if not TELEGRAM_ENABLED:
        print('TELEGRAM | disabled | set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID', flush=True)
        return False
    try:
        r = _api('getMe', timeout=10)
        if r.status_code != 200:
            print(f'TELEGRAM | getMe ERROR | HTTP {r.status_code} | {r.text[:300]}', flush=True)
            return False
        data = r.json()
        if not data.get('ok'):
            print(f'TELEGRAM | getMe ERROR | {data}', flush=True)
            return False
        bot = data.get('result', {})
        global _bot_username
        _bot_username = bot.get('username')
        print(f'TELEGRAM | token OK | bot=@{bot.get("username", "unknown")}', flush=True)

        # Do not send a startup message here. Railway/container restarts can happen
        # automatically and must never create a Telegram-message loop.
        # Position-open/close and daily-report messages are sent separately.
        print(f'TELEGRAM | chat configured | chat_id={TELEGRAM_CHAT_ID}', flush=True)
        return True
    except requests.RequestException as e:
        print(f'TELEGRAM | connection ERROR | {type(e).__name__} | {e}', flush=True)
        return False
    except Exception as e:
        print(f'TELEGRAM | connection ERROR | {type(e).__name__} | {e}', flush=True)
        return False


def send_message(text: str) -> bool:
    """Original single admin-broadcast channel — unchanged behavior."""
    if not TELEGRAM_ENABLED:
        print('TELEGRAM | not configured', flush=True)
        return False
    ok = send_to(TELEGRAM_CHAT_ID, text)
    if ok:
        print('TELEGRAM | message sent', flush=True)
    return ok


def entry_message(p: dict) -> str:
    side_emoji = '🟢' if p['side'] == 'LONG' else '🔴'
    return (
        f'{side_emoji} TEST32 PAPER — YENİ POZİSYON\n\n'
        f'{p["side"]} {p["symbol"]}\n'
        f'Giriş: {p["entry_price"]:,.2f}\n'
        f'ATR: {p["atr"]:,.2f}\n'
        f'SL: {p["sl"]:,.2f}\n'
        f'TP: {p["tp"]:,.2f}\n'
        f'Miktar: {p["qty_eth"]:.2f} ETH\n'
        f'Sinyal: {p["signal_time"]}'
    )


def exit_message(t: dict) -> str:
    positive = float(t['net_pnl']) >= 0
    emoji = '✅' if positive else '❌'
    return (
        f'{emoji} TEST32 PAPER — POZİSYON KAPANDI\n\n'
        f'{t["side"]} {t["symbol"]}\n'
        f'Giriş: {float(t["entry_price"]):,.2f}\n'
        f'Çıkış: {float(t["exit_price"]):,.2f}\n'
        f'Net P&L: {float(t["net_pnl"]):+,.2f} $\n'
        f'Ücretler: {float(t["fees"]):,.2f} $\n'
        f'Çıkış nedeni: {t["reason"]}\n'
        f'Süre: {t.get("entry_time", "-")} → {t.get("exit_time", "-")}\n'
        f'Sanal bakiye: {float(t["equity_after"]):,.2f} $'
    )


def daily_report(state: dict, trades: list, report_date: str) -> str:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    local_date = datetime.fromisoformat(report_date).date()
    previous_date = (local_date - timedelta(days=1)).isoformat()
    day_trades = []
    for t in trades:
        raw = str(t.get('exit_time', ''))
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo('UTC'))
            if dt.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat() == previous_date:
                day_trades.append(t)
        except Exception:
            continue
    wins = sum(1 for t in day_trades if float(t.get('net_pnl', 0)) > 0)
    losses = sum(1 for t in day_trades if float(t.get('net_pnl', 0)) < 0)
    day_pnl = sum(float(t.get('net_pnl', 0)) for t in day_trades)
    all_pnl = sum(float(t.get('net_pnl', 0)) for t in trades)
    start = float(os.environ.get('PAPER_INITIAL_CAPITAL', '10000'))
    equity = float(state.get('equity', start))
    pos = state.get('position')
    lines = [
        '📊 TEST32 PAPER — GÜNLÜK RAPOR',
        f'Rapor saati: 09:00 Europe/Istanbul | Gün: {previous_date}',
        '',
        f'Önceki gün: {len(day_trades)} işlem | {wins}W / {losses}L | P&L {day_pnl:+,.2f} $',
        f'Toplam: {len(trades)} işlem | P&L {all_pnl:+,.2f} $',
        f'Sanal bakiye: {equity:,.2f} $',
        f'Getiri: {(equity/start-1)*100:+.2f}%',
    ]
    if pos:
        side = pos['side']; symbol = pos['symbol']; entry = float(pos['entry_price'])
        cp = float(state.get('market_prices', {}).get(symbol, entry)); qty = float(pos.get('qty_eth', 1))
        unreal = (cp-entry)*qty if side == 'LONG' else (entry-cp)*qty
        active_stop = float(pos.get('trail_stop')) if pos.get('trail_active') and pos.get('trail_stop') is not None else float(pos['sl'])
        lines += ['', f'📌 Açık pozisyon: {side} {symbol}', f'Giriş {entry:,.2f} | Güncel {cp:,.2f}', f'Unrealized P&L: {unreal:+,.2f} $', f'SL {active_stop:,.2f} | TP {float(pos["tp"]):,.2f}']
    else:
        lines += ['', '📌 Açık pozisyon: FLAT']
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# PHASE 2 — per-user LIVE (real-money) message builders. These are sent to a
# single user's own linked chat_id (via send_to), never to the shared admin
# channel, and are always about that user's own real Binance orders.
# ---------------------------------------------------------------------------

def live_entry_message(side, symbol, qty, price, leverage, usd_notional):
    side_emoji = '🟢' if side == 'LONG' else '🔴'
    return (
        f'{side_emoji} CANLI (GERÇEK PARA) — YENİ POZİSYON\n\n'
        f'{side} {symbol}\n'
        f'Miktar: {qty}\n'
        f'Yaklaşık giriş: {price:,.2f}\n'
        f'Büyüklük: ~{usd_notional:,.2f} USD | Kaldıraç: {leverage}x\n\n'
        f'Bu gerçek Binance hesabınızda açılan bir işlemdir.'
    )


def live_exit_message(side, symbol, qty, entry_price, exit_price, pnl, reason):
    emoji = '✅' if pnl >= 0 else '❌'
    return (
        f'{emoji} CANLI (GERÇEK PARA) — POZİSYON KAPANDI\n\n'
        f'{side} {symbol}\n'
        f'Miktar: {qty}\n'
        f'Giriş: {entry_price:,.2f} → Çıkış: {exit_price:,.2f}\n'
        f'Tahmini K/Z: {pnl:+,.2f} USD\n'
        f'Neden: {reason}\n\n'
        f'Kesin tutar için Binance hesabınızı kontrol edin — bu bir tahmindir.'
    )


def live_risk_alert(text):
    return f'⚠️ RİSK UYARISI\n\n{text}'


def live_daily_report(username, report_date, trades, realized_pnl_today, open_positions):
    wins = sum(1 for t in trades if float(t.get('pnl', 0)) > 0)
    losses = sum(1 for t in trades if float(t.get('pnl', 0)) < 0)
    day_pnl = sum(float(t.get('pnl', 0)) for t in trades)
    lines = [
        '📊 CANLI İŞLEM — GÜNLÜK ÖZET',
        f'Hesap: {username} | Gün: {report_date}',
        '',
        f'Kapanan işlem: {len(trades)} | {wins}K / {losses}Z | Tahmini K/Z: {day_pnl:+,.2f} USD',
    ]
    if open_positions:
        lines.append('')
        lines.append(f'📌 Şu an açık: {len(open_positions)} pozisyon')
        for sym, p in open_positions.items():
            lines.append(f'  {p.get("side")} {sym} | giriş {float(p.get("entry_price", 0)):,.2f}')
    else:
        lines.append('')
        lines.append('📌 Şu an açık pozisyon yok.')
    lines.append('')
    lines.append('Bu, botun kendi tahmini takibidir — kesin bakiye/K-Z için Binance hesabınızı kontrol edin.')
    return '\n'.join(lines)
