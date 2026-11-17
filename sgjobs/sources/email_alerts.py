"""Job-alert emails the user already receives from LinkedIn, JobStreet, Indeed, Glassdoor etc.

Nothing here touches those websites: it only reads alert emails from the user's own
mailbox (exported .eml/.mbox files, or their IMAP inbox with their own app password).
"""
import base64
import email
import email.policy
import gzip
import imaplib
import json
import mailbox
import os
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

# (source, sender-domain hint, job-link regex -> id group)
PATTERNS = [
    ("linkedin", "linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")),
    ("jobstreet", "jobstreet", re.compile(r"jobstreet\.com(?:\.sg)?/(?:en/)?job/(\d+)")),
    ("jobstreet", "jobstreet", re.compile(r"jobstreet[^\"'\s]*?[?&](?:jobId|jobid)=(\d+)")),
    ("indeed", "indeed", re.compile(r"indeed\.com[^\"'\s]*?[?&]jk=([0-9a-f]{8,})")),
    ("glassdoor", "glassdoor", re.compile(r"glassdoor\.[a-z.]+[^\"'\s]*?(?:jobListingId|jl)=(\d+)")),
]
SALARY = re.compile(r"\$\s?([\d,]{3,})\s*(?:–|-|to)\s*\$?\s?([\d,]{3,})")
SKIP_TEXT = re.compile(r"^(view( job)?|view all jobs|[\w &,/-]{2,40} jobs|apply( now)?|see (all|more)( jobs)?|easy apply|save|"
                       r"unsubscribe|learn more|new|promoted|actively recruiting|\d+ (applicants?|connections?).*|"
                       r"be an early applicant|salary|job type|work setting|job description preview)$", re.I)


def _unwrap(href):
    """Indeed wraps job links as cts.indeed.com/v3/<gzip+base64 JSON>; the real URL is inside."""
    m = re.search(r"cts\.indeed\.com/v\d/([A-Za-z0-9_-]{20,})", href or "")
    if not m:
        return href
    try:
        blob = m.group(1)
        raw = gzip.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("u"), str):
                return data["u"]
        except ValueError:
            pass
        m = re.search(r"https?://[^\"'\s]+", raw)
        return m.group(0) if m else href
    except Exception:
        return href


class _Links(HTMLParser):
    """Collect text chunks in order, remembering which link each chunk sits inside."""

    def __init__(self):
        super().__init__()
        self.chunks = []  # (href or None, text)
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = _unwrap(dict(attrs).get("href"))

    def handle_endtag(self, tag):
        if tag == "a":
            self.href = None

    def handle_data(self, data):
        t = " ".join(data.split())
        if t:
            self.chunks.append((self.href, t))


def _match(href):
    for source, _, rx in PATTERNS:
        m = rx.search(href or "")
        if m:
            return source, m.group(1)
    return None


def parse_html(html_body, received=None):
    p = _Links()
    p.feed(html_body)
    found = {}
    chunks = p.chunks
    for i, (href, text) in enumerate(chunks):
        hit = _match(href)
        if not hit or SKIP_TEXT.match(text) or len(text) < 3 or len(text) > 140:
            continue
        source, ext = hit
        key = (source, ext)
        if key in found and found[key]["title"]:
            continue
        # The next one or two plain-text chunks after the title are usually "Company · Location".
        after = [t for h, t in chunks[i + 1:i + 6] if not SKIP_TEXT.match(t) and _match(h) != hit][:3]
        dotted = [t for t in after if re.search(r"\s[·•]\s", t)]
        if dotted:  # LinkedIn: "Company · Location" is the reliable line; skip stray tags before it
            after = dotted[:1]
        else:
            after = after[:2]
        company, location = "", "Singapore"
        if after:
            parts = re.split(r"\s+[·•|–-]\s+", after[0])
            company = parts[0]
            if len(parts) > 1:
                location = parts[1]
            elif len(after) > 1 and len(after[1]) < 60:
                location = after[1]
        item = {
            "source": source,
            "ext_id": ext,
            "url": _canonical(source, ext, href),
            "title": text,
            "company": company,
            "location": location,
            "posted": received,
            # Alert emails don't say when a job closes; assume ~30 days unless MCF gives a real expiry.
            "expiry": (datetime.fromisoformat(received) + timedelta(days=30)).date().isoformat() if received else None,
        }
        for _, t in chunks[i + 1:i + 6]:
            m = SALARY.search(t)
            if m:
                lo, hi = (int(x.replace(",", "")) for x in m.groups())
                if hi > 30000 or re.search(r"\b(a|per) (year|annum)\b", t, re.I):  # annual figure
                    lo, hi = round(lo / 12), round(hi / 12)
                item["salary_min"], item["salary_max"] = lo, hi
                if re.search(r"\b(an|per) hour\b", t, re.I):
                    item["salary_period"] = "hour"
                break
        found[key] = item
    return list(found.values())


def _canonical(source, ext, href):
    return {
        "linkedin": f"https://www.linkedin.com/jobs/view/{ext}",
        "jobstreet": f"https://sg.jobstreet.com/job/{ext}",
        "indeed": f"https://sg.indeed.com/viewjob?jk={ext}",
    }.get(source, href)


def _from_message(msg):
    sender = (msg.get("From") or "").lower()
    if not any(hint in sender for _, hint, _ in PATTERNS):
        return []
    try:
        received = email.utils.parsedate_to_datetime(msg.get("Date")).date().isoformat()
    except Exception:
        received = None
    out = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                out += parse_html(part.get_content(), received)
            except Exception:
                payload = part.get_payload(decode=True) or b""
                out += parse_html(payload.decode("utf-8", "replace"), received)
    return out


def from_path(path):
    """Read a .eml file, a folder of .eml files, or an .mbox export (e.g. Google Takeout)."""
    path = Path(path)
    files = sorted(path.glob("**/*.eml")) if path.is_dir() else [path]
    listings, emails = [], 0
    for f in files:
        if f.suffix.lower() == ".mbox":
            for msg in mailbox.mbox(str(f), factory=lambda fp: email.message_from_binary_file(fp, policy=email.policy.default)):
                got = _from_message(msg)
                emails += bool(got)
                listings += got
        else:
            msg = email.message_from_bytes(f.read_bytes(), policy=email.policy.default)
            got = _from_message(msg)
            emails += bool(got)
            listings += got
    return listings, emails


def from_imap(host, user, days=14, password_env="SGJOBS_IMAP_PASSWORD"):
    """Read alert emails straight from the user's inbox. The password comes from an env var
    (e.g. a Gmail app password the user creates themselves) and is never stored."""
    password = os.environ.get(password_env)
    if not password:
        raise SystemExit(f"Set the {password_env} environment variable to your email app password first.")
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    listings, emails = [], 0
    with imaplib.IMAP4_SSL(host) as box:
        box.login(user, password)
        box.select("INBOX", readonly=True)
        for hint in ("linkedin", "jobstreet", "indeed", "glassdoor"):
            _, data = box.search(None, f'(SINCE {since} FROM "{hint}")')
            for num in data[0].split():
                _, msg_data = box.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
                got = _from_message(msg)
                emails += bool(got)
                listings += got
    return listings, emails
