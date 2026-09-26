"""
Downloads BTC/USD and ETH/USD history from BEFORE Binance's own listing dates
(BTCUSDT perpetual ~2019-09, spot ~2017-08), so the Backtest page can honestly
extend coverage back to ~2016, using two free, no-API-key public sources:

  --source bitstamp   BTC/USD, native 4-hour candles (step=14400 seconds).
                       Bitstamp has traded BTC/USD since 2011, so this should
                       reach back to 2016 with no gaps.
  --source coinbase    ETH/USD, from Coinbase Exchange (formerly GDAX), which
                       listed ETH/USD in May 2016. Coinbase's public candle
                       endpoint has NO native 4-hour granularity (only up to
                       6h/1d), so this pulls 1-hour candles and resamples them
                       to 4h with pandas.

IMPORTANT — I could not test either API from this environment (network here
is restricted the same way it was for Binance). Both are written against each
exchange's publicly documented REST API as of my training, but if the first
run errors out or returns something unexpected, paste the error/response here
and I'll fix the request shape immediately — same as we did for
download_klines.py.

Output: same CSV shape as download_klines.py (timestamp,open,high,low,close,volume),
so it drops straight into backtest_eth.py / backtest_btc.py / splice_with_binance.py.

Usage:
  python download_history_pre2019.py --source bitstamp --pair btcusd --start 2016-01-01 --end 2019-09-08 --out data/BTCUSD_bitstamp_4h.csv
  python download_history_pre2019.py --source coinbase --pair ETH-USD --start 2016-05-01 --end 2019-09-08 --out data/ETHUSD_coinbase_4h.csv
"""
import argparse
import time
from datetime import datetime, timezone

import pandas as pd
import requests

BITSTAMP_STEP_4H = 14400  # seconds; Bitstamp's OHLC endpoint takes step in seconds directly
BITSTAMP_MAX_LIMIT = 1000  # candles per request (documented ceiling)

COINBASE_GRANULARITY_1H = 3600  # seconds; Coinbase Exchange has no native 4h bucket
COINBASE_MAX_CANDLES = 300  # candles per request (documented ceiling)


def to_epoch(date_str):
    return int(datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


def fetch_bitstamp(pair, start_epoch, end_epoch):
    """GET https://www.bitstamp.net/api/v2/ohlc/<pair>/?step=14400&limit=1000&start=..&end=.. """
    url = f'https://www.bitstamp.net/api/v2/ohlc/{pair}/'
    rows = []
    cursor = start_epoch
    span = BITSTAMP_STEP_4H * BITSTAMP_MAX_LIMIT
    while cursor < end_epoch:
        window_end = min(cursor + span, end_epoch)
        params = {'step': BITSTAMP_STEP_4H, 'limit': BITSTAMP_MAX_LIMIT, 'start': cursor, 'end': window_end}
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        ohlc = (body.get('data') or {}).get('ohlc') or []
        if not ohlc:
            cursor = window_end + 1
            continue
        rows.extend(ohlc)
        last_ts = int(ohlc[-1]['timestamp'])
        print(f'  bitstamp: ...{len(rows)} candles, up to {datetime.fromtimestamp(last_ts, tz=timezone.utc):%Y-%m-%d}')
        cursor = last_ts + BITSTAMP_STEP_4H
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = pd.DataFrame(rows)
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='s', utc=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c])
    return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].drop_duplicates('timestamp').sort_values('timestamp')


def fetch_coinbase(product_id, start_epoch, end_epoch):
    """GET https://api.exchange.coinbase.com/products/<product_id>/candles?start=ISO&end=ISO&granularity=3600
    Response rows: [time, low, high, open, close, volume] (note the order!)."""
    url = f'https://api.exchange.coinbase.com/products/{product_id}/candles'
    rows = []
    cursor = start_epoch
    span = COINBASE_GRANULARITY_1H * COINBASE_MAX_CANDLES
    headers = {'User-Agent': 'herobot-ai-backtest/1.0'}
    while cursor < end_epoch:
        window_end = min(cursor + span, end_epoch)
        params = {
            'start': datetime.fromtimestamp(cursor, tz=timezone.utc).isoformat(),
            'end': datetime.fromtimestamp(window_end, tz=timezone.utc).isoformat(),
            'granularity': COINBASE_GRANULARITY_1H,
        }
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            cursor = window_end + 1
            continue
        rows.extend(batch)
        last_ts = max(row[0] for row in batch)
        print(f'  coinbase: ...{len(rows)} candles, up to {datetime.fromtimestamp(last_ts, tz=timezone.utc):%Y-%m-%d}')
        cursor = window_end + 1
        time.sleep(0.4)  # Coinbase's public endpoint is rate-limited (~3 req/s)
    if not rows:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = pd.DataFrame(rows, columns=['time', 'low', 'high', 'open', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].drop_duplicates('timestamp').sort_values('timestamp')
    # Resample 1h -> 4h to match the bot's native interval.
    df = df.set_index('timestamp')
    agg = df.resample('4h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    return agg.dropna(subset=['open']).reset_index()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', choices=['bitstamp', 'coinbase'], required=True)
    ap.add_argument('--pair', required=True, help="bitstamp: e.g. btcusd  |  coinbase: e.g. ETH-USD")
    ap.add_argument('--start', default='2016-01-01')
    ap.add_argument('--end', default='2019-09-08', help='default: just before Binance futures BTCUSDT listing')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    start_epoch, end_epoch = to_epoch(a.start), to_epoch(a.end)
    out = a.out or f'data/{a.pair.upper().replace("-", "")}_{a.source}_4h.csv'

    print(f'Fetching {a.pair} from {a.source} ({a.start} -> {a.end}) ...')
    if a.source == 'bitstamp':
        df = fetch_bitstamp(a.pair.lower(), start_epoch, end_epoch)
    else:
        df = fetch_coinbase(a.pair, start_epoch, end_epoch)

    if df.empty:
        print('No data returned — check the pair name / date range, or paste the error here.')
        return

    import os
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    df.to_csv(out, index=False)
    print(f'Saved {len(df)} candles -> {out}')
    print(f'Range: {df.timestamp.min()}  ->  {df.timestamp.max()}')


if __name__ == '__main__':
    main()
