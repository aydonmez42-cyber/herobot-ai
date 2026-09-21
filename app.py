"""
Long Tarayici — dashboard (BIST + Binance Futures).
DI+ / DI- (ADX/DMI) kesisim sistemi.

Calistirmak icin:
    streamlit run bist_screener/app.py
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from bist_screener import data as bist_data
from bist_screener import crypto_data
from bist_screener import pwa
from bist_screener.engine import Settings, compute, last_row_summary
from bist_screener.scan import _label

TZ = ZoneInfo("Europe/Istanbul")

st.set_page_config(page_title="Long Tarayıcı", page_icon="◆",
                   layout="wide", initial_sidebar_state="auto")

# Vurgu renkleri hem koyu hem acik temada okunacak sekilde secildi.
# Zemin ve yazi renkleri Streamlit temasindan gelir (var(--...)), boylece
# sag ustteki menuden temayi degistirdiginizde arayuz de birlikte degisir.
UP, DOWN, ACCENT = "#1FB47C", "#E35240", "#12AEB4"
BLUE, VIOLET, GRID = "#4A8FE7", "#8B6FE0", "#8A9AAB"

st.markdown("""
<style>
  .block-container { padding-top: 2.1rem; max-width: 1720px;
                     padding-left: 2.2rem; padding-right: 2.2rem; }

  /* Kenar cubugu etiketleri tam kontrastta */
  section[data-testid="stSidebar"] label,
  section[data-testid="stSidebar"] .stMarkdown p {
      color: var(--text-color, #EDF3F9) !important;
      font-size: .87rem; opacity: 1;
  }
  section[data-testid="stSidebar"] h3 {
      font-size: .73rem; letter-spacing: .08em; text-transform: uppercase;
      opacity: .72; margin: 1.5rem 0 .5rem; font-weight: 600;
  }
  .hint { font-size: .74rem; opacity: .62; margin: -.35rem 0 .7rem; }

  /* Piyasa secici (BIST / Kripto). st.container(key=...) Streamlit'te
     "st-key-<key>" sinifini otomatik ekler, buradan hedefliyoruz. */
  .st-key-piyasa_kutusu [role="radiogroup"] { gap: .4rem; }
  .st-key-piyasa_kutusu label {
      border: 1px solid rgba(128,150,175,.32); border-radius: 7px;
      padding: .3rem .9rem !important; margin: 0 !important;
  }

  .hdr { display:flex; align-items:baseline; gap:.7rem; margin-bottom:.15rem; }
  .hdr h1 { font-size:1.6rem; font-weight:650; letter-spacing:-.02em; margin:0; }
  .hdr .badge { font-size:.68rem; letter-spacing:.08em; text-transform:uppercase;
                color:#12AEB4; border:1px solid #12AEB477; border-radius:3px;
                padding:2px 7px; }
  .sub { opacity:.66; font-size:.85rem; margin-bottom:1.3rem; }

  .cards { display:grid; grid-template-columns:repeat(4,1fr); gap:.7rem;
           margin:.2rem 0 .4rem; }
  .card { background:var(--secondary-background-color, #1B2836);
          border:1px solid rgba(128,150,175,.28); border-radius:7px;
          padding:.85rem 1rem; }
  .card .n { font-size:1.8rem; font-weight:600; line-height:1.05;
             font-variant-numeric:tabular-nums; }
  .card .l { font-size:.74rem; opacity:.66; margin-top:.25rem; }

  .meta { opacity:.66; font-size:.78rem; margin:.6rem 0 1.1rem; }
  .meta b { opacity:1; font-weight:600; }

  .sect { font-size:.73rem; letter-spacing:.08em; text-transform:uppercase;
          opacity:.66; font-weight:600;
          border-top:1px solid rgba(128,150,175,.28);
          padding-top:1.1rem; margin:1.8rem 0 .8rem; }

  [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

  /* ---------- Telefon duzeni ---------- */
  .mobil-liste { display:none; }

  @media (max-width: 820px) {
      .block-container { padding-top:1.2rem; padding-left:.8rem;
                         padding-right:.8rem; }
      .cards { grid-template-columns:repeat(2,1fr); gap:.5rem; }
      .card { padding:.65rem .75rem; }
      .card .n { font-size:1.45rem; }
      .hdr h1 { font-size:1.28rem; }
      .sub { font-size:.8rem; margin-bottom:1rem; }

      /* Genis tablo telefonda okunmuyor; yerine kart listesi */
      .st-key-tablo_genis { display:none !important; }
      .mobil-liste { display:block; }
  }

  @media (min-width: 821px) { .mobil-liste { display:none !important; } }

  /* Hisse kartlari */
  .hk { background:var(--secondary-background-color, #1B2836);
        border:1px solid rgba(128,150,175,.28); border-radius:8px;
        padding:.7rem .8rem; margin-bottom:.5rem; }
  .hk-ust { display:flex; justify-content:space-between; align-items:baseline;
            gap:.5rem; }
  .hk-ad { font-size:1.02rem; font-weight:650; letter-spacing:.01em; }
  .hk-fiyat { font-size:1.0rem; font-weight:600;
              font-variant-numeric:tabular-nums; }
  .hk-rozet { display:inline-block; font-size:.7rem; font-weight:650;
              padding:2px 8px; border-radius:3px; margin:.4rem .3rem 0 0;
              letter-spacing:.02em; }
  .hk-alt { display:flex; flex-wrap:wrap; gap:.15rem .9rem; font-size:.73rem;
            opacity:.72; font-variant-numeric:tabular-nums; margin-top:.5rem; }
  .hk-alt b { font-weight:600; opacity:1; }
</style>
""", unsafe_allow_html=True)


def _auth_yapilandirilmis_mi() -> bool:
    """secrets.toml icinde [auth] blogu ve client_id doluysa True doner."""
    try:
        return bool(st.secrets.get("auth", {}).get("client_id"))
    except Exception:
        return False


def _izinli_mi(email: str) -> bool:
    """
    ALLOWED_EMAILS ortam degiskeni virgulle ayrilmis e-postalar ve/veya
    "@sirket.com" seklinde alan adlari icerir. Degisken bossa hic kimse
    giremez (varsayilan: kapali kayit) — bu bilincli bir tercih, cunku izin
    listesi bu urunun tek erisim kontrolu.

    Test/deneme amacli bir kacis kapisi: ALLOWED_EMAILS tam olarak "*" ise
    (baska hicbir sey degil, sadece yildiz), herkesin girisine izin verilir.
    Bu, varsayilan "bos = kapali" davranisini DEGISTIRMEZ — sadece bilinçli
    olarak * yazildiginda devreye girer. Uretimde/gercek kullanicilar icin
    onerilmez; sadece siz test ederken kullanin, sonra gercek e-posta
    listenizle degistirin.
    """
    izinli = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not izinli:
        return False
    if izinli == "*":
        return True
    email = (email or "").strip().lower()
    kurallar = [k.strip().lower() for k in izinli.split(",") if k.strip()]
    return any(
        email == k or (k.startswith("@") and email.endswith(k))
        for k in kurallar
    )


def gate() -> None:
    """
    Auth0 uzerinden e-posta/sifre ve Google ile giris + e-posta izin listesi.

    st.login() kullaniciyi Auth0'in kendi barindirdigi giris sayfasina
    yonlendirir; o sayfada hem e-posta/sifre formu hem "Google ile devam et"
    butonu birlikte gorunur (Auth0 Universal Login) — Streamlit tarafinda
    ayrica form yazmaya gerek yok. Giristen sonra ALLOWED_EMAILS listesinde
    olmayan hesaplar dashboard'u goremez.
    """
    if not _auth_yapilandirilmis_mi():
        st.markdown('<div class="hdr"><h1>Long Tarayıcı</h1></div>',
                    unsafe_allow_html=True)
        st.error("Giriş sistemi henüz yapılandırılmadı. README'deki Auth0 "
                 "kurulum adımlarını tamamlayın ve Railway'e gerekli ortam "
                 "değişkenlerini ekleyin.")
        st.stop()

    if not st.user.is_logged_in:
        st.markdown('<div class="hdr"><h1>Long Tarayıcı</h1></div>',
                    unsafe_allow_html=True)
        st.write("Devam etmek için giriş yapın ya da üye olun.")
        if st.button("Giriş yap / Üye ol", type="primary"):
            st.login()
        st.stop()

    if not _izinli_mi(st.user.get("email", "")):
        st.markdown('<div class="hdr"><h1>Long Tarayıcı</h1></div>',
                    unsafe_allow_html=True)
        st.warning(
            f"**{st.user.get('email', '')}** ile giriş yaptınız, ama bu "
            "hesaba erişim tanımlı değil. Erişim talep etmek için yönetici "
            "ile iletişime geçin.")
        if st.button("Çıkış yap"):
            st.logout()
        st.stop()


gate()
pwa.enable("#111A24")

# ------------------------------------------------------------- piyasa secimi
# Her iki veri modulu de ayni arayuzu sunar: get_symbols(full_market), download(),
# LAST_SOURCE. Buradaki sozluk sadece hangi modulun ve hangi metin/varsayilanin
# kullanilacagina karar verir; hesaplama motoru (engine.compute) ikisi icin de
# birebir aynidir.
MARKETS = {
    "BIST": dict(
        modul=bist_data, baslik="BIST Sinyal Tarayıcı", birim="Hisse",
        hacim_birimi="lot", hacim_varsayilan=500_000, hacim_adim=100_000,
        hacim_max=100_000_000, evren_ac="Tüm BIST", evren_yedek="Yedek liste",
        dosya_onek="bist", ornek_sembol="THYAO",
        # (kod, etiket) - tek secenek varsa sidebar'da zaman dilimi kutusu
        # hic gosterilmez, gereksiz ayarla kalabalik edilmez.
        zaman_dilimleri=[("1d", "Günlük")],
        aciklama="ADX DI+/DI- kesişim sistemiyle BIST hisseleri üzerinde "
                 "otomatik tarama: DI+ (yeşil) DI-'yi (kırmızı) yukarı "
                 "keserse AL, DI- DI+'yi yukarı keserse SAT sinyali üretir.",
    ),
    "Kripto": dict(
        modul=crypto_data, baslik="Kripto Sinyal Tarayıcı", birim="Coin",
        hacim_birimi="USDT", hacim_varsayilan=1_000_000, hacim_adim=500_000,
        hacim_max=20_000_000_000, evren_ac="Tüm Binance Futures",
        evren_yedek="Majör Coinler", dosya_onek="kripto", ornek_sembol="BTCUSDT",
        zaman_dilimleri=[("4h", "4 Saatlik"), ("1d", "Günlük")],
        aciklama="Aynı DI+/DI- kesişim sisteminin Binance USDT-M Futures "
                 "paritelerinde, seçtiğiniz zaman diliminde otomatik taraması.",
    ),
}

with st.container(key="piyasa_kutusu"):
    piyasa = st.radio("Piyasa", list(MARKETS.keys()), horizontal=True,
                      label_visibility="collapsed", key="piyasa_secim")
pcfg = MARKETS[piyasa]


@st.cache_data(ttl=1800, show_spinner=False)
def load_prices(piyasa_adi: str, symbols: tuple[str, ...],
                interval: str) -> dict[str, pd.DataFrame]:
    return MARKETS[piyasa_adi]["modul"].download(list(symbols), interval=interval)


@st.cache_data(ttl=1800, show_spinner=False)
def load_symbols(piyasa_adi: str, full: bool) -> tuple[list[str], str]:
    modul = MARKETS[piyasa_adi]["modul"]
    syms = modul.get_symbols(full_market=full)
    return syms, modul.LAST_SOURCE


@st.cache_data(ttl=1800, show_spinner=False, max_entries=40)
def hesapla(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detay grafigi icin DI+/DI-/ADX hesabi. Onbelleklenmesi sart: aksi halde
    her etkilesimde bastan hesaplanir ve Streamlit betik calisirken tum
    arayuzu kilitler — panel "pasif" gorunur.
    """
    return compute(df, Settings())


# --------------------------------------------------------------- kenar cubugu
with st.sidebar:
    uc1, uc2 = st.columns([3, 1])
    uc1.markdown(f'<div class="hint">👤 {st.user.get("email", "")}</div>',
                unsafe_allow_html=True)
    if uc2.button("Çıkış", use_container_width=True):
        st.logout()

    st.markdown("### Tarama ayarları")
    st.markdown('<div class="hint">Bunları değiştirdikten sonra taramayı '
                'yeniden çalıştırmanız gerekir.</div>', unsafe_allow_html=True)

    evren = st.radio(f"{pcfg['birim']} evreni", [pcfg["evren_ac"], pcfg["evren_yedek"]],
                     index=0, key=f"evren_{piyasa}")

    zd_secenekler = pcfg["zaman_dilimleri"]
    if len(zd_secenekler) > 1:
        zd_etiketler = [e for _, e in zd_secenekler]
        zd_secim = st.radio("Zaman dilimi", zd_etiketler, horizontal=True,
                            index=0, key=f"zaman_dilimi_{piyasa}",
                            help="Tarama hangi mum periyodunda çalışsın. "
                                 "4 Saatlik daha sık ve daha erken kesişim "
                                 "yakalar, karşılığında daha çok yanlış "
                                 "sinyal de üretebilir.")
        interval = {e: k for k, e in zd_secenekler}[zd_secim]
    else:
        interval = zd_secenekler[0][0]

    _bar_adi = "gün" if interval == "1d" else "4 saatlik bar"
    lookback = st.slider(
        "Sinyal tazeliği (bar)", 1, 5, 1,
        help="DI+/DI- kesişimi, kesildiği barda bir kez tetiklenir. 1 sadece "
             f"son kapanmış {_bar_adi}ü gösterir. 3 yaparsanız son üç "
             f"{_bar_adi} içinde kesişmiş {pcfg['birim'].lower()}ler de "
             "listeye girer — kaçırdığınız sinyalleri yakalarsınız, "
             "karşılığında liste eskir ve uzar.")

    st.markdown("### Liste filtresi")
    st.markdown('<div class="hint">Bunlar anında uygulanır, yeniden tarama '
                'gerektirmez.</div>', unsafe_allow_html=True)

    yon_secim = st.multiselect(
        "Sinyal yönü", ["AL", "SAT"], default=["AL", "SAT"],
        help="AL: DI+ DI-'yi yukarı kesti. SAT: DI- DI+'yi yukarı kesti.")
    min_adx = st.slider(
        "Minimum ADX", 0, 50, 20,
        help="ADX trendin gücünü ölçer, yönünü değil. 20'nin altı genelde "
             "yatay/kararsız piyasa demektir ve bu tür seyirde DI+/DI- "
             "kesişimleri sık yanlış çıkar. 20–25 vermek yatay seyredenleri "
             "eler; 0 hepsini geçirir.")
    min_hacim = st.number_input(
        f"Minimum hacim ({pcfg['hacim_birimi']})", 0, pcfg["hacim_max"],
        pcfg["hacim_varsayilan"], step=pcfg["hacim_adim"], key=f"min_hacim_{piyasa}")

    calistir = st.button("Taramayı çalıştır", type="primary",
                         use_container_width=True)

tarama_imzasi = (piyasa, evren, lookback, interval)
zd_etiket_secili = dict(pcfg["zaman_dilimleri"])[interval]

st.markdown(
    f'<div class="hdr"><h1>{pcfg["baslik"]}</h1>'
    f'<span class="badge">{zd_etiket_secili}</span></div>'
    f'<div class="sub">{pcfg["aciklama"]} Ön eleme aracıdır, yatırım tavsiyesi '
    'değildir.</div>',
    unsafe_allow_html=True,
)

if "sonuc" not in st.session_state:
    st.session_state.sonuc = None
    st.session_state.frames = {}
    st.session_state.imza = None

if calistir:
    syms, kaynak = load_symbols(piyasa, evren == pcfg["evren_ac"])
    with st.spinner(f"{len(syms)} {pcfg['birim'].lower()} için veri indiriliyor…"):
        frames = load_prices(piyasa, tuple(syms), interval)

    if not frames:
        st.error(
            f"Hiç veri indirilemedi. {piyasa} veri kaynağına bu sunucudan "
            "erişilemiyor olabilir (bölge kısıtlaması) ya da ağ sorunu var. "
            "Birkaç dakika sonra tekrar deneyin."
        )
        st.stop()

    cfg = Settings()
    rows = []
    bar = st.progress(0.0, text="DI+/DI- hesaplanıyor")
    for i, (sym, df) in enumerate(frames.items()):
        try:
            res = compute(df, cfg)
        except Exception:
            continue
        r = last_row_summary(sym, res, lookback=lookback)
        if r["AL"] or r["SAT"]:
            r["Sinyal"] = _label(r)
            rows.append(r)
        if i % 25 == 0:
            bar.progress(i / max(len(frames), 1), text="DI+/DI- hesaplanıyor")
    bar.empty()
    st.session_state.frames = frames
    st.session_state.sonuc = pd.DataFrame(rows)
    st.session_state.zaman = dt.datetime.now(TZ)
    st.session_state.evren_boyut = len(frames)
    st.session_state.kaynak = kaynak
    st.session_state.imza = tarama_imzasi
    st.session_state.piyasa_adi = piyasa
    st.session_state.interval = interval
    st.session_state.interval_etiket = zd_etiket_secili
    son_barlar = [x.index[-1] for x in frames.values()]
    st.session_state.veri_zaman = max(son_barlar, default=None)
    st.session_state.veri_tarihi = (
        st.session_state.veri_zaman.date() if st.session_state.veri_zaman is not None
        else None)

sonuc = st.session_state.sonuc
# Sonuclar, kenar cubugundaki GUNCEL secimden degil, TARANDIGI piyasadan
# etiketlenir — aksi halde piyasa degistirilip yeniden taranmadan once
# etiketler yanlis piyasayi gosterir.
pcfg_sonuc = MARKETS.get(st.session_state.get("piyasa_adi", piyasa), pcfg)

if sonuc is None:
    st.info("Soldaki panelden ayarları seçip **Taramayı çalıştır**'a basın. "
            "İlk tarama birkaç dakika sürer, sonrakiler önbellekten gelir.")
    st.stop()

if st.session_state.imza != tarama_imzasi:
    st.warning("Tarama ayarlarını değiştirdiniz. Aşağıdaki liste hâlâ eski "
               "ayarlarla üretildi — **Taramayı çalıştır**'a basın.")

# ---------------------------------------------------------------------- filtre
d = sonuc.copy()
if not d.empty:
    d = d[d["Sinyal"].isin(yon_secim)]
    d = d[(d["ADX"].fillna(0) >= min_adx) & (d["Hacim"] >= min_hacim)]
    d = d.sort_values(["Sinyal", "ADX"], ascending=[True, False])

# --------------------------------------------------------------- ozet kartlari
kartlar = [
    (st.session_state.evren_boyut, f"taranan {pcfg_sonuc['birim'].lower()}", "inherit"),
    (len(d), "sinyal veren", ACCENT),
    (int((d["Sinyal"] == "AL").sum()) if not d.empty else 0, "AL sinyali", UP),
    (int((d["Sinyal"] == "SAT").sum()) if not d.empty else 0, "SAT sinyali", DOWN),
]
st.markdown(
    '<div class="cards">'
    + "".join(f'<div class="card"><div class="n" style="color:{c}">{n}</div>'
              f'<div class="l">{lbl}</div></div>' for n, lbl, c in kartlar)
    + "</div>",
    unsafe_allow_html=True,
)

vt = st.session_state.veri_tarihi
vz = st.session_state.get("veri_zaman")
gunluk_mi = st.session_state.get("interval", "1d") == "1d"
bayat = bool(vt) and vt != dt.datetime.now(TZ).date()
if vz is not None:
    veri_metni = f'{vz:%d.%m.%Y}' if gunluk_mi else f'{vz:%d.%m.%Y %H:%M}'
else:
    veri_metni = ""
st.markdown(
    f'<div class="meta">Son tarama <b>{st.session_state.zaman:%d.%m.%Y %H:%M}</b>'
    f' · sembol kaynağı <b>{st.session_state.kaynak}</b>'
    f' · zaman dilimi <b>{st.session_state.get("interval_etiket", "Günlük")}</b>'
    + (f' · veri <b>{veri_metni}</b>' if veri_metni else "")
    + (f' <span style="color:{DOWN}">⚠ bugüne ait değil</span>' if bayat else "")
    + "</div>",
    unsafe_allow_html=True,
)

if d.empty:
    st.warning("Seçilen kriterlerde sinyal yok. Minimum ADX'i düşürmeyi "
               "veya sinyal tazeliğini artırıp yeniden taramayı deneyin.")
    st.stop()

# ----------------------------------------------------------------------- tablo
st.markdown(f'<div class="sect">Sinyal veren {pcfg_sonuc["birim"].lower()}ler'
           '</div>', unsafe_allow_html=True)

_kesisim_kolon = "Kaç gün önce" if gunluk_mi else "Kaç bar önce"
tab = d[["Hisse", "Sinyal", "Fiyat", "Degisim %", "DI+", "DI-", "ADX",
         "Kesisim Gun", "Hacim"]].copy()
tab["Hacim"] = (tab["Hacim"] / 1_000_000).round(2)
tab["Sinyal"] = tab["Sinyal"].map({"AL": "🟢 AL", "SAT": "🔴 SAT"})
tab = tab.rename(columns={
    "Hisse": "Sembol", "Degisim %": "Değ %", "Kesisim Gun": _kesisim_kolon,
    "Hacim": "Hacim M"})

# Genis ekran: tam tablo. Telefon: kart listesi. Ikisi de her zaman uretilir,
# hangisinin gorunecegine CSS medya sorgusu karar verir (Python ekran
# genisligini bilemez).
try:
    kap = st.container(key="tablo_genis")
except TypeError:            # eski Streamlit surumlerinde key destegi yok
    kap = st.container()

with kap:
    st.dataframe(
        tab, use_container_width=True, hide_index=True,
        height=min(600, 36 * (len(tab) + 1) + 8),
        column_config={
            "Sembol": st.column_config.TextColumn(width="small"),
            "Sinyal": st.column_config.TextColumn(
                width="small",
                help="AL — DI+ DI-'yi yukarı kesti. SAT — DI- DI+'yi yukarı kesti."),
            "Fiyat": st.column_config.NumberColumn(format="%.2f",
                                                   width="small"),
            "Değ %": st.column_config.NumberColumn(format="%+.2f", width="small",
                                                   help="Bir önceki bara göre değişim %"),
            "DI+": st.column_config.NumberColumn(
                format="%.0f", width="small", help="Yükseliş yönü gücü"),
            "DI-": st.column_config.NumberColumn(
                format="%.0f", width="small", help="Düşüş yönü gücü"),
            "ADX": st.column_config.NumberColumn(
                format="%.0f", width="small",
                help="Trend gücü (yön değil). 20 altı yatay seyir."),
            _kesisim_kolon: st.column_config.NumberColumn(
                format="%d", width="small",
                help="Kesişim kaç bar önce tetiklendi. 0 = son bar."),
            "Hacim M": st.column_config.NumberColumn(
                format="%.1f", width="small", help="Milyon"),
        },
    )

ROZET = {"AL": UP, "SAT": DOWN}


def _n(x, basamak: int = 0) -> str:
    """Kisa gecmisli hisselerde DI/ADX bos gelebilir; karta 'nan' yazmasin."""
    return "—" if pd.isna(x) else f"{x:.{basamak}f}"


_birim_kisa = "gün" if gunluk_mi else "bar"


def _kart(r: pd.Series) -> str:
    deg = 0.0 if pd.isna(r["Degisim %"]) else float(r["Degisim %"])
    dr = UP if deg >= 0 else DOWN
    ok = "▲" if deg >= 0 else "▼"
    renk = ROZET.get(r["Sinyal"], GRID)
    hacim = r["Hacim"] / 1_000_000
    return (
        f'<div class="hk">'
        f'<div class="hk-ust"><span class="hk-ad">{r["Hisse"]}</span>'
        f'<span class="hk-fiyat">{r["Fiyat"]:.2f}'
        f'<span style="color:{dr};font-size:.8rem;margin-left:.4rem">'
        f'{ok}{abs(deg):.2f}%</span></span></div>'
        f'<div><span class="hk-rozet" style="background:{renk}26;color:{renk}">'
        f'{r["Sinyal"]}</span></div>'
        f'<div class="hk-alt">'
        f'<span>DI+ <b>{_n(r["DI+"])}</b></span>'
        f'<span>DI- <b>{_n(r["DI-"])}</b></span>'
        f'<span>ADX <b>{_n(r["ADX"])}</b></span>'
        f'<span>Kesişim <b>{int(r["Kesisim Gun"])} {_birim_kisa} önce</b></span>'
        f'<span>Hacim <b>{_n(hacim, 1)}M</b></span>'
        f'</div></div>'
    )


st.markdown(
    '<div class="mobil-liste">'
    + "".join(_kart(r) for _, r in d.iterrows())
    + "</div>",
    unsafe_allow_html=True,
)

st.download_button("CSV indir", d.to_csv(index=False).encode("utf-8-sig"),
                   file_name=f"{pcfg_sonuc['dosya_onek']}_di_"
                            f"{dt.datetime.now(TZ):%Y%m%d}.csv",
                   mime="text/csv")

# ------------------------------------------------------------- genel gorunum
st.markdown('<div class="sect">Tüm Piyasa — Genel Görünüm</div>',
           unsafe_allow_html=True)

c1, c2 = st.columns([2, 1])
ara = c1.text_input(
    "Sembol ara", placeholder=f"Sembol ara — örn. {pcfg_sonuc['ornek_sembol']}",
                    label_visibility="collapsed")
sirala = c2.selectbox(
    "Sırala", ["Alfabetik", "En çok yükselen", "En çok düşen"],
    label_visibility="collapsed")

genel_satirlar = []
for sym, gdf in st.session_state.frames.items():
    if len(gdf) < 2:
        continue
    son = float(gdf["close"].iloc[-1])
    onceki = float(gdf["close"].iloc[-2])
    deg = (son / onceki - 1) * 100 if onceki else float("nan")
    genel_satirlar.append({"Hisse": sym, "Fiyat": son, "Değ %": deg})

genel = pd.DataFrame(genel_satirlar)
if ara.strip():
    genel = genel[genel["Hisse"].str.contains(ara.strip().upper())]
if sirala == "Alfabetik":
    genel = genel.sort_values("Hisse")
elif sirala == "En çok yükselen":
    genel = genel.sort_values("Değ %", ascending=False)
else:
    genel = genel.sort_values("Değ %", ascending=True)
genel = genel.reset_index(drop=True)


def _renk(v: float) -> str:
    if pd.isna(v):
        return ""
    return f"color:{UP};font-weight:600" if v >= 0 else f"color:{DOWN};font-weight:600"


def _ok(v: float) -> str:
    if pd.isna(v):
        return "—"
    return f"{'▲' if v >= 0 else '▼'} {v:+.2f}%"


gost = pd.DataFrame({
    "Sembol": genel["Hisse"],
    "Fiyat": genel["Fiyat"].round(2),
    "Değ %": genel["Değ %"],       # renklendirme icin sayisal kalir
})

st.dataframe(
    gost.style
        .map(_renk, subset=["Değ %"])
        .format({"Fiyat": "{:.2f}", "Değ %": _ok}),
    use_container_width=True, hide_index=True,
    height=min(560, 36 * (len(genel) + 1) + 8),
)
st.caption(f"{len(genel)} {pcfg_sonuc['birim'].lower()} listeleniyor.")

# ----------------------------------------------------------------------- detay
st.markdown(f'<div class="sect">{pcfg_sonuc["birim"]} detayı</div>',
           unsafe_allow_html=True)
secim = st.selectbox("Hisse", d["Hisse"].tolist(), label_visibility="collapsed")

df = st.session_state.frames.get(secim)
if df is not None:
    res = hesapla(df)
    tail, px = res.tail(180), df.tail(180)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.66, 0.34], vertical_spacing=0.035)
    fig.add_trace(go.Candlestick(
        x=px.index, open=px["open"], high=px["high"], low=px["low"],
        close=px["close"], name=secim,
        increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
    ), row=1, col=1)

    al_pts = tail[tail["AL_CROSS"]]
    if len(al_pts):
        fig.add_trace(go.Scatter(
            x=al_pts.index, y=al_pts["close"] * 0.965, mode="markers",
            name="AL", marker=dict(symbol="triangle-up", size=13, color=UP,
                                   line=dict(width=0))), row=1, col=1)
    sat_pts = tail[tail["SAT_CROSS"]]
    if len(sat_pts):
        fig.add_trace(go.Scatter(
            x=sat_pts.index, y=sat_pts["close"] * 1.035, mode="markers",
            name="SAT", marker=dict(symbol="triangle-down", size=13, color=DOWN,
                                    line=dict(width=0))), row=1, col=1)

    fig.add_trace(go.Scatter(x=tail.index, y=tail["di_plus"], name="DI+",
                             line=dict(color=UP, width=1.4)), row=2, col=1)
    fig.add_trace(go.Scatter(x=tail.index, y=tail["di_minus"], name="DI-",
                             line=dict(color=DOWN, width=1.4)), row=2, col=1)
    fig.add_trace(go.Scatter(x=tail.index, y=tail["adx"], name="ADX",
                             line=dict(color=GRID, width=1.2, dash="dot")),
                  row=2, col=1)
    fig.add_hline(y=min_adx, line=dict(color=GRID, width=1, dash="dash"),
                  opacity=.45, row=2, col=1)

    # Saydam zemin: grafik sayfanin temasini alir, tema degisince uyumlu kalir.
    fig.update_layout(
        height=460, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=GRID, size=11), margin=dict(l=4, r=4, t=4, b=4),
        xaxis_rangeslider_visible=False, hovermode="x unified",
        legend=dict(orientation="h", y=1.08, x=0, bgcolor="rgba(0,0,0,0)",
                    font=dict(size=10)),
    )
    fig.update_xaxes(gridcolor="rgba(138,154,171,.18)", zeroline=False)
    fig.update_yaxes(gridcolor="rgba(138,154,171,.18)", zeroline=False)
    st.plotly_chart(fig, use_container_width=True,
                    config={"displayModeBar": False})

    son = res.iloc[-1]
    yon = "AL" if son["di_plus"] > son["di_minus"] else "SAT"
    renk = UP if yon == "AL" else DOWN
    st.markdown(
        f'<div class="cards" style="grid-template-columns:repeat(5,1fr);">'
        f'<div class="card"><div class="n" style="color:{renk}">{yon}</div>'
        f'<div class="l">güncel yön</div></div>'
        f'<div class="card"><div class="n">{son["di_plus"]:.0f}</div>'
        f'<div class="l">DI+</div></div>'
        f'<div class="card"><div class="n">{son["di_minus"]:.0f}</div>'
        f'<div class="l">DI-</div></div>'
        f'<div class="card"><div class="n">{son["adx"]:.0f}</div>'
        f'<div class="l">ADX</div></div>'
        f'<div class="card"><div class="n">{int(son["kesisim_gun"])}</div>'
        f'<div class="l">{_birim_kisa} önce kesişti</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
