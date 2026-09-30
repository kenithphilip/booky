#!/usr/bin/env bash
# comic-convert.sh — device copies of comics, made with KCC (Kindle Comic Converter), one at a time
# (docs/COMICS.md). Installed by bookstack.sh Deploy as /etc/cron.d/bookstack-comics (every 3
# minutes, under flock) when COMICS_ENABLED=true.
#
#   Kobo:   a comic whose owner reads on a Kobo (or that a reader asked for) gets a colour,
#           fixed-layout KEPUB added to the SAME Calibre book (calibredb add_format). Calibre-Web's
#           Kobo sync prefers a stored KEPUB and sends a pre-paginated one as EPUB3FL, so it
#           reaches the Kobo through the reader's existing link. Stored as .kepub on purpose:
#           Calibre-Web's cover/metadata enforcer rewrites only .epub/.azw3 files.
#   Kindle: a comic sent to a Kindle is converted when it is sent (KCC's "Send to Kindle" EPUB, for
#           the reader's Kindle), made to fit the mail limit, left in library/staging/kindle-comics/<job>/
#           for the portal to mail, and never stored in the library.
#   v6.2:   the portal says which device each copy is for (the readers choose theirs on the start
#           page; none chosen: KCC_KOBO_PROFILE / KCC_KINDLE_PROFILE, in colour, as before) and how
#           the pages are laid out (spreads, landscape pages, webtoon strips, newspaper dailies).
#
# Why a host job: the portal has no Docker socket (on purpose). The portal decides what is due
# (python -m admin_cli comics ...); this runs KCC in a throwaway container with no network, as
# PUID:PGID, memory-capped, and reports back. The owner tags are read before and after every
# write, like scripts/metadata-push.sh; a change is an alert.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
export STACK_DIR
exec python3 - "$@" <<'PY'
import glob, json, os, shutil, subprocess, sys, time

STACK = os.environ["STACK_DIR"]
ENV = os.path.join(STACK, ".env")
ALERT = os.environ.get("COMIC_ALERT") or os.path.join(STACK, "scripts", "alert.sh")
LIBRARY_HOST = os.path.join(STACK, "library", "books")
WORK = os.path.join(STACK, "library", "staging", "kcc")
KINDLE_OUT = os.path.join(STACK, "library", "staging", "kindle-comics")
CALIBREDB = "/app/calibre/calibredb"
LIBRARY = "/calibre-library"
LOCK = os.environ.get("COMIC_METAPUSH_LOCK", "/run/lock/bookstack-metapush.lock")

def envget(key, default=""):
    try:
        for line in open(ENV, encoding="utf-8"):
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].rstrip("\n")
                if len(v) >= 2 and v[0] == v[-1] == "'":
                    v = v[1:-1].replace("'\\''", "'")
                return v
    except OSError:
        pass
    return default

if envget("COMICS_ENABLED") != "true" and os.environ.get("COMICS_ENABLED") != "true":
    sys.exit(0)
UID = envget("PUID", "1000") or "1000"
GID = envget("PGID", "1000") or "1000"
IMG = envget("IMG_KCC") or "ghcr.io/ciromattia/kcc:v12.0.0"
MEM = envget("KCC_MEMORY") or "1536m"          # KCC measured ~1.2 GiB peak on 268- and 700-page colour volumes (v6.0.1)
CPUS = envget("KCC_CPUS") or "1.5"
KOBO_PROFILE = envget("KCC_KOBO_PROFILE") or "KoLC"      # Kobo Libra Colour; a Clara Colour shows it scaled
KINDLE_PROFILE = envget("KCC_KINDLE_PROFILE") or "KCS"   # Kindle Colorsoft
# v6.0.1: one Kobo book per comic, never split (KCC splits past 400 MB by default and a split copy
# was refused); a volume whose Kobo copy would be larger than this gets none, and the admin is told why
try:
    KOBO_MAX_MB = int(envget("KCC_KOBO_MAX_MB") or "1024")
except ValueError:
    KOBO_MAX_MB = 1024

# v6.2: how a comic's pages are laid out (the portal chooses from ALL its pages, or the reader does:
# librarian/comics.py LAYOUTS). Only these, and only KCC profiles of real devices, are ever run.
LAYOUT_FLAGS = {
    "portrait": [],                          # no wide pages: nothing to cut or turn
    "spreads": ["-r", "2", "-c", "0"],       # each half, then the whole spread turned; no margin crop,
                                             # which cut uneven-margin spreads off the gutter (measured)
    "split": ["-r", "0", "-c", "0"],         # halves only (KCC's classic), cut on the gutter
    "rotate": ["-r", "1"],                   # a landscape book: turned, never cut (v6.1)
    "strip": ["-w"],                         # a webtoon
    "dailies": ["--maximizestrips"],         # every page a row of panels: two rows, no turning
}
# A Kindle copy is KCC's Send-to-Kindle format, which frames EVERY page of a book at one size taken
# from its source pages: a landscape book gets a landscape frame, and a page turned sideways inside
# it came out a narrow strip (measured: 646 x 916 of 1236 x 916, v6.1's -r 1), dailies made two rows
# a thumbnail. On a Kindle such pages stay whole and upright (up to 1920 px): turned to landscape,
# the Kindle shows them across its whole screen.
KINDLE_FLAGS = {"rotate": ["-r", "1", "--norotate"], "dailies": ["-r", "1", "--norotate"]}
PROFILES = {"KoLC", "KoCC", "KoC", "KoL", "KoS", "KoE", "KoF", "KoN", "KoGHD", "KoAO",
            "KCS", "KSCS", "KPW6", "KPW5", "KPW34", "K11", "KO", "KS", "KS3", "KV"}

def plan(row, default_profile):
    """(profile, colour, layout, upscale) for one row. A portal from before v6.2 sends only
    strip/landscape; a profile or layout this job does not know is never passed to KCC."""
    layout = row.get("layout")
    if layout not in LAYOUT_FLAGS:
        layout = "strip" if row.get("strip") else "rotate" if row.get("landscape") else "portrait"
    profile = row.get("profile") if row.get("profile") in PROFILES else default_profile
    colour = row.get("colour") is not False
    upscale = row.get("upscale") is True and layout not in ("strip", "dailies")
    return profile, colour, layout, upscale

class Final(RuntimeError):
    """A result another try cannot change (the Kobo copy is too large): recorded once, not retried."""

def run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def admin(*args):
    r = run(["docker", "exec", "-i", "librarian", "python", "-m", "admin_cli", *args])
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout or "no output")[:200]}

def alert(title, text):
    try:
        subprocess.run([ALERT, title, text, "high"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass

def calibredb(*args):
    return run(["docker", "exec", "-u", f"{UID}:{GID}", "-e", "HOME=/tmp", "calibre-web", CALIBREDB, *args,
                "--with-library", LIBRARY])

def machine_json(out):
    out = out or ""
    i = out.find("[")
    return json.loads(out[i:]) if i >= 0 else []

def book(book_id):
    r = calibredb("list", "--fields", "tags,formats", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        rows = machine_json(r.stdout)
    except ValueError:
        return None
    if not rows:
        return None
    tags = rows[0].get("tags") or []
    tags = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",") if t.strip()]
    return {"owners": sorted(t for t in tags if t.startswith("owner:")),
            "formats": sorted({os.path.splitext(f)[1].lstrip(".").upper() for f in rows[0].get("formats", [])})}

def own(path):
    """Hand a file or folder to PUID:PGID (KCC and the portal run as it). Best effort: a test run
    as an ordinary user cannot chown, and there the files are its own already."""
    try:
        os.chown(path, int(UID), int(GID))
    except PermissionError:
        pass

def safe_rel(rel):
    return rel and not rel.startswith("/") and ".." not in rel.split("/")

def kcc(src, outdir, profile, fmt, kind, title, extra=(), layout="portrait", colour=True, upscale=False, kindle=False):
    """One KCC run in a throwaway container: no network, PUID:PGID, capped. [output files].
    v6.0.1: KCC sees the comic under a plain name of ours (comic.<ext>, alone in its folder): a
    library file whose name starts with '-' was read by KCC's 7-Zip as an option ('Extraction
    failed, install specialized extraction software'), and the folder held every other file of
    the book too. A hard link where the disk allows it (no copy of a 2 GB omnibus), else a copy."""
    os.makedirs(outdir, exist_ok=True)
    own(outdir)
    indir = outdir.rstrip("/") + "-in"
    shutil.rmtree(indir, ignore_errors=True)
    os.makedirs(indir)
    plain = os.path.join(indir, "comic" + os.path.splitext(src)[1].lower())
    try:
        os.link(src, plain)
    except OSError:
        shutil.copyfile(src, plain)
    own(indir)
    try:
        args = ["docker", "run", "--rm", "--network", "none", "--user", f"{UID}:{GID}",
                "--cpus", CPUS, "--memory", MEM, "--memory-swap", MEM,
                "-v", f"{indir}:/in:ro", "-v", f"{outdir}:/out", IMG,
                "-p", profile, "-f", fmt, "-t", title, "-o", "/out"]
        if colour:                             # v6.2: greyscale for a black-and-white e-reader (smaller, e-ink tuned)
            args.append("--forcecolor")
        if fmt == "EPUB":
            args.append("--nokepub")
        if kind == "manga":
            args.append("-m")                  # right to left: the halves of a spread in manga order
        args += (KINDLE_FLAGS.get(layout) if kindle else None) or LAYOUT_FLAGS[layout]   # v6.2: every page shape
        if upscale:
            args.append("-u")                  # a low-resolution scan: KCC resizes it (sharper than the device)
        args += list(extra) + [f"/in/{os.path.basename(plain)}"]
        r = run(args, timeout=3600)            # a 700-page colour volume took ~4 min; 2 GB, about 12
    finally:
        shutil.rmtree(indir, ignore_errors=True)
    made = sorted(glob.glob(os.path.join(outdir, "*.epub")))
    if r.returncode != 0 or not made:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        oom = r.returncode == 137
        raise RuntimeError("KCC ran out of memory (KCC_MEMORY)" if oom else ("KCC failed: " + " | ".join(tail))[:250])
    return made

def flock_metapush():
    """calibredb writes go one at a time with scripts/metadata-push.sh (and mem-tidy's restart)."""
    import fcntl
    fd = open(LOCK, "w")
    for _ in range(120):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            time.sleep(1)
    fd.close()
    return None

# ---- Kobo copies ---------------------------------------------------------------------------------
kobo_ok = kobo_fail = 0
due = admin("comics", "kobo-due", "--limit", "1")
for row in (due.get("rows") or []) if due.get("ok") else []:
    bid, rel = int(row["calibre_id"]), row.get("rel") or ""
    src = os.path.join(LIBRARY_HOST, rel)
    out = os.path.join(WORK, f"kobo-{bid}")
    shutil.rmtree(out, ignore_errors=True)
    why = ""
    try:
        if not safe_rel(rel) or not os.path.isfile(src) or os.path.islink(src):
            raise RuntimeError("refused: the comic's file is not where Calibre says")
        before = book(bid)
        if before is None:
            raise RuntimeError("could not read the book in Calibre")
        need = 3 * os.path.getsize(src) + 2 * 1024 ** 3     # the Kobo copy, its copy in the container, the library
        if shutil.disk_usage(WORK if os.path.isdir(WORK) else STACK).free < need:
            print(f"comic-convert: book {bid}: not enough free disk for its Kobo copy yet; later")
            continue                                        # not a failure: tried again next run
        profile, colour, layout, upscale = plan(row, KOBO_PROFILE)
        made = kcc(src, out, profile, "EPUB", row.get("kind"), row.get("title") or "Comic",
                   ("-b", "0"), layout=layout, colour=colour, upscale=upscale)
        if len(made) > 1:
            raise RuntimeError("KCC split it into several files although asked not to: not added")
        mb = os.path.getsize(made[0]) >> 20
        if mb > KOBO_MAX_MB:
            raise Final(f"its Kobo copy would be {mb} MB, over KCC_KOBO_MAX_MB ({KOBO_MAX_MB} MB): no Kobo copy; "
                        f"readers can still download the CBZ")
        name = os.path.splitext(os.path.basename(src))[0] + ".kepub"
        inside = f"/tmp/bookstack-kcc-{bid}.kepub"
        if run(["docker", "cp", made[0], f"calibre-web:{inside}"], timeout=300).returncode != 0:
            raise RuntimeError("could not hand the file to the library container")
        run(["docker", "exec", "calibre-web", "chown", f"{UID}:{GID}", inside], timeout=30)
        lock = flock_metapush()
        try:
            # remake (v6.1, 'Remake Kobo copy'): the new copy replaces the old one
            r = calibredb("add_format", *([] if row.get("remake") else ["--dont-replace"]), str(bid), inside)
        finally:
            if lock:
                lock.close()
        run(["docker", "exec", "calibre-web", "rm", "-f", inside], timeout=30)
        after = book(bid)
        if after is None or after["owners"] != before["owners"]:
            alert("Bookstack: adding a comic's Kobo copy CHANGED its owner tags",
                  f"Calibre book {bid}: {before['owners']} -> {(after or {}).get('owners')}.")
            raise RuntimeError("the owner tags changed: stopped")
        if r.returncode != 0 or "KEPUB" not in after["formats"]:
            raise RuntimeError(("calibredb add_format: " + (r.stderr or r.stdout or "no KEPUB afterwards"))[-250:])
        admin("comics", "kobo-result", str(bid), "ok", "--made",
              json.dumps({"profile": profile, "colour": colour, "layout": layout, "upscale": upscale}))
        kobo_ok += 1
    except Exception as e:
        why = str(e)
        final = isinstance(e, Final)
        res = admin("comics", "kobo-result", str(bid), "final" if final else "fail", "--reason", why)
        if res.get("status") == "failed":
            alert("Bookstack: a comic's Kobo copy could not be made",
                  f"Calibre book {bid} ({row.get('title')}): {why}." + ("" if final else " Tried three times; readers can "
                  "still download the CBZ.") + " Library -> Comics shows the settings.")
        kobo_fail += 1
    finally:
        shutil.rmtree(out, ignore_errors=True)

# ---- Kindle copies (made when a comic is sent, never stored) ------------------------------------
kindle_ok = kindle_fail = 0
due = admin("comics", "kindle-due", "--limit", "1")
for row in (due.get("rows") or []) if due.get("ok") else []:
    job, rel = int(row["job"]), row.get("rel") or ""
    src = os.path.join(LIBRARY_HOST, rel)
    limit = int(row.get("max_mb") or 45) * 1024 * 1024
    out = os.path.join(WORK, f"kindle-{job}")
    dest = os.path.join(KINDLE_OUT, str(job))
    try:
        if not safe_rel(rel) or not os.path.isfile(src) or os.path.islink(src):
            raise RuntimeError("refused: the comic's file is not where Calibre says")
        title = row.get("title") or "Comic"
        profile, colour, layout, upscale = plan(row, KINDLE_PROFILE)
        made = []
        # 1. KCC's Send-to-Kindle format; 2. the same at lower image quality; 3. split into parts
        for fmt, extra in (("KFX", ()), ("KFX", ("--jpeg-quality", "75")),
                           ("EPUB", ("--targetsize", str(max(10, int(row.get("max_mb") or 45) * 85 // 100)), "-b", "1"))):
            shutil.rmtree(out, ignore_errors=True)
            made = kcc(src, out, profile, fmt, row.get("kind"), title, extra,
                       layout=layout, colour=colour, upscale=upscale, kindle=True)
            if all(os.path.getsize(f) <= limit for f in made):
                break
        else:
            raise RuntimeError("still over the Send-to-Kindle mail limit after splitting")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        names = []
        for i, f in enumerate(made, 1):
            name = f"{i:02d}.epub" if len(made) > 1 else "comic.epub"
            shutil.move(f, os.path.join(dest, name))
            own(os.path.join(dest, name))
            names.append(name)
        own(dest)
        own(KINDLE_OUT)
        admin("comics", "kindle-result", str(job), "ok", "--files", *names)
        kindle_ok += 1
    except Exception as e:
        admin("comics", "kindle-result", str(job), "fail", "--reason", str(e))
        shutil.rmtree(dest, ignore_errors=True)
        kindle_fail += 1
    finally:
        shutil.rmtree(out, ignore_errors=True)

if kobo_ok or kobo_fail or kindle_ok or kindle_fail:
    print(f"comic-convert: Kobo {kobo_ok} added, {kobo_fail} failed; Kindle {kindle_ok} made, {kindle_fail} failed")
PY
