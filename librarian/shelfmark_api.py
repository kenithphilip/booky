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
            "note": x.get("note") or "",
            "isbns": [v for v in (b.get("isbn_13"), b.get("isbn_10")) if v]}


def pending(force=False, cache=True):
    """[rows] waiting in Shelfmark, cached briefly. Returns [] when not configured; raises
    ShelfmarkError when configured but unreachable (the page says so rather than showing none).
    cache=False (the worker's family-sharing gate, every few seconds): always fetched, and never
    written into the page's cache, which would otherwise hide a brand-new request from the
    admin's Pending card for up to CACHE_SECONDS."""
    if not configured():
        return []
    now = time.time()
    if cache and not force and _state["pending"] is not None and now - _state["at"] < CACHE_SECONDS:
        return _state["pending"]
    r = _call("GET", "/api/admin/requests", params={"status": "pending", "limit": 100})
    if r.status_code != 200:
        raise ShelfmarkError(f"Shelfmark answered HTTP {r.status_code} for its request list")
    rows = [_row(x) for x in (r.json() or []) if isinstance(x, dict)]
    if cache:
        _state.update(pending=rows, at=now)
    return rows


def queue_status():
    """Shelfmark's download queue, {status: {task_id: task}} (queued, resolving, locating,
    downloading, complete, error, cancelled). The service account is an admin, so this is every
    user's queue. Finished and failed tasks stay listed for STATUS_TIMEOUT (1 h)."""
    if not configured():
        return {}
    r = _call("GET", "/api/status")
    if r.status_code != 200:
        raise ShelfmarkError(f"Shelfmark answered HTTP {r.status_code} for its queue")
    st = r.json() or {}
    return st if isinstance(st, dict) else {}


def waiting_for_files(status=None):
    """[{title, author}] of Shelfmark downloads a client reports done but whose file has not
    appeared yet ("Waiting for completed files"): what the seedbox job should bring back."""
    st = queue_status() if status is None else status
    out = []
    for task in (st.get("locating") or {}).values():
        if isinstance(task, dict) and "completed files" in (task.get("status_message") or "").lower():
            out.append({"title": task.get("title") or "", "author": task.get("author") or ""})
    return out


def failed(status=None):
    """[{task_id, title, author, user, message}] of downloads that ended in an error (a source
    that failed, a stall Shelfmark cancelled): nobody but the reader would otherwise know.
    Task fields as v1.4.0's orchestrator._task_to_dict writes them: id, title, author, username,
    status_message (which carries the error text; last_error_message is not serialized)."""
    st = queue_status() if status is None else status
    out = []
    for tid, task in (st.get("error") or {}).items():
        if isinstance(task, dict):
            out.append({"task_id": str(task.get("id") or tid), "title": task.get("title") or "Unknown title",
                        "author": task.get("author") or "", "user": task.get("username") or "",
                        "message": task.get("status_message") or ""})
    return out


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
