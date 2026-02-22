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
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import signal
import sys
import http.server
import socketserver

# Load secrets from .env file if present (Wispbyte / local dev)
load_dotenv()

# ─────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────

# Slot IDs to monitor
SLOT_IDS = [1, 2, 3, 4]
SLOT_LABELS = {1: "A", 2: "B", 3: "C", 4: "D"}  # Human-friendly labels

BASE_URL = (
    "https://arms.sse.saveetha.com/Handler/Student.ashx"
    "?Page=StudentInfobyId&Mode=GetCourseBySlot&Id={slot_id}"
)

# ARMS credentials for auto-login
ARMS_USERNAME = os.environ.get("ARMS_USERNAME", "")   # ARMS username / roll number
ARMS_PASSWORD = os.environ.get("ARMS_PASSWORD", "")   # ARMS password

ARMS_LOGIN_URL = "https://arms.sse.saveetha.com/Login.aspx"

# ARMS session cookie — auto-refreshed via login; also settable via /setcookie
_session = os.environ.get("ARMS_SESSION", "")
COOKIES = {"ASP.NET_SessionId": _session}

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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ADMIN_CHAT_ID      = os.environ.get("ADMIN_CHAT_ID", "")       # only this ID can run admin commands
ADMIN_PHONE        = os.environ.get("ADMIN_PHONE", "")          # your phone number (for reference)
CHANNEL_CHAT_ID    = os.environ.get("CHANNEL_CHAT_ID", "")      # private channel — all slot alerts go here

# File that stores subscribers across restarts
SUBSCRIBERS_FILE   = Path("subscribers.json")

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

    try:
        log.info("  [Login] Attempting auto-login to ARMS…")
        s = requests.Session()

        # Step 1: GET login page to grab hidden ASP.NET fields
        r = s.get(ARMS_LOGIN_URL, timeout=15)
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
        resp = s.post(ARMS_LOGIN_URL, data=payload, timeout=15, allow_redirects=True)

        # Step 3: Extract session cookie
        session_id = s.cookies.get("ASP.NET_SessionId")
        if not session_id:
            # Try from response cookies directly
            session_id = resp.cookies.get("ASP.NET_SessionId")

        if session_id:
            COOKIES["ASP.NET_SessionId"] = session_id
            _last_alert.clear()   # reset cooldowns — fresh session
            log.info(f"  [Login] ✅ Auto-login successful! Session: {session_id[:12]}…")
            send_message(
                ADMIN_CHAT_ID,
                f"🔑 <b>Auto-login successful!</b>\n"
                f"New session: <code>{session_id[:16]}…</code>"
            )
            return True
        else:
            log.error("  [Login] ❌ Login failed — bad credentials or ARMS changed its form.")
            send_message(
                ADMIN_CHAT_ID,
                "❌ <b>Auto-login failed!</b>\n"
                "Could not extract session cookie.\n"
                "Check username/password or use /setcookie manually."
            )
            return False

    except Exception as e:
        log.error(f"  [Login] ❌ Exception during login: {e}")
        send_message(ADMIN_CHAT_ID, f"❌ <b>Auto-login error:</b> {e}")
        return False



# ─────────────────────────────────────────────────────
#  SUBSCRIBER STORAGE
# ─────────────────────────────────────────────────────

def load_db() -> dict:
    if SUBSCRIBERS_FILE.exists():
        return json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
    return {"approved": [], "pending": {}}


def save_db(db: dict) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")


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


def send_message(chat_id: str | int, text: str, reply_markup=None) -> None:
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("sendMessage", **payload)


def broadcast(text: str) -> None:
    """Send a slot alert to the private channel."""
    log.info(f"  [Bot] Sending alert to channel {CHANNEL_CHAT_ID}…")
    send_message(CHANNEL_CHAT_ID, text)


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
    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=15)
        resp.raise_for_status()
        body = resp.text.strip()
        if not body:
            # Try auto-login first before alerting admin
            log.warning(f"  [Slot {slot_id}] Empty response — attempting auto-login…")
            if auto_login():
                # Retry the request with the new cookie
                try:
                    resp2 = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=15)
                    body = resp2.text.strip()
                except Exception:
                    body = ""
            if not body:
                alert_admin(
                    f"slot{slot_id}_empty",
                    f"❌ Slot {slot_id}: Empty response — session cookie has likely <b>expired</b>.\n"
                    "Auto-login also failed. Update with /setcookie &lt;new_value&gt;"
                )
                return None
        data = json.loads(body).get("Table", [])
        # Clear any previous empty-response alert for this slot
        _last_alert.pop(f"slot{slot_id}_empty", None)
        return data
    except requests.exceptions.ConnectionError:
        alert_admin(f"slot{slot_id}_conn", f"🌐 Slot {slot_id}: <b>Connection error</b> — no internet or ARMS is down.")
        return None
    except requests.exceptions.Timeout:
        alert_admin(f"slot{slot_id}_timeout", f"⏱ Slot {slot_id}: <b>Request timed out</b> — ARMS may be slow or unreachable.")
        return None
    except requests.exceptions.HTTPError as e:
        alert_admin(f"slot{slot_id}_http", f"🚫 Slot {slot_id}: <b>HTTP error</b> — {e}")
        return None
    except Exception as e:
        alert_admin(f"slot{slot_id}_err", f"💥 Slot {slot_id}: Unexpected error — {e}")
        return None


def summarise(courses: list[dict]) -> str:
    available = [c for c in courses if c.get("AvailableCount", 0) > 0]
    if not available:
        return "No open slots."
    lines = [f"• {c['SubjectCode']} – {c['AvailableCount']} slots" for c in available[:5]]
    if len(available) > 5:
        lines.append(f"  … and {len(available) - 5} more")
    return "\n".join(lines)





# ─────────────────────────────────────────────────────
#  SLOT MONITOR THREAD
# ─────────────────────────────────────────────────────

def monitor_thread():
    log.info("  [Monitor] Starting slot monitor…")
    baselines: dict[int, dict] = {}
    poll = 0

    while True:
        poll += 1
        log.info(f"\n[Poll #{poll:04d}]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        for slot_id in SLOT_IDS:
            courses = fetch_courses(slot_id)
            if courses is None:
                log.warning(f"  [Slot {slot_id}] ⚠  No response, skipping.")
                continue

            current_count = len(courses)

            if slot_id not in baselines:
                log.info(f"  [Slot {slot_id}] ✅ Baseline: {current_count} courses.")
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

                log.info(f"  [Slot {slot_id}] 🔔 COUNT INCREASED: {prev_count} → {current_count} (+{delta})")

                prev_ids = {c["SubjectId"]: c for c in prev_courses}
                curr_ids = {c["SubjectId"]: c for c in courses}

                added_lines = []
                for sid, c in curr_ids.items():
                    if sid not in prev_ids:
                        added_lines.append(f"  ➕ {c['SubjectCode']} – {c['SubjectName']} ({c['AvailableCount']} slots)")

                # Build Telegram message
                label = SLOT_LABELS.get(slot_id, str(slot_id))
                tg = [f"<b>🔔 ARMS Slot {label}: New Course Added! ▲</b>",
                      f"Courses: <b>{prev_count} → {current_count}</b>  (+{delta})"]
                if added_lines:
                    tg.append("\n<b>Added:</b>\n" + "\n".join(added_lines))
                tg.append(f"\n🕐 <i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>")
                tg_text = "\n".join(tg)

                broadcast(tg_text)
                send_message(ADMIN_CHAT_ID, tg_text)

            elif current_count < prev_count:
                log.info(f"  [Slot {slot_id}] 📉 Count decreased {prev_count}→{current_count} (no notification sent)")
            else:
                pass  # Reduced logging: only log on changes

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────────────
#  SHUTDOWN HANDLER
# ─────────────────────────────────────────────────────

def handle_shutdown(signum=None, frame=None):
    """Notify admin and exit gracefully on shutdown signals."""
    sig_name = signal.Signals(signum).name if signum else "Manual"
    log.info(f"\n[System] 🛑 Shutdown signal ({sig_name}) received. Notifying admin…")
    try:
        send_message(
            ADMIN_CHAT_ID,
            "🛑 <b>ARMS Monitor — Server Powering Down</b>\n\n"
            "The bot process is stopping or the server is restarting.\n"
            "Monitoring will be paused until the service is back online.\n\n"
            f"🕐 <i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
    except Exception as e:
        log.error(f"  [System] Failed to send shutdown message: {e}")
    
    log.info("Goodbye!")
    os._exit(0)  # Kill all threads and exit immediately


# ─────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────
#  WEB DASHBOARD (Flask)
# ─────────────────────────────────────────────────────

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# Basic Auth Credentials
DASHBOARD_USER = "admin"
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "saveetha123") # Change this in .env!

def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS

def authenticate():
    return Response(
    'Could not verify your access level for that URL.\n'
    'You have to login with proper credentials', 401,
    {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated

@app.route("/")
@requires_auth
def index():
    # Return a premium Glassmorphism React/Vanilla-JS Dashboard
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ARMS Monitor Control Panel</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-color: #0d1117;
                --card-bg: rgba(22, 27, 34, 0.6);
                --card-border: rgba(255, 255, 255, 0.1);
                --text-main: #c9d1d9;
                --text-muted: #8b949e;
                --accent: #58a6ff;
                --success: #238636;
                --danger: #da3633;
            }
            body {
                background-color: var(--bg-color);
                background-image: radial-gradient(circle at 15% 50%, rgba(88, 166, 255, 0.15), transparent 25%),
                                  radial-gradient(circle at 85% 30%, rgba(35, 134, 54, 0.15), transparent 25%);
                color: var(--text-main);
                font-family: 'Outfit', sans-serif;
                margin: 0;
                padding: 2rem;
                min-height: 100vh;
                box-sizing: border-box;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
                display: grid;
                grid-template-columns: 300px 1fr;
                gap: 2rem;
            }
            @media (max-width: 768px) {
                .container { grid-template-columns: 1fr; }
            }
            .header {
                grid-column: 1 / -1;
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--card-border);
                padding-bottom: 1rem;
                margin-bottom: 1rem;
            }
            h1 { margin: 0; font-weight: 600; font-size: 1.8rem; letter-spacing: -0.5px; display:flex; align-items:center; gap: 10px; }
            .status-badge {
                background: rgba(35, 134, 54, 0.2);
                color: #3fb950;
                padding: 5px 12px;
                border-radius: 20px;
                font-size: 0.85rem;
                font-weight: 600;
                border: 1px solid rgba(63, 185, 80, 0.4);
                animation: pulse 2s infinite;
            }
            @keyframes pulse {
                0% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.4); }
                70% { box-shadow: 0 0 0 10px rgba(63, 185, 80, 0); }
                100% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }
            }
            .glass-panel {
                background: var(--card-bg);
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 1.5rem;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            }
            .stat-grid {
                display: grid;
                grid-template-columns: 1fr;
                gap: 1rem;
            }
            .stat-card {
                background: rgba(255,255,255,0.03);
                border-radius: 12px;
                padding: 1rem;
                border: 1px solid rgba(255,255,255,0.05);
            }
            .stat-value { font-size: 2rem; font-weight: 600; color: var(--accent); margin-top: 5px;}
            .stat-label { font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px;}
            
            #log-container {
                background: #010409;
                border-radius: 8px;
                padding: 1rem;
                height: 600px;
                overflow-y: auto;
                font-family: 'Courier New', Courier, monospace;
                font-size: 0.9rem;
                line-height: 1.5;
                border: 1px solid #30363d;
                scrollbar-width: thin;
                scrollbar-color: #58a6ff #010409;
            }
            .log-line { border-bottom: 1px solid rgba(255,255,255,0.05); padding: 4px 0; }
            .log-time { color: var(--text-muted); margin-right: 15px; }
            .log-info { color: #8a2be2; }
            .log-warn { color: #d29922; }
            .log-err { color: var(--danger); }
            .log-success { color: #3fb950;}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎓 ARMS Slot Monitor</h1>
                <div class="status-badge">● SYSTEM ONLINE</div>
            </div>
            
            <div class="sidebar">
                <div class="glass-panel stat-grid">
                    <div class="stat-card">
                        <div class="stat-label">Subscribers</div>
                        <div class="stat-value" id="sub-count">--</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Monitored Slots</div>
                        <div class="stat-value" id="slot-count">--</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Last Poll Update</div>
                        <div class="stat-value" style="font-size:1.2rem; color:#c9d1d9;" id="last-poll">Waiting...</div>
                    </div>
                </div>
            </div>

            <div class="main-content">
                <div class="glass-panel">
                    <h2 style="margin-top:0; font-size: 1.2rem; color: var(--text-muted);">Live Terminal Logs</h2>
                    <div id="log-container">Loading system logs...</div>
                </div>
            </div>
        </div>

        <script>
            function formatLog(line) {
                if(!line) return '';
                let formatted = line.replace(/</g, "&lt;").replace(/>/g, "&gt;");
                
                // Colorize based on keywords
                if(formatted.includes("[Bot]") || formatted.includes("[Monitor]")) formatted = `<span class="log-info">${formatted}</span>`;
                if(formatted.includes("⚠") || formatted.includes("WARNING")) formatted = `<span class="log-warn">${formatted}</span>`;
                if(formatted.includes("❌") || formatted.includes("ERROR")) formatted = `<span class="log-err">${formatted}</span>`;
                if(formatted.includes("✅") || formatted.includes("started")) formatted = `<span class="log-success">${formatted}</span>`;
                
                // Extract timestamp if it exists (assuming format HH:MM:SS at start)
                const timeMatch = formatted.match(/^(\d{2}:\d{2}:\d{2})\s+(.*)/);
                if(timeMatch) {
                    return `<div class="log-line"><span class="log-time">[${timeMatch[1]}]</span>${timeMatch[2]}</div>`;
                }
                return `<div class="log-line">${formatted}</div>`;
            }

            async function updateDashboard() {
                try {
                    const statsRes = await fetch('/api/stats');
                    const stats = await statsRes.json();
                    document.getElementById('sub-count').innerText = stats.subscribers;
                    document.getElementById('slot-count').innerText = stats.slots;
                    document.getElementById('last-poll').innerText = stats.time;

                    const logsRes = await fetch('/api/logs');
                    const logsData = await logsRes.json();
                    
                    const logContainer = document.getElementById('log-container');
                    const isScrolledToBottom = logContainer.scrollHeight - logContainer.clientHeight <= logContainer.scrollTop + 50;
                    
                    logContainer.innerHTML = logsData.logs.map(formatLog).join('');
                    
                    if(isScrolledToBottom) {
                        logContainer.scrollTop = logContainer.scrollHeight;
                    }
                } catch(e) {
                    console.error("Dashboard update failed", e);
                }
            }

            // Update immediately, then every 3 seconds
            updateDashboard();
            setInterval(updateDashboard, 3000);
        </script>
    </body>
    </html>
    """
    return html

@app.route("/api/stats")
@requires_auth
def api_stats():
    db = load_db()
    return jsonify({
        "subscribers": len(db.get("approved", [])),
        "slots": len(SLOT_IDS),
        "time": datetime.now().strftime("%I:%M:%S %p")
    })

@app.route("/api/logs")
@requires_auth
def api_logs():
    try:
        # Read last 150 lines efficiently
        with open("slot_monitor.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": lines[-150:]})
    except Exception as e:
        return jsonify({"logs": [f"Error reading logs: {e}"]})

def flask_server():
    import werkzeug.serving
    import logging
    
    # Force Werkzeug logger to be quiet
    log_werkzeug = logging.getLogger('werkzeug')
    log_werkzeug.setLevel(logging.ERROR)
    log_werkzeug.disabled = True

    # Custom handler to completely suppress request logging
    class NoLogRequestHandler(werkzeug.serving.WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            pass
        def log(self, type, message, *args):
            pass

    port = int(os.environ.get("PORT", 8100))
    ip_addr = os.environ.get("IP", "::")
    log.info(f"  [Web] Attempting to start Flask WSGI on {ip_addr}:{port} (Silent HTTP mode)")
    werkzeug.serving.run_simple(
        ip_addr, port, app,
        use_reloader=False, 
        use_debugger=False,
        request_handler=NoLogRequestHandler
    )

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  ARMS Slot Monitor  –  Multi-User Bot Service")
    log.info(f"  Admin : {ADMIN_CHAT_ID}  |  Slots: {SLOT_IDS}")
    log.info("=" * 60)

    # Ensure subscribers file exists
    if not SUBSCRIBERS_FILE.exists():
        save_db({"approved": [], "pending": {}})
        log.info("  Created subscribers.json")

    # Auto-login to get a fresh session cookie
    if ARMS_USERNAME and ARMS_PASSWORD:
        auto_login()
    else:
        log.info("  [Login] Running with hardcoded session cookie (no credentials set).")

    # Update bot profile (Bio/About)
    set_bot_profile()

    # Startup message to admin
    slot_labels_str = ", ".join(SLOT_LABELS[s] for s in SLOT_IDS)
    send_message(
        ADMIN_CHAT_ID,
        "🚀 <b>ARMS Slot Monitor is running!</b>\n\n"
        f"👁 Watching Slots: <b>{slot_labels_str}</b>\n"
        f"⏱ Poll Interval: every <b>{POLL_INTERVAL}s</b>\n"
        "/setcookie &lt;value&gt; – update session cookie live",
    )

    # Start Flask Web Dashboard in background thread for Alwaysdata HTTP
    t_web = threading.Thread(target=flask_server, daemon=True, name="WebThread")
    t_web.start()

    # Start bot in background thread
    t_bot = threading.Thread(target=bot_thread, daemon=True, name="BotThread")
    t_bot.start()

    # Register shutdown signals (SIGINT for Ctrl+C, SIGTERM for cloud restarts)
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Run monitor on main thread
    try:
        monitor_thread()
    except Exception as e:
        log.error(f"CRITICAL ERROR in Monitor Thread: {e}")
        handle_shutdown()
