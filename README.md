# SYNERGY Annotation Tool

Human vs LLM PICO Extraction & Screening Experiment.

## Changes in this version
- ⏱  **Timer hidden** — time-on-task still recorded silently but never shown to annotators
- ✍  **Highlighting fixed** — PDF.js text layer makes PDF text genuinely selectable; click and drag to highlight
- 🔒  **Secure hosting** — token-based access control + admin dashboard with one-click CSV exports

---

## Quick Start (local, no auth)

```bash
pip install flask flask-cors gunicorn

cd synergy_app

# Optional: drop PDFs into papers/  named  PSY-04.pdf  MED-01.pdf  etc.

python app.py
# Open http://localhost:5050
# Admin: http://localhost:5050/admin
```

---

## Secure Hosting on Railway (recommended — free, HTTPS, persistent disk)

### 1. Create account
https://railway.app — free tier is enough for a research experiment.

### 2. Install CLI and deploy
```bash
npm install -g @railway/cli
railway login
cd synergy_app
railway init      # new project
railway up        # deploys
```

### 3. Set environment variables (Railway dashboard → Variables)

| Variable | Value |
|---|---|
| `ACCESS_TOKEN` | Token you share with students, e.g. `synergy-ucsf-2025` |
| `ADMIN_TOKEN` | Long random string only you know |
| `FLASK_SECRET` | Random 32-char string for session cookies |

Generate tokens:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Or via CLI:
```bash
railway variables set ACCESS_TOKEN="synergy-ucsf-2025"
railway variables set ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
railway variables set FLASK_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

### 4. Add persistent disk (keeps the database across redeploys)
Railway dashboard → your service → Settings → Volumes → Add Volume
- Mount path: `/app`
- Size: 1 GB (free tier includes this)

### 5. Share with students
Your URL: `https://your-project.up.railway.app`

Students simply visit the URL, enter their name/email/role, and start annotating.
The access token is invisible to them — it's embedded in the session when they visit.

If you want to gate access, tell students to enter `ACCESS_TOKEN` on first visit
(you can add a simple token-entry screen if needed, or just share the token alongside the URL).

---

## Alternative: ngrok (quickest for a single-day session)

```bash
python app.py &
npx ngrok http 5050
# Share the https://xxxx.ngrok.io URL — works for 8h on free tier
```

---

## Admin Dashboard

Go to: `https://your-app.up.railway.app/admin?admin_token=YOUR_ADMIN_TOKEN`

Shows:
- Live counts: annotators, sessions completed, decisions, extractions, highlights
- Per-annotator table with session counts, completion rate, average time
- Recent decisions table
- Download buttons for all data (CSV and JSON)

---

## Data exports

```bash
BASE="https://your-app.up.railway.app"
TOK="your-admin-token"

# CSV (open in Excel or import to R/Python)
curl "$BASE/api/export/csv/screening?admin_token=$TOK"   -o screening.csv
curl "$BASE/api/export/csv/extractions?admin_token=$TOK" -o extractions.csv

# JSON
curl -H "X-Admin-Token: $TOK" $BASE/api/export/screening
curl -H "X-Admin-Token: $TOK" $BASE/api/export/extractions
curl -H "X-Admin-Token: $TOK" $BASE/api/export/highlights
```

---

## What is recorded per annotator

| Field | Table | Detail |
|---|---|---|
| Name, email, role | `annotators` | Entered once at login |
| Paper open, task, timestamp | `sessions` | One row per annotator × paper × task |
| **Time on task** | `sessions.duration_sec` | Computed silently at "Mark Complete" — never displayed |
| Include / Exclude / Uncertain | `screening_decisions` | + confidence 1-5 + reason text + notes |
| PICO extractions (P/I/C/O/SD/SS/M) | `pico_extractions` | Extracted text + source location |
| PDF highlights | `highlights` | Selected text, page number, bounding box rects, color |
| Event log | `events` | Every open, save, decision, complete, resume |

---

## How PDF highlighting works

1. Click **Highlight: OFF** in the viewer toolbar to toggle ON
2. Click and drag to select text in the PDF  
3. On mouseup, selected text + exact bounding box rects saved to DB  
4. Coloured overlay appears at the exact selection position  
5. Highlights restored on session resume  

Colour key (optional):
- Yellow — general  |  Teal — Population  |  Blue — Intervention
- Orange — Comparator  |  Pink — Outcome
