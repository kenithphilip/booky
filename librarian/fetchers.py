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
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import requests, config
from urllib.parse import urlsplit
from lxml import etree
import opds

UA = {"User-Agent": "bookstack-librarian/2.0"}
# One slow catalog used to hold the whole page: the search ran the adapters one after the
# other with a 20 s timeout each (16-35 s pages were normal). Every HTTP call now has a short
# timeout, the adapters run in parallel and the page renders with whatever answered in time.
TIMEOUT = 8
SEARCH_DEADLINE = 12

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

def _get(url, **kw):
    return requests.get(url, headers=UA, timeout=TIMEOUT, **kw)

def gutenberg(q, limit=8):
    out = []
    try:
        r = _get("https://gutendex.com/books", params={"search": q}).json()
        for b in r.get("results", [])[:limit]:
            fmts = b.get("formats", {})
            url = (fmts.get("application/epub+zip")
                   or next((v for k, v in fmts.items() if k.startswith("application/epub")), None))
            if not url:
                continue
            if config.GUTENBERG_MIRROR:
                url = url.replace("https://www.gutenberg.org", config.GUTENBERG_MIRROR.rstrip("/"))
            out.append({"source": "gutenberg", "kind": "ebook",
                        "title": b.get("title", "?"),
                        "author": ", ".join(a["name"] for a in b.get("authors", [])) or "Unknown",
                        "identifier": f"gutenberg:{b.get('id')}",
                        "format": "epub", "download_url": url, "is_torrent": False})
    except Exception:
        pass
    return out

_SE_CACHE = {"feed": None, "at": 0.0}
SE_CACHE_SECONDS = 6 * 3600

def _se_feed():
    """The whole Standard Ebooks catalog feed, cached for a few hours. A failed fetch (the feed
    answers 401 without a Patrons Circle login) raises and is NOT cached, so the next search
    tries again instead of silently finding nothing until the portal restarts."""
    if _SE_CACHE["feed"] is None or time.time() - _SE_CACHE["at"] > SE_CACHE_SECONDS:
        r = _get("https://standardebooks.org/feeds/opds/all")
        r.raise_for_status()
        _SE_CACHE.update(feed=r.content, at=time.time())
    return _SE_CACHE["feed"]

def standard_ebooks(q, limit=8):
    out = []
    try:
        root = etree.fromstring(_se_feed())
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

def internet_archive(q, limit=8):
    out = []
    try:
        coll = " OR ".join(f"collection:{c}" for c in config.IA_COLLECTIONS)
        params = {"q": f'({q}) AND mediatype:texts AND ({coll})',
                  "fl[]": ["identifier", "title", "creator"], "rows": limit, "output": "json"}
        docs = _get("https://archive.org/advancedsearch.php", params=params).json()["response"]["docs"]
        for d in docs:
            ident = d["identifier"]
            meta = _get(f"https://archive.org/metadata/{ident}").json()
            epub = next((f["name"] for f in meta.get("files", [])
                         if f.get("name", "").lower().endswith(".epub")), None)
            if not epub:
                continue
            out.append({"source": "internet_archive", "kind": "ebook",
                        "title": d.get("title", ident),
                        "author": (d.get("creator") if isinstance(d.get("creator"), str)
                                   else ", ".join(d.get("creator", [])) or "Unknown"),
                        "identifier": f"ia:{ident}", "format": "epub",
                        "download_url": f"https://archive.org/download/{ident}/{epub}",
                        "is_torrent": False})
    except Exception:
        pass
    return out

def librivox(q, limit=6):
    out = []
    try:
        r = _get("https://librivox.org/api/feed/audiobooks",
                 params={"title": q, "format": "json", "extended": 1}).json()
        for b in r.get("books", [])[:limit]:
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
    A source that is down or slow costs the page its own results, not the whole search."""
    names = [n for n, on in config.SOURCES.items() if on and n in _ADAPTERS]
    if not names:
        return []
    results, until = [], time.monotonic() + deadline
    ex = ThreadPoolExecutor(max_workers=len(names))
    try:
        futures = [ex.submit(_ADAPTERS[n], q) for n in names]
        for f in futures:
            try:
                results.extend(f.result(timeout=max(0.1, until - time.monotonic())))
            except FutureTimeout:
                pass                  # that source keeps working in its thread; the page does not wait
            except Exception:
                pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return _dedupe(results)
