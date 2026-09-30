"""v6.2.1: the library's rules, checked in ONE place by the code that makes its states.

The self-check (scripts/selftest.sh, hourly, pushed to Uptime Kuma) used to hold its own copy of
"every book carries an owner tag" in shell SQL; v6.1 added a normal state that breaks it (a book
its last reader removed has no owner for LIBRARY_RELEASE_DAYS before it is deleted), and the copy
never learned it: The Kite Runner failed the self-check every hour. Now the self-check asks this
module (python -m admin_cli invariants), and so does the admin dashboard, so a new state is
classified once, here, with a test:

  problems - something is wrong and a person must act (a FAIL in the self-check);
  notes    - normal states worth seeing, each with where to act (never a FAIL).

Read-only; each part on its own, so one unreadable source never costs the others."""
import json, logging, time
import config, db

log = logging.getLogger("crosscheck")


def _first(titles, n=3):
    return "; ".join(titles[:n]) + ("…" if len(titles) > n else "")


def _library():
    """({book id: title} of books with no owner tag, {owner name: [book ids]})."""
    import library
    c = library._conn()
    try:
        like = config.OWNER_PREFIX + "%"
        untagged = {r[0]: r[1] for r in c.execute(
            "SELECT b.id, b.title FROM books b WHERE NOT EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t "
            "ON t.id=l.tag WHERE l.book=b.id AND t.name LIKE ?) ORDER BY b.id", (like,))}
        owned = {}
        for bid, tag in c.execute("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag "
                                  "WHERE t.name LIKE ?", (like,)):
            owned.setdefault(tag[len(config.OWNER_PREFIX):], []).append(bid)
        titles = dict(c.execute("SELECT id, title FROM books"))
    finally:
        c.close()
    return untagged, owned, titles


def run(now=None):
    """{'problems': [{code, text}], 'notes': [{code, text}]}"""
    now = now or time.time()
    problems, notes = [], []
    try:
        untagged, owned, titles = _library()
    except Exception as e:
        return {"problems": [], "notes": [], "error": f"the library catalogue could not be read ({e})"}

    # books with no owner: a removal counting down, a share being tagged, or a real problem
    releasing = {r["calibre_id"]: r for r in db.releases(("waiting", "due"))}
    with db._conn() as c:
        tagging = {r[0] for r in c.execute("SELECT calibre_id FROM tag_push WHERE status='pending' "
                                           "AND coalesce(op, 'add')='add'")}
    lost = [f"{bid} {untagged[bid]}" for bid in untagged if bid not in releasing and bid not in tagging]
    if lost:
        problems.append({"code": "untagged", "text":
                         f"{len(lost)} book(s) carry NO owner:<user> tag and are therefore invisible to every non-admin — in "
                         f"Calibre-Web, OPDS, Kobo sync and the portal alike — while still using disk. First: {_first(lost)} "
                         f"(Calibre-Web -> edit the book's tags, or the portal's needs-tag queue, to set the owner tag)"})
    rel = [(bid, r) for bid, r in releasing.items() if bid in untagged]
    if rel:
        days = config.LIBRARY_RELEASE_DAYS
        items = [f"{untagged[bid]} (in {max(0, round(days - (now - r['since']) / 86400, 1)):g} days)" for bid, r in rel]
        notes.append({"code": "releasing", "text":
                      f"{len(rel)} book(s) no reader has any more, deleted after {days} days unless someone asks for them: {_first(items)}"})
    shared = [untagged[bid] for bid in untagged if bid in tagging and bid not in releasing]
    if shared:
        notes.append({"code": "being-tagged", "text": f"{len(shared)} book(s) being given to a reader (the host job adds the tag): {_first(shared)}"})

    # owner tags naming accounts that do not exist
    try:
        import cwa
        accounts = {u["name"] for u in cwa.list_users(include_canary=True)}
    except Exception as e:
        accounts = None
        notes.append({"code": "accounts-unread", "text": f"the accounts could not be read, so owner tags were not checked ({e})"})
    if accounts is not None:
        orphans = sorted(o for o in owned if o not in accounts)
        if orphans:
            problems.append({"code": "orphan-owner", "text":
                             f"owner tag(s) naming accounts that no longer exist: {', '.join(orphans)} — every book with one is "
                             f"invisible to everybody (re-create the account, or re-tag those books to a current user)"})

        # books a reader deleted on their Kobo and still has: normal, and they can put them back
        try:
            # deleted on the Kobo by the reader: not the ones the portal took off (finished books, Take it off)
            gone = [(u, bid) for u, bid in cwa.kobo_archived()
                    if bid in owned.get(u, []) and not db.untag_pending(bid, u)
                    and (db.device_book(u, bid, "kobo") or {}).get("status") != "off"]
        except Exception:
            gone = []
        if gone:
            # v6.3: counts per reader, never titles: what someone reads stays theirs, even on this page
            by = {}
            for u, _bid in gone:
                by[u] = by.get(u, 0) + 1
            notes.append({"code": "kobo-deleted", "text":
                          f"{len(gone)} book(s) deleted on a reader's Kobo but still in their library (their page offers "
                          f"'Put it back on my Kobo'): " + ", ".join(f"{u} {n}" for u, n in sorted(by.items()))})

    # audiobooks: the same rules in Audiobookshelf (owner tags on items; v6.1 removal countdown)
    _audiobooks(now, accounts, problems, notes)

    # a removal that has waited over a week for the reader's Kobo to sync
    with db._conn() as c:
        slow = c.execute("SELECT COUNT(*) FROM tag_push WHERE status='pending' AND op='remove' AND kobo_wait IS NOT NULL "
                         "AND created < ?", (now - 7 * 86400,)).fetchone()[0]
    if slow:
        notes.append({"code": "kobo-wait", "text":
                      f"{slow} removal(s) waiting over a week for a reader's Kobo to sync (it goes ahead once the Kobo syncs, "
                      f"or after the wait runs out)"})

    # comics whose Kobo copy was made for other Kobos than their readers' now
    if config.COMICS_ENABLED:
        try:
            import comics
            books = comics.comic_books()
            state = db.comic_convert_state(books.keys())
            stale = [b["title"] for bid, b in books.items()
                     if "KEPUB" in b["formats"] and comics.kobo_copy_status(b, state.get(bid))["stale"]]
        except Exception as e:
            log.debug("crosscheck: comics: %s", e)
            stale = []
        if stale:
            notes.append({"code": "comic-stale", "text":
                          f"{len(stale)} comic Kobo cop(ies) made for other Kobos than their readers' now (Remake Kobo copy on "
                          f"the comic's page): {_first(stale)}"})
    return {"problems": problems, "notes": notes}


ABS_SETTLE = 3600          # an item Audiobookshelf found this last hour may still be getting its tag


def _audiobooks(now, accounts, problems, notes):
    """Audiobookshelf items: no owner tag (lost, a removal counting down, or still being added),
    owner tags naming accounts that do not exist."""
    try:
        import abs as absapi, share
        if not absapi.configured():
            return
        items = share._abs_items()
    except Exception as e:
        notes.append({"code": "abs-unread", "text": f"Audiobookshelf could not be read, so audiobooks were not checked ({e})"})
        return
    releasing = {r["item_id"]: r for r in db.audio_releases(("waiting",))}
    lost, counting, settling, orphans = [], [], [], set()
    days = config.LIBRARY_RELEASE_DAYS
    for it in items:
        media = it.get("media") or {}
        title = (media.get("metadata") or {}).get("title") or it.get("relPath") or it.get("id")
        owners = [t[len(config.OWNER_PREFIX):] for t in media.get("tags") or [] if t.startswith(config.OWNER_PREFIX)]
        if accounts is not None:
            orphans |= {o for o in owners if o not in accounts}
        if owners:
            continue
        r = releasing.get(it.get("id"))
        if r:
            counting.append(f"{title} (in {max(0, round(days - (now - r['since']) / 86400, 1)):g} days)")
        elif now - (it.get("addedAt") or 0) / 1000 < ABS_SETTLE:
            settling.append(title)
        else:
            lost.append(title)
    if lost:
        problems.append({"code": "abs-untagged", "text":
                         f"{len(lost)} audiobook(s) carry NO owner:<user> tag: no reader can see them in Audiobookshelf or the "
                         f"portal. First: {_first(lost)} (Audiobookshelf -> the item -> Edit -> Tags: add owner:<name>)"})
    if orphans:
        problems.append({"code": "abs-orphan-owner", "text":
                         f"audiobook owner tag(s) naming accounts that no longer exist: {', '.join(sorted(orphans))}"})
    if counting:
        notes.append({"code": "abs-releasing", "text":
                      f"{len(counting)} audiobook(s) no reader has any more, deleted after {days} days unless someone asks for them: "
                      f"{_first(counting)}"})
    if settling:
        notes.append({"code": "abs-settling", "text": f"{len(settling)} audiobook(s) just added, getting their owner tag: {_first(settling)}"})


def as_json(now=None):
    return json.dumps(run(now))
