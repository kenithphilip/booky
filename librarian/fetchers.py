"""Search adapters for the curated, redistributable catalogs.

PROVIDER CONTRACT (how to add a legitimate content source)
----------------------------------------------------------
A provider is a function  search(query: str) -> list[dict]  where each dict is:
    {source, kind ("ebook"|"audio"), title, author, identifier,
     format, download_url, is_torrent}
Register it in _ADAPTERS below and add an enable flag in config.SOURCES. That is the
whole extension surface: the search bar, result rendering, request queue, approval flow,
owner-tagging and device sync all work unchanged. No frontend edits.

By design each adapter is bound in code to ONE specific, vetted source (Gutenberg, Standard
Ebooks, Internet Archive, LibriVox, a configured OPDS catalog). There is deliberately no
generic "enter any endpoint + credentials + query template" provider: that would be a
point-it-at-anything grabber. To add a source, you (or I) write a concrete adapter for that
source's real API — the same way the ones below are written.
"""
import re, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import requests, config
from urllib.parse import urlsplit, quote
from lxml import etree
import opds

UA = {"User-Agent": "bookstack-librarian/2.0"}
# One slow catalog used to hold the whole page: the search ran the adapters one after the
# other with a 20 s timeout each (16-35 s pages were normal). Every HTTP call now has a short
# timeout, the adapters run in parallel and the page renders with whatever answered in time.
TIMEOUT = 8
CONNECT_TIMEOUT = 3          # a catalog that does not answer the handshake is down, not slow
SEARCH_DEADLINE = 12
# What ONE adapter may spend in total, however many HTTP calls it makes. A single timeout is
# not enough any more: two adapters now make a second call when the first one comes back empty
# (LibriVox title-then-author) or needs detail (Internet Archive search-then-metadata), and
# 2 x TIMEOUT would outlive the page and leave threads running behind an already-rendered page.
SOURCE_BUDGET = SEARCH_DEADLINE - 2


class _Budget:
    """What is left of one adapter's wall clock. Every HTTP call takes the smaller of TIMEOUT
    and the remainder, so an adapter cannot spend more than SOURCE_BUDGET no matter how many
    calls it makes."""

    def __init__(self, seconds=SOURCE_BUDGET):
        self.until = time.monotonic() + seconds

    @property
    def left(self):
        return self.until - time.monotonic()

    def timeout(self, cap=TIMEOUT):
        return min(cap, max(0.0, self.left))

    def pair(self, connect=CONNECT_TIMEOUT, read=TIMEOUT):
        """(connect, read), not one number: requests applies a scalar `timeout` to the connect
        AND to every read, so `timeout=8` is really up to 16 s against a host that accepts the
        connection and then stalls. Measured on a throttled archive.org and on gutenberg.org
        under a burst, where a single call ran to 13.4 s inside a 10 s budget. Never zero:
        urllib3 rejects a zero timeout outright, and "fail at once" is what we mean."""
        c = max(0.05, self.timeout(connect))
        return c, max(0.05, min(read, self.left - c))

    def spent(self):
        return self.left <= 0.2          # too little left to be worth another round trip

# Two shared, bounded pools — NOT one per request.
#
# `search()` used to build a ThreadPoolExecutor per call and internet_archive() a second one
# inside it, both released with shutdown(wait=False): that cancels QUEUED work but never work
# already running, so an adapter abandoned at the page deadline kept its thread and its socket
# for the rest of its own budget. GET / is the only authenticated route with no rate limit in
# front of it, so a reader holding down refresh multiplied threads and outbound sockets at up
# to 11 per request, on the single gunicorn worker that also runs the queue, dropbox and
# housekeeping loops in-process on a 2-core box.
#
# With a fixed budget, concurrent searches SHARE these threads instead of multiplying them:
# the tenth simultaneous search queues rather than spawning, and the page deadlines below
# render whatever answered in time. That also makes SEARCH_DEADLINE/ENRICH_DEADLINE mean
# something, because a timed-out future is cancelled here rather than left running.
#
# They must stay two pools: an adapter runs in _SEARCH and submits its detail calls to
# _DETAIL, and one pool doing both would deadlock the moment every worker is an adapter
# waiting on a detail task queued behind it.
SEARCH_WORKERS = 8           # ~ two searches' worth of adapters (5 sources) fully parallel
DETAIL_WORKERS = 8           # IA /metadata and Open Library enrichment; matches gunicorn's threads
_SEARCH = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="search")
_DETAIL = ThreadPoolExecutor(max_workers=DETAIL_WORKERS, thread_name_prefix="detail")

def submit_detail(fn, *a, **kw):
    """Queue one short outbound detail call (an archive.org /metadata GET, an Open Library
    enrichment) on the shared detail pool. app.index uses this for covers/blurbs so the search
    page and the adapters draw on one thread budget instead of two."""
    return _DETAIL.submit(fn, *a, **kw)

def _collect(futures, until, sink):
    """Take each future's result until `until`, then CANCEL the rest. Cancelling is the half
    the old per-request pools could not do: shutdown(cancel_futures=True) on a pool that is
    being thrown away still leaves the running tasks running."""
    for f in futures:
        try:
            sink(f.result(timeout=max(0.1, until - time.monotonic())))
        except FutureTimeout:
            f.cancel()        # still queued: it never starts. Already running: its own _Budget ends it.
        except Exception:
            pass

# Hosts each adapter may hand to the worker. The request form echoes download_url back, so
# app.make_request re-validates it here before anything is fetched from the host network.
ALLOWED_HOSTS = {
    "gutenberg":        {"www.gutenberg.org", "gutenberg.org"},
    "standard_ebooks":  {"standardebooks.org"},
    "internet_archive": {"archive.org", "*.archive.org"},
    "librivox":         {"archive.org", "*.archive.org", "librivox.org", "www.librivox.org"},
    "mycatalog":        set(),          # exactly the configured MYCATALOG_URL origin
}

def _host_matches(host, pattern):
    return host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))

def _same_origin(p, configured):
    """p (a SplitResult) has the scheme, host and port of the configured base URL and no userinfo."""
    c = urlsplit(configured or "")
    return bool(c.hostname) and p.scheme == c.scheme and p.hostname == c.hostname.lower() \
        and p.port == c.port and p.username is None

def url_allowed(source, url):
    try:
        p = urlsplit(url or "")
        host = (p.hostname or "").lower()
        if not host or p.username is not None:
            return False
        if source == "mycatalog":
            return _same_origin(p, config.MYCATALOG_URL)
        if source == "gutenberg" and config.GUTENBERG_MIRROR and _same_origin(p, config.GUTENBERG_MIRROR):
            return True
        if p.scheme != "https":
            return False
        return any(_host_matches(host, a) for a in ALLOWED_HOSTS.get(source, ()))
    except ValueError:            # malformed port / IPv6 literal
        return False

def _get(url, budget=None, **kw):
    kw.setdefault("timeout", budget.pair() if budget else (CONNECT_TIMEOUT, TIMEOUT))
    return requests.get(url, headers=UA, **kw)

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
PG_BASE = "https://www.gutenberg.org"
# In the search feed a book entry's <id> is https://www.gutenberg.org/ebooks/<n>.opds; the two
# navigation entries at the top of every result ("Authors", "Subjects") have no number there.
_PG_BOOK_ID = re.compile(r"/ebooks/(\d+)\.opds$")

def gutenberg(q, limit=8, budget=None):
    """Project Gutenberg's own OPDS search.

    This adapter used to call gutendex.com, a third-party mirror of Gutenberg's metadata. That
    host now sits behind a Cloudflare challenge no plain HTTP client gets past: measured 5/5
    timeouts at TIMEOUT=8 and 3/3 at 12 s. Since SRC_GUTENBERG is on by default, every search
    on the portal spent the whole page deadline waiting for it and returned nothing.
    gutenberg.org's own OPDS endpoint is first-party and answers in ~1.4 s.

    It is a navigation feed: each entry carries the title, the author in <content> and the book
    id in <id>, and the EPUB address is derivable from that id (/ebooks/<n>.epub3.images —
    verified 200 application/epub+zip for five ids). So this stays ONE request. Following each
    entry's own .opds page for its acquisition link would be exactly the N+1 that made the
    Internet Archive adapter take ten seconds.
    """
    budget = budget or _Budget()
    out = []
    try:
        r = _get(f"{PG_BASE}/ebooks/search.opds/", budget=budget, params={"query": q})
        r.raise_for_status()
        for e in etree.fromstring(r.content).findall("a:entry", ATOM_NS):
            m = _PG_BOOK_ID.search(e.findtext("a:id", default="", namespaces=ATOM_NS) or "")
            if not m:
                continue
            url = f"{PG_BASE}/ebooks/{m.group(1)}.epub3.images"
            if config.GUTENBERG_MIRROR:
                url = url.replace(PG_BASE, config.GUTENBERG_MIRROR.rstrip("/"))
            out.append({"source": "gutenberg", "kind": "ebook",
                        "title": (e.findtext("a:title", default="", namespaces=ATOM_NS) or "?").strip() or "?",
                        "author": (e.findtext("a:content", default="", namespaces=ATOM_NS) or "").strip() or "Unknown",
                        "identifier": f"gutenberg:{m.group(1)}",
                        "format": "epub", "download_url": url, "is_torrent": False})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out

_SE_CACHE = {"feed": None, "at": 0.0}
SE_CACHE_SECONDS = 6 * 3600

def _se_feed(budget=None):
    """The whole Standard Ebooks catalog feed, cached for a few hours. A failed fetch (the feed
    answers 401 without a Patrons Circle login) raises and is NOT cached, so the next search
    tries again instead of silently finding nothing until the portal restarts."""
    if _SE_CACHE["feed"] is None or time.time() - _SE_CACHE["at"] > SE_CACHE_SECONDS:
        r = _get("https://standardebooks.org/feeds/opds/all", budget=budget)
        r.raise_for_status()
        _SE_CACHE.update(feed=r.content, at=time.time())
    return _SE_CACHE["feed"]

def standard_ebooks(q, limit=8, budget=None):
    out = []
    try:
        root = etree.fromstring(_se_feed(budget or _Budget()))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        ql = q.lower()
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", default="", namespaces=ns) or "")
            author = (e.findtext("a:author/a:name", default="", namespaces=ns) or "")
            if ql not in f"{title} {author}".lower():
                continue
            href = None
            for link in e.findall("a:link", ns):
                rel, typ = link.get("rel", ""), link.get("type", "")
                if "acquisition" in rel and "epub" in typ:
                    href = link.get("href"); break
            if not href:
                continue
            if href.startswith("/"):
                href = "https://standardebooks.org" + href
            out.append({"source": "standard_ebooks", "kind": "ebook",
                        "title": title, "author": author or "Unknown",
                        "identifier": f"se:{href}", "format": "epub",
                        "download_url": href, "is_torrent": False})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out

IA_META_WORKERS = DETAIL_WORKERS    # these run on the shared _DETAIL pool; this is its width
# Shorter than TIMEOUT on purpose. These run as one parallel round, so the round costs what
# the SLOWEST item costs, and a single item sitting there for ten seconds was taking the
# adapter's whole budget on 3 of 4 test queries. We ask for more rows than we need, so
# dropping a slow item is cheaper than waiting for it.
IA_META_CONNECT, IA_META_READ = 2.0, 4.0
# archive.org visibly throttles a burst of /metadata calls (measured: the same query's
# search leg went 0.9 s -> 4 s -> 8 s -> read timeout while probing). Identifiers repeat
# across searches, so remembering which file we picked keeps the next search off the wire.
_IA_FILES = {}                       # identifier -> (epub name or None, fetched at)
IA_FILES_TTL = 6 * 3600
IA_FILES_MAX = 2000

def _ia_meta(ident, budget):
    try:
        if budget.spent():
            return None
        return _get(f"https://archive.org/metadata/{ident}",
                    timeout=budget.pair(IA_META_CONNECT, IA_META_READ)).json()
    except Exception:
        return None

def _ia_epub(meta):
    """The item's plain EPUB file name, or None.

    `<id>_lcp.epub` is the Readium-LCP lending copy and `access-restricted-item: true` means
    the download needs a borrow: both answer 401/403 to us (verified: ahabswifeorstar00nasl
    401, trialsofwordessa00lewi 403). Offering either queues a request that can only fail."""
    if not meta:
        return None
    if str((meta.get("metadata") or {}).get("access-restricted-item", "")).lower() == "true":
        return None
    for f in meta.get("files", []):
        name = f.get("name", "")
        low = name.lower()
        if low.endswith(".epub") and not low.endswith("_lcp.epub"):
            # The whole file dict, not just the name. archive.org states md5, sha1 and size for
            # every file and we were fetching all of it and keeping one string — so the only
            # cryptographic verification available anywhere in this stack was being discarded
            # at zero saving. It costs no extra HTTP call: /metadata is already fetched for
            # every hit and cached for six hours.
            md = meta.get("metadata") or {}
            return {"name": name,
                    "md5": f.get("md5"), "sha1": f.get("sha1"),
                    "size": int(f["size"]) if str(f.get("size", "")).isdigit() else None,
                    # work-level identifiers archive.org happens to know: free verification
                    # evidence and free cross-links to other catalogues
                    "openlibrary_work": md.get("openlibrary_work"),
                    "openlibrary_edition": md.get("openlibrary_edition"),
                    "lccn": md.get("lccn"), "publisher": md.get("publisher"),
                    "date": md.get("date"), "language": md.get("language")}
    return None

def _ia_epub_cached(ident, budget):
    """_ia_epub for one identifier, remembered for a few hours. A call that did not come back
    at all is NOT remembered — only an answer from archive.org is."""
    hit = _IA_FILES.get(ident)
    if hit and time.time() - hit[1] <= IA_FILES_TTL:
        return hit[0]
    meta = _ia_meta(ident, budget)
    if meta is None:
        return None
    name = _ia_epub(meta)
    if len(_IA_FILES) >= IA_FILES_MAX:
        _IA_FILES.clear()            # a family portal: a whole-cache reset beats an LRU here
    _IA_FILES[ident] = (name, time.time())
    return name

# archive.org's advancedsearch endpoint speaks Lucene, and the query below wraps the reader's
# words in parentheses. Punctuation a reader types straight into a search box breaks that
# expression, and archive.org answers 200 with {"error": ...} and no "response" key, which
# reached the reader as an empty result page with no explanation. Verified broken raw and
# fixed here: 'moby)', 'moby "dick', 'moby /dick', 'who? what!', 'moby ^^', 'moby AND'.
# Words are all this adapter ever meant to send.
_LUCENE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]+')
_LUCENE_OPS = re.compile(r'\b(?:AND|OR|NOT|TO)\b')

def _ia_q(q):
    return (_LUCENE_OPS.sub(" ", _LUCENE.sub(" ", q))).strip() or q.strip()

def internet_archive(q, limit=8, budget=None):
    """One search, then every item's metadata at once.

    Three measured problems, all fixed here:
      * N+1: one search plus `limit` SERIAL /metadata GETs at ~1.1 s each. Measured on eight
        identifiers: 15.3 s serial, 1.5 s for the same eight fetched together.
      * most hits had no EPUB at all (3/8 usable). `format:EPUB` in the query and `format` in
        fl[] took that to 8/8 on three test queries.
      * lending items advertise an EPUB that we cannot download; see _ia_epub. Excluding
        `inlibrary` and `printdisabled` at query time takes the unusable share of a page of
        results from 7/12 and 4/12 to 0/12 on two test queries, so the metadata call below is
        now only for the file name, which is not derivable from the identifier.
    A few rows over `limit` are asked for because _ia_epub still drops the odd one.
    """
    budget = budget or _Budget()
    out = []
    try:
        coll = " OR ".join(f"collection:{c}" for c in config.IA_COLLECTIONS)
        params = {"q": f'({_ia_q(q)}) AND mediatype:texts AND format:EPUB '
                       f'AND NOT collection:inlibrary AND NOT collection:printdisabled '
                       f'AND ({coll})',
                  "fl[]": ["identifier", "title", "creator", "format"],
                  "rows": limit + 4, "output": "json"}
        r = _get("https://archive.org/advancedsearch.php", budget=budget, params=params)
        r.raise_for_status()
        docs = r.json()["response"]["docs"]
        if not docs or budget.spent():
            return out
        # the shared detail pool, never a pool of our own: `with ThreadPoolExecutor(...)` here
        # meant every concurrent search started its own eight threads on top of the adapters'
        names = list(_DETAIL.map(lambda d: _ia_epub_cached(d.get("identifier", ""), budget), docs))
        for d, rec in zip(docs, names):
            if not rec:
                continue
            epub = rec["name"]
            ident = d["identifier"]
            out.append({"source": "internet_archive", "kind": "ebook",
                        "title": d.get("title", ident),
                        "author": (d.get("creator") if isinstance(d.get("creator"), str)
                                   else ", ".join(d.get("creator", [])) or "Unknown"),
                        "identifier": f"ia:{ident}", "format": "epub",
                        # file names contain spaces ("level 2 - Moby Dick.epub"); an unquoted
                        # space makes a URL the form echo cannot round-trip past url_allowed
                        "download_url": f"https://archive.org/download/{quote(ident)}/{quote(epub)}",
                        # Stage A evidence, carried on the candidate so the download can be
                        # CHECKED rather than assumed. archive.org already told us all of it.
                        "expect_size": rec.get("size"),
                        "expect_md5": rec.get("md5"), "expect_sha1": rec.get("sha1"),
                        "src_ids": [k for k in (
                            ("openlibrary_work", rec.get("openlibrary_work")),
                            ("openlibrary_edition", rec.get("openlibrary_edition")),
                            ("lccn", rec.get("lccn"))) if k[1]],
                        "is_torrent": False})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out

def librivox(q, limit=6, budget=None):
    """LibriVox's `title` filter is an exact/prefix match, not a substring: `title=moby` is
    HTTP 404 and `title=^moby` returns Moby Dick (verified, both 5/5). Without the anchor
    every ordinary query came back empty, which is why the audiobook source looked dead.

    A 404 is also LibriVox's honest "nothing recorded" — `hobbit` 404s with and without the
    anchor because it is still in copyright, a true negative. When the title search finds
    nothing we spend one more call on the `author` filter (that one IS a substring match), so
    a query like "dickens", which is an author and not a title, stops being a dead search.

    `extended=1` is gone: it adds a per-chapter section listing nobody here reads (63 KB for
    two books) and costs 2.5-7.5 s against 0.5-1.0 s without it, measured 5+3 times. Both
    fields this adapter uses, `url_zip_file` and `authors`, are in the plain response.
    """
    budget = budget or _Budget()
    anchored = "^" + q.strip().lstrip("^")
    out = []
    for params in ({"title": anchored}, {"author": q.strip()}):
        if out or budget.spent():
            break
        try:
            r = _get("https://librivox.org/api/feed/audiobooks", budget=budget,
                     params={**params, "format": "json"})
            if r.status_code == 404:          # LibriVox says "no match" with a 404, not an empty list
                continue
            r.raise_for_status()
            for b in (r.json().get("books") or [])[:limit]:
                url = b.get("url_zip_file")
                if not url:
                    continue
                out.append({"source": "librivox", "kind": "audio",
                            "title": b.get("title", "?"),
                            "author": ", ".join(a.get("last_name", "") for a in b.get("authors", [])) or "Unknown",
                            "identifier": f"librivox:{b.get('id')}", "format": "zip",
                            "download_url": url, "is_torrent": False})
        except Exception:
            pass
    return out

_ADAPTERS = {"gutenberg": gutenberg, "standard_ebooks": standard_ebooks,
             "internet_archive": internet_archive, "librivox": librivox,
             "mycatalog": opds.search}

# Human-readable registry (each provider is a concrete adapter for one vetted source).
PROVIDERS = [
    {"name": "gutenberg",        "label": "Project Gutenberg",   "kind": "ebook"},
    {"name": "standard_ebooks",  "label": "Standard Ebooks",     "kind": "ebook"},
    {"name": "internet_archive", "label": "Internet Archive",    "kind": "ebook"},
    {"name": "librivox",         "label": "LibriVox",            "kind": "audio"},
    {"name": "mycatalog",        "label": "My OPDS catalog",     "kind": "ebook"},
]

def _dedupe(results):
    """The same book from the same source twice (Gutenberg lists several editions, IA the same
    scan under two identifiers) is noise on a family search page."""
    seen, out = set(), []
    for r in results:
        key = (r.get("source"), (r.get("title") or "").strip().lower(),
               (r.get("author") or "").strip().lower(), r.get("download_url"))
        short = (r.get("source"), key[1], key[2])
        if key in seen or short in seen:
            continue
        seen.add(key); seen.add(short)
        out.append(r)
    return out

def search(q, deadline=SEARCH_DEADLINE):
    """Ask every enabled catalog at once and return what answered within `deadline` seconds.
    A source that is down or slow costs the page its own results, not the whole search.

    Three fences, not one: `deadline` is how long the PAGE waits, each adapter holds a _Budget
    (SOURCE_BUDGET) covering all of its own HTTP calls so an abandoned one stops working
    shortly after the page has been rendered, and the shared _SEARCH pool caps how many
    adapter threads the whole portal can have in flight however many readers are searching."""
    names = [n for n, on in config.SOURCES.items() if on and n in _ADAPTERS]
    if not names:
        return []
    results, until = [], time.monotonic() + deadline
    futures = [_SEARCH.submit(_ADAPTERS[n], q) for n in names]
    _collect(futures, until, results.extend)
    return _dedupe(results)
