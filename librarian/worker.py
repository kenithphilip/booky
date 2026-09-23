"""Background worker: turns queued requests into files placed correctly and tagged to the
owner. Ingest is ATOMIC — a book is built under an ignored extension and then renamed into
place, so Calibre-Web never sees a half-written file (which it warns causes bad imports).

Trust boundary: the worker runs on the HOST network, so every URL it fetches is checked
against loopback/LAN/tailnet/link-local ranges first (a logged-in user controls the URL of a
request), catalog credentials only ever go to the configured catalog origin, and downloads
are capped per kind (local files too: a huge PDF must not OOM-kill the portal). Each loop
keeps a heartbeat in HEARTBEAT for /healthz."""
import os, time, threading, shutil, tempfile, glob, zipfile, re, socket, ipaddress, logging, unicodedata, errno
from uuid import uuid4
from urllib.parse import urlsplit, urljoin
import requests
import config, db, notify, abs as absapi, kindle, cwa
from tagger import (add_owner_tag, add_owner_tag_pdf, add_owner_tag_cbz, precheck_zip, TagError,
                    MAX_ZIP_MEMBERS as TAG_MAX_ZIP_MEMBERS)

log = logging.getLogger("worker")
UA = {"User-Agent": "bookstack-librarian/4.3"}
HEARTBEAT = {}          # loop name -> time.time() of its last pass ("queue", "dropbox", "housekeeping", "imap")
NEEDS_TAG = "needs-tag" # terminal status for formats that cannot carry the owner tag
TAGGING = "tagging"     # audiobook placed; waiting for Audiobookshelf to index it so the owner tag can be set

def _beat(name):
    HEARTBEAT[name] = time.time()

def _finish(rid, status, detail=None):
    db.set_status(rid, status, detail)
    r = db.get(rid)
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
    return {urlsplit(u).netloc.lower() for u in (config.MYCATALOG_URL, config.GUTENBERG_MIRROR) if u} - {""}

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
    if (req or {}).get("source") != "mycatalog" or not config.MYCATALOG_USER:
        return None
    c, p = urlsplit(config.MYCATALOG_URL), urlsplit(url)
    if c.netloc and (p.scheme, p.netloc.lower()) == (c.scheme, c.netloc.lower()):
        return (config.MYCATALOG_USER, config.MYCATALOG_PASS)
    return None

def _limit_for(kind):
    return (config.MAX_AUDIO_MB if kind == "audio" else config.MAX_EBOOK_MB) * 1024 * 1024

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

def _tag_or_fail(part, owner, ext, title=None, author=None):
    """Tag the staged copy. Admins import untagged when that fails (they see everything
    anyway); for an isolated user an untagged import would be invisible to them and owned by
    nobody, so the request fails instead of silently importing."""
    tag = _owner_tag(owner)
    try:
        if ext == "epub":
            add_owner_tag(part, tag)
        elif ext == "pdf":
            if os.path.getsize(part) > config.MAX_PDF_MB * 1024 * 1024:
                raise ValueError(f"PDF is {os.path.getsize(part) >> 20} MB: too large to tag safely "
                                 f"(MAX_PDF_MB={config.MAX_PDF_MB})")
            add_owner_tag_pdf(part, tag, title=title, author=author)
        else:
            _TAGGERS[ext](part, tag, title=title)
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
            note = _tag_or_fail(part, owner, ext, title=title or final_base, author=author)
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
            note = f"{NEEDS_TAG}: {ext} cannot carry a tag; admin sets {_owner_tag(owner)} in CWA"
        os.rename(part, os.path.join(config.INGEST_DIR, f"{_unique(final_base, owner, rid)}.{ext}"))
    except BaseException:
        for leftover in (part, part + ".tmp"):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise
    return note

def _status_for(note):
    if note.startswith(NEEDS_TAG):
        return NEEDS_TAG
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
        return "; auto-Kindle " + kindle.send(u["kindle_mail"], path, title or filename, filename)
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
    try:
        expanded = _expand_audio_zips(inc)
        if _audio_duplicate(owner, base, _tree_size(inc)):
            raise ValueError("this audiobook is already in your audiobooks (same name and size)")
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
    owner-tagged before import; mobi/azw3/fb2/txt import untagged ('needs-tag'); CBR is
    refused (RAR cannot be tagged and CWA converts it badly): convert to CBZ."""
    name = os.path.basename(path)
    stem, _, ext = name.rpartition(".") if "." in name else (name, "", "")
    ext, base = ext.lower(), _safe(stem)
    if ext == "cbr":
        raise ValueError("CBR (RAR) comics are not supported: convert to CBZ and upload again")
    sniffed = _sniff_ext(path)
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
    audio = ebooks = texts = 0
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
    if audio:
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
    note = ingest_local_file(p, owner, rid)
    os.remove(p)
    return note

def _folder_leftovers(path):
    """Files left in a folder after its ebooks were imported that are not mere sidecars."""
    return [os.path.relpath(os.path.join(r, n), path) for r, _d, ns in os.walk(path) for n in ns
            if _ext(n) not in SIDECAR_EXTS and not n.startswith(".")]

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
                               lambda rid, f=f: _ingest_file_entry(f, owner, rid))
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
        return _handle(p, owner, name, "audio", box, name,
                       lambda rid: (_beat("dropbox"), _place_audio_dir(p, owner, _safe(name), rid))[1])
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
    for dirname in sorted(os.listdir(base)):
        d = os.path.join(base, dirname)
        if not os.path.isdir(d) or dirname.startswith("."):
            continue
        owner = _known_user(dirname)
        if not owner:
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            # skip hidden/partial files (uploads in progress, Shelfmark/rsync temp names)
            if name.startswith(".") or name.lower().endswith(PARTIAL):
                continue
            if os.path.islink(p):
                # check BEFORE isdir: a directory symlink is followed by isdir and would never
                # reach the refusal (it could point at another user's folder or at /etc)
                handled += _handle(p, owner, name, "ebook", d, name,
                                   lambda rid, p=p: _refuse_links(p))
            elif os.path.isdir(p):
                if _settled_dir(p, now):
                    handled += _folder_entry(p, owner, name, d)
            elif os.path.isfile(p) or os.path.islink(p):
                if now - os.lstat(p).st_mtime < SETTLE_SECONDS:     # let the write finish
                    continue
                kind = "audio" if _ext(name) in config.AUDIO_EXTS else "ebook"
                handled += _handle(p, owner, name, kind, d, name, lambda rid, p=p: _ingest_file_entry(p, owner, rid))
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
    """metadata.db knows the book: CWA imported it. The suffix survives in the file name
    calibre stores (data.name) and, for files without metadata, in the title."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=5)
        try:
            like = f"%{marker}%"
            r = c.execute("SELECT 1 FROM data WHERE name LIKE ? LIMIT 1", (like,)).fetchone() or \
                c.execute("SELECT 1 FROM books WHERE title LIKE ? OR path LIKE ? LIMIT 1", (like, like)).fetchone()
            return bool(r)
        finally:
            c.close()
    except Exception:
        return False

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
            if _in_calibre(marker):
                _finish(r["id"], "done", "imported into your library (a restart interrupted the "
                                         "request, but the file had already been handed over)")
                changed += 1
            continue
        # An 'importing' row is only closed here when we KNOW its file reached the ingest
        # folder: either metadata.db has it, or this loop is the one that re-opened it (a row
        # that is still downloading has neither and must be left alone).
        if r["status"] == "importing" and (_in_calibre(marker) or STILL_WAITING in detail):
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
    process_tag_jobs(now)
    _guarded(nudge_ingest_once, now)
    if now - _LAST_RECONCILE[0] >= RECONCILE_EVERY:
        _LAST_RECONCILE[0] = now
        _guarded(reconcile_imports)
    if now - _LAST_CHECKPOINT[0] >= CHECKPOINT_EVERY:
        _LAST_CHECKPOINT[0] = now
        cwa.checkpoint_passive()
        _guarded(check_password_drift)
        _guarded(fail_orphaned_pending)

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

def sweep_orphans():
    """At worker start nothing of ours is in flight (one process), so every partial file we
    own is an orphan of a crash or OOM kill: /ingest/<uuid>.part, dropbox .<uuid>.uploading and
    the download temp dirs under /staging/tmp. They are deleted whatever their age (they are
    copies; a quarantine would only fill the disk the way the crash loop did)."""
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
    return removed

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
