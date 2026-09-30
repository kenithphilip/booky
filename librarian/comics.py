"""Comics and manga: requests, the search through Shelfmark, arrivals, device copies (docs/COMICS.md).

Nothing here downloads. A request is matched against the family library first (a copy someone
already has is shared), then the best release is chosen from Shelfmark's own Prowlarr search and
queued in SHELFMARK under the reader's account, so the seedbox, the seeding and the delivery into
the reader's dropbox are exactly those of a Shelfmark book. When the file arrives the dropbox
watcher hands it here: CBR/CB7 become CBZ, the file is matched to the request, its metadata is
written into it (ComicInfo.xml for readers and KCC, ComicBookInfo for Calibre), and it is imported
like a book. The host job scripts/comic-convert.sh adds the Kobo copy (KCC) for readers who have
a Kobo, and makes Kindle copies when one is sent."""
import json, logging, os, re, shutil, sqlite3, struct, subprocess, time, zipfile
from xml.sax.saxutils import escape
import config, db, comicmeta, comicrel, cwa, notify, share

log = logging.getLogger("comics")

COMIC_TAGS = {"comic": "Comics", "collected": "Comics", "manga": "Manga", "manhwa": "Manhwa", "manhua": "Manhua"}
ALL_COMIC_TAGS = ("Comics", "Manga", "Manhwa", "Manhua")
STRIP_TAG = "Long strip"            # webtoon-style vertical pages: KCC's webtoon mode
LANDSCAPE_TAG = "Landscape pages"   # v6.1: wide pages (The Complete Peanuts): rotated on e-readers, never split
RTL_KINDS = ("manga",)
RETRY = (3600, 6 * 3600) + (86400,) * 60
CHAPTER_SEARCH_DAYS = 7              # a chapter nobody posted in a week will not be; the volume will come
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jxl")
MAX_PAGES = 3000


class ComicError(Exception):
    pass


# ---- a reader asks ---------------------------------------------------------------------------------
def request(owner, series, item, language=None, reading=None, now=None):
    """Record one request (or return the open one). series/item as comicmeta returns them.
    Returns (request_id, what): 'shared' (the family had it: nothing downloaded), 'owned',
    'queued' or 'exists'."""
    if series.get("kind") == "novel":
        raise ComicError("this is a novel, not a comic: request it on the Search page")
    if db.comic_count_open(owner) >= config.COMIC_MAX_OPEN_PER_USER and not _is_admin(owner):
        raise ComicError(f"you have {config.COMIC_MAX_OPEN_PER_USER} comic requests open already")
    kind = series.get("kind") or "comic"
    fields = {"provider": series["provider"], "series_id": str(series["id"]), "series_name": series["name"],
              "kind": kind, "reading": reading or comicmeta.READING.get(kind, "ltr"), "strip": None,
              "number": str(item["number"]), "label": item.get("label"), "year": series.get("year"),
              "publisher": series.get("publisher"), "language": (language or config.BOOK_LANGUAGE).lower(),
              "cover": item.get("cover") or series.get("cover"), "authors": series.get("authors") or [],
              "summary": (series.get("desc") or "")[:1000], "unit": item.get("unit") or ""}
    rid, created = db.comic_add(owner, fields, now)
    if not created:
        return rid, "exists"
    m = find_in_library(library_series(fields), item["number"], kind)
    if m and config.FAMILY_SHARING:
        if owner in m["owners"]:
            db.comic_update(rid, status="done", calibre_id=m["book_id"], detail="already in your library")
            return rid, "owned"
        share.give_ebook(m, owner)
        db.comic_update(rid, status="shared", calibre_id=m["book_id"],
                        detail="already in the family library: added to yours, nothing downloaded")
        notify.admin("shared", {"owner": owner, "title": f"{series['name']} {fields['label']}", "source": "comics",
                                "status": "shared", "seq": notify.seq_id("comic", rid)})
        return rid, "shared"
    if config.APPROVALS_REQUIRED and not _is_admin(owner):
        db.comic_update(rid, status="pending", detail="waiting for the admin's approval")
        notify.admin("requested", {"owner": owner, "title": f"{series['name']} {fields['label']}", "source": "comics",
                                   "status": "pending", "seq": notify.seq_id("comic", rid)})
        return rid, "pending"
    return rid, "queued"


def library_series(req):
    """The Calibre series a request's comic goes into: chapters have their own ('X (chapters)'),
    so the series' volume numbers stay the volumes on the Kobo."""
    return f"{req['series_name']} (chapters)" if req.get("unit") == "chapter" else req["series_name"]


def _is_admin(owner):
    try:
        return bool(((cwa.get_user(owner) or {}).get("role") or 0) & cwa.ROLE_ADMIN)
    except Exception:
        return False


# ---- the family library ---------------------------------------------------------------------------
def _calibre():
    c = sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def find_in_library(series_name, number, kind="comic", exclude=()):
    """The Calibre book that IS this issue/volume (a comic tag, the series, the number), with
    its owners, or None. Never guesses between two. `exclude`: Calibre ids a reader said were
    the wrong comic."""
    want = comicrel.norm(series_name)
    try:
        n = float(number)
    except (TypeError, ValueError):
        return None
    try:
        with _calibre() as c:
            rows = c.execute(f"""
                SELECT b.id, s.name AS series, b.series_index
                FROM books b JOIN books_series_link bsl ON bsl.book=b.id JOIN series s ON s.id=bsl.series
                WHERE b.series_index = ? AND EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag
                      WHERE l.book=b.id AND t.name IN ({','.join('?' * len(ALL_COMIC_TAGS))}))""",
                             (n, *ALL_COMIC_TAGS)).fetchall()
            hits = [r["id"] for r in rows if comicrel.norm(r["series"]) == want and r["id"] not in exclude]
            if len(hits) != 1:
                return None
            owners = sorted(t[len(config.OWNER_PREFIX):] for (t,) in c.execute(
                "SELECT t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=? AND t.name LIKE ?",
                (hits[0], config.OWNER_PREFIX + "%")))
    except sqlite3.Error:
        return None
    return {"book_id": hits[0], "owners": owners, "how": "series and number"}


# ---- the search, through Shelfmark -----------------------------------------------------------------
def _alt_names(req):
    """The series' other names, from the provider (cached), for matching release titles."""
    try:
        info, _items = comicmeta.series(req["provider"], req["series_id"], req.get("language") or "en")
        return [n for n in info.get("alt_names") or [] if n and n.isascii()][:15]
    except comicmeta.MetaError:
        return []


def _rejected_books(req):
    return {int(b[5:]) for b in req.get("blocked") or [] if isinstance(b, str) and b.startswith("book:") and b[5:].isdigit()}


def search_once(req, shelfmark_api, now=None):
    """One attempt for one queued request. Returns the new status."""
    now = now or time.time()
    rid, owner = req["id"], req["owner"]
    m = find_in_library(library_series(req), req["number"], req["kind"], exclude=_rejected_books(req))
    if m and (config.FAMILY_SHARING or owner in m["owners"]):   # arrived (for someone else) while this one waited
        if owner not in m["owners"]:
            share.give_ebook(m, owner)
        db.comic_update(rid, status="shared" if owner not in m["owners"] else "done", calibre_id=m["book_id"],
                        detail="already in the family library: added to yours, nothing downloaded")
        return "shared"
    if (req.get("candidate") or {}).get("_confirmed"):
        return _queue(req, req["candidate"], shelfmark_api, now)      # confirmed, was waiting for disk space
    req = dict(req, alt_names=_alt_names(req))
    blocked = set(req.get("blocked") or [])
    releases, errors = [], []
    for q in comicrel.queries(req):
        try:
            releases += [r for r in shelfmark_api.search_releases(q) if comicrel.norm(r.get("title") or "") not in blocked]
        except shelfmark_api.ShelfmarkError as e:
            errors.append(str(e))
        best, _notes = comicrel.pick(req, releases, exclude=set(req.get("tried") or []))
        if best and "pack" not in comicrel.judge(req, best)[2]:
            break                                # an exact single issue/volume: no need to ask again
    best, notes = comicrel.pick(req, releases, exclude=set(req.get("tried") or []))
    attempts = (req.get("attempts") or 0) + 1
    if not best:
        why = "; ".join(errors) if errors and not releases else _why_none(notes)
        days = CHAPTER_SEARCH_DAYS if req.get("unit") == "chapter" else config.COMIC_SEARCH_DAYS
        if now - (req.get("created") or now) > days * 86400:
            db.comic_update(rid, status="not-found", attempts=attempts,
                            detail=f"not found in {days} days of looking ({why})" +
                                   ("; your indexers may not carry this series' chapters: its volume will come" if req.get("unit") == "chapter" else ""))
            notify.admin("wanted-expired", {"owner": owner, "title": _title(req), "source": "comics",
                                            "seq": notify.seq_id("comic", rid)})
            return "not-found"
        wait = RETRY[min(attempts - 1, len(RETRY) - 1)]
        db.comic_update(rid, attempts=attempts, next_try=now + wait,
                        detail=f"not found yet ({why}); looking again {_when(wait)}")
        return "queued"
    if config.COMIC_CONFIRM == "sure" and not blocked and comicrel.sure(req, best):
        return _queue(dict(req, attempts=attempts), best, shelfmark_api, now)
    db.comic_update(rid, status="confirm", attempts=attempts, candidate=best, reasons=comicrel.explain(req, best),
                    detail="found a copy: is it the one you want?")
    notify.reader(owner, f"Found: {req['series_name']}", f"Copies of {req['series_name']} are waiting for your yes (Yes to all on Comics).",
                  click=notify.portal_url("/comics"), tags="question", seq=f"comicconf-{owner}-{req['provider']}-{req['series_id']}")
    return "confirm"


def _queue(req, release, shelfmark_api, now):
    """Hand the chosen release to Shelfmark, as the reader."""
    rid, owner = req["id"], req["owner"]
    release = {k: v for k, v in release.items() if k != "_confirmed"}
    import bookreq
    if not bookreq._room_for(release, comic_id=rid):
        # v6.0.1: comics may be large now (MAX_COMIC_MB); the reader's yes is kept with the copy
        # and it downloads once there is room, without asking again; the admin is told once
        first = not (req.get("candidate") or {}).get("_confirmed")
        db.comic_update(rid, status="queued", candidate=dict(release, _confirmed=True), next_try=now + 900,
                        detail="in the queue: it downloads as soon as the disk has room beside the downloads under way (checked every 15 min)")
        if first:
            notify.admin("error", {"owner": owner, "title": _title(req), "source": "comics", "seq": notify.seq_id("comic", rid),
                                   "detail": "a comic waits: not enough free disk space for it"})
        return "queued"
    try:
        uid = shelfmark_api.user_id(owner)
        if not uid:
            raise shelfmark_api.ShelfmarkError(f"{owner} has no Shelfmark account yet")
        shelfmark_api.queue_release(release, uid)
    except shelfmark_api.ShelfmarkError as e:
        db.comic_update(rid, status="queued", candidate=None, attempts=req.get("attempts") or 0, next_try=now + 900,
                        detail=f"Shelfmark: {str(e)[:200]}; looking again soon")
        return "queued"
    tried = list(req.get("tried") or []) + [str(release.get("source_id"))]
    db.comic_update(rid, status="downloading", attempts=req.get("attempts") or 0, tried=tried, queued_at=now,
                    candidate=None, size_bytes=int(release.get("size_bytes") or 0) or None, release_title=(release.get("title") or "")[:300], release_id=str(release.get("source_id")),
                    detail=f"downloading: {release.get('title')}")
    notify.admin("requested", {"owner": owner, "title": _title(req), "source": "comics", "status": "queued",
                               "detail": release.get("title"), "seq": notify.seq_id("comic", rid)})
    return "downloading"


def confirm(rid, shelfmark_api, now=None):
    """'Yes, that one': the offered copy is downloaded."""
    req = db.comic_get(rid)
    if not req or req["status"] != "confirm" or not req.get("candidate"):
        raise ComicError("there is no copy waiting to be confirmed")
    return _queue(req, req["candidate"], shelfmark_api, now or time.time())


def confirm_all(owner, provider, series_id, shelfmark_api, now=None):
    """'Yes to all' for one series: every copy offered to this reader in it is downloaded."""
    n = 0
    for req in db.comic_open(owner, statuses=("confirm",)):
        if req["provider"] == provider and req["series_id"] == str(series_id) and req.get("candidate"):
            n += _queue(req, req["candidate"], shelfmark_api, now or time.time()) == "downloading"
    return n


def _block(req, release_title=None, release_id=None, book_id=None):
    tried, blocked = list(req.get("tried") or []), list(req.get("blocked") or [])
    if release_id and str(release_id) not in tried:
        tried.append(str(release_id))
    if release_title and comicrel.norm(release_title) not in blocked:
        blocked.append(comicrel.norm(release_title))
    if book_id and f"book:{book_id}" not in blocked:
        blocked.append(f"book:{book_id}")
    return tried, blocked


def reject(rid, now=None):
    """'Not it', for an offered copy or a held file: never offered again; the search goes on."""
    now = now or time.time()
    req = db.comic_get(rid)
    if not req or req["status"] not in ("confirm", "held"):
        raise ComicError("there is nothing waiting for your answer")
    if req["status"] == "confirm":
        c = req.get("candidate") or {}
        tried, blocked = _block(req, c.get("title"), c.get("source_id"))
    else:
        tried, blocked = _block(req, req.get("release_title"), req.get("release_id"))
        drop_held(req)
        notify.admin("error", {"owner": req["owner"], "title": _title(req), "source": "comics",
                               "detail": f"the reader turned down the file that came from {req.get('release_title')}",
                               "seq": notify.seq_id("comic", rid)})
    db.comic_update(rid, status="queued", next_try=now, tried=tried, blocked=blocked, candidate=None, held_path=None,
                    detail="not that one: looking for another copy")
    return "queued"


def keep(rid, now=None):
    """'Keep it anyway' for a held file: back into the reader's dropbox, imported unchecked."""
    req = db.comic_get(rid)
    if not req or req["status"] != "held" or not req.get("held_path") or not os.path.isfile(req["held_path"]):
        raise ComicError("the held file is no longer there")
    box = os.path.join(config.DROPBOX_DIR, req["owner"])
    os.makedirs(box, exist_ok=True)
    name = os.path.basename(req["held_path"])
    dest = os.path.join(box, name) if not os.path.exists(os.path.join(box, name)) else os.path.join(box, f"{rid}-{name}")
    shutil.move(req["held_path"], dest)
    shutil.rmtree(os.path.dirname(req["held_path"]), ignore_errors=True)
    db.comic_update(rid, status="downloading", skip_check=1, held_path=None, queued_at=now or time.time(),
                    detail="kept: being added to your library")
    return "downloading"


def wrong_comic(owner, book_id, now=None):
    """'Wrong comic' on a delivered comic's page: out of the reader's library, that release and
    that Calibre book never offered again, the admin told, and the search goes on (asking first)."""
    now = now or time.time()
    req = db.comic_for_book(owner, book_id)
    if not req:
        raise ComicError("this comic did not come from a comic request")
    share.remove_ebook(owner, book_id, now)             # v6.2.1: off their Kobo too, as Remove does
    tried, blocked = _block(req, req.get("release_title"), req.get("release_id"), book_id)
    db.comic_update(req["id"], status="queued", next_try=now, tried=tried, blocked=blocked, calibre_id=None,
                    skip_check=0, detail="you said it was the wrong comic: looking for another copy")
    notify.admin("error", {"owner": owner, "title": _title(req), "source": "comics",
                           "detail": f"wrong comic reported; it came from {req.get('release_title') or 'an unknown release'}",
                           "seq": notify.seq_id("comic", req["id"])})
    return req["id"]


def _why_none(notes):
    if not notes:
        return "no releases at all"
    reasons = {}
    for _t, ok, _s, why in notes:
        if not ok:
            reasons[why] = reasons.get(why, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
    return f"{len(notes)} releases, none right: " + ", ".join(f"{n} {w}" for w, n in top)


def _when(seconds):
    return "in an hour" if seconds <= 3600 else f"in {seconds // 3600} hours" if seconds < 86400 else "tomorrow"


def _title(req):
    return f"{req['series_name']} {req.get('label') or req['number']}"


def watch_downloads(shelfmark_api, queue=None, now=None):
    """A queued download that never arrived (Shelfmark's download failed, or nothing came in a
    day): try the next-best release."""
    now = now or time.time()
    failed_titles = {(f.get("title") or "").strip().lower() for f in (shelfmark_api.failed(queue) if queue else [])}
    n = 0
    for req in db.comic_open(statuses=("downloading", "confirm")):
        # v6.0.1: already in the reader's library by series and number (it arrived under another
        # name, or the admin fixed its metadata in Calibre-Web): done, never downloaded again.
        # v6.1: a copy waiting for the reader's yes too (the Peanuts was offered again after it arrived)
        m = find_in_library(library_series(req), req["number"], req["kind"], exclude=_rejected_books(req))
        if m and req["owner"] in m["owners"]:
            db.comic_update(req["id"], status="done", calibre_id=m["book_id"], candidate=None,
                            detail="in your library" if req["status"] == "downloading" else
                            "already in your library: nothing to confirm")
            continue
        if req["status"] != "downloading":
            continue
        rel = (req.get("release_title") or "").strip().lower()
        stale = now - (req.get("queued_at") or now) > config.COMIC_ARRIVAL_HOURS * 3600
        if (rel and rel in failed_titles) or stale:
            db.comic_update(req["id"], status="queued", next_try=now,
                            detail=("the download failed in Shelfmark" if rel in failed_titles else
                                    f"nothing arrived in {config.COMIC_ARRIVAL_HOURS} h") + "; trying another release")
            n += 1
    return n


# ---- arrival -----------------------------------------------------------------------------------------
def is_comic_file(name):
    return name.lower().rsplit(".", 1)[-1] in config.COMIC_EXTS


def prepare_arrival(path, owner, workdir):
    """A comic file in a reader's dropbox -> (cbz_path, base_name, request or None) ready to
    import, or ('skip', why, request) for a volume nobody asked for (the rest of a pack).
    The CBZ is a copy in `workdir`; the original is left for the caller."""
    name = os.path.basename(path)
    ext = name.lower().rsplit(".", 1)[-1]
    cbz = os.path.join(workdir, "comic.cbz")
    if ext == "cbz":
        shutil.copyfile(path, cbz)
    else:
        repack_to_cbz(path, cbz, workdir)
    # v6.0.1: ' - Title' (Shelfmark's 'Author - Title' with no author) is 'Title'
    stem = re.sub(r"^[\s\-\u2013\u2014_.]+", "", name.rsplit(".", 1)[0]) or name.rsplit(".", 1)[0]
    req, pack_of = match_request(owner, stem)
    if req is None and pack_of is not None:
        return "skip", f"skipped: part of a pack; {_title(pack_of)} was asked for, not this one", pack_of
    if req is None:                              # the release this reader is downloading, under another name?
        req = next((r for r in db.comic_open(owner, statuses=("downloading",))
                    if r.get("release_title") and comicrel.norm(r["release_title"]) == comicrel.norm(stem)), None)
        if req and not req.get("skip_check"):
            return "skip", _hold(req, path, ["its name does not say it is " + _title(req)], {"file": name}), req
    if req and not req.get("skip_check"):
        info = comicinfo(cbz)
        problems = verify_arrival(req, cbz, stem, info)
        if problems:
            meta = {"file": name, "series": info.get("Series") or "", "number": info.get("Number") or info.get("Volume") or "",
                    "language": info.get("LanguageISO") or "", "pages": page_count(cbz)}
            return "skip", _hold(req, path, problems, meta), req
    strip = looks_like_strip(cbz)
    landscape = not strip and looks_landscape(cbz)
    base = _title(req) if req else display_title(stem)
    write_metadata(cbz, req, base, strip, landscape)
    return cbz, base, req


def display_title(stem):
    """v6.0.1: a readable Calibre title for a comic nobody asked for, from its release name:
    'The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire)' ->
    'The Complete Peanuts Vol. 1 (2004)'. Never ' - ' inside it: Calibre reads 'X - Y' in a file
    name as title and AUTHOR (the Peanuts got '1950 to 1952 (2004) (digital)...' as its author)."""
    p = comicrel.parse(stem)
    starts = [m.start() for rx in (comicrel._VOL, comicrel._CH, comicrel._HASH) for m in [rx.search(stem)] if m]
    head = stem[:min(starts)] if starts else re.split(r"[(\[]", stem)[0]
    if not starts and p["issues"]:                       # 'Saga 012 (2013)': the bare number ends the series
        m = comicrel._BARE.search(re.split(r"[(\[]", stem)[0])
        head = stem[:m.start()] if m else head
    series = re.sub(r"\s+[-\u2013\u2014]\s+", ": ", head).strip(" -\u2013\u2014_.:,")
    series = re.sub(r"\s+", " ", series) or re.sub(r"\s+[-\u2013\u2014]\s+", ": ", stem)
    num = lambda r: ("%g" % r[0]) if r and r[0] == r[1] else (f"{r[0]:g}-{r[1]:g}" if r else "")
    if p["volumes"]:
        series += f" Vol. {num(p['volumes'])}"
    elif p["chapters"]:
        series += f" Ch. {num(p['chapters'])}"
    elif p["issues"]:
        series += f" #{num(p['issues'])}"
    return series + (f" ({p['year']})" if p["year"] else "")


# ---- the arrival check (v5.9): is the file the comic that was asked for? --------------------------------
MIN_PAGES = {"volume": 40, "issue": 8, "chapter": 5}
HELD_DIR = os.path.join(os.path.dirname(config.STATE_DB), "held-comics")


def page_count(cbz):
    try:
        with zipfile.ZipFile(cbz) as z:
            return sum(1 for n in z.namelist() if n.lower().endswith(IMAGE_EXTS) and "__macosx" not in n.lower())
    except (zipfile.BadZipFile, OSError):
        return 0


def comicinfo(cbz):
    """{Series, Number, Volume, LanguageISO, ...} from a ComicInfo.xml the file already carries;
    {} when there is none. No entities, no network, at most 1 MB."""
    try:
        from lxml import etree
        with zipfile.ZipFile(cbz) as z:
            name = next((n for n in z.namelist() if n.lower().rsplit("/", 1)[-1] == "comicinfo.xml"), None)
            if not name or z.getinfo(name).file_size > 1024 * 1024:
                return {}
            root = etree.fromstring(z.read(name), etree.XMLParser(resolve_entities=False, no_network=True))
        return {el.tag: (el.text or "").strip() for el in root if isinstance(el.tag, str) and (el.text or "").strip()}
    except Exception:                            # a broken ComicInfo says nothing either way
        return {}


def verify_arrival(req, cbz, stem, info=None):
    """[problems]: why this file does not look like the issue or volume asked for."""
    info = comicinfo(cbz) if info is None else info
    problems = []
    names = [req["series_name"]] + list(req.get("alt_names") or [])
    if info.get("Series"):
        s = comicrel.norm(info["Series"])
        if not any(s == comicrel.norm(n) or s.startswith(comicrel.norm(n) + " ") for n in names if n):
            problems.append(f"it says it is from “{info['Series'][:80]}”")
    n = comicrel._num(req["number"])
    volume = req["kind"] in comicrel.PAGE_KINDS and req.get("unit") != "chapter"
    said = comicrel._num(info.get("Volume") if volume and info.get("Volume") else info.get("Number"))
    if said is not None and n is not None and said != n and not (volume and info.get("Volume") is None and said > n * 3):
        problems.append(f"it says it is number {info.get('Volume') if volume and info.get('Volume') else info.get('Number')}")
    code = (info.get("LanguageISO") or "").lower()[:2]
    lang = (req.get("language") or "en").lower()
    if len(code) == 2 and code.isalpha() and code != lang:
        problems.append(f"it says it is in {code}")
    langs = comicrel.parse(stem)["langs"]
    if langs and lang not in langs:
        problems.append(f"its name says {', '.join(sorted(langs))}")
    pages = page_count(cbz)
    chapter = req.get("unit") == "chapter"
    least = MIN_PAGES["chapter" if chapter else "volume" if volume else "issue"]
    if pages < least:
        problems.append(f"only {pages} pages" + (": a chapter, not a volume?" if volume and not chapter else ""))
    return problems


def _hold(req, path, problems, meta):
    """Keep a copy of the arrived file aside for the reader; the caller removes the original."""
    # v6.0: in the reader's own dropbox under a dot-name the scanner skips (not in /state's backup)
    held = os.path.join(config.DROPBOX_DIR, req["owner"], f".held-comic-{int(req['id'])}")
    shutil.rmtree(held, ignore_errors=True)
    os.makedirs(held, exist_ok=True)
    dest = os.path.join(held, os.path.basename(path))
    shutil.copyfile(path, dest)
    db.comic_update(req["id"], status="held", held_path=dest, held_meta=meta,
                    detail="the file that came does not look like this one: " + "; ".join(problems))
    notify.reader(req["owner"], f"Check: {_title(req)}", f"What arrived for {_title(req)} does not look like it: " + "; ".join(problems),
                  click=notify.portal_url("/comics"), tags="warning", seq=f"comic-{req['id']}")
    notify.admin("error", {"owner": req["owner"], "title": _title(req), "source": "comics",
                           "detail": "held for the reader to check: " + "; ".join(problems),
                           "seq": notify.seq_id("comic", req["id"])})
    return f"skipped: held for {req['owner']} to check, it does not look like {_title(req)}: " + "; ".join(problems)


def drop_held(req):
    if req.get("held_path"):
        shutil.rmtree(os.path.dirname(req["held_path"]), ignore_errors=True)


def match_request(owner, stem):
    """(request, None) when the file is one this reader asked for; (None, request) when it is
    ANOTHER number of a series they are downloading (a pack's other volumes); (None, None)."""
    p = comicrel.parse(stem)
    series_hit = None
    for req in db.comic_open(owner):
        r = dict(req, alt_names=[])
        if not comicrel._series_ok(r, p):
            continue
        n = comicrel._num(req["number"])
        if req.get("unit") == "chapter":
            rng = p["chapters"]
        else:
            rng = p["volumes"] if req["kind"] in comicrel.PAGE_KINDS else p["issues"]
            rng = rng or p["volumes"] or p["issues"]
            if p["chapters"] and not p["volumes"]:
                rng = None                       # a chapter file never fills a volume or issue request
        if rng and n is not None and rng[0] == rng[1] == n:
            return req, None
        if req["status"] == "downloading":
            series_hit = req
    return None, series_hit


def _expand_limit():
    """How far a comic archive may expand: three times the comic cap (images barely compress, so
    a real one expands by a few percent; an archive that claims more is refused BEFORE it is
    unpacked onto the 80 GB disk)."""
    return config.MAX_COMIC_MB * 1024 * 1024 * 3


def _check_room(src, claimed):
    """Refuse before unpacking: more than _expand_limit(), or more than the disk can take while
    keeping 2 GiB free (the unpacked pages, the CBZ and the Calibre copy exist at once)."""
    if claimed > _expand_limit():
        raise ComicError("the archive expands to more than a comic can be")
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(src))).free
    except OSError:
        return
    need = 2 * max(claimed, os.path.getsize(src)) + 2 * 1024 ** 3
    if free < need:
        raise ComicError(f"not enough free disk to unpack it ({free >> 30} GB free, {need >> 30} GB needed); "
                         f"it is kept, and imported when you move it back once there is room")


def _unpack(src, out):
    """unar first (every RAR version, 7z; no nested archives), bsdtar if unar is missing."""
    if shutil.which(config.UNAR):
        listing = subprocess.run([config.LSAR, src], capture_output=True, text=True, timeout=120)
        if listing.returncode != 0:
            raise ComicError(f"not a readable comic archive: {(listing.stdout or listing.stderr or '')[-120:]}")
        if len(listing.stdout.splitlines()) > MAX_PAGES + 1:
            raise ComicError("the archive has far more files than a comic")
        sizes = subprocess.run([config.LSAR, "-j", src], capture_output=True, text=True, timeout=120)
        try:
            claimed = sum(int(e.get("XADFileSize") or 0) for e in json.loads(sizes.stdout).get("lsarContents") or [])
        except (ValueError, AttributeError, TypeError):
            claimed = 0                          # no JSON listing: the size check after unpacking still runs
        _check_room(src, claimed)
        return subprocess.run([config.UNAR, "-q", "-f", "-D", "-nr", "-o", out, src],
                              capture_output=True, text=True, timeout=1800)
    listing = subprocess.run([config.BSDTAR, "-tf", src], capture_output=True, text=True, timeout=120)
    if listing.returncode != 0:
        raise ComicError(f"not a readable comic archive: {(listing.stderr or '')[:120]}")
    if len(listing.stdout.splitlines()) > MAX_PAGES:
        raise ComicError("the archive has far more files than a comic")
    verbose = subprocess.run([config.BSDTAR, "-tvf", src], capture_output=True, text=True, timeout=120)
    claimed = 0
    for line in verbose.stdout.splitlines():     # -rw-r--r--  0 u g  <size> <date> <name>
        f = line.split()
        if len(f) > 4 and f[4].isdigit():
            claimed += int(f[4])
    _check_room(src, claimed)
    return subprocess.run([config.BSDTAR, "-xf", src, "-C", out, "--no-same-owner", "--no-same-permissions"],
                          capture_output=True, text=True, timeout=1800)


def repack_to_cbz(src, dst, workdir):
    """CBR (any RAR) or CB7 -> CBZ: images only, in reading order. Never follows a link, keeps
    nothing that unpacked outside its folder, refuses absurd member counts and archives that
    expand past the ebook size limit several times."""
    out = os.path.join(workdir, "pages")
    os.makedirs(out, exist_ok=True)
    try:
        r = _unpack(src, out)
    except (OSError, subprocess.SubprocessError) as e:
        raise ComicError(f"cannot read this archive ({type(e).__name__})")
    if r.returncode != 0:
        raise ComicError(f"could not unpack it: {(r.stderr or r.stdout or '')[:120]}")
    real_out = os.path.realpath(out)
    pages, total = [], 0
    for root, _dirs, files in os.walk(out):
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full) or "__macosx" in full.lower() or f.startswith("."):
                continue
            if not os.path.realpath(full).startswith(real_out + os.sep):
                continue
            if f.lower().endswith(IMAGE_EXTS) or f == "ComicInfo.xml":
                total += os.path.getsize(full)
                pages.append(full)
    if total > _expand_limit():
        raise ComicError("the archive expands to more than a comic can be")
    if not any(p.lower().endswith(IMAGE_EXTS) for p in pages):
        raise ComicError("no pages (images) in this archive")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:
        for full in sorted(pages, key=lambda p: _natural(os.path.relpath(p, out))):
            z.write(full, os.path.relpath(full, out))
    shutil.rmtree(out, ignore_errors=True)


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---- metadata written into the file -------------------------------------------------------------------
def write_metadata(cbz, req, stem, strip, landscape=False):
    """ComicInfo.xml (read by KCC, Panels, Chunky, KOReader) and a ComicBookInfo block in the
    zip comment (the only comic metadata Calibre reads: series, number, credits, tags). The
    owner tag is added afterwards by the ordinary CBZ tagger, which keeps this block."""
    from tagger import CBI_KEY
    kind = (req or {}).get("kind") or "comic"
    tags = [COMIC_TAGS.get(kind, "Comics")] + ([STRIP_TAG] if strip else []) + ([LANDSCAPE_TAG] if landscape else [])
    title = _title(req) if req else stem
    ci = {"Title": title}
    cbi = {"title": title, "tags": tags}
    if req:
        n = req["number"]
        ci.update({"Series": req["series_name"], "Number": n, "LanguageISO": req.get("language") or "en",
                   "Manga": "YesAndRightToLeft" if req.get("reading") == "rtl" else "No",
                   "Summary": req.get("summary") or "", "Publisher": req.get("publisher") or "",
                   "Writer": ", ".join(req.get("authors") or []), "Notes": f"bookstack comic request {req['id']}"})
        if req["kind"] in comicrel.PAGE_KINDS and req.get("unit") != "chapter":
            ci["Volume"] = n
        if req.get("year"):
            ci["Year"] = str(req["year"])
        if req.get("unit") == "chapter":
            tags.append("Chapter")
        cbi.update({"series": library_series(req), "issue": n, "publisher": req.get("publisher") or None,
                    "comments": req.get("summary") or None, "language": req.get("language") or "en",
                    "credits": [{"person": a, "role": "Writer", "primary": True} for a in req.get("authors") or []]})
        if req.get("year"):
            cbi["publicationYear"] = int(req["year"])
    if strip:
        ci["Format"] = "Web Comic"
    xml = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<ComicInfo xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">\n" + \
          "".join(f"  <{k}>{escape(str(v))}</{k}>\n" for k, v in ci.items() if v not in (None, "")) + "</ComicInfo>\n"
    tmp = cbz + ".meta"
    with zipfile.ZipFile(cbz) as zin, zipfile.ZipFile(tmp, "w") as zout:
        try:
            info = json.loads(zin.comment.decode("utf-8")) if zin.comment else {}
            if not isinstance(info, dict):
                info = {}
        except (ValueError, UnicodeDecodeError):
            info = {}
        old = info.get(CBI_KEY) if isinstance(info.get(CBI_KEY), dict) else {}
        keep = [t for t in (old.get("tags") or []) if isinstance(t, str) and t.startswith(config.OWNER_PREFIX)]
        cbi = {k: v for k, v in cbi.items() if v not in (None, "", [])}
        cbi["tags"] = tags + keep
        info[CBI_KEY] = cbi
        info.setdefault("appID", "bookstack")
        for zi in zin.infolist():
            if zi.filename.lower() == "comicinfo.xml":
                continue
            zout.writestr(zi, zin.read(zi.filename))
        zout.writestr("ComicInfo.xml", xml)
        zout.comment = json.dumps(info).encode("utf-8")
    os.replace(tmp, cbz)


# ---- long strips (webtoons): taller than wide by far ----------------------------------------------------
def image_size(data):
    """(width, height) from the first bytes of a JPEG, PNG, GIF or WebP, or None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if data[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if data[12:16] == b"VP8L":
                b = data[21:25]
                return (1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
        if data[:2] == b"\xff\xd8":
            i = 2
            while i <= len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h)
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


def looks_landscape(cbz, sample=12):
    """v6.1: a landscape BOOK, most sampled pages clearly wider than tall (The Complete Peanuts:
    three or four strips stacked on a wide page). KCC's default takes a wide page for a two-page
    spread and cuts it in half, which cut every strip; such a book is rotated instead. A portrait
    comic with the odd double spread keeps KCC's default."""
    try:
        with zipfile.ZipFile(cbz) as z:
            names = sorted((n for n in z.namelist() if n.lower().endswith(IMAGE_EXTS)), key=_natural)
            if len(names) < 3:
                return False
            step = max(1, len(names) // sample)
            wide = seen = 0
            for n in names[::step][:sample]:
                with z.open(n) as f:
                    size = image_size(f.read(256 * 1024))
                if size and size[1]:
                    seen += 1
                    wide += size[0] / size[1] >= 1.15
            return seen >= 3 and wide * 10 >= seen * 6
    except (zipfile.BadZipFile, OSError):
        return False


# ---- v6.2: how a comic's pages are laid out on an e-reader, from the shape of ALL its pages -------------
# KCC (v12) counts a page as a two-page spread when it is more than 1.16 times wider than tall;
# its default cuts every such page in half, which is right for a spread and ruins a landscape
# page (a strip across the whole width). The layout is chosen per book, from its pages, and a
# reader can choose another on the comic's page; the host job turns it into KCC's options.
WIDE = 1.16                 # KCC's own threshold (image.py splitCheck)
STRIP_RATIO = 2.5           # taller than this: a webtoon strip; wider than this: a row of panels
LAYOUTS = {                 # what the reader reads; KCC's options for each: scripts/comic-convert.sh LAYOUT_FLAGS
    "portrait": "Pages as they are (no wide pages)",
    "spreads": "Two-page spreads: each half in turn, then the whole spread turned sideways",
    "split": "Wide pages cut in half (the classic way)",
    "rotate": "Wide pages turned sideways, never cut (landscape books, like The Complete Peanuts)",
    "strip": "Long strip (webtoon): each tall page is cut into screens",
    "dailies": "Newspaper dailies: each row of panels becomes two rows (every page must be a row of panels)",
}
CHOOSABLE = ("spreads", "split", "rotate", "strip", "dailies")
_SHAPES = {}                # (path, size, mtime) -> page_shapes(): a book page is shown often


def _read_size(z, name):
    with z.open(name) as f:
        head = f.read(64 * 1024)
        size = image_size(head)
        if size is None and len(head) == 64 * 1024:          # a JPEG with a large EXIF block first
            size = image_size(head + f.read(448 * 1024))
    return size


def page_shapes(cbz, limit=400):
    """{pages, seen, wide, tall, panels, sizes} from the image headers of a CBZ: every page up to
    `limit` (spread out evenly past it), headers only, never a whole image. wide: w/h > 1.16;
    tall: h/w >= 2.5 (webtoon); panels: w/h >= 2.5 (a row of newspaper panels)."""
    out = {"pages": 0, "seen": 0, "wide": 0, "tall": 0, "panels": 0, "sizes": []}
    try:
        st = os.stat(cbz)
        key = (cbz, st.st_size, st.st_mtime)
        if key in _SHAPES:
            return _SHAPES[key]
        with zipfile.ZipFile(cbz) as z:
            names = sorted((n for n in z.namelist() if n.lower().endswith(IMAGE_EXTS) and "__macosx" not in n.lower()),
                           key=_natural)
            out["pages"] = len(names)
            step = max(1, -(-len(names) // limit))
            for n in names[::step]:
                size = _read_size(z, n)
                if not size or not size[0] or not size[1]:
                    continue
                w, h = size
                out["seen"] += 1
                out["sizes"].append((w, h))
                out["wide"] += w / h > WIDE
                out["tall"] += h / w >= STRIP_RATIO
                out["panels"] += w / h >= STRIP_RATIO
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return out
    if len(_SHAPES) > 500:
        _SHAPES.clear()
    _SHAPES[key] = out
    return out


def auto_layout(shapes, strip_tag=False):
    """The layout a book's pages call for:
      strip    - a webtoon (the Long strip tag, or most pages 2.5 times taller than wide);
      dailies  - EVERY page a row of panels (2.5 times wider than tall): KCC's maximizestrips
                 reshapes every page, so one portrait cover rules it out;
      rotate   - a landscape book: at least 60% of the pages wide (The Complete Peanuts);
      spreads  - a portrait book with some two-page spreads: each half, then the whole spread;
      portrait - no wide page at all."""
    seen = shapes.get("seen") or 0
    if strip_tag or (seen and shapes["tall"] * 2 > seen):
        return "strip"
    if not seen:
        return "portrait"
    if shapes["panels"] == seen and seen >= 3:
        return "dailies"
    if shapes["wide"] * 10 >= seen * 6:
        return "rotate"
    if shapes["wide"]:
        return "spreads"
    return "portrait"


def wants_upscale(shapes, profile, layout, default=None):
    """KCC's -u: most pages smaller than 80% of the screen (a low-resolution scan). Resized by
    KCC (Lanczos, tuned for e-ink) rather than by the device, which shows it sharper; never for a
    webtoon (KCC refuses) or dailies."""
    import devicemodels
    screen = devicemodels.SCREEN.get(profile or default or config.KCC_KOBO_PROFILE)
    sizes = shapes.get("sizes") or []
    if not screen or not sizes or layout in ("strip", "dailies"):
        return False
    small = sum(1 for w, h in sizes if max(w, h) < 0.8 * screen[1] and min(w, h) < 0.8 * screen[0])
    return small * 10 >= len(sizes) * 6


def layout_for(b, shapes=None):
    """(layout, chosen_by_reader) for a comic_books() row."""
    chosen = db.comic_layout_get(b["calibre_id"])
    if chosen in LAYOUTS:
        return chosen, True
    if b.get("strip"):
        return "strip", False
    if shapes is None:
        shapes = page_shapes(os.path.join(config.LIBRARY_DIR, b["rel"])) if b.get("rel") else {}
    return auto_layout(shapes), False


def looks_like_strip(cbz, sample=8):
    """A webtoon: most sampled pages at least 2.5 times taller than wide."""
    try:
        with zipfile.ZipFile(cbz) as z:
            names = sorted((n for n in z.namelist() if n.lower().endswith(IMAGE_EXTS)), key=_natural)
            if not names:
                return False
            step = max(1, len(names) // sample)
            tall = seen = 0
            for n in names[::step][:sample]:
                with z.open(n) as f:
                    size = image_size(f.read(256 * 1024))
                if size and size[0]:
                    seen += 1
                    tall += size[1] / size[0] >= 2.5
            return seen > 0 and tall * 2 > seen
    except (zipfile.BadZipFile, OSError):
        return False


# ---- after the import -----------------------------------------------------------------------------------
def arrived(req, note):
    """The file of a request was handed to Calibre-Web."""
    if req:
        db.comic_update(req["id"], status="done", detail=note[:300])
        notify.reader(req["owner"], _title(req), f"{_title(req)} is in your library.", click=notify.portal_url("/comics"),
                      tags="books", seq=f"comicdone-{req['owner']}-{req['provider']}-{req['series_id']}")
        notify.admin("done", {"owner": req["owner"], "title": _title(req), "source": "comics", "status": "done",
                              "seq": notify.seq_id("comic", req["id"])})


def comic_books(ids=None):
    """{calibre_id: {rel, title, kind, strip, owners, formats}} for the comics in the library that
    have a CBZ (restricted to `ids` when given)."""
    out = {}
    try:
        with _calibre() as c:
            q = f"""SELECT b.id, b.path, b.title, d.name, d.format FROM books b JOIN data d ON d.book=b.id
                    WHERE EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=b.id
                    AND t.name IN ({','.join('?' * len(ALL_COMIC_TAGS))}))"""
            args = list(ALL_COMIC_TAGS)
            if ids:
                q += f" AND b.id IN ({','.join('?' * len(ids))})"
                args += list(ids)
            for r in c.execute(q, args):
                b = out.setdefault(r["id"], {"calibre_id": r["id"], "title": r["title"], "formats": {}, "tags": set()})
                b["formats"][r["format"].upper()] = f"{r['path']}/{r['name']}.{r['format'].lower()}"
            for bid, b in out.items():
                b["tags"] = {t for (t,) in c.execute("SELECT t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag "
                                                     "WHERE l.book=?", (bid,))}
    except sqlite3.Error as e:
        log.warning("could not read the library's comics: %s", e)
        return {}
    for b in out.values():
        tags = b.pop("tags")
        b["owners"] = sorted(t[len(config.OWNER_PREFIX):] for t in tags if t.startswith(config.OWNER_PREFIX))
        b["kind"] = "manga" if "Manga" in tags else "manhwa" if "Manhwa" in tags else "manhua" if "Manhua" in tags else "comic"
        b["strip"] = STRIP_TAG in tags
        b["landscape"] = LANDSCAPE_TAG in tags
        b["rel"] = b["formats"].get("CBZ")
    return out


def uses_kobo(owner):
    """The reader's Kobo syncs, or (v6.2) they said on the start page that they read on a Kobo."""
    try:
        import devicemodels
        if devicemodels.has(owner, devicemodels.KOBO):
            return True
    except Exception:
        pass
    try:
        st = cwa.kobo_status(owner)
        return bool(st.get("books_on_device") or st.get("last_reading"))
    except Exception:
        return False


def kobo_queue(now=None):
    """Every comic waiting for its Kobo copy, in the order they are made (v6.0.1, a fair queue):
    a reader's 'Make Kobo copy' first, then the readers IN TURN (each one's oldest first), so one
    reader adding fifty volumes never keeps everyone else's comic waiting behind them. KCC makes one
    at a time on the 4 GB box (scripts/comic-convert.sh). Not a book a conversion failed for lately."""
    now = now or time.time()
    books = comic_books()
    state = db.comic_convert_state(books.keys())
    kobo, forced, lanes = {}, [], {}
    for bid, b in sorted(books.items()):
        st = state.get(bid)
        remake = bool(st and st.get("remake") and st.get("forced") and st["status"] == "due")
        if not b["rel"] or ("KEPUB" in b["formats"] and not remake):
            continue
        if st and (st["status"] in ("done", "failed") and not st.get("forced")):
            continue
        if st and st.get("next_try") and st["next_try"] > now:
            continue
        row = {"calibre_id": bid, "rel": b["rel"], "title": b["title"], "kind": b["kind"], "strip": b["strip"],
               "landscape": b["landscape"], "remake": remake, "owners": b["owners"]}
        if st and st.get("forced") and st["status"] == "due":
            forced.append((st.get("updated") or 0, row))
            continue
        readers = []
        for o in b["owners"]:
            if o not in kobo:
                kobo[o] = uses_kobo(o)
            if kobo[o]:
                readers.append(o)
        if readers:
            lanes.setdefault(readers[0], []).append(row)
    out = [r for _t, r in sorted(forced, key=lambda x: x[0])]
    for turn in range(max((len(v) for v in lanes.values()), default=0)):   # one from each reader, in turn
        out += [lanes[o][turn] for o in sorted(lanes) if len(lanes[o]) > turn]
    return out


def _device_fields(row, profile, colour, default):
    """What the host job runs KCC with (v6.2): the layout (from the pages, or the reader's choice),
    the device profile (None: its configured one), colour, upscaling. strip/landscape stay for a
    host job from before v6.2."""
    shapes = page_shapes(os.path.join(config.LIBRARY_DIR, row["rel"])) if row.get("rel") else {}
    layout, chosen = layout_for(row, shapes)
    row.update(layout=layout, layout_chosen=chosen, profile=profile, colour=colour,
               upscale=wants_upscale(shapes, profile, layout, default),
               strip=layout == "strip", landscape=layout == "rotate")
    return row


def kobo_copy_status(b, st=None):
    """v6.2.1, one definition for the comic's page and the cross-check: {layout, chosen, made,
    names, stale}. made: what its Kobo copy was made for (None before v6.2); names: its readers'
    Kobo models; stale: those Kobos, or its layout, now call for another copy."""
    import devicemodels
    layout, chosen = layout_for(b)
    try:
        made = json.loads((st or {}).get("made") or "null")
    except ValueError:
        made = None
    readers = [o for o in b.get("owners") or [] if uses_kobo(o)]
    profile, colour, names = devicemodels.kobo_target(readers)
    stale = bool(made and names and (made.get("profile") != (profile or made.get("profile"))
                                     or made.get("colour") != colour or made.get("layout") != layout))
    return {"layout": layout, "chosen": chosen, "made": made, "names": names, "stale": stale}


def kobo_due(now=None, limit=3):
    """The next comics for the host job (kobo_queue's head), each with its layout and the Kobo it
    is made for: the sharpest of its readers' Kobos (devicemodels.kobo_target)."""
    import devicemodels
    rows = kobo_queue(now)[:limit]
    for r in rows:
        readers = [o for o in r.pop("owners", []) if uses_kobo(o)]
        profile, colour, _names = devicemodels.kobo_target(readers)
        _device_fields(r, profile, colour, config.KCC_KOBO_PROFILE)
    return rows


def kobo_position(book_id, now=None):
    """How many comics are ahead of this one for a Kobo copy, or None when it is not waiting."""
    for i, r in enumerate(kobo_queue(now)):
        if r["calibre_id"] == book_id:
            return i
    return None


def kindle_request(owner, is_admin, book_id, title):
    """A comic to a Kindle: the host job converts it first (KCC), the worker mails the parts."""
    now = time.time()
    with db._lock, db._conn() as c:
        return c.execute("INSERT INTO kindle_jobs(owner, is_admin, book_id, title, status, kind, next_try, created, updated) "
                         "VALUES(?,?,?,?, 'converting', 'comic', ?,?,?)",
                         (owner, 1 if is_admin else 0, book_id, title, now, now, now)).lastrowid


def kindle_due(limit=2):
    rows = db.kindle_comic_jobs("converting", limit)
    books = comic_books([r["book_id"] for r in rows]) if rows else {}
    out = []
    import devicemodels
    for j in rows:
        b = books.get(j["book_id"])
        if not b or not b["rel"] or (j["owner"] not in b["owners"] and not j["is_admin"]):
            db.kindle_update(j["id"], status="failed", detail="the comic is no longer in your library")
            continue
        profile, colour, _name = devicemodels.kindle_target(j["owner"])     # v6.2: their Kindle's screen
        out.append(_device_fields({"job": j["id"], "calibre_id": j["book_id"], "rel": b["rel"], "title": b["title"],
                                   "kind": b["kind"], "strip": b["strip"], "max_mb": config.KINDLE_MAX_MB},
                                  profile, colour, config.KCC_KINDLE_PROFILE))
    return out


KINDLE_STAGE = "kindle-comics"


def kindle_result(job, ok, files=(), reason=None):
    """The host job made the Kindle files (names under staging/kindle-comics/<job>/) or could not."""
    if not ok:
        db.kindle_update(job, status="failed", detail=f"could not make a Kindle copy: {(reason or '')[:200]}")
        return
    d = os.path.join(config.STAGING_DIR, KINDLE_STAGE, str(int(job)))
    names = [os.path.basename(f) for f in files if f and os.path.isfile(os.path.join(d, os.path.basename(f)))]
    if not names:
        db.kindle_update(job, status="failed", detail="the Kindle copy was not found")
        return
    db.kindle_update(job, status="queued", files=json.dumps(names), next_try=time.time())



# ---- v5.9: a volume replaces the chapters a reader had (they decide) ----------------------------------
def _chapter_books(owner, series_name):
    """[{book_id, number}] of the reader's chapters of a series (Calibre series 'X (chapters)')."""
    want = comicrel.norm(f"{series_name} (chapters)")
    tag = config.OWNER_PREFIX + owner
    try:
        with _calibre() as c:
            rows = c.execute("""SELECT b.id, s.name AS series, b.series_index FROM books b
                JOIN books_series_link bsl ON bsl.book=b.id JOIN series s ON s.id=bsl.series
                WHERE EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=b.id AND t.name=?)""",
                             (tag,)).fetchall()
    except sqlite3.Error:
        return []
    return sorted(({"book_id": r["id"], "number": r["series_index"]} for r in rows if comicrel.norm(r["series"]) == want),
                  key=lambda x: x["number"] or 0)


def offer_swap(req, volume_book):
    """A volume arrived (or was shared) for a reader who has chapters of the series: offer to take
    out the ones it holds (pre-ticked from MangaDex when it knows; the reader decides)."""
    if req.get("unit") == "chapter" or req["kind"] not in comicrel.PAGE_KINDS or db.comic_swap_exists(req["owner"], req["id"]):
        return None
    chapters = _chapter_books(req["owner"], req["series_name"])
    if not chapters:
        return None
    held = None
    if req["provider"] == "mangaupdates":
        import mangadex
        held = mangadex.chapters_in(req["series_id"], req["series_name"], req["number"])
    held_set = set(held or [])
    rows = [dict(ch, tick=bool(held_set) and float(ch["number"] or -1) in held_set) for ch in chapters]
    if held_set and not any(r["tick"] for r in rows):
        return None                              # none of the reader's chapters are in this volume
    return db.comic_swap_add(req["owner"], req["id"], req["series_name"], req["number"], volume_book, rows, bool(held_set))


def swap(owner, sid, book_ids):
    """Take the chosen chapters out of the reader's library (as Remove from my library does)."""
    sw = db.comic_swap_get(sid)
    if not sw or sw["owner"] != owner or sw["status"] != "offered":
        raise ComicError("nothing to swap")
    mine = {c["book_id"] for c in sw["chapters"]}
    n = 0
    for bid in book_ids:
        if bid in mine:
            share.remove_ebook(owner, bid)               # v6.2.1: off their Kobo too, as Remove does
            n += 1
    db.comic_swap_set(sid, "done" if n else "kept")
    return n
