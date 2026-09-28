"""Read-only check: is this book already in the part of the Calibre library this user can see?
Advisory (the 'in library' badge on search results) — it never blocks a request.

It used to be `lower(b.title) = lower(?)`, one full query per search result, and nothing else.
That failed in both directions:
  * 'The Hobbit' vs 'The Hobbit: 75th Anniversary Edition' never matched, so a family member was
    told they did not have a book they did;
  * two unrelated books sharing a title always matched, so they were told they had a book they
    did not.

Matching now follows the spec's precedence, strongest first:
  1. an identifier from Calibre's own `identifiers` table (ISBN) or the book's Calibre UUID —
     CWA fills that table from each file's dc:identifier, and no portal module had ever read it;
  2. normalised title AND an overlapping author name;
  3. normalised title alone — only when one side has no author at all to compare.
A known author that DISAGREES is a definite 'not a duplicate', never a fall-through to rung 3:
that fall-through is exactly how absent evidence gets read as confidence.

Identifiers are a DEDUPE and VERIFICATION key here, never a query key.
"""
import re
import sqlite3
import unicodedata

import config
import library

_ARTICLES = ("the ", "a ", "an ")
_SUBTITLE = re.compile(r"\s*(?::|\s-\s|\s–\s|\s—\s|\(|\[).*$")
_NONWORD = re.compile(r"[^a-z0-9]+")
# initials and connective noise that would make every 'J. Smith' agree with every 'J. Jones'
_AUTHOR_NOISE = {"jr", "sr", "ii", "iii", "iv", "de", "la", "le", "van", "von", "der", "den",
                 "del", "du", "da", "di", "and", "et", "al", "dr", "mr", "mrs", "ms", "sir"}


def _fold(s):
    """Lowercase, accents removed ('Brontë' == 'Bronte'), everything else left for the callers."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def norm_title(title):
    """The comparable core of a title: no subtitle, no edition bracket, no leading article, no
    punctuation. 'The Hobbit: or There and Back Again' and 'Hobbit (75th Anniversary)' both
    become 'hobbit'."""
    t = _SUBTITLE.sub("", _fold(title))
    for art in _ARTICLES:
        if t.startswith(art):
            t = t[len(art):]
            break
    return _NONWORD.sub(" ", t).strip()


def author_tokens(author):
    """Name words long enough to mean something. Order-free on purpose: Gutenberg says
    'Austen, Jane', Calibre says 'Jane Austen', and both must agree."""
    words = _NONWORD.split(_fold(author))
    return {w for w in words if len(w) > 1 and w not in _AUTHOR_NOISE}


def _ident_keys(identifiers):
    """(calibre_type, value) pairs to look up. Calibre stores ISBN-10 and ISBN-13 alike under
    type 'isbn'; the book's own UUID lives in books.uuid, looked up as ('uuid', value)."""
    out = []
    for i in identifiers or ():
        kind = (i.get("kind") or "").lower()
        val = str(i.get("value") or "").strip()
        if not val:
            continue
        if kind in ("isbn", "isbn13", "isbn10"):
            digits = "".join(ch for ch in val if ch.isdigit() or ch in "Xx").upper()
            if len(digits) in (10, 13):
                out.append(("isbn", digits))
        elif kind in ("uuid", "calibre_uuid"):
            out.append(("uuid", val.lower()))
    return out


class Index:
    """What ONE reader can see, loaded once per page instead of once per search result.

    Built from a single scoped read of metadata.db, so a family member is never told they have
    a book that only someone else owns (F53), and a 20-result search page costs one query, not
    twenty."""

    def __init__(self, owner=None, is_admin=False, force=False):
        self.by_title = {}          # norm_title -> [(book_id, author_token_set)]
        self.by_ident = {}          # (type, value) -> book_id
        self.stale = None           # set when metadata.db had to be read in immutable mode
        if not (config.DEDUPE_WARN or force):   # force: family sharing reads it regardless
            return
        scope, params = library._scope_sql(owner, is_admin)
        try:
            c = library._conn()
        except sqlite3.Error:
            return
        self.stale = library.STALE_READ[0]
        try:
            rows = c.execute(
                "SELECT b.id, b.title, b.uuid, "
                "(SELECT group_concat(a.name, '|') FROM books_authors_link al "
                " JOIN authors a ON a.id = al.author WHERE al.book = b.id) "
                f"FROM books b WHERE 1=1 {scope}", params).fetchall()
            for bid, title, uuid, authors in rows:
                toks = set()
                for name in (authors or "").split("|"):
                    toks |= author_tokens(name)
                self.by_title.setdefault(norm_title(title), []).append((bid, toks))
                if uuid:
                    self.by_ident[("uuid", str(uuid).lower())] = bid
            if rows:
                ids = tuple(r[0] for r in rows)
                # chunked: SQLite's host-parameter limit is 999 on older builds
                for n in range(0, len(ids), 900):
                    part = ids[n:n + 900]
                    marks = ",".join("?" * len(part))
                    for bid, typ, val in c.execute(
                            f"SELECT book, type, val FROM identifiers WHERE book IN ({marks})", part):
                        t = (typ or "").lower()
                        v = str(val or "").strip()
                        if t == "isbn":
                            v = "".join(ch for ch in v if ch.isdigit() or ch in "Xx").upper()
                        self.by_ident[(t, v)] = bid
        except sqlite3.Error:
            pass
        finally:
            c.close()

    def match(self, title, author=None, identifiers=()):
        """None, or {"how": "isbn"|"uuid"|"title+author"|"title", "book_id": n}."""
        for key in _ident_keys(identifiers):
            bid = self.by_ident.get(key)
            if bid:
                return {"how": key[0], "book_id": bid}
        cands = self.by_title.get(norm_title(title))
        if not cands:
            return None
        want = author_tokens(author)
        if not want:
            return {"how": "title", "book_id": cands[0][0]}
        for bid, have in cands:
            if have and want & have:
                return {"how": "title+author", "book_id": bid}
        for bid, have in cands:
            if not have:                 # the library copy has no author to disagree with
                return {"how": "title", "book_id": bid}
        return None                      # same title, a DIFFERENT author: not this book


def exists(title, owner=None, is_admin=False, author=None, identifiers=()):
    """One-off form of Index(...).match(...), for callers that check a single title."""
    if not (config.DEDUPE_WARN and title):
        return False
    return bool(Index(owner, is_admin).match(title, author, identifiers))
