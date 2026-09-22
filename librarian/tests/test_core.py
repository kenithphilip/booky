"""Queue state, tagging (EPUB/PDF/CBZ), library reads, worker ingest paths and trust boundary,
dropbox watcher recovery, e-mail intake, provider parsing and URL allowlists."""
import os, zipfile, time, threading, json, shutil, logging
import pytest
from lxml import etree
from pypdf import PdfReader
import config, db, tagger, library, worker, cwa, kindle, fetchers, opds
from conftest import make_epub, make_pdf, make_cbz, add_calibre_book, calibre_conn

# ---- db -------------------------------------------------------------------------------
def _req(owner="alice", **kw):
    r = {"kind": "ebook", "source": "gutenberg", "identifier": "gutenberg:1", "title": "T",
         "author": "A", "download_url": "https://example.test/b.epub", "is_torrent": False}
    r.update(kw); return db.add(owner, r, status=kw.get("status", "queued"))

def test_claim_one_is_fifo_and_exclusive():
    a, b = _req(title="first"), _req(title="second")
    assert db.claim_one()["id"] == a and db.get(a)["status"] == "importing"
    assert db.claim_one()["id"] == b
    assert db.claim_one() is None

def test_claim_one_never_hands_out_the_same_request_twice_under_contention():
    ids = [_req(title=f"t{i}") for i in range(30)]
    got, lock = [], threading.Lock()
    def grab():
        while True:
            r = db.claim_one()
            if not r: return
            with lock: got.append(r["id"])
    ts = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(got) == sorted(ids) and len(got) == len(set(got))

def test_list_for_scopes_by_owner_and_status_updates_keep_detail():
    a = _req("alice"); _req("bob")
    assert [r["owner"] for r in db.list_for("alice", False)] == ["alice"]
    assert len(db.list_for("admin", True)) == 2
    db.set_status(a, "error", "boom"); db.set_status(a, "queued")      # None detail keeps the old one
    assert db.get(a)["detail"] == "boom"
    assert db.last_for("alice", "T", "gutenberg")["id"] == a and db.last_for("alice", "T", "dropbox") is None

def test_recover_on_start_requeues_refetchable_rows_and_fails_local_ones():
    http = _req("alice"); local = _req("bob", source="dropbox", title="up.epub", download_url="local")
    tor = _req("carol", is_torrent=True, download_url="magnet:?xt=a"); fresh = _req("dave", is_torrent=True, download_url="magnet:?xt=b")
    db.claim_one(); db.claim_one()                                        # http + local are now 'importing'
    db.set_status(tor, "downloading"); db.set_status(fresh, "downloading")
    with db._conn() as c:
        c.execute("UPDATE requests SET updated=? WHERE id=?", (time.time() - 90000, tor))
    db.recover_on_start()
    assert db.get(http)["status"] == "queued" and db.get(http)["detail"] == "requeued after restart"
    assert db.get(local)["status"] == "error" and db.get(local)["detail"] == "interrupted by restart"
    assert db.get(tor)["status"] == "error" and "24 h" in db.get(tor)["detail"]
    assert db.get(fresh)["status"] == "downloading"

def test_prefs_defaults_and_validation():
    assert db.get_prefs("alice") == {"preferred_format": "epub", "auto_kindle": False, "notify_email": False, "last_kindle_test": None}
    db.set_prefs("alice", preferred_format="azw3", auto_kindle=True)
    assert db.get_prefs("alice")["preferred_format"] == "azw3" and db.get_prefs("alice")["auto_kindle"] is True
    db.set_prefs("alice", notify_email=True)
    assert db.get_prefs("alice")["notify_email"] is True and db.get_prefs("alice")["auto_kindle"] is True
    db.set_prefs("alice", preferred_format="exe")                      # unknown format ignored
    assert db.get_prefs("alice")["preferred_format"] == "azw3"
    with db._conn() as c:
        c.execute("UPDATE prefs SET preferred_format='kepub' WHERE owner='alice'")    # stored before kepub was dropped
    assert db.get_prefs("alice")["preferred_format"] == "epub"
    db.set_prefs("alice", last_kindle_test=1700000000.0)
    assert db.get_prefs("alice")["last_kindle_test"] == 1700000000.0 and db.get_prefs("alice")["notify_email"] is True

# ---- tagger -----------------------------------------------------------------------------
def _subjects(path, opf="OEBPS/content.opf"):
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read(opf))
    return [s.text for s in root.iter("{http://purl.org/dc/elements/1.1/}subject")]

def test_owner_tag_is_written_once_and_epub_stays_valid(tmp_path):
    p = make_epub(str(tmp_path / "b.epub"), subjects=["Fiction"])
    tagger.add_owner_tag(p, "owner:alice")
    tagger.add_owner_tag(p, "owner:alice")
    assert _subjects(p) == ["Fiction", "owner:alice"]
    with zipfile.ZipFile(p) as z:
        assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.testzip() is None
    assert not os.path.exists(p + ".tmp")

def test_tagger_keeps_compression_timestamps_and_size(tmp_path):
    p = str(tmp_path / "big.epub")
    with zipfile.ZipFile(make_epub(p), "a") as z:                        # a real-world sized, well compressible chapter
        z.writestr(zipfile.ZipInfo("OEBPS/ch2.xhtml", date_time=(2020, 1, 2, 3, 4, 6)), "<p>lorem ipsum </p>" * 60000, compress_type=zipfile.ZIP_DEFLATED)
    before = {i.filename: (i.compress_type, i.date_time) for i in zipfile.ZipFile(p).infolist()}
    size_before = os.path.getsize(p)
    tagger.add_owner_tag(p, "owner:alice")
    after = {i.filename: (i.compress_type, i.date_time) for i in zipfile.ZipFile(p).infolist()}
    assert after == before and os.path.getsize(p) <= 1.1 * size_before
    assert list(after)[0] == "mimetype" and _subjects(p) == ["owner:alice"]

def test_tagger_adds_metadata_element_when_missing(tmp_path):
    p = str(tmp_path / "bare.epub")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="x.opf"/></rootfiles></container>')
        z.writestr("x.opf", '<package xmlns="http://www.idpf.org/2007/opf"/>')
    tagger.add_owner_tag(p, "owner:bob")
    with zipfile.ZipFile(p) as z:
        assert b"owner:bob" in z.read("x.opf")

def test_tagger_resolves_encoded_and_miscased_opf_paths_and_fails_closed(tmp_path):
    def epub_with(full_path, opf_name):
        p = str(tmp_path / f"{opf_name.replace('/', '_')}.epub")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", f'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="{full_path}"/></rootfiles></container>')
            z.writestr(opf_name, '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"/></package>')
        return p, opf_name
    p, opf = epub_with("OEBPS/content%20x.opf", "OEBPS/content x.opf"); tagger.add_owner_tag(p, "owner:a"); assert _subjects(p, opf) == ["owner:a"]
    p, opf = epub_with("OEBPS/Content.OPF", "OEBPS/content.opf"); tagger.add_owner_tag(p, "owner:a"); assert _subjects(p, opf) == ["owner:a"]
    p, _ = epub_with("OEBPS/missing.opf", "OEBPS/content.opf")
    with pytest.raises(tagger.TagError, match="OPF not found"):
        tagger.add_owner_tag(p, "owner:a")
    bad = str(tmp_path / "bad.epub"); open(bad, "wb").write(b"not a zip")
    with pytest.raises(tagger.TagError, match="not a zip"):
        tagger.add_owner_tag(bad, "owner:a")
    nocont = str(tmp_path / "nocont.epub")
    with zipfile.ZipFile(nocont, "w") as z: z.writestr("mimetype", "application/epub+zip")
    with pytest.raises(tagger.TagError, match="container.xml"):
        tagger.add_owner_tag(nocont, "owner:a")
    assert not any(n.endswith(".tmp") for n in os.listdir(tmp_path))

def test_pdf_tag_merges_keywords_idempotently_and_fills_missing_title(tmp_path):
    p = make_pdf(str(tmp_path / "paper.pdf"), title="A Paper", keywords="Science; History")
    tagger.add_owner_tag_pdf(p, "owner:alice", title="ignored")
    tagger.add_owner_tag_pdf(p, "owner:alice")
    m = PdfReader(p).metadata
    assert m["/Keywords"] == "Science, History, owner:alice" and m["/Title"] == "A Paper"     # Calibre splits Keywords into tags
    q = make_pdf(str(tmp_path / "untitled.pdf"))
    tagger.add_owner_tag_pdf(q, "owner:bob", title="untitled")
    m = PdfReader(q).metadata; assert m["/Keywords"] == "owner:bob" and m["/Title"] == "untitled"
    bad = str(tmp_path / "bad.pdf"); open(bad, "wb").write(b"%PDF-1.4 garbage")
    with pytest.raises(tagger.TagError):
        tagger.add_owner_tag_pdf(bad, "owner:bob")
    assert not os.path.exists(bad + ".tmp")

def test_cbz_tag_lives_in_a_comicbookinfo_comment_and_merges(tmp_path):
    p = make_cbz(str(tmp_path / "comic.cbz"))
    tagger.add_owner_tag_cbz(p, "owner:alice", title="comic")
    tagger.add_owner_tag_cbz(p, "owner:alice")
    with zipfile.ZipFile(p) as z:
        info = json.loads(z.comment.decode()); assert z.testzip() is None and len(z.namelist()) == 2
    assert info["ComicBookInfo/1.0"]["tags"] == ["owner:alice"] and info["ComicBookInfo/1.0"]["title"] == "comic" and info["appID"] == "bookstack"
    existing = json.dumps({"appID": "other", "ComicBookInfo/1.0": {"title": "Real Title", "tags": ["Comics"], "series": "S"}}).encode()
    q = make_cbz(str(tmp_path / "tagged.cbz"), comment=existing)
    tagger.add_owner_tag_cbz(q, "owner:bob", title="tagged")
    info = json.loads(zipfile.ZipFile(q).comment.decode())
    assert info["ComicBookInfo/1.0"] == {"title": "Real Title", "tags": ["Comics", "owner:bob"], "series": "S"} and info["appID"] == "other"
    junk = make_cbz(str(tmp_path / "junk.cbz"), comment=b"not json")
    tagger.add_owner_tag_cbz(junk, "owner:bob")
    assert json.loads(zipfile.ZipFile(junk).comment.decode())["ComicBookInfo/1.0"]["tags"] == ["owner:bob"]
    with pytest.raises(tagger.TagError):
        tagger.add_owner_tag_cbz(str(make_pdf(str(tmp_path / "x.cbz"))), "owner:bob")

# ---- library --------------------------------------------------------------------------
def test_library_is_tag_scoped_and_paths_are_confined():
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"], formats=("epub", "pdf"))
    add_calibre_book(2, "Bob Book", "Bo Writer", tags=["owner:bob"])
    add_calibre_book(3, "Untagged", "Nobody")
    assert [b["title"] for b in library.books_for("alice")] == ["Alice Book"]
    assert library.books_for("alice")[0]["formats"] == ["epub", "pdf"]
    assert sorted(b["title"] for b in library.books_for("admin", is_admin=True)) == ["Alice Book", "Bob Book", "Untagged"]
    assert library.books_for("admin", is_admin=True)[0]["owners"] in (["alice"], ["bob"], [])
    f = library.file_for("alice", 1, "pdf")
    assert f and f["path"].endswith("Alice Book - Ann Author.pdf") and f["filename"] == "Alice Book - Ann Author.pdf"
    assert library.file_for("alice", 2, "epub") is None            # bob's book
    assert library.file_for("alice", 1, "exe") is None             # unknown format
    assert library.file_for("alice", 1, "azw3") is None            # format not present
    assert library.file_for("admin", 2, "epub", is_admin=True)
    assert library.visible("alice", 1) and not library.visible("alice", 2) and library.visible("admin", 2, is_admin=True) and not library.visible("alice", 99)
    # a hostile path in metadata.db can't escape the library root
    c = calibre_conn(config.CALIBRE_DB); c.execute("UPDATE books SET path='../../etc' WHERE id=1"); c.commit(); c.close()
    assert library.file_for("alice", 1, "pdf") is None
    assert library.best_format({"formats": ["pdf", "epub"]}, "azw3") == "epub"
    assert library.best_format({"formats": ["pdf"]}, "epub") == "pdf"
    assert library.best_format({"formats": []}, "epub") is None

# ---- worker: local ingest paths ------------------------------------------------------
def _ingested(pattern=""):
    return sorted(n for n in os.listdir(config.INGEST_DIR) if pattern in n)

def test_safe_keeps_unicode_strips_separators_and_controls():
    assert worker._safe("Толстой, Лев") == "Толстой, Лев"
    assert worker._safe("युद्ध और शांति") == "युद्ध और शांति"
    assert worker._safe("a/b\\c:d*e?f\"g<h>i|j\x00k") == "a_b_c_d_e_f_g_h_i_j_k"
    assert worker._safe(" ..hidden.. ") == "hidden" and worker._safe("") == "book" and worker._safe(None) == "book"
    assert len(worker._safe("x" * 400)) == 150
    assert worker._safe("Pride & Prejudice (1813)") == "Pride & Prejudice (1813)"

def test_epub_ingest_is_tagged_atomic_and_uniquely_named(tmp_path):
    src = make_epub(str(tmp_path / "in.epub"))
    note = worker.ingest_local_file(src, "alice")
    out = os.listdir(config.INGEST_DIR)
    assert len(out) == 1 and out[0].startswith("in [alice-") and out[0].endswith(".epub") and "tagged owner:alice" in note
    assert _subjects(os.path.join(config.INGEST_DIR, out[0])) == ["owner:alice"]
    assert not any(n.endswith((".part", ".tmp")) for n in out)
    worker.ingest_local_file(src, "bob", rid=7)
    assert "in [bob-7].epub" in os.listdir(config.INGEST_DIR) and len(os.listdir(config.INGEST_DIR)) == 2   # no overwrite

def test_pdf_and_cbz_are_tagged_before_import_other_formats_need_a_tag(tmp_path):
    pdf = make_pdf(str(tmp_path / "paper.pdf"), keywords="Science")
    assert worker.ingest_local_file(str(pdf), "alice", rid=1) == "tagged owner:alice"
    assert _ingested() == ["paper [alice-1].pdf"]
    m = PdfReader(os.path.join(config.INGEST_DIR, "paper [alice-1].pdf")).metadata
    assert m["/Keywords"] == "Science, owner:alice" and m["/Title"] == "paper"
    cbz = make_cbz(str(tmp_path / "comic.cbz"))
    assert worker.ingest_local_file(str(cbz), "alice", rid=2) == "tagged owner:alice"
    assert json.loads(zipfile.ZipFile(os.path.join(config.INGEST_DIR, "comic [alice-2].cbz")).comment.decode())["ComicBookInfo/1.0"]["tags"] == ["owner:alice"]
    mobi = tmp_path / "old.mobi"; mobi.write_bytes(b"BOOKMOBI")
    note = worker.ingest_local_file(str(mobi), "alice", rid=3)
    assert note.startswith("needs-tag") and "owner:alice" in note and worker._status_for(note) == "needs-tag"
    assert "old [alice-3].mobi" in _ingested()
    with pytest.raises(ValueError, match="CBZ"):
        worker.ingest_local_file(str(tmp_path / "x.cbr"), "alice")
    (tmp_path / "x.exe").write_bytes(b"MZ")
    with pytest.raises(ValueError, match="unsupported file type"):
        worker.ingest_local_file(str(tmp_path / "x.exe"), "alice")
    assert not any(n.endswith((".part", ".tmp")) for n in os.listdir(config.INGEST_DIR))

def test_untaggable_file_fails_closed_for_isolated_users_but_not_admins(tmp_path):
    cwa.add_user("alice", "alicepass1"); cwa.add_user("boss", "bosspass1", admin=True)
    bad = tmp_path / "broken.pdf"; bad.write_bytes(b"%PDF-1.4 nope")
    with pytest.raises(RuntimeError, match="could not embed owner tag"):
        worker.ingest_local_file(str(bad), "alice", rid=1)
    assert os.listdir(config.INGEST_DIR) == []                          # nothing imported untagged, no .part left
    note = worker.ingest_local_file(str(bad), "boss", rid=2)
    assert note.startswith("tag skipped") and _ingested() == ["broken [boss-2].pdf"]
    badepub = tmp_path / "broken.epub"; badepub.write_bytes(b"not a zip")
    with pytest.raises(RuntimeError, match="not a zip"):
        worker.ingest_local_file(str(badepub), "alice")
    with pytest.raises(RuntimeError):                                   # unknown owner is not an admin either
        worker.ingest_local_file(str(badepub), "ghost")

def test_audio_ingest(tmp_path):
    z = tmp_path / "audio book.zip"
    with zipfile.ZipFile(z, "w") as zf: zf.writestr("01.mp3", b"ID3")
    note = worker.ingest_local_file(str(z), "alice")
    assert "set tag owner:alice in ABS" in note and "skipped" in note      # no ABS token in tests
    assert os.listdir(config.AUDIO_DIR) == ["alice - audio book"] and os.listdir(os.path.join(config.AUDIO_DIR, "alice - audio book")) == ["01.mp3"]

def _drop(owner, name, maker=None, age=60):
    d = os.path.join(config.DROPBOX_DIR, owner); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if maker: maker(p)
    else: open(p, "wb").write(b"x")
    old = time.time() - age; os.utime(p, (old, old))
    return p

def test_dropbox_scan_skips_partial_hidden_fresh_files_and_unknown_users(caplog):
    cwa.add_user("alice", "alicepass1")
    d = os.path.join(config.DROPBOX_DIR, "alice")
    _drop("alice", "ready.epub", make_epub)
    make_epub(os.path.join(d, "fresh.epub"))                                            # just written
    for n in (".hidden.epub", "x.epub.part", "y.crdownload", ".book.epub.uploading"):
        _drop("alice", n)
    os.makedirs(os.path.join(d, "subfolder"))                                           # empty: never settled
    _drop("stranger", "stray.epub", make_epub)                                          # not a user
    _drop("Al ice", "stray.epub", make_epub)                                            # not a valid name
    with caplog.at_level(logging.WARNING):
        assert worker.scan_dropbox_once() == 1
    assert "dropbox/stranger" in caplog.text
    assert sorted(os.listdir(d)) == [".book.epub.uploading", ".hidden.epub", "fresh.epub", "subfolder", "x.epub.part", "y.crdownload"]
    assert _ingested() == [n for n in _ingested() if n.startswith("ready [alice-")] and len(_ingested()) == 1
    assert os.listdir(os.path.join(config.DROPBOX_DIR, "stranger")) == ["stray.epub"]
    rows = db.list_for("alice", False)
    assert len(rows) == 1 and rows[0]["status"] == "done" and rows[0]["source"] == "dropbox" and rows[0]["title"] == "ready.epub"
    assert db.list_for("admin", True) == rows
    # second pass: nothing new (fresh one still settling)
    assert worker.scan_dropbox_once() == 0
    assert worker.scan_dropbox_once(now=time.time() + 60) == 1
    assert worker.HEARTBEAT["dropbox"] > time.time() - 5

def test_dropbox_uses_canonical_user_name_and_needs_tag_status(tmp_path):
    cwa.add_user("Alice", "alicepass1")
    _drop("Alice", "notes.txt")
    _drop("Alice", "paper.pdf", make_pdf)
    assert worker.scan_dropbox_once() == 2
    rows = {r["title"]: r for r in db.list_for("Alice", False)}
    assert rows["notes.txt"]["status"] == "needs-tag" and rows["paper.pdf"]["status"] == "done"
    assert sorted(_ingested()) == ["notes [Alice-%d].txt" % rows["notes.txt"]["id"], "paper [Alice-%d].pdf" % rows["paper.pdf"]["id"]]

def test_dropbox_failure_records_one_row_and_parks_the_file(monkeypatch):
    cwa.add_user("bob", "bobpass1")
    p = _drop("bob", "broken.epub")                                      # not a zip -> untaggable -> error for a non-admin
    _drop("bob", "bad.zip")                                              # not a zip -> audio extraction fails
    assert worker.scan_dropbox_once() == 2
    rows = db.list_for("bob", False)
    assert [r["status"] for r in rows] == ["error", "error"] and all(".failed/" in r["detail"] for r in rows)
    failed = os.path.join(config.DROPBOX_DIR, "bob", ".failed")
    assert sorted(os.listdir(failed)) == ["bad.zip", "broken.epub"] and not os.path.exists(p)
    assert os.listdir(config.INGEST_DIR) == [] and os.listdir(config.AUDIO_DIR) == []
    assert worker.scan_dropbox_once() == 0 and len(db.list_for("bob", False)) == 2       # parked: not retried
    # even if the park itself fails, the existing error row stops the flood
    p = _drop("bob", "broken.epub")
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("ro fs")))
    assert worker.scan_dropbox_once() == 0 and len(db.list_for("bob", False)) == 2
    monkeypatch.undo()
    # an admin's untaggable EPUB still imports (they see untagged books anyway)
    cwa.add_user("boss", "bosspass1", admin=True)
    _drop("boss", "broken.epub")
    assert worker.scan_dropbox_once() == 1 and db.list_for("boss", False)[0]["status"] == "done"
    assert "tag skipped" in db.list_for("boss", False)[0]["detail"]

def test_settled_directory_in_dropbox_becomes_an_audiobook(monkeypatch):
    import abs as absapi
    cwa.add_user("alice", "alicepass1")
    d = os.path.join(config.DROPBOX_DIR, "alice", "Great Audiobook"); os.makedirs(os.path.join(d, "Disc 1"))
    for n in ("Disc 1/01.mp3", "02.mp3", "cover.jpg"):
        open(os.path.join(d, n), "wb").write(b"ID3")
    assert worker.scan_dropbox_once() == 0                               # files are fresh: still settling
    now = time.time() + 60
    started = []
    monkeypatch.setattr(config, "ABS_TOKEN", "k"); monkeypatch.setattr(absapi, "trigger_scan", lambda: "ABS scan triggered")
    monkeypatch.setattr(absapi, "tag_folder_async", lambda folder, owner, rid=None: started.append((folder, owner, rid)))
    open(os.path.join(d, "03.mp3.part"), "wb").write(b"x")               # a download still in progress inside
    assert worker.scan_dropbox_once(now=now) == 0
    os.remove(os.path.join(d, "03.mp3.part"))
    assert worker.scan_dropbox_once(now=now) == 1
    final = os.path.join(config.AUDIO_DIR, "alice - Great Audiobook")
    assert sorted(os.listdir(final)) == ["02.mp3", "Disc 1", "cover.jpg"] and os.listdir(os.path.join(final, "Disc 1")) == ["01.mp3"]
    assert not os.path.exists(d)
    r = db.list_for("alice", False)[0]
    assert r["kind"] == "audio" and r["status"] == "done" and started == [("alice - Great Audiobook", "alice", r["id"])]
    # a directory that is not a user's is ignored; a cross-device move falls back to copy
    monkeypatch.setattr(os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("EXDEV")) if a.startswith(config.DROPBOX_DIR) else os.replace(a, b))
    d2 = os.path.join(config.DROPBOX_DIR, "alice", "Second"); os.makedirs(d2); open(os.path.join(d2, "a.mp3"), "wb").write(b"x")
    assert worker.scan_dropbox_once(now=now + 100) == 1
    assert os.listdir(os.path.join(config.AUDIO_DIR, "alice - Second")) == ["a.mp3"] and not os.path.exists(d2)

def test_sweep_stale_quarantines_old_partials_only():
    old = time.time() - 7200
    for n in ("crash.part", "crash.part.tmp", "recent.part", "book.epub"):
        p = os.path.join(config.INGEST_DIR, n); open(p, "wb").write(b"x")
        if n != "recent.part": os.utime(p, (old, old))
    d = os.path.join(config.DROPBOX_DIR, "alice"); os.makedirs(d)
    p = os.path.join(d, ".up.epub.uploading"); open(p, "wb").write(b"x"); os.utime(p, (old, old))
    assert worker.sweep_stale() == 3
    assert sorted(os.listdir(config.INGEST_DIR)) == ["book.epub", "recent.part"] and os.listdir(d) == []
    q = os.listdir(os.path.join(config.STAGING_DIR, "quarantine"))
    assert len(q) == 3 and all(n.split("-", 1)[1] in ("crash.part", "crash.part.tmp", ".up.epub.uploading") for n in q)

def test_auto_kindle_runs_before_ingest_when_opted_in(monkeypatch, tmp_path):
    cwa.add_user("alice", "alicepass1"); cwa.set_kindle_mail("alice", "alice@kindle.com")
    db.set_prefs("alice", auto_kindle=True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    sent = {}
    def fake_send(to, path, title=None, filename=None):
        sent.update(to=to, filename=filename, exists=os.path.exists(path)); return f"sent to {to}"
    monkeypatch.setattr(kindle, "send", fake_send)
    note = worker.ingest_local_file(make_epub(str(tmp_path / "k.epub")), "alice")
    assert sent == {"to": "alice@kindle.com", "filename": "k.epub", "exists": True} and "auto-Kindle sent" in note   # no ingest suffix in the mail
    cwa.set_kindle_mail("alice", "")
    assert "no address" in worker.ingest_local_file(make_epub(str(tmp_path / "k2.epub")), "alice")
    db.set_prefs("alice", auto_kindle=False)
    assert "Kindle" not in worker.ingest_local_file(make_epub(str(tmp_path / "k3.epub")), "alice")

def test_process_http_ebook_and_torrent_handoff(monkeypatch, tmp_path):
    src = make_epub(str(tmp_path / "dl.epub"))
    monkeypatch.setattr(worker, "_download", lambda url, dest, **kw: shutil.copyfile(src, dest))
    rid = _req("alice", title="Pride & Prejudice", author="Jane Austen")
    worker._process(db.claim_one())
    r = db.get(rid)
    assert r["status"] == "done" and os.listdir(config.INGEST_DIR) == [f"Jane Austen - Pride & Prejudice [alice-{rid}].epub"]
    pdfsrc = make_pdf(str(tmp_path / "dl.pdf"))
    monkeypatch.setattr(worker, "_download", lambda url, dest, **kw: shutil.copyfile(pdfsrc, dest))
    pid = _req("alice", title="A Paper", author="X", source="mycatalog")
    worker._process(db.claim_one())
    assert db.get(pid)["status"] == "done" and f"X - A Paper [alice-{pid}].pdf" in os.listdir(config.INGEST_DIR)   # sniffed, not assumed epub
    added = {}
    monkeypatch.setattr(worker.Qbit, "add", lambda self, url, path: added.update(url=url, path=path))
    monkeypatch.setattr(worker.Qbit, "__init__", lambda self: None)
    tid = _req("bob", is_torrent=True, download_url="magnet:?xt=urn:btih:abc")
    worker._process(db.claim_one())
    assert db.get(tid)["status"] == "downloading" and added == {"url": "magnet:?xt=urn:btih:abc", "path": config.STAGING_DIR}
    tid2 = _req("bob", is_torrent=True, download_url="http://127.0.0.1:2019/x.torrent")     # .torrent URLs are fenced too
    worker._process(db.claim_one())
    assert db.get(tid2)["status"] == "error" and "non-public" in db.get(tid2)["detail"]
    def boom(*a, **k): raise RuntimeError("network down")
    monkeypatch.setattr(worker, "_download", boom)
    eid = _req("alice"); worker._process(db.claim_one())
    assert db.get(eid)["status"] == "error" and "network down" in db.get(eid)["detail"]

def test_http_ebook_that_cannot_be_tagged_is_kept_for_the_admin(monkeypatch, tmp_path):
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker, "_download", lambda url, dest, **kw: open(dest, "wb").write(b"PK\x03\x04junk"))
    rid = _req("alice", title="Odd", author="Q")
    worker._process(db.claim_one())
    r = db.get(rid)
    assert r["status"] == "error" and "could not embed owner tag" in r["detail"] and "dropbox/alice/.failed/Q - Odd.epub" in r["detail"]
    assert os.path.exists(os.path.join(config.DROPBOX_DIR, "alice", ".failed", "Q - Odd.epub")) and os.listdir(config.INGEST_DIR) == []

# ---- worker: outbound trust boundary ------------------------------------------------------
def _resolves_to(monkeypatch, *ips):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(0, 0, 0, "", (ip, port)) for ip in ips])

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.9", "169.254.169.254", "100.101.102.103",
                                "0.0.0.0", "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_check_target_refuses_internal_addresses(monkeypatch, ip):
    _resolves_to(monkeypatch, ip)
    with pytest.raises(ValueError, match="non-public"):
        worker._check_target("https://host.example/x.epub")

def test_check_target_allows_public_hosts_and_trusted_catalog_origins(monkeypatch):
    import socket
    _resolves_to(monkeypatch, "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    worker._check_target("https://www.gutenberg.org/ebooks/1.epub")
    _resolves_to(monkeypatch, "93.184.216.34", "10.0.0.1")                  # ANY internal answer is enough to refuse
    with pytest.raises(ValueError): worker._check_target("https://dual.example/")
    for bad in ("ftp://x/y", "file:///etc/passwd", "gopher://x", "https:///nohost"):
        with pytest.raises(ValueError, match="unsupported"): worker._check_target(bad)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no such host")))
    with pytest.raises(ValueError, match="cannot resolve"): worker._check_target("https://nx.example/")
    # the admin's own catalog / mirror on the tailnet is trusted without resolution (exact netloc)
    monkeypatch.setattr(config, "MYCATALOG_URL", "http://100.101.102.103:8080/opds")
    monkeypatch.setattr(config, "GUTENBERG_MIRROR", "http://mirror.lan/")
    worker._check_target("http://100.101.102.103:8080/get/epub/1"); worker._check_target("http://mirror.lan/ebooks/1.epub")
    with pytest.raises(ValueError): worker._check_target("http://100.101.102.103:8081/")   # other port: not the catalog
    with pytest.raises(ValueError): worker._check_target("http://user@mirror.lan/")

def test_auth_only_goes_to_the_configured_catalog_origin(monkeypatch):
    monkeypatch.setattr(config, "MYCATALOG_URL", "https://books.mine.tld/opds/search/{q}")
    monkeypatch.setattr(config, "MYCATALOG_USER", "me"); monkeypatch.setattr(config, "MYCATALOG_PASS", "pw")
    req = {"source": "mycatalog"}
    assert worker._auth_for(req, "https://books.mine.tld/get/epub/1") == ("me", "pw")
    assert worker._auth_for(req, "https://BOOKS.mine.tld/get/epub/1") == ("me", "pw")
    assert worker._auth_for(req, "https://evil.tld/get/epub/1") is None                    # tampered form / redirect
    assert worker._auth_for(req, "http://books.mine.tld/get/epub/1") is None                # downgrade
    assert worker._auth_for(req, "https://books.mine.tld:8443/get/epub/1") is None
    assert worker._auth_for({"source": "gutenberg"}, "https://books.mine.tld/x") is None
    assert worker._auth_for(None, "https://books.mine.tld/x") is None

class _Resp:
    def __init__(self, status=200, headers=None, body=b"data", chunks=None):
        self.status_code, self.headers, self._chunks = status, headers or {}, chunks or [body]
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self):
        if self.status_code >= 400: raise IOError(str(self.status_code))
    def iter_content(self, n):
        yield from self._chunks

def test_fetch_follows_redirects_by_hand_rechecks_each_hop_and_caps_size(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(config, "MYCATALOG_URL", "https://cat.mine.tld/opds"); monkeypatch.setattr(config, "MYCATALOG_USER", "me")
    monkeypatch.setattr(worker, "_check_target", lambda url: seen.append(url) if "127.0.0.1" not in url else (_ for _ in ()).throw(ValueError("non-public")))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: os.statvfs_result((0,) * 10) if False else type("D", (), {"free": 100 * 1024 ** 3})())
    responses = {"https://cat.mine.tld/get/1": _Resp(302, {"Location": "/files/1.epub"}),
                 "https://cat.mine.tld/files/1.epub": _Resp(307, {"Location": "https://cdn.example/1.epub"}),
                 "https://cdn.example/1.epub": _Resp(200, {"Content-Length": "4"}, b"book")}
    auths = {}
    def fake_get(url, **kw):
        auths[url] = kw["auth"]; assert kw["allow_redirects"] is False; return responses[url]
    monkeypatch.setattr(worker.requests, "get", fake_get)
    dest = str(tmp_path / "out")
    worker._fetch("https://cat.mine.tld/get/1", dest, {"source": "mycatalog", "kind": "ebook"})
    assert open(dest, "rb").read() == b"book" and seen == list(responses)                 # every hop checked, in order
    assert auths == {"https://cat.mine.tld/get/1": ("me", ""), "https://cat.mine.tld/files/1.epub": ("me", ""), "https://cdn.example/1.epub": None}
    responses["https://cdn.example/1.epub"] = _Resp(302, {"Location": "http://127.0.0.1:2019/config/"})
    with pytest.raises(ValueError, match="non-public"):                                     # a redirect cannot escape the fence
        worker._fetch("https://cat.mine.tld/get/1", dest, {"source": "mycatalog"})
    loop = {"https://a/": _Resp(302, {"Location": "https://a/"})}
    monkeypatch.setattr(worker.requests, "get", lambda url, **kw: loop[url])
    with pytest.raises(ValueError, match="too many redirects"):
        worker._fetch("https://a/", dest)
    # size gates: Content-Length first, then the bytes actually streamed; per kind
    monkeypatch.setattr(worker.requests, "get", lambda url, **kw: _Resp(200, {"Content-Length": str(700 * 2 ** 20)}, b""))
    with pytest.raises(ValueError, match="700 MB"):
        worker._fetch("https://x/", dest, {"kind": "ebook"})
    worker._fetch("https://x/", dest, {"kind": "audio"})                                   # a 700 MB LibriVox zip is fine
    monkeypatch.setattr(worker.requests, "get", lambda url, **kw: _Resp(200, {}, chunks=[b"x" * 1024] * 3))
    monkeypatch.setattr(config, "MAX_EBOOK_MB", 0)
    with pytest.raises(ValueError, match="exceeded"):
        worker._fetch("https://x/", dest, {"kind": "ebook"})
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("D", (), {"free": 2 * 1024 ** 3 + 10})())
    monkeypatch.setattr(worker.requests, "get", lambda url, **kw: _Resp(200, {"Content-Length": "100"}, b""))
    with pytest.raises(ValueError, match="free disk"):
        worker._fetch("https://x/", dest, {"kind": "audio"})

def test_download_retries_with_backoff_but_not_policy_refusals(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(worker, "_check_target", lambda url: None)
    monkeypatch.setattr(worker.requests, "get", lambda *a, **k: (calls.append(1), _Resp(503 if len(calls) < 3 else 200))[1])
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    rid = _req(); dest = str(tmp_path / "out")
    worker._download("https://x", dest, rid=rid)
    assert len(calls) == 3 and open(dest, "rb").read() == b"data" and db.get(rid)["status"] == "retrying"
    calls.clear()
    monkeypatch.setattr(worker, "_check_target", lambda url: (_ for _ in ()).throw(ValueError("non-public")))
    with pytest.raises(ValueError):
        worker._download("https://x", dest, rid=rid)
    assert calls == []                                                                        # refused once, no retries

# ---- e-mail intake --------------------------------------------------------------------------
class FakeImap:
    def __init__(self, messages): self.messages = messages; self.flagged = []
    def select(self, folder): return "OK", [b"1"]
    def search(self, charset, crit): return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]
    def fetch(self, num, what): return "OK", [(b"", self.messages[int(num) - 1])]
    def store(self, num, flags, value): self.flagged.append(num)
    def logout(self): pass

def _mail(to, delivered=None, filename="my book.epub", payload=b"PK\x03\x04data", sender="alice@example.test"):
    from email.message import EmailMessage
    m = EmailMessage(); m["From"] = f"Someone <{sender}>"; m["To"] = to
    if delivered: m["Delivered-To"] = delivered
    m["Subject"] = "book"; m.set_content("hi")
    m.add_attachment(payload, maintype="application", subtype="epub+zip", filename=filename)
    return m.as_bytes()

def test_imap_intake_routes_by_plus_address_checks_users_and_senders(monkeypatch, caplog):
    import imap
    cwa.add_user("alice", "alicepass1", "alice@example.test"); cwa.add_user("bob", "bobpass1", "Bob@Example.test")
    fake = FakeImap([_mail("intake+alice@example.test"),
                     _mail("intake@example.test", delivered="intake+bob@example.test", filename="../evil:;.epub", sender="bob@example.test"),
                     _mail("intake@example.test", filename="noone.epub"),                       # no plus address, no default user
                     _mail("intake+carol@example.test", filename="notes.exe"),                   # not a user
                     _mail("intake+nosuchuser@example.test"),
                     _mail("intake+../alice@example.test"),
                     _mail("intake+alice@example.test", filename="spoof.epub", sender="mallory@evil.test"),   # wrong sender
                     _mail("intake+bob@example.test", filename="日本語 の本.EPUB", sender="bob@example.test"),
                     _mail("intake+alice@example.test", filename="huge.epub", payload=b"x" * (config.MAX_UPLOAD_MB * 1024 * 1024 + 1))])
    monkeypatch.setattr(imap, "_connect", lambda: fake)
    with caplog.at_level(logging.WARNING):
        assert imap.poll_once() == 3
    assert os.listdir(os.path.join(config.DROPBOX_DIR, "alice")) == ["my book.epub"]
    assert sorted(os.listdir(os.path.join(config.DROPBOX_DIR, "bob"))) == ["evil_;.epub", "日本語 の本.epub"]   # ../ and : gone, Unicode kept
    assert not os.path.exists(os.path.join(config.DROPBOX_DIR, "carol")) and not os.path.exists(os.path.join(config.DROPBOX_DIR, "nosuchuser"))
    assert fake.flagged == [str(i).encode() for i in range(1, 10)]                                # all marked seen
    assert "sender not allowed" in caplog.text and "unknown user" in caplog.text and "over" in caplog.text
    # an explicit sender allowlist replaces the own-address rule
    monkeypatch.setattr(config, "IMAP_ALLOWED_SENDERS", ["forwarder@example.test"])
    fake2 = FakeImap([_mail("intake+alice@example.test", filename="fwd.epub", sender="Forwarder@example.test"),
                      _mail("intake+alice@example.test", filename="own.epub")])                  # her own address no longer enough
    monkeypatch.setattr(imap, "_connect", lambda: fake2)
    assert imap.poll_once() == 1 and sorted(os.listdir(os.path.join(config.DROPBOX_DIR, "alice"))) == ["fwd.epub", "my book.epub"]
    monkeypatch.setattr(config, "IMAP_DEFAULT_USER", "dave"); cwa.add_user("dave", "davepass1", "dave@example.test")
    fake3 = FakeImap([_mail("intake@example.test", sender="forwarder@example.test")]); monkeypatch.setattr(imap, "_connect", lambda: fake3)
    assert imap.poll_once() == 1 and os.listdir(os.path.join(config.DROPBOX_DIR, "dave")) == ["my book.epub"]

def test_imap_connect_modes(monkeypatch):
    import imap, imaplib
    calls = {}
    class Fake:
        def __init__(self, host, port): calls.update(host=host, port=port)
        def login(self, u, p): calls.update(user=u)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Fake); monkeypatch.setattr(imaplib, "IMAP4", Fake)
    monkeypatch.setattr(config, "IMAP_HOST", "mail.example.test"); monkeypatch.setattr(config, "IMAP_USER", "u")
    monkeypatch.setattr(config, "IMAP_SSL", True); monkeypatch.setattr(config, "IMAP_PORT", 0)
    imap._connect(); assert calls["port"] == 993
    monkeypatch.setattr(config, "IMAP_SSL", False); imap._connect(); assert calls["port"] == 143
    monkeypatch.setattr(config, "IMAP_PORT", 3143); imap._connect(); assert calls["port"] == 3143

# ---- providers ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, data=None, content=b""): self._d, self.content = data, content
    def json(self): return self._d
    def raise_for_status(self): pass

def test_gutenberg_adapter_prefers_epub_and_mirror(monkeypatch):
    data = {"results": [{"id": 1342, "title": "Pride and Prejudice", "authors": [{"name": "Austen, Jane"}],
                         "formats": {"application/epub+zip": "https://www.gutenberg.org/ebooks/1342.epub3.images", "text/plain": "x"}},
                        {"id": 1, "title": "No epub", "authors": [], "formats": {"text/plain": "x"}}]}
    monkeypatch.setattr(fetchers, "_get", lambda url, **kw: FakeResp(data))
    out = fetchers.gutenberg("pride")
    assert len(out) == 1 and out[0]["download_url"].endswith("1342.epub3.images") and out[0]["identifier"] == "gutenberg:1342"
    assert fetchers.url_allowed("gutenberg", out[0]["download_url"])
    monkeypatch.setattr(config, "GUTENBERG_MIRROR", "https://mirror.local/")
    assert fetchers.gutenberg("pride")[0]["download_url"].startswith("https://mirror.local/ebooks/")
    assert fetchers.url_allowed("gutenberg", fetchers.gutenberg("pride")[0]["download_url"])

def test_url_allowed_per_source(monkeypatch):
    ok, no = fetchers.url_allowed, lambda s, u: not fetchers.url_allowed(s, u)
    assert ok("gutenberg", "https://www.gutenberg.org/ebooks/1.epub") and ok("gutenberg", "https://gutenberg.org/cache/1.epub")
    assert no("gutenberg", "http://www.gutenberg.org/ebooks/1.epub") and no("gutenberg", "https://www.gutenberg.org.evil/x")
    assert no("gutenberg", "https://www.gutenberg.org@evil.test/x") and no("gutenberg", "https://evil.test/www.gutenberg.org")
    assert no("gutenberg", "") and no("gutenberg", None) and no("gutenberg", "https://[::1/") and no("nope", "https://www.gutenberg.org/")
    monkeypatch.setattr(config, "GUTENBERG_MIRROR", "http://mirror.lan:8000/gutenberg")
    assert ok("gutenberg", "http://mirror.lan:8000/gutenberg/1.epub") and no("gutenberg", "http://mirror.lan:8001/x") and no("gutenberg", "https://mirror.lan:8000/x")
    assert no("standard_ebooks", "http://mirror.lan:8000/x") and ok("standard_ebooks", "https://standardebooks.org/ebooks/x.epub")
    assert ok("internet_archive", "https://archive.org/download/x/x.epub") and ok("internet_archive", "https://ia800.us.archive.org/1/x.epub")
    assert no("internet_archive", "https://notarchive.org/x") and no("internet_archive", "https://archive.org.evil.test/x")
    assert ok("librivox", "https://www.archive.org/download/x/x_64kb_mp3.zip") and ok("librivox", "https://librivox.org/x.zip")
    assert no("mycatalog", "https://books.mine.tld/get/1")                                  # not configured
    monkeypatch.setattr(config, "MYCATALOG_URL", "https://books.mine.tld/opds/search/{q}")
    assert ok("mycatalog", "https://books.mine.tld/get/1") and no("mycatalog", "http://books.mine.tld/get/1")
    assert no("mycatalog", "https://books.mine.tld:8443/get/1") and no("mycatalog", "https://other.tld/get/1")
    monkeypatch.setattr(config, "MYCATALOG_URL", "http://100.101.102.103:8080/opds")        # tailnet catalog, plain http
    assert ok("mycatalog", "http://100.101.102.103:8080/get/1") and no("gutenberg", "http://100.101.102.103:8080/get/1")

OPDS = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>My Novel</title><author><name>Me</name></author>
<link rel="http://opds-spec.org/acquisition" type="application/pdf" href="/get/pdf/1"/>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/epub/1"/></entry>
<entry><title>Other</title><author><name>X</name></author><link rel="alternate" href="/x"/></entry></feed>"""

def test_opds_adapter_picks_best_format_and_resolves_relative_links(monkeypatch):
    monkeypatch.setattr(config, "MYCATALOG_URL", "https://books.mine.tld/opds/search/{q}")
    seen = {}
    monkeypatch.setattr(opds.requests, "get", lambda url, **kw: (seen.update(url=url, auth=kw.get("auth")), FakeResp(content=OPDS))[1])
    out = opds.search("my novel")
    assert seen["url"] == "https://books.mine.tld/opds/search/my%20novel" and seen["auth"] is None
    assert out == [{"source": "mycatalog", "kind": "ebook", "title": "My Novel", "author": "Me",
                    "identifier": "mycatalog:https://books.mine.tld/get/epub/1", "format": "epub",
                    "download_url": "https://books.mine.tld/get/epub/1", "is_torrent": False}]
    assert fetchers.url_allowed("mycatalog", out[0]["download_url"])

def test_search_aggregates_only_enabled_sources_and_swallows_adapter_errors(monkeypatch):
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True, "standard_ebooks": False, "internet_archive": True, "librivox": False, "mycatalog": False})
    monkeypatch.setitem(fetchers._ADAPTERS, "gutenberg", lambda q: [{"title": "g"}])
    def boom(q): raise RuntimeError("down")
    monkeypatch.setitem(fetchers._ADAPTERS, "internet_archive", boom)
    monkeypatch.setitem(fetchers._ADAPTERS, "librivox", lambda q: [{"title": "should not appear"}])
    assert fetchers.search("x") == [{"title": "g"}]
