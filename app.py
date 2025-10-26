from flask import Flask, jsonify, request, render_template_string, abort
import yaml, os, time, re
from collections import defaultdict

app = Flask(__name__)

# === In-memory state (pod-local). ===
# We keep a canonical "display name" and a case-insensitive index to prevent duplicates.
PLAYERS = set()                 # canonical display names
NAME_INDEX = {}                 # lowercased -> canonical display name
SCORES = defaultdict(int)       # canonical name -> score
SUBMITTED = set()               # canonical names who submitted current question
LAST_SUBMISSION_TS = {}         # canonical name -> float
PHASE = "lobby"                 # lobby | question | reveal | final
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
        return []
    mx = max(SCORES.values())
    return [n for (n, s) in SCORES.items() if s == mx], mx

_ws_collapse = re.compile(r"\s+")
def normalize_name(name: str) -> str:
    """Trim, collapse inner whitespace, casefold for duplicate detection."""
    name = (name or "").strip()
    name = _ws_collapse.sub(" ", name)
    # Disallow empty, too long, or purely punctuation
    if not name or len(name) > 40:
        return ""
    return name

def to_canonical(submitted_name: str):
    """Returns (ok, canonical or error message)."""
    name = normalize_name(submitted_name)
    if not name:
        return False, "Invalid name"
    lower = name.casefold()
    # If the lowercased exists, return canonical for consistency
    if lower in NAME_INDEX:
        return True, NAME_INDEX[lower]
    # If brand new and we're just mapping to canonical, return the cleaned version
    return True, name

def ensure_unique_on_register(requested_name: str):
    """Enforce unique, case-insensitive names on registration."""
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
let lastRenderKey = ""; // phase:index to avoid re-rendering question block

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

async function loadState(){
  const r = await fetch('/api/state');
  state = await r.json();
  renderState();
}

function renderState(){
  const pc = el('phaseCard');
  const qc = el('questionCard');
  const lb = el('leaderboard');

  if(!val('player') && myName){ el('player').value = myName; }

  // Always update phase banner counts (cheap, non-destructive)
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
    // Only render the question panel if phase/index changed
    if(currentKey !== lastRenderKey){
      const Q = state.question;
      if(Q){
        qc.style.display='block';
        const existingSelection = document.querySelector('input[name="opt"]:checked');
        const selectedVal = existingSelection ? existingSelection.value : null;

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

        // If we had a selection and the question didn't actually change (defensive),
        // try to restore it. (When index changed, this will simply not match and do nothing.)
        if(selectedVal !== null){
          const toCheck = document.querySelector('input[name="opt"][value="'+selectedVal+'"]');
          if(toCheck) toCheck.checked = true;
        }
      } else {
        qc.style.display='none';
      }
      lb.style.display='none';
      lastRenderKey = currentKey;
    }
    return;
  }

  if(state.phase === 'reveal'){
    // Only render leaderboard once per phase/index
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
  s.style.display='inline-block';
  if(r.ok && data.accepted){
    s.className='badge ok';
    s.textContent = data.correct ? 'Saved! Correct ✅' : 'Saved';
  } else {
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
    <div>Leaderboard (project <code>/api/leaderboard</code> on screen)</div>
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
    SCORES[canonical] = SCORES[canonical]  # ensure key exists
    return jsonify({"ok": True})

@app.route("/api/state")
def api_state():
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

@app.route("/api/submit", methods=["POST"])
def api_submit():
    """Accept one answer per player for current question. Correct=+1, wrong/missing=0."""
    global SUBMITTED
    if PHASE != "question":
        return jsonify({"accepted": False, "message": "Not accepting answers now"}), 400
    payload = request.get_json(force=True)
    submitted_name = (payload.get("name") or "")
    ok, canon_or_msg = to_canonical(submitted_name)
    if not ok:
        return jsonify({"accepted": False, "message": canon_or_msg}), 400
    canonical = canon_or_msg
    lower = canonical.casefold()
    if lower not in NAME_INDEX:
        return jsonify({"accepted": False, "message": "Please register first"}), 400
    canonical = NAME_INDEX[lower]

    now = time.time()
    if canonical in LAST_SUBMISSION_TS and (now - LAST_SUBMISSION_TS[canonical] < 0.8):
        return jsonify({"accepted": False, "message": "Slow down"}), 429
    LAST_SUBMISSION_TS[canonical] = now

    if canonical in SUBMITTED:
        return jsonify({"accepted": False, "message": "Already submitted"}), 200

    q = current_question()
    correct = False
    try:
        if payload.get("answer") == q["answer"]:
            correct = True
            SCORES[canonical] += 1
    except Exception:
        pass

    SUBMITTED.add(canonical)

    # auto-reveal if all registered players submitted
    if len(PLAYERS) > 0 and len(SUBMITTED) >= len(PLAYERS):
        _advance_to_reveal()

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
    global PHASE, SUBMITTED
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
    # reset scores for registered players only
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
    elif PHASE == "lobby":
        return jsonify({"ok": False, "message": "Start the quiz first"}), 400
    else:
        return jsonify({"ok": True, "message": "Already final"})

@app.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    _require_admin()
    global PHASE, CURRENT_INDEX, PLAYERS, NAME_INDEX, SCORES, SUBMITTED, LAST_SUBMISSION_TS
    PHASE = "lobby"
    CURRENT_INDEX = -1
    SUBMITTED = set()
    LAST_SUBMISSION_TS = {}
    # Keep players but clear scores; or uncomment next two lines to wipe players too
    # PLAYERS = set()
    # NAME_INDEX = {}
    SCORES = defaultdict(int, {name: 0 for name in PLAYERS})
    return jsonify({"ok": True, "message": "Reset to lobby"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
