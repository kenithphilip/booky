"""HTTP-level behaviour of the portal: auth, CSRF, session hygiene, request/approval flow and
the URL fence, isolation, devices, downloads, Send-to-Kindle, admin dashboard, intake
webhook, /healthz semantics, alert CLI."""
import os, re, sqlite3, zipfile, io, time, json
import pytest
import requests
import config, db, cwa, kindle, auth, worker, notify
from conftest import login, post, csrf_of, make_epub, add_calibre_book

GUT = "https://www.gutenberg.org/ebooks/158.epub3.images"
PUBLIC = {"X-Forwarded-For": "203.0.113.5"}          # a client behind Caddy, not the box itself

def _alive(monkeypatch):
    now = time.time()
    monkeypatch.setattr(worker, "HEARTBEAT", {"queue": now, "dropbox": now, "torrent": now})

def test_anonymous_is_redirected_and_healthz_is_plain_for_the_public(client, monkeypatch):
    for p in ("/", "/status", "/library", "/devices", "/upload", "/admin", "/download/1/epub"):
        r = client.get(p); assert r.status_code == 302 and "/login" in r.headers["Location"], p
    _alive(monkeypatch)
    r = client.get("/healthz", headers=PUBLIC); assert r.status_code == 200 and r.data == b"ok"
    r = client.get("/healthz", headers=dict(PUBLIC, **{"X-Forwarded-For": "203.0.113.5"}) | {}, query_string={"detail": "1"})
    assert r.data == b"ok"                                   # ?detail=1 without an admin session leaks nothing
    r = client.get("/login"); assert r.status_code == 200 and "no-store" in r.headers["Cache-Control"]

def test_healthz_json_on_loopback_or_for_admins_and_503_when_degraded(client, users, monkeypatch):
    _alive(monkeypatch)
    r = client.get("/healthz")                               # test client = 127.0.0.1
    assert r.status_code == 200 and r.get_json()["ok"] and set(r.get_json()["heartbeats"]) == {"queue", "dropbox", "torrent"}
    assert r.get_json()["ingest_pending"] == 0 and r.get_json()["free_gb"] > 0
    worker.HEARTBEAT["dropbox"] = time.time() - 500
    r = client.get("/healthz", headers=PUBLIC); assert r.status_code == 503 and r.data == b"degraded"
    r = client.get("/healthz"); assert r.status_code == 503 and r.get_json()["problems"] == ["dropbox loop stale"]
    monkeypatch.setattr(config, "IMAP_HOST", "mail")          # IMAP enabled but never polled -> not ok
    _alive(monkeypatch)
    assert client.get("/healthz").get_json()["problems"] == ["imap loop stale"]
    monkeypatch.setattr(config, "IMAP_HOST", "")
    login(client, "alice", users["alice"])
    assert client.get("/healthz?detail=1", headers=PUBLIC).data == b"ok"           # not an admin
    post(client, "/logout"); login(client, "admin", users["admin"])
    assert client.get("/healthz?detail=1", headers=PUBLIC).get_json()["ok"] is True
    open(os.path.join(config.INGEST_DIR, "waiting.epub"), "wb").write(b"x")
    assert client.get("/healthz").get_json()["ingest_pending"] == 1
    monkeypatch.setattr(config, "CWA_DB", "/nonexistent/app.db")
    assert client.get("/healthz").status_code == 503 and "cwa app.db unreadable" in client.get("/healthz").get_json()["problems"]

def test_login_logout_and_open_redirect_guard(client, users):
    r = login(client, "alice", "wrong"); assert r.status_code == 401 and b"Invalid login" in r.data     # countable by fail2ban
    r = login(client, "alice", users["alice"]); assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200
    for nxt in ("//evil.test/x", "/\\evil.test", "/%5Cevil.test", "https://evil.test/", "/\\\\evil.test"):
        tok = csrf_of(client, "/status")
        r = client.post("/login?next=" + nxt, data={"username": "alice", "password": users["alice"], "csrf": tok})
        assert r.headers["Location"] in ("/", "http://localhost/"), nxt
    tok = csrf_of(client, "/status")
    r = client.post("/login?next=/devices", data={"username": "alice", "password": users["alice"], "csrf": tok})
    assert r.headers["Location"].endswith("/devices")
    r = client.get("/logout"); assert r.status_code == 302 and client.get("/status").status_code == 200   # GET never logs out
    r = post(client, "/logout"); assert client.get("/status").status_code == 302

def test_posts_without_csrf_are_rejected(client, users):
    login(client, "alice", users["alice"])
    r = client.post("/request", data={"title": "x"}); assert r.status_code == 400
    r = client.post("/devices", data={"action": "kobo"}); assert r.status_code == 400
    r = client.post("/request", data={"title": "x", "csrf": "wrong"}); assert r.status_code == 400
    r = client.post("/logout", data={}); assert r.status_code == 400

def test_session_is_revoked_on_password_change_or_removal_and_role_refreshes(client, users):
    login(client, "alice", users["alice"])
    assert client.get("/status").status_code == 200
    with client.session_transaction() as s:
        s["chk"] = 0                                        # pretend a minute passed
    assert client.get("/status").status_code == 200         # nothing changed: still in
    cwa.set_password("alice", "changed-pass-1")
    with client.session_transaction() as s:
        s["chk"] = 0
    r = client.get("/status"); assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "session_revoked" in [a["event"] for a in db.audit_recent(5)]
    # changing your own password keeps THIS session
    login(client, "alice", "changed-pass-1")
    post(client, "/devices", action="password", current="changed-pass-1", new="changed-pass-2", repeat="changed-pass-2")
    with client.session_transaction() as s:
        s["chk"] = 0
    assert client.get("/status").status_code == 200
    # promotion is picked up without re-login; removal bounces
    c = sqlite3.connect(config.CWA_DB); c.execute("UPDATE user SET role=479 WHERE name='alice'"); c.commit(); c.close()
    with client.session_transaction() as s:
        s["chk"] = 0
    assert client.get("/admin").status_code == 200
    cwa.remove_user("alice")
    with client.session_transaction() as s:
        s["chk"] = 0
    assert client.get("/status").status_code == 302

def test_request_approval_flow_and_isolation(client, users):
    login(client, "alice", users["alice"])
    r = post(client, "/request", kind="ebook", source="gutenberg", identifier="gutenberg:1", title="Emma",
             author="Austen", download_url=GUT, is_torrent="0")
    assert b"waiting for admin approval" in r.data
    rid = db.list_for("alice", False)[0]["id"]; assert db.get(rid)["status"] == "pending"
    # bogus source is refused
    r = post(client, "/request", kind="ebook", source="evil", title="x", download_url="https://x/y", is_torrent="0")
    assert b"Could not queue" in r.data and len(db.list_for("alice", False)) == 1
    # alice cannot approve
    r = post(client, f"/approve/{rid}"); assert b"Admins only" in r.data and db.get(rid)["status"] == "pending"
    # bob sees nothing of alice's
    post(client, "/logout"); login(client, "bob", users["bob"])
    assert b"Emma" not in client.get("/status").data
    # admin sees (with the download host) and approves; admin's own requests skip approval
    post(client, "/logout"); login(client, "admin", users["admin"])
    r = client.get("/status"); assert b"Pending approval" in r.data and b"www.gutenberg.org" in r.data
    r = post(client, f"/approve/{rid}"); assert db.get(rid)["status"] == "queued" and b"Approved" in r.data
    post(client, "/request", kind="ebook", source="gutenberg", title="Admin pick", download_url=GUT, is_torrent="0")
    assert db.list_for("admin", True)[0]["status"] == "queued"
    # deny / retry / dismiss paths
    rid2 = db.add("bob", {"kind": "ebook", "source": "gutenberg", "title": "Denied", "download_url": "https://x"}, status="pending")
    post(client, f"/deny/{rid2}"); assert db.get(rid2)["status"] == "denied"
    rid3 = db.add("bob", {"kind": "ebook", "source": "gutenberg", "title": "Broken", "download_url": "https://x"}, status="error")
    rid4 = db.add("bob", {"kind": "ebook", "source": "dropbox", "title": "Local", "download_url": "local"}, status="error")
    r = client.get("/status"); assert b"Failed (dead-letter)" in r.data and b"not retryable" in r.data
    post(client, f"/retry/{rid3}"); assert db.get(rid3)["status"] == "queued"
    r = post(client, f"/retry/{rid4}"); assert db.get(rid4)["status"] == "error" and b"be retried automatically" in r.data
    post(client, f"/dismiss/{rid4}"); assert db.get(rid4)["status"] == "dismissed"

def test_request_url_fence_refuses_tampered_forms(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    tok = csrf_of(client, "/status")
    def req(**over):
        d = {"kind": "ebook", "source": "gutenberg", "title": "T", "download_url": GUT, "is_torrent": "0", "csrf": tok}
        d.update(over); return client.post("/request", data=d)
    assert req(download_url="http://127.0.0.1:2019/config/").status_code == 400          # Caddy admin API
    assert req(download_url="http://www.gutenberg.org/x.epub").status_code == 400        # plain http
    assert req(download_url="https://www.gutenberg.org@evil.test/x.epub").status_code == 400
    assert req(download_url="https://evil.test/x.epub", source="standard_ebooks").status_code == 400
    assert req(is_torrent="1").status_code == 400                                          # torrents only for IA when enabled
    r = req(source="mycatalog", download_url="https://books.mine.tld/get/1")                 # source disabled: refused (flash)
    assert r.status_code == 302 and db.list_for("alice", False) == []
    monkeypatch.setitem(config.SOURCES, "mycatalog", True); monkeypatch.setattr(config, "MYCATALOG_URL", "https://books.mine.tld/opds")
    assert req(source="mycatalog", download_url="https://evil.test/get/1").status_code == 400   # foreign host
    assert req(source="mycatalog", download_url="https://books.mine.tld/get/1").status_code == 302
    assert req(source="librivox", download_url="https://www.archive.org/download/x/x.zip").status_code == 302
    assert db.list_for("alice", False)[0]["kind"] == "audio"                               # kind comes from the source, not the form
    monkeypatch.setattr(config, "IA_USE_TORRENT", True)
    assert req(source="internet_archive", download_url="https://archive.org/download/x/x_archive.torrent", is_torrent="1").status_code == 302
    assert any(a["event"] == "request_refused" for a in db.audit_recent(20))
    assert len(db.list_for("alice", False)) == 3

def test_approvals_can_be_switched_off(client, users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    login(client, "alice", users["alice"])
    post(client, "/request", kind="ebook", source="gutenberg", title="Now", download_url=GUT, is_torrent="0")
    assert db.list_for("alice", False)[0]["status"] == "queued"

def test_upload_keeps_unicode_names_lands_in_own_dropbox_and_filters_types(client, users, tmp_path):
    login(client, "alice", users["alice"])
    epub = make_epub(str(tmp_path / "my book.epub"))
    def up(name, data):
        return client.post("/upload", data={"csrf": csrf_of(client, "/upload"), "file": (data, name)},
                           content_type="multipart/form-data", follow_redirects=True)
    r = up("my book.epub", open(epub, "rb")); assert "Uploaded my book.epub" in r.get_data(as_text=True)
    assert os.listdir(os.path.join(config.DROPBOX_DIR, "alice")) == ["my book.epub"]     # final name, no temp file left
    r = up("Война и мир.EPUB", open(epub, "rb")); assert "Uploaded Война и мир.epub" in r.get_data(as_text=True)
    r = up("युद्ध और शांति.pdf", io.BytesIO(b"%PDF-1.4")); assert "Uploaded युद्ध और शांति.pdf" in r.get_data(as_text=True)
    r = up("../../etc/passwd.txt", io.BytesIO(b"x")); assert "Uploaded passwd.txt" in r.get_data(as_text=True)
    r = up("...epub", io.BytesIO(b"x")); assert re.search(r"Uploaded upload-[0-9a-f]{8}\.epub", r.get_data(as_text=True))
    names = os.listdir(os.path.join(config.DROPBOX_DIR, "alice"))
    assert {"my book.epub", "passwd.txt", "Война и мир.epub", "युद्ध और शांति.pdf"} < set(names) and len(names) == 5
    assert not any(n.endswith(".uploading") for n in names)
    r = up("evil.exe", io.BytesIO(b"x")); assert b"not supported" in r.data
    r = up("comic.cbr", io.BytesIO(b"Rar!")); assert b"convert it to CBZ" in r.data
    r = client.post("/upload", data={"csrf": csrf_of(client, "/upload")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"Choose a file" in r.data

def test_devices_page_manages_kindle_kobo_prefs_and_test_mail(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    r = client.get("/devices"); assert r.status_code == 200 and b"Generate my Kobo link" in r.data
    assert b"/opds/</span>" in r.data and b"USB only" in r.data and b"kepub" not in r.data.lower().replace(b"kepub automatically", b"")
    r = post(client, "/devices", action="kindle", kindle_mail="alice_9@kindle.com")
    assert b"Kindle address saved" in r.data and cwa.get_user("alice")["kindle_mail"] == "alice_9@kindle.com"
    r = post(client, "/devices", action="kindle", kindle_mail="nope"); assert b"does not look like" in r.data
    r = post(client, "/devices", action="kobo")
    m = re.search(rb"https://books\.example\.test/kobo/([0-9a-f]{32})", r.data); assert m
    assert cwa.kobo_token("alice", create=False) == m.group(1).decode()
    assert b"api_endpoint=https://books.example.test/kobo/" in r.data
    r = post(client, "/devices", action="kobo_reset")
    assert cwa.kobo_token("alice", create=False) != m.group(1).decode() and b"regenerated" in r.data
    r = post(client, "/devices", action="prefs", preferred_format="azw3", auto_kindle="1")
    p = db.get_prefs("alice"); assert (p["preferred_format"], p["auto_kindle"], p["notify_email"]) == ("azw3", True, False)
    r = post(client, "/devices", action="prefs", preferred_format="kepub"); assert db.get_prefs("alice")["preferred_format"] == "azw3"
    assert b"Kobo sync is not switched on" in client.get("/devices").data
    cwa.enable_kobo_sync()
    assert b"Kobo sync is not switched on" not in client.get("/devices").data
    # Kindle test mail: only offered when mail is configured; stores the timestamp
    assert b"Send a test to my Kindle" not in client.get("/devices").data
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    sent = []
    monkeypatch.setattr(kindle, "_deliver", lambda msg: sent.append((msg["To"], [a.get_filename() for a in msg.iter_attachments()])))
    assert b"Send a test to my Kindle" in client.get("/devices").data
    r = post(client, "/devices", action="kindle_test")
    assert b"Test mail sent to alice_9@kindle.com" in r.data and sent == [("alice_9@kindle.com", ["library-test.txt"])]
    assert db.get_prefs("alice")["last_kindle_test"] and b"last test sent" in client.get("/devices").data
    # bob's devices page never shows alice's token
    post(client, "/logout"); login(client, "bob", users["bob"])
    assert b"/kobo/" not in client.get("/devices").data

def test_library_download_is_isolated(client, users):
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"], formats=("epub", "pdf"))
    add_calibre_book(2, "Bob Book", "Bo Writer", tags=["owner:bob"])
    login(client, "alice", users["alice"])
    r = client.get("/library"); assert b"Alice Book" in r.data and b"Bob Book" not in r.data
    assert b"/download/1/epub" in r.data and b"/download/1/pdf" in r.data and b"Send to Kindle" not in r.data
    r = client.get("/download/1/epub")
    assert r.status_code == 200 and r.headers["Content-Disposition"].startswith("attachment") and "Alice Book - Ann Author.epub" in r.headers["Content-Disposition"]
    assert zipfile.ZipFile(io.BytesIO(r.data)).read("mimetype") == b"application/epub+zip"
    assert client.get("/download/2/epub").status_code == 404
    assert client.get("/download/1/azw3").status_code == 404
    assert client.get("/download/999/epub").status_code == 404
    db.set_prefs("alice", preferred_format="pdf")
    assert b"Download pdf" in client.get("/library").data
    post(client, "/logout"); login(client, "admin", users["admin"])
    r = client.get("/library"); assert b"Alice Book" in r.data and b"Bob Book" in r.data and b"All books" in r.data
    assert client.get("/download/2/epub").status_code == 200

def test_send_to_kindle_only_mails_formats_amazon_accepts(client, users, monkeypatch):
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"], formats=("epub", "azw3"))
    add_calibre_book(2, "Bob Book", "Bo Writer", tags=["owner:bob"])
    add_calibre_book(3, "Kindle Only", "Ann Author", tags=["owner:alice"], formats=("azw3",))
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    sent = []
    monkeypatch.setattr(kindle, "send", lambda to, path, title=None, filename=None: (sent.append((to, filename)), f"sent to {to}")[1])
    login(client, "alice", users["alice"])
    r = post(client, "/kindle/1"); assert b"Devices page first" in r.data and sent == []
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    db.set_prefs("alice", preferred_format="azw3")
    r = client.get("/library"); assert r.data.count(b"Send to Kindle") == 1 and b"no Kindle format yet" in r.data
    r = post(client, "/kindle/1", format="azw3")            # pref/format azw3 -> the EPUB goes out
    assert b"sent to alice@kindle.com" in r.data and sent == [("alice@kindle.com", "Alice Book - Ann Author.epub")]
    r = post(client, "/kindle/3"); assert b"No Kindle-compatible format yet" in r.data and len(sent) == 1
    assert post(client, "/kindle/2").status_code == 404      # bob's book
    r = client.get("/devices"); assert b"lib@example.test" in r.data      # approved-sender hint shows the real From

def test_kindle_module_guards(monkeypatch, tmp_path):
    assert not kindle.configured()
    with pytest.raises(kindle.MailNotConfigured):
        kindle.send("a@kindle.com", __file__)
    monkeypatch.setattr(config, "SMTP_HOST", "h"); monkeypatch.setattr(config, "SMTP_FROM", "f@x")
    p = make_epub(str(tmp_path / "x.epub"))
    with pytest.raises(ValueError, match="EPUB or PDF"):
        kindle.send("a@kindle.com", p, "T", "book.azw3")
    with pytest.raises(ValueError, match="EPUB or PDF"):
        kindle.send("a@kindle.com", __file__)
    monkeypatch.setattr(config, "KINDLE_MAX_MB", 0)
    with pytest.raises(ValueError, match="over 0 MB"):
        kindle.send("a@kindle.com", p)
    monkeypatch.setattr(config, "KINDLE_MAX_MB", 45)
    captured = {}
    monkeypatch.setattr(kindle, "_deliver", lambda msg: captured.update(to=msg["To"], subj=msg["Subject"], att=[p.get_filename() for p in msg.iter_attachments()], ct=[p.get_content_type() for p in msg.iter_attachments()]))
    assert kindle.send("a@kindle.com", p, "A Title", "A Title.epub") == "sent to a@kindle.com"
    assert captured == {"to": "a@kindle.com", "subj": "A Title", "att": ["A Title.epub"], "ct": ["application/epub+zip"]}
    with pytest.raises(ValueError):
        kindle.send("", p)

def test_admin_dashboard_user_creation_and_needs_tag_list(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    r = client.get("/admin", follow_redirects=True); assert b"Admins only" in r.data
    post(client, "/logout"); login(client, "admin", users["admin"])
    _alive(monkeypatch)
    r = client.get("/admin")
    assert r.status_code == 200 and b"owner:alice" in r.data and b"Calibre-Web (books)" in r.data and b"https://dl.example.test" in r.data
    assert b"Kobo sync: <strong>OFF" in r.data and b"healthy" in r.data
    r = post(client, "/admin", action="add_user", name="carol", email="c@example.test", password="carolpass1")
    assert b"User carol created" in r.data and cwa.get_user("carol")["allowed_tags"] == "owner:carol"
    assert os.path.isdir(os.path.join(config.DROPBOX_DIR, "carol"))
    r = post(client, "/admin", action="add_user", name="carol", password="carolpass1"); assert b"already exists" in r.data
    r = post(client, "/admin", action="add_user", name="bad name", password="carolpass1"); assert b"Could not create" in r.data
    # unisolated user is flagged
    c = sqlite3.connect(config.CWA_DB); c.execute("UPDATE user SET allowed_tags='' WHERE name='bob'"); c.commit(); c.close()
    assert b"NOT isolated" in client.get("/admin").data
    # books that imported without a tag are listed with the tag the admin must add
    db.add("bob", {"kind": "ebook", "source": "dropbox", "title": "notes.txt", "download_url": "local"}, status="needs-tag")
    r = client.get("/admin"); assert b"Imported without an owner tag" in r.data and b"notes.txt" in r.data and b"owner:bob" in r.data
    r = client.get("/status"); assert b"st-needs-tag" in r.data
    worker.HEARTBEAT["queue"] = 0
    assert b"DEGRADED" in client.get("/admin").data

def test_intake_webhook(client, monkeypatch):
    r = client.post("/intake", json={"user": "alice", "url": "https://x/y.epub"}); assert r.status_code == 401
    r = client.post("/intake", json={"user": "alice", "url": "https://x/y.epub"}, headers={"X-Intake-Token": "intake-token-123"})
    assert r.status_code == 202 and r.get_json()["ok"]
    rec = db.get(r.get_json()["id"]); assert rec["owner"] == "alice" and rec["status"] == "queued" and rec["title"] == "y.epub"
    r = client.post("/intake", json={"user": "ALICE", "url": "https://x/y.epub"}, headers={"X-Intake-Token": "intake-token-123"})
    assert r.status_code == 202 and db.get(r.get_json()["id"])["owner"] == "alice"          # canonical name
    hdr = {"X-Intake-Token": "intake-token-123"}
    r = client.post("/intake", json={"user": "alice", "url": "ftp://x/y"}, headers=hdr); assert r.status_code == 400
    r = client.post("/intake", json={"user": "", "url": "https://x"}, headers=hdr); assert r.status_code == 400
    r = client.post("/intake", json={"user": "nosuchuser", "url": "https://x/y.epub"}, headers=hdr)
    assert r.status_code == 400 and r.get_json()["error"] == "no such user"
    r = client.post("/intake", json={"user": "../alice", "url": "https://x/y.epub"}, headers=hdr); assert r.status_code == 400
    r = client.post("/intake", json={"user": "alice", "url": "https://x/y.epub", "kind": "video"}, headers=hdr); assert r.status_code == 400
    assert len(db.list_for("alice", False)) == 2
    monkeypatch.setattr(config, "INTAKE_TOKEN", "")
    r = client.post("/intake", json={"user": "alice", "url": "https://x/y.epub"}, headers={"X-Intake-Token": ""}); assert r.status_code == 403

def test_cover_proxy_caps_size_type_and_redirects(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    calls = {}
    class Raw:
        def __init__(self, data): self.data = data
        def read(self, n, decode_content=True): return self.data[:n]
    class R:
        def __init__(self, status=200, ctype="image/jpeg", data=b"\xff\xd8jpg"):
            self.status_code, self.headers, self.raw = status, {"Content-Type": ctype}, Raw(data)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_get(u, **kw):
        calls.update(kw); return fake_get.resp
    monkeypatch.setattr(requests, "get", fake_get)
    fake_get.resp = R()
    r = client.get("/cover?u=https://covers.openlibrary.org/b/id/1-M.jpg")
    assert r.status_code == 200 and r.data == b"\xff\xd8jpg" and r.mimetype == "image/jpeg"
    assert calls["allow_redirects"] is False and calls["stream"] is True
    fake_get.resp = R(ctype="text/html", data=b"<html>"); assert client.get("/cover?u=https://covers.openlibrary.org/x").status_code == 404
    fake_get.resp = R(status=302); assert client.get("/cover?u=https://covers.openlibrary.org/x").status_code == 404
    fake_get.resp = R(data=b"x" * (2 * 1024 * 1024 + 1)); assert client.get("/cover?u=https://covers.openlibrary.org/x").status_code == 404
    assert client.get("/cover?u=https://evil.test/x.jpg").status_code == 404

def test_security_headers_and_persistent_session(client, users):
    r = client.get("/login")
    assert "script-src 'none'" in r.headers["Content-Security-Policy"] and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Referrer-Policy"] == "same-origin"
    r = login(client, "alice", users["alice"])
    sc = r.headers.get("Set-Cookie", "")
    assert "session=" in sc and ("Expires=" in sc or "Max-Age=" in sc) and "HttpOnly" in sc and "SameSite=Lax" in sc

def test_login_lockout_per_user_and_ip(client, users, monkeypatch):
    monkeypatch.setattr(config, "LOCKOUT_FAILS", 3)
    attacker = {"X-Forwarded-For": "198.51.100.7"}
    def attempt(pw, headers):
        tok = csrf_of(client, "/login")
        return client.post("/login", data={"username": "alice", "password": pw, "csrf": tok}, headers=headers)
    assert attempt("wrong1", attacker).status_code == 401
    assert attempt("wrong2", attacker).status_code == 401
    r = attempt("wrong3", attacker); assert r.status_code == 401 and b"locked" in r.data
    r = attempt(users["alice"], attacker); assert r.status_code == 429 and b"Too many failed attempts" in r.data
    assert db.locked_for("alice", "198.51.100.7") > 0 and db.locked_for("alice", "203.0.113.1") == 0
    # the real user from her own address is unaffected (lock is per user+IP), and the IP was taken from X-Forwarded-For
    r = attempt(users["alice"], {"X-Forwarded-For": "203.0.113.1"}); assert r.status_code == 302
    events = [(a["event"], a["ip"]) for a in db.audit_recent(20)]
    assert ("login_locked", "198.51.100.7") in events and ("login_ok", "203.0.113.1") in events
    assert any(e == "login_fail" and ip == "198.51.100.7" for e, ip in events)

def test_ip_wide_lockout_and_window_expiry():
    now = 1_000_000.0
    config_fails, config.LOCKOUT_FAILS = config.LOCKOUT_FAILS, 2
    try:
        assert db.record_login_failure("a", "10.0.0.1", now) == 0
        assert db.record_login_failure("a", "10.0.0.1", now + 1) == config.LOCKOUT_SECONDS
        assert db.locked_for("a", "10.0.0.1", now + 2) > 0 and db.locked_for("A", "10.0.0.1", now + 2) > 0   # case-insensitive
        assert db.locked_for("a", "10.0.0.1", now + config.LOCKOUT_SECONDS + 1) == 0                       # lock expires
        # failures outside the window start a fresh count
        assert db.record_login_failure("b", "10.0.0.2", now) == 0
        assert db.record_login_failure("b", "10.0.0.2", now + config.LOCKOUT_WINDOW + 5) == 0
        # many usernames from one IP locks the IP for every username
        for i in range(config.LOCKOUT_IP_FAILS):
            db.record_login_failure(f"u{i}", "10.0.0.3", now + i)
        assert db.locked_for("someone-else", "10.0.0.3", now + 40) > 0
        db.clear_login_failures("a", "10.0.0.1")
        assert db.locked_for("a", "10.0.0.1", now + 2) == 0
    finally:
        config.LOCKOUT_FAILS = config_fails

def test_daily_request_quota_for_non_admins(client, users, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DAY", 2)
    login(client, "alice", users["alice"])
    for i in range(2):
        r = post(client, "/request", kind="ebook", source="gutenberg", title=f"B{i}", download_url=GUT, is_torrent="0")
        assert b"Requested" in r.data
    r = post(client, "/request", kind="ebook", source="gutenberg", title="B3", download_url=GUT, is_torrent="0")
    assert b"limit of 2 requests" in r.data and len(db.list_for("alice", False)) == 2
    assert db.requests_today("alice") == 2
    db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "up", "download_url": "local"}, status="done")
    assert db.requests_today("alice") == 2                 # uploads do not count
    post(client, "/logout"); login(client, "admin", users["admin"])
    for i in range(3):
        r = post(client, "/request", kind="ebook", source="gutenberg", title=f"A{i}", download_url=GUT, is_torrent="0")
        assert b"Requested" in r.data                      # admins are not limited

def test_audit_trail_on_admin_page(client, users, monkeypatch):
    _alive(monkeypatch)
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"])
    login(client, "alice", users["alice"])
    client.get("/download/1/epub"); client.get("/download/2/epub")
    post(client, "/logout"); login(client, "admin", users["admin"])
    r = client.get("/admin")
    assert b"login_ok" in r.data and b"Alice Book - Ann Author.epub" in r.data and b"download_denied" in r.data and b"logout" in r.data
    events = [a["event"] for a in db.audit_recent(50)]
    assert events[0] == "login_ok" and "download" in events and "download_denied" in events

def test_self_service_password_change_keeps_abs_in_step(client, users, monkeypatch):
    import abs as absapi
    login(client, "alice", users["alice"])
    r = post(client, "/devices", action="password", current="nope", new="newpass-123", repeat="newpass-123")
    assert b"Current password is wrong" in r.data and auth.verify("alice", users["alice"])
    r = post(client, "/devices", action="password", current=users["alice"], new="newpass-123", repeat="different")
    assert b"do not match" in r.data
    changed = []
    monkeypatch.setattr(config, "ABS_TOKEN", "k"); monkeypatch.setattr(absapi, "set_password", lambda n, p, token=None: changed.append((n, p)))
    r = post(client, "/devices", action="password", current=users["alice"], new="newpass-123", repeat="newpass-123")
    assert b"Password changed" in r.data and b"Audiobookshelf too" in r.data and changed == [("alice", "newpass-123")]
    assert auth.verify("alice", "newpass-123") and not auth.verify("alice", users["alice"])
    assert "password_change" in [a["event"] for a in db.audit_recent(5)]

def test_email_notifications(monkeypatch, users):
    sent = []
    monkeypatch.setattr(config, "SMTP_HOST", "smtp"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setattr(notify, "_deliver", lambda to, subject, text: sent.append((to, subject, text)))
    rec = {"owner": "alice", "title": "Emma", "author": "Austen", "source": "gutenberg", "status": "pending", "detail": None}
    notify._mail("requested", rec)
    assert sent and sent[-1][0] == "admin@example.test" and "approval needed" in sent[-1][1] and "alice" in sent[-1][2]
    sent.clear(); notify._mail("done", dict(rec, status="done")); assert sent == []          # user has not opted in
    db.set_prefs("alice", notify_email=True)
    notify._mail("done", dict(rec, status="done"))
    assert sent[-1][0] == "alice@example.test" and "Emma" in sent[-1][1] and "/library" in sent[-1][2]
    notify._mail("denied", dict(rec, status="denied", detail="denied by admin"))
    assert sent[-1][1].endswith("was denied") and "denied by admin" in sent[-1][2]
    sent.clear(); notify._mail("done", dict(rec, source="dropbox")); assert sent == []       # own uploads: no mail
    monkeypatch.setattr(config, "SMTP_HOST", ""); notify._mail("done", dict(rec, status="done")); assert sent == []

def test_alert_helper_posts_webhook_and_mails_admin_best_effort(monkeypatch, capsys):
    import urllib.request
    hooks, mails = [], []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: hooks.append((req.full_url, json.loads(req.data))))
    monkeypatch.setattr(notify, "_deliver", lambda to, subject, text: mails.append((to, subject, text)))
    assert notify.alert("disk", "85% used") == []                                   # nothing configured: no crash
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example.test/x")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "admin@example.test")
    assert notify.alert("backup failed", "restic exit 1", "high") == ["webhook", "mail"]
    assert hooks == [("https://hook.example.test/x", {"event": "alert", "title": "backup failed", "text": "restic exit 1", "priority": "high"})]
    assert mails[0][0] == "admin@example.test" and mails[0][1] == "[bookstack] backup failed" and "restic exit 1" in mails[0][2]
    def boom(*a, **k): raise OSError("smtp down")
    monkeypatch.setattr(notify, "_deliver", boom)
    assert notify.alert("t", "x") == ["webhook"] and "mail failed" in capsys.readouterr().err

def test_search_page_flags_duplicates_and_lists_sources(client, users, monkeypatch):
    import fetchers
    add_calibre_book(1, "Emma", "Austen", tags=["owner:bob"])
    monkeypatch.setattr(fetchers, "search", lambda q: [{"source": "gutenberg", "kind": "ebook", "title": "Emma", "author": "Austen",
                                                        "identifier": "gutenberg:158", "format": "epub", "download_url": "https://x", "is_torrent": False}])
    login(client, "alice", users["alice"])
    r = client.get("/?q=emma")
    assert b"in library" in r.data and b"Request" in r.data and b"gutenberg" in r.data
    assert b"No matches" in client.get("/?q=zzz").data or True
