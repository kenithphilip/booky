"""One-tap book requests (v5.8.3): the portal finds the copy, the reader confirms it, Shelfmark
downloads it, and the file is checked before it reaches the library.

Until v5.8.2 a book from New for you opened Shelfmark already searching for it and the reader
picked a copy there. Now "Get it" works as a comic request does (comics.py):

  1. the family library first: a copy someone already has is given to the reader (their owner
     tag is added, share.py), nothing is downloaded;
  2. the approval setting: with APPROVALS_REQUIRED a reader's request waits for the admin
     (Status page), exactly like every other request;
  3. the search: Shelfmark's own Prowlarr search (GET /api/releases, manual query, category
     Books), scored by bookrel.py; nothing right is searched again after 1 h, 6 h, then daily,
     for BOOK_SEARCH_DAYS, and then the reader is told to pick one in Shelfmark;
  4. CONFIRM: the copy found is shown to the reader (name, format, size, indexer, why) and nothing
     downloads until they say "Yes, that one"; "Not it" remembers that release name and looks for
     the next. BOOK_CONFIRM=sure skips the question only for a certain pick (title and author in
     the name, a retail EPUB, the reader's language), never after the reader turned one down;
  5. the download: POST /api/releases/download on behalf of the reader's Shelfmark account.
     From there it is Shelfmark's normal download: the seedbox client with Shelfmark's category,
     torrents kept seeding, Syncthing, the path mappings, the reader's dropbox. The portal never
     touches the seedbox;
  6. THE FILE IS CHECKED on arrival, before the import: its own title, author, language and ISBN
     (read from inside the EPUB/MOBI/AZW3) and its format against the book asked for. An ISBN
     that is one of the book's editions in the reader's language (Hardcover) settles it. A file
     that does not match is HELD, not imported: the reader sees what it says it is and chooses
     "Keep it anyway" or "Not it" (the next release);
  7. done when the book is in the library with the reader's owner tag. "Wrong book" on its page
     takes it out of their library, remembers that release and that Calibre book, tells the
     admin, and looks again (always asking first).

"Pick in Shelfmark" stays next to "Get it" for a reader who wants to choose the edition."""
import logging, os, shutil, time, zipfile
import config, db, bookrel, dedupe, matching, notify, share

log = logging.getLogger("bookreq")

RETRY = (3600, 6 * 3600) + (86400,) * 60


class BookRequestError(Exception):
    pass


def _is_admin(owner):
    import comics
    return comics._is_admin(owner)


def _title(req):
    return req["title"] + (f" by {req['author']}" if req.get("author") else "")


def _own_copy(owner, title, author):
    """The reader's own copy when family sharing is off (share.find_ebook looks only when it is on)."""
    m = dedupe.Index(owner, force=True).match(title, author)
    return dict(m, owners=[owner]) if m and m["how"] in share.STRONG else None


def request(owner, title, author="", series=None, language=None, hardcover_id=None, notice_id=None, now=None):
    """(request_id, what): 'owned' (the reader has it), 'shared' (the family had it: added to
    theirs, nothing downloaded), 'pending' (waits for the admin), 'queued' or 'exists'."""
    title, author = (title or "").strip()[:300], (author or "").strip()[:200]
    if not title:
        raise BookRequestError("no title to look for")
    if db.bookreq_count_open(owner) >= config.BOOK_MAX_OPEN_PER_USER and not _is_admin(owner):
        raise BookRequestError(f"you have {config.BOOK_MAX_OPEN_PER_USER} book requests open already")
    fields = {"title": title, "author": author, "series": series, "hardcover_id": hardcover_id,
              "notice_id": notice_id, "language": (language or db.get_prefs(owner).get("language")
                                                    or config.BOOK_LANGUAGE).lower()}
    rid, created = db.bookreq_add(owner, fields, now)
    if not created:
        return rid, "exists"
    m = share.find_ebook(title, author) or _own_copy(owner, title, author)
    if m:
        if owner in m["owners"]:
            db.bookreq_update(rid, status="owned", calibre_id=m["book_id"], detail="already in your library")
            return rid, "owned"
        share.give_ebook(m, owner)
        db.bookreq_update(rid, status="shared", calibre_id=m["book_id"],
                          detail="already in the family library: added to yours, nothing downloaded")
        notify.admin("shared", {"owner": owner, "title": title, "author": author, "source": "books",
                                "status": "shared", "seq": notify.seq_id("book", rid)})
        return rid, "shared"
    if config.APPROVALS_REQUIRED and not _is_admin(owner):
        db.bookreq_update(rid, status="pending", detail="waiting for the admin's approval")
        notify.admin("requested", {"owner": owner, "title": title, "author": author, "source": "books",
                                   "status": "pending", "seq": notify.seq_id("book", rid)})
        return rid, "pending"
    return rid, "queued"


def _library_copy(req):
    """The family's (or the reader's own) copy of the book, never one the reader said was wrong."""
    rejected = {b for b in req.get("blocked") or [] if isinstance(b, str) and b.startswith("book:")}
    m = share.find_ebook(req["title"], req.get("author") or "") or _own_copy(req["owner"], req["title"], req.get("author") or "")
    return None if not m or f"book:{m['book_id']}" in rejected else m


def search_once(req, shelfmark_api, now=None):
    """One attempt for one queued request. Returns the new status."""
    now = now or time.time()
    rid, owner = req["id"], req["owner"]
    m = _library_copy(req)
    if m:                                        # arrived for someone else while this one waited
        if owner not in m["owners"]:
            share.give_ebook(m, owner)
        status = "owned" if owner in m["owners"] else "shared"
        db.bookreq_update(rid, status=status, calibre_id=m["book_id"],
                          detail="already in your library" if status == "owned" else
                          "already in the family library: added to yours, nothing downloaded")
        return status
    want = {k: req.get(k) for k in ("title", "author", "series", "language")}
    exclude = set(req.get("tried") or [])
    blocked = set(req.get("blocked") or [])
    releases, errors = [], []
    for q in bookrel.queries(want):
        try:
            releases += [r for r in shelfmark_api.search_releases(q, book_id="book")
                         if bookrel.blocked_key(r.get("title") or "") not in blocked]
        except shelfmark_api.ShelfmarkError as e:
            errors.append(str(e))
        best, _notes = bookrel.pick(want, releases, exclude=exclude)
        if best and bookrel.judge(want, best)[2] == "exact":
            break                                # title and author: no need to ask again
    best, notes = bookrel.pick(want, releases, exclude=exclude)
    attempts = (req.get("attempts") or 0) + 1
    if not best:
        why = "; ".join(errors) if errors and not releases else _why_none(notes)
        if now - (req.get("created") or now) > config.BOOK_SEARCH_DAYS * 86400:
            db.bookreq_update(rid, status="not-found", attempts=attempts,
                              detail=f"not found in {config.BOOK_SEARCH_DAYS} days of looking ({why}); "
                                     f"try Pick in Shelfmark, which searches every source")
            notify.admin("wanted-expired", {"owner": owner, "title": req["title"], "author": req.get("author"),
                                            "source": "books", "seq": notify.seq_id("book", rid)})
            return "not-found"
        wait = RETRY[min(attempts - 1, len(RETRY) - 1)]
        db.bookreq_update(rid, attempts=attempts, next_try=now + wait,
                          detail=f"not found yet ({why}); looking again {_when(wait)}")
        return "queued"
    if config.BOOK_CONFIRM == "sure" and not blocked and bookrel.sure(want, best):
        return _queue(dict(req, attempts=attempts), best, shelfmark_api, now)
    db.bookreq_update(rid, status="confirm", attempts=attempts, candidate=best, reasons=bookrel.explain(want, best),
                      detail="found a copy: is it the one you want?")
    return "confirm"


def _queue(req, release, shelfmark_api, now):
    """Hand the chosen release to Shelfmark, as the reader."""
    rid, owner = req["id"], req["owner"]
    try:
        uid = shelfmark_api.user_id(owner)
        if not uid:
            raise shelfmark_api.ShelfmarkError(f"{owner} has no Shelfmark account yet")
        shelfmark_api.queue_release(release, uid)
    except shelfmark_api.ShelfmarkError as e:
        db.bookreq_update(rid, status="queued", candidate=None, attempts=req.get("attempts") or 0, next_try=now + 900,
                          detail=f"Shelfmark: {str(e)[:200]}; looking again soon")
        return "queued"
    tried = list(req.get("tried") or []) + [str(release.get("source_id"))]
    db.bookreq_update(rid, status="downloading", attempts=req.get("attempts") or 0, tried=tried, queued_at=now,
                      downloaded=None, candidate=None,
                      release_title=(release.get("title") or "")[:300], release_id=str(release.get("source_id")),
                      detail=f"downloading: {release.get('title')}")
    notify.admin("requested", {"owner": owner, "title": req["title"], "author": req.get("author"), "source": "books",
                               "status": "queued", "detail": release.get("title"), "seq": notify.seq_id("book", rid)})
    return "downloading"


def confirm(rid, shelfmark_api, now=None):
    """'Yes, that one': the offered copy is downloaded."""
    req = db.bookreq_get(rid)
    if not req or req["status"] != "confirm" or not req.get("candidate"):
        raise BookRequestError("there is no copy waiting to be confirmed")
    return _queue(req, req["candidate"], shelfmark_api, now or time.time())


def _block(req, release_title=None, release_id=None, book_id=None):
    tried, blocked = list(req.get("tried") or []), list(req.get("blocked") or [])
    if release_id and str(release_id) not in tried:
        tried.append(str(release_id))
    if release_title and bookrel.blocked_key(release_title) not in blocked:
        blocked.append(bookrel.blocked_key(release_title))
    if book_id and f"book:{book_id}" not in blocked:
        blocked.append(f"book:{book_id}")
    return tried, blocked


def reject(rid, now=None):
    """'Not it', for an offered copy or a held file: that release is never offered again; the
    held file is deleted; the search goes on."""
    now = now or time.time()
    req = db.bookreq_get(rid)
    if not req or req["status"] not in ("confirm", "held"):
        raise BookRequestError("there is nothing waiting for your answer")
    if req["status"] == "confirm":
        c = req.get("candidate") or {}
        tried, blocked = _block(req, c.get("title"), c.get("source_id"))
    else:
        tried, blocked = _block(req, req.get("release_title"), req.get("release_id"))
        _drop_held(req)
        notify.admin("error", {"owner": req["owner"], "title": req["title"], "author": req.get("author"), "source": "books",
                               "detail": f"the reader turned down the file that came from {req.get('release_title')}",
                               "seq": notify.seq_id("book", rid)})
    db.bookreq_update(rid, status="queued", next_try=now, tried=tried, blocked=blocked, candidate=None,
                      held_path=None, detail="not that one: looking for another copy")
    return "queued"


def keep(rid, now=None):
    """'Keep it anyway' for a held file: it goes back to the reader's dropbox and is imported
    without being checked again."""
    req = db.bookreq_get(rid)
    if not req or req["status"] != "held" or not req.get("held_path") or not os.path.isfile(req["held_path"]):
        raise BookRequestError("the held file is no longer there")
    box = os.path.join(config.DROPBOX_DIR, req["owner"])
    os.makedirs(box, exist_ok=True)
    name = os.path.basename(req["held_path"])
    dest = os.path.join(box, name)
    if os.path.exists(dest):
        dest = os.path.join(box, f"{rid}-{name}")
    shutil.move(req["held_path"], dest)
    shutil.rmtree(os.path.dirname(req["held_path"]), ignore_errors=True)
    db.bookreq_update(rid, status="downloading", skip_check=1, held_path=None, queued_at=now or time.time(),
                      detail="kept: being added to your library")
    return "downloading"


def wrong_book(owner, book_id, now=None):
    """'Wrong book' on a delivered book's page: it leaves the reader's library (as Remove does),
    the release and that Calibre book are never offered again, the admin hears of it, and the
    search goes on, asking the reader before anything downloads."""
    now = now or time.time()
    req = db.bookreq_for_book(owner, book_id)
    if not req:
        raise BookRequestError("this book did not come from a Get it request")
    db.queue_untag(book_id, owner)
    tried, blocked = _block(req, req.get("release_title"), req.get("release_id"), book_id)
    db.bookreq_update(req["id"], status="queued", next_try=now, tried=tried, blocked=blocked, calibre_id=None,
                      downloaded=None, skip_check=0, detail="you said it was the wrong book: looking for another copy")
    notify.admin("error", {"owner": owner, "title": req["title"], "author": req.get("author"), "source": "books",
                           "detail": f"wrong book reported; it came from {req.get('release_title') or 'an unknown release'}",
                           "seq": notify.seq_id("book", req["id"])})
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


def _complete_titles(queue):
    """Titles of downloads Shelfmark reports complete (kept in its queue for an hour)."""
    out = set()
    for task in ((queue or {}).get("complete") or {}).values():
        if isinstance(task, dict) and task.get("title"):
            out.add(task["title"].strip().lower())
    return out


def watch_downloads(shelfmark_api, queue=None, now=None):
    """Downloads under way: done once the book is in the library with the reader's tag; the next
    release when Shelfmark's download failed, or when nothing arrived in BOOK_ARRIVAL_HOURS and
    Shelfmark never reported it complete. A download Shelfmark DID complete is never replaced by
    another (the file came, only its title in Calibre differs): after BOOK_ARRIVAL_HOURS it is
    closed and the reader is told to look in their library. Returns (arrived, retried)."""
    now = now or time.time()
    rows = db.bookreq_open(statuses=("downloading",))
    if not rows:
        return 0, 0
    failed = {(f.get("title") or "").strip().lower() for f in (shelfmark_api.failed(queue) if queue else [])}
    complete = _complete_titles(queue)
    index = dedupe.Index(None, is_admin=True, force=True)
    arrived = retried = 0
    for req in rows:
        m = index.match(req["title"], req.get("author") or "")
        if m and m["how"] in share.STRONG and f"book:{m['book_id']}" not in (req.get("blocked") or []):
            try:
                owners = share._owners_of_book(m["book_id"])
            except Exception:
                owners = []
            if req["owner"] in owners:
                db.bookreq_update(req["id"], status="done", calibre_id=m["book_id"], detail="in your library")
                arrived += 1
                continue
        names = {n for n in ((req.get("release_title") or "").strip().lower(), req["title"].strip().lower()) if n}
        if not req.get("downloaded") and names & complete:
            db.bookreq_update(req["id"], downloaded=now, detail="downloaded; being added to your library")
            continue
        if req.get("downloaded"):
            if now - req["downloaded"] > config.BOOK_ARRIVAL_HOURS * 3600:
                db.bookreq_update(req["id"], status="done",
                                  detail="Shelfmark downloaded it, but it was not found in your library under this "
                                         "title: look in My books (it may carry the file's own title)")
            continue
        stale = now - (req.get("queued_at") or now) > config.BOOK_ARRIVAL_HOURS * 3600
        if names & failed or stale:
            db.bookreq_update(req["id"], status="queued", next_try=now,
                              detail=("the download failed in Shelfmark" if not stale else
                                      f"nothing arrived in {config.BOOK_ARRIVAL_HOURS} h") + "; trying another release")
            retried += 1
    return arrived, retried


# ---- the arrival check (worker._ingest_file_entry, before the import) --------------------------------
BOOK_FILE_EXTS = ("epub", "kepub", "azw3", "azw", "mobi")
HELD_DIR = os.path.join(os.path.dirname(config.STATE_DB), "held-books")
OPF_MAX = 2 * 1024 * 1024


def file_meta(path, ext):
    """{title, author, language, identifiers} from inside the file; {} when it cannot say.
    EPUB: its OPF (no entities, no network, at most 2 MB); MOBI/AZW3: filemeta (EXTH)."""
    try:
        if ext in ("epub", "kepub"):
            from lxml import etree
            import tagger
            with zipfile.ZipFile(path) as z:
                opf = tagger._opf_path(z)
                if z.getinfo(opf).file_size > OPF_MAX:
                    return {}
                root = etree.fromstring(z.read(opf), etree.XMLParser(resolve_entities=False, no_network=True))
            meta = root.find("{http://www.idpf.org/2007/opf}metadata")
            if meta is None:
                meta = next((e for e in root.iter() if isinstance(e.tag, str) and e.tag.endswith("metadata")), None)
            return tagger._read_opf_meta(meta) if meta is not None else {}
        if ext in ("mobi", "azw3", "azw"):
            import filemeta
            return filemeta.read(path, ext)
    except Exception:                            # a damaged file is the import's problem, not this check's
        return {}
    return {}


def _isbns(meta):
    out = set()
    for i in meta.get("identifiers") or []:
        v = "".join(ch for ch in str(i.get("value") or "") if ch.isdigit() or ch in "Xx").upper()
        if len(v) in (10, 13) and (i.get("kind") in ("isbn", "unknown") or v.startswith("97")):
            out.add(v)
    return out


def _edition_match(req, meta):
    """The file's ISBN is one of this book's editions in the reader's language (Hardcover)."""
    have = _isbns(meta)
    if not have or not req.get("hardcover_id"):
        return False
    import hardcover
    for e in hardcover.editions(req["hardcover_id"]):
        if have & {x.upper() for x in e["isbns"]} and (not e["language"] or e["language"] == (req.get("language") or "en")):
            return True
    return False


def same_author(a, b):
    """A shared name AND no given name that differs on both sides: 'F. Herbert' and 'Frank Herbert'
    agree, 'Brian Herbert' and 'Frank Herbert' do not (he wrote Dune books too)."""
    ta, tb = dedupe.author_tokens(a), dedupe.author_tokens(b)
    return bool(ta & tb) and not (ta - tb and tb - ta)


def verify(req, ext, meta):
    """[problems]: why this file does not look like the book asked for ([] when it does)."""
    problems = []
    if ext not in BOOK_FILE_EXTS:
        problems.append(f"it is a {ext.upper() or 'unknown'} file, not an ebook")
    by_isbn = _edition_match(req, meta)
    want = {k: req.get(k) for k in ("title", "author", "series")}
    if meta.get("title") and not by_isbn:
        left = bookrel.title_left(want, meta["title"])
        if left is None or left:
            problems.append(f"the file says it is “{meta['title'][:120]}”")
    if meta.get("author") and req.get("author") and not by_isbn and not same_author(meta["author"], req["author"]):
        problems.append(f"by {meta['author'][:80]}")
    code = matching.lang(meta.get("language") or "") if meta.get("language") else None
    # two-letter codes only: 'und' (undetermined), 'mul' or an unmapped three-letter code says nothing
    if code and len(code) == 2 and code != (req.get("language") or "en"):
        problems.append(f"in another language ({code})")
    return problems


def _belongs(req, stem, meta):
    """Is this arrival the download of this request? By the release name, the title in the file
    name, or the title inside the file."""
    words = bookrel._plain(stem).split()
    if req.get("release_title") and bookrel._plain(req["release_title"]) == bookrel._plain(stem):
        return True
    wt = bookrel._title_words(req["title"])
    if wt and bookrel._phrase_at(words, wt) >= 0:
        return True
    return bool(meta.get("title")) and dedupe.norm_title(meta["title"]) == dedupe.norm_title(req["title"])


def check_arrival(path, owner, now=None):
    """None to import the file as usual, or a 'skipped:' note when the file was HELD because it
    does not look like the book this reader is downloading."""
    rows = [r for r in db.bookreq_open(statuses=("downloading",), owner=owner)]
    if not rows:
        return None
    name = os.path.basename(path)
    stem, _, ext = name.rpartition(".")
    ext = ext.lower() if stem else ""
    stem = stem or name
    meta = file_meta(path, ext)
    req = next((r for r in rows if _belongs(r, stem, meta)), None)
    if not req or req.get("skip_check"):
        return None
    problems = verify(req, ext, meta)
    if not problems:
        db.bookreq_update(req["id"], detail="arrived and checked: it is this book; being added to your library")
        return None
    held = os.path.join(HELD_DIR, str(req["id"]))
    os.makedirs(held, exist_ok=True)
    dest = os.path.join(held, name)
    shutil.move(path, dest)
    db.bookreq_update(req["id"], status="held", held_path=dest,
                      held_meta={"title": meta.get("title") or "", "author": meta.get("author") or "",
                                 "language": meta.get("language") or "", "format": ext, "file": name},
                      detail="the file that came does not look like this book: " + "; ".join(problems))
    notify.admin("error", {"owner": owner, "title": req["title"], "author": req.get("author"), "source": "books",
                           "detail": "held for the reader to check: " + "; ".join(problems),
                           "seq": notify.seq_id("book", req["id"])})
    return f"skipped: held for {owner} to check, it does not look like {req['title']}: " + "; ".join(problems)


def _drop_held(req):
    if req.get("held_path"):
        shutil.rmtree(os.path.dirname(req["held_path"]), ignore_errors=True)
