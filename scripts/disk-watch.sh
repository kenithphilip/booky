#!/usr/bin/env bash
# Hourly disk watchdog (installed by bookstack.sh Deploy as /etc/cron.d/bookstack-disk).
# "used" below is the WORSE of blocks-used and inodes-used (see the pctread block): an
# exhausted inode table wedges the stack exactly like a full disk, and `df` alone never shows it.
#   >= DISK_WARN_PCT (85) used : alert once per 24 h
#   >= DISK_STOP_PCT (95) used : also stop the downloaders (shelfmark; qBittorrent when it runs)
#                                AND raise the pause flag the portal's own worker honours, alert high
#   <  DISK_RESUME_PCT (80)    : start again whatever THIS script stopped, and drop the flag
# The three thresholds come from the environment or $STACK_DIR/.env: hardcoding them meant an
# admin had to edit this file, which copy_code_trees overwrites on every Deploy and Update.
# Always: delete stale partials, trim journald, Docker build cache, dangling images, apt's
# package cache and restic's cache.
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

# Blocks AND inodes. A filesystem can sit at 60 % blocks and 100 % inodes; the symptoms are
# identical to a full disk (SQLite ENOSPC, ingest failures, Caddy unable to log) while every
# block threshold reads normal and nothing is ever paused. One directory per book, plus its
# covers and formats, plus multi-part audiobooks, is the classic inode-hungry workload.
# $pct is the WORSE of the two, so the thresholds, the 24 h latch and the pause flag below are
# untouched; $trip names the one that tripped, because the remedies have nothing in common
# (delete big files vs. delete many small ones, and only a mkfs adds inodes to ext4).
# Anything that is not a 0..100 number is a df that did not answer what we asked — busybox
# (no --output), a filesystem with no fixed inode table printing "-", a test stub falling
# through to the human table. pctread prints nothing then, and an unknown reading must never
# be treated as "full": that would stop the downloaders on a perfectly healthy box. The bound
# is that test, not a clamp.
pctread(){ local v; v=$(df "$@" "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
  case "$v" in ''|*[!0-9]*) return 0;; esac; [ "$v" -le 100 ] || return 0; printf '%s' "$v"; }
bpct=$(pctread --output=pcent); bpct="${bpct:-0}"
ipct=$(pctread -i --output=ipcent)                       # "" where inodes are not counted
if [ "${ipct:-0}" -gt "$bpct" ]; then pct="$ipct"; else pct="$bpct"; fi
# "blocks 96%", "inodes 97%" or "blocks 96% and inodes 97%" — whichever is at or over $1.
trip(){ local w=""
  [ "$bpct" -ge "$1" ] && w="blocks ${bpct}%"
  [ -n "$ipct" ] && [ "$ipct" -ge "$1" ] && w="${w:+$w and }inodes ${ipct}%"
  printf '%s' "${w:-blocks ${bpct}%${ipct:+, inodes ${ipct}%}}"; }
# Freeing gigabytes does nothing for an inode shortage, and `df -h` will keep saying there is
# room, so say so in the alert itself rather than leaving the admin to delete large files.
inode_hint(){ [ -n "$ipct" ] && [ "$ipct" -ge "$1" ] || return 0
  printf ' INODES, not bytes: `df -h` still shows free space and deleting one big file will not help. Delete MANY small files (cwa/config/processed_books, abs/metadata/cache, old thumbnails) or move the library to a filesystem with more inodes — ext4 fixes its inode count at mkfs time.'; }
now=$(date +%s); last=$(state_get last_alert); last="${last:-0}"
free_h=$(df -h "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}')
ifree=""; [ -n "$ipct" ] && ifree=$(df -i --output=iavail "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
free_txt="$free_h free${ifree:+ and $ifree free inodes}"

if [ "$pct" -ge "$STOP_PCT" ]; then
  if [ "$(state_get paused)" != 1 ]; then
    stopped=""; nostop=""
    if compose stop shelfmark >/dev/null 2>&1; then stopped="shelfmark"; else nostop="shelfmark"; fi
    if running qbittorrent; then
      if compose stop qbittorrent >/dev/null 2>&1; then stopped="${stopped:+$stopped, }qbittorrent"; else nostop="${nostop:+$nostop, }qbittorrent"; fi
    fi
    state_set paused 1
    ALERT_SEQ=disk-level "$ALERT" "Disk ${pct}% full on $(hostname)" "$(trip "$STOP_PCT") under $STACK_DIR; only $free_txt. Downloaders stopped (${stopped:-none})${nostop:+; COULD NOT stop: $nostop}. The portal's own imports are paused too.$(inode_hint "$STOP_PCT") Free space, then they restart automatically below ${RESUME_PCT}%." high
    state_set last_alert "$now"
  fi
elif [ "$pct" -ge "$WARN_PCT" ]; then
  if [ $((now - last)) -ge 86400 ]; then
    ALERT_SEQ=disk-level "$ALERT" "Disk ${pct}% full on $(hostname)" "$(trip "$WARN_PCT") under $STACK_DIR; $free_txt. At ${STOP_PCT}% the downloaders are stopped.$(inode_hint "$WARN_PCT") Check Operations -> Self-test and library/audiobooks."
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
    ALERT_SEQ=disk-level ALERT_TAGS=white_check_mark "$ALERT" "Disk back to ${pct}% on $(hostname)" "Now at blocks ${bpct}%${ipct:+, inodes ${ipct}%}; $free_txt. Downloaders started again ($started)."
  else
    # paused stays 1: the next hourly run tries again instead of leaving a dead container behind.
    if [ "$(state_get resume_failed)" != 1 ] || [ $((now - last)) -ge 86400 ]; then
      ALERT_SEQ=disk-level "$ALERT" "Disk back to ${pct}% on $(hostname) but a downloader did NOT start" "Could not start: $nostart${started:+ (started: $started)}. Still paused; the watchdog retries hourly. Operations -> Logs, then Operations -> Restart a service." high
      state_set last_alert "$now"
    fi
    state_set resume_failed 1
  fi
fi
# The flag always follows the latch, and is re-touched on every paused run so the stale-file
# sweep below (-mtime +14 under library/staging) can never quietly un-pause the portal.
if [ "$(state_get paused)" = 1 ]; then
  mkdir -p "$(dirname "$PAUSE_FLAG")" 2>/dev/null
  printf 'disk %s%% >= %s%% (%s); set by scripts/disk-watch.sh at %s\n' "$pct" "$STOP_PCT" "$(trip "$STOP_PCT")" "$(date -Is 2>/dev/null)" > "$PAUSE_FLAG" 2>/dev/null || true
else
  rm -f "$PAUSE_FLAG" 2>/dev/null || true
fi

# growers
find "$STACK_DIR/downloads/incomplete" "$STACK_DIR/library/staging" -type f -mtime +14 -delete 2>/dev/null
# Stale .part files from an ingest that died. 6 HOURS, not the 15 minutes this used to be:
# tagging is NOT the only work the portal does under the .part name. librarian/worker.py's
# _atomic_ingest runs, all inside one try and all BEFORE `os.rename(part, ...)`: a
# shutil.copyfile of up to MAX_EBOOK_MB=200 MB (MAX_PDF_MB=250 for PDFs), _zip_ok, the tagger
# (pypdf clones a 250 MB PDF in memory), _drm_note and - for epub and pdf - _auto_kindle,
# which is a SYNCHRONOUS SMTP session uploading up to KINDLE_MAX_MB=45 MB to Amazon.
# librarian/kindle.py's timeout=30 is per socket operation, not a total, so a 45 MB body at
# 20 KB/s takes ~37 minutes without one send() ever timing out. -mmin is mtime and the last
# thing to refresh it is the tagger's os.replace, so that whole upload ran inside the old
# 15-minute window. When the reaper won, os.rename raised FileNotFoundError, _atomic_ingest's
# `except BaseException` re-raised and the request was closed as an error - AFTER the book had
# already been mailed to the reader's Kindle. It fired only under disk pressure and a slow
# uplink, i.e. exactly when the admin is already chasing other symptoms.
# 6 h is ~10x the worst case above and still reclaims within a quarter of a day; the real
# owner of this cleanup is librarian/worker.py's sweep_orphans, which matches the exact
# ^[0-9a-f]{32}\.part(\.tmp)?$ shape and runs at worker start, when nothing of the worker's is
# in flight by construction. This line is only the backstop for a worker that is not restarting.
find "$STACK_DIR/library/ingest" -name '*.part' -mmin +360 -delete 2>/dev/null
journalctl --vacuum-size=200M >/dev/null 2>&1
# NOTE, deliberately left as-is: caddy/Dockerfile builds Caddy from source with xcaddy and
# three pinned plugins, and both Deploy and Update run `compose build --pull caddy librarian`,
# so this weekly eviction guarantees every Update compiles Caddy cold on two shared vCPUs.
# That is real but bounded, and disk - not CPU - is the binding constraint at 80 GB, so the
# cache is not worth one to two GB of it by default. If cold Update builds ever actually hurt,
# the cheap fix is local and one line: raise `until=168h`, and accept that disk. Do NOT stand
# up a registry and a cross-arch publish pipeline for an event that happens a few times a year.
docker builder prune -f --filter until=168h >/dev/null 2>&1
# Image layers nothing refers to any more (every Update and Deploy rebuild leaves the previous
# caddy/librarian layers behind): DANGLING only. Tagged images stay, including the :prev tags
# Operations -> Update keeps for its rollback. And the .deb packages apt keeps after upgrades.
docker image prune -f >/dev/null 2>&1
apt-get clean >/dev/null 2>&1
if [ -f /etc/bookstack/restic.env ]; then ( set -a; . /etc/bookstack/restic.env; set +a; restic cache --cleanup >/dev/null 2>&1 ); fi

# ---- once a day: old versions of this stack's images ----------------------------------------
# After an Update, the previous version of each image stays on disk (tagged, so the dangling
# prune above never takes it): a Shelfmark or CWA image is 0.5-2 GB. Removed here: images of a
# repository this stack pins (IMG_* in .env, bookstack/caddy and bookstack/librarian) whose tag
# is not the pinned one. Kept: anything a container uses (running or stopped), the pinned tags,
# the bookstack/*:prev rollback images, and, while an update is unfinished or for 7 days after
# one, the previous tags in .env.images.prev (Operations -> Update's rollback point). Images of
# anything else on the box are never touched.
due(){ local last; last=$(state_get "$1"); case "$last" in ''|*[!0-9]*) return 0;; esac; [ $(( $(date +%s) - last )) -ge "$2" ]; }
prune_old_images(){
  local keep repos img k v prev="$STACK_DIR/.env.images.prev"
  keep=$(docker ps -a --format '{{.Image}}' 2>/dev/null)
  repos="bookstack/caddy"$'\n'"bookstack/librarian"
  keep="$keep"$'\n'"bookstack/caddy:latest"$'\n'"bookstack/caddy:prev"$'\n'"bookstack/librarian:latest"$'\n'"bookstack/librarian:prev"
  for k in $(grep -oE '^IMG_[A-Z_]+' "$ENV_FILE" 2>/dev/null); do
    v=$(envget "$k"); [ -n "$v" ] || continue
    keep="$keep"$'\n'"$v"; repos="$repos"$'\n'"${v%:*}"
  done
  if [ -f "$prev" ] && { [ -e "$STACK_DIR/.update-in-progress" ] || [ $(( $(date +%s) - $(stat -c %Y "$prev" 2>/dev/null || echo 0) )) -lt 604800 ]; }; then
    keep="$keep"$'\n'"$(cut -d= -f2- "$prev")"
  fi
  docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | while read -r img; do
    case "$img" in *'<none>'*) continue;; esac
    printf '%s\n' "$repos" | grep -qxF "${img%:*}" || continue      # not one of ours
    printf '%s\n' "$keep" | grep -qxF "$img" && continue
    docker rmi "$img" >/dev/null 2>&1 && echo "disk-watch: removed the old image $img"
  done
}
if due image_sweep 86400; then prune_old_images; state_set image_sweep "$(date +%s)"; fi

# ---- once a week: packages and leftovers nothing needs ---------------------------------------
# apt's autoremove takes only packages installed as dependencies that nothing depends on any
# more (old kernels, orphaned libraries), never one installed on purpose. Audiobookshelf writes
# a log file a day and keeps them all. A better copy (Find a better copy) whose swap failed
# leaves its staged EPUB behind.
if due weekly_sweep 604800; then
  DEBIAN_FRONTEND=noninteractive apt-get -y -qq autoremove --purge >/dev/null 2>&1
  find "$STACK_DIR/abs/metadata/logs" -type f -name '*.txt' -mtime +14 -delete 2>/dev/null
  find "$STACK_DIR/library/staging/replace" -type f -mtime +14 -delete 2>/dev/null
  state_set weekly_sweep "$(date +%s)"
fi
# dead-man's switch: Kuma's "Disk watchdog" monitor goes red if this hourly run stops happening.
# Always "up": the disk level itself is alerted above, through alert.sh, with its own latch.
"${KUMA_PUSH:-$STACK_DIR/scripts/kuma-push.sh}" disk up "ran: blocks ${bpct}%${ipct:+, inodes ${ipct}%}$([ -e "$PAUSE_FLAG" ] && printf ', downloaders PAUSED')" >/dev/null 2>&1 || true
exit 0
