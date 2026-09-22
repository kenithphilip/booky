"""Best-effort metadata enrichment from Open Library (open data). Server-side and cached,
so users' searches aren't sent to a third party from their browsers. Retrieval of the
actual book still happens only through the legal content adapters."""
import requests
import config

UA = {"User-Agent": "bookstack-librarian/4.0"}
_cache = {}

def for_book(title, author=""):
    if not config.ENRICH_METADATA or not title:
        return {}
    key = f"{title}|{author}".lower()
    if key in _cache:
        return _cache[key]
    out = {}
    try:
        params = {"title": title, "limit": 1,
                  "fields": "cover_i,subtitle,first_sentence"}
        if author:
            params["author"] = author
        docs = requests.get("https://openlibrary.org/search.json",
                            params=params, headers=UA, timeout=8).json().get("docs") or []
        if docs:
            d = docs[0]
            if d.get("cover_i"):
                out["cover_url"] = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg"
            fs = d.get("first_sentence")
            if isinstance(fs, list):
                fs = fs[0] if fs else None
            blurb = d.get("subtitle") or fs or ""
            if blurb:
                out["blurb"] = blurb[:240]
    except Exception:
        pass
    if len(_cache) < 5000:
        _cache[key] = out
    return out
