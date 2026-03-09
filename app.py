# app.py
# نسخة محسنة وكاملة لتشغيل تطبيق Flask + Telethon على Railway أو بيئة مشابهة.
# المزايا:
# - pool للاتصالات بـ PostgreSQL
# - إدارة جلسات Telethon في حلقة asyncio منفصلة (background loop thread)
# - pending_logins مؤقتة مع انتهاء صلاحية
# - دعم AJAX: ردود JSON واضحة (401 عند عدم التفويض بدلاً من إعادة توجيه HTML)
# - تعامل محسن مع أخطاء Telethon (FloodWait, 2FA، أكواد خاطئة)
# - تسجيل لوج مفصل
# - إعدادات قابلة للتعديل عبر متغيرات بيئية
# - واجهات /account/add_step1 و /account/add_step2 متوافقة مع JSON و form-data
#
# ملاحظة: من الأفضل استخدام Gunicorn في Production. أضف Procfile:
# web: gunicorn -w 4 -b 0.0.0.0:$PORT app:app
#
# متطلبات (requirements.txt) اقتراحية:
# flask
# flask-login
# aiohttp
# telethon
# psycopg2-binary
# gunicorn (اختياري)
# python-dotenv (اختياري لتحميل متغيرات بيئية محلياً)
#
# احفظ هذا الملف كـ app.py ثم نفّذ redeploy على Railway.

import os
import re
import json
import time
import atexit
import logging
import asyncio
import threading
from datetime import datetime, timedelta

import aiohttp
import psycopg2
import psycopg2.pool

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, session, abort
)
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash

from telethon import TelegramClient, events, errors

# -------------------------
# إعدادات عامة (قابلة للتعديل عبر متغيرات بيئية)
# -------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "radar-super-secret-key-2026")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/radar")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # سيخزن كهاش عند init
OPENROUTER_API_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

# -------------------------
# لوقنج
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "radar.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("RadarApp")

# -------------------------
# Flask app و LoginManager
# -------------------------
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = SECRET_KEY

login_manager = LoginManager(app)
login_manager.login_view = "login"

# -------------------------
# متغيرات النظام
# -------------------------
# Pool لقاعدة البيانات
db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    logger.info("✅ PostgreSQL pool created successfully.")
except Exception as e:
    logger.exception("❌ Failed to create DB pool - will retry on demand: %s", e)
    db_pool = None

def get_db():
    """احصل على اتصال من pool أو افتح اتصال جديد إذا pool غير متوفّر."""
    if db_pool:
        return db_pool.getconn()
    return psycopg2.connect(DATABASE_URL)

def put_db(conn):
    """أعد الاتصال إلى pool أو أغلقه إذا pool غير متوفّر."""
    try:
        if db_pool and conn:
            db_pool.putconn(conn)
        else:
            if conn:
                conn.close()
    except Exception as e:
        logger.exception("put_db error: %s", e)

# -------------------------
# init_db: إنشاء الجداول والإعدادات الافتراضية
# -------------------------
def init_db():
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id SERIAL PRIMARY KEY,
                keyword TEXT UNIQUE NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT PRIMARY KEY,
                api_id INTEGER NOT NULL,
                api_hash TEXT NOT NULL,
                alert_group TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # إعدادات افتراضية
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("admin_email", ADMIN_EMAIL))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("admin_password", generate_password_hash(ADMIN_PASSWORD)))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("ai_enabled", "0"))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("openrouter_api_key", ""))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("radar_status", "1"))
        # كلمات مفتاحية افتراضية (يمكنك إضافة / تعديل لاحقاً من الواجهة)
        cur.execute("SELECT COUNT(*) FROM keywords")
        count = cur.fetchone()[0]
        if count == 0:
            default_keywords = ["مساعدة","بحوث","واجب","مشروع","شرح","مدرس","برق","assignment","research","project"]
            for kw in default_keywords:
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING",(kw,))
        conn.commit()
        cur.close()
        logger.info("✅ Database initialized successfully.")
    except Exception as e:
        logger.exception("init_db error: %s", e)
    finally:
        if conn:
            put_db(conn)

def log_event(content):
    """سجل حدثًا في جدول logs مع محاولة عدم كسر التطبيق عند فشل DB."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO logs (content) VALUES (%s)", (content,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error("log_event DB failure: %s", e)
    finally:
        try:
            if conn:
                put_db(conn)
        except Exception:
            pass
    logger.info(content)

def get_setting(key, default=None):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else default
    except Exception as e:
        logger.error("get_setting error: %s", e)
        return default
    finally:
        if conn:
            put_db(conn)

def set_setting(key, value):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.exception("set_setting error: %s", e)
    finally:
        if conn:
            put_db(conn)

# -------------------------
# pending_logins: إدارة جلسات خطوة التوثيق المؤقتة
# هيكل: { phone: {client, api_id, api_hash, phone_code_hash, created_at, expires_at} }
# -------------------------
pending_logins = {}
PENDING_TTL_SECONDS = int(os.environ.get("PENDING_TTL_SECONDS", 300))  # 5 دقائق افتراضياً

def cleanup_pending_logins():
    now = time.time()
    expired = [p for p, info in pending_logins.items() if info.get("expires_at", 0) <= now]
    for p in expired:
        try:
            client = pending_logins[p].get("client")
            if client:
                # محاولة قطع الاتصال بهدوء
                try:
                    asyncio.run_coroutine_threadsafe(client.disconnect(), radar_engine.loop)
                except Exception:
                    pass
        except Exception:
            pass
        del pending_logins[p]
        logger.info("pending_logins: expired and cleaned %s", p)

# جدولة تنظيف بسيط عبر thread
def pending_cleaner_worker():
    while True:
        try:
            cleanup_pending_logins()
        except Exception as e:
            logger.exception("pending_cleaner error: %s", e)
        time.sleep(30)

_cleaner_thread = threading.Thread(target=pending_cleaner_worker, daemon=True)
_cleaner_thread.start()

# -------------------------
# AI classification (OpenRouter) - مرن وآمن
# -------------------------
PROMPT_TEMPLATE = """
أنت مساعد ذكي مصنّف: قرر إن كانت الرسالة من طالب seeker أم معلن marketer.
أعد JSON فقط بصيغة: {"type":"seeker"/"marketer","confidence":int,"reason":"شرح قصير"}
نص الرسالة: {message}
"""

async def classify_message_openrouter(text, api_key):
    if not api_key:
        return {"type":"seeker","confidence":100,"reason":"AI disabled"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "qwen/qwen-2.5-72b-instruct",
        "messages": [{"role":"user","content":PROMPT_TEMPLATE.format(message=text)}],
        "max_tokens": 200
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=30) as resp:
                text_resp = await resp.text()
                if resp.status != 200:
                    logger.error("OpenRouter non-200: %s %s", resp.status, text_resp)
                    return {"type":"seeker","confidence":0,"reason":f"api_status_{resp.status}"}
                data = json.loads(text_resp)
                # best-effort لاستخراج JSON من النص
                choices = data.get("choices") or []
                content = ""
                if choices:
                    content = choices[0].get("message",{}).get("content","") or choices[0].get("text","")
                else:
                    content = data.get("text","")
                m = re.search(r'\{.*\}', content, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group())
                    except Exception:
                        pass
                return {"type":"seeker","confidence":50,"reason":content[:200]}
    except Exception as e:
        logger.exception("classify_message error: %s", e)
        return {"type":"seeker","confidence":0,"reason":"exception"}

# -------------------------
# Telegram engine: يدير عملاء Telethon في حلقة منفصلة (thread + asyncio loop)
# -------------------------
class TelegramEngine:
    def __init__(self):
        self.clients = {}  # phone -> client
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def get_settings(self):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
            cur.close()
            return {k: v for k, v in rows}
        except Exception as e:
            logger.exception("get_settings error: %s", e)
            return {}
        finally:
            if 'conn' in locals() and conn:
                put_db(conn)

    def get_keywords(self):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT keyword FROM keywords")
            rows = [r[0] for r in cur.fetchall()]
            cur.close()
            return [r.lower() for r in rows]
        except Exception as e:
            logger.exception("get_keywords error: %s", e)
            return []
        finally:
            if 'conn' in locals() and conn:
                put_db(conn)

    async def _run_client(self, account):
        phone = account['phone']
        session_file = os.path.join(SESSIONS_DIR, f"{phone}.session")
        client = TelegramClient(session_file, int(account['api_id']), account['api_hash'])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log_event(f"⚠️ الحساب {phone} ليس مفوّض. يحتاج تسجيل دخول يدوياً.")
                await client.disconnect()
                return

            self.clients[phone] = client
            log_event(f"🚀 الحساب {phone} متصل وبدأ الرصد.")

            @client.on(events.NewMessage)
            async def _on_new_message(event):
                try:
                    settings = self.get_settings()
                    if settings.get("radar_status") != "1":
                        return
                    if event.is_private or getattr(event.message, "out", False):
                        return
                    text = (event.message.message or "").lower()
                    keywords = self.get_keywords()
                    found = next((k for k in keywords if k in text), None)
                    if found:
                        log_event(f"🔍 كلمة '{found}' رصدت في مجموعة عبر {phone}")
                        ai_enabled = settings.get("ai_enabled") == "1"
                        api_key = settings.get("openrouter_api_key")
                        is_seeker = True
                        if ai_enabled and api_key:
                            result = await classify_message_openrouter(event.message.message or "", api_key)
                            log_event(f"🧠 AI classified: {result}")
                            if result.get("type") == "marketer" and int(result.get("confidence",0)) > 60:
                                is_seeker = False
                        if is_seeker:
                            await self._forward_alert(client, event, account.get("alert_group"))
                except Exception as e:
                    logger.exception("_on_new_message error: %s", e)

            await client.run_until_disconnected()
        except errors.FloodWaitError as e:
            log_event(f"⏳ Flood wait {e.seconds} secs for {phone}")
            await asyncio.sleep(e.seconds)
        except errors.SessionPasswordNeededError:
            log_event(f"🔐 حساب {phone} يحتاج 2FA - سجّل الدخول يدوياً")
        except Exception as e:
            log_event(f"❌ توقف الحساب {phone}: {e}")
        finally:
            try:
                if phone in self.clients:
                    del self.clients[phone]
                await client.disconnect()
            except Exception:
                pass

    async def _forward_alert(self, client, event, target_group):
        try:
            sender = await event.get_sender()
            chat = await event.get_chat()
            sender_name = ((getattr(sender,'first_name','') or '') + ' ' + (getattr(sender,'last_name','') or '')).strip() or "غير معروف"
            sender_usr = f"@{sender.username}" if getattr(sender,'username',None) else "لا يوجد يوزر"
            chat_title = getattr(chat,'title','مجموعة غير معروفة')
            chat_username = getattr(chat,'username',None)
            if chat_username:
                chat_link = f"https://t.me/{chat_username}"
            else:
                try:
                    chat_link = f"https://t.me/c/{chat.id}/{event.id}"
                except Exception:
                    chat_link = "لا يوجد رابط"

            footer = f"\n\n━━━━━━━━━━\nرادار ذكي - طلب مساعدة\nالنص: {event.message.message}\nالمرسل: {sender_name} - {sender_usr}\nالمجموعة: {chat_title}\nرابط: {chat_link}\n━━━━━━━━━━"

            if not target_group:
                log_event("⚠️ لم تُحدد مجموعة تنبيهات لهذا الحساب.")
                return

            dest = int(target_group) if str(target_group).lstrip('-').isdigit() else target_group
            try:
                await client.forward_messages(dest, event.message)
                await client.send_message(dest, footer)
                log_event("✅ تم تحويل/إرسال التنبيه بنجاح.")
            except errors.ChatForwardsRestrictedError:
                full_text = f"{event.message.message}\n\n(تم إرسال نسخة بسبب منع التحويل){footer}"
                if event.message.media:
                    await client.send_file(dest, event.message.media, caption=full_text)
                else:
                    await client.send_message(dest, full_text)
                log_event("✅ تم إرسال نسخة بدلاً من التحويل.")
            except Exception as e:
                log_event(f"❌ خطأ أثناء إرسال التنبيه: {e}")
        except Exception as e:
            log_event(f"❌ خطأ في _forward_alert: {e}")

    def start_all(self):
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT phone, api_id, api_hash, alert_group FROM accounts WHERE enabled = TRUE")
            rows = cur.fetchall()
            cur.close()
            for row in rows:
                acc = {"phone": row[0], "api_id": int(row[1]), "api_hash": row[2], "alert_group": row[3]}
                asyncio.run_coroutine_threadsafe(self._run_client(acc), self.loop)
        except Exception as e:
            logger.exception("start_all error: %s", e)
        finally:
            if conn:
                put_db(conn)

    def stop_all(self):
        for phone, client in list(self.clients.items()):
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), self.loop)
            except Exception as e:
                logger.error("stop client error %s: %s", phone, e)

radar_engine = TelegramEngine()

# -------------------------
# Flask-Login: المستخدم البسيط
# -------------------------
class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

# عند الطلبات غير المصرح بها (AJAX) نرجع JSON 401 بدل إعادة توجيه HTML
@login_manager.unauthorized_handler
def unauthorized_callback():
    is_ajax = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in (request.headers.get("Accept",""))
    if is_ajax:
        return jsonify({"status":"error","msg":"غير مصرح - يرجى تسجيل الدخول"}), 401
    return redirect(url_for("login"))

# -------------------------
# Routes: صفحات بسيطة + API endpoints
# -------------------------
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        admin_email = get_setting("admin_email", ADMIN_EMAIL)
        admin_password_hash = get_setting("admin_password", generate_password_hash(ADMIN_PASSWORD))
        if email == admin_email and check_password_hash(admin_password_hash, password):
            login_user(User(email))
            flash("تم تسجيل الدخول بنجاح", "success")
            return redirect(url_for("index"))
        flash("بيانات الدخول خاطئة", "danger")
    # إن لم توجد قوالب يمكن إرجاع نصي بسيط
    try:
        return render_template("login.html")
    except Exception:
        return "<h2>Login Page</h2><form method='post'><input name='email'><input name='password' type='password'><button type='submit'>Login</button></form>"

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT keyword FROM keywords")
        keywords = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT phone, api_id, api_hash, alert_group, enabled FROM accounts")
        accounts = [{"phone":r[0],"api_id":r[1],"api_hash":r[2],"alert_group":r[3],"enabled":r[4]} for r in cur.fetchall()]
        cur.execute("SELECT content, created_at FROM logs ORDER BY created_at DESC LIMIT 100")
        logs = [{"content":r[0],"created_at":r[1]} for r in cur.fetchall()]
        cur.execute("SELECT key, value FROM settings")
        settings = {r[0]:r[1] for r in cur.fetchall()}
        cur.close()
        return render_template("index.html", keywords="\n".join(keywords), accounts=accounts, logs=logs, settings=settings, active_clients=list(radar_engine.clients.keys()))
    except Exception as e:
        logger.exception("index error: %s", e)
        return "Error loading dashboard", 500
    finally:
        if conn:
            put_db(conn)

# حفظ إعدادات (يدعم form)
@app.route("/api/settings/save", methods=["POST"])
@login_required
def save_settings():
    api_key = request.form.get("openrouter_api_key","")
    ai_enabled = "1" if request.form.get("ai_enabled") else "0"
    radar_status = request.form.get("radar_status","1")
    old_status = get_setting("radar_status","1")
    set_setting("openrouter_api_key", api_key)
    set_setting("ai_enabled", ai_enabled)
    set_setting("radar_status", radar_status)
    if radar_status == "0" and old_status == "1":
        radar_engine.stop_all()
        log_event("🛑 تم إيقاف الرادار يدوياً.")
    elif radar_status == "1" and old_status == "0":
        radar_engine.start_all()
        log_event("▶️ تم تشغيل الرادار.")
    flash("تم حفظ الإعدادات", "success")
    return redirect(url_for("index"))

@app.route("/keyword/add", methods=["POST"])
@login_required
def add_keyword():
    keyword = request.form.get("keyword","").strip()
    if keyword:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING",(keyword,))
            conn.commit()
            cur.close()
            log_event(f"➕ تم إضافة كلمة مفتاحية: {keyword}")
            flash("تم إضافة الكلمة", "success")
        except Exception as e:
            logger.exception("add_keyword error: %s", e)
            flash("فشل إضافة الكلمة", "danger")
        finally:
            if conn:
                put_db(conn)
    return redirect(url_for("index"))

@app.route("/keyword/delete/<keyword>")
@login_required
def delete_keyword(keyword):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM keywords WHERE keyword = %s", (keyword,))
        conn.commit()
        cur.close()
        log_event(f"🗑️ تم حذف كلمة مفتاحية: {keyword}")
        flash("تم حذف الكلمة", "success")
    except Exception as e:
        logger.exception("delete_keyword error: %s", e)
        flash("فشل حذف الكلمة", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for("index"))

# --- نقطتا التحقق لإضافة حساب Telegram (تعمل مع JSON أو form) ---
@app.route("/account/add_step1", methods=["POST"])
@login_required
def account_step1():
    # قبول JSON و form data
    data = request.get_json(silent=True) or request.form or {}
    phone = data.get("phone")
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    if not phone or not api_id or not api_hash:
        return jsonify({"status":"error","msg":"جميع الحقول مطلوبة (phone, api_id, api_hash)"}), 400

    try:
        api_id = int(api_id)
    except ValueError:
        return jsonify({"status":"error","msg":"api_id يجب أن يكون عددًا"}), 400

    # دالة async لطلب كود التحقق عبر Telethon داخل حلقة الرادار
    async def _req_code():
        session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            # خزن العميل مؤقتاً مع phone_code_hash
            pending_logins[phone] = {
                "client": client,
                "api_id": api_id,
                "api_hash": api_hash,
                "phone_code_hash": sent.phone_code_hash,
                "created_at": time.time(),
                "expires_at": time.time() + PENDING_TTL_SECONDS
            }
            return {"status":"success","msg":"تم إرسال الكود"}
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            return {"status":"error","msg":"رقم الهاتف غير صالح"}
        except errors.ApiIdInvalidError:
            await client.disconnect()
            return {"status":"error","msg":"API ID أو API Hash غير صحيح"}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {"status":"error","msg":str(e)}

    future = asyncio.run_coroutine_threadsafe(_req_code(), radar_engine.loop)
    try:
        res = future.result(timeout=25)
        return jsonify(res)
    except Exception as e:
        logger.exception("account_step1 future error: %s", e)
        return jsonify({"status":"error","msg":f"خطأ في الاتصال: {e}"}), 500

@app.route("/account/add_step2", methods=["POST"])
@login_required
def account_step2():
    data = request.get_json(silent=True) or request.form or {}
    phone = data.get("phone")
    code = data.get("code")
    password = data.get("password", None)

    if not phone or not code:
        return jsonify({"status":"error","msg":"phone و code مطلوبان"}), 400

    if phone not in pending_logins:
        return jsonify({"status":"error","msg":"انتهت الجلسة أو لم تبدأ خطوة1"}), 400

    login_data = pending_logins[phone]
    client = login_data.get("client")
    phone_code_hash = login_data.get("phone_code_hash")

    async def _verify():
        try:
            # مهم: استخدم client الذي خزّنّا (Telethon session object)
            await client.connect()
            try:
                # بعض نسخ Telethon تطلب phone_code_hash كوسيط
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except TypeError:
                # fallback لsignature مختلفة
                await client.sign_in(phone, code)
            return "success"
        except errors.SessionPasswordNeededError:
            if not password:
                return "need_password"
            try:
                await client.sign_in(password=password)
                return "success"
            except errors.PasswordHashInvalidError:
                return "password_invalid"
            except Exception as e:
                return f"error:{e}"
        except errors.PhoneCodeInvalidError:
            return "code_invalid"
        except Exception as e:
            return f"error:{e}"

    future = asyncio.run_coroutine_threadsafe(_verify(), radar_engine.loop)
    try:
        res = future.result(timeout=40)
    except Exception as e:
        logger.exception("account_step2 verify future error: %s", e)
        return jsonify({"status":"error","msg":f"خطأ في الاتصال: {e}"}), 500

    if res == "success":
        # حفظ الحساب في DB وتشغيله في الرادار
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO accounts (phone, api_id, api_hash, alert_group, enabled) VALUES (%s,%s,%s,%s,TRUE) ON CONFLICT (phone) DO UPDATE SET api_id=EXCLUDED.api_id, api_hash=EXCLUDED.api_hash, alert_group=EXCLUDED.alert_group, enabled=TRUE",
                (phone, login_data.get("api_id"), login_data.get("api_hash"), data.get("alert_group",""))
            )
            conn.commit()
            cur.close()
            log_event(f"✅ تم إضافة الحساب: {phone}")
            # تشغيل العميل في المحرك
            acc = {"phone": phone, "api_id": login_data.get("api_id"), "api_hash": login_data.get("api_hash"), "alert_group": data.get("alert_group","")}
            asyncio.run_coroutine_threadsafe(radar_engine._run_client(acc), radar_engine.loop)
            # تنظيف pending
            try:
                del pending_logins[phone]
            except KeyError:
                pass
            return jsonify({"status":"success"})
        except Exception as e:
            logger.exception("account_step2 DB save error: %s", e)
            return jsonify({"status":"error","msg":str(e)}), 500
        finally:
            if conn:
                put_db(conn)
    elif res == "need_password":
        return jsonify({"status":"need_password"})
    elif res == "password_invalid":
        return jsonify({"status":"error","msg":"كلمة السر خاطئة"}), 400
    elif res == "code_invalid":
        return jsonify({"status":"error","msg":"الرمز غير صحيح"}), 400
    else:
        return jsonify({"status":"error","msg":str(res)}), 400

@app.route("/account/update_group", methods=["POST"])
@login_required
def update_account_group():
    phone = request.form.get("phone")
    group_id = request.form.get("group_id", "")
    if not phone:
        flash("رقم الهاتف مطلوب", "danger")
        return redirect(url_for("index"))
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE accounts SET alert_group = %s WHERE phone = %s", (group_id, phone))
        conn.commit()
        cur.close()
        log_event(f"تم تحديث مجموعة التنبيهات للحساب {phone} -> {group_id}")
        flash("تم التحديث", "success")
    except Exception as e:
        logger.exception("update_account_group error: %s", e)
        flash("فشل التحديث", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for("index"))

@app.route("/account/delete/<phone>")
@login_required
def delete_account(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM accounts WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
        if phone in radar_engine.clients:
            try:
                asyncio.run_coroutine_threadsafe(radar_engine.clients[phone].disconnect(), radar_engine.loop)
            except Exception:
                pass
            del radar_engine.clients[phone]
        session_file = os.path.join(SESSIONS_DIR, f"{phone}.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception as e:
                logger.error("remove session file error: %s", e)
        log_event(f"🗑️ تم حذف الحساب {phone}")
        flash("تم الحذف", "success")
    except Exception as e:
        logger.exception("delete_account error: %s", e)
        flash("فشل الحذف", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for("index"))

# -------------------------
# When app starts
# -------------------------
@app.before_first_request
def startup_tasks():
    # init db once
    init_db()
    # start radar engine if setting == '1'
    try:
        if get_setting("radar_status","1") == "1":
            radar_engine.start_all()
    except Exception as e:
        logger.exception("startup radar start error: %s", e)

# Ensure engine stops cleanly on exit
def shutdown():
    logger.info("Shutting down - stopping radar engine")
    try:
        radar_engine.stop_all()
    except Exception as e:
        logger.exception("shutdown error: %s", e)

atexit.register(shutdown)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting Flask app on 0.0.0.0:%s", port)
    # ملاحظة: Gunicorn أعلى أداء للـ production. لكن للتشغيل التقليدي:
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
