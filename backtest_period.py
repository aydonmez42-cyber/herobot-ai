#!/usr/bin/env python3
"""
Herobot-AI | Yeni sistem (Supertrend 10 / 4.1, 1 saatlik mum) donem backtesti.

Robotun kendi motorunu kullanir (supertrend_signal.py): dinamik Supertrend stopu, kademeli kar kilidi, %15 acil stop,
goreli hacim filtresi, stop mesafesi siniri. Proje klasorunde (backtest_tf.py, indicators.py, supertrend_signal.py yan yana) calistirin:

    python3 backtest_period.py                                   # BTC,ETH,SOL,XRP,BNB  2020-08-01 -> 2026-11-14, 1 saatlik mum
    python3 backtest_period.py --confirm 5                       # canli sistemdeki 5 mum onay kurali ile
    python3 backtest_period.py --symbols BTCUSDT,ETHUSDT
    python3 backtest_period.py --start 2020-08-01 --end 2026-11-14 --out backtest_yeni

Cikti: <out>.json (siteye islenecek), <out>.txt (okunur ozet), <out>_islemler.csv (senaryo A islemleri).
Veri onbellegi: veri_onbellek/<SEMBOL>_15m.csv (Binance'ten 15 dk'lik ham mumlar indirilir; 1 saatlik/4 saatlik mumlar bunlardan birlestirilir; tekrar calistirmada yeniden indirmez).

Varsayimlar: giris sinyal mumunu izleyen mumun acilisi; Supertrend stopu mum kapanisinda; acil stop/kar kilidi mum ici (temkinli sira);
komisyon+kayma yon basina --fee %; ayni coinde tek pozisyon. Robotun BTC rejim korumasi, portfoy/maruziyet limiti ve canli
emir gecikmeleri MODELLENMEZ. Gecmis performans gelecegi garanti etmez.
Hesap simulasyonu: risk bazli boyut (bakiye x %risk / stop mesafesi, tek pozisyon ust siniri bakiyenin %cap'i), islemler cikis sirasina gore bilesik.
"""
import argparse, json, os, sys, time, urllib.request, urllib.parse
import numpy as np, pandas as pd
import supertrend_signal as sts
import backtest_tf as bt

CACHE = "veri_onbellek"
MS15 = 15 * 60 * 1000


def _klines(symbol, start_ms, end_ms):
    rows = []; cur = start_ms
    while cur < end_ms:
        chunk = bt._get("/fapi/v1/klines", {"symbol": symbol, "interval": "15m", "limit": 1500, "startTime": cur, "endTime": end_ms})
        if not chunk: break
        rows += chunk; cur = chunk[-1][0] + MS15
        time.sleep(0.7)                                          # ortak IP'de robotun limitini yememek icin yavas
        if len(chunk) < 1500: break
    return rows


def load(symbol, start, end):
    os.makedirs(CACHE, exist_ok=True); fp = f"{CACHE}/{symbol}_15m.csv"
    s_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000) - 8 * 86400000      # isinma icin 8 gun once
    e_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) + 86400000            # bitis gunu dahil
    old = None
    if os.path.exists(fp):
        old = pd.read_csv(fp, index_col=0, parse_dates=True)
    if old is not None and len(old) and old.index[0] <= pd.Timestamp(start) - pd.Timedelta(days=7):
        have_end = int(old.index[-1].timestamp() * 1000) + MS15
        new = _klines(symbol, have_end, e_ms) if have_end < e_ms else []
    else:
        old = None; new = _klines(symbol, s_ms, e_ms)
    if new:
        d = pd.DataFrame(new).iloc[:, :6]; d.columns = ["t", "open", "high", "low", "close", "volume"]
        d.index = pd.to_datetime(d["t"], unit="ms"); d = d[["open", "high", "low", "close", "volume"]].astype(float)
        df = pd.concat([old, d]) if old is not None else d
    else:
        df = old
    df = df[~df.index.duplicated()].sort_index(); df.to_csv(fp)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
    return df[df.index < end_ts].iloc[:-1] if len(df) and df.index[-1] >= end_ts - pd.Timedelta(minutes=15) else df[df.index < end_ts]


def account_sim(trades, risk_pct, cap_pct, start_bal, max_sd):
    """Risk bazli bilesik hesap: islemler cikis zamanina gore sirali uygulanir (maruziyet limiti yok)."""
    eq = start_bal; peak = eq; mdd = 0.0; curve = []; n = 0
    for t in sorted(trades, key=lambda x: x["t_exit"]):
        sd = max(t["sd"], 0.3)
        if max_sd > 0 and sd > max_sd: continue
        notional = min(eq * risk_pct / 100 / (sd / 100), eq * cap_pct / 100)
        eq += notional * t["net"] / 100; n += 1
        peak = max(peak, eq); mdd = min(mdd, (eq / peak - 1) * 100)
        curve.append((t["t_exit"], eq))
    return eq, mdd, curve


def block(tr, fee_note=None):
    s = bt.stats(tr)
    if not s: return {"islem": 0}
    L = bt.stats([t for t in tr if t["side"] == "LONG"]); S = bt.stats([t for t in tr if t["side"] == "SHORT"])
    net = sorted(t["net"] for t in tr)
    f = lambda x: None if x is None or x == float("inf") else round(float(x), 3)
    return {"islem": s["n"], "kazanma": round(s["win%"], 1), "ort": round(s["avg%"], 3), "PF": f(s["pf"]), "toplam": round(s["sum%"], 1),
            "ort_kazanc": round(s["avg_win"], 2), "ort_kayip": round(s["avg_loss"], 2), "en_iyi": round(s["best%"], 1), "en_kotu": round(s["worst%"], 1),
            "en5siz": round(sum(net[:-5]), 1) if len(net) > 5 else None,
            "long": {"islem": L["n"], "PF": f(L["pf"]), "toplam": round(L["sum%"], 1)} if L else None,
            "short": {"islem": S["n"], "PF": f(S["pf"]), "toplam": round(S["sum%"], 1)} if S else None}


def run_scenario(name, data, start, a, vol_min):
    allt = []
    for sym, df in data.items():
        warm = int(df.index.searchsorted(pd.Timestamp(start)))
        ev, d = bt.prep(df); fa, fp = bt.filt_arrays(sym, df); rv, vw = bt.vol_arrays(sym, df, 0)
        for t in bt.simulate(df, a.fee, warm, emerg=a.emerg, events=ev, d=d, adx_arr=fa, atrp_arr=fp, relvol=rv, vol_min=vol_min, maxstop=a.maxstop, whipsaw=a.whipsaw, confirm=a.confirm):
            t["symbol"] = sym; allt.append(t)
    res = {"ad": name, "hacim_filtresi": vol_min, "genel": block(allt), "coin": {}, "yil": {}}
    for sym in data: res["coin"][sym] = block([t for t in allt if t["symbol"] == sym])
    for y in sorted({t["t"].year for t in allt}): res["yil"][str(y)] = block([t for t in allt if t["t"].year == y])
    eq, mdd, curve = account_sim(allt, a.risk, a.cap, a.balance, a.maxstop)
    mc = pd.Series([c[1] for c in curve], index=[c[0] for c in curve]).resample("QE").last().ffill() if curve else pd.Series(dtype=float)
    res["hesap"] = {"baslangic": a.balance, "bitis": round(eq, 2), "getiri_%": round((eq / a.balance - 1) * 100, 1), "maxDD_%": round(mdd, 1),
                    "risk_%": a.risk, "ust_sinir_%": a.cap, "egri_ceyrek": [[str(k.date()), round(float(v), 2)] for k, v in mc.items()]}
    return res, allt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT")
    ap.add_argument("--start", default="2020-08-01"); ap.add_argument("--end", default="2026-11-14")
    ap.add_argument("--fee", type=float, default=0.06, help="yon basina komisyon+kayma %% (sunumdaki: 0.04 komisyon + 0.02 kayma)"); ap.add_argument("--emerg", type=float, default=sts.EMERGENCY_STOP_PCT)
    ap.add_argument("--maxstop", type=float, default=sts.MAX_STOP_DIST_PCT); ap.add_argument("--volmult", type=float, default=sts.MIN_REL_VOL)
    ap.add_argument("--risk", type=float, default=0.25); ap.add_argument("--cap", type=float, default=25.0); ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--confirm", type=int, default=0, help="onay kurali: sinyalden N mum sonra, arada ters donus yoksa gir (0=kapali, canli: 5)"); ap.add_argument("--whipsaw", type=int, default=0, help="testere filtresi: son N mumda ters yonlu ST donusu varsa girme (0=kapali, canli: 16)"); ap.add_argument("--mult", type=float, default=None, help="Supertrend carpani (varsayilan: canli ayar)"); ap.add_argument("--tf", default="1h", choices=["15m","1h","4h"], help="mum periyodu (15 dakikalik onbellekten birlestirilir, yeniden indirmez)"); ap.add_argument("--out", default="backtest_yeni"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.mult: sts.ST_MULT = a.mult
    syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    data = {}
    for s in syms:
        if a.selftest:
            rng = np.random.default_rng(abs(hash(s)) % 2**32); n = 60000
            r = np.cumsum(rng.normal(0, 0.003, n)) + np.sin(np.arange(n) / 3000) * 0.4
            c = 100 * np.exp(r); idx = pd.date_range(a.start, periods=n, freq="15min")
            df = pd.DataFrame({"open": c * (1 + rng.normal(0, .0005, n)), "close": c, "volume": rng.uniform(50, 150, n) * (1 + 2 * (rng.random(n) < .1))}, index=idx)
            df["high"] = np.maximum(df.open, df.close) * 1.002; df["low"] = np.minimum(df.open, df.close) * .998
            df = pd.concat([pd.DataFrame(df.iloc[:600].values, columns=df.columns, index=idx[:600] - pd.Timedelta(days=7)), df]) if False else df
        else:
            print(f"{s}: indiriliyor/okunuyor...", flush=True); df = load(s, a.start, a.end)
        if a.tf != "15m":
            rule = {"1h": "1h", "4h": "4h"}[a.tf]
            df = df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        data[s] = df; print(f"  {s}: {len(df)} mum, {df.index[0]} -> {df.index[-1]}", flush=True)
    print(f"Supertrend {sts.ST_PERIOD}/{sts.ST_MULT:g} | kilit [{sts.lock_tiers_text()}] | acil %{a.emerg:g} | stop siniri %{a.maxstop:g} | goreli hacim>={a.volmult:g}\n")
    out = {"olusturma": time.strftime("%Y-%m-%d %H:%M"), "baslangic": a.start, "bitis": a.end, "mum": a.tf, "supertrend": f"{sts.ST_PERIOD}/{sts.ST_MULT:g}", "testere_mum": a.whipsaw, "onay_mum": a.confirm,
           "komisyon_kayma_yon_%": a.fee, "acil_stop_%": a.emerg, "stop_siniri_%": a.maxstop, "kilit": sts.lock_tiers_text(),
           "coin_baslangic": {s: str(d.index[0].date()) for s, d in data.items()}, "senaryolar": []}
    lines = []; trades_A = None
    for name, vm in (("A) Canli ayarlar (hacim filtreli)", a.volmult), ("B) Hacim filtresiz", 0.0)):
        res, tr = run_scenario(name, data, a.start, a, vm); out["senaryolar"].append(res)
        if trades_A is None: trades_A = tr
        g = res["genel"]; h = res["hesap"]
        lines.append(f"== {name} ==")
        lines.append(f"Genel: {g.get('islem')} islem, kazanma %{g.get('kazanma')}, PF {g.get('PF')}, toplam {g.get('toplam')}% (fiyat uzerinden, kaldiracsiz), en5'siz {g.get('en5siz')}%, en kotu {g.get('en_kotu')}%")
        lines.append(f"Hesap simulasyonu ({a.balance:g} USD, risk %{a.risk}): bitis {h['bitis']} USD ({h['getiri_%']:+}%), maxDD {h['maxDD_%']}%")
        for y, b in res["yil"].items(): lines.append(f"  {y}: n={b.get('islem')} kaz=%{b.get('kazanma')} PF={b.get('PF')} toplam={b.get('toplam')}%")
        for s, b in res["coin"].items(): lines.append(f"  {s}: n={b.get('islem')} kaz=%{b.get('kazanma')} PF={b.get('PF')} toplam={b.get('toplam')}%")
        lines.append("")
    print("\n".join(lines))
    json.dump(out, open(a.out + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(a.out + ".txt", "w", encoding="utf-8").write("\n".join(lines))
    pd.DataFrame(trades_A).to_csv(a.out + "_islemler.csv", index=False)
    print(f"Kaydedildi: {a.out}.json, {a.out}.txt, {a.out}_islemler.csv")


if __name__ == "__main__":
    main()
