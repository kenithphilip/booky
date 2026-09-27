#!/usr/bin/env python3
"""synthetic.py — the canary journey (L08). Two hidden accounts walk the path a family member
walks, twice a day, and the admin hears about it the moment one step stops working:

  log in (portal) -> upload a freshly generated EPUB (through Cloudflare when no gate is in the
  way) -> Calibre-Web imports it WITH the owner tag -> the owner downloads it -> the second
  canary is refused the same file -> OPDS and the Kobo endpoint answer through Cloudflare, and
  OPDS shows the book to its owner only -> Shelfmark accepts the login -> (weekly, opt-in) a
  Send-to-Kindle to the admin's own address -> the book is removed again.

Every run is recorded on /admin (admin_cli canary record), with the import time: it climbs
before anything fails, the earliest sign of a struggling Calibre-Web. A failure alerts through
scripts/alert.sh; success reports to Uptime Kuma (push monitor "Canary journey").

Runs on the host as root (bookstack-canary.timer), standard library only. The accounts come
from /etc/bookstack/canary.env (Operations -> Canary journey writes it). The portal login form
is used unless the Turnstile bot check guards it, which no script can pass: then the portal
mints a session for the canary account only (admin_cli canary session).
Every URL and container can be overridden from the environment (CANARY_*), which is how
tests/stack-test.sh runs this same file against its throwaway stack.
"""
import datetime, io, json, os, re, secrets, sqlite3, subprocess, sys, time, uuid, zipfile
import urllib.error, urllib.parse, urllib.request, base64

STACK = os.environ.get("STACK_DIR", "/srv/bookstack")
ENV = os.path.join(STACK, ".env")
CANARY_ENV = os.environ.get("CANARY_ENV", "/etc/bookstack/canary.env")


def envfile(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                if len(v) >= 2 and v[0] == v[-1] == "'":
                    v = v[1:-1].replace("'\\''", "'")
                out[k.strip()] = v
    except OSError:
        pass
    return out


E = envfile(ENV)
C = envfile(CANARY_ENV)
D = E.get("DOMAIN", "")
env = lambda k, d="": os.environ.get(k) or d
PORTAL = env("CANARY_PORTAL", "http://127.0.0.1:8090")
PUBLIC_PORTAL = env("CANARY_PUBLIC_PORTAL", f"https://request.{D}")
PUBLIC_BOOKS = env("CANARY_PUBLIC_BOOKS", f"https://books.{D}")
SHELF = env("CANARY_SHELF", "http://127.0.0.1:8084")
LIBRARIAN = env("CANARY_LIBRARIAN", "librarian")
CWA = env("CANARY_CWA", "calibre-web")
METADATA_DB = env("CANARY_METADATA_DB", os.path.join(STACK, "library/books/metadata.db"))
IMPORT_TIMEOUT = int(env("CANARY_IMPORT_TIMEOUT", "900"))
ALERT = env("CANARY_ALERT", os.path.join(STACK, "scripts/alert.sh"))
KUMA_PUSH = env("CANARY_KUMA_PUSH", os.path.join(STACK, "scripts/kuma-push.sh"))
OWNER_PREFIX = E.get("OWNER_PREFIX", "owner:") or "owner:"
A, A_PW = C.get("CANARY_A", ""), C.get("CANARY_A_PW", "")
B, B_PW = C.get("CANARY_B", ""), C.get("CANARY_B_PW", "")
UID, GID = E.get("PUID", "1000") or "1000", E.get("PGID", "1000") or "1000"
GATED = E.get("AUTHELIA_ENABLED") == "true"          # the web paths then need an Authelia login
TURNSTILE = bool(E.get("TURNSTILE_SITEKEY"))


# ---- HTTP: cookies carried by hand (the portal cookie is Secure and host-only, and the journey
# moves between the loopback bind and the public name on purpose) -------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


class Client:
    def __init__(self):
        self.cookies = {}

    def req(self, url, data=None, headers=None, files=None, json_body=None, method=None, timeout=60):
        h = {"User-Agent": "bookstack-canary/1"}
        h.update(headers or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode(); h["Content-Type"] = "application/json"
        elif files:
            bd = uuid.uuid4().hex; parts = []
            for k, v in (data or {}).items():
                parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, (fn, content, ct) in files.items():
                parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                             f"Content-Type: {ct}\r\n\r\n".encode() + content + b"\r\n")
            parts.append(f"--{bd}--\r\n".encode()); body = b"".join(parts)
            h["Content-Type"] = f"multipart/form-data; boundary={bd}"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body is not None else "GET"))
        try:
            resp = OPENER.open(r, timeout=timeout)
            st, hd, b = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            st, hd, b = e.code, e.headers, e.read()
        except Exception as e:                 # DNS, TLS, refused: a step failure, not a crash
            return 0, {}, str(e).encode()
        for sc in hd.get_all("Set-Cookie") or []:
            k, _, v = sc.split(";", 1)[0].partition("=")
            if v:
                self.cookies[k.strip()] = v.strip()
            else:
                self.cookies.pop(k.strip(), None)
        return st, dict(hd), b


def csrf(html):
    m = re.search(rb'name="csrf" value="([^"]+)"', html or b"")
    return m.group(1).decode() if m else ""


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def run(args, stdin=None, timeout=120):
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)


def admin_cli(*args, stdin=None):
    r = run(["docker", "exec", "-i", LIBRARIAN, "python", "-m", "admin_cli", *args], stdin=stdin)
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout or "no output")[:200]}


def calibredb(*args):
    return run(["docker", "exec", "-u", f"{UID}:{GID}", "-e", "HOME=/tmp", CWA, "/app/calibre/calibredb",
                *args, "--with-library", "/calibre-library"])


def make_epub(title, author):
    ident = uuid.uuid4().hex
    opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>en</dc:language>'
           f'<dc:identifier id="id">urn:uuid:{ident}</dc:identifier></metadata>'
           '<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>'
           '<spine toc="ncx"><itemref idref="c"/></spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="urn:uuid:{ident}"/></head><docTitle><text>{title}</text></docTitle>'
           '<navMap><navPoint id="n" playOrder="1"><navLabel><text>Start</text></navLabel>'
           '<content src="c.xhtml"/></navPoint></navMap></ncx>')
    page = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>'
            f'{title}</title></head><body><h1>{title}</h1><p>A test book written by the bookstack canary '
            'journey. It is removed again at the end of the run.</p></body></html>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                   'full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
                   zipfile.ZIP_DEFLATED)
        z.writestr("content.opf", opf, zipfile.ZIP_DEFLATED)
        z.writestr("toc.ncx", ncx, zipfile.ZIP_DEFLATED)
        z.writestr("c.xhtml", page, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def library_books(title_like, owner):
    """(id, title, added) for books whose title matches and that carry owner:<owner>. Read-only."""
    c = sqlite3.connect(f"file:{METADATA_DB}?mode=ro", uri=True, timeout=10)
    try:
        return c.execute("SELECT b.id, b.title, b.timestamp FROM books b JOIN books_tags_link l ON l.book=b.id "
                         "JOIN tags t ON t.id=l.tag WHERE b.title LIKE ? AND t.name=?",
                         (title_like, OWNER_PREFIX + owner)).fetchall()
    finally:
        c.close()


# ---- the journey ---------------------------------------------------------------------------
steps = []


def step(name, fn):
    t = time.time()
    try:
        ok, note = fn()
    except Exception as e:                      # a bug here is a failed step, never a silent run
        ok, note = False, f"{e.__class__.__name__}: {e}"
    steps.append({"name": name, "ok": bool(ok), "secs": round(time.time() - t, 1), "note": str(note or "")[:200]})
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}{'' if ok else '  (' + str(note) + ')'}", flush=True)
    return ok


def login(cl, user, pw):
    if TURNSTILE:
        ans = admin_cli("canary", "session", user)
        if not ans.get("ok"):
            return False, ans.get("error", "no session")
        cl.cookies["session"] = ans["cookie"]
        return True, "session minted (login form has the Turnstile check)"
    st, _, b = cl.req(PORTAL + "/login")
    tok = csrf(b)
    st, h, _ = cl.req(PORTAL + "/login", data={"username": user, "password": pw, "csrf": tok})
    if st in (302, 303) and "/login" not in h.get("Location", ""):
        return True, ""
    return False, f"login answered {st}"


def shelfmark_ok():
    if E.get("SHELFMARK_AUTH_METHOD") == "proxy":
        # behind the Authelia gate Shelfmark takes the identity Caddy passes on and has no
        # password login: check it maps the canary to its own account, never an admin, and
        # that a request without an identity is refused
        st, _, b = Client().req(SHELF + "/api/auth/check", headers={"Remote-User": A, "Remote-Groups": "users"})
        try:
            j = json.loads(b or b"{}")
        except ValueError:
            j = {}
        if not (st == 200 and j.get("authenticated") and j.get("username") == A and not j.get("is_admin")):
            return False, f"header login: HTTP {st} {str(j)[:120]}"
        st2, _, _ = Client().req(SHELF + "/api/settings")
        return st2 == 401, "" if st2 == 401 else f"a request with no identity got HTTP {st2} (expected 401)"
    st, _, b = Client().req(SHELF + "/api/auth/login", json_body={"username": A, "password": A_PW})
    return st == 200, f"HTTP {st}"


def page_csrf(cl, base, path="/upload"):
    st, _, b = cl.req(base + path)
    return csrf(b) if st == 200 else ""


def main():
    if not (A and A_PW and B and B_PW):
        print(f"canary: {CANARY_ENV} lacks the two accounts (Operations -> Canary journey sets them up)", file=sys.stderr)
        return 2
    t0 = time.time()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M") + "-" + secrets.token_hex(3)
    title = f"Canary {stamp}"
    ca, cb = Client(), Client()
    web = PORTAL if GATED else PUBLIC_PORTAL     # the gate would answer with its own login page
    ctx = {"book": None, "import_secs": None}

    if not (step("portal login (canary A)", lambda: login(ca, A, A_PW))
            & step("portal login (canary B)", lambda: login(cb, B, B_PW))):
        return finish(t0, ctx)

    def upload():
        tok = page_csrf(ca, web)
        if not tok:
            return False, f"no upload form at {web}/upload"
        st, h, b = ca.req(web + "/upload", data={"csrf": tok},
                          files={"file": (f"{title}.epub", make_epub(title, "Bookstack Canary"), "application/epub+zip")})
        if st in (302, 303) and "/status" in h.get("Location", ""):
            return True, "through Cloudflare" if web == PUBLIC_PORTAL else "on loopback (the Authelia gate is on)"
        return False, f"upload answered {st} {b[:80]!r}"
    if not step("upload through the portal", upload):
        return finish(t0, ctx)

    def imported():
        t = time.time()
        while time.time() - t < IMPORT_TIMEOUT:
            rows = library_books(title, A)
            if rows:
                ctx["book"] = rows[0][0]
                ctx["import_secs"] = round(time.time() - t, 1)
                return True, f"book {ctx['book']} in {ctx['import_secs']} s"
            time.sleep(5)
        return False, f"not in the library with {OWNER_PREFIX}{A} after {IMPORT_TIMEOUT} s"
    if step("imported with the owner tag", imported):
        bid = ctx["book"]

        def download():
            st, h, b = ca.req(f"{web}/download/{bid}/epub", timeout=120)
            return (st == 200 and b[:2] == b"PK"), f"HTTP {st}, {len(b)} bytes"
        step("owner downloads it", download)

        def refused():
            st, _, _ = cb.req(f"{web}/download/{bid}/epub")
            return st in (403, 404), f"the other canary got HTTP {st}"
        step("another reader is refused it", refused)

        def opds():
            st, _, b = ca.req(PUBLIC_BOOKS + "/opds/new", headers=basic(A, A_PW))
            st2, _, b2 = cb.req(PUBLIC_BOOKS + "/opds/new", headers=basic(B, B_PW))
            if st != 200 or st2 != 200:
                return False, f"OPDS answered {st} / {st2}"
            if title.encode() not in b:
                return False, "OPDS does not list the book for its owner"
            if title.encode() in b2:
                return False, "OPDS lists the book to ANOTHER reader (isolation broken)"
            return True, ""
        step("OPDS through Cloudflare, owner only", opds)
    else:
        bid = None

    def kobo():
        r = run(["docker", "exec", LIBRARIAN, "python", "-m", "cwa", "kobo-url", A])
        m = re.search(r"/kobo/([0-9A-Za-z-]{8,})", r.stdout or "")
        if not m:
            return False, "no Kobo token for the canary"
        st, _, _ = Client().req(f"{PUBLIC_BOOKS}/kobo/{m.group(1)}/v1/initialization", headers={"User-Agent": "Kobo"})
        return st == 200, f"HTTP {st}"
    step("Kobo endpoint through Cloudflare", kobo)

    step("Shelfmark login", shelfmark_ok)

    kto = E.get("CANARY_KINDLE_TO", "")
    if kto and bid and datetime.date.today().weekday() == 6 and datetime.datetime.now().hour < 12:
        def kindle():
            r = run(["docker", "exec", LIBRARIAN, "python", "-m", "cwa", "kindle", A, kto])
            if r.returncode:
                return False, "could not set the Kindle address"
            tok = page_csrf(ca, PORTAL, "/library")
            st, h, _ = ca.req(f"{PORTAL}/kindle/{bid}", data={"csrf": tok})
            return st in (302, 303), f"queued for {kto} (HTTP {st})"
        step("Send-to-Kindle (weekly)", kindle)
    return finish(t0, ctx)


def cleanup_leftovers():
    """Books an earlier run left behind (it failed before its cleanup, or the import landed after
    it gave up): any canary book older than an hour."""
    cutoff = time.time() - 3600
    for bid, title, added in library_books("Canary %", A):
        try:
            ts = datetime.datetime.fromisoformat(str(added).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if title.startswith("Canary ") and ts < cutoff:
            calibredb("remove", str(bid))


def finish(t0, ctx):
    cleanup_ok = True
    try:
        if ctx.get("book"):
            r = calibredb("remove", str(ctx["book"]))
            cleanup_ok = r.returncode == 0
        cleanup_leftovers()
    except Exception:
        cleanup_ok = False
    if not cleanup_ok:
        steps.append({"name": "cleanup", "ok": False, "secs": 0, "note": "calibredb remove failed"})
    ok = all(s["ok"] for s in steps)
    run_ = {"ts": t0, "ok": ok, "secs": round(time.time() - t0, 1), "import_secs": ctx.get("import_secs"), "steps": steps}
    rec = admin_cli("canary", "record", stdin=json.dumps(run_))
    if not rec.get("ok"):
        print(f"canary: could not record the run: {rec.get('error')}", file=sys.stderr)
    failed = next((s for s in steps if not s["ok"]), None)
    if failed:
        body = "\n".join(f"- {s['name']}: {'ok' if s['ok'] else 'FAILED ' + s['note']}" for s in steps)
        run([ALERT, f"Bookstack: canary journey FAILED at '{failed['name']}'",
             f"{body}\n\nThis is what a family member would hit. /admin -> Canary journey shows the history."], timeout=60)
        run([KUMA_PUSH, "canary", "down", f"failed at {failed['name']}"], timeout=30)
        print(f"canary: FAILED at {failed['name']}")
        return 1
    run([KUMA_PUSH, "canary", "up", f"import {ctx.get('import_secs')} s"], timeout=30)
    print(f"canary: journey passed in {round(time.time() - t0)} s (import {ctx.get('import_secs')} s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                  # never die silently: the timer has no OnFailure=
        try:
            run([ALERT, "Bookstack: canary journey crashed", f"{e.__class__.__name__}: {e}"], timeout=60)
        finally:
            raise
