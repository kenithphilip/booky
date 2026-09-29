"""What a comic or manga IS: series, its issues or volumes, kind, reading direction (docs/COMICS.md).

Three free providers, each for what it describes best:
  metron        Western comics (metron.cloud, a free account: its API key, or user + password; 20 requests/min)
  comicvine     Western comics, the fallback (a free API key; 200 requests/resource/hour)
  mangaupdates  manga, manhwa, manhua (no key)
Results are cached in the portal's database (a day), so a reader paging through a series costs
the provider one request, and a provider that is down shows the cached answer.

A series comes back as {provider, id, name, year, publisher, kind, count, desc, cover, language,
alt_names, authors}; its items as [{number, label, date, cover, id}] (issues for comics, volumes
for manga: request what people actually download).
Kinds: comic (left to right), manga (right to left), manhwa / manhua (left to right, often a long
vertical strip), novel (text: not a comic, offered as a book request instead)."""
import json, re, threading
import requests
import config, db

UA = f"bookstack-librarian/{config.BUILD_VERSION} (self-hosted family library)"
TIMEOUT = (5, 20)
CACHE_SECONDS = 86400
MAX_ITEMS = 400

METRON = "https://metron.cloud/api"
COMICVINE = "https://comicvine.gamespot.com/api"
MANGAUPDATES = "https://api.mangaupdates.com/v1"

READING = {"comic": "ltr", "collected": "ltr", "manga": "rtl", "manhwa": "ltr", "manhua": "ltr", "novel": "ltr"}
KIND_LABEL = {"comic": "Comic", "collected": "Collected edition", "manga": "Manga", "manhwa": "Manhwa",
              "manhua": "Manhua", "novel": "Novel"}
VOLUME_KINDS = ("manga", "manhwa", "manhua", "collected")      # numbered by volume, not issue
# Metron series types whose "issues" are volumes (Saga TPB, Batman: Year One HC ...)
COLLECTED_TYPES = ("trade paperback", "hard cover", "hardcover", "omnibus", "graphic novel")


class MetaError(Exception):
    pass


def providers():
    """Which providers can answer, in the order they are asked."""
    western = []
    if config.METRON_TOKEN or (config.METRON_USER and config.METRON_PASS):
        western.append("metron")
    if config.COMICVINE_API_KEY:
        western.append("comicvine")
    return {"comic": western, "manga": ["mangaupdates"]}


def kind_from_mangaupdates(t):
    t = (t or "").strip().lower()
    if t in ("manhwa",):
        return "manhwa"
    if t in ("manhua",):
        return "manhua"
    if t in ("novel", "light novel", "web novel"):
        return "novel"
    if t in ("oel", "french", "spanish", "nordic", "filipino", "indonesian", "thai", "vietnamese", "malaysian"):
        return "comic"
    return "manga"                      # Manga, Doujinshi, Artbook, and anything new


def kind_from_metron(series_type_name, name=""):
    t = (series_type_name or "").lower()
    if "manga" in t or "manga" in (name or "").lower():
        return "manga"
    if any(c in t for c in COLLECTED_TYPES):
        return "collected"
    return "comic"


# ---- HTTP and cache ----------------------------------------------------------------------------
_FRESH = threading.local()      # follows.py's daily check: ask the provider, not the day-old cache

def _get_json(url, params=None, auth=None, method="GET", body=None, headers=None):
    key = "cm:" + json.dumps([method, url, params, body], sort_keys=True)
    hit = None if getattr(_FRESH, "on", False) else db.cache_get(key, CACHE_SECONDS)
    if hit is not None:
        return hit
    try:
        r = requests.request(method, url, params=params, json=body, auth=auth, timeout=TIMEOUT,
                             headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    except requests.RequestException as e:
        stale = db.cache_get(key, None)                  # a provider that is down: the last answer
        if stale is not None:
            return stale
        raise MetaError(f"{_host(url)} did not answer ({type(e).__name__})") from e
    if r.status_code == 429:
        raise MetaError(f"{_host(url)} asked us to slow down; try again in a minute")
    if r.status_code in (401, 403):
        raise MetaError(f"{_host(url)} refused the login or key (HTTP {r.status_code}): Library -> Comics")
    if r.status_code != 200:
        raise MetaError(f"{_host(url)} answered HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise MetaError(f"{_host(url)} sent something that is not JSON") from e
    db.cache_put(key, data)
    return data


def _host(url):
    return re.sub(r"^https?://([^/]+).*$", r"\1", url)


def _metron_auth():
    """Metron's API key as a Bearer token (what its own client, mokkari, sends; it wins over a
    login), else the account's user name and password."""
    if config.METRON_TOKEN:
        return {"headers": {"Authorization": f"Bearer {config.METRON_TOKEN}"}}
    return {"auth": (config.METRON_USER, config.METRON_PASS)}


def _cv(path, **params):
    return _get_json(f"{COMICVINE}/{path}", params=dict(params, api_key=config.COMICVINE_API_KEY, format="json"))


def _num_label(kind, number):
    return f"Vol. {number}" if kind in VOLUME_KINDS else f"#{number}"


def _clean_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# ---- search ------------------------------------------------------------------------------------
def search(query, kind="comic", limit=20):
    """[series] for a name. kind 'comic' asks the Western providers, 'manga' MangaUpdates (which
    also knows manhwa and manhua). Raises MetaError only when every provider failed."""
    query = (query or "").strip()
    if not query:
        return []
    errors = []
    for prov in providers()["manga" if kind != "comic" else "comic"]:
        try:
            return {"metron": _metron_search, "comicvine": _cv_search, "mangaupdates": _mu_search}[prov](query, limit)
        except MetaError as e:
            errors.append(str(e))
    if not providers()["manga" if kind != "comic" else "comic"]:
        raise MetaError("no comic metadata provider is set up (Library -> Comics: a free Metron account)")
    raise MetaError("; ".join(errors))


def _metron_search(query, limit):
    d = _get_json(f"{METRON}/series/", params={"name": query}, **_metron_auth())
    out = []
    for s in (d.get("results") or [])[:limit]:
        name = s.get("display_name") or s.get("series") or s.get("name") or ""
        pub = (s.get("publisher") or {}).get("name") if isinstance(s.get("publisher"), dict) else None
        st = (s.get("series_type") or {}).get("name") if isinstance(s.get("series_type"), dict) else None
        out.append({"provider": "metron", "id": str(s.get("id")), "name": _strip_year(name),
                    "year": s.get("year_began"), "publisher": pub, "kind": kind_from_metron(st, name),
                    "count": s.get("issue_count"), "volume": s.get("volume"), "cover": None, "desc": ""})
    return out


def _cv_search(query, limit):
    d = _cv("search/", query=query, resources="volume", limit=limit,
            field_list="id,name,start_year,publisher,count_of_issues,image,deck")
    out = []
    for s in d.get("results") or []:
        out.append({"provider": "comicvine", "id": str(s.get("id")), "name": s.get("name") or "",
                    "year": _int(s.get("start_year")), "publisher": (s.get("publisher") or {}).get("name"),
                    "kind": "comic", "count": s.get("count_of_issues"),
                    "cover": (s.get("image") or {}).get("thumb_url"), "desc": _clean_html(s.get("deck"))})
    return out


def _mu_search(query, limit):
    d = _get_json(f"{MANGAUPDATES}/series/search", method="POST", body={"search": query, "perpage": limit})
    out = []
    for r in d.get("results") or []:
        s = r.get("record") or {}
        out.append({"provider": "mangaupdates", "id": str(s.get("series_id")), "name": s.get("title") or "",
                    "year": _int(s.get("year")), "publisher": None, "kind": kind_from_mangaupdates(s.get("type")),
                    "count": None, "cover": ((s.get("image") or {}).get("url") or {}).get("thumb"),
                    "desc": _clean_html(s.get("description"))[:400]})
    return out


# ---- one series and its issues / volumes -----------------------------------------------------------
def series(provider, sid, language="en", fresh=False):
    """(series, items). For manga the items are volumes 1..N, N being the ENGLISH volume count
    when the reader reads English and an English publisher is listed (what can be found in
    English), else the original count. fresh: past the cache (the answer is cached again)."""
    if fresh:
        _FRESH.on = True
        try:
            return series(provider, sid, language)
        finally:
            _FRESH.on = False
    if provider == "metron":
        return _metron_series(sid)
    if provider == "comicvine":
        return _cv_series(sid)
    if provider == "mangaupdates":
        return _mu_series(sid, language)
    raise MetaError(f"unknown provider {provider!r}")


def _metron_series(sid):
    s = _get_json(f"{METRON}/series/{int(sid)}/", **_metron_auth())
    st = (s.get("series_type") or {}).get("name") if isinstance(s.get("series_type"), dict) else None
    name = s.get("name") or s.get("display_name") or ""
    info = {"provider": "metron", "id": str(sid), "name": _strip_year(name), "year": s.get("year_began"),
            "publisher": (s.get("publisher") or {}).get("name"), "kind": kind_from_metron(st, name),
            "count": s.get("issue_count"), "desc": _clean_html(s.get("desc")), "cover": None,
            "language": s.get("language") or "en", "alt_names": s.get("alt_names") or [], "authors": [],
            "volume": s.get("volume"), "cv_id": s.get("cv_id")}
    items, url, params = [], f"{METRON}/issue/", {"series_id": int(sid)}
    while url and len(items) < MAX_ITEMS:
        page = _get_json(url, params=params, **_metron_auth())
        for i in page.get("results") or []:
            sr = i.get("series") or {}
            if sr.get("id") not in (None, int(sid)) and str(sr.get("id")) != str(sid):
                continue                                  # a filter the server did not apply
            items.append({"id": str(i.get("id")), "number": str(i.get("number") or "").strip(),
                          "date": i.get("store_date") or i.get("cover_date"), "cover": i.get("image")})
        url, params = page.get("next"), None
    if not info["cover"] and items:
        info["cover"] = items[0].get("cover")
    return info, _finish_items(info["kind"], items)


def _cv_series(sid):
    v = _cv(f"volume/4050-{int(sid)}/", field_list="id,name,start_year,publisher,count_of_issues,image,deck,description")
    s = v.get("results") or {}
    info = {"provider": "comicvine", "id": str(sid), "name": s.get("name") or "", "year": _int(s.get("start_year")),
            "publisher": (s.get("publisher") or {}).get("name"), "kind": "comic", "count": s.get("count_of_issues"),
            "desc": _clean_html(s.get("deck") or s.get("description"))[:600],
            "cover": (s.get("image") or {}).get("medium_url"), "language": "en", "alt_names": [], "authors": []}
    items, offset = [], 0
    while len(items) < MAX_ITEMS:
        page = _cv("issues/", filter=f"volume:{int(sid)}", sort="issue_number:asc", limit=100, offset=offset,
                   field_list="id,issue_number,name,cover_date,store_date,image")
        rows = page.get("results") or []
        for i in rows:
            items.append({"id": str(i.get("id")), "number": str(i.get("issue_number") or "").strip(),
                          "date": i.get("store_date") or i.get("cover_date"), "cover": (i.get("image") or {}).get("thumb_url"),
                          "title": i.get("name")})
        offset += len(rows)
        if not rows or offset >= int(page.get("number_of_total_results") or 0):
            break
    return info, _finish_items("comic", items)


VOLUMES_RE = re.compile(r"(\d+)\s+Volumes?", re.I)


def _mu_series(sid, language="en"):
    s = _get_json(f"{MANGAUPDATES}/series/{int(sid)}")
    kind = kind_from_mangaupdates(s.get("type"))
    original = _int((VOLUMES_RE.search(s.get("status") or "") or [None, None])[1])
    english = None
    for p in s.get("publishers") or []:
        if (p.get("type") or "").lower() == "english":
            n = _int((VOLUMES_RE.search(p.get("notes") or "") or [None, None])[1])
            if n and (english is None or n > english):
                english = n
    count = english if (language == "en" and english) else original
    info = {"provider": "mangaupdates", "id": str(sid), "name": s.get("title") or "", "year": _int(s.get("year")),
            "publisher": next((p.get("publisher_name") for p in s.get("publishers") or []
                               if (p.get("type") or "").lower() == ("english" if language == "en" else "original")), None),
            "kind": kind, "count": count, "count_original": original, "count_english": english,
            "desc": _clean_html(s.get("description"))[:600],
            "cover": ((s.get("image") or {}).get("url") or {}).get("original"),
            "language": language, "alt_names": [a.get("title") for a in s.get("associated") or [] if a.get("title")][:30],
            "authors": sorted({a.get("name") for a in s.get("authors") or [] if a.get("name")}),
            "completed": bool(s.get("completed")), "status": (s.get("status") or "").strip(),
            "latest_chapter": _int(s.get("latest_chapter"))}     # v5.9: following chapters
    items = [{"id": f"{sid}:v{n}", "number": str(n), "date": None, "cover": None} for n in range(1, (count or 0) + 1)]
    return info, _finish_items(kind, items)


def _finish_items(kind, items):
    seen, out = set(), []
    for i in items:
        if not i["number"] or i["number"] in seen:
            continue
        seen.add(i["number"])
        i["label"] = _num_label(kind, i["number"])
        out.append(i)
    return sorted(out, key=lambda i: (number_key(i["number"]), i["number"]))


# ---- helpers ------------------------------------------------------------------------------------
def number_key(n):
    """'1' < '1.5' < '2' < '10' < 'Annual 1': numbers numerically, anything else after them."""
    try:
        return (0, float(str(n).strip().lstrip("#")))
    except ValueError:
        return (1, 0.0)


def same_number(a, b):
    try:
        return float(str(a).lstrip("#")) == float(str(b).lstrip("#"))
    except ValueError:
        return str(a).strip().lower() == str(b).strip().lower()


def _strip_year(name):
    return re.sub(r"\s*\((\d{4})\)\s*$", "", name or "").strip()


def _int(v):
    try:
        return int(str(v).strip()[:4]) if v not in (None, "") else None
    except ValueError:
        return None


def check():
    """For Library -> Comics: can each configured provider answer? {provider: 'ok' | error}"""
    out = {}
    probes = {"metron": lambda: _metron_search("batman", 1), "comicvine": lambda: _cv_search("batman", 1),
              "mangaupdates": lambda: _mu_search("one piece", 1)}
    for prov in {p for ps in providers().values() for p in ps}:
        try:
            db.cache_clear_prefix("cm:")
            out[prov] = "ok" if probes[prov]() is not None else "no answer"
        except MetaError as e:
            out[prov] = str(e)
    return out


if __name__ == "__main__":
    import sys
    db.init()
    if sys.argv[1:] == ["check"]:
        res = check()
        print(json.dumps({"ok": all(v == "ok" for v in res.values()) and bool(res), "providers": res}))
        sys.exit(0 if res and all(v == "ok" for v in res.values()) else 1)
    print("usage: python -m comicmeta check", file=sys.stderr)
    sys.exit(2)
