# FINAL V1 Multi-Coin Dashboard V6

Bu sürüm mevcut Paper Trading Dashboard'u korur ve içine Binance Futures USDT-M perpetual coin scanner ekler.

## Scanner
- Binance Futures USDⓈ-M `TRADING` + `PERPETUAL` + `USDT` sözleşmeleri otomatik keşfedilir.
- 4H sinyal yalnızca son kapanmış mumdan hesaplanır; oluşan mum kullanılmaz.
- Mevcut `strategy.py` ve `config.py` kuralları aynen kullanılır.
- 1D volatilite veto aktifse yalnızca aday sinyal oluşan coinlerde kontrol edilir.
- Gerçek emir yoktur; scanner sadece gözlem/sinyal üretir.
- Coin listesi ve 24s ticker bilgisi Binance public REST API'den alınır.
- İlk tarama dashboard başlarken arka planda başlar; sonra yaklaşık 15 dakikada bir cache süresi dolduğunda yenilenir.
- Dashboard'dan `Tümünü Tara` ile manuel tarama yapılabilir.

## Dashboard endpoints
- `/` dashboard
- `/api/status` paper trading durumu
- `/api/scanner` scanner durumu ve sonuçlar
- `/api/scanner/scan?force=1` manuel tarama başlatır
- `/health` sağlık kontrolü

## Railway
Dockerfile otomatik olarak `python paper_trading.py` çalıştırır.
`runtime.txt`, `railway.toml`, `Procfile` ve `__pycache__` paketlenmez.

## Önemli
Scanner hiçbir coinde otomatik pozisyon açmaz. ETH paper trading de aynen paper-only kalır.
