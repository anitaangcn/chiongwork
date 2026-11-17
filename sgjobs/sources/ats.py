"""Company career pages on public applicant-tracking-system (ATS) job boards.

Greenhouse, Lever, Ashby and SmartRecruiters all publish each company's open roles
through documented, unauthenticated endpoints meant for embedding job boards.
Only Singapore-based roles are kept.
"""
import json
from datetime import datetime, timezone

from ..util import http_json


def _is_sg(*parts):
    blob = " ".join(json.dumps(p) if not isinstance(p, str) else p for p in parts if p).lower()
    return "singapore" in blob


def _date_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat() if ms else None


def _base(co, ext_id, url, title, location, posted, employment_type=""):
    return {
        "source": co["ats"],
        "ext_id": f"{co['slug']}:{ext_id}",
        "url": url,
        "title": (title or "").strip(),
        "company": co["name"],
        "company_aliases": co.get("aliases", []) + [co["slug"]],
        "location": location or "Singapore",
        "posted": (posted or "")[:10] or None,
        "employment_type": employment_type or "",
    }


def greenhouse(co):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{co['slug']}/jobs") or {}
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        if _is_sg(loc):
            yield _base(co, j["id"], j.get("absolute_url"), j.get("title"), loc,
                        j.get("first_published") or j.get("updated_at"))


def lever(co):
    d = http_json(f"https://api.lever.co/v0/postings/{co['slug']}?mode=json") or []
    for j in d:
        cat = j.get("categories") or {}
        if not _is_sg(cat.get("location"), cat.get("allLocations"), j.get("country") == "SG" and "singapore"):
            continue
        item = _base(co, j["id"], j.get("hostedUrl"), j.get("text"), cat.get("location"),
                     _date_ms(j.get("createdAt")), cat.get("commitment"))
        sr = j.get("salaryRange") or {}
        if sr.get("currency") == "SGD" and sr.get("min"):
            div = 12 if (sr.get("interval") or "").startswith("per-year") else 1
            item["salary_min"], item["salary_max"] = round(sr["min"] / div), round(sr.get("max", sr["min"]) / div)
        yield item


def ashby(co):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{co['slug']}?includeCompensation=true") or {}
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        sec = [s.get("location") for s in j.get("secondaryLocations") or []]
        if not _is_sg(j.get("location"), sec):
            continue
        item = _base(co, j["id"], j.get("jobUrl"), j.get("title"), j.get("location"),
                     j.get("publishedAt"), j.get("employmentType"))
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        if comp and "SGD" in comp:
            item["salary_note"] = comp
        yield item


def smartrecruiters(co):
    offset = 0
    while True:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{co['slug']}/postings"
                      f"?country=sg&limit=100&offset={offset}") or {}
        content = d.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            yield _base(co, j["id"], f"https://jobs.smartrecruiters.com/{co['slug']}/{j['id']}",
                        j.get("name"), loc.get("city") or "Singapore", j.get("releasedDate"),
                        (j.get("typeOfEmployment") or {}).get("label"))
        offset += len(content)
        if not content or offset >= d.get("totalFound", 0):
            break


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}


def fetch_company(co):
    return list(FETCHERS[co["ats"]](co))
