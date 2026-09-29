"""Read-only view of the Calibre library (metadata.db + files) scoped by the owner tag, so a
user can download their own books straight from the portal or send them to a Kindle.
Admins (no tag restriction in CWA) see everything, mirroring CWA's own behaviour."""
import os, sqlite3
import config

# Set when a read had to fall back to immutable=1, so callers and /healthz can say that the
# answer may be stale instead of quietly serving an old catalogue. Cleared on the next good read.
STALE_READ = [None]

def _conn():
    """Open Calibre's metadata.db read-only, correctly, in both states it is really found in.

    Measured on a read-only bind mount (the librarian service mounts ./library/books :ro):
      * CWA running, non-empty WAL  -> mode=ro WORKS and sees the WAL; immutable=1 silently
        returns the pre-WAL contents, i.e. the newest books simply missing.
      * a -wal present with no -shm (an unclean stop: OOM kill, docker kill, the 04:30 reboot)
        -> mode=ro fails 'unable to open database file'; immutable=1 works.
    So neither mode is right alone. mode=ro first because it is the correct one; immutable=1
    only as a fallback, and never silently — reading stale data and calling it the library is
    how 'My books' would show the wrong thing with every monitor green."""
    try:
        c = sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=10)
        c.execute("SELECT 1 FROM books LIMIT 1")      # force the open; a lazy connect hides it
        STALE_READ[0] = None
        return c
    except sqlite3.OperationalError as e:
        c = sqlite3.connect(f"file:{config.CALIBRE_DB}?immutable=1", uri=True, timeout=10)
        STALE_READ[0] = (f"the library catalogue could not be opened normally ({e}); it is being "
                         f"read in immutable mode, which ignores Calibre's write-ahead log, so "
                         f"very recent books may be missing until Calibre-Web is restarted")
        return c

def _scope_sql(owner, is_admin):
    if is_admin:
        return "", ()
    return ("AND EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag "
            "WHERE l.book=b.id AND t.name=?)", (f"{config.OWNER_PREFIX}{owner}",))

PAGE = 300
MIMETYPES = {"kepub": "application/kepub+zip", "epub": "application/epub+zip", "pdf": "application/pdf",
             "cbz": "application/vnd.comicbook+zip", "azw3": "application/vnd.amazon.ebook",
             "mobi": "application/x-mobipocket-ebook", "txt": "text/plain", "fb2": "application/x-fictionbook+xml"}

def _search_sql(q):
    if not q:
        return "", ()
    like = f"%{q.lower()}%"
    return ("AND (lower(b.title) LIKE ? OR EXISTS (SELECT 1 FROM books_authors_link al JOIN authors a "
            "ON a.id=al.author WHERE al.book=b.id AND lower(a.name) LIKE ?))", (like, like))

COMIC_TAGS = ("Comics", "Manga", "Manhwa", "Manhua")
SORTS = {"added": "b.timestamp DESC, b.id DESC", "title": "b.sort COLLATE NOCASE, b.id",
         "author": "b.author_sort COLLATE NOCASE, b.sort COLLATE NOCASE",
         "series": "(SELECT s.sort FROM books_series_link l JOIN series s ON s.id=l.series WHERE l.book=b.id) IS NULL, "
                   "(SELECT s.sort FROM books_series_link l JOIN series s ON s.id=l.series WHERE l.book=b.id) COLLATE NOCASE, "
                   "b.series_index, b.sort COLLATE NOCASE"}

def _filter_sql(kind=None, only=None, exclude=None):
    """v6.0 My books filters: comics or books only, and a set of ids to keep or to leave out
    (the reading-status filters, from Calibre-Web's reading state)."""
    sql, params = "", []
    if kind in ("comics", "books"):
        marks = ",".join("?" * len(COMIC_TAGS))
        sql += (" AND " if kind == "comics" else " AND NOT ") + (
            f"EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=b.id AND t.name IN ({marks}))")
        params += COMIC_TAGS
    if only is not None:
        ids = list(only)[:30000] or [-1]      # SQLite takes 32766 host parameters
        sql += f" AND b.id IN ({','.join('?' * len(ids))})"
        params += ids
    if exclude:
        ids = list(exclude)[:30000]
        sql += f" AND b.id NOT IN ({','.join('?' * len(ids))})"
        params += ids
    return sql, tuple(params)

def count_for(owner, is_admin=False, q="", kind=None, only=None, exclude=None):
    scope, params = _scope_sql(owner, is_admin)
    search, sparams = _search_sql(q)
    filt, fparams = _filter_sql(kind, only, exclude)
    try:
        with _conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM books b WHERE 1=1 {scope} {search} {filt}",
                             (*params, *sparams, *fparams)).fetchone()[0]
    except Exception:
        return 0

def books_for(owner, is_admin=False, limit=PAGE, offset=0, q="", kind=None, only=None, exclude=None, sort="added"):
    """[{id,title,author,formats:[...],added,series,index}] visible to this user, newest first
    (or by `sort`); `q` filters on title/author, `offset` pages through big libraries."""
    scope, params = _scope_sql(owner, is_admin)
    search, sparams = _search_sql(q)
    filt, fparams = _filter_sql(kind, only, exclude)
    order = SORTS.get(sort, SORTS["added"])
    try:
        with _conn() as c:
            rows = c.execute(f"""
                SELECT b.id, b.title, b.timestamp,
                       (SELECT group_concat(a.name, ' & ') FROM books_authors_link al
                          JOIN authors a ON a.id=al.author WHERE al.book=b.id) AS author,
                       (SELECT group_concat(lower(d.format)) FROM data d WHERE d.book=b.id) AS formats,
                       (SELECT group_concat(t.name) FROM books_tags_link l JOIN tags t ON t.id=l.tag
                          WHERE l.book=b.id AND t.name LIKE ?) AS owners,
                       (SELECT s.name FROM books_series_link l JOIN series s ON s.id=l.series WHERE l.book=b.id) AS series,
                       b.series_index
                FROM books b WHERE 1=1 {scope} {search} {filt} ORDER BY {order} LIMIT ? OFFSET ?""",
                (f"{config.OWNER_PREFIX}%", *params, *sparams, *fparams, limit, max(0, offset))).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        fmts = sorted(set((r[4] or "").split(","))) if r[4] else []
        out.append({"id": r[0], "title": r[1], "added": (r[2] or "")[:10], "author": r[3] or "Unknown",
                    "formats": [f for f in fmts if f],
                    "owners": [o[len(config.OWNER_PREFIX):] for o in (r[5] or "").split(",") if o],
                    "series": r[6], "index": r[7]})
    return out

def title_of(owner, book_id, is_admin=False):
    """(title, author) of a book this user may see, or None (a placeholder cover's text)."""
    scope, params = _scope_sql(owner, is_admin)
    try:
        with _conn() as c:
            r = c.execute(f"SELECT b.title, (SELECT group_concat(a.name, ' & ') FROM books_authors_link l "
                          f"JOIN authors a ON a.id = l.author WHERE l.book = b.id) FROM books b WHERE b.id=? {scope}",
                          (book_id, *params)).fetchone()
    except Exception:
        return None
    return (r[0] or "", r[1] or "") if r else None

def visible(owner, book_id, is_admin=False):
    """True if this user may see the book at all (regardless of formats)."""
    scope, params = _scope_sql(owner, is_admin)
    try:
        with _conn() as c:
            return c.execute(f"SELECT 1 FROM books b WHERE b.id=? {scope}", (book_id, *params)).fetchone() is not None
    except Exception:
        return False

def file_for(owner, book_id, fmt, is_admin=False):
    """Absolute path of one book file if the user may see the book, else None. A KEPUB, if the
    library ever holds one, is served as <name>.kepub.epub so other readers and Kobo
    side-loading recognise it. Nothing in this stack produces one today — CWA v4.0.6 cannot
    find its own kepubify binary, so Kobo sync ships plain EPUB (see config.DOWNLOAD_FORMATS);
    this path is here for a library where an admin pre-generated them."""
    scope, params = _scope_sql(owner, is_admin)
    fmt = (fmt or "").lower()
    if fmt not in config.DOWNLOAD_FORMATS:
        return None
    try:
        with _conn() as c:
            r = c.execute(f"""SELECT b.path, d.name, d.format, b.title,
                              (SELECT group_concat(a.name, ' & ') FROM books_authors_link al
                               JOIN authors a ON a.id = al.author WHERE al.book = b.id)
                              FROM books b JOIN data d ON d.book=b.id
                              WHERE b.id=? AND lower(d.format)=? {scope}""",
                          (book_id, fmt, *params)).fetchone()
    except Exception:
        return None
    if not r:
        if fmt == "kepub" and config.KEPUBIFY:
            return _kepub_from_epub(owner, book_id, is_admin)
        return None
    path = os.path.join(config.LIBRARY_DIR, r[0], f"{r[1]}.{r[2].lower()}")
    # never follow anything that escapes the library root
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(config.LIBRARY_DIR) + os.sep) or not os.path.isfile(real):
        return None
    filename = f"{r[1]}.kepub.epub" if fmt == "kepub" else f"{r[1]}.{fmt}"
    return {"path": real, "title": r[3], "authors": r[4] or "", "format": fmt, "filename": filename,
            "mimetype": MIMETYPES.get(fmt, "application/octet-stream")}

_KEPUB_LOCK = __import__("threading").Semaphore(1)   # one conversion at a time on 2 cores

def _kepub_from_epub(owner, book_id, is_admin):
    """The book's EPUB converted to KEPUB for a Kobo reader who downloads it (L11). Cached by
    book id and the EPUB's own mtime, so a re-converted book is never served stale; the cache
    is trimmed oldest-first to KEPUB_CACHE_MB. Visibility is the EPUB's: same owner scope."""
    import subprocess, tempfile
    src = file_for(owner, book_id, "epub", is_admin)
    if not src:
        return None
    os.makedirs(config.KEPUB_CACHE_DIR, exist_ok=True)
    key = f"{book_id}-{int(os.path.getmtime(src['path']))}.kepub.epub"
    out = os.path.join(config.KEPUB_CACHE_DIR, key)
    if not os.path.isfile(out):
        with _KEPUB_LOCK:
            if not os.path.isfile(out):
                with tempfile.TemporaryDirectory(dir=config.KEPUB_CACHE_DIR) as tmp:
                    r = subprocess.run([config.KEPUBIFY, "--output", tmp, src["path"]],
                                       capture_output=True, timeout=180)
                    made = [f for f in os.listdir(tmp) if f.endswith(".kepub.epub")]
                    if r.returncode != 0 or not made:
                        return None
                    os.replace(os.path.join(tmp, made[0]), out)
        _trim_kepub_cache()
    base = src["filename"][: -len(".epub")] if src["filename"].endswith(".epub") else src["filename"]
    return dict(src, path=out, format="kepub", filename=f"{base}.kepub.epub", mimetype=MIMETYPES["kepub"])

def _trim_kepub_cache():
    d = config.KEPUB_CACHE_DIR
    files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".kepub.epub")),
                   key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    while files and total > config.KEPUB_CACHE_MB * 1024 * 1024:
        f = files.pop(0)
        total -= os.path.getsize(f)
        os.remove(f)

def best_format(book, preferred):
    """Pick the file to hand out: preferred if present, else epub, else the first available.
    A KEPUB preference is met from the EPUB when the portal can convert (L11)."""
    fmts = book.get("formats") or []
    if preferred in fmts:
        return preferred
    if preferred == "kepub" and config.KEPUBIFY and "epub" in fmts:
        return "kepub"
    if "epub" in fmts:
        return "epub"
    return fmts[0] if fmts else None

# ---- book / author pages ---------------------------------------------------------------------
_TAGS = __import__("re").compile(r"<[^>]+>")

def _plain(html):
    """Calibre stores comments as HTML, and they arrive from providers and from the files
    themselves — untrusted either way. Rendered as plain text, never as markup."""
    import html as _h
    return _h.unescape(_TAGS.sub(" ", html or "")).strip()

def book_detail(owner, book_id, is_admin=False):
    """Everything Calibre knows about one book this reader may see, or None. Calibre is the
    authority here — it holds what the metadata push wrote AND anything a family member fixed
    by hand in Calibre-Web — so the portal's own metadata only fills what Calibre lacks."""
    scope, params = _scope_sql(owner, is_admin)
    try:
        with _conn() as c:
            r = c.execute(f"""SELECT b.id, b.title, b.pubdate, b.series_index, b.path, b.has_cover
                              FROM books b WHERE b.id=? {scope}""", (book_id, *params)).fetchone()
            if not r:
                return None
            authors = [a for (a,) in c.execute(
                "SELECT a.name FROM books_authors_link l JOIN authors a ON a.id=l.author "
                "WHERE l.book=? ORDER BY l.id", (book_id,))]
            formats = sorted({f.lower() for (f,) in c.execute(
                "SELECT format FROM data WHERE book=?", (book_id,))})
            series = c.execute("SELECT s.name FROM books_series_link l JOIN series s ON s.id=l.series "
                               "WHERE l.book=?", (book_id,)).fetchone()
            pub = c.execute("SELECT p.name FROM books_publishers_link l JOIN publishers p "
                            "ON p.id=l.publisher WHERE l.book=?", (book_id,)).fetchone()
            langs = [x for (x,) in c.execute(
                "SELECT lg.lang_code FROM books_languages_link l JOIN languages lg ON lg.id=l.lang_code "
                "WHERE l.book=?", (book_id,))]
            com = c.execute("SELECT text FROM comments WHERE book=?", (book_id,)).fetchone()
            owners = [t[len(config.OWNER_PREFIX):] for (t,) in c.execute(
                "SELECT t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag "
                "WHERE l.book=? AND t.name LIKE ?", (book_id, f"{config.OWNER_PREFIX}%"))]
    except sqlite3.Error:
        return None
    pubdate = (r[2] or "")[:10]
    return {"id": r[0], "title": r[1], "authors": authors, "formats": formats,
            # calibre stores an unknown date as year 0101
            "published": "" if pubdate.startswith("0101") else pubdate,
            "series": series[0] if series else None, "series_index": r[3],
            "publisher": pub[0] if pub else None, "languages": langs,
            "description": _plain(com[0]) if com else "",
            "has_cover": bool(r[5]), "path": r[4],
            "owners": owners if is_admin else []}      # a reader never sees who else has a book

def cover_path(owner, book_id, is_admin=False):
    """Calibre's own cover.jpg for a book this reader may see — local, so the browser never
    contacts a third party for it, and confined to the library root."""
    d = book_detail(owner, book_id, is_admin)
    if not d or not d["has_cover"]:
        return None
    p = os.path.realpath(os.path.join(config.LIBRARY_DIR, d["path"], "cover.jpg"))
    root = os.path.realpath(config.LIBRARY_DIR) + os.sep
    return p if p.startswith(root) and os.path.isfile(p) else None

def books_by_author(owner, name, is_admin=False, limit=200):
    """This reader's own books whose authors include `name` (case-insensitive)."""
    scope, params = _scope_sql(owner, is_admin)
    try:
        with _conn() as c:
            rows = c.execute(f"""SELECT DISTINCT b.id, b.title, b.series_index,
                                 (SELECT s.name FROM books_series_link sl JOIN series s ON s.id=sl.series
                                  WHERE sl.book=b.id)
                                 FROM books b JOIN books_authors_link l ON l.book=b.id
                                 JOIN authors a ON a.id=l.author
                                 WHERE lower(a.name)=lower(?) {scope}
                                 ORDER BY b.title LIMIT ?""", (name, *params, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r[0], "title": r[1], "series_index": r[2], "series": r[3]} for r in rows]

def visible_ids(owner, ids, is_admin=False):
    """The subset of Calibre ids this reader may see — one query, not one per id."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return set()
    scope, params = _scope_sql(owner, is_admin)
    marks = ",".join("?" * len(ids))
    try:
        with _conn() as c:
            return {r[0] for r in c.execute(
                f"SELECT b.id FROM books b WHERE b.id IN ({marks}) {scope}", (*ids, *params))}
    except sqlite3.Error:
        return set()
