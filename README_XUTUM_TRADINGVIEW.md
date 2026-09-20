# TEST32 RSI72 — XUTUM / Native TradingView 4H Scanner

Bu sürüm BIST 100/BIST Tüm-100 birleştirmesi kullanmaz. XUTUM evreni TradingView Turkey Screener üzerinden dinamik olarak alınır.

## Veri kaynağı
- Evren: TradingView Turkey Screener, aktif BIST common stocks
- OHLCV: TradingView Chart WebSocket
- 4H: native TradingView `240` dakika serisi
- Günlük volatilite filtresi açıksa günlük ATRP de TradingView `1D` serisinden alınır

TradingView XUTUM teknik ekranında 4 saatlik zaman dilimi ayrı bir timeframe olarak sunulmaktadır. citeturn0search0

## Mum kuralı
TradingView bar timestamp'i barın başlangıcıdır. Scanner 240 dakikalık BIST barını +4 saat sonrasında kapalı kabul eder. Böylece sadece kapanmış 4H mum TEST32 RSI72 sinyaline girer.

## Strateji
TEST32 RSI72 parametreleri aynen korunur. Bu değişiklik yalnızca BIST veri kaynağını Yahoo 1H→özel 4H üretiminden TradingView native 4H verisine taşır.

Gerçek emir yoktur. SHORT yalnızca teknik sinyaldir.

## Not
TradingView WebSocket resmi chart veri beslemesinde kullanılan özel protokol üzerinden native chart serisi alınır; protokol ve `create_series` yöntemi kamuya açık teknik örneklerde dokümante edilmiştir.
