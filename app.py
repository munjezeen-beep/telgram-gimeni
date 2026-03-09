import os
import asyncio
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import DictCursor

# إعداد المسارات لضمان عمل الواجهة في Railway
base_dir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, 
            template_folder=os.path.join(base_dir, 'templates'),
            static_folder=os.path.join(base_dir, 'static'))

# جلب المتغيرات من Railway أو استخدام قيم افتراضية
app.secret_key = os.environ.get("SECRET_KEY", "radar-secret-999")
DATABASE_URL = os.environ.get("DATABASE_URL")

# إعداد السجلات لمراقبة الأخطاء
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RadarApp")

# --- إدارة قاعدة البيانات ---
class DBManager:
    def get_connection(self):
        # تصحيح بروتوكول postgres للعمل مع psycopg2 في Railway
        if not DATABASE_URL:
            raise Exception("DATABASE_URL variable is missing in Railway!")
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url, cursor_factory=DictCursor)

    def init_db(self):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # إنشاء الجداول الأساسية إذا لم تكن موجودة
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                    cur.execute("CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY)")
                    cur.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, api_id TEXT, api_hash TEXT, alert_group TEXT, enabled BOOLEAN DEFAULT TRUE)")
                    cur.execute("CREATE TABLE IF NOT EXISTS logs (id SERIAL PRIMARY KEY, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                    
                    # جلب بيانات الإدارة من متغيرات البيئة في Railway (التي أضفتها أنت)
                    admin_email = os.environ.get("ADMIN_EMAIL", "admin@gmail.com")
                    admin_pass = os.environ.get("ADMIN_PASSWORD", "123456")
                    
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", 
                               ('admin_email', admin_email))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", 
                               ('admin_password', generate_password_hash(admin_pass)))
                conn.commit()
                logger.info("✅ Database initialized successfully.")
        except Exception as e:
            logger.error(f"❌ DB Init Error: {e}")

db = DBManager()

# --- نظام الحماية ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id): self.id = id

@login_manager.user_loader
def load_user(user_id): return User(user_id)

# --- المسارات (Routes) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        try:
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key='admin_email'")
                    stored_email = cur.fetchone()[0]
                    cur.execute("SELECT value FROM settings WHERE key='admin_password'")
                    stored_password = cur.fetchone()[0]
                    
                    if email == stored_email and check_password_hash(stored_password, password):
                        login_user(User(email))
                        return redirect(url_for('index'))
                    else:
                        flash("بيانات الدخول غير صحيحة")
        except Exception as e:
            logger.error(f"Login Error: {e}")
            flash("حدث خطأ في الاتصال بقاعدة البيانات")
            
    return render_template('login.html')

@app.route('/')
@login_required
def index():
    keywords = []
    accounts = []
    logs = []
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword FROM keywords")
                keywords = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT * FROM accounts")
                accounts = cur.fetchall()
                cur.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 10")
                logs = cur.fetchall()
    except Exception as e:
        logger.error(f"Index Fetch Error: {e}")
    
    return render_template('index.html', keywords=keywords, accounts=accounts, logs=logs)

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# تشغيل التطبيق
if __name__ == '__main__':
    db.init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
