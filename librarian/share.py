"""Family sharing: a book that is already in the library is given to the next reader who asks
for it, instead of being downloaded a second time (seedbox traffic, tracker ratio, disk).

"Given" means their owner tag is added to the SAME copy: per-reader isolation stays exactly as
it is (every reader sees only books carrying their own owner tag), a shared book simply carries
two. Ebooks: Calibre's database, written by the host job (tag_push with share=1, see
scripts/metadata-push.sh; the portal mounts the library read-only on purpose). Audiobooks:
Audiobookshelf's API, which the portal already uses to tag every audiobook it places.

Only a STRONG match shares anything: an identifier (ISBN, the Calibre UUID) or the same title
AND an overlapping author. Title alone never does, and a known author that disagrees is a no
(dedupe.Index). Only books that already have an owner are candidates: an untagged book is
someone's import still in progress (worker.reconcile_untagged adopts those).

Callers: the Shelfmark gate (worker.shelfmark_gate_once, before anything is downloaded), arrivals
in a dropbox or from Shelfmark (worker._atomic_ingest, _place_audio_*) and portal requests
(worker._process), each before any bytes are fetched or imported.
"""
import logging
import sqlite3

import abs as absapi
import config
import db
import dedupe
import library

log = logging.getLogger("share")
STRONG = ("isbn", "uuid", "title+author")


def _owners_of_book(book_id):
    c = library._conn()
    try:
        return sorted(t[len(config.OWNER_PREFIX):] for (t,) in c.execute(
            "SELECT t.name FROM tags t JOIN books_tags_link l ON l.tag=t.id WHERE l.book=? AND t.name LIKE ?",
            (book_id, config.OWNER_PREFIX + "%")))
    finally:
        c.close()


def shareable(owners, viewer=None):
    """v6.3: the owners whose copy may be offered to `viewer`: everyone but readers who keep their
    books private (Devices / the start page), and always the viewer themself."""
    try:
        private = db.private_readers()
    except Exception:
        private = set()
    return [o for o in owners or [] if o == viewer or o not in private]


def _released_ok(kind, key):
    """v6.3: a book counting down may be given to another reader only if whoever removed it did
    not keep their books private."""
    try:
        who = db.release_removed_by(kind, key)
        return not who or who not in db.private_readers()
    except Exception:
        return False


def isbn_ids(*values):
    """[{"kind": "isbn", "value": ...}] from loose ISBN strings (Shelfmark's isbn_10 / isbn_13)."""
    return [{"kind": "isbn", "value": str(v)} for v in values if v]


def find_ebook(title, author="", identifiers=()):
    """{"book_id", "how", "owners"} for a strong match anywhere in the library, else None."""
    if not config.FAMILY_SHARING or not (title or identifiers):
        return None
    try:
        m = dedupe.Index(None, is_admin=True, force=True).match(title or "", author or "", identifiers)
        if not m or m["how"] not in STRONG:
            return None
        owners = _owners_of_book(m["book_id"])
    except sqlite3.Error as e:
        log.warning("family sharing: could not read metadata.db: %s", e)
        return None
    if owners:
        # v6.3: only readers who share their books count; a copy only private readers have is not
        # offered (whoever asks downloads their own)
        return dict(m, owners=owners) if shareable(owners) else None
    # v6.1: nobody has it any more, but it is still here, counting down to deletion (removed in the
    # last LIBRARY_RELEASE_DAYS): asked for again, it is given back, never downloaded again. Not an
    # untagged book otherwise: that is someone's import still under way.
    try:
        import db
        if db.release_waiting(m["book_id"]) and _released_ok("book", m["book_id"]):
            return dict(m, owners=[], released=True)
    except Exception as e:
        log.warning("family sharing: could not read the release countdown: %s", e)
    return None


def audiobook_owned_by(title, author, owner, exclude=()):
    """The reader's own audiobook of this title and author ({"item_id", "how", "owners"}), else None;
    looked up with family sharing off too (v6.0: Get it for audiobooks). Only items carrying their
    owner tag count, so another reader's copy of the same title never hides theirs; `exclude`:
    item ids the reader said were the wrong audiobook."""
    return _audiobook_match(title, author, owner=owner, exclude=exclude)


def find_audiobook(title, author="", exclude=()):
    """{"item_id", "how", "owners"} for an Audiobookshelf item with the same title AND an
    overlapping author (an audiobook's name is all there is to go on) that someone owns, else None."""
    if not config.FAMILY_SHARING:
        return None
    return _audiobook_match(title, author, exclude=exclude)


_ABS_ITEMS = {"at": 0.0, "items": None}
ABS_ITEMS_TTL = 60


def _abs_items():
    """The Audiobookshelf library's items (with their tags), read at most once a minute: the
    worker asks for every downloading audiobook request each pass (v6.0)."""
    import time
    now = time.time()
    if _ABS_ITEMS["items"] is not None and now - _ABS_ITEMS["at"] < ABS_ITEMS_TTL:
        return _ABS_ITEMS["items"]
    lib_id, _ = absapi.ensure_library()
    r = absapi._req("GET", f"/api/libraries/{lib_id}/items", params={"limit": 0})
    if r.status_code != 200:
        raise absapi.AbsError(f"Audiobookshelf answered HTTP {r.status_code}")
    _ABS_ITEMS.update(items=absapi._json(r).get("results", []), at=now)
    return _ABS_ITEMS["items"]


def _audiobook_match(title, author="", owner=None, exclude=()):
    want_t, want_a = dedupe.norm_title(title or ""), dedupe.author_tokens(author or "")
    if not (want_t and want_a and absapi.configured()):
        return None
    try:
        items = _abs_items()
    except Exception as e:                   # ABS down: download as usual rather than fail
        log.warning("family sharing: Audiobookshelf lookup failed: %s", e)
        return None
    hits = []
    for it in items:
        media = it.get("media") or {}
        md = media.get("metadata") or {}
        if it.get("id") in exclude:
            continue
        if dedupe.norm_title(md.get("title") or "") == want_t and want_a & dedupe.author_tokens(md.get("authorName") or ""):
            owners = sorted(t[len(config.OWNER_PREFIX):] for t in (media.get("tags") or []) if t.startswith(config.OWNER_PREFIX))
            if owners and not shareable(owners, owner):
                continue                         # v6.3: only private readers have it: not offered
            hits.append({"item_id": it["id"], "how": "title+author", "owners": owners})
    if owner:
        mine = [h for h in hits if owner in h["owners"]]
        return mine[0] if mine else None
    owned = [h for h in hits if h["owners"]]
    if not owned:
        # v6.1: removed by its last reader, still here counting down: given back, never downloaded
        # again. Any other untagged item is someone's import still under way.
        try:
            import db
            back = [h for h in hits if db.audio_release_waiting(h["item_id"]) and _released_ok("audio", h["item_id"])]
        except Exception:
            back = []
        return dict(back[0], released=True) if back else None
    return max(owned, key=lambda h: len(h["owners"]))   # copies of the same book: the most shared one


def give_ebook(match, owner, rid=None, now=None):
    """Queue the host job that adds owner:<owner> to the existing Calibre book."""
    if owner not in match["owners"] or db.untag_pending(match["book_id"], owner):
        # v6.2.1: a book counting down (released) has no owner: its give-back is marked as such, or the
        # host job refused it as "a family share, but the book has no owner yet" (never given back)
        db.queue_tag_push(match["book_id"], rid, owner, now, share=2 if match.get("released") else True)
    try:                                         # v6.1: never 'archived' for them (their Kobo would drop it)
        import cwa
        cwa.kobo_unarchive(owner, match["book_id"])
    except Exception as e:
        log.warning("could not clear the Kobo archive mark of book %s for %s: %s", match["book_id"], owner, e)
    if rid:
        db.link_calibre(rid, match["book_id"], owner)


KOBO_REMOVE_WAIT = 7 * 86400     # a Kobo that never syncs holds a removal at most this long (v6.1)


def remove_ebook(owner, book_id, now=None):
    """v6.2.1, the ONE way a book leaves a reader's library (Remove, Wrong book, Wrong comic, a volume
    replacing chapters): their Kobo is told to delete it first (cwa.kobo_remove: Calibre-Web's
    archive), then their owner tag comes off (host job), held until the Kobo has synced, at most
    KOBO_REMOVE_WAIT. Returns how the Kobo is told ('archive' / 'shelf'), or None (never on it)."""
    import cwa, time
    try:
        kobo = cwa.kobo_remove(owner, book_id)
    except Exception as e:
        log.warning("Kobo removal of book %s for %s: %s", book_id, owner, e)
        kobo = None
    db.queue_untag(book_id, owner, now=now,
                   not_before=((now or time.time()) + KOBO_REMOVE_WAIT) if kobo else None, kobo_wait=kobo)
    return kobo


def give_audiobook(match, owner):
    """Tag the existing Audiobookshelf item for this reader, now."""
    if owner not in match["owners"]:
        absapi.tag_item(match["item_id"], absapi.owner_tag(owner))
        _ABS_ITEMS["at"] = 0.0                   # the next lookup sees the new owner
