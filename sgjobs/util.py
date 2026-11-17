"""Shared helpers: HTTP, HTML-to-text, and the name normalisation used for de-duplication."""
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Your own data (profile, tracker, notes) lives in the project folder (small, safe to sync with OneDrive).
USER_DIR = ROOT / "data"
# The job cache is big (~270 MB), rewritten on every refresh and fully re-downloadable, so it lives
# outside OneDrive: a live SQLite file in a synced folder gets re-uploaded constantly and can corrupt.
# (Not AppData: Microsoft Store Python silently redirects AppData writes to a hidden per-app folder.)
CACHE_DIR = Path(os.environ.get("SGJOBS_CACHE") or Path.home() / ".sgjobs")
UA = "Mozilla/5.0 (compatible; chiongwork/0.3; personal job aggregator)"


def http_json(url, *, retries=2, timeout=25):
    """GET a URL and parse JSON. Returns None on 404; raises after retries on other errors."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
        except Exception as e:  # network hiccup
            last = e
        time.sleep(1 + attempt)
    raise RuntimeError(f"{url}: {last}")


class _Text(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(s):
    if not s:
        return ""
    p = _Text()
    p.feed(html.unescape(s) if "&lt;" in s else s)
    text = "".join(p.out)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_COMPANY_JUNK = re.compile(
    r"\b(pte|ltd|limited|private|inc|llc|plc|corp|corporation|co|company|holdings?|group|"
    r"singapore|sg|asia|pacific|apac|technologies|technology|tech|services|the)\b"
)


def norm_company(name):
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _COMPANY_JUNK.sub(" ", s)
    return " ".join(s.split())


_TITLE_JUNK = re.compile(
    r"(\$[\d,.k]+.*?(?=\s|$))|(#\w+)|\b(urgent|immediate|hiring|new|up to|contract|perm|permanent|"
    r"\d+\s*(months?|mths?|years?|yrs?))\b",
    re.I,
)


SENIORITY = {"intern", "junior", "associate", "senior", "staff", "principal", "lead", "head", "manager",
             "director", "vp", "chief", "executive", "assistant", "trainee", "graduate"}


def seniority(title_norm):
    return set(title_norm.split()) & SENIORITY


def norm_title(title):
    s = (title or "").lower()
    # strip pay/duration/hashtag noise but keep words like "(ETL)" or "(Java)" that change the job
    s = re.sub(r"\$\s?[\d,.]+\s*k?", " ", s)
    s = _TITLE_JUNK.sub(" ", s)
    s = re.sub(r"\b(to|uid \d+)\b", " ", s)
    s = s.replace("snr", "senior").replace("sr.", "senior").replace("jr.", "junior")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def fingerprint(company, title):
    return f"{norm_company(company)}|{norm_title(title)}"


# ---------------------------------------------------------------- hype-style sales ads
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2728]")
_SALESY = re.compile(r"sales|marketing|events?\b|road ?shows?|ambassador|brand rep|promoter|customer acquisition", re.I)
_STRONG = re.compile(
    r"travel (perks|opportunit)|youngsters|fun (sales|events?|marketing)|earn (&|and) learn"
    r"|no (sales |prior )?exp(erience)?\b.{0,30}(sales|marketing|events?)"
    r"|(sales|marketing|events?)\b.{0,40}no (prior )?exp(erience)?\b"
    r"|weekly (paid|pay).{0,30}(sales|marketing|events?)|(sales|marketing|events?)\b.{0,30}weekly (paid|pay)"
    r"|people[- ]person|1[- ]?(to|-)[- ]?1 mentorship|direct marketing (associate|rep|executive)"
    r"|(sales|marketing) (&|and) (marketing|brand|business development|events?) (trainee|associate|ambassador)"
    r"|brand outreach", re.I)


def is_hype(title, company=None, hype_companies=frozenset()):
    """Emoji-laden or 'no experience, travel perks' sales/event ads: usually commission-only roles.
    A company that posts several of these gets all its sales/marketing/events ads flagged too."""
    t = title or ""
    if (_EMOJI.search(t) and _SALESY.search(t)) or _STRONG.search(t):
        return 1
    return int(bool(company and company in hype_companies and _SALESY.search(t)))
