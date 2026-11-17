"""ChiongWork — one terminal feed for Singapore jobs across many sources.

  cw fetch                      pull MyCareersFuture + company career pages
  cw search "data analyst" --min-salary 5000 --direct
  cw show 42 / open 42 / save 42 / status 42 applied
  cw tracker                    your saved / applied / interview pipeline
  cw import-emails alerts.mbox  add LinkedIn / JobStreet / Indeed alert emails
  cw browse                     interactive mode
  cw web                        local web app at http://127.0.0.1:8700
"""
import argparse
import json
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table

from . import cv, db, feed, geo, tracker, userdata
from .feed import SOURCE_LABEL, STATUSES
from . import gmail
from .sources import ats, email_alerts, mcf
from .util import ROOT

console = Console()
def load_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fetch
def cmd_fetch(args):
    cfg = load_json("config.json")
    conn = db.connect()
    ts = db.now()
    db.meta_set(conn, "prev_fetch", db.meta_get(conn, "last_fetch", ts))
    db.meta_set(conn, "last_fetch", ts)
    counts = {}

    def tally(src, res):
        c = counts.setdefault(src, {"new": 0, "merged": 0, "seen": 0})
        c[res] += 1

    delay = cfg.get("request_delay_seconds", 0.3)
    with Progress(console=console, transient=True) as prog:
        if not args.no_mcf:
            plan = [(None, 900 if args.full else cfg.get("mcf_newest_pages", 10))]
            if not args.full:
                kws = args.keywords or cfg.get("mcf_keywords", [])
                if not args.keywords:  # plus the roles from your profile
                    kws = kws + [r for r in userdata.profile().get("roles", []) if r.lower() not in {k.lower() for k in kws}]
                plan += [(k, cfg.get("mcf_pages_per_keyword", 3)) for k in kws]
            skipped = []
            for kw, pages in plan:
                task = prog.add_task(f"MyCareersFuture: {kw or 'newest jobs'}", total=pages)
                try:
                    for L in mcf.fetch(kw, pages, delay,
                                       progress=lambda p, n, tot, t=task: prog.update(t, completed=p,
                                                                                        total=min(n, -(-(tot or 0) // 100)) or n)):
                        tally("mcf", db.upsert(conn, L, ts))
                        if sum(counts["mcf"].values()) % 200 == 0:
                            conn.commit()  # long sweeps: keep progress and don't lock the DB for minutes
                except RuntimeError as e:  # a slow or dropped connection: keep what we got, move on
                    skipped.append(f"{kw or 'newest jobs'} ({str(e).rsplit(': ', 1)[-1]})")
                prog.update(task, completed=prog.tasks[task].total)
                conn.commit()
            if skipped:
                console.print(f"[yellow]MyCareersFuture searches cut short (network):[/] {', '.join(skipped)}. "
                              "The next refresh will pick them up.")
                if not counts.get("mcf"):
                    raise SystemExit("Couldn't reach MyCareersFuture. Check your internet connection and try again.")

        if not args.no_ats:
            companies = load_json("companies.json")
            task = prog.add_task(f"Company career pages ({len(companies)})", total=len(companies))
            failed = []
            with ThreadPoolExecutor(8) as ex:
                futs = {ex.submit(ats.fetch_company, co): co for co in companies}
                for fut in as_completed(futs):
                    co = futs[fut]
                    try:
                        for L in fut.result():
                            tally(co["ats"], db.upsert(conn, L, ts))
                        db.close_missing(conn, co["ats"], co["slug"], ts)
                    except Exception as e:
                        failed.append(f"{co['name']} ({e.__class__.__name__})")
                    prog.advance(task)
            conn.commit()
            if failed:
                console.print(f"[yellow]Skipped {len(failed)} career pages:[/] {', '.join(failed)}")

    if not getattr(args, "no_gmail", False) and gmail.status()["connected"]:
        try:
            g = gmail.check(conn, log=lambda m: console.print(f"[dim]{m}[/]"))
            console.print(f"Gmail: {g['job_emails']} alert emails → {g['links']} jobs "
                          f"([green]{g['new']} new[/], {g['merged']} matched, {g['seen']} already had)")
        except Exception as e:
            console.print(f"[yellow]Gmail check skipped:[/] {e}")

    t = Table(title="Fetch summary", show_edge=False)
    for col in ("Source", "New jobs", "Merged into existing", "Already had"):
        t.add_column(col, justify="right" if col != "Source" else "left")
    for src, c in counts.items():
        t.add_row(src, str(c["new"]), str(c["merged"]), str(c["seen"]))
    console.print(t)
    db.refresh_hype_companies(conn)
    if geo.home():
        geo.update_distances(conn)
    if cv.get_cv():
        n = cv.compute(conn)
        console.print(f"[dim]CV match scores updated for {n:,} jobs[/]")
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {db.active_clause()}").fetchone()[0]
    console.print(f"[bold]{total:,}[/] active jobs in your feed. Try: [cyan]cw search --new[/]")


# ---------------------------------------------------------------- search
def _query(conn, a):
    return feed.query(conn, a)


def fmt_salary(j):
    lo, hi = j["salary_min"], j["salary_max"]
    if not lo and not hi:
        return "[dim]—[/]"
    hourly = j["salary_period"] == "hour"
    k = (lambda v: f"{v:g}") if hourly else (lambda v: f"{v / 1000:.1f}k".replace(".0k", "k"))
    s = f"${k(lo)}–{k(hi)}" if hi and hi != lo else f"${k(lo or hi)}"
    if hourly:
        s += "/hr"
    if "srcs" in j.keys() and feed.salary_from_mcf(j):
        s += " [dim](MCF)[/]"
    return s


RICH_TONE = {"new": "bold green", "status": "bold magenta", "warn": "yellow", "bad": "red",
             "muted": "dim", "good": "cyan"}


def flags(j, last_fetch):
    return " ".join(f"[{RICH_TONE[tone]}]{text}[/]" for text, tone in feed.signals(j, last_fetch))


def render_list(rows, total, conn):
    last_fetch = db.meta_get(conn, "last_fetch")
    t = Table(show_lines=False, header_style="bold", expand=True)
    t.add_column("#", justify="right", style="cyan", no_wrap=True)
    t.add_column("Job", ratio=4)
    t.add_column("Company", ratio=2)
    t.add_column("Salary/mo", no_wrap=True)
    t.add_column("Posted", no_wrap=True)
    t.add_column("Seen on", ratio=1)
    t.add_column("Signals", ratio=2)
    for j in rows:
        srcs = feed.sources(j)
        t.add_row(str(j["id"]), j["title"], j["company"] or "", fmt_salary(j), (j["posted"] or "")[5:],
                  " + ".join(srcs), flags(j, last_fetch))
    console.print(t)
    console.print(f"[dim]Showing {len(rows)} of {total:,} matching jobs. "
                  f"`cw show <#>` for details, `cw save <#>` to track.[/]")


def cmd_search(a):
    conn = db.connect()
    rows, total = _query(conn, a)
    render_list(rows, total, conn)


# ---------------------------------------------------------------- one job
def get_job(conn, job_id):
    j = feed.get_job(conn, job_id)
    if not j:
        sys.exit(f"No job #{job_id}")
    return j


def cmd_show(a):
    conn = db.connect()
    j = get_job(conn, a.id)
    listings = conn.execute("SELECT * FROM listings WHERE job_id=? ORDER BY first_seen", (a.id,)).fetchall()
    info = [
        f"[bold]{j['title']}[/]\n{j['company']} · {j['location'] or 'Singapore'}",
        f"Salary: {fmt_salary(j)}   Type: {j['employment_type'] or '—'}   Level: {j['level'] or '—'}"
        + (f"   Min exp: {j['min_experience']}y" if j["min_experience"] is not None else ""),
    ]
    if j["salary_note"]:
        info.append(f"Pay note: {j['salary_note']}")
    if j["applicants"] is not None:
        info.append(f"Applicants: {j['applicants']}   Views: {j['views']}   Reposted: {j['reposts'] or 0}×   "
                    f"First posted: {j['original_posted']}   Closes: {j['expiry']}")
    sig = flags(j, db.meta_get(conn, "last_fetch"))
    if sig:
        info.append(sig)
    if j["skills"]:
        info.append(f"[dim]Skills: {j['skills']}[/]")
    info.append("\n[bold]Where it's listed[/]")
    for l in listings:
        closed = " [red](closed)[/]" if l["closed"] else ""
        info.append(f"  {SOURCE_LABEL.get(l['source'], l['source'])}: {l['url']}{closed}")
    if j["status"]:
        info.append(f"\nTracker: [magenta]{j['status']}[/] ({j['status_updated']})" + (f" — {j['note']}" if j["note"] else ""))
    console.print(Panel("\n".join(info), title=f"#{j['id']}", border_style="cyan"))
    if j["description"]:
        d = j["description"]
        console.print(d if a.full else d[:1500] + ("\n[dim]… (--full for the rest)[/]" if len(d) > 1500 else ""))


def cmd_open(a):
    conn = db.connect()
    rows = conn.execute("SELECT url, source FROM listings WHERE job_id=? AND closed=0 ORDER BY "
                        "CASE WHEN source IN ('greenhouse','lever','ashby','smartrecruiters') THEN 0 WHEN source='mcf' THEN 1 ELSE 2 END", (a.id,)).fetchall()
    if not rows:
        sys.exit("No open listing for that job.")
    webbrowser.open(rows[0]["url"])
    console.print(f"Opened {rows[0]['url']}")


def cmd_status(a):
    conn = db.connect()
    get_job(conn, a.id)
    tracker.set_status_for_job(conn, a.id, a.status, a.note)
    console.print(f"#{a.id} → [magenta]{a.status}[/]")


def cmd_tracker(a):
    conn = db.connect()
    rows = feed.tracked(conn)
    if not rows:
        console.print("Nothing tracked yet. Use [cyan]cw save <#>[/] or [cyan]cw status <#> applied[/].")
        return
    t = Table(title="Your pipeline", expand=True)
    for c in ("#", "Status", "Job", "Company", "Salary/mo", "Updated", "Note"):
        t.add_column(c)
    for j in rows:
        t.add_row(str(j["id"]), j["status"], j["title"], j["company"], fmt_salary(j),
                  (j["status_updated"] or "")[:10], j["note"] or "")
    console.print(t)


# ---------------------------------------------------------------- email alerts
def cmd_import(a):
    if a.imap:
        listings, emails = email_alerts.from_imap(a.imap, a.user, a.days)
    else:
        listings, emails = email_alerts.from_path(a.path)
    conn = db.connect()
    ts = db.now()
    res = {"new": 0, "merged": 0, "seen": 0}
    for L in listings:
        res[db.upsert(conn, L, ts)] += 1
    conn.commit()
    console.print(f"Read {emails} alert emails → {len(listings)} job links: "
                  f"[green]{res['new']} new[/], {res['merged']} matched jobs already in your feed, {res['seen']} already imported.")


def cmd_gmail(a):
    conn = db.connect()
    g = gmail.check(conn, days=a.days, log=lambda m: console.print(f"[dim]{m}[/]"))
    console.print(f"Read {g['emails']} alert emails ({g['job_emails']} had jobs) → {g['links']} job links: "
                  f"[green]{g['new']} new[/], {g['merged']} matched jobs already in your feed, {g['seen']} already imported.")
    for e in json.loads(db.meta_get(conn, "gmail_no_jobs") or "[]"):
        console.print(f"  [yellow]no jobs found in:[/] {e['from']} | {e['subject']}")


# ---------------------------------------------------------------- misc
def cmd_stats(a):
    conn = db.connect()
    act = db.active_clause()
    q = lambda sql: conn.execute(sql).fetchone()[0]
    total = q(f"SELECT COUNT(*) FROM jobs j WHERE {act}")
    t = Table(title="Your feed", show_edge=False)
    t.add_column("")
    t.add_column("", justify="right")
    t.add_row("Active jobs", f"{total:,}")
    for src, n in conn.execute(f"SELECT l.source, COUNT(DISTINCT l.job_id) FROM listings l JOIN jobs j ON j.id=l.job_id "
                               f"WHERE l.closed=0 AND {act} GROUP BY l.source ORDER BY 2 DESC"):
        t.add_row(f"  seen on {src}", f"{n:,}")
    t.add_row("Listed on 2+ sources (de-duplicated)",
              f"{q(f'SELECT COUNT(*) FROM jobs j WHERE {act} AND (SELECT COUNT(DISTINCT source) FROM listings l WHERE l.job_id=j.id)>1'):,}")
    t.add_row("With a salary range", f"{q(f'SELECT COUNT(*) FROM jobs j WHERE {act} AND salary_max IS NOT NULL'):,}")
    t.add_row("Career-page jobs given a salary from MCF",
              f"{q(f'''SELECT COUNT(*) FROM jobs j WHERE {act} AND salary_source='mcf' AND EXISTS (SELECT 1 FROM listings l WHERE l.job_id=j.id AND l.source!='mcf')'''):,}")
    t.add_row("Reposted at least once", f"{q(f'SELECT COUNT(*) FROM jobs j WHERE {act} AND reposts>0'):,}")
    t.add_row("Posted by recruitment agencies", f"{q(f'SELECT COUNT(*) FROM jobs j WHERE {act} AND agency=1'):,}")
    t.add_row("Last fetch", db.meta_get(conn, "last_fetch", "never"))
    console.print(t)


def cmd_keywords(a):
    cfg = load_json("config.json")
    kws = cfg.setdefault("mcf_keywords", [])
    if a.action == "add":
        kws += [k for k in a.words if k not in kws]
    elif a.action == "remove":
        cfg["mcf_keywords"] = kws = [k for k in kws if k not in a.words]
    (ROOT / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    console.print("Keywords fetched from MyCareersFuture: " + ", ".join(f"[cyan]{k}[/]" for k in kws))


def cmd_browse(a):
    """Tiny interactive loop over a search."""
    conn = db.connect()
    console.print("[bold]cw browse[/] — type a search (e.g. [cyan]data analyst[/]), "
                  "a job number to view it, [cyan]s 12[/] save, [cyan]a 12[/] applied, [cyan]h 12[/] hide, "
                  "[cyan]o 12[/] open in browser, [cyan]more[/], [cyan]q[/] quit.")
    state = argparse.Namespace(query="", min_salary=None, max_applicants=None, direct=False, type=None, level=None,
                               days=None, new=False, source=None, multi=False, show_hidden=False, sort="date", limit=20)
    rows, total = _query(conn, state)
    render_list(rows, total, conn)
    while True:
        try:
            cmd = console.input("[bold cyan]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("q", "quit", "exit"):
            break
        parts = cmd.split(maxsplit=1)
        if cmd.isdigit():
            cmd_show(argparse.Namespace(id=int(cmd), full=False))
        elif parts and parts[0] in ("s", "a", "h", "i") and len(parts) == 2 and parts[1].isdigit():
            st = {"s": "saved", "a": "applied", "h": "hidden", "i": "interview"}[parts[0]]
            cmd_status(argparse.Namespace(id=int(parts[1]), status=st, note=None))
        elif parts and parts[0] == "o" and len(parts) == 2 and parts[1].isdigit():
            cmd_open(argparse.Namespace(id=int(parts[1])))
        elif cmd == "more":
            state.limit += 20
            rows, total = _query(conn, state)
            render_list(rows, total, conn)
        elif cmd.startswith("$") and cmd[1:].isdigit():
            state.min_salary = int(cmd[1:])
            rows, total = _query(conn, state)
            render_list(rows, total, conn)
        else:
            state.query, state.limit = cmd, 20
            rows, total = _query(conn, state)
            render_list(rows, total, conn)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cw", description="One feed for Singapore jobs across many sites.")
    sub = p.add_subparsers(dest="cmd")

    f = sub.add_parser("fetch", help="pull fresh jobs from all sources")
    f.add_argument("--keywords", nargs="*", help="override MCF keywords from config.json")
    f.add_argument("--full", action="store_true", help="sweep ALL of MyCareersFuture (~900 requests, ~5 min)")
    f.add_argument("--no-mcf", action="store_true")
    f.add_argument("--no-ats", action="store_true")
    f.add_argument("--no-gmail", action="store_true", help="skip reading job-alert emails from Gmail")
    f.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("search", help="search your feed")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--min-salary", type=int, help="monthly SGD; jobs whose range reaches at least this")
    s.add_argument("--max-applicants", type=int, help="hide crowded jobs")
    s.add_argument("--direct", action="store_true", help="hide recruitment-agency postings")
    s.add_argument("--type", help="Full Time, Contract, Part Time, Internship...")
    s.add_argument("--level", help="Executive, Senior Executive, Manager, Fresh/entry level...")
    s.add_argument("--days", type=int, help="posted in the last N days")
    s.add_argument("--new", action="store_true", help="only jobs that appeared in the latest fetch")
    s.add_argument("--source", choices=["mcf", "careers", "linkedin", "jobstreet", "indeed", "glassdoor"])
    s.add_argument("--multi", action="store_true", help="only jobs seen on 2+ sources")
    s.add_argument("--anywhere", action="store_true", help="also match skills and category, not just title/company")
    s.add_argument("--show-hidden", action="store_true")
    s.add_argument("--sort", choices=["date", "salary", "applicants"], default="date")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_search)

    sh = sub.add_parser("show", help="full details of one job")
    sh.add_argument("id", type=int)
    sh.add_argument("--full", action="store_true")
    sh.set_defaults(fn=cmd_show)

    o = sub.add_parser("open", help="open the job in your browser")
    o.add_argument("id", type=int)
    o.set_defaults(fn=cmd_open)

    sv = sub.add_parser("save", help="add to your tracker")
    sv.add_argument("id", type=int)
    sv.add_argument("--note")
    sv.set_defaults(fn=lambda a: cmd_status(argparse.Namespace(id=a.id, status="saved", note=a.note)))

    st = sub.add_parser("status", help="set tracker status")
    st.add_argument("id", type=int)
    st.add_argument("status", choices=STATUSES)
    st.add_argument("--note")
    st.set_defaults(fn=cmd_status)

    sub.add_parser("tracker", help="show your pipeline").set_defaults(fn=cmd_tracker)
    sub.add_parser("stats", help="what's in your feed").set_defaults(fn=cmd_stats)
    sub.add_parser("browse", help="interactive mode").set_defaults(fn=cmd_browse)

    im = sub.add_parser("import-emails", help="add jobs from LinkedIn/JobStreet/Indeed alert emails")
    im.add_argument("path", nargs="?", help=".eml file, folder of .eml files, or .mbox export")
    im.add_argument("--imap", help="IMAP host, e.g. imap.gmail.com (password from SGJOBS_IMAP_PASSWORD)")
    im.add_argument("--user", help="your email address for IMAP")
    im.add_argument("--days", type=int, default=14)
    im.set_defaults(fn=cmd_import)

    g = sub.add_parser("gmail-check", help="read new job-alert emails from your connected Gmail")
    g.add_argument("--days", type=int, help="look back this many days (default: since the last check)")
    g.set_defaults(fn=cmd_gmail)

    w = sub.add_parser("web", help="open the local web app (http://127.0.0.1:8700)")
    w.add_argument("--port", type=int, default=8700)
    w.add_argument("--no-browser", action="store_true")
    w.set_defaults(fn=lambda a: __import__("sgjobs.web", fromlist=["run"]).run(a.port, not a.no_browser))

    k = sub.add_parser("keywords", help="list/add/remove MCF search keywords")
    k.add_argument("action", choices=["list", "add", "remove"], nargs="?", default="list")
    k.add_argument("words", nargs="*")
    k.set_defaults(fn=cmd_keywords)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        console.print(__doc__)
        return
    if a.cmd == "import-emails" and not (a.path or a.imap):
        p.error("give a file/folder path, or --imap HOST --user EMAIL")
    a.fn(a)


if __name__ == "__main__":
    main()
