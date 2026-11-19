"""
Supertrend 10 / 4.6 sinyal motoru (1 saatlik mum, kaynak OHLC4). Giris: son kapanmis mum cizginin ustunde LONG, altinda SHORT (ST_ENTRY_MODE=state); cikis: mum kapanisinda cizgi tersine donunce + kademeli kar kilidi.

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

# Supertrend kaynagi: "ohlc4" = (Acilis+Yuksek+Dusuk+Kapanis)/4 (varsayilan) | "hl2" = (Yuksek+Dusuk)/2
ST_SOURCE = os.environ.get("ST_SOURCE", "ohlc4").strip().lower()
# Giris modu: "state" = son kapanmis mum cizginin ustunde -> LONG, altinda -> SHORT (donus beklenmez) | "flip" = yalnizca yeni donus sinyali
ENTRY_MODE = os.environ.get("ST_ENTRY_MODE", "state").strip().lower()


def st_calc(df, period, mult):
    """Projedeki TUM Supertrend hesaplari bu fonksiyondan gecer (kaynak ayari tek yerde)."""
    src = None
    if ST_SOURCE == "ohlc4" and "open" in df.columns:
        src = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    return supertrend(df["high"], df["low"], df["close"], period, mult, src=src)

ST_PERIOD = 10
ST_MULT = float(os.environ.get("ST_MULT", "4.6"))                       # altcoin (1 saatlik mum) taramasi ve islemleri
# BTC yon/rejim Supertrend (1 saatlik mum, altcoin ayariyla ayni): rejim kapisi + yon degisimi korumasi bu ayarla calisir
REGIME_ST_PERIOD = int(float(os.environ.get("ST_REGIME_PERIOD", "10")))
REGIME_ST_MULT = float(os.environ.get("ST_REGIME_MULT", "4.6"))
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
LOCK_TIERS = _parse_tiers(os.environ.get("ST_LOCK_TIERS", "1.5:1,2:1.5,3:2,4:3"))
TRAIL_ARM_PCT = LOCK_TIERS[0][0]
TRAIL_FLOOR_PCT = LOCK_TIERS[0][1]


def _parse_gaps(txt):
    out = []
    for part in str(txt).split(","):
        if ":" in part:
            a, g = part.split(":", 1)
            try:
                out.append((float(a), float(g)))
            except ValueError:
                pass
    out.sort()
    return out


# Son basamagin ustunde izleyen kilit: zirve kar >= esik oldugunda taban = zirve - bosluk (puan). Ornek "8:2,10:1.5":
# +%8..+%10 arasi zirvenin 2 puan, +%10 ustunde 1.5 puan altinda. Bos birakirsaniz ("none") yalnizca basamaklar gecerli.
TRAIL_GAPS = _parse_gaps(os.environ.get("ST_TRAIL_GAPS", "none"))      # son basamak (+%4 -> %3) ustunde kilit = zirve - 1 puan (+%5 -> %4, +%6 -> %5 ...)


def _gap_for(peak):
    gap = None
    for a, g in TRAIL_GAPS:
        if peak >= a:
            gap = g
    return gap


# BTC rejimi pozisyonlarin aleyhine donunce: kar >= esik olanlar kapatilir, 0..esik arasi olanlar basabasa (komisyon payi) cekilir
REGIME_FLIP = os.environ.get("ST_REGIME_FLIP", "on").strip().lower() != "off"
REGIME_TAKE_PROFIT_PCT = float(os.environ.get("ST_REGIME_TAKE_PROFIT_PCT", "1"))
BREAKEVEN_FLOOR_PCT = float(os.environ.get("ST_BREAKEVEN_FLOOR_PCT", "0.1"))


def pos_floor(pos, peak):
    """Pozisyonun etkin koruma tabani %: kilit basamaklari ile basabas tabaninin buyugu; yoksa None."""
    fl = lock_floor(peak)
    be = pos.get("be_floor")
    if be is not None:
        fl = float(be) if fl is None else max(fl, float(be))
    return fl


def lock_floor(peak):
    """Zirve kara gore korunan (kilitli) kar %; henuz kilit yoksa None."""
    if not LOCK_TIERS:
        return None
    fl = None
    for arm, floor in LOCK_TIERS:
        if peak >= arm:
            fl = floor
    if fl is None:
        return None
    arm, floor = LOCK_TIERS[-1]
    if peak > arm:
        fl = max(fl, peak - (arm - floor))
    gap = _gap_for(peak)
    if gap is not None:
        fl = max(fl, peak - gap)
    return fl


def next_lock_target(peak):
    """Sonraki kilit basamaginin tetik kari %."""
    for arm, _ in LOCK_TIERS:
        if peak < arm:
            return arm
    gap = _gap_for(peak)
    return peak + (gap if gap is not None else (LOCK_TIERS[-1][0] - LOCK_TIERS[-1][1]))


def tp_text():
    return f"%{TP_PCT:g}" if TP_PCT > 0 else "yok"


def lock_tiers_text():
    t = " · ".join(f"+%{a:g} → %{f:g}" for a, f in LOCK_TIERS)
    if TRAIL_GAPS:
        t += " · sonrasında zirvenin " + ", ".join(f"+%{a:g}'dan itibaren {g:g}" for a, g in TRAIL_GAPS) + " puan altı"
    return t

# GIRIS ZAMANLAMASI (1 saatlik mum): yalnizca SON kapanmis mumdaki sinyale girilir (yas 0). LONG: mum cizgi ustunde KAPANDIKTAN sonra.
# SHORT: LONG gibi mum kapanisinda onaylanir (sahte kirilim riski nedeniyle). Mum-ici SHORT (SHORT_INTRABAR) varsayilan KAPALI; ST_SHORT_INTRABAR=1 ile acilir.
# Eski "N mum onay" kurali (CONFIRM_BARS) varsayilan olarak kapali (0); istenirse ST_CONFIRM_BARS ile acilir.
CONFIRM_BARS = int(os.environ.get("ST_CONFIRM_BARS", "0"))
CONFIRM_EXTRA_BARS = int(os.environ.get("ST_CONFIRM_EXTRA_BARS", "0"))
SHORT_INTRABAR = os.environ.get("ST_SHORT_INTRABAR", "0").strip().lower() in ("1", "true", "on", "yes")
# Mum-ici SHORT'ta mum kapanisinda donus teyit edilmediyse (fiyat cizgi ustune geri kapandiysa) hemen cik (1) / yalnizca kirmizi cizgi ustu kapanista cik (0, varsayilan)
INTRABAR_FAKEOUT_EXIT = os.environ.get("ST_INTRABAR_FAKEOUT_EXIT", "0").strip().lower() in ("1", "true", "on", "yes")
MAX_SIGNAL_AGE_BARS = CONFIRM_BARS + CONFIRM_EXTRA_BARS + 1   # SABIT: yalnizca son kapanmis mum (yas 0). Ortam degiskeniyle degistirilemez (eski Railway ayari 4+ mumluk sinyali geciriyordu)
WHIPSAW_BARS = int(os.environ.get("ST_WHIPSAW_BARS", "0"))   # eski 16 mumluk testere filtresi (onay kurali onun yerine gecti; 0 = kapali)
LATE_ENTRY_MAX_MOVE_PCT = float(os.environ.get("ST_LATE_ENTRY_MAX_MOVE_PCT", "1.5"))  # sinyal kapanisindan beri en fazla bu kadar kosmus fiyata girilmez
EMERGENCY_STOP_PCT = float(os.environ.get("ST_EMERGENCY_STOP_PCT", "15"))             # felaket freni: giristen % bu kadar ters hareket (0 = kapali). Normal stop = Supertrend mum kapanisi
# Islem acilmayacak semboller (altin/gumus tokenlari: fiyat neredeyse hic oynamaz, sinyal anlamsiz)
EXCLUDE_SYMBOLS = {x.strip().upper() for x in os.environ.get("ST_EXCLUDE_SYMBOLS", "PAXGUSDT,XAUTUSDT,XAUUSDT,XAGUSDT").split(",") if x.strip()}
# Girişte Supertrend stopu fiyattan bu % den uzaksa islem acilmaz (cok oynak coin; acil stop yok -> kayip siniri). 0 = kapali
MAX_STOP_DIST_PCT = float(os.environ.get("ST_MAX_STOP_DIST_PCT", "6"))


# Pozisyon boyutu (paper): risk bazli. Stop olursa kaybedilecek tutar = guncel bakiyenin RISK_PCT %'si
RISK_PCT = float(os.environ.get("ST_RISK_PCT", "0.25"))              # 0 = eski sabit boyut
MAX_NOTIONAL_USD = float(os.environ.get("ST_MAX_NOTIONAL_USD", "2500"))   # tek pozisyon nominal ust siniri
MIN_NOTIONAL_USD = float(os.environ.get("ST_MIN_NOTIONAL_USD", "100"))    # bundan kucuk pozisyon acilmaz
MAX_EXPOSURE_X = float(os.environ.get("ST_MAX_EXPOSURE_X", "3"))          # toplam acik nominal <= bakiye x bu
# BTC rejimi: BTC'nin Supertrend yonunun TERSINE en fazla bu kadar acik pozisyon
COUNTER_TREND_MAX = int(float(os.environ.get("ST_COUNTER_TREND_MAX", "2")))
REGIME_SYMBOL = os.environ.get("ST_REGIME_SYMBOL", "BTCUSDT").upper()


def position_notional(equity, open_notional, price, level, base_usd):
    """(nominal_usd, neden). Risk bazli boyut: risk_usd / stop_mesafesi, ust/alt sinir ve toplam maruziyet siniri ile.
    nominal 0 ise pozisyon acilmaz."""
    if RISK_PCT <= 0:
        return float(base_usd), None
    equity = float(equity or 0.0)
    if equity <= 0 or not price:
        return 0.0, "NO_EQUITY"
    dist = abs(price - level) / price if level else 0.06
    dist = max(dist, 0.005)                      # cok yakin stopta boyut sismesin
    n = min(equity * RISK_PCT / 100.0 / dist, MAX_NOTIONAL_USD)
    room = equity * MAX_EXPOSURE_X - float(open_notional or 0.0)
    if room < n:
        n = room
    if n < MIN_NOTIONAL_USD:
        return 0.0, "EXPOSURE_LIMIT" if room < MIN_NOTIONAL_USD else "TOO_SMALL"
    return float(n), None


# Hacim filtresi: sinyal mumunun hacmi / onceki REL_VOL_N mumun ortalamasi >= MIN_REL_VOL olmali (0 = kapali).
# Backtest (12 coin, 90 ve 180 gun): 1.5 esigi her iki donemde PF'yi yukseltti.
MIN_REL_VOL = float(os.environ.get("ST_MIN_REL_VOL", "1.5"))
REL_VOL_N = 20


def signal_rel_volume(ind, signal_time, n=None):
    """Sinyal mumunun goreli hacmi. ind: kapanmis mumlar (index = close_time, 'volume' sutunu). Hesaplanamazsa None."""
    n = n or REL_VOL_N
    if "volume" not in ind.columns:
        return None
    ts = pd.Timestamp(signal_time)
    if ind.index.tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    elif ind.index.tz is None and ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    try:
        i = int(ind.index.get_loc(ts))
    except Exception:
        return None
    if i < n:
        return None
    v = ind["volume"].to_numpy(dtype=float)
    prev = float(np.mean(v[i - n:i]))
    return float(v[i] / prev) if prev > 0 else None


def regime_direction(df_closed):
    """Kapanmis mumlarla REGIME_SYMBOL'un Supertrend yonu: 'LONG' | 'SHORT' | None."""
    st = st_calc(df_closed, REGIME_ST_PERIOD, REGIME_ST_MULT)
    d = st["supertrend_direction"].iloc[-1]
    if np.isnan(d):
        return None
    return "LONG" if int(d) == 1 else "SHORT"


def is_excluded(symbol):
    return bool(symbol) and str(symbol).upper() in EXCLUDE_SYMBOLS


def run_state_machine(df, stop_mode=None):
    """Pine durum makinesini df (kapanmis mumlar) uzerinde calistirir.
    Doner: (events, final_state). events: sozluk listesi
      type in LONG | SHORT | RE_LONG | RE_SHORT | STOP_LONG | STOP_SHORT
    """
    stop_mode = stop_mode or ST_STOP_MODE
    st = st_calc(df, ST_PERIOD, ST_MULT)
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


def state_entry_signal(df):
    """Durum bazli giris: SON KAPANMIS mum Supertrend cizgisinin ustunde (yon +1) ise LONG, altinda (yon -1) ise SHORT.
    Sinyal zamani = mevcut trendin basladigi mum (ayni trendde cikistan sonra tekrar girilmez). age_bars daima 0."""
    df = df.iloc[-ST_WARMUP_BARS:]
    st = st_calc(df, ST_PERIOD, ST_MULT)
    d = st["supertrend_direction"].to_numpy()
    line = st["supertrend"].to_numpy()
    if len(d) < 2 or np.isnan(d[-1]) or np.isnan(line[-1]):
        return None
    j = len(d) - 1
    while j > 0 and not np.isnan(d[j - 1]) and d[j - 1] == d[-1]:
        j -= 1
    side = "LONG" if int(d[-1]) == 1 else "SHORT"
    ln = float(line[-1])
    return {"side": side, "kind": side, "signal_time": df.index[j], "signal_close": float(df["close"].iloc[-1]),
            "level": ln, "st": ln, "st_signal": ln, "age_bars": 0}


def latest_entry_signal(df, max_age_bars=None, min_age_bars=None):
    if ENTRY_MODE == "state":
        return state_entry_signal(df)
    return _flip_entry_signal(df, max_age_bars, min_age_bars)


def _flip_entry_signal(df, max_age_bars=None, min_age_bars=None):
    """ONAYLANMIS LONG/SHORT sinyali (flip veya yeniden giris): sinyal mumundan en az `min_age_bars` (varsayilan CONFIRM_BARS)
    ve en fazla `max_age_bars`-1 mum gecmis, aradaki mumlarda sinyalin tersine donus/stop OLMAMIS olmali.
    En yeni gecerli sinyali doner, yoksa None. age_bars: 0 = son kapanmis mum.
    "st" alani = SU ANKI Supertrend cizgisi (onay beklerken cizgi hareket eder; stop/boyutlandirma guncel cizgiye gore)."""
    max_age_bars = MAX_SIGNAL_AGE_BARS if max_age_bars is None else max_age_bars
    min_age_bars = CONFIRM_BARS if min_age_bars is None else min_age_bars
    df = df.iloc[-ST_WARMUP_BARS:]
    events, _ = run_state_machine(df)
    last = len(df) - 1
    for k, e in enumerate(reversed(events)):
        if e["type"] in ("LONG", "RE_LONG", "SHORT", "RE_SHORT") and min_age_bars <= last - e["i"] <= max_age_bars - 1:
            newer = list(reversed(events))[:k]            # bu sinyalden SONRAKI olaylar
            side_of = "LONG" if e["type"] in ("LONG", "RE_LONG") else "SHORT"
            if any(x["type"] == "STOP_" + side_of for x in newer):
                return None                               # sinyal sonradan stop oldu: girme
            _st_now = st_calc(df, ST_PERIOD, ST_MULT)["supertrend"].iloc[-1]
            return {"side": "LONG" if e["type"] in ("LONG", "RE_LONG") else "SHORT",
                    "kind": e["type"], "signal_time": e["time"], "signal_close": e["close"],
                    "level": e["level"], "st": (float(_st_now) if _st_now == _st_now else e["st"]), "st_signal": e["st"],
                    "age_bars": last - e["i"]}
    return None


def whipsaw_blocked(ind, sig, bars=None):
    """Testere (whipsaw) filtresi: sinyalin LONG/SHORT donusunden onceki `bars` mum icinde TERS yonlu
    bir Supertrend donusu varsa (trend < `bars` mum yasadi) True doner. ind: kapanmis mumlar."""
    bars = WHIPSAW_BARS if bars is None else bars
    if bars <= 0 or ind is None or sig is None:
        return False
    df = ind.iloc[-ST_WARMUP_BARS:]
    events, _ = run_state_machine(df)
    last = len(df) - 1
    side = sig.get("side")
    opp = "SHORT" if side == "LONG" else "LONG"
    try:
        sig_t = pd.Timestamp(sig.get("signal_time"))
    except Exception:
        sig_t = None
    i_sig = last
    if sig_t is not None:
        for e in events:
            if pd.Timestamp(e["time"]) == sig_t and e["type"] in ("LONG", "RE_LONG", "SHORT", "RE_SHORT"):
                i_sig = e["i"]
    return any(e["type"] == opp and 0 < i_sig - e["i"] < bars for e in events)


def last_closed_side_ok(ind, side):
    """Giris kurali: LONG icin SON KAPANMIS mum Supertrend cizgisinin USTUNDE (trend yukari), SHORT icin ALTINDA."""
    st = st_calc(ind, ST_PERIOD, ST_MULT)
    close = float(ind["close"].iloc[-1])
    line = float(st["supertrend"].iloc[-1])
    d = int(st["supertrend_direction"].iloc[-1])
    if np.isnan(line):
        return False
    return (d == 1 and close > line) if side == "LONG" else (d == -1 and close < line)


def intrabar_short_signal(df, forming_close_time=None):
    """Mum-ici SHORT: df = kapanmis mumlar + SON satir olusmakta olan mum (close = canli fiyat).
    Son kapanmis mum trend YUKARI iken, olusan mumda fiyat yesil cizgiyi asagi kirdiysa (mum o fiyatta kapansaydi donus olurdu)
    SHORT sinyali doner. 'st' = olusan (gecici) kirmizi cizgi; 'level' = kirilan yesil cizgi."""
    if df is None or len(df) < 20:
        return None
    st = st_calc(df, ST_PERIOD, ST_MULT)
    d = st["supertrend_direction"].to_numpy()
    line = st["supertrend"].to_numpy()
    if d[-2] != 1 or d[-1] != -1 or np.isnan(line[-1]) or np.isnan(line[-2]):
        return None
    return {"side": "SHORT", "kind": "SHORT", "signal_time": forming_close_time, "signal_close": float(df["close"].iloc[-1]),
            "level": float(line[-2]), "st": float(line[-1]), "age_bars": 0, "intrabar": True}


def entry_allowed(sig, last_price, ind=None, symbol=None):
    """Tarama filtreleri. (ok, neden) doner. 24s degisim filtresi YOK (kaldirildi).
    ind verilirse (kapanmis mumlar) son mumun Supertrend'e gore konumu da sart."""
    if sig is None:
        return False, "NO_SIGNAL"
    if is_excluded(symbol):
        return False, "EXCLUDED_SYMBOL"
    if int(sig.get("age_bars") or 0) > CONFIRM_BARS + CONFIRM_EXTRA_BARS:
        return False, "SIGNAL_TOO_OLD"           # son kapanmis mumdan eski sinyalle giris yok
    _ib = bool(sig.get("intrabar"))                # mum-ici SHORT: son kapanmis mum henuz yesil cizgi ustunde
    if ind is not None and not _ib and not last_closed_side_ok(ind, sig["side"]):
        return False, "LAST_CANDLE_WRONG_SIDE_OF_ST"
    if ind is not None and not _ib and whipsaw_blocked(ind, sig):
        return False, "WHIPSAW_16"
    if MAX_STOP_DIST_PCT > 0 and last_price:
        line = None
        if ind is not None and not _ib:
            _st = st_calc(ind, ST_PERIOD, ST_MULT)
            line = float(_st["supertrend"].iloc[-1])
            line = None if np.isnan(line) else line
        if line is None and sig.get("st") is not None:
            line = float(sig["st"])
        if line is not None and abs(last_price - line) / last_price * 100.0 > MAX_STOP_DIST_PCT:
            return False, "STOP_TOO_FAR"
    lvl = sig.get("level")
    if lvl is not None:
        if sig["side"] == "LONG" and last_price <= lvl:
            return False, "BELOW_LEVEL"
        if sig["side"] == "SHORT" and last_price >= lvl:
            return False, "ABOVE_LEVEL"
    sc = sig["signal_close"]
    if ind is not None and CONFIRM_BARS > 0 and int(sig.get("age_bars") or 0) >= CONFIRM_BARS:
        sc = float(ind["close"].iloc[-1])           # onay beklenirken fiyat zaten kosmus olabilir: "gec giris" olcusu son kapanmis mumdan
    move = (last_price - sc) / sc * 100.0
    move = move if sig["side"] == "LONG" else -move          # + = lehe, - = aleyhe
    if move > LATE_ENTRY_MAX_MOVE_PCT:
        return False, "LATE_ENTRY"
    return True, "OK"


def check_exit(pos, df, last_price):
    """Acik pozisyon icin cikis karari. pos: side, entry_price, entry_time, st_level.
    df: KAPANMIS 1 saatlik mumlar (en az giristen sonrasini icermeli). Doner: cikis nedeni veya None.
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

    if EMERGENCY_STOP_PCT > 0 and profit <= -EMERGENCY_STOP_PCT:
        return "EMERGENCY_STOP"
    if TP_PCT > 0 and profit >= TP_PCT:
        return "TP"
    _fl = pos_floor(pos, peak)
    if _fl is not None and profit <= _fl:
        return "TRAIL_LOCK"

    st = st_calc(df, ST_PERIOD, ST_MULT)
    close, direction = float(df["close"].iloc[-1]), int(st["supertrend_direction"].iloc[-1])
    level = pos.get("st_level")
    if side == "LONG":
        if (ST_STOP_MODE == "level" and level is not None and close < level) or direction == -1:
            return "ST_STOP"
    else:
        if pos.get("st_intrabar") and not pos.get("st_confirmed"):
            # Mum-ici SHORT: donus mum kapanisinda teyit edilene kadar ters yonlu ST cizgisi stop sayilmaz.
            if len(since) == 0:
                return None                                   # giris mumu henuz kapanmadi
            if direction == -1:
                pos["st_confirmed"] = True                    # mum asagi donusle kapandi: normal Supertrend stopu
            else:
                if INTRABAR_FAKEOUT_EXIT:
                    return "ST_STOP"
                if level is not None and close > level:       # SL: kirmizi cizginin ustunde MUM KAPANISI
                    return "ST_STOP"
                return None
        if (ST_STOP_MODE == "level" and level is not None and close > level) or direction == 1:
            return "ST_STOP"
    return None
