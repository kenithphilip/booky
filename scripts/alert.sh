#!/usr/bin/env bash
# alert.sh "<title>" "<text>" [priority]  — one alert channel for host-side jobs (backup,
# restore test, disk watchdog, Cloudflare allowlist refresh). Never fails its caller.
#  1. the portal delivers (NOTIFY_WEBHOOK and/or ADMIN_EMAIL via its SMTP); `python -m notify`
#     exits non-zero (3) when no channel took the message
#  2. otherwise: always the journal (journalctl -t bookstack) AND, when NOTIFY_WEBHOOK is set in
#     $STACK_DIR/.env, a direct POST from the host (ntfy-compatible: body = text, Title header),
#     so an alert still arrives while the portal container is down.
title="${1:-bookstack}"; text="${2:-}"; prio="${3:-}"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }

if docker exec -i librarian python -m notify alert "$title" "$text" ${prio:+"$prio"} >/dev/null 2>&1; then
  exit 0
fi
logger -t bookstack -p user.warning "ALERT $title: $text" 2>/dev/null || echo "ALERT $title: $text" >&2
hook=$(envget NOTIFY_WEBHOOK)
if [ -n "$hook" ]; then
  htitle=$(printf '%s' "$title" | tr -d '\r\n')          # a header value must stay one line
  args=(-fsS -m 20 --retry 2 -X POST -H "Title: $htitle" --data-binary "$text")
  [ "$prio" = high ] && args+=(-H "Priority: high")
  curl "${args[@]}" "$hook" >/dev/null 2>&1 \
    || logger -t bookstack -p user.warning "ALERT webhook delivery failed for: $title" 2>/dev/null || true
fi
exit 0
