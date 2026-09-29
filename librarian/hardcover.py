"""Hardcover (hardcover.app) for following BOOK series and authors (follows.py, v5.8).

Only with HARDCOVER_API_KEY (Library -> Metadata sources; free at hardcover.app -> Settings ->
API). Its GraphQL API has what the Goodreads mirror does not: a series' books with their release
dates, and an author's books, including ones announced but not out yet. Query shapes follow
Calibre-Web's own Hardcover provider (cps/metadata_provider/hardcover.py) and Hardcover's schema
docs: search(query, query_type: "Series" | "Author"), series.book_series { position book },
books.release_date / compilation / canonical_id. Compilations (omnibuses, box sets) and
duplicate records are left out."""
import datetime, json, time
import requests
import config

URL = "https://api.hardcover.app/v1/graphql"
TIMEOUT = (10, 30)
RETRY_SLEEP = 3              # one more try when the connection itself failed (a ConnectTimeout, live 2026-09-29)


class HardcoverError(Exception):
    pass


def configured():
    return bool(config.HARDCOVER_API_KEY)


class TokenRefused(HardcoverError):
    """A reader's own token was refused (v5.9.1, hcaudio.py): theirs to fix, not the admin's."""


def _q(query, variables, token=None):
    """One GraphQL call with the admin's key, or with `token` (a reader's own, hcaudio.py)."""
    if not (token or configured()):
        raise HardcoverError("no Hardcover API key (Library -> Metadata sources)")
    own = bool(token)
    token = (token or config.HARDCOVER_API_KEY).replace("Bearer ", "").strip()
    for attempt in (1, 2):
        try:
            r = requests.post(URL, json={"query": query, "variables": variables}, timeout=TIMEOUT,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                       "User-Agent": "bookstack-librarian (self-hosted family library)"})
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == 2:
                raise HardcoverError(f"Hardcover did not answer ({type(e).__name__})") from e
            time.sleep(RETRY_SLEEP)
        except requests.RequestException as e:
            raise HardcoverError(f"Hardcover did not answer ({type(e).__name__})") from e
    if r.status_code in (401, 403):
        if own:
            raise TokenRefused(f"Hardcover refused your token (HTTP {r.status_code}); a token made since "
                               f"August 2026 needs the read:library and write:library permissions")
        raise HardcoverError(f"Hardcover refused the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        raise HardcoverError("Hardcover asked us to slow down")
    if r.status_code != 200:
        raise HardcoverError(f"Hardcover answered HTTP {r.status_code}")
    d = r.json() or {}
    if d.get("errors"):
        raise HardcoverError("Hardcover: " + "; ".join(str(e.get("message")) for e in d["errors"])[:200])
    return d.get("data") or {}


def _hits(results):
    """search()'s `results` is Typesense's answer: {hits: [{document: {...}}]}, sometimes as JSON text."""
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except ValueError:
            return []
    return [h.get("document") or {} for h in (results or {}).get("hits") or []]


def search(query, kind="Series", limit=10):
    """[{id, name, author, books_count, books}] for a series or an author name."""
    d = _q("query S($q: String!, $t: String!, $n: Int!) { search(query: $q, query_type: $t, per_page: $n, page: 1) { results } }",
           {"q": query, "t": kind, "n": limit})
    out = []
    for doc in _hits((d.get("search") or {}).get("results")):
        if doc.get("id") is None:
            continue
        out.append({"id": str(doc["id"]), "name": doc.get("name") or "",
                    "author": doc.get("author_name") or "", "books_count": doc.get("primary_books_count") or doc.get("books_count"),
                    "books": [b for b in (doc.get("books") or []) if isinstance(b, str)][:6]})
    # the real series first: a search for 'Discworld' answered a 4-book stray by 'Unknown' before
    # Pratchett's 41-book series (measured 2026-09-29)
    return sorted(out, key=lambda r: -(r["books_count"] or 0))


def _book(b, position=None):
    authors = [((c.get("author") or {}).get("name")) for c in (b.get("contributions") or [])]
    return {"id": str(b.get("id")), "title": b.get("title") or "", "date": b.get("release_date"),
            "position": position, "author": next((a for a in authors if a), "")}


def series_books(series_id):
    """(series name, completed?, [book]) — its books in order, no compilations or duplicates."""
    d = _q("""query B($id: Int!) { series(where: {id: {_eq: $id}}) { name is_completed
                book_series(order_by: {position: asc_nulls_last}) { position
                  book { id title release_date compilation canonical_id contributions { author { name } } } } } }""",
           {"id": int(series_id)})
    rows = d.get("series") or []
    if not rows:
        raise HardcoverError("Hardcover has no such series")
    s = rows[0]
    books = []
    for bs in s.get("book_series") or []:
        b = bs.get("book") or {}
        if b.get("compilation") or b.get("canonical_id") or not b.get("id") or not (b.get("title") or "").strip():
            continue                             # omnibuses, duplicates, and untitled stray records
        books.append(_book(b, bs.get("position")))
    return s.get("name") or "", bool(s.get("is_completed")), books


AUTHOR_MIN_READERS = 5


def author_books(author_id, limit=60):
    """(author name, [book]) — the author's RELEASED books, newest first, no compilations or
    duplicates, and only those at least AUTHOR_MIN_READERS people shelved: an author's record also
    holds game supplements, split editions and handbooks with one or two readers, and placeholders
    dated 2035 (measured 2026-09-29). A genuinely new book below the mark is noticed a day or two
    later, when it passes it."""
    d = _q("""query A($id: Int!, $n: Int!, $t: date!, $r: Int!) { authors(where: {id: {_eq: $id}}) { name }
                books(where: {contributions: {author: {id: {_eq: $id}}}, compilation: {_eq: false}, canonical_id: {_is_null: true},
                              release_date: {_lte: $t}, users_count: {_gte: $r}},
                      order_by: {release_date: desc_nulls_last}, limit: $n) { id title release_date contributions { author { name } } } }""",
           {"id": int(author_id), "n": limit, "t": datetime.date.today().isoformat(), "r": AUTHOR_MIN_READERS})
    a = (d.get("authors") or [{}])
    return (a[0].get("name") if a else "") or "", [_book(b) for b in d.get("books") or [] if (b.get("title") or "").strip()]


def editions(book_id):
    """[{isbns: [...], language: 'en'}] of one book's editions (measured 2026-09-29: editions.
    isbn_13 / isbn_10 / language.code2). The arrival check uses them: a file whose ISBN is one of
    the book's editions in the reader's language is the book, whatever title its file carries.
    Cached 30 days; [] when Hardcover cannot say."""
    import db
    ck = f"hardcover:editions:{book_id}"
    hit = db.cache_get(ck, 30 * 86400)
    if hit is not None:
        return hit
    try:
        d = _q("query E($id: Int!) { editions(where: {book_id: {_eq: $id}}, limit: 200) { isbn_13 isbn_10 language { code2 } } }",
               {"id": int(book_id)})
    except (HardcoverError, ValueError):
        return []
    out = [{"isbns": [i for i in (e.get("isbn_13"), e.get("isbn_10")) if i],
            "language": ((e.get("language") or {}).get("code2") or "").lower()} for e in d.get("editions") or []]
    out = [e for e in out if e["isbns"]]
    db.cache_put(ck, out, keep_days=30)
    return out


PAGE_CACHE = 6 * 3600


def cached(what, key):
    """For the portal's pages: [name, completed, books] of a series or [name, books] of an author,
    from the portal's cache (6 h), or its last answer when Hardcover is down. The daily follow
    checks ask Hardcover itself."""
    import db
    ck = f"hardcover:{what}:{key}"
    hit = db.cache_get(ck, PAGE_CACHE)
    if hit is not None:
        return hit
    try:
        val = list(series_books(key) if what == "series" else author_books(key))
    except HardcoverError:
        old = db.cache_get(ck, None)
        if old is not None:
            return old
        raise
    db.cache_put(ck, val, keep_days=30)
    return val


def check():
    """For the TUI: does the key work, and do the series/author queries answer?"""
    hits = search("Harry Potter", "Series", 1)
    if not hits:
        raise HardcoverError("the search answered, but found nothing for 'Harry Potter'")
    name, _done, books = series_books(hits[0]["id"])
    return {"series": name, "books": len(books)}


if __name__ == "__main__":
    import sys
    try:
        print(json.dumps({"ok": True, **check()}))
    except HardcoverError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
