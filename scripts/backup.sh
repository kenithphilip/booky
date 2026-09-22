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
set -euo pipefail
set -a; . "${RESTIC_ENV:-/etc/bookstack/restic.env}"; set +a
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
SNAP="$STACK_DIR/.backup-snap"
extra_tag=""; [ "${1:-}" = "--tag" ] && extra_tag="${2:-}"

rm -rf "$SNAP"; mkdir -p "$SNAP/host"; : > "$SNAP/MANIFEST"
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
dst.close(); src.close()
PY
    then printf '%s\t%s\n' "$name" "$rel" >> "$SNAP/MANIFEST"
    else echo "warning: could not snapshot $rel" >&2; fi
  done
done
# host state (small, root-only): needed to rebuild the server, not just the stack
for p in /etc/bookstack/restic.env /etc/fail2ban/jail.local /etc/fail2ban/filter.d/caddy-auth.conf \
         /etc/fail2ban/filter.d/caddy-device-auth.conf /etc/ssh/sshd_config.d/90-bookstack.conf \
         /etc/sysctl.d/90-bookstack.conf /etc/docker/daemon.json /etc/cron.d/bookstack-cfips /etc/cron.d/bookstack-disk; do
  [ -f "$p" ] && { mkdir -p "$SNAP/host$(dirname "$p")"; cp -p "$p" "$SNAP/host$p"; }
done
mkdir -p "$SNAP/host/etc/systemd/system"; cp -p /etc/systemd/system/bookstack-* "$SNAP/host/etc/systemd/system/" 2>/dev/null || true
command -v ufw >/dev/null && ufw status verbose > "$SNAP/host/ufw-status.txt" 2>/dev/null || true
chmod -R go-rwx "$SNAP"

restic snapshots >/dev/null 2>&1 || restic init
restic backup "$STACK_DIR" \
  --exclude "$STACK_DIR/downloads" \
  --exclude "$STACK_DIR/caddy/data/access.log*" \
  --exclude "$STACK_DIR/cwa/config/processed_books" \
  --exclude "$STACK_DIR/library/staging" \
  --exclude '*.db-wal' --exclude '*.db-shm' --exclude '*.sqlite-wal' --exclude '*.sqlite-shm' \
  --tag bookstack ${extra_tag:+--tag "$extra_tag"}

# Verify before pruning: a structural check every night, 5 % of the data re-read on Sundays.
restic check
[ "$(date +%u)" = 7 ] && restic check --read-data-subset=5%
restic stats latest --json || true
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-tag pre-update --prune
