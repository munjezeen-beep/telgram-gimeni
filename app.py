# app.py
import os
import logging
import asyncio
import threading
import json
import re
from datetime import datetime

import aiohttp
import psycopg2
import psycopg2.pool

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from telethon import TelegramClient, events, errors

# ==========================================
# 0. تهيئات عامة ومسارات الملفات
# ==========================================
base_dir = os.path.abspath(os.path.dirname(__file__))
sessions_dir = os.path.join(base_dir, 'sessions')
os.makedirs(sessions_dir, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(base_dir, 'templates'))
app.secret_key = os.environ.get("SECRET_KEY", "radar-super-secret-key-2024")

# ==========================================
# 0.5 متغيرات مفقودة سابقاً
# ==========================================
# قاموس لتخزين جلسات تسجيل الدخول المؤقتة أثناء خطوة التوثيق (pending logins)
pending_logins = {}

# ==========================================
# 1. إعداد اللوق والدخول
# ==========================================
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
# 2. إعداد قاعدة البيانات (Postgres) مع pool
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/radar")

db_pool = None
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    logger.info("✅ اتصال PostgreSQL ناجح (pool).")
except Exception as e:
    logger.error(f"❌ فشل إنشاء pool لقاعدة البيانات: {e}")
    # سنبقى نحاول الاتصال بشكل منفصل لاحقًا عند الحاجة

def get_db():
    """
    الحصول على اتصال من pool إن وُجد، وإلا فتح اتصال مباشر.
    تذكر: إذا أُعيد الاتصال يجب استخدام put_db لإعادته إلى pool أو إغلاقه.
    """
    if db_pool:
        return db_pool.getconn()
    else:
        return psycopg2.connect(DATABASE_URL)

def put_db(conn):
    """إعادة الاتصال إلى الpool أو إغلاقه إذا pool غير معرف."""
    try:
        if db_pool and conn:
            db_pool.putconn(conn)
        else:
            if conn:
                conn.close()
    except Exception as e:
        logger.error(f"خطأ في put_db: {e}")

def init_db():
    """تهيئة الجداول الأساسية عند بدء التشغيل."""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # جدوال
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
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@radar.com")
        admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            ('admin_email', admin_email)
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            ('admin_password', generate_password_hash(admin_pass))
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            ('ai_enabled', '0')
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            ('openrouter_api_key', '')
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            ('radar_status', '1')
        )
        # كلمات افتراضية (قائمة)
        cur.execute("SELECT COUNT(*) FROM keywords")
        count = cur.fetchone()[0]
        if count == 0:
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
        logger.info("✅ تم تهيئة قاعدة البيانات بنجاح.")
    except Exception as e:
        logger.exception(f"خطأ أثناء init_db: {e}")
    finally:
        if conn:
            put_db(conn)

def log_event(content):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO logs (content) VALUES (%s)", (content,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل الحدث: {e}")
    finally:
        try:
            if conn:
                put_db(conn)
        except:
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
        logger.error(f"get_setting error: {e}")
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
        logger.error(f"set_setting error: {e}")
    finally:
        if conn:
            put_db(conn)

# ==========================================
# 3. تكامل الذكاء الاصطناعي (OpenRouter) - تحسين التعامل مع الردود
# ==========================================
PROMPT_TEMPLATE = """
أنت مساعد ذكي متخصص في تحليل رسائل تليجرام وتصنيف المرسلين بدقة عالية. المهمة: تحديد ما إذا كان المرسل **طالباً يطلب مساعدة** (seeker) أم **معلناً يروج لخدمات** (marketer).

الرسالة المراد تحليلها:
{message}
"""

async def classify_message(text, api_key):
    """
    استدعاء OpenRouter (أو أي مزود مماثل) لتحليل النص.
    يعيد dict بصيغة: {"type": "seeker"/"marketer", "confidence": int, "reason": str}
    """
    if not api_key:
        return {"type": "seeker", "confidence": 100, "reason": "AI Disabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "qwen/qwen-2.5-72b-instruct",
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE.format(message=text)}],
        "max_tokens": 200
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=30) as resp:
                text_resp = await resp.text()
                if resp.status != 200:
                    logger.error(f"OpenRouter status {resp.status}: {text_resp}")
                    return {"type": "seeker", "confidence": 0, "reason": f"API Error: {resp.status}"}
                data = json.loads(text_resp)
                # محاولة استخراج نص الإجابة — قد يختلف الهيكل حسب المزود
                choices = data.get('choices') or []
                if choices:
                    content = choices[0].get('message', {}).get('content', '') or choices[0].get('text', '')
                else:
                    content = data.get('text', '')
                # نحاول إيجاد JSON داخل المحتوى
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except Exception:
                        pass
                # إذا لم نجد JSON، نبني نتيجة بسيطة مستنبطة من النص
                # (محايدة: نرجع seeker مع ثقة 50)
                return {"type": "seeker", "confidence": 50, "reason": content[:300]}
    except Exception as e:
        logger.exception(f"AI Classification Error: {e}")
        return {"type": "seeker", "confidence": 0, "reason": "Exception occurred"}

# ==========================================
# 4. محرك Telegram (Telethon) - مُحسّن
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
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
            cur.close()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error(f"get_settings error: {e}")
            return {}
        finally:
            if conn:
                put_db(conn)

    def get_keywords(self):
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT keyword FROM keywords")
            rows = cur.fetchall()
            cur.close()
            return [row[0].lower() for row in rows]
        except Exception as e:
            logger.error(f"get_keywords error: {e}")
            return []
        finally:
            if conn:
                put_db(conn)

    async def _run_client(self, account):
        phone = account['phone']
        session_path = os.path.join(sessions_dir, f"{phone}.session")
        client = TelegramClient(session_path, int(account['api_id']), account['api_hash'])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log_event(f"⚠️ الحساب {phone} غير مصرح به. يحتاج تسجيل ورود.")
                await client.disconnect()
                return

            self.clients[phone] = client
            log_event(f"🚀 الحساب {phone} متصل وبدأ الرصد.")

            @client.on(events.NewMessage)
            async def message_handler(event):
                try:
                    # تحقق حالة الرادار
                    settings = self.get_settings()
                    if settings.get('radar_status') != '1':
                        return

                    # تجاهل الرسائل الخاصة أو رسائل المُرسل نفسه
                    if event.is_private or getattr(event.message, 'out', False):
                        return

                    msg_text = (event.message.message or "").lower()
                    keywords = self.get_keywords()
                    found_word = next((word for word in keywords if word in msg_text), None)

                    if found_word:
                        log_event(f"🔍 رصد كلمة '{found_word}' في مجموعة عبر الحساب {phone}")

                        ai_enabled = settings.get('ai_enabled') == '1'
                        api_key = settings.get('openrouter_api_key')

                        is_seeker = True

                        if ai_enabled and api_key:
                            result = await classify_message(event.message.message, api_key)
                            log_event(f"🧠 تصنيف الذكاء الاصطناعي: {result.get('type')} (الثقة: {result.get('confidence')}%) - {result.get('reason', '')}")
                            if result.get('type') == 'marketer' and int(result.get('confidence', 0)) > 60:
                                is_seeker = False
                                log_event("🚫 تم تجاهل الرسالة لأنها إعلان (Marketer).")

                        if is_seeker:
                            await self._forward_alert(client, event, account.get('alert_group'))
                except Exception as e:
                    logger.exception(f"message_handler error: {e}")

            await client.run_until_disconnected()
        except errors.FloodWaitError as e:
            log_event(f"⏳ Flood wait لمدة {e.seconds} ثانية للحساب {phone}")
            await asyncio.sleep(e.seconds)
        except errors.SessionPasswordNeededError:
            log_event(f"🔐 حساب {phone} يحتاج تحقق بخطوتين - يرجى تسجيل الدخول يدوياً أولاً")
        except Exception as e:
            log_event(f"❌ توقف الحساب {phone}: {str(e)}")
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

            sender_name = (getattr(sender, 'first_name', '') or '') + ' ' + (getattr(sender, 'last_name', '') or '')
            sender_name = sender_name.strip() or 'غير معروف'
            sender_username = f"@{sender.username}" if getattr(sender, 'username', None) else "لا يوجد يوزر"

            chat_title = getattr(chat, 'title', 'مجموعة غير معروفة')
            chat_username = getattr(chat, 'username', None)
            if chat_username:
                chat_link = f"https://t.me/{chat_username}"
            else:
                # إذا لم يكن لديه يوزر، حاول استخدام صيغة c/ (works for groups with id)
                try:
                    chat_link = f"https://t.me/c/{chat.id}/{event.id}"
                except Exception:
                    chat_link = "لا يوجد رابط"

            footer = f"""
━━━━━━━━━━━━━━━━━━━
🚨 رادار ذكي - طلب مساعدة
━━━━━━━━━━━━━━━━━━━
النص الأصلي: {event.message.message}
المرسل: {sender_name} - {sender_username}
المجموعة: {chat_title} - [رابط]({chat_link})
━━━━━━━━━━━━━━━━━━━
"""

            if not target_group:
                log_event("⚠️ لم يتم تحديد مجموعة تنبيهات لهذا الحساب.")
                return

            dest = int(target_group) if str(target_group).lstrip('-').isdigit() else target_group
            # محاولة إعادة التوجيه (forward)
            try:
                await client.forward_messages(dest, event.message)
                await client.send_message(dest, footer)
                log_event("✅ تم إرسال التنبيه (تحويل) بنجاح.")
            except errors.ChatForwardsRestrictedError:
                # نسخ النص بدل التحويل
                full_text = f"{event.message.message}\n\n*(تم إرسال نسخة بسبب منع التحويل)*\n{footer}"
                if event.message.media:
                    await client.send_file(dest, event.message.media, caption=full_text)
                else:
                    await client.send_message(dest, full_text)
                log_event("✅ تم إرسال التنبيه (نسخة) بنجاح.")
            except Exception as e:
                log_event(f"❌ فشل إرسال التنبيه: {e}")
        except Exception as e:
            log_event(f"❌ خطأ في _forward_alert: {e}")

    def start_all(self):
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT phone, api_id, api_hash, alert_group, enabled FROM accounts WHERE enabled = TRUE")
            accounts = cur.fetchall()
            cur.close()
            for row in accounts:
                acc_dict = {
                    'phone': row[0],
                    'api_id': int(row[1]),
                    'api_hash': row[2],
                    'alert_group': row[3]
                }
                asyncio.run_coroutine_threadsafe(self._run_client(acc_dict), self.loop)
        except Exception as e:
            logger.exception(f"start_all error: {e}")
        finally:
            if conn:
                put_db(conn)

    def stop_all(self):
        for phone, client in list(self.clients.items()):
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), self.loop)
            except Exception as e:
                logger.error(f"Error stopping client {phone}: {e}")

radar_engine = TelegramEngine()

# ==========================================
# 5. واجهة المستخدم و Flask-Login
# ==========================================
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    # user_id هنا هو email الذي خزّنناه عند تسجيل الدخول
    return User(user_id)

# تخصيص رد عدم التفويض ليُرجع JSON للطلبات AJAX
@login_manager.unauthorized_handler
def unauthorized_callback():
    # إذا كانت طلب AJAX أو JSON، نرجع JSON 401 بدل إعادة توجيه للصفحة
    is_json_request = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.headers.get('Accept') == 'application/json'
    if is_json_request:
        return jsonify({'status': 'error', 'msg': 'غير مصرح - يرجى تسجيل الدخول'}), 401
    # بخلاف ذلك إعادة توجيه للصفحة
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        admin_email = get_setting('admin_email')
        admin_password_hash = get_setting('admin_password')
        if email == admin_email and check_password_hash(admin_password_hash, password):
            login_user(User(email))
            return redirect(url_for('index'))
        flash("بيانات الدخول خاطئة", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT keyword FROM keywords")
        kws = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT phone, api_id, api_hash, alert_group, enabled FROM accounts")
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
        return render_template('index.html', keywords='\n'.join(kws), accounts=accs, logs=logs, settings=settings, active_clients=list(radar_engine.clients.keys()))
    except Exception as e:
        logger.exception(f"index error: {e}")
        flash("حدث خطأ أثناء جلب البيانات", "danger")
        return render_template('index.html', keywords='', accounts=[], logs=[], settings={}, active_clients=[])
    finally:
        if conn:
            put_db(conn)

# ==========================================
# 6. مسارات API / إعدادات
# ==========================================
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
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT (keyword) DO NOTHING", (keyword,))
            conn.commit()
            cur.close()
            log_event(f"➕ تم إضافة كلمة مفتاحية: {keyword}")
            flash("تم إضافة الكلمة بنجاح", "success")
        except Exception as e:
            logger.exception(f"add_keyword error: {e}")
            flash("فشل إضافة الكلمة", "danger")
        finally:
            if conn:
                put_db(conn)
    return redirect(url_for('index'))

@app.route('/keyword/delete/<keyword>')
@login_required
def delete_keyword(keyword):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM keywords WHERE keyword = %s", (keyword,))
        conn.commit()
        cur.close()
        log_event(f"🗑️ تم حذف كلمة مفتاحية: {keyword}")
        flash("تم حذف الكلمة بنجاح", "success")
    except Exception as e:
        logger.exception(f"delete_keyword error: {e}")
        flash("فشل حذف الكلمة", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for('index'))

# ==========================================
# 7. مسارات إضافة وتوثيق حسابات تليجرام (خطوتين)
# ==========================================
# ملاحظة: هذه المسارات تدعم الطلبات JSON (AJAX). ترجع JSON عند حدوث خطأ أو نجاح.
@app.route('/account/add_step1', methods=['POST'])
@login_required
def account_step1():
    # دعم body JSON أو form-data
    data = request.get_json(silent=True) or request.form or {}
    phone = data.get('phone')
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')

    if not phone or not api_id or not api_hash:
        return jsonify({'status': 'error', 'msg': 'جميع الحقول مطلوبة'}), 400

    # دالة async لطلب الكود
    async def req_code():
        client = TelegramClient(os.path.join(sessions_dir, f"{phone}.session"), int(api_id), api_hash)
        await client.connect()
        try:
            sent_code = await client.send_code_request(phone)
            pending_logins[phone] = {
                'client': client,
                'phone_code_hash': sent_code.phone_code_hash,
                'api_id': int(api_id),
                'api_hash': api_hash,
                'created_at': datetime.utcnow().isoformat()
            }
            return {'status': 'success'}
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            return {'status': 'error', 'msg': 'رقم الهاتف غير صالح'}
        except errors.ApiIdInvalidError:
            await client.disconnect()
            return {'status': 'error', 'msg': 'API ID أو API Hash غير صحيح'}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {'status': 'error', 'msg': str(e)}

    future = asyncio.run_coroutine_threadsafe(req_code(), radar_engine.loop)
    try:
        result = future.result(timeout=20)
        return jsonify(result)
    except Exception as e:
        logger.exception(f"account_step1 error: {e}")
        return jsonify({'status': 'error', 'msg': f'خطأ في الاتصال: {e}'}), 500

@app.route('/account/add_step2', methods=['POST'])
@login_required
def account_step2():
    data = request.get_json(silent=True) or request.form or {}
    phone = data.get('phone')
    code = data.get('code')
    password = data.get('password', None)

    if not phone:
        return jsonify({'status': 'error', 'msg': 'رقم الهاتف مطلوب'}), 400

    if phone not in pending_logins:
        return jsonify({'status': 'error', 'msg': 'انتهت الجلسة أو لم تبدأ خطوة1، حاول مجدداً'}), 400

    login_data = pending_logins[phone]
    client = login_data['client']

    async def verify_code():
        try:
            # في telethon النسخة الحديثة sign_in signature يمكن أن تختلف، لذلك نجرب المسارات الممكنة
            try:
                # أولاً نستخدم phone_code_hash إن وُجد
                await client.sign_in(phone=phone, code=code, phone_code_hash=login_data.get('phone_code_hash'))
            except TypeError:
                # fallback إن كانت التواقيع مختلفة
                await client.sign_in(phone, code)
            return 'success'
        except errors.SessionPasswordNeededError:
            if not password:
                return 'need_password'
            try:
                await client.sign_in(password=password)
                return 'success'
            except errors.PasswordHashInvalidError:
                return 'password_invalid'
            except Exception as e:
                return f'error: {str(e)}'
        except errors.PhoneCodeInvalidError:
            return 'code_invalid'
        except Exception as e:
            return f'error: {str(e)}'

    future = asyncio.run_coroutine_threadsafe(verify_code(), radar_engine.loop)
    try:
        res = future.result(timeout=30)
    except Exception as e:
        logger.exception(f"account_step2 verify error: {e}")
        return jsonify({'status': 'error', 'msg': f'خطأ في الاتصال: {e}'}), 500

    if res == 'success':
        # حفظ الحساب في قاعدة البيانات
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO accounts (phone, api_id, api_hash, alert_group, enabled) VALUES (%s, %s, %s, %s, TRUE) ON CONFLICT (phone) DO UPDATE SET api_id = EXCLUDED.api_id, api_hash = EXCLUDED.api_hash, alert_group = EXCLUDED.alert_group, enabled = TRUE",
                (phone, login_data['api_id'], login_data['api_hash'], data.get('alert_group', ''))
            )
            conn.commit()
            cur.close()
            log_event(f"✅ تم إضافة الحساب بنجاح: {phone}")

            # تشغيل العميل في محرك الرادار
            acc_dict = {
                'phone': phone,
                'api_id': login_data['api_id'],
                'api_hash': login_data['api_hash'],
                'alert_group': data.get('alert_group', '')
            }
            asyncio.run_coroutine_threadsafe(radar_engine._run_client(acc_dict), radar_engine.loop)

            # تنظيف pending_logins
            try:
                del pending_logins[phone]
            except KeyError:
                pass

            return jsonify({'status': 'success'})
        except Exception as e:
            logger.exception(f"account_step2 DB save error: {e}")
            return jsonify({'status': 'error', 'msg': str(e)}), 500
        finally:
            if conn:
                put_db(conn)
    elif res == 'need_password':
        return jsonify({'status': 'need_password'})
    elif res == 'password_invalid':
        return jsonify({'status': 'error', 'msg': 'كلمة سر ثانية خاطئة'}), 400
    elif res == 'code_invalid':
        return jsonify({'status': 'error', 'msg': 'الرمز غير صحيح'}), 400
    else:
        return jsonify({'status': 'error', 'msg': str(res)}), 400

@app.route('/account/update_group', methods=['POST'])
@login_required
def update_account_group():
    phone = request.form.get('phone')
    group_id = request.form.get('group_id', '')
    if not phone:
        flash("رقم الهاتف مطلوب", "danger")
        return redirect(url_for('index'))
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE accounts SET alert_group = %s WHERE phone = %s", (group_id, phone))
        conn.commit()
        cur.close()
        log_event(f"تم تحديث مجموعة التنبيهات للحساب {phone} إلى {group_id}")
        flash("تم تحديث المجموعة بنجاح", "success")
    except Exception as e:
        logger.exception(f"update_account_group error: {e}")
        flash("فشل تحديث المجموعة", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for('index'))

@app.route('/account/delete/<phone>')
@login_required
def delete_account(phone):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM accounts WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
        # فصل العميل إذا كان يعمل
        if phone in radar_engine.clients:
            try:
                asyncio.run_coroutine_threadsafe(radar_engine.clients[phone].disconnect(), radar_engine.loop)
            except Exception:
                pass
            del radar_engine.clients[phone]
        # حذف ملف الجلسة
        session_file = os.path.join(sessions_dir, f"{phone}.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception as e:
                logger.error(f"cannot remove session file: {e}")
        log_event(f"🗑️ تم حذف الحساب {phone}")
        flash("تم حذف الحساب بنجاح", "success")
    except Exception as e:
        logger.exception(f"delete_account error: {e}")
        flash("فشل حذف الحساب", "danger")
    finally:
        if conn:
            put_db(conn)
    return redirect(url_for('index'))

# ==========================================
# 8. نقطة الدخول للتشغيل
# ==========================================
if __name__ == '__main__':
    # تهيئة DB
    init_db()
    # تشغيل الرادار تلقائياً إذا الإعداد يطلب ذلك
    try:
        if get_setting('radar_status', '1') == '1':
            radar_engine.start_all()
    except Exception as e:
        logger.error(f"خطأ عند محاولة تشغيل الرادار تلقائياً: {e}")

    port = int(os.environ.get("PORT", 8080))
    # استخدم host 0.0.0.0 ليتلقى الطلبات من خارج الحاوية
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
