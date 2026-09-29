#!/usr/bin/env bash
# Integration test for monitoring/kuma_bootstrap.py against the REAL pinned Uptime Kuma image
# (louislam/uptime-kuma:2.5.5-slim, v5.9.1) — the socket.io API is the part no unit test can vouch for.
#   bash tests/monitoring-test.sh            (needs Docker; ~1-2 minutes)
# Proves: first-run account setup, idempotent re-runs, feature toggles add/remove only managed
# monitors, a hand-made monitor survives, wrong credentials exit 2, push URLs are live, a DOWN
# push reaches the webhook ntfy-style, settings and the reboot maintenance window are applied.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck disable=SC2034  # met/rc/state are read inside the check strings (eval)
N=bsmon; NET=${N}-net; KUMA=${N}-kuma; HOOK=${N}-hook; MAIL=${N}-mail
IMG_KUMA=${IMG_KUMA:-louislam/uptime-kuma:2.5.5-slim}
pass=0; fail=0
ok(){ printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
check(){ if eval "$2"; then ok "$1"; else no "$1"; fi; }
cleanup(){ docker rm -f "$KUMA" "$HOOK" "$MAIL" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; true; }
trap cleanup EXIT
cleanup
docker build -q -t bookstack/kuma-bootstrap:local monitoring >/dev/null || { echo "bootstrap image build failed"; exit 1; }
docker network create "$NET" >/dev/null
docker run -d --name "$KUMA" --network "$NET" -e TZ=Europe/Berlin -e UPTIME_KUMA_DB_TYPE=sqlite "$IMG_KUMA" >/dev/null
# webhook receiver: every request -> one JSON line on stdout (docker logs)
docker run -d --name "$HOOK" --network "$NET" python:3.12-slim python -u -c '
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        print(json.dumps({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n).decode("utf-8", "replace")}), flush=True)
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()' >/dev/null
# SMTP + IMAP for the e-mail channel (the same GreenMail the e2e suite uses)
docker run -d --name "$MAIL" --network "$NET" -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled" greenmail/standalone:2.1.3 >/dev/null

TOK_SELF=selftest0123456789abcdefghijklmn; TOK_DISK=disk0123456789abcdefghijklmnopqr
cfg(){ # features-json [password] [smtp=1]
  python3 - "$1" "${2:-correct-horse-battery-staple-42}" "${3:-0}" << 'PY'
import json, sys
notify = {"webhook": "http://bsmon-hook:8000/bookstack", "format": "ntfy"}
if sys.argv[3] == "1":
    notify.update(to="admin@example.test", smtp={"host": "bsmon-mail", "port": 3025, "security": "none",
                                                 "user": "", "password": "", "from": "library@example.test"})
print(json.dumps({"url": "http://bsmon-kuma:3001", "wait": 120, "user": "kenith-admin", "password": sys.argv[2],
  "domain": "example.test", "bind_ip": "203.0.113.7", "features": json.loads(sys.argv[1]),
  "push": {"selftest": "selftest0123456789abcdefghijklmn", "disk": "disk0123456789abcdefghijklmnopqr"},
  "notify": notify, "reboot_time": "04:30"}))
PY
}
boot(){ docker run --rm -i --network "$NET" bookstack/kuma-bootstrap:local; }
field(){ python3 -c "import sys,json; d=json.loads(sys.stdin.read().strip().splitlines()[-1]); print($1)"; }

echo "== first run (fresh Kuma)"
out=$(cfg '{}' | boot); rc=$?
echo "     $out" | cut -c1-300
check "first run exits 0" '[ "$rc" = 0 ]'
check "admin account created on first run" '[ "$(printf %s "$out" | field "d[\"setup\"]")" = created ]'
check "10 core + push monitors added (5 core, caddy, public, home, 2 push)" '[ "$(printf %s "$out" | field "len(d[\"added\"])")" = 10 ]'
check "one notification (webhook)" '[ "$(printf %s "$out" | field "d[\"notifications\"]")" = "[%s]" ] || printf %s "$out" | field "d[\"notifications\"]" | grep -q "bookstack: webhook"'
check "maintenance cron = 04:25 daily" '[ "$(printf %s "$out" | field "d[\"maintenance\"]")" = "25 4 * * *" ]'

echo "== second run is a no-op"
out=$(cfg '{}' | boot); rc=$?
echo "     $out" | cut -c1-400
check "second run exits 0" '[ "$rc" = 0 ]'
check "nothing added/updated/deleted" '[ "$(printf %s "$out" | field "len(d[\"added\"])+len(d[\"updated\"])+len(d[\"deleted\"])")" = 0 ]'
check "setup reported as existing" '[ "$(printf %s "$out" | field "d[\"setup\"]")" = existing ]'

echo "== a hand-made monitor is never touched"
docker run --rm -i --network "$NET" --entrypoint python bookstack/kuma-bootstrap:local - << 'PY' >/dev/null
from uptime_kuma_api import UptimeKumaApi, MonitorType
a = UptimeKumaApi("http://bsmon-kuma:3001"); a.login("kenith-admin", "correct-horse-battery-staple-42")
a.add_monitor(type=MonitorType.HTTP, name="my own check", url="http://example.test/")
a.disconnect()
PY
echo "== features on: ephemera + flaresolverr + torrents + authelia"
out=$(cfg '{"ephemera":true,"flaresolverr":true,"torrents":true,"authelia":true}' | boot); rc=$?
check "feature run exits 0" '[ "$rc" = 0 ]'
check "4 feature monitors added" '[ "$(printf %s "$out" | field "len(d[\"added\"])")" = 4 ]'
check "hand-made monitor not deleted" '[ "$(printf %s "$out" | field "len(d[\"deleted\"])")" = 0 ]'
echo "== features off again"
out=$(cfg '{}' | boot)
check "4 feature monitors deleted" '[ "$(printf %s "$out" | field "len(d[\"deleted\"])")" = 4 ]'
check "deleted set is exactly the feature monitors" 'printf %s "$out" | field "sorted(d[\"deleted\"])" | grep -q "Authelia" && ! printf %s "$out" | grep -q "my own check"'

echo "== drift is put back"
docker run --rm -i --network "$NET" --entrypoint python bookstack/kuma-bootstrap:local - << 'PY' >/dev/null
from uptime_kuma_api import UptimeKumaApi
a = UptimeKumaApi("http://bsmon-kuma:3001"); a.login("kenith-admin", "correct-horse-battery-staple-42")
m = [m for m in a.get_monitors() if m["name"].startswith("Calibre-Web")][0]
a.edit_monitor(m["id"], interval=999, url="http://127.0.0.1:1/"); a.pause_monitor(m["id"])
a.disconnect()
PY
out=$(cfg '{}' | boot)
check "the edited + paused managed monitor is corrected" '[ "$(printf %s "$out" | field "d[\"updated\"]")" = "[%s]" ] || printf %s "$out" | field "d[\"updated\"]" | grep -q "Calibre-Web"'

echo "== state inside Kuma"
state=$(docker run --rm -i --network "$NET" --entrypoint python bookstack/kuma-bootstrap:local - << 'PY'
import json
from uptime_kuma_api import UptimeKumaApi
a = UptimeKumaApi("http://bsmon-kuma:3001"); a.login("kenith-admin", "correct-horse-battery-staple-42")
s = a.get_settings(); ms = a.get_monitors(); mt = a.get_maintenances()
cw = [m for m in ms if m["name"].startswith("Calibre-Web")][0]
print(json.dumps({"keep": s["keepDataPeriodDays"], "base": s["primaryBaseURL"], "proxy": s["trustProxy"],
  "n": len(ms), "own": any(m["name"] == "my own check" for m in ms),
  "cw": [cw["interval"], cw["url"], cw["active"]],
  "notif": sorted({len(m.get("notificationIDList") or []) for m in ms if (m.get("description") or "").startswith("bookstack-managed:")}),
  "maint": [(x["title"], x["strategy"], x.get("cron"), x.get("durationMinutes")) for x in mt],
  "maint_monitors": len(a.get_monitor_maintenance(mt[0]["id"])) if mt else 0}))
a.disconnect()
PY
)
echo "     $state" | cut -c1-400
sf(){ printf %s "$state" | python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
check "history kept 30 days" '[ "$(sf "d[\"keep\"]")" = 30 ]'
check "primary base URL = https://monitor.example.test" '[ "$(sf "d[\"base\"]")" = https://monitor.example.test ]'
check "trustProxy on (Caddy in front)" '[ "$(sf "d[\"proxy\"]")" = True ]'
check "10 managed + 1 own monitor" '[ "$(sf "d[\"n\"]")" = 11 ]'
check "drifted monitor restored (60 s, /login, active)" '[ "$(sf "d[\"cw\"]")" = "[60, '"'"'http://127.0.0.1:8083/login'"'"', True]" ]'
check "every managed monitor carries exactly 1 notification" '[ "$(sf "d[\"notif\"]")" = "[1]" ]'
check "one maintenance window, cron strategy, 25 min" 'sf "d[\"maint\"]" | grep -q "cron.*25 4 \* \* \*.*25"'
check "maintenance covers the 10 managed monitors" '[ "$(sf "d[\"maint_monitors\"]")" = 10 ]'

echo "== push URLs"
pr=$(docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://bsmon-kuma:3001/api/push/$TOK_DISK?status=up&msg=OK&ping=" 2>/dev/null)
check "disk push token accepted ($pr)" 'printf %s "$pr" | grep -q "\"ok\":true"'
pr=$(docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://bsmon-kuma:3001/api/push/nope?status=up" 2>/dev/null)
check "an unknown token is refused" 'printf %s "$pr" | grep -q "\"ok\":false"'
docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://bsmon-kuma:3001/api/push/$TOK_SELF?status=down&msg=2%20checks%20failed" >/dev/null 2>&1
got=""; for _ in $(seq 1 20); do got=$(docker logs "$HOOK" 2>/dev/null | grep -F "Self-test" | head -1); [ -n "$got" ] && break; sleep 1; done
echo "     ${got:0:300}"
check "a DOWN push reaches the webhook" '[ -n "$got" ]'
check "  ntfy-style: plain-text body naming the monitor" 'printf %s "$got" | python3 -c "import sys,json; d=json.load(sys.stdin); b=d[\"body\"]; sys.exit(0 if \"Self-test\" in b and not b.lstrip().startswith(\"{\") else 1)"'
check "  with Title and Priority headers" 'printf %s "$got" | python3 -c "import sys,json; h=json.load(sys.stdin)[\"headers\"]; sys.exit(0 if h.get(\"Title\")==\"Bookstack monitor\" and h.get(\"Priority\")==\"high\" else 1)"'
check "  carrying the pushed message" 'printf %s "$got" | grep -q "2 checks failed"'

echo "== e-mail channel (SMTP) added next to the webhook"
out=$(cfg '{}' correct-horse-battery-staple-42 1 | boot); rc=$?
check "smtp run exits 0" '[ "$rc" = 0 ]'
check "  two notifications now" 'printf %s "$out" | field "d[\"notifications\"]" | grep -q "bookstack: e-mail"'
check "  every managed monitor re-attached (10 updated for notifications)" '[ "$(printf %s "$out" | field "sum(1 for u in d[\"updated\"] if \"notifications\" in u)")" = 10 ]'
docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://bsmon-kuma:3001/api/push/$TOK_DISK?status=down&msg=disk%20watchdog%20test" >/dev/null 2>&1
mail=""; for _ in $(seq 1 30); do
  mail=$(docker run --rm --network "$NET" python:3.12-slim python -c '
import imaplib
m = imaplib.IMAP4("bsmon-mail", 3143); m.login("admin@example.test", "x"); m.select("INBOX")
_, ids = m.search(None, "ALL")
for i in ids[0].split():
    _, d = m.fetch(i, "(RFC822)"); print(d[0][1].decode("utf-8", "replace"))' 2>/dev/null)
  printf %s "$mail" | grep -q "Disk watchdog" && break; sleep 2; done
check "a DOWN push arrives as an e-mail to ADMIN_EMAIL" 'printf %s "$mail" | grep -q "Disk watchdog"'
check "  from SMTP_FROM" 'printf %s "$mail" | grep -qi "^From:.*library@example.test"'
out=$(cfg '{}' | boot)
check "SMTP removed from config -> the e-mail notification is deleted" '! printf %s "$out" | field "d[\"notifications\"]" | grep -q "e-mail"'

echo "== /metrics with the admin login (what scripts/selftest.sh reads)"
docker run --rm --network "$NET" curlimages/curl:8.11.1 -s "http://bsmon-kuma:3001/api/push/$TOK_SELF?status=down&msg=metrics%20probe" >/dev/null 2>&1; sleep 2
met=$(docker run --rm --network "$NET" curlimages/curl:8.11.1 -s -u "kenith-admin:correct-horse-battery-staple-42" http://bsmon-kuma:3001/metrics 2>/dev/null)
check "/metrics answers with basic auth and lists monitor_status" 'printf %s "$met" | grep -q "^monitor_status{"'
check "  names the managed monitors" 'printf %s "$met" | grep -q "monitor_name=\"Portal (request.) - health\""'
# 2.x puts monitor_id (and tag labels) before monitor_name: match the label anywhere, as selftest.sh does
check "  the DOWN push shows as 0" 'printf %s "$met" | grep -E "^monitor_status\{.*monitor_name=\"Self-test \(hourly\)\"" | grep -qE " 0$"'
bad=$(docker run --rm --network "$NET" curlimages/curl:8.11.1 -s -o /dev/null -w '%{http_code}' -u "kenith-admin:nope" http://bsmon-kuma:3001/metrics 2>/dev/null)
check "  a wrong password is refused ($bad)" '[ "$bad" = 401 ]'

echo "== wrong credentials"
out=$(cfg '{}' wrong-password-entirely | boot); rc=$?
check "exit 2 on a refused login" '[ "$rc" = 2 ]'
check "  code=credentials" '[ "$(printf %s "$out" | field "d[\"code\"]")" = credentials ]'

echo "== bad input"
out=$(printf 'not json' | boot); rc=$?
check "exit 1 + code=input on non-JSON config" '[ "$rc" = 1 ] && printf %s "$out" | grep -q "\"code\": \"input\""'

echo; echo "MONITORING RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
