# live_trading.py dosyasında get_runtime_status() fonksiyonunun HEMEN ALTINA yapıştırın.

def clear_last_error(username):
    """Canlı işlem açılıp/kapatıldığında eski hata yazısını temizler.
    Yalnızca last_error alanlarına dokunur; pozisyon ve P&L kayıtları korunur."""
    with _lock:
        runtime = _load_runtime()
        rec = _get_user_runtime(runtime, username)
        rec['last_error'] = None
        rec['last_error_notified'] = None
        _save_runtime(runtime)
