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
ADMIN_USER=$(envget ADMIN_USER); ADMIN_USER="${ADMIN_USER:-admin}"
TORRENTS=$(envget TORRENTS_ENABLED)

echo "== Containers"
core="caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma"; [ "$TORRENTS" = true ] && core="$core qbittorrent"
for c in $core; do
  st=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null | tr -d '\n'); st="${st:-missing}"
  # "running starting" = inside the image's healthcheck start period (CWA 120 s, ABS 60 s,
  # librarian/shelfmark 30 s). Self-test is most often run in the first two minutes after a
  # Deploy, Update or reboot, and calling that a FAIL trains the admin to ignore the result.
  case "$st" in
    running\ healthy|running\ |running) ok "$c: $st";;
    running\ starting) warn "$c: still inside its healthcheck start period — re-run Self-test in a minute";;
    *) bad "$c: $st";;
  esac
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
# Shelfmark silently falls back to auth mode "none" (no login at all, everyone admin) when CWA's
# app.db is missing or unreadable, and it re-resolves the mode per request. Its healthcheck
# asserts this too, but nothing depends_on Shelfmark, so an unhealthy container just sits there.
curl -fs -m 5 http://127.0.0.1:8084/api/auth/check 2>/dev/null | grep -q '"auth_mode": *"cwa"' \
  && ok "shelfmark authenticates against Calibre-Web (auth_mode: cwa)" \
  || bad "shelfmark is NOT in auth_mode 'cwa': it is open to everyone (check ./cwa/config/app.db is readable, then Operations -> Restart shelfmark)"
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
# J03: the running portal image vs the code that was last deployed. Without this a stale
# librarian image looks perfectly healthy while none of the deployed fixes are actually live.
# /healthz?detail=1 answers JSON on loopback; "version" is the image's BUILD_VERSION build arg.
want_ver=$(cut -d' ' -f1 "$STACK_DIR/.version" 2>/dev/null)
got_ver=$(curl -fsS -m 5 'http://127.0.0.1:8090/healthz?detail=1' 2>/dev/null \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
if [ -z "$want_ver" ]; then warn "no $STACK_DIR/.version yet (run Install -> Deploy or Operations -> Update)"
elif [ -z "$got_ver" ]; then warn "the portal reports no version: its image predates the version stamp — Operations -> Update rebuilds it"
elif [ "$got_ver" = "$want_ver" ]; then ok "portal image matches the deployed code ($want_ver)"
else warn "STALE PORTAL IMAGE: running '$got_ver', deployed code is '$want_ver' — the container is older than $STACK_DIR/librarian; run Operations -> Update to rebuild"; fi

echo "== Security posture"
if command -v ufw >/dev/null; then
  ufw status | grep -q "Status: active" && ok "ufw active" || bad "ufw inactive"
  ufw status | grep -qE "^443/tcp.*Anywhere" && bad "443 open to Anywhere (should be Cloudflare ranges only)" || ok "443 not open to the world"
  ufw status | grep -qE "^22/tcp.*ALLOW.*Anywhere" && warn "SSH still public (Security → Lock SSH once Tailscale works)" || ok "SSH not public"
fi
for p in 8083 13378 8080 8090 8084 3001 9091 8286; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|:)$p\$" ; then
    ss -ltn | awk '{print $4}' | grep -E ":$p\$" | grep -qvE '^(127\.0\.0\.1|\[::1\]):' && bad "port $p bound to a non-loopback address" || ok "port $p loopback-only"
  fi
done
ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ':2019$' && bad "Caddy admin API listens on TCP :2019 (must be the unix socket)" || ok "Caddy admin API not on TCP"
pub=$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | while IFS=$'\t' read -r n p; do
        printf '%s' "$p" | tr ',' '\n' | grep -E '(0\.0\.0\.0|\[::\]|:::)[0-9]+->' | grep -qv ':6881->' && printf '%s ' "$n"; done)
[ -z "$pub" ] && ok "no container port published on a public address (except the torrent peer port)" || bad "published on a public address: $pub (Docker bypasses ufw)"
if [ "$TORRENTS" != true ] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -qE '^6881'; then warn "port 6881 open in ufw but torrents are off (re-run Install -> System)"; fi
# the EFFECTIVE sshd configuration: a provider drop-in (50-cloud-init.conf) can override ours
if command -v sshd >/dev/null; then
  if sshd -T 2>/dev/null | grep -qi '^passwordauthentication no'; then ok "SSH password login disabled (effective sshd -T)"
  elif [ -f /etc/ssh/sshd_config.d/01-bookstack.conf ]; then bad "SSH password login is still ON although 01-bookstack.conf disables it: another sshd drop-in overrides it (sshd -T | grep -i passwordauth)"
  else warn "SSH password login not disabled (add a key, re-run System step)"; fi
fi
if [ "$(envget SSH_LOCKED)" = true ] && ufw status 2>/dev/null | grep -qE "^22/tcp.*ALLOW.*Anywhere"; then bad "Lock SSH was chosen but port 22 is open to the world again (ufw delete allow 22/tcp)"; fi
[ "$(stat -c %a "$ENV_FILE" 2>/dev/null)" = "600" ] && ok ".env is 0600" || bad ".env permissions are not 0600"
[ "$(stat -c %U "$ENV_FILE" 2>/dev/null)" = root ] && ok ".env owned by root" || bad ".env is owned by $(stat -c %U "$ENV_FILE" 2>/dev/null) (containers run as uid 1000; run Configure)"
tsip=$(envget TAILSCALE_IP)
if [ -n "$tsip" ] && [ "$tsip" != 127.0.0.1 ] && command -v ip >/dev/null; then
  ip -o addr show 2>/dev/null | grep -qF " $tsip/" && ok "Tailscale IP $tsip is on an interface" \
    || warn "Tailscale IP $tsip is not on any interface (tailscaled down or IP changed): monitor./dl. unreachable; run Install -> Tailscale, then Configure"
fi
if command -v tailscale >/dev/null && ! ufw status 2>/dev/null | grep -qE '^22/tcp.*ALLOW.*Anywhere'; then
  exp=$(tailscale status --json 2>/dev/null | jq -r '.Self.KeyExpiry // "null"' 2>/dev/null)
  if [ "$exp" = null ] || [ -z "$exp" ]; then ok "Tailscale key expiry disabled"
  else days=$(( ($(date -d "$exp" +%s 2>/dev/null || echo 0) - $(date +%s)) / 86400 ))
    [ "$days" -gt 30 ] && ok "Tailscale key expires in $days d" || bad "Tailscale key expires in $days d and SSH is Tailscale-only: admin console -> Machines -> Disable key expiry"; fi
fi
if command -v fail2ban-client >/dev/null; then
  if systemctl is-active fail2ban >/dev/null 2>&1; then ok "fail2ban running"
  else bad "fail2ban is installed but NOT running, so not even the SSH jail is active (journalctl -u fail2ban; Security -> fail2ban)"; fi
  fail2ban-client status caddy-auth >/dev/null 2>&1 && ok "fail2ban caddy-auth jail active" || warn "caddy-auth jail not active (Security -> fail2ban)"
  fail2ban-client status caddy-device-auth >/dev/null 2>&1 && ok "fail2ban caddy-device-auth jail active" || warn "caddy-device-auth jail not active (Security -> fail2ban)"
  # Audiobookshelf has no login lockout of its own; this jail is it (J25).
  fail2ban-client status caddy-abs-login >/dev/null 2>&1 && ok "fail2ban caddy-abs-login jail active (Audiobookshelf login lockout)" || warn "caddy-abs-login jail not active: Audiobookshelf /login has no lockout (Security -> fail2ban)"
  # Both Caddy filters are host-scoped templates. An unrendered @@DOMAIN@@ matches nothing, so
  # the jail is "active" and counts zero failures — worse than no jail, because it looks fine.
  for f in /etc/fail2ban/filter.d/caddy-auth.conf /etc/fail2ban/filter.d/caddy-abs-login.conf; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    if grep -q '@@DOMAIN@@' "$f"; then bad "$b still carries the @@DOMAIN@@ placeholder: the jail matches nothing (Security -> fail2ban to re-render)"
    elif [ -n "$D" ] && ! grep -qF "$(printf '%s' "$D" | sed 's/\./\\./g')" "$f"; then bad "$b is scoped to a different domain than $D (Security -> fail2ban to re-render)"
    else ok "$b is rendered${D:+ for $D}"; fi
  done
fi
# the factory admin/admin123 must be dead (CWA's login needs the CSRF token of a session)
cj=$(mktemp); tok=$(curl -s -m 5 -c "$cj" http://127.0.0.1:8083/login 2>/dev/null | grep -oE 'name="csrf_token"[^>]*value="[^"]+"' | grep -oE 'value="[^"]+"' | cut -d'"' -f2)
lc=000
fus="admin"; [ "$ADMIN_USER" != admin ] && fus="admin $ADMIN_USER"   # the factory row may have been renamed to ADMIN_USER
for fu in $fus; do
  c1=$(curl -s -m 8 -b "$cj" -o /dev/null -w '%{http_code}' --data-urlencode "csrf_token=$tok" --data-urlencode "username=$fu" -d 'password=admin123&submit=&next=/' http://127.0.0.1:8083/login 2>/dev/null)
  case "$c1" in 302|303) lc=$c1; break;; 000) ;; *) lc=$c1;; esac
done; rm -f "$cj"
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
  sel=""; for c in auto_ingest_automerge duplicate_auto_resolve_enabled duplicate_notifications_enabled auto_metadata_update_tags auto_convert_ignored_formats koreader_sync_enabled; do
    case " $cols " in *" $c "*) sel="$sel IFNULL($c,'') AS $c,";; esac; done
  row=$(docker exec calibre-web sqlite3 -json /config/cwa.db "select ${sel%,} from cwa_settings limit 1" 2>/dev/null)
  val(){ printf '%s' "$row" | python3 -c 'import sys,json; r=json.load(sys.stdin); print(r[0].get(sys.argv[1],"") if r else "")' "$1" 2>/dev/null; }
  [ "$(val auto_ingest_automerge)" = new_record ] && ok "auto_ingest_automerge = new_record" || bad "auto_ingest_automerge = '$(val auto_ingest_automerge)' (must be new_record: Library -> Formats)"
  case " $cols " in *" duplicate_auto_resolve_enabled "*) [ "$(val duplicate_auto_resolve_enabled)" = 0 ] && ok "duplicate auto-resolve off" || bad "duplicate_auto_resolve_enabled=1 can merge two users' copies (Users -> Repair)";; esac
  # Per-user copies of one title are intentional here; CWA's duplicate notice asks the admin to
  # 'resolve' them, which means deleting another reader's book (J31).
  case " $cols " in *" duplicate_notifications_enabled "*) [ "$(val duplicate_notifications_enabled)" = 0 ] && ok "duplicate notifications off (per-user copies are intentional)" || bad "duplicate_notifications_enabled=1 invites deleting another user's copy (Users -> Repair)";; esac
  case " $cols " in *" auto_metadata_update_tags "*) [ "$(val auto_metadata_update_tags)" = 0 ] && ok "metadata fetch leaves tags alone" || bad "auto_metadata_update_tags=1 can replace owner:<user> tags (Users -> Repair)";; esac
  echo "  info: auto_convert_ignored_formats='$(val auto_convert_ignored_formats)' koreader_sync_enabled='$(val koreader_sync_enabled)'"
fi

echo "== Public reachability (via Cloudflare)"
if [ -n "$D" ]; then
  for h in books audio request shelf; do
    c=$(code "https://$h.$D/")
    case "$c" in 200|302|301|401) ok "https://$h.$D -> $c";; *) bad "https://$h.$D -> $c";; esac
  done
  # Caddy fetches Cloudflare's published ranges at start (trusted_proxies cloudflare). If that
  # fetch fails it starts ANYWAY with an empty trust list and silently falls back to the socket
  # peer — which behind Cloudflare is an EDGE address. Then the whole family shares one
  # rate-limit bucket and every fail2ban jail would ban Cloudflare itself. Nothing warned.
  # The four requests just made came through Cloudflare, so the access log must now hold a line
  # whose client_ip differs from remote_ip. If it never does, the list did not load.
  alog="$STACK_DIR/caddy/data/access.log"
  if [ -s "$alog" ]; then
    if tail -500 "$alog" 2>/dev/null | python3 -c '
import sys, json
for line in sys.stdin:
    try: r = json.loads(line).get("request") or {}
    except Exception: continue
    ci, ri = r.get("client_ip"), r.get("remote_ip")
    if ci and ri and ci != ri: sys.exit(0)
sys.exit(1)' 2>/dev/null; then ok "Caddy resolves the real client IP behind Cloudflare (client_ip != remote_ip in the access log)"
    else bad "Caddy is logging the Cloudflare EDGE address as the client: its Cloudflare IP list never loaded (no egress at start?). Rate limits are shared by everyone and a fail2ban ban would hit Cloudflare — restart caddy with working egress: docker compose restart caddy"; fi
  else warn "no Caddy access log yet; real-client-IP trust not verified"; fi
  # CWA ships convert-library / epub-fixer / cwa-logs / cwa-internal / reconnect with no auth at
  # all. The 403 is in the production Caddyfile only — assert it on the real edge, anonymously.
  for u in /cwa-convert-library-overview /cwa-internal/reconnect-db '/cwa-convert-library-start;x'; do
    c=$(code "https://books.$D$u")
    [ "$c" = 403 ] && ok "books.$D$u -> 403 (CWA admin job blocked at the edge)" || bad "books.$D$u -> $c (must be 403: Calibre-Web serves it unauthenticated)"
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
  tok=$(docker exec librarian python -m cwa kobo-url "$ADMIN_USER" 2>/dev/null | tr -d '"' | sed 's#.*/kobo/##; s#/.*##')
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
else warn "backups not scheduled (Install → Backups)"; fi
# An enabled timer proves nothing: scripts/backup.sh passing a flag this restic does not have
# (--retry-lock on Debian 12's 0.14) failed EVERY scheduled run while the install, the timer and
# this test all looked green. What matters is whether a backup actually succeeded, and recently.
# These checks run whether or not the timer is enabled, so a repository with no timer is caught.
if [ -f /etc/bookstack/restic.env ] && command -v restic >/dev/null; then
  age=$( (set -a; . /etc/bookstack/restic.env; set +a; restic snapshots --latest 1 --json 2>/dev/null) | python3 -c '
import sys, json, re, datetime
s = re.sub(r"\.\d+", "", json.load(sys.stdin)[-1]["time"]).replace("Z", "+00:00")
print(int((datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(s)).total_seconds() // 3600))' 2>/dev/null)
  if [ -z "$age" ]; then bad "NO restic snapshot exists (or the repository is unreachable): nothing has ever been backed up successfully — run scripts/backup.sh by hand and read the error"
  elif [ "$age" -le 36 ]; then ok "latest backup snapshot ${age} h old"
  else bad "latest backup snapshot ${age} h old (> 36 h): the scheduled backup is failing — journalctl -u bookstack-backup -n 50"; fi
  # ExecMainStatus is 0 for a unit that has never run, so only trust it once there is a start
  # timestamp to go with it.
  bts=$(systemctl show bookstack-backup.service -p ExecMainStartTimestamp --value 2>/dev/null)
  if systemctl is-failed bookstack-backup.service >/dev/null 2>&1; then bad "the last bookstack-backup run FAILED (journalctl -u bookstack-backup -n 50)"
  elif [ -n "$bts" ] && [ "$(systemctl show bookstack-backup.service -p ExecMainStatus --value 2>/dev/null)" = 0 ]; then ok "the last bookstack-backup run exited 0 ($bts)"
  elif [ -z "$bts" ]; then warn "bookstack-backup has never run yet (the timer fires at 01:00; Install -> Backups can run one now)"; fi
  if systemctl is-failed bookstack-restore-test.service >/dev/null 2>&1; then bad "the last restore test FAILED: the backup may not be restorable (journalctl -u bookstack-restore-test -n 50)"; fi
elif [ -f /etc/bookstack/restic.env ]; then bad "restic.env exists but restic is not installed: no backup can run (apt-get install restic)"; fi
[ -f /etc/cron.d/bookstack-disk ] && ok "disk watchdog installed" || warn "disk watchdog not installed (re-run Deploy)"
[ -e "$STACK_DIR/library/staging/.disk-paused" ] && warn "the disk watchdog has PAUSED the downloaders AND the portal's own imports (library/staging/.disk-paused); it clears itself below DISK_RESUME_PCT"
if [ -f /etc/cron.d/bookstack-disk ] || [ -f /etc/cron.d/bookstack-cfips ]; then
  systemctl is-active cron >/dev/null 2>&1 && ok "cron daemon running (disk watchdog, Cloudflare IP refresh)" || bad "cron is not running: the disk watchdog and the Cloudflare IP refresh never run (apt-get install cron; systemctl enable --now cron)"
fi

echo "== Alerts"
if [ -n "$(envget NOTIFY_WEBHOOK)" ]; then ok "alert webhook configured"
elif [ -n "$(envget SMTP_HOST)" ]; then ok "alerts go by e-mail (SMTP configured, no webhook)"
else bad "no alert channel: failed backups and a full disk reach nobody (Install -> Alerts)"; fi

echo; echo "RESULT: $pass passed, $fail failed"
exit "$fail"
