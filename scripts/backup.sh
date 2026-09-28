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
R=(); grep -q -- '--retry-lock' <<< "$(restic backup --help 2>/dev/null)" && R=(--retry-lock 30m)
envget(){ local raw; raw=$({ grep -E "^$1=" "$STACK_DIR/.env" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
PING_URL=$(envget BACKUP_PING_URL)
# Retention (RESTIC_KEEP_DAILY/WEEKLY/MONTHLY, from the environment or $STACK_DIR/.env) is read
# by scripts/prune.sh, which applies it — here when this key may delete, else elsewhere (L15).
# num() stays: RESTIC_CHECK_GROUPS below uses it. A non-numeric value falls back to the default.
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
finish(){
  local rc=$?
  if [ -n "$PING_URL" ]; then
    if [ "$rc" = 0 ]; then curl -fsS -m 10 --retry 3 "$PING_URL" >/dev/null 2>&1 || true
    else curl -fsS -m 10 --retry 3 "${PING_URL%/}/fail" >/dev/null 2>&1 || true; fi
  fi
  # Kuma's "Backup (nightly)" push monitor: success only. A failed run is already an alert
  # (the unit's OnFailure=); silence past 26 h is Kuma's to report.
  [ "$rc" = 0 ] && { "${KUMA_PUSH:-$STACK_DIR/scripts/kuma-push.sh}" backup up "snapshot saved" >/dev/null 2>&1 || true; }
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
# backup.state carries the weekly-verification counter: without it a rebuilt server restarts at
# group 1 and re-reads what the old one already verified while the rest waits another year.
#
# /etc/bookstack/restic.env is DELIBERATELY NOT in this list. $SNAP lives under $STACK_DIR and
# $STACK_DIR is the backup root with no --exclude for it, so copying restic.env here put
# RESTIC_PASSWORD - and, for an s3:/B2 repository, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
# - INSIDE every snapshot: the key that decrypts the repository, and the bucket credentials
# that can prune and delete it, stored in the thing they protect. That is the one secret
# bookstack.sh tells the admin to keep off this server ("Keep these OFF this server"), and
# rotating RESTIC_PASSWORD would not have revoked it, because the bucket key is unchanged and
# the old snapshots stay readable with the old password. A redacted stub goes in instead, so a
# rebuilt server still knows WHICH repository to open; the password and the object-store keys
# come from the password manager. scripts/restore-test.sh asserts both halves of this.
mkdir -p "$SNAP/host/etc/bookstack"
{ echo "# REDACTED COPY - written by scripts/backup.sh, NOT the live file."
  echo "# RESTIC_PASSWORD, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are deliberately absent:"
  echo "# a snapshot must never carry the key that decrypts it or the credentials that can"
  echo "# delete it. Take them from your password manager and add them back by hand, then"
  echo "# bookstack.sh -> Install -> Backups to re-write the real /etc/bookstack/restic.env."
  # a rest: address carries the home server's login (user:password@): never in a snapshot either
  echo "RESTIC_REPOSITORY=$(printf '%s' "${RESTIC_REPOSITORY:-}" | sed -E 's#^(rest:https?://)[^@/]+@#\1REDACTED@#')"
  echo "RESTIC_APPEND_ONLY=${RESTIC_APPEND_ONLY:-0}"
  echo "STACK_DIR=$STACK_DIR"
} > "$SNAP/host/etc/bookstack/restic.env"
for p in /etc/bookstack/backup.state /etc/fail2ban/jail.local /etc/fail2ban/filter.d/caddy-auth.conf \
         /etc/fail2ban/filter.d/caddy-device-auth.conf /etc/fail2ban/filter.d/caddy-abs-login.conf \
         /etc/ssh/sshd_config.d/01-bookstack.conf \
         /etc/sysctl.d/90-bookstack.conf /etc/docker/daemon.json /etc/cron.d/bookstack-cfips /etc/cron.d/bookstack-disk; do
  [ -f "$p" ] && { mkdir -p "$SNAP/host$(dirname "$p")"; cp -p "$p" "$SNAP/host$p"; }
done
mkdir -p "$SNAP/host/etc/systemd/system"; cp -p /etc/systemd/system/bookstack-* "$SNAP/host/etc/systemd/system/" 2>/dev/null || true
command -v ufw >/dev/null && ufw status verbose > "$SNAP/host/ufw-status.txt" 2>/dev/null || true
chmod -R go-rwx "$SNAP"

# Same key=value state file idiom as /etc/bookstack/disk.state, kept beside restic.env so it
# survives a redeploy (copy_code_trees overwrites everything under $STACK_DIR/scripts).
BSTATE="${BACKUP_STATE:-$(dirname "${RESTIC_ENV:-/etc/bookstack/restic.env}")/backup.state}"
# The `|| true` is not decoration: with `set -e -o pipefail` a grep that finds nothing (1) or
# cannot open the file yet (2) becomes the pipeline's status and ends the whole backup.
bstate_get(){ { grep -E "^$1=" "$BSTATE" 2>/dev/null || true; } | head -1 | cut -d= -f2-; }
bstate_set(){ # a counter we cannot persist would restart at 1 every week: say so, loudly
  if ! { mkdir -p "$(dirname "$BSTATE")" && { grep -vE "^$1=" "$BSTATE" 2>/dev/null || true; echo "$1=$2"; } > "$BSTATE.tmp" && mv "$BSTATE.tmp" "$BSTATE"; } 2>/dev/null; then
    echo "WARNING: could not write $BSTATE; the weekly verification will keep re-reading group 1 instead of rotating through the repository." >&2
  fi
}

restic "${R[@]}" cat config >/dev/null 2>&1 || {
  echo "ERROR: the restic repository $RESTIC_REPOSITORY is not reachable or not initialised." >&2
  echo "       Not creating one here (an unmounted local path would get a new repo on the root disk)." >&2
  echo "       Check the mount / network / keys, or re-run bookstack.sh -> Install -> Backups." >&2
  exit 1; }
# L15: the snapshot the LAST run wrote must still be there. Every retention policy keeps the
# newest snapshot, so nothing legitimate removes it: gone means someone with a key that can
# delete (or, on B2, hide) has been at the repository. Alert at once — on B2 with a key that
# lacks deleteFiles, hidden files are recoverable until the bucket's lifecycle rule expires them.
last=$(bstate_get last_snapshot)
if [ -n "$last" ]; then
  have=$(restic "${R[@]}" snapshots --json 2>/dev/null | python3 -c '
import sys, json
print("yes" if sys.argv[1] in {x.get("id") for x in (json.load(sys.stdin) or [])} else "no")' "$last" 2>/dev/null || echo "?")
  if [ "$have" = no ]; then
    echo "WARNING: snapshot $last, written by the previous backup, is no longer in the repository" >&2
    "${BACKUP_ALERT:-$STACK_DIR/scripts/alert.sh}" "Bookstack: a backup snapshot has DISAPPEARED" \
"Snapshot ${last:0:8}, written by the previous nightly backup, is no longer in $RESTIC_REPOSITORY. No retention policy removes the newest snapshot, so something with a key that can delete (or hide) has touched the repository.

Do not prune. On Backblaze B2, hidden files can be restored (b2 ls --versions, then unhide) until the bucket's lifecycle rule removes them. Then rotate the bucket keys and look at who had them." high >/dev/null 2>&1 || true
  fi
fi
# Re-downloadable data and caches are not worth the repository space.
restic "${R[@]}" backup "$STACK_DIR" \
  --exclude "$STACK_DIR/downloads" \
  --exclude "$STACK_DIR/caddy/data/access.log*" \
  --exclude "$STACK_DIR/cwa/config/processed_books" \
  --exclude "$STACK_DIR/library/staging" \
  --exclude "$STACK_DIR/library/seedbox" \
  --exclude "$STACK_DIR/library/seedbox-sync" \
  --exclude "$STACK_DIR/ephemera/downloads" \
  --exclude "$STACK_DIR/abs/metadata/cache" \
  --exclude "$STACK_DIR/abs/metadata/logs" \
  --exclude '*.db-wal' --exclude '*.db-shm' --exclude '*.sqlite-wal' --exclude '*.sqlite-shm' \
  --tag bookstack ${extra_tag:+--tag "$extra_tag"}

# Verify before pruning: a structural check once a week, plus one 52nd of the pack data.
# --read-data-subset=5% picked its 5 % AT RANDOM every week. After a year ~92 % of packs have
# been read *in expectation* and no particular pack is guaranteed ever to have been, so silent
# bit rot in a cold pack can survive indefinitely. This repository holds the only copy of the
# family's uploaded books and of the owner:<user> tags the whole isolation model rests on.
# The n/t form reads the n-th of t equal groups, so a counter rotating 1..52 reads every byte
# exactly once a year — and weekly transfer DROPS from 5 % to ~1.9 %. (n/t is also the oldest
# spelling of the flag: restic 0.14 on Debian 12 accepts it, unlike --read-data-subset=<size>.)
# The counter advances only after a check that PASSED: a failure stops this script (set -e)
# before the prune, and next week re-reads the same group instead of moving past it. A week the
# box is off is a delay, not a gap, for the same reason.
CHECK_GROUPS=$(num RESTIC_CHECK_GROUPS 52); [ "$CHECK_GROUPS" -ge 1 ] || CHECK_GROUPS=52
if [ "$(date +%u)" = "${BACKUP_CHECK_DOW:-7}" ]; then
  n=$(bstate_get check_group); case "$n" in ''|*[!0-9]*) n=1;; esac
  { [ "$n" -ge 1 ] && [ "$n" -le "$CHECK_GROUPS" ]; } || n=1
  restic "${R[@]}" check --read-data-subset="$n/$CHECK_GROUPS"
  bstate_set check_group "$(( n % CHECK_GROUPS + 1 ))"
fi
restic "${R[@]}" stats latest --json || true
new_snap=$(restic "${R[@]}" snapshots --json --path "$STACK_DIR" 2>/dev/null | python3 -c '
import sys, json
s = sorted(json.load(sys.stdin) or [], key=lambda x: x["time"])
print(s[-1]["id"] if s else "")' 2>/dev/null || true)
[ -n "$new_snap" ] && bstate_set last_snapshot "$new_snap"
# Retention. With an append-only key (RESTIC_APPEND_ONLY=1, set by Install -> Backups) nothing
# here may delete: scripts/prune.sh runs with a SEPARATE key, monthly from the bookstack-prune
# timer or from the admin's own computer. Otherwise this key prunes, as it always did.
if [ "${RESTIC_APPEND_ONLY:-0}" = 1 ]; then
  echo "append-only key: no forget/prune here (scripts/prune.sh applies retention with the separate prune key)"
else
  RESTIC_PRUNE_ENV="${RESTIC_ENV:-/etc/bookstack/restic.env}" "${PRUNE_SH:-$(dirname "${BASH_SOURCE[0]}")/prune.sh}"
fi

if [ -n "$snapfail" ]; then
  echo "BACKUP INCOMPLETE: no consistent copy of:$snapfail (the raw files are in the snapshot, possibly without their WAL)" >&2
  exit 1
fi
