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
from telethon import TelegramClient, events
import requests

# --- Configuration & Paths ---
base_dir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, 
            template_folder=os.path.join(base_dir, 'templates'),
            static_folder=os.path.join(base_dir, 'static'))

app.secret_key = os.environ.get("SECRET_KEY", "radar-super-secret-999")
DATABASE_URL = os.environ.get("DATABASE_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RadarSystem")

# --- Global Storage for Telegram Clients ---
# We use a dictionary to keep track of active Telethon clients
active_clients = {}

# --- Database Manager ---
class DBManager:
    def get_connection(self):
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL else ""
        return psycopg2.connect(url, cursor_factory=DictCursor)

    def init_db(self):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Settings & Auth
                    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                    # Keywords to monitor
                    cur.execute("CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY)")
                    # Telegram Accounts
                    cur.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, api_id TEXT, api_hash TEXT, session_str TEXT, alert_group TEXT, enabled BOOLEAN DEFAULT TRUE)")
                    # Logs
                    cur.execute("CREATE TABLE IF NOT EXISTS logs (id SERIAL PRIMARY KEY, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                    
                    # Default Admin
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                               ('admin_email', 'admin@gmail.com'))
                    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                               ('admin_password', generate_password_hash('123456')))
                conn.commit()
                logger.info("Database Schema Initialized.")
        except Exception as e:
            logger.error(f"DB Init Error: {e}")

    def add_log(self, content):
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO logs (content) VALUES (%s)", (content,))
                conn.commit()
        except: pass

db = DBManager()

# --- AI Integration (Gemini) ---
def analyze_with_ai(text):
    if not GEMINI_API_KEY: return text
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [{
                    "text": f"قم بتلخيص هذه الرسالة المستلمة من تليجرام واستخراج أهم المعلومات منها بشكل احترافي وجذاب لإرسالها كتنبيه: {text}"
                }]
            }]
        }
        res = requests.post(url, json=payload, timeout=10)
        result = res.json()
        return result['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"AI Analysis Error: {e}")
        return text

# --- Telegram Radar Logic ---
async def start_radar_for_account(acc_data):
    phone = acc_data['phone']
    if phone in active_clients: return

    client = TelegramClient(f"sessions/{phone}", acc_data['api_id'], acc_data['api_hash'])
    
    @client.on(events.NewMessage)
    async def handler(event):
        # Get Keywords from DB
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT keyword FROM keywords")
                keywords = [r[0] for r in cur.fetchall()]
        
        msg_text = event.raw_text
        if any(kw.lower() in msg_text.lower() for kw in keywords):
            # Identified interesting message!
            ai_summary = analyze_with_ai(msg_text)
            alert_text = f"🚨 **رادار الخليج - اكتشاف جديد**\n\n{ai_summary}\n\n🔗 المصدر: {phone}"
            
            # Send to Alert Group
            if acc_data['alert_group']:
                try:
                    await client.send_message(int(acc_data['alert_group']), alert_text)
                    db.add_log(f"تم رصد رسالة وإرسال تنبيه من حساب {phone}")
                except Exception as e:
                    logger.error(f"Failed to send alert: {e}")

    try:
        await client.start()
        active_clients[phone] = client
        db.add_log(f"تم تشغيل الرادار بنجاح للحساب: {phone}")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Radar Loop Error for {phone}: {e}")

def run_radar_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Fetch all enabled accounts
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM accounts WHERE enabled = TRUE")
                accounts = cur.fetchall()
        
        tasks = [start_radar_for_account(acc) for acc in accounts]
        if tasks:
            loop.run_until_complete(asyncio.gather(*tasks))
    except Exception as e:
        logger.error(f"Global Radar Thread Error: {e}")

# --- Auth System ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id): self.id = id

@login_manager.user_loader
def load_user(user_id): return User(user_id)

# --- Flask Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key='admin_email'")
                stored_email = cur.fetchone()[0]
                cur.execute("SELECT value FROM settings WHERE key='admin_password'")
                stored_password = cur.fetchone()[0]
                if email == stored_email and check_password_hash(stored_password, password):
                    login_user(User(email))
                    return redirect(url_for('index'))
        flash("خطأ في البيانات")
    return render_template('login.html')

@app.route('/')
@login_required
def index():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT keyword FROM keywords")
            keywords = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT * FROM accounts")
            accounts = cur.fetchall()
            cur.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 10")
            logs = cur.fetchall()
    return render_template('index.html', keywords=keywords, accounts=accounts, logs=logs)

# --- API for Telegram Connection (AJAX) ---
temp_clients = {} # To hold clients during auth steps

@app.route('/account/add_step1', methods=['POST'])
@login_required
def add_step1():
    data = request.json
    phone = data.get('phone')
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    
    client = TelegramClient(f"sessions/{phone}", api_id, api_hash)
    temp_clients[phone] = client
    
    async def get_code():
        await client.connect()
        await client.send_code_request(phone)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(get_code())
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/account/add_step2', methods=['POST'])
@login_required
def add_step2():
    data = request.json
    phone = data.get('phone')
    code = data.get('code')
    password = data.get('password')
    client = temp_clients.get(phone)
    
    async def verify():
        try:
            await client.sign_in(phone, code, password=password)
            # Save to DB
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO accounts (phone, api_id, api_hash, enabled) VALUES (%s, %s, %s, %s) ON CONFLICT (phone) DO UPDATE SET api_id=EXCLUDED.api_id", 
                               (phone, str(client.api_id), client.api_hash, True))
                conn.commit()
            return True
        except Exception as e:
            return str(e)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(verify())
    
    if result is True:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "msg": result})

@app.route('/keyword/add', methods=['POST'])
@login_required
def add_keyword():
    kw = request.form.get('keyword')
    if kw:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO keywords (keyword) VALUES (%s) ON CONFLICT DO NOTHING", (kw,))
            conn.commit()
    return redirect(url_for('index'))

@app.route('/account/update_group', methods=['POST'])
@login_required
def update_group():
    phone = request.form.get('phone')
    group_id = request.form.get('group_id')
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE accounts SET alert_group=%s WHERE phone=%s", (group_id, phone))
        conn.commit()
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    db.init_db()
    # Start Radar Thread
    threading.Thread(target=run_radar_thread, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
