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

# "Abonelik Sistemine Geç" bank-transfer details, shown to users choosing a
# plan. *** PLACEHOLDER *** — set SUBSCRIPTION_IBAN / SUBSCRIPTION_IBAN_HOLDER
# to the real bank account before going live; a wrong/fake IBAN here would
# send customers' money nowhere, so this is deliberately left as an obvious
# placeholder rather than a guessed value.
SUBSCRIPTION_IBAN = os.environ.get('SUBSCRIPTION_IBAN', 'TR00 0000 0000 0000 0000 0000 00 — GERÇEK IBAN BURAYA GİRİLMELİ')
SUBSCRIPTION_IBAN_HOLDER = os.environ.get('SUBSCRIPTION_IBAN_HOLDER', 'Hesap sahibi adı buraya girilmeli')

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
_rate_limit_buckets = defaultdict(deque)  # (bucket, ip) -> deque[timestamp, ...]


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
    '/api/admin/kill-switch', '/api/admin/kill-switch/toggle', '/api/admin/delete-user',
    '/api/account/telegram/link-code', '/api/account/telegram/unlink',
    '/api/live/my-positions',
    # These two are the whole point of being reachable after a trial expires:
    # an expired user still needs to be able to ask a question or tell the
    # system they've paid.
    '/api/account/ask-admin', '/api/account/subscription-request',
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
  .nav-toggle{display:none;background:none;border:1px solid #2A3238;border-radius:8px;padding:9px 10px;cursor:pointer;flex-direction:column;justify-content:center;gap:4px}
  .nav-toggle span{display:block;width:19px;height:2px;background:#E9EDF0;border-radius:2px}
  @media (max-width:900px){
    .grid3,.grid2,.grid4,.grid6{grid-template-columns:1fr}
    .wrap{padding-left:22px;padding-right:22px}
    nav .nav-links{display:none}
    nav .nav-links.open{display:flex;flex-direction:column;align-items:stretch;gap:2px;position:absolute;top:100%;left:0;right:0;background:#0A0D10;border-bottom:1px solid #1B2126;padding:6px 22px 16px}
    nav .nav-links.open a{padding:12px 0;border-bottom:1px solid #161B20}
    .nav-toggle{display:flex}
    .hero-h1{font-size:38px!important}
    .split{flex-direction:column}
    .stats{gap:32px}
    .hide-sm{display:none}
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
    <div class="nav-links" id="navLinks">
      <a href="#strateji">Strateji</a>
      <a href="#piyasalar">Piyasalar</a>
      <a href="#guvenlik">Güvenlik</a>
      <a href="/tanitim">Tanıtım</a>
      <a href="/login">Panel</a>
    </div>
    <div style="display:flex;align-items:center;gap:14px">
      <span style="font-size:13px;font-weight:600;color:#7A8590" class="hide-sm">Paper mod · risksiz</span>
      <a href="/register" class="btn btn-primary" style="padding:11px 22px;font-size:14px">Ücretsiz Dene</a>
      <button class="nav-toggle" onclick="document.getElementById('navLinks').classList.toggle('open')" aria-label="Menü">
        <span></span><span></span><span></span>
      </button>
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
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <a href="#strateji" style="font-size:13px;color:#7A8590;font-weight:600">Strateji</a>
        <a href="#piyasalar" style="font-size:13px;color:#7A8590;font-weight:600">Piyasalar</a>
        <a href="#guvenlik" style="font-size:13px;color:#7A8590;font-weight:600">Güvenlik</a>
        <a href="/tanitim" style="font-size:13px;color:#7A8590;font-weight:600">Tanıtım</a>
        <a href="/login" style="font-size:13px;color:#7A8590;font-weight:600">Panel</a>
      </div>
    </div>
  </div>
</div>

</body>
</html>
'''

TANITIM_HTML = r'''<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tanıtım — Herobot-ai</title>
<meta name="description" content="HeroBot AI: kripto, BIST ve ABD hisselerinde duygusuz, disiplinli ve 7/24 çalışan bir strateji motoru.">
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
  .card{background:#11161A;border:1px solid #1E252B;border-radius:14px}
  .eyebrow{font-size:13px;font-weight:700;color:#3DD9A8;letter-spacing:1px;text-transform:uppercase}
  nav .nav-links{display:flex;align-items:center;gap:40px}
  nav .nav-links a{font-size:14px;font-weight:600;color:#B7C0C6}
  nav .nav-links a.active{color:#3DD9A8}
  .nav-toggle{display:none;background:none;border:1px solid #2A3238;border-radius:8px;padding:9px 10px;cursor:pointer;flex-direction:column;justify-content:center;gap:4px}
  .nav-toggle span{display:block;width:19px;height:2px;background:#E9EDF0;border-radius:2px}
  @media (max-width:900px){
    .wrap{padding-left:22px;padding-right:22px}
    nav .nav-links{display:none}
    nav .nav-links.open{display:flex;flex-direction:column;align-items:stretch;gap:2px;position:absolute;top:100%;left:0;right:0;background:#0A0D10;border-bottom:1px solid #1B2126;padding:6px 22px 16px}
    nav .nav-links.open a{padding:12px 0;border-bottom:1px solid #161B20}
    .nav-toggle{display:flex}
  }
</style>
</head>
<body>

<!-- ===== NAV ===== -->
<nav style="position:sticky;top:0;z-index:20;background:rgba(10,13,16,0.9);backdrop-filter:blur(10px);border-bottom:1px solid #1B2126">
  <div class="wrap" style="display:flex;align-items:center;justify-content:space-between;padding:20px 0">
    <a href="/" style="display:flex;align-items:center;gap:10px">
      <div style="width:34px;height:34px;border-radius:9px;background:linear-gradient(160deg,#3DD9A8,#1D8F6E);display:flex;align-items:center;justify-content:center">
        <span class="disp" style="font-size:16px;font-weight:700;color:#08110D">H</span>
      </div>
      <span class="disp" style="font-size:19px;font-weight:700;color:#E9EDF0">Herobot-ai</span>
    </a>
    <div class="nav-links" id="navLinks">
      <a href="/#strateji">Strateji</a>
      <a href="/#piyasalar">Piyasalar</a>
      <a href="/#guvenlik">Güvenlik</a>
      <a href="/tanitim" class="active">Tanıtım</a>
      <a href="/login">Panel</a>
    </div>
    <div style="display:flex;align-items:center;gap:14px">
      <a href="/register" class="btn btn-primary" style="padding:11px 22px;font-size:14px">Ücretsiz Dene</a>
      <button class="nav-toggle" onclick="document.getElementById('navLinks').classList.toggle('open')" aria-label="Menü">
        <span></span><span></span><span></span>
      </button>
    </div>
  </div>
</nav>

<!-- ===== MAKALE ===== -->
<div class="wrap" style="padding:64px 0 88px 0">
  <div style="max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:26px">
    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;gap:14px">
      <span class="eyebrow">Tanıtım</span>
      <h1 class="disp" style="margin:0;font-size:36px;font-weight:700;line-height:1.25">Finansal Evrenin Yeni Hakimi: HeroBot AI ile Duygusuz ve Disiplinli Yatırımın Sırları</h1>
    </div>
    <img src="/static/tanitim-infografik.png" alt="HeroBot AI özellik infografiği" style="width:100%;border-radius:14px;border:1px solid #1E252B;display:block">
    <div style="display:flex;flex-direction:column;gap:26px;font-size:15px;line-height:1.85;color:#B7C0C6">
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">1. Giriş: Yatırımın Duygusal Yükünden Kurtulmak</h3>
        <p style="margin:0">Finansal piyasalarda işlem yapmak, çoğu zaman bir veri savaşından ziyade bir irade savaşına dönüşür. Yatırımcılar; bilgi kirliliği, FOMO (fırsatı kaçırma korkusu) ve ani fiyat hareketlerinin tetiklediği panik ataklar arasında sıkışıp kalır. Açık konuşalım; bir ekranın başında mumların hareketine bakarak ter dökmek, strateji değil, modern bir işkence yöntemidir. İşte tam bu noktada, "Gölgelerin gücü adına değil, verinin gücü adına" diyerek sahneye HeroBot AI (He-Robot) çıkıyor. İnsan psikolojisinin o meşhur zayıflıklarını — açgözlülük, korku ve kararsızlık — devre dışı bırakan bu teknoloji, 7/24 çalışan disiplinli bir strateji motoru olarak karşımıza çıkıyor. HeroBot AI, piyasanın gürültüsünü susturup sadece rakamların fısıldadığı gerçeklere odaklanarak yatırım dünyasında kuralları yeniden yazıyor.</p>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">2. Sınırsız Kripto Likiditesi ve Küresel Hisse Piyasaları</h3>
        <p style="margin:0 0 10px 0">Geleneksel yatırımcılar bir piyasadan diğerine geçerken platformlar arasında boğulurken, HeroBot AI tek bir panelden küresel bir hakimiyet alanı kuruyor. Bu sadece bir erişim kolaylığı değil, stratejik bir üstünlüktür:</p>
        <ul style="margin:0 0 10px 0;padding-left:20px;display:flex;flex-direction:column;gap:6px">
          <li><b style="color:#E9EDF0">Kripto Paralar:</b> Binance Futures USDT-M altındaki tüm perpetual sözleşmelerin otomatik taranması.</li>
          <li><b style="color:#E9EDF0">BIST (Borsa İstanbul):</b> BIST 100 ve Tüm-100 evreninin tamamı (TradingView native 4H verisiyle).</li>
          <li><b style="color:#E9EDF0">ABD Hisseleri:</b> NASDAQ, NYSE ve AMEX borsalarında yer alan devasa hisse havuzu.</li>
        </ul>
        <p style="margin:0">Sistem, piyasa kapanışlarını beklemenizi gerektirmez; siz de beklemezsiniz. Yaklaşık 15 dakikada bir yapılan otomatik taramaların yanı sıra, kullanıcının tek bir tuşla tüm piyasayı o an süzgeçten geçirebileceği "Tümünü Tara" (Scan All) özelliği mevcuttur. Hızın para ettiği bir dünyada, bu anlık tarama kapasitesi sizi kalabalığın fersah fersah önüne geçirir.</p>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">3. Stratejinin Kalbi: Veri Odaklı ve Çok Katmanlı Onay Mekanizması</h3>
        <p style="margin:0 0 10px 0">HeroBot AI'nın zekası, rastgele tahminlerden değil, çok katmanlı bir teknik analiz süzgecinden gelir. Sistem, bir işlemin onaylanması için adeta bir "senato onayı" bekler:</p>
        <ul style="margin:0 0 10px 0;padding-left:20px;display:flex;flex-direction:column;gap:6px">
          <li><b style="color:#E9EDF0">Yön ve Trend Teyidi:</b> EMA (Hareketli Ortalamalar) ve Supertrend filtreleri ile ana yön belirlenir.</li>
          <li><b style="color:#E9EDF0">Piyasa Karakteri:</b> ADX indikatörü ile piyasanın gerçek bir trendde mi yoksa sadece yatayda "patinaj mı" yaptığı ayırt edilir.</li>
          <li><b style="color:#E9EDF0">Momentum ve Onay:</b> RSI, CCI, Stoch RSI ve MACD gibi indikatörler birer onay katmanı oluşturur.</li>
        </ul>
        <p style="margin:0">Sistemin en katı kurallarından biri, yalnızca kapanmış mumlardan sinyal üretilmesidir. Anlık fiyat iğnelerine ve piyasa gürültüsüne asla güvenmeyen bu yaklaşım, yatırımcıyı "fake" sinyallerden ve ani piyasa manipülasyonlarından korur.</p>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">4. Akıllı Risk Yönetimi: ATRP ile Piyasanın Nabzını Tutmak</h3>
        <p style="margin:0">Çoğu algoritma, piyasanın ruhunu anlamadan sabit yüzdeli stop-loss seviyeleri kullanır. HeroBot AI ise ATR (Average True Range) bazlı dinamik bir risk yönetimi uygular. Ancak burada asıl otoriter dokunuş, ATRP (365 günlük volatilite veto filtresi) ile gelir. Eğer piyasa oynaklığı (ATRP), son 365 günlük yüzdelik dilimde aşırı uçlardaysa, sistem yeni pozisyon açmayı reddeder. Çünkü bazen en kârlı işlem, hiç açılmamış olandır. "Sistem 'sakin kalması gerektiğini bilir'." Piyasa rasyonel olmayan bir çılgınlığa kapıldığında, HeroBot AI soğukkanlılığını koruyarak sermayenizi emniyete alır.</p>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">5. Yapay Zeka Destekli Performans Analisti: Claude ile Tanışın</h3>
        <p style="margin:0">Teknoloji dünyasının en zeki beyinlerinden biri olan Claude, HeroBot AI ekosisteminde sizin kişisel performans analistiniz olarak görev yapar. Kapanan her işlem grubu periyodik olarak Claude'un masasına düşer. Bu AI destekli analist; kazanma oranı ve kâr faktörü (profit factor) gibi verileri işleyerek size objektif bir "performans aynası" tutar. Claude sadece bir rapor sunmaz; yatırımcıyı kendi stratejik hatalarıyla yüzleştiren, duygusal önyargıları (bias) kıran bir disiplin aracıdır. Bir algoritmanın kendi geçmişini AI ile analiz etmesi, insan faktörünün girmesi muhtemel olan "şanslıydım" veya "piyasa kötüydü" gibi bahaneleri ortadan kaldıran vizyoner bir adımdır.</p>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">6. Kurumsal Seviye Güvenlik ve Şeffaflık</h3>
        <p style="margin:0 0 10px 0">HeroBot AI, "önce güvenlik" diyen kurumsal bir disipline sahiptir:</p>
        <ul style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:6px">
          <li><b style="color:#E9EDF0">Paper (Demo) Modu:</b> Sistem şu anda demo modunda çalışmaktadır. Gerçek emir göndermez ve API anahtarı gerektirmez; böylece stratejiyi sıfır riskle, laboratuvar ortamında izleyebilirsiniz.</li>
          <li><b style="color:#E9EDF0">Acil Durdurma Protokolü (Admin Stop Switch):</b> Kurumsal ciddiyetin bir kanıtı olarak, admin gerektiğinde tek bir tıkla tüm kullanıcılar için yeni işlemleri durdurabilir; bu, sistemin kontrol dışı kalmasını engelleyen en büyük sigortadır.</li>
          <li><b style="color:#E9EDF0">Maksimum Zarar Limiti:</b> Günlük zarar limiti dolduğunda sistem yeni pozisyonları otomatik olarak durdurur.</li>
          <li><b style="color:#E9EDF0">Uptime ve Hız:</b> %99.9 uptime garantisi ve 200ms ortalama yanıt süresi ile kesintisiz veri işleme.</li>
          <li><b style="color:#E9EDF0">Sessiz Disiplin:</b> Telegram entegrasyonu ile sadece kritik anlarda bildirim alırsınız; spam yok, sadece stratejik bilgi var.</li>
        </ul>
      </div>
      <div>
        <h3 class="disp" style="margin:0 0 10px 0;font-size:19px;font-weight:700;color:#E9EDF0">7. Sonuç: Geleceğin Teknolojisi Bugün Burada mı?</h3>
        <p style="margin:0">HeroBot AI, 50.000'den fazla aktif kullanıcının parçası olduğu bir vizyonla, yatırım süreçlerini "akıllı otomasyon" ve "AI destekli analiz" ile yeniden tanımlıyor. Bu sistem, sadece bir yazılım değil; finansal teknoloji dünyasındaki sınırsız olasılıkların bir temsilcisidir. Her ne kadar bu sistem şu an paper-trading modunda çalışsa ve geçmiş başarılar geleceğin garantisi olmasa da (çünkü finansın doğasında bu vardır), HeroBot AI bize bir şeyi kanıtlıyor: Geleceğin başarılı yatırımcısı, duygularıyla değil, kusursuz çalışan algoritmalarıyla hareket eden kişidir.</p>
      </div>
    </div>
    <p style="text-align:center;font-size:18px;font-weight:700;color:#E9EDF0;margin:10px 0 0 0;line-height:1.6">Yatırım kararlarınızda duygularınızı bir kenara bırakıp, stratejinin soğukkanlı disiplinine güvenmeye hazır mısınız?</p>
    <div style="display:flex;justify-content:center;margin-top:6px">
      <a href="/register" class="btn btn-primary">Ücretsiz Dene</a>
    </div>
  </div>
</div>

<!-- ===== TANITIM VİDEOSU ===== -->
<div style="background:#0D1114;border-top:1px solid #1B2126;border-bottom:1px solid #1B2126">
  <div class="wrap" style="padding:84px 0">
    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:40px">
      <span class="eyebrow">Bizi tanıyın</span>
      <h2 class="disp" style="margin:12px 0 0 0;font-size:32px;font-weight:700">Herobot-ai'ı anlatan kısa video</h2>
    </div>
    <div class="card" style="max-width:900px;margin:0 auto;padding:10px;overflow:hidden">
      <video controls preload="metadata" playsinline style="width:100%;display:block;border-radius:8px;background:#000">
        <source src="/static/tanitim.mp4" type="video/mp4">
        Tarayıcınız video oynatmayı desteklemiyor.
      </video>
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
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <a href="/#strateji" style="font-size:13px;color:#7A8590;font-weight:600">Strateji</a>
        <a href="/#piyasalar" style="font-size:13px;color:#7A8590;font-weight:600">Piyasalar</a>
        <a href="/#guvenlik" style="font-size:13px;color:#7A8590;font-weight:600">Güvenlik</a>
        <a href="/tanitim" style="font-size:13px;color:#7A8590;font-weight:600">Tanıtım</a>
        <a href="/login" style="font-size:13px;color:#7A8590;font-weight:600">Panel</a>
      </div>
    </div>
  </div>
</div>

</body>
</html>
'''

# Shared "Abonelik Sistemine Geç" / "Admin'e Soru Sor" / FAQ block — embedded
# both in the main dashboard (HTML, under the account panel) and in the
# trial-expired page (TRIAL_EXPIRED_HTML, where it's the main call to
# action). __USERNAME__/__IBAN__/__IBAN_HOLDER__ are replaced the same way
# __USERNAME__ already is elsewhere in these templates.
ACCOUNT_EXTRAS_HTML = r'''
<div class="stack-gap">
  <div class="account-row">
    <button class="btn" type="button" onclick="toggleBox('subscriptionBox')" data-i18n="account.subscriptionBtn">💳 Switch to Subscription</button>
    <button class="btn" type="button" onclick="toggleBox('askAdminBox')" data-i18n="account.askAdminBtn">✉️ Ask Admin</button>
  </div>

  <div id="subscriptionBox" style="display:none">
    <div class="section-title" style="margin-top:14px" data-i18n="subscription.title">Switch to Subscription</div>
    <p class="text-faint" style="font-size:12.5px;margin:0 0 4px 0" data-i18n="subscription.desc">Choose a plan, send the payment to the IBAN below, and click "I've Sent the Payment" — our team will verify your payment and activate your account as soon as possible.</p>
    <div class="plan-choices">
      <button type="button" class="plan-btn" id="planBtn_monthly" onclick="selectPlan('monthly')">
        <span class="plan-name" data-i18n="subscription.planMonthlyName">Monthly Subscription</span>
        <span class="plan-price" data-i18n="subscription.planMonthlyPrice">50 USD</span>
      </button>
      <button type="button" class="plan-btn" id="planBtn_annual" onclick="selectPlan('annual')">
        <span class="plan-name" data-i18n="subscription.planAnnualName">Annual Subscription</span>
        <span class="plan-price" data-i18n="subscription.planAnnualPrice">500 USD</span>
      </button>
    </div>
    <div id="planIbanBox" style="display:none">
      <div class="iban-box">
        <div class="account-row text-faint"><span data-i18n="subscription.selectedPlanLabel">Selected plan:</span> <b id="planSelectedLabel" style="color:var(--text)">—</b></div>
        <div class="account-row" style="margin-top:8px" data-i18n="subscription.ibanLabel">IBAN:</div>
        <div class="iban-num">__IBAN__</div>
        <div class="account-row text-faint" style="margin-top:4px"><span data-i18n="subscription.recipientLabel">Recipient:</span> __IBAN_HOLDER__</div>
        <div class="account-row text-faint" data-i18n="subscription.usernameNote" data-i18n-html="1">Adding your username (<b>__USERNAME__</b>) to the payment description speeds up verification.</div>
        <div class="account-row" style="margin-top:10px">
          <button class="btn btn-primary" type="button" id="paySentBtn" onclick="confirmPaymentSent()" data-i18n="subscription.paySentBtn">I've Sent the Payment</button>
        </div>
        <div id="paySentMsg" style="margin-top:8px;font-size:12.5px"></div>
      </div>
    </div>
  </div>

  <div id="askAdminBox" class="ask-box" style="display:none">
    <div class="section-title" style="margin-top:14px" data-i18n="askAdmin.title">Ask Admin</div>
    <p class="text-faint" style="font-size:12.5px;margin:0 0 8px 0" data-i18n="askAdmin.desc">Your message will be sent to herobotai.int@gmail.com.</p>
    <textarea id="askAdminMsg" placeholder="Type your question here…" data-i18n-placeholder="askAdmin.placeholder"></textarea>
    <div class="account-row" style="margin-top:8px">
      <button class="btn btn-primary" type="button" id="askAdminBtn" onclick="submitAskAdmin()" data-i18n="askAdmin.sendBtn">Send</button>
    </div>
    <div id="askAdminResult" style="margin-top:8px;font-size:12.5px"></div>
  </div>

  <div>
    <div class="section-title" style="margin-top:18px" data-i18n="faq.title">FAQ — Frequently Asked Questions</div>
    <div>
      <details class="faq-item">
        <summary data-i18n="faq.q1">Does this bot trade with real money?</summary>
        <p data-i18n="faq.a1">By default, no — the system runs in paper/demo mode and never places real orders. To trade with real money you need to connect your Binance API key and turn on "Live Trading" yourself from your own panel.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q2">How long is the free trial and what happens when it ends?</summary>
        <p data-i18n="faq.a2">7 days. Once it ends, your access to the panel is restricted; to continue, just pick a plan here, pay to the IBAN, and click "I've Sent the Payment" — our team will verify it and activate your account.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q3">How is the subscription paid, is card payment available?</summary>
        <p data-i18n="faq.a3">Currently payments are accepted via bank transfer/EFT to the IBAN. The monthly plan is billed as 50 USD, the annual plan as 500 USD.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q4">Is it safe to give my Binance API key?</summary>
        <p data-i18n="faq.a4">Your key is stored encrypted on the server and is only used to open and close orders on your behalf. We recommend creating an API key without "withdrawal" permission on the Binance side.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q5">Which markets are traded?</summary>
        <p data-i18n="faq.a5">Binance Futures (crypto perpetuals), Borsa Istanbul, and US stocks (NASDAQ/NYSE/AMEX) — all scanned from a single panel.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q6">How often are signals generated?</summary>
        <p data-i18n="faq.a6">The system runs an automatic scan roughly every 15 minutes; signals are only generated from closed 4-hour candles, not from instantaneous price noise.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q7">How do I turn on Telegram notifications?</summary>
        <p data-i18n="faq.a7">Just get a link code from the "Telegram Connection" section on the panel and start the bot on Telegram — open/close and daily summary notifications arrive automatically.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q8">What should I do if I need to urgently close an open position?</summary>
        <p data-i18n="faq.a8">You can use the "Close Now" button next to the relevant position in the "Binance Live Account" panel; this instantly sends a real market order and closes the position.</p>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq.q9">I have another question, who can I reach?</summary>
        <p data-i18n="faq.a9">Just click the "Ask Admin" button above and write your message — it goes straight to the admin team.</p>
      </details>
    </div>
  </div>
</div>
<script>
function toggleBox(id){
  const el=document.getElementById(id);
  if(!el) return;
  el.style.display = (el.style.display==='none'||!el.style.display) ? 'block' : 'none';
}
let _selectedPlan=null;
function selectPlan(plan){
  _selectedPlan=plan;
  document.getElementById('planBtn_monthly').classList.toggle('selected', plan==='monthly');
  document.getElementById('planBtn_annual').classList.toggle('selected', plan==='annual');
  document.getElementById('planSelectedLabel').textContent = plan==='monthly' ? t('subscription.selectedMonthly') : t('subscription.selectedAnnual');
  document.getElementById('planIbanBox').style.display='block';
  document.getElementById('paySentMsg').textContent='';
  const btn=document.getElementById('paySentBtn');
  btn.disabled=false; btn.textContent=t('subscription.paySentBtn');
}
async function confirmPaymentSent(){
  if(!_selectedPlan) return;
  const btn=document.getElementById('paySentBtn');
  btn.disabled=true; btn.textContent=t('subscription.sending');
  try{
    const r=await fetch('/api/account/subscription-request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan:_selectedPlan})});
    const d=await r.json();
    document.getElementById('paySentMsg').innerHTML = d.ok
      ? '<span style="color:var(--bull)">'+t('subscription.successMsg')+'</span>'
      : '<span style="color:var(--bear)">'+(d.error||t('subscription.genericError'))+'</span>';
  }catch(e){
    document.getElementById('paySentMsg').innerHTML='<span style="color:var(--bear)">'+t('subscription.connectionError')+'</span>';
  }
  btn.disabled=false; btn.textContent=t('subscription.resendBtn');
}
async function submitAskAdmin(){
  const msg=(document.getElementById('askAdminMsg').value||'').trim();
  const resultEl=document.getElementById('askAdminResult');
  if(!msg){ resultEl.innerHTML='<span style="color:var(--bear)">'+t('askAdmin.emptyMessage')+'</span>'; return; }
  const btn=document.getElementById('askAdminBtn');
  btn.disabled=true; btn.textContent=t('askAdmin.sending');
  try{
    const r=await fetch('/api/account/ask-admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    const d=await r.json();
    if(d.ok){
      resultEl.innerHTML='<span style="color:var(--bull)">'+t('askAdmin.successMsg')+'</span>';
      document.getElementById('askAdminMsg').value='';
    } else {
      resultEl.innerHTML='<span style="color:var(--bear)">'+(d.error||t('askAdmin.genericError'))+'</span>';
    }
  }catch(e){
    resultEl.innerHTML='<span style="color:var(--bear)">'+t('askAdmin.connectionError')+'</span>';
  }
  btn.disabled=false; btn.textContent=t('askAdmin.sendBtn');
}
</script>
'''

HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
.lang-select{background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:var(--font-d)}
.lang-select:hover{border-color:var(--accent);color:var(--accent)}
.lang-select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
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
.tv-chart-empty{padding:60px 18px;text-align:center;color:var(--text-faint);font-size:13px}
.tv-chart-frame{width:100%;height:560px;border:0;display:block}
.tv-chart-frame.hidden{display:none}
@media(max-width:640px){.tv-chart-frame{height:400px}}
.stack-gap{display:flex;flex-direction:column;gap:10px}
.section-title{font-size:15px;font-weight:700;margin:0 0 6px 0}
.plan-choices{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
.plan-btn{flex:1;min-width:140px;background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;padding:14px;cursor:pointer;text-align:left;color:var(--text);font-family:var(--font-d)}
.plan-btn:hover{border-color:var(--accent)}
.plan-btn.selected{border-color:var(--accent);background:var(--accent-soft)}
.plan-btn .plan-name{font-size:13px;font-weight:600;color:var(--text-dim)}
.plan-btn .plan-price{display:block;font-family:var(--font-m);font-size:18px;font-weight:700;color:var(--accent);margin-top:4px}
.iban-box{background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:4px}
.iban-box .iban-num{font-family:var(--font-m);font-size:15px;font-weight:700;letter-spacing:1px;word-break:break-all;color:var(--text)}
.ask-box textarea{width:100%;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:13px;font-family:var(--font-d);min-height:100px;resize:vertical;box-sizing:border-box}
.faq-item{border-bottom:1px solid var(--border-soft);padding:10px 0}
.faq-item:last-child{border-bottom:none}
.faq-item summary{cursor:pointer;font-weight:600;font-size:13.5px;list-style:none}
.faq-item summary::-webkit-details-marker{display:none}
.faq-item summary::before{content:'+ ';color:var(--accent);font-weight:700}
.faq-item[open] summary::before{content:'\2013 '}
.faq-item p{margin:8px 0 0 0;font-size:13px;color:var(--text-dim);line-height:1.6}
.link-btn{background:none;border:none;color:var(--accent);cursor:pointer;font-size:12.5px;padding:0;text-decoration:underline;font-family:var(--font-d)}

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
    <select class="lang-select" id="langSelect" aria-label="Language" onchange="applyTranslation(this.value)">
      <option value="en">English</option>
      <option value="tr">Türkçe</option>
      <option value="zh">中文</option>
      <option value="de">Deutsch</option>
      <option value="fr">Français</option>
      <option value="es">Español</option>
    </select>
    <span class="clock" id="clock">—:—:—</span>
    <span class="status-pill wait" id="status"><span class="dot"></span><span data-i18n="nav.connecting">Connecting</span></span>
    <span class="text-faint" id="whoami">__USERNAME__</span>
    <a class="btn" id="adminLink" href="/admin" style="display:none;text-decoration:none" data-i18n="nav.admin">Admin</a>
    <button class="btn" onclick="logout()" data-i18n="nav.logout">Logout</button>
  </div>
</header>

<section class="kpistrip">
  <div class="kpi kpi-equity">
    <div class="kpi-label" data-i18n="kpi.equity">Current balance</div>
    <div class="kpi-value" id="equity">—</div>
    <div class="kpi-sub" id="pnl">—</div>
    <svg class="sparkline" id="sparkline" viewBox="0 0 200 30" preserveAspectRatio="none"></svg>
  </div>
  <div class="kpi-divider"></div>
  <div class="kpi"><div class="kpi-label" data-i18n="kpi.openPnl">Open positions P&amp;L</div><div class="kpi-value" id="openPnl">—</div><div class="kpi-sub" id="openPnlSub">—</div></div>
  <div class="kpi-divider"></div>
  <div class="kpi"><div class="kpi-label" data-i18n="kpi.trades">Trades &middot; win rate</div><div class="kpi-value" id="trades">—</div><div class="kpi-sub" id="winrate">—</div></div>
  <div class="kpi-divider"></div>
  <div class="kpi"><div class="kpi-label" data-i18n="kpi.profitFactor">Profit factor</div><div class="kpi-value" id="pf">—</div><div class="kpi-sub" id="avg">—</div></div>
  <div class="kpi-divider"></div>
  <div class="kpi"><div class="kpi-label" data-i18n="kpi.maxDrawdown">Max. drawdown</div><div class="kpi-value" id="dd">—</div><div class="kpi-sub" id="candle">—</div></div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="account.title">My Account</h2></div>
  <div class="position-body">
    <div class="account-row">👤 <b>__USERNAME__</b></div>
    <div class="account-row text-faint">✉️ <span id="acctEmail">—</span></div>
    <div style="margin-top:14px">''' + ACCOUNT_EXTRAS_HTML + r'''</div>
  </div>
</section>

<section class="cols">
  <div class="panel">
    <div class="panel-head"><h2><span data-i18n="panel.openPosition">Open position</span> <span class="text-faint" id="detailSymbol">— ETHUSDT</span></h2></div>
    <div class="position-body" id="position" data-i18n="panel.loading">Loading…</div>
  </div>
  <div class="panel">
    <div class="panel-head"><h2 data-i18n="panel.signalMatrix">Signal matrix</h2></div>
    <div class="signal-body" id="signals">—</div>
  </div>
</section>

<section class="panel scanner-panel">
  <div class="panel-head scanner-tabs">
    <button class="tab active" data-tab="crypto" onclick="switchTab('crypto')" data-i18n="scanner.tabCrypto">Binance Futures</button>
    <button class="tab" data-tab="bist" onclick="switchTab('bist')" data-i18n="scanner.tabBist">Borsa Istanbul</button>
    <button class="tab" data-tab="us" onclick="switchTab('us')" data-i18n="scanner.tabUs">Wall Street</button>
    <div class="scanner-note" id="scannerNote" data-i18n="scanner.note">USDT-M perpetual &middot; 4H closed candle &middot; for signal purposes only, no real orders</div>
  </div>

  <div class="tabpane active" id="tab-crypto">
    <div class="scanner-controls">
      <input id="coinSearch" placeholder="Search coin (e.g. BTC)" data-i18n-placeholder="scanner.coinSearchPlaceholder" oninput="renderScanner()">
      <select id="signalFilter" onchange="renderScanner()">
        <option value="ALL" data-i18n="scanner.allSignals">All signals</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>
      </select>
      <button class="btn" onclick="startScanner(true)" data-i18n="scanner.scanAll">Scan all</button>
      <span class="scanner-status" id="scannerStatus" data-i18n="scanner.preparing">Preparing…</span>
    </div>
    <div class="scanner-summary"><span id="coinCount">0 coins</span><span class="tag tag-long" id="longCount">LONG 0</span><span class="tag tag-short" id="shortCount">SHORT 0</span><span class="tag tag-flat" id="noCount">NO SIGNAL 0</span></div>
    <div class="table-scroll tall">
      <table class="datatable" id="scannerTable">
        <thead><tr>
          <th class="sortable" data-key="symbol" data-tbl="scanner" data-i18n="scanner.headerCoin">Coin</th>
          <th class="sortable num" data-key="price" data-tbl="scanner" data-i18n="scanner.headerPrice">Price</th>
          <th class="sortable num" data-key="change_pct" data-tbl="scanner" data-i18n="scanner.headerChange24h">24h %</th>
          <th class="sortable num" data-key="volume" data-tbl="scanner" data-i18n="scanner.headerVolume">Volume</th>
          <th data-key="st" data-i18n="scanner.headerSt">ST</th>
          <th class="sortable num" data-key="adx" data-tbl="scanner" data-i18n="scanner.headerAdx">ADX</th>
          <th class="sortable num" data-key="rsi" data-tbl="scanner" data-i18n="scanner.headerRsi">RSI</th>
          <th class="sortable num" data-key="cci" data-tbl="scanner" data-i18n="scanner.headerCci">CCI</th>
          <th data-i18n="scanner.headerMacd">MACD</th>
          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="scanner" data-i18n="scanner.headerAtrp">ATRP %ile</th>
          <th class="sortable" data-key="signal" data-tbl="scanner" data-i18n="scanner.headerSignal">Signal</th>
          <th data-i18n="scanner.headerReason">Description</th>
          <th data-i18n="scanner.headerAdd">Add</th>
        </tr></thead>
        <tbody id="scannerRows"><tr><td colspan="13" class="empty" data-i18n="scanner.waiting">Waiting for scan…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="tabpane" id="tab-bist">
    <div class="scanner-controls">
      <input id="bistSearch" placeholder="Search stock (e.g. THYAO)" data-i18n-placeholder="scanner.stockSearchPlaceholderBist" oninput="renderBistScanner()">
      <select id="bistSignalFilter" onchange="renderBistScanner()">
        <option value="ALL" data-i18n="scanner.allSignals">All signals</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>
      </select>
      <button class="btn" onclick="startBistScanner(true)" data-i18n="scanner.scanBist">Scan Borsa Istanbul</button>
      <span class="scanner-status" id="bistScannerStatus" data-i18n="scanner.preparing">Preparing…</span>
    </div>
    <div class="scanner-summary"><span id="bistCount">0 stocks</span><span class="tag tag-long" id="bistLongCount">LONG 0</span><span class="tag tag-short" id="bistShortCount">SHORT 0</span><span class="tag tag-flat" id="bistNoCount">NO SIGNAL 0</span></div>
    <div class="table-scroll tall">
      <table class="datatable" id="bistScannerTable">
        <thead><tr>
          <th class="sortable" data-key="symbol" data-tbl="bist" data-i18n="scanner.headerStock">Stock</th>
          <th class="sortable num" data-key="price" data-tbl="bist" data-i18n="scanner.headerPrice">Price</th>
          <th class="sortable num" data-key="change_pct" data-tbl="bist" data-i18n="scanner.headerChangeDaily">Daily %</th>
          <th data-key="st" data-i18n="scanner.headerSt">ST</th>
          <th class="sortable num" data-key="adx" data-tbl="bist" data-i18n="scanner.headerAdx">ADX</th>
          <th class="sortable num" data-key="rsi" data-tbl="bist" data-i18n="scanner.headerRsi">RSI</th>
          <th class="sortable num" data-key="cci" data-tbl="bist" data-i18n="scanner.headerCci">CCI</th>
          <th data-i18n="scanner.headerMacd">MACD</th>
          <th class="num" data-i18n="scanner.headerStochKd">Stoch K/D</th>
          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="bist" data-i18n="scanner.headerAtrp">ATRP %ile</th>
          <th class="sortable" data-key="signal" data-tbl="bist" data-i18n="scanner.headerSignal">Signal</th>
          <th data-i18n="scanner.headerReason">Description</th>
          <th data-i18n="scanner.headerAdd">Add</th>
        </tr></thead>
        <tbody id="bistScannerRows"><tr><td colspan="13" class="empty" data-i18n="scanner.waiting">Waiting for scan…</td></tr></tbody>
      </table>
    </div>
    <div class="footnote" data-i18n="scanner.bistFootnote">SHORT here is only the strategy's technical signal; it does not mean a direct short-sale order on the BIST spot market.</div>
  </div>

  <div class="tabpane" id="tab-us">
    <div class="scanner-controls">
      <input id="usSearch" placeholder="Search stock (e.g. AAPL)" data-i18n-placeholder="scanner.stockSearchPlaceholderUs" oninput="renderUsScanner()">
      <select id="usSignalFilter" onchange="renderUsScanner()">
        <option value="ALL" data-i18n="scanner.allSignals">All signals</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option><option value="NO SIGNAL">NO SIGNAL</option>
      </select>
      <button class="btn" onclick="startUsScanner(true)" data-i18n="scanner.scanUs">Scan S&amp;P500/Nasdaq-100</button>
      <span class="scanner-status" id="usScannerStatus" data-i18n="scanner.preparing">Preparing…</span>
    </div>
    <div class="scanner-summary"><span id="usCount">0 stocks</span><span class="tag tag-long" id="usLongCount">LONG 0</span><span class="tag tag-short" id="usShortCount">SHORT 0</span><span class="tag tag-flat" id="usNoCount">NO SIGNAL 0</span></div>
    <div class="table-scroll tall">
      <table class="datatable" id="usScannerTable">
        <thead><tr>
          <th class="sortable" data-key="symbol" data-tbl="us" data-i18n="scanner.headerStock">Stock</th>
          <th class="sortable num" data-key="price" data-tbl="us" data-i18n="scanner.headerPrice">Price</th>
          <th class="sortable num" data-key="change_pct" data-tbl="us" data-i18n="scanner.headerChangeDaily">Daily %</th>
          <th data-key="st" data-i18n="scanner.headerSt">ST</th>
          <th class="sortable num" data-key="adx" data-tbl="us" data-i18n="scanner.headerAdx">ADX</th>
          <th class="sortable num" data-key="rsi" data-tbl="us" data-i18n="scanner.headerRsi">RSI</th>
          <th class="sortable num" data-key="cci" data-tbl="us" data-i18n="scanner.headerCci">CCI</th>
          <th data-i18n="scanner.headerMacd">MACD</th>
          <th class="num" data-i18n="scanner.headerStochKd">Stoch K/D</th>
          <th class="sortable num" data-key="atrp_percentile_1d" data-tbl="us" data-i18n="scanner.headerAtrp">ATRP %ile</th>
          <th class="sortable" data-key="signal" data-tbl="us" data-i18n="scanner.headerSignal">Signal</th>
          <th data-i18n="scanner.headerReason">Description</th>
          <th data-i18n="scanner.headerAdd">Add</th>
        </tr></thead>
        <tbody id="usScannerRows"><tr><td colspan="13" class="empty" data-i18n="scanner.waiting">Waiting for scan…</td></tr></tbody>
      </table>
    </div>
    <div class="footnote" data-i18n="scanner.usFootnote">S&amp;P 500 + Nasdaq-100 universe (static list, should be updated periodically). US stocks added to the watchlist open an independent paper position just like the crypto watchlist; the SHORT side is a pure simulation that does not model borrow/margin constraints.</div>
  </div>
</section>

<section class="panel" id="tvChartPanel">
  <div class="panel-head"><h2><span data-i18n="tvChart.title">TradingView Chart</span> <span class="text-faint" id="tvChartSymbol" data-i18n="tvChart.noSymbol">— no symbol selected</span></h2></div>
  <div id="tvChartEmpty" class="tv-chart-empty" data-i18n="tvChart.emptyMessage">Click a row in the scan tables above to view that symbol's TradingView chart here.</div>
  <iframe id="tvChartFrame" class="tv-chart-frame hidden" allowfullscreen></iframe>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.paperTrades">Paper Trades</h2><span class="text-faint" id="watchlistCount">0 / 10</span></div>
  <div class="table-scroll">
    <table class="datatable">
      <thead><tr><th data-i18n="watchlist.headerSymbol">Symbol</th><th data-i18n="watchlist.headerMarket">Market</th><th data-i18n="watchlist.headerDirection">Direction / Signal</th><th class="num" data-i18n="watchlist.headerPrice">Price</th><th class="num" data-i18n="watchlist.headerUnrealizedPnl">Unrealized P&amp;L</th><th data-i18n="watchlist.headerAdded">Added</th><th></th></tr></thead>
      <tbody id="watchlistRows"><tr><td colspan="7" class="empty" data-i18n="watchlist.loading">Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="footnote"><span data-i18n="watchlist.footnotePrefix">Click a row to view that symbol's position and signal detail below. Crypto symbols open an independent paper position using the same strategy (size: $</span><span id="wlUsd">—</span><span data-i18n="watchlist.footnoteSuffix"> notional). Borsa Istanbul symbols are signal-only; no real/paper order is placed.</span></div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.binanceConnection">Binance Connection</h2><span class="text-faint" id="binanceStatusPill">—</span></div>
  <div class="position-body" id="accountBody">
    <div class="pos-empty" data-i18n="panel.loading">Loading…</div>
  </div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.telegramConnection">Telegram Connection</h2></div>
  <div class="position-body">
    <div id="telegramPanelBody"><div class="pos-empty" data-i18n="panel.loading">Loading…</div></div>
  </div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.liveAccount">Binance Live Account</h2><span class="text-faint" id="liveMineCount">—</span></div>
  <div class="live-panel" style="margin-top:0">
    <h4 data-i18n="liveAccount.title">Live Trading (Real Money)</h4>
    <div id="livePanelBody"><div class="pos-empty" data-i18n="panel.loading">Loading…</div></div>
  </div>
  <div class="table-scroll" style="margin-top:14px">
    <table class="datatable">
      <thead><tr><th data-i18n="watchlist.headerSymbol">Symbol</th><th data-i18n="history.headerDirection">Direction</th><th class="num" data-i18n="liveOpen.headerQty">Qty</th><th class="num" data-i18n="pos.entry">Entry</th><th class="num" data-i18n="liveOpen.headerCurrent">Current</th><th class="num" data-i18n="watchlist.headerUnrealizedPnl">Unrealized P&amp;L</th><th data-i18n="liveOpen.headerLeverage">Leverage</th><th data-i18n="liveOpen.headerOpened">Opened</th><th></th></tr></thead>
      <tbody id="liveMineOpenRows"><tr><td colspan="9" class="empty" data-i18n="panel.loading">Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="table-scroll" style="margin-top:14px">
    <table class="datatable">
      <thead><tr><th data-i18n="history.headerDate">Date</th><th data-i18n="history.headerDirection">Direction</th><th data-i18n="watchlist.headerSymbol">Symbol</th><th class="num" data-i18n="pos.entry">Entry</th><th class="num" data-i18n="history.headerExit">Exit</th><th class="num" data-i18n="history.headerPnl">P&amp;L</th><th data-i18n="history.headerReason">Reason</th></tr></thead>
      <tbody id="liveMineClosedRows"><tr><td colspan="7" class="empty" data-i18n="panel.loading">Loading…</td></tr></tbody>
    </table>
  </div>
  <div class="footnote" data-i18n="liveAccount.footnote" data-i18n-html="1">This panel only shows live trades on <b>your own</b> Binance account — it's never mixed with the paper/demo panel above or with other users; no one but you can see this.</div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.recentTrades">Recent Trades</h2></div>
  <div class="table-scroll">
    <table class="datatable">
      <thead><tr><th data-i18n="history.headerDate">Date</th><th data-i18n="history.headerDirection">Direction</th><th data-i18n="watchlist.headerSymbol">Symbol</th><th class="num" data-i18n="pos.entry">Entry</th><th class="num" data-i18n="history.headerExit">Exit</th><th class="num" data-i18n="history.headerPnl">P&amp;L</th><th data-i18n="history.headerReason">Reason</th></tr></thead>
      <tbody id="history"><tr><td colspan="7" class="empty" data-i18n="panel.loading">Loading…</td></tr></tbody>
    </table>
  </div>
</section>

<section class="panel">
  <div class="panel-head"><h2 data-i18n="panel.aiAnalyst">AI Trade Analyst</h2><button class="btn" id="aiRunBtn" onclick="runAiAnalysis()" data-i18n="ai.runBtn">Analyze Now</button></div>
  <div class="ai-body" id="aiAnalysisBody" data-i18n="panel.loading">Loading…</div>
</section>

<div class="page-footer" data-i18n="footer.autoRefresh">Auto-refresh: position 5s &middot; scanners 10s &middot; paper trading, no real orders.</div>
</div>

<script>
const translations = {
  en: {
  "nav.connecting": "Connecting",
  "nav.botActive": "Bot active",
  "nav.standby": "Standby",
  "nav.connectionError": "Connection error",
  "nav.admin": "Admin",
  "nav.logout": "Logout",
  "nav.language": "Language",
  "kpi.equity": "Current balance",
  "kpi.openPnl": "Open positions P&L",
  "kpi.openPnlSub": "All open positions",
  "kpi.trades": "Trades · win rate",
  "kpi.winRate": "Win rate",
  "kpi.profitFactor": "Profit factor",
  "kpi.avg": "Avg.",
  "kpi.maxDrawdown": "Max. drawdown",
  "kpi.lastCandle": "Last candle:",
  "account.title": "My Account",
  "account.subscriptionBtn": "💳 Switch to Subscription",
  "account.askAdminBtn": "✉️ Ask Admin",
  "account.emailNotRegistered": "(no email on file)",
  "account.admin": "(admin)",
  "account.trialDaysLeft": "Trial: {n} days left",
  "account.trialExpired": "Trial expired",
  "account.credentialWarning": "⚠️ CREDENTIAL_ENCRYPTION_KEY is not configured on the server — API keys cannot be saved because they cannot be encrypted safely. Please contact your admin.",
  "account.savedKeyLabel": "Saved key:",
  "account.lastVerified": "Last verified:",
  "account.riskAckGiven": "Risk acknowledgement given on",
  "account.riskAckLabel": "I confirm and accept that this bot may place real-money orders on crypto futures, that it carries a risk of loss, and that any resulting losses are my own responsibility, not the bot's.",
  "account.apiKeyLabel": "Binance API Key",
  "account.apiSecretLabel": "Binance API Secret",
  "account.apiKeyPlaceholderChange": "Enter a new key to change it",
  "account.apiKeyPlaceholderNew": "Binance Futures API key",
  "account.apiSecretPlaceholderChange": "Enter a new secret to change it",
  "account.apiSecretPlaceholderNew": "Binance Futures API secret",
  "account.saveVerifyBtn": "Save & Verify",
  "account.savingVerifying": "Saving & verifying…",
  "account.removeConnectionBtn": "Remove Connection",
  "account.verifyError": "Verification error",
  "account.verifiedConnected": "Connected and verified",
  "account.connectedNotVerified": "Connected, not verified",
  "account.notConnected": "Not connected",
  "subscription.title": "Switch to Subscription",
  "subscription.desc": "Choose a plan, send the payment to the IBAN below, and click “I've Sent the Payment” — our team will verify your payment and activate your account as soon as possible.",
  "subscription.planMonthlyName": "Monthly Subscription",
  "subscription.planMonthlyPrice": "50 USD",
  "subscription.planAnnualName": "Annual Subscription",
  "subscription.planAnnualPrice": "500 USD",
  "subscription.selectedPlanLabel": "Selected plan:",
  "subscription.ibanLabel": "IBAN:",
  "subscription.recipientLabel": "Recipient:",
  "subscription.usernameNote": "Adding your username (<b>__USERNAME__</b>) to the payment description speeds up verification.",
  "subscription.paySentBtn": "I've Sent the Payment",
  "subscription.sending": "Sending…",
  "subscription.resendBtn": "Notify Again",
  "subscription.successMsg": "✓ Notification received — our team will activate your account once your payment is verified.",
  "subscription.genericError": "Something went wrong, please try again.",
  "subscription.connectionError": "Connection error, please try again.",
  "subscription.selectedMonthly": "Monthly — 50 USD",
  "subscription.selectedAnnual": "Annual — 500 USD",
  "askAdmin.title": "Ask Admin",
  "askAdmin.desc": "Your message will be sent to herobotai.int@gmail.com.",
  "askAdmin.placeholder": "Type your question here…",
  "askAdmin.sendBtn": "Send",
  "askAdmin.sending": "Sending…",
  "askAdmin.emptyMessage": "Please enter a message.",
  "askAdmin.successMsg": "✓ Your message has been sent. We'll get back to you shortly.",
  "askAdmin.genericError": "Could not send, please try again.",
  "askAdmin.connectionError": "Connection error, please try again.",
  "faq.title": "FAQ — Frequently Asked Questions",
  "faq.q1": "Does this bot trade with real money?",
  "faq.a1": "By default, no — the system runs in paper/demo mode and never places real orders. To trade with real money you need to connect your Binance API key and turn on “Live Trading” yourself from your own panel.",
  "faq.q2": "How long is the free trial and what happens when it ends?",
  "faq.a2": "7 days. Once it ends, your access to the panel is restricted; to continue, just pick a plan here, pay to the IBAN, and click “I've Sent the Payment” — our team will verify it and activate your account.",
  "faq.q3": "How is the subscription paid, is card payment available?",
  "faq.a3": "Currently payments are accepted via bank transfer/EFT to the IBAN. The monthly plan is billed as 50 USD, the annual plan as 500 USD.",
  "faq.q4": "Is it safe to give my Binance API key?",
  "faq.a4": "Your key is stored encrypted on the server and is only used to open and close orders on your behalf. We recommend creating an API key without “withdrawal” permission on the Binance side.",
  "faq.q5": "Which markets are traded?",
  "faq.a5": "Binance Futures (crypto perpetuals), Borsa Istanbul, and US stocks (NASDAQ/NYSE/AMEX) — all scanned from a single panel.",
  "faq.q6": "How often are signals generated?",
  "faq.a6": "The system runs an automatic scan roughly every 15 minutes; signals are only generated from closed 4-hour candles, not from instantaneous price noise.",
  "faq.q7": "How do I turn on Telegram notifications?",
  "faq.a7": "Just get a link code from the “Telegram Connection” section on the panel and start the bot on Telegram — open/close and daily summary notifications arrive automatically.",
  "faq.q8": "What should I do if I need to urgently close an open position?",
  "faq.a8": "You can use the “Close Now” button next to the relevant position in the “Binance Live Account” panel; this instantly sends a real market order and closes the position.",
  "faq.q9": "I have another question, who can I reach?",
  "faq.a9": "Just click the “Ask Admin” button above and write your message — it goes straight to the admin team.",
  "panel.openPosition": "Open position",
  "panel.loading": "Loading…",
  "pos.noOpenPosition": "No open paper position. It will appear here once a signal is generated.",
  "pos.entry": "Entry",
  "pos.current": "Current",
  "pos.unrealizedPnl": "Unrealized P&L",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "Current price",
  "pos.trailingActive": "ACTIVE @",
  "pos.trailingStandby": "standby",
  "pos.trailingLabel": "Trailing:",
  "pos.entryTimeLabel": "Entry time:",
  "panel.signalMatrix": "Signal matrix",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "1D Volatility",
  "signal.atrp1d": "1D ATRP %ile",
  "signal.final": "Final signal",
  "scanner.tabCrypto": "Binance Futures",
  "scanner.tabBist": "Borsa Istanbul",
  "scanner.tabUs": "Wall Street",
  "scanner.note": "USDT-M perpetual · 4H closed candle · for signal purposes only, no real orders",
  "scanner.coinSearchPlaceholder": "Search coin (e.g. BTC)",
  "scanner.stockSearchPlaceholderBist": "Search stock (e.g. THYAO)",
  "scanner.stockSearchPlaceholderUs": "Search stock (e.g. AAPL)",
  "scanner.allSignals": "All signals",
  "scanner.scanAll": "Scan all",
  "scanner.scanBist": "Scan Borsa Istanbul",
  "scanner.scanUs": "Scan S&P500/Nasdaq-100",
  "scanner.preparing": "Preparing…",
  "scanner.waiting": "Waiting for scan…",
  "scanner.noResults": "No results.",
  "scanner.headerCoin": "Coin",
  "scanner.headerStock": "Stock",
  "scanner.headerPrice": "Price",
  "scanner.headerChange24h": "24h %",
  "scanner.headerChangeDaily": "Daily %",
  "scanner.headerVolume": "Volume",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP %ile",
  "scanner.headerSignal": "Signal",
  "scanner.headerReason": "Description",
  "scanner.headerAdd": "Add",
  "scanner.coinCountSuffix": "coins",
  "scanner.stockCountSuffix": "stocks",
  "scanner.bistFootnote": "SHORT here is only the strategy's technical signal; it does not mean a direct short-sale order on the BIST spot market.",
  "scanner.usFootnote": "S&P 500 + Nasdaq-100 universe (static list, should be updated periodically). US stocks added to the watchlist open an independent paper position just like the crypto watchlist; the SHORT side is a pure simulation that does not model borrow/margin constraints.",
  "scanner.scanning": "Scanning",
  "scanner.ready": "Ready",
  "scanner.lastScan4h": "Last 4H scan",
  "scanner.universe": "Universe",
  "scanner.error": "Error",
  "scanner.unknownError": "Unknown error",
  "scanner.startingScan": "Starting scan…",
  "scanner.startingUsScan": "Starting scan… (514 stocks, may take a few minutes)",
  "scanner.startingBistScan": "Starting Borsa Istanbul scan…",
  "tvChart.title": "TradingView Chart",
  "tvChart.noSymbol": "— no symbol selected",
  "tvChart.emptyMessage": "Click a row in the scan tables above to view that symbol's TradingView chart here.",
  "panel.paperTrades": "Paper Trades",
  "watchlist.headerSymbol": "Symbol",
  "watchlist.headerMarket": "Market",
  "watchlist.headerDirection": "Direction / Signal",
  "watchlist.headerPrice": "Price",
  "watchlist.headerUnrealizedPnl": "Unrealized P&L",
  "watchlist.headerAdded": "Added",
  "watchlist.footnotePrefix": "Click a row to view that symbol's position and signal detail below. Crypto symbols open an independent paper position using the same strategy (size: $",
  "watchlist.footnoteSuffix": " notional). Borsa Istanbul symbols are signal-only; no real/paper order is placed.",
  "watchlist.loading": "Loading…",
  "watchlist.empty": "Watchlist is empty. Click “+ Add” on any symbol showing LONG/SHORT in the scanner to have the bot watch/paper-trade it.",
  "watchlist.mainEngine": "Main engine",
  "watchlist.noPosition": "no position",
  "watchlist.watched": "watched",
  "watchlist.removeBtn": "Remove",
  "watchlist.addBtn": "+ Add",
  "watchlist.adding": "Adding…",
  "watchlist.added": "Added ✓",
  "watchlist.symbolRemoved": "This symbol may have been removed from the watchlist.",
  "market.binance": "Binance",
  "market.bist": "Borsa Istanbul",
  "market.usStock": "US Stock",
  "panel.binanceConnection": "Binance Connection",
  "panel.telegramConnection": "Telegram Connection",
  "panel.liveAccount": "Binance Live Account",
  "liveAccount.title": "Live Trading (Real Money)",
  "liveAccount.openPositionsSuffix": "open positions",
  "liveAccount.liveOff": "Live trading is off",
  "liveAccount.footnote": "This panel only shows live trades on <b>your own</b> Binance account — it's never mixed with the paper/demo panel above or with other users; no one but you can see this.",
  "panel.recentTrades": "Recent Trades",
  "history.headerDate": "Date",
  "history.headerDirection": "Direction",
  "history.headerSymbol": "Symbol",
  "history.headerEntry": "Entry",
  "history.headerExit": "Exit",
  "history.headerPnl": "P&L",
  "history.headerReason": "Reason",
  "history.noClosedTrades": "No closed trades yet.",
  "history.noClosedLiveTrades": "You have no closed live trades yet.",
  "liveOpen.headerQty": "Qty",
  "liveOpen.headerCurrent": "Current",
  "liveOpen.headerLeverage": "Leverage",
  "liveOpen.headerOpened": "Opened",
  "liveOpen.noOpenPositions": "You have no open live positions right now.",
  "liveOpen.closeNowBtn": "Close Now",
  "liveOpen.closing": "Closing…",
  "panel.aiAnalyst": "AI Trade Analyst",
  "ai.runBtn": "Analyze Now",
  "ai.running": "Analyzing…",
  "ai.disabled": "AI Analyst is disabled — ANTHROPIC_API_KEY is not set.",
  "ai.noAnalysisYet": "No analysis generated yet. Click “Analyze Now” to create the first report.",
  "ai.lastAttemptFailed": "Last attempt failed",
  "ai.tradesAnalyzedPrefix": "trades analyzed (total",
  "footer.autoRefresh": "Auto-refresh: position 5s · scanners 10s · paper trading, no real orders.",
  "live.needConnectFirst": "You need to save and verify your Binance API key above before you can start live (real money) trading.",
  "live.killSwitch": "🛑 All live trading has been temporarily stopped by the admin",
  "live.pausedToday": "⏸ Daily max loss limit reached — no new trades will open today",
  "live.on": "🟢 Live trading is ON",
  "live.off": "Live trading is off — the bot is running in paper (demo) mode only",
  "live.openPositionCount": "Open live positions:",
  "live.todayRealizedPnl": "Today's estimated realized P&L:",
  "live.positionUsdLabel": "USD amount per trade",
  "live.positionUsdPlaceholder": "e.g. 100",
  "live.maxLeverageLabel": "Max leverage (1-{n}x)",
  "live.maxLeveragePlaceholder": "e.g. 2",
  "live.dailyLossLimitLabel": "Daily max loss limit (USD) — trading stops automatically for the day if exceeded",
  "live.dailyLossLimitPlaceholder": "e.g. 50",
  "live.maxPositionsLabel": "Max open positions (1-{n})",
  "live.maxPositionsPlaceholder": "e.g. 1",
  "live.saveSettingsBtn": "Save Settings",
  "live.turnOffBtn": "Turn Off Live Trading",
  "live.turnOnBtn": "Turn ON Live Trading (real money)",
  "live.dangerText": "⚠️ Once live trading is turned on, the bot opens/closes orders using <b>real money</b> on your registered Binance account. You — not the bot — are responsible for any losses. This is not investment advice; compliance with applicable regulations is your own responsibility.",
  "live.toggleOnConfirm": "You are about to turn on live trading. From this moment the bot will open and close orders with REAL MONEY on your Binance account. Do you confirm that you accept the risk of loss and that these settings are correct?",
  "telegram.notEnabled": "Telegram bot is not configured on the server.",
  "telegram.linked": "🟢 Telegram connected",
  "telegram.notificationsDesc": "Live trade open/close notifications, risk alerts, and the daily summary will arrive here.",
  "telegram.removeConnectionBtn": "Remove Connection",
  "telegram.notLinkedDesc": "Connect to receive your live trade notifications on your own Telegram.",
  "telegram.getCodeBtn": "Get Link Code",
  "telegram.step1": "1) Open",
  "telegram.step1Fallback": "our bot",
  "telegram.step1End": "on Telegram.",
  "telegram.step2": "2) Send this:",
  "telegram.codeExpiresPrefix": "Code expires in",
  "telegram.minutes": "min",
  "telegram.seconds": "sec",
  "telegram.unlinkConfirm": "Are you sure you want to remove the Telegram connection?",
  "alert.apiKeySecretRequired": "API key and secret are required.",
  "alert.riskAckRequired": "You must check the risk acknowledgement box before continuing.",
  "alert.saveFailedGeneric": "Could not save",
  "alert.connectionError": "Connection error",
  "alert.notAdded": "Could not add",
  "alert.codeNotObtained": "Could not get code",
  "alert.disconnectBinanceConfirm": "Are you sure you want to remove the Binance connection?",
  "alert.closePositionConfirmPrefix": "Are you sure you want to close the",
  "alert.closePositionConfirmSuffix": "position now with a real market order? This cannot be undone.",
  "alert.closePositionFailed": "Could not close position",
  "alert.actionFailed": "Action failed"
},
  tr: {
  "nav.connecting": "Bağlanıyor",
  "nav.botActive": "Bot aktif",
  "nav.standby": "Beklemede",
  "nav.connectionError": "Bağlantı hatası",
  "nav.admin": "Yönetim",
  "nav.logout": "Çıkış",
  "nav.language": "Dil",
  "kpi.equity": "Güncel bakiye",
  "kpi.openPnl": "Açık pozisyonlar P&L",
  "kpi.openPnlSub": "Tüm açık pozisyonlar",
  "kpi.trades": "İşlem · kazanma oranı",
  "kpi.winRate": "Win rate",
  "kpi.profitFactor": "Profit factor",
  "kpi.avg": "Ort.",
  "kpi.maxDrawdown": "Maks. drawdown",
  "kpi.lastCandle": "Son mum:",
  "account.title": "Hesabım",
  "account.subscriptionBtn": "💳 Abonelik Sistemine Geç",
  "account.askAdminBtn": "✉️ Admin'e Soru Sor",
  "account.emailNotRegistered": "(e-posta kayıtlı değil)",
  "account.admin": "(admin)",
  "account.trialDaysLeft": "Deneme: {n} gün kaldı",
  "account.trialExpired": "Deneme doldu",
  "account.credentialWarning": "⚠️ Sunucuda CREDENTIAL_ENCRYPTION_KEY tanımlı değil — API anahtarları güvenle şifrelenemediği için kaydedilemez. Lütfen yöneticinizle iletişime geçin.",
  "account.savedKeyLabel": "Kayıtlı anahtar:",
  "account.lastVerified": "Son doğrulama:",
  "account.riskAckGiven": "Risk onayı tarihi",
  "account.riskAckLabel": "Bu botun kripto vadeli işlemlerde gerçek para ile emir açabileceğini, kayıp riski taşıdığını ve olası kayıplardan botun değil kendi sorumluluğumda olduğumu anladığımı ve kabul ettiğimi onaylıyorum.",
  "account.apiKeyLabel": "Binance API Key",
  "account.apiSecretLabel": "Binance API Secret",
  "account.apiKeyPlaceholderChange": "Değiştirmek için yeni key girin",
  "account.apiKeyPlaceholderNew": "Binance Futures API key",
  "account.apiSecretPlaceholderChange": "Değiştirmek için yeni secret girin",
  "account.apiSecretPlaceholderNew": "Binance Futures API secret",
  "account.saveVerifyBtn": "Kaydet ve Doğrula",
  "account.savingVerifying": "Kaydediliyor ve doğrulanıyor…",
  "account.removeConnectionBtn": "Bağlantıyı Kaldır",
  "account.verifyError": "Doğrulama hatası",
  "account.verifiedConnected": "Bağlı ve doğrulandı",
  "account.connectedNotVerified": "Bağlı, doğrulanmadı",
  "account.notConnected": "Bağlı değil",
  "subscription.title": "Abonelik Sistemine Geç",
  "subscription.desc": "Bir plan seçin, IBAN'a ödemeyi gönderin ve “Tutarı Gönderdim” butonuna basın — ekibimiz ödemenizi kontrol edip hesabınızı en kısa sürede aktif hale getirecek.",
  "subscription.planMonthlyName": "Aylık Abonelik",
  "subscription.planMonthlyPrice": "50 USD",
  "subscription.planAnnualName": "Yıllık Abonelik",
  "subscription.planAnnualPrice": "500 USD",
  "subscription.selectedPlanLabel": "Seçilen plan:",
  "subscription.ibanLabel": "IBAN:",
  "subscription.recipientLabel": "Alıcı:",
  "subscription.usernameNote": "Açıklama kısmına kullanıcı adınızı (<b>__USERNAME__</b>) yazmanız kontrolü hızlandırır.",
  "subscription.paySentBtn": "Tutarı Gönderdim",
  "subscription.sending": "Gönderiliyor…",
  "subscription.resendBtn": "Tekrar Bildir",
  "subscription.successMsg": "✓ Bildirim alındı — ekibimiz ödemenizi kontrol ettikten sonra hesabınızı aktif hale getirecek.",
  "subscription.genericError": "Bir hata oluştu, lütfen tekrar deneyin.",
  "subscription.connectionError": "Bağlantı hatası, lütfen tekrar deneyin.",
  "subscription.selectedMonthly": "Aylık — 50 USD",
  "subscription.selectedAnnual": "Yıllık — 500 USD",
  "askAdmin.title": "Admin'e Soru Sor",
  "askAdmin.desc": "Mesajınız herobotai.int@gmail.com adresine iletilecek.",
  "askAdmin.placeholder": "Sorunuzu buraya yazın…",
  "askAdmin.sendBtn": "Gönder",
  "askAdmin.sending": "Gönderiliyor…",
  "askAdmin.emptyMessage": "Lütfen bir mesaj yazın.",
  "askAdmin.successMsg": "✓ Mesajınız gönderildi. En kısa sürede size dönüş yapılacaktır.",
  "askAdmin.genericError": "Gönderilemedi, lütfen tekrar deneyin.",
  "askAdmin.connectionError": "Bağlantı hatası, lütfen tekrar deneyin.",
  "faq.title": "FAQ — Sıkça Sorulan Sorular",
  "faq.q1": "Bu bot gerçek parayla mı işlem yapıyor?",
  "faq.a1": "Varsayılan olarak hayır — sistem paper/demo modda çalışır ve gerçek emir göndermez. Gerçek parayla işlem yapmak isterseniz Binance API anahtarınızı bağlayıp “Canlı İşlem” ayarını kendi panelinizden siz açmanız gerekir.",
  "faq.q2": "Ücretsiz deneme süresi ne kadar ve dolunca ne olur?",
  "faq.a2": "7 gündür. Süre dolduğunda panele erişiminiz kısıtlanır; devam etmek için buradan bir plan seçip IBAN'a ödeme yaptıktan sonra “Tutarı Gönderdim” demeniz yeterli — ekibimiz kontrol edip hesabınızı aktif hale getirir.",
  "faq.q3": "Abonelik nasıl ödeniyor, kartla ödeme var mı?",
  "faq.a3": "Şu an ödemeler banka havalesi/EFT ile IBAN üzerinden alınıyor. Aylık plan 50 USD, yıllık plan 500 USD karşılığı olarak tahsil edilir.",
  "faq.q4": "Binance API anahtarımı vermek güvenli mi?",
  "faq.a4": "Anahtarınız sunucuda şifrelenerek saklanır ve yalnızca sizin adınıza emir açıp kapatmak için kullanılır. Binance tarafında “para çekme” (withdrawal) izni olmayan bir API anahtarı oluşturmanızı öneririz.",
  "faq.q5": "Hangi piyasalarda işlem yapılıyor?",
  "faq.a5": "Binance Futures (kripto vadeli işlemler), Borsa İstanbul ve ABD hisseleri (NASDAQ/NYSE/AMEX) — hepsi tek panelden taranır.",
  "faq.q6": "Sinyaller ne sıklıkta üretiliyor?",
  "faq.a6": "Sistem yaklaşık 15 dakikada bir otomatik tarama yapar; sinyaller yalnızca kapanmış 4 saatlik mumlardan üretilir, anlık fiyat gürültüsüne güvenilmez.",
  "faq.q7": "Telegram bildirimlerini nasıl açarım?",
  "faq.a7": "Panelde “Telegram Bağlantısı” bölümünden bir bağlantı kodu alıp Telegram'da botu başlatmanız yeterli — açılış/kapanış ve günlük özet bildirimleri otomatik gelir.",
  "faq.q8": "Açık bir pozisyonu acil kapatmam gerekirse ne yapmalıyım?",
  "faq.a8": "“Binance Gerçek Hesap” panelindeki ilgili pozisyonun yanındaki “Şimdi Kapat” butonunu kullanabilirsiniz; bu işlem anında gerçek bir market emri gönderip pozisyonu kapatır.",
  "faq.q9": "Başka bir sorum var, kime ulaşabilirim?",
  "faq.a9": "Yukarıdaki “Admin'e Soru Sor” butonuna tıklayıp mesajınızı yazmanız yeterli — doğrudan yönetici ekibine iletilir.",
  "panel.openPosition": "Açık pozisyon",
  "panel.loading": "Yükleniyor…",
  "pos.noOpenPosition": "Açık paper pozisyon yok. Sinyal oluştuğunda burada görünecek.",
  "pos.entry": "Giriş",
  "pos.current": "Güncel",
  "pos.unrealizedPnl": "Unrealized P&L",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "Güncel fiyat",
  "pos.trailingActive": "AKTİF @",
  "pos.trailingStandby": "beklemede",
  "pos.trailingLabel": "Trailing:",
  "pos.entryTimeLabel": "Giriş zamanı:",
  "panel.signalMatrix": "Sinyal matrisi",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "1D Volatilite",
  "signal.atrp1d": "1D ATRP %ile",
  "signal.final": "Son sinyal",
  "scanner.tabCrypto": "Binance Futures",
  "scanner.tabBist": "Borsa İstanbul",
  "scanner.tabUs": "Wall Street",
  "scanner.note": "USDT-M perpetual · 4H kapalı mum · sinyal amaçlı, gerçek emir yok",
  "scanner.coinSearchPlaceholder": "Coin ara (örn. BTC)",
  "scanner.stockSearchPlaceholderBist": "Hisse ara (örn. THYAO)",
  "scanner.stockSearchPlaceholderUs": "Hisse ara (örn. AAPL)",
  "scanner.allSignals": "Tüm sinyaller",
  "scanner.scanAll": "Tümünü tara",
  "scanner.scanBist": "Borsa İstanbul tara",
  "scanner.scanUs": "S&P500/Nasdaq-100 tara",
  "scanner.preparing": "Hazırlanıyor…",
  "scanner.waiting": "Tarama bekleniyor…",
  "scanner.noResults": "Sonuç yok.",
  "scanner.headerCoin": "Coin",
  "scanner.headerStock": "Hisse",
  "scanner.headerPrice": "Fiyat",
  "scanner.headerChange24h": "24s %",
  "scanner.headerChangeDaily": "Günlük %",
  "scanner.headerVolume": "Hacim",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP %ile",
  "scanner.headerSignal": "Sinyal",
  "scanner.headerReason": "Açıklama",
  "scanner.headerAdd": "Ekle",
  "scanner.coinCountSuffix": "coin",
  "scanner.stockCountSuffix": "hisse",
  "scanner.bistFootnote": "SHORT burada yalnızca stratejinin teknik sinyalidir; BIST spot piyasasında doğrudan açığa satış emri anlamına gelmez.",
  "scanner.usFootnote": "S&P 500 + Nasdaq-100 evreni (statik liste, periyodik güncellenmeli). Takip listesine eklenen ABD hisseleri, kripto watchlist'i gibi bağımsız bir paper pozisyon açar; SHORT taraf ödünç/marj kısıtlarını modellemeyen saf bir simülasyondur.",
  "scanner.scanning": "Tarama yapılıyor",
  "scanner.ready": "Hazır",
  "scanner.lastScan4h": "Son 4H tarama",
  "scanner.universe": "Evren",
  "scanner.error": "Hata",
  "scanner.unknownError": "Bilinmeyen hata",
  "scanner.startingScan": "Tarama başlatılıyor…",
  "scanner.startingUsScan": "Tarama başlatılıyor… (514 hisse, birkaç dakika sürebilir)",
  "scanner.startingBistScan": "Borsa İstanbul taraması başlatılıyor…",
  "tvChart.title": "TradingView Grafiği",
  "tvChart.noSymbol": "— sembol seçilmedi",
  "tvChart.emptyMessage": "Yukarıdaki tarama tablolarından bir satıra tıklayarak o sembolün TradingView grafiğini burada görüntüleyebilirsiniz.",
  "panel.paperTrades": "Deneme İşlemleri",
  "watchlist.headerSymbol": "Sembol",
  "watchlist.headerMarket": "Piyasa",
  "watchlist.headerDirection": "Yön / Sinyal",
  "watchlist.headerPrice": "Fiyat",
  "watchlist.headerUnrealizedPnl": "Unrealized P&L",
  "watchlist.headerAdded": "Eklenme",
  "watchlist.footnotePrefix": "Bir satıra tıklayarak o sembolün pozisyon ve sinyal detayını aşağıda görüntüleyebilirsiniz. Kripto sembolleri aynı strateji ile bağımsız bir paper pozisyon açar (boyut: $",
  "watchlist.footnoteSuffix": " nominal). Borsa İstanbul sembolleri yalnızca sinyal takibidir; gerçek/paper emir açılmaz.",
  "watchlist.loading": "Yükleniyor…",
  "watchlist.empty": "Takip listesi boş. Tarayıcıda LONG/SHORT veren bir sembole “+ Ekle” diyerek botun izlemesini/paper trade etmesini sağlayabilirsin.",
  "watchlist.mainEngine": "Ana motor",
  "watchlist.noPosition": "pozisyon yok",
  "watchlist.watched": "izleniyor",
  "watchlist.removeBtn": "Kaldır",
  "watchlist.addBtn": "+ Ekle",
  "watchlist.adding": "Ekleniyor…",
  "watchlist.added": "Eklendi ✓",
  "watchlist.symbolRemoved": "Bu sembol takip listesinden kaldırılmış olabilir.",
  "market.binance": "Binance",
  "market.bist": "Borsa İstanbul",
  "market.usStock": "ABD Hisse",
  "panel.binanceConnection": "Binance Bağlantısı",
  "panel.telegramConnection": "Telegram Bağlantısı",
  "panel.liveAccount": "Binance Gerçek Hesap",
  "liveAccount.title": "Canlı İşlem (Gerçek Para)",
  "liveAccount.openPositionsSuffix": "açık pozisyon",
  "liveAccount.liveOff": "Canlı işlem kapalı",
  "liveAccount.footnote": "Bu panel yalnızca <b>sizin</b> Binance hesabınızda gerçekleşen canlı işlemleri gösterir — yukarıdaki paper/demo panel ile veya başka kullanıcılarla karışmaz; sizden başka hiç kimse burayı göremez.",
  "panel.recentTrades": "Son işlemler",
  "history.headerDate": "Tarih",
  "history.headerDirection": "Yön",
  "history.headerSymbol": "Sembol",
  "history.headerEntry": "Giriş",
  "history.headerExit": "Çıkış",
  "history.headerPnl": "P&L",
  "history.headerReason": "Neden",
  "history.noClosedTrades": "Henüz kapanmış işlem yok.",
  "history.noClosedLiveTrades": "Henüz kapanmış canlı işleminiz yok.",
  "liveOpen.headerQty": "Miktar",
  "liveOpen.headerCurrent": "Güncel",
  "liveOpen.headerLeverage": "Kaldıraç",
  "liveOpen.headerOpened": "Açılış",
  "liveOpen.noOpenPositions": "Şu an açık canlı pozisyonunuz yok.",
  "liveOpen.closeNowBtn": "Şimdi Kapat",
  "liveOpen.closing": "Kapatılıyor…",
  "panel.aiAnalyst": "AI Trade Analisti",
  "ai.runBtn": "Şimdi Analiz Et",
  "ai.running": "Analiz ediliyor…",
  "ai.disabled": "AI Analist devre dışı — ANTHROPIC_API_KEY tanımlı değil.",
  "ai.noAnalysisYet": "Henüz bir analiz üretilmedi. “Şimdi Analiz Et” ile ilk raporu oluşturabilirsiniz.",
  "ai.lastAttemptFailed": "Son deneme başarısız oldu",
  "ai.tradesAnalyzedPrefix": "işlem incelendi (toplam",
  "footer.autoRefresh": "Otomatik yenileme: pozisyon 5 sn · tarayıcılar 10 sn · paper trading, gerçek emir yok.",
  "live.needConnectFirst": "Canlı (gerçek para) işlem açabilmek için önce yukarıdan Binance API anahtarınızı kaydedip doğrulatmanız gerekiyor.",
  "live.killSwitch": "🛑 Yönetici tarafından tüm canlı işlemler geçici olarak durduruldu",
  "live.pausedToday": "⏸ Günlük maksimum kayıp limitine ulaşıldı — bugün için yeni işlem açılmıyor",
  "live.on": "🟢 Canlı işlem AÇIK",
  "live.off": "Canlı işlem kapalı — bot sadece paper (deneme) modda çalışıyor",
  "live.openPositionCount": "Açık canlı pozisyon:",
  "live.todayRealizedPnl": "Bugünkü tahmini gerçekleşmiş K/Z:",
  "live.positionUsdLabel": "İşlem başına USD tutarı",
  "live.positionUsdPlaceholder": "Örn. 100",
  "live.maxLeverageLabel": "Maksimum kaldıraç (1-{n}x)",
  "live.maxLeveragePlaceholder": "Örn. 2",
  "live.dailyLossLimitLabel": "Günlük maksimum kayıp limiti (USD) — aşılırsa o gün otomatik durur",
  "live.dailyLossLimitPlaceholder": "Örn. 50",
  "live.maxPositionsLabel": "Maksimum açık pozisyon sayısı (1-{n})",
  "live.maxPositionsPlaceholder": "Örn. 1",
  "live.saveSettingsBtn": "Ayarları Kaydet",
  "live.turnOffBtn": "Canlı İşlemi Kapat",
  "live.turnOnBtn": "Canlı İşlemi AÇ (gerçek para)",
  "live.dangerText": "⚠️ Canlı işlem açıldığında bot, kayıtlı Binance hesabınızda <b>gerçek parayla</b> emir açar/kapatır. Kayıplardan bot değil siz sorumlusunuz. Bu, yatırım tavsiyesi değildir; ilgili düzenlemelere uygunluk sizin sorumluluğunuzdadır.",
  "live.toggleOnConfirm": "Canlı işlemi açmak üzeresiniz. Bot bu andan itibaren Binance hesabınızda GERÇEK PARA ile emir açıp kapatacak. Kayıp riskini kabul ettiğinizi ve bu ayarları doğru girdiğinizi onaylıyor musunuz?",
  "telegram.notEnabled": "Sunucuda Telegram botu tanımlı değil.",
  "telegram.linked": "🟢 Telegram bağlı",
  "telegram.notificationsDesc": "Canlı işlem giriş/çıkış bildirimleri, risk uyarıları ve günlük özet buraya gelecek.",
  "telegram.removeConnectionBtn": "Bağlantıyı Kaldır",
  "telegram.notLinkedDesc": "Canlı işlem bildirimlerinizi kendi Telegram'ınızda almak için bağlanın.",
  "telegram.getCodeBtn": "Bağlantı Kodu Al",
  "telegram.step1": "1) Telegram'da",
  "telegram.step1Fallback": "botumuzu",
  "telegram.step1End": "açın.",
  "telegram.step2": "2) Şunu gönderin:",
  "telegram.codeExpiresPrefix": "Kod",
  "telegram.minutes": "dakika",
  "telegram.seconds": "saniye içinde geçersiz olur.",
  "telegram.unlinkConfirm": "Telegram bağlantısını kaldırmak istediğinize emin misiniz?",
  "alert.apiKeySecretRequired": "API key ve secret gerekli.",
  "alert.riskAckRequired": "Devam etmeden önce risk onayı kutusunu işaretlemelisiniz.",
  "alert.saveFailedGeneric": "Kaydedilemedi",
  "alert.connectionError": "Bağlantı hatası",
  "alert.notAdded": "Eklenemedi",
  "alert.codeNotObtained": "Kod alınamadı",
  "alert.disconnectBinanceConfirm": "Binance bağlantısını kaldırmak istediğinize emin misiniz?",
  "alert.closePositionConfirmPrefix": "",
  "alert.closePositionConfirmSuffix": "pozisyonunu şimdi gerçek bir market emriyle kapatmak istediğinize emin misiniz? Bu işlem geri alınamaz.",
  "alert.closePositionFailed": "Pozisyon kapatılamadı",
  "alert.actionFailed": "İşlem başarısız"
},
  zh: {
  "nav.connecting": "连接中",
  "nav.botActive": "机器人运行中",
  "nav.standby": "待机",
  "nav.connectionError": "连接错误",
  "nav.admin": "管理",
  "nav.logout": "退出登录",
  "nav.language": "语言",
  "kpi.equity": "当前余额",
  "kpi.openPnl": "持仓盈亏",
  "kpi.openPnlSub": "所有持仓",
  "kpi.trades": "交易次数 · 胜率",
  "kpi.winRate": "胜率",
  "kpi.profitFactor": "盈亏比",
  "kpi.avg": "平均",
  "kpi.maxDrawdown": "最大回撤",
  "kpi.lastCandle": "最新K线：",
  "account.title": "我的账户",
  "account.subscriptionBtn": "💳 升级为订阅",
  "account.askAdminBtn": "✉️ 联系管理员",
  "account.emailNotRegistered": "（未登记邮箱）",
  "account.admin": "（管理员）",
  "account.trialDaysLeft": "试用：剩余 {n} 天",
  "account.trialExpired": "试用已到期",
  "account.credentialWarning": "⚠️ 服务器未配置 CREDENTIAL_ENCRYPTION_KEY — 无法安全加密，API 密钥无法保存。请联系您的管理员。",
  "account.savedKeyLabel": "已保存的密钥：",
  "account.lastVerified": "上次验证时间：",
  "account.riskAckGiven": "风险确认时间",
  "account.riskAckLabel": "我确认并接受：此机器人可能在加密货币期货交易中下单真实资金，存在亏损风险，任何损失由我自己承担，而非机器人责任。",
  "account.apiKeyLabel": "Binance API Key",
  "account.apiSecretLabel": "Binance API Secret",
  "account.apiKeyPlaceholderChange": "输入新密钥以更换",
  "account.apiKeyPlaceholderNew": "Binance 合约 API Key",
  "account.apiSecretPlaceholderChange": "输入新 Secret 以更换",
  "account.apiSecretPlaceholderNew": "Binance 合约 API Secret",
  "account.saveVerifyBtn": "保存并验证",
  "account.savingVerifying": "正在保存并验证…",
  "account.removeConnectionBtn": "解除连接",
  "account.verifyError": "验证错误",
  "account.verifiedConnected": "已连接并已验证",
  "account.connectedNotVerified": "已连接，尚未验证",
  "account.notConnected": "未连接",
  "subscription.title": "升级为订阅",
  "subscription.desc": "选择一个套餐，将款项汇入下面的 IBAN，然后点击“我已付款”——我们的团队将尽快核实您的付款并激活您的账户。",
  "subscription.planMonthlyName": "月订阅",
  "subscription.planMonthlyPrice": "50 美元",
  "subscription.planAnnualName": "年订阅",
  "subscription.planAnnualPrice": "500 美元",
  "subscription.selectedPlanLabel": "已选套餐：",
  "subscription.ibanLabel": "IBAN：",
  "subscription.recipientLabel": "收款人：",
  "subscription.usernameNote": "在付款备注中填写您的用户名（<b>__USERNAME__</b>）可加快审核。",
  "subscription.paySentBtn": "我已付款",
  "subscription.sending": "发送中…",
  "subscription.resendBtn": "再次通知",
  "subscription.successMsg": "✓ 已收到通知 — 核实付款后团队将激活您的账户。",
  "subscription.genericError": "发生错误，请重试。",
  "subscription.connectionError": "连接错误，请重试。",
  "subscription.selectedMonthly": "月度 — 50 美元",
  "subscription.selectedAnnual": "年度 — 500 美元",
  "askAdmin.title": "联系管理员",
  "askAdmin.desc": "您的消息将发送到 herobotai.int@gmail.com。",
  "askAdmin.placeholder": "在此输入您的问题…",
  "askAdmin.sendBtn": "发送",
  "askAdmin.sending": "发送中…",
  "askAdmin.emptyMessage": "请输入消息内容。",
  "askAdmin.successMsg": "✓ 您的消息已发送，我们将尽快回复。",
  "askAdmin.genericError": "发送失败，请重试。",
  "askAdmin.connectionError": "连接错误，请重试。",
  "faq.title": "常见问题（FAQ）",
  "faq.q1": "这个机器人会用真实资金交易吗？",
  "faq.a1": "默认不会 — 系统默认运行在模拟/演示模式，不会下达任何真实订单。如需用真实资金交易，需要您自己在面板中绑定 Binance API 密钥并开启“实盘交易”。",
  "faq.q2": "免费试用期多长，到期后会怎样？",
  "faq.a2": "试用期为 7 天。到期后您对面板的访问将受到限制；如需继续使用，只需在此处选择套餐并向 IBAN 付款，然后点击“我已付款”，我们的团队将审核并激活您的账户。",
  "faq.q3": "订阅费如何支付，支持刷卡吗？",
  "faq.a3": "目前仅支持通过银行转账/EFT 向 IBAN 付款。月度套餐收费 50 美元，年度套餐收费 500 美元。",
  "faq.q4": "提供我的 Binance API 密钥安全吗？",
  "faq.a4": "您的密钥会在服务器上加密存储，仅用于代您开平订单。建议您在 Binance 上创建一个不带“提现”权限的 API 密钥。",
  "faq.q5": "交易哪些市场？",
  "faq.a5": "Binance 合约（加密货币永续合约）、伊斯坦布尔交易所以及美股（NASDAQ/NYSE/AMEX）— 均可在同一面板中扫描。",
  "faq.q6": "信号多久生成一次？",
  "faq.a6": "系统大约每 15 分钟自动扫描一次；信号仅基于已收盘的4小时K线生成，不会受瞬时价格噪声影响。",
  "faq.q7": "如何开启Telegram通知？",
  "faq.a7": "只需在面板的“Telegram 连接”部分获取绑定代码，并在 Telegram 中启动机器人 — 开仓/平仓及每日汇总通知将自动到达。",
  "faq.q8": "如需紧急平仓已开仓位，该怎么做？",
  "faq.a8": "可在“Binance 实盘账户”面板中对应仓位旁点击“立即平仓”按钮；这将立即发送一笔真实市价单并平仓。",
  "faq.q9": "我还有其他问题，应联系谁？",
  "faq.a9": "只需点击上方的“联系管理员”按钮并写下您的消息 — 将直接发送给管理团队。",
  "panel.openPosition": "持仓",
  "panel.loading": "加载中…",
  "pos.noOpenPosition": "当前无模拟持仓。信号生成后将在此显示。",
  "pos.entry": "入场价",
  "pos.current": "当前价",
  "pos.unrealizedPnl": "未实现盈亏",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "当前价格",
  "pos.trailingActive": "已启用 @",
  "pos.trailingStandby": "未启用",
  "pos.trailingLabel": "移动止损：",
  "pos.entryTimeLabel": "入场时间：",
  "panel.signalMatrix": "信号矩阵",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "日波动率",
  "signal.atrp1d": "日ATRP百分位",
  "signal.final": "最终信号",
  "scanner.tabCrypto": "Binance 合约",
  "scanner.tabBist": "伊斯坦布尔交易所",
  "scanner.tabUs": "美股",
  "scanner.note": "USDT-M 永续合约 · 4小时已收盘K线 · 仅供信号参考，无真实订单",
  "scanner.coinSearchPlaceholder": "搜索币种（如 BTC）",
  "scanner.stockSearchPlaceholderBist": "搜索股票（如 THYAO）",
  "scanner.stockSearchPlaceholderUs": "搜索股票（如 AAPL）",
  "scanner.allSignals": "全部信号",
  "scanner.scanAll": "扫描全部",
  "scanner.scanBist": "扫描伊斯坦布尔交易所",
  "scanner.scanUs": "扫描 S&P500/纳斯达克100",
  "scanner.preparing": "准备中…",
  "scanner.waiting": "等待扫描…",
  "scanner.noResults": "无结果。",
  "scanner.headerCoin": "币种",
  "scanner.headerStock": "股票",
  "scanner.headerPrice": "价格",
  "scanner.headerChange24h": "24小时涨跌%",
  "scanner.headerChangeDaily": "日涨跌%",
  "scanner.headerVolume": "成交量",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP百分位",
  "scanner.headerSignal": "信号",
  "scanner.headerReason": "说明",
  "scanner.headerAdd": "添加",
  "scanner.coinCountSuffix": "个币种",
  "scanner.stockCountSuffix": "只股票",
  "scanner.bistFootnote": "此处的 SHORT 仅为策略的技术信号；并不意味着在 BIST 现货市场直接下达卖空订单。",
  "scanner.usFootnote": "S&P 500 + 纳斯达克100 股票池（静态列表，应定期更新）。添加到自选列表的美股会像加密自选列表一样开立独立的模拟仓位；SHORT 仅为纯模拟，不考虑借券/保证金限制。",
  "scanner.scanning": "正在扫描",
  "scanner.ready": "已就绪",
  "scanner.lastScan4h": "上次 4H 扫描",
  "scanner.universe": "标的范围",
  "scanner.error": "错误",
  "scanner.unknownError": "未知错误",
  "scanner.startingScan": "正在启动扫描…",
  "scanner.startingUsScan": "正在启动扫描…（514 只股票，可能需要几分钟）",
  "scanner.startingBistScan": "正在启动伊斯坦布尔交易所扫描…",
  "tvChart.title": "TradingView 图表",
  "tvChart.noSymbol": "— 未选择交易对",
  "tvChart.emptyMessage": "点击上方扫描表格中的一行，即可在此处查看该交易对的 TradingView 图表。",
  "panel.paperTrades": "模拟交易",
  "watchlist.headerSymbol": "交易对",
  "watchlist.headerMarket": "市场",
  "watchlist.headerDirection": "方向 / 信号",
  "watchlist.headerPrice": "价格",
  "watchlist.headerUnrealizedPnl": "未实现盈亏",
  "watchlist.headerAdded": "添加时间",
  "watchlist.footnotePrefix": "点击一行即可在下方查看该交易对的仓位及信号详情。加密交易对会以相同策略开立独立的模拟仓位（仓位规模：$",
  "watchlist.footnoteSuffix": " 名义本金）。伊斯坦布尔交易对仅用于信号跟踪，不会开立真实/模拟订单。",
  "watchlist.loading": "加载中…",
  "watchlist.empty": "自选列表为空。在扫描列表中对显示 LONG/SHORT 的交易对点击“+ 添加”，即可让机器人对其进行跟踪/模拟交易。",
  "watchlist.mainEngine": "主引擎",
  "watchlist.noPosition": "无仓位",
  "watchlist.watched": "跟踪中",
  "watchlist.removeBtn": "移除",
  "watchlist.addBtn": "+ 添加",
  "watchlist.adding": "添加中…",
  "watchlist.added": "已添加 ✓",
  "watchlist.symbolRemoved": "该交易对可能已从自选列表中移除。",
  "market.binance": "Binance",
  "market.bist": "伊斯坦布尔交易所",
  "market.usStock": "美股",
  "panel.binanceConnection": "Binance 连接",
  "panel.telegramConnection": "Telegram 连接",
  "panel.liveAccount": "Binance 实盘账户",
  "liveAccount.title": "实盘交易（真实资金）",
  "liveAccount.openPositionsSuffix": "个持仓",
  "liveAccount.liveOff": "实盘交易已关闭",
  "liveAccount.footnote": "此面板仅显示 <b>您自己</b> Binance 账户中的实盘交易 — 不会与上方的模拟面板或其他用户混淆；除了您本人之外，任何人都无法查看此处。",
  "panel.recentTrades": "最近交易",
  "history.headerDate": "日期",
  "history.headerDirection": "方向",
  "history.headerSymbol": "交易对",
  "history.headerEntry": "入场价",
  "history.headerExit": "出场价",
  "history.headerPnl": "盈亏",
  "history.headerReason": "原因",
  "history.noClosedTrades": "暂无已平仓交易。",
  "history.noClosedLiveTrades": "暂无已平仓的实盘交易。",
  "liveOpen.headerQty": "数量",
  "liveOpen.headerCurrent": "当前价",
  "liveOpen.headerLeverage": "杠杆",
  "liveOpen.headerOpened": "开仓时间",
  "liveOpen.noOpenPositions": "您当前没有开启的实盘仓位。",
  "liveOpen.closeNowBtn": "立即平仓",
  "liveOpen.closing": "平仓中…",
  "panel.aiAnalyst": "AI 交易分析师",
  "ai.runBtn": "立即分析",
  "ai.running": "分析中…",
  "ai.disabled": "AI 分析师已禁用 — 未设置 ANTHROPIC_API_KEY。",
  "ai.noAnalysisYet": "尚未生成分析。点击“立即分析”生成第一份报告。",
  "ai.lastAttemptFailed": "上次尝试失败",
  "ai.tradesAnalyzedPrefix": "笔交易已分析（共计",
  "footer.autoRefresh": "自动刷新：仓位 5 秒 · 扫描器 10 秒 · 模拟交易，无真实订单。",
  "live.needConnectFirst": "您需要先在上方保存并验证 Binance API 密钥，才能开始实盘（真实资金）交易。",
  "live.killSwitch": "🛑 管理员已暂时停止所有实盘交易",
  "live.pausedToday": "⏸ 已达到每日最大亏损限额 — 今日不会开启新交易",
  "live.on": "🟢 实盘交易已开启",
  "live.off": "实盘交易已关闭 — 机器人仅在模拟（演示）模式下运行",
  "live.openPositionCount": "实盘持仓数：",
  "live.todayRealizedPnl": "今日预估已实现盈亏：",
  "live.positionUsdLabel": "每笔交易金额（美元）",
  "live.positionUsdPlaceholder": "例如 100",
  "live.maxLeverageLabel": "最大杠杆倍数（1-{n}倍）",
  "live.maxLeveragePlaceholder": "例如 2",
  "live.dailyLossLimitLabel": "每日最大亏损限额（美元）— 超过则当日自动停止交易",
  "live.dailyLossLimitPlaceholder": "例如 50",
  "live.maxPositionsLabel": "最大持仓数（1-{n}）",
  "live.maxPositionsPlaceholder": "例如 1",
  "live.saveSettingsBtn": "保存设置",
  "live.turnOffBtn": "关闭实盘交易",
  "live.turnOnBtn": "开启实盘交易（真实资金）",
  "live.dangerText": "⚠️ 开启实盘交易后，机器人将在您绑定的 Binance 账户上用<b>真实资金</b>开平订单。任何亏损由您本人而非机器人承担。本工具不构成投资建议；遵守相关监管规定为您自己的责任。",
  "live.toggleOnConfirm": "您即将开启实盘交易。从此刻起，机器人将在您的 Binance 账户上用真实资金开平订单。您确认接受亏损风险且以上设置正确吗？",
  "telegram.notEnabled": "服务器未配置 Telegram 机器人。",
  "telegram.linked": "🟢 Telegram 已连接",
  "telegram.notificationsDesc": "实盘开/平仓通知、风险提醒以及每日汇总将发送至此。",
  "telegram.removeConnectionBtn": "解除连接",
  "telegram.notLinkedDesc": "连接后可在您自己的 Telegram 中接收实盘交易通知。",
  "telegram.getCodeBtn": "获取绑定代码",
  "telegram.step1": "1) 在 Telegram 中打开",
  "telegram.step1Fallback": "我们的机器人",
  "telegram.step1End": "。",
  "telegram.step2": "2) 发送以下内容：",
  "telegram.codeExpiresPrefix": "验证码将在",
  "telegram.minutes": "分",
  "telegram.seconds": "秒后失效。",
  "telegram.unlinkConfirm": "确定要解除 Telegram 连接吗？",
  "alert.apiKeySecretRequired": "需要填写 API Key 和 Secret。",
  "alert.riskAckRequired": "继续之前请勾选风险确认框。",
  "alert.saveFailedGeneric": "保存失败",
  "alert.connectionError": "连接错误",
  "alert.notAdded": "添加失败",
  "alert.codeNotObtained": "无法获取验证码",
  "alert.disconnectBinanceConfirm": "确定要解除 Binance 连接吗？",
  "alert.closePositionConfirmPrefix": "确定要立即以真实市价单平仓",
  "alert.closePositionConfirmSuffix": "吗？此操作不可撤销。",
  "alert.closePositionFailed": "平仓失败",
  "alert.actionFailed": "操作失败"
},
  de: {
  "nav.connecting": "Verbindung wird hergestellt",
  "nav.botActive": "Bot aktiv",
  "nav.standby": "Standby",
  "nav.connectionError": "Verbindungsfehler",
  "nav.admin": "Verwaltung",
  "nav.logout": "Abmelden",
  "nav.language": "Sprache",
  "kpi.equity": "Aktueller Kontostand",
  "kpi.openPnl": "Offene Positionen P&L",
  "kpi.openPnlSub": "Alle offenen Positionen",
  "kpi.trades": "Trades · Trefferquote",
  "kpi.winRate": "Trefferquote",
  "kpi.profitFactor": "Profit-Faktor",
  "kpi.avg": "Durchschn.",
  "kpi.maxDrawdown": "Max. Drawdown",
  "kpi.lastCandle": "Letzte Kerze:",
  "account.title": "Mein Konto",
  "account.subscriptionBtn": "💳 Zum Abo wechseln",
  "account.askAdminBtn": "✉️ Admin fragen",
  "account.emailNotRegistered": "(keine E-Mail hinterlegt)",
  "account.admin": "(Admin)",
  "account.trialDaysLeft": "Testphase: noch {n} Tage",
  "account.trialExpired": "Testphase abgelaufen",
  "account.credentialWarning": "⚠️ CREDENTIAL_ENCRYPTION_KEY ist auf dem Server nicht konfiguriert — API-Schlüssel können nicht sicher verschlüsselt und daher nicht gespeichert werden. Bitte wenden Sie sich an Ihren Administrator.",
  "account.savedKeyLabel": "Gespeicherter Schlüssel:",
  "account.lastVerified": "Zuletzt verifiziert:",
  "account.riskAckGiven": "Risikobestätigung erteilt am",
  "account.riskAckLabel": "Ich bestätige und akzeptiere, dass dieser Bot Orders mit echtem Geld im Krypto-Futures-Handel platzieren kann, dass dies ein Verlustrisiko birgt und dass ich selbst — nicht der Bot — für etwaige Verluste verantwortlich bin.",
  "account.apiKeyLabel": "Binance API Key",
  "account.apiSecretLabel": "Binance API Secret",
  "account.apiKeyPlaceholderChange": "Neuen Key eingeben, um ihn zu ändern",
  "account.apiKeyPlaceholderNew": "Binance Futures API Key",
  "account.apiSecretPlaceholderChange": "Neues Secret eingeben, um es zu ändern",
  "account.apiSecretPlaceholderNew": "Binance Futures API Secret",
  "account.saveVerifyBtn": "Speichern & Verifizieren",
  "account.savingVerifying": "Wird gespeichert & verifiziert…",
  "account.removeConnectionBtn": "Verbindung entfernen",
  "account.verifyError": "Verifizierungsfehler",
  "account.verifiedConnected": "Verbunden und verifiziert",
  "account.connectedNotVerified": "Verbunden, nicht verifiziert",
  "account.notConnected": "Nicht verbunden",
  "subscription.title": "Zum Abo wechseln",
  "subscription.desc": "Wählen Sie einen Plan, überweisen Sie den Betrag an die untenstehende IBAN und klicken Sie auf „Betrag überwiesen“ — unser Team prüft Ihre Zahlung und aktiviert Ihr Konto so schnell wie möglich.",
  "subscription.planMonthlyName": "Monatsabo",
  "subscription.planMonthlyPrice": "50 USD",
  "subscription.planAnnualName": "Jahresabo",
  "subscription.planAnnualPrice": "500 USD",
  "subscription.selectedPlanLabel": "Gewählter Plan:",
  "subscription.ibanLabel": "IBAN:",
  "subscription.recipientLabel": "Empfänger:",
  "subscription.usernameNote": "Wenn Sie Ihren Benutzernamen (<b>__USERNAME__</b>) im Verwendungszweck angeben, beschleunigt das die Prüfung.",
  "subscription.paySentBtn": "Betrag überwiesen",
  "subscription.sending": "Wird gesendet…",
  "subscription.resendBtn": "Erneut melden",
  "subscription.successMsg": "✓ Meldung erhalten — unser Team aktiviert Ihr Konto, sobald die Zahlung bestätigt ist.",
  "subscription.genericError": "Etwas ist schiefgelaufen, bitte versuchen Sie es erneut.",
  "subscription.connectionError": "Verbindungsfehler, bitte versuchen Sie es erneut.",
  "subscription.selectedMonthly": "Monatlich — 50 USD",
  "subscription.selectedAnnual": "Jährlich — 500 USD",
  "askAdmin.title": "Admin fragen",
  "askAdmin.desc": "Ihre Nachricht wird an herobotai.int@gmail.com gesendet.",
  "askAdmin.placeholder": "Schreiben Sie hier Ihre Frage…",
  "askAdmin.sendBtn": "Senden",
  "askAdmin.sending": "Wird gesendet…",
  "askAdmin.emptyMessage": "Bitte geben Sie eine Nachricht ein.",
  "askAdmin.successMsg": "✓ Ihre Nachricht wurde gesendet. Wir melden uns in Kürze bei Ihnen.",
  "askAdmin.genericError": "Konnte nicht gesendet werden, bitte versuchen Sie es erneut.",
  "askAdmin.connectionError": "Verbindungsfehler, bitte versuchen Sie es erneut.",
  "faq.title": "FAQ — Häufig gestellte Fragen",
  "faq.q1": "Handelt dieser Bot mit echtem Geld?",
  "faq.a1": "Standardmäßig nein — das System läuft im Paper-/Demo-Modus und sendet keine echten Orders. Um mit echtem Geld zu handeln, müssen Sie Ihren Binance-API-Schlüssel verbinden und „Live-Handel“ selbst in Ihrem Panel aktivieren.",
  "faq.q2": "Wie lange läuft die kostenlose Testphase und was passiert danach?",
  "faq.a2": "7 Tage. Nach Ablauf wird Ihr Zugriff auf das Panel eingeschränkt; um fortzufahren, wählen Sie hier einfach einen Plan, zahlen an die IBAN und klicken auf „Betrag überwiesen“ — unser Team prüft dies und aktiviert Ihr Konto.",
  "faq.q3": "Wie wird das Abo bezahlt, gibt es Kartenzahlung?",
  "faq.a3": "Derzeit werden Zahlungen per Banküberweisung/EFT an die IBAN entgegengenommen. Der Monatsplan kostet 50 USD, der Jahresplan 500 USD.",
  "faq.q4": "Ist es sicher, meinen Binance-API-Schlüssel anzugeben?",
  "faq.a4": "Ihr Schlüssel wird verschlüsselt auf dem Server gespeichert und nur verwendet, um in Ihrem Namen Orders zu öffnen und zu schließen. Wir empfehlen, auf Binance-Seite einen API-Schlüssel ohne „Auszahlungs“-Berechtigung zu erstellen.",
  "faq.q5": "An welchen Märkten wird gehandelt?",
  "faq.a5": "Binance Futures (Krypto-Perpetuals), Borsa Istanbul sowie US-Aktien (NASDAQ/NYSE/AMEX) — alle werden von einem einzigen Panel aus gescannt.",
  "faq.q6": "Wie oft werden Signale generiert?",
  "faq.a6": "Das System führt etwa alle 15 Minuten einen automatischen Scan durch; Signale werden nur aus geschlossenen 4-Stunden-Kerzen generiert, nicht aus kurzfristigem Preisrauschen.",
  "faq.q7": "Wie aktiviere ich Telegram-Benachrichtigungen?",
  "faq.a7": "Holen Sie sich einfach einen Verknüpfungscode im Bereich „Telegram-Verbindung“ im Panel und starten Sie den Bot in Telegram — Öffnungs-/Schließungs- und tägliche Zusammenfassungen kommen dann automatisch an.",
  "faq.q8": "Was soll ich tun, wenn ich eine offene Position dringend schließen muss?",
  "faq.a8": "Sie können die Schaltfläche „Jetzt schließen“ neben der betreffenden Position im Panel „Binance Live-Konto“ verwenden; dies sendet sofort eine echte Market-Order und schließt die Position.",
  "faq.q9": "Ich habe eine andere Frage, an wen kann ich mich wenden?",
  "faq.a9": "Klicken Sie einfach oben auf „Admin fragen“ und schreiben Sie Ihre Nachricht — sie geht direkt an das Admin-Team.",
  "panel.openPosition": "Offene Position",
  "panel.loading": "Wird geladen…",
  "pos.noOpenPosition": "Keine offene Paper-Position. Erscheint hier, sobald ein Signal generiert wird.",
  "pos.entry": "Einstieg",
  "pos.current": "Aktuell",
  "pos.unrealizedPnl": "Unrealized P&L",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "Aktueller Preis",
  "pos.trailingActive": "AKTIV @",
  "pos.trailingStandby": "inaktiv",
  "pos.trailingLabel": "Trailing:",
  "pos.entryTimeLabel": "Einstiegszeit:",
  "panel.signalMatrix": "Signalmatrix",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "1T-Volatilität",
  "signal.atrp1d": "1T ATRP-Perzentil",
  "signal.final": "Finales Signal",
  "scanner.tabCrypto": "Binance Futures",
  "scanner.tabBist": "Borsa Istanbul",
  "scanner.tabUs": "Wall Street",
  "scanner.note": "USDT-M Perpetual · geschlossene 4H-Kerze · nur zu Signalzwecken, keine echten Orders",
  "scanner.coinSearchPlaceholder": "Coin suchen (z. B. BTC)",
  "scanner.stockSearchPlaceholderBist": "Aktie suchen (z. B. THYAO)",
  "scanner.stockSearchPlaceholderUs": "Aktie suchen (z. B. AAPL)",
  "scanner.allSignals": "Alle Signale",
  "scanner.scanAll": "Alle scannen",
  "scanner.scanBist": "Borsa Istanbul scannen",
  "scanner.scanUs": "S&P500/Nasdaq-100 scannen",
  "scanner.preparing": "Wird vorbereitet…",
  "scanner.waiting": "Warte auf Scan…",
  "scanner.noResults": "Keine Ergebnisse.",
  "scanner.headerCoin": "Coin",
  "scanner.headerStock": "Aktie",
  "scanner.headerPrice": "Preis",
  "scanner.headerChange24h": "24h %",
  "scanner.headerChangeDaily": "Tages-%",
  "scanner.headerVolume": "Volumen",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP-Perzentil",
  "scanner.headerSignal": "Signal",
  "scanner.headerReason": "Beschreibung",
  "scanner.headerAdd": "Hinzufügen",
  "scanner.coinCountSuffix": "Coins",
  "scanner.stockCountSuffix": "Aktien",
  "scanner.bistFootnote": "SHORT ist hier nur das technische Signal der Strategie; es bedeutet keine direkte Leerverkaufsorder am BIST-Kassamarkt.",
  "scanner.usFootnote": "S&P-500- + Nasdaq-100-Universum (statische Liste, sollte regelmäßig aktualisiert werden). Zur Watchlist hinzugefügte US-Aktien eröffnen wie die Krypto-Watchlist eine unabhängige Paper-Position; die SHORT-Seite ist eine reine Simulation ohne Modellierung von Leih-/Margin-Beschränkungen.",
  "scanner.scanning": "Scan läuft",
  "scanner.ready": "Bereit",
  "scanner.lastScan4h": "Letzter 4H-Scan",
  "scanner.universe": "Universum",
  "scanner.error": "Fehler",
  "scanner.unknownError": "Unbekannter Fehler",
  "scanner.startingScan": "Scan wird gestartet…",
  "scanner.startingUsScan": "Scan wird gestartet… (514 Aktien, kann einige Minuten dauern)",
  "scanner.startingBistScan": "Borsa-Istanbul-Scan wird gestartet…",
  "tvChart.title": "TradingView-Chart",
  "tvChart.noSymbol": "— kein Symbol ausgewählt",
  "tvChart.emptyMessage": "Klicken Sie auf eine Zeile in den obigen Scan-Tabellen, um hier den TradingView-Chart dieses Symbols anzuzeigen.",
  "panel.paperTrades": "Paper-Trades",
  "watchlist.headerSymbol": "Symbol",
  "watchlist.headerMarket": "Markt",
  "watchlist.headerDirection": "Richtung / Signal",
  "watchlist.headerPrice": "Preis",
  "watchlist.headerUnrealizedPnl": "Unrealized P&L",
  "watchlist.headerAdded": "Hinzugefügt",
  "watchlist.footnotePrefix": "Klicken Sie auf eine Zeile, um unten die Positions- und Signaldetails dieses Symbols zu sehen. Krypto-Symbole eröffnen mit derselben Strategie eine unabhängige Paper-Position (Größe: $",
  "watchlist.footnoteSuffix": " nominal). Borsa-Istanbul-Symbole dienen nur der Signalbeobachtung; es wird keine echte/Paper-Order eröffnet.",
  "watchlist.loading": "Wird geladen…",
  "watchlist.empty": "Die Watchlist ist leer. Klicken Sie bei einem Symbol mit LONG/SHORT im Scanner auf „+ Hinzufügen“, damit der Bot es beobachtet/als Paper-Trade führt.",
  "watchlist.mainEngine": "Haupt-Engine",
  "watchlist.noPosition": "keine Position",
  "watchlist.watched": "beobachtet",
  "watchlist.removeBtn": "Entfernen",
  "watchlist.addBtn": "+ Hinzufügen",
  "watchlist.adding": "Wird hinzugefügt…",
  "watchlist.added": "Hinzugefügt ✓",
  "watchlist.symbolRemoved": "Dieses Symbol wurde möglicherweise von der Watchlist entfernt.",
  "market.binance": "Binance",
  "market.bist": "Borsa Istanbul",
  "market.usStock": "US-Aktie",
  "panel.binanceConnection": "Binance-Verbindung",
  "panel.telegramConnection": "Telegram-Verbindung",
  "panel.liveAccount": "Binance Live-Konto",
  "liveAccount.title": "Live-Handel (Echtgeld)",
  "liveAccount.openPositionsSuffix": "offene Positionen",
  "liveAccount.liveOff": "Live-Handel ist deaktiviert",
  "liveAccount.footnote": "Dieses Panel zeigt ausschließlich Live-Trades auf <b>Ihrem eigenen</b> Binance-Konto — es wird nie mit dem obigen Paper-/Demo-Panel oder mit anderen Nutzern vermischt; niemand außer Ihnen kann dies sehen.",
  "panel.recentTrades": "Letzte Trades",
  "history.headerDate": "Datum",
  "history.headerDirection": "Richtung",
  "history.headerSymbol": "Symbol",
  "history.headerEntry": "Einstieg",
  "history.headerExit": "Ausstieg",
  "history.headerPnl": "P&L",
  "history.headerReason": "Grund",
  "history.noClosedTrades": "Noch keine geschlossenen Trades.",
  "history.noClosedLiveTrades": "Sie haben noch keine geschlossenen Live-Trades.",
  "liveOpen.headerQty": "Menge",
  "liveOpen.headerCurrent": "Aktuell",
  "liveOpen.headerLeverage": "Hebel",
  "liveOpen.headerOpened": "Eröffnet",
  "liveOpen.noOpenPositions": "Sie haben derzeit keine offenen Live-Positionen.",
  "liveOpen.closeNowBtn": "Jetzt schließen",
  "liveOpen.closing": "Wird geschlossen…",
  "panel.aiAnalyst": "KI-Trade-Analyst",
  "ai.runBtn": "Jetzt analysieren",
  "ai.running": "Wird analysiert…",
  "ai.disabled": "KI-Analyst ist deaktiviert — ANTHROPIC_API_KEY ist nicht gesetzt.",
  "ai.noAnalysisYet": "Noch keine Analyse erstellt. Klicken Sie auf „Jetzt analysieren“, um den ersten Bericht zu erstellen.",
  "ai.lastAttemptFailed": "Letzter Versuch fehlgeschlagen",
  "ai.tradesAnalyzedPrefix": "Trades analysiert (insgesamt",
  "footer.autoRefresh": "Auto-Aktualisierung: Position 5 s · Scanner 10 s · Paper-Trading, keine echten Orders.",
  "live.needConnectFirst": "Sie müssen zuerst oben Ihren Binance-API-Schlüssel speichern und verifizieren, bevor Sie den Live-Handel (Echtgeld) starten können.",
  "live.killSwitch": "🛑 Der gesamte Live-Handel wurde vom Administrator vorübergehend gestoppt",
  "live.pausedToday": "⏸ Tägliches Verlustlimit erreicht — heute werden keine neuen Trades eröffnet",
  "live.on": "🟢 Live-Handel ist AN",
  "live.off": "Live-Handel ist deaktiviert — der Bot läuft nur im Paper-(Demo-)Modus",
  "live.openPositionCount": "Offene Live-Positionen:",
  "live.todayRealizedPnl": "Heutiger geschätzter realisierter G/V:",
  "live.positionUsdLabel": "USD-Betrag pro Trade",
  "live.positionUsdPlaceholder": "z. B. 100",
  "live.maxLeverageLabel": "Max. Hebel (1-{n}x)",
  "live.maxLeveragePlaceholder": "z. B. 2",
  "live.dailyLossLimitLabel": "Tägliches Verlustlimit (USD) — der Handel stoppt automatisch für den Tag, wenn überschritten",
  "live.dailyLossLimitPlaceholder": "z. B. 50",
  "live.maxPositionsLabel": "Max. offene Positionen (1-{n})",
  "live.maxPositionsPlaceholder": "z. B. 1",
  "live.saveSettingsBtn": "Einstellungen speichern",
  "live.turnOffBtn": "Live-Handel ausschalten",
  "live.turnOnBtn": "Live-Handel EINSCHALTEN (Echtgeld)",
  "live.dangerText": "⚠️ Sobald der Live-Handel aktiviert ist, eröffnet/schließt der Bot Orders mit <b>echtem Geld</b> auf Ihrem registrierten Binance-Konto. Sie — nicht der Bot — sind für etwaige Verluste verantwortlich. Dies ist keine Anlageberatung; die Einhaltung geltender Vorschriften liegt in Ihrer eigenen Verantwortung.",
  "live.toggleOnConfirm": "Sie sind dabei, den Live-Handel zu aktivieren. Ab sofort eröffnet und schließt der Bot Orders mit ECHTEM GELD auf Ihrem Binance-Konto. Bestätigen Sie, dass Sie das Verlustrisiko akzeptieren und diese Einstellungen korrekt sind?",
  "telegram.notEnabled": "Der Telegram-Bot ist auf dem Server nicht konfiguriert.",
  "telegram.linked": "🟢 Telegram verbunden",
  "telegram.notificationsDesc": "Benachrichtigungen über Live-Trade-Eröffnungen/-Schließungen, Risikowarnungen und die tägliche Zusammenfassung erscheinen hier.",
  "telegram.removeConnectionBtn": "Verbindung entfernen",
  "telegram.notLinkedDesc": "Verbinden Sie sich, um Ihre Live-Trade-Benachrichtigungen in Ihrem eigenen Telegram zu erhalten.",
  "telegram.getCodeBtn": "Verknüpfungscode erhalten",
  "telegram.step1": "1) Öffnen Sie",
  "telegram.step1Fallback": "unseren Bot",
  "telegram.step1End": "in Telegram.",
  "telegram.step2": "2) Senden Sie Folgendes:",
  "telegram.codeExpiresPrefix": "Der Code läuft in",
  "telegram.minutes": "Min.",
  "telegram.seconds": "Sek. ab.",
  "telegram.unlinkConfirm": "Sind Sie sicher, dass Sie die Telegram-Verbindung entfernen möchten?",
  "alert.apiKeySecretRequired": "API-Key und Secret sind erforderlich.",
  "alert.riskAckRequired": "Sie müssen die Risikobestätigung ankreuzen, bevor Sie fortfahren können.",
  "alert.saveFailedGeneric": "Konnte nicht gespeichert werden",
  "alert.connectionError": "Verbindungsfehler",
  "alert.notAdded": "Konnte nicht hinzugefügt werden",
  "alert.codeNotObtained": "Code konnte nicht abgerufen werden",
  "alert.disconnectBinanceConfirm": "Sind Sie sicher, dass Sie die Binance-Verbindung entfernen möchten?",
  "alert.closePositionConfirmPrefix": "Sind Sie sicher, dass Sie die Position",
  "alert.closePositionConfirmSuffix": "jetzt mit einer echten Market-Order schließen möchten? Dies kann nicht rückgängig gemacht werden.",
  "alert.closePositionFailed": "Position konnte nicht geschlossen werden",
  "alert.actionFailed": "Aktion fehlgeschlagen"
},
  fr: {
  "nav.connecting": "Connexion en cours",
  "nav.botActive": "Bot actif",
  "nav.standby": "En attente",
  "nav.connectionError": "Erreur de connexion",
  "nav.admin": "Administration",
  "nav.logout": "Déconnexion",
  "nav.language": "Langue",
  "kpi.equity": "Solde actuel",
  "kpi.openPnl": "P&L positions ouvertes",
  "kpi.openPnlSub": "Toutes les positions ouvertes",
  "kpi.trades": "Trades · taux de réussite",
  "kpi.winRate": "Taux de réussite",
  "kpi.profitFactor": "Facteur de profit",
  "kpi.avg": "Moy.",
  "kpi.maxDrawdown": "Drawdown max.",
  "kpi.lastCandle": "Dernière bougie :",
  "account.title": "Mon compte",
  "account.subscriptionBtn": "💳 Passer à l'abonnement",
  "account.askAdminBtn": "✉️ Contacter l'admin",
  "account.emailNotRegistered": "(aucun e-mail enregistré)",
  "account.admin": "(admin)",
  "account.trialDaysLeft": "Essai : {n} jours restants",
  "account.trialExpired": "Essai expiré",
  "account.credentialWarning": "⚠️ CREDENTIAL_ENCRYPTION_KEY n'est pas configuré sur le serveur — les clés API ne peuvent pas être enregistrées car elles ne peuvent pas être chiffrées en toute sécurité. Veuillez contacter votre administrateur.",
  "account.savedKeyLabel": "Clé enregistrée :",
  "account.lastVerified": "Dernière vérification :",
  "account.riskAckGiven": "Confirmation du risque donnée le",
  "account.riskAckLabel": "Je confirme et accepte que ce bot puisse placer des ordres en argent réel sur des contrats à terme crypto, que cela comporte un risque de perte, et que je suis seul responsable — et non le bot — de toute perte éventuelle.",
  "account.apiKeyLabel": "Clé API Binance",
  "account.apiSecretLabel": "Secret API Binance",
  "account.apiKeyPlaceholderChange": "Entrez une nouvelle clé pour la modifier",
  "account.apiKeyPlaceholderNew": "Clé API Binance Futures",
  "account.apiSecretPlaceholderChange": "Entrez un nouveau secret pour le modifier",
  "account.apiSecretPlaceholderNew": "Secret API Binance Futures",
  "account.saveVerifyBtn": "Enregistrer et vérifier",
  "account.savingVerifying": "Enregistrement et vérification…",
  "account.removeConnectionBtn": "Supprimer la connexion",
  "account.verifyError": "Erreur de vérification",
  "account.verifiedConnected": "Connecté et vérifié",
  "account.connectedNotVerified": "Connecté, non vérifié",
  "account.notConnected": "Non connecté",
  "subscription.title": "Passer à l'abonnement",
  "subscription.desc": "Choisissez un plan, envoyez le paiement à l'IBAN ci-dessous puis cliquez sur « J'ai envoyé le paiement » — notre équipe vérifiera votre paiement et activera votre compte au plus vite.",
  "subscription.planMonthlyName": "Abonnement mensuel",
  "subscription.planMonthlyPrice": "50 USD",
  "subscription.planAnnualName": "Abonnement annuel",
  "subscription.planAnnualPrice": "500 USD",
  "subscription.selectedPlanLabel": "Plan sélectionné :",
  "subscription.ibanLabel": "IBAN :",
  "subscription.recipientLabel": "Bénéficiaire :",
  "subscription.usernameNote": "Indiquer votre nom d'utilisateur (<b>__USERNAME__</b>) dans le motif du virement accélère la vérification.",
  "subscription.paySentBtn": "J'ai envoyé le paiement",
  "subscription.sending": "Envoi en cours…",
  "subscription.resendBtn": "Notifier à nouveau",
  "subscription.successMsg": "✓ Notification reçue — notre équipe activera votre compte dès que votre paiement sera vérifié.",
  "subscription.genericError": "Une erreur s'est produite, veuillez réessayer.",
  "subscription.connectionError": "Erreur de connexion, veuillez réessayer.",
  "subscription.selectedMonthly": "Mensuel — 50 USD",
  "subscription.selectedAnnual": "Annuel — 500 USD",
  "askAdmin.title": "Contacter l'admin",
  "askAdmin.desc": "Votre message sera envoyé à herobotai.int@gmail.com.",
  "askAdmin.placeholder": "Écrivez votre question ici…",
  "askAdmin.sendBtn": "Envoyer",
  "askAdmin.sending": "Envoi en cours…",
  "askAdmin.emptyMessage": "Veuillez saisir un message.",
  "askAdmin.successMsg": "✓ Votre message a été envoyé. Nous vous répondrons sous peu.",
  "askAdmin.genericError": "Envoi impossible, veuillez réessayer.",
  "askAdmin.connectionError": "Erreur de connexion, veuillez réessayer.",
  "faq.title": "FAQ — Questions fréquentes",
  "faq.q1": "Ce bot trade-t-il avec de l'argent réel ?",
  "faq.a1": "Par défaut, non — le système fonctionne en mode paper/démo et n'envoie jamais d'ordres réels. Pour trader avec de l'argent réel, vous devez connecter votre clé API Binance et activer vous-même le « Trading en direct » depuis votre panneau.",
  "faq.q2": "Combien de temps dure l'essai gratuit et que se passe-t-il à la fin ?",
  "faq.a2": "7 jours. Une fois expiré, l'accès au panneau est restreint ; pour continuer, choisissez simplement un plan ici, payez l'IBAN, puis cliquez sur « J'ai envoyé le paiement » — notre équipe vérifiera et activera votre compte.",
  "faq.q3": "Comment l'abonnement est-il payé, le paiement par carte est-il disponible ?",
  "faq.a3": "Actuellement, les paiements sont acceptés par virement bancaire/EFT vers l'IBAN. Le plan mensuel est facturé 50 USD, le plan annuel 500 USD.",
  "faq.q4": "Est-il sûr de fournir ma clé API Binance ?",
  "faq.a4": "Votre clé est stockée chiffrée sur le serveur et n'est utilisée que pour ouvrir et fermer des ordres en votre nom. Nous vous recommandons de créer une clé API sans autorisation de « retrait » côté Binance.",
  "faq.q5": "Sur quels marchés le trading a-t-il lieu ?",
  "faq.a5": "Binance Futures (contrats perpétuels crypto), Borsa Istanbul et actions américaines (NASDAQ/NYSE/AMEX) — tous scannés depuis un seul panneau.",
  "faq.q6": "À quelle fréquence les signaux sont-ils générés ?",
  "faq.a6": "Le système effectue un scan automatique environ toutes les 15 minutes ; les signaux ne sont générés qu'à partir de bougies de 4 heures clôturées, jamais du bruit de prix instantané.",
  "faq.q7": "Comment activer les notifications Telegram ?",
  "faq.a7": "Il suffit d'obtenir un code de liaison depuis la section « Connexion Telegram » du panneau et de démarrer le bot sur Telegram — les notifications d'ouverture/clôture et le résumé quotidien arrivent alors automatiquement.",
  "faq.q8": "Que dois-je faire si je dois fermer d'urgence une position ouverte ?",
  "faq.a8": "Vous pouvez utiliser le bouton « Fermer maintenant » à côté de la position concernée dans le panneau « Compte réel Binance » ; cela envoie instantanément un ordre au marché réel et ferme la position.",
  "faq.q9": "J'ai une autre question, qui puis-je contacter ?",
  "faq.a9": "Cliquez simplement sur le bouton « Contacter l'admin » ci-dessus et écrivez votre message — il sera transmis directement à l'équipe d'administration.",
  "panel.openPosition": "Position ouverte",
  "panel.loading": "Chargement…",
  "pos.noOpenPosition": "Aucune position paper ouverte. Elle apparaîtra ici dès qu'un signal sera généré.",
  "pos.entry": "Entrée",
  "pos.current": "Actuel",
  "pos.unrealizedPnl": "P&L latent",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "Prix actuel",
  "pos.trailingActive": "ACTIF @",
  "pos.trailingStandby": "en attente",
  "pos.trailingLabel": "Trailing :",
  "pos.entryTimeLabel": "Heure d'entrée :",
  "panel.signalMatrix": "Matrice de signaux",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "Volatilité 1J",
  "signal.atrp1d": "ATRP %ile 1J",
  "signal.final": "Signal final",
  "scanner.tabCrypto": "Binance Futures",
  "scanner.tabBist": "Borsa Istanbul",
  "scanner.tabUs": "Wall Street",
  "scanner.note": "Perpétuel USDT-M · bougie 4H clôturée · à titre de signal uniquement, aucun ordre réel",
  "scanner.coinSearchPlaceholder": "Rechercher un coin (ex. BTC)",
  "scanner.stockSearchPlaceholderBist": "Rechercher une action (ex. THYAO)",
  "scanner.stockSearchPlaceholderUs": "Rechercher une action (ex. AAPL)",
  "scanner.allSignals": "Tous les signaux",
  "scanner.scanAll": "Tout scanner",
  "scanner.scanBist": "Scanner Borsa Istanbul",
  "scanner.scanUs": "Scanner S&P500/Nasdaq-100",
  "scanner.preparing": "Préparation…",
  "scanner.waiting": "En attente du scan…",
  "scanner.noResults": "Aucun résultat.",
  "scanner.headerCoin": "Coin",
  "scanner.headerStock": "Action",
  "scanner.headerPrice": "Prix",
  "scanner.headerChange24h": "24h %",
  "scanner.headerChangeDaily": "% quotidien",
  "scanner.headerVolume": "Volume",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP %ile",
  "scanner.headerSignal": "Signal",
  "scanner.headerReason": "Description",
  "scanner.headerAdd": "Ajouter",
  "scanner.coinCountSuffix": "coins",
  "scanner.stockCountSuffix": "actions",
  "scanner.bistFootnote": "SHORT n'indique ici que le signal technique de la stratégie ; cela ne signifie pas un ordre de vente à découvert direct sur le marché au comptant BIST.",
  "scanner.usFootnote": "Univers S&P 500 + Nasdaq-100 (liste statique, à mettre à jour périodiquement). Les actions américaines ajoutées à la watchlist ouvrent une position paper indépendante, comme la watchlist crypto ; le côté SHORT est une simulation pure qui ne modélise pas les contraintes d'emprunt/marge.",
  "scanner.scanning": "Analyse en cours",
  "scanner.ready": "Prêt",
  "scanner.lastScan4h": "Dernier scan 4H",
  "scanner.universe": "Univers",
  "scanner.error": "Erreur",
  "scanner.unknownError": "Erreur inconnue",
  "scanner.startingScan": "Démarrage du scan…",
  "scanner.startingUsScan": "Démarrage du scan… (514 actions, peut prendre quelques minutes)",
  "scanner.startingBistScan": "Démarrage du scan Borsa Istanbul…",
  "tvChart.title": "Graphique TradingView",
  "tvChart.noSymbol": "— aucun symbole sélectionné",
  "tvChart.emptyMessage": "Cliquez sur une ligne dans les tableaux de scan ci-dessus pour afficher ici le graphique TradingView de ce symbole.",
  "panel.paperTrades": "Trades paper",
  "watchlist.headerSymbol": "Symbole",
  "watchlist.headerMarket": "Marché",
  "watchlist.headerDirection": "Direction / Signal",
  "watchlist.headerPrice": "Prix",
  "watchlist.headerUnrealizedPnl": "P&L latent",
  "watchlist.headerAdded": "Ajouté",
  "watchlist.footnotePrefix": "Cliquez sur une ligne pour afficher ci-dessous le détail de la position et du signal de ce symbole. Les symboles crypto ouvrent une position paper indépendante avec la même stratégie (taille : $",
  "watchlist.footnoteSuffix": " nominal). Les symboles Borsa Istanbul servent uniquement au suivi du signal ; aucun ordre réel/paper n'est ouvert.",
  "watchlist.loading": "Chargement…",
  "watchlist.empty": "La watchlist est vide. Cliquez sur « + Ajouter » sur un symbole affichant LONG/SHORT dans le scanner pour que le bot le suive/le trade en paper.",
  "watchlist.mainEngine": "Moteur principal",
  "watchlist.noPosition": "pas de position",
  "watchlist.watched": "suivi",
  "watchlist.removeBtn": "Retirer",
  "watchlist.addBtn": "+ Ajouter",
  "watchlist.adding": "Ajout en cours…",
  "watchlist.added": "Ajouté ✓",
  "watchlist.symbolRemoved": "Ce symbole a peut-être été retiré de la watchlist.",
  "market.binance": "Binance",
  "market.bist": "Borsa Istanbul",
  "market.usStock": "Action US",
  "panel.binanceConnection": "Connexion Binance",
  "panel.telegramConnection": "Connexion Telegram",
  "panel.liveAccount": "Compte réel Binance",
  "liveAccount.title": "Trading en direct (argent réel)",
  "liveAccount.openPositionsSuffix": "positions ouvertes",
  "liveAccount.liveOff": "Trading en direct désactivé",
  "liveAccount.footnote": "Ce panneau n'affiche que les trades en direct sur <b>votre propre</b> compte Binance — jamais mélangés avec le panneau paper/démo ci-dessus ni avec d'autres utilisateurs ; personne d'autre que vous ne peut voir cela.",
  "panel.recentTrades": "Trades récents",
  "history.headerDate": "Date",
  "history.headerDirection": "Direction",
  "history.headerSymbol": "Symbole",
  "history.headerEntry": "Entrée",
  "history.headerExit": "Sortie",
  "history.headerPnl": "P&L",
  "history.headerReason": "Raison",
  "history.noClosedTrades": "Aucun trade clôturé pour le moment.",
  "history.noClosedLiveTrades": "Vous n'avez encore aucun trade en direct clôturé.",
  "liveOpen.headerQty": "Quantité",
  "liveOpen.headerCurrent": "Actuel",
  "liveOpen.headerLeverage": "Levier",
  "liveOpen.headerOpened": "Ouvert le",
  "liveOpen.noOpenPositions": "Vous n'avez actuellement aucune position en direct ouverte.",
  "liveOpen.closeNowBtn": "Fermer maintenant",
  "liveOpen.closing": "Fermeture en cours…",
  "panel.aiAnalyst": "Analyste IA de trading",
  "ai.runBtn": "Analyser maintenant",
  "ai.running": "Analyse en cours…",
  "ai.disabled": "L'analyste IA est désactivé — ANTHROPIC_API_KEY n'est pas défini.",
  "ai.noAnalysisYet": "Aucune analyse générée pour le moment. Cliquez sur « Analyser maintenant » pour créer le premier rapport.",
  "ai.lastAttemptFailed": "La dernière tentative a échoué",
  "ai.tradesAnalyzedPrefix": "trades analysés (total",
  "footer.autoRefresh": "Actualisation automatique : position 5 s · scanners 10 s · trading paper, aucun ordre réel.",
  "live.needConnectFirst": "Vous devez d'abord enregistrer et vérifier votre clé API Binance ci-dessus avant de pouvoir démarrer le trading en direct (argent réel).",
  "live.killSwitch": "🛑 Tout le trading en direct a été temporairement arrêté par l'administrateur",
  "live.pausedToday": "⏸ Limite quotidienne de perte maximale atteinte — aucun nouveau trade ne sera ouvert aujourd'hui",
  "live.on": "🟢 Trading en direct ACTIVÉ",
  "live.off": "Trading en direct désactivé — le bot fonctionne uniquement en mode paper (démo)",
  "live.openPositionCount": "Positions en direct ouvertes :",
  "live.todayRealizedPnl": "P&L réalisé estimé du jour :",
  "live.positionUsdLabel": "Montant en USD par trade",
  "live.positionUsdPlaceholder": "ex. 100",
  "live.maxLeverageLabel": "Levier max. (1-{n}x)",
  "live.maxLeveragePlaceholder": "ex. 2",
  "live.dailyLossLimitLabel": "Limite quotidienne de perte maximale (USD) — le trading s'arrête automatiquement pour la journée si dépassée",
  "live.dailyLossLimitPlaceholder": "ex. 50",
  "live.maxPositionsLabel": "Nombre max. de positions ouvertes (1-{n})",
  "live.maxPositionsPlaceholder": "ex. 1",
  "live.saveSettingsBtn": "Enregistrer les paramètres",
  "live.turnOffBtn": "Désactiver le trading en direct",
  "live.turnOnBtn": "ACTIVER le trading en direct (argent réel)",
  "live.dangerText": "⚠️ Une fois le trading en direct activé, le bot ouvre/ferme des ordres avec de l'<b>argent réel</b> sur votre compte Binance enregistré. C'est vous — et non le bot — qui êtes responsable des pertes éventuelles. Ceci ne constitue pas un conseil en investissement ; la conformité avec la réglementation applicable est de votre seule responsabilité.",
  "live.toggleOnConfirm": "Vous êtes sur le point d'activer le trading en direct. À partir de maintenant, le bot ouvrira et fermera des ordres avec de l'ARGENT RÉEL sur votre compte Binance. Confirmez-vous accepter le risque de perte et que ces paramètres sont corrects ?",
  "telegram.notEnabled": "Le bot Telegram n'est pas configuré sur le serveur.",
  "telegram.linked": "🟢 Telegram connecté",
  "telegram.notificationsDesc": "Les notifications d'ouverture/clôture de trades en direct, les alertes de risque et le résumé quotidien arriveront ici.",
  "telegram.removeConnectionBtn": "Supprimer la connexion",
  "telegram.notLinkedDesc": "Connectez-vous pour recevoir vos notifications de trading en direct sur votre propre Telegram.",
  "telegram.getCodeBtn": "Obtenir le code de liaison",
  "telegram.step1": "1) Ouvrez",
  "telegram.step1Fallback": "notre bot",
  "telegram.step1End": "sur Telegram.",
  "telegram.step2": "2) Envoyez ceci :",
  "telegram.codeExpiresPrefix": "Le code expire dans",
  "telegram.minutes": "min",
  "telegram.seconds": "sec.",
  "telegram.unlinkConfirm": "Êtes-vous sûr de vouloir supprimer la connexion Telegram ?",
  "alert.apiKeySecretRequired": "La clé API et le secret sont requis.",
  "alert.riskAckRequired": "Vous devez cocher la case de confirmation du risque avant de continuer.",
  "alert.saveFailedGeneric": "Enregistrement impossible",
  "alert.connectionError": "Erreur de connexion",
  "alert.notAdded": "Ajout impossible",
  "alert.codeNotObtained": "Impossible d'obtenir le code",
  "alert.disconnectBinanceConfirm": "Êtes-vous sûr de vouloir supprimer la connexion Binance ?",
  "alert.closePositionConfirmPrefix": "Êtes-vous sûr de vouloir fermer la position",
  "alert.closePositionConfirmSuffix": "maintenant avec un ordre au marché réel ? Cette action est irréversible.",
  "alert.closePositionFailed": "Impossible de fermer la position",
  "alert.actionFailed": "Échec de l'opération"
},
  es: {
  "nav.connecting": "Conectando",
  "nav.botActive": "Bot activo",
  "nav.standby": "En espera",
  "nav.connectionError": "Error de conexión",
  "nav.admin": "Administración",
  "nav.logout": "Cerrar sesión",
  "nav.language": "Idioma",
  "kpi.equity": "Saldo actual",
  "kpi.openPnl": "P&L de posiciones abiertas",
  "kpi.openPnlSub": "Todas las posiciones abiertas",
  "kpi.trades": "Operaciones · tasa de acierto",
  "kpi.winRate": "Tasa de acierto",
  "kpi.profitFactor": "Factor de beneficio",
  "kpi.avg": "Prom.",
  "kpi.maxDrawdown": "Drawdown máx.",
  "kpi.lastCandle": "Última vela:",
  "account.title": "Mi cuenta",
  "account.subscriptionBtn": "💳 Cambiar a suscripción",
  "account.askAdminBtn": "✉️ Preguntar al admin",
  "account.emailNotRegistered": "(sin correo registrado)",
  "account.admin": "(admin)",
  "account.trialDaysLeft": "Prueba: quedan {n} días",
  "account.trialExpired": "Prueba caducada",
  "account.credentialWarning": "⚠️ CREDENTIAL_ENCRYPTION_KEY no está configurada en el servidor — las claves API no se pueden guardar porque no se pueden cifrar de forma segura. Por favor, contáctate con tu administrador.",
  "account.savedKeyLabel": "Clave guardada:",
  "account.lastVerified": "Última verificación:",
  "account.riskAckGiven": "Confirmación de riesgo otorgada el",
  "account.riskAckLabel": "Confirmo y acepto que este bot puede abrir órdenes con dinero real en futuros de criptomonedas, que esto conlleva riesgo de pérdida, y que cualquier pérdida resultante es responsabilidad mía, no del bot.",
  "account.apiKeyLabel": "Clave API de Binance",
  "account.apiSecretLabel": "Secreto API de Binance",
  "account.apiKeyPlaceholderChange": "Introduce una nueva clave para cambiarla",
  "account.apiKeyPlaceholderNew": "Clave API de Binance Futures",
  "account.apiSecretPlaceholderChange": "Introduce un nuevo secreto para cambiarlo",
  "account.apiSecretPlaceholderNew": "Secreto API de Binance Futures",
  "account.saveVerifyBtn": "Guardar y verificar",
  "account.savingVerifying": "Guardando y verificando…",
  "account.removeConnectionBtn": "Eliminar conexión",
  "account.verifyError": "Error de verificación",
  "account.verifiedConnected": "Conectado y verificado",
  "account.connectedNotVerified": "Conectado, sin verificar",
  "account.notConnected": "No conectado",
  "subscription.title": "Cambiar a suscripción",
  "subscription.desc": "Elige un plan, envía el pago al IBAN de abajo y haz clic en “He enviado el pago” — nuestro equipo verificará tu pago y activará tu cuenta lo antes posible.",
  "subscription.planMonthlyName": "Suscripción mensual",
  "subscription.planMonthlyPrice": "50 USD",
  "subscription.planAnnualName": "Suscripción anual",
  "subscription.planAnnualPrice": "500 USD",
  "subscription.selectedPlanLabel": "Plan seleccionado:",
  "subscription.ibanLabel": "IBAN:",
  "subscription.recipientLabel": "Beneficiario:",
  "subscription.usernameNote": "Incluir tu nombre de usuario (<b>__USERNAME__</b>) en el concepto del pago agiliza la verificación.",
  "subscription.paySentBtn": "He enviado el pago",
  "subscription.sending": "Enviando…",
  "subscription.resendBtn": "Notificar de nuevo",
  "subscription.successMsg": "✓ Notificación recibida — nuestro equipo activará tu cuenta en cuanto se verifique tu pago.",
  "subscription.genericError": "Algo salió mal, inténtalo de nuevo.",
  "subscription.connectionError": "Error de conexión, inténtalo de nuevo.",
  "subscription.selectedMonthly": "Mensual — 50 USD",
  "subscription.selectedAnnual": "Anual — 500 USD",
  "askAdmin.title": "Preguntar al admin",
  "askAdmin.desc": "Tu mensaje se enviará a herobotai.int@gmail.com.",
  "askAdmin.placeholder": "Escribe tu pregunta aquí…",
  "askAdmin.sendBtn": "Enviar",
  "askAdmin.sending": "Enviando…",
  "askAdmin.emptyMessage": "Por favor, escribe un mensaje.",
  "askAdmin.successMsg": "✓ Tu mensaje ha sido enviado. Te responderemos en breve.",
  "askAdmin.genericError": "No se pudo enviar, inténtalo de nuevo.",
  "askAdmin.connectionError": "Error de conexión, inténtalo de nuevo.",
  "faq.title": "Preguntas frecuentes",
  "faq.q1": "¿Este bot opera con dinero real?",
  "faq.a1": "Por defecto, no — el sistema funciona en modo paper/demo y nunca envía órdenes reales. Para operar con dinero real debes conectar tu clave API de Binance y activar tú mismo el “Trading en vivo” desde tu panel.",
  "faq.q2": "¿Cuánto dura la prueba gratuita y qué pasa cuando termina?",
  "faq.a2": "7 días. Al terminar, tu acceso al panel se restringe; para continuar, elige un plan aquí, paga al IBAN y haz clic en “He enviado el pago” — nuestro equipo lo verificará y activará tu cuenta.",
  "faq.q3": "¿Cómo se paga la suscripción, hay pago con tarjeta?",
  "faq.a3": "Actualmente los pagos se aceptan mediante transferencia bancaria/EFT al IBAN. El plan mensual se cobra como 50 USD y el anual como 500 USD.",
  "faq.q4": "¿Es seguro proporcionar mi clave API de Binance?",
  "faq.a4": "Tu clave se almacena cifrada en el servidor y solo se usa para abrir y cerrar órdenes en tu nombre. Recomendamos crear una clave API sin permiso de “retiro” en Binance.",
  "faq.q5": "¿En qué mercados se opera?",
  "faq.a5": "Binance Futures (perpetuos cripto), Borsa Istanbul y acciones de EE. UU. (NASDAQ/NYSE/AMEX) — todos escaneados desde un solo panel.",
  "faq.q6": "¿Con qué frecuencia se generan las señales?",
  "faq.a6": "El sistema realiza un escaneo automático aproximadamente cada 15 minutos; las señales solo se generan a partir de velas de 4 horas cerradas, no del ruido de precio instantáneo.",
  "faq.q7": "¿Cómo activo las notificaciones de Telegram?",
  "faq.a7": "Simplemente obtén un código de enlace desde la sección “Conexión Telegram” del panel e inicia el bot en Telegram — las notificaciones de apertura/cierre y el resumen diario llegarán automáticamente.",
  "faq.q8": "¿Qué debo hacer si necesito cerrar urgentemente una posición abierta?",
  "faq.a8": "Puedes usar el botón “Cerrar ahora” junto a la posición correspondiente en el panel “Cuenta real de Binance”; esto envía al instante una orden de mercado real y cierra la posición.",
  "faq.q9": "Tengo otra pregunta, ¿a quién puedo contactar?",
  "faq.a9": "Simplemente haz clic en el botón “Preguntar al admin” de arriba y escribe tu mensaje — se enviará directamente al equipo de administración.",
  "panel.openPosition": "Posición abierta",
  "panel.loading": "Cargando…",
  "pos.noOpenPosition": "No hay posición paper abierta. Aparecerá aquí en cuanto se genere una señal.",
  "pos.entry": "Entrada",
  "pos.current": "Actual",
  "pos.unrealizedPnl": "P&L no realizado",
  "pos.atr": "ATR",
  "pos.currentPriceTitle": "Precio actual",
  "pos.trailingActive": "ACTIVO @",
  "pos.trailingStandby": "inactivo",
  "pos.trailingLabel": "Trailing:",
  "pos.entryTimeLabel": "Hora de entrada:",
  "panel.signalMatrix": "Matriz de señales",
  "signal.ema": "EMA 50 / 100",
  "signal.supertrend": "Supertrend",
  "signal.adx": "ADX",
  "signal.rsi": "RSI",
  "signal.cci": "CCI",
  "signal.stochRsi": "Stoch RSI",
  "signal.macd": "MACD",
  "signal.volatility1d": "Volatilidad 1D",
  "signal.atrp1d": "ATRP %il 1D",
  "signal.final": "Señal final",
  "scanner.tabCrypto": "Binance Futures",
  "scanner.tabBist": "Borsa Istanbul",
  "scanner.tabUs": "Wall Street",
  "scanner.note": "Perpetuo USDT-M · vela 4H cerrada · solo con fines de señal, sin órdenes reales",
  "scanner.coinSearchPlaceholder": "Buscar moneda (ej. BTC)",
  "scanner.stockSearchPlaceholderBist": "Buscar acción (ej. THYAO)",
  "scanner.stockSearchPlaceholderUs": "Buscar acción (ej. AAPL)",
  "scanner.allSignals": "Todas las señales",
  "scanner.scanAll": "Escanear todo",
  "scanner.scanBist": "Escanear Borsa Istanbul",
  "scanner.scanUs": "Escanear S&P500/Nasdaq-100",
  "scanner.preparing": "Preparando…",
  "scanner.waiting": "Esperando escaneo…",
  "scanner.noResults": "Sin resultados.",
  "scanner.headerCoin": "Moneda",
  "scanner.headerStock": "Acción",
  "scanner.headerPrice": "Precio",
  "scanner.headerChange24h": "24h %",
  "scanner.headerChangeDaily": "% diario",
  "scanner.headerVolume": "Volumen",
  "scanner.headerSt": "ST",
  "scanner.headerAdx": "ADX",
  "scanner.headerRsi": "RSI",
  "scanner.headerCci": "CCI",
  "scanner.headerMacd": "MACD",
  "scanner.headerStochKd": "Stoch K/D",
  "scanner.headerAtrp": "ATRP %il",
  "scanner.headerSignal": "Señal",
  "scanner.headerReason": "Descripción",
  "scanner.headerAdd": "Añadir",
  "scanner.coinCountSuffix": "monedas",
  "scanner.stockCountSuffix": "acciones",
  "scanner.bistFootnote": "SHORT aquí es solo la señal técnica de la estrategia; no implica una orden de venta en corto directa en el mercado spot de BIST.",
  "scanner.usFootnote": "Universo S&P 500 + Nasdaq-100 (lista estática, debe actualizarse periódicamente). Las acciones de EE. UU. añadidas a la watchlist abren una posición paper independiente igual que la watchlist cripto; el lado SHORT es una simulación pura que no modela restricciones de préstamo/margen.",
  "scanner.scanning": "Escaneando",
  "scanner.ready": "Listo",
  "scanner.lastScan4h": "Último escaneo 4H",
  "scanner.universe": "Universo",
  "scanner.error": "Error",
  "scanner.unknownError": "Error desconocido",
  "scanner.startingScan": "Iniciando escaneo…",
  "scanner.startingUsScan": "Iniciando escaneo… (514 acciones, puede tardar unos minutos)",
  "scanner.startingBistScan": "Iniciando escaneo de Borsa Istanbul…",
  "tvChart.title": "Gráfico de TradingView",
  "tvChart.noSymbol": "— ningún símbolo seleccionado",
  "tvChart.emptyMessage": "Haz clic en una fila de las tablas de escaneo de arriba para ver aquí el gráfico de TradingView de ese símbolo.",
  "panel.paperTrades": "Operaciones de prueba",
  "watchlist.headerSymbol": "Símbolo",
  "watchlist.headerMarket": "Mercado",
  "watchlist.headerDirection": "Dirección / Señal",
  "watchlist.headerPrice": "Precio",
  "watchlist.headerUnrealizedPnl": "P&L no realizado",
  "watchlist.headerAdded": "Añadido",
  "watchlist.footnotePrefix": "Haz clic en una fila para ver abajo el detalle de posición y señal de ese símbolo. Los símbolos cripto abren una posición paper independiente con la misma estrategia (tamaño: $",
  "watchlist.footnoteSuffix": " nominal). Los símbolos de Borsa Istanbul son solo de seguimiento de señal; no se abre ninguna orden real/paper.",
  "watchlist.loading": "Cargando…",
  "watchlist.empty": "La watchlist está vacía. Haz clic en “+ Añadir” en cualquier símbolo que muestre LONG/SHORT en el escaneo para que el bot lo siga/opere en modo paper.",
  "watchlist.mainEngine": "Motor principal",
  "watchlist.noPosition": "sin posición",
  "watchlist.watched": "en seguimiento",
  "watchlist.removeBtn": "Quitar",
  "watchlist.addBtn": "+ Añadir",
  "watchlist.adding": "Añadiendo…",
  "watchlist.added": "Añadido ✓",
  "watchlist.symbolRemoved": "Es posible que este símbolo se haya eliminado de la watchlist.",
  "market.binance": "Binance",
  "market.bist": "Borsa Istanbul",
  "market.usStock": "Acción EE. UU.",
  "panel.binanceConnection": "Conexión con Binance",
  "panel.telegramConnection": "Conexión con Telegram",
  "panel.liveAccount": "Cuenta real de Binance",
  "liveAccount.title": "Trading en vivo (dinero real)",
  "liveAccount.openPositionsSuffix": "posiciones abiertas",
  "liveAccount.liveOff": "Trading en vivo desactivado",
  "liveAccount.footnote": "Este panel solo muestra operaciones en vivo en <b>tu propia</b> cuenta de Binance — nunca se mezcla con el panel paper/demo de arriba ni con otros usuarios; nadie más que tú puede verlo.",
  "panel.recentTrades": "Operaciones recientes",
  "history.headerDate": "Fecha",
  "history.headerDirection": "Dirección",
  "history.headerSymbol": "Símbolo",
  "history.headerEntry": "Entrada",
  "history.headerExit": "Salida",
  "history.headerPnl": "P&L",
  "history.headerReason": "Motivo",
  "history.noClosedTrades": "Aún no hay operaciones cerradas.",
  "history.noClosedLiveTrades": "Aún no tienes operaciones en vivo cerradas.",
  "liveOpen.headerQty": "Cantidad",
  "liveOpen.headerCurrent": "Actual",
  "liveOpen.headerLeverage": "Apalancamiento",
  "liveOpen.headerOpened": "Apertura",
  "liveOpen.noOpenPositions": "Ahora mismo no tienes posiciones en vivo abiertas.",
  "liveOpen.closeNowBtn": "Cerrar ahora",
  "liveOpen.closing": "Cerrando…",
  "panel.aiAnalyst": "Analista de trading con IA",
  "ai.runBtn": "Analizar ahora",
  "ai.running": "Analizando…",
  "ai.disabled": "El analista de IA está desactivado — ANTHROPIC_API_KEY no está definida.",
  "ai.noAnalysisYet": "Aún no se ha generado ningún análisis. Haz clic en “Analizar ahora” para crear el primer informe.",
  "ai.lastAttemptFailed": "El último intento falló",
  "ai.tradesAnalyzedPrefix": "operaciones analizadas (total",
  "footer.autoRefresh": "Actualización automática: posición 5 s · escaneos 10 s · trading de prueba, sin órdenes reales.",
  "live.needConnectFirst": "Primero debes guardar y verificar tu clave API de Binance arriba antes de poder iniciar el trading en vivo (dinero real).",
  "live.killSwitch": "🛑 El administrador ha detenido temporalmente todo el trading en vivo",
  "live.pausedToday": "⏸ Se alcanzó el límite diario de pérdida máxima — hoy no se abrirán nuevas operaciones",
  "live.on": "🟢 Trading en vivo ACTIVADO",
  "live.off": "Trading en vivo desactivado — el bot solo funciona en modo paper (demo)",
  "live.openPositionCount": "Posiciones en vivo abiertas:",
  "live.todayRealizedPnl": "P&L realizado estimado de hoy:",
  "live.positionUsdLabel": "Importe en USD por operación",
  "live.positionUsdPlaceholder": "ej. 100",
  "live.maxLeverageLabel": "Apalancamiento máx. (1-{n}x)",
  "live.maxLeveragePlaceholder": "ej. 2",
  "live.dailyLossLimitLabel": "Límite diario de pérdida máxima (USD) — el trading se detiene automáticamente ese día si se supera",
  "live.dailyLossLimitPlaceholder": "ej. 50",
  "live.maxPositionsLabel": "Número máx. de posiciones abiertas (1-{n})",
  "live.maxPositionsPlaceholder": "ej. 1",
  "live.saveSettingsBtn": "Guardar configuración",
  "live.turnOffBtn": "Desactivar trading en vivo",
  "live.turnOnBtn": "ACTIVAR trading en vivo (dinero real)",
  "live.dangerText": "⚠️ Una vez activado el trading en vivo, el bot abre/cierra órdenes con <b>dinero real</b> en tu cuenta de Binance registrada. Tú — no el bot — eres responsable de cualquier pérdida. Esto no es asesoramiento de inversión; el cumplimiento de la normativa aplicable es tu propia responsabilidad.",
  "live.toggleOnConfirm": "Estás a punto de activar el trading en vivo. A partir de este momento, el bot abrirá y cerrará órdenes con DINERO REAL en tu cuenta de Binance. ¿Confirmas que aceptas el riesgo de pérdida y que esta configuración es correcta?",
  "telegram.notEnabled": "El bot de Telegram no está configurado en el servidor.",
  "telegram.linked": "🟢 Telegram conectado",
  "telegram.notificationsDesc": "Aquí llegarán las notificaciones de apertura/cierre de operaciones en vivo, las alertas de riesgo y el resumen diario.",
  "telegram.removeConnectionBtn": "Eliminar conexión",
  "telegram.notLinkedDesc": "Conéctate para recibir tus notificaciones de trading en vivo en tu propio Telegram.",
  "telegram.getCodeBtn": "Obtener código de enlace",
  "telegram.step1": "1) Abre",
  "telegram.step1Fallback": "nuestro bot",
  "telegram.step1End": "en Telegram.",
  "telegram.step2": "2) Envía esto:",
  "telegram.codeExpiresPrefix": "El código caduca en",
  "telegram.minutes": "min",
  "telegram.seconds": "seg.",
  "telegram.unlinkConfirm": "¿Seguro que quieres eliminar la conexión con Telegram?",
  "alert.apiKeySecretRequired": "Se requieren la clave API y el secreto.",
  "alert.riskAckRequired": "Debes marcar la casilla de confirmación de riesgo antes de continuar.",
  "alert.saveFailedGeneric": "No se pudo guardar",
  "alert.connectionError": "Error de conexión",
  "alert.notAdded": "No se pudo añadir",
  "alert.codeNotObtained": "No se pudo obtener el código",
  "alert.disconnectBinanceConfirm": "¿Seguro que quieres eliminar la conexión con Binance?",
  "alert.closePositionConfirmPrefix": "¿Seguro que quieres cerrar la posición",
  "alert.closePositionConfirmSuffix": "ahora con una orden de mercado real? Esta acción no se puede deshacer.",
  "alert.closePositionFailed": "No se pudo cerrar la posición",
  "alert.actionFailed": "La acción falló"
}
};
// ---- i18n ----
let currentLang = 'en';
function trGet(lang, key){
  let v = translations[lang] ? translations[lang][key] : undefined;
  if(v === undefined || v === null) v = translations['en'][key];
  return (v === undefined || v === null) ? key : v;
}
function t(key){ return trGet(currentLang, key); }
function applyTranslation(lang){
  if(!translations[lang]) lang = 'en';
  currentLang = lang;
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    const key = el.getAttribute('data-i18n');
    const val = trGet(lang, key);
    if(el.hasAttribute('data-i18n-html')) el.innerHTML = val; else el.textContent = val;
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>{
    el.setAttribute('placeholder', trGet(lang, el.getAttribute('data-i18n-placeholder')));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{
    el.setAttribute('title', trGet(lang, el.getAttribute('data-i18n-title')));
  });
  try{ localStorage.setItem('lang', lang); }catch(e){}
  const sel = document.getElementById('langSelect');
  if(sel && sel.value !== lang) sel.value = lang;
  refreshDynamicTexts();
}
function refreshDynamicTexts(){
  try{ if(statusCache) render(statusCache); else renderWatchlistTable(); }catch(e){}
  try{ renderScanner(); }catch(e){}
  try{ renderBistScanner(); }catch(e){}
  try{ renderUsScanner(); }catch(e){}
  try{ if(accountCache) renderAccount(accountCache); }catch(e){}
  try{ if(aiAnalysisCache) renderAiAnalysis(aiAnalysisCache); }catch(e){}
  try{ if(liveMineCache) renderMyLive(liveMineCache); }catch(e){}
}
let _initialLang = 'en';
try{ _initialLang = localStorage.getItem('lang') || 'en'; }catch(e){}

const money=x=>x==null?'—':'$'+Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const num=x=>x==null||x===''?'—':Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const cls=x=>Number(x)>=0?'pos':'neg';

// live clock
function tickClock(){const d=new Date();document.getElementById('clock').textContent=d.toLocaleTimeString(currentLang||'en-US',{hour12:false});}
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
    posEl.innerHTML='<div class="pos-top"><span class="side-tag" style="background:var(--neu-bg);color:var(--text-dim);border:1px solid var(--border-soft)">FLAT</span></div><div class="pos-empty">'+t('pos.noOpenPosition')+'</div>';
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
      <div><div class="kpi-label">${t('pos.entry')}</div><div class="val">${num(entry)}</div></div>
      <div><div class="kpi-label">${t('pos.current')}</div><div class="val">${num(cur)}</div></div>
      <div><div class="kpi-label">${t('pos.unrealizedPnl')}</div><div class="val ${cls(pnl)}">${money(pnl)}</div></div>
      <div><div class="kpi-label">${t('pos.atr')}</div><div class="val">${num(p.atr)}</div></div>
    </div>
    <div class="bar-wrap">
      <div class="bar-labels"><span>SL ${num(stop)}</span><span>TP ${num(tp)}</span></div>
      <div class="bar-track">
        <div class="bar-fill" style="left:0%;right:0%"></div>
        <div class="bar-dot sl" style="left:${pct(stop)}%"></div>
        <div class="bar-dot tp" style="left:${pct(tp)}%"></div>
        <div class="bar-dot cur" style="left:${pct(cur)}%" title="${t('pos.currentPriceTitle')}"></div>
      </div>
    </div>
    <div class="pos-foot">${t('pos.trailingLabel')} ${p.trail_active?(t('pos.trailingActive')+' '+num(p.trail_stop)):t('pos.trailingStandby')} &middot; ${t('pos.entryTimeLabel')} ${p.entry_time}</div>`;
}
function renderSignalCard(sigEl,s){
  s=s||{};
  const rows=[[t('signal.ema'),s.ema],[t('signal.supertrend'),s.supertrend],[t('signal.adx'),s.adx],[t('signal.rsi'),s.rsi],[t('signal.cci'),s.cci],[t('signal.stochRsi'),s.stoch],[t('signal.macd'),s.macd],[t('signal.volatility1d'),s.volatility],[t('signal.atrp1d'),s.atrp_percentile_1d]];
  sigEl.innerHTML=
    '<div class="chip-grid">'+
    rows.map(r=>`<div class="chip"><span class="chip-label">${r[0]}</span><span class="chip-value ${chipClass(r[1])}">${r[1]??'—'}</span></div>`).join('')+
    `<div class="chip final"><span class="chip-label">${t('signal.final')}</span><span class="chip-value ${chipClass(s.final)}">${s.final??'—'}</span></div>`+
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
    if(!statusCache){posEl.innerHTML=t('panel.loading');sigEl.innerHTML='—';return;}
    renderPositionCard(posEl,statusCache.position);
    renderSignalCard(sigEl,statusCache.signals);
    return;
  }
  const item=(watchlistCache.items||[]).find(x=>x.symbol===selectedSymbol);
  if(!item){
    posEl.innerHTML='<div class="pos-empty">'+t('watchlist.symbolRemoved')+'</div>';
    sigEl.innerHTML='—';
    return;
  }
  renderPositionCard(posEl,item.position);
  renderSignalCard(sigEl,item.indicators);
}

function render(d){
  statusCache=d;
  const st=document.getElementById('status');
  if(d.bot_alive){st.className='status-pill live';st.innerHTML='<span class="dot"></span>'+t('nav.botActive');}
  else{st.className='status-pill wait';st.innerHTML='<span class="dot"></span>'+t('nav.standby');}

  document.getElementById('equity').textContent=money(d.equity);
  document.getElementById('pnl').innerHTML=`<span class="${cls(d.net_pnl)}">${money(d.net_pnl)}</span> &middot; ${Number(d.return_pct||0).toFixed(2)}%`;
  document.getElementById('openPnl').innerHTML=`<span class="${cls(d.total_open_pnl)}">${money(d.total_open_pnl)}</span>`;
  document.getElementById('openPnlSub').textContent=t('kpi.openPnlSub');
  document.getElementById('trades').textContent=d.stats.trades;
  document.getElementById('winrate').textContent=t('kpi.winRate')+' '+d.stats.win_rate.toFixed(2)+'%';
  document.getElementById('pf').textContent=d.stats.profit_factor.toFixed(3);
  document.getElementById('avg').textContent=t('kpi.avg')+' '+money(d.stats.avg_trade);
  document.getElementById('dd').textContent=d.stats.max_drawdown.toFixed(2)+'%';
  document.getElementById('candle').textContent=t('kpi.lastCandle')+' '+(d.last_closed_time||'—');

  const eqSeries=(d.history||[]).slice().reverse().map(t=>Number(t.equity_after)).filter(v=>!isNaN(v));
  if(eqSeries.length<2 && d.equity!=null) eqSeries.push(Number(d.equity));
  drawSparkline(eqSeries);

  document.getElementById('history').innerHTML=(d.history||[]).map(tr=>`<tr><td>${tr.exit_time||'—'}</td><td><span class="pill ${String(tr.side).toLowerCase()}">${tr.side}</span></td><td>${tr.symbol}</td><td class="num">${num(tr.entry_price)}</td><td class="num">${num(tr.exit_price)}</td><td class="num ${cls(tr.net_pnl)}"><b>${money(tr.net_pnl)}</b></td><td class="wrap-cell">${tr.reason||''}</td></tr>`).join('') || `<tr><td colspan="7" class="empty">${t('history.noClosedTrades')}</td></tr>`;

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
  if(watchlistSymbols.has(symbol)) return '<span class="added-tag">'+t('watchlist.added')+'</span>';
  return `<button class="add-btn" onclick="event.stopPropagation();addToWatchlist('${symbol}','${market}','${signal}',this)">${t('watchlist.addBtn')}</button>`;
}

// TradingView "Advanced Chart" widget — TradingView's own free public embed
// (no API key, no account needed; https://www.tradingview.com/widget/advanced-chart/).
// Clicking any scanner row loads that symbol's live chart into the panel below.
function openTvChart(tvSymbol,label){
  document.getElementById('tvChartSymbol').textContent='— '+label;
  const frame=document.getElementById('tvChartFrame');
  frame.src='https://s.tradingview.com/widgetembed/?symbol='+encodeURIComponent(tvSymbol)
    +'&interval=240&hidesidetoolbar=0&symboledit=1&saveimage=0&toolbarbg=0D1114'
    +'&theme=dark&style=1&timezone=Etc%2FUTC&withdateranges=1&studies=%5B%5D&locale='+encodeURIComponent(currentLang||'en');
  frame.classList.remove('hidden');
  document.getElementById('tvChartEmpty').style.display='none';
  document.getElementById('tvChartPanel').scrollIntoView({behavior:'smooth',block:'start'});
}
async function addToWatchlist(symbol,market,signal,btn){
  if(btn){btn.disabled=true;btn.textContent=t('watchlist.adding');}
  try{
    const r=await fetch(`/api/watchlist/add?symbol=${encodeURIComponent(symbol)}&market=${market}&signal=${encodeURIComponent(signal)}`,{cache:'no-store'});
    const d=await r.json();
    if(!d.ok && btn){btn.disabled=false;btn.textContent=t('watchlist.addBtn');alert(d.error||t('alert.notAdded'));}
  }catch(e){ if(btn){btn.disabled=false;btn.textContent=t('watchlist.addBtn');} }
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
    const pnlCell=p?`<span class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</span>`:'<span class="text-faint">'+t('watchlist.noPosition')+'</span>';
    const sel=selectedSymbol==='ETHUSDT'?' row-selected':'';
    rowsHtml+=`<tr class="row-clickable${sel}" onclick="selectSymbol('ETHUSDT')"><td><b>ETHUSDT</b></td><td>${t('market.binance')}</td><td>${sideCell}</td><td class="num">${num(p?p.current_price:statusCache.price)}</td><td class="num">${pnlCell}</td><td class="text-faint">${t('watchlist.mainEngine')}</td><td></td></tr>`;
  }

  rowsHtml+=items.map(x=>{
    const p=x.position;
    let sideCell, pnlCell;
    if(p){
      sideCell=`<span class="pill ${p.side.toLowerCase()}">${p.side}</span>`;
      pnlCell=`<span class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</span>`;
    } else {
      sideCell=sigPill(x.current_signal||'NO SIGNAL');
      pnlCell=x.market==='bist'?'<span class="text-faint">'+t('watchlist.watched')+'</span>':'<span class="text-faint">'+t('watchlist.noPosition')+'</span>';
    }
    const added=(x.added_at||'').replace('T',' ').slice(0,16);
    const sel=selectedSymbol===x.symbol?' row-selected':'';
    const marketLabel=x.market==='bist'?t('market.bist'):x.market==='us_stock'?t('market.usStock'):t('market.binance');
    return `<tr class="row-clickable${sel}" onclick="selectSymbol('${x.symbol}')"><td><b>${x.symbol}</b></td><td>${marketLabel}</td><td>${sideCell}</td><td class="num">${num(p?p.current_price:x.current_price)}</td><td class="num">${pnlCell}</td><td class="text-faint">${added}</td><td><button class="btn" onclick="event.stopPropagation();removeFromWatchlist('${x.symbol}')">${t('watchlist.removeBtn')}</button></td></tr>`;
  }).join('');

  document.getElementById('watchlistRows').innerHTML=rowsHtml||'<tr><td colspan="7" class="watchlist-empty">'+t('watchlist.empty')+'</td></tr>';
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
  document.getElementById('scannerRows').innerHTML=rows.map(x=>`<tr class="row-clickable" onclick="openTvChart('BINANCE:${x.symbol}.P','${x.symbol} · Binance Futures')"><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td class="num">${Number(x.volume||0).toLocaleString('en-US',{maximumFractionDigits:0})}</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'crypto',x.signal)}</td></tr>`).join('')||`<tr><td colspan="13" class="empty">${t('scanner.noResults')}</td></tr>`;
  document.getElementById('coinCount').textContent=rows.length+' '+t('scanner.coinCountSuffix');
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
  document.getElementById('bistScannerRows').innerHTML=rows.map(x=>`<tr class="row-clickable" onclick="openTvChart('BIST:${x.symbol}','${x.symbol} · Borsa Istanbul')"><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.stoch_k??'—'} / ${x.stoch_d??'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'bist',x.signal)}</td></tr>`).join('')||`<tr><td colspan="13" class="empty">${t('scanner.noResults')}</td></tr>`;
  document.getElementById('bistCount').textContent=rows.length+' '+t('scanner.stockCountSuffix');
  document.getElementById('bistLongCount').textContent='LONG '+rows.filter(x=>x.signal==='LONG').length;
  document.getElementById('bistShortCount').textContent='SHORT '+rows.filter(x=>x.signal==='SHORT').length;
  document.getElementById('bistNoCount').textContent='NO SIGNAL '+rows.filter(x=>x.signal==='NO SIGNAL').length;
}
attachSort('scannerTable',()=>scannerCache,renderScanner);
attachSort('bistScannerTable',()=>bistScannerCache,renderBistScanner);
attachSort('usScannerTable',()=>usScannerCache,renderUsScanner);

async function refreshBistScanner(){
  let d=await bistScannerData();bistScannerCache=d;
  let st=d.status||'IDLE';let src=d.universe_source?` &middot; ${t('scanner.universe')}: ${d.universe_source}`:'';
  let txt=st==='SCANNING'?`${t('scanner.tabBist')}: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`${t('scanner.ready')} &middot; ${t('scanner.lastScan4h')}: ${d.last_scan_candle||'—'}${src}`:st==='ERROR'?`${t('scanner.error')}: ${d.last_error||t('scanner.unknownError')}`:t('scanner.waiting');
  document.getElementById('bistScannerStatus').textContent=txt;renderBistScanner();
}
async function startBistScanner(force=false){document.getElementById('bistScannerStatus').textContent=t('scanner.startingBistScan');try{await fetch('/api/bist-scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshBistScanner();}
refreshBistScanner();setInterval(refreshBistScanner,10000);

async function usScannerData(){try{let r=await fetch('/api/us-scanner',{cache:'no-store'});return await r.json()}catch(e){return {status:'ERROR',results:[],last_error:String(e)}}}
let usScannerCache={results:[]};
function renderUsScanner(){
  let q=(document.getElementById('usSearch')?.value||'').toUpperCase();
  let f=document.getElementById('usSignalFilter')?.value||'ALL';
  let rows=usScannerCache.results.filter(x=>(!q||x.symbol.includes(q))&&(f==='ALL'||x.signal===f));
  rows=sortRows(rows,'us');
  document.getElementById('usScannerRows').innerHTML=rows.map(x=>`<tr class="row-clickable" onclick="openTvChart('${(x.exchange||'NASDAQ')}:${x.symbol}','${x.symbol} · US Stock')"><td><b>${x.symbol}</b></td><td class="num">${num(x.price)}</td><td class="num ${Number(x.change_pct)>=0?'pos':'neg'}">${Number(x.change_pct||0).toFixed(2)}%</td><td>${x.st||'—'}</td><td class="num">${x.adx??'—'}</td><td class="num">${x.rsi??'—'}</td><td class="num">${x.cci??'—'}</td><td>${x.macd||'—'}</td><td class="num">${x.stoch_k??'—'} / ${x.stoch_d??'—'}</td><td class="num">${x.atrp_percentile_1d??'—'}</td><td>${sigPill(x.signal)}</td><td class="wrap-cell">${x.reason||''}</td><td>${addCell(x.symbol,'us_stock',x.signal)}</td></tr>`).join('')||`<tr><td colspan="13" class="empty">${t('scanner.noResults')}</td></tr>`;
  document.getElementById('usCount').textContent=rows.length+' '+t('scanner.stockCountSuffix');
  document.getElementById('usLongCount').textContent='LONG '+rows.filter(x=>x.signal==='LONG').length;
  document.getElementById('usShortCount').textContent='SHORT '+rows.filter(x=>x.signal==='SHORT').length;
  document.getElementById('usNoCount').textContent='NO SIGNAL '+rows.filter(x=>x.signal==='NO SIGNAL').length;
}
async function refreshUsScanner(){
  let d=await usScannerData();usScannerCache=d;
  let st=d.status||'IDLE';let src=d.universe_source?` &middot; ${t('scanner.universe')}: ${d.universe_source}`:'';
  let txt=st==='SCANNING'?`${t('scanner.scanning')}: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`${t('scanner.ready')} &middot; ${t('scanner.lastScan4h')}: ${d.last_scan_candle||'—'}${src}`:st==='ERROR'?`${t('scanner.error')}: ${d.last_error||t('scanner.unknownError')}`:t('scanner.waiting');
  document.getElementById('usScannerStatus').textContent=txt;renderUsScanner();
}
async function startUsScanner(force=false){document.getElementById('usScannerStatus').textContent=t('scanner.startingUsScan');try{await fetch('/api/us-scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshUsScanner();}
refreshUsScanner();setInterval(refreshUsScanner,10000);

async function refreshScanner(){
  let d=await scannerData();scannerCache=d;
  let st=d.status||'IDLE';
  let txt=st==='SCANNING'?`${t('scanner.scanning')}: ${d.symbols_done||0}/${d.symbols_total||0}`:st==='READY'?`${t('scanner.ready')} &middot; ${t('scanner.lastScan4h')}: ${d.last_scan_candle||'—'}`:st==='ERROR'?`${t('scanner.error')}: ${d.last_error||t('scanner.unknownError')}`:t('scanner.waiting');
  document.getElementById('scannerStatus').textContent=txt;renderScanner();
}
async function startScanner(force=false){document.getElementById('scannerStatus').textContent=t('scanner.startingScan');try{await fetch('/api/scanner/scan?force='+(force?'1':'0'),{cache:'no-store'})}catch(e){}refreshScanner();}
refreshScanner();setInterval(refreshScanner,10000);

async function refresh(){
  try{let r=await fetch('/api/status',{cache:'no-store'});let d=await r.json();render(d)}
  catch(e){const st=document.getElementById('status');st.className='status-pill err';st.innerHTML='<span class="dot"></span>'+t('nav.connectionError');}
}
refresh();setInterval(refresh,5000);
refreshWatchlist();setInterval(refreshWatchlist,10000);

let liveMineCache=null;
function renderMyLive(d){
  liveMineCache=d;
  const open=d.open||[], closed=d.closed||[];
  document.getElementById('liveMineCount').textContent = d.live_trading_enabled
    ? `${open.length} ${t('liveAccount.openPositionsSuffix')}`
    : t('liveAccount.liveOff');

  document.getElementById('liveMineOpenRows').innerHTML = open.map(p=>{
    const opened=(p.entry_time||'').replace('T',' ').slice(0,16);
    return `<tr><td><b>${p.symbol}</b></td><td><span class="pill ${String(p.side).toLowerCase()}">${p.side}</span></td>`
      +`<td class="num">${num(p.qty)}</td><td class="num">${num(p.entry_price)}</td><td class="num">${num(p.current_price)}</td>`
      +`<td class="num ${cls(p.unrealized_pnl)}"><b>${money(p.unrealized_pnl)}</b></td><td>${p.leverage||1}x</td><td class="text-faint">${opened}</td>`
      +`<td><button class="btn btn-danger" onclick="closeLivePosition('${p.symbol}',this)">${t('liveOpen.closeNowBtn')}</button></td></tr>`;
  }).join('') || `<tr><td colspan="9" class="empty">${t('liveOpen.noOpenPositions')}</td></tr>`;

  document.getElementById('liveMineClosedRows').innerHTML = closed.map(tr=>{
    return `<tr><td>${(tr.exit_time||'—')}</td><td><span class="pill ${String(tr.side).toLowerCase()}">${tr.side}</span></td><td>${tr.symbol}</td>`
      +`<td class="num">${num(tr.entry_price)}</td><td class="num">${num(tr.exit_price)}</td>`
      +`<td class="num ${cls(tr.pnl)}"><b>${money(tr.pnl)}</b></td><td class="wrap-cell">${tr.reason||''}</td></tr>`;
  }).join('') || `<tr><td colspan="7" class="empty">${t('history.noClosedLiveTrades')}</td></tr>`;
}
async function refreshMyLive(){
  let d;
  try{ const r=await fetch('/api/live/my-positions',{cache:'no-store'}); d=await r.json(); }catch(e){ return; }
  renderMyLive(d);
}
refreshMyLive();setInterval(refreshMyLive,15000);

async function closeLivePosition(symbol,btn){
  if(!confirm(`${t('alert.closePositionConfirmPrefix')} ${symbol} ${t('alert.closePositionConfirmSuffix')}`)) return;
  btn.disabled=true; btn.textContent=t('liveOpen.closing');
  try{
    const r=await fetch('/api/live/close-position',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol})});
    const d=await r.json();
    if(!d.ok){ alert(d.error||t('alert.closePositionFailed')); btn.disabled=false; btn.textContent=t('liveOpen.closeNowBtn'); return; }
  }catch(e){ alert(t('alert.connectionError')); btn.disabled=false; btn.textContent=t('liveOpen.closeNowBtn'); return; }
  await refreshMyLive();
}

let aiAnalysisCache=null;
function renderAiAnalysis(d){
  aiAnalysisCache=d;
  const body=document.getElementById('aiAnalysisBody');
  if(!d.enabled){ body.innerHTML='<div class="ai-disabled">'+t('ai.disabled')+'</div>'; return; }
  const a=d.analysis;
  if(!a || !a.ok){
    let msg=t('ai.noAnalysisYet');
    if(d.last_error && d.last_error.error){
      msg=t('ai.lastAttemptFailed')+' ('+(d.last_error.at||'').replace('T',' ').slice(0,16)+'): '+d.last_error.error;
    }
    body.innerHTML='<div class="ai-disabled">'+msg.replace(/</g,'&lt;')+'</div>';
    return;
  }
  const meta=`<div class="ai-meta">${(a.generated_at||'').replace('T',' ').slice(0,16)} &middot; ${a.trades_analyzed} ${t('ai.tradesAnalyzedPrefix')} ${a.total_trades_all_time})</div>`;
  body.innerHTML=meta+'<div>'+a.text.replace(/</g,'&lt;')+'</div>';
}
async function refreshAiAnalysis(){
  let d;
  try{ const r=await fetch('/api/ai-analysis',{cache:'no-store'}); d=await r.json(); }catch(e){ return; }
  renderAiAnalysis(d);
}
async function runAiAnalysis(){
  const btn=document.getElementById('aiRunBtn');
  btn.disabled=true; btn.textContent=t('ai.running');
  try{ await fetch('/api/ai-analysis/run',{cache:'no-store'}); }catch(e){}
  await refreshAiAnalysis();
  btn.disabled=false; btn.textContent=t('ai.runBtn');
}
refreshAiAnalysis();setInterval(refreshAiAnalysis,60000);

async function logout(){
  try{ await fetch('/logout',{method:'POST',cache:'no-store'}); }catch(e){}
  window.location='/login';
}

let accountCache=null;
function renderAccount(a){
  accountCache=a;
  const pill=document.getElementById('binanceStatusPill');
  const body=document.getElementById('accountBody');
  let whoText=a.username?('👤 '+a.username):'';
  if(a.is_admin){ whoText+=' <span class="badge-admin">'+t('account.admin')+'</span>'; }
  else if(a.subscription_status==='trial'){ whoText+=` <span class="badge-trial">${t('account.trialDaysLeft').replace('{n}',a.days_left)}</span>`; }
  else if(a.subscription_status==='expired'){ whoText+=' <span class="badge-error">'+t('account.trialExpired')+'</span>'; }
  document.getElementById('whoami').innerHTML=whoText;
  document.getElementById('adminLink').style.display=a.is_admin?'inline-block':'none';
  const emailEl=document.getElementById('acctEmail'); if(emailEl) emailEl.textContent=a.email||t('account.emailNotRegistered');
  if(a.binance_connected){
    if(a.binance_verify_error){ pill.innerHTML='<span class="badge-error">'+t('account.verifyError')+'</span>'; }
    else if(a.binance_verified_at){ pill.innerHTML='<span class="badge-verified">'+t('account.verifiedConnected')+'</span>'; }
    else{ pill.innerHTML='<span class="badge-unverified">'+t('account.connectedNotVerified')+'</span>'; }
  } else {
    pill.innerHTML='<span class="badge-unverified">'+t('account.notConnected')+'</span>';
  }
  let notice='';
  if(!a.credential_encryption_ready){
    notice=`<div class="account-notice">${t('account.credentialWarning')}</div>`;
  }
  const maskedRow=a.binance_connected?`<div class="account-row">${t('account.savedKeyLabel')} <b>${a.binance_key_masked}</b></div>`:'';
  const verifyRow=a.binance_verified_at?`<div class="account-row text-faint">${t('account.lastVerified')} ${a.binance_verified_at.replace('T',' ').slice(0,16)}</div>`
    :(a.binance_verify_error?`<div class="account-row"><span class="badge-error">${a.binance_verify_error}</span></div>`:'');
  const riskRow=a.risk_ack_at
    ? `<div class="account-row text-faint">${t('account.riskAckGiven')}: ${a.risk_ack_at.replace('T',' ').slice(0,16)}</div>`
    : `<label class="risk-ack"><input type="checkbox" id="riskAck"> ${t('account.riskAckLabel')}</label>`;
  body.innerHTML=`
    ${maskedRow}${verifyRow}
    <form class="account-form" id="binanceForm" onsubmit="return submitBinanceForm(event)">
      <div>
        <label>${t('account.apiKeyLabel')}</label>
        <input type="text" id="binApiKey" autocomplete="off" placeholder="${a.binance_connected?t('account.apiKeyPlaceholderChange'):t('account.apiKeyPlaceholderNew')}">
      </div>
      <div>
        <label>${t('account.apiSecretLabel')}</label>
        <input type="password" id="binApiSecret" autocomplete="off" placeholder="${a.binance_connected?t('account.apiSecretPlaceholderChange'):t('account.apiSecretPlaceholderNew')}">
      </div>
      ${riskRow}
      <div class="account-row">
        <button class="btn" type="submit" id="binSaveBtn">${t('account.saveVerifyBtn')}</button>
        ${a.binance_connected?'<button class="btn" type="button" onclick="disconnectBinance()">'+t('account.removeConnectionBtn')+'</button>':''}
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
    box.innerHTML=`<div class="account-notice">${t('live.needConnectFirst')}</div>`;
    return;
  }
  const rt=a.live_runtime||{};
  let statusLine;
  if(a.global_kill_switch_active){
    statusLine=`<span class="badge-live-paused">${t('live.killSwitch')}</span>`;
  } else if(a.live_trading_enabled && rt.paused_today){
    statusLine=`<span class="badge-live-paused">${t('live.pausedToday')}</span>`;
  } else if(a.live_trading_enabled){
    statusLine=`<span class="badge-live-on">${t('live.on')}</span>`;
  } else {
    statusLine=`<span class="badge-live-off">${t('live.off')}</span>`;
  }
  const openPos=rt.open_position_count?`<div class="account-row text-faint">${t('live.openPositionCount')} ${rt.open_position_count}</div>`:'';
  const pnlRow=`<div class="account-row text-faint">${t('live.todayRealizedPnl')} ${(rt.realized_pnl_usd||0).toFixed(2)} USD</div>`;
  const errRow=rt.last_error?`<div class="account-row"><span class="badge-error">${(''+rt.last_error).slice(0,200)}</span></div>`:'';
  box.innerHTML=`
    <div class="account-row">${statusLine}</div>
    ${openPos}${pnlRow}${errRow}
    <form class="account-form" id="liveSettingsForm" onsubmit="return submitLiveSettings(event)" style="margin-top:10px">
      <div>
        <label>${t('live.positionUsdLabel')}</label>
        <input type="number" step="0.01" min="0" id="livePositionUsd" value="${a.live_position_usd||''}" placeholder="${t('live.positionUsdPlaceholder')}">
      </div>
      <div>
        <label>${t('live.maxLeverageLabel').replace('{n}',a.live_max_leverage_cap||10)}</label>
        <input type="number" step="1" min="1" max="${a.live_max_leverage_cap||10}" id="liveMaxLeverage" value="${a.live_max_leverage||''}" placeholder="${t('live.maxLeveragePlaceholder')}">
      </div>
      <div>
        <label>${t('live.dailyLossLimitLabel')}</label>
        <input type="number" step="0.01" min="0" id="liveDailyLossLimit" value="${a.live_daily_loss_limit_usd||''}" placeholder="${t('live.dailyLossLimitPlaceholder')}">
      </div>
      <div>
        <label>${t('live.maxPositionsLabel').replace('{n}',a.live_max_positions_cap||5)}</label>
        <input type="number" step="1" min="1" max="${a.live_max_positions_cap||5}" id="liveMaxPositions" value="${a.live_max_open_positions||''}" placeholder="${t('live.maxPositionsPlaceholder')}">
      </div>
      <div class="account-row">
        <button class="btn" type="submit">${t('live.saveSettingsBtn')}</button>
        ${a.live_trading_enabled
          ? `<button class="btn" type="button" onclick="toggleLiveTrading(false)">${t('live.turnOffBtn')}</button>`
          : `<button class="btn" type="button" onclick="toggleLiveTrading(true)" style="background:var(--bear);border-color:var(--bear-border)">${t('live.turnOnBtn')}</button>`}
      </div>
    </form>
    <div class="live-danger">${t('live.dangerText')}</div>
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
    if(!d.ok){ alert(d.error||t('alert.saveFailedGeneric')); }
  }catch(e){ alert(t('alert.connectionError')); }
  await refreshAccount();
  return false;
}

async function toggleLiveTrading(enabled){
  if(enabled && !confirm(t('live.toggleOnConfirm'))) return;
  try{
    const r=await fetch('/api/account/live-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
    const d=await r.json();
    if(!d.ok){ alert(d.error||t('alert.actionFailed')); }
  }catch(e){ alert(t('alert.connectionError')); }
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
    box.innerHTML=`<div class="account-notice">${t('telegram.notEnabled')}</div>`;
    return;
  }
  if(a.telegram_linked){
    if(_tgCodeTimer){ clearInterval(_tgCodeTimer); _tgCodeTimer=null; }
    _tgActiveCode=null;
    box.innerHTML=`
      <div class="account-row">${t('telegram.linked')}${a.telegram_username?(' — @'+a.telegram_username):''}</div>
      <div class="account-row text-faint">${t('telegram.notificationsDesc')}</div>
      <div class="account-row"><button class="btn" type="button" onclick="unlinkTelegram()">${t('telegram.removeConnectionBtn')}</button></div>
    `;
    return;
  }
  if(_tgActiveCode){
    renderTelegramCodeBox();
    return;
  }
  box.innerHTML=`
    <div class="account-row text-faint">${t('telegram.notLinkedDesc')}</div>
    <div class="account-row"><button class="btn" type="button" id="tgLinkBtn" onclick="getTelegramLinkCode()">${t('telegram.getCodeBtn')}</button></div>
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
      ${t('telegram.step1')} ${botLink?`<a href="${botLink}" target="_blank" style="color:var(--accent)">@${bot_username}</a>`:t('telegram.step1Fallback')} ${t('telegram.step1End')}<br>
      ${t('telegram.step2')} <span class="tg-code">/start ${code}</span><br>
      <span class="text-faint">${t('telegram.codeExpiresPrefix')} ${Math.floor(remaining/60)} ${t('telegram.minutes')} ${remaining%60} ${t('telegram.seconds')}</span>
    </div>`;
}

async function getTelegramLinkCode(){
  const btn=document.getElementById('tgLinkBtn');
  if(btn){ btn.disabled=true; }
  try{
    const r=await fetch('/api/account/telegram/link-code',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(!d.ok){ alert(d.error||t('alert.codeNotObtained')); if(btn) btn.disabled=false; return; }
    _tgActiveCode={code:d.code, bot_username:d.bot_username, obtainedAt:Date.now(), ttlSeconds:d.expires_in_seconds||600};
    renderTelegramCodeBox();
    if(_tgCodeTimer) clearInterval(_tgCodeTimer);
    _tgCodeTimer=setInterval(async ()=>{
      if(!_tgActiveCode){ clearInterval(_tgCodeTimer); return; }
      const remaining=_tgActiveCode.ttlSeconds - Math.floor((Date.now()-_tgActiveCode.obtainedAt)/1000);
      if(remaining<=0){ clearInterval(_tgCodeTimer); _tgActiveCode=null; await refreshAccount(); return; }
      await refreshAccount(); // re-renders; if /start already landed, telegram_linked flips to true
    },4000);
  }catch(e){ alert(t('alert.connectionError')); }
  if(btn) btn.disabled=false;
}

async function unlinkTelegram(){
  if(!confirm(t('telegram.unlinkConfirm'))) return;
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
  if(!key||!secret){ alert(t('alert.apiKeySecretRequired')); return false; }
  if(riskEl && !riskAck){ alert(t('alert.riskAckRequired')); return false; }
  const btn=document.getElementById('binSaveBtn');
  btn.disabled=true; btn.textContent=t('account.savingVerifying');
  try{
    const r=await fetch('/api/account/connect-binance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key,api_secret:secret,risk_ack:riskAck})});
    const d=await r.json();
    if(!d.ok){ alert(d.error||t('alert.saveFailedGeneric')); }
  }catch(e){ alert(t('alert.connectionError')); }
  btn.disabled=false; btn.textContent=t('account.saveVerifyBtn');
  await refreshAccount();
  return false;
}

async function disconnectBinance(){
  if(!confirm(t('alert.disconnectBinanceConfirm'))) return;
  try{ await fetch('/api/account/disconnect-binance',{method:'POST',cache:'no-store'}); }catch(e){}
  await refreshAccount();
}

applyTranslation(_initialLang);
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
.auth-card.wide{max-width:640px;text-align:left}
.text-faint{color:var(--text-faint)}
.account-notice{background:var(--accent-soft);border:1px solid #4a3d22;color:var(--accent);border-radius:8px;padding:10px 12px;font-size:12px;margin-top:10px;line-height:1.55}
.account-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px;align-items:center}
.btn{background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 15px;font-size:13px;font-weight:600;cursor:pointer;font-family:var(--font-d)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn-primary{background:var(--accent);color:#1a1406;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent);color:#1a1406;opacity:.9}
.stack-gap{display:flex;flex-direction:column;gap:10px}
.section-title{font-size:15px;font-weight:700;margin:0 0 6px 0}
.plan-choices{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
.plan-btn{flex:1;min-width:140px;background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;padding:14px;cursor:pointer;text-align:left;color:var(--text);font-family:var(--font-d)}
.plan-btn:hover{border-color:var(--accent)}
.plan-btn.selected{border-color:var(--accent);background:var(--accent-soft)}
.plan-btn .plan-name{font-size:13px;font-weight:600;color:var(--text-dim)}
.plan-btn .plan-price{display:block;font-family:var(--font-m);font-size:18px;font-weight:700;color:var(--accent);margin-top:4px}
.iban-box{background:var(--bg-elev);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:4px}
.iban-box .iban-num{font-family:var(--font-m);font-size:15px;font-weight:700;letter-spacing:1px;word-break:break-all;color:var(--text)}
.ask-box textarea{width:100%;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:13px;font-family:var(--font-d);min-height:100px;resize:vertical;box-sizing:border-box}
.faq-item{border-bottom:1px solid var(--border-soft);padding:10px 0}
.faq-item:last-child{border-bottom:none}
.faq-item summary{cursor:pointer;font-weight:600;font-size:13.5px;list-style:none}
.faq-item summary::-webkit-details-marker{display:none}
.faq-item summary::before{content:'+ ';color:var(--accent);font-weight:700}
.faq-item[open] summary::before{content:'\2013 '}
.faq-item p{margin:8px 0 0 0;font-size:13px;color:var(--text-dim);line-height:1.6}
.link-btn{background:none;border:none;color:var(--accent);cursor:pointer;font-size:12.5px;padding:0;text-decoration:underline;font-family:var(--font-d)}
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
  <div class="auth-switch" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--border)"><a href="/">← Anasayfaya Dön</a></div>
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
<div class="auth-page"><div class="auth-card wide">
  <h1>Deneme süreniz doldu</h1>
  <p class="sub">__USERNAME__ hesabınızın ücretsiz deneme süresi sona erdi. Panele tekrar erişmek için aşağıdan aboneliğinizi aktive edebilir ya da bir sorunuz varsa admin'e yazabilirsiniz.</p>
  ''' + ACCOUNT_EXTRAS_HTML + r'''
  <div style="margin-top:22px;padding-top:16px;border-top:1px solid var(--border)">
    <button class="btn" onclick="logout()">Çıkış Yap</button>
  </div>
</div></div>
<script>
async function logout(){ try{ await fetch('/logout',{method:'POST'}); }catch(e){} window.location='/login'; }
// On this page, subscribing is the primary action, so open it by default
// instead of making the person click "Abonelik Sistemine Geç" first.
document.getElementById('subscriptionBox').style.display='block';
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
.btn-danger{border-color:#7a2a2a;color:#ff8080}
.btn-danger:hover{border-color:#ff5c5c;color:#ff5c5c;background:rgba(255,92,92,0.08)}
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
    <thead><tr><th>Kullanıcı</th><th>E-posta</th><th>Giriş türü</th><th>Binance</th><th>Durum</th><th>Kalan gün</th><th>Canlı işlem</th><th>Bekleyen ödeme</th><th>İşlem</th><th></th></tr></thead>
    <tbody id="tbody"><tr><td colspan="10">Yükleniyor…</td></tr></tbody>
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
  if(r.status===403){ document.getElementById('tbody').innerHTML='<tr><td colspan="10">Bu sayfaya erişim yetkiniz yok.</td></tr>'; return; }
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
      <td>${u.pending_plan?`<span class="badge badge-trial">${u.pending_plan==='monthly'?'Aylık':'Yıllık'} · ${u.pending_amount_usd} USD</span> <span class="text-faint">${(u.pending_requested_at||'').replace('T',' ').slice(0,16)}</span>`:'<span class="text-faint">—</span>'}</td>
      <td>
        <select onchange="setStatus('${u.username}', this.value)">
          <option value="trial" ${u.payment_status==='trial'?'selected':''}>Deneme</option>
          <option value="active" ${u.payment_status==='active'?'selected':''}>Aktif (ödedi)</option>
          <option value="expired" ${u.payment_status==='expired'?'selected':''}>Süresi doldu</option>
          <option value="inactive" ${u.payment_status==='inactive'?'selected':''}>Pasif</option>
        </select>
      </td>
      <td>${u.is_admin?'':`<button class="btn btn-danger" onclick="deleteUser('${u.username}')">Sil</button>`}</td>
    </tr>`).join('');
  document.getElementById('tbody').innerHTML = rows || '<tr><td colspan="10">Henüz kullanıcı yok.</td></tr>';
}
async function setStatus(username, status){
  await fetch('/api/admin/set-status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,status})});
  load();
}
async function deleteUser(username){
  if(!confirm(`"${username}" kullanıcısını KALICI olarak silmek istediğinize emin misiniz? Bu işlem geri alınamaz.`)) return;
  const r=await fetch('/api/admin/delete-user',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username})});
  const d=await r.json();
  if(!d.ok){ alert(d.error||'Silinemedi.'); }
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

    def _serve_static(self, path):
        """Serves a whitelisted file from the local static/ folder (marketing
        assets — landing-page image/video). Supports HTTP Range requests
        (Range: bytes=start-end) because browsers request video in chunks
        for seeking/scrubbing; without this, <video> playback works but
        the seek bar doesn't. Public — no auth required, same as the rest
        of the logged-out landing page."""
        name = path[len('/static/'):]
        # No path traversal: only a bare filename from our fixed whitelist,
        # never anything containing '/' or '..'.
        allowed = {
            'tanitim.mp4': 'video/mp4',
            'tanitim-infografik.png': 'image/png',
        }
        content_type = allowed.get(name)
        if not content_type or '/' in name or '..' in name:
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers(); return
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', name)
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers(); return

        start, end = 0, file_size - 1
        status = 200
        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            try:
                rng = range_header.split('=', 1)[1].split('-')
                if rng[0]:
                    start = int(rng[0])
                if len(rng) > 1 and rng[1]:
                    end = int(rng[1])
                end = min(end, file_size - 1)
                if start > end or start < 0:
                    raise ValueError
                status = 206
            except (ValueError, IndexError):
                start, end, status = 0, file_size - 1, 200

        length = end - start + 1
        with open(file_path, 'rb') as f:
            f.seek(start)
            data = f.read(length)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Content-Length', str(len(data)))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client aborted/seeked away mid-transfer — not an error

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

        if path.startswith('/static/'):
            self._serve_static(path); return

        # Public marketing homepage: a logged-out visitor hitting "/" sees
        # the Herobot-ai landing page instead of being bounced to /login.
        # A logged-in user still falls through to the normal dashboard
        # further down — this only intercepts the logged-out case.
        if path=='/' and not self._current_user():
            self._send_html(LANDING_HTML); return

        if path=='/tanitim':
            self._send_html(TANITIM_HTML); return

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
                html = (TRIAL_EXPIRED_HTML.replace('__USERNAME__', user)
                        .replace('__IBAN__', SUBSCRIPTION_IBAN).replace('__IBAN_HOLDER__', SUBSCRIPTION_IBAN_HOLDER))
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
        html=(HTML.replace('__USERNAME__', self._current_user() or '')
              .replace('__IBAN__', SUBSCRIPTION_IBAN).replace('__IBAN_HOLDER__', SUBSCRIPTION_IBAN_HOLDER))
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

        if path=='/api/admin/delete-user':
            if not auth.is_admin(user):
                self._send_json({'error': 'forbidden'}, status=403); return
            data=self._read_json_body()
            target=(data.get('username') or '').strip()
            if not target:
                self._send_json({'ok': False, 'error': 'Kullanıcı adı gerekli.'}, status=400); return
            ok, err = auth.delete_user(target)
            self._send_json({'ok': ok, 'error': err}, status=200 if ok else 400); return

        if path=='/api/account/ask-admin':
            message=(self._read_json_body().get('message') or '').strip()
            if not message:
                self._send_json({'ok': False, 'error': 'Lütfen bir soru/mesaj yazın.'}, status=400); return
            if len(message) > 4000:
                message = message[:4000]
            acct = auth.get_account_status(user)
            ok, err = email_notifier.send_admin_question(user, acct.get('email'), message)
            if not ok and err == 'not configured':
                self._send_json({'ok': False, 'error': 'Sunucuda e-posta gönderimi yapılandırılmamış. Lütfen doğrudan herobotai.int@gmail.com adresine yazın.'}, status=200); return
            self._send_json({'ok': ok, 'error': err}); return

        if path=='/api/account/subscription-request':
            data=self._read_json_body()
            plan=(data.get('plan') or '').strip().lower()
            plans={'monthly': ('Aylık', 50), 'annual': ('Yıllık', 500)}
            if plan not in plans:
                self._send_json({'ok': False, 'error': 'Geçersiz plan.'}, status=400); return
            plan_label, amount_usd = plans[plan]
            auth.record_subscription_request(user, plan, amount_usd)
            acct = auth.get_account_status(user)
            ok, err = email_notifier.send_subscription_payment_notice(user, acct.get('email'), plan_label, amount_usd)
            if not ok and err == 'not configured':
                # The request is still recorded (visible in the admin panel)
                # even if outbound e-mail isn't set up on this server.
                self._send_json({'ok': True, 'error': None}); return
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

def trial_expiry_notify_loop():
    """Checks every few minutes for users whose free trial just ran out and
    emails the admin inbox once per user (auth.list_users_needing_trial_expiry_notice
    only returns users not yet flagged, and we flag them right after sending
    — see auth.mark_trial_expiry_notified — so this is safe to poll on a
    short interval without spamming). Wrapped so a bad email config or a
    transient error can never take the whole loop down."""
    while True:
        try:
            for u in auth.list_users_needing_trial_expiry_notice():
                username = u['username']
                ok, err = email_notifier.send_trial_expired_notice(username, u.get('email'))
                if not ok and err != 'not configured':
                    print(f'[email] deneme süresi bildirimi gönderilemedi ({username}): {err}', flush=True)
                # Mark as notified even if e-mail sending is unconfigured/failed —
                # otherwise a persistently broken SMTP config would retry (and
                # log) the same user forever. The admin panel's "Bekleyen ödeme"
                # / "Durum" columns remain the source of truth regardless.
                auth.mark_trial_expiry_notified(username)
        except Exception as e:
            print(f'[trial_expiry_notify_loop] {type(e).__name__}: {e}', flush=True)
        time.sleep(1800)  # 30 dakika

def start_dashboard():
    server=ThreadingHTTPServer(('0.0.0.0',PORT),Handler)
    print(f'DASHBOARD | http://0.0.0.0:{PORT} | PAPER ONLY + COIN SCANNER',flush=True)
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=bist_background_loop, daemon=True).start()
    threading.Thread(target=us_background_loop, daemon=True).start()
    threading.Thread(target=telegram_link.poll_loop, daemon=True).start()
    threading.Thread(target=trial_expiry_notify_loop, daemon=True).start()
    server.serve_forever()
