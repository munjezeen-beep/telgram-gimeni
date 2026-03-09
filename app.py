import os
import logging
import asyncio
import threading
import json
import aiohttp
import psycopg2
import psycopg2.pool
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from telethon import TelegramClient, events, errors
from telethon.tl.types import PeerChannel, PeerChat
import re

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

# ==========================================
# 2. إدارة قاعدة البيانات (PostgreSQL)
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/radar")

# إنشاء pool اتصالات
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    logger.info("✅ اتصال PostgreSQL ناجح")
except Exception as e:
    logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    db_pool = None

def get_db():
    """الحصول على اتصال من pool"""
    if db_pool:
        return db_pool.getconn()
    else:
        return psycopg2.connect(DATABASE_URL)

def put_db(conn):
    """إعادة الاتصال إلى pool"""
    if db_pool:
        db_pool.putconn(conn)
    else:
        conn.close()

def init_db():
    """إنشاء الجداول إذا لم تكن موجودة"""
    conn = get_db()
    cur = conn.cursor()
    
    # جدول الإعدادات
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # جدول الكلمات المفتاحية
    cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id SERIAL PRIMARY KEY,
            keyword TEXT UNIQUE NOT NULL
        )
    """)
    
    # جدول الحسابات
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
    
    # جدول السجلات
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # الإعدادات الافتراضية للأدمن والذكاء الاصطناعي
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
    
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ('admin_email', admin_email))
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ('admin_password', generate_password_hash(admin_pass)))
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ('ai_enabled', '0'))
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ('openrouter_api_key', ''))
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", ('radar_status', '1')) # 1 = يعمل, 0 = متوقف
    
    # الكلمات المفتاحية الافتراضية (قائمة موسعة)
    cur.execute("SELECT COUNT(*) FROM keywords")
    if cur.fetchone()[0] == 0:
        default_keywords = [
            'مساعدة', 'ساعدوني', 'ساعدني', 'أبي أحد', 'أبي حد', 'أبي مساعدة', 'محتاج', 'محتاجة', 'ضروري', 'مستعجل', 'أرجوكم', 'لو سمحتم',
            'واجب', 'واجبات', 'تكليف', 'تكاليف', 'حل', 'يحل', 'اسايمنت', 'assignment', 'homework', 'تكليفات', 'واجبي', 'واجباتي', 'أسايمنت',
            'بحث', 'بحوث', 'تقرير', 'تقارير', 'ريبورت', 'report', 'research', 'بحثي', 'تقريري', 'دراسة', 'دراسة حالة', 'case study', 'رسالة', 'رسائل',
            'مشروع', 'مشاريع', 'بروجكت', 'project', 'بروجيكت', 'مشروع تخرج', 'مشاريع تخرج', 'مشروعي', 'بروجكتي', 'خطة مشروع', 'مشروع نهائي',
            'برزنتيشن', 'presentation', 'بوربوينت', 'powerpoint', 'عرض', 'عروض', 'تصميم', 'تصاميم', 'بوستر', 'poster', 'برشور', 'brochure', 'انفوجرافيك', 'infographic', 'خريطة ذهنية', 'mind map',
            'فيديو', 'فيديوهات', 'مونتاج', 'مقطع', 'تصوير', 'تحرير', 'انميشن', 'animation', 'موشن جرافيك', 'motion graphic',
            'اختبار', 'اختبارات', 'كويز', 'كويزات', 'فاينل', 'ميد', 'امتحان', 'امتحانات', 'اختبار نهائي', 'اختبار منتصف', 'كويزات',
            'شرح', 'يشرح', 'درس', 'دروس', 'ملخص', 'ملخصات', 'مذكرة', 'مذكرات', 'أساسيات', 'تمارين', 'تدريبات', 'فهم', 'استيعاب', 'تبسيط',
            'رياضيات', 'فيزياء', 'كيمياء', 'أحياء', 'إنجليزي', 'عربي', 'تاريخ', 'جغرافيا', 'فلسفة', 'منطق', 'قانون', 'محاسبة', 'اقتصاد', 'إدارة', 'تسويق', 'برمجة', 'علوم حاسب', 'هندسة', 'طب', 'صيدلة', 'تمريض', 'حقوق', 'علوم سياسية', 'إعلام',
            'دكتور خصوصي', 'مدرس خصوصي', 'معلم خصوصي', 'مدرسة خصوصية', 'دروس خصوصية', 'تدريس خصوصي', 'شرح خصوصي', 'يشرح خصوصي', 'معيد', 'متخصص',
            'تعرفون أحد', 'تعرفون حد', 'من يعرف', 'من تعرف', 'أحد يعرف', 'حد يعرف', 'وين ألقى', 'كيف ألقى', 'كيف أحصل', 'مصدر', 'مرجع',
            'جامعة', 'كلية', 'دراسة', 'أكاديمي', 'تعليم', 'مدرسة', 'طالب', 'طالبة', 'خريج', 'مبتعث', 'ابتعاث', 'منحة', 'قبول', 'تسجيل', 'مواد', 'مقررات', 'خطة دراسية', 'جدول', 'محاضرة', 'محاضرات',
            'ترجمة', 'تلخيص', 'تدقيق', 'صياغة', 'كتابة', 'إعداد', 'تنفيذ', 'استشارة', 'توجيه', 'إرشاد', 'مراجعة', 'تصحيح', 'حل', 'مناقشة',
            'مراجعة', 'ليالي الامتحان', 'أسئلة', 'إجابات', 'نماذج', 'تجميعات', 'شروحات', 'تبسيط', 'حفظ', 'تذكر',
            'رسالة ماجستير', 'رسالة دكتوراه', 'أطروحة', 'بحث علمي', 'نشر', 'ورقة بحثية', 'مؤتمر', 'مجلة علمية', 'تحكيم', 'نشر علمي',
            'برمجة', 'كود', 'برنامج', 'تطبيق', 'موقع', 'نظام', 'قاعدة بيانات', 'خوارزمية', 'هيكل بيانات', 'واجهة', 'تصميم', 'اختبار', 'debug', 'troubleshooting',
            'رسم', 'أوتوكاد', 'سوليدوركس', 'ريفيت', 'ديزاين', 'تصميم معماري', 'إنشائي', 'ميكانيكي', 'كهربائي', 'civil', 'mechanical', 'electrical',
            'فوتوشوب', 'إليستريتور', 'ان ديزاين', 'جرافيك', 'graphic design', 'تصميم جرافيكي', 'شعار', 'logo', 'هوية', 'identity', 'براند', 'brand',
            'ترجمة لغة', 'ترجمة إنجليزي', 'ترجمة عربي', 'ترجمة علمية', 'ترجمة أدبية', 'تلخيص كتاب', 'تلخيص مقال', 'تحرير نص', 'تدقيق لغوي',
            'أحد يساعد', 'أحد يحل', 'أحد يشرح', 'أحد يعمل', 'أحد يسوي', 'أحد يصمم', 'أحد يبرمج', 'أحد يترجم', 'أحد يلخص', 'أحد يدقق', 'أحد يراجع'
        ]
        for kw in default_keywords:
            cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING", (kw,))
    
    conn.commit()
    cur.close()
    put_db(conn)
    logger.info("✅ تم تهيئة قاعدة البيانات بنجاح.")

def log_event(content):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO logs (content) VALUES (%s)", (content,))
    conn.commit()
    cur.close()
    put_db(conn)
    logger.info(content)

def get_setting(key, default=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    put_db(conn)
    return row[0] if row else default

def set_setting(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
    conn.commit()
    cur.close()
    put_db(conn)

# ==========================================
# 3. محرك الذكاء الاصطناعي (OpenRouter) - محسّن
# ==========================================
PROMPT_TEMPLATE = """
أنت مساعد ذكي متخصص في تحليل رسائل تليجرام وتصنيف المرسلين بدقة عالية. المهمة: تحديد ما إذا كان المرسل **طالباً يطلب مساعدة** (seeker) أم **معلناً يروج لخدمات** (marketer).

### **معايير التصنيف الدقيقة**

#### **أولاً: فئة الطالب (seeker)**
- السمات: يطلب مساعدة في مجاله الدراسي أو الأكاديمي. قد يطلب شرحاً، حل واجب، بحث، مشروع، ترجمة، إلخ.
- الأمثلة:
  - "حد يعرف دكتور يشرح عملي الفارما؟"
  - "أبي أحد يحل واجب الرياضيات ضروري"
  - "من يعرف مدرس خصوصي للفيزياء؟"
  - "محتاج بحث عن الذكاء الاصطناعي"
  - "كيف أسوي برزنتيشن احترافي؟"
  - "أحد عنده خبرة في برنامج SPSS؟"
  - "تعرفون دكتور خصوصي يشرح أونلاين؟"

#### **ثانياً: فئة المعلن (marketer)**
- السمات: يقدم خدمات تجارية (مدفوعة)، يحتوي على روابط واتساب أو تليجرام، قوائم طويلة بالخدمات، استخدام رموز تزيينية (⭐, ✅, ═════, ☆, 💯), عبارات مثل "نقدم لكم", "للتواصل خاص", "عروض حصرية".
- الأمثلة:
  - "إذا تبون حد شاطر يسوي البروجكتات والتقارير والبحوث والبوسترات كلموني عالخاص 🤍"
  - "✨📚 خدمات طلابية شاملة لدعم نجاحك الأكاديمي! 🎓✨\n🖋️ حل الواجبات والتمارين بدقة عالية ✅\n📑 إعداد أبحاث وتقارير معتمدة 📚\nللتواصل: @user"
  - "╔════════════════════════╗\n║ 🚀🌟 AQL – عقل الذكاء🌟🚀\n║💻 برمجة تطبيقات شاملة\n╚════════════════════════╝\n📩 @MMMM_9MMMMM9"
  - "✅سكليف( اجازه مرضيه pdf)\n✅كشف طبي(جوازات)\n✅اعذار طبية معتمدة صحتي\n♻️تواصل واتس +966568861079"

### **تعليمات خاصة**
- إذا كانت الرسالة تحتوي على روابط (واتساب، تليجرام) + قائمة خدمات → **marketer**.
- إذا كانت الرسالة استفهاماً (علامة استفهام) وتخلو من الروابط وقوائم الخدمات → **seeker**.
- إذا كانت الرسالة طويلة ومنسقة (نقاط، رموز) وتدعو للتواصل → **marketer**.
- انتبه للهجة الخليجية: عبارات مثل "أبي أحد", "تعرفون حد", "من يعرف" تدل على طالب، بينما "نقدم لكم", "لدينا", "للتواصل" تدل على معلن.

### **المخرجات المطلوبة**
يجب أن تكون النتيجة بصيغة JSON فقط ولا تحتوي على أي نص آخر. على سبيل المثال:
- للطالب: {"type": "seeker", "confidence": 95, "reason": "يطلب مساعدة في شرح مادة، ولا توجد أي روابط أو عروض تجارية."}
- للمعلن: {"type": "marketer", "confidence": 98, "reason": "يقدم قائمة خدمات طلابية مع رابط واتساب، ويستخدم رموز ترويجية."}

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
        "model": "qwen/qwen-2.5-72b-instruct",  # نموذج قوي ومجاني ويدعم العربية
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE.format(message=text)}]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data['choices'][0]['message']['content'].strip()
                    # استخراج JSON من النص (في حال أضاف النموذج أي نص زائد)
                    json_match = re.search(r'\{.*\}', content, re.DOTALL)
                    if json_match:
                        return json.loads(json_match.group())
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
        rows = cur.fetchall()
        cur.close()
        put_db(conn)
        return {row[0]: row[1] for row in rows}

    def get_keywords(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT keyword FROM keywords")
        rows = cur.fetchall()
        cur.close()
        put_db(conn)
        return [row[0].lower() for row in rows]

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
                    
                    is_seeker = True  # الافتراضي إرسال
                    
                    if ai_enabled and api_key:
                        result = await classify_message(event.message.message, api_key)
                        log_event(f"🧠 تصنيف الذكاء الاصطناعي: {result.get('type')} (الثقة: {result.get('confidence')}%) - {result.get('reason', '')}")
                        if result.get('type') == 'marketer' and result.get('confidence', 0) > 60:
                            is_seeker = False
                            log_event("🚫 تم تجاهل الرسالة لأنها إعلان (Marketer).")

                    if is_seeker:
                        await self._forward_alert(client, event, account['alert_group'])

            await client.run_until_disconnected()
        except errors.FloodWaitError as e:
            log_event(f"⏳ Flood wait لمدة {e.seconds} ثانية للحساب {phone}")
            await asyncio.sleep(e.seconds)
        except errors.SessionPasswordNeededError:
            log_event(f"🔐 حساب {phone} يحتاج تحقق بخطوتين - يرجى تسجيل الدخول يدوياً أولاً")
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
            sender_username = f"@{sender.username}" if getattr(sender, 'username', None) else "لا يوجد يوزر"
            
            chat_title = getattr(chat, 'title', 'مجموعة غير معروفة')
            chat_username = f"@{chat.username}" if getattr(chat, 'username', None) else None
            if chat_username:
                chat_link = f"https://t.me/{chat_username}"
            else:
                chat_link = f"https://t.me/c/{chat.id}/{event.id}" if hasattr(chat, 'id') else "لا يوجد رابط"

            # بناء التذييل
            footer = f"""
━━━━━━━━━━━━━━━━━━━
🚨 **رادار ذكي - طلب مساعدة**
━━━━━━━━━━━━━━━━━━━
📝 **النص الأصلي**: {event.message.message}
👤 **المرسل**: {sender_name} - {sender_username}
🏢 **المجموعة**: {chat_title} - [رابط]({chat_link})
━━━━━━━━━━━━━━━━━━━
            """
            
            if not target_group:
                log_event("⚠️ لم يتم تحديد مجموعة تنبيهات لهذا الحساب.")
                return

            # محاولة إعادة التوجيه أولاً (Forward)
            try:
                await client.forward_messages(int(target_group) if target_group.lstrip('-').isdigit() else target_group, event.message)
                await client.send_message(int(target_group) if target_group.lstrip('-').isdigit() else target_group, footer)
                log_event("✅ تم إرسال التنبيه (تحويل) بنجاح.")
            except errors.ChatForwardsRestrictedError:
                # إذا كانت المجموعة تمنع التحويل، ننسخ النص
                full_text = f"{event.message.message}\n\n*(تم إرسال نسخة بسبب منع التحويل)*{footer}"
                if event.message.media:
                    await client.send_file(int(target_group) if target_group.lstrip('-').isdigit() else target_group, event.message.media, caption=full_text)
                else:
                    await client.send_message(int(target_group) if target_group.lstrip('-').isdigit() else target_group, full_text)
                log_event("✅ تم إرسال التنبيه (نسخة) بنجاح.")
        except Exception as e:
            log_event(f"❌ فشل إرسال التنبيه: {e}")

    def start_all(self):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM accounts WHERE enabled = TRUE")
        accounts = cur.fetchall()
        cur.close()
        put_db(conn)
        for acc in accounts:
            # تحويل الصف إلى قاموس
            acc_dict = {
                'phone': acc[0],
                'api_id': acc[1],
                'api_hash': acc[2],
                'alert_group': acc[3],
                'enabled': acc[4]
            }
            asyncio.run_coroutine_threadsafe(self._run_client(acc_dict), self.loop)

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
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        admin_email = get_setting('admin_email')
        admin_password = get_setting('admin_password')
        if email == admin_email and check_password_hash(admin_password, password):
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
    kws = [row[0] for row in cur.fetchall()]
    cur.execute("SELECT * FROM accounts")
    accs = []
    for row in cur.fetchall():
        accs.append({
            'phone': row[0],
            'api_id': row[1],
            'api_hash': row[2],
            'alert_group': row[3],
            'enabled': row[4]
        })
    cur.execute("SELECT content, created_at FROM logs ORDER BY created_at DESC LIMIT 100")
    logs = [{'content': row[0], 'created_at': row[1]} for row in cur.fetchall()]
    cur.execute("SELECT key, value FROM settings")
    settings = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    put_db(conn)
    
    return render_template('index.html', keywords='\n'.join(kws), accounts=accs, logs=logs, settings=settings, active_clients=list(radar_engine.clients.keys()))

# --- مسارات الذكاء الاصطناعي والإعدادات ---
@app.route('/api/settings/save', methods=['POST'])
@login_required
def save_settings():
    api_key = request.form.get('openrouter_api_key', '')
    ai_enabled = '1' if request.form.get('ai_enabled') else '0'
    radar_status = request.form.get('radar_status', '1')
    
    old_status = get_setting('radar_status', '1')
    
    set_setting('openrouter_api_key', api_key)
    set_setting('ai_enabled', ai_enabled)
    set_setting('radar_status', radar_status)
    
    if radar_status == '0' and old_status == '1':
        radar_engine.stop_all()
        log_event("🛑 تم إيقاف الرادار يدوياً.")
    elif radar_status == '1' and old_status == '0':
        radar_engine.start_all()
        log_event("▶️ تم تشغيل الرادار.")
        
    flash("تم حفظ الإعدادات بنجاح", "success")
    return redirect(url_for('index'))

@app.route('/keyword/add', methods=['POST'])
@login_required
def add_keyword():
    keyword = request.form.get('keyword', '').strip()
    if keyword:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING", (keyword,))
        conn.commit()
        cur.close()
        put_db(conn)
        log_event(f"➕ تم إضافة كلمة مفتاحية: {keyword}")
        flash("تم إضافة الكلمة بنجاح", "success")
    return redirect(url_for('index'))

@app.route('/keyword/delete/<keyword>')
@login_required
def delete_keyword(keyword):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM keywords WHERE keyword = %s", (keyword,))
    conn.commit()
    cur.close()
    put_db(conn)
    log_event(f"🗑️ تم حذف كلمة مفتاحية: {keyword}")
    flash("تم حذف الكلمة بنجاح", "success")
    return redirect(url_for('index'))

# --- مسارات إضافة وتوثيق حسابات تليجرام ---
@app.route('/account/add_step1', methods=['POST'])
@login_required
def account_step1():
    data = request.json
    phone = data.get('phone')
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    
    if not phone or not api_id or not api_hash:
        return jsonify({'status': 'error', 'msg': 'جميع الحقول مطلوبة'})

    # دالة غير متزامنة لطلب الكود
    async def req_code():
        client = TelegramClient(os.path.join(sessions_dir, f"{phone}.session"), int(api_id), api_hash)
        await client.connect()
        try:
            sent_code = await client.send_code_request(phone)
            # تخزين بيانات الجلسة مؤقتاً
            pending_logins[phone] = {
                'client': client,
                'phone_code_hash': sent_code.phone_code_hash,
                'api_id': int(api_id),
                'api_hash': api_hash
            }
            return {'status': 'success'}
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            return {'status': 'error', 'msg': 'رقم الهاتف غير صالح'}
        except errors.ApiIdInvalidError:
            await client.disconnect()
            return {'status': 'error', 'msg': 'API ID أو API Hash غير صحيح'}
        except Exception as e:
            await client.disconnect()
            return {'status': 'error', 'msg': str(e)}

    future = asyncio.run_coroutine_threadsafe(req_code(), radar_engine.loop)
    try:
        result = future.result(timeout=15)
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'خطأ في الاتصال: {e}'})

@app.route('/account/add_step2', methods=['POST'])
@login_required
def account_step2():
    data = request.json
    phone = data.get('phone')
    code = data.get('code')
    password = data.get('password')
    
    if phone not in pending_logins:
        return jsonify({'status': 'error', 'msg': 'انتهت الجلسة، حاول مجدداً'})
        
    login_data = pending_logins[phone]
    client = login_data['client']

    async def verify_code():
        try:
            await client.sign_in(phone, code, phone_code_hash=login_data['phone_code_hash'])
            return 'success'
        except errors.SessionPasswordNeededError:
            if not password:
                return 'need_password'
            try:
                await client.sign_in(password=password)
                return 'success'
            except errors.PasswordHashInvalidError:
                return 'كلمة سر خاطئة'
            except Exception as e:
                return f'خطأ: {str(e)}'
        except errors.PhoneCodeInvalidError:
            return 'الرمز غير صحيح'
        except Exception as e:
            return f'خطأ: {str(e)}'

    future = asyncio.run_coroutine_threadsafe(verify_code(), radar_engine.loop)
    try:
        res = future.result(timeout=15)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': f'خطأ في الاتصال: {e}'})

    if res == 'success':
        # حفظ في قاعدة البيانات
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO accounts (phone, api_id, api_hash, alert_group, enabled) VALUES (%s, %s, %s, %s, TRUE) ON CONFLICT (phone) DO UPDATE SET api_id = EXCLUDED.api_id, api_hash = EXCLUDED.api_hash, alert_group = EXCLUDED.alert_group, enabled = TRUE",
            (phone, login_data['api_id'], login_data['api_hash'], data.get('alert_group', ''))
        )
        conn.commit()
        cur.close()
        put_db(conn)
        log_event(f"✅ تم إضافة الحساب بنجاح: {phone}")
        
        # نقل العميل إلى محرك الرادار ليعمل في الخلفية
        acc_dict = {
            'phone': phone,
            'api_id': login_data['api_id'],
            'api_hash': login_data['api_hash'],
            'alert_group': data.get('alert_group', '')
        }
        asyncio.run_coroutine_threadsafe(radar_engine._run_client(acc_dict), radar_engine.loop)
        
        del pending_logins[phone]
        return jsonify({'status': 'success'})
    elif res == 'need_password':
        return jsonify({'status': 'need_password'})
    else:
        return jsonify({'status': 'error', 'msg': res})

@app.route('/account/update_group', methods=['POST'])
@login_required
def update_account_group():
    phone = request.form.get('phone')
    group_id = request.form.get('group_id', '')
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE accounts SET alert_group = %s WHERE phone = %s", (group_id, phone))
    conn.commit()
    cur.close()
    put_db(conn)
    log_event(f"تم تحديث مجموعة التنبيهات للحساب {phone} إلى {group_id}")
    flash("تم تحديث المجموعة بنجاح", "success")
    return redirect(url_for('index'))

@app.route('/account/delete/<phone>')
@login_required
def delete_account(phone):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM accounts WHERE phone = %s", (phone,))
    conn.commit()
    cur.close()
    put_db(conn)
    
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
    # لا تبدأ الرادار تلقائياً؟ نبدأه إذا كان radar_status == '1'
    if get_setting('radar_status', '1') == '1':
        radar_engine.start_all()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
