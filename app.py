from flask import Flask, jsonify, request, render_template_string
import yaml, os, time
from collections import defaultdict

app = Flask(__name__)

# Simple in-memory store (pod-local) for demo purposes
LEADERBOARD = defaultdict(int)
LAST_SUBMISSION_TS = {}

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OpenShift Quiz</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 20px; }
    .container { max-width: 800px; margin: auto; }
    h1 { margin-bottom: 0.2rem; }
    .muted { color: #555; margin-top: 0; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin: 12px 0; }
    button { padding: 10px 16px; border: 0; border-radius: 6px; cursor: pointer; }
    .primary { background: #ee0000; color: white; }
    .secondary { background: #f2f2f2; }
    .grid { display: grid; gap: 8px; }
    .qtitle { font-weight: 600; margin-bottom: 6px; }
    .footer { margin-top: 24px; font-size: 14px; color: #666; }
    .leaderboard { font-variant-numeric: tabular-nums; }
    .badge { display:inline-block;background:#222;color:#fff;border-radius:20px;padding:4px 10px;font-size:12px;margin-left:6px;}
    .ok { background:#198754; }
    .warn { background:#6c757d; }
  </style>
</head>
<body>
<div class="container">
  <h1>☸️ OpenShift Quiz</h1>
  <p class="muted">Lightweight, collaborative, and a bit nerdy — powered by open source.</p>

  <div class="card">
    <div class="grid">
      <label>Team or Name:
        <input id="player" placeholder="e.g., Pods, Operators, Alice..." style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px">
      </label>
      <div>
        <button class="secondary" onclick="loadQuestions()">Load/Reload Questions</button>
        <button class="secondary" onclick="showLeaderboard()">Show Leaderboard</button>
      </div>
    </div>
  </div>

  <div id="questions"></div>

  <div class="card">
    <button class="primary" onclick="submitAnswers()">Submit Answers</button>
    <span id="result" class="badge warn" style="display:none;margin-left:12px;"></span>
  </div>

  <div id="board" class="card leaderboard" style="display:none"></div>

  <div class="footer">Tip: the facilitator can project <code>/api/leaderboard</code> directly for live scores.</div>
</div>

<script>
let Q = [];
function el(id){ return document.getElementById(id); }

async function loadQuestions(){
  const r = await fetch('/api/questions');
  Q = await r.json();
  const wrap = el('questions');
  wrap.innerHTML = '';
  Q.forEach((q, i) => {
    const div = document.createElement('div');
    div.className='card';
    const opts = q.options.map((o, j) =>
      `<label style="display:block;margin:4px 0;">
        <input type="radio" name="q_${i}" value="${j}">
        ${o}
      </label>`).join('');
    div.innerHTML = `
      <div class="qtitle">${i+1}. ${q.text}</div>
      ${opts}
      ${q.note ? `<div class="muted" style="margin-top:8px;">💡 ${q.note}</div>` : ''}
    `;
    wrap.appendChild(div);
  });
}

async function submitAnswers(){
  const name = el('player').value.trim();
  if(!name){ alert('Please enter your team or name.'); return; }
  const answers = [];
  for(let i=0;i<Q.length;i++){
    const chosen = document.querySelector(`input[name="q_${i}"]:checked`);
    answers.push(chosen ? parseInt(chosen.value) : null);
  }
  const r = await fetch('/api/submit', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name, answers })
  });
  const data = await r.json();
  const badge = el('result');
  badge.style.display='inline-block';
  badge.className='badge ok';
  badge.textContent = `Score: ${data.score}/${Q.length}`;
  showLeaderboard();
}

async function showLeaderboard(){
  const r = await fetch('/api/leaderboard');
  const data = await r.json();
  const b = el('board');
  b.style.display='block';
  b.innerHTML = '<h3>Leaderboard</h3>' +
    data.map((row, i) => `<div>${i+1}. <strong>${row.name}</strong> — ${row.score}</div>`).join('');
}

loadQuestions();
</script>
</body>
</html>
"""

def load_questions():
    path = os.environ.get("QUESTIONS_FILE", "/app/questions.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # Normalize: expect list of {text, options, answer, note?}
    return data.get("questions", [])

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/api/questions")
def api_questions():
    return jsonify(load_questions())

@app.route("/api/submit", methods=["POST"])
def api_submit():
    payload = request.get_json(force=True)
    name = (payload.get("name") or "anonymous").strip()
    answers = payload.get("answers") or []
    questions = load_questions()
    score = 0
    for i, q in enumerate(questions):
        try:
            if answers[i] == q["answer"]:
                score += 1
        except Exception:
            pass
    # Prevent spam: allow one submission every 10s per name
    now = time.time()
    if name in LAST_SUBMISSION_TS and (now - LAST_SUBMISSION_TS[name] < 10):
        pass
    else:
        LEADERBOARD[name] = max(LEADERBOARD[name], score)
        LAST_SUBMISSION_TS[name] = now
    return jsonify({"score": score})

@app.route("/api/leaderboard")
def api_leaderboard():
    sorted_rows = sorted(LEADERBOARD.items(), key=lambda kv: kv[1], reverse=True)
    return jsonify([{"name": k, "score": v} for k, v in sorted_rows])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
