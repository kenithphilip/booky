#!/usr/bin/env bash
# Proves the backup is restorable AND usable: restores the latest snapshot to a temp dir,
# checks the consistent DB copies open and hold real data, and that the snapshot is recent.
# Reads /etc/bookstack/restic.env. Run by bookstack.sh (Operations -> Restore test) and by
# the monthly bookstack-restore-test.timer (a failure alerts via bookstack-alert@).
set -uo pipefail
set -a; . "${RESTIC_ENV:-/etc/bookstack/restic.env}"; set +a
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
# Next to the stack, on the same disk: /tmp is a small RAM-backed tmpfs on Debian 13.
tmp="$(mktemp -d "$(dirname "$STACK_DIR")/.bs-restore-test.XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
fail=0
R=(--retry-lock 30m)   # the nightly backup may hold the repository lock
ok(){ echo "  [ OK ] $1"; }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

age=$(restic "${R[@]}" snapshots --latest 1 --json 2>/dev/null | python3 -c '
import sys, json, re, datetime
s = re.sub(r"\.\d+", "", json.load(sys.stdin)[-1]["time"]).replace("Z", "+00:00")   # RFC3339 with nanoseconds
t = datetime.datetime.fromisoformat(s)
print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 3600))' 2>/dev/null)
if [ -z "$age" ]; then bad "no snapshot found"; elif [ "$age" -le 36 ]; then ok "latest snapshot is ${age} h old"; else bad "latest snapshot is ${age} h old (> 36 h: is the timer running?)"; fi

echo "Restoring latest snapshot to $tmp ..."
# Only the config and DB snapshots are needed to prove the backup is usable; the library and
# audiobook trees can be tens of GB and would not fit next to the live copy on a small VPS.
restic "${R[@]}" restore latest --target "$tmp" \
  --include "$STACK_DIR/docker-compose.yml" --include "$STACK_DIR/.env" --include "$STACK_DIR/caddy" \
  --include "$STACK_DIR/.backup-snap" --include "$STACK_DIR/authelia" --include "$STACK_DIR/librarian" \
  >/dev/null || { bad "restic restore failed"; echo "RESTORE TEST FAILED"; exit 1; }
r="$tmp$STACK_DIR"
for f in docker-compose.yml caddy/Caddyfile .env .backup-snap/MANIFEST; do
  [ -e "$r/$f" ] && ok "found $f" || bad "missing $f"
done

# the core databases must have a consistent copy whenever they exist on this server: without it
# a restore falls back to the raw file without its WAL (recent users, Kobo tokens lost)
for core in cwa/config/app.db cwa/config/cwa.db library/books/metadata.db librarian/state/librarian.db abs/config/absdatabase.sqlite; do
  [ -f "$STACK_DIR/$core" ] || continue
  if [ -f "$r/.backup-snap/MANIFEST" ] && cut -f2 "$r/.backup-snap/MANIFEST" | grep -qxF "$core"; then ok "consistent copy of $core in the snapshot"
  else bad "no consistent copy of $core in the snapshot (backup.sh could not snapshot it)"; fi
done

# every consistent DB copy must pass an integrity check; the two that matter most must hold data
if [ -f "$r/.backup-snap/MANIFEST" ]; then
  while IFS=$'\t' read -r snap rel; do
    [ -n "$snap" ] || continue
    res=$(python3 -c 'import sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0])' "$r/.backup-snap/$snap" 2>&1)
    [ "$res" = ok ] && ok "integrity: $rel" || bad "integrity: $rel -> $res"
    case "$rel" in
      cwa/config/app.db)
        n=$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True).execute("SELECT COUNT(*) FROM user").fetchone()[0])' "$r/.backup-snap/$snap" 2>/dev/null)
        [ "${n:-0}" -gt 0 ] && ok "app.db has $n user(s)" || bad "app.db has no users";;
      library/books/metadata.db)
        live=$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True).execute("SELECT COUNT(*) FROM books").fetchone()[0])' "$STACK_DIR/library/books/metadata.db" 2>/dev/null || echo 0)
        n=$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True).execute("SELECT COUNT(*) FROM books").fetchone()[0])' "$r/.backup-snap/$snap" 2>/dev/null)
        if [ "${live:-0}" = 0 ]; then ok "metadata.db: live library is empty, count check skipped"
        elif [ "${n:-0}" -ge 1 ]; then ok "metadata.db has $n book(s)"; else bad "metadata.db has no books while the live library has $live"; fi;;
    esac
  done < "$r/.backup-snap/MANIFEST"
fi

# a full restore goes in place into the stack directory: it needs the snapshot's size in free space
need=$(restic "${R[@]}" stats latest --mode restore-size --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["total_size"])' 2>/dev/null)
disk=$(df -Pk "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $2}')
if [ -n "$need" ] && [ -n "$disk" ]; then
  echo "  info: a full restore needs $(( need / 1073741824 )) GB; this disk holds $(( disk / 1048576 )) GB in total (a replacement server needs at least that much free)"
fi

echo
echo "To restore for real: bookstack.sh -> Operations -> 'Restore from backup' (pick a snapshot,"
echo "everything or config + databases only; stops the stack, restores in place, then copies"
echo ".backup-snap/* over the live DB files per MANIFEST)."
[ "$fail" = 0 ] && echo "RESTORE TEST PASSED" || { echo "RESTORE TEST FAILED ($fail)"; exit 1; }
