# KOBİNET v6 — Onarım Notları (uyumsuzluk giderme)

v6'da base.html yeni bir tasarım sistemine (Space Grotesk + Geist, ink/blue/slate
paleti, yeni class isimleri) geçirilmişti; ancak yalnızca `index.html` yeni
vokabülere taşınmıştı. Diğer 15 şablon hâlâ eski class isimlerini kullandığı
için ekranlar stilsiz/dağılmış görünüyordu.

## 1) CSS uyumsuzluğu — base.html'e uyumluluk katmanı eklendi
Eski isimler yeni tasarım token'larına eşlendi (tek dosya, tüm ekranlar tutarlı):
- Butonlar: `btn-dark` → ink, `btn-success`/`btn-green` → buy, `btn-red` → danger,
  `btn-gold` → sell, `btn-cyan` → blue
- Kart: `card` / `card-head` / `card-body` / `card-foot` (padding'siz kapsayıcı + bölümler)
- Rozetler: `badge-buy/sell/sector/city`
- Yerleşim: `two-col`, `three-col`, `form-grid`, `wrap-md`, `hr`, `sh`
- Yardımcılar: `form-hint`, `empty`, `eyebrow`, `ml-1`
- Eski pager kalıbı: çıplak `<a>`, `span.on`, `span.dots`, `.nav-p`

Etkilenen şablonlar: create, edit_profile, forgot_password, listing, login,
message, messages, my_offers, profile, register, reset_password, saved_searches,
admin_dashboard, admin_listings, admin_users.

## 2) Fonksiyonel regresyon — e-posta doğrulama kilidi
v6'da ilan verme/teklif için `email_verified` şartı eklenmişti. Mail
yapılandırılmamışsa (Render varsayılanı) kullanıcı asla doğrulanamadığı için
HİÇ ilan veremiyordu. Düzeltme: mail kurulu değilse kayıt anında otomatik
doğrula; mail kuruluysa normal doğrulama akışı korunur. (app.py → register)

## Doğrulama
- 26 route'un tamamı GET/POST 200/302 (CSRF token'larıyla)
- Tanımsız CSS class: 0

---

# İkinci tur — kullanıcı testinde çıkan sorunlar

## 3) CSS değişkeni uyumsuzluğu (asıl görsel bozukluk)
base.html yeniden tasarlanınca eski değişken isimleri (`--gray-*`, `--white`,
`--accent`, `--radius*`) kaldırılmıştı; 12 şablonun kendi <style> blokları hâlâ
bunları kullandığı için tüm kenarlık/renk/köşe stilleri tarayıcı varsayılanına
düşüyordu. Belirtiler: "ürün alıyorum/satıyorum" radyo seçimi belli olmuyor;
mesajlar ekranında kutu/çizgi yok. Düzeltme: base.html :root'a eski→yeni
değişken eşleme bloğu eklendi. Ayrıca admin sayfalarının koyu tema değişkenleri
(`--bg-card`, `--gold`, `--red`, `--text`...) tanımlandı (eski sürümde de
tanımsızdı). Sonuç: tanımsız CSS değişkeni = 0.

## 4) Dosya yükleme — "0 dosya eklendi" sessiz hatası
Geçersiz format / bozuk dosya / hiç seçilmemiş dosya durumlarında kullanıcı
nedeni göremiyordu. Düzeltme:
- Yükleme sonucu net mesaj (`_flash_upload_result`): "Önce bir dosya seçin",
  "N eklendi · M reddedildi", "Dosya eklenemedi: geçersiz/bozuk... HEIC desteklenmez".
- İlan detayında seçilen dosya adını gösteren JS (gizli input artık sessiz değil).
- create() içindeki yükleme döngüsü de reddedilenleri raporluyor.

## 5) E-posta doğrulama — yapılandırılabilir kapı
`REQUIRE_EMAIL_VERIFICATION` ayarı eklendi:
- Boş → yalnızca mail kuruluyken zorunlu (varsayılan akıllı davranış).
- true → her zaman zorunlu · false → asla zorunlu değil.
Kayıt, login uyarısı, /create, teklif gate'leri ve verify-banner bu ayara bağlandı.

---

# Üçüncü tur — kullanıcı testi (ikinci parti)

## 6) Şifre placeholder uyumsuzluğu
register.html "En az 6 karakter" yazıyordu ama kural 8. Placeholder "En az 8
karakter" yapıldı + minlength=8 eklendi.

## 7) Yüklenen resim açılamıyordu
İlan detayında foto thumbnail'ı bir linke sarılı değildi (PDF'ler sarılıydı).
`<img>` artık `<a target="_blank">` içinde — owner ve alıcı tam boyut açabilir.
(/uploads/listings/<f> route'u zaten herkese açıktı.)

## 8) E-posta doğrulama — eski davranış geri getirildi
Eski sürümde doğrulama KAPISI yoktu (kayıt → anında ilan). v6 bunu eklemişti.
REQUIRE_EMAIL_VERIFICATION artık varsayılan KAPALI → eski akış. İstenirse
true ile açılır (SMTP gerekir).

## 9) Tekliflerim ekranı yeniden tasarlandı (gelen teklifler + ürün filtresi)
Sorun: nav'daki "Tekliflerim N" rozeti ilanlara GELEN teklifleri sayıyordu ama
sayfa kullanıcının VERDİĞİ teklifleri gösteriyordu (badge 1, sayfa 0 → tutarsız).
Yeni:
- İki sekme: "Aldığım Teklifler" (varsayılan) ve "Verdiğim Teklifler".
- Aldığım sekmesinde ürün bazlı filtre çubuğu (her ilan + teklif sayısı).
- Teklif kartı: teklif veren firma (profile link), fiyat, mesaj, tarih,
  "Mesaj Gönder" + "İlana Git".
- Sayfa açılınca last_seen_offers güncellenir → nav rozeti temizlenir.

## 10) Favicon (logo)
base.html'e SVG data-URI favicon eklendi (KOBİNET logosu) — tarayıcı sekmesinde
artık jenerik dünya ikonu yerine logo görünür.

---

# Dördüncü tur — doğrulama düzeltmesi (yön değişti)

## 11) E-posta doğrulama ZORUNLU hale getirildi + mailsiz akış
Kullanıcı "doğrulamadan ilan verebiliyorum, olmamalı" dedi → REQUIRE_EMAIL_VERIFICATION
varsayılanı AÇIK yapıldı. Tek engel: yerelde SMTP yok → link ulaşamıyordu.
Çözüm: SMTP kurulu DEĞİLSE doğrulama linki sayfanın üstündeki bantta doğrudan
"Hesabı Doğrula →" olarak gösterilir (dev_verify_link context processor).
SMTP kuruluysa link normal e-postayla gider, bant "Doğrulama Mailini Gönder" der.
Akış: kayıt → doğrulanmamış → /create-teklif YASAK → banttan/maillen doğrula →
açılır. REQUIRE_EMAIL_VERIFICATION=false ile tümüyle kapatılabilir.
