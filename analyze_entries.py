"""Which entry features go with stop-outs? Usage:
    python analyze_entries.py closed_trades.csv entry_context.csv
Both files come from the dashboard (/api/closed-trades.csv and /api/entry-context.csv). Trades are
joined on symbol + entry_time; only trades opened after entry logging started have features.
For each feature the trades are split into 3 equal groups (low / mid / high) and the stop-out rate,
average net PnL and trade count of each group are shown. With fewer than ~60 joined trades the
differences are mostly noise - collect more before changing a limit."""
import sys
import pandas as pd

FEATURES = ['chg24_pct', 'rsi', 'ema_dist_atr', 'st_dist_atr', 'range24_pos', 'pullback_atr',
            'atr15_pct', 'atr4h_pct', 'entries_last_hour', 'hour_utc']


def main(trades_csv, ctx_csv):
    t = pd.read_csv(trades_csv, encoding='utf-8-sig')
    c = pd.read_csv(ctx_csv, encoding='utf-8-sig')
    for df in (t, c):
        df['entry_time'] = pd.to_datetime(df['entry_time'], utc=True, format='mixed').dt.floor('s')
    d = t.merge(c.drop(columns=['side'], errors='ignore'), on=['symbol', 'entry_time'], how='inner', suffixes=('', '_ctx'))
    print(f'{len(t)} kapanan işlem, {len(c)} giriş kaydı, {len(d)} eşleşti')
    if len(d) < 10:
        print('Eşleşen işlem çok az; birkaç gün daha bekleyin.')
        return
    d['stop'] = d['reason'].isin(['ATR_SL', 'TREND_SL']).astype(int)
    d['win'] = (d['net_pnl'] > 0).astype(int)
    print(f"Genel: stop oranı %{d['stop'].mean()*100:.0f}, kazanma %{d['win'].mean()*100:.0f}, "
          f"ort. net {d['net_pnl'].mean():.1f}, toplam {d['net_pnl'].sum():.0f}")
    for f in FEATURES:
        if f not in d or d[f].notna().sum() < 9 or d[f].nunique() < 3:
            continue
        try:
            d['_g'] = pd.qcut(d[f], 3, labels=['düşük', 'orta', 'yüksek'], duplicates='drop')
        except ValueError:
            continue
        g = d.groupby('_g', observed=True).agg(n=('net_pnl', 'size'), aralık_min=(f, 'min'), aralık_max=(f, 'max'),
                                              stop_oranı=('stop', 'mean'), kazanma=('win', 'mean'),
                                              ort_net=('net_pnl', 'mean'), toplam=('net_pnl', 'sum'))
        print(f'\n== {f} ==')
        print(g.round(2).to_string())


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
