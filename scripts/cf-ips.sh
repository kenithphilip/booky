#!/usr/bin/env bash
# Allow web traffic (80/443) ONLY from Cloudflare's edge. Re-runnable; installed as a nightly cron
# by bookstack.sh so the allowlist follows Cloudflare's published ranges.
set -euo pipefail
STATE=/etc/bookstack/cf-ips.txt
mkdir -p /etc/bookstack

new=$(curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6)
[ -n "$new" ] || { echo "could not fetch Cloudflare IP list"; exit 1; }

# remove rules for ranges no longer published
if [ -f "$STATE" ]; then
  while read -r cidr; do
    [ -n "$cidr" ] || continue
    grep -qx "$cidr" <<< "$new" && continue
    for p in 80 443; do ufw --force delete allow proto tcp from "$cidr" to any port "$p" >/dev/null 2>&1 || true; done
    ufw --force delete allow proto udp from "$cidr" to any port 443 >/dev/null 2>&1 || true
  done < "$STATE"
fi

while read -r cidr; do
  [ -n "$cidr" ] || continue
  for p in 80 443; do ufw allow proto tcp from "$cidr" to any port "$p" comment cloudflare >/dev/null; done
  ufw allow proto udp from "$cidr" to any port 443 comment cloudflare >/dev/null
done <<< "$new"

echo "$new" > "$STATE"
echo "ufw: web ports limited to $(wc -l < "$STATE") Cloudflare ranges"
