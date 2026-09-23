import threading, os, hmac, secrets, shutil, datetime, time, sqlite3, ipaddress
from functools import wraps
from urllib.parse import urlsplit
from uuid import uuid4
from flask import (Flask, request, session, redirect, url_for, render_template, flash,
                   Response, jsonify, send_file, abort)
from concurrent.futures import ThreadPoolExecutor
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import Markup
import config, db, auth, fetchers, worker, notify, dedupe, enrich, cwa, library, kindle
import abs as absapi

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=config.SESSION_HOURS),
                  MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024)
if config.TRUST_PROXY:
    # The portal binds 127.0.0.1 and only Caddy talks to it. Caddy REPLACES X-Forwarded-For
    # with the real client address it resolved from CF-Connecting-IP (header_up
    # X-Forwarded-For {client_ip}), so the header carries exactly one trustworthy value and
    # x_for=1 turns it into request.remote_addr for the lockout, the audit trail and /healthz.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'none'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
REVALIDATE_SECONDS = 60      # how often a logged-in session is checked against the account
ENRICH_DEADLINE = 6          # seconds the search page waits for covers/blurbs before rendering

db.init()
if os.environ.get("LIBRARIAN_NO_WORKER") != "1":       # tests drive the worker directly
    threading.Thread(target=worker.run_forever, daemon=True).start()

def _ip():
    """The client's address (see ProxyFix above); a request straight to the loopback bind
    without Caddy (healthchecks, the TUI) shows as 127.0.0.1."""
    return request.remote_addr or "?"

def _audit(event, detail=None, user=None):
    db.audit(event, user or session.get("user"), _ip(), detail)

# ---- CSRF: every state-changing form carries a per-session token -----------------
def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]

def _friendly_detail(detail, is_admin=False):
    """What a family member reads in the Detail column. The internal wording (owner tags, ABS
    scan notes, container paths) stays for admins, who act on it."""
    d = (detail or "").strip()
    if is_admin or not d:
        return d
    if d.startswith(f"tagged {config.OWNER_PREFIX}"):
        return "added to your library"
    if d.startswith("ABS scan triggered"):
        return "added to your audiobooks"
    if worker.TAG_WAIT_NOTE in d:
        return "in your audiobooks; the app is still indexing it"
    if d.startswith(worker.NEEDS_TAG):
        return "imported, but the admin has to tag it to you before you can see it"
    if "dropbox/" in d:               # .failed/ paths mean nothing to a user
        return d.split(" (moved to")[0].split(" (could not")[0]
    return d

@app.context_processor
def _inject():
    return {"csrf_field": lambda: Markup(f'<input type="hidden" name="csrf" value="{_csrf_token()}">'),
            "kindle_enabled": kindle.configured(), "cfg": config,
            "source_label": config.source_label, "friendly_detail": _friendly_detail,
            "mail_intake": _mail_intake_address}

def _mail_intake_address(user):
    """'books+alice@example.com' when e-mail intake is configured, else None: without this the
    feature was undiscoverable (no page mentioned it)."""
    box = config.IMAP_USER or ""
    if not config.IMAP_HOST or "@" not in box or not user:
        return None
    local, _, domain = box.partition("@")
    return f"{local}+{user}@{domain}"

@app.before_request
def _csrf_check():
    if request.method == "POST" and request.endpoint != "intake":     # /intake uses its own token
        tok = session.get("csrf", "")
        sent = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not tok or not sent or not hmac.compare_digest(tok, sent):
            abort(400, "Invalid or missing form token. Reload the page and try again.")

@app.before_request
def _revalidate_session():
    """A cookie minted at login stays valid for SESSION_HOURS; re-check the account every
    minute so a password reset, removal or demotion takes effect within that minute."""
    if "user" not in session or time.time() - session.get("chk", 0) <= REVALIDATE_SECONDS:
        return None
    r = auth.fingerprint(session["user"])
    if r is auth.UNAVAILABLE:        # app.db momentarily unreadable: keep the session, retry next request
        return None
    if r is None or r[0] != session.get("fp"):
        _audit("session_revoked", "account changed or removed")
        session.clear()
        return redirect(url_for("login"))
    session["admin"], session["chk"] = r[1], time.time()
    return None

@app.after_request
def _headers(resp):
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        if not session.get("admin"):
            flash("Admins only.")
            return redirect(url_for("status"))
        return f(*a, **k)
    return w

def _safe_next(nxt):
    """Only a same-site path: no scheme, no host, no '//' or backslash tricks."""
    p = urlsplit(nxt or "")
    ok = bool(nxt) and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt \
        and not p.scheme and not p.netloc
    return nxt if ok else url_for("index")

def _cwa_user(name):
    try:
        return cwa.get_user(name) or {}
    except cwa.CwaError:
        return {}

# ---- auth ------------------------------------------------------------------------
@app.errorhandler(413)
def _too_large(_e):
    """Werkzeug's bare '413 Request Entity Too Large' page told a family member nothing."""
    flash(f"That file is larger than {config.MAX_UPLOAD_MB} MB, which is the limit for browser "
          f"uploads. Put it in your dropbox folder instead (ask the admin how), or e-mail it in.")
    return redirect(url_for("upload")), 302

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user") and request.method == "GET":
        return redirect(url_for("index"))     # already signed in: the form would only confuse
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        wait = db.locked_for(username, _ip())
        if wait:
            _audit("login_locked", f"{username} ({wait}s left)", user=username)
            flash(f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).")
            _csrf_token()
            return render_template("login.html"), 429, {"Retry-After": str(max(1, wait))}
        u = auth.verify(username, request.form.get("password", ""))
        if u is auth.UNAVAILABLE:              # CWA restarting: not the user's fault, not a failure
            _audit("login_unavailable", username, user=username or None)
            flash("The library is restarting. Try again in a minute.")
            _csrf_token()
            return render_template("login.html"), 503
        if u:
            session.clear()
            session.permanent = True
            session["user"] = u["name"]; session["admin"] = u["is_admin"]
            session["fp"] = u["fp"]; session["chk"] = time.time()
            _csrf_token()
            db.clear_login_failures(u["name"], _ip())
            _audit("login_ok", user=u["name"])
            return redirect(_safe_next(request.args.get("next")))
        locked = db.record_login_failure(username, _ip())
        _audit("login_fail", username + (" -> locked" if locked else ""), user=username or None)
        flash("Invalid login." + (" Too many attempts: this account is locked for a while from your address." if locked else ""))
        _csrf_token()
        return render_template("login.html"), 401       # 401 so Caddy's log / fail2ban can count it
    _csrf_token()
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    if session.get("user"):
        _audit("logout")
    session.clear(); return redirect(url_for("login"))

@app.route("/logout", methods=["GET"])
def logout_get():
    # a link cannot log someone out (that would be CSRF); the nav button POSTs. Sending a
    # signed-in user to /login only looked as if the logout had worked.
    if session.get("user"):
        flash("Use the Log out button at the top right to sign out.")
        return redirect(url_for("index"))
    return redirect(url_for("login"))

# ---- search & requests -----------------------------------------------------------
@app.route("/", methods=["GET"])
@login_required
def index():
    q = request.args.get("q", "").strip()
    results = fetchers.search(q) if q else []
    for r in results:                      # advisory duplicate flag, only for books this user can see
        r["dupe"] = dedupe.exists(r.get("title"), session["user"], session.get("admin", False))
    if results and config.ENRICH_METADATA:
        # covers/blurbs in parallel and best-effort, but never at the cost of the page: what
        # Open Library has not answered within ENRICH_DEADLINE is simply left out
        until = time.time() + ENRICH_DEADLINE
        ex = ThreadPoolExecutor(max_workers=6)
        try:
            futures = [ex.submit(enrich.for_book, r["title"], r.get("author", "")) for r in results]
            for r, f in zip(results, futures):
                try:
                    r.update(f.result(timeout=max(0.1, until - time.time())))
                except Exception:
                    pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    return render_template("index.html", q=q, results=results,
                           sources=[s for s, on in config.SOURCES.items() if on])

@app.route("/request", methods=["POST"])
@login_required
def make_request():
    r = {k: request.form.get(k) for k in
         ("kind", "source", "identifier", "title", "author", "download_url")}
    if not r["title"] or not r["download_url"] or config.SOURCES.get(r.get("source")) is not True:
        flash("Could not queue that item.")
        return redirect(url_for("status"))
    # The form echoes the URL the adapter produced; a tampered one must not reach the worker.
    # (There is no P2P path any more: a torrent flag is a tampered form.)
    if not fetchers.url_allowed(r["source"], r["download_url"]) or request.form.get("is_torrent") == "1":
        _audit("request_refused", f"{r['source']} {r['download_url'][:120]}")
        abort(400, "That download address is not one this source hands out.")
    r["kind"] = "audio" if r["source"] == "librivox" else "ebook"
    is_admin = session.get("admin", False)
    status = "queued" if (is_admin or not config.APPROVALS_REQUIRED) else "pending"
    limit = 0 if is_admin else config.MAX_REQUESTS_PER_DAY
    # counted and inserted in one transaction: five parallel submissions cannot all pass a
    # separate "still under the limit" check
    rid, left, resets = db.add_if_under_quota(session["user"], r, limit, status=status)
    if rid is None:
        _audit("request_quota", r["title"])
        flash(f"You have reached the limit of {limit} requests per day. "
              f"You can request again after {datetime.datetime.fromtimestamp(resets).strftime('%H:%M')}. "
              f"(Denied and failed requests do not count.)")
        return redirect(url_for("status"))
    rec = db.get(rid)
    _audit("request", f"#{rid} {r['title']} [{r['source']}] -> {status}")
    note = f" {left} of {limit} requests left today." if limit else ""
    if status == "pending":
        notify.send("requested", rec)
        flash(f"Requested: {r['title']} — waiting for admin approval.{note}")
    else:
        flash(f"Requested: {r['title']} — it will appear in your library shortly.{note}")
    return redirect(url_for("status"))

@app.route("/approve/<int:rid>", methods=["POST"])
@admin_required
def approve(rid):
    rec = db.get(rid)
    # conditional on the row still being pending: two admins clicking at once (or Approve
    # racing Deny) otherwise both notified the requester
    if rec and db.set_status_if(rid, "pending", "queued", "approved by admin"):
        notify.send("approved", db.get(rid))
        _audit("approve", f"#{rid} {rec['title']} for {rec['owner']}")
        flash(f"Approved: {rec['title']}")
    elif rec and rec["status"] != "pending":
        flash("Another admin already handled that request.")
    return redirect(url_for("status"))

@app.route("/deny/<int:rid>", methods=["POST"])
@admin_required
def deny(rid):
    rec = db.get(rid)
    # "denied by admin" alone left the requester guessing; a one-line reason reaches them
    # in the status list and in the notification
    reason = " ".join((request.form.get("reason") or "").split())[:200]
    detail = f"denied by admin: {reason}" if reason else "denied by admin"
    if rec and db.set_status_if(rid, "pending", "denied", detail):
        notify.send("denied", db.get(rid))
        _audit("deny", f"#{rid} {rec['title']} for {rec['owner']}" + (f" ({reason})" if reason else ""))
        flash(f"Denied: {rec['title']}")
    elif rec and rec["status"] != "pending":
        flash("Another admin already handled that request.")
    return redirect(url_for("status"))

COVER_MAX = 2 * 1024 * 1024
COVER_REDIRECTS = 2

def _cover_host_ok(url):
    """Open Library serves many covers by redirecting to archive.org; nothing else is fetched."""
    p = urlsplit(url)
    h = (p.hostname or "").lower()
    return p.scheme == "https" and p.username is None and (
        h == "covers.openlibrary.org" or h == "archive.org" or h.endswith(".archive.org"))

@app.route("/cover")
@login_required
def cover():
    u = request.args.get("u", "")
    if not u.startswith("https://covers.openlibrary.org/"):
        return "", 404
    try:
        import requests as _r
        from urllib.parse import urljoin
        for _hop in range(COVER_REDIRECTS + 1):
            if not _cover_host_ok(u):
                return "", 404
            with _r.get(u, timeout=8, stream=True, allow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    u = urljoin(u, resp.headers["Location"])
                    continue
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code != 200 or not ctype.startswith("image/"):
                    return "", 404
                body = resp.raw.read(COVER_MAX + 1, decode_content=True)
            if len(body) > COVER_MAX:
                return "", 404
            return Response(body, mimetype=ctype.split(";")[0], headers={"Cache-Control": "public, max-age=604800"})
        return "", 404
    except Exception:
        return "", 404

def _retryable(rec):
    return bool(rec and rec["status"] == "error" and rec.get("download_url")
                and rec["download_url"] != "local")

@app.route("/retry/<int:rid>", methods=["POST"])
@admin_required
def retry(rid):
    rec = db.get(rid)
    if _retryable(rec):
        db.requeue(rid, "requeued by admin")
        _audit("retry", f"#{rid} {rec['title']} for {rec['owner']}")
        flash(f"Requeued: {rec['title']}")
    else:
        flash("That request can't be retried automatically (no re-fetchable source).")
    return redirect(url_for("status"))

@app.route("/retry_all", methods=["POST"])
@admin_required
def retry_all():
    n = 0
    for r in db.rows_by_status(("error",)):      # every failed row, not just the newest 200
        if _retryable(r):
            db.requeue(r["id"], "requeued by admin (bulk)")
            n += 1
    _audit("retry_all", f"{n} request(s)")
    flash(f"Requeued {n} failed request(s).")
    return redirect(url_for("status"))

# 'pending' is admin-only (the check below keeps users to their own 'error' rows): a request
# whose owner was removed cannot be approved or honestly denied, so it needs a way out.
DISMISSABLE = ("error", worker.NEEDS_TAG, "pending")

@app.route("/dismiss/<int:rid>", methods=["POST"])
@login_required
def dismiss(rid):
    """Admins clear any dead row — including a needs-tag one they have just tagged by hand,
    which the dashboard told them to do but no button allowed. A user may clear their OWN
    failed rows instead of staring at them."""
    rec = db.get(rid)
    is_admin = session.get("admin", False)
    if not rec or (not is_admin and rec["owner"] != session["user"]):
        abort(404)
    if rec["status"] not in DISMISSABLE or (not is_admin and rec["status"] != "error"):
        flash("That request cannot be dismissed.")
        return redirect(url_for("status"))
    who = "admin" if is_admin else "owner"
    db.set_status(rid, "dismissed", f"dismissed by {who}"
                  + (" (owner tag added by hand)" if rec["status"] == worker.NEEDS_TAG else ""))
    _audit("dismiss", f"#{rid} {rec['title']} ({rec['status']})")
    flash(f"Dismissed: {rec['title']}")
    return redirect(url_for("admin" if is_admin and rec["status"] == worker.NEEDS_TAG else "status"))

@app.route("/status")
@login_required
def status():
    rows = db.list_for(session["user"], session.get("admin", False))
    is_admin = session.get("admin", False)
    # The actionable lists come from dedicated queries, NOT from the capped scrollback: once
    # 200 newer rows existed, a waiting approval and every failed download became invisible
    # here while /admin kept counting them (and 'Retry all', which is itself uncapped,
    # disappeared with the section that holds it).
    pending = db.rows_by_status(("pending",)) if is_admin else []
    for r in pending:                       # the approver must see WHERE the file comes from
        r["host"] = urlsplit(r.get("download_url") or "").hostname or "?"
    failed = db.rows_by_status(("error",)) if is_admin else []
    for r in failed:
        r["retryable"] = _retryable(r)
    for r in rows:                          # a user may clear their own failed rows (J40)
        r["can_dismiss"] = r["status"] == "error"
    return render_template("status.html", rows=rows, pending=pending, failed=failed,
                           admin=is_admin)

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a file to upload.")
            return redirect(url_for("upload"))
        stem, _, ext = f.filename.replace("\\", "/").rsplit("/", 1)[-1].rpartition(".")
        ext = ext.lower()
        if ext not in config.EBOOK_EXTS + config.AUDIO_EXTS:
            flash("That file type is not supported.")
            return redirect(url_for("upload"))
        if ext == "cbr":
            flash("CBR (RAR) comics cannot be tagged to you: convert it to CBZ and upload again.")
            return redirect(url_for("upload"))
        # keep the real (Unicode) name; only path separators / control characters go
        owner = session["user"]
        if not cwa._valid_name(owner):
            # the dropbox watcher only scans folders named like a valid account; accepting the
            # file would report success for something that is never imported
            _audit("upload_refused", f"account name {owner!r} has spaces or symbols")
            flash("Your account name contains spaces or symbols, so uploads cannot be matched to you. "
                  "Ask the admin to rename the account (letters, digits, dot, dash, underscore).")
            return redirect(url_for("upload"))
        # a 0-byte or obviously wrong file would be accepted with "it will be added shortly"
        # and only fail minutes later in the worker: say so now, while the user is here
        problem = _upload_problem(f, ext)
        if problem:
            _audit("upload_rejected", f"{f.filename[:80]}: {problem}")
            flash(problem)
            return redirect(url_for("upload"))
        stem = worker._safe(stem) if stem.strip(" .") else f"upload-{uuid4().hex[:8]}"
        d = os.path.join(config.DROPBOX_DIR, owner)
        # unpredictable temp name, never follows a planted symlink, never replaces a file of the
        # same name still waiting for pickup; the watcher then tags + ingests it
        try:
            fn = os.path.basename(worker.place_in_dropbox(d, f"{stem}.{ext}", f.save))
        except OSError as e:
            _audit("upload_failed", f"{f.filename[:80]}: {e.__class__.__name__}")
            flash("That file could not be saved (its name may be too long). Rename it and try again.")
            return redirect(url_for("upload"))
        _audit("upload", fn)
        flash(f"Uploaded {fn} — it will be added to your library shortly.")
        return redirect(url_for("status"))
    return render_template("upload.html", u_email=_cwa_user(session["user"]).get("email"))

MAGIC = {"epub": b"PK\x03\x04", "cbz": b"PK\x03\x04", "zip": b"PK\x03\x04", "pdf": b"%PDF"}

def _upload_problem(f, ext):
    """None when the file looks usable, else a plain sentence for the user."""
    head = f.stream.read(8)
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if not size:
        return "That file is empty (0 bytes). Check the file and upload it again."
    magic = MAGIC.get(ext)
    if magic and not head.startswith(magic):
        return (f"That file does not look like a {ext.upper()} inside (it may be renamed, "
                f"incomplete or corrupted). Check it and upload it again.")
    return None

# ---- my library: download / send-to-kindle -----------------------------------------
@app.route("/library")
@login_required
def my_library():
    user, is_admin = session["user"], session.get("admin", False)
    q = request.args.get("q", "").strip()[:100]
    page = max(0, request.args.get("page", 0, type=int) or 0)
    prefs = db.get_prefs(user)
    books = library.books_for(user, is_admin, offset=page * library.PAGE, q=q)
    total = library.count_for(user, is_admin, q=q)
    for b in books:
        b["best"] = library.best_format(b, prefs["preferred_format"])
        b["kindle_ok"] = any(f in config.KINDLE_FORMATS for f in b["formats"])
    u = _cwa_user(user)
    return render_template("library.html", books=books, prefs=prefs, admin=is_admin,
                           kindle_mail=u.get("kindle_mail") or "", q=q, page=page, total=total,
                           first=page * library.PAGE + 1, last=page * library.PAGE + len(books),
                           more=(page + 1) * library.PAGE < total)

@app.route("/download/<int:book_id>/<fmt>")
@login_required
def download(book_id, fmt):
    f = library.file_for(session["user"], book_id, fmt, session.get("admin", False))
    if not f:
        _audit("download_denied", f"book {book_id} {fmt}")
        abort(404)
    _audit("download", f["filename"])
    return send_file(f["path"], as_attachment=True, download_name=f["filename"], max_age=0,
                     mimetype=f.get("mimetype"))

@app.route("/kindle/<int:book_id>", methods=["POST"])
@login_required
def send_kindle(book_id):
    user, is_admin = session["user"], session.get("admin", False)
    if not library.visible(user, book_id, is_admin):
        abort(404)                          # not this user's book (or no such book)
    f = next((x for x in (library.file_for(user, book_id, fmt, is_admin) for fmt in config.KINDLE_FORMATS) if x), None)
    if not f:
        flash("No Kindle-compatible format yet (Amazon takes EPUB or PDF by mail); "
              "the library converts new books to EPUB on import when conversion is on.")
        return redirect(url_for("my_library"))
    addr = _cwa_user(user).get("kindle_mail") or ""
    if not addr:
        flash("Set your Kindle address on the Devices page first.")
        return redirect(url_for("devices"))
    try:
        flash(kindle.send(addr, f["path"], f["title"], f["filename"]))
        _audit("kindle_send", f["filename"])
    except Exception as e:
        flash(f"Could not send: {e}")
    return redirect(url_for("my_library"))

# ---- devices: Kindle address, Kobo link, preferences ---------------------------------
@app.route("/devices", methods=["GET", "POST"])
@login_required
def devices():
    user = session["user"]
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "kindle":
                addr = cwa.set_kindle_mail(user, request.form.get("kindle_mail", ""))
                _audit("kindle_set", addr or "(cleared)")
                flash("Kindle address saved." if addr else "Kindle address cleared.")
            elif action == "kindle_test":
                addr = _cwa_user(user).get("kindle_mail") or ""
                if not addr:
                    flash("Save your Kindle address first.")
                else:
                    try:
                        note = kindle.send_test(addr)
                        db.set_prefs(user, last_kindle_test=time.time())
                        _audit("kindle_test", addr)
                        flash(f"Test mail {note}. If nothing arrives within a few minutes, check the approved-sender list at Amazon.")
                    except Exception as e:
                        flash(f"Test mail could not be sent: {e}")
            elif action == "email":
                addr = cwa.set_email(user, request.form.get("email", ""))
                _audit("email_set", addr)
                flash("E-mail address saved.")
            elif action == "prefs":
                want_auto = request.form.get("auto_kindle") == "1"
                # switching auto-Kindle on with no address saved silently did nothing until a
                # book arrived and the request said "auto-Kindle skipped"
                has_addr = bool(_cwa_user(user).get("kindle_mail"))
                db.set_prefs(user, preferred_format=request.form.get("preferred_format"),
                             auto_kindle=want_auto and has_addr,
                             notify_email=request.form.get("notify_email") == "1")
                if want_auto and not has_addr:
                    flash("Preferences saved, but automatic Send-to-Kindle stays off until you "
                          "save your Kindle address above.")
                else:
                    flash("Preferences saved.")
            elif action == "kobo":
                cwa.kobo_url(user, create=True); _audit("kobo_link"); flash("Your Kobo sync link is ready.")
            elif action == "kobo_reset":
                cwa.reset_kobo_token(user); _audit("kobo_reset"); flash("Kobo link regenerated — update the device.")
            elif action == "password":
                cur, new, rep = request.form.get("current", ""), request.form.get("new", ""), request.form.get("repeat", "")
                ok = auth.verify(user, cur)
                if ok is auth.UNAVAILABLE:
                    flash("The library is restarting. Try again in a minute.")
                elif not ok:
                    _audit("password_change_failed"); flash("Current password is wrong.")
                elif new != rep:
                    flash("The new passwords do not match.")
                else:
                    cwa.set_password(user, new)
                    note = ""
                    if absapi.configured():
                        try:
                            absapi.set_password(user, new); note = " Audiobookshelf too."
                        except Exception:
                            note = " (Audiobookshelf could not be updated — tell the admin.)"
                    fp = auth.fingerprint(user)          # keep THIS session; older cookies expire
                    if fp and fp is not auth.UNAVAILABLE:
                        session["fp"], session["chk"] = fp[0], time.time()
                        db.set_pw_fingerprint(user, fp[0])   # this change IS synced: do not warn about it
                    if config.AUTHELIA_ENABLED:
                        note += " The sign-in page in front of the sites (Authelia) keeps its own password: ask the admin to change it too."
                    _audit("password_change")
                    # NOT Shelfmark: it signs its own session cookie and only checks app.db at
                    # login, so an old cookie keeps working until the container is restarted.
                    # Saying otherwise told someone whose password had leaked that they were
                    # covered when they were not (the restart is an admin action).
                    note += (" Shelfmark keeps you signed in on devices that were already signed in: "
                             "ask the admin to restart Shelfmark if this was because of a leaked password.")
                    flash("Password changed for the portal and the library." + note)
        except cwa.CwaError as e:
            flash(str(e))
        return redirect(url_for("devices"))
    u = _cwa_user(user)
    kobo = None
    try:
        kobo = cwa.kobo_url(user, create=False)
    except cwa.CwaError:
        pass
    try:
        kobo_on = cwa.kobo_sync_enabled()
    except Exception:
        kobo_on = False
    prefs = db.get_prefs(user)
    if prefs["last_kindle_test"]:
        prefs["last_kindle_test"] = datetime.datetime.fromtimestamp(prefs["last_kindle_test"]).strftime("%Y-%m-%d %H:%M")
    return render_template("devices.html", u=u, prefs=prefs, kobo=kobo,
                           kobo_on=kobo_on, formats=config.FORMATS, kosync=config.KOSYNC_ENABLED,
                           abs_linked=absapi.configured())

# ---- admin dashboard ---------------------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    if request.method == "POST" and request.form.get("action") == "add_user":
        if config.AUTHELIA_ENABLED:
            flash("Authelia is on: add users in the admin tools (bookstack.sh -> Users) so they also get an Authelia login.")
            return redirect(url_for("admin"))
        try:
            pw = request.form.get("password", "")
            email = request.form.get("email", "").strip()
            if "@" not in email or " " in email:
                raise cwa.CwaError("a real e-mail address is needed (notifications and mail-to-library check it)")
            u = cwa.add_user(request.form.get("name", "").strip(), pw, email, request.form.get("admin") == "1")
            os.makedirs(os.path.join(config.DROPBOX_DIR, u["name"]), exist_ok=True)
            note = ""
            if absapi.configured() and not (u["role"] & 1):
                try:
                    absapi.ensure_user(u["name"], pw); note = " Audiobookshelf account created."
                except Exception:
                    note = " (Audiobookshelf account could not be created — Users → Repair in the admin tools.)"
            _audit("user_add", u["name"] + (" (admin)" if u["role"] & 1 else ""))
            flash(f"User {u['name']} created" + ("" if u["role"] & 1 else f" (isolated to {cwa.owner_tag(u['name'])})") + "." + note)
        except cwa.CwaError as e:
            flash(f"Could not create user: {e}")
        return redirect(url_for("admin"))
    # dedicated queries: the dashboard used to count only the newest 200 rows, so a busy week
    # hid both failures and needs-tag rows from the admin
    all_counts = db.counts_by_status()
    counts = {s: all_counts.get(s, 0)
              for s in ("pending", "queued", "downloading", "retrying", "importing", "tagging",
                        "done", "needs-tag", "error")}
    counts["needs_tag"] = counts.pop("needs-tag")
    needs_tag = db.rows_by_status((worker.NEEDS_TAG,))
    try:
        users = cwa.list_users()
    except cwa.CwaError:
        users = []
    for usr in users:
        usr["bad_name"] = not cwa._valid_name(usr["name"])
    try:
        du = shutil.disk_usage(config.INGEST_DIR)
        disk = {"free_gb": round(du.free / 2**30, 1), "pct": round(100 * du.used / du.total)}
    except Exception:
        disk = None
    try:
        kobo_on = cwa.kobo_sync_enabled()
    except Exception:
        kobo_on = False
    audit_rows = db.audit_recent(100)
    for a in audit_rows:
        a["when"] = datetime.datetime.fromtimestamp(a["ts"]).strftime("%Y-%m-%d %H:%M")
    return render_template("admin.html", links=config.admin_links(), counts=counts,
                           users=users, disk=disk, kobo_on=kobo_on, audit=audit_rows, needs_tag=needs_tag,
                           abs_linked=absapi.configured(), quota=config.MAX_REQUESTS_PER_DAY,
                           sources=[s for s, on in config.SOURCES.items() if on], health=_health(),
                           alerts_ok=bool(config.NOTIFY_WEBHOOK or (config.ADMIN_EMAIL and kindle.configured())),
                           authelia=config.AUTHELIA_ENABLED)

# ---- automation & health ------------------------------------------------------------
@app.route("/intake", methods=["POST"])
def intake():
    """Automation hook for legal sources: an authorized script (an itch.io/Gumroad
    purchase downloader, a Gutenberg-mirror job, an rclone post-hook) posts a concrete
    download URL to acquire on behalf of a user. Requires the shared intake token."""
    if not config.INTAKE_TOKEN:              # off until the admin enables it (Library -> Intake)
        abort(404)
    auth_hdr = request.headers.get("Authorization", "")
    bearer = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else ""
    tok = request.headers.get("X-Intake-Token") or bearer or request.form.get("token") or ""
    if not hmac.compare_digest(tok, config.INTAKE_TOKEN):
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or request.form
    user = (body.get("user") or "").strip()
    url = (body.get("url") or "").strip()
    kind = body.get("kind") or "ebook"
    if not user or not url or not url.lower().startswith(("http://", "https://")):
        return jsonify(error="user and http(s) url required"), 400
    if kind not in ("ebook", "audio"):
        return jsonify(error="kind must be ebook or audio"), 400
    u = _cwa_user(user) if cwa._valid_name(user) else None
    if not u:
        db.audit("intake_refused", user, _ip(), "no such user")
        return jsonify(error="no such user"), 400
    # a hook that fires twice (a retry, a re-run of the same job) must not queue the book twice
    existing = db.find_open_by_url(u["name"], url)
    if existing:
        db.audit("intake_duplicate", u["name"], _ip(), f"#{existing['id']} {existing['title']}")
        return jsonify(ok=True, id=existing["id"], duplicate=True, status=existing["status"]), 200
    r = {"kind": kind, "source": body.get("source", "intake"),
         "identifier": body.get("identifier"), "title": body.get("title") or url.rsplit("/", 1)[-1],
         "author": body.get("author", ""), "download_url": url}
    rid = db.add(u["name"], r, status="queued")     # automation bypasses approval
    db.audit("intake", u["name"], _ip(), f"#{rid} {r['title']}")
    notify.send("requested", db.get(rid))
    return jsonify(ok=True, id=rid), 202

STALE_SECONDS, MIN_FREE_BYTES = 300, 1024 ** 3   # a loop may legitimately be busy for minutes (big audiobook)
INGEST_STUCK_SECONDS = 900                        # CWA picks a file up within seconds; 15 min = its ingest is dead
CWA_PROBE_SECONDS = 60                            # how long a Calibre-Web probe result is reused
_CWA_PROBE = {"at": 0.0, "problem": None}

def _cwa_probe(now=None):
    """Is Calibre-Web itself answering? /healthz only opened app.db, which is a FILE and stays
    readable while the container is down — so the portal reported 'ok' with the library dead.
    Cached, because /admin and every healthcheck would otherwise probe on each hit."""
    now = now or time.time()
    if not config.CWA_URL:
        return None
    if now - _CWA_PROBE["at"] < CWA_PROBE_SECONDS:
        return _CWA_PROBE["problem"]
    problem = None
    try:
        import requests as _r
        r = _r.get(config.CWA_URL.rstrip("/") + "/login", timeout=4, allow_redirects=False)
        if r.status_code >= 500:
            problem = f"calibre-web answered {r.status_code}"
    except Exception:
        problem = "calibre-web is not answering (the library site is down)"
    _CWA_PROBE.update(at=now, problem=problem)
    return problem

def _health():
    """What /healthz and the admin page report: are the worker loops alive, can we write
    /ingest, is there disk, can CWA's app.db be opened. Never raises."""
    now = time.time()
    loops = ["queue", "dropbox", "housekeeping"] + (["imap"] if config.IMAP_HOST else [])
    ages = {n: (round(now - worker.HEARTBEAT[n]) if n in worker.HEARTBEAT else None) for n in loops}
    problems = [f"{n} loop stale" for n, a in ages.items() if a is None or a > STALE_SECONDS]
    pending, oldest = 0, 0
    try:
        for n in os.listdir(config.INGEST_DIR):
            p = os.path.join(config.INGEST_DIR, n)
            if os.path.isfile(p) and not n.endswith((".part", ".tmp")):
                pending += 1
                oldest = max(oldest, now - os.path.getmtime(p))
        if oldest > INGEST_STUCK_SECONDS:
            problems.append(f"ingest stalled: oldest file waiting {int(oldest // 60)} min (is CWA's ingest running?)")
        if not os.access(config.INGEST_DIR, os.W_OK):
            problems.append("ingest dir not writable")
        free = shutil.disk_usage(config.INGEST_DIR).free
        if free < MIN_FREE_BYTES:
            problems.append("less than 1 GiB free")
    except OSError as e:
        free = 0
        problems.append(f"ingest dir: {e.__class__.__name__}")
    try:
        c = sqlite3.connect(f"file:{config.CWA_DB}?mode=ro", uri=True, timeout=5)
        c.execute("SELECT 1 FROM user LIMIT 1").fetchall(); c.close()
    except Exception:
        problems.append("cwa app.db unreadable")
    cwa_problem = _cwa_probe(now)
    return {"ok": not problems, "problems": problems, "heartbeats": ages, "ingest_pending": pending,
            "ingest_oldest_s": round(oldest), "free_gb": round(free / 2**30, 1),
            "version": config.BUILD_VERSION,
            # the library being down is shown to the admin but does not make the PORTAL
            # unhealthy: compose would restart the portal for Calibre-Web's problem
            "cwa": cwa_problem or "ok"}

def _is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False

@app.route("/healthz")
def healthz():
    """200 'ok' / 503 'degraded' for the compose healthcheck, Kuma and selftest. The JSON
    details are for the box itself (loopback) or an admin asking with ?detail=1; the public
    edge only ever learns up/down."""
    h = _health()
    code = 200 if h["ok"] else 503
    if _is_loopback(request.remote_addr or "") or (request.args.get("detail") == "1" and session.get("admin")):
        return jsonify(h), code
    return ("ok" if h["ok"] else "degraded"), code, {"Content-Type": "text/plain; charset=utf-8"}
