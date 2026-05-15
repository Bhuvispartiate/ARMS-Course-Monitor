import os
import json
import sqlite3
import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response
from functools import wraps
import pytz

SUBSCRIBERS_FILE = Path("subscribers.json")
METRICS_FILE = Path("metrics.json")
IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    return datetime.datetime.now(IST)

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
            if "slots" not in data:
                data["slots"] = default_db["slots"]
            return data
        except json.JSONDecodeError:
            pass
    return default_db

def save_db(db: dict) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
app = Flask(__name__)

# Authentication removed — dashboard is open access
def requires_auth(f):
    f.__name__ = f.__name__
    return f

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
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
                max-width: 1400px;
                margin: 0 auto;
                display: grid;
                grid-template-columns: 320px 1fr;
                gap: 2rem;
            }
            .header {
                grid-column: 1 / -1;
                display: flex;
                flex-wrap: wrap;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--card-border);
                padding-bottom: 1rem;
                margin-bottom: 1rem;
                gap: 1rem;
            }
            h1 { margin: 0; font-weight: 600; font-size: 1.8rem; letter-spacing: -0.5px; display:flex; align-items:center; gap: 10px; }
            .status-badge {
                background: rgba(35, 134, 54, 0.2);
                color: #3fb950;
                padding: 6px 16px;
                border-radius: 20px;
                font-size: 0.9rem;
                font-weight: 600;
                border: 1px solid rgba(63, 185, 80, 0.4);
                animation: pulse 2s infinite;
                white-space: nowrap;
            }
            @keyframes pulse {
                0% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.4); }
                70% { box-shadow: 0 0 0 10px rgba(63, 185, 80, 0); }
                100% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }
            }
            .glass-panel {
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid var(--card-border);
                border-radius: 20px;
                padding: 1.8rem;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
                margin-bottom: 1.5rem;
            }
            .stat-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
                gap: 1rem;
            }
            .stat-card {
                background: rgba(255,255,255,0.03);
                border-radius: 12px;
                padding: 1.2rem;
                border: 1px solid rgba(255,255,255,0.05);
                transition: transform 0.2s, background 0.2s;
            }
            .stat-card:hover {
                transform: translateY(-2px);
                background: rgba(255,255,255,0.06);
            }
            .stat-value { font-size: 2.2rem; font-weight: 600; color: var(--accent); margin-top: 5px; line-height: 1.2;}
            .stat-label { font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px;}
            
            #log-container {
                background: #010409;
                border-radius: 12px;
                padding: 1rem;
                height: 400px;
                overflow-y: auto;
                font-family: 'JetBrains Mono', 'Courier New', Courier, monospace;
                font-size: 0.9rem;
                line-height: 1.6;
                border: 1px solid #30363d;
                scrollbar-width: thin;
                scrollbar-color: #58a6ff #010409;
            }
            .log-line { border-bottom: 1px solid rgba(255,255,255,0.02); padding: 5px 0; }
            .log-time { color: var(--text-muted); margin-right: 15px; }
            .log-info { color: #8a2be2; }
            .log-warn { color: #d29922; }
            .log-err { color: var(--danger); }
            .log-success { color: #3fb950;}

            .tabs { display: flex; gap: 10px; margin-bottom: 1.5rem; overflow-x: auto; padding-bottom: 5px; }
            .tabs::-webkit-scrollbar { height: 4px; }
            .tabs::-webkit-scrollbar-thumb { background: var(--card-border); border-radius: 4px; }
            
            .tab-btn {
                background: rgba(255,255,255,0.05); color: var(--text-main); border: 1px solid var(--card-border);
                padding: 10px 20px; border-radius: 10px; cursor: pointer; font-family: 'Outfit'; font-size: 1rem;
                transition: all 0.2s ease; white-space: nowrap;
            }
            .tab-btn:hover { background: rgba(255,255,255,0.1); }
            .tab-btn.active { background: var(--accent); color: #000; font-weight: 600; border-color: var(--accent); box-shadow: 0 4px 15px rgba(88, 166, 255, 0.4);}
            
            .tab-content { display: none; animation: fadeIn 0.4s ease; }
            .tab-content.active { display: block; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

            .input-group { margin-bottom: 1rem; }
            .input-group label { display: block; margin-bottom: 5px; color: var(--text-muted); font-size:0.9rem; }
            .input-group input { 
                width: 100%; padding: 12px; border-radius: 8px; border: 1px solid var(--card-border); 
                background: rgba(0,0,0,0.4); color: white; font-family: 'Outfit'; font-size: 1rem; box-sizing: border-box;
                transition: border-color 0.2s;
            }
            .input-group input:focus { border-color: var(--accent); outline: none; }
            
            .btn {
                background: var(--success); color: white; border: none; padding: 12px 24px; font-size: 1rem;
                border-radius: 8px; cursor: pointer; font-family: 'Outfit'; font-weight: 600; transition: 0.2s; box-shadow: 0 4px 15px rgba(35, 134, 54, 0.3);
            }
            .btn:hover { background: #2ea043; transform: translateY(-1px); }
            
            .slot-row { display: flex; gap: 10px; margin-bottom: 15px; align-items:center; }
            .slot-row input { flex:1; }
            .btn-danger { background: var(--danger); box-shadow: 0 4px 15px rgba(218, 54, 51, 0.3); padding: 12px 16px;}
            .btn-danger:hover { background: #f85149; }

            /* Modern Mobile Responsiveness */
            @media (max-width: 900px) {
                body { padding: 1rem; }
                .container { 
                    grid-template-columns: 1fr; 
                    gap: 1.5rem;
                }
                .sidebar { order: -1; } /* Bring stats to top on mobile */
                .stat-grid { grid-template-columns: repeat(2, 1fr); }
                .stat-card[style*="grid-column"] { grid-column: 1 / -1 !important; }
                h1 { font-size: 1.5rem; }
            }
            
            @media (max-width: 600px) {
                body { padding: 0.5rem; }
                .container { gap: 1rem; }
                .glass-panel { padding: 1.2rem; border-radius: 16px; margin-bottom: 1rem; }
                .stat-grid { gap: 0.8rem; }
                .stat-card { padding: 1rem; }
                .stat-value { font-size: 1.6rem; }
                .stat-label { font-size: 0.75rem; }
                
                .header { flex-direction: column; align-items: flex-start; gap: 0.8rem; }
                .status-badge { align-self: flex-start; font-size: 0.8rem; padding: 4px 12px; }
                
                #log-container { height: 300px; font-size: 0.8rem; padding: 0.8rem; }
                .slot-row { flex-direction: column; align-items: stretch; background: rgba(0,0,0,0.2); padding: 10px; border-radius: 8px;}
                .btn-danger { width: 100%; margin-top: 5px; }
                .tab-btn { padding: 8px 16px; font-size: 0.9rem; }
            }
            
            @media (max-width: 400px) {
                .stat-grid { grid-template-columns: 1fr; }
            }
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
                        <div class="stat-label">Total Courses</div>
                        <div class="stat-value" id="course-count">--</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">System Uptime</div>
                        <div class="stat-value" style="font-size:1.4rem;" id="uptime">--</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Total Polls</div>
                        <div class="stat-value" id="poll-count">--</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">API Latency</div>
                        <div class="stat-value" id="api-latency">--</div>
                    </div>
                    <div class="stat-card" style="grid-column: 1 / -1;">
                        <div class="stat-label">Last Poll Update</div>
                        <div class="stat-value" style="font-size:1.2rem; color:#c9d1d9;" id="last-poll">Waiting...</div>
                    </div>
                </div>
            </div>

            <div class="main-content">
                <div class="tabs">
                    <button class="tab-btn active" onclick="switchTab('overview')">Overview</button>
                    <button class="tab-btn" onclick="switchTab('settings')">Settings</button>
                </div>

                <div id="tab-overview" class="tab-content active">
                    <div class="glass-panel">
                        <h2 style="margin-top:0; font-size: 1.2rem; color: var(--text-muted);">Analytics History (24h)</h2>
                        <div style="height: 250px; width: 100%;">
                            <canvas id="analyticsChart"></canvas>
                        </div>
                    </div>

                    <div class="glass-panel">
                        <h2 style="margin-top:0; font-size: 1.2rem; color: var(--text-muted);">Live Terminal Logs</h2>
                        <div id="log-container">Loading system logs...</div>
                    </div>
                </div>

                <div id="tab-settings" class="tab-content glass-panel">
                    <h2 style="margin-top:0;">Slot Configuration</h2>
                    <p style="color:var(--text-muted); font-size:0.9rem;">Change which ARMS slots are actively monitored. The background bot updates instantly upon save.</p>
                    
                    <div id="slots-editor"></div>
                    
                    <button class="tab-btn" onclick="addSlotRow()" style="margin-top:10px;">+ Add Slot</button>
                    <br><br>
                    <button class="btn" onclick="saveSlots()">💾 Save Configuration</button>
                </div>
            </div>
        </div>

        <script>
            let chartInstance = null;

            function switchTab(tabId) {
                document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById('tab-' + tabId).classList.add('active');
                event.target.classList.add('active');
                if(tabId === 'settings') loadSettings();
            }

            function formatLog(line) {
                if(!line) return '';
                if(line.includes("GET /api/")) return ''; // Hide dashboard noise
                let formatted = line.replace(/</g, "&lt;").replace(/>/g, "&gt;");
                if(formatted.includes("[Bot]") || formatted.includes("[Monitor]")) formatted = `<span class="log-info">${formatted}</span>`;
                if(formatted.includes("⚠") || formatted.includes("WARNING")) formatted = `<span class="log-warn">${formatted}</span>`;
                if(formatted.includes("❌") || formatted.includes("ERROR")) formatted = `<span class="log-err">${formatted}</span>`;
                if(formatted.includes("✅") || formatted.includes("started") || formatted.includes("INCREASED")) formatted = `<span class="log-success">${formatted}</span>`;
                const timeMatch = formatted.match(/^(\\d{2}:\\d{2}:\\d{2})\\s+(.*)/);
                if(timeMatch) return `<div class="log-line"><span class="log-time">[${timeMatch[1]}]</span>${timeMatch[2]}</div>`;
                return `<div class="log-line">${formatted}</div>`;
            }

            async function updateDashboard() {
                try {
                    const statsRes = await fetch('/api/stats');
                    const stats = await statsRes.json();
                    document.getElementById('sub-count').innerText = stats.subscribers;
                    document.getElementById('slot-count').innerText = stats.slots;
                    document.getElementById('course-count').innerText = stats.total_courses;
                    document.getElementById('uptime').innerText = stats.uptime;
                    document.getElementById('poll-count').innerText = stats.polls;
                    document.getElementById('api-latency').innerText = stats.latency;
                    document.getElementById('last-poll').innerText = stats.time;

                    const logsRes = await fetch('/api/logs');
                    const logsData = await logsRes.json();
                    const logContainer = document.getElementById('log-container');
                    const isScrolledToBottom = logContainer.scrollHeight - logContainer.clientHeight <= logContainer.scrollTop + 50;
                    logContainer.innerHTML = logsData.logs.map(formatLog).join('');
                    if(isScrolledToBottom) logContainer.scrollTop = logContainer.scrollHeight;

                    // Fetch chart history
                    const histRes = await fetch('/api/history');
                    updateChart(await histRes.json());
                } catch(e) { console.error("Update failed", e); }
            }

            function updateChart(data) {
                const ctx = document.getElementById('analyticsChart').getContext('2d');
                if(!chartInstance) {
                    chartInstance = new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: data.labels,
                            datasets: data.datasets
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            animation: false,
                            scales: {
                                y: { beginAtZero: false, grid: {color: 'rgba(255,255,255,0.05)'}, ticks: {color: '#8b949e'} },
                                x: { grid: {display: false}, ticks: {color: '#8b949e', maxTicksLimit: 10} }
                            },
                            plugins: {
                                legend: { labels: { color: '#c9d1d9' } }
                            }
                        }
                    });
                } else {
                    chartInstance.data.labels = data.labels;
                    chartInstance.data.datasets = data.datasets;
                    chartInstance.update();
                }
            }

            // --- Settings Editor Logic ---
            async function loadSettings() {
                const res = await fetch('/api/settings');
                const data = await res.json();
                const container = document.getElementById('slots-editor');
                container.innerHTML = '';
                data.slots.forEach(s => addSlotRow(s.id, s.label));
            }

            function addSlotRow(id = '', label = '') {
                const row = document.createElement('div');
                row.className = 'slot-row';
                row.innerHTML = `
                    <input type="number" placeholder="Slot ID (e.g. 1)" value="${id}" class="s-id">
                    <input type="text" placeholder="Label (e.g. A-1)" value="${label}" class="s-label">
                    <button class="btn btn-danger" onclick="this.parentElement.remove()">X</button>
                `;
                document.getElementById('slots-editor').appendChild(row);
            }

            async function saveSlots() {
                const rows = document.querySelectorAll('.slot-row');
                const newSlots = [];
                rows.forEach(r => {
                    const id = parseInt(r.querySelector('.s-id').value);
                    const label = r.querySelector('.s-label').value;
                    if(!isNaN(id) && label) newSlots.push({id, label});
                });
                
                await fetch('/api/settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({slots: newSlots})
                });
                alert('Saved! The bot will use these slots on its next poll.');
            }

            // Init loop
            updateDashboard();
            setInterval(updateDashboard, 5000);
        </script>
    </body>
    </html>
    """
    return html

@app.route("/api/stats")
@requires_auth
def api_stats():
    db = load_db()
    metrics = {
        "start_time": get_ist_now().isoformat(),
        "polls": 0,
        "latency": "0.00s",
        "total_courses": 0
    }
    if METRICS_FILE.exists():
        try:
            with open(METRICS_FILE, "r") as f:
                saved = json.load(f)
                metrics.update(saved)
        except Exception:
            pass
            
    try:
        start_time = datetime.datetime.fromisoformat(metrics["start_time"])
    except:
        start_time = get_ist_now()

    uptime_delta = get_ist_now() - start_time
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{uptime_delta.days}d {hours}h {minutes}m" if uptime_delta.days > 0 else f"{hours}h {minutes}m"
    
    return jsonify({
        "subscribers": len(db.get("approved", [])),
        "slots": len(db.get("slots", [])),
        "total_courses": metrics["total_courses"],
        "uptime": uptime_str,
        "polls": metrics["polls"],
        "latency": metrics["latency"],
        "time": f"{get_ist_now().strftime('%I:%M:%S %p')} IST"
    })
@app.route("/api/history")
@requires_auth
def api_history():
    # Provide Chart.js formatting from SQLite History
    db = load_db()
    active_slots = db.get("slots", [])
    
    conn = sqlite3.connect("history.db")
    c = conn.cursor()
    # Fetch last 50 distinct timestamps
    c.execute("SELECT DISTINCT timestamp FROM history ORDER BY timestamp DESC LIMIT 50")
    times = [r[0] for r in c.fetchall()][::-1] 
    
    datasets = []
    colors = ['#58a6ff', '#238636', '#d29922', '#8a2be2', '#da3633']
    
    for idx, slot in enumerate(active_slots):
        sid = slot["id"]
        color = colors[idx % len(colors)]
        
        c.execute("SELECT timestamp, course_count FROM history WHERE slot_id = ? ORDER BY timestamp DESC LIMIT 50", (sid,))
        # Map out the values against the standard times
        raw_data = {r[0]: r[1] for r in c.fetchall()}
        
        data_points = []
        last_val = 0
        for t in times:
            if t in raw_data:
                last_val = raw_data[t]
            data_points.append(last_val)
            
        datasets.append({
            "label": f"Slot {slot['label']} ({sid})",
            "data": data_points,
            "borderColor": color,
            "backgroundColor": color + "33",
            "borderWidth": 2,
            "pointRadius": 0,
            "fill": True,
            "tension": 0.4
        })
    conn.close()
    
    # Format labels cleanly (HH:MM)
    clean_labels = [datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S").strftime("%H:%M") for t in times]
    return jsonify({"labels": clean_labels, "datasets": datasets})

@app.route("/api/settings", methods=["GET", "POST"])
@requires_auth
def api_settings():
    db = load_db()
    if request.method == "POST":
        data = request.json
        if "slots" in data:
            db["slots"] = data["slots"]
            save_db(db)
            return jsonify({"status": "success"})
    return jsonify({"slots": db.get("slots", [])})

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

@app.route("/ping")
def ping():
    return "pong"


if __name__ == "__main__":
    import werkzeug.serving
    import logging
    
    log_werkzeug = logging.getLogger('werkzeug')
    log_werkzeug.setLevel(logging.ERROR)
    log_werkzeug.disabled = True

    class NoLogRequestHandler(werkzeug.serving.WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            pass
        def log(self, type, message, *args):
            pass

    port = int(os.environ.get("PORT", 8100))
    ip_addr = os.environ.get("IP", "0.0.0.0")
    print(f"  [Web] Attempting to start Flask WSGI on {ip_addr}:{port} (Silent HTTP mode)")
    werkzeug.serving.run_simple(
        ip_addr, port, app,
        use_reloader=False, 
        use_debugger=False,
        request_handler=NoLogRequestHandler
    )
