"""
Binance USD-M Futures - borsa tarafi ACIL STOP emri (Algo Service).

2025-12-09'dan beri STOP_MARKET gibi kosullu emirler /fapi/v1/order'da kabul edilmez (-4120);
yeni uc nokta: POST /fapi/v1/algoOrder (algoType=CONDITIONAL). Bu modul binance_live.py'ye DOKUNMAZ,
onun imzali istek yardimcilarini kullanir.

closePosition=true: tetiklenince o sembolun TUM pozisyonunu kapatir (miktar/reduceOnly gonderilmez).
Tek yonlu (one-way) hesap varsayilir - mevcut place_market_order de positionSide gondermiyor.
"""
import binance_live as blive


def _tick(symbol):
    f = blive._symbol_filters(symbol) or {}
    try:
        return float((f.get('PRICE_FILTER') or {}).get('tickSize') or 0)
    except Exception:
        return 0.0


def round_price(symbol, price):
    """Fiyati sembolun tickSize'ina yuvarlar; (deger, metin) doner. Bilimsel gosterim (1e-05) Binance'te reddedilir."""
    tick = _tick(symbol)
    if tick <= 0 or not price or price <= 0:
        return None, None
    dec = blive._decimals_from_step(tick)
    px = round(round(price / tick) * tick, dec)
    return px, format(px, f'.{dec}f')


def place_emergency_stop(api_key, api_secret, symbol, position_side, stop_price):
    """position_side: 'LONG' (SELL stop) veya 'SHORT' (BUY stop). Doner: (ok, algo_id | hata_metni)."""
    px, px_txt = round_price(symbol, stop_price)
    if not px:
        return False, f'{symbol} icin fiyat adimi okunamadi'
    params = {
        'algoType': 'CONDITIONAL', 'symbol': symbol,
        'side': 'SELL' if position_side == 'LONG' else 'BUY',
        'type': 'STOP_MARKET', 'triggerPrice': px_txt,
        'closePosition': 'true', 'workingType': 'MARK_PRICE',
    }
    r = blive._signed_request('POST', '/fapi/v1/algoOrder', api_key, api_secret, params)
    data, err = blive._json_or_error(r)
    if err:
        return False, err
    return True, (data or {}).get('algoId') or (data or {}).get('clientAlgoId') or 'ok'


def cancel_emergency_stop(api_key, api_secret, symbol, algo_id=None):
    """Acil stop emrini iptal eder. algo_id yoksa o sembolun TUM kosullu emirlerini iptal eder.
    Zaten tetiklenmis/iptal edilmis emir hata sayilmaz. Doner: (ok, mesaj)."""
    if algo_id and algo_id != 'ok':
        r = blive._signed_request('DELETE', '/fapi/v1/algoOrder', api_key, api_secret, {'algoId': algo_id})
    else:
        r = blive._signed_request('DELETE', '/fapi/v1/algoOpenOrders', api_key, api_secret, {'symbol': symbol})
    data, err = blive._json_or_error(r)
    if err and ('Unknown' in err or 'does not exist' in err or 'not found' in err.lower() or '-2011' in err):
        return True, 'zaten yok'
    return (err is None), (err or 'iptal edildi')
