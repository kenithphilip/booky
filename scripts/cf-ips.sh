#!/usr/bin/env bash
# Allow web traffic (80/443) ONLY from Cloudflare's edge. Re-runnable; installed as a nightly cron
# by bookstack.sh so the allowlist follows Cloudflare's published ranges.
#
# Both lists are fetched separately and validated (CIDR syntax, a minimum count) BEFORE ufw is
# touched: one failed or garbled fetch must never delete the rules the public sites depend on.
# Any failure leaves the firewall as it was, exits non-zero and alerts the admin.
set -euo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"            # cron's default PATH lacks ufw's /usr/sbin
STATE="${CF_IPS_STATE:-/etc/bookstack/cf-ips.txt}"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ALERT="${CF_IPS_ALERT:-$STACK_DIR/scripts/alert.sh}"
MIN_V4=10; MIN_V6=5                                     # Cloudflare publishes 15 v4 and 7 v6 ranges
mkdir -p "$(dirname "$STATE")"

fail(){
  echo "cf-ips: $1 — no allow rule was deleted" >&2
  [ -x "$ALERT" ] && "$ALERT" "Cloudflare IP refresh failed on $(hostname 2>/dev/null)" "$1. No existing ufw allow rule was deleted (the sites keep working with the previous ranges)." || true
  exit 1
}
fetch(){ # url regex min -> validated list on stdout
  local body n
  body=$(curl -fsS -m 30 --retry 2 "$1") || return 1
  body=$(printf '%s\n' "$body" | tr -d '\r' | sed '/^[[:space:]]*$/d')
  [ -n "$body" ] || return 3
  grep -qvE "$2" <<< "$body" && return 2                      # anything that is not a CIDR
  n=$(grep -c . <<< "$body" || true)
  [ "$n" -ge "$3" ] || return 3
  printf '%s\n' "$body"
}
V4RE='^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'
V6RE='^[0-9a-fA-F:]+:[0-9a-fA-F:]*/[0-9]{1,3}$'
v4=$(fetch https://www.cloudflare.com/ips-v4 "$V4RE" "$MIN_V4") || fail "IPv4 list from cloudflare.com/ips-v4 could not be fetched or failed validation (rc=$?)"
v6=$(fetch https://www.cloudflare.com/ips-v6 "$V6RE" "$MIN_V6") || fail "IPv6 list from cloudflare.com/ips-v6 could not be fetched or failed validation (rc=$?)"
new=$(printf '%s\n%s\n' "$v4" "$v6")

# add first (current ranges), then remove ranges no longer published: the site never has a gap
while read -r cidr; do
  [ -n "$cidr" ] || continue
  # 443 only (L19): certificates use DNS-01 and Cloudflare is set to "Always Use HTTPS" with
  # Full (strict) SSL, so nothing ever reaches the origin on port 80
  ufw allow proto tcp from "$cidr" to any port 443 comment cloudflare >/dev/null || fail "ufw could not add $cidr port 443"
  ufw --force delete allow proto tcp from "$cidr" to any port 80 >/dev/null 2>&1 || true
  ufw allow proto udp from "$cidr" to any port 443 comment cloudflare >/dev/null || fail "ufw could not add $cidr udp/443"
done <<< "$new"

if [ -f "$STATE" ]; then
  while read -r cidr; do
    [ -n "$cidr" ] || continue
    grep -qxF "$cidr" <<< "$new" && continue
    for p in 80 443; do ufw --force delete allow proto tcp from "$cidr" to any port "$p" >/dev/null 2>&1 || true; done
    ufw --force delete allow proto udp from "$cidr" to any port 443 >/dev/null 2>&1 || true
  done < "$STATE"
fi

printf '%s\n' "$new" > "$STATE"
echo "ufw: web ports limited to $(grep -c . "$STATE") Cloudflare ranges"
# dead-man's switch (success only: a failure already alerted through fail() above)
"${KUMA_PUSH:-$STACK_DIR/scripts/kuma-push.sh}" cfips up "$(grep -c . "$STATE") ranges" >/dev/null 2>&1 || true
