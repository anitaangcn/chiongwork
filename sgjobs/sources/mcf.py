"""MyCareersFuture (Singapore government job portal) via its public JSON API.

Every listing carries a salary range, applicant count, repost count and expiry date,
which is what powers salary filtering and the ghost-job flags in the rest of the app.
"""
import time
import urllib.parse

from ..util import html_to_text, http_json

API = "https://api.mycareersfuture.gov.sg/v2/jobs"
PAGE_SIZE = 100


def _listing(j):
    md = j.get("metadata") or {}
    sal = j.get("salary") or {}
    comp = j.get("postedCompany") or {}
    hiring = j.get("hiringCompany") or {}
    smin, smax = sal.get("minimum"), sal.get("maximum")
    stype = ((sal.get("type") or {}).get("salaryType") or "Monthly").lower()
    if md.get("isHideSalary"):
        smin = smax = None
    elif stype.startswith("annual") and smin:
        smin, smax = round(smin / 12), round((smax or smin) / 12)
    # Employers often type an hourly rate into MCF's "Monthly" field ($12-15). Nobody earns <$100/month.
    period = "hour" if smax and smax < 100 else "month"
    addr = j.get("address") or {}
    districts = addr.get("districts") or []
    location = ", ".join(sorted({d.get("location") for d in districts if d.get("location")})) or "Singapore"
    ssic = str(comp.get("ssicCode2020") or comp.get("ssicCode") or "")
    company = hiring.get("name") or comp.get("name") or ""
    return {
        "source": "mcf",
        "ext_id": j["uuid"],
        "url": md.get("jobDetailsUrl") or f"https://www.mycareersfuture.gov.sg/job/{j['uuid']}",
        "title": (j.get("title") or "").strip(),
        "company": company,
        "location": location,
        "salary_min": smin,
        "salary_max": smax,
        "salary_period": period,
        "employment_type": ", ".join(e["employmentType"] for e in j.get("employmentTypes") or []),
        "level": ", ".join(p["position"] for p in j.get("positionLevels") or []),
        "category": ", ".join(c["category"] for c in j.get("categories") or []),
        "skills": ", ".join(s["skill"] for s in (j.get("skills") or [])[:15]),
        "posted": md.get("newPostingDate"),
        "original_posted": md.get("originalPostingDate"),
        "expiry": md.get("expiryDate"),
        "applicants": md.get("totalNumberJobApplication"),
        "views": md.get("totalNumberOfView"),
        "reposts": md.get("repostCount"),
        "responsive": 1 if (comp.get("responsiveEmployer") or {}).get("isResponsive") else 0,
        # SSIC 78xxx = employment/recruitment agencies; also "posted on behalf of" a client
        "agency": 1 if ssic.startswith("78") or md.get("isPostedOnBehalf") else 0,
        "min_experience": j.get("minimumYearsExperience"),
        "description": html_to_text(j.get("description"))[:6000],
        # workplace location, for commute estimates (private addresses come without coordinates)
        "lat": addr.get("lat") if not addr.get("isOverseas") else None,
        "lng": addr.get("lng") if not addr.get("isOverseas") else None,
        "postal": (addr.get("postalCode") or "").strip() or None,
        "flex": ", ".join(f["flexibleWorkArrangement"] for f in j.get("flexibleWorkArrangements") or []
                          if f.get("flexibleWorkArrangement")) or None,
    }


def fetch(search=None, pages=3, delay=0.3, progress=None):
    """Yield listings, newest first. search=None sweeps the newest jobs overall."""
    for page in range(pages):
        params = {"limit": PAGE_SIZE, "page": page, "sortBy": "new_posting_date"}
        if search:
            params["search"] = search
        data = http_json(f"{API}?{urllib.parse.urlencode(params)}")
        results = (data or {}).get("results") or []
        if progress:
            progress(page + 1, pages, (data or {}).get("total"))
        for j in results:
            yield _listing(j)
        if len(results) < PAGE_SIZE:
            break
        time.sleep(delay)


def total_active():
    data = http_json(f"{API}?limit=1&page=0")
    return (data or {}).get("total")
