"""Which chapters a manga volume holds, from MangaDex (v5.9: swapping chapters for the volume).

MangaUpdates (comicmeta.py) says how many volumes and the latest chapter, but not which chapters
are in which volume. MangaDex's free API does (no key): GET /manga/{id}/aggregate lists each
volume with its chapters. The MangaDex entry is found by title and accepted ONLY when its
links.mu is this series' MangaUpdates id (base 36, measured 2026-09-29: Chainsaw Man, MU
75336092483 = 'ylx5wzn'), so a "(Official Colored)" edition or a same-named series is never used.

The answer is community data and can be untidy (a volume listing chapters of the next), so it
only PRE-TICKS the chapters a reader is offered to remove; nothing is removed without them."""
import requests
import db

API = "https://api.mangadex.org"
TIMEOUT = (5, 20)
CACHE = 7 * 86400
UA = {"User-Agent": "bookstack-librarian (self-hosted family library)"}


def base36(n):
    n, s, a = int(n), "", "0123456789abcdefghijklmnopqrstuvwxyz"
    while n:
        n, r = divmod(n, 36)
        s = a[r] + s
    return s or "0"


def _get(path, params=None):
    r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def volumes(mu_id, name):
    """{volume number (float): [chapter numbers]} for the MangaUpdates series, or None."""
    key = f"mangadex:vol:{mu_id}"
    hit = db.cache_get(key, CACHE)
    if hit is not None:
        return {float(k): v for k, v in hit.items()} if hit else None
    want = base36(mu_id)
    try:
        found = _get("/manga", {"title": name, "limit": 10}).get("data") or []
        md = next((m for m in found if ((m.get("attributes") or {}).get("links") or {}).get("mu") == want), None)
        if not md:
            db.cache_put(key, {}, keep_days=14)
            return None
        agg = _get(f"/manga/{md['id']}/aggregate").get("volumes") or {}
    except (requests.RequestException, ValueError):
        return None                              # not cached: asked again next time
    out = {}
    for vol, v in agg.items():
        n = _num(vol)
        if n is None:
            continue                             # 'none': chapters not in a volume yet
        out[n] = sorted({c for c in (_num(k) for k in (v.get("chapters") or {})) if c is not None})
    db.cache_put(key, {str(k): v for k, v in out.items()}, keep_days=14)
    return out or None


def chapters_in(mu_id, name, volume):
    """[chapter numbers] MangaDex lists in this volume, or None when it cannot say."""
    v = volumes(mu_id, name) or {}
    got = v.get(_num(volume))
    return got or None
