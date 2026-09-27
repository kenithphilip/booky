"""Device metadata push: the portal decides and queues, the host applies with calibredb.

Two properties matter more than anything else here, because this is the one path that WRITES
into the family's shared Calibre database:
  * no field outside title/sort/authors/series/series_index can ever reach calibredb — above all
    not `tags`, which carries owner:<user>, the per-reader isolation;
  * it FILLS GAPS and never overwrites: a real title (perhaps one a family member corrected by
    hand) is never replaced with provider data.
"""
import json
import db, worker, config
from conftest import add_calibre_book, calibre_conn


def test_the_allowlist_is_enforced_where_the_row_is_written(users):
    # v5 allows comments/identifiers/cover_url (fill-only); tags, rating, timestamp stay out
    pid = db.queue_push(7, {"title": "T", "tags": "owner:mallory", "rating": "10",
                            "timestamp": "2020-01-01", "comments": "x", "series": "S"})
    assert pid
    with db._conn() as c:
        stored = json.loads(c.execute("SELECT fields FROM device_push WHERE id=?", (pid,)).fetchone()[0])
    assert stored == {"title": "T", "comments": "x", "series": "S"}
    assert "tags" not in db.PUSH_FIELDS


def test_the_allowlist_is_enforced_again_on_the_way_out(users):
    """A row from an older version, or written by hand, still cannot smuggle a field through."""
    with db._conn() as c:
        c.execute("INSERT INTO device_push(calibre_id,fields,status,created,updated) VALUES(9,?,'pending',0,0)",
                  (json.dumps({"title": "T", "tags": "owner:mallory"}),))
    rows = [r for r in db.pending_pushes() if r["calibre_id"] == 9]
    assert rows[0]["fields"] == {"title": "T"}


def test_nothing_but_forbidden_fields_queues_nothing(users):
    assert db.queue_push(7, {"tags": "owner:x", "rating": 5}) is None


def test_one_pending_push_per_book(users):
    assert db.queue_push(7, {"title": "A"})
    assert db.queue_push(7, {"title": "B"}) is None, "a second push must wait for the first"


def test_a_failing_push_retries_then_gives_up_visibly(users):
    pid = db.queue_push(7, {"title": "A"})
    for _ in range(4):
        assert db.push_result(pid, False, "calibredb busy") == "pending"
    assert db.push_result(pid, False, "calibredb busy") == "failed"


def _linked_request(title_in_calibre, author_in_calibre, *, req_title, series=None):
    """A book imported for alice, enriched, and linked to Calibre id 1."""
    add_calibre_book(1, title_in_calibre, author_in_calibre, tags=["owner:alice"])
    if series:
        c = calibre_conn(config.CALIBRE_DB)
        sid = c.execute("INSERT INTO series(name,sort) VALUES(?,?)", (series, series)).lastrowid
        c.execute("INSERT INTO books_series_link(book,series) VALUES(1,?)", (sid,))
        c.commit(); c.close()
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": req_title, "author": "",
                           "download_url": "local"}, status="done")
    wid = db.meta_store({"title": "Moby-Dick", "full_title": "Moby-Dick; or, The Whale",
                         "authors": [{"name": "Herman Melville"}],
                         "series": "Great American Novels", "series_position": "2",
                         "identifiers": [{"kind": "goodreads_work", "value": "153747"}],
                         "_providers": ["bookinfo"]}, rid=rid, owner="alice")
    assert wid
    db.link_calibre(rid, 1, "alice")
    return rid


def test_a_placeholder_title_is_replaced_and_its_sort_follows(users):
    rid = _linked_request(f"Melville.Moby.Dick.RETAIL {worker._ingest_marker('alice', 1)}", "Unknown",
                          req_title="Melville.Moby.Dick.RETAIL")
    assert worker.queue_device_pushes() == 1
    f = db.pending_pushes()[0]["fields"]
    assert f["title"] == "Moby-Dick; or, The Whale"
    assert f["sort"] == "Moby-Dick; or, The Whale"
    assert f["authors"] == "Herman Melville"
    assert f["series"] == "Great American Novels" and f["series_index"] == 2.0
    assert "tags" not in f
    assert rid


def test_a_real_title_is_never_overwritten(users):
    """A family member may have fixed the title in Calibre-Web by hand."""
    _linked_request("Moby Dick (Mum's copy)", "Herman Melville", req_title="Melville.Moby.Dick.RETAIL")
    worker.queue_device_pushes()
    f = db.pending_pushes()[0]["fields"]
    assert "title" not in f and "sort" not in f and "authors" not in f
    assert f["series"] == "Great American Novels", "a missing series is still a gap worth filling"


def test_an_existing_series_is_left_alone(users):
    _linked_request("Moby Dick", "Herman Melville", req_title="x", series="My Own Shelf")
    assert worker.queue_device_pushes() == 0
    assert db.pending_pushes() == []
    # and it is not re-examined on every pass for ever
    with db._conn() as c:
        assert c.execute("SELECT status FROM device_push WHERE calibre_id=1").fetchone()[0] == "skipped"
    assert db.push_candidates() == []


def test_title_sort_follows_calibres_article_rule():
    assert worker._title_sort("The Hobbit") == "Hobbit, The"
    assert worker._title_sort("A Tale of Two Cities") == "Tale of Two Cities, A"
    assert worker._title_sort("Moby-Dick") == "Moby-Dick"


def _epub_titled(path, title, creator):
    import zipfile
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator><dc:language>en</dc:language>'
           '</metadata><manifest/><spine/></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf", opf)
    return path


def _opf_of(path):
    import zipfile
    with zipfile.ZipFile(path) as z:
        return z.read("content.opf").decode()


def test_the_kindle_copy_carries_the_librarys_title_and_the_stored_file_does_not_change(tmp_path):
    """calibredb fixes the database, not the OPF inside the EPUB — and Amazon reads the file."""
    import kindle, os
    p = _epub_titled(str(tmp_path / "b.epub"), "Melville.Moby.Dick.RETAIL", "Unknown")
    before = open(p, "rb").read()
    out, note = kindle.kindle_ready(p, title="Moby-Dick", author="Herman Melville")
    try:
        assert out != p
        assert "<dc:title>Moby-Dick</dc:title>" in _opf_of(out)
        assert "<dc:creator>Herman Melville</dc:creator>" in _opf_of(out)
        assert "title" in note and "creator" in note
        assert open(p, "rb").read() == before, "the library's stored file must never change"
    finally:
        os.unlink(out)


def test_a_kindle_copy_that_already_agrees_is_sent_as_is(tmp_path):
    import kindle
    p = _epub_titled(str(tmp_path / "b.epub"), "Moby-Dick", "Herman Melville")
    out, note = kindle.kindle_ready(p, title="Moby-Dick", author="Herman Melville")
    assert out == p and note == "", "no needless rewrite when the file already agrees"


def test_the_mail_subject_never_rewrites_the_books_title(tmp_path, monkeypatch):
    """The auto-Kindle path passes a subject that can be a bare filename before import.
    Stamping THAT into dc:title would make the Kindle copy worse than the file it came from."""
    import kindle
    seen = {}
    real = kindle.kindle_ready
    def spy(path, title=None, author=None):
        seen["title"], seen["author"] = title, author
        return real(path, title=title, author=author)
    monkeypatch.setattr(kindle, "kindle_ready", spy)
    monkeypatch.setattr(kindle, "configured", lambda: True)
    monkeypatch.setattr(kindle.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("no smtp in tests")))
    p = _epub_titled(str(tmp_path / "b.epub"), "Real Title", "Real Author")
    try:
        kindle.send("a@kindle.com", p, "Melville.Moby.Dick.RETAIL.epub", "b.epub")
    except Exception:
        pass
    assert seen == {"title": None, "author": None}
