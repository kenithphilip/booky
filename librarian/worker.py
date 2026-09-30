"""Background worker: turns queued requests into files placed correctly and tagged to the
owner. Ingest is ATOMIC — a book is built under an ignored extension and then renamed into
place, so Calibre-Web never sees a half-written file (which it warns causes bad imports).

Trust boundary: the worker runs on the HOST network, so every URL it fetches is checked
against loopback/LAN/tailnet/link-local ranges first (a logged-in user controls the URL of a
request), catalog credentials only ever go to the configured catalog origin, and downloads
are capped per kind (local files too: a huge PDF must not OOM-kill the portal). Each loop
keeps a heartbeat in HEARTBEAT for /healthz."""
import os, time, threading, shutil, tempfile, glob, zipfile, re, socket, ipaddress, logging, unicodedata, errno, hashlib, json
from uuid import uuid4
from urllib.parse import urlsplit, urljoin
import requests
import config, db, notify, abs as absapi, kindle, cwa, library, metadata, dedupe, wanted, filemeta, share, comics
from tagger import (add_owner_tag, add_owner_tag_pdf, add_owner_tag_cbz, precheck_zip, TagError,
                    MAX_ZIP_MEMBERS as TAG_MAX_ZIP_MEMBERS)

log = logging.getLogger("worker")
UA = {"User-Agent": "bookstack-librarian/4.3"}
HEARTBEAT = {}          # loop name -> time.time() of its last pass ("queue", "dropbox", "housekeeping", "imap")
NEEDS_TAG = "needs-tag" # terminal status for formats that cannot carry the owner tag
TAGGING = "tagging"     # audiobook placed; waiting for Audiobookshelf to index it so the owner tag can be set
# MOBI/AZW3/FB2/TXT cannot carry the owner tag in the file: the host job adds it in Calibre after
# the import (reconcile_untagged + scripts/metadata-push.sh). No alarm while that is under way.
AUTO_TAG_NOTE = "the owner tag is added in Calibre automatically after the import"
AUTO_TAG_ESCALATE = 45 * 60   # still not found in Calibre after this: tell the admin and the reader

def _beat(name):
    HEARTBEAT[name] = time.time()

def _finish(rid, status, detail=None):
    db.set_status(rid, status, detail)
    r = db.get(rid)
    if status == NEEDS_TAG and AUTO_TAG_NOTE in (detail or ""):
        return                  # being tagged in Calibre; reconcile_untagged speaks up if that fails
    if r and status in notify.EVENTS:
        notify.send(status, r)

def _tmpdir():
    """Temp space for downloads under /staging (a host bind mount that is cleared at worker
    start), not the container's /tmp where a SIGKILLed job would leak a full copy forever."""
    base = os.path.join(config.STAGING_DIR, "tmp")
    try:
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(dir=base)
    except OSError:
        return tempfile.mkdtemp()

# scripts/disk-watch.sh touches this at DISK_STOP_PCT and removes it below DISK_RESUME_PCT.
# Stopping Shelfmark and qBittorrent is not enough on its own: the queue, the dropbox watcher
# and the mail intake each write multi-GB files too, and the admin has already been told
# (alert, README, self-test, Advanced settings) that intake stops when the disk is full.
DISK_PAUSE_FLAG = ".disk-paused"

def _disk_paused():
    return os.path.exists(os.path.join(config.STAGING_DIR, DISK_PAUSE_FLAG))

# Linux NAME_MAX is 255 BYTES, not characters: 150 Cyrillic or Devanagari characters are 300+
# bytes and every rename then fails with ENAMETOOLONG. Names are cut to this many UTF-8 bytes,
# which leaves headroom for the ' [owner-rid]' ingest suffix and the extension.
NAME_MAX_BYTES = 180

def _safe(name, max_bytes=NAME_MAX_BYTES):
    """A file-name-safe version of a title/author/stem: strips path separators, shell-hostile
    and control characters, keeps everything else (Calibre and ext4 are UTF-8 safe) and
    truncates by UTF-8 length so a non-ASCII name cannot overflow NAME_MAX."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    # v6.0.1: 'Author - Title' with no author came as ' - Title' (Shelfmark's naming): no leading
    # or trailing separator in a file or title name
    name = re.sub(r"^[\s\-\u2013\u2014_.]+|[\s\-\u2013\u2014_]+$", "", name)
    raw = name.encode("utf-8")
    if len(raw) > max_bytes:
        name = raw[:max_bytes].decode("utf-8", "ignore").strip(" .")
    return name or "book"

def _owner_tag(owner):
    return f"{config.OWNER_PREFIX}{owner}"

NAME_MAX_TOTAL = 255      # Linux NAME_MAX, in BYTES
EXT_ROOM = 12             # '.epub' ... '.crdownload'-sized headroom for the extension

def _unique(base, owner, rid=None):
    """Two users importing the same title within CWA's pickup window must not overwrite each
    other's file in /ingest; CWA reads the metadata from the file, not the name.

    The suffix is what identifies the row (reconcile_imports looks for it), so it is never
    cut: the BASE is re-truncated to whatever is left of NAME_MAX. A long Cyrillic author plus
    a long title used to build a 378-byte name and fail every retry with a raw ENAMETOOLONG."""
    suffix = f" [{_safe(owner, 64)}-{rid if rid is not None else uuid4().hex[:6]}]"
    room = NAME_MAX_TOTAL - len(suffix.encode("utf-8")) - EXT_ROOM
    return _safe(base, max(16, room)) + suffix

def _cwa_user(owner):
    try:
        return cwa.get_user(owner)
    except cwa.CwaError:
        return None

def _is_admin(owner):
    u = _cwa_user(owner)
    return bool(u and (u.get("role") or 0) & cwa.ROLE_ADMIN)

# ---- outbound trust boundary --------------------------------------------------------------
CGNAT = ipaddress.ip_network("100.64.0.0/10")     # Tailscale addresses live here
REDIRECTS = (301, 302, 303, 307, 308)

def _trusted_netlocs():
    """The admin's own catalog / Gutenberg mirror may legitimately sit on the tailnet or LAN."""
    import catalogs
    return ({urlsplit(u).netloc.lower() for u in (config.MYCATALOG_URL, config.GUTENBERG_MIRROR) if u}
            | catalogs.trusted_netlocs()) - {""}

def _check_target(url):
    """Refuse URLs whose host resolves to loopback, private, link-local, CGNAT, multicast or
    reserved space: from the host network those are Caddy's admin API, qBittorrent, ABS, the
    Tailscale local API, cloud metadata... Called before EVERY hop, redirects included."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"unsupported download URL: {url[:80]}")
    if p.netloc.lower() in _trusted_netlocs():
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError) as e:
        raise ValueError(f"cannot resolve {p.hostname}: {e}")
    for _fam, _t, _pr, _c, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or ip in CGNAT):
            # the user gets a plain refusal; WHICH internal address it resolved to is an
            # infrastructure detail and goes to the log and the audit trail only
            log.warning("refused %s: resolves to the non-public address %s", p.hostname, ip)
            try:
                db.audit("download_refused", None, None, f"{p.hostname} resolves to {ip}")
            except Exception:
                pass
            raise ValueError("that address is not allowed (it is not a public download address)")

def _auth_for(req, url):
    """Catalog credentials go to the configured catalog origin only — never to a host the
    request form supplied or a redirect pointed at."""
    import catalogs
    cat = catalogs.get((req or {}).get("source"))
    if not cat or not cat["user"]:
        return None
    c, p = urlsplit(cat["url"]), urlsplit(url)
    if c.netloc and (p.scheme, p.netloc.lower()) == (c.scheme, c.netloc.lower()):
        return (cat["user"], cat["password"])
    return None

def _limit_for(kind):
    mb = {"audio": config.MAX_AUDIO_MB, "comic": config.MAX_COMIC_MB}.get(kind, config.MAX_EBOOK_MB)
    return mb * 1024 * 1024

def _fetch(url, dest, req=None):
    """One download attempt: check each hop's target, follow at most 5 redirects by hand
    (re-checking every Location), enforce the size cap on Content-Length and on the bytes
    actually streamed, and make sure the disk has room (2 GiB headroom) before starting."""
    limit = _limit_for((req or {}).get("kind"))
    for _ in range(6):
        _beat("queue")
        _check_target(url)
        with requests.get(url, headers=UA, stream=True, timeout=90, auth=_auth_for(req, url),
                          allow_redirects=False) as r:
            if r.status_code in REDIRECTS and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            r.raise_for_status()
            cl = r.headers.get("Content-Length", "")
            if cl.isdigit():
                if int(cl) > limit:
                    raise ValueError(f"file is {int(cl) >> 20} MB; the limit is {limit >> 20} MB")
                if shutil.disk_usage(os.path.dirname(dest) or ".").free - 2 * 1024 ** 3 < int(cl):
                    raise ValueError("not enough free disk space for this download")
            n = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    n += len(chunk)
                    if n > limit:
                        raise ValueError(f"download exceeded {limit >> 20} MB")
                    f.write(chunk)
                    _beat("queue")
        return
    raise ValueError("too many redirects")

DOWNLOADING = "downloading"     # the file is being fetched from the source right now

def _final_http(e):
    """A 4xx answer means the source will keep saying no (gone, private, paywalled): retrying
    three times only delays the honest error. 5xx / timeouts are worth another attempt."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    return bool(code and 400 <= code < 500)

def _friendly(e):
    """What a family member should read instead of a requests exception repr."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if code == 404 or code == 410:
        return "that file is no longer available at the source (404)"
    if code in (401, 403):
        return "the source refused the download (it is not freely available)"
    if code and code >= 500:
        return f"the source had an error ({code}); try again later"
    if isinstance(e, requests.Timeout):
        return "the source did not answer in time; try again later"
    if isinstance(e, requests.ConnectionError):
        return "the source could not be reached; try again later"
    return str(e)[:200]

def verify_download(path, req):
    """Check a downloaded file against what the SOURCE said it would be. Returns a list of
    reasons it does NOT match; empty means it does (or that there was nothing to check).

    This is the half Readarr got wrong and the half that matters. Its scorer treated a MISSING
    identifier as weak evidence of a match (0.1) against a WRONG one (10.0), so absent evidence
    read as confidence and a 40 KB sample could satisfy a request for a 452-page book.
    Here, evidence that is absent produces no verdict at all — only evidence that is present
    and DISAGREES produces a reject."""
    reasons = []
    if not req:
        return reasons
    try:
        actual = os.path.getsize(path)
    except OSError:
        return ["the downloaded file could not be read"]
    want = req.get("expect_size")
    if want and actual != want:
        # a hard mismatch: archive.org states the exact byte count of the file it serves
        reasons.append(f"size is {actual} bytes, the source said {want}")
    for algo in ("sha1", "md5"):
        want_hash = req.get(f"expect_{algo}")
        if not want_hash:
            continue
        h = hashlib.new(algo)
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    _beat("queue")           # a large file must not look like a dead loop
        except OSError as e:
            reasons.append(f"could not checksum the file ({e.strerror or e})")
            break
        if h.hexdigest().lower() != str(want_hash).lower():
            reasons.append(f"{algo} does not match what the source published")
        break                                 # one strong hash is enough; sha1 is preferred
    return reasons

def _download(url, dest, req=None, rid=None, attempts=3):
    """Download with retry + exponential backoff. Sets a 'downloading' state so the user sees
    what is happening, and a 'retrying' state between attempts so a transient network blip
    doesn't fail the request outright. Policy refusals (ValueError: bad target, too large) and
    4xx answers are final and not retried."""
    last = None
    if rid is not None:
        db.set_status(rid, DOWNLOADING, "downloading from the source")
    for attempt in range(1, attempts + 1):
        try:
            _fetch(url, dest, req)
            return
        except ValueError:
            raise
        except Exception as e:
            last = e
            if _final_http(e):
                raise ValueError(_friendly(e))
            if attempt < attempts:
                if rid is not None:
                    db.set_status(rid, "retrying", f"attempt {attempt} failed ({_friendly(e)[:80]}); retrying")
                time.sleep(2 ** attempt)          # 2s, 4s, ...
    raise ValueError(_friendly(last))

# ---- ebooks: stage, tag, place --------------------------------------------------------------
_TAGGERS = {"epub": add_owner_tag, "pdf": add_owner_tag_pdf, "cbz": add_owner_tag_cbz}

def _tag_or_fail(part, owner, ext, title=None, author=None, seen=None):
    """Tag the staged copy. Admins import untagged when that fails (they see everything
    anyway); for an isolated user an untagged import would be invisible to them and owned by
    nobody, so the request fails instead of silently importing.

    `seen` is an optional dict the taggers fill with what the file says about itself (title,
    author, language, identifiers). The portal otherwise records the FILENAME as the title and
    "" as the author for every dropbox, Shelfmark, qBittorrent and mailed-in arrival."""
    seen = {} if seen is None else seen
    tag = _owner_tag(owner)
    try:
        if ext == "epub":
            found = add_owner_tag(part, tag)
        elif ext == "pdf":
            if os.path.getsize(part) > config.MAX_PDF_MB * 1024 * 1024:
                raise ValueError(f"PDF is {os.path.getsize(part) >> 20} MB: too large to tag safely "
                                 f"(MAX_PDF_MB={config.MAX_PDF_MB})")
            found = add_owner_tag_pdf(part, tag, title=title, author=author)
        else:
            found = _TAGGERS[ext](part, tag, title=title)
        # what the FILE says about itself, for the caller to record. Never fatal.
        if isinstance(found, dict):
            seen.update({k: v for k, v in found.items() if v})
        return f"tagged {tag}"
    except Exception as e:
        if _is_admin(owner):
            return f"tag skipped: {str(e)[:120]}"
        raise RuntimeError(f"could not embed owner tag: {str(e)[:150]}")

DRM_NOTE = "looks DRM-protected; may not open"

def _drm_note(path, ext):
    """An Adobe-DRM EPUB imports like any other and then refuses to open. Say so up front:
    META-INF/rights.xml, or an encryption.xml that encrypts something other than fonts
    (font obfuscation is normal and harmless)."""
    if ext != "epub":
        return ""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "META-INF/rights.xml" in names:
                return f"; {DRM_NOTE}"
            if "META-INF/encryption.xml" in names:
                enc = z.read("META-INF/encryption.xml").decode("utf-8", "ignore").lower()
                if "embedding" not in enc and "idpf" not in enc and "ocf" not in enc:
                    return f"; {DRM_NOTE}"
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return ""

def _atomic_ingest(src, owner, final_base, ext, rid=None, title=None, author=None):
    """Copy into ingest under a '.part' name (CWA ignores it), tag in place, rename to the
    real extension (atomic within the ingest mount). EPUB/PDF/CBZ carry the owner tag;
    mobi/azw3/fb2/txt cannot and are placed raw with a 'needs-tag' outcome. `title`/`author`
    fill in a PDF/CBZ that has no metadata of its own (default: the file-name base)."""
    part = os.path.join(config.INGEST_DIR, uuid4().hex + ".part")
    try:
        # inside the try: an ENOSPC here used to leave a full-size orphan .part holding the
        # last free bytes of the disk, which nothing reclaimed for 24 h (disk-watch), was
        # filtered out of every listing, and kept the disk watchdog latched at 'paused'
        shutil.copyfile(src, part)
        if ext in ("epub", "cbz"):
            # before anything opens it, and for admins too: an absurd archive OOM-kills the
            # container, and "tag skipped" would hand it to Calibre-Web to die on instead
            _zip_ok(part, f"this {ext.upper()}", TAG_MAX_ZIP_MEMBERS)
        if ext in _TAGGERS:
            seen = {}
            note = _tag_or_fail(part, owner, ext, title=title or final_base, author=author, seen=seen)
            # the file's own title/author/identifiers, recorded against the request. For a
            # dropbox or Shelfmark arrival this replaces "the filename" as the only evidence.
            db.set_file_meta(rid, seen)
            fam = _family_copy(owner, rid, seen, src, ext)
            if fam:                            # before anything is mailed or imported
                os.remove(part)
                return fam
            if ext in ("epub", "pdf"):
                note += _drm_note(part, ext)
                # before CWA consumes the file; Amazon takes EPUB and PDF by mail
                note += _auto_kindle(owner, part, f"{final_base}.{ext}", title=title or final_base)
            elif ext == "cbz" and cwa.kindle_fixer_on():
                # CWA's fixer rewrites the archive on import and drops the zip comment that
                # carries the tag (Calibre reads comic metadata from nowhere else). Be honest.
                note = (f"{NEEDS_TAG}: CWA's Kindle EPUB fixer is ON and strips the owner tag from comics "
                        f"on import; turn it off (Library -> Formats) or set {_owner_tag(owner)} in CWA")
        else:
            # the title/author the file carries: how the book is found in Calibre afterwards,
            # also when CWA converted it to EPUB and named it from its own metadata
            meta = filemeta.read(part, ext)
            db.set_file_meta(rid, meta)
            fam = _family_copy(owner, rid, meta, src, ext)
            if fam:
                os.remove(part)
                return fam
            note = f"{NEEDS_TAG}: {ext} cannot carry a tag; {AUTO_TAG_NOTE}: {_owner_tag(owner)}"
        os.rename(part, os.path.join(config.INGEST_DIR, f"{_unique(final_base, owner, rid)}.{ext}"))
    except BaseException:
        for leftover in (part, part + ".tmp"):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise
    return note

FAMILY_NOTE = "already in the library"          # v6.3: says nothing about who else has it
FAMILY_NOTE_OLD = "already in the family library"   # the wording of requests recorded before v6.3

REPLACE_NOTE = "a better copy: it replaces the library's file of this book, for everyone who has it, within a few minutes"
SHELF_PICK_EPUB = ("A better copy of this book is being looked for: request it again and choose an EPUB "
                   "result (that is the copy that replaces the one in the library).")

def _stage_better_copy(job, src, ext, rid, owner):
    """'Find a better copy' is open for this book and an EPUB arrived: hand the file, exactly
    as it came (not our owner-tagged copy: the book's tags live in Calibre), to the host job,
    which swaps it into the SAME Calibre book. True if staged."""
    if ext != "epub" or not src:
        return False
    d = os.path.join(config.STAGING_DIR, "replace")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"{job['id']}.epub")
    shutil.copyfile(src, dest + ".tmp")
    os.replace(dest + ".tmp", dest)
    if db.stage_replace(job["id"], dest, "epub", rid, owner):
        db.audit("replace_staged", owner, None, f"book {job['calibre_id']} job {job['id']}")
        return True
    os.remove(dest)
    return False

def _family_copy(owner, rid, meta, src=None, ext=None):
    """Family sharing (share.py): an arrival that is a book already in the library is not
    imported a second time. The reader gets the existing copy (their owner tag is added by the
    host job); a book they already have is simply not duplicated. While 'Find a better copy' is
    open for that book, an EPUB arrival replaces the library's file instead. None = import it
    as usual."""
    if not meta or not (meta.get("title") or meta.get("identifiers")):
        return None
    m = share.find_ebook(meta.get("title"), meta.get("author") or "", meta.get("identifiers") or ())
    if not m:
        return None
    import bookreq
    if m["book_id"] in bookreq.rejected_for(owner)[0]:
        return None                              # the copy this reader called the wrong book: import the new one
    job = db.replace_live(m["book_id"])
    if job and job["status"] == "open" and _stage_better_copy(job, src, ext, rid, owner):
        if owner in m["owners"]:
            return REPLACE_NOTE
        share.give_ebook(m, owner, rid)
        return f"{NEEDS_TAG}: {FAMILY_NOTE}, {REPLACE_NOTE}; {AUTO_TAG_NOTE}: {_owner_tag(owner)}"
    if owner in m["owners"]:
        return f"already in your library (matched by {m['how']}); this copy was not imported again"
    share.give_ebook(m, owner, rid)
    return (f"{NEEDS_TAG}: {FAMILY_NOTE} (matched by {m['how']}), this copy was not imported again; "
            f"{AUTO_TAG_NOTE}: {_owner_tag(owner)}")

def _family_audio(owner, base):
    """_family_copy for an audiobook: the arrival's name ('Author - Title', either order) is all
    there is, so both halves must agree with one Audiobookshelf item. None = add it as usual."""
    parts = [x.strip() for x in re.split(r"\s+[-\u2013\u2014]\s+", _strip_name_tail(base), maxsplit=1)]
    if len(parts) != 2 or not all(parts):
        return None
    import bookreq
    ex = bookreq.rejected_for(owner)[1]          # never the audiobook this reader called the wrong one
    m = share.find_audiobook(parts[1], parts[0], exclude=ex) or share.find_audiobook(parts[0], parts[1], exclude=ex)
    if not m:
        return None
    if owner in m["owners"]:
        return "already in your audiobooks; this copy was not added again"
    try:
        share.give_audiobook(m, owner)
    except Exception as e:                       # ABS refused the tag: add the copy as usual
        log.warning("family sharing: could not tag ABS item %s for %s: %s", m["item_id"], owner, e)
        return None
    return f"{FAMILY_NOTE}: added to your audiobooks, this copy was not added again"

def _family_request(req):
    """A portal request for a book the family already has: nothing is downloaded."""
    owner = req["owner"]
    if req["kind"] == "audio":
        m = share.find_audiobook(req.get("title"), req.get("author") or "")
        if not m:
            return None
        if owner in m["owners"]:
            return "already in your audiobooks; nothing downloaded"
        try:
            share.give_audiobook(m, owner)
        except Exception as e:                   # could not tag it: download as usual
            log.warning("family sharing: could not tag ABS item %s for %s: %s", m["item_id"], owner, e)
            return None
        return f"{FAMILY_NOTE}: added to your audiobooks, nothing downloaded"
    m = share.find_ebook(req.get("title"), req.get("author") or "")
    if not m or db.replace_live(m["book_id"]):
        return None                              # new, or a better copy is wanted: download it
    if owner in m["owners"]:
        return f"already in your library (matched by {m['how']}); nothing downloaded"
    share.give_ebook(m, owner, req["id"])
    return f"{NEEDS_TAG}: {FAMILY_NOTE} (matched by {m['how']}), nothing downloaded; {AUTO_TAG_NOTE}: {_owner_tag(owner)}"

SHELF_SHARED = ("Added to your library at once, so nothing was downloaded. It appears there within a few "
                "minutes.")
SHELF_OWNED = "Already in your library, so nothing was downloaded."

def shelfmark_gate_once(now=None):
    """Every reader's Shelfmark download arrives as a request (REQUESTS_ENABLED, see
    docker-compose.yml). BEFORE anything is downloaded:
      * a book the family already has is given to the reader (share.py) and the request is
        closed with a note saying so. Shelfmark cannot mark a picked release done without
        downloading it, so it shows as 'rejected', with that note;
      * anything else is approved at once, unless APPROVALS_REQUIRED, when it waits on the
        portal's Pending card for the admin, as before.
    Admin accounts download directly in Shelfmark; their repeats are merged on arrival instead.
    Returns (shared, approved)."""
    import shelfmark_api
    if not shelfmark_api.configured():
        return 0, 0
    try:
        queue = shelfmark_api.queue_status()     # one read of the queue for both uses below
    except Exception as e:
        log.debug("could not read Shelfmark's queue: %s", e)
        queue = None
    _export_waiting(shelfmark_api, status=queue)
    _report_failed_downloads(shelfmark_api, queue)
    try:
        rows = shelfmark_api.pending(cache=False)
    except shelfmark_api.ShelfmarkError as e:
        log.warning("Shelfmark gate: %s", e)
        return 0, 0
    shared = approved = 0
    for x in rows:
        owner = x.get("requester") or ""
        told = {"owner": owner, "title": x.get("title"), "author": x.get("author"), "source": "shelfmark",
                "seq": notify.seq_id("shelf", x.get("id"))}
        def waiting():                           # left for the admin: say so once
            if db.first_notice(f"shelfmark-pending-{x.get('id')}"):
                notify.admin("requested", dict(told, status="pending"))
        try:
            if not owner or cwa.get_user(owner) is None:
                waiting()
                continue                         # not a library account: the admin decides
        except cwa.CwaError:
            continue
        try:
            if x["kind"] == "audiobook":
                m = share.find_audiobook(x["title"], x["author"])
                if m and owner not in m["owners"]:
                    share.give_audiobook(m, owner)
            else:
                m = share.find_ebook(x["title"], x["author"], share.isbn_ids(*x.get("isbns", ())))
                job = db.replace_live(m["book_id"]) if m else None
                # v6.3: only a reader who has the book hears about a better copy being looked for; anyone
                # else simply gets the library's copy (never told someone else is replacing it)
                if job and job["status"] == "open" and owner in m["owners"]:
                    # 'Find a better copy': an EPUB release goes through (and replaces the file on
                    # arrival); anything else is sent back asking for an EPUB
                    if (x.get("format") or "").lower() == "epub":
                        if not config.APPROVALS_REQUIRED:
                            shelfmark_api.decide(x["id"], True, "")
                            approved += 1
                            notify.admin("requested", dict(told, status="queued", detail="a better copy of a book already in the library"))
                        else:
                            waiting()
                    else:
                        shelfmark_api.decide(x["id"], False, SHELF_PICK_EPUB)
                    continue
                if m and owner not in m["owners"]:
                    rid = db.add(owner, {"kind": "ebook", "source": "shelfmark", "title": x["title"],
                                         "author": x["author"], "download_url": "local"}, status=NEEDS_TAG)
                    db.set_status(rid, NEEDS_TAG, f"{NEEDS_TAG}: {FAMILY_NOTE} (matched by {m['how']}), "
                                                  f"nothing downloaded; {AUTO_TAG_NOTE}: {_owner_tag(owner)}")
                    share.give_ebook(m, owner, rid, now)
            if m:
                note = SHELF_OWNED if owner in m["owners"] else SHELF_SHARED
                shelfmark_api.decide(x["id"], False, note)
                db.audit("family_share", owner, "shelfmark", f"#{x['id']} {x['title'][:80]} ({m['how']})")
                shared += 1
                if note == SHELF_SHARED:
                    notify.admin("shared", dict(told, status="shared", detail=f"matched by {m['how']}"))
            elif not config.APPROVALS_REQUIRED:
                shelfmark_api.decide(x["id"], True, "")
                approved += 1
                notify.admin("requested", dict(told, status="queued"))
            else:
                waiting()
        except Exception as e:                   # one bad row never blocks the others
            log.warning("Shelfmark gate: request #%s: %s", x.get("id"), e)
    return shared, approved

WAITING_FILE = os.path.join(os.path.dirname(config.STATE_DB), "seedbox-wanted.json")
_WAITING = {"last": None, "at": 0.0}

def _report_failed_downloads(shelfmark_api, queue):
    """A Shelfmark download that failed (a source that broke, a stall it cancelled) is otherwise
    only on the reader's own Shelfmark page: tell the admin, once per download."""
    n = 0
    try:
        for f in (shelfmark_api.failed(queue) if queue else []):
            if db.first_notice(f"shelfmark-error-{f['task_id']}"):
                notify.admin("error", {"owner": f["user"] or "?", "title": f["title"], "author": f["author"],
                                       "source": "shelfmark", "status": "error",
                                       "detail": f["message"] or "the download failed in Shelfmark",
                                       "seq": notify.seq_id("shelf-task", f["task_id"])})
                n += 1
    except Exception as e:                       # never let this block the gate
        log.warning("could not report Shelfmark's failed downloads: %s", e)
    return n

def _export_waiting(shelfmark_api, now=None, status=None):
    """The seedbox job (scripts/seedbox-fetch.py) drops its synced copy of a download after a
    week and tells Syncthing to ignore it. Asked for again, the torrent is already complete on
    the seedbox, so Shelfmark just waits for the file: this list (librarian/state, which the host
    reads) is what makes the job bring that copy back through Syncthing."""
    now = now or time.time()
    try:
        titles = shelfmark_api.waiting_for_files() if status is None else shelfmark_api.waiting_for_files(status)
    except Exception as e:                       # never let this block the gate
        log.debug("could not read Shelfmark's queue: %s", e)
        return
    if titles == _WAITING["last"] and now - _WAITING["at"] < 60:
        return
    tmp = WAITING_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": int(now), "waiting": titles}, f)
        os.replace(tmp, WAITING_FILE)
        _WAITING.update(last=titles, at=now)
    except OSError as e:
        log.warning("could not write %s: %s", WAITING_FILE, e)

# ---- books no reader has any more ---------------------------------------------------------------
RELEASE_EVERY = 3600
_LAST_RELEASE = [0.0]

def _owner_tags_by_book():
    c = library._conn()
    try:
        out = {}
        for bid, name in c.execute("SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag "
                                   "WHERE t.name LIKE ?", (config.OWNER_PREFIX + "%",)):
            out.setdefault(bid, set()).add(name)
        return out
    finally:
        c.close()


def release_kobo_waits(now=None):
    """v6.1: a reader's removal waits (their owner tag stays) until their Kobo has synced and been
    told to delete the book (cwa.kobo_remove); then it goes ahead. A Kobo that never syncs holds
    it at most share.KOBO_REMOVE_WAIT (queue_untag's not_before). Returns how many went ahead."""
    n = 0
    for w in db.kobo_waits():
        try:
            if cwa.kobo_removed(w["owner"], w["calibre_id"], w["kobo_wait"]):
                db.kobo_wait_over(w["id"], now)
                n += 1
        except Exception as e:                   # app.db busy: next pass
            log.debug("Kobo removal check for book %s: %s", w["calibre_id"], e)
    return n

def reconcile_audio_releases(now=None):
    """v6.1: audiobooks nobody has any more (their last reader removed them) are deleted from the
    server after LIBRARY_RELEASE_DAYS, like books; one a reader has again (asked for it) is kept.
    Returns the number deleted."""
    days = config.LIBRARY_RELEASE_DAYS
    if days <= 0 or not absapi.configured():
        return 0
    now = now or time.time()
    gone = 0
    for r in db.audio_releases():
        try:
            owners = absapi.item_owners(r["item_id"])
        except Exception as e:
            log.warning("audiobook release check for %s: %s", r["item_id"], e)
            continue
        if owners is None:
            db.audio_release_set(r["item_id"], "deleted", now=now)       # already gone
        elif owners:
            db.audio_release_set(r["item_id"], "kept", now=now)          # a reader has it again
        elif now - r["since"] >= days * 86400:
            try:
                absapi.delete_item(r["item_id"])
                db.audio_release_set(r["item_id"], "deleted", now=now)
                share._ABS_ITEMS["at"] = 0.0
                db.audit("audiobook_deleted", None, "worker", f"{r['item_id']} {r.get('title') or ''}"[:200])
                gone += 1
            except Exception as e:
                db.audio_release_set(r["item_id"], "failed" if (r.get("attempts") or 0) >= 2 else "waiting",
                                     error=str(e), now=now)
    return gone

def reconcile_releases(now=None):
    """Count down the books no reader has any more; hand the due ones to the host job, which
    deletes them from the VPS (their seedbox copy stays on the seedbox). A book is 'no reader's'
    when its last reader removed it (admin_cli, after the host took the tag off) or when every
    owner tag it carries names an account that no longer exists. A book that never had an owner
    (the admin's own, added in Calibre-Web) is never counted. Returns the number made due."""
    days = config.LIBRARY_RELEASE_DAYS
    if days <= 0:
        return 0
    now = now or time.time()
    try:
        names = {u["name"] for u in cwa.list_users(include_canary=True)}
        tags = _owner_tags_by_book()
    except Exception as e:
        log.warning("release check skipped: %s", e)
        return 0
    if not names:
        return 0                                 # an unreadable user list is never "nobody"
    busy = {r["calibre_id"] for r in db.pending_tag_pushes(1000)}
    tracked = {r["calibre_id"]: r for r in db.releases()}
    pre = len(config.OWNER_PREFIX)
    for bid, t in tags.items():
        if {x[pre:] for x in t} & names:
            if bid in tracked:
                db.release_keep(bid, now)        # someone has it again
                tracked.pop(bid)
        elif bid not in tracked and bid not in busy:
            db.release_note(bid, "every reader who had it has been removed", sorted(t), now)
    made = 0
    for bid, r in tracked.items():
        if r["status"] == "waiting" and now - r["since"] >= days * 86400 \
                and bid not in busy and not db.replace_live(bid):
            db.release_due(bid, now)
            made += 1
    return made

def _status_for(note):
    if note.startswith(NEEDS_TAG):
        return NEEDS_TAG
    if note.startswith("skipped:"):
        return "skipped"
    return TAGGING if TAG_WAIT_NOTE in note else "done"

def _auto_kindle(owner, path, filename, title=None):
    """If the user opted in (Devices page) and mail is configured, e-mail the tagged book
    (EPUB or PDF — the formats Amazon accepts by mail). The mail's subject is the title."""
    try:
        if not kindle.configured() or not db.get_prefs(owner)["auto_kindle"]:
            return ""
        u = cwa.get_user(owner)
        if not u or not u.get("kindle_mail"):
            return "; auto-Kindle skipped (no address on Devices page)"
        # The same daily ceiling the manual button obeys, drawn from the same counter. It
        # protects the SMTP account, not the reader — and a provider suspension would take out
        # Send-to-Kindle, the notification mails AND alert.sh's fallback channel together. A
        # bulk dropbox drop is exactly the way to hit it without anyone pressing a button.
        limit = config.KINDLE_MAX_PER_DAY
        if limit and db.audit_count("kindle_send", owner, time.time() - 86400) >= limit:
            return f"; auto-Kindle skipped ({limit} sent today, the daily limit)"
        note = kindle.send(u["kindle_mail"], path, title or filename, filename)
        db.audit("kindle_send", owner, None, f"auto: {filename}")
        return "; auto-Kindle " + note
    except Exception as e:
        return f"; auto-Kindle failed: {str(e)[:80]}"

def _park(path, owner, name, dropbox_dir=None):
    """Move a file or folder that cannot be ingested into <that dropbox>/.failed/ (hidden, so
    the watcher never retries it) where the admin can look at it. The folder the user actually
    syncs is used (it may be spelled differently from the canonical name). Returns a label."""
    box = dropbox_dir or os.path.join(config.DROPBOX_DIR, owner)
    d = os.path.join(box, ".failed")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, name)
    if os.path.isdir(dst) and not os.path.islink(dst):
        shutil.rmtree(dst)
    elif os.path.lexists(dst):
        os.remove(dst)
    shutil.move(path, dst)
    return f"dropbox/{os.path.basename(box)}/.failed/{name}"

def _publish(tmp, dropbox_dir, name):
    """Give the finished temp file its name WITHOUT replacing a file of the same name that is
    still waiting for pickup (two uploads of 'book.epub', two attachments with one name):
    the next free 'name (2).ext' is used instead. A hard link is an atomic 'create only if
    absent'; a planted symlink at the name is replaced (never followed)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for i in range(1, 1000):
        final = os.path.join(dropbox_dir, name if i == 1 else f"{stem} ({i}){dot}{ext}")
        if os.path.islink(final):
            os.replace(tmp, final)
            return final
        try:
            os.link(tmp, final)
        except FileExistsError:
            continue
        except OSError:                      # a filesystem without hard links
            if os.path.lexists(final):
                continue
            os.replace(tmp, final)
            return final
        os.remove(tmp)
        return final
    raise OSError(f"too many files named {name} waiting in the dropbox")

def place_in_dropbox(dropbox_dir, name, write):
    """Create a NEW file in a dropbox without ever writing through a planted symlink: the
    temp name is unpredictable, O_EXCL|O_NOFOLLOW refuses to follow a link, and the final
    name never overwrites anything but a link. `write(fileobj)` fills it. Returns the path."""
    os.makedirs(dropbox_dir, exist_ok=True)
    tmp = os.path.join(dropbox_dir, f".{uuid4().hex}.uploading")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
        return _publish(tmp, dropbox_dir, name)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def _refuse_links(path):
    """Symbolic links in a dropbox are never followed: they could point at app.db, /etc or
    another user's folder and would be copied into the library under this user's name."""
    if os.path.islink(path):
        raise ValueError("symbolic links are not accepted in a dropbox")
    if os.path.isdir(path):
        for root, dirs, names in os.walk(path):
            for n in dirs + names:
                if os.path.islink(os.path.join(root, n)):
                    raise ValueError(f"symbolic link inside the folder is not accepted: {n}")

# ---- audiobooks -------------------------------------------------------------------------------
MAX_ZIP_MEMBERS, MAX_ZIP_UNPACKED = 2000, 8 * 1024 ** 3   # zip-bomb guard for audiobook archives

def _zip_ok(path, what="this archive", max_members=MAX_ZIP_MEMBERS):
    """Refuse an absurd archive BEFORE zipfile.ZipFile() parses (and materialises) the whole
    central directory — _safe_extract's member count runs far too late to save the container.
    tagger owns the End-Of-Central-Directory reader; this is its ValueError face.

    Never pass a limit stricter than the guard this front-runs, or files that used to import
    are refused with a reason that is not true: audiobook archives are held to the tighter
    count here, EPUB and CBZ to tagger's, which is what actually gates their tagging."""
    try:
        precheck_zip(path, what, max_members)
    except TagError as e:
        raise ValueError(str(e))

def _safe_extract(zf, dest):
    """Extract an audiobook zip without path traversal, symlinks or absurd expansion."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > MAX_ZIP_UNPACKED:
        raise ValueError("archive too large")
    root = os.path.realpath(dest)
    for info in infos:
        name = info.filename
        if name.endswith("/") or not name or (info.external_attr >> 16) & 0o120000 == 0o120000:
            continue
        target = os.path.realpath(os.path.join(dest, name))
        if not target.startswith(root + os.sep):
            raise ValueError(f"unsafe path in archive: {name}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)

# Sniffing recognises the CONTAINER, not the codec: ADTS AAC shares MPEG frame sync with MP3
# and Opus rides in an Ogg container, so an .aac was renamed .mp3 and an .opus .ogg — wrong
# names in Audiobookshelf for files that were perfectly well named. A name that is already a
# valid extension for the sniffed container is kept; only an unrelated one (download.bin, an
# m4b someone called .zip) is corrected.
SNIFF_ALIASES = {"mp3": ("mp3", "aac"), "ogg": ("ogg", "opus", "oga", "spx"),
                 "m4a": ("m4a", "m4b", "aac", "mp4"), "m4b": ("m4b", "m4a"),
                 "flac": ("flac",), "wav": ("wav",)}

TAG_WAIT_NOTE = "waiting for Audiobookshelf to index it to tag"

def _finish_audio(final, owner, rid=None):
    """After an audiobook folder is in place: scan, and queue a PERSISTENT tag job that the
    housekeeping loop retries until ABS has indexed the item and the owner tag is set (the
    request stays 'tagging' until then). Without an ABS token it is a 'needs-tag' import."""
    scan = absapi.trigger_scan()
    if absapi.configured():
        db.add_tag_job(rid, os.path.basename(final), owner)
        return f"{scan}; {TAG_WAIT_NOTE} {_owner_tag(owner)}"
    return f"{NEEDS_TAG}: no ABS API token, set tag {_owner_tag(owner)} in ABS by hand ({scan})"

def _audio_final(owner, base):
    final = os.path.join(config.AUDIO_DIR, f"{_safe(owner)} - {base}")
    if os.path.exists(final):
        final += "-" + uuid4().hex[:6]
    return final

def _tree_size(path):
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total

def _audio_duplicate(owner, base, size):
    """The same audiobook a second time: a folder of that exact name already exists and holds
    exactly as many bytes. Better than silently creating 'Title' and 'Title-fbaadd'."""
    final = os.path.join(config.AUDIO_DIR, f"{_safe(owner)} - {base}")
    return bool(size) and os.path.isdir(final) and _tree_size(final) == size

NO_AUDIO = ("this file has no playable audio in it, so it is not an audiobook "
            "(an audiobook is one M4B/MP3 or a zip of them)")

def _ingest_extracted_ebooks(root, owner, rid=None):
    """An archive that turned out to hold BOOKS (an EPUB is a zip too) goes to the ebook
    ingest, one request note per file, instead of being exploded into the audiobook library."""
    notes, count = [], 0
    for r, _dirs, names in sorted(os.walk(root)):
        for n in sorted(names):
            e = _ext(n)
            if n.startswith(".") or e not in config.EBOOK_EXTS or e == "cbr":
                continue
            stem = n.rsplit(".", 1)[0] if "." in n else n
            notes.append(_atomic_ingest(os.path.join(r, n), owner, _safe(stem), e, rid))
            count += 1
            _beat("dropbox")
    if not count:
        raise ValueError(NO_AUDIO)
    head = f"this archive holds {count} book(s), not audio: imported into your books"
    if any(x.startswith(NEEDS_TAG) for x in notes):
        return f"{NEEDS_TAG}: {head}; {notes[0]}"
    return f"{head}; {notes[0]}"

def _place_audio_file(src, owner, base, rid=None):
    """Place one audiobook file or archive. The CONTENT decides: an archive of ebooks is sent
    to the ebook ingest, and anything without a playable audio file is refused (parked) rather
    than dumped into Audiobookshelf where it sits 'tagging' for hours."""
    inc = os.path.join(config.AUDIO_DIR, ".incoming-" + uuid4().hex)
    os.makedirs(inc, exist_ok=True)
    try:
        if zipfile.is_zipfile(src):
            _zip_ok(src)
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, inc)
            kind = _classify_dir(inc)
            if kind == "ebooks":
                note = _ingest_extracted_ebooks(inc, owner, rid)
                shutil.rmtree(inc, ignore_errors=True)
                return note
            if kind != "audio":
                raise ValueError(NO_AUDIO)
        else:
            # name the copy for what it IS: a download temp file ('download.bin') or an m4b
            # someone named .zip would otherwise look like "no audio here"
            name = os.path.basename(src)
            sniffed = _sniff_ext(src)
            if sniffed in AUDIO_SNIFFED and _ext(name) not in SNIFF_ALIASES.get(sniffed, (sniffed,)):
                name = f"{name.rsplit('.', 1)[0] if '.' in name else name}.{sniffed}"
            shutil.copyfile(src, os.path.join(inc, name))
            if _classify_dir(inc) != "audio":
                raise ValueError(NO_AUDIO)
        _beat("dropbox")
        if _audio_duplicate(owner, base, _tree_size(inc)):
            raise ValueError("this audiobook is already in your audiobooks (same name and size)")
        fam = _family_audio(owner, base)
        if fam:
            shutil.rmtree(inc, ignore_errors=True)
            return fam
        final = _audio_final(owner, base)
        os.rename(inc, final)
    except Exception:
        shutil.rmtree(inc, ignore_errors=True)
        raise
    return _finish_audio(final, owner, rid)

def _copy_beating(src, dst, *, follow_symlinks=True):
    _beat("dropbox")                             # a multi-GB cross-device copy must not look like a dead loop
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)

def _expand_audio_zips(d):
    """A dropped folder often carries the audiobook as one zip (LibriVox, a Shelfmark grab).
    Audiobookshelf cannot read a zip, so each audio archive is unpacked where it lies and the
    archive itself removed — otherwise the folder lands in the library holding nothing
    playable, ABS indexes no item and the tag job waits 24 h for something that never appears.

    Only ever called on our own staging copy, and the archive is NOT deleted here: it may be
    the reader's only copy, so it goes once the folder is safely in the library. Returns
    [(archive, [files it produced])] so a failure in between can be undone exactly, leaving
    the folder byte-identical to what was dropped."""
    expanded = []
    zips = [os.path.join(r, n) for r, _dirs, names in os.walk(d) for n in names if _ext(n) == "zip"]
    for p in zips:
        if _zip_kind(p) != "audio-zip":
            continue
        _zip_ok(p)
        here = os.path.dirname(p)
        before = set(_files_under(here))
        with zipfile.ZipFile(p) as zf:
            _safe_extract(zf, here)
        expanded.append((p, sorted(set(_files_under(here)) - before)))
        _beat("dropbox")
    return expanded

def _files_under(d):
    return [os.path.join(r, n) for r, _dirs, names in os.walk(d) for n in names]

def _place_audio_dir(src_dir, owner, base, rid=None):
    """A folder of audio files dropped in a dropbox (Shelfmark, rsync) becomes one audiobook.

    The folder is taken out of the dropbox into our own staging name first, and everything
    that can fail happens there. On any failure it goes back exactly as it was: nothing the
    reader dropped is lost, and its fingerprint still matches, so _handle parks it with the
    real reason instead of 'the file was still being written'."""
    final = _audio_final(owner, base)
    inc = os.path.join(config.AUDIO_DIR, ".incoming-" + uuid4().hex)
    moved = False
    try:
        os.rename(src_dir, inc)                  # same filesystem: instant and atomic
        moved = True
    except OSError:
        shutil.copytree(src_dir, inc, copy_function=_copy_beating)
    expanded = []
    fam = None
    try:
        expanded = _expand_audio_zips(inc)
        if _audio_duplicate(owner, base, _tree_size(inc)):
            raise ValueError("this audiobook is already in your audiobooks (same name and size)")
        fam = _family_audio(owner, base)
        if not fam:
            os.rename(inc, final)
    except BaseException:
        if moved:
            for _archive, made in expanded:      # undo the unpacking, keep the archive
                for f in made:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            try:
                os.rename(inc, src_dir)          # the reader's folder, exactly as they made it
            except OSError:
                shutil.rmtree(inc, ignore_errors=True)
        else:
            shutil.rmtree(inc, ignore_errors=True)
        raise
    if fam:                                      # the family already has it: this copy goes
        shutil.rmtree(inc, ignore_errors=True)
        if not moved:
            shutil.rmtree(src_dir, ignore_errors=True)
        return fam
    # in the library now, and Audiobookshelf cannot read a zip: the archive has done its job
    for archive, _made in expanded:
        try:
            os.remove(os.path.join(final, os.path.relpath(archive, inc)))
        except OSError:
            pass
    if not moved:
        shutil.rmtree(src_dir)
    return _finish_audio(final, owner, rid)

def ingest_local_file(path, owner, rid=None):
    """Ingest one file a user legally owns, mapping it to that user. EPUB, PDF and CBZ are
    owner-tagged before import; mobi/azw3/fb2/txt import untagged ('needs-tag'); a comic (CBZ,
    CBR, CB7) goes through comics.py first: repacked as CBZ, matched to the reader's request,
    its series and number written into it (docs/COMICS.md)."""
    name = os.path.basename(path)
    stem, _, ext = name.rpartition(".") if "." in name else (name, "", "")
    ext, base = ext.lower(), _safe(stem)
    sniffed = _sniff_ext(path)
    if ext in config.COMIC_EXTS or (not ext and sniffed == "cbr"):
        if os.path.getsize(path) > _limit_for("comic"):     # v6.0.1: comics have their own cap
            raise ValueError(f"file is {os.path.getsize(path) >> 20} MB; the limit for comics is "
                             f"{_limit_for('comic') >> 20} MB (MAX_COMIC_MB)")
        return _ingest_comic(path, owner, rid)
    if ext in config.EBOOK_EXTS and sniffed in AUDIO_SNIFFED:
        ext = sniffed          # an .m4b renamed .epub (or mailed as one) is still an audiobook
    elif ext not in config.AUDIO_EXTS and ext not in config.EBOOK_EXTS and sniffed:
        ext = _PARK_EXT.get(sniffed, sniffed)    # unknown/missing extension: trust the bytes
    if ext in config.AUDIO_EXTS or ext in config.EBOOK_EXTS:
        size, limit = os.path.getsize(path), _limit_for("audio" if ext in config.AUDIO_EXTS else "ebook")
        if size > limit:
            raise ValueError(f"file is {size >> 20} MB; the limit for this kind is {limit >> 20} MB "
                             f"(MAX_{'AUDIO' if ext in config.AUDIO_EXTS else 'EBOOK'}_MB)")
    if ext in config.AUDIO_EXTS:
        return _place_audio_file(path, owner, base, rid)
    if ext in config.EBOOK_EXTS:
        return _atomic_ingest(path, owner, base, ext, rid)
    raise ValueError(f"unsupported file type '.{ext}' (kept in .failed/, not deleted)")

def _ingest_comic(path, owner, rid):
    work = tempfile.mkdtemp(dir=os.path.dirname(_tmpdir()))
    try:
        got = comics.prepare_arrival(path, owner, work)
        if got[0] == "skip":
            return got[1]
        cbz, base, req = got
        note = _atomic_ingest(cbz, owner, _safe(base), "cbz", rid, title=base)
        comics.arrived(req, note)
        return note
    except comics.ComicError as e:
        raise ValueError(str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)

# ---- dropbox watcher ------------------------------------------------------------------------
SETTLE_SECONDS = 12   # a file must be untouched this long before it is picked up
PARTIAL = (".part", ".tmp", ".crdownload", ".uploading")
# files that may sit next to books (Calibre library folders, audiobook rips) and mean nothing
SIDECAR_EXTS = ("jpg", "jpeg", "png", "gif", "webp", "bmp", "opf", "nfo", "sfv", "md5", "url",
                "cue", "log", "m3u", "m3u8", "json", "xml", "ds_store")
_WARNED = set()

def _known_user(dirname):
    """Only an existing CWA account's dropbox is scanned; returns the canonical user name
    (the tag must match Allowed Tags exactly) or None for a stray directory."""
    u = _cwa_user(dirname) if cwa._valid_name(dirname) else None
    if not u and dirname not in _WARNED:
        _WARNED.add(dirname)
        log.warning("dropbox/%s is not an existing user's folder; ignored", dirname)
    return u["name"] if u else None

def _settled_dir(path, now):
    """True when the folder has files and none of them (nor a partial download) changed
    within SETTLE_SECONDS: a source is still writing otherwise."""
    newest, files = 0, 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            if n.lower().endswith(PARTIAL):
                return False
            files += 1
            newest = max(newest, os.path.getmtime(os.path.join(root, n)))
    return files > 0 and now - newest >= SETTLE_SECONDS

def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def _fingerprint(p):
    """(size, mtime) of a file, or (total size, newest mtime) of a folder: tells a re-dropped
    file with an old name apart from the one that failed before."""
    if os.path.isdir(p) and not os.path.islink(p):
        size, newest = 0, 0.0
        for root, _dirs, names in os.walk(p):
            for n in names:
                st = os.lstat(os.path.join(root, n))
                size += st.st_size
                newest = max(newest, st.st_mtime)
        return size, newest
    st = os.lstat(p)
    return st.st_size, st.st_mtime

def _classify_dir(path):
    """What a settled dropbox folder is: 'audio' (audio files and no ebook files: one
    audiobook), 'ebooks' (ebook files and no audio: each is imported on its own), 'mixed' or
    'nothing' (parked for the admin). A .txt next to audio is read as notes, not a book."""
    audio = ebooks = texts = companions = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            e = _ext(n)
            if e == "zip":
                # a folder whose only content is an audiobook ZIP (the usual LibriVox shape)
                # counted as neither audio nor ebook and was parked as 'nothing'
                if _zip_kind(os.path.join(root, n)) == "audio-zip":
                    audio += 1
            elif e in config.AUDIO_EXTS:
                audio += 1
            elif e == "txt":
                texts += 1
            elif e in config.EBOOK_EXTS:
                ebooks += 1
                companions += e in ("pdf", "epub")
    if audio:
        # v6.0: an audiobook release often carries its companion PDF (the Audible supplement) or
        # an EPUB of the text: one or two of those beside the audio are part of the audiobook
        if ebooks and ebooks == companions and companions <= 2:
            return "audio"
        return "mixed" if ebooks else "audio"
    return "ebooks" if ebooks or texts else "nothing"

def _decide(owner, title, fp):
    """'go', 'skip' or 'park' for a dropbox entry, from its previous row (if any):
      - no earlier failure                         -> go
      - failed and was parked in .failed/          -> go (what is here now is a NEW file)
      - failed, parking failed, same size+mtime    -> skip (one error row, no flood)
      - interrupted by restarts twice, same file   -> park (it is what kills the portal)"""
    last = db.last_for(owner, title, "dropbox")
    if not last or last["status"] != "error":
        return "go"
    detail = last.get("detail") or ""
    if detail == db.INTERRUPTED:
        return "park" if db.interrupted_count(owner, title, "dropbox", *fp) >= 2 else "go"
    if ".failed/" in detail:
        return "go"
    if last.get("src_size") is not None:
        return "skip" if (last["src_size"], last["src_mtime"]) == tuple(fp) else "go"
    return "skip" if (last.get("updated") or 0) >= fp[1] else "go"     # rows from before fingerprints

def _changed_since(p, fp):
    """True when the file/folder is still there but no longer the one we fingerprinted: its
    writer paused (12 s of stillness is not always the end of an scp) and finished meanwhile."""
    try:
        return os.path.lexists(p) and tuple(_fingerprint(p)) != tuple(fp)
    except OSError:
        return False

def _handle(p, owner, title, kind, box, park_name, fn):
    """One dropbox entry -> one request row. `fn(rid)` ingests it and returns the note; on
    any failure the entry is parked under <box>/.failed/<park_name>. Returns 1 if handled."""
    fp = _fingerprint(p)
    what = _decide(owner, title, fp)
    if what == "skip":
        return 0
    rid = db.add(owner, {"kind": kind, "source": "dropbox", "title": title, "author": "",
                         "download_url": "local", "src_size": fp[0], "src_mtime": fp[1]}, status="importing")
    try:
        if what == "park":
            raise RuntimeError("a restart interrupted this import twice (too large for the portal's memory?)")
        note = fn(rid)
        _finish(rid, _status_for(note), note)
        # the retry worked: the old 'interrupted by restart' row for the same file is noise
        db.dismiss_interrupted(owner, title, "dropbox", rid)
    except Exception as e:
        log.exception("dropbox/%s/%s failed", owner, title)
        if _changed_since(p, fp):
            # the writer was still going (a paused scp/SMB copy): do not bury the finished file
            # in .failed/ — leave it where it is and let the next pass import the complete file
            _finish(rid, "error", f"{str(e)[:200]} (the file was still being written; "
                                  f"it is left in your folder and picked up again when it is complete)")
            _beat("dropbox")
            return 1
        try:
            where = "moved to " + _park(p, owner, park_name, box)
        except Exception as e2:
            where = f"could not move it aside: {str(e2)[:60]}"
        _finish(rid, "error", f"{str(e)[:200]} ({where})")
    _beat("dropbox")
    return 1

def _ingest_file_entry(p, owner, rid):
    _refuse_links(p)
    import bookreq
    held = bookreq.check_arrival(p, owner)      # a Get it download that is not the book: held
    if held:
        return held
    note = ingest_local_file(p, owner, rid)
    os.remove(p)
    return note

def _folder_leftovers(path):
    """Files left in a folder after its ebooks were imported that are not mere sidecars."""
    return [os.path.relpath(os.path.join(r, n), path) for r, _d, ns in os.walk(path) for n in ns
            if _ext(n) not in SIDECAR_EXTS and not n.startswith(".")]

def _opf_sidecar(book_path):
    """The Calibre-style sidecar next to a book — '<name>.opf' or the folder's 'metadata.opf'
    (a Calibre library export keeps one per book folder). Used to be deleted unread as noise;
    it is often the ONLY identification a MOBI or AZW3 carries."""
    from lxml import etree
    from tagger import _read_opf_meta
    d = os.path.dirname(book_path)
    for cand in (os.path.splitext(book_path)[0] + ".opf", os.path.join(d, "metadata.opf")):
        if not os.path.isfile(cand) or os.path.islink(cand) or os.path.getsize(cand) > 2 * 1024 * 1024:
            continue
        try:
            root = etree.parse(cand, etree.XMLParser(resolve_entities=False, no_network=True)).getroot()
        except (etree.XMLSyntaxError, OSError):
            continue
        meta = root.find("{http://www.idpf.org/2007/opf}metadata")
        if meta is None:
            meta = next((e for e in root.iter() if isinstance(e.tag, str) and e.tag.endswith("metadata")), None)
        if meta is not None:
            got = _read_opf_meta(meta)
            if got.get("title") or got.get("identifiers"):
                return got
    return None

def _ingest_with_sidecar(f, owner, rid):
    side = _opf_sidecar(f)
    note = _ingest_file_entry(f, owner, rid)
    if side:
        db.merge_file_meta(rid, side)
    return note

def _ebook_folder(p, owner, name, box):
    """A folder of ebooks (a Calibre export, an rsync of a collection): every ebook file in it
    becomes its own request; the folder goes away once only sidecar files are left."""
    handled = 0
    for root, dirs, names in os.walk(p):
        dirs.sort()
        for n in sorted(names):
            if _ext(n) not in config.EBOOK_EXTS or n.startswith("."):
                continue
            f = os.path.join(root, n)
            rel = os.path.relpath(f, p)
            handled += _handle(f, owner, f"{name}/{rel}", "ebook", box, f"{name} - {rel.replace(os.sep, ' - ')}",
                               lambda rid, f=f: _ingest_with_sidecar(f, owner, rid))
    if not _folder_leftovers(p) and not any(_ext(n) in config.EBOOK_EXTS for _r, _d, ns in os.walk(p) for n in ns):
        shutil.rmtree(p, ignore_errors=True)
    return handled

def _folder_entry(p, owner, name, box):
    """A settled folder: audiobook, a set of ebooks, or parked with a clear message."""
    try:
        _refuse_links(p)
        kind = _classify_dir(p)
    except Exception as e:                      # a symlink inside: refuse the whole folder
        return _handle(p, owner, name, "audio", box, name, lambda rid, e=e: (_ for _ in ()).throw(e))
    if kind == "audio":
        import bookreq
        def place(rid):
            _beat("dropbox")
            held = bookreq.check_audio_arrival(p, owner)   # v6.0: a Get the audiobook download that is not the book
            return held or _place_audio_dir(p, owner, _safe(name), rid)
        return _handle(p, owner, name, "audio", box, name, place)
    if kind == "ebooks":
        return _ebook_folder(p, owner, name, box)
    why = ("has both audio and ebook files: put the audiobook and the ebooks in separate folders"
           if kind == "mixed" else
           f"has no audio or ebook files ({', '.join(_folder_leftovers(p)[:3]) or 'only covers/metadata'})")
    return _handle(p, owner, name, "ebook", box, name,
                   lambda rid: (_ for _ in ()).throw(ValueError(f"folder {why}")))

def scan_dropbox_once(now=None):
    """One pass over /dropbox/<user>/: ingest every settled file or folder a user dropped
    there (portal upload, Shelfmark, scp/rsync/Syncthing/WebDAV, e-mail intake). A folder is an
    audiobook only if it holds audio and no ebooks; a folder of ebooks is imported file by
    file. Returns the number of request rows created. Failures are parked under .failed/ with
    one error row each; a new file dropped under a failed file's name is processed again."""
    now = now or time.time()
    handled = 0
    base = config.DROPBOX_DIR
    if not os.path.isdir(base):
        return 0
    if _disk_paused():        # leave what was dropped where it is; it is picked up on resume
        return 0
    # v6.0.1, a fair queue: the readers take turns, one item each, instead of one reader's whole
    # folder first (fifty comics dropped by one reader kept everyone else's book waiting behind them)
    lanes = []
    for dirname in sorted(os.listdir(base)):
        d = os.path.join(base, dirname)
        if not os.path.isdir(d) or dirname.startswith("."):
            continue
        owner = _known_user(dirname)
        if not owner:
            continue
        names = [n for n in sorted(os.listdir(d)) if not (n.startswith(".") or n.lower().endswith(PARTIAL))]
        if names:
            lanes.append((owner, d, names))
    for turn in range(max((len(n) for _o, _d, n in lanes), default=0)):
        for owner, d, names in lanes:
            if turn >= len(names):
                continue
            if turn and _disk_paused():          # a large arrival filled the disk: the rest waits
                return handled
            name = names[turn]
            p = os.path.join(d, name)
            if not os.path.lexists(p):           # moved or taken meanwhile
                continue
            if os.path.islink(p):
                # check BEFORE isdir: a directory symlink is followed by isdir and would never
                # reach the refusal (it could point at another user's folder or at /etc)
                handled += _handle(p, owner, name, "ebook", d, name,
                                   lambda rid, p=p: _refuse_links(p))
            elif os.path.isdir(p):
                if _settled_dir(p, now):
                    handled += _folder_entry(p, owner, name, d)
            elif os.path.isfile(p):
                if now - os.lstat(p).st_mtime < SETTLE_SECONDS:     # let the write finish
                    continue
                kind = "audio" if _ext(name) in config.AUDIO_EXTS else "ebook"
                handled += _handle(p, owner, name, kind, d, name, lambda rid, p=p, owner=owner: _ingest_file_entry(p, owner, rid))
            _beat("dropbox")
    return handled

# ---- http requests --------------------------------------------------------------------------
AUDIO_SNIFFED = ("m4b", "m4a", "mp3", "flac", "ogg", "wav")   # single audio files we recognise
ARCHIVE_SNIFFED = ("audio-zip", "book-zip", "zip")            # archives worth opening
_SNIFF_NAMES = {"pdf": "a PDF", "epub": "an EPUB", "cbz": "a comic archive",
                "zip": "a zip archive with no books or audio in it",
                "damaged-zip": "a damaged or incomplete zip/EPUB (the download may have been cut short)",
                "oversize-zip": "an archive claiming far more files than any real book or audiobook "
                                "has (it would use more memory than the portal has)",
                "cbr": "a RAR (CBR) archive, which cannot be tagged", "m4b": "an M4B audiobook",
                "m4a": "an M4A audio file", "mp3": "an MP3", "flac": "a FLAC file",
                "ogg": "an Ogg file", "wav": "a WAV file"}
# what such a file is CALLED on disk when it has to be parked for the admin
_PARK_EXT = {"audio-zip": "zip", "book-zip": "zip", "damaged-zip": "zip", "zip": "zip",
             "oversize-zip": "zip"}

def _zip_kind(path):
    """What a zip really is: an EPUB, a comic, an audiobook archive, an archive of books, or
    something with nothing usable in it."""
    try:
        _zip_ok(path)
    except ValueError:
        return "oversize-zip"           # refused on the EOCD entry count, before ZipFile
    try:
        with zipfile.ZipFile(path) as z:
            low = [n.lower() for n in z.namelist()]
    except (zipfile.BadZipFile, OSError):
        return "damaged-zip"
    if "mimetype" in low or "meta-inf/container.xml" in low:
        return "epub"
    exts = {n.rsplit(".", 1)[-1] for n in low if "." in n}
    if exts & {a for a in config.AUDIO_EXTS if a != "zip"}:
        return "audio-zip"
    if exts & set(config.EBOOK_EXTS):
        return "book-zip"
    if exts & {"jpg", "jpeg", "png", "webp", "gif"}:
        return "cbz"
    return "zip"

def _sniff_ext(path):
    """The real type from the first bytes, whatever the name or the requested kind says: an
    m4b asked for as an ebook is still audio, and an EPUB behind an 'audio' intake URL is
    still a book. Returns '' when nothing is recognised."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path)
    if head[4:8] == b"ftyp":
        return "m4b" if head[8:11].lower() == b"m4b" else "m4a"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"Rar!"):
        return "cbr"
    return ""

def _place_http(req):
    """Download, then route by what the bytes ARE: audio to Audiobookshelf, books to the
    library ingest, anything else parked with a plain-language reason under its real
    extension. The requested 'kind' is only a hint — LibriVox zips, direct .m4b links and
    intake URLs that point at the wrong kind all end up in the right place."""
    tmpdir = _tmpdir()
    try:
        raw = os.path.join(tmpdir, "download.bin")
        _download(req["download_url"], raw, req=req, rid=req["id"])
        # Verify against what the SOURCE published, before the file is tagged or imported.
        # A mismatch is NOT silently accepted and NOT silently discarded: the row goes to
        # needs-review with the reason, because "we got something different from what was
        # advertised" is a decision for a person, not a retry.
        bad = verify_download(raw, req)
        if bad:
            db.set_status(req["id"], "needs-review",
                          "the download does not match what the source published: "
                          + "; ".join(bad) + ". Nothing was imported — retry it, or dismiss it.")
            db.audit("download_mismatch", req["owner"], None,
                     f"#{req['id']} {req.get('title','')[:60]}: {'; '.join(bad)[:150]}")
            return
        # ONE byte budget for the whole name: two independent 180-byte truncations added up to
        # 363 bytes, and the ingest rename then failed with ENAMETOOLONG on every retry
        base = _safe(f"{_safe(req['author'])} - {_safe(req['title'])}")
        ext = _sniff_ext(raw)
        if ext in AUDIO_SNIFFED or ext in ("audio-zip", "book-zip"):
            # _place_audio_file opens the archive and sends an archive of BOOKS to the ebook
            # ingest instead of exploding it into the audiobook library
            note = _place_audio_file(raw, req["owner"], base, req["id"])
        elif ext in ("epub", "pdf", "cbz"):
            try:
                note = _atomic_ingest(raw, req["owner"], base, ext, req["id"],
                                      title=req.get("title") or None, author=req.get("author") or None)
            except RuntimeError as e:        # untaggable for an isolated user: keep it for the admin
                raise RuntimeError(f"{e}; file kept at {_park(raw, req['owner'], f'{base}.{ext}')}")
        else:
            kept = _park(raw, req["owner"], f"{base}.{_PARK_EXT.get(ext, ext) or 'bin'}")
            raise ValueError(f"the download is not a book or an audiobook "
                             f"({_SNIFF_NAMES.get(ext, 'an unrecognised file')}); kept at {kept}")
        _finish(req["id"], _status_for(note), note)
    except OSError as e:
        # never surface a raw errno and an internal container path to a family member, and
        # never blame the title for a disk that is simply full: the admin acts on this text
        why = e.strerror or e.__class__.__name__
        if e.errno == errno.ENAMETOOLONG:
            why += "; the title or the author name may be too long for this filesystem"
        elif e.errno in (errno.ENOSPC, errno.EDQUOT):
            why = "the server is out of disk space"
        raise ValueError(f"the file could not be saved ({why})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _place_ebook_http(req):
    _place_http(req)

def _place_audio_http(req):
    _place_http(req)

def _owner_gone(owner):
    """True only when app.db positively has no such account (a removed user); an unreadable
    app.db is not a reason to fail a request."""
    try:
        return cwa.get_user(owner) is None
    except cwa.CwaError:
        return False

def _process(req):
    try:
        if _owner_gone(req["owner"]):
            raise RuntimeError(f"the account '{req['owner']}' no longer exists; not imported")
        if req.get("is_torrent"):
            raise RuntimeError("the P2P (torrent) download path was removed; request it again")
        fam = _family_request(req)
        if fam:
            _finish(req["id"], _status_for(fam), fam)
            return
        if req["kind"] == "audio":
            _place_audio_http(req)
        else:
            _place_ebook_http(req)
    except Exception as e:
        log.warning("request #%s failed: %s", req["id"], e)
        _finish(req["id"], "error", str(e)[:300])

# ---- Audiobookshelf tag jobs ------------------------------------------------------------------
TAG_BACKOFF = (15, 30, 60, 120, 300)      # seconds between attempts, then every 5 min until the give-up

def _tag_outcome(job, status, note):
    rid = job.get("rid")
    if rid is None:
        return
    rec = db.get(rid)
    if not rec:
        return
    base = (rec.get("detail") or "").split(f"; {TAG_WAIT_NOTE}")[0]
    _finish(rid, status, f"{base}; {note}".strip("; "))

def process_tag_jobs(now=None):
    """Retry every due audiobook tag job: find the item ABS indexed for the folder, set
    owner:<user>, and only then mark the request done. After ABS_TAG_GIVE_UP_HOURS the request
    becomes 'needs-tag' and the admin is alerted. Returns the number of items tagged."""
    now = now or time.time()
    if not absapi.configured():
        for job in db.pending_tag_jobs():         # token removed since: hand these to the admin
            db.tag_job_close(job["id"], now)
            _tag_outcome(job, NEEDS_TAG, f"{NEEDS_TAG}: no ABS API token, set tag {_owner_tag(job['owner'])} in ABS by hand")
        return 0
    tagged = 0
    for job in db.due_tag_jobs(now):
        tag, err = _owner_tag(job["owner"]), None
        try:
            # a box set or a multi-book zip becomes SEVERAL ABS items under one folder: tag
            # every one of them, and only close the job when the count stopped growing (ABS
            # indexes the books of a folder one by one)
            items = absapi.find_items_by_folder(job["folder"])
            if items:
                for it in items:
                    absapi.tag_item(it["id"], tag)
                n = len(items)
                if n == (job.get("last_count") or 0):
                    db.tag_job_close(job["id"], now)
                    _tag_outcome(job, "done", f"tagged {tag} in ABS" if n == 1
                                 else f"tagged {n} items {tag} in ABS")
                    tagged += n
                    continue
                db.tag_job_retry(job["id"], now + TAG_BACKOFF[0], None, count=n)
                continue
            if job["attempts"] % 4 == 3:          # ABS missed the folder? nudge it again
                absapi.trigger_scan()
        except Exception as e:                    # ABS down / API error: keep trying
            err = f"{e.__class__.__name__}: {str(e)[:120]}"
        if now - (job["created"] or now) > config.ABS_TAG_GIVE_UP_HOURS * 3600:
            db.tag_job_close(job["id"], now)
            msg = (f"{NEEDS_TAG}: Audiobookshelf did not index '{job['folder']}' within "
                   f"{config.ABS_TAG_GIVE_UP_HOURS} h" + (f" (last error: {err})" if err else "")
                   + f"; set tag {tag} in ABS by hand")
            _tag_outcome(job, NEEDS_TAG, msg)
            notify.alert("audiobook left untagged", f"{job['owner']}: {job['folder']}\n{msg}", "high")
        else:
            db.tag_job_retry(job["id"], now + TAG_BACKOFF[min(job["attempts"], len(TAG_BACKOFF) - 1)], err)
            if err and job.get("rid") is not None:
                rec = db.get(job["rid"])
                if rec and rec["status"] == TAGGING:
                    base = (rec.get("detail") or "").split(" [last ABS error")[0]
                    db.set_status(job["rid"], TAGGING, f"{base} [last ABS error: {err}]")
    return tagged

# ---- /ingest watchdog: CWA only reacts to inotify events ---------------------------------------
# Calibre-Web Automated's ingest service watches /ingest with inotify and does NOT sweep the
# folder at start. Anything that lands while it is down, restarting or updating is therefore
# never imported — while the portal already told the user 'done'. Renaming the file out and
# back re-fires the event, which imported such files in seconds in testing.
INGEST_NUDGE_SECONDS = 60      # a file CWA has not taken within this long gets a nudge
INGEST_NUDGE_EVERY = 60        # ... and at most one nudge per file per minute
INGEST_NUDGE_ALERT = 5         # this many fruitless nudges: tell the admin (once per file)
_NUDGES = {}                   # ingest file name -> [count, last nudge time]
_NUDGE_ALERTED = set()

def _cwa_alive():
    """CWA's own app.db opens and has its user table: the container is up. (A nudge while it
    is down would only rewrite the file for nothing.)"""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{config.CWA_DB}?mode=ro", uri=True, timeout=5)
        try:
            c.execute("SELECT 1 FROM user LIMIT 1").fetchone()
        finally:
            c.close()
        return True
    except Exception:
        return False

def nudge_ingest_once(now=None):
    """Re-fire CWA's inotify for files that are sitting in /ingest: rename each one aside and
    straight back (same directory, so both renames are atomic and the file is never lost).
    Returns the number of files nudged."""
    now = now or time.time()
    try:
        names = os.listdir(config.INGEST_DIR)
    except OSError:
        return 0
    if not _cwa_alive():
        return 0
    nudged = 0
    for name in sorted(names):
        p = os.path.join(config.INGEST_DIR, name)
        if name.startswith(".") or name.endswith((".part", ".tmp")) or not os.path.isfile(p):
            continue
        try:
            if now - os.path.getmtime(p) < INGEST_NUDGE_SECONDS:
                continue
        except OSError:
            continue
        count, last = _NUDGES.get(name, (0, 0.0))
        if now - last < INGEST_NUDGE_EVERY:
            continue
        tmp = os.path.join(config.INGEST_DIR, f".{uuid4().hex}.nudge")
        try:
            os.rename(p, tmp)
            os.rename(tmp, p)
        except OSError as e:
            log.warning("could not nudge %s: %s", name, e)
            continue
        count += 1
        _NUDGES[name] = (count, now)
        nudged += 1
        log.info("nudged /ingest/%s (%d)", name, count)
        if count >= INGEST_NUDGE_ALERT and name not in _NUDGE_ALERTED:
            _NUDGE_ALERTED.add(name)
            notify.alert("library import stuck",
                         f"'{name}' has been in /ingest for a while and Calibre-Web has not "
                         f"imported it after {count} nudges. Check the calibre-web container "
                         f"and its ingest service.", "high")
    for gone in [n for n in _NUDGES if n not in names]:
        _NUDGES.pop(gone, None)
        _NUDGE_ALERTED.discard(gone)
    return nudged

def _ingest_marker(owner, rid):
    """The ' [owner-rid]' suffix _unique() puts on every file handed to CWA: our fingerprint
    for finding that exact book again in /ingest, in metadata.db or in CWA's failed folder."""
    return f"[{_safe(owner)}-{rid}]"

def _in_calibre(marker):
    """The Calibre book id for an import, or None. The ' [owner-rid]' marker survives in the
    file name calibre stores (data.name) and, for files without metadata, in the title.

    Returns the ID rather than a bool on purpose: this is the ONE moment the portal knows with
    certainty which Calibre row belongs to which request, so it is the only free and reliable
    join we will ever get. Callers that only care whether it imported can still treat the
    result as truthy — id 0 does not exist in Calibre."""
    import sqlite3
    try:
        c = library._conn()
        try:
            like = f"%{marker}%"
            r = c.execute("SELECT book FROM data WHERE name LIKE ? LIMIT 1", (like,)).fetchone() or \
                c.execute("SELECT id FROM books WHERE title LIKE ? OR path LIKE ? LIMIT 1", (like, like)).fetchone()
            return int(r[0]) if r else None
        finally:
            c.close()
    except sqlite3.Error as e:
        # NOT a bare except returning False: "the catalogue is unreadable" and "this book did
        # not import" are different answers and were indistinguishable here.
        log.warning("could not read metadata.db while reconciling %s: %s", marker, e)
        return None

IMPORT_GRACE_SECONDS = 180     # CWA normally takes seconds; this is generous
# A row nobody has touched for this long is settled: CWA either imported it or the ingest
# watchdog has been alerting about it for hours. Without a ceiling this loop re-walked the
# newest 200 rows — every one of them a listdir of /ingest, a walk of CWA's failed tree and a
# metadata.db query — every pass, for ever.
RECONCILE_MAX_AGE = 24 * 3600
STILL_WAITING = "still waiting for the library to import it"

def _ingest_names():
    """What is waiting in /ingest — read ONCE per pass, not once per row."""
    try:
        return os.listdir(config.INGEST_DIR)
    except OSError:
        return []

def _cwa_failed_names():
    """CWA moves files its importer could not handle to processed_books/failed. One walk of
    that tree per pass, not one per row."""
    out = []
    try:
        for _root, dirs, names in os.walk(os.path.join(config.CWA_PROCESSED_DIR, "failed")):
            out += dirs + names
    except OSError:
        pass
    return out

def reconcile_imports(now=None):
    """'Handed to CWA' is not 'imported'. For every ebook row we called done (or that is still
    importing, or that a restart killed mid-ingest), check what actually happened to its file:
      - it is in CWA's failed folder            -> the request FAILS visibly (and the admin is told)
      - it is still in /ingest after the grace  -> back to 'importing' (the nudge loop works on it)
      - metadata.db knows it                    -> done
    Returns the number of rows whose status changed."""
    now = now or time.time()
    changed = 0
    ingest, failed_names = _ingest_names(), _cwa_failed_names()
    rows = db.rows_by_status(("done", "importing"), limit=200) + db.interrupted_rows(limit=200)
    for r in rows:
        updated = r.get("updated") or now
        if r["kind"] == "audio" or updated > now - IMPORT_GRACE_SECONDS or updated < now - RECONCILE_MAX_AGE:
            continue
        marker = _ingest_marker(r["owner"], r["id"])
        detail = r.get("detail") or ""
        interrupted = r["status"] == "error"      # a local row db.recover_on_start gave up on
        if any(marker in n for n in failed_names):
            if interrupted:
                continue                          # already an error row, and for the honest reason
            _finish(r["id"], "error",
                    "the library (Calibre-Web) could not import this file — it is in CWA's "
                    "failed folder; the admin can look at it and retry")
            notify.alert("a book failed to import",
                         f"{r['owner']}: '{r['title']}' is in Calibre-Web's processed_books/failed.", "high")
            changed += 1
            continue
        if any(marker in n for n in ingest):
            if r["status"] == "done":
                db.set_status(r["id"], "importing", f"{detail}; {STILL_WAITING}".lstrip("; "))
                changed += 1
            elif interrupted:
                # the file DID reach /ingest before the kill: the row is a bogus permanent
                # failure for a book the reader can already see
                db.set_status(r["id"], "importing", f"the restart happened after the file was handed "
                                                    f"to the library; {STILL_WAITING}")
                changed += 1
            continue
        if interrupted:
            cid = _in_calibre(marker)
            if cid:
                db.link_calibre(r["id"], cid, r["owner"])
                _finish(r["id"], "done", "imported into your library (a restart interrupted the "
                                         "request, but the file had already been handed over)")
                changed += 1
            continue
        if r["status"] == "done" and not r.get("calibre_id"):
            # Already reported done, but never joined to its Calibre row: for an EPUB carrying
            # its own metadata Calibre renames the file and the marker is gone, so the book was
            # invisible to the metadata push and the book page (seen on the real stack).
            cid = _find_imported(r)
            if cid:
                db.link_calibre(r["id"], cid, r["owner"])
            continue
        # An 'importing' row is only closed here when we KNOW its file reached the ingest
        # folder: either metadata.db has it, or this loop is the one that re-opened it (a row
        # that is still downloading has neither and must be left alone).
        cid = _in_calibre(marker) if r["status"] == "importing" else None
        if cid:
            # the one moment the request-to-Calibre mapping is certain; every later join needs it
            db.link_calibre(r["id"], cid, r["owner"])
        if r["status"] == "importing" and (cid or STILL_WAITING in detail):
            clean = detail.replace(f"; {STILL_WAITING}", "") or "imported into your library"
            if STILL_WAITING in detail:
                # this loop re-opened a row that had already been reported done (and notified):
                # a second _finish sent the reader "it is in your library" all over again
                db.set_status(r["id"], "done", clean)
            else:
                _finish(r["id"], "done", clean)
            changed += 1
    return changed

# ---- passwords changed in CWA's own UI ----------------------------------------------------
def check_password_drift():
    """A password changed on Calibre-Web's /me page reaches the portal and Shelfmark but NOT
    Audiobookshelf (its own credential store). Detect it (the stored fingerprint no longer
    matches app.db) and tell user and admin instead of leaving a silent 401 in the app."""
    import auth
    told = 0
    try:
        users = cwa.list_users()
    except Exception:
        return 0
    for u in users:
        name = u["name"]
        fp = auth.fingerprint(name)
        if not fp or fp is auth.UNAVAILABLE:
            continue
        known = db.get_pw_fingerprint(name)
        if known is None:
            db.set_pw_fingerprint(name, fp[0])       # first sight: remember, do not alert
            continue
        if known == fp[0]:
            continue
        db.set_pw_fingerprint(name, fp[0])
        # the change came from CWA's own UI, so it is still sitting in the WAL that Shelfmark
        # (immutable=1) cannot see: fold it in now instead of at the next 5-minute checkpoint,
        # during which the OLD password still opened Shelfmark
        cwa._checkpoint()
        if not absapi.configured():
            continue
        db.audit("password_changed_in_cwa", name, None,
                 "password changed outside the portal; Audiobookshelf still has the old one")
        notify.alert("a password was changed outside the portal",
                     f"{name} changed their password in Calibre-Web. Audiobookshelf keeps its own "
                     f"password and was NOT updated: ask them to change it again on the portal's "
                     f"Devices page, or run the admin tools' Users -> repair.", "default")
        told += 1
    return told

CHECKPOINT_EVERY = 300
# Reconciliation has its own, much shorter cadence: riding on the 300 s checkpoint meant a
# reader could see 'done' (and get the "it is in your library" mail) for up to IMPORT_GRACE +
# CHECKPOINT ~= 8 minutes before it was corrected. Now the worst case is ~4.
RECONCILE_EVERY = 60
_LAST_CHECKPOINT = [0.0]
_LAST_RECONCILE = [0.0]
_LAST_ENRICH = [0.0]

def _guarded(fn, *a):
    try:
        fn(*a)
    except Exception:
        log.exception("%s failed", fn.__name__)

def housekeeping_once(now=None):
    """Tag jobs and the /ingest watchdog every pass; the import reconciliation every minute;
    a passive app.db WAL checkpoint every few minutes (so Shelfmark, which ignores the WAL,
    sees password changes made in CWA's own UI) and with it the password-drift check."""
    now = now or time.time()
    _guarded(release_kobo_waits, now)
    process_tag_jobs(now)
    _guarded(nudge_ingest_once, now)
    _guarded(kindle_once, now)
    if now - _LAST_RECONCILE[0] >= RECONCILE_EVERY:
        _LAST_RECONCILE[0] = now
        _guarded(reconcile_imports)
        _guarded(reconcile_untagged, now)
    if now - _LAST_RELEASE[0] >= RELEASE_EVERY:
        _LAST_RELEASE[0] = now
        _guarded(reconcile_releases, now)
        _guarded(reconcile_audio_releases, now)
    if now - _LAST_ENRICH[0] >= ENRICH_EVERY:
        _LAST_ENRICH[0] = now
        _guarded(enrich_once, now)
        _guarded(queue_device_pushes)
    if now - _LAST_CHECKPOINT[0] >= CHECKPOINT_EVERY:
        _LAST_CHECKPOINT[0] = now
        cwa.checkpoint_passive()
        _guarded(check_password_drift)
        _guarded(fail_orphaned_pending)
        _guarded(close_orphaned_requests)
        _guarded(expire_held, now)
        _guarded(db.candidate_purge)

ENRICH_EVERY = 120          # seconds between enrichment passes
ENRICH_BATCH = 3            # books per pass: 2 cores, beside Calibre conversions

def enrich_once(now=None):
    """Fill in descriptive metadata for recently imported books, from the provider chain.

    BACKGROUND ONLY, and deliberately unhurried. The first provider's cold path measured 28.4 s
    — more than twice the search page's entire deadline — so this never runs inside a request.
    Nobody is waiting on it: a book is readable the moment it imports, and the metadata makes
    the portal nicer and the matching possible.

    The query is title+author (plus any identifier the FILE carried, which tagger.py now hands
    back). Identifiers are a VERIFICATION and DEDUPE key, never a query key — the release
    protocols have no ISBN field at all."""
    if not config.METADATA_ENABLED:
        return 0
    now = now or time.time()
    done = 0
    for r in db.needs_enrichment(ENRICH_BATCH):
        _beat("housekeeping")                    # a slow provider must not look like a dead loop
        query = {"title": r["file_title"] or r["title"],
                 "author": r["file_author"] or r["author"] or "",
                 "identifiers": db.file_ids(r["id"])}
        if r.get("work_key"):                  # chosen from its work page: identified exactly
            query["identifiers"] = query["identifiers"] + [{"kind": "openlibrary_work", "value": r["work_key"]}]
        key = metadata.cache_key(query)
        if metadata.negative_cached(key, now=now):
            # the whole chain already drew a blank on this one; re-asking three providers about
            # it on every pass for ever is how a background job becomes a self-inflicted load
            continue
        try:
            merged, trace = metadata.fetch(query, now=now)
        except Exception:
            log.exception("enrichment failed for request %s", r["id"])
            continue
        if merged:
            db.meta_store(merged, rid=r["id"], owner=r["owner"], now=now)
            db.meta_miss_clear(key)
            done += 1
        else:
            # NOT silent: the trace says whether we asked and nobody knew, or whether every
            # provider was stood down. Those are different problems with different fixes.
            metadata.remember_miss(key, now=now)
            log.info("no metadata for request %s (%s): %s",
                     r["id"], query["title"][:60], metadata.describe_trace(trace))
    return done

# ---- keep looking (wanted.py) ----------------------------------------------------------------
WANTED_EVERY = 60           # seconds between passes; each entry has its own schedule (wanted.SCHEDULE)
WANTED_BATCH = 2            # entries looked for per pass: every one is a search of every catalog

def _owner_admin(owner):
    try:
        u = cwa.get_user(owner)
    except Exception:
        return None
    if not u:
        return None
    return bool(u["role"] & cwa.ROLE_ADMIN)

def _wanted_ids(w, now):
    """ISBNs for a wanted book, from the metadata chain, asked ONCE per entry (on its first
    look). Used only to VERIFY a search result, never as the query. Adopted only when the
    chain's book has the same normalised title and does not disagree on the author, so a
    namesake's edition cannot vouch for the wrong result."""
    if w["identifiers"] or w["checks"] or not config.METADATA_ENABLED:
        return w["identifiers"] or []
    query = {"title": w["title"], "author": w["author"] or "", "identifiers": []}
    key = metadata.cache_key(query)
    if metadata.negative_cached(key, now=now):
        return []
    try:
        merged, _trace = metadata.fetch(query, now=now)
    except Exception:
        log.exception("metadata lookup failed for wanted %s", w["id"])
        return []
    if not merged:
        metadata.remember_miss(key, now=now)
        return []
    names = " ".join(a.get("name") or "" for a in merged.get("authors") or [])
    if dedupe.norm_title(merged.get("title")) != dedupe.norm_title(w["title"]):
        return []
    wa, ma = dedupe.author_tokens(w["author"]), dedupe.author_tokens(names)
    if wa and ma and not wa & ma:
        return []
    return [i for i in merged.get("identifiers") or [] if str(i.get("kind", "")).startswith("isbn")]

def _wanted_request(w, r, conf, reasons, is_admin):
    """Turn a match into an ordinary request, under the same rules a click on Request obeys:
    enabled source, a download address that source really hands out, the approval setting and
    the reader's daily limit. Returns (rid, status) or (None, why)."""
    req = {"kind": wanted.result_kind(r), "source": r.get("source"), "identifier": r.get("identifier"),
           "title": r.get("title"), "author": r.get("author"), "download_url": r.get("download_url"),
           # the evidence the source gave, so the download is checked against it
           "expect_size": r.get("expect_size"), "expect_md5": r.get("expect_md5"),
           "expect_sha1": r.get("expect_sha1"), "src_ids": r.get("src_ids"),
           "work_key": w.get("work_key") or r.get("work_key"), "language": r.get("language")}
    import fetchers                           # lazy: fetchers imports opds, which imports worker
    if not fetchers.source_enabled(req["source"]) or not req["download_url"]:
        return None, "that source is switched off"
    if not fetchers.url_allowed(req["source"], req["download_url"]):
        log.warning("wanted %s: refused a download address %s did not hand out", w["id"], req["source"])
        return None, "the download address was not one that source hands out"
    status = "queued" if (is_admin or not config.APPROVALS_REQUIRED) else "pending"
    limit = 0 if is_admin else config.MAX_REQUESTS_PER_DAY
    rid, _left, resets = db.add_if_under_quota(w["owner"], req, limit, status=status)
    if rid is None:
        return None, f"your daily request limit is used up (it resets at {time.strftime('%H:%M', time.localtime(resets))})"
    db.set_match(rid, conf, reasons, wanted_kind=w["kind"])
    db.audit("wanted_request", user=w["owner"],
             detail=f"wanted #{w['id']} -> request #{rid} {req['title']} [{req['source']}] ({conf:.2f}) -> {status}")
    if status == "pending":
        notify.send("requested", db.get(rid))
    return rid, status

def _wanted_from_work(w, ids):
    """The book's own catalogue links first (bookmeta.copies): a new Gutenberg, LibriVox or
    Standard Ebooks copy appears there, already tied to this exact work, and each copy is read
    and verified — language included. (result, confidence, reasons) like wanted.best, or None."""
    import bookmeta
    try:
        work = bookmeta.work(w["work_key"])
        if not work:
            return None
        lang = db.get_prefs(w["owner"])["language"]
        rejected = set(w.get("rejected") or [])
        for c in bookmeta.copies(work, lang):
            if c["kind"] != (w["kind"] or "ebook") or c["download_url"] in rejected:
                continue
            m = c["match"]
            if m["verdict"] == "reject":
                continue
            conf = 1 - m["distance"] if m["verdict"] == "auto" else min(wanted.AUTO - 0.05, 1 - m["distance"])
            return dict(c, work_key=work["key"], language=lang), round(conf, 3), m["reasons"]
    except bookmeta.Unavailable as e:
        log.info("wanted %s: Open Library unavailable (%s); falling back to the catalogues", w["id"], e)
    return None

def _wanted_note(w, event, detail=None):
    notify.send(event, {"owner": w["owner"], "title": w["title"], "author": w["author"],
                        "source": "wanted", "status": event, "detail": detail})

def check_wanted(w, now=None):
    """One look for one wanted book. Returns what happened: 'requested', 'candidate',
    'in-library', 'waiting', 'nothing', 'gone' or 'cancelled'."""
    now = now or time.time()
    is_admin = _owner_admin(w["owner"])
    if is_admin is None:
        db.wanted_update(w["id"], only_if_open=True, status="cancelled",
                         detail="the account that asked for it no longer exists")
        return "gone"
    # already here? (uploaded meanwhile, or found by Shelfmark) — then there is nothing to find
    ids = _wanted_ids(w, now)
    hit = dedupe.Index(w["owner"], False).match(w["title"], w["author"], ids)
    if hit and hit["how"] != "title":
        if db.wanted_update(w["id"], only_if_open=True, status="found", identifiers=ids,
                            last_check=now, detail=f"already in your library (matched by {hit['how']})"):
            return "in-library"
        return "cancelled"
    import fetchers
    nxt = now + wanted.next_delay(w["checks"] + 1)
    base = dict(checks=w["checks"] + 1, last_check=now, next_check=nxt, identifiers=ids)
    got = _wanted_from_work(w, ids) if w.get("work_key") else None
    results = []
    if not got:
        results = fetchers.search(wanted.query(w))
        lang = db.get_prefs(w["owner"])["language"]
        got = wanted.best(dict(w, identifiers=ids, language=lang), results, rejected=w["rejected"])
    if not got:
        db.wanted_update(w["id"], only_if_open=True, **base,
                         detail=f"looked {w['checks'] + 1} time(s); not in any catalog yet ({len(results)} unrelated result(s))")
        return "nothing"
    r, conf, reasons = got
    if conf >= wanted.AUTO:
        rid, why = _wanted_request(w, r, conf, reasons, is_admin)
        if rid:
            if db.wanted_update(w["id"], only_if_open=True, **base, status="found", rid=rid,
                                confidence=conf, reasons=reasons, candidate=r,
                                detail=f"found at {config.SOURCE_LABELS.get(r.get('source'), r.get('source'))}; request #{rid} ({why})"):
                _wanted_note(w, "wanted-found", f"from {config.SOURCE_LABELS.get(r.get('source'), r.get('source'))}")
                return "requested"
            return "cancelled"
        # a match we cannot request right now (limit, source off): keep it, retry sooner
        db.wanted_update(w["id"], only_if_open=True, **dict(base, next_check=now + 3600),
                         status="candidate", candidate=r, confidence=conf, reasons=reasons,
                         detail=f"found, not requested yet: {why}")
        return "waiting"
    fresh = (w.get("candidate") or {}).get("download_url") != r.get("download_url")
    if db.wanted_update(w["id"], only_if_open=True, **base, status="candidate", candidate=r,
                        confidence=conf, reasons=reasons,
                        detail="a possible match turned up; confirm it is the right book"):
        if fresh:
            _wanted_note(w, "wanted-candidate", "; ".join(reasons))
        return "candidate"
    return "cancelled"

def wanted_once(now=None):
    """Expire old entries, then look for the few that are due."""
    now = now or time.time()
    for w in db.wanted_expired(now - config.WANTED_DAYS * 86400):
        if db.wanted_update(w["id"], only_if_open=True, status="expired",
                            detail=f"not found in {config.WANTED_DAYS} days; stopped looking"):
            _wanted_note(w, "wanted-expired")
    if _disk_paused():
        return 0                             # nothing new is queued while the disk is full
    n = 0
    for w in db.wanted_due(now, WANTED_BATCH):
        _beat("wanted")
        try:
            check_wanted(w, now)
        except Exception:
            log.exception("keep-looking check failed for wanted %s", w["id"])
            db.wanted_update(w["id"], only_if_open=True, next_check=now + 3600)
        n += 1
    return n

def _find_imported(r):
    """The Calibre book of a finished request: the ' [owner-rid]' marker, else the reader's OWN
    owner tag + arrived after the file was handed over + a title agreeing with the file's own +
    not already joined to another request — and exactly one such book. Anything less certain
    stays unjoined; a wrong join would push one book's metadata onto another."""
    import sqlite3, matching
    cid = _in_calibre(_ingest_marker(r["owner"], r["id"]))
    if cid:
        return cid
    want = matching.clean(r.get("file_title") or r.get("title") or "")
    if not want:
        return None
    try:
        c = library._conn()
        try:
            rows = c.execute(
                "SELECT b.id, b.title FROM books b JOIN books_tags_link l ON l.book=b.id JOIN tags t ON t.id=l.tag "
                "WHERE t.name=? AND b.timestamp >= datetime(?, 'unixepoch', '-600 seconds')",
                (f"{config.OWNER_PREFIX}{r['owner']}", r.get("created") or r.get("updated") or 0)).fetchall()
        finally:
            c.close()
    except sqlite3.Error as e:
        log.warning("could not read metadata.db to join request %s: %s", r["id"], e)
        return None
    taken = db.linked_calibre_ids()
    hits = [b for b, t in rows if b not in taken and matching.similarity(want, matching.clean(t)) >= 0.9]
    return int(hits[0]) if len(hits) == 1 else None

# ---- L21: Send-to-Kindle jobs ---------------------------------------------------------------------
KINDLE_RETRY = (60, 300, 1800)       # a relay that is briefly down gets three more tries

def kindle_once(now=None):
    """Mail the queued Send-to-Kindle jobs. The file is resolved again here, with the same
    visibility rule the click used: a book removed or re-tagged meanwhile is not sent."""
    now = now or time.time()
    n = 0
    for j in db.kindle_due(now):
        if j.get("kind") == "comic":
            n += _kindle_comic(j, now)
            continue
        f = next((x for x in (library.file_for(j["owner"], j["book_id"], fmt, bool(j["is_admin"]))
                              for fmt in config.KINDLE_FORMATS) if x), None)
        addr = (cwa.get_user(j["owner"]) or {}).get("kindle_mail") or ""
        if not f or not addr:
            db.kindle_update(j["id"], status="failed",
                             detail="the book or your Kindle address is no longer available")
            continue
        try:
            msg = kindle.send(addr, f["path"], f["title"], f["filename"], author=f.get("authors"), book_title=f["title"])
            db.kindle_update(j["id"], status="sent", detail=msg, attempts=j["attempts"] + 1)
        except Exception as e:
            tries = j["attempts"] + 1
            if tries > len(KINDLE_RETRY):
                db.kindle_update(j["id"], status="failed", attempts=tries, detail=f"could not send: {str(e)[:200]}")
                notify.send("error", {"owner": j["owner"], "title": j["title"], "source": "kindle", "status": "error",
                                      "detail": f"Send-to-Kindle failed: {str(e)[:200]}"})
            else:
                db.kindle_update(j["id"], attempts=tries, next_try=now + KINDLE_RETRY[tries - 1],
                                 detail=f"retrying: {str(e)[:150]}")
        n += 1
    return n

def _kindle_comic(j, now):
    """A comic's Kindle copy, made by the host job (KCC) in staging/kindle-comics/<job>/: one or
    more parts under the Send-to-Kindle mail limit, each mailed as it is (no EPUB fixes: KCC
    wrote them for Amazon's converter). The files are removed once sent or given up."""
    d = os.path.join(config.STAGING_DIR, comics.KINDLE_STAGE, str(j["id"]))
    addr = (cwa.get_user(j["owner"]) or {}).get("kindle_mail") or ""
    names = json.loads(j.get("files") or "[]")
    if not addr or not names:
        db.kindle_update(j["id"], status="failed", detail="your Kindle address or the Kindle copy is gone")
        shutil.rmtree(d, ignore_errors=True)
        return 1
    try:
        for i, name in enumerate(names, 1):
            part = f" (part {i} of {len(names)})" if len(names) > 1 else ""
            kindle.send(addr, os.path.join(d, name), f"{j['title']}{part}", name, book_title=f"{j['title']}{part}",
                        fix=False)
        db.kindle_update(j["id"], status="sent", attempts=j["attempts"] + 1,
                         detail=f"sent in {len(names)} parts" if len(names) > 1 else "sent")
        shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        tries = j["attempts"] + 1
        if tries > len(KINDLE_RETRY):
            db.kindle_update(j["id"], status="failed", attempts=tries, detail=f"could not send: {str(e)[:200]}")
            shutil.rmtree(d, ignore_errors=True)
        else:
            db.kindle_update(j["id"], attempts=tries, next_try=now + KINDLE_RETRY[tries - 1], detail=f"retrying: {str(e)[:150]}")
    return 1

# ---- comics (docs/COMICS.md): search through Shelfmark, notice what never arrived ------------------
COMICS_EVERY = 60
_COMIC_LINKED = set()

def comics_once(now=None):
    if not config.COMICS_ENABLED:
        return 0
    import shelfmark_api
    now = now or time.time()
    if not shelfmark_api.configured():
        return 0
    try:
        queue = shelfmark_api.queue_status()
    except Exception as e:
        log.debug("comics: could not read Shelfmark's queue: %s", e)
        queue = None
    comics.watch_downloads(shelfmark_api, queue, now)
    n = 0
    for req in db.comic_due(now, limit=2):
        try:
            comics.search_once(req, shelfmark_api, now)
        except Exception as e:                   # one bad request never blocks the others
            log.warning("comics: request %s: %s", req["id"], e)
            db.comic_update(req["id"], next_try=now + 3600, detail=f"error: {str(e)[:200]}; trying again in an hour")
        n += 1
    _link_arrived_comics(now)
    _offer_shared_swaps(now)
    return n

# ---- one-tap book requests (v5.8.3, bookreq.py): the same Shelfmark path as comics ---------------
BOOKS_EVERY = 60

def books_once(now=None):
    import shelfmark_api, bookreq
    now = now or time.time()
    if not shelfmark_api.configured() or not db.bookreq_open(statuses=("queued", "downloading")):
        return 0
    try:
        queue = shelfmark_api.queue_status()
    except Exception as e:
        log.debug("books: could not read Shelfmark's queue: %s", e)
        queue = None
    try:
        bookreq.watch_downloads(shelfmark_api, queue, now)
    except Exception as e:
        log.warning("books: watching downloads: %s", e)
    n = 0
    for req in db.bookreq_due(now, limit=2):
        try:
            bookreq.search_once(req, shelfmark_api, now)
        except Exception as e:                   # one bad request never blocks the others
            log.warning("books: request %s: %s", req["id"], e)
            db.bookreq_update(req["id"], next_try=now + 3600, detail=f"error: {str(e)[:200]}; trying again in an hour")
        n += 1
    return n

FOLLOWS_EVERY = 600

def follows_once(now=None):
    import follows, anilist
    n = follows.run_once(now)
    try:
        anilist.sync_once()
    except Exception as e:                       # AniList down never stops the follow checks
        log.warning("AniList sync: %s", e)
    try:
        import metrontrack
        metrontrack.sync_once()
    except Exception as e:                       # nor does Metron
        log.warning("Metron sync: %s", e)
    try:
        import hcaudio
        hcaudio.sync_once()
    except Exception as e:                       # nor Hardcover (audiobook progress, v5.9.1)
        log.warning("Hardcover audiobook sync: %s", e)
    try:
        import hcwant
        hcwant.sync_once()
    except Exception as e:                       # nor the Want to Read list (v6.0)
        log.warning("Hardcover Want to Read: %s", e)
    return n

def _offer_shared_swaps(now):
    """A volume given from the family library replaces chapters too (v5.9)."""
    for req in db.comic_open(statuses=("shared",)):
        if req.get("calibre_id") and now - (req.get("updated") or now) < 86400 and not db.comic_swap_exists(req["owner"], req["id"]):
            try:
                comics.offer_swap(req, req["calibre_id"])
            except Exception as e:
                log.warning("comics: swap offer for request %s: %s", req["id"], e)

def _link_arrived_comics(now):
    """A delivered comic's Calibre id (for its page and for auto-send to Kindle)."""
    for req in db.comic_open(statuses=("done",)):
        if req.get("calibre_id") or req["id"] in _COMIC_LINKED or now - (req.get("updated") or now) > 86400:
            continue
        m = comics.find_in_library(comics.library_series(req), req["number"], req["kind"], viewer=req["owner"])
        if not m or req["owner"] not in m["owners"]:
            continue
        _COMIC_LINKED.add(req["id"])
        db.comic_update(req["id"], calibre_id=m["book_id"])
        try:
            comics.offer_swap(req, m["book_id"])     # v5.9: the chapters this volume replaces
        except Exception as e:
            log.warning("comics: swap offer for request %s: %s", req["id"], e)
        prefs = db.get_prefs(req["owner"])
        if prefs.get("auto_kindle") and (cwa.get_user(req["owner"]) or {}).get("kindle_mail"):
            comics.kindle_request(req["owner"], _is_admin(req["owner"]), m["book_id"], comics._title(req))

# ---- L10: owner tags for books whose file could not carry one ----------------------------------
UNTAGGED_WINDOW = 7 * 86400          # needs-tag rows younger than this are still reconciled

_NAME_TAIL = re.compile(r"(\.(mobi|azw3?|prc|fb2|txt)|\s*\[[^\]]*-\d+\]|\s*\((19|20)\d\d\))\s*$", re.I)

def _strip_name_tail(name):
    """'Author - Title (2003).mobi', 'Title [alice-12].fb2' -> the name without extension,
    our ' [owner-rid]' marker and a trailing year, in whatever order they come."""
    prev = None
    while prev != name:
        prev, name = name, _NAME_TAIL.sub("", name).strip()
    return name

def _untagged_guesses(r):
    """(title, author) pairs to look for in Calibre, most trusted first: what the FILE said
    (filemeta.py), then the request / file name read as it is, as 'Author - Title (Year)'
    (Shelfmark's naming) and as 'Title - Author'."""
    out = []
    if r.get("file_title"):
        out.append((r["file_title"], r.get("file_author") or ""))
    base = _strip_name_tail(r.get("title") or "")
    if base:
        out.append((base, r.get("author") or ""))
        parts = [x.strip() for x in re.split(r"\s+[-\u2013\u2014]\s+", base, maxsplit=1)]
        if len(parts) == 2 and all(parts):
            out += [(parts[1], parts[0]), (parts[0], parts[1])]
    return out

def _same_book(guess, title, authors):
    """Title agrees (the comparable core, or near-identical clean text) and, when both sides
    name an author, the names overlap. A known author that disagrees is a no."""
    import matching
    gt, ga = guess
    if not (dedupe.norm_title(gt) and dedupe.norm_title(gt) == dedupe.norm_title(title)) \
            and matching.similarity(matching.clean(gt), matching.clean(title)) < 0.9:
        return False
    want, have = dedupe.author_tokens(ga), dedupe.author_tokens(authors)
    return not (want and have) or bool(want & have)

def _untagged_match(r):
    """The Calibre book for a needs-tag request, or None. The ' [owner-rid]' marker first (it
    survives when Calibre took the title from the file name: TXT, FB2 without metadata); then
    a strict fallback for a book whose own metadata replaced the name, in ANY format (CWA
    converts MOBI/AZW3/FB2 to EPUB on import): arrived after the file was placed, NO owner tag
    yet, title and author agreeing with what the file (or its name) said, and exactly one such
    book. Anything less certain is left for the admin, as before."""
    import sqlite3
    cid = r.get("calibre_id") or _in_calibre(_ingest_marker(r["owner"], r["id"]))
    if cid:
        return int(cid)
    guesses = _untagged_guesses(r)
    if not guesses:
        return None
    try:
        c = library._conn()
        try:
            rows = c.execute(
                "SELECT b.id, b.title, (SELECT group_concat(a.name, ' & ') FROM books_authors_link bal "
                "JOIN authors a ON a.id=bal.author WHERE bal.book=b.id) FROM books b "
                "WHERE b.timestamp >= datetime(?, 'unixepoch', '-120 seconds') "
                "AND NOT EXISTS (SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag "
                "WHERE l.book=b.id AND t.name LIKE ?)",
                ((r["updated"] or r["created"]), f"{config.OWNER_PREFIX}%")).fetchall()
        finally:
            c.close()
    except sqlite3.Error as e:
        log.warning("could not read metadata.db for untagged book %s: %s", r["id"], e)
        return None
    hits = [bid for bid, title, authors in rows if any(_same_book(g, title, authors or "") for g in guesses)]
    return int(hits[0]) if len(hits) == 1 else None

def reconcile_untagged(now=None):
    """Queue the host job that adds the owner tag in Calibre (scripts/metadata-push.sh). A book
    that could not be found automatically within AUTO_TAG_ESCALATE is handed to the admin, with
    one alert, as before this ran on its own."""
    now = now or time.time()
    queued = 0
    for r in db.rows_by_status((NEEDS_TAG,), limit=100):
        if r.get("kind") == "audio" or (r.get("updated") or 0) < now - UNTAGGED_WINDOW:
            continue                          # audiobooks are Audiobookshelf's; old rows are the admin's
        if db.tag_push_open_for(r["id"]):
            continue
        cid = _untagged_match(r)
        if cid and db.queue_tag_push(cid, r["id"], r["owner"], now):
            db.link_calibre(r["id"], cid, r["owner"])
            db.set_status(r["id"], NEEDS_TAG, f"{r.get('detail') or NEEDS_TAG}; the owner tag is being added in Calibre")
            queued += 1
        elif not cid and AUTO_TAG_NOTE in (r.get("detail") or "") \
                and (r.get("updated") or r.get("created") or now) < now - AUTO_TAG_ESCALATE:
            ext = (r.get("detail") or "").split(":", 1)[1].strip().split(" ", 1)[0]
            _finish(r["id"], NEEDS_TAG, f"{NEEDS_TAG}: {ext} cannot carry a tag and the book was not found in "
                                        f"Calibre automatically; admin sets {_owner_tag(r['owner'])} in CWA")
    return queued

PLACEHOLDER_AUTHORS = {"", "unknown", "unknown author", "anonymous"}

def _calibre_current(calibre_id):
    """Calibre's present title, authors and series for one book (read-only)."""
    import sqlite3
    try:
        c = library._conn()
        try:
            r = c.execute("SELECT title FROM books WHERE id=?", (calibre_id,)).fetchone()
            if not r:
                return None
            authors = [a for (a,) in c.execute(
                "SELECT a.name FROM books_authors_link l JOIN authors a ON a.id=l.author "
                "WHERE l.book=?", (calibre_id,))]
            series = c.execute("SELECT s.name FROM books_series_link l JOIN series s "
                               "ON s.id=l.series WHERE l.book=?", (calibre_id,)).fetchone()
            extra = c.execute("SELECT has_cover, pubdate FROM books WHERE id=?", (calibre_id,)).fetchone()
            comments = c.execute("SELECT text FROM comments WHERE book=?", (calibre_id,)).fetchone()
            pub = c.execute("SELECT 1 FROM books_publishers_link WHERE book=?", (calibre_id,)).fetchone()
            langs = c.execute("SELECT 1 FROM books_languages_link WHERE book=?", (calibre_id,)).fetchone()
            ids = {k: v for k, v in c.execute("SELECT type, val FROM identifiers WHERE book=?", (calibre_id,))}
            return {"title": r[0] or "", "authors": authors, "series": series[0] if series else None,
                    "has_cover": bool(extra and extra[0]), "pubdate": (extra[1] or "") if extra else "",
                    "comments": bool(comments and (comments[0] or "").strip()), "publisher": bool(pub),
                    "languages": bool(langs), "identifiers": ids}
        finally:
            c.close()
    except sqlite3.Error:
        return None

def _looks_like_placeholder(calibre_title, owner, rid, request_title):
    """True when Calibre's title is a stand-in rather than a real one: it still carries our own
    ' [owner-rid]' ingest marker, it is the raw filename the request recorded, or it is empty.
    Only then is a provider title allowed to REPLACE it — a real title, possibly one a family
    member corrected by hand, is never overwritten with provider data."""
    t = (calibre_title or "").strip()
    if not t:
        return True
    if _ingest_marker(owner, rid) in t:
        return True
    return bool(request_title) and dedupe.norm_title(t) == dedupe.norm_title(request_title) \
        and any(ch in request_title for ch in "._") and " " not in request_title.strip()

def _title_sort(title):
    """Calibre's convention: a leading article moves to the end ('The Hobbit' -> 'Hobbit, The')."""
    t = (title or "").strip()
    for art in ("The ", "A ", "An "):
        if t.startswith(art) and len(t) > len(art):
            return f"{t[len(art):]}, {art.strip()}"
    return t

COVER_HOSTS = ("covers.openlibrary.org", "books.google.com", "books.googleusercontent.com",
               "i.gr-assets.com", "images.gr-assets.com", "assets.hardcover.app")

def cover_ok(url):
    """A cover address the host job may fetch: https, one of the providers' image hosts."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "").lower() in COVER_HOSTS and p.username is None

def queue_device_pushes(limit=20):
    """Decide what Calibre should learn from the portal's metadata, and queue it for the host.

    FILL GAPS, NEVER OVERWRITE: series goes in only when Calibre has none; authors only when
    Calibre says 'Unknown'; the title only when Calibre's is a placeholder. The owner tag is not
    in the vocabulary at all (db.PUSH_FIELDS), so it cannot be touched from here."""
    queued = 0
    for c in db.push_candidates(limit):
        cur = _calibre_current(c["calibre_id"])
        if cur is None:
            continue                             # unreadable right now: try again next pass
        fields = {}
        if _looks_like_placeholder(cur["title"], c["owner"], c["rid"], c["req_title"]):
            fields["title"] = c["full_title"] or c["title"]
            # calibredb leaves 'Title sort' at the OLD value (measured), so without this the
            # book keeps sorting under its release filename in Calibre-Web
            fields["sort"] = _title_sort(fields["title"])
        if not cur["authors"] or all(a.strip().lower() in PLACEHOLDER_AUTHORS for a in cur["authors"]):
            names = db.work_authors(c["work_id"])
            if names:
                fields["authors"] = " & ".join(names[:3])   # calibredb's own separator
        if not cur["series"]:
            s = db.work_series(c["work_id"])
            if s and s.get("name"):
                fields["series"] = s["name"]
                if s.get("sort_position") is not None:
                    fields["series_index"] = s["sort_position"]
        # v5, the second half — each strictly FILL-ONLY, like the fields above
        if not cur["comments"] and (c.get("description") or "").strip():
            fields["comments"] = c["description"].strip()[:20000]
        if not cur["publisher"] and c.get("publisher"):
            fields["publisher"] = c["publisher"][:200]
        if (not cur["pubdate"] or cur["pubdate"].startswith("0101")) and re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", c.get("release_date") or ""):
            fields["pubdate"] = c["release_date"]
        lang = __import__("matching").lang(c.get("language"))
        if not cur["languages"] and lang:
            fields["languages"] = lang
        isbn = db.work_isbn13(c["work_id"])
        if isbn and "isbn" not in cur["identifiers"]:
            # calibredb replaces the identifier SET, so the ones Calibre has are written back with it
            fields["identifiers"] = ",".join(f"{k}:{v}" for k, v in {**cur["identifiers"], "isbn": isbn}.items())
        if not cur["has_cover"] and cover_ok(c.get("cover_url")):
            fields["cover_url"] = c["cover_url"]
        if fields and db.queue_push(c["calibre_id"], fields, rid=c["rid"], owner=c["owner"]):
            queued += 1
        elif not fields:
            db.push_nothing_needed(c["calibre_id"], rid=c["rid"], owner=c["owner"])
    return queued

def fail_orphaned_pending():
    """A request awaiting approval is never claimed, so _owner_gone never sees it: removing the
    user left it pending for ever, un-actionable (an admin could only 'deny' a person who no
    longer exists). Fail it once, with a reason that says what happened."""
    failed = 0
    for r in db.rows_by_status(("pending",)):
        if _owner_gone(r["owner"]):
            _finish(r["id"], "error", f"the account '{r['owner']}' was removed while this was waiting for approval")
            failed += 1
    return failed

def close_orphaned_requests():
    """v6.0: a removed account's Get it / comic requests stop searching and pushing: cancelled,
    their held files dropped, their phone topic and Want to Read sync switched off."""
    import bookreq
    closed = 0
    owners = {r["owner"] for r in db.bookreq_open()} | {r["owner"] for r in db.comic_open()}
    for owner in owners:
        if not _owner_gone(owner):
            continue
        for r in db.bookreq_open(owner=owner):
            bookreq._drop_held(r)
            db.bookreq_update(r["id"], status="cancelled", candidate=None, held_path=None, detail="the account was removed")
            closed += 1
        for r in db.comic_open(owner):
            comics.drop_held(r)
            db.comic_update(r["id"], status="cancelled", candidate=None, held_path=None, detail="the account was removed")
            closed += 1
        db.set_prefs_v6(owner, ntfy_topic="", hc_want=0)
    return closed

def expire_held(now=None):
    """v6.0: an arrival held for a reader's check is not kept for ever on the 80 GB disk: after
    HELD_DAYS without an answer it is dropped and the request is looked for again."""
    import bookreq
    now = now or time.time()
    n = 0
    for r in db.bookreq_open(statuses=("held",)):
        if now - (r.get("updated") or now) > bookreq.HELD_DAYS * 86400:
            bookreq._drop_held(r)
            db.bookreq_update(r["id"], status="queued", next_try=now, held_path=None,
                              detail=f"the held file waited {bookreq.HELD_DAYS} days without an answer: looking for another copy")
            n += 1
    for r in db.comic_open(statuses=("held",)):
        if now - (r.get("updated") or now) > bookreq.HELD_DAYS * 86400:
            comics.drop_held(r)
            db.comic_update(r["id"], status="queued", next_try=now, held_path=None,
                            detail=f"the held file waited {bookreq.HELD_DAYS} days without an answer: looking for another copy")
            n += 1
    return n

DEVICES_EVERY = 600

def devices_once(now=None):
    """v6.3 (ondevice.py): an admin's Kobo gets their own books only (unless they chose otherwise),
    and finished books leave the Kobo of readers who asked for it, after their delay."""
    import ondevice
    try:
        ondevice.admin_kobo_pass(now)
    except Exception as e:
        log.warning("admins' Kobos: %s", e)
    return ondevice.offload_finished(now)

def _loop(name, fn, every):
    _beat(name)
    while True:
        try:
            fn()
        except Exception:
            log.exception("%s pass failed", name)
        _beat(name)
        time.sleep(every)

_OWN_PART = re.compile(r"^[0-9a-f]{32}\.part(\.tmp)?$")
_OWN_UPLOAD = re.compile(r"^\.[0-9a-f]{32}\.uploading$")
_OWN_INCOMING = re.compile(r"^\.incoming-[0-9a-f]{32}$")

def _sweep_audio_incoming():
    """library/audiobooks/.incoming-<uuid32> — a half-placed audiobook.

    _place_audio_dir and _place_audio_file rename the source INTO one of these and rename it
    out again when the book is complete; the put-it-back cleanup lives in an `except
    BaseException`, which a SIGKILL never runs. The OOM killer, an expiring stop_grace_period
    and the 04:30 automatic reboot all kill this process that way, and nothing else on the box
    knew the shape: sweep_orphans globbed only .part/.uploading/staging-tmp and
    scripts/disk-watch.sh reaps downloads/incomplete, library/staging and library/ingest/*.part.
    So each interrupted import leaked its own full size (a dropped folder is uncapped) as a
    hidden directory that shows up in no Audiobookshelf scan, no request row, no /admin and no
    self-test — but in every nightly restic snapshot. The reader, whose source folder is gone,
    simply drops it again, and the next kill makes a second copy.

    Swept here for the same reason as the rest: at worker start nothing of ours is in flight.
    The size is logged because a leak nobody can see is the whole problem."""
    removed = 0
    for p in glob.glob(os.path.join(config.AUDIO_DIR, ".incoming-*")):
        if not _OWN_INCOMING.match(os.path.basename(p)):
            continue
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        mb = _tree_size(p) / 2**20
        shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(p):
            log.warning("could not remove orphaned audiobook import %s", p)
            continue
        log.warning("removed orphaned audiobook import %s (%.1f MB reclaimed)", p, mb)
        removed += 1
    return removed

def sweep_orphans():
    """At worker start nothing of ours is in flight (one process), so every partial file we
    own is an orphan of a crash or OOM kill: /ingest/<uuid>.part, dropbox .<uuid>.uploading,
    the download temp dirs under /staging/tmp and the half-placed audiobooks under
    /audiobooks/.incoming-<uuid>. They are deleted whatever their age (they are copies; a
    quarantine would only fill the disk the way the crash loop did)."""
    removed = 0
    for p in glob.glob(os.path.join(config.INGEST_DIR, "*.part*")) + \
            glob.glob(os.path.join(config.DROPBOX_DIR, "*", ".*.uploading")):
        name = os.path.basename(p)
        if not (_OWN_PART.match(name) or _OWN_UPLOAD.match(name)):
            continue
        try:
            if os.path.isfile(p) and not os.path.islink(p):
                os.remove(p)
                log.warning("removed orphaned partial file %s", p)
                removed += 1
        except OSError:
            log.exception("could not remove %s", p)
    tmp = os.path.join(config.STAGING_DIR, "tmp")
    if os.path.isdir(tmp):
        for n in os.listdir(tmp):
            shutil.rmtree(os.path.join(tmp, n), ignore_errors=True)
            removed += 1
    return removed + _sweep_audio_incoming()

def queue_once():
    """Claim and process one queued request. True if there was one."""
    if _disk_paused():                           # nothing new on a disk that is already full
        return False
    req = db.claim_one()
    if not req:
        return False
    _process(req)
    return True

def run_forever():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.init()
    db.recover_on_start()
    sweep_orphans()
    _beat("queue")
    threading.Thread(target=_loop, args=("dropbox", scan_dropbox_once, 10), daemon=True).start()
    threading.Thread(target=_loop, args=("housekeeping", housekeeping_once, 15), daemon=True).start()
    # every reader's Shelfmark download waits here for a few seconds: shared if the family has
    # it, else approved (or held for the admin when APPROVALS_REQUIRED)
    threading.Thread(target=_loop, args=("shelfmark", shelfmark_gate_once, 8), daemon=True).start()
    # its own thread: a keep-looking pass is catalog searches (up to ~12 s each) and, once per
    # entry, the metadata chain — never allowed to hold up tag jobs or import reconciliation
    threading.Thread(target=_loop, args=("wanted", wanted_once, WANTED_EVERY), daemon=True).start()
    threading.Thread(target=_loop, args=("comics", comics_once, COMICS_EVERY), daemon=True).start()
    threading.Thread(target=_loop, args=("books", books_once, BOOKS_EVERY), daemon=True).start()
    # v5.8: followed series and authors (each checked once a day), AniList progress
    threading.Thread(target=_loop, args=("follows", follows_once, FOLLOWS_EVERY), daemon=True).start()
    # v6.3: finished books leave the Kobos of readers who chose it; admins' Kobos get only their own books
    threading.Thread(target=_loop, args=("devices", devices_once, DEVICES_EVERY), daemon=True).start()
    if config.IMAP_HOST:
        import imap
        threading.Thread(target=imap.poll_forever, daemon=True).start()
    while True:           # the queue loop must never die: a full disk or a locked db is transient
        _beat("queue")
        try:
            busy = queue_once()
        except Exception:
            log.exception("queue pass failed")
            busy = False
            time.sleep(5)
        if not busy:
            time.sleep(3)
