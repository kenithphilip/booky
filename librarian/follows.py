"""Follow a series or an author, and hear when something new is out (v5.8).

A reader follows a comic or manga series (comicmeta: Metron, ComicVine, MangaUpdates), a book
series or an author (hardcover.py). Once a day each follow is checked: what is OUT now (released,
not merely announced) and was not out at the last check becomes a NOTICE on the reader's "New for
you" list, with one tap to request it: a comic or a manga volume through the Comics flow, a book
by opening Shelfmark already searching for it (the reader picks the copy, family sharing applies).
Nothing downloads by itself. The first check only records what is already out, so following a
long series never floods anyone.

Readers who asked for mail (Devices) get one digest per check; the admin's ntfy gets a daily count."""
import datetime, logging, random, time
from urllib.parse import quote
import config, db, comicmeta, hardcover, notify

log = logging.getLogger("follows")

DAY = 86400
FAILED_RETRY = 6 * 3600
KINDS = ("comic", "book-series", "author")


class FollowError(Exception):
    pass


def _today():
    return datetime.date.today().isoformat()


def _released(date):
    """Out already: no date at all counts as out (a provider that does not know), a future date not."""
    return not date or str(date)[:10] <= _today()


def follow(owner, kind, provider, key, name, extra=None, now=None):
    if kind not in KINDS:
        raise FollowError("unknown kind of follow")
    if kind != "comic" and not hardcover.configured():
        raise FollowError("following books needs the admin's Hardcover key (Library -> Metadata sources)")
    return db.follow_add(owner, kind, provider, key, name, extra, now)


# ---- what is out now, per kind: {item_key: {title, detail, item}} ------------------------------------
def _comic_items(f):
    ex = f["extra"] or {}
    info, items = comicmeta.series(f["provider"], f["key"], ex.get("language") or "en", fresh=True)
    out = {}
    for it in items:
        if not _released(it.get("date")):
            continue
        out[it["number"]] = {"title": f"{info['name']} {it['label']}",
                             "detail": "out now" + (f" ({it['date']})" if it.get("date") else ""),
                             "item": {"type": "comic", "provider": f["provider"], "series_id": f["key"],
                                      "number": it["number"], "label": it["label"],
                                      "language": ex.get("language") or "en"}}
    return out


def _book(b, series_name=None):
    pos = f" #{b['position']}" if b.get("position") not in (None, "") else ""
    return {"title": b["title"] + (f" ({series_name}{pos})" if series_name else ""),
            "detail": f"by {b['author']}" + (f", out {b['date']}" if b.get("date") else "") if b.get("author") else
                      (f"out {b['date']}" if b.get("date") else ""),
            "item": {"type": "book", "title": b["title"], "author": b.get("author") or ""}}


def _book_series_items(f):
    name, _done, books = hardcover.series_books(f["key"])
    return {b["id"]: _book(b, name or f["name"]) for b in books if _released(b.get("date"))}


def _author_items(f):
    _name, books = hardcover.author_books(f["key"])
    return {b["id"]: _book(b) for b in books if _released(b.get("date"))}


CHECKERS = {"comic": _comic_items, "book-series": _book_series_items, "author": _author_items}


def check(f, now=None):
    """One follow: record what is out; what is new since the last check becomes a notice.
    Returns the number of new notices (0 on the first check)."""
    now = now or time.time()
    nxt = now + DAY + random.uniform(0, 3600)          # spread over the day
    try:
        current = CHECKERS[f["kind"]](f)
    except (comicmeta.MetaError, hardcover.HardcoverError, KeyError, ValueError) as e:
        db.follow_retry(f["id"], now + FAILED_RETRY, f"could not check: {str(e)[:200]}")
        return 0
    known = f.get("known")
    new = 0
    if known is not None:
        seen = set(known)
        for key, n in current.items():
            if key in seen:
                continue
            if db.notice_add(f["owner"], f["id"], key, n["title"], n["detail"], n["item"], now):
                new += 1
    db.follow_checked(f["id"], sorted(set(current) | set(known or [])), nxt,
                      f"{len(current)} out" + (f", {new} new" if new else ""), now)
    return new


def run_once(now=None, limit=10):
    now = now or time.time()
    total = 0
    for f in db.follow_due(now, limit):
        total += check(f, now)
    if total:
        mail_digests()
    admin_summary(now)
    return total


# ---- one tap ---------------------------------------------------------------------------------------
def shelfmark_search_url(title, author):
    base = f"https://shelf.{config.DOMAIN}" if config.DOMAIN else "/"
    return f"{base}/#q={quote(title)}" + (f"&author={quote(author)}" if author else "")


def act(owner, nid, action):
    """'request' (a comic: queued through the Comics flow; a book: the Shelfmark search to open)
    or 'dismiss'. Returns (what, url_or_None)."""
    n = db.notice_get(nid)
    if not n or n["owner"] != owner:
        raise FollowError("no such notice")
    if action == "dismiss":
        db.notice_set(nid, "dismissed")
        return "dismissed", None
    it = n["item"]
    if it.get("type") == "comic":
        import comics
        info, items = comicmeta.series(it["provider"], it["series_id"], it.get("language") or "en")
        item = next((i for i in items if i["number"] == it["number"]), None) or \
            {"number": it["number"], "label": it.get("label") or it["number"]}
        _rid, what = comics.request(owner, info, item, it.get("language"))
        db.notice_set(nid, "requested")
        return what, None
    db.notice_set(nid, "requested")
    return "shelfmark", shelfmark_search_url(it.get("title") or n["title"], it.get("author") or "")


# ---- telling people ---------------------------------------------------------------------------------
def mail_digests():
    """One mail per reader who asked for mail (Devices), listing what is new for them."""
    import cwa, kindle
    rows = db.notices_unmailed()
    if not rows or not kindle.configured():
        return 0
    by = {}
    for n in rows:
        by.setdefault(n["owner"], []).append(n)
    sent = 0
    for owner, items in by.items():
        ids = [n["id"] for n in items]
        if not db.get_prefs(owner).get("notify_email"):
            db.notices_mailed(ids)                 # never mailed later in a surprise batch
            continue
        u = cwa.get_user(owner) or {}
        if not (u.get("email") and "@" in u["email"]):
            db.notices_mailed(ids)
            continue
        lines = "\n".join(f"- {n['title']}" + (f" — {n['detail']}" if n.get("detail") else "") for n in items)
        try:
            notify._deliver(u["email"], f"[library] new for you: {items[0]['title']}" + (f" and {len(items) - 1} more" if len(items) > 1 else ""),
                            f"Something you follow has something new:\n\n{lines}\n\nRequest it with one tap: "
                            f"https://request.{config.DOMAIN}/\n")
            sent += 1
        except Exception as e:
            log.warning("could not mail %s their notices: %s", owner, e)
            continue
        db.notices_mailed(ids)
    return sent


_SUMMARY = {"at": 0.0}


def admin_summary(now=None):
    """Once a day, the admin's ntfy: how many new releases turned up for how many readers."""
    now = now or time.time()
    if now - _SUMMARY["at"] < DAY:
        return None
    last = db.cache_get("follows:summary", None) or 0
    if now - float(last) < DAY:
        _SUMMARY["at"] = float(last)
        return None
    n, readers = db.notices_since(now - DAY)
    _SUMMARY["at"] = now
    db.cache_put("follows:summary", now, keep_days=30)
    if n:
        notify.alert(f"{n} new release{'s' if n != 1 else ''} for {readers} reader{'s' if readers != 1 else ''}",
                     "Things the family follows came out in the last day. They are on each reader's "
                     "New for you list.", "low", seq="follows-daily", tags="sparkles")
    return n
