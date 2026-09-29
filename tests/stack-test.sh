#!/usr/bin/env bash
# End-to-end UAT: brings up the REAL stack (Calibre-Web Automated, Audiobookshelf, Shelfmark,
# portal) plus test doubles (GreenMail SMTP/IMAP, a file server, Authelia behind a plain-HTTP
# Caddy using the production forward_auth snippet) in a throwaway directory, writes .env
# through the installer's own envset, then drives every user journey (tests/e2e_driver.py).
# Also samples container memory so the VPS sizing advice is measured, not guessed.
# Needs Docker. Takes ~6-10 minutes. Leaves nothing behind.
#   bash tests/stack-test.sh            # run and clean up
#   KEEP=1 bash tests/stack-test.sh     # keep the stack running for inspection
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STACK="${STACK:-$(mktemp -d "${TMPDIR:-/tmp}/bookstack-e2e.XXXXXX")}"
PROJECT=bookstack-e2e
export STACK_DIR="$STACK" BOOKSTACK_LIB=1
# shellcheck source=../bookstack.sh
source "$REPO/bookstack.sh"          # envset/envget/authelia_add_user etc., nothing runs
set +e; set -uo pipefail

compose() { (cd "$STACK" && docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.test.yml "$@"); }
SAMPLER_PID=""
cleanup() {
  rc=$?
  [ -n "$SAMPLER_PID" ] && kill "$SAMPLER_PID" 2>/dev/null
  if [ -f "$STACK/stats.log" ]; then
    echo "== peak memory per container during the run (docker stats samples)"
    python3 - "$STACK/stats.log" <<'PY'
import sys, re, collections
peak = collections.defaultdict(float); n = 0
for line in open(sys.argv[1]):
    m = re.match(r"(\S+)\s+([\d.]+)(GiB|MiB|KiB)", line)
    if not m: continue
    v = float(m.group(2)) * {"GiB": 1024, "MiB": 1, "KiB": 1/1024}[m.group(3)]
    peak[m.group(1)] = max(peak[m.group(1)], v); n += 1
tot = 0
for k, v in sorted(peak.items(), key=lambda kv: -kv[1]):
    print(f"   {k:16s} {v:7.0f} MiB"); tot += v
print(f"   {'TOTAL (peaks)':16s} {tot:7.0f} MiB   ({n} samples)")
PY
  fi
  if [ "${KEEP:-0}" = 1 ]; then echo "KEEP=1: stack left running in $STACK (project $PROJECT)"; exit $rc; fi
  echo "-- tearing down"; compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$STACK"; exit $rc
}
trap cleanup EXIT

# ---- static assertions -------------------------------------------------------------------
# Config-file properties that no running container can demonstrate, checked before anything is
# built so a broken one fails in seconds instead of ten minutes. Each is a property a previous
# round got wrong; the reasoning lives next to the config itself.
sfail=0
sok(){ echo "   [ OK ] $1"; }
sbad(){ echo "   [FAIL] $1"; sfail=$((sfail+1)); }
echo "== static assertions (config files, no containers)"
TPL="$REPO/caddy/Caddyfile.template"
grep -q '/duplicates/invalidate-cache' "$TPL" \
  && sok "Caddyfile: /duplicates/invalidate-cache is in @cwa_admin_jobs" \
  || sbad "Caddyfile: /duplicates/invalidate-cache is NOT blocked (anonymous CSRF-exempt write into cwa.db)"
grep -q 'duplicates/invalidate-cache).*;' "$TPL" \
  && sok "Caddyfile: ...and in the ';'-parameter path_regexp companion" \
  || sbad "Caddyfile: /duplicates/invalidate-cache missing from the path_regexp alternation"
# /opds and /kosync must NOT share a rate-limit budget: OPDS needs hundreds a minute (one cover
# per entry), KOReader needs single digits and is the password oracle.
grep -q 'zone kosync_auth' "$TPL" \
  && sok "Caddyfile: /kosync has its own rate-limit zone" \
  || sbad "Caddyfile: /kosync has no zone of its own (it is back in the loose 300/min OPDS bucket)"
grep -qE '^\s*path /opds\* /kosync\*' "$TPL" \
  && sbad "Caddyfile: /opds and /kosync share one rate-limit matcher again" \
  || sok "Caddyfile: /opds and /kosync no longer share a matcher"
# The blanket no-store deletes every app's own cache directive; `private` must survive any relaxation.
# Both spellings: the default inside (hardening)'s header block, and the per-site
# `header @matcher >Cache-Control "..."` exceptions. Every one must contain `private`.
cc=$(grep -oE '>Cache-Control "[^"]*"' "$TPL")
if [ -n "$cc" ] && ! printf '%s\n' "$cc" | grep -qv private; then
  sok "Caddyfile: all $(printf '%s\n' "$cc" | grep -c .) >Cache-Control values keep 'private' (no shared-cache leak)"
else sbad "Caddyfile: a >Cache-Control value is missing 'private' — Cloudflare could cache one reader's response for everyone"; fi
# ABS 2.36.1 has no route under /s; an unanchored bypass of the SSO gate must not outlive its route.
grep -vE '^[[:space:]]*#' "$REPO/authelia/configuration.yml.template" | grep -q "'\^/s/" \
  && sbad "Authelia: the unjustified '^/s/.*' bypass is back (no such route in ABS 2.36.1)" \
  || sok "Authelia: no '/s/' bypass (ABS 2.36.1 has no route there)"
grep -vE '^[[:space:]]*#' "$REPO/authelia/inject-gate.py" | grep -q '/s/\*' \
  && sbad "inject-gate.py: the unjustified /s/* bypass is back" \
  || sok "inject-gate.py: no /s/* bypass"
# The snapshot must never carry the key that decrypts it or the credentials that can delete it.
grep -qE '^for p in .*restic\.env' "$REPO/scripts/backup.sh" \
  && sbad "backup.sh: /etc/bookstack/restic.env is staged verbatim again (RESTIC_PASSWORD and the S3 keys would be inside every snapshot)" \
  || sok "backup.sh: restic.env is not copied verbatim into the snapshot"
grep -q 'RESTIC_PASSWORD, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are deliberately absent' "$REPO/scripts/backup.sh" \
  && sok "backup.sh: writes the redacted restic.env stub instead" \
  || sbad "backup.sh: no redacted restic.env stub (a rebuild would not know which repository to open)"
# The .part reaper must not be able to delete a file the portal is still holding: _atomic_ingest
# copies up to 250 MB, tags it, and may run a synchronous 45 MB SMTP upload, all under the .part name.
pm=$(grep -oE "name '\*\.part' -mmin \+[0-9]+" "$REPO/scripts/disk-watch.sh" | grep -oE '[0-9]+$')
if [ -n "$pm" ] && [ "$pm" -ge 120 ]; then sok "disk-watch.sh: the .part reaper waits ${pm} min (past copy + tag + a slow Send-to-Kindle upload)"
else sbad "disk-watch.sh: the .part reaper window is ${pm:-?} min — it can delete a file worker.py is still writing"; fi
# The token table must not ask for a permission nothing uses.
grep -q '| Config Rules | Edit |' "$REPO/README.md" \
  && sbad "README: asks for Cloudflare 'Config Rules: Edit' again, but nothing creates a Configuration Rule" \
  || sok "README: no Config Rules permission requested (nothing uses it)"
grep -qE 'Size caps: 200 MB ebooks' "$REPO/README.md" \
  && sok "README: the ebook size cap matches MAX_EBOOK_MB=200" \
  || sbad "README: the ebook size cap disagrees with librarian/config.py's MAX_EBOOK_MB=200"
# The Shelfmark healthcheck must assert app.db is readable NOW, not only that the process
# started with it: the auth mode is resolved once at import and never re-checked.
grep -q 'SQLite format 3' "$REPO/docker-compose.yml" \
  && sok "compose: the shelfmark healthcheck re-checks app.db, not just the cached auth_mode" \
  || sbad "compose: the shelfmark healthcheck only reads auth_mode, which stays 'cwa' after app.db vanishes"
[ "$sfail" = 0 ] || { echo "== $sfail static assertion(s) FAILED"; exit 1; }

echo "== stack dir: $STACK"
mkdir -p "$STACK"; cd "$STACK"
cp "$REPO/docker-compose.yml" "$REPO/tests/docker-compose.test.yml" .
rsync -a --exclude tests --exclude __pycache__ "$REPO/librarian/" librarian/
rsync -a --exclude db.sqlite3 --exclude notification.txt --exclude configuration.yml "$REPO/authelia/" authelia/
mkdir -p cwa/config abs/config abs/metadata shelfmark/config librarian/state testfiles caddy-test \
         library/{books,ingest,audiobooks,podcasts,staging,dropbox}
# .env exactly as the installer writes it (quoted values incl. a hostile secret)
envset DOMAIN example.test; envset ADMIN_EMAIL admin@example.test; envset TZ UTC
envset PUID "$(id -u)"; envset PGID "$(id -g)"; envset CF_API_TOKEN dummy; envset CF_DNS_TOKEN dummy
envset LIBRARIAN_SECRET 'pa$$w0rd&|it'"'"'s"tricky'; envset INTAKE_TOKEN e2e-intake; envset PUBLIC_IP 127.0.0.1; envset BIND_IP 127.0.0.1; envset TAILSCALE_IP 127.0.0.1
envset COMICS_ENABLED true             # v5.7 comics (docs/COMICS.md): the section after the canary journey
envset ADMIN_HASH '$2a$14$hash'; envset APPROVALS_REQUIRED true; envset SHELFMARK_LANGUAGE en; envset AUTHELIA_ENABLED true
# L16: the portal's service login for Shelfmark's approval API (bookstack.sh ensure_shelfmark_service)
SVC_PW="svc-e2e-$(date +%s)-pw"; envset SHELFMARK_SVC_USER svc-portal; envset SHELFMARK_SVC_PASS "$SVC_PW"
compose config -q || { echo "compose files do not render"; exit 1; }

# Authelia: same rendering the installer does (Security -> Authelia), plus one user via the
# installer's own authelia_add_user (validates users_database.yml + argon2 hash for real)
# L05: the OpenID Connect provider for Audiobookshelf, rendered by the installer's own function
envset ABS_OIDC_SECRET e2e-abs-oidc-secret-0123456789abcdef; envset AUTHELIA_OIDC_HMAC e2e-oidc-hmac-0123456789abcdef0123456789abcdef
( umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out authelia/oidc-jwks.pem 2>/dev/null )
render_authelia_config >/dev/null 2>&1; grep -q "client_id: 'audiobookshelf'" authelia/configuration.yml || { echo "render_authelia_config did not render the OIDC client"; exit 1; }
# one factor for the harness: the driver signs in with a password, it cannot enrol TOTP. The rules
# as rendered are kept beside it: section 14c swaps them in to prove admins DO need a second factor
cp authelia/configuration.yml authelia/configuration.production.yml.keep
sed -i.bak -e "s|policy: two_factor|policy: one_factor|" -e "s|policy: 'two_factor'|policy: 'one_factor'|" authelia/configuration.yml && rm -f authelia/configuration.yml.bak
# Authelia's OpenID endpoints only work over https, and Audiobookshelf's server calls them: a
# throwaway CA and a certificate for auth.example.test, served by the gate, trusted by ABS
mkdir -p tls
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=bookstack e2e CA" -keyout tls/ca.key -out tls/ca.pem \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj "/CN=auth.example.test" -keyout tls/auth.key -out tls/auth.csr >/dev/null 2>&1
printf 'subjectAltName=DNS:auth.example.test\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > tls/ext
openssl x509 -req -in tls/auth.csr -CA tls/ca.pem -CAkey tls/ca.key -CAcreateserial -days 2 -extfile tls/ext -out tls/auth.crt >/dev/null 2>&1
chmod 644 tls/*.pem tls/*.crt tls/*.key
echo "users: {}" > authelia/users_database.yml
authelia_add_user alice Alice alice@example.test 'alice-authelia-pw1' >/dev/null 2>&1 || { echo "authelia_add_user failed"; exit 1; }
authelia_add_user admin Admin admin@example.test 'admin-authelia-pw1' >/dev/null 2>&1 || { echo "authelia_add_user admin failed"; exit 1; }
# Plain-HTTP Caddy "gate": the production per-host markers + the production snippet, injected
# by the production injector (authelia/inject-gate.py), then two test-only substitutions:
# (a) the Authelia address (127.0.0.1 on the host network in prod -> the service name here)
# (b) X-Forwarded-Proto pinned to https, emulating the TLS termination Authelia insists on
#     (it refuses to issue redirects for http:// targets so its session cookie stays secure).
cat > caddy-test/Caddyfile <<'EOF'
{
	auto_https off
	admin off
	default_sni auth.example.test
}
https://auth.example.test {
	tls /certs/auth.crt /certs/auth.key
	reverse_proxy authelia:9091
}
http://shelf.example.test {
	# @AUTHELIA_GATE:shelf@
	reverse_proxy shelfmark:8084
}
http://books.example.test {
	# @AUTHELIA_GATE:books@
	reverse_proxy calibre-web:8083
}
http://audio.example.test {
	# @AUTHELIA_GATE:audio@
	reverse_proxy audiobookshelf:80
}
http://request.example.test {
	# @AUTHELIA_GATE:request@
	reverse_proxy librarian:8090
}
http://auth.example.test {
	reverse_proxy authelia:9091
}
# @HOME_SITE@
EOF
# Give the test gate the SAME books. route that 403s CWA's unauthenticated admin-job endpoints
# (convert-library, epub-fixer, cwa-logs, cwa-internal, /reconnect), copied from the production
# template so the two can never drift. Must run before the gate is injected (it replaces the marker).
python3 - caddy-test/Caddyfile "$REPO/caddy/Caddyfile.template" <<'PY'
import sys, re
p, tpl_path = sys.argv[1], sys.argv[2]
s, tpl = open(p).read(), open(tpl_path).read()
m = re.search(r"\n\troute \{\n\t\t@cwa_admin_jobs .*?\n\t\}\n", tpl, re.S)
if not m:
    sys.exit("could not find the @cwa_admin_jobs route in caddy/Caddyfile.template")
block = re.sub(r"^\t", "", m.group(0), flags=re.M).lstrip("\n")
block = block.replace("@@KOBO_RS_BLOCK@@", "")          # what render_caddyfile writes for CWA >= v4.0.7
marker = "\t# @AUTHELIA_GATE:books@"
if marker not in s:
    sys.exit("books gate marker missing from the test Caddyfile")
open(p, "w").write(s.replace(marker, "\t" + block.replace("\n", "\n") + marker, 1))
print("   gate: copied the CWA admin-jobs 403 route from the production template")
PY

# v6.0: the start page, as the production template writes it (its ONE route: gate, then the start
# page or the redirect to request.), with only the addresses changed for this network
python3 - caddy-test/Caddyfile "$REPO/caddy/Caddyfile.template" <<'PY'
import sys, re
p, tpl_path = sys.argv[1], sys.argv[2]
s, tpl = open(p).read(), open(tpl_path).read()
m = re.search(r"\nhome\.@@DOMAIN@@ \{\n.*?\n(\troute \{\n.*?\n\t\})\n\}\n", tpl, re.S)
if not m:
    sys.exit("could not find home.'s route in caddy/Caddyfile.template")
route = (m.group(1).replace("127.0.0.1:8090", "librarian:8090")
         .replace("https://request.@@DOMAIN@@", "http://request.example.test").replace("@@DOMAIN@@", "example.test"))
open(p, "w").write(s.replace("# @HOME_SITE@", "http://home.example.test {\n" + route + "\n}", 1))
print("   gate: home. site copied from the production template")
PY
python3 "$REPO/authelia/inject-gate.py" caddy-test/Caddyfile authelia/caddy-gate.snippet || { echo "inject-gate.py failed"; exit 1; }
python3 - caddy-test/Caddyfile <<'PY'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = s.replace("127.0.0.1:9091", "authelia:9091")
s = re.sub(r"^(\t+)(uri /api/authz/forward-auth)$", r"\1\2\n\1header_up X-Forwarded-Proto https", s, flags=re.M)
open(p, "w").write(s)
print("   gate: %d forward_auth blocks, %d bypass matchers" % (s.count("forward_auth"), s.count("not path")))
PY

echo "== building portal image"; compose build -q librarian || { echo "portal image build failed"; exit 1; }
echo "== starting calibre-web (waits for its healthcheck)"; compose up -d calibre-web
for _ in $(seq 1 120); do [ -f cwa/config/app.db ] && curl -fs -o /dev/null http://127.0.0.1:18083/login && break; sleep 2; done
curl -fs -o /dev/null http://127.0.0.1:18083/login || { echo "CWA never came up"; compose logs calibre-web | tail -30; exit 1; }
# the same CWA conversion policy apply_library_defaults sets on a real deploy (convert to EPUB,
# keep per-user copies, Kindle fixer, leave PDF/comics in their native format)
docker exec -i calibre-web sqlite3 /config/cwa.db "UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='epub', auto_ingest_automerge='new_record', kindle_epub_fixer=0, auto_convert_ignored_formats='pdf,cbz,cbr,cb7', auto_backup_imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0, koreader_sync_enabled=1;" \
  && echo "   [ OK ] CWA conversion defaults applied (as apply_library_defaults does)" || echo "   [FAIL] could not apply CWA defaults"
# L05: Calibre-Web trusts Remote-User behind the gate (what gate_sso_on runs), then a restart
compose run --rm --no-deps -T librarian python -m cwa proxy-login on >/dev/null 2>&1 && compose restart calibre-web >/dev/null 2>&1 \
  && echo "   [ OK ] Calibre-Web header login switched on through the portal CLI (L05)" || echo "   [FAIL] cwa proxy-login on"
for _ in $(seq 1 120); do curl -fs -o /dev/null http://127.0.0.1:18083/login && break; sleep 2; done
echo "== starting audiobookshelf and bootstrapping it through the portal image CLI (what Library -> Audiobookshelf runs)"
compose up -d audiobookshelf
for _ in $(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:23378/healthcheck && break; sleep 2; done
absout=$(compose run --rm --no-deps -T librarian python -m abs init --user root --password rootpass-e2e1 2>&1 | tail -1)
abskey=$(printf '%s' "$absout" | python3 -c 'import sys,json; print(json.load(sys.stdin)["api_key"])' 2>/dev/null || true)
if [ -n "$abskey" ]; then envset ABS_TOKEN "$abskey"; echo "   [ OK ] python -m abs init: root + API key + library"; else echo "   [FAIL] abs init: $absout"; fi
# v5.6: Audiobookshelf's own nightly database copy, switched on the way Deploy does it
bout=$(compose run --rm --no-deps -T -e ABS_TOKEN="$abskey" librarian python -m abs backups 2>&1 | tail -1)
printf '%s' "$bout" | grep -q '"backupSchedule": "30 2 \* \* \*", "backupsToKeep": 3, "maxBackupSize": 1' \
  && echo "   [ OK ] python -m abs backups: Audiobookshelf keeps 3 nightly copies of its database (read back from it)" \
  || { echo "   [FAIL] abs backups: $bout"; pre_fail=$(( ${pre_fail:-0} + 1 )); }
echo "== starting the rest"; compose up -d greenmail filesrv authelia librarian shelfmark gate
# memory sampler (every 5 s) for the sizing table
( while true; do docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null | sed 's|/.*||'; sleep 5; done ) > stats.log 2>/dev/null &
SAMPLER_PID=$!
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:18090/healthz >/dev/null 2>&1 && break; sleep 2; done
curl -fs http://127.0.0.1:18090/healthz >/dev/null || { echo "portal not healthy"; compose logs librarian | tail -40; exit 1; }
printf '%s\n' "$SVC_PW" | docker exec -i librarian python -m cwa add-user svc-portal --email svc-portal@localhost --password-stdin --admin >/dev/null 2>&1 \
  && echo "   [ OK ] Shelfmark service account created (L16)" || echo "   [FAIL] could not create the Shelfmark service account"
got=$(docker exec librarian printenv LIBRARIAN_SECRET)
[ "$got" = 'pa$$w0rd&|it'"'"'s"tricky' ] && echo "   [ OK ] quoted .env value reaches the container intact" || { echo "   [FAIL] .env quoting broken: got [$got]"; exit 1; }
for _ in $(seq 1 60); do curl -fs http://127.0.0.1:18084/api/health >/dev/null 2>&1 && break; sleep 2; done
for _ in $(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:23378/healthcheck && break; sleep 2; done
for _ in $(seq 1 60); do (echo > /dev/tcp/127.0.0.1/13025) >/dev/null 2>&1 && break; sleep 2; done
for _ in $(seq 1 30); do curl -fs -o /dev/null -H 'Host: auth.example.test' http://127.0.0.1:18080/ && break; sleep 2; done
# a real, tiny MP3 for the audiobook journey, made with Audiobookshelf's own ffmpeg
docker exec audiobookshelf ffmpeg -loglevel error -f lavfi -i anullsrc=r=22050:cl=mono -t 2 -q:a 9 -y /tmp/silence.mp3 \
  && docker cp audiobookshelf:/tmp/silence.mp3 testfiles/silence.mp3 || echo "(could not produce a test mp3; audiobook journey will be skipped)"
sleep 3
echo "== driving the user journeys"
python3 "$REPO/tests/e2e_driver.py" "$STACK"; rc=$?
rc=$(( rc + ${pre_fail:-0} ))           # set-up checks that failed before the driver ran
echo "== L08: the canary journey (scripts/synthetic.py) against this stack"
CE="$STACK/canary.env"; : > "$CE"; cok=1
for n in canary-a canary-b; do
  pw=$(openssl rand -hex 12); k=$([ "$n" = canary-a ] && echo A || echo B)
  printf '%s\n' "$pw" | docker exec -i librarian python -m cwa add-user "$n" --password-stdin --no-abs >/dev/null 2>&1 || cok=0
  printf 'CANARY_%s=%s\nCANARY_%s_PW=%s\n' "$k" "$n" "$k" "$pw" >> "$CE"
done
[ $cok = 1 ] && echo "   [ OK ] canary accounts created (no ABS account)" || { echo "   [FAIL] could not create the canary accounts"; rc=$((rc+1)); }
printf '#!/usr/bin/env bash\necho "ALERT $1" >> "%s"\n' "$STACK/canary-alert.log" > "$STACK/canary-alert.sh"
printf '#!/usr/bin/env bash\necho "PUSH $*" >> "%s"\n' "$STACK/canary-push.log" > "$STACK/canary-push.sh"; chmod +x "$STACK/canary-alert.sh" "$STACK/canary-push.sh"
canary(){ STACK_DIR="$STACK" CANARY_ENV="$CE" CANARY_PORTAL=http://127.0.0.1:18090 CANARY_PUBLIC_PORTAL=http://127.0.0.1:18090 \
  CANARY_PUBLIC_BOOKS=http://127.0.0.1:18083 CANARY_SHELF="${1:-http://127.0.0.1:18084}" CANARY_ALERT="$STACK/canary-alert.sh" \
  CANARY_KUMA_PUSH="$STACK/canary-push.sh" CANARY_IMPORT_TIMEOUT=300 python3 "$REPO/scripts/synthetic.py"; }
: > "$STACK/canary-alert.log"; : > "$STACK/canary-push.log"
canary; crc=$?
left=$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).execute("select count(*) from books where title like ?", ("Canary %",)).fetchone()[0])' "$STACK/library/books/metadata.db" 2>/dev/null)
last=$(docker exec librarian python -m admin_cli canary recent --limit 1 2>/dev/null | tail -1)
if [ $crc = 0 ] && grep -q "PUSH canary up" "$STACK/canary-push.log" && [ ! -s "$STACK/canary-alert.log" ] && [ "$left" = 0 ] \
   && printf '%s' "$last" | python3 -c 'import json,sys; r=json.load(sys.stdin)["rows"][0]; sys.exit(0 if r["ok"] and r["import_secs"] is not None and len(r["steps"]) >= 8 else 1)'; then
  echo "   [ OK ] the canary journey passes on a healthy stack: recorded with its import time, Kuma told, its book removed again"
else echo "   [FAIL] canary journey on a healthy stack (exit $crc, left $left book(s), last run: ${last:0:300})"; cat "$STACK/canary-alert.log"; rc=$((rc+1)); fi
: > "$STACK/canary-alert.log"; : > "$STACK/canary-push.log"
canary http://127.0.0.1:9 >/dev/null; crc=$?
if [ $crc = 1 ] && grep -q "ALERT Bookstack: canary journey FAILED at 'Shelfmark login'" "$STACK/canary-alert.log" && grep -q "PUSH canary down" "$STACK/canary-push.log" \
   && docker exec librarian python -m admin_cli canary recent --limit 1 | tail -1 | grep -q '"failed": "Shelfmark login"'; then
  echo "   [ OK ] a broken step fails the run, names the step in the alert, pushes DOWN to Kuma and shows on /admin"
else echo "   [FAIL] canary journey with Shelfmark unreachable (exit $crc)"; cat "$STACK/canary-alert.log" "$STACK/canary-push.log"; rc=$((rc+1)); fi
echo "== v5.7: a comic, from a reader's dropbox to her Kobo and her Kindle (docs/COMICS.md)"
cfail(){ echo "   [FAIL] $1"; rc=$((rc+1)); }
python3 - "$STACK/testfiles/comic.cbz" <<'PY'
import struct, sys, zipfile, zlib
def png(w, h, i):
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in ((x * 7 + i * 40) % 256, (y * 5) % 256, (x + y + i * 90) % 256)) for y in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(rows, 6)) + ch(b"IEND", b"")
with zipfile.ZipFile(sys.argv[1], "w") as z:
    for i in range(40):                      # v5.9: a volume under 40 pages is held as "a chapter, not a volume?"
        z.writestr(f"{i + 1:03d}.png", png(600, 900, i))
with zipfile.ZipFile(sys.argv[1].replace(".cbz", "-short.cbz"), "w") as z:
    for i in range(12):
        z.writestr(f"{i + 1:03d}.png", png(600, 900, i))
PY
crid=$(docker exec -i librarian python - <<'PY'
import db
db.init()
rid, _ = db.comic_add("alice", {"provider": "mangaupdates", "series_id": "990001", "series_name": "E2E Manga", "kind": "manga",
                                "reading": "rtl", "number": "3", "label": "Vol. 3", "language": "en", "authors": ["E2E Mangaka"]})
db.comic_update(rid, status="downloading", release_title="E2E Manga v03 (Digital)")
print(rid)
PY
)
mkdir -p "$STACK/library/dropbox/alice"
# v6.0.1: named the way the live server's Peanuts came (Shelfmark's 'Author - Title' with no author,
# a year range after the volume): it must still be matched to the request, as volume 3
cp "$STACK/testfiles/comic.cbz" "$STACK/library/dropbox/alice/ - E2E Manga v03 - 2019 to 2021 (Digital) (e2e).cbz"
cbid=""
for _ in $(seq 1 60); do
  cbid=$(python3 - "$STACK/library/books/metadata.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
r = c.execute("SELECT b.id FROM books b JOIN books_series_link l ON l.book=b.id JOIN series s ON s.id=l.series "
              "WHERE s.name='E2E Manga' AND b.series_index=3").fetchone()
print(r[0] if r else "")
PY
)
  [ -n "$cbid" ] && break; sleep 3
done
if [ -n "$cbid" ]; then
  echo "   [ OK ] the volume arrived in Calibre as E2E Manga #3 (book $cbid), the series read from the file"
  facts=$(python3 - "$STACK/library/books/metadata.db" "$cbid" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
tags = sorted(t for (t,) in c.execute("SELECT t.name FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=?", (sys.argv[2],)))
fmts = sorted(f for (f,) in c.execute("SELECT format FROM data WHERE book=?", (sys.argv[2],)))
print("|".join(tags), "|".join(fmts))
PY
)
  case "$facts" in *Manga*owner:alice*" CBZ") echo "   [ OK ] tagged Manga and owner:alice, kept as CBZ ($facts)";; *) cfail "tags/formats after import: $facts";; esac
  st=$(docker exec librarian python -c "import db; db.init(); print(db.comic_get($crid)['status'])")
  [ "$st" = done ] && echo "   [ OK ] alice's comic request reads 'done'" || cfail "comic request status '$st'"
  docker exec librarian python -c "import db; db.init(); db.comic_convert_force($cbid)"
  t0=$(date +%s)
  COMICS_ENABLED=true COMIC_METAPUSH_LOCK="$STACK/metapush.lock" STACK_DIR="$STACK" bash "$REPO/scripts/comic-convert.sh" > "$STACK/comic-convert.log" 2>&1
  t1=$(date +%s)
  # through calibredb, inside the container: Calibre-Web's latest writes sit in the WAL, which a
  # reader on the Mac side of OrbStack's VM does not see (on the server both share one kernel)
  kfile=$(docker exec -u "$(id -u):$(id -g)" -e HOME=/tmp calibre-web /app/calibre/calibredb list --fields formats \
          --search "id:$cbid" --for-machine --with-library /calibre-library 2>/dev/null | python3 -c '
import json, sys
out = sys.stdin.read(); rows = json.loads(out[out.find("["):]) if "[" in out else []
print(next((f[len("/calibre-library/"):] for r in rows for f in r.get("formats", []) if f.endswith(".kepub")), ""))')
  if [ -n "$kfile" ] && [ -f "$STACK/library/books/$kfile" ]; then
    echo "   [ OK ] KCC made the Kobo copy and it was added to the same book as KEPUB ($((t1 - t0)) s)"
    python3 - "$STACK/library/books/$kfile" <<'PY' && echo "   [ OK ] the Kobo copy is fixed-layout and in colour" || cfail "the Kobo copy is not a colour fixed-layout EPUB"
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
opf = next(n for n in z.namelist() if n.endswith(".opf"))
ok_layout = b"pre-paginated" in z.read(opf)
jpg = next(n for n in z.namelist() if n.lower().endswith((".jpg", ".jpeg")))
d = z.read(jpg); i = d.find(b"\xff\xc0") if d.find(b"\xff\xc0") > 0 else d.find(b"\xff\xc2")
colour = d[i + 9] == 3 if i > 0 else False
sys.exit(0 if ok_layout and colour else 1)
PY
  else
    cfail "no KEPUB after comic-convert.sh: $(tail -3 "$STACK/comic-convert.log")"
  fi
  tok=$(docker exec librarian python -m cwa kobo-url alice 2>/dev/null | grep -o '[0-9a-f]\{32\}' | head -1)
  btok=$(docker exec librarian python -m cwa kobo-url bob 2>/dev/null | grep -o '[0-9a-f]\{32\}' | head -1)
  kobo_fmt(){ curl -s -m 30 -H "User-Agent: Kobo eReader" -H "x-kobo-synctoken: " "http://127.0.0.1:18083/kobo/$1/v1/library/sync" | python3 -c '
import json, sys
fmts = []
for e in json.load(sys.stdin) or []:
    ent = e.get("NewEntitlement") or e.get("ChangedEntitlement") or {}
    md = ent.get("BookMetadata") or {}
    if "E2E Manga" in (md.get("Title") or ""):
        fmts += [u.get("Format") for u in md.get("DownloadUrls") or []]
print(",".join(f for f in fmts if f))'; }
  # Calibre-Web notices calibredb's write a little later (a real Kobo syncs long after): ask for a minute
  af=""; for _ in $(seq 1 20); do af=$(kobo_fmt "$tok"); [ -n "$af" ] && break; sleep 3; done
  bf=$(kobo_fmt "$btok")
  case "$af" in *EPUB3FL*|*KEPUB*) echo "   [ OK ] alice's Kobo is offered the comic as $af (fixed layout, through her existing link)";; *) cfail "alice's Kobo sync offers the comic as '${af:-nothing}'";; esac
  [ -z "$bf" ] && echo "   [ OK ] bob's Kobo is not offered alice's comic" || cfail "bob's Kobo sees alice's comic ($bf)"
  # v5.8: the Kobo says alice finished it (the request a real Kobo makes); the portal reads it back
  uuid=$(curl -s -m 30 -H "User-Agent: Kobo eReader" "http://127.0.0.1:18083/kobo/$tok/v1/library/sync" | python3 -c '
import json, sys
for e in json.load(sys.stdin) or []:
    ent = e.get("NewEntitlement") or e.get("ChangedEntitlement") or {}
    md = ent.get("BookMetadata") or {}
    if "E2E Manga" in (md.get("Title") or ""):
        print(md.get("EntitlementId") or ""); break')
  [ -z "$uuid" ] && uuid=$(docker exec -u "$(id -u):$(id -g)" -e HOME=/tmp calibre-web /app/calibre/calibredb list --fields uuid --search "id:$cbid" --for-machine --with-library /calibre-library 2>/dev/null | python3 -c 'import json,sys; o=sys.stdin.read(); print(json.loads(o[o.find("["):])[0]["uuid"])' 2>/dev/null)
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  st=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X PUT -H "User-Agent: Kobo eReader" -H "Content-Type: application/json" \
       "http://127.0.0.1:18083/kobo/$tok/v1/library/$uuid/state" --data "{\"ReadingStates\": [{\"EntitlementId\": \"$uuid\", \"LastModified\": \"$now\",
       \"StatusInfo\": {\"Status\": \"Finished\", \"LastModified\": \"$now\"},
       \"CurrentBookmark\": {\"ProgressPercent\": 100, \"ContentSourceProgressPercent\": 100, \"LastModified\": \"$now\"},
       \"Statistics\": {\"SpentReadingMinutes\": 12, \"RemainingTimeMinutes\": 0, \"LastModified\": \"$now\"}}]}")
  rs=$(docker exec librarian python -c "import cwa, anilist; print(cwa.reading_state('alice').get($cbid), anilist.finished_volumes('alice'))")
  case "$rs" in *"'status': 'read'"*"'E2E Manga': 3"*) echo "   [ OK ] v5.8: alice's Kobo said 'Finished' ($st); the portal reads it as Read, and AniList would count volume 3";;
    *) cfail "reading status after the Kobo's Finished (HTTP $st, uuid ${uuid:-none}): $rs";; esac
  kjob=$(docker exec librarian python -c "import db, comics; db.init(); print(comics.kindle_request('alice', False, $cbid, 'E2E Manga Vol. 3'))")
  COMICS_ENABLED=true COMIC_METAPUSH_LOCK="$STACK/metapush.lock" STACK_DIR="$STACK" bash "$REPO/scripts/comic-convert.sh" >> "$STACK/comic-convert.log" 2>&1
  kj=$(docker exec librarian python -c "import db, json; db.init(); j=[x for x in db.kindle_recent('alice', 5) if x['id']==$kjob][0]; print(j['status'], j.get('files'))")
  kdir="$STACK/library/staging/kindle-comics/$kjob"
  if [ "${kj%% *}" = queued ] && ls "$kdir"/*.epub >/dev/null 2>&1 && [ -z "$(find "$kdir" -name '*.epub' -size +45M)" ]; then
    echo "   [ OK ] a Kindle copy (Colorsoft, Send-to-Kindle EPUB) was made under the mail limit and handed to the portal to mail"
  else cfail "Kindle comic job: $kj; $(tail -3 "$STACK/comic-convert.log")"; fi
else
  cfail "the comic never reached Calibre: $(docker exec librarian python -c "import db; db.init(); print(db.comic_get($crid))" 2>&1 | tail -1)"
fi
# v5.9: a file that is not the volume asked for (12 pages: a chapter) is held for the reader, not imported
hrid=$(docker exec -i librarian python - <<'PY'
import db
db.init()
rid, _ = db.comic_add("alice", {"provider": "mangaupdates", "series_id": "990001", "series_name": "E2E Manga", "kind": "manga",
                                "reading": "rtl", "number": "4", "label": "Vol. 4", "language": "en"})
db.comic_update(rid, status="downloading", release_title="E2E Manga v04 (Digital)")
print(rid)
PY
)
cp "$STACK/testfiles/comic-short.cbz" "$STACK/library/dropbox/alice/E2E Manga v04 (Digital).cbz"
hst=""
for _ in $(seq 1 40); do
  hst=$(docker exec librarian python -c "import db; db.init(); r = db.comic_get($hrid); print(r['status'], r['held_path'] or '')")
  case "$hst" in held*) break;; esac; sleep 3
done
case "$hst" in "held /"*) echo "   [ OK ] a 12-page 'volume' is held for alice to check, not imported ($(docker exec librarian python -c "import db; db.init(); print(db.comic_get($hrid)['detail'][-60:])"))";;
  *) cfail "the short file was not held: '$hst'";; esac
[ ! -e "$STACK/library/dropbox/alice/E2E Manga v04 (Digital).cbz" ] && echo "   [ OK ] and it left her dropbox" || cfail "the held file is still in the dropbox"
echo "== process stability"
if compose logs librarian 2>/dev/null | grep -qE 'SIGBUS|SIGSEGV|Worker failed to boot|Fatal Python error'; then
  echo "   [FAIL] the portal worker crashed during the run:"; compose logs librarian 2>/dev/null | grep -B2 -A25 -E 'SIGBUS|SIGSEGV|Fatal Python error' | head -60; rc=$((rc+1))
else echo "   [ OK ] no portal worker crashes"; fi
for c in calibre-web audiobookshelf shelfmark; do
  n=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null || echo 0); [ "$n" = 0 ] && echo "   [ OK ] $c never restarted" || { echo "   [FAIL] $c restarted $n time(s)"; rc=$((rc+1)); }
done
if [ $rc != 0 ]; then
  echo "-- portal log tail:"; compose logs --tail 60 librarian | grep -v healthz
  echo "-- CWA ingest log tail:"; compose logs --tail 40 calibre-web | grep -iE 'ingest|import|convert|error' || true
  echo "-- gate/authelia log tail:"; compose logs --tail 20 gate authelia
fi
exit $rc
