#!/usr/bin/env python3
"""End-to-end UAT against the REAL containers (Calibre-Web Automated, Audiobookshelf,
Shelfmark, the portal) plus test doubles (GreenMail, a file server, Authelia + Caddy gate).
Invoked by tests/stack-test.sh once the stack is up. Standard library only.

Journeys: admin bootstrap -> CWA accepts portal-created users -> devices (Kobo token that CWA
honours, Kindle address) -> upload -> tag -> ingest -> CWA import -> My books -> isolated
download -> Kobo device sync sees only the owner's books -> intake webhook -> Shelfmark-style
dropbox drop -> Send-to-Kindle and auto-Kindle (mail captured) -> e-mail intake -> admin
dashboard -> Shelfmark login -> Authelia gate (bypass for Kobo/OPDS, gate for the web) ->
Audiobookshelf API (init, library, users, scan, tag, per-user visibility).
Exit code = number of failed checks."""
import sys, os, re, json, time, sqlite3, zipfile, subprocess, urllib.request, urllib.parse, urllib.error
import http.cookiejar, mimetypes, uuid, base64, smtplib, imaplib, email, io
from email.message import EmailMessage

STACK = sys.argv[1]
PORTAL, CWA, SHELF, ABS = "http://127.0.0.1:18090", "http://127.0.0.1:18083", "http://127.0.0.1:18084", "http://127.0.0.1:23378"
GATE = "http://127.0.0.1:18080"
SMTP_HOST, SMTP_PORT, IMAP_HOST, IMAP_PORT = "127.0.0.1", 13025, "127.0.0.1", 13143
ALICE_PW, BOB_PW, ADMIN_PW = "alicepass-e2e1", "bobpass-e2e11", "adminpass-e2e1"
fails = 0

def check(cond, what, extra=""):
    global fails
    print(("  [ OK ] " if cond else "  [FAIL] ") + what + (f"  ({extra})" if extra and not cond else ""), flush=True)
    if not cond: fails += 1
    return cond

def skip(what, why): print(f"  [skip] {what}  ({why})", flush=True)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None

class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())
    def req(self, url, data=None, headers=None, json_body=None, files=None, method=None):
        h = dict(headers or {}); body = None
        if json_body is not None:
            body = json.dumps(json_body).encode(); h["Content-Type"] = "application/json"
        elif files is not None:
            boundary = uuid.uuid4().hex; parts = []
            for k, v in (data or {}).items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
            for k, (fn, content) in files.items():
                ct = mimetypes.guess_type(fn)[0] or "application/octet-stream"
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n".encode() + content + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode()); body = b"".join(parts)
            h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body is not None else "GET"))
        try:
            resp = self.op.open(r, timeout=60)
            return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except Exception as e:
            return 0, {}, str(e).encode()
    def get(self, url, **kw): return self.req(url, **kw)
    def put(self, url, **kw): return self.req(url, method="PUT", **kw)
    def post(self, url, data=None, **kw): return self.req(url, data=data if data is not None else ({} if kw.get("json_body") is None and kw.get("files") is None else None), **kw)

def csrf(html): m = re.search(rb'name="csrf" value="([^"]+)"', html); return m.group(1).decode() if m else None
def basic(user, pw): return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
def bearer(tok): return {"Authorization": f"Bearer {tok}"}
def wait(pred, secs, every=2):
    end = time.time() + secs
    while time.time() < end:
        try:
            v = pred()
            if v: return v
        except Exception: pass
        time.sleep(every)
    return None
def lib(*args):
    return subprocess.run(["docker", "exec", "-i", "librarian", "python", "-m", "cwa", *args], capture_output=True, text=True)
def jload(b):
    try: return json.loads(b)
    except Exception: return None

def make_epub(title, author, path=None, lang=True):
    path = path or os.path.join(STACK, f"{re.sub(r'[^a-z0-9]+', '_', title.lower())}.epub")
    z = zipfile.ZipFile(path, "w")
    z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf"><dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator>{"<dc:language>en</dc:language>" if lang else ""}<dc:identifier id="bookid">urn:uuid:{uuid.uuid4()}</dc:identifier></metadata><manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="ch1"/></spine></package>')
    z.writestr("OEBPS/toc.ncx", '<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="x"/></head><docTitle><text>T</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Chapter 1</text></navLabel><content src="ch1.xhtml"/></navPoint></navMap></ncx>')
    z.writestr("OEBPS/ch1.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>1</title></head><body><h1>{title}</h1><p>End-to-end test book.</p></body></html>')
    z.close(); return open(path, "rb").read()

def portal_login(user, pw):
    s = Session(); st, h, b = s.get(PORTAL + "/login"); tok = csrf(b)
    st, h, b = s.post(PORTAL + "/login", {"username": user, "password": pw, "csrf": tok})
    return s, st, h.get("Location", "")
def portal_post(s, path, page, data):
    st, h, b = s.get(PORTAL + page); data = dict(data, csrf=csrf(b)); return s.post(PORTAL + path, data)
def cwa_login(user, pw):
    s = Session(); st, h, b = s.get(CWA + "/login")
    m = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', b) or re.search(rb'value="([^"]+)"[^>]*name="csrf_token"', b)
    data = {"username": user, "password": pw, "submit": "", "next": "/"}
    if m: data["csrf_token"] = m.group(1).decode()
    st, h, b = s.post(CWA + "/login", data)
    return s, st, h.get("Location", ""), b
def imported(title, owner):
    c = sqlite3.connect(f"file:{STACK}/library/books/metadata.db?mode=ro", uri=True)
    r = c.execute("SELECT b.id FROM books b JOIN books_tags_link l ON l.book=b.id JOIN tags t ON t.id=l.tag WHERE b.title LIKE ? AND t.name=?", (title + "%", f"owner:{owner}")).fetchone()
    c.close(); return r[0] if r else None
PAYLOADS = {}   # subject -> first attachment bytes (kept out of the json-dumped dicts)
def imap_messages(user, subject_contains=None):
    M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT); M.login(user, "x"); M.select("INBOX")
    _, data = M.search(None, "ALL"); out = []
    for num in data[0].split():
        _, d = M.fetch(num, "(RFC822)"); msg = email.message_from_bytes(d[0][1])
        if subject_contains and subject_contains not in (msg.get("Subject") or ""): continue
        out.append({"subject": msg.get("Subject"), "to": msg.get("To"), "from": msg.get("From"),
                    "attachments": [p.get_filename() for p in msg.walk() if p.get_filename()]})
        PAYLOADS[msg.get("Subject")] = next((p.get_payload(decode=True) for p in msg.walk() if p.get_filename()), None)
    M.logout(); return out

# ---------------------------------------------------------------------------------------
print("== 1. admin bootstrap via the portal image CLI (what the TUI runs)")
r = lib("harden"); check(r.returncode == 0, "cwa harden (registration off, Kobo sync on)", r.stderr)
h = jload(r.stdout) or {}
check(h.get("changed") is True and h.get("restart_cwa") is True, "harden reports it changed settings (CWA restart needed)")
if h.get("restart_cwa"):
    subprocess.run(["docker", "restart", "calibre-web"], capture_output=True)
    check(wait(lambda: Session().get(CWA + "/login")[0] == 200, 180, 3), "calibre-web back after restart")
r = lib("harden"); check((jload(r.stdout) or {}).get("changed") is False, "harden is idempotent (no second restart)")
r = lib("passwd", "admin", "--password", ADMIN_PW); check(r.returncode == 0, "admin password set", r.stderr)
r = lib("add-user", "alice", "--email", "alice@example.test", "--password", ALICE_PW); check(r.returncode == 0, "add-user alice", r.stderr)
# `cwa add-user` now provisions the Audiobookshelf account too (it used to leave the CLI path
# behind /admin and the TUI, so a CLI-created user had no audio login).
check((jload(r.stdout) or {}).get("abs") == "created", "add-user also created the Audiobookshelf account", r.stdout[:160])
r = lib("add-user", "bob", "--password", BOB_PW); check(r.returncode == 0, "add-user bob", r.stderr)
r = lib("list"); users = jload(r.stdout) or []
check({u["name"] for u in users} >= {"admin", "alice", "bob"}, "list shows admin/alice/bob")
check("Guest" not in {u["name"] for u in users}, "CWA's anonymous Guest row is not listed as a user")
check(all(u["isolated"] for u in users if not u["is_admin"]), "non-admins are tag-isolated")
c = sqlite3.connect(f"file:{STACK}/cwa/config/app.db?mode=ro&immutable=1", uri=True)
check(c.execute("SELECT COUNT(*) FROM user WHERE name IN ('alice','bob')").fetchone()[0] == 2, "new users are checkpointed into app.db (visible to an immutable=1 reader like Shelfmark)")
c.close()

print("== 2. Calibre-Web itself accepts the accounts the portal created")
s, st, loc, body = cwa_login("alice", ALICE_PW)
check(st in (302, 303) and "/login" not in loc, "CWA login alice -> redirect into the app", f"status {st} loc {loc}")
s2, st2, loc2, body2 = cwa_login("alice", "wrong-password"); check(st2 == 200 or "/login" in loc2, "CWA rejects a wrong password", f"status {st2}")
sa, sta, loca, _ = cwa_login("admin", ADMIN_PW); check(sta in (302, 303) and "/login" not in loca, "CWA login admin with the new password", f"status {sta}")
st, h, b = Session().get(CWA + "/opds", headers=basic("alice", ALICE_PW)); check(st == 200 and b.lstrip().startswith(b"<?xml"), "OPDS catalog answers for alice (HTTP Basic)", f"status {st}")
st, h, b = Session().get(CWA + "/opds", headers=basic("alice", "wrong")); check(st == 401, "OPDS rejects a wrong password", f"status {st}")

print("== 3. Portal: login, CSRF, devices (Kobo link + Kindle)")
p, st, loc = portal_login("alice", ALICE_PW); check(st == 302 and loc.endswith("/"), "portal login alice", f"{st} {loc}")
_, st, _ = portal_login("alice", "nope"); check(st == 401, "portal rejects a wrong password with 401 (fail2ban-countable)", str(st))
st, h, b = p.post(PORTAL + "/devices", {"action": "kobo"}); check(st == 400, "POST without CSRF token is refused (400)", str(st))
portal_post(p, "/devices", "/devices", {"action": "kobo"})
st, h, b = p.get(PORTAL + "/devices"); m = re.search(rb"kobo/([0-9a-f]{32})", b); check(bool(m), "Kobo link generated on Devices page")
alice_kobo = m.group(1).decode() if m else "0" * 32
st, h, b = Session().get(f"{CWA}/kobo/{alice_kobo}/v1/initialization", headers={"User-Agent": "Kobo"})
check(st == 200 and b"Resources" in b, "CWA Kobo sync endpoint accepts the token from the portal", f"status {st}")
st, h, b = Session().get(f"{CWA}/kobo/{'0'*32}/v1/initialization"); check(st in (401, 403, 404), "CWA rejects an unknown Kobo token", str(st))
portal_post(p, "/devices", "/devices", {"action": "kindle", "kindle_mail": "alice_e2e@kindle.com"})
c = sqlite3.connect(f"file:{STACK}/cwa/config/app.db?mode=ro", uri=True)
check(c.execute("SELECT kindle_mail FROM user WHERE name='alice'").fetchone()[0] == "alice_e2e@kindle.com", "Kindle address stored in CWA's app.db")
check(c.execute("SELECT config_kobo_sync, config_public_reg FROM settings").fetchone() == (1, 0), "CWA settings: kobo sync on, registration off")
c.close()
pb, st, loc = portal_login("bob", BOB_PW); check(st == 302, "portal login bob")
portal_post(pb, "/devices", "/devices", {"action": "kobo"})
st, h, b = pb.get(PORTAL + "/devices"); m = re.search(rb"kobo/([0-9a-f]{32})", b); bob_kobo = m.group(1).decode() if m else "1" * 32
check(bob_kobo != alice_kobo and len(bob_kobo) == 32, "bob gets his own Kobo token")

print("== 3b. Abuse controls: security headers, brute-force lockout, request quota")
st, h, b = Session().get(PORTAL + "/login")
check("script-src 'none'" in h.get("Content-Security-Policy", "") and h.get("X-Frame-Options") == "DENY", "portal sends CSP / anti-framing headers")
def attempt(user, pw, ip):
    s = Session(); st, h, b = s.get(PORTAL + "/login"); tok = csrf(b)
    return s.post(PORTAL + "/login", {"username": user, "password": pw, "csrf": tok}, headers={"X-Forwarded-For": ip})
codes = [attempt("bob", "wrong-pw", "198.51.100.9")[0] for _ in range(4)]
check(codes == [401, 401, 401, 401], "four wrong passwords -> 401 each (LOCKOUT_FAILS=4)", str(codes))
st, h, b = attempt("bob", BOB_PW, "198.51.100.9"); check(st == 429, "5th attempt from that address is locked out even with the right password", str(st))
st, h, b = attempt("bob", BOB_PW, "203.0.113.77"); check(st == 302, "bob still logs in from another address (lock is per user+IP)", str(st))
for i in range(3):
    st, h, b = portal_post(p, "/request", "/status", {"kind": "ebook", "source": "gutenberg", "identifier": f"gutenberg:{i}", "title": f"Quota Book {i}", "author": "Q", "download_url": f"http://filesrv:8000/missing-{i}.epub", "is_torrent": "0"})
st, h, b = portal_post(p, "/request", "/status", {"kind": "ebook", "source": "gutenberg", "title": "Quota Book 3", "author": "Q", "download_url": "http://filesrv:8000/missing-3.epub", "is_torrent": "0"})
st, h, b = p.get(PORTAL + "/status"); check(b"limit of 3 requests" in b and b"Quota Book 3" not in b, "4th request of the day is refused (MAX_REQUESTS_PER_DAY=3)")
check(b"Quota Book 0" in b and b"pending" in b, "non-admin requests wait for approval")
# SSRF fence: a tampered download URL never reaches the worker (400, audited), even from an admin
pa0, st, loc = portal_login("admin", ADMIN_PW)
for bad in ("http://127.0.0.1:2019/config/", "http://169.254.169.254/latest/meta-data/", "https://evil.example/x.epub"):
    st, h, b = portal_post(pa0, "/request", "/status", {"kind": "ebook", "source": "gutenberg", "title": "SSRF", "author": "x", "download_url": bad, "is_torrent": "0"})
    check(st == 400, f"request with a download URL outside the source's allowlist is refused: {bad}", str(st))
st, h, b = portal_post(pa0, "/request", "/status", {"kind": "ebook", "source": "standard_ebooks", "title": "Disabled?", "author": "x", "download_url": "https://standardebooks.org/x.epub", "is_torrent": "0"})
check(st in (302, 400), "request through an enabled/disabled source is decided server-side", str(st))

print("== 4. Upload -> tag -> ingest -> CWA import -> My books -> isolated download")
epub = make_epub("E2E Portal Book", "Test Harness")
st, h, b = p.get(PORTAL + "/upload"); tok = csrf(b)
st, h, b = p.post(PORTAL + "/upload", {"csrf": tok}, files={"file": ("e2e portal book.epub", epub)}); check(st == 302, "upload accepted", str(st))
check(wait(lambda: not os.path.exists(f"{STACK}/library/dropbox/alice/e2e portal book.epub") and os.path.exists(f"{STACK}/library/dropbox/alice") , 60) and wait(lambda: b"e2e portal book" in p.get(PORTAL + "/status")[2], 30), "dropbox watcher picked the file up (Unicode-safe name kept)")
book_id = wait(lambda: imported("E2E Portal Book", "alice"), 300, 5)
check(book_id is not None, "CWA imported the book WITH the owner:alice tag (metadata.db)")
st, h, b = p.get(PORTAL + "/library"); check(b"E2E Portal Book" in b, "book listed under My books for alice")
if book_id:
    st, h, b = p.get(f"{PORTAL}/download/{book_id}/epub")
    check(st == 200 and b[:2] == b"PK" and "attachment" in h.get("Content-Disposition", ""), "alice downloads her book", str(st))
    st, h, b = pb.get(f"{PORTAL}/download/{book_id}/epub"); check(st == 404, "bob cannot download alice's book (404)", str(st))
    st, h, b = pb.get(PORTAL + "/library"); check(b"E2E Portal Book" not in b, "bob's My books does not list alice's book")
    st, h, b = pb.get(PORTAL + "/status"); check(b"e2e" not in b.lower(), "bob's request list does not show alice's upload")
    st, h, b = Session().get(CWA + "/opds/new", headers=basic("bob", BOB_PW)); check(st == 200 and b"E2E Portal Book" not in b, "CWA OPDS for bob hides alice's book", str(st))
    st, h, b = Session().get(CWA + "/opds/new", headers=basic("alice", ALICE_PW)); check(st == 200 and b"E2E Portal Book" in b, "CWA OPDS for alice shows her book", str(st))

print("== 5. Kobo device simulation: /v1/library/sync returns only the owner's books")
def kobo_sync_titles(token):
    st, h, b = Session().get(f"{CWA}/kobo/{token}/v1/library/sync", headers={"User-Agent": "Kobo eReader", "x-kobo-synctoken": ""})
    items = jload(b) if st == 200 else None
    titles = set()
    for it in items or []:
        ent = it.get("NewEntitlement") or it.get("ChangedEntitlement") or {}
        md = ent.get("BookMetadata") or {}
        if md.get("Title"): titles.add(md["Title"])
    return st, titles
st, titles = kobo_sync_titles(alice_kobo); check(st == 200 and any("E2E Portal Book" in t for t in titles), "alice's Kobo receives her book", f"status {st} titles {sorted(titles)}")
st, titles = kobo_sync_titles(bob_kobo); check(st == 200 and not any("E2E Portal Book" in t for t in titles), "bob's Kobo does NOT receive alice's book", f"status {st} titles {sorted(titles)}")

print("== 6. Intake webhook (authorised automation posting a concrete URL for a user)")
os.makedirs(f"{STACK}/testfiles", exist_ok=True)
make_epub("Intake Hook Book", "Automation", path=f"{STACK}/testfiles/intake.epub")
st, h, b = Session().post(PORTAL + "/intake", json_body={"user": "bob", "url": "http://filesrv:8000/intake.epub", "title": "Intake Hook Book", "author": "Automation"})
check(st == 401, "intake without token is refused", str(st))
st, h, b = Session().post(PORTAL + "/intake", json_body={"user": "bob", "url": "http://filesrv:8000/intake.epub", "title": "Intake Hook Book", "author": "Automation"}, headers={"X-Intake-Token": "e2e-intake"})
check(st == 202, "intake accepted (202)", f"{st} {b[:80]!r}")
st2, h2, b2 = Session().post(PORTAL + "/intake", json_body={"user": "bob", "url": "http://filesrv:8000/intake.epub", "title": "Intake Hook Book", "author": "Automation"}, headers={"X-Intake-Token": "e2e-intake"})
check(st2 == 200 and b'"duplicate"' in b2, "a replayed intake POST returns the existing request instead of downloading twice", f"{st2} {b2[:100]!r}")
intake_id = wait(lambda: imported("Intake Hook Book", "bob"), 300, 5)
check(intake_id is not None, "intake download was fetched, tagged owner:bob and imported by CWA")
if intake_id:
    st, h, b = pb.get(PORTAL + "/library"); check(b"Intake Hook Book" in b, "bob sees the intake book")
    st, h, b = p.get(PORTAL + "/library"); check(b"Intake Hook Book" not in b, "alice does not see bob's intake book")

print("== 7. Shelfmark-style drop: a file appearing in library/dropbox/<user> is tagged + imported")
make_epub("Dropbox Drop Book", "Shelfmark Sim", path=f"{STACK}/library/dropbox/alice/shelfmark drop.epub")
drop_id = wait(lambda: imported("Dropbox Drop Book", "alice"), 300, 5)
check(drop_id is not None, "dropbox file was tagged owner:alice and imported (Shelfmark/rsync/Syncthing path)")
check(not os.path.exists(f"{STACK}/library/dropbox/alice/shelfmark drop.epub"), "dropbox file consumed after ingest")

print("== 8. Send-to-Kindle from the portal + auto-Kindle, captured by the test mail server")
if book_id:
    st, h, b = portal_post(p, f"/kindle/{book_id}", "/library", {"format": "epub"})
    st2, h2, b2 = p.get(PORTAL + "/library")      # the POST redirects; the message is on the next page
    check(st in (302, 303) and b"on its way" in b2, "Send to Kindle answers at once and queues the mail (L21: no 524 on a slow relay)", f"{st}")
    msgs = wait(lambda: imap_messages("alice_e2e@kindle.com", "E2E Portal Book") or None, 90, 3) or []
    check(bool(msgs) and any(a and a.lower().endswith(".epub") for a in msgs[0]["attachments"]), "Send to Kindle delivered an EPUB attachment to the Kindle address", json.dumps(msgs)[:200])
    check(bool(msgs) and "library@example.test" in (msgs[0]["from"] or ""), "mail comes from the configured SMTP_FROM")
# An EPUB with no dc:language (Amazon bounces those) is repaired by the portal on the way out;
# the library copy stays as uploaded (CWA's import-time fixer is off: it strips comic tags).
nl = make_epub("No Language Book", "Test Harness", lang=False)
st, h, b = p.get(PORTAL + "/upload"); tok = csrf(b); p.post(PORTAL + "/upload", {"csrf": tok}, files={"file": ("no language book.epub", nl)})
nl_id = wait(lambda: imported("No Language Book", "alice"), 300, 5)
check(nl_id is not None, "EPUB without dc:language imported (owner-tagged)")
if nl_id:
    lib_file = next((os.path.join(r, f) for r, _, fs in os.walk(f"{STACK}/library/books") for f in fs if f.endswith(".epub") and "No Language Book" in f), None)
    check(lib_file is not None and b"dc:language" not in zipfile.ZipFile(lib_file).read("OEBPS/content.opf"), "library copy left as uploaded (no import-time rewrite)")
    portal_post(p, f"/kindle/{nl_id}", "/library", {"format": "epub"})
    nm = wait(lambda: imap_messages("alice_e2e@kindle.com", "No Language Book") or None, 90, 3) or []
    ok_fix = False
    if nm and PAYLOADS.get(nm[0]["subject"]):
        try:
            ok_fix = b"<dc:language>en</dc:language>" in zipfile.ZipFile(io.BytesIO(PAYLOADS[nm[0]["subject"]])).read("OEBPS/content.opf")
        except Exception as e:
            print("   (attachment not a zip:", e, ")")
    check(ok_fix, "Send to Kindle added dc:language to the mailed copy (portal-side Kindle fix)")
    st, h, b = p.get(PORTAL + "/status"); check(b"Kindle fixes applied: encoding, language" in b or b"Kindle fixes applied: language" in b, "the user is told which Kindle fixes were applied (Status: Sent to Kindle)")
portal_post(p, "/devices", "/devices", {"action": "prefs", "preferred_format": "epub", "auto_kindle": "1"})
auto = make_epub("Auto Kindle Book", "Test Harness")
st, h, b = p.get(PORTAL + "/upload"); tok = csrf(b); p.post(PORTAL + "/upload", {"csrf": tok}, files={"file": ("auto kindle book.epub", auto)})
msgs = wait(lambda: imap_messages("alice_e2e@kindle.com", "auto kindle book") or None, 120, 3) or []
check(bool(msgs) and any((a or "").endswith(".epub") for a in msgs[0]["attachments"]), "auto-Kindle mailed the new upload to alice's Kindle without a click", json.dumps(msgs)[:200])
check(wait(lambda: imported("Auto Kindle Book", "alice"), 300, 5) is not None, "...and it was still imported into her library")
portal_post(p, "/devices", "/devices", {"action": "prefs", "preferred_format": "epub"})   # auto-kindle off again
portal_post(p, "/devices", "/devices", {"action": "kindle_test"})
tm = wait(lambda: imap_messages("alice_e2e@kindle.com", "test") or None, 40, 3) or []
check(bool(tm), "Devices -> 'Send a test to my Kindle' delivers a test mail", json.dumps(tm)[:160])
st, h, b = p.get(PORTAL + "/devices"); check(b"Last test" in b or b"last test" in b.lower(), "Devices page records the last Kindle test")

print("== 8b. Non-EPUB uploads: PDF and CBZ are tagged before import; Unicode names; CBR refused; TXT needs the admin")
def make_pdf(title):
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
            b"<< /Length 44 >>stream\nBT /F1 12 Tf 20 100 Td (hello) Tj ET\nendstream",
            ("<< /Title (%s) /Author (PDF Person) >>" % title).encode()]
    out = b"%PDF-1.4\n"; offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out
def make_cbz():
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    buf = zipfile.ZipFile(zp := os.path.join(STACK, "comic.cbz"), "w")
    buf.writestr("001.png", png); buf.writestr("002.png", png); buf.close(); return open(zp, "rb").read()
def upload_as(sess, fn, content):
    st, h, b = sess.get(PORTAL + "/upload"); tok = csrf(b)
    return sess.post(PORTAL + "/upload", {"csrf": tok}, files={"file": (fn, content)})
st, h, b = upload_as(p, "PDF Paper Book.pdf", make_pdf("PDF Paper Book")); check(st == 302, "PDF upload accepted", str(st))
st, h, b = upload_as(p, "Comic Test Book.cbz", make_cbz()); check(st == 302, "CBZ upload accepted", str(st))
st, h, b = upload_as(p, "Война и мир.epub", make_epub("Война и мир", "Лев Толстой")); check(st == 302, "Cyrillic filename accepted", str(st))
st, h, b = upload_as(p, "bad.cbr", b"Rar!\x1a\x07\x00"); st2, h2, b2 = p.get(PORTAL + "/upload")
check(b"convert it to CBZ" in b2, "CBR is refused with an explanation")
st, h, b = upload_as(pb, "plain notes.txt", b"just some text\n"); check(st == 302, "TXT upload accepted (cannot carry a tag)")
pdf_id = wait(lambda: imported("PDF Paper Book", "alice"), 300, 5); check(pdf_id is not None, "PDF imported WITH owner:alice (tag carried in /Keywords)")
cbz_id = wait(lambda: imported("Comic Test Book", "alice"), 300, 5); check(cbz_id is not None, "CBZ imported WITH owner:alice (ComicBookInfo tag)")
cyr_id = wait(lambda: imported("Война и мир", "alice"), 300, 5); check(cyr_id is not None, "Cyrillic-titled EPUB imported with the owner tag")
st, h, b = p.get(PORTAL + "/library")
check(all(x in b.decode("utf-8", "replace") for x in ("PDF Paper Book", "Comic Test Book", "Война и мир")), "alice sees PDF, CBZ and the Cyrillic book under My books")
if pdf_id:
    st, h, b = p.get(f"{PORTAL}/download/{pdf_id}/pdf"); check(st == 200 and b[:4] == b"%PDF", "alice downloads the PDF as PDF (not converted)", str(st))
    st, h, b = pb.get(f"{PORTAL}/download/{pdf_id}/pdf"); check(st == 404, "bob cannot download alice's PDF", str(st))
st, h, b = pb.get(PORTAL + "/status"); check(b"needs-tag" in b, "bob's TXT upload shows the honest 'needs-tag' state")
st, h, b = portal_login("admin", ADMIN_PW)[0].get(PORTAL + "/admin"); check(b"needs-tag" in b or b"needs the admin" in b.lower() or b"Needs tag" in b, "admin page lists the needs-tag item")

print("== 9. E-mail intake: a book mailed to intake+bob@ lands in bob's library")
def mail_in(sender, plus_user, title, fn):
    m = EmailMessage(); m["From"] = sender; m["To"] = f"intake+{plus_user}@example.test"; m["Delivered-To"] = f"intake+{plus_user}@example.test"
    m["Subject"] = f"a book for {plus_user}"; m.set_content("attached")
    m.add_attachment(make_epub(title, "Postman"), maintype="application", subtype="epub+zip", filename=fn)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.sendmail(sender, ["intake@example.test"], m.as_bytes())
        return True
    except Exception as e:
        print("   smtp error:", e); return False
check(mail_in("bob@example.test", "bob", "Mailed In Book", "mailed in book.epub"), "test mail server accepted the inbound message (from bob's own address)")
check(mail_in("stranger@example.test", "alice", "Planted Book", "planted.epub"), "...and a message from a stranger addressed to alice")
check(mail_in("bob@example.test", "nosuchuser", "Ghost Book", "ghost.epub"), "...and one for a user that does not exist")
mail_id = wait(lambda: imported("Mailed In Book", "bob"), 360, 5)
check(mail_id is not None, "IMAP intake routed the attachment to bob (plus-address, sender = bob's e-mail) -> tagged owner:bob -> imported")
check(imported("Planted Book", "alice") is None and not os.path.exists(f"{STACK}/library/dropbox/alice/planted.epub"), "a stranger cannot plant a book in alice's library by e-mail")
check(not os.path.exists(f"{STACK}/library/dropbox/nosuchuser"), "mail for a non-existent user creates nothing")

print("== 10. Admin dashboard, approvals, notifications, audit trail")
pa, st, loc = portal_login("admin", ADMIN_PW); check(st == 302, "portal login admin")
st, h, b = pa.get(PORTAL + "/admin"); check(st == 200 and b"owner:alice" in b and b"owner:bob" in b, "admin dashboard lists isolated users")
adm_msgs = wait(lambda: imap_messages("admin@example.test", "approval needed") or None, 30, 3) or []
check(len(adm_msgs) >= 3 and "Quota Book" in adm_msgs[0]["subject"], "admin was e-mailed for each request awaiting approval", json.dumps(adm_msgs)[:160])
portal_post(p, "/devices", "/devices", {"action": "prefs", "preferred_format": "epub", "notify_email": "1"})
st, h, b = pa.get(PORTAL + "/status")
m = re.search(rb'action="/deny/(\d+)"', b); deny_id = m.group(1).decode() if m else None
check(bool(deny_id), "admin sees Approve/Deny controls for pending requests")
if deny_id:
    portal_post(pa, f"/deny/{deny_id}", "/status", {})
    dm = wait(lambda: imap_messages("alice@example.test", "was denied") or None, 30, 3) or []
    check(bool(dm) and "Quota Book" in dm[0]["subject"], "requester was e-mailed when her request was denied (opted in)", json.dumps(dm)[:160])
st, h, b = pa.get(PORTAL + "/admin")
check(b"login_locked" in b and b"198.51.100.9" in b and b"download_denied" in b and b"deny" in b, "audit trail shows lockouts with the real client IP, denied downloads and admin actions")
st, h, b = p.get(PORTAL + "/admin"); check(st == 302, "alice is bounced from /admin", str(st))
portal_post(pa, "/admin", "/admin", {"action": "add_user", "name": "carol", "email": "carol@example.test", "password": "carolpass-e2e"})
r = lib("list"); check("carol" in r.stdout and os.path.isdir(f"{STACK}/library/dropbox/carol"), "admin created carol from the dashboard (+ dropbox)")
sc, stc, locc, _ = cwa_login("carol", "carolpass-e2e"); check(stc in (302, 303) and "/login" not in locc, "CWA accepts carol")
pc, st, loc = portal_login("carol", "carolpass-e2e")
portal_post(pc, "/devices", "/devices", {"action": "password", "current": "carolpass-e2e", "new": "carol-new-pw-e2e", "repeat": "carol-new-pw-e2e"})
sc, stc, locc, _ = cwa_login("carol", "carol-new-pw-e2e"); check(stc in (302, 303) and "/login" not in locc, "self-service password change is accepted by Calibre-Web")
st, h, b = Session().post(ABS + "/login", json_body={"username": "carol", "password": "carol-new-pw-e2e"}); check(st == 200, "...and by Audiobookshelf (account created by the admin page, password kept in step)", str(st))
sc, stc, locc, _ = cwa_login("carol", "carolpass-e2e"); check(stc == 200 or "/login" in locc, "old password no longer works")
st, h, b = pa.get(PORTAL + "/status"); check(b"Intake Hook Book" in b and b"e2e portal book" in b and b"shelfmark drop" in b, "admin sees every request in the queue (intake, upload, dropbox)")

print("== 11. Shelfmark (CWA companion) health + login with the same account")
st, h, b = Session().get(SHELF + "/api/health"); check(st == 200, "shelfmark /api/health", str(st))
ss = Session(); st, h, b = ss.post(SHELF + "/api/auth/login", json_body={"username": "alice", "password": ALICE_PW}); check(st == 200, "shelfmark login alice via CWA auth", f"status {st} body {b[:120]!r}")
st2, h2, b2 = Session().post(SHELF + "/api/auth/login", json_body={"username": "alice", "password": "wrong"}); check(st2 in (401, 403, 400), "shelfmark rejects a wrong password", str(st2))
st, h, b = ss.get(SHELF + "/api/auth/check"); check(st == 200 and b'"authenticated":true' in b.replace(b" ", b""), "shelfmark session check", f"{st} {b[:100]!r}")
st, h, b = Session().post(SHELF + "/api/auth/login", json_body={"username": "carol", "password": "carol-new-pw-e2e"}); check(st == 200, "shelfmark accepts a user created AFTER it started, with her self-changed password (WAL checkpoint)", str(st))
st, h, b = Session().post(SHELF + "/api/auth/login", json_body={"username": "carol", "password": "carolpass-e2e"}); check(st in (401, 403), "shelfmark rejects carol's old password", str(st))

print("== 12. Authelia gate (production forward_auth snippet, plain HTTP): web gated, devices bypass")
g = Session()
BROWSER = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0"}
st, h, b = g.get(GATE + "/", headers={"Host": "request.example.test", **BROWSER})
check(st == 302 and "auth.example.test" in h.get("Location", "") and b"Sign in</h2>" not in b, "portal web UI is gated: a browser is redirected to the Authelia portal", f"status {st} loc {h.get('Location','')[:80]}")
st, h, b = g.get(GATE + "/", headers={"Host": "books.example.test", **BROWSER}); check(st == 302 and "auth.example.test" in h.get("Location", ""), "Calibre-Web web UI is gated: a browser is redirected to the Authelia portal", f"status {st} loc {h.get('Location','')[:80]}")
st, h, b = g.get(GATE + "/api/health", headers={"Host": "request.example.test", "Accept": "application/json"}); check(st == 401, "non-browser request without a session gets 401 (no content leaks)", str(st))
st, h, b = g.get(f"{GATE}/kobo/{alice_kobo}/v1/initialization", headers={"Host": "books.example.test", "User-Agent": "Kobo"}); check(st == 200 and b"Resources" in b, "/kobo/* bypasses the gate (Kobo devices keep syncing)", str(st))
st, h, b = g.get(GATE + "/opds", headers={"Host": "books.example.test", **basic("alice", ALICE_PW)}); check(st == 200, "/opds bypasses the gate (reader apps keep working)", str(st))
st, h, b = g.get(GATE + "/opds", headers={"Host": "books.example.test"}); check(st == 401, "/opds still requires CWA credentials behind the bypass", str(st))
st, h, b = g.get(GATE + "/opds", headers={"Host": "books.example.test", "Remote-User": "admin"}); check(st == 401, "a client-supplied Remote-User header is stripped on a bypassed path", str(st))
st, h, b = g.get(GATE + "/kosync/users/auth", headers={"Host": "books.example.test", **basic("alice", ALICE_PW)})
check(st == 200 and b'"authorized"' in b, "/kosync (KOReader) bypasses the gate and authenticates with the library password", f"{st} {b[:120]}")
st, h, b = g.get(GATE + "/kosync/users/auth", headers={"Host": "books.example.test", **basic("alice", "wrong-pw")})
check(st == 401, "/kosync refuses a wrong password (no gate, so CWA must do it)", str(st))
# The endpoint that actually had the problem. /kosync/users/auth is the ONE kosync route that
# answers 401; the progress routes raise KOSyncError(ERROR_UNAUTHORIZED_USER) and
# handle_sync_error() returns it as 400 (cps/progress_syncing/protocols/kosync.py:532,223), so
# the 401-only fail2ban filter counted none of them and the jail could never fire. Assert the
# status this stack now bans on, so a CWA change that turned it into something else would show
# up here instead of silently un-banning the oracle.
st, h, b = g.get(f"{GATE}/kosync/syncs/progress/{'a'*32}", headers={"Host": "books.example.test", **basic("alice", "wrong-pw")})
check(st == 400, "/kosync/syncs/progress refuses a wrong password with 400 (the status configs/fail2ban/caddy-device-auth.conf now counts)", str(st))
st, h, b = g.get(f"{GATE}/kosync/syncs/progress/{'a'*32}", headers={"Host": "books.example.test"})
check(st in (400, 401), "/kosync/syncs/progress refuses an anonymous caller", str(st))
# a full KOReader round trip: PUT progress as alice, read it back, and make sure bob cannot
doc = "e2e" + "0" * 29
prog = {"document": doc, "progress": "/body/DocFragment[3]", "percentage": 0.42, "device": "KOReader", "device_id": "e2e-dev"}
st, h, b = g.put(GATE + "/kosync/syncs/progress", json_body=prog, headers={"Host": "books.example.test", **basic("alice", ALICE_PW)})
check(st == 200 and doc.encode() in b, "KOReader progress is accepted (PUT /kosync/syncs/progress)", f"{st} {b[:160]}")
st, h, b = g.get(f"{GATE}/kosync/syncs/progress/{doc}", headers={"Host": "books.example.test", **basic("alice", ALICE_PW)})
check(st == 200 and b'"percentage"' in b and b"DocFragment" in b, "alice reads her own progress back", f"{st} {b[:160]}")
st, h, b = g.get(f"{GATE}/kosync/syncs/progress/{doc}", headers={"Host": "books.example.test", **basic("bob", BOB_PW)})
check(b"DocFragment" not in b, "bob does not see alice's reading position", f"{st} {b[:160]}")
# CWA ships its convert-library / epub-fixer / log endpoints with no authentication at all
# (reproduced against the image: 200 anonymously). Caddy must 403 them for everyone, gate or no gate.
BOOKS = {"Host": "books.example.test"}
blocked = ["/cwa-convert-library-overview", "/cwa-convert-library-start", "/convert-library-status",
           "/cwa-epub-fixer-overview", "/cwa-epub-fixer-start", "/epub-fixer-status",
           "/cwa-logs/read/x", "/cwa-logs/download/x", "/reconnect",
           "/cwa-convert-library-overview?x=1", "/CWA-Convert-Library-Overview", "//cwa-logs/read/x",
           # ';'-parameter shape: a different string to Caddy's `path` matcher, same endpoint to
           # some routers (Werkzeug 404s it today, so this is the last variant, not a live hole)
           "/cwa-convert-library-start;x", "/cwa-logs/read/x;y"]
codes = {u: g.get(GATE + u, headers=BOOKS)[0] for u in blocked}
check(all(c == 403 for c in codes.values()), "CWA's unauthenticated admin-job endpoints are 403 at the edge (anonymous, no gate needed)", str(codes))
st, h, b = g.post(GATE + "/cwa-internal/reconnect-db", json_body={}, headers=BOOKS)
check(st == 403, "POST /cwa-internal/* is 403 too", str(st))
# /duplicates/invalidate-cache is the same class: cps/duplicates.py:1196 declares it with
# @csrf.exempt and NO auth decorator, while every sibling in that file carries
# @login_required_if_no_ano + @admin_or_edit_required. Verified anonymously against the pinned
# image: POST http://127.0.0.1:8083/duplicates/invalidate-cache -> 200 {"success":true}, which
# commits `UPDATE cwa_duplicate_cache SET scan_pending=1` into cwa.db. Its only in-tree caller
# is CWA's own scripts/ingest_processor.py over container loopback, which never passes through
# Caddy, so the 403 costs nothing.
st, h, b = g.post(GATE + "/duplicates/invalidate-cache", json_body={}, headers=BOOKS)
check(st == 403, "POST /duplicates/invalidate-cache is 403 at the edge (anonymous CSRF-exempt write into cwa.db)", str(st))
st, h, b = g.post(GATE + "/duplicates/invalidate-cache;x", json_body={}, headers=BOOKS)
check(st == 403, "...and its ';'-parameter shape is caught by the path_regexp companion", str(st))
# ONLY that one path: /duplicates and /duplicates/status are the admin's own UI and are
# properly gated by CWA. Blocking the whole /duplicates* prefix would break them for no reason.
st, h, b = g.get(GATE + "/duplicates/status", headers=BOOKS)
check(st != 403, "...while /duplicates/status is NOT blocked (CWA gates it itself)", str(st))
st, h, b = g.get(GATE + "/login", headers=BOOKS)
check(st != 403, "...while Calibre-Web's own login page is not caught by the block (the Authelia gate still applies)", str(st))
st, h, b = g.get(GATE + "/opds", headers={**BOOKS, **basic("alice", ALICE_PW)})
check(st == 200, "...and OPDS is not caught by the block", str(st))
# The Caddy matcher and the Authelia rule must agree: Authelia's '^/opds.*$' bypassed /opdsfoo,
# so Caddy's anchored matcher bought nothing — the LOOSER list is the one that decides.
st, h, b = g.get(GATE + "/opdsfoo", headers={**BOOKS, **BROWSER})
check(st == 302 and "auth.example.test" in h.get("Location", ""), "/opdsfoo is NOT bypassed (Caddy's and Authelia's bypass lists agree)", f"status {st} loc {h.get('Location','')[:80]}")
st, h, b = g.get(GATE + "/ping", headers={"Host": "audio.example.test"}); check(st == 200, "Audiobookshelf /ping bypasses the gate (mobile apps keep working)", str(st))
# POST /init is how the FIRST root user is created; with the gate on there is no Authelia
# account that could get past it either, so gating it locked the admin out of the setup screen.
st, h, b = g.post(GATE + "/init", json_body={}, headers={"Host": "audio.example.test"})
check(st not in (302, 401), "Audiobookshelf POST /init bypasses the gate (first-run root creation)", str(st))
# A Socket.IO client may ask for the bare /socket.io (no trailing slash); Caddy emitted a
# prefix-only matcher for it while Authelia allowed both.
st, h, b = g.get(GATE + "/socket.io?EIO=4&transport=polling", headers={"Host": "audio.example.test"})
check(st not in (302, 401), "bare /socket.io (no trailing slash) bypasses the gate", str(st))
st, h, b = g.post(GATE + "/login", json_body={"username": "alice", "password": ALICE_PW}, headers={"Host": "audio.example.test"}); check(st in (200, 401), "Audiobookshelf's own /login is reachable through the gate", str(st))
st, h, b = g.get(GATE + "/", headers={"Host": "audio.example.test", **BROWSER}); check(st == 302 and "auth.example.test" in h.get("Location", ""), "Audiobookshelf web UI is still gated for browsers", str(st))
st, h, b = g.get(GATE + "/healthz", headers={"Host": "request.example.test"}); check(st in (302, 401), "/healthz is gated at the edge (health checks use loopback)", str(st))
st, h, b = g.post(GATE + "/intake", json_body={"user": "bob", "url": "http://filesrv:8000/intake.epub"}, headers={"Host": "request.example.test", "X-Intake-Token": "wrong"}); check(st == 401, "/intake bypasses the gate and still enforces its own token", str(st))
st, h, b = g.get(GATE + "/", headers={"Host": "auth.example.test"}); check(st == 200 and b"<html" in b.lower(), "Authelia portal is served", str(st))
st, h, b = g.post(GATE + "/api/firstfactor", json_body={"username": "alice", "password": "alice-authelia-pw1", "targetURL": "https://request.example.test/"}, headers={"Host": "auth.example.test", "X-Forwarded-Proto": "https"})
check(st == 200 and b'"OK"' in b, "Authelia accepts the user written by the installer's authelia_add_user (argon2)", f"status {st} {b[:120]!r}")
st, h, b = Session().post(GATE + "/api/firstfactor", json_body={"username": "alice", "password": "wrong", "targetURL": "https://request.example.test/"}, headers={"Host": "auth.example.test", "X-Forwarded-Proto": "https"})
check(st in (401, 403), "Authelia rejects a wrong password", str(st))

print("== 12b. One login behind the gate (L05), and no side doors (L01)")
GATE_SECRET = "e2e-gate-secret-0123456789abcdef0123456789abcdef"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def authelia_cookie(user, pw):
    st, h, b = Session().post(GATE + "/api/firstfactor", json_body={"username": user, "password": pw, "targetURL": "https://request.example.test/"},
                              headers={"Host": "auth.example.test", "X-Forwarded-Proto": "https"})
    m = re.search(r"authelia_session=([^;]+)", h.get("Set-Cookie", ""))
    return (st, m.group(1) if m else None)
st, ac = authelia_cookie("alice", "alice-authelia-pw1")
check(st == 200 and ac, "signed in to Authelia as alice", str(st))
gh = lambda host: {"Host": host, "Cookie": f"authelia_session={ac}", "X-Forwarded-Proto": "https", **BROWSER}
st, h, b = Session().get(GATE + "/status", headers=gh("request.example.test"))
check(st == 200 and b"alice" in b and b'name="password"' not in b, "the portal behind the gate: signed in as alice with NO second login", f"status {st} {b[:120]!r}")
st, h, b = Session().get(GATE + "/", headers=gh("books.example.test"))
check(st == 200 and b"alice" in b and b'id="password"' not in b, "Calibre-Web behind the gate: signed in as alice with NO second login", f"status {st}")
# the header is worthless without the gate: direct, with a guessed secret, or smuggled through a bypass
st, h, b = Session().get(PORTAL + "/status", headers={"Remote-User": "admin"})
check(st in (302, 303) and "/login" in h.get("Location", ""), "the portal ignores Remote-User that did not come through the gate", str(st))
st, h, b = Session().get(PORTAL + "/status", headers={"Remote-User": "admin", "X-Bookstack-Gate": "guess"})
check(st in (302, 303), "...and a guessed gate secret", str(st))
st, h, b = Session().get(GATE + "/opds", headers={"Host": "books.example.test", "Remote-User": "alice"})
check(st == 401, "a Remote-User header smuggled through a bypassed path (/opds) is stripped: Calibre-Web still asks for the password", str(st))
st, h, b = Session().get(GATE + "/status", headers={"Host": "request.example.test", "Remote-User": "admin", "X-Bookstack-Gate": GATE_SECRET, **BROWSER})
check(st == 302 and "auth.example.test" in h.get("Location", ""), "even the right secret sent by a client does not get past the gate", str(st))
# L01: Shelfmark (third-party, internet-facing) cannot open a connection to anything but its own network.
# (Not the portal: in production it is on the host network, on no bridge at all; only this harness
# joins it to Shelfmark's network so it can call shelfmark:8084 by name.)
probe = ("import socket,sys\nbad=[]\nfor h,p in (('calibre-web',8083),('audiobookshelf',80),('authelia',9091),('greenmail',3025)):\n"
         "    try:\n        socket.create_connection((h,p),3).close(); bad.append(h)\n    except OSError: pass\nprint(','.join(bad) or 'none')")
r = subprocess.run(["docker", "exec", "shelfmark", "sh", "-c", 'command -v python3 >/dev/null && exec python3 -c "$0" || exec /app/.venv/bin/python -c "$0"', probe], capture_output=True, text=True)
check(r.stdout.strip() == "none", "Shelfmark reaches none of Calibre-Web, Audiobookshelf, Authelia or the mail server (own network, L01)", r.stdout.strip() or r.stderr[:160])
r = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.CapDrop}} {{.HostConfig.CapAdd}}", "calibre-web", "shelfmark", "audiobookshelf"], capture_output=True, text=True)
check(r.returncode == 0 and r.stdout.count("[ALL]") == 3, "Calibre-Web, Shelfmark and Audiobookshelf run with capabilities dropped (and still pass every journey above)", r.stdout.strip())
# password sync: carol was created on /admin and changed her password in the portal (section 11)
def gate_sync():
    return subprocess.run(["python3", f"{REPO_DIR}/scripts/gate-sync.py"], env={**os.environ, "STACK_DIR": STACK}, capture_output=True, text=True, timeout=120)
def authelia_ok(user, pw, secs=20):
    return wait(lambda: authelia_cookie(user, pw)[0] == 200, secs, 2)
r = gate_sync()
check(r.returncode == 0 and "carol: ok (gate login created)" in r.stdout, "gate-sync created carol's gate login from the /admin creation (e-mail kept across her password change)", (r.stdout + r.stderr)[-200:])
check(authelia_ok("carol", "carol-new-pw-e2e"), "Authelia accepts carol with the password she set in the portal (no restart: it watches its file)")
pc3, _, _ = portal_login("carol", "carol-new-pw-e2e")
portal_post(pc3, "/devices", "/devices", {"action": "password", "current": "carol-new-pw-e2e", "new": "carol-third-pw-e2e", "repeat": "carol-third-pw-e2e"})
r = gate_sync()
check("carol: ok (password updated)" in r.stdout, "a later portal password change is carried to the gate", (r.stdout + r.stderr)[-200:])
check(authelia_ok("carol", "carol-third-pw-e2e"), "Authelia accepts the new password")
check(authelia_cookie("carol", "carol-new-pw-e2e")[0] in (401, 403), "and no longer the old one")
r = subprocess.run(["docker", "exec", "librarian", "python", "-m", "admin_cli", "gate", "pending"], capture_output=True, text=True)
check('"rows": []' in r.stdout, "the queue is empty afterwards (no hash left behind)", r.stdout[-160:])

print("== 13. Audiobookshelf: bootstrapped by the installer, accounts by the Users menu, tagging by the worker")
def absctl(*args):
    return subprocess.run(["docker", "exec", "-i", "librarian", "python", "-m", "abs", *args], capture_output=True, text=True)
a = Session()
st, h, b = a.get(ABS + "/status"); check((jload(b) or {}).get("isInit") is True, "ABS was initialised by `python -m abs init` (stack-test)")
st, h, b = a.post(ABS + "/login", json_body={"username": "root", "password": "rootpass-e2e1"}); root = (jload(b) or {}).get("user") or {}
root_tok = root.get("token") or root.get("accessToken"); check(bool(root_tok), "ABS root login with the password given to abs init", str(st))
r = absctl("status"); check(r.returncode == 0 and '"isInit": true' in r.stdout.replace(" ", " "), "portal container reaches ABS with ABS_TOKEN", r.stderr[:100])
# "created" or "updated": `cwa add-user` provisions the ABS account itself now, so by the time
# this runs alice usually already has one. Both outcomes prove Users -> Add reaches ABS; the
# next line is the one that pins the idempotent/re-align behaviour.
r = absctl("ensure-user", "alice", "--password", ALICE_PW)
check(r.returncode == 0 and ('"created"' in r.stdout or '"updated"' in r.stdout), "abs ensure-user alice (what Users -> Add runs)", r.stderr[:120] or r.stdout[:120])
r = absctl("ensure-user", "bob", "--password", BOB_PW); check(r.returncode == 0, "abs ensure-user bob", r.stderr[:120])
r = absctl("ensure-user", "alice"); check(r.returncode == 0 and '"updated"' in r.stdout, "ensure-user is idempotent / re-aligns (Users -> Repair)", r.stdout[:120])
r = absctl("list-users"); ul = jload(r.stdout) or []
iso = {u["username"]: u["isolated"] for u in ul if u["type"] == "user"}
check(set(iso) >= {"alice", "bob", "carol"} and all(iso.values()), "every ABS user (incl. carol from the admin page) is tag-restricted to owner:<user>", str(iso))
if root_tok:
    st, h, b = a.get(ABS + "/api/libraries", headers=bearer(root_tok)); libs = (jload(b) or {}).get("libraries") or []
    lib_id = next((l["id"] for l in libs if any(f.get("fullPath") == "/audiobooks" for f in l.get("folders", []))), None)
    check(bool(lib_id) and len(libs) == 1, "exactly one ABS library, pointing at /audiobooks", str([(l.get("name"), l.get("folders")) for l in libs]))
    mp3 = f"{STACK}/testfiles/silence.mp3"
    if os.path.exists(mp3) and lib_id:
        zpath = f"{STACK}/e2e_audio.zip"
        with zipfile.ZipFile(zpath, "w") as z: z.write(mp3, "01 - Chapter One.mp3")
        st, h, b = p.get(PORTAL + "/upload"); tok = csrf(b)
        st, h, b = p.post(PORTAL + "/upload", {"csrf": tok}, files={"file": ("e2e audiobook.zip", open(zpath, "rb").read())}); check(st == 302, "audiobook zip uploaded via portal", str(st))
        folder = wait(lambda: next((d for d in os.listdir(f"{STACK}/library/audiobooks") if d.startswith("alice - ")), None), 60, 3)
        check(bool(folder), "worker placed the audiobook as '<user> - <title>' in the ABS library folder", str(os.listdir(f"{STACK}/library/audiobooks")))
        def tagged_item():
            st, h, b = a.get(ABS + f"/api/libraries/{lib_id}/items", headers=bearer(root_tok)); res = (jload(b) or {}).get("results") or []
            it = next((it for it in res if folder and folder in (it.get("relPath") or it.get("path") or "")), None)
            return it if it and "owner:alice" in ((it.get("media") or {}).get("tags") or []) else None
        item = wait(tagged_item, 150, 5)
        check(bool(item), "worker scanned AND tagged the audiobook owner:alice in ABS automatically (no admin action)")
        check(wait(lambda: b"tagged owner:alice in ABS" in pa.get(PORTAL + "/status")[2] or None, 90, 3) is not None, "admin request detail records the automatic ABS tagging (status tagging -> done)")
        st, h, b = p.get(PORTAL + "/status")
        check(b"added to your audiobooks" in b or b"in your audiobooks" in b, "...while alice reads plain language, with no owner: tag or container path", b[b.find(b"E2E Audio"):][:200])
        if item:
            def visible_to(name, pw):
                s = Session(); st, h, b = s.post(ABS + "/login", json_body={"username": name, "password": pw}); u = (jload(b) or {}).get("user") or {}
                t = u.get("token") or u.get("accessToken")
                st, h, b = s.get(ABS + f"/api/libraries/{lib_id}/items", headers=bearer(t)); return [it["id"] for it in ((jload(b) or {}).get("results") or [])]
            check(item["id"] in visible_to("alice", ALICE_PW), "alice sees her audiobook in ABS")
            check(item["id"] not in visible_to("bob", BOB_PW), "bob does not see alice's audiobook in ABS (tag restriction)")
        r = absctl("passwd", "alice", "--password", "alice-new-abs-pw1"); check(r.returncode == 0, "abs passwd (Users -> Reset password)")
        st, h, b = Session().post(ABS + "/login", json_body={"username": "alice", "password": "alice-new-abs-pw1"}); check(st == 200, "ABS accepts the new password")
        r = absctl("remove-user", "bob"); check(r.returncode == 0 and '"removed": true' in r.stdout, "abs remove-user (Users -> Remove)")
        st, h, b = Session().post(ABS + "/login", json_body={"username": "bob", "password": BOB_PW}); check(st in (401, 403), "removed ABS user can no longer log in", str(st))
    else:
        skip("audiobook journey", "no test mp3 or library")

print("== 13b. One login for Shelfmark (header) and Audiobookshelf (OpenID Connect) behind the gate (L05)")
import ssl
NOVERIFY = ssl.create_default_context(); NOVERIFY.check_hostname = False; NOVERIFY.verify_mode = ssl.CERT_NONE
class Raw:
    """No redirects, cookies by hand: the flow crosses hosts (ABS, Authelia) like a browser does."""
    def __init__(self): self.c = {}
    def get(self, url, headers=None, tls=False):
        h = dict(headers or {})
        if self.c: h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.c.items())
        op = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=NOVERIFY))
        try:
            r = op.open(urllib.request.Request(url, headers=h), timeout=60); st, hd, b = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            st, hd, b = e.code, e.headers, e.read()
        for sc in hd.get_all("Set-Cookie") or []:
            k, _, v = sc.split(";", 1)[0].partition("=")
            if v: self.c[k.strip()] = v.strip()
        return st, hd, b
def env_set(k, v):
    subprocess.run(["bash", "-c", f'source "{REPO_DIR}/bookstack.sh"; envset {k} {v}'], env={**os.environ, "STACK_DIR": STACK, "BOOKSTACK_LIB": "1"}, capture_output=True)
def recreate(*svcs):
    return subprocess.run(["docker", "compose", "-p", "bookstack-e2e", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml", "up", "-d", *svcs],
                          cwd=STACK, capture_output=True, text=True)
def groups_sync():
    return subprocess.run(["bash", "-c", f'source "{REPO_DIR}/bookstack.sh"; authelia_sync_admin_groups'], env={**os.environ, "STACK_DIR": STACK, "BOOKSTACK_LIB": "1"}, capture_output=True, text=True)
r = groups_sync()
ub = open(f"{STACK}/authelia/users_database.yml").read()
check(r.returncode == 0 and re.search(r"  admin:\n(?:    .*\n)*?      - admins", ub) and not re.search(r"  alice:\n(?:    .*\n)*?      - admins", ub),
      "Authelia's admins group mirrors Calibre-Web's admins (admin in, alice out)", (r.stderr or "")[-160:])
# --- Shelfmark: header login behind the gate
env_set("SHELFMARK_AUTH_METHOD", "proxy"); r = recreate("shelfmark", "librarian")
check(wait(lambda: b'"proxy"' in Session().get(SHELF + "/api/auth/check")[2], 120, 3), "Shelfmark restarted in proxy mode (healthcheck follows the mode)", r.stderr[-160:])
wait(lambda: Session().get(PORTAL + "/healthz")[0] == 200, 60, 2)
_, ac_alice = authelia_cookie("alice", "alice-authelia-pw1"); _, ac_admin = authelia_cookie("admin", "admin-authelia-pw1")
sh = lambda ac: {"Host": "shelf.example.test", "Cookie": f"authelia_session={ac}", "X-Forwarded-Proto": "https", **BROWSER}
st, h, b = Session().get(GATE + "/api/auth/check", headers=sh(ac_alice)); j = jload(b) or {}
check(st == 200 and j.get("authenticated") and j.get("username") == "alice" and not j.get("is_admin"), "Shelfmark behind the gate: alice signed in with NO second login, not an admin", f"{st} {b[:160]!r}")
st, h, b = Session().get(GATE + "/api/auth/check", headers=sh(ac_admin)); j = jload(b) or {}
check(st == 200 and j.get("username") == "admin" and j.get("is_admin"), "...and the admin is an admin (from the admins group)", f"{st} {b[:160]!r}")
st, h, b = Session().get(GATE + "/api/settings", headers={"Host": "shelf.example.test", "Remote-User": "admin", "Remote-Groups": "admins", **BROWSER})
check(st == 302 and "auth.example.test" in h.get("Location", ""), "headers a client sends do not get past the gate", str(st))
st, h, b = Session().get(SHELF + "/api/settings")
check(st == 401, "Shelfmark refuses a request with no gate identity (401)", str(st))
r = subprocess.run(["docker", "exec", "librarian", "python", "-c", "import shelfmark_api, json; print(json.dumps(shelfmark_api.pending(force=True), default=str)[:200])"], capture_output=True, text=True)
check(r.returncode == 0 and "refused" not in r.stdout + r.stderr, "the portal's approval queue still reads Shelfmark (service identity by header)", (r.stdout + r.stderr)[-200:])
# --- and back: the ordinary Calibre-Web login works again for the same accounts
env_set("SHELFMARK_AUTH_METHOD", "cwa"); r = recreate("shelfmark", "librarian")
check(wait(lambda: b'"cwa"' in Session().get(SHELF + "/api/auth/check")[2], 120, 3), "Shelfmark back in cwa mode when the gate goes", r.stderr[-160:])
wait(lambda: Session().get(PORTAL + "/healthz")[0] == 200, 60, 2)
st, h, b = Session().post(SHELF + "/api/auth/login", json_body={"username": "alice", "password": ALICE_PW})
check(st == 200, "alice signs in to Shelfmark with her library password again (same account)", str(st))
# --- Audiobookshelf: OpenID Connect through Authelia
r = absctl("oidc", "on"); check(r.returncode == 0 and '"openid"' in r.stdout, "Audiobookshelf switched to sign in through Authelia (local login kept for the apps)", (r.stdout + r.stderr)[-200:])
st, h, b = Session().get(ABS + "/status"); check("openid" in json.dumps(jload(b) or {}), "ABS advertises the OpenID login", b[:160])
def abs_oidc_flow():
    ab = Raw(); ah = {"Host": "audio.example.test", "X-Forwarded-Proto": "https"}
    st, h, b = ab.get(ABS + "/auth/openid?callback=" + urllib.parse.quote("https://audio.example.test/audiobookshelf/login"), headers=ah)
    loc = h.get("Location", "")
    check(st == 302 and loc.startswith("https://auth.example.test/api/oidc/authorization?") and "redirect_uri=https%3A%2F%2Faudio.example.test%2Fauth%2Fopenid%2Fcallback" in loc,
          "ABS sends the browser to Authelia's authorization endpoint", f"{st} {loc[:160]} {b[:160]!r}")
    if not loc.startswith("https://"): return
    az = Raw(); az.c["authelia_session"] = ac_alice
    st, h, b = az.get(loc.replace("https://auth.example.test", "https://127.0.0.1:18443"), headers={"Host": "auth.example.test"})
    back = h.get("Location", "")
    check(st in (302, 303) and back.startswith("https://audio.example.test/auth/openid/callback?") and "code=" in back,
          "Authelia (already signed in at the gate) answers with a code, no consent screen", f"{st} {back[:160]} {b[:120]!r}")
    if not back.startswith("https://"): return
    st, h, b = ab.get(back.replace("https://audio.example.test", ABS), headers=ah)
    done = h.get("Location", "")
    tok = urllib.parse.parse_qs(urllib.parse.urlparse(done).query).get("accessToken", [""])[0]
    check(st == 302 and done.startswith("https://audio.example.test/audiobookshelf/login?setToken=") and tok, "ABS exchanged the code with Authelia (over TLS) and signed alice in", f"{st} {done[:120]} {b[:200]!r}")
    st, h, b = Session().get(ABS + "/api/me", headers=bearer(tok)); me = jload(b) or {}
    check(st == 200 and me.get("username") == "alice" and me.get("type") == "user" and me.get("itemTagsSelected") == ["owner:alice"],
          "it is alice's EXISTING account: same type, still restricted to owner:alice", f"{st} {json.dumps({k: me.get(k) for k in ('username', 'type', 'itemTagsSelected')})}")
try:
    abs_oidc_flow()
except Exception as e:
    check(False, "the Audiobookshelf OpenID flow ran to the end", repr(e)[:200])
st, h, b = Session().post(ABS + "/login", json_body={"username": "alice", "password": "alice-new-abs-pw1"})   # set in section 13
check(st == 200, "the apps' local login still works beside it", str(st))
r = absctl("oidc", "off"); check(r.returncode == 0 and '"openid"' not in r.stdout, "and it switches off again with the gate", (r.stdout + r.stderr)[-160:])

print("== 14. Health endpoints")
st, h, b = Session().get(PORTAL + "/healthz"); check(st == 200 and b == b"ok", "portal /healthz answers plain 'ok' to non-loopback callers")
r = subprocess.run(["docker", "exec", "-i", "librarian", "python", "-c", "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8090/healthz').read().decode())"], capture_output=True, text=True)
hj = jload(r.stdout) or {}
check(hj.get("ok") is True and all(v is not None and v < 120 for v in (hj.get("heartbeats") or {}).values()), "inside the box /healthz reports JSON with fresh heartbeats", r.stdout[:200])
check(bool(hj.get("version")) and "cwa" in hj, "/healthz detail reports the portal build version and whether Calibre-Web answers", json.dumps({k: hj.get(k) for k in ("version", "cwa")})[:160])
# The VALUE, not just the key: with CWA_URL unset the probe reported the library down for every
# run and the whole suite still went green, so the admin dashboard's headline line was untested.
check(hj.get("cwa") == "ok", "the Calibre-Web liveness probe reports 'ok' against a live CWA", repr(hj.get("cwa")))
st, h, b = Session().get(ABS + "/healthcheck"); check(st == 200, "audiobookshelf /healthcheck", str(st))

print("== 15. v5: metadata-first search, conversion, cover fill, Shelfmark approvals (real services)")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def host_job():
    return subprocess.run(["bash", f"{REPO}/scripts/metadata-push.sh"], capture_output=True, text=True,
                          env=dict(os.environ, STACK_DIR=STACK), timeout=900)
def calibre(sql, *a):
    c = sqlite3.connect(f"file:{STACK}/library/books/metadata.db?mode=ro", uri=True); r = c.execute(sql, a).fetchall(); c.close(); return r
st, h, b = p.get(PORTAL + "/?q=pride+and+prejudice")
if b"unavailable right now" in b:
    skip("metadata-first search", "Open Library did not answer from this machine")
else:
    check(b'href="/work/OL' in b and b"free ebook" in b, "search finds BOOKS (Open Library works) with availability badges")
if book_id:
    fmts0 = {f for (f,) in calibre("SELECT format FROM data WHERE book=?", book_id)}
    target = next(f for f in ("AZW3", "MOBI", "PDF", "RTF") if f not in fmts0)
    portal_post(p, f"/book/{book_id}/convert", f"/book/{book_id}", {"format": target.lower()})
    r = subprocess.run(["docker", "exec", "librarian", "python", "-c", f"""
import db, worker
rid = [r for r in db.rows_by_status(('done',), limit=300) if (r['title'] or '').lower().startswith('e2e portal book')][0]['id']
db.meta_store({{'title':'E2E Portal Book','authors':[{{'name':'Test Harness'}}],'_providers':['openlibrary'],
  'description':'A book made by the end-to-end test.','cover_url':'https://covers.openlibrary.org/b/id/14348537-L.jpg',
  'identifiers':[{{'kind':'goodreads_work','value':'e2e-cover-{book_id}'}}]}}, rid=rid, owner='alice')
db.link_calibre(rid, {book_id}, 'alice'); print(worker.queue_device_pushes())"""], capture_output=True, text=True)
    job = host_job()
    fmts1 = {f for (f,) in calibre("SELECT format FROM data WHERE book=?", book_id)}
    check(target in fmts1, f"Convert to {target}: Calibre's ebook-convert made it and added it to the same book", job.stdout[-200:])
    if calibre("SELECT has_cover FROM books WHERE id=?", book_id)[0][0] != 1 and \
            Session().get("https://covers.openlibrary.org/b/id/14348537-L.jpg")[0] not in (200, 302):
        skip("cover fill", "Open Library's cover service did not answer from this machine")
    else:
        check(calibre("SELECT has_cover FROM books WHERE id=?", book_id)[0][0] == 1, "a missing cover was fetched from the provider's image host and set in Calibre", job.stdout[-200:])
    check(bool(calibre("SELECT text FROM comments WHERE book=?", book_id)), "a missing description was filled in")
    check([t for (t,) in calibre("SELECT t.name FROM tags t JOIN books_tags_link l ON l.tag=t.id WHERE l.book=? AND t.name LIKE 'owner:%'", book_id)] == ["owner:alice"],
          "the owner tag is exactly as it was after all of it")
st, h, b = ss.post(SHELF + "/api/requests", json_body={
    "book_data": {"title": "The Time Machine", "author": "H. G. Wells", "provider": "openlibrary", "provider_id": "OL52267W"},
    "release_data": {"source": "direct_download", "source_id": "0" * 32, "title": "The Time Machine", "format": "epub"},
    "context": {"source": "direct_download", "content_type": "ebook", "request_level": "release"}})
check(st == 201, "Shelfmark obeys the portal's approval rule: a reader's download becomes a pending request (L16)", f"{st} {b[:120]!r}")
st, h, b = pa.get(PORTAL + "/status")
m = re.search(rb'action="/shelfmark/(\d+)/deny"', b)
check(b"Pending approval in Shelfmark" in b and b"The Time Machine" in b and bool(m), "it appears on the portal's Pending card")
if m:
    portal_post(pa, f"/shelfmark/{m.group(1).decode()}/deny", "/status", {"reason": "e2e: denied from the portal"})
    st, h, b = ss.get(SHELF + "/api/requests")
    check(b'"status":"rejected"' in b.replace(b" ", b"") and b"denied from the portal" in b, "denied from the portal, and the reader sees it (with the reason) in Shelfmark")

print(f"\nE2E RESULT: {fails} failed")
sys.exit(fails)
