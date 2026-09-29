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
RTL_KINDS = ("manga",)
RETRY = (3600, 6 * 3600) + (86400,) * 60
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
              "summary": (series.get("desc") or "")[:1000]}
    rid, created = db.comic_add(owner, fields, now)
    if not created:
        return rid, "exists"
    m = find_in_library(series["name"], item["number"], kind)
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


def find_in_library(series_name, number, kind="comic"):
    """The Calibre book that IS this issue/volume (a comic tag, the series, the number), with
    its owners, or None. Never guesses between two."""
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
            hits = [r["id"] for r in rows if comicrel.norm(r["series"]) == want]
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


def search_once(req, shelfmark_api, now=None):
    """One attempt for one queued request. Returns the new status."""
    now = now or time.time()
    rid, owner = req["id"], req["owner"]
    m = find_in_library(req["series_name"], req["number"], req["kind"])
    if m and config.FAMILY_SHARING:              # arrived for someone else while this one waited
        if owner not in m["owners"]:
            share.give_ebook(m, owner)
        db.comic_update(rid, status="shared" if owner not in m["owners"] else "done", calibre_id=m["book_id"],
                        detail="already in the family library: added to yours, nothing downloaded")
        return "shared"
    req = dict(req, alt_names=_alt_names(req))
    releases, errors = [], []
    for q in comicrel.queries(req):
        try:
            releases += shelfmark_api.search_releases(q)
        except shelfmark_api.ShelfmarkError as e:
            errors.append(str(e))
        best, _notes = comicrel.pick(req, releases, exclude=set(req.get("tried") or []))
        if best and "pack" not in comicrel.judge(req, best)[2]:
            break                                # an exact single issue/volume: no need to ask again
    best, notes = comicrel.pick(req, releases, exclude=set(req.get("tried") or []))
    attempts = (req.get("attempts") or 0) + 1
    if not best:
        why = "; ".join(errors) if errors and not releases else _why_none(notes)
        if now - (req.get("created") or now) > config.COMIC_SEARCH_DAYS * 86400:
            db.comic_update(rid, status="not-found", attempts=attempts,
                            detail=f"not found in {config.COMIC_SEARCH_DAYS} days of looking ({why})")
            notify.admin("wanted-expired", {"owner": owner, "title": _title(req), "source": "comics",
                                            "seq": notify.seq_id("comic", rid)})
            return "not-found"
        wait = RETRY[min(attempts - 1, len(RETRY) - 1)]
        db.comic_update(rid, attempts=attempts, next_try=now + wait,
                        detail=f"not found yet ({why}); looking again {_when(wait)}")
        return "queued"
    try:
        uid = shelfmark_api.user_id(owner)
        if not uid:
            raise shelfmark_api.ShelfmarkError(f"{owner} has no Shelfmark account yet")
        shelfmark_api.queue_release(best, uid)
    except shelfmark_api.ShelfmarkError as e:
        db.comic_update(rid, attempts=attempts, next_try=now + 900, detail=f"Shelfmark: {str(e)[:200]}; trying again soon")
        return "queued"
    tried = list(req.get("tried") or []) + [str(best.get("source_id"))]
    db.comic_update(rid, status="downloading", attempts=attempts, tried=tried, queued_at=now,
                    release_title=(best.get("title") or "")[:300], release_id=str(best.get("source_id")),
                    detail=f"downloading: {best.get('title')}")
    notify.admin("requested", {"owner": owner, "title": _title(req), "source": "comics", "status": "queued",
                               "detail": best.get("title"), "seq": notify.seq_id("comic", rid)})
    return "downloading"


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
    for req in db.comic_open(statuses=("downloading",)):
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
    stem = name.rsplit(".", 1)[0]
    req, pack_of = match_request(owner, stem)
    if req is None and pack_of is not None:
        return "skip", f"skipped: part of a pack; {_title(pack_of)} was asked for, not this one", pack_of
    strip = looks_like_strip(cbz)
    write_metadata(cbz, req, stem, strip)
    base = _title(req) if req else stem
    return cbz, base, req


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
        rng = p["volumes"] if req["kind"] in comicrel.PAGE_KINDS else p["issues"]
        rng = rng or p["volumes"] or p["issues"]
        if rng and n is not None and rng[0] == rng[1] == n:
            return req, None
        if req["status"] == "downloading":
            series_hit = req
    return None, series_hit


def _unpack(src, out):
    """unar first (every RAR version, 7z; no nested archives), bsdtar if unar is missing."""
    if shutil.which(config.UNAR):
        listing = subprocess.run([config.LSAR, src], capture_output=True, text=True, timeout=120)
        if listing.returncode != 0:
            raise ComicError(f"not a readable comic archive: {(listing.stdout or listing.stderr or '')[-120:]}")
        if len(listing.stdout.splitlines()) > MAX_PAGES + 1:
            raise ComicError("the archive has far more files than a comic")
        return subprocess.run([config.UNAR, "-q", "-f", "-D", "-nr", "-o", out, src],
                              capture_output=True, text=True, timeout=600)
    listing = subprocess.run([config.BSDTAR, "-tf", src], capture_output=True, text=True, timeout=120)
    if listing.returncode != 0:
        raise ComicError(f"not a readable comic archive: {(listing.stderr or '')[:120]}")
    if len(listing.stdout.splitlines()) > MAX_PAGES:
        raise ComicError("the archive has far more files than a comic")
    return subprocess.run([config.BSDTAR, "-xf", src, "-C", out, "--no-same-owner", "--no-same-permissions"],
                          capture_output=True, text=True, timeout=600)


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
    if total > config.MAX_EBOOK_MB * 1024 * 1024 * 4:
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
def write_metadata(cbz, req, stem, strip):
    """ComicInfo.xml (read by KCC, Panels, Chunky, KOReader) and a ComicBookInfo block in the
    zip comment (the only comic metadata Calibre reads: series, number, credits, tags). The
    owner tag is added afterwards by the ordinary CBZ tagger, which keeps this block."""
    from tagger import CBI_KEY
    kind = (req or {}).get("kind") or "comic"
    tags = [COMIC_TAGS.get(kind, "Comics")] + ([STRIP_TAG] if strip else [])
    title = _title(req) if req else stem
    ci = {"Title": title}
    cbi = {"title": title, "tags": tags}
    if req:
        n = req["number"]
        ci.update({"Series": req["series_name"], "Number": n, "LanguageISO": req.get("language") or "en",
                   "Manga": "YesAndRightToLeft" if req.get("reading") == "rtl" else "No",
                   "Summary": req.get("summary") or "", "Publisher": req.get("publisher") or "",
                   "Writer": ", ".join(req.get("authors") or []), "Notes": f"bookstack comic request {req['id']}"})
        if req["kind"] in comicrel.PAGE_KINDS:
            ci["Volume"] = n
        if req.get("year"):
            ci["Year"] = str(req["year"])
        cbi.update({"series": req["series_name"], "issue": n, "publisher": req.get("publisher") or None,
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
        b["rel"] = b["formats"].get("CBZ")
    return out


def uses_kobo(owner):
    try:
        st = cwa.kobo_status(owner)
        return bool(st.get("books_on_device") or st.get("last_reading"))
    except Exception:
        return False


def kobo_due(now=None, limit=3):
    """Comics that need their Kobo copy: a CBZ and no KEPUB, and an owner who reads on a Kobo
    (or a reader asked for it with 'Make Kobo copy'). Not a book a conversion failed for lately."""
    now = now or time.time()
    books = comic_books()
    state = db.comic_convert_state(books.keys())
    kobo = {}
    out = []
    # a reader's 'Make Kobo copy' first, then the oldest
    order = sorted(books.items(), key=lambda kv: (not (state.get(kv[0]) or {}).get("forced"), kv[0]))
    for bid, b in order:
        if not b["rel"] or "KEPUB" in b["formats"]:
            continue
        st = state.get(bid)
        if st and (st["status"] in ("done", "failed") and not st.get("forced")):
            continue
        if st and st.get("next_try") and st["next_try"] > now:
            continue
        forced = bool(st and st.get("forced") and st["status"] == "due")
        if not forced:
            wanted = False
            for o in b["owners"]:
                if o not in kobo:
                    kobo[o] = uses_kobo(o)
                wanted = wanted or kobo[o]
            if not wanted:
                continue
        out.append({"calibre_id": bid, "rel": b["rel"], "title": b["title"], "kind": b["kind"], "strip": b["strip"]})
        if len(out) >= limit:
            break
    return out


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
    for j in rows:
        b = books.get(j["book_id"])
        if not b or not b["rel"] or (j["owner"] not in b["owners"] and not j["is_admin"]):
            db.kindle_update(j["id"], status="failed", detail="the comic is no longer in your library")
            continue
        out.append({"job": j["id"], "calibre_id": j["book_id"], "rel": b["rel"], "title": b["title"],
                    "kind": b["kind"], "strip": b["strip"], "max_mb": config.KINDLE_MAX_MB})
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
