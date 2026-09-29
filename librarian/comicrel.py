"""Which release is the comic a reader asked for (docs/COMICS.md, "Classification").

A release title says a lot, inconsistently: "One Piece v05 (2023) (Digital) (1r0n)",
"Saga 012 (2013) (Digital) (Zone-Empire)", "Berserk Vol. 1-10 [Dark Horse]", "Chainsaw Man c150
[Raw]", "Asterix T01 (VF)". parse() reads it into parts; judge() applies the hard rules (the
series, the number, never a chapter release for a volume, the reader's language, a comic format,
a sane size) and scores what passes; pick() returns the best. The request (comic_requests row)
supplies series name and alternative names, kind, number, year and language."""
import math, re, unicodedata

COMIC_FORMATS = ("cbz", "cbr", "cb7", "pdf", "epub", "zip", "rar")
# kinds numbered by VOLUME (a manga tankobon, a Western trade paperback / hardcover / omnibus);
# a plain 'comic' is numbered by issue
PAGE_KINDS = ("manga", "manhwa", "manhua", "collected")

# explicit language markers in a release title (words, or bracketed tags)
LANG_MARKERS = {
    "en": ("english", "eng"),
    "fr": ("french", "francais", "vf", "vostfr"),
    "de": ("german", "deutsch", "ger"),
    "es": ("spanish", "espanol", "castellano", "esp", "latino"),
    "it": ("italian", "italiano", "ita"),
    "pt": ("portuguese", "portugues", "ptbr", "pt-br"),
    "nl": ("dutch", "nederlands"),
    "pl": ("polish", "polski"),
    "ru": ("russian", "rus"),
    "ja": ("japanese", "jpn", "raw", "raws"),
    "zh": ("chinese", "chi"),
    "ko": ("korean", "kor"),
}
OFFICIAL = ("viz", "yen press", "kodansha", "seven seas", "dark horse", "vertical", "square enix",
            "tokyopop", "denpa", "one peace", "udon", "ghost ship", "j-novel", "marvel", "dc comics",
            "image comics", "idw", "boom", "dynamite", "oni press", "titan", "fantagraphics", "drawn & quarterly")
FAN = ("scanlation", "scans", "scanlated", "fan translation")
EDITIONS = {"omnibus": "omnibus", "deluxe": "deluxe", "3-in-1": "omnibus", "2-in-1": "omnibus",
            "collector": "deluxe", "annual": "annual", "tpb": "tpb", "trade paperback": "tpb",
            "hardcover": "hc", "absolute": "deluxe", "box set": "box"}

_VOL = re.compile(r"(?<![a-z0-9])(?:v|vol\.?|volume|volumes|tome|t|band)\s*0*(\d+(?:\.\d+)?)"
                  r"(?:\s*(?:-|–|~|to)\s*(?:v|vol\.?|volume|t)?\s*0*(\d+(?:\.\d+)?))?(?![a-z0-9])", re.I)
# a chapter's decimal is one digit ('61.5'): 'One.Piece.C1072.2023' is chapter 1072 of 2023, not 1072.2023
_CH = re.compile(r"(?<![a-z0-9])(?:c|ch\.?|chap\.?|chapter|chapters)\s*0*(\d+(?:\.\d(?!\d))?)"
                 r"(?:\s*(?:-|–|~|to)\s*(?:c|ch\.?)?\s*0*(\d+(?:\.\d(?!\d))?))?(?![a-z0-9])", re.I)
_HASH = re.compile(r"#\s*0*(\d+(?:\.\d+)?)(?:\s*(?:-|–)\s*#?\s*0*(\d+(?:\.\d+)?))?", re.I)
# a bare issue number after the series: "Saga 012 (2013)", "Batman 001-050"
_BARE = re.compile(r"(?<![\w.#])0*(\d{1,4}(?:\.\d)?)(?:\s*(?:-|–)\s*0*(\d{1,4}))?(?=\s*(?:\(|\[|$|of\b|\.cb|\.pdf|\.epub))", re.I)
_YEAR = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]")
_FORMAT = re.compile(r"\.?\b(cbz|cbr|cb7|pdf|epub|zip|rar)\b", re.I)


def norm(s):
    """lower case, accents and punctuation gone, '&' -> 'and', 'the ' dropped at the start."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"^the\s+", "", s)


def _words(s):
    return set(re.findall(r"[a-z0-9]+", norm(s)))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(title, fmt=None):
    """The parts of a release title. Numbers as floats; ranges as (low, high)."""
    t = title or ""
    low = t.lower()
    brackets = " ".join(re.findall(r"[(\[]([^)\]]*)[)\]]", t)).lower()
    out = {"title": t, "volumes": None, "chapters": None, "issues": None, "year": None, "langs": set(),
           "digital": False, "scan": False, "official": False, "fan": False, "editions": set(),
           "format": (fmt or "").lower().lstrip(".") or None, "prefix": ""}
    first_number_at = len(t)
    m = _VOL.search(t)
    if m:
        a = _num(m.group(1)); b = _num(m.group(2)) or a
        out["volumes"] = (min(a, b), max(a, b))
        first_number_at = min(first_number_at, m.start())
    m = _CH.search(t)
    if m:
        a = _num(m.group(1)); b = _num(m.group(2)) or a
        out["chapters"] = (min(a, b), max(a, b))
        first_number_at = min(first_number_at, m.start())
    m = _HASH.search(t)
    if m:
        a = _num(m.group(1)); b = _num(m.group(2)) or a
        out["issues"] = (min(a, b), max(a, b))
        first_number_at = min(first_number_at, m.start())
    if not out["issues"] and not out["volumes"] and not out["chapters"]:
        head = t.split("(")[0].split("[")[0]
        for m in _BARE.finditer(head):
            a = _num(m.group(1)); b = _num(m.group(2)) or a
            if a is not None and not (1900 <= a <= 2099 and not m.group(2)):
                out["issues"] = (min(a, b), max(a, b))
                first_number_at = min(first_number_at, m.start())
                break
    ym = _YEAR.search(t)
    if ym:
        out["year"] = int(ym.group(1))
    out["prefix"] = norm(re.split(r"[(\[]", t[:first_number_at])[0])
    words = _words(t)
    for code, marks in LANG_MARKERS.items():
        for mk in marks:
            if (" " not in mk and mk in words and (len(mk) > 3 or mk in brackets.split() or mk in _words(brackets))) \
               or (len(mk) > 3 and mk in norm(brackets)):
                out["langs"].add(code)
    out["digital"] = "digital" in words or "webrip" in words
    out["scan"] = "c2c" in words or ("scan" in words and not out["digital"])
    out["official"] = any(p in low for p in OFFICIAL)
    out["fan"] = any(p in low for p in FAN)
    for k, v in EDITIONS.items():
        if k in low:
            out["editions"].add(v)
    if not out["format"]:
        fm = _FORMAT.search(t)
        out["format"] = fm.group(1).lower() if fm else None
    return out


def _series_ok(req, p):
    """The release is this series: its title before the number is the series name (or one of
    its other names), give or take a year or a leading 'the'. 'Batman' is not 'Batman Beyond'."""
    names = [req.get("series_name")] + list(req.get("alt_names") or [])
    pre = re.sub(r"\b(19|20)\d{2}\b", "", p["prefix"]).strip()
    pre = re.sub(r"\s+", " ", pre)
    for n in names:
        nn = norm(n)
        if not nn:
            continue
        if pre == nn:
            return True
        # 'one piece digital colored' / 'saga the deluxe edition': the extra words are edition words
        if pre.startswith(nn + " "):
            extra = set(pre[len(nn):].split())
            if extra <= {"digital", "colored", "coloured", "color", "colour", "full", "comics", "the", "edition", "deluxe",
                         "omnibus", "complete", "collection", "tpb", "hc", "official"}:
                return True
    return False


def judge(req, release):
    """(ok, score, why). Hard rules first; a release that passes them all gets a score."""
    p = parse(release.get("title") or "", release.get("format"))
    kind = req.get("kind") or "comic"
    n = _num(req.get("number"))
    lang = (req.get("language") or "en").lower()
    if not _series_ok(req, p):
        return False, 0, "another series"
    pack = False
    if req.get("unit") == "chapter":             # v5.9: one chapter, never a volume or a chapter pack
        if p["chapters"] is None:
            return False, 0, ("a volume, not the chapter" if p["volumes"] else "no chapter number")
        lo, hi = p["chapters"]
        if n is None or not (lo <= n <= hi):
            return False, 0, "another chapter"
        if hi > lo:
            return False, 0, "a pack of chapters"
    elif kind in PAGE_KINDS:
        if p["volumes"] is None:
            return False, 0, ("chapters, not a volume" if p["chapters"] else "no volume number")
        lo, hi = p["volumes"]
        if n is None or not (lo <= n <= hi):
            return False, 0, "another volume"
        pack = hi > lo
    else:
        if p["volumes"] and not p["issues"]:
            return False, 0, "a collected edition, not the issue"
        if p["issues"] is None:
            return False, 0, "no issue number"
        lo, hi = p["issues"]
        if n is None or not (lo <= n <= hi):
            return False, 0, "another issue"
        pack = hi > lo
        if req.get("year") and p["year"] and p["year"] < int(req["year"]) - 1:
            return False, 0, f"from {p['year']}, before this series began"
    if p["langs"] and lang not in p["langs"]:
        return False, 0, f"in another language ({', '.join(sorted(p['langs']))})"
    if lang != "ja" and "ja" in p["langs"]:
        return False, 0, "untranslated (raw)"
    if p["format"] and p["format"] not in COMIC_FORMATS:
        return False, 0, f"a {p['format']} file"
    size = release.get("size_bytes") or 0
    if size and size < 512 * 1024:
        return False, 0, "too small to be a comic"
    if size and not pack and size > 2 * 1024 ** 3:
        return False, 0, "too large for one issue or volume"
    score = 100.0
    if pack:
        score -= 25 + min(20, (hi - lo))                # the whole run to get one volume
    if p["digital"]:
        score += 12
    if p["official"]:
        score += 8
    if p["fan"]:
        score -= 10
    if p["scan"]:
        score -= 4
    if lang in p["langs"]:
        score += 3
    elif lang != "en" and not p["langs"]:
        score -= 15                                     # unmarked is usually English
    score += {"cbz": 6, "cbr": 4, "cb7": 3, "epub": 2, "zip": 1, "rar": 1, "pdf": -6}.get(p["format"] or "", 0)
    if "annual" in p["editions"] and "annual" not in norm(req.get("number")):
        score -= 30
    proto = (release.get("protocol") or "").lower()
    if proto in ("usenet", "nzb"):
        score += 5                                      # nothing to seed
    seeders = release.get("seeders")
    if proto == "torrent":
        if seeders is not None and seeders <= 0:
            return False, 0, "no seeders"
        score += min(10, math.log2((seeders or 1) + 1) * 2)
    return True, round(score, 1), ("a pack that contains it" if pack else "exact")


def pick(req, releases, exclude=()):
    """The best release (or None) and why each other one was passed over (for the admin)."""
    best, notes = None, []
    for r in releases or []:
        if not isinstance(r, dict) or str(r.get("source_id")) in exclude:
            continue
        ok, score, why = judge(req, r)
        notes.append((r.get("title"), ok, score, why))
        if ok and (best is None or score > best[1]):
            best = (r, score)
    return (best[0] if best else None), notes


def queries(req):
    """What to ask the indexers, most specific first. Two at most: each is a Shelfmark search."""
    name = req.get("series_name") or ""
    n = req.get("number") or ""
    try:
        whole = int(float(n)) == float(n)
        num = int(float(n)) if whole else n
    except ValueError:
        whole, num = False, n
    if req.get("unit") == "chapter":             # 'One Piece Chapter 1148', 'Chainsaw Man Chapter 0190', 'One.Piece.C1072'
        return [f"{name} chapter {num}", name]
    if (req.get("kind") or "comic") in PAGE_KINDS:
        return [f"{name} v{num:02d}" if whole else f"{name} v{num}", name]
    return [f"{name} {num:03d}" if whole else f"{name} {num}", f"{name} #{num}"]


def volume_of(filename):
    """The number a delivered FILE carries (to take the requested volume out of a pack)."""
    p = parse(filename)
    for k in ("volumes", "issues"):
        if p[k] and p[k][0] == p[k][1]:
            return p[k][0]
    return None


def sure(req, release):
    """A pick good enough to download without asking (COMIC_CONFIRM=sure): the exact issue or
    volume (not a pack), digital, not a fan translation, the reader's language (marked, or
    English for an English reader), and Usenet or a torrent with a few seeders."""
    ok, _score, why = judge(req, release)
    if not ok or why != "exact":
        return False
    p = parse(release.get("title") or "", release.get("format"))
    lang = (req.get("language") or "en").lower()
    lang_ok = lang in p["langs"] or (lang == "en" and not p["langs"])
    proto = (release.get("protocol") or "").lower()
    seeded = proto in ("usenet", "nzb") or (proto == "torrent" and (release.get("seeders") or 0) >= 3)
    return p["digital"] and not p["fan"] and lang_ok and seeded


def explain(req, release):
    """Why this copy was chosen, for the reader who confirms it."""
    p = parse(release.get("title") or "", release.get("format"))
    _ok, _score, why = judge(req, release)
    out = ["exactly this " + ("chapter" if req.get("unit") == "chapter" else
                              "volume" if (req.get("kind") or "comic") in PAGE_KINDS else "issue")
           if why == "exact" else "a pack that contains it (only this one is imported)"]
    out.append(f"{p['format'].upper()} file" if p["format"] else "format not stated in the name")
    if p["digital"]:
        out.append("digital")
    elif p["scan"]:
        out.append("a scan")
    if p["official"]:
        out.append("official publisher")
    if p["fan"]:
        out.append("fan translation")
    lang = (req.get("language") or "en").lower()
    out.append(f"language: {', '.join(sorted(p['langs']))}" if p["langs"] else
               ("no language marked (usually English)" if lang == "en" else "no language marked: check it"))
    proto = (release.get("protocol") or "").lower()
    if proto in ("usenet", "nzb"):
        out.append("Usenet (nothing to seed)")
    elif proto == "torrent":
        out.append(f"torrent, {release.get('seeders') or 0} seeders (kept seeding on the seedbox)")
    return out
