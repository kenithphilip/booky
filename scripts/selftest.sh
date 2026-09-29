#!/usr/bin/env bash
# Non-destructive health check of a deployed stack. Run from bookstack.sh (Operations →
# Self-test) or directly: bash /srv/bookstack/scripts/selftest.sh
# Exit code = number of failed checks.
set -uo pipefail
# an unknown terminal (Ghostty's xterm-ghostty...) must not break tput/clear here either
if [ -n "${TERM:-}" ] && command -v tput >/dev/null 2>&1 && ! tput -T "$TERM" longname >/dev/null 2>&1; then export TERM=xterm-256color; fi
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
TMPDIR="${TMPDIR:-/tmp}"
ENV_FILE="$STACK_DIR/.env"
fail=0; pass=0
# THE RULE, learned the hard way. `ok` may assert that a feature WORKS — something was
# exercised and behaved. It may NEVER assert that a guard rail is SET, because a setting
# having a safe value is not evidence that the thing it guards was ever tested.
#
# This file printed a green OK reading "metadata fetch leaves tags alone" whenever the column
# auto_metadata_update_tags was 0. That claim is about BEHAVIOUR; the check only read a column.
# It was green for a feature that was switched off and had never run — and it survived three
# audit rounds, which is precisely how the owner came to believe metadata fetching existed.
#
# So: a protective setting gets `guard`, which is counted separately and PRINTS DIFFERENTLY,
# and whose text must name the setting and its value rather than the harm it prevents.
# If you find yourself writing a guard message in the present tense about what cannot happen,
# it belongs in `ok` and needs a test that actually makes it happen.
ok(){ printf '  [ OK ] %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  [FAIL] %s\n' "$1"; fail=$((fail+1)); }
warn(){ printf '  [warn] %s\n' "$1"; }
guard(){ printf '  [set ] %s\n' "$1"; guards=$((guards+1)); }
guards=0
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
export COMPOSE_IGNORE_ORPHANS=true   # Authelia/Ephemera run from overlay files: expected, never remove them
compose(){ (cd "$STACK_DIR" && docker compose "$@"); }
# curl already prints 000 when it cannot connect, and then exits non-zero: "|| echo 000" made that
# "000000", so the one check that WANTS a refused connection (the origin lock) reported a failure
# One retry when nothing answered at all (000): a single request Cloudflare dropped on the way is
# not a finding, and it was the only thing a live FAIL on 2026-09-28 turned out to be.
code(){ local c t; for t in 1 2; do c=$(curl -s -m 12 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null) || true; [ "${c:-000}" != 000 ] && break; [ "$t" = 1 ] && sleep "${CODE_RETRY_SLEEP:-3}"; done; printf '%s' "${c:-000}"; }
# the same one retry for the edge probes that read headers or bodies (curl exits non-zero when
# nothing answered; an HTTP error status is still exit 0 without -f)
ecurl(){ local out rc t; for t in 1 2; do out=$(curl "$@" 2>/dev/null); rc=$?; [ "$rc" = 0 ] && break; [ "$t" = 1 ] && sleep "${CODE_RETRY_SLEEP:-3}"; done; printf '%s' "$out"; return "$rc"; }
# NEVER `producer | grep -q`: with pipefail on, grep -q quits at its first match, the producer dies
# of SIGPIPE writing the rest, and the pipeline "fails" although the match was found (sshd -T,
# Caddy's access log, `ip -o addr` on a box with dozens of Docker interfaces: all false alarms on
# the real server). Take the output first, then search it: has "<pattern>" <<< "$(producer)".
D=$(envget DOMAIN)
ADMIN_USER=$(envget ADMIN_USER); ADMIN_USER="${ADMIN_USER:-admin}"
TORRENTS=$(envget TORRENTS_ENABLED)
# FlareSolverr runs while Shelfmark is set to use it or Ephemera is on (bookstack.sh solver_on)
SOLVER=false; { [ "$(envget FLARESOLVERR_ENABLED)" = true ] || [ "$(envget EPHEMERA_ENABLED)" = true ]; } && SOLVER=true
# SELFTEST_SCHEDULED=1 is the hourly timer (bookstack.sh install_selftest_timer). It skips the two
# probes that cost something when repeated 24 times a day: the factory-password login, which
# counts against Calibre-Web's 40-per-day login limit for that username (cps/web.py
# @limiter.limit("40/day", key=username)), and the remote restic query, which is repository
# transactions every hour (the backup job reports itself to Kuma instead). The post-reboot run
# and every run from the menu still make both.
SCHEDULED="${SELFTEST_SCHEDULED:-0}"

echo "== Containers"
core="caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma"; [ "$TORRENTS" = true ] && core="$core qbittorrent"
[ "$SOLVER" = true ] && core="$core flaresolverr"
[ "$(envget SEEDBOX_ENABLED)" = true ] && core="$core syncthing"
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
for c in authelia ephemera; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true); [ -n "$st" ] && { [ "$st" = running ] && ok "$c: running (optional)" || warn "$c: $st (optional)"; }
done
oom=$(for c in $(docker ps -aq 2>/dev/null); do docker inspect -f '{{.Name}} {{.State.OOMKilled}}' "$c" 2>/dev/null; done | awk '$2=="true"{print $1}' | tr -d / | tr '\n' ' ')
[ -z "$oom" ] && ok "no container was OOM-killed" || bad "OOM-killed: $oom (docker inspect ... OOMKilled; raise its mem_limit or the VPS size)"
avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null); [ -n "$avail" ] && { [ "$avail" -ge 524288 ] && ok "MemAvailable $((avail/1024)) MiB" || warn "MemAvailable only $((avail/1024)) MiB (< 512 MiB)"; }
# FlareSolverr + Ephemera were measured at ~1.1 GB worst case on top of the stack's ~2.2 GB peak
# (2026-09-25): they fit the 4 GB plan. Below ~3.5 GB of RAM they do not.
if [ "$SOLVER" = true ]; then tot=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null); [ "${tot:-0}" -gt 0 ] && [ "${tot:-0}" -lt 3500000 ] && warn "FlareSolverr/Ephemera on a machine with $((tot/1024)) MiB of RAM: measured worst case needs ~3.3 GB for the stack"; fi

echo "== Local endpoints"
curl -fs -m 5 http://127.0.0.1:8090/healthz >/dev/null && ok "portal /healthz" || bad "portal /healthz"
curl -fs -m 5 http://127.0.0.1:8084/api/health >/dev/null && ok "shelfmark /api/health" || bad "shelfmark /api/health"
# Shelfmark silently falls back to auth mode "none" (no login at all, everyone admin) when CWA's
# app.db is missing or unreadable AT START. The mode is then FIXED for the life of the process:
# in the pinned image shelfmark/config/env.py evaluates `CWA_DB_PATH = _resolve_cwa_db_path()`
# once at module import and core/auth_modes.py's determine_auth_mode() only tests the
# TRUTHINESS of that cached Path, never its existence. So this first check proves Shelfmark
# STARTED with a readable app.db and nothing more — it does NOT re-resolve per request, and an
# earlier comment here said it did. Measured: with app.db moved away afterwards, /api/auth/check
# still answered "cwa" while every login answered 500 "Database configuration error".
SM_AUTH=$(envget SHELFMARK_AUTH_METHOD); SM_AUTH=${SM_AUTH:-cwa}
if [ "$SM_AUTH" = proxy ]; then
  # L05, behind the Authelia gate: Remote-User from Caddy is the login. Without it, refused.
  grep -q '"auth_mode": *"proxy"' <<< "$(curl -fs -m 5 http://127.0.0.1:8084/api/auth/check 2>/dev/null)" \
    && ok "shelfmark in auth_mode 'proxy' (one login through the Authelia gate)" \
    || bad "shelfmark is NOT in auth_mode 'proxy' although the gate is on: check SHELFMARK_AUTH_METHOD and Operations -> Restart shelfmark"
  smp=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8084/api/settings 2>/dev/null || echo 000)
  [ "$smp" = 401 ] && ok "shelfmark refuses a request that carries no gate identity (401)" \
    || bad "shelfmark answered $smp to a request with no Remote-User (expected 401): it may be open"
else
grep -q '"auth_mode": *"cwa"' <<< "$(curl -fs -m 5 http://127.0.0.1:8084/api/auth/check 2>/dev/null)" \
  && ok "shelfmark STARTED in auth_mode 'cwa' (Calibre-Web accounts)" \
  || bad "shelfmark is NOT in auth_mode 'cwa': it is open to everyone (check ./cwa/config/app.db is readable, then Operations -> Restart shelfmark)"
# The assertion that does NOT depend on the cached path: can Shelfmark still reach app.db RIGHT
# NOW? A deliberately wrong password must be answered 401 by the CWA lookup. It answers 500
# "Database configuration error" when the ./cwa/config bind-mount was lost, app.db was renamed
# by a restore, or CWA's config dir was wiped — the one regression the check above cannot see,
# and the one where no family member can sign in at shelf.<domain> while Docker health, this
# script and any Uptime Kuma keyword monitor on "cwa" all read normal.
# One attempt per run, with a name no reader will ever have: Shelfmark locks an account for 30
# minutes after 10 failed attempts and then answers 429 regardless of the database, so running
# this ten times within half an hour turns it inconclusive (a warn) rather than green.
# The name changes every run: Shelfmark counts failures PER USERNAME in memory and never forgets
# a count below 10, so one fixed name reached the lockout after ten runs — ten hours, now that the
# self-test is hourly — and every tenth run came back inconclusive.
probe_user="__bookstack_selftest_$(date +%s)_$$__"
smc=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      -d "{\"username\":\"$probe_user\",\"password\":\"not-a-real-password\"}" \
      http://127.0.0.1:8084/api/auth/login 2>/dev/null || echo 000)
case "$smc" in
  401) ok "shelfmark can still read Calibre-Web's app.db (a wrong password is rejected, not 500)";;
  500) bad "shelfmark CANNOT read Calibre-Web's app.db any more: every login fails with 'Database configuration error' and NOBODY can sign in at shelf.$D, even though auth_mode still says 'cwa'. Check the ./cwa/config bind-mount and that app.db exists, then Operations -> Restart shelfmark";;
  429) warn "shelfmark rate-limited the probe account (self-test run more than 10 times in 30 min): app.db readability UNVERIFIED this run";;
  000) warn "could not probe shelfmark's login route";;
  *)   warn "shelfmark login probe answered $smc (expected 401)";;
esac
fi
curl -fs -m 5 -o /dev/null http://127.0.0.1:8083/login && ok "calibre-web /login" || bad "calibre-web /login"
curl -fs -m 5 -o /dev/null http://127.0.0.1:13378/healthcheck && ok "audiobookshelf /healthcheck" || bad "audiobookshelf /healthcheck"
# L05: behind the gate Audiobookshelf's web login goes through Authelia (OpenID Connect)
if [ "$(envget AUTHELIA_ENABLED)" = true ] && [ -n "$(envget ABS_OIDC_SECRET)" ]; then
  grep -q '"openid"' <<< "$(curl -fs -m 5 http://127.0.0.1:13378/status 2>/dev/null)" \
    && ok "audiobookshelf signs in through Authelia (OpenID Connect)" \
    || warn "audiobookshelf does not offer the Authelia sign-in: readers log in twice there (Security -> Authelia: enable again, or check ABS_TOKEN)"
fi
if [ "$SOLVER" = true ]; then
  grep -q '"ok"' <<< "$(curl -fs -m 5 http://127.0.0.1:8191/health 2>/dev/null)" && ok "flaresolverr /health" || bad "flaresolverr /health: protection challenges cannot be solved (Operations -> Logs -> flaresolverr)"
  # what Shelfmark actually uses is the NAME on the compose network, not the loopback port
  if [ "$(envget FLARESOLVERR_ENABLED)" = true ]; then
    docker exec shelfmark curl -fs -m 5 http://flaresolverr:8191/health >/dev/null 2>&1 \
      && ok "shelfmark reaches flaresolverr:8191 (its external bypasser)" \
      || bad "shelfmark cannot reach http://flaresolverr:8191, which it is configured to use: every protected source fails (Operations -> FlareSolverr, or Logs -> shelfmark)"
  fi
fi

echo "== Config validity"
compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && ok "Caddyfile validates" || bad "Caddyfile does not validate (compose exec caddy caddy validate)"
if grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" 2>/dev/null; then
  compose -f docker-compose.yml -f docker-compose.authelia.yml exec -T authelia authelia validate-config --config /config/configuration.yml >/dev/null 2>&1 \
    && ok "Authelia config validates (gate active)" || bad "Authelia gate is in the Caddyfile but its config does not validate"
fi
[ -f "$STACK_DIR/caddy/cf-origin-pull-ca.pem" ] && ok "Cloudflare origin-pull CA present" || bad "cf-origin-pull-ca.pem missing (run Cloudflare step)"
# Library -> Seedbox: finished seedbox downloads (through Syncthing) handed to Shelfmark every minute.
# The job refuses to run and pauses the folder when one here is not Receive Only, so a run that
# succeeded recently also means every folder was Receive Only then.
if [ -f /etc/systemd/system/bookstack-seedbox.timer ]; then
  sbst=$(python3 -c 'import json,time; d=json.load(open("/etc/bookstack/seedbox.state")); print(int(time.time())-int(d.get("last_ok",0)), d.get("fails",0), (d.get("last_error") or "-")[:200])' 2>/dev/null)
  sbage=${sbst%% *}; sbrest=${sbst#* }; sbfails=${sbrest%% *}; sberr=${sbrest#* }
  if [ -z "$sbst" ]; then warn "seedbox sync is on but has not completed a run yet (journalctl -u bookstack-seedbox -n 20; the seedbox side may not be set up yet)"
  elif [ "${sbage:-999999}" -le 900 ]; then ok "seedbox sync ran OK $(( sbage / 60 )) min ago (seedbox connected; every folder here Receive Only)"
  else bad "seedbox sync has not succeeded for $(( sbage / 60 )) min (${sbfails} failed tries): ${sberr} — Library -> Seedbox -> Check the connection"; fi
fi
# Seedbox safety: Shelfmark's own clean-up after an import must leave the seedbox alone
sm_act=$(docker exec shelfmark /app/.venv/bin/python -c 'from shelfmark.core.config import config; print(config.get("PROWLARR_TORRENT_ACTION"), config.get("PROWLARR_USENET_ACTION"))' 2>/dev/null | tail -1)
case "$sm_act" in
  "keep copy") ok "Shelfmark leaves seedbox downloads in place after an import (torrents: keep, Usenet: copy)";;
  "") warn "could not read Shelfmark's completion actions";;
  *) bad "Shelfmark's completion actions are '$sm_act', not 'keep copy': it could remove torrents or Usenet jobs from the seedbox after an import (re-run Install -> Deploy)";;
esac
# L08: the canary journey (a test reader's path, twice a day)
if [ -f /etc/systemd/system/bookstack-canary.timer ]; then
  if systemctl is-failed bookstack-canary.service >/dev/null 2>&1; then bad "the last canary journey FAILED: what a family member would hit (journalctl -u bookstack-canary -n 60; /admin -> Canary journey)"
  else ok "canary journey scheduled (06:20 / 18:20); last run did not fail"; fi
fi
# v5.7 comics (docs/COMICS.md): the device-copy job, its converter, the archive tools, the metadata
if [ "$(envget COMICS_ENABLED)" = true ]; then
  [ -f /etc/cron.d/bookstack-comics ] && ok "comic device copies scheduled (every 3 min)" || bad "comics are on but the device-copy job is not scheduled (re-run Deploy)"
  kcc=$(envget IMG_KCC); kcc="${kcc:-ghcr.io/ciromattia/kcc:v12.0.0}"
  docker image inspect "$kcc" >/dev/null 2>&1 && ok "KCC image present ($kcc)" || warn "KCC image $kcc is not pulled yet: Kobo/Kindle copies of comics wait for it (re-run Deploy)"
  docker exec librarian sh -c 'command -v unar >/dev/null' 2>/dev/null && ok "the portal can open CBR and CB7 comics (unar)" || bad "the portal image has no unar: CBR/CB7 comics fail to import (Operations -> Update rebuilds it)"
  if [ "$SCHEDULED" != 1 ]; then
    cm=$(docker exec librarian python -m comicmeta check 2>/dev/null | tail -1)
    case "$cm" in *'"ok": true'*) ok "comic metadata providers answer ($(printf '%s' "$cm" | grep -o '"providers": {[^}]*}' | sed 's/"providers": //'))";;
      *) warn "a comic metadata provider did not answer: ${cm:-no reply} (Library -> Comics)";; esac
  fi
fi
# v6.0.1: a Shelfmark setting its failed downloads point at (their errors stay listed for an hour)
if [ "$SCHEDULED" != 1 ]; then
  if sp=$(docker exec librarian python -c 'import shelfmark_api; print("\n".join(shelfmark_api.config_problems()))' 2>/dev/null); then
    if [ -n "$sp" ]; then while IFS= read -r l; do [ -n "$l" ] && warn "$l"; done <<< "$sp"
    else ok "Shelfmark's recent failed downloads point at none of its settings"; fi
  fi
fi
# L14: which certificate the origin lock accepts
case "$(envget AOP_MODE)" in
  zone) ok "origin lock: only this zone's own Cloudflare client certificate is accepted";;
  both) warn "origin lock: switching to this zone's own certificate is unfinished (Caddy trusts both): Security -> Origin lock";;
  *) warn "origin lock uses Cloudflare's SHARED client certificate (any Cloudflare zone can present it): Security -> Origin lock";;
esac
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
  ufws=$(ufw status 2>/dev/null)
  grep -q "Status: active" <<< "$ufws" && ok "ufw active" || bad "ufw inactive"
  grep -qE "^443/tcp.*Anywhere" <<< "$ufws" && bad "443 open to Anywhere (should be Cloudflare ranges only)" || ok "443 not open to the world"
  grep -qE "^22/tcp.*ALLOW.*Anywhere" <<< "$ufws" && warn "SSH still public (Security → Lock SSH once Tailscale works)" || ok "SSH not public"
fi
listen=$(ss -ltn 2>/dev/null | awk '{print $4}')
for p in 8083 13378 8080 8090 8084 3001 9091 8286; do
  if grep -qE "(^|:)$p\$" <<< "$listen"; then
    grep -E ":$p\$" <<< "$listen" | grep -vE '^(127\.0\.0\.1|\[::1\]):' | grep -q . && bad "port $p bound to a non-loopback address" || ok "port $p loopback-only"
  fi
done
grep -qE ':2019$' <<< "$listen" && bad "Caddy admin API listens on TCP :2019 (must be the unix socket)" || ok "Caddy admin API not on TCP"
pub=$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | while IFS=$'\t' read -r n p; do
        printf '%s' "$p" | tr ',' '\n' | grep -E '(0\.0\.0\.0|\[::\]|:::)[0-9]+->' | grep -qv ':6881->' && printf '%s ' "$n"; done)
[ -z "$pub" ] && ok "no container port published on a public address (except the torrent peer port)" || bad "published on a public address: $pub (Docker bypasses ufw)"
if [ "$TORRENTS" != true ] && command -v ufw >/dev/null && grep -qE '^6881' <<< "$(ufw status 2>/dev/null)"; then warn "port 6881 open in ufw but torrents are off (re-run Install -> System)"; fi
# the EFFECTIVE sshd configuration: a provider drop-in (50-cloud-init.conf) can override ours
if command -v sshd >/dev/null; then
  # captured first: under pipefail, grep -q quitting at the match kills sshd -T with SIGPIPE mid-way
  # through its long output and the pipeline "fails" although the setting is right (seen on Debian 13)
  sshd_eff=$(sshd -T 2>/dev/null || true)
  if printf '%s\n' "$sshd_eff" | grep -qi '^passwordauthentication no'; then ok "SSH password login disabled (effective sshd -T)"
  elif [ -f /etc/ssh/sshd_config.d/01-bookstack.conf ]; then bad "SSH password login is still ON although 01-bookstack.conf disables it: another sshd drop-in overrides it (sshd -T | grep -i passwordauth)"
  else warn "SSH password login not disabled (add a key, re-run System step)"; fi
fi
if [ "$(envget SSH_LOCKED)" = true ] && grep -qE "^22/tcp.*ALLOW.*Anywhere" <<< "$(ufw status 2>/dev/null)"; then bad "Lock SSH was chosen but port 22 is open to the world again (ufw delete allow 22/tcp)"; fi
[ "$(stat -c %a "$ENV_FILE" 2>/dev/null)" = "600" ] && ok ".env is 0600" || bad ".env permissions are not 0600"
[ "$(stat -c %U "$ENV_FILE" 2>/dev/null)" = root ] && ok ".env owned by root" || bad ".env is owned by $(stat -c %U "$ENV_FILE" 2>/dev/null) (containers run as uid 1000; run Configure)"
tsip=$(envget TAILSCALE_IP)
if [ -n "$tsip" ] && [ "$tsip" != 127.0.0.1 ] && command -v ip >/dev/null; then
  grep -qF " $tsip/" <<< "$(ip -o addr show 2>/dev/null)" && ok "Tailscale IP $tsip is on an interface" \
    || warn "Tailscale IP $tsip is not on any interface (tailscaled down or IP changed): monitor./dl. unreachable; run Install -> Tailscale, then Configure"
fi
if command -v tailscale >/dev/null && ! grep -qE '^22/tcp.*ALLOW.*Anywhere' <<< "$(ufw status 2>/dev/null)"; then
  exp=$(tailscale status --json 2>/dev/null | jq -r '.Self.KeyExpiry // "null"' 2>/dev/null)
  if [ "$exp" = null ] || [ -z "$exp" ]; then ok "Tailscale key expiry disabled"
  else days=$(( ($(date -d "$exp" +%s 2>/dev/null || echo 0) - $(date +%s)) / 86400 ))
    [ "$days" -gt 30 ] && ok "Tailscale key expires in $days d" || bad "Tailscale key expires in $days d and SSH is Tailscale-only: admin console -> Machines -> Disable key expiry"; fi
fi
# The tailnet ACL is load-bearing for one specific reason, so stop assuming it is applied.
# Shelfmark's cover cache is an AUTHENTICATED arbitrary-URL fetcher: GET
# /api/covers/<id>?url=<base64url> is @login_required only, and its SSRF fence
# (core/image_cache.py::_prepare_safe_url) rejects only is_private/is_loopback/is_link_local/
# is_reserved — which in Python does NOT include 100.64.0.0/10. Measured in the pinned image:
# 169.254.169.254 and 172.17.0.1 blocked, 100.64.1.5 ALLOWED. The portal's own fence
# (librarian/worker.py::_check_target) does block that range, so the stack already decided it
# is in scope. Caddy cannot help — this is outbound — and the only compensating control is the
# tag:bookstack tailnet ACL, which until now was prose in docker-compose.yml.
# A connection that SUCCEEDS is conclusive: the ACL is not denying. A connection that does not
# is consistent with the ACL but is not proof (the peer may simply be off), and is reported as
# such rather than as a pass this script cannot actually make.
if command -v tailscale >/dev/null && docker inspect -f '{{.State.Running}}' shelfmark 2>/dev/null | grep -q true; then
  peers=$(tailscale status --json 2>/dev/null | jq -r '(.Peer // {}) | to_entries[] | .value.TailscaleIPs[0] // empty' 2>/dev/null | grep -E '^100\.' | head -3)
  if [ -z "$peers" ]; then warn "no tailnet peers to probe: Shelfmark's cover-proxy exposure to the tailnet is UNVERIFIED"
  else
    reach=""
    for ip in $peers; do
      for port in 22 80 443; do
        docker exec shelfmark python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(3)
sys.exit(0 if s.connect_ex(('$ip',$port))==0 else 1)" >/dev/null 2>&1 && { reach="$reach $ip:$port"; break; }
      done
    done
    [ -z "$reach" ] && warn "no tailnet peer answered a TCP connect from inside shelfmark (consistent with the tag:bookstack ACL denying outbound, but not proof — a peer that is simply off looks the same)" \
      || bad "the shelfmark container REACHED the tailnet:$reach. Its cover proxy is an authenticated arbitrary-URL fetcher whose SSRF fence does not cover 100.64.0.0/10, so any family member with a Calibre-Web login can make it probe your other tailnet devices. Apply the tag:bookstack ACL (no outbound), or turn Shelfmark's COVERS_CACHE_ENABLED off — see docs/DECISIONS-PENDING.md"
  fi
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
if [ "$SCHEDULED" != 1 ]; then
cj=$(mktemp); tok=$(curl -s -m 5 -c "$cj" http://127.0.0.1:8083/login 2>/dev/null | grep -oE 'name="csrf_token"[^>]*value="[^"]+"' | grep -oE 'value="[^"]+"' | cut -d'"' -f2)
lc=000
fus="admin"; [ "$ADMIN_USER" != admin ] && fus="admin $ADMIN_USER"   # the factory row may have been renamed to ADMIN_USER
for fu in $fus; do
  c1=$(curl -s -m 8 -b "$cj" -o /dev/null -w '%{http_code}' --data-urlencode "csrf_token=$tok" --data-urlencode "username=$fu" -d 'password=admin123&submit=&next=/' http://127.0.0.1:8083/login 2>/dev/null)
  case "$c1" in 302|303) lc=$c1; break;; 000) ;; *) lc=$c1;; esac
done; rm -f "$cj"
case "$lc" in 302|303) bad "Calibre-Web still accepts admin/admin123 (Users -> Reset password NOW)";; 000) warn "could not test the factory admin password";; *) ok "factory admin password rejected";; esac
fi
grep -q '"isInit":false' <<< "$(curl -s -m 5 http://127.0.0.1:13378/status 2>/dev/null)" && bad "Audiobookshelf has NO root user: the first visitor becomes admin (Library -> Audiobookshelf)" || ok "Audiobookshelf initialised"
# Per-check scratch files: mktemp, never a fixed name under a shared $TMPDIR. These two carry
# the ONLY per-user isolation FAIL text this script produces — including in the unattended
# post-reboot run, whose whole point is to speak up when nobody is watching — and this script
# runs as root with TMPDIR defaulting to /tmp, where a pre-existing symlink at a predictable
# name is followed by `>` and the named list comes back blank.
# The helper's exit status is also read SEPARATELY from the file: with `pipefail` set, a
# python3 that cannot start at all (127) used to land on the same branch as "not isolated" and
# print a FAIL with an empty list, which is the same false alarm in the other direction.
uiso=$(mktemp); aiso=$(mktemp); trap 'rm -f "$uiso" "$aiso"' EXIT
ulist=$(docker exec librarian python -m cwa list 2>/dev/null || true)
if [ -z "$ulist" ]; then warn "could not list users (portal not running?)"
else
  printf '%s' "$ulist" | python3 -c 'import sys,json; u=json.load(sys.stdin); bad=[x["name"] for x in u if not x["is_admin"] and not x["isolated"]]; print(",".join(bad)); sys.exit(1 if bad else 0)' >"$uiso" 2>/dev/null; rc=$?
  if [ "$rc" = 0 ]; then ok "every non-admin user is tag-isolated"
  elif [ "$rc" = 1 ] && [ -s "$uiso" ]; then bad "NOT isolated: $(cat "$uiso") (Users → repair isolation)"
  else warn "could not check per-user tag isolation (helper exited $rc with no list): isolation is UNVERIFIED, not proven good"; fi
fi
# config_allow_reverse_proxy_header_login is the CWA setting the Remote-* probe above is often
# assumed to test and does not: that probe runs through Caddy, which strips the header first.
# CWA v4.0.6 really does have the column (cps/config_sql.py:153, default False) and nothing in
# this stack ever writes it — librarian/cwa.py's hardening writes config_public_reg,
# config_anonbrowse and config_remote_login only — so an admin who ticks "Allow Reverse Proxy
# Authentication" in CWA's own UI, or a future CWA default change, would turn header login on
# with nothing anywhere to notice. Read it here, alongside the other two.
# L05: with the Authelia gate on (and its secret), header login is ON by design — one login —
# and must name exactly Remote-User, the header Caddy strips on every path; with the gate off
# it must be OFF.
GATE_ON=false; [ "$(envget AUTHELIA_ENABLED)" = true ] && [ -n "$(envget GATE_SECRET)" ] && GATE_ON=true
docker exec calibre-web sqlite3 /config/app.db "select config_public_reg, config_kobo_sync, IFNULL(config_allow_reverse_proxy_header_login,0), IFNULL(config_reverse_proxy_login_header_name,''), IFNULL(config_reverse_proxy_auto_create_users,0) from settings" 2>/dev/null | { IFS='|' read -r reg kobo rph rpn rpa; [ "${reg:-1}" = 0 ] && guard "config_public_reg=0 (no public registration)" || bad "CWA public registration is ON"; [ "${kobo:-0}" = 1 ] && ok "CWA Kobo sync on" || warn "CWA Kobo sync off (Users menu → enable)"
  if [ "$GATE_ON" = true ]; then
    if [ "${rph:-0}" = 1 ] && [ "$rpn" = Remote-User ] && [ "${rpa:-0}" = 0 ]; then ok "CWA header login ON for the Authelia gate (Remote-User, which Caddy strips on every path; no auto-created accounts)"
    else bad "CWA header login is not set up for the gate (on=${rph:-?} header='${rpn}' auto-create=${rpa:-?}): readers log in twice at books. — run Security -> Authelia again"; fi
  else
    [ "${rph:-1}" = 0 ] && guard "config_allow_reverse_proxy_header_login=0" || bad "CWA config_allow_reverse_proxy_header_login=1 while the Authelia gate is OFF: anything that can reach 127.0.0.1:8083 with a Remote-User header becomes that user. Deploy turns it off again (cwa harden), or untick 'Allow Reverse Proxy Authentication' in Calibre-Web -> Admin -> Basic Configuration"
  fi; }
if [ -n "$(envget ABS_TOKEN)" ]; then
  alist=$(docker exec librarian python -m abs list-users 2>/dev/null || true)
  if [ -z "$alist" ]; then bad "Audiobookshelf API not reachable with ABS_TOKEN (Library → Audiobookshelf to re-run setup)"
  else
    printf '%s' "$alist" | python3 -c 'import sys,json; u=json.load(sys.stdin); bad=[x["username"] for x in u if x["type"]=="user" and not x["isolated"]]; print(",".join(bad)); sys.exit(1 if bad else 0)' >"$aiso" 2>/dev/null; rc=$?
    if [ "$rc" = 0 ]; then ok "every Audiobookshelf user is tag-restricted"
    elif [ "$rc" = 1 ] && [ -s "$aiso" ]; then bad "Audiobookshelf users NOT tag-restricted: $(cat "$aiso") (Users → Repair)"
    else warn "could not check Audiobookshelf tag restriction (helper exited $rc with no list): UNVERIFIED, not proven good"; fi
  fi
else warn "Audiobookshelf automation not set up (Library → Audiobookshelf): audiobooks must be tagged by hand"; fi

echo "== Isolation invariants (the library itself)"
# Everything above checks the USER side: that each account is tag-restricted. Nothing checked
# the BOOK side, and that is the half the whole product rests on. In the pinned CWA image
# cps/db.py::common_filters builds
#   pos_content_tags_filter = true() if postags_list == [''] else Books.tags.any(Tags.name.in_(postags_list))
# and cps/kobo.py, cps/opds.py and the web UI all go through it, as does
# librarian/library.py::_scope_sql. So a book carrying NO `owner:%` tag, or `owner:<someone
# who no longer exists>`, matches no non-admin ANYWHERE: not the Calibre-Web UI, not OPDS, not
# Kobo sync, not the portal. It is invisible to its owner, invisible to everyone else, and it
# still occupies the 80 GB disk for ever. Every route that bypasses the portal produces one:
# Calibre-Web's own upload form, a file scp'd into library/ingest, an admin editing tags in
# CWA's UI, a `needs-tag` format, a removed user (cwa.remove_user touches app.db only, never
# metadata.db), a renamed admin. The portal's needs-tag queue covers only rows the portal
# itself created — the one route that was already fine.
# Two pure reads, one query each, against the live metadata.db inside the container.
untagged=$(docker exec calibre-web sqlite3 /calibre-library/metadata.db \
  "select count(*) from books b where not exists (select 1 from books_tags_link l join tags t on t.id=l.tag where l.book=b.id and t.name like 'owner:%')" 2>/dev/null | tr -dc 0-9)
if [ -z "$untagged" ]; then warn "could not read library/books/metadata.db (calibre-web down?): the owner-tag invariant is UNVERIFIED"
elif [ "$untagged" = 0 ]; then ok "every book in the library carries an owner:<user> tag"
else
  names=$(docker exec calibre-web sqlite3 /calibre-library/metadata.db \
    "select b.id || ' ' || substr(b.title,1,40) from books b where not exists (select 1 from books_tags_link l join tags t on t.id=l.tag where l.book=b.id and t.name like 'owner:%') order by b.id limit 3" 2>/dev/null | tr '\n' ';')
  bad "$untagged book(s) carry NO owner:<user> tag and are therefore invisible to every non-admin — in Calibre-Web, OPDS, Kobo sync and the portal alike — while still using disk. First: ${names:-?} (Calibre-Web -> Admin, or the portal's needs-tag queue, to set the owner tag)"
fi
if [ -n "$ulist" ]; then
  # owner:<x> tags whose <x> is not a CWA account any more. `cwa rename-user` and `cwa
  # remove-user` both leave metadata.db untouched, so a rename or a removal orphans every book
  # that user owned in one step.
  otags=$(docker exec calibre-web sqlite3 /calibre-library/metadata.db \
    "select distinct substr(t.name,7) from tags t join books_tags_link l on l.tag=t.id where t.name like 'owner:%'" 2>/dev/null)
  if [ -z "$otags" ] && [ "$untagged" != 0 ]; then :
  elif [ -z "$otags" ]; then warn "no owner:<user> tags found at all in metadata.db"
  else
    known=$(printf '%s' "$ulist" | python3 -c 'import sys,json; print("\n".join(x["name"] for x in json.load(sys.stdin)))' 2>/dev/null)
    orph=$(printf '%s\n' "$otags" | while read -r u; do [ -n "$u" ] || continue
             printf '%s\n' "$known" | grep -qxF "$u" || printf '%s ' "$u"; done)
    [ -z "$orph" ] && ok "every owner:<user> tag names an existing Calibre-Web account" \
      || bad "owner tag(s) naming accounts that no longer exist: $orph — every book with one is invisible to everybody (re-create the account, or re-tag those books to a current user)"
  fi
fi
# A dropbox folder whose CWA account does not exist is a black hole: librarian/worker.py's
# _known_user returns None, logs "dropbox/%s is not an existing user's folder; ignored" ONCE
# per process (_WARNED is a module-level set), and scan_dropbox_once skips it for ever. Four
# routes create one: step_user_remove keeps the folder on purpose, ensure_admin_name renames
# the admin without moving it, docker-compose.ephemera.yml bind-mounts
# ./library/dropbox/${EPHEMERA_OWNER} at container-creation time, and the Torrents help screen
# has the admin type category save paths like /dropbox/alice by hand. Anything writing into
# one fills an 80 GB disk with files no listing, no /admin count and no request row mentions.
# Hidden entries are skipped so the `.removed-<user>/` convention stays quiet.
if [ -n "$ulist" ] && [ -d "$STACK_DIR/library/dropbox" ]; then
  known=$(printf '%s' "$ulist" | python3 -c 'import sys,json; print("\n".join(x["name"] for x in json.load(sys.stdin)))' 2>/dev/null)
  if [ -z "$known" ]; then warn "could not list CWA users: orphan dropbox folders UNVERIFIED"
  else
    orphfull=""; orphempty=""
    for d in "$STACK_DIR"/library/dropbox/*/; do
      [ -d "$d" ] || continue
      u=$(basename "$d"); case "$u" in .*) continue;; esac
      printf '%s\n' "$known" | grep -qxF "$u" && continue
      n=$(find "$d" -mindepth 1 ! -name '.*' 2>/dev/null | wc -l | tr -dc 0-9)
      if [ "${n:-0}" -gt 0 ]; then orphfull="$orphfull $u($n)"; else orphempty="$orphempty $u"; fi
    done
    if [ -n "$orphfull" ]; then bad "dropbox folder(s) with no Calibre-Web account HOLDING FILES:$orphfull — nothing scans them, nothing counts them, and Shelfmark/qBittorrent/Ephemera may still be writing into them. Move the files into a current user's dropbox (or delete them), and check EPHEMERA_OWNER and the qBittorrent category save paths"
    elif [ -n "$orphempty" ]; then warn "empty dropbox folder(s) with no Calibre-Web account:$orphempty (harmless now, but anything filed there will never be imported)"
    else ok "every dropbox folder belongs to an existing Calibre-Web account"; fi
  fi
fi

echo "== Isolation invariants (cwa.db)"
cols=$(docker exec calibre-web sqlite3 /config/cwa.db "pragma table_info(cwa_settings)" 2>/dev/null | cut -d'|' -f2 | tr '\n' ' ')
if [ -z "$cols" ]; then warn "could not read cwa_settings"
else
  sel=""; for c in auto_ingest_automerge duplicate_auto_resolve_enabled duplicate_notifications_enabled auto_metadata_update_tags auto_convert_ignored_formats koreader_sync_enabled; do
    case " $cols " in *" $c "*) sel="$sel IFNULL($c,'') AS $c,";; esac; done
  row=$(docker exec calibre-web sqlite3 -json /config/cwa.db "select ${sel%,} from cwa_settings limit 1" 2>/dev/null)
  val(){ printf '%s' "$row" | python3 -c 'import sys,json; r=json.load(sys.stdin); print(r[0].get(sys.argv[1],"") if r else "")' "$1" 2>/dev/null; }
  [ "$(val auto_ingest_automerge)" = new_record ] && ok "auto_ingest_automerge = new_record" || bad "auto_ingest_automerge = '$(val auto_ingest_automerge)' (must be new_record: Library -> Formats)"
  case " $cols " in *" duplicate_auto_resolve_enabled "*) [ "$(val duplicate_auto_resolve_enabled)" = 0 ] && guard "duplicate_auto_resolve_enabled=0" || bad "duplicate_auto_resolve_enabled=1 can merge two users' copies (Users -> Repair)";; esac
  # Per-user copies of one title are intentional here; CWA's duplicate notice asks the admin to
  # 'resolve' them, which means deleting another reader's book (J31).
  case " $cols " in *" duplicate_notifications_enabled "*) [ "$(val duplicate_notifications_enabled)" = 0 ] && guard "duplicate_notifications_enabled=0 (per-user copies are intentional)" || bad "duplicate_notifications_enabled=1 invites deleting another user's copy (Users -> Repair)";; esac
  case " $cols " in *" auto_metadata_update_tags "*) [ "$(val auto_metadata_update_tags)" = 0 ] && guard "auto_metadata_update_tags=0 (CWA's metadata fetch is also OFF, so this guards a path that does not run today)" || bad "auto_metadata_update_tags=1 can replace owner:<user> tags (Users -> Repair)";; esac
  echo "  info: auto_convert_ignored_formats='$(val auto_convert_ignored_formats)' koreader_sync_enabled='$(val koreader_sync_enabled)'"
fi

echo "== Public reachability (via Cloudflare)"
if [ -n "$D" ]; then
  # home.: the v6.0 start page, unless that name belongs to another service (HOME_URL is then set)
  for h in $([ -z "$(envget HOME_URL)" ] && echo home) books audio request shelf; do
    c=$(code "https://$h.$D/")
    case "$c" in 200|302|301|401) ok "https://$h.$D -> $c";; *) bad "https://$h.$D -> $c";; esac
  done
  # Caddy fetches Cloudflare's published ranges at start (trusted_proxies cloudflare). If that
  # fetch fails it starts ANYWAY with an empty trust list and silently falls back to the socket
  # peer — which behind Cloudflare is an EDGE address. Then the whole family shares one
  # rate-limit bucket and every fail2ban jail would ban Cloudflare itself. Nothing warned.
  # The four requests just made came through Cloudflare. scripts/caddy-clientip.sh reads the
  # whole log itself: the old `tail -500 | python3` stopped at the first good line, tail died of
  # SIGPIPE on a real-sized log, and pipefail turned a PASS into this FAIL on the live server
  # (2026-09-28; the same trap as the sshd -T | grep -q false alarm).
  cichk="$STACK_DIR/scripts/caddy-clientip.sh"
  CLIENTIP_SINCE=0 STACK_DIR="$STACK_DIR" "$cichk" >/dev/null 2>&1; circ=$?
  case "$circ" in
    0) ok "Caddy resolves the real client IP behind Cloudflare (client_ip != remote_ip for Cloudflare-delivered requests)";;
    1) bad "Caddy is logging the Cloudflare EDGE address as the client: its Cloudflare IP list never loaded (no egress at start?). Rate limits are shared by everyone and a fail2ban ban would hit Cloudflare. scripts/heal.sh restarts Caddy within the hour; or now: docker compose restart caddy";;
    *) warn "could not tell whether Caddy resolves client IPs behind Cloudflare (no Cloudflare-delivered request in the access log yet, or no /etc/bookstack/cf-ips.txt)";;
  esac
  # CWA ships convert-library / epub-fixer / cwa-logs / cwa-internal / reconnect with no auth at
  # all. The 403 is in the production Caddyfile only — assert it on the real edge, anonymously.
  for u in /cwa-convert-library-overview /cwa-internal/reconnect-db '/cwa-convert-library-start;x'; do
    c=$(code "https://books.$D$u")
    [ "$c" = 403 ] && ok "books.$D$u -> 403 (CWA admin job blocked at the edge)" || bad "books.$D$u -> $c (must be 403: Calibre-Web serves it unauthenticated)"
  done
  # /api/v3/* and /api/UserStorage/* are worse than unauthenticated: CWA relays them verbatim to
  # https://readingservices.kobo.com whenever annotation sync is off, Kobo sync is off, or the
  # caller is anonymous (cps/readingservices.py requires_reading_services_auth_and_config), with
  # the caller's method, headers and body — an open relay wearing this origin's IP. Anything but
  # 403 here means the Caddyfile on the box predates that block.
  for u in '/api/v3/x;y' /api/v3/content/x/progress /api/userstorage/Metadata; do
    c=$(code "https://books.$D$u")
    [ "$c" = 403 ] && ok "books.$D$u -> 403 (Kobo reading-services relay blocked at the edge)" || bad "books.$D$u -> $c (must be 403: Calibre-Web relays it to readingservices.kobo.com for anyone)"
  done
  # CWA v4.0.7+ tells the Kobo to call four reading-services paths HERE and answers them itself
  # with constant empty JSON; a Kobo whose calls fail aborts its whole sync. Exactly that JSON
  # proves both that they pass and that CWA's stub (not the relay) answered them.
  cwav=$(envget IMG_CWA); cwav="${cwav##*:}"; cwav="${cwav#v}"
  if [[ "$cwav" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]] && { [ "${BASH_REMATCH[1]}" -lt 4 ] || { [ "${BASH_REMATCH[1]}" = 4 ] && [ "${BASH_REMATCH[2]}" = 0 ] && [ "${BASH_REMATCH[3]}" -lt 7 ]; }; }; then
    for u in /api/v3/content/checkforchanges /api/UserStorage/Metadata; do
      c=$(code "https://books.$D$u")
      [ "$c" = 403 ] && ok "books.$D$u -> 403 (CWA $cwav relays it: blocked)" || bad "books.$D$u -> $c (must be 403 on CWA $cwav: it relays to readingservices.kobo.com)"
    done
  else
    for pair in "/api/v3/content/checkforchanges|[]" "/api/UserStorage/Metadata|{}"; do
      u="${pair%%|*}"; want="${pair#*|}"
      b=$(ecurl -s -m 12 "https://books.$D$u" | tr -d ' \r\n')
      [ "$b" = "$want" ] && ok "books.$D$u answers CWA's own empty reply (the Kobo's sync needs it)" \
        || bad "books.$D$u answered '${b:0:80}', not '$want': a Kobo's sync fails on it (re-run Install -> Deploy to render the Caddyfile)"
    done
  fi
  c=$(code -k "https://$(envget PUBLIC_IP)/" -H "Host: books.$D")
  [ "$c" = 000 ] && ok "origin refuses direct (non-Cloudflare) connections" || bad "origin answered a direct connection ($c) — mTLS/firewall not enforcing"
  cc=$(ecurl -sI -m 12 "https://books.$D/login" | grep -i '^cf-cache-status:' | awk '{print toupper($2)}' | tr -d '\r')
  case "$cc" in HIT|"") [ -n "$cc" ] && bad "Cloudflare served books./login from its cache ($cc): create the no-cache Cache Rule" || warn "no cf-cache-status header (not behind Cloudflare?)";; *) ok "edge cache: $cc";; esac
  # device paths must reach the apps, not a Cloudflare challenge page
  hdr=$(mktemp); body=$(mktemp)
  c=$(ecurl -s -m 12 -D "$hdr" -o "$body" -w '%{http_code}' "https://books.$D/opds/")
  if grep -qi '^cf-mitigated' "$hdr"; then bad "Cloudflare challenges /opds (Browser Integrity Check / Bot Fight Mode must be OFF)"
  elif [ "$c" = 401 ] && grep -qi '^www-authenticate' "$hdr"; then ok "/opds answers a Basic-auth challenge (CWA reached)"
  else bad "/opds -> $c without WWW-Authenticate"; fi
  tok=$(docker exec librarian python -m cwa kobo-url "$ADMIN_USER" 2>/dev/null | tr -d '"' | sed 's#.*/kobo/##; s#/.*##')
  case "$tok" in None|null|"") tok="";; esac
  if [ -n "$tok" ]; then
    c=$(ecurl -s -m 12 -A 'Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/533.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/533.1 Kobo' -D "$hdr" -o "$body" -w '%{http_code}' "https://books.$D/kobo/$tok/v1/initialization" 2>/dev/null)
    if grep -qi '^cf-mitigated' "$hdr" || [ "$c" = 403 ] || [ "$c" = 503 ]; then bad "Kobo init challenged by Cloudflare ($c): Browser Integrity Check / Bot Fight Mode must be OFF"
    elif [ "$c" = 200 ] && grep -q Resources "$body"; then ok "Kobo /v1/initialization -> 200 with Resources"
    else bad "Kobo /v1/initialization -> $c"; fi
  else warn "no admin Kobo token yet (Devices page or Users -> Kobo link); Kobo probe skipped"; fi
  # This probe goes through the PUBLIC edge, so it proves Caddy's header strip, not Calibre-Web's
  # configuration: (hardening) in caddy/Caddyfile.template does `request_header -Remote-User`
  # (plus -Remote-Groups/-Remote-Email/-Remote-Name), and `request_header` is ordered with
  # `header`, ahead of route/reverse_proxy, so the header is gone before CWA is reached. Calling
  # any non-200 "Calibre-Web ignored it" was green by construction: it would have printed OK
  # whether or not CWA's own header login was on. The CWA setting is asserted separately below.
  c=$(code -H 'Remote-User: admin' "https://books.$D/me"); [ "$c" = 200 ] && bad "a client-supplied Remote-User header reached Calibre-Web and logged in: the (hardening) request_header strip is not in the running Caddyfile (re-run Configure)" || ok "Caddy strips client-supplied Remote-* headers ($c at the edge)"
  if grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" 2>/dev/null; then
    c=$(code "https://audio.$D/ping"); [ "$c" = 200 ] && ok "gate lets the Audiobookshelf app through (/ping 200)" || bad "audio./ping -> $c (Authelia gate blocks the ABS apps)"
    c=$(code -u x:y "https://books.$D/kosync/users/auth")
    case "$c" in
      401) ok "gate lets KOReader sync through (/kosync 401)";;
      503) warn "KOReader sync is off in Calibre-Web (it answers 503), so the gate's /kosync bypass is untested; turn it on under Library -> Formats if anyone uses KOReader";;
      *) bad "books./kosync -> $c (Authelia gate blocks KOReader)";;
    esac
  fi
  # /kosync/users/auth is the ONE kosync endpoint that answers a wrong password with 401. The
  # progress endpoints answer 400 (cps/progress_syncing/protocols/kosync.py:532 raises
  # KOSyncError(ERROR_UNAUTHORIZED_USER); handle_sync_error returns it as 400), which the
  # fail2ban filter used to ignore completely — a clean, unbanned password oracle. Probe the
  # endpoint that actually had the problem: anything but 400 (wrong password) or 503 (KOReader
  # sync disabled) means it answered a stranger. 200 would mean no authentication at all.
  c=$(code -u nosuchuser:nosuchpassword "https://books.$D/kosync/syncs/progress/0123456789abcdef")
  case "$c" in
    400|401|503) ok "books.$D/kosync/syncs/progress rejects bad credentials ($c)";;
    200) bad "books.$D/kosync/syncs/progress answered 200 to a wrong password: KOReader progress sync is UNAUTHENTICATED";;
    *) warn "books.$D/kosync/syncs/progress -> $c (expected 400 wrong-password, or 503 when KOReader sync is off)";;
  esac
  rm -f "$hdr" "$body"
fi

echo "== Storage & backups"
df -h "$STACK_DIR" | awk 'NR==2{print "  disk: "$4" free ("$5" used)"}'
availk=$(df --output=avail "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9); pct=$(df --output=pcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
# Inodes as well as blocks, the same way scripts/disk-watch.sh does it: a filesystem at 60 %
# blocks and 100 % inodes fails exactly like a full disk (SQLite ENOSPC, ingest failures, Caddy
# unable to log) and every block check here stays green. Anything that is not a 0..100 number
# is a df that did not answer (no fixed inode table prints "-"), and is reported as nothing at
# all rather than as 0 % — see the pctread comment in disk-watch.sh.
ipct=$(df -i --output=ipcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
case "$ipct" in ''|*[!0-9]*) ipct="";; *) [ "$ipct" -le 100 ] || ipct="";; esac
iavail=""; [ -n "$ipct" ] && iavail=$(df -i --output=iavail "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)
# max(blocks, inodes) so the one threshold below keeps its meaning; the message names which.
worst="${pct:-0}"; [ -n "$ipct" ] && [ "$ipct" -gt "$worst" ] && worst="$ipct"
if [ -n "$availk" ]; then [ "$availk" -lt 5242880 ] || [ "${pct:-0}" -ge 90 ] && bad "low disk BLOCKS: $((availk/1048576)) GB free, ${pct}% used (< 10 % or < 5 GB)" || ok "disk blocks: $((availk/1048576)) GB free, ${pct}% used"; fi
fstype=$(df --output=fstype "$STACK_DIR" 2>/dev/null | tail -1 | tr -d ' ')
if [ -z "$ipct" ]; then
  case "$fstype" in
    btrfs|zfs) ok "disk inodes: $fstype creates them as it needs them (no fixed table that could run out)";;
    *) warn "inode use not reported for $STACK_DIR (${fstype:-unknown} filesystem): the disk watchdog cannot see an inode shortage either";;
  esac
elif [ "$ipct" -ge 90 ]; then bad "low disk INODES: ${ipct}% of inodes used${iavail:+, only $iavail left}. This is NOT a byte shortage: freeing gigabytes will not help and df -h will keep showing free space. Delete many small files (cwa/config/processed_books, abs/metadata/cache, thumbnails); ext4 fixes its inode count at mkfs time"
else ok "disk inodes: ${ipct}% used${iavail:+, $iavail free}"; fi
# The watchdog latches on max(blocks, inodes); say what it is seeing so the two agree.
[ "$worst" -ge 95 ] && warn "the disk watchdog stops the downloaders at 95 % and the worst of blocks/inodes is now ${worst}%"
stuck=$(find "$STACK_DIR/library/ingest" -type f ! -name '*.part' -mmin +30 2>/dev/null | head -3 | tr '\n' ' '); [ -n "$stuck" ] && warn "ingest stuck: files older than 30 min in library/ingest ($stuck) — check calibre-web logs"
pb=$(du -sm "$STACK_DIR/cwa/config/processed_books" 2>/dev/null | cut -f1); [ "${pb:-0}" -gt 5120 ] && warn "cwa/config/processed_books is ${pb} MB (Library -> Formats: turn CWA's file copies off)"
if systemctl is-enabled bookstack-backup.timer >/dev/null 2>&1; then
  ok "backup timer enabled ($(systemctl show bookstack-backup.timer -p NextElapseUSecRealtime --value))"
  systemctl is-enabled bookstack-restore-test.timer >/dev/null 2>&1 && ok "monthly restore-test timer enabled" || warn "restore-test timer missing (re-run Install -> Backups)"
else warn "backups not scheduled (Install → Backups)"; fi
# The post-reboot self-test is the only check that runs while nobody is watching, so an admin
# who never opens the TUI needs to learn here that it is missing or that it last failed.
if systemctl is-enabled bookstack-postboot.service >/dev/null 2>&1; then
  if systemctl is-failed bookstack-postboot.service >/dev/null 2>&1; then
    bad "the last post-reboot self-test FAILED (journalctl -u bookstack-postboot -n 100)"
  else ok "post-reboot self-test installed"; fi
else warn "post-reboot self-test not installed: the 04:30 unattended reboot is unverified (re-run Install → Deploy or Operations → Update)"; fi
# An enabled timer proves nothing: scripts/backup.sh passing a flag this restic does not have
# (--retry-lock on Debian 12's 0.14) failed EVERY scheduled run while the install, the timer and
# this test all looked green. What matters is whether a backup actually succeeded, and recently.
# These checks run whether or not the timer is enabled, so a repository with no timer is caught.
if [ "$SCHEDULED" = 1 ]; then :     # the hourly run: the backup reports itself to Kuma
elif [ -f /etc/bookstack/restic.env ] && command -v restic >/dev/null; then
  # --no-lock: a read-only listing needs no lock, and taking one could make the backup's own
  # `forget --prune` (exclusive lock; Debian 12's restic 0.14 has no --retry-lock) fail if the
  # two ever overlapped
  age=$( (set -a; . /etc/bookstack/restic.env; set +a; restic --no-lock snapshots --latest 1 --json 2>/dev/null) | python3 -c '
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
  # The weekly `restic check --read-data-subset=n/52` walks the repository one 52nd at a time so
  # every byte is re-read once a year. The counter lives beside restic.env; if it never moves,
  # the same 1/52 is verified for ever and the rest is never read (scripts/backup.sh warns to
  # the journal when it cannot write it, which nobody reads).
  bstate=/etc/bookstack/backup.state
  grp=$({ grep -E '^check_group=' "$bstate" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [ -n "$grp" ]; then ok "backup verification rotation at group $grp of 52 (every byte re-read once a year)"
  elif [ -f "$bstate" ]; then bad "$bstate exists but holds no check_group: the weekly verification restarts at 1/52 every week and 51/52 of the repository is never read"
  else warn "no $bstate yet: the rotating weekly verification writes it after the first Sunday backup (BACKUP_CHECK_DOW)"; fi
  # L15: who can delete the backups. With an append-only nightly key, retention runs elsewhere
  # (the monthly prune timer or the admin's computer); snapshots older than the policy piling up
  # is how a forgotten laptop prune shows.
  if grep -qE '^RESTIC_APPEND_ONLY=1' /etc/bookstack/restic.env 2>/dev/null; then
    km=$(envget RESTIC_KEEP_MONTHLY); case "$km" in ''|*[!0-9]*) km=6;; esac
    oldest=$( (set -a; . /etc/bookstack/restic.env; set +a; restic --no-lock snapshots --json 2>/dev/null) | python3 -c '
import sys, json, re, datetime
now = datetime.datetime.now(datetime.timezone.utc)
ts = [datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", x["time"]).replace("Z", "+00:00"))
      for x in (json.load(sys.stdin) or []) if "pre-update" not in (x.get("tags") or [])]
print(max((now - t).days for t in ts) if ts else "")' 2>/dev/null)
    where="your own computer"; [ -f /etc/bookstack/restic-prune.env ] && where="the monthly bookstack-prune timer"
    grep -qE '^RESTIC_PRUNE_WHERE=home' /etc/bookstack/restic.env 2>/dev/null && where="the home backup computer (Install -> Backups showed the monthly command)"
    if systemctl is-failed bookstack-prune.service >/dev/null 2>&1; then bad "the last monthly prune FAILED (journalctl -u bookstack-prune -n 50): old snapshots are piling up"
    elif [ -n "$oldest" ] && [ "$oldest" -gt $(( (km + 2) * 31 )) ]; then warn "the oldest snapshot is $oldest days old, past the $km-month policy: retention has not run lately (it runs from $where)"
    else ok "backup key is append-only: nothing on this server can delete a snapshot$([ -f /etc/bookstack/restic-prune.env ] && echo ' except the separate prune key in /etc/bookstack/restic-prune.env')"; fi
  else
    warn "the nightly backup key can DELETE snapshots: whoever takes this server can wipe the backups too (Install -> Backups: use an append-only key)"
  fi
elif [ -f /etc/bookstack/restic.env ] && ! command -v restic >/dev/null; then bad "restic.env exists but restic is not installed: no backup can run (apt-get install restic)"; fi
# The staging area is INSIDE the backup root ($SNAP = $STACK_DIR/.backup-snap) with no matching
# --exclude, so anything staged there ends up in every snapshot. scripts/backup.sh used to copy
# /etc/bookstack/restic.env verbatim, which put RESTIC_PASSWORD — and, for an s3:/B2 repository,
# AWS_SECRET_ACCESS_KEY — inside the repository those keys decrypt and can delete. It now writes
# a redacted stub instead. Assert that, so the staging cannot quietly regain the secrets: this
# is the one leak that survives rotating RESTIC_PASSWORD, because the bucket key is unchanged
# and the old snapshots stay readable with the old password.
if [ -d "$STACK_DIR/.backup-snap/host" ]; then
  leak=$(grep -rlE '^(RESTIC_PASSWORD|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID)=..' "$STACK_DIR/.backup-snap/host" 2>/dev/null | head -3 | tr '\n' ' ')
  [ -z "$leak" ] && ok "no repository password or object-store key staged into the backup" \
    || bad "the backup staging area carries repository secrets: $leak — every snapshot then contains the key that decrypts it and the credentials that can delete it. Update scripts/scripts (copy_code_trees) and rotate: restic key add + a NEW B2/S3 application key"
fi
[ -f /etc/cron.d/bookstack-disk ] && ok "disk watchdog installed" || warn "disk watchdog not installed (re-run Deploy)"
if [ "$(envget DISK_REPORT)" != false ]; then
  [ -f /etc/cron.d/bookstack-diskreport ] && ok "daily disk summary scheduled" || warn "daily disk summary not scheduled (re-run Deploy)"
fi
[ -e "$STACK_DIR/library/staging/.disk-paused" ] && warn "the disk watchdog has PAUSED the downloaders AND the portal's own imports (library/staging/.disk-paused); it clears itself below DISK_RESUME_PCT"
if [ -f /etc/cron.d/bookstack-disk ] || [ -f /etc/cron.d/bookstack-cfips ]; then
  systemctl is-active cron >/dev/null 2>&1 && ok "cron daemon running (disk watchdog, Cloudflare IP refresh)" || bad "cron is not running: the disk watchdog and the Cloudflare IP refresh never run (apt-get install cron; systemctl enable --now cron)"
fi

# The hourly self-test is a schedule, and a schedule proves nothing until it has run: read its
# last result instead of asking systemd whether the timer is enabled.
if [ -f /etc/systemd/system/bookstack-selftest.timer ]; then
  hl="$STACK_DIR/.selftest-hourly.log"
  hts=$(grep -E '^finished=' "$hl" 2>/dev/null | tail -1 | sed 's/^finished=//; s/ exit=.*//')
  hage=""; [ -n "$hts" ] && hage=$(( ( $(date +%s) - $(date -d "$hts" +%s 2>/dev/null || echo 0) ) / 60 ))
  if [ -z "$hts" ]; then warn "the hourly self-test has not finished a run yet (systemctl list-timers bookstack-selftest.timer)"
  elif [ "${hage:-9999}" -le 130 ]; then ok "hourly self-test last finished ${hage} min ago ($(grep -E '^finished=' "$hl" | tail -1 | sed 's/.* exit=/exit=/'))"
  else bad "the hourly self-test last finished ${hage} min ago: its timer is not firing (systemctl status bookstack-selftest.timer)"; fi
else warn "hourly self-test not installed (re-run Install -> Deploy)"; fi

# L02: qBittorrent's Web UI answers the password bookstack set (not a temporary one)
if [ "$TORRENTS" = true ] && [ -n "$(envget QBIT_PASS)" ]; then
  qcode=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -H 'Referer: http://127.0.0.1:8080' \
          --data-urlencode "username=admin" --data-urlencode "password=$(envget QBIT_PASS)" http://127.0.0.1:8080/api/v2/auth/login 2>/dev/null || echo 000)
  case "$qcode" in 200|204) ok "qBittorrent Web UI accepts the stored admin password (QBIT_PASS)";;
    000) warn "could not reach qBittorrent's Web UI to check its login";;
    *) bad "qBittorrent refused QBIT_PASS (HTTP $qcode): its password was changed in its own UI, or the seed did not apply (Library -> Torrents: disable + enable re-seeds it)";; esac
fi
# L17: a leaked family password is the likeliest way in; say so while there is no second factor
if [ "$(envget AUTHELIA_ENABLED)" != true ]; then
  warn "no second factor in front of books./audio./request./shelf. (Security -> Authelia: SSO + 2FA; devices keep working)"
fi

echo "== Monitoring (Uptime Kuma)"
if curl -fs -m 5 -o /dev/null http://127.0.0.1:3001/; then
  ku=$(envget KUMA_USER); kp=$(envget KUMA_PASS)
  if [ -z "$ku" ] || [ -z "$kp" ]; then
    bad "Uptime Kuma runs but bookstack never configured it: no monitors, no alerts (Operations -> Monitoring)"
  else
    # /metrics with the admin login: Kuma's own view of every monitor (0 down, 1 up, 2 pending,
    # 3 maintenance). Evidence that the monitors EXIST and are running, not that a setup ran once.
    met=$(curl -fs -m 10 -u "$ku:$kp" http://127.0.0.1:3001/metrics 2>/dev/null | grep '^monitor_status{')
    if [ -z "$met" ]; then
      bad "Uptime Kuma refused bookstack's login or has no monitors (Operations -> Monitoring repairs both)"
    else
      nmon=$(printf '%s\n' "$met" | grep -c .)
      down=$(printf '%s\n' "$met" | awk '$NF=="0"' | sed -E 's/.*monitor_name="([^"]*)".*/\1/' | head -5 | paste -sd, - | sed 's/,/, /g')
      if [ -z "$down" ]; then ok "Uptime Kuma: $nmon monitor(s) reporting, none down"
      else warn "Uptime Kuma sees DOWN: $down (https://monitor.$D)"; fi
    fi
  fi
else bad "Uptime Kuma is not answering on 127.0.0.1:3001: nothing is watching the stack between self-tests"; fi

echo "== Alerts"
if [ -n "$(envget NOTIFY_WEBHOOK)" ]; then ok "alert webhook configured"
elif [ -n "$(envget SMTP_HOST)" ]; then ok "alerts go by e-mail (SMTP configured, no webhook)"
else bad "no alert channel: failed backups and a full disk reach nobody (Install -> Alerts)"; fi

# Optional dead-man's switch. BACKUP_PING_URL (scripts/backup.sh) fires once a night from the
# backup, so between backups nothing notices the VPS itself going away — the one failure no
# on-box check can report, by construction. HEALTH_PING_URL is the sibling: pinged from here,
# so when this script runs unattended (the post-reboot unit, or a timer) a free
# healthchecks.io-style check goes red if the box stops answering at all. Same idiom and same
# <url> / <url>/fail convention as backup.sh. Never fatal: a ping that cannot be sent must not
# turn a healthy self-test into a failed one.
hp=$(envget HEALTH_PING_URL)
if [ -n "$hp" ]; then
  if [ "$fail" = 0 ]; then curl -fsS -m 10 --retry 3 "$hp" >/dev/null 2>&1 || true
  else curl -fsS -m 10 --retry 3 "${hp%/}/fail" >/dev/null 2>&1 || true; fi
else
  # v5.9.1: said every hour until set, because nothing on this box can report the box being gone
  warn "no external check (HEALTH_PING_URL): if the whole server goes down, nothing tells you. A free healthchecks.io check (period 1 h, grace 1 h), pasted in Operations -> Monitoring, fixes that"
fi

echo; echo "RESULT: $pass passed, $fail failed, $guards guard(s) set"
# Guards are reported apart from passes on purpose: "$guards guard(s) set" says a protective
# setting has the expected value, NOT that the protection was exercised. Folding them into the
# pass count is what made a switched-off feature look verified.
exit "$fail"
