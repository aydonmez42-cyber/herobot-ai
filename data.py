"""
BIST veri katmani.

Sembol listesi once canli kaynaktan alinir (isyatirimhisse), olmazsa asagidaki
statik listeye duser. Fiyat verisi yfinance uzerinden ".IS" ekiyle cekilir ve
diske parquet olarak onbelleklenir, boylece gun icinde tekrar tarama yaparken
600 hisseyi bastan indirmezsiniz.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(os.environ.get("BIST_CACHE", Path.home() / ".bist_screener_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# BIST 100 + yuksek hacimli isimler. Canli liste cekilemezse bu kullanilir.
FALLBACK_SYMBOLS = [
    "AEFES", "AGHOL", "AKBNK", "AKCNS", "AKFGY", "AKFYE", "AKSA", "AKSEN",
    "ALARK", "ALBRK", "ALFAS", "ANSGR", "ARCLK", "ASELS", "ASTOR", "ASUZU",
    "AYDEM", "AYGAZ", "BERA", "BIENY", "BIMAS", "BIOEN", "BOBET", "BRSAN",
    "BRYAT", "BUCIM", "CANTE", "CCOLA", "CEMTS", "CIMSA", "CWENE", "DOAS",
    "DOHOL", "ECILC", "ECZYT", "EGEEN", "EKGYO", "ENERY", "ENJSA", "ENKAI",
    "EREGL", "EUPWR", "EUREN", "FROTO", "GARAN", "GENIL", "GESAN", "GLYHO",
    "GUBRF", "GWIND", "HALKB", "HEKTS", "IPEKE", "ISCTR", "ISDMR", "ISGYO",
    "ISMEN", "IZMDC", "KARSN", "KAYSE", "KCAER", "KCHOL", "KLSER", "KMPUR",
    "KONTR", "KONYA", "KORDS", "KOZAA", "KOZAL", "KRDMD", "MAVI", "MGROS",
    "MIATK", "MPARK", "ODAS", "OTKAR", "OYAKC", "PENTA", "PETKM", "PGSUS",
    "PSGYO", "QUAGR", "SAHOL", "SASA", "SDTTR", "SELEC", "SISE", "SKBNK",
    "SMRTG", "SOKM", "TAVHL", "TCELL", "THYAO", "TKFEN", "TOASO", "TSKB",
    "TTKOM", "TTRAK", "TUKAS", "TUPRS", "TURSG", "ULKER", "VAKBN", "VESBE",
    "VESTL", "YEOTK", "YKBNK", "YYLGD", "ZOREN",
]


LAST_SOURCE = "?"        # son cagrida hangi kaynagin kullanildigi


def _from_tradingview(timeout: int = 20) -> list[str]:
    """
    TradingView screener'inin kendi uc noktasi. Kimlik dogrulama istemez,
    tum BIST paylarini tek istekte doner.
    """
    import json
    import urllib.request

    payload = json.dumps({
        "filter": [{"left": "type", "operation": "equal", "right": "stock"}],
        "options": {"lang": "tr"},
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": ["name"],
        "sort": {"sortBy": "name", "sortOrder": "asc"},
        "range": [0, 1500],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://scanner.tradingview.com/turkey/scan",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; bist-screener/1.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))

    out = []
    for row in body.get("data", []):
        code = (row.get("d") or [None])[0] or row.get("s", "").split(":")[-1]
        code = str(code).strip().upper()
        # Ana pazar paylari 4-5 harflidir; varant/sertifika/BYF'leri eler
        if code.isalpha() and 4 <= len(code) <= 5:
            out.append(code)
    return sorted(set(out))


def _from_isyatirim() -> list[str]:
    from isyatirimhisse import fetch_stock_list  # type: ignore

    data = fetch_stock_list()
    col = "CODE" if "CODE" in data.columns else data.columns[0]
    syms = {str(x).strip().upper() for x in data[col] if str(x).strip()}
    return sorted(s for s in syms if s.isalpha() and 4 <= len(s) <= 5)


def get_symbols(full_market: bool = True) -> list[str]:
    """
    Tum BIST pay listesini sirayla su kaynaklardan dener:
      1. TradingView screener uc noktasi (ek paket gerektirmez)
      2. isyatirimhisse paketi (kuruluysa)
      3. Repodaki yedek liste
    Kullanilan kaynak LAST_SOURCE degiskeninde tutulur.
    """
    global LAST_SOURCE

    if full_market:
        for isim, fn in [("TradingView", _from_tradingview),
                         ("Is Yatirim", _from_isyatirim)]:
            try:
                syms = fn()
            except Exception:
                continue
            if len(syms) > 150:
                LAST_SOURCE = f"{isim} ({len(syms)} pay)"
                return syms

    LAST_SOURCE = f"yedek liste ({len(FALLBACK_SYMBOLS)} pay)"
    return FALLBACK_SYMBOLS


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.parquet"


def download(symbols: list[str], years: int = 4, use_cache: bool = True,
             progress_cb=None, interval: str = "1d") -> dict[str, pd.DataFrame]:
    """
    Gunluk OHLCV verisi indirir. Cikti: {sembol: DataFrame}.
    Onbellek ayni gun icinde tekrar indirmeyi engeller.

    `interval` parametresi crypto_data.download ile ayni imzayi korumak icin
    var (dashboard'daki Zaman dilimi secici ikisini de ayni sekilde cagirir).
    BIST tarafinda yfinance'ten sadece gunluk bar cekiliyor; su an "1d" disinda
    bir deger gelirse yok sayilir.
    """
    import yfinance as yf

    today = dt.date.today()
    start = today - dt.timedelta(days=365 * years + 30)
    result: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []

    for s in symbols:
        p = _cache_path(s)
        if use_cache and p.exists():
            age = dt.date.fromtimestamp(p.stat().st_mtime)
            if age == today:
                try:
                    result[s] = pd.read_parquet(p)
                    continue
                except Exception:
                    pass
        to_fetch.append(s)

    batch_size = 40
    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        tickers = [f"{s}.IS" for s in batch]
        try:
            raw = yf.download(
                tickers, start=start.isoformat(), interval="1d",
                group_by="ticker", auto_adjust=False, threads=True,
                progress=False,
            )
        except Exception:
            continue

        for s, t in zip(batch, tickers):
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                sub = sub.rename(columns=str.lower)[
                    ["open", "high", "low", "close", "volume"]
                ].dropna()
                if len(sub) < 400:      # EMA365 icin yeterli gecmis yoksa atla
                    continue
                sub.to_parquet(_cache_path(s))
                result[s] = sub
            except Exception:
                continue

        if progress_cb:
            progress_cb(min(i + batch_size, len(to_fetch)), len(to_fetch))

    return result
