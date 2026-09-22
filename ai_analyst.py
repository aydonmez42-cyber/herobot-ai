"""
AI Trade Analyst — a periodic, READ-ONLY report written by an LLM (Anthropic
Claude) about the bot's own recent closed-trade history.

Hard boundary, by design: this module never opens, closes, sizes or manages
a position, and it never reads or changes any value in config.py that the
trading engine uses. It only reads paper_trades.csv / the state file and
writes a text report back into state (and, optionally, Telegram). Every
public entry point below is wrapped so a failure here (missing API key,
network error, malformed response) can never take down the trading loop —
the bot must keep trading exactly as before even if this feature is fully
broken or disabled.
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import requests

import config as cfg
from telegram_notifier import send_message
from state_store import TRADES_FILE

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '').strip()
ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'

# Soft-disabled without an API key, regardless of the config.py toggle.
AI_ANALYST_ENABLED = bool(ANTHROPIC_API_KEY) and bool(getattr(cfg, 'AI_ANALYST_ENABLED', True))


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _starting_equity():
    return float(os.environ.get('PAPER_INITIAL_CAPITAL', str(cfg.INITIAL_CAPITAL)))


def build_summary(trades_df, state, lookback=40):
    """Compact, numeric summary of recent closed trades — never a raw row
    dump — so the prompt stays small and the model reasons about aggregate
    patterns rather than picking one anecdote to focus on."""
    if trades_df is None or trades_df.empty:
        return None

    df = trades_df.copy()
    for c in ['entry_price', 'exit_price', 'net_pnl', 'gross_pnl', 'fees', 'equity_after', 'qty_eth', 'atr']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['net_pnl'])
    if df.empty:
        return None

    recent = df.tail(lookback)

    def side_stats(sub):
        if sub is None or sub.empty:
            return {'n': 0, 'win_rate': None, 'profit_factor': None, 'total_pnl': 0.0}
        wins = sub[sub.net_pnl > 0]
        losses = sub[sub.net_pnl < 0]
        gp = wins.net_pnl.sum()
        gl = abs(losses.net_pnl.sum())
        return {
            'n': int(len(sub)),
            'win_rate': round(len(wins) / len(sub) * 100, 1),
            'profit_factor': round(gp / gl, 2) if gl else None,
            'total_pnl': round(float(sub.net_pnl.sum()), 2),
        }

    by_reason = {}
    if 'reason' in df.columns:
        for reason, sub in recent.groupby('reason'):
            by_reason[str(reason)] = {
                'n': int(len(sub)),
                'avg_pnl': round(float(sub.net_pnl.mean()), 2),
                'total_pnl': round(float(sub.net_pnl.sum()), 2),
            }

    # Current win/loss streak within the recent window, most-recent-first.
    streak_type, streak_len = None, 0
    for pnl in recent.net_pnl.iloc[::-1]:
        is_win = pnl > 0
        if streak_type is None:
            streak_type = 'win' if is_win else 'loss'
            streak_len = 1
        elif (streak_type == 'win') == is_win:
            streak_len += 1
        else:
            break

    equity_start = _starting_equity()
    equity_now = _f(state.get('equity'), equity_start)

    recent_list = []
    for _, t in recent.tail(15).iterrows():
        recent_list.append({
            'side': t.get('side'), 'symbol': t.get('symbol'),
            'net_pnl': round(_f(t.get('net_pnl')), 2),
            'reason': t.get('reason'),
            'entry_time': str(t.get('entry_time', ''))[:16],
            'exit_time': str(t.get('exit_time', ''))[:16],
        })

    return {
        'total_trades_all_time': int(len(df)),
        'trades_in_window': int(len(recent)),
        'window_size': lookback,
        'overall': side_stats(df),
        'window_overall': side_stats(recent),
        'window_long': side_stats(recent[recent.side == 'LONG']) if 'side' in recent.columns else None,
        'window_short': side_stats(recent[recent.side == 'SHORT']) if 'side' in recent.columns else None,
        'by_exit_reason_in_window': by_reason,
        'current_streak': {'type': streak_type, 'length': streak_len} if streak_type else None,
        'equity_now': round(equity_now, 2),
        'equity_start': round(equity_start, 2),
        'return_pct': round((equity_now / equity_start - 1) * 100, 2) if equity_start else None,
        'recent_trades': recent_list,
    }


SYSTEM_PROMPT = (
    "Sen otonom çalışan bir kripto/BIST trading botunun geçmiş işlemlerini "
    "inceleyen bir performans analistisin. Bot, sabit kurallara (EMA, "
    "Supertrend, ADX, RSI, CCI, MACD, Stoch RSI'dan oluşan bir puanlama "
    "sistemi ve ATR tabanlı stop/take-profit/trailing) göre otomatik işlem "
    "açıp kapatıyor; kararları SEN vermiyorsun ve bu raporun hiçbir sonucu "
    "botun canlı davranışını otomatik olarak değiştirmiyor — sadece "
    "kullanıcının okuyacağı bir özet yazıyorsun.\n\n"
    "Sana bot performansının özet istatistikleri JSON olarak verilecek. "
    "Görevin:\n"
    "1. Genel görünümü 2-3 cümleyle özetle (kazanma oranı, profit factor, "
    "getiri, mevcut kazanma/kayıp serisi).\n"
    "2. Dikkat çeken, veriyle desteklenmiş 2-4 kalıp/gözlem belirt (ör. bir "
    "tarafın diğerinden zayıf performans göstermesi, belirli bir çıkış "
    "türünün ağırlıklı olması, art arda kayıplar, vb). Sadece verilen "
    "sayılara dayan, veri dışı varsayımda bulunma.\n"
    "3. Kullanıcının bir sonraki gözden geçirmede bakabileceği 1-3 somut "
    "soru/nokta öner.\n\n"
    "KESİN SINIRLAR:\n"
    "- Asla somut bir parametre/eşik değişikliği önerme (örn. 'ADX eşiğini "
    "X yap' deme) — sadece gözlem paylaş, karar kullanıcıya ait.\n"
    "- Asla yeni bir trade veya giriş/çıkış tavsiyesi verme.\n"
    "- Yatırım tavsiyesi verme; bu yalnızca geçmiş performansın açıklamalı "
    "bir özeti.\n"
    "- Örneklem küçükse (ör. pencerede 10'dan az işlem) bunu açıkça belirt "
    "ve kesin sonuçtan kaçın.\n"
    "- Türkçe, düz metin yaz (Telegram'a düz metin olarak gönderiliyor — "
    "markdown başlık/kalın/tire işaretleri kullanma), 250-400 kelime."
)


def _call_anthropic(summary):
    if not ANTHROPIC_API_KEY:
        return None
    payload = {
        'model': getattr(cfg, 'AI_ANALYST_MODEL', 'claude-sonnet-5'),
        'max_tokens': 900,
        'system': SYSTEM_PROMPT,
        'messages': [
            {'role': 'user', 'content': json.dumps(summary, ensure_ascii=False)}
        ],
    }
    headers = {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': ANTHROPIC_VERSION,
        'content-type': 'application/json',
    }
    try:
        r = requests.post(ANTHROPIC_API_URL, json=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            print(f'AI_ANALYST | API ERROR | HTTP {r.status_code} | {r.text[:400]}', flush=True)
            return None
        data = r.json()
        parts = data.get('content', [])
        text = ''.join(p.get('text', '') for p in parts if p.get('type') == 'text').strip()
        return text or None
    except requests.RequestException as e:
        print(f'AI_ANALYST | request ERROR | {type(e).__name__} | {e}', flush=True)
        return None
    except Exception as e:
        print(f'AI_ANALYST | ERROR | {type(e).__name__} | {e}', flush=True)
        return None


def run_analysis(state, trades_df, now=None, notify_telegram=True):
    """Build the summary, call the LLM, and return a result dict. Does NOT
    persist to state itself — callers (the main loop vs. the dashboard's
    manual trigger) save through different, non-interchangeable paths."""
    now = now or datetime.now(timezone.utc)
    if not AI_ANALYST_ENABLED:
        return {'ok': False, 'error': 'AI Analist devre dışı (ANTHROPIC_API_KEY tanımlı değil veya AI_ANALYST_ENABLED=False)'}

    lookback = getattr(cfg, 'AI_ANALYST_LOOKBACK_TRADES', 40)
    summary = build_summary(trades_df, state, lookback=lookback)
    if summary is None:
        return {'ok': False, 'error': 'Analiz için yeterli kapanmış işlem yok'}

    text = _call_anthropic(summary)
    if not text:
        return {'ok': False, 'error': 'LLM çağrısı başarısız oldu (bot loglarına bakın)'}

    result = {
        'ok': True,
        'text': text,
        'generated_at': now.isoformat(),
        'trades_analyzed': summary['trades_in_window'],
        'total_trades_all_time': summary['total_trades_all_time'],
    }
    if notify_telegram:
        send_message('🤖 AI TRADE ANALİZİ\n\n' + text)
    return result


def maybe_run_analysis(state, now):
    """Gate function for the main bot loop: only actually calls the LLM when
    enough time has passed AND enough new trades have closed since the last
    run, so a low-frequency strategy doesn't get a near-identical report
    every cycle. Mutates `state` in place and saves it (mirrors how the
    daily_report block elsewhere in paper_trading.py already handles its own
    save). Never raises."""
    try:
        if not AI_ANALYST_ENABLED:
            return
        min_hours = getattr(cfg, 'AI_ANALYST_MIN_INTERVAL_HOURS', 168)
        min_new_trades = getattr(cfg, 'AI_ANALYST_MIN_NEW_TRADES', 3)

        last_run = state.get('ai_analyst_last_run') or {}
        last_at = last_run.get('at')
        last_total_trades = int(last_run.get('total_trades', 0))

        if not os.path.exists(TRADES_FILE):
            return
        trades_df = pd.read_csv(TRADES_FILE)
        total_trades = len(trades_df)
        new_trades = total_trades - last_total_trades

        if last_at:
            try:
                elapsed_hours = (now - datetime.fromisoformat(last_at)).total_seconds() / 3600
            except (TypeError, ValueError):
                elapsed_hours = min_hours  # malformed timestamp: don't block forever
            if elapsed_hours < min_hours:
                return
        if new_trades < min_new_trades:
            return

        from state_store import save_state
        result = run_analysis(state, trades_df, now=now, notify_telegram=True)
        state['ai_analyst_last_run'] = {'at': now.isoformat(), 'total_trades': total_trades}
        if result.get('ok'):
            state['ai_analysis'] = result
            print(f"AI_ANALYST | analysis sent | {result['trades_analyzed']} işlem incelendi", flush=True)
        else:
            print(f"AI_ANALYST | skipped/failed | {result.get('error')}", flush=True)
        save_state(state)
    except Exception as e:
        print(f'AI_ANALYST | maybe_run_analysis ERROR | {type(e).__name__}: {e}', flush=True)


def run_now():
    """Manual trigger (dashboard's 'Şimdi Analiz Et' button). Ignores the
    interval/new-trade gating and always attempts a fresh analysis,
    persisting the result through state_store's lock-protected update_state
    so it can't race with the main bot loop's own state writes.

    On failure the error is ALSO persisted (as 'ai_analyst_last_error'),
    separately from 'ai_analysis' (which only ever holds the last
    *successful* report). Before this, a failed manual run vanished
    silently — the button would spin and revert with the panel completely
    unchanged, giving no indication anything had even been attempted."""
    from state_store import load_state as _ss_load, update_state as _ss_update

    starting_equity = _starting_equity()
    state = _ss_load(starting_equity)
    if not os.path.exists(TRADES_FILE):
        result = {'ok': False, 'error': 'Henüz kapanmış işlem yok'}
    else:
        trades_df = pd.read_csv(TRADES_FILE)
        now = datetime.now(timezone.utc)
        result = run_analysis(state, trades_df, now=now, notify_telegram=True)

    def m(s):
        s['ai_analyst_last_run'] = {'at': datetime.now(timezone.utc).isoformat(),
                                     'total_trades': len(trades_df) if os.path.exists(TRADES_FILE) else 0}
        if result.get('ok'):
            s['ai_analysis'] = result
            s['ai_analyst_last_error'] = None
        else:
            s['ai_analyst_last_error'] = {'at': datetime.now(timezone.utc).isoformat(), 'error': result.get('error')}
    _ss_update(m, starting_equity)
    return result


def get_status():
    """Read-only snapshot for the dashboard's /api/ai-analysis endpoint."""
    from state_store import load_state as _ss_load
    state = _ss_load(_starting_equity())
    return {
        'enabled': AI_ANALYST_ENABLED,
        'analysis': state.get('ai_analysis'),
        'last_run': state.get('ai_analyst_last_run'),
        'last_error': state.get('ai_analyst_last_error'),
        'min_interval_hours': getattr(cfg, 'AI_ANALYST_MIN_INTERVAL_HOURS', 168),
        'min_new_trades': getattr(cfg, 'AI_ANALYST_MIN_NEW_TRADES', 3),
    }
