#!/usr/bin/env bash
# heal.sh — restart a container that Docker reports UNHEALTHY (L20). Cron, every 2 minutes.
#
# `restart: unless-stopped` only reacts to PID 1 exiting; Docker never acts on a failing
# healthcheck, so a wedged Calibre-Web (the sync that 500s for ever, measured) stays "unhealthy"
# until someone notices. The usual fix, an autoheal container, needs /var/run/docker.sock —
# root on the host, inside a third-party image. This is the same job as a root cron script
# that already has Docker, with the guard rails that make it safe to leave alone:
#   * only the services listed in HEAL (never caddy: its healthcheck-free restart would drop every
#     public site; never qbittorrent: a restart mid-download is its own problem)
#   * two consecutive unhealthy readings (4 minutes) before acting — a slow start is not a fault
#   * at most one restart per container per 30 minutes; a container still unhealthy after that is
#     an alert for a person, not a restart loop
#   * every restart is an alert (scripts/alert.sh), so a healing box is never a silent one
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
STATE="${HEAL_STATE:-/etc/bookstack/heal.state}"
ALERT="${HEAL_ALERT:-$STACK_DIR/scripts/alert.sh}"
HEAL="${HEAL_SERVICES:-calibre-web librarian shelfmark audiobookshelf flaresolverr uptime-kuma}"
COOLDOWN="${HEAL_COOLDOWN:-1800}"
now=$(date +%s)
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
touch "$STATE" 2>/dev/null || true
get(){ grep -E "^$1=" "$STATE" 2>/dev/null | tail -1 | cut -d= -f2-; }
put(){ { grep -vE "^$1=" "$STATE" 2>/dev/null || true; echo "$1=$2"; } > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"; }

for c in $HEAL; do
  st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null || true)
  if [ "$st" != unhealthy ]; then
    put "seen_$c" 0
    continue
  fi
  seen=$(get "seen_$c"); seen=$(( ${seen:-0} + 1 )); put "seen_$c" "$seen"
  [ "$seen" -ge 2 ] || continue
  last=$(get "restart_$c"); last=${last:-0}
  if [ $(( now - last )) -lt "$COOLDOWN" ]; then
    # already restarted recently and still unhealthy: a person has to look; say so once
    if [ "$(get "told_$c")" != "$last" ]; then
      "$ALERT" "Bookstack: $c is still unhealthy after a restart" \
        "It was restarted $(( (now - last) / 60 )) min ago and is unhealthy again; not restarting it again for now. Operations -> Logs -> $c." high >/dev/null 2>&1 || true
      put "told_$c" "$last"
    fi
    continue
  fi
  why=$(docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$c" 2>/dev/null | tail -c 300 | tr '\n' ' ')
  if docker restart "$c" >/dev/null 2>&1; then
    put "restart_$c" "$now"; put "seen_$c" 0
    "$ALERT" "Bookstack: restarted $c (unhealthy)" \
      "Docker reported $c unhealthy for two checks in a row, so it was restarted. Last healthcheck output: ${why:-none}" >/dev/null 2>&1 || true
    logger -t bookstack-heal "restarted $c (unhealthy): ${why:0:200}" 2>/dev/null || true
  else
    "$ALERT" "Bookstack: could not restart unhealthy $c" "docker restart $c failed. Operations -> Logs -> $c." high >/dev/null 2>&1 || true
  fi
done
exit 0
