# ================================================================
# نظام رادار الخليج الذكي v5.0 (نسخة الإنتاج الشاملة)
# متوافق تماماً مع PostgreSQL و Railway و Nixpacks
# ================================================================

import os
import re
import json
import asyncio
import logging
import aiohttp
import secrets
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

# --- 1. إعدادات السجلات والبيئة ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("RadarPro_V5")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(24))

# إعدادات قاعدة البيانات
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- 2. محرك قاعدة البيانات المتقدم (Postgres Manager) ---
class DBManager:
    def __init__(self):
        # تصحيح الرابط لـ PostgreSQL
        url = DATABASE_URL
        if url and url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        self.db_url = url
        self._init_db()

    def get_connection(self):
        """إنشاء اتصال جديد بقاعدة البيانات"""
        return psycopg2.connect(self.db_url, cursor_factory=DictCursor)

    def _init_db(self):
        """تهيئة الجداول بنظام Postgres"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # جدول الحسابات المراقبِة
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
                    # جدول الإعدادات العامة للسيستم
                    cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
                    # جدول سجلات النشاط (Logs)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS logs (
                            id SERIAL PRIMARY KEY,
                            content TEXT,
                            log_type TEXT DEFAULT 'info',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # إدخال الإعدادات الافتراضية إذا لم تكن موجودة
                    admin_mail = os.environ.get("ADMIN_EMAIL", "admin@gmail.com")
                    admin_pass = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "123456"))
                    
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('admin_email', admin_mail))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('admin_password', admin_pass))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('ai_enabled', '0'))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('openrouter_key', ''))
                    
                conn.commit()
            logger.info("قاعدة البيانات جاهزة للعمل.")
        except Exception as e:
            logger.error(f"خطأ في تهيئة قاعدة البيانات: {e}")

    def add_log(self, content, l_type='info'):
        """إضافة سجل للوحة التحكم"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO logs (content, log_type) VALUES (%s, %s)", (content, l_type))
                conn.commit()
        except Exception as e:
            logger.error(f"خطأ في إضافة السجل: {e}")

db = DBManager()

# --- 3. نظام إدارة المستخدمين (Authentication) ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id): self.id = id

@login_manager.user_loader
def load_user(user_id): return User(user_id)

# --- 4. محرك تليجرام العملاق (Telegram Radar Engine) ---
class TelegramEngine:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.clients = {}  # {phone: client_instance}
        self.temp_sessions = {} # للتخزين المؤقت أثناء تسجيل الدخول
        self.is_running = False
        
        # بدء حلقة الأحداث في خيط منفصل لضمان عدم توقف Flask
        Thread(target=self._run_event_loop, daemon=True).start()

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def start_all_clients(self):
        """تنشيط كافة الحسابات المحفوظة في قاعدة البيانات"""
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM accounts WHERE enabled = 1")
                rows = cur.fetchall()
                for row in rows:
                    if row['session_str']:
                        await self.connect_client(row)

    async def connect_client(self, acc_data):
        """ربط حساب تليجرام واحد بالرادار"""
        phone = acc_data['phone']
        try:
            client = TelegramClient(
                StringSession(acc_data['session_str']), 
                acc_data['api_id'], 
                acc_data['api_hash']
            )
            await client.connect()
            
            if await client.is_user_authorized():
                self.clients[phone] = client
                # إعداد مستمع الرسائل
                self._setup_event_handlers(client, acc_data)
                db.add_log(f"تم ربط الحساب بنجاح: {phone}")
                logger.info(f"حساب {phone} مفعل الآن.")
            else:
                db.add_log(f"الجلسة منتهية للحساب: {phone}", "warning")
                logger.warning(f"حساب {phone} يتطلب تسجيل دخول جديد.")
        except Exception as e:
            logger.error(f"فشل ربط الحساب {phone}: {e}")

    def _setup_event_handlers(self, client, acc_data):
        """إضافة مستمعات الأحداث للحساب"""
        @client.on(events.NewMessage)
        async def message_handler(event):
            await self.process_incoming_message(event, acc_data)

    async def process_incoming_message(self, event, acc_data):
        """تحليل الرسائل الواردة بناءً على الكلمات المفتاحية"""
        if not event.raw_text: return
        
        # جلب الكلمات المفتاحية الحالية
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword FROM keywords")
                keywords = [r['keyword'].lower() for r in cur.fetchall()]
        
        message_text = event.raw_text.lower()
        
        for kw in keywords:
            # استخدام Regex للبحث الدقيق عن الكلمات
            if re.search(rf'\b{re.escape(kw)}\b', message_text):
                await self.dispatch_alert(event, acc_data, kw)
                break

    async def dispatch_alert(self, event, acc_data, matched_keyword):
        """إرسال تنبيه مفصل لمجموعة الإشعارات"""
        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, 'title', 'محادثة خاصة')
            sender = await event.get_sender()
            sender_name = getattr(sender, 'first_name', 'مجهول')
            
            # تنسيق الرسالة
            alert_text = (
                f"🌟 **تم رصد تطابق جديد!**\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔑 **الكلمة:** `{matched_keyword}`\n"
                f"📡 **المصدر:** {chat_title}\n"
                f"👤 **المرسل:** {sender_name}\n"
                f"📱 **عبر حساب:** {acc_data['phone']}\n\n"
                f"💬 **النص المكتشف:**\n_{event.raw_text[:250]}..._\n"
                f"━━━━━━━━━━━━━━\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            
            # إرسال التنبيه للمجموعة المحددة لهذا الحساب
            if acc_data['alert_group']:
                await self.clients[acc_data['phone']].send_message(acc_data['alert_group'], alert_text)
                db.add_log(f"تنبيه: تم العثور على '{matched_keyword}' من {acc_data['phone']}")
        except Exception as e:
            logger.error(f"خطأ في إرسال التنبيه: {e}")

    # --- عمليات تسجيل الدخول ---
    async def init_login(self, phone, api_id, api_hash):
        try:
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            self.temp_sessions[phone] = {
                'client': client,
                'hash': sent.phone_code_hash,
                'api_id': api_id,
                'api_hash': api_hash
            }
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    async def complete_login(self, phone, code, password=None):
        if phone not in self.temp_sessions:
            return {"status": "error", "msg": "انتهت مهلة الجلسة، حاول مجدداً."}
        
        data = self.temp_sessions[phone]
        client = data['client']
        
        try:
            await client.sign_in(phone, code, phone_code_hash=data['hash'])
        except errors.SessionPasswordNeededError:
            if not password:
                return {"status": "needs_password"}
            await client.sign_in(password=password)
        except Exception as e:
            return {"status": "error", "msg": str(e)}
        
        # حفظ الجلسة في قاعدة البيانات
        session_str = client.session.save()
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO accounts (phone, api_id, api_hash, session_str) 
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE SET session_str = EXCLUDED.session_str
                ''', (phone, data['api_id'], data['api_hash'], session_str))
            conn.commit()
            
        # تشغيل الرادار فوراً لهذا الحساب
        await self.connect_client({
            'phone': phone, 'api_id': data['api_id'], 
            'api_hash': data['api_hash'], 'session_str': session_str,
            'alert_group': None, 'enabled': 1
        })
        
        del self.temp_sessions[phone]
        return {"status": "success"}

radar_engine = TelegramEngine()

# --- 5. مسارات الويب واجهة الإدارة (Web Routes) ---

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
                    login_user(User(email))
                    return redirect(url_for('dashboard'))
        flash("فشل تسجيل الدخول، تأكد من البيانات.")
    return render_template('login.html') # واجهة الدخول

@app.route('/')
@login_required
def dashboard():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accounts ORDER BY created_at DESC")
            accounts = cur.fetchall()
            cur.execute("SELECT * FROM keywords")
            keywords = [r['keyword'] for r in cur.fetchall()]
            cur.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 20")
            recent_logs = cur.fetchall()
            cur.execute("SELECT value FROM settings WHERE key='ai_enabled'")
            ai_status = cur.fetchone()[0]
    return render_template('index.html', accounts=accounts, keywords=keywords, logs=recent_logs, ai_status=ai_status)

# مسارات إدارة الكلمات المفتاحية
@app.route('/keyword/add', methods=['POST'])
@login_required
def add_keyword():
    kw = request.form.get('keyword', '').strip()
    if kw:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT DO NOTHING", (kw,))
            conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/keyword/delete/<kw>')
@login_required
def delete_keyword(kw):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM keywords WHERE keyword = %s", (kw,))
        conn.commit()
    return redirect(url_for('dashboard'))

# مسارات إدارة الحسابات
@app.route('/account/add_step1', methods=['POST'])
@login_required
def add_account_step1():
    data = request.json
    res = asyncio.run_coroutine_threadsafe(
        radar_engine.init_login(data['phone'], data['api_id'], data['api_hash']), 
        radar_engine.loop
    ).result()
    return jsonify(res)

@app.route('/account/add_step2', methods=['POST'])
@login_required
def add_account_step2():
    data = request.json
    res = asyncio.run_coroutine_threadsafe(
        radar_engine.complete_login(data['phone'], data['code'], data.get('password')), 
        radar_engine.loop
    ).result()
    return jsonify(res)

@app.route('/account/delete/<phone>')
@login_required
def delete_account(phone):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE phone = %s", (phone,))
        conn.commit()
    
    if phone in radar_engine.clients:
        asyncio.run_coroutine_threadsafe(radar_engine.clients[phone].disconnect(), radar_engine.loop)
        del radar_engine.clients[phone]
        
    flash(f"تم حذف الحساب {phone}")
    return redirect(url_for('dashboard'))

@app.route('/account/update_group', methods=['POST'])
@login_required
def update_group():
    phone = request.form.get('phone')
    group_id = request.form.get('group_id')
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE accounts SET alert_group = %s WHERE phone = %s", (group_id, phone))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- 6. تشغيل النظام النهائي ---
if __name__ == '__main__':
    # تشغيل الرادار عند بدء السيرفر
    asyncio.run_coroutine_threadsafe(radar_engine.start_all_clients(), radar_engine.loop)
    
    # تشغيل تطبيق الويب
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
