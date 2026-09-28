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
# one factor for the harness: the driver signs in with a password, it cannot enrol TOTP
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
