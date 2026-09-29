"""Which release is the AUDIOBOOK a reader asked for (v6.0, Get it for audiobooks; bookreq.py).

The book rules (bookrel.py: the title as a phrase with nothing left over, the author's surname,
one book: no packs, box sets, abridged copies, summaries or study guides), with audio formats:
M4B first (one file, chapters), then M4A, MP3, FLAC/OGG/Opus; an ebook file is refused. Audiobook
names carry extra words that say nothing about WHICH book: "Unabridged", "Audiobook", a bitrate,
"narrated by Ray Porter" (taken out before the title check, so a narrator is not "another book").
Preferred: M4B, "unabridged" in the name, Usenet (nothing to seed), more seeders."""
import math, re
import bookrel, matching

AUDIO_FORMATS = {"m4b": 14, "m4a": 8, "mp3": 6, "flac": 4, "opus": 3, "ogg": 3, "aac": 2}
EBOOK_FORMATS = ("epub", "pdf", "mobi", "azw3", "azw", "kepub", "cbz", "cbr", "txt", "djvu", "fb2")
_FORMAT = re.compile(r"\b(m4b|m4a|mp3|flac|opus|ogg|aac|epub|pdf|mobi|azw3|cbz|cbr)\b", re.I)
_NARRATOR = re.compile(r"\b(?:narrated|read|performed)\s+by\s+[^\[\](){}]+?(?=\s*(?:[\[({]|\s[-–—]\s|$))", re.I)
_AUDIO_NOISE = re.compile(r"\b(unabridged|audio\s*book|audiobook|audio|narrated|m4b|m4a|mp3|flac|opus|aac|ogg|\d{2,3}\s*kbps|kbps|"
                          r"vbr|cbr|mono|stereo|chaptered|retail)\b", re.I)
MIN_SIZE = 15 * 1024 ** 2


def parse(release):
    title = release.get("title") or ""
    fmt = (release.get("format") or "").lower().lstrip(".") or None
    if not fmt:
        m = _FORMAT.search(title)
        fmt = m.group(1).lower() if m else None
    return {"title": title, "format": fmt, "unabridged": bool(re.search(r"\bunabridged\b", title, re.I)),
            "langs": bookrel.comicrel.parse(title)["langs"]}


def _clean(title):
    return _AUDIO_NOISE.sub(" ", _NARRATOR.sub(" ", title))


def judge(want, release):
    """(ok, score, why) for an audiobook release."""
    p = parse(release)
    raw = p["title"]
    flat = re.sub(r"[._]+", " ", raw)
    want_folded = matching.fold(want.get("title") or "")
    if p["format"] in EBOOK_FORMATS:
        return False, 0, f"an ebook ({p['format']}), not the audiobook"
    flags = matching.edition_flags(flat, want.get("title") or "")
    if flags:
        return False, 0, flags[0]
    if bookrel.PACK.search(flat) and not bookrel.PACK.search(want_folded):
        return False, 0, "a pack of several books"
    side = bookrel.SIDE_BOOKS.search(flat)
    if side and not bookrel.SIDE_BOOKS.search(want_folded):
        return False, 0, f"a {side.group(1).lower()}, not the book"
    left = bookrel.title_left(want, _clean(raw))
    if left is None:
        return False, 0, "another title"
    if left:
        return False, 0, "another book (" + " ".join(left[:4]) + ")"
    sname = bookrel.surname(want.get("author") or "")
    has_author = bool(sname) and sname in bookrel._plain(raw).split()
    if sname and not has_author and len(bookrel._title_words(want.get("title") or "")) < 3:
        return False, 0, "no author, and the title is too short to be sure"
    lang = (want.get("language") or "en").lower()
    rel_lang = (release.get("language") or "").lower()[:2]
    if rel_lang and rel_lang != lang:
        return False, 0, f"in another language ({rel_lang})"
    if p["langs"] and lang not in p["langs"]:
        return False, 0, f"in another language ({', '.join(sorted(p['langs']))})"
    size = release.get("size_bytes") or 0
    if size and size < MIN_SIZE:
        return False, 0, "too small to be an audiobook"
    import config
    if size and size > config.MAX_AUDIO_MB * 1024 ** 2:      # v6.0.1: the import cap, one setting
        return False, 0, "too large for one audiobook"
    proto = (release.get("protocol") or "").lower()
    seeders = release.get("seeders")
    if proto == "torrent" and seeders is not None and seeders <= 0:
        return False, 0, "no seeders"
    score = 100.0 + AUDIO_FORMATS.get(p["format"] or "", -4)
    if not has_author:
        score -= 20
    if p["unabridged"]:
        score += 5
    if lang in p["langs"]:
        score += 2
    elif lang != "en" and not p["langs"] and not rel_lang:
        score -= 15
    if proto in ("usenet", "nzb"):
        score += 5
    if proto == "torrent":
        score += min(10, math.log2((seeders or 1) + 1) * 2)
    return True, round(score, 1), "exact" if has_author else "title only"


def pick(want, releases, exclude=()):
    best, notes = None, []
    for r in releases or []:
        if not isinstance(r, dict) or str(r.get("source_id")) in exclude:
            continue
        ok, score, why = judge(want, r)
        notes.append((r.get("title"), ok, score, why))
        if ok and (best is None or score > best[1]):
            best = (r, score)
    return (best[0] if best else None), notes


def sure(want, release):
    """Downloadable without asking (BOOK_CONFIRM=sure): title and author, M4B, unabridged, the language."""
    ok, _s, why = judge(want, release)
    p = parse(release)
    lang = (want.get("language") or "en").lower()
    lang_ok = lang in p["langs"] or (lang == "en" and not p["langs"] and not release.get("language"))
    return ok and why == "exact" and p["format"] == "m4b" and p["unabridged"] and lang_ok


def explain(want, release):
    p = parse(release)
    _ok, _s, why = judge(want, release)
    out = ["the title and the author match" if why == "exact" else "the title matches (no author in the name)"]
    out.append(f"{p['format'].upper()} audio" if p["format"] else "audio format not stated in the name")
    out.append("unabridged" if p["unabridged"] else "not marked unabridged")
    size = release.get("size_bytes") or 0
    if size:
        out.append(f"{size / 1024 ** 2:.0f} MB")
    proto = (release.get("protocol") or "").lower()
    if proto in ("usenet", "nzb"):
        out.append("Usenet (nothing to seed)")
    elif proto == "torrent":
        out.append(f"torrent, {release.get('seeders') or 0} seeders (kept seeding on the seedbox)")
    return out
