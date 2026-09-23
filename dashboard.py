import csv, json, os, secrets, threading, time
from collections import defaultdict, deque
import requests
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config as cfg
from scanner import snapshot as scanner_snapshot, start_scan as scanner_start, ensure_background_scan, background_loop
from bist_scanner import snapshot as bist_scanner_snapshot, start_scan as bist_scanner_start, background_loop as bist_background_loop
from us_scanner import snapshot as us_scanner_snapshot, start_scan as us_scanner_start, background_loop as us_background_loop
from state_store import (

    STATE_FILE, TRADES_FILE,
    load_state as _load_shared_state,
    add_to_watchlist as _add_to_watchlist,
    remove_from_watchlist as _remove_from_watchlist,
)
import ai_analyst
import auth
import email_notifier
import live_trading
import telegram_link
import telegram_notifier as tg_notifier


PORT = int(os.environ.get('PORT', '8080'))
STARTING_EQUITY = float(os.environ.get('PAPER_INITIAL_CAPITAL', str(cfg.INITIAL_CAPITAL)))
SESSION_COOKIE = 'session_token'

# ---------------------------------------------------------------------------
# Per-IP rate limiting for the auth-adjacent endpoints (login, register,
# forgot/reset password) — the ones an attacker would hit for credential
# stuffing, brute-forcing a password, or mail-bombing someone's inbox via
# the reset-email flow. A simple in-memory sliding window is enough here:
# this process is the single source of truth for these routes (one Railway
# instance), so it needs no shared/external store, and a restart merely
# resets everyone's counters rather than opening a security hole.
# ---------------------------------------------------------------------------

_rate_limit_lock = threading.Lock()

_rate_limit_buckets = defaultdict(deque) # (bucket, ip) -> deque[timestamp, ...]

 

 

def _rate_limit_check(bucket, ip, limit, window_seconds):

    """Returns True if this request is allowed (and records it), False if

    the caller has exceeded `limit` requests in the trailing

    `window_seconds` for this bucket+ip and should be rejected."""

    now = time.time()

    key = (bucket, ip)

    with _rate_limit_lock:

        dq = _rate_limit_buckets[key]

        while dq and now - dq[0] > window_seconds:

            dq.popleft()

        if len(dq) >= limit:

            return False

        dq.append(now)

        return True

 

# Every user must be logged in to see anything except these — the main

# dashboard page and every /api/* route are members-only, per the "each

# connected user gets their own login" requirement.

PUBLIC_PATHS = {'/health', '/login', '/register', '/auth0/login', '/callback', '/forgot-password', '/reset-password'}

 

# Routes a logged-in user can always reach even once their trial/subscription

# has expired — they still need to see their billing status, log out, or

# (for an admin) manage other users' subscriptions.

ACCOUNT_ALWAYS_ALLOWED = {

    '/api/account', '/api/account/connect-binance', '/api/account/disconnect-binance',

    '/api/account/risk-ack', '/logout', '/admin', '/api/admin/users', '/api/admin/set-status',

    '/api/account/live-settings', '/api/account/live-toggle',

    '/api/admin/kill-switch', '/api/admin/kill-switch/toggle',

    '/api/account/telegram/link-code', '/api/account/telegram/unlink',

    '/api/live/my-positions',

}

 

LANDING_HTML = r'''<!doctype html>

<html lang="tr">

<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Herobot-ai — Kripto, BIST ve ABD Hisseleri için Otomatik Strateji Motoru</title>

<meta name="description" content="Herobot-ai; kripto vadeli işlemler, BIST ve ABD hisselerinde 4 saatlik teknik sinyalleri tarayan, ATR bazlı risk yönetimiyle çalışan bir strateji motorudur.">

<link rel="preconnect" href="https://fonts.googleapis.com">

<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">

<style>

  *{box-sizing:border-box}

  body{margin:0;background:#0A0D10;font-family:'Manrope',system-ui,sans-serif;color:#E9EDF0;}

  a{color:#3DD9A8;text-decoration:none}

  a:hover{color:#6CE6C0}

  .disp{font-family:'Space Grotesk',system-ui,sans-serif}

  ::selection{background:#3DD9A8;color:#08110D}

  .wrap{max-width:1320px;margin:0 auto;padding-left:40px;padding-right:40px}

  .btn{display:inline-block;padding:14px 28px;border-radius:9px;font-size:15px;font-weight:700;cursor:pointer;border:1px solid transparent}

  .btn-primary{background:#3DD9A8;color:#08110D}

  .btn-primary:hover{background:#6CE6C0;color:#08110D}

  .btn-ghost{border-color:#2A3238;color:#E9EDF0}

  .btn-ghost:hover{border-color:#3DD9A8;color:#3DD9A8}

  .card{background:#11161A;border:1px solid #1E252B;border-radius:14px}

  .grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}

  .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:28px}

  .grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px}

  .grid6{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:16px}

  .eyebrow{font-size:13px;font-weight:700;color:#3DD9A8;letter-spacing:1px;text-transform:uppercase}

  .stats{display:flex;gap:64px;justify-content:center;flex-wrap:wrap}

  nav .nav-links{display:flex;align-items:center;gap:40px}

  nav .nav-links a{font-size:14px;font-weight:600;color:#B7C0C6}

  @media (max-width:900px){

    .grid3,.grid2,.grid4,.grid6{grid-template-columns:1fr}

    .wrap{padding-left:22px;padding-right:22px}

    nav .nav-links{display:none}

    .hero-h1{font-size:38px!important}

    .split{flex-direction:column}

    .stats{gap:32px}

  }

</style>

</head>

<body>

 

<!-- ===== NAV ===== -->

<nav style="position:sticky;top:0;z-index:20;background:rgba(10,13,16,0.9);backdrop-filter:blur(10px);border-bottom:1px solid #1B2126">

  <div class="wrap" style="display:flex;align-items:center;justify-content:space-between;padding:20px 0">

    <div style="display:flex;align-items:center;gap:10px">

      <div style="width:34px;height:34px;border-radius:9px;background:linear-gradient(160deg,#3DD9A8,#1D8F6E);display:flex;align-items:center;justify-content:center">

        <span class="disp" style="font-size:16px;font-weight:700;color:#08110D">H</span>

      </div>

      <span class="disp" style="font-size:19px;font-weight:700">Herobot-ai</span>

    </div>

    <div class="nav-links">

      <a href="#strateji">Strateji</a>

      <a href="#piyasalar">Piyasalar</a>

      <a href="#guvenlik">Güvenlik</a>

      <a href="/login">Panel</a>

    </div>

    <div style="display:flex;align-items:center;gap:14px">

      <span style="font-size:13px;font-weight:600;color:#7A8590" class="hide-sm">Paper mod · risksiz</span>

      <a href="/register" class="btn btn-primary" style="padding:11px 22px;font-size:14px">Ücretsiz Dene</a>

    </div>

  </div>

</nav>

 

<!-- ===== HERO ===== -->

<div style="position:relative;overflow:hidden">

  <div style="position:absolute;top:-220px;left:50%;transform:translateX(-50%);width:900px;height:500px;background:radial-gradient(ellipse at center, rgba(61,217,168,0.16), transparent 70%);pointer-events:none"></div>

  <div class="wrap" style="position:relative;padding:96px 0 88px 0;display:flex;flex-direction:column;align-items:center;text-align:center">

    <div style="display:flex;align-items:center;gap:8px;padding:8px 16px;border:1px solid #23A57B;border-radius:999px;background:rgba(61,217,168,0.08);margin-bottom:28px">

      <div style="width:7px;height:7px;border-radius:50%;background:#3DD9A8"></div>

      <span style="font-size:13px;font-weight:700;color:#3DD9A8">Kripto · BIST · ABD hisseleri — tek motor</span>

    </div>

    <h1 class="disp hero-h1" style="margin:0;font-size:60px;line-height:1.08;font-weight:700;max-width:920px;letter-spacing:-0.5px">

      Duygu yok, panik yok.<br>Sadece <span style="color:#3DD9A8">disiplinli sinyal.</span>

    </h1>

    <p style="margin:26px 0 0 0;font-size:19px;line-height:1.6;color:#B7C0C6;max-width:640px;font-weight:500">

      Herobot-ai, kripto vadeli işlemler, BIST ve ABD hisselerinde 4 saatlik teknik sinyalleri

      7/24 tarayan, ATR bazlı risk yönetimiyle çalışan bir strateji motorudur.

    </p>

    <div style="display:flex;gap:14px;margin-top:40px;flex-wrap:wrap;justify-content:center">

      <a href="/register" class="btn btn-primary">Ücretsiz Dene</a>

      <a href="#strateji" class="btn btn-ghost">Stratejiyi İncele</a>

    </div>

    <div class="stats" style="margin-top:72px;padding-top:36px;border-top:1px solid #1B2126;width:100%;max-width:760px">

      <div style="display:flex;flex-direction:column;align-items:center;gap:4px">

        <span class="disp" style="font-size:28px;font-weight:700;color:#3DD9A8">3</span>

        <span style="font-size:13px;color:#7A8590;font-weight:600">piyasa, tek panel</span>

      </div>

      <div style="display:flex;flex-direction:column;align-items:center;gap:4px">

        <span class="disp" style="font-size:28px;font-weight:700;color:#3DD9A8">~15dk</span>

        <span style="font-size:13px;color:#7A8590;font-weight:600">otomatik tarama sıklığı</span>

      </div>

      <div style="display:flex;flex-direction:column;align-items:center;gap:4px">

        <span class="disp" style="font-size:28px;font-weight:700;color:#3DD9A8">8+</span>

        <span style="font-size:13px;color:#7A8590;font-weight:600">teknik onay katmanı</span>

      </div>

      <div style="display:flex;flex-direction:column;align-items:center;gap:4px">

        <span class="disp" style="font-size:28px;font-weight:700;color:#3DD9A8">7/24</span>

        <span style="font-size:13px;color:#7A8590;font-weight:600">kesintisiz izleme</span>

      </div>

    </div>

  </div>

</div>

 

<!-- ===== NASIL ÇALIŞIR ===== -->

<div style="background:#0D1114;border-top:1px solid #1B2126;border-bottom:1px solid #1B2126">

  <div class="wrap" style="padding:80px 0">

    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:48px">

      <span class="eyebrow">Nasıl çalışır</span>

      <h2 class="disp" style="margin:12px 0 0 0;font-size:32px;font-weight:700">Üç adımda, baştan sona otomatik</h2>

    </div>

    <div class="grid3">

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:14px">

        <span class="disp" style="font-size:15px;font-weight:700;color:#3DD9A8">01</span>

        <h3 style="margin:0;font-size:18px;font-weight:700">Kapanmış mumu okur</h3>

        <p style="margin:0;font-size:14.5px;line-height:1.65;color:#97A0A6">Sinyal yalnızca kapanmış 4 saatlik mumdan üretilir — anlık fiyat gürültüsüne asla güvenilmez, yarım kalan mum işleme girmez.</p>

      </div>

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:14px">

        <span class="disp" style="font-size:15px;font-weight:700;color:#3DD9A8">02</span>

        <h3 style="margin:0;font-size:18px;font-weight:700">Çoklu katman onaylar</h3>

        <p style="margin:0;font-size:14.5px;line-height:1.65;color:#97A0A6">Trend, Momentum, Hacim, Volatilite göstergeleri aynı anda hizalanmadan pozisyon açılmaz. Tek indikatöre güvenilmez.</p>

      </div>

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:14px">

        <span class="disp" style="font-size:15px;font-weight:700;color:#3DD9A8">03</span>

        <h3 style="margin:0;font-size:18px;font-weight:700">ATR ile riski çizer</h3>

        <p style="margin:0;font-size:14.5px;line-height:1.65;color:#97A0A6">Stop-loss, take-profit ve trailing stop sabit yüzde değil, o anki volatiliteye göre otomatik hesaplanır.</p>

      </div>

    </div>

  </div>

</div>

 

<!-- ===== STRATEJİ MOTORU ===== -->

<div class="wrap split" style="padding:88px 0;display:flex;gap:64px;align-items:flex-start" id="strateji">

  <div style="flex:0 0 340px;display:flex;flex-direction:column;gap:18px">

    <span class="eyebrow">Strateji motoru</span>

    <h2 class="disp" style="margin:0;font-size:30px;font-weight:700;line-height:1.2">Sekiz göstergenin hizalanmasını bekleyen bir sistem</h2>

    <p style="margin:0;font-size:15px;line-height:1.7;color:#97A0A6">Trend, momentum ve volatilite filtreleri birlikte çalışır. Piyasa yatay ya da aşırı uçtaysa sistem susmayı tercih eder — her mumda işlem açmaz.</p>

    <div style="display:flex;align-items:center;gap:10px;margin-top:8px;padding:14px 16px;background:#11161A;border:1px solid #1E252B;border-radius:10px">

      <span style="font-size:13px;color:#7A8590;font-weight:600">Volatilite vetosu: piyasa aşırı sakin ya da aşırı oynak bir uçtaysa sistem yeni pozisyon açmaz.</span>

    </div>

  </div>

  <div class="grid2" style="flex:1">

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">TREND</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">Üstel hareketli ortalama değerleri</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">Uzun vadeli yön filtresi</div>

    </div>

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">YÖN</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">Supertrend</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">Trend teyidi</div>

    </div>

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">GÜÇ</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">Ortalama Yön Endeksi</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">Fiyat trendinin gücünün ölçümü — Gerçek trend / yatay piyasa ayrımı</div>

    </div>

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">MOMENTUM</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">RSI hassas ayarları</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">LONG ve SHORT bantları ayrı</div>

    </div>

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">ONAY</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">Momentum, Kanal hareketleri, Dip tepe tespitleri, Hareketli ortalamaların uzaklaşma ve yakınlaşmaları</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">Çoklu filtre yanlış sinyali eler</div>

    </div>

    <div class="card" style="padding:20px 22px">

      <div style="font-size:13px;font-weight:700;color:#3DD9A8;margin-bottom:6px">RİSK</div>

      <div style="font-size:15px;font-weight:700;margin-bottom:4px">Volatilite Bazlı SL / TP / Trailing Stop</div>

      <div style="font-size:13.5px;color:#7A8590;line-height:1.55">Long · Short pozisyon çıkışları ve kar alım ve zarar kes seviyeleri</div>

    </div>

  </div>

</div>

 

<!-- ===== PİYASALAR ===== -->

<div style="background:#0D1114;border-top:1px solid #1B2126;border-bottom:1px solid #1B2126" id="piyasalar">

  <div class="wrap" style="padding:84px 0">

    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:48px">

      <span class="eyebrow">Kapsam</span>

      <h2 class="disp" style="margin:12px 0 0 0;font-size:32px;font-weight:700">Tek panel, üç piyasa</h2>

      <p style="margin:14px 0 0 0;font-size:16px;color:#97A0A6;max-width:560px">Aynı strateji mantığı, üç farklı piyasada aynı disiplinle çalışır.</p>

    </div>

    <div class="grid3">

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:12px">

        <div style="width:44px;height:44px;border-radius:11px;background:rgba(61,217,168,0.12);display:flex;align-items:center;justify-content:center">

          <div style="width:18px;height:18px;border-radius:50%;border:3px solid #3DD9A8"></div>

        </div>

        <h3 style="margin:6px 0 0 0;font-size:17px;font-weight:700">Kripto Vadeli İşlem</h3>

        <p style="margin:0;font-size:14px;line-height:1.6;color:#97A0A6">Binance Futures USDT-M perpetual sözleşmelerinin tamamı otomatik keşfedilir ve taranır.</p>

      </div>

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:12px">

        <div style="width:44px;height:44px;border-radius:11px;background:rgba(61,217,168,0.12);display:flex;align-items:center;justify-content:center">

          <div style="width:18px;height:12px;border-bottom:3px solid #3DD9A8;border-left:3px solid #3DD9A8;border-right:3px solid #3DD9A8"></div>

        </div>

        <h3 style="margin:6px 0 0 0;font-size:17px;font-weight:700">BIST Tüm</h3>

        <p style="margin:0;font-size:14px;line-height:1.6;color:#97A0A6">BIST 100 + Tüm-100 evreninde, TradingView'in native 4 saatlik verisiyle sinyal taraması.</p>

      </div>

      <div class="card" style="padding:30px;display:flex;flex-direction:column;gap:12px">

        <div style="width:44px;height:44px;border-radius:11px;background:rgba(61,217,168,0.12);display:flex;align-items:center;justify-content:center">

          <div style="width:18px;height:18px;border-radius:4px;border:3px solid #3DD9A8"></div>

        </div>

        <h3 style="margin:6px 0 0 0;font-size:17px;font-weight:700">ABD Hisseleri</h3>

        <p style="margin:0;font-size:14px;line-height:1.6;color:#97A0A6">NASDAQ, NYSE ve AMEX'te otomatik borsa tespiti ile geniş bir hisse evreni taranır.</p>

      </div>

    </div>

  </div>

</div>

 

<!-- ===== AI ANALYST + TELEGRAM ===== -->

<div class="wrap grid2" style="padding:88px 0">

  <div class="card" style="padding:36px;display:flex;flex-direction:column;gap:16px">

    <span class="eyebrow">AI Trade Analyst</span>

    <h3 class="disp" style="margin:0;font-size:22px;font-weight:700">Sistemin kendi geçmişini okuyan bir analist</h3>

    <p style="margin:0;font-size:14.5px;line-height:1.7;color:#97A0A6">Kapanan işlemler periyodik olarak yapay zekâ destekli bir analistin masasına düşer: kazanma oranı, profit factor, sebep bazlı kırılım. Salt okunur — stratejiye asla müdahale etmez, sadece aynayı tutar.</p>

    <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap">

      <span style="padding:7px 13px;background:rgba(61,217,168,0.1);border:1px solid #23A57B;border-radius:999px;font-size:12.5px;font-weight:700;color:#3DD9A8">Salt okunur</span>

      <span style="padding:7px 13px;background:rgba(61,217,168,0.1);border:1px solid #23A57B;border-radius:999px;font-size:12.5px;font-weight:700;color:#3DD9A8">Otomatik</span>

    </div>

  </div>

  <div class="card" style="padding:36px;display:flex;flex-direction:column;gap:16px">

    <span class="eyebrow">Telegram Bildirimleri</span>

    <h3 class="disp" style="margin:0;font-size:22px;font-weight:700">Cebine gelen, spam olmayan bildirim</h3>

    <p style="margin:0;font-size:14.5px;line-height:1.7;color:#97A0A6">Yeni pozisyon açıldığında, kapandığında ve her sabah 09:00'da (Europe/Istanbul) günlük özet raporla anında haberdar olursun. Yeniden başlatmalar mesaj göndermez.</p>

    <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap">

      <span style="padding:7px 13px;background:rgba(61,217,168,0.1);border:1px solid #23A57B;border-radius:999px;font-size:12.5px;font-weight:700;color:#3DD9A8">Açılış / kapanış</span>

      <span style="padding:7px 13px;background:rgba(61,217,168,0.1);border:1px solid #23A57B;border-radius:999px;font-size:12.5px;font-weight:700;color:#3DD9A8">Günlük rapor</span>

    </div>

  </div>

</div>

 

<!-- ===== GÜVENLİK ===== -->

<div style="background:#0D1114;border-top:1px solid #1B2126;border-bottom:1px solid #1B2126" id="guvenlik">

  <div class="wrap" style="padding:84px 0">

    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:44px">

      <span class="eyebrow">Risk kontrolleri</span>

      <h2 class="disp" style="margin:12px 0 0 0;font-size:32px;font-weight:700">Kurumsal seviye güvenlik rayları</h2>

      <p style="margin:14px 0 0 0;font-size:16px;color:#97A0A6;max-width:620px">Paper modda sıfır risk; gerçek işleme geçenler için ise sıkı, kullanıcı bazlı sınırlar.</p>

    </div>

    <div class="grid4">

      <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:10px">

        <span style="font-size:20px">🔒</span>

        <div style="font-size:15px;font-weight:700">Sabit pozisyon büyüklüğü</div>

        <div style="font-size:13px;line-height:1.6;color:#7A8590">Bakiyenin rastgele yüzdesi değil, kullanıcı bazlı sabit USD tutarı</div>

      </div>

      <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:10px">

        <span style="font-size:20px">🛑</span>

        <div style="font-size:15px;font-weight:700">Günlük zarar limiti</div>

        <div style="font-size:13px;line-height:1.6;color:#7A8590">Limit dolunca yeni pozisyon durur, açık olanlar yönetilmeye devam eder</div>

      </div>

      <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:10px">

        <span style="font-size:20px">⚖️</span>

        <div style="font-size:15px;font-weight:700">Kaldıraç tavanı</div>

        <div style="font-size:13px;line-height:1.6;color:#7A8590">Kullanıcı bazlı sınır + sistem genelinde mutlak tavan</div>

      </div>

      <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:10px">

        <span style="font-size:20px">⏻</span>

        <div style="font-size:15px;font-weight:700">Acil durdurma anahtarı</div>

        <div style="font-size:13px;line-height:1.6;color:#7A8590">Admin tek tıkla tüm kullanıcılar için yeni işlemleri durdurur</div>

      </div>

    </div>

  </div>

</div>

 

<!-- ===== PANEL ===== -->

<div class="wrap split" style="padding:88px 0;display:flex;align-items:center;gap:64px">

  <div style="flex:1;display:flex;flex-direction:column;gap:18px">

    <span class="eyebrow">Canlı panel</span>

    <h2 class="disp" style="margin:0;font-size:30px;font-weight:700;line-height:1.2">Tek ekranda pozisyon, sinyal ve tarayıcı</h2>

    <p style="margin:0;font-size:15px;line-height:1.7;color:#97A0A6">Web tabanlı panelden anlık pozisyon durumu, güncel sinyal ve tarayıcı sonuçları tek bakışta görülür. Deneme süresi, abonelik ve admin paneliyle çok kullanıcılı yapıya hazır.</p>

    <div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap">

      <a href="/register" class="btn btn-primary">Ücretsiz Dene</a>

      <a href="/login" class="btn btn-ghost">Panele Giriş Yap</a>

    </div>

  </div>

  <div style="flex:1;width:100%">

    <div class="card" style="padding:24px;display:flex;flex-direction:column;gap:14px">

      <div style="display:flex;align-items:center;justify-content:space-between">

        <div style="display:flex;align-items:center;gap:8px">

          <div style="width:8px;height:8px;border-radius:50%;background:#3DD9A8"></div>

          <span style="font-size:13px;font-weight:700;color:#3DD9A8">Bot aktif</span>

        </div>

        <span style="font-size:12px;color:#7A8590;font-weight:600">ETHUSD_PERP</span>

      </div>

      <div style="display:flex;justify-content:space-between;padding:16px;background:#0D1114;border-radius:10px;border:1px solid #1B2126;flex-wrap:wrap;gap:10px">

        <div style="display:flex;flex-direction:column;gap:4px">

          <span style="font-size:12px;color:#7A8590;font-weight:600">Açık Pozisyon</span>

          <span style="font-size:16px;font-weight:700;color:#3DD9A8">LONG · 2664.52</span>

        </div>

        <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end">

          <span style="font-size:12px;color:#7A8590;font-weight:600">Equity</span>

          <span style="font-size:16px;font-weight:700">9,997.93</span>

        </div>

      </div>

      <div style="display:flex;flex-direction:column;gap:8px">

        <div style="display:flex;justify-content:space-between;padding:10px 4px;border-bottom:1px solid #1B2126">

          <span style="font-size:13px;color:#97A0A6">RSI</span><span style="font-size:13px;font-weight:700">58.2</span>

        </div>

        <div style="display:flex;justify-content:space-between;padding:10px 4px;border-bottom:1px solid #1B2126">

          <span style="font-size:13px;color:#97A0A6">ADX</span><span style="font-size:13px;font-weight:700">31.4</span>

        </div>

        <div style="display:flex;justify-content:space-between;padding:10px 4px">

          <span style="font-size:13px;color:#97A0A6">Supertrend</span><span style="font-size:13px;font-weight:700;color:#3DD9A8">Boğa</span>

        </div>

      </div>

    </div>

  </div>

</div>

 

<!-- ===== FOOTER ===== -->

<div style="border-top:1px solid #1B2126">

  <div class="wrap" style="padding:44px 0 52px 0;display:flex;flex-direction:column;gap:22px">

    <div class="card" style="padding:18px 22px">

      <p style="margin:0;font-size:12.5px;line-height:1.7;color:#7A8590">Bu sistem şu anda paper-trading (demo) modda çalışır; gerçek emir göndermez. Herhangi bir algoritmik strateji geçmiş performansa dayanır ve gelecekteki sonuçları garanti etmez — bu bir yatırım tavsiyesi değildir.</p>

    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px">

      <div style="display:flex;align-items:center;gap:10px">

        <div style="width:26px;height:26px;border-radius:7px;background:linear-gradient(160deg,#3DD9A8,#1D8F6E);display:flex;align-items:center;justify-content:center">

          <span class="disp" style="font-size:12px;font-weight:700;color:#08110D">H</span>

        </div>

        <span style="font-size:13px;color:#7A8590;font-weight:600">© Herobot-ai — kripto · BIST · ABD hisseleri strateji motoru</span>

      </div>

      <div style="display:flex;gap:24px">

        <a href="#strateji" style="font-size:13px;color:#7A8590;font-weight:600">Strateji</a>

        <a href="#piyasalar" style="font-size:13px;color:#7A8590;font-weight:600">Piyasalar</a>

        <a href="#guvenlik" style="font-size:13px;color:#7A8590;font-weight:600">Güvenlik</a>

      </div>

    </div>

  </div>

</div>

 

</body>

</html>

'''

 

HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>A&amp;I Trading Terminal</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

<style>

:root{

  --bg:#090c12; --bg-elev:#0d1119; --panel:#111623; --panel-2:#161c2c;

  --border:#212a3d; --border-soft:#1a2233;

  --text:#e7ecf6; --text-dim:#8b93a8; --text-faint:#565f74;

  --accent:#d4a857; --accent-soft:#3a3120;

  --bull:#3ecf8e; --bull-bg:#0f2419; --bull-border:#1e4531;

  --bear:#f1596e; --bear-bg:#2a151b; --bear-border:#4a2530;

  --neu-bg:#1b2233; --neu-border:#2a3348;

  --font-d:'Space Grotesk','IBM Plex Sans',system-ui,-apple-system,sans-serif;

  --font-m:'JetBrains Mono','IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;

}

*{box-sizing:border-box}

body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-d);-webkit-font-smoothing:antialiased}

::selection{background:var(--accent-soft);color:var(--accent)}

:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.app{max-width:1440px;margin:0 auto;padding:18px 22px 40px}

 

/* Top bar */

.topbar{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 18px;background:var(--bg-elev);border:1px solid var(--border);border-radius:12px;margin-bottom:14px}

.brand{display:flex;align-items:center;gap:11px}

.brand-mark{width:9px;height:9px;border-radius:50%;background:var(--accent);flex:none}

.brand-name{font-weight:700;font-size:17px;letter-spacing:.2px}

.brand-sub{color:var(--text-dim);font-size:12.5px;margin-top:2px}

.topbar-right{display:flex;align-items:center;gap:14px}

.clock{font-family:var(--font-m);color:var(--text-dim);font-size:13px;letter-spacing:.5px}

.status-pill{display:flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;font-size:12px;font-weight:600;background:var(--neu-bg);border:1px solid var(--neu-border);color:var(--text-dim)}

.status-pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor}

.status-pill.live{background:var(--bull-bg);border-color:var(--bull-border);color:var(--bull)}

.status-pill.live .dot{animation:pulse 1.8s ease-in-out infinite}

.status-pill.wait{background:var(--accent-soft);border-color:#4a3d22;color:var(--accent)}

.status-pill.err{background:var(--bear-bg);border-color:var(--bear-border);color:var(--bear)}

@media (prefers-reduced-motion:no-preference){@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}}

 

/* KPI strip */

.kpistrip{display:flex;align-items:stretch;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin-bottom:14px;gap:22px;overflow:auto}

.kpi{display:flex;flex-direction:column;justify-content:center;min-width:118px;flex:1}

.kpi-equity{min-width:210px;flex:1.6;position:relative}

.kpi-label{font-size:11.5px;color:var(--text-faint);margin-bottom:6px}

.kpi-value{font-family:var(--font-m);font-size:22px;font-weight:700;letter-spacing:-.2px}

.kpi-sub{font-size:12px;color:var(--text-dim);margin-top:4px;font-family:var(--font-m)}

.kpi-divider{width:1px;background:var(--border-soft);flex:none}

.sparkline{width:100%;height:30px;margin-top:8px;display:block}

.pos{color:var(--bull)}.neg{color:var(--bear)}

 

/* Panels */

.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin-bottom:14px;overflow:hidden}

.panel-head{padding:14px 18px;border-bottom:1px solid var(--border-soft);display:flex;align-items:center;justify-content:space-between;gap:10px}

.panel-head h2{font-size:14.5px;margin:0;font-weight:600}

.cols{display:grid;grid-template-columns:1.3fr 1fr;gap:14px;margin-bottom:14px}

.cols .panel{margin-bottom:0}

 

/* Position card */

.position-body{padding:18px}

.pos-empty{color:var(--text-dim);font-size:13px;padding:6px 0 2px}

.pos-top{display:flex;align-items:center;gap:10px;margin-bottom:16px}

.side-tag{font-family:var(--font-m);font-weight:700;font-size:13px;padding:5px 10px;border-radius:6px;letter-spacing:.4px}

.side-tag.long{background:var(--bull-bg);color:var(--bull);border:1px solid var(--bull-border)}

.side-tag.short{background:var(--bear-bg);color:var(--bear);border:1px solid var(--bear-border)}

.pos-symbol{font-family:var(--font-m);font-size:16px;font-weight:600}

.pos-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}

.pos-grid .kpi-label{margin-bottom:5px}

.pos-grid .val{font-family:var(--font-m);font-size:15.5px;font-weight:600}

.bar-wrap{margin-top:6px}

.bar-labels{display:flex;justify-content:space-between;font-family:var(--font-m);font-size:11px;color:var(--text-faint);margin-bottom:6px}

.bar-track{position:relative;height:8px;border-radius:5px;background:var(--neu-bg);border:1px solid var(--border-soft)}

.bar-fill{position:absolute;top:0;bottom:0;border-radius:5px;background:linear-gradient(90deg,var(--bear),var(--accent),var(--bull));opacity:.28}

.bar-dot{position:absolute;top:50%;width:11px;height:11px;border-radius:50%;transform:translate(-50%,-50%);border:2px solid var(--bg)}

.bar-dot.cur{background:var(--accent);width:13px;height:13px;box-shadow:0 0 0 3px rgba(212,168,87,.18)}

.bar-dot.sl{background:var(--bear)}

.bar-dot.tp{background:var(--bull)}

.pos-foot{margin-top:10px;color:var(--text-dim);font-size:12px;font-family:var(--font-m)}

 

/* Signal matrix */

.signal-body{padding:14px 18px}

.chip-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}

.chip{display:flex;justify-content:space-between;align-items:center;padding:9px 11px;border-radius:8px;background:var(--neu-bg);border:1px solid var(--border-soft)}

.chip-label{font-size:12px;color:var(--text-dim)}

.chip-value{font-family:var(--font-m);font-weight:700;font-size:12.5px}

.chip-value.pos{color:var(--bull)}.chip-value.neg{color:var(--bear)}.chip-value.neu{color:var(--text)}

.chip.final{grid-column:1/-1;background:var(--accent-soft);border-color:#4a3d22}

.chip.final .chip-label{color:var(--accent)}

.chip.final .chip-value{font-size:14px}

 

/* Tables */

.table-scroll{overflow:auto}

.table-scroll.tall{max-height:600px}

table.datatable{width:100%;border-collapse:collapse;font-size:12.5px}

table.datatable th{position:sticky;top:0;background:var(--panel);text-align:left;padding:10px 12px;color:var(--text-faint);font-weight:600;font-size:11.5px;border-bottom:1px solid var(--border);white-space:nowrap}

table.datatable td{padding:9px 12px;border-bottom:1px solid var(--border-soft);font-family:var(--font-m);white-space:nowrap}

table.datatable td.wrap-cell{white-space:normal;font-family:var(--font-d);color:var(--text-dim);font-size:12px}

table.datatable tbody tr:hover{background:var(--panel-2)}

table.datatable td.empty{color:var(--text-dim);font-family:var(--font-d);white-space:normal;padding:22px 12px;text-align:center}

.num{text-align:right}

td.num{text-align:right}

th.sortable{cursor:pointer;user-select:none}

th.sortable:hover{color:var(--text)}

th.sort-active{color:var(--accent)}

.pill{display:inline-block;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:700;font-family:var(--font-m)}

.pill.long{background:var(--bull-bg);color:var(--bull);border:1px solid var(--bull-border)}

.pill.short{background:var(--bear-bg);color:var(--bear);border:1px solid var(--bear-border)}

.pill.flat{background:var(--neu-bg);color:var(--text-dim);border:1px solid var(--border-soft)}

 

/* Scanner */

.scanner-tabs{gap:16px;flex-wrap:wrap}

.tab{background:none;border:none;color:var(--text-faint);font-family:var(--font-d);font-size:13.5px;font-weight:600;padding:4px 0;cursor:pointer;border-bottom:2px solid transparent}

.tab.active{color:var(--text);border-color:var(--accent)}

.scanner-note{color:var(--text-faint);font-size:11.5px;margin-left:auto}

.tabpane{display:none;padding:14px 18px 18px}

.tabpane.active{display:block}

.scanner-controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}

.scanner-controls input,.scanner-controls select{background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 11px;font-size:12.5px;font-family:var(--font-d)}

.btn{background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 13px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:var(--font-d)}

.btn:hover{border-color:var(--accent);color:var(--accent)}

.btn-danger{border-color:#7a2a2a;color:#ff8080}

.btn-danger:hover{border-color:#ff5c5c;color:#ff5c5c;background:rgba(255,92,92,0.08)}

.btn:disabled{opacity:0.55;cursor:default}

.scanner-status{color:var(--text-dim);font-size:12px;align-self:center;margin-left:2px}

.scanner-summary{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap;font-size:11.5px}

.scanner-summary>span:first-child{color:var(--text-dim);align-self:center;margin-right:4px}

.tag{padding:3px 9px;border-radius:999px;font-family:var(--font-m);font-weight:700}

.tag-long{background:var(--bull-bg);color:var(--bull)}

.tag-short{background:var(--bear-bg);color:var(--bear)}

.tag-flat{background:var(--neu-bg);color:var(--text-dim)}

.footnote{margin-top:12px;color:var(--text-faint);font-size:11.5px}

.text-faint{color:var(--text-faint)}

.add-btn{background:var(--accent-soft);color:var(--accent);border:1px solid #4a3d22;border-radius:6px;padding:4px 9px;font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font-d);white-space:nowrap}

.add-btn:hover{background:var(--accent);color:#1a1406}

.add-btn:disabled{opacity:.55;cursor:default}

.ai-body{padding:16px 18px;font-size:13px;line-height:1.65;color:var(--text);white-space:pre-wrap}

.ai-meta{color:var(--text-faint);font-size:11.5px;margin-bottom:10px}

.ai-disabled{color:var(--text-dim);font-size:13px}

.added-tag{color:var(--bull);font-size:11px;font-weight:700;font-family:var(--font-m);white-space:nowrap}

.watchlist-empty{color:var(--text-dim);font-size:13px;padding:4px 0}

.row-clickable{cursor:pointer}

.row-clickable:hover{background:var(--panel-2)}

.row-selected{background:var(--panel-2)!important;box-shadow:inset 3px 0 0 var(--accent)}

.account-form{display:flex;flex-direction:column;gap:10px;max-width:440px}

.account-form label{font-size:11.5px;color:var(--text-faint)}

.account-form input{background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-size:13px;font-family:var(--font-m);width:100%}

.account-notice{background:var(--accent-soft);border:1px solid #4a3d22;color:var(--accent);border-radius:8px;padding:10px 12px;font-size:12px;margin-top:10px;line-height:1.55}

.account-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px;align-items:center}

.badge-verified{color:var(--bull);font-weight:700}

.badge-unverified{color:var(--text-dim)}

.badge-error{color:var(--bear);font-weight:600}

.badge-trial{color:var(--accent);font-weight:600}

.badge-admin{color:var(--bull);font-weight:600}

.risk-ack{display:flex;gap:8px;align-items:flex-start;font-size:12px;color:var(--text-dim);line-height:1.45;margin-top:2px}

.live-panel{border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-top:16px}

.live-panel h4{margin:0 0 10px;font-size:13px}

.live-danger{background:var(--bear-bg);border:1px solid var(--bear-border);color:var(--bear);border-radius:8px;padding:10px 12px;font-size:12px;margin-top:10px;line-height:1.55}

.badge-live-on{color:var(--bull);font-weight:700}

.badge-live-off{color:var(--text-dim)}

.badge-live-paused{color:var(--accent);font-weight:700}

.tg-code{font-family:var(--font-m);font-size:20px;font-weight:700;letter-spacing:3px;background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;padding:8px 14px;display:inline-block;margin:6px 0}

.risk-ack input{width:auto!important;margin-top:2px}

.auth-page{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}

.auth-card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:32px;width:100%;max-width:380px}

.auth-card h1{font-size:19px;margin:0 0 4px}

.auth-card p.sub{color:var(--text-dim);font-size:12.5px;margin:0 0 22px}

.auth-card label{font-size:11.5px;color:var(--text-faint);display:block;margin-bottom:5px}

.auth-card input{width:100%;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:13.5px;font-family:var(--font-d);margin-bottom:14px}

.auth-card button.primary{width:100%;background:var(--accent);color:#1a1406;border:none;border-radius:8px;padding:11px;font-size:13.5px;font-weight:700;cursor:pointer;font-family:var(--font-d)}

.auth-card button.primary:disabled{opacity:.6;cursor:default}

.auth-error{background:var(--bear-bg);border:1px solid var(--bear-border);color:var(--bear);border-radius:8px;padding:9px 11px;font-size:12.5px;margin-bottom:14px;display:none}

.auth-switch{text-align:center;margin-top:16px;font-size:12.5px;color:var(--text-dim)}

.auth-switch a{color:var(--accent);text-decoration:none}

.page-footer{text-align:center;color:var(--text-faint);font-size:11.5px;margin-top:6px}

 

@media(max-width:900px){.cols{grid-template-columns:1fr}.pos-grid{grid-template-columns:1fr 1fr}.chip-grid{grid-template-columns:1fr}}

@media(max-width:640px){.app{padding:12px}.kpistrip{flex-wrap:wrap}.kpi-divider{display:none}.kpi{min-width:45%}.topbar{flex-wrap:wrap}}

</style></head>

<body><div class="app">

 

<header class="topbar">

  <div class="brand">

    <span class="brand-mark"></span>

    <div>

      <div class="brand-name">A&amp;I Trading Terminal</div>

      <div class="brand-sub">Welcome to AI Trading Platform</div>

    </div>

  </div>

  <div class="topbar-right">

    <span class="clock" id="clock">—:—:—</span>

    <span class="status-pill wait" id="status"><span class="dot"></span>Bağlanıyor</span>

    <span class="text-faint" id="whoami">__USERNAME__</span>

    <a class="btn" id="adminLink" href="/admin" style="display:none;text-decoration:none">Yönetim</a>

    <button class="btn" onclick="logout()">Çıkış</button>

  </div>

</header>

 

<section class="kpistrip">

  <div class="kpi kpi-equity">

    <div class="kpi-label">Sanal bakiye</div>

    <div class="kpi-value" id="equity">—</div>

    <div class="kpi-sub" id="pnl">—</div>

    <svg class="sparkline" id="sparkline" viewBox="0 0 200 30" preserveAspectRatio="none"></svg>

  </div>

  <div class="kpi-divider"></div>

  <div class="kpi"><div class="kpi-label">Açık pozisyonlar P&amp;L</div><div class="kpi-value" id="openPnl">—</div><div class="kpi-sub" id="openPnlSub">—</div></div>

  <div class="kpi-divider"></div>

  <div class="kpi"><div class="kpi-label">İşlem &middot; kazanma oranı</div><div class="kpi-value" id="trades">—</div><div class="kpi-sub" id="winrate">—</div></div>

  <div class="kpi-divider"></div>

  <div class="kpi"><div class="kpi-label">Profit factor</div><div class="kpi-value" id="pf">—</div><div class="kpi-sub" id="avg">—</div></div>

  <div class="kpi-divider"></div>

  <div class="kpi"><div class="kpi-label">Maks. drawdown</div><div class="kpi-value" id="dd">—</div><div class="kpi-sub" id="candle">—</div></div>

</section>

 

<section class="panel">

  <div class="panel-head"><h2>Hesabım — Binance Bağlantım</h2><span class="text-faint" id="binanceStatusPill">—</span></div>

  <div class="position-body" id="accountBody">

    <div class="pos-empty">Yükleniyor…</div>

  </div>

  <div class="live-panel">

    <h4>Canlı İşlem (Gerçek Para)</h4>

    <div id="livePanelBody"><div class="pos-empty">Yükleniyor…</div></div>

  </div>

  <div class="live-panel">

    <h4>Telegram Bildirimleri</h4>

    <div id="telegramPanelBody"><div class="pos-empty">Yükleniyor…</div></div>

  </div>

</section>

 

<section class="panel">

  <div class="panel-head"><h2>Canlı İşlemlerim</h2><span class="text-faint" id="liveMineCount">—</span></div>

  <div class="table-scroll">

    <table class="datatable">

      <thead><tr><th>Sembol</th><th>Yön</th><th class="num">Miktar</th><th class="num">Giriş</th><th class="num">Güncel</th><th class="num">Unrealized P&amp;L</th><th>Kaldıraç</th><th>Açılış</th><th></th></tr></thead>

      <tbody id="liveMineOpenRows"><tr><td colspan="9" class="empty">Yükleniyor…</td></tr></tbody>

    </table>

  </div>

  <div class="table-scroll" style="margin-top:14px">

    <table class="datatable">

      <thead><tr><th>Tarih</th><th>Yön</th><th>Sembol</th><th class="num">Giriş</th><th class="num">Çıkış</th><th class="num">P&amp;L</th><th>Neden</th></tr></thead>

      <tbody id="liveMineClosedRows"><tr><td colspan="7" class="empty">Yükleniyor…</td></tr></tbody>

    </table>

  </div>

  <div class="footnote">Bu panel yalnızca <b>sizin</b> Binance hesabınızda gerçekleşen canlı işlemleri gösterir — yukarıdaki paper/demo panel ile veya başka kullanıcılarla karışmaz; sizden başka hiç kimse burayı göremez.</div>

</section>

 

<section class="panel">

  <div class="panel-head"><h2>Deneme İşlemleri Listesi</h2><span class="text-faint" id="watchlistCount">0 / 10</span></div>

  <div class="table-scroll">

    <table class="datatable">

      <thead><tr><th>Sembol</th><th>Piyasa</th><th>Yön / Sinyal</th><th class="num">Fiyat</th><th class="num">Unrealized P&amp;L</th><th>Eklenme</th><th></th></tr></thead>

      <tbody id="watchlistRows"><tr><td colspan="7" class="empty">Yükleniyor…</td></tr></tbody>

    </table>

  </div>

  <div class="footnote">Bir satıra tıklayarak o sembolün pozisyon ve sinyal detayını aşağıda görüntüleyebilirsiniz. Kripto sembolleri aynı strateji ile bağımsız bir paper pozisyon açar (boyut: $<span id="wlUsd">—</span> nominal). XUTUM sembolleri yalnızca sinyal takibidir; gerçek/paper emir açılmaz.</div>

</section>

 

<section class="cols">

  <div class="panel">

    <div class="panel-head"><h2>Açık pozisyon <span class="text-faint" id="detailSymbol">— ETHUSDT</span></h2></div>

    <div class="position-body" id="position">Yükleniyor…</div>

  </div>

  <div class="panel">

    <div class="panel-head"><h2>Sinyal matrisi</h2></div>

    <div class="signal-body" id="signals">—</div>

  </div>

</section>

 

<section class="panel">

  <div class="panel-head"><h2>Son işlemler</h2></div>

  <div class="table-scroll">

    <table class="datatable">

      <thead><tr><th>Tarih</th><th>Yön</th><th>Sembol</th><th class="num">Giriş</th><th class="num">Çıkış</th><th class="num">P&amp;L</th><th>Neden</th></tr></thead>

      <tbody id="history"><tr><td colspan="7" class="empty">Yükleniyor…</td></tr></tbody>

    </table>

  </div>

</section>

 

<section class="panel">

  <div class="panel-head"><h2>AI Trade Analisti</h2><button class="btn" id="aiRunBtn" onclick="runAiAnalysis()">Şimdi Analiz Et</button></div>

  <div class="ai-body" id="aiAnalysisBody">Yükleniyor…</div>

</section>

 

<section class="panel scanner-panel">

  <div class="panel-head scanner-tabs">

    <button class="tab active" data-tab="crypto" onclick="switchTab('crypto')">Binance Futures</button>

    <button class="tab" data-tab="bist" onclick="switchTab('bist')">XUTUM</button>

    <button class="tab" data-tab="us" onclick="switchTab('us')">S&amp;P 500 / Nasdaq-100</button>

    <div class="scanner-note" id="scannerNote">USDT-M perpetual &middot; 4H kapalı mum &middot; sinyal amaçlı, gerçek emir yok</div>

  </div>

 

  <div class="tabpane active" id="tab-crypto">

    <div class="scanner-controls">

      <input id="coinSearch" placeholder="Coin ara (örn. BTC)" oninput="renderScanner()">

      <select id="signalFilter" onchange="renderScanner()">

        <option value="ALL">Tüm sinyaller</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>

      </select>

      <button class="btn" onclick="startScanner(true)">Tümünü tara</button>

      <span class="scanner-status" id="scannerStatus">Hazırlanıyor…</span>

    </div>

    <div class="scanner-summary"><span id="coinCount">0 coin</span><span class="tag tag-long" id="longCount">LONG 0</span><span class="tag tag-short" id="shortCount">SHORT 0</span><span class="tag tag-flat" id="noCount">NO SIGNAL 0</span></div>

    <div class="table-scroll tall">

      <table class="datatable" id="scannerTable">

        <thead><tr>

          <th class="sortable" data-key="symbol" data-tbl="scanner">Coin</th>

          <th class="sortable num" data-key="price" data-tbl="scanner">Fiyat</th>

          <th class="sortable num" data-key="change_pct" data-tbl="scanner">24s %</th>

          <th class="sortable num" data-key="volume" data-tbl="scanner">Hacim</th>

          <th data-key="st">ST</th>

          <th class="sortable num" data-key="adx" data-tbl="scanner">ADX</th>

          <th class="sortable num" data-key="rsi" data-tbl="scanner">RSI</th>

          <th class="sortable num" data-key="cci" data-tbl="scanner">CCI</th>

          <th>MACD</th>

          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="scanner">ATRP %ile</th>

          <th class="sortable" data-key="signal" data-tbl="scanner">Sinyal</th>

          <th>Açıklama</th>

          <th>Ekle</th>

        </tr></thead>

        <tbody id="scannerRows"><tr><td colspan="13" class="empty">Tarama bekleniyor…</td></tr></tbody>

      </table>

    </div>

  </div>

 

  <div class="tabpane" id="tab-bist">

    <div class="scanner-controls">

      <input id="bistSearch" placeholder="Hisse ara (örn. THYAO)" oninput="renderBistScanner()">

      <select id="bistSignalFilter" onchange="renderBistScanner()">

        <option value="ALL">Tüm sinyaller</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>

      </select>

      <button class="btn" onclick="startBistScanner(true)">XUTUM tara</button>

      <span class="scanner-status" id="bistScannerStatus">Hazırlanıyor…</span>

    </div>

    <div class="scanner-summary"><span id="bistCount">0 hisse</span><span class="tag tag-long" id="bistLongCount">LONG 0</span><span class="tag tag-short" id="bistShortCount">SHORT 0</span><span class="tag tag-flat" id="bistNoCount">NO SIGNAL 0</span></div>

    <div class="table-scroll tall">

      <table class="datatable" id="bistScannerTable">

        <thead><tr>

          <th class="sortable" data-key="symbol" data-tbl="bist">Hisse</th>

          <th class="sortable num" data-key="price" data-tbl="bist">Fiyat</th>

          <th class="sortable num" data-key="change_pct" data-tbl="bist">Günlük %</th>

          <th data-key="st">ST</th>

          <th class="sortable num" data-key="adx" data-tbl="bist">ADX</th>

          <th class="sortable num" data-key="rsi" data-tbl="bist">RSI</th>

          <th class="sortable num" data-key="cci" data-tbl="bist">CCI</th>

          <th>MACD</th>

          <th class="num">Stoch K/D</th>

          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="bist">ATRP %ile</th>

          <th class="sortable" data-key="signal" data-tbl="bist">Sinyal</th>

          <th>Açıklama</th>

          <th>Ekle</th>

        </tr></thead>

        <tbody id="bistScannerRows"><tr><td colspan="13" class="empty">Tarama bekleniyor…</td></tr></tbody>

      </table>

    </div>

    <div class="footnote">SHORT burada yalnızca stratejinin teknik sinyalidir; BIST spot piyasasında doğrudan açığa satış emri anlamına gelmez.</div>

  </div>

 

  <div class="tabpane" id="tab-us">

    <div class="scanner-controls">

      <input id="usSearch" placeholder="Hisse ara (örn. AAPL)" oninput="renderUsScanner()">

      <select id="usSignalFilter" onchange="renderUsScanner()">

        <option value="ALL">Tüm sinyaller</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>

      </select>

      <button class="btn" onclick="startUsScanner(true)">S&amp;P500/Nasdaq-100 tara</button>

      <span class="scanner-status" id="usScannerStatus">Hazırlanıyor…</span>

    </div>

    <div class="scanner-summary"><span id="usCount">0 hisse</span><span class="tag tag-long" id="usLongCount">LONG 0</span><span class="tag tag-short" id="usShortCount">SHORT 0</span><span class="tag tag-flat" id="usNoCount">NO SIGNAL 0</span></div>

    <div class="table-scroll tall">

      <table class="datatable" id="usScannerTable">

        <thead><tr>

          <th class="sortable" data-key="symbol" data-tbl="us">Hisse</th>

          <th class="sortable num" data-key="price" data-tbl="us">Fiyat</th>

          <th class="sortable num" data-key="change_pct" data-tbl="us">Günlük %</th>

          <th data-key="st">ST</th>

          <th class="sortable num" data-key="adx" data-tbl="us">ADX</th>

          <th class="sortable num" data-key="rsi" data-tbl="us">RSI</th>

          <th class="sortable num" data-key="cci" data-tbl="us">CCI</th>

          <th>MACD</th>

          <th class="num">Stoch K/D</th>

          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="us">ATRP %ile</th>

          <th class="sortable" data-key="signal" data-tbl="us">Sinyal</th>

          <th>Açıklama</th>

          <th>Ekle</th>

        </tr></thead>

        <tbody id="usScannerRows"><tr><td colspan="13" class="empty">Tarama bekleniyor…</td></tr></tbody>

      </table>

    </div>

    <div class="footnote">S&amp;P 500 + Nasdaq-100 evreni (statik liste, periyodik güncellenmeli). Takip listesine eklenen ABD hisseleri, kripto watchlist'i gibi bağımsız bir paper pozisyon açar; SHORT taraf ödünç/marj kısıtlarını modellemeyen saf bir simülasyondur.</div>

  </div>

</section>

 

<div class="page-footer">Otomatik yenileme: pozisyon 5 sn &middot; tarayıcılar 10 sn &middot; paper trading, gerçek emir yok.</div>

</div>

 

<script>

const money=x=>x==null?'—':'$'+Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

const num=x=>x==null||x===''?'—':Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

const cls=x=>Number(x)>=0?'pos':'neg';

 

// live clock

function tickClock(){const d=new Date();document.getElementById('clock').textContent=d.toLocaleTimeString('tr-TR',{hour12:false});}

tickClock();setInterval(tickClock,1000);

 

function chipClass(v){

  if(v==null) return 'neu';

  const s=String(v).toUpperCase();

  if(s.includes('BULL')||s==='LONG'||s==='OK') return 'pos';

  if(s.includes('BEAR')||s==='SHORT'||s==='BLOCKED') return 'neg';

  return 'neu';

}

 

function drawSparkline(values){

  const svg=document.getElementById('sparkline');

  if(!values||values.length<2){svg.innerHTML='';return;}

  const w=200,h=30,pad=2;

  const min=Math.min(...values),max=Math.max(...values);

  const span=(max-min)||1;

  const pts=values.map((v,i)=>{

    const x=pad+(i/(values.length-1))*(w-pad*2);

    const y=h-pad-((v-min)/span)*(h-pad*2);

    return x.toFixed(1)+','+y.toFixed(1);

  }).join(' ');

  const up=values[values.length-1]>=values[0];

  const color=up?'var(--bull)':'var(--bear)';

  svg.innerHTML=`<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;

}

 

function renderPositionCard(posEl,p){

  if(!p){

    posEl.innerHTML='<div class="pos-top"><span class="side-tag" style="background:var(--neu-bg);color:var(--text-dim);border:1px solid var(--border-soft)">FLAT</span></div><div class="pos-empty">Açık paper pozisyon yok. Sinyal oluştuğunda burada görünecek.</div>';

    return;

  }

  const sideCls=p.side==='LONG'?'long':'short';

  const pnl=p.unrealized_pnl;

  const stop=Number(p.active_stop),tp=Number(p.tp),cur=Number(p.current_price),entry=Number(p.entry_price);

  const vals=[stop,tp,cur,entry].filter(v=>!isNaN(v));

  const lo=Math.min(...vals),hi=Math.max(...vals),span=(hi-lo)||1;

  const pct=v=>((v-lo)/span*100).toFixed(1);

  posEl.innerHTML=`

    <div class="pos-top"><span class="side-tag ${sideCls}">${p.side}</span><span class="pos-symbol">${p.symbol}</span></div>

    <div class="pos-grid">

      <div><div class="kpi-label">Giriş</div><div class="val">${num(entry)}</div></div>

      <div><div class="kpi-label">Güncel</div><div class="val">${num(cur)}</div></div>

      <div><div class="kpi-label">Unrealized P&amp;L</div><div class="val ${cls(pnl)}">${money(pnl)}</div></div>

      <div><div class="kpi-label">ATR</div><div class="val">${num(p.atr)}</div></div>

    </div>

    <div class="bar-wrap">

      <div class="bar-labels"><span>SL ${num(stop)}</span><span>TP ${num(tp)}</span></div>

      <div class="bar-track">

        <div class="bar-fill" style="left:0%;right:0%"></div>

        <div class="bar-dot sl" style="left:${pct(stop)}%"></div>

        <div class="bar-dot tp" style="left:${pct(tp)}%"></div>

        <div class="bar-dot cur" style="left:${pct(cur)}%" title="Güncel fiyat"></div>

      </div>

    </div>

    <div class="pos-foot">Trailing: ${p.trail_active?('AKTİF @ '+num(p.trail_stop)):'beklemede'} &middot; Giriş zamanı: ${p.entry_time}</div>`;

}

function renderSignalCard(sigEl,s){

  s=s||{};

  const rows=[['EMA 50 / 100',s.ema],['Supertrend',s.supertrend],['ADX',s.adx],['RSI',s.rsi],['CCI',s.cci],['Stoch RSI',s.stoch],['MACD',s.macd],['1D Volatilite',s.volatility],['1D ATRP %ile',s.atrp_percentile_1d]];

  sigEl.innerHTML=

    '<div class="chip-grid">'+

    rows.map(r=>`<div class="chip"><span class="chip-label">${r[0]}</span><span class="chip-value ${chipClass(r[1])}">${r[1]??'—'}</span></div>`).join('')+

    `<div class="chip final"><span class="chip-label">Son sinyal</span><span class="chip-value ${chipClass(s.final)}">${s.final??'—'}</span></div>`+

    '</div>';

}

 

let statusCache=null;

let watchlistCache={items:[]};

let selectedSymbol='ETHUSDT';

 

function selectSymbol(symbol){

  selectedSymbol=symbol;

  renderDetail();

  renderWatchlistTable();

}

 

function renderDetail(){

  const posEl=document.getElementById('position');

  const sigEl=document.getElementById('signals');

  document.getElementById('detailSymbol').textContent='— '+selectedSymbol;

  if(selectedSymbol==='ETHUSDT'){

    if(!statusCache){posEl.innerHTML='Yükleniyor…';sigEl.innerHTML='—';return;}

    renderPositionCard(posEl,statusCache.position);

    renderSignalCard(sigEl,statusCache.signals);

    return;

  }

  const item=(watchlistCache.items||[]).find(x=>x.symbol===selectedSymbol);

  if(!item){

    posEl.innerHTML='<div class="pos-empty">Bu sembol takip listesinden kaldırılmış olabilir.</div>';

    sigEl.innerHTML='—';

    return;

  }

  renderPositionCard(posEl,item.position);

  renderSignalCard(sigEl,item.indicators);

}

 

function render(d){

  statusCache=d;

  const st=document.getElementById('status');

  if(d.bot_alive){st.className='status-pill live';st.innerHTML='<span class="dot"></span>Bot aktif';}

  else{st.className='status-pill wait';st.innerHTML='<span class="dot"></span>Beklemede';}

 

  document.getElementById('equity').textContent=money(d.equity);

  document.getElementById('pnl').innerHTML=`<span class="${cls(d.net_pnl)}">${money(d.net_pnl)}</span> &middot; ${Number(d.return_pct||0).toFixed(2)}%`;

  document.getElementById('openPnl').innerHTML=`<span class="${cls(d.total_open_pnl)}">${money(d.total_open_pnl)}</span>`;

  document.getElementById('openPnlSub').textContent='Tüm açık pozisyonlar';

  document.getElementById('trades').textContent=d.stats.trades;

  document.getElementById('winrate').textContent='Win rate '+d.stats.win_rate.toFixed(2)+'%';

  document.getElementById('pf').textContent=d.stats.profit_factor.toFixed(3);

  document.getElementById('avg').textContent='Ort. '+money(d.stats.avg_trade);

  document.getElementById('dd').textContent=d.stats.max_drawdown.toFixed(2)+'%';

  document.getElementById('candle').textContent='Son mum: '+(d.last_closed_time||'—');

 

  const eqSeries=(d.history||[]).slice().reverse().map(t=>Number(t.equity_after)).filter(v=>!isNaN(v));

  if(eqSeries.length<2 && d.equity!=null) eqSeries.push(Number(d.equity));

  drawSparkline(eqSeries);

 

  document.getElementById('history').innerHTML=(d.history||[]).map(t=>`<tr><td>${t.exit_time||'—'}</td><td><span class="pill ${String(t.side).toLowerCase()}">${t.side}</span></td><td>${t.symbol}</td><td class="num">${num(t.entry_price)}</td><td class="num">${num(t.exit_price)}</td><td class="num ${cls(t.net_pnl)}"><b>${money(t.net_pnl)}</b></td><td class="wrap-cell">${t.reason||''}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">Henüz kapanmış işlem yok.</td></tr>';

 

  renderWatchlistTable();

  if(selectedSymbol==='ETHUSDT') renderDetail();

}

 

function switchTab(name){

  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));

  document.querySelectorAll('.tabpane').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));

}

 

function sigClass(x){return x==='LONG'?'sig-long':x==='SHORT'?'sig-short':x==='ERROR'?'sig-error':'sig-none'}

function sigPill(x){const c=x==='LONG'?'long':x==='SHORT'?'short':'flat';return `<span class="pill ${c}">${x}</span>`}

 

let watchlistSymbols=new Set();

function addCell(symbol,market,signal){

  if(signal!=='LONG'&&signal!=='SHORT') return '<span class="text-faint">—</span>';

  if(watchlistSymbols.has(symbol)) return '<span class="added-tag">Eklendi ✓</span>';

  return `<button class="add-btn" onclick="addToWatchlist('${symbol}','${market}','${signal}',this)">+ Ekle</button>`;

}

async function addToWatchlist(symbol,market,signal,btn){

  if(btn){btn.disabled=true;btn.textContent='Ekleniyor…';}

  try{

    const r=await fetch(`/api/watchlist/add?symbol=${encodeURIComponent(symbol)}&market=${market}&signal=${encodeURIComponent(signal)}`,{cache:'no-store'});

    const d=await r.json();

    if(!d.ok && btn){btn.disabled=false;btn.textContent='+ Ekle';alert(d.error||'Eklenemedi');}

  }catch(e){ if(btn){btn.disabled=false;btn.textContent='+ Ekle';} }

  await refreshWatchlist();

}

async function removeFromWatchlist(symbol){

  try{ await fetch(`/api/watchlist/remove?symbol=${encodeURIComponent(symbol)}`,{cache:'no-store'}); }catch(e){}

  await refreshWatchlist();

}

function renderWatchlistTable(){

  const d=watchlistCache;

  document.getElementById('wlUsd').textContent=Number(d.position_usd||0).toLocaleString('en-US');

  const items=d.items||[];

  watchlistSymbols=new Set(items.map(x=>x.symbol));

  document.getElementById('watchlistCount').textContent=`${items.length} / ${d.max_symbols??'—'}`;

 

  // Pinned row for the main ETH engine — always present, never removable, and

  // clickable just like any other tracked symbol.

  let rowsHtml='';

  if(statusCache){

    const p=statusCache.position;

    const sideCell=p?`<span class="pill ${p.side.toLowerCase()}">${p.side}</span>`:sigPill((statusCache.signals&&statusCache.signals.final)||'NO SIGNAL');

    const pnlCell=p?`<span class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</span>`:'<span class="text-faint">pozisyon yok</span>';

    const sel=selectedSymbol==='ETHUSDT'?' row-selected':'';

    rowsHtml+=`<tr class="row-clickable${sel}" onclick="selectSymbol('ETHUSDT')"><td><b>ETHUSDT</b></td><td>Binance</td><td>${sideCell}</td><td class="num">${num(p?p.current_price:statusCache.price)}</td><td class="num">${pnlCell}</td><td class="text-faint">Ana motor</td><td></td></tr>`;

  }

 

  rowsHtml+=items.map(x=>{

    const p=x.position;

    let sideCell, pnlCell;

    if(p){

      sideCell=`<span class="pill ${p.side.toLowerCase()}">${p.side}</span>`;

      pnlCell=`<span class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</span>`;

    } else {

      sideCell=sigPill(x.current_signal||'NO SIGNAL');

      pnlCell=x.market==='bist'?'<span class="text-faint">izleniyor</span>':'<span class="text-faint">pozisyon yok</span>';

    }

    const added=(x.added_at||'').replace('T',' ').slice(0,16);

    const sel=selectedSymbol===x.symbol?' row-selected':'';

    const marketLabel=x.market==='bist'?'XUTUM':x.market==='us_stock'?'ABD Hisse':'Binance';

    return `<tr class="row-clickable${sel}" onclick="selectSymbol('${x.symbol}')"><td><b>${x.symbol}</b></td><td>${marketLabel}</td><td>${sideCell}</td><td class="num">${num(p?p.current_price:x.current_price)}</td><td class="num">${pnlCell}</td><td class="text-faint">${added}</td><td><button class="btn" onclick="event.stopPropagation();removeFromWatchlist('${x.symbol}')">Kaldır</button></td></tr>`;

  }).join('');

 

  document.getElementById('watchlistRows').innerHTML=rowsHtml||'<tr><td colspan="7" class="watchlist-empty">Takip listesi boş. Tarayıcıda LONG/SHORT veren bir sembole "+ Ekle" diyerek botun izlemesini/paper trade etmesini sağlayabilirsin.</td></tr>';

}

 

async function refreshWatchlist(){

  let d;

  try{ const r=await fetch('/api/watchlist',{cache:'no-store'}); d=await r.json(); }catch(e){ return; }

  watchlistCache=d;

  renderWatchlistTable();

  if(selectedSymbol!=='ETHUSDT') renderDetail();

  renderScanner(); renderBistScanner(); renderUsScanner();

}

 

const sortState={scanner:{key:null,dir:1},bist:{key:null,dir:1},us:{key:null,dir:1}};

function attachSort(tblId,cacheGetter,renderFn){

  document.querySelectorAll(`#${tblId} th[data-key]`).forEach(th=>{

    if(!th.classList.contains('sortable'))return;

    th.addEventListener('click',()=>{

      const tbl=th.dataset.tbl,key=th.dataset.key;

      const state=sortState[tbl];

      state.dir=(state.key===key)?-state.dir:1; state.key=key;

      document.querySelectorAll(`#${tblId} th`).forEach(h=>h.classList.remove('sort-active'));

      th.classList.add('sort-active');

      renderFn();

    });

  });

}

function sortRows(rows,tbl){

  const state=sortState[tbl];

  if(!state.key) return rows;

  const k=state.key,dir=state.dir;

  return rows.slice().sort((a,b)=>{

    let av=a[k],bv=b[k];

    const an=Number(av),bn=Number(bv);

    if(!isNaN(an)&&!isNaN(bn)&&av!==null&&bv!==null){return (an-bn)*dir;}

    return String(av??'').localeCompare(String(bv??''))*dir;

  });

}

 

async function scannerData(){try{let r=await fetch('/api/scanner',{cache:'no-store'});return await r.json()}catch(e){return {status:'ERROR',results:[],last_error:String(e)}}}

let scannerCache={results:[]};

function renderScanner(){

  let q=(document.getElementById('coinSearch')?.value||'').toUpperCase();

  let f=document.getElementById('signalFilter')?.value||'ALL';

  let rows=scannerCache.results.filter(x=>(!q||x.symbol.includes(q))&&(f==='ALL'||x.signal===f));

  rows=sortRows(rows,'scanner');

  document.getElementById('scannerRows').innerHTML=rows.map(x=>`<tr><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td class="num">${Number(x.volume||0).toLocaleString('en-US',{maximumFractionDigits:0})}</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'crypto',x.signal)}</td></tr>`).join('')||'<tr><td colspan="13" class="empty">Sonuç yok.</td></tr>';

  document.getElementById('coinCount').textContent=rows.length+' coin';

  document.getElementById('longCount').textContent='LONG '+rows.filter(x=>x.signal==='LONG').length;

  document.getElementById('shortCount').textContent='SHORT '+rows.filter(x=>x.signal==='SHORT').length;

  document.getElementById('noCount').textContent='NO SIGNAL '+rows.filter(x=>x.signal==='NO SIGNAL').length;

}

async function bistScannerData(){try{let r=await fetch('/api/bist-scanner',{cache:'no-store'});return await r.json()}catch(e){return {status:'ERROR',results:[],last_error:String(e)}}}

let bistScannerCache={results:[]};

function renderBistScanner(){

  let q=(document.getElementById('bistSearch')?.value||'').toUpperCase();

  let f=document.getElementById('bistSignalFilter')?.value||'ALL';

  let rows=bistScannerCache.results.filter(x=>(!q||x.symbol.includes(q))&&(f==='ALL'||x.signal===f));

  rows=sortRows(rows,'bist');

  document.getElementById('bistScannerRows').innerHTML=rows.map(x=>`<tr><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.stoch_k??'—'} / ${x.stoch_d??'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'bist',x.signal)}</td></tr>`).join('')||'<tr><td colspan="13" class="empty">Sonuç yok.</td></tr>';

  document.getElementById('bistCount').textContent=rows.length+' hisse';

  document.getElementById('bistLongCount').textContent='LONG '+rows.filter(x=>x.signal==='LONG').length;

  document.getElementById('bistShortCount').textContent='SHORT '+rows.filter(x=>x.signal==='SHORT').length;

  document.getElementById('bistNoCount').textContent='NO SIGNAL '+rows.filter(x=>x.signal==='NO SIGNAL').length;

}

attachSort('scannerTable',()=>scannerCache,renderScanner);

attachSort('bistScannerTable',()=>bistScannerCache,renderBistScanner);

attachSort('usScannerTable',()=>usScannerCache,renderUsScanner);

 

async function refreshBistScanner(){

  let d=await bistScannerData();bistScannerCache=d;

  let st=d.status||'IDLE';let src=d.universe_source?` &middot; Evren: ${d.universe_source}`:'';

  let txt=st==='SCANNING'?`XUTUM taraması: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`Hazır &middot; Son 4H: ${d.last_scan_candle||'—'}${src}`:st==='ERROR'?`Hata: ${d.last_error||'Bilinmeyen hata'}`:'Bekleniyor…';

  document.getElementById('bistScannerStatus').textContent=txt;renderBistScanner();

}

async function startBistScanner(force=false){document.getElementById('bistScannerStatus').textContent='XUTUM taraması başlatılıyor…';try{await fetch('/api/bist-scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshBistScanner();}

refreshBistScanner();setInterval(refreshBistScanner,10000);

 

async function usScannerData(){try{let r=await fetch('/api/us-scanner',{cache:'no-store'});return await r.json()}catch(e){return {status:'ERROR',results:[],last_error:String(e)}}}

let usScannerCache={results:[]};

function renderUsScanner(){

  let q=(document.getElementById('usSearch')?.value||'').toUpperCase();

  let f=document.getElementById('usSignalFilter')?.value||'ALL';

  let rows=usScannerCache.results.filter(x=>(!q||x.symbol.includes(q))&&(f==='ALL'||x.signal===f));

  rows=sortRows(rows,'us');

  document.getElementById('usScannerRows').innerHTML=rows.map(x=>`<tr><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.stoch_k??'—'} / ${x.stoch_d??'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'us_stock',x.signal)}</td></tr>`).join('')||'<tr><td colspan="13" class="empty">Sonuç yok.</td></tr>';

  document.getElementById('usCount').textContent=rows.length+' hisse';

  document.getElementById('usLongCount').textContent='LONG '+rows.filter(x=>x.signal==='LONG').length;

  document.getElementById('usShortCount').textContent='SHORT '+rows.filter(x=>x.signal==='SHORT').length;

  document.getElementById('usNoCount').textContent='NO SIGNAL '+rows.filter(x=>x.signal==='NO SIGNAL').length;

}

async function refreshUsScanner(){

  let d=await usScannerData();usScannerCache=d;

  let st=d.status||'IDLE';let src=d.universe_source?` &middot; Evren: ${d.universe_source}`:'';

  let txt=st==='SCANNING'?`Tarama: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`Hazır &middot; Son 4H: ${d.last_scan_candle||'—'}${src}`:st==='ERROR'?`Hata: ${d.last_error||'Bilinmeyen hata'}`:'Bekleniyor…';

  document.getElementById('usScannerStatus').textContent=txt;renderUsScanner();

}

async function startUsScanner(force=false){document.getElementById('usScannerStatus').textContent='Tarama başlatılıyor… (514 hisse, birkaç dakika sürebilir)';try{await fetch('/api/us-scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshUsScanner();}

refreshUsScanner();setInterval(refreshUsScanner,10000);

 

async function refreshScanner(){

  let d=await scannerData();scannerCache=d;

  let st=d.status||'IDLE';

  let txt=st==='SCANNING'?`Tarama yapılıyor: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`Hazır &middot; Son 4H tarama: ${d.last_scan_candle||'—'}`:st==='ERROR'?`Hata: ${d.last_error||'Bilinmeyen hata'}`:'Bekleniyor…';

  document.getElementById('scannerStatus').textContent=txt;renderScanner();

}

async function startScanner(force=false){document.getElementById('scannerStatus').textContent='Tarama başlatılıyor…';try{await fetch('/api/scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshScanner();}

refreshScanner();setInterval(refreshScanner,10000);

 

async function refresh(){

  try{let r=await fetch('/api/status',{cache:'no-store'});let d=await r.json();render(d)}

  catch(e){const st=document.getElementById('status');st.className='status-pill err';st.innerHTML='<span class="dot"></span>Bağlantı hatası';}

}

refresh();setInterval(refresh,5000);

refreshWatchlist();setInterval(refreshWatchlist,10000);

 

async function refreshMyLive(){

  let d;

  try{ const r=await fetch('/api/live/my-positions',{cache:'no-store'}); d=await r.json(); }catch(e){ return; }

  const open=d.open||[], closed=d.closed||[];

  document.getElementById('liveMineCount').textContent = d.live_trading_enabled

    ? `${open.length} açık pozisyon`

    : 'Canlı işlem kapalı';

 

  document.getElementById('liveMineOpenRows').innerHTML = open.map(p=>{

    const opened=(p.entry_time||'').replace('T',' ').slice(0,16);

    return `<tr><td><b>${p.symbol}</b></td><td><span class="pill ${String(p.side).toLowerCase()}">${p.side}</span></td>`

      +`<td class="num">${num(p.qty)}</td><td class="num">${num(p.entry_price)}</td><td class="num">${num(p.current_price)}</td>`

      +`<td class="num ${cls(p.unrealized_pnl)}"><b>${money(p.unrealized_pnl)}</b></td><td>${p.leverage||1}x</td><td class="text-faint">${opened}</td>`

      +`<td><button class="btn btn-danger" onclick="closeLivePosition('${p.symbol}',this)">Şimdi Kapat</button></td></tr>`;

  }).join('') || '<tr><td colspan="9" class="empty">Şu an açık canlı pozisyonunuz yok.</td></tr>';

 

  document.getElementById('liveMineClosedRows').innerHTML = closed.map(t=>{

    return `<tr><td>${(t.exit_time||'—')}</td><td><span class="pill ${String(t.side).toLowerCase()}">${t.side}</span></td><td>${t.symbol}</td>`

      +`<td class="num">${num(t.entry_price)}</td><td class="num">${num(t.exit_price)}</td>`

      +`<td class="num ${cls(t.pnl)}"><b>${money(t.pnl)}</b></td><td class="wrap-cell">${t.reason||''}</td></tr>`;

  }).join('') || '<tr><td colspan="7" class="empty">Henüz kapanmış canlı işleminiz yok.</td></tr>';

}

refreshMyLive();setInterval(refreshMyLive,15000);

 

async function closeLivePosition(symbol,btn){

  if(!confirm(`${symbol} pozisyonunu şimdi gerçek bir market emriyle kapatmak istediğinize emin misiniz? Bu işlem geri alınamaz.`)) return;

  btn.disabled=true; btn.textContent='Kapatılıyor…';

  try{

    const r=await fetch('/api/live/close-position',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol})});

    const d=await r.json();

    if(!d.ok){ alert(d.error||'Pozisyon kapatılamadı'); btn.disabled=false; btn.textContent='Şimdi Kapat'; return; }

  }catch(e){ alert('Bağlantı hatası'); btn.disabled=false; btn.textContent='Şimdi Kapat'; return; }

  await refreshMyLive();

}

 

async function refreshAiAnalysis(){

  let d;

  try{ const r=await fetch('/api/ai-analysis',{cache:'no-store'}); d=await r.json(); }catch(e){ return; }

  const body=document.getElementById('aiAnalysisBody');

  if(!d.enabled){ body.innerHTML='<div class="ai-disabled">AI Analist devre dışı — ANTHROPIC_API_KEY tanımlı değil.</div>'; return; }

  const a=d.analysis;

  if(!a || !a.ok){

    let msg='Henüz bir analiz üretilmedi. "Şimdi Analiz Et" ile ilk raporu oluşturabilirsiniz.';

    if(d.last_error && d.last_error.error){

      msg='Son deneme başarısız oldu ('+(d.last_error.at||'').replace('T',' ').slice(0,16)+'): '+d.last_error.error;

    }

    body.innerHTML='<div class="ai-disabled">'+msg.replace(/</g,'&lt;')+'</div>';

    return;

  }

  const meta=`<div class="ai-meta">${(a.generated_at||'').replace('T',' ').slice(0,16)} &middot; ${a.trades_analyzed} işlem incelendi (toplam ${a.total_trades_all_time})</div>`;

  body.innerHTML=meta+'<div>'+a.text.replace(/</g,'&lt;')+'</div>';

}

async function runAiAnalysis(){

  const btn=document.getElementById('aiRunBtn');

  btn.disabled=true; btn.textContent='Analiz ediliyor…';

  try{ await fetch('/api/ai-analysis/run',{cache:'no-store'}); }catch(e){}

  await refreshAiAnalysis();

  btn.disabled=false; btn.textContent='Şimdi Analiz Et';

}

refreshAiAnalysis();setInterval(refreshAiAnalysis,60000);

 

async function logout(){

  try{ await fetch('/logout',{method:'POST',cache:'no-store'}); }catch(e){}

  window.location='/login';

}

 

function renderAccount(a){

  const pill=document.getElementById('binanceStatusPill');

  const body=document.getElementById('accountBody');

  let whoText=a.username?('👤 '+a.username):'';

  if(a.is_admin){ whoText+=' <span class="badge-admin">(admin)</span>'; }

  else if(a.subscription_status==='trial'){ whoText+=` <span class="badge-trial">Deneme: ${a.days_left} gün kaldı</span>`; }

  else if(a.subscription_status==='expired'){ whoText+=' <span class="badge-error">Deneme doldu</span>'; }

  document.getElementById('whoami').innerHTML=whoText;

  document.getElementById('adminLink').style.display=a.is_admin?'inline-block':'none';

  if(a.binance_connected){

    if(a.binance_verify_error){ pill.innerHTML='<span class="badge-error">Doğrulama hatası</span>'; }

    else if(a.binance_verified_at){ pill.innerHTML='<span class="badge-verified">Bağlı ve doğrulandı</span>'; }

    else{ pill.innerHTML='<span class="badge-unverified">Bağlı, doğrulanmadı</span>'; }

  } else {

    pill.innerHTML='<span class="badge-unverified">Bağlı değil</span>';

  }

  let notice='';

  if(!a.credential_encryption_ready){

    notice=`<div class="account-notice">⚠️ Sunucuda CREDENTIAL_ENCRYPTION_KEY tanımlı değil — API anahtarları güvenle şifrelenemediği için kaydedilemez. Lütfen yöneticinizle iletişime geçin.</div>`;

  }

  const maskedRow=a.binance_connected?`<div class="account-row">Kayıtlı anahtar: <b>${a.binance_key_masked}</b></div>`:'';

  const verifyRow=a.binance_verified_at?`<div class="account-row text-faint">Son doğrulama: ${a.binance_verified_at.replace('T',' ').slice(0,16)}</div>`

    :(a.binance_verify_error?`<div class="account-row"><span class="badge-error">${a.binance_verify_error}</span></div>`:'');

  const riskRow=a.risk_ack_at

    ? `<div class="account-row text-faint">Risk onayı: ${a.risk_ack_at.replace('T',' ').slice(0,16)} tarihinde verildi</div>`

    : `<label class="risk-ack"><input type="checkbox" id="riskAck"> Bu botun kripto vadeli işlemlerde gerçek para ile emir açabileceğini, kayıp riski taşıdığını ve olası kayıplardan botun değil kendi sorumluluğumda olduğumu anladığımı ve kabul ettiğimi onaylıyorum.</label>`;

  body.innerHTML=`

    ${maskedRow}${verifyRow}

    <form class="account-form" id="binanceForm" onsubmit="return submitBinanceForm(event)">

      <div>

        <label>Binance API Key</label>

        <input type="text" id="binApiKey" autocomplete="off" placeholder="${a.binance_connected?'Değiştirmek için yeni key girin':'Binance Futures API key'}">

      </div>

      <div>

        <label>Binance API Secret</label>

        <input type="password" id="binApiSecret" autocomplete="off" placeholder="${a.binance_connected?'Değiştirmek için yeni secret girin':'Binance Futures API secret'}">

      </div>

      ${riskRow}

      <div class="account-row">

        <button class="btn" type="submit" id="binSaveBtn">Kaydet ve Doğrula</button>

        ${a.binance_connected?'<button class="btn" type="button" onclick="disconnectBinance()">Bağlantıyı Kaldır</button>':''}

      </div>

    </form>

    ${notice}

  `;

  renderLivePanel(a);

  renderTelegramPanel(a);

}

 

function renderLivePanel(a){

  const box=document.getElementById('livePanelBody');

  if(!box) return;

  if(!a.binance_connected || !a.binance_verified_at){

    box.innerHTML=`<div class="account-notice">Canlı (gerçek para) işlem açabilmek için önce yukarıdan Binance API anahtarınızı kaydedip doğrulatmanız gerekiyor.</div>`;

    return;

  }

  const rt=a.live_runtime||{};

  let statusLine;

  if(a.global_kill_switch_active){

    statusLine=`<span class="badge-live-paused">🛑 Yönetici tarafından tüm canlı işlemler geçici olarak durduruldu</span>`;

  } else if(a.live_trading_enabled && rt.paused_today){

    statusLine=`<span class="badge-live-paused">⏸ Günlük maksimum kayıp limitine ulaşıldı — bugün için yeni işlem açılmıyor</span>`;

  } else if(a.live_trading_enabled){

    statusLine=`<span class="badge-live-on">🟢 Canlı işlem AÇIK</span>`;

  } else {

    statusLine=`<span class="badge-live-off">Canlı işlem kapalı — bot sadece paper (deneme) modda çalışıyor</span>`;

  }

  const openPos=rt.open_position_count?`<div class="account-row text-faint">Açık canlı pozisyon: ${rt.open_position_count}</div>`:'';

  const pnlRow=`<div class="account-row text-faint">Bugünkü tahmini gerçekleşmiş K/Z: ${(rt.realized_pnl_usd||0).toFixed(2)} USD</div>`;

  const errRow=rt.last_error?`<div class="account-row"><span class="badge-error">${(''+rt.last_error).slice(0,200)}</span></div>`:'';

  box.innerHTML=`

    <div class="account-row">${statusLine}</div>

    ${openPos}${pnlRow}${errRow}

    <form class="account-form" id="liveSettingsForm" onsubmit="return submitLiveSettings(event)" style="margin-top:10px">

      <div>

        <label>İşlem başına USD tutarı</label>

        <input type="number" step="0.01" min="0" id="livePositionUsd" value="${a.live_position_usd||''}" placeholder="Örn. 100">

      </div>

      <div>

        <label>Maksimum kaldıraç (1-${a.live_max_leverage_cap||10}x)</label>

        <input type="number" step="1" min="1" max="${a.live_max_leverage_cap||10}" id="liveMaxLeverage" value="${a.live_max_leverage||''}" placeholder="Örn. 2">

      </div>

      <div>

        <label>Günlük maksimum kayıp limiti (USD) — aşılırsa o gün otomatik durur</label>

        <input type="number" step="0.01" min="0" id="liveDailyLossLimit" value="${a.live_daily_loss_limit_usd||''}" placeholder="Örn. 50">

      </div>

      <div>

        <label>Maksimum açık pozisyon sayısı (1-${a.live_max_positions_cap||5})</label>

        <input type="number" step="1" min="1" max="${a.live_max_positions_cap||5}" id="liveMaxPositions" value="${a.live_max_open_positions||''}" placeholder="Örn. 1">

      </div>

      <div class="account-row">

        <button class="btn" type="submit">Ayarları Kaydet</button>

        ${a.live_trading_enabled

          ? `<button class="btn" type="button" onclick="toggleLiveTrading(false)">Canlı İşlemi Kapat</button>`

          : `<button class="btn" type="button" onclick="toggleLiveTrading(true)" style="background:var(--bear);border-color:var(--bear-border)">Canlı İşlemi AÇ (gerçek para)</button>`}

      </div>

    </form>

    <div class="live-danger">⚠️ Canlı işlem açıldığında bot, kayıtlı Binance hesabınızda <b>gerçek parayla</b> emir açar/kapatır. Kayıplardan bot değil siz sorumlusunuz. Bu, yatırım tavsiyesi değildir; ilgili düzenlemelere (ör. SPK) uygunluk sizin sorumluluğunuzdadır.</div>

  `;

}

 

async function submitLiveSettings(ev){

  ev.preventDefault();

  const body={

    position_usd: document.getElementById('livePositionUsd').value,

    max_leverage: document.getElementById('liveMaxLeverage').value,

    daily_loss_limit_usd: document.getElementById('liveDailyLossLimit').value,

    max_open_positions: document.getElementById('liveMaxPositions').value,

  };

  try{

    const r=await fetch('/api/account/live-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

    const d=await r.json();

    if(!d.ok){ alert(d.error||'Kaydedilemedi'); }

  }catch(e){ alert('Bağlantı hatası'); }

  await refreshAccount();

  return false;

}

 

async function toggleLiveTrading(enabled){

  if(enabled && !confirm('Canlı işlemi açmak üzeresiniz. Bot bu andan itibaren Binance hesabınızda GERÇEK PARA ile emir açıp kapatacak. Kayıp riskini kabul ettiğinizi ve bu ayarları doğru girdiğinizi onaylıyor musunuz?')) return;

  try{

    const r=await fetch('/api/account/live-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});

    const d=await r.json();

    if(!d.ok){ alert(d.error||'İşlem başarısız'); }

  }catch(e){ alert('Bağlantı hatası'); }

  await refreshAccount();

}

 

// Holds an in-progress link code across renderAccount() re-renders (the

// polling below calls refreshAccount() every few seconds to notice the

// moment the user sends /start, and that re-render must not wipe the code

// that's still on screen).

let _tgActiveCode=null; // {code, bot_username, obtainedAt, ttlSeconds}

let _tgCodeTimer=null;

 

function renderTelegramPanel(a){

  const box=document.getElementById('telegramPanelBody');

  if(!box) return;

  if(!a.telegram_bot_enabled){

    if(_tgCodeTimer){ clearInterval(_tgCodeTimer); _tgCodeTimer=null; }

    box.innerHTML=`<div class="account-notice">Sunucuda Telegram botu tanımlı değil.</div>`;

    return;

  }

  if(a.telegram_linked){

    if(_tgCodeTimer){ clearInterval(_tgCodeTimer); _tgCodeTimer=null; }

    _tgActiveCode=null;

    box.innerHTML=`

      <div class="account-row">🟢 Telegram bağlı${a.telegram_username?(' — @'+a.telegram_username):''}</div>

      <div class="account-row text-faint">Canlı işlem giriş/çıkış bildirimleri, risk uyarıları ve günlük özet buraya gelecek.</div>

      <div class="account-row"><button class="btn" type="button" onclick="unlinkTelegram()">Bağlantıyı Kaldır</button></div>

    `;

    return;

  }

  if(_tgActiveCode){

    renderTelegramCodeBox();

    return;

  }

  box.innerHTML=`

    <div class="account-row text-faint">Canlı işlem bildirimlerinizi kendi Telegram'ınızda almak için bağlanın.</div>

    <div class="account-row"><button class="btn" type="button" id="tgLinkBtn" onclick="getTelegramLinkCode()">Bağlantı Kodu Al</button></div>

  `;

}

 

function renderTelegramCodeBox(){

  const box=document.getElementById('telegramPanelBody');

  if(!box || !_tgActiveCode) return;

  const {code, bot_username, obtainedAt, ttlSeconds}=_tgActiveCode;

  const remaining=Math.max(0, ttlSeconds - Math.floor((Date.now()-obtainedAt)/1000));

  const botLink=bot_username?`https://t.me/${bot_username}`:null;

  box.innerHTML=`

    <div class="account-notice">

      1) Telegram'da ${botLink?`<a href="${botLink}" target="_blank" style="color:var(--accent)">@${bot_username}</a>`:'botumuzu'} açın.<br>

      2) Şunu gönderin: <span class="tg-code">/start ${code}</span><br>

      <span class="text-faint">Kod ${Math.floor(remaining/60)} dakika ${remaining%60} saniye içinde geçersiz olur.</span>

    </div>`;

}

 

async function getTelegramLinkCode(){

  const btn=document.getElementById('tgLinkBtn');

  if(btn){ btn.disabled=true; }

  try{

    const r=await fetch('/api/account/telegram/link-code',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});

    const d=await r.json();

    if(!d.ok){ alert(d.error||'Kod alınamadı'); if(btn) btn.disabled=false; return; }

    _tgActiveCode={code:d.code, bot_username:d.bot_username, obtainedAt:Date.now(), ttlSeconds:d.expires_in_seconds||600};

    renderTelegramCodeBox();

    if(_tgCodeTimer) clearInterval(_tgCodeTimer);

    _tgCodeTimer=setInterval(async ()=>{

      if(!_tgActiveCode){ clearInterval(_tgCodeTimer); return; }

      const remaining=_tgActiveCode.ttlSeconds - Math.floor((Date.now()-_tgActiveCode.obtainedAt)/1000);

      if(remaining<=0){ clearInterval(_tgCodeTimer); _tgActiveCode=null; await refreshAccount(); return; }

      await refreshAccount(); // re-renders; if /start already landed, telegram_linked flips to true

    },4000);

  }catch(e){ alert('Bağlantı hatası'); }

  if(btn) btn.disabled=false;

}

 

async function unlinkTelegram(){

  if(!confirm('Telegram bağlantısını kaldırmak istediğinize emin misiniz?')) return;

  try{ await fetch('/api/account/telegram/unlink',{method:'POST',cache:'no-store'}); }catch(e){}

  await refreshAccount();

}

 

async function refreshAccount(){

  let d;

  try{ const r=await fetch('/api/account',{cache:'no-store'}); if(r.status===401){window.location='/login';return;} d=await r.json(); }catch(e){ return; }

  renderAccount(d);

}

 

async function submitBinanceForm(ev){

  ev.preventDefault();

  const key=document.getElementById('binApiKey').value.trim();

  const secret=document.getElementById('binApiSecret').value.trim();

  const riskEl=document.getElementById('riskAck');

  const riskAck=riskEl?riskEl.checked:true; // already acked previously -> element isn't shown

  if(!key||!secret){ alert('API key ve secret gerekli.'); return false; }

  if(riskEl && !riskAck){ alert('Devam etmeden önce risk onayı kutusunu işaretlemelisiniz.'); return false; }

  const btn=document.getElementById('binSaveBtn');

  btn.disabled=true; btn.textContent='Kaydediliyor ve doğrulanıyor…';

  try{

    const r=await fetch('/api/account/connect-binance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,api_secret:secret,risk_ack:riskAck})});

    const d=await r.json();

    if(!d.ok){ alert(d.error||'Kaydedilemedi'); }

  }catch(e){ alert('Bağlantı hatası'); }

  btn.disabled=false; btn.textContent='Kaydet ve Doğrula';

  await refreshAccount();

  return false;

}

 

async function disconnectBinance(){

  if(!confirm('Binance bağlantısını kaldırmak istediğinize emin misiniz?')) return;

  try{ await fetch('/api/account/disconnect-binance',{method:'POST',cache:'no-store'}); }catch(e){}

  await refreshAccount();

}

 

refreshAccount();

</script></body></html>'''

 

_AUTH_STYLE = r'''

<style>

:root{

  --bg:#090c12; --bg-elev:#0d1119; --panel:#111623; --panel-2:#161c2c;

  --border:#212a3d; --border-soft:#1a2233;

  --text:#e7ecf6; --text-dim:#8b93a8; --text-faint:#565f74;

  --accent:#d4a857; --accent-soft:#3a3120;

  --bull:#3ecf8e; --bull-bg:#0f2419; --bull-border:#1e4531;

  --bear:#f1596e; --bear-bg:#2a151b; --bear-border:#4a2530;

  --font-d:'Space Grotesk','IBM Plex Sans',system-ui,-apple-system,sans-serif;

  --font-m:'JetBrains Mono','IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;

}

*{box-sizing:border-box}

body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-d);-webkit-font-smoothing:antialiased}

.auth-page{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}

.auth-card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:32px;width:100%;max-width:380px}

.auth-card h1{font-size:19px;margin:0 0 4px}

.auth-card p.sub{color:var(--text-dim);font-size:12.5px;margin:0 0 22px}

.auth-card label{font-size:11.5px;color:var(--text-faint);display:block;margin-bottom:5px}

.auth-card input{width:100%;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:13.5px;font-family:var(--font-d);margin-bottom:14px}

.auth-card button.primary{width:100%;background:var(--accent);color:#1a1406;border:none;border-radius:8px;padding:11px;font-size:13.5px;font-weight:700;cursor:pointer;font-family:var(--font-d)}

.auth-card button.primary:disabled{opacity:.6;cursor:default}

.auth-error{background:var(--bear-bg);border:1px solid var(--bear-border);color:var(--bear);border-radius:8px;padding:9px 11px;font-size:12.5px;margin-bottom:14px;display:none}

.auth-switch{text-align:center;margin-top:16px;font-size:12.5px;color:var(--text-dim)}

.auth-switch a{color:var(--accent);text-decoration:none}

.auth-divider{display:flex;align-items:center;gap:10px;margin:16px 0;color:var(--text-faint);font-size:11.5px}

.auth-divider::before,.auth-divider::after{content:'';flex:1;height:1px;background:var(--border)}

</style>

'''

 

LOGIN_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Giriş — A&amp;I Trading Terminal</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + r'''</head>

<body>

<div class="auth-page"><div class="auth-card">

  <h1>A&amp;I Trading Terminal</h1>

  <p class="sub">Devam etmek için giriş yapın</p>

  <div class="auth-error" id="err"></div>

  __AUTH0_LOGIN_BLOCK__

  <form onsubmit="return doLogin(event)">

    <label>Kullanıcı adı</label>

    <input type="text" id="username" autocomplete="username" required>

    <label>Şifre</label>

    <input type="password" id="password" autocomplete="current-password" required>

    <button class="primary" type="submit" id="btn">Giriş Yap</button>

  </form>

  <div class="auth-switch" style="margin-top:4px"><a href="/forgot-password">Şifremi unuttum</a></div>

  <div class="auth-switch">Hesabınız yok mu? <a href="/register">Kayıt olun</a></div>

</div></div>

<script>

async function doLogin(ev){

  ev.preventDefault();

  const btn=document.getElementById('btn'), err=document.getElementById('err');

  err.style.display='none'; btn.disabled=true; btn.textContent='Giriş yapılıyor…';

  try{

    const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({

      username:document.getElementById('username').value.trim(),

      password:document.getElementById('password').value,

    })});

    const d=await r.json();

    if(d.ok){ window.location='/'; return false; }

    err.textContent=d.error||'Giriş başarısız'; err.style.display='block';

  }catch(e){ err.textContent='Bağlantı hatası'; err.style.display='block'; }

  btn.disabled=false; btn.textContent='Giriş Yap';

  return false;

}

</script>

</body></html>'''

 

REGISTER_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Kayıt Ol — A&amp;I Trading Terminal</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + r'''</head>

<body>

<div class="auth-page"><div class="auth-card">

  <h1>Hesap oluştur</h1>

  <p class="sub">A&amp;I Trading Terminal'e katılın — __TRIAL_DAYS__ gün ücretsiz deneme</p>

  <div class="auth-error" id="err"></div>

  __AUTH0_LOGIN_BLOCK__

  <form onsubmit="return doRegister(event)">

    <label>Kullanıcı adı</label>

    <input type="text" id="username" autocomplete="username" required minlength="3" maxlength="32">

    <label>E-posta (opsiyonel)</label>

    <input type="email" id="email" autocomplete="email">

    <label>Şifre (en az 8 karakter)</label>

    <input type="password" id="password" autocomplete="new-password" required minlength="8">

    <button class="primary" type="submit" id="btn">Kayıt Ol</button>

  </form>

  <div class="auth-switch">Zaten hesabınız var mı? <a href="/login">Giriş yapın</a></div>

</div></div>

<script>

async function doRegister(ev){

  ev.preventDefault();

  const btn=document.getElementById('btn'), err=document.getElementById('err');

  err.style.display='none'; btn.disabled=true; btn.textContent='Kayıt olunuyor…';

  try{

    const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({

      username:document.getElementById('username').value.trim(),

      email:document.getElementById('email').value.trim(),

      password:document.getElementById('password').value,

    })});

    const d=await r.json();

    if(d.ok){ window.location='/'; return false; }

    err.textContent=d.error||'Kayıt başarısız'; err.style.display='block';

  }catch(e){ err.textContent='Bağlantı hatası'; err.style.display='block'; }

  btn.disabled=false; btn.textContent='Kayıt Ol';

  return false;

}

</script>

</body></html>'''

 

FORGOT_PASSWORD_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Şifremi unuttum — Herobot-ai</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + r'''</head>

<body>

<div class="auth-page"><div class="auth-card">

  <h1>Şifremi unuttum</h1>

  <p class="sub">Hesabınıza kayıtlı e-posta adresini girin, size bir sıfırlama bağlantısı gönderelim.</p>

  <div class="auth-error" id="err"></div>

  <div class="auth-error" id="ok" style="display:none;background:rgba(61,217,168,0.12);border-color:#23A57B;color:#3DD9A8"></div>

  <form onsubmit="return doForgot(event)" id="form">

    <label>E-posta</label>

    <input type="email" id="email" autocomplete="email" required>

    <button class="primary" type="submit" id="btn">Sıfırlama Bağlantısı Gönder</button>

  </form>

  <div class="auth-switch"><a href="/login">Girişe dön</a></div>

</div></div>

<script>

async function doForgot(ev){

  ev.preventDefault();

  const btn=document.getElementById('btn'), err=document.getElementById('err'), ok=document.getElementById('ok');

  err.style.display='none'; ok.style.display='none'; btn.disabled=true; btn.textContent='Gönderiliyor…';

  try{

    const r=await fetch('/api/auth/forgot-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({

      email:document.getElementById('email').value.trim(),

    })});

    const d=await r.json();

    document.getElementById('form').style.display='none';

    ok.textContent='Eğer bu e-posta adresi kayıtlıysa, birazdan gelen kutunuza bir şifre sıfırlama bağlantısı ulaşacak.';

    ok.style.display='block';

    return false;

  }catch(e){ err.textContent='Bağlantı hatası'; err.style.display='block'; }

  btn.disabled=false; btn.textContent='Sıfırlama Bağlantısı Gönder';

  return false;

}

</script>

</body></html>'''

 

RESET_PASSWORD_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Şifre sıfırla — Herobot-ai</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + r'''</head>

<body>

<div class="auth-page"><div class="auth-card">

  <h1>Yeni şifre belirle</h1>

  <p class="sub">Hesabınız için yeni bir şifre girin.</p>

  <div class="auth-error" id="err"></div>

  <div class="auth-error" id="ok" style="display:none;background:rgba(61,217,168,0.12);border-color:#23A57B;color:#3DD9A8"></div>

  <form onsubmit="return doReset(event)" id="form">

    <label>Yeni şifre (en az 8 karakter)</label>

    <input type="password" id="password" autocomplete="new-password" required minlength="8">

    <label>Yeni şifre (tekrar)</label>

    <input type="password" id="password2" autocomplete="new-password" required minlength="8">

    <button class="primary" type="submit" id="btn">Şifreyi Sıfırla</button>

  </form>

  <div class="auth-switch"><a href="/login">Girişe dön</a></div>

</div></div>

<script>

function tokenFromUrl(){ return new URLSearchParams(window.location.search).get('token') || ''; }

async function doReset(ev){

  ev.preventDefault();

  const btn=document.getElementById('btn'), err=document.getElementById('err'), ok=document.getElementById('ok');

  err.style.display='none'; ok.style.display='none';

  const p1=document.getElementById('password').value, p2=document.getElementById('password2').value;

  if(p1!==p2){ err.textContent='Şifreler eşleşmiyor'; err.style.display='block'; return false; }

  const token=tokenFromUrl();

  if(!token){ err.textContent='Geçersiz veya eksik bağlantı. Lütfen yeni bir sıfırlama bağlantısı isteyin.'; err.style.display='block'; return false; }

  btn.disabled=true; btn.textContent='Kaydediliyor…';

  try{

    const r=await fetch('/api/auth/reset-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token,password:p1})});

    const d=await r.json();

    if(d.ok){

      document.getElementById('form').style.display='none';

      ok.textContent='Şifreniz güncellendi. Şimdi yeni şifrenizle giriş yapabilirsiniz.';

      ok.style.display='block';

      setTimeout(()=>{ window.location='/login'; }, 2000);

      return false;

    }

    err.textContent=d.error||'Şifre sıfırlanamadı'; err.style.display='block';

  }catch(e){ err.textContent='Bağlantı hatası'; err.style.display='block'; }

  btn.disabled=false; btn.textContent='Şifreyi Sıfırla';

  return false;

}

</script>

</body></html>'''

 

# Shown on /login and /register in place of __AUTH0_LOGIN_BLOCK__ when Auth0

# is configured (auth.auth0_enabled()). It's a plain link, not a JS SDK —

# clicking it starts the standard OAuth2 authorization-code redirect flow

# handled by GET /auth0/login and GET /callback below.

AUTH0_LOGIN_BLOCK = r'''<a href="/auth0/login" style="display:block;text-align:center;text-decoration:none;padding:11px;border-radius:8px;background:var(--accent);color:#1a1406;font-weight:700;font-size:13.5px;font-family:var(--font-d)">Google ile giriş yap</a>

  <div class="auth-divider">veya</div>'''

 

TRIAL_EXPIRED_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Deneme süresi doldu — A&amp;I Trading Terminal</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + r'''</head>

<body>

<div class="auth-page"><div class="auth-card">

  <h1>Deneme süreniz doldu</h1>

  <p class="sub">__USERNAME__ hesabınızın ücretsiz deneme süresi sona erdi. Kullanmaya devam etmek için aboneliğinizi aktive etmemiz gerekiyor.</p>

  <div class="auth-notice" style="background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;font-size:12.5px;color:var(--text-dim);margin-bottom:16px">

    Aboneliğinizi aktive etmek için lütfen bizimle iletişime geçin. Ödemeniz onaylandıktan sonra hesabınız birkaç dakika içinde tekrar aktif olacaktır.

  </div>

  <button class="primary" onclick="logout()">Çıkış Yap</button>

</div></div>

<script>

async function logout(){ try{ await fetch('/logout',{method:'POST'}); }catch(e){} window.location='/login'; }

</script>

</body></html>'''

 

_ADMIN_STYLE_EXTRA = r'''

<style>

.admin-wrap{max-width:1100px;margin:0 auto;padding:28px 20px}

.admin-wrap h1{font-size:20px;margin:0 0 4px}

.admin-wrap p.sub{color:var(--text-dim);font-size:12.5px;margin:0 0 20px}

table.admin-table{width:100%;border-collapse:collapse;font-size:12.5px}

table.admin-table th{text-align:left;color:var(--text-faint);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.03em}

table.admin-table td{padding:9px 10px;border-bottom:1px solid var(--border-soft)}

table.admin-table select{background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font-family:var(--font-d);font-size:12px}

.badge{display:inline-block;padding:2px 8px;border-radius:100px;font-size:11px;font-weight:600}

.badge-active{background:var(--bull-bg);color:var(--bull);border:1px solid var(--bull-border)}

.badge-trial{background:var(--accent-soft);color:var(--accent);border:1px solid var(--border)}

.badge-expired{background:var(--bear-bg);color:var(--bear);border:1px solid var(--bear-border)}

.back-link{color:var(--accent);text-decoration:none;font-size:12.5px}

.text-faint{color:var(--text-faint)}

</style>

'''

 

ADMIN_HTML = r'''<!doctype html>

<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">

<title>Yönetim — A&amp;I Trading Terminal</title>

<link rel="preconnect" href="https://fonts.googleapis.com">

<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">

''' + _AUTH_STYLE + _ADMIN_STYLE_EXTRA + r'''</head>

<body>

<div class="admin-wrap">

  <a class="back-link" href="/">← Dashboard'a dön</a>

  <h1 style="margin-top:14px">Kullanıcılar</h1>

  <p class="sub">Ödeme aldığınız kullanıcıyı "aktif" yapın; deneme süresi dolanlar otomatik olarak dashboard'a erişemez.</p>

 

  <div id="killSwitchBox" class="account-notice" style="margin-bottom:16px">Yükleniyor…</div>

 

  <table class="admin-table" id="tbl">

    <thead><tr><th>Kullanıcı</th><th>E-posta</th><th>Giriş türü</th><th>Binance</th><th>Durum</th><th>Kalan gün</th><th>Canlı işlem</th><th>İşlem</th></tr></thead>

    <tbody id="tbody"><tr><td colspan="8">Yükleniyor…</td></tr></tbody>

  </table>

</div>

<script>

function badge(status){

  if(status==='active') return '<span class="badge badge-active">Aktif</span>';

  if(status==='trial') return '<span class="badge badge-trial">Deneme</span>';

  return '<span class="badge badge-expired">Süresi doldu</span>';

}

async function loadKillSwitch(){

  const r=await fetch('/api/admin/kill-switch',{cache:'no-store'});

  if(r.status!==200) return;

  const d=await r.json();

  const box=document.getElementById('killSwitchBox');

  if(d.active){

    box.innerHTML=`🛑 <b>ACİL DURDURMA AKTİF</b> — tüm kullanıcılar için yeni canlı emir açılmıyor (${d.set_by?('kapatan: '+d.set_by+', '):''}${(d.set_at||'').replace('T',' ').slice(0,16)}).

      <div style="margin-top:8px"><button class="btn" onclick="toggleKillSwitch(false)">Canlı işlemi tekrar aç</button></div>`;

  } else {

    box.innerHTML=`✅ Canlı işlem normal çalışıyor.

      <div style="margin-top:8px"><button class="btn" onclick="toggleKillSwitch(true)" style="background:var(--bear);border-color:var(--bear-border)">🛑 TÜM canlı işlemleri acil durdur</button></div>`;

  }

}

async function toggleKillSwitch(active){

  if(active && !confirm('Bu, TÜM kullanıcıların yeni canlı (gerçek para) işlem açmasını hemen durduracak. Zaten açık olan pozisyonlar bot tarafından SL/TP ile yönetilmeye devam eder. Emin misiniz?')) return;

  await fetch('/api/admin/kill-switch/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active})});

  loadKillSwitch();

}

async function load(){

  const r=await fetch('/api/admin/users',{cache:'no-store'});

  if(r.status===403){ document.getElementById('tbody').innerHTML='<tr><td colspan="8">Bu sayfaya erişim yetkiniz yok.</td></tr>'; return; }

  const d=await r.json();

  const rows=d.users.map(u=>`

    <tr>

      <td>${u.username}${u.is_admin?' <span class="text-faint">(admin)</span>':''}</td>

      <td>${u.email||'—'}</td>

      <td>${u.auth_provider==='auth0'?'Google':'Şifre'}</td>

      <td>${u.binance_connected?(u.binance_verified_at?'Doğrulandı':'Bağlı'):'—'}</td>

      <td>${badge(u.subscription_status)}</td>

      <td>${u.days_left!=null?u.days_left+' gün':'—'}</td>

      <td>${u.live_trading_enabled?`<span class="badge badge-active">Açık</span> <span class="text-faint">(${u.live_position_usd||0} USD, ${u.live_max_leverage||1}x, max ${u.live_max_open_positions||1} pozisyon, limit ${u.live_daily_loss_limit_usd||0} USD)</span>`:'<span class="text-faint">Kapalı</span>'}</td>

      <td>

        <select onchange="setStatus('${u.username}', this.value)">

          <option value="trial" ${u.payment_status==='trial'?'selected':''}>Deneme</option>

          <option value="active" ${u.payment_status==='active'?'selected':''}>Aktif (ödedi)</option>

          <option value="expired" ${u.payment_status==='expired'?'selected':''}>Süresi doldu</option>

          <option value="inactive" ${u.payment_status==='inactive'?'selected':''}>Pasif</option>

        </select>

      </td>

    </tr>`).join('');

  document.getElementById('tbody').innerHTML = rows || '<tr><td colspan="8">Henüz kullanıcı yok.</td></tr>';

}

async function setStatus(username, status){

  await fetch('/api/admin/set-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,status})});

  load();

}

loadKillSwitch();

load();

</script>

</body></html>'''

 

def read_state():

    try:

        return _load_shared_state(STARTING_EQUITY)

    except Exception:

        return {}

 

def read_trades():

    if not os.path.exists(TRADES_FILE): return []

    try:

        with open(TRADES_FILE,'r',encoding='utf-8',newline='') as f:return list(csv.DictReader(f))

    except Exception:return []

 

def f(v):

    try:return float(v)

    except:return 0.0

 

def status():

    s=read_state(); trades=read_trades(); closed=[]

    for t in trades:

        for k in ['entry_price','exit_price','net_pnl','gross_pnl','fees','equity_after','qty_eth']:

            if k in t:t[k]=f(t[k])

        closed.append(t)

    wins=sum(1 for t in closed if t.get('net_pnl',0)>0); losses=sum(1 for t in closed if t.get('net_pnl',0)<0)

    gross_win=sum(t['net_pnl'] for t in closed if t['net_pnl']>0); gross_loss=abs(sum(t['net_pnl'] for t in closed if t['net_pnl']<0))

    pf=gross_win/gross_loss if gross_loss else (999.0 if gross_win else 0.0)

    avg=sum(t['net_pnl'] for t in closed)/len(closed) if closed else 0

    start=float(os.environ.get('PAPER_INITIAL_CAPITAL','10000'))

    equity=f(s.get('equity',start)); net=equity-start

    peak=start; maxdd=0

    for t in closed:

        e=f(t.get('equity_after',start)); peak=max(peak,e); maxdd=min(maxdd,(e/peak-1)*100 if peak else 0)

    p=s.get('position'); pos=None; total_open_pnl=0.0

    if p:

        cp=f(s.get('market_prices',{}).get(p.get('symbol'),p.get('entry_price')))

        qty=f(p.get('qty_eth',1)); ep=f(p.get('entry_price')); unreal=(cp-ep)*qty if p.get('side')=='LONG' else (ep-cp)*qty

        active=f(p.get('trail_stop')) if p.get('trail_active') and p.get('trail_stop') is not None else f(p.get('sl'))

        pos={**p,'current_price':cp,'unrealized_pnl':unreal,'active_stop':active}

        total_open_pnl+=unreal

    for wsym, wp in (s.get('positions', {}) or {}).items():

        wsig=(s.get('watchlist_signals', {}) or {}).get(wsym, {})

        cp=f(wsig.get('price', wp.get('entry_price')))

        qty=f(wp.get('qty_eth', 0)); ep=f(wp.get('entry_price'))

        unreal=(cp-ep)*qty if wp.get('side')=='LONG' else (ep-cp)*qty

        total_open_pnl+=unreal

    indicators=s.get('indicators',{})

    return {'bot_alive': bool(s.get('last_heartbeat')),'heartbeat':s.get('last_heartbeat'),'equity':equity,'net_pnl':net,'return_pct':net/start*100,'price':f(s.get('market_prices',{}).get('ETHUSDT',0)),'price_time':s.get('market_price_time'),'last_closed_time':s.get('last_closed_time'),'position':pos,'signals':s.get('signals',indicators),'total_open_pnl':total_open_pnl,'stats':{'trades':len(closed),'wins':wins,'losses':losses,'win_rate':wins/len(closed)*100 if closed else 0,'profit_factor':pf,'avg_trade':avg,'max_drawdown':maxdd},'history':list(reversed(closed[-20:]))}

 

def watchlist_status():

    s = read_state()

    watchlist = s.get('watchlist', {}) or {}

    positions = s.get('positions', {}) or {}

    signals = s.get('watchlist_signals', {}) or {}

    items = []

    for symbol, w in watchlist.items():

        sig = signals.get(symbol, {})

        pos = None

        p = positions.get(symbol)

        if p:

            cp = f(sig.get('price', p.get('entry_price')))

            qty = f(p.get('qty_eth', 0)); ep = f(p.get('entry_price'))

            unreal = (cp - ep) * qty if p.get('side') == 'LONG' else (ep - cp) * qty

            active = f(p.get('trail_stop')) if p.get('trail_active') and p.get('trail_stop') is not None else f(p.get('sl'))

            pos = {**p, 'current_price': cp, 'unrealized_pnl': unreal, 'active_stop': active}

        items.append({

            'symbol': symbol, 'market': w.get('market'), 'added_at': w.get('added_at'),

            'added_signal': w.get('added_signal'), 'current_signal': sig.get('final'),

            'current_price': sig.get('price'), 'atrp_percentile_1d': sig.get('atrp_percentile_1d'),

            'updated_at': sig.get('updated_at'), 'position': pos,

            'indicators': sig.get('indicators'),

        })

    items.sort(key=lambda x: x.get('added_at') or '', reverse=True)

    return {'items': items, 'max_symbols': cfg.WATCHLIST_MAX_SYMBOLS, 'position_usd': cfg.WATCHLIST_POSITION_USD}

 

 

class Handler(BaseHTTPRequestHandler):

    # -- session/auth helpers --------------------------------------------

    def _session_token(self):

        raw = self.headers.get('Cookie')

        if not raw:

            return None

        c = SimpleCookie()

        try:

            c.load(raw)

        except Exception:

            return None

        morsel = c.get(SESSION_COOKIE)

        return morsel.value if morsel else None

 

    def _get_cookie(self, name):

        raw = self.headers.get('Cookie')

        if not raw:

            return None

        c = SimpleCookie()

        try:

            c.load(raw)

        except Exception:

            return None

        morsel = c.get(name)

        return morsel.value if morsel else None

 

    def _current_user(self):

        return auth.get_session_user(self._session_token())

 

    def _set_session_cookie(self, token):

        self.send_header('Set-Cookie', f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={auth.SESSION_TTL_SECONDS}')

 

    def _clear_session_cookie(self):

        self.send_header('Set-Cookie', f'{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0')

 

    def _base_url(self):

        # Railway terminates TLS at its edge and forwards plain HTTP to the

        # container, so the scheme has to come from X-Forwarded-Proto, not

        # from how this process itself was reached.

        proto = (self.headers.get('X-Forwarded-Proto', '') or '').split(',')[0].strip()

        host = self.headers.get('Host', 'localhost')

        if not proto:

            proto = 'http' if host.startswith('localhost') or host.startswith('127.0.0.1') else 'https'

        return f'{proto}://{host}'

 

    def _client_ip(self):

        # Railway's edge proxy forwards the real client IP in

        # X-Forwarded-For (first entry = original client); without it every

        # request would appear to come from Railway's own internal address,

        # making per-IP rate limiting useless.

        fwd = (self.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip()

        return fwd or (self.client_address[0] if self.client_address else 'unknown')

 

    def _rate_limited(self, bucket, limit, window_seconds):

        """True if this IP has exceeded `limit` requests to `bucket` in the

        trailing `window_seconds` and the caller should send 429 and stop."""

        return not _rate_limit_check(bucket, self._client_ip(), limit, window_seconds)

 

    def _error_body(self, e):

        # A raw exception message/type can leak internal details (file

        # paths, library names, query fragments) to whoever triggered the

        # 500 — including an attacker probing for weaknesses. The full

        # traceback always goes to the server log either way; only an

        # already-authenticated admin also gets it in the HTTP response,

        # for convenient live debugging.

        is_admin = False

        try:

            is_admin = auth.is_admin(self._current_user())

        except Exception:

            pass

        body = {'error': 'internal server error'}

        if is_admin:

            body['detail'] = f'{type(e).__name__}: {e}'

        return body

 

    def _send_json(self, obj, status=200, extra_headers=None):

        body = json.dumps(obj, ensure_ascii=False).encode()

        self.send_response(status)

        self.send_header('Content-Type', 'application/json; charset=utf-8')

        self.send_header('Cache-Control', 'no-store')

        self.send_header('Content-Length', str(len(body)))

        if extra_headers:

            for k, v in extra_headers:

                self.send_header(k, v)

        self.end_headers()

        self.wfile.write(body)

 

    def _send_html(self, html, status=200, extra_headers=None):

        body = html.encode()

        self.send_response(status)

        self.send_header('Content-Type', 'text/html; charset=utf-8')

        self.send_header('Cache-Control', 'no-store')

        self.send_header('Content-Length', str(len(body)))

        if extra_headers:

            for k, v in extra_headers:

                self.send_header(k, v)

        self.end_headers()

        self.wfile.write(body)

 

    def _redirect(self, location):

        self.send_response(302)

        self.send_header('Location', location)

        self.send_header('Cache-Control', 'no-store')

        self.send_header('Content-Length', '0')

        self.end_headers()

 

    def _read_json_body(self):

        try:

            length = int(self.headers.get('Content-Length', 0))

        except (TypeError, ValueError):

            length = 0

        if length <= 0:

            return {}

        raw = self.rfile.read(length)

        try:

            return json.loads(raw.decode('utf-8'))

        except Exception:

            return {}

 

    def do_GET(self):

        # Top-level safety net: an unhandled exception anywhere below used to

        # crash the connection with NO response at all (the client/Railway's

        # edge just sees a dropped connection -> "Application failed to

        # respond", with nothing useful in the browser to diagnose from).

        # Catching it here means a bug in one route can never again look

        # like a full outage, and the real error is visible immediately.

        try:

            self._do_GET_inner()

        except Exception as e:

            import traceback

            traceback.print_exc()

            try:

                self._send_json(self._error_body(e), status=500)

            except Exception:

                pass

 

    def _do_GET_inner(self):

        path=urlparse(self.path).path

 

        # Public marketing homepage: a logged-out visitor hitting "/" sees

        # the Herobot-ai landing page instead of being bounced to /login.

        # A logged-in user still falls through to the normal dashboard

        # further down — this only intercepts the logged-out case.

        if path=='/' and not self._current_user():

            self._send_html(LANDING_HTML); return

 

        if path=='/login':

            if self._current_user():

                self._redirect('/'); return

            block = AUTH0_LOGIN_BLOCK if auth.auth0_enabled() else ''

            html = LOGIN_HTML.replace('__AUTH0_LOGIN_BLOCK__', block)

            self._send_html(html); return

        if path=='/register':

            if self._current_user():

                self._redirect('/'); return

            block = AUTH0_LOGIN_BLOCK if auth.auth0_enabled() else ''

            html = REGISTER_HTML.replace('__AUTH0_LOGIN_BLOCK__', block).replace('__TRIAL_DAYS__', str(auth.TRIAL_DAYS))

            self._send_html(html); return

 

        if path=='/forgot-password':

            if self._current_user():

                self._redirect('/'); return

            self._send_html(FORGOT_PASSWORD_HTML); return

        if path=='/reset-password':

            if self._current_user():

                self._redirect('/'); return

            self._send_html(RESET_PASSWORD_HTML); return

 

        if path=='/auth0/login':

            if not auth.auth0_enabled():

                self._redirect('/login'); return

            state = secrets.token_urlsafe(24)

            url = auth.build_auth0_authorize_url(state)

            self.send_response(302)

            self.send_header('Location', url)

            self.send_header('Set-Cookie', f'auth0_state={state}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600')

            self.send_header('Content-Length', '0')

            self.end_headers()

            return

 

        if path=='/callback':

            q = parse_qs(urlparse(self.path).query)

            code = (q.get('code', [''])[0] or '')

            state = (q.get('state', [''])[0] or '')

            cookie_state = self._get_cookie('auth0_state')

            if not code or not state or not cookie_state or state != cookie_state:

                self._redirect('/login'); return

            ok, username, err = auth.login_or_register_auth0(code)

            if not ok:

                self._redirect('/login'); return

            token = auth.create_session(username)

            self.send_response(302)

            self.send_header('Location', '/')

            self.send_header('Set-Cookie', f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={auth.SESSION_TTL_SECONDS}')

            self.send_header('Set-Cookie', 'auth0_state=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0')

            self.send_header('Content-Length', '0')

            self.end_headers()

            return

 

        # Everything else is members-only: the main dashboard page redirects

        # to /login, and every /api/* route (except /health) returns 401.

        if path not in PUBLIC_PATHS:

            user = self._current_user()

            if not user:

                if path.startswith('/api/'):

                    self._send_json({'error': 'login required'}, status=401); return

                self._redirect('/login'); return

 

            # Trial/subscription gate — an admin always passes (see

            # auth.subscription_status), everyone else needs an active trial

            # or a payment the admin has manually marked as received.

            if path not in ACCOUNT_ALWAYS_ALLOWED and not auth.has_active_access(user):

                if path.startswith('/api/'):

                    self._send_json({'error': 'subscription required', 'subscription_status': 'expired'}, status=402); return

                html = TRIAL_EXPIRED_HTML.replace('__USERNAME__', user)

                self._send_html(html); return

 

        if path=='/admin':

            user = self._current_user()

            if not auth.is_admin(user):

                self._redirect('/'); return

            self._send_html(ADMIN_HTML); return

 

        if path=='/api/admin/users':

            user = self._current_user()

            if not auth.is_admin(user):

                self._send_json({'error': 'forbidden'}, status=403); return

            self._send_json({'users': auth.list_users_admin()}); return

 

        if path=='/api/account':

            # NOTE: deliberately not named `status` — this method also has an

            # `/api/status` branch that calls the module-level status()

            # function; Python's function-wide local scoping means a local

            # variable named `status` anywhere in this method shadows that

            # function for the ENTIRE method, which previously broke

            # /api/status on every single call with UnboundLocalError.

            user = self._current_user()

            acct = auth.get_account_status(user)

            acct['live_runtime'] = live_trading.get_runtime_status(user)

            acct['telegram_bot_username'] = tg_notifier.ensure_bot_username()

            acct['telegram_bot_enabled'] = tg_notifier.TELEGRAM_BOT_ENABLED

            self._send_json(acct); return

 

        if path=='/api/admin/kill-switch':

            user = self._current_user()

            if not auth.is_admin(user):

                self._send_json({'error': 'forbidden'}, status=403); return

            self._send_json(auth.get_global_kill_switch()); return

 

        if path=='/api/admin/bist-debug':

            # TEMPORARY diagnostic route — admin-only, read-only. Round 2:

            # tries several candidate TradingView filter payloads and several

            # candidate CNBC-E regex patterns directly against the live

            # responses, and reports match counts for each, so we can find

            # working ones without needing shell/network access outside of

            # Railway. Safe to remove once the scraper is fixed for good.

            user = self._current_user()

            if not auth.is_admin(user):

                self._send_json({'error': 'forbidden'}, status=403); return

            import re as _re

            import bist_xutum_universe as bxu

            out = {}

 

            # --- TradingView: try several payload variants -----------------

            tv_variants = {

                'original': bxu._tv_payload(),

                'no_is_primary': {**bxu._tv_payload(), 'filter': [f for f in bxu._tv_payload()['filter'] if f.get('left') != 'is_primary']},

                'no_typespecs': {**bxu._tv_payload(), 'filter': [f for f in bxu._tv_payload()['filter'] if f.get('left') != 'typespecs']},

                'exchange_only': {

                    'columns': ['name', 'description'],

                    'filter': [{'left': 'exchange', 'operation': 'equal', 'right': 'BIST'}],

                    'filterOR': [], 'ignore_unknown_fields': False,

                    'options': {'lang': 'tr'}, 'price_conversion': {},

                    'range': [0, 50], 'sort': {'sortBy': 'name', 'sortOrder': 'asc'},

                    'symbols': {'query': {'types': []}, 'tickers': []}, 'markets': ['turkey'],

                },

                'no_filters_at_all': {

                    'columns': ['name', 'description'], 'filter': [], 'filterOR': [],

                    'ignore_unknown_fields': False, 'options': {'lang': 'tr'}, 'price_conversion': {},

                    'range': [0, 50], 'sort': {'sortBy': 'name', 'sortOrder': 'asc'},

                    'symbols': {'query': {'types': []}, 'tickers': []}, 'markets': ['turkey'],

                },

            }

            out['tradingview_variants'] = {}

            for label, payload in tv_variants.items():

                try:

                    r = requests.post(bxu.TV_SCANNER_URL, json=payload, headers=bxu._headers(), timeout=20)

                    body = {}

                    try:

                        body = r.json()

                    except Exception:

                        pass

                    out['tradingview_variants'][label] = {

                        'status': r.status_code,

                        'totalCount': body.get('totalCount'),

                        'data_len': len(body.get('data') or []),

                        'sample': (body.get('data') or [])[:3],

                        'body_snippet': None if body else r.text[:300],

                    }

                except Exception as e:

                    out['tradingview_variants'][label] = {'error': str(e)}

 

            # --- CNBC-E: try several regex patterns + a real content excerpt

            try:

                r2 = requests.get(bxu.CNBC_XUTUM_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)

                html = r2.text

                out['cnbc_raw_status'] = r2.status_code

                out['cnbc_html_length'] = len(html)

                regex_variants = {

                    'original_lower_dash': r'/borsa/hisseler/([a-z0-9]+)-',

                    'any_case_dash': r'/borsa/hisseler/([a-zA-Z0-9]+)-',

                    'no_trailing_dash': r'/borsa/hisseler/([a-zA-Z0-9]+)',

                    'data_symbol_attr': r'data-symbol=["\']([A-Z0-9]+)["\']',

                    'hisse_senedi_path': r'/borsa/hisse-senedi/([a-zA-Z0-9]+)',

                    'symbol_in_table_cell': r'"symbol"\s*:\s*"([A-Z0-9]+)"',

                }

                out['cnbc_regex_matches'] = {}

                for label, pattern in regex_variants.items():

                    found = _re.findall(pattern, html)

                    uniq = sorted(set(f.upper() for f in found))

                    out['cnbc_regex_matches'][label] = {'count': len(uniq), 'sample': uniq[:15]}

                # Grab a real excerpt around the first stock-table heading so we

                # can see the actual current markup by eye.

                idx = html.find('HİSSELERİ')

                if idx == -1:

                    idx = html.upper().find('BIST TUM')

                if idx != -1:

                    out['cnbc_table_excerpt'] = html[max(0, idx-200): idx+3000]

                else:

                    out['cnbc_table_excerpt'] = None

            except Exception as e:

                out['cnbc_error'] = str(e)

 

            self._send_json(out); return

 

        if path=='/api/status':

            body=json.dumps(status(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/health':

            body=b'OK'; self.send_response(200); self.send_header('Content-Type','text/plain; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/bist-scanner':

            body=json.dumps(bist_scanner_snapshot(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/bist-scanner/scan':

            q=parse_qs(urlparse(self.path).query); force=q.get('force',['0'])[0]=='1'

            started=bist_scanner_start(force=force)

            body=json.dumps({'started':started,'status':bist_scanner_snapshot().get('status')}).encode(); self.send_response(202 if started else 200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/us-scanner':

            body=json.dumps(us_scanner_snapshot(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/us-scanner/scan':

            q=parse_qs(urlparse(self.path).query); force=q.get('force',['0'])[0]=='1'

            started=us_scanner_start(force=force)

            body=json.dumps({'started':started,'status':us_scanner_snapshot().get('status')}).encode(); self.send_response(202 if started else 200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/scanner':

            body=json.dumps(scanner_snapshot(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/scanner/scan':

            q=parse_qs(urlparse(self.path).query); force=q.get('force',['0'])[0]=='1'

            started=scanner_start(force=force)

            body=json.dumps({'started':started,'status':scanner_snapshot().get('status')}).encode(); self.send_response(202 if started else 200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/watchlist':

            body=json.dumps(watchlist_status(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/watchlist/add':

            q=parse_qs(urlparse(self.path).query)

            symbol=(q.get('symbol',[''])[0] or '').strip().upper()

            market=(q.get('market',[''])[0] or '').strip().lower()

            signal=(q.get('signal',[''])[0] or None)

            ok, error = False, None

            if market not in ('crypto','bist','us_stock'):

                error='geçersiz piyasa'

            elif not symbol:

                error='sembol gerekli'

            else:

                cur=read_state().get('watchlist', {}) or {}

                if symbol not in cur and len(cur) >= cfg.WATCHLIST_MAX_SYMBOLS:

                    error=f'takip listesi dolu (maks {cfg.WATCHLIST_MAX_SYMBOLS})'

                else:

                    _add_to_watchlist(symbol, market, signal); ok=True

            body=json.dumps({'ok':ok,'error':error},ensure_ascii=False).encode(); self.send_response(200 if ok else 400); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/watchlist/remove':

            q=parse_qs(urlparse(self.path).query)

            symbol=(q.get('symbol',[''])[0] or '').strip().upper()

            if symbol: _remove_from_watchlist(symbol)

            body=json.dumps({'ok':bool(symbol)}).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/ai-analysis':

            body=json.dumps(ai_analyst.get_status(),ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/ai-analysis/run':

            result=ai_analyst.run_now()

            body=json.dumps(result,ensure_ascii=False).encode(); self.send_response(200 if result.get('ok') else 400); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        if path=='/api/live/my-positions':

            # Scoped to the logged-in user's OWN live Binance positions/trades

            # only — never the shared paper/demo panel and never another

            # user's data (live_trading.get_my_live_summary() reads only the

            # runtime record and trade-log rows keyed to this username).

            user=self._current_user()

            summary=live_trading.get_my_live_summary(user)

            summary['live_trading_enabled']=bool((auth.get_user(user) or {}).get('live_trading_enabled'))

            body=json.dumps(summary,ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return

        html=HTML.replace('__USERNAME__', self._current_user() or '')

        body=html.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

 

    # -- auth / account POST routes ---------------------------------------

    def do_POST(self):

        # Same safety net as do_GET — see the comment there.

        try:

            self._do_POST_inner()

        except Exception as e:

            import traceback

            traceback.print_exc()

            try:

                self._send_json(self._error_body(e), status=500)

            except Exception:

                pass

 

    def _do_POST_inner(self):

        path=urlparse(self.path).path

 

        if path=='/login':

            if self._rate_limited('login', limit=10, window_seconds=300):

                self._send_json({'ok': False, 'error': 'Çok fazla giriş denemesi yapıldı. Lütfen birkaç dakika sonra tekrar deneyin.'}, status=429); return

            data=self._read_json_body()

            ok, err = auth.authenticate(data.get('username',''), data.get('password',''))

            if not ok:

                self._send_json({'ok': False, 'error': err}, status=401); return

            token = auth.create_session(data.get('username',''))

            body=json.dumps({'ok': True}).encode()

            self.send_response(200)

            self.send_header('Content-Type','application/json; charset=utf-8')

            self.send_header('Content-Length', str(len(body)))

            self._set_session_cookie(token)

            self.end_headers()

            self.wfile.write(body)

            return

 

        if path=='/register':

            if self._rate_limited('register', limit=5, window_seconds=3600):

                self._send_json({'ok': False, 'error': 'Çok fazla kayıt denemesi yapıldı. Lütfen daha sonra tekrar deneyin.'}, status=429); return

            data=self._read_json_body()

            ok, err = auth.register_user(data.get('username',''), data.get('password',''), data.get('email',''))

            if not ok:

                self._send_json({'ok': False, 'error': err}, status=400); return

            token = auth.create_session(data.get('username',''))

            body=json.dumps({'ok': True}).encode()

            self.send_response(200)

            self.send_header('Content-Type','application/json; charset=utf-8')

            self.send_header('Content-Length', str(len(body)))

            self._set_session_cookie(token)

            self.end_headers()

            self.wfile.write(body)

            return

 

        if path=='/logout':

            auth.delete_session(self._session_token())

            body=json.dumps({'ok': True}).encode()

            self.send_response(200)

            self.send_header('Content-Type','application/json; charset=utf-8')

            self.send_header('Content-Length', str(len(body)))

            self._clear_session_cookie()

            self.end_headers()

            self.wfile.write(body)

            return

 

        if path=='/api/auth/forgot-password':

            if self._rate_limited('forgot-password', limit=5, window_seconds=3600):

                # Same generic ok:true even when rate-limited — a 429 here

                # would itself leak "this IP has already tried a valid

                # email", so we quietly no-op instead of returning an error.

                self._send_json({'ok': True}); return

            # Always answers {"ok": true} whether or not the email matches an

            # account — telling a caller "no account with that email" would

            # let anyone enumerate registered addresses. If it does match a

            # local-password account, a single-use, 30-minute reset token is

            # emailed to it; everything else (unknown email, Auth0-only

            # account, email not configured) fails silently from the caller's

            # point of view and is only visible in the server logs.

            data=self._read_json_body()

            email=(data.get('email') or '').strip()

            username = auth.find_username_by_email(email)

            if username:

                token = auth.create_password_reset_token(username)

                if token:

                    reset_url = f'{self._base_url()}/reset-password?token={token}'

                    ok, err = email_notifier.send_password_reset_email(email, reset_url)

                    if not ok:

                        print(f'[email] password reset e-postası gönderilemedi ({email}): {err}')

            self._send_json({'ok': True}); return

 

        if path=='/api/auth/reset-password':

            if self._rate_limited('reset-password', limit=10, window_seconds=3600):

                self._send_json({'ok': False, 'error': 'Çok fazla deneme yapıldı. Lütfen daha sonra tekrar deneyin.'}, status=429); return

            data=self._read_json_body()

            ok, err = auth.reset_password_with_token(data.get('token',''), data.get('password',''))

            if not ok:

                self._send_json({'ok': False, 'error': err}, status=400); return

            self._send_json({'ok': True}); return

 

        # Everything below requires a logged-in user.

        user = self._current_user()

        if not user:

            self._send_json({'error': 'login required'}, status=401); return

 

        if path=='/api/account/connect-binance':

            # A trial-expired, non-paying user can still be logged in (to see

            # billing status) but must not be able to (re)connect a live key.

            if not auth.has_active_access(user):

                self._send_json({'ok': False, 'error': 'Deneme süreniz doldu. Devam etmek için aboneliğinizi aktive etmemiz gerekiyor.'}, status=402); return

            data=self._read_json_body()

            api_key=(data.get('api_key') or '').strip()

            api_secret=(data.get('api_secret') or '').strip()

            risk_ack=bool(data.get('risk_ack'))

            ok, err = auth.save_binance_credentials(user, api_key, api_secret, risk_ack=risk_ack)

            if ok:

                ok2, err2 = auth.verify_binance_key(user)

                if not ok2:

                    self._send_json({'ok': False, 'error': err2}); return

            self._send_json({'ok': ok, 'error': err}); return

 

        if path=='/api/account/disconnect-binance':

            auth.clear_binance_credentials(user)

            self._send_json({'ok': True}); return

 

        if path=='/api/account/risk-ack':

            auth.ack_risk(user)

            self._send_json({'ok': True}); return

 

        if path=='/api/account/live-settings':

            if not auth.has_active_access(user):

                self._send_json({'ok': False, 'error': 'Deneme süreniz doldu. Devam etmek için aboneliğinizi aktive etmemiz gerekiyor.'}, status=402); return

            data=self._read_json_body()

            ok, err = auth.set_live_settings(

                user, data.get('position_usd'), data.get('max_leverage'),

                data.get('daily_loss_limit_usd'), data.get('max_open_positions'),

            )

            self._send_json({'ok': ok, 'error': err}); return

 

        if path=='/api/account/live-toggle':

            if not auth.has_active_access(user):

                self._send_json({'ok': False, 'error': 'Deneme süreniz doldu. Devam etmek için aboneliğinizi aktive etmemiz gerekiyor.'}, status=402); return

            data=self._read_json_body()

            ok, err = auth.set_live_trading_enabled(user, bool(data.get('enabled')))

            self._send_json({'ok': ok, 'error': err}); return

 

        if path=='/api/live/close-position':

            # Deliberately NOT gated by has_active_access/trial status — a

            # user closing their own already-open real position should never

            # be blocked by a subscription check, same reasoning as

            # live_trading.on_exit_signal not being gated by it either.

            data=self._read_json_body()

            symbol=(data.get('symbol') or '').strip().upper()

            if not symbol:

                self._send_json({'ok': False, 'error': 'Sembol gerekli'}, status=400); return

            ok, err = live_trading.close_position_now(user, symbol)

            self._send_json({'ok': ok, 'error': err}, status=200 if ok else 400); return

 

        if path=='/api/admin/set-status':

            if not auth.is_admin(user):

                self._send_json({'error': 'forbidden'}, status=403); return

            data=self._read_json_body()

            ok, err = auth.set_payment_status(data.get('username', ''), data.get('status', ''))

            self._send_json({'ok': ok, 'error': err}); return

 

        if path=='/api/admin/kill-switch/toggle':

            if not auth.is_admin(user):

                self._send_json({'error': 'forbidden'}, status=403); return

            data=self._read_json_body()

            active=bool(data.get('active'))

            auth.set_global_kill_switch(active, user)

            try:

                live_trading.notify_kill_switch_change(active, user)

            except Exception as e:

                print(f'LIVE | TELEGRAM KILL SWITCH NOTIFY ERROR | {type(e).__name__}: {e}', flush=True)

            self._send_json({'ok': True, 'state': auth.get_global_kill_switch()}); return

 

        if path=='/api/account/telegram/link-code':

            code, err = auth.create_telegram_link_code(user)

            self._send_json({

                'ok': bool(code), 'error': err, 'code': code,

                'bot_username': tg_notifier.ensure_bot_username(),

                'expires_in_seconds': auth.LINK_CODE_TTL_SECONDS,

            }); return

 

        if path=='/api/account/telegram/unlink':

            auth.unlink_telegram(user)

            self._send_json({'ok': True}); return

 

        self._send_json({'error': 'not found'}, status=404)

 

    def log_message(self,*args):return

 

def start_dashboard():

    server=ThreadingHTTPServer(('0.0.0.0',PORT),Handler)

    print(f'DASHBOARD | http://0.0.0.0:{PORT} | PAPER ONLY + COIN SCANNER',flush=True)

    threading.Thread(target=background_loop, daemon=True).start()

    threading.Thread(target=bist_background_loop, daemon=True).start()

    threading.Thread(target=us_background_loop, daemon=True).start()

    threading.Thread(target=telegram_link.poll_loop, daemon=True).start()

    server.serve_forever()
