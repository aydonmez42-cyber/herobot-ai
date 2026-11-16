"""ETH Supertrend taramasi: 12 carpan x 4 zaman dilimi (15m, 1h, 4h, 1d).

Calistirma (backtest_period.py ile ayni klasorde):
    python eth_grid.py
    python eth_grid.py --symbol ETHUSDT --maxstop 0 --out eth_grid

Veri: veri_onbellek/ETHUSDT_15m.csv varsa indirmez; yoksa Binance'ten indirir (yavas, ban riski icin ayni IP'de
robot varsa yerel bilgisayarda calistir). 1h/4h/1d mumlari 15 dakikalik veriden birlestirilir.
Ayarlar canli sistemle ayni: ATR 10, komisyon+kayma yon basina 0.06, acil stop %15, kar kilidi kademeleri,
goreli hacim >= 1.5 (A) ve hacim filtresiz (B), stop siniri varsayilan 0 (kapali).
Cikti: <out>.csv (tum satirlar), <out>.txt (okunabilir ozet).
"""
import argparse, sys, time
import numpy as np, pandas as pd
import supertrend_signal as sts
import backtest_tf as bt
import backtest_period as bp

MULTS = [3, 3.7, 4, 4.4, 4.7, 4.9, 5, 5.5, 5.9, 6, 6.5, 7]
TFS = ["15m", "1h", "4h", "1d"]


def resample(df, tf):
    if tf == "15m":
        return df
    return df.resample(tf).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def pf_of(tr):
    s = bt.stats(tr)
    return (s["pf"] if s else float("nan"))


def run(df, key, mult, vol_min, a):
    sts.ST_MULT = mult
    warm = int(df.index.searchsorted(pd.Timestamp(a.start)))
    ev, d = bt.prep(df)
    fa, fp = bt.filt_arrays(key, df)          # key = sembol|tf: onbellek zaman dilimine ozel
    rv, _ = bt.vol_arrays(key, df, 0)
    tr = bt.simulate(df, a.fee, warm, emerg=a.emerg, events=ev, d=d, adx_arr=fa, atrp_arr=fp, relvol=rv, vol_min=vol_min, maxstop=a.maxstop)
    return tr


def summarize(tr, a):
    s = bt.stats(tr)
    if not s:
        return None
    eq, mdd, _ = bp.account_sim(tr, a.risk, a.cap, a.balance, a.maxstop)
    yrs = {}
    for t in tr:
        yrs.setdefault(t["t"].year, []).append(t)
    pos_years = sum(1 for y, v in yrs.items() if sum(x["net"] for x in v) > 0)
    last2 = [t for t in tr if t["t"].year >= 2025]
    longs = [t for t in tr if t["side"] == "LONG"]; shorts = [t for t in tr if t["side"] == "SHORT"]
    pf = s["pf"] if s["pf"] != float("inf") else 99.0
    return {"islem": s["n"], "kazanma_%": round(s["win%"], 1), "PF": round(pf, 3), "toplam_%": round(s["sum%"], 1),
            "ort_kazanc": round(s["avg_win"], 2), "ort_kayip": round(s["avg_loss"], 2), "en_kotu_%": round(s["worst%"], 1),
            "hesap_getiri_%": round((eq / a.balance - 1) * 100, 1), "hesap_maxDD_%": round(mdd, 1),
            "karli_yil": f"{pos_years}/{len(yrs)}", "PF_2025_26": round(pf_of(last2), 3) if last2 else None,
            "PF_long": round(pf_of(longs), 3) if longs else None, "PF_short": round(pf_of(shorts), 3) if shorts else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--start", default="2020-08-01"); ap.add_argument("--end", default="2026-11-14")
    ap.add_argument("--fee", type=float, default=0.06); ap.add_argument("--emerg", type=float, default=sts.EMERGENCY_STOP_PCT)
    ap.add_argument("--maxstop", type=float, default=0.0, help="stop siniri %% (0 = kapali, canli varsayilan)")
    ap.add_argument("--volmult", type=float, default=sts.MIN_REL_VOL)
    ap.add_argument("--risk", type=float, default=0.25); ap.add_argument("--cap", type=float, default=25.0); ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--mults", default=",".join(f"{m:g}" for m in MULTS)); ap.add_argument("--tfs", default=",".join(TFS))
    ap.add_argument("--out", default="eth_grid"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    mults = [float(x) for x in a.mults.split(",") if x.strip()]; tfs = [x.strip() for x in a.tfs.split(",") if x.strip()]
    if a.selftest:
        rng = np.random.default_rng(7); n = 60000
        r = np.cumsum(rng.normal(0, 0.003, n)) + np.sin(np.arange(n) / 3000) * 0.4
        c = 100 * np.exp(r); idx = pd.date_range(a.start, periods=n, freq="15min")
        base = pd.DataFrame({"open": c * (1 + rng.normal(0, .0005, n)), "close": c, "volume": rng.uniform(50, 150, n) * (1 + 2 * (rng.random(n) < .1))}, index=idx)
        base["high"] = np.maximum(base.open, base.close) * 1.002; base["low"] = np.minimum(base.open, base.close) * .998
    else:
        print(f"{a.symbol}: okunuyor/indiriliyor...", flush=True)
        base = bp.load(a.symbol, a.start, a.end)
    print(f"{len(base)} adet 15m mum: {base.index[0]} -> {base.index[-1]}\n", flush=True)
    rows = []; t0 = time.time()
    for tf in tfs:
        df = resample(base, tf); key = f"{a.symbol}|{tf}"
        for vname, vm in (("A hacim filtreli", a.volmult), ("B filtresiz", 0.0)):
            for m in mults:
                tr = run(df, key, m, vm, a); s = summarize(tr, a)
                if s is None:
                    continue
                s.update({"zaman": tf, "carpan": m, "senaryo": vname}); rows.append(s)
        print(f"  {tf} bitti ({len(df)} mum, {time.time() - t0:.0f} sn)", flush=True)
    res = pd.DataFrame(rows)
    cols = ["senaryo", "zaman", "carpan", "islem", "kazanma_%", "PF", "toplam_%", "ort_kazanc", "ort_kayip", "en_kotu_%",
            "hesap_getiri_%", "hesap_maxDD_%", "karli_yil", "PF_2025_26", "PF_long", "PF_short"]
    res = res[cols]
    # komsu carpanlarla yumusatilmis PF: tek carpana ozel (uyum) mi, genis bolge mi?
    res["PF_komsu_ort"] = np.nan
    for (sc, tf), g in res.groupby(["senaryo", "zaman"]):
        g = g.sort_values("carpan"); pf = g["PF"].to_numpy()
        sm = [np.mean(pf[max(0, i - 1):i + 2]) for i in range(len(pf))]
        res.loc[g.index, "PF_komsu_ort"] = np.round(sm, 3)
    res.to_csv(a.out + ".csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    lines = [f"ETH Supertrend taramasi | ATR {sts.ST_PERIOD} | {a.start} -> {a.end} | komisyon+kayma yon basina %{a.fee} | acil stop %{a.emerg:g} | stop siniri %{a.maxstop:g}", ""]
    for sc in ("A hacim filtreli", "B filtresiz"):
        for tf in tfs:
            g = res[(res["senaryo"] == sc) & (res["zaman"] == tf)].sort_values("carpan")
            if g.empty:
                continue
            lines.append(f"== {sc} | {tf} ==")
            lines.append(g.drop(columns=["senaryo", "zaman"]).to_string(index=False)); lines.append("")
    A = res[res["senaryo"] == "A hacim filtreli"]
    top = A[A["islem"] >= 30].sort_values("PF_komsu_ort", ascending=False).head(10)
    lines.append("== En iyi 10 (hacim filtreli, en az 30 islem, komsu-ortalama PF'ye gore) ==")
    lines.append(top[["zaman", "carpan", "islem", "PF", "PF_komsu_ort", "toplam_%", "hesap_getiri_%", "hesap_maxDD_%", "karli_yil", "PF_2025_26"]].to_string(index=False))
    lines.append("")
    lines.append("== Zaman dilimi ozeti (hacim filtreli, en az 30 islemli carpanlar; az islemli satirlar guvenilmez) ==")
    sm = A[A["islem"] >= 30].groupby("zaman").agg(PF_ort=("PF", "mean"), PF_en_iyi=("PF", "max"), PF_en_kotu=("PF", "min"), karli_carpan=("PF", lambda x: int((x > 1).sum())), islem_ort=("islem", "mean"), hesap_getiri_ort=("hesap_getiri_%", "mean")).round(3)
    lines.append(sm.reindex([t for t in tfs if t in sm.index]).to_string())
    txt = "\n".join(lines)
    print("\n" + txt)
    open(a.out + ".txt", "w", encoding="utf-8").write(txt)


if __name__ == "__main__":
    main()
