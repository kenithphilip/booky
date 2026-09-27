"""Descriptive metadata for the portal: a provider chain with real failsafes.

Three jobs (docs spec v3): a rich portal UI, correct identification for the fetchers, and a
subset pushed to the devices. Metadata lives in the PORTAL DATABASE — stored files are never
modified for it. `tagger.py` keeps writing exactly one thing, `owner:<user>`, which is access
control rather than description.

BACKGROUND ONLY. The primary provider's cold path was measured at 28.4 s (warm 0.43 s), which
is more than twice the search page's whole deadline. Enrichment runs from the worker, never
inside a request — a slow source that returns nothing after 12 s is the exact failure this
project has already shipped twice.

THE CHAIN, ranked, keyless first because a key the admin must create, store and rotate is a
provider that fails on a date nobody wrote down:
  1. bookinfo.pro  (rreading-glasses, Goodreads mirror) — ISBN identity, series, author bios.
     Measured: ISBN->303->book 2.0 s; 2 of 4 common ISBNs 404'd; ISBN-10 answers HTTP 400,
     so only ISBN-13 is ever sent.
  2. hardcover.bookinfo.pro — same software, community-curated data.
  3. openlibrary.org — keyless floor, no token, Internet Archive backed.

WHAT NEVER CROSSES THIS BOUNDARY: a provider's `Genres`/`subject` array. Measured on Project
Hail Mary, bookinfo returns nine genres. CWA appends tags rather than replacing, so anything
that leaked into Calibre would be permanent, and a genre landing on a reader's denied-tags
list would HIDE THE BOOK FROM ITS OWNER. It is dropped here, at the adapter, not filtered
downstream — see _clean().
"""
import json
import time
import requests

import config
import db

# A contact address earns Open Library's 3 req/s tier; without one they throttle to 1 req/s,
# which the portal was silently taking for nothing.
UA = {"User-Agent": f"bookstack-librarian/4.4 (+https://{config.DOMAIN or 'example.invalid'}; "
                    f"{config.ADMIN_EMAIL or 'admin@example.invalid'})"}

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 10
# One book's whole enrichment. Generous compared with the search page (12 s for everything)
# because nobody is waiting on this — but bounded, because "no bound" is how a background job
# becomes a stuck thread that never beats again.
BOOK_BUDGET = 45
BREAKER_THRESHOLD = 3          # consecutive HARD failures before a provider is stood down
BREAKER_COOLDOWN = 900
NEG_CACHE_SECONDS = 7 * 86400  # a book the whole chain has never heard of is not news weekly


class Miss(Exception):
    """A clean 'not in this catalogue': HTTP 200 carrying nothing useful, or a 404.

    Deliberately NOT a failure. Advancing the chain is correct here, and counting it against
    the breaker would stand down a perfectly healthy provider for having no answer about one
    obscure book — which is most books, for most providers."""


class ProviderDown(Exception):
    """A hard failure: DNS, refused, a timeout, a 5xx, or a rate limit. Counts."""


def _get(url, budget, params=None, allow_redirects=True):
    """One HTTP call inside the book's remaining wall clock.

    (connect, read) as a pair, not a scalar: requests applies a scalar to the connect AND to
    every read, so `timeout=10` is really up to 20 s against a host that accepts the connection
    and then stalls — which is precisely the slow-failure mode that bit this project."""
    left = budget - time.monotonic()
    if left <= 0.2:
        raise ProviderDown("out of time for this book")
    c = max(0.05, min(CONNECT_TIMEOUT, left))
    r_to = max(0.05, min(READ_TIMEOUT, left - c))
    try:
        r = requests.get(url, params=params, headers=UA, timeout=(c, r_to),
                         allow_redirects=allow_redirects)
    except requests.RequestException as e:
        raise ProviderDown(f"{e.__class__.__name__}: {str(e)[:120]}")
    if r.status_code == 404:
        raise Miss("not found")
    if r.status_code == 429:
        raise ProviderDown("rate limited (429)")
    if r.status_code >= 500:
        raise ProviderDown(f"upstream {r.status_code}")
    if r.status_code >= 400:
        # 400 is how bookinfo answers an ISBN-10. A bad request is OUR fault, not the
        # provider's: a miss, so the chain advances and the breaker stays shut.
        raise Miss(f"rejected ({r.status_code})")
    return r


def _post(url, budget, payload, headers):
    """_get's twin for a JSON POST (Hardcover's GraphQL), same wall clock and same verdicts."""
    left = budget - time.monotonic()
    if left <= 0.2:
        raise ProviderDown("out of time for this book")
    c = max(0.05, min(CONNECT_TIMEOUT, left))
    try:
        r = requests.post(url, json=payload, headers={**UA, **headers},
                          timeout=(c, max(0.05, min(READ_TIMEOUT, left - c))))
    except requests.RequestException as e:
        raise ProviderDown(f"{e.__class__.__name__}: {str(e)[:120]}")
    if r.status_code in (401, 403):
        raise ProviderDown(f"the key was refused ({r.status_code})")
    if r.status_code == 429 or r.status_code >= 500:
        raise ProviderDown(f"upstream {r.status_code}")
    if r.status_code >= 400:
        raise Miss(f"rejected ({r.status_code})")
    return r


def _json(r):
    try:
        return r.json()
    except ValueError:
        raise Miss("not JSON")


# Fields a provider may contribute. Anything not named here is dropped, so a provider adding a
# new field in a future release cannot silently start feeding the portal something unreviewed.
_ALLOWED = ("title", "full_title", "short_title", "description", "first_publish_year",
            "release_date", "language", "publisher", "pages", "kind", "abridged", "narrator",
            "translator", "edition_statement", "duration_seconds", "part_count", "byte_size",
            "cover_url", "series", "series_position", "authors", "identifiers")


def _clean(d):
    """The adapter boundary. Drops every field the portal has not asked for — `Genres` above
    all, which is a provider handing us a tags array and is the one field with a demonstrated
    blast radius and no identity value."""
    return {k: v for k, v in (d or {}).items() if k in _ALLOWED and v not in (None, "", [], {})}


def _ids(pairs, provider, exact):
    """Identifier tuples, normalised. ISBNs keep digits and a trailing X only; an ISBN-10 is
    recorded as such and never sent to bookinfo, which answers 400 for one."""
    out = []
    for kind, value in pairs:
        v = str(value or "").strip()
        if not v:
            continue
        if kind.startswith("isbn"):
            v = "".join(ch for ch in v if ch.isdigit() or ch in "Xx").upper()
            if len(v) not in (10, 13):
                continue
            kind = "isbn13" if len(v) == 13 else "isbn10"
        out.append({"kind": kind, "value": v, "provider": provider, "exact": bool(exact)})
    return out


# ---- providers ---------------------------------------------------------------------------
def _bookinfo(base, name, q, budget):
    """rreading-glasses. Author-centric: an ISBN resolves (via 303) to the AUTHOR record with
    the works nested, which is the Readarr model this software exists to serve."""
    isbn = next((i["value"] for i in q.get("identifiers", []) if i["kind"] == "isbn13"), None)
    if not isbn:
        raise Miss("no ISBN-13 to look up")        # its only exact entry point; do not guess
    data = _json(_get(f"{base}/book/isbn/{isbn}", budget))
    works = data.get("Works") or []
    if not works:
        raise Miss("no works on the record")
    w = works[0]
    series = (w.get("Series") or [{}])[0] if w.get("Series") else {}
    return _clean({
        "title": w.get("ShortTitle") or w.get("Title"),
        "full_title": w.get("FullTitle"), "short_title": w.get("ShortTitle"),
        "description": w.get("Description") or data.get("Description"),
        "release_date": (w.get("ReleaseDateRaw") or w.get("ReleaseDate") or "")[:10] or None,
        "series": series.get("Title") or series.get("Name"),
        "series_position": w.get("PositionInSeries") or w.get("SeriesPosition"),
        "cover_url": w.get("ImageUrl") or data.get("ImageUrl"),
        "authors": [{"name": data.get("Name"), "bio": data.get("Description"),
                     "image_url": data.get("ImageUrl"),
                     "foreign_id": str(data.get("ForeignId") or "") or None}],
        "identifiers": _ids([("goodreads_work", w.get("ForeignId")),
                             ("goodreads_author", data.get("ForeignId"))], name, True),
        # NOTE: w["Genres"] exists and is deliberately not read. See the module docstring.
    })


def _openlibrary(q, budget):
    """The keyless floor. Its isbn/language/publisher/subject arrays aggregate across EVERY
    edition of the work — 11 languages on one Neuromancer record — so taking [0] is
    confidently wrong. Only work-level facts are taken, and they are marked exact=False —
    except when the request already carries the Open Library WORK it was chosen as (a book
    requested from its work page): then the record is looked up by key, exactly."""
    isbn = next((i["value"] for i in q.get("identifiers", []) if i["kind"] == "isbn13"), None)
    work_key = next((i["value"] for i in q.get("identifiers", [])
                     if i["kind"] in ("openlibrary_work", "olid") and str(i["value"]).endswith("W")), None)
    if work_key:
        params = {"q": f"key:/works/{work_key}", "limit": 1}
    elif isbn:
        params = {"q": f"isbn:{isbn}", "limit": 1}
    elif q.get("title"):
        params = {"title": q["title"], "limit": 1}
        if q.get("author"):
            params["author"] = q["author"]
    else:
        raise Miss("nothing to search with")
    params["fields"] = ("key,title,subtitle,author_name,author_key,first_publish_year,"
                        "number_of_pages_median,cover_i,language,publisher")
    docs = (_json(_get("https://openlibrary.org/search.json", budget, params=params))
            .get("docs") or [])
    if not docs:
        raise Miss("no docs")                      # HTTP 200 + docs:[] is an unambiguous miss
    d = docs[0]
    cover = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg" if d.get("cover_i") else None
    key = (d.get("key") or "").rsplit("/", 1)[-1]
    description = ""
    try:                                   # the work record carries the blurb; best effort
        wj = _json(_get(f"https://openlibrary.org/works/{key}.json", budget)) if key else {}
        desc = wj.get("description")
        description = (desc.get("value") if isinstance(desc, dict) else desc) or ""
    except (Miss, ProviderDown):
        pass
    return _clean({
        "description": description.strip() if isinstance(description, str) else "",
        "title": d.get("title"), "full_title": d.get("subtitle") and
        f"{d.get('title')}: {d['subtitle']}" or d.get("title"),
        "short_title": d.get("title"),
        "first_publish_year": d.get("first_publish_year"),
        "pages": d.get("number_of_pages_median"),
        "cover_url": cover,
        "authors": [{"name": n} for n in (d.get("author_name") or [])[:3]],
        "identifiers": _ids([("olid", key)], "openlibrary", bool(work_key)),
    })


def _google(q, budget):
    """Google Books, only with the owner's free API key: the keyless quota is shared by every
    anonymous caller on Earth and was exhausted when tested (HTTP 429). `categories` is a tags
    array and is dropped at _clean like every provider's genres."""
    if not config.GOOGLE_BOOKS_API_KEY:
        raise Miss("no key")
    isbn = next((i["value"] for i in q.get("identifiers", []) if i["kind"] in ("isbn13", "isbn10")), None)
    if isbn:
        query = f"isbn:{isbn}"
    elif q.get("title"):
        query = f'intitle:"{q["title"]}"' + (f' inauthor:"{q["author"]}"' if q.get("author") else "")
    else:
        raise Miss("nothing to search with")
    items = _json(_get("https://www.googleapis.com/books/v1/volumes", budget,
                       params={"q": query, "maxResults": 1, "printType": "books",
                               "key": config.GOOGLE_BOOKS_API_KEY})).get("items") or []
    if not items:
        raise Miss("no items")
    v = items[0].get("volumeInfo") or {}
    img = (v.get("imageLinks") or {}).get("thumbnail") or ""
    ids = [(("isbn13" if x.get("type") == "ISBN_13" else "isbn10"), x.get("identifier"))
           for x in v.get("industryIdentifiers") or [] if x.get("type") in ("ISBN_13", "ISBN_10")]
    return _clean({
        "title": v.get("title"), "full_title": v.get("subtitle") and f"{v.get('title')}: {v['subtitle']}" or v.get("title"),
        "description": v.get("description"), "release_date": v.get("publishedDate"),
        "pages": v.get("pageCount"), "language": v.get("language"), "publisher": v.get("publisher"),
        "cover_url": img.replace("http://", "https://") or None,
        "authors": [{"name": n} for n in (v.get("authors") or [])[:3]],
        "identifiers": _ids(ids + [("google_books", items[0].get("id"))], "google", bool(isbn)),
    })


def _hardcover_api(q, budget):
    """Hardcover's own API, only with the owner's token (Library -> Metadata sources): the best
    series data there is. Query as CWA's cps/metadata_provider/hardcover.py does it; the token
    goes on THIS request only (CWA stores it on a shared class dict — a cross-user leak)."""
    if not config.HARDCOVER_API_KEY:
        raise Miss("no token")
    term = next((i["value"] for i in q.get("identifiers", []) if i["kind"] == "isbn13"), None) \
        or " ".join(x for x in (q.get("title"), q.get("author")) if x)
    if not term:
        raise Miss("nothing to search with")
    token = config.HARDCOVER_API_KEY.replace("Bearer ", "").strip()
    data = _json(_post("https://api.hardcover.app/v1/graphql", budget,
                       {"query": 'query S($q: String!) { search(query: $q, query_type: "Book", per_page: 5) { results } }',
                        "variables": {"q": term}},
                       {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}))
    results = ((data.get("data") or {}).get("search") or {}).get("results") or {}
    if isinstance(results, str):
        import json as _j
        try:
            results = _j.loads(results)
        except ValueError:
            raise Miss("unreadable results")
    hits = results.get("hits") or []
    if not hits:
        raise Miss("no hits")
    d = hits[0].get("document") or {}
    series = (d.get("featured_series") or {})
    return _clean({
        "title": d.get("title"), "full_title": d.get("subtitle") and f"{d.get('title')}: {d['subtitle']}" or d.get("title"),
        "description": d.get("description"), "release_date": d.get("release_date"),
        "first_publish_year": d.get("release_year"), "pages": d.get("pages"),
        "cover_url": (d.get("image") or {}).get("url"),
        "series": (series.get("series") or {}).get("name"), "series_position": series.get("position"),
        "authors": [{"name": n} for n in (d.get("author_names") or [])[:3]],
        "identifiers": _ids([("isbn13", i) for i in (d.get("isbns") or []) if len(str(i)) == 13][:5]
                            + [("hardcover_book", d.get("id"))], "hardcover_api", False),
    })


# Ordered. Each entry: (name, callable). Keyless and open-licensed first.
PROVIDERS = (
    ("bookinfo", lambda q, b: _bookinfo("https://api.bookinfo.pro", "bookinfo", q, b)),
    ("hardcover", lambda q, b: _bookinfo("https://hardcover.bookinfo.pro", "hardcover", q, b)),
    ("hardcover_api", _hardcover_api),     # only with HARDCOVER_API_KEY
    ("google", _google),                   # only with GOOGLE_BOOKS_API_KEY
    ("openlibrary", _openlibrary),
)
_KEYED = {"hardcover_api": lambda: bool(config.HARDCOVER_API_KEY),
          "google": lambda: bool(config.GOOGLE_BOOKS_API_KEY)}


def enabled_providers():
    return [(n, f) for n, f in PROVIDERS
            if config.METADATA_PROVIDERS.get(n, True) and _KEYED.get(n, lambda: True)()]


def fetch(query, now=None):
    """Walk the chain for one book. Returns (merged, trace).

    `trace` is the point of this function as much as `merged` is: every provider's outcome,
    in order, with a reason. A chain that silently falls through to nothing is the failure
    this project keeps rediscovering, so "we asked nobody because all three were stood down"
    has to be distinguishable from "we asked and nobody knew"."""
    now = now or time.time()
    merged, trace = {}, []
    deadline = time.monotonic() + BOOK_BUDGET
    for name, fn in enabled_providers():
        is_open, retry = db.breaker_state(name, now=now)
        if is_open:
            trace.append({"provider": name, "outcome": "skipped",
                          "reason": f"circuit open until {int(retry - now)}s from now"})
            continue
        started = time.monotonic()
        try:
            got = fn(query, deadline)
        except Miss as e:
            db.breaker_record(name, ok=True, now=now)        # a miss proves the provider works
            trace.append({"provider": name, "outcome": "miss", "reason": str(e)[:120],
                          "seconds": round(time.monotonic() - started, 2)})
            continue
        except ProviderDown as e:
            opened = db.breaker_record(name, ok=False, error=str(e), now=now,
                                       threshold=BREAKER_THRESHOLD, cooldown=BREAKER_COOLDOWN)
            trace.append({"provider": name, "outcome": "down", "reason": str(e)[:120],
                          "opened_breaker": opened,
                          "seconds": round(time.monotonic() - started, 2)})
            continue
        except Exception as e:                                # a provider must never take the worker down
            db.breaker_record(name, ok=False, error=repr(e)[:160], now=now)
            trace.append({"provider": name, "outcome": "error", "reason": repr(e)[:120]})
            continue
        db.breaker_record(name, ok=True, now=now)
        trace.append({"provider": name, "outcome": "hit",
                      "fields": sorted(got), "seconds": round(time.monotonic() - started, 2)})
        merged = _merge(merged, got, name)
        if _sufficient(merged):
            break                                   # nothing below can improve on this
    return merged, trace


def _merge(into, got, provider):
    """First non-empty wins per field, because the chain is ranked. Identifiers and authors
    accumulate instead: a second provider knowing one more identifier is the whole point of
    having a chain, and dropping it would throw away the best verification evidence we get."""
    out = dict(into)
    for k, v in got.items():
        if k == "identifiers":
            seen = {(i["kind"], i["value"]) for i in out.get("identifiers", [])}
            out.setdefault("identifiers", []).extend(
                i for i in v if (i["kind"], i["value"]) not in seen)
        elif k == "authors":
            have = {(a.get("name") or "").lower() for a in out.get("authors", [])}
            out.setdefault("authors", []).extend(
                a for a in v if (a.get("name") or "").lower() not in have)
        elif not out.get(k):
            out[k] = v
    out.setdefault("_providers", []).append(provider)
    return out


def _sufficient(m):
    """Enough to stop asking. A title plus a description plus a cover is a usable book page;
    querying a 28-second provider to add a page count nobody asked for is not worth it."""
    return bool(m.get("title") and m.get("description") and m.get("cover_url"))


def describe_trace(trace):
    """One line an admin can read, for the request row and the admin page."""
    if not trace:
        return "no metadata provider was asked"
    return "; ".join(
        f"{t['provider']}: {t['outcome']}" + (f" ({t['reason']})" if t.get("reason") else "")
        for t in trace)


def negative_cached(key, now=None):
    """True when the WHOLE chain already drew a blank on this key recently. Without it a book
    nobody has heard of is re-asked of three providers on every housekeeping pass, for ever."""
    now = now or time.time()
    row = db.meta_miss_get(key)
    return bool(row and row > now - NEG_CACHE_SECONDS)


def remember_miss(key, now=None):
    db.meta_miss_set(key, now or time.time())


def cache_key(query):
    isbn = next((i["value"] for i in query.get("identifiers", []) if i["kind"] == "isbn13"), "")
    if isbn:
        return f"isbn13:{isbn}"
    t = (query.get("title") or "").strip().lower()
    a = (query.get("author") or "").strip().lower()
    return f"ta:{t}|{a}"


def to_json(obj):
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return None
