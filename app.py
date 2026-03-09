# ==========================================
# نظام رادار تليجرام الذكي v3.0 (Cloud Edition)
# الجزء 1: الإعدادات، قاعدة البيانات، والذكاء الاصطناعي
# ==========================================

import asyncio
import os
import re
import json
import sqlite3
import logging
from datetime import datetime
from threading import Thread

# مكتبات الويب والواجهة
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# مكتبات تليجرام والشبكة
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
import aiohttp

# --- 1. إعداد السجلات (Logging) ---
# مثالي لمنصات مثل Railway حيث تقرأ السجلات من الـ Console مباشرة
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("RadarSystem")

# --- 2. إعدادات بيئة العمل (Environment Variables) ---
DB_FILE = os.environ.get("DB_FILE", "radar.db")
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

# تهيئة تطبيق Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# إعداد نظام تسجيل الدخول
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# نموذج المستخدم الوهمي (لدينا مستخدم واحد فقط وهو المدير)
class AdminUser(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return AdminUser(user_id) if user_id == "1" else None

# --- 3. إدارة قاعدة البيانات (Database Manager) ---
class DBManager:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # جدول الحسابات (نستخدم session_str لتخزين الجلسة كنص لمنع ضياعها في Railway)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    api_id INTEGER NOT NULL,
                    api_hash TEXT NOT NULL,
                    alert_group TEXT,
                    session_str TEXT,
                    enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # جدول الكلمات المفتاحية
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT UNIQUE NOT NULL
                )
            ''')
            # جدول الإعدادات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # حقن بيانات المدير الافتراضية إذا لم تكن موجودة
            cursor.execute("SELECT value FROM settings WHERE key='admin_email'")
            if not cursor.fetchone():
                hashed_pass = generate_password_hash(DEFAULT_ADMIN_PASS)
                cursor.execute("INSERT INTO settings (key, value) VALUES ('admin_email', ?)", (DEFAULT_ADMIN_EMAIL,))
                cursor.execute("INSERT INTO settings (key, value) VALUES ('admin_password', ?)", (hashed_pass,))
                cursor.execute("INSERT INTO settings (key, value) VALUES ('ai_enabled', '0')")
                cursor.execute("INSERT INTO settings (key, value) VALUES ('openrouter_api_key', '')")
            conn.commit()

    def get_setting(self, key, default=""):
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row['value'] if row else default

    def set_setting(self, key, value):
        with self.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
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
            # إذا كان الذكاء الاصطناعي معطلاً، نعتبر الرسالة "طالب" افتراضياً لضمان عدم ضياع الفرص
            return {"type": "seeker", "confidence": 100, "reason": "التحليل الذكي معطل."}

        # الـ Prompt الدقيق والاحترافي الذي طلبته
        prompt = f"""
        أنت مساعد ذكي متخصص في تحليل رسائل تليجرام وتصنيف المرسلين بدقة عالية. المهمة: تحديد ما إذا كان المرسل **طالباً يطلب مساعدة** (seeker) أم **معلناً يروج لخدمات** (marketer).

        ### **معايير التصنيف الدقيقة**
        #### **أولاً: فئة الطالب (seeker)**
        - السمات: يطلب مساعدة في مجاله الدراسي أو الأكاديمي.
        - أمثلة: "أبي أحد يحل واجب الرياضيات ضروري"، "محتاج بحث عن الذكاء الاصطناعي"، "من يعرف مدرس خصوصي للفيزياء؟".

        #### **ثانياً: فئة المعلن (marketer)**
        - السمات: يقدم خدمات تجارية (مدفوعة)، يحتوي على روابط واتساب أو تليجرام للتواصل، قوائم طويلة بالخدمات، استخدام رموز تزيينية (⭐, ✅, 💯).
        - أمثلة: "✨📚 خدمات طلابية شاملة لدعم نجاحك الأكاديمي! 🎓✨ للتواصل: @user".

        ### **المخرجات المطلوبة**
        يجب أن تكون النتيجة بصيغة JSON فقط ولا تحتوي على أي نص آخر.
        مثال لطالب: {{"type": "seeker", "confidence": 95, "reason": "يطلب مساعدة في شرح مادة."}}
        مثال لمعلن: {{"type": "marketer", "confidence": 98, "reason": "يقدم قائمة خدمات مع روابط."}}

        الرسالة المراد تحليلها:
        {message_text}
        """

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "qwen/qwen3-vl-30b-a3b-thinking", # النموذج المفضل
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1 # تقليل العشوائية لضمان دقة الـ JSON
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, headers=headers, json=payload, timeout=30) as resp:
                    if resp.status != 200:
                        logger.warning(f"AI API Error: Status {resp.status}")
                        return {"type": "seeker", "confidence": 50, "reason": "خطأ في استجابة الخادم"}
                    
                    result = await resp.json()
                    content = result['choices'][0]['message']['content']
                    
                    # استخراج JSON من النص بأمان
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if json_match:
                        return json.loads(json_match.group())
                    else:
                        return {"type": "seeker", "confidence": 50, "reason": "فشل في تحليل المخرجات"}
        except asyncio.TimeoutError:
            logger.warning("AI API Timeout.")
            return {"type": "seeker", "confidence": 50, "reason": "نفذ الوقت (Timeout)"}
        except Exception as e:
            logger.error(f"AI Classification Exception: {e}")
            return {"type": "seeker", "confidence": 50, "reason": f"خطأ برمجي: {str(e)}"}

ai_classifier = AIClassifier()

# ==========================================
# الجزء 2: محرك تليجرام السحابي (The Telegram Engine)
# ==========================================

class TelegramEngine:
    def __init__(self, db_manager):
        self.db = db_manager
        self.clients = {}          # لتخزين الحسابات النشطة العاملة (رقم الهاتف -> الكائن)
        self.pending_logins = {}   # لتخزين الجلسات المعلقة أثناء انتظار إدخال الكود لمنع انقطاع الاتصال
        
        # إنشاء حلقة أحداث (Event Loop) خاصة بتليجرام لتعمل في الخلفية بعيداً عن Flask
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._start_loop, daemon=True)
        self.thread.start()

    def _start_loop(self):
        """تشغيل حلقة الأحداث في الخلفية لضمان عمل الرادار 24/7"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_coroutine(self, coro):
        """دالة مساعدة لتشغيل الوظائف غير المتزامنة (Async) من داخل واجهة Flask المتزامنة"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # ----------------------------------------------------------------
    # نظام المصادقة وتسجيل الدخول (Authentication Flow)
    # ----------------------------------------------------------------

    async def request_code(self, phone, api_id, api_hash):
        """
        الخطوة 1: طلب كود التحقق.
        يتم إنشاء العميل وحفظه في الذاكرة المؤقتة (pending_logins) لضمان بقاء الاتصال حياً (Keep-Alive).
        """
        try:
            # نستخدم StringSession فارغ في البداية للحسابات الجديدة
            client = TelegramClient(StringSession(""), int(api_id), api_hash,
                                    device_model="RadarBot Cloud", 
                                    system_version="1.0",
                                    request_retries=5,
                                    connection_retries=5)
            
            await client.connect()
            if not await client.is_user_authorized():
                # نطلب الكود من تليجرام
                send_code_result = await client.send_code_request(phone)
                
                # نحفظ العميل وكود الـ Hash المرتبط به كي لا يضيع عند إدخال الكود
                self.pending_logins[phone] = {
                    'client': client,
                    'phone_code_hash': send_code_result.phone_code_hash,
                    'api_id': api_id,
                    'api_hash': api_hash
                }
                return {"success": True, "message": "تم إرسال الكود بنجاح. بانتظار الإدخال."}
            else:
                return {"success": False, "message": "الحساب مسجل دخول بالفعل!"}
                
        except errors.FloodWaitError as e:
            return {"success": False, "message": f"تليجرام يطلب الانتظار لمدة {e.seconds} ثانية (Flood Wait)."}
        except Exception as e:
            logger.error(f"Error requesting code for {phone}: {e}")
            return {"success": False, "message": f"حدث خطأ غير متوقع: {str(e)}"}

    async def submit_code(self, phone, code):
        """
        الخطوة 2: إرسال كود التحقق.
        نقوم باستدعاء نفس العميل (Client) من الذاكرة لضمان تطابق الـ Hash وعدم ظهور خطأ Code Expired.
        """
        if phone not in self.pending_logins:
            return {"success": False, "message": "لم يتم العثور على جلسة معلقة لهذا الرقم. يرجى طلب الكود مجدداً."}

        pending = self.pending_logins[phone]
        client = pending['client']

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=pending['phone_code_hash'])
            return await self._finalize_login(phone, client, pending['api_id'], pending['api_hash'])

        except errors.SessionPasswordNeededError:
            # إذا كان الحساب محمياً بالتحقق بخطوتين
            return {"success": True, "needs_password": True, "message": "الحساب محمي بكلمة مرور (التحقق بخطوتين)."}
        except errors.PhoneCodeExpiredError:
            return {"success": False, "message": "الكود منتهي الصلاحية. يرجى إعادة المحاولة."}
        except errors.PhoneCodeInvalidError:
            return {"success": False, "message": "الكود الذي أدخلته غير صحيح."}
        except Exception as e:
            logger.error(f"Error submitting code for {phone}: {e}")
            return {"success": False, "message": f"خطأ: {str(e)}"}

    async def submit_password(self, phone, password):
        """الخطوة 3 (اختيارية): إدخال كلمة المرور إذا كان التحقق بخطوتين مفعلاً."""
        if phone not in self.pending_logins:
            return {"success": False, "message": "الجلسة مفقودة."}

        pending = self.pending_logins[phone]
        client = pending['client']

        try:
            await client.sign_in(password=password)
            return await self._finalize_login(phone, client, pending['api_id'], pending['api_hash'])
        except errors.PasswordHashInvalidError:
            return {"success": False, "message": "كلمة المرور غير صحيحة."}
        except Exception as e:
            return {"success": False, "message": f"خطأ: {str(e)}"}

    async def _finalize_login(self, phone, client, api_id, api_hash):
        """حفظ الجلسة الدائمة في قاعدة البيانات بعد النجاح في تسجيل الدخول"""
        session_string = client.session.save()
        
        with self.db.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO accounts (phone, api_id, api_hash, session_str, enabled)
                VALUES (?, ?, ?, ?, 1)
            ''', (phone, api_id, api_hash, session_string))
            conn.commit()

        # ننقل العميل من "المعلق" إلى "النشط"
        self.clients[phone] = client
        del self.pending_logins[phone]
        logger.info(f"تم تسجيل الدخول وحفظ حساب {phone} بنجاح.")
        return {"success": True, "message": "تم تسجيل الدخول وحفظ الحساب بنجاح!"}

    # ----------------------------------------------------------------
    # إدارة الحسابات المحفوظة والتنبيهات (Session Management & Alerts)
    # ----------------------------------------------------------------

    async def boot_all_accounts(self, message_handler):
        """
        تشغيل جميع الحسابات المحفوظة عند إعادة إقلاع السيرفر.
        يقوم بربط دالة (message_handler) التي سنبرمجها في الجزء القادم بكل حساب.
        """
        with self.db.get_connection() as conn:
            accounts = conn.execute("SELECT * FROM accounts WHERE enabled=1").fetchall()

        for acc in accounts:
            phone = acc['phone']
            try:
                client = TelegramClient(StringSession(acc['session_str']), acc['api_id'], acc['api_hash'])
                await client.connect()
                
                if await client.is_user_authorized():
                    # ربط الحساب بـ "مستمع الرسائل"
                    client.add_event_handler(message_handler, events.NewMessage(incoming=True))
                    self.clients[phone] = client
                    logger.info(f"✅ تم تشغيل الحساب وربطه بالرادار: {phone}")
                else:
                    logger.warning(f"⚠️ جلسة الحساب {phone} منتهية. يجب إعادة تسجيل الدخول.")
                    # يمكن هنا تحديث قاعدة البيانات لتعطيل الحساب تلقائياً
            except Exception as e:
                logger.error(f"❌ فشل تشغيل حساب {phone}: {e}")

    async def stop_account(self, phone):
        """إيقاف وفصل حساب معين"""
        if phone in self.clients:
            await self.clients[phone].disconnect()
            del self.clients[phone]
            logger.info(f"تم إيقاف اتصال الحساب {phone}.")

    async def forward_alert(self, source_client, message, target_group, footer_text):
        """
        إرسال التنبيه إلى المجموعة الخاصة.
        يحاول إعادة التوجيه (Forward) أولاً، وإن فشل (بسبب الحماية)، يقوم بالنسخ.
        """
        if not target_group:
            logger.warning("لا يوجد مجموعة مستهدفة لإرسال التنبيه.")
            return

        try:
            # المحاولة الأولى: إعادة التوجيه
            await source_client.forward_messages(target_group, message)
            # إرسال تذييل المعلومات (اسم المرسل، المجموعة، الخ)
            await source_client.send_message(target_group, footer_text)
            logger.info("تم إعادة توجيه التنبيه بنجاح.")
        except errors.MessageForwardRestrictError:
            # المحاولة الثانية: المجموعة محمية ضد التحويل، لذلك سنقوم بنسخ المحتوى
            logger.info("المجموعة محمية، جاري إرسال نسخة من الرسالة...")
            text_to_send = f"{message.text}\n\n*(تم إرسال نسخة بسبب منع التحويل)*\n\n{footer_text}"
            
            # التحقق مما إذا كانت الرسالة تحتوي على وسائط (صورة، ملف)
            if message.media:
                await source_client.send_message(target_group, text_to_send, file=message.media)
            else:
                await source_client.send_message(target_group, text_to_send)
        except Exception as e:
            logger.error(f"فشل إرسال التنبيه للمجموعة {target_group}: {e}")

# تهيئة المحرك وربطه بقاعدة البيانات
telegram_engine = TelegramEngine(db)


# ==========================================
# الجزء 3: منطق الرادار والفلترة (The Radar Logic)
# ==========================================

class RadarLogic:
    def __init__(self, db_manager, engine, classifier):
        self.db = db_manager
        self.engine = engine
        self.ai = classifier
        self.is_running = False

    async def handle_new_message(self, event):
        """
        الدالة المركزية لمعالجة كل رسالة جديدة تصل لأي حساب مربوط بالرادار.
        """
        # 1. الفلاتر الأولية (تجاهل الرسائل غير المناسبة)
        if not event.is_group: return # تجاهل الخاص والقنوات
        if event.is_channel: return # تجاهل القنوات
        if not event.message.text: return # تجاهل الرسائل التي لا تحتوي على نص
        
        # تجاهل الرسائل الصادرة من الحساب نفسه (لتجنب الحلقات اللانهائية)
        sender = await event.get_sender()
        if not sender or sender.is_self: return

        message_text = event.message.text.lower()
        
        # 2. جلب الكلمات المفتاحية من قاعدة البيانات
        with self.db.get_connection() as conn:
            keywords_rows = conn.execute("SELECT keyword FROM keywords").fetchall()
            keywords = [row['keyword'].lower() for row in keywords_rows]

        # 3. فحص الكلمات المفتاحية (Pattern Matching)
        found_word = None
        for word in keywords:
            # البحث عن الكلمة كنمط "تحتوي على" لضمان أقصى دقة
            if word in message_text:
                found_word = word
                break
        
        if not found_word: return # إذا لم توجد كلمة مفتاحية، توقف هنا.

        logger.info(f"🔍 رصد كلمة '{found_word}' في مجموعة '{event.chat.title}'")

        # 4. التصنيف الذكي (AI Classification)
        ai_result = await self.ai.classify_message(message_text)
        
        # إذا صنف الذكاء الاصطناعي الرسالة كـ "معلن" وبثقة عالية، نتجاهلها تماماً
        if ai_result.get('type') == 'marketer' and ai_result.get('confidence', 0) > 60:
            logger.info(f"🚫 تم تجاهل معلن (ثقة {ai_result['confidence']}%): {ai_result['reason']}")
            return

        # 5. جمع معلومات المصدر والمرسل للصياغة
        try:
            sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip() or "بدون اسم"
            sender_id = sender.id
            sender_username = f"@{sender.username}" if sender.username else f"tg://user?id={sender_id}"
            
            group_name = event.chat.title
            group_link = ""
            if event.chat.username:
                group_link = f"https://t.me/{event.chat.username}"
            
            # جلب رابط الرسالة المباشر (إن وجد)
            msg_link = f"https://t.me/c/{event.chat_id}/{event.id}" # للمجموعات الخاصة
            if event.chat.username:
                msg_link = f"https://t.me/{event.chat.username}/{event.id}"

            # 6. صياغة التذييل (Footer) الاحترافي كما طلبت
            footer_text = (
                f"🚨 **رادار ذكي - طلب مساعدة**\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🔍 **الكلمة المرصودة**: #{found_word.replace(' ', '_')}\n"
                f"👤 **المرسل**: {sender_name} ({sender_username})\n"
                f"🏢 **المجموعة**: {group_name}\n"
                f"🔗 **رابط الرسالة**: [اضغط هنا للذهاب]({msg_link})\n"
                f"🤖 **تحليل الذكاء**: {ai_result.get('reason', 'N/A')}\n"
                f"━━━━━━━━━━━━━━━━━━━"
            )

            # 7. تحديد مجموعة الإرسال (الافتراضية أو الخاصة بالحساب)
            # سنقوم بجلب الحساب الحالي من قاعدة البيانات لمعرفة مجموعته
            # ملاحظة: سنستخدم الـ phone المرتبط بالـ client
            current_phone = None
            for phone, client in self.engine.clients.items():
                if client == event.client:
                    current_phone = phone
                    break
            
            with self.db.get_connection() as conn:
                acc_info = conn.execute("SELECT alert_group FROM accounts WHERE phone=?", (current_phone,)).fetchone()
                target_group = acc_info['alert_group'] if acc_info else None

            # 8. تنفيذ عملية الإرسال/التحويل
            if target_group:
                # تحويل الرابط إلى كيان (Entity) صالح للتليجرام
                try:
                    target_entity = await event.client.get_input_entity(target_group)
                    await self.engine.forward_alert(event.client, event.message, target_entity, footer_text)
                except Exception as e:
                    logger.error(f"خطأ في الوصول لمجموعة التنبيهات {target_group}: {e}")
            else:
                logger.warning(f"الحساب {current_phone} لا يملك مجموعة تنبيهات محددة.")

        except Exception as e:
            logger.error(f"خطأ أثناء معالجة تفاصيل الرسالة: {e}")

    # ----------------------------------------------------------------
    # وظائف التحكم في تشغيل وإيقاف الرادار
    # ----------------------------------------------------------------

    async def start_radar(self):
        """بدء تشغيل الرادار لجميع الحسابات"""
        if self.is_running: return
        logger.info("جاري تشغيل محرك الرادار...")
        
        # تمرير دالة handle_new_message كـ مستمع للأحداث
        await self.engine.boot_all_accounts(self.handle_new_message)
        self.is_running = True
        logger.info("🚀 الرادار يعمل الآن ويبحث عن الطلبات...")

    async def stop_radar(self):
        """إيقاف الرادار وفصل الحسابات"""
        if not self.is_running: return
        logger.info("جاري إيقاف الرادار...")
        
        # نقوم بإغلاق جميع الـ Clients في الـ Engine
        phones = list(self.engine.clients.keys())
        for phone in phones:
            await self.engine.stop_account(phone)
            
        self.is_running = False
        logger.info("🛑 تم إيقاف الرادار بنجاح.")

# تهيئة الرادار وربطه بالمكونات السابقة
radar_logic = RadarLogic(db, telegram_engine, ai_classifier)


# ==========================================
# الجزء 4: واجهة المستخدم والتحكم (Web UI & Control)
# ==========================================

# --- مسارات تسجيل الدخول (Authentication) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
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
    
    # صفحة دخول بسيطة وأنيقة (In-line HTML لضمان عمل الكود في ملف واحد)
    return '''
    <!DOCTYPE html>
    <html dir="rtl">
    <head>
        <title>دخول - رادار تليجرام</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-900 text-white flex items-center justify-center h-screen">
        <div class="bg-gray-800 p-8 rounded-lg shadow-xl w-96">
            <h2 class="text-2xl font-bold mb-6 text-center text-green-400">🚀 رادار تليجرام الذكي</h2>
            <form method="POST">
                <input type="email" name="email" placeholder="البريد الإلكتروني" class="w-full p-2 mb-4 rounded bg-gray-700 border border-gray-600 focus:outline-none focus:border-green-500">
                <input type="password" name="password" placeholder="كلمة المرور" class="w-full p-2 mb-6 rounded bg-gray-700 border border-gray-600 focus:outline-none focus:border-green-500">
                <button type="submit" class="w-full bg-green-600 hover:bg-green-700 p-2 rounded font-bold transition">دخول النظام</button>
            </form>
        </div>
    </body>
    </html>
    '''

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
        accounts = conn.execute("SELECT * FROM accounts").fetchall()
        keywords = "\n".join([row['keyword'] for row in conn.execute("SELECT keyword FROM keywords").fetchall()])
    
    ai_enabled = db.get_setting("ai_enabled") == "1"
    ai_key = db.get_setting("openrouter_api_key")
    radar_status = "يعمل" if radar_logic.is_running else "متوقف"
    status_color = "text-green-400" if radar_logic.is_running else "text-red-400"

    return render_template_string('''
    <!DOCTYPE html>
    <html dir="rtl">
    <head>
        <title>لوحة تحكم الرادار</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;700&display=swap" rel="stylesheet">
        <style>body { font-family: 'Cairo', sans-serif; }</style>
    </head>
    <body class="bg-gray-900 text-gray-100 min-h-screen pb-10">
        <nav class="bg-gray-800 p-4 shadow-md flex justify-between items-center px-10">
            <h1 class="text-xl font-bold text-green-400">🚀 رادار تليجرام الذكي</h1>
            <div class="flex items-center gap-4">
                <span class="text-sm text-gray-400">مرحباً، مدير النظام</span>
                <a href="/logout" class="bg-red-600 hover:bg-red-700 px-3 py-1 rounded text-sm transition">خروج</a>
            </div>
        </nav>

        <div class="container mx-auto mt-8 px-4 grid grid-cols-1 lg:grid-cols-3 gap-6">
            
            <div class="lg:col-span-1 space-y-6">
                <div class="bg-gray-800 p-6 rounded-lg shadow-lg border-t-4 border-blue-500">
                    <h3 class="text-lg font-bold mb-4">حالة النظام</h3>
                    <div class="flex items-center justify-between">
                        <span class="font-bold {{ status_color }}">{{ radar_status }}</span>
                        <form action="/toggle_radar" method="POST">
                            <button class="px-6 py-2 rounded font-bold transition {{ 'bg-red-600 hover:bg-red-700' if radar_logic.is_running else 'bg-green-600 hover:bg-green-700' }}">
                                {{ 'إيقاف الرادار' if radar_logic.is_running else 'تشغيل الرادار' }}
                            </button>
                        </form>
                    </div>
                </div>

                <div class="bg-gray-800 p-6 rounded-lg shadow-lg border-t-4 border-purple-500">
                    <h3 class="text-lg font-bold mb-4">إعدادات الذكاء الاصطناعي</h3>
                    <form action="/save_ai" method="POST" class="space-y-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">مفتاح OpenRouter</label>
                            <input type="password" name="api_key" value="{{ ai_key }}" class="w-full p-2 bg-gray-700 rounded border border-gray-600 focus:border-purple-500 outline-none">
                        </div>
                        <div class="flex items-center gap-2">
                            <input type="checkbox" name="enabled" {{ 'checked' if ai_enabled }} class="w-4 h-4">
                            <label>تفعيل التصنيف الذكي</label>
                        </div>
                        <button class="w-full bg-purple-600 hover:bg-purple-700 p-2 rounded font-bold transition">حفظ الإعدادات</button>
                    </form>
                </div>
            </div>

            <div class="lg:col-span-2 space-y-6">
                <div class="bg-gray-800 p-6 rounded-lg shadow-lg border-t-4 border-green-500">
                    <h3 class="text-lg font-bold mb-4">إدارة الكلمات المفتاحية</h3>
                    <form action="/save_keywords" method="POST">
                        <textarea name="keywords" rows="5" class="w-full p-3 bg-gray-700 rounded border border-gray-600 outline-none focus:border-green-500 mb-3" placeholder="ضع كل كلمة في سطر منفصل...">{{ keywords }}</textarea>
                        <button class="bg-green-600 hover:bg-green-700 px-6 py-2 rounded font-bold transition">تحديث القائمة</button>
                    </form>
                </div>

                <div class="bg-gray-800 p-6 rounded-lg shadow-lg border-t-4 border-yellow-500">
                    <h3 class="text-lg font-bold mb-4">الحسابات المضافة</h3>
                    <table class="w-full text-right border-collapse">
                        <thead>
                            <tr class="text-gray-400 border-b border-gray-700">
                                <th class="py-2">الهاتف</th>
                                <th class="py-2">المجموعة</th>
                                <th class="py-2">الإجراءات</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for acc in accounts %}
                            <tr class="border-b border-gray-700 last:border-0">
                                <td class="py-3">{{ acc.phone }}</td>
                                <td class="py-3 text-sm text-gray-400">{{ acc.alert_group or 'عامة' }}</td>
                                <td class="py-3">
                                    <a href="/delete_account/{{ acc.phone }}" class="text-red-500 hover:text-red-400 text-sm">حذف الحساب</a>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    <hr class="my-6 border-gray-700">
                    <h4 class="font-bold mb-4 text-yellow-500">إضافة حساب جديد</h4>
                    <form action="/add_account" method="POST" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <input type="text" name="phone" placeholder="رقم الهاتف (966...)" class="p-2 bg-gray-700 rounded border border-gray-600 outline-none">
                        <input type="number" name="api_id" placeholder="API ID" class="p-2 bg-gray-700 rounded border border-gray-600 outline-none">
                        <input type="text" name="api_hash" placeholder="API Hash" class="p-2 bg-gray-700 rounded border border-gray-600 outline-none">
                        <input type="text" name="alert_group" placeholder="يوزر مجموعة الإشعارات" class="p-2 bg-gray-700 rounded border border-gray-600 outline-none">
                        <button class="md:col-span-2 bg-yellow-600 hover:bg-yellow-700 p-2 rounded font-bold transition">طلب كود التحقق</button>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    ''', **locals())

# --- مسارات الـ API (العمليات الخلفية) ---

@app.route('/toggle_radar', methods=['POST'])
@login_required
def toggle_radar():
    if radar_logic.is_running:
        telegram_engine.run_coroutine(radar_logic.stop_radar())
    else:
        telegram_engine.run_coroutine(radar_logic.start_radar())
    return redirect(url_for('dashboard'))

@app.route('/save_keywords', methods=['POST'])
@login_required
def save_keywords():
    raw_text = request.form.get('keywords', '')
    keywords = [k.strip() for k in raw_text.split('\n') if k.strip()]
    with db.get_connection() as conn:
        conn.execute("DELETE FROM keywords")
        for k in keywords:
            conn.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (k,))
        conn.commit()
    flash("تم تحديث الكلمات بنجاح!", "success")
    return redirect(url_for('dashboard'))

@app.route('/save_ai', methods=['POST'])
@login_required
def save_ai():
    db.set_setting("openrouter_api_key", request.form.get('api_key', ''))
    db.set_setting("ai_enabled", "1" if request.form.get('enabled') else "0")
    flash("تم حفظ إعدادات الذكاء الاصطناعي.", "success")
    return redirect(url_for('dashboard'))

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    # هنا يتم استدعاء logic طلب الكود من الجزء 2
    phone = request.form.get('phone')
    api_id = request.form.get('api_id')
    api_hash = request.form.get('api_hash')
    # حفظ المعلومات مبدئياً لإتمام تسجيل الدخول (سنحتاج لصفحة إدخال الكود)
    res = telegram_engine.run_coroutine(telegram_engine.request_code(phone, api_id, api_hash))
    if res['success']:
        return f'''
        <body style="background:#111; color:white; font-family:sans-serif; text-align:center; padding-top:50px;">
            <h2>تم إرسال الكود إلى {phone}</h2>
            <form action="/verify_code" method="POST">
                <input type="hidden" name="phone" value="{phone}">
                <input type="text" name="code" placeholder="أدخل الكود هنا" style="padding:10px; border-radius:5px;">
                <button type="submit" style="padding:10px 20px; background:green; color:white; border:none; border-radius:5px;">تأكيد</button>
            </form>
        </body>
        '''
    return f"خطأ: {res['message']}"

@app.route('/verify_code', methods=['POST'])
@login_required
def verify_code():
    phone = request.form.get('phone')
    code = request.form.get('code')
    res = telegram_engine.run_coroutine(telegram_engine.submit_code(phone, code))
    if res.get('success'):
        return redirect(url_for('dashboard'))
    return f"خطأ في التحقق: {res.get('message')}"

# --- تشغيل التطبيق النهائي ---
if __name__ == '__main__':
    # تشغيل الرادار تلقائياً عند بدء السيرفر
    # telegram_engine.run_coroutine(radar_logic.start_radar())
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)



