#!/usr/bin/env bash
# Apply the portal's queued metadata to CALIBRE's database, so the family's devices show it.
# Installed by bookstack.sh as /etc/cron.d/bookstack-metapush (every 2 minutes, under flock).
#
# Why a host job and not the portal: Kobo sync serves from Calibre's metadata.db, and the portal
# deliberately cannot write it — it mounts the library read-only and has no Docker socket. The
# portal decides and queues (librarian/worker.py queue_device_pushes); this applies, with the
# same calibredb that Calibre-Web Automated itself uses to import books.
#
# Three rules this file exists to keep:
#   1. ONLY title, sort (title sort), authors, series, series_index. The allowlist is enforced in the portal's
#      database layer, again on the way out, and a third time here. `tags` is never written:
#      owner:<user> is what keeps family members' libraries apart.
#   2. calibredb runs as PUID:PGID, never root. A root-owned metadata.db-wal or -shm would stop
#      Calibre-Web (which runs as that user) from writing its own database.
#   3. The owner tag is read BEFORE and AFTER every write, and a change is an alert, not a log
#      line. That is the isolation invariant checked at runtime, not just in the test suite.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
export STACK_DIR
exec python3 - "$@" <<'PY'
import json, os, subprocess, sys, tempfile, urllib.request
from urllib.parse import urlsplit

STACK = os.environ["STACK_DIR"]
ENV = os.path.join(STACK, ".env")
ALERT = os.path.join(STACK, "scripts", "alert.sh")
ALLOWED = ("title", "sort", "authors", "series", "series_index",   # rule 1, third copy
           "comments", "publisher", "pubdate", "languages", "identifiers", "cover_url")
# cover_url is fetched HERE and becomes Calibre's `cover`: only from the providers' image hosts,
# at most 8 MB, and only when the bytes really are an image (third copy of worker.COVER_HOSTS)
COVER_HOSTS = ("covers.openlibrary.org", "books.google.com", "books.googleusercontent.com",
               "i.gr-assets.com", "images.gr-assets.com", "assets.hardcover.app")
COVER_MAX = 8 * 1024 * 1024
MAGIC = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"GIF8": "gif", b"RIFF": "webp"}
CALIBREDB = "/app/calibre/calibredb"                           # off PATH inside the CWA image
LIBRARY = "/calibre-library"

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

UID = envget("PUID", "1000") or "1000"
GID = envget("PGID", "1000") or "1000"

def run(args, timeout=120):
    """argv, never a shell string: a book title is arbitrary text and must stay data."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def admin(*args):
    r = run(["docker", "exec", "-i", "librarian", "python", "-m", "admin_cli", *args])
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (r.stderr or r.stdout or "no output")[:200]}

def calibredb(*args):
    # HOME: run as PUID, calibredb finds /root/.config unwritable and prints "No write access to
    # /root/.config/calibre using a temporary dir instead" on STDOUT, ahead of the JSON — which
    # made every read here fail on the real CWA image (measured 2026-09-26; the harness stubs
    # docker and never saw it). machine_json() below also tolerates any such line.
    return run(["docker", "exec", "-u", f"{UID}:{GID}", "-e", "HOME=/tmp", "calibre-web", CALIBREDB, *args,
                "--with-library", LIBRARY])

def machine_json(out):
    """calibredb --for-machine output, from its first '[' — never trusting stdout to be clean."""
    out = out or ""
    i = out.find("[")
    return json.loads(out[i:]) if i >= 0 else []

def owner_tags(book_id):
    r = calibredb("list", "--fields", "tags", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        rows = machine_json(r.stdout)
    except ValueError:
        return None
    tags = rows[0].get("tags", []) if rows else []
    return sorted(t for t in tags if t.startswith("owner:"))

def alert(title, body):
    if os.access(ALERT, os.X_OK):
        run([ALERT, title, body, "high"], timeout=60)
    print(f"ALERT: {title}: {body}", file=sys.stderr)

def fetch_cover(url, pid):
    """(path inside the CWA container, None) or (None, reason). Downloaded by this host job,
    checked, copied into the container for calibredb to read."""
    p = urlsplit(url or "")
    if p.scheme != "https" or (p.hostname or "").lower() not in COVER_HOSTS:
        return None, "cover address not on the allowed image hosts"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bookstack-metadata-push/5"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            final = urlsplit(resp.geturl())
            if final.scheme != "https" or not ((final.hostname or "").endswith(".archive.org")
                                              or (final.hostname or "").lower() in COVER_HOSTS):
                return None, "the cover redirected somewhere unexpected"
            body = resp.read(COVER_MAX + 1)
    except Exception as e:
        return None, f"cover download failed ({type(e).__name__})"
    if len(body) > COVER_MAX or len(body) < 1000:
        return None, "cover too large or too small to be real"
    ext = next((x for m, x in MAGIC.items() if body.startswith(m)), None)
    if not ext:
        return None, "the cover is not an image"
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(body)
        local = f.name
    inside = f"/tmp/bookstack-cover-{pid}.{ext}"
    r = run(["docker", "cp", local, f"calibre-web:{inside}"], timeout=60)
    os.unlink(local)
    if r.returncode != 0:
        return None, "could not hand the cover to the library container"
    run(["docker", "exec", "calibre-web", "chown", f"{UID}:{GID}", inside], timeout=30)
    return inside, None

pending = admin("pushes", "pending")
if not pending.get("ok"):
    print(f"could not read the push queue: {pending.get('error')}", file=sys.stderr)
    sys.exit(1)

applied = failed = 0
owner_moved = False
for row in pending.get("rows", []):
    pid, bid = row["id"], row["calibre_id"]
    fields = {k: v for k, v in (row.get("fields") or {}).items() if k in ALLOWED}
    if not fields:
        admin("pushes", "result", str(pid), "ok")      # nothing left after the allowlist
        continue
    before = owner_tags(bid)
    if before is None:
        admin("pushes", "result", str(pid), "fail", "--reason", "could not read the book's tags")
        failed += 1
        continue
    argv = ["set_metadata", str(bid)]
    cover_tmp = None
    if "cover_url" in fields:
        cover_tmp, why = fetch_cover(fields.pop("cover_url"), pid)
        if cover_tmp:
            argv += ["--field", f"cover:{cover_tmp}"]
        elif not fields:
            admin("pushes", "result", str(pid), "fail", "--reason", why)
            failed += 1
            continue
    for k, v in fields.items():
        argv += ["--field", f"{k}:{v}"]
    r = calibredb(*argv)
    if cover_tmp:
        run(["docker", "exec", "calibre-web", "rm", "-f", cover_tmp], timeout=30)
    if r.returncode != 0:
        admin("pushes", "result", str(pid), "fail", "--reason", (r.stderr or r.stdout)[:250])
        failed += 1
        continue
    after = owner_tags(bid)
    if after != before:
        # The one outcome that must never be quiet: a metadata write moved a book between
        # family members' libraries. Report it with both values so it can be put back.
        owner_moved = True
        alert("Bookstack: a metadata update CHANGED a book's owner tag",
              f"Calibre book {bid}: owner tags were {before}, are now {after}. "
              f"Put them back in Calibre-Web (Edit metadata -> Tags) and stop this job "
              f"(rm /etc/cron.d/bookstack-metapush) until it is understood.")
        admin("pushes", "result", str(pid), "fail", "--reason", f"owner tag changed {before} -> {after}")
        failed += 1
        continue
    admin("pushes", "result", str(pid), "ok")
    applied += 1

# ---- second pass: L10 owner tags ---------------------------------------------------------
# For books whose FILE could not carry the owner tag (MOBI/AZW3/FB2/TXT/DJVU; comics CWA's
# Kindle fixer stripped). ONE operation, and the narrowest possible: add owner:<x> to a book
# that has NO owner tag at all. A book that already has one is refused, never "corrected" —
# that would be moving a book between family members' libraries. The one exception is a
# family share (share=1, librarian/share.py): a SECOND owner added to a book that already has
# one, so the next reader gets the family's copy instead of a new download; existing owners
# are never removed, and an untagged book is never adopted that way. `calibredb set_metadata
# --field tags:` REPLACES the whole list, so the list written is the current one plus the
# owner tag, and it is read back and compared: every other tag must be exactly as it was.
def all_tags(book_id):
    r = calibredb("list", "--fields", "tags", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        rows = machine_json(r.stdout)
    except ValueError:
        return None
    return sorted(rows[0].get("tags", [])) if rows else None

tagged = tag_failed = 0
tags_pending = admin("tags", "pending")
for row in (tags_pending.get("rows") or []) if tags_pending.get("ok") else []:
    pid, bid, owner, share = row["id"], row["calibre_id"], row["owner"], bool(row.get("share"))
    want_tag = f"owner:{owner}"
    before = all_tags(bid)
    if before is None:
        admin("tags", "result", str(pid), "fail", "--reason", "could not read the book's tags")
        tag_failed += 1
        continue
    if row.get("op") == "remove":
        # 'Remove from my library': THIS reader's tag goes, and nothing else
        if want_tag not in before:
            admin("tags", "result", str(pid), "ok")      # already gone
            tagged += 1
            continue
        keep = [t for t in before if t != want_tag]
        if any("," in t for t in keep):
            admin("tags", "result", str(pid), "fail", "--reason", "refused: a tag contains a comma")
            tag_failed += 1
            continue
        r = calibredb("set_metadata", str(bid), "--field", "tags:" + ",".join(keep))
        after = all_tags(bid)
        if r.returncode != 0 or after is None or sorted(after) != sorted(keep):
            if after is not None and sorted(after) != sorted(before) and sorted(after) != sorted(keep):
                owner_moved = True
                alert("Bookstack: removing a reader's tag changed other tags",
                      f"Calibre book {bid}: tags were {before}, are now {after} (wanted {keep}).")
            admin("tags", "result", str(pid), "fail", "--reason", (r.stderr or r.stdout or f"read back {after}")[:250])
            tag_failed += 1
            continue
        admin("tags", "result", str(pid), "ok")
        tagged += 1
        continue
    owners_now = [t for t in before if t.startswith("owner:")]
    if share:
        # family sharing: a SECOND owner for a book that already has one (share.py). Never the
        # first owner of an untagged book: that is an import in progress, not a family copy.
        if want_tag in before:
            admin("tags", "result", str(pid), "ok")      # already theirs: nothing to write
            tagged += 1
            continue
        if not owners_now:
            admin("tags", "result", str(pid), "fail", "--reason", "refused: a family share, but the book has no owner yet")
            tag_failed += 1
            continue
    elif owners_now:
        admin("tags", "result", str(pid), "fail", "--reason",
              f"refused: the book already has {owners_now}")
        tag_failed += 1
        continue
    if any("," in t for t in before) or "," in want_tag:
        admin("tags", "result", str(pid), "fail", "--reason", "refused: a tag contains a comma")
        tag_failed += 1
        continue
    r = calibredb("set_metadata", str(bid), "--field", "tags:" + ",".join(before + [want_tag]))
    after = all_tags(bid)
    if r.returncode != 0 or after is None:
        admin("tags", "result", str(pid), "fail", "--reason", (r.stderr or r.stdout or "no read-back")[:250])
        tag_failed += 1
        continue
    if sorted(after) != sorted(before + [want_tag]):
        alert("Bookstack: adding an owner tag changed other tags",
              f"Calibre book {bid}: tags were {before}, are now {after} (wanted {before + [want_tag]}). "
              f"Check it in Calibre-Web and stop this job (rm /etc/cron.d/bookstack-metapush) until understood.")
        owner_moved = True
        admin("tags", "result", str(pid), "fail", "--reason", f"tags changed unexpectedly: {after}")
        tag_failed += 1
        continue
    admin("tags", "result", str(pid), "ok")
    tagged += 1
failed += tag_failed

# ---- third pass: on-demand conversions ---------------------------------------------------
# Calibre's own ebook-convert, inside the CWA container, as PUID:PGID, one job at a time; the
# result is added to THE SAME book (add_format --dont-replace), so its owner tag, and so who can
# see it, cannot change — checked anyway, before and after, like every write here.
EBOOK_CONVERT = "/app/calibre/ebook-convert"
TARGETS = ("epub", "azw3", "mobi", "pdf", "txt", "docx", "fb2", "rtf")   # copy of config.CONVERT_TARGETS
converted = convert_failed = 0
conv = admin("converts", "pending")
for job in (conv.get("rows") or []) if conv.get("ok") else []:
    jid, bid, dst, rel = job["id"], job["calibre_id"], job["dst_fmt"], job["src_path"]
    if dst not in TARGETS or ".." in rel.split("/") or rel.startswith("/"):
        admin("converts", "result", str(jid), "fail", "--reason", "refused: not an allowed conversion")
        convert_failed += 1
        continue
    before = owner_tags(bid)
    out = f"/tmp/bookstack-convert-{jid}.{dst}"
    r = run(["docker", "exec", "-u", f"{UID}:{GID}", "-e", "HOME=/tmp", "calibre-web", EBOOK_CONVERT,
             f"{LIBRARY}/{rel}", out], timeout=900)
    ok = r.returncode == 0
    if ok:
        r = calibredb("add_format", "--dont-replace", str(bid), out)
        ok = r.returncode == 0
    run(["docker", "exec", "calibre-web", "rm", "-f", out], timeout=30)
    after = owner_tags(bid)
    if before is not None and after != before:
        owner_moved = True
        alert("Bookstack: a conversion CHANGED a book's owner tag", f"Calibre book {bid}: {before} -> {after}.")
        ok = False
    if ok:
        admin("converts", "result", str(jid), "ok")
        converted += 1
    else:
        admin("converts", "result", str(jid), "fail", "--reason", ((r.stderr or r.stdout or "conversion failed")[-250:]))
        convert_failed += 1
failed += convert_failed

# ---- fourth pass: 'Find a better copy' ---------------------------------------------------
# A reader (or the admin) asked for a better file of a book, and an EPUB of it arrived: the
# portal staged it in library/staging/replace/<job>.epub. It goes into THE SAME Calibre book
# (calibredb add_format replaces the EPUB), so its owners, cover, corrected metadata and the
# Kobo's identity of the book all stay; every other format was made from the old file (the
# KEPUB for the Kobo, a MOBI kept from the import, a conversion) and is removed, to be made again
# from the new one. Tags read before and after, like every write here.
REPLACE_DIR = os.path.join(STACK, "library", "staging", "replace")

def book_formats(book_id):
    r = calibredb("list", "--fields", "formats", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        rows = machine_json(r.stdout)
    except ValueError:
        return None
    if not rows:
        return None
    return sorted({os.path.splitext(f)[1].lstrip(".").upper() for f in rows[0].get("formats", [])})

replaced = replace_failed = 0
reps = admin("replaces", "pending")
for job in (reps.get("rows") or []) if reps.get("ok") else []:
    jid, bid = int(job["id"]), int(job["calibre_id"])
    host = os.path.join(REPLACE_DIR, f"{jid}.epub")
    if job.get("fmt") != "epub" or not os.path.isfile(host) or os.path.islink(host):
        admin("replaces", "result", str(jid), "fail", "--reason", "refused: the staged EPUB is missing")
        replace_failed += 1
        continue
    before, fmts = all_tags(bid), book_formats(bid)
    if before is None or fmts is None:
        admin("replaces", "result", str(jid), "fail", "--reason", "could not read the book (tags or formats)")
        replace_failed += 1
        continue
    inside = f"/tmp/bookstack-replace-{jid}.epub"
    ok = run(["docker", "cp", host, f"calibre-web:{inside}"], timeout=120).returncode == 0
    why = "could not hand the file to the library container"
    if ok:
        run(["docker", "exec", "calibre-web", "chown", f"{UID}:{GID}", inside], timeout=30)
        r = calibredb("add_format", str(bid), inside)          # replaces the book's EPUB
        ok, why = r.returncode == 0, (r.stderr or r.stdout or "add_format failed")[-250:]
    if ok:
        for f in fmts:
            if f != "EPUB":
                calibredb("remove_format", str(bid), f)       # made from the old file
    run(["docker", "exec", "calibre-web", "rm", "-f", inside], timeout=30)
    after, fmts_after = all_tags(bid), book_formats(bid)
    if after != before:
        owner_moved = True
        alert("Bookstack: replacing a book's file CHANGED its tags",
              f"Calibre book {bid}: tags were {before}, are now {after}. Check it in Calibre-Web.")
        ok, why = False, f"tags changed unexpectedly: {after}"
    elif ok and fmts_after != ["EPUB"]:
        ok, why = False, f"formats after the swap: {fmts_after}"
    if ok:
        admin("replaces", "result", str(jid), "ok")
        os.remove(host)
        replaced += 1
    else:
        admin("replaces", "result", str(jid), "fail", "--reason", why)
        replace_failed += 1
failed += replace_failed

# ---- fifth pass: books no reader has any more ----------------------------------------------
# The portal counted them down (LIBRARY_RELEASE_DAYS after the last reader removed it, or after
# every owner's account was removed) and hands over each with the owner tags it saw then. It
# is deleted from Calibre ONLY if those are still exactly its owner tags: a book someone got
# again in the meantime (a share, a new request) is refused and kept. Its seedbox copy, if any,
# is untouched; asking for it again brings it back.
def book_exists(book_id):
    r = calibredb("list", "--fields", "title", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        return bool(machine_json(r.stdout))
    except ValueError:
        return None

deleted = delete_failed = 0
due = admin("releases", "due")
for row in (due.get("rows") or []) if due.get("ok") else []:
    bid, expect = int(row["calibre_id"]), sorted(row.get("tags") or [])
    there = book_exists(bid)
    if there is False:
        admin("releases", "result", str(bid), "ok")          # already gone (deleted by hand)
        deleted += 1
        continue
    now_owners = owner_tags(bid)
    if there is None or now_owners is None:
        admin("releases", "result", str(bid), "fail", "--reason", "could not read the book")
        delete_failed += 1
        continue
    if now_owners != expect:
        admin("releases", "result", str(bid), "fail", "--reason", f"refused: its owners are now {now_owners}")
        continue                                              # kept: someone has it again
    r = calibredb("remove", "--permanent", str(bid))
    if r.returncode == 0 and book_exists(bid) is False:
        admin("releases", "result", str(bid), "ok")
        deleted += 1
    else:
        admin("releases", "result", str(bid), "fail", "--reason", (r.stderr or r.stdout or "still there")[-250:])
        delete_failed += 1
failed += delete_failed

print(f"metadata push: {applied} applied, {failed} failed")
if replaced or replace_failed:
    print(f"better copies: {replaced} swapped in, {replace_failed} failed")
if deleted or delete_failed:
    print(f"books no reader has: {deleted} deleted from the server, {delete_failed} failed")
if converted or convert_failed:
    print(f"conversions: {converted} made, {convert_failed} failed")
if tagged or tag_failed:
    print(f"owner tags: {tagged} added, {tag_failed} failed")
# Kuma's dead-man's switch for this job: "up" means the queue was read and worked through. A run
# that could not read the queue exits above without a beat, which Kuma reports after 45 minutes.
# An owner-tag change is "down" as well as the alert already sent.
push = os.environ.get("KUMA_PUSH") or os.path.join(STACK, "scripts", "kuma-push.sh")
if os.access(push, os.X_OK):
    run([push, "metapush", "down" if owner_moved else "up",
         f"{applied} applied, {failed} failed" + (" - an OWNER TAG CHANGED" if owner_moved else "")], timeout=30)
sys.exit(1 if failed else 0)
PY
