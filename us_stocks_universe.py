"""
S&P 500 + Nasdaq-100 ticker universe for the US stock scanner.

Unlike the BIST universe (bist_xutum_universe.py), which scrapes a live
source with a cached fallback, this list is a STATIC SNAPSHOT captured from
public index-constituent pages (Wikipedia's S&P 500 list, slickcharts.com's
Nasdaq-100 holdings) on 2026-09-19. Index membership changes a handful of
times a year (additions/removals on rebalance), so this list will drift
slowly out of date — it is not fetched live on every scan the way the BIST
universe is. Refresh it periodically by regenerating the two source lists
and re-running the dedup below.

get_us_symbols() mirrors get_xutum_symbols()'s (symbols, source) return
shape so bist_scanner-style scanners can use either interchangeably.
"""

SP500 = [
    'MMM','AOS','ABT','ABBV','ACN','ADBE','AMD','AES','AFL','A','APD','ABNB','AKAM','ALB','ARE','ALGN','ALLE','LNT','ALL','GOOGL','GOOG','MO','AMZN','AMCR','AEE','AEP','AXP','AIG','AMT','AWK','AMP','AME','AMGN','APH','ADI','AON','APA','APO','AAPL','AMAT','APP','APTV','ACGL','ADM','ARES','ANET','AJG','AIZ','T','ATO','ADSK','ADP','AZO','AVB','AVY','AXON','BKR','BALL','BAC','BAX','BDX','BRK.B','BBY','TECH','BIIB','BLK','BX','XYZ','BNY','BA','BKNG','BSX','BMY','AVGO','BR','BRO','BF.B','BLDR','BG','BXP','CHRW','CDNS','CPT','COF','CAH','CCL','CARR','CVNA','CASY','CAT','CBOE','CBRE','CDW','COR','CNC','CNP','CF','CRL','SCHW','CHTR','CVX','CMG','CB','CHD','CIEN','CI','CINF','CTAS','CSCO','C','CFG','CLX','CME','CMS','KO','CTSH','COHR','COIN','CL','CMCSA','FIX','COP','ED','STZ','CEG','COO','CPRT','GLW','CPAY','CTVA','CSGP','COST','CRH','CRWD','CCI','CSX','CMI','CVS','DHR','DRI','DDOG','DVA','DECK','DE','DELL','DAL','DVN','DXCM','FANG','DLR','DG','DLTR','D','DPZ','DASH','DOV','DOW','DHI','DTE','DUK','DD','ETN','EBAY','ECL','EIX','EW','EA','ELV','EME','EMR','ETR','EOG','EQT','EFX','EQIX','EQR','ERIE','ESS','EL','EG','EVRG','ES','EXC','EXE','EXPE','EXPD','EXR','XOM','FFIV','FDS','FICO','FAST','FRT','FDX','FIS','FITB','FSLR','FE','FISV','FLEX','F','FTNT','FTV','FOXA','FOX','BEN','FCX','GRMN','IT','GE','GEHC','GEV','GEN','GNRC','GD','GIS','GM','GPC','GILD','GPN','GL','GDDY','GS','HAL','HIG','HAS','HCA','DOC','HSIC','HSY','HPE','HLT','HD','HONA','HON','HRL','HST','HWM','HPQ','HUBB','HUM','HBAN','HII','IBM','IEX','IDXX','ITW','INCY','IR','PODD','INTC','IBKR','ICE','IFF','IP','INTU','ISRG','IVZ','INVH','IQV','IRM','JBHT','JBL','JKHY','J','JNJ','JCI','JPM','KVUE','KDP','KEY','KEYS','KMB','KIM','KMI','KKR','KLAC','KHC','KR','LHX','LH','LRCX','LVS','LDOS','LEN','LII','LLY','LIN','LYV','LMT','L','LOW','LULU','LITE','LYB','MTB','MPC','MAR','MLM','MRVL','MAS','MA','MKC','MCD','MCK','MDT','MRK','META','MET','MTD','MGM','MCHP','MU','MSFT','MAA','MRNA','TAP','MDLZ','MPWR','MNST','MCO','MS','MOS','MSI','MSCI','NDAQ','NTAP','NFLX','NEM','NWSA','NWS','NEE','NKE','NI','NDSN','NSC','NTRS','NOC','NCLH','NRG','NUE','NVDA','NVR','NXPI','ORLY','OXY','ODFL','OMC','ON','OKE','ORCL','OTIS','PCAR','PKG','PLTR','PANW','PSKY','PH','PAYX','PYPL','PNR','PEP','PFE','PCG','PM','PSX','PNW','PNC','PPG','PPL','PFG','PG','PGR','PLD','PRU','PEG','PTC','PSA','PHM','PWR','QCOM','DGX','RL','RJF','RTX','O','REG','REGN','RF','RSG','RMD','RVTY','HOOD','ROK','ROL','ROP','ROST','RCL','SPGI','CRM','SNDK','SBAC','SLB','STX','SRE','NOW','SHW','SPG','SWKS','SJM','SW','SNA','SOLV','SO','LUV','SWK','SBUX','STT','STLD','STE','SYK','SMCI','SYF','SNPS','SYY','TMUS','TROW','TTWO','TPR','TRGP','TGT','TEL','TDY','TER','TSLA','TXN','TPL','TXT','TMO','TJX','TKO','TTD','TSCO','TT','TDG','TRV','TRMB','TFC','TYL','TSN','USB','UBER','UDR','ULTA','UNP','UAL','UPS','URI','UNH','UHS','VLO','VEEV','VTR','VLTO','VRSN','VRSK','VZ','VRTX','VRT','VTRS','VICI','V','VST','VMC','WRB','GWW','WAB','WMT','DIS','WBD','WM','WAT','WEC','WFC','WELL','WST','WDC','WY','WSM','WMB','WTW','WDAY','WYNN','XEL','XYL','YUM','ZBRA','ZBH','ZTS',
]

NASDAQ100 = [
    'NVDA','AAPL','MSFT','AMZN','GOOGL','GOOG','SPCX','AVGO','META','TSLA','MU','AMD','WMT','ASML','INTC','CSCO','PLTR','COST','LRCX','AMAT','NFLX','PANW','ARM','SNDK','TXN','CRWD','KLAC','MRVL','LIN','AMGN','STX','QCOM','GILD','ADI','TMUS','PEP','SHOP','WDC','ISRG','VRTX','BKNG','FTNT','PDD','SBUX','ADP','APP','ADBE','ABNB','MELI','CEG','MAR','MNST','CSX','DASH','LITE','DDOG','INTU','REGN','CMCSA','CTAS','CDNS','MDLZ','SNPS','ROST','WBD','ORLY','HON','AEP','MSTR','PCAR','NBIS','MPWR','TER','NXPI','BKR','FAST','FANG','ALAB','HONA','WDAY','ADSK','XEL','CRWV','PYPL','CCEP','EXC','KDP','PAYX','RKLB','TRI','IDXX','MCHP','FER','TTWO','ROP','AXON','ODFL','DXCM','ALNY','GEHC','CPRT',
]

_SYMBOLS = sorted(set(SP500) | set(NASDAQ100))


# Dow Jones Industrial Average (30). STATIK YEDEK: TradingView'den canli liste alinamazsa kullanilir; endeks bilesimi
# zamanla degisir (son bilinen liste). Canli liste her zaman once denenir.
DOW30 = ['AAPL','AMGN','AMZN','AXP','BA','CAT','CRM','CSCO','CVX','DIS','GS','HD','HON','IBM','JNJ','JPM','KO',
         'MCD','MMM','MRK','MSFT','NKE','NVDA','PG','SHW','TRV','UNH','V','VZ','WMT']

# ---- Endeks bazli evren (canli: TradingView Screener 'symbolset'; yedek: kayitli liste) ----
import os, re, time, threading
import requests

TV_US_SCANNER_URL = os.environ.get('TRADINGVIEW_US_SCANNER_URL', 'https://scanner.tradingview.com/america/scan')
US_INDEXES = {
    'DJI': {'name': 'Dow Jones', 'sym': 'SYML:DJ;DJI', 'ok': (28, 32), 'static': DOW30},
    'NDX': {'name': 'Nasdaq 100', 'sym': 'SYML:NASDAQ;NDX', 'ok': (95, 110), 'static': NASDAQ100},
    'SPX': {'name': 'S&P 500', 'sym': 'SYML:SP;SPX', 'ok': (480, 520), 'static': SP500},
}
_US_CACHE_S = int(os.environ.get('US_INDEX_CACHE_SECONDS', '21600'))
_idx_lock = threading.Lock()
_idx_cache = {}


def _tv_us_payload(sym):
    return {'columns': ['name', 'description'], 'filter': [], 'filterOR': [], 'ignore_unknown_fields': False,
            'options': {'lang': 'en'}, 'price_conversion': {}, 'range': [0, 700],
            'sort': {'sortBy': 'name', 'sortOrder': 'asc'},
            'symbols': {'symbolset': [sym], 'query': {'types': []}, 'tickers': []}, 'markets': ['america']}


def get_us_index_symbols(code, force=False):
    """Dow Jones / Nasdaq 100 / S&P 500. Doner: (semboller, kaynak, approx, {sembol: borsa}).
    approx=True: canli liste alinamadi, kayitli (eski olabilen) liste kullanildi."""
    code = str(code).upper()
    meta = US_INDEXES[code]
    now = time.time()
    with _idx_lock:
        c = _idx_cache.get(code)
        if c and not force and now - c['ts'] < _US_CACHE_S:
            return list(c['symbols']), c['source'], c['approx'], dict(c['ex'])
    try:
        r = requests.post(TV_US_SCANNER_URL, json=_tv_us_payload(meta['sym']), timeout=30, headers={
            'Accept': 'application/json', 'Content-Type': 'text/plain;charset=UTF-8',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
            'Origin': 'https://www.tradingview.com', 'Referer': 'https://www.tradingview.com/'})
        r.raise_for_status()
        syms, ex = [], {}
        for row in (r.json().get('data') or []):
            full = str(row.get('s') or '').upper()
            exch, _, sym = full.partition(':')
            if sym and re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym) and sym not in ex:
                syms.append(sym); ex[sym] = exch
        lo, hi = meta['ok']
        if not (lo <= len(syms) <= hi):
            raise RuntimeError(f"{meta['name']} bilesen sayisi beklenenden farkli: {len(syms)}")
        out = (sorted(syms), f"TradingView {meta['name']} endeks bilesenleri ({len(syms)})", False, ex)
    except Exception as exc:
        stat = sorted(set(meta['static']))
        out = (stat, f"kayitli liste ({meta['name']}, {len(stat)}; canli liste alinamadi: {type(exc).__name__})", True, {})
    with _idx_lock:
        _idx_cache[code] = {'symbols': out[0], 'source': out[1], 'approx': out[2], 'ex': out[3], 'ts': time.time()}
    return list(out[0]), out[1], out[2], dict(out[3])


def get_us_symbols(force=False):
    """Mirrors bist_xutum_universe.get_xutum_symbols()'s (symbols, source)
    shape. `force` is accepted for interface compatibility but unused —
    this list is static, not re-fetched."""
    return list(_SYMBOLS), 'static-snapshot-2026-09-19 (S&P500+Nasdaq100)'
