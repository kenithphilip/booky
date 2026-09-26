#!/usr/bin/env bash
# Apply the portal's queued metadata to CALIBRE's database, so the family's devices show it.
# Installed by bookstack.sh as /etc/cron.d/bookstack-metapush (every 15 minutes).
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
import json, os, subprocess, sys

STACK = os.environ["STACK_DIR"]
ENV = os.path.join(STACK, ".env")
ALERT = os.path.join(STACK, "scripts", "alert.sh")
ALLOWED = ("title", "sort", "authors", "series", "series_index")   # rule 1, third copy
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
    return run(["docker", "exec", "-u", f"{UID}:{GID}", "calibre-web", CALIBREDB, *args,
                "--with-library", LIBRARY])

def owner_tags(book_id):
    r = calibredb("list", "--fields", "tags", "--search", f"id:{book_id}", "--for-machine")
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    tags = rows[0].get("tags", []) if rows else []
    return sorted(t for t in tags if t.startswith("owner:"))

def alert(title, body):
    if os.access(ALERT, os.X_OK):
        run([ALERT, title, body, "high"], timeout=60)
    print(f"ALERT: {title}: {body}", file=sys.stderr)

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
    for k, v in fields.items():
        argv += ["--field", f"{k}:{v}"]
    r = calibredb(*argv)
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

print(f"metadata push: {applied} applied, {failed} failed")
# Kuma's dead-man's switch for this job: "up" means the queue was read and worked through. A run
# that could not read the queue exits above without a beat, which Kuma reports after 45 minutes.
# An owner-tag change is "down" as well as the alert already sent.
push = os.environ.get("KUMA_PUSH") or os.path.join(STACK, "scripts", "kuma-push.sh")
if os.access(push, os.X_OK):
    run([push, "metapush", "down" if owner_moved else "up",
         f"{applied} applied, {failed} failed" + (" - an OWNER TAG CHANGED" if owner_moved else "")], timeout=30)
sys.exit(1 if failed else 0)
PY
