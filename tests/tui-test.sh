#!/usr/bin/env bash
# Regression tests for the installer's logic (bookstack.sh) without a VPS: the script is
# sourced in library mode, prompts and external commands are stubbed, and each menu action
# is checked for the files/env/commands it produces. The helper scripts (backup, restore
# test, disk watchdog) run as real processes against a fake stack with a stub restic/docker
# on PATH. Runs in seconds; needs bash + python3 (+ docker for the compose parity check).
#   bash tests/tui-test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/bookstack-tui.XXXXXX")"; trap 'rm -rf "$T"' EXIT
export STACK_DIR="$T/stack" BOOKSTACK_LIB=1 BOOKSTACK_ETC="$T/etc"
fails=0; pass=0
exec 2> >(grep -v --line-buffered '^UIBYTES' >&2)   # the stub's dialog marker is only interesting inside $(...) captures
ok(){ echo "  [ OK ] $1"; pass=$((pass+1)); }
bad(){ echo "  [FAIL] $1"; fails=$((fails+1)); }
expect(){ if eval "$1"; then ok "$2"; else bad "$2  -- ($1)"; fi; }

echo "== syntax"
bash -n "$REPO/bookstack.sh" && ok "bash -n bookstack.sh" || bad "bash -n bookstack.sh"
for f in "$REPO"/scripts/*.sh "$REPO"/tests/*.sh; do bash -n "$f" || bad "bash -n $f"; done; ok "bash -n scripts/tests"
command -v shellcheck >/dev/null && { shellcheck -S error "$REPO/bookstack.sh" "$REPO"/scripts/*.sh && ok "shellcheck (errors)" || bad "shellcheck"; }

# shellcheck source=../bookstack.sh
source "$REPO/bookstack.sh"
set +e   # the sourced script enables errexit; tests want to observe failures instead

# ---- stubs -------------------------------------------------------------------------
# Prompt answers are a FILE queue (command substitution runs prompts in subshells, so a
# shell array index could never advance). "<cancel>" makes a prompt return failure.
LOG="$T/calls.log"; Q="$T/answers"; : > "$LOG"; : > "$Q"
reset(){ : > "$LOG"; : > "$Q"; local a; for a in "$@"; do printf '%s\n' "$a" >> "$Q"; done; }
pop(){ local a; a="$(head -n1 "$Q")"; tail -n +2 "$Q" > "$Q.n" && mv "$Q.n" "$Q"; printf '%s' "$a"; }
# "<blank>" submits an empty string even when the prompt has a default (like clearing the box).
ask()   { echo "ask: $1" >> "$LOG"; local a; a="$(pop)"; [ "$a" = "<cancel>" ] && return 1; [ -z "$a" ] && a="${2:-}"; [ "$a" = "<blank>" ] && a=""; printf '%s' "$a"; }
askpw() { echo "askpw: $1" >> "$LOG"; [ -s "$Q" ] || return 1; local a; a="$(pop)";   # queue exhausted = Cancel (no endless askpw2 loop)
  [ "$a" = "<cancel>" ] && return 1; [ "$a" = "<blank>" ] && a=""; printf '%s' "$a"; }
yesno() { echo "yesno: $1" >> "$LOG"; local a; a="$(pop)"; [ -z "$a" ] && a=yes; [ "$a" = "yes" ]; }
clear() { :; }
sleep() { :; }
# askpw2, msg and big are the REAL functions: they run through this whiptail stub, which -
# like the real binary - draws its dialog on STDOUT (UIBYTES) and answers on stderr. Any
# msgbox drawing that leaks into a $(...) capture therefore shows up in the captured value.
whiptail(){ echo "whiptail: $*" >> "$LOG"; local a
  case " $* " in
    *" --msgbox "*) printf 'UIBYTES\e[0m\n'; return 0;;
    *" --yesno "*) a="$(pop)"; [ "$a" = "<esc>" ] && return 255; [ -z "$a" ] || [ "$a" = yes ];;
    *) a="$(pop)"; [ "$a" = "<esc>" ] && return 255; [ "$a" = "<cancel>" ] && return 1; printf '%s' "$a" >&2;;
  esac; }
docker() {
  echo "docker: $*" >> "$LOG"
  local stdin_raw=""
  case "$*" in *--password-stdin*|*"caddy hash-password"*)
    stdin_raw="$(cat; echo x)"; stdin_raw="${stdin_raw%x}"                      # keep the trailing newline
    echo "docker-stdin: $(printf '%s' "$stdin_raw")" >> "$LOG";; esac            # secrets arrive on stdin, never argv
  case "$*" in
    # real caddy 2.11 `hash-password` needs the newline-terminated password on stdin; without it
    # it exits 1 with "Error: EOF" and prints nothing. The stub is just as strict.
    *"caddy hash-password"*) [ "${HASH_STUB_BROKEN:-0}" = 1 ] && { echo "Error: EOF" >&2; return 1; }
       case "$stdin_raw" in *$'\n') echo '$2a$14$STUBHASH/abc';; *) echo "Error: EOF" >&2; return 1;; esac;;
    *"authelia crypto hash"*) echo 'Digest: $argon2id$v=19$m=65536,t=3,p=4$stubsalt$stubhash';;
    *"python -m cwa add-user"*) echo '{"ok": true, "user": "alice", "id": 2, "kobo_url": "https://books.example.test/kobo/abc123"}';;
    *"caddy validate"*) [ "${FAIL_VALIDATE:-0}" = 1 ] && { echo "Error: adapting config: bad directive"; return 1; }; :;;
    *"python -m cwa rename-user"*) [ "${FAIL_RENAME:-0}" = 1 ] && return 1; echo '{"ok": true}';;
    *"python -m cwa list"*) echo '[{"name":"admin","is_admin":true,"isolated":false},{"name":"alice","is_admin":false,"isolated":true},{"name":"bob","is_admin":false,"isolated":false}]';;
    *"python -m cwa passwd"*) [ "${FAIL_PASSWD:-0}" = 1 ] && return 1; echo '{"ok": true}';;
    *"python -m cwa "*) echo '{"ok": true}';;
    *"python -m abs init"*) echo '{"ok": true, "created_root": true, "api_key": "abs-key-STUB", "library_id": "lib1", "created_library": true}';;
    *"python -m abs ensure-user"*) echo '{"ok": true, "user": "alice", "result": "created"}';;
    *"python -m abs status"*) echo "{\"isInit\": ${ABS_INIT:-true}, \"app\": \"audiobookshelf\"}";;
    *"python -m abs "*) echo '{"ok": true}';;
    *"sqlite3 /config/cwa.db SELECT"*) echo '1|epub|new_record|1||0';;
    *"logs --tail"*) [ "${FAIL_LOGS:-0}" = 1 ] && return 1; :;;
    *"inspect -f"*) echo true;;
    # matched on "build --pull" so it also fires when compose carries --profile torrents.
    # J03: the image build must carry the checkout's version in its environment.
    *"build --pull"*) echo "build-version: ${BUILD_VERSION:-<unset>}" >> "$LOG"; [ "${FAIL_BUILD:-0}" = 1 ] && return 1; :;;
    *"ps -q"*) :;;
    *) :;;
  esac
}
curl(){ echo "curl: $*" >> "$LOG"; case "$*" in *ipify*) echo "${IPIFY:-203.0.113.5}";;
  *127.0.0.1:8090/healthz*) [ "${FAIL_HEALTHZ:-0}" = 1 ] && return 22; return 0;;
  *127.0.0.1:9091/api/health*) [ "${FAIL_AUTHELIA_HEALTH:-0}" = 1 ] && return 7; return 0;;
  *) return 0;; esac; }
chown(){ echo "chown: $*" >> "$LOG"; }
install(){ local skip=0 d; for d in "$@"; do if [ $skip = 1 ]; then skip=0; continue; fi
  case "$d" in -o|-g|-m) skip=1;; -*) ;; *) mkdir -p "$d";; esac; done; }
tailscale(){ echo "tailscale: $*" >> "$LOG"; case "$*" in *"status --json"*) echo "{\"Self\":{\"KeyExpiry\":${TS_EXPIRY:-null}}}";; "ip -4") echo 100.64.0.1;; esac; }
# `ip` as on a VPS: IP_SRC = the route's source address, IP_ADDRS = addresses on interfaces
ip(){ echo "ip: $*" >> "$LOG"; case "$*" in
  *"route get"*) [ -n "${IP_SRC:-}" ] && echo "1.1.1.1 via 10.0.0.1 dev eth0 src $IP_SRC uid 0";;
  *"addr show"*) local a; for a in ${IP_ADDRS:-}; do echo "2: eth0    inet $a/24 scope global eth0"; done;;
  esac; return 0; }
gpasswd(){ echo "gpasswd: $*" >> "$LOG"; }
sshd(){ echo "sshd: $*" >> "$LOG"; case "$1" in -t) return "${SSHD_T_RC:-0}";; -T) echo "port 22"; echo "passwordauthentication ${SSHD_PA:-no}";; esac; }
sysctl(){ echo "sysctl: $*" >> "$LOG"; }
fallocate(){ echo "fallocate: $*" >> "$LOG"; return 1; }
ufw(){ echo "ufw: $*" >> "$LOG"; }
apt-get(){ echo "apt-get: $*" >> "$LOG"; }; systemctl(){ echo "systemctl: $*" >> "$LOG"; }
# Stub binaries for the helper scripts bookstack.sh starts as child processes (cf-ips.sh, alert.sh...):
# a child never sees the shell functions above, and must never reach the network or a real container.
bin="$T/bin"; mkdir -p "$bin"; export DLOG="$T/docker.log"; : > "$DLOG"
export CF_IPS_STATE="$T/etc/bookstack/cf-ips.txt" CF_V4_FILE="$T/cf-v4.txt" CF_V6_FILE="$T/cf-v6.txt"
python3 - "$T" <<'PY2'
import sys
t = sys.argv[1]
open(t + "/cf-v4.txt", "w").write("\n".join("173.245.%d.0/22" % (i * 4) for i in range(15)) + "\n")
open(t + "/cf-v6.txt", "w").write("\n".join("2400:cb0%d::/32" % i for i in range(7)) + "\n")
PY2
cat > "$bin/curl" <<'EOS'
#!/usr/bin/env bash
echo "curl: $*" >> "$DLOG"
case "$*" in
  *cloudflare.com/ips-v4*) [ "${CF_FAIL_V4:-0}" = 1 ] && exit 22; cat "$CF_V4_FILE";;
  *cloudflare.com/ips-v6*) [ "${CF_FAIL_V6:-0}" = 1 ] && exit 22; cat "$CF_V6_FILE";;
esac
exit 0
EOS
for c in ufw logger hostname journalctl; do printf '#!/usr/bin/env bash\necho "%s: $*" >> "$DLOG"; exit 0\n' "$c" > "$bin/$c"; done
printf '#!/usr/bin/env bash\necho "docker: $*" >> "$DLOG"; exit 0\n' > "$bin/docker"
chmod +x "$bin"/*
seen(){ grep -qF -- "$1" "$LOG"; }
line_of(){ grep -nF -- "$1" "$LOG" | head -1 | cut -d: -f1; }

echo "== prompt helpers (N02: whiptail draws on stdout)"
reset "short" "goodpass-123" "goodpass-123"; got="$(askpw2 "x")"
[ "$got" = goodpass-123 ] && ok "askpw2 returns exactly the password after a too-short attempt (msgbox bytes not captured)" || bad "askpw2 captured [$got]"
reset "firstpass-1" "otherpass-2" "secondpass-3" "secondpass-3"; got="$(askpw2 "x")"
[ "$got" = secondpass-3 ] && ok "askpw2 retries after a mismatch and returns the clean value" || bad "askpw2 mismatch path captured [$got]"
reset "<cancel>"; askpw2 "x" >/dev/null; expect '[ $? = 1 ]' "askpw2 Cancel returns 1"
got="$(msg "hello")"; [ -z "$got" ] && ok "msg writes nothing to stdout" || bad "msg leaked [$got] to stdout"
got="$(big "T" "body")"; [ -z "$got" ] && ok "big writes nothing to stdout" || bad "big leaked to stdout"

echo "== .env round trip (envset/envget) and compose parity"
mkdir -p "$STACK_DIR"
tricky=('plain' 'pa$$w0rd' 'a&b|c;d' "it's here" 'quote"double' 'back\slash' '$2a$14$bcrypt/hash.x' 'spaces  inside' 'trailing space ' '' 'hash#inside' 'eq=sign' '${NOT_EXPANDED}')
i=0; allok=1
for v in "${tricky[@]}"; do envset "K$i" "$v"; got="$(envget "K$i")"; [ "$got" = "$v" ] || { allok=0; echo "     mismatch for [$v] -> [$got]"; }; i=$((i+1)); done
[ $allok = 1 ] && ok "envget(envset(v)) == v for ${#tricky[@]} hostile values" || bad "envset/envget round trip"
envset K1 'changed'; expect '[ "$(grep -c "^K1=" "$ENV_FILE")" = 1 ] && [ "$(envget K1)" = changed ]' "re-setting a key replaces it (no duplicates)"
mode=$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE"); [ "$mode" = 600 ] && ok ".env is mode 600" || bad ".env mode is $mode"
# F37: the temp file is 0600 from its first byte (it holds every secret before the rename)
mv(){ [ "${1##*/}" = .env.tmp ] && { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; } > "$T/tmpmode"; command mv "$@"; }
( umask 022; envset K_UMASK x ); unset -f mv
expect '[ "$(cat "$T/tmpmode")" = 600 ]' "envset writes .env.tmp with mode 600 even under umask 022 (F37)"
printf 'LEGACY=bare$value\n' >> "$ENV_FILE"; expect '[ "$(envget LEGACY)" = "bare\$value" ]' "bare legacy values still read"
envdefault K1 'ignored'; envdefault KNEW 'set'; expect '[ "$(envget K1)" = changed ] && [ "$(envget KNEW)" = set ]' "envdefault only fills blanks"
expect '[ "$(img IMG_CWA)" = crocodilestick/calibre-web-automated:v4.0.6 ]' "img() falls back to the pinned default"
envset IMG_CWA x/y:1; expect '[ "$(img IMG_CWA)" = x/y:1 ]' "img() prefers .env"; envset IMG_CWA ""
if command docker info >/dev/null 2>&1; then
  printf 'services:\n  t:\n    image: alpine\n    environment:\n' > "$STACK_DIR/docker-compose.yml"
  for j in $(seq 0 $((i-1))); do printf '      - K%s=${K%s}\n' "$j" "$j" >> "$STACK_DIR/docker-compose.yml"; done
  # ask a real container what it received (compose config output escapes $ as $$, so it can't be compared directly)
  actual=$(cd "$STACK_DIR" && command docker compose run --rm -T t sh -c 'for j in $(seq 0 12); do eval "printf \"%s\\n\" \"\$K$j\""; done' 2>/dev/null | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split("\n")[:13]))')
  expected=$(python3 -c '
import json
exp=["plain","changed","a&b|c;d","it'"'"'s here","quote\"double","back\\slash","$2a$14$bcrypt/hash.x","spaces  inside","trailing space ","","hash#inside","eq=sign","${NOT_EXPANDED}"]
print(json.dumps(exp))')
  [ "$actual" = "$expected" ] && ok "a real container receives every value byte-for-byte" || { bad "compose parity"; echo "     got:      $actual"; echo "     expected: $expected"; }
  rm -f "$STACK_DIR/docker-compose.yml"
else
  echo "  [skip] docker not available for compose parity"
fi

PATH="$bin:$PATH"   # from here on child processes only ever see the stub binaries

echo "== image pins: bookstack.sh defaults == compose defaults == .env.example"
python3 - "$REPO" "$IMG_DEFAULTS" <<'PY' && ok "IMG_* defaults agree across bookstack.sh, compose files and .env.example" || bad "IMG_* pin mismatch"
import re, sys, glob
repo, defaults = sys.argv[1], dict(kv.split("=", 1) for kv in sys.argv[2].split())
bad = []
for f in glob.glob(repo + "/docker-compose*.yml"):
    for k, v in re.findall(r"image:\s*\$\{(IMG_\w+):-([^}]+)\}", open(f).read()):
        if defaults.get(k) != v: bad.append(f"{f}: {k}={v} vs {defaults.get(k)}")
    for img in re.findall(r"^\s*image:\s*(\S+)\s*$", open(f).read(), re.M):
        if not img.startswith("${IMG_") and not img.startswith("bookstack/"): bad.append(f"{f}: unpinned {img}")
env = dict(re.findall(r"^(IMG_\w+)=(\S+)", open(repo + "/.env.example").read(), re.M))
for k, v in defaults.items():
    if env.get(k) != v: bad.append(f".env.example: {k}={env.get(k)} vs {v}")
print("\n".join("     " + b for b in bad)); sys.exit(1 if bad else 0)
PY
expect 'grep -q "^MAX_UPLOAD_MB=95" "$REPO/.env.example" && grep -q "^SHELFMARK_CONCURRENCY=1" "$REPO/.env.example"' ".env.example: 95 MB upload cap, Shelfmark concurrency 1"

echo "== system step pieces"
reset; write_docker_daemon_json && ok "write_docker_daemon_json" || bad "write_docker_daemon_json failed"
expect 'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"ip\"]==\"127.0.0.1\" and d[\"live-restore\"] is True and d[\"no-new-privileges\"] is True and d[\"log-opts\"][\"max-size\"]==\"10m\"" "$T/etc/docker/daemon.json" && seen "systemctl: restart docker"' "daemon.json: loopback publish default, live-restore, log caps; docker restarted on change"
reset; write_docker_daemon_json; expect '! seen "systemctl: restart docker"' "unchanged daemon.json -> no docker restart"

# uid 1000 handling: PASSWD is a fake /etc/passwd the stubs read and useradd appends to
PASSWD="$T/passwd"
getent(){ [ "$1" = passwd ] || return 2; awk -F: -v k="$2" '($1==k || $3==k){print; f=1} END{exit !f}' "$PASSWD"; }
useradd(){ echo "useradd: $*" >> "$LOG"; local u="${*: -1}" id=""; [ "$1" = -m ] && id="$3"
  awk -F: -v i="$id" -v n="$u" '($3==i || $1==n){e=1} END{exit !e}' "$PASSWD" && { echo "useradd: UID $id is not unique" >&2; return 4; }
  echo "$u:x:$id:$id::/home/$u:/bin/bash" >> "$PASSWD"; }
usermod(){ echo "usermod: $*" >> "$LOG"; }
printf 'root:x:0:0::/root:/bin/bash\ndebian:x:1000:1000::/home/debian:/bin/bash\n' > "$PASSWD"
reset; STACK_USER=books; ensure_stack_user; rc=$?
expect '[ $rc = 0 ] && [ "$STACK_USER" = debian ] && [ "$STACK_HOME" = /home/debian ] && ! seen "useradd:" && seen "gpasswd: -d debian docker" && ! seen "usermod: -aG docker"' "uid 1000 taken by the image's default user -> that account is reused, no useradd (was: abort)"
printf 'root:x:0:0::/root:/bin/bash\n' > "$PASSWD"
reset; STACK_USER=books; ensure_stack_user; rc=$?
expect '[ $rc = 0 ] && [ "$STACK_USER" = books ] && seen "useradd: -m -u 1000 -s /bin/bash books" && grep -q "^books:x:1000:" "$PASSWD" && ! seen "usermod: -aG docker"' "fresh image -> 'books' created with uid 1000"
reset; STACK_USER=books; ensure_stack_user; rc=$?
expect '[ $rc = 0 ] && [ "$STACK_USER" = books ] && ! seen "useradd:"' "re-run is idempotent (existing books/1000 reused)"
printf 'root:x:0:0::/root:/bin/bash\nbooks:x:1001:1001::/home/books:/bin/bash\n' > "$PASSWD"
reset; STACK_USER=books; ensure_stack_user; rc=$?
expect '[ $rc = 0 ] && [ "$STACK_USER" = books1000 ] && grep -q "^books1000:x:1000:" "$PASSWD" && grep -q "^books:x:1001:" "$PASSWD"' "a 'books' account with another uid is left alone; uid 1000 gets its own account"
printf 'root:x:0:0::/root:/bin/bash\n' > "$PASSWD"
useradd(){ echo "useradd: $*" >> "$LOG"; return 1; }
reset; STACK_USER=books; ensure_stack_user; rc=$?
expect '[ $rc = 1 ] && grep -F "msgbox" "$LOG" | grep -q "Could not create" && ! seen "gpasswd:"' "useradd failure is reported and stops the step (no half-configured account)"
unset -f getent useradd usermod; STACK_USER=books

echo "== system: SSH drop-in, sysctl, firewall, swap (F10, F12, F38, C3)"
mkdir -p "$T/etc/ssh/sshd_config.d"; echo "PasswordAuthentication no" > "$T/etc/ssh/sshd_config.d/90-bookstack.conf"
keys="$T/root_keys"; echo "ssh-ed25519 AAAA test" > "$keys"; export BOOKSTACK_ROOT_KEYS="$keys"
reset; sshnote=""; harden_ssh; rc=$?
expect '[ $rc = 0 ] && [ -f "$T/etc/ssh/sshd_config.d/01-bookstack.conf" ] && [ ! -f "$T/etc/ssh/sshd_config.d/90-bookstack.conf" ] && grep -q "^PasswordAuthentication no" "$T/etc/ssh/sshd_config.d/01-bookstack.conf"' "drop-in is 01-bookstack.conf (sorts before 50-cloud-init.conf); the old 90- file is removed"
expect 'seen "sshd: -t" && [ "$(line_of "sshd: -t")" -lt "$(line_of "systemctl: reload ssh")" ] && seen "sshd: -T" && printf "%s" "$sshnote" | grep -q "verified"' "sshd -t before the reload, effective config verified with sshd -T"
SSHD_PA=yes; reset; sshnote=""; harden_ssh; rc=$?; SSHD_PA=no
expect '[ $rc = 1 ] && printf "%s" "$sshnote" | grep -q "STILL ON"' "another drop-in overriding PasswordAuthentication is reported, not 'key-only'"
SSHD_T_RC=1; reset; sshnote=""; harden_ssh; rc=$?; SSHD_T_RC=0
expect '[ $rc = 1 ] && [ ! -f "$T/etc/ssh/sshd_config.d/01-bookstack.conf" ] && ! seen "systemctl: reload"' "sshd -t failure: drop-in removed, sshd NOT reloaded"
rm -f "$keys"; reset; sshnote=""; harden_ssh; expect 'printf "%s" "$sshnote" | grep -q "password login was left ON" && ! seen "sshd:"' "no key anywhere -> password login left on"
reset; write_sysctl; expect 'grep -q "^net.ipv4.ip_nonlocal_bind = 1" "$T/etc/sysctl.d/90-bookstack.conf" && grep -q "^net.ipv6.ip_nonlocal_bind = 1" "$T/etc/sysctl.d/90-bookstack.conf"' "sysctl allows non-local binds (tailnet IP loss cannot stop Caddy)"
mkdir -p "$STACK_DIR"; envset SSH_LOCKED true; envset TORRENTS_ENABLED false
reset; setup_firewall; expect 'seen "ufw: --force delete allow 22/tcp" && ! seen "ufw: allow 22/tcp" && seen "ufw: --force delete allow 6881/tcp" && ! seen "ufw: allow 6881"' "SSH_LOCKED=true: re-running System keeps 22 closed; 6881 closed while torrents are off (F12/F41)"
envset SSH_LOCKED false; envset TORRENTS_ENABLED true
reset; setup_firewall; expect 'seen "ufw: allow 22/tcp" && seen "ufw: allow 6881/tcp" && seen "ufw: allow 6881/udp"' "not locked: 22 allowed; torrents on: 6881 open"
envset TORRENTS_ENABLED false
: > "$T/etc/fstab"; reset; setup_swap; expect '! grep -q swapfile "$T/etc/fstab"' "swapfile creation failed -> no fstab entry (F38)"
expect 'declare -f step_system | grep -q " cron" && declare -f step_system | grep -q "enable --now cron"' "System installs and enables cron (F35/F42)"
rm -f "$ENV_FILE"

echo "== configure step"
rm -f "$ENV_FILE"; mkdir -p "$STACK_DIR"
reset "example.test" "admin@example.test" "Asia/Kolkata" "cf-token-123" "gatepass-12345" "gatepass-12345"
step_configure && ok "step_configure ran" || bad "step_configure failed"
missing=""; for k in DOMAIN ADMIN_EMAIL TZ CF_API_TOKEN CF_DNS_TOKEN PUBLIC_IP BIND_IP ADMIN_USER LIBRARIAN_SECRET ADMIN_HASH SRC_GUTENBERG IA_COLLECTIONS SHELFMARK_LANGUAGE EPHEMERA_ENABLED AUTHELIA_ENABLED APPROVALS_REQUIRED TORRENTS_ENABLED SHELFMARK_TITLE IMG_CWA IMG_ABS IMG_SHELFMARK IMG_QBIT IMG_KUMA IMG_AUTHELIA IMG_FLARESOLVERR; do
  [ -n "$(envget $k)" ] || missing="$missing $k"; done; [ -z "$missing" ] && ok "all keys written (incl. IMG_* pins)" || bad "missing keys:$missing"
expect '! grep -qE "^(ARIA2_SECRET|IMG_ARIA2|IMG_ARIANG|IA_USE_TORRENT|QBIT_USER)=" "$ENV_FILE"' "no aria2 / IA-torrent / QBIT keys written (C4, C5)"
expect '[ -z "$(envget INTAKE_TOKEN)" ] && [ "$(envget APPROVALS_REQUIRED)" = false ] && [ "$(envget TORRENTS_ENABLED)" = false ]' "family defaults: intake webhook off, approvals off, torrents off (C11, C5)"
expect '[ "$(envget ADMIN_USER)" = libadmin ] && grep -q "ask: Username of YOUR admin account" "$LOG"' "admin username asked; suggestion avoids 'admin' (C10)"
expect '[ "$(envget CF_DNS_TOKEN)" = cf-token-123 ]' "no separate DNS token given -> Caddy's CF_DNS_TOKEN reuses the main token (C7)"
expect '[ "$(stat -c %a "$STACK_DIR/.version" 2>/dev/null || stat -f %Lp "$STACK_DIR/.version")" != "" ] && [ -s "$STACK_DIR/.version" ]' "deployed code version recorded in \$STACK_DIR/.version (F11)"
expect '[ "$(envget PUBLIC_IP)" = 203.0.113.5 ]' "public IP detected"
expect '[ "$(envget ADMIN_HASH)" = "\$2a\$14\$STUBHASH/abc" ] && seen "docker-stdin: gatepass-12345" && ! seen "plaintext"' "admin gate password hashed from stdin (never --plaintext argv), hash stored verbatim"
expect '[ "$(envget CF_API_TOKEN)" = cf-token-123 ] && [ "$(envget TZ)" = Asia/Kolkata ] && [ "$(envget SHELFMARK_CONCURRENCY)" = 1 ]' "token, timezone and concurrency 1 stored"
expect 'seen "chown: root:root $ENV_FILE"' ".env handed back to root after the recursive chown"
missing=""; for f in docker-compose.yml docker-compose.authelia.yml docker-compose.ephemera.yml caddy/Dockerfile caddy/Caddyfile.template caddy/Caddyfile authelia/configuration.yml.template scripts/caddy-gate.snippet scripts/inject-gate.py scripts/backup.sh scripts/selftest.sh scripts/cf-ips.sh scripts/alert.sh scripts/disk-watch.sh scripts/restore-test.sh configs/fail2ban/jail.local configs/fail2ban/caddy-device-auth.conf configs/fail2ban/caddy-abs-login.conf librarian/app.py librarian/cwa.py librarian/templates/devices.html librarian/Dockerfile librarian/.dockerignore; do
  [ -f "$STACK_DIR/$f" ] || missing="$missing $f"; done; [ -z "$missing" ] && ok "stack files copied" || bad "not copied:$missing"
expect '[ ! -d "$STACK_DIR/librarian/tests" ]' "tests are not shipped to the server"
expect '[ ! -e "$STACK_DIR/authelia/inject-gate.py" ] && [ ! -e "$STACK_DIR/authelia/caddy-gate.snippet" ]' "root-run gate injector lives in scripts/, not in the Authelia-writable authelia/ (F36)"
expect 'seen "chown: root:root $STACK_DIR" && seen "chown: -R root:root $STACK_DIR/scripts" && ! seen "chown: -R 1000:1000 $STACK_DIR " && ! grep -qx "chown: -R 1000:1000 $STACK_DIR" "$LOG" && seen "chown: -R 1000:1000 $STACK_DIR/library"' "stack dir, code and scripts root-owned; only data dirs chowned to uid 1000 (C14/F36)"
expect '! grep -q "@@" "$STACK_DIR/caddy/Caddyfile"' "Caddyfile has no unrendered placeholders"
expect 'grep -q "^books.example.test {" "$STACK_DIR/caddy/Caddyfile" && grep -q "^shelf.example.test {" "$STACK_DIR/caddy/Caddyfile" && grep -q "^monitor.example.test {" "$STACK_DIR/caddy/Caddyfile"' "Caddyfile has books/shelf/monitor vhosts"
expect '! grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "^aria\." "$STACK_DIR/caddy/Caddyfile"' "no ephemera./dl. vhost while those features are off; no aria. (C4-C6)"
expect 'grep -q "bind 203.0.113.5" "$STACK_DIR/caddy/Caddyfile" && grep -q "import tailnet_only" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "bind 127.0.0.1" "$STACK_DIR/caddy/Caddyfile"' "public vhosts bind BIND_IP; admin vhosts are tailnet_only, never bound to the tailnet IP"
envset EPHEMERA_ENABLED true; envset TORRENTS_ENABLED true; render_caddyfile
expect 'grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "@@" "$STACK_DIR/caddy/Caddyfile"' "enabling Ephemera / torrents renders their vhosts (C5, C6)"
envset EPHEMERA_ENABLED false; envset TORRENTS_ENABLED false; render_caddyfile
tsbak=$(envget TAILSCALE_IP); envset TAILSCALE_IP ""; cp "$STACK_DIR/caddy/Caddyfile" "$T/cf.before"; reset; render_caddyfile; rc=$?; envset TAILSCALE_IP "$tsbak"
expect '[ $rc = 1 ] && cmp -s "$STACK_DIR/caddy/Caddyfile" "$T/cf.before" && grep -F msgbox "$LOG" | grep -q "TAILSCALE_IP is empty"' "empty TAILSCALE_IP: render refuses (the tailnet_only matcher would break every site)"
# F11: a running Caddy gets the new file validated and reloaded; a bad file is put back
reset "example.test" "admin@example.test" "UTC" "" "yes"; step_configure
expect 'seen "compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile" && seen "caddy reload --config /etc/caddy/Caddyfile" && [ "$(line_of "caddy validate")" -lt "$(line_of "caddy reload")" ]' "Configure validates then reloads the running Caddy (F11)"
echo "# good" > "$STACK_DIR/caddy/Caddyfile.prev"; echo "# broken" > "$STACK_DIR/caddy/Caddyfile"
FAIL_VALIDATE=1; reset; apply_caddy; rc=$?; FAIL_VALIDATE=0
expect '[ $rc = 1 ] && [ "$(cat "$STACK_DIR/caddy/Caddyfile")" = "# good" ] && ! seen "caddy reload" && grep -F msgbox "$LOG" | grep -q "does NOT validate"' "invalid new Caddyfile: previous one restored, no reload"
render_caddyfile
# C8 / F18 / F33: behind 1:1 NAT the public IP is kept when set by hand; Caddy binds the route source address
envset PUBLIC_IP 198.51.100.7; envset BIND_IP ""; IP_SRC=10.0.0.5; IP_ADDRS="10.0.0.5"
reset "example.test" "admin@example.test" "UTC" "" "yes" "" "" "no"; step_configure
expect '[ "$(envget PUBLIC_IP)" = 198.51.100.7 ] && [ "$(envget BIND_IP)" = 10.0.0.5 ] && grep -q "bind 10.0.0.5" "$STACK_DIR/caddy/Caddyfile" && seen "yesno: The stored public IP (198.51.100.7) differs"' "NAT: manual PUBLIC_IP kept (asked, answered No); BIND_IP = route source, used by Caddy"
reset "example.test" "admin@example.test" "UTC" "" "yes" "" "" "yes"; step_configure
expect '[ "$(envget PUBLIC_IP)" = 203.0.113.5 ] && [ "$(envget BIND_IP)" = 10.0.0.5 ]' "choosing re-detect takes the detected public IP; a still-valid BIND_IP is kept"
IP_SRC=""; IP_ADDRS=""; envset BIND_IP ""
reset "example.test" "admin@example.test" "UTC" "" "yes"; step_configure
expect '[ "$(envget BIND_IP)" = 203.0.113.5 ] && ! seen "yesno: The stored public IP"' "no route source -> BIND_IP falls back to PUBLIC_IP; same IP -> no question"
reset "example.test" "admin@example.test" "UTC" "" "yes" "" "dns-only-token"; step_configure
expect '[ "$(envget CF_DNS_TOKEN)" = dns-only-token ] && [ "$(envget CF_API_TOKEN)" = cf-token-123 ]' "a separate DNS-only token is stored for Caddy (C7)"
reset "example.test" "admin@example.test" "UTC" "cf-token-NEW" "yes"; step_configure
expect '[ "$(envget CF_DNS_TOKEN)" = dns-only-token ] && [ "$(envget CF_API_TOKEN)" = cf-token-NEW ]' "changing the main token leaves the separate DNS token alone"
envset CF_DNS_TOKEN cf-token-NEW; reset "example.test" "admin@example.test" "UTC" "cf-token-123" "yes"; step_configure
expect '[ "$(envget CF_DNS_TOKEN)" = cf-token-123 ]' "a DNS token that was a copy of the main token follows it"
before=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
reset "example.test" "admin@example.test" "UTC" "" "yes" "Bad Name!"; step_configure; rc=$?
expect '[ $rc = 1 ] && [ "$(envget ADMIN_USER)" = libadmin ]' "an invalid admin username is refused"
expect '! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "no Authelia gate while disabled"
expect '[ "$(grep -cE "# @AUTHELIA_GATE:(books|audio|request|shelf)@" "$STACK_DIR/caddy/Caddyfile")" = 4 ]' "4 per-host gate markers (books, audio, request, shelf)"
reset "example.test" "admin@example.test" "UTC" "" "yes"; step_configure
expect '[ "$(envget CF_API_TOKEN)" = cf-token-123 ] && [ "$(envget ADMIN_HASH)" = "\$2a\$14\$STUBHASH/abc" ] && [ "$(envget TZ)" = UTC ]' "re-running Configure keeps token and gate password, updates the rest"
reset "example.test" "admin@example.test" "<cancel>" "" "yes"; step_configure
expect '[ "$(envget TZ)" = UTC ]' "Cancel at the timezone prompt keeps the stored value"
reset "example.test" "admin@example.test" "UTC" "" "no" "newgate-12345" "newgate-12345"; step_configure
expect 'seen "docker-stdin: newgate-12345"' "declining 'keep password' re-hashes a new one"
before=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
reset "<cancel>"; step_configure; rc=$?
after=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
expect '[ "$rc" != 0 ] && [ "$before" = "$after" ]' "cancel at the first prompt aborts (non-zero) and leaves .env untouched"
# hash-password failure path: the stub returns garbage -> nothing stored, Configure reports failure
reset "example.test" "admin@example.test" "UTC" "" "no" "bad-12345" "bad-12345"; HASH_STUB_BROKEN=1 step_configure; rc=$?
expect '[ "$rc" != 0 ] && [ "$(envget ADMIN_HASH)" = "\$2a\$14\$STUBHASH/abc" ] && grep -F "msgbox" "$LOG" | grep -q "Could not hash"' "a failed caddy hash-password keeps the old hash and reports the failure"

echo "== Authelia gate + users"
inject_authelia_gate >/dev/null; expect '[ "$(grep -cE "^\s*forward_auth " "$STACK_DIR/caddy/Caddyfile")" = 4 ] && [ "$(grep -cE "^\s*forward_auth @authelia_protected " "$STACK_DIR/caddy/Caddyfile")" = 3 ] && grep -qE "^\s*forward_auth 127.0.0.1:9091" "$STACK_DIR/caddy/Caddyfile"' "gate injected into 4 vhosts (shelf without a bypass matcher)"
expect 'grep -qF "not path_regexp ^(?:/kobo/|/kosync(/|$)|/opds(/|$))" "$STACK_DIR/caddy/Caddyfile" && grep -qF "not path_regexp ^(?:/intake$)" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "@@BYPASS@@" "$STACK_DIR/caddy/Caddyfile"' "Kobo/OPDS/KOReader and intake bypasses present as anchored, case-sensitive regexps"
render_caddyfile; expect '! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "re-render removes the gate (disable path)"
envset AUTHELIA_ENABLED true; reset "example.test" "admin@example.test" "UTC" "" "yes"; step_configure >/dev/null
expect 'grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "Configure re-applies the gate when Authelia is enabled"
reset; authelia_add_user alice "Alice" alice@example.test secret-pass-1 && ok "authelia_add_user" || bad "authelia_add_user"
expect '! seen "secret-pass-1" && seen "docker: run --rm -e PW authelia/authelia:4.39.28"' "Authelia hash: password passed through the environment, pinned image, never in argv"
authelia_add_user alice "Alice Two" alice2@example.test secret-pass-2
f="$STACK_DIR/authelia/users_database.yml"
expect '[ "$(grep -c "^  alice:" "$f")" = 1 ] && grep -q "Alice Two" "$f" && ! grep -q "users: {}" "$f"' "re-adding a user replaces the entry"
authelia_add_user bob "Bob \"the\" O'Brien: yes" bob@example.test secret-pass-3
expect '[ "$(grep -c "^  [a-z]*:$" "$f")" = 2 ]' "two users in users_database.yml"
authelia_add_user alice "" "" secret-pass-5
expect '[ "$(grep -c "^  alice:" "$f")" = 1 ] && grep -q "alice2@example.test" "$f" && grep -q "Alice Two" "$f"' "password reset with blank name/e-mail keeps the stored ones (C9)"
authelia_add_user 'bad name;' Bad bad@example.test secret-pass-4; expect '[ $? = 1 ] && ! grep -q "bad name" "$f"' "usernames outside [A-Za-z0-9._-] are refused"
python3 - "$f" <<'PY' && ok "users_database.yml is well-formed; hostile display name JSON-quoted" || bad "users_database.yml malformed"
import sys,re,json
s=open(sys.argv[1]).read()
assert re.search(r"^users:\n(  \S+:\n(    .+\n)+)+", s, re.M), s
assert s.count("displayname:") == 2 and s.count("groups:") == 2, s
dn = re.search(r'^    displayname: (".*")$', s.split("  bob:")[1], re.M).group(1)
assert json.loads(dn) == "Bob \"the\" O'Brien: yes", dn
try:
    import yaml
    d = yaml.safe_load(s); assert d["users"]["bob"]["displayname"] == "Bob \"the\" O'Brien: yes" and d["users"]["alice"]["password"].startswith("$argon2id$")
except ImportError:
    pass
PY

authelia_add_user zed "" "" secret-pass-6; expect '[ $? != 0 ] && ! grep -q "^  zed:" "$f"' "a NEW Authelia login without an e-mail is refused, never <user>@DOMAIN (F49)"
authelia_add_user zed "" "zed@mail.example" secret-pass-6
authelia_remove_user zed; expect '! grep -q "^  zed:" "$f" && grep -q "^  alice:" "$f" && grep -q "^  bob:" "$f"' "authelia_remove_user drops only that user"
cp "$f" "$T/users.keep"
# C9: SMTP notifier rendered into configuration.yml (password via Authelia's template filter only)
envset SMTP_HOST smtp.example.test; envset SMTP_PORT 587; envset SMTP_SECURITY starttls; envset SMTP_USER "lib@example.test"; envset SMTP_FROM "Library <lib@example.test>"
render_authelia_config; ac="$STACK_DIR/authelia/configuration.yml"
if grep -q "@NOTIFIER_BEGIN@" "$STACK_DIR/authelia/configuration.yml.template"; then
  expect 'grep -q "address: '"'"'submission://smtp.example.test:587'"'"'" "$ac" && grep -qF '"'"'password: {{ env "BOOKSTACK_SMTP_PASS" | quote }}'"'"' "$ac" && ! grep -q "^  filesystem:" "$ac" && ! grep -q "^[^#]*AUTHELIA_NOTIFIER_SMTP_PASSWORD" "$ac" && grep -q "@NOTIFIER_END@" "$ac"' "SMTP set: smtp notifier (submission://, password from env template) replaces the filesystem one"
  envset SMTP_SECURITY ssl; envset SMTP_PORT 465; render_authelia_config
  expect 'grep -q "submissions://smtp.example.test:465" "$ac"' "SSL/TLS -> submissions://"
  envset SMTP_HOST ""; render_authelia_config
  expect 'grep -q "^  filesystem:" "$ac" && ! grep -q "^  smtp:" "$ac" && ! grep -q "@@DOMAIN@@" "$ac"' "no SMTP: filesystem notifier kept"
else
  echo "  [skip] configuration.yml.template has no @NOTIFIER_BEGIN@ markers yet"
fi
envset SMTP_HOST ""; envset SMTP_USER ""; envset SMTP_FROM ""; envset SMTP_PORT ""; envset SMTP_SECURITY ""
# F14: Authelia goes live only when healthy AND with at least one user
envset AUTHELIA_ENABLED false; render_caddyfile; echo "users: {}" > "$f"
reset "yes" "<cancel>" "<cancel>" "<cancel>"; step_authelia; rc=$?
expect '[ $rc = 1 ] && [ "$(envget AUTHELIA_ENABLED)" = false ] && ! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" && seen "stop authelia" && grep -F msgbox "$LOG" | grep -q "No Authelia user exists"' "no user created -> gate NOT enabled, Authelia stopped"
FAIL_AUTHELIA_HEALTH=1; reset "yes"; step_authelia; rc=$?; FAIL_AUTHELIA_HEALTH=0
expect '[ $rc = 1 ] && [ "$(envget AUTHELIA_ENABLED)" = false ] && ! seen "askpw:" && grep -F msgbox "$LOG" | grep -q "did not become healthy"' "Authelia unhealthy -> gate NOT enabled, no users asked"
reset "yes" "adminpw-123" "adminpw-123" "boss@mail.example" "<cancel>" "<cancel>"; step_authelia; rc=$?
expect '[ $rc = 0 ] && [ "$(envget AUTHELIA_ENABLED)" = true ] && grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" && grep -q "^  admin:" "$f" && [ "$(line_of "authelia crypto hash")" -lt "$(line_of "caddy reload")" ] && grep -F msgbox "$LOG" | grep -q "notification.txt"' "one user created -> gate injected and Caddy reloaded afterwards; no SMTP -> admin told where codes go"
step_authelia_off >/dev/null; expect '[ "$(envget AUTHELIA_ENABLED)" = false ] && ! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "Authelia disable removes the gate"
cp "$T/users.keep" "$f"; envset AUTHELIA_ENABLED true; render_caddy_all

echo "== users & devices menu"
reset "alice" "alice@example.test" "alicepass-123" "alicepass-123" "no" ""     # not admin, no kindle
step_user_add && ok "step_user_add" || bad "step_user_add"
expect 'seen "python -m cwa add-user alice --email alice@example.test --password-stdin" && seen "docker-stdin: alicepass-123"' "calls the portal CLI with the password on stdin"
expect '! grep -E "python -m (cwa|abs) .*--password " "$LOG"' "no --password <value> on any cwa/abs argv"
expect '! seen "alicepass-123 --admin"' "non-admin by default"
expect '[ -d "$STACK_DIR/library/dropbox/alice" ]' "dropbox created"
expect 'seen "https://books.example.test/kobo/abc123"' "Kobo link shown to the admin"
expect 'seen "authelia crypto hash"' "Authelia login created alongside (Authelia enabled)"
reset "boss" "boss@mail.example" "bosspass-123" "bosspass-123" "yes" "k@kindle.com"; step_user_add
expect 'seen "add-user boss --email boss@mail.example --password-stdin --admin" && ! grep -q "ask: .*e-mail.*boss@example.test" "$LOG"' "admin flag; the e-mail is asked, not pre-filled as <user>@DOMAIN (F49)"
expect 'seen "cwa kindle boss k@kindle.com"' "Kindle set during creation"
reset "nomail" ""; step_user_add; expect '[ $? = 1 ] && ! seen "add-user nomail"' "blank e-mail refused (F49)"
reset "carl" "carl@mail.example" "carlpass-1234" "carlpass-1234" "no" "<cancel>"; step_user_add; expect '! seen "cwa kindle" && seen "User carl created"' "Cancel at the Kindle prompt during Add still finishes the user"
reset "alice" "kindle@x.com"; step_user_kindle; expect 'seen "cwa kindle alice kindle@x.com"' "set Kindle address"
reset "alice" "<cancel>"; step_user_kindle; expect '! seen "cwa kindle"' "Cancel at the Kindle prompt leaves the address unchanged"
reset "alice" ""; step_user_kindle; expect '! seen "cwa kindle"' "blank at the Kindle prompt changes nothing"
reset "alice" "none"; step_user_kindle; expect 'grep -qE "cwa kindle alice $" "$LOG"' "typing 'none' clears the Kindle address"
reset "alice" "no"; step_user_kobo; expect 'seen "cwa kobo-url alice --reset"' "Kobo link regenerate"
reset "alice" "yes"; step_user_kobo; expect '! seen "--reset"' "Kobo link show (no reset)"
reset "alice" "newpass-1234" "newpass-1234"; step_user_passwd; expect 'seen "cwa passwd alice --password-stdin" && seen "docker-stdin: newpass-1234"' "password reset via stdin"
expect '! seen "python -m abs"' "no Audiobookshelf calls while ABS is not set up"
# J07: Shelfmark only reads app.db at login and keeps a SIGNED cookie -> restart it after a reset
expect 'seen "docker: compose restart shelfmark"' "a password reset restarts Shelfmark so its signed sessions die (J07)"
expect 'grep -F msgbox "$LOG" | grep -q "may SURVIVE this reset until they expire"' "the reset message says open Calibre-Web / Audiobookshelf sessions can outlive the reset (J07)"
# J14: a password changed on CWA's own /me page never reaches Audiobookshelf
expect 'grep -F msgbox "$LOG" | grep -q "ONLY in the portal" && grep -F msgbox "$LOG" | grep -q "never reaches Audiobookshelf"' "the reset message tells the admin that users must change passwords only in the portal (J14)"

echo "== audiobookshelf setup + users"
envset AUTHELIA_ENABLED false
reset "root" "rootpass-1234" "rootpass-1234"; step_abs_setup && ok "step_abs_setup" || bad "step_abs_setup"
expect 'seen "python -m abs init --user root --password-stdin" && seen "docker-stdin: rootpass-1234" && [ "$(envget ABS_TOKEN)" = abs-key-STUB ] && [ "$(envget ABS_ROOT_USER)" = root ]' "ABS init (password on stdin) stores the API key"
expect 'seen "python -m abs ensure-user alice" && seen "python -m abs ensure-user bob" && ! seen "ensure-user admin"' "existing non-admin users aligned in ABS"
reset "dave" "dave@mail.example" "davepass-1234" "davepass-1234" "no" ""; step_user_add
expect 'seen "python -m abs ensure-user dave --password-stdin" && seen "docker-stdin: davepass-1234"' "new user gets an ABS account with the same password (stdin)"
reset "erin" "erin@mail.example" "erinpass-1234" "erinpass-1234" "yes" ""; step_user_add
expect '! seen "ensure-user erin"' "admins do not get a tag-restricted ABS account"
reset "alice" "newpass-9999" "newpass-9999"; step_user_passwd; expect 'seen "python -m abs ensure-user alice --password-stdin"' "password reset also updates ABS"
reset "alice" "yes"; step_user_remove; expect 'seen "python -m abs remove-user alice" && ! grep -q "^  alice:" "$STACK_DIR/authelia/users_database.yml" && grep -q "^  bob:" "$STACK_DIR/authelia/users_database.yml"' "user removal also removes the ABS account and the Authelia login (C9)"
reset; step_user_repair; expect 'seen "python -m abs ensure-user bob" && ! seen "abs ensure-user admin"' "repair aligns ABS accounts for non-admins"
reset "yes" "root" "rootpass-1234" "rootpass-1234"; step_abs_setup; expect 'seen "yesno: Audiobookshelf is already set up"' "re-running setup asks first"
reset "alice" "yes"; step_user_remove; expect 'seen "cwa remove-user alice"' "remove user after confirmation"
reset "alice" "no"; step_user_remove; expect '! seen "cwa remove-user"' "remove user aborted on No"
expect '! seen "compose restart shelfmark"' "an aborted removal does not restart anything"
# J07: the removed user's signed Shelfmark cookie keeps working until the container restarts
reset "alice" "yes"; step_user_remove
expect 'seen "docker: compose restart shelfmark" && [ "$(line_of "cwa remove-user alice")" -lt "$(line_of "compose restart shelfmark")" ]' "removing a user restarts Shelfmark afterwards, killing their session (J07)"
expect 'grep -F msgbox "$LOG" | grep -q "any session .* still had open there is dead" && grep -F msgbox "$LOG" | grep -q "until that session expires"' "the removal message explains the Shelfmark restart and the app sessions that may linger (J07)"
reset; step_user_repair; expect 'seen "cwa isolate bob" && seen "cwa isolate alice" && ! seen "cwa isolate admin" && seen "cwa harden"' "repair re-isolates every non-admin (never admins) + hardens"

echo "== formats, mail, defaults"
reset "yes" "no" "pdf,azw3" "new_record" "yes" "no"; step_formats && ok "step_formats" || bad "step_formats"
expect "seen \"UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='epub', kindle_epub_fixer=0, auto_convert_retained_formats='pdf,azw3', auto_ingest_automerge='new_record';\"" "formats written to CWA settings; target always epub"
expect '! grep -q "Target format" "$LOG" && ! grep -qE "azw3 \"AZW3|mobi \"MOBI|kepub \"KEPUB" "$LOG"' "no target-format picker: azw3/mobi/pdf/kepub are not offered as the conversion target (C12)"
expect "seen \"koreader_sync_enabled=1\" && [ \"\$(envget KOSYNC_ENABLED)\" = true ]" "KOReader sync toggled on in CWA and advertised to the portal"
expect 'seen "auto_backup_imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0"' "CWA file copies off by default"
reset "no" "yes" "x; DROP TABLE--" "overwrite" "no" "yes"; step_formats
expect "seen \"auto_convert_retained_formats='xdroptable'\" && seen \"koreader_sync_enabled=0\" && [ \"\$(envget KOSYNC_ENABLED)\" = false ] && seen \"auto_backup_imports=1\"" "retained-formats input is sanitised; KOReader off again; copies re-enabled on request"
reset "smtp.example.test" "465" "ssl" "user@x" "smtp-pass" "lib@x" ""; step_mail && ok "step_mail" || bad "step_mail"
expect '[ "$(envget SMTP_HOST)" = smtp.example.test ] && [ "$(envget SMTP_PORT)" = 465 ] && [ "$(envget SMTP_SECURITY)" = ssl ] && [ "$(envget SMTP_USER)" = user@x ] && [ "$(envget SMTP_PASS)" = smtp-pass ] && [ "$(envget SMTP_FROM)" = lib@x ]' "SMTP settings stored"
reset "smtp.example.test" "465" "ssl" "user@x" "" "lib@x" "me@x"; step_mail
expect '[ "$(envget SMTP_PASS)" = smtp-pass ] && seen "python -m kindle test me@x"' "blank password keeps the old one; test mail sent"
reset "smtp.example.test" "465" "ssl" "<cancel>"; step_mail; expect '[ "$(envget SMTP_USER)" = user@x ]' "Cancel at the SMTP username keeps the setting"
reset "<blank>"; step_mail; expect '[ -z "$(envget SMTP_HOST)" ]' "blank host disables mail"
envset SMTP_PORT ""; envset SMTP_FROM ""
reset "smtp2.example.test" "" "starttls" "u2@x" "pw2" "" ""; step_mail
expect '[ "$(envget SMTP_PORT)" = 587 ] && [ "$(envget SMTP_FROM)" = u2@x ] && seen "ask: SMTP port:"' "empty stored port/from -> real defaults 587 and the username (F71: envget always exits 0)"
envset SMTP_HOST ""
reset; apply_library_defaults; expect "seen \"cwa harden\" && seen \"auto_ingest_automerge='new_record'\" && seen \"auto_backup_imports=0\"" "library defaults: harden + conversion policy + no CWA copies"
# J31: per-user copies are intentional, so CWA must neither auto-resolve nor NOTIFY about them
expect 'seen "duplicate_auto_resolve_enabled=0" && seen "UPDATE cwa_settings SET duplicate_notifications_enabled=0;"' "duplicate auto-resolve AND the duplicate notification are both turned off (J31)"
expect '[ "$(grep -c "sqlite3 /config/cwa.db UPDATE cwa_settings SET duplicate_notifications_enabled=0;" "$LOG")" = 1 ]' "duplicate_notifications_enabled is its own statement (an older schema cannot take the other defaults down with it)"
expect 'grep -q "duplicate_notifications_enabled" "$REPO/scripts/selftest.sh"' "Self-test checks duplicate_notifications_enabled too (J31)"
reset; step_user_repair >/dev/null; expect 'seen "duplicate_notifications_enabled=0"' "Users -> Repair re-applies it as well (J31)"

echo "== sources"
reset '"GUTENBERG" "LIBRIVOX"' "gutenberg,cdl" "yes" "25x" "no"; step_sources
expect '[ "$(envget SRC_GUTENBERG)" = true ] && [ "$(envget SRC_STANDARD)" = false ] && [ "$(envget SRC_LIBRIVOX)" = true ] && [ "$(envget IA_COLLECTIONS)" = gutenberg,cdl ] && [ "$(envget APPROVALS_REQUIRED)" = true ] && [ "$(envget SRC_MYCATALOG)" = false ] && [ "$(envget MAX_REQUESTS_PER_DAY)" = 25 ]' "source toggles + approvals + sanitised daily quota stored"
expect '! grep -qi "torrent" "$LOG" && [ -z "$(envget IA_USE_TORRENT)" ]' "no Internet-Archive-over-torrent prompt any more (C5)"
envset MAX_REQUESTS_PER_DAY ""; reset '"GUTENBERG"' "" "no" "" "no"; step_sources
expect '[ "$(envget MAX_REQUESTS_PER_DAY)" = 30 ] && [ "$(envget APPROVALS_REQUIRED)" = false ]' "empty stored quota -> default 30 offered and kept"

echo "== fail2ban rendering"
# Cloudflare API stub with state: PATCHed settings and PUT/POSTed records read back (F13)
CFSTORE="$T/cfstore"; mkdir -p "$CFSTORE"
cf(){ echo "cf: $*" >> "$LOG"; local m="$1" path="$2" data="" n
  [ "${3:-}" = --data ] && data="$4"
  case "$m $path" in
    "GET /zones?name="*) echo '{"result":[{"id":"zone-STUB"}]}';;
    "PATCH "*/settings/*) [ "${path##*/}" = "${CF_FAIL_SETTING:-}" ] && return 22
       printf '%s' "$data" | command jq -r .value > "$CFSTORE/set_${path##*/}"; echo '{"success":true}';;
    "GET "*/settings/*) printf '{"result":{"value":"%s"}}\n' "$(cat "$CFSTORE/set_${path##*/}" 2>/dev/null)";;
    "GET "*"/dns_records?type=A&name="*) n="${path##*name=}"
       if [ -f "$CFSTORE/dns_$n" ]; then printf '{"result":[%s]}\n' "$(command jq -c '. + {id:"rec-STUB"}' "$CFSTORE/dns_$n")"; else echo '{"result":[]}'; fi;;
    "PUT "*/dns_records/*|"POST "*/dns_records) n=$(printf '%s' "$data" | command jq -r .name); printf '%s' "$data" > "$CFSTORE/dns_$n"; echo '{"success":true}';;
    *) echo '{"result":[]}';;
  esac; }
jq(){ command jq "$@"; }
mkdir -p "$T/etc"
rm -f "$STACK_DIR/caddy/data/access.log"
reset; render_fail2ban; rc=$?; j="$T/etc/fail2ban/jail.local"
expect '[ $rc = 0 ] && [ "$(grep -c "action   = cloudflare-token\[cftoken=\"cf-token-123\", cfzone=\"zone-STUB\"\]" "$j")" = 3 ] && ! grep -q "@@" "$j" && grep -q "^\[caddy-device-auth\]" "$j" && [ -f "$T/etc/fail2ban/filter.d/caddy-device-auth.conf" ]' "all three Caddy jails render with the Cloudflare token + zone and no placeholders left"
expect '[ "$(grep -c "^logpath  = $STACK_DIR/caddy/data/access.log$" "$j")" = 3 ] && [ -f "$STACK_DIR/caddy/data/access.log" ]' "jail logpath follows STACK_DIR and the access log is created (fail2ban starts after a restore, F15)"
# J25: Audiobookshelf has no login lockout of its own and its /login bypasses Authelia
af="$T/etc/fail2ban/filter.d/caddy-abs-login.conf"
expect 'grep -q "^\[caddy-abs-login\]" "$j" && grep -A9 "^\[caddy-abs-login\]" "$j" | grep -q "^maxretry = 10$" && grep -A9 "^\[caddy-abs-login\]" "$j" | grep -q "^findtime = 10m$" && grep -A9 "^\[caddy-abs-login\]" "$j" | grep -q "^bantime  = 1h$"' "Audiobookshelf login jail: 10 failures in 10 min -> 1 h ban via cloudflare-token (J25)"
expect '[ -f "$af" ] && ! grep -q "@@" "$af" && grep -q "audio\\\\.example\\\\.test" "$af" && grep -q "^datepattern = " "$af"' "its filter is rendered with the REGEX-ESCAPED domain (audio\\.example\\.test), no placeholders left (J25)"
python3 - "$af" "$REPO/configs/fail2ban/caddy-auth.conf" <<'PY' && ok "caddy-abs-login matches only POST /login 401 on audio.<domain>, keyed on client_ip (J25)" || bad "caddy-abs-login filter regex"
import re, sys
def rx(path):
    conf = open(path).read()
    return re.compile(re.search(r"failregex = (.*)", conf).group(1).replace("<HOST>", r"(?P<host>\S+?)"))
abs_, login = rx(sys.argv[1]), rx(sys.argv[2])
# a realistic Caddy JSON access line: client_ip ... method ... host ... uri ... status
line = lambda cip, m, host, uri, st: ('{"level":"info","ts":1790101150.84,"logger":"http.log.access.log0","msg":"handled request",'
  '"request":{"remote_ip":"172.71.150.23","remote_port":"41234","client_ip":"%s","proto":"HTTP/2.0","method":"%s","host":"%s","uri":"%s",'
  '"headers":{"User-Agent":["Audiobookshelf/2.36.1"],"Cf-Connecting-Ip":["%s"]},"tls":{"server_name":"%s"}},'
  '"bytes_read":58,"user_id":"","duration":0.01,"size":42,"status":%d}') % (cip, m, host, uri, cip, host, st)
assert abs_.search(line("203.0.113.9", "POST", "audio.example.test", "/login", 401)).group("host") == "203.0.113.9"   # the visitor, not Cloudflare
assert abs_.search(line("2001:db8::5", "POST", "audio.example.test", "/login?redirect=%2F", 401))
assert not abs_.search(line("203.0.113.9", "POST", "audio.example.test", "/login", 200))        # successful sign-in
assert not abs_.search(line("203.0.113.9", "GET",  "audio.example.test", "/login", 401))        # the login page itself
assert not abs_.search(line("203.0.113.9", "POST", "audio.example.test", "/api/authorize", 401))# app token refresh
assert not abs_.search(line("203.0.113.9", "POST", "audio.other.test",   "/login", 401))        # another zone entirely
assert not abs_.search(line("203.0.113.9", "POST", "request.example.test", "/login", 401))      # portal -> caddy-auth
assert not abs_.search(line("203.0.113.9", "GET",  "books.example.test", "/opds", 401))         # reader challenge
assert login.search(line("203.0.113.9", "POST", "request.example.test", "/login", 401))         # still the portal's jail
PY
python3 - "$REPO/configs/fail2ban/caddy-auth.conf" "$REPO/configs/fail2ban/caddy-device-auth.conf" <<'PY' && ok "caddy-auth counts login POSTs only; caddy-device-auth counts /opds + /kosync 401s, both by client_ip" || bad "fail2ban filter regexes"
import re, sys
def rx(path):
    conf = open(path).read()
    return re.compile(re.search(r"failregex = (.*)", conf).group(1).replace("<HOST>", r"(?P<host>\S+?)"))
login, dev = rx(sys.argv[1]), rx(sys.argv[2])
line = lambda ip, cip, m, uri, st: '{"request":{"remote_ip":"%s","remote_port":"1","client_ip":"%s","proto":"HTTP/2.0","method":"%s","host":"request.x","uri":"%s","headers":{}},"status":%d}' % (ip, cip, m, uri, st)
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 401)).group("host") == "203.0.113.9"      # the visitor, not Cloudflare
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/auth/login", 401))                          # Shelfmark
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/firstfactor", 401))                          # Authelia
assert not login.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 401))                                  # reader app challenge: other jail, looser
assert not login.search(line("172.71.1.1", "203.0.113.9", "GET", "/login", 200))
assert not login.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 302))                                # success
assert not login.search(line("172.71.1.1", "203.0.113.9", "POST", "/request", 401))
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 401)).group("host") == "203.0.113.9"
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds/new?page=2", 401))
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/kosync/users/auth", 401))
assert not dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 200))
assert not dev.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 401))
PY

echo "== cloudflare step"
envset TAILSCALE_IP 100.64.0.1; envset CF_API_TOKEN cf-token-123
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"; chmod +x "$STACK_DIR/scripts/cf-ips.sh"
rm -f "$CFSTORE"/*; reset; step_cloudflare && ok "step_cloudflare ran" || bad "step_cloudflare failed"
expect 'seen "cf: PATCH /zones/zone-STUB/settings/browser_check --data {\"value\":\"off\"}" && seen "cf: PATCH /zones/zone-STUB/settings/email_obfuscation --data {\"value\":\"off\"}" && seen "settings/rocket_loader --data {\"value\":\"off\"}"' "Browser Integrity Check, e-mail obfuscation and Rocket Loader set OFF"
expect 'seen "cf: PUT /zones/zone-STUB/rulesets/phases/http_request_cache_settings/entrypoint" && grep -F "http_request_cache_settings/entrypoint --data" "$LOG" | grep -q "\"cache\":false" && grep -F "cache_settings/entrypoint --data" "$LOG" | grep -q "books.example.test"' "no-cache Cache Rule for the four public hosts"
expect '! seen "Bot Fight Mode: ON" && grep -F "msgbox" "$LOG" | grep -q "Do NOT enable Bot Fight Mode"' "Bot Fight Mode advice: must stay OFF"
expect '[ -f "$T/etc/cron.d/bookstack-cfips" ] && grep -q "^PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin$" "$T/etc/cron.d/bookstack-cfips" && grep -q "cf-ips.sh" "$T/etc/cron.d/bookstack-cfips"' "cf-ips cron installed with a full PATH (ufw is in /usr/sbin, F31)"
expect 'seen "cf: POST /zones/zone-STUB/dns_records --data {\"type\":\"A\",\"name\":\"monitor.example.test\",\"content\":\"100.64.0.1\",\"ttl\":1,\"proxied\":false}" && ! grep -q "name\":\"dl.example.test" "$LOG" && ! grep -q "name\":\"aria.example.test" "$LOG" && seen "dns_records?type=A&name=aria.example.test"' "tailnet-only hosts grey-clouded; no dl. while torrents are off; stale aria. looked up for deletion"
expect 'seen "cf: GET /zones/zone-STUB/settings/ssl" && seen "cf: GET /zones/zone-STUB/settings/tls_client_auth" && grep -F msgbox "$LOG" | grep -q "read back and verified"' "SSL mode and origin pulls are read back before success is claimed (F13)"
rm -f "$CFSTORE"/*; CF_FAIL_SETTING=tls_client_auth; reset; step_cloudflare; rc=$?; CF_FAIL_SETTING=""
expect '[ $rc = 1 ] && grep -F "NOT fully configured" "$LOG" | grep -q "tls_client_auth" && ! grep -F msgbox "$LOG" | grep -q "Cloudflare configured"' "a failed zone setting is listed and the step fails instead of claiming success (F13)"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STACK_DIR/scripts/cf-ips.sh"; rm -f "$CFSTORE"/*; reset; step_cloudflare; rc=$?
expect '[ $rc = 1 ] && grep -F "NOT fully configured" "$LOG" | grep -q "firewall allowlist"' "a failed firewall allowlist makes the step fail"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"

echo "== deploy: Caddy last, admin password loop, ABS init before exposure"
rm -f "$STACK_DIR/caddy/cf-origin-pull-ca.pem"; reset; step_deploy; expect 'seen "Run the Cloudflare step first"' "deploy refuses without origin-pull CA"
touch "$STACK_DIR/caddy/cf-origin-pull-ca.pem"; mkdir -p "$STACK_DIR/cwa/config"; touch "$STACK_DIR/cwa/config/app.db"
envset ADMIN_PW_SET false; envset ABS_TOKEN ""
FAIL_BUILD=1; reset; step_deploy; rc=$?; FAIL_BUILD=0
expect '[ $rc = 1 ] && seen "Image build failed" && ! seen "compose up -d"' "compose build failure -> step_deploy returns 1 before anything starts"
reset "adminpass-1234" "adminpass-1234"; step_deploy && ok "step_deploy (ABS already initialised)" || bad "step_deploy failed"
first=$(grep -F "compose up -d calibre-web" "$LOG" | head -1)
expect '[ -n "$first" ] && ! printf "%s" "$first" | grep -q caddy' "first compose up does not include caddy"
expect 'seen "cwa passwd libadmin --password-stdin" && seen "docker-stdin: adminpass-1234" && [ "$(envget ADMIN_PW_SET)" = true ]' "admin password set via stdin for the chosen admin name, ADMIN_PW_SET recorded"
expect 'seen "cwa rename-user admin libadmin" && [ "$(line_of "cwa rename-user admin libadmin")" -lt "$(line_of "cwa passwd libadmin")" ]' "factory 'admin' renamed to ADMIN_USER before its password is set (C10/F19)"
expect 'seen "compose up -d caddy" && [ "$(line_of "cwa passwd libadmin")" -lt "$(line_of "compose up -d caddy")" ]' "caddy started only AFTER the admin password"
expect '[ -f "$T/etc/cron.d/bookstack-disk" ] && grep -q "disk-watch.sh" "$T/etc/cron.d/bookstack-disk" && grep -q "^PATH=/usr/local/sbin" "$T/etc/cron.d/bookstack-disk"' "hourly disk watchdog cron installed with a full PATH"
expect '! grep -E "compose .*up -d .*(qbittorrent|aria2|ariang)" "$LOG" && ! seen "profile torrents"' "torrents off: qBittorrent not started, no aria2/AriaNg (C4/C5)"
expect '[ "$(line_of "compose up -d caddy")" -lt "$(line_of "caddy validate")" ] && seen "caddy reload"' "an already-running Caddy gets the new Caddyfile validated + reloaded (F11)"
expect '! seen "python -m abs init"' "ABS setup not re-run when already initialised"
# F11: Deploy ships the checkout's code (a stale copy on the server is replaced)
echo "stale" > "$STACK_DIR/scripts/selftest.sh"; rm -f "$STACK_DIR/.version"; envset TORRENTS_ENABLED true
reset; step_deploy >/dev/null
expect 'cmp -s "$REPO/scripts/selftest.sh" "$STACK_DIR/scripts/selftest.sh" && [ -s "$STACK_DIR/.version" ]' "Deploy copies the repo's code trees and records the version (F11)"
# J03: the built image must carry the same identifier that .version records, or a stale portal
# image is indistinguishable from a fresh one
expect 'seen "build-version: " && ! seen "build-version: <unset>" && [ "$(grep -m1 "^build-version: " "$LOG" | cut -d" " -f2)" = "$(cut -d" " -f1 "$STACK_DIR/.version")" ]' "Deploy passes BUILD_VERSION to the image build and .version records the same value (J03)"
v1=$(build_version); expect '[ -n "$v1" ] && [ "${v1#* }" = "$v1" ]' "build_version() returns a single non-empty token"
( SRC_DIR="$T"; record_version ); expect '[ -n "$(deployed_version)" ] && grep -qE "^[^ ]+ [0-9]{4}-[0-9]{2}-[0-9]{2}T" "$STACK_DIR/.version"' "outside a git checkout the version falls back to a date stamp, still one token + timestamp (J03)"
record_version
expect 'grep -q "healthz?detail=1" "$REPO/scripts/selftest.sh" && grep -q "STALE PORTAL IMAGE" "$REPO/scripts/selftest.sh" && grep -q "\$STACK_DIR/.version" "$REPO/scripts/selftest.sh"' "Self-test compares the running portal's reported version with \$STACK_DIR/.version (J03)"
expect 'seen "docker: compose --profile torrents up -d calibre-web audiobookshelf uptime-kuma qbittorrent"' "torrents on: every compose call carries --profile torrents and qBittorrent starts"
envset TORRENTS_ENABLED false
FAIL_RENAME=1; envset ADMIN_PW_SET false; reset "adminpass-1234" "adminpass-1234"; step_deploy >/dev/null; FAIL_RENAME=0
expect 'seen "cwa passwd admin --password-stdin" && [ "$(envget ADMIN_USER)" = admin ] && [ "$(envget ADMIN_PW_SET)" = true ]' "rename fails -> the factory admin still gets a new password (never left on admin123)"
envset ADMIN_USER libadmin; envset ADMIN_USER_PREV ""
envset ADMIN_PW_SET false
reset "<cancel>"; step_deploy && ok "step_deploy with Cancel at the password prompt" || bad "step_deploy (cancel) failed"
gen=$(grep -F "docker-stdin: " "$LOG" | head -1 | cut -d' ' -f2-)   # (rename-user has no stdin)
expect '[ -n "$gen" ] && [ "${#gen}" -ge 20 ] && [ "$(grep -c "GENERATED password: $gen  (change it" "$LOG")" = 1 ] && [ "$(envget ADMIN_PW_SET)" = true ]' "Cancel generates a random admin password, sets it and shows it once (no factory default left)"
envset ADMIN_PW_SET false
FAIL_PASSWD=1; reset "adminpass-1234" "adminpass-1234" "no"; step_deploy; rc=$?; FAIL_PASSWD=0
expect '[ $rc = 1 ] && ! seen "compose up -d caddy" && [ "$(envget ADMIN_PW_SET)" = false ]' "portal cannot set the password + no retry -> Caddy NOT started, returns 1"
envset ADMIN_PW_SET false
ABS_INIT=false; reset "adminpass-1234" "adminpass-1234" "root" "rootpass-1234" "rootpass-1234"; step_deploy; rc=$?; ABS_INIT=true
expect '[ $rc = 0 ] && seen "python -m abs init --user root --password-stdin" && [ "$(line_of "abs init")" -lt "$(line_of "compose up -d caddy")" ] && [ "$(envget ABS_TOKEN)" = abs-key-STUB ]' "uninitialised ABS gets its root user BEFORE caddy starts"
envset ADMIN_PW_SET false; envset ABS_TOKEN ""
ABS_INIT=false; reset "adminpass-1234" "adminpass-1234" "<cancel>" "no"; step_deploy; rc=$?; ABS_INIT=true
expect '[ $rc = 1 ] && ! seen "compose up -d caddy"' "ABS setup cancelled + 'start anyway' declined -> Caddy NOT started"
reset "no"; step_quick; expect '! seen "apt-get"' "quick install does nothing when declined"
q=$(declare -f step_quick)
expect '! printf "%s" "$q" | grep -q step_abs_setup && printf "%s" "$q" | grep -q step_fail2ban && printf "%s" "$q" | grep -q step_alerts && printf "%s" "$q" | grep -q step_lock_ssh && printf "%s" "$q" | grep -q "tailscale status"' "Quick install: no duplicate ABS prompt; ends with alerts, fail2ban and a Tailscale-checked Lock SSH offer (F71)"

echo "== Shelfmark's literal '{User}' dropbox folder (J35)"
ph="$STACK_DIR/library/dropbox/{User}"
mkdir -p "$ph"; prune_shelfmark_placeholder; rc=$?
expect '[ $rc = 0 ] && [ ! -d "$ph" ]' "an EMPTY '{User}' folder (Shelfmark's un-substituted INGEST_DIR) is removed"
mkdir -p "$ph"; echo x > "$ph/book.epub"; prune_shelfmark_placeholder; rc=$?
expect '[ $rc = 0 ] && [ -f "$ph/book.epub" ]' "a NON-empty '{User}' folder is left alone: nobody's files are deleted"
rm -rf "$ph"; prune_shelfmark_placeholder; rc=$?
expect '[ $rc = 0 ]' "no '{User}' folder at all: the prune still succeeds (never breaks the step)"
expect '[ -d "$STACK_DIR/library/dropbox/alice" ]' "real per-user dropboxes are untouched"
expect 'declare -f step_deploy | grep -q prune_shelfmark_placeholder && declare -f stack_up_all | grep -q prune_shelfmark_placeholder && declare -f step_shelfmark | grep -q prune_shelfmark_placeholder && declare -f restart_shelfmark | grep -q prune_shelfmark_placeholder' "every path that starts Shelfmark prunes the placeholder afterwards (J35)"

echo "== backups, alerts, restore, lock SSH"
export RLOG="$T/restic.log" FAKESNAP="$T/fakesnap"; : > "$RLOG"
cat > "$bin/restic" <<'EOS'
#!/usr/bin/env bash
echo "restic: $*" >> "$RLOG"
while [ "${1:-}" = --retry-lock ]; do shift 2; done
case "${1:-}" in
  cat) [ "${RESTIC_NOREPO:-0}" = 1 ] && exit 1;;
  restore) [ "${2:-}" = --help ] && exit 0
    tgt=""; incs=(); shift 2
    while [ $# -gt 0 ]; do case "$1" in --target) tgt="$2"; shift;; --include) incs+=("$2"); shift;; esac; shift; done
    for p in "${incs[@]}"; do [ -e "$FAKESNAP$p" ] || continue; d="$tgt/${p%/*}"; mkdir -p "$d"; cp -a "$FAKESNAP$p" "$d/"; done;;
  snapshots) case "$*" in *--json*) printf '[{"time":"%s","id":"abc12345ffffffff","short_id":"abc12345","tags":["bookstack"],"paths":["/srv/bookstack"]}]\n' "${RESTIC_SNAP_TIME:-$(date -u +%Y-%m-%dT%H:%M:%S.123456789Z)}";; esac;;
  stats) echo "{\"total_size\":${RESTIC_STATS_SIZE:-1}}";;
esac
exit 0
EOS
chmod +x "$bin/restic"
reset "/mnt/backup" "resticpass-123" "resticpass-123" "https://hc-ping.example/uuid" "no"; step_backup && ok "step_backup" || bad "step_backup failed"
renv="$T/etc/bookstack/restic.env"; u="$T/etc/systemd/system"
expect 'grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv" && grep -q "^RESTIC_PASSWORD=resticpass-123$" "$renv" && [ "$(stat -c %a "$renv" 2>/dev/null || stat -f %Lp "$renv")" = 600 ]' "restic.env written 0600 with repo + password"
expect 'grep -q "restic: cat config" "$RLOG" && ! grep -q "restic: init" "$RLOG" && [ "$(envget BACKUP_PING_URL)" = https://hc-ping.example/uuid ]' "existing repository opened (not re-initialised); optional ping URL stored (C1)"
expect 'grep -q "^OnCalendar=\*-\*-\* 01:00:00" "$u/bookstack-backup.timer" && grep -q "^OnFailure=bookstack-alert@backup.service" "$u/bookstack-backup.service"' "backup at 01:00 with OnFailure alert"
expect 'grep -q "^OnCalendar=\*-\*-01 13:00:00" "$u/bookstack-restore-test.timer" && grep -q "restore-test.sh" "$u/bookstack-restore-test.service" && grep -q "^OnFailure=bookstack-alert@restore-test.service" "$u/bookstack-restore-test.service"' "restore test on the 1st at 13:00 (never overlaps the 01:00 backup, F40) with OnFailure alert"
expect 'grep -q "scripts/alert.sh" "$u/bookstack-alert@.service" && grep -q "%i" "$u/bookstack-alert@.service"' "templated bookstack-alert@.service"
expect 'seen "systemctl: enable --now bookstack-backup.timer bookstack-restore-test.timer" && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server" && grep -F msgbox "$LOG" | grep -q "NO alert channel"' "timers enabled; offsite-secrets checklist shown; missing alert channel called out"
: > "$RLOG"; export RESTIC_NOREPO=1; reset "/mnt/backup" "resticpass-123" "resticpass-123" "" "no"; step_backup; export RESTIC_NOREPO=0
expect 'grep -q "restic: init" "$RLOG"' "a new repository is initialised by the Backups step (only there, F67)"
reset "s3:s3.example/bucket" "resticpass-123" "resticpass-123" "<cancel>"; step_backup; expect '[ $? = 1 ] && grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv"' "Cancel at the S3 key prompt aborts without touching restic.env"
# C1: alerts step
printf '#!/usr/bin/env bash\necho "alert.sh $*" >> "%s"\n' "$LOG" > "$STACK_DIR/scripts/alert.sh"; chmod +x "$STACK_DIR/scripts/alert.sh"
reset "https://ntfy.sh/family-secret-topic" "yes"; step_alerts; rc=$?
expect '[ $rc = 0 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ] && seen "compose up -d librarian" && seen "alert.sh Bookstack test alert" && seen "yesno: A test alert was sent"' "Alerts: webhook stored, portal restarted, test alert sent through alert.sh, admin confirms (C1)"
reset "" "no"; step_alerts; rc=$?; expect '[ $rc = 1 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ]' "blank keeps the webhook; unconfirmed delivery returns 1"
reset "ftp://nope"; step_alerts; expect '[ $? = 1 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ]' "non-http URL refused"
expect 'declare -f menu_install | grep -q step_alerts && declare -f menu_ops | grep -q step_alerts' "Alerts in the Install and Operations menus"
TS_EXPIRY='"2027-03-01T00:00:00Z"'; reset "yes" "no"; step_lock_ssh; rc=$?; expect '[ $rc = 1 ] && ! seen "ufw: --force delete" && grep -F "msgbox" "$LOG" | grep -q "Disable key expiry"' "key expiry set + 'not done' -> SSH stays public, told what to do"
envset SSH_LOCKED false
reset "yes" "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp" && [ "$(envget SSH_LOCKED)" = true ]' "confirmed -> port 22 closed and SSH_LOCKED recorded (F12)"; TS_EXPIRY=null
reset "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp" && ! grep -q "yesno: IMPORTANT" "$LOG"' "KeyExpiry null -> no extra prompt"
envset SSH_LOCKED false
reset; step_tailscale >/dev/null; expect 'grep -F "msgbox" "$LOG" | grep -q "Disable key expiry" && grep -q "advertise-tags=tag:bookstack" "$LOG" && [ "$(envget TAILSCALE_IP)" = 100.64.0.1 ]' "Tailscale step: key expiry warning + tag:bookstack ACL advice; IP stored"
TS_EXPIRY='"2027-03-01T00:00:00Z"'; reset "no"; step_tailscale >/dev/null; rc=$?; TS_EXPIRY=null
expect '[ $rc = 0 ] && seen "yesno: Key expiry is still ENABLED"' "Tailscale step checks KeyExpiry and asks the admin to disable it (C3)"
# restore onto this server: pick a snapshot, stop, restore in place, DB copies per MANIFEST, keep fresh IPs, restart
mkdir -p "$T/fakesnap$STACK_DIR/.backup-snap" "$T/fakesnap$STACK_DIR/cwa/config" "$T/fakesnap$STACK_DIR/library/books"
python3 - "$T/fakesnap$STACK_DIR" <<'PY'
import sqlite3, sys, os
d = sys.argv[1]
c = sqlite3.connect(d + "/.backup-snap/cwa_config_app.db"); c.execute("create table user(id int)"); c.execute("insert into user values(1)"); c.commit(); c.close()
open(d + "/.backup-snap/MANIFEST", "w").write("cwa_config_app.db\tcwa/config/app.db\n")
open(d + "/cwa/config/app.db", "w").write("RAW-INCONSISTENT"); open(d + "/cwa/config/app.db-wal", "w").write("wal")
open(d + "/.env", "w").write("DOMAIN='example.test'\nPUBLIC_IP='198.51.100.9'\nTAILSCALE_IP='100.64.9.9'\nTZ='Europe/Oslo'\nABS_TOKEN='from-snapshot'\nCF_API_TOKEN='cf-token-123'\nAUTHELIA_ENABLED='false'\nADMIN_HASH='$2a$14$SNAPSHOTHASH/xyz'\n")
open(d + "/docker-compose.yml", "w").write("services: {}\n")
PY
rsync(){ command rsync "$@"; }
fail2ban-client(){ :; }       # "installed": restore must restart fail2ban AFTER the stack is up
envset PUBLIC_IP 203.0.113.5; envset TAILSCALE_IP 100.64.0.1; envset TZ UTC; envset ABS_TOKEN old-token
echo "live" > "$STACK_DIR/cwa/config/app.db-wal"; echo "book" > "$T/fakesnap$STACK_DIR/library/books/big.epub"
: > "$RLOG"; reset "abc12345" "full" "yes" "yes"; step_restore && ok "step_restore ran" || bad "step_restore failed"
expect 'grep -q "restic: snapshots --json" "$RLOG" && grep -F "whiptail: " "$LOG" | grep -q "Restore: pick a snapshot" && grep -q "restic: stats abc12345 --mode restore-size --json" "$RLOG"' "snapshot picker (restic snapshots --json) and a restore-size check before anything stops (F01/F16)"
expect 'grep -qx "restic: restore abc12345 --target / --include $STACK_DIR" "$RLOG" && seen "docker: compose down" && [ -f "$STACK_DIR/library/books/big.epub" ] && ! ls -d "$(dirname "$STACK_DIR")"/.bs-restore.* >/dev/null 2>&1' "restores the picked snapshot IN PLACE (target /), no temporary copy of the library (F01)"
expect '[ "$(envget ABS_TOKEN)" = from-snapshot ] && [ "$(envget PUBLIC_IP)" = 203.0.113.5 ] && [ "$(envget TAILSCALE_IP)" = 100.64.0.1 ] && [ "$(envget TZ)" = UTC ]' "snapshot .env restored, but this server's PUBLIC_IP / TAILSCALE_IP / TZ kept"
expect '[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from user\").fetchone()[0])" "$STACK_DIR/cwa/config/app.db")" = 1 ] && [ ! -f "$STACK_DIR/cwa/config/app.db-wal" ]' "consistent DB copy replaced the raw file per MANIFEST; stale -wal removed"
expect 'seen "docker: compose up -d" && [ "$(line_of "compose down")" -lt "$(line_of "compose up -d")" ] && [ -f "$T/etc/cron.d/bookstack-disk" ] && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server"' "stack restarted, watchdog re-installed, checklist shown"
expect 'seen "systemctl: restart fail2ban" && [ "$(line_of "compose up -d")" -lt "$(line_of "systemctl: restart fail2ban")" ] && [ -f "$STACK_DIR/caddy/data/access.log" ]' "fail2ban restarted after the stack is up, with its log file present (F15)"
: > "$RLOG"; reset "abc12345" "config" "yes" "yes"; step_restore >/dev/null
expect 'grep "restic: restore abc12345 --target /" "$RLOG" | grep -q -- "--include $STACK_DIR/.env --include $STACK_DIR/.backup-snap" && ! grep -qE -- "--include $STACK_DIR( |$)" "$RLOG" && ! grep -q "restic: stats" "$RLOG"' "'config + databases only' restores .env, DB copies and app configs, not the library (F16)"
: > "$RLOG"; export RESTIC_STATS_SIZE=999999999999999; reset "abc12345" "full"; step_restore; rc=$?; unset RESTIC_STATS_SIZE
expect '[ $rc = 1 ] && ! grep -q "restic: restore" "$RLOG" && ! seen "compose down" && grep -F msgbox "$LOG" | grep -q "Not enough disk space"' "snapshot larger than the free space: refused before the stack is stopped (F01)"
reset "abc12345" "full" "yes" "no"; : > "$RLOG"
step_restore; rc=$?
expect '[ "$rc" != 0 ] && ! grep -q "restic: restore" "$RLOG" && ! seen "compose down"' "second confirmation declined -> nothing restored, stack not stopped"
reset "<cancel>"; step_restore; expect '[ $? != 0 ] && ! seen "compose down"' "Cancel in the snapshot picker changes nothing"
unset -f fail2ban-client

echo "== update: pre-update backup, code + tags, local health gate, rollback"
printf '#!/usr/bin/env bash\necho "backup.sh $*" >> "%s"\nexit ${BACKUP_RC:-0}\n' "$LOG" > "$STACK_DIR/scripts/backup.sh"
printf '#!/usr/bin/env bash\nexit ${SELFTEST_RC:-0}\n' > "$STACK_DIR/scripts/selftest.sh"; chmod +x "$STACK_DIR/scripts/"*.sh
copy_code_trees(){ echo "copy_code_trees" >> "$LOG"; record_version; }   # keep the stub scripts above in place
C6="<cancel> <cancel> <cancel> <cancel> <cancel> <cancel>"   # skip the remaining 6 of 7 image prompts
export BACKUP_RC=1; reset; step_update; rc=$?; export BACKUP_RC=0
expect '[ $rc = 1 ] && seen "backup.sh --tag pre-update" && seen "Pre-update backup failed" && ! seen "compose pull"' "step_update returns 1 when the pre-update backup fails (nothing pulled)"
envset IMG_CWA crocodilestick/calibre-web-automated:v4.0.6
reset "yes" "crocodilestick/calibre-web-automated:v4.0.7" $C6 "yes"; step_update && ok "step_update (bump one tag)" || bad "step_update failed"
expect 'grep -q "^IMG_CWA=crocodilestick/calibre-web-automated:v4.0.6$" "$STACK_DIR/.env.images.prev" && ! grep -q "IMG_ARIA" "$STACK_DIR/.env.images.prev"' ".env.images.prev records the previous tags (no aria2 keys)"
expect '[ "$(envget IMG_CWA)" = crocodilestick/calibre-web-automated:v4.0.7 ] && grep -q "IMG_CWA: crocodilestick/calibre-web-automated:v4.0.6 -> crocodilestick/calibre-web-automated:v4.0.7" "$LOG"' "new tag stored and shown old -> new before applying"
expect 'seen "compose pull --ignore-buildable" && seen "compose build --pull caddy librarian" && seen "compose up -d" && seen "docker: system prune -f --filter until=72h" && [ "$(line_of "compose up -d")" -lt "$(line_of "system prune")" ]' "pull/build/up then prune only at the end"
expect 'seen "copy_code_trees" && [ "$(line_of "copy_code_trees")" -lt "$(line_of "compose build")" ] && seen "caddy validate"' "Update deploys the checkout's code and re-renders/validates the Caddyfile before building (F11)"
expect 'seen "build-version: " && ! seen "build-version: <unset>" && [ "$(grep -m1 "^build-version: " "$LOG" | cut -d" " -f2)" = "$(cut -d" " -f1 "$STACK_DIR/.version")" ]' "Update rebuilds with BUILD_VERSION and re-records .version (J03)"
expect 'seen "docker: image tag bookstack/caddy:latest bookstack/caddy:prev" && seen "docker: image tag bookstack/librarian:latest bookstack/librarian:prev" && [ "$(line_of "image tag bookstack/caddy:latest")" -lt "$(line_of "compose build")" ]' "locally built caddy/librarian images kept as :prev before the rebuild (F17)"
expect 'seen "curl: -fs -m 5 -o /dev/null http://127.0.0.1:8090/healthz" && seen "curl: -fs -m 5 -o /dev/null http://127.0.0.1:13378/healthcheck"' "gate = health checks + local endpoints"
export SELFTEST_RC=3; reset "no" "yes"; step_update; rc=$?; export SELFTEST_RC=0
expect '[ $rc = 0 ] && ! seen "yesno: Update problem" && seen "system prune" && grep -q "3 failure(s) unrelated" "$LOG"' "a failing full self-test (disk, NTP, Tailscale...) is reported but is NOT a rollback reason (F17)"
FAIL_HEALTHZ=1; reset "no" "yes" "no"; step_update; rc=$?; FAIL_HEALTHZ=0
expect '[ $rc = 1 ] && grep -F "yesno: Update problem" "$LOG" | grep -q "portal:/healthz" && ! seen "system prune"' "a failing local endpoint -> rollback offered, no prune"
envset IMG_CWA x/y:old; printf 'IMG_CWA=x/y:old\n' > "$STACK_DIR/.env.images.prev"
reset "yes" "x/y:new" $C6 "no"; step_update
expect '[ "$(envget IMG_CWA)" = x/y:old ] && ! seen "compose pull"' "declining at the final confirmation restores the previous tags"
echo "# before update" > "$STACK_DIR/caddy/Caddyfile"
FAIL_HEALTHZ=1; reset "yes" "x/y:new" $C6 "yes" "yes"; step_update; FAIL_HEALTHZ=0
expect '[ "$(envget IMG_CWA)" = x/y:old ] && [ "$(grep -c "docker: compose up -d" "$LOG")" -ge 2 ] && seen "docker: image tag bookstack/caddy:prev bookstack/caddy:latest" && [ "$(cat "$STACK_DIR/caddy/Caddyfile")" = "# before update" ]' "rollback restores the previous tags, the previous caddy/librarian builds and the previous Caddyfile"
expect 'grep -F msgbox "$LOG" | grep -q "tagged .pre-update. -> .Config + databases only."' "rollback advice names the pre-update snapshot and the config + databases restore (F16)"
rm -f "$renv"; reset "no"; step_update; expect '[ $? = 1 ] && seen "yesno: No backup repository is configured"' "without backups, Update asks and stops on No"
unset -f copy_code_trees; source <(sed -n '/^copy_code_trees(){/,/^}/p' "$REPO/bookstack.sh")

echo "== menus survive failures (F34)"
printf '#!/usr/bin/env bash\necho "selftest ran"\nexit 3\n' > "$STACK_DIR/scripts/selftest.sh"; chmod +x "$STACK_DIR/scripts/selftest.sh"
reset "T" "S" "<esc>" "0"; out=$( (set -euo pipefail; menu_ops </dev/null; echo "MENU-RETURNED") 2>/dev/null )
expect 'printf "%s" "$out" | grep -q "selftest ran" && printf "%s" "$out" | grep -q "MENU-RETURNED"' "failing self-test and Esc in Status return to the menu under errexit"
reset "L" "caddy" "0"; out=$( (set -euo pipefail; FAIL_LOGS=1 menu_ops </dev/null; echo "MENU-RETURNED") 2>/dev/null )
expect 'printf "%s" "$out" | grep -q "MENU-RETURNED" && declare -f step_logs | grep -q "trap : INT"' "Logs: an ending/failing docker logs returns to the menu; Ctrl-C is trapped"

echo "== helper scripts as real processes (stub restic/docker on PATH)"
cat > "$bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker: $*" >> "$DLOG"
case "$*" in
  *"inspect -f"*qbittorrent*) [ "${QBIT_RUNNING:-0}" = 1 ] && echo true || echo false;;
  *"python -m notify alert"*) exit "${NOTIFY_RC:-0}";;
esac
exit 0
EOS
chmod +x "$bin"/*; : > "$DLOG"; : > "$RLOG"
fs="$T/fs"; mkdir -p "$fs/cwa/config" "$fs/library/books" "$fs/librarian/state" "$fs/downloads/incomplete" "$fs/library/staging" "$fs/library/ingest" "$fs/shelfmark/config" "$fs/scripts"
cp "$REPO/scripts/alert.sh" "$fs/scripts/"
python3 - "$fs" <<'PY'
import sqlite3, sys
d = sys.argv[1]
c = sqlite3.connect(d + "/cwa/config/app.db"); c.execute("pragma journal_mode=wal"); c.execute("create table user(id int, name text)"); c.execute("insert into user values(1,'admin')"); c.commit()  # keep open: -wal exists while 'running'
m = sqlite3.connect(d + "/library/books/metadata.db"); m.execute("create table books(id int)"); m.execute("insert into books values(1)"); m.commit(); m.close()
s = sqlite3.connect(d + "/shelfmark/config/shelfmark.db"); s.execute("create table t(x)"); s.commit(); s.close()
open(d + "/library/ingest/stuck.epub", "w").write("x")
PY
printf 'RESTIC_REPOSITORY=/mnt/backup\nRESTIC_PASSWORD=x\n' > "$T/restic.env"
printf "BACKUP_PING_URL='https://hc-ping.example/uuid'\n" > "$fs/.env"
BACKUP_CHECK_DOW=$(date +%u) STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" --tag pre-update >"$T/backup.out" 2>&1 && ok "backup.sh runs" || { bad "backup.sh failed"; cat "$T/backup.out"; }
expect '[ -f "$fs/.backup-snap/cwa_config_app.db" ] && [ -f "$fs/.backup-snap/library_books_metadata.db" ] && [ -f "$fs/.backup-snap/shelfmark_config_shelfmark.db" ] && grep -q "^cwa_config_app.db	cwa/config/app.db$" "$fs/.backup-snap/MANIFEST"' "consistent SQLite copies + MANIFEST (incl. shelfmark glob)"
expect '[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from user\").fetchone()[0])" "$fs/.backup-snap/cwa_config_app.db")" = 1 ]' "snapshot copy contains the committed row (WAL-safe backup API)"
expect 'grep -q "restic: --retry-lock 30m backup $fs --exclude $fs/downloads .*--exclude $fs/cwa/config/processed_books --exclude $fs/library/staging --exclude $fs/ephemera/downloads --exclude $fs/abs/metadata/cache --exclude $fs/abs/metadata/logs --exclude \*.db-wal --exclude \*.db-shm --exclude \*.sqlite-wal --exclude \*.sqlite-shm --tag bookstack --tag pre-update" "$RLOG"' "restic backup with the excludes (incl. re-downloadable caches, F77), --retry-lock and the extra tag"
rl(){ grep -nF -- "$1" "$RLOG" | head -1 | cut -d: -f1; }
expect '[ "$(rl "restic: --retry-lock 30m check --read-data-subset=5%")" -lt "$(rl "restic: --retry-lock 30m forget")" ] && grep -q "restic: --retry-lock 30m forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-tag pre-update --prune" "$RLOG" && grep -q "restic: --retry-lock 30m stats latest --json" "$RLOG"' "weekly restic check runs BEFORE forget --prune; stats logged"
expect '! grep -q "restic: .*init" "$RLOG" && ! grep -q "tag --remove" "$RLOG" && grep -q "curl: -fsS -m 10 --retry 3 https://hc-ping.example/uuid$" "$DLOG"' "backup.sh never inits; recent pre-update snapshots kept; success ping sent (C1)"
: > "$RLOG"; BACKUP_CHECK_DOW=8 STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect '! grep -q "restic: .* check" "$RLOG"' "no check on the other days of the week (F77)"
: > "$RLOG"; RESTIC_SNAP_TIME=2020-01-01T00:00:00Z STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect 'grep -q "restic: --retry-lock 30m tag --remove pre-update abc12345ffffffff" "$RLOG"' "pre-update snapshots older than 90 days lose their keep tag (F77)"
: > "$RLOG"; : > "$DLOG"; RESTIC_NOREPO=1 STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >"$T/b2.out" 2>&1; rc=$?
expect '[ $rc != 0 ] && ! grep -q "restic: .*init" "$RLOG" && ! grep -q "restic: .* backup" "$RLOG" && grep -q "not reachable or not initialised" "$T/b2.out" && grep -q "hc-ping.example/uuid/fail" "$DLOG"' "unreachable repository: fails without creating one, failure ping sent (F67, C1)"
echo "not a database" > "$fs/cwa/config/cwa.db"; : > "$RLOG"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >"$T/b3.out" 2>&1; rc=$?; rm -f "$fs/cwa/config/cwa.db"
expect '[ $rc != 0 ] && grep -q "restic: --retry-lock 30m backup" "$RLOG" && grep -q "BACKUP INCOMPLETE: no consistent copy of: cwa/config/cwa.db" "$T/b3.out"' "a DB that cannot be snapshotted: backup still taken, run reported as failed (F68)"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
: > "$RLOG"; export FAKESNAP="$T/fakesnap2"; mkdir -p "$FAKESNAP$fs"; cp -a "$fs/." "$FAKESNAP$fs/"; printf 'DOMAIN=x\n' > "$FAKESNAP$fs/.env"; printf 'x\n' > "$FAKESNAP$fs/docker-compose.yml"; mkdir -p "$FAKESNAP$fs/caddy"; : > "$FAKESNAP$fs/caddy/Caddyfile"
cp -a "$T/rt.out" "$T/rt.prev" 2>/dev/null; STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/restore-test.sh" >"$T/rt.out" 2>&1 && ok "restore-test.sh passes on a good snapshot" || { bad "restore-test.sh failed"; cat "$T/rt.out"; }
expect 'grep -q "integrity: cwa/config/app.db" "$T/rt.out" && grep -q "app.db has 1 user" "$T/rt.out" && grep -q "metadata.db has 1 book" "$T/rt.out" && grep -q "snapshot is 0 h old" "$T/rt.out" && grep -q "consistent copy of cwa/config/app.db" "$T/rt.out" && grep -q "full restore needs" "$T/rt.out" && grep -q "restic: --retry-lock 30m restore latest" "$RLOG"' "restore test checks core DB copies, integrity, counts, age and reports the full-restore size (F68, F01)"
grep -v "^cwa_config_app.db" "$FAKESNAP$fs/.backup-snap/MANIFEST" > "$T/m.n"; cp "$T/m.n" "$FAKESNAP$fs/.backup-snap/MANIFEST"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/restore-test.sh" >"$T/rt3.out" 2>&1; expect '[ $? != 0 ] && grep -q "no consistent copy of cwa/config/app.db" "$T/rt3.out"' "restore test FAILS when a core DB lacks its consistent copy (F68)"
rm -f "$FAKESNAP$fs/.backup-snap/MANIFEST"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/restore-test.sh" >"$T/rt2.out" 2>&1; expect '[ $? != 0 ] && grep -q "missing .backup-snap/MANIFEST" "$T/rt2.out"' "restore test FAILS when the snapshot lacks the consistent copies"
# disk watchdog
cat > "$bin/df" <<'EOS'
#!/usr/bin/env bash
case "$*" in *--output=pcent*) printf 'Use%%\n %s%%\n' "$DF_PCT";; *--output=avail*) printf 'Avail\n1000000\n';; *) printf 'Filesystem Size Used Avail Use%% Mounted\n/dev/x 80G 70G 10G %s%% /\n' "$DF_PCT";; esac
EOS
chmod +x "$bin/df"
touch -t 202001010000 "$fs/downloads/incomplete/old.part" "$fs/library/staging/old.bin" "$fs/library/ingest/old.part"; touch "$fs/downloads/incomplete/new.part"
printf "TORRENTS_ENABLED='false'\n" > "$fs/.env"
dw(){ DF_PCT=$1 STACK_DIR="$fs" DISK_STATE="$T/disk.state" bash "$REPO/scripts/disk-watch.sh"; }
: > "$DLOG"; dw 96 && ok "disk-watch.sh runs" || bad "disk-watch.sh failed"
expect 'grep -q "docker: compose stop shelfmark" "$DLOG" && ! grep -q "aria2" "$DLOG" && ! grep -q "stop qbittorrent" "$DLOG" && grep -q "^paused=1" "$T/disk.state" && grep -q "python -m notify alert Disk 96% full" "$DLOG" && grep -q " high$" "$DLOG"' "96 %: shelfmark stopped (no aria2; qBittorrent not running), high-priority alert, state recorded"
expect '[ ! -f "$fs/downloads/incomplete/old.part" ] && [ ! -f "$fs/library/staging/old.bin" ] && [ ! -f "$fs/library/ingest/old.part" ] && [ -f "$fs/downloads/incomplete/new.part" ] && [ -f "$fs/library/ingest/stuck.epub" ]' "stale partials/staging deleted; fresh files and real ingest files kept"
expect 'grep -q "journalctl: --vacuum-size=200M" "$DLOG" && grep -q "docker: builder prune -f --filter until=168h" "$DLOG"' "journal and build cache trimmed"
: > "$DLOG"; dw 96; expect '! grep -q "notify alert" "$DLOG"' "still 96 %: no repeated alert"
: > "$DLOG"; dw 50; expect 'grep -q "docker: compose start shelfmark" "$DLOG" && ! grep -q qbittorrent "$DLOG" && grep -q "^paused=0" "$T/disk.state"' "back under 80 %: shelfmark started again; qBittorrent left alone while torrents are off"
printf "TORRENTS_ENABLED='true'\n" > "$fs/.env"
: > "$DLOG"; QBIT_RUNNING=1 dw 97; expect 'grep -q "docker: compose --profile torrents stop qbittorrent" "$DLOG" && grep -q "qbittorrent" "$DLOG"' "96 %+ with torrents running: qBittorrent container stopped (F32)"
: > "$DLOG"; dw 40; expect 'grep -q "docker: compose --profile torrents up -d qbittorrent" "$DLOG"' "below 80 % with torrents enabled: qBittorrent started again"
grep -v '^last_alert=' "$T/disk.state" > "$T/disk.state.n"; mv "$T/disk.state.n" "$T/disk.state"   # pretend the last alert was long ago
: > "$DLOG"; dw 88; expect '! grep -q "compose stop" "$DLOG" && grep -q "notify alert Disk 88% full" "$DLOG"' "88 %: alert only (once per 24 h)"
: > "$DLOG"; dw 88; expect '! grep -q "notify alert" "$DLOG"' "88 % again within 24 h: silent"
# alert.sh (C1)
printf "NOTIFY_WEBHOOK='https://ntfy.example/secret-topic'\n" > "$fs/.env"
: > "$DLOG"; STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "T" "body" high; expect 'grep -q "docker: exec -i librarian python -m notify alert T body high" "$DLOG" && ! grep -q "^curl:" "$DLOG" && ! grep -q "^logger:" "$DLOG"' "alert.sh hands off to the portal's notify CLI (delivered -> nothing else)"
: > "$DLOG"; NOTIFY_RC=3 STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "Disk full" "body text" high; rc=$?
expect '[ $rc = 0 ] && grep -q "logger: -t bookstack -p user.warning ALERT Disk full: body text" "$DLOG" && grep -qF "curl: -fsS -m 20 --retry 2 -X POST -H Title: Disk full --data-binary body text -H Priority: high https://ntfy.example/secret-topic" "$DLOG"' "portal says 'not delivered' (exit 3): journal + direct ntfy POST from the host with Title/Priority (C1)"
printf '#!/usr/bin/env bash\necho "docker: $*" >> "$DLOG"; exit 1\n' > "$bin/docker"; : > "$DLOG"; printf "X=1\n" > "$fs/.env"
STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "T" "body"; rc=$?; expect '[ $rc = 0 ] && grep -q "logger: -t bookstack -p user.warning ALERT T: body" "$DLOG" && ! grep -q "^curl:" "$DLOG"' "portal down, no webhook: journal only, never fails"
printf '#!/usr/bin/env bash\necho "docker: $*" >> "$DLOG"; exit 0\n' > "$bin/docker"
# cf-ips.sh (F08 / F31)
cfips(){ CF_IPS_ALERT="$T/alert-stub" STACK_DIR="$fs" bash "$REPO/scripts/cf-ips.sh" >"$T/cfips.out" 2>&1; }
printf '#!/usr/bin/env bash\necho "ALERT $*" >> "%s"\n' "$DLOG" > "$T/alert-stub"; chmod +x "$T/alert-stub"
rm -f "$CF_IPS_STATE"; : > "$DLOG"; cfips; rc=$?
expect '[ $rc = 0 ] && [ "$(grep -c . "$CF_IPS_STATE")" = 22 ] && grep -q "ufw: allow proto tcp from 173.245.0.0/22 to any port 443 comment cloudflare" "$DLOG" && grep -q "ufw: allow proto udp from 2400:cb00::/32 to any port 443" "$DLOG"' "good v4 + v6 lists: 22 ranges allowed and recorded"
: > "$DLOG"; CF_FAIL_V4=1 cfips; rc=$?
expect '[ $rc != 0 ] && ! grep -q "^ufw:" "$DLOG" && grep -q "^ALERT Cloudflare IP refresh failed" "$DLOG" && [ "$(grep -c . "$CF_IPS_STATE")" = 22 ]' "v4 fetch fails, v6 answers: ufw untouched (no v4 rule deleted), admin alerted (F08)"
: > "$DLOG"; CF_FAIL_V6=1 cfips; expect '[ $? != 0 ] && ! grep -q "^ufw:" "$DLOG"' "v6 fetch fails: ufw untouched"
cp "$CF_V4_FILE" "$T/v4.good"; printf '<html>error</html>\n' > "$CF_V4_FILE"; : > "$DLOG"; cfips
expect '[ $? != 0 ] && ! grep -q "^ufw:" "$DLOG" && grep -q "^ALERT" "$DLOG"' "garbage instead of CIDRs: rejected, ufw untouched"
head -3 "$T/v4.good" > "$CF_V4_FILE"; : > "$DLOG"; cfips
expect '[ $? != 0 ] && ! grep -q "^ufw:" "$DLOG"' "too few v4 ranges (3 < 10): rejected, ufw untouched"
grep -v "^173.245.4.0/22$" "$T/v4.good" > "$CF_V4_FILE"; : > "$DLOG"; cfips
expect '[ $? = 0 ] && [ "$(grep -c "ufw: --force delete" "$DLOG")" = 3 ] && grep -q "ufw: --force delete allow proto tcp from 173.245.4.0/22 to any port 80" "$DLOG" && [ "$(line_of_d() { grep -n "$1" "$DLOG" | head -1 | cut -d: -f1; }; line_of_d "ufw: allow")" -lt "$(grep -n "ufw: --force delete" "$DLOG" | head -1 | cut -d: -f1)" ]' "a range Cloudflare stopped publishing is removed (3 rules), after the current ones were (re)added"
cp "$T/v4.good" "$CF_V4_FILE"

echo "== ephemera enable/disable"
envset TAILSCALE_IP 100.64.0.1; envset CF_API_TOKEN ""
reset "yes" "https://archive.example" "" "" "alice"; step_ephemera
expect '[ "$(envget EPHEMERA_ENABLED)" = true ] && [ "$(envget EPHEMERA_OWNER)" = alice ] && [ -d "$STACK_DIR/library/dropbox/alice" ] && seen "compose -f docker-compose.yml -f docker-compose.ephemera.yml build ephemera"' "ephemera enabled, owner dropbox, pinned build"
expect 'grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && seen "caddy reload"' "enabling Ephemera renders its vhost and reloads Caddy (C6)"
reset; step_ephemera_off; expect '[ "$(envget EPHEMERA_ENABLED)" = false ] && seen "stop ephemera flaresolverr" && ! grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && seen "caddy reload"' "ephemera disabled; vhost removed and Caddy reloaded"
envset EPHEMERA_OWNER ""; envset ADMIN_USER famadmin; reset "yes" "https://archive.example" "" "" ""; step_ephemera >/dev/null
expect '[ "$(envget EPHEMERA_OWNER)" = famadmin ]' "Ephemera's default owner is the chosen admin account, not 'admin'"
step_ephemera_off >/dev/null

echo "== torrents (qBittorrent opt-in, C5)"
rm -rf "$STACK_DIR/qbt/config/qBittorrent"; envset TORRENTS_ENABLED false
reset "yes"; step_torrents; rc=$?; qc="$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"
expect '[ $rc = 0 ] && [ "$(envget TORRENTS_ENABLED)" = true ] && seen "ufw: allow 6881/tcp" && seen "docker: compose --profile torrents up -d qbittorrent" && grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile"' "enable: profile start, port 6881 opened, dl. vhost rendered"
expect 'grep -q "^\[BitTorrent\]" "$qc" && grep -qF "Session\\DefaultSavePath=/dropbox/$(envget ADMIN_USER)" "$qc" && grep -qF "Session\\TempPath=/downloads/incomplete" "$qc" && grep -qF "Session\\TempPathEnabled=true" "$qc"' "qBittorrent.conf seeded: default save path = the admin's dropbox, partials in /downloads/incomplete"
printf '[BitTorrent]\nSession\\DefaultSavePath=/downloads\nSession\\Port=6881\n\n[Preferences]\nWebUI\\Port=8080\n' > "$qc"; qbt_seed_config
expect '[ "$(grep -c "DefaultSavePath" "$qc")" = 1 ] && grep -qF "Session\\DefaultSavePath=/dropbox/" "$qc" && grep -qF "Session\\Port=6881" "$qc" && grep -qF "WebUI\\Port=8080" "$qc" && [ "$(grep -c "^\[BitTorrent\]" "$qc")" = 1 ]' "existing qBittorrent.conf: keys replaced in place, other settings kept"
expect 'grep -q "Save path: /dropbox/alice" "$LOG"' "admin told to point per-user categories at /dropbox/<user>"
reset "yes"; step_torrents
expect '[ "$(envget TORRENTS_ENABLED)" = false ] && seen "ufw: --force delete allow 6881/tcp" && seen "rm -f qbittorrent" && ! grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile"' "disable: container removed, 6881 closed, dl. vhost gone"
expect 'declare -f menu_library | grep -q step_torrents' "Torrents item in the Library menu"

echo "== intake webhook (C11)"
envset INTAKE_TOKEN ""; reset "yes"; step_intake_webhook
tok=$(envget INTAKE_TOKEN); expect '[ ${#tok} -ge 32 ] && seen "compose up -d librarian" && grep -F msgbox "$LOG" | grep -q "X-Intake-Token: $tok"' "intake webhook off until enabled here; enabling generates a token and restarts the portal"
reset "no"; step_intake_webhook; expect '[ -z "$(envget INTAKE_TOKEN)" ]' "and it can be turned off again"

echo; echo "TUI RESULT: $pass passed, $fails failed"; exit $fails
