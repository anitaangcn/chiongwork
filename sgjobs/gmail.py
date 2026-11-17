"""Read job-alert emails (LinkedIn, JobStreet, Indeed, Glassdoor) straight from your Gmail.

- You create a Gmail *app password* yourself; it is kept in the system's password store
  (Windows Credential Manager; the macOS Keychain or Secret Service on Mac/Linux via `keyring`),
  never in a file.
- The mailbox is opened read-only (IMAP EXAMINE + BODY.PEEK): nothing is marked read,
  moved, deleted or sent. Only emails from job sites are downloaded; everything else is
  never fetched.
- Your Gmail address is kept in data/my-chiongwork.json so the connection survives a cache reset.
"""
import email
import email.policy
import imaplib
import json
import re
import socket
import sys
from datetime import datetime, timedelta

from . import db, userdata
from .sources import email_alerts

HOST = "imap.gmail.com"
TARGET = "sgjobs:gmail"  # name of the entry in the password store (kept for existing installs)
FIRST_LOOKBACK_DAYS = 30
# Gmail search (X-GM-RAW). {a b} means "a OR b". Only these senders are ever looked at.
SENDERS = "{from:linkedin.com from:jobstreet from:indeed from:glassdoor from:seek.com}"
JOBSITE = re.compile(r"linkedin|jobstreet|indeed|glassdoor|seek", re.I)


# ---------------------------------------------------------------- password store
if sys.platform == "win32":  # Windows Credential Manager, no extra install
    import ctypes
    from ctypes import wintypes

    class _CRED(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _CRED_TYPE_GENERIC, _PERSIST_LOCAL_MACHINE = 1, 2

    def _advapi():
        return ctypes.WinDLL("advapi32", use_last_error=True)

    def _save_secret(user, secret):
        blob = secret.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        c = _CRED(Type=_CRED_TYPE_GENERIC, TargetName=TARGET, UserName=user, Persist=_PERSIST_LOCAL_MACHINE,
                  CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)),
                  Comment="ChiongWork: read-only access to job-alert emails")
        if not _advapi().CredWriteW(ctypes.byref(c), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def _load_secret():
        api = _advapi()
        p = ctypes.POINTER(_CRED)()
        if not api.CredReadW(TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            return None, None
        try:
            c = p.contents
            return c.UserName, ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        finally:
            api.CredFree(p)

    def _delete_secret():
        try:
            _advapi().CredDeleteW(TARGET, _CRED_TYPE_GENERIC, 0)
        except Exception:
            pass

else:  # macOS Keychain / Linux Secret Service, through the keyring package
    def _keyring():
        try:
            import keyring
            return keyring
        except ImportError as e:
            raise GmailError("Saving the Gmail app password needs the keyring package: run "
                             "`pip install -r requirements.txt` in the ChiongWork folder.") from e

    def _save_secret(user, secret):
        kr = _keyring()
        kr.set_password(TARGET, "address", user)
        kr.set_password(TARGET, "password", secret)

    def _load_secret():
        try:
            kr = _keyring()
            return kr.get_password(TARGET, "address"), kr.get_password(TARGET, "password")
        except Exception:
            return None, None

    def _delete_secret():
        try:
            kr = _keyring()
            for k in ("address", "password"):
                kr.delete_password(TARGET, k)
        except Exception:
            pass


# ---------------------------------------------------------------- connection
class GmailError(Exception):
    """A problem with a friendly explanation for the page."""


def _login(address, password):
    try:
        box = imaplib.IMAP4_SSL(HOST, timeout=30)
    except (socket.gaierror, OSError) as e:
        raise GmailError("Couldn't reach Gmail. Check your internet connection and try again.") from e
    try:
        box.login(address, password)
    except imaplib.IMAP4.error as e:
        msg = str(e)
        box.shutdown()
        if "Application-specific password required" in msg or "app password" in msg.lower():
            raise GmailError("Gmail wants an app password here, not your normal password. "
                             "Create one with the link in step 2.") from e
        if "IMAP" in msg and ("disabled" in msg or "enable" in msg):
            raise GmailError("IMAP is turned off for this Gmail. Turn it on in Gmail → Settings → "
                             "See all settings → Forwarding and POP/IMAP.") from e
        raise GmailError("Gmail said the address or app password is wrong. Copy the 16-letter app password "
                         "again (spaces don't matter) and check the address.") from e
    return box


def _all_mail(box):
    """Gmail's 'All Mail' folder (its name depends on your Gmail language), so alerts that a
    filter archived or that sit under Promotions/Updates are still found. Falls back to INBOX."""
    _, rows = box.list()
    for raw in rows or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                return '"' + m.group(1) + '"'
    return "INBOX"


def status():
    data = userdata.gmail()
    user, secret = (None, None)
    try:
        user, secret = _load_secret()
    except Exception:
        pass
    return {"connected": bool(data.get("address") and secret), "address": data.get("address"),
            "connected_at": data.get("connected_at"), "last_check": data.get("last_check"),
            "last_result": data.get("last_result"), "totals": data.get("totals")}


def connect(address, password):
    address = (address or "").strip()
    password = re.sub(r"\s+", "", password or "")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise GmailError("That doesn't look like an email address.")
    if len(password) < 8:
        raise GmailError("Paste the 16-letter app password from Google (step 2).")
    box = _login(address, password)  # prove it works before saving anything
    try:
        box.logout()
    except Exception:
        pass
    _save_secret(address, password)
    userdata.save_gmail({"address": address, "connected_at": db.now()})
    c = db.connect()
    db.meta_set(c, "gmail_seen", "{}")  # a (re)connected mailbox is read from scratch
    c.commit()
    c.close()
    return status()


def disconnect():
    _delete_secret()
    userdata.save_gmail({})
    return status()


# ---------------------------------------------------------------- reading alerts
def check(conn=None, days=None, log=print):
    """Fetch new job-alert emails and add their jobs to the feed. Returns a summary dict."""
    data = userdata.gmail()
    address = data.get("address")
    _, password = _load_secret()
    if not (address and password):
        raise GmailError("Gmail isn't connected yet.")
    conn = conn or db.connect()
    if days is None:
        days = FIRST_LOOKBACK_DAYS
        if data.get("last_check"):
            gap = (datetime.now() - datetime.fromisoformat(data["last_check"])).days
            days = max(3, min(FIRST_LOOKBACK_DAYS, gap + 2))
    seen = json.loads(db.meta_get(conn, "gmail_seen") or "{}")  # {uidvalidity: [uids]}

    box = _login(address, password)
    res = {"emails": 0, "job_emails": 0, "links": 0, "new": 0, "merged": 0, "seen": 0, "by_source": {},
           "no_jobs": [], "days": days}
    try:
        folder = _all_mail(box)
        typ, _ = box.select(folder, readonly=True)
        if typ != "OK":
            folder = "INBOX"
            box.select(folder, readonly=True)
        validity = (box.response("UIDVALIDITY")[1] or [b"0"])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity)
        done = set(seen.get(validity, []))
        query = f"{SENDERS} newer_than:{days}d"
        typ, found = box.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if typ != "OK":  # not Gmail's extension? plain IMAP search per sender
            uids = set()
            since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            for hint in ("linkedin", "jobstreet", "indeed", "glassdoor"):
                _, d = box.uid("SEARCH", None, f'(SINCE {since} FROM "{hint}")')
                uids |= set((d[0] or b"").split())
            found = [b" ".join(sorted(uids))]
        uids = [u.decode() for u in (found[0] or b"").split() if u.decode() not in done]
        log(f"Gmail: {len(uids)} new emails from job sites in the last {days} days")

        # 1) headers only, to skip LinkedIn's non-job mail (messages, connection requests...)
        wanted = []
        for i in range(0, len(uids), 100):
            chunk = ",".join(uids[i:i + 100])
            _, rows = box.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                uid = re.search(rb"UID (\d+)", row[0]).group(1).decode()
                h = email.message_from_bytes(row[1], policy=email.policy.default)
                sender, subject = str(h.get("From") or ""), str(h.get("Subject") or "")
                if not JOBSITE.search(sender) or re.search(r"community", sender, re.I):  # Glassdoor forum digests
                    done.add(uid)
                    continue
                if "linkedin" in sender.lower() and not re.search(r"job", sender + " " + subject, re.I):
                    done.add(uid)
                    continue
                wanted.append(uid)

        # 2) full emails for the job alerts
        ts = db.now()
        for i in range(0, len(wanted), 20):
            chunk = ",".join(wanted[i:i + 20])
            _, rows = box.uid("FETCH", chunk, "(BODY.PEEK[])")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                uid = re.search(rb"UID (\d+)", row[0]).group(1).decode()
                msg = email.message_from_bytes(row[1], policy=email.policy.default)
                res["emails"] += 1
                listings = email_alerts._from_message(msg)
                if listings:
                    res["job_emails"] += 1
                else:  # kept locally so the parser can be improved for this email type
                    res["no_jobs"].append({"from": str(msg.get("From") or "")[:80],
                                           "subject": str(msg.get("Subject") or "")[:120]})
                for L in listings:
                    r = db.upsert(conn, L, ts)
                    res[r] += 1
                    res["links"] += 1
                    res["by_source"][L["source"]] = res["by_source"].get(L["source"], 0) + 1
                done.add(uid)
            conn.commit()
            log(f"Gmail: read {min(i + 20, len(wanted))}/{len(wanted)} alert emails")
    finally:
        try:
            box.logout()
        except Exception:
            pass

    seen = {validity: sorted(done, key=int)[-5000:]}
    db.meta_set(conn, "gmail_seen", json.dumps(seen))
    db.meta_set(conn, "gmail_no_jobs", json.dumps(res["no_jobs"][-50:]))
    conn.commit()
    summary = {k: res[k] for k in ("emails", "job_emails", "links", "new", "merged", "seen", "by_source", "days")}
    summary["no_jobs"] = len(res["no_jobs"])
    info = userdata.gmail()
    totals = info.get("totals") or {}
    for k in ("job_emails", "links", "new", "merged"):
        totals[k] = totals.get(k, 0) + summary[k]
    for src, n in summary["by_source"].items():
        totals.setdefault("by_source", {})[src] = totals.get("by_source", {}).get(src, 0) + n
    userdata.save_gmail({**info, "last_check": db.now(), "last_result": summary, "totals": totals})
    return summary
