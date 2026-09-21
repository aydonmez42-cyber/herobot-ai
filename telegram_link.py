"""
Links each user's own Telegram account to their dashboard account, using the
single shared bot (TELEGRAM_BOT_TOKEN) — no per-user bot needed.

Flow:
  1. User clicks "Bağlantı Kodu Al" in the account panel -> auth.py hands out
     a short-lived 6-character code (POST /api/account/telegram/link-code).
  2. User opens the bot in Telegram and sends "/start <code>".
  3. This module long-polls Telegram's getUpdates for incoming messages,
     matches the code via auth.consume_telegram_link_code(), and stores that
     chat's id on the user's account. From then on, live_trading.py can
     message that user directly via telegram_notifier.send_to(chat_id, ...).

Only needs TELEGRAM_BOT_TOKEN (not TELEGRAM_CHAT_ID — that's the separate,
original admin-only broadcast channel). If no token is configured, this
loop prints one line and returns immediately rather than polling forever.
"""
import json
import os
import time

import requests

import auth
import telegram_notifier as tg

DATA_DIR = os.environ.get('PAPER_DATA_DIR', '')
OFFSET_FILE = os.environ.get(
    'TELEGRAM_OFFSET_FILE',
    os.path.join(DATA_DIR, 'telegram_offset.json') if DATA_DIR else 'telegram_offset.json'
)


def _load_offset():
    try:
        with open(OFFSET_FILE, 'r', encoding='utf-8') as f:
            return int(json.load(f).get('offset', 0))
    except Exception:
        return 0


def _save_offset(offset):
    tmp = OFFSET_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'offset': offset}, f)
    os.replace(tmp, OFFSET_FILE)


def _handle_update(upd):
    msg = upd.get('message') or upd.get('edited_message') or {}
    text = (msg.get('text') or '').strip()
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    tg_username = chat.get('username')
    if not chat_id or not text.startswith('/start'):
        return
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ''
    if not code:
        tg.send_to(chat_id, 'Merhaba! Hesabınızı bağlamak için A&I Trading Terminal panelinden aldığınız kodu şu şekilde gönderin:\n/start KOD')
        return
    username = auth.consume_telegram_link_code(code, chat_id, tg_username)
    if username:
        tg.send_to(chat_id, f'✅ Telegram bağlantısı başarılı! (Hesap: {username})\nArtık canlı işlem bildirimleriniz ve günlük özetiniz buraya gelecek.')
        print(f'TELEGRAM LINK | linked | {username} | chat_id={chat_id}', flush=True)
    else:
        tg.send_to(chat_id, '❌ Kod geçersiz veya süresi dolmuş (10 dakika). Lütfen hesap panelinden yeni bir kod alın.')


def poll_loop():
    if not tg.TELEGRAM_BOT_ENABLED:
        print('TELEGRAM LINK | disabled | TELEGRAM_BOT_TOKEN tanımlı değil', flush=True)
        return
    tg.ensure_bot_username()
    print('TELEGRAM LINK | polling started', flush=True)
    offset = _load_offset()
    while True:
        try:
            r = requests.get(
                f'https://api.telegram.org/bot{tg.TELEGRAM_BOT_TOKEN}/getUpdates',
                params={'timeout': 25, 'offset': offset + 1},
                timeout=35,
            )
            if r.status_code == 200:
                data = r.json()
                for upd in data.get('result', []):
                    offset = max(offset, int(upd.get('update_id', offset)))
                    try:
                        _handle_update(upd)
                    except Exception as e:
                        print(f'TELEGRAM LINK | update handling error | {type(e).__name__}: {e}', flush=True)
                _save_offset(offset)
            else:
                print(f'TELEGRAM LINK | getUpdates HTTP {r.status_code} | {r.text[:200]}', flush=True)
                time.sleep(5)
        except requests.RequestException as e:
            print(f'TELEGRAM LINK | poll error | {type(e).__name__}: {e}', flush=True)
            time.sleep(5)
        except Exception as e:
            print(f'TELEGRAM LINK | poll error | {type(e).__name__}: {e}', flush=True)
            time.sleep(5)
