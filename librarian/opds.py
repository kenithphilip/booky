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

def _auth():
    if config.MYCATALOG_USER:
        return (config.MYCATALOG_USER, config.MYCATALOG_PASS)
    return None

def search(q, limit=12):
    url = config.MYCATALOG_URL
    if not url:
        return []
    if "{q}" in url:
        url = url.replace("{q}", urllib.parse.quote(q))
    out = []
    try:
        r = requests.get(url, headers=UA, auth=_auth(), timeout=25)
        r.raise_for_status()
        root = etree.fromstring(r.content)
        ql = q.lower().strip()
        for e in root.findall(f"{{{ATOM}}}entry"):
            title = (e.findtext(f"{{{ATOM}}}title") or "").strip()
            author = (e.findtext(f"{{{ATOM}}}author/{{{ATOM}}}name") or "").strip()
            if ql and ql not in f"{title} {author}".lower():
                continue
            # choose the best acquisition link
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
                base = urllib.parse.urlsplit(config.MYCATALOG_URL)
                href = f"{base.scheme}://{base.netloc}{href}"
            fmt = next((t for t in PREF_TYPES if t in (best[2] or "").lower()), "epub")
            out.append({"source": "mycatalog", "kind": "ebook",
                        "title": title or "(untitled)", "author": author or "Unknown",
                        "identifier": f"mycatalog:{href}", "format": fmt,
                        "download_url": href, "is_torrent": False})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out
