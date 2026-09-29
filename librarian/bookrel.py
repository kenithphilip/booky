"""Which release is the book a reader asked for (one-tap book requests, v5.8.3; bookreq.py).

The comic rules (comicrel.py) read a series and a number; a book has neither, only a title and
an author, and its releases are spelled every way: "Terry Pratchett - Guards! Guards! (1989)
[EPUB]", "Wind and Truth by Brandon Sanderson epub", "Brandon.Sanderson.-.Wind.and.Truth.2024.
RETAIL.EPUB.eBook-DiVU". judge() applies the hard rules and scores what passes; pick() returns
the best. The rules lean towards NOT picking: a reader who gets nothing is offered "Pick in
Shelfmark"; a reader who gets the wrong book has a wrong book on their Kobo.

Hard rules:
  * the title is there as a phrase, and nothing is left over that could make it ANOTHER book
    ('Dune' is not 'Dune Messiah'), once the author, the year, the format and quality words and
    anything in brackets are taken out; a subtitle after ':' is allowed;
  * the author's surname is there (for short titles: 'Emma' needs 'Austen'); a long title may
    go without it, at a cost;
  * a single book: no collections, box sets, 'books 1-5', abridged, summaries or study guides
    (matching.edition_flags), unless the wanted title says so itself;
  * an ebook format (EPUB, AZW3, MOBI; no PDF: it does not reflow on a Kobo or a Kindle), the
    reader's language, a sane size, and a torrent somebody seeds.
Preferred: EPUB, a retail copy, Usenet (nothing to seed), more seeders."""
import math, re

import comicrel, dedupe, matching

FORMATS = {"epub": 12, "azw3": 7, "kepub": 10, "mobi": 4, "azw": 3}
REFUSED_FORMATS = ("pdf", "m4b", "mp3", "flac", "m4a", "aac", "ogg", "cbz", "cbr", "cb7", "djvu", "txt", "doc", "docx", "rtf", "zip", "rar")
AUDIO_WORDS = ("audiobook", "unabridged", "narrated", "m4b", "mp3", "audio")
# words a release title carries that say nothing about WHICH book it is
NOISE = {"epub", "mobi", "azw3", "azw", "kepub", "ebook", "ebk", "e", "book", "retail", "true", "v5", "repack",
         "converted", "conv", "scan", "ocr", "html", "by", "a", "an", "the", "and", "of", "novel", "edition",
         "ed", "english", "eng", "en", "us", "uk", "illustrated", "annotated", "hq", "hardcover", "paperback",
         "kindle", "calibre", "fixed", "proper", "series", "vol", "volume", "part", "bk", "no", "nr"}
PACK = re.compile(r"\b(books?|novels?|vols?|volumes?)\s*\d+\s*(?:-|–|to|&|and)\s*\d+\b|\b\d+\s*(?:books|novels)\b|"
                  r"\bcomplete\s+(?:series|saga|set|collection|novels)\b|\bbundle\b", re.I)
SIDE_BOOKS = re.compile(r"\b(workbook|companion|guide to|analysis|summary of|cliff'?s ?notes|coloring book|"
                        r"cookbook|journal|screenplay|graphic novel|manga|sampler|preview|excerpt|chapter \d+)\b", re.I)
_SEP = re.compile(r"\s*:\s*|\s+[-–—]\s+")
_SCENE_TAIL = re.compile(r"(?<=[a-z0-9])-[a-z0-9]{2,12}$", re.I)       # 'eBook-DiVU'
_BRACKETS = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_FORMAT = re.compile(r"\b(epub|kepub|azw3|azw|mobi|pdf|m4b|mp3|flac|cbz|cbr|djvu|txt|docx?|rtf)\b", re.I)
MAX_SIZE = 150 * 1024 ** 2
MIN_SIZE = 20 * 1024


def _plain(s):
    """Dots and underscores as spaces (scene names), apostrophes dropped, folded, punctuation gone."""
    s = re.sub(r"['’]", "", re.sub(r"[._]+", " ", s or ""))
    return comicrel.norm(s)


def _title_words(title):
    """The wanted title without its subtitle and leading article (dedupe.norm_title), as words."""
    return dedupe.norm_title(re.sub(r"['’]", "", title or "")).split()


def surname(author):
    toks = [w for w in re.split(r"[\s,]+", comicrel.norm(author or "")) if len(w) > 1]
    if not toks:
        return ""
    return toks[0] if "," in (author or "") else toks[-1]


def _phrase_at(words, phrase):
    n = len(phrase)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == phrase:
            return i
    return -1


def parse(release):
    title = release.get("title") or ""
    fmt = (release.get("format") or "").lower().lstrip(".") or None
    if not fmt:
        m = _FORMAT.search(title)
        fmt = m.group(1).lower() if m else None
    return {"title": title, "format": fmt, "langs": comicrel.parse(title)["langs"],
            "retail": bool(re.search(r"\bretail\b", title, re.I))}


def title_left(want, text):
    """None when the wanted title is not in `text` as a phrase; else the words left over that
    could make it ANOTHER book ([] when there are none). The title is looked for in one part of
    the name ('Author - Title: Subtitle (Series 3)'); the parts before it may only hold the
    author or the series, and in its own part nothing may be left over. Parts after it are a
    subtitle or the author. Used on release names and on the title inside an arrived file."""
    wt = _title_words(want.get("title") or "")
    if not wt:
        return None
    body = _SCENE_TAIL.sub("", _BRACKETS.sub(" ", text or "").strip())
    parts = [_plain(x).split() for x in _SEP.split(re.sub(r"[._]+", " ", body))]
    allowed = NOISE | dedupe.author_tokens(want.get("author") or "") | set(_plain(want.get("series") or "").split()) \
        | set(_plain(want.get("title") or "").split())

    def extra(ws):
        return [w for w in ws if w not in allowed and not w.isdigit() and len(w) > 1]   # 1 letter: initials

    at = next((i for i, ws in enumerate(parts) if _phrase_at(ws, wt) >= 0), -1)
    if at < 0:
        return None
    ws = parts[at]
    i = _phrase_at(ws, wt)
    return extra(ws[:i] + ws[i + len(wt):]) + [w for before in parts[:at] for w in extra(before)]


def judge(want, release):
    """(ok, score, why). want: {title, author, language, series?}."""
    p = parse(release)
    raw = p["title"]
    wt = _title_words(want.get("title") or "")
    if not wt:
        return False, 0, "no title to look for"
    want_folded = matching.fold(want.get("title") or "")
    # the edition, before anything is taken out of the title
    flags = matching.edition_flags(re.sub(r"[._]+", " ", raw), want.get("title") or "")
    if flags:
        return False, 0, flags[0]
    if PACK.search(re.sub(r"[._]+", " ", raw)) and not PACK.search(want_folded):
        return False, 0, "a pack of several books"
    side = SIDE_BOOKS.search(re.sub(r"[._]+", " ", raw))
    if side and not SIDE_BOOKS.search(want_folded):
        return False, 0, f"a {side.group(1).lower()}, not the book"
    if p["format"] in REFUSED_FORMATS:
        return False, 0, f"a {p['format']} file"
    if p["format"] is None and any(w in _plain(raw).split() for w in AUDIO_WORDS):
        return False, 0, "an audiobook"
    left = title_left(want, raw)
    if left is None:
        return False, 0, "another title"
    if left:
        return False, 0, "another book (" + " ".join(left[:4]) + ")"
    sname = surname(want.get("author") or "")
    has_author = bool(sname) and sname in _plain(raw).split()     # '[Frank Herbert]' counts too
    if sname and not has_author:
        if len(wt) < 3:
            return False, 0, "no author, and the title is too short to be sure"
    lang = (want.get("language") or "en").lower()
    rel_lang = (release.get("language") or "").lower()[:2]
    if rel_lang and rel_lang != lang:
        return False, 0, f"in another language ({rel_lang})"
    if p["langs"] and lang not in p["langs"]:
        return False, 0, f"in another language ({', '.join(sorted(p['langs']))})"
    size = release.get("size_bytes") or 0
    if size and size < MIN_SIZE:
        return False, 0, "too small to be a book"
    if size and size > MAX_SIZE:
        return False, 0, "too large for one ebook"
    proto = (release.get("protocol") or "").lower()
    seeders = release.get("seeders")
    if proto == "torrent" and seeders is not None and seeders <= 0:
        return False, 0, "no seeders"
    score = 100.0 + FORMATS.get(p["format"] or "", 0)
    if p["format"] is None:
        score -= 6                                      # probably an ebook (the category says so), not certain
    if not has_author:
        score -= 20
    if p["retail"]:
        score += 5
    if lang in p["langs"]:
        score += 2
    elif lang != "en" and not p["langs"] and not rel_lang:
        score -= 15                                     # unmarked is usually English
    if proto in ("usenet", "nzb"):
        score += 5                                      # nothing to seed
    if proto == "torrent":
        score += min(10, math.log2((seeders or 1) + 1) * 2)
    return True, round(score, 1), "exact" if has_author else "title only"


def pick(want, releases, exclude=()):
    """The best release (or None) and why each other one was passed over."""
    best, notes = None, []
    for r in releases or []:
        if not isinstance(r, dict) or str(r.get("source_id")) in exclude:
            continue
        ok, score, why = judge(want, r)
        notes.append((r.get("title"), ok, score, why))
        if ok and (best is None or score > best[1]):
            best = (r, score)
    return (best[0] if best else None), notes


def queries(want):
    """What to ask the indexers: the title with the surname, then the title alone."""
    title = re.split(r"\s*[:(\[]", want.get("title") or "")[0].strip()
    sname = surname(want.get("author") or "")
    out = [f"{title} {sname}".strip(), title]
    return list(dict.fromkeys(q for q in out if q))


def sure(want, release):
    """A pick good enough to download without asking (BOOK_CONFIRM=sure): the title AND the
    author in the name, an EPUB, a retail copy, and the reader's language (marked, or English
    for an English reader: unmarked releases are nearly always English)."""
    ok, _score, why = judge(want, release)
    if not ok or why != "exact":
        return False
    p = parse(release)
    lang = (want.get("language") or "en").lower()
    rel_lang = (release.get("language") or "").lower()[:2]
    lang_ok = rel_lang == lang or lang in p["langs"] or (lang == "en" and not rel_lang and not p["langs"])
    return p["format"] in ("epub", "kepub") and p["retail"] and lang_ok


def explain(want, release):
    """Why this copy was chosen, for the reader who confirms it."""
    p = parse(release)
    _ok, _score, why = judge(want, release)
    out = ["the title and the author match" if why == "exact" else "the title matches (no author in the name)"]
    out.append(f"{p['format'].upper()} file" if p["format"] else "format not stated in the name")
    if p["retail"]:
        out.append("a retail copy")
    lang = (want.get("language") or "en").lower()
    rel_lang = (release.get("language") or "").lower()[:2]
    if rel_lang or p["langs"]:
        out.append(f"language: {rel_lang or ', '.join(sorted(p['langs']))}")
    else:
        out.append("no language marked (usually English)" if lang == "en" else "no language marked: check it")
    proto = (release.get("protocol") or "").lower()
    if proto in ("usenet", "nzb"):
        out.append("Usenet (nothing to seed)")
    elif proto == "torrent":
        out.append(f"torrent, {release.get('seeders') or 0} seeders (kept seeding on the seedbox)")
    return out


def blocked_key(title):
    """How a release name the reader turned down is remembered (the same name from another indexer too)."""
    return _plain(title)
