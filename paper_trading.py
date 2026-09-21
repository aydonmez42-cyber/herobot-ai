import json, os, time
from datetime import datetime, timezone
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
import live_trading
import threading

POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '30'))
STARTING_EQUITY = float(os.environ.get('PAPER_INITIAL_CAPITAL', str(cfg.INITIAL_CAPITAL)))

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
        live_trading.on_entry_signal(cfg.SIGNAL_SYMBOL, side, price, source='eth_bot')
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
    sized in USD notional (cfg.WATCHLIST_POSITION_USD) instead of a fixed
    coin quantity, since watchlist symbols can have wildly different prices.

    live_eligible=True only for Binance-tradable crypto watchlist symbols
    (see run_watchlist_symbol); US-stock/BIST watchlist symbols are never
    live-tradable through this bot and must never reach live_trading.py."""
    atr_val = float(signal_row['atr'])
    if side == 'LONG':
        sl = price - cfg.ATR_SL_MULTIPLIER * atr_val
        tp = price + cfg.ATR_LONG_TP_MULTIPLIER * atr_val
    else:
        sl = price + cfg.ATR_SHORT_SL_MULTIPLIER * atr_val
        tp = price - cfg.ATR_SHORT_TP_MULTIPLIER * atr_val
    qty = (cfg.WATCHLIST_POSITION_USD / price) if price else 0.0
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
            live_trading.on_entry_signal(symbol, side, price, source='watchlist')
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
    save_state(state)
    if live_eligible:
        try:
            live_trading.on_exit_signal(symbol, side, price, reason, source='watchlist')
        except Exception as e:
            print(f'LIVE | ERROR | {type(e).__name__}: {e}', flush=True)


def process_intrabar_symbol(state, symbol, candle, now, live_eligible=False):
    p = state.get('positions', {}).get(symbol)
    if not p:
        return False
    high, low = float(candle['high']), float(candle['low'])
    entry, atr_val = p['entry_price'], p['atr']
    if p['side'] == 'LONG':
        stop = p['trail_stop'] if p['trail_active'] and p['trail_stop'] is not None else p['sl']
        if low <= stop:
            exit_symbol_position(state, symbol, stop, 'ATR_TRAILING_SL' if p['trail_active'] else 'ATR_SL', now, live_eligible=live_eligible)
            return True
        if cfg.USE_ATR_TP and high >= p['tp']:
            exit_symbol_position(state, symbol, p['tp'], 'ATR_TP', now, live_eligible=live_eligible)
            return True
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
        if high >= stop:
            exit_symbol_position(state, symbol, stop, 'ATR_TRAILING_SL' if p['trail_active'] else 'ATR_SL', now, live_eligible=live_eligible)
            return True
        if cfg.USE_ATR_TP and low <= p['tp']:
            exit_symbol_position(state, symbol, p['tp'], 'ATR_TP', now, live_eligible=live_eligible)
            return True
        if cfg.USE_ATR_TRAILING and low <= entry - cfg.ATR_TRAIL_ACTIVATION * atr_val:
            if not p['trail_active']:
                p['trail_active'] = True
                p['lowest_low'] = low
                p['trail_stop'] = low + cfg.ATR_TRAIL_MULTIPLIER * atr_val
            else:
                p['lowest_low'] = min(p['lowest_low'], low)
                p['trail_stop'] = min(p['trail_stop'], p['lowest_low'] + cfg.ATR_TRAIL_MULTIPLIER * atr_val)
    save_state(state)
    return False


def run_watchlist_symbol(state, symbol, now):
    """Fetch data, refresh the signal snapshot, and manage a paper position for
    one manually-added crypto watchlist symbol using the exact same strategy
    and indicators as the main ETH engine (USD-M Binance Futures klines)."""
    df = fetch(symbol, 'USD_M', 300)
    daily_df = fetch(symbol, 'USD_M', 400, interval='1d')
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

    last_map = state.setdefault('symbol_last_closed', {})
    last_ts = pd.Timestamp(last_map.get(symbol)) if last_map.get(symbol) else None
    if last_ts is None or latest_closed_time > last_ts:
        last_map[symbol] = latest_closed_time.isoformat()
        if symbol not in state.get('positions', {}):
            if blocked:
                go_long = go_short = False
            if go_long and not go_short:
                px = exec_price(float(df.iloc[-1]['open']), 'BUY')
                enter_symbol(state, symbol, 'LONG', px, latest, now, live_eligible=True)
            elif go_short and not go_long:
                px = exec_price(float(df.iloc[-1]['open']), 'SELL')
                enter_symbol(state, symbol, 'SHORT', px, latest, now, live_eligible=True)
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
            signal_df = fetch(cfg.SIGNAL_SYMBOL, 'USD_M', 300)
            long_df = fetch(cfg.LONG_SYMBOL, 'COIN_M', 100)
            short_df = fetch(cfg.SHORT_SYMBOL, 'USD_M', 100)
            # Daily volatility veto uses only the latest fully closed 1D candle.
            daily_df = fetch(cfg.SIGNAL_SYMBOL, 'USD_M', 400, interval='1d')
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
                    if go_long and not go_short:
                        px = exec_price(float(long_df.iloc[-1]['open']), 'BUY')
                        enter(state, 'LONG', px, latest, now)
                    elif go_short and not go_long:
                        px = exec_price(float(short_df.iloc[-1]['open']), 'SELL')
                        enter(state, 'SHORT', px, latest, now)
                    else:
                        print(f"CANDLE {latest_closed_time} | no signal", flush=True)
                else:
                    print(f"CANDLE {latest_closed_time} | position already open: {state['position']['side']}", flush=True)
                save_state(state)

            # Manually-added watchlist symbols (from the scanner's "+ Ekle" button).
            # Crypto and US stock symbols each get a full, independent paper
            # position using the same strategy; BIST symbols are signal-only
            # (bist_scanner never trades).
            for wsym, w in list(state.get('watchlist', {}).items()):
                market = w.get('market')
                try:
                    if market == 'crypto':
                        run_watchlist_symbol(state, wsym, now)
                    elif market == 'us_stock':
                        run_us_stock_watchlist_symbol(state, wsym, now)
                except Exception as e:
                    print(f'WATCHLIST ERROR | {wsym} | {type(e).__name__}: {e}', flush=True)
            sync_bist_watchlist_signals(state, now)

            time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f'ERROR | {type(e).__name__}: {e}', flush=True)
            time.sleep(POLL_SECONDS)

if __name__ == '__main__':
    main()
