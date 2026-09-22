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
askpw() { echo "askpw: $1" >> "$LOG"; local a; a="$(pop)"; [ "$a" = "<cancel>" ] && return 1; [ "$a" = "<blank>" ] && a=""; printf '%s' "$a"; }
yesno() { echo "yesno: $1" >> "$LOG"; local a; a="$(pop)"; [ -z "$a" ] && a=yes; [ "$a" = "yes" ]; }
clear() { :; }
sleep() { :; }
# askpw2, msg and big are the REAL functions: they run through this whiptail stub, which -
# like the real binary - draws its dialog on STDOUT (UIBYTES) and answers on stderr. Any
# msgbox drawing that leaks into a $(...) capture therefore shows up in the captured value.
whiptail(){ echo "whiptail: $*" >> "$LOG"; local a
  case " $* " in
    *" --msgbox "*) printf 'UIBYTES\e[0m\n'; return 0;;
    *" --yesno "*) a="$(pop)"; [ -z "$a" ] || [ "$a" = yes ];;
    *) a="$(pop)"; printf '%s' "$a" >&2;;
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
    *"python -m cwa list"*) echo '[{"name":"admin","is_admin":true,"isolated":false},{"name":"alice","is_admin":false,"isolated":true},{"name":"bob","is_admin":false,"isolated":false}]';;
    *"python -m cwa passwd"*) [ "${FAIL_PASSWD:-0}" = 1 ] && return 1; echo '{"ok": true}';;
    *"python -m cwa "*) echo '{"ok": true}';;
    *"python -m abs init"*) echo '{"ok": true, "created_root": true, "api_key": "abs-key-STUB", "library_id": "lib1", "created_library": true}';;
    *"python -m abs ensure-user"*) echo '{"ok": true, "user": "alice", "result": "created"}';;
    *"python -m abs status"*) echo "{\"isInit\": ${ABS_INIT:-true}, \"app\": \"audiobookshelf\"}";;
    *"python -m abs "*) echo '{"ok": true}';;
    *"sqlite3 /config/cwa.db SELECT"*) echo '1|epub|new_record|1||0';;
    *"inspect -f"*) echo true;;
    *"compose build"*) [ "${FAIL_BUILD:-0}" = 1 ] && return 1; :;;
    *"ps -q"*) :;;
    *) :;;
  esac
}
curl(){ echo "curl: $*" >> "$LOG"; case "$*" in *ipify*) echo 203.0.113.5;; *) return 0;; esac; }
chown(){ echo "chown: $*" >> "$LOG"; }
install(){ local d; for d in "$@"; do case "$d" in -*|1000) ;; *) mkdir -p "$d";; esac; done; }
tailscale(){ echo "tailscale: $*" >> "$LOG"; case "$*" in *"status --json"*) echo "{\"Self\":{\"KeyExpiry\":${TS_EXPIRY:-null}}}";; esac; }
ufw(){ echo "ufw: $*" >> "$LOG"; }
apt-get(){ echo "apt-get: $*" >> "$LOG"; }; systemctl(){ echo "systemctl: $*" >> "$LOG"; }
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

echo "== configure step"
rm -f "$ENV_FILE"; mkdir -p "$STACK_DIR"
reset "example.test" "admin@example.test" "Asia/Kolkata" "cf-token-123" "gatepass-12345" "gatepass-12345"
step_configure && ok "step_configure ran" || bad "step_configure failed"
missing=""; for k in DOMAIN ADMIN_EMAIL TZ CF_API_TOKEN PUBLIC_IP ARIA2_SECRET LIBRARIAN_SECRET INTAKE_TOKEN ADMIN_HASH SRC_GUTENBERG IA_COLLECTIONS SHELFMARK_LANGUAGE EPHEMERA_ENABLED AUTHELIA_ENABLED APPROVALS_REQUIRED SHELFMARK_TITLE IMG_CWA IMG_ABS IMG_SHELFMARK IMG_QBIT IMG_ARIA2 IMG_ARIANG IMG_KUMA IMG_AUTHELIA IMG_FLARESOLVERR; do
  [ -n "$(envget $k)" ] || missing="$missing $k"; done; [ -z "$missing" ] && ok "all keys written (incl. IMG_* pins)" || bad "missing keys:$missing"
expect '[ "$(envget PUBLIC_IP)" = 203.0.113.5 ]' "public IP detected"
expect '[ "$(envget ADMIN_HASH)" = "\$2a\$14\$STUBHASH/abc" ] && seen "docker-stdin: gatepass-12345" && ! seen "plaintext"' "admin gate password hashed from stdin (never --plaintext argv), hash stored verbatim"
expect '[ "$(envget CF_API_TOKEN)" = cf-token-123 ] && [ "$(envget TZ)" = Asia/Kolkata ] && [ "$(envget SHELFMARK_CONCURRENCY)" = 1 ]' "token, timezone and concurrency 1 stored"
expect 'seen "chown: root:root $ENV_FILE"' ".env handed back to root after the recursive chown"
missing=""; for f in docker-compose.yml docker-compose.authelia.yml docker-compose.ephemera.yml caddy/Dockerfile caddy/Caddyfile.template caddy/Caddyfile authelia/configuration.yml.template authelia/caddy-gate.snippet authelia/inject-gate.py scripts/backup.sh scripts/selftest.sh scripts/cf-ips.sh scripts/alert.sh scripts/disk-watch.sh scripts/restore-test.sh configs/fail2ban/jail.local configs/fail2ban/caddy-device-auth.conf librarian/app.py librarian/cwa.py librarian/templates/devices.html librarian/Dockerfile librarian/.dockerignore; do
  [ -f "$STACK_DIR/$f" ] || missing="$missing $f"; done; [ -z "$missing" ] && ok "stack files copied" || bad "not copied:$missing"
expect '[ ! -d "$STACK_DIR/librarian/tests" ]' "tests are not shipped to the server"
expect '! grep -q "@@" "$STACK_DIR/caddy/Caddyfile"' "Caddyfile has no unrendered placeholders"
expect 'grep -q "^books.example.test {" "$STACK_DIR/caddy/Caddyfile" && grep -q "^shelf.example.test {" "$STACK_DIR/caddy/Caddyfile" && grep -q "^ephemera.example.test {" "$STACK_DIR/caddy/Caddyfile"' "Caddyfile has books/shelf/ephemera vhosts"
expect 'grep -q "bind 203.0.113.5" "$STACK_DIR/caddy/Caddyfile" && grep -q "bind 127.0.0.1" "$STACK_DIR/caddy/Caddyfile"' "vhosts bind public and tailnet IPs"
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

echo "== users & devices menu"
reset "alice" "alice@example.test" "alicepass-123" "alicepass-123" "no" ""     # not admin, no kindle
step_user_add && ok "step_user_add" || bad "step_user_add"
expect 'seen "python -m cwa add-user alice --email alice@example.test --password-stdin" && seen "docker-stdin: alicepass-123"' "calls the portal CLI with the password on stdin"
expect '! grep -E "python -m (cwa|abs) .*--password " "$LOG"' "no --password <value> on any cwa/abs argv"
expect '! seen "alicepass-123 --admin"' "non-admin by default"
expect '[ -d "$STACK_DIR/library/dropbox/alice" ]' "dropbox created"
expect 'seen "https://books.example.test/kobo/abc123"' "Kobo link shown to the admin"
expect 'seen "authelia crypto hash"' "Authelia login created alongside (Authelia enabled)"
reset "boss" "" "bosspass-123" "bosspass-123" "yes" "k@kindle.com"; step_user_add
expect 'seen "add-user boss --email boss@example.test --password-stdin --admin"' "admin flag + default e-mail"
expect 'seen "cwa kindle boss k@kindle.com"' "Kindle set during creation"
reset "carl" "" "carlpass-1234" "carlpass-1234" "no" "<cancel>"; step_user_add; expect '! seen "cwa kindle" && seen "User carl created"' "Cancel at the Kindle prompt during Add still finishes the user"
reset "alice" "kindle@x.com"; step_user_kindle; expect 'seen "cwa kindle alice kindle@x.com"' "set Kindle address"
reset "alice" "<cancel>"; step_user_kindle; expect '! seen "cwa kindle"' "Cancel at the Kindle prompt leaves the address unchanged"
reset "alice" ""; step_user_kindle; expect '! seen "cwa kindle"' "blank at the Kindle prompt changes nothing"
reset "alice" "none"; step_user_kindle; expect 'grep -qE "cwa kindle alice $" "$LOG"' "typing 'none' clears the Kindle address"
reset "alice" "no"; step_user_kobo; expect 'seen "cwa kobo-url alice --reset"' "Kobo link regenerate"
reset "alice" "yes"; step_user_kobo; expect '! seen "--reset"' "Kobo link show (no reset)"
reset "alice" "newpass-1234" "newpass-1234"; step_user_passwd; expect 'seen "cwa passwd alice --password-stdin" && seen "docker-stdin: newpass-1234"' "password reset via stdin"
expect '! seen "python -m abs"' "no Audiobookshelf calls while ABS is not set up"

echo "== audiobookshelf setup + users"
envset AUTHELIA_ENABLED false
reset "root" "rootpass-1234" "rootpass-1234"; step_abs_setup && ok "step_abs_setup" || bad "step_abs_setup"
expect 'seen "python -m abs init --user root --password-stdin" && seen "docker-stdin: rootpass-1234" && [ "$(envget ABS_TOKEN)" = abs-key-STUB ] && [ "$(envget ABS_ROOT_USER)" = root ]' "ABS init (password on stdin) stores the API key"
expect 'seen "python -m abs ensure-user alice" && seen "python -m abs ensure-user bob" && ! seen "ensure-user admin"' "existing non-admin users aligned in ABS"
reset "dave" "" "davepass-1234" "davepass-1234" "no" ""; step_user_add
expect 'seen "python -m abs ensure-user dave --password-stdin" && seen "docker-stdin: davepass-1234"' "new user gets an ABS account with the same password (stdin)"
reset "erin" "" "erinpass-1234" "erinpass-1234" "yes" ""; step_user_add
expect '! seen "ensure-user erin"' "admins do not get a tag-restricted ABS account"
reset "alice" "newpass-9999" "newpass-9999"; step_user_passwd; expect 'seen "python -m abs ensure-user alice --password-stdin"' "password reset also updates ABS"
reset "alice" "yes"; step_user_remove; expect 'seen "python -m abs remove-user alice"' "user removal also removes the ABS account"
reset; step_user_repair; expect 'seen "python -m abs ensure-user bob" && ! seen "abs ensure-user admin"' "repair aligns ABS accounts for non-admins"
reset "yes" "root" "rootpass-1234" "rootpass-1234"; step_abs_setup; expect 'seen "yesno: Audiobookshelf is already set up"' "re-running setup asks first"
reset "alice" "yes"; step_user_remove; expect 'seen "cwa remove-user alice"' "remove user after confirmation"
reset "alice" "no"; step_user_remove; expect '! seen "cwa remove-user"' "remove user aborted on No"
reset; step_user_repair; expect 'seen "cwa isolate bob" && seen "cwa isolate alice" && ! seen "cwa isolate admin" && seen "cwa harden"' "repair re-isolates every non-admin (never admins) + hardens"

echo "== formats, mail, defaults"
reset "kepub" "yes" "no" "pdf,azw3" "new_record" "yes" "no"; step_formats && ok "step_formats" || bad "step_formats"
expect "seen \"UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='kepub', kindle_epub_fixer=0, auto_convert_retained_formats='pdf,azw3', auto_ingest_automerge='new_record';\"" "formats written to CWA settings"
expect "seen \"koreader_sync_enabled=1\" && [ \"\$(envget KOSYNC_ENABLED)\" = true ]" "KOReader sync toggled on in CWA and advertised to the portal"
expect 'seen "auto_backup_imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0"' "CWA file copies off by default"
reset "epub" "no" "yes" "x; DROP TABLE--" "overwrite" "no" "yes"; step_formats
expect "seen \"auto_convert_retained_formats='xdroptable'\" && seen \"koreader_sync_enabled=0\" && [ \"\$(envget KOSYNC_ENABLED)\" = false ] && seen \"auto_backup_imports=1\"" "retained-formats input is sanitised; KOReader off again; copies re-enabled on request"
reset "smtp.example.test" "465" "ssl" "user@x" "smtp-pass" "lib@x" ""; step_mail && ok "step_mail" || bad "step_mail"
expect '[ "$(envget SMTP_HOST)" = smtp.example.test ] && [ "$(envget SMTP_PORT)" = 465 ] && [ "$(envget SMTP_SECURITY)" = ssl ] && [ "$(envget SMTP_USER)" = user@x ] && [ "$(envget SMTP_PASS)" = smtp-pass ] && [ "$(envget SMTP_FROM)" = lib@x ]' "SMTP settings stored"
reset "smtp.example.test" "465" "ssl" "user@x" "" "lib@x" "me@x"; step_mail
expect '[ "$(envget SMTP_PASS)" = smtp-pass ] && seen "python -m kindle test me@x"' "blank password keeps the old one; test mail sent"
reset "smtp.example.test" "465" "ssl" "<cancel>"; step_mail; expect '[ "$(envget SMTP_USER)" = user@x ]' "Cancel at the SMTP username keeps the setting"
reset "<blank>"; step_mail; expect '[ -z "$(envget SMTP_HOST)" ]' "blank host disables mail"
reset; apply_library_defaults; expect "seen \"cwa harden\" && seen \"auto_ingest_automerge='new_record'\" && seen \"auto_backup_imports=0\"" "library defaults: harden + conversion policy + no CWA copies"

echo "== sources"
reset '"GUTENBERG" "LIBRIVOX"' "gutenberg,cdl" "no" "yes" "25x" "no"; step_sources
expect '[ "$(envget SRC_GUTENBERG)" = true ] && [ "$(envget SRC_STANDARD)" = false ] && [ "$(envget SRC_LIBRIVOX)" = true ] && [ "$(envget IA_COLLECTIONS)" = gutenberg,cdl ] && [ "$(envget IA_USE_TORRENT)" = false ] && [ "$(envget APPROVALS_REQUIRED)" = true ] && [ "$(envget SRC_MYCATALOG)" = false ] && [ "$(envget MAX_REQUESTS_PER_DAY)" = 25 ]' "source toggles + approvals + sanitised daily quota stored"

echo "== fail2ban rendering"
cf(){ echo "cf: $*" >> "$LOG"; echo '{"result":[{"id":"zone-STUB"}]}'; }; jq(){ command jq "$@"; }
mkdir -p "$T/etc"
( f2b_root="$T/etc/fail2ban"; mkdir -p "$f2b_root/filter.d"
  render_fail2ban_test(){ sed -e "s|@@CADDY_JAIL@@|true|" -e "s|@@CF_API_TOKEN@@|$(envget CF_API_TOKEN)|" -e "s|@@CF_ZONE@@|zone-STUB|" "$STACK_DIR/configs/fail2ban/jail.local"; }
  render_fail2ban_test > "$f2b_root/jail.local"
  [ "$(grep -c 'action   = cloudflare-token\[cftoken="cf-token-123", cfzone="zone-STUB"\]' "$f2b_root/jail.local")" = 2 ] && ! grep -q '@@' "$f2b_root/jail.local" && grep -q '^\[caddy-device-auth\]' "$f2b_root/jail.local" && echo OK > "$T/f2b.ok" )
expect '[ -f "$T/f2b.ok" ]' "both Caddy jails render with the Cloudflare token + zone and no placeholders left"
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
reset; step_cloudflare && ok "step_cloudflare ran" || bad "step_cloudflare failed"
expect 'seen "cf: PATCH /zones/zone-STUB/settings/browser_check --data {\"value\":\"off\"}" && seen "cf: PATCH /zones/zone-STUB/settings/email_obfuscation --data {\"value\":\"off\"}" && seen "settings/rocket_loader --data {\"value\":\"off\"}"' "Browser Integrity Check, e-mail obfuscation and Rocket Loader set OFF"
expect 'seen "cf: PUT /zones/zone-STUB/rulesets/phases/http_request_cache_settings/entrypoint" && grep -F "http_request_cache_settings/entrypoint --data" "$LOG" | grep -q "\"cache\":false" && grep -F "cache_settings/entrypoint --data" "$LOG" | grep -q "books.example.test"' "no-cache Cache Rule for the four public hosts"
expect '! seen "Bot Fight Mode: ON" && grep -F "msgbox" "$LOG" | grep -q "Do NOT enable Bot Fight Mode"' "Bot Fight Mode advice: must stay OFF"
expect '[ -f "$T/etc/cron.d/bookstack-cfips" ]' "cf-ips cron installed under the host config root"
expect 'seen "cf: PUT /zones/zone-STUB/dns_records/zone-STUB --data {\"type\":\"A\",\"name\":\"dl.example.test\",\"content\":\"100.64.0.1\",\"ttl\":1,\"proxied\":false}"' "tailnet-only hosts are grey-clouded"

echo "== deploy: Caddy last, admin password loop, ABS init before exposure"
rm -f "$STACK_DIR/caddy/cf-origin-pull-ca.pem"; reset; step_deploy; expect 'seen "Run the Cloudflare step first"' "deploy refuses without origin-pull CA"
touch "$STACK_DIR/caddy/cf-origin-pull-ca.pem"; mkdir -p "$STACK_DIR/cwa/config"; touch "$STACK_DIR/cwa/config/app.db"
envset ADMIN_PW_SET false; envset ABS_TOKEN ""
FAIL_BUILD=1; reset; step_deploy; rc=$?; FAIL_BUILD=0
expect '[ $rc = 1 ] && seen "Image build failed" && ! seen "compose up -d"' "compose build failure -> step_deploy returns 1 before anything starts"
reset "adminpass-1234" "adminpass-1234"; step_deploy && ok "step_deploy (ABS already initialised)" || bad "step_deploy failed"
first=$(grep -F "compose up -d calibre-web" "$LOG" | head -1)
expect '[ -n "$first" ] && ! printf "%s" "$first" | grep -q caddy' "first compose up does not include caddy"
expect 'seen "cwa passwd admin --password-stdin" && seen "docker-stdin: adminpass-1234" && [ "$(envget ADMIN_PW_SET)" = true ]' "admin password set via stdin, ADMIN_PW_SET recorded"
expect 'seen "compose up -d caddy" && [ "$(line_of "cwa passwd admin")" -lt "$(line_of "compose up -d caddy")" ]' "caddy started only AFTER the admin password"
expect '[ -f "$T/etc/cron.d/bookstack-disk" ] && grep -q "disk-watch.sh" "$T/etc/cron.d/bookstack-disk"' "hourly disk watchdog cron installed"
expect '! seen "python -m abs init"' "ABS setup not re-run when already initialised"
envset ADMIN_PW_SET false
reset "<cancel>"; step_deploy && ok "step_deploy with Cancel at the password prompt" || bad "step_deploy (cancel) failed"
gen=$(grep -F "docker-stdin: " "$LOG" | head -1 | cut -d' ' -f2-)
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

echo "== backups, restore, lock SSH"
reset "/mnt/backup" "resticpass-123" "resticpass-123" "no"; step_backup && ok "step_backup" || bad "step_backup failed"
renv="$T/etc/bookstack/restic.env"; u="$T/etc/systemd/system"
expect 'grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv" && grep -q "^RESTIC_PASSWORD=resticpass-123$" "$renv" && [ "$(stat -c %a "$renv" 2>/dev/null || stat -f %Lp "$renv")" = 600 ]' "restic.env written 0600 with repo + password"
expect 'grep -q "^OnCalendar=\*-\*-\* 01:00:00" "$u/bookstack-backup.timer" && grep -q "^OnFailure=bookstack-alert@backup.service" "$u/bookstack-backup.service"' "backup at 01:00 with OnFailure alert"
expect 'grep -q "^OnCalendar=monthly" "$u/bookstack-restore-test.timer" && grep -q "restore-test.sh" "$u/bookstack-restore-test.service" && grep -q "^OnFailure=bookstack-alert@restore-test.service" "$u/bookstack-restore-test.service"' "monthly restore-test timer with OnFailure alert"
expect 'grep -q "scripts/alert.sh" "$u/bookstack-alert@.service" && grep -q "%i" "$u/bookstack-alert@.service"' "templated bookstack-alert@.service"
expect 'seen "systemctl: enable --now bookstack-backup.timer bookstack-restore-test.timer" && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server"' "timers enabled; offsite-secrets checklist shown"
reset "s3:s3.example/bucket" "resticpass-123" "resticpass-123" "<cancel>"; step_backup; expect '[ $? = 1 ] && grep -q "^RESTIC_REPOSITORY=/mnt/backup$" "$renv"' "Cancel at the S3 key prompt aborts without touching restic.env"
TS_EXPIRY='"2027-03-01T00:00:00Z"'; reset "yes" "no"; step_lock_ssh; rc=$?; expect '[ $rc = 1 ] && ! seen "ufw: --force delete" && grep -F "msgbox" "$LOG" | grep -q "Disable key expiry"' "key expiry set + 'not done' -> SSH stays public, told what to do"
reset "yes" "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp"' "confirmed -> port 22 closed"; TS_EXPIRY=null
reset "yes"; step_lock_ssh; expect 'seen "ufw: --force delete allow 22/tcp" && ! grep -q "yesno: IMPORTANT" "$LOG"' "KeyExpiry null -> no extra prompt"
reset; step_tailscale >/dev/null; expect 'grep -F "msgbox" "$LOG" | grep -q "Disable key expiry"' "Tailscale step warns about key expiry"
# restore onto this server: stop, restic restore, rsync, DB copies per MANIFEST, keep fresh IPs, restart
bin="$T/bin"; mkdir -p "$bin" "$T/fakesnap$STACK_DIR/.backup-snap" "$T/fakesnap$STACK_DIR/cwa/config"
cat > "$bin/restic" <<'EOS'
#!/usr/bin/env bash
echo "restic: $*" >> "$RLOG"
case "$1" in
  restore) tgt=""; while [ $# -gt 0 ]; do [ "$1" = --target ] && tgt="$2"; shift; done; mkdir -p "$tgt"; cp -a "$FAKESNAP/." "$tgt/";;
  snapshots) case "$*" in *--json*) printf '[{"time":"%s","paths":["/srv/bookstack"]}]\n' "$(date -u +%Y-%m-%dT%H:%M:%S.123456789Z)";; esac;;
  stats) echo '{"total_size":1}';;
esac
exit 0
EOS
chmod +x "$bin/restic"; export RLOG="$T/restic.log" FAKESNAP="$T/fakesnap"; : > "$RLOG"
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
PATH="$bin:$PATH"; envset PUBLIC_IP 203.0.113.5; envset TAILSCALE_IP 100.64.0.1; envset TZ UTC; envset ABS_TOKEN old-token
reset "yes" "yes"; step_restore && ok "step_restore ran" || bad "step_restore failed"
expect 'grep -q "restic: restore latest --target" "$RLOG" && seen "docker: compose down"' "restore stops the stack and restores the latest snapshot"
expect '[ "$(envget ABS_TOKEN)" = from-snapshot ] && [ "$(envget PUBLIC_IP)" = 203.0.113.5 ] && [ "$(envget TAILSCALE_IP)" = 100.64.0.1 ] && [ "$(envget TZ)" = UTC ]' "snapshot .env restored, but this server's PUBLIC_IP / TAILSCALE_IP / TZ kept"
expect '[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from user\").fetchone()[0])" "$STACK_DIR/cwa/config/app.db")" = 1 ] && [ ! -f "$STACK_DIR/cwa/config/app.db-wal" ]' "consistent DB copy replaced the raw file per MANIFEST; stale -wal removed"
expect 'seen "docker: compose up -d" && [ "$(line_of "compose down")" -lt "$(line_of "compose up -d")" ] && [ -f "$T/etc/cron.d/bookstack-disk" ] && grep -F "msgbox" "$LOG" | grep -q "Keep these OFF this server"' "stack restarted, watchdog re-installed, checklist shown"
reset "yes" "no"; n_before=$(grep -c "restore latest" "$RLOG" || true); d_before=$(grep -c "compose down" "$LOG" || true)   # counts AFTER reset (it truncates the log)
step_restore; rc=$?
expect '[ "$rc" != 0 ] && [ "$(grep -c "restore latest" "$RLOG" || true)" = "$n_before" ] && [ "$(grep -c "compose down" "$LOG" || true)" = "$d_before" ]' "second confirmation declined -> nothing restored, stack not stopped"

echo "== update: pre-update backup, tag bump, health gate, rollback"
printf '#!/usr/bin/env bash\necho "backup.sh $*" >> "%s"\nexit ${BACKUP_RC:-0}\n' "$LOG" > "$STACK_DIR/scripts/backup.sh"
printf '#!/usr/bin/env bash\nexit ${SELFTEST_RC:-0}\n' > "$STACK_DIR/scripts/selftest.sh"; chmod +x "$STACK_DIR/scripts/"*.sh
export BACKUP_RC=1; reset; step_update; rc=$?; export BACKUP_RC=0
expect '[ $rc = 1 ] && seen "backup.sh --tag pre-update" && seen "Pre-update backup failed" && ! seen "compose pull"' "step_update returns 1 when the pre-update backup fails (nothing pulled)"
envset IMG_CWA crocodilestick/calibre-web-automated:v4.0.6
reset "yes" "crocodilestick/calibre-web-automated:v4.0.7" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "yes"; step_update && ok "step_update (bump one tag)" || bad "step_update failed"
expect '[ -f "$STACK_DIR/.images.prev" ] && grep -q "^IMG_CWA=crocodilestick/calibre-web-automated:v4.0.6$" "$STACK_DIR/.env.images.prev"' ".images.prev and .env.images.prev record the previous state"
expect '[ "$(envget IMG_CWA)" = crocodilestick/calibre-web-automated:v4.0.7 ] && grep -q "IMG_CWA: crocodilestick/calibre-web-automated:v4.0.6 -> crocodilestick/calibre-web-automated:v4.0.7" "$LOG"' "new tag stored and shown old -> new before applying"
expect 'seen "compose pull --ignore-buildable" && seen "compose build --pull caddy librarian" && seen "compose up -d" && seen "docker: system prune -f --filter until=72h" && [ "$(line_of "compose up -d")" -lt "$(line_of "system prune")" ]' "pull/build/up then prune only at the end"
export SELFTEST_RC=1; reset "no" "yes" "yes"; step_update; rc=$?; export SELFTEST_RC=0
expect '[ $rc = 1 ] && grep -F "yesno: Update problem" "$LOG" | grep -q "self-test reported failures" && [ "$(envget IMG_CWA)" = crocodilestick/calibre-web-automated:v4.0.7 ] && ! seen "system prune"' "failed self-test -> rollback offered, no prune"
envset IMG_CWA x/y:old; printf 'IMG_CWA=x/y:old\n' > "$STACK_DIR/.env.images.prev"
reset "yes" "x/y:new" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "no"; step_update
expect '[ "$(envget IMG_CWA)" = x/y:old ] && ! seen "compose pull"' "declining at the final confirmation restores the previous tags"
export SELFTEST_RC=1; reset "yes" "x/y:new" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "<cancel>" "yes" "yes"; step_update; export SELFTEST_RC=0
expect '[ "$(envget IMG_CWA)" = x/y:old ] && [ "$(grep -c "docker: compose up -d" "$LOG")" -ge 2 ]' "rollback restores the previous tags and restarts the stack"
rm -f "$renv"; reset "no"; step_update; expect '[ $? = 1 ] && seen "yesno: No backup repository is configured"' "without backups, Update asks and stops on No"

echo "== helper scripts as real processes (stub restic/docker on PATH)"
cat > "$bin/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker: $*" >> "$DLOG"; exit 0
EOS
for c in journalctl logger hostname; do printf '#!/usr/bin/env bash\necho "%s: $*" >> "$DLOG"; exit 0\n' "$c" > "$bin/$c"; done
chmod +x "$bin"/*; export DLOG="$T/docker.log"; : > "$DLOG"; : > "$RLOG"
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
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" PATH="$bin:$PATH" bash "$REPO/scripts/backup.sh" --tag pre-update >"$T/backup.out" 2>&1 && ok "backup.sh runs" || { bad "backup.sh failed"; cat "$T/backup.out"; }
expect '[ -f "$fs/.backup-snap/cwa_config_app.db" ] && [ -f "$fs/.backup-snap/library_books_metadata.db" ] && [ -f "$fs/.backup-snap/shelfmark_config_shelfmark.db" ] && grep -q "^cwa_config_app.db	cwa/config/app.db$" "$fs/.backup-snap/MANIFEST"' "consistent SQLite copies + MANIFEST (incl. shelfmark glob)"
expect '[ "$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from user\").fetchone()[0])" "$fs/.backup-snap/cwa_config_app.db")" = 1 ]' "snapshot copy contains the committed row (WAL-safe backup API)"
expect 'grep -q "restic: backup $fs --exclude $fs/downloads .*--exclude $fs/cwa/config/processed_books --exclude $fs/library/staging --exclude \*.db-wal --exclude \*.db-shm --exclude \*.sqlite-wal --exclude \*.sqlite-shm --tag bookstack --tag pre-update" "$RLOG"' "restic backup with the excludes and the extra tag"
expect '[ "$(line_of() { grep -nF -- "$1" "$RLOG" | head -1 | cut -d: -f1; }; line_of "restic: check")" -lt "$(grep -nF "restic: forget" "$RLOG" | head -1 | cut -d: -f1)" ] && grep -q "restic: forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --keep-tag pre-update --prune" "$RLOG" && grep -q "restic: stats latest --json" "$RLOG"' "restic check runs BEFORE forget --prune; stats logged; pre-update snapshots kept"
: > "$RLOG"; export FAKESNAP="$T/fakesnap2"; mkdir -p "$FAKESNAP$fs"; cp -a "$fs/." "$FAKESNAP$fs/"; printf 'DOMAIN=x\n' > "$FAKESNAP$fs/.env"; printf 'x\n' > "$FAKESNAP$fs/docker-compose.yml"; mkdir -p "$FAKESNAP$fs/caddy"; : > "$FAKESNAP$fs/caddy/Caddyfile"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" PATH="$bin:$PATH" bash "$REPO/scripts/restore-test.sh" >"$T/rt.out" 2>&1 && ok "restore-test.sh passes on a good snapshot" || { bad "restore-test.sh failed"; cat "$T/rt.out"; }
expect 'grep -q "integrity: cwa/config/app.db" "$T/rt.out" && grep -q "app.db has 1 user" "$T/rt.out" && grep -q "metadata.db has 1 book" "$T/rt.out" && grep -q "snapshot is 0 h old" "$T/rt.out"' "restore test checks integrity, user count, book count and snapshot age"
rm -f "$FAKESNAP$fs/.backup-snap/MANIFEST"
STACK_DIR="$fs" RESTIC_ENV="$T/restic.env" PATH="$bin:$PATH" bash "$REPO/scripts/restore-test.sh" >"$T/rt2.out" 2>&1; expect '[ $? != 0 ] && grep -q "missing .backup-snap/MANIFEST" "$T/rt2.out"' "restore test FAILS when the snapshot lacks the consistent copies"
# disk watchdog
cat > "$bin/df" <<'EOS'
#!/usr/bin/env bash
case "$*" in *--output=pcent*) printf 'Use%%\n %s%%\n' "$DF_PCT";; *--output=avail*) printf 'Avail\n1000000\n';; *) printf 'Filesystem Size Used Avail Use%% Mounted\n/dev/x 80G 70G 10G %s%% /\n' "$DF_PCT";; esac
EOS
chmod +x "$bin/df"
touch -t 202001010000 "$fs/downloads/incomplete/old.part" "$fs/library/staging/old.bin" "$fs/library/ingest/old.part"; touch "$fs/downloads/incomplete/new.part"
printf "QBIT_USER='admin'\nQBIT_PASS='pw'\n" > "$fs/.env"
: > "$DLOG"; DF_PCT=96 STACK_DIR="$fs" DISK_STATE="$T/disk.state" PATH="$bin:$PATH" bash "$REPO/scripts/disk-watch.sh" && ok "disk-watch.sh runs" || bad "disk-watch.sh failed"
expect 'grep -q "docker: compose stop shelfmark aria2" "$DLOG" && grep -q "^paused=1" "$T/disk.state" && grep -q "python -m notify alert Disk 96% full" "$DLOG" && grep -q " high$" "$DLOG"' "96 %: downloaders stopped, high-priority alert, state recorded"
expect '[ ! -f "$fs/downloads/incomplete/old.part" ] && [ ! -f "$fs/library/staging/old.bin" ] && [ ! -f "$fs/library/ingest/old.part" ] && [ -f "$fs/downloads/incomplete/new.part" ] && [ -f "$fs/library/ingest/stuck.epub" ]' "stale partials/staging deleted; fresh files and real ingest files kept"
expect 'grep -q "journalctl: --vacuum-size=200M" "$DLOG" && grep -q "docker: builder prune -f --filter until=168h" "$DLOG"' "journal and build cache trimmed"
: > "$DLOG"; DF_PCT=96 STACK_DIR="$fs" DISK_STATE="$T/disk.state" PATH="$bin:$PATH" bash "$REPO/scripts/disk-watch.sh"; expect '! grep -q "notify alert" "$DLOG"' "still 96 %: no repeated alert"
: > "$DLOG"; DF_PCT=50 STACK_DIR="$fs" DISK_STATE="$T/disk.state" PATH="$bin:$PATH" bash "$REPO/scripts/disk-watch.sh"; expect 'grep -q "docker: compose start shelfmark aria2" "$DLOG" && grep -q "^paused=0" "$T/disk.state"' "back under 80 %: downloaders started again"
grep -v '^last_alert=' "$T/disk.state" > "$T/disk.state.n"; mv "$T/disk.state.n" "$T/disk.state"   # pretend the last alert was long ago
: > "$DLOG"; DF_PCT=88 STACK_DIR="$fs" DISK_STATE="$T/disk.state" PATH="$bin:$PATH" bash "$REPO/scripts/disk-watch.sh"; expect '! grep -q "compose stop" "$DLOG" && grep -q "notify alert Disk 88% full" "$DLOG"' "88 %: alert only (once per 24 h)"
: > "$DLOG"; DF_PCT=88 STACK_DIR="$fs" DISK_STATE="$T/disk.state" PATH="$bin:$PATH" bash "$REPO/scripts/disk-watch.sh"; expect '! grep -q "notify alert" "$DLOG"' "88 % again within 24 h: silent"
: > "$DLOG"; PATH="$bin:$PATH" bash "$REPO/scripts/alert.sh" "T" "body" high; expect 'grep -q "docker: exec -i librarian python -m notify alert T body high" "$DLOG"' "alert.sh hands off to the portal's notify CLI"
printf '#!/usr/bin/env bash\nexit 1\n' > "$bin/docker"; : > "$DLOG"; PATH="$bin:$PATH" bash "$REPO/scripts/alert.sh" "T" "body"; rc=$?; expect '[ $rc = 0 ] && grep -q "logger: -t bookstack -p user.warning ALERT T: body" "$DLOG"' "alert.sh falls back to logger and never fails"
PATH="${PATH#"$bin:"}"

echo "== ephemera enable/disable"
envset TAILSCALE_IP 100.64.0.1; envset CF_API_TOKEN ""
reset "yes" "https://archive.example" "" "" "alice"; step_ephemera
expect '[ "$(envget EPHEMERA_ENABLED)" = true ] && [ "$(envget EPHEMERA_OWNER)" = alice ] && [ -d "$STACK_DIR/library/dropbox/alice" ] && seen "compose -f docker-compose.yml -f docker-compose.ephemera.yml build ephemera"' "ephemera enabled, owner dropbox, pinned build"
step_ephemera_off; expect '[ "$(envget EPHEMERA_ENABLED)" = false ] && seen "stop ephemera flaresolverr"' "ephemera disabled"

echo; echo "TUI RESULT: $pass passed, $fails failed"; exit $fails
