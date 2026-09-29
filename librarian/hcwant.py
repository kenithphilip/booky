"""A reader's Hardcover "Want to Read" list feeds the portal (v6.0; opt-in on Devices).

With the Hardcover token the reader already set on Devices, every 10 minutes: books newly added
to their Want to Read shelf (user_books status 1) become Get it requests, as an ebook, an
audiobook, or both (their choice). Each request then goes the usual way (bookreq.py): a copy
the family has is added to theirs with nothing downloaded; otherwise the copy found waits for
their "Yes, that one". Nothing downloads by itself.

Switching it on never floods anyone: the first sync only records what is already on the list;
Devices offers "Get the N already on it" (at most BACKLOG_MAX at a time)."""
import logging, time
import db, cwa, hardcover, bookreq

log = logging.getLogger("hcwant")

LIST_LIMIT = 100
BACKLOG_MAX = 20
QUERY = """query W($n: Int!) { me { user_books(where: {status_id: {_eq: 1}}, order_by: {id: desc}, limit: $n) {
             id book { id title contributions(limit: 3) { author { name } } } } } }"""


def want_list(token):
    """[{id, title, author, ub}] on the reader's Want to Read shelf, newest first (ub: the
    Hardcover user_book id, which only grows as books are added)."""
    d = hardcover._q(QUERY, {"n": LIST_LIMIT}, token)
    me = d.get("me") or []
    me = me[0] if isinstance(me, list) and me else (me if isinstance(me, dict) else {})
    out = []
    for ub in me.get("user_books") or []:
        b = ub.get("book") or {}
        if not b.get("id") or not (b.get("title") or "").strip():
            continue
        author = next((c["author"]["name"] for c in b.get("contributions") or [] if (c.get("author") or {}).get("name")), "")
        out.append({"id": int(b["id"]), "title": b["title"].strip(), "author": author, "ub": int(ub.get("id") or 0)})
    return out


def _kinds(kind):
    return ("ebook", "audio") if kind == "both" else (kind,)


def _request(owner, b, kind):
    n = 0
    for k in _kinds(kind):
        try:
            _rid, what = bookreq.request(owner, b["title"], b["author"], hardcover_id=str(b["id"]), kind=k, ask=True)
            n += what in ("queued", "pending", "shared")
        except bookreq.BookRequestError as e:
            log.info("Want to Read for %s: %s (%s)", owner, b["title"], e)
            return n, str(e)
    return n, None


def sync_owner(owner, token, kind, now=None):
    """Returns the number of new requests. The first sync records the list and requests nothing."""
    now = now or time.time()
    books = want_list(token)
    seen = db.hc_want_seen(owner)
    if not db.get_prefs(owner).get("hc_want_seeded"):
        # switched on (the first time, or again after being off): what is on the list NOW is only
        # recorded, never requested; an empty list is recorded too, so the next book added counts
        fresh = [b for b in books if b["id"] not in seen]
        for b in fresh:
            db.hc_want_mark(owner, b["id"], b["title"], b["author"], False, now)
        db.set_prefs_v6(owner, hc_want_seeded=now, hc_want_after=max([b.get("ub") or 0 for b in books] or [0]))
        _clear_error(owner)
        _note(owner, f"connected: {len(fresh)} book{'s' if len(fresh) != 1 else ''} already on your list "
                     f"(not requested; Devices can get them); new ones will be requested")
        return 0
    _clear_error(owner)
    after = db.hc_want_after(owner)
    made = 0
    for b in reversed(books):                   # oldest first
        if b["id"] in seen:
            continue
        if b.get("ub") and b["ub"] <= after:
            # on the list before it was recorded, only now inside the newest-LIST_LIMIT window
            # (a newer one was removed): part of the backlog, not a new book
            db.hc_want_mark(owner, b["id"], b["title"], b["author"], False, now)
            continue
        n, err = _request(owner, b, kind)
        db.hc_want_mark(owner, b["id"], b["title"], b["author"], not err, now)
        made += n
        if err:
            _note(owner, f"stopped: {err}")
            break
    if made:
        _note(owner, f"{made} new request{'s' if made != 1 else ''} from your list, {time.strftime('%Y-%m-%d %H:%M')}")
    return made


def request_backlog(owner, kind, limit=BACKLOG_MAX):
    """'Get the books already on my list': at most `limit` of them now."""
    made = 0
    for row in db.hc_want_unrequested(owner)[:limit]:
        n, err = _request(owner, {"id": row["book_id"], "title": row["title"], "author": row["author"]}, kind)
        if err:
            return made, err
        db.hc_want_mark(owner, row["book_id"], row["title"], row["author"], True)
        made += n
    return made, None


def _note(owner, text):
    db.cache_put(f"hcwant:note:{owner}", text, keep_days=365)


def _error(owner, text):
    """A problem the reader must fix (no token, token refused): shown until a sync succeeds."""
    db.cache_put(f"hcwant:err:{owner}", text, keep_days=365)


def _clear_error(owner):
    if db.cache_get(f"hcwant:err:{owner}", None):
        db.cache_put(f"hcwant:err:{owner}", "", keep_days=365)


def note(owner):
    return db.cache_get(f"hcwant:err:{owner}", None) or db.cache_get(f"hcwant:note:{owner}", None)


def sync_once():
    readers = [o for o, on in db.prefs_with("hc_want") if on]
    if not readers:
        return 0
    try:
        tokens = cwa.hardcover_tokens()
    except cwa.CwaError:
        return 0
    total = 0
    for owner in readers:
        token = tokens.get(owner)
        if not token:
            _error(owner, "no Hardcover token set (Devices -> Hardcover)")
            continue
        try:
            total += sync_owner(owner, token, db.get_prefs(owner)["hc_want_kind"])
        except hardcover.TokenRefused as e:
            _error(owner, str(e))
        except hardcover.HardcoverError as e:
            log.warning("Want to Read for %s: %s", owner, e)
    return total
