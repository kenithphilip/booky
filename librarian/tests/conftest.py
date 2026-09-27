"""Test environment for the portal.

Every test runs against a real Calibre-Web Automated app.db schema and a real Calibre
metadata.db schema (both dumped from the actual containers, see fixtures/), in a fresh temp
directory. Environment is fixed before the application modules are imported."""
import os, sys, sqlite3, shutil, tempfile, zipfile, re
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)

BASE = tempfile.mkdtemp(prefix="librarian-tests-")
DIRS = {d: os.path.join(BASE, d) for d in ("cwa", "state", "ingest", "staging", "audiobooks", "dropbox", "library")}
os.environ.update({
    "CWA_DB": os.path.join(DIRS["cwa"], "app.db"),
    "STATE_DB": os.path.join(DIRS["state"], "librarian.db"),
    "INGEST_DIR": DIRS["ingest"], "STAGING_DIR": DIRS["staging"], "AUDIO_DIR": DIRS["audiobooks"],
    "DROPBOX_DIR": DIRS["dropbox"], "CALIBRE_DB": os.path.join(DIRS["library"], "metadata.db"),
    "LIBRARY_DIR": DIRS["library"],
    "DOMAIN": "example.test", "LIBRARIAN_SECRET": "test-secret", "COOKIE_SECURE": "false",
    "LIBRARIAN_NO_WORKER": "1", "INTAKE_TOKEN": "intake-token-123", "ENRICH_METADATA": "false",
    "DEDUPE_WARN": "true", "SMTP_HOST": "", "SMTP_FROM": "", "ABS_TOKEN": "", "APPROVALS_REQUIRED": "true",
})

import config, db, cwa  # noqa: E402  (after env)


def _reset_dirs():
    for d in DIRS.values():
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)


def make_cwa_db(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")          # CWA keeps app.db in WAL mode
    c.executescript(open(os.path.join(FIX, "cwa_app_schema.sql")).read())
    c.execute("INSERT INTO settings DEFAULT VALUES")
    c.commit(); c.close()


def calibre_conn(path):
    c = sqlite3.connect(path)
    # calibre registers these SQL functions in its own process; triggers call them
    c.create_function("title_sort", 1, lambda s: s)
    c.create_function("uuid4", 0, lambda: "00000000-0000-0000-0000-000000000000")
    return c


def make_calibre_db(path):
    c = calibre_conn(path)
    c.executescript(open(os.path.join(FIX, "calibre_metadata_schema.sql")).read())
    c.commit(); c.close()


def make_epub(path, title="Test Book", author="Test Author", subjects=()):
    """A minimal but valid EPUB 2 (mimetype first & stored, container.xml, OPF, one chapter)."""
    subj = "".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator>
<dc:language>en</dc:language><dc:identifier id="bookid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>{subj}
</metadata>
<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
<spine toc="ncx"><itemref idref="ch1"/></spine></package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="urn:uuid:11111111-2222-3333-4444-555555555555"/></head><docTitle><text>T</text></docTitle>
<navMap><navPoint id="n1" playOrder="1"><navLabel><text>Chapter 1</text></navLabel><content src="ch1.xhtml"/></navPoint></navMap></ncx>"""
    ch = """<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>1</title></head>
<body><h1>Chapter 1</h1><p>Hello, library.</p></body></html>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/ch1.xhtml", ch, compress_type=zipfile.ZIP_DEFLATED)
    return path


def make_pdf(path, title=None, keywords=None):
    """A one-page PDF with optional Info metadata (what a user would upload)."""
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    meta = {}
    if title: meta["/Title"] = title
    if keywords: meta["/Keywords"] = keywords
    if meta: w.add_metadata(meta)
    with open(path, "wb") as f:
        w.write(f)
    return path


def make_cbz(path, comment=None, pages=2):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i + 1:03d}.jpg", b"\xff\xd8\xff\xe0" + b"0" * 64)
        if comment is not None:
            z.comment = comment
    return path


def add_calibre_book(book_id, title, author, tags=(), formats=("epub",), lib_dir=None, md_path=None):
    """Insert a book the way calibre lays it out: <lib>/<Author>/<Title (id)>/<Title - Author>.<fmt>"""
    lib_dir = lib_dir or config.LIBRARY_DIR
    md_path = md_path or config.CALIBRE_DB
    rel = os.path.join(author, f"{title} ({book_id})")
    name = f"{title} - {author}"
    os.makedirs(os.path.join(lib_dir, rel), exist_ok=True)
    for f in formats:
        p = os.path.join(lib_dir, rel, f"{name}.{f}")
        if f == "epub":
            make_epub(p, title, author, subjects=tags)
        else:
            open(p, "wb").write(b"%PDF-1.4 fake\n" if f == "pdf" else b"data")
    c = calibre_conn(md_path)
    c.execute("INSERT INTO books(id,title,sort,timestamp,pubdate,series_index,author_sort,path,flags,uuid,has_cover,last_modified) "
              "VALUES(?,?,?,datetime('now'),datetime('now'),1.0,?,?,1,?,0,datetime('now'))",
              (book_id, title, title, author, rel, f"uuid-{book_id}"))
    a = c.execute("SELECT id FROM authors WHERE name=?", (author,)).fetchone()
    aid = a[0] if a else c.execute("INSERT INTO authors(name,sort) VALUES(?,?)", (author, author)).lastrowid
    c.execute("INSERT INTO books_authors_link(book,author) VALUES(?,?)", (book_id, aid))
    for t in tags:
        r = c.execute("SELECT id FROM tags WHERE name=?", (t,)).fetchone()
        tid = r[0] if r else c.execute("INSERT INTO tags(name) VALUES(?)", (t,)).lastrowid
        c.execute("INSERT INTO books_tags_link(book,tag) VALUES(?,?)", (book_id, tid))
    for f in formats:
        c.execute("INSERT INTO data(book,format,uncompressed_size,name) VALUES(?,?,?,?)", (book_id, f.upper(), 1000, name))
    c.commit(); c.close()
    return book_id


@pytest.fixture(autouse=True)
def fresh_env(monkeypatch):
    """Fresh CWA db, calibre db, state db and folders for every test; config restored."""
    # Offline by default: the metadata-first search must never reach Open Library from a test
    # (tests/test_bookmeta.py replaces this with the recorded real responses).
    import requests as _rq, bookmeta
    def _offline(*a, **k):
        raise _rq.ConnectionError("tests are offline")
    monkeypatch.setattr(bookmeta.requests, "get", _offline)
    for c in (bookmeta._SEARCH, bookmeta._RECORD, bookmeta._COPIES):
        c.clear()
    _reset_dirs()
    make_cwa_db(config.CWA_DB)
    make_calibre_db(config.CALIBRE_DB)
    db.init()
    yield DIRS


@pytest.fixture
def users():
    """admin / alice / bob in the CWA database (admin has no tag restriction)."""
    cwa.add_user("admin", "adminpass1", "admin@example.test", admin=True)
    cwa.add_user("alice", "alicepass1", "alice@example.test")
    cwa.add_user("bob", "bobpass1", "bob@example.test")
    return {"admin": "adminpass1", "alice": "alicepass1", "bob": "bobpass1"}


@pytest.fixture
def client(users):
    import app as appmod
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    m = CSRF_RE.search(html)
    assert m, f"no csrf field on {path}"
    return m.group(1)


def login(client, user, password):
    tok = csrf_of(client, "/login")
    r = client.post("/login", data={"username": user, "password": password, "csrf": tok})
    return r


def post(client, path, **data):
    """POST a form with the session's CSRF token (as the browser would)."""
    data.setdefault("csrf", csrf_of(client, "/status"))
    return client.post(path, data=data, follow_redirects=True)
