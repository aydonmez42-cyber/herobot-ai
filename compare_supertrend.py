"""
Compares the live Supertrend setting (period=10, multiplier=7.8, from
config.py) against an alternative (period=10, multiplier=6 by default —
override with --period/--multiplier) on both ETH and BTC, using the exact
same backtest engine as backtest_eth.py / backtest_btc.py.

Only cfg.SUPERTREND_MULTIPLIER / cfg.SUPERTREND_PERIOD are changed for the
"new" run; every other parameter (entry scoring, ATR SL/TP/trailing, fees,
slippage) stays identical, so the comparison isolates the effect of this one
change. Temporarily monkeypatches config in-process and restores it after —
does not touch config.py on disk.

Usage (data must already exist — see download_klines.py):
  python download_klines.py --symbol ETHUSDT --interval 4h --start 2019-01-01
  python download_klines.py --symbol BTCUSDT --interval 4h --start 2019-01-01
  python compare_supertrend.py
  python compare_supertrend.py --period 10 --multiplier 6
"""
import argparse

import config as cfg
import backtest_eth
import backtest_btc


def fmt_stats(trades, max_dd, initial_capital, label):
    if trades.empty:
        return {'label': label, 'trades': 0}
    wins = trades[trades.net_pnl > 0]
    losses = trades[trades.net_pnl <= 0]
    gp = wins.net_pnl.sum()
    gl = -losses.net_pnl.sum()
    net = trades.net_pnl.sum()
    return {
        'label': label,
        'trades': len(trades),
        'win_rate': len(wins) / len(trades) * 100,
        'profit_factor': (gp / gl) if gl else float('inf'),
        'net_pnl': net,
        'return_pct': net / initial_capital * 100,
        'max_dd_pct': max_dd * 100,
    }


def print_comparison(title, base, new):
    print(f'\n=== {title} ===')
    if base['trades'] == 0 or new['trades'] == 0:
        print('  (one of the runs produced no trades — check data length)')
        return
    rows = [
        ('Trades', base['trades'], new['trades'], f"{new['trades']-base['trades']:+d}"),
        ('Win rate', f"{base['win_rate']:.1f}%", f"{new['win_rate']:.1f}%", f"{new['win_rate']-base['win_rate']:+.1f}pp"),
        ('Profit factor', f"{base['profit_factor']:.2f}", f"{new['profit_factor']:.2f}", f"{new['profit_factor']-base['profit_factor']:+.2f}"),
        ('Net PnL', f"{base['net_pnl']:,.2f}", f"{new['net_pnl']:,.2f}", f"{new['net_pnl']-base['net_pnl']:+,.2f}"),
        ('Return', f"{base['return_pct']:.1f}%", f"{new['return_pct']:.1f}%", f"{new['return_pct']-base['return_pct']:+.1f}pp"),
        ('Max drawdown', f"{base['max_dd_pct']:.1f}%", f"{new['max_dd_pct']:.1f}%", f"{new['max_dd_pct']-base['max_dd_pct']:+.1f}pp"),
    ]
    print(f"  {'Metric':<15}{'Baseline (10/7.8)':<20}{'New':<20}{'Change'}")
    for name, b, n, d in rows:
        print(f"  {name:<15}{str(b):<20}{str(n):<20}{d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', type=int, default=10, help='new SUPERTREND_PERIOD (default: unchanged, 10)')
    ap.add_argument('--multiplier', type=float, default=6.0, help='new SUPERTREND_MULTIPLIER (default: 6)')
    ap.add_argument('--eth-data', default='data/ETHUSDT_4h.csv')
    ap.add_argument('--btc-data', default='data/BTCUSDT_4h.csv')
    ap.add_argument('--btc-size-usd', type=float, default=1000.0)
    a = ap.parse_args()

    orig_period = cfg.SUPERTREND_PERIOD
    orig_mult = cfg.SUPERTREND_MULTIPLIER
    print(f'Baseline Supertrend: period={orig_period}, multiplier={orig_mult}')
    print(f'New Supertrend     : period={a.period}, multiplier={a.multiplier}')

    # --- ETH ---
    print('\nRunning ETH backtests...')
    cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER = orig_period, orig_mult
    eth_trades_base, _, eth_dd_base = backtest_eth.run(a.eth_data, cfg.INITIAL_CAPITAL)
    cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER = a.period, a.multiplier
    eth_trades_new, _, eth_dd_new = backtest_eth.run(a.eth_data, cfg.INITIAL_CAPITAL)

    # --- BTC ---
    print('Running BTC backtests...')
    cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER = orig_period, orig_mult
    btc_trades_base, _, btc_dd_base = backtest_btc.run(a.btc_data, cfg.INITIAL_CAPITAL, a.btc_size_usd)
    cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER = a.period, a.multiplier
    btc_trades_new, _, btc_dd_new = backtest_btc.run(a.btc_data, cfg.INITIAL_CAPITAL, a.btc_size_usd)

    # restore
    cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER = orig_period, orig_mult

    eth_base = fmt_stats(eth_trades_base, eth_dd_base, cfg.INITIAL_CAPITAL, 'ETH baseline')
    eth_new = fmt_stats(eth_trades_new, eth_dd_new, cfg.INITIAL_CAPITAL, 'ETH new')
    btc_base = fmt_stats(btc_trades_base, btc_dd_base, cfg.INITIAL_CAPITAL, 'BTC baseline')
    btc_new = fmt_stats(btc_trades_new, btc_dd_new, cfg.INITIAL_CAPITAL, 'BTC new')

    print_comparison('ETH', eth_base, eth_new)
    print_comparison('BTC', btc_base, btc_new)


if __name__ == '__main__':
    main()
