#!/usr/bin/env bash
# v5.9.1: Uptime Kuma 1.23.17 -> 2.5.5-slim on the SAME data, the way Operations -> Update does it
# (bookstack.sh kuma_v2_prepare). Proves: the bootstrap manages a 1.x Kuma with the new library
# without churn, 2.x migrates the 1.x database on first start, the bootstrap waits for that and
# then finds every monitor, the notification and the maintenance window in place (nothing added,
# nothing deleted), push monitors still accept their tokens, and a third run is a no-op.
#   bash tests/kuma-upgrade-test.sh        (needs Docker; ~3-5 minutes)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
N=bskup; NET=${N}-net; KUMA=${N}-kuma; VOL=${N}-data
OLD=${KUMA_OLD:-louislam/uptime-kuma:1}; NEW=${KUMA_NEW:-louislam/uptime-kuma:2.5.5-slim}
pass=0; fail=0
ok(){ printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
check(){ if eval "$2"; then ok "$1"; else no "$1"; fi; }
cleanup(){ docker rm -f "$KUMA" >/dev/null 2>&1; docker volume rm "$VOL" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; true; }
trap cleanup EXIT
cleanup
docker build -q -t bookstack/kuma-bootstrap:local monitoring >/dev/null || { echo "bootstrap image build failed"; exit 1; }
docker network create "$NET" >/dev/null; docker volume create "$VOL" >/dev/null
TOK=selftest0123456789abcdefghijklmn
cfg(){ python3 -c "
import json; print(json.dumps({'url': 'http://$KUMA:3001', 'wait': $1, 'user': 'kenith-admin', 'password': 'correct-horse-battery-staple-42',
  'domain': 'example.test', 'bind_ip': '203.0.113.7', 'features': {}, 'push': {'selftest': '$TOK'},
  'notify': {'webhook': 'http://example.invalid/hook', 'format': 'ntfy'}, 'reboot_time': '04:30'}))"; }
boot(){ docker run --rm -i --network "$NET" bookstack/kuma-bootstrap:local; }
field(){ python3 -c "import sys,json; d=json.loads(sys.stdin.read().strip().splitlines()[-1]); print($1)"; }

echo "== 1.x, managed by the new library"
docker run -d --name "$KUMA" --network "$NET" -v "$VOL:/app/data" "$OLD" >/dev/null
out=$(cfg 180 | boot); rc=$?
check "seed run on $OLD exits 0" '[ "$rc" = 0 ] && [ "$(printf %s "$out" | field "d[\"setup\"]")" = created ]'
n1=$(printf %s "$out" | field 'len(d["added"])')
out=$(cfg 60 | boot)
check "a second run on 1.x changes nothing (no jsonPathOperator churn)" '[ "$(printf %s "$out" | field "len(d[\"added\"])+len(d[\"updated\"])+len(d[\"deleted\"])")" = 0 ]'

echo "== swap to $NEW on the same data"
docker rm -f "$KUMA" >/dev/null
docker run -d --name "$KUMA" --network "$NET" -v "$VOL:/app/data" -e UPTIME_KUMA_DB_TYPE=sqlite "$NEW" >/dev/null
t0=$(date +%s); out=$(cfg 1800 | boot); rc=$?
echo "     migrated and reconciled in $(( $(date +%s) - t0 )) s: $(printf %s "$out" | cut -c1-200)"
check "the bootstrap waits out the migration and exits 0" '[ "$rc" = 0 ]'
check "the account survived (setup: existing)" '[ "$(printf %s "$out" | field "d[\"setup\"]")" = existing ]'
check "every monitor survived: nothing added, nothing deleted ($n1 monitors)" '[ "$(printf %s "$out" | field "len(d[\"added\"])+len(d[\"deleted\"])")" = 0 ]'
check "the maintenance window survived" '[ "$(printf %s "$out" | field "d[\"maintenance\"]")" = "25 4 * * *" ]'
out=$(cfg 60 | boot)
check "a third run is a no-op" '[ "$(printf %s "$out" | field "len(d[\"added\"])+len(d[\"updated\"])+len(d[\"deleted\"])")" = 0 ]'
r=$(docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://$KUMA:3001/api/push/$TOK?status=up&msg=ok")
check "the push monitor still takes its token" 'printf %s "$r" | grep -q "\"ok\":true"'
echo; echo "KUMA UPGRADE RESULT: $pass passed, $fail failed"
exit "$fail"
