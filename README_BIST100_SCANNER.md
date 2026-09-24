# BIST_TUM TEST32 RSI72 Scanner

Bu modül mevcut TEST32 RSI72 teknik sinyal mantığını BIST_TUM hisselerinde **yalnızca tarama/sinyal** amacıyla uygular.

## Kurallar
- 4H kapalı mum kullanılır.
- EMA50 / EMA100 trend filtresi
- Supertrend 10 / 7.8 trend filtresi
- ADX > 25
- LONG RSI >55 ve <=72
- SHORT RSI <30
- LONG MACD filtresi
- CCI: LONG >100, SHORT <-50
- Stoch RSI koşulları
- Mevcut 1D volatilite veto mantığı

## Veri
- BIST_TUM evreni öncelikle CNBC-E BIST_TUM sayfasından dinamik alınır.
- Kaynak alınamazsa paket içindeki fallback evren kullanılır.
- Fiyat verisi Yahoo Finance chart endpointinden saatlik alınır ve BIST seansına göre 4H barlara dönüştürülür.

## Önemli
Bu scanner BIST için backtest edilmiş bir strateji değildir. Crypto TEST32'nin BIST'e teknik olarak uygulanmış gözlem sürümüdür.
BIST'te 4H mum yapısı kriptodaki 7/24 4H mumlarla birebir aynı değildir; burada 10:00–14:00 ve 14:00–18:00 seans barları kullanılır.

SHORT sinyali yalnızca teknik sinyaldir. BIST spot piyasasında doğrudan açığa satış emri anlamına gelmez.

Gerçek emir yoktur.


Evren: BIST Tüm = BIST 100 + BIST Tüm-100. CNBC-E kaynakları çalışma anında birleştirilir. Borsa İstanbul kural setinde BIST Tüm Endeksi, Yıldız Pazar, Ana Pazar, Alt Pazar ve PÖİP paylarından oluşur; BIST Tüm-100 ise BIST Tüm içinde BIST 100 dışındaki paylardan oluşur.
