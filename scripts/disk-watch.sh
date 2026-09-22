#!/usr/bin/env bash
# Hourly disk watchdog (installed by bookstack.sh Deploy as /etc/cron.d/bookstack-disk).
#   >= 85 % used : alert once per 24 h
#   >= 95 % used : also stop the downloaders (shelfmark; qBittorrent when it runs), alert high
#   <  80 % used : start again whatever THIS script stopped (qBittorrent only while it is enabled)
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
state_get(){ grep -E "^$1=" "$STATE" 2>/dev/null | head -1 | cut -d= -f2-; }
state_set(){ { grep -vE "^$1=" "$STATE" 2>/dev/null || true; echo "$1=$2"; } > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
torrents_on(){ [ "$(envget TORRENTS_ENABLED)" = true ]; }
compose(){ if torrents_on; then (cd "$STACK_DIR" && docker compose --profile torrents "$@"); else (cd "$STACK_DIR" && docker compose "$@"); fi; }
running(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

pct=$(df --output=pcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9); pct="${pct:-0}"
now=$(date +%s); last=$(state_get last_alert); last="${last:-0}"
free_h=$(df -h "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}')

if [ "$pct" -ge 95 ]; then
  if [ "$(state_get paused)" != 1 ]; then
    stopped="shelfmark"
    compose stop shelfmark >/dev/null 2>&1
    if running qbittorrent; then compose stop qbittorrent >/dev/null 2>&1; stopped="$stopped, qbittorrent"; fi
    state_set paused 1
    "$ALERT" "Disk ${pct}% full on $(hostname)" "Only $free_h free under $STACK_DIR. Downloaders stopped ($stopped). Free space, then they restart automatically below 80%." high
    state_set last_alert "$now"
  fi
elif [ "$pct" -ge 85 ]; then
  if [ $((now - last)) -ge 86400 ]; then
    "$ALERT" "Disk ${pct}% full on $(hostname)" "$free_h free under $STACK_DIR. At 95% the downloaders are stopped. Check Operations -> Self-test and library/audiobooks."
    state_set last_alert "$now"
  fi
elif [ "$pct" -lt 80 ] && [ "$(state_get paused)" = 1 ]; then
  started="shelfmark"
  compose start shelfmark >/dev/null 2>&1
  if torrents_on; then compose up -d qbittorrent >/dev/null 2>&1; started="$started, qbittorrent"; fi
  state_set paused 0
  "$ALERT" "Disk back to ${pct}% on $(hostname)" "Downloaders started again ($started)."
fi

# growers
find "$STACK_DIR/downloads/incomplete" "$STACK_DIR/library/staging" -type f -mtime +14 -delete 2>/dev/null
find "$STACK_DIR/library/ingest" -name '*.part' -mmin +1440 -delete 2>/dev/null
journalctl --vacuum-size=200M >/dev/null 2>&1
docker builder prune -f --filter until=168h >/dev/null 2>&1
if [ -f /etc/bookstack/restic.env ]; then ( set -a; . /etc/bookstack/restic.env; set +a; restic cache --cleanup >/dev/null 2>&1 ); fi
exit 0
