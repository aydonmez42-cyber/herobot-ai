"""
Binance historical kline (candlestick) downloader.

Paginates through Binance's REST API (1500 candles per request, the API's
max) and saves a CSV in the exact format the bot's own backtest scripts
expect: timestamp,open,high,low,close,volume (UTC).

Works for both:
  --market futures  -> fapi.binance.com  (USDT-M perpetuals — what the live
                        bot actually trades against; BTCUSDT perpetual
                        history starts ~2019-09-08)
  --market spot      -> api.binance.com   (spot pairs; BTCUSDT spot history
                        starts ~2017-08-17, ~2 years further back, but is a
                        slightly different price series than the futures
                        contract the bot trades)

You don't need to know the exact first available date: pass an early
--start (e.g. 2017-01-01) and the script will simply get an empty batch for
the period before the symbol existed and start from whenever real data
begins.

Usage:
  python download_klines.py --symbol BTCUSDT --interval 4h --market futures --start 2019-09-01
  python download_klines.py --symbol BTCUSDT --interval 4h --market spot --start 2017-08-01 --out data/BTCUSDT_4h_spot.csv
"""
import argparse
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

FUTURES_URL = 'https://fapi.binance.com/fapi/v1/klines'
SPOT_URL = 'https://api.binance.com/api/v3/klines'

KLINE_COLS = [
    'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
    'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore',
]


def to_ms(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_all(symbol, interval, start_ms, end_ms, market):
    url = FUTURES_URL if market == 'futures' else SPOT_URL
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            'symbol': symbol, 'interval': interval,
            'startTime': cursor, 'endTime': end_ms, 'limit': 1500,
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            # No candles at all in this window — either before listing, or
            # (if rows already collected) we've reached "now".
            if rows:
                break
            # nudge the window forward in case the symbol just wasn't listed
            # yet at `cursor` (rare — Binance usually returns the earliest
            # available candles instead of an empty list, but be safe).
            cursor += 1000 * 60 * 60 * 24 * 30  # +30 days
            continue
        rows.extend(batch)
        last_open = batch[-1][0]
        print(f'  ...{len(rows)} candles so far, up to {datetime.fromtimestamp(last_open/1000, tz=timezone.utc):%Y-%m-%d}')
        if len(batch) < 1500:
            break
        cursor = last_open + 1
        time.sleep(0.25)  # stay comfortably under Binance's rate limit
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--interval', default='4h', help='1m,5m,15m,1h,4h,1d,... (bot default is 4h)')
    ap.add_argument('--market', choices=['futures', 'spot'], default='futures')
    ap.add_argument('--start', default='2019-01-01', help='YYYY-MM-DD (UTC)')
    ap.add_argument('--end', default=None, help='YYYY-MM-DD (UTC), default = now')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    start_ms = to_ms(a.start)
    end_ms = to_ms(a.end) if a.end else int(time.time() * 1000)
    out = a.out or f'data/{a.symbol}_{a.interval}.csv'

    print(f'Downloading {a.symbol} {a.interval} ({a.market}) from {a.start} to {a.end or "now"} ...')
    rows = fetch_all(a.symbol, a.interval, start_ms, end_ms, a.market)
    if not rows:
        print('No data returned at all — check the symbol name / market.')
        return

    df = pd.DataFrame(rows, columns=KLINE_COLS)
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c])
    df = (df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
          .drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True))

    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    df.to_csv(out, index=False)
    print(f'Saved {len(df)} candles -> {out}')
    print(f'Range: {df.timestamp.min()}  ->  {df.timestamp.max()}')


if __name__ == '__main__':
    main()
