"""SQLite store. A *job* is one real vacancy; a *listing* is where we saw it (MCF, a company
career page, a LinkedIn alert...). Several listings collapse into one job when they match."""
import json
import sqlite3
from datetime import date, datetime
from difflib import SequenceMatcher

from .util import CACHE_DIR, ROOT, fingerprint, is_hype, norm_company, norm_title, seniority

DB_PATH = CACHE_DIR / "sgjobs.db"
OLD_DB_PATH = ROOT / "data" / "sgjobs.db"  # before 2026-11-17 the cache lived in the project folder

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  fp TEXT, company_norm TEXT, company_core TEXT, title_norm TEXT,
  title TEXT, company TEXT, location TEXT,
  salary_min INTEGER, salary_max INTEGER, salary_note TEXT, salary_source TEXT, salary_period TEXT,
  employment_type TEXT, level TEXT, category TEXT, skills TEXT, min_experience INTEGER,
  posted TEXT, original_posted TEXT, expiry TEXT,
  applicants INTEGER, views INTEGER, reposts INTEGER, responsive INTEGER, agency INTEGER,
  description TEXT,
  first_seen TEXT, last_seen TEXT,
  status TEXT, note TEXT, status_updated TEXT
);
CREATE INDEX IF NOT EXISTS jobs_fp ON jobs(fp);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  source TEXT, ext_id TEXT, url TEXT, title TEXT, company TEXT,
  first_seen TEXT, last_seen TEXT, closed INTEGER DEFAULT 0,
  UNIQUE(source, ext_id)
);
CREATE INDEX IF NOT EXISTS listings_job ON listings(job_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cv_match (job_id INTEGER PRIMARY KEY, score INTEGER, have TEXT, missing TEXT, exp_gap INTEGER);
"""

JOB_FIELDS = ["location", "salary_min", "salary_max", "salary_period", "salary_note", "employment_type", "level", "category",
              "skills", "min_experience", "posted", "original_posted", "expiry", "applicants", "views",
              "reposts", "responsive", "agency", "description", "lat", "lng", "postal", "flex"]
# Fields where the government portal is the authority, so its value wins over others.
MCF_WINS = {"salary_min", "salary_max", "salary_period", "applicants", "views", "reposts", "responsive", "agency",
            "expiry", "original_posted", "description", "skills", "level", "category", "min_experience",
            "lat", "lng", "postal", "flex"}
ATS_SOURCES = {"greenhouse", "lever", "ashby", "smartrecruiters"}


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and OLD_DB_PATH.exists() and OLD_DB_PATH != DB_PATH:
        _move_old_db()
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.create_function("hype", 2, lambda t, c: is_hype(t, c, HYPE_COMPANIES))
    try:  # WAL lets the web app keep reading while a fetch is writing
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "company_core" not in cols:  # older DBs
        conn.execute("ALTER TABLE jobs ADD COLUMN company_core TEXT")
        for r in conn.execute("SELECT id, company_norm FROM jobs").fetchall():
            conn.execute("UPDATE jobs SET company_core=? WHERE id=?", (_core(r[1]), r[0]))
        conn.commit()
    if "salary_period" not in cols:  # older DBs: hourly rates typed into MCF's monthly field
        conn.execute("ALTER TABLE jobs ADD COLUMN salary_period TEXT")
        conn.execute("UPDATE jobs SET salary_period = CASE WHEN salary_max < 100 THEN 'hour' ELSE 'month' END "
                     "WHERE salary_max IS NOT NULL")
        conn.commit()
    for col, typ in (("lat", "REAL"), ("lng", "REAL"), ("postal", "TEXT"), ("flex", "TEXT"), ("home_km", "REAL")):
        if col not in cols:  # workplace location (from MCF) and distance from your home (see geo.py)
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
            conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_core ON jobs(company_core)")
    global HYPE_COMPANIES
    if not HYPE_COMPANIES:
        saved = meta_get(conn, "hype_companies")
        if saved is None and conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
            refresh_hype_companies(conn)
        elif saved:
            HYPE_COMPANIES = frozenset(json.loads(saved))
    return conn


def _core(company_norm):
    """First word of the normalised company name: 'stripe payments' -> 'stripe'. Indexed for fast matching."""
    return (company_norm or "").split(" ")[0] or None


def _move_old_db():
    """One-time move of the job cache out of the (OneDrive-synced) project folder."""
    import shutil
    old = sqlite3.connect(OLD_DB_PATH, timeout=30)
    old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    old.execute("PRAGMA journal_mode=DELETE")
    old.close()
    shutil.move(str(OLD_DB_PATH), str(DB_PATH))
    for ext in ("-wal", "-shm", "-journal"):
        f = OLD_DB_PATH.with_name(OLD_DB_PATH.name + ext)
        if f.exists():
            f.unlink()


HYPE_COMPANIES = frozenset()


def refresh_hype_companies(conn):
    """Companies with 3+ hype-style ads (by title alone). Recomputed after each fetch."""
    global HYPE_COMPANIES
    counts = {}
    for t, c in conn.execute(f"SELECT title, company FROM jobs j WHERE {active_clause()}"):
        if is_hype(t):
            counts[c] = counts.get(c, 0) + 1
    HYPE_COMPANIES = frozenset(c for c, n in counts.items() if n >= 3)
    meta_set(conn, "hype_companies", json.dumps(sorted(HYPE_COMPANIES)))
    conn.commit()


def now():
    return datetime.now().isoformat(timespec="seconds")


def meta_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def _find_fuzzy(conn, L, cnorm, tnorm):
    """Same company (by core name token or alias) and near-identical title, from a different source."""
    keys = {cnorm} | {norm_company(a) for a in L.get("company_aliases", [])}
    keys = {k for k in keys if len(k) >= 3}
    if not keys or not tnorm:
        return None
    best, best_score = None, 0.0
    for k in keys:
        core = k.split()[0]
        if len(core) < 3:
            continue
        rows = conn.execute(
            "SELECT j.id, j.title_norm, GROUP_CONCAT(l.source) AS srcs FROM jobs j JOIN listings l ON l.job_id=j.id "
            "WHERE j.company_core = ? AND EXISTS (SELECT 1 FROM listings o WHERE o.job_id=j.id "
            "AND o.source != ?) GROUP BY j.id LIMIT 400",
            (core, L["source"]),
        ).fetchall()
        for r in rows:
            if L["source"] in (r["srcs"] or "").split(","):
                continue  # fuzzy matching is only for the same job seen on a *different* site
            if seniority(tnorm) != seniority(r["title_norm"] or ""):
                continue  # "Staff Engineer" and "Engineer" are different jobs
            a, b = set(tnorm.split()), set((r["title_norm"] or "").split())
            if a - b and b - a:
                continue  # "Engineer, Billing" vs "Engineer, Lending": each has a word the other lacks
            score = SequenceMatcher(None, tnorm, r["title_norm"] or "").ratio()
            if score > best_score:
                best, best_score = r["id"], score
    return best if best_score >= 0.9 else None


def upsert(conn, L, ts):
    """Insert or merge one listing. Returns 'new', 'merged' or 'seen'."""
    row = conn.execute("SELECT id, job_id FROM listings WHERE source=? AND ext_id=?",
                       (L["source"], str(L["ext_id"]))).fetchone()
    if row:
        conn.execute("UPDATE listings SET last_seen=?, closed=0, url=COALESCE(?, url) WHERE id=?",
                     (ts, L.get("url"), row["id"]))
        _merge_fields(conn, row["job_id"], L, ts)
        return "seen"

    cnorm, tnorm = norm_company(L.get("company")), norm_title(L.get("title"))
    fp = fingerprint(L.get("company"), L.get("title"))
    job_id = None
    if cnorm and tnorm:
        r = conn.execute(
            "SELECT j.id FROM jobs j WHERE j.fp=? AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.job_id=j.id "
            "AND l.source=? AND l.source IN ('greenhouse','lever','ashby','smartrecruiters'))",
            (fp, L["source"]),
        ).fetchone()
        job_id = r["id"] if r else _find_fuzzy(conn, L, cnorm, tnorm)

    result = "merged" if job_id else "new"
    if not job_id:
        cur = conn.execute(
            "INSERT INTO jobs(fp, company_norm, company_core, title_norm, title, company, first_seen, last_seen) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (fp, cnorm, _core(cnorm), tnorm, L.get("title"), L.get("company"), ts, ts),
        )
        job_id = cur.lastrowid
    conn.execute(
        "INSERT INTO listings(job_id, source, ext_id, url, title, company, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?)",
        (job_id, L["source"], str(L["ext_id"]), L.get("url"), L.get("title"), L.get("company"), ts, ts),
    )
    _merge_fields(conn, job_id, L, ts)
    return result


def _merge_fields(conn, job_id, L, ts):
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    sets, vals = ["last_seen=?"], [ts]
    for f in JOB_FIELDS:
        v = L.get(f)
        if v in (None, ""):
            continue
        if job[f] in (None, "") or (L["source"] == "mcf" and f in MCF_WINS and job[f] != v):
            sets.append(f"{f}=?")
            vals.append(v)
    if L.get("salary_min") and (job["salary_min"] is None or L["source"] == "mcf"):
        sets.append("salary_source=?")
        vals.append(L["source"])
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*vals, job_id))


def close_missing(conn, source, prefix, ts):
    """Career pages list every open role, so anything not seen in this fetch has closed."""
    conn.execute("UPDATE listings SET closed=1 WHERE source=? AND ext_id LIKE ? AND last_seen<?",
                 (source, f"{prefix}:%", ts))


def set_status(conn, job_id, status, note=None):
    conn.execute("UPDATE jobs SET status=?, status_updated=?, note=COALESCE(?, note) WHERE id=?",
                 (status, now(), note, job_id))


def active_clause():
    today = date.today().isoformat()
    # A live career-page listing is the best proof a job is open, so it overrides a guessed expiry.
    return (f"(j.expiry IS NULL OR j.expiry >= '{today}' OR EXISTS (SELECT 1 FROM listings l3 WHERE "
            "l3.job_id=j.id AND l3.closed=0 AND l3.source IN ('greenhouse','lever','ashby','smartrecruiters'))) "
            "AND EXISTS (SELECT 1 FROM listings l2 WHERE l2.job_id=j.id AND l2.closed=0)")
