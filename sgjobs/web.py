"""Local web app for ChiongWork: `cw web` → http://127.0.0.1:8700

Standard library only. Binds to 127.0.0.1, so nothing is reachable from other machines.
Write requests need an `X-SGJobs` header, which other websites can't send to localhost
without a CORS preflight this server never approves.
"""
import email
import email.policy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from . import cv, db, feed, geo, gmail, tracker, userdata
from .sources import email_alerts
from .util import CACHE_DIR, ROOT

STATIC = Path(__file__).parent / "web"
LOG = CACHE_DIR / "fetch.log"
_fetch = {"running": False, "started": None, "finished": None, "code": None, "mode": None}
_cache = {}
_gm = {"running": False, "error": None, "result": None}
_lock = threading.Lock()

# Starting points for the setup screen (job counts are filled in from your feed).
ROLE_SUGGESTIONS = ["Data Analyst", "Software Engineer", "Accountant", "Marketing Executive", "Admin Assistant",
                    "Sales Executive", "HR Executive", "Business Analyst", "Project Manager", "Customer Service",
                    "Operations Executive", "Graphic Designer", "Product Manager", "Account Executive",
                    "Data Scientist", "Nurse", "Teacher", "Logistics", "Finance Manager", "UX Designer"]
LEVEL_GROUPS = {  # friendly name -> MyCareersFuture position levels
    "Fresh grad / entry": ["Fresh/entry level", "Non-executive"],
    "Executive": ["Junior Executive", "Executive"],
    "Senior executive / specialist": ["Senior Executive", "Professional"],
    "Manager": ["Manager", "Middle Management"],
    "Senior leadership": ["Senior Management"],
}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _bust():
    _cache.clear()


# ---------------------------------------------------------------- JSON shapes
def card(j, last_fetch):
    srcs = (j["srcs"] or "").split(",")
    v = feed.verdict(j)
    return {
        "id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"],
        "salary_min": j["salary_min"], "salary_max": j["salary_max"], "salary_from_mcf": feed.salary_from_mcf(j),
        "salary_period": j["salary_period"] or "month", "salary_note": j["salary_note"],
        "posted": j["posted"], "employment_type": j["employment_type"], "level": j["level"],
        "sources": feed.sources(j), "status": j["status"], "applicants": j["applicants"], "agency": bool(j["agency"]),
        "is_new": bool(j["first_seen"] and last_fetch and j["first_seen"] >= last_fetch),
        "exclusive": "mcf" not in srcs, "copies": j["copies"] or 0,
        "match": j["match"] if "match" in j.keys() else None,
        "km": round(j["home_km"], 1) if j["home_km"] is not None else None, "mins": geo.minutes(j["home_km"]),
        "mode": geo.work_mode(j["location"], j["flex"]),
        "verdict": {"key": v[0], "label": v[1], "why": v[2]} if v else None,
    }


def detail(conn, j):
    d = card(j, db.meta_get(conn, "last_fetch"))
    listings = conn.execute(
        "SELECT source, url, first_seen, last_seen, closed FROM listings WHERE job_id=? ORDER BY "
        f"CASE WHEN source IN {feed.ATS_IN} THEN 0 WHEN source='mcf' THEN 1 ELSE 2 END, first_seen",
        (j["id"],)).fetchall()
    market = feed.market_for(conn, j)
    d.update({
        "category": j["category"], "skills": j["skills"], "min_experience": j["min_experience"],
        "views": j["views"], "reposts": j["reposts"], "original_posted": j["original_posted"], "expiry": j["expiry"],
        "description": j["description"], "note": j["note"], "status_updated": j["status_updated"],
        "first_seen": j["first_seen"], "market": market, "points": feed.points(j, market),
        "app": tracker.by_job(conn, j["id"]),
        "cv": cv.match_for(conn, j["id"]),
        "postal": j["postal"], "flex": j["flex"], "has_place": j["lat"] is not None,
        "listings": [{"source": feed.SOURCE_LABEL.get(l["source"], l["source"]), "raw_source": l["source"],
                      "url": l["url"], "first_seen": l["first_seen"], "closed": bool(l["closed"])} for l in listings],
    })
    return d


def _split_counts(conn, col):
    counts = {}
    for v, n in conn.execute(f"SELECT {col}, COUNT(*) FROM jobs j WHERE {db.active_clause()} AND {col} IS NOT NULL "
                             f"GROUP BY {col}"):
        for part in (p.strip() for p in str(v).split(",")):
            if part:
                counts[part] = counts.get(part, 0) + n
    return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:12]


def _profile_filters(p, noise=True):
    levels = [m for g in p.get("levels", []) for m in LEVEL_GROUPS.get(g, [])]
    f = {"roles": p.get("roles") or [], "levels": levels, "direct": p.get("direct"), "exact_facets": True}
    if noise and p.get("hide_noise", True):
        f.update(no_ghost=True, no_copies=True)
    if p.get("max_commute") and geo.home():
        f["max_km_soft"] = geo.km_for_minutes(p["max_commute"])
    return f


def meta(conn):
    def heavy():
        act = db.active_clause()
        q = lambda sql: conn.execute(sql).fetchone()[0]
        by_source = {}
        for src, n in conn.execute(f"SELECT l.source, COUNT(DISTINCT l.job_id) FROM listings l JOIN jobs j ON j.id=l.job_id "
                                   f"WHERE l.closed=0 AND {act} GROUP BY l.source"):
            label = feed.SOURCE_LABEL.get(src, src)
            by_source[label] = by_source.get(label, 0) + n
        return {"active": q(f"SELECT COUNT(*) FROM jobs j WHERE {act}"), "by_source": by_source,
                "types": _split_counts(conn, "employment_type"), "levels": _split_counts(conn, "level")}
    out = dict(_cached("meta", 300, heavy))
    out["last_fetch"] = db.meta_get(conn, "last_fetch")
    out["tracked"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IS NOT NULL AND status!='hidden'").fetchone()[0]
    out["fetch"] = fetch_status(conn)
    out["profile"] = userdata.profile()
    out["paths"] = {"yours": str(userdata.FILE), "cache": str(db.DB_PATH)}
    out["gmail"] = gmail_status()
    out["cv"] = bool(cv.get_cv())
    h = geo.home()
    out["home"] = {"postal": h["postal"], "precision": h["precision"]} if h else None
    out["located"] = _cached("located", 600, lambda: conn.execute(
        f"SELECT COUNT(*) FROM jobs j WHERE {db.active_clause()} AND j.lat IS NOT NULL").fetchone()[0])
    return out


def _par(tasks):
    """Run independent read queries in parallel, each on its own connection (SQLite releases the GIL)."""
    def run(fn):
        c = db.connect()
        try:
            return fn(c)
        finally:
            c.close()
    with ThreadPoolExecutor(min(8, len(tasks))) as ex:
        return list(ex.map(run, tasks))


def home(conn):
    p = userdata.profile()
    lf = db.meta_get(conn, "last_fetch")
    base = _profile_filters(p)
    ns = lambda **kw: Namespace(**{**base, **kw})
    link = lambda **kw: {"mine": 1, **{k: v for k, v in kw.items() if v not in (None, False)}}

    def section(key, title, sub, a, lnk):
        def run(c):
            rows, total = feed.query(c, a)
            return {"key": key, "title": title, "sub": sub, "total": total, "link": lnk,
                    "jobs": [card(j, lf) for j in rows]}
        return run

    min_pay = p.get("min_pay")
    raw = _profile_filters(p, noise=False)
    count_of = lambda **kw: (lambda c: feed.count(c, Namespace(**kw)))
    has_cv = bool(cv.get_cv())
    tasks = [
        section("cv", "Best matches for your CV", "Jobs from the last 30 days that ask for the skills on your CV, best first",
                Namespace(**{**base, "roles": []}, sort="match", min_match=cv.GOOD, days=30, limit=6),
                {"sort": "match", "min_match": cv.GOOD, "days": 30}) if has_cv else (lambda c: None),
        section("best", "Best bets for you", "Fresh, not crowded, posted by the employer, with pay shown",
                ns(sort="best", days=14, min_salary=min_pay, limit=6), link(sort="best", days=14, min_salary=min_pay)),
        section("early", "Apply early", "Posted in the last 3 days with 10 or fewer applicants",
                ns(early=True, sort="date", min_salary=min_pay, limit=6), link(early=1, min_salary=min_pay)),
        section("exclusive", "You won't find these on MyCareersFuture", "Straight from company career pages and your alert emails",
                ns(exclusive=True, sort="date", limit=6), link(exclusive=1)),
        section("pay", "Highest paying", "Your roles, sorted by the top of the advertised range",
                ns(sort="salary", min_salary=min_pay, limit=6), link(sort="salary", min_salary=min_pay)),
        section("closing", "Closing this week", "Apply before these come down",
                ns(closing=7, sort="closing", min_salary=min_pay, limit=6), link(closing=7, sort="closing", min_salary=min_pay)),
        count_of(**base), count_of(**base, new=True), count_of(**raw, only_ghost=True), count_of(**raw, only_mass=True),
    ]
    pay = None
    if p.get("roles"):
        role = p["roles"][0]
        w, params = feed.where(conn, Namespace(roles=[role], levels=raw["levels"], exact_facets=True, no_copies=True))
        tasks.append(lambda c: dict(feed.pay_stats(c, w, params), role=role))
    res = _par(tasks)
    sections, (n_match, n_new, n_ghost, n_mass) = res[:6], res[6:10]
    if p.get("roles"):
        pay = res[10]
    ex = sections[3]
    sections = [s for s in sections if s]
    if ex["total"] == 0 and p.get("roles"):  # career pages are mostly tech/finance: show the newest anyway
        rows, total = feed.query(conn, Namespace(exclusive=True, sort="date", limit=6, no_ghost=True))
        ex.update(jobs=[card(j, lf) for j in rows], total=total, link={"exclusive": 1},
                  sub="None match your roles yet. Here are the newest from all companies we follow.")
    counts = {
        "matches": n_match, "new": n_new, "ghost": n_ghost, "mass": n_mass,
        "tracker": dict(conn.execute("SELECT status, COUNT(*) FROM jobs WHERE status IS NOT NULL AND status!='hidden' "
                                     "GROUP BY status").fetchall()),
    }
    return {"profile": p, "sections": sections, "counts": counts, "pay": pay, "last_fetch": lf}


def pay_page(conn, role, levels):
    lv = [m for g in levels for m in LEVEL_GROUPS.get(g, [])]
    w, params = feed.where(conn, Namespace(roles=[role] if role else [], levels=lv, exact_facets=True, no_copies=True))
    stats = feed.pay_stats(conn, w, params, detail=True)
    stats["total"] = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", params).fetchone()[0]
    stats["role"] = role
    return stats


def suggestions(conn):
    def build():
        act = db.active_clause()
        return [{"role": r, "n": conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {act} AND j.title LIKE ?",
                                              (f"%{r}%",)).fetchone()[0]} for r in ROLE_SUGGESTIONS]
    return {"roles": _cached("suggest", 3600, build), "levels": list(LEVEL_GROUPS)}


# ---------------------------------------------------------------- background fetch
def start_fetch(full=False):
    with _lock:
        if _fetch["running"]:
            return False
        _fetch.update(running=True, started=datetime.now().isoformat(timespec="seconds"), finished=None,
                      code=None, mode="full" if full else "quick")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", COLUMNS="110")
    cmd = [sys.executable, "-m", "sgjobs", "fetch"] + (["--full"] if full else [])

    def run():
        with open(LOG, "w", encoding="utf-8") as log:
            code = subprocess.call(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with _lock:
            _fetch.update(running=False, finished=datetime.now().isoformat(timespec="seconds"), code=code)
        _bust()

    threading.Thread(target=run, daemon=True).start()
    return True


def fetch_status(conn):
    s = dict(_fetch)
    if s["started"]:
        s["listings_done"] = conn.execute("SELECT COUNT(*) FROM listings WHERE last_seen >= ?", (s["started"],)).fetchone()[0]
    if not s["running"] and s["finished"] and LOG.exists():
        s["log"] = LOG.read_text(encoding="utf-8", errors="replace")[-1500:]
    return s


def cv_overview(conn):
    p = userdata.profile()
    w, params = (None, ())
    if p.get("roles"):
        w = "(" + " OR ".join("j.title LIKE ?" for _ in p["roles"]) + ")"
        params = tuple(f"%{r}%" for r in p["roles"])
    out = cv.overview(conn, w, params)
    out["thresholds"] = {"strong": cv.STRONG, "good": cv.GOOD}
    return out


# ---------------------------------------------------------------- Gmail
def gmail_status():
    s = gmail.status()
    s.update(checking=_gm["running"], error=_gm["error"])
    return s


def start_gmail_check(days=None):
    with _lock:
        if _gm["running"]:
            return False
        _gm.update(running=True, error=None)

    def run():
        try:
            _gm["result"] = gmail.check(days=days, log=lambda m: None)
        except Exception as e:
            _gm["error"] = str(e) if isinstance(e, gmail.GmailError) else f"{e.__class__.__name__}: {e}"
        finally:
            _gm["running"] = False
            _bust()
    threading.Thread(target=run, daemon=True).start()
    return True


# ---------------------------------------------------------------- email import
def import_bytes(name, raw):
    if name.lower().endswith(".mbox"):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "upload.mbox"
            f.write_bytes(raw)
            listings, emails = email_alerts.from_path(f)
    else:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        listings = email_alerts._from_message(msg)
        emails = int(bool(listings))
    conn = db.connect()
    ts = db.now()
    res = {"new": 0, "merged": 0, "seen": 0}
    jobs = []
    for L in listings:
        r = db.upsert(conn, L, ts)
        res[r] += 1
        jid = conn.execute("SELECT job_id FROM listings WHERE source=? AND ext_id=?",
                           (L["source"], str(L["ext_id"]))).fetchone()[0]
        jobs.append({"id": jid, "title": L["title"], "company": L["company"], "source": L["source"], "result": r})
    conn.commit()
    _bust()
    return {"file": name, "emails": emails, "links": len(listings), **res, "jobs": jobs}


# ---------------------------------------------------------------- HTTP
def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _list(v):
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def search_args(q, conn):
    a = dict(query=q.get("q", ""), min_salary=_int(q.get("min_salary")), max_applicants=_int(q.get("max_applicants")),
             direct=q.get("direct") == "1", type=q.get("type") or None, level=q.get("level") or None,
             days=_int(q.get("days")), new=q.get("new") == "1", source=q.get("source") or None,
             multi=q.get("multi") == "1", no_copies=q.get("no_copies") == "1", no_ghost=q.get("no_ghost") == "1",
             anywhere=q.get("anywhere") == "1", exclusive=q.get("exclusive") == "1", early=q.get("early") == "1",
             closing=_int(q.get("closing")), roles=_list(q.get("roles")), show_hidden=False,
             min_match=_int(q.get("min_match")),
             max_km=geo.km_for_minutes(_int(q.get("max_min"))) if _int(q.get("max_min")) else None,
             sort=q.get("sort", "date"), exact_facets=True,
             limit=min(_int(q.get("limit")) or 30, 100), offset=_int(q.get("offset")) or 0)
    if q.get("mine") == "1":  # apply your profile: roles, levels, direct, hide ghost & mass ads
        for k, v in _profile_filters(userdata.profile()).items():
            if v and not a.get(k):
                a[k] = v
    return Namespace(**a)


class Handler(BaseHTTPRequestHandler):
    server_version = "ChiongWork"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            conn = db.connect()
            if u.path == "/api/search":
                a = search_args(q, conn)
                rows, total = feed.query(conn, a)
                lf = db.meta_get(conn, "last_fetch")
                out = {"total": total, "jobs": [card(j, lf) for j in rows]}
                if a.offset == 0:  # pay summary for this exact search
                    key = "pay:" + urlencode(sorted((k, v) for k, v in q.items() if k not in ("offset", "limit", "sort")))
                    w, params = feed.where(conn, a)
                    out["pay"] = _cached(key, 120, lambda: feed.pay_stats(conn, w, params))
                return self._send(200, out)
            if u.path.startswith("/api/job/"):
                j = feed.get_job(conn, _int(u.path.rsplit("/", 1)[-1]))
                return self._send(200, detail(conn, j)) if j else self._send(404, {"error": "no such job"})
            if u.path == "/api/home":
                return self._send(200, _cached("home", 120, lambda: home(conn)))
            if u.path == "/api/pay":
                role, levels = q.get("role", "").strip(), _list(q.get("levels"))
                return self._send(200, _cached(f"paypage:{role}:{levels}", 300, lambda: pay_page(conn, role, levels)))
            if u.path == "/api/suggestions":
                return self._send(200, suggestions(conn))
            if u.path == "/api/tracker":
                return self._send(200, tracker.overview(conn))
            if u.path.startswith("/api/app/") and u.path.endswith("/ics"):
                parts = u.path.split("/")
                body = tracker.ics(parts[3], q.get("i"))
                if not body:
                    return self._send(404, {"error": "no such interview"})
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="interview.ics"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path.startswith("/api/app/"):
                a = tracker.get(conn, u.path.split("/")[3])
                return self._send(200, a) if a else self._send(404, {"error": "no such application"})
            if u.path == "/api/meta":
                return self._send(200, meta(conn))
            if u.path == "/api/fetch":
                return self._send(200, fetch_status(conn))
            if u.path == "/api/gmail":
                return self._send(200, gmail_status())
            if u.path == "/api/cv":
                return self._send(200, cv_overview(conn))
            if u.path == "/api/skills":
                return self._send(200, {"skills": cv.suggest(conn, q.get("q", ""))})
            return self._send(404, {"error": "not found"})
        except Exception as e:  # surface errors to the page instead of hanging it
            return self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-SGJobs") != "1":
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 50 * 1024 * 1024:
            return self._send(413, {"error": "file too large (50 MB max)"})
        raw = self.rfile.read(n) if n else b""
        try:
            if u.path.startswith("/api/job/") and u.path.endswith("/status"):
                jid = _int(u.path.split("/")[3])
                body = json.loads(raw or b"{}")
                status = body.get("status") or None
                if status and status not in feed.STATUSES:
                    return self._send(400, {"error": "bad status"})
                conn = db.connect()
                if not feed.get_job(conn, jid):
                    return self._send(404, {"error": "no such job"})
                tracker.set_status_for_job(conn, jid, status, body.get("note") if "note" in body else None)
                _cache.pop("home", None)
                return self._send(200, detail(conn, feed.get_job(conn, jid)))
            if u.path.startswith("/api/job/") and u.path.endswith("/clicked"):
                conn = db.connect()
                jid = _int(u.path.split("/")[3])
                if not feed.get_job(conn, jid):
                    return self._send(404, {"error": "no such job"})
                app = tracker.clicked_apply(conn, jid)
                _cache.pop("home", None)
                return self._send(200, tracker.summary(conn, app))
            if u.path == "/api/app":  # add a job found elsewhere
                try:
                    app = tracker.create_manual(db.connect(), json.loads(raw or b"{}"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, tracker.full(db.connect(), app))
            if u.path.startswith("/api/app/"):
                parts = u.path.split("/")  # /api/app/<id>[/action]
                aid, action = parts[3], (parts[4] if len(parts) > 4 else "update")
                body = json.loads(raw or b"{}")
                conn = db.connect()
                try:
                    if action == "update":
                        app = tracker.update(conn, aid, body)
                    elif action == "event":
                        app = tracker.add_event(conn, aid, body.get("kind") or "note", body.get("text"))
                    elif action == "interview":
                        app = tracker.add_interview(conn, aid, body.get("at") or "", body.get("kind"), body.get("note"))
                    elif action == "interview-remove":
                        app = tracker.remove_interview(aid, body.get("id"))
                    elif action == "delete":
                        tracker.delete(conn, aid)
                        _cache.pop("home", None)
                        return self._send(200, {"deleted": aid})
                    else:
                        return self._send(404, {"error": "not found"})
                except KeyError as e:
                    return self._send(404, {"error": str(e).strip("'")})
                except ValueError as e:
                    return self._send(400, {"error": str(e) or "bad value"})
                _cache.pop("home", None)
                return self._send(200, tracker.full(conn, app))
            if u.path == "/api/home":
                conn = db.connect()
                try:
                    h = geo.set_home(conn, (json.loads(raw or b"{}").get("postal") or "").strip())
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                _bust()
                return self._send(200, {"home": {"postal": h["postal"], "precision": h["precision"]} if h else None})
            if u.path == "/api/profile":
                p = userdata.save_profile(json.loads(raw or b"{}"))
                _bust()
                return self._send(200, p)
            if u.path == "/api/fetch":
                body = json.loads(raw or b"{}")
                ok = start_fetch(full=bool(body.get("full")))
                return self._send(202 if ok else 409, {"started": ok})
            if u.path.startswith("/api/cv/"):
                conn = db.connect()
                action = u.path.rsplit("/", 1)[-1]
                try:
                    if action == "upload":
                        name = unquote(self.headers.get("X-Filename") or "cv")
                        cv.save_cv(conn, name, cv.extract_text(name, raw))
                    elif action == "text":
                        cv.save_cv(conn, "Pasted text", json.loads(raw or b"{}").get("text"))
                    elif action == "skills":
                        body = json.loads(raw or b"{}")
                        cv.edit_cv(conn, body.get("add") or [], body.get("remove") or [], body.get("years"))
                    elif action == "delete":
                        cv.delete_cv(conn)
                    else:
                        return self._send(404, {"error": "not found"})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:  # unreadable file
                    return self._send(400, {"error": f"Couldn't read that file ({e.__class__.__name__}). Try a PDF or .docx, or paste the text."})
                _bust()
                return self._send(200, cv_overview(conn))
            if u.path == "/api/gmail/connect":
                body = json.loads(raw or b"{}")
                try:
                    gmail.connect(body.get("address"), body.get("password"))
                except gmail.GmailError as e:
                    return self._send(400, {"error": str(e)})
                start_gmail_check()  # first look back 30 days
                return self._send(200, gmail_status())
            if u.path == "/api/gmail/disconnect":
                gmail.disconnect()
                return self._send(200, gmail_status())
            if u.path == "/api/gmail/check":
                start_gmail_check()
                return self._send(202, gmail_status())
            if u.path == "/api/import":
                name = self.headers.get("X-Filename") or "upload.eml"
                return self._send(200, import_bytes(name, raw))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{e.__class__.__name__}: {e}"})


def run(port=8700, open_browser=True):
    conn = db.connect()  # create / migrate the DB before serving
    restored = tracker.restore(conn)
    conn.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"ChiongWork is running at {url}  (Ctrl+C to stop)", flush=True)
    if restored:
        print(f"Tracker: {restored} applications ({userdata.FILE})", flush=True)

    def warm():  # pre-compute the slow-ish first-load data so the page opens fast
        c = db.connect()
        suggestions(c)
        if geo.home():
            geo.update_distances(c)
        if cv.get_cv():
            cv.compute(c, force=False)  # new jobs since the last scoring
        meta(c)
        if userdata.profile().get("setup_done"):
            _cached("home", 120, lambda: home(c))
        c.close()
    threading.Thread(target=warm, daemon=True).start()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
