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


def get_us_symbols(force=False):
    """Mirrors bist_xutum_universe.get_xutum_symbols()'s (symbols, source)
    shape. `force` is accepted for interface compatibility but unused —
    this list is static, not re-fetched."""
    return list(_SYMBOLS), 'static-snapshot-2026-09-19 (S&P500+Nasdaq100)'
