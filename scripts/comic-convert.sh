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
#   Kindle: a comic sent to a Kindle is converted when it is sent (KCC's "Send to Kindle" EPUB, the
#           Colorsoft profile), made to fit the mail limit, left in library/staging/kindle-comics/<job>/
#           for the portal to mail, and never stored in the library.
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
MEM = envget("KCC_MEMORY") or "1536m"          # KCC measured ~1.5 GiB peak on a 40-page colour issue
CPUS = envget("KCC_CPUS") or "1.5"
KOBO_PROFILE = envget("KCC_KOBO_PROFILE") or "KoLC"      # Kobo Libra Colour; a Clara Colour shows it scaled
KINDLE_PROFILE = envget("KCC_KINDLE_PROFILE") or "KCS"   # Kindle Colorsoft

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

def kcc(src, outdir, profile, fmt, kind, strip, title, extra=()):
    """One KCC run in a throwaway container: no network, PUID:PGID, capped. [output files]."""
    os.makedirs(outdir, exist_ok=True)
    own(outdir)
    args = ["docker", "run", "--rm", "--network", "none", "--user", f"{UID}:{GID}",
            "--cpus", CPUS, "--memory", MEM, "--memory-swap", MEM,
            "-v", f"{os.path.dirname(src)}:/in:ro", "-v", f"{outdir}:/out", IMG,
            "-p", profile, "--forcecolor", "-f", fmt, "-t", title, "-o", "/out"]
    if fmt == "EPUB":
        args.append("--nokepub")
    if kind == "manga":
        args.append("-m")
    if strip:
        args.append("-w")
    args += list(extra) + [f"/in/{os.path.basename(src)}"]
    r = run(args, timeout=1800)
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
        made = kcc(src, out, KOBO_PROFILE, "EPUB", row.get("kind"), row.get("strip"), row.get("title") or "Comic")
        if len(made) > 1:
            raise RuntimeError("KCC split it into several files (over 400 MB): not added")
        name = os.path.splitext(os.path.basename(src))[0] + ".kepub"
        inside = f"/tmp/bookstack-kcc-{bid}.kepub"
        if run(["docker", "cp", made[0], f"calibre-web:{inside}"], timeout=300).returncode != 0:
            raise RuntimeError("could not hand the file to the library container")
        run(["docker", "exec", "calibre-web", "chown", f"{UID}:{GID}", inside], timeout=30)
        lock = flock_metapush()
        try:
            r = calibredb("add_format", "--dont-replace", str(bid), inside)
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
        admin("comics", "kobo-result", str(bid), "ok")
        kobo_ok += 1
    except Exception as e:
        why = str(e)
        res = admin("comics", "kobo-result", str(bid), "fail", "--reason", why)
        if res.get("status") == "failed":
            alert("Bookstack: a comic's Kobo copy could not be made",
                  f"Calibre book {bid} ({row.get('title')}): {why}. Tried three times; readers can still download "
                  f"the CBZ. Library -> Comics shows the settings.")
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
        made = []
        # 1. KCC's Send-to-Kindle format; 2. the same at lower image quality; 3. split into parts
        for fmt, extra in (("KFX", ()), ("KFX", ("--jpeg-quality", "75")),
                           ("EPUB", ("--targetsize", str(max(10, int(row.get("max_mb") or 45) * 85 // 100)), "-b", "1"))):
            shutil.rmtree(out, ignore_errors=True)
            made = kcc(src, out, KINDLE_PROFILE, fmt, row.get("kind"), row.get("strip"), title, extra)
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
