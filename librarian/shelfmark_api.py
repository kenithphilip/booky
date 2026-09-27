"""Shelfmark's pending requests in the portal's own Pending card (L16): one approval queue.

Shelfmark has request policies of its own (REQUESTS_ENABLED + REQUEST_POLICY_DEFAULT_*), which
docker-compose.yml sets from the portal's APPROVALS_REQUIRED, so a reader's Shelfmark download
waits for approval exactly as a portal request does. Approving it here calls Shelfmark's own
admin API, which queues the release the READER picked under the READER's name — it lands in
their dropbox and is owner-tagged like every other arrival (requests_service.fulfil_request).

Shelfmark has no API key: the portal signs in as a dedicated Calibre-Web admin account
(SHELFMARK_SVC_USER, created by bookstack.sh with a generated password). Its session cookie is
flagged Secure in production, and a cookie jar will not send a Secure cookie to
http://127.0.0.1 — so the cookie is carried by hand.

With the Authelia gate on (L05) Shelfmark runs in proxy mode and trusts Remote-User; the portal,
on the host like Caddy, then names the service account in that header instead of logging in."""
import threading, time
import requests
import config

TIMEOUT = (3, 10)
_lock = threading.Lock()
_state = {"cookie": None, "pending": None, "at": 0.0, "error": None}
CACHE_SECONDS = 20


class ShelfmarkError(Exception):
    pass


def _proxy():
    return config.SHELFMARK_AUTH_METHOD == "proxy"


def configured():
    return bool(config.SHELFMARK_SVC_USER and config.SHELFMARK_API and (config.SHELFMARK_SVC_PASS or _proxy()))


def _login():
    r = requests.post(f"{config.SHELFMARK_API}/api/auth/login", timeout=TIMEOUT,
                      json={"username": config.SHELFMARK_SVC_USER, "password": config.SHELFMARK_SVC_PASS})
    if r.status_code != 200:
        raise ShelfmarkError(f"Shelfmark refused the portal's service login (HTTP {r.status_code})")
    cookie = next((f"{c.name}={c.value}" for c in r.cookies), None)
    if not cookie:
        raw = r.headers.get("Set-Cookie", "")
        cookie = raw.split(";", 1)[0] if "=" in raw else None
    if not cookie:
        raise ShelfmarkError("Shelfmark set no session cookie")
    _state["cookie"] = cookie
    return cookie


def _call(method, path, **kw):
    if _proxy():
        try:
            r = requests.request(method, f"{config.SHELFMARK_API}{path}", timeout=TIMEOUT, headers={
                "Remote-User": config.SHELFMARK_SVC_USER, "Remote-Groups": "admins"}, **kw)
        except requests.RequestException as e:
            raise ShelfmarkError(f"Shelfmark did not answer ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise ShelfmarkError(f"Shelfmark refused the portal's service identity (HTTP {r.status_code})")
        return r
    with _lock:
        cookie = _state["cookie"] or _login()
    for attempt in (0, 1):
        try:
            r = requests.request(method, f"{config.SHELFMARK_API}{path}", timeout=TIMEOUT,
                                 headers={"Cookie": cookie}, **kw)
        except requests.RequestException as e:
            raise ShelfmarkError(f"Shelfmark did not answer ({type(e).__name__})") from e
        if r.status_code in (401, 403) and attempt == 0:
            with _lock:
                cookie = _login()             # the session expired or Shelfmark restarted
            continue
        return r
    raise ShelfmarkError("Shelfmark refused the portal's admin session")


def _row(x):
    b, rel = x.get("book_data") or {}, x.get("release_data") or {}
    return {"id": x.get("id"), "requester": x.get("username") or "?", "title": b.get("title") or rel.get("title") or "Unknown title",
            "author": b.get("author") or ", ".join(b.get("authors") or []) if isinstance(b.get("authors"), list) else b.get("author") or "",
            "source": rel.get("source") or x.get("source_hint") or "", "format": rel.get("format") or "",
            "kind": x.get("content_type") or "ebook", "level": "release" if rel else "book",
            "note": x.get("note") or ""}


def pending(force=False):
    """[rows] waiting in Shelfmark, cached briefly. Returns [] when not configured; raises
    ShelfmarkError when configured but unreachable (the page says so rather than showing none)."""
    if not configured():
        return []
    now = time.time()
    if not force and _state["pending"] is not None and now - _state["at"] < CACHE_SECONDS:
        return _state["pending"]
    r = _call("GET", "/api/admin/requests", params={"status": "pending", "limit": 100})
    if r.status_code != 200:
        raise ShelfmarkError(f"Shelfmark answered HTTP {r.status_code} for its request list")
    rows = [_row(x) for x in (r.json() or []) if isinstance(x, dict)]
    _state.update(pending=rows, at=now)
    return rows


def decide(request_id, approve, note=""):
    """Approve (fulfil with the reader's own release choice) or reject one Shelfmark request."""
    path = f"/api/admin/requests/{int(request_id)}/{'fulfil' if approve else 'reject'}"
    body = {"admin_note": note[:500] or None}
    r = _call("POST", path, json=body)
    _state["pending"] = None                  # the next view refetches
    if r.status_code != 200:
        try:
            err = (r.json() or {}).get("error")
        except ValueError:
            err = None
        raise ShelfmarkError(err or f"Shelfmark answered HTTP {r.status_code}")
    return r.json()
