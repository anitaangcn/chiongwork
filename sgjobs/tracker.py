"""Your job applications: the tracker.

Each application keeps its own copy of the job ad (MyCareersFuture and others delete ads when they
close, often right before your interview), a timeline of what happened, interviews, contacts,
pay asked/offered and follow-up dates.

Stored in your data file (data/my-chiongwork.json). The job cache only mirrors the status, so the
"Saved"/"Applied" badges show up in search results; restore() rebuilds that mirror.
"""
import functools
import secrets
import threading
from datetime import date, datetime, timedelta

from . import db, feed, userdata

_LOCK = threading.RLock()  # the web server is threaded: serialise read-modify-write of the data file


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with _LOCK:
            return fn(*a, **k)
    return wrapper

STATUSES = ["saved", "applied", "interview", "offer", "rejected", "no_reply", "withdrawn"]
CLOSED = {"rejected", "no_reply", "withdrawn"}
LABEL = {"saved": "Saved", "applied": "Applied", "interview": "Interviewing", "offer": "Offer",
         "rejected": "Rejected", "no_reply": "No reply", "withdrawn": "Withdrawn"}
FOLLOW_UP_DAYS = 7
NO_REPLY_DAYS = 30
SNAP_FIELDS = ["title", "company", "location", "salary_min", "salary_max", "salary_period", "employment_type", "level",
               "category", "skills", "min_experience", "posted", "expiry", "description"]
EDITABLE = {"contact_name", "contact_email", "contact_phone", "cv_version", "salary_asked", "salary_offered",
            "note", "next_followup", "applied_at", "title", "company", "url", "location"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _today():
    return date.today().isoformat()


def _apps():
    return userdata.load_key("applications") or []


def _save(apps):
    userdata.save_key("applications", apps)


def _event(app, kind, text, **extra):
    app.setdefault("events", []).append({"at": _now(), "type": kind, "text": text, **extra})


# ---------------------------------------------------------------- linking apps <-> cached jobs
def _listings(conn, job_id):
    return [{"source": l["source"], "ext_id": l["ext_id"], "url": l["url"]}
            for l in conn.execute("SELECT source, ext_id, url FROM listings WHERE job_id=?", (job_id,))]


def _best_url(conn, job_id):
    r = conn.execute("SELECT url FROM listings WHERE job_id=? AND closed=0 ORDER BY "
                     f"CASE WHEN source IN {feed.ATS_IN} THEN 0 WHEN source='mcf' THEN 1 ELSE 2 END", (job_id,)).fetchone()
    return r["url"] if r else None


def _job_id_for(conn, app):
    """The cached job this application belongs to (ids can change if the cache is rebuilt)."""
    if app.get("job_id"):
        r = conn.execute("SELECT 1 FROM jobs WHERE id=?", (app["job_id"],)).fetchone()
        if r and any(conn.execute("SELECT 1 FROM listings WHERE job_id=? AND source=? AND ext_id=?",
                                  (app["job_id"], l["source"], l["ext_id"])).fetchone() for l in app.get("listings", [])):
            return app["job_id"]
    for l in app.get("listings", []):
        r = conn.execute("SELECT job_id FROM listings WHERE source=? AND ext_id=?", (l["source"], str(l["ext_id"]))).fetchone()
        if r:
            return r["job_id"]
    return None


def snapshot(conn, job_id):
    j = feed.get_job(conn, job_id)
    if not j:
        return None
    snap = {f: j[f] for f in SNAP_FIELDS}
    snap["sources"] = feed.sources(j)
    snap["listings"] = _listings(conn, job_id)
    snap["url"] = _best_url(conn, job_id)
    return snap


def find(apps, aid=None, job_id=None):
    for a in apps:
        if (aid and a["id"] == aid) or (job_id and a.get("job_id") == job_id):
            return a
    return None


def _mirror(conn, app):
    jid = app.get("job_id")
    if jid:
        conn.execute("UPDATE jobs SET status=?, note=?, status_updated=? WHERE id=?",
                     (app["status"], app.get("note"), app.get("updated"), jid))


# ---------------------------------------------------------------- changes
def _apply_status(app, status, at=None):
    old = app.get("status")
    if status == old:
        return
    app["status"] = status
    app["updated"] = _now()
    if status == "applied" and not app.get("applied_at"):
        app["applied_at"] = (at or _today())[:10]
        app["next_followup"] = (date.fromisoformat(app["applied_at"]) + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    if status in ("interview", "offer", "rejected") and not app.get("applied_at"):
        app["applied_at"] = (at or _today())[:10]  # you can't get a reply without applying
    if status in ("interview", "offer") or status in CLOSED:
        app["next_followup"] = None
    if status in ("interview", "offer", "rejected") and app.get("applied_at") and not app.get("replied_at"):
        app["replied_at"] = _today()
    _event(app, "status", f"Moved to {LABEL[status]}" if old else f"Added as {LABEL[status]}", status=status)


@_locked
def set_status_for_job(conn, job_id, status, note=None):
    """From the job drawer / search cards. status=None removes the application."""
    apps = _apps()
    app = find(apps, job_id=job_id)
    if status is None:
        if app:
            apps.remove(app)
            _save(apps)
        conn.execute("UPDATE jobs SET status=NULL, status_updated=? WHERE id=?", (db.now(), job_id))
        conn.commit()
        return None
    if status == "hidden":  # "not interested" isn't an application
        if app:
            apps.remove(app)
            _save(apps)
        db.set_status(conn, job_id, "hidden")
        conn.commit()
        return None
    if status not in STATUSES:
        raise ValueError("bad status")
    if not app:
        snap = snapshot(conn, job_id)
        if not snap:
            raise KeyError("no such job")
        app = {"id": "a" + secrets.token_hex(5), "job_id": job_id, "created": _now(), "status": None,
               "snap": snap, "listings": snap.pop("listings"), "url": snap.pop("url"), "events": [], "interviews": []}
        apps.insert(0, app)
    else:
        fresh = snapshot(conn, job_id)  # keep the richest copy of the ad
        if fresh and len(fresh.get("description") or "") >= len(app["snap"].get("description") or ""):
            app["listings"] = fresh.pop("listings")
            app["url"] = fresh.pop("url") or app.get("url")
            app["snap"].update({k: v for k, v in fresh.items() if v not in (None, "")})
    _apply_status(app, status)
    if note is not None:
        app["note"] = note or None
    _mirror(conn, app)
    conn.commit()
    _save(apps)
    return app


@_locked
def clicked_apply(conn, job_id):
    """You opened the application page: keep the job (as Saved) and remember to ask if you applied."""
    app = find(_apps(), job_id=job_id) or set_status_for_job(conn, job_id, "saved")
    apps = _apps()
    app = find(apps, aid=app["id"])
    app["apply_clicked_at"] = _now()
    _save(apps)
    return app


@_locked
def update(conn, aid, fields):
    apps = _apps()
    app = find(apps, aid=aid)
    if not app:
        raise KeyError("no such application")
    if "status" in fields and fields["status"]:
        if fields["status"] not in STATUSES:
            raise ValueError("bad status")
        _apply_status(app, fields["status"], fields.get("applied_at"))
        if fields["status"] != "saved":
            app.pop("apply_clicked_at", None)
    for k, v in fields.items():
        if k in EDITABLE:
            v = v.strip() if isinstance(v, str) else v
            if k in ("title", "company", "location"):
                if v:
                    app["snap"][k] = v
                continue
            app[k] = v or None
    if fields.get("dismiss_apply_prompt"):
        app.pop("apply_clicked_at", None)
    app["updated"] = _now()
    app["job_id"] = _job_id_for(conn, app)
    _mirror(conn, app)
    conn.commit()
    _save(apps)
    return app


@_locked
def add_event(conn, aid, kind, text):
    apps = _apps()
    app = find(apps, aid=aid)
    if not app:
        raise KeyError("no such application")
    if kind == "followup":
        _event(app, "followup", text or "Followed up")
        app["next_followup"] = (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    elif kind == "snooze":
        app["next_followup"] = (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    else:
        _event(app, "note", text)
    app["updated"] = _now()
    _save(apps)
    return app


@_locked
def add_interview(conn, aid, at, kind, note):
    apps = _apps()
    app = find(apps, aid=aid)
    if not app:
        raise KeyError("no such application")
    datetime.fromisoformat(at)  # validate
    app.setdefault("interviews", []).append({"id": secrets.token_hex(4), "at": at[:16], "kind": kind or "Interview",
                                             "note": note or None})
    app["interviews"].sort(key=lambda x: x["at"])
    _event(app, "interview", f"{kind or 'Interview'} scheduled for {at[:16].replace('T', ' ')}")
    if app["status"] in ("saved", "applied"):
        _apply_status(app, "interview")
    app["updated"] = _now()
    app["job_id"] = _job_id_for(conn, app)
    _mirror(conn, app)
    conn.commit()
    _save(apps)
    return app


@_locked
def remove_interview(aid, iid):
    apps = _apps()
    app = find(apps, aid=aid)
    if not app:
        raise KeyError("no such application")
    app["interviews"] = [i for i in app.get("interviews", []) if i["id"] != iid]
    _save(apps)
    return app


@_locked
def create_manual(conn, data):
    title, company = (data.get("title") or "").strip(), (data.get("company") or "").strip()
    if not title or not company:
        raise ValueError("A job title and company are needed")
    status = data.get("status") if data.get("status") in STATUSES else "saved"
    sal = lambda k: int(data[k]) if str(data.get(k) or "").isdigit() else None
    app = {"id": "a" + secrets.token_hex(5), "job_id": None, "created": _now(), "status": None, "manual": True,
           "snap": {"title": title, "company": company, "location": (data.get("location") or "").strip() or None,
                    "salary_min": sal("salary_min"), "salary_max": sal("salary_max"), "salary_period": "month",
                    "description": (data.get("description") or "").strip() or None,
                    "sources": [data.get("source") or "Added by you"]},
           "listings": [], "url": (data.get("url") or "").strip() or None, "events": [], "interviews": []}
    _apply_status(app, status, data.get("applied_at"))
    apps = _apps()
    apps.insert(0, app)
    _save(apps)
    return app


@_locked
def delete(conn, aid):
    apps = _apps()
    app = find(apps, aid=aid)
    if app:
        apps.remove(app)
        _save(apps)
        if app.get("job_id"):
            conn.execute("UPDATE jobs SET status=NULL WHERE id=?", (app["job_id"],))
            conn.commit()


# ---------------------------------------------------------------- reading
def _auto_close(apps):
    """Applied a month ago with no news: almost always means no reply. Move it, and say so."""
    changed = False
    cutoff = (date.today() - timedelta(days=NO_REPLY_DAYS)).isoformat()
    for a in apps:
        if a["status"] == "applied" and a.get("applied_at") and a["applied_at"] <= cutoff \
                and (a.get("updated") or "")[:10] <= cutoff:
            _apply_status(a, "no_reply")
            a["events"][-1]["text"] = f"Moved to No reply automatically ({NO_REPLY_DAYS} days without news)"
            changed = True
    return changed


def _ad_open(conn, app):
    jid = app.get("job_id")
    if not jid:
        return None
    r = conn.execute(f"SELECT 1 FROM jobs j WHERE j.id=? AND {db.active_clause()}", (jid,)).fetchone()
    return bool(r)


def summary(conn, app):
    s = app["snap"]
    today = _today()
    upcoming = [i for i in app.get("interviews", []) if i["at"][:10] >= today]
    out = {
        "id": app["id"], "job_id": app.get("job_id"), "status": app["status"], "label": LABEL.get(app["status"], ""),
        "title": s.get("title"), "company": s.get("company"), "location": s.get("location"),
        "salary_min": s.get("salary_min"), "salary_max": s.get("salary_max"), "salary_period": s.get("salary_period"),
        "sources": s.get("sources") or [], "url": app.get("url"), "created": app.get("created"), "updated": app.get("updated"),
        "applied_at": app.get("applied_at"), "note": app.get("note"), "next_followup": app.get("next_followup"),
        "next_interview": upcoming[0] if upcoming else None, "manual": bool(app.get("manual")),
        "apply_clicked_at": app.get("apply_clicked_at") if app["status"] == "saved" else None,
        "followup_due": bool(app["status"] == "applied" and app.get("next_followup") and app["next_followup"] <= today),
        "ad_open": _ad_open(conn, app),
    }
    return out


def full(conn, app):
    d = summary(conn, app)
    d.update({k: app.get(k) for k in ("contact_name", "contact_email", "contact_phone", "cv_version", "salary_asked",
                                      "salary_offered", "replied_at")})
    d["snap"] = app["snap"]
    d["interviews"] = app.get("interviews", [])
    d["events"] = list(reversed(app.get("events", [])))
    d["listings"] = app.get("listings", [])
    return d


def stats(apps):
    applied = [a for a in apps if a.get("applied_at")]
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for k in range(7, -1, -1):
        start = monday - timedelta(weeks=k)
        end = start + timedelta(days=7)
        weeks.append({"start": start.isoformat(),
                      "n": sum(1 for a in applied if start.isoformat() <= a["applied_at"] < end.isoformat())})
    # only count applications old enough to have had a chance of a reply
    settled = [a for a in applied if a["applied_at"] <= (today - timedelta(days=7)).isoformat() or a.get("replied_at")]
    replied = [a for a in settled if a.get("replied_at")]
    interviewed = [a for a in applied if any(e.get("status") in ("interview", "offer") for e in a.get("events", []))
                   or a.get("interviews")]
    waits = [(date.fromisoformat(a["replied_at"]) - date.fromisoformat(a["applied_at"])).days for a in replied]
    by_source = {}
    for a in applied:
        src = (a["snap"].get("sources") or ["Other"])[0]
        b = by_source.setdefault(src, {"applied": 0, "replied": 0})
        b["applied"] += 1
        b["replied"] += bool(a.get("replied_at"))
    return {
        "applied": len(applied), "this_week": weeks[-1]["n"], "weeks": weeks,
        "settled": len(settled), "replied": len(replied),
        "reply_rate": round(100 * len(replied) / len(settled)) if settled else None,
        "interview_rate": round(100 * len(interviewed) / len(applied)) if applied else None,
        "interviews": len(interviewed),
        "avg_reply_days": round(sum(waits) / len(waits)) if waits else None,
        "offers": sum(1 for a in apps if a["status"] == "offer"),
        "by_source": [{"source": k, **v} for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]["applied"])],
    }


@_locked
def overview(conn):
    apps = _apps()
    if _auto_close(apps):
        _save(apps)
        for a in apps:
            _mirror(conn, a)
        conn.commit()
    items = [summary(conn, a) for a in apps]
    today = _today()
    soon = (date.today() + timedelta(days=7)).isoformat()
    attention = []
    for a in items:
        if a["apply_clicked_at"]:
            attention.append({"kind": "did_apply", "app": a})
        if a["followup_due"]:
            attention.append({"kind": "followup", "app": a})
        if a["next_interview"] and a["next_interview"]["at"][:10] <= soon:
            attention.append({"kind": "interview", "app": a})
    attention.sort(key=lambda x: {"interview": 0, "did_apply": 1, "followup": 2}[x["kind"]])
    return {"apps": items, "attention": attention, "stats": stats(apps), "today": today}


def get(conn, aid):
    app = find(_apps(), aid=aid)
    if not app:
        return None
    return full(conn, app)


def by_job(conn, job_id):
    app = find(_apps(), job_id=job_id)
    return summary(conn, app) if app else None


def ics(aid, iid):
    app = find(_apps(), aid=aid)
    it = next((i for i in (app or {}).get("interviews", []) if i["id"] == iid), None)
    if not it:
        return None
    start = datetime.fromisoformat(it["at"])
    end = start + timedelta(hours=1)
    f = lambda d: d.strftime("%Y%m%dT%H%M%S")
    esc = lambda t: (t or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    s = app["snap"]
    desc = "\n".join(x for x in [it.get("note"), app.get("contact_name") and f"Contact: {app['contact_name']}",
                                 app.get("url")] if x)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ChiongWork//EN", "BEGIN:VEVENT",
        f"UID:{aid}-{iid}@chiongwork.local", f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID=Asia/Singapore:{f(start)}", f"DTEND;TZID=Asia/Singapore:{f(end)}",
        f"SUMMARY:{esc(it['kind'])}: {esc(s.get('title'))} at {esc(s.get('company'))}",
        f"DESCRIPTION:{esc(desc)}", "BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY", "DESCRIPTION:Interview in 1 hour",
        "END:VALARM", "END:VEVENT", "END:VCALENDAR", ""])


# ---------------------------------------------------------------- startup sync
@_locked
def restore(conn):
    """Bring the job-cache mirror in line with your applications (after a cache reset or on startup).
    Also migrates the old tracker format. Returns how many applications are tracked."""
    apps = _apps()
    old = userdata.load_key("tracker")
    if old and not apps:  # v1 tracker: [{title, company, status, note, listings}]
        for it in old:
            st = it.get("status") if it.get("status") in STATUSES else "saved"
            app = {"id": "a" + secrets.token_hex(5), "job_id": None, "created": it.get("status_updated") or _now(),
                   "status": st, "updated": it.get("status_updated") or _now(), "note": it.get("note"),
                   "snap": {"title": it["title"], "company": it["company"]}, "listings": it.get("listings", []),
                   "url": (it.get("listings") or [{}])[0].get("url"), "events": [], "interviews": []}
            if st != "saved":
                app["applied_at"] = (it.get("status_updated") or _now())[:10]
            apps.append(app)
    ts = db.now()
    for app in apps:
        jid = _job_id_for(conn, app)
        if not jid and app.get("listings"):
            l = app["listings"][0]
            db.upsert(conn, {"source": l["source"], "ext_id": l["ext_id"], "url": l.get("url"),
                             "title": app["snap"].get("title"), "company": app["snap"].get("company")}, ts)
            jid = _job_id_for(conn, app)
        app["job_id"] = jid
        if jid:
            fresh = snapshot(conn, jid)
            if fresh and len(fresh.get("description") or "") > len(app["snap"].get("description") or ""):
                fresh.pop("listings"), fresh.pop("url")
                app["snap"].update({k: v for k, v in fresh.items() if v not in (None, "")})
    tracked_ids = {a["job_id"] for a in apps if a.get("job_id")}
    for r in conn.execute("SELECT id FROM jobs WHERE status IS NOT NULL AND status != 'hidden'").fetchall():
        if r["id"] not in tracked_ids:
            conn.execute("UPDATE jobs SET status=NULL WHERE id=?", (r["id"],))
    for app in apps:
        _mirror(conn, app)
    conn.commit()
    _save(apps)
    if old is not None:
        userdata.save_key("tracker", None)
    return len(apps)
