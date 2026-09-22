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
if config.TRUST_PROXY:                                  # Caddy (127.0.0.1) sets X-Forwarded-For
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'none'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
REVALIDATE_SECONDS = 60      # how often a logged-in session is checked against the account

db.init()
if os.environ.get("LIBRARIAN_NO_WORKER") != "1":       # tests drive the worker directly
    threading.Thread(target=worker.run_forever, daemon=True).start()

def _ip():
    return request.remote_addr or "?"

def _audit(event, detail=None, user=None):
    db.audit(event, user or session.get("user"), _ip(), detail)

# ---- CSRF: every state-changing form carries a per-session token -----------------
def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]

@app.context_processor
def _inject():
    return {"csrf_field": lambda: Markup(f'<input type="hidden" name="csrf" value="{_csrf_token()}">'),
            "kindle_enabled": kindle.configured(), "cfg": config}

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
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        wait = db.locked_for(username, _ip())
        if wait:
            _audit("login_locked", f"{username} ({wait}s left)", user=username)
            flash(f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).")
            _csrf_token()
            return render_template("login.html"), 429
        u = auth.verify(username, request.form.get("password", ""))
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
    return redirect(url_for("login"))       # a link cannot log someone out; the nav button POSTs

# ---- search & requests -----------------------------------------------------------
@app.route("/", methods=["GET"])
@login_required
def index():
    q = request.args.get("q", "").strip()
    results = fetchers.search(q) if q else []
    for r in results:                      # advisory duplicate flag
        r["dupe"] = dedupe.exists(r.get("title"))
    if results and config.ENRICH_METADATA:  # covers/blurbs in parallel, best-effort
        with ThreadPoolExecutor(max_workers=6) as ex:
            metas = list(ex.map(lambda r: enrich.for_book(r["title"], r.get("author", "")), results))
        for r, m in zip(results, metas):
            r.update(m)
    return render_template("index.html", q=q, results=results,
                           sources=[s for s, on in config.SOURCES.items() if on])

@app.route("/request", methods=["POST"])
@login_required
def make_request():
    r = {k: request.form.get(k) for k in
         ("kind", "source", "identifier", "title", "author", "download_url")}
    r["is_torrent"] = request.form.get("is_torrent") == "1"
    if not r["title"] or not r["download_url"] or config.SOURCES.get(r.get("source")) is not True:
        flash("Could not queue that item.")
        return redirect(url_for("status"))
    # The form echoes the URL the adapter produced; a tampered one must not reach the worker.
    if not fetchers.url_allowed(r["source"], r["download_url"]) or \
            (r["is_torrent"] and not (r["source"] == "internet_archive" and config.IA_USE_TORRENT)):
        _audit("request_refused", f"{r['source']} {r['download_url'][:120]}")
        abort(400, "That download address is not one this source hands out.")
    r["kind"] = "audio" if r["source"] == "librivox" else "ebook"
    is_admin = session.get("admin", False)
    if not is_admin and config.MAX_REQUESTS_PER_DAY and db.requests_today(session["user"]) >= config.MAX_REQUESTS_PER_DAY:
        _audit("request_quota", r["title"])
        flash(f"You have reached the limit of {config.MAX_REQUESTS_PER_DAY} requests per day. Try again tomorrow.")
        return redirect(url_for("status"))
    status = "queued" if (is_admin or not config.APPROVALS_REQUIRED) else "pending"
    rid = db.add(session["user"], r, status=status)
    rec = db.get(rid)
    _audit("request", f"#{rid} {r['title']} [{r['source']}] -> {status}")
    if status == "pending":
        notify.send("requested", rec)
        flash(f"Requested: {r['title']} — waiting for admin approval.")
    else:
        flash(f"Requested: {r['title']} — it will appear in your library shortly.")
    return redirect(url_for("status"))

@app.route("/approve/<int:rid>", methods=["POST"])
@admin_required
def approve(rid):
    rec = db.get(rid)
    if rec and rec["status"] == "pending":
        db.set_status(rid, "queued", "approved by admin")
        notify.send("approved", db.get(rid))
        _audit("approve", f"#{rid} {rec['title']} for {rec['owner']}")
        flash(f"Approved: {rec['title']}")
    return redirect(url_for("status"))

@app.route("/deny/<int:rid>", methods=["POST"])
@admin_required
def deny(rid):
    rec = db.get(rid)
    if rec and rec["status"] == "pending":
        db.set_status(rid, "denied", "denied by admin")
        notify.send("denied", db.get(rid))
        _audit("deny", f"#{rid} {rec['title']} for {rec['owner']}")
        flash(f"Denied: {rec['title']}")
    return redirect(url_for("status"))

COVER_MAX = 2 * 1024 * 1024

@app.route("/cover")
@login_required
def cover():
    u = request.args.get("u", "")
    if not u.startswith("https://covers.openlibrary.org/"):
        return "", 404
    try:
        import requests as _r
        with _r.get(u, timeout=8, stream=True, allow_redirects=False) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code != 200 or not ctype.startswith("image/"):
                return "", 404
            body = resp.raw.read(COVER_MAX + 1, decode_content=True)
        if len(body) > COVER_MAX:
            return "", 404
        return Response(body, mimetype=ctype.split(";")[0], headers={"Cache-Control": "public, max-age=604800"})
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
        db.set_status(rid, "queued", "requeued by admin")
        flash(f"Requeued: {rec['title']}")
    else:
        flash("That request can't be retried automatically (no re-fetchable source).")
    return redirect(url_for("status"))

@app.route("/retry_all", methods=["POST"])
@admin_required
def retry_all():
    n = 0
    for r in db.list_for(session["user"], True):
        if _retryable(r):
            db.set_status(r["id"], "queued", "requeued by admin (bulk)")
            n += 1
    flash(f"Requeued {n} failed request(s).")
    return redirect(url_for("status"))

@app.route("/dismiss/<int:rid>", methods=["POST"])
@admin_required
def dismiss(rid):
    rec = db.get(rid)
    if rec and rec["status"] == "error":
        db.set_status(rid, "dismissed", "dismissed by admin")
    return redirect(url_for("status"))

@app.route("/status")
@login_required
def status():
    rows = db.list_for(session["user"], session.get("admin", False))
    is_admin = session.get("admin", False)
    pending = [r for r in rows if r["status"] == "pending"] if is_admin else []
    for r in pending:                       # the approver must see WHERE the file comes from
        r["host"] = urlsplit(r.get("download_url") or "").hostname or "?"
    failed = [r for r in rows if r["status"] == "error"] if is_admin else []
    for r in failed:
        r["retryable"] = _retryable(r)
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
        stem = worker._safe(stem) if stem.strip(" .") else f"upload-{uuid4().hex[:8]}"
        fn = f"{stem}.{ext}"
        owner = session["user"]
        d = os.path.join(config.DROPBOX_DIR, owner)
        # unpredictable temp name, never follows a planted symlink; the watcher then tags + ingests it
        worker.place_in_dropbox(d, fn, f.save)
        _audit("upload", fn)
        flash(f"Uploaded {fn} — it will be added to your library shortly.")
        return redirect(url_for("status"))
    return render_template("upload.html")

# ---- my library: download / send-to-kindle -----------------------------------------
@app.route("/library")
@login_required
def my_library():
    user, is_admin = session["user"], session.get("admin", False)
    prefs = db.get_prefs(user)
    books = library.books_for(user, is_admin)
    for b in books:
        b["best"] = library.best_format(b, prefs["preferred_format"])
        b["kindle_ok"] = any(f in config.KINDLE_FORMATS for f in b["formats"])
    u = _cwa_user(user)
    return render_template("library.html", books=books, prefs=prefs, admin=is_admin,
                           kindle_mail=u.get("kindle_mail") or "")

@app.route("/download/<int:book_id>/<fmt>")
@login_required
def download(book_id, fmt):
    f = library.file_for(session["user"], book_id, fmt, session.get("admin", False))
    if not f:
        _audit("download_denied", f"book {book_id} {fmt}")
        abort(404)
    _audit("download", f["filename"])
    return send_file(f["path"], as_attachment=True, download_name=f["filename"], max_age=0)

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
            elif action == "prefs":
                db.set_prefs(user, preferred_format=request.form.get("preferred_format"),
                             auto_kindle=request.form.get("auto_kindle") == "1",
                             notify_email=request.form.get("notify_email") == "1")
                flash("Preferences saved.")
            elif action == "kobo":
                cwa.kobo_url(user, create=True); _audit("kobo_link"); flash("Your Kobo sync link is ready.")
            elif action == "kobo_reset":
                cwa.reset_kobo_token(user); _audit("kobo_reset"); flash("Kobo link regenerated — update the device.")
            elif action == "password":
                cur, new, rep = request.form.get("current", ""), request.form.get("new", ""), request.form.get("repeat", "")
                if not auth.verify(user, cur):
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
                    if fp:
                        session["fp"], session["chk"] = fp[0], time.time()
                    _audit("password_change")
                    flash("Password changed for the portal, the library and Shelfmark." + note)
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
        try:
            pw = request.form.get("password", "")
            u = cwa.add_user(request.form.get("name", "").strip(), pw,
                             request.form.get("email", "").strip(), request.form.get("admin") == "1")
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
    rows = db.list_for(session["user"], True)
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("pending", "queued", "downloading", "importing", "done", "needs-tag", "error")}
    counts["needs_tag"] = counts.pop("needs-tag")
    needs_tag = [r for r in rows if r["status"] == worker.NEEDS_TAG]
    try:
        users = cwa.list_users()
    except cwa.CwaError:
        users = []
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
    return render_template("admin.html", links=[l for l in config.ADMIN_LINKS if l[1]], counts=counts,
                           users=users, disk=disk, kobo_on=kobo_on, audit=audit_rows, needs_tag=needs_tag,
                           abs_linked=absapi.configured(), quota=config.MAX_REQUESTS_PER_DAY,
                           sources=[s for s, on in config.SOURCES.items() if on], health=_health())

# ---- automation & health ------------------------------------------------------------
@app.route("/intake", methods=["POST"])
def intake():
    """Automation hook for legal sources: an authorized script (an itch.io/Gumroad
    purchase downloader, a Gutenberg-mirror job, an rclone post-hook) posts a concrete
    download URL to acquire on behalf of a user. Requires the shared intake token."""
    if not config.INTAKE_TOKEN:
        return jsonify(error="intake disabled"), 403
    tok = request.headers.get("X-Intake-Token") or request.form.get("token") or ""
    if not hmac.compare_digest(tok, config.INTAKE_TOKEN):
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or request.form
    user = (body.get("user") or "").strip()
    url = (body.get("url") or "").strip()
    kind = body.get("kind") or "ebook"
    if not user or not url or not url.lower().startswith(("http://", "https://", "magnet:")):
        return jsonify(error="user and http(s)/magnet url required"), 400
    if kind not in ("ebook", "audio"):
        return jsonify(error="kind must be ebook or audio"), 400
    u = _cwa_user(user) if cwa._valid_name(user) else None
    if not u:
        db.audit("intake_refused", user, _ip(), "no such user")
        return jsonify(error="no such user"), 400
    r = {"kind": kind, "source": body.get("source", "intake"),
         "identifier": body.get("identifier"), "title": body.get("title") or url.rsplit("/", 1)[-1],
         "author": body.get("author", ""), "download_url": url,
         "is_torrent": str(body.get("is_torrent", "")).lower() in ("1", "true", "yes")}
    rid = db.add(u["name"], r, status="queued")     # automation bypasses approval
    db.audit("intake", u["name"], _ip(), f"#{rid} {r['title']}")
    notify.send("requested", db.get(rid))
    return jsonify(ok=True, id=rid), 202

STALE_SECONDS, MIN_FREE_BYTES = 300, 1024 ** 3   # a loop may legitimately be busy for minutes (big audiobook)

def _health():
    """What /healthz and the admin page report: are the worker loops alive, can we write
    /ingest, is there disk, can CWA's app.db be opened. Never raises."""
    now = time.time()
    loops = ["queue", "dropbox", "torrent"] + (["imap"] if config.IMAP_HOST else [])
    ages = {n: (round(now - worker.HEARTBEAT[n]) if n in worker.HEARTBEAT else None) for n in loops}
    problems = [f"{n} loop stale" for n, a in ages.items() if a is None or a > STALE_SECONDS]
    pending, oldest = 0, 0
    try:
        for n in os.listdir(config.INGEST_DIR):
            p = os.path.join(config.INGEST_DIR, n)
            if os.path.isfile(p) and not n.endswith((".part", ".tmp")):
                pending += 1
                oldest = max(oldest, now - os.path.getmtime(p))
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
    return {"ok": not problems, "problems": problems, "heartbeats": ages, "ingest_pending": pending,
            "ingest_oldest_s": round(oldest), "free_gb": round(free / 2**30, 1)}

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
