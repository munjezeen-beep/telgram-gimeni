# app.py
# تطبيق Radar - Flask + Telegram على Railway
# المميزات:
# - pool لـ PostgreSQL
# - دعم Telegram async (background loop thread)
# - pending_logins لتخزين بيانات تسجيل الدخول المؤقتة
# - AJAX: إرسال JSON (401 للأخطاء، 200 للنجاح)
# - معالجة Telegram (FloodWait، 2FA)
# - تخزين الكلمات المفتاحية
# - routes: /account/add_step1 و /account/add_step2 (JSON و form-data)
#
# ملاحظة: استخدم Gunicorn للـ Production. في Procfile:
# web: gunicorn -w 4 -b 0.0.0.0:$PORT app:app
#
# المتطلبات (requirements.txt):
# flask
# flask-login
# aiohttp
# telegram
# psycopg2-binary
# gunicorn (للـ Production)
# python-dotenv (اختياري)
#
# ملاحظة مهمة: هذا الملف يعاد deploy تلقائياً عند تغييره في Railway.

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

from telegram import TelegramClient, events, errors

# ==========================================
# إعدادات أساسية
# ==========================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "radar-super-secret-key-2026")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/radar")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # غير آمن - استخدم متغيرات البيئة
OPENROUTER_API_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

# ==========================================
# إعدادات السجلات
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "radar.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("RadarApp")

# ==========================================
# Flask app و LoginManager
# ==========================================
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = SECRET_KEY

login_manager = LoginManager(app)
login_manager.login_view = "login"

# ==========================================
# قاعدة البيانات
# ==========================================
# Pool لـ PostgreSQL
db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    logger.info("✅ PostgreSQL pool created successfully.")
except Exception as e:
    logger.exception("❌ Failed to create DB pool - will retry on demand: %s", e)
    db_pool = None

def get_db():
    """الحصول على اتصال من pool أو إنشاء واحد جديد إذا لم يكن pool متاحاً."""
    if db_pool:
        return db_pool.getconn()
    return psycopg2.connect(DATABASE_URL)

def put_db(conn):
    """إرجاع الاتصال إلى pool أو إغلاقه إذا لم يكن pool متاحاً."""
    try:
        if db_pool and conn:
            db_pool.putconn(conn)
        else:
            if conn:
                conn.close()
    except Exception as e:
        logger.exception("put_db error: %s", e)

# ==========================================
# init_db: إنشاء الجداول وإدراج البيانات الأولية
# ==========================================
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
        # إدراج البيانات الأولية
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("admin_email", ADMIN_EMAIL))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("admin_password", generate_password_hash(ADMIN_PASSWORD)))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("ai_enabled", "0"))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("openrouter_api_key", ""))
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ("radar_status", "1"))
        # إدراج الكلمات المفتاحية الافتراضية
        cur.execute("SELECT COUNT(*) FROM keywords")
        count = cur.fetchone()[0]
        if count == 0:
            default_keywords = ["بحث", "تسويق", "دراسة", "تطوير", "مشروع", "تحليل", "assignment", "research", "project"]
            for kw in default_keywords:
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING", (kw,))
        conn.commit()
        cur.close()
        logger.info("✅ Database initialized successfully.")
    except Exception as e:
        logger.exception("init_db error: %s", e)
    finally:
        if conn:
            put_db(conn)

def log_event(content):
    """تسجيل الأحداث في قاعدة البيانات."""
    conn = None
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

# ==========================================
# pending_logins: تخزين بيانات تسجيل الدخول المؤقتة
# ==========================================
pending_logins = {}
PENDING_TTL_SECONDS = int(os.environ.get("PENDING_TTL_SECONDS", 300))  # 5 دقائق

def cleanup_pending_logins():
    now = time.time()
    expired = [p for p, info in pending_logins.items() if info.get("expires_at", 0) <= now]
    for p in expired:
        try:
            client = pending_logins[p].get("client")
            if client:
                # قطع الاتصال بشكل آمن
                try:
                    asyncio.run_coroutine_threadsafe(client.disconnect(), radar_engine.loop)
                except Exception:
                    pass
        except Exception:
            pass
        del pending_logins[p]
        logger.info("pending_logins: expired and cleaned %s", p)

# خيط منفصل للتنظيف
def pending_cleaner_worker():
    while True:
        try:
            cleanup_pending_logins()
        except Exception as e:
            logger.exception("pending_cleaner error: %s", e)
        time.sleep(30)

_cleaner_thread = threading.Thread(target=pending_cleaner_worker, daemon=True)
_cleaner_thread.start()

# ==========================================
# AI classification (OpenRouter) - تصنيف الرسائل
# ==========================================
PROMPT_TEMPLATE = """
أنت مساعد متخصص في تصنيف الرسائل. حدد ما إذا كانت الرسالة تتعلق بـ seeker أو marketer.
إرجع JSON: {"type":"seeker"/"marketer","confidence":int,"reason":"السبب"}
الرسالة: {message}
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
                # best-effort محاولة استخراج JSON من الرد
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

# ==========================================
# Telegram engine: محرك Telegram
# (thread + asyncio loop)
# ==========================================
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
                log_event(f"⚠️ حساب {phone} غير مصرح. يحتاج تسجيل دخول جديد.")
                await client.disconnect()
                return

            self.clients[phone] = client
            log_event(f"✅ حساب {phone} متصل بنجاح.")

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
                        log_event(f"🔔 كلمة مفتاحية '{found}' وجدت في رسالة من {phone}")
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
            log_event(f"⏱️ Flood wait {e.seconds} secs for {phone}")
            await asyncio.sleep(e.seconds)
        except errors.SessionPasswordNeededError:
            log_event(f"🔐 حساب {phone} يحتاج 2FA - يحتاج تسجيل دخول جديد.")
        except Exception as e:
            log_event(f"❌ خطأ في حساب {phone}: {e}")
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
            sender_name = ((getattr(sender,'first_name','') or '') + ' ' + (getattr(sender,'last_name','') or '')).strip() or "مستخدم مجهول"
            sender_usr = f"@{sender.username}" if getattr(sender,'username',None) else "لا يوجد username"
            chat_title = getattr(chat,'title','مجموعة مجهولة')
            chat_username = getattr(chat,'username',None)
            if chat_username:
                chat_link = f"https://t.me/{chat_username}"
            else:
                try:
                    chat_link = f"https://t.me/c/{chat.id}/{event.id}"
                except Exception:
                    chat_link = "لا يوجد رابط"

            footer = f"\n\n═══════════════════════════════\nالرسالة: {event.message.message}\nالمرسل: {sender_name} - {sender_usr}\nالمجموعة: {chat_title}\nالرابط: {chat_link}\n═══════════════════════════════"

            if not target_group:
                log_event("⚠️ لا يوجد مجموعة تنبيهات محددة.")
                return

            dest = int(target_group) if str(target_group).lstrip('-').isdigit() else target_group
            try:
                await client.forward_messages(dest, event.message)
                await client.send_message(dest, footer)
                log_event("✅ تم إرسال التنبيه بنجاح.")
            except errors.ChatForwardsRestrictedError:
                full_text = f"{event.message.message}\n\n(لا يمكن إعادة توجيه الرسائل في هذه المجموعة)"
                await client.send_message(dest, full_text)
        except Exception as e:
            logger.exception("_forward_alert error: %s", e)

radar_engine = TelegramEngine()

# ==========================================
# Flask Routes
# ==========================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        admin_email = get_setting('admin_email')
        admin_password = get_setting('admin_password')
        if email == admin_email and check_password_hash(admin_password, password):
            session['user'] = email
            flash('تم تسجيل الدخول بنجاح', 'success')
            return redirect(url_for('dashboard'))
        flash('بيانات دخول غير صحيحة', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    flash('تم تسجيل الخروج', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('dashboard.html')

@app.route('/account/add_step1', methods=['GET', 'POST'])
def add_step1():
    """الخطوة الأولى: إدخال رقم الهاتف و API credentials"""
    if request.method == 'POST':
        try:
            data = request.get_json() or request.form
            phone = data.get('phone', '').strip()
            api_id = data.get('api_id', '').strip()
            api_hash = data.get('api_hash', '').strip()
            
            if not phone or not api_id or not api_hash:
                return jsonify({"status": "error", "message": "جميع الحقول مطلوبة"}), 400
            
            # تخزين البيانات في pending_logins
            pending_logins[phone] = {
                "api_id": api_id,
                "api_hash": api_hash,
                "step": 1,
                "created_at": time.time(),
                "expires_at": time.time() + PENDING_TTL_SECONDS
            }
            
            log_event(f"📝 بدء تسجيل حساب جديد: {phone}")
            return jsonify({"status": "success", "message": "تم حفظ البيانات. انتقل للخطوة التالية"}), 200
        except Exception as e:
            logger.exception("add_step1 error: %s", e)
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return render_template('add_step1.html')

@app.route('/account/add_step2', methods=['GET', 'POST'])
def add_step2():
    """الخطوة الثانية: تأكيد رمز التحقق و alert group"""
    if request.method == 'POST':
        try:
            data = request.get_json() or request.form
            phone = data.get('phone', '').strip()
            verification_code = data.get('verification_code', '').strip()
            alert_group = data.get('alert_group', '').strip()
            
            if not phone or phone not in pending_logins:
                return jsonify({"status": "error", "message": "جلسة غير صحيحة"}), 400
            
            if not verification_code:
                return jsonify({"status": "error", "message": "رمز التحقق مطلوب"}), 400
            
            pending_info = pending_logins[phone]
            
            # إنشاء حساب جديد في قاعدة البيانات
            conn = None
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO accounts (phone, api_id, api_hash, alert_group) VALUES (%s, %s, %s, %s) ON CONFLICT (phone) DO UPDATE SET api_hash = EXCLUDED.api_hash, alert_group = EXCLUDED.alert_group",
                    (phone, pending_info['api_id'], pending_info['api_hash'], alert_group)
                )
                conn.commit()
                cur.close()
                log_event(f"✅ تم إضافة حساب جديد: {phone}")
            except Exception as e:
                logger.exception("add_step2 DB error: %s", e)
                return jsonify({"status": "error", "message": "خطأ في قاعدة البيانات"}), 500
            finally:
                if conn:
                    put_db(conn)
            
            # حذف من pending_logins
            del pending_logins[phone]
            
            return jsonify({"status": "success", "message": "تم إضافة الحساب بنجاح"}), 200
        except Exception as e:
            logger.exception("add_step2 error: %s", e)
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return render_template('add_step2.html')

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    """الحصول على قائمة الحسابات"""
    if 'user' not in session:
        return jsonify({"status": "error"}), 401
    
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT phone, api_id, alert_group, enabled, created_at FROM accounts")
        rows = cur.fetchall()
        cur.close()
        accounts = [
            {
                "phone": r[0],
                "api_id": r[1],
                "alert_group": r[2],
                "enabled": r[3],
                "created_at": r[4].isoformat() if r[4] else None
            }
            for r in rows
        ]
        return jsonify({"status": "success", "accounts": accounts}), 200
    except Exception as e:
        logger.exception("get_accounts error: %s", e)
        return jsonify({"status": "error"}), 500
    finally:
        if conn:
            put_db(conn)

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    """الحصول على أو تحديث الإعدادات"""
    if 'user' not in session:
        return jsonify({"status": "error"}), 401
    
    if request.method == 'GET':
        try:
            settings = radar_engine.get_settings()
            return jsonify({"status": "success", "settings": settings}), 200
        except Exception as e:
            logger.exception("api_settings GET error: %s", e)
            return jsonify({"status": "error"}), 500
    
    if request.method == 'POST':
        try:
            data = request.get_json()
            for key, value in data.items():
                set_setting(key, value)
            log_event(f"⚙️ تم تحديث الإعدادات")
            return jsonify({"status": "success"}), 200
        except Exception as e:
            logger.exception("api_settings POST error: %s", e)
            return jsonify({"status": "error"}), 500

# ==========================================
# تشغيل التطبيق
# ==========================================
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
