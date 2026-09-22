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

echo "== stack dir: $STACK"
mkdir -p "$STACK"; cd "$STACK"
cp "$REPO/docker-compose.yml" "$REPO/tests/docker-compose.test.yml" .
rsync -a --exclude tests --exclude __pycache__ "$REPO/librarian/" librarian/
rsync -a --exclude db.sqlite3 --exclude notification.txt --exclude configuration.yml "$REPO/authelia/" authelia/
mkdir -p cwa/config abs/config abs/metadata shelfmark/config librarian/state testfiles caddy-test \
         library/{books,ingest,audiobooks,podcasts,staging,dropbox}
# .env exactly as the installer writes it (quoted values incl. a hostile secret)
envset DOMAIN example.test; envset ADMIN_EMAIL admin@example.test; envset TZ UTC
envset PUID "$(id -u)"; envset PGID "$(id -g)"; envset CF_API_TOKEN dummy; envset ARIA2_SECRET dummy
envset LIBRARIAN_SECRET 'pa$$w0rd&|it'"'"'s"tricky'; envset INTAKE_TOKEN e2e-intake; envset PUBLIC_IP 127.0.0.1; envset TAILSCALE_IP 127.0.0.1
envset ADMIN_HASH '$2a$14$hash'; envset APPROVALS_REQUIRED true; envset SHELFMARK_LANGUAGE en; envset AUTHELIA_ENABLED true
compose config -q || { echo "compose files do not render"; exit 1; }

# Authelia: same rendering the installer does (Security -> Authelia), plus one user via the
# installer's own authelia_add_user (validates users_database.yml + argon2 hash for real)
sed "s|@@DOMAIN@@|example.test|g" authelia/configuration.yml.template > authelia/configuration.yml
echo "users: {}" > authelia/users_database.yml
authelia_add_user alice Alice alice@example.test 'alice-authelia-pw1' >/dev/null 2>&1 || { echo "authelia_add_user failed"; exit 1; }
# Plain-HTTP Caddy "gate": the production per-host markers + the production snippet, injected
# by the production injector (authelia/inject-gate.py), then two test-only substitutions:
# (a) the Authelia address (127.0.0.1 on the host network in prod -> the service name here)
# (b) X-Forwarded-Proto pinned to https, emulating the TLS termination Authelia insists on
#     (it refuses to issue redirects for http:// targets so its session cookie stays secure).
cat > caddy-test/Caddyfile <<'EOF'
{
	auto_https off
	admin off
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
docker exec -i calibre-web sqlite3 /config/cwa.db "UPDATE cwa_settings SET auto_convert=1, auto_convert_target_format='epub', auto_ingest_automerge='new_record', kindle_epub_fixer=0, auto_convert_ignored_formats='pdf,cbz,cbr,cb7', auto_backup_imports=0, auto_backup_conversions=0, auto_backup_epub_fixes=0;" \
  && echo "   [ OK ] CWA conversion defaults applied (as apply_library_defaults does)" || echo "   [FAIL] could not apply CWA defaults"
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
