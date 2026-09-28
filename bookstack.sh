#!/usr/bin/env bash
# bookstack.sh — menu-driven installer and manager for the book library VPS.
# Run as root on a fresh Debian: sudo bash bookstack.sh
# Every setting lives in $STACK_DIR/.env (written here) and every task is a menu entry.
set -euo pipefail

STACK_DIR="${STACK_DIR:-/srv/bookstack}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ENV_FILE="$STACK_DIR/.env"
ETC="${BOOKSTACK_ETC:-/etc}"          # host config root (tests point it at a temp dir)
STACK_USER=books
CF_API=https://api.cloudflare.com/client/v4
BOOKSTACK_VERSION=4
# Image pins (looked up 2026-09-22). Seeded into .env by Configure; changed by Operations -> Update.
IMG_DEFAULTS="IMG_CWA=crocodilestick/calibre-web-automated:v4.0.6 IMG_ABS=ghcr.io/advplyr/audiobookshelf:2.36.1
IMG_SHELFMARK=ghcr.io/calibrain/shelfmark:v1.3.15 IMG_QBIT=lscr.io/linuxserver/qbittorrent:5.2.3
IMG_KUMA=louislam/uptime-kuma:1 IMG_AUTHELIA=authelia/authelia:4.39.28 IMG_FLARESOLVERR=ghcr.io/flaresolverr/flaresolverr:v3.5.2
IMG_SYNCTHING=syncthing/syncthing:2.1.5"
CADDY_BASE=caddy:2.11.4               # used for `caddy hash-password`; same base as caddy/Dockerfile

# BOOKSTACK_LIB=1 sources this file for tests without running anything.
if [ "${BOOKSTACK_LIB:-0}" != 1 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash $0"; exit 1; }
  # envset is read-modify-write, so two TUI sessions (two SSH logins, or one left open in tmux)
  # silently drop each other's .env keys — and could run Deploy/Update against the same compose
  # project at once. One instance per server; the lock is released when this process exits.
  BOOKSTACK_LOCKFILE="${BOOKSTACK_LOCK:-/run/bookstack.lock}"
  if command -v flock >/dev/null 2>&1 && : > "$BOOKSTACK_LOCKFILE" 2>/dev/null; then
    exec 9>"$BOOKSTACK_LOCKFILE"
    flock -n 9 || { echo "Another bookstack.sh is already running on this server. Close it (or wait for it to finish) and run this again."; exit 1; }
  fi
  # the only apt call the owner meets before any menu is drawn: a freshly booted VPS still has
  # cloud-init / unattended-upgrades holding the dpkg lock, and errexit would abort with a raw
  # apt error and no explanation.
  command -v whiptail >/dev/null || { apt-get update -qq || true; apt-get install -y -qq whiptail \
    || { echo "Could not install whiptail, which draws these menus. apt may still be busy on a freshly booted server (cloud-init and the first unattended-upgrades run hold the dpkg lock for a minute or two), or the network is not up yet. Wait a moment and run this again."; exit 1; }; }
fi

# ---------- helpers ----------
# whiptail draws its dialog on stdout. msg/big are called from inside $(...) captures (askpw2),
# so the drawing goes to stderr: the terminal still shows it, the capture never contains it.
msg()    { whiptail --title "Bookstack" --msgbox "$1" 18 78 1>&2; }
big()    { whiptail --title "$1" --scrolltext --msgbox "$2" 30 86 1>&2; }
ask()    { whiptail --title "Bookstack" --inputbox "$1" 10 76 "${2:-}" 3>&1 1>&2 2>&3; }
askpw()  { whiptail --title "Bookstack" --passwordbox "$1" 10 76 3>&1 1>&2 2>&3; }
yesno()  { whiptail --title "Bookstack" --yesno "$1" 14 76; }
askpw2() { # password typed twice, min 8 chars; refuses anything carrying terminal escape bytes
  local a b
  while true; do
    a=$(askpw "$1") || return 1
    [ "${#a}" -ge 8 ] || { msg "At least 8 characters, please."; continue; }
    b=$(askpw "Repeat it:") || return 1
    if [ "$a" = "$b" ]; then
      [[ "$a" == *$'\e'* ]] && { msg "Internal error: terminal bytes ended up in the password. Not saved."; return 1; }
      printf '%s' "$a"; return 0
    fi
    msg "They did not match. Try again."
  done
}
# .env is also read by docker compose, which interpolates $VAR inside bare and double-quoted
# values (a password like p$ss would silently become p). Values are therefore stored
# single-quoted; compose takes those literally, except \' which it reads as a quote — so a
# quote is the only character escaped. envget reads the same form back (and bare values
# written by older versions).
envget() {
  local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  # a .env edited or restored from a Windows editor is CRLF: without this every value carries a
  # trailing \r into the Caddyfile, the Cloudflare URLs and the ADMIN_HASH shape check
  raw="${raw%$'\r'}"
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then
    raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'
    raw="${raw//"$bs$q"/$q}"
  fi
  printf '%s' "$raw"
}
# Returns non-zero when the value could not be stored. Every caller must check: a silent no-op
# (full disk, root remounted read-only) used to leave .env and the running stack disagreeing
# while the UI reported success — and trapped set_admin_password in an endless prompt.
envset() {
  # one key = one line: a value carrying a newline writes a second, key-shaped line into .env
  case "$2" in *$'\n'*) msg "A setting cannot contain a line break. Nothing was saved."; return 1;; esac
  mkdir -p "$STACK_DIR" || return 1; touch "$ENV_FILE" || return 1; chmod 600 "$ENV_FILE" || return 1
  local v="$2" bs=\\ q=\'
  v="${v//"$q"/$bs$q}"
  # umask 077 in a subshell: the temp file holds every secret and must be 0600 from its first byte
  ( umask 077; rm -f "$ENV_FILE.tmp"; { grep -vE "^$1=" "$ENV_FILE" || true; printf "%s='%s'\n" "$1" "$v"; } > "$ENV_FILE.tmp" ) || return 1
  mv "$ENV_FILE.tmp" "$ENV_FILE" || return 1
  chmod 600 "$ENV_FILE" || return 1
  # never readable by the containers' uid 1000; a failure here is not worth losing the value over
  [ "$(id -u)" -eq 0 ] && { chown root:root "$ENV_FILE" || true; }
  return 0
}
envdefault(){ [ -n "$(envget "$1")" ] || envset "$1" "$2"; }
img(){ # IMG_X -> value from .env, else the pinned default
  local v kv; v=$(envget "$1"); [ -n "$v" ] && { printf '%s' "$v"; return 0; }
  for kv in $IMG_DEFAULTS; do [ "${kv%%=*}" = "$1" ] && printf '%s' "${kv#*=}"; done; }
need()   { for v in "$@"; do [ -n "$(envget "$v")" ] || { msg "Missing $v. Run 'Configure' first."; return 1; }; done; }
# -m 30: without a deadline an API that accepts the connection and then stops answering freezes
# the whole TUI on a blank screen half-way through a zone's ~20 calls, with no way to tell what
# was written. --retry covers a refused connection / 5xx blip.
cf()     { curl -fsS -m 30 --retry 2 --retry-connrefused -X "$1" "$CF_API$2" -H "Authorization: Bearer $(envget CF_API_TOKEN)" -H "Content-Type: application/json" "${@:3}"; }
# qBittorrent is opt-in (compose profile "torrents", Library -> Torrents): every compose call
# that starts or stops the stack carries the profile while it is enabled.
torrents_on(){ [ "$(envget TORRENTS_ENABLED)" = true ]; }
# FlareSolverr (compose profile "solver") is shared: Shelfmark uses it when FLARESOLVERR_ENABLED,
# Ephemera always needs it. It runs while either one wants it.
solver_on(){ [ "$(envget FLARESOLVERR_ENABLED)" = true ] || [ "$(envget EPHEMERA_ENABLED)" = true ]; }
# The seedbox's Syncthing peer (compose profile "seedbox", Library -> Seedbox).
seedbox_on(){ [ "$(envget SEEDBOX_ENABLED)" = true ]; }
compose_profiles(){ if torrents_on; then printf '%s\n' --profile torrents; fi
  if solver_on; then printf '%s\n' --profile solver; fi
  if seedbox_on; then printf '%s\n' --profile seedbox; fi; }
compose(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose ${p[@]+"${p[@]}"} "$@"); }
composeA(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose -f docker-compose.yml -f docker-compose.authelia.yml ${p[@]+"${p[@]}"} "$@"); }
composeE(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose -f docker-compose.yml -f docker-compose.ephemera.yml ${p[@]+"${p[@]}"} "$@"); }
lib()    { docker exec -i librarian python -m cwa "$@"; }          # user/device management lives in the portal image
absctl() { docker exec -i librarian python -m abs "$@"; }          # Audiobookshelf automation (needs ABS_TOKEN after setup)
# The three things that live only inside the portal's own database / dropbox tree and had no
# console path at all: login lockouts, the request queue past the newest 200 rows, and files
# parked in dropbox/<user>/.failed. Every arm prints ONE line of JSON and exits non-zero with
# {"ok":false,"error":"..."} on failure (librarian/admin_cli.py).
admin_cli(){ docker exec -i librarian python -m admin_cli "$@"; }
# the error sentence out of an admin_cli answer; the raw output when it is not JSON at all
cli_err(){ local e; e=$(printf '%s' "$1" | json 'd.get("error") or ""') || e=""
  printf '%s' "${e:-$(printf '%s' "${1:-}" | tail -3)}"; }
abs_ready(){ [ -n "$(envget ABS_TOKEN)" ]; }
cwa_sql(){ docker exec -i calibre-web sqlite3 /config/cwa.db "$1"; } # CWA's own settings DB
running(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }
portal_up(){ running librarian; }
# `compose up -d librarian` RECREATES the container, which is what makes a changed .env value
# reach the portal; `restart` would not. The exit status is returned, never swallowed: a
# failure means the running gunicorn still holds the old setting (or the old session secret).
restart_portal(){ compose up -d librarian >/dev/null 2>&1; }
restart_portal_ok(){ # restart + say so when it did not work, so no step claims a setting is live
  restart_portal && return 0
  msg "The portal could NOT be restarted, so the setting you just changed is NOT active yet (Operations -> Logs -> librarian)."
  return 1
}
admin_user(){ local u; u=$(envget ADMIN_USER); printf '%s' "${u:-admin}"; }   # the chosen CWA admin name (C10)
# A truncated or partially written hash starts with '$2' too, and basic_auth then compares every
# password against garbage: the admin is locked out of the tailnet-only tools with a plain
# "wrong password". Match the real shape instead of the prefix.
valid_admin_hash(){ case "$1" in '$2'?'$'??'$'?*) return 0;; '$argon2'*'$'*'$'?*) return 0;; *) return 1;; esac; }
render_caddyfile(){
  # An empty hash would render "admin " inside basic_auth: Caddy rejects the config and every
  # site on the box stays down. Refuse here, where the cause is obvious.
  valid_admin_hash "$(envget ADMIN_HASH)" \
    || { msg "ADMIN_HASH is missing or invalid; the admin-gate password must be set first (Install -> Configure). Caddyfile NOT rendered."; return 1; }
  # the admin sites' tailnet_only matcher needs the tailnet IP; empty = a config error for every site
  [ -n "$(envget TAILSCALE_IP)" ] || { msg "TAILSCALE_IP is empty; run Install -> Tailscale (or Configure, which sets a placeholder) first. Caddyfile NOT rendered."; return 1; }
  local f="$STACK_DIR/caddy/Caddyfile" bind; bind=$(envget BIND_IP); bind="${bind:-$(envget PUBLIC_IP)}"
  [ -n "$bind" ] || { msg "Neither BIND_IP nor PUBLIC_IP is set; run Install -> Configure first. Caddyfile NOT rendered."; return 1; }
  # optional vhosts: their DNS name and upstream only exist while the feature is enabled
  local drop=""
  torrents_on || drop="$drop TORRENTS"
  [ "$(envget EPHEMERA_ENABLED)" = true ] || drop="$drop EPHEMERA"
  [ "$(envget AUTHELIA_ENABLED)" = true ] || drop="$drop AUTHELIA"
  # keep the last good file: apply_caddy puts it back when the new one does not validate
  [ -s "$f" ] && cat "$f" > "$f.prev"
  # Substitution is a plain string replace in python, NOT sed: on sed's replacement side '&' means
  # "the whole match" and '|' ends the s||| expression, so a domain or contact address carrying
  # one of them used to write a corrupted (or zero-byte) Caddyfile and still report success.
  local rc=0
  CFR_DOMAIN="$(envget DOMAIN)" CFR_ADMIN_EMAIL="$(envget ADMIN_EMAIL)" CFR_BIND_IP="$bind" \
  CFR_TAILSCALE_IP="$(envget TAILSCALE_IP)" CFR_ADMIN_HASH="$(envget ADMIN_HASH)" \
  python3 - "$STACK_DIR/caddy/Caddyfile.template" "$f.new" "$drop" <<'PYC' || rc=$?
import os, sys
tmpl, out, drop = sys.argv[1], sys.argv[2], sys.argv[3].split()
s = open(tmpl).read()
for name in drop:                       # a feature's whole vhost block, markers included
    b, e = "# @%s_BEGIN@" % name, "# @%s_END@" % name
    while b in s and e in s[s.index(b):]:
        i = s.index(b); j = s.index(e, i) + len(e)
        if j < len(s) and s[j] == "\n":
            j += 1
        s = s[:i] + s[j:]
for k in ("DOMAIN", "ADMIN_EMAIL", "BIND_IP", "TAILSCALE_IP", "ADMIN_HASH"):
    s = s.replace("@@%s@@" % k, os.environ["CFR_" + k])
if "@@" in s:                           # a placeholder this script does not know about
    sys.exit(2)
open(out, "w").write(s)
PYC
  if [ "$rc" != 0 ] || [ ! -s "$f.new" ]; then
    rm -f "$f.new"
    msg "Could not render the Caddyfile from its template$([ "$rc" = 2 ] && printf ' (an @@PLACEHOLDER@@ was left unsubstituted)'). The previous one is untouched and Caddy keeps running unchanged.\n\nCheck Install -> Configure: the domain and admin e-mail must be a plain hostname and address."
    return 1
  fi
  # written in place (never mv): the running container bind-mounts this exact inode
  cat "$f.new" > "$f"; rm -f "$f.new"
  chown root:root "$f"; chmod 644 "$f"      # read-only for the container; not writable by uid 1000
}
render_caddy_all(){ # Caddyfile + the Authelia gate when enabled
  render_caddyfile || return 1
  if [ "$(envget AUTHELIA_ENABLED)" = "true" ]; then
    inject_authelia_gate || { msg "Authelia is enabled but the gate could NOT be injected into the Caddyfile (see the error above). Fix this before starting Caddy, or disable Authelia (Security menu)."; return 1; }
  fi
}
apply_caddy(){ # validate the rendered Caddyfile inside the running Caddy, then reload it; a bad file is rolled back
  running caddy || return 0          # not started yet: Deploy starts it with the new file
  local f="$STACK_DIR/caddy/Caddyfile" out
  if out=$(compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1); then
    reload_caddy && return 0
    msg "Caddy did not reload the new configuration. Operations -> Logs -> caddy."; return 1
  fi
  [ -s "$f.prev" ] && cat "$f.prev" > "$f"
  msg "The new Caddyfile does NOT validate, so the previous one was put back and Caddy keeps running unchanged.\n\n$(printf '%s' "$out" | tail -5)"
  return 1
}
ensure_stack_user(){ # the host account behind uid 1000, which every container runs as (PUID/PGID=1000)
  # Files are owned by the NUMBER 1000, so the account's name does not matter. Many Debian
  # cloud images already ship a default user with uid 1000 ("debian", "admin", the provider's
  # name); `useradd -u 1000 books` then fails with "UID 1000 is not unique" and aborted the
  # system step half-way. Reuse whichever account owns uid 1000; create "books" only if none does.
  local by_id by_name
  by_id=$(getent passwd 1000 | cut -d: -f1)
  by_name=$(getent passwd "$STACK_USER" | cut -d: -f3)
  if [ -n "$by_id" ]; then
    [ "$by_id" != "$STACK_USER" ] && echo "uid 1000 already belongs to '$by_id'; using that account for the stack."
    STACK_USER="$by_id"
  elif [ -n "$by_name" ]; then
    # a 'books' account exists with another uid: leave it alone, give uid 1000 its own account
    STACK_USER="books1000"
    getent passwd "$STACK_USER" >/dev/null || useradd -m -u 1000 -s /usr/sbin/nologin "$STACK_USER" \
      || { msg "Could not create a user with uid 1000 ('$STACK_USER'). Create one by hand and run System again."; return 1; }
  else
    # L01: nothing logs in as this account (root runs compose, cron and the TUI): no shell.
    # An account that already owns uid 1000 (the image's default user) keeps its shell — it may
    # be how the admin logs in.
    useradd -m -u 1000 -s /usr/sbin/nologin "$STACK_USER" \
      || { msg "Could not create the '$STACK_USER' user (uid 1000). Run System again after checking 'getent passwd 1000'."; return 1; }
  fi
  STACK_HOME=$(getent passwd "$STACK_USER" | cut -d: -f6)
  # uid 1000 is what every container runs as: membership in the docker group would turn any
  # container escape into root. Root runs compose; the account needs no docker access.
  gpasswd -d "$STACK_USER" docker >/dev/null 2>&1 || true
}
# Ownership model (C14): $STACK_DIR, code, compose files, scripts and rendered configs belong to
# root (root executes them); only the data directories the containers write are uid 1000.
DATA_DIRS="caddy/data caddy/config cwa abs qbt downloads library kuma librarian/state shelfmark ephemera authelia syncthing"
own_data_dirs(){
  local d; chown root:root "$STACK_DIR"; chmod 755 "$STACK_DIR"
  for d in $DATA_DIRS; do [ -e "$STACK_DIR/$d" ] && chown -R 1000:1000 "$STACK_DIR/$d"; done
  [ -f "$ENV_FILE" ] && { chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
  return 0
}
copy_code_trees(){ # repo -> $STACK_DIR: code, templates and scripts (never live data or secrets)
  install -d -o root -g root -m 755 "$STACK_DIR/caddy" "$STACK_DIR/scripts" "$STACK_DIR/librarian"
  install -d -o 1000 -g 1000 "$STACK_DIR/authelia"
  cp -f "$SRC_DIR/docker-compose.yml" "$SRC_DIR/docker-compose.authelia.yml" "$SRC_DIR/docker-compose.ephemera.yml" "$STACK_DIR/"
  # never overwrite the live user database or rendered config with the repo's empty templates.
  # authelia/ is mounted read-write into the Authelia container, so nothing root executes lives
  # there: the gate injector and its snippet go to the root-owned scripts/ instead.
  rsync -a --exclude db.sqlite3 --exclude notification.txt --exclude configuration.yml --exclude users_database.yml \
    --exclude '*.bak' --exclude __pycache__ --exclude inject-gate.py --exclude caddy-gate.snippet "$SRC_DIR/authelia/" "$STACK_DIR/authelia/"
  rm -f "$STACK_DIR/authelia/inject-gate.py" "$STACK_DIR/authelia/caddy-gate.snippet"
  [ -f "$STACK_DIR/authelia/users_database.yml" ] || cp -f "$SRC_DIR/authelia/users_database.yml" "$STACK_DIR/authelia/"
  cp -f "$SRC_DIR/caddy/Dockerfile" "$SRC_DIR/caddy/Caddyfile.template" "$STACK_DIR/caddy/"
  cp -f "$SRC_DIR/scripts/"*.sh "$SRC_DIR/scripts/"*.py "$SRC_DIR/authelia/inject-gate.py" "$SRC_DIR/authelia/caddy-gate.snippet" "$STACK_DIR/scripts/"
  rsync -a --delete --exclude state --exclude tests --exclude __pycache__ "$SRC_DIR/librarian/" "$STACK_DIR/librarian/"
  rsync -a --delete --exclude __pycache__ "$SRC_DIR/monitoring/" "$STACK_DIR/monitoring/"   # kuma-bootstrap build context
  rm -rf "$STACK_DIR/configs"; cp -R "$SRC_DIR/configs" "$STACK_DIR/configs"
  # root-owned and not writable by uid 1000 (cron, systemd and this TUI run them as root)
  local d
  for d in caddy/Dockerfile caddy/Caddyfile.template scripts configs monitoring docker-compose.yml docker-compose.authelia.yml docker-compose.ephemera.yml; do
    chown -R root:root "$STACK_DIR/$d"; chmod -R go-w "$STACK_DIR/$d"
  done
  chown -R root:root "$STACK_DIR/librarian"; chmod -R go-w "$STACK_DIR/librarian"
  install -d -o 1000 -g 1000 "$STACK_DIR/librarian/state"; chown -R 1000:1000 "$STACK_DIR/librarian/state"   # portal data
  chmod 755 "$STACK_DIR/scripts/"*.sh "$STACK_DIR/scripts/inject-gate.py"
  record_version
}
build_version(){ # identifier baked into the librarian/caddy images (compose: ${BUILD_VERSION:-dev})
  # `git describe` when this checkout is a git working tree, else a date stamp. The portal
  # reports it on /healthz?detail=1, so a stale image can be spotted instead of guessed (J03).
  local v; v=$(git -C "$SRC_DIR" describe --always --dirty --tags 2>/dev/null) || v=""
  printf '%s' "${v:-$(date -u +%Y%m%d-%H%M)}"
}
record_version(){ # which code is deployed: the build version plus the copy date
  printf '%s %s\n' "$(build_version)" "$(date -u +%Y-%m-%dT%H:%MZ)" > "$STACK_DIR/.version"
}
deployed_version(){ cut -d' ' -f1 "$STACK_DIR/.version" 2>/dev/null; }
prune_shelfmark_placeholder(){ # J35: Shelfmark mkdirs its un-substituted INGEST_DIR template
  # ("/dropbox/{User}") at start-up; the portal then logs it as an unknown user's folder every
  # scan. Remove it only while it is empty — a real (if oddly named) folder is never touched.
  rmdir "$STACK_DIR/library/dropbox/{User}" 2>/dev/null || true
  return 0
}
# Shelfmark keeps a SIGNED client-side session and only consults CWA's app.db at login, so a
# removed or re-passworded user stays signed in (J07) — and a plain restart does NOT change that:
# Shelfmark persists its Flask secret to CONFIG_DIR/.flask_secret, which compose bind-mounts, so
# the same key verifies the same cookie after the restart. Deleting that file first is what
# actually ends every Shelfmark session. Returns non-zero when the restart failed, in which case
# the running process still holds the old key in memory and nobody has been signed out.
restart_shelfmark(){ # [--end-sessions]
  local rc=0
  [ "${1:-}" = --end-sessions ] && rm -f "$STACK_DIR/shelfmark/config/.flask_secret"
  compose restart shelfmark >/dev/null 2>&1 || rc=1
  prune_shelfmark_placeholder
  return $rc
}
inject_authelia_gate(){ # per-host bypass lists live in inject-gate.py (reads first, writes second)
  python3 "$STACK_DIR/scripts/inject-gate.py" "$STACK_DIR/caddy/Caddyfile" "$STACK_DIR/scripts/caddy-gate.snippet"
}
# Caddy's admin API is a unix socket (no TCP port on the host); fall back to a restart.
reload_caddy(){ compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --address unix//run/caddy-admin.sock 2>/dev/null || compose restart caddy; }
route_src_ip(){ ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}'; }
public_ip(){ curl -4 -fsS -m 10 https://api.ipify.org 2>/dev/null || route_src_ip; }
ip_on_host(){ ip -o addr show 2>/dev/null | grep -qF " $1/"; }
# Shape check only (fail2ban and Cloudflare do the real validation): enough to keep a typo out
# of an API URL and out of `fail2ban-client set <jail> unbanip`.
valid_ip(){ [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { [[ "$1" == *:* ]] && [[ "$1" =~ ^[0-9A-Fa-f:.]+$ ]]; }; }
# The address this SSH session comes FROM. After a Cloudflare ban that is almost always the one
# that has to be released: the whole household shares one public address, so the admin is locked
# out of the sites together with everyone else. Over Tailscale it is a 100.64/10 tailnet address,
# which is never what Cloudflare banned — the caller then picks from the banned list instead.
caller_ip(){
  local c="${SSH_CLIENT:-${SSH_CONNECTION:-}}"; c="${c%% *}"
  case "$c" in 100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*|127.*|"") c="";; esac
  printf '%s' "$c"
}
json(){ python3 -c "import sys,json; d=json.load(sys.stdin); print($1)" 2>/dev/null; }
wait_for(){ # url seconds
  local i; for i in $(seq 1 "$2"); do curl -fsS -m 3 "$1" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

# ---------- 1. base system ----------
# Docker publishes ports around ufw, so the daemon's default publish address is loopback: a
# `ports:` line without an explicit host IP can never open a service to the internet.
# Written BEFORE docker-ce is installed; if docker is already running and it changed, restart.
write_docker_daemon_json() { # merge bookstack's keys into an existing daemon.json (provider images may set data-root etc.)
  local f="$ETC/docker/daemon.json" changed
  mkdir -p "$ETC/docker"
  changed=$(python3 - "$f" <<'PY'
import json, sys, os
f = sys.argv[1]
want = {"ip": "127.0.0.1", "live-restore": True, "no-new-privileges": True,
        "log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
cur = {}
if os.path.exists(f):
    try: cur = json.load(open(f))
    except Exception: cur = {}
new = dict(cur); new.update(want)
if json.dumps(new, sort_keys=True) == json.dumps(cur, sort_keys=True):
    print("no"); sys.exit(0)
json.dump(new, open(f, "w"), indent=2); print("yes")
PY
)
  if [ "$changed" = yes ] && systemctl is-active docker >/dev/null 2>&1; then systemctl restart docker; fi
}
setup_swap(){ # swapfile sized to RAM; fstab only gets the line once, and only when swapon worked
  [ -f /swapfile ] && return 0
  local ram swap=2G; ram=$(free -g 2>/dev/null | awk '/Mem/{print $2}'); [ "${ram:-8}" -le 4 ] && swap=4G
  if fallocate -l "$swap" /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q '^/swapfile ' "$ETC/fstab" 2>/dev/null || echo '/swapfile none swap sw 0 0' >> "$ETC/fstab"
  else
    rm -f /swapfile; echo "(could not create a swapfile; continuing without swap)"
  fi
}
# L19: Tailscale from its SIGNED apt repository, not `curl | sh` — the same packages, but apt
# verifies every future update against the repository key instead of trusting one download.
install_tailscale_apt() {
  local id codename
  id=$(. /etc/os-release 2>/dev/null; echo "${ID:-debian}"); codename=$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-bookworm}")
  case "$id" in debian|ubuntu) ;; *) id=debian;; esac
  mkdir -p /usr/share/keyrings
  curl -fsSL "https://pkgs.tailscale.com/stable/$id/$codename.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && curl -fsSL "https://pkgs.tailscale.com/stable/$id/$codename.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list \
    && apt-get update -qq && apt-get install -y -qq tailscale
}
# L19: keep the journal across reboots (the 04:30 reboot otherwise erases the evidence of what
# went wrong before it), capped: disk is this plan's binding constraint.
write_journald(){
  mkdir -p "$ETC/systemd/journald.conf.d"
  [ "$ETC" = /etc ] && mkdir -p /var/log/journal
  cat > "$ETC/systemd/journald.conf.d/90-bookstack.conf" << 'JRN'
[Journal]
Storage=persistent
SystemMaxUse=200M
JRN
  systemctl restart systemd-journald 2>/dev/null || true
}
write_sysctl(){
  mkdir -p "$ETC/sysctl.d"
  cat > "$ETC/sysctl.d/90-bookstack.conf" << 'SYS'
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
# Caddy binds BIND_IP on the five public sites (the tailnet-only sites deliberately do not bind
# at all — see the tailnet_only snippet in caddy/Caddyfile.template). Kept so that a BIND_IP
# which is temporarily absent, e.g. an interface flap on a NAT'd VPS, cannot stop Caddy starting.
net.ipv4.ip_nonlocal_bind = 1
net.ipv6.ip_nonlocal_bind = 1
# L19: kernel hardening that costs a single-purpose box nothing
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.yama.ptrace_scope = 1
kernel.unprivileged_bpf_disabled = 1
fs.protected_symlinks = 1
fs.protected_hardlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2
# small-host tuning: prefer RAM over the swapfile; large libraries need many inotify watches (ABS)
vm.swappiness = 10
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
SYS
  sysctl --system >/dev/null
}
setup_firewall(){ # deny in; SSH only until Lock SSH; the torrent peer port only while torrents are enabled
  ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
  if [ "$(envget SSH_LOCKED)" = true ]; then
    ufw --force delete allow 22/tcp >/dev/null 2>&1 || true      # Lock SSH was chosen: keep it closed
  else
    ufw allow 22/tcp >/dev/null
  fi
  torrent_port "$(torrents_on && echo open || echo close)"
  seedbox_port "$(seedbox_on && echo open || echo close)"
  ufw --force enable >/dev/null
}
seedbox_port(){ # open|close Syncthing's port 22000 (the seedbox connects to it)
  if [ "$1" = open ]; then ufw allow 22000/tcp >/dev/null; ufw allow 22000/udp >/dev/null
  else ufw --force delete allow 22000/tcp >/dev/null 2>&1 || true; ufw --force delete allow 22000/udp >/dev/null 2>&1 || true; fi
}
torrent_port(){ # open|close the qBittorrent peer port 6881
  if [ "$1" = open ]; then ufw allow 6881/tcp >/dev/null; ufw allow 6881/udp >/dev/null
  else ufw --force delete allow 6881/tcp >/dev/null 2>&1 || true; ufw --force delete allow 6881/udp >/dev/null 2>&1 || true; fi
}
# sshd keeps the FIRST value it reads for a keyword and includes sshd_config.d/*.conf in lexical
# order, so a provider's 50-cloud-init.conf (PasswordAuthentication yes) beats a 90- file. 01- wins.
SSH_DROPIN=01-bookstack.conf
harden_ssh(){ # sets $sshnote; key-only SSH, verified against sshd's EFFECTIVE configuration
  local d="$ETC/ssh/sshd_config.d" home="${STACK_HOME:-/home/$STACK_USER}" rootkeys="${BOOKSTACK_ROOT_KEYS:-/root/.ssh/authorized_keys}"
  if ! [ -s "$rootkeys" ] && ! [ -s "$home/.ssh/authorized_keys" ]; then
    sshnote="No SSH key found, so password login was left ON. Add your key to /root/.ssh/authorized_keys and re-run this step."; return 0
  fi
  mkdir -p "$d"; rm -f "$d/90-bookstack.conf"
  cat > "$d/$SSH_DROPIN" << 'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 3
# L19: the rest of the checklist. No AllowUsers on purpose: a provider's default login account
# (debian, ubuntu, admin...) would be locked out by a list that does not name it.
PermitEmptyPasswords no
LoginGraceTime 20
ClientAliveInterval 300
ClientAliveCountMax 2
AllowAgentForwarding no
AllowTcpForwarding no
SSH
  if ! sshd -t 2>/dev/null; then
    rm -f "$d/$SSH_DROPIN"
    sshnote="WARNING: sshd rejected the configuration (sshd -t), so the hardening was NOT applied and SSH was not reloaded. Check /etc/ssh/sshd_config."; return 1
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  local sshd_eff; sshd_eff=$(sshd -T 2>/dev/null || true)    # not piped: grep -q + pipefail = SIGPIPE "failure"
  if printf '%s\n' "$sshd_eff" | grep -qi '^passwordauthentication no'; then
    sshnote="SSH is now key-only (verified with sshd -T)."
  else
    sshnote="WARNING: SSH password login is STILL ON although $SSH_DROPIN says no — another file in /etc/ssh overrides it (check: sshd -T | grep -i passwordauth)."; return 1
  fi
}
step_system() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get -y -qq upgrade
  # cron runs the disk watchdog and the Cloudflare allowlist refresh; minimal images lack it.
  # iproute2 (ip) backs route_src_ip/ip_on_host — without it BIND_IP falls back to the public
  # address and the Lock-SSH guard can never pass; procps (free) sizes the swapfile.
  apt-get -y -qq install ca-certificates curl jq ufw unattended-upgrades openssl rsync python3 systemd-timesyncd cron iproute2 procps \
    || { msg "Package installation failed (apt). Check the network and run System again."; return 1; }
  systemctl enable --now cron >/dev/null 2>&1 || true
  timedatectl set-ntp true 2>/dev/null || true   # TOTP, Kobo sync timestamps, ACME and Cloudflare mTLS all need a correct clock

  write_docker_daemon_json
  if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc || { msg "Could not fetch Docker's signing key."; return 1; }
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq && apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-compose-plugin \
      || { msg "Docker installation failed. Nothing else was set up; fix apt and run System again."; return 1; }
  fi

  ensure_stack_user || return 1
  setup_swap
  write_sysctl
  write_journald
  setup_firewall

  dpkg-reconfigure -f noninteractive unattended-upgrades
  sed -i 's|^//Unattended-Upgrade::Automatic-Reboot .*|Unattended-Upgrade::Automatic-Reboot "true";|;s|^//Unattended-Upgrade::Automatic-Reboot-Time .*|Unattended-Upgrade::Automatic-Reboot-Time "04:30";|' /etc/apt/apt.conf.d/50unattended-upgrades

  local sshnote=""; harden_ssh || true

  make_dirs
  local fw="SSH + firewall"; [ "$(envget SSH_LOCKED)" = true ] && fw="firewall (SSH stays Tailscale-only: Lock SSH is on)"
  msg "System ready: updates, Docker, $fw, swap, kernel hardening, auto-updates.\n\n$sshnote"
}
make_dirs() {
  mkdir -p "$STACK_DIR"/{caddy/data,caddy/config,cwa/config,abs/config,abs/metadata,qbt/config,downloads/incomplete,authelia}
  mkdir -p "$STACK_DIR"/library/{books,ingest,audiobooks,podcasts,staging,dropbox,seedbox,seedbox-sync}
  mkdir -p "$STACK_DIR"/{kuma/data,librarian/state,shelfmark/config,ephemera/data,ephemera/downloads,syncthing}
  own_data_dirs
}

# ---------- 2. tailscale ----------
step_tailscale() {
  command -v tailscale >/dev/null || install_tailscale_apt || { msg "Could not install Tailscale from its signed package repository (see the output above)."; return 1; }
  # The package normally starts tailscaled itself; on a Debian 13 minimal image it did not
  # ("dial unix /var/run/tailscaled.socket: no such file"), so every tailscale command failed.
  systemctl enable --now tailscaled >/dev/null 2>&1 || true
  local w; for w in $(seq 1 15); do tailscale status >/dev/null 2>&1 && break; tailscale status 2>&1 | grep -q "Logged out\|NeedsLogin" && break; sleep 1; done
  systemctl is-active --quiet tailscaled || { msg "The Tailscale service (tailscaled) does not start.\n\nSee: journalctl -u tailscaled -n 30 --no-pager"; return 1; }
  clear
  echo "Tailscale will print a login URL. Open it in your browser and approve this machine."
  echo
  tailscale up --ssh || { msg "tailscale up failed. Run the Tailscale step again."; return 1; }
  ip=$(tailscale ip -4 | head -1)
  [ -n "$ip" ] || { msg "Tailscale did not report an IPv4 address. Run the Tailscale step again."; return 1; }
  envset TAILSCALE_IP "$ip"
  ufw allow in on tailscale0 >/dev/null
  msg "Tailscale is up. This server's private IP is $ip.\n\nInstall the Tailscale app on your laptop and phone (same account). The admin tools (monitor., dl. when torrents are on) and later SSH will only be reachable while connected to it.\n\n$TS_EXPIRY_NOTE"
  big "Tailscale: keep this server from reaching your other devices" "Recommended once (containers here share the host network, so a compromised one sits on
your tailnet too):

1. Tag this machine:   tailscale up --ssh --advertise-tags=tag:bookstack
   (the admin console must allow the tag: Access controls -> tagOwners:
      \"tag:bookstack\": [\"autogroup:admin\"] )
2. In your ACL policy, NO rule may have tag:bookstack as a SOURCE. Your own devices may
   reach it; it may reach nothing. Example:
      {\"action\": \"accept\", \"src\": [\"autogroup:member\"], \"dst\": [\"tag:bookstack:*\"]}
3. Tagged machines do not expire by default; otherwise disable key expiry (next screen)."
  # checked, not just advised: an expired node key silently drops the admin tools and SSH
  while ! ts_key_expiry_disabled; do
    yesno "Key expiry is still ENABLED for this machine.\n\nOpen $TS_ADMIN_URL -> this machine -> '...' -> 'Disable key expiry', then choose Yes to check again (No = remind me later; Self-test keeps warning)." || break
  done
  return 0
}
TS_ADMIN_URL="https://login.tailscale.com/admin/machines"
TS_EXPIRY_NOTE="IMPORTANT: Tailscale node keys expire after 180 days by default. Open the Tailscale admin console ($TS_ADMIN_URL) -> this machine -> 'Disable key expiry', or SSH over Tailscale stops working ~6 months from now (Self-test warns when it is due)."
ts_key_expiry_disabled(){ [ "$(tailscale status --json 2>/dev/null | jq -r '.Self.KeyExpiry // "null"' 2>/dev/null)" = null ]; }

# ---------- 3. configure ----------
suggest_admin_name(){ # "<mail-local-part>-admin", never the guessable "admin"
  local l; l=$(printf '%s' "${1%%@*}" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9._-')
  case "$l" in ""|admin|root|administrator|info|postmaster|webmaster) printf 'libadmin';; *) printf '%s-admin' "$l";; esac
}
valid_username(){ [[ "$1" =~ ^[a-z0-9][a-z0-9._-]{1,31}$ ]]; }
valid_email(){ [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; }
# The domain is rendered into every Caddy site address and into the Cloudflare API URLs; a
# character that does not belong in a hostname can only produce a config nobody can reach.
valid_domain(){ [[ "$1" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$ ]]; }
# `lib rename-user` moves the Calibre-Web account row; the portal's own owner-keyed rows
# (requests, prefs, pw_sync, abs_tags, audit) are librarian/cwa.py's half of the same rename.
# Four stores live OUTSIDE both databases and key on the NAME, and a rename that leaves them
# behind is silent: the admin's dropbox becomes a folder the watcher no longer scans (so their own
# uploads are never imported), an already-configured qBittorrent keeps saving into it, Authelia
# still knows them under the old name (they pass the gate as one person and the apps as another),
# and Ephemera's bind mount still points at the old dropbox. Every arm is best-effort and
# independent: a rename must never be undone by one of them failing.
rename_user_artifacts(){ # old new
  local old="$1" new="$2" db="$STACK_DIR/library/dropbox"
  [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
  if [ -d "$db/$old" ]; then
    if [ -d "$db/$new" ]; then      # both exist (a half-finished earlier rename): keep the new
      find "$db/$old" -mindepth 1 -maxdepth 1 -exec mv -f {} "$db/$new/" \; >/dev/null 2>&1 || true
      rmdir "$db/$old" >/dev/null 2>&1 || true
    else
      mv "$db/$old" "$db/$new" >/dev/null 2>&1 || true
    fi
  fi
  install -d -o 1000 -g 1000 "$db/$new" >/dev/null 2>&1 || true
  authelia_rename_user "$old" "$new" >/dev/null 2>&1 || true
  if [ "$(envget EPHEMERA_OWNER)" = "$old" ]; then envset EPHEMERA_OWNER "$new" || true; fi
  # the save path is the ADMIN's dropbox; reseed only when the admin is the one being renamed
  if [ "$new" = "$(admin_user)" ] && torrents_on; then qbt_seed_config >/dev/null 2>&1 || true; fi
  return 0
}
ensure_admin_name(){ # rename the CWA admin row to ADMIN_USER when it still has an older name (factory 'admin')
  # 0 = the admin is called ADMIN_USER now, 1 = could not tell (portal down), 2 = rename failed
  local want list src; want=$(admin_user)
  list=$(users_json) || return 1; [ -n "$list" ] || return 1
  printf '%s' "$list" | python3 -c 'import sys,json; sys.exit(0 if any(x["name"]==sys.argv[1] for x in json.load(sys.stdin)) else 1)' "$want" 2>/dev/null && return 0
  for src in "$(envget ADMIN_USER_PREV)" admin; do
    [ -n "$src" ] && [ "$src" != "$want" ] || continue
    if printf '%s' "$list" | python3 -c 'import sys,json; sys.exit(0 if any(x["name"]==sys.argv[1] and x["is_admin"] for x in json.load(sys.stdin)) else 1)' "$src" 2>/dev/null; then
      lib rename-user "$src" "$want" >/dev/null 2>&1 && { rename_user_artifacts "$src" "$want"; envset ADMIN_USER_PREV ""; return 0; }
      return 2                     # the old admin exists but the rename failed
    fi
  done
  return 1
}
step_configure() {
  # what the containers hold right now: a changed value only reaches them when they are RECREATED
  local pre_d pre_tz pre_email pre_cf pre_dns pre_au
  pre_d=$(envget DOMAIN); pre_tz=$(envget TZ); pre_email=$(envget ADMIN_EMAIL)
  pre_cf=$(envget CF_API_TOKEN); pre_dns=$(envget CF_DNS_TOKEN); pre_au=$(envget ADMIN_USER)
  d=$(ask "Domain (zone in Cloudflare):" "$(envget DOMAIN)")                       ; [ -n "$d" ] || return 1
  valid_domain "$d" || { msg "'$d' is not a hostname (letters, digits, hyphens and at least one dot, e.g. example.com). Nothing was changed."; return 1; }
  e=$(ask "Admin email (for Let's Encrypt notices):" "$(envget ADMIN_EMAIL)")     ; [ -n "$e" ] || return 1
  valid_email "$e" || { msg "'$e' is not an e-mail address. Let's Encrypt would refuse the ACME account and no certificate would ever issue. Nothing was changed."; return 1; }
  tz=$(ask "Timezone:" "$(envget TZ)") || tz="$(envget TZ)"                       ; [ -n "$tz" ] || tz=UTC
  tok=$(askpw "Cloudflare API token (Zone:Read, DNS:Edit, Zone Settings:Edit, Cache Rules:Edit, Firewall Services:Edit; SSL and Certificates:Edit for Security -> Origin lock). Leave blank to keep existing.") ; [ -n "$tok" ] || tok="$(envget CF_API_TOKEN)"
  [ -n "$tok" ] || { msg "A Cloudflare API token is required."; return 1; }
  if [ -n "$(envget ADMIN_HASH)" ] && yesno "Keep the existing admin-gate password (for the Tailscale-only admin tools)?"; then pw=""; else
    pw=$(askpw2 "Password for the admin gate in front of the Tailscale-only admin tools (qBittorrent, Ephemera):") || return 1; fi
  local cur_au au sugg dtok
  cur_au=$(envget ADMIN_USER); sugg=$(suggest_admin_name "$e")
  au=$(ask "Username of YOUR admin account (Calibre-Web, the portal and Shelfmark). Avoid 'admin': bots guess it, and Calibre-Web then locks that name for the day." "${cur_au:-$sugg}") || au="${cur_au:-$sugg}"
  au=$(printf '%s' "$au" | tr 'A-Z' 'a-z'); [ -n "$au" ] || au="${cur_au:-$sugg}"
  valid_username "$au" || { msg "'$au' is not a valid username (2-32 of a-z 0-9 . _ -). Nothing was changed."; return 1; }
  dtok=$(askpw "Optional, recommended: a SECOND Cloudflare token with only Zone -> DNS -> Edit and Zone -> Zone -> Read on this zone. Caddy uses it for certificates, so the powerful main token never sits in a container.\n\nBlank = keep the current one (or reuse the main token).") || dtok=""

  local old_tok; old_tok=$(envget CF_API_TOKEN)
  envset DOMAIN "$d"; envset ADMIN_EMAIL "$e"; envset TZ "$tz"
  envset PUID 1000; envset PGID 1000
  envset CF_API_TOKEN "$tok"
  # Caddy's certificate token (C7): the separate one when given; else it follows the main token
  if [ -n "$dtok" ]; then envset CF_DNS_TOKEN "$dtok"
  elif [ -z "$(envget CF_DNS_TOKEN)" ] || [ "$(envget CF_DNS_TOKEN)" = "$old_tok" ]; then envset CF_DNS_TOKEN "$tok"; fi
  # append-safe: ADMIN_USER_PREV is the only record of the name the CWA row still carries, and
  # ensure_admin_name clears it once the rename lands. Overwriting it on a second rename (the
  # portal was down for the first) loses the real old name and the rename can never succeed.
  if [ -n "$cur_au" ] && [ "$cur_au" != "$au" ] && [ -z "$(envget ADMIN_USER_PREV)" ]; then envset ADMIN_USER_PREV "$cur_au"; fi
  envset ADMIN_USER "$au"
  # PUBLIC_IP = the DNS A records. Detected only when empty or when the admin agrees: a value set by
  # hand (1:1 NAT providers) is never silently replaced.
  local det cur_pub cur_bind rsrc
  det=$(public_ip); cur_pub=$(envget PUBLIC_IP)
  if [ -z "$cur_pub" ]; then envset PUBLIC_IP "$det"
  elif [ -n "$det" ] && [ "$det" != "$cur_pub" ] && yesno "The stored public IP ($cur_pub) differs from the detected one ($det).\n\nYes = re-detect (use $det).\nNo = keep $cur_pub (for example you set it by hand behind NAT)."; then envset PUBLIC_IP "$det"; fi
  # BIND_IP = the local address Caddy listens on (behind 1:1 NAT it is the private address)
  rsrc=$(route_src_ip); cur_bind=$(envget BIND_IP)
  if [ -z "$cur_bind" ] || ! ip_on_host "$cur_bind"; then envset BIND_IP "${rsrc:-$(envget PUBLIC_IP)}"; fi
  envdefault LIBRARIAN_SECRET "$(openssl rand -hex 32)"
  # INTAKE_TOKEN stays empty (webhook off) until Library -> Intake enables it
  for kv in SRC_GUTENBERG:true SRC_STANDARD:true SRC_ARCHIVE:true SRC_LIBRIVOX:true SRC_MYCATALOG:false TORRENTS_ENABLED:false \
            APPROVALS_REQUIRED:false SHELFMARK_LANGUAGE:en SHELFMARK_CONCURRENCY:1 EPHEMERA_ENABLED:false AUTHELIA_ENABLED:false \
            FLARESOLVERR_ENABLED:false; do
    envdefault "${kv%%:*}" "${kv##*:}"; done
  for kv in $IMG_DEFAULTS; do envdefault "${kv%%=*}" "${kv#*=}"; done
  envdefault IA_COLLECTIONS "gutenberg,opensource,americana,cdl"
  envdefault SHELFMARK_TITLE "Library search"
  envdefault TAILSCALE_IP "127.0.0.1"

  # caddy reads the password from stdin when --plaintext is omitted: it never appears in argv.
  if [ -n "$pw" ]; then
    # caddy hash-password reads the password from stdin and needs the trailing newline
    # (without it caddy 2.11 exits "Error: EOF" and prints nothing)
    local h; h=$(printf '%s\n' "$pw" | docker run --rm -i "$CADDY_BASE" caddy hash-password 2>/dev/null) || h=""
    # the settings above are already in .env: say so, or the admin concludes the token and the
    # admin username were not stored and stops looking for the half-written ADMIN_HASH
    valid_admin_hash "$h" || { msg "Could not hash the admin-gate password (docker run $CADDY_BASE caddy hash-password failed). The other settings you typed WERE saved, but no Caddyfile was rendered — which is what the next step will complain about. Fix Docker and run Configure again."; return 1; }
    envset ADMIN_HASH "$h"
  fi

  make_dirs
  copy_code_trees
  render_caddy_all || return 1
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && render_authelia_config
  own_data_dirs   # compose runs as root; the containers' uid must not read the secrets
  # a running Caddy picks the new file up now (validated first), not at the next reboot
  apply_caddy || return 1
  if portal_up && [ "$(envget ADMIN_PW_SET)" = true ]; then
    local rc=0; ensure_admin_name || rc=$?
    case "$rc" in
      2) msg "Could not rename the Calibre-Web admin account to '$au' (Operations -> Logs -> librarian). Deploy tries again.";;
      # rc=1 used to be silent, so .env could name an account that does not exist while the
      # Deploy summary, the qBittorrent save path and the Ephemera owner all trusted it
      1) msg "Could not confirm the Calibre-Web admin account is called '$au' (the portal answered nothing, or no matching account was found). .env now says ADMIN_USER=$au — run Users -> List users to check which name really exists.";;
    esac
  fi
  # A changed DOMAIN / TZ / token only reaches a container when it is RECREATED. Reloading Caddy
  # is not enough: caddy keeps the OLD CF_DNS_TOKEN in its environment, so ACME DNS-01 for the new
  # zone is attempted with the old token and no certificate ever issues, and the portal keeps
  # building every user-facing link from the old domain.
  local applied=""
  if [ "$(envget DOMAIN)" != "$pre_d" ] || [ "$(envget TZ)" != "$pre_tz" ] || [ "$(envget ADMIN_EMAIL)" != "$pre_email" ] \
     || [ "$(envget CF_API_TOKEN)" != "$pre_cf" ] || [ "$(envget CF_DNS_TOKEN)" != "$pre_dns" ] || [ "$(envget ADMIN_USER)" != "$pre_au" ]; then
    if running caddy || portal_up; then
      clear; echo "Recreating the containers so they pick up the new settings..."
      if stack_up_all >/dev/null 2>&1; then applied="\n\nThe containers were recreated, so caddy, the portal and Shelfmark now hold the new values."
      else applied="\n\nWARNING: the containers could NOT be recreated, so they still hold the OLD domain, timezone and Cloudflare token — certificates for a new zone will not issue and the portal's links stay stale. Run Install -> 5 Deploy (Operations -> Logs shows why)."; fi
    else
      applied="\n\nNothing is running yet: Install -> 5 Deploy starts everything with these values."
    fi
    # jail.local bakes in the Cloudflare token and the abs-login filter bakes in the domain: an
    # 'active' jail with a revoked token or the old hostname looks fine and bans nothing.
    if [ "$(envget DOMAIN)" != "$pre_d" ] || [ "$(envget CF_API_TOKEN)" != "$pre_cf" ]; then
      if command -v fail2ban-client >/dev/null 2>&1; then
        render_fail2ban || true; systemctl restart fail2ban >/dev/null 2>&1 || true
        applied="$applied\nfail2ban's jails were re-rendered with the new domain / Cloudflare token."
      fi
    fi
  fi
  msg "Configuration written to $STACK_DIR.\n\nPublic IP (DNS): $(envget PUBLIC_IP)   Caddy binds: $(envget BIND_IP)\nTailscale IP: $(envget TAILSCALE_IP)   Admin account: $(admin_user)$applied\n\nRequests from family members are fulfilled at once (no admin approval). Turn approvals on under Library -> Sources if you want to review each request.\n\n(If Tailscale IP is 127.0.0.1, run the Tailscale step then Configure again.)"
}

# ---------- 4. cloudflare ----------
CF_FAILS=""   # what the current Cloudflare run could not do; listed instead of claiming success
cf_fail(){ CF_FAILS="$CF_FAILS\n  - $1"; }
cf_dns() { # name ip proxied
  local id body
  id=$(cf GET "/zones/$ZONE/dns_records?type=A&name=$1.$DOMAIN" | jq -r '.result[0].id // empty') || { cf_fail "DNS $1: lookup failed"; return 1; }
  body=$(jq -nc --arg n "$1.$DOMAIN" --arg ip "$2" --argjson p "$3" '{type:"A",name:$n,content:$ip,ttl:1,proxied:$p}')
  if [ -n "$id" ]; then cf PUT "/zones/$ZONE/dns_records/$id" --data "$body" >/dev/null || { cf_fail "DNS $1.$DOMAIN -> $2"; return 1; }
  else cf POST "/zones/$ZONE/dns_records" --data "$body" >/dev/null || { cf_fail "DNS $1.$DOMAIN -> $2"; return 1; }; fi
}
cf_dns_check() { # name ip proxied: read the record back
  cf GET "/zones/$ZONE/dns_records?type=A&name=$1.$DOMAIN" 2>/dev/null \
    | jq -e --arg ip "$2" --argjson p "$3" '.result[0] | select(.content == $ip and .proxied == $p)' >/dev/null 2>&1 \
    || cf_fail "DNS $1.$DOMAIN does not point at $2 (proxied=$3) when read back"
}
cf_dns_delete() { # name: remove an A record bookstack no longer uses (e.g. the old aria. host)
  local id; id=$(cf GET "/zones/$ZONE/dns_records?type=A&name=$1.$DOMAIN" 2>/dev/null | jq -r '.result[0].id // empty' 2>/dev/null)
  [ -z "$id" ] || cf DELETE "/zones/$ZONE/dns_records/$id" >/dev/null 2>&1 || true
}
cf_setting() { cf PATCH "/zones/$ZONE/settings/$1" --data "{\"value\":\"$2\"}" >/dev/null || cf_fail "zone setting $1=$2 (token needs Zone Settings:Edit)"; }
cf_setting_check() { # name value: read a security-critical setting back
  [ "$(cf GET "/zones/$ZONE/settings/$1" 2>/dev/null | jq -r '.result.value // empty' 2>/dev/null)" = "$2" ] \
    || cf_fail "zone setting $1 is not '$2' when read back"
}
cf_zone() { DOMAIN=$(envget DOMAIN); ZONE=$(cf GET "/zones?name=$DOMAIN" | jq -r '.result[0].id // empty'); [ -n "$ZONE" ]; }
cf_ruleset_rule() { # phase description rule-json: upsert ONE rule (matched by description) in the zone's entrypoint ruleset, keeping the others
  local phase="$1" desc="$2" rule="$3" cur rules
  cur=$(cf GET "/zones/$ZONE/rulesets/phases/$phase/entrypoint" 2>/dev/null | jq -c '.result.rules // []' 2>/dev/null); cur="${cur:-[]}"
  rules=$(jq -nc --argjson cur "$cur" --argjson r "$rule" --arg d "$desc" '[$cur[] | select(.description != $d) | del(.last_updated, .version)] + [$r]') || return 1
  cf PUT "/zones/$ZONE/rulesets/phases/$phase/entrypoint" --data "{\"rules\":$rules}" >/dev/null
}
cf_cache_rule() { # never let the edge cache a book/audio response and serve it to another user
  local d="$1" rule
  rule=$(jq -nc --arg e "(http.host in {\"books.$d\" \"audio.$d\" \"request.$d\" \"shelf.$d\"})" \
    '{action:"set_cache_settings",action_parameters:{cache:false},expression:$e,description:"bookstack no-cache",enabled:true}')
  cf_ruleset_rule http_request_cache_settings "bookstack no-cache" "$rule"
}

write_cron(){ # name schedule command: /etc/cron.d file with a full PATH (cron's default lacks /usr/sbin, where ufw lives)
  mkdir -p "$ETC/cron.d"
  printf 'SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n%s root %s\n' "$2" "$3" > "$ETC/cron.d/$1"
  chown root:root "$ETC/cron.d/$1"; chmod 644 "$ETC/cron.d/$1"
}
step_cloudflare() {
  need DOMAIN CF_API_TOKEN PUBLIC_IP TAILSCALE_IP || return 1
  cf_zone || { msg "Zone $(envget DOMAIN) not found with this token."; return 1; }
  CF_FAILS=""
  local h pub ts; pub=$(envget PUBLIC_IP); ts=$(envget TAILSCALE_IP)
  local public="books audio request shelf" private="monitor upload" privnote=""
  # auth. only exists while Authelia runs; published unconditionally it is a public hostname that
  # 502s and renews a certificate forever for a service nothing listens on.
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && public="$public auth"
  torrents_on && private="$private dl"
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && private="$private ephemera"
  # 127.0.0.1 is the placeholder Configure writes before Tailscale exists: publishing the admin
  # hostnames pointing at it advertises them in the zone and the read-back would still "verify".
  if [ "$ts" = 127.0.0.1 ]; then
    privnote="\n- $private: DNS NOT created — TAILSCALE_IP is still the 127.0.0.1 placeholder. Run Install -> Tailscale, then this step again."
    private=""
  fi
  for h in $public; do cf_dns "$h" "$pub" true; done
  for h in $private; do cf_dns "$h" "$ts" false; done
  cf_dns_delete aria    # AriaNg/aria2 were removed from the stack

  cf_setting ssl strict
  cf_setting always_use_https on
  cf_setting min_tls_version 1.2
  cf_setting tls_1_3 on
  cf_setting tls_client_auth on
  cf_setting security_level medium
  # Kobo, KOReader, OPDS readers and the Audiobookshelf apps are not browsers: the Browser
  # Integrity Check challenges them and the failure is silent on the device.
  cf_setting browser_check off
  cf_setting opportunistic_encryption on
  cf_setting automatic_https_rewrites on
  # Content rewriting breaks the portal (script-src 'none' CSP): obfuscated e-mails and injected JS.
  cf_setting email_obfuscation off
  cf_setting rocket_loader off
  cf_setting cache_level basic
  local cachenote
  if cf_cache_rule "$DOMAIN"; then cachenote="- Cache Rule: never cache books/audio/request/shelf responses at the edge"
  else cachenote="- Cache Rule could NOT be created (token needs Cache Rules:Edit). In the dashboard: Rules -> Cache Rules -> add\n  'bookstack no-cache': hostname is books./audio./request./shelf.$DOMAIN -> Bypass cache"; fi

  # Cloudflare's SHARED origin-pull CA: kept beside the trust file, which aop_write_trust fills
  # with it, with the zone's own CA (L14), or with both while a switch is being proven.
  curl -fsS https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem \
       -o "$STACK_DIR/caddy/cf-shared-ca.pem" || { msg "Could not download Cloudflare's origin-pull CA. Check the network and run this step again."; return 1; }
  chown root:root "$STACK_DIR/caddy/cf-shared-ca.pem"; chmod 644 "$STACK_DIR/caddy/cf-shared-ca.pem"
  aop_write_trust "$(aop_mode)" || { msg "Could not write $STACK_DIR/caddy/cf-origin-pull-ca.pem. Run this step again."; return 1; }

  "$STACK_DIR/scripts/cf-ips.sh" >/dev/null || cf_fail "firewall allowlist (scripts/cf-ips.sh) could not be applied"
  # STACK_DIR= like the disk-watch cron: cf-ips.sh reads $STACK_DIR/.env for the alert channel, so
  # without it a failed nightly refresh on a non-default STACK_DIR is completely silent.
  write_cron bookstack-cfips "15 3 * * *" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/cf-ips.sh >/dev/null 2>&1"

  # verify what matters instead of trusting the PATCH/PUT answers: with SSL not strict or
  # origin pulls off, Caddy's client-certificate check rejects Cloudflare and every site fails
  cf_setting_check ssl strict
  cf_setting_check tls_client_auth on
  for h in $public; do cf_dns_check "$h" "$pub" true; done
  for h in $private; do cf_dns_check "$h" "$ts" false; done
  if [ -n "$CF_FAILS" ]; then
    big "Cloudflare: NOT fully configured" "These items failed:$CF_FAILS

Fix the token permissions (Zone:Read, DNS:Edit, Zone Settings:Edit,
Cache Rules:Edit, Firewall Services:Edit) or set them in the dashboard, then run this step again.
The public sites will not work while SSL is not 'Full (strict)' or Authenticated Origin Pulls is off."
    return 1
  fi
  local aopnote="- Origin lock: Cloudflare's SHARED client certificate (proves 'a Cloudflare edge', not 'your zone')"
  case "$(aop_mode)" in
    zone) aopnote="- Origin lock: this zone's OWN client certificate (Security -> Origin lock rotates it)";;
    both) aopnote="- Origin lock: switching to this zone's own certificate (finished after Deploy)";;
    *) if yesno "Lock the origin to THIS zone only?\n\nRight now Caddy accepts Cloudflare's shared client certificate, which every Cloudflare customer's traffic carries. A certificate of your own, uploaded to this zone, proves the request came through YOUR zone.\n\nNeeds the token permission Zone -> SSL and Certificates -> Edit.\n\nSet it up now?"; then
         step_origin_cert && aopnote="- Origin lock: this zone's OWN client certificate$([ "$(aop_mode)" = both ] && printf ' (Caddy trusts both until Deploy proves it)')"
       fi;;
  esac
  msg "Cloudflare configured (read back and verified):\n- DNS: $(printf '%s' "$public" | tr ' ' '/') -> proxied (orange)$([ -n "$private" ] && printf '; %s -> tailnet IP only' "$private")$privnote\n- SSL Full (strict), TLS 1.2+, HTTPS forced, Authenticated Origin Pulls ON\n- Browser Integrity Check, e-mail obfuscation and Rocket Loader OFF (they break e-readers and the portal)\n$cachenote\n- Firewall allows web ports only from Cloudflare (auto-refreshed nightly)\n$aopnote\n\nIn the dashboard: on a paid plan turn the WAF managed rules ON (the Free plan's baseline runs by itself; nothing to buy).\nDo NOT enable Bot Fight Mode: it challenges Kobo/OPDS/KOReader/Audiobookshelf apps, cannot be exempted on the Free plan, and the devices fail silently. Leave it OFF."
}

# ---------- L14: this zone's own origin-pull certificate ----------
# Authenticated Origin Pulls with Cloudflare's shared CA proves "some Cloudflare edge": the same
# client certificate is presented for every Cloudflare customer. Zone-level AOP presents a
# certificate the admin issued, so Caddy can require "came through THIS zone". The CA and the
# client key live in /etc/bookstack/aop (root, 0600; never in a snapshot: a rebuilt server
# issues new ones). The switch is proven before it is final: Caddy first trusts both CAs, then
# ours only, and a request through Cloudflare must still succeed or it goes back to both.
AOP_DIR_REL=bookstack/aop
aop_dir(){ printf '%s' "$ETC/$AOP_DIR_REL"; }
aop_mode(){ local m; m=$(envget AOP_MODE); case "$m" in zone|both) printf '%s' "$m";; *) printf shared;; esac; }
aop_write_trust(){ # shared | both | zone -> caddy/cf-origin-pull-ca.pem (what Caddy's client_auth trusts)
  local t="$STACK_DIR/caddy/cf-origin-pull-ca.pem" sh="$STACK_DIR/caddy/cf-shared-ca.pem" own; own="$(aop_dir)/ca.pem"
  case "$1" in
    shared) [ -s "$sh" ] && cp -f "$sh" "$t.new";;
    both) [ -s "$own" ] && [ -s "$sh" ] && cat "$own" "$sh" > "$t.new";;
    zone) [ -s "$own" ] && cp -f "$own" "$t.new";;
    *) false;;
  esac || { rm -f "$t.new"; return 1; }
  mv -f "$t.new" "$t" && chmod 644 "$t"
}
aop_make_certs(){ # a private CA and one client leaf (RSA 4096, CA:FALSE, clientAuth), as Cloudflare requires
  local d; d=$(aop_dir); mkdir -p "$d"; chmod 700 "$d"
  rm -f "$d/client.pem.new" "$d/client.key.new"
  ( umask 077; cd "$d" || exit 1; dom=$(envget DOMAIN)
    if [ ! -s ca.key ] || ! openssl x509 -checkend $((400*86400)) -noout -in ca.pem >/dev/null 2>&1; then
      openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 3650 -subj "/CN=bookstack origin-pull CA ($dom)" \
        -keyout ca.key -out ca.pem -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" || exit 1
    fi
    openssl req -newkey rsa:4096 -nodes -sha256 -subj "/CN=cloudflare-origin-pull.$dom" -keyout client.key.new -out client.csr || exit 1
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n' > client.ext
    openssl x509 -req -sha256 -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 1095 -extfile client.ext -out client.pem.new || exit 1
    rm -f client.csr client.ext ca.srl ) >/dev/null 2>&1 || return 1
  openssl verify -CAfile "$d/ca.pem" "$d/client.pem.new" >/dev/null 2>&1 || return 1
  chmod 644 "$d/ca.pem"; chmod 600 "$d/ca.key" "$d/client.key.new" "$d/client.pem.new"
}
step_origin_cert(){
  need DOMAIN CF_API_TOKEN || return 1
  cf_zone || { msg "Zone $(envget DOMAIN) not found with this token."; return 1; }
  [ -s "$STACK_DIR/caddy/cf-shared-ca.pem" ] || { msg "Run Install -> Cloudflare first."; return 1; }
  local d body ans id st i old
  d=$(aop_dir)
  aop_make_certs || { msg "Could not generate the origin-pull certificate (openssl). Nothing was changed."; return 1; }
  body=$(jq -nc --rawfile c "$d/client.pem.new" --rawfile k "$d/client.key.new" '{certificate:$c, private_key:$k}')
  ans=$(cf POST "/zones/$ZONE/origin_tls_client_auth" --data "$body" 2>/dev/null) || ans=""
  id=$(printf '%s' "$ans" | jq -r '.result.id // empty' 2>/dev/null)
  if [ -z "$id" ]; then
    rm -f "$d/client.pem.new" "$d/client.key.new"
    msg "Cloudflare did not accept the certificate upload. The usual cause: the API token lacks Zone -> SSL and Certificates -> Edit (add it in the Cloudflare dashboard, then run Security -> Origin lock).\n\nNothing changed: the origin stays locked with Cloudflare's shared certificate."
    return 1
  fi
  mv -f "$d/client.pem.new" "$d/client.pem"; mv -f "$d/client.key.new" "$d/client.key"
  for i in $(seq 1 30); do
    st=$(cf GET "/zones/$ZONE/origin_tls_client_auth/$id" 2>/dev/null | jq -r '.result.status // empty' 2>/dev/null)
    [ "$st" = active ] && break; sleep 10
  done
  old=$(envget AOP_CERT_ID); [ "$old" = "$id" ] && old=""
  envset AOP_CERT_ID "$id"; [ -n "$old" ] && envset AOP_OLD_CERT_ID "$old"
  if [ "$st" != active ]; then
    msg "The certificate was uploaded but Cloudflare has not deployed it yet (status: ${st:-unknown}).\n\nNothing else changed. Run Security -> Origin lock again in a few minutes."
    return 1
  fi
  # trust both BEFORE Cloudflare switches, so no request fails in between
  aop_write_trust both || { msg "Could not write the trust file. Nothing else changed."; return 1; }
  caddy_up && reload_caddy
  cf PUT "/zones/$ZONE/origin_tls_client_auth/settings" --data '{"enabled":true}' >/dev/null 2>&1 || true
  if [ "$(cf GET "/zones/$ZONE/origin_tls_client_auth/settings" 2>/dev/null | jq -r '.result.enabled // empty' 2>/dev/null)" != true ]; then
    aop_write_trust "$([ "$(envget AOP_MODE)" = zone ] && echo zone || echo shared)"; caddy_up && reload_caddy
    msg "Cloudflare did not switch zone-level origin pulls on (read back: not enabled). Caddy was put back as it was."
    return 1
  fi
  envset AOP_MODE both
  aop_tighten
}
step_origin_lock(){ # Security menu: set up, finish, or renew the zone's own certificate
  case "$(aop_mode)" in
    both) cf_zone >/dev/null 2>&1; aop_tighten;;
    zone) yesno "The origin already accepts only this zone's own certificate (valid until $(openssl x509 -enddate -noout -in "$(aop_dir)/client.pem" 2>/dev/null | cut -d= -f2)).\n\nIssue and upload a NEW one now (renewal)? Caddy keeps trusting the same CA, so nothing breaks while Cloudflare rolls it out." && step_origin_cert;;
    *) step_origin_cert;;
  esac
}
caddy_up(){ [ "$(docker inspect -f '{{.State.Running}}' caddy 2>/dev/null)" = true ]; }
# A request through Cloudflare, from here: any answer below 500 means the TLS handshake between
# the edge and Caddy succeeded (the gate's login page or a 404 are fine); 525/526 mean it failed.
aop_probe(){ local c i d n="${1:-3}"; d=$(envget DOMAIN)
  for i in $(seq 1 "$n"); do
    c=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://request.$d/healthz?aop=$i$RANDOM" 2>/dev/null || echo 000)
    [ "${c:-000}" -ge 200 ] 2>/dev/null && [ "$c" -lt 500 ] && return 0
    sleep 5
  done; return 1; }
aop_tighten(){ # both -> zone, proven; called by step_origin_cert and at the end of Deploy
  [ "$(aop_mode)" = both ] || return 0
  caddy_up || { msg "This zone's certificate is active at Cloudflare. Caddy trusts both certificates for now; Deploy finishes the switch."; return 0; }
  # The site must answer through Cloudflare BEFORE the switch: right after Deploy starts Caddy
  # its own certificates are still being issued (DNS-01, about a minute), and a probe then fails
  # for that reason — which the switch below would blame on the origin certificate.
  if ! aop_probe 12; then
    msg "The site does not answer through Cloudflare yet (Caddy may still be getting its certificates; that takes a minute or two after it starts).\n\nNothing was changed: Caddy trusts both certificates. When https://request.$(envget DOMAIN) opens in a browser, run Security -> Origin lock."
    return 1
  fi
  aop_write_trust zone || return 1
  # a restart, not a reload: Cloudflare keeps connections to the origin open, and a reused one
  # would pass the probe on the OLD trust
  compose restart caddy >/dev/null 2>&1; sleep 5
  if aop_probe 6; then
    envset AOP_MODE zone
    local old; old=$(envget AOP_OLD_CERT_ID)
    if [ -n "$old" ]; then [ -n "${ZONE:-}" ] || cf_zone >/dev/null 2>&1; cf DELETE "/zones/$ZONE/origin_tls_client_auth/$old" >/dev/null 2>&1 || true; envset AOP_OLD_CERT_ID ""; fi
    msg "Origin locked to THIS zone: Caddy now accepts only the client certificate this server issued and uploaded to $(envget DOMAIN). A request through Cloudflare was made to prove it.\n\nThe certificate is valid 3 years; the daily cert-watch warns 60 days ahead (Security -> Origin lock renews it)."
    return 0
  fi
  aop_write_trust both; compose restart caddy >/dev/null 2>&1
  msg "Cloudflare did not present this zone's certificate yet (a request through it failed with only that certificate trusted), so Caddy was put back to trusting both. Nothing is broken.\n\nRun Security -> Origin lock again later; Cloudflare can take several minutes to roll a new certificate out."
  return 1
}

# ---------- 5. deploy ----------
apply_library_defaults() {
  # Secure + sane defaults inside the apps, so nothing has to be clicked in a GUI:
  # CWA: registration off, Kobo sync on, store proxy off; convert on ingest to EPUB, keep
  # per-user copies separate (new_record), and CWA's import-time Kindle EPUB fixer OFF (it
  # rewrites every archive and strips the owner tag from CBZ) — see step_formats.
  local out
  out=$(lib harden 2>/dev/null) || return 1
  cwa_sql "UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='epub', auto_ingest_automerge='new_record', kindle_epub_fixer=0;" >/dev/null 2>&1 || true
  # PDFs and comics keep their native format: converting them to reflowed EPUB is slow on two
  # cores and poor quality, and the portal tags PDF/CBZ before import anyway.
  cwa_sql "UPDATE cwa_settings SET auto_convert_ignored_formats='pdf,cbz,cbr,cb7';" >/dev/null 2>&1 || true
  # CWA's own "backup" copies of every processed file double the disk use and end up in the
  # restic snapshot too; the real backup is restic. Separate statement: older schemas lack the columns.
  cwa_sql "UPDATE cwa_settings SET auto_backup_imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0;" >/dev/null 2>&1 || true
  # Isolation invariants the self-test checks: duplicate auto-resolve could merge two users' copies,
  # a metadata fetch could replace the owner:<user> tag. Separate statement for older schemas.
  cwa_sql "UPDATE cwa_settings SET duplicate_auto_resolve_enabled=0, auto_metadata_update_tags=0;" >/dev/null 2>&1 || true
  # Per-user copies of the same title are INTENTIONAL here (owner:<user> isolation), so CWA's
  # duplicate scan groups different people's books and invites the admin to 'resolve' them by
  # deleting another reader's copy. Auto-resolve is off above; this silences the prompt too
  # (column exists in CWA v4.0.6's cwa_schema.sql). Own statement: older schemas lack it.
  cwa_sql "UPDATE cwa_settings SET duplicate_notifications_enabled=0;" >/dev/null 2>&1 || true
  # CWA's Hardcover auto-ID task writes identifiers across the WHOLE library (the CWA source read
  # for v5 found it); per-reader Hardcover progress sync (Devices) does not need it. Kept off.
  cwa_sql "UPDATE cwa_settings SET hardcover_auto_fetch_enabled=0;" >/dev/null 2>&1 || true
  # CWA reads its settings table at start-up: restart it when something actually changed.
  if printf '%s' "$out" | grep -q '"changed": true'; then
    echo "Restarting Calibre-Web to apply security defaults..."
    compose restart calibre-web >/dev/null 2>&1 || true
    wait_for http://127.0.0.1:8083/login 60 || true
  fi
}
install_disk_watch() { # hourly watchdog: alerts at 85 %, stops downloaders at 95 %, cleans growers (scripts/disk-watch.sh)
  mkdir -p "$ETC/bookstack"
  write_cron bookstack-disk "17 * * * *" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/disk-watch.sh >/dev/null 2>&1"
  install_metadata_push
  install_heal
  install_cert_watch
  install_update_check
}
install_update_check() { # L06: weekly "a newer release exists" notice (never updates by itself)
  write_cron bookstack-updatecheck "20 7 * * 1" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/update-check.sh 2>&1 | logger -t bookstack-updatecheck"
}
install_cert_watch() { # L09: daily certificate / origin CA / Cloudflare token expiry watch
  write_cron bookstack-certwatch "40 6 * * *" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/cert-watch.sh 2>&1 | logger -t bookstack-certwatch"
}
install_heal() { # L20: restart a container Docker reports unhealthy (scripts/heal.sh; no socket-mounted container)
  write_cron bookstack-heal "*/2 * * * *" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/heal.sh 2>&1 | logger -t bookstack-heal"
}
install_metadata_push() { # every 15 min: the portal's queued metadata -> Calibre, so devices show it
  # A host job because the portal cannot do it: it mounts the library read-only and has no Docker
  # socket, both on purpose. Output goes to the journal (logger), not /dev/null: this is the job
  # that alerts if a write ever changes a book's owner tag, and its routine output is how an admin
  # confirms it is running at all.
  write_cron bookstack-metapush "*/15 * * * *" "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/metadata-push.sh 2>&1 | logger -t bookstack-metapush"
}
# Loop until the factory admin/admin123 is gone. Cancel generates a random password (shown in
# the summary): there is no path that leaves the default live behind a public hostname.
set_admin_password() {
  local apw au
  until [ "$(envget ADMIN_PW_SET)" = true ]; do
    # the factory account is called 'admin'; it is renamed to the chosen name first (C10)
    local rc=0; ensure_admin_name || rc=$?
    if [ "$rc" = 2 ]; then
      if [ "$(admin_user)" != admin ]; then
        msg "Could not rename the factory 'admin' account to '$(admin_user)'. Its password is set now anyway; the name stays 'admin' (run Install -> Configure later to try the rename again)."
        envset ADMIN_USER_PREV "$(admin_user)"; envset ADMIN_USER admin
      fi
    fi
    au=$(admin_user)
    ADMIN_PW_GENERATED=""
    if ! apw=$(askpw2 "Set the password for your admin account '$au' (Calibre-Web, the portal and Shelfmark all use it). Replaces the factory default admin123.\n\nCancel = generate a random one and show it at the end."); then
      apw=$(openssl rand -base64 18); ADMIN_PW_GENERATED="$apw"
    fi
    if printf '%s' "$apw" | lib passwd "$au" --password-stdin >/dev/null 2>&1; then
      # the loop's only exit condition is this flag: a silent write failure (full disk, read-only
      # root) used to re-prompt for the password forever with no Cancel path out
      envset ADMIN_PW_SET true || { msg "Could not write $ENV_FILE (disk full, or the filesystem is read-only?). The password WAS set on the account but not recorded here, so Deploy cannot continue. Free some space and run Deploy again."; return 1; }
    else
      yesno "Could not set the admin password (is the portal up? Operations -> Logs -> librarian). Try again?" \
        || { msg "The admin password is still the factory default, so Caddy was NOT started: nothing is reachable from the internet. Fix the portal and run Deploy again."; return 1; }
    fi
  done
}
abs_initialised(){ [ "$(absctl status 2>/dev/null | json 'd.get("isInit")')" = True ]; }   # fail closed: unknown = not initialised
step_deploy() {
  need DOMAIN CF_API_TOKEN || return 1
  [ -f "$STACK_DIR/caddy/cf-origin-pull-ca.pem" ] || { msg "Run the Cloudflare step first."; return 1; }
  # the checkout this script runs from is what gets deployed (code, templates, scripts)
  make_dirs; copy_code_trees
  render_caddy_all || return 1
  own_data_dirs
  write_shelfmark_metadata_env || true
  clear; echo "Building images and starting containers (first run takes a few minutes)..."
  # BUILD_VERSION is baked into the images (compose build arg, default "dev"); the portal
  # reports it on /healthz and Self-test compares it with $STACK_DIR/.version (J03).
  BUILD_VERSION="$(build_version)" compose build --pull caddy librarian kuma-bootstrap || { msg "Image build failed (caddy/librarian/kuma-bootstrap). See the output above; nothing was started."; return 1; }
  compose pull --ignore-buildable || { msg "Image pull failed. Check the network / registry and run Deploy again."; return 1; }
  # Caddy (the only thing that listens publicly) starts LAST: after the admin password is
  # set and Audiobookshelf has its root user, never before.
  local early="calibre-web audiobookshelf uptime-kuma"; torrents_on && early="$early qbittorrent"
  compose up -d $early || { msg "Containers failed to start. Operations -> Logs shows why."; return 1; }
  echo -n "Waiting for Calibre-Web to initialise its database"
  for _ in $(seq 1 60); do [ -f "$STACK_DIR/cwa/config/app.db" ] && break; echo -n .; sleep 2; done; echo
  if [ ! -f "$STACK_DIR/cwa/config/app.db" ]; then
    msg "Calibre-Web did not create app.db in time. Check 'Logs -> calibre-web', then run Deploy again."; return 1
  fi
  compose up -d librarian shelfmark || { msg "The portal or Shelfmark failed to start. Operations -> Logs shows why."; return 1; }
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && { composeA up -d authelia || { msg "Authelia failed to start."; return 1; }; }
  solver_on && { compose up -d flaresolverr || echo "(FlareSolverr did not start; see Operations -> Logs)"; }
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && { composeE up -d ephemera || echo "(Ephemera did not start; see Operations -> Logs)"; }
  echo "Waiting for the portal..."; wait_for http://127.0.0.1:8090/healthz 45 || true
  sleep 5
  prune_shelfmark_placeholder    # J35: drop Shelfmark's empty "{User}" template folder
  apply_library_defaults || echo "(could not apply library defaults yet — run Users → Repair later)"
  d=$(envget DOMAIN)
  set_admin_password || return 1
  if ! abs_initialised; then
    msg "Audiobookshelf has no root user yet. Whoever opened https://audio.$d first would become its administrator, so it is set up now, before the site goes public."
    step_abs_setup || yesno "Audiobookshelf is still uninitialised: the first visitor of audio.$d would become root. Start Caddy anyway (NOT recommended)?" || { msg "Caddy was not started. Run Library -> Audiobookshelf, then Deploy again."; return 1; }
  fi
  # L16: the portal's Shelfmark service login (recreate the portal so it sees the credentials)
  if ensure_shelfmark_service; then compose up -d librarian >/dev/null 2>&1 || true
  else echo "(could not create the Shelfmark service account; Shelfmark approvals stay in Shelfmark's own UI)"; fi
  compose up -d caddy || { msg "Caddy failed to start. Operations -> Logs -> caddy."; return 1; }
  apply_caddy || true     # an already-running Caddy is not recreated by `up`: validate + reload the new file
  install_disk_watch
  install_postboot_unit   # the unattended 04:30 reboot needs checking even without restic configured
  install_selftest_timer  # ...and every hour after it, pushed to Kuma
  # Monitoring is set up as the LAST step, once everything it watches is running: Kuma gets its
  # admin account, notification channels and monitors without anyone opening its web UI.
  # Never fatal: a stack that is up must not be reported as a failed Deploy because Kuma lagged.
  echo "Configuring Uptime Kuma (monitors, alert channels, reboot window)..."
  # L14: a switch to this zone's own origin-pull certificate is proven once Caddy runs
  [ "$(aop_mode)" = both ] && { aop_tighten || true; }
  local monline; setup_monitoring || true; monline="$MON_NOTE"
  local privline="https://monitor.$d  (Uptime Kuma)" qline=""
  if torrents_on; then
    qpw=$(envget QBIT_PASS)
    privline="https://dl.$d  (qBittorrent)   $privline"; qline="  qBittorrent: admin / ${qpw:-<see: docker logs qbittorrent>}"
  fi
  adminline="the password you just set"; [ -n "${ADMIN_PW_GENERATED:-}" ] && adminline="GENERATED password: $ADMIN_PW_GENERATED  (change it under Users -> Reset password)"
  # read AFTER the ABS branch above: on a fresh install step_abs_setup asks for this name a few
  # seconds earlier in this same Deploy, and this summary is the screen the admin copies from
  ru=$(envget ABS_ROOT_USER); absnote="Audiobookshelf: root user '${ru:-root}' (Library -> Audiobookshelf re-runs setup)"
  big "Stack is up" "Public (your users):
  https://request.$d   the portal: search, request, upload, My books, Devices
  https://books.$d     the library (Kobo/OPDS/Send-to-Kindle also live here)
  https://audio.$d     audiobooks        https://shelf.$d   extended search

Private (Tailscale only, admin):
  $privline

Logins:
  $(admin_user) (portal / Calibre-Web / Shelfmark): $adminline
  $absnote
$qline
Monitoring: $monline

Already applied for you: public registration OFF, Kobo sync ON, convert-to-EPUB on import,
per-user copies kept separate, CWA's Kindle EPUB fixer OFF (the portal applies the Kindle fixes when it mails a book; on import the fixer would strip the owner tag from comics), CWA's duplicate file copies OFF,
hourly disk watchdog (alerts at 85 %, stops downloaders at 95 %), hourly self-test (pushed to Kuma).

Next: Users & devices -> Add user (isolated account + Kobo link + ABS login in one go),
Library -> Mail for Send-to-Kindle from the portal, Install -> Backups, Install -> Alerts, Operations -> Self-test."
}

# ---------- 6. backups ----------
RESTIC_ENV_FILE_REL=bookstack/restic.env   # under $ETC; the scripts read /etc/bookstack/restic.env
# RESTIC_ENV_PATH lets step_backup try a CANDIDATE credential file against the repository before
# it replaces the live one (that file is the only on-host copy of the repository key).
restic_env(){ printf '%s' "${RESTIC_ENV_PATH:-$ETC/$RESTIC_ENV_FILE_REL}"; }
write_restic_env() { # prompts for repository / password / S3 keys; writes restic.env.new (0600 root)
  local repo rpw k1="" k2="" cur new="$(restic_env).new" kind
  command -v restic >/dev/null || apt-get -y -qq install restic
  cur=$(grep -E '^RESTIC_REPOSITORY=' "$(restic_env)" 2>/dev/null | cut -d= -f2- || true)
  HOME_BACKUP=0
  kind=$(whiptail --title "Backups" --menu "Where should the encrypted backups go?" 15 84 3 \
    H "A computer at home, over Tailscale (free; this server cannot delete them)" \
    S "A storage bucket (Backblaze B2, Wasabi, Cloudflare R2 ...)" \
    O "Other: an SFTP host, a local path, or a repository address you already have" 3>&1 1>&2 2>&3) || return 1
  case "$kind" in
    H) home_backup_target "$cur" || return 1; repo="$HOME_REPO"; HOME_BACKUP=1;;
    S) repo=$(ask "Bucket address for restic (e.g. s3:s3.eu-central-003.backblazeb2.com/my-bucket):" "$cur") || return 1;;
    *) repo=$(ask "restic repository (e.g. sftp:user@host:/path, /mnt/backup, or the rest:http://... address from your notes):" "$cur") || return 1;;
  esac
  [ -n "$repo" ] || return 1
  rpw=$(askpw2 "Encryption password for the backup repository (STORE THIS SAFELY — without it backups are unreadable):") || return 1
  if [[ "$repo" == s3:* ]]; then
    k1=$(ask "S3 / B2 key ID:") || return 1; k2=$(askpw "S3 / B2 application key:") || return 1
  fi
  mkdir -p "$ETC/bookstack"
  # Written NEXT TO the live file, never over it: step_backup moves it into place only after the
  # password has actually opened (or created) the repository. Overwriting first meant one typo —
  # or a "rotation" typed here, which restic does not support (it needs `key add`) — left every
  # existing snapshot permanently unreadable, with nothing on screen saying so.
  # shell-quoted (%q): the file is sourced by bash, so a password with $, spaces, & or quotes
  # must survive intact instead of being expanded or executed
  ( umask 077
    { printf 'RESTIC_REPOSITORY=%q\nRESTIC_PASSWORD=%q\nSTACK_DIR=%q\n' "$repo" "$rpw" "$STACK_DIR"
      [ -n "$k1" ] && printf 'AWS_ACCESS_KEY_ID=%q\n' "$k1"; [ -n "$k2" ] && printf 'AWS_SECRET_ACCESS_KEY=%q\n' "$k2"; true; } > "$new" ) \
    || { msg "Could not write $new (disk full or read-only?). Nothing was changed."; return 1; }
  chmod 600 "$new"; chown root:root "$new"
  # prove it reads back exactly before anyone relies on it
  local back; back=$(bash -c 'set -a; . "$1"; printf "%s" "$RESTIC_PASSWORD"' _ "$new")
  [ "$back" = "$rpw" ] || { rm -f "$new"; msg "Internal error: the backup password did not round-trip through restic.env. Nothing saved."; return 1; }
}
# The free, append-only target: restic's rest-server on a computer the admin already owns,
# reached over Tailscale. Started with --append-only it refuses every DELETE from this server
# (measured: 403 on forget), --private-repos confines this server to its own repository, and
# retention runs on that computer against the folder itself (scripts/prune.sh's job, done there).
REST_SERVER_IMG="restic/rest-server:0.14.0"
RESTIC_IMG="restic/restic:0.19.1"
home_backup_target(){ # [current repository] -> HOME_REPO (rest:http://bookstack:<pw>@<ip>:<port>/bookstack/)
  local cur="$1" ip port pw="" h code d
  d=$(printf '%s' "$cur" | sed -nE 's#^rest:http://bookstack:([A-Za-z0-9]+)@([0-9.]+):([0-9]+)/bookstack/?$#\1 \2 \3#p')
  ip=$(ask "The home computer's Tailscale address (100.x.y.z; it must be on the same Tailscale account and switched on at night — the backup runs at 01:00):" "$(printf '%s' "$d" | cut -d' ' -f2)") || return 1
  [[ "$ip" =~ ^100\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]] && [ "${BASH_REMATCH[1]}" -ge 64 ] && [ "${BASH_REMATCH[1]}" -le 127 ] \
    || { msg "'$ip' is not a Tailscale address (100.64.0.0 - 100.127.255.255). Find it in the Tailscale app on that computer."; return 1; }
  port=$(ask "Port for the backup server on that computer:" "$(printf '%s' "$d" | cut -d' ' -f3 | grep . || echo 8000)") || return 1
  [[ "$port" =~ ^[0-9]{2,5}$ ]] || { msg "'$port' is not a port number."; return 1; }
  # keep the login a working home server already has; a new one needs a new .htpasswd line
  if [ -n "$d" ] && yesno "Keep the backup server login this server already uses?\n\n(No = make a new one; you then replace the line in .htpasswd on the home computer.)"; then
    pw=$(printf '%s' "$d" | cut -d' ' -f1)
  fi
  if [ -z "$pw" ]; then
    pw=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)
    h=$(printf '%s\n' "$pw" | docker run --rm -i "$CADDY_BASE" caddy hash-password 2>/dev/null) || h=""
    [[ "$h" == '$2'* ]] || { msg "Could not make the password hash for the home server (docker run $CADDY_BASE caddy hash-password failed). Nothing was changed."; return 1; }
    big "Set up the backup server at home (once)" "On the computer at home. It needs Docker and Tailscale (same account as this server).

1. Make a folder for the backups, e.g. /srv/restic, on a disk with room for the library.
   Put exactly this ONE line in /srv/restic/.htpasswd:

   bookstack:$h

2. Start the backup server, listening on the Tailscale address only:

   docker run -d --name restic-rest --restart unless-stopped \\
     -p $ip:$port:8000 -v /srv/restic:/data \\
     -e OPTIONS=\"--append-only --private-repos\" $REST_SERVER_IMG

   --append-only: this server can add backups but never delete one.

3. Tailscale admin console -> Access controls. Tag that computer tag:backup and let THIS
   server reach that one port (and nothing else):
     {\"action\": \"accept\", \"src\": [\"tag:bookstack\"], \"dst\": [\"tag:backup:$port\"]}

4. Once a month, ON THAT COMPUTER, drop old backups (this server is not allowed to):
   docker run --rm -v /srv/restic:/data -e RESTIC_PASSWORD='<the backup password>' \\
     $RESTIC_IMG -r /data/bookstack forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
   The self-test here warns when old backups pile up.

Next screen: this server checks it can reach the backup server."
  fi
  while true; do
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -u "bookstack:$pw" "http://$ip:$port/bookstack/config" 2>/dev/null || echo 000)
    case "$code" in
      200|404) break;;                  # 404 = logged in, repository not created yet
      401) yesno "The home backup server answered but refused the login (HTTP 401): the .htpasswd line is missing or different.\n\nFix it (step 1), then Yes to check again." || return 1;;
      *) yesno "Could not reach the backup server at http://$ip:$port (HTTP ${code:-000}).\n\nIs the home computer on, on Tailscale, the container running (step 2), and the Tailscale rule in place (step 3)?\n\nYes = check again." || return 1;;
    esac
  done
  HOME_REPO="rest:http://bookstack:$pw@$ip:$port/bookstack/"
}
POSTBOOT_LOG_REL=.postboot-selftest.log   # written by the post-boot unit; read by postboot_last
# The OnFailure= target of every bookstack unit. Written by each installer that references it:
# it used to exist only once backups were configured, so on an install without restic the
# post-boot unit's OnFailure= pointed at a unit that did not exist, and failed silently.
write_alert_template() {
  local u="$ETC/systemd/system"; mkdir -p "$u"
  cat > "$u/bookstack-alert@.service" << UNIT
[Unit]
Description=Bookstack alert for %i
[Service]
Type=oneshot
ExecStart=$STACK_DIR/scripts/alert.sh 'Bookstack: %i FAILED' 'see: journalctl -u bookstack-%i' high
UNIT
}
install_backup_units() { # backup nightly at 01:00 (before the 04:30 reboot window), restore test on the 1st at 13:00 (never
  # overlapping the backup's repository lock), both alert on failure; plus the post-reboot
  # self-test that proves the stack actually came back from the 04:30 unattended-upgrades reboot
  local u="$ETC/systemd/system"; mkdir -p "$u"
  write_alert_template
  cat > "$u/bookstack-backup.service" << UNIT
[Unit]
Description=Bookstack encrypted backup
OnFailure=bookstack-alert@backup.service
[Service]
Type=oneshot
ExecStart=$STACK_DIR/scripts/backup.sh
UNIT
  cat > "$u/bookstack-backup.timer" << 'UNIT'
[Unit]
Description=Nightly bookstack backup
[Timer]
OnCalendar=*-*-* 01:00:00
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  cat > "$u/bookstack-restore-test.service" << UNIT
[Unit]
Description=Bookstack backup restore test
OnFailure=bookstack-alert@restore-test.service
[Service]
Type=oneshot
ExecStart=$STACK_DIR/scripts/restore-test.sh
UNIT
  cat > "$u/bookstack-restore-test.timer" << 'UNIT'
[Unit]
Description=Monthly bookstack restore test
[Timer]
OnCalendar=*-*-01 13:00:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  install_postboot_unit
  systemctl daemon-reload && systemctl enable --now bookstack-backup.timer bookstack-restore-test.timer
}
# step_system sets unattended-upgrades Automatic-Reboot at 04:30: the one scheduled event that
# restarts every container while nobody is watching. scripts/selftest.sh otherwise runs only from
# the TUI and at the end of an Update, so a stack that never came back stayed silent until
# somebody tried to read a book.
# Installed by install_backup_units AND by Deploy, next to install_disk_watch: an admin who never
# configures restic still gets the one check that runs unattended.
install_postboot_unit() {
  local u="$ETC/systemd/system"; mkdir -p "$u"
  write_alert_template
  # The wrapper is generated here, beside restic.env and disk.state, rather than living in
  # $STACK_DIR/scripts: it bakes in STACK_DIR the same way the unit files do, and
  # copy_code_trees does not own it.
  local pb="$ETC/bookstack/postboot.sh"; mkdir -p "$ETC/bookstack"
  { printf '#!/usr/bin/env bash\nSTACK_DIR=%q\n' "$STACK_DIR"; cat << 'PB'
# Generated by bookstack.sh (install_postboot_unit) — rewritten on every run, do not edit.
# Runs once per boot from bookstack-postboot.service.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
export STACK_DIR                    # selftest.sh and alert.sh both read it from the environment
LOG="$STACK_DIR/.postboot-selftest.log"
ALERT="$STACK_DIR/scripts/alert.sh"
# CWA's healthcheck has a 120 s start period and ABS 60 s. selftest.sh deliberately only WARNS
# for a container still inside its start period, so running too early turns a real failure into
# a warning nobody reads. Wait for the containers to settle instead of sleeping a fixed time: a
# healthy container reports healthy long before its start period is over. Nothing running at all
# (Docker never came up) falls through the deadline and is reported by the self-test itself.
# "something is running and nothing says 'starting'" is NOT enough: caddy and uptime-kuma carry no
# healthcheck, so in the window after dockerd has restarted the first restart:unless-stopped
# container but before calibre-web and audiobookshelf exist, that test is already true — and the
# self-test then reports the containers that have not started yet as FAILED, which is an alert
# after every healthy 04:30 reboot. Wait for the core set to be PRESENT as well. Optional
# containers (qbittorrent, ephemera) are left out: they are not always deployed.
WAIT=${POSTBOOT_WAIT:-900}; STEP=${POSTBOOT_SLEEP:-15}
CORE=${POSTBOOT_CORE:-"caddy calibre-web audiobookshelf librarian shelfmark"}
# disk-watch.sh stops shelfmark when the disk latches, and a container stopped that way does not
# come back under restart:unless-stopped. Waiting for it would burn the whole deadline before the
# self-test ran at all — 15 minutes late for an alert about a box that is already degraded. The
# self-test still reports the stopped container, so dropping it here hides nothing.
[ "$(grep -E '^paused=' /etc/bookstack/disk.state 2>/dev/null | tail -1 | cut -d= -f2)" = 1 ] \
  && CORE=$(printf '%s\n' $CORE | grep -vx shelfmark | tr '\n' ' ')
deadline=$(( $(date +%s) + WAIT ))
while :; do
  names=$(docker ps --format '{{.Names}}' 2>/dev/null)
  missing=0
  for c in $CORE; do printf '%s\n' "$names" | grep -qx -- "$c" || missing=1; done
  st=$(for c in $(docker ps -q 2>/dev/null); do docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null; done | grep -c '^starting$')
  [ "$missing" -eq 0 ] && [ "${st:-0}" -eq 0 ] && break
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep "$STEP"
done
rc=0
# One self-test at a time: the hourly timer can fire while this one is still waiting for the
# containers. This run waits for the lock (it must run); the hourly one skips instead.
# (Best effort: without a writable lock file or flock(1) the run simply goes ahead.)
if { exec 9>"${SELFTEST_LOCK:-/run/bookstack-selftest.lock}"; } 2>/dev/null && command -v flock >/dev/null; then flock -w 900 9 || true; fi
# Publish 'killed' BEFORE the self-test runs. Without this, a run that systemd SIGTERMs at
# TimeoutStartSec (a hung selftest.sh is one of the failure modes this unit exists for) never
# reaches the mv below, so $LOG still holds the PREVIOUS boot's result and every menu reports
# the last reboot as all-clear. A stale pass is worse than no result.
printf 'finished=%s exit=killed\n' "$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)" > "$LOG" 2>/dev/null || true
bash "$STACK_DIR/scripts/selftest.sh" > "$LOG.tmp" 2>&1 || rc=$?
printf 'finished=%s exit=%s\n' "$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)" "$rc" >> "$LOG.tmp"
mv -f "$LOG.tmp" "$LOG" 2>/dev/null || true
cat "$LOG" 2>/dev/null || true      # also into journalctl -u bookstack-postboot
# Kuma's "Self-test (hourly)" push monitor hears about this run too (best effort; the alert
# below is this unit's own channel and does not depend on it)
if [ "$rc" = 0 ]; then "$STACK_DIR/scripts/kuma-push.sh" selftest up "post-reboot: all checks passed" || true
else "$STACK_DIR/scripts/kuma-push.sh" selftest down "post-reboot: $rc check(s) failed: $(grep -F '[FAIL]' "$LOG" 2>/dev/null | head -1 | sed 's/^ *\[FAIL\] //')" || true; fi
# alert.sh directly, not OnFailure=: the generic bookstack-alert@ template can only say the unit
# failed, and after an unattended reboot WHICH checks failed is the whole message.
if [ "$rc" != 0 ]; then
  "$ALERT" "Bookstack: post-reboot self-test FAILED" "$rc check(s) failed after the reboot:

$(grep -F '[FAIL]' "$LOG" 2>/dev/null | head -12)

Full result: $LOG (or journalctl -u bookstack-postboot)" high
fi
exit "$rc"
PB
  } > "$pb"
  chmod 755 "$pb"; chown root:root "$pb" 2>/dev/null || true
  cat > "$u/bookstack-postboot.service" << UNIT
[Unit]
Description=Bookstack post-reboot self-test
# After, never Requires: if Docker itself fails to come up this unit must still RUN and FAIL
# loudly. A unit that is skipped sends no alert, which is exactly the silence being fixed.
After=docker.service network-online.target
Wants=network-online.target
# The wrapper alerts with the failing check names, which is the message that matters. This is
# the backstop for what the wrapper CANNOT reach: a SIGTERM at TimeoutStartSec, a missing or
# non-executable alert.sh, a log redirect that will not open. Without it those modes are silent.
OnFailure=bookstack-alert@post-reboot-selftest.service
[Service]
Type=oneshot
# the health wait plus the self-test's own edge probes can legitimately take minutes; the
# default 90 s start timeout would kill the unit half-way through and call it a failure
TimeoutStartSec=1800
ExecStart=$pb
[Install]
WantedBy=multi-user.target
UNIT
  # enabled, not --now: this unit is for the NEXT boot. Starting it here would sit in the health
  # wait and then re-run a self-test the admin can run from Operations in a second.
  # Both commands are idempotent, so a second Deploy or Backups run rewrites rather than doubles.
  systemctl daemon-reload
  systemctl enable bookstack-postboot.service >/dev/null 2>&1 || true
}
# Hourly self-test. The post-boot unit covers one moment a day; this covers the other 23 hours:
# a container that dies at 14:00, an owner tag that goes missing, a certificate, a full disk.
# Notification is Kuma's job (its "Self-test (hourly)" push monitor alerts on the change and
# repeats once a day while red), so a failure that persists is not an alert every hour. alert.sh
# is the fallback for when Kuma could not be told at all — Kuma down, or monitoring not set up.
# The scheduled run skips the two probes that cost something when repeated 24 times a day:
# the factory-password login (it counts against Calibre-Web's 40-per-day limit for that
# username) and the remote restic query (repository transactions; the backup reports itself).
install_selftest_timer() {
  local u="$ETC/systemd/system" w="$ETC/bookstack/selftest-hourly.sh"; mkdir -p "$u" "$ETC/bookstack"
  write_alert_template
  { printf '#!/usr/bin/env bash\nSTACK_DIR=%q\n' "$STACK_DIR"; cat << 'HW'
# Generated by bookstack.sh (install_selftest_timer) — rewritten on every run, do not edit.
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
export STACK_DIR
LOG="$STACK_DIR/.selftest-hourly.log"
STATE="${SELFTEST_STATE:-/etc/bookstack/selftest.state}"
# a run already going (the post-boot one, or an admin's) makes this hour's redundant
if { exec 9>"${SELFTEST_LOCK:-/run/bookstack-selftest.lock}"; } 2>/dev/null && command -v flock >/dev/null; then flock -n 9 || exit 0; fi
rc=0
SELFTEST_SCHEDULED=1 bash "$STACK_DIR/scripts/selftest.sh" > "$LOG.tmp" 2>&1 || rc=$?
printf 'finished=%s exit=%s\n' "$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)" "$rc" >> "$LOG.tmp"
mv -f "$LOG.tmp" "$LOG" 2>/dev/null || true
first=$(grep -F '[FAIL]' "$LOG" 2>/dev/null | head -1 | sed 's/^ *\[FAIL\] //')
if [ "$rc" = 0 ]; then st=up; m="all checks passed"; now=pass; else st=down; m="$rc check(s) failed: $first"; now=fail; fi
prev=$(cat "$STATE" 2>/dev/null || echo pass)
printf '%s\n' "$now" > "$STATE" 2>/dev/null || true
if ! "$STACK_DIR/scripts/kuma-push.sh" selftest "$st" "$m"; then
  if [ "$now" != "$prev" ]; then
    if [ "$now" = fail ]; then
      "$STACK_DIR/scripts/alert.sh" "Bookstack: hourly self-test FAILED" "$rc check(s) failed:

$(grep -F '[FAIL]' "$LOG" 2>/dev/null | head -12)

(Uptime Kuma could not be told, so this came directly.) Full result: $LOG" high
    else
      "$STACK_DIR/scripts/alert.sh" "Bookstack: hourly self-test passes again" "All checks pass. Full result: $LOG"
    fi
  fi
fi
exit 0
HW
  } > "$w"
  chmod 755 "$w"; chown root:root "$w" 2>/dev/null || true
  cat > "$u/bookstack-selftest.service" << UNIT
[Unit]
Description=Bookstack hourly self-test
After=docker.service
# only a crash of the wrapper itself lands here: it exits 0 after reporting a failed self-test
OnFailure=bookstack-alert@selftest.service
[Service]
Type=oneshot
TimeoutStartSec=900
ExecStart=$w
UNIT
  cat > "$u/bookstack-selftest.timer" << 'UNIT'
[Unit]
Description=Hourly bookstack self-test
[Timer]
OnCalendar=hourly
RandomizedDelaySec=5m
# no catch-up burst after downtime: the post-boot unit runs the first test after a boot
Persistent=false
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now bookstack-selftest.timer >/dev/null 2>&1 || true
}
# The result of the last automatic post-reboot self-test, one line, for the menus. Returns 1 when
# it has never run — which is also the state of every install made before this unit existed.
postboot_last() {
  local log="$STACK_DIR/$POSTBOOT_LOG_REL" line rc when
  [ -s "$log" ] || return 1
  line=$(grep -E '^finished=' "$log" 2>/dev/null | tail -1); [ -n "$line" ] || return 1
  when=${line#finished=}; when=${when%% exit=*}; rc=${line##* exit=}
  # 'killed' is the stamp the wrapper writes BEFORE the self-test: seeing it here means the run
  # was killed part-way (a hung self-test, an OOM, the 1800 s timeout). Reporting that as "never
  # ran" would hide it behind the same silence as a fresh install, so it is a FAILURE.
  case "$rc" in
    killed) printf '%s — KILLED part-way (hung or timed out); see journalctl -u bookstack-postboot' "$when"; return 0;;
    ''|*[!0-9]*) return 1;;
  esac
  [ "$rc" = 0 ] && printf '%s — all checks passed' "$when" || printf '%s — %s check(s) FAILED' "$when" "$rc"
}
# Only a FAILED post-reboot self-test earns space on the first screen; a pass is visible in
# Operations -> Self-test and in journalctl -u bookstack-postboot.
postboot_banner() {
  local last; last=$(postboot_last) || return 0
  case "$last" in *FAILED*|*KILLED*) printf '\\n\\nPost-reboot self-test: %s\\nSee Operations -> Self-test.' "$last";; esac
}
offsite_checklist() {
  big "Keep these OFF this server" "A backup only helps if you can open it from a fresh machine. Store, in a password
manager or on paper, away from this VPS:

  1. restic repository:  $(grep -E '^RESTIC_REPOSITORY=' "$(restic_env)" 2>/dev/null | cut -d= -f2-)
  2. restic password:    (the one you just typed; /etc/bookstack/restic.env holds the only copy here)
  3. S3 / B2 key ID + application key (if the repository is s3:)
  4. Cloudflare API token and the Cloudflare account login
  5. Tailscale account login
  6. A copy of $STACK_DIR/.env (ABS_TOKEN, Authelia secrets, SMTP/IMAP passwords) — it is
     inside the backup too, but only readable with items 1-3.

Rebuild on a new VPS: install Debian, run bookstack.sh -> System, Tailscale, Configure
(same domain), then Operations -> 'Restore from backup'. Kobo links, ABS accounts and
Authelia users come back from the snapshot; only the Tailscale IP changes."
}
# L15: retention with an append-only nightly key. The prune key either lives on this server
# (monthly timer; protects against the nightly key leaking on its own, NOT against root) or on
# the admin's computer (nothing here can delete a snapshot).
PRUNE_ENV_FILE_REL=bookstack/restic-prune.env
remove_prune_units(){
  local u="$ETC/systemd/system"
  if [ -f "$u/bookstack-prune.timer" ]; then
    systemctl disable --now bookstack-prune.timer >/dev/null 2>&1 || true
    rm -f "$u/bookstack-prune.timer" "$u/bookstack-prune.service"; systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  rm -f "$ETC/$PRUNE_ENV_FILE_REL"
}
install_prune_units(){
  local u="$ETC/systemd/system"; mkdir -p "$u"
  write_alert_template
  cat > "$u/bookstack-prune.service" << UNIT
[Unit]
Description=Bookstack backup retention (separate prune key)
OnFailure=bookstack-alert@prune.service
[Service]
Type=oneshot
Environment=RESTIC_PRUNE_ENV=$ETC/$PRUNE_ENV_FILE_REL
ExecStart=$STACK_DIR/scripts/prune.sh
UNIT
  cat > "$u/bookstack-prune.timer" << 'UNIT'
[Unit]
Description=Monthly bookstack backup retention
[Timer]
OnCalendar=*-*-15 03:00:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload && systemctl enable --now bookstack-prune.timer
}
step_prune_key(){
  local live pf repo k1="" k2="" rpw
  live="$(restic_env)"; pf="$ETC/$PRUNE_ENV_FILE_REL"
  repo=$(grep -E '^RESTIC_REPOSITORY=' "$live" | cut -d= -f2-)
  if ! yesno "Retention (forget + prune) needs a key that CAN delete. Where should it live?\n\nYes = on THIS server, in $pf, used by a monthly timer. It stops a leaked nightly key from wiping the backups, but root on this server can read it too.\n\nNo  = on your own computer only (stronger: nothing on this server can delete a snapshot). You run scripts/prune.sh there once a month; the self-test warns when old snapshots pile up."; then
    remove_prune_units
    big "Prune from your own computer" "Once a month, on a computer that is NOT this server:

  1. install restic
  2. create prune.env (chmod 600):
       RESTIC_REPOSITORY=$repo
       RESTIC_PASSWORD=<the backup password>
       AWS_ACCESS_KEY_ID=<a key that CAN delete>        (s3:/B2 only)
       AWS_SECRET_ACCESS_KEY=<its secret>
  3. copy scripts/prune.sh from this repository and run:
       RESTIC_PRUNE_ENV=./prune.env bash prune.sh

Keep 7 daily / 4 weekly / 6 monthly unless you set RESTIC_KEEP_* in the same shell.
The server's self-test warns when snapshots older than the policy pile up."
    return 0
  fi
  if [[ "$repo" == s3:* ]]; then
    k1=$(ask "Prune key ID (a B2/S3 key that CAN delete — NOT the nightly one):") || return 1
    k2=$(askpw "Prune application key:") || return 1
    [ -n "$k1" ] && [ -n "$k2" ] || { msg "No prune key entered: retention will not run. Run Install -> Backups again to add one."; return 1; }
  else
    repo=$(ask "Repository address for pruning (rest-server: a user that is NOT append-only, e.g. rest:https://prune:PASS@host/repo):" "$repo") || return 1
  fi
  rpw=$(bash -c 'set -a; . "$1"; printf "%s" "$RESTIC_PASSWORD"' _ "$live")
  ( umask 077
    { printf 'RESTIC_REPOSITORY=%q\nRESTIC_PASSWORD=%q\nSTACK_DIR=%q\n' "$repo" "$rpw" "$STACK_DIR"
      [ -n "$k1" ] && printf 'AWS_ACCESS_KEY_ID=%q\nAWS_SECRET_ACCESS_KEY=%q\n' "$k1" "$k2"; true; } > "$pf.new" ) \
    || { msg "Could not write $pf.new. Nothing was changed."; return 1; }
  if ! RESTIC_ENV_PATH="$pf.new" restic_run cat config >/dev/null 2>&1; then
    rm -f "$pf.new"; msg "That prune key did not open the repository. Nothing was stored; retention will not run until you add a working one (Install -> Backups)."; return 1
  fi
  mv "$pf.new" "$pf"; chmod 600 "$pf"; chown root:root "$pf" 2>/dev/null || true
  install_prune_units
}
# Debian 12 ships restic 0.14, which has no --retry-lock: passing it unconditionally makes every
# call fail with "unknown flag". Probe it the way the restore path already probes --overwrite.
restic_run(){ ( set -a; . "$(restic_env)"; set +a
  local R=(); restic backup --help 2>/dev/null | grep -q -- '--retry-lock' && R=(--retry-lock 30m)
  restic ${R[@]+"${R[@]}"} "$@" ); }
step_backup() {
  local live new
  live="${RESTIC_ENV_PATH:-$ETC/$RESTIC_ENV_FILE_REL}"; new="$live.new"
  write_restic_env || { rm -f "$new"; return 1; }
  # The repository is created here and only here. backup.sh never runs `restic init`: with a local
  # path on a volume that failed to mount it would quietly start a new repository on the root disk.
  # Everything below runs against the CANDIDATE file; the live one is replaced only on success.
  if ! RESTIC_ENV_PATH="$new" restic_run cat config >/dev/null 2>&1; then
    if [ -s "$live" ] && ! yesno "That password did not open the repository, and $live still holds the CURRENT key — it was NOT overwritten.\n\nNote: a restic password cannot be changed by typing a new one here; restic needs 'key add' and every existing snapshot would otherwise become unreadable. Operations -> 'Rotate the backup repository password' does exactly that, safely.\n\nIs this a brand-new, EMPTY repository that should be created now?"; then
      rm -f "$new"; msg "Nothing was changed: $live still opens your existing backups. Run Backups again with the right password."; return 1
    fi
    if ! RESTIC_ENV_PATH="$new" restic_run init; then
      rm -f "$new"
      msg "Could not open or create the backup repository. Check the address, keys and password, then run Backups again.\n\n$([ -s "$live" ] && printf 'The existing %s was left unchanged.' "$live" || printf 'No credentials were stored.')"
      return 1
    fi
  fi
  # L15: the nightly job runs as root with this key. A key that can delete lets whoever takes the
  # server (or just the key) wipe every snapshot along with it.
  local ao=0
  if [ "${HOME_BACKUP:-0}" = 1 ]; then
    ao=1; printf 'RESTIC_APPEND_ONLY=1\nRESTIC_PRUNE_WHERE=home\n' >> "$new"      # rest-server --append-only; pruned at home
  elif yesno "Is this key APPEND-ONLY — can it add snapshots but NOT delete them?\n\n  - Backblaze B2 (s3: address): an application key WITHOUT deleteFiles:\n      b2 key create --bucket BUCKET bookstack-nightly listBuckets,listFiles,readFiles,writeFiles\n    and a bucket lifecycle rule that keeps hidden files 30 days. restic's deletes become 'hides', recoverable for those 30 days.\n  - rest-server started with --append-only\n\nYes = the nightly job never forgets or prunes; retention runs with a SEPARATE key (next question).\nNo  = this key prunes nightly, and can delete every snapshot."; then
    ao=1; printf 'RESTIC_APPEND_ONLY=1\n' >> "$new"
  fi
  mv "$new" "$live" || { rm -f "$new"; msg "The repository opened, but $live could not be replaced (disk full or read-only?). Nothing was changed."; return 1; }
  chmod 600 "$live"; chown root:root "$live"
  if [ "${HOME_BACKUP:-0}" = 1 ]; then remove_prune_units
  elif [ "$ao" = 1 ]; then step_prune_key || true; else remove_prune_units; fi
  local ping; ping=$(ask "Optional: a dead-man's-switch ping URL (e.g. a free healthchecks.io check). backup.sh calls it after every good backup and URL/fail after a failed one, so you also hear about it when the server is gone. Blank = none." "$(envget BACKUP_PING_URL)") || ping="$(envget BACKUP_PING_URL)"
  envset BACKUP_PING_URL "$ping"
  install_backup_units
  local alertnote="failures alert you via Install -> Alerts"
  [ -z "$(envget NOTIFY_WEBHOOK)" ] && [ -z "$(envget SMTP_HOST)" ] && alertnote="NO alert channel is set yet: run Install -> Alerts, or a failing backup is only in the journal"
  if yesno "Run the first backup now? (may take a while depending on library size)"; then
    clear; "$STACK_DIR/scripts/backup.sh" && msg "First backup complete. Nightly at 01:00; a restore test runs on the 1st of each month; $alertnote." \
      || msg "The first backup FAILED (see the output above). Nightly backups are scheduled anyway; $alertnote."
  else
    msg "Backups scheduled nightly at 01:00 (restore test on the 1st of each month; $alertnote)."
  fi
  offsite_checklist
}
# The safe counterpart to the Backups prompt above, which REFUSES a new password: restic wraps
# one data key per password, so a password is changed by adding a second key and removing the
# first — never by editing RESTIC_PASSWORD, which would leave every snapshot unreadable.
# Order matters and is the whole point: add the new key against the OLD credential, prove the new
# credential opens the repository, remove the old key THROUGH the new one, and only then replace
# /etc/bookstack/restic.env. Any failure leaves the working credential file untouched.
step_restic_rotate() {
  local live new npw pf old_id rc=0 rmnote
  live="${RESTIC_ENV_PATH:-$ETC/$RESTIC_ENV_FILE_REL}"; new="$live.new"
  [ -s "$live" ] || { msg "No backup repository is configured yet, so there is no password to rotate. Install -> Backups first."; return 1; }
  restic_run cat config >/dev/null 2>&1 \
    || { msg "The credential in $live does not open the repository right now, so there is nothing safe to rotate FROM (a rotation needs the working password). Fix Install -> Backups, or the network / S3 keys, and try again."; return 1; }
  yesno "Rotate the backup repository password?\n\nWhat happens, in this order:\n  1. a SECOND key with the new password is added to the repository\n  2. the new key is tested against the live repository\n  3. only then is the old key removed and $live rewritten\n\nEvery existing snapshot stays readable: restic encrypts the data once and wraps that key per password, so nothing is re-encrypted and no snapshot is rewritten.\n\nKeep the OLD password written down until you have run a restore test with the new one.\n\nContinue?" || return 0
  npw=$(askpw2 "NEW repository password (STORE IT SAFELY — without it the backups are unreadable):") || return 1
  pf=$(mktemp "${TMPDIR:-/tmp}/bookstack-newkey.XXXXXX") || { msg "Could not create a temporary file for the new key. Nothing was changed."; return 1; }
  chmod 600 "$pf"; printf '%s' "$npw" > "$pf"
  clear; echo "Adding the new key to the repository..."
  # `key add` runs against the OLD credential: it is the only one that can open the repository now
  if ! restic_run key add --new-password-file "$pf" >/dev/null 2>&1; then
    rm -f "$pf"
    msg "restic refused to add the new key, so NOTHING was changed: $live still holds the working password and every snapshot is still readable exactly as before.\n\n(A repository on read-only storage, or an S3 key without write access, is the usual cause.)"; return 1
  fi
  rm -f "$pf"
  # the candidate credential: same repository and S3 keys, the new password. Written NEXT TO the
  # live file the way write_restic_env does, so a failure below cannot cost the working one.
  ( umask 077; { grep -vE '^RESTIC_PASSWORD=' "$live" || true; printf 'RESTIC_PASSWORD=%q\n' "$npw"; } > "$new" ) \
    || { rm -f "$new"; msg "Could not write $new (disk full or read-only?).\n\nThe new key IS in the repository now, and $live still holds the old password, so backups keep working. Free some space and run this step again."; return 1; }
  chmod 600 "$new"; chown root:root "$new"
  if ! RESTIC_ENV_PATH="$new" restic_run cat config >/dev/null 2>&1; then
    rm -f "$new"
    msg "The new password does NOT open the repository, so nothing was replaced: $live still holds the working one and no key was removed.\n\nA key carrying the new password may have been added; 'restic key list' shows it and 'restic key remove <id>' takes it out again."; return 1
  fi
  # which key the OLD password unlocks — read BEFORE the file is replaced, while it is current
  old_id=$(restic_run key list --json 2>/dev/null | json '"".join(k["id"] for k in d if k.get("current"))') || old_id=""
  if [ -n "$old_id" ]; then
    # removed through the NEW credential: restic refuses to remove the key it is authenticating with
    RESTIC_ENV_PATH="$new" restic_run key remove "$old_id" >/dev/null 2>&1 \
      && rmnote="The old key was removed, so the old password no longer opens the repository." \
      || { rc=1; rmnote="WARNING: the OLD key could NOT be removed, so the old password still opens the repository. Remove it by hand: restic key list, then restic key remove $old_id."; }
  else
    rc=1; rmnote="WARNING: restic did not say which key the old password used, so the OLD key was left in place and that password still opens the repository. Check 'restic key list' and remove it by hand."
  fi
  mv "$new" "$live" || { rm -f "$new"; msg "The new key works and the old one is dealt with, but $live could not be replaced (disk full or read-only?).\n\nThe NIGHTLY BACKUP WILL FAIL until you put the new password into $live by hand (RESTIC_PASSWORD=...)."; return 1; }
  chmod 600 "$live"; chown root:root "$live"
  msg "Repository password rotated. $rmnote\n\n$live now holds the new password and every existing snapshot is still readable with it.\n\nDo this next: Operations -> 'Backup restore test' proves the new credential really restores, and put the new password in your password manager (Install -> Backups shows the off-site checklist)."
  return $rc
}

# ---------- 6a. alerts ----------
step_alerts() { # C1: one channel that reaches the admin even when the portal is down
  local cur url; cur=$(envget NOTIFY_WEBHOOK)
  url=$(ask "Where should server alerts go (failed backups, full disk, failed restore test)?\n\nFree default: the ntfy app (ntfy.sh) on your phone, subscribed to a SECRET topic, e.g.\n  https://ntfy.sh/bookstack-$(openssl rand -hex 6)\nAny ntfy-compatible webhook URL works. Blank = keep; type  none  to remove." "${cur:-https://ntfy.sh/bookstack-$(openssl rand -hex 6)}") || return 1
  [ -n "$url" ] || url="$cur"
  [ "$url" = none ] && url=""
  case "$url" in ""|https://*|http://*) ;; *) msg "That is not an http(s) URL. Nothing changed."; return 1;; esac
  envset NOTIFY_WEBHOOK "$url"; restart_portal_ok || true
  if [ -z "$url" ]; then
    msg "Alert webhook removed.$([ -z "$(envget SMTP_HOST)" ] && printf '\n\nWARNING: no SMTP either, so alerts only reach the journal (Self-test FAILS on this).')"; return 0
  fi
  clear; echo "Sending a test alert..."
  wait_for http://127.0.0.1:8090/healthz 20 || true
  "$STACK_DIR/scripts/alert.sh" "Bookstack test alert" "If you can read this, server alerts reach you. ($(hostname 2>/dev/null))" high
  if yesno "A test alert was sent to:\n  $url\n\nDid it arrive (subscribe to the topic in the ntfy app first)?"; then
    msg "Alerts confirmed. Failed backups, a full disk and a failed restore test will reach you there."
  else
    msg "Not confirmed. Check the URL / topic subscription and run Alerts again. Until then alerts are only written to the journal (journalctl -t bookstack)."; return 1
  fi
}

# ---------- 6b. restore onto a (fresh) server ----------
pick_snapshot(){ # sets SNAP_ID/SNAP_DESC; newest first, tags shown (pre-update)
  local js items
  js=$(restic_run snapshots --json 2>/dev/null) || return 1
  mapfile -t items < <(printf '%s' "$js" | python3 -c '
import sys, json
s = json.load(sys.stdin) or []
s.sort(key=lambda x: x["time"], reverse=True)
for x in s[:40]:
    print(x.get("short_id") or x["id"][:8])
    print("%s UTC  %s" % (x["time"][:16].replace("T", " "), ",".join(x.get("tags") or [])))
' 2>/dev/null)
  [ "${#items[@]}" -ge 2 ] || { msg "No snapshots found in the repository (wrong repository or password?)."; return 1; }
  SNAP_ID=$(whiptail --title "Restore: pick a snapshot" --menu "Newest first. To undo a bad update pick the one tagged pre-update from before it." 22 80 14 "${items[@]}" 3>&1 1>&2 2>&3) || return 1
  local i; SNAP_DESC="$SNAP_ID"
  for ((i=0; i<${#items[@]}; i+=2)); do [ "${items[$i]}" = "$SNAP_ID" ] && SNAP_DESC="$SNAP_ID (${items[$((i+1))]})"; done
  return 0
}
# config + databases only: everything except the library/audiobook trees (a rollback after a bad
# update, or when the books are fine and only settings/users broke)
RESTORE_CONFIG_PATHS=".env .backup-snap authelia caddy/data cwa/config abs/config librarian/state kuma/data shelfmark/config qbt/config"
restore_host_files() { # $1 = .backup-snap/host  -> sets HOST_RESTORED / HOST_MANUAL
  # Files OUTSIDE $STACK_DIR that nothing else regenerates. backup.sh stages them; before this
  # existed, step_restore restored them as inert data under .backup-snap/host/ and nothing ever
  # read them — so a recovery onto a replacement VPS came up serving books with the SSH
  # hardening, the kernel hardening and dockerd's pinned publish address all MISSING, while the
  # restore, the restore test and the self-test all reported success.
  #
  # Only these three. fail2ban's jail and filters, the cron.d files and the systemd units are
  # regenerated from the checkout later in step_restore, and copying the OLD server's versions
  # over the fresh ones would be a downgrade. /etc/bookstack/restic.env is deliberately never
  # auto-restored: the repository credentials come from the owner's password manager, by design.
  local host="$1" src dst
  HOST_RESTORED=""; HOST_MANUAL=""
  [ -d "$host" ] || return 0
  for rel in etc/ssh/sshd_config.d/01-bookstack.conf etc/sysctl.d/90-bookstack.conf etc/docker/daemon.json; do
    src="$host/$rel"; dst="/$rel"
    [ -f "$src" ] || continue
    cmp -s "$src" "$dst" 2>/dev/null && continue          # already identical: nothing to do
    mkdir -p "$(dirname "$dst")"
    install -o root -g root -m 0644 "$src" "$dst" 2>/dev/null || { HOST_MANUAL="$HOST_MANUAL\n  - $dst (copy failed)"; continue; }
    HOST_RESTORED="$HOST_RESTORED\n  - $dst"
  done
  # sysctl is safe to apply immediately and inert until it is.
  case "$HOST_RESTORED" in *sysctl.d*) sysctl --system >/dev/null 2>&1 || true;; esac
  # sshd: validate BEFORE reloading. A bad drop-in that gets reloaded can lock the owner out of
  # a machine they are in the middle of recovering, which is the worst possible moment.
  case "$HOST_RESTORED" in *sshd_config.d*)
    if sshd -t 2>/dev/null; then systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    else rm -f /etc/ssh/sshd_config.d/01-bookstack.conf
         HOST_MANUAL="$HOST_MANUAL\n  - /etc/ssh/sshd_config.d/01-bookstack.conf (restored copy failed sshd -t; REMOVED, re-run Install -> System)"
         HOST_RESTORED=$(printf '%s' "$HOST_RESTORED" | grep -v sshd_config.d || true)
    fi;;
  esac
  # daemon.json is NOT applied here: restarting dockerd would kill the stack in the middle of
  # its own restore. It takes effect at the next Docker restart, and until then a published
  # port can still bind 0.0.0.0 — which the self-test checks explicitly.
  case "$HOST_RESTORED" in *daemon.json*)
    HOST_MANUAL="$HOST_MANUAL\n  - /etc/docker/daemon.json was restored but Docker was NOT restarted (that would kill this restore). Run 'systemctl restart docker' at a quiet moment, or let the 04:30 reboot do it; until then Operations -> Self-test will flag any port published on a public address.";;
  esac
  return 0
}
restore_fits(){ # snapshot-id: in-place restore needs (restore size - what is already here) + 1 GB
  local need have cur
  need=$(restic_run stats "$1" --mode restore-size --json 2>/dev/null | json 'd["total_size"]') || need=""
  [ -n "$need" ] || { echo "(could not read the snapshot's restore size; continuing)"; return 0; }
  have=$(df -Pk "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}'); cur=$(du -sk "$STACK_DIR" 2>/dev/null | cut -f1)
  # restic restore never DELETES a file the snapshot does not contain, so whatever is already
  # here beyond the snapshot's own contents is NOT reclaimed. Credit at most the snapshot size,
  # or an old snapshot restored onto a library that has since grown passes and then fills the disk.
  local need_k=$(( need / 1024 )) cur_k=${cur:-0}
  [ "$cur_k" -gt "$need_k" ] && cur_k=$need_k
  local want_k=$(( need_k - cur_k + 1048576 ))
  RESTORE_NEED_GB=$(( (need / 1024 + 1048575) / 1048576 )); RESTORE_FREE_GB=$(( ${have:-0} / 1048576 ))
  [ "$want_k" -le "${have:-0}" ]
}
step_restore() {
  need DOMAIN || return 1
  [ -f "$(restic_env)" ] || { msg "No backup repository is configured on this machine yet: enter the SAME repository and password as the old server (for a home backup server choose Other and paste the rest:http://... address from your notes)."; write_restic_env || return 1; }
  pick_snapshot || return 1
  local mode
  mode=$(whiptail --title "Restore: what" --menu "Snapshot $SNAP_DESC" 14 80 2 \
    full   "Everything: configs, databases AND the library / audiobooks (new server)" \
    config "Config + databases only (library and its catalog stay; roll back a bad update)" 3>&1 1>&2 2>&3) || return 1
  if [ "$mode" = full ] && ! restore_fits "$SNAP_ID"; then
    msg "Not enough disk space: the snapshot needs about ${RESTORE_NEED_GB} GB and only ${RESTORE_FREE_GB} GB are free under $STACK_DIR (plus 1 GB headroom). Nothing was stopped or changed.\n\nUse a larger disk, or restore 'config + databases only'."; return 1
  fi
  yesno "Restore snapshot $SNAP_DESC ($([ "$mode" = full ] && echo 'everything' || echo 'config + databases only')) into $STACK_DIR?\n\nThis STOPS the whole stack and overwrites the restored files in place. Fresh PUBLIC_IP / TAILSCALE_IP / TZ from this server are kept.\n\nContinue?" || return 1
  yesno "Second confirmation: the files in $STACK_DIR are replaced by the backup. Really restore?" || return 1
  local pub ts tz bind p incl=() ow=()
  pub=$(envget PUBLIC_IP); ts=$(envget TAILSCALE_IP); tz=$(envget TZ); bind=$(envget BIND_IP)
  clear; echo "Stopping the stack..."; compose down >/dev/null 2>&1 || true
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && composeA down >/dev/null 2>&1 || true
  if [ "$mode" = full ]; then incl=(--include "$STACK_DIR")
  else for p in $RESTORE_CONFIG_PATHS; do incl+=(--include "$STACK_DIR/$p"); done; fi
  # restic >= 0.17 skips files that are already identical (a mostly intact library restores fast)
  restic restore --help 2>/dev/null | grep -q -- '--overwrite' && ow=(--overwrite if-changed)
  # in place, straight into the stopped $STACK_DIR: no temporary copy, so the disk never needs
  # room for the library twice
  echo "Restoring $SNAP_DESC into $STACK_DIR (this can take a while)..."
  if ! restic_run restore "$SNAP_ID" --target / "${incl[@]}" ${ow[@]+"${ow[@]}"}; then
    msg "restic restore failed part-way. The stack is stopped. Fix the cause (disk space? network?) and run Restore again: it resumes over the files already restored."; return 1
  fi
  [ -f "$ENV_FILE" ] || { msg "The snapshot did not contain $ENV_FILE: wrong snapshot or repository?"; return 1; }
  # Consistent DB copies (SQLite backup API, taken while the apps ran) replace the raw files.
  if [ -f "$STACK_DIR/.backup-snap/MANIFEST" ]; then
    while IFS=$'\t' read -r snap rel; do
      [ -n "$snap" ] && [ -f "$STACK_DIR/.backup-snap/$snap" ] || continue
      # "config + databases only" promises the library files stay. library/books/metadata.db is
      # the CATALOG of exactly those files: rolling it back would make every book imported since
      # the snapshot vanish from Calibre-Web, the portal, Kobo sync and every reader's shelf
      # while the files sit untouched on disk. Skip the whole library tree in that mode.
      if [ "$mode" = config ]; then
        case "$rel" in library/*) echo "  kept the live $rel (library files and their catalog stay in this mode)"; continue;; esac
      fi
      mkdir -p "$(dirname "$STACK_DIR/$rel")"
      cp -f "$STACK_DIR/.backup-snap/$snap" "$STACK_DIR/$rel"; rm -f "$STACK_DIR/$rel-wal" "$STACK_DIR/$rel-shm"
      echo "  restored DB $rel"
    done < "$STACK_DIR/.backup-snap/MANIFEST"
  fi
  [ -n "$pub" ] && envset PUBLIC_IP "$pub"; [ -n "$ts" ] && envset TAILSCALE_IP "$ts"; [ -n "$tz" ] && envset TZ "$tz"
  [ -n "$bind" ] && envset BIND_IP "$bind"
  # The snapshot carried the OLD server's copy of the code and templates; this checkout is the
  # version being deployed, so the code trees come from here (data and secrets stay restored).
  make_dirs; copy_code_trees; own_data_dirs; write_shelfmark_metadata_env || true
  # host files first: sshd/sysctl/daemon.json are regenerated by NOTHING below this line
  restore_host_files "$STACK_DIR/.backup-snap/host"
  render_caddy_all || { msg "Restored, but the Caddyfile could not be rendered; Caddy was NOT started. Fix it (Install -> Configure) then Install -> Deploy."; return 1; }
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && render_authelia_config
  if [ -n "$(envget CF_API_TOKEN)" ]; then echo "Pointing Cloudflare DNS at this server..."; step_cloudflare || echo "(Cloudflare step failed; run Install -> Cloudflare)"; fi
  install_backup_units
  # the snapshot carried the OLD server's post-reboot result; keeping it would put a stale
  # "N check(s) FAILED" banner on the main menu of a machine that has not rebooted yet
  rm -f "$STACK_DIR/$POSTBOOT_LOG_REL"
  echo "Starting the stack..."; stack_up_all || { msg "Restore copied the files but the stack did not start: Operations -> Logs."; return 1; }
  # after the stack: fail2ban's Caddy jails need caddy/data/access.log (not in the backup)
  command -v fail2ban-client >/dev/null && { render_fail2ban || true; systemctl restart fail2ban || true; }
  install_disk_watch
  msg "Restored $SNAP_DESC and started.\n\nRun Operations -> Self-test now. Users' Kobo links, Audiobookshelf accounts and Authelia logins came back with the databases; the Tailscale IP of this server is new (monitor./dl.).\
${HOST_RESTORED:+\n\nHost files restored from the snapshot (nothing else regenerates these):$HOST_RESTORED}\
${HOST_MANUAL:+\n\nNEEDS YOU:$HOST_MANUAL}"
  offsite_checklist
}
# One file (or one folder) out of a snapshot, next to the stack instead of over it. Getting a
# single accidentally deleted book back used to mean a full in-place restore with the whole stack
# down; restic restores a path natively, so nothing has to stop and nothing live is overwritten.
step_restore_file() {
  [ -f "$(restic_env)" ] || { msg "No backup repository is configured on this machine (Install -> Backups)."; return 1; }
  pick_snapshot || return 1
  local p abs stage
  p=$(ask "Path to restore out of $SNAP_DESC — a full path, or one relative to $STACK_DIR, e.g.\n  library/books/Jane Doe/A Title/A Title.epub\nA folder works too (everything under it comes back):") || return 1
  [ -n "$p" ] || return 1
  case "$p" in /*) abs="$p";; *) abs="$STACK_DIR/$p";; esac
  case "$abs" in "$STACK_DIR"/*) ;; *) msg "'$abs' is outside $STACK_DIR, and $STACK_DIR is the only tree the snapshots contain. Nothing was restored."; return 1;; esac
  stage="$(dirname "$STACK_DIR")/bookstack-restored-$(date -u +%Y%m%d-%H%M%S)"
  yesno "Restore\n  $abs\nfrom snapshot $SNAP_DESC into\n  $stage\n\nNothing inside $STACK_DIR is touched and nothing is stopped: the file is written BESIDE the stack and you copy back what you want after looking at it.\n\nContinue?" || return 0
  mkdir -p "$stage" || { msg "Could not create $stage (disk full, or the parent is read-only?). Nothing was restored."; return 1; }
  clear; echo "Restoring $abs into $stage ..."
  if ! restic_run restore "$SNAP_ID" --target "$stage" --include "$abs"; then
    # rmdir only succeeds on an empty directory: a restore that died part-way leaves a partial
    # tree, and claiming it was cleaned up hides a copy of the library beside the stack.
    if rmdir "$stage" 2>/dev/null; then
      msg "restic could not restore from $SNAP_DESC (see the output above). Nothing was changed; $stage was removed again."
    else
      msg "restic could not restore from $SNAP_DESC (see the output above). Nothing in $STACK_DIR was changed, but a PARTIAL restore was left in\n  $stage\n\nLook at it, then delete it — it sits outside the backup and outside the disk watchdog."
    fi
    return 1
  fi
  # restic exits 0 when an --include matches nothing at all, so an empty target is the real answer
  if [ -z "$(find "$stage" -mindepth 1 -print -quit 2>/dev/null)" ]; then
    rmdir "$stage" 2>/dev/null || true
    msg "That snapshot contains nothing at\n  $abs\n\nrestic reports success even when the path matches no file, so nothing landed. Check the exact spelling (it is case-sensitive) and the snapshot date, then try again."; return 1
  fi
  msg "Restored into:\n  $stage$abs\n\nNothing in $STACK_DIR was touched and the stack kept running. Copy back what you need (files under library/ must end up owned by uid 1000: chown -R 1000:1000), then delete $stage — it sits beside the stack, so the nightly backup does not pick it up and the disk watchdog does not clean it either."
}

# ---------- 7. lock SSH ----------
step_lock_ssh() {
  local ts; ts=$(envget TAILSCALE_IP)
  tailscale status >/dev/null 2>&1 || { msg "Tailscale is not running. Not locking SSH."; return 1; }
  # The same guard Quick install applies before it offers this (bookstack.sh's step_quick).
  # Without it the prompt names 127.0.0.1 — the placeholder Configure writes before Tailscale is
  # set up — which is trivially "reachable", invites a yes, and closes port 22 for good.
  [ -n "$ts" ] && [ "$ts" != 127.0.0.1 ] \
    || { msg "TAILSCALE_IP is ${ts:-empty}, the placeholder Configure writes before Tailscale exists — not a tailnet address. Run Install -> Tailscale first; SSH stays public."; return 1; }
  ip_on_host "$ts" \
    || { msg "TAILSCALE_IP ($ts) is not an address on this host — tailscaled is not up on it, or a re-auth assigned a new one. Run Install -> Tailscale first; SSH stays public."; return 1; }
  yesno "This removes public SSH (port 22) and leaves it reachable only over Tailscale.\n\nConfirm you can ALREADY SSH to $ts (or 'tailscale ssh') from another machine before continuing." || return 1
  if ! ts_key_expiry_disabled; then
    yesno "$TS_EXPIRY_NOTE\n\nWith public SSH closed, an expired key locks you out of everything but the provider console.\n\nHave you disabled key expiry for THIS machine in the Tailscale admin console?" \
      || { msg "Do that first (admin console -> Machines -> ... -> Disable key expiry), then run this step again. SSH stays public for now."; return 1; }
  fi
  ufw --force delete allow 22/tcp >/dev/null 2>&1 || true
  envset SSH_LOCKED true      # System re-runs keep port 22 closed from now on
  msg "Public SSH closed. Use: ssh root@$(envget TAILSCALE_IP)\n\nYour VPS provider's web console remains a fallback. Re-running System keeps it closed; Security -> 'Reopen public SSH' undoes this from here."
}
# The reverse of Lock SSH, and the reason it exists: setup_firewall re-deletes the rule on every
# System run while SSH_LOCKED is true, so an admin whose Tailscale account is locked or whose node
# key expired had to reach the provider's serial console and hand-edit $ENV_FILE — the only
# file-edit instruction in the whole TUI, needed exactly when experimenting is hardest.
step_unlock_ssh() {
  if [ "$(envget SSH_LOCKED)" != true ]; then
    msg "Public SSH is not locked: SSH_LOCKED is '$(envget SSH_LOCKED)', so Install -> System already allows port 22.\n\nIf port 22 still does not answer, the cause is elsewhere: check 'ufw status' and your VPS provider's own firewall."
    return 0
  fi
  yesno "Reopen public SSH (port 22) to the internet?\n\nufw allows 22/tcp again and SSH_LOCKED is set to false, so re-running Install -> System keeps it open instead of closing it.\n\nKey-only authentication still applies: password logins stay OFF (the 01-bookstack.conf drop-in sets PasswordAuthentication no), so anyone reaching port 22 still needs your private key. fail2ban's sshd jail keeps watching it.\n\nReopen it?" || return 0
  ufw allow 22/tcp >/dev/null || { msg "ufw refused to open port 22, so nothing was changed. Check 'ufw status' on the server."; return 1; }
  envset SSH_LOCKED false \
    || { msg "Port 22 is open in ufw NOW, but $ENV_FILE could not be written (disk full or read-only?), so SSH_LOCKED is still true and the next Install -> System run will close it again. Free some space and run this step again."; return 1; }
  msg "Public SSH is open again: ufw allows 22/tcp and SSH_LOCKED is false, so System re-runs leave it open.\n\nKey-only authentication still applies — passwords are refused, your key is required. Run Security -> 'Lock SSH to Tailscale only' again once Tailscale works, and Operations -> Self-test to confirm the rest of the firewall is unchanged."
}

# ---------- Q. quick install ----------
step_quick() {
  yesno "Quick install runs, in order: System -> Tailscale -> Configure -> Cloudflare -> Deploy -> Backups -> Alerts -> fail2ban,
then helps you add the first user and offers to lock SSH to Tailscale. Each step still asks what it needs. You can stop at any prompt and resume from the Install menu later.

Before starting you need: a Cloudflare zone for your domain + an API token (Zone:Read, DNS:Edit, Zone Settings:Edit, Cache Rules:Edit, Firewall Services:Edit, SSL and Certificates:Edit), and a Tailscale account.

Start?" || return 0
  step_system || { msg "System step did not finish."; return 1; }
  if yesno "Set up Tailscale now? (needed for the admin tools and safe SSH; you will open a login URL)"; then step_tailscale || true; fi
  step_configure   || { msg "Configure did not finish. Resume from Install -> Configure."; return 1; }
  step_cloudflare  || { msg "Cloudflare step did not finish. Resume from Install -> Cloudflare."; return 1; }
  step_deploy      || { msg "Deploy did not finish. Resume from Install -> Deploy."; return 1; }   # includes Audiobookshelf setup
  if yesno "Configure encrypted nightly backups now?"; then step_backup || true; fi
  if yesno "Set up alerts now (phone notification when a backup fails or the disk fills)? Strongly recommended."; then step_alerts || true; fi
  step_fail2ban || true
  # L17: the strongest single protection for internet-facing logins, offered while it is cheap
  if [ "$(envget AUTHELIA_ENABLED)" != true ] && yesno "Put single sign-on with two-factor authentication (Authelia) in front of the public sites now? Recommended: a leaked family password alone then opens nothing.\n\n(Kobo, OPDS and KOReader keep working: devices bypass the gate.)"; then
    step_authelia || true
  fi
  while yesno "Add a user now? (creates an isolated library account + Kobo link)"; do step_user_add || break; done
  # only offered when Tailscale actually works; step_lock_ssh still asks for proof of access
  if tailscale status >/dev/null 2>&1 && ip_on_host "$(envget TAILSCALE_IP)" \
     && yesno "Lock SSH to Tailscale only? (recommended once you have logged in over Tailscale: ssh root@$(envget TAILSCALE_IP))"; then
    step_lock_ssh || true
  fi
  msg "Quick install complete. Suggested next: Library -> Mail (Send-to-Kindle), Security -> Authelia (SSO+2FA), Operations -> Self-test."
}

# ---------- U. users & devices ----------
users_json() { lib list 2>/dev/null; }
step_user_list() {
  portal_up || { msg "The portal is not running (Install -> Deploy first)."; return 1; }
  # %-formatting, not f-strings: this block runs on the HOST, and nesting the same quote inside
  # an f-string is python 3.12+ (PEP 701). Debian 12 ships 3.11 and raised a SyntaxError here.
  users_json | python3 -c '
import sys, json
u = json.load(sys.stdin)
row = "%-18s %-6s %-22s %s"
print(row % ("user", "role", "isolation", "kindle"))
for x in u:
    iso = "sees all" if x["is_admin"] else ("owner:" + x["name"] if x["isolated"] else "NOT ISOLATED")
    print(row % (x["name"], "admin" if x["is_admin"] else "user", iso, x.get("kindle_mail") or "-"))
' | whiptail --title "Library users" --textbox /dev/stdin 22 90
}
step_user_add() {
  portal_up || { msg "The portal is not running (Install -> Deploy first)."; return 1; }
  u=$(ask "Username (lowercase letters/digits . _ - ; this is also their owner tag):") ; [ -n "$u" ] || return 1
  # The dropbox watcher skips folders whose name starts with '.', so a name like '.kim' produced
  # a fully working account whose file intake was permanently dead (uploads confirmed, never
  # imported), and '..' would have resolved the dropbox to $STACK_DIR/library itself.
  valid_username "$u" || { msg "'$u' is not a valid username: 2-32 characters, starting with a lowercase letter or digit, then a-z 0-9 . _ - only. A name starting with a dot would never have its dropbox scanned. Nothing was created."; return 1; }
  # a REAL address (F49): Authelia sends 2FA enrolment / reset codes there, the portal its notices
  em=$(ask "$u's real e-mail address (2FA codes, password resets and portal notices go here; they can change it on their Devices page):" "") || return 1
  valid_email "$em" || { msg "'$em' is not an e-mail address. Nothing was created."; return 1; }
  pw=$(askpw2 "Password for $u:") || return 1
  role=""; yesno "Make $u an ADMIN? (sees every book and all applications; normal users see only their own)" && role="--admin"
  out=$(printf '%s' "$pw" | lib add-user "$u" --email "$em" --password-stdin $role 2>&1) || { msg "Could not create user:\n\n$out"; return 1; }
  install -d -o 1000 -g 1000 "$STACK_DIR/library/dropbox/$u"
  kobo=$(printf '%s' "$out" | json 'd.get("kobo_url","")')
  if [ "$(envget AUTHELIA_ENABLED)" = "true" ]; then authelia_add_user "$u" "$u" "$em" "$pw" && anote="Authelia SSO account created with the same password (enrol 2FA at first login)." || anote="Authelia user could NOT be added — use Security -> Authelia add user."; else anote="(Authelia is off; enable it under Security for SSO + 2FA.)"; fi
  if abs_ready; then
    if [ -n "$role" ]; then absnote="Audiobookshelf: admins use the ABS root account (Library -> Audiobookshelf)."
    elif printf '%s' "$pw" | absctl ensure-user "$u" --password-stdin >/dev/null 2>&1; then absnote="Audiobookshelf account created with the same password; sees only audiobooks tagged owner:$u."
    else absnote="Audiobookshelf account could NOT be created — run Users -> Repair after checking Library -> Audiobookshelf."; fi
  else absnote="(Audiobookshelf not set up yet: Library -> Audiobookshelf, then Users -> Repair.)"; fi
  km=$(ask "Kindle e-mail for $u (blank to skip; they can set it themselves under Devices):" "") || km=""
  [ -n "$km" ] && lib kindle "$u" "$km" >/dev/null 2>&1 || true
  big "User $u created" "Give $u:
  Portal:   https://request.$(envget DOMAIN)     login: $u / (the password you set)
  Library:  https://books.$(envget DOMAIN)       Audiobooks: https://audio.$(envget DOMAIN)

Isolation: $([ -n "$role" ] && echo "admin — sees everything" || echo "Allowed Tags = owner:$u — sees only their own books, on every device")
Kobo sync link (also shown to them under Devices in the portal):
  ${kobo:-<generate under Devices>}
Kindle: ${km:-not set (Devices page)}
Dropbox: $STACK_DIR/library/dropbox/$u  (anything placed here is tagged to $u and imported)
$anote
$absnote

Tell $u: change the password ONLY in the portal (Devices page). Calibre-Web's own profile
page (/me) writes just its own database — Audiobookshelf would keep the old password."
}
step_user_kindle() {
  u=$(ask "Username:") || return 1; [ -n "$u" ] || return 1
  km=$(ask "Kindle e-mail for $u (type  none  to clear the address; Cancel keeps it):" "") || return 1
  [ -n "$km" ] || { msg "Nothing changed (type 'none' to clear the address)."; return 1; }
  [ "$km" = none ] && km=""
  local from; from=$(envget SMTP_FROM)
  out=$(lib kindle "$u" "$km" 2>&1) && msg "Saved.\n\nRemind $u to add ${from:-the sender address} to Amazon's approved list." || msg "Failed:\n$out"
}
step_user_kobo() {
  u=$(ask "Username:"); [ -n "$u" ] || return 1
  if yesno "Show the CURRENT link (Yes) or REGENERATE it (No -> old link stops working)?"; then url=$(lib kobo-url "$u" 2>&1); else url=$(lib kobo-url "$u" --reset 2>&1); fi
  big "Kobo sync for $u" "On the Kobo, edit .kobo/Kobo/Kobo eReader.conf and under [OneStoreServices] set:

  api_endpoint=$url

then eject and tap Sync. Only $u's books arrive. (The same link is on their Devices page.)"
}
step_user_passwd() {
  u=$(ask "Username:"); [ -n "$u" ] || return 1
  pw=$(askpw2 "New password for $u:") || return 1
  out=$(printf '%s' "$pw" | lib passwd "$u" --password-stdin 2>&1) || { msg "Failed:\n$out"; return 1; }
  extra=""
  if abs_ready; then printf '%s' "$pw" | absctl ensure-user "$u" --password-stdin >/dev/null 2>&1 && extra="\nAudiobookshelf: same password (account created if it was missing)." || extra="\nAudiobookshelf: could not update (is it set up? Library -> Audiobookshelf)."; fi
  # blank display name / e-mail = keep what Authelia already has (2FA reset mails keep working)
  if [ "$(envget AUTHELIA_ENABLED)" = "true" ]; then authelia_add_user "$u" "" "" "$pw" >/dev/null 2>&1 && extra="$extra\nAuthelia: same password (stored e-mail kept)." || extra="$extra\nAuthelia: not updated (no Authelia login for $u yet? Security -> Authelia: add or reset a user)."; fi
  # J07/V02: Shelfmark's session cookie is signed and only checked against app.db at login, and
  # its signing key is persisted to config/.flask_secret — dropping that key is what ends the
  # sessions; the restart alone would not.
  local smnote
  if restart_shelfmark --end-sessions; then
    smnote="\n\nShelfmark was restarted with a NEW session key, so every Shelfmark session — including $u's — is dead and the new password is required there."
  else
    smnote="\n\nWARNING: Shelfmark could NOT be restarted, so it still holds its old session key: $u stays signed in there and could keep downloading. Operations -> Logs -> shelfmark, then run this reset again."
  fi
  msg "Password updated for $u (portal, Calibre-Web, Shelfmark).$extra$smnote\n\nOpen Calibre-Web and Audiobookshelf sessions on devices they are already signed in on may SURVIVE this reset until they expire — have $u sign out there (or restart those apps) if the reset was because of a lost or shared password.\n\nTell $u to change their password ONLY in the portal (https://request.$(envget DOMAIN) -> Devices). A change made on Calibre-Web's own profile page (/me) never reaches Audiobookshelf, so their audiobook login would silently keep the old password."
}
step_user_remove() {
  u=$(ask "Username to remove (their books and dropbox are kept):"); [ -n "$u" ] || return 1
  yesno "Remove login '$u' from the library, portal, Audiobookshelf and Authelia?\n\nTheir books stay in the library. Requests already being worked on are failed by the portal; requests still waiting for approval stay in the queue until you deny them there." || return 0
  out=$(lib remove-user "$u" 2>&1) || { msg "Failed:\n$out"; return 1; }
  # The confirmation above promises Audiobookshelf too, and an audiobook account nothing reports
  # on is an account that keeps working: `|| true` here used to swallow both a missing ABS_TOKEN
  # and a failed API call, and selftest.sh cannot catch it either (a leftover account for a
  # removed user is still tag-restricted, so its assertion passes). Say what really happened.
  local absnote
  if ! abs_ready; then
    absnote="\nAudiobookshelf has no API key here (Library -> Audiobookshelf), so no audiobook account was touched: if $u has one, delete it there under Settings -> Users."
  elif absctl remove-user "$u" >/dev/null 2>&1; then
    absnote="\nTheir Audiobookshelf account was removed too."
  else
    absnote="\nWARNING: their Audiobookshelf account could NOT be removed, so $u can still sign in to the audiobook app and reach every audiobook tagged owner:$u. Delete it in Audiobookshelf -> Settings -> Users (Operations -> Logs -> audiobookshelf shows why)."
  fi
  local anote=""
  if [ -f "$STACK_DIR/authelia/users_database.yml" ] && grep -q "^  $u:" "$STACK_DIR/authelia/users_database.yml"; then
    authelia_remove_user "$u" && anote="\nTheir Authelia login was removed too." || anote="\nCould NOT remove their Authelia login: delete '$u' from $STACK_DIR/authelia/users_database.yml."
  fi
  # J07/V02: without dropping the persisted signing key the removed user's SIGNED Shelfmark
  # cookie keeps working (Shelfmark reads CWA's app.db only at login, and a plain restart
  # re-reads the same key from config/.flask_secret), so they could still search and download.
  local smnote
  if restart_shelfmark --end-sessions; then
    smnote="\n\nShelfmark was restarted with a NEW session key, so any session $u still had open there is dead."
  else
    smnote="\n\nWARNING: Shelfmark could NOT be restarted, so it still holds its old session key and $u can keep searching and downloading there. Operations -> Logs -> shelfmark, then run this removal again."
  fi
  msg "Removed $u. Books tagged owner:$u remain in the library (admin sees them).$absnote$anote$smnote\n\nA browser tab already signed in to Calibre-Web or Audiobookshelf may keep working until that session expires; Security -> 'Rotate the portal session secret' ends every portal session at once if you need that now."
}
step_user_repair() {
  portal_up || { msg "The portal is not running."; return 1; }
  n=0; a=0
  for u in $(users_json | json '" ".join(x["name"] for x in d if not x["is_admin"])'); do
    lib isolate "$u" >/dev/null 2>&1 && n=$((n+1))
    abs_ready && absctl ensure-user "$u" >/dev/null 2>&1 && a=$((a+1))
  done
  apply_library_defaults || true
  msg "Re-applied owner-tag isolation to $n non-admin user(s); registration OFF, Kobo sync ON, format defaults confirmed.\nAudiobookshelf accounts aligned: $a $(abs_ready || echo '(ABS not set up yet: Library -> Audiobookshelf)')"
}
# A portal login lockout could be SEEN nowhere and cleared nowhere: it shows up only as a
# 'login_locked' row in the audit trail. The bare-IP key is the sharp edge — a family behind one
# NAT shares one address, so enough wrong passwords from anywhere in the house lock out everybody,
# the admin included, and the only cure was waiting LOCKOUT_SECONDS out.
step_lockout() {
  portal_up || { msg "The portal is not running (Install -> Deploy first), so its login lockouts cannot be read."; return 1; }
  local out txt ch u i what rc=0
  out=$(admin_cli lockout status 2>&1) || rc=$?
  [ "$rc" = 0 ] || { msg "Could not read the lockouts from the portal:\n\n$(cli_err "$out")"; return 1; }
  # %-formatting, not f-strings: this runs on the HOST and Debian 12 ships python 3.11 (PEP 701)
  txt=$(printf '%s' "$out" | python3 -c '
import sys, json, time
d = json.load(sys.stdin)
now = time.time()
users, ips = d.get("users") or [], d.get("ips") or []
if not users and not ips:
    print("Nobody is locked out right now.")
for x in users:
    print("user     %-16s from %-32s %3d min left" % (x.get("user") or "?", x.get("ip") or "?", max(0, x["until"] - now) // 60 + 1))
for x in ips:
    print("ADDRESS  %-53s %3d min left  (EVERY account from it)" % (x.get("ip") or "?", max(0, x["until"] - now) // 60 + 1))
' 2>/dev/null) || txt="(the portal answered something this version cannot read: $out)"
  big "Portal login lockouts" "$txt

A 'user' row locks that name from that one address. An 'ADDRESS' row locks the address itself,
so every account behind it is refused — that is the one that takes the whole household down.
Both expire on their own after LOCKOUT_SECONDS (Operations -> Advanced settings -> lockout).

Clearing a lockout only forgets the failed attempts; it does not change anyone's password."
  ch=$(whiptail --title "Clear a login lockout" --menu "Nothing is cleared until you pick one." 14 78 4 \
    U "Release one user (from every address)" \
    I "Release one address (and every account locked from it)" \
    A "Release EVERYTHING" \
    0 "Back" 3>&1 1>&2 2>&3) || return 0
  case "$ch" in
    U) u=$(ask "Username to release:") || return 0; [ -n "$u" ] || return 0
       out=$(admin_cli lockout clear --user "$u" 2>&1) || { msg "Could not clear the lockout for '$u':\n\n$(cli_err "$out")"; return 1; }
       what="user '$u'";;
    I) i=$(ask "Address to release (this releases every account locked from it):" "$(caller_ip)") || return 0
       [ -n "$i" ] || return 0
       valid_ip "$i" || { msg "'$i' does not look like an IP address. Nothing was cleared."; return 1; }
       out=$(admin_cli lockout clear --ip "$i" 2>&1) || { msg "Could not clear the lockout for $i:\n\n$(cli_err "$out")"; return 1; }
       what="address $i";;
    A) yesno "Release EVERY portal login lockout, for every user and every address?\n\nThe record of recent failed logins is deleted with them, so a brute-force attempt in progress starts counting from zero again. Prefer releasing just the address your family is behind." || return 0
       out=$(admin_cli lockout clear --all 2>&1) || { msg "Could not clear the lockouts:\n\n$(cli_err "$out")"; return 1; }
       what="everything";;
    *) return 0;;
  esac
  msg "Released $(printf '%s' "$out" | json 'd.get("cleared", 0)') lockout key(s) for $what.\n\nThey can sign in again immediately. If it happens repeatedly, raise LOCKOUT_FAILS / LOCKOUT_IP_FAILS under Operations -> Advanced settings — one household shares one address, so the per-address counter is reached much sooner than it looks."
}
menu_users() {
  while true; do
    ch=$(whiptail --title "Users & devices" --menu "Every user gets an isolated library account (owner tag) usable in the portal, Calibre-Web and Shelfmark, plus their own Kobo link and Kindle address." 22 86 10 \
      1 "List users (role, isolation, Kindle)" \
      2 "Add a user (account + isolation + Kobo link + Authelia if on)" \
      3 "Set a user's Kindle e-mail" \
      4 "Show / regenerate a user's Kobo sync link" \
      5 "Reset a user's password" \
      6 "Remove a user" \
      7 "Repair: re-apply isolation + secure defaults to everyone" \
      8 "How isolation works (guide)" \
      9 "Login lockouts: who is locked out, and release them" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      1) step_user_list || true;; 2) step_user_add || true;; 3) step_user_kindle || true;; 4) step_user_kobo || true;;
      5) step_user_passwd || true;; 6) step_user_remove || true;; 7) step_user_repair || true;; 8) step_isolation || true;;
      9) step_lockout || true;; 0) return 0;;
    esac
  done
}

# ---------- F. formats & conversion ----------
step_formats() {
  docker inspect -f '{{.State.Running}}' calibre-web 2>/dev/null | grep -q true || { msg "Calibre-Web is not running."; return 1; }
  cur=$(cwa_sql "SELECT auto_convert||'|'||auto_convert_target_format||'|'||auto_ingest_automerge||'|'||kindle_epub_fixer||'|'||IFNULL(auto_convert_retained_formats,'')||'|'||IFNULL(koreader_sync_enabled,0) FROM cwa_settings;" 2>/dev/null) || { msg "Could not read CWA settings."; return 1; }
  IFS='|' read -r _ _ c_merge _ c_keep c_ko <<< "$cur"
  # The conversion target is always EPUB: the one format every reader handles. Kobo devices get
  # EPUB too, not KEPUB: CWA v4.0.6 autodetects kepubify only at /opt/kepubify/kepubify-linux-
  # {64,32}bit (cps/config_sql.py) while the image installs it at /usr/bin/kepubify, so
  # config_kepubifypath is permanently empty and the conversion in cps/kobo.py never fires.
  # EPUB syncs to a Kobo and reads fine; the only loss is that the device records reading
  # position at chapter boundaries instead of paragraph-exact. Do NOT "fix" this by pointing
  # config_kepubifypath at the real binary: sync then converts inline, the writers collide with
  # the sync's reader and the sync dies with HTTP 500 part-way through the library while every
  # health check stays green. docs/DECISIONS-PENDING.md records the safe order (convert the
  # library first, with nobody syncing). Kindles take EPUB by mail. Other targets would break
  # Send-to-Kindle or the Kobo path.
  fmt=epub
  msg "Imported books are converted to EPUB (fixed: Kobo syncs EPUB, Kindle accepts EPUB by mail). Next: whether to convert at all, and which original formats to keep next to the EPUB."
  on=0; yesno "Convert on import? (No = files are imported as-is)" && on=1
  fix=0; yesno "Run CWA's Kindle EPUB fixer on IMPORT?\n\nRecommended: NO. The portal already applies the Kindle fixes (language, encoding) when it mails a book to a Kindle. On import the CWA fixer rewrites every archive and strips the owner tag from comics (CBZ), which then need manual tagging in CWA." && fix=1
  keep=$(ask "Original formats to KEEP alongside the EPUB (comma list, e.g. pdf,azw3; blank = EPUB only):" "$c_keep")
  merge=$(whiptail --title "Duplicate policy" --radiolist "What happens when a title already exists in the library.\nnew_record is REQUIRED for per-user isolation (each user keeps their own copy)." 14 76 3 \
        new_record "Keep as a separate book (per-user copies) — required" "$([ "$c_merge" = new_record ] && echo ON || echo OFF)" \
        overwrite  "Replace the existing file (breaks isolation)" "$([ "$c_merge" = overwrite ] && echo ON || echo OFF)" \
        ignore     "Skip the new file (breaks isolation)" "$([ "$c_merge" = ignore ] && echo ON || echo OFF)" 3>&1 1>&2 2>&3) || return 1
  keep=$(printf '%s' "$keep" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9,')
  ko=0; yesno "Enable KOReader progress sync (KOSync)? KOReader users then sync reading position via https://books.$(envget DOMAIN)/kosync with their library login.$([ "${c_ko:-0}" = 1 ] && echo ' (currently ON)')" && ko=1
  cwa_sql "UPDATE cwa_settings SET auto_convert=$on, auto_convert_target_format='$fmt', kindle_epub_fixer=$fix, auto_convert_retained_formats='$keep', auto_ingest_automerge='$merge';" \
    || { msg "Could not write CWA settings."; return 1; }
  cwa_sql "UPDATE cwa_settings SET koreader_sync_enabled=$ko;" >/dev/null 2>&1 || true
  bk=0; yesno "Keep CWA's own copies of every imported/converted/fixed file (cwa/config/processed_books)?\n\nOFF is the default: they double disk use and restic already backs up the library." && bk=1
  cwa_sql "UPDATE cwa_settings SET auto_backup_imports=$bk, auto_backup_conversions=$bk, auto_backup_epub_fixes=$bk;" >/dev/null 2>&1 || true
  envset KOSYNC_ENABLED "$([ "$ko" = 1 ] && echo true || echo false)"; restart_portal_ok || true
  msg "Saved. Applies to the next import.\n\nconvert=$on -> $fmt, keep=[$keep], kindle fixer=$fix, duplicates=$merge, KOReader sync=$ko\n\nUsers pick their own preferred DOWNLOAD format on the portal's Devices page; the library serves whichever formats exist.$([ "$ko" = 1 ] && echo ' KOReader instructions now appear on their Devices page.')"
}

# ---------- A. Audiobookshelf (root, API key, library, per-user isolation) ----------
step_abs_setup() {
  portal_up || { msg "The portal is not running (Install -> Deploy first)."; return 1; }
  docker inspect -f '{{.State.Running}}' audiobookshelf 2>/dev/null | grep -q true || { msg "Audiobookshelf is not running."; return 1; }
  if abs_ready; then
    yesno "Audiobookshelf is already set up (API key present). Re-run setup? (keeps existing users; needs the root password)" || return 0
  fi
  ru=$(envget ABS_ROOT_USER); ru=$(ask "Audiobookshelf root username:" "${ru:-root}") || return 1; [ -n "$ru" ] || ru=root
  rp=$(askpw2 "Audiobookshelf root password (created now if ABS is still uninitialised; otherwise the existing one):") || return 1
  clear; echo "Setting up Audiobookshelf (root, API key, library)..."
  out=$(printf '%s' "$rp" | absctl init --user "$ru" --password-stdin 2>&1) || { msg "Audiobookshelf setup failed:\n\n$out\n\nIf ABS was initialised in its web UI with a different root password, use that one."; return 1; }
  key=$(printf '%s' "$out" | json 'd["api_key"]'); [ -n "$key" ] || { msg "No API key returned:\n$out"; return 1; }
  envset ABS_TOKEN "$key"; envset ABS_ROOT_USER "$ru"
  restart_portal_ok || true; wait_for http://127.0.0.1:8090/healthz 45 || true
  n=0; for u in $(users_json | json '" ".join(x["name"] for x in d if not x["is_admin"])'); do absctl ensure-user "$u" >/dev/null 2>&1 && n=$((n+1)); done
  local libname; libname=$(envget ABS_LIBRARY_NAME)
  big "Audiobookshelf ready" "Root user: $ru   Library: ${libname:-Audiobooks} -> /audiobooks   API key stored in .env (ABS_TOKEN)

What is now automatic:
- every audiobook a user requests or uploads is placed in the library, scanned, and tagged
  owner:<user> in Audiobookshelf within a minute (the request's detail line shows it)
- new users (Users -> Add) get an Audiobookshelf account with the same password that can
  only see items tagged owner:<user>; Users -> Reset password keeps both in step
- existing users whose ABS account already existed were aligned now: $n
  (users created BEFORE this step get their ABS login when you run Users -> Reset password)

Users sign in to https://audio.$(envget DOMAIN) or the Audiobookshelf app with their library
username and password. You (admin) use the root account there."
}
# absctl has wrapped the scan API since Audiobookshelf was added, but no menu entry called it, so
# the only way to rescan was knowing the invocation by heart.
step_abs_scan() {
  portal_up || { msg "The portal is not running; the Audiobookshelf helper runs inside it (Install -> Deploy first)."; return 1; }
  abs_ready || { msg "Audiobookshelf has no API key yet, so nothing here can talk to it. Run Library -> Audiobookshelf first."; return 1; }
  yesno "Ask Audiobookshelf to rescan its library now?\n\nUse this when an audiobook was copied straight into $STACK_DIR/library/audiobooks by hand (scp/rsync) and has not appeared. Anything the portal imported — requests, uploads, dropboxes, Shelfmark — is scanned and tagged automatically and needs no rescan.\n\nAudiobookshelf reads every folder; on a large library that takes minutes, and it stays usable while it works.\n\nStart the rescan?" || return 0
  clear; echo "Asking Audiobookshelf to rescan..."
  local out
  out=$(absctl scan 2>&1) || { msg "The rescan could not be started:\n\n$out\n\nIs ABS_TOKEN still valid (Library -> Audiobookshelf re-runs setup)? Operations -> Logs -> audiobookshelf shows the other side."; return 1; }
  msg "Rescan started: $(printf '%s' "$out" | json 'd.get("note") or "accepted"')\n\nItems appear in Audiobookshelf as it works; watch it at https://audio.$(envget DOMAIN).\n\nA file you copied in by hand carries no owner:<user> tag, so only admins see it until you tag it in Audiobookshelf (Library -> 'How per-user isolation works')."
}

# ---------- M. mail (SMTP for Send-to-Kindle) ----------
step_mail() {
  h=$(ask "SMTP host (blank disables Send-to-Kindle from the portal):" "$(envget SMTP_HOST)")
  if [ -z "$h" ]; then envset SMTP_HOST ""; restart_portal_ok || true
    if [ "$(envget AUTHELIA_ENABLED)" = true ]; then render_authelia_config; composeA up -d authelia >/dev/null 2>&1 || true; fi
    msg "Portal mail disabled."; return 0; fi
  local dp; dp=$(envget SMTP_PORT)
  p=$(ask "SMTP port:" "${dp:-587}") || return 1; [ -n "$p" ] || p=587
  sec=$(whiptail --title "Security" --radiolist "Transport security" 12 60 3 \
        starttls "STARTTLS (port 587)" "$([ "$(envget SMTP_SECURITY)" != ssl ] && echo ON || echo OFF)" \
        ssl "SSL/TLS (port 465)" "$([ "$(envget SMTP_SECURITY)" = ssl ] && echo ON || echo OFF)" \
        none "none (local relay only)" OFF 3>&1 1>&2 2>&3) || return 1
  u=$(ask "SMTP username:" "$(envget SMTP_USER)") || return 1
  pw=$(askpw "SMTP password (blank keeps existing):") || return 1; [ -n "$pw" ] || pw="$(envget SMTP_PASS)"
  local df; df=$(envget SMTP_FROM)
  from=$(ask "From address (users must add THIS to Amazon's approved senders):" "${df:-$u}") || return 1; [ -n "$from" ] || from="$u"
  envset SMTP_HOST "$h"; envset SMTP_PORT "$p"; envset SMTP_SECURITY "$sec"; envset SMTP_USER "$u"; envset SMTP_PASS "$pw"; envset SMTP_FROM "$from"
  restart_portal_ok || true; wait_for http://127.0.0.1:8090/healthz 30 || true
  # Authelia mails enrolment / reset codes through the same SMTP (C9)
  if [ "$(envget AUTHELIA_ENABLED)" = true ]; then render_authelia_config; composeA up -d authelia >/dev/null 2>&1 || true; fi
  if t=$(ask "Send a test mail to (blank to skip):" "$(envget ADMIN_EMAIL)") && [ -n "$t" ]; then
    out=$(docker exec -i librarian python -m kindle test "$t" 2>&1) && msg "Mail works: $out\n\nAlso set the same SMTP in Calibre-Web (Admin -> Edit e-mail server settings) if you want its own Send-to-Kindle button; run Security -> Mail auth for SPF/DMARC." || msg "Test failed:\n$out"
  fi
}

# ---------- 11. per-user isolation (guide) ----------
step_isolation() {
  big "How per-user isolation works" \
"Every book carries a tag  owner:<username>  and every non-admin account is restricted
(Calibre-Web 'Allowed Tags') to its own tag. Visibility, OPDS, Kobo sync, Send-to-Kindle and
the portal's My books / download all obey that restriction, so a user only ever sees or
receives their own books. Admins have no restriction and see everything.

Everything that adds a book applies the tag automatically:
  - portal requests and uploads, per-user dropboxes, e-mail intake, the intake webhook
  - Shelfmark downloads (routed to the user's dropbox)
  - Ephemera downloads (routed to the configured owner's dropbox)
EPUB, PDF and CBZ carry the tag INSIDE the file and it is written before import; PDFs and
comics (CBZ/CBR/CB7) keep their own format, other ebooks are converted to EPUB afterwards.
MOBI, AZW3, FB2 and TXT can carry no tag at all: they import UNTAGGED and are listed under
'Imported without an owner tag' on the portal's /admin page until an admin adds owner:<user>
in Calibre-Web. Check that list: until then the book is invisible to the person who asked
for it.

Users & devices -> Add user sets the restriction; -> Repair re-applies it to everyone.
Keep the duplicate policy at new_record (Library -> Formats) so users' copies stay separate.

Audiobooks: Audiobookshelf has its own accounts. Tag items owner:<user> in ABS and give the
user access to that tag (Settings -> Users) for the same effect."
}

# ---------- 12. sources ----------
step_sources() {
  cur() { [ "$(envget "$1")" = "true" ] && echo ON || echo OFF; }
  sel=$(whiptail --title "Curated sources in the portal" --checklist \
"Space toggles. These are the catalogs users can request from in the portal — all free to\nredistribute. (Shelfmark's own sources are configured inside Shelfmark.)" 18 78 6 \
    GUTENBERG "Project Gutenberg (public domain ebooks)"      "$(cur SRC_GUTENBERG)" \
    STANDARD  "Standard Ebooks (polished; found via book pages)" "$(cur SRC_STANDARD)" \
    ARCHIVE   "Internet Archive (filtered collections)"       "$(cur SRC_ARCHIVE)" \
    LIBRIVOX  "LibriVox (public-domain audiobooks)"           "$(cur SRC_LIBRIVOX)" \
    3>&1 1>&2 2>&3) || return 1
  for k in GUTENBERG STANDARD ARCHIVE LIBRIVOX; do
    case "$sel" in *"\"$k\""*|*"$k"*) envset "SRC_$k" true;; *) envset "SRC_$k" false;; esac
  done
  cols=$(ask "Internet Archive collections to search (comma-separated):" "$(envget IA_COLLECTIONS)")
  [ -n "$cols" ] && envset IA_COLLECTIONS "$cols"
  if yesno "Require admin approval for non-admin requests?\n\nNo (recommended for a family) = every request is fulfilled immediately.\nYes = you approve each one first — portal requests AND Shelfmark downloads, both on the portal's Pending card."; then envset APPROVALS_REQUIRED true; else envset APPROVALS_REQUIRED false; fi
  local dq; dq=$(envget MAX_REQUESTS_PER_DAY)
  q=$(ask "Maximum requests per user per day (0 = unlimited; admins are never limited):" "${dq:-30}") || q="${dq:-30}"
  q=$(printf '%s' "$q" | tr -cd '0-9'); envset MAX_REQUESTS_PER_DAY "${q:-30}"
  yesno "Manage your OWN catalogs now (any number of OPDS feeds: Calibre, Calibre-Web, COPS, Kavita, Komga, BookLore, a library's feed)?\n\nThe portal's admin page can do the same." && { step_catalogs || true; }
  # Shelfmark reads the approval rule from its environment (REQUESTS_ENABLED): recreate it too
  ensure_shelfmark_service >/dev/null 2>&1 || true
  compose up -d shelfmark >/dev/null 2>&1 || true
  prune_shelfmark_placeholder
  if restart_portal; then msg "Sources updated and the portal restarted."
  else msg "Sources written to $ENV_FILE, but the portal could NOT be restarted, so it is still offering the OLD set of sources (Operations -> Logs -> librarian)."; return 1; fi
}

# ---------- your own OPDS catalogs (librarian/catalogs.py via admin_cli) ----------
step_catalogs() {
  local out rows ch id nm url us pw res
  while true; do
    out=$(admin_cli catalogs list 2>/dev/null) || { msg "The portal did not answer (is it running? Operations -> Logs -> librarian): $(cli_err "$out")"; return 1; }
    rows=$(printf '%s' "$out" | json '"\n".join("%-12s %-3s %-22s %s%s" % (r["id"], "on" if r["enabled"] else "off", r["name"][:22], r["url"][:60], " (.env)" if r["legacy"] else "") for r in d["rows"]) or "(none yet)"')
    ch=$(whiptail --title "Your catalogs" --menu "OPDS catalogs searched by the portal (search page, book pages, keep-looking):\n\n$rows" 24 96 5 \
      A "Add a catalog (it is tested first)" T "Test an address without saving" E "Turn one on / off" R "Remove one" 0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      A) id=$(ask "Short id (lowercase letters, digits, dashes; e.g. home):" "") || continue
         nm=$(ask "Name readers will see:" "") || continue
         url=$(ask "OPDS feed address. Put {q} where the search term goes if the server supports it,\ne.g. https://books.mine.tld/opds/search/{q}:" "") || continue
         us=$(ask "Login (blank if none):" "") || continue
         pw=""; [ -n "$us" ] && { pw=$(askpw "Password for $us:") || continue; }
         res=$(printf '%s\n' "$pw" | admin_cli catalogs add "$id" "$nm" "$url" --user "$us" --password-stdin 2>/dev/null)
         if printf '%s' "$res" | json 'd.get("ok")' | grep -q True; then msg "Catalog '$nm' added: $(printf '%s' "$res" | json 'd.get("detail","")')"
         elif yesno "Not added: $(cli_err "$res")\n\nSave it anyway (for a catalog that is down right now)?"; then
           res=$(printf '%s\n' "$pw" | admin_cli catalogs add "$id" "$nm" "$url" --user "$us" --password-stdin --force 2>/dev/null)
           msg "$(printf '%s' "$res" | json 'd.get("ok")' | grep -q True && echo "Saved." || echo "Still not saved: $(cli_err "$res")")"; fi;;
      T) url=$(ask "OPDS feed address to test:" "") || continue
         us=$(ask "Login (blank if none):" "") || continue
         pw=""; [ -n "$us" ] && { pw=$(askpw "Password:") || continue; }
         res=$(printf '%s\n' "$pw" | admin_cli catalogs test "$url" --user "$us" --password-stdin 2>/dev/null)
         msg "$(printf '%s' "$res" | json 'd.get("detail") or d.get("error")')";;
      E) id=$(ask "Id of the catalog to turn on/off:" "") || continue
         [[ "$id" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { msg "'$id' is not a catalog id."; continue; }
         if printf '%s' "$out" | json "[r for r in d['rows'] if r['id']=='$id'][0]['enabled']" | grep -q True; then res=$(admin_cli catalogs disable "$id" 2>/dev/null); else res=$(admin_cli catalogs enable "$id" 2>/dev/null); fi
         printf '%s' "$res" | json 'd.get("ok")' | grep -q True || msg "$(cli_err "$res")";;
      R) id=$(ask "Id of the catalog to remove:" "") || continue
         yesno "Remove catalog '$id'? Books already imported from it stay in the library." || continue
         res=$(admin_cli catalogs remove "$id" 2>/dev/null)
         printf '%s' "$res" | json 'd.get("ok")' | grep -q True && msg "Removed." || msg "$(cli_err "$res")";;
      0) return 0;;
    esac
  done
}
# ---------- keep looking: every reader's list ----------
step_wanted() {
  local out id
  out=$(admin_cli wanted list 2>/dev/null) || { msg "The portal did not answer: $(cli_err "$out")"; return 1; }
  printf '%s' "$out" | json '"\n".join("#%-4s %-12s %-10s %-34s %-18s %s" % (r["id"], r["owner"][:12], r["status"], (r["title"] or "")[:34], (r["author"] or "")[:18], (r["detail"] or "")[:70]) for r in d["rows"]) or "Nobody is waiting for a book."' \
    | whiptail --title "Keep looking (all readers)" --scrolltext --textbox /dev/stdin 24 120 || true
  id=$(ask "Cancel an entry? Its number (blank = no):" "") || return 0
  [ -n "$id" ] || return 0
  out=$(admin_cli wanted cancel "${id#\#}" 2>/dev/null)
  printf '%s' "$out" | json 'd.get("ok")' | grep -q True && msg "Entry $id cancelled." || msg "$(cli_err "$out")"
}

# ---------- 13. mail auth (SPF/DMARC) ----------
step_mailauth() {
  need DOMAIN CF_API_TOKEN || return 1
  cf_zone || { msg "Zone not found."; return 1; }
  inc=$(ask "Your SMTP provider's SPF include (e.g. spf.brevo.com, spf.mailjet.com, _spf.google.com).\nLeave blank to skip SPF:" "")
  txt_upsert() { # name content
    local id body
    id=$(cf GET "/zones/$ZONE/dns_records?type=TXT&name=$1" | jq -r --arg c "$2" '.result[] | select(.content|test($c;"i")) | .id' | head -1)
    body=$(jq -nc --arg n "$1" --arg c "$2" '{type:"TXT",name:$n,content:$c,ttl:1}')
    if [ -n "$id" ]; then cf PUT "/zones/$ZONE/dns_records/$id" --data "$body" >/dev/null
    else cf POST "/zones/$ZONE/dns_records" --data "$body" >/dev/null; fi
  }
  [ -n "$inc" ] && txt_upsert "$DOMAIN" "v=spf1 include:$inc ~all"
  txt_upsert "_dmarc.$DOMAIN" "v=DMARC1; p=quarantine; rua=mailto:$(envget ADMIN_EMAIL); fo=1"
  msg "SPF/DMARC written.\n\nDKIM is provider-specific: in your SMTP provider (Brevo/Mailjet/etc.)\nopen their DKIM page and add the CNAME/TXT record they give you to Cloudflare DNS.\nWithout DKIM, Amazon may drop Send-to-Kindle mail."
}

# ---------- 14. fail2ban ----------
render_fail2ban() { # writes $ETC/fail2ban/{jail.local,filter.d/caddy-*.conf}; returns 1 if the Caddy jails are off
  local zone="" on=false log="$STACK_DIR/caddy/data/access.log"
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone 2>/dev/null; then zone="$ZONE"; on=true; fi
  # fail2ban refuses to start when a jail's log file is missing, and the access log is not in the
  # backup: after a restore that took the SSH jail down with it. The file always exists now.
  mkdir -p "$(dirname "$log")"; [ -e "$log" ] || { touch "$log"; chown 1000:1000 "$log"; }
  install -d "$ETC/fail2ban/filter.d"
  sed -e "s|@@CADDY_JAIL@@|$on|" -e "s|@@CF_API_TOKEN@@|$(envget CF_API_TOKEN)|" -e "s|@@CF_ZONE@@|$zone|" -e "s|@@CADDY_LOG@@|$log|" \
      "$STACK_DIR/configs/fail2ban/jail.local" > "$ETC/fail2ban/jail.local"
  chmod 600 "$ETC/fail2ban/jail.local"
  cp -f "$STACK_DIR/configs/fail2ban/caddy-device-auth.conf" "$ETC/fail2ban/filter.d/"
  # Host-scoped jails carry the domain in their filter (regex-escaped: a dot in a hostname must
  # not match any character). caddy-abs-login is scoped to audio.<domain>; caddy-auth is scoped
  # to the non-audio hosts, so an Audiobookshelf app with a stale password cannot trip the
  # stricter 2 h jail that locks the reader out of every site at once.
  local dom rep flt
  dom=$(envget DOMAIN); dom=$(printf '%s' "$dom" | sed 's/[.[\*^$]/\\&/g')   # regex-escape
  rep=$(printf '%s' "${dom:-[^\"]+}" | sed 's/[\&|]/\\&/g')                  # then sed-RHS-escape
  for flt in caddy-auth caddy-abs-login; do
    sed -e "s|@@DOMAIN@@|$rep|g" "$STACK_DIR/configs/fail2ban/$flt.conf" > "$ETC/fail2ban/filter.d/$flt.conf"
  done
  [ "$on" = true ]
}
step_fail2ban() {
  # python3-systemd: the sshd jail reads the journal (backend = systemd). Debian 13 writes no
  # /var/log/auth.log, and a minimal image may skip fail2ban's recommended packages.
  apt-get -y -qq install fail2ban python3-systemd || { msg "Could not install fail2ban (apt)."; return 1; }
  if render_fail2ban; then note="Caddy jails ON: repeated failed logins (portal, Shelfmark, Authelia: 8 in 5 min) and Basic-auth guessing on the device paths /opds and /kosync (30 in 10 min) get the visitor's REAL IP banned at Cloudflare for 2 h (IP Access Rule on the zone).\nAudiobookshelf has no lockout of its own, so audio.$(envget DOMAIN) POST /login answering 401 is jailed separately: 10 in 10 min -> 1 h ban.\nThe API token must have Zone -> Firewall Services -> Edit; if bans fail, add it in Cloudflare and re-run this step."
  else note="Caddy jail OFF (no Cloudflare token/zone yet — run Configure + Cloudflare, then this step again)."; fi
  systemctl enable --now fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban || { msg "fail2ban did NOT start: journalctl -u fail2ban shows why."; return 1; }
  msg "fail2ban active: SSH brute-force jail (local firewall).\n\n$note\n\nCheck with: fail2ban-client status caddy-auth (also caddy-device-auth, caddy-abs-login).\nSecurity -> 'Bans: show and release an address' undoes a ban, at fail2ban AND at Cloudflare."
}
# Every jail bookstack installs. sshd bans in the local firewall; the caddy-* ones ban at
# Cloudflare, because web logins arrive through the edge and the local firewall never sees them.
F2B_JAILS="sshd caddy-auth caddy-abs-login caddy-device-auth"
f2b_banned(){ # <jail> -> the addresses it currently holds banned, one per line
  fail2ban-client status "$1" 2>/dev/null | sed -n 's/.*Banned IP list:[[:space:]]*//p' | tr ' \t' '\n\n' | grep -v '^$' || true
}
# The local unban does NOT remove the Cloudflare IP Access Rule the cloudflare-token action
# created, and that rule is what actually cuts the household off — at the edge, before the
# request ever reaches this server. Both halves or the ban is still in force.
cf_unban(){ # <ip> -> prints one sentence describing what happened at Cloudflare
  local ids id n=0
  [ -n "$(envget CF_API_TOKEN)" ] || { printf 'no Cloudflare token is configured, so no access rule was looked up'; return 1; }
  cf_zone 2>/dev/null || { printf 'the Cloudflare zone could not be read with the stored token, so its access rules were NOT touched'; return 1; }
  # cf() is `curl -fsS`: a 403 (token without Firewall Services -> Read), a rate limit or a
  # network blip all yield empty output. Reporting that as "no rule exists" is the worst
  # possible answer here — the household stays banned while the screen says it is released.
  local body
  body=$(cf GET "/zones/$ZONE/firewall/access_rules/rules?mode=block&configuration_target=ip&configuration_value=$1&match=all&per_page=50" 2>/dev/null) \
    || { printf 'the Cloudflare access rules could NOT be read (token permissions or network), so a block rule may still be in place'; return 1; }
  ids=$(printf '%s' "$body" | jq -r '.result[]?.id // empty' 2>/dev/null) || ids=""
  [ -n "$ids" ] || { printf 'Cloudflare held no block rule for that address'; return 0; }
  for id in $ids; do cf DELETE "/zones/$ZONE/firewall/access_rules/rules/$id" >/dev/null 2>&1 && n=$((n+1)); done
  if [ "$n" = 0 ]; then printf 'a Cloudflare block rule exists for that address but could NOT be deleted (the token needs Zone -> Firewall Services -> Edit)'; return 1; fi
  printf '%s Cloudflare block rule(s) for that address were deleted' "$n"
}
# The urgent-when-it-happens case: one reader mistypes their password eight times and the
# household's single NAT address is banned at Cloudflare for two hours — every family member, on
# every device, including the admin's own browser. Nothing anywhere released it.
step_unban() {
  command -v fail2ban-client >/dev/null 2>&1 \
    || { msg "fail2ban is not installed on this server, so nothing here is banned by it. Security -> fail2ban installs it."; return 1; }
  local j b list="" first="" ip cfnote local_note="" cleared="" rc=0
  for j in $F2B_JAILS; do
    b=$(f2b_banned "$j" | tr '\n' ' ')
    list="$list\n  $j: ${b:-(nothing)}"
    [ -n "$first" ] || first="${b%% *}"
  done
  big "Banned addresses" "What each jail holds right now:
$(printf '%b' "$list")

The caddy-* jails ban at CLOUDFLARE (an IP Access Rule on the zone), not in this server's
firewall, so the address is refused at the edge before it ever reaches here. sshd bans locally.

Your household shares ONE public address, so a single reader's typo locks out everybody.
Releasing an address below clears it in every jail AND deletes its Cloudflare rule."
  ip=$(ask "Address to release (it is cleared from every jail above and from Cloudflare):" "${first:-$(caller_ip)}") || return 0
  [ -n "$ip" ] || return 0
  valid_ip "$ip" || { msg "'$ip' does not look like an IP address. Nothing was released."; return 1; }
  yesno "Release $ip?\n\n  - fail2ban-client set <jail> unbanip $ip, for: $F2B_JAILS\n  - delete the Cloudflare IP Access Rule blocking $ip on zone $(envget DOMAIN)\n\nNothing else changes: the jails stay enabled and the address can be banned again by the next round of failures.\n\nRelease it?" || return 0
  clear; echo "Releasing $ip..."
  for j in $F2B_JAILS; do
    fail2ban-client set "$j" unbanip "$ip" >/dev/null 2>&1 && cleared="$cleared $j"
  done
  [ -n "$cleared" ] && local_note="Released locally in:$cleared." || local_note="fail2ban held no ban on $ip in any jail (it may have expired, or the ban is only at Cloudflare)."
  cfnote=$(cf_unban "$ip") || rc=1
  msg "$ip\n\n$local_note\n\nCloudflare: $cfnote.\n\nAsk them to try again now — the edge rule is gone immediately, no cache to wait for.$([ "$rc" != 0 ] && printf '\n\nSomething above did not fully succeed; check https://dash.cloudflare.com -> Security -> WAF -> Tools (IP Access Rules) for a leftover rule.')\n\nIf the household keeps locking itself out, the portal's own counters are the usual cause: Operations -> Advanced settings -> lockout, and Users & devices -> 'Login lockouts'."
  return $rc
}

# ---------- 15. monitoring (Uptime Kuma) ----------
# Kuma used to be started and then handed to the admin as a to-do list of seven monitors to type
# in by hand, so on a real install it watched nothing. Now monitoring/kuma_bootstrap.py configures
# it: admin account, the alert channels scripts/alert.sh already uses, a monitor per service and
# per enabled feature, push (dead-man's switch) monitors for the scheduled jobs, and a
# maintenance window over the nightly reboot. Re-run after every Deploy and feature toggle.
KUMA_JOBS="selftest disk metapush cfips backup canary"   # push monitors; each job has KUMA_PUSH_<JOB>
ensure_kuma_secrets() {
  envdefault KUMA_USER "$(admin_user)" || return 1
  # alphanumeric: it goes through .env, JSON and a whiptail box, and 24 random characters from
  # three classes is far past Kuma's own strength check
  [ -n "$(envget KUMA_PASS)" ] || envset KUMA_PASS "$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-24)" || return 1
  local j; for j in $KUMA_JOBS; do
    envdefault "KUMA_PUSH_$(printf '%s' "$j" | tr '[:lower:]' '[:upper:]')" "$(openssl rand -hex 16)" || return 1
  done
}
kuma_reboot_time() { # the unattended-upgrades reboot time step_system configured, else 04:30
  local t; t=$(grep -hoE '^Unattended-Upgrade::Automatic-Reboot-Time +"[0-9]{1,2}:[0-9]{2}"' \
    "$ETC/apt/apt.conf.d/50unattended-upgrades" 2>/dev/null | grep -oE '[0-9]{1,2}:[0-9]{2}' | tail -1) || t=""
  printf '%s' "${t:-04:30}"      # (no match is grep exit 1: under pipefail + errexit that ended Deploy)
}
kuma_config() { # the bootstrap's JSON input on stdout — secrets included, so only ever piped
  local bind; bind=$(envget BIND_IP); bind="${bind:-$(envget PUBLIC_IP)}"
  # a push monitor only for a job that is actually scheduled here: an unscheduled job would
  # read as "missed its heartbeat" forever
  local ps="" pd="" pm="" pc="" pb="" pk=""
  [ -f "$ETC/systemd/system/bookstack-selftest.timer" ] && ps=$(envget KUMA_PUSH_SELFTEST)
  [ -f "$ETC/cron.d/bookstack-disk" ] && pd=$(envget KUMA_PUSH_DISK)
  [ -f "$ETC/cron.d/bookstack-metapush" ] && pm=$(envget KUMA_PUSH_METAPUSH)
  [ -f "$ETC/cron.d/bookstack-cfips" ] && pc=$(envget KUMA_PUSH_CFIPS)
  [ -f "$(restic_env)" ] && pb=$(envget KUMA_PUSH_BACKUP)
  [ -f "$ETC/systemd/system/bookstack-canary.timer" ] && pk=$(envget KUMA_PUSH_CANARY)
  KC_USER="$(envget KUMA_USER)" KC_PASS="$(envget KUMA_PASS)" KC_DOMAIN="$(envget DOMAIN)" KC_BIND="$bind" \
  KC_TOR="$(envget TORRENTS_ENABLED)" KC_EPH="$(envget EPHEMERA_ENABLED)" KC_AUTH="$(envget AUTHELIA_ENABLED)" \
  KC_FS="$(solver_on && echo true)" KC_REBOOT="$(kuma_reboot_time)" KC_SMA="$(envget SHELFMARK_AUTH_METHOD)" \
  KC_PS="$ps" KC_PD="$pd" KC_PM="$pm" KC_PC="$pc" KC_PB="$pb" KC_PK="$pk" \
  KC_HOOK="$(envget NOTIFY_WEBHOOK)" KC_FMT="$(envget NOTIFY_WEBHOOK_FORMAT)" KC_TO="$(envget ADMIN_EMAIL)" \
  KC_SH="$(envget SMTP_HOST)" KC_SP="$(envget SMTP_PORT)" KC_SS="$(envget SMTP_SECURITY)" \
  KC_SU="$(envget SMTP_USER)" KC_SW="$(envget SMTP_PASS)" KC_SF="$(envget SMTP_FROM)" \
  python3 -c '
import json, os
e = lambda k: os.environ.get(k, "")
on = lambda k: e(k) == "true"
smtp = {"host": e("KC_SH"), "port": e("KC_SP") or "587", "security": e("KC_SS") or "starttls",
        "user": e("KC_SU"), "password": e("KC_SW"), "from": e("KC_SF")} if e("KC_SH") else None
print(json.dumps({"url": "http://127.0.0.1:3001", "user": e("KC_USER"), "password": e("KC_PASS"),
  "domain": e("KC_DOMAIN"), "bind_ip": e("KC_BIND"), "reboot_time": e("KC_REBOOT"),
  "shelfmark_auth": e("KC_SMA") or "cwa",
  "features": {"torrents": on("KC_TOR"), "ephemera": on("KC_EPH"), "authelia": on("KC_AUTH"), "flaresolverr": on("KC_FS")},
  "push": {k: e(v) for k, v in (("selftest", "KC_PS"), ("disk", "KC_PD"), ("metapush", "KC_PM"),
                                ("cfips", "KC_PC"), ("backup", "KC_PB"), ("canary", "KC_PK")) if e(v)},
  "notify": {"webhook": e("KC_HOOK"), "format": e("KC_FMT") or "auto", "to": e("KC_TO"), "smtp": smtp}}))'
}
# setup_monitoring -> 0 configured | 1 failed | 2 Kuma has an account that is not ours.
# Never prompts (Deploy calls it); sets MON_NOTE to one line saying what happened.
setup_monitoring() {
  MON_NOTE=""
  local d out rc=0; d=$(envget DOMAIN)
  ensure_kuma_secrets || { MON_NOTE="could not write the Kuma credentials to $ENV_FILE (disk full?)"; return 1; }
  compose up -d uptime-kuma >/dev/null 2>&1 || { MON_NOTE="Uptime Kuma did not start (Operations -> Logs -> uptime-kuma)"; return 1; }
  wait_for http://127.0.0.1:3001 90 || { MON_NOTE="Uptime Kuma is not answering on 127.0.0.1:3001 (Operations -> Logs -> uptime-kuma), so it was not configured; run Operations -> Monitoring once it is up"; return 1; }
  docker image inspect bookstack/kuma-bootstrap:local >/dev/null 2>&1 || compose build kuma-bootstrap >/dev/null 2>&1 \
    || { MON_NOTE="the kuma-bootstrap image could not be built (compose build kuma-bootstrap)"; return 1; }
  out=$(kuma_config | compose run --rm -T kuma-bootstrap 2>/dev/null | tail -1) || rc=$?
  local ok added upd del mons chans err code
  ok=$(printf '%s' "$out" | json 'd.get("ok")') || ok=""
  if [ "$ok" != True ]; then
    err=$(printf '%s' "$out" | json 'd.get("error") or ""') || err=""
    code=$(printf '%s' "$out" | json 'd.get("code") or ""') || code=""
    MON_NOTE="Kuma was NOT configured: ${err:-no answer from the bootstrap (exit $rc)}"
    [ "$code" = credentials ] && return 2
    return 1
  fi
  mons=$(printf '%s' "$out" | json 'd.get("monitors")')
  added=$(printf '%s' "$out" | json 'len(d.get("added") or [])')
  upd=$(printf '%s' "$out" | json 'len(d.get("updated") or [])')
  del=$(printf '%s' "$out" | json 'len(d.get("deleted") or [])')
  chans=$(printf '%s' "$out" | json '", ".join(n.replace("bookstack: ", "") for n in d.get("notifications") or []) or "NONE"')
  envset KUMA_BOOTSTRAP_AT "$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%S)" || true
  MON_NOTE="$mons monitors at https://monitor.$d (+$added ~$upd -$del this run), alerts via: $chans; login '$(envget KUMA_USER)', password under Operations -> Monitoring"
  [ "$chans" = NONE ] && MON_NOTE="$MON_NOTE. NO alert channel: Kuma can show problems but tell nobody (Install -> Alerts, then Operations -> Monitoring)"
  return 0
}
# after a feature toggle: add/remove that feature's monitor. Only once monitoring was set up at
# least once (otherwise Deploy / Operations -> Monitoring will do it), and never fatal.
monitoring_refresh() {
  [ -n "$(envget KUMA_BOOTSTRAP_AT)" ] || return 0
  setup_monitoring >/dev/null 2>&1 || true
}
step_monitoring() {
  local d rc=0 u p; d=$(envget DOMAIN)
  clear; echo "Configuring Uptime Kuma (monitors, alert channels, reboot window)..."
  setup_monitoring || rc=$?
  if [ "$rc" = 2 ]; then
    yesno "Uptime Kuma already has an admin account, and the credentials bookstack holds are not it (it was set up by hand, or its password was changed).\n\nEnter that account now so bookstack can manage the monitors? Your own monitors are never touched; only the ones bookstack creates." || { msg "Monitoring left as it is. $MON_NOTE"; return 1; }
    u=$(ask "Uptime Kuma username:" "$(envget KUMA_USER)") || return 1
    p=$(askpw "Uptime Kuma password for '$u':") || return 1
    [ -n "$u" ] && [ -n "$p" ] || { msg "Nothing entered; nothing changed."; return 1; }
    envset KUMA_USER "$u"; envset KUMA_PASS "$p"
    rc=0; setup_monitoring || rc=$?
  fi
  [ "$rc" = 0 ] || { msg "$MON_NOTE"; return 1; }
  # The one check Kuma cannot make from ON this server. Every self-test (hourly, and after each
  # reboot) GETs <url> when all checks pass and <url>/fail when not, so a healthchecks.io-style
  # check with a ~2 h period goes red both for a failing stack and for a box that is gone.
  local hp; hp=$(ask "Optional: external dead-man's-switch URL for the self-test (e.g. a free healthchecks.io check, period 1 hour, grace 1 hour). Every self-test pings it; silence means the whole server is down. Blank = none." "$(envget HEALTH_PING_URL)") || hp=$(envget HEALTH_PING_URL)
  case "$hp" in ""|https://*) envset HEALTH_PING_URL "$hp";; *) msg "'$hp' is not an https:// URL; HEALTH_PING_URL left as it was."; hp=$(envget HEALTH_PING_URL);; esac
  big "Monitoring" "Uptime Kuma: https://monitor.$d  (Tailscale only)
  user:     $(envget KUMA_USER)
  password: $(envget KUMA_PASS)
  (kept in $ENV_FILE as KUMA_USER / KUMA_PASS; change it in Kuma and enter it here again)

$MON_NOTE

What it watches, every minute unless noted: the portal, Calibre-Web, Audiobookshelf (still
initialised), Shelfmark (and that it still demands library logins), Caddy's public listener,
the public path through Cloudflare (every 5 min), plus each optional service that is on.
Dead-man's switches: the hourly self-test, the disk watchdog, the metadata push, the
Cloudflare IP refresh and the nightly backup each report in; silence past their schedule
is an alert. The nightly reboot is a maintenance window, not an alert.

Monitors you add yourself in Kuma are left alone. Bookstack's own are put back to spec on
every Deploy (and when a feature is switched on or off).

Kuma runs ON this server, so it cannot tell you the whole VPS is down.
External check (HEALTH_PING_URL, pinged by every self-test): ${hp:-NOT SET — run this entry again to add one}"
}

# ---------- 16. Cloudflare Access (guide) ----------
step_cfaccess() {
  big "Cloudflare Access — hosted SSO + 2FA alternative (free <= 50 users)" \
"This puts an identity check BEFORE the app's own login. All in the Cloudflare dashboard
(Zero Trust). Authelia (Security menu) is the self-hosted equivalent; pick one.

1. Zero Trust -> Settings -> Authentication -> add a login method
   (One-time PIN by email is the simplest; or Google/GitHub).
2. Zero Trust -> Access -> Applications -> Add -> Self-hosted:
      books.$(envget DOMAIN)  audio.$(envget DOMAIN)  request.$(envget DOMAIN)  shelf.$(envget DOMAIN)
3. Policy: Allow, with an Emails/Email-domain rule listing your users' emails.
4. Session duration to taste (e.g. 24h).

For Kobo sync and OPDS apps, add ONE more Access application for path
   books.$(envget DOMAIN)/kobo/*   (and /opds*)  with a 'Bypass' policy — devices cannot do SSO.
The per-book tag isolation still applies behind it."
}

# ---------- 17. restore test ----------
step_restore_test() {
  [ -f "$(restic_env)" ] || { msg "Set up backups first (Install -> Backups)."; return 1; }
  clear; "$STACK_DIR/scripts/restore-test.sh" || true
  echo; read -rp "Press Enter to continue..." _
}

# ---------- 18-20. Authelia ----------
render_authelia_config(){ # configuration.yml from the template; an SMTP notifier replaces the file one when mail is set (C9)
  local out="$STACK_DIR/authelia/configuration.yml"
  sed "s|@@DOMAIN@@|$(envget DOMAIN)|g" "$STACK_DIR/authelia/configuration.yml.template" > "$out.new" || return 1
  # the password never lands in the file: X_AUTHELIA_CONFIG_FILTERS=template expands
  # {{ env "BOOKSTACK_SMTP_PASS" }} (= SMTP_PASS) at start-up. (AUTHELIA_NOTIFIER_SMTP_PASSWORD is
  # deliberately unused: Authelia 4.39 refuses to start when it is set next to a filesystem notifier.)
  python3 - "$out.new" "$(envget SMTP_HOST)" "$(envget SMTP_PORT)" "$(envget SMTP_SECURITY)" "$(envget SMTP_USER)" "$(envget SMTP_FROM)" <<'PYN'
import sys
f, host, port, sec, user, frm = sys.argv[1:7]
s = open(f).read()
b, e = "# @NOTIFIER_BEGIN@", "# @NOTIFIER_END@"
sender = frm or user
if host and "@" in sender and b in s and e in s:
    scheme = {"ssl": "submissions", "none": "smtp"}.get(sec, "submission")
    port = port or {"ssl": "465", "none": "25"}.get(sec, "587")
    q = lambda v: "'" + v.replace("'", "''") + "'"
    block = ["notifier:", "  smtp:", "    address: " + q("%s://%s:%s" % (scheme, host, port)),
             "    sender: " + q(sender), "    subject: '[Library] {title}'"]
    if user:
        block.append("    username: " + q(user))
        # rendered by Authelia's template filter from the container env (never written to disk)
        block.append('    password: {{ env "BOOKSTACK_SMTP_PASS" | quote }}')
    if sec == "none":
        block.append("    disable_require_tls: true")
    i = s.index(b); j = s.index(e, i)
    start = s.index("\n", i) + 1            # line after the BEGIN marker
    stop = s.rfind("\n", 0, j) + 1          # start of the END marker line
    s = s[:start] + "\n".join(block) + "\n" + s[stop:]
open(f, "w").write(s)
PYN
  # L05: the OpenID Connect provider for Audiobookshelf, once its secrets exist (gate_sso_on)
  if [ -n "$(envget ABS_OIDC_SECRET)" ] && [ -s "$STACK_DIR/authelia/oidc-jwks.pem" ]; then
    python3 - "$out.new" "$(envget DOMAIN)" "$(envget ABS_OIDC_SECRET)" <<'PYO' || { rm -f "$out.new"; return 1; }
import sys, base64, hashlib, os
f, dom, secret = sys.argv[1:4]
ab64 = lambda b: base64.b64encode(b).decode().rstrip("=").replace("+", ".")
salt = os.urandom(16)
digest = "$pbkdf2-sha512$310000$%s$%s" % (ab64(salt), ab64(hashlib.pbkdf2_hmac("sha512", secret.encode(), salt, 310000, 64)))
s = open(f).read()
b, e = "# @OIDC_BEGIN@", "# @OIDC_END@"
block = f"""identity_providers:
  oidc:
    hmac_secret: {{{{ env "BOOKSTACK_OIDC_HMAC" | quote }}}}
    jwks:
      - key_id: 'bookstack'
        algorithm: 'RS256'
        use: 'sig'
        key: {{{{ secret "/config/oidc-jwks.pem" | mindent 10 "|" | msquote }}}}
    clients:
      - client_id: 'audiobookshelf'
        client_name: 'Audiobookshelf'
        client_secret: '{digest}'
        public: false
        authorization_policy: 'two_factor'
        consent_mode: 'implicit'
        redirect_uris:
          - 'https://audio.{dom}/auth/openid/callback'
          - 'https://audio.{dom}/auth/openid/mobile-redirect'
        scopes: ['openid', 'profile', 'email', 'groups']
        response_types: ['code']
        grant_types: ['authorization_code']
        token_endpoint_auth_method: 'client_secret_basic'
        id_token_signed_response_alg: 'RS256'
        userinfo_signed_response_alg: 'none'
"""
i = s.index(b); j = s.index(e, i)
s = s[:s.index("\n", i) + 1] + block + s[s.rfind("\n", 0, j) + 1:]
open(f, "w").write(s)
PYO
  fi
  mv "$out.new" "$out"; chown 1000:1000 "$out"
}
authelia_user_count(){ grep -cE '^  [A-Za-z0-9._-]+:[[:space:]]*$' "$STACK_DIR/authelia/users_database.yml" 2>/dev/null || true; }
authelia_healthy(){ wait_for http://127.0.0.1:9091/api/health "${1:-30}"; }   # 2 s per try
step_authelia() {
  need DOMAIN PUBLIC_IP || return 1
  yesno "Enable self-hosted SSO + 2FA (Authelia) in front of books / audio / request / shelf?\n\nAt least one Authelia user is created first, and the gate goes live only once Authelia answers its health check. e-reader (/kobo) and OPDS paths are bypassed so devices keep working.\n\nReversible instantly with 'Authelia: disable'. Proceed?" || return 1
  envdefault AUTHELIA_SESSION_SECRET "$(openssl rand -hex 32)"
  envdefault AUTHELIA_STORAGE_ENCRYPTION_KEY "$(openssl rand -hex 32)"
  envdefault AUTHELIA_JWT_SECRET "$(openssl rand -hex 32)"
  envdefault GATE_SECRET "$(openssl rand -hex 32)"      # L05: Caddy vouches for gated requests with it
  render_authelia_config || { msg "Could not render Authelia's configuration."; return 1; }
  [ -f "$STACK_DIR/authelia/users_database.yml" ] || echo "users: {}" > "$STACK_DIR/authelia/users_database.yml"
  chown -R 1000:1000 "$STACK_DIR/authelia"
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone; then cf_dns auth "$(envget PUBLIC_IP)" true || true; fi
  # users FIRST: Authelia 4.39 refuses to start with an empty user file ("users: non zero value
  # required", measured on the real server), and without one every visitor would meet a login
  # nobody can pass. Hashing runs in a throwaway container, so Authelia need not be up for it.
  local u pw em
  if [ "$(authelia_user_count)" = 0 ] || yesno "Create or refresh Authelia logins for the existing library users? (each gets a password you type; usernames stay identical)"; then
    for u in $(users_json | json '" ".join(x["name"] for x in d)'); do
      pw=$(askpw2 "Authelia password for $u (Cancel skips this user):") || continue
      em=$(ask "$u's real e-mail address (2FA enrolment / reset codes). Blank = keep the stored one:" "") || continue
      [ -z "$em" ] || valid_email "$em" || { msg "'$em' is not an e-mail address; $u skipped."; continue; }
      authelia_add_user "$u" "$u" "$em" "$pw" || msg "Could not add $u (a new Authelia login needs an e-mail address)."
    done
  fi
  if [ "$(authelia_user_count)" = 0 ]; then
    composeA stop authelia >/dev/null 2>&1 || true
    msg "No Authelia user exists, so the gate was NOT enabled (nobody could log in, and Authelia refuses to start without one). Run Security -> Authelia again and give at least one user a password and an e-mail address."; return 1
  fi
  clear; echo "Starting Authelia..."
  composeA up -d authelia || { msg "Authelia did not start (Operations -> Logs -> authelia). The gate was NOT enabled."; return 1; }
  if ! authelia_healthy 30; then
    composeA stop authelia >/dev/null 2>&1 || true
    msg "Authelia did not become healthy within 60 s (Operations -> Logs -> authelia). The gate was NOT enabled; the apps keep their own logins."; return 1
  fi
  envset AUTHELIA_ENABLED true
  if ! render_caddy_all || ! apply_caddy; then
    envset AUTHELIA_ENABLED false; render_caddyfile >/dev/null 2>&1 && apply_caddy >/dev/null 2>&1
    composeA stop authelia >/dev/null 2>&1 || true
    msg "The Authelia gate could NOT be put in front of the sites (see the error above). Caddy keeps the previous configuration and Authelia stays disabled."; return 1
  fi
  # The portal reads AUTHELIA_ENABLED at start-up, and that is what stops /admin from offering
  # "Add a user" — an account created there would have no Authelia login and could sign in
  # nowhere. Without this restart the guard written for exactly this case stays inert.
  local pnote=""
  restart_portal || pnote="\n\nNOTE: the portal could not be restarted, so it still asks for its own login behind the gate (Operations -> Logs -> librarian)."
  # L05: one login. Caddy needs BOOKSTACK_GATE_SECRET in its environment (a recreate, once),
  # Calibre-Web trusts Remote-User, and portal password changes reach Authelia's file.
  gate_sso_on || pnote="$pnote\n\nNOTE: single sign-on could not be switched on everywhere (see Operations -> Self-test); the apps still ask for their own login behind the gate."
  local mailnote="Enrolment and reset codes are e-mailed through your SMTP server (Library -> Mail)."
  [ -n "$(envget SMTP_HOST)" ] || mailnote="No SMTP is configured, so enrolment/reset codes are NOT e-mailed: they are written to $STACK_DIR/authelia/notification.txt on this server (read it with: cat $STACK_DIR/authelia/notification.txt). Set up Library -> Mail to e-mail them instead."
  monitoring_refresh
  msg "Authelia enabled and the gate is live.$pnote\n\nTEST NOW: open https://books.$(envget DOMAIN) — you should meet the Authelia login before the app.\n\nUsers log in at https://auth.$(envget DOMAIN) and enrol TOTP or a passkey on first login. $mailnote\n\nIf anything misbehaves, 'Authelia: disable' removes the gate immediately."
}
step_authelia_off() {
  envset AUTHELIA_ENABLED false
  gate_sso_off
  render_caddy_all || return 1
  apply_caddy || return 1
  composeA stop authelia >/dev/null 2>&1 || true
  # without this the portal keeps AUTHELIA_ENABLED=true and still shows a dead 'Authelia' link
  local pnote=""
  restart_portal || pnote="\nThe portal could not be restarted, so its /admin page still links to the (now stopped) Authelia — Operations -> Logs -> librarian."
  monitoring_refresh
  msg "Gate removed — apps are back to their own logins. Authelia container stopped.$pnote\nRe-enable any time (your users and secrets are kept)."
}
# ---------- L05: one login behind the gate ----------
caddy_has_gate_secret(){ [ -n "$(envget GATE_SECRET)" ] && docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' caddy 2>/dev/null | grep -qxF "BOOKSTACK_GATE_SECRET=$(envget GATE_SECRET)"; }
install_gate_sync_units(){
  local u="$ETC/systemd/system"; mkdir -p "$u"
  write_alert_template
  cat > "$u/bookstack-gate-sync.service" << UNIT
[Unit]
Description=Bookstack: portal password changes into the Authelia gate
OnFailure=bookstack-alert@gate-sync.service
[Service]
Type=oneshot
Environment=STACK_DIR=$STACK_DIR
ExecStart=/usr/bin/python3 $STACK_DIR/scripts/gate-sync.py
UNIT
  cat > "$u/bookstack-gate-sync.path" << UNIT
[Unit]
Description=Bookstack: run gate-sync when the portal queues a password
[Path]
PathModified=$STACK_DIR/librarian/state/gate-sync.flag
[Install]
WantedBy=paths.target
UNIT
  cat > "$u/bookstack-gate-sync.timer" << 'UNIT'
[Unit]
Description=Bookstack: gate-sync safety net
[Timer]
OnCalendar=*:0/10
[Install]
WantedBy=timers.target
UNIT
  install -o "$(envget PUID || echo 1000)" -g "$(envget PGID || echo 1000)" -m 644 /dev/null "$STACK_DIR/librarian/state/gate-sync.flag" 2>/dev/null \
    || touch "$STACK_DIR/librarian/state/gate-sync.flag"
  systemctl daemon-reload && systemctl enable --now bookstack-gate-sync.path bookstack-gate-sync.timer
}
remove_gate_sync_units(){
  local u="$ETC/systemd/system"
  [ -f "$u/bookstack-gate-sync.path" ] || return 0
  systemctl disable --now bookstack-gate-sync.path bookstack-gate-sync.timer >/dev/null 2>&1 || true
  rm -f "$u/bookstack-gate-sync.path" "$u/bookstack-gate-sync.timer" "$u/bookstack-gate-sync.service"
  systemctl daemon-reload >/dev/null 2>&1 || true
}
# Authelia's "admins" group mirrors who is an admin in Calibre-Web: Shelfmark (proxy mode) takes
# admin rights from that group and from nothing else.
authelia_sync_admin_groups(){
  local f="$STACK_DIR/authelia/users_database.yml" admins
  [ -f "$f" ] || return 0
  admins=$(users_json | json '" ".join(x["name"] for x in d if x.get("is_admin"))' 2>/dev/null) || return 1
  python3 - "$f" "$admins" <<'PYG' || return 1
import sys, re
f, admins = sys.argv[1], set(sys.argv[2].split())
s = open(f).read()
def fix(m):
    name, body = m.group(1), m.group(2)
    groups = re.findall(r"(?m)^      - (\S+)\s*$", body)
    want = [g for g in groups if g != "admins"] + (["admins"] if name in admins else [])
    if "users" not in want:
        want.insert(0, "users")
    body = re.sub(r"(?ms)^    groups:\n(?:      - .*\n?)*", "", body)
    return "  %s:\n%s    groups:\n%s" % (name, body if body.endswith("\n") or not body else body + "\n", "".join("      - %s\n" % g for g in want))
# anchored at line starts: a "\n  name:" pattern let one entry swallow the newline the next needed
s2 = re.sub(r"(?m)^  ([A-Za-z0-9._-]+):\n((?:    .*\n?)*)", fix, s)
if s2 != s:
    open(f, "w").write(s2)
PYG
  chown 1000:1000 "$f" 2>/dev/null || true
}
gate_sso_on(){
  local rc=0
  if running caddy && ! caddy_has_gate_secret; then compose up -d caddy >/dev/null 2>&1 || rc=1; fi
  docker exec librarian python -m cwa proxy-login on >/dev/null 2>&1 && compose restart calibre-web >/dev/null 2>&1 || rc=1
  # Shelfmark: header login; Authelia's admins group decides who administers it
  authelia_sync_admin_groups || rc=1
  envset SHELFMARK_AUTH_METHOD proxy
  # Audiobookshelf: OpenID Connect through Authelia (its only single sign-on)
  envdefault ABS_OIDC_SECRET "$(openssl rand -hex 32)"
  envdefault AUTHELIA_OIDC_HMAC "$(openssl rand -hex 32)"
  if [ ! -s "$STACK_DIR/authelia/oidc-jwks.pem" ]; then
    ( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$STACK_DIR/authelia/oidc-jwks.pem" 2>/dev/null ) || rc=1
    chown 1000:1000 "$STACK_DIR/authelia/oidc-jwks.pem" 2>/dev/null || true
  fi
  render_authelia_config && composeA up -d authelia >/dev/null 2>&1 && authelia_healthy 30 || rc=1
  compose up -d shelfmark librarian >/dev/null 2>&1 || rc=1        # new env: auth mode, OIDC secret
  wait_for http://127.0.0.1:8090/healthz 30 >/dev/null 2>&1 || true
  if [ -n "$(envget ABS_TOKEN)" ]; then absctl oidc on >/dev/null 2>&1 || rc=1; fi
  install_gate_sync_units || rc=1
  env STACK_DIR="$STACK_DIR" python3 "$STACK_DIR/scripts/gate-sync.py" >/dev/null 2>&1 || true
  return $rc
}
gate_sso_off(){
  docker exec librarian python -m cwa proxy-login off >/dev/null 2>&1 && compose restart calibre-web >/dev/null 2>&1 || true
  [ -n "$(envget ABS_TOKEN)" ] && { absctl oidc off >/dev/null 2>&1 || true; }
  envset SHELFMARK_AUTH_METHOD cwa
  compose up -d shelfmark librarian >/dev/null 2>&1 || true
  remove_gate_sync_units
}
authelia_add_user() { # name displayname email password (blank displayname/email = keep the stored ones; a NEW user needs an e-mail)
  local hash f
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  # The password reaches the container through the environment (`-e PW` passes the variable
  # through from docker's own env), never through the host's argv / `ps` / `docker inspect`.
  hash=$(PW="$4" docker run --rm -e PW "$(img IMG_AUTHELIA)" sh -c 'authelia crypto hash generate argon2 --password "$PW"' 2>/dev/null | awk -F': ' '/Digest|Hash/{print $2; exit}')
  [ -n "$hash" ] || return 1
  f="$STACK_DIR/authelia/users_database.yml"
  [ -f "$f" ] || echo "users: {}" > "$f"
  # Scalars are written JSON-quoted (valid YAML double-quoted strings): a quote or colon in a
  # display name or e-mail can no longer break the file and lock everyone out (Authelia fails closed).
  python3 - "$f" "$1" "$2" "$3" "$hash" <<'PYU' || return 1
import sys, re, json
f, u, dn, em, h = sys.argv[1:6]
s = open(f).read()
s = re.sub(r"^users: \{\}\s*$", "users:", s, flags=re.M)
if not re.search(r"^users:", s, re.M):
    s = s.rstrip("\n") + "\nusers:\n"
entry = re.search(r"\n  " + re.escape(u) + r":\n((?:    .*\n?)*)", s)
def old(key):
    if not entry:
        return ""
    m = re.search(r"^    " + key + r": (.*)$", entry.group(1), re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    try:
        return json.loads(v) if v.startswith('"') else v.strip("'")
    except ValueError:
        return v
dn = dn or old("displayname") or u
em = em or old("email")
if not em:
    sys.exit(3)          # a new login needs a real e-mail address (2FA codes); never invent one
s = re.sub(r"\n  " + re.escape(u) + r":\n(?:    .*\n?)*", "\n", s)   # drop an existing entry (password reset)
s = s.rstrip("\n") + "\n  %s:\n    displayname: %s\n    password: %s\n    email: %s\n    groups:\n      - users\n" % (
    u, json.dumps(dn), json.dumps(h), json.dumps(em))
with open(f, "w") as out:
    out.write(s)
PYU
  chown 1000:1000 "$f"
  authelia_sync_admin_groups >/dev/null 2>&1 || true     # admins group = Calibre-Web admins (L05)
  composeA restart authelia >/dev/null 2>&1 || true
}
# Renaming the key is the only way to carry an Authelia login across a rename: the argon2 hash
# cannot be recomputed without the password, so authelia_add_user could not be used here.
authelia_rename_user() { # old new (keeps the stored hash, e-mail, groups; 1 = nothing renamed)
  local f="$STACK_DIR/authelia/users_database.yml"
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] && [[ "$2" =~ ^[A-Za-z0-9._-]+$ ]] && [ -f "$f" ] || return 1
  python3 - "$f" "$1" "$2" <<'PYM' || return 1
import sys, re, json
f, old, new = sys.argv[1:4]
s = open(f).read()
if not re.search(r"^  " + re.escape(old) + r":\s*$", s, re.M):
    sys.exit(1)                      # no login under the old name: nothing to do
if re.search(r"^  " + re.escape(new) + r":\s*$", s, re.M):
    sys.exit(1)                      # the new name already has its own login: never merge two
def fix(m):
    body = m.group(1)
    # the display name is a human label ("Kim"), so it is only touched when it WAS the username
    body = re.sub(r"^    displayname: (\"?)" + re.escape(old) + r"\1[ \t]*$",
                  "    displayname: " + json.dumps(new), body, count=1, flags=re.M)
    return "\n  " + new + ":\n" + body
s = re.sub(r"\n  " + re.escape(old) + r":\n((?:    .*\n?)*)", fix, s, count=1)
with open(f, "w") as out:
    out.write(s)
PYM
  chown 1000:1000 "$f"
  composeA restart authelia >/dev/null 2>&1 || true
}
authelia_remove_user() { # name
  local f="$STACK_DIR/authelia/users_database.yml"
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] && [ -f "$f" ] || return 1
  python3 - "$f" "$1" <<'PYR' || return 1
import sys, re
f, u = sys.argv[1:3]
s = open(f).read()
s = re.sub(r"\n  " + re.escape(u) + r":\n(?:    .*\n?)*", "\n", s)
if not re.search(r"^  \S+:\s*$", s, re.M):
    s = re.sub(r"^users:\s*$", "users: {}", s, flags=re.M)
with open(f, "w") as out:
    out.write(s.rstrip("\n") + "\n")
PYR
  chown 1000:1000 "$f"
  composeA restart authelia >/dev/null 2>&1 || true
}
step_authelia_user() {
  u=$(ask "Authelia username (must match their library username):"); [ -n "$u" ] || return 1
  dn=$(ask "Display name:" "$u")
  em=$(ask "$u's real e-mail address (2FA enrolment and reset codes go here). Blank = keep the stored one:" "") || return 1
  [ -z "$em" ] || valid_email "$em" || { msg "'$em' is not an e-mail address."; return 1; }
  pw=$(askpw2 "Password:") || return 1
  clear; echo "Hashing password..."
  authelia_add_user "$u" "$dn" "$em" "$pw" && msg "User '$u' added/updated. They sign in at https://auth.$(envget DOMAIN) and enrol 2FA on first login." || msg "Could not add/update '$u' (password hashing failed, or a NEW login needs an e-mail address)."
}

# ---------- 21. Intake & dropboxes ----------
step_intake() {
  d=$(envget DOMAIN)
  while true; do
    ch=$(whiptail --title "Intake & acquisition" --menu "Automated, event-driven intake on legal sources." 20 78 8 \
      1 "Create a per-user dropbox folder" \
      2 "Intake webhook: enable / show token / disable ($([ -n "$(envget INTAKE_TOKEN)" ] && echo on || echo off))" \
      3 "Set a Gutenberg mirror (pull EPUBs from a local/rsync mirror)" \
      4 "Configure email-to-library (IMAP)" \
      5 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      1) u=$(ask "Username to create a dropbox for (match their library username):"); [ -n "$u" ] || continue
         # '.foo' is never scanned by the watcher and '..' would resolve to library/ itself
         valid_username "$u" || { msg "'$u' is not a valid library username (2-32 characters, starting with a lowercase letter or digit, then a-z 0-9 . _ -). A folder starting with a dot is never scanned. Nothing was created."; continue; }
         install -d -o 1000 -g 1000 "$STACK_DIR/library/dropbox/$u"
         msg "Dropbox ready: $STACK_DIR/library/dropbox/$u\n\nAnything dropped there (scp/rsync/Syncthing/WebDAV) is tagged owner:$u and ingested automatically." ;;
      2) step_intake_webhook || true ;;
      3) m=$(ask "Gutenberg mirror base URL (e.g. https://gutenberg.pglaf.org). Blank to clear:" "$(envget GUTENBERG_MIRROR)")
         envset GUTENBERG_MIRROR "$m"; restart_portal_ok || true
         msg "Gutenberg source will now pull EPUBs from: ${m:-<official site>}" ;;
      4) h=$(ask "IMAP host (blank to disable):" "$(envget IMAP_HOST)")
         if [ -n "$h" ]; then
           iu=$(ask "IMAP username:" "$(envget IMAP_USER)") || continue; ip=$(askpw "IMAP password (blank keeps existing):") || continue
           du=$(ask "Default owner if no plus-address match (blank = require plus-address):" "$(envget IMAP_DEFAULT_USER)")
           al=$(ask "Allowed sender addresses (comma list). Blank = a mail is only accepted from the target user's own e-mail address:" "$(envget IMAP_ALLOWED_SENDERS)")
           local ra=true
           yesno "Accept a mail only when the receiving mail server vouches for the sender (Authentication-Results: DMARC, DKIM or SPF pass)?\n\nYes (recommended) stops forged 'From' addresses. Answer No only for a local relay that adds no Authentication-Results header." || ra=false
           envset IMAP_HOST "$h"; envset IMAP_USER "$iu"; [ -n "$ip" ] && envset IMAP_PASS "$ip"; envset IMAP_DEFAULT_USER "$du"; envset IMAP_ALLOWED_SENDERS "$al"; envset IMAP_REQUIRE_AUTH "$ra"
           msg "Email-to-library on. Send books to  <mailbox>+username@...  from the user's own address$([ -n "$al" ] && echo " (or: $al)") and they land in that user's dropbox. Mail for unknown users or from other senders is dropped. Restarting portal."
         else
           envset IMAP_HOST ""; msg "Email-to-library disabled."
         fi
         restart_portal_ok || true ;;
      5) return 0 ;;
    esac
  done
}

step_intake_webhook() { # C11: off (empty token, the portal answers 404) until the admin turns it on here
  local d; d=$(envget DOMAIN)
  if [ -z "$(envget INTAKE_TOKEN)" ]; then
    yesno "The intake webhook is OFF (POST https://request.$d/intake answers 404).\n\nTurn it on? It lets automation you control (a script, an RSS/OPDS feed watcher) push a legal book URL into a user's library with a secret token." || return 0
    envset INTAKE_TOKEN "$(openssl rand -hex 24)"; restart_portal_ok || true
  elif yesno "The intake webhook is ON.\n\nYes = show the token and usage.\nNo = turn it OFF (the token stops working)."; then :
  else envset INTAKE_TOKEN ""; restart_portal_ok || true; msg "Intake webhook turned off."; return 0; fi
  msg "Intake webhook (for authorized legal-source automation):\n\n  POST https://request.$d/intake\n  Header:  X-Intake-Token: $(envget INTAKE_TOKEN)\n  JSON:    {\"user\":\"alice\",\"url\":\"https://.../book.epub\",\"kind\":\"ebook\"}\n\nIt pulls the exact URL you give and maps it to that user. Turn it off again from this menu."
}

# ---------- 21a. request queue & parked files ----------
# The portal's /status page shows the newest 200 rows and nothing else, so with approvals on a
# request could sit at "waiting for approval" forever once 200 newer rows existed: unreachable
# from the web console AND from here. This pages through the whole table instead of capping it.
REQ_PAGE=20
step_requests() {
  portal_up || { msg "The portal is not running (Install -> Deploy first), so the request queue cannot be read."; return 1; }
  local st out total rows=() items=() ch off=0 shown
  st=$(whiptail --title "Request queue" --menu "Which rows? /status in the browser only ever shows the newest 200 — everything older is reachable only here." 17 78 6 \
    pending   "Waiting for your approval" \
    error     "Failed downloads and imports" \
    needs-tag "Imported but not tagged yet" \
    done      "Completed" \
    all       "Everything" \
    0         "Back" 3>&1 1>&2 2>&3) || return 0
  [ "$st" = 0 ] && return 0
  [ "$st" = all ] && st=""
  while true; do
    out=$(admin_cli requests list ${st:+--status "$st"} --limit "$REQ_PAGE" --offset "$off" 2>&1) \
      || { msg "Could not read the request queue:\n\n$(cli_err "$out")"; return 1; }
    total=$(printf '%s' "$out" | json 'd.get("total", 0)') || total=0
    mapfile -t rows < <(printf '%s' "$out" | python3 -c '
import sys, json
for r in (json.load(sys.stdin).get("rows") or []):
    print(r["rid"])
    print("%-10s %-9s %-40s %s" % ((r.get("user") or "-")[:10], (r.get("status") or "-")[:9],
                                   (r.get("title") or "(no title)")[:40], (r.get("detail") or "")[:34]))
' 2>/dev/null)
    shown=$(( ${#rows[@]} / 2 ))
    if [ "$shown" = 0 ]; then
      msg "No ${st:-} requests at that point in the queue (${total:-0} in total)."
      [ "$off" = 0 ] && return 0
      off=0; continue
    fi
    items=(${rows[@]+"${rows[@]}"})
    [ "$off" -gt 0 ] && items+=(P "<-- previous $REQ_PAGE")
    [ $(( off + REQ_PAGE )) -lt "${total:-0}" ] && items+=(N "next $REQ_PAGE -->")
    items+=(0 "Back")
    ch=$(whiptail --title "Request queue: ${st:-everything}" --menu "Rows $((off+1))-$((off+shown)) of ${total:-0}. Pick one to retry or dismiss it." 22 96 12 "${items[@]}" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      0) return 0;;
      P) off=$(( off - REQ_PAGE )); [ "$off" -lt 0 ] && off=0; continue;;
      N) off=$(( off + REQ_PAGE )); continue;;
      *) step_request_row "$ch" || true;;
    esac
  done
}
step_request_row() { # <rid>
  local a out
  a=$(whiptail --title "Request #$1" --menu "What should happen to request #$1?" 14 76 3 \
    R "Retry it (fetch the source again)" \
    D "Dismiss it (take it off the queue for good)" \
    0 "Back" 3>&1 1>&2 2>&3) || return 0
  case "$a" in
    R) out=$(admin_cli requests retry "$1" 2>&1) || { msg "Could not retry #$1:\n\n$(cli_err "$out")"; return 1; }
       msg "#$1 is back in the queue. The portal picks it up within a minute; watch it on https://request.$(envget DOMAIN)/status.";;
    D) yesno "Dismiss request #$1?\n\nIt leaves the queue permanently. No file is deleted and the user is not notified — a request still waiting for approval is simply cancelled.\n\nDismiss it?" || return 0
       out=$(admin_cli requests dismiss "$1" 2>&1) || { msg "Could not dismiss #$1:\n\n$(cli_err "$out")"; return 1; }
       msg "#$1 dismissed.";;
  esac
}
# Files in dropbox/<user>/.failed had no console path at all: SSH plus mv/rm was the only route,
# so they accumulated forever, occupying disk and going into every nightly restic snapshot.
step_parked() {
  portal_up || { msg "The portal is not running (Install -> Deploy first), so the parked files cannot be listed."; return 1; }
  local out items=() ch
  while true; do
    out=$(admin_cli parked list 2>&1) || { msg "Could not list the parked files:\n\n$(cli_err "$out")"; return 1; }
    mapfile -t items < <(printf '%s' "$out" | python3 -c '
import sys, json
for r in (json.load(sys.stdin).get("rows") or []):
    print(r["token"])
    print("%-9s %-32s %8.1f MB  %s" % ((r.get("user") or "-")[:9], (r.get("name") or "?")[:32],
                                       (r.get("bytes") or 0) / 1048576.0,
                                       (r.get("reason") or "no reason recorded")[:36]))
' 2>/dev/null)
    if [ "${#items[@]}" = 0 ]; then
      msg "Nothing is parked: every $STACK_DIR/library/dropbox/<user>/.failed folder is empty.\n\nFiles land there when the importer cannot use them — a mixed or empty folder, an unreadable archive, a file past the size ceiling (Operations -> Advanced settings -> uploads)."
      return 0
    fi
    ch=$(whiptail --title "Parked files (dropbox/<user>/.failed)" --menu "Files the importer could not use. They stay here forever and go into every nightly backup until you deal with them." 22 96 12 "${items[@]}" 0 "Back" 3>&1 1>&2 2>&3) || return 0
    [ "$ch" = 0 ] && return 0
    step_parked_one "$ch" || true
  done
}
step_parked_one() { # <token>
  local a out
  a=$(whiptail --title "Parked file" --menu "What should happen to it?" 14 78 3 \
    R "Retry: move it back into the dropbox so the importer tries again" \
    D "Delete it from the server, permanently" \
    0 "Back" 3>&1 1>&2 2>&3) || return 0
  case "$a" in
    R) out=$(admin_cli parked retry "$1" 2>&1) || { msg "Could not move it back:\n\n$(cli_err "$out")"; return 1; }
       msg "Moved back to:\n  $(printf '%s' "$out" | json 'd.get("moved") or "the dropbox"')\n\nThe watcher picks it up within about 15 seconds. If it fails for the same reason it is parked again — the reason is on the list you came from.";;
    D) yesno "DELETE this parked file from the server?\n\nIt is removed from disk immediately and cannot be undone from here: only a restic snapshot taken BEFORE now still holds it (Operations -> 'Restore a single file').\n\nReally delete it?" || return 0
       out=$(admin_cli parked delete "$1" 2>&1) || { msg "Could not delete it:\n\n$(cli_err "$out")"; return 1; }
       msg "Deleted. The disk space is free again; the next nightly snapshot no longer carries it.";;
  esac
}

# ---------- T. torrents (qBittorrent, opt-in) ----------
qbt_seed_config(){ # default save path = the admin's dropbox (tagged + imported), partials in /downloads/incomplete
  local f="$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"
  mkdir -p "$(dirname "$f")"
  # L02: a real Web UI password, set before first start. The LSIO image otherwise prints a new
  # temporary one on every restart until someone sets it in the UI. Stored in .env (QBIT_PASS);
  # the config holds only its PBKDF2 hash, in qBittorrent's own format (proven on 5.2.3).
  [ -n "$(envget QBIT_PASS)" ] || envset QBIT_PASS "$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | cut -c1-20)" || return 1
  QBIT_PASS="$(envget QBIT_PASS)" python3 - "$f" "/dropbox/$(admin_user)" <<'PYQ' || return 1
import sys, os, re, hashlib, base64
f, save = sys.argv[1:3]
pw = os.environ["QBIT_PASS"].encode()
salt = os.urandom(16)
pbkdf2 = '"@ByteArray(%s:%s)"' % (base64.b64encode(salt).decode(),
                                   base64.b64encode(hashlib.pbkdf2_hmac("sha512", pw, salt, 100000, 64)).decode())
prefs = {"WebUI\\Username": "admin", "WebUI\\Password_PBKDF2": pbkdf2}
want = {"Session\\DefaultSavePath": save, "Session\\TempPath": "/downloads/incomplete", "Session\\TempPathEnabled": "true"}
lines = open(f).read().splitlines() if os.path.exists(f) else []
out, sec, done = [], None, set()
def flush():
    for k, v in want.items():
        if k not in done: out.append("%s=%s" % (k, v)); done.add(k)
for ln in lines:
    m = re.match(r"^\[(.+)\]\s*$", ln)
    if m:
        if sec == "BitTorrent": flush()
        sec = m.group(1)
    elif sec == "BitTorrent" and "=" in ln and ln.split("=", 1)[0] in want:
        k = ln.split("=", 1)[0]; ln = "%s=%s" % (k, want[k]); done.add(k)
    out.append(ln)
if sec == "BitTorrent": flush()
if len(done) < len(want):
    out += ["", "[BitTorrent]"]; flush()
# [Preferences]: the Web UI login, replaced in place or added
text = "\n".join(out)
body, psec, pdone = [], None, set()
for ln in text.splitlines():
    m = re.match(r"^\[(.+)\]\s*$", ln)
    if m:
        if psec == "Preferences":
            body += ["%s=%s" % (k, v) for k, v in prefs.items() if k not in pdone]; pdone |= set(prefs)
        psec = m.group(1)
    elif psec == "Preferences" and "=" in ln and ln.split("=", 1)[0] in prefs:
        k = ln.split("=", 1)[0]; ln = "%s=%s" % (k, prefs[k]); pdone.add(k)
    body.append(ln)
if psec == "Preferences":
    body += ["%s=%s" % (k, v) for k, v in prefs.items() if k not in pdone]; pdone |= set(prefs)
if len(pdone) < len(prefs):
    body += ["", "[Preferences]"] + ["%s=%s" % (k, v) for k, v in prefs.items()]
open(f, "w").write("\n".join(body).strip("\n") + "\n")
PYQ
  chown -R 1000:1000 "$STACK_DIR/qbt/config"
}
step_torrents() { # C5: qBittorrent runs only while enabled (compose profile), 6881 + dl. only then
  local d; d=$(envget DOMAIN)
  if torrents_on; then
    yesno "qBittorrent is ENABLED.\n\nDisable it? The container is stopped and removed, the peer port 6881 is closed and https://dl.$d stops answering. Its settings (qbt/config) are kept." || return 0
    compose stop qbittorrent >/dev/null 2>&1 || true; compose rm -f qbittorrent >/dev/null 2>&1 || true
    envset TORRENTS_ENABLED false
    torrent_port close
    local cnote="" pnote=""
    { render_caddy_all && apply_caddy; } || cnote="\n\nCaddy did not pick the change up, so https://dl.$d may still be configured there (Operations -> Logs -> caddy)."
    # the portal reads TORRENTS_ENABLED at start-up to decide whether /admin shows the link
    restart_portal || pnote="\nThe portal could not be restarted, so its /admin page still links to qBittorrent."
    monitoring_refresh
    msg "qBittorrent disabled and removed; port 6881 closed.$pnote$cnote"; return 0
  fi
  yesno "Enable qBittorrent (admin-only torrent client at https://dl.$d, Tailscale only)?\n\nIt opens peer port 6881 to the internet, which also shows this server's IP to every swarm it joins. Download only what you are allowed to.\n\nEnable?" || return 0
  envset TORRENTS_ENABLED true
  install -d -o 1000 -g 1000 "$STACK_DIR/qbt/config" "$STACK_DIR/qbt/config/qBittorrent" "$STACK_DIR/downloads/incomplete"
  running qbittorrent && compose stop qbittorrent >/dev/null 2>&1    # it rewrites its config on exit
  qbt_seed_config
  torrent_port open
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone; then cf_dns dl "$(envget TAILSCALE_IP)" false || true; fi
  # the success screen below hands out a URL and a password: say so when Caddy has no such site
  local cnote=""
  { render_caddy_all && apply_caddy; } || cnote="WARNING: Caddy did NOT pick up the dl. site (an invalid Caddyfile, or ADMIN_HASH is not set — Install -> Configure), so https://dl.$d will not answer yet. Operations -> Logs -> caddy.

"
  compose up -d qbittorrent || { msg "qBittorrent did not start (Operations -> Logs -> qbittorrent). It stays enabled; run this again after fixing it, or disable it."; return 1; }
  restart_portal || cnote="${cnote}NOTE: the portal could not be restarted, so its /admin page does not show the qBittorrent link yet.

"
  sleep 5
  monitoring_refresh
  local qpw; qpw=$(envget QBIT_PASS)
  big "qBittorrent enabled" "${cnote}Open https://dl.$d (Tailscale on; admin-gate password first).
Web UI login: admin / ${qpw:-<see: docker logs qbittorrent>}  (generated, kept in $ENV_FILE as QBIT_PASS; enabling torrents again resets the Web UI to it).

Default save path is /dropbox/$(admin_user) (your own library). So downloads land in the right
person's library (tagged owner:<user>), set up ONE category per family member, in qBittorrent:
right-click Categories -> Add category:
    Category: alice     Save path: /dropbox/alice
    Category: bob       Save path: /dropbox/bob
and pick the category when adding a torrent. Finished files are moved there, the portal tags
them owner:<user> and imports them (ebooks to the library, audiobooks to Audiobookshelf).
Incomplete data stays in /downloads/incomplete. Nothing else on the server is reachable from
the container. At 95 % disk the watchdog stops qBittorrent; it starts again below 80 %."
}

# ---------- 22. Shelfmark ----------
step_shelfmark() {
  d=$(envget DOMAIN)
  lang=$(ask "Default search language for Shelfmark (ISO code, e.g. en, hi, de):" "$(envget SHELFMARK_LANGUAGE)"); [ -n "$lang" ] || lang=en
  conc=$(ask "Simultaneous downloads (small VPS: 1; each download is a parallel process next to Calibre conversions):" "$(envget SHELFMARK_CONCURRENCY)"); conc=$(printf '%s' "$conc" | tr -cd '0-9'); [ -n "$conc" ] || conc=1
  envset SHELFMARK_LANGUAGE "$lang"; envset SHELFMARK_CONCURRENCY "$conc"
  envdefault SHELFMARK_TITLE "Library search"
  compose up -d shelfmark >/dev/null 2>&1 || { msg "Shelfmark did not restart (Operations -> Logs -> shelfmark). Settings were saved."; return 1; }
  prune_shelfmark_placeholder
  big "Shelfmark (CWA companion) — first-run checklist" \
"Shelfmark runs at https://shelf.$d (public, behind Cloudflare and, if enabled, Authelia).
Restarted with language=$lang, concurrency=$conc.

HOW IT FITS THIS STACK
- Login: the same library accounts. Admins are Shelfmark admins.
- Downloads land in library/dropbox/<username>/ and are tagged owner:<username> by the
  portal within ~15 s. Users see only their own downloads.
- Folders are fine: a folder of audio files = ONE audiobook (moved to Audiobookshelf and
  tagged owner:<user> automatically; the request shows 'tagging' until ABS confirms), a
  folder of ebooks = one import per book. Mixed or empty folders are parked in
  dropbox/<user>/.failed with a note saying why.

DO THIS ONCE, AS ADMIN, IN https://shelf.$d -> Settings:
1. Release sources: turn on ONLY the sources you are permitted to pull from
   (nothing is enabled until you choose).
2. Metadata provider: Open Library (default) or Hardcover/Google Books.
3. Formats: allow only epub, pdf and cbz for ebooks (everything else is converted or
   rejected on import anyway). Leave 'Destination' as set by compose (per-user dropbox).
4. Optional torrent sources (only with Library -> Torrents on): qBittorrent client URL http://qbittorrent:8080 with the
   Web UI credentials (127.0.0.1 is not reachable from inside the container).

Updates with Operations -> Update. Logs: Operations -> Logs -> shelfmark."
}

# ---------- 23/24. Ephemera ----------
step_ephemera() {
  need DOMAIN TAILSCALE_IP || return 1
  whiptail --title "Ephemera — read before enabling" --scrolltext --yesno \
"Ephemera = search + a request queue that auto-downloads a title once it appears.
It uses the shared FlareSolverr (headless Chromium) for protection challenges,
which is started with it. Measured: Ephemera ~60 MiB, FlareSolverr ~50 MiB idle
and ~500 MiB per open browser (fenced at 1 GiB) — it fits the 4 GB plan.

STATUS: the upstream project and its container image were REMOVED from GitHub in
early 2026. This builds the last release (v1.3.1, Nov 2025) from a community
re-upload pinned to an exact commit. It is unmaintained: no security fixes will
arrive. For that reason it is reachable ONLY over Tailscale behind the admin
password gate (https://ephemera.$(envget DOMAIN)), never publicly, and it has
no per-user accounts — everything it fetches is filed to ONE user's dropbox.

Shelfmark is the maintained tool for the same job. Enable Ephemera only if you
specifically want its request-and-wait queue or newznab mode.

Build takes several minutes (Node toolchain). Proceed?" 24 84 || return 1
  aa=$(ask "Archive base URL Ephemera should search (required by Ephemera, e.g. https://host.tld):" "$(envget EPHEMERA_AA_BASE_URL)")
  [ -n "$aa" ] || { msg "Ephemera needs a base URL. Not enabled."; return 1; }
  lg=$(ask "Alternative download source base URL (optional, blank to skip):" "$(envget EPHEMERA_LG_BASE_URL)")
  key=$(askpw "Archive API key (optional, blank to skip / keep existing):")
  own_default=$(envget EPHEMERA_OWNER); [ -n "$own_default" ] || own_default=$(admin_user)
  own=$(ask "Library username whose library receives Ephemera's downloads:" "$own_default"); [ -n "$own" ] || own="$own_default"
  valid_username "$own" || { msg "'$own' is not a valid library username. Not enabled."; return 1; }
  envset EPHEMERA_AA_BASE_URL "$aa"; envset EPHEMERA_LG_BASE_URL "$lg"
  [ -n "$key" ] && envset EPHEMERA_AA_API_KEY "$key"
  envset EPHEMERA_OWNER "$own"; envset EPHEMERA_ENABLED true
  install -d -o 1000 -g 1000 "$STACK_DIR/library/dropbox/$own" "$STACK_DIR/ephemera/data" "$STACK_DIR/ephemera/downloads"
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone; then cf_dns ephemera "$(envget TAILSCALE_IP)" false; fi
  clear; echo "Building Ephemera from the pinned source (several minutes)..."
  if composeE build ephemera && composeE up -d flaresolverr ephemera; then
    # the ephemera. vhost exists only while it is enabled; without it the URL below answers nothing
    local cnote=""
    { render_caddy_all && apply_caddy; } || cnote="\n\nWARNING: Caddy did NOT pick up the ephemera. site, so that URL will not answer yet (Operations -> Logs -> caddy)."
    # Ephemera only reaches FlareSolverr over the compose network; prove it from inside
    local fnote=""
    wait_for http://127.0.0.1:8191/health 60 >/dev/null 2>&1 || true
    docker exec ephemera wget -qO- -T 5 http://flaresolverr:8191/health >/dev/null 2>&1 \
      || fnote="\n\nWARNING: Ephemera cannot reach FlareSolverr (http://flaresolverr:8191): protected sources will fail. Operations -> Logs -> flaresolverr."
    monitoring_refresh
    msg "Ephemera is up at https://ephemera.$(envget DOMAIN) (Tailscale only; admin gate password).\n\nDownloads are filed to '$own' (owner:$own) via library/dropbox/$own.\nFlareSolverr is running for it (Operations -> FlareSolverr shares it with Shelfmark).\nDisable any time under Operations.$cnote$fnote"
  else
    envset EPHEMERA_ENABLED false
    msg "Ephemera build or start failed — left disabled. Check the output above (the pinned source must still be reachable on GitHub)."
  fi
}
step_ephemera_off() {
  composeE stop ephemera >/dev/null 2>&1 || true
  composeE rm -f ephemera >/dev/null 2>&1 || true
  envset EPHEMERA_ENABLED false
  # FlareSolverr is shared: it stays up while Shelfmark is set to use it
  local fsnote="FlareSolverr is kept running: Shelfmark uses it (Operations -> FlareSolverr)."
  if ! solver_on; then
    compose stop flaresolverr >/dev/null 2>&1 || true
    compose rm -f flaresolverr >/dev/null 2>&1 || true
    fsnote="FlareSolverr stopped too (nothing else uses it)."
  fi
  local cnote=""
  { render_caddy_all && apply_caddy; } || cnote="\n\nCaddy did not pick the change up, so https://ephemera.$(envget DOMAIN) may still be configured there (Operations -> Logs -> caddy)."
  monitoring_refresh
  msg "Ephemera stopped and removed. Its data (ephemera/) and settings are kept; re-enable any time.\n$fsnote$cnote"
}

# ---------- FlareSolverr (shared protection-challenge solver) ----------
# Shelfmark: its "external bypasser" (USING_EXTERNAL_BYPASSER, set from FLARESOLVERR_ENABLED in
# docker-compose.yml) sends challenge pages here instead of starting its own Chromium inside its
# 768 MiB fence. Ephemera: always uses it. The container runs while either one wants it.
step_flaresolverr() {
  local cur; cur=$(envget FLARESOLVERR_ENABLED)
  if [ "$cur" = true ]; then
    yesno "FlareSolverr is ON for Shelfmark.\n\nTurn it off? Shelfmark goes back to its built-in challenge solver (a Chromium inside its own container).$([ "$(envget EPHEMERA_ENABLED)" = true ] && printf '\n\nFlareSolverr itself keeps running: Ephemera needs it.')" || return 0
    envset FLARESOLVERR_ENABLED false
    # `up -d` (not restart): the container must be RECREATED to see the changed environment
    compose up -d shelfmark >/dev/null 2>&1 || { msg "Shelfmark could NOT be recreated, so it still points at FlareSolverr (Operations -> Logs -> shelfmark)."; return 1; }
    prune_shelfmark_placeholder
    if ! solver_on; then compose stop flaresolverr >/dev/null 2>&1 || true; compose rm -f flaresolverr >/dev/null 2>&1 || true; fi
    monitoring_refresh
    msg "Shelfmark uses its built-in solver again.$(solver_on && printf ' FlareSolverr keeps running for Ephemera.' || printf ' FlareSolverr stopped.')"
    return 0
  fi
  yesno "FlareSolverr solves the browser challenges some download sites put in front of their pages (a headless Chromium), for Shelfmark — and for Ephemera, which always uses it.\n\nMeasured on the pinned v3.5.2: ~50 MiB idle, ~450-500 MiB per page being solved, back down afterwards; fenced at 1 GiB, room for two at once. Loopback only; no web page of its own.\n\nWith it on, Shelfmark sends challenges here instead of running its own Chromium inside its 768 MiB container.\n\nTurn it on?" || return 0
  envset FLARESOLVERR_ENABLED true
  clear; echo "Starting FlareSolverr and pointing Shelfmark at it..."
  if ! compose up -d flaresolverr || ! wait_for http://127.0.0.1:8191/health 90; then
    envset FLARESOLVERR_ENABLED false
    compose up -d shelfmark >/dev/null 2>&1 || true
    msg "FlareSolverr did not come up on 127.0.0.1:8191, so Shelfmark was left on its built-in solver (Operations -> Logs -> flaresolverr)."
    return 1
  fi
  compose up -d shelfmark >/dev/null 2>&1 || { msg "FlareSolverr is up, but Shelfmark could NOT be recreated to use it (Operations -> Logs -> shelfmark)."; return 1; }
  prune_shelfmark_placeholder
  # what matters is Shelfmark reaching it by name over the compose network, not the loopback port
  local reach=""
  for _ in $(seq 1 20); do
    docker exec shelfmark curl -fs -m 5 http://flaresolverr:8191/health >/dev/null 2>&1 && { reach=ok; break; }
    sleep 3
  done
  monitoring_refresh
  [ "$reach" = ok ] || { msg "FlareSolverr is running, but Shelfmark cannot reach http://flaresolverr:8191 from inside its container, so protected sources will fail. Operations -> Logs -> shelfmark / flaresolverr."; return 1; }
  msg "FlareSolverr is on. Shelfmark now sends protection challenges to it (confirmed from inside the Shelfmark container). Its Settings page shows the external bypasser as set by the deployment; turn it off here, not there."
}

# ---------- operations ----------
step_selftest() { # a failing check must never drop the admin out of the TUI (selftest exits with the fail count)
  clear
  # what the unattended 04:30 reboot left behind, before this run overwrites the screen with a
  # fresh result: the automatic test is the only one that ever runs while nobody is watching
  local last; last=$(postboot_last) \
    && printf 'Last automatic post-reboot self-test: %s\n  (full result: %s, or journalctl -u bookstack-postboot)\n\n' "$last" "$STACK_DIR/$POSTBOOT_LOG_REL"
  STACK_DIR="$STACK_DIR" bash "$STACK_DIR/scripts/selftest.sh" 2>&1 | tee "${TMPDIR:-/tmp}/bookstack-selftest.log" || true
  echo; read -rp "Press Enter to continue..." _ || true
}
step_status() { docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1 | whiptail --title "Containers" --textbox /dev/stdin 24 110 || true; }
step_logs()   {
  s=$(ask "Service (caddy, calibre-web, audiobookshelf, librarian, shelfmark, uptime-kuma, qbittorrent, authelia, ephemera, flaresolverr). Ctrl-C returns to the menu:" caddy) || return 0
  [ -n "$s" ] || return 0
  clear
  trap : INT                         # Ctrl-C ends `docker logs -f`, not this TUI
  docker logs --tail 200 -f "$s" || true
  trap - INT
}
IMG_KEYS="IMG_CWA IMG_ABS IMG_SHELFMARK IMG_QBIT IMG_KUMA IMG_AUTHELIA IMG_FLARESOLVERR IMG_SYNCTHING"
BUILT_IMAGES="bookstack/caddy bookstack/librarian"   # built locally: kept as :prev across an update
stack_up_all() { # (re)start every enabled service with the tags in .env
  compose up -d || return 1
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && { composeA up -d authelia || return 1; }
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && { composeE up -d ephemera || return 1; }
  prune_shelfmark_placeholder    # J35
  return 0
}
wait_healthy() { # seconds: every container that HAS a healthcheck reports healthy
  local i c st all
  for i in $(seq 1 $(( $1 / 5 ))); do
    all=1
    for c in $(docker ps -q 2>/dev/null); do
      st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)
      case "$st" in healthy|none) ;; *) all=0;; esac
    done
    [ "$all" = 1 ] && return 0; sleep 5
  done; return 1
}
# latest_tag image -> newest version-looking tag from Docker Hub / GHCR (lscr.io mirrors GHCR), or "?"
latest_tag() {
  python3 - "$1" <<'PY' 2>/dev/null || echo "?"
import sys, json, re, urllib.request
img = sys.argv[1].split(":")[0]
if img.startswith("lscr.io/"): img = "ghcr.io/" + img[len("lscr.io/"):]
def get(url, hdr={}):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=15))
if img.startswith("ghcr.io/"):
    repo = img[len("ghcr.io/"):]
    tok = get(f"https://ghcr.io/token?scope=repository:{repo}:pull")["token"]
    tags = get(f"https://ghcr.io/v2/{repo}/tags/list?n=10000", {"Authorization": "Bearer " + tok})["tags"]  # one page of ALL tags: the default page missed the newest
else:
    repo = img if "/" in img else "library/" + img
    tags = [t["name"] for t in get(f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated")["results"]]
vt = [t for t in tags if re.fullmatch(r"v?\d+(\.\d+){1,3}", t)]
key = lambda t: tuple(int(x) for x in t.lstrip("v").split("."))
print(max(vt, key=key) if vt else "?")
PY
}
step_check_updates() {
  clear; echo "Checking registries (current -> newest release tag)..."; echo
  local k cur new out=""
  for k in $IMG_KEYS; do
    cur=$(img "$k"); new=$(latest_tag "$cur")
    out="$out$(printf '%-16s %-52s newest: %s' "$k" "$cur" "$new")"$'\n'
  done
  big "Image versions" "$out
Tags ending in :latest or :1 follow a line; the others are exact pins.
Read the release notes before bumping (schema migrations happen):
  CWA:            https://github.com/crocodilestick/Calibre-Web-Automated/releases
  Audiobookshelf: https://github.com/advplyr/audiobookshelf/releases
  Shelfmark:      https://github.com/calibrain/shelfmark/releases
  Authelia:       https://github.com/authelia/authelia/releases

Operations -> Update lets you type the new tags; it backs up first and can roll back."
}
local_checks() { # the update gate: what an update can break. Not disk, NTP, Tailscale or edge probes
  local c fails="" want="caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma"
  torrents_on && want="$want qbittorrent"
  solver_on && want="$want flaresolverr"
  for c in $want; do running "$c" || fails="$fails $c:not-running"; done
  curl -fs -m 5 -o /dev/null http://127.0.0.1:8090/healthz || fails="$fails portal:/healthz"
  curl -fs -m 5 -o /dev/null http://127.0.0.1:8084/api/health || fails="$fails shelfmark:/api/health"
  curl -fs -m 5 -o /dev/null http://127.0.0.1:8083/login || fails="$fails calibre-web:/login"
  curl -fs -m 5 -o /dev/null http://127.0.0.1:13378/healthcheck || fails="$fails audiobookshelf:/healthcheck"
  compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || fails="$fails caddy:config"
  GATE_FAILS="$fails"; [ -z "$fails" ]
}
tag_images() { # from to: bookstack/{caddy,librarian}:from -> :to (a rebuilt :latest keeps its predecessor)
  local i; for i in $BUILT_IMAGES; do docker image inspect "$i:$1" >/dev/null 2>&1 && docker image tag "$i:$1" "$i:$2"; done; return 0
}
UPDATE_MARKER_REL=.update-in-progress   # set by update_failed, cleared on a clean update or rollback
step_update() {
  local k cur new changed="" f="$(restic_env)" mark="$STACK_DIR/$UPDATE_MARKER_REL" keep=0
  # "investigate, then retry" is the most natural thing to do after a failed update — and it used
  # to write the BROKEN tags over the last-known-good ones and re-point :prev at the broken build,
  # leaving nothing to roll back to. While the marker exists, the rollback point is frozen.
  [ -f "$mark" ] && keep=1
  # (a) a restorable point before anything moves
  if [ -f "$f" ]; then
    clear; echo "Pre-update backup (tag pre-update)..."
    "$STACK_DIR/scripts/backup.sh" --tag pre-update || { msg "Pre-update backup failed; not updating. Check Operations -> Backups."; return 1; }
  else
    yesno "No backup repository is configured (Install -> Backups). Update WITHOUT a pre-update backup?" || return 1
  fi
  # (b) remember what runs now — unless an earlier update is still unresolved (see $mark)
  if [ "$keep" = 0 ]; then
    { for k in $IMG_KEYS; do printf '%s=%s\n' "$k" "$(img "$k")"; done; } > "$STACK_DIR/.env.images.prev"; chmod 600 "$STACK_DIR/.env.images.prev"
  else
    msg "A previous update did not finish and you chose not to roll it back, so the last known-good image tags in $STACK_DIR/.env.images.prev and the bookstack/*:prev images are KEPT as they are. This run will not overwrite them."
  fi
  # (c) which tags
  if yesno "Change image versions? (No = re-pull the current tags and rebuild caddy/librarian; Yes = type a new tag per image, Cancel keeps one)"; then
    for k in $IMG_KEYS; do
      cur=$(img "$k"); new=$(ask "$k (current: $cur). New image:tag, or leave as is:" "$cur") || continue
      [ -n "$new" ] && [ "$new" != "$cur" ] && { envset "$k" "$new"; changed="$changed\n  $k: $cur -> $new"; }
    done
  fi
  yesno "Apply the update now?$([ -n "$changed" ] && printf '\n\nTag changes:%b' "$changed" || printf '\n\nNo tag changes: same pins, refreshed images.')\n\nThe code in $SRC_DIR is deployed too. The stack restarts; on failure you can roll back." || { rollback_images; return 1; }
  # (d) code, config, pull / build / up. The locally built images keep their predecessor as :prev.
  clear
  local cf="$STACK_DIR/caddy/Caddyfile"
  [ -s "$cf" ] && cat "$cf" > "$cf.pre-update"
  [ "$keep" = 0 ] && tag_images latest prev
  copy_code_trees; own_data_dirs; write_shelfmark_metadata_env || true
  # host-side units the update may be introducing: an existing install that only ever runs
  # Operations -> Update would otherwise never pick up a new one (idempotent, so it is free)
  install_postboot_unit
  render_caddy_all || { update_failed "the new Caddyfile could not be rendered"; return 1; }
  # the rebuilt images carry this checkout's version (J03); Self-test compares it with .version
  if ! { compose pull --ignore-buildable && BUILD_VERSION="$(build_version)" compose build --pull caddy librarian kuma-bootstrap && stack_up_all; }; then
    update_failed "pull/build/start failed"; return 1
  fi
  apply_caddy >/dev/null 2>&1 || true
  # (e) health gate: container health checks + the local endpoints this update could break
  echo "Waiting for health checks (up to 5 min)..."
  if ! wait_healthy 300; then update_failed "a container did not become healthy"; return 1; fi
  local i; for i in $(seq 1 12); do local_checks && break; sleep 5; done
  if [ -n "$GATE_FAILS" ]; then update_failed "local checks failed:$GATE_FAILS"; return 1; fi
  # the full self-test is information, not a rollback reason (disk, NTP, Tailscale, edge...)
  local st=0; STACK_DIR="$STACK_DIR" bash "$STACK_DIR/scripts/selftest.sh" > "${TMPDIR:-/tmp}/bookstack-selftest.log" 2>&1 || st=$?
  # (g) only now free old layers; the update landed, so the frozen rollback point is released
  rm -f "$mark"
  docker system prune -f --filter until=72h >/dev/null 2>&1 || true
  # new code may carry new monitors or scheduled jobs (the timer wrapper is regenerated too)
  install_selftest_timer; setup_monitoring >/dev/null 2>&1 || true
  msg "Updated and healthy (deployed $(deployed_version)).$([ -n "$changed" ] && printf '\n\nNew tags:%b' "$changed")$([ "$st" != 0 ] && printf '\n\nThe full self-test reports %s failure(s) unrelated to the update gate: Operations -> Self-test.' "$st")"
}
rollback_images() { # restore the IMG_* values saved by step_update
  [ -f "$STACK_DIR/.env.images.prev" ] || return 0
  local line; while IFS= read -r line; do [ -n "$line" ] && envset "${line%%=*}" "${line#*=}"; done < "$STACK_DIR/.env.images.prev"
}
update_failed() { # (f) offer the rollback: previous tags, previous caddy/librarian builds, previous Caddyfile
  # Freeze the rollback point first: whatever the admin answers, the record of the last known-good
  # build must survive a retry of this update (step_update honours the marker).
  touch "$STACK_DIR/$UPDATE_MARKER_REL" 2>/dev/null || true
  if yesno "Update problem: $1.\n\nRoll back to the previous image tags and the previous caddy/librarian builds?"; then
    rollback_images; tag_images prev latest
    local cf="$STACK_DIR/caddy/Caddyfile"; [ -s "$cf.pre-update" ] && cat "$cf.pre-update" > "$cf"
    rm -f "$STACK_DIR/$UPDATE_MARKER_REL"      # back on the known-good build: nothing left to protect
    if stack_up_all; then
      msg "Rolled back to the previous images.\n\nIf the new version already migrated a database, restore the pre-update snapshot: Operations -> Restore from backup -> pick the snapshot tagged 'pre-update' -> 'Config + databases only'."
    else
      msg "Rollback started but some containers did not come up: Operations -> Logs. The pre-update snapshot is under Operations -> Restore from backup (tag 'pre-update')."
    fi
  else
    msg "Left as is. Operations -> Logs / Self-test to investigate; the previous tags are in $STACK_DIR/.env.images.prev.\n\nThat file and the bookstack/*:prev images are now FROZEN: running Update again will not overwrite them, so this rollback point stays available until an update succeeds or you take it."
  fi
}

# ---------- operations: one service, and the tunables ----------
# Authelia and Ephemera exist only in their overlay compose files: plain `compose` can neither
# see nor start them, so every action has to go through the wrapper that owns the service.
# FlareSolverr lives in docker-compose.yml (profile "solver"), so plain compose owns it.
compose_for(){ case "$1" in authelia) printf 'composeA';; ephemera) printf 'composeE';; *) printf 'compose';; esac; }
stack_services(){ # what compose knows about, with the optional services appended when enabled
  local s; s=$(compose ps --services 2>/dev/null | tr -d '\r' | grep -v '^$' || true)
  # a stack that has never been started lists nothing; the admin still needs the menu
  [ -n "$s" ] || s="caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma"
  [ "$(envget AUTHELIA_ENABLED)" = true ] && s="$s authelia"
  [ "$(envget EPHEMERA_ENABLED)" = true ] && s="$s ephemera"
  solver_on && s="$s flaresolverr"
  printf '%s\n' $s | awk '!seen[$0]++'
}
# "Restart audiobookshelf, it's wedged" is the commonest thing an admin does to a stack like this
# and it was not in the console at all — which also meant there was no console path to make a
# setting this TUI wrote actually reach its container.
step_service() {
  local svcs=() items=() s st act cw rc=0 note=""
  mapfile -t svcs < <(stack_services)
  [ "${#svcs[@]}" -gt 0 ] || { msg "No services found. Is $STACK_DIR/docker-compose.yml in place (Install -> Deploy)?"; return 1; }
  for s in "${svcs[@]}"; do
    running "$s" && st="running" || st="NOT running"
    items+=("$s" "$st")
  done
  s=$(whiptail --title "Restart / stop / start a service" --menu "One service at a time. Operations -> Status shows the same list with images and uptime." 20 76 11 "${items[@]}" 0 "Back" 3>&1 1>&2 2>&3) || return 0
  [ "$s" = 0 ] && return 0
  act=$(whiptail --title "$s" --menu "What should happen to $s?" 14 76 4 \
    R "Restart it (recreated, so .env and image changes are picked up)" \
    S "Stop it (it stays stopped until you start it here)" \
    U "Start it" \
    0 "Back" 3>&1 1>&2 2>&3) || return 0
  cw=$(compose_for "$s")
  case "$act" in
    R) yesno "Restart $s?\n\nThe container is RECREATED rather than just restarted, so anything this TUI wrote to $ENV_FILE and any changed image tag take effect. $s is unavailable for a few seconds.\n\nRestart it?" || return 0
       clear; echo "Recreating $s..."; $cw up -d --force-recreate "$s" || rc=1;;
    S) # caddy is the only process that answers from the internet, and nothing restarts it by itself
       if [ "$s" = caddy ]; then
         yesno "STOP caddy?\n\ncaddy is the ONLY process that answers from the internet. While it is stopped, books., audio., request. and shelf. answer nothing at all — for everyone, on every device — and nothing brings it back by itself: you have to come back to this menu and start it.\n\nTailscale-only tools (monitor., dl., ephemera.) go down with it too.\n\nReally stop caddy?" || return 0
       else
         yesno "Stop $s?\n\nIt stays stopped until you start it again here or the server reboots. Nothing else is changed.\n\nStop it?" || return 0
       fi
       clear; echo "Stopping $s..."; $cw stop "$s" || rc=1;;
    U) clear; echo "Starting $s..."
       # `up -d`, never `start`: a container that was REMOVED (Update, Restore, a disabled feature)
       # cannot be started, only recreated — and this is where the admin comes to fix exactly that
       $cw up -d "$s" || rc=1;;
    *) return 0;;
  esac
  if [ "$rc" != 0 ]; then
    msg "compose could not do that to $s (Operations -> Logs -> $s shows why). The service was left as it was."
    [ "$s" = caddy ] && [ "$act" != S ] && msg "WARNING: caddy is the only public listener. Check Operations -> Status: if it is not running, no public site answers."
    return 1
  fi
  case "$act" in
    S) note=""
       [ "$s" = caddy ] && note="\n\nEVERY public site is down until you start caddy again from this menu."
       msg "$s is stopped.$note";;
    *) wait_healthy 90 || note="\n\nSomething in the stack is not reporting healthy yet — give it a minute, then Operations -> Status."
       running "$s" || note="\n\nWARNING: $s is NOT running after that command. Operations -> Logs -> $s."
       msg "$s was $([ "$act" = R ] && printf recreated || printf started).$note";;
  esac
}
# Settings the code reads from the environment but nothing ever wrote. Fields:
#   group|KEY|default|kind|one-line explanation
# The defaults MUST match docker-compose.yml's ${KEY:-default} and .env.example, or this screen
# shows a value the container is not actually using.
ADV_SETTINGS='lockout|LOCKOUT_FAILS|5|int|Wrong passwords for one user from one address before that pair is locked
lockout|LOCKOUT_WINDOW|900|int|Seconds those failures are counted over
lockout|LOCKOUT_SECONDS|900|int|How long a lockout lasts, in seconds
lockout|LOCKOUT_IP_FAILS|20|int|Wrong passwords from ONE address across all accounts before the address is locked
lockout|SESSION_HOURS|12|int|How long a portal login stays signed in, in hours
uploads|MAX_UPLOAD_MB|95|int|Largest file the portal browser form takes (Cloudflare refuses bodies over 100 MB)
uploads|MAX_EBOOK_MB|200|int|Largest ebook the worker downloads or imports
uploads|MAX_AUDIO_MB|2048|int|Largest audiobook (a LibriVox zip of a long book runs past 1 GB)
uploads|MAX_PDF_MB|250|int|Largest PDF; bigger ones are parked, because tagging renders them in memory
uploads|MAX_MAIL_MB|40|int|Largest mailed-in attachment; a message is parsed in memory at ~12x its size, so keep this well under the upload cap
uploads|KINDLE_MAX_MB|45|int|Largest Send-to-Kindle attachment (Amazon refuses bigger ones)
mail|IMAP_PORT|0|int|IMAP port; 0 = the default for the mode (993 implicit TLS, 143 STARTTLS)
mail|IMAP_SSL|true|bool|true = implicit TLS on 993; false = STARTTLS on 143, for a local relay
mail|IMAP_FOLDER|INBOX|text|Mailbox the e-mail-to-library poller reads
mail|NOTIFY_WEBHOOK_FORMAT|auto|text|How alerts are posted: auto (ntfy style for an ntfy host), ntfy or json
mail|ABS_LIBRARY_NAME|Audiobooks|text|Audiobookshelf library the portal files audiobooks into
disk|DISK_WARN_PCT|85|pct|Disk use that alerts you, once per 24 h
disk|DISK_STOP_PCT|95|pct|Disk use that stops the downloaders and pauses imports
disk|DISK_RESUME_PCT|80|pct|Disk use they are started again below
backup|RESTIC_KEEP_DAILY|7|int|Daily snapshots the nightly forget --prune keeps
backup|RESTIC_KEEP_WEEKLY|4|int|Weekly snapshots kept
backup|RESTIC_KEEP_MONTHLY|6|int|Monthly snapshots kept'
adv_rows(){ printf '%s\n' "$ADV_SETTINGS" | grep "^$1|" || true; }
adv_value(){ local v; v=$(envget "$1"); printf '%s' "${v:-$2}"; }   # what the stack really uses now
step_advanced() {
  local g key def kind help cur new line items=() w s r
  while true; do
    g=$(whiptail --title "Advanced settings" --menu "Values the stack reads from $ENV_FILE. A key that is not set uses the built-in default, and that is what this screen shows — so the number you see is always the one in force.\n\nEditing docker-compose.yml instead does NOT work: every Deploy and Update rewrites it." 20 88 6 \
      lockout "Login lockout thresholds and session length" \
      uploads "Size ceilings for uploads, imports and Send-to-Kindle" \
      mail    "IMAP intake, alert format, Audiobookshelf library name" \
      disk    "Disk watchdog thresholds" \
      backup  "restic snapshot retention" \
      0       "Back" 3>&1 1>&2 2>&3) || return 0
    [ "$g" = 0 ] && return 0
    while true; do
      items=()
      while IFS='|' read -r _ key def kind help; do
        [ -n "${key:-}" ] || continue
        items+=("$key" "$(adv_value "$key" "$def")  -  ${help:0:56}")
      done <<< "$(adv_rows "$g")"
      [ "${#items[@]}" -gt 0 ] || break
      key=$(whiptail --title "Advanced: $g" --menu "The value shown is the one in force right now." 20 104 10 "${items[@]}" 0 "Back" 3>&1 1>&2 2>&3) || break
      [ "$key" = 0 ] && break
      line=$(printf '%s\n' "$ADV_SETTINGS" | grep "^$g|$key|" | head -1)
      IFS='|' read -r _ key def kind help <<< "$line"
      cur=$(adv_value "$key" "$def")
      new=$(ask "$key — $help\n\nBuilt-in default: $def" "$cur") || continue
      [ -n "$new" ] || { msg "Nothing was changed ($key is still $cur)."; continue; }
      [ "$new" = "$cur" ] && { msg "$key is already $cur. Nothing was changed."; continue; }
      case "$kind" in
        int) case "$new" in ''|*[!0-9]*) msg "$key must be a whole number. Nothing was changed."; continue;; esac;;
        pct) case "$new" in ''|*[!0-9]*) msg "$key is a percentage. Nothing was changed."; continue;; esac
             { [ "$new" -ge 1 ] && [ "$new" -le 99 ]; } || { msg "$key must be between 1 and 99. Nothing was changed."; continue; };;
        bool) case "$new" in true|false) ;; *) msg "$key must be exactly true or false. Nothing was changed."; continue;; esac;;
      esac
      envset "$key" "$new" || { msg "Could not write $ENV_FILE, so $key was NOT changed."; continue; }
      case "$g" in
        backup) msg "$key is now $new.\n\nscripts/backup.sh reads $ENV_FILE each time it runs, so nothing has to be restarted; the new retention applies at the next nightly forget --prune.";;
        disk)   w=$(adv_value DISK_WARN_PCT 85); s=$(adv_value DISK_STOP_PCT 95); r=$(adv_value DISK_RESUME_PCT 80)
                { [ "$r" -lt "$s" ] && [ "$w" -le "$s" ]; } \
                  || msg "WARNING: the thresholds now read warn=$w stop=$s resume=$r. They only work as resume < stop and warn <= stop — otherwise the watchdog either stops the downloaders before it ever warns you, or never starts them again."
                if restart_portal; then msg "$key is now $new.\n\nThe hourly watchdog reads $ENV_FILE when it runs, and the portal was recreated so its own copy of the thresholds matches."
                else msg "$key is now $new in $ENV_FILE and the hourly watchdog will use it, but the portal could NOT be restarted, so the portal still pauses its imports at the OLD threshold (Operations -> Logs -> librarian)."; fi;;
        *)      if restart_portal; then msg "$key is now $new and the portal was recreated, so it is live."
                else msg "$key is now $new in $ENV_FILE, but the portal could NOT be restarted, so it is NOT in force yet (Operations -> Logs -> librarian)."; fi;;
      esac
    done
  done
}

# ---------- menus ----------
# Every arm ends in `|| true`: under errexit a failed step, a whiptail Esc (255) or a non-zero
# self-test must return to the menu, never drop the admin to the shell.
menu_install() {
  while true; do
    ch=$(whiptail --title "Install & deploy" --menu "Fresh server: Q does everything in order. Re-run any single step later." 20 84 10 \
      Q "Quick install (all steps in order, then your first user)" \
      1 "System: updates, Docker, firewall, SSH keys, auto-updates" \
      2 "Tailscale: private network for admin tools + SSH" \
      3 "Configure: domain, email, Cloudflare token, admin account + gate password" \
      4 "Cloudflare: DNS, strict TLS, origin lock, firewall allowlist" \
      5 "Deploy: build and start the stack (+ secure app defaults)" \
      6 "Backups: encrypted nightly restic backup" \
      7 "Alerts: phone notification (ntfy / webhook) for failed backups, full disk" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in Q) step_quick || true;; 1) step_system || true;; 2) step_tailscale || true;; 3) step_configure || true;;
      4) step_cloudflare || true;; 5) step_deploy || true;; 6) step_backup || true;; 7) step_alerts || true;; 0) return 0;; esac
  done
}
# ---------- L16: the portal's service login for Shelfmark's approval API ----------
# Shelfmark has no API key, so the portal signs in as a dedicated Calibre-Web ADMIN account with
# a generated password (kept in .env only; nobody types it). Created/reset idempotently.
SHELFMARK_SVC_NAME=svc-portal
ensure_shelfmark_service() {
  local pw; pw=$(envget SHELFMARK_SVC_PASS)
  if [ -z "$pw" ]; then pw=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32); fi
  if lib list 2>/dev/null | json '" ".join(u["name"] for u in d)' | tr ' ' '\n' | grep -qx "$SHELFMARK_SVC_NAME"; then
    printf '%s\n' "$pw" | lib passwd "$SHELFMARK_SVC_NAME" --password-stdin >/dev/null 2>&1 || return 1
  else
    printf '%s\n' "$pw" | lib add-user "$SHELFMARK_SVC_NAME" --email "svc-portal@localhost" --password-stdin --admin >/dev/null 2>&1 || return 1
  fi
  envset SHELFMARK_SVC_USER "$SHELFMARK_SVC_NAME" && envset SHELFMARK_SVC_PASS "$pw"
}

# ---------- metadata sources ----------
# Open Library needs no key and is always on, in the portal and in Shelfmark (whose default
# metadata-first search had NO provider until v5 and answered "No metadata provider configured").
# Hardcover (best series data) and Google Books need the owner's own free key. .env is the one
# source of truth; shelfmark/metadata.env is regenerated from it, holding only keys that EXIST —
# Shelfmark treats an empty env var as set and would lock its key field blank.
write_shelfmark_metadata_env() {
  local f="$STACK_DIR/shelfmark/metadata.env" hc gb
  hc=$(envget HARDCOVER_API_KEY); gb=$(envget GOOGLE_BOOKS_API_KEY)
  mkdir -p "$STACK_DIR/shelfmark"
  ( umask 077
    { echo "# Generated by bookstack.sh from .env (Library -> Metadata sources). Do not edit."
      [ -n "$hc" ] && printf 'HARDCOVER_ENABLED=true\nHARDCOVER_API_KEY=%s\n' "$hc"
      [ -n "$gb" ] && printf 'GOOGLEBOOKS_ENABLED=true\nGOOGLEBOOKS_API_KEY=%s\n' "$gb"
      true; } > "$f.new" ) || return 1
  chown root:root "$f.new" 2>/dev/null || true
  mv -f "$f.new" "$f"
}
metadata_key_ok() { # hardcover|google key -> 0 when the service accepts it
  case "$1" in
    hardcover) curl -fsS -m 15 -X POST https://api.hardcover.app/v1/graphql \
                 -H "Authorization: Bearer ${2#Bearer }" -H "Content-Type: application/json" \
                 --data '{"query":"{ me { id } }"}' 2>/dev/null | grep -q '"me"';;
    google) curl -fsS -m 15 "https://www.googleapis.com/books/v1/volumes?q=isbn:9780141439518&maxResults=1&key=$2" >/dev/null 2>&1;;
  esac
}
step_metadata_sources() {
  local hc gb cur note=""
  cur=$(envget HARDCOVER_API_KEY)
  hc=$(askpw "Hardcover API token (optional; hardcover.app -> Settings -> API; starts with hc_pat_ or 'Bearer'). The best series data there is.\n\nBlank = keep the current one ($([ -n "$cur" ] && echo set || echo none)). Type - to remove it.") || hc=""
  case "$hc" in
    -) envset HARDCOVER_API_KEY ""; note="$note\nHardcover: removed.";;
    "") ;;
    *) if metadata_key_ok hardcover "$hc"; then envset HARDCOVER_API_KEY "$hc"; note="$note\nHardcover: accepted and saved."
       else note="$note\nHardcover: the token was REFUSED by api.hardcover.app, not saved."; fi;;
  esac
  cur=$(envget GOOGLE_BOOKS_API_KEY)
  gb=$(askpw "Google Books API key (optional; console.cloud.google.com -> APIs -> Books API -> Credentials; free, 1000 lookups a day). Without one Google Books is not used: its keyless quota is shared worldwide and is usually exhausted.\n\nBlank = keep the current one ($([ -n "$cur" ] && echo set || echo none)). Type - to remove it.") || gb=""
  case "$gb" in
    -) envset GOOGLE_BOOKS_API_KEY ""; note="$note\nGoogle Books: removed.";;
    "") ;;
    *) if metadata_key_ok google "$gb"; then envset GOOGLE_BOOKS_API_KEY "$gb"; note="$note\nGoogle Books: accepted and saved."
       else note="$note\nGoogle Books: the key was REFUSED by googleapis.com, not saved."; fi;;
  esac
  write_shelfmark_metadata_env || { msg "Could not write shelfmark/metadata.env (disk full?).$note"; return 1; }
  local rc=0
  compose up -d librarian shelfmark >/dev/null 2>&1 || rc=1
  prune_shelfmark_placeholder
  msg "Metadata sources$note\n\nAlways on: Open Library (book search, author pages, links to free copies) and the bookinfo/Hardcover mirrors for library enrichment.\nOptional now: Hardcover $([ -n "$(envget HARDCOVER_API_KEY)" ] && echo ON || echo off), Google Books $([ -n "$(envget GOOGLE_BOOKS_API_KEY)" ] && echo ON || echo off) — in the portal's enrichment AND in Shelfmark's own metadata search.$([ $rc != 0 ] && printf '\n\nWARNING: the portal or Shelfmark could not be recreated, so the change is not live yet (Operations -> Logs).')"
  return $rc
}
# ---------- Library -> Seedbox ----------
# Shelfmark sends a reader's pick to the seedbox's SABnzbd / rTorrent; they download on the
# SEEDBOX's disk. The seedbox's own Syncthing (SEND ONLY) sends the bookstack folders to this
# server's Syncthing container (RECEIVE ONLY) in library/seedbox-sync, and
# scripts/seedbox-fetch.py (every minute) hands each finished, fully arrived item to
# library/seedbox (Shelfmark's /seedbox), where Shelfmark's remote path mappings find it and file
# it into the right reader's dropbox. Nothing on the seedbox is ever changed. Addresses and
# passwords live in /etc/bookstack/seedbox.env (0600); only Syncthing's API key is in .env,
# because its container needs it.
SEEDBOX_ENV_REL=bookstack/seedbox.env
install_seedbox_units(){
  local u="$ETC/systemd/system"; mkdir -p "$u"
  cat > "$u/bookstack-seedbox.service" << UNIT
[Unit]
Description=Bookstack: hand finished seedbox downloads to Shelfmark
After=network-online.target docker.service
[Service]
Type=oneshot
Environment=STACK_DIR=$STACK_DIR
Environment=SEEDBOX_ENV=$ETC/$SEEDBOX_ENV_REL
ExecStart=/usr/bin/python3 $STACK_DIR/scripts/seedbox-fetch.py
TimeoutStartSec=6h
UNIT
  cat > "$u/bookstack-seedbox.timer" << 'UNIT'
[Unit]
Description=Bookstack: hand finished seedbox downloads to Shelfmark every minute
[Timer]
OnCalendar=*:*:00
AccuracySec=10s
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload && systemctl enable --now bookstack-seedbox.timer
}
remove_seedbox_units(){
  local u="$ETC/systemd/system"
  systemctl disable --now bookstack-seedbox.timer >/dev/null 2>&1 || true
  rm -f "$u/bookstack-seedbox.timer" "$u/bookstack-seedbox.service"; systemctl daemon-reload >/dev/null 2>&1 || true
}
seedbox_get(){ { grep -E "^$1=" "$ETC/$SEEDBOX_ENV_REL" 2>/dev/null || true; } | head -1 | cut -d= -f2-; }   # raw values, see step_seedbox
seedbox_fetch(){ env SEEDBOX_ENV="${SEEDBOX_ENV_FILE:-$ETC/$SEEDBOX_ENV_REL}" STACK_DIR="$STACK_DIR" python3 "$STACK_DIR/scripts/seedbox-fetch.py" "$@"; }
seedbox_mappings(){ # the rows the admin types into Shelfmark
  local sab_own rt_dir
  sab_own=$(seedbox_get SEEDBOX_SAB_OWN); rt_dir=$(seedbox_get SEEDBOX_RT_DIR)
  printf 'In Shelfmark (https://shelf.%s) -> Settings -> Download clients, at the bottom:\n\n' "$(envget DOMAIN)"
  printf '1. Completed Path Wait (seconds):  3600\n   (the default 60 s is too short: files arrive some minutes after the seedbox finishes)\n\n'
  printf '   Torrent / NZB Completion Action show Keep / Copy and cannot be changed there: they are\n   pinned by this server so Shelfmark never removes a torrent or a job after an import.\n\n'
  printf '2. Path Mappings -> Add Mapping:\n'
  [ -n "$sab_own" ] && printf '   Client SABnzbd:  Remote Path  %s\n                    Local Path   /seedbox/sabnzbd\n' "$sab_own"
  [ -n "$rt_dir" ] && [ -n "$(seedbox_get SEEDBOX_RT_URL)" ] && printf '   Client rTorrent: Remote Path  %s\n                    Local Path   /seedbox/rtorrent\n' "$rt_dir"
  printf '\nSave. Then request one small book through Shelfmark as a test.'
}
seedbox_remote_steps(){ # what to do in the SEEDBOX's Syncthing; $1 = this server's device ID
  local ip cats c rt_dir sab_own
  ip=$(envget PUBLIC_IP); cats=$(seedbox_get SEEDBOX_SAB_CATS); sab_own=$(seedbox_get SEEDBOX_SAB_OWN); rt_dir=$(seedbox_get SEEDBOX_RT_DIR)
  printf 'On the SEEDBOX, open its Syncthing web page.\n\n'
  printf '1. "+ Add Remote Device" (bottom right):\n'
  printf '     Device ID:    %s\n' "$1"
  printf '     Device Name:  bookstack\n'
  printf '     Advanced tab -> Addresses:  tcp://%s:22000\n' "${ip:-THIS-SERVER-IP}"
  printf '     Leave "Auto Accept" OFF. Save.\n\n'
  printf '2. Within a minute it asks, once per folder: "bookstack wants to share folder ...".\n'
  printf '   Click Add on each, and BEFORE saving:\n'
  printf '     General tab  -> Folder Path: the real folder on the seedbox (paths below are as\n'
  printf '                     rTorrent / SABnzbd see them; the seedbox'"'"'s Syncthing may show the\n'
  printf '                     same folder under another root, e.g. /sdb/NAME/data/... or ~/...):\n'
  [ -n "$rt_dir" ] && printf '                       "Bookstack rTorrent"          -> %s\n' "$rt_dir"
  for c in $cats; do printf '                       "Bookstack SABnzbd %s"  -> %s/%s\n' "$c" "${sab_own:-.../completed}" "$c"; done
  printf '     Advanced tab -> Folder Type:  Send Only          <- REQUIRED\n'
  printf '                     Full Rescan Interval (s):  300\n'
  printf '   Save.\n\n'
  printf 'Send Only means the seedbox'"'"'s Syncthing only READS these folders: nothing there is ever\n'
  printf 'moved, deleted or changed, and this server'"'"'s side is Receive Only as well. Syncthing adds\n'
  printf 'one small marker folder, .stfolder, inside each (its own; rTorrent and SABnzbd ignore it).\n'
  printf 'A folder that shows 0 files although the seedbox has some means a wrong Folder Path:\n'
  printf 'remove it there (Edit -> Remove: that deletes nothing on disk) and add it again.\n\n'
  printf 'Then: Library -> Seedbox -> Check the connection.'
}
seedbox_wait(){ local i; for i in $(seq 1 45); do curl -fsS -m 3 http://127.0.0.1:8384/rest/noauth/health >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }
step_seedbox(){
  local cf="$ETC/$SEEDBOX_ENV_REL" ch out me
  if [ -f "$ETC/systemd/system/bookstack-seedbox.timer" ]; then
    ch=$(whiptail --title "Seedbox" --menu "Finished seedbox downloads arrive through Syncthing and are handed to Shelfmark every minute." 16 86 6 \
      C "Check the connection now" \
      S "Show what to set up on the seedbox (this server's Syncthing ID)" \
      M "Show the Shelfmark path mappings to enter" \
      E "Change the settings" \
      D "Turn it off" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      C) out=$(seedbox_fetch --check 2>&1); msg "$out"; return 0;;
      S) me=$(seedbox_fetch --device-id 2>&1) || { msg "This server's Syncthing does not answer:\n$me\n\nOperations -> Logs -> syncthing."; return 1; }
         big "Seedbox: on the seedbox" "$(seedbox_remote_steps "$me")"; return 0;;
      M) big "Seedbox: Shelfmark settings" "$(seedbox_mappings)"; return 0;;
      D) remove_seedbox_units
         compose stop syncthing >/dev/null 2>&1 || true; compose rm -f syncthing >/dev/null 2>&1 || true
         envset SEEDBOX_ENABLED false; seedbox_port close
         msg "Seedbox sync is off: this server's Syncthing is stopped and port 22000 closed. Nothing on the seedbox was touched (remove the 'bookstack' device there if you like).\n\nItems already here stay in $STACK_DIR/library/seedbox-sync and library/seedbox; the settings stay in $cf."; return 0;;
      E) ;;
      *) return 0;;
    esac
  fi
  big "Seedbox: what this sets up" "Shelfmark (Prowlarr search) sends a reader's pick to your seedbox's SABnzbd or rTorrent,
which download on the SEEDBOX. Syncthing then brings the finished files here:

  seedbox Syncthing (Send Only)  -->  this server's Syncthing (Receive Only)

and every minute each finished, fully arrived item is handed to Shelfmark (/seedbox), which
files it into that reader's dropbox; the library imports it.

NOTHING ON THE SEEDBOX IS EVER MOVED, DELETED OR CHANGED:
  - the seedbox side is Send Only: it only reads, and ignores everything from here
  - this server's side is Receive Only: nothing done here is ever sent; checked every
    minute, and a folder found otherwise is paused at once
  - torrents are handed over only once rTorrent reports them complete, and keep seeding
  - this server keeps about a week of them, then drops its OWN copy (never the seedbox's)

You need: the seedbox's Syncthing Device ID (its Syncthing: Actions -> Show ID), SABnzbd's
completed folder, and the rTorrent folder and login Shelfmark already uses."
  local dev addr sabown cats rturl rtdir rtu rtp srcs c fid gp ids="" rc=0
  dev=$(ask "The seedbox's Syncthing Device ID (on the seedbox's Syncthing page: Actions -> Show ID):" "$(seedbox_get SEEDBOX_ST_DEVICE)") || return 0
  dev=$(printf '%s' "$dev" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
  [[ "$dev" =~ ^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$ ]] || { msg "'$dev' is not a Syncthing Device ID (eight groups of seven letters and digits, joined by -). Nothing was changed."; return 1; }
  addr=$(ask "The seedbox's Syncthing address, only if your seedbox provider lists one (e.g. tcp://NAME.YOUR-SEEDBOX:PORT). Blank = found automatically:" "$(seedbox_get SEEDBOX_ST_ADDRESS)") || return 0
  [ -z "$addr" ] || [[ "$addr" =~ ^(tcp|quic)://[^/[:space:]]+:[0-9]+$ ]] || { msg "'$addr' is not an address like tcp://host:port. Nothing was changed."; return 1; }
  sabown=$(ask "SABnzbd's COMPLETED folder as SABnzbd shows it (Config -> Folders -> Completed Download Folder, e.g. /data/watch/downloads/sabnzbd/completed). Blank = no SABnzbd:" "$(seedbox_get SEEDBOX_SAB_OWN)") || return 0
  cats=""
  if [ -n "$sabown" ]; then
    cats=$(ask "The SABnzbd categories Shelfmark uses (space-separated):" "$(seedbox_get SEEDBOX_SAB_CATS | grep . || echo "bookstack-ebooks bookstack-audiobooks")") || return 0
  fi
  rturl=$(ask "rTorrent's XML-RPC address, the one that passed Shelfmark's test (e.g. https://NAME-rutorrent.YOUR-SEEDBOX/RPC2). Blank = no torrents:" "$(seedbox_get SEEDBOX_RT_URL)") || return 0
  rtdir=""; rtu=""; rtp=""
  if [ -n "$rturl" ]; then
    rtdir=$(ask "The folder Shelfmark gives rTorrent (Shelfmark's rTorrent 'Download Directory', e.g. /sdb/NAME/data/rtorrent/bookstack):" "$(seedbox_get SEEDBOX_RT_DIR)") || return 0
    [ -n "$rtdir" ] || { msg "The rTorrent folder is needed to know which torrents are Shelfmark's. Nothing was changed."; return 1; }
    rtu=$(ask "rTorrent's username (the seedbox login in front of ruTorrent). Blank = none:" "$(seedbox_get SEEDBOX_RT_USER)") || return 0
    [ -n "$rtu" ] && { rtp=$(askpw "Password for $rtu:") || return 0; }
  fi
  [ -n "$cats" ] || [ -n "$rturl" ] || { msg "Neither SABnzbd nor rTorrent given: nothing to bring back. Nothing was changed."; return 1; }
  srcs=""
  for c in $cats; do
    [[ "$c" =~ ^[A-Za-z0-9._-]+$ ]] || { msg "'$c' is not a SABnzbd category name this can use (letters, digits, . _ -). Nothing was changed."; return 1; }
    fid="bookstack-sab-${c#bookstack-}"
    case " $ids " in *" $fid "*) msg "Two categories give the same Syncthing folder ($fid). Nothing was changed."; return 1;; esac
    ids="$ids $fid"; srcs="$srcs${srcs:+;}$fid|sabnzbd/$c|sab"
  done
  [ -n "$rturl" ] && srcs="$srcs${srcs:+;}bookstack-rtorrent|rtorrent|rt"
  gp=$(seedbox_get SEEDBOX_ST_GUI_PASS); [ -n "$gp" ] || gp=$(openssl rand -hex 16)
  ( umask 077; mkdir -p "$(dirname "$cf")"
    # raw KEY=value lines: read by seedbox-fetch.py (Python), never sourced by a shell, so a
    # password with $, quotes or spaces arrives exactly as typed (printf %q would add backslashes)
    { printf 'SEEDBOX_ST_DEVICE=%s\nSEEDBOX_ST_ADDRESS=%s\nSEEDBOX_ST_GUI_PASS=%s\n' "$dev" "$addr" "$gp"
      printf 'SEEDBOX_SAB_OWN=%s\nSEEDBOX_SAB_CATS=%s\n' "${sabown%/}" "$cats"
      printf 'SEEDBOX_RT_URL=%s\nSEEDBOX_RT_DIR=%s\nSEEDBOX_RT_USER=%s\nSEEDBOX_RT_PASS=%s\n' "$rturl" "${rtdir%/}" "$rtu" "$rtp"
      printf 'SEEDBOX_SOURCES=%s\n' "$srcs"; } > "$cf.new" ) || { msg "Could not write $cf.new. Nothing was changed."; return 1; }
  if [ -n "$rturl" ]; then
    clear; echo "Asking rTorrent..."
    out=$(SEEDBOX_ENV_FILE="$cf.new" seedbox_fetch --check-rtorrent 2>&1) || rc=$?
    if [ $rc != 0 ] && ! yesno "The rTorrent check found problems:\n\n$out\n\nSave these settings anyway?"; then rm -f "$cf.new"; msg "Nothing was saved."; return 1; fi
  fi
  mv -f "$cf.new" "$cf"; chmod 600 "$cf"; chown root:root "$cf" 2>/dev/null || true
  envdefault SYNCTHING_API_KEY "$(openssl rand -hex 24)" || { msg "Could not write $ENV_FILE. Syncthing was not started."; return 1; }
  envset SEEDBOX_ENABLED true
  install -d -o 1000 -g 1000 "$STACK_DIR/syncthing" "$STACK_DIR/library/seedbox" "$STACK_DIR/library/seedbox-sync" \
    "$STACK_DIR/library/seedbox-sync/rtorrent" "$STACK_DIR/library/seedbox-sync/sabnzbd"
  for c in $cats; do install -d -o 1000 -g 1000 "$STACK_DIR/library/seedbox-sync/sabnzbd/$c"; done
  seedbox_port open
  clear; echo "Starting this server's Syncthing..."
  compose up -d syncthing >/dev/null 2>&1 && seedbox_wait \
    || { msg "This server's Syncthing did not start (Operations -> Logs -> syncthing). The settings are saved; run this step again."; return 1; }
  me=$(seedbox_fetch --setup 2>&1) || { msg "This server's Syncthing could not be set up:\n\n$me\n\nThe settings are saved; run this step again."; return 1; }
  me=$(printf '%s' "$me" | tail -1)
  running shelfmark && { compose up -d shelfmark >/dev/null 2>&1 || true; }    # the /seedbox mount
  install_seedbox_units
  big "Seedbox: next, on the seedbox" "$(seedbox_remote_steps "$me")"
  big "Seedbox: and in Shelfmark" "$(seedbox_mappings)"
}
menu_library() {
  while true; do
    ch=$(whiptail --title "Library" --menu "What the library does with books, and where they come from." 24 88 13 \
      F "Formats & conversion: convert to EPUB on import, kept formats, Kindle fixer, duplicates" \
      A "Audiobookshelf: root + API key + library; per-user audiobook isolation ($(abs_ready && echo set up || echo not set up))" \
      M "Mail: SMTP so the portal can Send-to-Kindle (and test it)" \
      S "Sources: catalogs offered in the portal, approvals, your own OPDS catalog" \
      H "Shelfmark: extended search settings + first-run checklist" \
      K "Metadata sources: Open Library (always on) + optional Hardcover / Google Books keys" \
      C "Your catalogs: add / test / remove your own OPDS feeds" \
      W "Keep looking: every reader's waiting list, cancel entries" \
      I "Intake & dropboxes: webhook, Gutenberg mirror, email-to-library" \
      T "Torrents (qBittorrent): enable/disable ($(torrents_on && echo on || echo off))" \
      B "Seedbox: Shelfmark's seedbox downloads, through Syncthing ($([ -f "$ETC/systemd/system/bookstack-seedbox.timer" ] && echo on || echo off))" \
      Q "Request queue: approvals and failures, all of them, retry / dismiss" \
      P "Parked files: what the importer could not use, retry / delete" \
      R "Audiobookshelf: rescan the library now" \
      G "How per-user isolation works (guide)" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in F) step_formats || true;; A) step_abs_setup || true;; M) step_mail || true;; S) step_sources || true;; H) step_shelfmark || true;;
      K) step_metadata_sources || true;; C) step_catalogs || true;; W) step_wanted || true;;
      I) step_intake || true;; T) step_torrents || true;; B) step_seedbox || true;; Q) step_requests || true;; P) step_parked || true;; R) step_abs_scan || true;;
      G) step_isolation || true;; 0) return 0;; esac
  done
}
menu_security() {
  while true; do
    ch=$(whiptail --title "Security" --menu "Authelia gate: $([ "$(envget AUTHELIA_ENABLED)" = true ] && echo ON || echo off)   Public SSH: $([ "$(envget SSH_LOCKED)" = true ] && echo LOCKED || echo open)" 23 84 12 \
      A "Authelia: enable self-hosted SSO + 2FA in front of the public apps" \
      D "Authelia: disable the gate" \
      U "Authelia: add or reset a user" \
      L "Lock SSH to Tailscale only" \
      O "Reopen public SSH (undo Lock SSH; key-only auth stays)" \
      F "fail2ban: brute-force protection (SSH + login pages)" \
      B "Bans: show what is banned and release an address (fail2ban + Cloudflare)" \
      M "Mail auth: SPF/DMARC records for Send-to-Kindle deliverability" \
      C "Cloudflare Access: hosted SSO + 2FA alternative (guide)" \
      R "Rotate the portal session secret (logs every portal user out)" \
      T "Login bot check: Cloudflare Turnstile on the portal login ($([ -n "$(envget TURNSTILE_SITEKEY)" ] && echo on || echo off))" \
      Z "Origin lock: this zone's own Cloudflare client certificate ($(aop_mode))" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in A) step_authelia || true;; D) step_authelia_off || true;; U) step_authelia_user || true;; L) step_lock_ssh || true;;
      O) step_unlock_ssh || true;; F) step_fail2ban || true;; B) step_unban || true;; M) step_mailauth || true;;
      C) step_cfaccess || true;; R) step_rotate_secret || true;; T) step_turnstile || true;; Z) step_origin_lock || true;; 0) return 0;; esac
  done
}
# L17: Cloudflare Turnstile on the portal login. Off by default: it is the one page that may then
# load Cloudflare's challenge script (every other page keeps script-src 'none').
step_turnstile() {
  local sk sec ans
  if [ -n "$(envget TURNSTILE_SITEKEY)" ] && yesno "The Turnstile bot check is ON for the portal login.\n\nTurn it off?"; then
    envset TURNSTILE_SITEKEY ""; envset TURNSTILE_SECRET ""
    restart_portal_ok && msg "Turnstile is off; the login page is back to no scripts at all."; return 0
  fi
  sk=$(ask "Turnstile SITE key (Cloudflare dashboard -> Turnstile -> Add widget, domain request.$(envget DOMAIN), mode Managed):" "") || return 0
  [ -n "$sk" ] || return 0
  sec=$(askpw "Turnstile SECRET key:") || return 0
  [ -n "$sec" ] || return 0
  # a real secret answers 'invalid-input-response' for a dummy token; a wrong one 'invalid-input-secret'
  ans=$(curl -fsS -m 15 -X POST https://challenges.cloudflare.com/turnstile/v0/siteverify \
        --data-urlencode "secret=$sec" --data-urlencode "response=bookstack-key-check" 2>/dev/null) || ans=""
  case "$ans" in
    *invalid-input-secret*) msg "Cloudflare says that secret key is wrong. Nothing was changed."; return 1;;
    "") msg "Cloudflare did not answer the key check. Nothing was changed; try again."; return 1;;
  esac
  envset TURNSTILE_SITEKEY "$sk"; envset TURNSTILE_SECRET "$sec"
  restart_portal_ok || return 1
  msg "Turnstile is on for the portal login.\n\nOpen https://request.$(envget DOMAIN)/login and check the widget appears. If Cloudflare is ever unreachable, logins still work (and are audited) — the lockout and fail2ban remain the hard limits."
}
step_rotate_secret() {
  yesno "Rotate the portal's session-signing secret? Every portal session (all users, all devices) is logged out immediately; Kobo/OPDS/Kindle are unaffected." || return 0
  envset LIBRARIAN_SECRET "$(openssl rand -hex 32)" \
    || { msg "Could not write $ENV_FILE, so the secret was NOT rotated and every session is still valid."; return 1; }
  # This is the step an admin reaches for after a leaked password, and a swallowed restart
  # failure means the OLD gunicorn keeps signing and accepting the cookies it is meant to kill.
  # Confirm the new process answers before claiming anyone was logged out.
  if ! restart_portal || ! wait_for http://127.0.0.1:8090/healthz 30; then
    msg "The new secret is in $ENV_FILE but the portal did NOT come back up, so the OLD secret is still signing and accepting session cookies: NOBODY has been logged out yet. Fix the portal (Operations -> Logs -> librarian) and run this again."
    return 1
  fi
  msg "Portal secret rotated and the portal restarted (confirmed on /healthz). Every portal session is dead; users simply sign in again."
}
menu_ops() {
  while true; do
    ch=$(whiptail --title "Operations" --menu "Ephemera: $([ "$(envget EPHEMERA_ENABLED)" = true ] && echo ON || echo off)   FlareSolverr: $(solver_on && echo running || echo off)   (the list scrolls)" 24 88 15 \
      T "Self-test: containers, endpoints, configs, firewall, TLS, isolation, backups" \
      S "Status: all containers" \
      V "Restart / stop / start ONE service" \
      L "Logs: follow a service" \
      D "Advanced settings: lockout, upload caps, mail, disk, retention" \
      C "Check for updates: current vs newest image tags" \
      U "Update: backup first, deploy this code, pull/bump tags, health gate, rollback" \
      B "Backups: configure / change" \
      K "Rotate the backup repository password (restic key add + remove)" \
      A "Alerts: phone notification (ntfy / webhook)" \
      R "Backup restore test" \
      W "Restore from backup: pick a snapshot, everything or config + databases" \
      F "Restore a SINGLE file from a snapshot (beside the stack, nothing stops)" \
      M "Monitoring: Uptime Kuma (set up / repair monitors) + external check" \
      G "FlareSolverr: $([ "$(envget FLARESOLVERR_ENABLED)" = true ] && echo "ON for Shelfmark — turn off" || echo "off for Shelfmark — turn on") (challenge solver)" \
      E "Ephemera: enable (Tailscale-only, unmaintained upstream — read notice)" \
      X "Ephemera: disable" \
      J "Canary journey: a test reader's path twice a day ($([ -f "$ETC/systemd/system/bookstack-canary.timer" ] && echo on || echo off))" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in T) step_selftest || true;; S) step_status || true;; V) step_service || true;; L) step_logs || true;;
      D) step_advanced || true;; C) step_check_updates || true;; U) step_update || true;;
      B) step_backup || true;; K) step_restic_rotate || true;; A) step_alerts || true;; R) step_restore_test || true;;
      W) step_restore || true;; F) step_restore_file || true;; M) step_monitoring || true;;
      G) step_flaresolverr || true;; E) step_ephemera || true;; X) step_ephemera_off || true;; J) step_canary || true;; 0) return 0;; esac
  done
}
# ---------- L08: the canary journey ----------
# Two hidden accounts (CANARY_USERS: left out of every user list) and scripts/synthetic.py on a
# timer at 06:20 and 18:20. Their passwords live in /etc/bookstack/canary.env (root, 0600),
# never in .env, which the portal container reads.
CANARY_ENV_REL=bookstack/canary.env
CANARY_NAMES="canary-a canary-b"
install_canary_units(){
  local u="$ETC/systemd/system"; mkdir -p "$u"
  cat > "$u/bookstack-canary.service" << UNIT
[Unit]
Description=Bookstack canary journey (a test reader's path)
After=docker.service
[Service]
Type=oneshot
Environment=STACK_DIR=$STACK_DIR
Environment=CANARY_ENV=$ETC/$CANARY_ENV_REL
ExecStart=/usr/bin/python3 $STACK_DIR/scripts/synthetic.py
TimeoutStartSec=1800
UNIT
  cat > "$u/bookstack-canary.timer" << 'UNIT'
[Unit]
Description=Bookstack canary journey, twice a day
[Timer]
OnCalendar=*-*-* 06,18:20:00
RandomizedDelaySec=10m
Persistent=false
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload && systemctl enable --now bookstack-canary.timer
}
step_canary(){
  need DOMAIN || return 1
  local cf="$ETC/$CANARY_ENV_REL" n pw out
  if [ -f "$ETC/systemd/system/bookstack-canary.timer" ]; then
    local ch; ch=$(whiptail --title "Canary journey" --menu "The canary journey runs at 06:20 and 18:20. /admin shows every run." 14 76 3 \
      R "Run it now (takes a minute or two; watch the output)" \
      D "Turn it off and remove the two canary accounts" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      R) clear; env STACK_DIR="$STACK_DIR" CANARY_ENV="$cf" python3 "$STACK_DIR/scripts/synthetic.py"; local rc=$?
         msg "$([ $rc = 0 ] && echo "The canary journey PASSED." || echo "The canary journey FAILED (see the output above; the alert channel was told).")\n\n/admin -> Canary journey shows the history and the import time."; return $rc;;
      D) systemctl disable --now bookstack-canary.timer >/dev/null 2>&1 || true
         rm -f "$ETC/systemd/system/bookstack-canary.timer" "$ETC/systemd/system/bookstack-canary.service"; systemctl daemon-reload >/dev/null 2>&1 || true
         for n in $CANARY_NAMES; do docker exec librarian python -m cwa remove-user "$n" >/dev/null 2>&1 || true; done
         rm -f "$cf"; envset CANARY_USERS ""; restart_portal_ok || true
         monitoring_refresh >/dev/null 2>&1 || true
         msg "The canary journey is off and its two accounts are removed."; return 0;;
      *) return 0;;
    esac
  fi
  yesno "Turn on the canary journey?\n\nTwice a day two hidden test accounts (canary-a, canary-b) do what a family member does: log in, upload a small generated book (through Cloudflare), wait for Calibre-Web to import it with the owner tag, download it, check the OTHER account cannot, read OPDS and the Kobo endpoint through Cloudflare, and log in to Shelfmark. The book is removed again.\n\nA failure alerts you; /admin shows every run and how long the import took (it slows down before Calibre-Web fails).\n\nThe accounts never appear in user lists. They are ordinary, isolated readers." || return 0
  ( umask 077; mkdir -p "$(dirname "$cf")"; : > "$cf.new" ) || { msg "Could not write $cf. Nothing was changed."; return 1; }
  for n in $CANARY_NAMES; do
    pw=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-24)
    docker exec librarian python -m cwa remove-user "$n" >/dev/null 2>&1 || true   # a leftover from an earlier setup
    out=$(printf '%s\n' "$pw" | docker exec -i librarian python -m cwa add-user "$n" --password-stdin --no-abs 2>&1) \
      || { rm -f "$cf.new"; msg "Could not create the canary account $n:\n$out"; return 1; }
    printf '%s=%s\n%s_PW=%s\n' "CANARY_$([ "$n" = canary-a ] && echo A || echo B)" "$n" "CANARY_$([ "$n" = canary-a ] && echo A || echo B)" "$pw" >> "$cf.new"
  done
  mv -f "$cf.new" "$cf"; chmod 600 "$cf"; chown root:root "$cf" 2>/dev/null || true
  envset CANARY_USERS "$(printf '%s' "$CANARY_NAMES" | tr ' ' ',')"
  restart_portal_ok || true
  envdefault KUMA_PUSH_CANARY "$(openssl rand -hex 16)" || true
  install_canary_units
  monitoring_refresh >/dev/null 2>&1 || true
  local kto; kto=$(ask "Optional: once a week (Sunday morning run) the canary also sends its book to a Kindle address, proving Send-to-Kindle end to end. Put YOUR own Kindle address here (it must allow the sender in Amazon's approved list). Blank = skip." "$(envget CANARY_KINDLE_TO)") || kto="$(envget CANARY_KINDLE_TO)"
  envset CANARY_KINDLE_TO "$kto"
  if yesno "The canary journey is on (06:20 and 18:20).\n\nRun it once now?"; then
    clear; env STACK_DIR="$STACK_DIR" CANARY_ENV="$cf" python3 "$STACK_DIR/scripts/synthetic.py" \
      && msg "The canary journey PASSED. /admin -> Canary journey shows it." \
      || msg "The canary journey FAILED on its first run (see the output above). Fix the cause, then Operations -> Canary journey -> Run it now."
  fi
}
main_menu() {
  local banner h
  while true; do
    # a failed post-reboot self-test belongs on the FIRST screen, not three menus down; the box
    # grows so the extra lines cannot push the menu out of an 80x24 terminal
    banner=$(postboot_banner); h=18; [ -n "$banner" ] && h=21
    choice=$(whiptail --title "Bookstack v$BOOKSTACK_VERSION — $(envget DOMAIN)" --menu "Private, per-user book library. Everything is configured from here.$banner" "$h" 84 6 \
      I "Install & deploy   (Quick install, system, Tailscale, Cloudflare, deploy, backups, alerts)" \
      U "Users & devices    (add users, Kindle address, Kobo link, passwords, isolation)" \
      L "Library            (formats & conversion, mail, sources, Shelfmark, intake, torrents)" \
      S "Security           (Authelia SSO+2FA, SSH lock, fail2ban, mail auth)" \
      O "Operations         (self-test, status, logs, update, backups, restore, Ephemera)" \
      0 "Exit" 3>&1 1>&2 2>&3) || exit 0
    case "$choice" in I) menu_install || true;; U) menu_users || true;; L) menu_library || true;; S) menu_security || true;; O) menu_ops || true;; 0) exit 0;; esac
  done
}

if [ "${BOOKSTACK_LIB:-0}" != 1 ]; then
  main_menu
fi
