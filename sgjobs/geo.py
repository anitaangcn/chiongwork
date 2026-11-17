"""Commute estimates from your home, worked out offline.

- Your home: you type a Singapore postal code. It's located using the postal codes and map coordinates
  that MyCareersFuture publishes for job addresses (tens of thousands of points across the island),
  so it never leaves this PC: the exact code if a job has it, else the nearest code in the same postal
  sector, else the sector's centre.
- Each job: MyCareersFuture gives the workplace's coordinates. Jobs from LinkedIn, Indeed or company
  career pages have no address, so they show no distance (and are never filtered out for it).
- Time: straight-line distance turned into a rough public-transport time (about 8 min to get going plus
  2.7 min per km), calibrated on typical MRT trips. It's an estimate, not a route plan.
"""
import math

from . import db, userdata

BASE_MIN, MIN_PER_KM = 8.0, 2.7


def minutes(km):
    return None if km is None else int(round(BASE_MIN + MIN_PER_KM * km))


def km_for_minutes(mins):
    return max(0.0, (mins - BASE_MIN) / MIN_PER_KM)


def km(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _points(conn):
    return conn.execute("SELECT postal, AVG(lat) lat, AVG(lng) lng FROM jobs WHERE postal IS NOT NULL AND lat IS NOT NULL "
                        "AND lat BETWEEN 1.15 AND 1.48 AND lng BETWEEN 103.6 AND 104.1 GROUP BY postal").fetchall()


def locate(conn, postal):
    """(lat, lng, precision) for a Singapore postal code, or None."""
    postal = "".join(ch for ch in str(postal or "") if ch.isdigit())
    if len(postal) != 6:
        return None
    pts = _points(conn)
    exact = next((r for r in pts if r["postal"] == postal), None)
    if exact:
        return exact["lat"], exact["lng"], "exact"
    sector = [r for r in pts if r["postal"][:2] == postal[:2] and r["postal"].isdigit()]
    if not sector:
        return None
    near = min(sector, key=lambda r: abs(int(r["postal"]) - int(postal)))
    if abs(int(near["postal"]) - int(postal)) <= 60:  # neighbouring codes are usually the same street / estate
        return near["lat"], near["lng"], "near"
    return (sum(r["lat"] for r in sector) / len(sector), sum(r["lng"] for r in sector) / len(sector), "area")


def fill_missing(conn):
    """Jobs with a postal code but no coordinates (MCF hides some) get them from other jobs at that code."""
    pts = {r["postal"]: (r["lat"], r["lng"]) for r in _points(conn)}
    todo = [(pts[r["postal"]][0], pts[r["postal"]][1], r["id"])
            for r in conn.execute("SELECT id, postal FROM jobs WHERE lat IS NULL AND postal IS NOT NULL") if r["postal"] in pts]
    conn.executemany("UPDATE jobs SET lat=?, lng=? WHERE id=?", todo)


def home():
    h = userdata.load_key("home") or {}
    return h if h.get("lat") is not None else None


def set_home(conn, postal):
    if not postal:
        userdata.save_key("home", None)
        conn.execute("UPDATE jobs SET home_km = NULL")
        conn.commit()
        return None
    loc = locate(conn, postal)
    if not loc:
        raise ValueError("Couldn't place that postal code. Check it's a 6-digit Singapore postal code, "
                         "or try the postal code of a nearby MRT station or mall.")
    h = {"postal": "".join(ch for ch in postal if ch.isdigit()), "lat": loc[0], "lng": loc[1], "precision": loc[2]}
    userdata.save_key("home", h)
    update_distances(conn)
    return h


def update_distances(conn):
    """Recompute every job's distance from home (after a fetch or a new home). ~1 s for 70k jobs."""
    h = home()
    if not h:
        conn.execute("UPDATE jobs SET home_km = NULL WHERE home_km IS NOT NULL")
        conn.commit()
        return 0
    fill_missing(conn)
    conn.commit()
    rows = conn.execute("SELECT id, lat, lng, home_km FROM jobs").fetchall()
    changes = []
    for r in rows:
        d = None if r["lat"] is None else round(km(h["lat"], h["lng"], r["lat"], r["lng"]), 2)
        if d != r["home_km"]:
            changes.append((d, r["id"]))
    for i in range(0, len(changes), 5000):  # short write bursts: a running job refresh can keep going
        conn.executemany("UPDATE jobs SET home_km=? WHERE id=?", changes[i:i + 5000])
        conn.commit()
    return len(changes)


def work_mode(location, flex):
    """'Remote' / 'Hybrid' / 'Some WFH' from LinkedIn-style locations or MCF's flexible-work list."""
    loc = (location or "").lower()
    if "remote" in loc:
        return "Remote"
    if "hybrid" in loc:
        return "Hybrid"
    if flex and "telecommut" in flex.lower():
        return "Some WFH"
    return None
