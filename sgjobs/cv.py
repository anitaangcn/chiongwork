"""Match your CV against every job, entirely on this PC (no AI service, nothing uploaded anywhere).

How it works
- The skill vocabulary comes from the live jobs themselves: MyCareersFuture tags every job with
  skills, so ~14,000 skills that employers actually ask for are known.
- Your CV is read (PDF, Word or text) and every known skill phrase in it is picked out. You can add
  or remove skills afterwards; your edits always win.
- Each job's score is the share of its skills you have, weighted by how specific each skill is:
  "IFRS 9" or "threat hunting" count far more than "team player". Jobs with only a few generic
  skills can't reach a high score. A big experience gap lowers the score.
- Scores for all live jobs are stored in the job cache (table cv_match) and refreshed after every
  fetch and every CV change.
"""
import io
import json
import math
import re
import threading
import zipfile
from datetime import date

from . import db, userdata

MIN_DF = 3                # a skill must appear on at least this many live jobs to count
GENERIC_DF = 2500         # skills on more jobs than this are "general" (Excel, teamwork...)
WEIGHT_FLOOR = 12.0       # jobs whose skills add up to less than this can't score 100%
STRONG, GOOD = 60, 45     # match bands shown in the app
_LOCK = threading.Lock()
_vocab_cache = {}
BLOCK = {"it", "go", "r", "c", "e", "as", "the", "and", "a", "an", "in", "on", "road", "job descriptions",
         "resume writing", "able to work independently", "interviewing", "recruiting", "screening"}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*(?:\.[a-z0-9]+)*")


def tokens(text):
    return _TOKEN.findall((text or "").lower().replace("&", " and "))


def norm(skill):
    return " ".join(tokens(skill))


def canon(s):
    """'microsoft excel' = 'excel', 'cyber security' = 'cybersecurity'."""
    return re.sub(r"^(microsoft|ms|ms\.) ", "", s).replace(" ", "")


# ---------------------------------------------------------------- reading files
def extract_text(name, raw):
    n = (name or "").lower()
    if n.endswith(".pdf") or raw[:4] == b"%PDF":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if n.endswith(".docx") or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    if n.endswith(".doc"):
        raise ValueError("Old .doc files can't be read. In Word, use File → Save As → .docx or PDF, then upload that.")
    return raw.decode("utf-8", "replace")


def years_from_text(text):
    """Rough years of experience from date ranges like '2019 – Present' or 'Mar 2016 - Jun 2020'."""
    this_year = date.today().year
    spans = []
    school = re.compile(r"universit|college|polytechnic|bachelor|master|diploma|degree|school|\bnus\b|\bntu\b|\bsmu\b|"
                        r"\bmba\b|\bbsc\b|\bba\b|\bphd\b|education|graduat|a.levels?|o.levels?|\bite\b|national service", re.I)
    for line in (text or "").splitlines():
        if school.search(line):  # study years aren't work experience
            continue
        for a, b in re.findall(r"\b((?:19|20)\d{2})\s*(?:-|–|—|to)\s*(?:[A-Za-z]{3,9}\.?\s+)?((?:19|20)\d{2}|present|current|now|today|date)\b",
                               line, re.I):
            start = int(a)
            end = this_year if not b[0].isdigit() else int(b)
            if 1960 <= start <= end <= this_year:
                spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    total, cur_s, cur_e = 0, *spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return min(total, 45) or None


# ---------------------------------------------------------------- vocabulary from live jobs
def vocab(conn):
    """{normalised skill: (display name, number of live jobs asking for it)}"""
    key = db.meta_get(conn, "last_fetch")
    if _vocab_cache.get("key") == key:
        return _vocab_cache["v"]
    counts, display = {}, {}
    for (skills,) in conn.execute(f"SELECT skills FROM jobs j WHERE {db.active_clause()} AND skills IS NOT NULL"):
        for s in {x.strip() for x in skills.split(",") if x.strip()}:
            k = norm(s)
            if k and k not in BLOCK and len(k) > 1:
                counts[k] = counts.get(k, 0) + 1
                display.setdefault(k, s)
    v = {k: (display[k], n) for k, n in counts.items() if n >= MIN_DF}
    vague = _vague_words(conn, [k for k in v if " " not in k and not _techy(k, v[k][0])])
    v = {k: x for k, x in v.items() if k not in vague}
    _vocab_cache.update(key=key, v=v, vague=vague, n=max(1, conn.execute(
        f"SELECT COUNT(*) FROM jobs j WHERE {db.active_clause()}").fetchone()[0]))
    return v


TECH_WORDS = {"python", "java", "javascript", "typescript", "linux", "excel", "react", "angular", "docker", "kubernetes",
              "tableau", "salesforce", "splunk", "git", "golang", "kotlin", "swift", "ruby", "php", "scala", "matlab",
              "autocad", "solidworks", "revit", "figma", "photoshop", "illustrator", "jira", "terraform", "ansible",
              "nessus", "wireshark", "snowflake", "databricks", "spark", "hadoop", "kafka", "pandas", "tensorflow",
              "pytorch", "powershell", "bash", "unix", "oracle", "mysql", "postgresql", "mongodb", "excel", "xero",
              "quickbooks", "netsuite", "workday", "servicenow", "sharepoint", "cybersecurity", "phishing", "forensics",
              "accounting", "auditing", "bookkeeping", "nursing", "phlebotomy", "welding", "plumbing", "carpentry"}


def _techy(k, display):
    """Acronyms (SQL, AML, SIEM), names with digits/symbols (C++, ISO 27001) or known tools are real skills."""
    return k in TECH_WORDS or bool(re.search(r"[\d+#.]", k)) or (display.isupper() and 2 <= len(display) <= 7)


def _vague_words(conn, singles, sample=4000, threshold=0.02):
    """Single words that are everyday job-ad vocabulary ("management", "risk", "experience") rather than skills:
    they show up in more than 2% of all job descriptions."""
    if not singles:
        return set()
    want = set(singles)
    seen = {}
    rows = conn.execute(f"SELECT description FROM jobs j WHERE {db.active_clause()} AND description IS NOT NULL "
                        f"ORDER BY (j.id * 2654435761) % 1000003 LIMIT ?", (sample,)).fetchall()
    for (d,) in rows:
        for t in set(tokens(d)) & want:
            seen[t] = seen.get(t, 0) + 1
    n = max(1, len(rows))
    return {w for w, c in seen.items() if c / n > threshold}


def _weight(df, n_jobs):
    """Specific skills (on few jobs) count up to 5; skills on thousands of jobs (Excel, teamwork) ~0.2-0.7."""
    return min(5.0, max(0.2, math.log(n_jobs / max(df, 1)) - 1.5))


def find_skills(text, voc):
    """Every known skill phrase (1-5 words) that appears in the text."""
    toks = tokens(text)
    found = {}
    for size in range(1, 6):
        for i in range(len(toks) - size + 1):
            k = " ".join(toks[i:i + size])
            if k in voc and k not in found:
                found[k] = voc[k]
    return found


# ---------------------------------------------------------------- your CV
def get_cv():
    return userdata.load_key("cv")


def cv_skills(cv):
    removed = set(cv.get("removed") or [])
    return {k for k in (cv.get("found") or []) + (cv.get("added") or []) if k not in removed}


def save_cv(conn, name, text):
    text = (text or "").strip()
    if len(text) < 200:
        raise ValueError("That doesn't look like a CV: almost no text could be read. If it's a scanned PDF, "
                         "export it from Word/Google Docs as a PDF with real text, or paste the text instead.")
    voc = vocab(conn)
    found = find_skills(text, voc)
    old = get_cv() or {}
    cv = {"filename": name, "uploaded": db.now(), "chars": len(text), "text": text[:60000],
          "found": sorted(found), "added": old.get("added") or [], "removed": old.get("removed") or [],
          "years": years_from_text(text), "years_set_by_you": False}
    if old.get("years_set_by_you"):
        cv.update(years=old.get("years"), years_set_by_you=True)
    userdata.save_key("cv", cv)
    compute(conn)
    return cv


def edit_cv(conn, add=(), remove=(), years=None):
    cv = get_cv()
    if not cv:
        raise ValueError("Upload your CV first")
    voc = vocab(conn)
    added, removed = list(cv.get("added") or []), set(cv.get("removed") or [])
    for s in add:
        k = norm(s)
        if not k:
            continue
        removed.discard(k)
        if k not in cv["found"] and k not in added:
            added.append(k)
        if k not in voc:  # unknown to job ads: keep it, it just won't match anything yet
            cv.setdefault("custom_names", {})[k] = s.strip()
    for s in remove:
        k = norm(s)
        if k in added:
            added.remove(k)
        else:
            removed.add(k)
    cv["added"], cv["removed"] = added, sorted(removed)
    if years is not None:
        cv["years"] = int(years) if str(years).isdigit() else None
        cv["years_set_by_you"] = True
    userdata.save_key("cv", cv)
    compute(conn)
    return cv


def delete_cv(conn):
    userdata.save_key("cv", None)
    conn.execute("DELETE FROM cv_match")
    db.meta_set(conn, "cv_match_key", "")
    conn.commit()


# ---------------------------------------------------------------- scoring every job
def _job_skills(row, voc):
    """(skills, tagged): MCF tags skills itself; for other jobs they're read from the ad text."""
    if row["skills"]:
        ks = {norm(s) for s in row["skills"].split(",") if s.strip()}
        return {k for k in ks if k in voc}, True
    # no skill tags (career pages, alert emails): pick specific skills out of the title + description
    found = find_skills(f"{row['title']}\n{row['description'] or ''}", voc)
    specific = [k for k, (_, df) in found.items() if df <= GENERIC_DF * 2]
    return set(sorted(specific, key=lambda k: found[k][1])[:15]), False


TITLE_STOP = {"and", "or", "of", "the", "for", "in", "a", "an", "to", "with", "at", "sg", "singapore", "job", "jobs",
              "urgent", "hiring", "new", "role", "based", "per", "month", "contract", "permanent", "full", "time", "part"}


def _stem(t):
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def title_tokens(title):
    from .util import SENIORITY
    return {_stem(t) for t in tokens(title) if t not in TITLE_STOP and t not in SENIORITY and not t.isdigit() and len(t) > 1}


def score_job(row, mine, mine_canon, voc, n_jobs, years, cv_words, title_df):
    js, tagged = _job_skills(row, voc)
    if not js or (not tagged and len(js) < 3):  # too little to judge from the ad text
        return None
    have, missing = [], []
    for k in js:
        w = _weight(voc[k][1], n_jobs)
        (have if (k in mine or canon(k) in mine_canon) else missing).append((w, voc[k][0], k))
    # Skills: how many of the job's 10 most specific skills you have (generic tags barely count).
    top = sorted(have + missing, reverse=True)[:10]
    got = sum(w for w, _, k in top if (k in mine or canon(k) in mine_canon))
    skill = got / max(sum(w for w, _, _ in top), WEIGHT_FLOOR if tagged else WEIGHT_FLOOR * 1.5)
    # Title: do the job title's distinctive words appear in your CV (past titles, summary)?
    tt = title_tokens(row["title"])
    tw = {t: math.log(n_jobs / max(title_df.get(t, 1), 1)) for t in tt}
    # one or two common words ("Operations Manager", "Engineer") can't prove much: divide by at least 9
    title = sum(w for t, w in tw.items() if t in cv_words) / max(sum(tw.values()), 9.0) if tw else 0
    score = 100 * (0.6 * min(skill, 1) + 0.4 * title)
    if skill < 0.15:  # a title echo alone isn't a match
        score = min(score, 25)
    gap = None
    if years is not None and row["min_experience"] is not None and row["min_experience"] - years >= 3:
        gap = int(row["min_experience"])
        score *= 0.75
    return (round(min(score, 100)), [n for _, n, _ in sorted(have, reverse=True)][:12],
            [n for _, n, _ in sorted(missing, reverse=True)][:10], gap)


def compute(conn, force=True):
    """(Re)score every live job. Fast enough (~2-3 s) to run after each change."""
    with _LOCK:
        cv = get_cv()
        if not cv:
            conn.execute("DELETE FROM cv_match")
            conn.commit()
            return 0
        key = json.dumps([db.meta_get(conn, "last_fetch"), sorted(cv_skills(cv)), cv.get("years")])
        if not force and db.meta_get(conn, "cv_match_key") == key:
            return None
        voc = vocab(conn)
        n_jobs = _vocab_cache["n"]
        mine = cv_skills(cv)
        mine_canon = {canon(k) for k in mine}
        rows = conn.execute(f"SELECT id, title, description, skills, min_experience FROM jobs j WHERE {db.active_clause()}").fetchall()
        title_df = {}
        for r in rows:
            for t in title_tokens(r["title"]):
                title_df[t] = title_df.get(t, 0) + 1
        cv_words = {_stem(t) for t in tokens(cv.get("text") or "")} | {_stem(t) for k in mine for t in k.split()}
        out = []
        for r in rows:
            res = score_job(r, mine, mine_canon, voc, n_jobs, cv.get("years"), cv_words, title_df)
            if res:
                s, have, missing, gap = res
                out.append((r["id"], s, "|".join(have), "|".join(missing), gap))
        conn.execute("DELETE FROM cv_match")
        conn.executemany("INSERT INTO cv_match(job_id, score, have, missing, exp_gap) VALUES(?,?,?,?,?)", out)
        db.meta_set(conn, "cv_match_key", key)
        conn.commit()
        return len(out)


def match_for(conn, job_id):
    r = conn.execute("SELECT score, have, missing, exp_gap FROM cv_match WHERE job_id=?", (job_id,)).fetchone()
    if not r:
        return None
    cv = get_cv() or {}
    return {"score": r["score"], "have": [x for x in (r["have"] or "").split("|") if x],
            "missing": [x for x in (r["missing"] or "").split("|") if x],
            "exp_gap": r["exp_gap"], "years": cv.get("years")}


# ---------------------------------------------------------------- the "My CV" page
def overview(conn, role_where=None, role_params=()):
    cv = get_cv()
    if not cv:
        return {"has": False}
    voc = vocab(conn)
    custom = cv.get("custom_names") or {}
    added = set(cv.get("added") or [])
    skills = []
    for k in sorted(cv_skills(cv)):
        name, df = voc.get(k, (custom.get(k, k), 0))
        skills.append({"key": k, "name": name, "jobs": df, "general": df > GENERIC_DF, "added": k in added})
    skills.sort(key=lambda s: (s["general"], -s["jobs"]))
    act = db.active_clause()
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
    bands = {"strong": q("SELECT COUNT(*) FROM cv_match WHERE score >= ?", STRONG),
             "good": q("SELECT COUNT(*) FROM cv_match WHERE score >= ? AND score < ?", GOOD, STRONG),
             "some": q("SELECT COUNT(*) FROM cv_match WHERE score >= 30 AND score < ?", GOOD),
             "scored": q("SELECT COUNT(*) FROM cv_match")}
    # skills that keep coming up in the jobs you're closest to: add them to your CV if you have them
    extra = f" AND {role_where}" if role_where else ""
    rows = conn.execute(f"SELECT m.missing FROM cv_match m JOIN jobs j ON j.id=m.job_id WHERE m.score >= 35 AND {act}{extra} "
                        f"ORDER BY m.score DESC LIMIT 400", tuple(role_params)).fetchall()
    tally = {}
    for (miss,) in rows:
        for s in (miss or "").split("|"):
            if s:
                tally[s] = tally.get(s, 0) + 1
    learn = [{"skill": s, "jobs": n} for s, n in sorted(tally.items(), key=lambda kv: -kv[1])[:16] if n >= 3]
    return {"has": True, "filename": cv.get("filename"), "uploaded": cv.get("uploaded"), "chars": cv.get("chars"),
            "years": cv.get("years"), "years_set_by_you": cv.get("years_set_by_you"), "skills": skills,
            "removed": [voc.get(k, (k,))[0] for k in cv.get("removed") or []], "bands": bands,
            "learn": learn, "near_jobs": len(rows)}


def suggest(conn, q, limit=8):
    q = norm(q)
    if not q:
        return []
    voc = vocab(conn)
    hits = [(df, name) for k, (name, df) in voc.items() if q in k]
    return [name for df, name in sorted(hits, key=lambda x: (-x[0]))[:limit]]
