#!/usr/bin/env bash
# mem-tidy.sh — nightly at 03:45 (installed by bookstack.sh Deploy as /etc/cron.d/bookstack-memtidy):
# give back memory a long-running service has built up over days, by restarting it. That is the
# only thing that returns it: Python and Node processes (Calibre-Web, Shelfmark, Audiobookshelf,
# Syncthing) keep the memory they once needed. Dropping Linux's page cache would NOT help — it is
# already reclaimed on demand, and dropping it only makes the next minutes slower.
#
# A service is restarted only when BOTH hold:
#   * its own memory is at or above MEM_TIDY_PCT (70) % of its mem_limit (docker-compose.yml), and
#   * it is idle: Calibre-Web has nothing waiting in /ingest and the host job (metadata-push, which
#     writes through Calibre-Web's container) is not running — its lock is held for the restart;
#     Shelfmark has nothing queued, searching, waiting or downloading; nobody is online in
#     Audiobookshelf. Syncthing resumes where it stopped, so it only needs the memory reason.
# One at a time, each back to healthy before the next. A service that does not come back is an
# alert. 03:45 is before the 04:30 unattended-upgrades reboot window, when the family is asleep.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
ALERT="$STACK_DIR/scripts/alert.sh"
LOCK="${MEMTIDY_METAPUSH_LOCK:-/run/lock/bookstack-metapush.lock}"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
PCT=$(num MEM_TIDY_PCT 70)

running(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }
# percent of the container's own mem_limit, as an integer; nothing when unknown
mem_pct(){ docker stats --no-stream --format '{{.MemPerc}}' "$1" 2>/dev/null | head -1 | tr -d '% ' | cut -d. -f1 | tr -cd '0-9'; }
busy(){ # service -> 0 when BUSY or unknown (never restart on a guess), 1 when idle
  local out
  out=$(docker exec -i librarian python -m admin_cli busy "$1" 2>/dev/null | tail -1)
  printf '%s' "$out" | grep -q '"busy": false' && return 1
  return 0
}
wait_healthy(){ local s
  for _ in $(seq 1 60); do
    s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null)
    case "$s" in healthy|running) return 0;; esac
    sleep 5
  done
  return 1
}
restart(){ # container, reason
  echo "mem-tidy: restarting $1 ($2)"
  docker restart "$1" >/dev/null 2>&1
  if wait_healthy "$1"; then echo "mem-tidy: $1 is back, now at $(mem_pct "$1")% of its memory limit"
  else
    [ -x "$ALERT" ] && "$ALERT" "Bookstack: $1 did not come back after its nightly memory restart" \
      "mem-tidy.sh restarted $1 ($2) and it is not healthy 5 minutes later. Operations -> Logs -> $1." high
    echo "mem-tidy: $1 is NOT healthy after the restart" >&2
  fi
}

for c in calibre-web shelfmark audiobookshelf syncthing; do
  running "$c" || continue
  p=$(mem_pct "$c")
  if [ -z "$p" ]; then echo "mem-tidy: $c: memory unknown, left alone"; continue; fi
  if [ "$p" -lt "$PCT" ]; then echo "mem-tidy: $c at ${p}% of its limit: fine"; continue; fi
  case "$c" in
    calibre-web)
      if find "$STACK_DIR/library/ingest" -maxdepth 1 -type f ! -name '*.part' ! -name '*.tmp' 2>/dev/null | grep -q .; then
        echo "mem-tidy: calibre-web at ${p}% but books are waiting to be imported: next night"; continue
      fi
      exec 9>"$LOCK"
      if ! flock -n 9; then echo "mem-tidy: calibre-web at ${p}% but the host job is writing to it: next night"; exec 9>&-; continue; fi
      restart "$c" "${p}% of its limit"             # the host job waits on the lock meanwhile
      exec 9>&-;;
    shelfmark|audiobookshelf)
      if busy "$c"; then echo "mem-tidy: $c at ${p}% but in use (or its state is unknown): next night"; continue; fi
      restart "$c" "${p}% of its limit";;
    *) restart "$c" "${p}% of its limit";;
  esac
done
exit 0
