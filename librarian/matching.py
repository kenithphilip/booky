"""Is this copy the book that was chosen? One scorer for every place that has to decide: the
work page's copies, keep-looking rechecks and (later) import identification.

Modelled on Readarr's identification distance (MediaFiles/BookImport/Identification: Distance.cs,
DistanceCalculator.cs, CloseAlbumMatchSpecification.cs) — read, not guessed:
  * every term is a penalty in [0, 1] times a weight; the distance is sum(penalty*weight) /
    sum(weight) over the terms that could be scored, so 0.0 is a perfect match;
  * weights: identifier 10 (ISBN / a catalogue cross-link), language 5, wrong format 5,
    author 3, title 3; a MISSING identifier on one side is only 0.1 — weak, never confidence;
  * accept automatically at distance <= 0.20; anything else goes to a person, with the names
    of the penalties as the reasons.
Two terms Readarr did not need and a family library does: an abridged / adapted / excerpted
edition, and an omnibus, each weighted like a wrong format — an abridged audiobook is not the
book that was asked for.

A few things are refused outright whatever the arithmetic says, because no distance can make
them right: another language when both sides state one, the wrong kind (audio vs ebook), and a
known author that disagrees completely.
"""
import re
import unicodedata

AUTO = 0.20          # Readarr's CloseAlbumMatchSpecification threshold
REVIEW = 0.40        # above this a copy is not even offered for a person to check

W = {"link": 10.0, "isbn": 10.0, "isbn_missing": 0.1, "language": 5.0, "wrong_format": 5.0,
     "abridged": 5.0, "omnibus": 5.0, "author": 3.0, "title": 3.0}

_ARTICLES = {"a", "an", "the"}
_NOISE = {"and", "or", "of"}
_SUB = re.compile(r"\s*(?::|;|\s[-–—]\s|\(|\[).*$")
_NONWORD = re.compile(r"[^a-z0-9]+")
_ABRIDGED = re.compile(r"\b(abridged|condensed|adapted|adaptation|retold|retelling|excerpts?|"
                       r"selections?|selected|simplified|graded reader|for children|junior edition|"
                       r"dramati[sz]ed|dramati[sz]ation|summary|study guide|sparknotes|cliffsnotes)\b")
_UNABRIDGED = re.compile(r"\bunabridged\b")
_OMNIBUS = re.compile(r"\b(complete works|collected works|omnibus|box(ed)? set|trilogy|"
                      r"collection|anthology|works of)\b")

# ISO 639-1 / 639-2 / English names -> one code, for the languages the catalogues actually use
_LANG = {"en": "en", "eng": "en", "english": "en", "fr": "fr", "fre": "fr", "fra": "fr", "french": "fr",
         "de": "de", "ger": "de", "deu": "de", "german": "de", "es": "es", "spa": "es", "spanish": "es",
         "it": "it", "ita": "it", "italian": "it", "pt": "pt", "por": "pt", "portuguese": "pt",
         "nl": "nl", "dut": "nl", "nld": "nl", "dutch": "nl", "fi": "fi", "fin": "fi", "finnish": "fi",
         "sv": "sv", "swe": "sv", "swedish": "sv", "ru": "ru", "rus": "ru", "russian": "ru",
         "zh": "zh", "chi": "zh", "zho": "zh", "chinese": "zh", "ja": "ja", "jpn": "ja", "japanese": "ja",
         "la": "la", "lat": "la", "latin": "la", "el": "el", "gre": "el", "ell": "el", "greek": "el",
         "pl": "pl", "pol": "pl", "polish": "pl", "hi": "hi", "hin": "hi", "hindi": "hi",
         "ml": "ml", "mal": "ml", "malayalam": "ml", "ta": "ta", "tam": "ta", "tamil": "ta"}


def lang(code):
    """One comparable code for 'en', 'eng', 'English', 'en-GB' — or None when unknown."""
    if not code:
        return None
    c = str(code).strip().lower().split("-")[0].split("_")[0]
    return _LANG.get(c, c if len(c) in (2, 3) and c.isalpha() else None)


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def clean(s):
    """Readarr's CleanAuthorName/CleanTitle: accents off, lowercase, non-leading articles and
    'and/or/of' dropped, punctuation gone."""
    words = [w for w in _NONWORD.split(fold(s)) if w]
    out = []
    for i, w in enumerate(words):
        if (w in _ARTICLES and i > 0) or w in _NOISE:
            continue
        if w in _ARTICLES and i == 0 and len(words) > 1:
            continue
        out.append(w)
    return " ".join(out)


def title_variants(t):
    """As Readarr's BookService candidates: as given, subtitle removed, bracket removed."""
    t = t or ""
    out = {clean(t), clean(_SUB.sub("", fold(t)))}
    return {v for v in out if v}


def lev(a, b):
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a, b):
    """Readarr's FuzzyMatch: the better of whole-string Levenshtein and a token score."""
    if not a or not b:
        return 0.0
    whole = 1 - lev(a, b) / max(len(a), len(b))
    ta, tb = a.split(), b.split()
    best = [max((1 - lev(x, y) / max(len(x), len(y)) for y in tb), default=0) for x in ta]
    token = sum(best) / max(len(ta), len(tb))
    return max(whole, token)


def _names(author):
    """'Austen, Jane' / 'Jane Austen' / 'A & B; C' -> set of cleaned 'first last' names."""
    if isinstance(author, (list, tuple)):
        parts = [a.get("name") if isinstance(a, dict) else str(a) for a in author]
    else:
        parts = re.split(r"\s*(?:;|/|&|\band\b|\s·\s)\s*", fold(author or ""))
    out = set()
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if "," in p:
            last, _, first = p.partition(",")
            first = re.sub(r"\(.*?\)|\d{3,4}\s*-\s*\d{0,4}", "", first)   # 'Austen, Jane, 1775-1817'
            p = f"{first.strip()} {last.strip()}"
        c = clean(p)
        if c:
            out.add(c)
    return out


def author_similarity(want, got):
    wa, ga = _names(want), _names(got)
    if not wa or not ga:
        return None
    best = 0.0
    for w in wa:
        for g in ga:
            s = similarity(w, g)
            # surnames agreeing is most of an author match ('j austen' vs 'jane austen')
            if w.split()[-1] == g.split()[-1]:
                s = max(s, 0.85)
            best = max(best, s)
    return best


def _isbns(ids):
    out = set()
    for i in ids or ():
        kind, val = ((i.get("kind"), i.get("value")) if isinstance(i, dict)
                     else (i[0], i[1]) if len(i) == 2 else ("", ""))
        digits = "".join(ch for ch in str(val) if ch.isdigit() or ch in "Xx").upper()
        if str(kind).lower().startswith("isbn") and len(digits) in (10, 13):
            out.add(digits)
    return out


def distance(want, cand):
    """(distance, verdict, reasons). verdict: 'auto' | 'review' | 'reject'.

    want: {title, author, kind, language?, isbns?, work_key?}
    cand: {title, author, kind|source, language?, src_ids?, linked?}"""
    reasons, terms = [], []
    wk = want.get("kind") or "ebook"
    ck = cand.get("kind") or ("audio" if cand.get("source") == "librivox" else "ebook")
    if wk != ck:
        return 1.0, "reject", [f"it is an {'audiobook' if ck == 'audio' else 'ebook'}, not an {'audiobook' if wk == 'audio' else 'ebook'}"]
    wl, cl = lang(want.get("language")), lang(cand.get("language"))
    if wl and cl and wl != cl:
        return 1.0, "reject", [f"it is in another language ({cl}, not {wl})"]
    a = author_similarity(want.get("author"), cand.get("author"))
    if a is not None and a < 0.5:
        return 1.0, "reject", ["it is by a different author"]

    # identifier evidence: a catalogue cross-link from the work's own record, or a shared ISBN
    if cand.get("linked"):
        terms.append(("link", 0.0)); reasons.append("the catalogue record links it to this book")
    wi, ci = _isbns(want.get("isbns")), _isbns(cand.get("src_ids"))
    if wi and ci:
        hit = bool(wi & ci)
        terms.append(("isbn", 0.0 if hit else 1.0))
        reasons.append("same ISBN" if hit else "a different ISBN")
    elif (wi or ci) and not cand.get("linked"):
        terms.append(("isbn_missing", 1.0))

    tv, cv = title_variants(want.get("title")), title_variants(cand.get("title"))
    t = max((similarity(x, y) for x in tv for y in cv), default=0.0)
    terms.append(("title", 1 - t))
    reasons.append("title matches" if t >= 0.95 else f"title {int(t * 100)}% similar")
    if a is None:
        terms.append(("author", 0.5))
        reasons.append("no author to compare")
    else:
        terms.append(("author", 1 - a))
        reasons.append("author matches" if a >= 0.85 else f"author {int(a * 100)}% similar")
    if wl and cl:
        terms.append(("language", 0.0))

    # Edition flags do not move the distance — the copy may be exactly the right text of the
    # wrong edition — they cap the verdict: shown to a person, labelled, never taken on its own.
    flags = edition_flags(cand.get("title"), want.get("title"), want.get("abridged_ok"))
    reasons += flags

    num = sum(p * W[k] for k, p in terms)
    den = sum(W[k] for k, _ in terms)
    d = round(num / den, 3) if den else 1.0
    verdict = "auto" if d <= AUTO else "review" if d <= REVIEW else "reject"
    # The title decides what book this is. Below 0.7 it is another book ('Pride and Prejudice
    # and Zombies' scores 0.66 against 'Pride and Prejudice'), whatever an identifier says;
    # below 0.8 it may be, and a person looks.
    if t < 0.7:
        if cand.get("linked") and wl and cl and wl == cl:
            # the catalogue ties it to this work, in the reader's own language, under another
            # title: a translation. Shown for a person to confirm, never taken on its own.
            verdict = "review"
            reasons.append("listed under another title (a translation?)")
        else:
            verdict = "reject"
            reasons.append("the title is a different book's")
    elif (t < 0.8 or flags) and verdict == "auto":
        verdict = "review"
    return d, verdict, reasons


def edition_flags(cand_title, want_title="", abridged_ok=False):
    """Reasons this copy is a different EDITION of the book: abridged/adapted, or an omnibus."""
    c, w = fold(cand_title), fold(want_title)
    out = []
    if _ABRIDGED.search(c) and not _UNABRIDGED.search(c) and not _ABRIDGED.search(w) and not abridged_ok:
        out.append("looks abridged or adapted")
    if _OMNIBUS.search(c) and not _OMNIBUS.search(w):
        out.append("looks like a collection, not the single book")
    return out
