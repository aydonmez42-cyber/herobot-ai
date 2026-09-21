# BIST Sinyal Tarayıcı

Basit ve şeffaf bir **ADX DI+/DI- kesişim** sistemi kullanır:

- **DI+ (yeşil)**, **DI-'yi (kırmızı)** yukarı keserse → **AL** sinyali
- **DI-**, **DI+'yi** yukarı keserse → **SAT** sinyali
- **ADX** yönü değil, o anki trendin **gücünü** ölçer; "Minimum ADX" filtresiyle
  yatay/kararsız piyasadaki gürültülü kesişimler elenebilir.

Her akşam tüm Borsa İstanbul hisselerini günlük barlarda tarar, AL veya SAT
sinyali üretenleri Telegram'a gönderir. Ayrıca web dashboard'u var (BIST +
Binance Futures kripto).

Bu repo GitHub + Railway kurulumu için hazırlandı.

---

## Dosyalar

```
├── README.md                    bu dosya
├── requirements.txt             Python bağımlılıkları
├── runtime.txt                  Python sürümü (3.12)
├── .gitignore                   .env ve önbelleği repo dışında tutar
├── railway.scanner.json         cron servisinin ayarları
├── railway.dashboard.json       web servisinin ayarları
├── .streamlit/
│   └── config.toml              dashboard teması + statik dosya sunumu
└── bist_screener/
    ├── __init__.py
    ├── pine.py                  Pine Script fonksiyonlarının Python karşılıkları
    ├── engine.py                DI+/DI- (ADX/DMI) kesişim sinyal motoru
    ├── data.py                  BIST sembol listesi ve veri indirme
    ├── scan.py                  tarama döngüsü (terminalden de çalışır)
    ├── notify.py                Telegram gönderimi
    ├── daily.py                 günlük iş — cron bunu çağırır (şu an sadece BIST)
    ├── pwa.py                   telefon uygulaması etiketleri
    ├── crypto_data.py           Binance Futures veri katmanı
    ├── app.py                   Streamlit dashboard (BIST + Kripto)
    └── static/                  manifest.json + uygulama simgeleri
```

Hepsi gerekli. Fazladan bir şey yok.

---

## Sembol listesi nereden geliyor

Bot tam BIST listesini sırayla üç kaynaktan dener:

1. **TradingView screener uç noktası** — kimlik doğrulama istemez, ek paket
   gerektirmez, tüm payları tek istekte döner. Normalde bu çalışır.
2. **isyatirimhisse** paketi — kuruluysa.
3. **Repodaki yedek liste** — 109 hisse.

Dashboard'da özet kartlarının altında hangi kaynağın kullanıldığı yazar. Orada
"yedek liste" görüyorsanız ilk iki kaynak başarısız olmuş demektir; taranan hisse
sayısı da 106 civarında kalır. Railway loglarına bakmak gerekir.

---

## Adım 1 — Telegram botu

1. Telegram'da **@BotFather**'a `/newbot` yazın, isim verin. Size bir token verir.
2. **@userinfobot**'a herhangi bir mesaj atın. `Id` alanındaki sayı chat id'niz.
   Bildirim bir gruba gidecekse botu gruba ekleyip grubun id'sini kullanın
   (grup id'leri `-100` ile başlar).

Bu iki değeri bir kenara not edin, Adım 3'te gireceksiniz.

---

## Adım 2 — GitHub

1. GitHub'da yeni bir **private** repo açın.
2. Bu klasördeki tüm dosyaları repoya yükleyin (web arayüzünden sürükleyip
   bırakabilirsiniz; `bist_screener` klasörünün yapısını koruyun).

`.gitignore` `.env` dosyasını dışarıda tutar. Zaten Railway'de token'ları
dosyaya değil, panele gireceksiniz.

---

## Adım 3 — Railway: tarayıcı servisi

Railway → **New Project** → **Deploy from GitHub repo** → reponuzu seçin.

Servis oluştuktan sonra **Settings** sekmesinde:

| Ayar | Değer |
|---|---|
| Config-as-code / Railway Config File | `railway.scanner.json` |
| Cron Schedule | `30 15 * * 1-5` |

**Variables** sekmesinde:

| Değişken | Değer |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'dan aldığınız token |
| `TELEGRAM_CHAT_ID` | @userinfobot'tan aldığınız sayı |
| `TZ` | `Europe/Istanbul` |

**Cron neden 15:30?** Railway cron ifadelerini UTC olarak değerlendirir.
18:30 Türkiye saati = 15:30 UTC. `1-5` pazartesi–cuma demektir.

Kaydedin, Railway otomatik deploy eder.

### Test edin

Beklemeden denemek için Settings → Cron Schedule'ı geçici olarak birkaç dakika
sonrasına alın, mesaj gelince asıl değere geri çevirin. Ya da servisi manuel
redeploy edin — start command hemen çalışır.

İlk çalıştırma tüm piyasa için 2–5 dakika sürer.

---

## Adım 4 — Railway: dashboard servisi (isteğe bağlı)

Sadece Telegram bildirimi istiyorsanız bu adımı atlayın.

Aynı projede **New** → **GitHub Repo** → **aynı repoyu** seçin. İkinci servis
oluşur. Settings'te:

| Ayar | Değer |
|---|---|
| Config-as-code / Railway Config File | `railway.dashboard.json` |
| Networking | **Generate Domain** |
| Cron Schedule | **boş bırakın** |

Railway size `xxx.up.railway.app` gibi bir adres verir; telefondan da açılır.
**Bu adresi bir sonraki adımda Auth0'a gireceğiniz için önce burada alın.**

Bu servis 7/24 açık kalır ve saatlik ücretlendirilir. Maliyeti sevmiyorsanız
dashboard'u kendi bilgisayarınızda çalıştırın (aşağıda).

---

## Adım 5 — Üyelik girişi (Auth0)

Dashboard artık paylaşılan tek bir parola yerine gerçek hesaplarla çalışıyor:
e-posta/şifre ile üye olma ve Google ile giriş, aynı ekranda. Bunu Streamlit
kendi başına yapamıyor — sadece OIDC sağlayıcılarla (Google gibi) konuşabiliyor
ve e-posta/şifre için yerleşik bir şeyi yok. **Auth0** bu ikisini tek barındırılan
giriş sayfasında birleştiriyor, ücretsiz katmanı (ayda 25.000 aktif kullanıcı)
bu ölçek için fazlasıyla yeterli.

### 5.1 — Auth0 hesabı ve uygulaması

1. [auth0.com](https://auth0.com) üzerinden ücretsiz hesap açın, bir tenant
   oluşturun (bölge olarak Europe seçmeniz gecikmeyi azaltır).
2. **Applications → Create Application** → isim verin → **Regular Web
   Application** seçin.
3. Açılan uygulamanın **Settings** sekmesinde:
   - **Allowed Callback URLs**: `https://xxx.up.railway.app/oauth2callback`
     (Adım 4'te aldığınız Railway adresi + `/oauth2callback`) — yerelde de
     test edecekseniz virgülle `http://localhost:8501/oauth2callback` ekleyin.
   - **Allowed Logout URLs**: aynı adresin kök hali, `https://xxx.up.railway.app`
   - Sayfanın altında **Save Changes**.
4. Aynı sayfanın üstünde **Domain**, **Client ID**, **Client Secret** değerlerini
   not edin — birazdan Railway'e gireceksiniz.

### 5.2 — E-posta/şifre ve Google'ı açma

1. Sol menüden **Authentication → Database** → varsayılan bağlantı
   (`Username-Password-Authentication`) zaten e-posta/şifreyi destekler,
   **Applications** sekmesinden az önce oluşturduğunuz uygulamayı etkinleştirin.
2. Sol menüden **Authentication → Social → Google** → etkinleştirin ve
   uygulamanızı buraya da bağlayın. Test aşamasında Auth0'ın kendi "Dev Keys"i
   yeterli; gerçek kullanıcı sayısı artınca kendi Google Cloud OAuth
   istemcinizi bağlamanız önerilir (Auth0'ın Google sayfasında adımları var).

Bu ikisi açıkken kullanıcı giriş sayfasında hem e-posta/şifre formunu hem
**Google ile devam et** butonunu birlikte görür.

### 5.3 — Railway'e ortam değişkenlerini ekleme

Dashboard servisinin Variables sekmesine ekleyin (artık `DASHBOARD_PASSWORD`
kullanılmıyor, kaldırabilirsiniz):

| Değişken | Değer |
|---|---|
| `AUTH0_DOMAIN` | Auth0'ın verdiği domain, örn. `sizin-tenant.eu.auth0.com` |
| `AUTH0_CLIENT_ID` | Auth0 uygulamasının Client ID'si |
| `AUTH0_CLIENT_SECRET` | Auth0 uygulamasının Client Secret'ı |
| `REDIRECT_URI` | `https://xxx.up.railway.app/oauth2callback` (Adım 4'teki adresiniz) |
| `COOKIE_SECRET` | rastgele, uzun, kimsenin tahmin edemeyeceği bir metin |
| `ALLOWED_EMAILS` | erişim vereceğiniz e-postalar, virgülle ayrılmış |

`COOKIE_SECRET`'ı terminalde `python3 -c "import secrets; print(secrets.token_hex(32))"`
ile üretebilirsiniz. Bu değeri bir kez belirleyin ve sabit tutun — her
değiştirdiğinizde herkesin oturumu düşer, yeniden giriş yapmaları gerekir.

`ALLOWED_EMAILS` erişimin **tek kontrol noktası**: boş bırakırsanız giriş
yapan hiç kimse dashboard'u göremez (varsayılan kapalı kayıt). İki format
karışık kullanılabilir:

```
ali@gmail.com, ayse@sirket.com, @baskasirket.com
```

**Test/deneme kaçış kapısı — `ALLOWED_EMAILS=*`:** Değeri tam olarak (başka
hiçbir karakter olmadan) tek bir yıldız `*` yaparsanız, giriş yapan **herkes**
dashboard'a erişebilir — izin listesi devre dışı kalır. Bu, "boş = kapalı"
varsayılanını değiştirmez; sadece siz bilinçli olarak `*` yazdığınızda
devreye girer. Sadece kısa süreli test/deneme için kullanın (örn. Auth0
akışının uçtan uca çalıştığını doğrularken); gerçek kullanıcılara açtığınız
an bunu gerçek e-posta listenizle (veya `@domaininiz.com` kalıbıyla)
değiştirin, aksi halde bağlantıyı bilen herkes veriye erişir.

`@baskasirket.com` o alan adının tamamına izin verir. Yeni birine erişim
vermek için bu listeye e-postasını ekleyip Railway'de kaydetmeniz yeterli —
kod değişikliği veya yeniden deploy gerekmez, Railway değişkeni güncelleyince
servisi zaten otomatik yeniden başlatır.

Nasıl çalıştığı: `start_dashboard.sh` container her başladığında bu
değişkenlerden `.streamlit/secrets.toml` dosyasını üretir (Streamlit sırları
sadece bu dosyadan okur, ortam değişkeninden değil), sonra Streamlit'i
başlatır. Dosya diskte sadece çalışırken var olur, repoya hiç girmez.

### 5.4 — Doğrulama

Railway'de servis yeniden başladıktan sonra adresi açın: "Giriş yap / Üye ol"
düğmesi Auth0'ın sayfasına yönlendirmeli. `ALLOWED_EMAILS` listesinde olan bir
e-postayla giriş yapınca dashboard açılır; olmayan bir e-postayla girerseniz
"bu hesaba erişim tanımlı değil" mesajını görürsünüz — bu, izin listesinin
çalıştığının kanıtı.

**Bunun kapsamadığı şey:** bu sadece kimlik doğrulama ve bir izin listesi;
ödeme veya abonelik durumu kontrolü yapmıyor. İleride gerçek bir ödeme akışı
(Stripe vb.) eklemek isterseniz ayrı bir proje olur — şimdilik erişimi elle,
`ALLOWED_EMAILS` listesi üzerinden yönetiyorsunuz.

### Yerelde test etmek

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# dosyayi kendi Auth0 degerlerinizle doldurun, Callback URL'i
# http://localhost:8501/oauth2callback olarak Auth0'a da eklemeyi unutmayin
export ALLOWED_EMAILS="sizin@e-postaniz.com"
streamlit run bist_screener/app.py
```

---

## Telegram mesajı neye benziyor

```
BIST DI+/DI- Tarama · 05.09.2026 18:30
Taranan 612 hisse · 6 sinyal · veri 05.09

🟢 AL — DI+ yukarı kesti
THYAO     312.50  ▲ 2.41%  DI+   31  DI-   19  ADX   34
ASELS      88.75  ▼ 0.62%  DI+   28  DI-   21  ADX   29

🔴 SAT — DI- yukarı kesti
KRDMD      24.06  ▲ 1.18%  DI+   17  DI-   30  ADX   26
```

Ardından tam liste CSV olarak ek dosya şeklinde gelir. Sinyal çıkmadığı günlerde
de mesaj gider. Tarama hata alırsa hata metni Telegram'a düşer — bot sessizce
ölmez.

---

## BIST + Kripto (Binance Futures)

Dashboard'un en üstünde bir **BIST / Kripto** seçici var. Hangisini seçerseniz
tarama, sinyal tablosu, genel görünüm ve hisse/coin detayı o piyasada çalışır —
aynı indikatör motoru, iki farklı veri kaynağı.

**Kripto veri kaynağı** Binance Futures'ın herkese açık uç noktaları
(`fapi.binance.com`), kimlik doğrulama gerektirmez, ek paket kurulumu istemez.
"Tüm Binance Futures" seçeneği USDT-M perpetual sözleşmelerin tamamını tarar;
"Majör Coinler" ağa hiç çıkmadan ~25 büyük coin ile anında çalışır.

**Zaman dilimi (yalnız Kripto).** Kripto seçiliyken kenar çubuğunda **4
Saatlik** / **Günlük** seçimi çıkar — hangisini seçerseniz DI+/DI- kesişimi o
mum periyodunda hesaplanır. 4 Saatlik daha sık ve daha erken kesişim yakalar
ama daha çok yanlış sinyal de üretebilir; Günlük daha az ama daha güvenilir
sinyal verir. İkisi ayrı ayrı önbelleklenir, aralarında geçiş yaptığınızda
yeniden tarama gerekir. BIST tarafı şimdilik yalnızca günlük barda çalışıyor
(yfinance'ten intraday BIST verisi ayrı bir konu; isterseniz ayrıca
ekleyebiliriz), o yüzden BIST seçiliyken bu seçici görünmez.

**Hacim kolonu kripto tarafında USDT cinsindendir**, coin'in kendi biriminde
değil — böylece BTC ile DOGE'nin hacmi aynı ölçekte karşılaştırılabilir. DI+/DI-
kesişim mantığı mutlak bir hacim eşiği kullanmaz (yalnızca fiyat hareketine
bakar), bu yüzden hacim birimi sinyal mantığını etkilemez; sadece "Minimum
hacim" filtresinde kıyaslama için kullanılır.

**Bilinmesi gereken bir risk:** Binance bazı bölgelerden `fapi.binance.com`'a
erişimi kısıtlayabiliyor. Railway sunucunuzun bölgesi engellenmişse kripto
taraması "veri indirilemedi" hatası verir — bu durumda Railway'de farklı bir
bölge denemeniz gerekebilir.

**Şu an dahil olmayan:** Telegram bildirimi ve günlük otomatik tarama
(`daily.py`) hâlâ yalnızca BIST için çalışıyor. Kripto tarafını da otomatik
taramaya eklemek isterseniz ayrıca söyleyin.

---

## Telefona uygulama olarak kurmak (Android)

1. Railway adresinizi telefonda **Chrome** ile açın.
2. Sağ üstteki **⋮** → **Ana ekrana ekle** / **Uygulamayı yükle**.
3. Ana ekranda mum grafiği simgesiyle görünür.

Arayüz telefonda otomatik olarak sadeleşir: kenar çubuğu kapalı açılır, özet
kartları ikişerli dizilir ve 12 kolonlu tablo yerine hisse başına bir kart
listesi gelir. Geniş ekranda tam tablo geri gelir; hangisinin görüneceğine CSS
karar verir, ayar yapmanız gerekmez.

### Adres çubuğu hâlâ görünüyorsa

Chrome'un uygulamayı tam ekran (WebAPK) kurabilmesi için sitenin kökünde bir
service worker olması gerekir. Streamlit dosyaları yalnızca `/app/static/`
altından sunar, o yüzden service worker'ın kapsamı köke ulaşmaz. Sonuç: simge ve
uygulama adı çalışır, ama Chrome bunu tam ekran uygulama yerine kısayol olarak
kurabilir ve üstte ince bir adres çubuğu kalır.

Tam ekran istiyorsanız Streamlit'in önüne kökten dosya sunabilen küçük bir
ters vekil sunucu (Caddy veya nginx) koymak gerekir; bu, dağıtımı Dockerfile'a
çevirmek demektir. Şu anki kurulumu bozmamak için o adımı ayrı tuttum.

---

## Tema

Varsayılan koyu tema `.streamlit/config.toml` dosyasındadır. Sağ üstteki **⋮ →
Settings → Theme** menüsünden açık/koyu arasında anlık geçiş yapabilirsiniz;
arayüz ve grafik ikisine de uyum sağlar.

Açık temayı kalıcı varsayılan yapmak isterseniz `config.toml` içindeki `[theme]`
bloğunu bununla değiştirin:

```toml
[theme]
base = "light"
primaryColor = "#0E8F95"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#F1F4F8"
textColor = "#16202C"
font = "sans serif"
```

---

## Kenar çubuğundaki ayarlar ne işe yarıyor

Panel iki bölüme ayrılmıştır. **Tarama ayarları** değiştiğinde yeniden tarama
gerekir; değiştirip taramadan bırakırsanız sayfa sizi uyarır. **Liste filtresi**
altındakiler mevcut sonuca anında uygulanır.

| Ayar | Ne yapar |
|---|---|
| **Zaman dilimi** (yalnız Kripto) | Taramanın hangi mum periyodunda çalışacağı: 4 Saatlik ya da Günlük. BIST'te bu seçici görünmez, her zaman günlüktür. |
| **Sinyal tazeliği** | DI+/DI- kesişimi, kesildiği barda bir kez tetiklenir. 1 sadece son barı gösterir; 3 yaparsanız son üç barda kesişenler de listeye girer. Kaçırdığınız sinyalleri yakalar, karşılığında liste eskir ve uzar. |
| **Sinyal yönü** | AL (DI+ yukarı kesti) ve/veya SAT (DI- yukarı kesti) sinyallerini gösterip göstermeyeceğinizi seçer. |
| **Minimum ADX** | ADX trendin gücünü ölçer, yönünü değil. 20'nin altı genelde yatay/kararsız piyasadır ve orada DI+/DI- kesişimleri sık yanlış çıkar. 20–25 vermek yatay seyredenleri eler, 0 hepsini geçirir. |
| **Minimum hacim** | Bu hacmin altındaki hisse/coin'leri listeden eler. |

Sistem sabit olarak standart Wilder parametreleriyle çalışır: DI uzunluğu 14,
ADX uzunluğu 14 (Pine'daki `ta.dmi(14, 14)` ile birebir aynı).

---

## Eşikleri ayarlama

`bist_screener/daily.py` dosyasının başındaki sabitler:

```python
MIN_HACIM = 500_000   # bu hacmin altındaki hisseleri eleme
MIN_ADX   = 20        # bu ADX'in altındaki (zayıf trend) sinyaller elenir
LOOKBACK  = 1         # 1 = sadece son kapanmış bar
MAX_SATIR = 40        # mesajda listelenecek azami hisse
```

İlk canlı taramadan sonra listeyi kalabalık bulursanız `MIN_ADX`'i 25-30'a
çekin; boş bulursanız 15'e indirin.
Dosyayı GitHub'da düzenleyip commit'lediğinizde Railway otomatik yeniden deploy eder.

---

## Kendi bilgisayarınızda çalıştırmak

```bash
pip install -r requirements.txt
streamlit run bist_screener/app.py        # dashboard
python -m bist_screener.scan              # terminalden tarama
```

Telegram'ı yerelde denemek için proje kökünde bir `.env` dosyası açın:

```
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

```bash
python -m bist_screener.daily --test      # bağlantıyı doğrula
python -m bist_screener.daily --zorla     # tam taramayı çalıştır
```

`.env` dosyası `.gitignore`'da, repoya gitmez.

---

## Railway cron'un iki kuralı

1. **Servis işini bitirince çıkmak zorunda.** `daily.py` öyle çalışıyor, ama
   Railway panelinde bir çalıştırma "Active" takılı görünüyorsa sonraki
   tetiklemeler atlanır. Zamanlama durursa ilk oraya bakın.
2. **Minimum aralık 5 dakika.** Günde bir çalıştırma için sorun değil.

`railway.scanner.json` içinde `restartPolicyType` bilerek `NEVER` yapıldı.
Railway'in varsayılanı başarısız bir çalıştırmayı 10 kez tekrarlar — bu da bir
hatada 10 tane hata mesajı demek olurdu.

---

## Bilinmesi gereken fark

**Veri kaynağı.** yfinance BIST verisi düzeltilmemiş gelir. Temettü ve
bedelsizlerde fiyat serisi TradingView'in düzeltilmiş serisinden hafifçe
sapabilir; bu da DI+/DI- değerlerini birkaç puan oynatabilir. İlk kurulumda
birkaç hisseyi TradingView'in ADX/DMI göstergesiyle karşılaştırıp
doğrulamanız iyi olur.

Motorun doğruluğu için DI+/DI-/ADX hesabı, Wilder'ın orijinal True Range/RMA
yumuşatma yöntemiyle (Pine'daki `ta.dmi(14, 14)` ile birebir aynı formül)
manuel referans hesaba karşı test edildi.

---

Tarama sonuçları bir ön eleme aracıdır, yatırım tavsiyesi değildir.
