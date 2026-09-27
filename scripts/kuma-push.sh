#!/usr/bin/env bash
# kuma-push.sh <job> [up|down] [message] — report a scheduled job to its Uptime Kuma push
# monitor (a dead-man's switch: silence past the job's schedule is an alert in Kuma).
# <job> is one of selftest, disk, metapush, cfips, backup, canary; its token is KUMA_PUSH_<JOB> in
# $STACK_DIR/.env (bookstack.sh generates it, monitoring/kuma_bootstrap.py creates the monitor).
# Exit 0 = Kuma took it, or monitoring is simply not set up for this job (no token).
# Exit 1 = there is a token but Kuma did not accept the push: the hourly self-test wrapper uses
# that to fall back to alert.sh. Every other caller appends `|| true`.
job="${1:-}"; st="${2:-up}"; m="${3:-OK}"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
KUMA_URL="${KUMA_URL:-http://127.0.0.1:3001}"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
case "$job" in selftest|disk|metapush|cfips|backup|canary) ;; *) echo "kuma-push: unknown job '$job'" >&2; exit 2;; esac
case "$st" in up|down) ;; *) st=down;; esac
tok=$(envget "KUMA_PUSH_$(printf '%s' "$job" | tr '[:lower:]' '[:upper:]')")
[ -n "$tok" ] || exit 0
# Kuma answers 200 {"ok":true} when it recorded the beat, 404 {"ok":false,...} for an unknown or
# paused monitor. -G + --data-urlencode: the message carries check names, spaces and colons.
ans=$(curl -sS -m 10 --retry 2 -G "$KUMA_URL/api/push/$tok" \
        --data-urlencode "status=$st" --data-urlencode "msg=${m:0:250}" --data-urlencode "ping=" 2>/dev/null) || exit 1
case "$ans" in *'"ok":true'*) exit 0;; *) exit 1;; esac
