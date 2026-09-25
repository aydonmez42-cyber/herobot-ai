"""
Single-symbol backtest that mirrors paper_trading.py's live execution logic
as closely as possible:
  - same entry scoring: strategy.long_signal() / short_signal() against the
    live config.py parameters (EMA/ADX/Supertrend/RSI/MACD/CCI/StochRSI)
  - same exit logic: ATR stop-loss, ATR take-profit, ATR trailing stop
    (activates at cfg.ATR_TRAIL_ACTIVATION, trails at cfg.ATR_TRAIL_MULTIPLIER)
  - same fee (cfg.FEE_RATE) and slippage (cfg.SLIPPAGE_RATE) assumptions
  - signal evaluated on a closed candle, executed at the *next* candle's open
    (no lookahead), exactly like paper_trading.py / multicoin_backtest.py

NOTE: this repo's old multicoin_backtest.py imports `add_indicators(d)` with
one argument and `from strategy import signal`, which no longer match the
current indicators.py (add_indicators(df, cfg)) / strategy.py (long_signal /
short_signal, no plain `signal`) — it's stale and would raise a NameError/
TypeError if run today. This script uses the *current* strategy engine.

Usage:
  1) Get data first (see download_klines.py):
       python download_klines.py --symbol BTCUSDT --interval 4h --start 2019-01-01
  2) Run the backtest:
       python backtest_btc.py --data data/BTCUSDT_4h.csv
       python backtest_btc.py --data data/BTCUSDT_4h.csv --size-usd 2000 --capital 10000
"""
import argparse

import pandas as pd

import config as cfg
from indicators import add_indicators
from strategy import long_signal, short_signal

WARMUP_BARS = 251  # lets EMA100/ADX/Supertrend/etc settle, same as before


def run(path, initial_capital, position_size_usd):
    d = pd.read_csv(path)
    d.columns = [c.lower() for c in d.columns]
    d['timestamp'] = pd.to_datetime(d['timestamp'], utc=True)
    d = d.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
    if len(d) < WARMUP_BARS + 10:
        raise SystemExit(f'Not enough candles ({len(d)}) — need at least {WARMUP_BARS + 10}.')
    d = add_indicators(d, cfg)

    equity = initial_capital
    pos = None
    peak = equity
    max_dd = 0.0
    trades = []
    equity_curve = [(d.iloc[WARMUP_BARS]['timestamp'], equity)]

    for i in range(WARMUP_BARS, len(d) - 1):
        row = d.iloc[i]

        if pos is not None:
            side, entry, atr = pos['side'], pos['entry'], pos['atr']
            xp, reason = None, None
            if side == 'LONG':
                stop = pos['trail_stop'] if pos['trail_active'] else pos['sl']
                if row['low'] <= stop:
                    xp, reason = stop, ('ATR_TRAILING_SL' if pos['trail_active'] else 'ATR_SL')
                elif cfg.USE_ATR_TP and row['high'] >= pos['tp']:
                    xp, reason = pos['tp'], 'ATR_TP'
                elif cfg.USE_ATR_TRAILING and row['high'] >= entry + cfg.ATR_TRAIL_ACTIVATION * atr:
                    if not pos['trail_active']:
                        pos['trail_active'] = True
                        pos['extreme'] = row['high']
                        pos['trail_stop'] = row['high'] - cfg.ATR_TRAIL_MULTIPLIER * atr
                    else:
                        pos['extreme'] = max(pos['extreme'], row['high'])
                        pos['trail_stop'] = max(pos['trail_stop'], pos['extreme'] - cfg.ATR_TRAIL_MULTIPLIER * atr)
            else:  # SHORT
                stop = pos['trail_stop'] if pos['trail_active'] else pos['sl']
                if row['high'] >= stop:
                    xp, reason = stop, ('ATR_TRAILING_SL' if pos['trail_active'] else 'ATR_SL')
                elif cfg.USE_ATR_TP and row['low'] <= pos['tp']:
                    xp, reason = pos['tp'], 'ATR_TP'
                elif cfg.USE_ATR_TRAILING and row['low'] <= entry - cfg.ATR_TRAIL_ACTIVATION * atr:
                    if not pos['trail_active']:
                        pos['trail_active'] = True
                        pos['extreme'] = row['low']
                        pos['trail_stop'] = row['low'] + cfg.ATR_TRAIL_MULTIPLIER * atr
                    else:
                        pos['extreme'] = min(pos['extreme'], row['low'])
                        pos['trail_stop'] = min(pos['trail_stop'], pos['extreme'] + cfg.ATR_TRAIL_MULTIPLIER * atr)

            if xp is not None:
                exit_price = xp * (1 - cfg.SLIPPAGE_RATE) if side == 'LONG' else xp * (1 + cfg.SLIPPAGE_RATE)
                gross = (exit_price - entry) * pos['qty'] if side == 'LONG' else (entry - exit_price) * pos['qty']
                exit_fee = abs(exit_price * pos['qty']) * cfg.FEE_RATE
                net = gross - exit_fee - pos['entry_fee']
                equity += gross - exit_fee
                trades.append({
                    'entry_time': pos['entry_time'], 'exit_time': row['timestamp'], 'side': side,
                    'entry': entry, 'exit': exit_price, 'atr': atr, 'reason': reason,
                    'gross_pnl': gross, 'fees': exit_fee + pos['entry_fee'], 'net_pnl': net,
                    'equity_after': equity,
                })
                pos = None

        if pos is None:
            side = 'LONG' if long_signal(d, i, cfg) else ('SHORT' if short_signal(d, i, cfg) else None)
            if side:
                nxt = d.iloc[i + 1]
                raw_entry = nxt['open']
                entry = raw_entry * (1 + cfg.SLIPPAGE_RATE) if side == 'LONG' else raw_entry * (1 - cfg.SLIPPAGE_RATE)
                atr = row['atr']
                qty = position_size_usd / entry if entry else 0.0
                sl = entry - cfg.ATR_SL_MULTIPLIER * atr if side == 'LONG' else entry + cfg.ATR_SHORT_SL_MULTIPLIER * atr
                tp = entry + cfg.ATR_LONG_TP_MULTIPLIER * atr if side == 'LONG' else entry - cfg.ATR_SHORT_TP_MULTIPLIER * atr
                entry_fee = abs(entry * qty) * cfg.FEE_RATE
                equity -= entry_fee
                pos = {
                    'side': side, 'entry': entry, 'entry_time': nxt['timestamp'], 'atr': atr, 'qty': qty,
                    'sl': sl, 'tp': tp, 'trail_active': False, 'trail_stop': None, 'entry_fee': entry_fee,
                }

        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak - 1)
        equity_curve.append((row['timestamp'], equity))

    return pd.DataFrame(trades), pd.DataFrame(equity_curve, columns=['timestamp', 'equity']), max_dd


def summarize(trades, equity_curve, max_dd, initial_capital):
    start_t, end_t = equity_curve.timestamp.iloc[0], equity_curve.timestamp.iloc[-1]
    years = (end_t - start_t).days / 365.25
    print(f'Period         : {start_t:%Y-%m-%d}  ->  {end_t:%Y-%m-%d}  (~{years:.1f} years)')
    if trades.empty:
        print('No trades were generated over this period (check data length / config thresholds).')
        return
    wins = trades[trades.net_pnl > 0]
    losses = trades[trades.net_pnl <= 0]
    gp = wins.net_pnl.sum()
    gl = -losses.net_pnl.sum()
    net = trades.net_pnl.sum()
    final_equity = initial_capital + net
    print(f'Trades         : {len(trades)}  ({len(wins)} win / {len(losses)} loss)')
    print(f'Win rate       : {len(wins) / len(trades) * 100:.1f}%')
    print(f'Profit factor  : {(gp / gl):.2f}' if gl else 'Profit factor  : inf (no losing trades)')
    print(f'Net PnL        : {net:,.2f} USD')
    print(f'Return         : {net / initial_capital * 100:.1f}%  ({initial_capital:,.0f} -> {final_equity:,.0f})')
    print(f'Max drawdown   : {max_dd * 100:.1f}%')
    by_reason = trades.groupby('reason').net_pnl.agg(['count', 'sum'])
    print('\nExits by reason:')
    print(by_reason.to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='data/BTCUSDT_4h.csv')
    ap.add_argument('--capital', type=float, default=None, help='starting equity, default = config.INITIAL_CAPITAL')
    ap.add_argument('--size-usd', type=float, default=1000.0, help='USD notional per trade (BTC has no fixed-qty config like ETH does)')
    ap.add_argument('--trades-out', default='backtest_btc_trades.csv')
    ap.add_argument('--equity-out', default='backtest_btc_equity.csv')
    a = ap.parse_args()

    initial_capital = a.capital if a.capital is not None else cfg.INITIAL_CAPITAL
    trades, equity_curve, max_dd = run(a.data, initial_capital, a.size_usd)
    summarize(trades, equity_curve, max_dd, initial_capital)

    if not trades.empty:
        trades.to_csv(a.trades_out, index=False)
        equity_curve.to_csv(a.equity_out, index=False)
        print(f'\nTrades -> {a.trades_out}')
        print(f'Equity curve -> {a.equity_out}')


if __name__ == '__main__':
    main()
