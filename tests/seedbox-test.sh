#!/usr/bin/env bash
# seedbox-test.sh — scripts/seedbox-fetch.py against two REAL Syncthing instances (the pinned
# image, with the compose file's capabilities) and a stand-in rTorrent XML-RPC:
#   sbt-seed  the seedbox: folders SEND ONLY, laid out like the owner's seedbox
#             .../sabnzbd/completed/bookstack-ebooks   (SABnzbd)
#             .../rtorrent/bookstack                   (rTorrent: private trackers, must keep seeding)
#   sbt-vps   this server: set up by `seedbox-fetch.py --setup` exactly as Library -> Seedbox does
# NOTHING ON THE SEEDBOX CHANGES is the property under test: the whole seedbox tree is
# fingerprinted (path, size, mtime, sha256 of every file and folder) and compared after every
# run, every destructive act on this side, and before every change the test itself makes there
# (as SABnzbd / rTorrent would). Needs Docker. bash tests/seedbox-test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/seedbox-test.XXXXXX"); PASS=0; FAIL=0
ok(){ echo "  [ OK ] $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
expect(){ if eval "$1"; then ok "$2"; else bad "$2  -- ($1)"; fi; }
cleanup(){ docker rm -f sbt-seed sbt-vps >/dev/null 2>&1; docker network rm sbt >/dev/null 2>&1; [ -n "${RTPID:-}" ] && kill "$RTPID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
IMG=$(grep -oE 'IMG_SYNCTHING=[^" ]+' "$REPO/bookstack.sh" | head -1 | cut -d= -f2)
SEED="$T/seed/data"; SAB="$SEED/sabnzbd/completed"; RT="$SEED/rtorrent/bookstack"
STACK="$T/stack"; SYNC="$STACK/library/seedbox-sync"; M="$STACK/library/seedbox"
mkdir -p "$SAB/bookstack-ebooks" "$SAB/bookstack-audiobooks" "$SAB/books" "$RT" "$T/seed/cfg" "$STACK/syncthing" \
  "$SYNC/rtorrent" "$SYNC/sabnzbd/bookstack-ebooks" "$SYNC/sabnzbd/bookstack-audiobooks" "$T/etc"
KEY=sbt-vps-api-key-0123456789; SKEY=sbt-seed-api-key-0123456789
printf 'PUID=%s\nPGID=%s\nSYNCTHING_API_KEY=%s\n' "$(id -u)" "$(id -g)" "$KEY" > "$STACK/.env"

# the seedbox's content, as SABnzbd and rTorrent leave it
mkdir -p "$SAB/bookstack-ebooks/Emma (1815)/extras" "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park"
head -c 300000 /dev/urandom > "$SAB/bookstack-ebooks/Emma (1815)/Emma.epub"; echo cover > "$SAB/bookstack-ebooks/Emma (1815)/extras/cover.jpg"
echo x > "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park/a.rar"; echo readarr > "$SAB/books/not-ours.epub"
mkdir -p "$RT/Middlemarch" "$RT/Unfinished" "$RT/Book [2020] {x}"; head -c 200000 /dev/urandom > "$RT/Middlemarch/Middlemarch.epub"
echo single > "$RT/Sense.epub"; echo half > "$RT/Unfinished/part.epub"; echo br > "$RT/Book [2020] {x}/a.epub"; echo new > "$RT/Recent.epub"
NOW=$(date +%s)
# rTorrent stand-in: d.multicall2 answers name / complete / directory / finished, from a file the test edits
cat > "$T/rt.py" <<'PY'
import json, sys
from xmlrpc.server import SimpleXMLRPCServer
def multicall2(target, view, *cmds):
    assert cmds == ("d.name=", "d.complete=", "d.directory=", "d.timestamp.finished="), cmds
    return json.load(open(sys.argv[1]))
s = SimpleXMLRPCServer(("127.0.0.1", 18789), logRequests=False, allow_none=True)
s.register_function(multicall2, "d.multicall2")
s.serve_forever()
PY
B=/sdb/hcus/data/rtorrent/bookstack
rt_list(){ printf '[["Middlemarch",1,"%s/Middlemarch",%s],["Sense.epub",1,"%s",%s],["Unfinished",0,"%s/Unfinished",0],["Book [2020] {x}",1,"%s/Book [2020] {x}",%s],["Recent.epub",1,"%s",%s],["NoCheck",1,"%s/NoCheck",%s],["Elsewhere",1,"/sdb/hcus/data/rtorrent/tv/Elsewhere",1]]' \
  "$B" $((NOW-7200)) "$B" $((NOW-7200)) "$B" "$B" $((NOW-7200)) "$B" "${RECENT_AT:-$(date +%s)}" "$B" $((NOW-7200)) > "$T/rt.json"; }
rt_list; python3 "$T/rt.py" "$T/rt.json" & RTPID=$!

docker network create sbt >/dev/null
cst(){ docker run -d --name "$1" --network sbt --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --cap-add DAC_OVERRIDE --cap-add FOWNER --security-opt no-new-privileges:true -e PUID="$(id -u)" -e PGID="$(id -g)" \
  -e STGUIAPIKEY="$2" -e STNOUPGRADE=1 -p "127.0.0.1:$3:8384" "${@:4}" "$IMG" >/dev/null; }
cst sbt-seed "$SKEY" 18385 -v "$T/seed/cfg:/var/syncthing" -v "$SEED:/data"
cst sbt-vps "$KEY" 18384 -v "$STACK/syncthing:/var/syncthing" -v "$SYNC:/sync"
api(){ python3 - "$@" <<'PY'
import json, sys, urllib.request
port, key = {"seed": ("18385", "sbt-seed-api-key-0123456789"), "vps": ("18384", "sbt-vps-api-key-0123456789")}[sys.argv[1]]
body = sys.argv[4] if len(sys.argv) > 4 else None
r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{sys.argv[3]}", method=sys.argv[2],
    data=body.encode() if body else None, headers={"X-API-Key": key, "Content-Type": "application/json"}), timeout=30).read()
print(r.decode())
PY
}
for p in 18384 18385; do for _ in $(seq 1 60); do curl -fs -o /dev/null "http://127.0.0.1:$p/rest/noauth/health" && break; sleep 1; done; done
myid(){ api "$1" GET /rest/system/status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null; }
for _ in $(seq 1 30); do SEEDID=$(myid seed); [ -n "$SEEDID" ] && [ -n "$(myid vps)" ] && break; sleep 1; done
[ -n "$SEEDID" ] || { echo "the seedbox's Syncthing did not answer"; exit 1; }
cat > "$T/etc/seedbox.env" <<EOF
SEEDBOX_ST_DEVICE=$SEEDID
SEEDBOX_ST_ADDRESS=tcp://sbt-seed:22000
SEEDBOX_ST_GUI_PASS=gui-pass-for-test
SEEDBOX_SAB_OWN=/data/watch/downloads/sabnzbd/completed
SEEDBOX_SAB_CATS=bookstack-ebooks bookstack-audiobooks
SEEDBOX_RT_URL=http://127.0.0.1:18789/RPC2
SEEDBOX_RT_DIR=$B
SEEDBOX_RT_USER=hcus
SEEDBOX_RT_PASS=front-door-password
SEEDBOX_SOURCES=bookstack-sab-ebooks|sabnzbd/bookstack-ebooks|sab;bookstack-sab-audiobooks|sabnzbd/bookstack-audiobooks|sab;bookstack-rtorrent|rtorrent|rt
EOF
printf '#!/usr/bin/env bash\necho "ALERT $1" >> "%s"\n' "$T/alerts.log" > "$T/alert.sh"; chmod +x "$T/alert.sh"; : > "$T/alerts.log"
SF(){ STACK_DIR="$STACK" SEEDBOX_ENV="$T/etc/seedbox.env" SEEDBOX_STATE="$T/etc/seedbox.state" SEEDBOX_ALERT="$T/alert.sh" \
  SEEDBOX_ST_URL=http://127.0.0.1:18384 SEEDBOX_FAILS_BEFORE_ALERT=15 SEEDBOX_MIN_AGE=1 SEEDBOX_RT_SETTLE="${SETTLE:-4}" SEEDBOX_KEEP_DAYS="${KEEP:-7}" \
  python3 "$REPO/scripts/seedbox-fetch.py" "$@"; }

echo "== Library -> Seedbox: this server's Syncthing is set up by --setup"
VPSID=$(SF --setup 2>&1 | tail -1)
expect '[ "$VPSID" = "$(api vps GET /rest/system/status | python3 -c "import json,sys; print(json.load(sys.stdin)[\"myID\"])")" ]' "--setup prints this server's device ID (what the admin types on the seedbox)"
expect 'api vps GET /rest/config/folders | python3 -c "
import json,sys; f={x[\"id\"]:x for x in json.load(sys.stdin)}
assert sorted(f)==[\"bookstack-rtorrent\",\"bookstack-sab-audiobooks\",\"bookstack-sab-ebooks\"], f.keys()
assert all(x[\"type\"]==\"receiveonly\" for x in f.values())
assert f[\"bookstack-rtorrent\"][\"path\"]==\"/sync/rtorrent\"
assert all(sorted(d[\"deviceID\"] for d in x[\"devices\"])==sorted([\"$SEEDID\",\"$VPSID\"]) for x in f.values())"' "three folders, all RECEIVE ONLY, shared with the seedbox and nothing else (Syncthing's Default Folder removed)"
expect 'api vps GET "/rest/db/ignores?folder=bookstack-sab-ebooks" | grep -qF "/_UNPACK_*"' "SABnzbd's work in progress (_UNPACK_, _FAILED_) is never pulled here"
api vps PATCH /rest/config/options '{"globalAnnounceEnabled":false,"relaysEnabled":false,"natEnabled":false}' >/dev/null
# on the seedbox: what the admin does in its Syncthing (add the device, accept the folders Send Only)
api seed PATCH /rest/config/options '{"globalAnnounceEnabled":false,"relaysEnabled":false,"localAnnounceEnabled":false,"natEnabled":false,"urAccepted":-1}' >/dev/null
api seed POST /rest/config/devices "{\"deviceID\":\"$VPSID\",\"name\":\"bookstack\",\"addresses\":[\"tcp://sbt-vps:22000\"]}" >/dev/null
seed_folder(){ api seed POST /rest/config/folders "{\"id\":\"$1\",\"path\":\"$2\",\"type\":\"${3:-sendonly}\",\"rescanIntervalS\":300,\"fsWatcherDelayS\":1,\"devices\":[{\"deviceID\":\"$VPSID\"}]}" >/dev/null; }
seed_folder bookstack-sab-ebooks /data/sabnzbd/completed/bookstack-ebooks
seed_folder bookstack-sab-audiobooks /data/sabnzbd/completed/bookstack-audiobooks
seed_folder bookstack-rtorrent /data/rtorrent/bookstack
FIDS="bookstack-sab-ebooks bookstack-sab-audiobooks bookstack-rtorrent"
# synced: after a rescan on the seedbox, this server has everything the seedbox announced
synced(){ local f; for f in $FIDS; do api seed POST "/rest/db/scan?folder=$f" >/dev/null 2>&1; done
  for _ in $(seq 1 90); do
    python3 - "$SEEDID" $FIDS <<'PY' && return 0
import json, sys, urllib.request
def get(port, key, path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-API-Key": key}), timeout=10).read())
seed = sys.argv[1]
for f in sys.argv[2:]:
    s = get(18385, "sbt-seed-api-key-0123456789", f"/rest/db/status?folder={f}")
    v = get(18384, "sbt-vps-api-key-0123456789", f"/rest/db/status?folder={f}")
    if s["state"] != "idle" or v["state"] != "idle" or v["needTotalItems"] or v["remoteSequence"].get(seed) != s["sequence"]:
        sys.exit(1)
PY
    sleep 1; done; return 1; }
fingerprint(){ python3 - "$SEED" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
for d, dirs, files in sorted(os.walk(root)):
    dirs[:] = sorted(x for x in dirs if x != ".stfolder")     # Syncthing's own marker, made when the folder was added
    st = os.stat(d); print("D", os.path.relpath(d, root), st.st_mtime_ns)
    for f in sorted(files):
        p = os.path.join(d, f); st = os.stat(p)
        print("F", os.path.relpath(p, root), st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
PY
}
UNTOUCHED=0; TOUCHED=0
same(){ local now; now=$(fingerprint); if [ "$now" = "$BASE" ]; then UNTOUCHED=$((UNTOUCHED+1)); else TOUCHED=$((TOUCHED+1)); echo "  !! seedbox changed ($1):" >&2; diff <(echo "$BASE") <(echo "$now") | head -5 >&2; fi; }
seed_do(){ same "before the test's own change"; eval "$1"; BASE=$(fingerprint); }     # what SABnzbd / rTorrent do there
run(){ local rc; SF; rc=$?; same "a run"; return $rc; }
runs(){ run > "$T/$1.a" 2>&1; sleep 2; run > "$T/$1.out" 2>&1; }       # an item must look the same twice, MIN_AGE apart
synced || bad "the two Syncthings did not sync within 90 s"
BASE=$(fingerprint)

echo "== the installer's check"
out=$(SF --check 2>&1); rc=$?; printf '%s\n' "$out" | sed 's/^/     /'
expect '[ $rc = 0 ] && printf "%s" "$out" | grep -q "every folder here is Receive Only" && printf "%s" "$out" | grep -q "seedbox.s Syncthing is connected" && [ "$(printf "%s" "$out" | grep -c "shared by the seedbox")" = 3 ] && printf "%s" "$out" | grep -q "rTorrent answers (5 finished"' "--check: Receive Only here, connected, all three folders shared, rTorrent answers"
expect '[ "$(SF --device-id)" = "$VPSID" ]' "--device-id shows this server's ID again (Library -> Seedbox -> Show what to set up)"

echo "== first runs"
RECENT_AT=$(date +%s) rt_list
runs r1; rc=$?; sed 's/^/     /' "$T/r1.out"
expect '[ $rc = 0 ]' "the run succeeds"
expect '[ "$(wc -c < "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub")" = 300000 ] && [ -f "$M/sabnzbd/bookstack-ebooks/Emma (1815)/extras/cover.jpg" ]' "a finished SABnzbd job is handed to Shelfmark whole, with its subfolders"
expect '[ "$(stat -c %i "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub" 2>/dev/null || stat -f %i "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub")" = "$(stat -c %i "$SYNC/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub" 2>/dev/null || stat -f %i "$SYNC/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub")" ]' "...as a hard link: no second copy on this server's disk"
expect '[ -f "$SAB/bookstack-ebooks/Emma (1815)/Emma.epub" ]' "...and it stays on the seedbox"
expect '[ ! -e "$SYNC/sabnzbd/bookstack-ebooks/_UNPACK_Mansfield Park" ] && [ ! -e "$M/sabnzbd/bookstack-ebooks/_UNPACK_Mansfield Park" ]' "SABnzbd's unpack folder is not even pulled here"
expect '[ ! -e "$SYNC/sabnzbd/books" ] && [ ! -e "$M/sabnzbd/books" ]' "other categories (Readarr's books) are not shared at all"
expect '[ "$(wc -c < "$M/rtorrent/Middlemarch/Middlemarch.epub")" = 200000 ] && [ -f "$M/rtorrent/Sense.epub" ] && [ -f "$M/rtorrent/Book [2020] {x}/a.epub" ]' "finished torrents (a folder, a single file, a name with [ ] { }) are handed over"
expect '[ -f "$SYNC/rtorrent/Unfinished/part.epub" ] && [ ! -e "$M/rtorrent/Unfinished" ]' "a torrent rTorrent does not report complete arrives here but is NOT handed over"
expect '[ -f "$SYNC/rtorrent/Recent.epub" ] && [ ! -e "$M/rtorrent/Recent.epub" ]' "a torrent finished moments ago waits (the seedbox's rescan must catch its last pieces first)"
sleep 4; runs r1b
expect '[ -f "$M/rtorrent/Recent.epub" ]' "...and is handed over once that wait has passed"
expect '[ ! -e "$M/rtorrent/Elsewhere" ]' "a complete torrent outside the bookstack folder is ignored"
expect '[ -z "$(ls -A "$M/.incoming" 2>/dev/null)" ]' "nothing is left in the staging folder"
expect '[ "$(stat -c %u "$M/rtorrent" 2>/dev/null || stat -f %u "$M/rtorrent")" = "$(id -u)" ] && [ "$(stat -c %u "$M/sabnzbd/bookstack-ebooks" 2>/dev/null || stat -f %u "$M/sabnzbd/bookstack-ebooks")" = "$(id -u)" ]' "the hand-over folders belong to PUID (Shelfmark's user), not root"

echo "== nothing twice; a change is handed over again"
rm -rf "$M/rtorrent/Middlemarch" "$M/sabnzbd/bookstack-ebooks/Emma (1815)"     # Shelfmark took them into dropboxes
runs r2
expect '[ ! -e "$M/rtorrent/Middlemarch" ] && [ ! -e "$M/sabnzbd/bookstack-ebooks/Emma (1815)" ] && grep -q "0 item(s) handed" "$T/r2.out"' "neither a torrent nor a Usenet job is handed over a second time"
seed_do 'echo more > "$SAB/bookstack-ebooks/Emma (1815)/Emma.opf"'; synced; runs r3
expect '[ -f "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.opf" ]' "a job that changed on the seedbox (SABnzbd repaired it) is handed over again"
seed_do 'mkdir -p "$SAB/bookstack-audiobooks/Big Audio"; head -c 900000 /dev/urandom > "$SAB/bookstack-audiobooks/Big Audio/part1.mp3"'
synced; runs r4
expect '[ -f "$M/sabnzbd/bookstack-audiobooks/Big Audio/part1.mp3" ]' "a new job in the second category arrives and is handed over"

echo "== destructive acts on THIS side never reach the seedbox"
wreck(){ rm -rf "$SYNC/rtorrent/Middlemarch" "$SYNC/sabnzbd/bookstack-ebooks/Emma (1815)"; echo changed > "$SYNC/rtorrent/Sense.epub"
  echo junk > "$SYNC/rtorrent/junk.epub"; mkdir -p "$SYNC/rtorrent/Junk Dir"; echo j > "$SYNC/rtorrent/Junk Dir/j"
  for f in $FIDS; do api vps POST "/rest/db/scan?folder=$f" >/dev/null; done; sleep 12; }
restore(){ for f in $FIDS; do api vps POST "/rest/db/revert?folder=$f" >/dev/null; done; synced; }   # the test's own clean-up; the job never reverts
wreck; same "deletions, edits and additions here (seedbox Send Only)"
expect '[ -f "$RT/Middlemarch/Middlemarch.epub" ] && [ "$(cat "$RT/Sense.epub")" = single ] && [ ! -e "$RT/junk.epub" ] && [ -f "$SAB/bookstack-ebooks/Emma (1815)/Emma.epub" ]' "deleting, editing and adding files in this server's copy leaves the seedbox exactly as it was"
restore
for f in $FIDS; do api seed PATCH "/rest/config/folders/$f" '{"type":"sendreceive"}' >/dev/null; done; sleep 3
wreck; same "the same, with the seedbox side WRONGLY Send & Receive"
expect '[ -f "$RT/Middlemarch/Middlemarch.epub" ] && [ "$(cat "$RT/Sense.epub")" = single ] && [ ! -e "$RT/junk.epub" ]' "...even with the seedbox side wrongly set to Send & Receive (Receive Only here is enough on its own)"
restore
for f in $FIDS; do api seed PATCH "/rest/config/folders/$f" '{"type":"sendonly"}' >/dev/null; done; sleep 2

echo "== guardrail 1: a folder here that is not Receive Only is paused, and nothing runs"
api vps PATCH /rest/config/folders/bookstack-rtorrent '{"type":"sendreceive"}' >/dev/null; : > "$T/alerts.log"
run > "$T/r5.out" 2>&1; rc=$?; run >/dev/null 2>&1
expect '[ $rc = 2 ] && grep -q "REFUSED" "$T/r5.out" && grep -q "bookstack-rtorrent is .sendreceive." "$T/r5.out"' "the run refuses and names the folder"
expect 'api vps GET /rest/config/folders/bookstack-rtorrent | grep -q "\"paused\": *true"' "...and PAUSES that folder at once, so nothing made here can be sent"
expect '[ "$(grep -c "REFUSED (safety)" "$T/alerts.log")" = 1 ]' "...with one alert, not one a minute"
out=$(SF --check 2>&1)
expect 'printf "%s" "$out" | grep -q "^FAIL: .*bookstack-rtorrent is .sendreceive."' "the installer's check reports it too"
SF --setup >/dev/null 2>&1; runs r6; rc=$?
expect '[ $rc = 0 ] && api vps GET /rest/config/folders/bookstack-rtorrent | grep -q "\"type\": *\"receiveonly\"" && api vps GET /rest/config/folders/bookstack-rtorrent | grep -q "\"paused\": *false"' "Library -> Seedbox -> Change the settings (--setup) puts it back to Receive Only and unpauses it"

echo "== guardrail 3: only the listed requests reach Syncthing"
expect 'python3 - "$REPO/scripts/seedbox-fetch.py" <<"PY"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sf", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.ST_URL = "http://127.0.0.1:9"
st = m.Syncthing("k")
bad = 0
for meth, path, body in (("POST", "/rest/db/revert", None), ("POST", "/rest/db/override", None), ("DELETE", "/rest/config/folders/x", None),
                         ("PATCH", "/rest/config/folders/x", {"type": "sendreceive"}), ("PATCH", "/rest/config/folders/x", {"paused": False}),
                         ("PUT", "/rest/config/folders/x", {"type": "sendonly"}), ("POST", "/rest/config/folders", {}), ("POST", "/rest/system/restart", None),
                         ("PUT", "/rest/config", {}), ("POST", "/rest/db/scan", None), ("PATCH", "/rest/config/options", {})):
    try:
        st.req(meth, path, body=body); bad += 1
    except m.Unsafe:
        pass
    except Exception:
        bad += 1            # anything but the guardrail means it tried the network
sys.exit(bad)
PY' "revert, override, config changes (bar pausing a folder), restart and scans are refused before a byte is sent"

echo "== guardrail 4: rTorrent is only ever asked one read-only question"
expect 'grep -q "^RT_COMMANDS = (\"d.name=\", \"d.complete=\", \"d.directory=\", \"d.timestamp.finished=\")" "$REPO/scripts/seedbox-fetch.py" && ! grep -qE "d\.(erase|stop|close|delete|set|custom1\.set|directory\.set)|(^|[^A-Za-z_])(execute|load)\.[a-z]" "$REPO/scripts/seedbox-fetch.py"' "the only rTorrent call reads name / complete / directory / finished; no erase, stop, set, execute or load anywhere"

echo "== after a week: this server drops its OWN copy (ignored in Syncthing first)"
KEEP=0.00002 runs r7; rc=$?; sed 's/^/     /' "$T/r7.out"
expect '[ $rc = 0 ] && [ ! -e "$SYNC/rtorrent/Middlemarch" ] && [ ! -e "$SYNC/rtorrent/Book [2020] {x}" ] && [ ! -e "$M/rtorrent/Sense.epub" ]' "handed-over items leave this server (the synced copy and the hand-over)"
api vps GET "/rest/db/ignores?folder=bookstack-rtorrent" > "$T/ign.json"
if python3 - "$T/ign.json" <<'PYI'
import json, sys
i = json.load(open(sys.argv[1]))["ignore"]
sys.exit(0 if "/Middlemarch" in i and r"/Book \[2020\] \{x\}" in i else 1)
PYI
then ok "...each first ignored in Syncthing by its exact name ([ ] { } escaped)"; else bad "...each first ignored in Syncthing by its exact name: $(cat "$T/ign.json")"; fi
expect '[ -d "$RT/Middlemarch" ] && [ -f "$RT/Book [2020] {x}/a.epub" ] && [ -f "$RT/Sense.epub" ]' "...and every one of them is still on the seedbox"
expect 'api vps GET /rest/db/status?folder=bookstack-rtorrent | python3 -c "import json,sys; s=json.load(sys.stdin); assert s[\"needTotalItems\"]==0 and s[\"receiveOnlyTotalItems\"]==0, s"' "...and Syncthing sees no local change to undo and nothing to pull back"
expect '[ -f "$SYNC/rtorrent/Unfinished/part.epub" ]' "an item never handed over (still downloading) is kept"

echo "== an item that leaves the seedbox is forgotten, and may come back"
seed_do 'rm -rf "$RT/Middlemarch"'; synced; run >/dev/null 2>&1
expect '! grep -q "rtorrent/Middlemarch" "$T/etc/seedbox.state" && ! api vps GET "/rest/db/ignores?folder=bookstack-rtorrent" | grep -q "\"/Middlemarch\""' "its record and its ignore line are dropped"
seed_do 'mkdir -p "$RT/Middlemarch"; echo again > "$RT/Middlemarch/Middlemarch.epub"'; synced
expect '[ "$(cat "$SYNC/rtorrent/Middlemarch/Middlemarch.epub")" = again ]' "a new download of the same name syncs again"

echo "== no rTorrent address: torrents are never handed over blind"
sed -i.bak '/SEEDBOX_RT_URL/d' "$T/etc/seedbox.env"
seed_do 'mkdir -p "$RT/NoCheck"; echo x > "$RT/NoCheck/a.epub"'; synced; runs r8
expect '[ -f "$SYNC/rtorrent/NoCheck/a.epub" ] && [ ! -e "$M/rtorrent/NoCheck" ]' "without rTorrent's completion list nothing is handed over from the torrent folder"
mv "$T/etc/seedbox.env.bak" "$T/etc/seedbox.env"
runs r9
expect '[ -f "$M/rtorrent/NoCheck/a.epub" ]' "...and with it back, the finished torrent is handed over"

echo "== the seedbox's Syncthing is down: one alert, not one a minute"
docker stop sbt-seed >/dev/null; : > "$T/alerts.log"; sleep 2
for _ in $(seq 1 16); do SF >/dev/null 2>&1; done
expect '[ "$(grep -c "not arriving" "$T/alerts.log")" = 1 ]' "after 15 failed minutes exactly one alert"
docker start sbt-seed >/dev/null; for _ in $(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:18385/rest/noauth/health && break; sleep 1; done
for _ in $(seq 1 40); do SF >/dev/null 2>&1 && break; sleep 1; done
expect 'grep -q "connected again" "$T/alerts.log"' "and one when it is back"

echo "== nothing on the seedbox was ever changed"
same "the end"
expect '[ "$TOUCHED" = 0 ] && [ "$UNTOUCHED" -ge 25 ]' "all $((UNTOUCHED+TOUCHED)) comparisons found every file and folder on the seedbox exactly as it was (path, size, mtime, sha256)"
expect '[ -z "$(find "$SEED" \( -name ".stignore" -o -name ".stversions" -o -name ".syncthing.*" -o -name "*.sync-conflict-*" \) -print)" ]' "no Syncthing ignore, version, temp or conflict file ever appeared on the seedbox"

echo
echo "SEEDBOX RESULT: $PASS passed, $FAIL failed"
exit "$FAIL"
