#!/usr/bin/env bash
# Hourly disk watchdog (installed by bookstack.sh Deploy as /etc/cron.d/bookstack-disk).
#   >= DISK_WARN_PCT (85) used : alert once per 24 h
#   >= DISK_STOP_PCT (95) used : also stop the downloaders (shelfmark; qBittorrent when it runs)
#                                AND raise the pause flag the portal's own worker honours, alert high
#   <  DISK_RESUME_PCT (80)    : start again whatever THIS script stopped, and drop the flag
# The three thresholds come from the environment or $STACK_DIR/.env: hardcoding them meant an
# admin had to edit this file, which copy_code_trees overwrites on every Deploy and Update.
# Always: delete stale partials, trim journald, Docker build cache and restic's cache.
# A full disk wedges SQLite, CWA ingest, Caddy logging and swap at once; nothing restarts out of it.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
STATE="${DISK_STATE:-/etc/bookstack/disk.state}"
ENV_FILE="$STACK_DIR/.env"
ALERT="$STACK_DIR/scripts/alert.sh"
mkdir -p "$(dirname "$STATE")"; touch "$STATE"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
# environment wins over .env; a non-numeric value falls back to the default rather than making
# every `[ "$pct" -ge ... ]` below an error.
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
WARN_PCT=$(num DISK_WARN_PCT 85); STOP_PCT=$(num DISK_STOP_PCT 95); RESUME_PCT=$(num DISK_RESUME_PCT 80)
# The portal's own downloaders (queue, dropbox watcher, IMAP intake) run inside the librarian
# container and are not a compose service we can stop without taking the whole portal — including
# the admin dashboard that explains WHY — down with them. They watch for this flag instead.
# $STACK_DIR/library/staging is bind-mounted into librarian as /staging, so the portal sees it at
# /staging/.disk-paused. CONTRACT: the portal side checks that exact path.
PAUSE_FLAG="${DISK_PAUSE_FLAG:-$STACK_DIR/library/staging/.disk-paused}"
state_get(){ grep -E "^$1=" "$STATE" 2>/dev/null | head -1 | cut -d= -f2-; }
state_set(){ { grep -vE "^$1=" "$STATE" 2>/dev/null || true; echo "$1=$2"; } > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
torrents_on(){ [ "$(envget TORRENTS_ENABLED)" = true ]; }
compose(){ if torrents_on; then (cd "$STACK_DIR" && docker compose --profile torrents "$@"); else (cd "$STACK_DIR" && docker compose "$@"); fi; }
running(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

pct=$(df --output=pcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9); pct="${pct:-0}"
now=$(date +%s); last=$(state_get last_alert); last="${last:-0}"
free_h=$(df -h "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}')

if [ "$pct" -ge "$STOP_PCT" ]; then
  if [ "$(state_get paused)" != 1 ]; then
    stopped="shelfmark"; nostop=""
    compose stop shelfmark >/dev/null 2>&1 || nostop="shelfmark"
    if running qbittorrent; then
      if compose stop qbittorrent >/dev/null 2>&1; then stopped="$stopped, qbittorrent"; else nostop="${nostop:+$nostop, }qbittorrent"; fi
    fi
    state_set paused 1
    "$ALERT" "Disk ${pct}% full on $(hostname)" "Only $free_h free under $STACK_DIR. Downloaders stopped ($stopped)${nostop:+; COULD NOT stop: $nostop}. The portal's own imports are paused too. Free space, then they restart automatically below ${RESUME_PCT}%." high
    state_set last_alert "$now"
  fi
elif [ "$pct" -ge "$WARN_PCT" ]; then
  if [ $((now - last)) -ge 86400 ]; then
    "$ALERT" "Disk ${pct}% full on $(hostname)" "$free_h free under $STACK_DIR. At ${STOP_PCT}% the downloaders are stopped. Check Operations -> Self-test and library/audiobooks."
    state_set last_alert "$now"
  fi
elif [ "$pct" -lt "$RESUME_PCT" ] && [ "$(state_get paused)" = 1 ]; then
  # `up -d`, not `start`: `compose down` (Operations -> Update, Restore) REMOVES the container and
  # `start` cannot recreate it. And the latch is only cleared when every service really came back —
  # clearing it on a failed start left Shelfmark dead with an all-clear notification and no retry.
  started=""; nostart=""
  if compose up -d shelfmark >/dev/null 2>&1; then started="shelfmark"; else nostart="shelfmark"; fi
  if torrents_on; then
    if compose up -d qbittorrent >/dev/null 2>&1; then started="${started:+$started, }qbittorrent"; else nostart="${nostart:+$nostart, }qbittorrent"; fi
  fi
  if [ -z "$nostart" ]; then
    state_set paused 0; state_set resume_failed 0
    "$ALERT" "Disk back to ${pct}% on $(hostname)" "Downloaders started again ($started)."
  else
    # paused stays 1: the next hourly run tries again instead of leaving a dead container behind.
    if [ "$(state_get resume_failed)" != 1 ] || [ $((now - last)) -ge 86400 ]; then
      "$ALERT" "Disk back to ${pct}% on $(hostname) but a downloader did NOT start" "Could not start: $nostart${started:+ (started: $started)}. Still paused; the watchdog retries hourly. Operations -> Logs, then Operations -> Restart a service." high
      state_set last_alert "$now"
    fi
    state_set resume_failed 1
  fi
fi
# The flag always follows the latch, and is re-touched on every paused run so the stale-file
# sweep below (-mtime +14 under library/staging) can never quietly un-pause the portal.
if [ "$(state_get paused)" = 1 ]; then
  mkdir -p "$(dirname "$PAUSE_FLAG")" 2>/dev/null
  printf 'disk %s%% >= %s%%; set by scripts/disk-watch.sh at %s\n' "$pct" "$STOP_PCT" "$(date -Is 2>/dev/null)" > "$PAUSE_FLAG" 2>/dev/null || true
else
  rm -f "$PAUSE_FLAG" 2>/dev/null || true
fi

# growers
find "$STACK_DIR/downloads/incomplete" "$STACK_DIR/library/staging" -type f -mtime +14 -delete 2>/dev/null
find "$STACK_DIR/library/ingest" -name '*.part' -mmin +1440 -delete 2>/dev/null
journalctl --vacuum-size=200M >/dev/null 2>&1
docker builder prune -f --filter until=168h >/dev/null 2>&1
if [ -f /etc/bookstack/restic.env ]; then ( set -a; . /etc/bookstack/restic.env; set +a; restic cache --cleanup >/dev/null 2>&1 ); fi
exit 0
