"""Regressions for the issues the persona journeys found (ids J..).

Each test names the journey it locks down. The fakes are the ones the rest of the suite uses:
a real CWA app.db and calibre metadata.db schema, a scripted Audiobookshelf, a scripted IMAP
mailbox. Nothing here talks to the network.
"""
import io, os, re, time, zipfile
import pytest
import config, db, worker, cwa, fetchers, tagger
import abs as absapi
from conftest import make_epub, make_pdf, make_cbz, add_calibre_book, login, post, csrf_of
from test_abs import FakeABS

# ---------------------------------------------------------------- helpers
def _req(owner="alice", **kw):
    r = {"kind": "ebook", "source": "gutenberg", "title": "T", "author": "A",
         "download_url": "https://x/y.epub"}
    r.update(kw)
    return db.add(owner, r, status=kw.pop("status", "queued"))


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _drop(user, name, maker=None, data=b"PK\x03\x04x"):
    d = os.path.join(config.DROPBOX_DIR, user)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if maker:
        maker(p)
    else:
        open(p, "wb").write(data)
    old = time.time() - 3600
    os.utime(p, (old, old))
    return p


def _ingested():
    return [n for n in os.listdir(config.INGEST_DIR) if not n.endswith(".part")]


# ---------------------------------------------------------------- J02
def test_j02_stale_ingest_files_are_nudged_and_the_admin_is_alerted(monkeypatch):
    """CWA's importer only reacts to inotify; a file that arrived while it was down is never
    imported. The housekeeping loop renames it out and back to re-fire the event."""
    cwa.add_user("alice", "alicepass1")
    p = os.path.join(config.INGEST_DIR, "book [alice-7].epub")
    make_epub(p)
    fresh = os.path.join(config.INGEST_DIR, "just-arrived.epub"); make_epub(fresh)
    part = os.path.join(config.INGEST_DIR, "abc.part"); open(part, "wb").write(b"x")
    old = time.time() - 600
    for f in (p, part):
        os.utime(f, (old, old))
    alerts = []
    monkeypatch.setattr(worker.notify, "alert", lambda t, x, prio="default": alerts.append((t, prio)))
    now = time.time()
    assert worker.nudge_ingest_once(now) == 1          # only the stale, non-partial file
    assert os.path.exists(p) and os.path.exists(fresh) and os.path.exists(part)
    assert worker.nudge_ingest_once(now + 1) == 0      # not more than once a minute
    os.remove(fresh)
    for i in range(1, worker.INGEST_NUDGE_ALERT + 2):
        assert worker.nudge_ingest_once(now + 61 * i) == 1
    assert alerts and alerts[0][1] == "high" and "stuck" in alerts[0][0]
    assert len(alerts) == 1                            # ... and only once per file
    assert not [n for n in os.listdir(config.INGEST_DIR) if n.endswith(".nudge")]


def test_j02_a_nudge_needs_a_live_calibre_web(monkeypatch):
    p = os.path.join(config.INGEST_DIR, "x.epub"); make_epub(p)
    os.utime(p, (time.time() - 600,) * 2)
    monkeypatch.setattr(config, "CWA_DB", os.path.join(config.INGEST_DIR, "gone.db"))
    assert worker._cwa_alive() is False and worker.nudge_ingest_once() == 0


def test_j02_done_is_only_done_when_calibre_actually_imported_it(monkeypatch):
    """'Handed to CWA' was reported as done even when the file sat in /ingest for ever, and a
    CWA import failure was invisible."""
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(worker.notify, "alert", lambda t, x, prio="default": alerts.append(t))
    rid = _req("alice", source="dropbox", download_url="local", status="done")
    db.set_status(rid, "done", "tagged owner:alice")
    stuck = os.path.join(config.INGEST_DIR, f"book [alice-{rid}].epub"); make_epub(stuck)
    later = time.time() + 1000
    assert worker.reconcile_imports(later) == 1
    r = db.get(rid)
    assert r["status"] == "importing" and "still waiting" in r["detail"]
    # CWA imported it: metadata.db knows the file
    os.remove(stuck)
    add_calibre_book(41, f"book [alice-{rid}]", "Someone", tags=("owner:alice",))
    assert worker.reconcile_imports(later + 1) == 1 and db.get(rid)["status"] == "done"
    # another one, which CWA's importer refused
    rid2 = _req("alice", source="dropbox", download_url="local", status="done")
    failed = os.path.join(config.CWA_PROCESSED_DIR, "failed")
    os.makedirs(failed, exist_ok=True)
    open(os.path.join(failed, f"other [alice-{rid2}].epub"), "wb").write(b"x")
    assert worker.reconcile_imports(later + 2) == 1
    assert db.get(rid2)["status"] == "error" and "could not import" in db.get(rid2)["detail"]
    assert alerts == ["a book failed to import"]


# ---------------------------------------------------------------- J03 / J08
def test_j03_j08_image_carries_a_build_version_and_stops_on_sigterm():
    docker = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Dockerfile")).read()
    assert "ARG BUILD_VERSION" in docker and "BUILD_VERSION=${BUILD_VERSION}" in docker
    assert docker.rstrip().endswith("app:app\"]")                    # exec form, not shell form
    assert "exec gunicorn" in docker and "--graceful-timeout 25" in docker


def test_j03_version_is_reported_on_healthz_and_admin(client, users, monkeypatch):
    monkeypatch.setattr(config, "BUILD_VERSION", "abc1234")
    monkeypatch.setattr(config, "CWA_URL", "")
    login(client, "admin", users["admin"])
    assert client.get("/healthz").get_json()["version"] == "abc1234"
    assert b"abc1234" in client.get("/admin").data


# ---------------------------------------------------------------- J04
@pytest.fixture
def abs_fake(monkeypatch):
    f = FakeABS()
    monkeypatch.setattr(absapi.requests, "request", f)
    monkeypatch.setattr(config, "ABS_TOKEN", "")
    key = absapi.bootstrap("root", "rootpass-1234")["api_key"]
    monkeypatch.setattr(config, "ABS_TOKEN", key)
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    return f


def _abs_item(fake, ident, rel):
    fake.items[ident] = {"id": ident, "libraryId": fake.libs[0]["id"], "relPath": rel,
                         "path": f"/audiobooks/{rel}", "media": {"tags": []}}


def test_j04_every_book_under_a_folder_is_tagged_not_just_the_first(abs_fake):
    """A box-set zip becomes several ABS items; only one used to get the owner tag, so the
    other books were invisible to their owner while the request said 'tagged'."""
    rid = _req("alice", kind="audio", source="dropbox", download_url="local", status="tagging")
    db.add_tag_job(rid, "alice - Box Set", "alice", now=1000.0)
    _abs_item(abs_fake, "i1", "alice - Box Set/Book One")
    assert worker.process_tag_jobs(1000.0) == 0                      # count not stable yet
    _abs_item(abs_fake, "i2", "alice - Box Set/Book Two")
    _abs_item(abs_fake, "i3", "alice - Box Set/Book Three")
    assert worker.process_tag_jobs(1100.0) == 0                      # three now: still growing
    assert worker.process_tag_jobs(1200.0) == 3                      # stable -> closed
    assert all(it["media"]["tags"] == ["owner:alice"] for it in abs_fake.items.values())
    r = db.get(rid)
    assert r["status"] == "done" and "tagged 3 items owner:alice" in r["detail"]
    assert db.pending_tag_jobs() == []


def test_j04_find_items_by_folder_returns_all_matches(abs_fake):
    _abs_item(abs_fake, "i1", "alice - Box Set/Book One")
    _abs_item(abs_fake, "i2", "alice - Box Set/Book Two")
    _abs_item(abs_fake, "i3", "bob - Other")
    assert {i["id"] for i in absapi.find_items_by_folder("alice - Box Set")} == {"i1", "i2"}
    assert absapi.find_item_by_folder("alice - Box Set")["id"] == "i1"
    assert absapi.find_items_by_folder("nothing here") == []


# ---------------------------------------------------------------- J06
def _bomb(path, member_size=200 * 1024 * 1024):
    """A small file that claims a huge member: read()+writestr would pull it into memory."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="c.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("c.opf", """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Boom</dc:title></metadata></package>""")
        z.writestr("big.bin", b"\0" * member_size)
    return path


def test_j06_a_zip_bomb_is_refused_instead_of_filling_memory(tmp_path, monkeypatch):
    p = _bomb(str(tmp_path / "bomb.epub"), member_size=4 * 1024 * 1024)
    assert os.path.getsize(p) < 100 * 1024                           # tiny on disk, megabytes inside
    monkeypatch.setattr(tagger, "MAX_ZIP_UNPACKED", 1024 * 1024)     # the real cap needs a 1 GB file
    with pytest.raises(tagger.TagError, match="cannot be tagged safely"):
        tagger.add_owner_tag(p, "owner:alice")
    assert not os.path.exists(p + ".tmp")
    monkeypatch.setattr(tagger, "MAX_ZIP_MEMBERS", 3)
    many = str(tmp_path / "many.cbz")
    with zipfile.ZipFile(many, "w") as z:
        for i in range(4):
            z.writestr(f"{i}.jpg", b"x")
    with pytest.raises(tagger.TagError, match="cannot be tagged safely"):
        tagger.add_owner_tag_cbz(many, "owner:alice")


def test_j06_members_are_streamed_and_keep_their_compression(tmp_path):
    p = make_epub(str(tmp_path / "ok.epub"))
    tagger.add_owner_tag(p, "owner:alice")
    with zipfile.ZipFile(p) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.getinfo("OEBPS/ch1.xhtml").compress_type == zipfile.ZIP_DEFLATED
        assert b"owner:alice" in z.read("OEBPS/content.opf")
    c = make_cbz(str(tmp_path / "c.cbz"))
    tagger.add_owner_tag_cbz(c, "owner:bob")
    with zipfile.ZipFile(c) as z:
        assert "owner:bob" in z.comment.decode() and len(z.namelist()) == 2


# ---------------------------------------------------------------- J09 / J15 / J36 / J40
def test_j09_needs_tag_rows_can_be_marked_resolved_and_leave_an_audit_row(client, users):
    login(client, "admin", users["admin"])
    rid = _req("alice", source="dropbox", download_url="local", status=worker.NEEDS_TAG)
    db.set_status(rid, worker.NEEDS_TAG, "needs-tag: txt cannot carry a tag")
    r = post(client, f"/dismiss/{rid}")
    assert db.get(rid)["status"] == "dismissed" and b"Dismissed: T" in r.data
    assert any(a["event"] == "dismiss" for a in db.audit_recent(10))


def test_j15_retry_and_retry_all_are_audited(client, users, monkeypatch):
    login(client, "admin", users["admin"])
    rid = _req("alice", status="error")
    post(client, f"/retry/{rid}")
    rid2 = _req("bob", status="error")
    post(client, "/retry_all")
    events = [a["event"] for a in db.audit_recent(20)]
    assert "retry" in events and "retry_all" in events
    assert db.get(rid)["status"] == "queued" and db.get(rid2)["status"] == "queued"


def test_j40_owners_can_clear_their_own_failed_rows_but_not_other_peoples(client, users):
    login(client, "alice", users["alice"])
    mine = _req("alice", status="error")
    theirs = _req("bob", status="error")
    r = post(client, f"/dismiss/{mine}")
    assert db.get(mine)["status"] == "dismissed" and b"Dismissed" in r.data
    assert client.post(f"/dismiss/{theirs}", data={"csrf": csrf_of(client, "/status")}).status_code == 404
    assert db.get(theirs)["status"] == "error"
    needs = _req("alice", status=worker.NEEDS_TAG)
    post(client, f"/dismiss/{needs}")
    assert db.get(needs)["status"] == worker.NEEDS_TAG          # only the admin resolves those


def test_j15_the_tui_user_commands_write_audit_rows(capsys):
    cwa.add_user("admin", "adminpass1", "a@example.test", admin=True)
    assert cwa._cli(["add-user", "carol", "--password", "carolpass1", "--email", "c@example.test"]) == 0
    assert cwa._cli(["passwd", "carol", "--password", "newpass-123"]) == 0
    assert cwa._cli(["kindle", "carol", "c@kindle.com"]) == 0
    assert cwa._cli(["remove-user", "carol"]) == 0
    capsys.readouterr()
    events = {a["event"]: a for a in db.audit_recent(20)}
    assert {"user_add", "password_reset", "kindle_set", "user_remove"} <= set(events)
    assert events["user_add"]["ip"] == "tui" and events["user_add"]["user"] == "carol"


# ---------------------------------------------------------------- J10 / J11 / J27
def test_j10_a_zip_of_ebooks_goes_to_the_library_not_to_audiobookshelf(tmp_path):
    cwa.add_user("alice", "alicepass1")
    z = str(tmp_path / "books.zip")
    make_epub(str(tmp_path / "one.epub"))
    with zipfile.ZipFile(z, "w") as out:
        out.write(str(tmp_path / "one.epub"), "one.epub")
    note = worker.ingest_local_file(z, "alice", rid=None)
    assert "holds 1 book(s), not audio" in note and os.listdir(config.AUDIO_DIR) == []
    assert len(_ingested()) == 1 and _ingested()[0].endswith(".epub")


def test_j27_an_archive_with_no_playable_audio_is_refused(tmp_path):
    cwa.add_user("alice", "alicepass1")
    z = _zip(str(tmp_path / "junk.zip"), {"readme.nfo": b"x", "cover.jpg": b"\xff\xd8"})
    with pytest.raises(ValueError, match="no playable audio"):
        worker.ingest_local_file(z, "alice")
    assert os.listdir(config.AUDIO_DIR) == []
    # an m4b someone named .zip is still an audiobook, not "an archive with no audio"
    m4b_named_zip = str(tmp_path / "book.zip")
    open(m4b_named_zip, "wb").write(b"\0\0\0\x20ftypM4B " + b"\0" * 64)
    worker.ingest_local_file(m4b_named_zip, "alice")
    assert os.listdir(os.path.join(config.AUDIO_DIR, "alice - book")) == ["book.m4b"]


def test_j11_the_bytes_decide_the_route_not_the_name_or_the_requested_kind(tmp_path, monkeypatch):
    cwa.add_user("alice", "alicepass1")
    # an M4B that was named .epub (mailed in, renamed): it is still an audiobook
    p = _drop("alice", "audiobook.epub", data=b"\0\0\0\x20ftypM4B " + b"\0" * 64)
    assert worker.scan_dropbox_once() == 1
    row = db.list_for("alice", False)[0]
    assert row["status"] in ("tagging", "needs-tag") and not os.path.exists(p)
    assert [d for d in os.listdir(config.AUDIO_DIR)] == ["alice - audiobook"]
    assert worker._sniff_ext(str(tmp_path / "nope")) == ""
    open(str(tmp_path / "s.bin"), "wb").write(b"ID3\x03")
    assert worker._sniff_ext(str(tmp_path / "s.bin")) == "mp3"
    open(str(tmp_path / "f.bin"), "wb").write(b"fLaCxx")
    assert worker._sniff_ext(str(tmp_path / "f.bin")) == "flac"
    assert worker._sniff_ext(make_pdf(str(tmp_path / "p.pdf"))) == "pdf"
    assert worker._sniff_ext(make_epub(str(tmp_path / "e.epub"))) == "epub"
    assert worker._sniff_ext(make_cbz(str(tmp_path / "c.cbz"))) == "cbz"
    assert worker._sniff_ext(_zip(str(tmp_path / "a.zip"), {"01.mp3": b"ID3"})) == "audio-zip"
    open(str(tmp_path / "broken.bin"), "wb").write(b"PK\x03\x04truncated")
    assert worker._sniff_ext(str(tmp_path / "broken.bin")) == "damaged-zip"


def test_j11_a_direct_m4b_url_requested_as_audio_is_placed_without_unzipping(monkeypatch):
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_download",
                        lambda url, dest, **kw: open(dest, "wb").write(b"\0\0\0\x20ftypM4B " + b"\0" * 64))
    rid = _req("alice", kind="audio", source="librivox", title="Listen", author="Reader",
               download_url="https://archive.org/x.m4b")
    worker._process(db.claim_one())
    assert db.get(rid)["status"] in ("tagging", "needs-tag")
    assert os.listdir(config.AUDIO_DIR) == ["alice - Reader - Listen"]


def test_j10_an_epub_behind_an_audio_request_is_imported_as_a_book(monkeypatch, tmp_path):
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    data = open(make_epub(str(tmp_path / "b.epub")), "rb").read()
    monkeypatch.setattr(worker, "_download", lambda url, dest, **kw: open(dest, "wb").write(data))
    rid = _req("alice", kind="audio", source="librivox", title="Book", author="A",
               download_url="https://archive.org/x.zip")
    worker._process(db.claim_one())
    assert db.get(rid)["status"] == "done" and os.listdir(config.AUDIO_DIR) == []
    assert len(_ingested()) == 1


# ---------------------------------------------------------------- J12 / J41 / J13
def test_j12_an_oversized_upload_gets_a_sentence_not_werkzeug_s_413(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    monkeypatch.setitem(client.application.config, "MAX_CONTENT_LENGTH", 32)
    r = client.post("/upload", data={"csrf": csrf_of(client, "/upload"),
                                     "file": (io.BytesIO(b"x" * 500), "big.epub")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200 and b"larger than" in r.data and b"dropbox" in r.data


def test_j41_empty_and_mistyped_uploads_are_refused_at_once(client, users):
    login(client, "alice", users["alice"])
    def up(name, data):
        return client.post("/upload", data={"csrf": csrf_of(client, "/upload"), "file": (data, name)},
                           content_type="multipart/form-data", follow_redirects=True)
    assert b"empty (0 bytes)" in up("empty.epub", io.BytesIO(b"")).data
    assert b"does not look like a EPUB" in up("text.epub", io.BytesIO(b"hello, not a zip")).data
    assert b"does not look like a PDF" in up("x.pdf", io.BytesIO(b"PK\x03\x04")).data
    assert not os.path.isdir(os.path.join(config.DROPBOX_DIR, "alice")) or \
        os.listdir(os.path.join(config.DROPBOX_DIR, "alice")) == []
    assert [a["event"] for a in db.audit_recent(5) if a["event"] == "upload_rejected"]


def test_j13_long_non_ascii_names_are_cut_by_bytes(client, users, tmp_path):
    """150 Cyrillic characters are 300 bytes: every rename failed with ENAMETOOLONG (a 500)."""
    login(client, "alice", users["alice"])
    name = "я" * 300 + ".epub"
    data = open(make_epub(str(tmp_path / "b.epub")), "rb").read()
    r = client.post("/upload", data={"csrf": csrf_of(client, "/upload"), "file": (io.BytesIO(data), name)},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200 and b"Uploaded" in r.data
    saved = os.listdir(os.path.join(config.DROPBOX_DIR, "alice"))
    assert len(saved) == 1 and len(saved[0].encode()) <= 200
    # and the ingest name, which adds ' [owner-rid]' and an extension, still fits NAME_MAX
    assert worker.scan_dropbox_once(now=time.time() + 60) == 1
    assert all(len(n.encode()) < 255 for n in _ingested()) and len(_ingested()) == 1


# ---------------------------------------------------------------- J14
def test_j14_a_password_changed_in_calibre_web_warns_instead_of_breaking_audiobookshelf(monkeypatch):
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(config, "ABS_TOKEN", "k")
    alerts = []
    monkeypatch.setattr(worker.notify, "alert", lambda t, x, prio="default": alerts.append(t))
    assert worker.check_password_drift() == 0          # first pass only learns the fingerprints
    cwa.set_password("alice", "changed-in-cwa-ui")     # as CWA's own /me page would
    assert worker.check_password_drift() == 1
    assert alerts == ["a password was changed outside the portal"]
    assert worker.check_password_drift() == 0          # reported once
    assert any(a["event"] == "password_changed_in_cwa" for a in db.audit_recent(10))


def test_j14_the_devices_page_says_where_to_change_a_password(client, users):
    login(client, "alice", users["alice"])
    body = client.get("/devices").get_data(as_text=True)
    assert "Change your password only here" in body


def test_j14_a_password_change_in_the_portal_does_not_warn(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    monkeypatch.setattr(config, "ABS_TOKEN", "")
    worker.check_password_drift()
    post(client, "/devices", action="password", current=users["alice"], new="brandnew-123", repeat="brandnew-123")
    alerts = []
    monkeypatch.setattr(worker.notify, "alert", lambda t, x, prio="default": alerts.append(t))
    assert worker.check_password_drift() == 0 and alerts == []


# ---------------------------------------------------------------- J16 / J17
def test_j16_deny_carries_the_admin_s_reason(client, users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    login(client, "alice", users["alice"])
    rid = _req("alice", status="pending")
    post(client, "/logout"); login(client, "admin", users["admin"])
    post(client, f"/deny/{rid}", reason="We already have it on the shelf")
    assert db.get(rid)["detail"] == "denied by admin: We already have it on the shelf"
    _req("bob", status="pending")                      # the form is on every pending row
    assert b"Reason (optional" in client.get("/status").data


def test_j17_uppercase_usernames_are_refused(client, users):
    with pytest.raises(cwa.CwaError, match="lowercase"):
        cwa.add_user("Adm_Kim", "password12", "k@example.test")
    login(client, "admin", users["admin"])
    r = post(client, "/admin", action="add_user", name="Adm_Kim", email="k@example.test", password="password12")
    assert b"lowercase" in r.data and cwa.get_user("Adm_Kim") is None


# ---------------------------------------------------------------- J18
def test_j18_the_dashboard_counts_every_row_not_only_the_newest_200(client, users):
    for i in range(210):
        db.add("alice", {"kind": "ebook", "source": "dropbox", "title": f"b{i}", "download_url": "local"},
               status="done")
    old_fail = _req("alice", source="dropbox", download_url="local", status="error")
    old_needs = _req("alice", status=worker.NEEDS_TAG)
    for i in range(210):                                  # push them past the 200-row window
        db.add("alice", {"kind": "ebook", "source": "dropbox", "title": f"c{i}", "download_url": "local"},
               status="done")
    assert db.counts_by_status()["done"] == 420
    login(client, "admin", users["admin"])
    body = client.get("/admin").get_data(as_text=True)
    assert re.search(r"needs tag <strong[^>]*>1<", body) and re.search(r"failed <strong[^>]*>1<", body)
    assert "owner:alice" in body                         # the old needs-tag row is listed
    post(client, "/retry_all")
    assert db.get(old_fail)["status"] == "error"          # dropbox rows are not re-fetchable...
    http_fail = _req("bob", status="error")
    post(client, "/retry_all")
    assert db.get(http_fail)["status"] == "queued" and db.get(old_needs)["status"] == worker.NEEDS_TAG


# ---------------------------------------------------------------- J19 / J30
def test_j19_get_logout_does_not_pretend_to_sign_you_out(client, users):
    login(client, "alice", users["alice"])
    r = client.get("/logout", follow_redirects=True)
    assert b"Log out button" in r.data and client.get("/status").status_code == 200
    assert client.get("/login").status_code == 302       # already signed in
    post(client, "/logout")
    assert client.get("/login").status_code == 200


def test_j30_the_lockout_answer_carries_retry_after(client, users, monkeypatch):
    monkeypatch.setattr(config, "LOCKOUT_FAILS", 2)
    for _ in range(3):
        login(client, "alice", "wrong-password")
    r = login(client, "alice", "wrong-password")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


# ---------------------------------------------------------------- J20 / J33
def test_j20_auto_kindle_covers_pdf_and_uses_the_title_as_subject(monkeypatch, tmp_path):
    cwa.add_user("alice", "alicepass1")
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    db.set_prefs("alice", auto_kindle=True)
    monkeypatch.setattr(worker.kindle, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(worker.kindle, "send", lambda addr, path, title, filename: sent.append((addr, title, filename)) or "sent")
    note = worker._atomic_ingest(make_pdf(str(tmp_path / "p.pdf")), "alice", "paper", "pdf", 5, title="The Paper")
    assert "auto-Kindle sent" in note and sent == [("alice@kindle.com", "The Paper", "paper.pdf")]


def test_j33_a_drm_protected_epub_is_flagged(tmp_path):
    cwa.add_user("alice", "alicepass1")
    p = make_epub(str(tmp_path / "drm.epub"))
    with zipfile.ZipFile(p, "a") as z:
        z.writestr("META-INF/rights.xml", "<rights/>")
        z.writestr("META-INF/encryption.xml", "<encryption/>")
    note = worker._atomic_ingest(p, "alice", "drm", "epub", 3)
    assert worker.DRM_NOTE in note
    plain = worker._atomic_ingest(make_epub(str(tmp_path / "ok.epub")), "alice", "ok", "epub", 4)
    assert worker.DRM_NOTE not in plain


# ---------------------------------------------------------------- J21
def test_j21_mail_is_peeked_not_marked_seen_and_skipped_attachments_are_audited(monkeypatch):
    import imap
    from test_core import FakeImap, _mail
    cwa.add_user("alice", "alicepass1", "alice@example.test")
    fake = FakeImap([_mail("intake+alice@example.test", filename="notes.exe")])
    fetched = []
    orig = fake.fetch
    fake.fetch = lambda num, what: fetched.append(what) or orig(num, what)
    monkeypatch.setattr(imap, "_connect", lambda: fake)
    assert imap.poll_once() == 0
    assert "(BODY.PEEK[])" in fetched and "(RFC822)" not in fetched
    skipped = [a["detail"] for a in db.audit_recent(10) if a["event"] == "imap_skipped"]
    assert skipped and "notes.exe" in skipped[0]


# ---------------------------------------------------------------- J22
def test_j22_a_file_still_being_written_is_not_buried_in_failed(monkeypatch):
    cwa.add_user("bob", "bobpass1")
    p = _drop("bob", "slow.epub", data=b"PK\x03\x04half")
    real = worker.ingest_local_file

    def grow_then_fail(path, owner, rid=None):
        open(path, "ab").write(b"the rest of the file")      # the writer finishes mid-import
        return real(path, owner, rid)
    monkeypatch.setattr(worker, "ingest_local_file", grow_then_fail)
    assert worker.scan_dropbox_once() == 1
    row = db.list_for("bob", False)[0]
    assert row["status"] == "error" and "still being written" in row["detail"]
    assert os.path.exists(p) and not os.path.exists(os.path.join(config.DROPBOX_DIR, "bob", ".failed", "slow.epub"))


# ---------------------------------------------------------------- J23
def test_j23_the_quota_is_atomic_and_ignores_denied_and_failed_requests(monkeypatch):
    cwa.add_user("alice", "alicepass1")
    r = {"kind": "ebook", "source": "gutenberg", "title": "T", "download_url": "https://x/y"}
    ids = [db.add_if_under_quota("alice", r, 3)[0] for _ in range(5)]
    assert sum(1 for i in ids if i) == 3 and ids[3] is None
    _, left, resets = db.add_if_under_quota("alice", r, 3)
    assert left == 0 and resets > time.time()
    db.set_status(ids[0], "denied"); db.set_status(ids[1], "error")
    assert db.requests_today("alice") == 1
    rid, left, _ = db.add_if_under_quota("alice", r, 3)
    assert rid and left == 1
    db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "u", "download_url": "local"})
    assert db.requests_today("alice") == 2               # uploads never count


def test_j23_the_portal_says_how_many_are_left(client, users, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DAY", 2)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    login(client, "alice", users["alice"])
    form = {"kind": "ebook", "source": "gutenberg", "identifier": "g:1", "title": "A", "author": "B",
            "download_url": "https://www.gutenberg.org/ebooks/1.epub"}
    r = post(client, "/request", **form)
    assert b"1 of 2 requests left today" in r.data
    post(client, "/request", **form)
    r = post(client, "/request", **form)
    assert b"reached the limit of 2" in r.data and b"You can request again after" in r.data


# ---------------------------------------------------------------- J24
def test_j24_a_dead_calibre_web_is_visible_to_the_admin(client, users, monkeypatch):
    import requests as _r
    now = time.time()
    monkeypatch.setattr(worker, "HEARTBEAT", {"queue": now, "dropbox": now, "housekeeping": now})
    monkeypatch.setattr(config, "CWA_URL", "http://127.0.0.1:65530")
    monkeypatch.setitem(app_health_cache(), "at", 0.0)
    monkeypatch.setattr(_r, "get", lambda *a, **k: (_ for _ in ()).throw(_r.ConnectionError("refused")))
    login(client, "admin", users["admin"])
    h = client.get("/healthz").get_json()
    assert h["cwa"].startswith("calibre-web is not answering") and h["ok"] is True   # portal itself is fine
    assert b"calibre-web is not answering" in client.get("/admin").data


def app_health_cache():
    import app
    return app._CWA_PROBE


# ---------------------------------------------------------------- J26
def test_j26_a_replayed_intake_url_does_not_queue_the_book_twice(client, users):
    body = {"user": "alice", "url": "https://x/one.epub"}
    hdr = {"X-Intake-Token": "intake-token-123"}
    first = client.post("/intake", json=body, headers=hdr)
    again = client.post("/intake", json=body, headers=hdr)
    assert first.status_code == 202 and again.status_code == 200
    assert again.get_json()["id"] == first.get_json()["id"] and again.get_json()["duplicate"]
    assert len(db.list_for("alice", False)) == 1
    db.set_status(first.get_json()["id"], "error", "boom")
    assert client.post("/intake", json=body, headers=hdr).status_code == 202   # a failed one may be retried


def test_j26_the_same_audiobook_twice_is_reported_not_duplicated(tmp_path):
    cwa.add_user("alice", "alicepass1")
    z = _zip(str(tmp_path / "book.zip"), {"01.mp3": b"ID3" + b"x" * 100})
    worker.ingest_local_file(z, "alice")
    assert os.listdir(config.AUDIO_DIR) == ["alice - book"]
    with pytest.raises(ValueError, match="already in your audiobooks"):
        worker.ingest_local_file(z, "alice")
    assert os.listdir(config.AUDIO_DIR) == ["alice - book"]


# ---------------------------------------------------------------- J28 / J29
def test_j28_the_user_never_sees_the_internal_address(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(0, 0, 0, "", ("192.168.107.2", p))])
    with pytest.raises(ValueError) as e:
        worker._check_target("https://calibre-web/x.epub")
    assert "192.168" not in str(e.value) and "not allowed" in str(e.value)
    assert any("192.168.107.2" in (a["detail"] or "") for a in db.audit_recent(5))


def test_j29_intake_accepts_an_authorization_bearer_token(client, users):
    r = client.post("/intake", json={"user": "alice", "url": "https://x/b.epub"},
                    headers={"Authorization": "Bearer intake-token-123"})
    assert r.status_code == 202
    r = client.post("/intake", json={"user": "alice", "url": "https://x/c.epub"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


# ---------------------------------------------------------------- J34
def test_j34_a_directory_symlink_in_a_dropbox_is_refused(tmp_path):
    cwa.add_user("bob", "bobpass1")
    d = os.path.join(config.DROPBOX_DIR, "bob")
    os.makedirs(d, exist_ok=True)
    empty = tmp_path / "empty"; empty.mkdir()
    link = os.path.join(d, "shortcut")
    os.symlink(str(empty), link)
    assert worker.scan_dropbox_once() == 1
    row = db.list_for("bob", False)[0]
    assert row["status"] == "error" and "symbolic link" in row["detail"]
    assert not os.path.lexists(link) and os.path.lexists(os.path.join(d, ".failed", "shortcut"))


# ---------------------------------------------------------------- J37 / J38 / J39
def test_j37_a_slow_source_cannot_hold_the_page_and_duplicates_collapse(monkeypatch):
    def slow(q):
        time.sleep(5)
        return [{"source": "gutenberg", "title": "Late", "author": "X", "download_url": "u"}]

    def quick(q):
        return [{"source": "librivox", "title": "Fast", "author": "Y", "download_url": "u1"},
                {"source": "librivox", "title": "Fast", "author": "Y", "download_url": "u1"},
                {"source": "librivox", "title": "fast", "author": "y", "download_url": "u2"}]
    monkeypatch.setitem(fetchers._ADAPTERS, "gutenberg", slow)
    monkeypatch.setitem(fetchers._ADAPTERS, "librivox", quick)
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True, "librivox": True})
    started = time.time()
    out = fetchers.search("x", deadline=0.5)
    assert time.time() - started < 3
    assert [r["title"] for r in out] == ["Fast"]        # the slow source lost, duplicates gone


def test_j38_j39_the_pages_speak_plain_language(client, users, monkeypatch):
    monkeypatch.setattr(config, "IMAP_HOST", "mail.example.test")
    monkeypatch.setattr(config, "IMAP_USER", "books@example.test")
    login(client, "alice", users["alice"])
    home = client.get("/").get_data(as_text=True)
    assert "Project Gutenberg" in home and "Internet Archive" in home and "internet_archive" not in home
    assert "Start here" in home and "books+alice@example.test" in home
    assert "Audiobooks ↗" in home                        # nav link to Audiobookshelf
    assert "Shelfmark" in home and "extended search" not in home
    assert "books+alice@example.test" in client.get("/upload").get_data(as_text=True)
    rid = _req("alice", source="dropbox", download_url="local", status="done")
    db.set_status(rid, "done", "tagged owner:alice")
    body = client.get("/status").get_data(as_text=True)
    assert "added to your library" in body and "owner:alice" not in body and "Your upload" in body
    post(client, "/logout"); login(client, "admin", users["admin"])
    assert "owner:alice" in client.get("/status").get_data(as_text=True)    # admins see the real note


def test_j39_an_audiobook_only_user_is_not_told_the_library_is_empty(client, users):
    login(client, "alice", users["alice"])
    body = client.get("/library").get_data(as_text=True)
    assert "No <em>ebooks</em> here yet" in body and "Audiobookshelf" in body


# ---------------------------------------------------------------- J42 / J43
def test_j42_a_successful_retry_clears_the_interrupted_row(monkeypatch, tmp_path):
    cwa.add_user("bob", "bobpass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    _drop("bob", "book.epub", maker=make_epub)
    rid = db.add("bob", {"kind": "ebook", "source": "dropbox", "title": "book.epub",
                         "download_url": "local"}, status="importing")
    db.recover_on_start()
    assert db.get(rid)["status"] == "error" and db.get(rid)["detail"] == db.INTERRUPTED
    assert worker.scan_dropbox_once() == 1
    rows = db.list_for("bob", False)
    assert rows[0]["status"] == "done"
    assert db.get(rid)["status"] == "dismissed" and "retry succeeded" in db.get(rid)["detail"]


def test_j43_auto_kindle_needs_an_address(client, users):
    login(client, "alice", users["alice"])
    r = post(client, "/devices", action="prefs", preferred_format="epub", auto_kindle="1")
    assert b"stays off until you save your Kindle address" in r.data
    assert db.get_prefs("alice")["auto_kindle"] is False
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    r = post(client, "/devices", action="prefs", preferred_format="epub", auto_kindle="1")
    assert db.get_prefs("alice")["auto_kindle"] is True


# ---------------------------------------------------------------- J40
def test_j40_a_404_is_final_and_errors_are_plain_language(monkeypatch, tmp_path):
    cwa.add_user("alice", "alicepass1")
    import requests as _r
    calls, states = [], []
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    rid = _req("alice")
    real_set = db.set_status
    monkeypatch.setattr(db, "set_status", lambda r, s, d=None: (states.append(s), real_set(r, s, d))[1])

    def gone(url, dest, req=None):
        calls.append(url)
        raise _r.HTTPError("404 Client Error", response=type("R", (), {"status_code": 404})())
    monkeypatch.setattr(worker, "_fetch", gone)
    with pytest.raises(ValueError, match="no longer available"):
        worker._download("https://x/y.epub", str(tmp_path / "out"), rid=rid)
    assert calls == ["https://x/y.epub"]                  # tried once, not three times
    assert states[0] == worker.DOWNLOADING                # the user sees 'downloading'

    calls.clear(); states.clear()
    def flaky(url, dest, req=None):
        calls.append(url)
        raise _r.ConnectionError("boom")
    monkeypatch.setattr(worker, "_fetch", flaky)
    with pytest.raises(ValueError, match="could not be reached"):
        worker._download("https://x/y.epub", str(tmp_path / "out"), rid=rid, attempts=3)
    assert len(calls) == 3 and "retrying" in states


def test_j37_search_covers_never_hold_the_page(client, users, monkeypatch):
    monkeypatch.setattr(config, "ENRICH_METADATA", True)
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True})
    monkeypatch.setitem(fetchers._ADAPTERS, "gutenberg", lambda q: [
        {"source": "gutenberg", "kind": "ebook", "title": "Slow Cover", "author": "A",
         "identifier": "g:1", "format": "epub", "download_url": "https://www.gutenberg.org/1.epub"}])

    def slow(title, author=""):
        time.sleep(5)
        return {"cover_url": "https://covers.openlibrary.org/b/id/1-M.jpg"}
    import app as appmod
    monkeypatch.setattr(appmod.enrich, "for_book", slow)
    monkeypatch.setattr(appmod, "ENRICH_DEADLINE", 0.5)
    login(client, "alice", users["alice"])
    started = time.time()
    body = client.get("/?q=slow").get_data(as_text=True)
    assert time.time() - started < 3 and "Slow Cover" in body and "covers.openlibrary" not in body
