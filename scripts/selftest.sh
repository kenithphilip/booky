#!/usr/bin/env bash
# Non-destructive health check of a deployed stack. Run from bookstack.sh (Operations →
# Self-test) or directly: bash /srv/bookstack/scripts/selftest.sh
# Exit code = number of failed checks.
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
TMPDIR="${TMPDIR:-/tmp}"
ENV_FILE="$STACK_DIR/.env"
fail=0; pass=0
ok(){ printf '  [ OK ] %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  [FAIL] %s\n' "$1"; fail=$((fail+1)); }
warn(){ printf '  [warn] %s\n' "$1"; }
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
compose(){ (cd "$STACK_DIR" && docker compose "$@"); }
code(){ curl -s -m 12 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo 000; }
D=$(envget DOMAIN)

echo "== Containers"
for c in caddy calibre-web audiobookshelf qbittorrent aria2 ariang librarian shelfmark uptime-kuma; do
  st=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null | tr -d '\n'); st="${st:-missing}"
  case "$st" in running\ healthy|running\ |running) ok "$c: $st";; *) bad "$c: $st";; esac
done
for c in authelia ephemera flaresolverr; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true); [ -n "$st" ] && { [ "$st" = running ] && ok "$c: running (optional)" || warn "$c: $st (optional)"; }
done
oom=$(for c in $(docker ps -aq 2>/dev/null); do docker inspect -f '{{.Name}} {{.State.OOMKilled}}' "$c" 2>/dev/null; done | awk '$2=="true"{print $1}' | tr -d / | tr '\n' ' ')
[ -z "$oom" ] && ok "no container was OOM-killed" || bad "OOM-killed: $oom (docker inspect ... OOMKilled; raise its mem_limit or the VPS size)"
avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null); [ -n "$avail" ] && { [ "$avail" -ge 524288 ] && ok "MemAvailable $((avail/1024)) MiB" || warn "MemAvailable only $((avail/1024)) MiB (< 512 MiB)"; }
if [ "$(envget EPHEMERA_ENABLED)" = true ]; then tot=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null); [ "${tot:-0}" -lt 7800000 ] && warn "Ephemera + FlareSolverr enabled on < 8 GB RAM"; fi

echo "== Local endpoints"
curl -fs -m 5 http://127.0.0.1:8090/healthz >/dev/null && ok "portal /healthz" || bad "portal /healthz"
curl -fs -m 5 http://127.0.0.1:8084/api/health >/dev/null && ok "shelfmark /api/health" || bad "shelfmark /api/health"
curl -fs -m 5 -o /dev/null http://127.0.0.1:8083/login && ok "calibre-web /login" || bad "calibre-web /login"
curl -fs -m 5 -o /dev/null http://127.0.0.1:13378/healthcheck && ok "audiobookshelf /healthcheck" || bad "audiobookshelf /healthcheck"

echo "== Config validity"
compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && ok "Caddyfile validates" || bad "Caddyfile does not validate (compose exec caddy caddy validate)"
if grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" 2>/dev/null; then
  compose -f docker-compose.yml -f docker-compose.authelia.yml exec -T authelia authelia validate-config --config /config/configuration.yml >/dev/null 2>&1 \
    && ok "Authelia config validates (gate active)" || bad "Authelia gate is in the Caddyfile but its config does not validate"
fi
[ -f "$STACK_DIR/caddy/cf-origin-pull-ca.pem" ] && ok "Cloudflare origin-pull CA present" || bad "cf-origin-pull-ca.pem missing (run Cloudflare step)"
docker compose -f "$STACK_DIR/docker-compose.yml" config -q 2>/dev/null && ok "docker-compose.yml renders" || bad "docker-compose.yml does not render"
if command -v timedatectl >/dev/null; then [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && ok "clock is NTP-synchronised" || bad "clock NOT NTP-synchronised (TOTP, Kobo sync, ACME and mTLS drift): timedatectl set-ntp true"; fi

echo "== Security posture"
if command -v ufw >/dev/null; then
  ufw status | grep -q "Status: active" && ok "ufw active" || bad "ufw inactive"
  ufw status | grep -qE "^443/tcp.*Anywhere" && bad "443 open to Anywhere (should be Cloudflare ranges only)" || ok "443 not open to the world"
  ufw status | grep -qE "^22/tcp.*ALLOW.*Anywhere" && warn "SSH still public (Security → Lock SSH once Tailscale works)" || ok "SSH not public"
fi
for p in 8083 13378 8080 6800 6880 8090 8084 3001 9091 8286; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|:)$p\$" ; then
    ss -ltn | awk '{print $4}' | grep -E ":$p\$" | grep -qvE '^(127\.0\.0\.1|\[::1\]):' && bad "port $p bound to a non-loopback address" || ok "port $p loopback-only"
  fi
done
ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ':2019$' && bad "Caddy admin API listens on TCP :2019 (must be the unix socket)" || ok "Caddy admin API not on TCP"
pub=$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | while IFS=$'\t' read -r n p; do
        printf '%s' "$p" | tr ',' '\n' | grep -E '(0\.0\.0\.0|\[::\]|:::)[0-9]+->' | grep -qv ':6881->' && printf '%s ' "$n"; done)
[ -z "$pub" ] && ok "no container port published on a public address (except the torrent peer port)" || bad "published on a public address: $pub (Docker bypasses ufw)"
grep -q "^PasswordAuthentication no" /etc/ssh/sshd_config.d/90-bookstack.conf 2>/dev/null && ok "SSH password login disabled" || warn "SSH password login not disabled (add a key, re-run System step)"
[ "$(stat -c %a "$ENV_FILE" 2>/dev/null)" = "600" ] && ok ".env is 0600" || bad ".env permissions are not 0600"
[ "$(stat -c %U "$ENV_FILE" 2>/dev/null)" = root ] && ok ".env owned by root" || bad ".env is owned by $(stat -c %U "$ENV_FILE" 2>/dev/null) (containers run as uid 1000; run Configure)"
if command -v tailscale >/dev/null && ! ufw status 2>/dev/null | grep -qE '^22/tcp.*ALLOW.*Anywhere'; then
  exp=$(tailscale status --json 2>/dev/null | jq -r '.Self.KeyExpiry // "null"' 2>/dev/null)
  if [ "$exp" = null ] || [ -z "$exp" ]; then ok "Tailscale key expiry disabled"
  else days=$(( ($(date -d "$exp" +%s 2>/dev/null || echo 0) - $(date +%s)) / 86400 ))
    [ "$days" -gt 30 ] && ok "Tailscale key expires in $days d" || bad "Tailscale key expires in $days d and SSH is Tailscale-only: admin console -> Machines -> Disable key expiry"; fi
fi
if command -v fail2ban-client >/dev/null; then
  fail2ban-client status caddy-auth >/dev/null 2>&1 && ok "fail2ban caddy-auth jail active" || warn "caddy-auth jail not active (Security -> fail2ban)"
  fail2ban-client status caddy-device-auth >/dev/null 2>&1 && ok "fail2ban caddy-device-auth jail active" || warn "caddy-device-auth jail not active (Security -> fail2ban)"
fi
# the factory admin/admin123 must be dead (CWA's login needs the CSRF token of a session)
cj=$(mktemp); tok=$(curl -s -m 5 -c "$cj" http://127.0.0.1:8083/login 2>/dev/null | grep -oE 'name="csrf_token"[^>]*value="[^"]+"' | grep -oE 'value="[^"]+"' | cut -d'"' -f2)
lc=$(curl -s -m 8 -b "$cj" -o /dev/null -w '%{http_code}' --data-urlencode "csrf_token=$tok" -d 'username=admin&password=admin123&submit=&next=/' http://127.0.0.1:8083/login 2>/dev/null); rm -f "$cj"
case "$lc" in 302|303) bad "Calibre-Web still accepts admin/admin123 (Users -> Reset password NOW)";; 000) warn "could not test the factory admin password";; *) ok "factory admin password rejected";; esac
curl -s -m 5 http://127.0.0.1:13378/status 2>/dev/null | grep -q '"isInit":false' && bad "Audiobookshelf has NO root user: the first visitor becomes admin (Library -> Audiobookshelf)" || ok "Audiobookshelf initialised"
ulist=$(docker exec librarian python -m cwa list 2>/dev/null || true)
if [ -z "$ulist" ]; then warn "could not list users (portal not running?)"
elif printf '%s' "$ulist" | python3 -c 'import sys,json; u=json.load(sys.stdin); bad=[x["name"] for x in u if not x["is_admin"] and not x["isolated"]]; print(",".join(bad)); sys.exit(1 if bad else 0)' >"$TMPDIR/.bs-unisolated" 2>/dev/null; then ok "every non-admin user is tag-isolated"
else bad "NOT isolated: $(cat "$TMPDIR/.bs-unisolated" 2>/dev/null) (Users → repair isolation)"; fi
docker exec calibre-web sqlite3 /config/app.db "select config_public_reg, config_kobo_sync from settings" 2>/dev/null | { IFS='|' read -r reg kobo; [ "${reg:-1}" = 0 ] && ok "CWA public registration off" || bad "CWA public registration is ON"; [ "${kobo:-0}" = 1 ] && ok "CWA Kobo sync on" || warn "CWA Kobo sync off (Users menu → enable)"; }
if [ -n "$(envget ABS_TOKEN)" ]; then
  alist=$(docker exec librarian python -m abs list-users 2>/dev/null || true)
  if [ -z "$alist" ]; then bad "Audiobookshelf API not reachable with ABS_TOKEN (Library → Audiobookshelf to re-run setup)"
  elif printf '%s' "$alist" | python3 -c 'import sys,json; u=json.load(sys.stdin); bad=[x["username"] for x in u if x["type"]=="user" and not x["isolated"]]; print(",".join(bad)); sys.exit(1 if bad else 0)' >"$TMPDIR/.bs-abs" 2>/dev/null; then ok "every Audiobookshelf user is tag-restricted"
  else bad "Audiobookshelf users NOT tag-restricted: $(cat "$TMPDIR/.bs-abs" 2>/dev/null) (Users → Repair)"; fi
else warn "Audiobookshelf automation not set up (Library → Audiobookshelf): audiobooks must be tagged by hand"; fi

echo "== Isolation invariants (cwa.db)"
cols=$(docker exec calibre-web sqlite3 /config/cwa.db "pragma table_info(cwa_settings)" 2>/dev/null | cut -d'|' -f2 | tr '\n' ' ')
if [ -z "$cols" ]; then warn "could not read cwa_settings"
else
  sel=""; for c in auto_ingest_automerge duplicate_auto_resolve_enabled auto_metadata_update_tags auto_convert_ignored_formats koreader_sync_enabled; do
    case " $cols " in *" $c "*) sel="$sel IFNULL($c,'') AS $c,";; esac; done
  row=$(docker exec calibre-web sqlite3 -json /config/cwa.db "select ${sel%,} from cwa_settings limit 1" 2>/dev/null)
  val(){ printf '%s' "$row" | python3 -c 'import sys,json; r=json.load(sys.stdin); print(r[0].get(sys.argv[1],"") if r else "")' "$1" 2>/dev/null; }
  [ "$(val auto_ingest_automerge)" = new_record ] && ok "auto_ingest_automerge = new_record" || bad "auto_ingest_automerge = '$(val auto_ingest_automerge)' (must be new_record: Library -> Formats)"
  case " $cols " in *" duplicate_auto_resolve_enabled "*) [ "$(val duplicate_auto_resolve_enabled)" = 0 ] && ok "duplicate auto-resolve off" || bad "duplicate_auto_resolve_enabled=1 can merge two users' copies (Users -> Repair)";; esac
  case " $cols " in *" auto_metadata_update_tags "*) [ "$(val auto_metadata_update_tags)" = 0 ] && ok "metadata fetch leaves tags alone" || bad "auto_metadata_update_tags=1 can replace owner:<user> tags (Users -> Repair)";; esac
  echo "  info: auto_convert_ignored_formats='$(val auto_convert_ignored_formats)' koreader_sync_enabled='$(val koreader_sync_enabled)'"
fi

echo "== Public reachability (via Cloudflare)"
if [ -n "$D" ]; then
  for h in books audio request shelf; do
    c=$(code "https://$h.$D/")
    case "$c" in 200|302|301|401) ok "https://$h.$D -> $c";; *) bad "https://$h.$D -> $c";; esac
  done
  c=$(code -k "https://$(envget PUBLIC_IP)/" -H "Host: books.$D")
  [ "$c" = 000 ] && ok "origin refuses direct (non-Cloudflare) connections" || bad "origin answered a direct connection ($c) — mTLS/firewall not enforcing"
  cc=$(curl -sI -m 12 "https://books.$D/login" 2>/dev/null | grep -i '^cf-cache-status:' | awk '{print toupper($2)}' | tr -d '\r')
  case "$cc" in HIT|"") [ -n "$cc" ] && bad "Cloudflare served books./login from its cache ($cc): create the no-cache Cache Rule" || warn "no cf-cache-status header (not behind Cloudflare?)";; *) ok "edge cache: $cc";; esac
  # device paths must reach the apps, not a Cloudflare challenge page
  hdr=$(mktemp); body=$(mktemp)
  c=$(curl -s -m 12 -D "$hdr" -o "$body" -w '%{http_code}' "https://books.$D/opds/" 2>/dev/null)
  if grep -qi '^cf-mitigated' "$hdr"; then bad "Cloudflare challenges /opds (Browser Integrity Check / Bot Fight Mode must be OFF)"
  elif [ "$c" = 401 ] && grep -qi '^www-authenticate' "$hdr"; then ok "/opds answers a Basic-auth challenge (CWA reached)"
  else bad "/opds -> $c without WWW-Authenticate"; fi
  tok=$(docker exec librarian python -m cwa kobo-url admin 2>/dev/null | tr -d '"' | sed 's#.*/kobo/##; s#/.*##')
  case "$tok" in None|null|"") tok="";; esac
  if [ -n "$tok" ]; then
    c=$(curl -s -m 12 -A 'Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/533.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/533.1 Kobo' -D "$hdr" -o "$body" -w '%{http_code}' "https://books.$D/kobo/$tok/v1/initialization" 2>/dev/null)
    if grep -qi '^cf-mitigated' "$hdr" || [ "$c" = 403 ] || [ "$c" = 503 ]; then bad "Kobo init challenged by Cloudflare ($c): Browser Integrity Check / Bot Fight Mode must be OFF"
    elif [ "$c" = 200 ] && grep -q Resources "$body"; then ok "Kobo /v1/initialization -> 200 with Resources"
    else bad "Kobo /v1/initialization -> $c"; fi
  else warn "no admin Kobo token yet (Devices page or Users -> Kobo link); Kobo probe skipped"; fi
  c=$(code -H 'Remote-User: admin' "https://books.$D/me"); [ "$c" = 200 ] && bad "a client-supplied Remote-User header logs into Calibre-Web (header login must be off)" || ok "Remote-User header ignored by Calibre-Web ($c)"
  if grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" 2>/dev/null; then
    c=$(code "https://audio.$D/ping"); [ "$c" = 200 ] && ok "gate lets the Audiobookshelf app through (/ping 200)" || bad "audio./ping -> $c (Authelia gate blocks the ABS apps)"
    c=$(code -u x:y "https://books.$D/kosync/users/auth"); [ "$c" = 401 ] && ok "gate lets KOReader sync through (/kosync 401)" || bad "books./kosync -> $c (Authelia gate blocks KOReader)"
  fi
  rm -f "$hdr" "$body"
fi

echo "== Storage & backups"
df -h "$STACK_DIR" | awk 'NR==2{print "  disk: "$4" free ("$5" used)"}'
availk=$(df --output=avail "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9); pct=$(df --output=pcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
if [ -n "$availk" ]; then [ "$availk" -lt 5242880 ] || [ "${pct:-0}" -ge 90 ] && bad "low disk: $((availk/1048576)) GB free, ${pct}% used (< 10 % or < 5 GB)" || ok "disk: $((availk/1048576)) GB free, ${pct}% used"; fi
stuck=$(find "$STACK_DIR/library/ingest" -type f ! -name '*.part' -mmin +30 2>/dev/null | head -3 | tr '\n' ' '); [ -n "$stuck" ] && warn "ingest stuck: files older than 30 min in library/ingest ($stuck) — check calibre-web logs"
pb=$(du -sm "$STACK_DIR/cwa/config/processed_books" 2>/dev/null | cut -f1); [ "${pb:-0}" -gt 5120 ] && warn "cwa/config/processed_books is ${pb} MB (Library -> Formats: turn CWA's file copies off)"
if systemctl is-enabled bookstack-backup.timer >/dev/null 2>&1; then
  ok "backup timer enabled ($(systemctl show bookstack-backup.timer -p NextElapseUSecRealtime --value))"
  systemctl is-enabled bookstack-restore-test.timer >/dev/null 2>&1 && ok "monthly restore-test timer enabled" || warn "restore-test timer missing (re-run Install -> Backups)"
  if [ -f /etc/bookstack/restic.env ] && command -v restic >/dev/null; then
    age=$( (set -a; . /etc/bookstack/restic.env; set +a; restic snapshots --latest 1 --json 2>/dev/null) | python3 -c '
import sys, json, re, datetime
s = re.sub(r"\.\d+", "", json.load(sys.stdin)[-1]["time"]).replace("Z", "+00:00")
print(int((datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(s)).total_seconds() // 3600))' 2>/dev/null)
    if [ -z "$age" ]; then bad "no restic snapshot found (or repository unreachable)"; elif [ "$age" -le 36 ]; then ok "latest backup snapshot ${age} h old"; else bad "latest backup snapshot ${age} h old (> 36 h)"; fi
  fi
else warn "backups not scheduled (Install → Backups)"; fi
[ -f /etc/cron.d/bookstack-disk ] && ok "disk watchdog installed" || warn "disk watchdog not installed (re-run Deploy)"

echo; echo "RESULT: $pass passed, $fail failed"
exit "$fail"
