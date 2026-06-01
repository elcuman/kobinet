import os, uuid, json, re
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
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

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
# psycopg2 yerine pg8000 kullan (Python 3.14 uyumlu)
if db_url.startswith("postgresql://") and "pg8000" not in db_url:
    db_url = db_url.replace("postgresql://","postgresql+pg8000://",1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url or "sqlite:///db.sqlite"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024

# Flask-Mail (.env'den okunur)
app.config["MAIL_SERVER"]   = os.getenv("MAIL_SERVER",  "smtp.gmail.com")
app.config["MAIL_PORT"]     = int(os.getenv("MAIL_PORT","587"))
app.config["MAIL_USE_TLS"]  = True
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME","")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD","")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_USERNAME","noreply@kobinet.com")

db   = SQLAlchemy(app)
mail = Mail(app)

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

def save_file(file, folder, allowed_set):
    """Dosyayı kaydet, unique isim döndür. Hata varsa None döner."""
    if not file or file.filename == "":
        return None
    if not allowed(file.filename, allowed_set):
        return None
    ext  = file.filename.rsplit(".",1)[1].lower()
    name = f"{uuid.uuid4().hex}.{ext}"
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, name))
    return name

def validate_tax_no(tax_no: str) -> bool:
    """Türkiye vergi numarası: tam 10 rakam."""
    return bool(re.fullmatch(r"\d{10}", tax_no.strip()))

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
user_state, user_data = {}, {}

def tg_token(): return os.getenv("TELEGRAM_TOKEN","")
def tg_chat():  return os.getenv("TELEGRAM_CHAT_ID","")

def send_text(chat_id, text):
    token = tg_token()
    if not token: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=5)
    except: pass

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
    except: pass

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
        print("WA ERROR:", e)

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
def send_offer_email(owner_email, owner_name, listing_title, bidder_name, price):
    """Teklif gelince ilan sahibine e-posta gönder."""
    if not app.config.get("MAIL_USERNAME"):
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
        mail.send(msg)
    except Exception as e:
        print("MAIL ERROR:", e)


def send_saved_search_email(recipient_email, company_name, listing):
    """Kayıtlı aramaya uygun ilan çıkınca bildir."""
    if not app.config.get("MAIL_USERNAME"):
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
        mail.send(msg)
    except Exception as e:
        print("SAVED SEARCH MAIL ERROR:", e)

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
    is_verified  = db.Column(db.Boolean, default=False)  # Admin onaylı
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
        return {"unread_messages": 0, "new_offers": 0}

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

    return {"unread_messages": unread, "new_offers": new_offers}

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
    offers = Offer.query.filter_by(user_id=current_user.id)\
                        .order_by(Offer.id.desc()).all()
    return render_template("my_offers.html", offers=offers)

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
        if len(password)<6:
            flash("Şifre en az 6 karakter.","danger"); return render_template("register.html")
        if tax_no and not validate_tax_no(tax_no):
            flash("Vergi numarası 10 haneli olmalıdır.","danger"); return render_template("register.html")

        user=User(company_name=company,email=email,password=generate_password_hash(password),
            phone=request.form.get("phone",""),sector=request.form.get("sector",""),
            city=request.form.get("city",""),description=request.form.get("description",""),
            tax_no=tax_no or None)
        db.session.add(user); db.session.commit()
        flash("Kayıt başarılı! Giriş yapabilirsiniz.","success")
        return redirect("/login")
    return render_template("register.html")

# ══════════════════════════════════════════════
# LOGIN / LOGOUT
# ══════════════════════════════════════════════
@app.route("/login",methods=["GET","POST"])
def login():
    if current_user.is_authenticated: return redirect("/")
    if request.method=="POST":
        user=User.query.filter_by(email=request.form.get("email","").strip().lower()).first()
        if user and check_password_hash(user.password,request.form.get("password","")):
            login_user(user)
            flash(f"Hoş geldiniz, {user.company_name}!","success")
            return redirect(request.args.get("next") or "/")
        flash("E-posta veya şifre hatalı.","danger")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user(); flash("Çıkış yapıldı.","info"); return redirect("/")

# ══════════════════════════════════════════════
# CREATE LISTING
# ══════════════════════════════════════════════
@app.route("/create",methods=["GET","POST"])
@login_required
def create():
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
        for f in files:
            if f and f.filename:
                ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
                fname = save_file(f, UPLOAD_LISTINGS, ALLOWED_FILES)
                if fname:
                    ftype = "image" if ext in ALLOWED_IMG else "pdf"
                    db.session.add(ListingFile(listing_id=listing.id,filename=fname,filetype=ftype))

        db.session.commit()

        # Kayıtlı aramalara bildir
        _notify_saved_searches(listing)
        notify_new_listing(listing, current_user.company_name)

        flash(f"İlanınız {LISTING_DAYS} gün süreyle yayınlandı.","success")
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
    added=0
    for f in files:
        if f and f.filename:
            ext=f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
            fname=save_file(f,UPLOAD_LISTINGS,ALLOWED_FILES)
            if fname:
                ftype="image" if ext in ALLOWED_IMG else "pdf"
                db.session.add(ListingFile(listing_id=lst.id,filename=fname,filetype=ftype))
                added+=1
    db.session.commit()
    flash(f"{added} dosya eklendi.","success")
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
            flash("Vergi numarası 10 haneli olmalıdır.","danger"); return render_template("edit_profile.html")

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
            if len(new_pw)<6:
                flash("Şifre en az 6 karakter.","danger"); return render_template("edit_profile.html")
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
    admin_email=os.getenv("ADMIN_EMAIL","admin@kobinet.com")
    admin_pw=os.getenv("ADMIN_PASSWORD","admin123")
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
        recent_listings=Listing.query.order_by(Listing.created_at.desc()).limit(8).all(),
        admin_email=admin_email,admin_pw=admin_pw)

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
@app.route("/telegram-webhook",methods=["POST"])
def telegram_webhook():
    data=request.json
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
            lst=Listing(title=d["title"],description=d["description"],type=d["type"],
                        sector=d["sector"],user_id=0,
                        expires_at=datetime.utcnow()+timedelta(days=LISTING_DAYS))
            db.session.add(lst); db.session.commit()
            send_text(chat_id,"✅ İlan oluşturuldu!")
            send_channel_message(f"📦 Telegram İlanı\n\n{d['title']}\n{d['sector']}")
            user_state.pop(key,None); user_data.pop(key,None)
    except Exception as e:
        print("WEBHOOK ERROR:",e)
    return "ok"

# ══════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════
if __name__=="__main__":
    with app.app_context():
        db.create_all()
        admin_email=os.getenv("ADMIN_EMAIL","admin@kobinet.com")
        admin_pw=os.getenv("ADMIN_PASSWORD","admin123")
        if not User.query.filter_by(email=admin_email).first():
            db.session.add(User(company_name="KOBİNET Admin",email=admin_email,
                password=generate_password_hash(admin_pw),is_admin=True))
            db.session.commit()
            print(f"✅ Admin: {admin_email} / {admin_pw}")
    app.run(host="0.0.0.0",port=10000,debug=False)
