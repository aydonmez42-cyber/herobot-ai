import numpy as np
import pandas as pd


def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rsi(close, length=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.where(avg_loss != 0, 100)
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    return result


def cci(high, low, close, length=20):
    typical_price = (high + low + close) / 3.0
    sma = typical_price.rolling(length).mean()
    mean_dev = typical_price.rolling(length).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    return (typical_price - sma) / (0.015 * mean_dev.replace(0, np.nan))


def atr(high, low, close, length=14):
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder-style ATR, consistent with the ADX smoothing used here.
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def adx(high, low, close, length=14):
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_value = tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    plus_smoothed = plus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    minus_smoothed = minus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    plus_di = 100 * plus_smoothed / atr_value.replace(0, np.nan)
    minus_di = 100 * minus_smoothed / atr_value.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_value = dx.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    return pd.DataFrame(
        {"plus_di": plus_di, "minus_di": minus_di, "adx": adx_value},
        index=close.index,
    )


def stoch_rsi(close, rsi_length=14, stoch_length=14, k_smooth=3, d_smooth=3):
    r = rsi(close, rsi_length)
    lowest = r.rolling(stoch_length).min()
    highest = r.rolling(stoch_length).max()

    raw = 100 * (r - lowest) / (highest - lowest).replace(0, np.nan)
    k = raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d



def supertrend(high, low, close, period=10, multiplier=3.0):
    """ATR-based Supertrend. Direction: +1 bullish, -1 bearish."""
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_value = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr_value
    basic_lower = hl2 - multiplier * atr_value

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    for i in range(1, len(close)):
        if pd.isna(atr_value.iloc[i]):
            continue

        prev_fu = final_upper.iloc[i - 1]
        prev_fl = final_lower.iloc[i - 1]
        prev_close = close.iloc[i - 1]

        if pd.isna(prev_fu):
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = (
                basic_upper.iloc[i]
                if basic_upper.iloc[i] < prev_fu or prev_close > prev_fu
                else prev_fu
            )

        if pd.isna(prev_fl):
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = (
                basic_lower.iloc[i]
                if basic_lower.iloc[i] > prev_fl or prev_close < prev_fl
                else prev_fl
            )

    direction = pd.Series(np.nan, index=close.index, dtype=float)
    st = pd.Series(np.nan, index=close.index, dtype=float)

    first_valid = atr_value.first_valid_index()
    if first_valid is None:
        return pd.DataFrame({"supertrend": st, "supertrend_direction": direction}, index=close.index)

    start = close.index.get_loc(first_valid)
    direction.iloc[start] = 1 if close.iloc[start] >= final_lower.iloc[start] else -1
    st.iloc[start] = final_lower.iloc[start] if direction.iloc[start] == 1 else final_upper.iloc[start]

    for i in range(start + 1, len(close)):
        if pd.isna(atr_value.iloc[i]):
            continue

        prev_dir = direction.iloc[i - 1]
        if prev_dir == 1:
            if close.iloc[i] < final_lower.iloc[i]:
                direction.iloc[i] = -1
                st.iloc[i] = final_upper.iloc[i]
            else:
                direction.iloc[i] = 1
                st.iloc[i] = final_lower.iloc[i]
        else:
            if close.iloc[i] > final_upper.iloc[i]:
                direction.iloc[i] = 1
                st.iloc[i] = final_lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st.iloc[i] = final_upper.iloc[i]

    return pd.DataFrame({"supertrend": st, "supertrend_direction": direction}, index=close.index)


def bollinger_bands(close, length=20, std_multiplier=2.0):
    mid = close.rolling(length).mean()
    std = close.rolling(length).std(ddof=0)
    upper = mid + std_multiplier * std
    lower = mid - std_multiplier * std
    return mid, upper, lower


def add_indicators(df, cfg):
    out = df.copy()

    out["ema50"] = ema(out["close"], cfg.EMA_FAST)
    out["ema100"] = ema(out["close"], cfg.EMA_SLOW)
    out["atr"] = atr(out["high"], out["low"], out["close"], cfg.ATR_LENGTH)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = bollinger_bands(
        out["close"], cfg.BB_LENGTH, cfg.BB_STD_MULTIPLIER
    )
    st = supertrend(
        out["high"], out["low"], out["close"],
        cfg.SUPERTREND_PERIOD, cfg.SUPERTREND_MULTIPLIER
    )
    out["supertrend"] = st["supertrend"]
    out["supertrend_direction"] = st["supertrend_direction"]
    out["supertrend_bullish"] = out["supertrend_direction"] == 1
    out["supertrend_bearish"] = out["supertrend_direction"] == -1
    adx_df = adx(out["high"], out["low"], out["close"], cfg.ADX_LENGTH)
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]
    out["adx"] = adx_df["adx"]
    out["cci"] = cci(out["high"], out["low"], out["close"], cfg.CCI_LENGTH)
    out["rsi"] = rsi(out["close"], cfg.RSI_LENGTH)

    # MACD(12,26,9) for TEST 26
    macd_fast = ema(out["close"], cfg.MACD_FAST_LENGTH)
    macd_slow = ema(out["close"], cfg.MACD_SLOW_LENGTH)
    out["macd_line"] = macd_fast - macd_slow
    out["macd_signal"] = ema(out["macd_line"], cfg.MACD_SIGNAL_LENGTH)
    out["macd_hist"] = out["macd_line"] - out["macd_signal"]
    out["macd_long_ok"] = (out["macd_line"] > out["macd_signal"]) & (out["macd_hist"] > 0)

    out["stoch_k"], out["stoch_d"] = stoch_rsi(
        out["close"],
        cfg.STOCH_RSI_RSI_LENGTH,
        cfg.STOCH_RSI_STOCH_LENGTH,
        cfg.STOCH_RSI_K_SMOOTH,
        cfg.STOCH_RSI_D_SMOOTH,
    )

    out["ema_bull_cross"] = (
        (out["ema50"].shift(1) <= out["ema100"].shift(1))
        & (out["ema50"] > out["ema100"])
    )
    out["ema_bear_cross"] = (
        (out["ema50"].shift(1) >= out["ema100"].shift(1))
        & (out["ema50"] < out["ema100"])
    )

    out["stoch_bull_cross"] = (
        (out["stoch_k"].shift(1) <= out["stoch_d"].shift(1))
        & (out["stoch_k"] > out["stoch_d"])
    )
    out["stoch_bear_cross"] = (
        (out["stoch_k"].shift(1) >= out["stoch_d"].shift(1))
        & (out["stoch_k"] < out["stoch_d"])
    )

    # Price was above the upper Bollinger Band and closes back inside it.
    out["bb_short_reentry"] = (
        (out["close"].shift(1) > out["bb_upper"].shift(1))
        & (out["close"] <= out["bb_upper"])
    )

    return out
