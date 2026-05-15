"""
ARMS Slot Monitor  –  Multi-User Telegram Bot Service
======================================================
Two threads run side-by-side:
  1. Bot Thread   – handles /start, contact sharing, admin /user commands
  2. Monitor Thread – polls ARMS API every 15 s; broadcasts to all subscribers

Setup:
    py -m pip install requests
    py api_monitor.py

Admin commands (only YOUR chat ID can use these):
    /user <phone>    – approve a subscriber (sends them congrats)
    /users           – list all approved subscribers
    /remove <phone>  – remove a subscriber
"""

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
import http.server
import socketserver

# ─────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────

BASE_URL = (
    "https://arms.sse.saveetha.com/Handler/Student.ashx"
    "?Page=StudentInfobyId&Mode=GetCourseBySlot&Id={slot_id}"
)

# ARMS credentials for auto-login (hardcoded)
ARMS_USERNAME = "P192512045"   # ARMS username / roll number
ARMS_PASSWORD = "welcome"      # ARMS password

ARMS_LOGIN_URL = "https://arms.sse.saveetha.com/Login.aspx?s=exp"

# ARMS session cookie — auto-refreshed via login; also settable via /setcookie
COOKIES = {"ASP.NET_SessionId": ""}

# Error alert rate-limiting: only alert admin once per error type per hour
_last_alert: dict[str, float] = {}
ALERT_COOLDOWN = 3600   # seconds

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.5",
    "cache-control": "no-cache, no-store",
    "pragma": "no-cache",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
}

# Seconds between slot polls
POLL_INTERVAL = 20

# ── Telegram ─────────────────────
TELEGRAM_BOT_TOKEN = "8340772186:AAGpn5IZx6Af_3oQaomJir2itlfiCmQJI3s"
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ADMIN_CHAT_ID      = "8467592443"        # only this ID can run admin commands
ADMIN_PHONE        = "9360406137"         # your phone number (for reference)
CHANNEL_CHAT_ID    = "-1003845063774"    # private channel — all slot alerts go here

# File that stores subscribers across restarts
SUBSCRIBERS_FILE   = Path("subscribers.json")

# Dashboard URL for notifications
DASHBOARD_URL      = "https://your-site.alwaysdata.net"

# ─────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("slot_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  AUTO-LOGIN
# ──────────────────────────────────────────────────────

def auto_login() -> bool:
    """
    Log into ARMS with ARMS_USERNAME / ARMS_PASSWORD and update
    COOKIES with the fresh ASP.NET_SessionId.
    Returns True on success, False on failure.
    """
    if not ARMS_USERNAME or not ARMS_PASSWORD:
        log.warning("  [Login] No credentials set — skipping auto-login.")
        return False

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"  [Login] Attempting auto-login to ARMS (Attempt {attempt})…")
            s = requests.Session()

            # Step 1: GET login page to grab hidden ASP.NET fields
            r = s.get(ARMS_LOGIN_URL, timeout=30)
            r.raise_for_status()

            import re
            def _field(name):
                m = re.search(rf'id="{name}"[^>]*value="([^"]*)"|name="{name}"[^>]*value="([^"]*)"|value="([^"]*)"[^>]*name="{name}"', r.text)
                return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""

            viewstate       = _field("__VIEWSTATE")
            eventvalidation = _field("__EVENTVALIDATION")
            vsgenerator     = _field("__VIEWSTATEGENERATOR")

            # Step 2: POST credentials
            payload = {
                "__VIEWSTATE":          viewstate,
                "__VIEWSTATEGENERATOR": vsgenerator,
                "__EVENTVALIDATION":    eventvalidation,
                "txtusername":          ARMS_USERNAME,
                "txtpassword":          ARMS_PASSWORD,
                "btnlogin":             "Login",
            }
            resp = s.post(ARMS_LOGIN_URL, data=payload, timeout=30, allow_redirects=True)

import sys
import http.server
import socketserver

# ─────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────

BASE_URL = (
    "https://arms.sse.saveetha.com/Handler/Student.ashx"
    "?Page=StudentInfobyId&Mode=GetCourseBySlot&Id={slot_id}"
)

# ARMS credentials for auto-login (hardcoded)
ARMS_USERNAME = "P192512045"   # ARMS username / roll number
ARMS_PASSWORD = "welcome"      # ARMS password

ARMS_LOGIN_URL = "https://arms.sse.saveetha.com/Login.aspx?s=exp"

# ARMS session cookie — auto-refreshed via login; also settable via /setcookie
COOKIES = {"ASP.NET_SessionId": ""}

# Error alert rate-limiting: only alert admin once per error type per hour
_last_alert: dict[str, float] = {}
ALERT_COOLDOWN = 3600   # seconds

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.5",
    "cache-control": "no-cache, no-store",
    "pragma": "no-cache",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
}

# Seconds between slot polls
POLL_INTERVAL = 20

# ── Telegram ─────────────────────
TELEGRAM_BOT_TOKEN = "8340772186:AAGpn5IZx6Af_3oQaomJir2itlfiCmQJI3s"
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ADMIN_CHAT_ID      = "8467592443"        # only this ID can run admin commands
ADMIN_PHONE        = "9360406137"         # your phone number (for reference)
CHANNEL_CHAT_ID    = "-1003845063774"    # private channel — all slot alerts go here

# File that stores subscribers across restarts
SUBSCRIBERS_FILE   = Path("subscribers.json")

# Dashboard URL for notifications
DASHBOARD_URL      = "https://your-site.alwaysdata.net"

# ─────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("slot_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────
#  AUTO-LOGIN
# ──────────────────────────────────────────────────────

def auto_login() -> bool:
    """
    Log into ARMS with ARMS_USERNAME / ARMS_PASSWORD and update
    COOKIES with the fresh ASP.NET_SessionId.
    Returns True on success, False on failure.
    """
    if not ARMS_USERNAME or not ARMS_PASSWORD:
        log.warning("  [Login] No credentials set — skipping auto-login.")
        return False

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"  [Login] Attempting auto-login to ARMS (Attempt {attempt})…")
            s = requests.Session()

            # Step 1: GET login page to grab hidden ASP.NET fields
            r = s.get(ARMS_LOGIN_URL, timeout=30)
            r.raise_for_status()

            import re
            def _field(name):
                m = re.search(rf'id="{name}"[^>]*value="([^"]*)"|name="{name}"[^>]*value="([^"]*)"|value="([^"]*)"[^>]*name="{name}"', r.text)
                return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""

            viewstate       = _field("__VIEWSTATE")
            eventvalidation = _field("__EVENTVALIDATION")
            vsgenerator     = _field("__VIEWSTATEGENERATOR")

            # Step 2: POST credentials
            payload = {
                "__VIEWSTATE":          viewstate,
                "__VIEWSTATEGENERATOR": vsgenerator,
                "__EVENTVALIDATION":    eventvalidation,
                "txtusername":          ARMS_USERNAME,
                "txtpassword":          ARMS_PASSWORD,
                "btnlogin":             "Login",
            }
            resp = s.post(ARMS_LOGIN_URL, data=payload, timeout=30, allow_redirects=True)

            # Step 3: Extract session cookie
            session_id = s.cookies.get("ASP.NET_SessionId") or resp.cookies.get("ASP.NET_SessionId")

            if session_id:
                COOKIES["ASP.NET_SessionId"] = session_id
                _last_alert.clear()   # reset cooldowns — fresh session
                log.info(f"  [Login] ✅ Auto-login successful! Session: {session_id[:12]}…")
                # send_message(
                #     ADMIN_CHAT_ID,
                #     f"🔑 <b>Auto-login successful!</b>\n"
                #     f"New session: <code>{session_id[:16]}…</code>"
                # )
                return True
            else:
                log.warning(f"  [Login] ⚠ Attempt {attempt} failed: No session ID in response.")
                if attempt == max_retries:
                    log.error("  [Login] ❌ All login attempts failed.")
                    send_message(ADMIN_CHAT_ID, "❌ <b>Auto-login failed!</b>\nCheck credentials or use /setcookie manually.")
                    return False
                time.sleep(2)

        except Exception as e:
            log.warning(f"  [Login] ⚠ Attempt {attempt} exception: {e}")
            if attempt == max_retries:
                log.error(f"  [Login] ❌ Exception during auto-login after {max_retries} tries: {e}")
                send_message(ADMIN_CHAT_ID, f"❌ <b>Auto-login error after retries:</b> {e}")
                return False
            time.sleep(2)
    return False



# ─────────────────────────────────────────────────────
#  SUBSCRIBER STORAGE
# ─────────────────────────────────────────────────────

def load_db() -> dict:
    default_db = {
        "approved": [], 
        "pending": {},
        "slots": [
            {"id": 4, "label": "A"},
            {"id": 5, "label": "B"},
            {"id": 2, "label": "C"},
            {"id": 7, "label": "D"}
        ]
    }
    if SUBSCRIBERS_FILE.exists():
        try:
            data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
            # Ensure new schema fields exist
            if "slots" not in data:
                data["slots"] = default_db["slots"]
            return data
        except json.JSONDecodeError:
            pass
    return default_db
    
# ─────────────────────────────────────────────────────
#  ANALYTICS STORAGE (SQLite)
# ─────────────────────────────────────────────────────

def init_history_db():
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            slot_id INTEGER,
            course_count INTEGER
        )
    ''')
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

init_history_db()


def save_db(db: dict) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")

# ─────────────────────────────────────────────────────
#  GLOBAL METRICS & TIMEZONE
# ─────────────────────────────────────────────────────

IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    return datetime.now(IST)

GLOBAL_METRICS = {
    "start_time": get_ist_now(),
    "polls": 0,
    "latency": "0.00s",
    "total_courses": 0
}

# ─────────────────────────────────────────────────────
#  TELEGRAM HELPERS
# ─────────────────────────────────────────────────────

def tg_post(method: str, **kwargs) -> dict:
    """POST to any Telegram Bot API method."""
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        log.warning(f"[Telegram] {method} failed: {e}")
        return {}


def send_message(chat_id: str | int, text: str, reply_markup=None, inline_keyboard=None) -> None:
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    elif inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    tg_post("sendMessage", **payload)


def broadcast(text: str, include_dashboard: bool = False) -> None:
    """Send a slot alert to the private channel."""
    log.info(f"  [Bot] Sending alert to channel {CHANNEL_CHAT_ID}…")
    
    inline_kb = None
    if include_dashboard and DASHBOARD_URL:
        inline_kb = [[{"text": "🔗 Open Dashboard", "url": DASHBOARD_URL}]]
        
    send_message(CHANNEL_CHAT_ID, text, inline_keyboard=inline_kb)


def set_bot_profile() -> None:
    """Update the bot's Bio (short description) and About (description)."""
    bio = "🚀 Instant real-time alerts for ARMS course slots. Never miss an opening! 🎓"
    about = (
        "ARMS Slot Monitor — The most reliable way to track course slot availability in real-time. ⚡\n\n"
        "✅ Instant Telegram notifications\n"
        "✅ 24/7 Slot Monitoring\n"
        "✅ Secure & Multi-user\n\n"
        "Monitoring slots so you don't have to! 🚀"
    )

    log.info("  [Bot] Updating bot profile (Bio/About)…")
    
    # 1. Set Short Description (Bio)
    r_bio = tg_post("setMyShortDescription", short_description=bio)
    if r_bio.get("ok"):
        log.info("  [Bot] ✅ Bio updated successfully.")
    else:
        log.warning(f"  [Bot] ⚠ Bio update failed: {r_bio.get('description')}")

    # 2. Set Description (About)
    r_about = tg_post("setMyDescription", description=about)
    if r_about.get("ok"):
        log.info("  [Bot] ✅ About description updated successfully.")
    else:
        log.warning(f"  [Bot] ⚠ About description update failed: {r_about.get('description')}")


# ─────────────────────────────────────────────────────
#  BOT COMMAND HANDLERS
# ─────────────────────────────────────────────────────

def handle_start(chat_id: str, first_name: str) -> None:
    """Send the 'Share Phone' button to a new user."""
    keyboard = {
        "keyboard": [[{"text": "📱 Share My Phone Number", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    send_message(
        chat_id,
        f"👋 Hello <b>{first_name}</b>!\n\n"
        "Welcome to <b>ARMS Slot Notifier</b>.\n"
        "Tap the button below to share your phone number so the admin can activate your subscription.",
        reply_markup=keyboard,
    )


def handle_contact(chat_id: str, phone: str, first_name: str) -> None:
    """Store the pending user and notify admin."""
    phone = phone.lstrip("+").strip()
    db = load_db()

    # Already approved?
    for sub in db["approved"]:
        if sub.get("phone") == phone:
            send_message(chat_id, "✅ You are already an approved subscriber!")
            return

    db["pending"][phone] = {"chat_id": str(chat_id), "name": first_name}
    save_db(db)

    send_message(
        chat_id,
        "✅ <b>Phone number received!</b>\n\n"
        "The admin has been notified. You'll get a confirmation message once approved.\n"
        "Hang tight! 🎉",
    )

    # Notify admin
    send_message(
        ADMIN_CHAT_ID,
        f"📬 <b>New subscriber request</b>\n"
        f"Name : {first_name}\n"
        f"Phone: <code>{phone}</code>\n\n"
        f"Approve with:\n<code>/user {phone}</code>",
    )
    log.info(f"  [Bot] New pending user: {first_name} ({phone})")


def handle_add_user(chat_id: str, phone: str) -> None:
    """Admin command: /user <phone> — approve a subscriber."""
    if str(chat_id) != ADMIN_CHAT_ID:
        send_message(chat_id, "⛔ You are not authorised to use this command.")
        return

    phone = phone.lstrip("+").strip()
    db = load_db()

    # Already approved?
    for sub in db["approved"]:
        if sub.get("phone") == phone:
            send_message(chat_id, f"ℹ️ <code>{phone}</code> is already approved.")
            return

    # Look up in pending
    pending_info = db["pending"].get(phone)

    if pending_info:
        subscriber_chat_id = pending_info["chat_id"]
        subscriber_name    = pending_info.get("name", "Subscriber")
        del db["pending"][phone]
    else:
        # Admin is adding someone manually (user hasn't started the bot yet)
        subscriber_chat_id = None
        subscriber_name    = "User"

    db["approved"].append({
        "chat_id": subscriber_chat_id,
        "phone":   phone,
        "name":    subscriber_name,
        "added":   datetime.now().isoformat(),
    })
    save_db(db)

    admin_msg = (
        f"✅ <b>{subscriber_name}</b> (<code>{phone}</code>) approved!\n"
        f"Total subscribers: {len(db['approved'])}"
    )
    send_message(ADMIN_CHAT_ID, admin_msg)

    # Send congrats to new subscriber (if we have their chat ID)
    if subscriber_chat_id:
        send_message(
            subscriber_chat_id,
            "🎉 <b>Congratulations! You're now subscribed!</b>\n\n"
            "You will receive instant Telegram notifications whenever\n"
            "course slots change in ARMS.\n\n"
            "Stay tuned — we'll alert you the moment a slot opens! 🚀",
        )
        log.info(f"  [Bot] ✅ Approved & notified: {subscriber_name} ({phone})")
    else:
        log.info(f"  [Bot] ✅ Approved (offline): {phone}")
        send_message(
            ADMIN_CHAT_ID,
            f"⚠️ {phone} has not started the bot yet — they won't receive messages until they do.",
        )


def handle_list_users(chat_id: str) -> None:
    """Admin command: /users — list all approved subscribers."""
    if str(chat_id) != ADMIN_CHAT_ID:
        send_message(chat_id, "⛔ Not authorised.")
        return

    db = load_db()
    approved = db.get("approved", [])
    pending  = db.get("pending", {})

    if not approved and not pending:
        send_message(chat_id, "📋 No subscribers yet.")
        return

    lines = [f"<b>📋 Subscribers ({len(approved)})</b>"]
    for i, sub in enumerate(approved, 1):
        lines.append(f"{i}. {sub.get('name','?')} — <code>{sub.get('phone','?')}</code>")

    if pending:
        lines.append(f"\n<b>⏳ Pending ({len(pending)})</b>")
        for phone, info in pending.items():
            lines.append(f"• {info.get('name','?')} — <code>{phone}</code>")

    send_message(chat_id, "\n".join(lines))


def handle_remove_user(chat_id: str, phone: str) -> None:
    """Admin command: /remove <phone> — remove a subscriber."""
    if str(chat_id) != ADMIN_CHAT_ID:
        send_message(chat_id, "⛔ Not authorised.")
        return

    phone = phone.lstrip("+").strip()
    db = load_db()
    before = len(db["approved"])
    db["approved"] = [s for s in db["approved"] if s.get("phone") != phone]

    if len(db["approved"]) < before:
        save_db(db)
        send_message(chat_id, f"✅ <code>{phone}</code> removed.")
        log.info(f"  [Bot] Removed subscriber: {phone}")
    else:
        send_message(chat_id, f"❓ <code>{phone}</code> not found in approved list.")


def handle_set_cookie(chat_id: str, value: str) -> None:
    """Admin command: /setcookie <value> — update session cookie live."""
    if str(chat_id) != ADMIN_CHAT_ID:
        send_message(chat_id, "⛔ Not authorised.")
        return

    value = value.strip()
    if not value:
        send_message(chat_id, "Usage: /setcookie &lt;ASP.NET_SessionId value&gt;")
        return

    COOKIES["ASP.NET_SessionId"] = value
    # Clear all error alert cooldowns so next poll will re-verify
    _last_alert.clear()
    log.info(f"  [Bot] 🔑 Session cookie updated by admin.")
    send_message(
        chat_id,
        f"✅ <b>Session cookie updated!</b>\n"
        f"<code>{value[:20]}…</code>\n\n"
        "Monitor will use the new cookie on the next poll (within 15 s).",
    )


# ─────────────────────────────────────────────────────
#  BOT POLLING THREAD
# ─────────────────────────────────────────────────────

def bot_thread():
    """Long-poll the Telegram Bot API for incoming messages."""
    log.info("  [Bot] Starting Telegram bot polling…")
    offset = 0

    while True:
        try:
            resp = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            ).json()

            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg:
                    continue

                chat_id    = str(msg["chat"]["id"])
                first_name = msg["chat"].get("first_name", "there")
                text       = msg.get("text", "").strip()
                contact    = msg.get("contact")

                # Contact shared
                if contact:
                    handle_contact(chat_id, contact.get("phone_number", ""), first_name)
                    continue

                # Commands
                if text.startswith("/start"):
                    handle_start(chat_id, first_name)

                elif text.startswith("/user "):
                    phone = text[6:].strip()
                    handle_add_user(chat_id, phone)

                elif text == "/users":
                    handle_list_users(chat_id)

                elif text.startswith("/remove "):
                    phone = text[8:].strip()
                    handle_remove_user(chat_id, phone)

                elif text.startswith("/setcookie "):
                    value = text[11:].strip()
                    handle_set_cookie(chat_id, value)

                elif text == "/setcookie":
                    send_message(chat_id, "Usage: /setcookie &lt;ASP.NET_SessionId value&gt;\n\nGet it from browser DevTools → Application → Cookies → arms.sse.saveetha.com")

                elif text == "/dashboard":
                    if DASHBOARD_URL:
                        send_message(chat_id, f"📊 <b>ARMS Monitor Dashboard</b>\n\nClick the button below to view live analytics and configuration.", 
                                     inline_keyboard=[[{"text": "🔗 Open Dashboard", "url": DASHBOARD_URL}]])
                    else:
                        send_message(chat_id, "⚠️ Dashboard URL not configured. Contact admin.")

        except Exception as e:
            log.warning(f"  [Bot] Poll error: {e}")
            time.sleep(5)


# ─────────────────────────────────────────────────────
#  ERROR ALERT HELPER
# ─────────────────────────────────────────────────────

def alert_admin(key: str, message: str) -> None:
    """Send an error alert to admin, rate-limited to once per hour per key."""
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_COOLDOWN:
        return   # already alerted recently
    _last_alert[key] = now
    log.error(f"  [ALERT] {message}")
    try:
        send_message(
            ADMIN_CHAT_ID,
            f"⚠️ <b>ARMS Monitor Alert</b>\n\n{message}\n\n"
            f"<i>🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>\n"
            "Use /setcookie &lt;value&gt; to update session cookie.",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────
#  SLOT MONITOR HELPERS
# ─────────────────────────────────────────────────────

def fetch_courses(slot_id: int) -> list[dict] | None:
    url = BASE_URL.format(slot_id=slot_id)
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
            resp.raise_for_status()
            body = resp.text.strip()
            
            if not body:
                # Try auto-login first before alerting admin
                log.warning(f"  [Slot {slot_id}] Empty response (Attempt {attempt}) — attempting auto-login…")
                if auto_login():
                    # Retry the request with the new cookie
                    try:
                        resp2 = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
                        body = resp2.text.strip()
                    except Exception:
                        body = ""
                
                if not body:
                    if attempt == max_retries:
                        alert_admin(
                            f"slot{slot_id}_empty",
                            f"❌ Slot {slot_id}: Empty response — session cookie has likely <b>expired</b>.\n"
                            "Auto-login also failed. Update with /setcookie &lt;new_value&gt;"
                        )
                        return None
                    time.sleep(2)
                    continue

            data = json.loads(body).get("Table", [])
            # Clear any previous empty-response alert for this slot
            _last_alert.pop(f"slot{slot_id}_empty", None)
            return data

        except requests.exceptions.RequestException as e:
            log.warning(f"  [Slot {slot_id}] ⚠ Attempt {attempt} failed: {e}")
            if attempt == max_retries:
                if isinstance(e, requests.exceptions.ConnectionError):
                    alert_admin(f"slot{slot_id}_conn", f"🌐 Slot {slot_id}: <b>Connection error</b> — no internet or ARMS is down.")
                elif isinstance(e, requests.exceptions.Timeout):
                    alert_admin(f"slot{slot_id}_timeout", f"⏱ Slot {slot_id}: <b>Request timed out</b> (30s) — ARMS may be slow.")
                elif isinstance(e, requests.exceptions.HTTPError):
                    alert_admin(f"slot{slot_id}_http", f"🚫 Slot {slot_id}: <b>HTTP error</b> — {e}")
                else:
                    alert_admin(f"slot{slot_id}_err", f"💥 Slot {slot_id}: Unexpected error — {e}")
                return None
            time.sleep(2)
            
        except Exception as e:
            log.error(f"  [Slot {slot_id}] 💥 Unexpected exception: {e}")
            return None
    return None


def summarise(courses: list[dict]) -> str:
    available = [c for c in courses if c.get("AvailableCount", 0) > 0]
    if not available:
        return "No open slots."
    
    lines = []
    for c in available[:5]:
        faculty_name = get_faculty_name(c)
        lines.append(f"• {c['SubjectCode']} – {c['AvailableCount']} slots | {faculty_name}")

    if len(available) > 5:
        lines.append(f"  … and {len(available) - 5} more")
    return "\n".join(lines)


def get_faculty_name(course: dict) -> str:
    """Return the best faculty name available from the ARMS payload."""
    faculty_keys = (
        "FacultyName",
        "Faculty",
        "StaffName",
        "Staff",
        "TeacherName",
        "Teacher",
        "EmployeeName",
        "Employee",
        "ProfessorName",
        "Professor",
    )
    for key in faculty_keys:
        value = course.get(key)
        if value is None:
            continue
        faculty_name = str(value).strip()
        if faculty_name and faculty_name.lower() not in {"null", "none", "nan"}:
            return faculty_name
    return "NTA"





# ─────────────────────────────────────────────────────
#  SLOT MONITOR THREAD
# ─────────────────────────────────────────────────────

def monitor_thread():
    log.info("  [Monitor] Starting slot monitor…")
    global _last_alert
    
    # Store known counts per slot
    baselines: dict[int, dict] = {}

    while True:
        db = load_db()
        active_slots = db.get("slots", [])
        
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
                    log.warning(f"  [Slot {slot_label}] ⚠  No response, skipping.")
                    continue

                log.info(f"  [API] Slot {slot_label} fetched in {(t1-t0):.2f}s")
                current_count = len(courses)
                cycle_courses_count += current_count
                
                log_history(slot_id, current_count)

                if slot_id not in baselines:
                    log.info(f"  [Slot {slot_label}] ✅ Baseline: {current_count} courses.")
                    baselines[slot_id] = {"count": current_count, "courses": courses}
                    continue

                prev_count   = baselines[slot_id]["count"]
                prev_courses = baselines[slot_id]["courses"]

                if current_count != prev_count:
                    # Update baseline and file only when data actually changes
                    baselines[slot_id] = {"count": current_count, "courses": courses}
                    with open(f"latest_slot{slot_id}.json", "w", encoding="utf-8") as f:
                        json.dump(courses, f, indent=2, ensure_ascii=False)

                    if current_count > prev_count:
                        # Only notify on INCREASE
                        delta = current_count - prev_count

                        log.info(f"  [Slot {slot_label}] 🔔 COUNT INCREASED: {prev_count} → {current_count} (+{delta})")

                        prev_ids = {c["SubjectId"]: c for c in prev_courses}
                        curr_ids = {c["SubjectId"]: c for c in courses}

                        added_lines = []
                        for sid, c in curr_ids.items():
                            if sid not in prev_ids:
                                faculty_name = get_faculty_name(c)
                                course_line = (
                                    f"  ➕ {html.escape(str(c['SubjectCode']))} – "
                                    f"{html.escape(str(c['SubjectName']))} "
                                    f"({c['AvailableCount']} slots) | Faculty: <b>{html.escape(faculty_name)}</b>"
                                )
                                added_lines.append(course_line)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Run monitor on main thread
    try:
        monitor_thread()
    except Exception as e:
        log.error(f"CRITICAL ERROR in Monitor Thread: {e}")
        handle_shutdown()
