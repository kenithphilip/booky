"""'Keep looking': a request for a book no catalog has yet. The portal and Shelfmark search when
they are asked, once; a reader whose book was not there had nothing to do but come back and try
again. A wanted entry is that retry, done by the worker on a widening schedule (the Readarr idea,
scaled to public catalogs that change slowly).

This module is the pure part — scoring a search result against what was wanted, and the
schedule — so every rule here is tested without a network or a database.

Matching rules (spec v3, and the lesson of Readarr scoring an absent identifier 0.1 and a wrong
one 10): evidence decides, absence of evidence never counts as agreement.
  * the kind must agree (an audiobook is not the ebook that was wanted);
  * the normalised title must be EQUAL (dedupe.norm_title: no subtitle, article or punctuation);
    'contains' is not enough — 'Emma' is contained in far too many titles;
  * an ISBN the wanted entry carries (from the metadata chain) that the result also carries is
    the strongest evidence there is;
  * a known author that DISAGREES rules the result out;
  * a missing author on either side can make a result a CANDIDATE for the reader to confirm,
    never an automatic request.
"""
import random

import dedupe

AUTO = 0.85          # at or above: requested automatically; below: shown to the reader to confirm
FLOOR = 0.5          # below this a result is not even offered as a candidate

# first check within the hour, then 6 h, then daily. Public-domain catalogs publish in batches;
# checking more often than daily only costs their bandwidth and our two cores.
SCHEDULE = (3600, 6 * 3600, 24 * 3600)
JITTER = 0.1

# when two results score the same: the better-produced edition first
SOURCE_RANK = {"standard_ebooks": 0, "gutenberg": 1, "librivox": 1, "internet_archive": 2, "mycatalog": 3}


def result_kind(r):
    return "audio" if r.get("source") == "librivox" else "ebook"


def _isbns(ids):
    out = set()
    for i in ids or ():
        if isinstance(i, dict):
            kind, val = (i.get("kind") or "").lower(), str(i.get("value") or "")
        else:                                   # fetchers' src_ids are (kind, value) pairs
            kind, val = (str(i[0]).lower(), str(i[1])) if len(i) == 2 else ("", "")
        digits = "".join(ch for ch in val if ch.isdigit() or ch in "Xx").upper()
        if kind.startswith("isbn") and len(digits) in (10, 13):
            out.add(digits)
    return out


def score(want, result):
    """(confidence, reasons) for one search result, or None when it is not this book."""
    if result_kind(result) != (want.get("kind") or "ebook"):
        return None
    wt, rt = dedupe.norm_title(want.get("title")), dedupe.norm_title(result.get("title"))
    if not wt or wt != rt:
        return None
    reasons = [f"title '{wt}' matches"]
    if _isbns(want.get("identifiers")) & _isbns(result.get("src_ids")):
        return 1.0, reasons + ["same ISBN"]
    wa, ra = dedupe.author_tokens(want.get("author")), dedupe.author_tokens(result.get("author"))
    if wa and ra:
        if not wa & ra:
            return None                           # a known author that disagrees is a no
        return 0.9, reasons + [f"author '{' '.join(sorted(wa & ra))}' matches"]
    if wa:
        return 0.6, reasons + ["the catalog gives no author to compare"]
    return FLOOR, reasons + ["no author was given, so this needs your confirmation"]


def best(want, results, rejected=()):
    """The strongest acceptable result: (result, confidence, reasons) or None. Results the
    reader already turned down are skipped."""
    rejected = set(rejected or ())
    scored = []
    for r in results or ():
        if r.get("download_url") in rejected:
            continue
        s = score(want, r)
        if s and s[0] >= FLOOR:
            scored.append((s[0], -SOURCE_RANK.get(r.get("source"), 9), r, s[1]))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    conf, _, r, reasons = scored[0]
    return r, conf, reasons


def next_delay(checks, rnd=random.random):
    """Seconds until the next look, after `checks` looks so far."""
    base = SCHEDULE[min(checks, len(SCHEDULE) - 1)]
    return base * (1 + JITTER * (2 * rnd() - 1))


def query(want):
    """What is typed into the catalogs: title and author, as a reader would."""
    return " ".join(x for x in (want.get("title"), want.get("author")) if x).strip()


def same_want(a, b):
    """Two entries for the same book (title equal after normalising, authors not disagreeing)."""
    if (a.get("kind") or "ebook") != (b.get("kind") or "ebook"):
        return False
    if dedupe.norm_title(a.get("title")) != dedupe.norm_title(b.get("title")):
        return False
    wa, wb = dedupe.author_tokens(a.get("author")), dedupe.author_tokens(b.get("author"))
    return not (wa and wb and not wa & wb)
