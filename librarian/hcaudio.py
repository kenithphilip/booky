"""Audiobook listening progress, from Audiobookshelf to each reader's own Hardcover (v5.9.1).

Calibre-Web already sends a reader's Kobo/KOReader EBOOK progress to Hardcover with the token they
set on Devices (app.db user.hardcover_token). Audiobooks never reached it. This does, with the
same token, so nothing new is asked of the reader:

  * each reader's progress from Audiobookshelf's admin API (GET /api/users/:id mediaProgress);
  * the Hardcover book found by the item's ASIN (an audiobook edition, reading format 2), else its
    ISBN, else its exact title and an overlapping author; never guessed, and remembered;
  * the reader's own Hardcover entry for it: added as Currently Reading (Read when Audiobookshelf
    says finished), and the newest unfinished read updated with progress_seconds, started_at,
    finished_at and the audiobook edition (all fields every time: a field left out is lost), or
    a new read when there is none. A book already Read on Hardcover is not reopened by a re-listen.

Only when something moved: 5 minutes of listening, or finishing. Hardcover's free limit is 60
requests a minute and 5,000 a day; this uses at most PER_RUN per run (every 10 minutes).
Schema measured live 2026-09-29 (insert_user_book / insert_user_book_read / update_user_book_read,
DatesReadInput, user_book_statuses 2 Currently Reading and 3 Read, reading_formats 2 Listened)."""
import datetime, logging, time
import db, cwa, dedupe, hardcover
import abs as absapi

log = logging.getLogger("hcaudio")

PER_RUN = 40
MIN_STEP = 300                    # seconds of listening before progress is sent again
READING, READ = 2, 3
LISTENED = 2


def _date(ms):
    try:
        return datetime.datetime.fromtimestamp(float(ms) / 1000, datetime.timezone.utc).date().isoformat() if ms else None
    except (TypeError, ValueError, OverflowError):
        return None


class _Budget:
    def __init__(self, n):
        self.n = n

    def q(self, query, variables, token):
        if self.n <= 0:
            raise StopIteration
        self.n -= 1
        return hardcover._q(query, variables, token)


def _audio_edition(b, book_id, token):
    d = b.q("query E($b: Int!) { editions(where: {book_id: {_eq: $b}, reading_format_id: {_eq: 2}}, "
            "order_by: {users_count: desc_nulls_last}, limit: 1) { id } }", {"b": book_id}, token)
    e = d.get("editions") or []
    return e[0]["id"] if e else None


def match(b, meta, token):
    """(book_id, edition_id, how) or (None, None, why not)."""
    if meta.get("asin"):
        d = b.q("query A($a: String!) { editions(where: {asin: {_eq: $a}}, limit: 5) { id book_id reading_format_id } }",
                {"a": meta["asin"]}, token)
        eds = d.get("editions") or []
        e = next((x for x in eds if x.get("reading_format_id") == LISTENED), None) or (eds[0] if eds else None)
        if e:
            return e["book_id"], e["id"] if e.get("reading_format_id") == LISTENED else _audio_edition(b, e["book_id"], token), "asin"
    isbn = "".join(ch for ch in meta.get("isbn") or "" if ch.isdigit() or ch in "Xx").upper()
    if len(isbn) in (10, 13):
        field = "isbn_13" if len(isbn) == 13 else "isbn_10"
        d = b.q(f"query I($i: String!) {{ editions(where: {{{field}: {{_eq: $i}}}}, limit: 1) {{ id book_id reading_format_id }} }}",
                {"i": isbn}, token)
        eds = d.get("editions") or []
        if eds:
            e = eds[0]
            return e["book_id"], e["id"] if e.get("reading_format_id") == LISTENED else _audio_edition(b, e["book_id"], token), "isbn"
    if meta.get("title"):
        d = b.q("query S($q: String!) { search(query: $q, query_type: \"Book\", per_page: 5, page: 1) { results } }",
                {"q": f"{meta['title']} {meta.get('author') or ''}".strip()}, token)
        want_t, want_a = dedupe.norm_title(meta["title"]), dedupe.author_tokens(meta.get("author") or "")
        for doc in hardcover._hits((d.get("search") or {}).get("results")):
            names = " ".join(doc.get("author_names") or [])
            if dedupe.norm_title(doc.get("title") or "") == want_t and want_a and want_a & dedupe.author_tokens(names):
                bid = int(doc["id"])
                return bid, _audio_edition(b, bid, token), "title and author"
    return None, None, "not found on Hardcover (no ASIN/ISBN match, no exact title and author)"


def _write(b, token, book_id, edition_id, p):
    """The reader's Hardcover entry for one book, brought up to this progress."""
    finished = bool(p.get("isFinished"))
    d = b.q("""query U($b: Int!) { me { user_books(where: {book_id: {_eq: $b}}) { id status_id
              user_book_reads(order_by: {id: desc}, limit: 5) { id started_at finished_at progress_seconds edition_id } } } }""",
            {"b": book_id}, token)
    me = (d.get("me") or [{}])
    me = me[0] if isinstance(me, list) and me else (me if isinstance(me, dict) else {})
    ubs = me.get("user_books") or []
    status = READ if finished else READING
    if not ubs:
        r = b.q("""mutation C($o: UserBookCreateInput!) { insert_user_book(object: $o) { error id } }""",
                {"o": {"book_id": book_id, "edition_id": edition_id, "status_id": status}}, token)
        res = r.get("insert_user_book") or {}
        if res.get("error") or not res.get("id"):
            raise hardcover.HardcoverError(f"Hardcover did not add the book: {res.get('error') or 'no id'}")
        ub_id, reads = res["id"], []
    else:
        ub = ubs[0]
        if ub.get("status_id") == READ and not finished:
            return "already Read on Hardcover (a re-listen is not sent)"
        ub_id, reads = ub["id"], ub.get("user_book_reads") or []
        if ub.get("status_id") != status:
            b.q("mutation S($id: Int!, $o: UserBookUpdateInput!) { update_user_book(id: $id, object: $o) { error id } }",
                {"id": ub_id, "o": {"status_id": status}}, token)
    obj = {"progress_seconds": int(p.get("currentTime") or 0), "started_at": _date(p.get("startedAt")) or _date(p.get("lastUpdate")),
           "finished_at": _date(p.get("finishedAt")) or (datetime.date.today().isoformat() if finished else None),
           "edition_id": edition_id}
    open_read = next((x for x in reads if not x.get("finished_at")), None)
    if open_read:
        obj["started_at"] = open_read.get("started_at") or obj["started_at"]
        b.q("mutation R($id: Int!, $o: DatesReadInput!) { update_user_book_read(id: $id, object: $o) { error id } }",
            {"id": open_read["id"], "o": obj}, token)
    else:
        b.q("mutation N($u: Int!, $o: DatesReadInput!) { insert_user_book_read(user_book_id: $u, user_book_read: $o) { error id } }",
            {"u": ub_id, "o": obj}, token)
    return "finished" if finished else f"{obj['progress_seconds'] // 60} min"


def sync_owner(owner, token, b, abs_user_id):
    sent = 0
    for p in sorted(absapi.progress(abs_user_id), key=lambda x: -(x.get("lastUpdate") or 0)):
        item = p.get("libraryItemId")
        if not item or not ((p.get("currentTime") or 0) > 0 or p.get("isFinished")):
            continue
        row = db.hc_audio_get(owner, item) or {}
        moved = abs((p.get("currentTime") or 0) - (row.get("sent_seconds") or 0)) >= MIN_STEP
        if row and not moved and bool(row.get("sent_finished")) == bool(p.get("isFinished")):
            continue
        if row.get("matched") == "":
            continue                             # looked for once and not on Hardcover
        if not row.get("book_id"):
            try:
                meta = absapi.item_meta(item)
            except absapi.AbsError:
                continue                         # one unreadable item never stops the reader's sync
            book_id, edition_id, how = match(b, meta, token)
            if not book_id:
                db.hc_audio_put(owner, item, matched="", last_update=p.get("lastUpdate"))
                continue
            db.hc_audio_put(owner, item, book_id=book_id, edition_id=edition_id, matched=how)
            row = db.hc_audio_get(owner, item)
        _write(b, token, row["book_id"], row.get("edition_id"), p)
        db.hc_audio_put(owner, item, sent_seconds=int(p.get("currentTime") or 0),
                        sent_finished=1 if p.get("isFinished") else 0, last_update=p.get("lastUpdate"))
        sent += 1
    return sent


def sync_once():
    """Every reader with a Hardcover token and an Audiobookshelf account. Returns books sent."""
    if not absapi.configured():
        return 0
    try:
        tokens = cwa.hardcover_tokens()
    except cwa.CwaError:
        return 0
    if not tokens:
        return 0
    users = {(u.get("username") or "").lower(): u.get("id") for u in absapi.list_users()}
    b, total = _Budget(PER_RUN), 0
    for owner, token in sorted(tokens.items()):
        uid = users.get(owner.lower())
        if not uid:
            continue
        try:
            n = sync_owner(owner, token, b, uid)
            total += n
            if n:
                db.hc_audio_note(owner, f"{n} audiobook{'s' if n != 1 else ''} updated {time.strftime('%Y-%m-%d %H:%M')}")
        except StopIteration:
            break                                # this run's share of Hardcover's limit is spent
        except hardcover.TokenRefused as e:
            db.hc_audio_note(owner, str(e))
        except (hardcover.HardcoverError, absapi.AbsError) as e:
            log.warning("Hardcover audiobooks for %s: %s", owner, e)
            db.hc_audio_note(owner, f"last try: {str(e)[:200]}")
    return total
