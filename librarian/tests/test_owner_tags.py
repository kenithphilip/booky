"""L10: books whose file cannot carry the owner tag get it added in Calibre by the host job."""
import json, time
import config, db, worker, admin_cli
from conftest import add_calibre_book, calibre_conn


def _needs_tag(owner, title, ext="mobi", file_title=None):
    rid = db.add(owner, {"kind": "ebook", "source": "dropbox", "title": title, "download_url": "local"},
                 status=worker.NEEDS_TAG)
    db.set_status(rid, worker.NEEDS_TAG, f"{worker.NEEDS_TAG}: {ext} cannot carry a tag; admin sets owner:{owner} in CWA")
    if file_title:
        db.set_file_meta(rid, {"title": file_title, "author": "Jane Austen"})
    return rid


def _stamp(book_id, when):
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("UPDATE books SET timestamp=datetime(?, 'unixepoch') WHERE id=?", (when, book_id))
    c.commit(); c.close()


def test_the_marker_finds_the_book_and_a_tag_job_is_queued(users):
    rid = _needs_tag("alice", "notes")
    add_calibre_book(5, f"notes [alice-{rid}]", "Unknown", tags=[], formats=("txt",))
    assert worker.reconcile_untagged() == 1
    (job,) = db.pending_tag_pushes()
    assert job["calibre_id"] == 5 and job["owner"] == "alice" and job["rid"] == rid
    assert worker.reconcile_untagged() == 0, "one job per request"


def test_a_mobi_with_its_own_title_is_found_by_title_format_and_time(users):
    _needs_tag("alice", "emma_final_v2", file_title="Emma")
    add_calibre_book(7, "Emma", "Jane Austen", tags=[], formats=("mobi",))
    _stamp(7, time.time())
    assert worker.reconcile_untagged() == 1 and db.pending_tag_pushes()[0]["calibre_id"] == 7


def test_ambiguity_is_left_for_the_admin(users):
    _needs_tag("alice", "x", file_title="Emma")
    for bid in (7, 8):
        add_calibre_book(bid, "Emma", "Jane Austen", tags=[], formats=("mobi",))
        _stamp(bid, time.time())
    assert worker.reconcile_untagged() == 0 and not db.pending_tag_pushes()


def test_a_book_that_already_has_an_owner_is_never_a_candidate(users):
    _needs_tag("alice", "x", file_title="Emma")
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:bob"], formats=("mobi",))
    _stamp(7, time.time())
    assert worker.reconcile_untagged() == 0


def test_an_older_book_is_not_mistaken_for_the_new_arrival(users):
    _needs_tag("alice", "x", file_title="Emma")
    add_calibre_book(7, "Emma", "Jane Austen", tags=[], formats=("mobi",))
    _stamp(7, time.time() - 86400)
    assert worker.reconcile_untagged() == 0


def test_the_host_reports_back_and_the_request_is_done(users, monkeypatch, capsys):
    told = []
    monkeypatch.setattr(worker.notify, "send", lambda ev, r: told.append(ev))
    import notify
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append(ev))
    rid = _needs_tag("alice", "notes")
    add_calibre_book(5, f"notes [alice-{rid}]", "Unknown", tags=[], formats=("txt",))
    worker.reconcile_untagged()
    admin_cli.main(["tags", "pending"])
    job = json.loads(capsys.readouterr().out)["rows"][0]
    assert admin_cli.main(["tags", "result", str(job["id"]), "ok"]) == 0
    assert db.get(rid)["status"] == "done" and "tagged owner:alice" in db.get(rid)["detail"]
    assert "done" in told


def test_a_refusal_from_the_host_is_final(users):
    rid = _needs_tag("alice", "notes")
    add_calibre_book(5, f"notes [alice-{rid}]", "Unknown", tags=[], formats=("txt",))
    worker.reconcile_untagged()
    job = db.pending_tag_pushes()[0]
    db.tag_push_result(job["id"], False, "refused: the book already has ['owner:bob']")
    assert not db.pending_tag_pushes() and db.get(rid)["status"] == worker.NEEDS_TAG


def test_audiobooks_are_not_touched(users):
    rid = db.add("alice", {"kind": "audio", "source": "dropbox", "title": "x", "download_url": "local"},
                 status=worker.NEEDS_TAG)
    add_calibre_book(5, f"x [alice-{rid}]", "Unknown", tags=[], formats=("txt",))
    assert worker.reconcile_untagged() == 0



def test_send_to_kindle_retries_then_fails_and_tells_the_reader(users, monkeypatch):
    """L21: the worker sends; a relay that stays down is retried three times, then reported."""
    import kindle, cwa, notify
    from conftest import add_calibre_book
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"], formats=("epub",))
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    monkeypatch.setattr(kindle, "send", lambda *a, **k: (_ for _ in ()).throw(OSError("relay down")))
    told = []
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append((ev, r["detail"])))
    jid = db.kindle_enqueue("alice", False, 1, "Alice Book")
    t = time.time()
    for i in range(4):
        worker.kindle_once(now=t + i * 4000)
    job = db.kindle_recent("alice")[0]
    assert job["id"] == jid and job["status"] == "failed" and job["attempts"] == 4
    assert told and told[0][0] == "error" and "relay down" in told[0][1]


def test_a_kindle_job_for_a_book_no_longer_visible_is_not_sent(users, monkeypatch):
    import kindle
    sent = []
    monkeypatch.setattr(kindle, "send", lambda *a, **k: sent.append(1))
    db.kindle_enqueue("alice", False, 99, "Gone")
    worker.kindle_once()
    assert not sent and db.kindle_recent("alice")[0]["status"] == "failed"



def test_a_finished_import_is_joined_to_its_calibre_book_even_without_the_marker(users):
    """An EPUB with its own metadata: Calibre renames it and the [owner-rid] marker is gone."""
    rid = db.add("alice", {"kind": "ebook", "source": "standard_ebooks", "title": "Pride and Prejudice",
                           "download_url": "https://standardebooks.org/x.epub"}, status="done")
    db.set_file_meta(rid, {"title": "Pride and Prejudice", "author": "Jane Austen"})
    with db._conn() as c:
        c.execute("UPDATE requests SET created=?, updated=? WHERE id=?", (time.time() - 400, time.time() - 400, rid))
    add_calibre_book(9, "Pride and Prejudice", "Jane Austen", tags=["owner:alice"])
    add_calibre_book(10, "Pride and Prejudice", "Jane Austen", tags=["owner:bob"])     # a sibling's copy
    _stamp(9, time.time()); _stamp(10, time.time())
    worker.reconcile_imports()
    assert db.get(rid)["calibre_id"] == 9, "her copy, never bob's"


def test_two_candidates_are_not_guessed_between(users):
    rid = db.add("alice", {"kind": "ebook", "source": "gutenberg", "title": "Emma", "download_url": "x"}, status="done")
    with db._conn() as c:
        c.execute("UPDATE requests SET created=?, updated=? WHERE id=?", (time.time() - 400, time.time() - 400, rid))
    for bid in (9, 10):
        add_calibre_book(bid, "Emma", "Jane Austen", tags=["owner:alice"]); _stamp(bid, time.time())
    worker.reconcile_imports()
    assert db.get(rid)["calibre_id"] is None


# ---- L12: the Kobo card ------------------------------------------------------------------------
def test_the_kobo_card_shows_status_tests_the_link_and_saves_options(client, users, monkeypatch):
    import cwa, requests as _r
    from conftest import login, post
    cwa.kobo_url("alice", create=True)
    with cwa._conn() as c:
        uid = c.execute("SELECT id FROM user WHERE name='alice'").fetchone()[0]
        c.execute("CREATE TABLE IF NOT EXISTS kobo_synced_books(id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS kobo_reading_state(id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER, last_modified TEXT, priority_timestamp TEXT)")
        c.executemany("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?,?)", [(uid, 1), (uid, 2), (uid + 99, 3)])
        c.execute("INSERT INTO kobo_reading_state(user_id, book_id, last_modified) VALUES(?, 1, '2026-09-25 20:15:00')", (uid,))
    login(client, "alice", users["alice"])
    html = client.get("/devices").get_data(as_text=True)
    assert "<strong>2</strong> books handed to your Kobo" in html and "2026-09-25 20:15" in html
    assert "Before you start" in html and "Libby" in html

    class R:
        status_code, text = 200, '{"Resources": {}}'
    seen = []
    monkeypatch.setattr(_r, "get", lambda url, **k: seen.append(url) or R())
    r = post(client, "/devices", action="kobo_test")
    assert b"Your Kobo link works" in r.data and seen[0].endswith("/v1/initialization") and "/kobo/" in seen[0]
    post(client, "/devices", action="kobo_prefs", shelves_only="1", hardcover_change="1", hardcover_token="hc_pat_x")
    st = cwa.kobo_status("alice")
    assert st["shelves_only"] and st["hardcover"]
    post(client, "/devices", action="kobo_prefs", hardcover_change="1", hardcover_token="")
    st = cwa.kobo_status("alice")
    assert not st["shelves_only"] and not st["hardcover"], "unticked = off; a blank token removes it (NULL, the column is UNIQUE)"


# ---- L17: optional Turnstile on the portal login ----------------------------------------------
def test_without_turnstile_the_login_page_runs_no_script(client):
    r = client.get("/login")
    assert "script-src 'none'" in r.headers["Content-Security-Policy"] and b"challenges.cloudflare.com" not in r.data


def test_turnstile_on_login_only_and_verified_server_side(client, users, monkeypatch):
    import config, requests as _r
    from conftest import csrf_of
    monkeypatch.setattr(config, "TURNSTILE_SITEKEY", "0x4AAA"); monkeypatch.setattr(config, "TURNSTILE_SECRET", "sec")
    r = client.get("/login")
    assert b'data-sitekey="0x4AAA"' in r.data and "script-src https://challenges.cloudflare.com" in r.headers["Content-Security-Policy"]

    class A:
        def __init__(self, ok): self.ok = ok
        def json(self): return {"success": self.ok}
    monkeypatch.setattr(_r, "post", lambda *a, **k: A(False))
    r = client.post("/login", data={"username": "alice", "password": users["alice"], "csrf": csrf_of(client)})
    assert r.status_code == 400 and b"bot check did not pass" in r.data
    monkeypatch.setattr(_r, "post", lambda *a, **k: A(True))
    r = client.post("/login", data={"username": "alice", "password": users["alice"], "csrf": csrf_of(client)})
    assert r.status_code == 302
    assert "script-src 'none'" in client.get("/status").headers["Content-Security-Policy"], "every other page keeps no scripts"


def test_cloudflare_unreachable_does_not_lock_the_family_out(client, users, monkeypatch):
    import config, requests as _r
    from conftest import csrf_of
    monkeypatch.setattr(config, "TURNSTILE_SITEKEY", "0x4AAA"); monkeypatch.setattr(config, "TURNSTILE_SECRET", "sec")
    monkeypatch.setattr(_r, "post", lambda *a, **k: (_ for _ in ()).throw(_r.ConnectionError("down")))
    r = client.post("/login", data={"username": "alice", "password": users["alice"], "csrf": csrf_of(client)})
    assert r.status_code == 302
    assert any(a["event"] == "turnstile_unreachable" for a in db.audit_recent(20))


# ---- L18: large uploads over Tailscale ----------------------------------------------------------
def test_the_tailnet_upload_site_raises_the_limit_and_only_it_can(client, users, monkeypatch):
    import io, app as appmod, config
    from conftest import login, csrf_of
    monkeypatch.setitem(appmod.app.config, "MAX_CONTENT_LENGTH", 2000)
    monkeypatch.setattr(config, "MAX_UPLOAD_TAILNET_MB", 1)
    login(client, "alice", users["alice"])
    big = b"x" * 6000
    r = client.post("/upload", data={"csrf": csrf_of(client, "/upload"), "file": (io.BytesIO(big), "a.epub")},
                    content_type="multipart/form-data")
    assert r.status_code == 302 and "upload" in r.headers["Location"], "through Cloudflare: over the cap, sent back"
    r = client.post("/upload", data={"csrf": csrf_of(client, "/upload"), "file": (io.BytesIO(big), "a.epub")},
                    content_type="multipart/form-data", headers={"X-Bookstack-Upload": "tailnet"})
    assert r.status_code != 413 and not (r.status_code == 302 and r.headers["Location"].endswith("/upload") and b"larger than" in client.get("/upload").data)


# ---- MOBI / AZW3 / FB2 tagged automatically after the import (v5.3) ---------------------------
import os, shutil
import filemeta, notify
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "untaggable")


def test_filemeta_reads_title_and_author_from_real_calibre_files():
    for ext in ("mobi", "azw3", "fb2"):
        m = filemeta.read(os.path.join(FIX, f"kite-runner.{ext}"), ext)
        assert m == {"title": "The Kite Runner", "author": "Khaled Hosseini & Ünïcode Co"}, (ext, m)
    assert filemeta.read(os.path.join(FIX, "plain-name-mobi6.mobi"), "mobi") == {"title": "Plain Name", "author": "A B"}


def test_filemeta_never_raises_on_junk(tmp_path):
    for ext, data in (("mobi", b"x" * 100), ("mobi", b"\0" * 60 + b"BOOKMOBI" + b"\xff" * 30), ("fb2", b"\xff\xfe<title-info>"),
                      ("azw3", b"")):
        p = tmp_path / f"j.{ext}"; p.write_bytes(data)
        assert filemeta.read(str(p), ext) == {}
    assert filemeta.read(str(tmp_path / "missing.mobi"), "mobi") == {}


def test_a_mobi_arrival_records_its_metadata_and_raises_no_alarm(users, monkeypatch, tmp_path):
    told = []
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append(ev))
    src = tmp_path / "Khaled Hosseini - The Kite Runner (2003).mobi"
    shutil.copyfile(os.path.join(FIX, "kite-runner.mobi"), src)
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": src.name, "download_url": "local"})
    note = worker._atomic_ingest(str(src), "alice", "Khaled Hosseini - The Kite Runner (2003)", "mobi", rid)
    assert note.startswith(worker.NEEDS_TAG) and worker.AUTO_TAG_NOTE in note
    r = db.get(rid)
    assert r["file_title"] == "The Kite Runner" and "Khaled Hosseini" in r["file_author"]
    worker._finish(rid, worker.NEEDS_TAG, note)
    assert told == [], "no 'needs an owner tag' alarm while the host job is adding it"


def test_a_mobi_that_cwa_converted_to_epub_is_still_found(users):
    rid = _needs_tag("alice", "Khaled Hosseini - The Kite Runner (2003).mobi", file_title="The Kite Runner")
    db.set_file_meta(rid, {"title": "The Kite Runner", "author": "Khaled Hosseini"})
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=[], formats=("epub",))
    _stamp(7, time.time())
    assert worker.reconcile_untagged() == 1 and db.pending_tag_pushes()[0]["calibre_id"] == 7


def test_a_shelfmark_named_file_without_metadata_is_found_by_its_name(users):
    """The row that was already waiting when this shipped: no file_title, only the file name."""
    _needs_tag("alice", "Khaled Hosseini - The Kite Runner (2003).mobi")
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=[], formats=("epub",))
    _stamp(7, time.time())
    assert worker.reconcile_untagged() == 1 and db.pending_tag_pushes()[0]["calibre_id"] == 7


def test_an_author_that_disagrees_is_not_a_match(users):
    rid = _needs_tag("alice", "x.mobi")
    db.set_file_meta(rid, {"title": "Emma", "author": "Jane Austen"})
    add_calibre_book(7, "Emma", "Somebody Else", tags=[], formats=("epub",))
    _stamp(7, time.time())
    assert worker.reconcile_untagged() == 0


def test_not_found_in_time_the_admin_hears_once(users, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append((ev, r["detail"])))
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "Nowhere.mobi", "download_url": "local"})
    db.set_status(rid, worker.NEEDS_TAG, f"{worker.NEEDS_TAG}: mobi cannot carry a tag; {worker.AUTO_TAG_NOTE}")
    assert worker.reconcile_untagged() == 0 and told == [], "too early to worry"
    later = time.time() + worker.AUTO_TAG_ESCALATE + 60
    worker.reconcile_untagged(now=later)
    worker.reconcile_untagged(now=later + 60)
    assert [e for e, _ in told] == [worker.NEEDS_TAG], "exactly one alert"
    assert "not found in Calibre automatically" in db.get(rid)["detail"]


def test_a_host_that_gives_up_hands_it_to_the_admin(users, monkeypatch, capsys):
    told = []
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append(ev))
    rid = _needs_tag("alice", "notes")
    add_calibre_book(5, f"notes [alice-{rid}]", "Unknown", tags=[], formats=("txt",))
    worker.reconcile_untagged()
    job = db.pending_tag_pushes()[0]
    for _ in range(5):
        admin_cli.main(["tags", "result", str(job["id"]), "fail", "--reason", "calibredb exited 1"])
    capsys.readouterr()
    assert told == [worker.NEEDS_TAG] and "could not be added in Calibre" in db.get(rid)["detail"]


def test_the_reader_is_told_it_is_on_its_way():
    import app
    assert "few minutes" in app._friendly_detail(f"needs-tag: mobi cannot carry a tag; {worker.AUTO_TAG_NOTE}")
    assert "admin has to tag" in app._friendly_detail("needs-tag: mobi cannot carry a tag and the book was not found")
