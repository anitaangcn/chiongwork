"""Your own data: what you're looking for (profile), your applications (tracker) and the connected Gmail address.

Stored in data/my-chiongwork.json inside the project folder: small, safe to sync with OneDrive.
The job cache only mirrors tracker statuses for fast badges; tracker.restore() rebuilds that
mirror if the cache is ever deleted.
"""
import json
import threading

from .util import USER_DIR

_LOCK = threading.RLock()

FILE = USER_DIR / "my-chiongwork.json"
OLD_FILE = USER_DIR / "my-sgjobs.json"
DEFAULT_PROFILE = {"roles": [], "min_pay": None, "levels": [], "direct": False, "hide_noise": True, "setup_done": False,
                   "max_commute": None}


def _load():
    if not FILE.exists() and OLD_FILE.exists():  # renamed from sgjobs
        OLD_FILE.replace(FILE)
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save(data):
    USER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)  # atomic: never leaves a half-written file


def load_key(key):
    return _load().get(key)


def save_key(key, value):
    with _LOCK:  # each writer only replaces its own key, so Gmail checks and tracker edits never undo each other
        data = _load()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        _save(data)


def profile():
    return {**DEFAULT_PROFILE, **(_load().get("profile") or {})}


def save_profile(p):
    clean = {
        "roles": [r.strip() for r in p.get("roles", []) if isinstance(r, str) and r.strip()][:12],
        "min_pay": int(p["min_pay"]) if str(p.get("min_pay") or "").isdigit() else None,
        "levels": [l for l in p.get("levels", []) if isinstance(l, str)][:10],
        "direct": bool(p.get("direct")),
        "hide_noise": bool(p.get("hide_noise", True)),
        "max_commute": int(p["max_commute"]) if str(p.get("max_commute") or "").isdigit() else None,
        "setup_done": True,
    }
    save_key("profile", clean)  # "Refresh jobs" also searches MyCareersFuture for these roles (see cmd_fetch)
    return clean


def gmail():
    """Which Gmail is connected (the address only; the app password lives in Windows Credential Manager)."""
    return _load().get("gmail") or {}


def save_gmail(info):
    save_key("gmail", info)
