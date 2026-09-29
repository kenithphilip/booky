import threading, os, re, hmac, secrets, shutil, datetime, time, sqlite3, ipaddress
from functools import wraps
from urllib.parse import urlsplit, quote
from uuid import uuid4
from flask import (Flask, request, session, redirect, url_for, render_template, flash,
                   Response, jsonify, send_file, abort, stream_with_context)
from concurrent.futures import TimeoutError as FutureTimeout
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import Markup
import config, db, auth, fetchers, worker, notify, dedupe, enrich, cwa, library, kindle, wanted, bookmeta
import comics, comicmeta, follows, hardcover, anilist
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
# the login page with Turnstile on (L17): Cloudflare's challenge script and frame, nothing else
CSP_TURNSTILE = CSP.replace("script-src 'none'", "script-src https://challenges.cloudflare.com") + \
    "; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com"
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
    if d.startswith(worker.NEEDS_TAG) and worker.FAMILY_NOTE in d:
        return "already in the family library: it appears in your library in a few minutes, nothing was downloaded"
    if d.startswith(worker.NEEDS_TAG) and (worker.AUTO_TAG_NOTE in d or "being added in Calibre" in d):
        return "imported; it appears in your library in a few minutes"
    if d.startswith(worker.NEEDS_TAG):
        return "imported, but the admin has to tag it to you before you can see it"
    if "dropbox/" in d:               # .failed/ paths mean nothing to a user
        return d.split(" (moved to")[0].split(" (could not")[0]
    return d

@app.template_filter("ago_or_in")
def _ago_or_in(ts, now=None):
    """'in 3 h' / '5 min ago' for a Unix time — a reader cares how long, not the timestamp."""
    if not ts:
        return ""
    d = float(ts) - (now or time.time())
    a = abs(d)
    n = (f"{int(a // 86400)} d" if a >= 86400 else f"{int(a // 3600)} h" if a >= 3600
         else f"{max(1, int(a // 60))} min")
    return f"in {n}" if d > 0 else f"{n} ago"

@app.context_processor
def _inject():
    return {"csrf_field": lambda: Markup(f'<input type="hidden" name="csrf" value="{_csrf_token()}">'),
            "kindle_enabled": kindle.configured(), "cfg": config,
            "source_label": config.source_label, "friendly_detail": _friendly_detail,
            "mail_intake": _mail_intake_address,
            # v5.8: what turned up for this reader (New for you), counted for the nav
            "new_for_you": lambda: db.notices(session["user"]) if session.get("user") else [],
            "reading": _reading_badge}

def _reading_badge(state):
    """'Read', 'Reading 45 %' or '' for one book's entry in cwa.reading_state()."""
    if not state:
        return ""
    if state["status"] == "read":
        return "Read"
    return f"Reading {state['pct']} %" if state.get("pct") else "Reading"

def _kobo_link_test(user):
    """Ask Calibre-Web, as the Kobo would, whether this reader's link works: the same
    /kobo/<token>/v1/initialization call a device makes first (L12)."""
    tok = cwa.kobo_token(user, create=False)
    if not tok:
        return "You have no Kobo link yet: generate one first."
    import requests as _r
    try:
        r = _r.get(f"{config.CWA_URL.rstrip('/')}/kobo/{tok}/v1/initialization", timeout=10,
                   headers={"User-Agent": "Kobo bookstack-link-test"})
    except Exception:
        return "The library did not answer the test — it may be restarting; try again in a minute."
    if r.status_code == 200 and "Resources" in r.text:
        return "Your Kobo link works: the library answered exactly as it answers a Kobo."
    if r.status_code in (401, 403):
        return "The library refused this link. Regenerate it and update the device."
    return f"The library answered HTTP {r.status_code} to the test. Ask the admin to check Kobo sync (Users menu)."

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
def _gate_sso():
    """L05: one login. Authelia already checked this person (password + 2FA) and Caddy says so
    with the gate secret; Remote-User names an existing Calibre-Web account. Without the secret
    (a request that did not come through the gate) the header means nothing."""
    if not (config.AUTHELIA_ENABLED and config.GATE_SECRET):
        return None
    who = request.headers.get("Remote-User", "").strip()
    sent = request.headers.get("X-Bookstack-Gate", "")
    if not who or not sent or not hmac.compare_digest(sent, config.GATE_SECRET):
        return None
    if session.get("user") == who:
        return None
    r = auth.fingerprint(who)
    if not r or r is auth.UNAVAILABLE:
        if session.get("user"):              # the gate says someone else: never keep the old identity
            session.clear()
        return None
    session.clear()
    session.permanent = True
    session["user"], session["admin"] = who, r[1]
    session["fp"], session["chk"], session["sso"] = r[0], time.time(), True
    _csrf_token()
    _audit("login_sso", "via the Authelia gate", user=who)
    return None

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

def _upload_limit():
    """L18: over Tailscale (upload.<domain>, which bypasses Cloudflare's 100 MB cap) the portal
    accepts large uploads. Keyed on a header only that Caddy site sets and every site strips from
    clients — never on Host, which a client controls. Registered FIRST (below): the CSRF hook reads
    the form, and a body parsed under the normal limit before this ran was refused anyway."""
    if request.headers.get("X-Bookstack-Upload") == "tailnet":
        request.max_content_length = config.MAX_UPLOAD_TAILNET_MB * 1024 * 1024
app.before_request_funcs.setdefault(None, []).insert(0, _upload_limit)

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
    big = f" Over Tailscale, https://upload.{config.DOMAIN} takes files up to {config.MAX_UPLOAD_TAILNET_MB} MB." if config.DOMAIN else ""
    flash(f"That file is larger than {config.MAX_UPLOAD_MB} MB, which is the limit for browser "
          f"uploads through Cloudflare.{big} Or put it in your dropbox folder (ask the admin how), or e-mail it in.")
    return redirect(url_for("upload")), 302

def _turnstile_on():
    return bool(config.TURNSTILE_SITEKEY and config.TURNSTILE_SECRET)

def _turnstile_ok():
    """Server-side siteverify. An explicit 'no' from Cloudflare refuses the login; Cloudflare
    being unreachable does not lock the household out (the lockout and fail2ban still apply),
    and is audited."""
    import requests as _r
    try:
        r = _r.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", timeout=8,
                    data={"secret": config.TURNSTILE_SECRET, "response": request.form.get("cf-turnstile-response", ""),
                          "remoteip": _ip()})
        return bool(r.json().get("success"))
    except Exception:
        _audit("turnstile_unreachable", "siteverify did not answer; login allowed")
        return True

def _login_page(status=200, headers=None):
    resp = app.make_response((render_template("login.html", turnstile=config.TURNSTILE_SITEKEY if _turnstile_on() else ""), status, headers or {}))
    if _turnstile_on():
        resp.headers["Content-Security-Policy"] = CSP_TURNSTILE
    return resp

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
            return _login_page(429, {"Retry-After": str(max(1, wait))})
        if _turnstile_on() and not _turnstile_ok():
            _audit("login_bot_check_failed", username, user=username or None)
            flash("The automatic bot check did not pass. Wait for the check box to finish, then sign in again.")
            _csrf_token()
            return _login_page(400)
        u = auth.verify(username, request.form.get("password", ""))
        if u is auth.UNAVAILABLE:              # CWA restarting: not the user's fault, not a failure
            _audit("login_unavailable", username, user=username or None)
            flash("The library is restarting. Try again in a minute.")
            _csrf_token()
            return _login_page(503)
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
        return _login_page(401)       # 401 so Caddy's log / fail2ban can count it
    _csrf_token()
    return _login_page()

@app.route("/logout", methods=["POST"])
def logout():
    if session.get("user"):
        _audit("logout")
    sso = session.get("sso")
    session.clear()
    if sso and config.DOMAIN:
        # the gate would sign them straight back in: end the Authelia session too
        return redirect(f"https://auth.{config.DOMAIN}/logout?rd=https://request.{config.DOMAIN}/")
    return redirect(url_for("login"))

@app.route("/logout", methods=["GET"])
def logout_get():
    # a link cannot log someone out (that would be CSRF); the nav button POSTs. Sending a
    # signed-in user to /login only looked as if the logout had worked.
    if session.get("user"):
        flash("Use the Log out button at the top right to sign out.")
        return redirect(url_for("index"))
    return redirect(url_for("login"))

# ---- search & requests -----------------------------------------------------------
def _lib_index():
    return dedupe.Index(session["user"], session.get("admin", False))

def _in_library(seen, w):
    return seen.match(w["title"], w["author"], [{"kind": "isbn", "value": i} for i in w.get("isbns", [])[:40]])

@app.route("/", methods=["GET"])
@login_required
def index():
    """Metadata-first: find the BOOK (Open Library), then its copies on the book's page. The
    old keyword search of the download catalogues is one click away (mode=catalogs), and is
    what the page falls back to when Open Library does not answer."""
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "books")
    if q and mode != "catalogs":
        lang = db.get_prefs(session["user"])["language"]
        try:
            works = bookmeta.search(q, limit=20)
        except bookmeta.Unavailable as e:
            flash(f"Book search is unavailable right now ({e}); showing the catalogues directly.")
            works = None
        if works is not None:
            seen = _lib_index()
            for w in works:
                w["dupe"] = _in_library(seen, w)
            bookmeta.prefetch(works, lang)
            return render_template("books.html", q=q, works=works, shelf=_shelf_link(q=q))
    return _catalog_search(q)

def _shelf_link(q=None, title=None, author=None, isbn=None, kind="ebook"):
    """A search for the same book in Shelfmark, which reaches the wider sources the admin has
    enabled there (with its own metadata-first mode, per-user downloads and request rules)."""
    if not config.SHELF_URL:
        return None
    from urllib.parse import urlencode
    p = {"content_type": "audiobook" if kind == "audio" else "ebook"}
    if q:
        p["q"] = q
    if title:
        p["q"] = title
    if author:
        p["author"] = author
    if isbn:
        p["isbn"] = isbn
    return f"{config.SHELF_URL.rstrip('/')}/?{urlencode(p)}"

def _catalog_search(q):
    results = fetchers.search(q) if q else []
    if results:
        # ONE scoped read of the library for the whole page (it was one query per result), and
        # a match that uses the author too — a same-title book by someone else is not this one.
        seen = dedupe.Index(session["user"], session.get("admin", False))
        for r in results:                  # advisory, only for books this user can see
            r["dupe"] = seen.match(r.get("title"), r.get("author"),
                                   [{"kind": k, "value": v} for k, v in r.get("src_ids") or ()])
    if results and config.ENRICH_METADATA:
        # covers/blurbs in parallel and best-effort, but never at the cost of the page: what
        # Open Library has not answered within ENRICH_DEADLINE is simply left out.
        # fetchers' shared detail pool, not a pool of this request's own: GET / has no rate
        # limit in front of it, and a pool per request meant a reader on refresh multiplied
        # threads instead of sharing a fixed budget with everyone else's search.
        until = time.monotonic() + ENRICH_DEADLINE
        futures = [fetchers.submit_detail(enrich.for_book, r["title"], r.get("author", ""))
                   for r in results]
        for r, f in zip(results, futures):
            try:
                r.update(f.result(timeout=max(0.1, until - time.monotonic())))
            except FutureTimeout:
                f.cancel()      # queued work never starts; a running one holds its own timeout
            except Exception:
                pass
    for r in results:
        r["token"] = db.candidate_put(session["user"], r)
    return render_template("index.html", q=q, results=results, sources=fetchers.enabled_sources())

@app.route("/work/<key>")
@login_required
def work_page(key):
    """One book as the metadata knows it, and every copy of it this stack can fetch — each
    checked against the book and the reader's language, with the reasons shown."""
    try:
        w = bookmeta.work(key)
    except bookmeta.Unavailable as e:
        flash(f"Book details are unavailable right now ({e}).")
        return redirect(url_for("index"))
    if not w:
        abort(404)
    lang = db.get_prefs(session["user"])["language"]
    w["dupe"] = _in_library(_lib_index(), w)
    try:
        found = bookmeta.copies(w, lang)
    except Exception:
        found = []
    offer, refused = [], []
    for c in found:
        if c["match"]["verdict"] == "reject":
            refused.append(c)
        else:
            c["token"] = db.candidate_put(session["user"], dict(c, work_key=w["key"], language=lang))
            offer.append(c)
    return render_template("work.html", w=w, offer=offer, refused=refused, lang=lang,
                           shelf=_shelf_link(title=w["title"], author=w["author"],
                                             isbn=(w["isbns"] or [None])[0]),
                           shelf_audio=_shelf_link(title=w["title"], author=w["author"], kind="audio"))

@app.route("/writer/<key>")
@login_required
def writer_page(key):
    try:
        a = bookmeta.author(key)
        works = bookmeta.author_works(key) if a else []
    except bookmeta.Unavailable as e:
        flash(f"Author details are unavailable right now ({e}).")
        return redirect(url_for("index"))
    if not a:
        abort(404)
    seen = _lib_index()
    for w in works:
        w["dupe"] = _in_library(seen, w)
    series = bookmeta.series_for_author(a.get("goodreads"))
    return render_template("writer.html", a=a, works=works, series=series)

@app.route("/get", methods=["POST"])
@login_required
def get_copy():
    """Request a copy offered on a page. The form carries only a token: the address, the
    expected size and checksum and the match reasons come from the server's own record, so
    nothing a browser sends can point the worker somewhere else."""
    c = db.candidate_get(request.form.get("token"), session["user"])
    if not c:
        flash("That offer has expired; open the book again to see its copies.")
        return redirect(url_for("index"))
    m = c.get("match") or {}
    if m.get("verdict") == "reject":
        abort(400, "That copy was not a match for the book.")
    r = {k: c.get(k) for k in ("kind", "source", "identifier", "title", "author", "download_url",
                               "expect_size", "expect_md5", "expect_sha1", "src_ids", "work_key", "language")}
    r["kind"] = "audio" if r["source"] == "librivox" else "ebook"
    return _queue_request(r, confidence=1 - m["distance"] if m else None, reasons=m.get("reasons"))

@app.route("/request", methods=["POST"])
@login_required
def make_request():
    if request.form.get("token"):
        return get_copy()
    r = {k: request.form.get(k) for k in
         ("kind", "source", "identifier", "title", "author", "download_url")}
    if not r["title"] or not r["download_url"] or not fetchers.source_enabled(r.get("source")):
        flash("Could not queue that item.")
        return redirect(url_for("status"))
    # The form echoes the URL the adapter produced; a tampered one must not reach the worker.
    # (There is no P2P path any more: a torrent flag is a tampered form.)
    if not fetchers.url_allowed(r["source"], r["download_url"]) or request.form.get("is_torrent") == "1":
        _audit("request_refused", f"{r['source']} {r['download_url'][:120]}")
        abort(400, "That download address is not one this source hands out.")
    r["kind"] = "audio" if r["source"] == "librivox" else "ebook"
    return _queue_request(r)

def _queue_request(r, confidence=None, reasons=None):
    """The one way a reader's click becomes a request: source enabled, address allowed, the
    approval setting and the daily limit — whichever page the click came from."""
    if not r.get("title") or not r.get("download_url") or not fetchers.source_enabled(r.get("source")):
        flash("Could not queue that item.")
        return redirect(url_for("status"))
    if not fetchers.url_allowed(r["source"], r["download_url"]):
        _audit("request_refused", f"{r['source']} {r['download_url'][:120]}")
        abort(400, "That download address is not one this source hands out.")
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
    if confidence is not None:
        db.set_match(rid, round(confidence, 3), reasons or [])
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
# Measured: 3.14 s per cover, the SAME 3.14 s on every repeat, for a 5,596-byte image fetched
# over three serial server-side hops. Eight covers on one search page therefore held all eight
# gunicorn threads for over three seconds and /library's p50 went 11.8 ms -> 32.9 ms (max
# 1,135 ms). The proxy itself stays (enrich.py explains why the reader's browser must not talk
# to covers.openlibrary.org), so the fix is to fetch each cover once.
COVER_CACHE_DIR = os.path.join(config.STAGING_DIR, "covers")
# ~28 MB holds the covers of a 5,000-book library; the cap is what keeps a portal that is
# handed thousands of distinct ids from eating the 80 GB disk the library itself needs.
# A constant, not an env read: the librarian container's environment is an explicit allowlist in
# docker-compose.yml, so an os.environ tunable that is not in that list can never be set on the
# deployed stack — it only looks configurable. 64 MB holds ~11k covers at the measured 5.6 KB
# each, which is more than this library will ever show.
COVER_CACHE_MB = 64
# One id can legitimately be asked for at three sizes, so the size is part of the key.
_COVER_KEY = re.compile(r"^https://covers\.openlibrary\.org/b/id/(\d{1,12})-([SML])\.jpg$")
_COVER_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
_cover_lock = threading.Lock()

def _cover_host_ok(url):
    """Open Library serves many covers by redirecting to archive.org; nothing else is fetched."""
    p = urlsplit(url)
    h = (p.hostname or "").lower()
    return p.scheme == "https" and p.username is None and (
        h == "covers.openlibrary.org" or h == "archive.org" or h.endswith(".archive.org")
        or h in COMIC_COVER_HOSTS)

# the comic providers' own image hosts (comicmeta.py), for the Comics pages
COMIC_COVER_HOSTS = ("cdn.mangaupdates.com", "static.metron.cloud", "comicvine.gamespot.com")

def _cover_key(url):
    """The Open Library cover id + size, or None for a URL we will not put on disk. Only the
    canonical /b/id/<n>-<S>.jpg form is cached: the key has to be a safe file name and it has
    to identify the image, and no other shape of URL does both."""
    m = _COVER_KEY.match(url or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None

def _cover_cached(key):
    for ctype, ext in _COVER_TYPES.items():
        path = os.path.join(COVER_CACHE_DIR, f"{key}.{ext}")
        try:
            with open(path, "rb") as f:
                body = f.read(COVER_MAX + 1)
            if len(body) > COVER_MAX:         # not something we wrote; do not serve it
                continue
        except OSError:
            continue
        try:
            os.utime(path, None)              # mtime is the eviction order, so a hit is "recent"
        except OSError:
            pass                              # a cover we cannot touch is still a cover we have
        return body, ctype
    return None, None

def _cover_store(key, body, ctype):
    """Write the image, then bring the directory back under COVER_CACHE_MB by deleting the
    least recently used files. Best effort: a cache that cannot be written must not turn a
    cover that was fetched successfully into a 404."""
    ext = _COVER_TYPES.get(ctype)
    if not ext:
        return
    try:
        os.makedirs(COVER_CACHE_DIR, exist_ok=True)
        tmp = os.path.join(COVER_CACHE_DIR, f".{uuid4().hex}")
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, os.path.join(COVER_CACHE_DIR, f"{key}.{ext}"))
    except OSError:
        return
    _cover_prune()

def _cover_prune():
    """Evict oldest-first until the directory fits. Under one lock: two threads pruning at
    once would each see the other's files and delete far past the cap."""
    cap = COVER_CACHE_MB * 1024 * 1024
    with _cover_lock:
        try:
            files = []
            with os.scandir(COVER_CACHE_DIR) as it:
                for e in it:
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if e.is_file():
                        files.append((st.st_mtime, st.st_size, e.path))
            total = sum(f[1] for f in files)
            for mtime, size, path in sorted(files):
                if total <= cap:
                    break
                try:
                    os.unlink(path)
                    total -= size
                except OSError:
                    pass
        except OSError:
            pass

@app.route("/cover")
@login_required
def cover():
    u = request.args.get("u", "")
    if not (u.startswith("https://covers.openlibrary.org/") or
            (_cover_host_ok(u) and (urlsplit(u).hostname or "").lower() in COMIC_COVER_HOSTS)):
        return "", 404
    key = _cover_key(u)
    if key:
        body, ctype = _cover_cached(key)
        if body is not None:
            return _cover_response(body, ctype)
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
            ctype = ctype.split(";")[0].strip()
            if key:
                _cover_store(key, body, ctype)
            return _cover_response(body, ctype)
        return "", 404
    except Exception:
        return "", 404

def _cover_response(body, ctype):
    # 'private', not 'public': /cover is behind @login_required and Cloudflare sits in front of
    # this origin. A shared cache must not be able to hand an authenticated response to anyone
    # else; the reader's own browser still caches it for a week.
    return Response(body, mimetype=ctype, headers={"Cache-Control": "private, max-age=604800"})

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
    kindle_sends = db.kindle_recent(None if is_admin else session["user"])
    shelf_pending, shelf_error = [], None
    if is_admin:
        import shelfmark_api
        try:
            shelf_pending = shelfmark_api.pending()
        except shelfmark_api.ShelfmarkError as e:
            shelf_error = str(e)
    looking = db.wanted_list(None if is_admin else session["user"])
    for w in looking:
        c = w.get("candidate") or {}
        w["cand_host"] = urlsplit(c.get("download_url") or "").hostname or ""
    return render_template("status.html", rows=rows, pending=pending, failed=failed,
                           admin=is_admin, looking=looking, kindle_sends=kindle_sends,
                           shelf_pending=shelf_pending, shelf_error=shelf_error)

@app.route("/shelfmark/<int:req_id>/<action>", methods=["POST"])
@admin_required
def shelfmark_decide(req_id, action):
    """Approve or deny a Shelfmark request from the portal's Pending card (L16)."""
    import shelfmark_api
    if action not in ("approve", "deny"):
        abort(404)
    note = (request.form.get("reason") or "").strip()
    try:
        shelfmark_api.decide(req_id, action == "approve", note)
        _audit(f"shelfmark_{action}", f"request #{req_id}" + (f": {note[:80]}" if note else ""))
        flash("Approved in Shelfmark: it downloads now, to the reader's own library." if action == "approve"
              else "Denied in Shelfmark; the reader sees your reason there.")
    except shelfmark_api.ShelfmarkError as e:
        flash(f"Shelfmark did not accept that: {e}")
    return redirect(url_for("status"))

# ---- keep looking (wanted.py) ------------------------------------------------------------------
WANTED_TITLE_MAX, WANTED_AUTHOR_MAX = 300, 200

def _own_wanted(wid):
    """The entry if it is this reader's (or the reader is an admin), else a 404: an id in a
    form is a guess away from someone else's list."""
    w = db.wanted_get(wid)
    if not w or (w["owner"] != session["user"] and not session.get("admin", False)):
        abort(404)
    return w

@app.route("/wanted", methods=["POST"])
@login_required
def wanted_add():
    title = (request.form.get("title") or "").strip()[:WANTED_TITLE_MAX]
    author = (request.form.get("author") or "").strip()[:WANTED_AUTHOR_MAX]
    kind = "audio" if request.form.get("kind") == "audio" else "ebook"
    if not dedupe.norm_title(title):
        flash("Type the book's title to keep looking for it.")
        return redirect(url_for("index"))
    limit = 0 if session.get("admin", False) else config.WANTED_MAX_PER_USER
    # the first look is an hour out: the reader has just searched every catalog and found nothing
    work_key = request.form.get("work_key") or None
    if work_key and not bookmeta._KEY.fullmatch(work_key):
        work_key = None
    # from a book's page the reader has only seen its catalogue links, not a keyword search:
    # the first look can come sooner there
    first = time.time() + (600 if work_key else wanted.SCHEDULE[0])
    wid, why = db.wanted_add(session["user"], kind, title, author, first_check=first, limit=limit,
                             same=wanted.same_want, work_key=work_key)
    if wid is None:
        if why == "limit":
            flash(f"You are already keeping an eye out for {limit} books. Cancel one on your Status page first.")
        else:
            flash(f"You are already looking for \"{title}\" — it is on your Status page.")
        return redirect(url_for("status"))
    _audit("wanted_add", f"#{wid} {title} / {author or '-'} [{kind}]")
    flash(f"We'll keep looking for \"{title}\"{' by ' + author if author else ''} and request it as soon as "
          f"a catalog has it{'' if author else ' (without an author we will ask you to confirm the match)'}."
          f" You can see it under Still looking.")
    return redirect(url_for("status"))

@app.route("/wanted/<int:wid>/cancel", methods=["POST"])
@login_required
def wanted_cancel(wid):
    w = _own_wanted(wid)
    if db.wanted_update(wid, only_if_open=True, status="cancelled",
                        detail=f"cancelled by {session['user']}"):
        _audit("wanted_cancel", f"#{wid} {w['title']}")
    return redirect(url_for("status"))

@app.route("/wanted/<int:wid>/accept", methods=["POST"])
@login_required
def wanted_accept(wid):
    """The reader says the candidate is the right book: request it, under every rule an
    ordinary Request click obeys (worker._wanted_request)."""
    w = _own_wanted(wid)
    cand = w.get("candidate")
    if w["status"] != "candidate" or not cand:
        flash("There is nothing to confirm for that one any more.")
        return redirect(url_for("status"))
    is_admin = worker._owner_admin(w["owner"])
    rid, why = worker._wanted_request(w, cand, w.get("confidence") or wanted.FLOOR,
                                      list(w.get("reasons") or []) + [f"confirmed by {session['user']}"],
                                      bool(is_admin))
    if not rid:
        flash(f"Could not request it: {why}.")
        return redirect(url_for("status"))
    db.wanted_update(wid, only_if_open=True, status="found", rid=rid,
                     detail=f"confirmed by {session['user']}; request #{rid} ({why})")
    flash(f"Requested: {cand.get('title')}" + (" — waiting for admin approval." if why == "pending" else " — it will appear in your library shortly."))
    return redirect(url_for("status"))

@app.route("/wanted/<int:wid>/reject", methods=["POST"])
@login_required
def wanted_reject(wid):
    """Not the right book: remember that exact download so it is never offered again, and go
    back to looking."""
    w = _own_wanted(wid)
    cand = w.get("candidate") or {}
    if w["status"] == "candidate" and cand.get("download_url"):
        rejected = list(w.get("rejected") or []) + [cand["download_url"]]
        db.wanted_update(wid, only_if_open=True, status="looking", candidate=None, confidence=None,
                         reasons=[], rejected=rejected[-50:],
                         next_check=time.time() + wanted.next_delay(w["checks"]),
                         detail="you said the last match was not it; still looking")
        _audit("wanted_reject", f"#{wid} {w['title']} <- {cand.get('source')}")
    return redirect(url_for("status"))

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
    # removed by this reader a moment ago: gone for them now, the host job catches up in minutes
    books = [x for x in books if not db.untag_pending(x["id"], user)]
    state = cwa.reading_state(user)
    for b in books:
        b["best"] = library.best_format(b, prefs["preferred_format"])
        b["kindle_ok"] = any(f in config.KINDLE_FORMATS for f in b["formats"])
        b["reading"] = state.get(b["id"])
    u = _cwa_user(user)
    return render_template("library.html", books=books, prefs=prefs, admin=is_admin,
                           kindle_mail=u.get("kindle_mail") or "", q=q, page=page, total=total,
                           first=page * library.PAGE + 1, last=page * library.PAGE + len(books),
                           more=(page + 1) * library.PAGE < total)

# ---- book / author / series pages ----------------------------------------------------------
# Isolation rules, the same as everywhere else in this portal, applied to a SHARED metadata store:
#   * a book page is a 404 for any book the reader cannot already see;
#   * an author or series page is reachable by a non-admin only through a book they OWN, and it
#     lists only their own books. The metadata store is household-wide, so listing 'book 4
#     exists' could only be known because a sibling imported book 4 — that would leak their
#     reading. Gaps are therefore computed from the reader's OWN positions. Admins see all.
def _ids(csv):
    return [int(x) for x in (csv or "").split(",") if x.strip().isdigit()]

@app.route("/book/<int:book_id>")
@login_required
def book_page(book_id):
    user, is_admin = session["user"], session.get("admin", False)
    b = library.book_detail(user, book_id, is_admin)
    if not b:
        abort(404)
    meta = db.meta_for_calibre(book_id) or {}
    work = meta.get("work") or {}
    prefs = db.get_prefs(user)
    b["best"] = library.best_format(b, prefs["preferred_format"])
    b["kindle_ok"] = any(f in config.KINDLE_FORMATS for f in b["formats"]) or "cbz" in b["formats"]
    is_comic = "cbz" in b["formats"]
    return render_template(
        "book.html", b=b, admin=is_admin, is_comic=is_comic, state=cwa.reading_state(user).get(book_id),
        kobo_state=db.comic_convert_state([book_id]).get(book_id) if is_comic else None,
        # Calibre is the authority (it holds hand corrections); the portal's metadata fills gaps
        description=b["description"] or work.get("description") or "",
        first_year=work.get("first_publish_year"),
        # no cover in Calibre: show the provider's (through the local proxy) until the host job
        # has written it into Calibre for the devices
        provider_cover=work.get("cover_url") if (work.get("cover_url") or "").startswith("https://covers.openlibrary.org/") else None,
        authors=meta.get("authors") or [], series=meta.get("series"),
        kindle_mail=_cwa_user(user).get("kindle_mail") or "",
        convert_to=[f for f in config.CONVERT_TARGETS if f not in b["formats"]]
                   if any(f in b["formats"] for f in config.CONVERT_SOURCES) else [],
        converting=db.convert_for_book(book_id),
        replacing=db.replace_for_book(book_id), replace_days=db.REPLACE_DAYS)

@app.route("/book/<int:book_id>/remove", methods=["GET", "POST"])
@login_required
def book_remove(book_id):
    """'Remove from my library': this reader's owner tag comes off the book (the host job does it;
    the portal mounts the library read-only). Everyone else who has it keeps it. The page first
    says what happens and how to delete the copies already on their devices, which the library
    cannot reach (Calibre-Web's Kobo sync never removes a book it no longer shows)."""
    user, is_admin = session["user"], session.get("admin", False)
    b = library.book_detail(user, book_id, is_admin)
    if not b:
        abort(404)
    mine = (not is_admin) or user in (b.get("owners") or [])
    if request.method == "GET":
        return render_template("remove.html", b=b, mine=mine, pending=db.untag_pending(book_id, user),
                               kindle_mail=_cwa_user(user).get("kindle_mail") or "",
                               days=config.LIBRARY_RELEASE_DAYS)
    if not mine:
        flash("This book is not on your own shelf; delete it for everyone in Calibre-Web.")
        return redirect(url_for("book_page", book_id=book_id))
    db.queue_untag(book_id, user)
    _audit("remove_from_library", f"book {book_id}")
    flash(f"“{b['title']}” is being removed from your library: it leaves My books within a couple of "
          "minutes. Delete the copies on your Kobo or Kindle as the page showed.")
    return redirect(url_for("my_library"))

@app.route("/book/<int:book_id>/replace", methods=["POST"])
@login_required
def book_replace(book_id):
    """'Find a better copy': for REPLACE_DAYS the next EPUB of this book that arrives (Shelfmark,
    a dropbox, an upload) replaces the library's file of it — for every reader who has it — while
    the book itself (owners, cover, corrected metadata) stays. Any reader who has the book may
    ask; the admin may for any book."""
    user, is_admin = session["user"], session.get("admin", False)
    if not library.book_detail(user, book_id, is_admin):
        abort(404)
    if request.form.get("action") == "cancel":
        if db.cancel_replace(book_id):
            _audit("replace_cancel", f"book {book_id}")
            flash("Stopped looking for a better copy.")
        return redirect(url_for("book_page", book_id=book_id))
    db.open_replace(book_id, user)
    _audit("replace_open", f"book {book_id}")
    flash(f"Looking for a better copy for {db.REPLACE_DAYS} days: request this book again in Shelfmark and "
          "choose an EPUB result, or upload an EPUB of it. The first one that arrives replaces this file.")
    return redirect(url_for("book_page", book_id=book_id))

@app.route("/book/<int:book_id>/convert", methods=["POST"])
@login_required
def book_convert(book_id):
    """Queue a conversion of one of this reader's books into a format the library lacks."""
    user, is_admin = session["user"], session.get("admin", False)
    b = library.book_detail(user, book_id, is_admin)
    if not b:
        abort(404)
    dst = (request.form.get("format") or "").lower()
    if dst not in config.CONVERT_TARGETS or dst in b["formats"]:
        flash("That format is not one the library can make, or the book already has it.")
        return redirect(url_for("book_page", book_id=book_id))
    src = next((f for f in config.CONVERT_SOURCES if f in b["formats"] and f != dst), None)
    f = library.file_for(user, book_id, src, is_admin) if src else None
    if not f:
        flash("There is no file of this book the library can convert from.")
        return redirect(url_for("book_page", book_id=book_id))
    if not is_admin and db.convert_count(user, time.time() - 86400) >= config.CONVERT_MAX_PER_DAY:
        flash(f"You have asked for {config.CONVERT_MAX_PER_DAY} conversions in the last 24 hours, which is the limit.")
        return redirect(url_for("book_page", book_id=book_id))
    rel = os.path.relpath(f["path"], os.path.realpath(config.LIBRARY_DIR))
    if rel.startswith("..") or os.path.isabs(rel):
        abort(400)
    jid = db.convert_queue(book_id, user, src, dst, rel)
    _audit("convert", f"book {book_id} {src} -> {dst}" + ("" if jid else " (already queued)"))
    flash(f"Converting to {dst.upper()} — it appears on this page, on your devices and in downloads "
          f"within a few minutes." if jid else f"A {dst.upper()} copy is already being made.")
    return redirect(url_for("book_page", book_id=book_id))

@app.route("/book/<int:book_id>/cover")
@login_required
def book_cover(book_id):
    p = library.cover_path(session["user"], book_id, session.get("admin", False))
    if not p:
        abort(404)
    resp = send_file(p, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"   # a cover does not change hourly
    return resp

@app.route("/author/<int:author_id>")
@login_required
def author_page(author_id):
    user, is_admin = session["user"], session.get("admin", False)
    rec = db.author_record(author_id)
    if not rec:
        abort(404)
    linked = [i for w in rec["works"] for i in _ids(w["calibre_ids"])]
    mine = library.visible_ids(user, linked, is_admin)
    yours, others = [], []
    for w in rec["works"]:
        own = [i for i in _ids(w["calibre_ids"]) if i in mine]
        if own:
            yours.append({**w, "book_id": own[0]})
        elif is_admin:
            others.append(w)
    shown = {w["book_id"] for w in yours}
    # Calibre books by that name the portal never enriched still belong on the page
    extra = [x for x in library.books_by_author(user, rec["author"]["name"], is_admin)
             if x["id"] not in shown]
    if not yours and not extra and not is_admin:
        abort(404)                 # not reachable except through a book this reader owns
    return render_template("author.html", a=rec["author"], yours=yours, extra=extra, others=others)

@app.route("/series/<int:series_id>")
@login_required
def series_page(series_id):
    user, is_admin = session["user"], session.get("admin", False)
    rec = db.series_record(series_id)
    if not rec:
        abort(404)
    linked = [i for w in rec["works"] for i in _ids(w["calibre_ids"])]
    mine = library.visible_ids(user, linked, is_admin)
    rows = []
    for w in rec["works"]:
        own = [i for i in _ids(w["calibre_ids"]) if i in mine]
        if own or is_admin:
            rows.append({**w, "book_id": own[0] if own else None})
    if not any(r["book_id"] for r in rows) and not is_admin:
        abort(404)
    # gaps from the reader's OWN whole-number positions only — never from what others hold
    have = sorted({int(r["sort_position"]) for r in rows
                   if r["book_id"] and r["sort_position"] is not None
                   and float(r["sort_position"]).is_integer()})
    missing = [n for n in range(1, have[-1]) if n not in have] if have else []
    nxt = have[-1] + 1 if have else None
    return render_template("series.html", s=rec["series"], rows=rows, missing=missing,
                           nxt=nxt, admin=is_admin)

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
    comic = None if f else library.file_for(user, book_id, "cbz", is_admin)
    if comic:
        f = comic
    if not f:
        flash("No Kindle-compatible format yet (Amazon takes EPUB or PDF by mail); "
              "the library converts new books to EPUB on import when conversion is on.")
        return redirect(url_for("my_library"))
    addr = _cwa_user(user).get("kindle_mail") or ""
    if not addr:
        flash("Set your Kindle address on the Devices page first.")
        return redirect(url_for("devices"))
    # A ceiling on the SMTP account, not on the reader: see config.KINDLE_MAX_PER_DAY. Counted
    # from the audit trail, which already records every send, so there is no second store to
    # keep. Admins are exempt, as with MAX_REQUESTS_PER_DAY.
    limit = 0 if is_admin else config.KINDLE_MAX_PER_DAY
    if limit and db.audit_count("kindle_send", user, time.time() - 86400) >= limit:
        _audit("kindle_quota", f"book {book_id}")
        flash(f"You have sent {limit} books to your Kindle in the last 24 hours, which is the "
              f"limit. Download the book from this page instead, or try again later.")
        return redirect(url_for("my_library"))
    # queued, not sent here: the worker mails it (worker.kindle_once), so a slow mail relay can
    # never outlast Cloudflare's 100 s and show a 524 for a mail that did go out. Counted now,
    # at the click, so the daily limit means what it says.
    if comic:
        jid = comics.kindle_request(user, is_admin, book_id, f["title"])
        _audit("kindle_send", f"{f['filename']} (comic job {jid})")
        flash(f"Making a Kindle copy of \"{f['title']}\" and sending it — a few minutes; the result shows on "
              f"your Status page. A big volume arrives in parts.")
        return redirect(url_for("book_page", book_id=book_id))
    jid = db.kindle_enqueue(user, is_admin, book_id, f["title"])
    _audit("kindle_send", f"{f['filename']} (job {jid})")
    flash(f"Sending \"{f['title']}\" to your Kindle — it is on its way; the result shows on your Status page.")
    return redirect(url_for("my_library"))

# ---- comics and manga (docs/COMICS.md) --------------------------------------------------------
def _comics_on():
    if not config.COMICS_ENABLED:
        abort(404)

@app.route("/comics")
@login_required
def comics_page():
    _comics_on()
    user = session["user"]
    q, kind = (request.args.get("q") or "").strip()[:120], request.args.get("kind", "manga")
    kind = kind if kind in ("comic", "manga") else "manga"
    results, error = [], None
    if q:
        try:
            results = comicmeta.search(q, kind)
        except comicmeta.MetaError as e:
            error = str(e)
    rows = db.comic_list(None if session.get("admin") and request.args.get("all") else user)
    return render_template("comics.html", q=q, kind=kind, results=results, error=error, rows=rows,
                           providers=comicmeta.providers(), labels=comicmeta.KIND_LABEL)

def _series_or_404(provider, sid, language):
    if provider not in ("metron", "comicvine", "mangaupdates") or not re.fullmatch(r"\d{1,20}", sid or ""):
        abort(404)
    try:
        return comicmeta.series(provider, sid, language)
    except comicmeta.MetaError as e:
        flash(f"Could not read this series: {e}")
        return None, None

@app.route("/comics/series/<provider>/<sid>")
@login_required
def comic_series(provider, sid):
    _comics_on()
    user = session["user"]
    language = db.get_prefs(user).get("language") or config.BOOK_LANGUAGE
    language = request.args.get("lang", language) if request.args.get("lang") in config.LANGUAGES else language
    info, items = _series_or_404(provider, sid, language)
    if not info:
        return redirect(url_for("comics_page"))
    mine = db.comic_series_status(user, provider, sid)
    have, books, state = {}, {}, cwa.reading_state(user)
    for it in items:
        m = comics.find_in_library(info["name"], it["number"], info["kind"])
        if m:
            have[it["number"]] = "yours" if user in m["owners"] else "family"
            if user in m["owners"]:
                books[it["number"]] = m["book_id"]
    progress = {n: state.get(bid) for n, bid in books.items()}
    next_unread = next((it for it in items if it["number"] in books and (progress.get(it["number"]) or {}).get("status") != "read"), None)
    return render_template("comic_series.html", s=info, items=items, mine=mine, have=have, language=language,
                           languages=config.LANGUAGES, labels=comicmeta.KIND_LABEL,
                           reading=comicmeta.READING.get(info["kind"], "ltr"), books=books, progress=progress,
                           next_unread=next_unread, followed=db.follow_find(user, "comic", provider, sid))

@app.route("/comics/request", methods=["POST"])
@login_required
def comic_request():
    _comics_on()
    user = session["user"]
    provider, sid = request.form.get("provider", ""), request.form.get("series_id", "")
    language = request.form.get("language") if request.form.get("language") in config.LANGUAGES else None
    reading = request.form.get("reading") if request.form.get("reading") in ("ltr", "rtl") else None
    info, items = _series_or_404(provider, sid, language or config.BOOK_LANGUAGE)
    if not info:
        return redirect(url_for("comics_page"))
    wanted = set(request.form.getlist("number"))
    picked = [it for it in items if it["number"] in wanted][:25]
    if not picked:
        flash("Choose at least one issue or volume.")
        return redirect(url_for("comic_series", provider=provider, sid=sid))
    said = {}
    for it in picked:
        try:
            rid, what = comics.request(user, info, it, language, reading)
            said[what] = said.get(what, 0) + 1
            _audit("comic_request", f"{info['name']} {it['label']} ({what}, #{rid})")
            if what == "queued":
                notify.admin("requested", {"owner": user, "title": f"{info['name']} {it['label']}",
                                           "source": "comics", "status": "queued", "seq": notify.seq_id("comic", rid)})
        except comics.ComicError as e:
            flash(str(e))
            break
    words = {"queued": "requested", "shared": "added from the family library", "owned": "already yours",
             "exists": "already requested", "pending": "waiting for approval"}
    if said:
        flash("; ".join(f"{n} {words[w]}" for w, n in said.items()) + ".")
    return redirect(url_for("comic_series", provider=provider, sid=sid))

@app.route("/comics/requests/<int:rid>/<action>", methods=["POST"])
@login_required
def comic_request_action(rid, action):
    _comics_on()
    user, is_admin = session["user"], session.get("admin", False)
    r = db.comic_get(rid)
    if not r or (r["owner"] != user and not is_admin):
        abort(404)
    if action == "cancel" and r["status"] in ("queued", "pending", "not-found", "failed"):
        db.comic_update(rid, status="cancelled", detail="cancelled")
    elif action == "retry" and r["status"] in ("not-found", "failed", "cancelled"):
        db.comic_update(rid, status="queued", next_try=time.time(), attempts=0, detail="looking again")
    elif action == "approve" and is_admin and r["status"] == "pending":
        db.comic_update(rid, status="queued", next_try=time.time(), detail="approved")
    else:
        abort(400)
    _audit(f"comic_{action}", f"#{rid} {r['series_name']} {r.get('label') or r['number']}")
    return redirect(request.referrer if request.referrer and urlsplit(request.referrer).netloc == request.host
                    else url_for("comics_page"))

@app.route("/book/<int:book_id>/kobo", methods=["POST"])
@login_required
def book_kobo_copy(book_id):
    """'Make Kobo copy' for a comic (made automatically only for readers whose Kobo syncs)."""
    user, is_admin = session["user"], session.get("admin", False)
    if not library.visible(user, book_id, is_admin) or not library.file_for(user, book_id, "cbz", is_admin):
        abort(404)
    db.comic_convert_force(book_id)
    _audit("comic_kobo_copy", f"book {book_id}")
    flash("Making the Kobo copy — a few minutes. Then sync your Kobo.")
    return redirect(url_for("book_page", book_id=book_id))

# ---- following series and authors; New for you (v5.8, follows.py) -------------------------------
@app.route("/following")
@login_required
def following():
    user = session["user"]
    q, kind = (request.args.get("q") or "").strip()[:120], request.args.get("kind", "Series")
    kind = kind if kind in ("Series", "Author") else "Series"
    results, error = [], None
    if q:
        if not hardcover.configured():
            error = "Following books needs the admin's Hardcover key (Library -> Metadata sources)."
        else:
            try:
                results = hardcover.search(q, kind)
            except hardcover.HardcoverError as e:
                error = str(e)
    return render_template("following.html", q=q, kind=kind, results=results, error=error,
                           follows=db.follow_list(user), books_ok=hardcover.configured(),
                           notices=db.notices(user, ("new", "requested"), 100))

@app.route("/follow", methods=["POST"])
@login_required
def follow_add():
    user = session["user"]
    kind, provider, key = request.form.get("kind", ""), request.form.get("provider", ""), request.form.get("key", "")
    name = (request.form.get("name") or "").strip()[:200]
    ok = (kind == "comic" and provider in ("metron", "comicvine", "mangaupdates")) or \
         (kind in ("book-series", "author") and provider == "hardcover")
    if not ok or not re.fullmatch(r"\d{1,20}", key or "") or not name:
        abort(400)
    extra = {}
    if kind == "comic":
        lang = request.form.get("language")
        extra["language"] = lang if lang in config.LANGUAGES else config.BOOK_LANGUAGE
    try:
        _fid, created = follows.follow(user, kind, provider, key, name, extra)
    except follows.FollowError as e:
        flash(str(e))
        return redirect(url_for("following"))
    _audit("follow", f"{kind} {provider}:{key} {name}")
    flash(f"Following {name}. Anything new that comes out shows up under New for you." if created else f"You already follow {name}.")
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("following"))

@app.route("/follows/<int:fid>/stop", methods=["POST"])
@login_required
def follow_stop(fid):
    user = session["user"]
    f = db.follow_get(fid)
    if not f or f["owner"] != user:
        abort(404)
    db.follow_remove(fid, user)
    _audit("unfollow", f"{f['kind']} {f['name']}")
    flash(f"Stopped following {f['name']}.")
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("following"))

@app.route("/notices/<int:nid>/<action>", methods=["POST"])
@login_required
def notice_action(nid, action):
    user = session["user"]
    if action not in ("request", "dismiss"):
        abort(400)
    try:
        what, url = follows.act(user, nid, action)
    except follows.FollowError:
        abort(404)
    except (comics.ComicError, comicmeta.MetaError) as e:
        flash(str(e))
        return redirect(url_for("index"))
    _audit(f"notice_{action}", f"#{nid} {what}")
    if url:
        return redirect(url)                       # a book: Shelfmark, already searching for it
    if action == "request":
        flash({"queued": "Requested.", "shared": "It was in the family library: added to yours.",
               "owned": "You have it already.", "exists": "Already requested.",
               "pending": "Requested; waiting for the admin's approval."}.get(what, "Requested."))
    return redirect(request.referrer if request.referrer and urlsplit(request.referrer).netloc == request.host
                    else url_for("index"))

# ---- AniList (v5.8, anilist.py) ----------------------------------------------------------------
@app.route("/anilist/connect")
@login_required
def anilist_connect():
    if not anilist.configured():
        abort(404)
    session["anilist_state"] = secrets.token_urlsafe(24)
    return redirect(anilist.authorize_url(session["anilist_state"]))

@app.route("/anilist/callback")
@login_required
def anilist_callback():
    if not anilist.configured():
        abort(404)
    state, want = request.args.get("state", ""), session.pop("anilist_state", "")
    if not want or not hmac.compare_digest(state, want):
        flash("That AniList sign-in did not start here, so it was ignored. Try Connect again.")
        return redirect(url_for("devices"))
    if request.args.get("error") or not request.args.get("code"):
        flash("AniList was not connected (the sign-in was cancelled).")
        return redirect(url_for("devices"))
    try:
        name = anilist.connect(session["user"], request.args["code"])
    except anilist.AniListError as e:
        flash(f"AniList was not connected: {e}")
        return redirect(url_for("devices"))
    _audit("anilist_connect", name)
    flash(f"AniList connected as {name}. Manga volumes you finish on your Kobo now count there.")
    return redirect(url_for("devices"))

@app.route("/anilist/disconnect", methods=["POST"])
@login_required
def anilist_disconnect():
    db.anilist_remove(session["user"])
    _audit("anilist_disconnect", "")
    flash("AniList disconnected (you can also revoke it on anilist.co -> Settings -> Apps).")
    return redirect(url_for("devices"))

# ---- audiobooks: a download for phones and tablets (the listening itself is Audiobookshelf's) ----
@app.route("/audiobooks")
@login_required
def my_audiobooks():
    user, is_admin = session["user"], session.get("admin", False)
    items, error = [], None
    if absapi.configured():
        try:
            items = absapi.items_for(user, is_admin)
        except Exception as e:
            error = f"Audiobookshelf did not answer ({str(e)[:100]})"
    else:
        error = "Audiobookshelf is not set up yet"
    return render_template("audiobooks.html", items=items, error=error)

@app.route("/audiobooks/<item_id>/download")
@login_required
def audiobook_download(item_id):
    """One audiobook as it is on disk: a single file as it is, a folder as one ZIP (stored, not
    compressed: audio does not compress), streamed. Only a reader who has it (the admin: any)."""
    user, is_admin = session["user"], session.get("admin", False)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id or ""):
        abort(404)
    try:
        it = next((x for x in absapi.items_for(user, is_admin) if x["id"] == item_id), None)
    except Exception:
        it = None
    root = os.path.realpath(config.AUDIO_DIR)
    real = os.path.realpath(it["path"]) if it else ""
    if not it or not (real == root or real.startswith(root + os.sep)) or not os.path.exists(real):
        _audit("download_denied", f"audiobook {item_id}")
        abort(404)
    name = re.sub(r'[\\/:*?"<>|]+', " ", f"{it['author']} - {it['title']}" if it["author"] else it["title"]).strip()[:150]
    _audit("download", f"audiobook {name}")
    if os.path.isfile(real):
        return send_file(real, as_attachment=True, download_name=f"{name}{os.path.splitext(real)[1]}", max_age=0)
    return Response(stream_with_context(_zip_stream(real)), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip"})

def _zip_stream(folder):
    """A ZIP of a folder, written as it is sent (no temporary copy of a 1 GB audiobook)."""
    import zipfile, io

    class _Sink(io.RawIOBase):
        def __init__(self):
            self.chunks = []
        def writable(self):
            return True
        def write(self, b):
            self.chunks.append(bytes(b))
            return len(b)

    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for base, dirs, files in os.walk(folder):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(base, f)
                if os.path.islink(full) or f.startswith("."):
                    continue
                with open(full, "rb") as src, z.open(os.path.relpath(full, folder), "w", force_zip64=True) as dst:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)
                        if sink.chunks:
                            yield b"".join(sink.chunks)
                            sink.chunks.clear()
                if sink.chunks:
                    yield b"".join(sink.chunks)
                    sink.chunks.clear()
    if sink.chunks:
        yield b"".join(sink.chunks)

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
                # prefs.last_kindle_test was recorded and displayed but never read as a guard,
                # so the button was an unmetered outbound-mail tap. A test proves the
                # approved-sender step once; clicking it again inside five minutes cannot tell
                # you anything the first one did not.
                last = db.get_prefs(user)["last_kindle_test"] or 0
                wait = int(config.KINDLE_TEST_COOLDOWN - (time.time() - last))
                if not addr:
                    flash("Save your Kindle address first.")
                elif wait > 0:
                    flash(f"A test was just sent. Give Amazon a few minutes to deliver it — you "
                          f"can send another in {max(1, wait // 60)} minute(s).")
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
                             notify_email=request.form.get("notify_email") == "1",
                             language=request.form.get("language"))
                if want_auto and not has_addr:
                    flash("Preferences saved, but automatic Send-to-Kindle stays off until you "
                          "save your Kindle address above.")
                else:
                    flash("Preferences saved.")
            elif action == "kobo":
                cwa.kobo_url(user, create=True); _audit("kobo_link"); flash("Your Kobo sync link is ready.")
            elif action == "kobo_reset":
                cwa.reset_kobo_token(user); _audit("kobo_reset"); flash("Kobo link regenerated — update the device.")
            elif action == "kobo_test":
                flash(_kobo_link_test(user))
            elif action == "kobo_prefs":
                cwa.set_kobo_prefs(user, shelves_only=request.form.get("shelves_only") == "1",
                                   hardcover_token=request.form.get("hardcover_token") if request.form.get("hardcover_change") == "1" else None)
                _audit("kobo_prefs")
                flash("Kobo options saved.")
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
                        db.gate_queue(user, new)
                        note += " The sign-in page in front of the sites (Authelia) takes the new password within a minute."
                    _audit("password_change")
                    # NOT Shelfmark: it signs its own session cookie and only checks app.db at
                    # login, so an old cookie keeps working until the container is restarted.
                    # Saying otherwise told someone whose password had leaked that they were
                    # covered when they were not (the restart is an admin action).
                    note += (" Shelfmark keeps you signed in on devices that were already signed in: "
                             "ask the admin to restart Shelfmark if this was because of a leaked password.")
                    # The Kobo sync link is a standing bearer credential for this user's whole
                    # library, sitting in a URL on a device, and nothing here revokes it — only
                    # reset_kobo_token does. A message that carefully lists three other systems
                    # and stays silent about that one reads as "you are covered" to someone who
                    # changed their password BECAUSE something leaked. Only mentioned when a
                    # token actually exists, so a reader without a Kobo is not sent looking for
                    # a feature they do not use.
                    try:
                        has_kobo = bool(cwa.kobo_url(user, create=False))
                    except cwa.CwaError:
                        has_kobo = False
                    if has_kobo:
                        note += (" Your Kobo sync link is a separate key and is NOT changed: "
                                 "regenerate it on this page (Kobo → Regenerate link) if this was "
                                 "because of a leak.")
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
    try:
        kstat = cwa.kobo_status(user) if kobo else None
    except Exception:
        kstat = None
    return render_template("devices.html", u=u, prefs=prefs, kobo=kobo, kstat=kstat,
                           kobo_on=kobo_on, formats=config.FORMATS, kosync=config.KOSYNC_ENABLED,
                           abs_linked=absapi.configured(), anilist_ok=anilist.configured(),
                           anilist_link=db.anilist_get(user))

# ---- admin dashboard ---------------------------------------------------------------
@app.route("/admin/catalogs", methods=["POST"])
@admin_required
def admin_catalogs():
    """Add / test / enable / disable / remove one of the admin's own OPDS catalogs."""
    import catalogs
    act = request.form.get("action")
    cid = (request.form.get("id") or "").strip().lower()
    if act in ("add", "test"):
        name, url = (request.form.get("name") or "").strip(), (request.form.get("url") or "").strip()
        user, pw = (request.form.get("user") or "").strip(), request.form.get("password") or ""
        ok, why = catalogs.test(url, user, pw) if url else (False, "no address given")
        if act == "test":
            flash(f"Test: {why}.")
            return redirect(url_for("admin") + "#catalogs")
        bad = catalogs.validate(cid, name, url)
        if bad:
            flash(f"Catalog not saved: {bad}.")
        elif not ok and request.form.get("force") != "1":
            flash(f"Catalog not saved: {why}. Tick 'save anyway' to keep it regardless.")
        else:
            db.catalog_put(cid, name, url, user, pw, True)
            _audit("catalog_add", f"{cid} {url[:120]}")
            flash(f"Catalog '{name}' saved ({why}). It is searched from now on.")
    elif act in ("enable", "disable"):
        row = next((r for r in db.catalog_rows() if r["id"] == cid), None)
        if row:
            db.catalog_put(row["id"], row["name"], row["url"], row["user"], "", act == "enable")
            _audit(f"catalog_{act}", cid)
    elif act == "remove":
        if db.catalog_delete(cid):
            _audit("catalog_remove", cid)
            flash("Catalog removed.")
    return redirect(url_for("admin") + "#catalogs")

@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    if request.method == "POST" and request.form.get("action") == "add_user":
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
            if config.AUTHELIA_ENABLED:
                # the gate's own login, same password; the host writes it into Authelia's file
                db.gate_queue(u["name"], pw, email=email, display=u["name"], admin=bool(u["role"] & 1))
                note += " Their sign-in page login (Authelia, same password; 2FA is set up at first sign-in) is ready within a minute."
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
                           wanted_counts=db.wanted_counts(),
                           catalogs=__import__("catalogs").all_catalogs(include_disabled=True),
                           users=users, disk=disk, kobo_on=kobo_on, audit=audit_rows, needs_tag=needs_tag,
                           abs_linked=absapi.configured(), quota=config.MAX_REQUESTS_PER_DAY,
                           sources=fetchers.enabled_sources(), health=_health(),
                           alerts_ok=bool(config.NOTIFY_WEBHOOK or (config.ADMIN_EMAIL and kindle.configured())),
                           authelia=config.AUTHELIA_ENABLED, canary=db.canary_recent(10),
                           canary_on=bool(config.CANARY_USERS))

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
    # bytes, not str: hmac.compare_digest raises TypeError on two str operands when either
    # holds a codepoint above U+00FF, so `X-Intake-Token: é中` was an unhandled exception — a
    # traceback in the gunicorn log and a 500 to the caller — on the one anonymous,
    # Authelia-bypassed, CSRF-exempt route this stack exposes. A wrong credential is a 401,
    # and a 500 is neither what Caddy's log nor fail2ban classifies as an auth failure.
    # Still timing-safe: compare_digest over bytes is what it is built for.
    if not hmac.compare_digest(tok.encode("utf-8", "surrogatepass"),
                               config.INTAKE_TOKEN.encode("utf-8", "surrogatepass")):
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
    import shelfmark_api
    loops = ["queue", "dropbox", "housekeeping", "wanted"] + (["imap"] if config.IMAP_HOST else []) \
        + (["shelfmark"] if shelfmark_api.configured() else [])   # readers' Shelfmark requests wait on it
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
            "cwa": cwa_problem or "ok",
            # Degraded, not down — reported, never a reason for compose to restart the portal:
            # a catalogue read that had to ignore Calibre's write-ahead log (recent books may be
            # missing from My books), and metadata providers the circuit breaker stood down.
            "library_read": library.STALE_READ[0] or "ok",
            "metadata_down": _breakers()}

def _breakers():
    try:
        return [b["provider"] for b in db.open_breakers()]
    except Exception:
        return []

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
