# ==========================================
# نظام رادار تليجرام الذكي v3.0 (Cloud Edition)
# الجزء 1: الإعدادات، قاعدة البيانات، والذكاء الاصطناعي
# ==========================================

import asyncio
import os
import re
import json
import logging
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime
from threading import Thread

# مكتبات الويب والواجهة
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, render_template_string
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# مكتبات تليجرام والشبكة
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
import aiohttp

# --- 1. إعداد السجلات (Logging) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("RadarSystem")

# --- 2. إعدادات بيئة العمل (Environment Variables) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-key-123")
DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

# تهيئة تطبيق Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# إعداد نظام تسجيل الدخول
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class AdminUser(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return AdminUser(user_id) if user_id == "1" else None

# --- 3. إدارة قاعدة البيانات (Database Manager - Postgres Version) ---
class DBManager:
    def __init__(self):
        self.db_url = DATABASE_URL
        self._init_db()

    def get_connection(self):
        # تصحيح بروتوكول الرابط ليتوافق مع psycopg2
        url = self.db_url
        if url and url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        
        # الاتصال بـ Postgres
        return psycopg2.connect(url, cursor_factory=DictCursor)

    def _init_db(self):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # جدول الحسابات
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS accounts (
                        phone TEXT PRIMARY KEY,
                        api_id INTEGER NOT NULL,
                        api_hash TEXT NOT NULL,
                        alert_group TEXT,
                        session_str TEXT,
                        enabled INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # جدول الكلمات المفتاحية
                cur.execute('CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY)')
                # جدول الإعدادات
                cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
                
                # إعداد بيانات المدير الافتراضية
                cur.execute("SELECT value FROM settings WHERE key='admin_email'")
                if not cur.fetchone():
                    hashed_pass = generate_password_hash(DEFAULT_ADMIN_PASS)
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", ('admin_email', DEFAULT_ADMIN_EMAIL))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", ('admin_password', hashed_pass))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", ('ai_enabled', '0'))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", ('openrouter_api_key', ''))
            conn.commit()

    def get_setting(self, key, default=""):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
                    res = cur.fetchone()
                    return res[0] if res else default
        except Exception as e:
            logger.error(f"Error getting setting {key}: {e}")
            return default

    def set_setting(self, key, value):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (key, str(value)))
            conn.commit()

db = DBManager()

# --- 4. محرك الذكاء الاصطناعي (AI Classifier) ---
class AIClassifier:
    def __init__(self):
        self.url = "https://openrouter.ai/api/v1/chat/completions"

    async def classify_message(self, message_text):
        api_key = db.get_setting("openrouter_api_key")
        is_enabled = db.get_setting("ai_enabled") == "1"

        if not is_enabled or not api_key:
            return {"type": "seeker", "confidence": 100, "reason": "التحليل الذكي معطل."}

        prompt = f"""
        أنت مساعد ذكي متخصص في تحليل رسائل تليجرام وتصنيف المرسلين.
        المهمة: تحديد ما إذا كان المرسل طالباً يطلب مساعدة (seeker) أم معلناً يروج لخدمات (marketer).
        أجب بصيغة JSON فقط: {{"type": "seeker/marketer", "confidence": 0-100, "reason": "..."}}
        الرسالة: {message_text}
        """

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "qwen/qwen-2.5-72b-instruct",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, headers=headers, json=payload, timeout=15) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        content = result['choices'][0]['message']['content']
                        json_match = re.search(r'\{.*\}', content, re.DOTALL)
                        return json.loads(json_match.group()) if json_match else {"type": "seeker", "confidence": 50}
                    return {"type": "seeker", "confidence": 50, "reason": "API Error"}
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return {"type": "seeker", "confidence": 50, "reason": str(e)}

ai_classifier = AIClassifier()
# ==========================================
# نظام رادار تليجرام الذكي v3.0 (Cloud Edition)
# الجزء 2: محرك تليجرام ومنطق الفلترة (Postgres Version)
# ==========================================

class TelegramEngine:
    def __init__(self, db_manager):
        self.db = db_manager
        self.clients = {}          # الحسابات النشطة (phone -> client)
        self.pending_logins = {}   # الجلسات المؤقتة أثناء تسجيل الدخول
        
        # إنشاء حلقة أحداث (Event Loop) منفصلة لتليجرام
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._start_loop, daemon=True)
        self.thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coroutine(self, coro):
        """تشغيل المهام غير المتزامنة من واجهة Flask"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # --- نظام تسجيل الدخول ---
    async def request_code(self, phone, api_id, api_hash):
        try:
            client = TelegramClient(StringSession(""), int(api_id), api_hash,
                                    device_model="Radar Cloud V3")
            await client.connect()
            
            send_code_result = await client.send_code_request(phone)
            self.pending_logins[phone] = {
                'client': client,
                'phone_code_hash': send_code_result.phone_code_hash,
                'api_id': api_id,
                'api_hash': api_hash
            }
            return {"success": True}
        except Exception as e:
            logger.error(f"Request code error: {e}")
            return {"success": False, "message": str(e)}

    async def submit_code(self, phone, code):
        if phone not in self.pending_logins:
            return {"success": False, "message": "الجلسة منتهية، اطلب الكود مجدداً."}

        pending = self.pending_logins[phone]
        client = pending['client']
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=pending['phone_code_hash'])
            return await self._finalize_login(phone, client, pending['api_id'], pending['api_hash'])
        except errors.SessionPasswordNeededError:
            return {"success": True, "needs_password": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def _finalize_login(self, phone, client, api_id, api_hash):
        session_str = client.session.save()
        # حفظ في Postgres باستخدام ON CONFLICT
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO accounts (phone, api_id, api_hash, session_str, enabled)
                    VALUES (%s, %s, %s, %s, 1)
                    ON CONFLICT (phone) DO UPDATE SET 
                        session_str = EXCLUDED.session_str,
                        api_id = EXCLUDED.api_id,
                        api_hash = EXCLUDED.api_hash
                ''', (phone, int(api_id), api_hash, session_str))
            conn.commit()
        
        self.clients[phone] = client
        del self.pending_logins[phone]
        return {"success": True}

    # --- تشغيل الحسابات عند البدء ---
    async def boot_all_accounts(self, message_handler):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM accounts WHERE enabled=1")
                accounts = cur.fetchall()

        for acc in accounts:
            phone = acc['phone']
            try:
                client = TelegramClient(StringSession(acc['session_str']), acc['api_id'], acc['api_hash'])
                await client.connect()
                if await client.is_user_authorized():
                    client.add_event_handler(message_handler, events.NewMessage(incoming=True))
                    self.clients[phone] = client
                    logger.info(f"✅ الحساب نشط: {phone}")
            except Exception as e:
                logger.error(f"❌ فشل تشغيل {phone}: {e}")

# ==========================================
# الجزء 3: منطق الرادار والفلترة (Radar Logic)
# ==========================================

class RadarLogic:
    def __init__(self, db_manager, engine, classifier):
        self.db = db_manager
        self.engine = engine
        self.ai = classifier
        self.is_running = False

    async def handle_new_message(self, event):
        # فلاتر أولية
        if not event.is_group or not event.message.text: return
        
        # منع التكرار
        sender = await event.get_sender()
        if not sender or sender.is_self: return

        text = event.message.text.lower()
        
        # جلب الكلمات من Postgres
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword FROM keywords")
                keywords = [row[0].lower() for row in cur.fetchall()]

        # فحص الكلمات
        found_word = next((w for w in keywords if w in text), None)
        if not found_word: return

        # التصنيف الذكي
        ai_res = await self.ai.classify_message(text)
        if ai_res.get('type') == 'marketer' and ai_res.get('confidence', 0) > 70:
            return # تجاهل الإعلانات

        # صياغة التنبيه
        msg_link = f"https://t.me/c/{event.chat_id}/{event.id}"
        if event.chat.username: msg_link = f"https://t.me/{event.chat.username}/{event.id}"
        
        footer = (
            f"🚨 **رادار ذكي - طلب جديد**\n"
            f"━━━━━━━━━━━━\n"
            f"🔍 الكلمة: #{found_word.replace(' ', '_')}\n"
            f"👤 المرسل: {getattr(sender, 'first_name', 'مستخدم')}\n"
            f"🏢 المجموعة: {event.chat.title}\n"
            f"🔗 [رابط الرسالة]({msg_link})\n"
            f"━━━━━━━━━━━━"
        )

        # جلب مجموعة التنبيهات الخاصة بالحساب
        current_phone = next((p for p, c in self.engine.clients.items() if c == event.client), None)
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT alert_group FROM accounts WHERE phone=%s", (current_phone,))
                res = cur.fetchone()
                target = res[0] if res else None

        if target:
            try:
                # محاولة إعادة التوجيه أو النسخ
                await event.client.send_message(target, footer, link_preview=False)
                await event.client.forward_messages(target, event.message)
            except Exception as e:
                logger.error(f"Forward error: {e}")

    async def start_radar(self):
        if not self.is_running:
            await self.engine.boot_all_accounts(self.handle_new_message)
            self.is_running = True

    async def stop_radar(self):
        for client in self.engine.clients.values():
            await client.disconnect()
        self.engine.clients.clear()
        self.is_running = False

# تهيئة الكائنات
telegram_engine = TelegramEngine(db)
radar_logic = RadarLogic(db, telegram_engine, ai_classifier)
# ==========================================
# نظام رادار تليجرام الذكي v3.0 (Cloud Edition)
# الجزء 3: واجهة المستخدم والتحكم (Web UI & Routes)
# ==========================================

# --- مسارات تسجيل الدخول (Authentication) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        admin_email = db.get_setting("admin_email")
        admin_pass = db.get_setting("admin_password")
        
        if email == admin_email and check_password_hash(admin_pass, password):
            user = AdminUser("1")
            login_user(user)
            return redirect(url_for('dashboard'))
        flash("بيانات الدخول غير صحيحة!", "danger")
    
    return render_template_string('''
    <!DOCTYPE html>
    <html dir="rtl">
    <head>
        <title>دخول - رادار تليجرام</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700&display=swap" rel="stylesheet">
        <style>body { font-family: 'Cairo', sans-serif; }</style>
    </head>
    <body class="bg-gray-900 text-white flex items-center justify-center h-screen">
        <div class="bg-gray-800 p-8 rounded-lg shadow-2xl w-96 border border-gray-700">
            <h2 class="text-2xl font-bold mb-6 text-center text-green-400">🚀 نظام الرادار v3.0</h2>
            <form method="POST">
                <input type="email" name="email" placeholder="البريد الإلكتروني" class="w-full p-3 mb-4 rounded bg-gray-700 border border-gray-600 focus:border-green-500 outline-none">
                <input type="password" name="password" placeholder="كلمة المرور" class="w-full p-3 mb-6 rounded bg-gray-700 border border-gray-600 focus:border-green-500 outline-none">
                <button type="submit" class="w-full bg-green-600 hover:bg-green-700 p-3 rounded font-bold transition duration-300">دخول النظام</button>
            </form>
        </div>
    </body>
    </html>
    ''')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- لوحة التحكم الرئيسية (Dashboard) ---
@app.route('/')
@login_required
def dashboard():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accounts")
            accounts = cur.fetchall()
            cur.execute("SELECT keyword FROM keywords")
            keywords = "\n".join([row[0] for row in cur.fetchall()])
    
    ai_enabled = db.get_setting("ai_enabled") == "1"
    ai_key = db.get_setting("openrouter_api_key")
    radar_status = "يعمل حالياً" if radar_logic.is_running else "متوقف"
    status_color = "text-green-400" if radar_logic.is_running else "text-red-400"

    return render_template_string('''
    <!DOCTYPE html>
    <html dir="rtl">
    <head>
        <title>لوحة التحكم | رادار ذكي</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700&display=swap" rel="stylesheet">
        <style>body { font-family: 'Cairo', sans-serif; }</style>
    </head>
    <body class="bg-gray-900 text-gray-100 min-h-screen">
        <nav class="bg-gray-800 p-4 shadow-lg border-b border-gray-700 flex justify-between items-center px-10">
            <h1 class="text-xl font-bold text-green-400">🚀 رادار تليجرام الذكي <span class="text-xs text-gray-500 font-normal">v3.0 Cloud</span></h1>
            <a href="/logout" class="bg-red-600 hover:bg-red-700 px-4 py-1 rounded text-sm transition">خروج</a>
        </nav>

        <div class="container mx-auto mt-8 px-4 grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div class="space-y-6">
                <div class="bg-gray-800 p-6 rounded-lg border-r-4 border-blue-500 shadow-lg">
                    <h3 class="font-bold mb-4 flex items-center gap-2">📡 حالة المحرك</h3>
                    <p class="mb-4 text-sm">الحالة: <span class="{{ status_color }} font-bold">{{ radar_status }}</span></p>
                    <form action="/toggle_radar" method="POST">
                        <button class="w-full py-2 rounded font-bold transition {{ 'bg-red-600 hover:bg-red-700' if radar_logic.is_running else 'bg-green-600 hover:bg-green-700' }}">
                            {{ 'إيقاف الرادار' if radar_logic.is_running else 'تشغيل الرادار' }}
                        </button>
                    </form>
                </div>

                <div class="bg-gray-800 p-6 rounded-lg border-r-4 border-purple-500 shadow-lg">
                    <h3 class="font-bold mb-4">🧠 إعدادات AI</h3>
                    <form action="/save_ai" method="POST" class="space-y-4">
                        <input type="password" name="api_key" value="{{ ai_key }}" placeholder="OpenRouter API Key" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm">
                        <div class="flex items-center gap-2">
                            <input type="checkbox" name="enabled" {{ 'checked' if ai_enabled }}>
                            <label class="text-sm">تفعيل التصنيف الذكي</label>
                        </div>
                        <button class="w-full bg-purple-600 py-2 rounded font-bold text-sm">حفظ</button>
                    </form>
                </div>
            </div>

            <div class="lg:col-span-2 space-y-6">
                <div class="bg-gray-800 p-6 rounded-lg border-r-4 border-green-500 shadow-lg">
                    <h3 class="font-bold mb-4">📝 الكلمات المفتاحية (كل كلمة في سطر)</h3>
                    <form action="/save_keywords" method="POST">
                        <textarea name="keywords" rows="4" class="w-full p-3 bg-gray-700 rounded border border-gray-600 outline-none text-sm">{{ keywords }}</textarea>
                        <button class="mt-2 bg-green-600 px-6 py-2 rounded font-bold text-sm">تحديث القائمة</button>
                    </form>
                </div>

                <div class="bg-gray-800 p-6 rounded-lg border-r-4 border-yellow-500 shadow-lg">
                    <h3 class="font-bold mb-4">👥 الحسابات المتصلة</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-right text-sm">
                            <thead>
                                <tr class="text-gray-400 border-b border-gray-700">
                                    <th class="pb-2">رقم الهاتف</th>
                                    <th class="pb-2 text-center">الإجراء</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for acc in accounts %}
                                <tr class="border-b border-gray-700">
                                    <td class="py-3">{{ acc.phone }}</td>
                                    <td class="py-3 text-center">
                                        <a href="/delete_account/{{ acc.phone }}" class="text-red-500 hover:underline">حذف</a>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    <hr class="my-6 border-gray-700">
                    <h4 class="font-bold mb-4 text-yellow-500 text-sm">إضافة حساب جديد</h4>
                    <form action="/add_account" method="POST" class="grid grid-cols-1 md:grid-cols-3 gap-3">
                        <input type="text" name="phone" placeholder="رقم الهاتف مع مفتاح الدولة" class="p-2 bg-gray-700 rounded border border-gray-600 text-sm" required>
                        <input type="text" name="api_id" placeholder="API ID" class="p-2 bg-gray-700 rounded border border-gray-600 text-sm" required>
                        <input type="text" name="api_hash" placeholder="API Hash" class="p-2 bg-gray-700 rounded border border-gray-600 text-sm" required>
                        <input type="text" name="alert_group" placeholder="رابط مجموعة التنبيهات" class="md:col-span-2 p-2 bg-gray-700 rounded border border-gray-600 text-sm" required>
                        <button class="bg-yellow-600 hover:bg-yellow-700 py-2 rounded font-bold text-sm">إرسال كود التحقق</button>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    ''', **locals())

# --- معالجة العمليات (Actions) ---

@app.route('/toggle_radar', methods=['POST'])
@login_required
def toggle_radar():
    if radar_logic.is_running:
        telegram_engine.run_coroutine(radar_logic.stop_radar())
    else:
        telegram_engine.run_coroutine(radar_logic.start_radar())
    return redirect(url_for('dashboard'))

@app.route('/save_ai', methods=['POST'])
@login_required
def save_ai():
    db.set_setting("openrouter_api_key", request.form.get('api_key'))
    db.set_setting("ai_enabled", "1" if request.form.get('enabled') else "0")
    flash("تم حفظ إعدادات الذكاء الاصطناعي", "success")
    return redirect(url_for('dashboard'))

@app.route('/save_keywords', methods=['POST'])
@login_required
def save_keywords():
    raw_k = request.form.get('keywords', '')
    k_list = [k.strip() for k in raw_k.split('\n') if k.strip()]
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM keywords")
            for k in k_list:
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT DO NOTHING", (k,))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    phone = request.form.get('phone')
    api_id = request.form.get('api_id')
    api_hash = request.form.get('api_hash')
    alert_group = request.form.get('alert_group')
    
    # طلب الكود
    res = telegram_engine.run_coroutine(telegram_engine.request_code(phone, api_id, api_hash))
    
    if res['success']:
        # عرض صفحة إدخال الكود (مبسطة)
        return render_template_string('''
            <body style="background:#111; color:white; text-align:center; padding-top:100px; font-family:sans-serif;">
                <h2>أدخل الكود المرسل لـ {{ phone }}</h2>
                <form action="/verify_code" method="POST">
                    <input type="hidden" name="phone" value="{{ phone }}">
                    <input type="text" name="code" placeholder="12345" style="padding:10px; border-radius:5px; border:none;">
                    <button type="submit" style="padding:10px 20px; background:green; color:white; border:none; border-radius:5px; cursor:pointer;">تأكيد</button>
                </form>
            </body>
        ''', phone=phone)
    return f"Error: {res.get('message')}"

@app.route('/verify_code', methods=['POST'])
@login_required
def verify_code():
    phone = request.form.get('phone')
    code = request.form.get('code')
    res = telegram_engine.run_coroutine(telegram_engine.submit_code(phone, code))
    
    if res.get('success'):
        flash("تم إضافة الحساب بنجاح!", "success")
        return redirect(url_for('dashboard'))
    return f"خطأ في التحقق: {res.get('message')}"

@app.route('/delete_account/<phone>')
@login_required
def delete_account(phone):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE phone=%s", (phone,))
        conn.commit()
    telegram_engine.run_coroutine(telegram_engine.stop_account(phone))
    return redirect(url_for('dashboard'))

# --- تشغيل التطبيق ---
if __name__ == '__main__':
    # ملاحظة لـ Railway: نستخدم المنفذ من متغير البيئة PORT
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
    
