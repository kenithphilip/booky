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
ok(){ echo "  [ OK ] $1"; }
bad(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

age=$(restic snapshots --latest 1 --json 2>/dev/null | python3 -c '
import sys, json, re, datetime
s = re.sub(r"\.\d+", "", json.load(sys.stdin)[-1]["time"]).replace("Z", "+00:00")   # RFC3339 with nanoseconds
t = datetime.datetime.fromisoformat(s)
print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 3600))' 2>/dev/null)
if [ -z "$age" ]; then bad "no snapshot found"; elif [ "$age" -le 36 ]; then ok "latest snapshot is ${age} h old"; else bad "latest snapshot is ${age} h old (> 36 h: is the timer running?)"; fi

echo "Restoring latest snapshot to $tmp ..."
# Only the config and DB snapshots are needed to prove the backup is usable; the library and
# audiobook trees can be tens of GB and would not fit next to the live copy on a small VPS.
restic restore latest --target "$tmp" \
  --include "$STACK_DIR/docker-compose.yml" --include "$STACK_DIR/.env" --include "$STACK_DIR/caddy" \
  --include "$STACK_DIR/.backup-snap" --include "$STACK_DIR/authelia" --include "$STACK_DIR/librarian" \
  >/dev/null || { bad "restic restore failed"; echo "RESTORE TEST FAILED"; exit 1; }
r="$tmp$STACK_DIR"
for f in docker-compose.yml caddy/Caddyfile .env .backup-snap/MANIFEST; do
  [ -e "$r/$f" ] && ok "found $f" || bad "missing $f"
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

echo
echo "To restore for real: bookstack.sh -> Operations -> 'Restore from backup' (stops the stack,"
echo "copies the tree back, then copies .backup-snap/* over the live DB files per MANIFEST)."
[ "$fail" = 0 ] && echo "RESTORE TEST PASSED" || { echo "RESTORE TEST FAILED ($fail)"; exit 1; }
