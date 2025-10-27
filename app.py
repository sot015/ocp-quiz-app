from flask import Flask, jsonify, request, render_template_string, abort, redirect, url_for, session
from werkzeug.middleware.proxy_fix import ProxyFix
import yaml, os, time, re
from collections import defaultdict

app = Flask(__name__)

# Trust OpenShift router proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# === In-memory state (pod-local). ===
PLAYERS = set()                 # canonical display names
NAME_INDEX = {}                 # lowercased -> canonical
SCORES = defaultdict(int)       # canonical -> score (cumulative)
SUBMITTED = set()               # canonical submitted at least once for current question
CURRENT_ANSWERS = {}            # canonical -> last selected option index (or None) for current question
LAST_SUBMISSION_TS = {}         # canonical -> float
PHASE = "lobby"                 # lobby | question | answer | reveal | final
CURRENT_INDEX = -1              # -1 in lobby; 0..N-1 during quiz

# Score-once guard
LAST_SCORED_INDEX = -1

# Leaderboard snapshot (only updated on reveal/final)
LB_ROWS_SNAPSHOT = []           # [{"name":..., "score":...}, ...]
LB_WINNERS_SNAPSHOT = []        # [names]
LB_MAX_SNAPSHOT = 0

# Quiz session id (changes on Start/Reset) to prevent client auto-select carryover
QUIZ_SESSION = str(int(time.time()))

# Admin auth (session-based)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# ----------------------- helpers -----------------------

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

def winners_from_scores():
    if not SCORES:
        return [], 0
    mx = max(SCORES.values())
    return [n for (n, s) in SCORES.items() if s == mx], mx

_ws_collapse = re.compile(r"\s+")
def normalize_name(name: str) -> str:
    name = (name or "").strip()
    name = _ws_collapse.sub(" ", name)
    if not name or len(name) > 40:
        return ""
    return name

def ensure_unique_on_register(requested_name: str):
    nm = normalize_name(requested_name)
    if not nm:
        return False, "Name required"
    lower = nm.casefold()
    if lower in NAME_INDEX:
        return False, "That name is already taken. Please pick a different name."
    return True, nm

def bump_session():
    global QUIZ_SESSION
    QUIZ_SESSION = str(int(time.time()*1000))

def score_current_question_once():
    """Award +1 to players whose *last* submitted answer matches the correct answer.
       Runs exactly once per question (on transition to 'answer')."""
    global LAST_SCORED_INDEX
    if CURRENT_INDEX == -1 or CURRENT_INDEX == LAST_SCORED_INDEX:
        return
    q = current_question()
    if not q:
        LAST_SCORED_INDEX = CURRENT_INDEX
        return
    correct_idx = q.get("answer", None)
    if correct_idx is None:
        LAST_SCORED_INDEX = CURRENT_INDEX
        return
    for name in PLAYERS:
        ans = CURRENT_ANSWERS.get(name, None)
        if ans == correct_idx:
            SCORES[name] += 1
    LAST_SCORED_INDEX = CURRENT_INDEX

def snapshot_leaderboard():
    """Create a snapshot of the leaderboard (used only in reveal/final)."""
    global LB_ROWS_SNAPSHOT, LB_WINNERS_SNAPSHOT, LB_MAX_SNAPSHOT
    rows = sorted(SCORES.items(), key=lambda kv: kv[1], reverse=True)
    LB_ROWS_SNAPSHOT = [{"name": k, "score": v} for k, v in rows]
    LB_WINNERS_SNAPSHOT, LB_MAX_SNAPSHOT = winners_from_scores()

def clear_leaderboard_snapshot():
    global LB_ROWS_SNAPSHOT, LB_WINNERS_SNAPSHOT, LB_MAX_SNAPSHOT
    LB_ROWS_SNAPSHOT = []
    LB_WINNERS_SNAPSHOT = []
    LB_MAX_SNAPSHOT = 0

def admin_logged_in():
    return session.get('is_admin') is True

def _require_admin():
    if not admin_logged_in():
        abort(403)

# ----------------------- HTML (user) -----------------------

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
    .error { color:#b00020; margin-left:10px; font-size: 90%; }
    .user-choice { outline: 2px solid #0d6efd; border-radius: 6px; padding: 2px 6px; }
    .disabled-note { font-size: 90%; color:#555; margin-left:8px; }
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
        <button id="regBtn" class="secondary" onclick="register()">Register / Update name</button>
        <span id="regStatus" class="badge warn" style="display:none">Registered</span>
        <span id="regError" class="error"></span>
        <span id="nameLocked" class="disabled-note" style="display:none">Name locked (quiz in progress)</span>
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

  <p class="muted">Facilitator controls at <code>/admin</code>.</p>
</div>

<script>
let state = null;
let myName = localStorage.getItem('quiz_name') || '';
let lastRenderKey = ""; // session:phase:index
let currentSession = null;
let lastSession = localStorage.getItem('quiz_session') || null;

function el(id){ return document.getElementById(id); }
function val(id){ return el(id).value.trim(); }

function setNameEditable(editable){
  el('player').disabled = !editable;
  el('regBtn').disabled = !editable;
  el('nameLocked').style.display = editable ? 'none' : 'inline';
}

async function register(){
  const name = val('player');
  el('regError').textContent = '';
  const prev = localStorage.getItem('quiz_name') || null; // send previous for rename support
  const r = await fetch('/api/register',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name, prev})
  });
  const data = await r.json().catch(()=>({}));
  if(r.ok && data.ok){
    localStorage.setItem('quiz_name', name);
    myName = name;
    const badge = el('regStatus');
    badge.style.display='inline-block';
    badge.className='badge ok';
    badge.textContent='Registered';
  }else{
    el('regError').textContent = (data && data.message) ? data.message : 'Registration failed';
  }
}

function lsKeyFor(session, idx){ return 'ans_'+session+'_'+idx; }

async function loadState(){
  const r = await fetch('/api/state');
  state = await r.json();
  // Session switch handling
  currentSession = state.session;
  if(currentSession && currentSession !== lastSession){
    localStorage.setItem('quiz_session', currentSession);
    lastSession = currentSession;
    lastRenderKey = ""; // force re-render
  }
  renderState();
}

function renderQuestion(readonly){
  const qc = el('questionCard');
  const Q = state.question;
  if(!Q){ qc.style.display='none'; return; }
  qc.style.display='block';

  const savedAns = currentSession ? localStorage.getItem(lsKeyFor(currentSession, state.current_index)) : null;
  const savedIdx = savedAns !== null ? parseInt(savedAns) : null;

  const opts = Q.options.map((o,i)=>{
    const userCls = (savedIdx !== null && savedIdx === i) ? 'user-choice' : '';
    const disabled = readonly ? 'disabled' : '';
    return `<label class="${userCls}" style="display:block;margin:4px 0;">
              <input type="radio" name="opt" value="${i}" ${disabled}> ${o}
            </label>`;
  }).join('');

  qc.innerHTML = `
    <div class="qtitle">${state.current_index+1}. ${Q.text}</div>
    ${opts}
    <div style="margin-top:8px;">
      ${readonly ? '' : '<button class="primary" onclick="submitAnswer()">Submit</button>'}
      <span id="submitStatus" class="badge warn" style="display:none"></span>
    </div>
    ${Q.note ? `<div class="muted" style="margin-top:8px;">💡 ${Q.note}</div>` : ''}
  `;

  // Ensure no default selection; restore only user's own choice in question phase
  Array.from(qc.querySelectorAll('input[name="opt"]')).forEach(r => { r.checked = false; });
  if(!readonly && savedIdx !== null){
    const toCheck = qc.querySelector('input[name="opt"][value="'+savedIdx+'"]');
    if(toCheck) toCheck.checked = true;
  }
}

function renderState(){
  const pc = el('phaseCard');
  const qc = el('questionCard');
  const lb = el('leaderboard');

  if(!val('player') && myName){ el('player').value = myName; }

  pc.innerHTML = `<strong>Phase:</strong> ${state.phase.toUpperCase()} ·
    Question ${state.current_index >= 0 ? state.current_index+1 : 0} / ${state.total_questions} ·
    Players: ${state.players_count} · Submissions: ${state.submissions_count}`;

  // NEW: toggle name editability only in lobby
  setNameEditable(state.phase === 'lobby');

  const currentKey = `${state.session}:${state.phase}:${state.current_index}`;

  if(state.phase === 'lobby'){
    qc.style.display='none';
    lb.style.display='none';
    lastRenderKey = currentKey;
    return;
  }

  if(state.phase === 'question'){
    if(currentKey !== lastRenderKey){
      renderQuestion(false);
      lb.style.display='none';
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'answer'){
    if(currentKey !== lastRenderKey){
      renderQuestion(true);
      lb.style.display='none';
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'reveal'){
    if(currentKey !== lastRenderKey){
      qc.style.display='none';
      loadLeaderboard(false);
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'final'){
    if(currentKey !== lastRenderKey){
      qc.style.display='none';
      loadLeaderboard(true);
      lastRenderKey = currentKey;
    }
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
  if(r.ok && data.accepted){
    if(chosen && currentSession){
      localStorage.setItem(lsKeyFor(currentSession, state.current_index), String(answer));
    }
    s.style.display='inline-block';
    s.className='badge warn';
    s.textContent = 'Saved';
    renderQuestion(false);
  } else {
    s.style.display='inline-block';
    s.className='badge warn';
    s.textContent = data.message || 'Not accepted';
  }
}

async function loadLeaderboard(final){
  const r = await fetch('/api/leaderboard');
  const data = await r.json();
  const lb = el('leaderboard');
  lb.style.display='block';
  const winners = data.winners || [];
  const max = data.max_score ?? 0;
  lb.innerHTML = `<h3>${final ? 'Final ' : ''}Leaderboard</h3>` +
    (data.rows || []).map((row, i) => {
      const cls = winners.includes(row.name) && final ? 'winner' : '';
      return `<div class="${cls}">${i+1}. <strong>${row.name}</strong> — ${row.score}${final && winners.includes(row.name) ? ' 🏆' : ''}</div>`;
    }).join('') || '<em>No scores yet.</em>';
}

function refresh(){ loadState(); }
setInterval(loadState, 2000);
loadState();
</script>
</body>
</html>
"""

# ----------------------- HTML (admin) -----------------------

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
    .correct { outline: 2px solid #198754; border-radius: 6px; padding: 2px 6px; }
    .status { margin-top:8px; font-size: 90%; color:#333; }
    .muted { color:#555; }
    a.small { font-size: 90%; margin-left: 8px; }
  </style>
</head>
<body>
  <h2>☸️ Quiz Admin</h2>

  <div class="card" id="state"></div>

  <div class="card">
    <button class="primary" onclick="post('/api/admin/start')">Start</button>
    <button class="secondary" onclick="post('/api/admin/advance')">Advance</button>
    <button onclick="post('/api/admin/reset')">Reset</button>
    <a class="small" href="/admin/logout">Logout</a>
    <div id="status" class="status muted">Ready.</div>
  </div>

  <div class="card" id="adminQuestion"></div>

  <div class="card">
    <div>Leaderboard</div>
    <div id="adminLeaderboard" class="muted">Waiting…</div>
  </div>

<script>
let adminPhase = 'lobby';
let adminSession = null;

async function post(url){
  const statusEl = document.getElementById('status');
  try{
    const r = await fetch(url, {method:'POST'});
    const data = await r.json();
    await loadEverything();
    statusEl.textContent = (data && data.message) ? data.message : (r.ok ? 'OK' : 'Error');
  }catch(e){
    statusEl.textContent = 'Request failed.';
  }
}

async function loadEverything(){
  await Promise.all([loadState(), loadAdminState(), maybeLoadLeaderboard()]);
}

async function loadState(){
  const r = await fetch('/api/state');
  const s = await r.json();
  adminPhase = s.phase;
  adminSession = s.session;
  document.getElementById('state').innerHTML =
    `<div><strong>Session:</strong> ${adminSession} · <strong>Phase:</strong> ${s.phase.toUpperCase()} · Q ${s.current_index>=0?s.current_index+1:0}/${s.total_questions}</div>
     <div><strong>Players:</strong> ${s.players_count} · <strong>Submissions:</strong> ${s.submissions_count}</div>`;
  if(!(adminPhase === 'reveal' || adminPhase === 'final')){
    document.getElementById('adminLeaderboard').innerHTML = '<em>Waiting for reveal…</em>';
  }
}

async function loadAdminState(){
  const r = await fetch('/api/admin_state');
  if(!r.ok){ document.getElementById('adminQuestion').innerHTML = '<em>Not authorized or unavailable.</em>'; return; }
  const a = await r.json();
  const q = a.question;
  if(!q){ document.getElementById('adminQuestion').innerHTML = '<em>No question loaded.</em>'; return; }

  let opts = q.options.map((o,i)=>{
    const cls = (a.phase === 'answer' && a.correct_answer_index === i) ? 'correct' : '';
    return `<div class="${cls}" style="margin:4px 0;">${i+1}. ${o}</div>`;
  }).join('');

  document.getElementById('adminQuestion').innerHTML =
    `<div><strong>Question ${a.current_index+1}:</strong> ${q.text}</div>
     ${q.note ? `<div class="muted" style="margin-top:4px;">💡 ${q.note}</div>` : ''}
     <div style="margin-top:8px;">${opts}</div>`;
}

async function maybeLoadLeaderboard(){
  if(!(adminPhase === 'reveal' || adminPhase === 'final')) return;
  const r = await fetch('/api/leaderboard');
  const data = await r.json();
  const winners = data.winners || [];
  const max = data.max_score ?? 0;
  document.getElementById('adminLeaderboard').innerHTML =
    (data.rows && data.rows.length ? data.rows : []).map((row, i) => {
      const crown = (adminPhase === 'final' && winners.includes(row.name)) ? ' 🏆' : '';
      return `<div>${i+1}. <strong>${row.name}</strong> — ${row.score}${crown}</div>`;
    }).join('') || '<em>No scores yet.</em>';
}

setInterval(loadEverything, 2000);
loadEverything();
</script>
</body>
</html>
"""

# ----------------------- HTML (admin login) -----------------------

LOGIN_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Admin Login</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 48px; }
    .box { max-width: 380px; margin: 0 auto; border: 1px solid #ddd; border-radius: 10px; padding: 18px; }
    .box * { box-sizing: border-box; } /* ensure inputs don't overflow card */
    input, button { padding: 10px 14px; border-radius:8px; border:1px solid #ccc; width:100%; display:block; }
    button { border:0; cursor:pointer; background:#ee0000; color:#fff; margin-top:10px; }
    .err { color:#b00020; margin-top:8px; font-size: 90%; }
    h2 { margin: 0 0 10px 0; }
  </style>
</head>
<body>
  <div class="box">
    <h2>Admin Login</h2>
    <form method="post">
      <input type="password" name="password" placeholder="Password">
      <button type="submit">Login</button>
      {% if error %}<div class="err">{{ error }}</div>{% endif %}
    </form>
  </div>
</body>
</html>
"""

# ----------------------- ROUTES -----------------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/admin")
def admin():
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    return render_template_string(ADMIN_HTML)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if ADMIN_PASSWORD and pw == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for("admin"))
        return render_template_string(LOGIN_HTML, error="Invalid password")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/api/register", methods=["POST"])
def api_register():
    """
    Register or RENAME a unique player (case-insensitive).
    - While in LOBBY: allow rename from 'prev' -> 'name' if unique.
    - After start: block renames; allow registering same name (no-op) but UI prevents changes anyway.
    """
    global PLAYERS, NAME_INDEX, SCORES, SUBMITTED, CURRENT_ANSWERS, LAST_SUBMISSION_TS
    payload = request.get_json(force=True)
    requested = (payload.get("name") or "")
    prev = (payload.get("prev") or "").strip() or None

    # Normalize targets
    new_norm = normalize_name(requested)
    if not new_norm:
        return jsonify({"ok": False, "message": "Name required"}), 400
    new_lower = new_norm.casefold()

    # If prev provided & exists -> possible rename flow
    if prev:
        prev_norm = normalize_name(prev)
        prev_lower = prev_norm.casefold() if prev_norm else ""
        prev_exists = prev_lower in NAME_INDEX

        # If it's actually the same name (case-insensitive), just ack
        if prev_exists and prev_lower == new_lower:
            return jsonify({"ok": True})

        # Renaming allowed only before quiz starts
        if PHASE != "lobby":
            return jsonify({"ok": False, "message": "Quiz already started — name changes are locked."}), 400

        # Ensure the new name is free (cannot collide with someone else)
        if new_lower in NAME_INDEX:
            return jsonify({"ok": False, "message": "That name is already taken. Please pick a different name."}), 400

        # If prev exists, migrate state to new canonical name
        if prev_exists:
            old_canon = NAME_INDEX.pop(prev_lower)
            if old_canon in PLAYERS:
                PLAYERS.remove(old_canon)
            old_score = SCORES.pop(old_canon, 0)

            PLAYERS.add(new_norm)
            NAME_INDEX[new_lower] = new_norm
            SCORES[new_norm] = old_score

            SUBMITTED.discard(old_canon)
            CURRENT_ANSWERS.pop(old_canon, None)
            LAST_SUBMISSION_TS.pop(old_canon, None)

            return jsonify({"ok": True})
        # If prev not found, fall through to "new registration" logic below.

    # New registration (or updating same name with no prev)
    if new_lower in NAME_INDEX:
        return jsonify({"ok": True})  # no-op if same name already present
    PLAYERS.add(new_norm)
    NAME_INDEX[new_lower] = new_norm
    SCORES[new_norm] = SCORES[new_norm]
    return jsonify({"ok": True})

@app.route("/api/state")
def api_state():
    # Public state for participants (NO CORRECT ANSWER)
    qs = load_questions()
    q = current_question()
    q_pub = None
    if q:
        q_pub = {"text": q.get("text"), "options": q.get("options", []), "note": q.get("note")}
    return jsonify({
        "session": QUIZ_SESSION,
        "phase": PHASE,
        "current_index": CURRENT_INDEX,
        "total_questions": len(qs),
        "players_count": len(PLAYERS),
        "submissions_count": len(SUBMITTED),
        "question": q_pub
    })

@app.route("/api/admin_state")
def api_admin_state():
    _require_admin()
    qs = load_questions()
    q = current_question()
    q_pub = None
    correct_index = None
    if q:
        q_pub = {"text": q.get("text"), "options": q.get("options", []), "note": q.get("note")}
        try:
            correct_index = int(q.get("answer"))
        except Exception:
            correct_index = None
    return jsonify({
        "session": QUIZ_SESSION,
        "phase": PHASE,
        "current_index": CURRENT_INDEX,
        "total_questions": len(qs),
        "players_count": len(PLAYERS),
        "submissions_count": len(SUBMITTED),
        "question": q_pub,
        "correct_answer_index": correct_index
    })

@app.route("/api/submit", methods=["POST"])
def api_submit():
    """
    Accept answers during QUESTION phase.
    Users may resubmit; we keep the last answer.
    Scoring is deferred to transition to ANSWER.
    """
    global SUBMITTED, CURRENT_ANSWERS
    if PHASE != "question":
        return jsonify({"accepted": False, "message": "Not accepting answers now"}), 400

    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    lower = normalize_name(name).casefold() if name else ""
    if lower not in NAME_INDEX:
        return jsonify({"accepted": False, "message": "Please register first"}), 400
    canonical = NAME_INDEX[lower]

    now = time.time()
    if canonical in LAST_SUBMISSION_TS and (now - LAST_SUBMISSION_TS[canonical] < 0.3):
        return jsonify({"accepted": False, "message": "Slow down"}), 429
    LAST_SUBMISSION_TS[canonical] = now

    answer = payload.get("answer", None)
    CURRENT_ANSWERS[canonical] = answer

    SUBMITTED.add(canonical)

    if len(PLAYERS) > 0 and len(SUBMITTED) >= len(PLAYERS):
        _advance_to_answer()

    return jsonify({"accepted": True})

@app.route("/api/leaderboard")
def api_leaderboard():
    """Returns the leaderboard SNAPSHOT (only refreshed in reveal/final)."""
    return jsonify({
        "rows": LB_ROWS_SNAPSHOT,
        "winners": LB_WINNERS_SNAPSHOT,
        "max_score": LB_MAX_SNAPSHOT
    })

# ----------------- Admin controls -----------------

def _advance_to_question():
    global PHASE, SUBMITTED, CURRENT_ANSWERS
    PHASE = "question"
    SUBMITTED = set()
    CURRENT_ANSWERS = {}

def _advance_to_answer():
    global PHASE
    score_current_question_once()
    PHASE = "answer"

def _advance_to_reveal():
    global PHASE
    PHASE = "reveal"
    snapshot_leaderboard()

def _advance_to_final():
    global PHASE
    PHASE = "final"
    snapshot_leaderboard()

@app.route("/api/admin/start", methods=["POST"])
def api_admin_start():
    _require_admin()
    global PHASE, CURRENT_INDEX, SCORES, SUBMITTED, CURRENT_ANSWERS, LAST_SCORED_INDEX
    clear_leaderboard_snapshot()
    bump_session()  # new session on every start
    qs = load_questions()
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    SUBMITTED = set()
    CURRENT_ANSWERS = {}
    LAST_SCORED_INDEX = -1
    CURRENT_INDEX = 0 if len(qs) > 0 else -1
    PHASE = "question" if CURRENT_INDEX >= 0 else "final"
    if PHASE == "final":
        snapshot_leaderboard()
    return jsonify({"ok": True, "message": "Quiz started"})

@app.route("/api/admin/advance", methods=["POST"])
def api_admin_advance():
    _require_admin()
    global PHASE, CURRENT_INDEX
    qs = load_questions()
    if PHASE == "question":
        _advance_to_answer()
        return jsonify({"ok": True, "message": "Showing correct answer"})
    elif PHASE == "answer":
        _advance_to_reveal()
        return jsonify({"ok": True, "message": "Showing leaderboard"})
    elif PHASE == "reveal":
        if CURRENT_INDEX + 1 < len(qs):
            CURRENT_INDEX += 1
            _advance_to_question()
            return jsonify({"ok": True, "message": f"Next question ({CURRENT_INDEX+1}/{len(qs)})"})
        else:
            _advance_to_final()
            return jsonify({"ok": True, "message": "Quiz finished"})
    elif PHASE == "lobby":
        return jsonify({"ok": False, "message": "Start the quiz first"}), 400
    else:
        return jsonify({"ok": True, "message": "Already final"})

@app.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    """Hard reset: requires users to register again and starts a new session."""
    _require_admin()
    global PHASE, CURRENT_INDEX, PLAYERS, NAME_INDEX, SCORES, SUBMITTED, CURRENT_ANSWERS
    global LAST_SUBMISSION_TS, LAST_SCORED_INDEX
    PHASE = "lobby"
    CURRENT_INDEX = -1
    PLAYERS = set()
    NAME_INDEX = {}
    SCORES = defaultdict(int)
    SUBMITTED = set()
    CURRENT_ANSWERS = {}
    LAST_SUBMISSION_TS = {}
    LAST_SCORED_INDEX = -1
    clear_leaderboard_snapshot()
    bump_session()  # fresh session on reset
    return jsonify({"ok": True, "message": "Hard reset complete — players must register again"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
