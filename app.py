# ================================================================
# نظام رادار تليجرام الذكي v4.0 (Cloud Edition - PostgreSQL)
# المبرمج: Gemini - المشروع: رادار الخليج للذكاء الاصطناعي
# ================================================================

import os
import re
import json
import asyncio
import logging
import aiohttp
from datetime import datetime
from threading import Thread
from functools import wraps

# مكتبات الويب والواجهة
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# مكتبات تليجرام والشبكة
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

# مكتبات PostgreSQL
import psycopg2
from psycopg2.extras import DictCursor

# --- 1. إعداد السجلات (Logging) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("RadarSystem")

# --- 2. إعدادات بيئة العمل (Environment Variables) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "radar-secret-2026")
PORT = int(os.environ.get("PORT", 8080))

# إعدادات الأدمن الافتراضية
DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@gmail.com")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "123456")

# --- 3. محرك قاعدة البيانات (PostgreSQL Manager) ---
class DBManager:
    def __init__(self):
        # تصحيح بروتوكول الرابط ليتوافق مع psycopg2
        url = DATABASE_URL
        if url and url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        self.db_url = url
        self._init_db()

    def get_connection(self):
        return psycopg2.connect(self.db_url, cursor_factory=DictCursor)

    def _init_db(self):
        """إنشاء الجداول اللازمة إذا لم تكن موجودة"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # جدول الحسابات
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS accounts (
                            phone TEXT PRIMARY KEY,
                            api_id INTEGER NOT NULL,
                            api_hash TEXT NOT NULL,
                            session_str TEXT,
                            alert_group TEXT,
                            enabled INTEGER DEFAULT 1,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    # جدول الكلمات المفتاحية
                    cur.execute('CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY)')
                    # جدول الإعدادات العامة
                    cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
                    # جدول السجلات (Logs)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS activity_logs (
                            id SERIAL PRIMARY KEY,
                            content TEXT,
                            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # إدخال البيانات الافتراضية
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                               ('admin_email', DEFAULT_ADMIN_EMAIL))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                               ('admin_password', generate_password_hash(DEFAULT_ADMIN_PASS)))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('ai_enabled', '0'))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('openrouter_api_key', ''))
                conn.commit()
            logger.info("تم تهيئة قاعدة البيانات بنجاح.")
        except Exception as e:
            logger.error(f"خطأ في تهيئة قاعدة البيانات: {e}")

    def log_activity(self, content):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO activity_logs (content) VALUES (%s)", (content,))
                conn.commit()
        except: pass

db = DBManager()

# --- 4. إعداد تطبيق Flask و Flask-Login ---
app = Flask(__name__)
app.secret_key = SECRET_KEY

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

# --- 5. محرك تليجرام المتقدم (Telegram Engine) ---
class TelegramEngine:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.clients = {}  # {phone: client_object}
        self.temp_clients = {} # لتخزين الكليانت أثناء مرحلة التسجيل
        self.is_running = False
        # بدء حلقة الأحداث في خيط منفصل
        self.thread = Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coroutine(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def start_all(self):
        """تشغيل جميع الحسابات المفعلة عند بدء التطبيق"""
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM accounts WHERE enabled = 1")
                accounts = cur.fetchall()
                for acc in accounts:
                    if acc['session_str']:
                        await self.activate_client(acc)

    async def activate_client(self, acc):
        """تنشيط عميل تليجرام واحد"""
        phone = acc['phone']
        try:
            client = TelegramClient(
                StringSession(acc['session_str']), 
                acc['api_id'], 
                acc['api_hash'],
                device_model="RadarCloud-v4",
                system_version="Linux"
            )
            await client.connect()
            if await client.is_user_authorized():
                self.clients[phone] = client
                self._attach_handlers(client, acc)
                logger.info(f"تم تنشيط الرادار للحساب: {phone}")
                db.log_activity(f"تنشيط الحساب: {phone}")
            else:
                logger.warning(f"الجلسة منتهية للحساب: {phone}")
        except Exception as e:
            logger.error(f"فشل تنشيط الحساب {phone}: {e}")

    def _attach_handlers(self, client, acc):
        """إضافة مستمع الرسائل لكل حساب"""
        @client.on(events.NewMessage)
        async def my_handler(event):
            if not event.is_private and not event.is_group: return
            await self.analyze_message(event, acc)

    async def analyze_message(self, event, acc):
        """تحليل الرسائل الواردة بناءً على الكلمات المفتاحية"""
        text = event.raw_text
        if not text: return

        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword FROM keywords")
                keywords = [r['keyword'] for r in cur.fetchall()]
        
        # البحث عن تطابق
        match = False
        matched_word = ""
        for kw in keywords:
            if re.search(rf'\b{re.escape(kw)}\b', text, re.IGNORECASE):
                match = True
                matched_word = kw
                break
        
        if match:
            await self.send_alert(event, acc, matched_word)

    async def send_alert(self, event, acc, word):
        """إرسال تنبيه لمجموعة التلجرام المحددة"""
        try:
            sender = await event.get_sender()
            sender_name = getattr(sender, 'first_name', 'مجهول')
            chat = await event.get_chat()
            chat_title = getattr(chat, 'title', 'دردشة خاصة')
            
            alert_msg = (
                f"🚨 **تنبيه الرادار الذكي**\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔍 **الكلمة المكتشفة:** `{word}`\n"
                f"👤 **المرسل:** {sender_name}\n"
                f"📍 **المصدر:** {chat_title}\n"
                f"📱 **الحساب المراقب:** {acc['phone']}\n"
                f"💬 **النص:**\n{event.raw_text[:200]}...\n"
                f"━━━━━━━━━━━━━━\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # إرسال التنبيه
            target_client = self.clients.get(acc['phone'])
            if target_client and acc['alert_group']:
                await target_client.send_message(acc['alert_group'], alert_msg)
                db.log_activity(f"تم إرسال تنبيه بكلمة ({word}) من حساب {acc['phone']}")
        except Exception as e:
            logger.error(f"خطأ في إرسال التنبيه: {e}")

    # --- عمليات تسجيل الحسابات الجديدة (Login Flow) ---
    async def request_code(self, phone, api_id, api_hash):
        try:
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            self.temp_clients[phone] = {
                'client': client,
                'phone_code_hash': sent.phone_code_hash,
                'api_id': api_id,
                'api_hash': api_hash
            }
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def verify_code(self, phone, code, password=None):
        if phone not in self.temp_clients:
            return {"success": False, "message": "انتهت الجلسة المؤقتة"}
        
        data = self.temp_clients[phone]
        client = data['client']
        try:
            await client.sign_in(phone, code, phone_code_hash=data['phone_code_hash'])
        except errors.SessionPasswordNeededError:
            if not password:
                return {"success": False, "needs_password": True}
            await client.sign_in(password=password)
        except Exception as e:
            return {"success": False, "message": str(e)}

        # نجاح تسجيل الدخول - حفظ الجلسة
        session_str = client.session.save()
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO accounts (phone, api_id, api_hash, session_str)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE SET 
                    session_str = EXCLUDED.session_str, 
                    api_id = EXCLUDED.api_id, 
                    api_hash = EXCLUDED.api_hash
                ''', (phone, data['api_id'], data['api_hash'], session_str))
            conn.commit()
        
        # تفعيل الحساب فوراً
        acc_data = {
            'phone': phone, 'api_id': data['api_id'], 
            'api_hash': data['api_hash'], 'session_str': session_str,
            'alert_group': None, 'enabled': 1
        }
        await self.activate_client(acc_data)
        del self.temp_clients[phone]
        return {"success": True}

telegram_engine = TelegramEngine()

# --- 6. واجهة الويب (Flask Routes) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key='admin_email'")
                admin_email = cur.fetchone()[0]
                cur.execute("SELECT value FROM settings WHERE key='admin_password'")
                admin_pass = cur.fetchone()[0]
                
                if email == admin_email and check_password_hash(admin_pass, password):
                    user = User(email)
                    login_user(user)
                    return redirect(url_for('dashboard'))
        flash("خطأ في بيانات الدخول")
    return render_template('login.html') # سيتم توفيره في رد لاحق أو مدمج

@app.route('/')
@login_required
def dashboard():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accounts ORDER BY created_at DESC")
            accounts = cur.fetchall()
            cur.execute("SELECT * FROM keywords")
            keywords = [r['keyword'] for r in cur.fetchall()]
            cur.execute("SELECT * FROM activity_logs ORDER BY timestamp DESC LIMIT 10")
            logs = cur.fetchall()
            cur.execute("SELECT value FROM settings WHERE key='ai_enabled'")
            ai_status = cur.fetchone()[0]
    return render_template('index.html', accounts=accounts, keywords=keywords, logs=logs, ai_status=ai_status)

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    phone = request.form.get('phone')
    api_id = request.form.get('api_id')
    api_hash = request.form.get('api_hash')
    res = telegram_engine.run_coroutine(telegram_engine.request_code(phone, api_id, api_hash))
    if res['success']:
        return jsonify({"status": "code_sent", "phone": phone})
    return jsonify({"status": "error", "message": res['message']})

@app.route('/verify_step', methods=['POST'])
@login_required
def verify_step():
    phone = request.form.get('phone')
    code = request.form.get('code')
    password = request.form.get('password')
    res = telegram_engine.run_coroutine(telegram_engine.verify_code(phone, code, password))
    return jsonify(res)

@app.route('/delete_account/<phone>')
@login_required
def delete_account(phone):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE phone = %s", (phone,))
        conn.commit()
    if phone in telegram_engine.clients:
        # محاولة إغلاق العميل برفق
        client = telegram_engine.clients.pop(phone)
        telegram_engine.run_coroutine(client.disconnect())
    flash(f"تم حذف الحساب {phone}")
    return redirect(url_for('dashboard'))

@app.route('/update_alert_group', methods=['POST'])
@login_required
def update_alert_group():
    phone = request.form.get('phone')
    group = request.form.get('group')
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE accounts SET alert_group = %s WHERE phone = %s", (group, phone))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/manage_keywords', methods=['POST'])
@login_required
def manage_keywords():
    action = request.form.get('action')
    word = request.form.get('keyword', '').strip()
    if not word: return redirect(url_for('dashboard'))
    
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            if action == 'add':
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT DO NOTHING", (word,))
            elif action == 'delete':
                cur.execute("DELETE FROM keywords WHERE keyword = %s", (word,))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/toggle_ai', methods=['POST'])
@login_required
def toggle_ai():
    status = request.form.get('status') # '1' or '0'
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE settings SET value = %s WHERE key = 'ai_enabled'", (status,))
        conn.commit()
    return jsonify({"success": True})

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- 7. تشغيل النظام ---
if __name__ == '__main__':
    # تهيئة أولية عند التشغيل
    try:
        telegram_engine.run_coroutine(telegram_engine.start_all())
    except Exception as e:
        logger.error(f"خطأ في بدء حسابات تليجرام: {e}")
    
    # تشغيل Flask
    app.run(host='0.0.0.0', port=PORT, debug=False)
