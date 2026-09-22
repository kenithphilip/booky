"""Read-only view of the Calibre library (metadata.db + files) scoped by the owner tag, so a
user can download their own books straight from the portal or send them to a Kindle.
Admins (no tag restriction in CWA) see everything, mirroring CWA's own behaviour."""
import os, sqlite3
import config

def _conn():
    return sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=10)

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

def count_for(owner, is_admin=False, q=""):
    scope, params = _scope_sql(owner, is_admin)
    search, sparams = _search_sql(q)
    try:
        with _conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM books b WHERE 1=1 {scope} {search}", (*params, *sparams)).fetchone()[0]
    except Exception:
        return 0

def books_for(owner, is_admin=False, limit=PAGE, offset=0, q=""):
    """[{id,title,author,formats:[...],added}] visible to this user, newest first; `q` filters
    on title/author, `offset` pages through big libraries."""
    scope, params = _scope_sql(owner, is_admin)
    search, sparams = _search_sql(q)
    try:
        with _conn() as c:
            rows = c.execute(f"""
                SELECT b.id, b.title, b.timestamp,
                       (SELECT group_concat(a.name, ' & ') FROM books_authors_link al
                          JOIN authors a ON a.id=al.author WHERE al.book=b.id) AS author,
                       (SELECT group_concat(lower(d.format)) FROM data d WHERE d.book=b.id) AS formats,
                       (SELECT group_concat(t.name) FROM books_tags_link l JOIN tags t ON t.id=l.tag
                          WHERE l.book=b.id AND t.name LIKE ?) AS owners
                FROM books b WHERE 1=1 {scope} {search} ORDER BY b.timestamp DESC, b.id DESC LIMIT ? OFFSET ?""",
                (f"{config.OWNER_PREFIX}%", *params, *sparams, limit, max(0, offset))).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        fmts = sorted(set((r[4] or "").split(","))) if r[4] else []
        out.append({"id": r[0], "title": r[1], "added": (r[2] or "")[:10], "author": r[3] or "Unknown",
                    "formats": [f for f in fmts if f],
                    "owners": [o[len(config.OWNER_PREFIX):] for o in (r[5] or "").split(",") if o]})
    return out

def visible(owner, book_id, is_admin=False):
    """True if this user may see the book at all (regardless of formats)."""
    scope, params = _scope_sql(owner, is_admin)
    try:
        with _conn() as c:
            return c.execute(f"SELECT 1 FROM books b WHERE b.id=? {scope}", (book_id, *params)).fetchone() is not None
    except Exception:
        return False

def file_for(owner, book_id, fmt, is_admin=False):
    """Absolute path of one book file if the user may see the book, else None. KEPUB (which
    CWA creates on Kobo sync and keeps as a format) is served as <name>.kepub.epub so other
    readers and Kobo side-loading recognise it."""
    scope, params = _scope_sql(owner, is_admin)
    fmt = (fmt or "").lower()
    if fmt not in config.DOWNLOAD_FORMATS:
        return None
    try:
        with _conn() as c:
            r = c.execute(f"""SELECT b.path, d.name, d.format, b.title FROM books b JOIN data d ON d.book=b.id
                              WHERE b.id=? AND lower(d.format)=? {scope}""",
                          (book_id, fmt, *params)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    path = os.path.join(config.LIBRARY_DIR, r[0], f"{r[1]}.{r[2].lower()}")
    # never follow anything that escapes the library root
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(config.LIBRARY_DIR) + os.sep) or not os.path.isfile(real):
        return None
    filename = f"{r[1]}.kepub.epub" if fmt == "kepub" else f"{r[1]}.{fmt}"
    return {"path": real, "title": r[3], "format": fmt, "filename": filename,
            "mimetype": MIMETYPES.get(fmt, "application/octet-stream")}

def best_format(book, preferred):
    """Pick the file to hand out: preferred if present, else epub, else the first available."""
    fmts = book.get("formats") or []
    if preferred in fmts:
        return preferred
    if "epub" in fmts:
        return "epub"
    return fmts[0] if fmts else None
