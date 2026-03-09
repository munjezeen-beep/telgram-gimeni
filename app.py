import os
import logging
import asyncio
import threading
import sqlite3
import json
import aiohttp
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from telethon import TelegramClient, events, errors
from telethon.tl.types import PeerChannel, PeerChat

# ==========================================
# 1. الإعدادات الأساسية والتهيئة
# ==========================================
base_dir = os.path.abspath(os.path.dirname(__file__))
sessions_dir = os.path.join(base_dir, 'sessions')
os.makedirs(sessions_dir, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(base_dir, 'templates'))
app.secret_key = os.environ.get("SECRET_KEY", "radar-super-secret-key-2024")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(base_dir, "radar.log"), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("RadarAI")

# مخزن الجلسات المؤقتة لتسجيل الدخول (الخطوة 1 والخطوة 2)
pending_logins = {}

# ==========================================
# 2. إدارة قاعدة البيانات (SQLite)
# ==========================================
DB_PATH = os.path.join(base_dir, 'radar.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # جدول الإعدادات
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    # جدول الكلمات المفتاحية
    cur.execute("CREATE TABLE IF NOT EXISTS keywords (id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT UNIQUE NOT NULL)")
    # جدول الحسابات
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, api_id INTEGER NOT NULL, api_hash TEXT NOT NULL,
        alert_group TEXT, enabled BOOLEAN DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # جدول السجلات (Logs)
    cur.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    # الإعدادات الافتراضية للأدمن والذكاء الاصطناعي
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
    
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_email', ?)", (admin_email,))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', ?)", (generate_password_hash(admin_pass),))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_enabled', '0')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('openrouter_api_key', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('radar_status', '1')") # 1 = يعمل, 0 = متوقف
    
    # كلمات مفتاحية افتراضية إذا كان الجدول فارغاً
    cur.execute("SELECT COUNT(*) FROM keywords")
    if cur.fetchone()[0] == 0:
        default_keywords = ['مساعدة', 'واجب', 'تكليف', 'مشروع', 'بحث', 'أبي أحد', 'تعرفون حد']
        for kw in default_keywords:
            cur.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (kw,))
            
    conn.commit()
    conn.close()
    logger.info("✅ تم تهيئة قاعدة البيانات بنجاح.")

def log_event(content):
    conn = get_db()
    conn.execute("INSERT INTO logs (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()
    logger.info(content)

# ==========================================
# 3. محرك الذكاء الاصطناعي (OpenRouter)
# ==========================================
PROMPT_TEMPLATE = """
أنت مساعد ذكي متخصص في تحليل رسائل تليجرام وتصنيف المرسلين بدقة عالية. المهمة: تحديد ما إذا كان المرسل **طالباً يطلب مساعدة** (seeker) أم **معلناً يروج لخدمات** (marketer).

### **معايير التصنيف الدقيقة**
#### **أولاً: فئة الطالب (seeker)**
- السمات: يطلب مساعدة في مجاله الدراسي أو الأكاديمي. استفسارات، بحث عن مدرسين.

#### **ثانياً: فئة المعلن (marketer)**
- السمات: يقدم خدمات تجارية، يحتوي على روابط واتساب، قوائم طويلة بالخدمات، رموز تزيينية كثيرة (⭐, ✅)، عبارات "للتواصل خاص".

### **المخرجات المطلوبة**
يجب أن تكون النتيجة بصيغة JSON فقط ولا تحتوي على أي نص آخر.
مثال: {{"type": "seeker", "confidence": 95, "reason": "يطلب مساعدة"}} أو {{"type": "marketer", "confidence": 98, "reason": "يقدم خدمات مع رابط"}}

الرسالة المراد تحليلها:
{message}
"""

async def classify_message(text, api_key):
    if not api_key:
        return {"type": "seeker", "confidence": 100, "reason": "AI Disabled"}
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "qwen/qwen-2.5-72b-instruct", # نموذج قوي ومجاني/رخيص ويدعم العربية
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE.format(message=text)}]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data['choices'][0]['message']['content'].strip()
                    # استخراج JSON من النص (في حال أضاف النموذج أي نص زائد)
                    start = content.find('{')
                    end = content.rfind('}') + 1
                    if start != -1 and end != 0:
                        return json.loads(content[start:end])
                return {"type": "seeker", "confidence": 0, "reason": f"API Error: {resp.status}"}
    except Exception as e:
        logger.error(f"AI Classification Error: {e}")
        return {"type": "seeker", "confidence": 0, "reason": "Exception occurred"}

# ==========================================
# 4. محرك تليجرام (إدارة الحسابات والرصد)
# ==========================================
class TelegramEngine:
    def __init__(self):
        self.clients = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        self.thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def get_settings(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        return {row['key']: row['value'] for row in cur.fetchall()}

    def get_keywords(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT keyword FROM keywords")
        return [row['keyword'].lower() for row in cur.fetchall()]

    async def _run_client(self, account):
        phone = account['phone']
        session_path = os.path.join(sessions_dir, f"{phone}.session")
        client = TelegramClient(session_path, account['api_id'], account['api_hash'])
        
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log_event(f"⚠️ الحساب {phone} غير مصرح به. يحتاج تسجيل دخول.")
                return

            self.clients[phone] = client
            log_event(f"🚀 الحساب {phone} متصل وبدأ الرصد.")

            @client.on(events.NewMessage)
            async def message_handler(event):
                # التحقق من حالة الرادار
                settings = self.get_settings()
                if settings.get('radar_status') != '1':
                    return

                # تجاهل الرسائل الخاصة ورسائل البوتات
                if event.is_private or getattr(event.message, 'out', False):
                    return

                msg_text = (event.message.message or "").lower()
                keywords = self.get_keywords()
                
                # البحث عن الكلمات المفتاحية
                found_word = next((word for word in keywords if word in msg_text), None)
                
                if found_word:
                    log_event(f"🔍 رصد كلمة '{found_word}' في مجموعة عبر الحساب {phone}")
                    
                    ai_enabled = settings.get('ai_enabled') == '1'
                    api_key = settings.get('openrouter_api_key')
                    
                    is_seeker = True # الافتراضي إرسال
                    
                    if ai_enabled and api_key:
                        result = await classify_message(event.message.message, api_key)
                        log_event(f"🧠 تصنيف الذكاء الاصطناعي: {result.get('type')} (الثقة: {result.get('confidence')}%)")
                        if result.get('type') == 'marketer' and result.get('confidence', 0) > 60:
                            is_seeker = False
                            log_event("🚫 تم تجاهل الرسالة لأنها إعلان (Marketer).")

                    if is_seeker:
                        await self._forward_alert(client, event, account['alert_group'])

            await client.run_until_disconnected()
        except Exception as e:
            log_event(f"❌ توقف الحساب {phone}: {str(e)}")
        finally:
            if phone in self.clients:
                del self.clients[phone]

    async def _forward_alert(self, client, event, target_group):
        try:
            sender = await event.get_sender()
            chat = await event.get_chat()
            
            sender_name = getattr(sender, 'first_name', '') + ' ' + getattr(sender, 'last_name', '')
            sender_name = sender_name.strip() or 'غير معروف'
            sender_user = f"@{sender.username}" if getattr(sender, 'username', None) else "لا يوجد يوزر"
            
            chat_title = getattr(chat, 'title', 'مجموعة غير معروفة')
            
            # محاولة جلب رابط الرسالة
            msg_link = f"https://t.me/c/{chat.id}/{event.id}" if getattr(chat, 'id', None) else "لا يوجد رابط"

            footer = f"\n\n🚨 **رادار ذكي - طلب مساعدة**\n━━━━━━━━━━━━━━━━━━━\n👤 **المرسل**: {sender_name} - {sender_user}\n🏢 **المجموعة**: {chat_title}\n🔗 **الرابط**: {msg_link}\n━━━━━━━━━━━━━━━━━━━"
            
            if not target_group:
                log_event("⚠️ لم يتم تحديد مجموعة تنبيهات لهذا الحساب.")
                return

            try:
                # محاولة إعادة التوجيه أولاً (Forward)
                await client.forward_messages(int(target_group) if target_group.lstrip('-').isdigit() else target_group, event.message)
                await client.send_message(int(target_group) if target_group.lstrip('-').isdigit() else target_group, footer)
            except errors.ChatForwardsRestrictedError:
                # إذا كانت المجموعة تمنع التحويل، ننسخ النص
                full_text = f"{event.message.message}\n\n*(تم إرسال نسخة بسبب منع التحويل)*{footer}"
                if event.message.media:
                    await client.send_file(int(target_group) if target_group.lstrip('-').isdigit() else target_group, event.message.media, caption=full_text)
                else:
                    await client.send_message(int(target_group) if target_group.lstrip('-').isdigit() else target_group, full_text)
                    
            log_event("✅ تم إرسال التنبيه للمجموعة بنجاح.")
        except Exception as e:
            log_event(f"❌ فشل إرسال التنبيه: {e}")

    def start_all(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM accounts WHERE enabled = 1")
        accounts = cur.fetchall()
        for acc in accounts:
            asyncio.run_coroutine_threadsafe(self._run_client(dict(acc)), self.loop)

    def stop_all(self):
        for phone, client in list(self.clients.items()):
            asyncio.run_coroutine_threadsafe(client.disconnect(), self.loop)

radar_engine = TelegramEngine()

# ==========================================
# 5. واجهة المستخدم (Flask Routes)
# ==========================================
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id): self.id = id
@login_manager.user_loader
def load_user(user_id): return User(user_id)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key='admin_email'")
        admin_e = cur.fetchone()[0]
        cur.execute("SELECT value FROM settings WHERE key='admin_password'")
        admin_p = cur.fetchone()[0]
        if email == admin_e and check_password_hash(admin_p, password):
            login_user(User(email))
            return redirect(url_for('index'))
        flash("بيانات الدخول خاطئة", "danger")
    return render_template('login.html')

@app.route('/')
@login_required
def index():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT keyword FROM keywords")
    kws = [r['keyword'] for r in cur.fetchall()]
    cur.execute("SELECT * FROM accounts")
    accs = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 100")
    logs = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT key, value FROM settings")
    settings = {r['key']: r['value'] for r in cur.fetchall()}
    
    return render_template('index.html', keywords='\n'.join(kws), accounts=accs, logs=logs, settings=settings, active_clients=list(radar_engine.clients.keys()))

# --- مسارات الذكاء الاصطناعي والإعدادات ---
@app.route('/api/settings/save', methods=['POST'])
@login_required
def save_settings():
    api_key = request.form.get('openrouter_api_key', '')
    ai_enabled = '1' if request.form.get('ai_enabled') else '0'
    radar_status = request.form.get('radar_status', '1')
    
    conn = get_db()
    conn.execute("UPDATE settings SET value=? WHERE key='openrouter_api_key'", (api_key,))
    conn.execute("UPDATE settings SET value=? WHERE key='ai_enabled'", (ai_enabled,))
    
    # تحديث حالة الرادار (تشغيل/إيقاف)
    old_status = conn.execute("SELECT value FROM settings WHERE key='radar_status'").fetchone()[0]
    conn.execute("UPDATE settings SET value=? WHERE key='radar_status'", (radar_status,))
    conn.commit()
    
    if radar_status == '0' and old_status == '1':
        radar_engine.stop_all()
        log_event("🛑 تم إيقاف الرادار يدوياً.")
    elif radar_status == '1' and old_status == '0':
        radar_engine.start_all()
        log_event("▶️ تم تشغيل الرادار.")
        
    flash("تم حفظ الإعدادات بنجاح", "success")
    return redirect(url_for('index'))

@app.route('/api/keywords/save', methods=['POST'])
@login_required
def save_keywords():
    keywords_text = request.form.get('keywords', '')
    words = [w.strip() for w in keywords_text.split('\n') if w.strip()]
    
    conn = get_db()
    conn.execute("DELETE FROM keywords") # مسح القديم
    for w in set(words): # إزالة المكرر
        conn.execute("INSERT INTO keywords (keyword) VALUES (?)", (w,))
    conn.commit()
    log_event(f"تم تحديث الكلمات المفتاحية (الإجمالي: {len(words)})")
    flash("تم حفظ الكلمات المفتاحية", "success")
    return redirect(url_for('index'))

# --- مسارات إضافة وتوثيق حسابات تليجرام ---
@app.route('/api/account/step1', methods=['POST'])
@login_required
def account_step1():
    data = request.json
    phone, api_id, api_hash = data.get('phone'), data.get('api_id'), data.get('api_hash')
    
    # دالة غير متزامنة لطلب الكود
    async def req_code():
        client = TelegramClient(os.path.join(sessions_dir, f"{phone}.session"), api_id, api_hash)
        await client.connect()
        try:
            sent_code = await client.send_code_request(phone)
            pending_logins[phone] = {'client': client, 'phone_code_hash': sent_code.phone_code_hash, 'api_id': api_id, 'api_hash': api_hash, 'alert_group': data.get('alert_group', '')}
            return {'status': 'success'}
        except Exception as e:
            await client.disconnect()
            return {'status': 'error', 'msg': str(e)}

    # تنفيذ الدالة في حلقة أحداث مؤقتة خاصة بالطلب
    future = asyncio.run_coroutine_threadsafe(req_code(), radar_engine.loop)
    return jsonify(future.result(timeout=15))

@app.route('/api/account/step2', methods=['POST'])
@login_required
def account_step2():
    data = request.json
    phone, code, password = data.get('phone'), data.get('code'), data.get('password')
    
    if phone not in pending_logins:
        return jsonify({'status': 'error', 'msg': 'انتهت الجلسة، حاول مجدداً'})
        
    login_data = pending_logins[phone]
    client = login_data['client']

    async def verify_code():
        try:
            await client.sign_in(phone, code, phone_code_hash=login_data['phone_code_hash'])
            return 'success'
        except errors.SessionPasswordNeededError:
            if not password: return 'need_password'
            try:
                await client.sign_in(password=password)
                return 'success'
            except Exception as e:
                return f'كلمة سر خاطئة: {str(e)}'
        except Exception as e:
            return f'خطأ: {str(e)}'

    future = asyncio.run_coroutine_threadsafe(verify_code(), radar_engine.loop)
    res = future.result(timeout=15)

    if res == 'success':
        # حفظ في قاعدة البيانات
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO accounts (phone, api_id, api_hash, alert_group, enabled) VALUES (?, ?, ?, ?, 1)",
                     (phone, login_data['api_id'], login_data['api_hash'], login_data['alert_group']))
        conn.commit()
        log_event(f"✅ تم إضافة الحساب بنجاح: {phone}")
        
        # نقل العميل إلى محرك الرادار ليعمل في الخلفية
        radar_engine.clients[phone] = client
        asyncio.run_coroutine_threadsafe(radar_engine._run_client({'phone': phone, 'api_id': login_data['api_id'], 'api_hash': login_data['api_hash'], 'alert_group': login_data['alert_group']}), radar_engine.loop)
        
        del pending_logins[phone]
        return jsonify({'status': 'success'})
    elif res == 'need_password':
        return jsonify({'status': 'need_password'})
    else:
        return jsonify({'status': 'error', 'msg': res})

@app.route('/account/delete/<phone>')
@login_required
def delete_account(phone):
    conn = get_db()
    conn.execute("DELETE FROM accounts WHERE phone=?", (phone,))
    conn.commit()
    
    # فصل العميل إذا كان يعمل
    if phone in radar_engine.clients:
        asyncio.run_coroutine_threadsafe(radar_engine.clients[phone].disconnect(), radar_engine.loop)
        del radar_engine.clients[phone]
        
    # حذف ملف الجلسة
    session_file = os.path.join(sessions_dir, f"{phone}.session")
    if os.path.exists(session_file):
        os.remove(session_file)
        
    log_event(f"🗑️ تم حذف الحساب {phone}")
    flash("تم حذف الحساب بنجاح", "success")
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# ==========================================
# 6. التشغيل (Entry Point)
# ==========================================
if __name__ == '__main__':
    init_db()
    radar_engine.start_all() # تشغيل الحسابات المحفوظة مسبقاً
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False) # use_reloader=False ضروري جداً لعدم تكرار Threads
