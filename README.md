# SYNERGY Annotation Tool

Human vs LLM PICO Extraction & Screening Experiment.

## Quick Start (local, no auth)

```bash
pip install flask flask-cors gunicorn

cd Study-Screening-Tool

# Optional: drop PDFs into papers/  named  PSY-04.pdf  MED-01.pdf  etc.

python app.py
# Open http://localhost:5050
# Admin: http://localhost:5050/admin
```

---

## Use ngrok (quickest for a single-day session)

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
