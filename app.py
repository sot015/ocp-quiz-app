from flask import Flask, jsonify, request, render_template_string, abort
import yaml, os, time, re
from collections import defaultdict

app = Flask(__name__)

# === In-memory state (pod-local). ===
PLAYERS = set()                 # canonical display names
NAME_INDEX = {}                 # lowercased -> canonical
SCORES = defaultdict(int)       # canonical -> score
SUBMITTED = set()               # canonical submitted this question
CURRENT_ANSWERS = {}            # canonical -> selected option index (or None)
LAST_SUBMISSION_TS = {}         # canonical -> float
PHASE = "lobby"                 # lobby | question | answer | reveal | final
CURRENT_INDEX = -1              # -1 in lobby; 0..N-1 during quiz

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

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

def winners():
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

def to_canonical(submitted_name: str):
    name = normalize_name(submitted_name)
    if not name:
        return False, "Invalid name"
    lower = name.casefold()
    if lower in NAME_INDEX:
        return True, NAME_INDEX[lower]
    return False, "Please register first"

def ensure_unique_on_register(requested_name: str):
    nm = normalize_name(requested_name)
    if not nm:
        return False, "Name required"
    lower = nm.casefold()
    if lower in NAME_INDEX:
        return False, "That name is already taken. Please pick a different name."
    return True, nm

# ----------------------- HTML -----------------------

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
    .disabled { opacity: 0.7; pointer-events: none; }
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
        <span id="regError" class="error"></span>
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
let state = null;
let myName = localStorage.getItem('quiz_name') || '';
let lastRenderKey = ""; // phase:index

function el(id){ return document.getElementById(id); }
function val(id){ return el(id).value.trim(); }

async function register(){
  const name = val('player');
  el('regError').textContent = '';
  const r = await fetch('/api/register',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const data = await r.json().catch(()=>({}));
  if(r.ok && data.ok){
    localStorage.setItem('quiz_name', name);
    const badge = el('regStatus');
    badge.style.display='inline-block';
    badge.className='badge ok';
    badge.textContent='Registered';
  }else{
    el('regError').textContent = (data && data.message) ? data.message : 'Registration failed';
  }
}

function lsKeyFor(idx){ return 'ans_q_'+idx; }

async function loadState(){
  const r = await fetch('/api/state');
  state = await r.json();
  renderState();
}

function renderQuestion(readonly){
  const qc = el('questionCard');
  const Q = state.question;
  if(!Q){ qc.style.display='none'; return; }
  qc.style.display='block';

  // user's previously submitted answer for this question (from localStorage)
  const savedAns = localStorage.getItem(lsKeyFor(state.current_index));
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

  // Nothing selected by default; explicitly ensure radios start unchecked.
  Array.from(qc.querySelectorAll('input[name="opt"]')).forEach(r => { r.checked = false; });
  // If user had an answer and we're still in question phase, pre-check it for convenience
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

  const currentKey = `${state.phase}:${state.current_index}`;

  if(state.phase === 'lobby'){
    qc.style.display='none';
    lb.style.display='none';
    lastRenderKey = currentKey;
    return;
  }

  if(state.phase === 'question'){
    if(currentKey !== lastRenderKey){
      renderQuestion(false); // answerable
      lb.style.display='none';
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'answer'){ // show user's own answer highlighted, inputs locked
    if(currentKey !== lastRenderKey){
      renderQuestion(true);
      lb.style.display='none';
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'reveal'){ // leaderboard
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
  // No "correct!" message — if accepted, either show neutral Saved (if wrong) or nothing (if correct)
  if(r.ok && data.accepted){
    // persist my answer for highlight across phases/page refresh
    if(chosen){ localStorage.setItem(lsKeyFor(state.current_index), String(answer)); }
    if(data.correct){
      s.style.display='none';
    }else{
      s.style.display='inline-block';
      s.className='badge warn';
      s.textContent = 'Saved';
    }
    // re-render to apply highlight class immediately
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
    .correct { outline: 2px solid #198754; border-radius: 6px; padding: 2px 6px; }
    .status { margin-top:8px; font-size: 90%; color:#333; }
    .muted { color:#555; }
  </style>
</head>
<body>
  <h2>☸️ Quiz Admin</h2>

  <div class="card" id="state"></div>

  <div class="card">
    <button class="primary" onclick="post('/api/admin/start')">Start</button>
    <button class="secondary" onclick="post('/api/admin/advance')">Advance</button>
    <button onclick="post('/api/admin/reset')">Reset</button>
    <div id="status" class="status muted">Ready.</div>
  </div>

  <div class="card" id="adminQuestion"></div>

  <div class="card">
    <div>Leaderboard (use below for reveal/final)</div>
    <div id="adminLeaderboard" class="muted">Waiting…</div>
  </div>

<script>
const token = new URLSearchParams(location.search).get('token') || '';

async function post(url){
  const statusEl = document.getElementById('status');
  try{
    const r = await fetch(url+'?token='+encodeURIComponent(token), {method:'POST'});
    const data = await r.json();
    await loadEverything();
    statusEl.textContent = (data && data.message) ? data.message : (r.ok ? 'OK' : 'Error');
  }catch(e){
    statusEl.textContent = 'Request failed.';
  }
}

async function loadEverything(){
  await Promise.all([loadState(), loadAdminState(), loadLeaderboard()]);
}

async function loadState(){
  const r = await fetch('/api/state');
  const s = await r.json();
  document.getElementById('state').innerHTML =
    `<div><strong>Phase:</strong> ${s.phase.toUpperCase()} · Q ${s.current_index>=0?s.current_index+1:0}/${s.total_questions}</div>
     <div><strong>Players:</strong> ${s.players_count} · <strong>Submissions:</strong> ${s.submissions_count}</div>`;
}

async function loadAdminState(){
  const r = await fetch('/api/admin_state?token='+encodeURIComponent(token));
  if(!r.ok){ document.getElementById('adminQuestion').innerHTML = '<em>Not authorized or unavailable.</em>'; return; }
  const a = await r.json();
  const q = a.question;
  if(!q){ document.getElementById('adminQuestion').innerHTML = '<em>No question loaded.</em>'; return; }

  // Show question and mark correct answer ONLY during ANSWER phase
  let opts = q.options.map((o,i)=>{
    const cls = (a.phase === 'answer' && a.correct_answer_index === i) ? 'correct' : '';
    return `<div class="${cls}" style="margin:4px 0;">${i+1}. ${o}</div>`;
  }).join('');

  document.getElementById('adminQuestion').innerHTML =
    `<div><strong>Question ${a.current_index+1}:</strong> ${q.text}</div>
     ${q.note ? `<div class="muted" style="margin-top:4px;">💡 ${q.note}</div>` : ''}
     <div style="margin-top:8px;">${opts}</div>`;
}

async function loadLeaderboard(){
  const r = await fetch('/api/leaderboard');
  const data = await r.json();
  const winners = data.winners || [];
  const max = data.max_score ?? 0;
  document.getElementById('adminLeaderboard').innerHTML =
    data.rows.map((row, i) => {
      const crown = (winners.includes(row.name) && (max>0) && document.getElementById('state').innerText.includes('FINAL')) ? ' 🏆' : '';
      return `<div>${i+1}. <strong>${row.name}</strong> — ${row.score}${crown}</div>`;
    }).join('') || '<em>No scores yet.</em>';
}

setInterval(loadEverything, 2000);
loadEverything();
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
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        return abort(403)
    return render_template_string(ADMIN_HTML)

@app.route("/api/register", methods=["POST"])
def api_register():
    """Register a unique player name (case-insensitive)."""
    global PLAYERS, NAME_INDEX, SCORES
    payload = request.get_json(force=True)
    requested = (payload.get("name") or "")
    ok, msg_or_name = ensure_unique_on_register(requested)
    if not ok:
        return jsonify({"ok": False, "message": msg_or_name}), 400
    canonical = msg_or_name
    lower = canonical.casefold()
    PLAYERS.add(canonical)
    NAME_INDEX[lower] = canonical
    SCORES[canonical] = SCORES[canonical]
    return jsonify({"ok": True})

@app.route("/api/state")
def api_state():
    # Public state for participants (NO CORRECT ANSWER LEAKED)
    qs = load_questions()
    q = current_question()
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

@app.route("/api/admin_state")
def api_admin_state():
    # Detailed state for facilitator (includes correct answer)
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        return abort(403)
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
    """Accept one answer per player for current question. Correct=+1, wrong/missing=0."""
    global SUBMITTED, CURRENT_ANSWERS
    if PHASE != "question":
        return jsonify({"accepted": False, "message": "Not accepting answers now"}), 400
    payload = request.get_json(force=True)
    ok, canon_or_msg = to_canonical(payload.get("name"))
    if not ok:
        return jsonify({"accepted": False, "message": canon_or_msg}), 400
    canonical = canon_or_msg

    now = time.time()
    if canonical in LAST_SUBMISSION_TS and (now - LAST_SUBMISSION_TS[canonical] < 0.8):
        return jsonify({"accepted": False, "message": "Slow down"}), 429
    LAST_SUBMISSION_TS[canonical] = now

    if canonical in SUBMITTED:
        return jsonify({"accepted": False, "message": "Already submitted"}), 200

    q = current_question()
    answer = payload.get("answer", None)
    CURRENT_ANSWERS[canonical] = answer

    correct = False
    try:
        if answer == q["answer"]:
            correct = True
            SCORES[canonical] += 1
    except Exception:
        pass

    SUBMITTED.add(canonical)

    # Auto-advance to ANSWER (show correct on admin) when everyone submitted
    if len(PLAYERS) > 0 and len(SUBMITTED) >= len(PLAYERS):
        _advance_to_answer()

    return jsonify({"accepted": True, "correct": correct})

@app.route("/api/leaderboard")
def api_leaderboard():
    rows = sorted(SCORES.items(), key=lambda kv: kv[1], reverse=True)
    win, mx = winners()
    return jsonify({"rows": [{"name": k, "score": v} for k, v in rows], "winners": win, "max_score": mx})

# ----------------- Admin controls -----------------

def _require_admin():
    if ADMIN_TOKEN and request.args.get("token") == ADMIN_TOKEN:
        return
    if ADMIN_TOKEN:
        abort(403)

def _advance_to_question():
    global PHASE, SUBMITTED, CURRENT_ANSWERS
    PHASE = "question"
    SUBMITTED = set()
    CURRENT_ANSWERS = {}

def _advance_to_answer():
    global PHASE
    PHASE = "answer"

def _advance_to_reveal():
    global PHASE
    PHASE = "reveal"

def _advance_to_final():
    global PHASE
    PHASE = "final"

@app.route("/api/admin/start", methods=["POST"])
def api_admin_start():
    _require_admin()
    global PHASE, CURRENT_INDEX, SCORES, SUBMITTED, CURRENT_ANSWERS
    qs = load_questions()
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    SUBMITTED = set()
    CURRENT_ANSWERS = {}
    CURRENT_INDEX = 0 if len(qs) > 0 else -1
    PHASE = "question" if CURRENT_INDEX >= 0 else "final"
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
    _require_admin()
    global PHASE, CURRENT_INDEX, PLAYERS, NAME_INDEX, SCORES, SUBMITTED, CURRENT_ANSWERS, LAST_SUBMISSION_TS
    PHASE = "lobby"
    CURRENT_INDEX = -1
    SUBMITTED = set()
    CURRENT_ANSWERS = {}
    LAST_SUBMISSION_TS = {}
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    return jsonify({"ok": True, "message": "Reset to lobby"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
