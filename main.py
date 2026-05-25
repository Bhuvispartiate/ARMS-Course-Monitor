import os
import requests
import json
import time
import threading
import logging
import sqlite3
import html
from datetime import datetime
import pytz
from pathlib import Path
import signal
import sys
import werkzeug
from flask import Flask, request, jsonify, Response, session, redirect, url_for
from functools import wraps

app = Flask(__name__)
app.secret_key = "arms_monitor_secret_key_123"

# ─────────────────────────────────────────────────────
#  CONFIGURATION & STORAGE
# ─────────────────────────────────────────────────────
CONFIG_FILE = Path("config.json")
METRICS_FILE = Path("metrics.json")

IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    return datetime.now(IST)

def load_config():
    default = {
        "arms_username": "P192512045",
        "arms_password": "welcome",
        "poll_interval": 20,
        "dashboard_user": "Knightwinner",
        "dashboard_pass": "GreaterShifter",
        "telegram_bot_token": "",
        "admin_chat_id": "",
        "channel_chat_id": "",
        "dashboard_url": "http://arms-course-monitor.alwaysdata.net/",
        "slots": [
            {"id": 4, "label": "A"},
            {"id": 5, "label": "B"},
            {"id": 2, "label": "C"},
            {"id": 7, "label": "D"}
        ]
    }
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(default, indent=2))
        return default
    try:
        data = json.loads(CONFIG_FILE.read_text())
        for k, v in default.items():
            if k not in data:
                data[k] = v
        return data
    except:
        return default

def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))

CONFIG = load_config()

TELEGRAM_API = f"https://api.telegram.org/bot{CONFIG.get('telegram_bot_token')}"
BASE_URL = "https://arms.sse.saveetha.com/Handler/Student.ashx?Page=StudentInfobyId&Mode=GetCourseBySlot&Id={slot_id}"
ARMS_LOGIN_URL = "https://arms.sse.saveetha.com/Login.aspx?s=exp"
COOKIES = {"ASP.NET_SessionId": ""}

_last_alert: dict[str, float] = {}
ALERT_COOLDOWN = 3600

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.5",
    "cache-control": "no-cache, no-store",
    "pragma": "no-cache",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

GLOBAL_METRICS = {
    "start_time": get_ist_now(),
    "polls": 0,
    "latency": "0.00s",
    "total_courses": 0
}

# ─────────────────────────────────────────────────────
#  LOGGING & DB
# ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("slot_monitor.log", encoding="utf-8")]
)
log = logging.getLogger(__name__)

def init_history_db():
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, slot_id INTEGER, course_count INTEGER)''')
    conn.commit()
    conn.close()

def log_history(slot_id: int, course_count: int):
    try:
        conn = sqlite3.connect("history.db")
        c = conn.cursor()
        c.execute("INSERT INTO history (slot_id, course_count) VALUES (?, ?)", (slot_id, course_count))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"  [DB] SQLite history logging failed: {e}")

# ──────────────────────────────────────────────────────
#  AUTO-LOGIN & TELEGRAM
# ──────────────────────────────────────────────────────
def auto_login() -> bool:
    username = CONFIG.get("arms_username")
    password = CONFIG.get("arms_password")
    if not username or not password: return False
    for attempt in range(1, 4):
        try:
            log.info(f"  [Login] Attempting auto-login to ARMS (Attempt {attempt})…")
            s = requests.Session()
            r = s.get(ARMS_LOGIN_URL, timeout=30)
            r.raise_for_status()
            import re
            def _field(name):
                m = re.search(rf'id="{name}"[^>]*value="([^"]*)"|name="{name}"[^>]*value="([^"]*)"|value="([^"]*)"[^>]*name="{name}"', r.text)
                return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""
            payload = {
                "__VIEWSTATE": _field("__VIEWSTATE"),
                "__VIEWSTATEGENERATOR": _field("__VIEWSTATEGENERATOR"),
                "__EVENTVALIDATION": _field("__EVENTVALIDATION"),
                "txtusername": username,
                "txtpassword": password,
                "btnlogin": "Login",
            }
            resp = s.post(ARMS_LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
            session_id = s.cookies.get("ASP.NET_SessionId") or resp.cookies.get("ASP.NET_SessionId")
            if session_id:
                COOKIES["ASP.NET_SessionId"] = session_id
                _last_alert.clear()
                log.info(f"  [Login] ✅ Auto-login successful! Session: {session_id[:12]}…")
                return True
            time.sleep(2)
        except Exception as e:
            time.sleep(2)
    return False

def tg_post(method: str, **kwargs) -> dict:
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
        return r.json()
    except: return {}

def send_message(chat_id: str | int, text: str, reply_markup=None, inline_keyboard=None) -> None:
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    elif inline_keyboard: payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    resp = tg_post("sendMessage", **payload)
    if resp and not resp.get("ok"):
        log.error(f"  [Telegram] Failed to send message: {resp.get('description')}")

def broadcast(text: str) -> None:
    dash = CONFIG.get('dashboard_url')
    inline_kb = [[{"text": "🔗 Open Dashboard", "url": dash}]] if dash else None
    send_message(CONFIG.get('channel_chat_id'), text, inline_keyboard=inline_kb)

def alert_admin(key: str, message: str) -> None:
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_COOLDOWN: return
    _last_alert[key] = now
    log.error(f"  [ALERT] {message}")
    send_message(CONFIG.get('admin_chat_id'), f"⚠️ <b>ARMS Monitor Alert</b>\n\n{message}\n\n<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>\nUse /setcookie &lt;value&gt; to update session cookie.")

def alert_admin_error(error_type: str, details: str):
    now = time.time()
    if now - _last_alert.get(error_type, 0) > 3600:
        send_message(CONFIG.get('admin_chat_id'), f"⚠️ <b>System Error</b>\n\nType: <code>{error_type}</code>\nDetails: <code>{details}</code>\n\n<i>Check the dashboard logs for details.</i>")
        _last_alert[error_type] = now

# ─────────────────────────────────────────────────────
#  BOT & MONITOR THREADS
# ─────────────────────────────────────────────────────
def bot_thread():
    log.info("  [Bot] Starting Telegram bot polling…")
    offset = 0
    while True:
        try:
            resp = requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35).json()
            if not resp.get("ok"):
                log.error(f"  [Bot] Telegram API Error: {resp.get('description')}")
                time.sleep(5)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg: continue
                chat_id = str(msg["chat"]["id"])
                text = msg.get("text", "").strip()
                
                admin_id = str(CONFIG.get("admin_chat_id"))
                if chat_id != admin_id:
                    log.warning(f"  [Bot] Ignored message from unauthorized chat_id: {chat_id} (expected {admin_id})")
                    continue
                
                if text.startswith("/setcookie "):
                    val = text[11:].strip()
                    COOKIES["ASP.NET_SessionId"] = val
                    _last_alert.clear()
                    send_message(chat_id, f"✅ <b>Session cookie updated!</b>\n<code>{val[:20]}…</code>\n\nMonitor will use the new cookie on the next poll (within 15 s).")
                elif text == "/setcookie":
                    send_message(chat_id, "Usage: /setcookie &lt;ASP.NET_SessionId value&gt;\n\nGet it from browser DevTools → Application → Cookies → arms.sse.saveetha.com")
                elif text == "/dashboard":
                    dash = CONFIG.get('dashboard_url')
                    if dash: send_message(chat_id, f"📊 <b>ARMS Monitor Dashboard</b>", inline_keyboard=[[{"text": "🔗 Open Dashboard", "url": dash}]])
        except Exception as e:
            log.error(f"  [Bot] Error: {e}")
            time.sleep(5)

def fetch_courses(slot_id: int):
    url = BASE_URL.format(slot_id=slot_id)
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
            resp.raise_for_status()
            body = resp.text.strip()
            if not body:
                if auto_login():
                    try: body = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30).text.strip()
                    except: body = ""
                if not body:
                    if attempt == 3: alert_admin(f"slot{slot_id}_empty", f"❌ Slot {slot_id}: Empty response — session cookie expired.")
                    time.sleep(2)
                    continue
            data = json.loads(body).get("Table", [])
            _last_alert.pop(f"slot{slot_id}_empty", None)
            return data
        except Exception as e:
            if attempt == 3:
                alert_admin(f"slot{slot_id}_err", f"💥 Slot {slot_id} Error: {e}")
            time.sleep(2)
    return None

def get_faculty_name(course: dict) -> str:
    keys = ("FacultyName", "Faculty", "StaffName", "Staff", "TeacherName", "Teacher", "EmployeeName", "Employee", "ProfessorName", "Professor")
    for key in keys:
        val = course.get(key)
        if val:
            name = str(val).strip()
            if name.lower() not in {"null", "none", "nan"}: return name
    return "NTA"

def monitor_thread():
    log.info("  [Monitor] Starting slot monitor…")
    baselines = {}
    while True:
        poll_interval = CONFIG.get("poll_interval", 20)
        active_slots = CONFIG.get("slots", [])
        GLOBAL_METRICS["polls"] += 1
        log.info(f"\n[Poll #{GLOBAL_METRICS['polls']:04d}]  {get_ist_now().strftime('%Y-%m-%d %I:%M:%S %p %Z')}")
        cycle_courses_count = 0
        cycle_start_t = time.time()
        
        for slot_data in active_slots:
            try:
                slot_id = slot_data["id"]
                slot_label = slot_data["label"]
                t0 = time.time()
                courses = fetch_courses(slot_id)
                t1 = time.time()
                if courses is None:
                    continue
                log.info(f"  [API] Slot {slot_label} fetched in {(t1-t0):.2f}s")
                current_count = len(courses)
                cycle_courses_count += current_count
                log_history(slot_id, current_count)
                
                if slot_id not in baselines:
                    log.info(f"  [Slot {slot_label}] ✅ Baseline: {current_count} courses.")
                    baselines[slot_id] = {"count": current_count, "courses": courses}
                    continue
                
                prev_count = baselines[slot_id]["count"]
                prev_courses = baselines[slot_id]["courses"]
                
                if current_count != prev_count:
                    baselines[slot_id] = {"count": current_count, "courses": courses}
                    if current_count > prev_count:
                        delta = current_count - prev_count
                        log.info(f"  [Slot {slot_label}] 🔔 COUNT INCREASED: {prev_count} → {current_count} (+{delta})")
                        prev_ids = {c["SubjectId"]: c for c in prev_courses}
                        curr_ids = {c["SubjectId"]: c for c in courses}
                        added_lines = []
                        for sid, c in curr_ids.items():
                            if sid not in prev_ids:
                                faculty_name = get_faculty_name(c)
                                added_lines.append(f"  ➕ {html.escape(str(c['SubjectCode']))} – {html.escape(str(c['SubjectName']))} ({c['AvailableCount']} slots) | Faculty: <b>{html.escape(faculty_name)}</b>")
                        tg = [f"<b>🔔 ARMS Slot {slot_label}: New Course Added! ▲</b>", f"Courses: <b>{prev_count} → {current_count}</b>  (+{delta})"]
                        if added_lines: tg.append("\n<b>Added:</b>\n" + "\n".join(added_lines))
                        tg.append(f"\n🕐 <i>{get_ist_now().strftime('%Y-%m-%d %I:%M:%S %p IST')}</i>")
                        tg_text = "\n".join(tg)
                        send_message(CONFIG.get("admin_chat_id"), tg_text)
                        broadcast(tg_text)
                    elif current_count < prev_count:
                        log.info(f"  [Slot {slot_id}] 📉 Count decreased {prev_count}→{current_count} (no notification sent)")
            except Exception as e:
                log.error(f"  [Slot {slot_label}] ❌ Error: {e}")
                
        cycle_end_t = time.time()
        GLOBAL_METRICS["latency"] = f"{(cycle_end_t - cycle_start_t):.2f}s"
        GLOBAL_METRICS["total_courses"] = cycle_courses_count
        
        try:
            with open(METRICS_FILE, "w") as mf:
                json.dump({"start_time": GLOBAL_METRICS["start_time"].isoformat(), "polls": GLOBAL_METRICS["polls"], "latency": GLOBAL_METRICS["latency"], "total_courses": GLOBAL_METRICS["total_courses"]}, mf)
        except Exception as e: pass
        time.sleep(poll_interval)

# ─────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, werkzeug.exceptions.HTTPException): return jsonify({"error": e.description}), e.code
    return jsonify({"error": str(e), "type": type(e).__name__}), 500

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.is_json or request.path.startswith("/api/"): return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.form if not request.is_json else request.json
        u = data.get("username")
        p = data.get("password")
        if u == CONFIG.get("dashboard_user") and p == CONFIG.get("dashboard_pass"):
            session["logged_in"] = True
            return jsonify({"status": "success"}) if request.is_json else redirect(url_for("index"))
        return jsonify({"error": "Invalid credentials"}), 401 if request.is_json else "Invalid credentials", 401
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login | ARMS Monitor</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600&display=swap" rel="stylesheet">
        <style>
            :root { --bg: #0d1117; --accent: #58a6ff; --card: #161b22; }
            body { background: var(--bg); color: white; font-family: 'Outfit', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .login-card { background: var(--card); padding: 2.5rem; border-radius: 16px; border: 1px solid rgba(255,255,255,0.1); width: 100%; max-width: 380px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
            h1 { margin-bottom: 1.5rem; font-size: 1.8rem; text-align: center; background: linear-gradient(90deg, #58a6ff, #3fb950); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            input { width: 100%; padding: 12px; margin-bottom: 1rem; border-radius: 8px; border: 1px solid #30363d; background: #010409; color: white; box-sizing: border-box; outline: none; }
            input:focus { border-color: var(--accent); }
            button { width: 100%; padding: 12px; border-radius: 8px; border: none; background: var(--accent); color: #0d1117; font-weight: 600; cursor: pointer; transition: 0.2s; }
            button:hover { opacity: 0.9; transform: translateY(-1px); }
        </style>
    </head>
    <body>
        <div class="login-card">
            <h1>ARMS Portal</h1>
            <form method="POST"><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Unlock Dashboard</button></form>
        </div>
    </body>
    </html>
    """

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/config", methods=["GET", "POST"])
@requires_auth
def api_config():
    if request.method == "POST":
        data = request.json
        config = load_config()
        keys = ["arms_username", "arms_password", "poll_interval", "dashboard_user", "dashboard_pass", "telegram_bot_token", "admin_chat_id", "channel_chat_id", "dashboard_url", "slots"]
        for k in keys:
            if k in data:
                if k == "poll_interval": config[k] = int(data[k])
                else: config[k] = data[k]
        save_config(config)
        global CONFIG, TELEGRAM_API
        CONFIG = load_config()
        TELEGRAM_API = f"https://api.telegram.org/bot{CONFIG.get('telegram_bot_token')}"
        return jsonify({"status": "success"})
    return jsonify(CONFIG)

@app.route("/favicon.ico")
def favicon(): return Response(status=204)

@app.route("/")
@requires_auth
def index():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ARMS Monitor | Premium Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&family=JetBrains+Mono&display=swap" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --bg: #030712; --surface: rgba(17, 24, 39, 0.7); --border: rgba(255, 255, 255, 0.08); --accent: #6366f1; --accent-glow: rgba(99, 102, 241, 0.3); --success: #10b981; --danger: #ef4444; --text: #f3f4f6; --text-dim: #9ca3af; }
            * { box-sizing: border-box; }
            body { margin: 0; font-family: 'Outfit', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; display: flex; overflow-x: hidden; }
            .bg-glow { position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: -1; background: radial-gradient(circle at 20% 30%, rgba(99, 102, 241, 0.15) 0%, transparent 40%), radial-gradient(circle at 80% 70%, rgba(16, 185, 129, 0.1) 0%, transparent 40%); filter: blur(80px); }
            .sidebar { width: 280px; height: 100vh; background: rgba(0,0,0,0.3); border-right: 1px solid var(--border); backdrop-filter: blur(20px); padding: 2rem 1.5rem; display: flex; flex-direction: column; position: sticky; top: 0; }
            .brand { font-size: 1.5rem; font-weight: 600; margin-bottom: 3rem; display: flex; align-items: center; gap: 12px; }
            .nav-link { padding: 12px 16px; border-radius: 12px; cursor: pointer; color: var(--text-dim); transition: 0.3s; margin-bottom: 8px; display: flex; align-items: center; gap: 12px; font-weight: 500; }
            .nav-link:hover { background: rgba(255,255,255,0.05); color: var(--text); }
            .nav-link.active { background: var(--accent); color: white; box-shadow: 0 4px 15px var(--accent-glow); }
            main { flex: 1; padding: 2rem 3rem; max-width: 1200px; margin: 0 auto; width: 100%; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2.5rem; }
            .status-chip { background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.2); color: var(--success); padding: 6px 14px; border-radius: 100px; font-size: 0.85rem; font-weight: 600; display: flex; align-items: center; gap: 8px; }
            .status-dot { width: 8px; height: 8px; background: var(--success); border-radius: 50%; box-shadow: 0 0 10px var(--success); animation: pulse 2s infinite; }
            @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
            .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1.5rem; margin-bottom: 2rem; }
            .glass-card { background: var(--surface); border: 1px solid var(--border); backdrop-filter: blur(12px); border-radius: 20px; padding: 1.5rem; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
            .stat-label { font-size: 0.85rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }
            .stat-value { font-size: 1.8rem; font-weight: 600; margin-top: 8px; }
            .terminal { background: #000; border: 1px solid var(--border); border-radius: 16px; padding: 1rem; height: 450px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; scrollbar-width: thin; scrollbar-color: var(--accent) transparent; }
            .log-entry { margin-bottom: 6px; line-height: 1.5; border-bottom: 1px solid rgba(255,255,255,0.03); padding-bottom: 4px; }
            .log-time { color: var(--text-dim); margin-right: 12px; }
            .log-msg-success { color: var(--success); } .log-msg-error { color: var(--danger); } .log-msg-info { color: var(--accent); }
            .form-group { margin-bottom: 1.5rem; }
            .form-group label { display: block; margin-bottom: 8px; color: var(--text-dim); font-size: 0.9rem; }
            input { width: 100%; background: rgba(0,0,0,0.3); border: 1px solid var(--border); padding: 12px 16px; border-radius: 12px; color: white; font-family: inherit; font-size: 1rem; outline: none; transition: 0.2s; }
            input:focus { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-glow); }
            .btn { background: var(--accent); color: white; border: none; padding: 12px 24px; border-radius: 12px; font-weight: 600; cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; gap: 8px; }
            .btn:hover { transform: translateY(-1px); opacity: 0.9; }
            .btn-secondary { background: rgba(255,255,255,0.1); }
            .slot-config-item { background: rgba(255,255,255,0.03); padding: 1rem; border-radius: 12px; margin-bottom: 12px; display: flex; gap: 1rem; align-items: center; }
            .tab-content { display: none; } .tab-content.active { display: block; animation: fadeIn 0.4s ease; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
            @media (max-width: 1000px) { .sidebar { display: none; } .stats-row { grid-template-columns: repeat(2, 1fr); } }
        </style>
    </head>
    <body>
        <div class="bg-glow"></div>
        <aside class="sidebar">
            <div class="brand">🚀 ARMS Monitor</div>
            <nav>
                <div class="nav-link active" onclick="switchTab('dashboard', this)"><span>📊</span> Dashboard</div>
                <div class="nav-link" onclick="switchTab('settings', this)"><span>⚙️</span> System Settings</div>
                <div class="nav-link" onclick="switchTab('logs', this)"><span>📜</span> Live Logs</div>
            </nav>
            <div style="margin-top: auto;"><a href="/logout" style="color: var(--danger); text-decoration: none; font-size: 0.9rem; font-weight: 600;">🚪 Logout System</a></div>
        </aside>

        <main>
            <div class="header">
                <h1 id="page-title">Dashboard Overview</h1>
                <div class="status-chip"><div class="status-dot"></div> Live Monitoring</div>
            </div>

            <div id="tab-dashboard" class="tab-content active">
                <div class="stats-row">
                    <div class="glass-card"><div class="stat-label">Total Polls</div><div id="stat-polls" class="stat-value">--</div></div>
                    <div class="glass-card"><div class="stat-label">System Uptime</div><div id="stat-uptime" class="stat-value">--</div></div>
                    <div class="glass-card"><div class="stat-label">Total Courses</div><div id="stat-courses" class="stat-value">--</div></div>
                    <div class="glass-card"><div class="stat-label">Latency</div><div id="stat-latency" class="stat-value">--</div></div>
                </div>
                <div class="glass-card" style="margin-bottom: 2rem;">
                    <div class="stat-label" style="margin-bottom: 1.5rem;">Slot Activity (24h)</div>
                    <div style="height: 350px;"><canvas id="mainChart"></canvas></div>
                </div>
            </div>

            <div id="tab-settings" class="tab-content">
                <div class="glass-card" style="margin-bottom: 2rem;">
                    <h3 style="margin-top:0;">ARMS Monitor Slots</h3>
                    <p style="color: var(--text-dim); font-size: 0.9rem; margin-bottom: 2rem;">Configure which Slot IDs the background engine should track.</p>
                    <div id="slot-list"></div>
                    <button class="btn btn-secondary" onclick="addSlotRow()" style="margin-top: 1rem;">+ Add New Slot</button>
                </div>

                <div class="glass-card" style="margin-bottom: 2rem;">
                    <h3 style="margin-top:0;">System Configuration</h3>
                    <h4 style="color: var(--accent); margin-bottom: 0.5rem;">ARMS Portal Credentials</h4>
                    <div class="form-group"><label>ARMS Username / Roll No</label><input type="text" id="arms-user"></div>
                    <div class="form-group"><label>ARMS Password</label><input type="password" id="arms-pass"></div>
                    <div class="form-group"><label>Poll Interval (Seconds)</label><input type="number" id="poll-int"></div>

                    <h4 style="color: var(--accent); margin-bottom: 0.5rem; margin-top: 1.5rem;">Dashboard Access</h4>
                    <div class="form-group"><label>Dashboard Username</label><input type="text" id="dash-user"></div>
                    <div class="form-group"><label>Dashboard Password</label><input type="password" id="dash-pass"></div>
                    
                    <h4 style="color: var(--accent); margin-bottom: 0.5rem; margin-top: 1.5rem;">Telegram Bot Config</h4>
                    <div class="form-group"><label>Bot Token</label><input type="text" id="tg-token"></div>
                    <div class="form-group"><label>Admin Chat ID</label><input type="text" id="admin-chat-id"></div>
                    <div class="form-group"><label>Channel Chat ID</label><input type="text" id="channel-chat-id"></div>
                    <div class="form-group"><label>Dashboard URL</label><input type="text" id="dash-url"></div>
                </div>

                <button class="btn" onclick="saveConfig()">💾 Save All Configurations</button>
            </div>

            <div id="tab-logs" class="tab-content">
                <div class="glass-card">
                    <h3 style="margin-top:0;">System Execution Logs</h3>
                    <div id="terminal" class="terminal">Loading logs...</div>
                </div>
            </div>
        </main>

        <script>
            let mainChart = null;

            function switchTab(tabId, el) {
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
                document.getElementById('tab-' + tabId).classList.add('active');
                el.classList.add('active');
                document.getElementById('page-title').innerText = el.innerText.trim();
                
                if(tabId === 'settings') loadConfig();
            }

            async function updateStats() {
                try {
                    const r = await fetch('/api/stats');
                    const d = await r.json();
                    document.getElementById('stat-polls').innerText = d.polls;
                    document.getElementById('stat-uptime').innerText = d.uptime;
                    document.getElementById('stat-courses').innerText = d.total_courses;
                    document.getElementById('stat-latency').innerText = d.latency;

                    const lr = await fetch('/api/logs');
                    const ld = await lr.json();
                    const term = document.getElementById('terminal');
                    const scroll = term.scrollHeight - term.clientHeight <= term.scrollTop + 50;
                    term.innerHTML = ld.logs.map(l => {
                        let cls = '';
                        if(l.includes('✅') || l.includes('SUCCESS')) cls = 'log-msg-success';
                        if(l.includes('❌') || l.includes('ERROR')) cls = 'log-msg-error';
                        if(l.includes('[Bot]')) cls = 'log-msg-info';
                        return `<div class="log-entry"><span class="log-time">[${l.substring(0,8)}]</span><span class="${cls}">${l.substring(10)}</span></div>`;
                    }).join('');
                    if(scroll) term.scrollTop = term.scrollHeight;

                    const hr = await fetch('/api/history');
                    renderChart(await hr.json());
                } catch(e) {}
            }

            function renderChart(data) {
                const ctx = document.getElementById('mainChart').getContext('2d');
                if(!mainChart) {
                    mainChart = new Chart(ctx, { type: 'line', data: data, options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { y: { grid: {color: 'rgba(255,255,255,0.05)'}, ticks: {color: '#9ca3af'} }, x: { grid: {display: false}, ticks: {color: '#9ca3af'} } }, plugins: { legend: { labels: { color: '#f3f4f6', font: {family: 'Outfit'} } } } } });
                } else {
                    mainChart.data = data;
                    mainChart.update();
                }
            }

            function addSlotRow(id='', label='') {
                const div = document.createElement('div');
                div.className = 'slot-config-item';
                div.innerHTML = `<input type="number" value="${id}" placeholder="ID" style="width: 100px;">
                    <input type="text" value="${label}" placeholder="Slot Name" style="flex:1;">
                    <button class="btn btn-secondary" onclick="this.parentElement.remove()" style="padding: 10px;">✕</button>`;
                document.getElementById('slot-list').appendChild(div);
            }

            async function loadConfig() {
                const r = await fetch('/api/config');
                const d = await r.json();
                document.getElementById('arms-user').value = d.arms_username || '';
                document.getElementById('arms-pass').value = d.arms_password || '';
                document.getElementById('poll-int').value = d.poll_interval || 20;
                document.getElementById('dash-user').value = d.dashboard_user || '';
                document.getElementById('dash-pass').value = d.dashboard_pass || '';
                document.getElementById('tg-token').value = d.telegram_bot_token || '';
                document.getElementById('admin-chat-id').value = d.admin_chat_id || '';
                document.getElementById('channel-chat-id').value = d.channel_chat_id || '';
                document.getElementById('dash-url').value = d.dashboard_url || '';
                
                const list = document.getElementById('slot-list');
                list.innerHTML = '';
                (d.slots || []).forEach(s => addSlotRow(s.id, s.label));
            }

            async function saveConfig() {
                const slots = [];
                document.querySelectorAll('.slot-config-item').forEach(row => {
                    const inputs = row.querySelectorAll('input');
                    if(inputs[0].value && inputs[1].value) slots.push({id: parseInt(inputs[0].value), label: inputs[1].value});
                });
                
                const data = {
                    arms_username: document.getElementById('arms-user').value,
                    arms_password: document.getElementById('arms-pass').value,
                    poll_interval: parseInt(document.getElementById('poll-int').value),
                    dashboard_user: document.getElementById('dash-user').value,
                    dashboard_pass: document.getElementById('dash-pass').value,
                    telegram_bot_token: document.getElementById('tg-token').value,
                    admin_chat_id: document.getElementById('admin-chat-id').value,
                    channel_chat_id: document.getElementById('channel-chat-id').value,
                    dashboard_url: document.getElementById('dash-url').value,
                    slots: slots
                };
                await fetch('/api/config', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                alert('All configurations saved successfully!');
            }

            updateStats();
            setInterval(updateStats, 5000);
        </script>
    </body>
    </html>
    """
    return html

@app.route("/api/stats")
@requires_auth
def api_stats():
    try: start_time = datetime.fromisoformat(GLOBAL_METRICS["start_time"])
    except: start_time = get_ist_now()
    uptime_delta = get_ist_now() - start_time
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{uptime_delta.days}d {hours}h {minutes}m" if uptime_delta.days > 0 else f"{hours}h {minutes}m"
    return jsonify({
        "slots": len(CONFIG.get("slots", [])),
        "total_courses": GLOBAL_METRICS["total_courses"],
        "uptime": uptime_str,
        "polls": GLOBAL_METRICS["polls"],
        "latency": GLOBAL_METRICS["latency"],
        "time": f"{get_ist_now().strftime('%I:%M:%S %p')} IST"
    })

@app.route("/api/history")
@requires_auth
def api_history():
    active_slots = CONFIG.get("slots", [])
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    c.execute("SELECT DISTINCT timestamp FROM history ORDER BY timestamp DESC LIMIT 50")
    times = [r[0] for r in c.fetchall()][::-1] 
    datasets = []
    colors = ['#58a6ff', '#238636', '#d29922', '#8a2be2', '#da3633']
    for idx, slot in enumerate(active_slots):
        sid = slot["id"]
        color = colors[idx % len(colors)]
        c.execute("SELECT timestamp, course_count FROM history WHERE slot_id = ? ORDER BY timestamp DESC LIMIT 50", (sid,))
        raw_data = {r[0]: r[1] for r in c.fetchall()}
        data_points = []
        last_val = 0
        for t in times:
            if t in raw_data: last_val = raw_data[t]
            data_points.append(last_val)
        datasets.append({ "label": f"Slot {slot['label']} ({sid})", "data": data_points, "borderColor": color, "backgroundColor": color + "33", "borderWidth": 2, "pointRadius": 0, "fill": True, "tension": 0.4 })
    conn.close()
    clean_labels = [datetime.strptime(t, "%Y-%m-%d %H:%M:%S").strftime("%H:%M") for t in times]
    return jsonify({"labels": clean_labels, "datasets": datasets})

@app.route("/api/logs")
@requires_auth
def api_logs():
    try:
        if not os.path.exists("slot_monitor.log"): return jsonify({"logs": ["No logs yet."]})
        with open("slot_monitor.log", "rb") as f:
            try: f.seek(-15000, os.SEEK_END)
            except IOError: pass
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
            return jsonify({"logs": lines[-150:]})
    except Exception as e: return jsonify({"logs": [f"Error reading logs: {e}"]})

@app.route("/ping")
def ping(): return "pong"

def handle_shutdown(signum=None, frame=None):
    sig_name = signal.Signals(signum).name if signum else "Manual shutdown"
    log.info(f"\n[System] 🛑 Shutdown signal ({sig_name}) received.")
    sys.exit(0)

if __name__ == "__main__":
    init_history_db()
    
    log.info("=" * 60)
    log.info("  ARMS Slot Monitor  –  Combined Config-Driven Service")
    log.info("=" * 60)

    auto_login()

    t_bot = threading.Thread(target=bot_thread, daemon=True, name="BotThread")
    t_bot.start()

    t_monitor = threading.Thread(target=monitor_thread, daemon=True, name="MonitorThread")
    t_monitor.start()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    log_werkzeug = logging.getLogger('werkzeug')
    log_werkzeug.setLevel(logging.ERROR)
    log_werkzeug.disabled = True
    class NoLogRequestHandler(werkzeug.serving.WSGIRequestHandler):
        def log_request(self, code='-', size='-'): pass
        def log(self, type, message, *args): pass

    port = int(os.environ.get("PORT", 8100))
    ip_addr = os.environ.get("IP", "0.0.0.0")
    print(f"  [Web] Attempting to start Flask WSGI on {ip_addr}:{port} (Silent HTTP mode)")
    werkzeug.serving.run_simple(ip_addr, port, app, use_reloader=False, use_debugger=False, request_handler=NoLogRequestHandler)
