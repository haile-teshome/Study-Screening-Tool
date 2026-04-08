"""
SYNERGY Annotation Tool — Backend API
Flask + SQLite.  python app.py

Security env vars:
  ACCESS_TOKEN   shared link token for annotators  (empty = open)
  ADMIN_TOKEN    private token for admin/export     (empty = open)
  FLASK_SECRET   session signing key (auto-generated if unset)

Generate tokens:
  python -c "import secrets; print(secrets.token_urlsafe(32))"
"""
import sqlite3, json, os, secrets
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import (Flask, request, jsonify, send_from_directory,
                   send_file, abort, session, redirect, Response)
from flask_cors import CORS
import os

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
CORS(app, supports_credentials=True)

ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN",  "")

DB_PATH  = Path(__file__).parent / "synergy_annotations.db"
PDF_DIR  = Path(__file__).parent / "papers"
STATIC   = Path(__file__).parent / "static"
STATIC.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)


# ── Auth decorators ──────────────────────────────────────────────────────────

def require_access(f):
    @wraps(f)
    def w(*a, **kw):
        if not ACCESS_TOKEN:
            return f(*a, **kw)
        tok = (request.headers.get("X-Access-Token")
               or request.args.get("token"))
        if session.get("authed") or tok == ACCESS_TOKEN:
            return f(*a, **kw)
        return jsonify({"error": "unauthorized"}), 401
    return w

def require_admin(f):
    @wraps(f)
    def w(*a, **kw):
        if not ADMIN_TOKEN:
            return f(*a, **kw)
        tok = (request.headers.get("X-Admin-Token")
               or request.args.get("admin_token")
               or (request.json or {}).get("admin_token"))
        if session.get("admin_authed") or tok == ADMIN_TOKEN:
            return f(*a, **kw)
        return jsonify({"error": "admin token required"}), 403
    return w


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS annotators (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            email      TEXT UNIQUE NOT NULL,
            role       TEXT DEFAULT 'student',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS papers (
            id           TEXT PRIMARY KEY,
            domain       TEXT NOT NULL,
            title        TEXT NOT NULL,
            authors      TEXT,
            year         INTEGER,
            journal      TEXT,
            doi          TEXT,
            pmid         TEXT,
            study_design TEXT,
            population   TEXT,
            intervention TEXT,
            comparator   TEXT,
            outcome      TEXT,
            key_metrics  TEXT,
            sample_size  TEXT,
            has_pdf      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            annotator_id INTEGER NOT NULL REFERENCES annotators(id),
            paper_id     TEXT NOT NULL REFERENCES papers(id),
            task         TEXT NOT NULL,
            started_at   TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            duration_sec INTEGER,
            status       TEXT DEFAULT 'in_progress',
            UNIQUE(annotator_id, paper_id, task)
        );
        CREATE TABLE IF NOT EXISTS screening_decisions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id INTEGER NOT NULL REFERENCES annotators(id),
            paper_id     TEXT NOT NULL REFERENCES papers(id),
            decision     TEXT NOT NULL,
            confidence   INTEGER,
            reason       TEXT,
            notes        TEXT,
            decided_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS pico_extractions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id    INTEGER NOT NULL REFERENCES annotators(id),
            paper_id        TEXT NOT NULL REFERENCES papers(id),
            element         TEXT NOT NULL,
            extracted_text  TEXT,
            source_location TEXT,
            confidence      INTEGER,
            notes           TEXT,
            saved_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS highlights (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id  INTEGER NOT NULL REFERENCES annotators(id),
            paper_id      TEXT NOT NULL REFERENCES papers(id),
            element       TEXT,
            selected_text TEXT NOT NULL,
            page_num      INTEGER,
            rects_json    TEXT,
            color         TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id INTEGER NOT NULL REFERENCES annotators(id),
            paper_id     TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            event_data   TEXT,
            ts           TEXT DEFAULT (datetime('now'))
        );
        """)
    _seed_papers()


def _seed_papers():
    """Scan papers folder and only create database entries for PDFs that exist."""
    import re
    
    # Metadata lookup for known papers (add more as needed)
    known_papers = {
        "01_Pradeepkiran_2024": ("Alz","Amyloid-β and phosphorylated tau are the key biomarkers and predictors of Alzheimer's disease","Pradeepkiran et al.",2024,"Aging and Disease","10.14336/AD.2024.0416","38739937"),
        "02_Alawode_2021": ("Alz","Transitioning from cerebrospinal fluid to blood tests to facilitate diagnosis and disease monitoring in Alzheimer's disease","Alawode et al.",2021,"Journal of Internal Medicine","10.1111/joim.13332","34021943"),
        "03_Ashton_2021": ("Alz","The validation status of blood biomarkers of amyloid and phospho-tau assessed with the 5-phase development framework for AD biomarkers","Ashton et al.",2021,"European Journal of Nuclear Medicine and Molecular Imaging","10.1007/s00259-021-05223-9","33677733"),
        "04_Chen_2022": ("Alz","Plasma tau proteins for the diagnosis of mild cognitive impairment and Alzheimer's disease: A systematic review and meta-analysis","Chen et al.",2022,"Frontiers in Aging Neuroscience","10.3389/fnagi.2022.942629","35959295"),
        "05_Dhauria_2024": ("Alz","Blood-based biomarkers in Alzheimer's disease: advancing non-invasive diagnostics and prognostics","Dhauria et al.",2024,"International Journal of Molecular Sciences","10.3390/ijms251910911","39456697"),
        "06_Geng_2024": ("Alz","Associations between Alzheimer's disease biomarkers and postoperative delirium or cognitive dysfunction: a meta-analysis and trial sequential analysis","Geng et al.",2024,"European Journal of Anaesthesiology","10.1097/EJA.0000000000001933","38038408"),
        "07_Garcia-Escobar_2024": ("Alz","Blood biomarkers of Alzheimer's disease and cognition: a literature review","Garcia-Escobar et al.",2024,"Biomolecules","10.3390/biomolecules14010093","38254693"),
        "Karikari_2020": ("Alz","Blood phosphorylated tau 181 as a biomarker for Alzheimer's disease","Karikari et al.",2020,"Lancet Neurology","10.1016/S1474-4422(20)30154-2","32359770"),
        "Palmqvist_2020": ("Alz","Discriminative accuracy of plasma phospho-tau217 for Alzheimer disease vs other neurodegenerative disorders","Palmqvist et al.",2020,"JAMA","10.1001/jama.2020.12134","32832587"),
    }
    
    # Scan papers folder for PDFs
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    
    with get_db() as db:
        # Clear old papers that don't have PDFs
        db.execute("DELETE FROM papers WHERE has_pdf=0")
        
        for pdf_path in pdf_files:
            paper_id = pdf_path.stem  # filename without .pdf
            
            # Get metadata if known, otherwise use filename as title
            if paper_id in known_papers:
                domain, title, authors, year, journal, doi, pmid = known_papers[paper_id]
            else:
                # Parse filename for basic info (e.g., "Author_YYYY" or "PIIS...")
                domain = "Res"
                title = paper_id.replace("_", " ")
                authors = "Unknown"
                year = 2024
                journal = "Unknown"
                doi = None
                pmid = None
                # Try to extract year from filename
                year_match = re.search(r'\d{4}', paper_id)
                if year_match:
                    year = int(year_match.group())
                # Try to extract author (first part before underscore or number)
                author_match = re.match(r'^([A-Za-z]+)', paper_id)
                if author_match:
                    authors = author_match.group(1) + " et al."
            
            db.execute("""INSERT OR REPLACE INTO papers
                (id,domain,title,authors,year,journal,doi,pmid,
                 study_design,population,intervention,comparator,
                 outcome,key_metrics,sample_size,has_pdf) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, domain, title, authors, year, journal, doi, pmid,
                 None, None, None, None, None, None, None, 1))


# ── Token auth endpoint ───────────────────────────────────────────────────────

@app.route("/auth", methods=["POST"])
def auth():
    """Annotators POST their access token here; sets a session cookie."""
    data = request.json or {}
    token = data.get("token","").strip()
    if not ACCESS_TOKEN or token == ACCESS_TOKEN:
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Wrong access token"}), 401

@app.route("/admin-auth", methods=["POST"])
def admin_auth():
    data = request.json or {}
    if not ADMIN_TOKEN or data.get("token","") == ADMIN_TOKEN:
        session["admin_authed"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Wrong admin token"}), 403


# ── Annotator API ─────────────────────────────────────────────────────────────

@app.route("/api/annotators", methods=["POST"])
@require_access
def create_annotator():
    data = request.json
    if not data.get("name") or not data.get("email"):
        return jsonify({"error": "name and email required"}), 400
    with get_db() as db:
        try:
            cur = db.execute(
                "INSERT OR IGNORE INTO annotators (name, email, role) VALUES (?,?,?)",
                (data["name"].strip(), data["email"].strip().lower(), data.get("role","student"))
            )
            aid = cur.lastrowid
            if aid == 0:
                row = db.execute("SELECT id FROM annotators WHERE email=?",
                                 (data["email"].strip().lower(),)).fetchone()
                aid = row["id"]
            row = db.execute("SELECT * FROM annotators WHERE id=?", (aid,)).fetchone()
            return jsonify(dict(row))
        except sqlite3.IntegrityError as e:
            return jsonify({"error": str(e)}), 409

@app.route("/api/annotators/<int:aid>")
@require_access
def get_annotator(aid):
    with get_db() as db:
        row = db.execute("SELECT * FROM annotators WHERE id=?", (aid,)).fetchone()
        return jsonify(dict(row)) if row else abort(404)

@app.route("/api/annotators/<int:aid>/progress")
@require_access
def annotator_progress(aid):
    with get_db() as db:
        rows = db.execute(
            "SELECT s.*,p.title,p.domain FROM sessions s JOIN papers p ON p.id=s.paper_id "
            "WHERE s.annotator_id=? ORDER BY s.started_at DESC", (aid,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])


# ── Papers API ────────────────────────────────────────────────────────────────

@app.route("/api/papers")
@require_access
def list_papers():
    dom = request.args.get("domain")
    with get_db() as db:
        if dom:
            rows = db.execute("SELECT * FROM papers WHERE domain=? ORDER BY id", (dom,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM papers ORDER BY domain,id").fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/papers/<pid>")
@require_access
def get_paper(pid):
    with get_db() as db:
        row = db.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
        return jsonify(dict(row)) if row else abort(404)

@app.route("/api/papers/<pid>/pdf")
@require_access
def get_pdf(pid):
    pdf = PDF_DIR / f"{pid}.pdf"
    if not pdf.exists():
        abort(404, "PDF not found")
    return send_file(pdf, mimetype="application/pdf")


# ── Sessions API ──────────────────────────────────────────────────────────────

@app.route("/api/sessions", methods=["POST"])
@require_access
def create_session():
    data = request.json
    aid, pid, task = data.get("annotator_id"), data.get("paper_id"), data.get("task","screening")
    if not aid or not pid:
        return jsonify({"error": "annotator_id and paper_id required"}), 400
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM sessions WHERE annotator_id=? AND paper_id=? AND task=?",
            (aid, pid, task)
        ).fetchone()
        if existing:
            db.execute("UPDATE sessions SET status='in_progress' WHERE id=?", (existing["id"],))
            db.execute("INSERT INTO events (session_id,annotator_id,paper_id,event_type) VALUES (?,?,?,?)",
                       (existing["id"],aid,pid,"resume"))
            return jsonify(dict(existing))
        cur = db.execute("INSERT INTO sessions (annotator_id,paper_id,task) VALUES (?,?,?)", (aid,pid,task))
        sid = cur.lastrowid
        db.execute("INSERT INTO events (session_id,annotator_id,paper_id,event_type) VALUES (?,?,?,?)",
                   (sid,aid,pid,"start"))
        return jsonify({"id":sid,"annotator_id":aid,"paper_id":pid,"task":task,"status":"in_progress"})

@app.route("/api/sessions/<int:sid>/complete", methods=["POST"])
@require_access
def complete_session(sid):
    with get_db() as db:
        sess = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not sess: abort(404)
        started = datetime.fromisoformat(sess["started_at"])
        now = datetime.utcnow()
        dur = int((now - started).total_seconds())
        db.execute("UPDATE sessions SET completed_at=?,duration_sec=?,status='completed' WHERE id=?",
                   (now.isoformat(),dur,sid))
        db.execute("INSERT INTO events (session_id,annotator_id,paper_id,event_type,event_data) VALUES (?,?,?,?,?)",
                   (sid,sess["annotator_id"],sess["paper_id"],"complete",json.dumps({"duration_sec":dur})))
        return jsonify({"session_id":sid,"duration_sec":dur})

@app.route("/api/sessions/<int:sid>/data")
@require_access
def session_data(sid):
    with get_db() as db:
        sess = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not sess: abort(404)
        scr = db.execute("SELECT * FROM screening_decisions WHERE session_id=?", (sid,)).fetchall()
        ext = db.execute("SELECT * FROM pico_extractions WHERE session_id=?", (sid,)).fetchall()
        hls = db.execute("SELECT * FROM highlights WHERE session_id=?", (sid,)).fetchall()
        return jsonify({"session":dict(sess),"screening":[dict(r) for r in scr],
                        "extractions":[dict(r) for r in ext],"highlights":[dict(r) for r in hls]})


# ── Annotation API ────────────────────────────────────────────────────────────

@app.route("/api/screening", methods=["POST"])
@require_access
def save_screening():
    data = request.json
    if not all(data.get(k) for k in ["session_id","annotator_id","paper_id","decision"]):
        return jsonify({"error":"session_id, annotator_id, paper_id, decision required"}), 400
    if data["decision"] not in ("include","exclude","uncertain"):
        return jsonify({"error":"decision must be include/exclude/uncertain"}), 400
    with get_db() as db:
        ex = db.execute("SELECT id FROM screening_decisions WHERE session_id=?",
                        (data["session_id"],)).fetchone()
        if ex:
            db.execute("UPDATE screening_decisions SET decision=?,confidence=?,reason=?,notes=?,decided_at=? WHERE id=?",
                       (data["decision"],data.get("confidence"),data.get("reason",""),
                        data.get("notes",""),datetime.utcnow().isoformat(),ex["id"]))
            return jsonify({"id":ex["id"],"updated":True})
        cur = db.execute(
            "INSERT INTO screening_decisions (session_id,annotator_id,paper_id,decision,confidence,reason,notes) VALUES (?,?,?,?,?,?,?)",
            (data["session_id"],data["annotator_id"],data["paper_id"],data["decision"],
             data.get("confidence"),data.get("reason",""),data.get("notes","")))
        db.execute("INSERT INTO events (session_id,annotator_id,paper_id,event_type,event_data) VALUES (?,?,?,?,?)",
                   (data["session_id"],data["annotator_id"],data["paper_id"],"decision",
                    json.dumps({"decision":data["decision"]})))
        return jsonify({"id":cur.lastrowid,"updated":False})

@app.route("/api/extraction", methods=["POST"])
@require_access
def save_extraction():
    data = request.json
    if not all(k in data for k in ["session_id","annotator_id","paper_id","elements"]):
        return jsonify({"error":"Missing fields"}), 400
    with get_db() as db:
        saved=[]
        for el in data["elements"]:
            db.execute("DELETE FROM pico_extractions WHERE session_id=? AND element=?",
                       (data["session_id"],el["element"]))
            cur = db.execute(
                "INSERT INTO pico_extractions (session_id,annotator_id,paper_id,element,extracted_text,source_location,confidence,notes) VALUES (?,?,?,?,?,?,?,?)",
                (data["session_id"],data["annotator_id"],data["paper_id"],el["element"],
                 el.get("extracted_text",""),el.get("source_location",""),el.get("confidence"),el.get("notes","")))
            saved.append(cur.lastrowid)
        db.execute("INSERT INTO events (session_id,annotator_id,paper_id,event_type,event_data) VALUES (?,?,?,?,?)",
                   (data["session_id"],data["annotator_id"],data["paper_id"],"save",
                    json.dumps({"elements_saved":len(saved)})))
    return jsonify({"saved":saved})

@app.route("/api/highlights", methods=["POST"])
@require_access
def save_highlight():
    data = request.json
    if not all(data.get(k) for k in ["session_id","annotator_id","paper_id","selected_text"]):
        return jsonify({"error":"Missing fields"}), 400
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO highlights (session_id,annotator_id,paper_id,element,selected_text,page_num,rects_json,color) VALUES (?,?,?,?,?,?,?,?)",
            (data["session_id"],data["annotator_id"],data["paper_id"],data.get("element"),
             data["selected_text"],data.get("page_num"),
             json.dumps(data.get("rects",[])),data.get("color","#FFEB3B")))
        return jsonify({"id":cur.lastrowid})

@app.route("/api/highlights/<int:hlid>", methods=["DELETE"])
@require_access
def delete_highlight(hlid):
    with get_db() as db:
        db.execute("DELETE FROM highlights WHERE id=?", (hlid,))
    return jsonify({"deleted":hlid})


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
@require_access
def stats():
    with get_db() as db:
        n_ann  = db.execute("SELECT COUNT(*) FROM annotators").fetchone()[0]
        n_sess = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_done = db.execute("SELECT COUNT(*) FROM sessions WHERE status='completed'").fetchone()[0]
        decs   = db.execute("SELECT decision,COUNT(*) n FROM screening_decisions GROUP BY decision").fetchall()
        bydom  = db.execute("""SELECT p.domain,COUNT(DISTINCT sd.annotator_id) annotators,COUNT(sd.id) decisions
                               FROM screening_decisions sd JOIN papers p ON p.id=sd.paper_id GROUP BY p.domain""").fetchall()
        avg    = db.execute("SELECT AVG(duration_sec) FROM sessions WHERE status='completed'").fetchone()[0]
        return jsonify({"annotators":n_ann,"sessions":n_sess,"completed":n_done,
                        "decisions":{r["decision"]:r["n"] for r in decs},
                        "by_domain":[dict(r) for r in bydom],
                        "avg_duration_sec":round(avg or 0)})


# ── Admin / Export (protected) ────────────────────────────────────────────────

@app.route("/api/export/screening")
@require_admin
def export_screening():
    with get_db() as db:
        rows = db.execute("""
            SELECT sd.id,a.name annotator_name,a.email,a.role,
                   sd.paper_id,p.domain,p.title,
                   sd.decision,sd.confidence,sd.reason,sd.notes,sd.decided_at,
                   s.started_at,s.completed_at,s.duration_sec,s.status
            FROM screening_decisions sd
            JOIN annotators a ON a.id=sd.annotator_id
            JOIN papers p ON p.id=sd.paper_id
            JOIN sessions s ON s.id=sd.session_id
            ORDER BY p.domain,sd.paper_id,a.name
        """).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/export/extractions")
@require_admin
def export_extractions():
    with get_db() as db:
        rows = db.execute("""
            SELECT pe.id,a.name annotator_name,a.email,a.role,
                   pe.paper_id,p.domain,p.title,
                   pe.element,pe.extracted_text,pe.source_location,
                   pe.confidence,pe.notes,pe.saved_at,
                   s.started_at,s.completed_at,s.duration_sec
            FROM pico_extractions pe
            JOIN annotators a ON a.id=pe.annotator_id
            JOIN papers p ON p.id=pe.paper_id
            JOIN sessions s ON s.id=pe.session_id
            ORDER BY p.domain,pe.paper_id,a.name,pe.element
        """).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/export/highlights")
@require_admin
def export_highlights():
    with get_db() as db:
        rows = db.execute("""
            SELECT h.id,a.name annotator_name,a.email,
                   h.paper_id,p.domain,h.element,
                   h.selected_text,h.page_num,h.color,h.created_at,s.task
            FROM highlights h
            JOIN annotators a ON a.id=h.annotator_id
            JOIN papers p ON p.id=h.paper_id
            JOIN sessions s ON s.id=h.session_id
            ORDER BY h.paper_id,a.name,h.page_num
        """).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/export/csv/screening")
@require_admin
def export_screening_csv():
    """Download screening data as CSV."""
    import csv, io
    with get_db() as db:
        rows = db.execute("""
            SELECT a.name,a.email,a.role,sd.paper_id,p.domain,
                   sd.decision,sd.confidence,sd.reason,sd.notes,
                   sd.decided_at,s.duration_sec,s.status
            FROM screening_decisions sd
            JOIN annotators a ON a.id=sd.annotator_id
            JOIN papers p ON p.id=sd.paper_id
            JOIN sessions s ON s.id=sd.session_id
            ORDER BY p.domain,sd.paper_id,a.name
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["annotator_name","email","role","paper_id","domain",
                "decision","confidence","reason","notes",
                "decided_at","duration_sec","status"])
    w.writerows([list(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment;filename=screening.csv"})

@app.route("/api/export/csv/extractions")
@require_admin
def export_extractions_csv():
    import csv, io
    with get_db() as db:
        rows = db.execute("""
            SELECT a.name,a.email,a.role,pe.paper_id,p.domain,
                   pe.element,pe.extracted_text,pe.source_location,
                   pe.saved_at,s.duration_sec
            FROM pico_extractions pe
            JOIN annotators a ON a.id=pe.annotator_id
            JOIN papers p ON p.id=pe.paper_id
            JOIN sessions s ON s.id=pe.session_id
            ORDER BY p.domain,pe.paper_id,a.name,pe.element
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["annotator_name","email","role","paper_id","domain",
                "element","extracted_text","source_location","saved_at","duration_sec"])
    w.writerows([list(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment;filename=extractions.csv"})


# ── Admin dashboard (HTML) ────────────────────────────────────────────────────

@app.route("/admin")
@require_admin
def admin_dashboard():
    with get_db() as db:
        stats_data = {
            "annotators": db.execute("SELECT COUNT(*) FROM annotators").fetchone()[0],
            "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "completed": db.execute("SELECT COUNT(*) FROM sessions WHERE status='completed'").fetchone()[0],
            "decisions": db.execute("SELECT COUNT(*) FROM screening_decisions").fetchone()[0],
            "extractions": db.execute("SELECT COUNT(*) FROM pico_extractions").fetchone()[0],
            "highlights": db.execute("SELECT COUNT(*) FROM highlights").fetchone()[0],
        }
        annotators = db.execute("""
            SELECT a.id,a.name,a.email,a.role,a.created_at,
                   COUNT(DISTINCT s.id) sessions,
                   SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) completed,
                   COUNT(DISTINCT sd.paper_id) papers_screened,
                   COUNT(DISTINCT pe.paper_id) papers_extracted,
                   ROUND(AVG(CASE WHEN s.status='completed' THEN s.duration_sec END)/60.0,1) avg_min
            FROM annotators a
            LEFT JOIN sessions s ON s.annotator_id=a.id
            LEFT JOIN screening_decisions sd ON sd.annotator_id=a.id
            LEFT JOIN pico_extractions pe ON pe.annotator_id=a.id
            GROUP BY a.id ORDER BY a.created_at DESC
        """).fetchall()
        decisions = db.execute("""
            SELECT a.name,a.email,sd.paper_id,p.domain,sd.decision,sd.confidence,
                   sd.reason,sd.decided_at,s.duration_sec
            FROM screening_decisions sd
            JOIN annotators a ON a.id=sd.annotator_id
            JOIN papers p ON p.id=sd.paper_id
            JOIN sessions s ON s.id=sd.session_id
            ORDER BY sd.decided_at DESC LIMIT 200
        """).fetchall()

    rows_html = "".join(f"""<tr>
        <td>{a['name']}</td><td style="color:#7b82a0">{a['email']}</td>
        <td><span style="background:rgba(91,141,238,.15);color:#5b8dee;padding:2px 7px;border-radius:12px;font-size:10px">{a['role']}</span></td>
        <td>{a['sessions']}</td><td>{a['completed']}</td>
        <td>{a['papers_screened']}</td><td>{a['papers_extracted']}</td>
        <td>{a['avg_min'] or '—'} min</td>
        <td><a href="/api/export/csv/screening?admin_token={ADMIN_TOKEN}" style="color:#5b8dee;font-size:11px">CSV ↓</a></td>
    </tr>""" for a in annotators)

    dec_html = "".join(f"""<tr>
        <td style="font-size:10px">{d['name']}</td>
        <td style="font-family:monospace;font-size:10px;color:#5b8dee">{d['paper_id']}</td>
        <td style="font-size:10px;color:#7b82a0">{d['domain']}</td>
        <td><span style="padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;
            background:{'#0d2a1c' if d['decision']=='include' else '#2a0f0e' if d['decision']=='exclude' else '#2a1c00'};
            color:{'#3ecf8e' if d['decision']=='include' else '#f25f5c' if d['decision']=='exclude' else '#f5a623'}">{d['decision']}</span></td>
        <td style="font-size:10px">{d['confidence'] or '—'}</td>
        <td style="font-size:9px;color:#7b82a0;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{(d['reason'] or '')[:80]}</td>
        <td style="font-size:9px;color:#7b82a0">{round((d['duration_sec'] or 0)/60,1)}m</td>
    </tr>""" for d in decisions)

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>SYNERGY Admin</title>
<style>
body{{background:#0f1117;color:#e8eaf6;font-family:'DM Sans',system-ui,sans-serif;margin:0;padding:24px}}
h1{{font-size:20px;font-weight:600;margin-bottom:4px}}h2{{font-size:14px;font-weight:600;margin:24px 0 10px;color:#7b82a0}}
.stats{{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}}
.stat{{background:#1a1d27;border:1px solid #2e3352;border-radius:10px;padding:14px 18px;min-width:100px}}
.stat .n{{font-size:28px;font-weight:600;color:#5b8dee}}.stat .l{{font-size:11px;color:#7b82a0;margin-top:2px}}
table{{width:100%;border-collapse:collapse;margin-bottom:24px;font-size:12px}}
th{{text-align:left;padding:6px 10px;color:#7b82a0;font-size:9px;text-transform:uppercase;letter-spacing:.7px;border-bottom:1px solid #2e3352}}
td{{padding:6px 10px;border-bottom:1px solid #1a1d27}}
tr:hover td{{background:#1a1d27}}
.export-links{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
.export-links a{{background:#22263a;border:1px solid #2e3352;border-radius:7px;padding:7px 14px;color:#5b8dee;text-decoration:none;font-size:12px}}
.export-links a:hover{{border-color:#5b8dee}}
</style></head><body>
<h1>🔬 SYNERGY Admin Dashboard</h1>
<p style="color:#7b82a0;font-size:12px;margin-bottom:20px">All annotator data. Refresh to update.</p>
<div class="stats">
  <div class="stat"><div class="n">{stats_data['annotators']}</div><div class="l">Annotators</div></div>
  <div class="stat"><div class="n">{stats_data['sessions']}</div><div class="l">Sessions</div></div>
  <div class="stat"><div class="n">{stats_data['completed']}</div><div class="l">Completed</div></div>
  <div class="stat"><div class="n">{stats_data['decisions']}</div><div class="l">Decisions</div></div>
  <div class="stat"><div class="n">{stats_data['extractions']}</div><div class="l">Extractions</div></div>
  <div class="stat"><div class="n">{stats_data['highlights']}</div><div class="l">Highlights</div></div>
</div>
<div class="export-links">
  <a href="/api/export/csv/screening?admin_token={ADMIN_TOKEN}">⬇ Screening CSV</a>
  <a href="/api/export/csv/extractions?admin_token={ADMIN_TOKEN}">⬇ Extractions CSV</a>
  <a href="/api/export/screening?admin_token={ADMIN_TOKEN}">⬇ Screening JSON</a>
  <a href="/api/export/extractions?admin_token={ADMIN_TOKEN}">⬇ Extractions JSON</a>
  <a href="/api/export/highlights?admin_token={ADMIN_TOKEN}">⬇ Highlights JSON</a>
</div>
<h2>Annotators</h2>
<table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Sessions</th><th>Completed</th><th>Screened</th><th>Extracted</th><th>Avg time</th><th>Export</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<h2>Recent Screening Decisions (last 200)</h2>
<table><thead><tr><th>Annotator</th><th>Paper</th><th>Domain</th><th>Decision</th><th>Conf.</th><th>Reason</th><th>Time</th></tr></thead>
<tbody>{dec_html}</tbody></table>
</body></html>"""


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(STATIC / "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC, path)

    
if __name__ == "__main__":
    init_db()
    access = f"  Access token: {ACCESS_TOKEN}" if ACCESS_TOKEN else "  Access token: NONE (open)"
    admin  = f"  Admin token:  {ADMIN_TOKEN}"  if ADMIN_TOKEN  else "  Admin token:  NONE (open)"
    print(f"\n🔬 SYNERGY Annotation Tool")
    print(f"   http://localhost:5050")
    print(f"   Admin dashboard: http://localhost:5050/admin")
    print(access)
    print(admin)
    print(f"\n   PDFs → papers/  |  DB → synergy_annotations.db\n")
    app.run(debug=False, port=5050, host="0.0.0.0")
