"""Background worker: turns queued requests into files placed correctly and tagged to the
owner. Ingest is ATOMIC — a book is built under an ignored extension and then renamed into
place, so Calibre-Web never sees a half-written file (which it warns causes bad imports).

Trust boundary: the worker runs on the HOST network, so every URL it fetches is checked
against loopback/LAN/tailnet/link-local ranges first (a logged-in user controls the URL of a
request), catalog credentials only ever go to the configured catalog origin, and downloads
are capped per kind. Each loop keeps a heartbeat in HEARTBEAT for /healthz."""
import os, time, threading, shutil, tempfile, glob, zipfile, re, socket, ipaddress, logging, unicodedata
from uuid import uuid4
from urllib.parse import urlsplit, urljoin
import requests
import config, db, notify, abs as absapi, kindle, cwa
from tagger import add_owner_tag, add_owner_tag_pdf, add_owner_tag_cbz
from qbittorrent import Qbit

log = logging.getLogger("worker")
UA = {"User-Agent": "bookstack-librarian/4.2"}
HEARTBEAT = {}          # loop name -> time.time() of its last pass ("queue", "dropbox", "torrent", "imap")
NEEDS_TAG = "needs-tag" # terminal status for formats that cannot carry the owner tag

def _beat(name):
    HEARTBEAT[name] = time.time()

def _finish(rid, status, detail=None):
    db.set_status(rid, status, detail)
    r = db.get(rid)
    if r:
        notify.send(status, r)

def _safe(name):
    """A file-name-safe version of a title/author/stem: strips path separators, shell-hostile
    and control characters, keeps everything else (Calibre and ext4 are UTF-8 safe)."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")[:150]
    return name or "book"

def _owner_tag(owner):
    return f"{config.OWNER_PREFIX}{owner}"

def _unique(base, owner, rid=None):
    """Two users importing the same title within CWA's pickup window must not overwrite each
    other's file in /ingest; CWA reads the metadata from the file, not the name."""
    return f"{base} [{_safe(owner)}-{rid if rid is not None else uuid4().hex[:6]}]"

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
            raise ValueError(f"{p.hostname} resolves to a non-public address ({ip}); refused")

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

def _download(url, dest, req=None, rid=None, attempts=3):
    """Download with retry + exponential backoff. Sets a 'retrying' state between attempts
    so a transient network blip doesn't fail the request outright. Policy refusals
    (ValueError: bad target, too large) are final and not retried."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            _fetch(url, dest, req)
            return
        except ValueError:
            raise
        except Exception as e:
            last = e
            if attempt < attempts:
                if rid is not None:
                    db.set_status(rid, "retrying", f"attempt {attempt} failed ({str(e)[:80]}); retrying")
                time.sleep(2 ** attempt)          # 2s, 4s, ...
    raise last

# ---- ebooks: stage, tag, place --------------------------------------------------------------
_TAGGERS = {"epub": add_owner_tag, "pdf": add_owner_tag_pdf, "cbz": add_owner_tag_cbz}

def _tag_or_fail(part, owner, ext, title=None):
    """Tag the staged copy. Admins import untagged when that fails (they see everything
    anyway); for an isolated user an untagged import would be invisible to them and owned by
    nobody, so the request fails instead of silently importing."""
    tag = _owner_tag(owner)
    try:
        if ext == "epub":
            add_owner_tag(part, tag)
        else:
            _TAGGERS[ext](part, tag, title=title)
        return f"tagged {tag}"
    except Exception as e:
        if _is_admin(owner):
            return f"tag skipped: {str(e)[:120]}"
        raise RuntimeError(f"could not embed owner tag: {str(e)[:150]}")

def _atomic_ingest(src, owner, final_base, ext, rid=None):
    """Copy into ingest under a '.part' name (CWA ignores it), tag in place, rename to the
    real extension (atomic within the ingest mount). EPUB/PDF/CBZ carry the owner tag;
    mobi/azw3/fb2/txt cannot and are placed raw with a 'needs-tag' outcome."""
    part = os.path.join(config.INGEST_DIR, uuid4().hex + ".part")
    shutil.copyfile(src, part)
    try:
        if ext in _TAGGERS:
            note = _tag_or_fail(part, owner, ext, title=final_base)
            if ext == "epub":
                note += _auto_kindle(owner, part, f"{final_base}.epub")   # before CWA consumes the file
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
    return NEEDS_TAG if note.startswith(NEEDS_TAG) else "done"

def _auto_kindle(owner, path, filename):
    """If the user opted in (Devices page) and mail is configured, e-mail the tagged EPUB."""
    try:
        if not kindle.configured() or not db.get_prefs(owner)["auto_kindle"]:
            return ""
        u = cwa.get_user(owner)
        if not u or not u.get("kindle_mail"):
            return "; auto-Kindle skipped (no address on Devices page)"
        return "; auto-Kindle " + kindle.send(u["kindle_mail"], path, filename, filename)
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

def place_in_dropbox(dropbox_dir, name, write):
    """Create a NEW file in a dropbox without ever writing through a planted symlink: the
    temp name is unpredictable, O_EXCL|O_NOFOLLOW refuses to follow a link, and os.replace
    onto the final name replaces a link instead of following it. `write(fileobj)` fills it."""
    os.makedirs(dropbox_dir, exist_ok=True)
    tmp = os.path.join(dropbox_dir, f".{uuid4().hex}.uploading")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
        final = os.path.join(dropbox_dir, name)
        os.replace(tmp, final)
        return final
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

def _finish_audio(final, owner, rid=None):
    """After an audiobook folder is in place: scan, then tag it to its owner in ABS (background)."""
    scan = absapi.trigger_scan()
    if absapi.configured():
        absapi.tag_folder_async(os.path.basename(final), owner, rid)
        return f"{scan}; tagging {_owner_tag(owner)} in ABS"
    return f"{scan}; set tag {_owner_tag(owner)} in ABS"

def _audio_final(owner, base):
    final = os.path.join(config.AUDIO_DIR, f"{_safe(owner)} - {base}")
    if os.path.exists(final):
        final += "-" + uuid4().hex[:6]
    return final

def _place_audio_file(src, owner, base, rid=None):
    inc = os.path.join(config.AUDIO_DIR, ".incoming-" + uuid4().hex)
    os.makedirs(inc, exist_ok=True)
    try:
        if src.lower().endswith(".zip"):
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, inc)
        else:
            shutil.copyfile(src, os.path.join(inc, os.path.basename(src)))
        _beat("dropbox")
        final = _audio_final(owner, base)
        os.rename(inc, final)
    except Exception:
        shutil.rmtree(inc, ignore_errors=True)
        raise
    return _finish_audio(final, owner, rid)

def _place_audio_dir(src_dir, owner, base, rid=None):
    """A whole folder dropped in a dropbox (Shelfmark, rsync) becomes one audiobook."""
    final = _audio_final(owner, base)
    try:
        os.rename(src_dir, final)                # same filesystem: instant and atomic
    except OSError:
        inc = os.path.join(config.AUDIO_DIR, ".incoming-" + uuid4().hex)
        try:
            shutil.copytree(src_dir, inc)
            os.rename(inc, final)
        except Exception:
            shutil.rmtree(inc, ignore_errors=True)
            raise
        shutil.rmtree(src_dir)
    return _finish_audio(final, owner, rid)

def ingest_local_file(path, owner, rid=None):
    """Ingest one file a user legally owns, mapping it to that user. EPUB, PDF and CBZ are
    owner-tagged before import; mobi/azw3/fb2/txt import untagged ('needs-tag'); CBR is
    refused (RAR cannot be tagged and CWA converts it badly): convert to CBZ."""
    name = os.path.basename(path)
    stem, _, ext = name.rpartition(".") if "." in name else (name, "", "")
    ext, base = ext.lower(), _safe(stem)
    if ext in config.AUDIO_EXTS:
        return _place_audio_file(path, owner, base, rid)
    if ext == "cbr":
        raise ValueError("CBR (RAR) comics are not supported: convert to CBZ and upload again")
    if ext in config.EBOOK_EXTS:
        return _atomic_ingest(path, owner, base, ext, rid)
    raise ValueError(f"unsupported file type '.{ext}' (kept in .failed/, not deleted)")

# ---- dropbox watcher ------------------------------------------------------------------------
SETTLE_SECONDS = 12   # a file must be untouched this long before it is picked up
PARTIAL = (".part", ".tmp", ".crdownload", ".uploading")
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

def _ingest_dropbox_entry(p, owner, rid):
    _refuse_links(p)
    if os.path.isdir(p):
        _beat("dropbox")
        return _place_audio_dir(p, owner, _safe(os.path.basename(p)), rid)
    note = ingest_local_file(p, owner, rid)
    os.remove(p)
    return note

def scan_dropbox_once(now=None):
    """One pass over /dropbox/<user>/: ingest every settled file — or settled folder, taken as
    one audiobook — a user dropped there (portal upload, Shelfmark, scp/rsync/Syncthing/WebDAV,
    e-mail intake). Returns the number of entries handled. An entry that failed before is left
    alone (one error row per file; the failing file is parked under .failed/)."""
    now = now or time.time()
    handled = 0
    base = config.DROPBOX_DIR
    if not os.path.isdir(base):
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
            if os.path.isdir(p):
                if not _settled_dir(p, now):
                    continue
                kind = "audio"
            elif os.path.isfile(p):
                if now - os.path.getmtime(p) < SETTLE_SECONDS:   # let the write finish
                    continue
                kind = "audio" if name.rsplit(".", 1)[-1].lower() in config.AUDIO_EXTS else "ebook"
            else:
                continue
            last = db.last_for(owner, name, "dropbox")
            # a file that failed and was parked is not retried; one interrupted by a restart
            # ("interrupted by restart") is still here and gets picked up again
            if last and last["status"] == "error" and ".failed/" in (last.get("detail") or ""):
                continue
            rid = db.add(owner, {"kind": kind, "source": "dropbox", "title": name,
                                 "author": "", "download_url": "local"}, status="importing")
            try:
                note = _ingest_dropbox_entry(p, owner, rid)
                _finish(rid, _status_for(note), note)
            except Exception as e:
                log.exception("dropbox/%s/%s failed", owner, name)
                try:
                    where = "moved to " + _park(p, owner, name, d)
                except Exception as e2:
                    where = f"could not move it aside: {str(e2)[:60]}"
                _finish(rid, "error", f"{str(e)[:200]} ({where})")
            handled += 1
            _beat("dropbox")
    return handled

# ---- http / torrent requests --------------------------------------------------------------
def _sniff_ext(path):
    with open(path, "rb") as f:
        head = f.read(4)
    return "pdf" if head.startswith(b"%PDF") else "epub"

def _place_ebook_http(req):
    tmpdir = tempfile.mkdtemp()
    try:
        raw = os.path.join(tmpdir, "book.bin")
        _download(req["download_url"], raw, req=req, rid=req["id"])
        base, ext = f"{_safe(req['author'])} - {_safe(req['title'])}", _sniff_ext(raw)
        try:
            note = _atomic_ingest(raw, req["owner"], base, ext, req["id"])
        except RuntimeError as e:            # untaggable for an isolated user: keep it for the admin
            raise RuntimeError(f"{e}; file kept at {_park(raw, req['owner'], f'{base}.{ext}')}")
        _finish(req["id"], "done", note)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _place_audio_http(req):
    # LibriVox / OPDS audio: ship as a zip -> one folder per book, built out-of-place then renamed.
    inc = os.path.join(config.AUDIO_DIR, ".incoming-" + uuid4().hex)
    os.makedirs(inc, exist_ok=True)
    tmpdir = tempfile.mkdtemp()
    try:
        z = os.path.join(tmpdir, "a.zip")
        _download(req["download_url"], z, req=req, rid=req["id"])
        with zipfile.ZipFile(z) as zf:
            _safe_extract(zf, inc)
        final = _audio_final(req["owner"], f"{_safe(req['author'])} - {_safe(req['title'])}")
        os.rename(inc, final)                    # atomic within the audio mount
        # ABS isolation is by tag restriction: tag the item owner:<user> (automatic with ABS_TOKEN)
        _finish(req["id"], "done", _finish_audio(final, req["owner"], req["id"]))
    except Exception:
        shutil.rmtree(inc, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _start_torrent(req):
    url = req["download_url"]
    if not url.startswith("magnet:"):
        _check_target(url)                       # qBittorrent would fetch the .torrent from anywhere
    Qbit().add(url, config.STAGING_DIR)
    db.set_status(req["id"], "downloading", "handed to qBittorrent")

def _process(req):
    try:
        if req["is_torrent"]:
            _start_torrent(req)
        elif req["kind"] == "audio":
            _place_audio_http(req)
        else:
            _place_ebook_http(req)
    except Exception as e:
        log.warning("request #%s failed: %s", req["id"], e)
        _finish(req["id"], "error", str(e)[:300])

def _torrent_pass():
    """Tag epubs that qBittorrent finished into staging, then atomically place them in ingest."""
    pending = db.downloading_for_torrents()
    if not pending:
        return
    for path in glob.glob(os.path.join(config.STAGING_DIR, "**", "*.epub"), recursive=True):
        if not os.path.isfile(path):
            continue
        match = next((r for r in pending
                      if r["identifier"] and r["identifier"].split(":")[-1] in path), None) \
                or (pending[0] if pending else None)
        if not match:
            break
        base = f"{_safe(match['author'])} - {_safe(match['title'])}"
        try:
            note = _atomic_ingest(path, match["owner"], base, "epub", match["id"])
            os.remove(path)
            _finish(match["id"], "done", f"from torrent; {note}")
        except Exception as e:
            log.exception("torrent import for #%s failed", match["id"])
            _finish(match["id"], "error", f"from torrent; {str(e)[:200]}")
        pending = [r for r in pending if r["id"] != match["id"]]

def _loop(name, fn, every):
    _beat(name)
    while True:
        try:
            fn()
        except Exception:
            log.exception("%s pass failed", name)
        _beat(name)
        time.sleep(every)

def sweep_stale(max_age=3600, now=None):
    """Move partial files a crash left behind (/ingest/*.part, dropbox .*.uploading) older
    than an hour into <staging>/quarantine so they neither confuse CWA nor linger forever."""
    now = now or time.time()
    q = os.path.join(config.STAGING_DIR, "quarantine")
    moved = 0
    for p in (glob.glob(os.path.join(config.INGEST_DIR, "*.part*"))
              + glob.glob(os.path.join(config.DROPBOX_DIR, "*", ".*.uploading"))):
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age:
                os.makedirs(q, exist_ok=True)
                shutil.move(p, os.path.join(q, f"{int(now)}-{os.path.basename(p)}"))
                log.warning("quarantined stale partial file %s", p)
                moved += 1
        except OSError:
            log.exception("could not quarantine %s", p)
    return moved

def run_forever():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.init()
    db.recover_on_start()
    sweep_stale()
    _beat("queue")
    threading.Thread(target=_loop, args=("torrent", _torrent_pass, 20), daemon=True).start()
    threading.Thread(target=_loop, args=("dropbox", scan_dropbox_once, 10), daemon=True).start()
    if config.IMAP_HOST:
        import imap
        threading.Thread(target=imap.poll_forever, daemon=True).start()
    while True:
        _beat("queue")
        req = db.claim_one()
        if req:
            _process(req)
        else:
            time.sleep(3)
