"""Hardcover (hardcover.app) for following BOOK series and authors (follows.py, v5.8).

Only with HARDCOVER_API_KEY (Library -> Metadata sources; free at hardcover.app -> Settings ->
API). Its GraphQL API has what the Goodreads mirror does not: a series' books with their release
dates, and an author's books, including ones announced but not out yet. Query shapes follow
Calibre-Web's own Hardcover provider (cps/metadata_provider/hardcover.py) and Hardcover's schema
docs: search(query, query_type: "Series" | "Author"), series.book_series { position book },
books.release_date / compilation / canonical_id. Compilations (omnibuses, box sets) and
duplicate records are left out."""
import json
import requests
import config

URL = "https://api.hardcover.app/v1/graphql"
TIMEOUT = (5, 30)


class HardcoverError(Exception):
    pass


def configured():
    return bool(config.HARDCOVER_API_KEY)


def _q(query, variables):
    if not configured():
        raise HardcoverError("no Hardcover API key (Library -> Metadata sources)")
    token = config.HARDCOVER_API_KEY.replace("Bearer ", "").strip()
    try:
        r = requests.post(URL, json={"query": query, "variables": variables}, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                   "User-Agent": "bookstack-librarian (self-hosted family library)"})
    except requests.RequestException as e:
        raise HardcoverError(f"Hardcover did not answer ({type(e).__name__})") from e
    if r.status_code in (401, 403):
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
    return out


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
        if b.get("compilation") or b.get("canonical_id") or not b.get("id"):
            continue
        books.append(_book(b, bs.get("position")))
    return s.get("name") or "", bool(s.get("is_completed")), books


def author_books(author_id, limit=60):
    """(author name, [book]) — the author's books, newest first, no compilations or duplicates."""
    d = _q("""query A($id: Int!, $n: Int!) { authors(where: {id: {_eq: $id}}) { name }
                books(where: {contributions: {author: {id: {_eq: $id}}}, compilation: {_eq: false}, canonical_id: {_is_null: true}},
                      order_by: {release_date: desc_nulls_last}, limit: $n) { id title release_date contributions { author { name } } } }""",
           {"id": int(author_id), "n": limit})
    a = (d.get("authors") or [{}])
    return (a[0].get("name") if a else "") or "", [_book(b) for b in d.get("books") or []]


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
