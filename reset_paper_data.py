"""
Paper (deneme) hesabini sifirlar: acik paper pozisyonlari, izleme listesi, kapanan paper islemler (CSV),
bakiye = config.PAPER_ACCOUNT_INITIAL_CAPITAL (10.000 $).

CANLI (gercek para) kayitlarina DOKUNMAZ: live_trades.csv, live_runtime.json vb.
Guvenlik: bir uyenin ACIK canli pozisyonu varsa durur (cikis sinyalleri paper pozisyonlardan geldigi icin
paper kaydi silinirse gercek pozisyon sahipsiz kalir). Once canli pozisyonlari kapatin veya --force verin.

Kullanim (Railway shell / sunucuda, botu DURDURDUKTAN sonra):
    python reset_paper_data.py            # kontrol + sifirlama
    python reset_paper_data.py --force    # acik canli pozisyon olsa da sifirla
"""
import os
import sys
from datetime import datetime, timezone

import config as cfg
import state_store
import live_trading


def main():
    force = '--force' in sys.argv
    try:
        holders = live_trading.get_open_live_holders()
    except Exception as e:
        print(f'UYARI: acik canli pozisyonlar okunamadi ({type(e).__name__}: {e})')
        holders = None
    if holders and not force:
        print('DURDU: acik canli pozisyon var:', {k: len(v) for k, v in holders.items()})
        print('Once bunlari kapatin ya da bilerek devam etmek icin --force kullanin.')
        sys.exit(1)
    if holders is None and not force:
        print('DURDU: canli pozisyon durumu dogrulanamadi. --force ile devam edebilirsiniz.')
        sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    ctx = os.path.join(os.path.dirname(state_store.TRADES_FILE) or '.', 'entry_context.csv')
    for path in (state_store.TRADES_FILE, ctx):
        if os.path.exists(path):
            os.replace(path, f'{path}.bak-{stamp}')
            print(f'Yedeklendi ve temizlendi: {path} -> {path}.bak-{stamp}')
    state_store.reset_paper_trading(cfg.PAPER_ACCOUNT_INITIAL_CAPITAL)
    print(f'Paper hesap sifirlandi. Bakiye = {cfg.PAPER_ACCOUNT_INITIAL_CAPITAL:,.2f} $')


if __name__ == '__main__':
    main()
