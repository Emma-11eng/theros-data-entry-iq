# app.py
import sqlite3
from datetime import datetime, timedelta
from math import isnan
import json
import os

from flask import Flask, request, g, jsonify, send_file, Response

# Optional OpenAI; safe to import only if available
USE_OPENAI = False
if "OPENAI_API_KEY" in os.environ:
    try:
        import openai
        openai.api_key = os.environ["OPENAI_API_KEY"]
        USE_OPENAI = True
    except Exception:
        USE_OPENAI = False

DB_PATH = "data.db"
app = Flask(__name__, static_folder="static", static_url_path="/static")

# --- Database helpers
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        measured_at TEXT NOT NULL,
        resting_hr INTEGER,
        hrv REAL,
        respiratory_rate REAL,
        body_temp REAL,
        spo2 REAL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()

init_db()

# --- Utility functions
def parse_iso_date(s):
    # accepts YYYY-MM-DD or full ISO
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.strptime(s, "%Y-%m-%d")

def row_to_dict(r):
    return {k: r[k] for k in r.keys()}

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs)/len(xs) if xs else None

def pct_change(old, new):
    try:
        if old is None or old == 0:
            return None
        return (new - old) / abs(old) * 100.0
    except Exception:
        return None

def simple_trend(values):
    # Linear trend via simple slope estimate (day index)
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    n = len(values)
    xs = list(range(n))
    ys = [v if v is not None else 0 for v in values]
    x_mean = sum(xs)/n
    y_mean = sum(ys)/n
    num = sum((xs[i]-x_mean)*(ys[i]-y_mean) for i in range(n))
    den = sum((xs[i]-x_mean)**2 for i in range(n))
    if den == 0:
        return 0
    slope = num/den
    return slope

# --- API: create measurement
@app.route("/api/measurements", methods=["POST"])
def create_measurement():
    data = request.json or {}
    measured_at = data.get("measured_at") or datetime.utcnow().isoformat()
    # Accept numbers or null
    def n(key):
        v = data.get(key)
        return None if v in (None, "", "null") else float(v)
    resting_hr = data.get("resting_hr")
    try:
        resting_hr = None if resting_hr in (None, "") else int(resting_hr)
    except:
        resting_hr = None
    hrv = n("hrv")
    respiratory_rate = n("respiratory_rate")
    body_temp = n("body_temp")
    spo2 = n("spo2")
    notes = data.get("notes")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO measurements (measured_at, resting_hr, hrv, respiratory_rate, body_temp, spo2, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (measured_at, resting_hr, hrv, respiratory_rate, body_temp, spo2, notes))
    conn.commit()
    rowid = cur.lastrowid
    cur.execute("SELECT * FROM measurements WHERE id = ?", (rowid,))
    row = cur.fetchone()
    conn.close()
    return jsonify(row_to_dict(row)), 201

# --- API: list measurements (range optional)
@app.route("/api/measurements", methods=["GET"])
def list_measurements():
    since = request.args.get("since")  # YYYY-MM-DD
    until = request.args.get("until")
    conn = get_db()
    cur = conn.cursor()
    if since and until:
        cur.execute("SELECT * FROM measurements WHERE date(measured_at) BETWEEN ? AND ? ORDER BY measured_at ASC", (since, until))
    elif since:
        cur.execute("SELECT * FROM measurements WHERE date(measured_at) >= ? ORDER BY measured_at ASC", (since,))
    else:
        cur.execute("SELECT * FROM measurements ORDER BY measured_at ASC")
    rows = cur.fetchall()
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])

# --- Helper: compute structured summary for last N days
def compute_summary(days=7):
    conn = get_db()
    cur = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days-1)).date().isoformat()
    cur.execute("SELECT * FROM measurements WHERE date(measured_at) >= ? ORDER BY measured_at ASC", (cutoff,))
    rows = cur.fetchall()
    conn.close()
    items = [row_to_dict(r) for r in rows]

    # build time series (by day) using latest measurement per day
    by_day = {}
    for it in items:
        d = it["measured_at"][:10]
        by_day[d] = it  # keep last entry for a day (assumes ordered)

    # create ordered list of days
    days_list = []
    for i in range(days):
        d = (datetime.utcnow().date() - timedelta(days=days-1-i)).isoformat()
        days_list.append(d)

    series = {"date": [], "resting_hr": [], "hrv": [], "respiratory_rate": [], "body_temp": [], "spo2": []}
    for d in days_list:
        series["date"].append(d)
        it = by_day.get(d)
        if it:
            series["resting_hr"].append(it.get("resting_hr"))
            series["hrv"].append(it.get("hrv"))
            series["respiratory_rate"].append(it.get("respiratory_rate"))
            series["body_temp"].append(it.get("body_temp"))
            series["spo2"].append(it.get("spo2"))
        else:
            series["resting_hr"].append(None)
            series["hrv"].append(None)
            series["respiratory_rate"].append(None)
            series["body_temp"].append(None)
            series["spo2"].append(None)

    # compute simple stats
    summary = {
        "days": days,
        "dates": series["date"],
        "series": series,
        "stats": {}
    }
    for key in ["resting_hr", "hrv", "respiratory_rate", "body_temp", "spo2"]:
        vals = series[key]
        last = next((v for v in reversed(vals) if v is not None), None)
        avg = mean([v for v in vals if v is not None])
        first = next((v for v in vals if v is not None), None)
        change_pct = pct_change(first, last) if first is not None and last is not None else None
        slope = simple_trend(vals)
        summary["stats"][key] = {"first": first, "last": last, "avg": avg, "change_pct": change_pct, "slope": slope}

    # anomalies: simple threshold checks
    anomalies = []
    for idx, d in enumerate(series["date"]):
        # temp >= 38 => fever flag
        t = series["body_temp"][idx]
        if t is not None and t >= 38.0:
            anomalies.append(f"{d}: high temp {t}°C")
        s = series["spo2"][idx]
        if s is not None and s < 94:
            anomalies.append(f"{d}: low SpO₂ {s}%")

    summary["anomalies"] = anomalies
    return summary

# --- API: get insights (deterministic + optional LLM)
@app.route("/api/insights", methods=["GET"])
def get_insights():
    days = int(request.args.get("days", 7))
    summary = compute_summary(days=days)

    # Build a deterministic, brief insight
    parts = []
    stats = summary["stats"]
    # RHR
    r = stats["resting_hr"]
    if r["first"] is not None and r["last"] is not None:
        if r["change_pct"] is not None and abs(r["change_pct"]) >= 3:
            direction = "increased" if r["change_pct"] > 0 else "decreased"
            parts.append(f"Resting HR {direction} by {round(r['change_pct'],1)}% (avg {round(r['avg'],1)} bpm).")
    # HRV
    h = stats["hrv"]
    if h["first"] is not None and h["last"] is not None:
        if h["change_pct"] is not None and abs(h["change_pct"]) >= 6:
            direction = "increased" if h["change_pct"] > 0 else "decreased"
            parts.append(f"HRV {direction} by {round(h['change_pct'],1)}% (avg {round(h['avg'],1)} ms).")
    # SpO2 anomalies
    if summary["anomalies"]:
        parts.append("Anomalies: " + "; ".join(summary["anomalies"]))

    deterministic_insight = " ".join(parts) if parts else "No notable changes detected in the last period."

    # Optionally call OpenAI to rewrite/summarize (if key present)
    ai_insight = None
    if USE_OPENAI:
        try:
            # Simple prompt using deterministic summary
            prompt = (
                "You are a concise, non-diagnostic health assistant. "
                "Given the structured summary below, write a short (1-2 sentence) user-facing insight. "
                "Be neutral and avoid medical diagnosis. If there are alarming flags, say 'consider seeking medical advice'.\n\n"
                f"StructuredSummary:\n{json.dumps(summary)}\n\nInsight:"
            )
            resp = openai.Completion.create(
                engine="text-davinci-003",
                prompt=prompt,
                max_tokens=120,
                temperature=0.2,
                n=1,
            )
            ai_insight = resp.choices[0].text.strip()
        except Exception as e:
            ai_insight = None

    out = {
        "deterministic": deterministic_insight,
        "ai": ai_insight,
        "summary": summary
    }
    return jsonify(out)

# --- Serve the simple UI
@app.route("/", methods=["GET"])
def index():
    # Single-file HTML; static Chart.js from CDN
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Theros Data Entry IQ</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#2563eb">
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
  <meta name="mobile-web-app-capable" content="yes">
  <style>
    body { font-family: system-ui, -apple-system, Roboto, Arial, sans-serif; padding:18px; background:#f7fafc; margin:0;}
    .container { max-width:900px; margin:12px auto; }
    header { margin-bottom:12px; }
    h1 { font-size:28px; margin:0 0 8px 0; font-weight:700; }
    .card { background:white; padding:14px; border-radius:10px; box-shadow:0 6px 18px rgba(0,0,0,0.06); margin-bottom:14px; }
    label { display:block; font-size:13px; margin-bottom:6px; color:#333; }
    input[type="date"], input[type="time"], input[type="text"], input[type="number"], textarea, select {
      width:100%; padding:10px 12px; border:1px solid #e2e8f0; border-radius:8px; box-sizing:border-box; font-size:14px;
      background:white;
    }
    textarea { resize:vertical; min-height:70px; }
    .field { margin-bottom:12px; }
    .small { font-size:13px; color:#555; }
    button { background:#2563eb; color:white; border:none; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    .chart-wrap { padding:8px 0; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Theros Data Entry IQ</h1>
      <div class="small">Quickly record your vitals — install to save to home screen.</div>
    </header>

    <div class="card" id="entryCard">
      <form id="entryForm">
        <div class="field">
          <label for="measured_at">Date</label>
          <input id="measured_at" type="date" required />
        </div>

        <div class="field">
          <label for="measured_time">Time</label>
          <input id="measured_time" type="time" />
        </div>

        <div class="field">
          <label for="resting_hr">Resting HR (bpm)</label>
          <input id="resting_hr" type="number" />
        </div>

        <div class="field">
          <label for="hrv">HRV (ms)</label>
          <input id="hrv" type="number" step="0.1" />
        </div>

        <div class="field">
          <label for="respiratory_rate">Respiratory rate (breaths/min)</label>
          <input id="respiratory_rate" type="number" step="0.1" />
        </div>

        <div class="field">
          <label for="body_temp">Body temp (°C)</label>
          <input id="body_temp" type="number" step="0.01" />
        </div>

        <div class="field">
          <label for="spo2">SpO₂ %</label>
          <input id="spo2" type="number" step="0.1" />
        </div>

        <div class="field">
          <label for="notes">Notes</label>
          <textarea id="notes" rows="3" placeholder="Optional: symptoms, sleep, exercise..."></textarea>
        </div>

        <div style="display:flex; justify-content:flex-start; gap:12px; align-items:center;">
          <button type="submit">Save entry</button>
          <div id="msg" class="small"></div>
        </div>
      </form>
    </div>

    <div class="card chart-wrap">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h3 style="margin:0">Chart (last 14 days)</h3>
        <div><button id="reload">Reload</button></div>
      </div>
      <div style="margin-top:12px;">
        <canvas id="chart" height="200" style="width:100%"></canvas>
      </div>
    </div>

    <div class="card">
      <h3 style="margin-top:0">Insights (7 days)</h3>
      <div id="insight" class="small">Loading...</div>
    </div>
  </div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const form = document.getElementById('entryForm');
const msg = document.getElementById('msg');
const insightDiv = document.getElementById('insight');
const measuredAt = document.getElementById('measured_at');
const measuredTime = document.getElementById('measured_time');

// set default date/time
const now = new Date();
measuredAt.value = now.toISOString().slice(0,10);
measuredTime.value = now.toTimeString().slice(0,5);

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  msg.textContent = 'Saving...';
  const payload = {
    measured_at: measuredAt.value + (measuredTime.value ? 'T' + measuredTime.value : ''),
    resting_hr: document.getElementById('resting_hr').value,
    hrv: document.getElementById('hrv').value,
    respiratory_rate: document.getElementById('respiratory_rate').value,
    body_temp: document.getElementById('body_temp').value,
    spo2: document.getElementById('spo2').value,
    notes: document.getElementById('notes').value
  };
  try {
    const res = await fetch('/api/measurements', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('save failed');
    msg.textContent = 'Saved';
    setTimeout(()=>msg.textContent='', 1400);
    loadData();
  } catch(err){
    console.error(err);
    msg.textContent = 'Save error';
  }
});

// Chart setup
const ctx = document.getElementById('chart').getContext('2d');
let chart = null;

async function loadData(){
  const res = await fetch('/api/measurements?since=' + new Date(Date.now()-13*24*3600*1000).toISOString().slice(0,10));
  const rows = await res.json();
  // group by day (latest per day)
  const map = {};
  rows.forEach(r => { const d = r.measured_at.slice(0,10); map[d] = r; });
  const labels = [];
  const days = [];
  for (let i=13;i>=0;i--){
    const d = new Date(Date.now()-i*24*3600*1000);
    const key = d.toISOString().slice(0,10);
    labels.push(key);
    days.push(map[key] || {});
  }
  const hr = labels.map((l,i)=> days[i].resting_hr ?? null);
  const hrv = labels.map((l,i)=> days[i].hrv ?? null);
  const temp = labels.map((l,i)=> days[i].body_temp ?? null);
  const spo2 = labels.map((l,i)=> days[i].spo2 ?? null);

  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label:'Resting HR', data:hr, spanGaps:true, yAxisID:'A', tension:0.2 },
        { label:'HRV', data:hrv, spanGaps:true, yAxisID:'B', tension:0.2 },
        { label:'Temp (°C)', data:temp, spanGaps:true, yAxisID:'C', tension:0.2 },
        { label:'SpO₂', data:spo2, spanGaps:true, yAxisID:'D', tension:0.2 }
      ]
    },
    options: {
      interaction: {mode:'index', intersect:false},
      scales: {
        A: { type:'linear', position:'left', title:{display:true, text:'bpm'} },
        B: { type:'linear', position:'right', title:{display:true, text:'ms'}, grid:{display:false} },
        C: { type:'linear', position:'right', title:{display:true, text:'°C'}, grid:{display:false}, offset:true },
        D: { type:'linear', position:'right', title:{display:true, text:'%'}, grid:{display:false}, offset:true }
      },
      plugins: { legend:{display:true} }
    }
  });

  // load insights
  const insRes = await fetch('/api/insights?days=7');
  const insJson = await insRes.json();
  insightDiv.textContent = insJson.ai || insJson.deterministic || 'No insight';
}

document.getElementById('reload').addEventListener('click', loadData);
loadData();
</script>

<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js')
    .then(reg => console.log('SW registered', reg.scope))
    .catch(err => console.warn('SW failed', err));
}
</script>
</body>
</html>
    """
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    app.run(debug=True, port=5000)
