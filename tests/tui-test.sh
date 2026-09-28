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
# A19: the inline python blocks run on the HOST. Nesting the same quote inside an f-string is
# python 3.12+ (PEP 701) and Debian 12 ships 3.11, where it is a SyntaxError.
if grep -nE 'f"[^"]*\{[^}]*"' "$REPO/bookstack.sh"; then bad "PEP 701 f-string (3.12-only) in bookstack.sh: Debian 12 python3.11 cannot parse it"
else ok "no python3.12-only f-string quoting in bookstack.sh's host-side python (A19)"; fi

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
    *"python -m abs remove-user"*) [ "${FAIL_ABS_REMOVE:-0}" = 1 ] && { echo '{"ok": false, "error": "401 Unauthorized"}'; return 1; }; echo '{"ok": true}';;
    *"python -m abs status"*) echo "{\"isInit\": ${ABS_INIT:-true}, \"app\": \"audiobookshelf\"}";;
    *"python -m abs "*) echo '{"ok": true}';;
    # admin_cli = the portal's admin back end (librarian/admin_cli.py). Its answers are variables
    # so a test can hand back a page, an empty page or a failure; CLI_FAIL makes every arm fail
    # the way the real one does (non-zero + one line of {"ok": false, "error": ...}).
    *"python -m admin_cli lockout status"*) [ "$CLI_FAIL" = 1 ] && { echo '{"ok": false, "error": "the portal database is locked"}'; return 1; }; echo "$CLI_LOCKOUT";;
    *"python -m admin_cli lockout clear"*)  [ "$CLI_FAIL" = 1 ] && { echo '{"ok": false, "error": "no such user"}'; return 1; }; echo "$CLI_CLEAR";;
    *"python -m admin_cli requests list"*)  [ "$CLI_FAIL" = 1 ] && { echo '{"ok": false, "error": "cannot read the queue"}'; return 1; }; echo "$CLI_REQUESTS";;
    *"python -m admin_cli parked list"*)    [ "$CLI_FAIL" = 1 ] && { echo '{"ok": false, "error": "the dropbox is unreadable"}'; return 1; }; echo "$CLI_PARKED";;
    *"python -m admin_cli"*)                [ "$CLI_FAIL$CLI_ACT_FAIL" = 00 ] || { echo '{"ok": false, "error": "that request has no re-fetchable source"}'; return 1; }; echo "$CLI_ACT";;
    *"ps --services"*) printf '%s\n' ${PS_SERVICES-caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma};;
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
# canned admin_cli answers; each test sets the ones it needs and resets CLI_FAIL
CLI_FAIL=0
CLI_LOCKOUT='{"ok": true, "users": [], "ips": []}'
CLI_CLEAR='{"ok": true, "cleared": 0}'
CLI_REQUESTS='{"ok": true, "total": 0, "rows": []}'
CLI_PARKED='{"ok": true, "rows": []}'
CLI_ACT='{"ok": true}'
CLI_ACT_FAIL=0
curl(){ echo "curl: $*" >> "$LOG"; case "$*" in *ipify*) echo "${IPIFY:-203.0.113.5}";;
  *127.0.0.1:8090/healthz*) [ "${FAIL_HEALTHZ:-0}" = 1 ] && return 22; return 0;;
  *"/healthz?aop="*) if [ -n "${AOP_SEQ:-}" ] && [ -s "$AOP_SEQ" ]; then head -1 "$AOP_SEQ"; tail -n +2 "$AOP_SEQ" > "$AOP_SEQ.n"; mv "$AOP_SEQ.n" "$AOP_SEQ"
     else printf '%s' "${AOP_PROBE:-200}"; fi;;   # L14: a request through Cloudflare (AOP_SEQ: one answer per call)
  *"/bookstack/config"*) printf '%s' "${REST_PROBE:-404}";;   # the home rest-server (404 = logged in, no repo yet)
  *authenticated_origin_pull_ca.pem*) local o=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && o="$2"; shift; done
     if [ -n "${CF_SHARED_PEM:-}" ]; then cp "$CF_SHARED_PEM" "$o"; else printf -- '-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n' > "$o"; fi;;
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
# A21: whiptail cannot return a newline, but a hand-edited or pasted .env value can carry one,
# and envset would then write a second, key-shaped line
reset; envset K_NL "$(printf 'one\ntwo')"; rc=$?
expect '[ $rc = 1 ] && [ -z "$(envget K_NL)" ] && ! grep -q "^K_NL=" "$ENV_FILE" && grep -F msgbox "$LOG" | grep -q "line break"' "envset refuses a value containing a line break instead of corrupting .env (A21)"
# A22: a .env written by a Windows editor (or restored from one) is CRLF
printf 'K_CR=value\r\n' >> "$ENV_FILE"; printf "K_CRQ='quoted'\r\n" >> "$ENV_FILE"
expect '[ "$(envget K_CR)" = value ] && [ "$(envget K_CRQ)" = quoted ]' "a CRLF .env yields clean values, quoted ones included (A22)"
# A11: a write that cannot happen must be reported, not silently swallowed
touch "$T/notadir"
( STACK_DIR="$T/notadir/sub"; ENV_FILE="$STACK_DIR/.env"; envset X y ) 2>/dev/null; rc=$?
expect '[ $rc != 0 ]' "envset returns non-zero when .env cannot be written (full disk / read-only root) (A11)"
envdefault K1 'ignored'; envdefault KNEW 'set'; expect '[ "$(envget K1)" = changed ] && [ "$(envget KNEW)" = set ]' "envdefault only fills blanks"
expect '[ "$(img IMG_CWA)" = crocodilestick/calibre-web-automated:v4.0.7 ]' "img() falls back to the pinned default"
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
echo "== entry-point guards and helper hygiene"
expect 'grep -A3 "command -v whiptail" "$REPO/bookstack.sh" | grep -q "Could not install whiptail"' "the whiptail bootstrap explains a busy dpkg lock instead of exiting on a raw apt error (A20)"
expect 'grep -q "flock -n 9" "$REPO/bookstack.sh" && grep -q "Another bookstack.sh is already running" "$REPO/bookstack.sh"' "a second concurrent TUI session is refused with flock, so two envsets cannot lose each other (A23)"
expect 'declare -f step_system | grep -q "iproute2 procps"' "System installs iproute2 (ip) and procps (free), which the TUI's own helpers call (A26)"
expect 'declare -f cf | grep -q -- "-m 30 --retry 2"' "Cloudflare API calls carry a max-time, so an outage cannot freeze the TUI indefinitely (A32)"
expect '! declare -f render_caddyfile | grep -q "PUBLIC_IP@@" && ! grep -q "@@PUBLIC_IP@@" "$REPO/caddy/Caddyfile.template"' "the dead @@PUBLIC_IP@@ substitution is gone (A28)"
expect 'declare -f write_sysctl | grep -q "temporarily absent" && ! declare -f write_sysctl | grep -q "Caddy binds the tailnet IP too"' "the ip_nonlocal_bind comment no longer claims Caddy binds the tailnet IP (A29)"
valid_admin_hash '$2a$14$STUBHASH/abc'; a=$?; valid_admin_hash '$2'; b=$?; valid_admin_hash ''; c=$?; valid_admin_hash '$argon2id$v=19$m=65536$salt$hash'; d=$?
expect '[ $a = 0 ] && [ $b = 1 ] && [ $c = 1 ] && [ $d = 0 ]' "the ADMIN_HASH guard matches the real hash shape, so a truncated '\$2' is refused (A24)"

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
expect '[ $rc = 0 ] && [ "$STACK_USER" = books ] && seen "useradd: -m -u 1000 -s /usr/sbin/nologin books" && grep -q "^books:x:1000:" "$PASSWD" && ! seen "usermod: -aG docker"' "fresh image -> 'books' created with uid 1000"
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
expect 'grep -F "msgbox" "$LOG" | grep -q "WERE saved"' "and it says the other settings were already written, instead of claiming a no-op (A30)"

echo "== Caddyfile templating: no sed metacharacters (A12/A13)"
dbak=$(envget DOMAIN)
envset DOMAIN 'ex&ample.com'; reset; render_caddyfile; rc=$?
expect '[ $rc = 0 ] && grep -q "^books.ex&ample.com {" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "@@" "$STACK_DIR/caddy/Caddyfile"' "an & in the domain is substituted literally, not as sed's whole-match (A13)"
envset DOMAIN 'mf|data.in'; reset; render_caddyfile; rc=$?
expect '[ $rc = 0 ] && [ -s "$STACK_DIR/caddy/Caddyfile" ] && grep -q "^books.mf|data.in {" "$STACK_DIR/caddy/Caddyfile"' "a | in the domain no longer produces a zero-byte Caddyfile reported as success (A12)"
envset DOMAIN 'ex\1ample.com'; reset; render_caddyfile; rc=$?
expect '[ $rc = 0 ] && grep -qF "books.ex\\1ample.com {" "$STACK_DIR/caddy/Caddyfile"' "a backslash reference in the domain is substituted literally (A12)"
envset DOMAIN "$dbak"; render_caddyfile; cp "$STACK_DIR/caddy/Caddyfile" "$T/cf.good2"
cp "$STACK_DIR/caddy/Caddyfile.template" "$T/tmpl.keep"; printf '\n# @@NEW_THING@@\n' >> "$STACK_DIR/caddy/Caddyfile.template"
reset; render_caddyfile; rc=$?
expect '[ $rc = 1 ] && cmp -s "$STACK_DIR/caddy/Caddyfile" "$T/cf.good2" && [ ! -e "$STACK_DIR/caddy/Caddyfile.new" ] && grep -F msgbox "$LOG" | grep -q "unsubstituted"' "a placeholder the renderer does not know refuses the render and leaves the live Caddyfile alone"
cp "$T/tmpl.keep" "$STACK_DIR/caddy/Caddyfile.template"; render_caddyfile
# A34: the auth. vhost is dropped while the gate is off, the way dl. and ephemera. are. This
# reads the SHIPPED template on purpose — an earlier version injected the markers into a copy,
# which passed happily while the real template carried no markers at all and nothing was dropped.
expect 'grep -q "^# @AUTHELIA_BEGIN@" "$STACK_DIR/caddy/Caddyfile.template" && grep -q "^# @AUTHELIA_END@" "$STACK_DIR/caddy/Caddyfile.template"' "the shipped template really wraps the auth. vhost in @AUTHELIA_BEGIN@/@AUTHELIA_END@ (A34)"
# F.2: CWA v4.0.6 proxies /api/v3/* and /api/UserStorage/* verbatim to
# https://readingservices.kobo.com - caller's method, headers and body - whenever annotation sync
# is off or the caller is anonymous, and books.<domain> is public. They belong in the same 403
# route that already covers convert-library / epub-fixer / cwa-logs / cwa-internal / reconnect.
# Asserted on the RENDERED Caddyfile: a pattern that only exists in the template protects nobody.
cwaj=$(grep -F '@cwa_admin_jobs path ' "$STACK_DIR/caddy/Caddyfile" | head -1)
cwajp=$(grep -F '@cwa_admin_jobs_params path_regexp' "$STACK_DIR/caddy/Caddyfile" | head -1)
expect '[ -n "$cwaj" ] && [ -n "$cwajp" ] && grep -q "respond @cwa_admin_jobs " "$STACK_DIR/caddy/Caddyfile" && grep -q "respond @cwa_admin_jobs_params " "$STACK_DIR/caddy/Caddyfile"' "the rendered Caddyfile still carries the CWA admin-jobs 403 route and its ;-parameter companion"
expect 'grep -q "respond @kobo_relay \"Not available\" 403" "$STACK_DIR/caddy/Caddyfile" && grep -q "path /api/v3 /api/v3/\* /api/UserStorage /api/UserStorage/\* /api/internal /api/internal/\*" "$STACK_DIR/caddy/Caddyfile" && grep -q "not path_regexp kobo_rs_stub" "$STACK_DIR/caddy/Caddyfile"' "the Kobo relay under /api is 403 at the edge, except CWA v4.0.7's four stub paths the Kobo sync needs (F.2)"
expect '[ -z "$(cwa_rs_block crocodilestick/calibre-web-automated:v4.0.7)" ] && [ -z "$(cwa_rs_block x/y:v4.1.0)" ] && [ -z "$(cwa_rs_block x/y:latest)" ] && [ "$(cwa_rs_block crocodilestick/calibre-web-automated:v4.0.6)" = " /api/v3 /api/v3/* /api/UserStorage /api/UserStorage/*" ]' \
  "the stub paths open only for CWA v4.0.7+: on v4.0.6 (a rollback) the same paths ARE the relay and stay fully blocked"
envset IMG_CWA crocodilestick/calibre-web-automated:v4.0.6; render_caddyfile
cwaj6=$(grep -F '@cwa_admin_jobs path ' "$STACK_DIR/caddy/Caddyfile" | head -1)
expect 'printf "%s\n" "$cwaj6" | grep -Eq "(^|[[:space:]])/api/v3/\*([[:space:]]|$)" && printf "%s\n" "$cwaj6" | grep -Eq "(^|[[:space:]])/api/UserStorage/\*([[:space:]]|$)"' "...rendered with CWA v4.0.6 pinned, the whole relay is in the 403 list again"
envset IMG_CWA ""; render_caddyfile
expect 'printf "%s\n" "$cwajp" | grep -q "api/v3" && printf "%s\n" "$cwajp" | grep -qi "api/UserStorage"' "the ;-smuggling path_regexp covers both relay prefixes too, so /api/v3/x;y is blocked as well (F.2)"
abak=$(envget AUTHELIA_ENABLED); envset AUTHELIA_ENABLED false; render_caddyfile
expect '! grep -q "^auth\.example\.test {" "$STACK_DIR/caddy/Caddyfile"' "auth. is not rendered while the gate is off (A34)"
envset AUTHELIA_ENABLED true; render_caddyfile
expect 'grep -q "^auth\.example\.test {" "$STACK_DIR/caddy/Caddyfile"' "and is rendered while it is on (A34)"
envset AUTHELIA_ENABLED "$abak"; render_caddyfile
hbak=$(envget ADMIN_HASH); envset ADMIN_HASH '$2'; reset; render_caddyfile; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "ADMIN_HASH is missing or invalid"' "a truncated ADMIN_HASH is refused instead of rendering a gate nobody can open (A24)"
envset ADMIN_HASH "$hbak"; render_caddyfile

echo "== Configure: input validation, rename marker, container recreation"
before=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
reset "not a domain!"; step_configure; rc=$?
after=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
expect '[ $rc = 1 ] && [ "$before" = "$after" ] && grep -F msgbox "$LOG" | grep -q "is not a hostname"' "a domain that is not a hostname is refused before anything is written (A12)"
reset "example.test" "not-an-email"; step_configure; rc=$?
after=$(md5 -q "$ENV_FILE" 2>/dev/null || md5sum "$ENV_FILE" | cut -d' ' -f1)
expect '[ $rc = 1 ] && [ "$before" = "$after" ] && grep -F msgbox "$LOG" | grep -q "is not an e-mail address"' "the Let's Encrypt contact address is validated too (A27)"
# A10: ADMIN_USER_PREV is the only record of the name the CWA row still has
envset ADMIN_USER libadmin; envset ADMIN_USER_PREV ""
reset "example.test" "admin@example.test" "UTC" "" "yes" "first-admin"; step_configure >/dev/null
expect '[ "$(envget ADMIN_USER_PREV)" = libadmin ] && [ "$(envget ADMIN_USER)" = first-admin ]' "a rename records the previous admin name"
reset "example.test" "admin@example.test" "UTC" "" "yes" "second-admin"; step_configure >/dev/null
expect '[ "$(envget ADMIN_USER_PREV)" = libadmin ] && [ "$(envget ADMIN_USER)" = second-admin ]' "a SECOND rename does not clobber it, so the real old name is not lost (A10)"
envset ADMIN_USER libadmin; envset ADMIN_USER_PREV ""
# R4-A6: `lib rename-user` moves the Calibre-Web row and the portal's own owner-keyed rows and
# nothing else, but four stores OUTSIDE both databases key on the name. The rename is not asked
# for — it happens as a side effect of changing ADMIN_USER in Configure — so leaving them behind
# is silent: the admin's uploads land in a dropbox the watcher no longer scans, an already
# configured qBittorrent keeps saving into it, Authelia still knows them under the old name (they
# pass the gate as one person and the apps as another) and Ephemera's bind mount is stale.
envset TORRENTS_ENABLED true; envset EPHEMERA_OWNER oldadmin; envset ADMIN_USER newadmin
mkdir -p "$STACK_DIR/library/dropbox/oldadmin"; : > "$STACK_DIR/library/dropbox/oldadmin/book.epub"
AUY="$STACK_DIR/authelia/users_database.yml"; cp "$AUY" "$T/auy.bak"
cat > "$AUY" <<'YML'
users:
  oldadmin:
    displayname: "oldadmin"
    password: "$argon2id$v=19$KEEPTHISHASH"
    email: "old@example.test"
    groups:
      - users
  bob:
    displayname: "Bob"
    password: "$argon2id$v=19$BOBHASH"
    email: "bob@example.test"
    groups:
      - users
YML
reset; rename_user_artifacts oldadmin newadmin
expect '[ -f "$STACK_DIR/library/dropbox/newadmin/book.epub" ] && [ ! -d "$STACK_DIR/library/dropbox/oldadmin" ]' "a rename carries the dropbox and its contents over, so the admin's own uploads are still scanned (R4-A6)"
expect 'grep -q "^  newadmin:" "$AUY" && ! grep -q "^  oldadmin:" "$AUY" && grep -q "KEEPTHISHASH" "$AUY" && grep -q "old@example.test" "$AUY"' "the Authelia login is renamed in place, keeping the argon2 hash and e-mail that cannot be recomputed (R4-A6)"
expect 'grep -q "^  bob:" "$AUY" && grep -q "BOBHASH" "$AUY" && grep -q "displayname: \"Bob\"" "$AUY"' "and no other login is touched (R4-A6)"
expect 'grep -q "displayname: \"newadmin\"" "$AUY"' "a display name that WAS the username follows the rename (R4-A6)"
expect '[ "$(envget EPHEMERA_OWNER)" = newadmin ]' "EPHEMERA_OWNER follows the rename, so its bind mount points at the dropbox that exists (R4-A6)"
expect 'grep -qF "DefaultSavePath=/dropbox/newadmin" "$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"' "an already-configured qBittorrent is reseeded with the new save path (R4-A6)"
# it must not merge two people: a name that already has its own login is left alone
reset; rename_user_artifacts bob newadmin
expect 'grep -q "^  bob:" "$AUY" && grep -q "BOBHASH" "$AUY" && grep -q "KEEPTHISHASH" "$AUY"' "renaming onto a name that already has an Authelia login is refused instead of merging the two (R4-A6)"
cp "$T/auy.bak" "$AUY"; rm -rf "$STACK_DIR/library/dropbox/newadmin"
envset TORRENTS_ENABLED false; envset EPHEMERA_OWNER ""; envset ADMIN_USER libadmin
expect 'declare -f ensure_admin_name | grep -q rename_user_artifacts && [ "$(declare -f ensure_admin_name | grep -c "rename-user")" -ge 1 ]' "ensure_admin_name runs it right after the rename it triggers automatically (R4-A6)"
# A06: .env only reaches a container when it is recreated; a reload of Caddy is not enough
envset TZ UTC; reset "example.test" "admin@example.test" "Europe/Oslo" "" "yes"; step_configure >/dev/null
expect 'seen "docker: compose up -d" && grep -F msgbox "$LOG" | grep -q "containers were recreated"' "a changed timezone/domain/token recreates the containers, not just reloads Caddy (A06)"
reset "example.test" "admin@example.test" "Europe/Oslo" "" "yes"; step_configure >/dev/null
expect '! grep -qE "^docker: compose( --profile torrents)? up -d$" "$LOG" && ! grep -F msgbox "$LOG" | grep -q "containers were recreated"' "re-running Configure with the same answers recreates nothing"
# A08: jail.local bakes in the Cloudflare token, the abs-login filter the domain
fail2ban-client(){ :; }
reset "example.test" "admin@example.test" "Europe/Oslo" "cf-token-ROTATED" "yes"; step_configure >/dev/null
expect 'seen "systemctl: restart fail2ban" && grep -q "cf-token-ROTATED" "$T/etc/fail2ban/jail.local"' "rotating the Cloudflare token re-renders fail2ban, so its bans do not silently stop working (A08)"
unset -f fail2ban-client
envset CF_API_TOKEN cf-token-123; envset TZ UTC
reset "example.test" "admin@example.test" "UTC" "cf-token-123" "yes"; step_configure >/dev/null

echo "== Authelia gate + users"
inject_authelia_gate >/dev/null; expect '[ "$(grep -cE "^\s*forward_auth " "$STACK_DIR/caddy/Caddyfile")" = 4 ] && [ "$(grep -cE "^\s*forward_auth @authelia_protected " "$STACK_DIR/caddy/Caddyfile")" = 3 ] && grep -qE "^\s*forward_auth 127.0.0.1:9091" "$STACK_DIR/caddy/Caddyfile"' "gate injected into 4 vhosts (shelf without a bypass matcher)"
expect 'grep -qF "not path_regexp ^(?:/api/UserStorage/|/api/internal/notebooks(/|$)|/api/v3/content/|/kobo/|/kosync(/|$)|/opds(/|$))" "$STACK_DIR/caddy/Caddyfile" && grep -qF "not path_regexp ^(?:/intake$)" "$STACK_DIR/caddy/Caddyfile" && ! grep -q "@@BYPASS@@" "$STACK_DIR/caddy/Caddyfile"' "Kobo/OPDS/KOReader and intake bypasses present as anchored, case-sensitive regexps"
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
expect '! grep -F "docker: " "$LOG" | grep -q "up -d authelia"' "with no user Authelia is never even started (4.39 refuses an empty user file: 'users: non zero value required')"
echo "users: {}" > "$f"; FAIL_AUTHELIA_HEALTH=1; reset "yes" "adminpw-123" "adminpw-123" "boss@mail.example" "<cancel>" "<cancel>"; step_authelia; rc=$?; FAIL_AUTHELIA_HEALTH=0
expect '[ $rc = 1 ] && [ "$(envget AUTHELIA_ENABLED)" = false ] && grep -F msgbox "$LOG" | grep -q "did not become healthy" && ! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "Authelia unhealthy -> gate NOT enabled"
expect '[ "$(line_of "authelia crypto hash")" -lt "$(line_of "up -d authelia")" ]' "the first user is written BEFORE Authelia starts (it would refuse an empty user file)"
echo "users: {}" > "$f"
reset "yes" "adminpw-123" "adminpw-123" "boss@mail.example" "<cancel>" "<cancel>"; step_authelia; rc=$?
expect '[ $rc = 0 ] && [ "$(envget AUTHELIA_ENABLED)" = true ] && grep -q forward_auth "$STACK_DIR/caddy/Caddyfile" && grep -q "^  admin:" "$f" && [ "$(line_of "authelia crypto hash")" -lt "$(line_of "caddy reload")" ] && grep -F msgbox "$LOG" | grep -q "notification.txt"' "one user created -> gate injected and Caddy reloaded afterwards; no SMTP -> admin told where codes go"
# A17: the portal reads AUTHELIA_ENABLED at start-up; that flag is what stops /admin offering
# "Add a user", which would create an account with no Authelia login and no way in anywhere
expect 'seen "compose up -d librarian" && [ "$(line_of "caddy reload")" -lt "$(line_of "compose up -d librarian")" ]' "enabling Authelia restarts the portal, so its add-user guard rail is actually live (A17)"
# L05: one login behind the gate
expect '[[ "$(envget GATE_SECRET)" =~ ^[0-9a-f]{64}$ ]] && grep -q "request_header -X-Bookstack-Gate" "$STACK_DIR/caddy/Caddyfile" && grep -q "request_header @authelia_protected X-Bookstack-Gate {env.BOOKSTACK_GATE_SECRET}" "$STACK_DIR/caddy/Caddyfile"' "a gate secret is generated; Caddy strips any client copy and vouches only for requests Authelia let through (L05)"
expect 'grep -A12 "reverse_proxy 127.0.0.1:8090\|request\.example\.test" "$STACK_DIR/caddy/Caddyfile" >/dev/null && ! grep -q "BOOKSTACK_GATE_SECRET=[0-9a-f]" "$STACK_DIR/caddy/Caddyfile" && grep -q "BOOKSTACK_GATE_SECRET=\${GATE_SECRET:-}" "$REPO/docker-compose.yml" && grep -q "GATE_SECRET=\${GATE_SECRET:-}" "$REPO/docker-compose.yml"' "the secret is a runtime placeholder, never written into the Caddyfile; compose hands it to Caddy and the portal"
expect 'seen "exec librarian python -m cwa proxy-login on" && seen "compose restart calibre-web" && seen "systemctl: enable --now bookstack-gate-sync.path bookstack-gate-sync.timer"' "Calibre-Web's header login is switched on and the password sync units are installed (L05)"
gu="$T/etc/systemd/system/bookstack-gate-sync"
expect 'grep -qF "PathModified=$STACK_DIR/librarian/state/gate-sync.flag" "$gu.path" && grep -qF "ExecStart=/usr/bin/python3 $STACK_DIR/scripts/gate-sync.py" "$gu.service" && grep -q "^OnCalendar=\*:0/10" "$gu.timer" && grep -q "^OnFailure=bookstack-alert@gate-sync.service" "$gu.service"' "gate-sync runs the moment the portal queues a password (path unit), with a 10-minute safety net"
expect 'grep -q "watch: true" "$REPO/authelia/configuration.yml.template"' "Authelia re-reads its user file itself (no restart that would sign everyone out)"
ac="$STACK_DIR/authelia/configuration.yml"
expect '[ "$(envget SHELFMARK_AUTH_METHOD)" = proxy ] && seen "compose up -d shelfmark librarian" && grep -q "AUTH_METHOD=\${SHELFMARK_AUTH_METHOD:-cwa}" "$REPO/docker-compose.yml" && grep -q "PROXY_AUTH_ADMIN_GROUP_NAME=admins" "$REPO/docker-compose.yml"' "Shelfmark switches to the header login behind the gate; admin rights only from the admins group (L05)"
expect '[[ "$(envget ABS_OIDC_SECRET)" =~ ^[0-9a-f]{64}$ ]] && [[ "$(envget AUTHELIA_OIDC_HMAC)" =~ ^[0-9a-f]{64}$ ]] && [ -s "$STACK_DIR/authelia/oidc-jwks.pem" ] && [ "$(stat -c %a "$STACK_DIR/authelia/oidc-jwks.pem" 2>/dev/null || stat -f %Lp "$STACK_DIR/authelia/oidc-jwks.pem")" = 600 ]' "Audiobookshelf's OpenID client secret, Authelia's HMAC secret and an RSA signing key (0600) are generated"
expect 'grep -q "client_id: .audiobookshelf." "$ac" && grep -qE "client_secret: .\\\$pbkdf2-sha512\\\$" "$ac" && ! grep -qF "$(envget ABS_OIDC_SECRET)" "$ac" && ! grep -qF "$(envget AUTHELIA_OIDC_HMAC)" "$ac" && grep -q "https://audio.example.test/auth/openid/callback" "$ac" && grep -q "consent_mode: .implicit." "$ac"' "the rendered OpenID block: only the HASH of the client secret, no HMAC secret, the audio. callback, no consent click"
expect 'grep -q "BOOKSTACK_OIDC_HMAC=\${AUTHELIA_OIDC_HMAC:-}" "$REPO/docker-compose.authelia.yml" && grep -qx "authelia/oidc-jwks.pem" "$REPO/.gitignore"' "the HMAC secret reaches Authelia through its environment; the signing key never enters git"
step_authelia_off >/dev/null; expect '[ "$(envget AUTHELIA_ENABLED)" = false ] && ! grep -q forward_auth "$STACK_DIR/caddy/Caddyfile"' "Authelia disable removes the gate"
expect 'seen "exec librarian python -m cwa proxy-login off" && [ ! -f "$gu.path" ] && seen "systemctl: disable --now bookstack-gate-sync.path bookstack-gate-sync.timer"' "and turns Calibre-Web's header login and the password sync off again (L05)"
expect '[ "$(envget SHELFMARK_AUTH_METHOD)" = cwa ]' "and Shelfmark goes back to its Calibre-Web login"
# the admins group follows Calibre-Web's admins
cat > "$T/adm-users.yml" <<'YML'
users:
  admin:
    displayname: "admin"
    password: "$argon2id$x"
    email: "a@x.test"
    groups:
      - users
  alice:
    displayname: "Alice"
    password: "$argon2id$y"
    email: "al@x.test"
    groups:
      - users
      - admins
YML
cp "$T/adm-users.yml" "$STACK_DIR/authelia/users_database.yml"; authelia_sync_admin_groups
expect 'python3 - "$STACK_DIR/authelia/users_database.yml" <<"PYC"
import sys, re
s = open(sys.argv[1]).read()
blk = lambda u: re.search(r"  " + u + r":\n((?:    .*\n?)*)", s).group(1)
ok = "      - admins" in blk("admin") and "      - admins" not in blk("alice") and "      - users" in blk("alice") and "$argon2id$y" in blk("alice")
sys.exit(0 if ok else 1)
PYC' "Authelia's admins group mirrors Calibre-Web: admin gains it, a reader who had it loses it, nothing else changes"
reset; step_authelia_off >/dev/null; expect 'seen "compose up -d librarian"' "disabling it restarts the portal too, so /admin stops linking to a stopped Authelia (A17)"
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
# A15: the dropbox watcher skips folders starting with '.', so such an account can never import
reset ".kim"; step_user_add; rc=$?
expect '[ $rc = 1 ] && ! seen "add-user .kim" && [ ! -d "$STACK_DIR/library/dropbox/.kim" ] && grep -F msgbox "$LOG" | grep -q "never have its dropbox scanned"' "a username starting with a dot is refused before anything is created (A15)"
reset ".."; step_user_add; rc=$?
expect '[ $rc = 1 ] && ! seen "add-user .."' "'..' — whose dropbox would resolve to library/ itself — is refused too (A15)"
reset "Alice"; step_user_add; rc=$?
expect '[ $rc = 1 ] && ! seen "add-user Alice"' "an uppercase name is refused with an explanation instead of a raw backend error (A15)"
reset "1" ".hidden" "5"; step_intake; rc=$?
expect '[ ! -d "$STACK_DIR/library/dropbox/.hidden" ] && grep -F msgbox "$LOG" | grep -q "never scanned"' "Intake -> create a dropbox applies the same rule (A15)"
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
# V02: a plain restart does NOT end Shelfmark sessions — it re-reads the same signing key from
# config/.flask_secret, which compose bind-mounts. Dropping that file first is the whole fix.
expect 'seen "docker: compose restart shelfmark" && [ ! -e "$STACK_DIR/shelfmark/config/.flask_secret" ]' "a password reset drops Shelfmark's persisted session key and restarts it (V02)"
expect 'grep -F msgbox "$LOG" | grep -q "NEW session key"' "and the message describes what actually ends the session, not just 'restarted' (V02)"
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
# A25: only rows the worker has CLAIMED are failed; a row still waiting for approval is not
expect 'grep -F "yesno: " "$LOG" | grep -q "waiting for approval stay in the queue"' "the removal prompt no longer says every queued request is failed (A25)"
reset "alice" "no"; step_user_remove; expect '! seen "cwa remove-user"' "remove user aborted on No"
expect '! seen "compose restart shelfmark"' "an aborted removal does not restart anything"
# J07: the removed user's signed Shelfmark cookie keeps working until the container restarts
reset "alice" "yes"; step_user_remove
expect 'seen "docker: compose restart shelfmark" && [ "$(line_of "cwa remove-user alice")" -lt "$(line_of "compose restart shelfmark")" ] && [ ! -e "$STACK_DIR/shelfmark/config/.flask_secret" ]' "removing a user drops Shelfmark's session key and restarts it, so their cookie stops verifying (V02)"
expect 'grep -F msgbox "$LOG" | grep -q "NEW session key" && grep -F msgbox "$LOG" | grep -q "any session .* still had open there is dead" && grep -F msgbox "$LOG" | grep -q "until that session expires"' "the removal message names the new session key and the app sessions that may linger (V02/J07)"
# and it must NOT claim the session is dead when the restart failed
mkdir -p "$STACK_DIR/shelfmark/config"; : > "$STACK_DIR/shelfmark/config/.flask_secret"
eval "real_compose() $(declare -f compose | sed '1d')"
compose(){ case "$*" in *"restart shelfmark"*) echo "docker: compose $*" >> "$LOG"; return 1;; esac; real_compose "$@"; }
reset "bob" "yes"; step_user_remove
expect 'grep -F msgbox "$LOG" | grep -q "still holds its old session key"' "a failed Shelfmark restart is reported instead of claiming the session is dead (V02)"
unset -f compose; eval "compose() $(declare -f real_compose | sed '1d')"; unset -f real_compose
# R4-A1: the confirmation promises Audiobookshelf, so the summary has to say what happened there.
# `abs_ready && absctl remove-user ... || true` swallowed both a missing API key and an API error,
# and selftest.sh cannot catch a leftover account either (it is still tag-restricted, so the
# assertion passes) — the admin was told the login was gone while the audiobook app still let
# the removed reader in.
reset "alice" "yes"; step_user_remove
expect 'grep -F msgbox "$LOG" | grep -q "Audiobookshelf account was removed too"' "a successful ABS removal is reported alongside the Authelia and Shelfmark halves (R4-A1)"
FAIL_ABS_REMOVE=1; reset "alice" "yes"; step_user_remove; FAIL_ABS_REMOVE=0
expect 'grep -F msgbox "$LOG" | grep -q "Audiobookshelf account could NOT be removed" && grep -F msgbox "$LOG" | grep -q "can still sign in to the audiobook app"' "a failing absctl remove-user makes the removal summary name Audiobookshelf instead of reporting success (R4-A1)"
abs_tok_keep=$(envget ABS_TOKEN); envset ABS_TOKEN ""
reset "alice" "yes"; step_user_remove
expect '! seen "python -m abs remove-user alice" && grep -F msgbox "$LOG" | grep -q "no audiobook account was touched"' "with no ABS API key the removal says so and points at Settings -> Users, rather than staying silent (R4-A1)"
envset ABS_TOKEN "$abs_tok_keep"
reset; step_user_repair; expect 'seen "cwa isolate bob" && seen "cwa isolate alice" && ! seen "cwa isolate admin" && seen "cwa harden"' "repair re-isolates every non-admin (never admins) + hardens"

echo "== formats, mail, defaults"
reset "yes" "no" "pdf,azw3" "new_record" "yes" "no"; step_formats && ok "step_formats" || bad "step_formats"
expect "seen \"UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='epub', kindle_epub_fixer=0, auto_convert_retained_formats='pdf,azw3', auto_ingest_automerge='new_record';\"" "formats written to CWA settings; target always epub"
expect '! grep -q "Target format" "$LOG" && ! grep -qE "azw3 \"AZW3|mobi \"MOBI|kepub \"KEPUB" "$LOG"' "no target-format picker: azw3/mobi/pdf/kepub are not offered as the conversion target (C12)"
# F.1: CWA v4.0.6 autodetects kepubify only at /opt/kepubify/kepubify-linux-{64,32}bit while its
# image installs it at /usr/bin/kepubify, so config_kepubifypath is never set and cps/kobo.py
# never converts. Kobo receives EPUB. Neither the screen nor the comment may claim otherwise.
expect '! grep -qi "Kobo gets KEPUB\|KEPUB converted on the fly\|Kobo gets KEPUB automatically" "$REPO/bookstack.sh"' "no claim anywhere in bookstack.sh that Kobo receives KEPUB (F.1)"
expect 'grep -q "config_kepubifypath" "$REPO/bookstack.sh" && grep -q "chapter boundaries" "$REPO/bookstack.sh" && grep -q "DECISIONS-PENDING" "$REPO/bookstack.sh"' "step_formats records WHY Kobo gets EPUB, what it costs, and where the safe enablement order is written down (F.1)"
expect 'grep -F msgbox "$LOG" | grep -q "Kobo syncs EPUB" && ! grep -F msgbox "$LOG" | grep -qi "Kobo gets KEPUB"' "and the Formats screen tells the admin EPUB, not KEPUB (F.1)"
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
# R4-A2: apply_library_defaults is where the isolation invariants are established, so its comment
# is what an auditor reads instead of the SQL. It used to say the Kindle EPUB fixer is turned ON
# while the very next statement sets kindle_epub_fixer=0 — the one setting the repo warns hardest
# about, because on import it rewrites every archive and drops the zip comment the CBZ tag lives in.
# bash drops comments from a function body, so this reads the source, not `declare -f`
ald=$(awk '/^apply_library_defaults\(\) \{/,/^\}/' "$REPO/bookstack.sh")
expect 'printf "%s" "$ald" | grep -qi "kindle epub fixer OFF" && ! printf "%s" "$ald" | grep -qi "fix EPUBs for Kindle"' "apply_library_defaults' comment agrees with its own SQL: the Kindle EPUB fixer is OFF (R4-A2)"
expect 'printf "%s" "$ald" | grep -q "kindle_epub_fixer=0"' "and the SQL it describes is still the one that turns it off (R4-A2)"

echo "== the isolation guide describes what the code actually does"
# R4-A3/A5: this screen is the admin's reference for the isolation model. It claimed every
# non-EPUB file is converted to EPUB with the tag added first. Neither half held: PDF/CBZ/CBR/CB7
# are in auto_convert_ignored_formats and are tagged in their own format, and worker._TAGGERS is
# {epub, pdf, cbz} only — MOBI/AZW3/FB2/TXT import untagged and land on the portal's needs-tag
# list. An admin who believes the old text never looks at that list, and those books stay
# invisible to their owner for good.
reset; step_isolation
iso=$(grep -F "whiptail: " "$LOG" | grep -c "msgbox")
expect '[ "$iso" -ge 1 ]' "step_isolation draws its guide"
expect '! declare -f step_isolation | grep -q "Non-EPUB files are converted to EPUB on import"' "the blanket 'every non-EPUB file is converted, tag added first' claim is gone (R4-A3/A5)"
expect 'declare -f step_isolation | grep -q "MOBI, AZW3, FB2 and TXT" && declare -f step_isolation | grep -qi "untagged" && declare -f step_isolation | grep -q "Imported without an owner tag"' "it names the formats that cannot carry a tag and the /admin list they wait on, by the name the page uses (R4-A5)"
expect 'declare -f step_isolation | grep -q "CBZ/CBR/CB7" && declare -f step_isolation | grep -qi "keep their own format"' "and says PDFs and comics keep their format rather than being converted (R4-A3)"
# the formats named as tag-carrying are exactly worker._TAGGERS, and the exempt list is exactly
# apply_library_defaults' auto_convert_ignored_formats: a drift in either makes the guide wrong again
expect 'python3 -c "import re,sys; m=re.search(r\"^_TAGGERS = \{(.*)\}\", open(sys.argv[1]).read(), re.M); sys.exit(0 if m and sorted(re.findall(chr(34)+r\"(\w+)\"+chr(34), m.group(1)))==[\"cbz\",\"epub\",\"pdf\"] else 1)" "$REPO/librarian/worker.py"' "worker._TAGGERS still carries exactly epub, pdf and cbz, which is what the guide now claims (R4-A5)"
expect 'declare -f apply_library_defaults | grep -q "auto_convert_ignored_formats='"'"'pdf,cbz,cbr,cb7'"'"'"' "the conversion-exempt list is still pdf,cbz,cbr,cb7, which is what the guide now claims (R4-A3)"

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
    # L14 zone-level origin pulls: an upload gets a fresh id; its status is CF_AOP_STATUS
    "POST "*/origin_tls_client_auth) [ "${CF_AOP_RC:-0}" = 0 ] || return 22
       n=$(( $(cat "$CFSTORE/aop_n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CFSTORE/aop_n"
       printf '%s' "$data" > "$CFSTORE/aop_upload"; printf '{"result":{"id":"aop-%s","status":"pending_deployment"}}\n' "$n";;
    "PUT "*/origin_tls_client_auth/settings) printf '%s' "$data" | command jq -r .enabled > "$CFSTORE/aop_enabled"; echo '{"success":true}';;
    "GET "*/origin_tls_client_auth/settings) printf '{"result":{"enabled":%s}}\n' "$(cat "$CFSTORE/aop_enabled" 2>/dev/null || echo false)";;
    "GET "*/origin_tls_client_auth/*) printf '{"result":{"status":"%s"}}\n' "${CF_AOP_STATUS:-active}";;
    "DELETE "*/origin_tls_client_auth/*) echo '{"success":true}';;
    "PATCH "*/settings/*) [ "${path##*/}" = "${CF_FAIL_SETTING:-}" ] && return 22
       printf '%s' "$data" | command jq -r .value > "$CFSTORE/set_${path##*/}"; echo '{"success":true}';;
    "GET "*/settings/*) printf '{"result":{"value":"%s"}}\n' "$(cat "$CFSTORE/set_${path##*/}" 2>/dev/null)";;
    "GET "*"/dns_records?type=A&name="*) n="${path##*name=}"
       if [ -f "$CFSTORE/dns_$n" ]; then printf '{"result":[%s]}\n' "$(command jq -c '. + {id:"rec-STUB"}' "$CFSTORE/dns_$n")"; else echo '{"result":[]}'; fi;;
    "PUT "*/dns_records/*|"POST "*/dns_records) n=$(printf '%s' "$data" | command jq -r .name); printf '%s' "$data" > "$CFSTORE/dns_$n"; echo '{"success":true}';;
    # IP Access Rules: what the fail2ban cloudflare-token action creates, and what a local
    # unban does NOT remove. "$CFSTORE/ban" stands in for one existing block rule.
    "GET "*/firewall/access_rules/rules?*) [ -f "$CFSTORE/ban" ] && echo '{"result":[{"id":"rule-STUB"}]}' || echo '{"result":[]}';;
    "DELETE "*/firewall/access_rules/rules/*) [ "${CF_UNBAN_RC:-0}" = 0 ] || return 22; rm -f "$CFSTORE/ban"; echo '{"success":true}';;
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
lf="$T/etc/fail2ban/filter.d/caddy-auth.conf"
expect '[ -f "$lf" ] && ! grep -q "@@" "$lf" && grep -q "(?:request|shelf|auth)\\\\.example\\\\.test" "$lf"' "caddy-auth is rendered with the regex-escaped domain too, so it can be host-scoped (V/contract 3)"
python3 - "$af" "$lf" <<'PY' && ok "caddy-abs-login matches only POST /login 401 on audio.<domain>, keyed on client_ip (J25)" || bad "caddy-abs-login filter regex"
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
python3 - "$lf" "$REPO/configs/fail2ban/caddy-device-auth.conf" <<'PY' && ok "caddy-auth counts login POSTs only; caddy-device-auth counts /opds + /kosync 401s, both by client_ip" || bad "fail2ban filter regexes"
import re, sys
def rx(path):
    conf = open(path).read()
    return re.compile(re.search(r"failregex = (.*)", conf).group(1).replace("<HOST>", r"(?P<host>\S+?)"))
login, dev = rx(sys.argv[1]), rx(sys.argv[2])
line = lambda ip, cip, m, uri, st, host="request.example.test": '{"request":{"remote_ip":"%s","remote_port":"1","client_ip":"%s","proto":"HTTP/2.0","method":"%s","host":"%s","uri":"%s","headers":{}},"status":%d}' % (ip, cip, m, host, uri, st)
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 401)).group("host") == "203.0.113.9"      # the visitor, not Cloudflare
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/auth/login", 401))                          # Shelfmark
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/firstfactor", 401))                          # Authelia
assert not login.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 401))                                  # reader app challenge: other jail, looser
assert not login.search(line("172.71.1.1", "203.0.113.9", "GET", "/login", 200))
assert not login.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 302))                                # success
assert not login.search(line("172.71.1.1", "203.0.113.9", "POST", "/request", 401))
# host-scoped: Audiobookshelf's own /login belongs to the looser caddy-abs-login jail, or an ABS
# app with a stale password trips the 2 h ban that locks the household out of all four sites
assert not login.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 401, "audio.example.test"))
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/firstfactor", 401, "auth.example.test"))
assert login.search(line("172.71.1.1", "203.0.113.9", "POST", "/api/auth/login", 401, "shelf.example.test"))
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 401)).group("host") == "203.0.113.9"
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds/new?page=2", 401))
assert dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/kosync/users/auth", 401))
assert not dev.search(line("172.71.1.1", "203.0.113.9", "GET", "/opds", 200))
assert not dev.search(line("172.71.1.1", "203.0.113.9", "POST", "/login", 401))
PY

echo "== cloudflare step"
envset TAILSCALE_IP 100.64.0.1; envset CF_API_TOKEN cf-token-123
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"; chmod +x "$STACK_DIR/scripts/cf-ips.sh"
rm -f "$CFSTORE"/*; reset "no"; step_cloudflare && ok "step_cloudflare ran" || bad "step_cloudflare failed"
expect 'seen "cf: PATCH /zones/zone-STUB/settings/browser_check --data {\"value\":\"off\"}" && seen "cf: PATCH /zones/zone-STUB/settings/email_obfuscation --data {\"value\":\"off\"}" && seen "settings/rocket_loader --data {\"value\":\"off\"}"' "Browser Integrity Check, e-mail obfuscation and Rocket Loader set OFF"
expect 'seen "cf: PUT /zones/zone-STUB/rulesets/phases/http_request_cache_settings/entrypoint" && grep -F "http_request_cache_settings/entrypoint --data" "$LOG" | grep -q "\"cache\":false" && grep -F "cache_settings/entrypoint --data" "$LOG" | grep -q "books.example.test"' "no-cache Cache Rule for the four public hosts"
expect '! seen "Bot Fight Mode: ON" && grep -F "msgbox" "$LOG" | grep -q "Do NOT enable Bot Fight Mode"' "Bot Fight Mode advice: must stay OFF"
expect '[ -f "$T/etc/cron.d/bookstack-cfips" ] && grep -q "^PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin$" "$T/etc/cron.d/bookstack-cfips" && grep -q "cf-ips.sh" "$T/etc/cron.d/bookstack-cfips"' "cf-ips cron installed with a full PATH (ufw is in /usr/sbin, F31)"
expect 'grep -q "STACK_DIR=$STACK_DIR $STACK_DIR/scripts/cf-ips.sh" "$T/etc/cron.d/bookstack-cfips"' "the cf-ips cron line carries STACK_DIR, so a failed nightly refresh can still find the alert channel (V09)"
# A34: auth. is published only while Authelia runs; otherwise it is a public hostname that 502s
expect '! grep -q "name\":\"auth.example.test" "$LOG"' "auth. is NOT published while Authelia is off (A34)"
envset AUTHELIA_ENABLED true; rm -f "$CFSTORE"/*; reset "no"; step_cloudflare >/dev/null
expect 'grep -q "name\":\"auth.example.test" "$LOG"' "with Authelia on it IS published (A34)"
envset AUTHELIA_ENABLED false
# A33: 127.0.0.1 is the placeholder Configure writes before Tailscale exists
tsbak3=$(envget TAILSCALE_IP); envset TAILSCALE_IP 127.0.0.1; rm -f "$CFSTORE"/*; reset "no"; step_cloudflare; rc=$?
expect '[ $rc = 0 ] && ! grep -q "name\":\"monitor.example.test" "$LOG" && grep -F msgbox "$LOG" | grep -q "127.0.0.1 placeholder"' "no tailnet IP yet: monitor./dl./ephemera. are NOT published and the summary says so (A33)"
envset TAILSCALE_IP "$tsbak3"; rm -f "$CFSTORE"/*; reset "no"; step_cloudflare >/dev/null
expect 'seen "cf: POST /zones/zone-STUB/dns_records --data {\"type\":\"A\",\"name\":\"monitor.example.test\",\"content\":\"100.64.0.1\",\"ttl\":1,\"proxied\":false}" && ! grep -q "name\":\"dl.example.test" "$LOG" && ! grep -q "name\":\"aria.example.test" "$LOG" && seen "dns_records?type=A&name=aria.example.test"' "tailnet-only hosts grey-clouded; no dl. while torrents are off; stale aria. looked up for deletion"
expect 'seen "cf: GET /zones/zone-STUB/settings/ssl" && seen "cf: GET /zones/zone-STUB/settings/tls_client_auth" && grep -F msgbox "$LOG" | grep -q "read back and verified"' "SSL mode and origin pulls are read back before success is claimed (F13)"
rm -f "$CFSTORE"/*; CF_FAIL_SETTING=tls_client_auth; reset; step_cloudflare; rc=$?; CF_FAIL_SETTING=""
expect '[ $rc = 1 ] && grep -F "NOT fully configured" "$LOG" | grep -q "tls_client_auth" && ! grep -F msgbox "$LOG" | grep -q "Cloudflare configured"' "a failed zone setting is listed and the step fails instead of claiming success (F13)"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STACK_DIR/scripts/cf-ips.sh"; rm -f "$CFSTORE"/*; reset "no"; step_cloudflare; rc=$?
expect '[ $rc = 1 ] && grep -F "NOT fully configured" "$LOG" | grep -q "firewall allowlist"' "a failed firewall allowlist makes the step fail"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"; rm -f "$CFSTORE"/*; reset "no"; step_cloudflare >/dev/null
expect '[ "$(aop_mode)" = shared ] && cmp -s "$STACK_DIR/caddy/cf-origin-pull-ca.pem" "$STACK_DIR/caddy/cf-shared-ca.pem" && grep -F "yesno: " "$LOG" | grep -q "Lock the origin to THIS zone only" && grep -qF "SHARED client certificate" "$LOG"' "by default Caddy trusts Cloudflare's shared CA, the step offers the per-zone lock and the summary says which one is in force (L14)"

echo "== L14: this zone's own origin-pull certificate"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STACK_DIR/scripts/cf-ips.sh"; chmod +x "$STACK_DIR/scripts/cf-ips.sh"
openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=cf-shared -days 30 -keyout "$T/shk.pem" -out "$T/shared.pem" >/dev/null 2>&1
export CF_SHARED_PEM="$T/shared.pem"
rm -f "$CFSTORE"/*; envset AOP_MODE ""; envset AOP_CERT_ID ""; reset "yes"; step_cloudflare; rc=$?
ad="$T/etc/bookstack/aop"; tr="$STACK_DIR/caddy/cf-origin-pull-ca.pem"
expect '[ $rc = 0 ] && [ "$(envget AOP_MODE)" = zone ] && [ "$(envget AOP_CERT_ID)" = aop-1 ] && cmp -s "$tr" "$ad/ca.pem" && [ "$(cat "$CFSTORE/aop_enabled")" = true ]' "set up: uploaded, enabled at Cloudflare, proven, and Caddy now trusts ONLY this zone's CA"
expect 'openssl verify -CAfile "$ad/ca.pem" "$ad/client.pem" >/dev/null 2>&1 && openssl x509 -in "$ad/client.pem" -noout -text | grep -q "CA:FALSE" && openssl x509 -in "$ad/client.pem" -noout -text | grep -q "TLS Web Client Authentication" && openssl x509 -in "$ad/client.pem" -noout -text | grep -qE "(Public-Key|Public Key): \(4096 bit\)"' "the leaf is what Cloudflare requires: RSA 4096, CA:FALSE, clientAuth, signed by our CA"
expect '[ "$(stat -c %a "$ad/client.key" 2>/dev/null || stat -f %Lp "$ad/client.key")" = 600 ] && [ "$(stat -c %a "$ad/ca.key" 2>/dev/null || stat -f %Lp "$ad/ca.key")" = 600 ] && [ "$(stat -c %a "$ad" 2>/dev/null || stat -f %Lp "$ad")" = 700 ] && ! ls "$STACK_DIR/caddy" | grep -q "\.key"' "private keys stay in /etc/bookstack/aop (0700/0600); only the CA certificate reaches caddy/"
expect 'command jq -e ".certificate | startswith(\"-----BEGIN CERTIFICATE\")" "$CFSTORE/aop_upload" >/dev/null && command jq -e ".private_key | test(\"PRIVATE KEY\")" "$CFSTORE/aop_upload" >/dev/null && ! command jq -r .certificate "$CFSTORE/aop_upload" | grep -q "$(sed -n 2p "$ad/ca.pem")"' "the upload carries the LEAF and its key, not the CA (Cloudflare: 'missing leaf certificate')"
expect 'seen "docker: compose" && grep -F "docker: " "$LOG" | grep -q "restart caddy" && seen "curl: -s -o /dev/null -m 15 -w %{http_code} https://request.example.test/healthz?aop=" && grep -qF "Origin locked to THIS zone" "$LOG"' "the switch is proven with a real request through Cloudflare after a Caddy RESTART (no reused connection)"
# the site does not answer through Cloudflare yet (Caddy still getting certificates): no switch at all
envset AOP_MODE both; aop_write_trust both; cp "$tr" "$T/tr.pre"; AOP_PROBE=526 reset; AOP_PROBE=526 step_origin_lock; rc=$?
expect '[ $rc = 1 ] && cmp -s "$tr" "$T/tr.pre" && ! grep -F "docker: " "$LOG" | grep -q "restart caddy" && grep -qF "does not answer through Cloudflare yet" "$LOG"' "no switch while the site itself is not answering yet: trust unchanged, Caddy not restarted, the admin told why"
# the probe fails: back to trusting both, nothing broken
envset AOP_MODE both; printf '200\n526\n526\n526\n526\n526\n526\n' > "$T/aopseq"; AOP_SEQ="$T/aopseq" reset; AOP_SEQ="$T/aopseq" step_origin_lock; rc=$?
expect '[ $rc = 1 ] && [ "$(envget AOP_MODE)" = both ] && [ "$(grep -c "BEGIN CERTIFICATE" "$tr")" = 2 ] && grep -qF "put back to trusting both" "$LOG"' "Cloudflare still presents the shared certificate: Caddy goes back to trusting both, and says so"
reset; step_origin_lock; expect '[ "$(envget AOP_MODE)" = zone ] && cmp -s "$tr" "$ad/ca.pem"' "a later run finishes the switch"
# renewal: a new leaf from the same CA; the old upload is deleted only after the proof
cp "$ad/ca.pem" "$T/ca.before"; cp "$ad/client.pem" "$T/leaf.before"
reset "yes"; step_origin_lock
expect '[ "$(envget AOP_CERT_ID)" = aop-2 ] && [ -z "$(envget AOP_OLD_CERT_ID)" ] && seen "cf: DELETE /zones/zone-STUB/origin_tls_client_auth/aop-1" && cmp -s "$ad/ca.pem" "$T/ca.before" && ! cmp -s "$ad/client.pem" "$T/leaf.before" && [ "$(envget AOP_MODE)" = zone ]' "renewal: new leaf from the SAME CA, uploaded, proven, then the old upload deleted"
# the token lacks SSL and Certificates: nothing changes
cp "$tr" "$T/tr.before"; cp "$ad/client.pem" "$T/leaf2"; CF_AOP_RC=1 reset "yes"; CF_AOP_RC=1 step_origin_lock; rc=$?
expect '[ $rc = 1 ] && grep -qF "SSL and Certificates" "$LOG" && [ ! -e "$ad/client.pem.new" ] && cmp -s "$tr" "$T/tr.before" && cmp -s "$ad/client.pem" "$T/leaf2" && [ "$(envget AOP_MODE)" = zone ] && [ "$(envget AOP_CERT_ID)" = aop-2 ]' "an upload Cloudflare refuses (token without SSL and Certificates) changes nothing, and says which permission is missing"
# not deployed yet at Cloudflare
envset AOP_MODE ""; envset AOP_CERT_ID ""; aop_write_trust shared; CF_AOP_STATUS=pending_deployment reset; CF_AOP_STATUS=pending_deployment step_origin_cert; rc=$?
expect '[ $rc = 1 ] && [ "$(aop_mode)" = shared ] && cmp -s "$tr" "$STACK_DIR/caddy/cf-shared-ca.pem" && grep -qF "has not deployed it yet" "$LOG"' "a certificate Cloudflare has not rolled out yet leaves Caddy on the shared CA"
expect 'declare -f step_deploy | grep -q "aop_tighten" && declare -f menu_security | grep -q step_origin_lock' "Deploy finishes a pending switch; Security -> Origin lock exists"
# re-running the Cloudflare step keeps a finished per-zone lock
envset AOP_MODE zone; aop_write_trust zone; rm -f "$CFSTORE"/*; reset; step_cloudflare >/dev/null
expect 'cmp -s "$tr" "$ad/ca.pem" && ! grep -F "yesno: " "$LOG" | grep -q "Lock the origin"' "re-running Install -> Cloudflare keeps the per-zone trust (it does not fall back to the shared CA)"
envset AOP_MODE ""; aop_write_trust shared; unset CF_SHARED_PEM

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
# F.6: the 04:30 reboot happens whether or not restic was ever configured, so the unit that
# checks the stack came back cannot depend on the admin having run Install -> Backups.
expect '[ -s "$T/etc/systemd/system/bookstack-postboot.service" ] && [ -x "$T/etc/bookstack/postboot.sh" ] && seen "systemctl: enable bookstack-postboot.service"' "Deploy installs the post-reboot self-test too, not only the Backups step (F.6)"
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
# A31: the summary is the screen the admin copies credentials from
envset ADMIN_PW_SET false; envset ABS_TOKEN ""; envset ABS_ROOT_USER ""
ABS_INIT=false; reset "adminpass-1234" "adminpass-1234" "abshero" "rootpass-1234" "rootpass-1234"; step_deploy >/dev/null; ABS_INIT=true
expect 'grep -q "root user .abshero." "$LOG" && ! grep -q "root user .root." "$LOG"' "the Stack-is-up summary names the ABS root user chosen during THIS Deploy, not 'root' (A31)"
# A11: ADMIN_PW_SET is the loop's only exit; a silent write failure prompted forever
envset ADMIN_PW_SET false
eval "real_envset() $(declare -f envset | sed '1d')"
envset(){ [ "$1" = ADMIN_PW_SET ] && return 1; real_envset "$@"; }
reset "looppass-1234" "looppass-1234"; set_admin_password; rc=$?
unset -f envset; eval "envset() $(declare -f real_envset | sed '1d')"; unset -f real_envset
expect '[ $rc = 1 ] && [ "$(grep -c "askpw: Set the password" "$LOG")" = 1 ] && grep -F msgbox "$LOG" | grep -q "Could not write"' "an unwritable .env ends set_admin_password with a message instead of prompting forever (A11)"
envset ADMIN_PW_SET false; envset ABS_TOKEN ""
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
  # `restic backup --help` is how both bookstack.sh and scripts/backup.sh probe for --retry-lock
  # (restic 0.16+). RESTIC_NO_RETRY_LOCK=1 plays the Debian 12 restic 0.14 that lacks it.
  backup) [ "${2:-}" = --help ] && { [ "${RESTIC_NO_RETRY_LOCK:-0}" = 1 ] || echo "      --retry-lock duration   retry to lock the repository"; exit 0; };;
  # RESTIC_BAD_PW plays a password that does not open the repository, so the rotation can be
  # tested at the exact point where the CANDIDATE credential must be rejected.
  cat) [ "${RESTIC_NOREPO:-0}" = 1 ] && exit 1
       [ -n "${RESTIC_BAD_PW:-}" ] && [ "${RESTIC_PASSWORD:-}" = "$RESTIC_BAD_PW" ] && exit 1;;
  key) case "${2:-}" in
         list) echo '[{"id":"0a1b2c3doldkey","userName":"root","current":true},{"id":"9f8e7d6cnewkey","userName":"root","current":false}]';;
         add) echo "restic-key-add-pw: $(cat "${4:-/dev/null}" 2>/dev/null)" >> "$RLOG"; [ "${RESTIC_KEYADD_RC:-0}" = 0 ] || exit 1;;
         remove) echo "restic-key-remove-pw: ${RESTIC_PASSWORD:-}" >> "$RLOG"; [ "${RESTIC_KEYRM_RC:-0}" = 0 ] || exit 1;;
       esac;;
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
RX='restic: (--retry-lock 30m )?'   # restic_run probes for --retry-lock, so it is present or not
reset "O" "/mnt/backup" "resticpass-123" "resticpass-123" "no" "https://hc-ping.example/uuid" "no"; step_backup && ok "step_backup" || bad "step_backup failed"
renv="$T/etc/bookstack/restic.env"; u="$T/etc/systemd/system"
expect 'grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv" && grep -q "^RESTIC_PASSWORD=resticpass-123$" "$renv" && [ "$(stat -c %a "$renv" 2>/dev/null || stat -f %Lp "$renv")" = 600 ]' "restic.env written 0600 with repo + password"
expect 'grep -qE "${RX}cat config" "$RLOG" && ! grep -qE "${RX}init" "$RLOG" && [ "$(envget BACKUP_PING_URL)" = https://hc-ping.example/uuid ]' "existing repository opened (not re-initialised); optional ping URL stored (C1)"
expect 'grep -q -- "--retry-lock 30m cat config" "$RLOG" && [ ! -e "$renv.new" ]' "restic_run probes restic for --retry-lock and passes it when the binary has it; no candidate file left behind"
: > "$RLOG"; RESTIC_NO_RETRY_LOCK=1 restic_run cat config
expect 'grep -qx "restic: cat config" "$RLOG" && ! grep -q -- "--retry-lock" "$RLOG"' "restic 0.14 (Debian 12) has no --retry-lock: it is NOT passed, so the call does not die on 'unknown flag'"
expect 'grep -q "^OnCalendar=\*-\*-\* 01:00:00" "$u/bookstack-backup.timer" && grep -q "^OnFailure=bookstack-alert@backup.service" "$u/bookstack-backup.service"' "backup at 01:00 with OnFailure alert"
expect 'grep -q "^OnCalendar=\*-\*-01 13:00:00" "$u/bookstack-restore-test.timer" && grep -q "restore-test.sh" "$u/bookstack-restore-test.service" && grep -q "^OnFailure=bookstack-alert@restore-test.service" "$u/bookstack-restore-test.service"' "restore test on the 1st at 13:00 (never overlaps the 01:00 backup, F40) with OnFailure alert"
expect 'grep -q "scripts/alert.sh" "$u/bookstack-alert@.service" && grep -q "%i" "$u/bookstack-alert@.service"' "templated bookstack-alert@.service"
expect 'seen "systemctl: enable --now bookstack-backup.timer bookstack-restore-test.timer" && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server" && grep -F msgbox "$LOG" | grep -q "NO alert channel"' "timers enabled; offsite-secrets checklist shown; missing alert channel called out"
# F.6: step_system sets unattended-upgrades Automatic-Reboot at 04:30 - the one scheduled event
# that restarts every container while nobody is watching - and selftest.sh otherwise runs only
# from the TUI and at the end of an Update, so a stack that never came back stayed silent.
pbu="$u/bookstack-postboot.service"; pbs="$T/etc/bookstack/postboot.sh"
expect '[ -s "$pbu" ] && [ -x "$pbs" ]' "Backups installs the post-reboot self-test unit and its wrapper beside the backup units (F.6)"
expect 'grep -q "^After=docker.service" "$pbu" && ! grep -q "^Requires=" "$pbu" && grep -q "^Type=oneshot" "$pbu" && grep -q "^WantedBy=multi-user.target" "$pbu"' "ordered After docker.service but NOT Requires: a unit that is skipped when Docker dies sends no alert (F.6)"
expect 'grep -qF "ExecStart=$pbs" "$pbu" && grep -qE "^TimeoutStartSec=(1800|infinity)$" "$pbu"' "the unit runs the wrapper and gets more than systemd's 90 s default to finish the wait plus the self-test (F.6)"
expect 'seen "systemctl: enable bookstack-postboot.service" && ! seen "systemctl: enable --now bookstack-postboot.service"' "enabled for the NEXT boot, not started now (F.6)"
expect 'bash -n "$pbs" && [ "$(head -1 "$pbs")" = "#!/usr/bin/env bash" ]' "the generated wrapper is valid bash"
expect 'grep -q "scripts/selftest.sh" "$pbs" && grep -q "scripts/alert.sh" "$pbs" && grep -q "starting" "$pbs" && grep -qF "STACK_DIR=$STACK_DIR" "$pbs"' "the wrapper waits out the healthcheck start periods, runs selftest.sh and alerts through alert.sh, with STACK_DIR baked in (F.6)"
cp "$pbu" "$T/pbu.1"; cp "$pbs" "$T/pbs.1"; install_backup_units
expect 'cmp -s "$pbu" "$T/pbu.1" && cmp -s "$pbs" "$T/pbs.1" && [ "$(grep -c "env bash" "$pbs")" = 1 ] && [ "$(ls "$u" | grep -c postboot)" = 1 ]' "a second install_backup_units rewrites the unit and wrapper in place instead of duplicating either (F.6)"
# A01: a password that does not open the repository must NOT replace /etc/bookstack/restic.env
cp "$renv" "$T/renv.good"; : > "$RLOG"; export RESTIC_NOREPO=1
reset "O" "/mnt/backup" "typo-password-9" "typo-password-9" "no"; step_backup; rc=$?; export RESTIC_NOREPO=0
expect '[ $rc = 1 ] && cmp -s "$renv" "$T/renv.good" && [ ! -e "$renv.new" ] && ! grep -qE "${RX}init" "$RLOG" && grep -F msgbox "$LOG" | grep -q "still opens your existing backups"' "a wrong/rotated restic password leaves the existing key file untouched and refuses to init over it (A01)"
expect 'grep -F "yesno: " "$LOG" | grep -q "key add"' "and the prompt says a restic password cannot be changed by typing a new one (it needs key add)"
: > "$RLOG"; export RESTIC_NOREPO=1; reset "O" "/mnt/backup" "resticpass-123" "resticpass-123" "yes" "no" "" "no"; step_backup; export RESTIC_NOREPO=0
expect 'grep -qE "${RX}init" "$RLOG" && grep -q "^RESTIC_PASSWORD=resticpass-123$" "$renv" && [ ! -e "$renv.new" ]' "a brand-new repository is initialised by the Backups step (only there, F67) and the key file is moved into place afterwards"
reset "S" "s3:s3.example/bucket" "resticpass-123" "resticpass-123" "<cancel>"; step_backup; expect '[ $? = 1 ] && grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv"' "Cancel at the S3 key prompt aborts without touching restic.env"
echo "== L15: an append-only nightly key; retention with a separate key"
expect '! grep -q "^RESTIC_APPEND_ONLY" "$renv" && [ ! -f "$u/bookstack-prune.timer" ]' "answering No keeps the old behaviour: no flag, no prune timer"
# append-only, prune key kept on the laptop
: > "$RLOG"; reset "O" "/mnt/backup" "resticpass-123" "resticpass-123" "yes" "no" "" "no"; step_backup
expect 'grep -q "^RESTIC_APPEND_ONLY=1$" "$renv" && [ ! -f "$u/bookstack-prune.timer" ] && [ ! -f "$T/etc/bookstack/restic-prune.env" ] && grep -qF "RESTIC_PRUNE_ENV=./prune.env bash prune.sh" "$LOG"' "append-only + laptop: flag stored, nothing on the server can prune, the laptop recipe is shown"
expect 'grep -F "yesno: " "$LOG" | grep -q "without deleteFiles\|WITHOUT deleteFiles" && grep -F "yesno: " "$LOG" | grep -q "listBuckets,listFiles,readFiles,writeFiles"' "the question says exactly which B2 key capabilities make a key append-only"
# append-only, prune key on the server (rest-server style: a different user in the address)
: > "$RLOG"; reset "O" "/mnt/backup" "resticpass-123" "resticpass-123" "yes" "yes" "rest:https://prune:pw@host/repo" "" "no"; step_backup
pf="$T/etc/bookstack/restic-prune.env"
expect 'grep -q "^RESTIC_REPOSITORY=rest:https://prune:pw@host/repo$" "$pf" && grep -q "^RESTIC_PASSWORD=resticpass-123$" "$pf" && [ "$(stat -c %a "$pf" 2>/dev/null || stat -f %Lp "$pf")" = 600 ] && [ ! -e "$pf.new" ]' "the prune key file: its own address, the same repository password, 0600"
expect 'grep -q "^OnCalendar=\*-\*-15 03:00:00" "$u/bookstack-prune.timer" && grep -qF "Environment=RESTIC_PRUNE_ENV=$pf" "$u/bookstack-prune.service" && grep -q "scripts/prune.sh" "$u/bookstack-prune.service" && grep -q "^OnFailure=bookstack-alert@prune.service" "$u/bookstack-prune.service" && seen "systemctl: enable --now bookstack-prune.timer"' "monthly prune timer on the 15th (never the 1st's restore test or the 01:00 backup), alerting on failure"
# an empty prune key is not stored
rm -f "$pf"; : > "$RLOG"
reset "S" "s3:s3.example/bucket" "resticpass-123" "resticpass-123" "AKID" "secret" "yes" "yes" "<blank>" "x" "" "no"; step_backup
expect '[ ! -f "$pf" ] && grep -F msgbox "$LOG" | grep -q "No prune key entered"' "an empty prune key is refused, not stored"
# back to a key that may delete: the prune timer and key go away
reset "O" "/mnt/backup" "resticpass-123" "resticpass-123" "no" "" "no"; step_backup
expect '! grep -q "^RESTIC_APPEND_ONLY" "$renv" && [ ! -f "$u/bookstack-prune.timer" ] && seen "systemctl: disable --now bookstack-prune.timer"' "switching back to a deleting key removes the monthly prune timer"
echo "== backups to a computer at home (rest-server over Tailscale, free, append-only)"
: > "$RLOG"; reset "H" "100.101.102.103" "8000" "resticpass-123" "resticpass-123" "" "no"; step_backup; rc=$?
expect '[ $rc = 0 ] && grep -qE "^RESTIC_REPOSITORY=rest:http://bookstack:[A-Za-z0-9]{32}@100\.101\.102\.103:8000/bookstack/$" "$renv" && grep -q "^RESTIC_APPEND_ONLY=1$" "$renv" && grep -q "^RESTIC_PRUNE_WHERE=home$" "$renv"' "the home target: a generated login in the repository address, append-only, pruned at home"
expect 'grep -qF "bookstack:\$2a\$14\$STUBHASH/abc" "$LOG" && grep -qF -- "--append-only --private-repos" "$LOG" && grep -qF "restic/rest-server:0.14.0" "$LOG" && grep -qF "\"dst\": [\"tag:backup:8000\"]" "$LOG" && grep -qF "forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune" "$LOG"' "the setup screen gives the .htpasswd line (bcrypt), the append-only docker command, the one Tailscale rule and the monthly prune for that computer"
expect 'seen "curl: -s -o /dev/null -m 10 -w %{http_code} -u bookstack:" && ! grep -F "yesno: " "$LOG" | grep -q "Is this key APPEND-ONLY" && [ ! -f "$u/bookstack-prune.timer" ] && grep -F "docker-stdin: " "$LOG" | grep -q .' "it checks the login against the home server before storing anything; no append-only question (it IS append-only); the password reaches Caddy's hasher on stdin"
hr1=$(grep "^RESTIC_REPOSITORY=" "$renv")
reset "H" "" "" "yes" "resticpass-123" "resticpass-123" "" "no"; step_backup
expect '[ "$(grep "^RESTIC_REPOSITORY=" "$renv")" = "$hr1" ] && ! grep -qF "Set up the backup server at home" "$LOG"' "running Backups again keeps the home server's login (no new .htpasswd line needed)"
reset "H" "192.168.1.5"; step_backup; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "not a Tailscale address" && [ "$(grep "^RESTIC_REPOSITORY=" "$renv")" = "$hr1" ]' "a LAN address is refused (the server reaches home only over Tailscale); nothing changed"
REST_PROBE=401 reset "H" "100.101.102.103" "8000" "no" "no"; REST_PROBE=401 step_backup; rc=$?
expect '[ $rc = 1 ] && grep -F "yesno: " "$LOG" | grep -q "refused the login (HTTP 401)" && [ "$(grep "^RESTIC_REPOSITORY=" "$renv")" = "$hr1" ]' "a home server that refuses the login is reported (HTTP 401) and nothing is stored"
REST_PROBE=000 reset "H" "100.101.102.103" "8000" "yes" "no"; REST_PROBE=000 step_backup; rc=$?
expect '[ $rc = 1 ] && grep -F "yesno: " "$LOG" | grep -q "Could not reach the backup server"' "an unreachable home server says what to check (on, Tailscale, container, rule)"
# C1: alerts step
printf '#!/usr/bin/env bash\necho "alert.sh $*" >> "%s"\n' "$LOG" > "$STACK_DIR/scripts/alert.sh"; chmod +x "$STACK_DIR/scripts/alert.sh"
reset "https://ntfy.sh/family-secret-topic" "yes"; step_alerts; rc=$?
expect '[ $rc = 0 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ] && seen "compose up -d librarian" && seen "alert.sh Bookstack test alert" && seen "yesno: A test alert was sent"' "Alerts: webhook stored, portal restarted, test alert sent through alert.sh, admin confirms (C1)"
reset "" "no"; step_alerts; rc=$?; expect '[ $rc = 1 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ]' "blank keeps the webhook; unconfirmed delivery returns 1"
reset "ftp://nope"; step_alerts; expect '[ $? = 1 ] && [ "$(envget NOTIFY_WEBHOOK)" = https://ntfy.sh/family-secret-topic ]' "non-http URL refused"
expect 'declare -f menu_install | grep -q step_alerts && declare -f menu_ops | grep -q step_alerts' "Alerts in the Install and Operations menus"
# A14: Lock SSH needs the same tailnet-address guard Quick install applies, or it closes port 22
# pointing at the 127.0.0.1 placeholder and only the provider's serial console gets back in.
tsbak2=$(envget TAILSCALE_IP); envset TAILSCALE_IP 127.0.0.1; IP_ADDRS="10.0.0.5"
reset "yes" "yes"; step_lock_ssh; rc=$?
expect '[ $rc = 1 ] && ! seen "ufw: --force delete" && [ "$(envget SSH_LOCKED)" != true ] && grep -F msgbox "$LOG" | grep -q "placeholder Configure writes"' "TAILSCALE_IP is still the 127.0.0.1 placeholder: SSH stays public, nothing asked (A14)"
envset TAILSCALE_IP 100.64.7.7; IP_ADDRS="10.0.0.5"
reset "yes" "yes"; step_lock_ssh; rc=$?
expect '[ $rc = 1 ] && ! seen "ufw: --force delete" && [ "$(envget SSH_LOCKED)" != true ] && grep -F msgbox "$LOG" | grep -q "not an address on this host"' "a stale tailnet address (Tailscale re-auth) also refuses to close port 22 (A14)"
envset TAILSCALE_IP "$tsbak2"; IP_ADDRS="$(envget TAILSCALE_IP)"
TS_EXPIRY='"2027-03-01T00:00:00Z"'; reset "yes" "no"; step_lock_ssh; rc=$?; expect '[ $rc = 1 ] && ! seen "ufw: --force delete" && grep -F "msgbox" "$LOG" | grep -q "Disable key expiry"' "key expiry set + 'not done' -> SSH stays public, told what to do"
envset SSH_LOCKED false
reset "yes" "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp" && [ "$(envget SSH_LOCKED)" = true ]' "confirmed -> port 22 closed and SSH_LOCKED recorded (F12)"; TS_EXPIRY=null
reset "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp" && ! grep -q "yesno: IMPORTANT" "$LOG"' "KeyExpiry null -> no extra prompt"
envset SSH_LOCKED false; IP_ADDRS=""
reset; step_tailscale >/dev/null; expect 'grep -F "msgbox" "$LOG" | grep -q "Disable key expiry" && grep -q "advertise-tags=tag:bookstack" "$LOG" && [ "$(envget TAILSCALE_IP)" = 100.64.0.1 ]' "Tailscale step: key expiry warning + tag:bookstack ACL advice; IP stored"
expect 'seen "systemctl: enable --now tailscaled" && [ "$(line_of "systemctl: enable --now tailscaled")" -lt "$(line_of "tailscale: up")" ]' "the step starts tailscaled before logging in (a Debian 13 minimal image leaves it stopped)"
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
printf 'finished=2020-01-01T00:00:00+00:00 exit=9\n' > "$STACK_DIR/.postboot-selftest.log"   # the OLD server's result, inside the snapshot
: > "$RLOG"; reset "abc12345" "full" "yes" "yes"; step_restore && ok "step_restore ran" || bad "step_restore failed"
expect '[ ! -e "$STACK_DIR/.postboot-selftest.log" ] && [ -z "$(postboot_banner)" ]' "a restore drops the snapshot's post-reboot result: no stale 'checks FAILED' banner on a machine that has not rebooted (F.6)"
expect 'grep -qE "${RX}snapshots --json" "$RLOG" && grep -F "whiptail: " "$LOG" | grep -q "Restore: pick a snapshot" && grep -qE "${RX}stats abc12345 --mode restore-size --json" "$RLOG"' "snapshot picker (restic snapshots --json) and a restore-size check before anything stops (F01/F16)"
expect 'grep -qxE "${RX}restore abc12345 --target / --include $STACK_DIR" "$RLOG" && seen "docker: compose down" && [ -f "$STACK_DIR/library/books/big.epub" ] && ! ls -d "$(dirname "$STACK_DIR")"/.bs-restore.* >/dev/null 2>&1' "restores the picked snapshot IN PLACE (target /), no temporary copy of the library (F01)"
expect '[ "$(envget ABS_TOKEN)" = from-snapshot ] && [ "$(envget PUBLIC_IP)" = 203.0.113.5 ] && [ "$(envget TAILSCALE_IP)" = 100.64.0.1 ] && [ "$(envget TZ)" = UTC ]' "snapshot .env restored, but this server's PUBLIC_IP / TAILSCALE_IP / TZ kept"
expect '[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from user\").fetchone()[0])" "$STACK_DIR/cwa/config/app.db")" = 1 ] && [ ! -f "$STACK_DIR/cwa/config/app.db-wal" ]' "consistent DB copy replaced the raw file per MANIFEST; stale -wal removed"
expect 'seen "docker: compose up -d" && [ "$(line_of "compose down")" -lt "$(line_of "compose up -d")" ] && [ -f "$T/etc/cron.d/bookstack-disk" ] && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server"' "stack restarted, watchdog re-installed, checklist shown"
expect 'seen "systemctl: restart fail2ban" && [ "$(line_of "compose up -d")" -lt "$(line_of "systemctl: restart fail2ban")" ] && [ -f "$STACK_DIR/caddy/data/access.log" ]' "fail2ban restarted after the stack is up, with its log file present (F15)"
: > "$RLOG"; reset "abc12345" "config" "yes" "yes"; step_restore >/dev/null
expect 'grep -E "${RX}restore abc12345 --target /" "$RLOG" | grep -q -- "--include $STACK_DIR/.env --include $STACK_DIR/.backup-snap" && ! grep -qE -- "--include $STACK_DIR( |$)" "$RLOG" && ! grep -qE "${RX}stats" "$RLOG"' "'config + databases only' restores .env, DB copies and app configs, not the library (F16)"
: > "$RLOG"; export RESTIC_STATS_SIZE=999999999999999; reset "abc12345" "full"; step_restore; rc=$?; unset RESTIC_STATS_SIZE
expect '[ $rc = 1 ] && ! grep -q "restore abc12345" "$RLOG" && ! seen "compose down" && grep -F msgbox "$LOG" | grep -q "Not enough disk space"' "snapshot larger than the free space: refused before the stack is stopped (F01)"
reset "abc12345" "full" "yes" "no"; : > "$RLOG"
step_restore; rc=$?
expect '[ "$rc" != 0 ] && ! grep -q "restore abc12345" "$RLOG" && ! seen "compose down"' "second confirmation declined -> nothing restored, stack not stopped"
# A16: "config + databases only" promises the library files stay — metadata.db is their CATALOG
printf 'library_books_metadata.db\tlibrary/books/metadata.db\n' >> "$T/fakesnap$STACK_DIR/.backup-snap/MANIFEST"
printf 'SNAPSHOT-CATALOG' > "$T/fakesnap$STACK_DIR/.backup-snap/library_books_metadata.db"
mkdir -p "$STACK_DIR/library/books"; printf 'LIVE-CATALOG' > "$STACK_DIR/library/books/metadata.db"
: > "$RLOG"; reset "abc12345" "config" "yes" "yes"; step_restore >/dev/null
expect '[ "$(cat "$STACK_DIR/library/books/metadata.db")" = LIVE-CATALOG ]' "'config + databases only' keeps the live Calibre catalog, so books imported since the snapshot do not vanish (A16)"
: > "$RLOG"; reset "abc12345" "full" "yes" "yes"; step_restore >/dev/null
expect '[ "$(cat "$STACK_DIR/library/books/metadata.db")" = SNAPSHOT-CATALOG ]' "a FULL restore does bring the catalog back with the library"
# The restore's silent gap: backup.sh stages sshd/sysctl/daemon.json under .backup-snap/host/
# and, before restore_host_files existed, step_restore read NONE of them — the box came back
# serving books with its SSH and kernel hardening missing while every check reported success.
hostdir="$T/fakesnap$STACK_DIR/.backup-snap/host"
mkdir -p "$hostdir/etc/ssh/sshd_config.d" "$hostdir/etc/sysctl.d" "$hostdir/etc/docker" "$hostdir/etc/fail2ban"
printf 'PasswordAuthentication no\n' > "$hostdir/etc/ssh/sshd_config.d/01-bookstack.conf"
printf 'net.ipv4.ip_nonlocal_bind=1\n'  > "$hostdir/etc/sysctl.d/90-bookstack.conf"
printf '{"ip":"127.0.0.1"}\n'           > "$hostdir/etc/docker/daemon.json"
printf 'OLD-JAIL\n'                     > "$hostdir/etc/fail2ban/jail.local"
HOST_RESTORED=""; HOST_MANUAL=""
FAKEROOT="$T/hostroot"; mkdir -p "$FAKEROOT"
# stubs so the test never touches the real machine. The harness has its OWN systemctl/sshd
# stubs that later assertions depend on, so save them and put them back rather than unsetting.
_saved_stubs=$(declare -f systemctl sshd sysctl install cmp 2>/dev/null || true)
install(){ local a=(); for x in "$@"; do case "$x" in -*) ;; *) a+=("$x");; esac; done
  local dst="${a[-1]}" srcf="${a[-2]}"; mkdir -p "$FAKEROOT$(dirname "$dst")"; command cp "$srcf" "$FAKEROOT$dst"; }
sysctl(){ echo "sysctl: $*" >> "$RLOG"; }
sshd(){ echo "sshd: $*" >> "$RLOG"; [ "${SSHD_OK:-1}" = 1 ]; }
systemctl(){ echo "systemctl: $*" >> "$RLOG"; }
cmp(){ return 1; }                                   # nothing matches the live file
: > "$RLOG"; restore_host_files "$hostdir"
expect '[ -f "$FAKEROOT/etc/ssh/sshd_config.d/01-bookstack.conf" ] && [ -f "$FAKEROOT/etc/sysctl.d/90-bookstack.conf" ] && [ -f "$FAKEROOT/etc/docker/daemon.json" ]' \
  "restore brings back the three host files nothing else regenerates (R5-restore)"
expect '[ ! -f "$FAKEROOT/etc/fail2ban/jail.local" ]' \
  "and NOT the ones the restore regenerates from the checkout, which would be a downgrade"
expect 'grep -q "sysctl: --system" "$RLOG" && grep -q "sshd: -t" "$RLOG"' \
  "sysctl is applied and sshd is VALIDATED before any reload"
expect 'printf %s "$HOST_RESTORED" | grep -q sshd_config.d && printf %s "$HOST_MANUAL" | grep -q "systemctl restart docker"' \
  "the admin is told what was restored, and that Docker still needs restarting for daemon.json"
# a drop-in that fails validation must be REMOVED, not reloaded: locking the owner out of a
# machine they are recovering is the worst possible moment for it
rm -f "$FAKEROOT/etc/ssh/sshd_config.d/01-bookstack.conf"
: > "$RLOG"; SSHD_OK=0 restore_host_files "$hostdir"
expect 'printf %s "$HOST_MANUAL" | grep -q "failed sshd -t" && ! grep -q "systemctl: reload" "$RLOG"' \
  "an sshd drop-in that fails validation is removed and reported, never reloaded"
unset -f install sysctl sshd systemctl cmp; unset SSHD_OK
eval "$_saved_stubs"; unset _saved_stubs        # restore the harness's own stubs

# V05: restic restore never deletes files the snapshot lacks, so what is already here beyond the
# snapshot's own size is not reclaimed and must not be credited against the space needed
df(){ printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 100 100 %s 50%% /\n' "${DF_AVAIL_K:-0}"; }
du(){ printf '%s\t%s\n' "${DU_K:-0}" "${2:-}"; }
export RESTIC_STATS_SIZE=$((5*1024*1024*1024))
DF_AVAIL_K=500000 DU_K=$((50*1024*1024)) restore_fits abc12345; rc=$?
expect '[ $rc != 0 ]' "a 5 GB snapshot with 0.5 GB free is refused even though 50 GB is already on disk (V05)"
DF_AVAIL_K=$((7*1024*1024)) DU_K=$((50*1024*1024)) restore_fits abc12345; rc=$?
expect '[ $rc = 0 ]' "and it still passes once the snapshot plus 1 GB headroom really fits"
unset RESTIC_STATS_SIZE; unset -f df du
reset "<cancel>"; step_restore; expect '[ $? != 0 ] && ! seen "compose down"' "Cancel in the snapshot picker changes nothing"
unset -f fail2ban-client

echo "== update: pre-update backup, code + tags, local health gate, rollback"
printf '#!/usr/bin/env bash\necho "backup.sh $*" >> "%s"\nexit ${BACKUP_RC:-0}\n' "$LOG" > "$STACK_DIR/scripts/backup.sh"
printf '#!/usr/bin/env bash\nexit ${SELFTEST_RC:-0}\n' > "$STACK_DIR/scripts/selftest.sh"; chmod +x "$STACK_DIR/scripts/"*.sh
copy_code_trees(){ echo "copy_code_trees" >> "$LOG"; record_version; }   # keep the stub scripts above in place
C6="<cancel> <cancel> <cancel> <cancel> <cancel> <cancel> <cancel>"   # skip the remaining 7 of 8 image prompts
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
rm -f "$T/etc/systemd/system/bookstack-postboot.service"; reset "no" "yes"; step_update >/dev/null
expect '[ -s "$T/etc/systemd/system/bookstack-postboot.service" ] && seen "systemctl: enable bookstack-postboot.service"' "Update installs the host-side units this version introduces, so an install that only ever Updates still gets them (F.6)"
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
# A07: "investigate, then retry" must not overwrite the record of the last known-good build
rm -f "$STACK_DIR/.update-in-progress"; envset IMG_CWA good/img:1
printf '#!/usr/bin/env bash\necho "backup.sh $*" >> "%s"\nexit 0\n' "$LOG" > "$STACK_DIR/scripts/backup.sh"; chmod +x "$STACK_DIR/scripts/backup.sh"
printf 'RESTIC_REPOSITORY=/mnt/backup\n' > "$renv"
FAIL_HEALTHZ=1; reset "yes" "broken/img:2" $C6 "yes" "no"; step_update >/dev/null; FAIL_HEALTHZ=0
expect '[ -f "$STACK_DIR/.update-in-progress" ] && grep -q "^IMG_CWA=good/img:1$" "$STACK_DIR/.env.images.prev"' "a declined rollback freezes the rollback point and records the known-good tag (A07)"
FAIL_HEALTHZ=1; reset "yes" "worse/img:3" $C6 "yes" "no"; step_update >/dev/null; FAIL_HEALTHZ=0
expect 'grep -q "^IMG_CWA=good/img:1$" "$STACK_DIR/.env.images.prev" && ! grep -q "broken/img:2" "$STACK_DIR/.env.images.prev" && [ "$(grep -c "docker: image tag bookstack/caddy:latest bookstack/caddy:prev" "$LOG")" = 0 ]' "a SECOND update neither overwrites .env.images.prev nor re-points :prev at the broken build (A07)"
expect 'grep -F msgbox "$LOG" | grep -q "will not overwrite them"' "and it says so, so the admin knows the rollback point is still intact (A07)"
reset "no" "yes" "yes"; step_update >/dev/null
expect '[ ! -f "$STACK_DIR/.update-in-progress" ]' "a successful update releases the frozen rollback point (A07)"
envset IMG_CWA ""
# A09: the step an admin reaches for after a leaked password must not claim sessions were ended
reset "yes"; step_rotate_secret; rc=$?
expect '[ $rc = 0 ] && seen "compose up -d librarian" && grep -F msgbox "$LOG" | grep -q "confirmed on /healthz"' "rotating the portal secret restarts the portal and confirms it came back (A09)"
sbak=$(envget LIBRARIAN_SECRET)
FAIL_HEALTHZ=1; reset "yes"; step_rotate_secret; rc=$?; FAIL_HEALTHZ=0
expect '[ $rc = 1 ] && [ "$(envget LIBRARIAN_SECRET)" != "$sbak" ] && grep -F msgbox "$LOG" | grep -q "NOBODY has been logged out"' "when the portal does not come back it says the OLD secret is still accepting cookies (A09)"
# A18: Monitoring used to hand out a page of instructions; it now configures Kuma, and a
# bootstrap that answers nothing (the stub docker prints nothing) must not read as success
reset; step_monitoring; expect '[ $? = 1 ] && grep -F msgbox "$LOG" | grep -q "Kuma was NOT configured"' "Monitoring never claims success when the Kuma bootstrap gave no answer"
eval "real_compose2() $(declare -f compose | sed '1d')"
compose(){ case "$*" in *"up -d uptime-kuma"*) echo "docker: compose $*" >> "$LOG"; return 1;; esac; real_compose2 "$@"; }
reset; step_monitoring; rc=$?
unset -f compose; eval "compose() $(declare -f real_compose2 | sed '1d')"; unset -f real_compose2
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "did not start" && ! seen "kuma-bootstrap"' "a Kuma that never started is reported, and nothing tries to configure it (A18)"
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
expect 'grep -q "restic: --retry-lock 30m backup $fs --exclude $fs/downloads .*--exclude $fs/cwa/config/processed_books --exclude $fs/library/staging --exclude $fs/library/seedbox --exclude $fs/library/seedbox-sync --exclude $fs/ephemera/downloads --exclude $fs/abs/metadata/cache --exclude $fs/abs/metadata/logs --exclude \*.db-wal --exclude \*.db-shm --exclude \*.sqlite-wal --exclude \*.sqlite-shm --tag bookstack --tag pre-update" "$RLOG"' "restic backup with the excludes (incl. re-downloadable caches, F77), --retry-lock and the extra tag"
rl(){ grep -nF -- "$1" "$RLOG" | head -1 | cut -d: -f1; }
expect '[ "$(rl "restic: --retry-lock 30m check --read-data-subset=1/52")" -lt "$(rl "restic: --retry-lock 30m forget")" ] && grep -q "restic: --retry-lock 30m forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-tag pre-update --prune" "$RLOG" && grep -q "restic: --retry-lock 30m stats latest --json" "$RLOG"' "weekly restic check runs BEFORE forget --prune; stats logged"
expect '! grep -q "restic: .*init" "$RLOG" && ! grep -q "tag --remove" "$RLOG" && grep -q "curl: -fsS -m 10 --retry 3 https://hc-ping.example/uuid$" "$DLOG"' "backup.sh never inits; recent pre-update snapshots kept; success ping sent (C1)"
# F.7: --read-data-subset=5% re-picked its 5 % AT RANDOM every week, so no particular pack was
# ever guaranteed to have been read and bit rot in a cold one could survive indefinitely. The
# n/52 form with a counter beside restic.env reads every byte exactly once a year - and only
# advances after a check that passed, so a failed week re-reads the same group.
expect 'grep -q "^check_group=2$" "$T/backup.state"' "the weekly verification counter advanced past the group it just read (F.7)"
: > "$RLOG"; BACKUP_CHECK_DOW=$(date +%u) STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect 'grep -q "restic: --retry-lock 30m check --read-data-subset=2/52" "$RLOG" && grep -q "^check_group=3$" "$T/backup.state"' "the next weekly run reads the NEXT 52nd, never a random 5% again (F.7)"
: > "$RLOG"; BACKUP_CHECK_DOW=8 STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect '! grep -q "restic: .* check" "$RLOG"' "no check on the other days of the week (F77)"
: > "$RLOG"; RESTIC_SNAP_TIME=2020-01-01T00:00:00Z STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect 'grep -q "restic: --retry-lock 30m tag --remove pre-update abc12345ffffffff" "$RLOG"' "pre-update snapshots older than 90 days lose their keep tag (F77)"
: > "$RLOG"; : > "$DLOG"; RESTIC_NOREPO=1 STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >"$T/b2.out" 2>&1; rc=$?
expect '[ $rc != 0 ] && ! grep -q "restic: .*init" "$RLOG" && ! grep -q "restic: .* backup" "$RLOG" && grep -q "not reachable or not initialised" "$T/b2.out" && grep -q "hc-ping.example/uuid/fail" "$DLOG"' "unreachable repository: fails without creating one, failure ping sent (F67, C1)"
# L15: the nightly job records the snapshot it wrote; next night it must still be there
expect 'grep -q "^last_snapshot=abc12345ffffffff$" "$T/backup.state"' "backup.sh records the snapshot it wrote (L15)"
printf '#!/usr/bin/env bash\necho "ALERT $*" >> "%s"\n' "$T/balert.log" > "$T/balert.sh"; chmod +x "$T/balert.sh"; : > "$T/balert.log"
: > "$RLOG"; BACKUP_ALERT="$T/balert.sh" STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >/dev/null 2>&1
expect '[ ! -s "$T/balert.log" ]' "the previous snapshot is still there: no alert"
sed -i.bak "s/^last_snapshot=.*/last_snapshot=deadbeef00000000/" "$T/backup.state"
: > "$RLOG"; BACKUP_ALERT="$T/balert.sh" STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" bash "$REPO/scripts/backup.sh" >"$T/b4.out" 2>&1; rc=$?
expect '[ $rc = 0 ] && grep -q "ALERT Bookstack: a backup snapshot has DISAPPEARED" "$T/balert.log" && grep -q "high$" "$T/balert.log" && grep -q "restic: --retry-lock 30m backup" "$RLOG"' "a vanished snapshot raises a high alert, and tonight's backup is still taken (L15)"
printf 'RESTIC_REPOSITORY=rest:http://bookstack:homesecret123@100.101.102.103:8000/bookstack/\nRESTIC_PASSWORD=x\nRESTIC_APPEND_ONLY=1\n' > "$T/restic-ao.env"; cp "$T/backup.state" "$T/backup-ao.state" 2>/dev/null
: > "$RLOG"; BACKUP_CHECK_DOW=$(date +%u) RESTIC_SNAP_TIME=2020-01-01T00:00:00Z BACKUP_STATE="$T/backup-ao.state" STACK_DIR="$fs" RESTIC_ENV="$T/restic-ao.env" bash "$REPO/scripts/backup.sh" >"$T/b5.out" 2>&1; rc=$?
expect '[ $rc = 0 ] && grep -q "restic: --retry-lock 30m backup" "$RLOG" && grep -q "restic: --retry-lock 30m check" "$RLOG" && ! grep -qE "restic: .*(forget|prune|tag --remove)" "$RLOG" && grep -q "append-only key: no forget/prune" "$T/b5.out"' "append-only key: backup and check run, nothing that deletes or rewrites a snapshot does (L15)"
expect 'grep -q "^RESTIC_APPEND_ONLY=1$" "$fs/.backup-snap/host/etc/bookstack/restic.env" && ! grep -q "^RESTIC_PASSWORD" "$fs/.backup-snap/host/etc/bookstack/restic.env"' "the redacted stub records the append-only mode (not a secret), still no password"
expect 'grep -qx "RESTIC_REPOSITORY=rest:http://REDACTED@100.101.102.103:8000/bookstack/" "$fs/.backup-snap/host/etc/bookstack/restic.env" && ! grep -rq homesecret123 "$fs/.backup-snap"' "a home server's login never reaches a snapshot (the address is kept, the user:password is not)"
: > "$RLOG"; RESTIC_SNAP_TIME=2020-01-01T00:00:00Z STACK_DIR="$fs" RESTIC_PRUNE_ENV="$T/restic.env" bash "$REPO/scripts/prune.sh" >"$T/p.out" 2>&1; rc=$?
expect '[ $rc = 0 ] && grep -q "restic: --retry-lock 30m tag --remove pre-update abc12345ffffffff" "$RLOG" && grep -q "restic: --retry-lock 30m forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-tag pre-update --prune" "$RLOG"' "prune.sh alone applies the retention policy with the key it is given (the timer or a laptop)"
RESTIC_KEEP_DAILY=3 STACK_DIR="$T/nowhere" RESTIC_PRUNE_ENV="$T/restic.env" bash "$REPO/scripts/prune.sh" >/dev/null 2>&1
expect 'grep -q "forget --keep-daily 3 --keep-weekly 4" "$RLOG"' "and runs away from the server (no .env) with RESTIC_KEEP_* from the shell"
RESTIC_PRUNE_ENV="$T/missing.env" bash "$REPO/scripts/prune.sh" >"$T/p2.out" 2>&1; expect '[ $? != 0 ] && grep -q "does not exist" "$T/p2.out"' "prune.sh without a key file refuses instead of guessing"
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
# The -i arms come FIRST: `df -i --output=ipcent` also matches *--output=pcent*... no, it does
# not, but it DOES fall through to the human table without them, which the >100 guard then
# rejects as "unknown" — so every inode assertion silently tested nothing.
# DF_IPCT=NONE simulates a filesystem that does not count inodes (df exits non-zero).
case "$*" in
  *-i*--output=ipcent*) [ "${DF_IPCT:-NONE}" = NONE ] && exit 1; printf 'IUse%%\n %s%%\n' "$DF_IPCT";;
  *-i*--output=iavail*) printf 'IFree\n4200000\n';;
  *--output=pcent*) printf 'Use%%\n %s%%\n' "$DF_PCT";;
  *--output=avail*) printf 'Avail\n1000000\n';;
  *) printf 'Filesystem Size Used Avail Use%% Mounted\n/dev/x 80G 70G 10G %s%% /\n' "$DF_PCT";;
esac
EOS
chmod +x "$bin/df"
touch -t 202001010000 "$fs/downloads/incomplete/old.part" "$fs/library/staging/old.bin" "$fs/library/ingest/old.part"; touch "$fs/downloads/incomplete/new.part"
printf "TORRENTS_ENABLED='false'\n" > "$fs/.env"
dw(){ DF_PCT=$1 DF_IPCT="${2-NONE}" STACK_DIR="$fs" DISK_STATE="$T/disk.state" bash "$REPO/scripts/disk-watch.sh"; }
: > "$DLOG"; dw 96 && ok "disk-watch.sh runs" || bad "disk-watch.sh failed"
expect 'grep -q "docker: compose stop shelfmark" "$DLOG" && ! grep -q "aria2" "$DLOG" && ! grep -q "stop qbittorrent" "$DLOG" && grep -q "^paused=1" "$T/disk.state" && grep -q "python -m notify alert Disk 96% full" "$DLOG" && grep -q " high --seq disk-level$" "$DLOG"' "96 %: shelfmark stopped (no aria2; qBittorrent not running), high-priority alert, state recorded"
expect '[ ! -f "$fs/downloads/incomplete/old.part" ] && [ ! -f "$fs/library/staging/old.bin" ] && [ ! -f "$fs/library/ingest/old.part" ] && [ -f "$fs/downloads/incomplete/new.part" ] && [ -f "$fs/library/ingest/stuck.epub" ]' "stale partials/staging deleted; fresh files and real ingest files kept"
expect 'grep -q "journalctl: --vacuum-size=200M" "$DLOG" && grep -q "docker: builder prune -f --filter until=168h" "$DLOG"' "journal and build cache trimmed"
: > "$DLOG"; dw 96; expect '! grep -q "notify alert" "$DLOG"' "still 96 %: no repeated alert"
: > "$DLOG"; dw 50; expect 'grep -q "docker: compose up -d shelfmark" "$DLOG" && ! grep -q qbittorrent "$DLOG" && grep -q "^paused=0" "$T/disk.state"' "back under 80 %: shelfmark recreated with up -d (start cannot revive a removed container); qBittorrent left alone while torrents are off"
expect 'grep -q "notify alert Disk back to 50% .* --seq disk-level --tags white_check_mark" "$DLOG"' "the all-clear carries the problem's id, so on the phone it replaces the 'Disk full' alert (v5.6)"
printf "TORRENTS_ENABLED='true'\n" > "$fs/.env"
: > "$DLOG"; QBIT_RUNNING=1 dw 97; expect 'grep -q "docker: compose --profile torrents stop qbittorrent" "$DLOG" && grep -q "qbittorrent" "$DLOG"' "96 %+ with torrents running: qBittorrent container stopped (F32)"
: > "$DLOG"; dw 40; expect 'grep -q "docker: compose --profile torrents up -d qbittorrent" "$DLOG"' "below 80 % with torrents enabled: qBittorrent started again"
grep -v '^last_alert=' "$T/disk.state" > "$T/disk.state.n"; mv "$T/disk.state.n" "$T/disk.state"   # pretend the last alert was long ago
: > "$DLOG"; dw 88; expect '! grep -q "compose stop" "$DLOG" && grep -q "notify alert Disk 88% full" "$DLOG"' "88 %: alert only (once per 24 h)"
: > "$DLOG"; dw 88; expect '! grep -q "notify alert" "$DLOG"' "88 % again within 24 h: silent"
# Inodes. The whole point of the inode watch is that blocks look fine while the filesystem is
# wedged, so the test that matters is blocks LOW and inodes HIGH — which the old df stub could
# never produce, leaving every inode branch unexecuted by the suite.
# Each case resets the latch explicitly: depending on what a previous assertion left behind is
# how the first draft of these tests silently tested nothing.
dwfresh(){ : > "$T/disk.state"; : > "$DLOG"; dw "$@"; }
dwfresh 40 97
# the profile flag is present or not depending on TORRENTS_ENABLED at this point in the suite
expect 'grep -q "notify alert Disk 97% full" "$DLOG" && grep -q "inodes 97%" "$DLOG" && grep -q "INODES, not bytes" "$DLOG" && grep -qE "docker: compose (--profile torrents )?stop shelfmark" "$DLOG"' \
  "blocks 40 % but inodes 97 %: the worse figure drives the stop, the alert names inodes and says bytes will not help"
dwfresh 40 88
expect 'grep -q "notify alert Disk 88% full" "$DLOG" && grep -q "inodes 88%" "$DLOG" && ! grep -q "compose stop" "$DLOG"' \
  "inodes 88 %: warn only, nothing stopped"
dwfresh 96 NONE
expect 'grep -q "notify alert Disk 96% full" "$DLOG" && ! grep -q "inodes" "$DLOG"' \
  "a filesystem that does not count inodes falls back to blocks alone, with no inode wording"
dwfresh 97 40
expect 'grep -q "notify alert Disk 97% full" "$DLOG" && grep -q "blocks 97%" "$DLOG" && ! grep -q "INODES, not bytes" "$DLOG"' \
  "blocks 97 % with inodes fine: no misleading inode advice"
# alert.sh (C1)
printf "NOTIFY_WEBHOOK='https://ntfy.example/secret-topic'\n" > "$fs/.env"
: > "$DLOG"; STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "T" "body" high; expect 'grep -q "docker: exec -i librarian python -m notify alert T body high" "$DLOG" && ! grep -q "^curl:" "$DLOG" && ! grep -q "^logger:" "$DLOG"' "alert.sh hands off to the portal's notify CLI (delivered -> nothing else)"
: > "$DLOG"; NOTIFY_RC=3 STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "Disk full" "body text" high; rc=$?
expect '[ $rc = 0 ] && grep -q "logger: -t bookstack -p user.warning ALERT Disk full: body text" "$DLOG" && grep -qF "curl: -fsS -m 20 --retry 2 -X POST -H Title: Disk full --data-binary body text -H Priority: high https://ntfy.example/secret-topic" "$DLOG"' "portal says 'not delivered' (exit 3): journal + direct ntfy POST from the host with Title/Priority (C1)"
: > "$DLOG"; ALERT_SEQ=selftest ALERT_TAGS=white_check_mark STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "T" "body"
expect 'grep -q "docker: exec -i librarian python -m notify alert T body --seq selftest --tags white_check_mark$" "$DLOG"' "alert.sh passes a problem id and tags to the portal (v5.6)"
: > "$DLOG"; NOTIFY_RC=3 ALERT_SEQ="disk level" ALERT_TAGS=warning STACK_DIR="$fs" bash "$REPO/scripts/alert.sh" "Disk full" "body text" high
expect 'grep -qF "curl: -fsS -m 20 --retry 2 -X POST -H Title: Disk full --data-binary body text -H Priority: high -H Sequence-ID: disk-level -H Tags: warning https://ntfy.example/secret-topic" "$DLOG"' "the host's own fallback POST carries them too, the id made header-safe (v5.6)"
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
expect '[ $? = 0 ] && [ "$(grep -c "ufw: --force delete.* from 173.245.4.0/22 " "$DLOG")" = 3 ] && [ "$(line_of_d() { grep -n "$1" "$DLOG" | head -1 | cut -d: -f1; }; line_of_d "ufw: allow")" -lt "$(grep -n "ufw: --force delete.*173.245.4.0/22" "$DLOG" | head -1 | cut -d: -f1)" ]' "a range Cloudflare stopped publishing is removed (all 3 of its rules), after the current ones were (re)added"
expect '! grep -q "ufw: allow .* port 80 " "$DLOG" && grep -q "ufw: --force delete allow proto tcp from 173.245.0.0/22 to any port 80" "$DLOG"' "port 80 is never opened any more, and the old port-80 rule of every current range is removed (L19)"
cp "$T/v4.good" "$CF_V4_FILE"

echo "== ephemera enable/disable"
envset TAILSCALE_IP 100.64.0.1; envset CF_API_TOKEN ""
reset "yes" "https://archive.example" "" "" "alice"; step_ephemera
expect '[ "$(envget EPHEMERA_ENABLED)" = true ] && [ "$(envget EPHEMERA_OWNER)" = alice ] && [ -d "$STACK_DIR/library/dropbox/alice" ] && seen "compose -f docker-compose.yml -f docker-compose.ephemera.yml --profile solver build ephemera"' "ephemera enabled, owner dropbox, pinned build (with the solver profile on, since Ephemera needs FlareSolverr)"
expect 'grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && seen "caddy reload"' "enabling Ephemera renders its vhost and reloads Caddy (C6)"
expect 'seen "compose -f docker-compose.yml -f docker-compose.ephemera.yml --profile solver up -d flaresolverr ephemera"' "enabling Ephemera starts the shared FlareSolverr with it (compose profile solver)"
envset FLARESOLVERR_ENABLED false
reset; step_ephemera_off; expect '[ "$(envget EPHEMERA_ENABLED)" = false ] && seen "stop ephemera" && seen "compose stop flaresolverr" && ! grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile" && seen "caddy reload"' "ephemera disabled; FlareSolverr stopped too (nothing else uses it); vhost removed and Caddy reloaded"
hbak3=$(envget ADMIN_HASH); envset ADMIN_HASH ""
reset "yes" "https://archive.example" "" "" "alice"; step_ephemera >/dev/null
expect 'grep -q "Caddy did NOT pick up the ephemera. site" "$LOG"' "Ephemera enabled but its vhost was not rendered: the success message says so (A18/V10)"
envset ADMIN_HASH "$hbak3"; reset; step_ephemera_off >/dev/null; render_caddyfile
envset EPHEMERA_OWNER ""; envset ADMIN_USER famadmin; reset "yes" "https://archive.example" "" "" ""; step_ephemera >/dev/null
expect '[ "$(envget EPHEMERA_OWNER)" = famadmin ]' "Ephemera's default owner is the chosen admin account, not 'admin'"
step_ephemera_off >/dev/null

echo "== torrents (qBittorrent opt-in, C5)"
rm -rf "$STACK_DIR/qbt/config/qBittorrent"; envset TORRENTS_ENABLED false
reset "yes"; step_torrents; rc=$?; qc="$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"
expect '[ $rc = 0 ] && [ "$(envget TORRENTS_ENABLED)" = true ] && seen "ufw: allow 6881/tcp" && seen "docker: compose --profile torrents up -d qbittorrent" && grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile"' "enable: profile start, port 6881 opened, dl. vhost rendered"
# A17: the portal reads TORRENTS_ENABLED at start-up to decide whether /admin shows the link
expect 'seen "compose --profile torrents up -d librarian"' "enabling torrents restarts the portal, so its qBittorrent link appears (A17)"
expect '! grep -F "whiptail: " "$LOG" | grep -q "will not answer yet"' "with Caddy happy the success screen makes no warning noise"
expect 'grep -q "^\[BitTorrent\]" "$qc" && grep -qF "Session\\DefaultSavePath=/dropbox/$(envget ADMIN_USER)" "$qc" && grep -qF "Session\\TempPath=/downloads/incomplete" "$qc" && grep -qF "Session\\TempPathEnabled=true" "$qc"' "qBittorrent.conf seeded: default save path = the admin's dropbox, partials in /downloads/incomplete"
printf '[BitTorrent]\nSession\\DefaultSavePath=/downloads\nSession\\Port=6881\n\n[Preferences]\nWebUI\\Port=8080\n' > "$qc"; qbt_seed_config
expect '[ "$(grep -c "DefaultSavePath" "$qc")" = 1 ] && grep -qF "Session\\DefaultSavePath=/dropbox/" "$qc" && grep -qF "Session\\Port=6881" "$qc" && grep -qF "WebUI\\Port=8080" "$qc" && [ "$(grep -c "^\[BitTorrent\]" "$qc")" = 1 ]' "existing qBittorrent.conf: keys replaced in place, other settings kept"
expect 'grep -q "Save path: /dropbox/alice" "$LOG"' "admin told to point per-user categories at /dropbox/<user>"
reset "yes"; step_torrents
expect '[ "$(envget TORRENTS_ENABLED)" = false ] && seen "ufw: --force delete allow 6881/tcp" && seen "rm -f qbittorrent" && ! grep -q "^dl.example.test {" "$STACK_DIR/caddy/Caddyfile"' "disable: container removed, 6881 closed, dl. vhost gone"
expect 'seen "compose up -d librarian"' "disabling torrents restarts the portal too (A17)"
# A18/V10: the success screen hands out a URL and a password, so a Caddy that did not pick the
# site up must be said out loud instead of contradicted
hbak2=$(envget ADMIN_HASH); envset ADMIN_HASH ""
reset "yes"; step_torrents >/dev/null; rc=$?
expect 'grep -q "will not answer yet" "$LOG"' "torrents enabled but Caddy has no dl. site: the screen says the URL will not answer (A18/V10)"
envset ADMIN_HASH "$hbak2"; reset "yes"; step_torrents >/dev/null; render_caddyfile
expect 'declare -f menu_library | grep -q step_torrents' "Torrents item in the Library menu"

echo "== intake webhook (C11)"
envset INTAKE_TOKEN ""; reset "yes"; step_intake_webhook
tok=$(envget INTAKE_TOKEN); expect '[ ${#tok} -ge 32 ] && seen "compose up -d librarian" && grep -F msgbox "$LOG" | grep -q "X-Intake-Token: $tok"' "intake webhook off until enabled here; enabling generates a token and restarts the portal"
reset "no"; step_intake_webhook; expect '[ -z "$(envget INTAKE_TOKEN)" ]' "and it can be turned off again"

echo "== FEAT-4: reopen public SSH (the reverse of Lock SSH)"
envset SSH_LOCKED false
reset; step_unlock_ssh; rc=$?
expect '[ $rc = 0 ] && ! seen "ufw: allow 22/tcp" && grep -F msgbox "$LOG" | grep -q "not locked"' "SSH is not locked: the step says so and touches nothing (FEAT-4)"
envset SSH_LOCKED true
reset "no"; step_unlock_ssh; rc=$?
expect '[ $rc = 0 ] && ! seen "ufw: allow 22/tcp" && [ "$(envget SSH_LOCKED)" = true ]' "declining the confirmation leaves port 22 closed (FEAT-4)"
reset "yes"; step_unlock_ssh; rc=$?
expect '[ $rc = 0 ] && seen "ufw: allow 22/tcp" && [ "$(envget SSH_LOCKED)" = false ]' "confirmed: ufw allows 22/tcp again and SSH_LOCKED is cleared (FEAT-4)"
expect 'grep -F msgbox "$LOG" | grep -q "[Kk]ey-only" && grep -F "yesno: " "$LOG" | grep -q "PasswordAuthentication no"' "and it says plainly that key-only authentication still applies (FEAT-4)"
# the whole point: System must stop re-closing the port on every run
reset; setup_firewall; expect 'seen "ufw: allow 22/tcp" && ! seen "ufw: --force delete allow 22/tcp"' "after reopening, Install -> System keeps port 22 open instead of deleting the rule again (FEAT-4)"
# an unwritable .env must not leave the admin believing the change survives the next System run
envset SSH_LOCKED true
eval "real_envset3() $(declare -f envset | sed '1d')"
envset(){ [ "$1" = SSH_LOCKED ] && return 1; real_envset3 "$@"; }
reset "yes"; step_unlock_ssh; rc=$?
unset -f envset; eval "envset() $(declare -f real_envset3 | sed '1d')"; unset -f real_envset3
expect '[ $rc = 1 ] && seen "ufw: allow 22/tcp" && grep -F msgbox "$LOG" | grep -q "will close it again"' "an unwritable .env is reported: the port is open now but System would close it again (FEAT-4)"
envset SSH_LOCKED false

echo "== FEAT-1: release a banned address (fail2ban AND Cloudflare)"
reset; step_unban; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "fail2ban is not installed"' "no fail2ban on the server: the step refuses instead of pretending (FEAT-1)"
# a jail that holds the household's address, and a Cloudflare IP Access Rule for the same one
fail2ban-client(){ echo "fail2ban-client: $*" >> "$LOG"
  case "$*" in
    "status caddy-auth") printf 'Status for the jail: caddy-auth\n`- Actions\n   |- Currently banned: 1\n   `- Banned IP list:\t%s\n' "${F2B_BANNED-203.0.113.9}";;
    "status "*) printf 'Status for the jail: x\n   `- Banned IP list:\t\n';;
    "set "*" unbanip "*) [ "${F2B_UNBAN_RC:-0}" = 0 ] || return 1;;
  esac; return 0; }
envset CF_API_TOKEN cf-token-123; envset DOMAIN example.test
touch "$CFSTORE/ban"; reset "" "yes"; step_unban; rc=$?
expect '[ $rc = 0 ] && seen "fail2ban-client: set caddy-auth unbanip 203.0.113.9" && seen "fail2ban-client: set sshd unbanip 203.0.113.9" && seen "fail2ban-client: set caddy-abs-login unbanip 203.0.113.9" && seen "fail2ban-client: set caddy-device-auth unbanip 203.0.113.9"' "the banned address is offered as the default and released in EVERY bookstack jail (FEAT-1)"
expect 'seen "cf: DELETE /zones/zone-STUB/firewall/access_rules/rules/rule-STUB" && [ ! -f "$CFSTORE/ban" ]' "and its Cloudflare IP Access Rule is deleted — the local unban alone would leave the edge ban in force (FEAT-1)"
expect 'grep -q "caddy-auth: 203.0.113.9" "$LOG" && [ "$(line_of "caddy-auth: 203.0.113.9")" -lt "$(line_of "ask: Address to release")" ]' "the currently banned addresses are shown BEFORE anything is asked (FEAT-1)"
reset "" "no"; touch "$CFSTORE/ban"; step_unban; rc=$?
expect '[ $rc = 0 ] && ! seen "fail2ban-client: set" && [ -f "$CFSTORE/ban" ]' "declining the confirmation releases nothing, locally or at Cloudflare (FEAT-1)"
reset "not.an.address.at.all" ; step_unban; rc=$?
expect '[ $rc = 1 ] && ! seen "fail2ban-client: set" && grep -F msgbox "$LOG" | grep -q "does not look like an IP"' "a typo is refused before it reaches fail2ban or the Cloudflare API (FEAT-1)"
touch "$CFSTORE/ban"; reset "203.0.113.9" "yes"; CF_UNBAN_RC=22; step_unban; rc=$?; CF_UNBAN_RC=0
expect '[ $rc = 1 ] && seen "fail2ban-client: set caddy-auth unbanip 203.0.113.9" && grep -F msgbox "$LOG" | grep -q "Firewall Services"' "a Cloudflare rule that cannot be deleted is reported (token scope), after the local unban ran (FEAT-1)"
rm -f "$CFSTORE/ban"
tokbak=$(envget CF_API_TOKEN); envset CF_API_TOKEN ""
reset "203.0.113.9" "yes"; step_unban; rc=$?
expect '[ $rc = 1 ] && seen "fail2ban-client: set caddy-auth unbanip 203.0.113.9" && grep -F msgbox "$LOG" | grep -q "no Cloudflare token"' "without a Cloudflare token the local unban still happens and the missing half is named (FEAT-1)"
envset CF_API_TOKEN "$tokbak"
F2B_BANNED=""; F2B_UNBAN_RC=1; touch "$CFSTORE/ban"; reset "198.51.100.44" "yes"; step_unban >/dev/null; F2B_UNBAN_RC=0
expect 'seen "fail2ban-client: set sshd unbanip 198.51.100.44" && [ ! -f "$CFSTORE/ban" ] && grep -F msgbox "$LOG" | grep -q "held no ban"' "an address no jail holds is still released at Cloudflare, and the difference is stated (FEAT-1)"
unset F2B_BANNED
expect 'declare -f menu_security | grep -q step_unban && declare -f menu_security | grep -q step_unlock_ssh' "both Security entries are wired into the menu (FEAT-1, FEAT-4)"
unset -f fail2ban-client

echo "== FEAT-2: clear a portal login lockout"
eval "real_portal_up() $(declare -f portal_up | sed '1d')"
portal_up(){ [ "${PORTAL_DOWN:-0}" = 0 ]; }
PORTAL_DOWN=1; reset; step_lockout; rc=$?; PORTAL_DOWN=0
expect '[ $rc = 1 ] && ! seen "admin_cli" && grep -F msgbox "$LOG" | grep -q "portal is not running"' "portal down: the step refuses instead of showing an empty list (FEAT-2)"
CLI_FAIL=1; reset; step_lockout; rc=$?; CLI_FAIL=0
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "the portal database is locked"' "an admin_cli failure surfaces its error sentence, not a traceback (FEAT-2)"
CLI_LOCKOUT='{"ok": true, "users": [{"user": "alice", "ip": "203.0.113.9", "until": 99999999999, "seconds": 600}], "ips": [{"ip": "203.0.113.9", "until": 99999999999, "seconds": 600}]}'
reset "0"; step_lockout; rc=$?
expect '[ $rc = 0 ] && seen "docker: exec -i librarian python -m admin_cli lockout status" && grep -F "msgbox" "$LOG" | grep -q "" && grep -F "whiptail: " "$LOG" | grep -q "Portal login lockouts"' "who is locked out is read from admin_cli and shown before anything is cleared (FEAT-2)"
CLI_CLEAR='{"ok": true, "cleared": 3}'
reset "U" "alice"; step_lockout; rc=$?
expect '[ $rc = 0 ] && seen "admin_cli lockout clear --user alice" && grep -F msgbox "$LOG" | grep -q "Released 3"' "clear one user: the right arm is called and the count reported (FEAT-2)"
reset "I" "203.0.113.9"; step_lockout
expect 'seen "admin_cli lockout clear --ip 203.0.113.9"' "clear one address — the key that takes the whole household down (FEAT-2)"
reset "I" "nonsense"; step_lockout; rc=$?
expect '[ $rc = 1 ] && ! seen "lockout clear --ip" && grep -F msgbox "$LOG" | grep -q "does not look like an IP"' "a malformed address is refused before admin_cli is called (FEAT-2)"
reset "A" "no"; step_lockout; rc=$?
expect '[ $rc = 0 ] && ! seen "lockout clear --all"' "'release everything' does nothing unless it is confirmed (FEAT-2)"
reset "A" "yes"; step_lockout
expect 'seen "admin_cli lockout clear --all"' "confirmed: every lockout is released (FEAT-2)"
expect 'declare -f menu_users | grep -q step_lockout' "the lockout entry is wired into the Users menu (FEAT-2)"

echo "== FEAT-7: the whole request queue, paginated (no silent 200-row cap)"
CLI_FAIL=1; reset "pending" ; step_requests; rc=$?; CLI_FAIL=0
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "cannot read the queue"' "an admin_cli failure is reported rather than shown as an empty queue (FEAT-7)"
CLI_REQUESTS='{"ok": true, "total": 412, "rows": [{"rid": 7, "user": "alice", "title": "A Title", "status": "pending", "detail": "waiting", "created": 1}, {"rid": 8, "user": "bob", "title": "Another", "status": "pending", "detail": "", "created": 2}]}'
reset "pending" "0"; step_requests; rc=$?
expect '[ $rc = 0 ] && seen "admin_cli requests list --status pending --limit 20 --offset 0"' "the queue is read with an explicit status, limit and offset (FEAT-7)"
expect 'grep -F "whiptail: " "$LOG" | grep -q "Rows 1-2 of 412"' "the row count comes from the full total, so nothing is capped at 200 (FEAT-7)"
reset "pending" "N" "P" "0"; step_requests >/dev/null
expect 'seen "requests list --status pending --limit 20 --offset 20" && seen "requests list --status pending --limit 20 --offset 0"' "next / previous page really move the offset (FEAT-7)"
reset "all" "0"; step_requests >/dev/null
expect 'grep -q "admin_cli requests list --limit 20 --offset 0" "$LOG"' "'everything' passes no --status filter at all (FEAT-7)"
reset "error" "7" "R" "<cancel>"; step_requests >/dev/null
expect 'seen "admin_cli requests retry 7"' "picking a row and choosing Retry calls the retry arm (FEAT-7)"
reset "error" "7" "D" "no" "<cancel>"; step_requests >/dev/null
expect '! seen "admin_cli requests dismiss"' "dismiss asks first, and a No dismisses nothing (FEAT-7)"
reset "error" "7" "D" "yes" "<cancel>"; step_requests >/dev/null
expect 'seen "admin_cli requests dismiss 7"' "confirmed: the row is dismissed (FEAT-7)"
CLI_ACT_FAIL=1; reset "error" "7" "R" "<cancel>"; step_requests >/dev/null; CLI_ACT_FAIL=0
expect 'grep -F msgbox "$LOG" | grep -q "no re-fetchable source"' "a retry the portal refuses explains why instead of claiming success (FEAT-7)"
CLI_REQUESTS='{"ok": true, "total": 0, "rows": []}'
reset "pending"; step_requests; rc=$?
expect '[ $rc = 0 ] && grep -F msgbox "$LOG" | grep -q "No pending requests"' "an empty queue says so instead of drawing an empty menu (FEAT-7)"

echo "== FEAT-8: parked files in dropbox/<user>/.failed"
CLI_FAIL=1; reset; step_parked; rc=$?; CLI_FAIL=0
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "the dropbox is unreadable"' "a failing parked list is reported (FEAT-8)"
reset; step_parked; rc=$?
expect '[ $rc = 0 ] && grep -F msgbox "$LOG" | grep -q "Nothing is parked"' "nothing parked: a sentence, not an empty menu (FEAT-8)"
CLI_PARKED='{"ok": true, "rows": [{"token": "dG9rZW4x", "user": "zoe", "name": "hobbit.zip", "bytes": 52428800, "mtime": 1, "reason": "mixed folder: neither ebooks nor audio"}]}'
reset "0"; step_parked; rc=$?
expect '[ $rc = 0 ] && grep -F "whiptail: " "$LOG" | grep -q "hobbit.zip" && grep -F "whiptail: " "$LOG" | grep -q "50.0 MB" && grep -F "whiptail: " "$LOG" | grep -q "mixed folder"' "each parked entry shows its owner, size in MB and the reason it was parked (FEAT-8)"
reset "dG9rZW4x" "R" "<cancel>"; CLI_ACT='{"ok": true, "moved": "/srv/bookstack/library/dropbox/zoe/hobbit.zip"}' step_parked >/dev/null
expect 'seen "admin_cli parked retry dG9rZW4x"' "Retry moves it back into the dropbox (FEAT-8)"
reset "dG9rZW4x" "D" "no" "<cancel>"; step_parked >/dev/null
expect '! seen "admin_cli parked delete"' "Delete asks first, and a No deletes nothing (FEAT-8)"
reset "dG9rZW4x" "D" "yes" "<cancel>"; step_parked >/dev/null
expect 'seen "admin_cli parked delete dG9rZW4x" && grep -F "yesno: " "$LOG" | grep -q "cannot be undone"' "confirmed deletion says out loud that it cannot be undone from here (FEAT-8)"
CLI_ACT='{"ok": true}'

echo "== FEAT-9: Audiobookshelf rescan"
abak2=$(envget ABS_TOKEN); envset ABS_TOKEN ""
reset; step_abs_scan; rc=$?
expect '[ $rc = 1 ] && ! seen "python -m abs scan" && grep -F msgbox "$LOG" | grep -q "no API key"' "no ABS_TOKEN: the rescan refuses and points at Library -> Audiobookshelf (FEAT-9)"
envset ABS_TOKEN abs-key-STUB
reset "no"; step_abs_scan; rc=$?
expect '[ $rc = 0 ] && ! seen "python -m abs scan"' "declining the confirmation starts no scan (FEAT-9)"
reset "yes"; step_abs_scan; rc=$?
expect '[ $rc = 0 ] && seen "docker: exec -i librarian python -m abs scan"' "confirmed: absctl scan is called, so the admin needs no invocation by heart (FEAT-9)"
envset ABS_TOKEN "$abak2"
expect 'declare -f menu_library | grep -q step_abs_scan && declare -f menu_library | grep -q step_requests && declare -f menu_library | grep -q step_parked' "the three Library entries are wired into the menu (FEAT-7, FEAT-8, FEAT-9)"
unset -f portal_up; eval "portal_up() $(declare -f real_portal_up | sed '1d')"; unset -f real_portal_up

echo "== FEAT-3: restart / stop / start one service"
reset "librarian" "R" "yes"; step_service; rc=$?
expect '[ $rc = 0 ] && seen "docker: compose ps --services" && grep -F "whiptail: " "$LOG" | grep -q "audiobookshelf"' "the service list comes from compose ps (FEAT-3)"
expect 'seen "docker: compose up -d --force-recreate librarian"' "Restart RECREATES the container, so a setting this TUI wrote is actually picked up (FEAT-3)"
reset "shelfmark" "U"; step_service; rc=$?
expect '[ $rc = 0 ] && seen "docker: compose up -d shelfmark" && ! grep -qE "docker: compose( --profile torrents)? start shelfmark" "$LOG"' "Start uses 'up -d', never 'start': a removed container is recreated (FEAT-3)"
reset "shelfmark" "S" "no"; step_service; rc=$?
expect '[ $rc = 0 ] && ! seen "compose stop shelfmark"' "declining the stop confirmation changes nothing (FEAT-3)"
reset "shelfmark" "S" "yes"; step_service
expect 'seen "docker: compose stop shelfmark"' "confirmed: the service is stopped (FEAT-3)"
reset "caddy" "S" "no"; step_service; rc=$?
expect '[ $rc = 0 ] && ! seen "compose stop caddy" && grep -F "yesno: " "$LOG" | grep -q "ONLY process that answers from the internet"' "stopping caddy names what goes dark and is refused on No (FEAT-3)"
reset "caddy" "S" "yes"; step_service
expect 'seen "docker: compose stop caddy" && grep -F msgbox "$LOG" | grep -q "EVERY public site is down"' "and when it IS stopped the admin is told, not left to find out (FEAT-3)"
abak3=$(envget AUTHELIA_ENABLED); envset AUTHELIA_ENABLED true
PS_SERVICES="caddy librarian"; reset "authelia" "R" "yes"; step_service >/dev/null
expect 'seen "docker: compose -f docker-compose.yml -f docker-compose.authelia.yml up -d --force-recreate authelia"' "Authelia is driven through its overlay compose file, which plain compose cannot see (FEAT-3)"
envset AUTHELIA_ENABLED "$abak3"; unset PS_SERVICES
eval "real_compose3() $(declare -f compose | sed '1d')"
compose(){ case "$*" in *"up -d --force-recreate"*) echo "docker: compose $*" >> "$LOG"; return 1;; esac; real_compose3 "$@"; }
reset "librarian" "R" "yes"; step_service; rc=$?
unset -f compose; eval "compose() $(declare -f real_compose3 | sed '1d')"; unset -f real_compose3
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "could not do that"' "a compose failure is reported instead of a success screen (FEAT-3)"
expect 'declare -f menu_ops | grep -q step_service' "the entry is wired into the Operations menu (FEAT-3)"

echo "== FEAT-5: restore a SINGLE file beside the stack"
export FAKESNAP="$T/fakesnap"
rm -f "$renv"; reset; step_restore_file; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "No backup repository"' "without a repository the step refuses (FEAT-5)"
printf 'RESTIC_REPOSITORY=/mnt/backup\nRESTIC_PASSWORD=resticpass-123\n' > "$renv"
: > "$RLOG"; reset "abc12345" "library/books/big.epub" "no"; step_restore_file; rc=$?
expect '[ $rc = 0 ] && ! grep -q "restore abc12345" "$RLOG" && ! seen "compose down"' "declining the confirmation restores nothing and stops nothing (FEAT-5)"
: > "$RLOG"; reset "abc12345" "/etc/passwd" "yes"; step_restore_file; rc=$?
expect '[ $rc = 1 ] && ! grep -q "restore abc12345" "$RLOG" && grep -F msgbox "$LOG" | grep -q "outside $STACK_DIR"' "a path outside \$STACK_DIR is refused: the snapshots contain nothing else (FEAT-5)"
: > "$RLOG"; rm -rf "$T"/bookstack-restored-*
reset "abc12345" "library/books/big.epub" "yes"; step_restore_file; rc=$?
stg=$(ls -d "$T"/bookstack-restored-* 2>/dev/null | head -1)
expect '[ $rc = 0 ] && [ -n "$stg" ] && [ -f "$stg$STACK_DIR/library/books/big.epub" ]' "the file lands in a staging directory BESIDE the stack, never in place (FEAT-5)"
expect 'grep -qE "restic: (--retry-lock 30m )?restore abc12345 --target $T/bookstack-restored-[0-9-]+ --include $STACK_DIR/library/books/big.epub" "$RLOG"' "restic restores exactly that one path, not the whole snapshot (FEAT-5)"
expect '! seen "docker: compose down" && grep -F msgbox "$LOG" | grep -q "$stg"' "nothing is stopped and the admin is told where it landed (FEAT-5)"
: > "$RLOG"; rm -rf "$T"/bookstack-restored-*
reset "abc12345" "library/books/never-existed.epub" "yes"; step_restore_file; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "contains nothing at" && [ -z "$(ls -d "$T"/bookstack-restored-* 2>/dev/null)" ]' "restic exits 0 when an --include matches nothing: the empty result is caught and the staging dir removed (FEAT-5)"
expect 'declare -f menu_ops | grep -q step_restore_file' "the entry is wired into the Operations menu (FEAT-5)"

echo "== FEAT-6: rotate the restic repository password safely"
rm -f "$renv"; reset; step_restic_rotate; rc=$?
expect '[ $rc = 1 ] && grep -F msgbox "$LOG" | grep -q "No backup repository"' "nothing configured: there is no password to rotate (FEAT-6)"
mkrenv(){ printf 'RESTIC_REPOSITORY=/mnt/backup\nRESTIC_PASSWORD=oldpass-123\nAWS_ACCESS_KEY_ID=keyid\n' > "$renv"; chmod 600 "$renv"; }
mkrenv; cp "$renv" "$T/renv.rot"
export RESTIC_NOREPO=1; : > "$RLOG"; reset; step_restic_rotate; rc=$?; export RESTIC_NOREPO=0
expect '[ $rc = 1 ] && cmp -s "$renv" "$T/renv.rot" && ! grep -q "key add" "$RLOG" && grep -F msgbox "$LOG" | grep -q "nothing safe to rotate FROM"' "a credential that does not open the repository is not a rotation starting point (FEAT-6)"
: > "$RLOG"; RESTIC_KEYADD_RC=1 reset "yes" "newpass-4567" "newpass-4567"; RESTIC_KEYADD_RC=1 step_restic_rotate; rc=$?
expect '[ $rc = 1 ] && cmp -s "$renv" "$T/renv.rot" && ! grep -q "key remove" "$RLOG" && [ ! -e "$renv.new" ] && grep -F msgbox "$LOG" | grep -q "NOTHING was changed"' "a refused 'key add' leaves the working credential file and every snapshot exactly as they were (FEAT-6)"
: > "$RLOG"; reset "yes" "newpass-4567" "newpass-4567"; RESTIC_BAD_PW=newpass-4567 step_restic_rotate; rc=$?
expect '[ $rc = 1 ] && cmp -s "$renv" "$T/renv.rot" && ! grep -q "key remove" "$RLOG" && [ ! -e "$renv.new" ] && grep -F msgbox "$LOG" | grep -q "does NOT open the repository"' "the new key is TESTED before anything is removed or replaced — the whole point of the step (FEAT-6)"
: > "$RLOG"; reset "yes" "newpass-4567" "newpass-4567"; step_restic_rotate; rc=$?
expect '[ $rc = 0 ] && grep -q "restic-key-add-pw: newpass-4567" "$RLOG" && grep -qE "${RX}key remove 0a1b2c3doldkey" "$RLOG"' "happy path: the new key is added, then the OLD key (the 'current' one) is removed (FEAT-6)"
expect '[ "$(rl "key add")" -lt "$(rl "key remove")" ] && grep -q "restic-key-remove-pw: newpass-4567" "$RLOG"' "add comes first, and the removal runs with the NEW password (restic refuses to remove the key it is using) (FEAT-6)"
expect 'grep -q "^RESTIC_PASSWORD=newpass-4567$" "$renv" && grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv" && grep -q "^AWS_ACCESS_KEY_ID=keyid$" "$renv" && [ ! -e "$renv.new" ] && [ "$(stat -c %a "$renv" 2>/dev/null || stat -f %Lp "$renv")" = 600 ]' "only now is restic.env rewritten: new password, repository and S3 keys kept, still 0600 (FEAT-6)"
mkrenv; : > "$RLOG"; reset "yes" "newpass-4567" "newpass-4567"; RESTIC_KEYRM_RC=1 step_restic_rotate; rc=$?
expect '[ $rc = 1 ] && grep -q "^RESTIC_PASSWORD=newpass-4567$" "$renv" && grep -F msgbox "$LOG" | grep -q "OLD key could NOT be removed"' "a failed 'key remove' still saves the working new password, and says the old one still opens the repository (FEAT-6)"
mkrenv; reset "no"; step_restic_rotate; rc=$?
expect '[ $rc = 0 ] && grep -q "^RESTIC_PASSWORD=oldpass-123$" "$renv"' "declining the confirmation rotates nothing (FEAT-6)"
# the destructive Backups re-run and this safe rotation must point at each other
expect 'declare -f step_backup | grep -q "Rotate the backup repository password" && declare -f menu_ops | grep -q step_restic_rotate' "Install -> Backups, which REFUSES a new password, names the step that does it safely (FEAT-6)"
printf 'RESTIC_REPOSITORY=/mnt/backup\n' > "$renv"

echo "== FEAT-10: advanced settings (the tunables that had no writer)"
# the values this screen shows must be the ones the container really falls back to
python3 - "$REPO" "$ADV_SETTINGS" <<'PY' && ok "every Advanced default matches docker-compose.yml's \${KEY:-default} and .env.example (FEAT-10)" || bad "Advanced settings default mismatch"
import re, sys
repo, spec = sys.argv[1], sys.argv[2]
comp = dict(re.findall(r"\$\{(\w+):-([^}]*)\}", open(repo + "/docker-compose.yml").read()))
env = dict(re.findall(r"^(\w+)=(\S*)", open(repo + "/.env.example").read(), re.M))
bad = []
for line in spec.strip().splitlines():
    _g, key, default, _kind, _help = line.split("|", 4)
    if key in comp and comp[key] != default:
        bad.append("%s: compose says %s, the TUI shows %s" % (key, comp[key], default))
    if key in env and env[key] != default:
        bad.append("%s: .env.example says %s, the TUI shows %s" % (key, env[key], default))
    if key not in comp and key not in env:
        bad.append("%s: reaches the stack from nowhere (not in compose, not in .env.example)" % key)
print("\n".join("     " + b for b in bad)); sys.exit(1 if bad else 0)
PY
envset LOCKOUT_FAILS ""; envset SESSION_HOURS ""
reset "lockout" "0" "0"; step_advanced; rc=$?
expect '[ $rc = 0 ] && grep -F "whiptail: " "$LOG" | grep -q "LOCKOUT_FAILS 5" && grep -F "whiptail: " "$LOG" | grep -q "SESSION_HOURS 12"' "an unset key shows the built-in default the container really uses, not a blank (FEAT-10)"
reset "lockout" "LOCKOUT_FAILS" "9" "0" "0"; step_advanced
expect '[ "$(envget LOCKOUT_FAILS)" = 9 ] && seen "compose up -d librarian" && grep -F msgbox "$LOG" | grep -q "is live"' "changing a lockout value writes it and recreates the portal so it is in force (FEAT-10)"
reset "lockout" "LOCKOUT_FAILS" "lots" "0" "0"; step_advanced
expect '[ "$(envget LOCKOUT_FAILS)" = 9 ] && grep -F msgbox "$LOG" | grep -q "must be a whole number"' "a non-numeric value is refused and the old one kept (FEAT-10)"
reset "mail" "IMAP_SSL" "maybe" "0" "0"; step_advanced
expect '[ -z "$(envget IMAP_SSL)" ] && grep -F msgbox "$LOG" | grep -q "must be exactly true or false"' "a boolean only takes true or false (FEAT-10)"
reset "mail" "IMAP_SSL" "false" "0" "0"; step_advanced
expect '[ "$(envget IMAP_SSL)" = false ] && seen "compose up -d librarian"' "and a valid boolean is stored and applied (FEAT-10)"
reset "disk" "DISK_STOP_PCT" "150" "0" "0"; step_advanced
expect '[ -z "$(envget DISK_STOP_PCT)" ] && grep -F msgbox "$LOG" | grep -q "between 1 and 99"' "a percentage outside 1-99 is refused (FEAT-10)"
envset DISK_WARN_PCT ""; envset DISK_RESUME_PCT ""
reset "disk" "DISK_STOP_PCT" "70" "0" "0"; step_advanced
expect '[ "$(envget DISK_STOP_PCT)" = 70 ] && grep -F msgbox "$LOG" | grep -q "warn=85 stop=70 resume=80"' "thresholds that cannot work together are called out instead of silently accepted (FEAT-10)"
envset DISK_STOP_PCT ""
reset "backup" "RESTIC_KEEP_MONTHLY" "12" "0" "0"; step_advanced
expect '[ "$(envget RESTIC_KEEP_MONTHLY)" = 12 ] && ! seen "compose up -d librarian" && grep -F msgbox "$LOG" | grep -q "nothing has to be restarted"' "retention is read by backup.sh at run time, so no container is restarted for it (FEAT-10)"
reset "uploads" "MAX_PDF_MB" "400" "0" "0"; step_advanced
expect '[ "$(envget MAX_PDF_MB)" = 400 ]' "the upload-cap group writes its keys too (FEAT-10)"
eval "real_envset4() $(declare -f envset | sed '1d')"
envset(){ [ "$1" = KINDLE_MAX_MB ] && return 1; real_envset4 "$@"; }
reset "uploads" "KINDLE_MAX_MB" "25" "0" "0"; step_advanced
unset -f envset; eval "envset() $(declare -f real_envset4 | sed '1d')"; unset -f real_envset4
expect 'grep -F msgbox "$LOG" | grep -q "was NOT changed"' "a write that cannot happen is reported, never reported as applied (FEAT-10)"
eval "real_restart_portal() $(declare -f restart_portal | sed '1d')"
restart_portal(){ return 1; }
reset "lockout" "SESSION_HOURS" "48" "0" "0"; step_advanced
unset -f restart_portal; eval "restart_portal() $(declare -f real_restart_portal | sed '1d')"; unset -f real_restart_portal
expect '[ "$(envget SESSION_HOURS)" = 48 ] && grep -F msgbox "$LOG" | grep -q "NOT in force yet"' "a portal that will not restart means the setting is NOT live, and it says so (FEAT-10)"
expect 'declare -f menu_ops | grep -q step_advanced' "the entry is wired into the Operations menu (FEAT-10)"
for k in LOCKOUT_FAILS SESSION_HOURS IMAP_SSL DISK_WARN_PCT DISK_STOP_PCT DISK_RESUME_PCT RESTIC_KEEP_MONTHLY MAX_PDF_MB; do envset "$k" ""; done

echo "== F.6: the post-reboot self-test runs, records its result and alerts"
# The generated wrapper is run as a REAL process, the way systemd runs it: stub selftest.sh,
# stub alert.sh, stub docker on PATH. POSTBOOT_WAIT/POSTBOOT_SLEEP are the only knobs the
# wrapper exposes, so the health wait can be exercised in seconds instead of 15 minutes.
pblog="$STACK_DIR/.postboot-selftest.log"; cp "$bin/docker" "$T/docker.stub.bak"
printf '#!/usr/bin/env bash\necho "== Containers"\necho "  [FAIL] calibre-web: exited"\necho "  [FAIL] portal /healthz"\necho "RESULT: 7 passed, 2 failed"\nexit 2\n' > "$STACK_DIR/scripts/selftest.sh"
printf '#!/usr/bin/env bash\n{ echo "alert.sh: $1"; echo "$2"; } >> "%s"\n' "$T/pb-alert.log" > "$STACK_DIR/scripts/alert.sh"
chmod +x "$STACK_DIR/scripts/selftest.sh" "$STACK_DIR/scripts/alert.sh"; : > "$T/pb-alert.log"; rm -f "$pblog"
POSTBOOT_WAIT=0 bash "$pbs" > "$T/pb.out" 2>&1; rc=$?
expect '[ $rc = 2 ] && grep -q "RESULT: 7 passed, 2 failed" "$pblog" && grep -qE "^finished=.* exit=2$" "$pblog" && [ ! -e "$pblog.tmp" ]' "the wrapper exits with the self-test's failure count and records the result atomically (F.6)"
expect 'grep -q "RESULT: 7 passed, 2 failed" "$T/pb.out"' "and repeats it on stdout, so journalctl -u bookstack-postboot holds the full result as well (F.6)"
expect 'grep -q "post-reboot self-test FAILED" "$T/pb-alert.log" && grep -q "calibre-web: exited" "$T/pb-alert.log" && grep -q "portal /healthz" "$T/pb-alert.log"' "a non-zero exit reaches scripts/alert.sh naming WHICH checks failed, not just that something did (F.6)"
expect 'postboot_last | grep -q "2 check(s) FAILED" && postboot_banner | grep -q "Post-reboot self-test"' "the TUI reads that result back, and a failure is worth the first screen (F.6)"
expect 'declare -f step_selftest | grep -q postboot_last && declare -f main_menu | grep -q postboot_banner' "Operations -> Self-test shows the last automatic run and the main menu flags a failed one (F.6)"
printf '#!/usr/bin/env bash\necho "RESULT: 9 passed, 0 failed"\nexit 0\n' > "$STACK_DIR/scripts/selftest.sh"; chmod +x "$STACK_DIR/scripts/selftest.sh"
: > "$T/pb-alert.log"; POSTBOOT_WAIT=0 bash "$pbs" >/dev/null 2>&1; rc=$?
expect '[ $rc = 0 ] && [ ! -s "$T/pb-alert.log" ] && postboot_last | grep -q "all checks passed" && [ -z "$(postboot_banner)" ]' "a clean post-reboot self-test alerts nobody and leaves the first screen alone (F.6)"
cat > "$bin/docker" <<'EOS'
#!/usr/bin/env bash
case "$*" in
  "ps -q") echo c1;;
  "ps --format "*) printf '%s\n' ${PB_NAMES-caddy calibre-web audiobookshelf librarian shelfmark uptime-kuma};;
  *inspect*) echo "${PB_HEALTH:-healthy}";;
esac
exit 0
EOS
chmod +x "$bin/docker"
t0=$(date +%s); POSTBOOT_WAIT=60 POSTBOOT_SLEEP=5 bash "$pbs" >/dev/null 2>&1; t1=$(date +%s)
expect '[ $((t1-t0)) -lt 5 ]' "an already-healthy stack is tested at once instead of after a fixed sleep (F.6)"
t0=$(date +%s); PB_HEALTH=starting POSTBOOT_WAIT=3 POSTBOOT_SLEEP=1 bash "$pbs" >/dev/null 2>&1; t1=$(date +%s)
expect '[ $((t1-t0)) -ge 3 ] && grep -q "RESULT: 9 passed" "$pblog"' "a container still inside its healthcheck start period (CWA 120 s, ABS 60 s) is waited out, then tested anyway (F.6)"
# R4-A4: dockerd restarts the restart:unless-stopped containers one after another, and caddy and
# uptime-kuma carry no healthcheck at all. "something is running and nothing says 'starting'" is
# therefore already true in the window before calibre-web and audiobookshelf exist — the
# self-test then calls the containers that have not started yet FAILED, and the admin gets an
# alert after every healthy 04:30 reboot, which is the fastest way to teach them to ignore it.
expect 'grep -q "ps --format" "$pbs" && for c in caddy calibre-web audiobookshelf librarian shelfmark; do grep -q "$c" "$pbs" || exit 1; done' "the generated wrapper waits for the core containers BY NAME, not just for a non-empty docker ps (R4-A4)"
expect '! grep -E "^CORE=" "$pbs" | grep -qE "qbittorrent|ephemera|flaresolverr"' "and not for the optional ones, which are not always deployed (R4-A4)"
t0=$(date +%s); PB_NAMES="caddy uptime-kuma" POSTBOOT_WAIT=3 POSTBOOT_SLEEP=1 bash "$pbs" >/dev/null 2>&1; t1=$(date +%s)
expect '[ $((t1-t0)) -ge 3 ]' "a part-started stack (caddy up, calibre-web not yet) is waited out instead of being reported as a failed boot (R4-A4)"
t0=$(date +%s); PB_NAMES="" POSTBOOT_WAIT=2 POSTBOOT_SLEEP=1 bash "$pbs" >/dev/null 2>&1; t1=$(date +%s)
expect '[ $((t1-t0)) -ge 2 ] && grep -q "RESULT: 9 passed" "$pblog"' "and a Docker that never came up still falls through the deadline and is reported by the self-test itself (R4-A4)"
cp "$T/docker.stub.bak" "$bin/docker"; rm -f "$pblog"

echo "== the touched helpers behave under the script's own errexit"
# the suite runs with `set +e`; these helpers really execute with -euo pipefail, where a final
# `[ x = y ] && cmd` that is false aborts the caller
out=$( (set -euo pipefail
  render_caddyfile >/dev/null
  restart_shelfmark >/dev/null || true
  restart_shelfmark --end-sessions >/dev/null || true
  RESTIC_NO_RETRY_LOCK=1 restic_run cat config >/dev/null
  restic_run cat config >/dev/null
  envset ERRX 1; envdefault ERRX 2; valid_admin_hash "$(envget ADMIN_HASH)"
  torrents_on || true
  # the new helpers: each ends in a test or an && list that is false on a normal, boring stack
  stack_services >/dev/null
  compose_for authelia >/dev/null; compose_for caddy >/dev/null
  adv_rows lockout >/dev/null; adv_value LOCKOUT_FAILS 5 >/dev/null
  valid_ip 203.0.113.9 || true; valid_ip nope || true; caller_ip >/dev/null
  cli_err '{"ok": false, "error": "x"}' >/dev/null; cli_err 'not json at all' >/dev/null
  postboot_last >/dev/null || true; postboot_banner >/dev/null
  # every arm of these two is a test or an && list that is false on a boring stack (no dropbox
  # for the old name, no Authelia file, torrents off) — and a rename must survive all of it
  rename_user_artifacts nosuch-old nosuch-new
  authelia_rename_user nosuch-old nosuch-new || true
  # monitoring + FlareSolverr helpers: false-y on a stack with nothing enabled and Kuma never set up
  solver_on || true; compose_profiles >/dev/null; compose_for flaresolverr >/dev/null
  ensure_kuma_secrets; kuma_reboot_time >/dev/null; kuma_config >/dev/null
  envset KUMA_BOOTSTRAP_AT ""; monitoring_refresh
  echo "ERREXIT-OK") 2>&1 )
expect 'printf "%s" "$out" | grep -q "ERREXIT-OK"' "render_caddyfile / restart_shelfmark / restic_run / envset and the new helpers all survive set -euo pipefail"


# ---------------------------------------------------------------------------------------------
echo "== Metadata push: the host side (scripts/metadata-push.sh)"
# The one path that WRITES into the family's shared Calibre database. What reaches calibredb is
# checked here as argv, because that is the only thing that matters.
MP="$T/mp"; mkdir -p "$MP/bin" "$MP/stack/scripts"
printf "PUID='1000'\nPGID='1000'\n" > "$MP/stack/.env"
printf '#!/usr/bin/env bash\necho "ALERT $*" >> "%s/log"\n' "$MP" > "$MP/stack/scripts/alert.sh"
chmod +x "$MP/stack/scripts/alert.sh"
cat > "$MP/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MP/log"
case "$*" in
  *"admin_cli pushes pending"*)
    # a row carrying a forbidden field, as if written by hand: the script must still drop it
    echo '{"ok":true,"rows":[{"id":5,"calibre_id":42,"fields":{"title":"Moby-Dick","tags":"owner:mallory","series":"S"}}]}';;
  *"admin_cli pushes result"*) echo '{"ok":true,"status":"done"}';;
  *"calibredb list"*)
    n=$(cat "$MP/listcount" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/listcount"
    if [ "$n" -ge 2 ] && [ -n "${MP_TAG_CHANGES:-}" ]; then echo '[{"id":42,"tags":["owner:bob"]}]'
    else echo '[{"id":42,"tags":["owner:alice"]}]'; fi;;
  *"calibredb set_metadata"*) : ;;
esac
EOS
chmod +x "$MP/bin/docker"
mprun(){ : > "$MP/log"; rm -f "$MP/listcount"; MP="$MP" PATH="$MP/bin:$PATH" STACK_DIR="$MP/stack" bash "$REPO/scripts/metadata-push.sh" >/dev/null 2>&1; }
mprun; rc=$?
expect 'grep "calibredb set_metadata" "$MP/log" | grep -q -- "-u 1000:1000"' \
  "calibredb runs as PUID:PGID, never root (a root-owned -wal would lock Calibre-Web out of its own database)"
expect 'grep "calibredb set_metadata" "$MP/log" | grep -q "title:Moby-Dick" && grep "calibredb set_metadata" "$MP/log" | grep -q "series:S"' \
  "the allowed fields reach calibredb"
expect '! grep "calibredb set_metadata" "$MP/log" | grep -qi "tags"' \
  "a tags field in the queue NEVER reaches calibredb, even when the row carries one"
expect '[ "$(grep -c "calibredb list" "$MP/log")" = 2 ] && grep -q "pushes result 5 ok" "$MP/log" && [ "$rc" = 0 ]' \
  "the owner tag is read before AND after the write, and an unchanged tag reports success"
MP_TAG_CHANGES=1 mprun; rc=$?
expect 'grep -q "ALERT Bookstack: a metadata update CHANGED a book" "$MP/log" && grep -q "pushes result 5 fail" "$MP/log" && [ "$rc" != 0 ]' \
  "a write that CHANGED the owner tag raises a high alert and fails the push, it is never just logged"
expect 'grep -q "bookstack-metapush" "$REPO/bookstack.sh" && grep -q "logger -t bookstack-metapush" "$REPO/bookstack.sh"' \
  "the push job is installed as a cron entry whose output reaches the journal, not /dev/null"
expect 'grep -q "\*/2 \* \* \* \*" <(grep -A2 "write_cron bookstack-metapush" "$REPO/bookstack.sh") && grep -q "flock -n /run/lock/bookstack-metapush.lock" "$REPO/bookstack.sh"' \
  "every 2 minutes (a reader waits for the owner tag), under flock so a long conversion never overlaps the next run"
# owner tags: the L10 adoption and the family share (a SECOND owner, librarian/share.py)
cat > "$MP/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MP/log"
case "$*" in
  *"admin_cli pushes pending"*|*"admin_cli converts pending"*) echo '{"ok":true,"rows":[]}';;
  *"admin_cli tags pending"*) echo "$MP_TAGROWS";;
  *"admin_cli tags result"*) echo '{"ok":true,"status":"done"}';;
  *"calibredb list"*)
    n=$(cat "$MP/listcount" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/listcount"
    if [ "$n" -ge 2 ]; then echo "$MP_AFTER"; else echo "$MP_BEFORE"; fi;;
  *"calibredb set_metadata"*) : ;;
esac
EOS
chmod +x "$MP/bin/docker"
share_row='{"ok":true,"rows":[{"id":8,"calibre_id":50,"rid":3,"owner":"bob","share":1}]}'
MP_TAGROWS="$share_row" MP_BEFORE='[{"id":50,"tags":["Fiction","owner:alice"]}]' MP_AFTER='[{"id":50,"tags":["Fiction","owner:alice","owner:bob"]}]' mprun
expect 'grep "calibredb set_metadata" "$MP/log" | grep -q "tags:Fiction,owner:alice,owner:bob" && grep -q "tags result 8 ok" "$MP/log"' \
  "a family share ADDS bob next to alice (her tag and every other tag kept), read back and confirmed"
MP_TAGROWS="$share_row" MP_BEFORE='[{"id":50,"tags":["Fiction"]}]' MP_AFTER='[{"id":50,"tags":["Fiction"]}]' mprun
expect '! grep -q "calibredb set_metadata" "$MP/log" && grep -q "tags result 8 fail --reason refused: a family share, but the book has no owner yet" "$MP/log"' \
  "a share never adopts an UNTAGGED book (that is someone's import in progress)"
MP_TAGROWS="$share_row" MP_BEFORE='[{"id":50,"tags":["owner:alice","owner:bob"]}]' MP_AFTER='[{"id":50,"tags":["owner:alice","owner:bob"]}]' mprun
expect '! grep -q "calibredb set_metadata" "$MP/log" && grep -q "tags result 8 ok" "$MP/log"' \
  "bob already has it: nothing is written, the job is simply done"
MP_TAGROWS='{"ok":true,"rows":[{"id":9,"calibre_id":50,"rid":4,"owner":"bob","share":0}]}' MP_BEFORE='[{"id":50,"tags":["owner:alice"]}]' MP_AFTER='[{"id":50,"tags":["owner:alice"]}]' mprun
expect '! grep -q "calibredb set_metadata" "$MP/log" && grep -q "tags result 9 fail --reason refused: the book already has" "$MP/log"' \
  "an ordinary (non-share) tag job still refuses a book that already has an owner"
MP_TAGROWS="$share_row" MP_BEFORE='[{"id":50,"tags":["owner:alice"]}]' MP_AFTER='[{"id":50,"tags":["owner:bob"]}]' mprun
expect 'grep -q "ALERT Bookstack: adding an owner tag changed other tags" "$MP/log" && grep -q "tags result 8 fail" "$MP/log"' \
  "a share that would REMOVE alice (read-back differs) raises the alert and fails"
# 'Find a better copy': a staged EPUB swapped into the SAME Calibre book
cat > "$MP/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MP/log"
case "$*" in
  *"admin_cli pushes pending"*|*"admin_cli converts pending"*|*"admin_cli tags pending"*) echo '{"ok":true,"rows":[]}';;
  *"admin_cli replaces pending"*) echo "$MP_REPROWS";;
  *"admin_cli replaces result"*) echo '{"ok":true,"status":"done"}';;
  *"--fields tags"*) n=$(cat "$MP/tc" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/tc"
    if [ "$n" -ge 2 ]; then echo "$MP_AFTER"; else echo "$MP_BEFORE"; fi;;
  *"--fields formats"*) n=$(cat "$MP/fc" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/fc"
    if [ "$n" -ge 2 ]; then echo "$MP_FAFTER"; else echo "$MP_FBEFORE"; fi;;
esac
EOS
chmod +x "$MP/bin/docker"
rpdir="$MP/stack/library/staging/replace"; mkdir -p "$rpdir"
rprun(){ rm -f "$MP/tc" "$MP/fc"; mprun; }
export MP_REPROWS='{"ok":true,"rows":[{"id":8,"calibre_id":50,"fmt":"epub","rid":3,"owner":"alice"}]}'
export MP_BEFORE='[{"id":50,"tags":["Fiction","owner:alice","owner:bob"]}]' MP_AFTER='[{"id":50,"tags":["Fiction","owner:alice","owner:bob"]}]'
export MP_FBEFORE='[{"id":50,"formats":["/calibre-library/A/B (50)/B - A.epub","/calibre-library/A/B (50)/B - A.kepub","/calibre-library/A/B (50)/B - A.mobi"]}]'
export MP_FAFTER='[{"id":50,"formats":["/calibre-library/A/B (50)/B - A.epub"]}]'
echo better > "$rpdir/8.epub"; rprun
expect 'grep -q "docker cp $rpdir/8.epub calibre-web:/tmp/bookstack-replace-8.epub" "$MP/log" && grep "calibredb add_format" "$MP/log" | grep -q "add_format 50 /tmp/bookstack-replace-8.epub" && ! grep -q "dont-replace" "$MP/log"' \
  "the better copy goes into the SAME book (add_format replaces its EPUB)"
expect 'grep -q "remove_format 50 KEPUB" "$MP/log" && grep -q "remove_format 50 MOBI" "$MP/log" && ! grep -q "remove_format 50 EPUB" "$MP/log"' \
  "formats made from the old file (the Kobo's KEPUB, a kept MOBI) are removed, to be made again from the new one"
expect 'grep -q "replaces result 8 ok" "$MP/log" && [ ! -e "$rpdir/8.epub" ] && [ "$(grep -c "fields tags" "$MP/log")" = 2 ]' \
  "owners read before and after, the job reported done, the staged copy removed"
rprun
expect 'grep -q "replaces result 8 fail --reason refused: the staged EPUB is missing" "$MP/log" && ! grep -q "add_format" "$MP/log"' \
  "no staged file: refused, nothing written"
echo better > "$rpdir/8.epub"; MP_AFTER='[{"id":50,"tags":["Fiction","owner:alice"]}]' rprun
expect 'grep -q "ALERT Bookstack: replacing a book.s file CHANGED its tags" "$MP/log" && grep -q "replaces result 8 fail" "$MP/log" && [ -e "$rpdir/8.epub" ]' \
  "a swap that changed the owners raises the alert and fails (the staged copy is kept)"
MP_FAFTER="$MP_FBEFORE" rprun
expect 'grep -q "replaces result 8 fail --reason formats after the swap" "$MP/log"' \
  "old formats that would not go away: reported, not called done"
unset MP_REPROWS MP_BEFORE MP_AFTER MP_FBEFORE MP_FAFTER
# 'Remove from my library' (op=remove) and deleting books no reader has any more
cat > "$MP/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MP/log"
case "$*" in
  *"admin_cli pushes pending"*|*"admin_cli converts pending"*|*"admin_cli replaces pending"*) echo '{"ok":true,"rows":[]}';;
  *"admin_cli tags pending"*) echo "${MP_TAGROWS:-{\"ok\":true,\"rows\":[]\}}";;
  *"admin_cli releases due"*) echo "${MP_DUE:-{\"ok\":true,\"rows\":[]\}}";;
  *"admin_cli"*"result"*) echo '{"ok":true,"status":"done"}';;
  *"--fields tags"*) n=$(cat "$MP/tc" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/tc"
    if [ "$n" -ge 2 ]; then echo "$MP_AFTER"; else echo "$MP_BEFORE"; fi;;
  *"--fields title"*) n=$(cat "$MP/ec" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$MP/ec"
    if [ "$n" -ge 2 ] && [ -n "${MP_GONE_AFTER:-}" ]; then echo '[]'; else echo '[{"id":60,"title":"X"}]'; fi;;
esac
EOS
chmod +x "$MP/bin/docker"
rmrun(){ rm -f "$MP/tc" "$MP/ec"; mprun; }
MP_TAGROWS='{"ok":true,"rows":[{"id":11,"calibre_id":60,"rid":null,"owner":"alice","share":0,"op":"remove"}]}' \
  MP_BEFORE='[{"id":60,"tags":["Classics","owner:alice","owner:bob"]}]' MP_AFTER='[{"id":60,"tags":["Classics","owner:bob"]}]' rmrun
expect 'grep "calibredb set_metadata" "$MP/log" | grep -q "tags:Classics,owner:bob" && grep -q "tags result 11 ok" "$MP/log"' \
  "'Remove from my library' takes off only alice's tag: bob's and every other tag stay"
MP_TAGROWS='{"ok":true,"rows":[{"id":11,"calibre_id":60,"rid":null,"owner":"alice","share":0,"op":"remove"}]}' \
  MP_BEFORE='[{"id":60,"tags":["Classics","owner:alice","owner:bob"]}]' MP_AFTER='[{"id":60,"tags":["Classics"]}]' rmrun
expect 'grep -q "ALERT Bookstack: removing a reader.s tag changed other tags" "$MP/log" && grep -q "tags result 11 fail" "$MP/log"' \
  "a removal that took more than that reader's tag raises the alert and fails"
MP_DUE='{"ok":true,"rows":[{"calibre_id":60,"tags":[]}]}' MP_BEFORE='[{"id":60,"tags":["Classics"]}]' MP_AFTER='[{"id":60,"tags":["Classics"]}]' MP_GONE_AFTER=1 rmrun
expect 'grep -q "calibredb remove --permanent 60" "$MP/log" && grep -q "releases result 60 ok" "$MP/log"' \
  "a book no reader has any more is deleted from Calibre (permanently: no trash on a small disk) once due"
MP_DUE='{"ok":true,"rows":[{"calibre_id":60,"tags":[]}]}' MP_BEFORE='[{"id":60,"tags":["owner:bob"]}]' MP_AFTER='[{"id":60,"tags":["owner:bob"]}]' rmrun
expect '! grep -q "calibredb remove" "$MP/log" && grep -q "releases result 60 fail --reason refused: its owners are now" "$MP/log"' \
  "a book someone has again (bob got it meanwhile) is refused and kept"
expect 'grep -q "^docker image prune -f" "$REPO/scripts/disk-watch.sh" && grep -q "^apt-get clean" "$REPO/scripts/disk-watch.sh" && ! grep -qE "image prune.*-a|prune -a" "$REPO/scripts/disk-watch.sh"' \
  "the disk watchdog clears DANGLING images and apt's cache, never tagged images (Update keeps :prev for its rollback)"

# ---------------------------------------------------------------------------------------------
echo "== FlareSolverr: one shared solver for Shelfmark and Ephemera"
envset FLARESOLVERR_ENABLED false; envset EPHEMERA_ENABLED false; envset TORRENTS_ENABLED false
expect '! compose_profiles | grep -q solver' "neither Shelfmark nor Ephemera wants it: no solver profile, FlareSolverr does not run"
envset FLARESOLVERR_ENABLED true
expect 'compose_profiles | grep -qx solver && stack_services | grep -qx flaresolverr' "FLARESOLVERR_ENABLED alone turns the solver profile on and lists the service"
envset FLARESOLVERR_ENABLED false; envset EPHEMERA_ENABLED true
expect 'compose_profiles | grep -qx solver' "Ephemera alone turns it on too (it always needs FlareSolverr)"
envset EPHEMERA_ENABLED false
expect '[ "$(compose_for flaresolverr)" = compose ] && [ "$(compose_for ephemera)" = composeE ]' "FlareSolverr lives in docker-compose.yml now, so plain compose owns it"
reset "yes"; step_flaresolverr >/dev/null; rc=$?
expect '[ $rc = 0 ] && [ "$(envget FLARESOLVERR_ENABLED)" = true ] && seen "compose --profile solver up -d flaresolverr" && seen "up -d shelfmark" && seen "exec shelfmark curl -fs -m 5 http://flaresolverr:8191/health"' "turning it on starts it, RECREATES Shelfmark (restart would keep the old env) and proves Shelfmark reaches it by name"
expect '! seen "compose restart shelfmark"' "and never uses 'restart', which would leave Shelfmark on the old environment"
envset FLARESOLVERR_ENABLED false
( curl(){ case "$*" in *8191*) return 7;; esac; return 0; }
  reset "yes"; step_flaresolverr >/dev/null; echo "rc=$? env=$(envget FLARESOLVERR_ENABLED)" > "$T/fs.out" )
expect 'grep -q "rc=1 env=false" "$T/fs.out" && grep -F msgbox "$LOG" | grep -q "left on its built-in solver"' "a FlareSolverr that never answers is rolled back: Shelfmark stays on its own solver, and it says so"
( docker(){ echo "docker: $*" >> "$LOG"; case "$*" in *"exec shelfmark curl"*) return 1;; esac; return 0; }
  reset "yes"; step_flaresolverr >/dev/null; echo "rc=$?" > "$T/fs.out" )
expect 'grep -q "rc=1" "$T/fs.out" && grep -F msgbox "$LOG" | grep -q "Shelfmark cannot reach http://flaresolverr:8191"' "running but unreachable from inside Shelfmark is a failure, not a success message"
envset FLARESOLVERR_ENABLED true; envset EPHEMERA_ENABLED true
reset "yes"; step_flaresolverr >/dev/null
expect '[ "$(envget FLARESOLVERR_ENABLED)" = false ] && seen "up -d shelfmark" && ! seen "stop flaresolverr"' "turning it off for Shelfmark keeps it running while Ephemera needs it"
envset FLARESOLVERR_ENABLED true; envset EPHEMERA_ENABLED false
reset "yes"; step_flaresolverr >/dev/null
expect '[ "$(envget FLARESOLVERR_ENABLED)" = false ] && seen "compose stop flaresolverr"' "and stops it when nothing else uses it"
envset FLARESOLVERR_ENABLED true; envset EPHEMERA_ENABLED true
reset; step_ephemera_off >/dev/null
expect 'seen "stop ephemera" && ! seen "stop flaresolverr" && grep -F msgbox "$LOG" | grep -q "kept running: Shelfmark uses it"' "disabling Ephemera leaves FlareSolverr up while Shelfmark uses it"
envset FLARESOLVERR_ENABLED false; envset EPHEMERA_ENABLED false
expect 'declare -f menu_ops | grep -q step_flaresolverr' "the toggle is in the Operations menu"
for f in docker-compose.yml docker-compose.ephemera.yml; do :; done
expect 'grep -q "USING_EXTERNAL_BYPASSER=\${FLARESOLVERR_ENABLED:-false}" "$REPO/docker-compose.yml" && grep -q "EXT_BYPASSER_URL=http://flaresolverr:8191" "$REPO/docker-compose.yml"' "Shelfmark's external bypasser follows FLARESOLVERR_ENABLED (env wins over its Settings page)"
expect '! grep -A3 "^  flaresolverr:" "$REPO/docker-compose.ephemera.yml" | grep -q "image:" && grep -A6 "depends_on:" "$REPO/docker-compose.ephemera.yml" | grep -q "flaresolverr:" && grep -q "required: false" "$REPO/docker-compose.ephemera.yml"' "the Ephemera overlay no longer defines its own FlareSolverr, and depends on the shared one without requiring it"
expect '! grep -rq "only enable Ephemera on >= 8 GB\|Needs >= 8 GB\|enabled on < 8 GB" "$REPO/docker-compose.ephemera.yml" "$REPO/scripts/selftest.sh" "$REPO/bookstack.sh"' "the unmeasured '8 GB' claim is gone from the overlay, the self-test and the installer"

echo "== Shelfmark's own metadata-first search has a provider"
expect 'grep -q "OPENLIBRARY_ENABLED=true" "$REPO/docker-compose.yml"' "Open Library (keyless) is switched on: Shelfmark's default 'universal' mode answered 'No metadata provider configured' without it"
expect '! sed -n "/^  shelfmark:/,/^  uptime-kuma:/p" "$REPO/docker-compose.yml" | grep -qE "(HARDCOVER|GOOGLEBOOKS)_API_KEY="' "optional keys never reach Shelfmark as empty env values (an empty env var locks its own key field blank)"
expect 'sed -n "/^  shelfmark:/,/^  uptime-kuma:/p" "$REPO/docker-compose.yml" | grep -q "path: ./shelfmark/metadata.env"' "keys go through an optional env file the installer writes only when a key exists"

echo "== Metadata sources and your own catalogs"
envset HARDCOVER_API_KEY ""; envset GOOGLE_BOOKS_API_KEY ""
( metadata_key_ok(){ [ "$2" = good-key ]; }
  reset "good-key" "bad-key"; step_metadata_sources >/dev/null; echo "hc=$(envget HARDCOVER_API_KEY) gb=$(envget GOOGLE_BOOKS_API_KEY)" > "$T/md.out" )
expect 'grep -q "^hc=good-key gb=$" "$T/md.out" && grep -q "REFUSED by googleapis.com" "$LOG"' "a key is tested against its service before it is kept; a refused one is not saved"
mdf="$STACK_DIR/shelfmark/metadata.env"
expect 'grep -qx "HARDCOVER_ENABLED=true" "$mdf" && grep -qx "HARDCOVER_API_KEY=good-key" "$mdf" && ! grep -q GOOGLEBOOKS "$mdf"' "Shelfmark's key file holds only the keys that exist"
expect 'seen "up -d librarian shelfmark"' "both are recreated so the keys are live"
( metadata_key_ok(){ return 0; }; reset "-" ""; step_metadata_sources >/dev/null )
expect '[ -z "$(envget HARDCOVER_API_KEY)" ] && ! grep -q HARDCOVER "$mdf"' "'-' removes a key, from .env and from Shelfmark's file"
expect 'declare -f step_deploy | grep -q write_shelfmark_metadata_env && declare -f step_update | grep -q write_shelfmark_metadata_env' "Deploy and Update regenerate it from .env (.env is the only source of truth)"
CLI_ACT='{"ok": true, "rows": [{"id": "home", "source": "opds:home", "name": "Home", "url": "https://b.example/opds", "user": "me", "enabled": true, "legacy": false}], "detail": "OPDS feed answered with 3 entries"}'
reset "A" "home" "Home" "https://b.example/opds/{q}" "me" "catpass" "0"; step_catalogs >/dev/null
expect 'grep -q "admin_cli catalogs add home Home https://b.example/opds/{q} --user me --password-stdin" "$LOG" && grep -q "docker-stdin: catpass" "$LOG" && ! grep -F "docker: " "$LOG" | grep -q catpass' "Your catalogs: added through admin_cli, the password over stdin only"
reset "E" "BAD ID" "0"; step_catalogs >/dev/null
expect 'grep -F msgbox "$LOG" | grep -q "is not a catalog id" && ! grep -q "catalogs disable\|catalogs enable" "$LOG"' "an id that is not an id never reaches the python expression or the portal"
CLI_ACT='{"ok": true, "rows": [{"id": 4, "owner": "alice", "kind": "ebook", "title": "Emma", "author": "Jane Austen", "status": "looking", "checks": 1, "next_check": 0, "detail": "", "rid": null, "work_key": null}]}'
reset "ok" "4"; step_wanted >/dev/null
expect 'grep -q "admin_cli wanted cancel 4" "$LOG"' "Keep looking: the admin sees every reader's list and can cancel an entry"
CLI_ACT='{"ok": true}'
expect 'declare -f menu_library | grep -q step_catalogs && declare -f menu_library | grep -q step_wanted && declare -f menu_library | grep -q step_metadata_sources' "all three are in the Library menu"

echo "== Monitoring: Uptime Kuma is configured by bookstack, not by hand"
envset KUMA_USER ""; envset KUMA_PASS ""; for j in SELFTEST DISK METAPUSH CFIPS BACKUP; do envset "KUMA_PUSH_$j" ""; done
envset ADMIN_USER famadmin
ensure_kuma_secrets
kp1=$(envget KUMA_PASS); kt1=$(envget KUMA_PUSH_DISK)
expect '[ "$(envget KUMA_USER)" = famadmin ] && [[ "$kp1" =~ ^[A-Za-z0-9]{24}$ ]]' "Kuma's admin is the stack admin, with a generated 24-character password"
expect 'for j in SELFTEST DISK METAPUSH CFIPS BACKUP; do [[ "$(envget KUMA_PUSH_$j)" =~ ^[0-9a-f]{32}$ ]] || exit 1; done' "one 32-hex push token per scheduled job"
ensure_kuma_secrets
expect '[ "$(envget KUMA_PASS)" = "$kp1" ] && [ "$(envget KUMA_PUSH_DISK)" = "$kt1" ]' "a second run keeps them (the jobs and Kuma already hold these)"
# which push monitors exist follows what is SCHEDULED on this host
mkdir -p "$ETC/cron.d" "$ETC/systemd/system" "$ETC/apt/apt.conf.d"
rm -f "$ETC/cron.d/bookstack-disk" "$ETC/cron.d/bookstack-metapush" "$ETC/cron.d/bookstack-cfips" "$ETC/systemd/system/bookstack-selftest.timer"
touch "$ETC/cron.d/bookstack-disk" "$ETC/systemd/system/bookstack-selftest.timer"
printf 'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "03:10";\n' > "$ETC/apt/apt.conf.d/50unattended-upgrades"
envset TORRENTS_ENABLED true; envset AUTHELIA_ENABLED false; envset EPHEMERA_ENABLED true; envset FLARESOLVERR_ENABLED false
envset NOTIFY_WEBHOOK "https://ntfy.sh/bookstack-x"; envset SMTP_HOST ""; envset DOMAIN example.test
kc=$(RESTIC_ENV_PATH="$T/no-restic.env" kuma_config)
kq(){ printf '%s' "$kc" | python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
expect '[ "$(kq "sorted(d[\"push\"])")" = "['"'"'disk'"'"', '"'"'selftest'"'"']" ]' "push monitors only for jobs scheduled here (selftest timer + disk cron; no cfips/metapush cron yet)"
expect '[ "$(kq "d[\"features\"]")" = "{'"'"'torrents'"'"': True, '"'"'ephemera'"'"': True, '"'"'authelia'"'"': False, '"'"'flaresolverr'"'"': True}" ]' "features follow .env; Ephemera implies the FlareSolverr monitor"
expect '[ "$(kq "d[\"reboot_time\"]")" = 03:10 ] && [ "$(kq "d[\"password\"]")" = "$kp1" ] && [ "$(kq "d[\"notify\"][\"smtp\"]")" = None ]' "the reboot window follows unattended-upgrades' own time; no SMTP -> no e-mail channel"
envset SMTP_HOST smtp.example.test; envset SMTP_FROM lib@example.test; envset ADMIN_EMAIL me@example.test
kc=$(kuma_config)
expect '[ "$(kq "d[\"notify\"][\"smtp\"][\"host\"]")" = smtp.example.test ] && [ "$(kq "d[\"notify\"][\"to\"]")" = me@example.test ]' "SMTP configured -> Kuma mails the same admin address alert.sh uses"
envset SMTP_HOST ""; envset TORRENTS_ENABLED false; envset EPHEMERA_ENABLED false
# setup_monitoring against a stub compose: the bootstrap's answer drives the result
KB_OUT='{"ok": true, "setup": "created", "added": ["a","b"], "updated": [], "deleted": [], "notifications": ["bookstack: webhook"], "maintenance": "5 3 * * *", "monitors": 9}'
kbdocker(){ echo "docker: $*" >> "$LOG"
  case "$*" in *"run --rm -T kuma-bootstrap"*) cat > "$T/kb.stdin"; echo "Creating ..." >&2; echo "$KB_OUT"; return 0;; esac; return 0; }
( docker(){ kbdocker "$@"; }; reset; envset KUMA_BOOTSTRAP_AT ""; setup_monitoring; echo "rc=$? note=$MON_NOTE" > "$T/sm.out" )
expect 'grep -q "^rc=0 note=9 monitors at https://monitor.example.test (+2 ~0 -0 this run), alerts via: webhook" "$T/sm.out" && [ -n "$(envget KUMA_BOOTSTRAP_AT)" ]' "a good bootstrap is summarised in one line and stamped in .env"
expect 'grep -q "$kp1" "$T/kb.stdin" && ! grep -F "docker: " "$LOG" | grep -qF "$kp1"' "the Kuma password travels on stdin, never on a command line"
expect 'seen "compose up -d uptime-kuma"' "Kuma is started first (the bootstrap needs it answering)"
KB_OUT='{"ok": true, "added": [], "updated": [], "deleted": [], "notifications": [], "monitors": 9}'
( docker(){ kbdocker "$@"; }; reset; setup_monitoring; echo "rc=$? note=$MON_NOTE" > "$T/sm.out" )
expect 'grep -q "NO alert channel" "$T/sm.out"' "no webhook and no SMTP: it says Kuma can show problems but tell nobody"
KB_OUT='{"ok": false, "code": "credentials", "error": "Uptime Kuma refused the login"}'
( docker(){ kbdocker "$@"; }; reset; setup_monitoring; echo "rc=$? note=$MON_NOTE" > "$T/sm.out" )
expect 'grep -q "^rc=2 note=Kuma was NOT configured: Uptime Kuma refused the login" "$T/sm.out"' "an account that is not ours is exit 2, so the menu can ask for it"
KB_OUT='not json at all'
( docker(){ kbdocker "$@"; }; reset; setup_monitoring; echo "rc=$?" > "$T/sm.out" )
expect 'grep -q "^rc=1" "$T/sm.out"' "a bootstrap that answers garbage is a failure, never a success"
# the menu entry: credentials refused -> ask for the real account -> retry
printf '0' > "$T/kbn"
kbdocker2(){ echo "docker: $*" >> "$LOG"
  case "$*" in *"run --rm -T kuma-bootstrap"*) cat > /dev/null; n=$(cat "$T/kbn"); echo $((n+1)) > "$T/kbn"
    if [ "$n" = 0 ]; then echo '{"ok": false, "code": "credentials", "error": "refused"}'; else echo '{"ok": true, "added": [], "updated": [], "deleted": [], "notifications": ["bookstack: webhook"], "monitors": 9}'; fi;; esac; return 0; }
( docker(){ kbdocker2 "$@"; }; reset "yes" "kuma-owner" "their-own-pass" "http://not-https.example/x"; step_monitoring >/dev/null; echo "rc=$?" > "$T/sm.out" )
expect 'grep -q "^rc=0" "$T/sm.out" && [ "$(envget KUMA_USER)" = kuma-owner ] && [ "$(envget KUMA_PASS)" = their-own-pass ] && [ "$(cat "$T/kbn")" = 2 ]' "Operations -> Monitoring adopts an existing Kuma account and retries"
expect '[ -z "$(envget HEALTH_PING_URL)" ] && grep -F msgbox "$LOG" | grep -q "is not an https:// URL"' "a non-https external check URL is refused"
expect 'grep -q "password: their-own-pass" "$LOG" && grep -q "cannot tell you the whole VPS is down" "$LOG"' "the result screen shows the login and says what Kuma cannot see"
envset KUMA_USER famadmin; envset KUMA_PASS "$kp1"
( docker(){ kbdocker2 "$@"; }; echo 1 > "$T/kbn"; reset "https://hc-ping.example/abc"; step_monitoring >/dev/null )
expect '[ "$(envget HEALTH_PING_URL)" = https://hc-ping.example/abc ]' "and stores an https:// one (HEALTH_PING_URL had no prompt anywhere before)"
envset HEALTH_PING_URL ""
expect 'declare -f step_deploy | grep -q "setup_monitoring || true" && declare -f step_deploy | grep -q install_selftest_timer' "Deploy sets monitoring up as its last step, and a Kuma problem cannot fail the Deploy"
expect 'declare -f step_update | grep -q setup_monitoring && declare -f step_torrents | grep -q monitoring_refresh && declare -f step_authelia | grep -q monitoring_refresh && declare -f step_ephemera | grep -q monitoring_refresh' "Update and every feature toggle reconcile the monitors"
envset KUMA_BOOTSTRAP_AT ""; reset; monitoring_refresh
expect '! seen "kuma-bootstrap"' "a refresh does nothing before monitoring was ever set up"

echo "== Monitoring: the hourly self-test and its alert path"
u="$ETC/systemd/system"; rm -f "$u/bookstack-alert@.service"
install_postboot_unit
expect '[ -s "$u/bookstack-alert@.service" ]' "the OnFailure= target exists without backups configured (it used to be written only by the Backups step)"
reset; install_selftest_timer
hw="$ETC/bookstack/selftest-hourly.sh"
expect 'grep -q "^OnCalendar=hourly" "$u/bookstack-selftest.timer" && grep -q "^OnFailure=bookstack-alert@selftest.service" "$u/bookstack-selftest.service" && grep -qF "ExecStart=$hw" "$u/bookstack-selftest.service"' "an hourly timer with the wrapper and an OnFailure backstop"
expect 'seen "systemctl: enable --now bookstack-selftest.timer" && bash -n "$hw"' "enabled now (unlike the post-boot unit), and the wrapper is valid bash"
HW="$T/hw"; mkdir -p "$HW/stack/scripts"
sed "s#^STACK_DIR=.*#STACK_DIR=$HW/stack#" "$hw" > "$HW/w.sh"
printf '#!/usr/bin/env bash\necho "scheduled=$SELFTEST_SCHEDULED"\nif [ "${ST_RC:-0}" != 0 ]; then echo "  [FAIL] calibre-web: exited"; echo "  [FAIL] portal /healthz"; fi\nexit ${ST_RC:-0}\n' > "$HW/stack/scripts/selftest.sh"
printf '#!/usr/bin/env bash\necho "push $*" >> "%s/log"\nexit ${KP_RC:-0}\n' "$HW" > "$HW/stack/scripts/kuma-push.sh"
printf '#!/usr/bin/env bash\necho "alert $1" >> "%s/log"\n' "$HW" > "$HW/stack/scripts/alert.sh"
chmod +x "$HW/stack/scripts/"*.sh
hwrun(){ : > "$HW/log"; SELFTEST_STATE="$HW/state" SELFTEST_LOCK="$HW/lock" ST_RC="$1" KP_RC="$2" bash "$HW/w.sh" >/dev/null 2>&1; }
rm -f "$HW/state"; hwrun 2 0; rc=$?
expect '[ $rc = 0 ] && grep -q "^push selftest down 2 check(s) failed: calibre-web: exited" "$HW/log" && ! grep -q "^alert" "$HW/log"' "a failure goes to Kuma (which alerts on the change) and NOT also to alert.sh: one message, not two"
expect 'grep -q "scheduled=1" "$HW/stack/.selftest-hourly.log" && grep -qE "^finished=.* exit=2$" "$HW/stack/.selftest-hourly.log"' "it runs the self-test in scheduled mode and records the result"
rm -f "$HW/state"; hwrun 2 1
expect 'grep -q "^alert Bookstack: hourly self-test FAILED" "$HW/log"' "Kuma unreachable: the failure comes through alert.sh instead"
hwrun 2 1
expect '! grep -q "^alert" "$HW/log"' "and a failure that persists is not re-sent every hour"
hwrun 0 1
expect 'grep -q "^alert Bookstack: hourly self-test passes again" "$HW/log"' "recovery is announced on the same fallback path"
hwrun 0 0
expect 'grep -q "^push selftest up all checks passed" "$HW/log" && ! grep -q "^alert" "$HW/log"' "a pass is a quiet heartbeat"
expect 'grep -q "kuma-push.sh\" selftest" "$pbs"' "the post-reboot run reports to the same Kuma monitor"

echo "== Monitoring: scripts/kuma-push.sh"
KP="$T/kp"; mkdir -p "$KP/bin" "$KP/stack"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" > "%s/args"\necho "${KP_ANS}"\n' "$KP" > "$KP/bin/curl"; chmod +x "$KP/bin/curl"
kprun(){ rm -f "$KP/args"; PATH="$KP/bin:$PATH" STACK_DIR="$KP/stack" KP_ANS="$2" bash "$REPO/scripts/kuma-push.sh" $1; }
printf "KUMA_PUSH_DISK='abc123'\n" > "$KP/stack/.env"
kprun "backup up" '{"ok":true}'; rc=$?
expect '[ $rc = 0 ] && [ ! -e "$KP/args" ]' "a job with no token (monitoring not set up) is a silent no-op"
kprun "disk down" '{"ok":true}'; rc=$?
expect '[ $rc = 0 ] && grep -qx "http://127.0.0.1:3001/api/push/abc123" "$KP/args" && grep -qx "status=down" "$KP/args" && grep -qx -- "--data-urlencode" "$KP/args"' "a token pushes to its monitor, message url-encoded"
kprun "disk up" '{"ok":false,"msg":"Monitor not found or not active."}'; rc=$?
expect '[ $rc = 1 ]' "Kuma refusing the beat is exit 1 (the hourly wrapper then falls back to alert.sh)"
kprun "nosuchjob up" ''; rc=$?
expect '[ $rc = 2 ]' "an unknown job name is refused"
expect 'grep -q "kuma-push.sh}\" disk up" "$REPO/scripts/disk-watch.sh" && grep -q "kuma-push.sh}\" cfips up" "$REPO/scripts/cf-ips.sh" && grep -q "kuma-push.sh}\" backup up" "$REPO/scripts/backup.sh" && grep -q "\"metapush\"" "$REPO/scripts/metadata-push.sh"' "disk watchdog, Cloudflare refresh, backup and metadata push each report in"

echo "== Monitoring: the self-test's scheduled mode"
st="$REPO/scripts/selftest.sh"
expect 'awk "/factory admin\/admin123 must be dead/{f=1} f&&/SCHEDULED\" != 1/{print; exit}" "$st" | grep -q SCHEDULED' "the hourly run skips the factory-password login (Calibre-Web's 40-per-day limit counts it against the admin's name)"
expect 'grep -q "restic --no-lock snapshots" "$st" && grep -q "if \[ \"\$SCHEDULED\" = 1 \]; then :" "$st"' "the restic listing takes no lock, and the hourly run skips it"
expect 'grep -q "probe_user=\"__bookstack_selftest_\$(date +%s)_\$\$__\"" "$st"' "the Shelfmark probe name changes every run, so 24 runs a day never reach its 10-failure lockout"
expect 'grep -q "u \"\$ku:\$kp\" http://127.0.0.1:3001/metrics" "$st"' "the self-test reads Kuma's own view (/metrics) instead of trusting that a setup once ran"

echo "== Metadata push, second pass: L10 owner tags (never moves a book between readers)"
TP="$T/tp"; mkdir -p "$TP/bin" "$TP/stack/scripts"
printf "PUID='1000'\nPGID='1000'\n" > "$TP/stack/.env"
printf '#!/usr/bin/env bash\necho "ALERT $*" >> "%s/log"\n' "$TP" > "$TP/stack/scripts/alert.sh"; chmod +x "$TP/stack/scripts/alert.sh"
cat > "$TP/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$TP/log"
case "$*" in
  *"admin_cli pushes pending"*) echo '{"ok":true,"rows":[]}';;
  *"admin_cli tags pending"*) echo '{"ok":true,"rows":[{"id":9,"calibre_id":77,"rid":3,"owner":"alice"}]}';;
  *"admin_cli tags result"*) echo '{"ok":true,"status":"done"}';;
  *"calibredb list"*)
    # what the real CWA image does as uid 1000 without HOME: a warning on STDOUT before the JSON
    [ "${TP_NOHOME:-0}" = 1 ] && case "$*" in *"HOME=/tmp"*) ;; *) echo "No write access to /root/.config/calibre using a temporary dir instead";; esac
    echo "No write access to /root/.config/calibre using a temporary dir instead"
    n=$(cat "$TP/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$TP/n"
    case "${TP_CASE:-clean}" in
      clean)  [ "$n" -ge 2 ] && echo '[{"id":77,"tags":["Classics","owner:alice"]}]' || echo '[{"id":77,"tags":["Classics"]}]';;
      owned)  echo '[{"id":77,"tags":["owner:bob"]}]';;
      clobber) [ "$n" -ge 2 ] && echo '[{"id":77,"tags":["owner:alice"]}]' || echo '[{"id":77,"tags":["Classics"]}]';;
    esac;;
  *"calibredb set_metadata"*) : ;;
esac
EOS
chmod +x "$TP/bin/docker"
tprun(){ : > "$TP/log"; rm -f "$TP/n"; TP="$TP" TP_CASE="$1" PATH="$TP/bin:$PATH" STACK_DIR="$TP/stack" bash "$REPO/scripts/metadata-push.sh" >/dev/null 2>&1; }
tprun clean
expect 'grep -q -- "-e HOME=/tmp calibre-web" "$TP/log"' "calibredb gets a writable HOME (as uid 1000 it otherwise prints a warning on stdout, measured on the real image)"
expect 'grep "calibredb set_metadata 77" "$TP/log" | grep -q -- "--field tags:Classics,owner:alice" && grep -q "tags result 9 ok" "$TP/log"' \
  "the owner tag is ADDED to the existing tags (calibredb replaces the list, so the old tags are written back with it)"
tprun owned
expect '! grep -q "calibredb set_metadata" "$TP/log" && grep "tags result 9 fail" "$TP/log" | grep -q "refused: the book already has"' \
  "a book that already has an owner is refused, never re-tagged (that would move it to another reader)"
tprun clobber
expect 'grep -q "ALERT Bookstack: adding an owner tag changed other tags" "$TP/log" && grep -q "tags result 9 fail" "$TP/log"' \
  "any other tag changing on the way is a high alert and a failure, never success"

echo "== Metadata push: descriptions/covers (fill-only) and on-demand conversions"
CV="$T/cv"; mkdir -p "$CV/bin" "$CV/stack/scripts"
printf "PUID='1000'\nPGID='1000'\n" > "$CV/stack/.env"
printf '#!/usr/bin/env bash\necho "ALERT $*" >> "%s/log"\n' "$CV" > "$CV/stack/scripts/alert.sh"; chmod +x "$CV/stack/scripts/alert.sh"
cat > "$CV/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$CV/log"
case "$*" in
  *"admin_cli pushes pending"*) echo '{"ok":true,"rows":[{"id":1,"calibre_id":5,"fields":{"comments":"A comedy.","publisher":"Penguin","cover_url":"https://evil.example/x.jpg"}}]}';;
  *"admin_cli tags pending"*) echo '{"ok":true,"rows":[]}';;
  *"admin_cli converts pending"*) echo '{"ok":true,"rows":[{"id":7,"calibre_id":5,"owner":"alice","src_fmt":"epub","dst_fmt":"azw3","src_path":"Jane Austen/Emma (5)/Emma - Jane Austen.epub"},{"id":8,"calibre_id":5,"owner":"alice","src_fmt":"epub","dst_fmt":"pdf","src_path":"../../etc/passwd"}]}';;
  *"admin_cli"*) echo '{"ok":true,"status":"done"}';;
  *"calibredb list"*) echo '[{"id":5,"tags":["owner:alice"]}]';;
  *) : ;;
esac
EOS
chmod +x "$CV/bin/docker"
: > "$CV/log"; CV="$CV" PATH="$CV/bin:$PATH" STACK_DIR="$CV/stack" bash "$REPO/scripts/metadata-push.sh" >/dev/null 2>&1
expect 'grep "calibredb set_metadata 5" "$CV/log" | grep -q -- "--field comments:A comedy." && grep "calibredb set_metadata 5" "$CV/log" | grep -q -- "--field publisher:Penguin"' "descriptions and publishers reach Calibre (fill-only fields decided by the portal)"
expect '! grep -q "cover:" "$CV/log"' "a cover from a host that is not a provider's image host is never fetched or set"
expect 'grep -q "ebook-convert /calibre-library/Jane Austen/Emma (5)/Emma - Jane Austen.epub /tmp/bookstack-convert-7.azw3" "$CV/log" && grep -q "calibredb add_format --dont-replace 5 /tmp/bookstack-convert-7.azw3" "$CV/log"' "a conversion runs Calibre's ebook-convert in the CWA container and adds the result to THE SAME book"
expect 'grep -q -- "-u 1000:1000 -e HOME=/tmp calibre-web /app/calibre/ebook-convert" "$CV/log"' "as the library user, with a writable HOME"
expect '! grep -q "etc/passwd" "$CV/log" || ! grep "ebook-convert" "$CV/log" | grep -q "etc/passwd"' "a job whose path leaves the library is refused before anything runs"
expect 'grep -q "converts result 8 fail --reason refused" "$CV/log"' "and reported as refused"

echo "== L19: host hardening extras"
write_sysctl >/dev/null 2>&1; write_journald >/dev/null 2>&1
for k in kernel.kptr_restrict kernel.dmesg_restrict kernel.yama.ptrace_scope kernel.unprivileged_bpf_disabled fs.protected_regular; do
  grep -q "^$k" "$ETC/sysctl.d/90-bookstack.conf" || bad "sysctl lacks $k"; done
ok "kernel hardening sysctls are written"
expect 'grep -q "^Storage=persistent" "$ETC/systemd/journald.conf.d/90-bookstack.conf" && grep -q "^SystemMaxUse=200M" "$ETC/systemd/journald.conf.d/90-bookstack.conf"' "the journal survives reboots, capped at 200 MB"
expect 'declare -f harden_ssh | grep -q "AllowTcpForwarding no" && declare -f harden_ssh | grep -q "LoginGraceTime 20" && ! declare -f harden_ssh | grep -q "^AllowUsers"' "sshd gets the extra hardening, and no AllowUsers that could lock out a provider's login account"
expect '! grep -q "tailscale.com/install.sh | sh" "$REPO/bookstack.sh" && declare -f install_tailscale_apt | grep -q "pkgs.tailscale.com/stable"' "Tailscale comes from its signed apt repository, not curl | sh"

echo "== L09: certificate and token expiry watch (scripts/cert-watch.sh)"
CW="$T/cw"; mkdir -p "$CW/certs/acme/books.example.test" "$CW/bin"
openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=books.example.test -days 5 -keyout "$CW/k.pem" -out "$CW/certs/acme/books.example.test/books.example.test.crt" >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=ca -days 400 -keyout "$CW/k2.pem" -out "$CW/ca.pem" >/dev/null 2>&1
printf '#!/usr/bin/env bash\necho "ALERT $1 $2" >> "%s/log"\n' "$CW" > "$CW/alert.sh"; chmod +x "$CW/alert.sh"
printf "CF_API_TOKEN='tok'\n" > "$CW/.env"
cat > "$CW/bin/curl" <<'EOS'
#!/usr/bin/env bash
printf '%s\n' "$CW_TOKEN_ANSWER"
EOS
chmod +x "$CW/bin/curl"
cwrun(){ : > "$CW/log"; PATH="$CW/bin:$PATH" STACK_DIR="$CW" CERT_DIR="$CW/certs" CERT_CA="$CW/ca.pem" CERT_ALERT="$CW/alert.sh" CW_TOKEN_ANSWER="${1:-"{\"result\":{\"status\":\"active\"}}"}" bash "$REPO/scripts/cert-watch.sh" >/dev/null 2>&1; }
cwrun; rc=$?
expect '[ $rc = 1 ] && grep -q "certificate for books.example.test expires in [0-9] days" "$CW/log" && grep -q "renewal is failing" "$CW/log"' "a certificate under 14 days is an alert: Caddy's renewal has been failing"
rm -rf "$CW/certs/acme"; cwrun '{"result":{"status":"disabled"}}'
expect 'grep -q "Cloudflare API token is .disabled." "$CW/log"' "a Cloudflare token that is no longer active is an alert"
cwrun '{"result":{"status":"active"}}'; rc=$?
expect '[ $rc = 0 ] && [ ! -s "$CW/log" ]' "all fine: silent, exit 0"
openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=leaf -days 20 -keyout "$CW/k3.pem" -out "$CW/aop.pem" >/dev/null 2>&1
printf "CF_API_TOKEN='tok'\nAOP_MODE=zone\n" > "$CW/.env"; : > "$CW/log"
PATH="$CW/bin:$PATH" STACK_DIR="$CW" CERT_DIR="$CW/certs" CERT_CA="$CW/ca.pem" CERT_AOP="$CW/aop.pem" CERT_ALERT="$CW/alert.sh" CW_TOKEN_ANSWER='{"result":{"status":"active"}}' bash "$REPO/scripts/cert-watch.sh" >/dev/null 2>&1; rc=$?
expect '[ $rc = 1 ] && grep -q "origin-pull certificate expires in [0-9]* days" "$CW/log" && grep -q "Origin lock" "$CW/log"' "L14: this zone's own origin-pull certificate is watched too (every public site fails when it lapses)"
: > "$CW/log"; PATH="$CW/bin:$PATH" STACK_DIR="$CW" CERT_DIR="$CW/certs" CERT_CA="$CW/ca.pem" CERT_AOP="$CW/nope.pem" CERT_ALERT="$CW/alert.sh" CW_TOKEN_ANSWER='{"result":{"status":"active"}}' bash "$REPO/scripts/cert-watch.sh" >/dev/null 2>&1
expect 'grep -q "aop.*missing\|nope.pem is missing" "$CW/log"' "and a zone lock whose certificate file is gone is an alert"
printf "CF_API_TOKEN='tok'\n" > "$CW/.env"
expect 'declare -f install_disk_watch | grep -q install_cert_watch' "installed on Deploy (daily cron, output to the journal)"

echo "== L06: update notices (scripts/update-check.sh)"
UC="$T/uc"; mkdir -p "$UC"; printf "IMG_CWA='crocodilestick/calibre-web-automated:v4.0.6'\nIMG_KUMA='louislam/uptime-kuma:1'\n" > "$UC/.env"
printf '#!/usr/bin/env bash\necho "ALERT $1" >> "%s/log"\n' "$UC" > "$UC/alert.sh"; chmod +x "$UC/alert.sh"
: > "$UC/log"; ( export https_proxy=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9; STACK_DIR="$UC" UPDATE_STATE="$UC/state" UPDATE_ALERT="$UC/alert.sh" bash "$REPO/scripts/update-check.sh" > "$UC/out" 2>&1 ); rc=$?
expect '[ $rc = 0 ] && grep -q "nothing new" "$UC/out" && [ ! -s "$UC/log" ]' "no registry reachable: a quiet 'nothing new', never a false alarm or a failed job"
expect 'grep -q "floating tags" "$REPO/scripts/update-check.sh" && ! grep -q "docker pull\|compose up\|compose pull" "$REPO/scripts/update-check.sh"' "it only reports: floating tags are left alone and nothing is pulled or restarted"
expect 'declare -f install_disk_watch | grep -q install_update_check' "installed on Deploy (weekly, output to the journal)"

echo "== L17: login bot check and the 2FA nudge"
( curl(){ echo '{"success":false,"error-codes":["invalid-input-secret"]}'; }; reset "0x4AAA" "wrong-secret"; step_turnstile >/dev/null )
expect '[ -z "$(envget TURNSTILE_SITEKEY)" ] && grep -F msgbox "$LOG" | grep -q "secret key is wrong"' "a wrong Turnstile secret is caught by asking Cloudflare, and nothing is saved"
( curl(){ echo '{"success":false,"error-codes":["invalid-input-response"]}'; }; reset "0x4AAA" "good-secret"; step_turnstile >/dev/null )
expect '[ "$(envget TURNSTILE_SITEKEY)" = 0x4AAA ] && [ "$(envget TURNSTILE_SECRET)" = good-secret ] && seen "up -d librarian"' "a real secret is saved and the portal recreated"
envset TURNSTILE_SITEKEY ""; envset TURNSTILE_SECRET ""
expect 'declare -f step_quick | grep -q step_authelia && grep -q "no second factor in front of" "$REPO/scripts/selftest.sh"' "Quick install offers SSO + 2FA, and the self-test warns while there is none"

echo "== L18: large uploads over Tailscale"
expect 'grep -q "^upload.@@DOMAIN@@ {" "$REPO/caddy/Caddyfile.template" && sed -n "/^upload.@@DOMAIN@@ {/,/^}/p" "$REPO/caddy/Caddyfile.template" | grep -q "import tailnet_only" && sed -n "/^upload.@@DOMAIN@@ {/,/^}/p" "$REPO/caddy/Caddyfile.template" | grep -q "max_size 2GB"' "upload. is a Tailscale-only site with a 2 GB body limit"
expect 'grep -q "request_header -X-Bookstack-Upload" "$REPO/caddy/Caddyfile.template" && sed -n "/^upload.@@DOMAIN@@ {/,/^}/p" "$REPO/caddy/Caddyfile.template" | grep -q "header_up X-Bookstack-Upload tailnet"' "the marker is stripped from every client and set only by that site"
expect 'grep -q "private=\"monitor upload\"" "$REPO/bookstack.sh"' "its DNS record points at the tailnet address (grey cloud), like the other admin sites"

echo "== L02: qBittorrent gets a real Web UI password before first start"
envset QBIT_PASS ""; qbt_seed_config >/dev/null 2>&1
qc="$STACK_DIR/qbt/config/qBittorrent/qBittorrent.conf"
expect '[[ "$(envget QBIT_PASS)" =~ ^[A-Za-z0-9]{20}$ ]] && grep -q "^WebUI\\\\Username=admin" "$qc" && grep -qE "^WebUI\\\\Password_PBKDF2=\"@ByteArray\([A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+\)\"" "$qc"' "a generated password, stored in .env; only its PBKDF2 hash in qBittorrent's own format"
qp1=$(envget QBIT_PASS); qbt_seed_config >/dev/null 2>&1
expect '[ "$(envget QBIT_PASS)" = "$qp1" ] && [ "$(grep -c "^\[Preferences\]" "$qc")" = 1 ] && [ "$(grep -c "Password_PBKDF2" "$qc")" = 1 ] && grep -q "TempPathEnabled=true" "$qc"' "re-seeding keeps the password and edits the file in place (one section, one hash)"
expect 'python3 - "$qc" "$qp1" <<"PYV"
import sys, re, base64, hashlib
line = [l for l in open(sys.argv[1]) if "Password_PBKDF2" in l][0]
salt, key = re.search(r"ByteArray\(([^:]+):([^)]+)\)", line).groups()
sys.exit(0 if base64.b64decode(key) == hashlib.pbkdf2_hmac("sha512", sys.argv[2].encode(), base64.b64decode(salt), 100000, 64) else 1)
PYV' "the stored hash really is that password (PBKDF2-SHA512, 100000 rounds)"

echo "== L01: container isolation"
python3 - "$REPO" <<'PYN' && ok "every bridge service sits on a network of its own; Shelfmark shares one with FlareSolverr only, Ephemera too, never with each other (L01)" || bad "compose networks (L01)"
import re, sys
r = sys.argv[1]
def svc(path, name):
    s = open(f"{r}/{path}").read()
    m = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [a-z-]+:\n|^[a-z]|\Z)", s)
    return m.group(1) if m else ""
def nets(body):
    m = re.search(r"networks: \{ ([a-z]+): \{\} \}", body)
    return m.group(1) if m else None
want = {("docker-compose.yml", "calibre-web"): "cwa", ("docker-compose.yml", "audiobookshelf"): "abs",
        ("docker-compose.yml", "qbittorrent"): "dl", ("docker-compose.yml", "shelfmark"): "fetch",
        ("docker-compose.yml", "flaresolverr"): "fetch", ("docker-compose.authelia.yml", "authelia"): "auth",
        ("docker-compose.ephemera.yml", "ephemera"): "eph"}
bad = [f"{n}: {nets(svc(p, n))} != {w}" for (p, n), w in want.items() if nets(svc(p, n)) != w]
if "flaresolverr:\n    networks: { eph: {} }" not in open(f"{r}/docker-compose.ephemera.yml").read():
    bad.append("flaresolverr does not join eph in the Ephemera overlay")
print("\n".join(bad)); sys.exit(1 if bad else 0)
PYN
expect 'for sv in calibre-web shelfmark qbittorrent; do python3 -c "import re,sys; s=open(sys.argv[1]).read(); b=re.search(r\"(?ms)^  \"+sys.argv[2]+r\":\\n(.*?)(?=^  [a-z-]+:\\n)\", s).group(1); sys.exit(0 if \"cap_drop: [ALL]\" in b and \"cap_add: [CHOWN, SETUID, SETGID, DAC_OVERRIDE, FOWNER]\" in b else 1)" "$REPO/docker-compose.yml" "$sv" || exit 1; done' "the root-starting images keep only the five capabilities their entrypoints need (L01)"
expect 'grep -A8 "^  audiobookshelf:" "$REPO/docker-compose.yml" | grep -q "cap_drop: \[ALL\]" && grep -A8 "container_name: flaresolverr" "$REPO/docker-compose.yml" | grep -q "cap_drop: \[ALL\]"' "Audiobookshelf and FlareSolverr run with no capabilities at all (L01)"

echo "== L08: the canary journey (scripts/synthetic.py on a timer)"
ce="$T/etc/bookstack/canary.env"; rm -f "$ce" "$T/etc/systemd/system/bookstack-canary."*
reset "yes" "<blank>" "no"; step_canary; rc=$?
expect '[ $rc = 0 ] && grep -q "^CANARY_A=canary-a$" "$ce" && grep -q "^CANARY_B=canary-b$" "$ce" && grep -qE "^CANARY_A_PW=[A-Za-z0-9]{24}$" "$ce" && grep -qE "^CANARY_B_PW=[A-Za-z0-9]{24}$" "$ce" && [ "$(stat -c %a "$ce" 2>/dev/null || stat -f %Lp "$ce")" = 600 ]' "two canary accounts with generated passwords, kept in /etc/bookstack/canary.env (0600)"
expect '[ "$(grep "^CANARY_A_PW=" "$ce" | cut -d= -f2)" != "$(grep "^CANARY_B_PW=" "$ce" | cut -d= -f2)" ] && ! grep -q "CANARY_A_PW\|CANARY_B_PW" "$ENV_FILE"' "different passwords, and never in .env (which the portal container reads)"
expect 'grep -F "docker: " "$LOG" | grep -q "exec -i librarian python -m cwa add-user canary-a --password-stdin --no-abs" && grep -F "docker: " "$LOG" | grep -q "add-user canary-b --password-stdin --no-abs" && ! grep -F "docker: " "$LOG" | grep -q "add-user canary-a --password [^-]"' "created through the portal's CLI, password on stdin, no Audiobookshelf account"
expect '[ "$(envget CANARY_USERS)" = "canary-a,canary-b" ] && grep -q "CANARY_USERS=\${CANARY_USERS:-}" "$REPO/docker-compose.yml" && seen "docker: compose up -d librarian"' "CANARY_USERS reaches the portal (it hides them), which is restarted"
cu="$T/etc/systemd/system/bookstack-canary"
expect 'grep -q "^OnCalendar=\*-\*-\* 06,18:20:00" "$cu.timer" && grep -qF "ExecStart=/usr/bin/python3 $STACK_DIR/scripts/synthetic.py" "$cu.service" && grep -qF "Environment=CANARY_ENV=$ce" "$cu.service" && seen "systemctl: enable --now bookstack-canary.timer"' "twice a day, 06:20 and 18:20, running scripts/synthetic.py with the canary credentials"
expect '[ -n "$(envget KUMA_PUSH_CANARY)" ] && kuma_config | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d[\"push\"].get(\"canary\") else 1)"' "a Kuma push monitor for it (only once the timer exists)"
expect 'declare -f copy_code_trees | grep -qF "scripts/\"*.py" && grep -q "canary" "$REPO/scripts/kuma-push.sh" && grep -q "push-canary" "$REPO/monitoring/kuma_bootstrap.py"' "Deploy ships synthetic.py; kuma-push.sh and the bootstrap know the canary job"
expect 'python3 -m py_compile "$REPO/scripts/synthetic.py"' "synthetic.py compiles"
reset "D"; step_canary
expect '[ ! -f "$ce" ] && [ ! -f "$cu.timer" ] && [ -z "$(envget CANARY_USERS)" ] && grep -F "docker: " "$LOG" | grep -q "python -m cwa remove-user canary-a" && seen "systemctl: disable --now bookstack-canary.timer"' "turning it off removes the timer, both accounts and their credentials"
echo "== self-test: a refused connection reads as 000, not 000000"
expect 'bash -c "$(grep -E "^code\(\)" "$REPO/scripts/selftest.sh"); CODE_RETRY_SLEEP=0; curl(){ printf 000; return 7; }; [ \"\$(code https://x)\" = 000 ] && curl(){ printf 403; return 0; } && [ \"\$(code https://x)\" = 403 ]"' "code() gives exactly 000 when curl cannot connect (the origin-lock check wants 000; it got 000000 on the real server)"
expect 'bash -c "$(grep -E "^code\(\)" "$REPO/scripts/selftest.sh"); CODE_RETRY_SLEEP=0; f=\$(mktemp); curl(){ [ -s \$f ] || { echo 1 > \$f; printf 000; return 28; }; printf 403; }; [ \"\$(code https://x)\" = 403 ]; r=\$?; rm -f \$f; exit \$r"' "code() asks once more when nothing answered: one request Cloudflare dropped is not a FAIL (live, 2026-09-28)"

echo "== Caddy and Cloudflare's address list (scripts/caddy-clientip.sh, heal.sh, Deploy/Update)"
CI="$T/ci"; mkdir -p "$CI"
printf '173.245.48.0/20\n2400:cb00::/32\n' > "$CI/ranges"
cilog(){ python3 - "$CI/log" "$@" <<'PYL'
import json, sys
out, rows = sys.argv[1], sys.argv[2:]
with open(out, "w") as f:
    for r in rows:
        ts, ri, ci = r.split(",")
        f.write(json.dumps({"ts": float(ts), "request": {"remote_ip": ri, "client_ip": ci}}) + "\n")
PYL
}
cichk(){ CADDY_ACCESS_LOG="$CI/log" CF_IPS_STATE="$CI/ranges" CLIENTIP_SINCE="${1:-0}" bash "$REPO/scripts/caddy-clientip.sh"; echo $?; }
cilog "100,173.245.48.7,203.0.113.9" "101,100.100.1.2,100.100.1.2"
expect '[ "$(cichk)" = 0 ]' "a Cloudflare-delivered request logged with the visitor's own address: the list is loaded"
cilog "100,173.245.48.7,173.245.48.7" "101,2400:cb00::5,2400:cb00::5" "102,127.0.0.1,127.0.0.1"
expect '[ "$(cichk)" = 1 ]' "every Cloudflare-delivered request logged as the edge itself: NOT loaded (loopback lines do not count)"
cilog "100,127.0.0.1,127.0.0.1"
expect '[ "$(cichk)" = 2 ]' "no Cloudflare-delivered request at all: cannot tell (never a restart on a guess)"
cilog "100,173.245.48.7,173.245.48.7" "200,173.245.48.7,198.51.100.4"
expect '[ "$(cichk 150)" = 0 ] && [ "$(cichk 250)" = 2 ]' "only requests served since a restart count"
python3 - "$CI/log" <<'PYB'
import json, sys
with open(sys.argv[1], "w") as f:
    for i in range(3000):                 # a real-sized log: ~3 MB, far beyond a pipe's 64 KB
        f.write(json.dumps({"ts": 1000.0 + i, "request": {"remote_ip": "173.245.48.7", "client_ip": "203.0.113.%d" % (i % 250),
                            "headers": {"User-Agent": ["x" * 900]}}}) + "\n")
PYB
expect '[ "$(cichk)" = 0 ]' "a real-sized log with every visitor resolved reads as loaded (the live false alarm was tail dying of SIGPIPE under pipefail)"
expect 'grep -q "caddy-clientip.sh" "$REPO/scripts/selftest.sh" && ! grep -q "tail -500 \"\$alog\" 2>/dev/null | python3" "$REPO/scripts/selftest.sh"' "the self-test uses the same checker, not a pipe that stops early"
HB="$T/hb"; mkdir -p "$HB/bin"
cat > "$HB/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$HB/log"
case "$*" in "inspect -f {{.State.Running}} caddy") echo true;; esac
exit 0
EOS
printf '#!/usr/bin/env bash\nexit "${CI_RC:-0}"\n' > "$HB/check"; printf '#!/usr/bin/env bash\necho "ALERT $1" >> "$HB/log"\n' > "$HB/alert"
chmod +x "$HB/bin/docker" "$HB/check" "$HB/alert"
hrun(){ : > "$HB/log"; HB="$HB" PATH="$HB/bin:$PATH" HEAL_SERVICES="" HEAL_STATE="$HB/state" HEAL_ALERT="$HB/alert" CLIENTIP_CHECK="$HB/check" STACK_DIR="$T" bash "$REPO/scripts/heal.sh"; }
rm -f "$HB/state"; CI_RC=1 hrun
expect 'grep -q "docker restart caddy" "$HB/log" && grep -q "ALERT Bookstack: restarted Caddy (Cloudflare address list not loaded)" "$HB/log"' "heal.sh restarts Caddy when it lost Cloudflare's list, and says so"
CI_RC=1 hrun
expect '! grep -q "docker restart caddy" "$HB/log"' "...at most once an hour"
rm -f "$HB/state"; CI_RC=0 hrun; CI_RC=2 hrun
expect '! grep -q "docker restart caddy" "$HB/log"' "...and never when the list is loaded or it cannot tell"
expect 'declare -f step_deploy | grep -q caddy_restart_verified && declare -f step_update | grep -q caddy_restart_verified' "Deploy and Update end with a full Caddy restart that is PROVEN to load Cloudflare's list"
expect '[ "$(TERM=no-such-terminal-xyz bash -c "$(sed -n "/^if \[ -n \"\${TERM:-}\" \] && command -v tput/,/^fi/p" "$REPO/bookstack.sh"); echo \$TERM")" = xterm-256color ] && [ "$(TERM=xterm bash -c "$(sed -n "/^if \[ -n \"\${TERM:-}\" \] && command -v tput/,/^fi/p" "$REPO/bookstack.sh"); echo \$TERM")" = xterm ]' \
  "an unknown terminal (Ghostty's xterm-ghostty) falls back to xterm-256color; a known one is left alone"

echo "== Old image versions: removed once a day, the rollback point kept"
MT="$T/mt"; mkdir -p "$MT/bin" "$MT/stack"
cat > "$MT/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MT/log"
case "$1" in
  ps) printf '%s\n' "ghcr.io/calibrain/shelfmark:v1.4.0" "someone/else:1";;
  images) printf '%s\n' "ghcr.io/calibrain/shelfmark:v1.4.0" "ghcr.io/calibrain/shelfmark:v1.3.15" \
      "crocodilestick/calibre-web-automated:v4.0.7" "crocodilestick/calibre-web-automated:v4.0.6" \
      "bookstack/librarian:latest" "bookstack/librarian:prev" "bookstack/librarian:old" "other/thing:9" "<none>:<none>";;
esac
exit 0
EOS
chmod +x "$MT/bin/docker"
printf "IMG_SHELFMARK=ghcr.io/calibrain/shelfmark:v1.4.0\nIMG_CWA=crocodilestick/calibre-web-automated:v4.0.7\n" > "$MT/stack/.env"
mtdw(){ : > "$MT/log"; MT="$MT" PATH="$MT/bin:$PATH" DF_PCT=40 STACK_DIR="$MT/stack" DISK_STATE="$MT/disk.state" DISK_PAUSE_FLAG="$MT/pause" bash "$REPO/scripts/disk-watch.sh" >/dev/null 2>&1; }
rm -f "$MT/disk.state"; mtdw
expect 'grep -q "docker rmi ghcr.io/calibrain/shelfmark:v1.3.15" "$MT/log" && grep -q "docker rmi crocodilestick/calibre-web-automated:v4.0.6" "$MT/log" && grep -q "docker rmi bookstack/librarian:old" "$MT/log"' \
  "old versions of the stack's own images are removed (a replaced Shelfmark, CWA, an old portal build)"
expect '! grep -qE "docker rmi (ghcr.io/calibrain/shelfmark:v1.4.0|crocodilestick/calibre-web-automated:v4.0.7|bookstack/librarian:(latest|prev)|other/thing|someone/else)" "$MT/log"' \
  "the pinned versions, the :prev rollback images and other people's images are never touched"
mtdw
expect '! grep -q "docker rmi" "$MT/log"' "once a day, not every hour"
rm -f "$MT/disk.state"; printf "IMG_SHELFMARK=ghcr.io/calibrain/shelfmark:v1.3.15\n" > "$MT/stack/.env.images.prev"; mtdw
expect '! grep -q "docker rmi ghcr.io/calibrain/shelfmark:v1.3.15" "$MT/log" && grep -q "docker rmi crocodilestick/calibre-web-automated:v4.0.6" "$MT/log"' \
  "for 7 days after an Update, the versions it would roll back to stay"
rm -f "$MT/disk.state"; touch -d "10 days ago" "$MT/stack/.env.images.prev" 2>/dev/null || touch -t 202001010000 "$MT/stack/.env.images.prev"; mtdw
expect 'grep -q "docker rmi ghcr.io/calibrain/shelfmark:v1.3.15" "$MT/log"' "after that, they go too"
rm -f "$MT/disk.state"; touch "$MT/stack/.update-in-progress"; mtdw; rm -f "$MT/stack/.update-in-progress"
expect '! grep -q "docker rmi ghcr.io/calibrain/shelfmark:v1.3.15" "$MT/log"' "never while an update is unfinished"

echo "== Nightly memory tidy: only a grown, idle service is restarted"
cat > "$MT/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$MT/log"
case "$*" in
  "inspect -f {{.State.Running}} "*) echo true;;
  "stats --no-stream --format {{.MemPerc}} "*) n="${*: -1}"; v="MEM_${n//-/_}"; echo "${!v:-5}.25%";;
  "exec -i librarian python -m admin_cli busy shelfmark") echo "{\"ok\": true, \"busy\": ${SM_BUSY:-false}}";;
  "exec -i librarian python -m admin_cli busy audiobookshelf") echo '{"ok": true, "busy": false}';;
  "inspect -f {{if .State.Health}}"*) echo healthy;;
esac
exit 0
EOS
printf '#!/usr/bin/env bash\nexit "${FLOCK_HELD:-0}"\n' > "$MT/bin/flock"; chmod +x "$MT/bin/docker" "$MT/bin/flock"
mkdir -p "$MT/stack/library/ingest"; printf "X=1\n" > "$MT/stack/.env"
mtt(){ : > "$MT/log"; MT="$MT" PATH="$MT/bin:$PATH" STACK_DIR="$MT/stack" MEMTIDY_METAPUSH_LOCK="$MT/lock" \
  MEM_calibre_web=85 MEM_shelfmark=80 MEM_audiobookshelf=30 MEM_syncthing=10 bash "$REPO/scripts/mem-tidy.sh" > "$MT/out" 2>&1; }
mtt
expect 'grep -q "docker restart calibre-web" "$MT/log" && grep -q "docker restart shelfmark" "$MT/log"' "a service past 70 % of its limit and idle is restarted (Calibre-Web, Shelfmark)"
expect '! grep -q "docker restart audiobookshelf" "$MT/log" && ! grep -q "docker restart syncthing" "$MT/log" && grep -q "audiobookshelf at 30% of its limit: fine" "$MT/out"' "one well under its limit is left alone"
SM_BUSY=true mtt
expect '! grep -q "docker restart shelfmark" "$MT/log" && grep -q "shelfmark at 80% but in use" "$MT/out"' "Shelfmark with a download under way is never restarted"
touch "$MT/stack/library/ingest/new book.epub"; mtt; rm -f "$MT/stack/library/ingest/new book.epub"
expect '! grep -q "docker restart calibre-web" "$MT/log" && grep -q "books are waiting to be imported" "$MT/out"' "Calibre-Web with a book waiting to import is never restarted"
FLOCK_HELD=1 mtt
expect '! grep -q "docker restart calibre-web" "$MT/log" && grep -q "the host job is writing to it" "$MT/out"' "...nor while the host job is writing through it"
expect 'grep -q "write_cron bookstack-memtidy \"45 3 \* \* \*\"" "$REPO/bookstack.sh" && declare -f install_disk_watch | grep -q install_mem_tidy' "installed nightly at 03:45 by Deploy (before the 04:30 reboot window)"

echo "== pipefail + early-exiting grep (the SIGPIPE false alarm, three times on the real server)"
# grep -q stops reading at its first match; a producer with more to write then dies of SIGPIPE and,
# under pipefail, the whole test reads as "not found". Harmless on tiny output, wrong on real output.
expect '[ "$(set -o pipefail; seq 1 200000 | grep -q "^5$"; echo $?)" != 0 ]' "the trap is real: with pipefail, a match found early still 'fails' (so the guard below matters)"
risky=$(grep -nE '(ip -o addr|ss -ltn|ufw status|restic [a-z]+ --help|sshd -T|find .*|journalctl .*|tail -n? ?[0-9]+ .*|docker logs .*|curl .*) *(2>[^|]*)?\| *grep -[a-zA-Z]*q' \
          "$REPO"/scripts/*.sh "$REPO/bookstack.sh" | grep -vE '^[^:]+:[0-9]+: *#' || true)
expect '[ -z "$risky" ]' "no script pipes a long-output command into grep -q (take the output first: grep -q ... <<< \"\$(cmd)\")"
[ -n "$risky" ] && printf '       %s\n' "$risky"
unset risky

expect '[ "$COMPOSE_IGNORE_ORPHANS" = true ] && grep -q "^export COMPOSE_IGNORE_ORPHANS=true" "$REPO/scripts/disk-watch.sh" && grep -q "^export COMPOSE_IGNORE_ORPHANS=true" "$REPO/scripts/selftest.sh" && ! grep -rn -- "--remove-orphans" "$REPO/bookstack.sh" "$REPO"/scripts/*.sh | grep -v "^[^:]*:[0-9]*: *#"' "Authelia and Ephemera (overlay files) are expected, not orphans: no warning, and nothing ever removes them"

echo "== Daily disk summary for the admin (scripts/disk-report.sh, v5.6)"
DR="$T/dr"; mkdir -p "$DR/bin" "$DR/stack/library/books" "$DR/stack/library/audiobooks" "$DR/stack/library/seedbox-sync" "$DR/etc"
cat > "$DR/bin/df" <<'EOS'
#!/usr/bin/env bash
S=85899345920                                      # 80 GiB
case "$*" in
  *-i*--output=ipcent*) printf 'IUse%%\n %s%%\n' "${DR_IPCT:-9}";;
  *--output=size*)  printf '1B-blocks\n%s\n' "$S";;
  *--output=used*)  printf 'Used\n%s\n' "$DR_USED";;
  *--output=avail*) printf 'Avail\n%s\n' "$((S - DR_USED))";;
  *--output=pcent*) printf 'Use%%\n %s%%\n' "$((DR_USED * 100 / S))";;
esac
EOS
printf '#!/usr/bin/env bash\nprintf "%%s\\ttotal\\n" 1073741824\n' > "$DR/bin/du"
cat > "$DR/bin/docker" <<'EOS'
#!/usr/bin/env bash
case "$*" in "system df --format "*) printf 'Images=7.9GB\nContainers=12MB\nLocal Volumes=0B\nBuild Cache=400MB\n';; esac
EOS
printf '#!/usr/bin/env bash\nprintf "              total  used  free\\nMem:   4294967296 2147483648 0\\nSwap:  2147483648 107374182 0\\n"\n' > "$DR/bin/free"
cat > "$DR/alert" <<'EOS'
#!/usr/bin/env bash
printf 'ALERT seq=%s tags=%s prio=%s title=%s\n%s\n' "$ALERT_SEQ" "$ALERT_TAGS" "$3" "$1" "$2" >> "$DR_LOG"
EOS
chmod +x "$DR"/bin/* "$DR/alert"; printf 'X=1\n' > "$DR/stack/.env"
dr(){ : > "$DR/log"; DR_LOG="$DR/log" PATH="$DR/bin:$PATH" STACK_DIR="$DR/stack" DISK_HISTORY="$DR/etc/hist" DISK_REPORT_ALERT="$DR/alert" \
  DR_USED="$1" bash "$REPO/scripts/disk-report.sh" > "$DR/out" 2>&1; }
GiB=1073741824
rm -f "$DR/etc/hist"; dr $((40 * GiB))
expect 'grep -q "^ALERT seq=disk-daily tags=floppy_disk prio=low title=Disk 50% used, 40.0 GB free$" "$DR/log"' "a quiet (low) notification with the same id every day, so today's replaces yesterday's"
expect 'grep -q "Used 40.0 GB of 80.0 GB (50%), 40.0 GB free · inodes 9%" "$DR/log" && grep -q "First report" "$DR/log"' "says how full, in bytes and inodes; the trend starts tomorrow"
expect 'grep -q "Ebooks 1.0 GB · Audiobooks 1.0 GB · Seedbox copies 1.0 GB" "$DR/log" && grep -q "Docker images 7.9GB, build cache 400MB" "$DR/log" && grep -q "Memory: 2.0 of 4.0 GB in use, swap 0.1 GB" "$DR/log"' "where the space went, Docker's share and memory"
expect 'grep -q "^$(date +%F) $((40 * GiB))$" "$DR/etc/hist"' "today's figure is kept for tomorrow's comparison"
{ echo "$(date -d '-7 day' +%F) $((33 * GiB))"; echo "$(date -d '-1 day' +%F) $((39 * GiB))"; } > "$DR/etc/hist"
dr $((40 * GiB))
expect 'grep -q "+1.0 GB since yesterday · 7-day average +1.0 GB a day · 85% in about 28 days" "$DR/log"' "the growth since yesterday, the weekly rate and when it reaches the warning level"
dr $((40 * GiB)); expect '[ "$(grep -c "^$(date +%F) " "$DR/etc/hist")" = 1 ]' "a second run the same day replaces today's figure instead of adding one"
dr $((70 * GiB))
expect 'grep -q "^ALERT seq=disk-daily tags=warning prio=default title=Disk 87% used" "$DR/log"' "at or over DISK_WARN_PCT it makes a sound and shows a warning sign"
printf 'DISK_REPORT=false\n' > "$DR/stack/.env"; dr $((40 * GiB)); printf 'X=1\n' > "$DR/stack/.env"
expect '[ ! -s "$DR/log" ]' "DISK_REPORT=false: nothing is sent"
envset DISK_REPORT ""; envset DISK_REPORT_HOUR ""; install_disk_report
expect 'grep -q "^5 9 \* \* \* root STACK_DIR=.*scripts/disk-report.sh" "$T/etc/cron.d/bookstack-diskreport"' "installed at 09:05 by default"
envset DISK_REPORT_HOUR 21; install_disk_report; expect 'grep -q "^5 21 \* \* \* root" "$T/etc/cron.d/bookstack-diskreport"' "DISK_REPORT_HOUR moves it"
envset DISK_REPORT_HOUR 31; install_disk_report; expect 'grep -q "^5 9 \* \* \* root" "$T/etc/cron.d/bookstack-diskreport"' "an hour that does not exist falls back to 09"
envset DISK_REPORT false; install_disk_report; expect '[ ! -e "$T/etc/cron.d/bookstack-diskreport" ]' "DISK_REPORT=false removes it"
envset DISK_REPORT ""; envset DISK_REPORT_HOUR ""
expect 'declare -f install_disk_watch | grep -q install_disk_report' "Deploy installs it with the other scheduled jobs"
expect 'declare -f step_deploy | grep -q "absctl backups"' "Deploy switches on Audiobookshelf's own nightly database copy"

echo "== Family sharing: Shelfmark's request step follows the portal's ability to answer it"
envset APPROVALS_REQUIRED false; envset FAMILY_SHARING true; envset SHELFMARK_SVC_USER ""; envset SHELFMARK_SVC_PASS ""; envset SHELFMARK_REQUESTS ""
sync_shelfmark_requests
expect '[ "$(envget SHELFMARK_REQUESTS)" = false ]' "no portal service login yet: requests stay OFF (a request nobody answers would wait forever)"
envset SHELFMARK_SVC_USER svc-portal; envset SHELFMARK_SVC_PASS x; sync_shelfmark_requests
expect '[ "$(envget SHELFMARK_REQUESTS)" = true ]' "with the portal able to answer, every reader's download passes it first (shared, or approved in seconds)"
envset FAMILY_SHARING false; sync_shelfmark_requests
expect '[ "$(envget SHELFMARK_REQUESTS)" = false ]' "family sharing off and no approvals: Shelfmark downloads directly, as before"
envset APPROVALS_REQUIRED true; envset SHELFMARK_SVC_USER ""; sync_shelfmark_requests
expect '[ "$(envget SHELFMARK_REQUESTS)" = true ]' "approvals on: requests on, as L16 always did"
envset APPROVALS_REQUIRED false; envset FAMILY_SHARING true; envset SHELFMARK_SVC_USER svc-portal
expect 'declare -f step_deploy | grep -q sync_shelfmark_requests' "Deploy sets it after the service login is ensured"

echo "== Library -> Seedbox: the seedbox's downloads through Syncthing (nothing there ever changes)"
sbe="$T/etc/bookstack/seedbox.env"; rm -f "$sbe"
mkdir -p "$STACK_DIR/scripts"; cat > "$STACK_DIR/scripts/seedbox-fetch.py" <<'PYS'
import os, sys
c = dict(l.rstrip("\n").split("=", 1) for l in open(os.environ["SEEDBOX_ENV"]) if "=" in l)
arg = sys.argv[1] if len(sys.argv) > 1 else ""
if arg == "--check-rtorrent":
    ok = c.get("SEEDBOX_RT_PASS") == "p$w'd \"x" and c.get("SEEDBOX_RT_USER") == "hcus"
    print("ok: stub rTorrent" if ok else "FAIL: rTorrent XML-RPC: the username/password was refused")
    sys.exit(0 if ok else 1)
if arg in ("--setup", "--device-id"):
    print("STUBVPS-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG"); sys.exit(0)
print("ok: stub check"); sys.exit(0)
PYS
DEV=PUN4DSQ-U7H4CSC-5EPCANX-EKFOQFH-OYPFUOP-C7JS42P-SAB4XTW-6CVGKA4
reset "$(printf '%s' "$DEV" | tr '[:upper:]' '[:lower:]')" "" "/data/watch/downloads/sabnzbd/completed/" "" \
      "https://hcus-rutorrent.primeape.seedbox.link/RPC2" "/sdb/hcus/data/rtorrent/bookstack" "" "hcus" "p\$w'd \"x"
step_seedbox; rc=$?
expect '[ $rc = 0 ] && grep -qxF "SEEDBOX_RT_PASS=p\$w'"'"'d \"x" "$sbe" && [ "$(stat -c %a "$sbe" 2>/dev/null || stat -f %Lp "$sbe")" = 600 ]' "a password with \$, quotes and spaces is stored exactly as typed (the job reads it raw), 0600"
expect 'grep -qxF "SEEDBOX_ST_DEVICE=$DEV" "$sbe" && grep -qxF "SEEDBOX_SAB_OWN=/data/watch/downloads/sabnzbd/completed" "$sbe" && grep -q "^SEEDBOX_ST_GUI_PASS=[0-9a-f]\{32\}$" "$sbe"' "the device ID is normalised to upper case; a GUI password is generated"
expect 'grep -qxF "SEEDBOX_SOURCES=bookstack-sab-ebooks|sabnzbd/bookstack-ebooks|sab;bookstack-sab-audiobooks|sabnzbd/bookstack-audiobooks|sab;bookstack-rtorrent|rtorrent|rt" "$sbe"' "both SABnzbd categories and the rTorrent folder become Syncthing folders"
expect '[ "$(envget SEEDBOX_ENABLED)" = true ] && [ "$(envget SYNCTHING_API_KEY | wc -c)" -ge 40 ] && ! grep -qE "^SEEDBOX_(ST|RT|SAB)" "$ENV_FILE"' ".env gets only the switch and Syncthing's API key; seedbox passwords never go there (the containers read .env)"
expect 'grep -F "docker: " "$LOG" | grep -q -- "--profile seedbox up -d syncthing" && seen "ufw: allow 22000/tcp"' "this server's Syncthing is started (profile seedbox) and its port opened"
expect 'grep -qxF "SEEDBOX_PROWLARR_URL=https://hcus-prowlarr.primeape.seedbox.link" "$sbe"' "Prowlarr's address is suggested from ruTorrent's (hcus-rutorrent -> hcus-prowlarr)"
nrc="$STACK_DIR/shelfmark/netrc/seedbox"
expect '[ "$(cat "$nrc")" = "machine hcus-prowlarr.primeape.seedbox.link login \"hcus\" password \"p\$w'"'"'d \\\"x\"" ] && [ "$(stat -c %a "$nrc" 2>/dev/null || stat -f %Lp "$nrc")" = 600 ]' "Shelfmark gets the seedbox login for Prowlarr's host only (netrc, quotes escaped, 0600)"
expect 'grep -q "NETRC=/run/netrc/seedbox" "$REPO/docker-compose.yml" && grep -q "./shelfmark/netrc:/run/netrc:ro" "$REPO/docker-compose.yml"' "...mounted read-only into Shelfmark, which reads it for its .torrent / .nzb fetches"
expect '[ -d "$STACK_DIR/library/seedbox-sync/sabnzbd/bookstack-ebooks" ] && [ -d "$STACK_DIR/library/seedbox-sync/rtorrent" ] && [ -d "$STACK_DIR/syncthing" ]' "its folders exist before it starts (so Docker does not make them root's)"
su="$T/etc/systemd/system/bookstack-seedbox"
expect 'grep -q "^OnCalendar=\*:\*:00/20$" "$su.timer" && grep -qF "ExecStart=/usr/bin/python3 $STACK_DIR/scripts/seedbox-fetch.py" "$su.service" && seen "systemctl: enable --now bookstack-seedbox.timer"' "every 20 seconds (Shelfmark cancels after 5 minutes of waiting), through scripts/seedbox-fetch.py"
expect 'grep -qF "Device ID:    STUBVPS-AAAAAAA" "$LOG" && grep -qF "Folder Type:  Send Only          <- REQUIRED" "$LOG" && grep -qF "Full Rescan Interval (s):  300" "$LOG" && grep -qF "\"Bookstack rTorrent\"          -> /sdb/hcus/data/rtorrent/bookstack" "$LOG" && grep -qF "Addresses:  tcp://$(envget PUBLIC_IP):22000" "$LOG"' "the seedbox steps: this server's ID and address, each folder's path, Send Only, rescan 300"
expect 'grep -qF "Remote Path  /data/watch/downloads/sabnzbd/completed" "$LOG" && grep -qF "Remote Path  /sdb/hcus/data/rtorrent/bookstack" "$LOG" && grep -qF "Local Path   /seedbox/rtorrent" "$LOG" && grep -qF "Completed Path Wait (seconds):  3600" "$LOG"' "the exact Shelfmark path mappings and wait time are shown"
expect 'grep -qF "NOTHING ON THE SEEDBOX IS EVER MOVED, DELETED OR CHANGED" "$LOG" && grep -qF "this server'"'"'s side is Receive Only" "$LOG"' "the setup screen says nothing on the seedbox changes, and why"
expect 'grep -q "./library/seedbox:/seedbox" "$REPO/docker-compose.yml" && grep -q "PROWLARR_TORRENT_ACTION=keep" "$REPO/docker-compose.yml" && grep -q "PROWLARR_USENET_ACTION=copy" "$REPO/docker-compose.yml"' "Shelfmark sees /seedbox, and its own clean-up is pinned: torrents keep, Usenet copy"
expect 'grep -q "profiles: \[\"seedbox\"\]" "$REPO/docker-compose.yml" && grep -q "./library/seedbox-sync:/sync" "$REPO/docker-compose.yml" && grep -q "\"127.0.0.1:8384:8384\"" "$REPO/docker-compose.yml"' "the Syncthing service: opt-in profile, the sync folder, its API on loopback only"
expect 'grep -q "library/seedbox\" " "$REPO/scripts/backup.sh" && grep -q "library/seedbox-sync" "$REPO/scripts/backup.sh"' "backups skip the transient seedbox copies"
# a failing rTorrent check: nothing saved unless the admin insists
cp "$sbe" "$T/sbe.good"
reset "E" "" "" "" "" "" "" "" "" "wrong" "no"
step_seedbox; rc=$?
expect '[ $rc = 1 ] && cmp -s "$sbe" "$T/sbe.good" && [ ! -e "$sbe.new" ] && grep -F "yesno: " "$LOG" | grep -q "The seedbox check found problems"' "a failing seedbox check saves nothing (the working settings stay)"
reset "E" "not-a-device-id"
step_seedbox; rc=$?
expect '[ $rc = 1 ] && cmp -s "$sbe" "$T/sbe.good" && seen "is not a Syncthing Device ID"' "a mistyped device ID is refused before anything changes"
reset "S"; step_seedbox
expect 'grep -qF "Device ID:    STUBVPS-AAAAAAA" "$LOG"' "Show what to set up: this server's ID again, any time"
reset "D"; step_seedbox
expect '[ ! -f "$su.timer" ] && seen "systemctl: disable --now bookstack-seedbox.timer" && [ "$(envget SEEDBOX_ENABLED)" = false ] && seen "ufw: --force delete allow 22000/tcp" && grep -F "docker: " "$LOG" | grep -q "stop syncthing" && [ ! -e "$nrc" ]' "turning it off stops the job and Syncthing, closes its port and takes Shelfmark's seedbox login away"

echo "== L20: restart unhealthy containers, carefully (scripts/heal.sh)"
HL="$T/hl"; mkdir -p "$HL/bin"
cat > "$HL/bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "$HL/log"
case "$*" in
  "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{end}} calibre-web") echo "${HL_CWA:-unhealthy}";;
  "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{end}} "*) echo healthy;;
  *"range .State.Health.Log"*) echo "sync returned 500";;
  "restart "*) : ;;
esac
EOS
chmod +x "$HL/bin/docker"
printf '#!/usr/bin/env bash\necho "ALERT $1" >> "%s/log"\n' "$HL" > "$HL/alert.sh"; chmod +x "$HL/alert.sh"
hlrun(){ HL="$HL" HL_CWA="${1:-unhealthy}" PATH="$HL/bin:$PATH" HEAL_STATE="$HL/state" HEAL_ALERT="$HL/alert.sh" bash "$REPO/scripts/heal.sh"; }
rm -f "$HL/state"; : > "$HL/log"; hlrun
expect '! grep -q "docker restart" "$HL/log"' "one unhealthy reading is not enough (a slow start is not a fault)"
hlrun
expect 'grep -q "docker restart calibre-web" "$HL/log" && grep -q "ALERT Bookstack: restarted calibre-web" "$HL/log"' "two in a row: restarted, and the admin is told"
: > "$HL/log"; hlrun; hlrun
expect '! grep -q "docker restart" "$HL/log" && grep -q "still unhealthy after a restart" "$HL/log"' "still unhealthy within 30 min: no restart loop, an alert for a person instead"
: > "$HL/log"; hlrun; hlrun
expect '[ "$(grep -c "still unhealthy" "$HL/log")" = 0 ]' "and that alert is sent once, not every 2 minutes"
expect '! grep -q "caddy\|qbittorrent" <<< "$(grep -o "HEAL_SERVICES:-[^}]*" "$REPO/scripts/heal.sh")"' "caddy and qbittorrent are never auto-restarted"
expect 'declare -f install_disk_watch | grep -q install_heal && grep -q "bookstack-heal" "$REPO/bookstack.sh"' "installed on Deploy as a cron job whose output reaches the journal"

# ---------------------------------------------------------------------------------------------
echo "== Backlog truth (docs/RESEARCH-GAPS.md vs the code)"
# Step 0. The backlog and the code drifted apart silently for three audit rounds: L13 shipped its
# guard rail (auto_metadata_update_tags=0) and never shipped the feature, so the repo looked as
# though metadata fetching existed. Prose cannot enforce itself — these probes can.
#
# Each probe answers ONE question: is the code for this item present? A probe that disagrees with
# the declared Status fails the suite, in either direction: claiming 'done' for absent code is a
# lie to the owner, and leaving 'not_done' on something that shipped is how the remaining half of
# a partial item becomes invisible. Items with no probe still must carry an explicit Status.
GAPS="$REPO/docs/RESEARCH-GAPS.md"
declared(){ sed -n "/^### $1 — /,/^### /p" "$GAPS" | sed -n 's/^- \*\*Status\*\*: \([a-z_]*\).*/\1/p' | head -1; }
# id | "present" probe (exit 0 = the code IS there) | what it looks for
probe(){ case "$1" in
  L06) [ -x "$REPO/scripts/update-check.sh" ] && grep -q install_update_check "$REPO/bookstack.sh";;   # update notices (no Diun: no socket)
  L07) [ -f "$REPO/monitoring/kuma_bootstrap.py" ] && grep -q setup_monitoring "$REPO/bookstack.sh";;  # Kuma configured
  L21) grep -q "def kindle_once" "$REPO/librarian/worker.py";;
  L01) grep -q "networks: { fetch: {} }" "$REPO/docker-compose.yml" && grep -q "cap_drop: \[ALL\]" "$REPO/docker-compose.yml";;
  L05) grep -q "def _gate_sso" "$REPO/librarian/app.py" && [ -f "$REPO/scripts/gate-sync.py" ] && grep -q "def proxy_login_settings" "$REPO/librarian/cwa.py";;
  L08) [ -f "$REPO/scripts/synthetic.py" ] && grep -q "step_canary" "$REPO/bookstack.sh" && grep -q "def canary_record" "$REPO/librarian/db.py";;
  L14) grep -q "origin_tls_client_auth" "$REPO/bookstack.sh" && grep -q "AOP_MODE" "$REPO/scripts/cert-watch.sh";;
  L15) grep -q "RESTIC_APPEND_ONLY" "$REPO/scripts/backup.sh" && [ -x "$REPO/scripts/prune.sh" ] && grep -q "step_prune_key" "$REPO/bookstack.sh";;
  L02) grep -q "Password_PBKDF2" "$REPO/bookstack.sh" && grep -q "QBIT_PASS" "$REPO/scripts/selftest.sh";;
  L18) grep -q "^upload.@@DOMAIN@@ {" "$REPO/caddy/Caddyfile.template" && grep -q "_upload_limit" "$REPO/librarian/app.py";;
  L17) grep -q "def _turnstile_ok" "$REPO/librarian/app.py" && grep -q "step_turnstile" "$REPO/bookstack.sh";;
  L12) grep -q "def kobo_status" "$REPO/librarian/cwa.py" && grep -q "kobo_test" "$REPO/librarian/app.py";;
  L09) [ -x "$REPO/scripts/cert-watch.sh" ] && grep -q install_cert_watch "$REPO/bookstack.sh";;
  L16) grep -q "REQUESTS_ENABLED=\${SHELFMARK_REQUESTS:-\${APPROVALS_REQUIRED" "$REPO/docker-compose.yml" && [ -f "$REPO/librarian/shelfmark_api.py" ];;
  L11) grep -q "kepubify" "$REPO/librarian/Dockerfile" && grep -q "def _kepub_from_epub" "$REPO/librarian/library.py";;
  L10) grep -q "def reconcile_untagged" "$REPO/librarian/worker.py" && grep -q '"tags", "pending"' "$REPO/scripts/metadata-push.sh";;
  L08) grep -rqi 'canary' "$REPO/bookstack.sh" "$REPO/scripts";;          # scheduled journey
  L13) grep -q 'auto_metadata_fetch_enabled' "$REPO/bookstack.sh";;       # CWA metadata fetch
  L15) grep -rq 'append-only\|append_only' "$REPO/scripts" "$REPO/bookstack.sh";;
  L20) [ -x "$REPO/scripts/heal.sh" ] && grep -q install_heal "$REPO/bookstack.sh";;   # restart unhealthy (host cron, no socket)
  *) return 2;; esac; }
for id in L01 L02 L03 L04 L05 L06 L07 L08 L09 L10 L11 L12 L13 L14 L15 L16 L17 L18 L19 L20 L21 L22; do
  d=$(declared "$id")
  case "$d" in done|partial|not_done|obsolete|dropped) ;; *)
    bad "$id has no valid **Status** line in RESEARCH-GAPS.md (got '${d:-none}')"; continue;; esac
  probe "$id"; rc=$?
  [ "$rc" = 2 ] && continue                       # no probe for this one; the Status line is the contract
  if [ "$rc" = 0 ] && [ "$d" = not_done ]; then
    bad "$id is marked not_done but its code IS present — update the Status line"
  elif [ "$rc" != 0 ] && [ "$d" = done ]; then
    bad "$id is marked done but its code is ABSENT (this is exactly how L13 hid)"
  else
    ok "$id status '$d' agrees with the code"
  fi
done
echo; echo "TUI RESULT: $pass passed, $fails failed"; exit $fails
