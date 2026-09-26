"""
Joins an older, pre-Binance source (Bitstamp for BTC, Coinbase for ETH) with
the existing Binance Futures data (2019+), into one continuous 4h CSV usable
by backtest_eth.py / backtest_btc.py.

The two series come from different venues (spot exchange vs. Binance USDT-M
perpetual futures), so prices can differ slightly right at the seam — this
script does NOT hide that. It prints the exact splice date/time and the price
gap at that seam, and writes a `source` column into the output CSV so anyone
(including the Backtest page, and a curious investor) can see exactly where
one dataset ends and the other begins.

Usage:
  python download_history_pre2019.py --source bitstamp --pair btcusd --start 2016-01-01 --end 2019-09-08
  python download_klines.py --symbol BTCUSDT --interval 4h --start 2019-09-01
  python splice_history.py --old data/BTCUSDBTCUSD_bitstamp_4h.csv --new data/BTCUSDT_4h.csv --out data/BTCUSDT_extended_4h.csv --label BTC
"""
import argparse

import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='pre-2019 CSV (Bitstamp/Coinbase)')
    ap.add_argument('--new', required=True, help='2019+ CSV (Binance)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--label', default='')
    a = ap.parse_args()

    old = pd.read_csv(a.old, parse_dates=['timestamp'])
    new = pd.read_csv(a.new, parse_dates=['timestamp'])
    old['timestamp'] = pd.to_datetime(old['timestamp'], utc=True)
    new['timestamp'] = pd.to_datetime(new['timestamp'], utc=True)

    splice_at = new['timestamp'].min()
    old_before = old[old['timestamp'] < splice_at].copy()
    if old_before.empty:
        raise SystemExit(f'Old dataset has no candles before {splice_at} — nothing to splice; check --old covers earlier dates than --new.')

    old_before['source'] = 'pre2019'
    new = new.copy()
    new['source'] = 'binance'

    last_old_close = old_before.iloc[-1]['close']
    first_new_open = new.iloc[0]['open']
    gap_pct = (first_new_open - last_old_close) / last_old_close * 100

    combined = pd.concat([old_before, new], ignore_index=True).sort_values('timestamp').reset_index(drop=True)
    combined.to_csv(a.out, index=False)

    label = f'[{a.label}] ' if a.label else ''
    print(f'{label}Splice point: {splice_at}')
    print(f'{label}Last pre-2019 close: {last_old_close:.4f}   First Binance open: {first_new_open:.4f}   Gap: {gap_pct:+.2f}%')
    print(f'{label}Pre-2019 candles: {len(old_before)} ({old_before.timestamp.min()} -> {old_before.timestamp.max()})')
    print(f'{label}Binance candles : {len(new)} ({new.timestamp.min()} -> {new.timestamp.max()})')
    print(f'{label}Combined total  : {len(combined)} candles -> {a.out}')
    if abs(gap_pct) > 3:
        print(f'{label}WARNING: seam gap is {gap_pct:+.2f}% — larger than a typical cross-venue spread. Worth a manual look before using this for headline numbers.')


if __name__ == '__main__':
    main()
