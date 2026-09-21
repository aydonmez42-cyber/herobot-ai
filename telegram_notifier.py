import html
import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _api(method: str, payload=None, timeout=15):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}'
    return requests.post(url, json=payload or {}, timeout=timeout)


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
    if not TELEGRAM_ENABLED:
        print('TELEGRAM | not configured', flush=True)
        return False
    try:
        # No parse_mode: avoids 400 errors caused by malformed HTML/Markdown.
        r = _api('sendMessage', {'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=15)
        if r.status_code != 200:
            print(f'TELEGRAM | send ERROR | HTTP {r.status_code} | {r.text[:500]}', flush=True)
            return False
        data = r.json()
        if not data.get('ok'):
            print(f'TELEGRAM | send ERROR | {data}', flush=True)
            return False
        print('TELEGRAM | message sent', flush=True)
        return True
    except requests.RequestException as e:
        print(f'TELEGRAM | send ERROR | {type(e).__name__} | {e}', flush=True)
        return False
    except Exception as e:
        print(f'TELEGRAM | send ERROR | {type(e).__name__} | {e}', flush=True)
        return False


def entry_message(p: dict) -> str:
    side_emoji = '🟢' if p['side'] == 'LONG' else '🔴'
    return (
        f'{side_emoji} YENİ POZİSYON\n\n'
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
        f'{emoji} POZİSYON KAPANDI\n\n'
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
        '📊 GÜNLÜK RAPOR',
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
