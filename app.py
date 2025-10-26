from flask import Flask, jsonify, request, render_template_string, abort
import yaml, os, time
from collections import defaultdict

app = Flask(__name__)

# === In-memory state (pod-local). For demo/offsite this is perfect. ===
PLAYERS = set()                 # registered names
SCORES = defaultdict(int)       # name -> total score
SUBMITTED = set()               # names who submitted current question
LAST_SUBMISSION_TS = {}         # throttle spam per name
PHASE = "lobby"                 # lobby | question | reveal | final
CURRENT_INDEX = -1              # -1 in lobby; 0..N-1 during quiz

def load_questions():
    path = os.environ.get("QUESTIONS_FILE", "/app/questions.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("questions", [])

def current_question():
    qs = load_questions()
    if 0 <= CURRENT_INDEX < len(qs):
        return qs[CURRENT_INDEX]
    return None

def winners():
    if not SCORES:
        return []
    max_score = max(SCORES.values())
    return [n for (n, s) in SCORES.items() if s == max_score], max_score

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")  # simple shared secret

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OpenShift Quiz</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 18px; }
    .container { max-width: 820px; margin: auto; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 12px 0; }
    h1 { margin: 0 0 8px 0; }
    .muted { color:#555; }
    input, button { padding: 10px 14px; border-radius:8px; border:1px solid #ccc; }
    button { border:0; cursor:pointer; }
    .primary { background:#ee0000; color:#fff; }
    .secondary { background:#f2f2f2; }
    .badge { display:inline-block; background:#222; color:#fff; padding:4px 10px; border-radius:16px; font-size:12px; margin-left:8px; }
    .ok { background:#198754; }
    .warn { background:#6c757d; }
    .grid { display:grid; gap:10px; }
    .qtitle { font-weight:600; margin-bottom:6px; }
    .leaderboard { font-variant-numeric:tabular-nums; }
    .winner { background: #fff3cd; border: 1px solid #ffe69c; padding: 8px; border-radius: 8px; }
  </style>
</head>
<body>
<div class="container">
  <h1>☸️ OpenShift Quiz</h1>
  <p class="muted">Register once, then answer questions one-by-one when the facilitator advances.</p>

  <div class="card">
    <div class="grid">
      <label>Team or Name:
        <input id="player" placeholder="Pods, Operators, Alice..." style="width:100%">
      </label>
      <div>
        <button class="secondary" onclick="register()">Register / Update name</button>
        <span id="regStatus" class="badge warn" style="display:none">Registered</span>
      </div>
    </div>
  </div>

  <div id="phaseCard" class="card"></div>

  <div id="questionCard" class="card" style="display:none"></div>

  <div id="leaderboard" class="card leaderboard" style="display:none"></div>

  <div class="card">
    <button class="secondary" onclick="refresh()">Refresh</button>
    <span class="muted">Auto-refreshes every 2s.</span>
  </div>

  <p class="muted">Facilitator controls at <code>/admin?token=YOUR_ADMIN_TOKEN</code>.</p>
</div>

<script>
let Q = null; // current question
let state = null;
let myName = localStorage.getItem('quiz_name') || '';

function el(id){ return document.getElementById(id); }
function val(id){ return el(id).value.trim(); }

async function register(){
  const name = val('player');
  if(!name){ alert('Please enter a name/team'); return; }
  const r = await fetch('/api/register',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  if(r.ok){
    localStorage.setItem('quiz_name', name);
    el('regStatus').style.display='inline-block';
    el('regStatus').textContent='Registered';
    el('regStatus').className='badge ok';
  }
}

async function loadState(){
  const r = await fetch('/api/state');
  state = await r.json();
  renderState();
}

function renderState(){
  const pc = el('phaseCard');
  const qc = el('questionCard');
  const lb = el('leaderboard');
  const name = val('player');

  if(!name && myName){ el('player').value = myName; }

  // Phase banner
  pc.innerHTML = `<strong>Phase:</strong> ${state.phase.toUpperCase()} ·
    Question ${state.current_index >= 0 ? state.current_index+1 : 0} / ${state.total_questions} ·
    Players: ${state.players_count} · Submissions: ${state.submissions_count}`;

  if(state.phase === 'lobby'){
    qc.style.display='none';
    lb.style.display='none';
    return;
  }

  if(state.phase === 'question'){
    // render current question (without answer)
    Q = state.question;
    if(Q){
      qc.style.display='block';
      const opts = Q.options.map((o,i)=>
        `<label style="display:block;margin:4px 0;">
          <input type="radio" name="opt" value="${i}"> ${o}
        </label>`).join('');
      qc.innerHTML = `
        <div class="qtitle">${state.current_index+1}. ${Q.text}</div>
        ${opts}
        <div style="margin-top:8px;">
          <button class="primary" onclick="submitAnswer()">Submit</button>
          <span id="submitStatus" class="badge warn" style="display:none"></span>
        </div>
        ${Q.note ? `<div class="muted" style="margin-top:8px;">💡 ${Q.note}</div>` : ''}
      `;
    }
    lb.style.display='none';
    return;
  }

  if(state.phase === 'reveal'){
    // show leaderboard after this question
    qc.style.display='none';
    loadLeaderboard(false);
    return;
  }

  if(state.phase === 'final'){
    qc.style.display='none';
    loadLeaderboard(true);
    return;
  }
}

async function submitAnswer(){
  const name = val('player') || myName;
  if(!name){ alert('Please register your name first.'); return; }
  const chosen = document.querySelector('input[name="opt"]:checked');
  const answer = chosen ? parseInt(chosen.value) : null;
  const r = await fetch('/api/submit', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name, answer })
  });
  const data = await r.json();
  const s = el('submitStatus');
  s.style.display='inline-block';
  s.className='badge ok';
  s.textContent = data.accepted ? `Saved! ${data.correct ? 'Correct ✅' : 'Submitted'}` : data.message || 'Already submitted';
}

async function loadLeaderboard(final){
  const r = await fetch('/api/leaderboard');
  const data = await r.json();
  const lb = el('leaderboard');
  lb.style.display='block';
  const winners = data.winners || [];
  const max = data.max_score ?? 0;
  lb.innerHTML = `<h3>${final ? 'Final ' : ''}Leaderboard</h3>` +
    data.rows.map((row, i) => {
      const cls = winners.includes(row.name) && final ? 'winner' : '';
      return `<div class="${cls}">${i+1}. <strong>${row.name}</strong> — ${row.score}${final && winners.includes(row.name) ? ' 🏆' : ''}</div>`;
    }).join('') +
    (final && winners.length ? `<div style="margin-top:10px;"><strong>Winner${winners.length>1?'s':''}:</strong> ${winners.join(', ')} (score ${max})</div>` : '');
}

function refresh(){ loadState(); }
setInterval(loadState, 2000);
loadState();
</script>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Quiz Admin</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 18px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 12px 0; }
    button { padding: 10px 14px; border: 0; border-radius: 8px; cursor: pointer; }
    .primary { background:#ee0000; color:#fff; }
    .secondary { background:#f2f2f2; }
    code { background:#f6f6f6; padding:2px 6px; border-radius:6px; }
  </style>
</head>
<body>
  <h2>☸️ Quiz Admin</h2>
  <div class="card" id="state"></div>
  <div class="card">
    <button class="primary" onclick="post('/api/admin/start')">Start</button>
    <button class="secondary" onclick="post('/api/admin/advance')">Advance (Reveal / Next / Final)</button>
    <button onclick="post('/api/admin/reset')">Reset</button>
  </div>
  <div class="card">
    <div>Leaderboard (project /api/leaderboard on screen)</div>
  </div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
async function post(url){
  const r = await fetch(url+'?token='+encodeURIComponent(token), {method:'POST'});
  const data = await r.json();
  await load();
  alert(data.message || 'OK');
}
async function load(){
  const r = await fetch('/api/state');
  const s = await r.json();
  document.getElementById('state').innerHTML =
    `<div><strong>Phase:</strong> ${s.phase.toUpperCase()} · Q ${s.current_index>=0?s.current_index+1:0}/${s.total_questions}</div>
     <div><strong>Players:</strong> ${s.players_count} · <strong>Submissions:</strong> ${s.submissions_count}</div>`;
}
setInterval(load, 2000);
load();
</script>
</body>
</html>
"""

# ----------------------- ROUTES -----------------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/admin")
def admin():
    # simple check: allow showing the page if a token query is provided; API endpoints still enforce token
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        return abort(403)
    return render_template_string(ADMIN_HTML)

@app.route("/api/register", methods=["POST"])
def api_register():
    global PLAYERS
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "name required"}), 400
    PLAYERS.add(name)
    SCORES[name] = SCORES[name]  # ensure key exists
    return jsonify({"ok": True})

@app.route("/api/state")
def api_state():
    qs = load_questions()
    q = current_question()
    # never send the correct answer to clients
    q_pub = None
    if q:
        q_pub = {"text": q.get("text"), "options": q.get("options", []), "note": q.get("note")}
    return jsonify({
        "phase": PHASE,
        "current_index": CURRENT_INDEX,
        "total_questions": len(qs),
        "players_count": len(PLAYERS),
        "submissions_count": len(SUBMITTED),
        "question": q_pub
    })

@app.route("/api/submit", methods=["POST"])
def api_submit():
    global SUBMITTED
    if PHASE != "question":
        return jsonify({"accepted": False, "message": "Not accepting answers now"}), 400
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    if not name or name not in PLAYERS:
        return jsonify({"accepted": False, "message": "Please register first"}), 400
    # throttle quick re-submits
    now = time.time()
    if name in LAST_SUBMISSION_TS and (now - LAST_SUBMISSION_TS[name] < 0.8):
        return jsonify({"accepted": False, "message": "Slow down"}), 429
    LAST_SUBMISSION_TS[name] = now
    if name in SUBMITTED:
        return jsonify({"accepted": False, "message": "Already submitted"}), 200

    # grade
    q = current_question()
    correct = False
    try:
        if payload.get("answer") == q["answer"]:
            correct = True
            SCORES[name] += 1
    except Exception:
        pass

    SUBMITTED.add(name)

    # auto-switch to reveal if all registered players have submitted at least once
    if len(PLAYERS) > 0 and len(SUBMITTED) >= len(PLAYERS):
        _advance_to_reveal()

    return jsonify({"accepted": True, "correct": correct})

@app.route("/api/leaderboard")
def api_leaderboard():
    rows = sorted(SCORES.items(), key=lambda kv: kv[1], reverse=True)
    win, mx = winners()
    return jsonify({"rows": [{"name": k, "score": v} for k, v in rows], "winners": win, "max_score": mx})

# ----------------- Admin controls (simple token) -----------------

def _require_admin():
    if ADMIN_TOKEN and request.args.get("token") == ADMIN_TOKEN:
        return
    if ADMIN_TOKEN:
        abort(403)

def _advance_to_question():
    global PHASE, CURRENT_INDEX, SUBMITTED
    PHASE = "question"
    SUBMITTED = set()

def _advance_to_reveal():
    global PHASE
    PHASE = "reveal"

def _advance_to_final():
    global PHASE
    PHASE = "final"

@app.route("/api/admin/start", methods=["POST"])
def api_admin_start():
    _require_admin()
    global PHASE, CURRENT_INDEX, SCORES, SUBMITTED
    qs = load_questions()
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    SUBMITTED = set()
    CURRENT_INDEX = 0 if len(qs) > 0 else -1
    PHASE = "question" if CURRENT_INDEX >= 0 else "final"
    return jsonify({"ok": True, "message": "Quiz started"})

@app.route("/api/admin/advance", methods=["POST"])
def api_admin_advance():
    _require_admin()
    global PHASE, CURRENT_INDEX, SUBMITTED
    qs = load_questions()
    if PHASE == "question":
        _advance_to_reveal()
        return jsonify({"ok": True, "message": "Revealing leaderboard"})
    elif PHASE == "reveal":
        if CURRENT_INDEX + 1 < len(qs):
            CURRENT_INDEX += 1
            _advance_to_question()
            return jsonify({"ok": True, "message": f"Next question ({CURRENT_INDEX+1}/{len(qs)})"})
        else:
            _advance_to_final()
            return jsonify({"ok": True, "message": "Quiz finished"})
    elif PHASE in ("lobby",):
        return jsonify({"ok": False, "message": "Start the quiz first"}), 400
    else:
        return jsonify({"ok": True, "message": "Already final"})

@app.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    _require_admin()
    global PHASE, CURRENT_INDEX, PLAYERS, SCORES, SUBMITTED, LAST_SUBMISSION_TS
    PHASE = "lobby"
    CURRENT_INDEX = -1
    SUBMITTED = set()
    LAST_SUBMISSION_TS = {}
    # keep PLAYERS if you want, or clear for a clean slate:
    # PLAYERS = set()
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    return jsonify({"ok": True, "message": "Reset to lobby"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
