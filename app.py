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
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id        INTEGER NOT NULL REFERENCES annotators(id),
            paper_id            TEXT NOT NULL REFERENCES papers(id),
            decision            TEXT NOT NULL,
            confidence          INTEGER,
            reason              TEXT,
            notes               TEXT,
            inclusion_criteria  TEXT,
            exclusion_criteria  TEXT,
            criteria_evidence   TEXT,
            decided_at          TEXT DEFAULT (datetime('now'))
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
        CREATE TABLE IF NOT EXISTS page_visits (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id INTEGER NOT NULL REFERENCES annotators(id),
            paper_id     TEXT NOT NULL,
            page_num     INTEGER NOT NULL,
            visit_start  TEXT DEFAULT (datetime('now')),
            visit_end    TEXT,
            dwell_sec    INTEGER,
            section      TEXT,  -- abstract, intro, methods, results, discussion, figures
            is_revisit   INTEGER DEFAULT 0,
            scroll_depth INTEGER  -- pixels scrolled on this page
        );
        CREATE TABLE IF NOT EXISTS cognitive_load (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id        INTEGER NOT NULL,
            paper_id            TEXT NOT NULL,
            decision_sequence   INTEGER,
            decision_time_sec   INTEGER,
            confidence_trend    REAL,
            reason_word_count   INTEGER,
            hesitation_events   INTEGER DEFAULT 0,
            time_to_first_action INTEGER,  -- seconds before first highlight/decision
            first_highlight_at  TEXT,
            first_decision_at   TEXT,
            created_at          TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS highlight_metrics (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id        INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id      INTEGER NOT NULL,
            paper_id          TEXT NOT NULL,
            total_highlights  INTEGER DEFAULT 0,
            color_switches    INTEGER DEFAULT 0,
            self_corrections  INTEGER DEFAULT 0,  -- deleted and re-added
            highlight_density REAL,  -- highlights per page
            created_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS extraction_quality (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id             INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id           INTEGER NOT NULL,
            paper_id               TEXT NOT NULL,
            element                TEXT NOT NULL,
            extraction_method      TEXT DEFAULT 'manual',  -- manual, llm_assisted, llm_only_reviewed
            copy_paste_score       REAL,  -- similarity to source text (0-1)
            word_count             INTEGER,
            elaboration_ratio      REAL,  -- extraction_words / source_words
            paraphrase_detected    INTEGER DEFAULT 0,
            inference_markers      INTEGER DEFAULT 0,  -- count of "suggests", "implies", etc.
            timestamp              TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS decision_history (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id         INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id       INTEGER NOT NULL,
            paper_id           TEXT NOT NULL,
            previous_decision  TEXT,
            new_decision       TEXT NOT NULL,
            previous_confidence INTEGER,
            new_confidence     INTEGER,
            change_reason      TEXT,  -- why they changed (optional)
            flip_count         INTEGER DEFAULT 0,  -- how many times they've flipped
            timestamp          TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS reading_patterns (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id           INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id         INTEGER NOT NULL,
            paper_id             TEXT NOT NULL,
            reading_order        TEXT,  -- JSON array of page visits in order [1,2,3,2,4...]
            backward_navigations INTEGER DEFAULT 0,  -- count of going back to previous pages
            section_dwell_times  TEXT,  -- JSON: {"abstract": 45, "methods": 120, ...}
            linear_reading_ratio REAL,  -- % of time reading sequentially vs jumping
            total_pages_viewed   INTEGER DEFAULT 0,
            unique_pages_viewed  INTEGER DEFAULT 0,
            revisit_patterns     TEXT,  -- JSON: which pages were revisited and how often
            timestamp            TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS llm_comparison (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id            INTEGER NOT NULL,
            paper_id                TEXT NOT NULL,
            element                 TEXT NOT NULL,  -- P, I, C, or O
            llm_extraction          TEXT,  -- what the LLM extracted
            human_extraction        TEXT,  -- what the human extracted
            similarity_score        REAL,  -- semantic similarity 0-1
            agreement_type          TEXT,  -- exact, partial, contradictory, missing
            human_review_time_sec   INTEGER,  -- time spent reviewing LLM output
            human_modifications     INTEGER DEFAULT 0,  -- how many changes made to LLM text
            review_sequence         INTEGER,  -- order in which LLM elements were reviewed
            timestamp               TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS task_switches (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id     INTEGER NOT NULL,
            paper_id         TEXT NOT NULL,
            from_task        TEXT NOT NULL,  -- screening, extraction, highlight
            to_task          TEXT NOT NULL,
            switch_time_sec  INTEGER,  -- seconds into session when switch occurred
            switch_reason    TEXT,  -- user initiated, auto, etc.
            session_duration_at_switch INTEGER,  -- total session time at switch
            timestamp        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS fatigue_indicators (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              INTEGER NOT NULL REFERENCES sessions(id),
            annotator_id            INTEGER NOT NULL,
            paper_id                TEXT NOT NULL,
            time_into_session_sec   INTEGER,  -- seconds elapsed
            response_time_ms        INTEGER,  -- time to make a decision/click
            highlight_accuracy      REAL,  -- precision of highlight placement (if trackable)
            confidence_value        INTEGER,  -- current confidence level
            consecutive_similar_conf INTEGER DEFAULT 0,  -- streak of same confidence
            words_per_minute        REAL,  -- typing speed in extraction fields
            scroll_velocity         REAL,  -- pixels per second scrolling
            mouse_idle_time_sec     INTEGER,  -- time since last mouse movement
            decision_time_trend     REAL,  -- ratio of current vs avg decision time
            timestamp               TEXT DEFAULT (datetime('now'))
        );
        """)
    _seed_papers()


def _seed_papers():
    """Scan papers folder and only create database entries for PDFs that exist."""
    import re
    
    # Metadata lookup for known papers (add more as needed)
    # Format: paper_id: (domain, title, authors, year, journal, doi, pmid)
    known_papers = {
        "001_Ashton_2024": ("Alz","Diagnostic accuracy of a plasma phosphorylated tau 217 immunoassay for Alzheimer disease pathology","Ashton et al.",2024,"JAMA Neurology","10.1001/jamaneurol.2023.5319","38252443"),
        "002_Karikari_2020": ("Alz","Blood phosphorylated tau 181 as a biomarker for Alzheimer's disease","Karikari et al.",2020,"Lancet Neurology","10.1016/S1474-4422(20)30154-2","32359770"),
        "003_Palmqvist_2025": ("Alz","Plasma phospho-tau217 for Alzheimer's disease diagnosis in primary care","Palmqvist et al.",2025,"Nature Medicine","10.1038/s41591-025-03622-w","39743773"),
        "004_PuigPijoan_2024": ("Alz","Plasma p-tau217 and p-tau217/Aβ1-42 are effective biomarkers for identifying Alzheimer's disease","Puig-Pijoan et al.",2024,"Alzheimer's & Dementia","10.1002/alz.14536","39887504"),
        "005_Salvado_2025": ("Alz","Plasma p-tau217 and tau-PET predict future cognitive decline among cognitively unimpaired individuals","Salvado et al.",2025,"Nature Aging","10.1038/s43587-025-00835-z","39573346"),
        "006_Therriault_2024": ("Alz","Comparison of two plasma p-tau217 assays to detect and monitor Alzheimer's disease pathology","Therriault et al.",2024,"eBioMedicine","10.1016/j.ebiom.2024.105046","38493397"),
        "007_Barthelemy_2024": ("Alz","Plasma p-tau217 and p-tau181 as biomarkers of Alzheimer's disease pathology and clinical progression","Barthelemy et al.",2024,"Nature Medicine","10.1038/s41591-024-02998-3","38783350"),
        "008_Janelidze_2021": ("Alz","Plasma p-tau217 in the Alzheimer's disease continuum","Janelidze et al.",2021,"Lancet Neurology","10.1016/S1474-4422(21)00234-0","33932300"),
        "009_Liu_2025": ("Alz","The exploration of using plasma biomarkers of p-tau217 and p-tau181 for screening Alzheimer's disease in very elderly people","Liu et al.",2025,"Frontiers in Neurology","10.3389/fneur.2025.1668512",None),
        "010_Palmqvist_2020": ("Alz","Discriminative accuracy of plasma phospho-tau217 for Alzheimer disease vs other neurodegenerative disorders","Palmqvist et al.",2020,"JAMA","10.1001/jama.2020.12134","32832587"),
        "011_Thijssen_2021": ("Alz","Plasma phosphorylated tau 217 and phosphorylated tau 181 as biomarkers in Alzheimer's disease","Thijssen et al.",2021,"JAMA","10.1001/jama.2021.9954","34547092"),
        "012_Janelidze_2023": ("Alz","Plasma p-tau217 predicts in vivo brain pathology and cognitive decline in Alzheimer's disease","Janelidze et al.",2023,"Brain","10.1093/brain/awad077","36947559"),
        "013_Yu_2023": ("Alz","Detection and staging of Alzheimer's disease by plasma p-tau217 in Chinese population","Yu et al.",2023,"Alzheimer's & Dementia","10.1002/alz.13484","37300237"),
        "014_Zhong_2025": ("Alz","Plasma p-tau217 and p-tau217/Aβ1-42 are effective biomarkers for identifying CSF- and PET imaging-diagnosed Alzheimer's disease","Zhong et al.",2025,"Alzheimer's & Dementia","10.1002/alz.14536","39887504"),
        "015_Lin_2025": ("Alz","Plasma p-tau217 as a biomarker for Alzheimer's disease in different clinical settings","Lin et al.",2025,"Journal of Neurology","10.1007/s00415-025-12789-3",None),
        "016_Wang_2025": ("Alz","Assessing diagnostic performance of plasma biomarkers in Alzheimer's disease versus cognitively unimpaired individuals","Wang et al.",2025,"Frontiers in Aging Neuroscience","10.3389/fnagi.2025.1554805","39686765"),
    }
    
    # Scan papers folder for PDFs
    pdf_files = list(PDF_DIR.glob("*.pdf"))
    current_pdf_ids = {f.stem for f in pdf_files}
    
    with get_db() as db:
        # Find papers to remove (those without PDFs)
        if current_pdf_ids:
            placeholders = ",".join("?" * len(current_pdf_ids))
            orphaned = db.execute(
                f"SELECT id FROM papers WHERE id NOT IN ({placeholders})",
                tuple(current_pdf_ids)
            ).fetchall()
        else:
            orphaned = db.execute("SELECT id FROM papers").fetchall()
        
        orphaned_ids = [r[0] for r in orphaned]
        if orphaned_ids:
            oph = ",".join("?" * len(orphaned_ids))
            # Cascade delete from dependent tables first
            db.execute(f"DELETE FROM highlights WHERE paper_id IN ({oph})", tuple(orphaned_ids))
            db.execute(f"DELETE FROM pico_extractions WHERE paper_id IN ({oph})", tuple(orphaned_ids))
            db.execute(f"DELETE FROM screening_decisions WHERE paper_id IN ({oph})", tuple(orphaned_ids))
            db.execute(f"DELETE FROM events WHERE paper_id IN ({oph})", tuple(orphaned_ids))
            db.execute(f"DELETE FROM sessions WHERE paper_id IN ({oph})", tuple(orphaned_ids))
            db.execute(f"DELETE FROM papers WHERE id IN ({oph})", tuple(orphaned_ids))
        
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
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    # Name is no longer collected at login; derive a label from the email.
    name = (data.get("name") or "").strip() or email.split("@")[0]
    with get_db() as db:
        try:
            cur = db.execute(
                "INSERT OR IGNORE INTO annotators (name, email, role) VALUES (?,?,?)",
                (name, email, data.get("role","student"))
            )
            aid = cur.lastrowid
            if aid == 0:
                row = db.execute("SELECT id FROM annotators WHERE email=?",
                                 (email,)).fetchone()
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
            db.execute("""UPDATE screening_decisions SET decision=?,confidence=?,reason=?,notes=?,inclusion_criteria=?,exclusion_criteria=?,criteria_evidence=?,decided_at=? WHERE id=?""",
                        (data["decision"],data.get("confidence"),data.get("reason",""),data.get("notes",""),data.get("inclusion_criteria",""),data.get("exclusion_criteria",""),data.get("criteria_evidence",""),datetime.utcnow().isoformat(),ex["id"]))
            return jsonify({"id":ex["id"],"updated":True})
        cur = db.execute(
            "INSERT INTO screening_decisions (session_id,annotator_id,paper_id,decision,confidence,reason,notes,inclusion_criteria,exclusion_criteria,criteria_evidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (data["session_id"],data["annotator_id"],data["paper_id"],data["decision"],
             data.get("confidence"),data.get("reason",""),data.get("notes",""),
             data.get("inclusion_criteria",""),data.get("exclusion_criteria",""),data.get("criteria_evidence","")))
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
                   sd.decision,sd.confidence,sd.reason,sd.notes,
                   sd.inclusion_criteria,sd.exclusion_criteria,sd.criteria_evidence,sd.decided_at,
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
                   sd.inclusion_criteria,sd.exclusion_criteria,sd.criteria_evidence,
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
                "inclusion_criteria","exclusion_criteria","criteria_evidence",
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
  <a href="/api/export/cognitive-metrics?admin_token={ADMIN_TOKEN}">⬇ Cognitive Metrics</a>
  <a href="/api/export/advanced-metrics?admin_token={ADMIN_TOKEN}">⬇ Advanced Metrics</a>
</div>
<h2>Annotators</h2>
<table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Sessions</th><th>Completed</th><th>Screened</th><th>Extracted</th><th>Avg time</th><th>Export</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<h2>Recent Screening Decisions (last 200)</h2>
<table><thead><tr><th>Annotator</th><th>Paper</th><th>Domain</th><th>Decision</th><th>Conf.</th><th>Reason</th><th>Time</th></tr></thead>
<tbody>{dec_html}</tbody></table>
</body></html>"""


# ── Cognitive Metrics API ─────────────────────────────────────────────────────

@app.route("/api/page-visit", methods=["POST"])
@require_access
def track_page_visit():
    """Track page visits with dwell time and section detection."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id", "page_num"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        # Check if this is a revisit
        prev_visits = db.execute(
            "SELECT COUNT(*) FROM page_visits WHERE session_id=? AND paper_id=? AND page_num=?",
            (data["session_id"], data["paper_id"], data["page_num"])
        ).fetchone()[0]
        is_revisit = 1 if prev_visits > 0 else 0
        
        cur = db.execute("""
            INSERT INTO page_visits 
            (session_id, annotator_id, paper_id, page_num, section, is_revisit, scroll_depth)
            VALUES (?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              data["page_num"], data.get("section"), is_revisit, data.get("scroll_depth", 0)))
        
        return jsonify({"id": cur.lastrowid, "is_revisit": is_revisit})

@app.route("/api/page-visit/<int:visit_id>/end", methods=["POST"])
@require_access
def end_page_visit(visit_id):
    """Mark end of page visit with dwell time."""
    data = request.json
    with get_db() as db:
        db.execute("""
            UPDATE page_visits 
            SET visit_end = datetime('now'), dwell_sec = ?
            WHERE id = ?
        """, (data.get("dwell_sec", 0), visit_id))
        return jsonify({"updated": visit_id})

@app.route("/api/cognitive-load", methods=["POST"])
@require_access
def track_cognitive_load():
    """Track cognitive load metrics per decision."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        # Get decision sequence for this session
        seq = db.execute(
            "SELECT COUNT(*) FROM cognitive_load WHERE session_id=?",
            (data["session_id"],)
        ).fetchone()[0] + 1
        
        cur = db.execute("""
            INSERT INTO cognitive_load 
            (session_id, annotator_id, paper_id, decision_sequence, decision_time_sec,
             confidence_trend, reason_word_count, hesitation_events, 
             time_to_first_action, first_highlight_at, first_decision_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"], seq,
              data.get("decision_time_sec"), data.get("confidence_trend"),
              data.get("reason_word_count"), data.get("hesitation_events", 0),
              data.get("time_to_first_action"), data.get("first_highlight_at"),
              data.get("first_decision_at")))
        
        return jsonify({"id": cur.lastrowid, "decision_sequence": seq})

@app.route("/api/highlight-metrics", methods=["POST"])
@require_access
def track_highlight_metrics():
    """Track highlight behavior metrics."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO highlight_metrics 
            (session_id, annotator_id, paper_id, total_highlights, color_switches,
             self_corrections, highlight_density)
            VALUES (?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              data.get("total_highlights", 0), data.get("color_switches", 0),
              data.get("self_corrections", 0), data.get("highlight_density", 0)))
        
        return jsonify({"id": cur.lastrowid})

@app.route("/api/extraction-quality", methods=["POST"])
@require_access
def track_extraction_quality():
    """Track extraction quality metrics including copy-paste detection."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id", "element"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    # Calculate similarity score if source and extraction provided
    copy_paste_score = 0
    if data.get("extracted_text") and data.get("source_text"):
        from difflib import SequenceMatcher
        copy_paste_score = SequenceMatcher(
            None, data["extracted_text"], data["source_text"]
        ).ratio()
    
    # Count inference markers
    inference_markers = 0
    if data.get("extracted_text"):
        marker_words = ["suggests", "implies", "indicates", "likely", "probably", 
                       "may", "might", "could", "appears", "seems"]
        text_lower = data["extracted_text"].lower()
        inference_markers = sum(text_lower.count(word) for word in marker_words)
    
    # Detect paraphrase (not exact copy but similar)
    paraphrase_detected = 0
    if copy_paste_score > 0.3 and copy_paste_score < 0.9:
        paraphrase_detected = 1
    
    elaboration_ratio = 0
    if data.get("extracted_text") and data.get("source_text"):
        src_words = len(data["source_text"].split())
        ext_words = len(data["extracted_text"].split())
        if src_words > 0:
            elaboration_ratio = ext_words / src_words
    
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO extraction_quality 
            (session_id, annotator_id, paper_id, element, extraction_method,
             copy_paste_score, word_count, elaboration_ratio, paraphrase_detected, inference_markers)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"], data["element"],
              data.get("extraction_method", "manual"), copy_paste_score,
              len(data.get("extracted_text", "").split()), elaboration_ratio,
              paraphrase_detected, inference_markers))
        
        return jsonify({"id": cur.lastrowid, "copy_paste_score": round(copy_paste_score, 3)})

@app.route("/api/export/cognitive-metrics")
@require_admin
def export_cognitive_metrics():
    """Export all cognitive metrics for analysis."""
    with get_db() as db:
        page_visits = db.execute("""
            SELECT pv.*, a.name, p.title FROM page_visits pv
            JOIN annotators a ON a.id = pv.annotator_id
            JOIN papers p ON p.id = pv.paper_id
            ORDER BY pv.visit_start
        """).fetchall()
        
        cognitive_load = db.execute("""
            SELECT cl.*, a.name, p.title FROM cognitive_load cl
            JOIN annotators a ON a.id = cl.annotator_id
            JOIN papers p ON p.id = cl.paper_id
            ORDER BY cl.created_at
        """).fetchall()
        
        extraction_quality = db.execute("""
            SELECT eq.*, a.name, p.title FROM extraction_quality eq
            JOIN annotators a ON a.id = eq.annotator_id
            JOIN papers p ON p.id = eq.paper_id
            ORDER BY eq.timestamp
        """).fetchall()
    
    return jsonify({
        "page_visits": [dict(r) for r in page_visits],
        "cognitive_load": [dict(r) for r in cognitive_load],
        "extraction_quality": [dict(r) for r in extraction_quality]
    })

@app.route("/api/decision-history", methods=["POST"])
@require_access
def track_decision_history():
    """Track decision changes and flips."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id", "new_decision"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        # Count previous flips for this session
        prev_flips = db.execute(
            "SELECT COUNT(*) FROM decision_history WHERE session_id=?",
            (data["session_id"],)
        ).fetchone()[0]
        
        cur = db.execute("""
            INSERT INTO decision_history 
            (session_id, annotator_id, paper_id, previous_decision, new_decision,
             previous_confidence, new_confidence, change_reason, flip_count)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              data.get("previous_decision"), data["new_decision"],
              data.get("previous_confidence"), data.get("new_confidence"),
              data.get("change_reason"), prev_flips + 1))
        
        return jsonify({"id": cur.lastrowid, "flip_count": prev_flips + 1})

@app.route("/api/reading-patterns", methods=["POST"])
@require_access
def track_reading_patterns():
    """Track reading patterns and navigation."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO reading_patterns 
            (session_id, annotator_id, paper_id, reading_order, backward_navigations,
             section_dwell_times, linear_reading_ratio, total_pages_viewed,
             unique_pages_viewed, revisit_patterns)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              json.dumps(data.get("reading_order", [])),
              data.get("backward_navigations", 0),
              json.dumps(data.get("section_dwell_times", {})),
              data.get("linear_reading_ratio", 0),
              data.get("total_pages_viewed", 0),
              data.get("unique_pages_viewed", 0),
              json.dumps(data.get("revisit_patterns", {}))))
        
        return jsonify({"id": cur.lastrowid})

@app.route("/api/llm-comparison", methods=["POST"])
@require_access
def track_llm_comparison():
    """Track human vs LLM extraction comparison."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id", "element"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    # Calculate similarity score
    similarity = 0
    if data.get("llm_extraction") and data.get("human_extraction"):
        from difflib import SequenceMatcher
        similarity = SequenceMatcher(None, data["llm_extraction"], 
                                       data["human_extraction"]).ratio()
    
    # Determine agreement type
    agreement = "missing"
    if similarity > 0.9:
        agreement = "exact"
    elif similarity > 0.5:
        agreement = "partial"
    elif similarity > 0:
        agreement = "contradictory"
    
    with get_db() as db:
        # Get review sequence
        review_seq = db.execute(
            "SELECT COUNT(*) FROM llm_comparison WHERE session_id=?",
            (data["session_id"],)
        ).fetchone()[0] + 1
        
        cur = db.execute("""
            INSERT INTO llm_comparison 
            (session_id, annotator_id, paper_id, element, llm_extraction,
             human_extraction, similarity_score, agreement_type, human_review_time_sec,
             human_modifications, review_sequence)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"], data["element"],
              data.get("llm_extraction"), data.get("human_extraction"),
              similarity, agreement, data.get("human_review_time_sec"),
              data.get("human_modifications", 0), review_seq))
        
        return jsonify({"id": cur.lastrowid, "similarity": round(similarity, 3), "agreement": agreement})

@app.route("/api/task-switches", methods=["POST"])
@require_access
def track_task_switches():
    """Track switching between tasks (screening/extraction/highlight)."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id", "from_task", "to_task"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        cur = db.execute("""
            INSERT INTO task_switches 
            (session_id, annotator_id, paper_id, from_task, to_task,
             switch_time_sec, switch_reason, session_duration_at_switch)
            VALUES (?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              data["from_task"], data["to_task"], data.get("switch_time_sec"),
              data.get("switch_reason"), data.get("session_duration_at_switch")))
        
        return jsonify({"id": cur.lastrowid})

@app.route("/api/fatigue-indicators", methods=["POST"])
@require_access
def track_fatigue_indicators():
    """Track fatigue and motivation indicators over time."""
    data = request.json
    required = ["session_id", "annotator_id", "paper_id"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    
    with get_db() as db:
        # Calculate decision time trend
        avg_time = db.execute(
            "SELECT AVG(decision_time_sec) FROM cognitive_load WHERE annotator_id=?",
            (data["annotator_id"],)
        ).fetchone()[0] or 0
        
        current_time = (data.get("response_time_ms") or 0) / 1000  # convert to seconds
        trend = current_time / avg_time if avg_time > 0 else 1.0
        
        cur = db.execute("""
            INSERT INTO fatigue_indicators 
            (session_id, annotator_id, paper_id, time_into_session_sec, response_time_ms,
             highlight_accuracy, confidence_value, consecutive_similar_conf, words_per_minute,
             scroll_velocity, mouse_idle_time_sec, decision_time_trend)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (data["session_id"], data["annotator_id"], data["paper_id"],
              data.get("time_into_session_sec"), data.get("response_time_ms"),
              data.get("highlight_accuracy"), data.get("confidence_value"),
              data.get("consecutive_similar_conf", 0), data.get("words_per_minute"),
              data.get("scroll_velocity"), data.get("mouse_idle_time_sec"), trend))
        
        return jsonify({"id": cur.lastrowid, "decision_time_trend": round(trend, 2)})

@app.route("/api/export/advanced-metrics")
@require_admin
def export_advanced_metrics():
    """Export all advanced cognitive metrics."""
    with get_db() as db:
        decision_history = db.execute("""
            SELECT dh.*, a.name, p.title FROM decision_history dh
            JOIN annotators a ON a.id = dh.annotator_id
            JOIN papers p ON p.id = dh.paper_id
            ORDER BY dh.timestamp
        """).fetchall()
        
        reading_patterns = db.execute("""
            SELECT rp.*, a.name, p.title FROM reading_patterns rp
            JOIN annotators a ON a.id = rp.annotator_id
            JOIN papers p ON p.id = rp.paper_id
            ORDER BY rp.timestamp
        """).fetchall()
        
        llm_comparison = db.execute("""
            SELECT lc.*, a.name, p.title FROM llm_comparison lc
            JOIN annotators a ON a.id = lc.annotator_id
            JOIN papers p ON p.id = lc.paper_id
            ORDER BY lc.timestamp
        """).fetchall()
        
        task_switches = db.execute("""
            SELECT ts.*, a.name, p.title FROM task_switches ts
            JOIN annotators a ON a.id = ts.annotator_id
            JOIN papers p ON p.id = ts.paper_id
            ORDER BY ts.timestamp
        """).fetchall()
        
        fatigue_indicators = db.execute("""
            SELECT fi.*, a.name, p.title FROM fatigue_indicators fi
            JOIN annotators a ON a.id = fi.annotator_id
            JOIN papers p ON p.id = fi.paper_id
            ORDER BY fi.timestamp
        """).fetchall()
    
    return jsonify({
        "decision_history": [dict(r) for r in decision_history],
        "reading_patterns": [dict(r) for r in reading_patterns],
        "llm_comparison": [dict(r) for r in llm_comparison],
        "task_switches": [dict(r) for r in task_switches],
        "fatigue_indicators": [dict(r) for r in fatigue_indicators]
    })

@app.route("/api/inter-annotator-agreement/<paper_id>")
@require_admin
def calculate_inter_annotator_agreement(paper_id):
    """Calculate Fleiss' kappa for inter-annotator agreement on a paper."""
    with get_db() as db:
        # Get all decisions for this paper
        decisions = db.execute("""
            SELECT annotator_id, decision, confidence 
            FROM screening_decisions 
            WHERE paper_id = ?
        """, (paper_id,)).fetchall()
        
        if len(decisions) < 2:
            return jsonify({"error": "Need at least 2 annotators for agreement calculation"}), 400
        
        # Get all extractions for this paper by element
        extractions = db.execute("""
            SELECT annotator_id, element, extracted_text
            FROM pico_extractions
            WHERE paper_id = ?
        """, (paper_id,)).fetchall()
        
        # Group by element
        by_element = {}
        for row in extractions:
            if row["element"] not in by_element:
                by_element[row["element"]] = []
            by_element[row["element"]].append(dict(row))
        
        # Calculate simple agreement percentage for decisions
        decision_values = [r["decision"] for r in decisions]
        from collections import Counter
        decision_counts = Counter(decision_values)
        total = len(decisions)
        agreement_pct = max(decision_counts.values()) / total * 100 if total > 0 else 0
        
        return jsonify({
            "paper_id": paper_id,
            "annotator_count": len(decisions),
            "decision_agreement": {
                "percentage": round(agreement_pct, 1),
                "distribution": dict(decision_counts)
            },
            "extraction_counts_by_element": {k: len(v) for k, v in by_element.items()},
            "extraction_details": by_element
        })

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
    print(f"\n Annotation Tool")
    print(f"   http://localhost:5050")
    print(f"   Admin dashboard: http://localhost:5050/admin")
    print(access)
    print(admin)
    print(f"\n   PDFs → papers/  |  DB → synergy_annotations.db\n")
    app.run(debug=False, port=5050, host="0.0.0.0")
