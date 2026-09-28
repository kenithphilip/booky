#!/usr/bin/env bash
# caddy-clientip.sh — has Caddy loaded Cloudflare's address list?
#
# Caddy learns Cloudflare's ranges from its caddy-cloudflare-ip module when it starts (and every
# 12 h). If that first fetch fails (a network blip during a Deploy), Caddy treats every visitor
# as the Cloudflare edge address that delivered them: rate limits are shared by the whole
# family, and a fail2ban ban would land on a Cloudflare address. Measured on the real server
# 2026-09-28. This reads the facts: among the most recent requests whose socket address
# (remote_ip) is in Cloudflare's published ranges (/etc/bookstack/cf-ips.txt, kept by
# scripts/cf-ips.sh), did Caddy resolve a different client_ip (CF-Connecting-IP)?
#   exit 0  yes: the list is loaded
#   exit 1  no: every recent Cloudflare-delivered request was logged as the edge itself
#   exit 2  cannot tell (no such requests yet, no log, no range file)
# CLIENTIP_SINCE=<epoch>: only requests Caddy served after that (a restart), so the lines the
# old process logged are not held against the new one.
# Used by Deploy / Update (restart Caddy until it is 0) and scripts/heal.sh (every 2 minutes).
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
LOG="${CADDY_ACCESS_LOG:-$STACK_DIR/caddy/data/access.log}"
RANGES="${CF_IPS_STATE:-/etc/bookstack/cf-ips.txt}"
[ -s "$LOG" ] && [ -s "$RANGES" ] || exit 2
tail -n "${CLIENTIP_LINES:-2000}" "$LOG" | python3 -c '
import ipaddress, json, sys
nets = []
for line in open(sys.argv[1]):
    line = line.strip()
    if line:
        try: nets.append(ipaddress.ip_network(line, strict=False))
        except ValueError: pass
since = float(sys.argv[2] or 0)
seen = []
for line in sys.stdin:
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if float(e.get("ts") or 0) < since:
        continue
    r = e.get("request") or {}
    ri, ci = r.get("remote_ip"), r.get("client_ip")
    try: a = ipaddress.ip_address(ri)
    except (TypeError, ValueError): continue
    if any(a in n for n in nets):
        seen.append(ci != ri)
recent = seen[-20:]
if not recent: sys.exit(2)
sys.exit(0 if any(recent) else 1)' "$RANGES" "${CLIENTIP_SINCE:-0}"
