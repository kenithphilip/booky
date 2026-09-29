"""A reader's home page on the portal (v6.0): what they are reading and listening to, the next
book in each series they are reading, and what arrived lately, above the search.

Built from what the stack already records, read-only, each part on its own so one slow or
missing source never costs the page: Calibre-Web's reading state (the Kobo, KOReader, the web
reader, marks made on the portal), Audiobookshelf's listening progress, and the reader's own
books in Calibre (an admin's home shows their OWN books, not the whole library)."""
import logging, sqlite3, time
import cwa, library
import abs as absapi

log = logging.getLogger("home")


def _mine(owner):
    """[{id, title, series, index}] of the reader's own books (owner tag), with their series."""
    scope, params = library._scope_sql(owner, False)
    try:
        c = library._conn()
    except sqlite3.Error:
        return []
    try:
        rows = c.execute(f"""SELECT b.id, b.title, s.name AS series, b.series_index AS idx
            FROM books b LEFT JOIN books_series_link l ON l.book=b.id LEFT JOIN series s ON s.id=l.series
            WHERE 1=1 {scope}""", params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()
    return [{"id": r[0], "title": r[1], "series": r[2], "index": r[3]} for r in rows]


def reading(owner, books=None, limit=8):
    """Books in progress, most advanced first: [{id, title, pct}]."""
    try:
        state = cwa.reading_state(owner)
    except Exception:
        return []
    mine = {b["id"]: b for b in (books if books is not None else _mine(owner))}
    out = [dict(mine[bid], pct=s.get("pct")) for bid, s in state.items() if s.get("status") == "reading" and bid in mine]
    return sorted(out, key=lambda b: -(b.get("pct") or 0))[:limit]


def next_in_series(owner, books=None, limit=6):
    """For each series the reader has finished at least one book of: the next one they have and
    have not read. [{id, title, series, index}]"""
    books = books if books is not None else _mine(owner)
    try:
        state = cwa.reading_state(owner)
    except Exception:
        state = {}
    series = {}
    for b in books:
        if b["series"] and b["index"] is not None:
            series.setdefault(b["series"], []).append(b)
    out = []
    for name, bs in series.items():
        bs.sort(key=lambda b: b["index"])
        read = [b["index"] for b in bs if (state.get(b["id"]) or {}).get("status") == "read"]
        if not read:
            continue
        nxt = next((b for b in bs if b["index"] > max(read) and (state.get(b["id"]) or {}).get("status") != "read"), None)
        if nxt:
            out.append(dict(nxt, series=name))
    return sorted(out, key=lambda b: b["series"].lower())[:limit]


_CACHE = {}                         # owner -> (at, listening); item id -> (at, meta); "users" -> (at, {name: id})
TTL = 60
META_TTL = 3600


def _cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    if len(_CACHE) > 2000:                           # bounded: the oldest half goes
        for k, _v in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:1000]:
            _CACHE.pop(k, None)
    return val


def listening(owner, limit=6):
    """Audiobooks in progress: [{id, title, author, pct, left_min}], most recent first. Cached a
    minute per reader (and each book's title an hour), so the home page costs Audiobookshelf at
    most a few calls a minute, never one per view; one unreadable item is skipped, not fatal."""
    if not absapi.configured():
        return []
    try:
        return _cached(("listening", owner), TTL, lambda: _listening(owner, limit))
    except Exception as e:                       # Audiobookshelf down never costs the page
        log.debug("home: listening for %s: %s", owner, e)
        _CACHE[("listening", owner)] = (time.time(), [])   # and is not asked again on every view
        return []


def _listening(owner, limit):
    users = _cached("users", 600, lambda: {(u.get("username") or "").lower(): u.get("id") for u in absapi.list_users()})
    uid = users.get(owner.lower())
    if not uid:
        return []
    prog = [p for p in absapi.progress(uid) if not p.get("isFinished") and (p.get("currentTime") or 0) > 0]
    prog.sort(key=lambda p: -(p.get("lastUpdate") or 0))
    out = []
    for p in prog[:limit]:
        try:
            meta = _cached(("meta", p["libraryItemId"]), META_TTL, lambda i=p["libraryItemId"]: absapi.item_meta(i))
        except Exception:
            continue
        dur = p.get("duration") or meta.get("duration") or 0
        out.append({"id": p["libraryItemId"], "title": meta["title"], "author": meta["author"],
                    "pct": round(100 * (p.get("progress") or 0)),
                    "left_min": int((dur - (p.get("currentTime") or 0)) // 60) if dur else None})
    return out


def recent(owner, limit=8):
    return library.books_for(owner, False, limit=limit)


def build(owner):
    books = _mine(owner)
    return {"reading": reading(owner, books), "next": next_in_series(owner, books),
            "listening": listening(owner), "recent": recent(owner)}
