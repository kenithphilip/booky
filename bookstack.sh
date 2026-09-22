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
IMG_SHELFMARK=ghcr.io/calibrain/shelfmark:v1.3.7 IMG_QBIT=lscr.io/linuxserver/qbittorrent:5.2.3
IMG_KUMA=louislam/uptime-kuma:1 IMG_AUTHELIA=authelia/authelia:4.39.28 IMG_FLARESOLVERR=ghcr.io/flaresolverr/flaresolverr:v3.5.2"
CADDY_BASE=caddy:2.11.4               # used for `caddy hash-password`; same base as caddy/Dockerfile

# BOOKSTACK_LIB=1 sources this file for tests without running anything.
if [ "${BOOKSTACK_LIB:-0}" != 1 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash $0"; exit 1; }
  command -v whiptail >/dev/null || { apt-get update -qq; apt-get install -y -qq whiptail; }
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
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then
    raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'
    raw="${raw//"$bs$q"/$q}"
  fi
  printf '%s' "$raw"
}
envset() {
  mkdir -p "$STACK_DIR"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  local v="$2" bs=\\ q=\'
  v="${v//"$q"/$bs$q}"
  # umask 077 in a subshell: the temp file holds every secret and must be 0600 from its first byte
  ( umask 077; rm -f "$ENV_FILE.tmp"; { grep -vE "^$1=" "$ENV_FILE" || true; printf "%s='%s'\n" "$1" "$v"; } > "$ENV_FILE.tmp" )
  mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  [ "$(id -u)" -eq 0 ] && chown root:root "$ENV_FILE"   # never readable by the containers' uid 1000
  return 0
}
envdefault(){ [ -n "$(envget "$1")" ] || envset "$1" "$2"; }
img(){ # IMG_X -> value from .env, else the pinned default
  local v kv; v=$(envget "$1"); [ -n "$v" ] && { printf '%s' "$v"; return 0; }
  for kv in $IMG_DEFAULTS; do [ "${kv%%=*}" = "$1" ] && printf '%s' "${kv#*=}"; done; }
need()   { for v in "$@"; do [ -n "$(envget "$v")" ] || { msg "Missing $v. Run 'Configure' first."; return 1; }; done; }
cf()     { curl -fsS -X "$1" "$CF_API$2" -H "Authorization: Bearer $(envget CF_API_TOKEN)" -H "Content-Type: application/json" "${@:3}"; }
# qBittorrent is opt-in (compose profile "torrents", Library -> Torrents): every compose call
# that starts or stops the stack carries the profile while it is enabled.
torrents_on(){ [ "$(envget TORRENTS_ENABLED)" = true ]; }
compose_profiles(){ if torrents_on; then printf '%s\n' --profile torrents; fi; }
compose(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose ${p[@]+"${p[@]}"} "$@"); }
composeA(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose -f docker-compose.yml -f docker-compose.authelia.yml ${p[@]+"${p[@]}"} "$@"); }
composeE(){ local p; mapfile -t p < <(compose_profiles); (cd "$STACK_DIR" && docker compose -f docker-compose.yml -f docker-compose.ephemera.yml ${p[@]+"${p[@]}"} "$@"); }
lib()    { docker exec -i librarian python -m cwa "$@"; }          # user/device management lives in the portal image
absctl() { docker exec -i librarian python -m abs "$@"; }          # Audiobookshelf automation (needs ABS_TOKEN after setup)
abs_ready(){ [ -n "$(envget ABS_TOKEN)" ]; }
cwa_sql(){ docker exec -i calibre-web sqlite3 /config/cwa.db "$1"; } # CWA's own settings DB
running(){ docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }
portal_up(){ running librarian; }
restart_portal(){ compose up -d librarian >/dev/null 2>&1 || true; }
admin_user(){ local u; u=$(envget ADMIN_USER); printf '%s' "${u:-admin}"; }   # the chosen CWA admin name (C10)
render_caddyfile(){
  # An empty hash would render "admin " inside basic_auth: Caddy rejects the config and every
  # site on the box stays down. Refuse here, where the cause is obvious.
  case "$(envget ADMIN_HASH)" in '$2'*|'$argon2'*) ;;
    *) msg "ADMIN_HASH is missing or invalid; the admin-gate password must be set first (Install -> Configure). Caddyfile NOT rendered."; return 1;; esac
  # the admin sites' tailnet_only matcher needs the tailnet IP; empty = a config error for every site
  [ -n "$(envget TAILSCALE_IP)" ] || { msg "TAILSCALE_IP is empty; run Install -> Tailscale (or Configure, which sets a placeholder) first. Caddyfile NOT rendered."; return 1; }
  local f="$STACK_DIR/caddy/Caddyfile" bind; bind=$(envget BIND_IP); bind="${bind:-$(envget PUBLIC_IP)}"
  [ -n "$bind" ] || { msg "Neither BIND_IP nor PUBLIC_IP is set; run Install -> Configure first. Caddyfile NOT rendered."; return 1; }
  # optional vhosts: their DNS name and upstream only exist while the feature is enabled
  local drop=()
  torrents_on || drop+=(-e '/# @TORRENTS_BEGIN@/,/# @TORRENTS_END@/d')
  [ "$(envget EPHEMERA_ENABLED)" = true ] || drop+=(-e '/# @EPHEMERA_BEGIN@/,/# @EPHEMERA_END@/d')
  # keep the last good file: apply_caddy puts it back when the new one does not validate
  [ -s "$f" ] && cat "$f" > "$f.prev"
  # written in place (never mv): the running container bind-mounts this exact inode
  sed ${drop[@]+"${drop[@]}"} \
      -e "s|@@DOMAIN@@|$(envget DOMAIN)|g" -e "s|@@ADMIN_EMAIL@@|$(envget ADMIN_EMAIL)|g" \
      -e "s|@@BIND_IP@@|$bind|g" -e "s|@@PUBLIC_IP@@|$bind|g" -e "s|@@TAILSCALE_IP@@|$(envget TAILSCALE_IP)|g" \
      -e "s|@@ADMIN_HASH@@|$(envget ADMIN_HASH)|g" \
      "$STACK_DIR/caddy/Caddyfile.template" > "$f"
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
    getent passwd "$STACK_USER" >/dev/null || useradd -m -u 1000 -s /bin/bash "$STACK_USER" \
      || { msg "Could not create a user with uid 1000 ('$STACK_USER'). Create one by hand and run System again."; return 1; }
  else
    useradd -m -u 1000 -s /bin/bash "$STACK_USER" \
      || { msg "Could not create the '$STACK_USER' user (uid 1000). Run System again after checking 'getent passwd 1000'."; return 1; }
  fi
  STACK_HOME=$(getent passwd "$STACK_USER" | cut -d: -f6)
  # uid 1000 is what every container runs as: membership in the docker group would turn any
  # container escape into root. Root runs compose; the account needs no docker access.
  gpasswd -d "$STACK_USER" docker >/dev/null 2>&1 || true
}
# Ownership model (C14): $STACK_DIR, code, compose files, scripts and rendered configs belong to
# root (root executes them); only the data directories the containers write are uid 1000.
DATA_DIRS="caddy/data caddy/config cwa abs qbt downloads library kuma librarian/state shelfmark ephemera authelia"
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
  cp -f "$SRC_DIR/scripts/"*.sh "$SRC_DIR/authelia/inject-gate.py" "$SRC_DIR/authelia/caddy-gate.snippet" "$STACK_DIR/scripts/"
  rsync -a --delete --exclude state --exclude tests --exclude __pycache__ "$SRC_DIR/librarian/" "$STACK_DIR/librarian/"
  rm -rf "$STACK_DIR/configs"; cp -R "$SRC_DIR/configs" "$STACK_DIR/configs"
  # root-owned and not writable by uid 1000 (cron, systemd and this TUI run them as root)
  local d
  for d in caddy/Dockerfile caddy/Caddyfile.template scripts configs docker-compose.yml docker-compose.authelia.yml docker-compose.ephemera.yml; do
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
# removed or re-passworded user stays signed in until the container restarts (J07).
restart_shelfmark(){ compose restart shelfmark >/dev/null 2>&1 || true; prune_shelfmark_placeholder; }
inject_authelia_gate(){ # per-host bypass lists live in inject-gate.py (reads first, writes second)
  python3 "$STACK_DIR/scripts/inject-gate.py" "$STACK_DIR/caddy/Caddyfile" "$STACK_DIR/scripts/caddy-gate.snippet"
}
# Caddy's admin API is a unix socket (no TCP port on the host); fall back to a restart.
reload_caddy(){ compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --address unix//run/caddy-admin.sock 2>/dev/null || compose restart caddy; }
route_src_ip(){ ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}'; }
public_ip(){ curl -4 -fsS -m 10 https://api.ipify.org 2>/dev/null || route_src_ip; }
ip_on_host(){ ip -o addr show 2>/dev/null | grep -qF " $1/"; }
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
# Caddy binds the tailnet IP too: if tailscaled restarts or loses its address, a strict bind
# would make Caddy fail to (re)start and take the PUBLIC sites down with it.
net.ipv4.ip_nonlocal_bind = 1
net.ipv6.ip_nonlocal_bind = 1
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
  ufw --force enable >/dev/null
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
SSH
  if ! sshd -t 2>/dev/null; then
    rm -f "$d/$SSH_DROPIN"
    sshnote="WARNING: sshd rejected the configuration (sshd -t), so the hardening was NOT applied and SSH was not reloaded. Check /etc/ssh/sshd_config."; return 1
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  if sshd -T 2>/dev/null | grep -qi '^passwordauthentication no'; then
    sshnote="SSH is now key-only (verified with sshd -T)."
  else
    sshnote="WARNING: SSH password login is STILL ON although $SSH_DROPIN says no — another file in /etc/ssh overrides it (check: sshd -T | grep -i passwordauth)."; return 1
  fi
}
step_system() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get -y -qq upgrade
  # cron runs the disk watchdog and the Cloudflare allowlist refresh; minimal images lack it
  apt-get -y -qq install ca-certificates curl jq ufw unattended-upgrades openssl rsync python3 systemd-timesyncd cron \
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
  mkdir -p "$STACK_DIR"/library/{books,ingest,audiobooks,podcasts,staging,dropbox}
  mkdir -p "$STACK_DIR"/{kuma/data,librarian/state,shelfmark/config,ephemera/data,ephemera/downloads}
  own_data_dirs
}

# ---------- 2. tailscale ----------
step_tailscale() {
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
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
ensure_admin_name(){ # rename the CWA admin row to ADMIN_USER when it still has an older name (factory 'admin')
  # 0 = the admin is called ADMIN_USER now, 1 = could not tell (portal down), 2 = rename failed
  local want list src; want=$(admin_user)
  list=$(users_json) || return 1; [ -n "$list" ] || return 1
  printf '%s' "$list" | python3 -c 'import sys,json; sys.exit(0 if any(x["name"]==sys.argv[1] for x in json.load(sys.stdin)) else 1)' "$want" 2>/dev/null && return 0
  for src in "$(envget ADMIN_USER_PREV)" admin; do
    [ -n "$src" ] && [ "$src" != "$want" ] || continue
    if printf '%s' "$list" | python3 -c 'import sys,json; sys.exit(0 if any(x["name"]==sys.argv[1] and x["is_admin"] for x in json.load(sys.stdin)) else 1)' "$src" 2>/dev/null; then
      lib rename-user "$src" "$want" >/dev/null 2>&1 && { envset ADMIN_USER_PREV ""; return 0; }
      return 2                     # the old admin exists but the rename failed
    fi
  done
  return 1
}
step_configure() {
  d=$(ask "Domain (zone in Cloudflare):" "$(envget DOMAIN)")                       ; [ -n "$d" ] || return 1
  e=$(ask "Admin email (for Let's Encrypt notices):" "$(envget ADMIN_EMAIL)")     ; [ -n "$e" ] || return 1
  tz=$(ask "Timezone:" "$(envget TZ)") || tz="$(envget TZ)"                       ; [ -n "$tz" ] || tz=UTC
  tok=$(askpw "Cloudflare API token (Zone:Read, DNS:Edit, Zone Settings:Edit, Config Rules:Edit, Cache Rules:Edit, Firewall Services:Edit). Leave blank to keep existing.") ; [ -n "$tok" ] || tok="$(envget CF_API_TOKEN)"
  [ -n "$tok" ] || { msg "A Cloudflare API token is required."; return 1; }
  if [ -n "$(envget ADMIN_HASH)" ] && yesno "Keep the existing admin-gate password (for the Tailscale-only admin tools)?"; then pw=""; else
    pw=$(askpw2 "Password for the admin gate in front of the Tailscale-only admin tools (qBittorrent, Ephemera):") || return 1; fi
  local cur_au au sugg dtok
  cur_au=$(envget ADMIN_USER); sugg=$(suggest_admin_name "$e")
  au=$(ask "Username of YOUR admin account (Calibre-Web, the portal and Shelfmark). Avoid 'admin': bots guess it, and Calibre-Web then locks that name for the day." "${cur_au:-$sugg}") || au="${cur_au:-$sugg}"
  au=$(printf '%s' "$au" | tr 'A-Z' 'a-z'); [ -n "$au" ] || au="${cur_au:-$sugg}"
  valid_username "$au" || { msg "'$au' is not a valid username (2-32 of a-z 0-9 . _ -). Nothing was changed."; return 1; }
  dtok=$(askpw "Optional, recommended: a SECOND Cloudflare token with only Zone -> DNS -> Edit on this zone. Caddy uses it for certificates, so the powerful main token never sits in a container.\n\nBlank = keep the current one (or reuse the main token).") || dtok=""

  local old_tok; old_tok=$(envget CF_API_TOKEN)
  envset DOMAIN "$d"; envset ADMIN_EMAIL "$e"; envset TZ "$tz"
  envset PUID 1000; envset PGID 1000
  envset CF_API_TOKEN "$tok"
  # Caddy's certificate token (C7): the separate one when given; else it follows the main token
  if [ -n "$dtok" ]; then envset CF_DNS_TOKEN "$dtok"
  elif [ -z "$(envget CF_DNS_TOKEN)" ] || [ "$(envget CF_DNS_TOKEN)" = "$old_tok" ]; then envset CF_DNS_TOKEN "$tok"; fi
  if [ -n "$cur_au" ] && [ "$cur_au" != "$au" ]; then envset ADMIN_USER_PREV "$cur_au"; fi
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
  for kv in SRC_GUTENBERG:true SRC_STANDARD:false SRC_ARCHIVE:true SRC_LIBRIVOX:true SRC_MYCATALOG:false TORRENTS_ENABLED:false \
            APPROVALS_REQUIRED:false SHELFMARK_LANGUAGE:en SHELFMARK_CONCURRENCY:1 EPHEMERA_ENABLED:false AUTHELIA_ENABLED:false; do
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
    case "$h" in '$2'*|'$argon2'*) envset ADMIN_HASH "$h";;
      *) msg "Could not hash the admin-gate password (docker run $CADDY_BASE caddy hash-password failed). Nothing was changed; check Docker and run Configure again."; return 1;; esac
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
    [ "$rc" = 2 ] && msg "Could not rename the Calibre-Web admin account to '$au' (Operations -> Logs -> librarian). Deploy tries again."
  fi
  msg "Configuration written to $STACK_DIR.\n\nPublic IP (DNS): $(envget PUBLIC_IP)   Caddy binds: $(envget BIND_IP)\nTailscale IP: $(envget TAILSCALE_IP)   Admin account: $(admin_user)\n\nRequests from family members are fulfilled at once (no admin approval). Turn approvals on under Library -> Sources if you want to review each request.\n\n(If Tailscale IP is 127.0.0.1, run the Tailscale step then Configure again.)"
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
  local public="books audio request shelf auth" private="monitor"
  torrents_on && private="$private dl"
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && private="$private ephemera"
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

  curl -fsS https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem \
       -o "$STACK_DIR/caddy/cf-origin-pull-ca.pem" || { msg "Could not download Cloudflare's origin-pull CA. Check the network and run this step again."; return 1; }
  chown root:root "$STACK_DIR/caddy/cf-origin-pull-ca.pem"; chmod 644 "$STACK_DIR/caddy/cf-origin-pull-ca.pem"

  "$STACK_DIR/scripts/cf-ips.sh" >/dev/null || cf_fail "firewall allowlist (scripts/cf-ips.sh) could not be applied"
  write_cron bookstack-cfips "15 3 * * *" "$STACK_DIR/scripts/cf-ips.sh >/dev/null 2>&1"

  # verify what matters instead of trusting the PATCH/PUT answers: with SSL not strict or
  # origin pulls off, Caddy's client-certificate check rejects Cloudflare and every site fails
  cf_setting_check ssl strict
  cf_setting_check tls_client_auth on
  for h in $public; do cf_dns_check "$h" "$pub" true; done
  for h in $private; do cf_dns_check "$h" "$ts" false; done
  if [ -n "$CF_FAILS" ]; then
    big "Cloudflare: NOT fully configured" "These items failed:$CF_FAILS

Fix the token permissions (Zone:Read, DNS:Edit, Zone Settings:Edit, Config Rules:Edit,
Cache Rules:Edit, Firewall Services:Edit) or set them in the dashboard, then run this step again.
The public sites will not work while SSL is not 'Full (strict)' or Authenticated Origin Pulls is off."
    return 1
  fi
  msg "Cloudflare configured (read back and verified):\n- DNS: books/audio/request/shelf/auth -> proxied (orange); $private -> tailnet IP only\n- SSL Full (strict), TLS 1.2+, HTTPS forced, Authenticated Origin Pulls ON\n- Browser Integrity Check, e-mail obfuscation and Rocket Loader OFF (they break e-readers and the portal)\n$cachenote\n- Firewall allows web ports only from Cloudflare (auto-refreshed nightly)\n\nIn the dashboard: Security > WAF > Managed rules: ON.\nDo NOT enable Bot Fight Mode: it challenges Kobo/OPDS/KOReader/Audiobookshelf apps, cannot be exempted on the Free plan, and the devices fail silently. Leave it OFF."
}

# ---------- 5. deploy ----------
apply_library_defaults() {
  # Secure + sane defaults inside the apps, so nothing has to be clicked in a GUI:
  # CWA: registration off, Kobo sync on, store proxy off; convert on ingest to EPUB, keep
  # per-user copies separate (new_record), fix EPUBs for Kindle.
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
    if printf '%s' "$apw" | lib passwd "$au" --password-stdin >/dev/null 2>&1; then envset ADMIN_PW_SET true
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
  clear; echo "Building images and starting containers (first run takes a few minutes)..."
  # BUILD_VERSION is baked into the images (compose build arg, default "dev"); the portal
  # reports it on /healthz and Self-test compares it with $STACK_DIR/.version (J03).
  BUILD_VERSION="$(build_version)" compose build --pull caddy librarian || { msg "Image build failed (caddy/librarian). See the output above; nothing was started."; return 1; }
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
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && { composeE up -d ephemera flaresolverr || echo "(Ephemera did not start; see Operations -> Logs)"; }
  echo "Waiting for the portal..."; wait_for http://127.0.0.1:8090/healthz 45 || true
  sleep 5
  prune_shelfmark_placeholder    # J35: drop Shelfmark's empty "{User}" template folder
  apply_library_defaults || echo "(could not apply library defaults yet — run Users → Repair later)"
  d=$(envget DOMAIN)
  set_admin_password || return 1
  ru=$(envget ABS_ROOT_USER); absnote="Audiobookshelf: root user '${ru:-root}' (Library -> Audiobookshelf re-runs setup)"
  if ! abs_initialised; then
    msg "Audiobookshelf has no root user yet. Whoever opened https://audio.$d first would become its administrator, so it is set up now, before the site goes public."
    step_abs_setup || yesno "Audiobookshelf is still uninitialised: the first visitor of audio.$d would become root. Start Caddy anyway (NOT recommended)?" || { msg "Caddy was not started. Run Library -> Audiobookshelf, then Deploy again."; return 1; }
  fi
  compose up -d caddy || { msg "Caddy failed to start. Operations -> Logs -> caddy."; return 1; }
  apply_caddy || true     # an already-running Caddy is not recreated by `up`: validate + reload the new file
  install_disk_watch
  local privline="https://monitor.$d  (Uptime Kuma)" qline=""
  if torrents_on; then
    qpw=$(docker logs qbittorrent 2>&1 | grep -oE 'temporary password.*: *[A-Za-z0-9]+' | tail -1 | awk '{print $NF}')
    privline="https://dl.$d  (qBittorrent)   $privline"; qline="  qBittorrent: admin / ${qpw:-<see: docker logs qbittorrent>}"
  fi
  adminline="the password you just set"; [ -n "${ADMIN_PW_GENERATED:-}" ] && adminline="GENERATED password: $ADMIN_PW_GENERATED  (change it under Users -> Reset password)"
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

Already applied for you: public registration OFF, Kobo sync ON, convert-to-EPUB on import,
per-user copies kept separate, CWA's Kindle EPUB fixer OFF (the portal applies the Kindle fixes when it mails a book; on import the fixer would strip the owner tag from comics), CWA's duplicate file copies OFF,
hourly disk watchdog (alerts at 85 %, stops downloaders at 95 %).

Next: Users & devices -> Add user (isolated account + Kobo link + ABS login in one go),
Library -> Mail for Send-to-Kindle from the portal, Install -> Backups, Install -> Alerts, Operations -> Self-test."
}

# ---------- 6. backups ----------
RESTIC_ENV_FILE_REL=bookstack/restic.env   # under $ETC; the scripts read /etc/bookstack/restic.env
restic_env(){ printf '%s' "$ETC/$RESTIC_ENV_FILE_REL"; }
write_restic_env() { # prompts for repository / password / S3 keys and writes restic.env (0600 root)
  local repo rpw k1="" k2="" cur
  command -v restic >/dev/null || apt-get -y -qq install restic
  cur=$(grep -E '^RESTIC_REPOSITORY=' "$(restic_env)" 2>/dev/null | cut -d= -f2- || true)
  repo=$(ask "restic repository (e.g. s3:s3.eu-central-003.backblazeb2.com/my-bucket, sftp:user@host:/path, or /mnt/backup):" "$cur") || return 1
  [ -n "$repo" ] || return 1
  rpw=$(askpw2 "Encryption password for the backup repository (STORE THIS SAFELY — without it backups are unreadable):") || return 1
  if [[ "$repo" == s3:* ]]; then
    k1=$(ask "S3 / B2 key ID:") || return 1; k2=$(askpw "S3 / B2 application key:") || return 1
  fi
  mkdir -p "$ETC/bookstack"
  # shell-quoted (%q): the file is sourced by bash, so a password with $, spaces, & or quotes
  # must survive intact instead of being expanded or executed
  { printf 'RESTIC_REPOSITORY=%q\nRESTIC_PASSWORD=%q\nSTACK_DIR=%q\n' "$repo" "$rpw" "$STACK_DIR"
    [ -n "$k1" ] && printf 'AWS_ACCESS_KEY_ID=%q\n' "$k1"; [ -n "$k2" ] && printf 'AWS_SECRET_ACCESS_KEY=%q\n' "$k2"; true; } > "$(restic_env)"
  chmod 600 "$(restic_env)"; chown root:root "$(restic_env)"
  # prove it reads back exactly before anyone relies on it
  local back; back=$(bash -c 'set -a; . "$1"; printf "%s" "$RESTIC_PASSWORD"' _ "$(restic_env)")
  [ "$back" = "$rpw" ] || { rm -f "$(restic_env)"; msg "Internal error: the backup password did not round-trip through restic.env. Nothing saved."; return 1; }
}
install_backup_units() { # backup nightly at 01:00 (before the 04:30 reboot window), restore test on the 1st at 13:00 (never
  # overlapping the backup's repository lock), both alert on failure
  local u="$ETC/systemd/system"; mkdir -p "$u"
  cat > "$u/bookstack-alert@.service" << UNIT
[Unit]
Description=Bookstack alert for %i
[Service]
Type=oneshot
ExecStart=$STACK_DIR/scripts/alert.sh 'Bookstack: %i FAILED' 'see: journalctl -u bookstack-%i' high
UNIT
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
  systemctl daemon-reload && systemctl enable --now bookstack-backup.timer bookstack-restore-test.timer
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
restic_run(){ ( set -a; . "$(restic_env)"; set +a; restic "$@" ); }
step_backup() {
  write_restic_env || return 1
  # The repository is created here and only here. backup.sh never runs `restic init`: with a local
  # path on a volume that failed to mount it would quietly start a new repository on the root disk.
  if ! restic_run cat config >/dev/null 2>&1; then
    restic_run init || { msg "Could not open or create the backup repository. Check the address, keys and password, then run Backups again."; return 1; }
  fi
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

# ---------- 6a. alerts ----------
step_alerts() { # C1: one channel that reaches the admin even when the portal is down
  local cur url; cur=$(envget NOTIFY_WEBHOOK)
  url=$(ask "Where should server alerts go (failed backups, full disk, failed restore test)?\n\nFree default: the ntfy app (ntfy.sh) on your phone, subscribed to a SECRET topic, e.g.\n  https://ntfy.sh/bookstack-$(openssl rand -hex 6)\nAny ntfy-compatible webhook URL works. Blank = keep; type  none  to remove." "${cur:-https://ntfy.sh/bookstack-$(openssl rand -hex 6)}") || return 1
  [ -n "$url" ] || url="$cur"
  [ "$url" = none ] && url=""
  case "$url" in ""|https://*|http://*) ;; *) msg "That is not an http(s) URL. Nothing changed."; return 1;; esac
  envset NOTIFY_WEBHOOK "$url"; restart_portal
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
restore_fits(){ # snapshot-id: in-place restore needs (restore size - what is already here) + 1 GB
  local need have cur
  need=$(restic_run stats "$1" --mode restore-size --json 2>/dev/null | json 'd["total_size"]') || need=""
  [ -n "$need" ] || { echo "(could not read the snapshot's restore size; continuing)"; return 0; }
  have=$(df -Pk "$STACK_DIR" 2>/dev/null | awk 'NR==2{print $4}'); cur=$(du -sk "$STACK_DIR" 2>/dev/null | cut -f1)
  local want_k=$(( need / 1024 - ${cur:-0} + 1048576 ))
  RESTORE_NEED_GB=$(( (need / 1024 + 1048575) / 1048576 )); RESTORE_FREE_GB=$(( ${have:-0} / 1048576 ))
  [ "$want_k" -le "${have:-0}" ]
}
step_restore() {
  need DOMAIN || return 1
  [ -f "$(restic_env)" ] || { msg "No backup repository is configured on this machine yet: enter the SAME repository and password as the old server."; write_restic_env || return 1; }
  pick_snapshot || return 1
  local mode
  mode=$(whiptail --title "Restore: what" --menu "Snapshot $SNAP_DESC" 14 80 2 \
    full   "Everything: configs, databases AND the library / audiobooks (new server)" \
    config "Config + databases only (library files stay; e.g. roll back a bad update)" 3>&1 1>&2 2>&3) || return 1
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
      mkdir -p "$(dirname "$STACK_DIR/$rel")"
      cp -f "$STACK_DIR/.backup-snap/$snap" "$STACK_DIR/$rel"; rm -f "$STACK_DIR/$rel-wal" "$STACK_DIR/$rel-shm"
      echo "  restored DB $rel"
    done < "$STACK_DIR/.backup-snap/MANIFEST"
  fi
  [ -n "$pub" ] && envset PUBLIC_IP "$pub"; [ -n "$ts" ] && envset TAILSCALE_IP "$ts"; [ -n "$tz" ] && envset TZ "$tz"
  [ -n "$bind" ] && envset BIND_IP "$bind"
  # The snapshot carried the OLD server's copy of the code and templates; this checkout is the
  # version being deployed, so the code trees come from here (data and secrets stay restored).
  make_dirs; copy_code_trees; own_data_dirs
  render_caddy_all || { msg "Restored, but the Caddyfile could not be rendered; Caddy was NOT started. Fix it (Install -> Configure) then Install -> Deploy."; return 1; }
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && render_authelia_config
  if [ -n "$(envget CF_API_TOKEN)" ]; then echo "Pointing Cloudflare DNS at this server..."; step_cloudflare || echo "(Cloudflare step failed; run Install -> Cloudflare)"; fi
  install_backup_units
  echo "Starting the stack..."; stack_up_all || { msg "Restore copied the files but the stack did not start: Operations -> Logs."; return 1; }
  # after the stack: fail2ban's Caddy jails need caddy/data/access.log (not in the backup)
  command -v fail2ban-client >/dev/null && { render_fail2ban || true; systemctl restart fail2ban || true; }
  install_disk_watch
  msg "Restored $SNAP_DESC and started.\n\nRun Operations -> Self-test now. Users' Kobo links, Audiobookshelf accounts and Authelia logins came back with the databases; the Tailscale IP of this server is new (monitor./dl.)."
  offsite_checklist
}

# ---------- 7. lock SSH ----------
step_lock_ssh() {
  tailscale status >/dev/null 2>&1 || { msg "Tailscale is not running. Not locking SSH."; return 1; }
  yesno "This removes public SSH (port 22) and leaves it reachable only over Tailscale.\n\nConfirm you can ALREADY SSH to $(envget TAILSCALE_IP) (or 'tailscale ssh') from another machine before continuing." || return 1
  if ! ts_key_expiry_disabled; then
    yesno "$TS_EXPIRY_NOTE\n\nWith public SSH closed, an expired key locks you out of everything but the provider console.\n\nHave you disabled key expiry for THIS machine in the Tailscale admin console?" \
      || { msg "Do that first (admin console -> Machines -> ... -> Disable key expiry), then run this step again. SSH stays public for now."; return 1; }
  fi
  ufw --force delete allow 22/tcp >/dev/null 2>&1 || true
  envset SSH_LOCKED true      # System re-runs keep port 22 closed from now on
  msg "Public SSH closed. Use: ssh root@$(envget TAILSCALE_IP)\n\nYour VPS provider's web console remains a fallback. Re-running System keeps it closed; to reopen: ufw allow 22/tcp and set SSH_LOCKED='false' in $ENV_FILE."
}

# ---------- Q. quick install ----------
step_quick() {
  yesno "Quick install runs, in order: System -> Tailscale -> Configure -> Cloudflare -> Deploy -> Backups -> Alerts -> fail2ban,
then helps you add the first user and offers to lock SSH to Tailscale. Each step still asks what it needs. You can stop at any prompt and resume from the Install menu later.

Before starting you need: a Cloudflare zone for your domain + an API token (Zone:Read, DNS:Edit, Zone Settings:Edit, Config Rules:Edit, Cache Rules:Edit, Firewall Services:Edit), and a Tailscale account.

Start?" || return 0
  step_system || { msg "System step did not finish."; return 1; }
  if yesno "Set up Tailscale now? (needed for the admin tools and safe SSH; you will open a login URL)"; then step_tailscale || true; fi
  step_configure   || { msg "Configure did not finish. Resume from Install -> Configure."; return 1; }
  step_cloudflare  || { msg "Cloudflare step did not finish. Resume from Install -> Cloudflare."; return 1; }
  step_deploy      || { msg "Deploy did not finish. Resume from Install -> Deploy."; return 1; }   # includes Audiobookshelf setup
  if yesno "Configure encrypted nightly backups now?"; then step_backup || true; fi
  if yesno "Set up alerts now (phone notification when a backup fails or the disk fills)? Strongly recommended."; then step_alerts || true; fi
  step_fail2ban || true
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
  users_json | python3 -c '
import sys,json
u=json.load(sys.stdin)
print(f"{"user":18} {"role":6} {"isolation":22} kindle")
for x in u: print(f"{x["name"]:18} {"admin" if x["is_admin"] else "user":6} {("sees all" if x["is_admin"] else ("owner:"+x["name"] if x["isolated"] else "NOT ISOLATED")):22} {x.get("kindle_mail") or "-"}")
' | whiptail --title "Library users" --textbox /dev/stdin 22 90
}
step_user_add() {
  portal_up || { msg "The portal is not running (Install -> Deploy first)."; return 1; }
  u=$(ask "Username (lowercase letters/digits . _ - ; this is also their owner tag):") ; [ -n "$u" ] || return 1
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
  # J07: Shelfmark's session cookie is signed and only checked against app.db at login, so the
  # old password keeps working there until the container restarts.
  restart_shelfmark
  msg "Password updated for $u (portal, Calibre-Web, Shelfmark).$extra\n\nShelfmark was restarted, so $u is signed out there and must use the new password.\n\nOpen Calibre-Web and Audiobookshelf sessions on devices they are already signed in on may SURVIVE this reset until they expire — have $u sign out there (or restart those apps) if the reset was because of a lost or shared password.\n\nTell $u to change their password ONLY in the portal (https://request.$(envget DOMAIN) -> Devices). A change made on Calibre-Web's own profile page (/me) never reaches Audiobookshelf, so their audiobook login would silently keep the old password."
}
step_user_remove() {
  u=$(ask "Username to remove (their books and dropbox are kept):"); [ -n "$u" ] || return 1
  yesno "Remove login '$u' from the library, portal, Audiobookshelf and Authelia?\n\nTheir books stay in the library; requests they still had queued are failed by the portal." || return 0
  out=$(lib remove-user "$u" 2>&1) || { msg "Failed:\n$out"; return 1; }
  abs_ready && absctl remove-user "$u" >/dev/null 2>&1 || true
  local anote=""
  if [ -f "$STACK_DIR/authelia/users_database.yml" ] && grep -q "^  $u:" "$STACK_DIR/authelia/users_database.yml"; then
    authelia_remove_user "$u" && anote="\nTheir Authelia login was removed too." || anote="\nCould NOT remove their Authelia login: delete '$u' from $STACK_DIR/authelia/users_database.yml."
  fi
  # J07: without this the removed user's SIGNED Shelfmark cookie keeps working (Shelfmark reads
  # CWA's app.db only at login), so they could still search and download after removal.
  restart_shelfmark
  msg "Removed $u. Books tagged owner:$u remain in the library (admin sees them).$anote\n\nShelfmark was restarted, so any session $u still had open there is dead.\n\nA browser tab already signed in to Calibre-Web or Audiobookshelf may keep working until that session expires; Security -> 'Rotate the portal session secret' ends every portal session at once if you need that now."
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
menu_users() {
  while true; do
    ch=$(whiptail --title "Users & devices" --menu "Every user gets an isolated library account (owner tag) usable in the portal, Calibre-Web and Shelfmark, plus their own Kobo link and Kindle address." 20 86 9 \
      1 "List users (role, isolation, Kindle)" \
      2 "Add a user (account + isolation + Kobo link + Authelia if on)" \
      3 "Set a user's Kindle e-mail" \
      4 "Show / regenerate a user's Kobo sync link" \
      5 "Reset a user's password" \
      6 "Remove a user" \
      7 "Repair: re-apply isolation + secure defaults to everyone" \
      8 "How isolation works (guide)" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in
      1) step_user_list || true;; 2) step_user_add || true;; 3) step_user_kindle || true;; 4) step_user_kobo || true;;
      5) step_user_passwd || true;; 6) step_user_remove || true;; 7) step_user_repair || true;; 8) step_isolation || true;; 0) return 0;;
    esac
  done
}

# ---------- F. formats & conversion ----------
step_formats() {
  docker inspect -f '{{.State.Running}}' calibre-web 2>/dev/null | grep -q true || { msg "Calibre-Web is not running."; return 1; }
  cur=$(cwa_sql "SELECT auto_convert||'|'||auto_convert_target_format||'|'||auto_ingest_automerge||'|'||kindle_epub_fixer||'|'||IFNULL(auto_convert_retained_formats,'')||'|'||IFNULL(koreader_sync_enabled,0) FROM cwa_settings;" 2>/dev/null) || { msg "Could not read CWA settings."; return 1; }
  IFS='|' read -r _ _ c_merge _ c_keep c_ko <<< "$cur"
  # The conversion target is always EPUB: the one format every reader handles. Kobo devices get
  # KEPUB converted on the fly by Calibre-Web's Kobo sync and the portal's download; Kindles take
  # EPUB by mail. Other targets would break Send-to-Kindle or the Kobo path.
  fmt=epub
  msg "Imported books are converted to EPUB (fixed: Kobo gets KEPUB automatically, Kindle accepts EPUB by mail). Next: whether to convert at all, and which original formats to keep next to the EPUB."
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
  envset KOSYNC_ENABLED "$([ "$ko" = 1 ] && echo true || echo false)"; restart_portal
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
  restart_portal; wait_for http://127.0.0.1:8090/healthz 45 || true
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

# ---------- M. mail (SMTP for Send-to-Kindle) ----------
step_mail() {
  h=$(ask "SMTP host (blank disables Send-to-Kindle from the portal):" "$(envget SMTP_HOST)")
  if [ -z "$h" ]; then envset SMTP_HOST ""; restart_portal
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
  restart_portal; wait_for http://127.0.0.1:8090/healthz 30 || true
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
Non-EPUB files are converted to EPUB on import; the tag is added to the EPUB before import.

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
    STANDARD  "Standard Ebooks (public domain, polished)"     "$(cur SRC_STANDARD)" \
    ARCHIVE   "Internet Archive (filtered collections)"       "$(cur SRC_ARCHIVE)" \
    LIBRIVOX  "LibriVox (public-domain audiobooks)"           "$(cur SRC_LIBRIVOX)" \
    3>&1 1>&2 2>&3) || return 1
  for k in GUTENBERG STANDARD ARCHIVE LIBRIVOX; do
    case "$sel" in *"\"$k\""*|*"$k"*) envset "SRC_$k" true;; *) envset "SRC_$k" false;; esac
  done
  cols=$(ask "Internet Archive collections to search (comma-separated):" "$(envget IA_COLLECTIONS)")
  [ -n "$cols" ] && envset IA_COLLECTIONS "$cols"
  if yesno "Require admin approval for non-admin requests?\n\nNo (recommended for a family) = every request is fulfilled immediately.\nYes = you approve each one in the portal first."; then envset APPROVALS_REQUIRED true; else envset APPROVALS_REQUIRED false; fi
  local dq; dq=$(envget MAX_REQUESTS_PER_DAY)
  q=$(ask "Maximum requests per user per day (0 = unlimited; admins are never limited):" "${dq:-30}") || q="${dq:-30}"
  q=$(printf '%s' "$q" | tr -cd '0-9'); envset MAX_REQUESTS_PER_DAY "${q:-30}"
  if yesno "Add your OWN self-hosted catalog as a source?\n\nThis connects to an OPDS feed you host (Calibre content server, Calibre-Web, Kavita, Komga, BookLore...) with one username/password."; then
    url=$(ask "OPDS feed URL. Put {q} where the search term goes if your server supports it,\ne.g. https://books.mine.tld/opds/search/{q} — otherwise give the catalog feed URL:" "$(envget MYCATALOG_URL)")
    if [ -n "$url" ]; then
      nm=$(ask "Name to show users for this source:" "$(envget MYCATALOG_NAME)")
      us=$(ask "Username for the catalog (blank if none):" "$(envget MYCATALOG_USER)")
      ps=$(askpw "Password for the catalog (blank if none / keep):")
      envset MYCATALOG_URL "$url"; envset MYCATALOG_NAME "${nm:-My catalog}"
      envset MYCATALOG_USER "$us"; [ -n "$ps" ] && envset MYCATALOG_PASS "$ps"
      envset SRC_MYCATALOG true
    fi
  else
    envset SRC_MYCATALOG false
  fi
  restart_portal
  msg "Sources updated and the portal restarted."
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
  cp -f "$STACK_DIR/configs/fail2ban/caddy-auth.conf" "$STACK_DIR/configs/fail2ban/caddy-device-auth.conf" "$ETC/fail2ban/filter.d/"
  # The Audiobookshelf jail is scoped to the audio. vhost, so its filter carries the domain
  # (regex-escaped: a dot in a hostname must not match any character).
  local dom rep
  dom=$(envget DOMAIN); dom=$(printf '%s' "$dom" | sed 's/[.[\*^$]/\\&/g')   # regex-escape
  rep=$(printf '%s' "${dom:-[^\"]+}" | sed 's/[\&|]/\\&/g')                  # then sed-RHS-escape
  sed -e "s|@@DOMAIN@@|$rep|g" "$STACK_DIR/configs/fail2ban/caddy-abs-login.conf" > "$ETC/fail2ban/filter.d/caddy-abs-login.conf"
  [ "$on" = true ]
}
step_fail2ban() {
  apt-get -y -qq install fail2ban || { msg "Could not install fail2ban (apt)."; return 1; }
  if render_fail2ban; then note="Caddy jails ON: repeated failed logins (portal, Shelfmark, Authelia: 8 in 5 min) and Basic-auth guessing on the device paths /opds and /kosync (30 in 10 min) get the visitor's REAL IP banned at Cloudflare for 2 h (IP Access Rule on the zone).\nAudiobookshelf has no lockout of its own, so audio.$(envget DOMAIN) POST /login answering 401 is jailed separately: 10 in 10 min -> 1 h ban.\nThe API token must have Zone -> Firewall Services -> Edit; if bans fail, add it in Cloudflare and re-run this step."
  else note="Caddy jail OFF (no Cloudflare token/zone yet — run Configure + Cloudflare, then this step again)."; fi
  systemctl enable --now fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban || { msg "fail2ban did NOT start: journalctl -u fail2ban shows why."; return 1; }
  msg "fail2ban active: SSH brute-force jail (local firewall).\n\n$note\n\nCheck with: fail2ban-client status caddy-auth (also caddy-device-auth, caddy-abs-login)"
}

# ---------- 15. monitoring (Uptime Kuma) ----------
step_monitoring() {
  compose up -d uptime-kuma >/dev/null 2>&1 || true
  local d; d=$(envget DOMAIN)
  msg "Uptime Kuma runs at https://monitor.$d (Tailscale only). It uses the host network, so loopback monitors work.\n\nOpen it, create the admin account, then add HTTP(s) monitors for:\n  https://books.$d   https://audio.$d\n  https://request.$d https://shelf.$d\n  http://127.0.0.1:8090/healthz    http://127.0.0.1:8084/api/health\n  http://127.0.0.1:8084/api/auth/check  (keyword monitor: \"cwa\" = Shelfmark still uses library logins)\nAdd a notification (ntfy/email/Telegram) so a dead container or expired cert pings you.\n\nKuma runs ON this server, so it cannot tell you when the whole VPS is down: add a free external check too (healthchecks.io via Backups' ping URL, or UptimeRobot on https://request.$d)."
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
  render_authelia_config || { msg "Could not render Authelia's configuration."; return 1; }
  [ -f "$STACK_DIR/authelia/users_database.yml" ] || echo "users: {}" > "$STACK_DIR/authelia/users_database.yml"
  chown -R 1000:1000 "$STACK_DIR/authelia"
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone; then cf_dns auth "$(envget PUBLIC_IP)" true || true; fi
  clear; echo "Starting Authelia..."
  composeA up -d authelia || { msg "Authelia did not start (Operations -> Logs -> authelia). The gate was NOT enabled."; return 1; }
  if ! authelia_healthy 30; then
    composeA stop authelia >/dev/null 2>&1 || true
    msg "Authelia did not become healthy within 60 s (Operations -> Logs -> authelia). The gate was NOT enabled; the apps keep their own logins."; return 1
  fi
  # users BEFORE the gate: without one, every visitor meets a login nobody can pass
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
    msg "No Authelia user exists, so the gate was NOT enabled (nobody could log in). Add one with Security -> 'Authelia: add or reset a user', then enable again."; return 1
  fi
  authelia_healthy 30 || { composeA stop authelia >/dev/null 2>&1 || true; msg "Authelia is not healthy after adding users (Operations -> Logs -> authelia). The gate was NOT enabled."; return 1; }
  envset AUTHELIA_ENABLED true
  if ! render_caddy_all || ! apply_caddy; then
    envset AUTHELIA_ENABLED false; render_caddyfile >/dev/null 2>&1 && apply_caddy >/dev/null 2>&1
    composeA stop authelia >/dev/null 2>&1 || true
    msg "The Authelia gate could NOT be put in front of the sites (see the error above). Caddy keeps the previous configuration and Authelia stays disabled."; return 1
  fi
  local mailnote="Enrolment and reset codes are e-mailed through your SMTP server (Library -> Mail)."
  [ -n "$(envget SMTP_HOST)" ] || mailnote="No SMTP is configured, so enrolment/reset codes are NOT e-mailed: they are written to $STACK_DIR/authelia/notification.txt on this server (read it with: cat $STACK_DIR/authelia/notification.txt). Set up Library -> Mail to e-mail them instead."
  msg "Authelia enabled and the gate is live.\n\nTEST NOW: open https://books.$(envget DOMAIN) — you should meet the Authelia login before the app.\n\nUsers log in at https://auth.$(envget DOMAIN) and enrol TOTP or a passkey on first login. $mailnote\n\nIf anything misbehaves, 'Authelia: disable' removes the gate immediately."
}
step_authelia_off() {
  envset AUTHELIA_ENABLED false
  render_caddyfile || return 1
  apply_caddy || return 1
  composeA stop authelia >/dev/null 2>&1 || true
  msg "Gate removed — apps are back to their own logins. Authelia container stopped.\nRe-enable any time (your users and secrets are kept)."
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
         install -d -o 1000 -g 1000 "$STACK_DIR/library/dropbox/$u"
         msg "Dropbox ready: $STACK_DIR/library/dropbox/$u\n\nAnything dropped there (scp/rsync/Syncthing/WebDAV) is tagged owner:$u and ingested automatically." ;;
      2) step_intake_webhook || true ;;
      3) m=$(ask "Gutenberg mirror base URL (e.g. https://gutenberg.pglaf.org). Blank to clear:" "$(envget GUTENBERG_MIRROR)")
         envset GUTENBERG_MIRROR "$m"; restart_portal
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
         restart_portal ;;
      5) return 0 ;;
    esac
  done
}

step_intake_webhook() { # C11: off (empty token, the portal answers 404) until the admin turns it on here
  local d; d=$(envget DOMAIN)
  if [ -z "$(envget INTAKE_TOKEN)" ]; then
    yesno "The intake webhook is OFF (POST https://request.$d/intake answers 404).\n\nTurn it on? It lets automation you control (a script, an RSS/OPDS feed watcher) push a legal book URL into a user's library with a secret token." || return 0
    envset INTAKE_TOKEN "$(openssl rand -hex 24)"; restart_portal
  elif yesno "The intake webhook is ON.\n\nYes = show the token and usage.\nNo = turn it OFF (the token stops working)."; then :
  else envset INTAKE_TOKEN ""; restart_portal; msg "Intake webhook turned off."; return 0; fi
  msg "Intake webhook (for authorized legal-source automation):\n\n  POST https://request.$d/intake\n  Header:  X-Intake-Token: $(envget INTAKE_TOKEN)\n  JSON:    {\"user\":\"alice\",\"url\":\"https://.../book.epub\",\"kind\":\"ebook\"}\n\nIt pulls the exact URL you give and maps it to that user. Turn it off again from this menu."
}

# ---------- T. torrents (qBittorrent, opt-in) ----------
qbt_seed_config(){ # default save path = the admin's dropbox (tagged + imported), partials in /downloads/incomplete
  local f="$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"
  mkdir -p "$(dirname "$f")"
  python3 - "$f" "/dropbox/$(admin_user)" <<'PYQ' || return 1
import sys, os, re
f, save = sys.argv[1:3]
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
open(f, "w").write("\n".join(out).strip("\n") + "\n")
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
    render_caddy_all && apply_caddy || true
    msg "qBittorrent disabled and removed; port 6881 closed."; return 0
  fi
  yesno "Enable qBittorrent (admin-only torrent client at https://dl.$d, Tailscale only)?\n\nIt opens peer port 6881 to the internet, which also shows this server's IP to every swarm it joins. Download only what you are allowed to.\n\nEnable?" || return 0
  envset TORRENTS_ENABLED true
  install -d -o 1000 -g 1000 "$STACK_DIR/qbt/config" "$STACK_DIR/qbt/config/qBittorrent" "$STACK_DIR/downloads/incomplete"
  running qbittorrent && compose stop qbittorrent >/dev/null 2>&1    # it rewrites its config on exit
  qbt_seed_config
  torrent_port open
  if [ -n "$(envget CF_API_TOKEN)" ] && cf_zone; then cf_dns dl "$(envget TAILSCALE_IP)" false || true; fi
  render_caddy_all && apply_caddy || true
  compose up -d qbittorrent || { msg "qBittorrent did not start (Operations -> Logs -> qbittorrent). It stays enabled; run this again after fixing it, or disable it."; return 1; }
  sleep 5
  local qpw; qpw=$(docker logs qbittorrent 2>&1 | grep -oE 'temporary password.*: *[A-Za-z0-9]+' | tail -1 | awk '{print $NF}')
  big "qBittorrent enabled" "Open https://dl.$d (Tailscale on; admin-gate password first).
Web UI login: admin / ${qpw:-<see: docker logs qbittorrent>}  -> change it under Tools -> Options -> Web UI.

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
"Ephemera = search + a request queue that auto-downloads a title once it appears,
with FlareSolverr (headless Chromium, ~0.5-1 GB RAM) as its helper.

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
    render_caddy_all && apply_caddy || true     # the ephemera. vhost exists only while it is enabled
    msg "Ephemera is up at https://ephemera.$(envget DOMAIN) (Tailscale only; admin gate password).\n\nDownloads are filed to '$own' (owner:$own) via library/dropbox/$own.\nDisable any time under Operations."
  else
    envset EPHEMERA_ENABLED false
    msg "Ephemera build or start failed — left disabled. Check the output above (the pinned source must still be reachable on GitHub)."
  fi
}
step_ephemera_off() {
  composeE stop ephemera flaresolverr >/dev/null 2>&1 || true
  composeE rm -f ephemera flaresolverr >/dev/null 2>&1 || true
  envset EPHEMERA_ENABLED false
  render_caddy_all && apply_caddy || true
  msg "Ephemera and FlareSolverr stopped and removed. Its data (ephemera/) and settings are kept; re-enable any time."
}

# ---------- operations ----------
step_selftest() { # a failing check must never drop the admin out of the TUI (selftest exits with the fail count)
  clear; STACK_DIR="$STACK_DIR" bash "$STACK_DIR/scripts/selftest.sh" 2>&1 | tee "${TMPDIR:-/tmp}/bookstack-selftest.log" || true
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
IMG_KEYS="IMG_CWA IMG_ABS IMG_SHELFMARK IMG_QBIT IMG_KUMA IMG_AUTHELIA IMG_FLARESOLVERR"
BUILT_IMAGES="bookstack/caddy bookstack/librarian"   # built locally: kept as :prev across an update
stack_up_all() { # (re)start every enabled service with the tags in .env
  compose up -d || return 1
  [ "$(envget AUTHELIA_ENABLED)" = "true" ] && { composeA up -d authelia || return 1; }
  [ "$(envget EPHEMERA_ENABLED)" = "true" ] && { composeE up -d flaresolverr ephemera || return 1; }
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
    tags = get(f"https://ghcr.io/v2/{repo}/tags/list", {"Authorization": "Bearer " + tok})["tags"]
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
step_update() {
  local k cur new changed="" f="$(restic_env)"
  # (a) a restorable point before anything moves
  if [ -f "$f" ]; then
    clear; echo "Pre-update backup (tag pre-update)..."
    "$STACK_DIR/scripts/backup.sh" --tag pre-update || { msg "Pre-update backup failed; not updating. Check Operations -> Backups."; return 1; }
  else
    yesno "No backup repository is configured (Install -> Backups). Update WITHOUT a pre-update backup?" || return 1
  fi
  # (b) remember what runs now
  { for k in $IMG_KEYS; do printf '%s=%s\n' "$k" "$(img "$k")"; done; } > "$STACK_DIR/.env.images.prev"; chmod 600 "$STACK_DIR/.env.images.prev"
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
  tag_images latest prev
  copy_code_trees; own_data_dirs
  render_caddy_all || { update_failed "the new Caddyfile could not be rendered"; return 1; }
  # the rebuilt images carry this checkout's version (J03); Self-test compares it with .version
  if ! { compose pull --ignore-buildable && BUILD_VERSION="$(build_version)" compose build --pull caddy librarian && stack_up_all; }; then
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
  # (g) only now free old layers
  docker system prune -f --filter until=72h >/dev/null 2>&1 || true
  msg "Updated and healthy (deployed $(deployed_version)).$([ -n "$changed" ] && printf '\n\nNew tags:%b' "$changed")$([ "$st" != 0 ] && printf '\n\nThe full self-test reports %s failure(s) unrelated to the update gate: Operations -> Self-test.' "$st")"
}
rollback_images() { # restore the IMG_* values saved by step_update
  [ -f "$STACK_DIR/.env.images.prev" ] || return 0
  local line; while IFS= read -r line; do [ -n "$line" ] && envset "${line%%=*}" "${line#*=}"; done < "$STACK_DIR/.env.images.prev"
}
update_failed() { # (f) offer the rollback: previous tags, previous caddy/librarian builds, previous Caddyfile
  if yesno "Update problem: $1.\n\nRoll back to the previous image tags and the previous caddy/librarian builds?"; then
    rollback_images; tag_images prev latest
    local cf="$STACK_DIR/caddy/Caddyfile"; [ -s "$cf.pre-update" ] && cat "$cf.pre-update" > "$cf"
    if stack_up_all; then
      msg "Rolled back to the previous images.\n\nIf the new version already migrated a database, restore the pre-update snapshot: Operations -> Restore from backup -> pick the snapshot tagged 'pre-update' -> 'Config + databases only'."
    else
      msg "Rollback started but some containers did not come up: Operations -> Logs. The pre-update snapshot is under Operations -> Restore from backup (tag 'pre-update')."
    fi
  else
    msg "Left as is. Operations -> Logs / Self-test to investigate; the previous tags are in $STACK_DIR/.env.images.prev."
  fi
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
menu_library() {
  while true; do
    ch=$(whiptail --title "Library" --menu "What the library does with books, and where they come from." 22 88 11 \
      F "Formats & conversion: convert to EPUB on import, kept formats, Kindle fixer, duplicates" \
      A "Audiobookshelf: root + API key + library; per-user audiobook isolation ($(abs_ready && echo set up || echo not set up))" \
      M "Mail: SMTP so the portal can Send-to-Kindle (and test it)" \
      S "Sources: catalogs offered in the portal, approvals, your own OPDS catalog" \
      H "Shelfmark: extended search settings + first-run checklist" \
      I "Intake & dropboxes: webhook, Gutenberg mirror, email-to-library" \
      T "Torrents (qBittorrent): enable/disable ($(torrents_on && echo on || echo off))" \
      G "How per-user isolation works (guide)" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in F) step_formats || true;; A) step_abs_setup || true;; M) step_mail || true;; S) step_sources || true;; H) step_shelfmark || true;;
      I) step_intake || true;; T) step_torrents || true;; G) step_isolation || true;; 0) return 0;; esac
  done
}
menu_security() {
  while true; do
    ch=$(whiptail --title "Security" --menu "Authelia gate: $([ "$(envget AUTHELIA_ENABLED)" = true ] && echo ON || echo off)" 20 84 9 \
      A "Authelia: enable self-hosted SSO + 2FA in front of the public apps" \
      D "Authelia: disable the gate" \
      U "Authelia: add or reset a user" \
      L "Lock SSH to Tailscale only" \
      F "fail2ban: brute-force protection (SSH + login pages)" \
      M "Mail auth: SPF/DMARC records for Send-to-Kindle deliverability" \
      C "Cloudflare Access: hosted SSO + 2FA alternative (guide)" \
      R "Rotate the portal session secret (logs every portal user out)" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in A) step_authelia || true;; D) step_authelia_off || true;; U) step_authelia_user || true;; L) step_lock_ssh || true;;
      F) step_fail2ban || true;; M) step_mailauth || true;; C) step_cfaccess || true;; R) step_rotate_secret || true;; 0) return 0;; esac
  done
}
step_rotate_secret() {
  yesno "Rotate the portal's session-signing secret? Every portal session (all users, all devices) is logged out immediately; Kobo/OPDS/Kindle are unaffected." || return 0
  envset LIBRARIAN_SECRET "$(openssl rand -hex 32)"; restart_portal
  msg "Portal secret rotated and the portal restarted. Users simply sign in again."
}
menu_ops() {
  while true; do
    ch=$(whiptail --title "Operations" --menu "Ephemera: $([ "$(envget EPHEMERA_ENABLED)" = true ] && echo ON || echo off)" 24 84 14 \
      T "Self-test: containers, endpoints, configs, firewall, TLS, isolation, backups" \
      S "Status: all containers" \
      L "Logs: follow a service" \
      C "Check for updates: current vs newest image tags" \
      U "Update: backup first, deploy this code, pull/bump tags, health gate, rollback" \
      B "Backups: configure / change" \
      A "Alerts: phone notification (ntfy / webhook)" \
      R "Backup restore test" \
      W "Restore from backup: pick a snapshot, everything or config + databases" \
      M "Monitoring: Uptime Kuma + external check" \
      E "Ephemera: enable (Tailscale-only, unmaintained upstream — read notice)" \
      X "Ephemera: disable" \
      0 "Back" 3>&1 1>&2 2>&3) || return 0
    case "$ch" in T) step_selftest || true;; S) step_status || true;; L) step_logs || true;; C) step_check_updates || true;; U) step_update || true;;
      B) step_backup || true;; A) step_alerts || true;; R) step_restore_test || true;; W) step_restore || true;; M) step_monitoring || true;;
      E) step_ephemera || true;; X) step_ephemera_off || true;; 0) return 0;; esac
  done
}
main_menu() {
  while true; do
    choice=$(whiptail --title "Bookstack v$BOOKSTACK_VERSION — $(envget DOMAIN)" --menu "Private, per-user book library. Everything is configured from here." 18 84 6 \
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
