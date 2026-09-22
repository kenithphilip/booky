#!/usr/bin/env bash
# Hourly disk watchdog (installed by bookstack.sh Deploy as /etc/cron.d/bookstack-disk).
#   >= 85 % used : alert once per 24 h
#   >= 95 % used : also stop the downloaders (shelfmark, aria2), pause qBittorrent, alert high
#   <  80 % used : start again whatever THIS script stopped
# Always: delete stale partials, trim journald, Docker build cache and restic's cache.
# A full disk wedges SQLite, CWA ingest, Caddy logging and swap at once; nothing restarts out of it.
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
STATE="${DISK_STATE:-/etc/bookstack/disk.state}"
ENV_FILE="$STACK_DIR/.env"
ALERT="$STACK_DIR/scripts/alert.sh"
mkdir -p "$(dirname "$STATE")"; touch "$STATE"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
state_get(){ grep -E "^$1=" "$STATE" 2>/dev/null | head -1 | cut -d= -f2-; }
state_set(){ { grep -vE "^$1=" "$STATE" 2>/dev/null || true; echo "$1=$2"; } > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
compose(){ (cd "$STACK_DIR" && docker compose "$@"); }
qbit_pause(){ # $1 = stop|start. Best effort via the Web API on loopback; qBittorrent 5 renamed
  # pause/resume to stop/start, older versions only know the old names -> try both, log the outcome.
  local u p c code alt; u=$(envget QBIT_USER); p=$(envget QBIT_PASS); [ -n "$u" ] || return 0
  case "$1" in stop) alt=pause;; start) alt=resume;; *) alt="$1";; esac
  c=$(mktemp)
  if curl -s -c "$c" --data-urlencode "username=$u" --data-urlencode "password=$p" http://127.0.0.1:8080/api/v2/auth/login >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -b "$c" -d 'hashes=all' "http://127.0.0.1:8080/api/v2/torrents/$1" 2>/dev/null)
    [ "$code" = 200 ] || code=$(curl -s -o /dev/null -w '%{http_code}' -b "$c" -d 'hashes=all' "http://127.0.0.1:8080/api/v2/torrents/$alt" 2>/dev/null)
    logger -t bookstack "disk-watch: qBittorrent torrents/$1 -> HTTP $code"
  else logger -t bookstack "disk-watch: qBittorrent login failed; torrents not ${1}ped"; fi
  rm -f "$c"; }

pct=$(df --output=pcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9); pct="${pct:-0}"
now=$(date +%s); last=$(state_get last_alert); last="${last:-0}"
free_h=$(df -h "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}')

if [ "$pct" -ge 95 ]; then
  if [ "$(state_get paused)" != 1 ]; then
    compose stop shelfmark aria2 >/dev/null 2>&1; qbit_pause stop; state_set paused 1
    "$ALERT" "Disk ${pct}% full on $(hostname)" "Only $free_h free under $STACK_DIR. Downloaders stopped (shelfmark, aria2; qBittorrent paused). Free space, then they restart automatically below 80%." high
    state_set last_alert "$now"
  fi
elif [ "$pct" -ge 85 ]; then
  if [ $((now - last)) -ge 86400 ]; then
    "$ALERT" "Disk ${pct}% full on $(hostname)" "$free_h free under $STACK_DIR. At 95% the downloaders are stopped. Check Operations -> Self-test and library/audiobooks."
    state_set last_alert "$now"
  fi
elif [ "$pct" -lt 80 ] && [ "$(state_get paused)" = 1 ]; then
  compose start shelfmark aria2 >/dev/null 2>&1; qbit_pause start; state_set paused 0
  "$ALERT" "Disk back to ${pct}% on $(hostname)" "Downloaders started again."
fi

# growers
find "$STACK_DIR/downloads/incomplete" "$STACK_DIR/library/staging" -type f -mtime +14 -delete 2>/dev/null
find "$STACK_DIR/library/ingest" -name '*.part' -mmin +1440 -delete 2>/dev/null
journalctl --vacuum-size=200M >/dev/null 2>&1
docker builder prune -f --filter until=168h >/dev/null 2>&1
if [ -f /etc/bookstack/restic.env ]; then ( set -a; . /etc/bookstack/restic.env; set +a; restic cache --cleanup >/dev/null 2>&1 ); fi
exit 0
