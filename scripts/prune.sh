#!/usr/bin/env bash
# prune.sh — apply the retention policy to the backup repository: forget + prune (L15).
#
# Who runs it, and with which key:
#   * the nightly backup.sh, with its own key — when that key MAY delete (RESTIC_APPEND_ONLY is
#     not 1 in /etc/bookstack/restic.env). This is the old behaviour.
#   * the monthly bookstack-prune timer, with a SEPARATE key in /etc/bookstack/restic-prune.env —
#     when the nightly key is append-only and the admin chose to keep a prune key on the server.
#   * the admin's own computer, monthly, with a prune key that never touches the server — the
#     strong option: nothing on the server can delete a snapshot then.
#       RESTIC_PRUNE_ENV=./prune.env bash prune.sh
#     prune.env holds RESTIC_REPOSITORY, RESTIC_PASSWORD and (for s3:) a key that can delete.
#
# Retention: RESTIC_KEEP_DAILY/WEEKLY/MONTHLY from the environment, else $STACK_DIR/.env (on the
# server), else 7/4/6. pre-update snapshots (Operations -> Update) are kept 90 days, then the
# normal policy applies.
set -euo pipefail
PENV="${RESTIC_PRUNE_ENV:-/etc/bookstack/restic-prune.env}"
[ -f "$PENV" ] || { echo "prune: $PENV does not exist (repository, password and a key that can delete)" >&2; exit 1; }
# shellcheck source=/dev/null
set -a; . "$PENV"; set +a
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
R=(); grep -q -- '--retry-lock' <<< "$(restic backup --help 2>/dev/null)" && R=(--retry-lock 30m)
envget(){ local raw; raw=$({ grep -E "^$1=" "$STACK_DIR/.env" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
KEEP_DAILY=$(num RESTIC_KEEP_DAILY 7)
KEEP_WEEKLY=$(num RESTIC_KEEP_WEEKLY 4)
KEEP_MONTHLY=$(num RESTIC_KEEP_MONTHLY 6)

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
echo "prune: retention applied (daily $KEEP_DAILY, weekly $KEEP_WEEKLY, monthly $KEEP_MONTHLY)"
