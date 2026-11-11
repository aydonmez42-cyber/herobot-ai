"""
Supertrend 10 / 4.1 sinyal motoru (15 dk) - TradingView "ST Sinyal" Pine kodunun birebir Python karsiligi.

Bu modul YALNIZCA karar verir (sinyal / cikis nedeni). Emir acmak/kapatmak paper_trading.py ve
live_trading.py'nin isidir. Tum fonksiyonlar KAPANMIS mumlar uzerinde calisir: cagirmadan once
son (henuz kapanmamis) mumu df'den cikarin.

Sag uzatilan cizgi = Pine'daki "kirilan seviye" (lvlLong / lvlShort): trend donusunde kirilan
supertrend degeri (st[1]) yatay olarak saklanir; stop ve yeniden giris bu sabit seviyeye gore yapilir.
"""
import os
import numpy as np
import pandas as pd
from indicators import supertrend

ST_PERIOD = 10
ST_MULT = 4.1
ST_STOP_MODE = os.environ.get("ST_STOP_MODE", "flip")      # "flip": stop = Supertrend'in kendi (dinamik) cizgisi | "level": kirilan eski seviye
ST_WARMUP_BARS = 499                                         # durum makinesini kurmak icin gecmis mum sayisi

TP_PCT = float(os.environ.get("ST_TP_PCT", "0"))             # sabit TP (fiyat uzerinden %, kaldiracsiz); 0 = kapali (basamakli kar kilidi kullanilir)


def _parse_tiers(txt):
    out = []
    for part in str(txt).split(","):
        if ":" in part:
            a, f = part.split(":", 1)
            try:
                out.append((float(a), float(f)))
            except ValueError:
                pass
    out.sort()
    return out or [(2.5, 1.0), (4.0, 2.5), (6.0, 4.0), (8.0, 6.0)]


# Basamakli kar kilidi: "zirve_kar:kilitli_kar" ciftleri (fiyat uzerinden %). Zirve kar ilk degere ulasinca
# kar o seviyenin kilidine iner, kilit zirve buyudukce yukselir. Son basamagin ustunde kilit = zirve - (son_zirve - son_kilit).
LOCK_TIERS = _parse_tiers(os.environ.get("ST_LOCK_TIERS", "2.5:1,4:2.5,6:4,8:6"))
TRAIL_ARM_PCT = LOCK_TIERS[0][0]
TRAIL_FLOOR_PCT = LOCK_TIERS[0][1]


def lock_floor(peak):
    """Zirve kara gore korunan (kilitli) kar %; henuz kilit yoksa None."""
    fl = None
    for arm, floor in LOCK_TIERS:
        if peak >= arm:
            fl = floor
    if fl is None:
        return None
    arm, floor = LOCK_TIERS[-1]
    if peak > arm:
        fl = max(fl, peak - (arm - floor))
    return fl


def next_lock_target(peak):
    """Sonraki kilit basamaginin tetik kari %."""
    for arm, _ in LOCK_TIERS:
        if peak < arm:
            return arm
    return peak + (LOCK_TIERS[-1][0] - LOCK_TIERS[-1][1])


def tp_text():
    return f"%{TP_PCT:g}" if TP_PCT > 0 else "yok"


def lock_tiers_text():
    return " · ".join(f"+%{a:g} → %{f:g}" for a, f in LOCK_TIERS)

MAX_SIGNAL_AGE_BARS = int(os.environ.get("ST_MAX_SIGNAL_AGE_BARS", "3"))   # yalnizca son 3 kapanmis 15 dk mumda olusan taze sinyal
LATE_ENTRY_MAX_MOVE_PCT = float(os.environ.get("ST_LATE_ENTRY_MAX_MOVE_PCT", "1.5"))  # sinyal kapanisindan beri en fazla bu kadar kosmus fiyata girilmez
EMERGENCY_STOP_PCT = float(os.environ.get("ST_EMERGENCY_STOP_PCT", "6.0"))            # acil stop: girisen % olarak olumsuz hareket


def run_state_machine(df, stop_mode=None):
    """Pine durum makinesini df (kapanmis mumlar) uzerinde calistirir.
    Doner: (events, final_state). events: sozluk listesi
      type in LONG | SHORT | RE_LONG | RE_SHORT | STOP_LONG | STOP_SHORT
    """
    stop_mode = stop_mode or ST_STOP_MODE
    st = supertrend(df["high"], df["low"], df["close"], ST_PERIOD, ST_MULT)
    line = st["supertrend"].to_numpy()
    d = st["supertrend_direction"].to_numpy()          # +1 yukselis, -1 dusus
    c = df["close"].to_numpy()
    idx = df.index

    side, wait = 0, 0
    lvl_long = lvl_short = np.nan
    events = []

    def ev(i, typ, level):
        events.append({"i": i, "time": idx[i], "type": typ, "close": float(c[i]),
                       "level": None if np.isnan(level) else float(level), "st": float(line[i])})

    for i in range(1, len(df)):
        if np.isnan(d[i]) or np.isnan(d[i - 1]):
            continue
        flip_long = d[i] == 1 and d[i - 1] == -1
        flip_short = d[i] == -1 and d[i - 1] == 1

        if side == 1:
            stop = (c[i] < lvl_long or flip_short) if stop_mode == "level" else flip_short
            if stop:
                ev(i, "STOP_LONG", lvl_long)
                side, wait = 0, (0 if flip_short else 1)
        elif side == -1:
            stop = (c[i] > lvl_short or flip_long) if stop_mode == "level" else flip_long
            if stop:
                ev(i, "STOP_SHORT", lvl_short)
                side, wait = 0, (0 if flip_long else -1)
        elif wait == 1 and d[i] == 1 and c[i] > lvl_long:
            ev(i, "RE_LONG", lvl_long)
            side, wait = 1, 0
        elif wait == -1 and d[i] == -1 and c[i] < lvl_short:
            ev(i, "RE_SHORT", lvl_short)
            side, wait = -1, 0

        if flip_short:
            side, wait, lvl_short = -1, 0, line[i - 1]
            ev(i, "SHORT", lvl_short)
        if flip_long:
            side, wait, lvl_long = 1, 0, line[i - 1]
            ev(i, "LONG", lvl_long)

    state = {"side": side, "wait": wait, "lvl_long": lvl_long, "lvl_short": lvl_short,
             "direction": int(d[-1]) if not np.isnan(d[-1]) else 0, "st": float(line[-1])}
    return events, state


def latest_entry_signal(df, max_age_bars=None):
    """Son `max_age_bars` kapanmis mum icinde LONG/SHORT (flip veya yeniden giris) var mi?
    En yeni sinyali doner, yoksa None. age_bars: 0 = son kapanmis mum."""
    max_age_bars = MAX_SIGNAL_AGE_BARS if max_age_bars is None else max_age_bars
    df = df.iloc[-ST_WARMUP_BARS:]
    events, _ = run_state_machine(df)
    last = len(df) - 1
    for k, e in enumerate(reversed(events)):
        if e["type"] in ("LONG", "RE_LONG", "SHORT", "RE_SHORT") and last - e["i"] <= max_age_bars - 1:
            newer = list(reversed(events))[:k]            # bu sinyalden SONRAKI olaylar
            side_of = "LONG" if e["type"] in ("LONG", "RE_LONG") else "SHORT"
            if any(x["type"] == "STOP_" + side_of for x in newer):
                return None                               # sinyal sonradan stop oldu: girme
            return {"side": "LONG" if e["type"] in ("LONG", "RE_LONG") else "SHORT",
                    "kind": e["type"], "signal_time": e["time"], "signal_close": e["close"],
                    "level": e["level"], "st": e["st"], "age_bars": last - e["i"]}
    return None


def last_closed_side_ok(ind, side):
    """Giris kurali: LONG icin SON KAPANMIS mum Supertrend cizgisinin USTUNDE (trend yukari), SHORT icin ALTINDA."""
    st = supertrend(ind["high"], ind["low"], ind["close"], ST_PERIOD, ST_MULT)
    close = float(ind["close"].iloc[-1])
    line = float(st["supertrend"].iloc[-1])
    d = int(st["supertrend_direction"].iloc[-1])
    if np.isnan(line):
        return False
    return (d == 1 and close > line) if side == "LONG" else (d == -1 and close < line)


def entry_allowed(sig, last_price, ind=None):
    """Tarama filtreleri. (ok, neden) doner. 24s degisim filtresi YOK (kaldirildi).
    ind verilirse (kapanmis mumlar) son mumun Supertrend'e gore konumu da sart."""
    if sig is None:
        return False, "NO_SIGNAL"
    if ind is not None and not last_closed_side_ok(ind, sig["side"]):
        return False, "LAST_CANDLE_WRONG_SIDE_OF_ST"
    lvl = sig.get("level")
    if lvl is not None:
        if sig["side"] == "LONG" and last_price <= lvl:
            return False, "BELOW_LEVEL"
        if sig["side"] == "SHORT" and last_price >= lvl:
            return False, "ABOVE_LEVEL"
    sc = sig["signal_close"]
    move = (last_price - sc) / sc * 100.0
    move = move if sig["side"] == "LONG" else -move          # + = lehe, - = aleyhe
    if move > LATE_ENTRY_MAX_MOVE_PCT:
        return False, "LATE_ENTRY"
    return True, "OK"


def check_exit(pos, df, last_price):
    """Acik pozisyon icin cikis karari. pos: side, entry_price, entry_time, st_level.
    df: KAPANMIS 15 dk mumlar (en az giristen sonrasini icermeli). Doner: cikis nedeni veya None.
    Oncelik: EMERGENCY_STOP > TP > TRAIL_LOCK > ST_STOP."""
    side, entry = pos["side"], float(pos["entry_price"])
    sign = 1.0 if side == "LONG" else -1.0
    profit = sign * (last_price - entry) / entry * 100.0

    since = df[df.index > pd.Timestamp(pos["entry_time"])] if pos.get("entry_time") else df.iloc[0:0]
    if len(since):
        extreme = since["high"].max() if side == "LONG" else since["low"].min()
        peak = max(sign * (extreme - entry) / entry * 100.0, profit)
    else:
        peak = profit
    peak = max(peak, float(pos.get('peak_pct') or 0.0))

    if profit <= -EMERGENCY_STOP_PCT:
        return "EMERGENCY_STOP"
    if TP_PCT > 0 and profit >= TP_PCT:
        return "TP"
    _fl = lock_floor(peak)
    if _fl is not None and profit <= _fl:
        return "TRAIL_LOCK"

    st = supertrend(df["high"], df["low"], df["close"], ST_PERIOD, ST_MULT)
    close, direction = float(df["close"].iloc[-1]), int(st["supertrend_direction"].iloc[-1])
    level = pos.get("st_level")
    if side == "LONG":
        if (ST_STOP_MODE == "level" and level is not None and close < level) or direction == -1:
            return "ST_STOP"
    else:
        if (ST_STOP_MODE == "level" and level is not None and close > level) or direction == 1:
            return "ST_STOP"
    return None
