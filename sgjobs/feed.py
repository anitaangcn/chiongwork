"""Search, job verdicts and pay statistics, shared by the terminal CLI and the local web app."""
from datetime import date, timedelta

from . import db
from .util import SENIORITY, is_hype

SOURCE_LABEL = {"mcf": "MCF", "greenhouse": "Careers", "lever": "Careers", "ashby": "Careers",
                "smartrecruiters": "Careers", "linkedin": "LinkedIn", "jobstreet": "JobStreet",
                "indeed": "Indeed", "glassdoor": "Glassdoor"}
STATUSES = ["saved", "applied", "interview", "offer", "rejected", "no_reply", "withdrawn", "hidden"]
ATS_IN = "('greenhouse','lever','ashby','smartrecruiters')"

# Long-open and re-posted, or re-posted many times: often never filled ("ghost jobs").
GHOST_SQL = ("((julianday('now') - julianday(j.original_posted) >= 60 AND COALESCE(j.reposts,0) >= 1) "
             "OR COALESCE(j.reposts,0) >= 3)")
# The same ad posted as 3+ separate listings: mass hiring / commission-only roles.
MASS_SQL = "((SELECT COUNT(*) FROM listings lm WHERE lm.job_id=j.id AND lm.source='mcf' AND lm.closed=0) >= 3)"
HYPE_SQL = "(hype(j.title, j.company) = 1)"  # emoji / "no experience, travel perks" sales ads (see util.is_hype)
MONTHLY_SQL = "(j.salary_max IS NOT NULL AND COALESCE(j.salary_period,'month')='month')"
# "Best bets": fresh, not crowded, direct, transparent pay, minus ghost/mass-ad penalties.
SCORE_SQL = f"""(
  CASE WHEN j.posted >= date('now','-3 day') THEN 3 WHEN j.posted >= date('now','-7 day') THEN 2
       WHEN j.posted >= date('now','-14 day') THEN 1 ELSE 0 END
  + CASE WHEN j.applicants IS NULL THEN 1 WHEN j.applicants <= 10 THEN 3 WHEN j.applicants <= 30 THEN 2
         WHEN j.applicants <= 80 THEN 1 ELSE -1 END
  + CASE WHEN COALESCE(j.agency,0)=0 THEN 1 ELSE 0 END
  + CASE WHEN COALESCE(j.responsive,0)=1 THEN 1 ELSE 0 END
  + CASE WHEN j.salary_max IS NOT NULL THEN 1 ELSE 0 END
  + CASE WHEN EXISTS (SELECT 1 FROM listings la WHERE la.job_id=j.id AND la.closed=0 AND la.source IN {ATS_IN}) THEN 1 ELSE 0 END
  - CASE WHEN {GHOST_SQL} THEN 4 ELSE 0 END
  - CASE WHEN {MASS_SQL} THEN 4 ELSE 0 END
  - CASE WHEN {HYPE_SQL} THEN 4 ELSE 0 END)"""
SORTS = {"date": "j.posted DESC, j.id DESC",
         "best": f"{SCORE_SQL} DESC, j.salary_max DESC NULLS LAST, j.posted DESC",
         "salary": f"{MONTHLY_SQL} DESC, j.salary_max DESC NULLS LAST, j.posted DESC",
         "applicants": "COALESCE(j.applicants, 999999) ASC, j.posted DESC",
         "closing": "j.expiry ASC, j.posted DESC",
         "match": "(SELECT score FROM cv_match cm2 WHERE cm2.job_id=j.id) DESC NULLS LAST, j.posted DESC",
         "near": "j.home_km IS NULL, j.home_km ASC, j.posted DESC"}

_EXTRA_COLS = ("(SELECT GROUP_CONCAT(DISTINCT source) FROM listings l WHERE l.job_id=j.id) AS srcs, "
               "(SELECT score FROM cv_match cm WHERE cm.job_id=j.id) AS match, "
               "(SELECT COUNT(*) FROM listings l5 WHERE l5.job_id=j.id AND l5.source='mcf' AND l5.closed=0) AS copies")


def where(conn, a):
    """Build the WHERE clause for a search. `a` is any object with search attributes (argparse Namespace works)."""
    g = lambda k, d=None: getattr(a, k, d)
    w, params = [db.active_clause()], []
    hay = ("(j.title || ' ' || j.company || ' ' || COALESCE(j.skills,'') || ' ' || COALESCE(j.category,''))"
           if g("anywhere") else "(j.title || ' ' || j.company)")
    for term in (g("query") or "").split():
        w.append(f"{hay} LIKE ?")
        params.append(f"%{term}%")
    roles = [r for r in (g("roles") or []) if r.strip()]
    if roles:  # any of your target roles, matched as a phrase in the title
        w.append("(" + " OR ".join("j.title LIKE ?" for _ in roles) + ")")
        params += [f"%{r.strip()}%" for r in roles]
    if g("min_salary"):
        w.append(f"j.salary_max >= ? AND {MONTHLY_SQL}")
        params.append(g("min_salary"))
    if g("max_applicants") is not None:
        w.append("COALESCE(j.applicants, 0) <= ?")
        params.append(g("max_applicants"))
    if g("direct"):
        w.append("COALESCE(j.agency, 0) = 0")
    for attr, col in (("type", "employment_type"), ("level", "level")):
        if g(attr):
            if g("exact_facets"):  # web filters: match one whole value of the comma list ("Executive" ≠ "Senior Executive")
                w.append(f"(', ' || j.{col} || ',') LIKE ?")
                params.append(f"%, {g(attr)},%")
            else:  # CLI: loose substring, so `--level manager` just works
                w.append(f"j.{col} LIKE ?")
                params.append(f"%{g(attr)}%")
    levels = g("levels") or []
    if levels:
        w.append("(j.level IS NULL OR " + " OR ".join("(', ' || j.level || ',') LIKE ?" for _ in levels) + ")")
        params += [f"%, {l},%" for l in levels]
    if g("days"):
        w.append("j.posted >= ?")
        params.append((date.today() - timedelta(days=int(g("days")))).isoformat())
    if g("new"):
        w.append("j.first_seen >= ?")
        params.append(db.meta_get(conn, "last_fetch", "9999"))
    if g("source"):
        srcs = {"careers": tuple(db.ATS_SOURCES)}.get(g("source"), (g("source"),))
        w.append(f"EXISTS (SELECT 1 FROM listings l3 WHERE l3.job_id=j.id AND l3.closed=0 "
                 f"AND l3.source IN ({','.join('?' * len(srcs))}))")
        params += list(srcs)
    if g("exclusive"):  # jobs you won't find on MyCareersFuture
        w.append("NOT EXISTS (SELECT 1 FROM listings lx WHERE lx.job_id=j.id AND lx.source='mcf')")
    if g("multi"):
        w.append("(SELECT COUNT(DISTINCT source) FROM listings l4 WHERE l4.job_id=j.id) > 1")
    if g("no_copies"):  # mass-hiring copies and hype-style sales ads
        w.append(f"NOT {MASS_SQL} AND NOT {HYPE_SQL}")
    if g("no_ghost"):
        w.append(f"NOT {GHOST_SQL}")
    if g("only_ghost"):
        w.append(GHOST_SQL)
    if g("only_mass"):
        w.append(f"({MASS_SQL} OR {HYPE_SQL})")
    if g("closing"):
        w.append("j.expiry BETWEEN date('now') AND date('now', ?)")
        params.append(f"+{int(g('closing'))} day")
    if g("min_match"):  # CV match score (see cv.py)
        w.append("EXISTS (SELECT 1 FROM cv_match mm WHERE mm.job_id=j.id AND mm.score >= ?)")
        params.append(int(g("min_match")))
    if g("max_km") is not None:  # search filter: only jobs with a known workplace this close to home
        w.append("j.home_km <= ?")
        params.append(g("max_km"))
    if g("max_km_soft") is not None:  # your profile: same, but keep jobs whose address isn't known
        w.append("(j.home_km IS NULL OR j.home_km <= ?)")
        params.append(g("max_km_soft"))
    if g("early"):  # posted in the last 3 days, 10 or fewer applicants
        w.append("j.posted >= date('now','-3 day') AND COALESCE(j.applicants, 0) <= 10")
    if not g("show_hidden"):
        w.append("COALESCE(j.status,'') != 'hidden'")
    return " AND ".join(w), params


def query(conn, a):
    w, params = where(conn, a)
    order = SORTS.get(getattr(a, "sort", None) or "date", SORTS["date"])
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", params).fetchone()[0]
    rows = conn.execute(f"SELECT j.*, {_EXTRA_COLS} FROM jobs j WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*params, getattr(a, "limit", None) or 30, getattr(a, "offset", None) or 0)).fetchall()
    return rows, total


def count(conn, a):
    w, params = where(conn, a)
    return conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", params).fetchone()[0]


def get_job(conn, job_id):
    return conn.execute(f"SELECT j.*, {_EXTRA_COLS} FROM jobs j WHERE id=?", (job_id,)).fetchone()


def tracked(conn):
    rows = conn.execute(f"SELECT j.*, {_EXTRA_COLS} FROM jobs j WHERE status IS NOT NULL AND status != 'hidden' "
                        "ORDER BY status_updated DESC").fetchall()
    order = {s: i for i, s in enumerate(STATUSES)}
    return sorted(rows, key=lambda r: order.get(r["status"], 9))


def sources(j):
    return sorted({SOURCE_LABEL.get(s, s) for s in (j["srcs"] or "").split(",") if s})


def salary_from_mcf(j):
    """True when a non-MCF job (LinkedIn, career page...) shows a salary borrowed from its MCF twin."""
    srcs = (j["srcs"] or "").split(",")
    return j["salary_source"] == "mcf" and any(s and s != "mcf" for s in srcs)


def _days_since(iso):
    try:
        return (date.today() - date.fromisoformat(iso[:10])).days
    except (TypeError, ValueError):
        return None


def signals(j, last_fetch, include_status=True):
    """[(text, tone)] with tone in new / status / warn / bad / muted / good. Used by the CLI table."""
    out = []
    if j["first_seen"] and last_fetch and j["first_seen"] >= last_fetch:
        out.append(("NEW", "new"))
    if include_status and j["status"]:
        out.append((j["status"].upper(), "status"))
    if j["reposts"]:
        out.append((f"reposted×{j['reposts']}", "warn"))
    if "copies" in j.keys() and (j["copies"] or 0) >= 3:
        out.append((f"posted {j['copies']}× as copies", "warn"))
    age = _days_since(j["original_posted"])
    if age is not None and age >= 45:
        out.append((f"open {age}d", "warn"))
    if j["applicants"] is not None:
        out.append((f"{j['applicants']} applied", "bad" if j["applicants"] >= 100 else "muted"))
    if j["agency"]:
        out.append(("agency", "muted"))
    if j["responsive"]:
        out.append(("replies fast", "good"))
    return out


def verdict(j):
    """One plain-English call per job: (key, label, why) or None."""
    age, fresh = _days_since(j["original_posted"]), _days_since(j["posted"])
    reposts, apps = j["reposts"] or 0, j["applicants"]
    copies = (j["copies"] or 0) if "copies" in j.keys() else 0
    careers = any(s in db.ATS_SOURCES for s in (j["srcs"] or "").split(","))
    if (age is not None and age >= 60 and reposts >= 1) or reposts >= 3:
        return ("ghost", "Possible ghost job", f"Open {age or '?'} days and re-posted {reposts}×. It may never be filled.")
    if is_hype(j["title"], j["company"], db.HYPE_COMPANIES):
        return ("hype", "Hype-style sales ad", "Emoji or 'no experience, travel perks' wording. That's typical of commission-only event sales.")
    if copies >= 3:
        return ("mass", "Mass-hiring ad", f"The same ad is posted {copies} times. That's typical of commission-only or bulk-hiring roles.")
    if apps is not None and apps >= 100:
        return ("crowded", "Crowded", f"{apps} people have already applied.")
    if fresh is not None and fresh <= 3 and ((apps is not None and apps <= 10) or (apps is None and careers)):
        return ("early", "Apply early", "Posted in the last 3 days, and hardly anyone has applied yet.")
    if fresh is not None and fresh <= 14 and (apps is None or apps <= 30) and not j["agency"]:
        return ("good", "Good bet", "Recent, not crowded, and posted by the employer itself.")
    return None


def points(j, market=None):
    """Pros and cons for the detail panel: [{good: bool, text}]."""
    out = []
    fresh, age = _days_since(j["posted"]), _days_since(j["original_posted"])
    srcs = (j["srcs"] or "").split(",")
    if fresh is not None:
        out.append({"good": fresh <= 7, "text": "Posted today" if fresh <= 0 else f"Posted {fresh} day{'s' if fresh != 1 else ''} ago"})
    if j["applicants"] is not None:
        a = j["applicants"]
        out.append({"good": a <= 30, "text": f"{a} applicant{'s' if a != 1 else ''} so far" + (". Still early" if a <= 10 else ". Crowded" if a >= 100 else "")})
    if any(s in db.ATS_SOURCES for s in srcs):
        out.append({"good": True, "text": "On the company's own career page, so the role is live there"})
    if "mcf" not in srcs:
        out.append({"good": True, "text": "Not on MyCareersFuture, so fewer people will see it"})
    if j["agency"]:
        out.append({"good": False, "text": "Posted by a recruitment agency, not the employer"})
    elif j["applicants"] is not None:
        out.append({"good": True, "text": "Posted directly by the employer"})
    if j["responsive"]:
        out.append({"good": True, "text": "MyCareersFuture says this employer replies to applicants quickly"})
    if j["reposts"]:
        out.append({"good": False, "text": f"Re-posted {j['reposts']}× by the employer"})
    if age is not None and age >= 45:
        out.append({"good": False, "text": f"First posted {age} days ago, so it may be hard to fill or not really open"})
    copies = (j["copies"] or 0) if "copies" in j.keys() else 0
    if copies >= 3:
        out.append({"good": False, "text": f"The same ad is posted {copies} times"})
    if j["salary_max"] is None:
        out.append({"good": False, "text": "No pay shown on any site"})
    elif market and market.get("position"):
        pos = market["position"]
        out.append({"good": pos != "below", "text": {"above": "Pays above the typical range for similar jobs",
                                                    "typical": "Pays within the typical range for similar jobs",
                                                    "below": "Pays below the typical range for similar jobs"}[pos]})
    return out


# ---------------------------------------------------------------- pay statistics
def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def pay_stats(conn, w, params, detail=False):
    """Monthly pay distribution (midpoint of each advertised range) for jobs matching WHERE w."""
    rows = conn.execute(f"SELECT j.salary_min, j.salary_max, j.level, j.company, j.applicants, j.agency "
                        f"FROM jobs j WHERE {w} AND {MONTHLY_SQL}", params).fetchall()
    mids = sorted(((r["salary_min"] or r["salary_max"]) + r["salary_max"]) / 2 for r in rows)
    n = len(mids)
    if n < 5:
        return {"n": n}
    out = {"n": n, "p10": _pct(mids, .1), "p25": _pct(mids, .25), "p50": _pct(mids, .5),
           "p75": _pct(mids, .75), "p90": _pct(mids, .9)}
    lo, hi = _pct(mids, .02), _pct(mids, .98)
    step = next((s for s in (250, 500, 1000, 2000, 5000, 10000) if (hi - lo) / s <= 18), 20000)
    start = int(lo // step * step)
    bins = []
    edge = start
    while edge <= hi:
        bins.append({"lo": edge, "hi": edge + step, "n": 0})
        edge += step
    for m in mids:
        i = min(max(int((m - start) // step), 0), len(bins) - 1)
        bins[i]["n"] += 1
    out["bins"] = bins
    if detail:
        by_level = {}
        for r in rows:
            lvl = (r["level"] or "").split(",")[0].strip()
            if lvl:
                by_level.setdefault(lvl, []).append(((r["salary_min"] or r["salary_max"]) + r["salary_max"]) / 2)
        out["by_level"] = sorted(({"level": k, "n": len(v), "p50": _pct(sorted(v), .5),
                                   "p25": _pct(sorted(v), .25), "p75": _pct(sorted(v), .75)}
                                  for k, v in by_level.items() if len(v) >= 5), key=lambda x: x["p50"])
        by_co = {}
        for r in rows:
            if not r["agency"]:  # direct employers only: agencies post on behalf of many companies
                by_co.setdefault(r["company"], []).append(((r["salary_min"] or r["salary_max"]) + r["salary_max"]) / 2)
        out["most_hiring"] = sorted(({"company": k, "n": len(v), "p50": _pct(sorted(v), .5)} for k, v in by_co.items()),
                                    key=lambda x: (-x["n"], -x["p50"]))[:8]
        apps = [r["applicants"] for r in rows if r["applicants"] is not None]
        out["median_applicants"] = _pct(sorted(apps), .5) if apps else None
        out["agency_share"] = round(100 * sum(1 for r in rows if r["agency"]) / n)
    return out


def role_key(title_norm):
    """'senior software engineer billing' -> 'software engineer' (drop seniority words, keep the first two)."""
    words = [w for w in (title_norm or "").split() if w not in SENIORITY and len(w) > 1 and not w.isdigit()]
    return " ".join(words[:2]) if len(words) >= 2 else (words[0] if words else "")


def market_for(conn, j):
    """How this job's pay compares with similar jobs (same role words in the title)."""
    key = role_key(j["title_norm"])
    if not key or j["salary_max"] is None or (j["salary_period"] or "month") != "month":
        return None
    stats = pay_stats(conn, f"{db.active_clause()} AND j.title_norm LIKE ?", [f"%{key}%"])
    if stats.get("n", 0) < 12:
        return None
    mid = ((j["salary_min"] or j["salary_max"]) + j["salary_max"]) / 2
    stats["key"] = key
    stats["mid"] = round(mid)
    stats["position"] = "above" if mid > stats["p75"] else "below" if mid < stats["p25"] else "typical"
    return stats
