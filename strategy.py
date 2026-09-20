import pandas as pd

def recent_true(series, current_pos, max_bars):
    start = max(0, current_pos - max_bars + 1)
    window = series.iloc[start:current_pos + 1]
    return bool(window.fillna(False).any())


def long_conditions(df, i, cfg):
    """The 8 scored LONG confluence conditions, as (name, bool) pairs.
    None of these alone is required anymore — see long_signal()."""
    row = df.iloc[i]
    return [
        ("close_above_ema100", bool(row["close"] > row["ema100"])),
        ("ema50_above_ema100", bool(row["ema50"] > row["ema100"])),
        ("adx_trending", bool(row["adx"] > cfg.ADX_THRESHOLD)),
        ("supertrend_bullish", bool(row["supertrend_bullish"])),
        ("rsi_momentum", bool(row["rsi"] > cfg.RSI_LONG_THRESHOLD)),
        ("macd_bullish", bool(row["macd_long_ok"])),
        ("cci_breakout_recent", recent_true(df["cci"] > cfg.CCI_LONG_THRESHOLD, i, cfg.CCI_VALID_BARS)),
        ("stoch_cross_recent", recent_true(df["stoch_bull_cross"], i, cfg.STOCH_VALID_BARS) and bool(row["stoch_d"] > cfg.STOCH_LONG_D_THRESHOLD)),
    ]


def short_conditions(df, i, cfg):
    """The 8 scored SHORT confluence conditions, mirroring long_conditions().
    bb_reentry counts as automatically satisfied while USE_BB_SHORT_FILTER is
    off, so it doesn't unfairly cost the short side a point vs. the long side's
    (always-on) MACD condition."""
    row = df.iloc[i]
    bb_ok = (not cfg.USE_BB_SHORT_FILTER) or bool(row["bb_short_reentry"])
    return [
        ("close_below_ema100", bool(row["close"] < row["ema100"])),
        ("ema50_below_ema100", bool(row["ema50"] < row["ema100"])),
        ("adx_trending", bool(row["adx"] > cfg.ADX_THRESHOLD)),
        ("supertrend_bearish", bool(row["supertrend_bearish"])),
        ("rsi_momentum", bool(row["rsi"] < cfg.RSI_SHORT_THRESHOLD)),
        ("bb_reentry", bb_ok),
        ("cci_breakdown_recent", recent_true(df["cci"] < cfg.CCI_SHORT_THRESHOLD, i, cfg.CCI_VALID_BARS)),
        ("stoch_cross_recent", recent_true(df["stoch_bear_cross"], i, cfg.STOCH_VALID_BARS) and bool(row["stoch_k"] < cfg.STOCH_SHORT_THRESHOLD)),
    ]


def long_signal(df, i, cfg):
    if i < 1:
        return False
    row = df.iloc[i]
    # Hard safety cap, not scored: never buy into extreme overbought (blow-off
    # top) regardless of how many other boxes are checked.
    if not (row["rsi"] <= cfg.RSI_LONG_MAX):
        return False
    score = sum(1 for _, ok in long_conditions(df, i, cfg) if ok)
    return score >= cfg.ENTRY_MIN_SCORE


def short_signal(df, i, cfg):
    if i < 1:
        return False
    score = sum(1 for _, ok in short_conditions(df, i, cfg) if ok)
    # SHORT gets its own (stricter) threshold: backtests showed shorts losing
    # money net (PF 0.90) at the same bar the long side was thriving (PF 1.38),
    # so the two sides no longer share one number here.
    return score >= cfg.SHORT_ENTRY_MIN_SCORE


def supertrend_exit_signal(df, i, position):
    """Candle-close Supertrend reversal. Signal is evaluated on closed bars.
    Execution is handled by the backtest on the next candle open.
    """
    if i < 1:
        return False
    prev = df.iloc[i - 1]
    row = df.iloc[i]
    if position == "LONG":
        return (float(prev["close"]) >= float(prev["supertrend"]) and
                float(row["close"]) < float(row["supertrend"]))
    if position == "SHORT":
        return (float(prev["close"]) <= float(prev["supertrend"]) and
                float(row["close"]) > float(row["supertrend"]))
    return False


def exit_signal(position, row):
    if position == "LONG":
        return row["close"] < row["ema100"]
    if position == "SHORT":
        return row["close"] > row["ema100"]
    return False
