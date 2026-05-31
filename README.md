# KOBİNET — Render Deployment Rehberi

## Render'da Canlıya Alma

### Adım 1 — GitHub'a yükle
```bash
git init
git add .
git commit -m "KOBİNET v1"
git remote add origin https://github.com/kullanicin/kobinet.git
git push -u origin main
```

### Adım 2 — Render'da yeni Web Service
1. https://render.com → New → Web Service
2. GitHub reponuzu bağlayın
3. Ayarlar:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

### Adım 3 — PostgreSQL ekle
1. Render Dashboard → New → PostgreSQL → Free plan
2. Database oluşturulduktan sonra **Internal Database URL**'yi kopyalayın
3. Web Service → Environment → `DATABASE_URL` = kopyaladığınız URL

### Adım 4 — Environment Variables
Render Dashboard → Environment'a şunları ekleyin:

| Değişken | Değer |
|---|---|
| `SECRET_KEY` | Rastgele uzun string (Render generate edebilir) |
| `DATABASE_URL` | PostgreSQL bağlantı URL'si |
| `ADMIN_EMAIL` | admin@sirketiniz.com |
| `ADMIN_PASSWORD` | Güçlü bir şifre |
| `TELEGRAM_TOKEN` | BotFather'dan alınan token |
| `TELEGRAM_CHAT_ID` | @kanaladin |
| `MAIL_USERNAME` | Gmail adresiniz |
| `MAIL_PASSWORD` | Gmail uygulama şifresi |

### Adım 5 — Deploy
Render otomatik deploy eder. İlk deploy'da admin hesabı otomatik oluşur.

### Admin Paneli
- **URL:** https://siteniz.onrender.com/admin-panel/dashboard
- **E-posta:** `ADMIN_EMAIL` değişkenindeki değer
- **Şifre:** `ADMIN_PASSWORD` değişkenindeki değer

### Dosya Yüklemeleri Hakkında
Render'ın ücretsiz planında disk kalıcı değil — her deploy'da sıfırlanır.
Kalıcı dosya depolama için:
- Render Disk ekleyin (aylık $0.25/GB) ve `UPLOAD_PATH=/var/data/uploads` ekleyin
- Veya Cloudinary entegrasyonu yapın (ücretsiz 25GB)

### Notlar
- Ücretsiz Render planında servis 15 dakika işlem görmezse uyur
- İlk istek ~30 saniye sürebilir (cold start)
- PostgreSQL ücretsiz planda 1GB limit var
