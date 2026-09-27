"""Search a self-hosted OPDS catalog (your own books server). OPDS is the standard
personal-library protocol spoken by Calibre, Calibre-Web, Kavita, Komga, BookLore, etc.
Single HTTP Basic credential supported (matches a one-user server)."""
import requests, urllib.parse
from lxml import etree
import config

UA = {"User-Agent": "bookstack-librarian/3.0"}
ATOM = "http://www.w3.org/2005/Atom"
ACQ_RELS = ("http://opds-spec.org/acquisition",
            "http://opds-spec.org/acquisition/open-access")
PREF_TYPES = ("epub", "kepub", "mobi", "azw3", "pdf")
# Your own catalog is usually on the tailnet and fast, but it is still one source on a page
# that gives up after fetchers.SEARCH_DEADLINE (12 s). The old 25 s left a thread fetching from
# a wedged catalog long after the reader had been shown the page. (Not imported from fetchers:
# fetchers imports this module.)
TIMEOUT = 10

DC = "http://purl.org/dc/terms/"
DC11 = "http://purl.org/dc/elements/1.1/"


def _legacy():
    return {"source": "mycatalog", "url": config.MYCATALOG_URL, "user": config.MYCATALOG_USER,
            "password": config.MYCATALOG_PASS}


def _ids(e):
    """dc:identifier values an OPDS entry carries (urn:isbn:…, urn:uuid:…, plain ISBNs): the
    evidence the work page scores a copy with."""
    out = []
    for tag in (f"{{{DC}}}identifier", f"{{{DC11}}}identifier"):
        for el in e.findall(tag):
            v = (el.text or "").strip().lower()
            if v.startswith("urn:isbn:") or v.replace("-", "").isdigit():
                out.append(("isbn", v.replace("urn:isbn:", "")))
            elif v.startswith("urn:uuid:"):
                out.append(("uuid", v[9:]))
    return out


def search(q, limit=12, budget=None, catalog=None):
    """Search ONE OPDS catalog (catalogs.py). catalog=None: the v4 single catalog in .env."""
    cat = catalog or _legacy()
    url = cat["url"]
    if not url:
        return []
    if "{q}" in url:
        url = url.replace("{q}", urllib.parse.quote(q))
    out = []
    try:
        # Build one when the caller did not, exactly as the other adapters do. Without it
        # the fallback is a per-connection timeout, not a wall-clock bound: a catalog that
        # accepts the connection and then stalls could outlive both SOURCE_BUDGET and the
        # page's own SEARCH_DEADLINE, which is what the budget exists to prevent.
        # imported here, not at module scope: fetchers imports this module
        import fetchers
        budget = budget or fetchers._Budget()
        auth = (cat["user"], cat["password"]) if cat.get("user") else None
        r = requests.get(url, headers=UA, auth=auth, timeout=budget.pair(read=TIMEOUT))
        r.raise_for_status()
        root = etree.fromstring(r.content)
        ql = q.lower().strip()
        base = urllib.parse.urlsplit(cat["url"])
        for e in root.findall(f"{{{ATOM}}}entry"):
            title = (e.findtext(f"{{{ATOM}}}title") or "").strip()
            author = (e.findtext(f"{{{ATOM}}}author/{{{ATOM}}}name") or "").strip()
            if ql and not all(w in f"{title} {author}".lower() for w in ql.split()):
                continue
            best = None
            for link in e.findall(f"{{{ATOM}}}link"):
                rel, typ, href = link.get("rel", ""), link.get("type", ""), link.get("href", "")
                if any(a in rel for a in ACQ_RELS) and href:
                    score = next((i for i, t in enumerate(PREF_TYPES) if t in typ.lower()), 99)
                    if best is None or score < best[0]:
                        best = (score, href, typ)
            if not best:
                continue
            href = best[1]
            if href.startswith("/"):
                href = f"{base.scheme}://{base.netloc}{href}"
            fmt = next((t for t in PREF_TYPES if t in (best[2] or "").lower()), "epub")
            lang = (e.findtext(f"{{{DC}}}language") or e.findtext(f"{{{DC11}}}language") or "").strip() or None
            out.append({"source": cat["source"], "kind": "ebook",
                        "title": title or "(untitled)", "author": author or "Unknown",
                        "identifier": f"{cat['source']}:{href}", "format": fmt, "language": lang,
                        "src_ids": _ids(e), "download_url": href, "is_torrent": False})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out
