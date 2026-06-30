import os, uuid, json, re, logging, threading, hmac, click
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, redirect, request,
                   flash, url_for, abort, jsonify, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         login_required, logout_user, current_user)
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_mail import Mail, Message as MailMessage
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kobinet")

LISTING_DAYS   = 30

# Render'da /opt/render/project/src altında çalışır
# Upload klasörleri — Render Disk kullanıyorsan /var/data olarak ayarla
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
UPLOAD_BASE    = os.getenv("UPLOAD_PATH", os.path.join(BASE_DIR, "static", "uploads"))
UPLOAD_LOGOS   = os.path.join(UPLOAD_BASE, "logos")
UPLOAD_LISTINGS= os.path.join(UPLOAD_BASE, "listings")

# Klasörleri oluştur
os.makedirs(UPLOAD_LOGOS,    exist_ok=True)
os.makedirs(UPLOAD_LISTINGS, exist_ok=True)
ALLOWED_IMG    = {"png","jpg","jpeg","gif","webp"}
ALLOWED_FILES  = {"png","jpg","jpeg","gif","webp","pdf"}
MAX_FILE_MB    = 8

# ══════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "DEGISTIR-PRODUCTION-DA")

db_url = os.getenv("DATABASE_URL","")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://","postgresql://",1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url or "sqlite:///db.sqlite"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024

# ── Oturum çerezi güvenliği ───────────────────
# IS_PRODUCTION: Render'da RENDER env değişkeni otomatik set edilir
IS_PRODUCTION = bool(os.getenv("RENDER") or os.getenv("PRODUCTION"))
app.config["SESSION_COOKIE_HTTPONLY"] = True            # JS çerezi okuyamaz (XSS hafifletme)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"           # cross-site POST'larda çerez gitmez
app.config["SESSION_COOKIE_SECURE"]   = IS_PRODUCTION   # sadece HTTPS üzerinden
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SECURE"]   = IS_PRODUCTION

# Render reverse proxy arkasında doğru scheme/IP görmek için
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── CSRF koruması ─────────────────────────────
csrf = CSRFProtect(app)

# ── Rate limiting (brute force koruması) ──────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],                  # global limit yok, sadece işaretli route'lar
    storage_uri="memory://",            # çoklu worker'da Redis'e geçirilmeli
)

# Flask-Mail (.env'den okunur)
app.config["MAIL_SERVER"]   = os.getenv("MAIL_SERVER",  "smtp.gmail.com")
app.config["MAIL_PORT"]     = int(os.getenv("MAIL_PORT","587"))
# TLS (587) varsayılan; SSL (465) için MAIL_USE_SSL=true ver. İkisi aynı anda olmaz.
_use_ssl = os.getenv("MAIL_USE_SSL","").lower() in ("1","true","yes")
app.config["MAIL_USE_SSL"]  = _use_ssl
app.config["MAIL_USE_TLS"]  = (not _use_ssl) and os.getenv("MAIL_USE_TLS","true").lower() in ("1","true","yes")
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME","")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD","")
# Gönderen adresi kullanıcı adından farklı olabilir (kurumsal mailde sık).
# MAIL_DEFAULT_SENDER verilmezse MAIL_USERNAME kullanılır.
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER") or os.getenv("MAIL_USERNAME") or "noreply@kobinet.com"
# Mail yapılandırılmış mı? (sağlayıcıdan bağımsız tek kontrol noktası)
app.config["MAIL_CONFIGURED"] = bool(app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"])
# E-posta doğrulama zorunlu mu?
#   - Varsayılan: AÇIK (doğrulamadan ilan/teklif YASAK).
#   - SMTP kuruluyken doğrulama linki e-postayla gider.
#   - SMTP kurulu DEĞİLSE link üstteki banttan tıklanarak doğrulanır (geliştirme modu).
#   - REQUIRE_EMAIL_VERIFICATION=false ile tamamen kapatılabilir.
_req_env = os.getenv("REQUIRE_EMAIL_VERIFICATION", "").lower()
if _req_env in ("0","false","no"):
    app.config["REQUIRE_EMAIL_VERIFICATION"] = False
else:
    app.config["REQUIRE_EMAIL_VERIFICATION"] = True

db   = SQLAlchemy(app)
mail = Mail(app)

# ── Veritabanı migrasyonları (Alembic) ────────
# Şema değişiklikleri artık db.create_all() ile değil migration ile yönetilir:
#   flask db migrate -m "açıklama"   → değişikliği algıla, script üret
#   flask db upgrade                 → veritabanına uygula
from flask_migrate import Migrate
migrate = Migrate(app, db)

# ══════════════════════════════════════════════
# LOGIN
# ══════════════════════════════════════════════
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view    = "login"
login_manager.login_message = "Giriş yapmalısınız."

# ══════════════════════════════════════════════
# HELPERS — dosya yükleme
# ══════════════════════════════════════════════
def allowed(filename, allowed_set):
    return "." in filename and filename.rsplit(".",1)[1].lower() in allowed_set

def _is_real_image(file_storage):
    """Uzantı yetmez — dosyanın gerçekten görsel olduğunu Pillow ile doğrula."""
    try:
        pos = file_storage.stream.tell()
        img = Image.open(file_storage.stream)
        img.verify()                      # bozuk/sahte dosyada exception fırlatır
        file_storage.stream.seek(pos)     # stream'i başa sar, save() tekrar okuyacak
        return True
    except Exception:
        try: file_storage.stream.seek(0)
        except Exception: pass
        return False

def _is_real_pdf(file_storage):
    """PDF magic byte kontrolü: dosya %PDF ile başlamalı."""
    try:
        pos = file_storage.stream.tell()
        head = file_storage.stream.read(5)
        file_storage.stream.seek(pos)
        return head.startswith(b"%PDF")
    except Exception:
        return False

def save_file(file, folder, allowed_set):
    """Dosyayı kaydet, unique isim döndür. Hata varsa None döner."""
    if not file or file.filename == "":
        return None
    if not allowed(file.filename, allowed_set):
        return None
    ext  = file.filename.rsplit(".",1)[1].lower()
    # İçerik doğrulama: uzantısı ne derse desin, içeriğe bak
    if ext in ALLOWED_IMG and not _is_real_image(file):
        log.warning("Sahte görsel reddedildi: %s", file.filename)
        return None
    if ext == "pdf" and not _is_real_pdf(file):
        log.warning("Sahte PDF reddedildi: %s", file.filename)
        return None
    name = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, name))
    return name

def _flash_upload_result(added, rejected, present):
    """Yükleme sonucunu kullanıcının anlayacağı şekilde bildir."""
    if present == 0:
        flash("Önce bir dosya seçin — 'Yükle'ye basmadan önce dosya seçili olmalı.","danger")
    elif added and rejected:
        flash(f"{added} dosya eklendi · {rejected} dosya reddedildi "
              f"(geçersiz format veya bozuk dosya).","warning")
    elif added:
        flash(f"{added} dosya eklendi.","success")
    else:
        flash(f"Dosya eklenemedi ({rejected} reddedildi): geçersiz format veya bozuk dosya. "
              f"İzin verilen: PNG, JPG, GIF, WEBP, PDF (maks {MAX_FILE_MB}MB). "
              f"iPhone HEIC fotoğrafları desteklenmez — JPG/PNG olarak kaydedip yükleyin.","danger")

def validate_tax_no(tax_no: str) -> bool:
    """
    Türkiye vergi kimlik doğrulaması (checksum dahil):
    - 10 hane → VKN (tüzel kişi) — GİB algoritması
    - 11 hane → TCKN (şahıs şirketi) — TC kimlik algoritması
    """
    t = tax_no.strip()
    if re.fullmatch(r"\d{10}", t):
        return _validate_vkn(t)
    if re.fullmatch(r"\d{11}", t):
        return _validate_tckn(t)
    return False

def _validate_vkn(vkn: str) -> bool:
    """GİB vergi kimlik numarası kontrol basamağı algoritması."""
    digits = [int(c) for c in vkn]
    total = 0
    for i in range(9):
        tmp = (digits[i] + (9 - i)) % 10
        total += (tmp * (2 ** (9 - i))) % 9 if tmp != 9 else 9
    check = (10 - (total % 10)) % 10
    return check == digits[9]

def _validate_tckn(tckn: str) -> bool:
    """TC kimlik numarası algoritması (şahıs şirketleri TCKN kullanır)."""
    d = [int(c) for c in tckn]
    if d[0] == 0:
        return False
    if ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
user_state, user_data = {}, {}

def tg_token(): return os.getenv("TELEGRAM_TOKEN","")
def tg_chat():  return os.getenv("TELEGRAM_CHAT_ID","")
def tg_webhook_secret(): return os.getenv("TELEGRAM_WEBHOOK_SECRET","")

def send_text(chat_id, text):
    token = tg_token()
    if not token: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception as e:
        log.error("Telegram sendMessage hatası: %s", e)

def send_channel_message(text):
    cid = tg_chat()
    if cid: send_text(cid, text)

def ask_type(chat_id):
    token = tg_token()
    if not token: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "İlan tipi seç:",
                  "reply_markup": {"inline_keyboard": [[
                      {"text":"📥 Alım","callback_data":"type_alim"},
                      {"text":"📤 Satım","callback_data":"type_satim"}
                  ]]}}, timeout=5)
    except Exception as e:
        log.error("Telegram ask_type hatası: %s", e)

# ══════════════════════════════════════════════
# WHATSAPP
# ══════════════════════════════════════════════
def send_whatsapp(text):
    token, phone_id, to = os.getenv("WA_TOKEN"), os.getenv("WA_PHONE_ID"), os.getenv("WA_TO")
    if not all([token, phone_id, to]): return
    try:
        requests.post(f"https://graph.facebook.com/v19.0/{phone_id}/messages",
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
            json={"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}},
            timeout=8)
    except Exception as e:
        log.error("WA ERROR: %s", e)

def notify_new_listing(listing, company_name):
    tip  = "📥 ALIM" if listing.type == "buy" else "📤 SATIM"
    text = (f"🔥 YENİ İLAN — KOBİNET\n\n{tip}\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 {listing.title}\n🏢 {company_name}\n"
            f"🏭 {listing.sector}\n📍 {listing.city or '—'}\n"
            f"💰 {listing.budget or 'Teklif Alınacak'}\n"
            f"━━━━━━━━━━━━━━\n"
            f"⏳ Bitiş: {listing.expires_at.strftime('%d.%m.%Y')}")
    send_channel_message(text)
    send_whatsapp(text)

def notify_new_offer(listing_title, company_name, price):
    """Teklif bildirimi — sadece e-posta, Telegram/WA'ya GÖNDERİLMEZ."""
    pass  # Telegram'a teklif bildirimi istenmiyor

# ══════════════════════════════════════════════
# E-POSTA
# ══════════════════════════════════════════════
def _send_mail_async(app_obj, msg):
    """E-postayı arka plan thread'inde gönder — request'i bloklamaz."""
    def _worker():
        with app_obj.app_context():
            try:
                mail.send(msg)
            except Exception as e:
                log.error("Mail gönderim hatası: %s", e)
    threading.Thread(target=_worker, daemon=True).start()

# ══════════════════════════════════════════════
# İMZALI TOKEN'LAR — şifre sıfırlama & e-posta doğrulama
# ══════════════════════════════════════════════
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

RESET_TOKEN_MAX_AGE  = 3600        # 1 saat
VERIFY_TOKEN_MAX_AGE = 60*60*72    # 3 gün

def _serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])

def generate_token(email: str, salt: str) -> str:
    """E-postayı SECRET_KEY ile imzala. Token DB'de saklanmaz —
    imza + süre kontrolü yeterlidir (itsdangerous)."""
    return _serializer().dumps(email, salt=salt)

def verify_token(token: str, salt: str, max_age: int):
    """Geçerliyse e-postayı, değilse None döndürür."""
    try:
        return _serializer().loads(token, salt=salt, max_age=max_age)
    except SignatureExpired:
        return None
    except BadSignature:
        return None

def send_verification_email(user):
    if not app.config.get("MAIL_CONFIGURED"):
        log.warning("MAIL_USERNAME yok — doğrulama e-postası gönderilemedi: %s", user.email)
        return
    token = generate_token(user.email, "email-verify")
    link  = url_for("verify_email", token=token, _external=True)
    msg = MailMessage(
        subject="KOBİNET — E-posta adresinizi doğrulayın",
        recipients=[user.email],
        html=f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
          <h2 style="font-size:20px;font-weight:600;margin-bottom:8px">Hoş geldiniz, {user.company_name}</h2>
          <p style="font-size:14px;color:#52525b;margin-bottom:20px">
            KOBİNET'te ilan vermeye ve teklif almaya başlamak için e-posta adresinizi doğrulayın.
            Bağlantı 3 gün geçerlidir.
          </p>
          <a href="{link}" style="display:inline-block;background:#18181b;color:white;padding:11px 22px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500">
            E-postamı Doğrula
          </a>
          <p style="font-size:12px;color:#a1a1aa;margin-top:20px">
            Bu kaydı siz yapmadıysanız bu e-postayı yok sayabilirsiniz.<br>KOBİNET · Türkiye B2B Tedarik Ağı
          </p>
        </div>""")
    _send_mail_async(app, msg)

def send_password_reset_email(user):
    if not app.config.get("MAIL_CONFIGURED"):
        log.warning("MAIL_USERNAME yok — sıfırlama e-postası gönderilemedi: %s", user.email)
        return
    token = generate_token(user.email, "password-reset")
    link  = url_for("reset_password", token=token, _external=True)
    msg = MailMessage(
        subject="KOBİNET — Şifre sıfırlama talebi",
        recipients=[user.email],
        html=f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
          <h2 style="font-size:20px;font-weight:600;margin-bottom:8px">Şifre Sıfırlama</h2>
          <p style="font-size:14px;color:#52525b;margin-bottom:20px">
            Hesabınız için şifre sıfırlama talebi aldık. Aşağıdaki bağlantı <strong>1 saat</strong> geçerlidir.
          </p>
          <a href="{link}" style="display:inline-block;background:#18181b;color:white;padding:11px 22px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500">
            Yeni Şifre Belirle
          </a>
          <p style="font-size:12px;color:#a1a1aa;margin-top:20px">
            Bu talebi siz yapmadıysanız bu e-postayı yok sayın — şifreniz değişmez.<br>KOBİNET · Türkiye B2B Tedarik Ağı
          </p>
        </div>""")
    _send_mail_async(app, msg)

def send_offer_email(owner_email, owner_name, listing_title, bidder_name, price):
    """Teklif gelince ilan sahibine e-posta gönder."""
    if not app.config.get("MAIL_CONFIGURED"):
        return
    try:
        msg = MailMessage(
            subject=f"Yeni Teklif: {listing_title}",
            recipients=[owner_email],
            html=f"""
            <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
              <h2 style="font-size:20px;font-weight:600;margin-bottom:16px">Yeni Teklif Aldınız</h2>
              <div style="background:#f4f4f5;border-radius:8px;padding:16px;margin-bottom:16px">
                <div style="font-size:13px;color:#71717a;margin-bottom:4px">İlan</div>
                <div style="font-size:15px;font-weight:600">{listing_title}</div>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px">
                <div style="background:#f4f4f5;border-radius:8px;padding:14px">
                  <div style="font-size:12px;color:#71717a;margin-bottom:3px">Teklif Eden</div>
                  <div style="font-weight:600">{bidder_name}</div>
                </div>
                <div style="background:#f4f4f5;border-radius:8px;padding:14px">
                  <div style="font-size:12px;color:#71717a;margin-bottom:3px">Teklif Fiyatı</div>
                  <div style="font-weight:600;color:#16a34a">{price}</div>
                </div>
              </div>
              <a href="#" style="display:inline-block;background:#18181b;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500">
                Teklifleri Gör →
              </a>
              <p style="font-size:12px;color:#a1a1aa;margin-top:20px">KOBİNET · Türkiye B2B Tedarik Ağı</p>
            </div>"""
        )
        _send_mail_async(app, msg)
    except Exception as e:
        log.error("MAIL ERROR: %s", e)


def send_saved_search_email(recipient_email, company_name, listing):
    """Kayıtlı aramaya uygun ilan çıkınca bildir."""
    if not app.config.get("MAIL_CONFIGURED"):
        return
    try:
        tip = "Alım" if listing.type == "buy" else "Satım"
        msg = MailMessage(
            subject=f"Sektörünüzde Yeni İlan: {listing.title}",
            recipients=[recipient_email],
            html=f"""
            <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
              <h2 style="font-size:20px;font-weight:600;margin-bottom:8px">Yeni İlan Bildirimi</h2>
              <p style="font-size:14px;color:#52525b;margin-bottom:20px">
                Kayıtlı aramanıza uygun yeni bir ilan yayınlandı.
              </p>
              <div style="background:#f4f4f5;border-radius:8px;padding:16px;margin-bottom:16px">
                <div style="font-size:12px;color:#71717a;margin-bottom:6px">{tip} · {listing.sector}</div>
                <div style="font-size:16px;font-weight:600;margin-bottom:8px">{listing.title}</div>
                {f'<div style="font-size:14px;color:#3f3f46">{listing.budget}</div>' if listing.budget else ''}
              </div>
              <p style="font-size:12px;color:#a1a1aa;margin-top:20px">KOBİNET · Türkiye B2B Tedarik Ağı</p>
            </div>"""
        )
        _send_mail_async(app, msg)
    except Exception as e:
        log.error("SAVED SEARCH MAIL ERROR: %s", e)

# ══════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════
class User(UserMixin, db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(150), nullable=False)
    email        = db.Column(db.String(150), unique=True, nullable=False)
    password     = db.Column(db.String(300), nullable=False)
    phone        = db.Column(db.String(50))
    sector       = db.Column(db.String(100))
    city         = db.Column(db.String(100))
    description  = db.Column(db.Text)
    # YENİ ALANLAR
    tax_no       = db.Column(db.String(20))          # Vergi numarası
    is_verified  = db.Column(db.Boolean, default=False)  # Admin onaylı (firma/vergi no doğrulaması)
    email_verified = db.Column(db.Boolean, default=False)  # E-posta sahipliği doğrulandı mı
    logo         = db.Column(db.String(200))         # Logo dosya adı
    is_admin     = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_messages = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_offers   = db.Column(db.DateTime, default=datetime.utcnow)  # teklif sayacı takibi

    listings      = db.relationship("Listing",     backref="owner",  lazy=True, foreign_keys="Listing.user_id")
    offers        = db.relationship("Offer",       backref="bidder", lazy=True, foreign_keys="Offer.user_id")
    saved_searches= db.relationship("SavedSearch", backref="user",   lazy=True)


class Listing(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    type        = db.Column(db.String(20))
    sector      = db.Column(db.String(100))
    city        = db.Column(db.String(100))
    budget      = db.Column(db.String(50))
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at  = db.Column(db.DateTime, default=lambda: datetime.utcnow()+timedelta(days=LISTING_DAYS))
    renewed_at  = db.Column(db.DateTime, nullable=True)

    offers = db.relationship("Offer",        backref="listing",  lazy=True)
    files  = db.relationship("ListingFile",  backref="listing",  lazy=True, cascade="all, delete-orphan")

    @property
    def is_expired(self): return datetime.utcnow() > self.expires_at
    @property
    def days_left(self):  return max(0, (self.expires_at - datetime.utcnow()).days)
    @property
    def is_visible(self): return self.is_active and not self.is_expired


class ListingFile(db.Model):
    """İlana eklenmiş dosya/fotoğraf."""
    id         = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("listing.id"), nullable=False)
    filename   = db.Column(db.String(200), nullable=False)
    filetype   = db.Column(db.String(10))   # 'image' | 'pdf'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Offer(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    price      = db.Column(db.String(50))
    message    = db.Column(db.Text)
    listing_id = db.Column(db.Integer, db.ForeignKey("listing.id"), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"),    nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Message(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content     = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class SavedSearch(db.Model):
    """Kullanıcının kayıtlı araması — uygun ilan çıkınca e-posta gönderilir."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sector     = db.Column(db.String(100))
    city       = db.Column(db.String(100))
    type_      = db.Column(db.String(20))   # 'buy' | 'sell' | ''
    keyword    = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ══════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════
class SecureView(ModelView):
    def is_accessible(self):
        return current_user.is_authenticated and current_user.is_admin
    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for("login"))

class UserAdmin(SecureView):
    column_list = ["id","company_name","email","sector","city","tax_no","is_verified","is_admin","created_at"]
    column_searchable_list = ["company_name","email","sector","city","tax_no"]
    column_filters = ["sector","city","is_verified","is_admin"]
    form_excluded_columns = ["password","listings","offers","saved_searches"]
    can_export = True; page_size = 25

class ListingAdmin(SecureView):
    column_list = ["id","title","type","sector","city","is_active","expires_at","created_at"]
    column_searchable_list = ["title","sector","city"]
    column_filters = ["type","sector","city","is_active"]
    can_export = True; page_size = 25

class SavedSearchAdmin(SecureView):
    column_list = ["id","user_id","sector","city","type_","keyword","created_at"]
    page_size = 25

admin_panel = Admin(app, name="KOBİNET Admin", url="/admin-panel")
admin_panel.add_view(UserAdmin(User,           db.session, name="Kullanıcılar"))
admin_panel.add_view(ListingAdmin(Listing,     db.session, name="İlanlar"))
admin_panel.add_view(SecureView(Offer,         db.session, name="Teklifler"))
admin_panel.add_view(SecureView(Message,       db.session, name="Mesajlar"))
admin_panel.add_view(SavedSearchAdmin(SavedSearch, db.session, name="Kayıtlı Aramalar"))

# ══════════════════════════════════════════════
# CONTEXT PROCESSOR — navbar bildirim sayaçları
# ══════════════════════════════════════════════
@app.context_processor
def inject_badges():
    if not current_user.is_authenticated:
        return {"unread_messages": 0, "new_offers": 0, "dev_verify_link": None}

    # Son görülmeden bu yana gelen mesajlar
    last_seen_msg = current_user.last_seen_messages or datetime.utcnow()
    unread = Message.query.filter(
        Message.receiver_id == current_user.id,
        Message.created_at  >  last_seen_msg
    ).count()

    # Son görülmeden bu yana ilanlarıma gelen teklifler
    last_seen_off = current_user.last_seen_offers or datetime.utcnow()
    user_listing_ids = [l.id for l in current_user.listings]
    new_offers = 0
    if user_listing_ids:
        new_offers = Offer.query.filter(
            Offer.listing_id.in_(user_listing_ids),
            Offer.created_at > last_seen_off
        ).count()

    # SMTP yokken: doğrulama linkini doğrudan bantta göstermek için üret
    dev_verify_link = None
    if (not current_user.email_verified and not current_user.is_admin
            and app.config.get("REQUIRE_EMAIL_VERIFICATION")
            and not app.config.get("MAIL_CONFIGURED")):
        token = generate_token(current_user.email, "email-verify")
        dev_verify_link = url_for("verify_email", token=token)

    return {"unread_messages": unread, "new_offers": new_offers,
            "dev_verify_link": dev_verify_link}

# ══════════════════════════════════════════════
# LOGIN LOADER / DECORATORS
# ══════════════════════════════════════════════
@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*a, **kw)
    return dec

# ══════════════════════════════════════════════
# DOSYA SERVIS — upload edilmiş dosyaları sun
# ══════════════════════════════════════════════
@app.route("/uploads/listings/<filename>")
def uploaded_listing_file(filename):
    return send_from_directory(UPLOAD_LISTINGS, filename)

@app.route("/uploads/logos/<filename>")
def uploaded_logo(filename):
    return send_from_directory(UPLOAD_LOGOS, filename)

# ══════════════════════════════════════════════
# TEKLİFLERİM — kullanıcının verdiği teklif geçmişi
# ══════════════════════════════════════════════
@app.route("/my-offers")
@login_required
def my_offers():
    view = request.args.get("view", "received")          # received | made
    listing_filter = request.args.get("listing", type=int)
    my_listing_ids = [l.id for l in current_user.listings]

    total_received = (Offer.query.filter(Offer.listing_id.in_(my_listing_ids)).count()
                      if my_listing_ids else 0)
    total_made = Offer.query.filter_by(user_id=current_user.id).count()

    filter_listings = []   # [(listing, offer_count)] — yalnızca teklif almış ilanlar

    if view == "made":
        offers = Offer.query.filter_by(user_id=current_user.id)\
                            .order_by(Offer.id.desc()).all()
    else:
        view = "received"
        if my_listing_ids:
            q = Offer.query.filter(Offer.listing_id.in_(my_listing_ids))
            if listing_filter and listing_filter in my_listing_ids:
                q = q.filter(Offer.listing_id == listing_filter)
            offers = q.order_by(Offer.id.desc()).all()
            # ürün bazlı filtre çubuğu için ilan başına teklif sayıları
            rows = db.session.query(Offer.listing_id, db.func.count(Offer.id))\
                             .filter(Offer.listing_id.in_(my_listing_ids))\
                             .group_by(Offer.listing_id).all()
            counts = {lid: c for lid, c in rows}
            by_id = {l.id: l for l in current_user.listings}
            filter_listings = sorted(
                [(by_id[lid], c) for lid, c in counts.items() if lid in by_id],
                key=lambda t: t[1], reverse=True)
        else:
            offers = []
        # "ilanlarıma gelen yeni teklif" rozetini temizle (kullanıcı gördü)
        current_user.last_seen_offers = datetime.utcnow()
        db.session.commit()

    return render_template("my_offers.html", offers=offers, view=view,
                           listing_filter=listing_filter, filter_listings=filter_listings,
                           total_received=total_received, total_made=total_made)

# ══════════════════════════════════════════════
# HOME
# ══════════════════════════════════════════════
@app.route("/")
def index():
    q=request.args.get("q","").strip(); sector=request.args.get("sector","").strip()
    city=request.args.get("city","").strip(); type_=request.args.get("type","").strip()
    page=request.args.get("page",1,type=int)
    listings=Listing.query.filter(Listing.is_active==True,Listing.expires_at>datetime.utcnow())
    if q:      listings=listings.filter(Listing.title.ilike(f"%{q}%"))
    if sector: listings=listings.filter_by(sector=sector)
    if city:   listings=listings.filter_by(city=city)
    if type_:  listings=listings.filter_by(type=type_)
    pagination=listings.order_by(Listing.id.desc()).paginate(page=page,per_page=12)
    sectors=db.session.query(Listing.sector).distinct().filter(Listing.sector!=None).all()
    cities =db.session.query(Listing.city  ).distinct().filter(Listing.city  !=None).all()
    return render_template("index.html",pagination=pagination,listings=pagination.items,
        sectors=[s[0] for s in sectors],cities=[c[0] for c in cities],
        q=q,sector=sector,city=city,type_=type_)

# ══════════════════════════════════════════════
# REGISTER
# ══════════════════════════════════════════════
@app.route("/register",methods=["GET","POST"])
@limiter.limit("10 per hour", methods=["POST"],
               error_message="Çok fazla kayıt denemesi. Lütfen daha sonra tekrar deneyin.")
def register():
    if current_user.is_authenticated: return redirect("/")
    if request.method=="POST":
        email=request.form.get("email","").strip().lower()
        company=request.form.get("company_name","").strip()
        password=request.form.get("password","")
        tax_no=request.form.get("tax_no","").strip()

        if not email or not company or not password:
            flash("Zorunlu alanları doldurun.","danger"); return render_template("register.html")
        if User.query.filter_by(email=email).first():
            flash("Bu e-posta kayıtlı.","danger"); return render_template("register.html")
        if len(password)<8:
            flash("Şifre en az 8 karakter olmalıdır.","danger"); return render_template("register.html")
        if tax_no and not validate_tax_no(tax_no):
            flash("Geçersiz vergi numarası. VKN 10, TCKN 11 hanelidir.","danger"); return render_template("register.html")

        # Doğrulama zorunluysa kullanıcı doğrulanmamış başlar; değilse baştan doğrulanmış.
        require_verify = app.config.get("REQUIRE_EMAIL_VERIFICATION")
        mail_on = app.config.get("MAIL_CONFIGURED")
        user=User(company_name=company,email=email,password=generate_password_hash(password),
            phone=request.form.get("phone",""),sector=request.form.get("sector",""),
            city=request.form.get("city",""),description=request.form.get("description",""),
            tax_no=tax_no or None, email_verified=(not require_verify))
        db.session.add(user); db.session.commit()
        if not require_verify:
            flash("Kayıt başarılı! Giriş yapabilirsiniz.","success")
        elif mail_on:
            send_verification_email(user)
            flash("Kayıt başarılı! E-posta adresinize bir doğrulama bağlantısı gönderdik — "
                  "ilan verebilmek için doğrulayın (spam klasörünü de kontrol edin).","success")
        else:
            # SMTP yok: kullanıcı giriş yapınca üstteki banttan 'Hesabı Doğrula' bağlantısıyla doğrular
            flash("Kayıt başarılı! Giriş yapın, ardından sayfanın üstündeki bantta yer alan "
                  "“Hesabı Doğrula” bağlantısına tıklayarak hesabınızı doğrulayın.","info")
        return redirect("/login")
    return render_template("register.html")

# ══════════════════════════════════════════════
# LOGIN / LOGOUT
# ══════════════════════════════════════════════
@app.route("/login",methods=["GET","POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"],
               error_message="Çok fazla deneme yaptınız. Lütfen bir dakika bekleyin.")
def login():
    if current_user.is_authenticated: return redirect("/")
    if request.method=="POST":
        user=User.query.filter_by(email=request.form.get("email","").strip().lower()).first()
        if user and check_password_hash(user.password,request.form.get("password","")):
            login_user(user)
            flash(f"Hoş geldiniz, {user.company_name}!","success")
            if app.config.get("REQUIRE_EMAIL_VERIFICATION") and not user.email_verified and not user.is_admin:
                flash("E-posta adresiniz henüz doğrulanmadı. İlan vermek için doğrulama gerekir.","info")
            return redirect(request.args.get("next") or "/")
        flash("E-posta veya şifre hatalı.","danger")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user(); flash("Çıkış yapıldı.","info"); return redirect("/")


# ══════════════════════════════════════════════
# E-POSTA DOĞRULAMA
# ══════════════════════════════════════════════
@app.route("/verify-email/<token>")
def verify_email(token):
    email = verify_token(token, "email-verify", VERIFY_TOKEN_MAX_AGE)
    if not email:
        flash("Doğrulama bağlantısı geçersiz veya süresi dolmuş. Yeniden gönderebilirsiniz.","danger")
        return redirect(url_for("login"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Kullanıcı bulunamadı.","danger"); return redirect(url_for("register"))
    if user.email_verified:
        flash("E-postanız zaten doğrulanmış.","info")
    else:
        user.email_verified = True
        db.session.commit()
        log.info("E-posta doğrulandı: %s", email)
        flash("E-postanız doğrulandı! Artık ilan verebilir ve teklif yapabilirsiniz.","success")
    return redirect(url_for("login") if not current_user.is_authenticated else url_for("index"))

@app.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("3 per hour",
               error_message="Çok sık denediniz. Lütfen daha sonra tekrar deneyin.")
def resend_verification():
    if current_user.email_verified:
        flash("E-postanız zaten doğrulanmış.","info")
    elif not app.config.get("MAIL_CONFIGURED"):
        flash("Mail sunucusu yapılandırılmadığı için e-posta gönderilemez. "
              "Üstteki banttaki “Hesabı Doğrula” bağlantısını kullanın.","info")
    else:
        send_verification_email(current_user)
        flash("Doğrulama e-postası yeniden gönderildi. Gelen kutunuzu (ve spam klasörünü) kontrol edin.","success")
    return redirect(request.referrer or url_for("index"))

# ══════════════════════════════════════════════
# ŞİFRE SIFIRLAMA
# ══════════════════════════════════════════════
@app.route("/forgot-password", methods=["GET","POST"])
@limiter.limit("5 per hour", methods=["POST"],
               error_message="Çok fazla sıfırlama talebi. Lütfen daha sonra tekrar deneyin.")
def forgot_password():
    if current_user.is_authenticated: return redirect("/")
    if request.method=="POST":
        email = request.form.get("email","").strip().lower()
        user  = User.query.filter_by(email=email).first()
        if user:
            send_password_reset_email(user)
        # Hesabın var olup olmadığını sızdırma — her durumda aynı mesaj
        flash("Eğer bu adres kayıtlıysa, şifre sıfırlama bağlantısı gönderildi.","info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET","POST"])
def reset_password(token):
    if current_user.is_authenticated: return redirect("/")
    email = verify_token(token, "password-reset", RESET_TOKEN_MAX_AGE)
    if not email:
        flash("Sıfırlama bağlantısı geçersiz veya süresi dolmuş. Yeni talep oluşturun.","danger")
        return redirect(url_for("forgot_password"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Kullanıcı bulunamadı.","danger"); return redirect(url_for("forgot_password"))
    if request.method=="POST":
        pw  = request.form.get("password","")
        pw2 = request.form.get("password2","")
        if len(pw)<8:
            flash("Şifre en az 8 karakter olmalıdır.","danger")
            return render_template("reset_password.html", token=token)
        if pw != pw2:
            flash("Şifreler eşleşmiyor.","danger")
            return render_template("reset_password.html", token=token)
        user.password = generate_password_hash(pw)
        db.session.commit()
        log.info("Şifre sıfırlandı: %s", email)
        flash("Şifreniz güncellendi. Yeni şifrenizle giriş yapabilirsiniz.","success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)

# ══════════════════════════════════════════════
# SEO — sitemap.xml & robots.txt
# ══════════════════════════════════════════════
@app.route("/sitemap.xml")
def sitemap():
    """Google'a aktif sayfaların listesini ver. Sadece herkese açık,
    indekslenmeye değer URL'ler: anasayfa, aktif ilanlar, firma profilleri."""
    pages = []
    base = request.url_root.rstrip("/")
    now  = datetime.utcnow().strftime("%Y-%m-%d")

    pages.append({"loc": f"{base}/", "lastmod": now, "changefreq": "hourly", "priority": "1.0"})

    active = Listing.query.filter(
        Listing.is_active==True, Listing.expires_at>datetime.utcnow()
    ).order_by(Listing.id.desc()).limit(5000).all()
    for l in active:
        lastmod = (l.renewed_at or l.created_at).strftime("%Y-%m-%d")
        pages.append({"loc": f"{base}/listing/{l.id}", "lastmod": lastmod,
                      "changefreq": "daily", "priority": "0.8"})

    # Sistem kullanıcısı hariç, en az bir ilanı olan firma profilleri
    owner_ids = {l.user_id for l in active}
    for uid in owner_ids:
        u = User.query.get(uid)
        if u and u.email != SYSTEM_USER_EMAIL:
            pages.append({"loc": f"{base}/profile/{u.id}", "lastmod": now,
                          "changefreq": "weekly", "priority": "0.5"})

    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in pages:
        xml.append(f"<url><loc>{p['loc']}</loc><lastmod>{p['lastmod']}</lastmod>"
                   f"<changefreq>{p['changefreq']}</changefreq><priority>{p['priority']}</priority></url>")
    xml.append("</urlset>")
    return app.response_class("\n".join(xml), mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
    body = f"""User-agent: *
Disallow: /admin-panel/
Disallow: /admin/
Disallow: /login
Disallow: /register
Disallow: /logout
Disallow: /messages
Disallow: /message/
Disallow: /my-offers
Disallow: /saved-searches
Disallow: /profile/edit
Disallow: /forgot-password
Disallow: /reset-password/
Disallow: /verify-email/
Disallow: /uploads/

Sitemap: {base}/sitemap.xml
"""
    return app.response_class(body, mimetype="text/plain")

# ══════════════════════════════════════════════
# CREATE LISTING
# ══════════════════════════════════════════════
@app.route("/create",methods=["GET","POST"])
@login_required
def create():
    if app.config.get("REQUIRE_EMAIL_VERIFICATION") and not current_user.email_verified and not current_user.is_admin:
        flash("İlan verebilmek için önce e-posta adresinizi doğrulayın.","danger")
        return redirect(url_for("index"))
    if request.method=="POST":
        title=request.form.get("title","").strip()
        type_=request.form.get("type","").strip()
        sector=request.form.get("sector","").strip()
        if not title or not type_ or not sector:
            flash("Başlık, tip ve sektör zorunludur.","danger")
            return render_template("create.html")

        listing=Listing(title=title,description=request.form.get("description","").strip(),
            type=type_,sector=sector,city=request.form.get("city",current_user.city),
            budget=request.form.get("budget",""),user_id=current_user.id,
            expires_at=datetime.utcnow()+timedelta(days=LISTING_DAYS))
        db.session.add(listing); db.session.flush()   # ID al

        # Dosya yükle
        files = request.files.getlist("files")
        present = [f for f in files if f and f.filename]
        rejected = 0
        for f in present:
            ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
            fname = save_file(f, UPLOAD_LISTINGS, ALLOWED_FILES)
            if fname:
                ftype = "image" if ext in ALLOWED_IMG else "pdf"
                db.session.add(ListingFile(listing_id=listing.id,filename=fname,filetype=ftype))
            else:
                rejected += 1

        db.session.commit()

        # Kayıtlı aramalara bildir
        _notify_saved_searches(listing)
        notify_new_listing(listing, current_user.company_name)

        flash(f"İlanınız {LISTING_DAYS} gün süreyle yayınlandı.","success")
        if rejected:
            flash(f"{rejected} dosya eklenemedi: geçersiz format veya bozuk dosya. "
                  f"İzin verilen: PNG, JPG, GIF, WEBP, PDF. iPhone HEIC fotoğrafları "
                  f"desteklenmez — JPG/PNG olarak kaydedip ekleyin.","warning")
        return redirect(url_for("listing", id=listing.id))
    return render_template("create.html")

def _notify_saved_searches(listing):
    """Yeni ilana uyan kayıtlı aramaları bul ve e-posta gönder."""
    searches = SavedSearch.query.all()
    notified = set()
    for s in searches:
        if s.user_id == listing.user_id: continue
        if s.sector and s.sector != listing.sector: continue
        if s.city   and s.city   != listing.city:   continue
        if s.type_  and s.type_  != listing.type:   continue
        if s.keyword and s.keyword.lower() not in listing.title.lower(): continue
        if s.user_id in notified: continue
        user = User.query.get(s.user_id)
        if user:
            send_saved_search_email(user.email, user.company_name, listing)
            notified.add(s.user_id)

# ══════════════════════════════════════════════
# LISTING DETAIL + FILE UPLOAD
# ══════════════════════════════════════════════
@app.route("/listing/<int:id>",methods=["GET","POST"])
def listing(id):
    lst=Listing.query.get_or_404(id)
    if request.method=="POST":
        if not current_user.is_authenticated:
            flash("Giriş yapın.","danger"); return redirect(url_for("login"))
        if app.config.get("REQUIRE_EMAIL_VERIFICATION") and not current_user.email_verified and not current_user.is_admin:
            flash("Teklif verebilmek için önce e-posta adresinizi doğrulayın.","danger")
            return redirect(url_for("listing",id=id))
        if lst.user_id==current_user.id:
            flash("Kendi ilanınıza teklif veremezsiniz.","danger"); return redirect(url_for("listing",id=id))
        if lst.is_expired or not lst.is_active:
            flash("Bu ilan artık aktif değil.","danger"); return redirect(url_for("listing",id=id))
        price=request.form.get("price","").strip()
        if not price:
            flash("Fiyat boş olamaz.","danger"); return redirect(url_for("listing",id=id))
        offer=Offer(price=price,message=request.form.get("message","").strip(),
                    listing_id=id,user_id=current_user.id)
        db.session.add(offer); db.session.commit()
        notify_new_offer(lst.title, current_user.company_name, price)
        # E-posta
        owner=User.query.get(lst.user_id)
        if owner:
            send_offer_email(owner.email,owner.company_name,lst.title,
                             current_user.company_name,price)
        flash("Teklifiniz gönderildi.","success")
        return redirect(url_for("listing",id=id))

    # Teklifler sadece ilan sahibi ve admin görebilir
    can_see_offers = (current_user.is_authenticated and
                      (current_user.id == lst.user_id or current_user.is_admin))

    # İlan sahibi kendi ilanını açınca teklif sayacını sıfırla
    if current_user.is_authenticated and current_user.id == lst.user_id:
        current_user.last_seen_offers = datetime.utcnow()
        db.session.commit()

    offers = Offer.query.filter_by(listing_id=id).order_by(Offer.id.desc()).all() if can_see_offers else []
    owner  = User.query.get(lst.user_id)
    similar= Listing.query.filter(
        Listing.sector==lst.sector, Listing.id!=lst.id,
        Listing.is_active==True, Listing.expires_at>datetime.utcnow()
    ).order_by(Listing.id.desc()).limit(4).all()
    needs_login = not current_user.is_authenticated
    return render_template("listing.html",listing=lst,offers=offers,owner=owner,
                           similar=similar,can_see_offers=can_see_offers,
                           needs_login=needs_login)


@app.route("/listing/<int:id>/upload",methods=["POST"])
@login_required
def listing_upload(id):
    lst=Listing.query.get_or_404(id)
    if lst.user_id!=current_user.id: abort(403)
    files=request.files.getlist("files")
    present=[f for f in files if f and f.filename]
    added=0; rejected=0
    for f in present:
        ext=f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
        fname=save_file(f,UPLOAD_LISTINGS,ALLOWED_FILES)
        if fname:
            ftype="image" if ext in ALLOWED_IMG else "pdf"
            db.session.add(ListingFile(listing_id=lst.id,filename=fname,filetype=ftype))
            added+=1
        else:
            rejected+=1
    db.session.commit()
    _flash_upload_result(added, rejected, len(present))
    return redirect(url_for("listing",id=id))


@app.route("/listing/<int:id>/file/<int:fid>/delete",methods=["POST"])
@login_required
def listing_file_delete(id,fid):
    lst=Listing.query.get_or_404(id)
    if lst.user_id!=current_user.id and not current_user.is_admin: abort(403)
    lf=ListingFile.query.get_or_404(fid)
    try: os.remove(os.path.join(UPLOAD_LISTINGS,lf.filename))
    except: pass
    db.session.delete(lf); db.session.commit()
    flash("Dosya silindi.","info")
    return redirect(url_for("listing",id=id))

# ══════════════════════════════════════════════
# RENEW / DELETE / TOGGLE
# ══════════════════════════════════════════════
@app.route("/listing/<int:id>/renew",methods=["POST"])
@login_required
def renew_listing(id):
    lst=Listing.query.get_or_404(id)
    if lst.user_id!=current_user.id: abort(403)
    lst.expires_at=datetime.utcnow()+timedelta(days=LISTING_DAYS)
    lst.renewed_at=datetime.utcnow(); lst.is_active=True
    db.session.commit()
    flash(f"İlan {LISTING_DAYS} gün uzatıldı.","success")
    return redirect(url_for("listing",id=id))

@app.route("/listing/<int:id>/delete",methods=["POST"])
@login_required
def delete_listing(id):
    lst=Listing.query.get_or_404(id)
    if lst.user_id!=current_user.id and not current_user.is_admin: abort(403)
    for lf in lst.files:
        try: os.remove(os.path.join(UPLOAD_LISTINGS,lf.filename))
        except: pass
    Offer.query.filter_by(listing_id=id).delete()
    db.session.delete(lst); db.session.commit()
    flash("İlan silindi.","info"); return redirect("/")

@app.route("/listing/<int:id>/toggle",methods=["POST"])
@login_required
@admin_required
def toggle_listing(id):
    lst=Listing.query.get_or_404(id)
    lst.is_active=not lst.is_active; db.session.commit()
    flash(f"İlan {'aktif' if lst.is_active else 'pasif'} edildi.","info")
    return redirect(url_for("listing",id=id))

# ══════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════
@app.route("/profile/<int:id>")
def profile(id):
    user=User.query.get_or_404(id)
    listings=Listing.query.filter_by(user_id=id).order_by(Listing.id.desc()).all()
    return render_template("profile.html",user=user,listings=listings)

@app.route("/profile/edit",methods=["GET","POST"])
@login_required
def edit_profile():
    if request.method=="POST":
        company=request.form.get("company_name","").strip()
        tax_no=request.form.get("tax_no","").strip()
        if not company:
            flash("Şirket adı boş olamaz.","danger"); return render_template("edit_profile.html")
        if tax_no and not validate_tax_no(tax_no):
            flash("Geçersiz vergi numarası. VKN 10, TCKN 11 hanelidir.","danger"); return render_template("edit_profile.html")

        current_user.company_name=company
        current_user.phone=request.form.get("phone","").strip()
        current_user.sector=request.form.get("sector","").strip()
        current_user.city=request.form.get("city","").strip()
        current_user.description=request.form.get("description","").strip()
        current_user.tax_no=tax_no or current_user.tax_no

        # Logo yükleme
        logo_file=request.files.get("logo")
        if logo_file and logo_file.filename:
            fname=save_file(logo_file,UPLOAD_LOGOS,ALLOWED_IMG)
            if fname:
                # Eski logoyu sil
                if current_user.logo:
                    try: os.remove(os.path.join(UPLOAD_LOGOS,current_user.logo))
                    except: pass
                current_user.logo=fname
            else:
                flash("Geçersiz logo formatı (png, jpg, gif, webp).","danger")
                return render_template("edit_profile.html")

        new_pw=request.form.get("new_password","")
        if new_pw:
            if len(new_pw)<8:
                flash("Şifre en az 8 karakter olmalıdır.","danger"); return render_template("edit_profile.html")
            current_user.password=generate_password_hash(new_pw)

        db.session.commit()
        flash("Profil güncellendi.","success")
        return redirect(url_for("profile",id=current_user.id))
    return render_template("edit_profile.html")

# ══════════════════════════════════════════════
# KAYITLI ARAMALAR
# ══════════════════════════════════════════════
@app.route("/saved-searches",methods=["GET","POST"])
@login_required
def saved_searches():
    # Sabit listeler — aktif ilan olup olmamasından bağımsız
    ALL_SECTORS = ['CNC / Freze','Elektronik','Yazılım / BT','Tekstil','Gıda',
                   'İnşaat','Lojistik','Kimya','Makine','Mobilya','Diğer']
    ALL_CITIES  = ['İstanbul','Ankara','İzmir','Bursa','Kocaeli','Gaziantep',
                   'Konya','Mersin','Adana','Antalya','Manisa','Kayseri','Malatya','Diğer']

    if request.method=="POST":
        sector  = request.form.get("sector","").strip()
        city    = request.form.get("city","").strip()
        type_   = request.form.get("type_","").strip()
        keyword = request.form.get("keyword","").strip()
        if not sector and not city and not keyword and not type_:
            flash("En az bir kriter girin.","danger")
            return redirect(url_for("saved_searches"))
        ss=SavedSearch(user_id=current_user.id,sector=sector,city=city,
                       type_=type_,keyword=keyword)
        db.session.add(ss); db.session.commit()
        flash("Kayıtlı arama oluşturuldu. Uygun ilan çıkınca e-posta alırsınız.","success")
        return redirect(url_for("saved_searches"))
    searches=SavedSearch.query.filter_by(user_id=current_user.id).order_by(SavedSearch.id.desc()).all()
    return render_template("saved_searches.html",searches=searches,
        sectors=ALL_SECTORS,cities=ALL_CITIES)

@app.route("/saved-searches/<int:id>/delete",methods=["POST"])
@login_required
def delete_saved_search(id):
    ss=SavedSearch.query.get_or_404(id)
    if ss.user_id!=current_user.id: abort(403)
    db.session.delete(ss); db.session.commit()
    flash("Kayıtlı arama silindi.","info")
    return redirect(url_for("saved_searches"))

# ══════════════════════════════════════════════
# MESSAGES
# ══════════════════════════════════════════════
@app.route("/messages")
@login_required
def messages():
    # Mesajlar sayfası açılınca "okundu" damgası güncelle
    current_user.last_seen_messages = datetime.utcnow()
    db.session.commit()
    sent=Message.query.filter_by(sender_id=current_user.id).all()
    received=Message.query.filter_by(receiver_id=current_user.id).all()
    ids=set()
    for m in sent: ids.add(m.receiver_id)
    for m in received: ids.add(m.sender_id)
    chats=[]
    for uid in ids:
        user=User.query.get(uid)
        if not user: continue
        last=Message.query.filter(
            ((Message.sender_id==current_user.id)&(Message.receiver_id==uid))|
            ((Message.sender_id==uid)&(Message.receiver_id==current_user.id))
        ).order_by(Message.created_at.desc()).first()
        chats.append({"user":user,"last_message":last})
    chats.sort(key=lambda x:x["last_message"].created_at,reverse=True)
    return render_template("messages.html",chats=chats)

@app.route("/message/<int:user_id>",methods=["GET","POST"])
@login_required
def message(user_id):
    other=User.query.get_or_404(user_id)
    if request.method=="POST":
        content=request.form.get("content","").strip()
        if content:
            db.session.add(Message(sender_id=current_user.id,receiver_id=user_id,content=content))
            db.session.commit()
        return redirect(url_for("message",user_id=user_id))
    # Mesaj sayfası açılınca okundu damgası güncelle
    current_user.last_seen_messages = datetime.utcnow()
    db.session.commit()
    msgs=Message.query.filter(
        ((Message.sender_id==current_user.id)&(Message.receiver_id==user_id))|
        ((Message.sender_id==user_id)&(Message.receiver_id==current_user.id))
    ).order_by(Message.created_at.asc()).all()
    return render_template("message.html",messages=msgs,other=other)

# ══════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════
@app.route("/admin-panel/dashboard")
@login_required
@admin_required
def admin_dashboard():
    from datetime import timedelta
    week_ago = datetime.utcnow() - timedelta(days=7)
    return render_template("admin_dashboard.html",
        total_users=User.query.count(),
        total_listings=Listing.query.count(),
        active_listings=Listing.query.filter(Listing.is_active==True,Listing.expires_at>datetime.utcnow()).count(),
        expired_listings=Listing.query.filter(Listing.expires_at<=datetime.utcnow()).count(),
        passive_listings=Listing.query.filter(Listing.is_active==False).count(),
        total_offers=Offer.query.count(),
        total_messages=Message.query.count(),
        pending_verification=User.query.filter(User.tax_no!=None,User.is_verified==False).count(),
        new_users_week=User.query.filter(User.created_at>=week_ago).count(),
        new_listings_week=Listing.query.filter(Listing.created_at>=week_ago).count(),
        new_offers_week=Offer.query.filter(Offer.created_at>=week_ago).count(),
        recent_users=User.query.order_by(User.created_at.desc()).limit(8).all(),
        recent_listings=Listing.query.order_by(Listing.created_at.desc()).limit(8).all())

@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    q=request.args.get("q","").strip()
    page=request.args.get("page",1,type=int)
    users=User.query
    if q: users=users.filter((User.company_name.ilike(f"%{q}%"))|(User.email.ilike(f"%{q}%")))
    pagination=users.order_by(User.id.desc()).paginate(page=page,per_page=20)
    return render_template("admin_users.html",pagination=pagination,users=pagination.items,q=q)

@app.route("/admin/listings")
@login_required
@admin_required
def admin_listings():
    q=request.args.get("q","").strip(); status=request.args.get("status","").strip()
    page=request.args.get("page",1,type=int)
    listings=Listing.query
    if q: listings=listings.filter(Listing.title.ilike(f"%{q}%"))
    if status=="active":  listings=listings.filter(Listing.is_active==True,Listing.expires_at>datetime.utcnow())
    elif status=="expired": listings=listings.filter(Listing.expires_at<=datetime.utcnow())
    elif status=="passive": listings=listings.filter(Listing.is_active==False)
    pagination=listings.order_by(Listing.id.desc()).paginate(page=page,per_page=20)
    return render_template("admin_listings.html",pagination=pagination,listings=pagination.items,q=q,status=status)

@app.route("/admin/user/<int:id>/verify",methods=["POST"])
@login_required
@admin_required
def admin_verify_user(id):
    user=User.query.get_or_404(id)
    user.is_verified=not user.is_verified; db.session.commit()
    flash(f"{'✓ Doğrulandı' if user.is_verified else 'Doğrulama kaldırıldı'}: {user.company_name}","success")
    return redirect(url_for("admin_users"))

@app.route("/admin/user/<int:id>/toggle-admin",methods=["POST"])
@login_required
@admin_required
def admin_toggle_admin(id):
    user=User.query.get_or_404(id)
    if user.id==current_user.id: flash("Kendi yetkinizi alamazsınız.","danger"); return redirect(url_for("admin_users"))
    user.is_admin=not user.is_admin; db.session.commit()
    flash(f"{'Admin yapıldı' if user.is_admin else 'Admin yetkisi alındı'}: {user.company_name}","info")
    return redirect(url_for("admin_users"))

@app.route("/admin/user/<int:id>/delete",methods=["POST"])
@login_required
@admin_required
def admin_delete_user(id):
    user=User.query.get_or_404(id)
    if user.is_admin: flash("Admin silinemez.","danger"); return redirect(url_for("admin_users"))
    for lst in user.listings:
        for lf in lst.files:
            try: os.remove(os.path.join(UPLOAD_LISTINGS,lf.filename))
            except: pass
        Offer.query.filter_by(listing_id=lst.id).delete()
        db.session.delete(lst)
    Offer.query.filter_by(user_id=id).delete()
    Message.query.filter((Message.sender_id==id)|(Message.receiver_id==id)).delete()
    SavedSearch.query.filter_by(user_id=id).delete()
    if user.logo:
        try: os.remove(os.path.join(UPLOAD_LOGOS,user.logo))
        except: pass
    db.session.delete(user); db.session.commit()
    flash(f"'{user.company_name}' silindi.","info"); return redirect(url_for("admin_users"))

@app.route("/admin/listing/<int:id>/delete",methods=["POST"])
@login_required
@admin_required
def admin_delete_listing(id):
    lst=Listing.query.get_or_404(id); title=lst.title
    for lf in lst.files:
        try: os.remove(os.path.join(UPLOAD_LISTINGS,lf.filename))
        except: pass
    Offer.query.filter_by(listing_id=id).delete()
    db.session.delete(lst); db.session.commit()
    flash(f"'{title}' silindi.","info"); return redirect(url_for("admin_listings"))

@app.route("/admin/listing/<int:id>/renew",methods=["POST"])
@login_required
@admin_required
def admin_renew_listing(id):
    lst=Listing.query.get_or_404(id)
    lst.expires_at=datetime.utcnow()+timedelta(days=LISTING_DAYS)
    lst.renewed_at=datetime.utcnow(); lst.is_active=True; db.session.commit()
    flash(f"'{lst.title}' uzatıldı.","success"); return redirect(url_for("admin_listings"))

# ══════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ══════════════════════════════════════════════
SYSTEM_USER_EMAIL = "telegram-bot@kobinet.internal"

def get_or_create_system_user():
    """Telegram'dan gelen ilanların bağlanacağı sistem kullanıcısı.
    user_id=0 gibi var olmayan bir FK yerine gerçek bir kayıt kullanılır."""
    su = User.query.filter_by(email=SYSTEM_USER_EMAIL).first()
    if not su:
        su = User(company_name="KOBİNET Telegram",
                  email=SYSTEM_USER_EMAIL,
                  password=generate_password_hash(uuid.uuid4().hex),  # giriş yapılamaz
                  is_verified=True, email_verified=True)
        db.session.add(su); db.session.commit()
    return su

@app.route("/telegram-webhook",methods=["POST"])
@csrf.exempt   # dış servis JSON POST'u — CSRF token taşıyamaz, secret ile doğrulanır
def telegram_webhook():
    # ── Güvenlik: Telegram'ın setWebhook'ta verdiğimiz secret_token'ı
    #    her istekte bu header ile göndermesini bekliyoruz.
    secret = tg_webhook_secret()
    if not secret:
        log.error("TELEGRAM_WEBHOOK_SECRET tanımlı değil — webhook reddedildi.")
        abort(403)
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received, secret):   # timing-safe karşılaştırma
        log.warning("Webhook: geçersiz secret token, IP=%s", request.remote_addr)
        abort(403)

    data=request.get_json(silent=True)
    if not data: return "ok"
    try:
        msg_text=chat_id=user_id=callback=None
        if "message" in data:
            msg_text=data["message"].get("text"); chat_id=data["message"]["chat"]["id"]; user_id=data["message"]["from"]["id"]
        elif "callback_query" in data:
            callback=data["callback_query"]["data"]; chat_id=data["callback_query"]["message"]["chat"]["id"]; user_id=data["callback_query"]["from"]["id"]
        if not chat_id or not user_id: return "ok"
        key=user_id
        if msg_text=="/start":
            user_state[key]="TYPE"; user_data[key]={}
            send_text(chat_id,"📦 İlan oluşturma başladı"); ask_type(chat_id); return "ok"
        if callback in ["type_alim","type_satim"]:
            user_state[key]="TITLE"; user_data[key]=user_data.get(key,{})
            user_data[key]["type"]="buy" if callback=="type_alim" else "sell"
            send_text(chat_id,"✍️ Başlığı yaz:"); return "ok"
        state=user_state.get(key)
        if state=="TITLE":
            if not msg_text: return "ok"
            user_data[key]["title"]=msg_text; user_state[key]="DESC"
            send_text(chat_id,"✍️ Açıklamayı yaz:"); return "ok"
        if state=="DESC":
            if not msg_text: return "ok"
            user_data[key]["description"]=msg_text; user_state[key]="SECTOR"
            send_text(chat_id,"🏭 Sektörü yaz:"); return "ok"
        if state=="SECTOR":
            if not msg_text: return "ok"
            user_data[key]["sector"]=msg_text; d=user_data[key]
            sysuser=get_or_create_system_user()
            lst=Listing(title=d["title"],description=d["description"],type=d["type"],
                        sector=d["sector"],user_id=sysuser.id,
                        expires_at=datetime.utcnow()+timedelta(days=LISTING_DAYS))
            db.session.add(lst); db.session.commit()
            send_text(chat_id,"✅ İlan oluşturuldu!")
            send_channel_message(f"📦 Telegram İlanı\n\n{d['title']}\n{d['sector']}")
            user_state.pop(key,None); user_data.pop(key,None)
    except Exception as e:
        log.error("WEBHOOK ERROR: %s", e)
    return "ok"

# ══════════════════════════════════════════════
# SEED — admin + sistem kullanıcısı (idempotent)
# ══════════════════════════════════════════════
def ensure_seed():
    """Admin ve Telegram sistem kullanıcısını oluştur. Varsa dokunma.
    Hem 'flask seed' CLI komutu hem lokal geliştirme tarafından çağrılır."""
    admin_email = os.getenv("ADMIN_EMAIL", "admin@kobinet.com")
    admin_pw    = os.getenv("ADMIN_PASSWORD", "admin123")
    if admin_pw == "admin123":
        log.warning("⚠️ ADMIN_PASSWORD varsayılan değerde! Production'da mutlaka değiştirin.")
    if not User.query.filter_by(email=admin_email).first():
        db.session.add(User(company_name="KOBİNET Admin", email=admin_email,
            password=generate_password_hash(admin_pw), is_admin=True, email_verified=True))
        db.session.commit()
        log.info("Admin hesabı oluşturuldu: %s", admin_email)
    get_or_create_system_user()

@app.cli.command("seed")
def seed_command():
    """Render start command'inde 'flask db upgrade && flask seed && gunicorn ...' olarak çalışır."""
    ensure_seed()
    print("✓ Seed tamamlandı.")

@app.cli.command("mailtest")
@click.argument("recipient")
def mailtest_command(recipient):
    """SMTP ayarlarını gerçek bir bağlantıyla test eder (sağlayıcıdan bağımsız).
    Kullanım:  flask mailtest hedef@adres.com
    Hangi SMTP sağlayıcısı olursa olsun (Gmail, Yandex, kurumsal, Brevo...) çalışır."""
    print("─" * 52)
    print("KOBİNET — SMTP Test")
    print("─" * 52)
    print(f"  MAIL_SERVER     : {app.config['MAIL_SERVER']}")
    print(f"  MAIL_PORT       : {app.config['MAIL_PORT']}")
    print(f"  MAIL_USE_TLS    : {app.config['MAIL_USE_TLS']}")
    print(f"  MAIL_USE_SSL    : {app.config['MAIL_USE_SSL']}")
    print(f"  MAIL_USERNAME   : {app.config['MAIL_USERNAME'] or '(boş)'}")
    print(f"  MAIL_PASSWORD   : {'(tanımlı, ' + str(len(app.config['MAIL_PASSWORD'])) + ' karakter)' if app.config['MAIL_PASSWORD'] else '(boş)'}")
    print(f"  DEFAULT_SENDER  : {app.config['MAIL_DEFAULT_SENDER']}")
    print("─" * 52)

    if not app.config.get("MAIL_CONFIGURED"):
        print("✗ MAIL_USERNAME ve/veya MAIL_PASSWORD boş.")
        print("  .env dosyana bu iki değeri ekle, sonra tekrar dene.")
        return

    # Gerçek SMTP bağlantısı + gönderim denemesi — hatayı OLDUĞU gibi göster
    try:
        msg = MailMessage(subject="KOBİNET SMTP test",
                          recipients=[recipient],
                          body="Bu bir test e-postasıdır. Bu mesajı aldıysanız mail ayarlarınız çalışıyor.")
        mail.send(msg)   # bilinçli olarak SENKRON — hatayı burada yakalamak istiyoruz
        print(f"✓ Test e-postası gönderildi → {recipient}")
        print("  Gelen kutusunu (ve spam klasörünü) kontrol et.")
    except Exception as e:
        print(f"✗ Gönderim BAŞARISIZ: {type(e).__name__}")
        print(f"  Detay: {e}")
        print()
        print("  Sık karşılaşılan nedenler:")
        print("  • Kimlik hatası (Authentication): şifre yanlış. Gmail/Yandex gibi")
        print("    sağlayıcılarda hesap şifresi değil, 'uygulama şifresi' gerekir.")
        print("  • Bağlantı reddi (Connection): MAIL_SERVER / MAIL_PORT yanlış,")
        print("    ya da TLS/SSL eşleşmiyor (587→TLS, 465→SSL).")
        print("  • Zaman aşımı (Timeout): ağ/firewall SMTP portunu engelliyor olabilir.")

# ══════════════════════════════════════════════
# RUN — sadece lokal geliştirme (python app.py)
# ══════════════════════════════════════════════
if __name__=="__main__":
    with app.app_context():
        db.create_all()      # lokal SQLite hızlı başlangıç; production'da migration kullanılır
        ensure_seed()
    app.run(host="0.0.0.0",port=10000,debug=False)
