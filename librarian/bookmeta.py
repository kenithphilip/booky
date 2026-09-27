"""Metadata-first search: find the BOOK first, then its copies.

The search page used to type the reader's words into each download catalogue and hope. Readarr,
Shelfmark (its default 'universal' mode) and every serious tool do it the other way round: ask a
metadata service which book is meant, let the reader pick it, then look for copies of THAT book
and check each one against it. This module is that first half plus the link to the second.

Open Library is the source, for one reason nobody else has: its search index records, per work,
the ids of the same book in the catalogues this stack downloads from —
    id_project_gutenberg, id_librivox, id_standard_ebooks, ia (Internet Archive), plus ISBNs.
(Measured 2026-09-26: 'Pride and Prejudice' OL66554W -> Gutenberg 1342/42671/45186, LibriVox
253/969/…, Standard Ebooks jane-austen/pride-and-prejudice.) So a chosen book resolves DIRECTLY to
downloadable copies; the keyword search is only the fallback. It is keyless, answers in well
under a second, and asks for a contact in the User-Agent, which metadata.UA carries.

Every copy found is verified before it is offered (matching.distance): the cross-links are
honest but not precise — 45186 above is the Finnish translation — so each copy's own record is
read (Gutenberg RDF, LibriVox API, archive.org metadata) and its language, kind and title are
checked against the book and the reader's language.

Etiquette and failure: at most 90 requests a minute (the figure Shelfmark's Open Library provider
uses), answers cached, and the shared circuit breaker (db.breaker_*) stands the service down after
repeated failures instead of making every page wait for it."""
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import requests
from lxml import etree

import config, db, fetchers, matching, metadata

OL = "https://openlibrary.org"
COVERS = "https://covers.openlibrary.org"
PROVIDER = "openlibrary"            # shares the breaker with metadata.py's Open Library adapter
TIMEOUT = (3, 8)
SEARCH_FIELDS = ("key,title,subtitle,author_name,author_key,first_publish_year,edition_count,isbn,"
                 "cover_i,language,ia,ebook_access,has_fulltext,id_project_gutenberg,id_librivox,"
                 "id_standard_ebooks,public_scan_b,number_of_pages_median")
PER_WORK = {"gutenberg": 4, "librivox": 4, "internet_archive": 6, "standard_ebooks": 2}
COPIES_DEADLINE = 12
LADDER_TIER_DEADLINE = 5.5            # two tiers must fit inside COPIES_DEADLINE
_KEY = re.compile(r"^OL\d{1,12}[WMA]$")


class Unavailable(Exception):
    """Open Library did not answer (or is stood down). The page says so instead of 'no books'."""


# ---- etiquette: rate limit + cache ------------------------------------------------------------
class _Bucket:
    def __init__(self, per_minute):
        self.cap, self.tokens, self.at, self.lock = per_minute, float(per_minute), time.monotonic(), threading.Lock()

    def take(self, wait=5.0):
        end = time.monotonic() + wait
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.at) * self.cap / 60)
                self.at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.1)


_BUCKET = _Bucket(90)


class _TTL:
    def __init__(self, ttl, size):
        self.ttl, self.size, self.d, self.lock = ttl, size, OrderedDict(), threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if not v or time.time() - v[0] > self.ttl:
                self.d.pop(k, None)
                return None
            self.d.move_to_end(k)
            return v[1]

    def put(self, k, val):
        with self.lock:
            self.d[k] = (time.time(), val)
            self.d.move_to_end(k)
            while len(self.d) > self.size:
                self.d.popitem(last=False)

    def clear(self):
        with self.lock:
            self.d.clear()


_SEARCH = _TTL(600, 300)
_RECORD = _TTL(3600, 500)
_COPIES = _TTL(1800, 200)


def _json(url, params=None):
    """GET Open Library JSON under the rate limit and the breaker. Raises Unavailable."""
    now = time.time()
    is_open, _retry = db.breaker_state(PROVIDER, now)
    if is_open:
        raise Unavailable("Open Library is stood down after repeated failures; it is retried automatically")
    if not _BUCKET.take():
        raise Unavailable("too many book lookups at once; try again in a moment")
    try:
        r = requests.get(url, params=params, headers=metadata.UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        db.breaker_record(PROVIDER, False, type(e).__name__, now)
        raise Unavailable("Open Library did not answer") from e
    if r.status_code == 404:
        db.breaker_record(PROVIDER, True, None, now)       # a miss is a healthy answer
        return None
    if r.status_code >= 500 or r.status_code == 429:
        db.breaker_record(PROVIDER, False, f"HTTP {r.status_code}", now)
        raise Unavailable(f"Open Library answered {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        db.breaker_record(PROVIDER, False, "not JSON", now)
        raise Unavailable("Open Library answered something that was not JSON") from e
    db.breaker_record(PROVIDER, True, None, now)
    return data


# ---- shapes ------------------------------------------------------------------------------------
def cover_url(cover_id, size="M"):
    return f"{COVERS}/b/id/{int(cover_id)}-{size}.jpg" if cover_id else None


def photo_url(photo_id, size="M"):
    return f"{COVERS}/a/id/{int(photo_id)}-{size}.jpg" if photo_id and int(photo_id) > 0 else None


def _text(v):
    if isinstance(v, dict):
        v = v.get("value")
    return (v or "").strip() if isinstance(v, str) else ""


def work_from_doc(d):
    """One search.json document -> the portal's work record."""
    key = (d.get("key") or "").rsplit("/", 1)[-1]
    authors = [{"name": n, "key": k} for n, k in zip(d.get("author_name") or [], d.get("author_key") or [])]
    if not authors and d.get("author_name"):
        authors = [{"name": n, "key": None} for n in d["author_name"]]
    links = {
        "gutenberg": [str(x) for x in d.get("id_project_gutenberg") or [] if str(x).isdigit()],
        "librivox": [str(x) for x in d.get("id_librivox") or [] if str(x).isdigit()],
        "standard_ebooks": [str(x) for x in d.get("id_standard_ebooks") or [] if re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)+", str(x))],
        "internet_archive": [str(x) for x in d.get("ia") or []],
    }
    public = d.get("ebook_access") == "public" or bool(d.get("public_scan_b"))
    return {"key": key, "title": d.get("title") or "", "subtitle": d.get("subtitle") or "",
            "authors": authors, "author": ", ".join(a["name"] for a in authors),
            "year": d.get("first_publish_year"), "editions": d.get("edition_count") or 0,
            "cover": cover_url(d.get("cover_i")), "cover_large": cover_url(d.get("cover_i"), "L"),
            "languages": [matching.lang(x) or x for x in (d.get("language") or [])][:12],
            "isbns": (d.get("isbn") or [])[:40], "pages": d.get("number_of_pages_median"),
            "ebook_access": d.get("ebook_access") or "", "links": links,
            "has_ebook": bool(links["gutenberg"] or links["standard_ebooks"] or (public and links["internet_archive"])),
            "has_audio": bool(links["librivox"])}


# ---- search, work, author ----------------------------------------------------------------------
def search(q, limit=20, lang=None):
    """Works matching the reader's words, most-edited first (Open Library's default ranking
    already favours the canonical work). Raises Unavailable."""
    q = (q or "").strip()
    if not q:
        return []
    k = ("s", q.lower(), limit, lang)
    hit = _SEARCH.get(k)
    if hit is not None:
        return hit
    params = {"q": q, "limit": limit, "fields": SEARCH_FIELDS}
    if lang:
        params["lang"] = lang                 # a preference for editions, not a filter on works
    data = _json(f"{OL}/search.json", params) or {}
    works = [work_from_doc(d) for d in data.get("docs") or [] if d.get("key", "").startswith("/works/")]
    _SEARCH.put(k, works)
    return works


def work(key):
    """Full record for one work: the search document (for the cross-links) plus the work JSON
    (for the description). None for an unknown or malformed key."""
    if not _KEY.fullmatch(key or "") or not key.endswith("W"):
        return None
    hit = _RECORD.get(("w", key))
    if hit is not None:
        return hit
    data = _json(f"{OL}/search.json", {"q": f"key:/works/{key}", "fields": SEARCH_FIELDS}) or {}
    docs = data.get("docs") or []
    if not docs:
        return None
    w = work_from_doc(docs[0])
    detail = _json(f"{OL}/works/{key}.json") or {}
    w["description"] = _text(detail.get("description"))
    # the search index's first_publish_year first: the work record's free-text date disagreed with
    # it on the very first book tried (1853 vs 1813 for Pride and Prejudice)
    w["first_published"] = str(w["year"]) if w["year"] else (detail.get("first_publish_date") or "")
    covers = [c for c in detail.get("covers") or [] if isinstance(c, int) and c > 0]
    if covers and not w["cover"]:
        w["cover"], w["cover_large"] = cover_url(covers[0]), cover_url(covers[0], "L")
    _RECORD.put(("w", key), w)
    return w


def author(key):
    if not _KEY.fullmatch(key or "") or not key.endswith("A"):
        return None
    hit = _RECORD.get(("a", key))
    if hit is not None:
        return hit
    d = _json(f"{OL}/authors/{key}.json")
    if not d:
        return None
    photos = [p for p in d.get("photos") or [] if isinstance(p, int) and p > 0]
    remote = d.get("remote_ids") or {}
    a = {"key": key, "name": d.get("name") or d.get("personal_name") or "",
         "bio": _text(d.get("bio")), "born": d.get("birth_date") or "", "died": d.get("death_date") or "",
         "photo": photo_url(photos[0]) if photos else None,
         "goodreads": remote.get("goodreads"), "wikipedia": d.get("wikipedia") or ""}
    _RECORD.put(("a", key), a)
    return a


def author_works(key, limit=40):
    if not _KEY.fullmatch(key or "") or not key.endswith("A"):
        return []
    k = ("aw", key, limit)
    hit = _SEARCH.get(k)
    if hit is not None:
        return hit
    data = _json(f"{OL}/search.json", {"author_key": key, "sort": "editions", "limit": limit,
                                        "fields": SEARCH_FIELDS}) or {}
    works = [work_from_doc(d) for d in data.get("docs") or [] if d.get("key", "").startswith("/works/")]
    _SEARCH.put(k, works)
    return works


# ---- copies: the cross-links, each read and verified ------------------------------------------
_RDF = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dcterms": "http://purl.org/dc/terms/",
        "pgterms": "http://www.gutenberg.org/2009/pgterms/"}


def _pg_base():
    return (config.GUTENBERG_MIRROR or fetchers.PG_BASE).rstrip("/")


def parse_pg_rdf(xml, pg_id):
    """Gutenberg's own catalogue record for one book: title, creators, language, DCMI type
    (Text / Sound), and the EPUB's byte size. None when it is not a text."""
    root = etree.fromstring(xml)
    ebook = root.find("pgterms:ebook", _RDF)
    if ebook is None:
        return None
    title = (ebook.findtext("dcterms:title", default="", namespaces=_RDF) or "").strip()
    names = [n.text.strip() for n in ebook.findall("dcterms:creator/pgterms:agent/pgterms:name", _RDF) if n.text]
    lang = ebook.findtext("dcterms:language/rdf:Description/rdf:value", default="", namespaces=_RDF)
    dtype = ebook.findtext("dcterms:type/rdf:Description/rdf:value", default="Text", namespaces=_RDF)
    size = None
    for f in ebook.findall("dcterms:hasFormat/pgterms:file", _RDF):
        if (f.get(f"{{{_RDF['rdf']}}}about") or "").endswith(f"/ebooks/{pg_id}.epub3.images"):
            ext = f.findtext("dcterms:extent", default="", namespaces=_RDF)
            size = int(ext) if ext.isdigit() else None
    return {"title": title, "author": "; ".join(names), "language": lang.strip() or None,
            "type": (dtype or "Text").strip(), "size": size}


def _gutenberg(pg_id, key):
    # the catalogue RECORD always from gutenberg.org — a local mirror serves the books but often
    # not the RDF (seen on the test mirror: every cross-link copy vanished); the DOWNLOAD below
    # still goes to the mirror when one is configured
    r = requests.get(f"{fetchers.PG_BASE}/cache/epub/{pg_id}/pg{pg_id}.rdf", headers=metadata.UA, timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    rec = parse_pg_rdf(r.content, pg_id)
    if not rec or rec["type"].lower() != "text":
        return None                           # Gutenberg also hosts recordings and images
    return {"source": "gutenberg", "kind": "ebook", "format": "epub", "title": rec["title"],
            "author": rec["author"], "language": rec["language"], "identifier": f"pg:{pg_id}",
            "download_url": f"{_pg_base()}/ebooks/{pg_id}.epub3.images", "linked": True,
            "src_ids": [("gutenberg", pg_id), ("openlibrary_work", key)],
            "detail": f"Project Gutenberg #{pg_id}"}


def _standard_ebooks(slug, w):
    """Standard Ebooks' OPDS feed now needs a Patrons login, but its downloads are public
    (verified 200). The slug IS the book (Open Library links it to this work), and every
    Standard Ebooks production is an English text of the original, so the work's own title
    and author stand for it."""
    base = slug.replace("/", "_")
    return {"source": "standard_ebooks", "kind": "ebook", "format": "epub", "title": w["title"],
            "author": w["author"], "language": "en", "identifier": f"se:{slug}",
            # ?source=download: without it the address answers 200 with an HTML "Your download has
            # started!" page (8 KB), not the book — found by requesting one on the real stack
            "download_url": f"https://standardebooks.org/ebooks/{slug}/downloads/{base}.epub?source=download",
            "linked": True, "src_ids": [("standard_ebooks", slug), ("openlibrary_work", w["key"])],
            "detail": "Standard Ebooks (a carefully produced edition)"}


def parse_librivox(data, lv_id, key):
    books = (data or {}).get("books") or []
    if not books:
        return None
    b = books[0]
    url = b.get("url_zip_file") or ""
    if not url:
        return None
    secs = int(b.get("totaltimesecs") or 0)
    parts = int(b.get("num_sections") or 0)
    names = ["{} {}".format(a.get("first_name", ""), a.get("last_name", "")).strip() for a in b.get("authors") or []]
    length = f"{secs // 3600} h {secs % 3600 // 60} min" if secs else "length unknown"
    return {"source": "librivox", "kind": "audio", "format": "zip", "title": b.get("title") or "",
            "author": "; ".join(n for n in names if n), "language": b.get("language"),
            "identifier": f"librivox:{lv_id}", "download_url": url, "linked": True,
            "duration_seconds": secs or None, "part_count": parts or None,
            "src_ids": [("librivox", lv_id), ("openlibrary_work", key)],
            "detail": f"LibriVox recording, {length}{f', {parts} parts' if parts else ''}"}


def _librivox(lv_id, key):
    # no extended=1: it adds a per-chapter listing nobody reads and took 2.5-7.5 s against
    # 0.5-1.0 s (fetchers.librivox measured it) — enough to miss the page's deadline
    r = requests.get("https://librivox.org/api/feed/audiobooks/", headers=metadata.UA, timeout=TIMEOUT,
                     params={"id": lv_id, "format": "json"})
    if r.status_code != 200:
        return None
    try:
        return parse_librivox(r.json(), lv_id, key)
    except ValueError:
        return None


def _archive(ident, key):
    rec = fetchers._ia_epub_cached(ident, fetchers._Budget())
    if not rec:
        return None                           # no plain EPUB, or a lending-only copy
    allowed = set(config.IA_COLLECTIONS)
    if allowed and not allowed & set(rec.get("collection") or []):
        return None                           # the same collection rule the keyword search applies
    from urllib.parse import quote
    lang = rec.get("language")
    return {"source": "internet_archive", "kind": "ebook", "format": "epub",
            "title": rec.get("title") or "", "author": rec.get("creator") or "",
            "language": lang[0] if isinstance(lang, list) and lang else lang,
            "identifier": f"ia:{ident}", "linked": True,
            "download_url": f"https://archive.org/download/{quote(ident)}/{quote(rec['name'])}",
            "expect_size": rec.get("size"), "expect_md5": rec.get("md5"), "expect_sha1": rec.get("sha1"),
            "src_ids": [("internet_archive", ident), ("openlibrary_work", key)]
                       + ([("openlibrary_edition", rec["openlibrary_edition"])] if rec.get("openlibrary_edition") else []),
            "detail": f"Internet Archive ({ident})"}


def want_of(w, kind="ebook", language=None):
    return {"title": w["title"], "author": w["author"], "kind": kind, "language": language,
            "isbns": [{"kind": "isbn", "value": i} for i in w.get("isbns") or []], "work_key": w["key"]}


def copies(w, language=None, deadline=COPIES_DEADLINE):
    """Every copy the cross-links lead to, in the ENABLED catalogues, each verified against the
    work and the reader's language. Sorted: automatic matches first, then the better
    production. Each copy carries match = {distance, verdict, reasons}."""
    ck = (w["key"], language, tuple(sorted(fetchers.enabled_sources())))
    hit = _COPIES.get(ck)
    if hit is not None:
        return hit
    on = {s for s, v in config.SOURCES.items() if v}
    # the keyword ladder runs beside the cross-links: it is what finds copies in catalogues
    # Open Library does not link to (your own OPDS catalogs, archive.org scans it never saw)
    ladder = _LADDER.submit(_ladder, w, language)
    jobs = []
    L = w["links"]
    if "gutenberg" in on:
        jobs += [(_gutenberg, (i, w["key"])) for i in L["gutenberg"][:PER_WORK["gutenberg"]]]
    if "standard_ebooks" in on:
        jobs += [(_standard_ebooks, (s, w)) for s in L["standard_ebooks"][:PER_WORK["standard_ebooks"]]]
    if "librivox" in on:
        jobs += [(_librivox, (i, w["key"])) for i in L["librivox"][:PER_WORK["librivox"]]]
    if "internet_archive" in on and (w["ebook_access"] == "public" or not w["ebook_access"]):
        jobs += [(_archive, (i, w["key"])) for i in L["internet_archive"][:PER_WORK["internet_archive"]]]
    futures = [fetchers.submit_detail(fn, *args) for fn, args in jobs]
    until = time.monotonic() + deadline
    found, timed_out = [], 0
    for f in futures:
        try:
            c = f.result(timeout=max(0.05, until - time.monotonic()))
        except FutureTimeout:
            f.cancel(); timed_out += 1; continue
        except Exception:
            continue
        if c and fetchers.url_allowed(c["source"], c["download_url"]):
            found.append(c)
    try:
        extra = ladder.result(timeout=max(0.05, until - time.monotonic()))
    except FutureTimeout:
        ladder.cancel(); timed_out += 1; extra = []
    except Exception:
        extra = []
    seen = {c["download_url"] for c in found}
    for r in extra:
        if r.get("download_url") in seen or not fetchers.url_allowed(r.get("source"), r.get("download_url")):
            continue
        seen.add(r["download_url"])
        # archive.org names the Open Library work its scan belongs to: then it IS a cross-link
        if ("openlibrary_work", w["key"]) in [tuple(x) for x in r.get("src_ids") or []]:
            r["linked"] = True
        r.setdefault("detail", f"found by searching {config.source_label(r.get('source'))}")
        found.append(r)
    out = []
    for c in found:
        d, verdict, reasons = matching.distance(want_of(w, c["kind"], language), c)
        c["match"] = {"distance": d, "verdict": verdict, "reasons": reasons}
        out.append(c)
    rank = {"standard_ebooks": 0, "gutenberg": 1, "librivox": 1, "internet_archive": 2}
    order = {"auto": 0, "review": 1, "reject": 2}
    out.sort(key=lambda c: (order[c["match"]["verdict"]], rank.get(c["source"], 9), c["match"]["distance"]))
    if not timed_out:                         # a partial answer is shown but never remembered
        _COPIES.put(ck, out)
    return out


# The search page warms the copy lookup for its top results, so opening a book usually renders
# from cache. Its own two threads: copies() waits on fetchers' detail pool, and a waiter must
# never occupy a slot in the pool it is waiting on.
_PREFETCH = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prefetch")


def _quiet(w, language):
    try:
        copies(w, language)
    except Exception:
        pass


def prefetch(works, language, n=4):
    for w in [w for w in works if w["has_ebook"] or w["has_audio"]][:n]:
        _PREFETCH.submit(_quiet, w, language)


# ---- the keyword ladder (Readarr's NewznabRequestGenerator tiers, for our catalogues) ----------
_LADDER = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ladder")


def _surname(author):
    first = (author or "").split(",")[0].strip()
    parts = first.split()
    return parts[-1] if parts else ""


def ladder_queries(w):
    """'title surname', then 'title' — stop at the first tier that yields a plausible copy."""
    t = re.sub(r"\s*[:(\[].*$", "", w["title"]).strip() or w["title"]
    s = _surname(w["author"])
    tiers = [f"{t} {s}".strip(), t] if s else [t]
    return list(dict.fromkeys(q for q in tiers if q))


def _ladder(w, language):
    for q in ladder_queries(w):
        results = fetchers.search(q, deadline=LADDER_TIER_DEADLINE)
        plausible = [r for r in results
                     if matching.distance(want_of(w, "audio" if r.get("source") == "librivox" else "ebook",
                                                  language), r)[1] != "reject"]
        if plausible:
            return results
    return []


# ---- series, from the Goodreads mirror (Open Library has no series data) -----------------------
# bookinfo.pro /author/{goodreads id} lists the author's series, each item a Goodreads work id,
# and the author's works (id -> title) in the same answer. Cold it took 14-53 s (measured), so
# it is NEVER on a page's critical path: the author page asks for it in the background and shows
# it once it is here.
BOOKINFO = "https://api.bookinfo.pro"
_SERIES = _TTL(6 * 3600, 200)
_SERIES_PENDING = set()
_SERIES_LOCK = threading.Lock()


def parse_bookinfo_series(data):
    titles = {w.get("ForeignId"): w.get("Title") for w in (data or {}).get("Works") or []}
    out = []
    for srs in (data or {}).get("Series") or []:
        items = []
        for li in srs.get("LinkItems") or []:
            t = titles.get(li.get("ForeignWorkId"))
            if t:
                items.append({"position": li.get("PositionInSeries") or li.get("SeriesPosition") or "",
                              "sort": li.get("SeriesPosition") or 0, "title": t,
                              "primary": bool(li.get("Primary"))})
        items.sort(key=lambda i: (float(i["sort"]) if str(i["sort"]).replace(".", "", 1).isdigit() else 9e9))
        if len(items) >= 2:
            out.append({"id": srs.get("ForeignId"), "title": srs.get("Title") or "", "books": items})
    return out


def _fetch_series(gid):
    try:
        is_open, _ = db.breaker_state("bookinfo")
        if is_open:
            return
        r = requests.get(f"{BOOKINFO}/author/{gid}", headers=metadata.UA, timeout=(3, 60))
        if r.status_code == 200:
            _SERIES.put(gid, parse_bookinfo_series(r.json()))
            db.breaker_record("bookinfo", True)
        elif r.status_code == 404:
            _SERIES.put(gid, [])
        elif r.status_code >= 500 or r.status_code == 429:
            db.breaker_record("bookinfo", False, f"HTTP {r.status_code}")
    except (requests.RequestException, ValueError) as e:
        db.breaker_record("bookinfo", False, type(e).__name__)
    finally:
        with _SERIES_LOCK:
            _SERIES_PENDING.discard(gid)


def series_for_author(goodreads_id):
    """The author's series if already known, else None (and a background fetch is started)."""
    gid = str(goodreads_id or "").strip()
    if not gid.isdigit():
        return []
    hit = _SERIES.get(gid)
    if hit is not None:
        return hit
    with _SERIES_LOCK:
        if gid not in _SERIES_PENDING:
            _SERIES_PENDING.add(gid)
            _PREFETCH.submit(_fetch_series, gid)
    return None
