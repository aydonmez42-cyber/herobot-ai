#!/usr/bin/env python3
"""
15 dk ve 4 saatlik zaman dilimini AYNI kurallarla karsilastiran geriye donuk test.

Robotun kendi motorunu kullanir (supertrend_signal.py: Supertrend 10 / 4.1, dinamik Supertrend stopu,
basamakli kar kilidi, %6 acil stop). Proje klasorunde calistirin (indicators.py ve supertrend_signal.py yan yana olmali):

    python3 backtest_tf.py                      # 40 coin, 90 gun, komisyon %0.05/yon
    python3 backtest_tf.py --coins 80 --days 120
    python3 backtest_tf.py --tfs 15m,1h,4h      # baska zaman dilimlerini de ekleyin

Varsayimlar (gercekten yakin ama birebir degil):
  - Sinyal mumu kapaninca sinyal gorulur, giris BIR SONRAKI mumun acilisindan yapilir.
  - Supertrend stopu: ters yonde trend donusu olan mumun KAPANISINDAN cikilir (robot da mum kapanisinda bakar).
  - Acil stop ve kar kilidi mum ici fiyatla (low/high) tetiklenir; ayni mumda once aleyhe hareket sayilir (temkinli).
  - Her iki yonde komisyon + kayma toplam --fee yuzde (varsayilan 0.05) kabul edilir. Kaldirac yok: sonuclar FIYAT uzerinden %'dir.
  - Ayni coinde ayni anda tek pozisyon. Portfoy limiti / bakiye simulasyonu yok: her islem esit agirlikli sayilir.
Sonuclar gecmisi gosterir, gelecegi garanti etmez; veri secimine (coin, donem) duyarlidir.
"""
import argparse, json, sys, time, urllib.request, urllib.parse
import numpy as np
import pandas as pd
import supertrend_signal as sts

FAPI = "https://fapi.binance.com"
TF_MIN = {"15m": 15, "1h": 60, "4h": 240}


def _get(path, params=None, retries=4):
    url = FAPI + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "bt"}), timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (418, 429):
                wait = 30 * (k + 1)
                print(f"  istek siniri ({code}), {wait} sn bekleniyor...", file=sys.stderr); time.sleep(wait)
            else:
                time.sleep(1.5)
    raise RuntimeError("istek basarisiz: " + url)


def top_symbols(n):
    info = _get("/fapi/v1/exchangeInfo")
    ok = {s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"}
    t = [x for x in _get("/fapi/v1/ticker/24hr") if x["symbol"] in ok]
    t.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"] for x in t[:n]]


def fetch(symbol, tf, days):
    need = int(days * 1440 / TF_MIN[tf]) + sts.ST_WARMUP_BARS
    end = int(time.time() * 1000); rows = []
    while len(rows) < need:
        chunk = _get("/fapi/v1/klines", {"symbol": symbol, "interval": tf, "limit": 1000, "endTime": end})
        if not chunk: break
        rows = chunk + rows
        end = chunk[0][0] - 1
        time.sleep(0.12)
        if len(chunk) < 1000: break
    rows = rows[-need:][:-1]                                   # son (kapanmamis) mumu at
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["t", "open", "high", "low", "close", "volume"]
    df.index = pd.to_datetime(df["t"], unit="ms")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def simulate(df, fee_pct, warm):
    """Tek coin: tum islemleri doner (liste of dict)."""
    events, _ = sts.run_state_machine(df)
    st = sts.supertrend(df["high"], df["low"], df["close"], sts.ST_PERIOD, sts.ST_MULT)
    d = st["supertrend_direction"].to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df); trades = []; busy_until = -1
    emerg = sts.EMERGENCY_STOP_PCT
    for e in events:
        if e["type"] not in ("LONG", "SHORT", "RE_LONG", "RE_SHORT"): continue
        i = e["i"]
        if i < warm or i + 1 >= n or i <= busy_until: continue
        side = 1 if e["type"] in ("LONG", "RE_LONG") else -1
        entry = o[i + 1]; peak = 0.0; exit_pct = None; reason = None; j = i + 1
        while j < n:
            adverse = (l[j] if side == 1 else h[j]); fav = (h[j] if side == 1 else l[j])
            adv_pct = side * (adverse - entry) / entry * 100
            fl = sts.lock_floor(peak)
            if adv_pct <= -emerg:
                exit_pct, reason = -emerg, "EMERGENCY_STOP"; break
            if fl is not None and adv_pct <= fl:
                exit_pct, reason = fl, "TRAIL_LOCK"; break
            peak = max(peak, side * (fav - entry) / entry * 100)
            if sts.TP_PCT > 0 and peak >= sts.TP_PCT:
                exit_pct, reason = sts.TP_PCT, "TP"; break
            close_pct = side * (c[j] - entry) / entry * 100
            if (side == 1 and d[j] == -1) or (side == -1 and d[j] == 1):
                exit_pct, reason = close_pct, "ST_STOP"; break
            j += 1
        if exit_pct is None:                                   # veri bitti: son kapanisla say
            exit_pct, reason, j = side * (c[-1] - entry) / entry * 100, "OPEN_END", n - 1
        busy_until = j
        trades.append({"side": "LONG" if side == 1 else "SHORT", "kind": e["type"], "bars": j - i,
                       "gross": exit_pct, "net": exit_pct - fee_pct * 2, "reason": reason, "t": df.index[i + 1]})
    return trades


def stats(tr):
    if not tr: return None
    net = np.array([t["net"] for t in tr]); gross = np.array([t["gross"] for t in tr])
    win = net[net > 0]; los = net[net <= 0]
    pf = (win.sum() / -los.sum()) if los.sum() < 0 else float("inf")
    cum = np.cumsum(net); dd = float((cum - np.maximum.accumulate(cum)).min())
    return {"n": len(net), "win%": 100 * len(win) / len(net), "avg%": net.mean(), "med%": float(np.median(net)),
            "avg_win": win.mean() if len(win) else 0, "avg_loss": los.mean() if len(los) else 0,
            "pf": pf, "sum%": net.sum(), "gross_sum%": gross.sum(), "maxdd%": dd, "worst%": net.min(), "best%": net.max()}


def fmt(name, s):
    if not s: return f"{name:<14} islem yok"
    return (f"{name:<14} n={s['n']:<5} kazanma={s['win%']:5.1f}%  ort={s['avg%']:+6.2f}%  medyan={s['med%']:+6.2f}%  "
            f"ort.kazanc={s['avg_win']:+6.2f}%  ort.kayip={s['avg_loss']:+6.2f}%  PF={s['pf']:5.2f}  "
            f"toplam={s['sum%']:+8.1f}%  maxDD={s['maxdd%']:7.1f}%  en kotu={s['worst%']:+6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=40); ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--fee", type=float, default=0.05, help="yon basina komisyon+kayma %%")
    ap.add_argument("--tfs", default="15m,4h"); ap.add_argument("--symbols", default="")
    ap.add_argument("--csv", default="", help="sonuc islemlerini CSV'ye yaz")
    a = ap.parse_args()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or top_symbols(a.coins)
    print(f"{len(syms)} coin, {a.days} gun, komisyon+kayma {a.fee}%/yon, kilit={sts.lock_tiers_text()}, acil stop %{sts.EMERGENCY_STOP_PCT}\n")
    allrows = []; res = {}
    for tf in a.tfs.split(","):
        tr_all = []
        for k, s in enumerate(syms, 1):
            try:
                df = fetch(s, tf, a.days)
            except Exception as ex:
                print(f"  {s} {tf}: {ex}", file=sys.stderr); continue
            warm = max(0, len(df) - int(a.days * 1440 / TF_MIN[tf]))
            for t in simulate(df, a.fee, warm):
                t.update(symbol=s, tf=tf); tr_all.append(t)
            if k % 10 == 0: print(f"  {tf}: {k}/{len(syms)} coin", file=sys.stderr)
        res[tf] = tr_all; allrows += tr_all
    print("=" * 150)
    for tf, tr in res.items():
        print(fmt(f"{tf} TUMU", stats(tr)))
        print(fmt(f"{tf} LONG", stats([t for t in tr if t["side"] == "LONG"])))
        print(fmt(f"{tf} SHORT", stats([t for t in tr if t["side"] == "SHORT"])))
        rs = pd.Series([t["reason"] for t in tr]).value_counts()
        print("   cikis nedenleri:", ", ".join(f"{k}={v}" for k, v in rs.items()),
              f"| ort. sure: {np.mean([t['bars'] for t in tr]) * TF_MIN[tf] / 60:.1f} saat" if tr else "")
        print("-" * 150)
    print("Not: 'toplam' islem basina fiyat % toplamidir (kaldiracsiz, esit agirlik). Kaldirac x ile yaklasik x katina cikar, risk de oyle.")
    if a.csv and allrows:
        pd.DataFrame(allrows).to_csv(a.csv, index=False); print("islemler yazildi:", a.csv)


if __name__ == "__main__":
    main()
