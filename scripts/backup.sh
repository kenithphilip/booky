#!/usr/bin/env bash
# Encrypted, deduplicated backup of the whole stack (configs + library) with restic.
# Reads /etc/bookstack/restic.env (written by bookstack.sh). Runs nightly via systemd timer;
# `backup.sh --tag pre-update` adds a tag (Operations -> Update takes one before touching images).
#
# Every app writes its SQLite database while we run (WAL mode), so the raw files in the
# snapshot may be inconsistent. Consistent copies are taken first with SQLite's online backup
# API into $STACK_DIR/.backup-snap/ (+ MANIFEST mapping copy -> live path); a restore copies
# those over the raw files (bookstack.sh -> Operations -> Restore from backup does it).
# Host state restic would otherwise never see is staged under .backup-snap/host/.
#
# The repository must already exist (Install -> Backups creates it): this script never runs
# `restic init`, so a local repository on a volume that failed to mount cannot silently be
# re-created on the root disk. BACKUP_PING_URL in $STACK_DIR/.env (healthchecks.io style) gets
# a ping after a good run and <url>/fail after a failed one.
set -euo pipefail
set -a; . "${RESTIC_ENV:-/etc/bookstack/restic.env}"; set +a
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
SNAP="$STACK_DIR/.backup-snap"
extra_tag=""; [ "${1:-}" = "--tag" ] && extra_tag="${2:-}"
# --retry-lock (wait for a concurrent restore test instead of failing) landed in restic 0.16.
# Debian 12 ships 0.14, which dies with "unknown flag" on EVERY call — i.e. every nightly backup.
# Probe for it the way bookstack.sh probes `restic restore --overwrite`.
R=(); restic backup --help 2>/dev/null | grep -q -- '--retry-lock' && R=(--retry-lock 30m)
envget(){ local raw; raw=$({ grep -E "^$1=" "$STACK_DIR/.env" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
PING_URL=$(envget BACKUP_PING_URL)
# Retention, tunable from the environment or $STACK_DIR/.env (Install -> Backups writes them) so
# an admin never has to edit this file — copy_code_trees restores it from the checkout on every
# Deploy and every Update, which would silently revert the edit. A non-numeric value falls back
# to the default rather than handing `restic forget` an argument it refuses.
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
KEEP_DAILY=$(num RESTIC_KEEP_DAILY 7)
KEEP_WEEKLY=$(num RESTIC_KEEP_WEEKLY 4)
KEEP_MONTHLY=$(num RESTIC_KEEP_MONTHLY 6)
finish(){
  local rc=$?
  if [ -n "$PING_URL" ]; then
    if [ "$rc" = 0 ]; then curl -fsS -m 10 --retry 3 "$PING_URL" >/dev/null 2>&1 || true
    else curl -fsS -m 10 --retry 3 "${PING_URL%/}/fail" >/dev/null 2>&1 || true; fi
  fi
  exit "$rc"
}
trap finish EXIT

rm -rf "$SNAP"; mkdir -p "$SNAP/host"; : > "$SNAP/MANIFEST"
snapfail=""
for db in cwa/config/app.db cwa/config/cwa.db library/books/metadata.db librarian/state/librarian.db \
          abs/config/absdatabase.sqlite kuma/data/kuma.db authelia/db.sqlite3 shelfmark/config/*.db; do
  for f in "$STACK_DIR"/$db; do                      # the shelfmark entry is a glob
    [ -f "$f" ] || continue
    rel="${f#"$STACK_DIR"/}"; name="${rel//\//_}"
    if python3 - "$f" "$SNAP/$name" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
# a self-contained single file: a WAL-flagged copy cannot be opened read-only (restore test)
dst.execute("PRAGMA journal_mode=DELETE")
dst.close(); src.close()
PY
    then printf '%s\t%s\n' "$name" "$rel" >> "$SNAP/MANIFEST"
    else echo "ERROR: could not snapshot $rel (the backup still runs; this run is reported as failed)" >&2; snapfail="$snapfail $rel"; fi
  done
done
# host state (small, root-only): needed to rebuild the server, not just the stack
for p in /etc/bookstack/restic.env /etc/fail2ban/jail.local /etc/fail2ban/filter.d/caddy-auth.conf \
         /etc/fail2ban/filter.d/caddy-device-auth.conf /etc/fail2ban/filter.d/caddy-abs-login.conf \
         /etc/ssh/sshd_config.d/01-bookstack.conf \
         /etc/sysctl.d/90-bookstack.conf /etc/docker/daemon.json /etc/cron.d/bookstack-cfips /etc/cron.d/bookstack-disk; do
  [ -f "$p" ] && { mkdir -p "$SNAP/host$(dirname "$p")"; cp -p "$p" "$SNAP/host$p"; }
done
mkdir -p "$SNAP/host/etc/systemd/system"; cp -p /etc/systemd/system/bookstack-* "$SNAP/host/etc/systemd/system/" 2>/dev/null || true
command -v ufw >/dev/null && ufw status verbose > "$SNAP/host/ufw-status.txt" 2>/dev/null || true
chmod -R go-rwx "$SNAP"

restic "${R[@]}" cat config >/dev/null 2>&1 || {
  echo "ERROR: the restic repository $RESTIC_REPOSITORY is not reachable or not initialised." >&2
  echo "       Not creating one here (an unmounted local path would get a new repo on the root disk)." >&2
  echo "       Check the mount / network / keys, or re-run bookstack.sh -> Install -> Backups." >&2
  exit 1; }
# Re-downloadable data and caches are not worth the repository space.
restic "${R[@]}" backup "$STACK_DIR" \
  --exclude "$STACK_DIR/downloads" \
  --exclude "$STACK_DIR/caddy/data/access.log*" \
  --exclude "$STACK_DIR/cwa/config/processed_books" \
  --exclude "$STACK_DIR/library/staging" \
  --exclude "$STACK_DIR/ephemera/downloads" \
  --exclude "$STACK_DIR/abs/metadata/cache" \
  --exclude "$STACK_DIR/abs/metadata/logs" \
  --exclude '*.db-wal' --exclude '*.db-shm' --exclude '*.sqlite-wal' --exclude '*.sqlite-shm' \
  --tag bookstack ${extra_tag:+--tag "$extra_tag"}

# Verify before pruning: a structural check once a week, with 5 % of the data re-read.
if [ "$(date +%u)" = "${BACKUP_CHECK_DOW:-7}" ]; then
  restic "${R[@]}" check --read-data-subset=5%
fi
restic "${R[@]}" stats latest --json || true
# pre-update snapshots (Operations -> Update) are kept 90 days, then the normal policy applies
old_pre=$(restic "${R[@]}" snapshots --tag pre-update --json 2>/dev/null | python3 -c '
import sys, json, re, datetime
now = datetime.datetime.now(datetime.timezone.utc)
for x in json.load(sys.stdin) or []:
    t = datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", x["time"]).replace("Z", "+00:00"))
    if (now - t).days > 90: print(x["id"])' 2>/dev/null || true)
if [ -n "$old_pre" ]; then
  # shellcheck disable=SC2086
  restic "${R[@]}" tag --remove pre-update $old_pre >/dev/null
fi
restic "${R[@]}" forget --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" --keep-tag pre-update --prune

if [ -n "$snapfail" ]; then
  echo "BACKUP INCOMPLETE: no consistent copy of:$snapfail (the raw files are in the snapshot, possibly without their WAL)" >&2
  exit 1
fi
