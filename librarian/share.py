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
    return dict(m, owners=owners) if owners else None


def find_audiobook(title, author=""):
    """{"item_id", "how", "owners"} for the one Audiobookshelf item with the same title AND an
    overlapping author (an audiobook's name is all there is to go on), else None."""
    want_t, want_a = dedupe.norm_title(title or ""), dedupe.author_tokens(author or "")
    if not (config.FAMILY_SHARING and want_t and want_a and absapi.configured()):
        return None
    try:
        lib_id, _ = absapi.ensure_library()
        r = absapi._req("GET", f"/api/libraries/{lib_id}/items", params={"limit": 0})
        if r.status_code != 200:
            return None
        hits = []
        for it in absapi._json(r).get("results", []):
            md = (it.get("media") or {}).get("metadata") or {}
            if dedupe.norm_title(md.get("title") or "") == want_t \
                    and want_a & dedupe.author_tokens(md.get("authorName") or ""):
                hits.append(it["id"])
        if len(hits) != 1:
            return None                      # none, or two candidates: never guess between them
        full = absapi._json(absapi._req("GET", f"/api/items/{hits[0]}"))
    except Exception as e:                   # ABS down: download as usual rather than fail
        log.warning("family sharing: Audiobookshelf lookup failed: %s", e)
        return None
    tags = ((full.get("media") or {}).get("tags") or [])
    owners = sorted(t[len(config.OWNER_PREFIX):] for t in tags if t.startswith(config.OWNER_PREFIX))
    return {"item_id": hits[0], "how": "title+author", "owners": owners} if owners else None


def give_ebook(match, owner, rid=None, now=None):
    """Queue the host job that adds owner:<owner> to the existing Calibre book."""
    if owner not in match["owners"]:
        db.queue_tag_push(match["book_id"], rid, owner, now, share=True)   # False: already queued
    if rid:
        db.link_calibre(rid, match["book_id"], owner)


def give_audiobook(match, owner):
    """Tag the existing Audiobookshelf item for this reader, now."""
    if owner not in match["owners"]:
        absapi.tag_item(match["item_id"], absapi.owner_tag(owner))
