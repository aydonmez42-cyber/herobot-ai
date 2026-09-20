import pandas as pd
from indicators import atr


def daily_atrp_percentile(daily_df, atr_length=14, percentile_length=365):
    """Return the latest CLOSED 1D ATRP percentile.

    ATRP = ATR / close * 100. The percentile is calculated as the rolling
    percentile rank of the current closed-day ATRP within the prior
    `percentile_length` observations (including the current one).
    """
    df = daily_df.copy()
    df["atr"] = atr(df["high"], df["low"], df["close"], atr_length)
    df["atrp"] = df["atr"] / df["close"] * 100.0

    def pct_rank(x):
        s = pd.Series(x).dropna()
        if s.empty:
            return float("nan")
        return float(s.rank(pct=True).iloc[-1] * 100.0)

    df["atrp_percentile"] = df["atrp"].rolling(
        percentile_length, min_periods=percentile_length
    ).apply(pct_rank, raw=False)
    return df


def is_volatile_daily(daily_df, atr_length=14, percentile_length=365,
                      low_pct=10.0, high_pct=90.0):
    """Evaluate volatility veto using the latest fully closed daily candle."""
    if len(daily_df) < percentile_length + atr_length + 2:
        return False, None, None

    enriched = daily_atrp_percentile(daily_df, atr_length, percentile_length)
    latest = enriched.iloc[-1]
    pct = latest["atrp_percentile"]
    if pd.isna(pct):
        return False, None, None
    return bool(pct > high_pct or pct < low_pct), float(pct), float(latest["atrp"])
