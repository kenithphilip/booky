"""Regressions for the adversarial-review findings: symlink-safe dropbox writes, XMP-aware PDF
tagging, transient app.db errors not revoking sessions, restart recovery of dropbox files,
unsupported files parked rather than deleted."""
import os, io, time, sqlite3, tempfile
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import config, db, worker, tagger, auth, cwa, kindle
from conftest import login, csrf_of, make_epub, make_pdf

def _old(p):
    t = time.time() - 60; os.utime(p, (t, t))

# ---- symlinks --------------------------------------------------------------------------------
def test_upload_never_writes_through_a_planted_symlink(client, users, tmp_path):
    victim = tmp_path / "victim.db"; victim.write_bytes(b"precious")
    d = os.path.join(config.DROPBOX_DIR, "alice"); os.makedirs(d, exist_ok=True)
    # an attacker with dropbox access pre-plants links under every name the old code would use
    os.symlink(str(victim), os.path.join(d, ".pwn.epub.uploading"))
    os.symlink(str(victim), os.path.join(d, "pwn.epub"))
    login(client, "alice", users["alice"])
    tok = csrf_of(client, "/upload")
    r = client.post("/upload", data={"csrf": tok, "file": (io.BytesIO(b"PK\x03\x04uploaded"), "pwn.epub")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"Uploaded pwn.epub" in r.data
    assert victim.read_bytes() == b"precious"                       # never written through
    final = os.path.join(d, "pwn.epub")
    assert not os.path.islink(final) and open(final, "rb").read() == b"PK\x03\x04uploaded"   # link replaced by a real file
    assert not any(n.endswith(".uploading") and not n.startswith(".pwn") for n in os.listdir(d))

def test_place_in_dropbox_cleans_up_on_failure(tmp_path):
    def boom(out): raise IOError("disk full")
    with pytest.raises(IOError):
        worker.place_in_dropbox(str(tmp_path), "x.epub", boom)
    assert os.listdir(tmp_path) == []

def test_dropbox_watcher_parks_symlinks_and_linked_folders(users, tmp_path):
    secret = tmp_path / "app.db"; secret.write_bytes(b"hashes")
    d = os.path.join(config.DROPBOX_DIR, "alice"); os.makedirs(d)
    os.symlink(str(secret), os.path.join(d, "leak.mp3")); _old(os.path.join(d, "leak.mp3"))
    os.symlink(str(secret), os.path.join(d, "passwd.txt")); _old(os.path.join(d, "passwd.txt"))
    bobdir = os.path.join(config.DROPBOX_DIR, "bob"); os.makedirs(bobdir)
    make_epub(os.path.join(bobdir, "bobs.epub")); _old(os.path.join(bobdir, "bobs.epub")); _old(bobdir)
    os.symlink(bobdir, os.path.join(d, "bobs-dropbox"))
    folder = os.path.join(d, "audio folder"); os.makedirs(folder)
    make_epub(os.path.join(folder, "01.mp3")); os.symlink(str(secret), os.path.join(folder, "02.mp3"))
    for p in (os.path.join(folder, "01.mp3"), folder): _old(p)
    handled = worker.scan_dropbox_once(now=time.time() + 100)
    assert handled == 5                                              # 4 refused for alice + bob's own epub
    # nothing reached the library or audio tree on alice's behalf; bob's real file did for bob
    assert os.listdir(config.AUDIO_DIR) == []
    assert [n for n in os.listdir(config.INGEST_DIR) if "[alice" in n] == []
    assert [n for n in os.listdir(config.INGEST_DIR) if n.startswith("bobs [bob")]
    rows = {r["title"]: r for r in db.list_for("alice", False)}
    for name in ("leak.mp3", "passwd.txt", "bobs-dropbox", "audio folder"):
        assert rows[name]["status"] == "error" and "symbolic link" in rows[name]["detail"], name
        assert not os.path.lexists(os.path.join(d, name)) and os.path.lexists(os.path.join(d, ".failed", name))
    assert secret.read_bytes() == b"hashes"                          # never read, moved or overwritten
    assert os.path.islink(os.path.join(d, ".failed", "bobs-dropbox")) and os.path.isdir(bobdir)   # the link moved, not bob's folder
    # parked entries are not retried, so no second error row appears
    assert worker.scan_dropbox_once(now=time.time() + 200) == 0
    assert len(db.list_for("alice", False)) == len(rows)

def test_unsupported_dropbox_file_is_parked_not_deleted(users):
    d = os.path.join(config.DROPBOX_DIR, "alice"); os.makedirs(d)
    p = os.path.join(d, "cover.jpg"); open(p, "wb").write(b"\xff\xd8jpg"); _old(p)
    assert worker.scan_dropbox_once() == 1
    r = db.list_for("alice", False)[0]
    assert r["status"] == "error" and "unsupported" in r["detail"] and ".failed/cover.jpg" in r["detail"]
    assert open(os.path.join(d, ".failed", "cover.jpg"), "rb").read() == b"\xff\xd8jpg"

def test_interrupted_dropbox_file_is_picked_up_after_restart(users):
    d = os.path.join(config.DROPBOX_DIR, "alice"); os.makedirs(d)
    p = os.path.join(d, "book.epub"); make_epub(p); _old(p)
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "book.epub", "download_url": "local"}, status="importing")
    db.recover_on_start()
    assert db.get(rid)["status"] == "error" and "restart" in db.get(rid)["detail"]
    assert worker.scan_dropbox_once() == 1                         # not treated as a parked failure
    assert [r["status"] for r in db.list_for("alice", False)][0] == "done" and not os.path.exists(p)

def test_park_uses_the_folder_the_user_syncs(users):
    d = os.path.join(config.DROPBOX_DIR, "Alice"); os.makedirs(d)    # differently-cased folder for user 'alice'
    p = os.path.join(d, "x.exe"); open(p, "wb").write(b"x"); _old(p)
    worker.scan_dropbox_once()
    assert os.path.exists(os.path.join(d, ".failed", "x.exe")) and not os.path.exists(os.path.join(config.DROPBOX_DIR, "alice"))

# ---- PDF with XMP ------------------------------------------------------------------------------
XMP = b"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about=""
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
<dc:title><rdf:Alt><rdf:li xml:lang="x-default">XMP Book</rdf:li></rdf:Alt></dc:title>
<dc:subject><rdf:Bag><rdf:li>Fiction</rdf:li></rdf:Bag></dc:subject>
<xmp:MetadataDate>2030-01-01T00:00:00Z</xmp:MetadataDate></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""

def _pdf_with_xmp(path, xmp=XMP):
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "XMP Book", "/Keywords": "Fiction"})
    s = DecodedStreamObject(); s.set_data(xmp)
    s[NameObject("/Type")] = NameObject("/Metadata"); s[NameObject("/Subtype")] = NameObject("/XML")
    w._root_object[NameObject("/Metadata")] = w._add_object(s)
    with open(path, "wb") as f: w.write(f)
    return path

def _xmp_subjects(path):
    from lxml import etree
    r = PdfReader(path); md = r.trailer["/Root"].get("/Metadata")
    if md is None: return None
    x = etree.fromstring(md.get_object().get_data())
    return [li.text for li in x.iter("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li") if li.getparent().getparent().tag.endswith("subject")]

def test_pdf_owner_tag_goes_into_xmp_too(tmp_path):
    p = _pdf_with_xmp(str(tmp_path / "xmp.pdf"))
    tagger.add_owner_tag_pdf(p, "owner:alice")
    tagger.add_owner_tag_pdf(p, "owner:alice")                       # idempotent
    assert _xmp_subjects(p) == ["Fiction", "owner:alice"]
    assert "owner:alice" in str(PdfReader(p).metadata.get("/Keywords"))
    assert PdfReader(p).metadata.get("/Title") == "XMP Book"

def test_pdf_unparseable_xmp_is_dropped_so_info_wins(tmp_path):
    p = _pdf_with_xmp(str(tmp_path / "bad.pdf"), xmp=b"<not xml")
    tagger.add_owner_tag_pdf(p, "owner:alice")
    assert _xmp_subjects(p) is None and "owner:alice" in str(PdfReader(p).metadata.get("/Keywords"))

def test_pdf_without_xmp_untouched_path(tmp_path):
    p = make_pdf(str(tmp_path / "plain.pdf"), title="Plain", keywords="History")
    tagger.add_owner_tag_pdf(p, "owner:bob")
    assert _xmp_subjects(p) is None and PdfReader(p).metadata.get("/Keywords") == "History, owner:bob"

# ---- sessions survive a transient app.db error ---------------------------------------------------
def test_transient_db_error_does_not_revoke_sessions(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    with client.session_transaction() as s:
        s["chk"] = 0
    monkeypatch.setattr(config, "CWA_DB", "/nonexistent/app.db")     # e.g. bind mount briefly gone
    assert auth.fingerprint("alice") is auth.UNAVAILABLE
    assert client.get("/status").status_code == 200                   # still logged in
    assert "session_revoked" not in [a["event"] for a in db.audit_recent(5)]
    monkeypatch.undo()
    cwa.remove_user("alice")
    with client.session_transaction() as s:
        s["chk"] = 0
    assert client.get("/status").status_code == 302                   # a real removal still revokes

# ---- CBZ vs CWA's Kindle EPUB fixer; portal-side Kindle fixes -----------------------------------
def _cwa_settings_db(fixer):
    p = os.path.join(os.path.dirname(config.CWA_DB), "cwa.db")
    c = sqlite3.connect(p); c.execute("CREATE TABLE IF NOT EXISTS cwa_settings (kindle_epub_fixer INTEGER)")
    c.execute("DELETE FROM cwa_settings"); c.execute("INSERT INTO cwa_settings VALUES (?)", (fixer,)); c.commit(); c.close()
    return p

def test_kindle_fixer_flag_read_from_cwa_db():
    p = os.path.join(os.path.dirname(config.CWA_DB), "cwa.db")
    if os.path.exists(p): os.remove(p)
    assert cwa.kindle_fixer_on() is False                             # no cwa.db yet -> shipped default (off)
    _cwa_settings_db(1); assert cwa.kindle_fixer_on() is True
    _cwa_settings_db(0); assert cwa.kindle_fixer_on() is False
    os.remove(p)

def test_cbz_is_honest_when_cwa_fixer_would_strip_the_tag(users, tmp_path, monkeypatch):
    import zipfile as zf
    def comic(name):
        p = tmp_path / name
        with zf.ZipFile(p, "w") as z:
            z.writestr("001.png", b"\x89PNG"); z.writestr("002.png", b"\x89PNG")
        return str(p)
    monkeypatch.setattr(cwa, "kindle_fixer_on", lambda: False)
    note = worker.ingest_local_file(comic("Comic A.cbz"), "alice")
    assert note.startswith("tagged owner:alice") and worker._status_for(note) == "done"
    monkeypatch.setattr(cwa, "kindle_fixer_on", lambda: True)
    note = worker.ingest_local_file(comic("Comic B.cbz"), "alice")
    assert worker._status_for(note) == worker.NEEDS_TAG and "Kindle EPUB fixer" in note and "owner:alice" in note
    placed = [n for n in os.listdir(config.INGEST_DIR) if n.endswith(".cbz")]
    assert len(placed) == 2                                           # both still imported (the file is tagged either way)
    import json as _json
    for n in placed:
        assert "owner:alice" in _json.loads(zf.ZipFile(os.path.join(config.INGEST_DIR, n)).comment)["ComicBookInfo/1.0"]["tags"]

def _epub_without_language(path, with_decl=False):
    import zipfile as zf
    z = zf.ZipFile(path, "w")
    z.writestr(zf.ZipInfo("mimetype"), "application/epub+zip", compress_type=zf.ZIP_STORED)
    z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("OEBPS/content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="b" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>No Lang</dc:title><dc:identifier id="b">x</dc:identifier></metadata><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>')
    z.writestr("OEBPS/c.xhtml", ('<?xml version="1.0" encoding="utf-8"?>' if with_decl else "") + '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>é</p></body></html>')
    z.close(); return str(path)

def _attachment(msg):
    return next(a.get_payload(decode=True) for a in msg.iter_attachments())

def test_send_to_kindle_applies_language_and_encoding_fixes(tmp_path, monkeypatch):
    import zipfile as zf, io as _io
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    sent = []; monkeypatch.setattr(kindle, "_deliver", lambda m: sent.append(m))
    p = _epub_without_language(tmp_path / "nolang.epub")
    out = kindle.send("a@kindle.com", p, "No Lang", "nolang.epub")
    assert out == "sent to a@kindle.com; Kindle fixes applied: encoding, language"
    z = zf.ZipFile(_io.BytesIO(_attachment(sent[-1])))
    assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == zf.ZIP_STORED
    assert b"<dc:language>en</dc:language>" in z.read("OEBPS/content.opf")
    assert z.read("OEBPS/c.xhtml").startswith(b'<?xml version="1.0" encoding="utf-8"?>')
    assert b"dc:language" not in zf.ZipFile(p).read("OEBPS/content.opf")
    assert not [f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".epub")]   # temp copy removed
    # a well-formed EPUB goes out byte-for-byte
    good = make_epub(str(tmp_path / "good.epub"), title="Good")
    out = kindle.send("a@kindle.com", good, "Good", "good.epub")
    assert out == "sent to a@kindle.com" and _attachment(sent[-1]) == open(good, "rb").read()
    # something that is not a zip at all still goes out (fixes are best effort)
    bad = tmp_path / "bad.epub"; bad.write_bytes(b"not a zip")
    assert kindle.send("a@kindle.com", str(bad), "Bad", "bad.epub") == "sent to a@kindle.com" and _attachment(sent[-1]) == b"not a zip"
