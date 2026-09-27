#!/usr/bin/env bash
# seedbox-test.sh — scripts/seedbox-fetch.py against a REAL Filebrowser (the seedbox's 2.63.23)
# and a stand-in rTorrent XML-RPC, laid out like the owner's seedbox:
#   /watch/downloads/sabnzbd/completed/bookstack-ebooks   (SABnzbd)
#   /rtorrent/bookstack                                   (rTorrent: private trackers, must keep seeding)
# COPY ONLY is the property under test: around EVERY run the whole seedbox tree is fingerprinted
# (path, size, mtime, sha256 of every file and folder) and must come out identical.
# Needs Docker. bash tests/seedbox-test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d "${TMPDIR:-/tmp}/seedbox-test.XXXXXX"); PASS=0; FAIL=0
ok(){ echo "  [ OK ] $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
expect(){ if eval "$1"; then ok "$2"; else bad "$2  -- ($1)"; fi; }
cleanup(){ docker rm -f sbt-fb >/dev/null 2>&1; [ -n "${RTPID:-}" ] && kill "$RTPID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
SRV="$T/srv"; SAB="$SRV/watch/downloads/sabnzbd/completed"; RT="$SRV/rtorrent/bookstack"
mkdir -p "$SAB/bookstack-ebooks" "$SAB/bookstack-audiobooks" "$SAB/books" "$RT" "$T/db" "$T/stack/library" "$T/etc"
old(){ touch -t 202601010000 "$@"; find "$@" -exec touch -t 202601010000 {} + 2>/dev/null; }
# SABnzbd: a finished job, a job still being moved in, an unpack in progress, someone else's category
mkdir -p "$SAB/bookstack-ebooks/Emma (1815)" "$SAB/bookstack-ebooks/Persuasion" "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park"
head -c 300000 /dev/urandom > "$SAB/bookstack-ebooks/Emma (1815)/Emma.epub"; mkdir -p "$SAB/bookstack-ebooks/Emma (1815)/extras"
echo cover > "$SAB/bookstack-ebooks/Emma (1815)/extras/cover.jpg"; old "$SAB/bookstack-ebooks/Emma (1815)"
echo partial > "$SAB/bookstack-ebooks/Persuasion/Persuasion.epub"                   # modified just now
echo x > "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park/a.rar"; old "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park"
echo readarr > "$SAB/books/not-ours.epub"; old "$SAB/books"
# rTorrent: one finished folder torrent, one finished single file, one still downloading
mkdir -p "$RT/Middlemarch"; head -c 200000 /dev/urandom > "$RT/Middlemarch/Middlemarch.epub"
echo single > "$RT/Sense.epub"; mkdir -p "$RT/Unfinished"; echo half > "$RT/Unfinished/part.epub"
chmod -R a+rwX "$T"

IMG=filebrowser/filebrowser:latest
docker run --rm -v "$T/db:/database" --entrypoint filebrowser $IMG config init -d /database/fb.db >/dev/null 2>&1
docker run --rm -v "$T/db:/database" --entrypoint filebrowser $IMG users add hcus fb-admin-password-1 --perm.admin -d /database/fb.db >/dev/null 2>&1
docker run --rm -v "$T/db:/database" --entrypoint filebrowser $IMG users add bookstack-reader fb-test-password-1 --perm.admin=false --perm.create=false \
  --perm.rename=false --perm.modify=false --perm.delete=false --perm.share=false --perm.execute=false --perm.download=true -d /database/fb.db >/dev/null 2>&1
docker run -d --name sbt-fb -p 127.0.0.1:18788:80 -v "$T/db:/database" -v "$SRV:/srv" --entrypoint filebrowser $IMG \
  -d /database/fb.db -r /srv -a 0.0.0.0 -p 80 >/dev/null
for _ in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:18788/health && break; sleep 1; done

# rTorrent stand-in: d.multicall2 answers name, complete, directory like the real one
cat > "$T/rt.py" <<'PY'
import sys
from xmlrpc.server import SimpleXMLRPCServer
base = sys.argv[1]
def multicall2(target, view, *cmds):
    return [["Middlemarch", 1, base + "/Middlemarch"], ["Sense.epub", 1, base],
            ["Unfinished", 0, base + "/Unfinished"], ["Elsewhere", 1, "/sdb/hcus/data/rtorrent/tv/Elsewhere"]]
s = SimpleXMLRPCServer(("127.0.0.1", 18789), logRequests=False, allow_none=True)
s.register_function(multicall2, "d.multicall2")
s.serve_forever()
PY
python3 "$T/rt.py" /sdb/hcus/data/rtorrent/bookstack & RTPID=$!
sleep 1

cat > "$T/etc/seedbox.env" <<EOF
SEEDBOX_FB_URL=http://127.0.0.1:18788
SEEDBOX_BASIC_USER=hcus
SEEDBOX_BASIC_PASS=front-door-password
SEEDBOX_FB_USER=bookstack-reader
SEEDBOX_FB_PASS=fb-test-password-1
SEEDBOX_RT_URL=http://127.0.0.1:18789/RPC2
SEEDBOX_RT_DIR=/sdb/hcus/data/rtorrent/bookstack
SEEDBOX_SOURCES=/watch/downloads/sabnzbd/completed/bookstack-ebooks|sabnzbd/bookstack-ebooks|sab;/watch/downloads/sabnzbd/completed/bookstack-audiobooks|sabnzbd/bookstack-audiobooks|sab;/rtorrent/bookstack|rtorrent|rt
EOF
printf '#!/usr/bin/env bash\necho "ALERT $1" >> "%s"\n' "$T/alerts.log" > "$T/alert.sh"; chmod +x "$T/alert.sh"; : > "$T/alerts.log"
M="$T/stack/library/seedbox"
fingerprint(){ python3 - "$SRV" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
for d, dirs, files in sorted(os.walk(root)):
    st = os.stat(d); print("D", os.path.relpath(d, root), st.st_mtime_ns)
    for f in sorted(files):
        p = os.path.join(d, f); st = os.stat(p)
        print("F", os.path.relpath(p, root), st.st_size, st.st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
PY
}
UNTOUCHED=0; TOUCHED=0
run(){ local before after rc
  before=$(fingerprint)
  STACK_DIR="$T/stack" SEEDBOX_ENV="$T/etc/seedbox.env" SEEDBOX_STATE="$T/etc/seedbox.state" SEEDBOX_ALERT="$T/alert.sh" \
    SEEDBOX_FREE_MARGIN_GB="${MARGIN:-0}" python3 "$REPO/scripts/seedbox-fetch.py"; rc=$?
  after=$(fingerprint)
  if [ "$before" = "$after" ]; then UNTOUCHED=$((UNTOUCHED+1)); else TOUCHED=$((TOUCHED+1)); diff <(echo "$before") <(echo "$after") | head -5 >&2; fi
  return $rc; }

echo "== first run"
run > "$T/run1.out" 2>&1; rc=$?; cat "$T/run1.out" | sed 's/^/     /'
expect '[ $rc = 0 ]' "the run succeeds"
expect 'cmp -s "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub" "$T/emma.orig" 2>/dev/null || [ "$(wc -c < "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.epub")" = 300000 ]' "a finished SABnzbd job arrives whole (300000 bytes)"
expect '[ -f "$M/sabnzbd/bookstack-ebooks/Emma (1815)/extras/cover.jpg" ]' "...with its subfolders"
expect '[ -f "$SAB/bookstack-ebooks/Emma (1815)/Emma.epub" ]' "...and STAYS on the seedbox (copy only)"
expect '[ ! -e "$M/sabnzbd/bookstack-ebooks/Persuasion" ] && [ -e "$SAB/bookstack-ebooks/Persuasion" ]' "a job whose files are still being written (under 2 min old) is left alone"
expect '[ ! -e "$M/sabnzbd/bookstack-ebooks/_UNPACK_Mansfield Park" ] && [ -e "$SAB/bookstack-ebooks/_UNPACK_Mansfield Park" ]' "SABnzbd's unpack folder is never touched"
expect '[ ! -e "$M/sabnzbd/books" ] && [ -e "$SAB/books/not-ours.epub" ]' "other categories (Readarr's books) are never touched"
expect '[ "$(wc -c < "$M/rtorrent/Middlemarch/Middlemarch.epub")" = 200000 ] && [ -f "$M/rtorrent/Sense.epub" ]' "finished torrents (a folder and a single file) are copied"
expect '[ -e "$RT/Middlemarch/Middlemarch.epub" ] && [ -e "$RT/Sense.epub" ]' "...and stay on the seedbox to keep seeding"
expect '[ ! -e "$M/rtorrent/Unfinished" ]' "a torrent rTorrent does not report complete is NOT copied (it could be half-downloaded)"
expect '[ ! -e "$M/rtorrent/Elsewhere" ]' "a complete torrent outside the bookstack folder is ignored"
expect '[ -z "$(ls -A "$M/.incoming" 2>/dev/null)" ]' "nothing is left in the staging folder"

echo "== second run: nothing twice"
rm -rf "$M/rtorrent/Middlemarch" "$M/sabnzbd/bookstack-ebooks/Emma (1815)"     # Shelfmark took them into dropboxes
run > "$T/run2.out" 2>&1
expect '[ ! -e "$M/rtorrent/Middlemarch" ] && [ ! -e "$M/sabnzbd/bookstack-ebooks/Emma (1815)" ] && grep -q "0 item(s) fetched" "$T/run2.out"' "neither a torrent nor a Usenet job is copied a second time (both still on the seedbox)"
old "$SAB/bookstack-ebooks/Persuasion"; run > "$T/run3.out" 2>&1
expect '[ -f "$M/sabnzbd/bookstack-ebooks/Persuasion/Persuasion.epub" ] && [ -f "$SAB/bookstack-ebooks/Persuasion/Persuasion.epub" ]' "once settled, the other job is copied the next minute, and stays on the seedbox"
echo more > "$SAB/bookstack-ebooks/Emma (1815)/Emma.opf"; old "$SAB/bookstack-ebooks/Emma (1815)"; run > "$T/run3b.out" 2>&1
expect '[ -f "$M/sabnzbd/bookstack-ebooks/Emma (1815)/Emma.opf" ]' "a job that changed on the seedbox (SABnzbd repaired it) is copied again"
echo "== a torrent that leaves the seedbox is forgotten"
rm -rf "$RT/Sense.epub"; run >/dev/null 2>&1
expect '! grep -q "Sense.epub" "$T/etc/seedbox.state"' "the copied-list forgets torrents removed from the seedbox"

echo "== not enough space: nothing fetched, nothing deleted"
mkdir -p "$SAB/bookstack-audiobooks/Big Audio"; head -c 50000 /dev/urandom > "$SAB/bookstack-audiobooks/Big Audio/part1.mp3"; old "$SAB/bookstack-audiobooks/Big Audio"
MARGIN=999999 run > "$T/run4.out" 2>&1; rc=$?
expect '[ $rc = 1 ] && grep -q "not enough free space" "$T/run4.out" && [ ! -e "$M/sabnzbd/bookstack-audiobooks/Big Audio" ]' "the disk check stops the copy"
run >/dev/null 2>&1
expect '[ -f "$M/sabnzbd/bookstack-audiobooks/Big Audio/part1.mp3" ] && [ -f "$SAB/bookstack-audiobooks/Big Audio/part1.mp3" ]' "with space again it is copied (and stays on the seedbox)"

echo "== unclaimed items go after a week"
mkdir -p "$M/sabnzbd/bookstack-ebooks/Old Job"; echo x > "$M/sabnzbd/bookstack-ebooks/Old Job/a.epub"; touch -t 202601010000 "$M/sabnzbd/bookstack-ebooks/Old Job"
run >/dev/null 2>&1
expect '[ ! -e "$M/sabnzbd/bookstack-ebooks/Old Job" ] && [ -d "$M/sabnzbd/bookstack-ebooks" ]' "an item nobody claimed for a week is cleared; the folders themselves stay"

echo "== the seedbox is down: one alert, not one a minute"
sed -i.bak 's#18788#18799#' "$T/etc/seedbox.env"; : > "$T/alerts.log"
for _ in $(seq 1 16); do run >/dev/null 2>&1; done
expect '[ "$(grep -c "not arriving" "$T/alerts.log")" = 1 ]' "after 15 failed minutes exactly one alert"
sed -i.bak 's#18799#18788#' "$T/etc/seedbox.env"; run >/dev/null 2>&1
expect 'grep -q "reachable again" "$T/alerts.log"' "and one when it recovers"
sed -i.bak "s#SEEDBOX_FB_PASS=.*#SEEDBOX_FB_PASS=wrong#" "$T/etc/seedbox.env"; run > "$T/run5.out" 2>&1; rc=$?
expect '[ $rc = 1 ] && grep -q "FAILED" "$T/run5.out"' "a wrong Filebrowser password is a failure, not a silent run"

echo "== no rTorrent address: torrents are never fetched blind"
sed -i.bak "s#SEEDBOX_FB_PASS=.*#SEEDBOX_FB_PASS=fb-test-password-1#; /SEEDBOX_RT_URL/d" "$T/etc/seedbox.env"
mkdir -p "$RT/NoCheck"; echo x > "$RT/NoCheck/a.epub"; run > "$T/run6.out" 2>&1
expect '[ ! -e "$M/rtorrent/NoCheck" ] && grep -q "no rTorrent address" "$T/run6.out"' "without rTorrent's completion list nothing is copied from the torrent folder"

echo "== guardrail 1: an account that could change the seedbox is refused"
mkdir -p "$SAB/bookstack-ebooks/Refused Job"; echo x > "$SAB/bookstack-ebooks/Refused Job/a.epub"; old "$SAB/bookstack-ebooks/Refused Job"
sed -i.bak "s#SEEDBOX_FB_USER=.*#SEEDBOX_FB_USER=hcus#; s#SEEDBOX_FB_PASS=.*#SEEDBOX_FB_PASS=fb-admin-password-1#" "$T/etc/seedbox.env"; : > "$T/alerts.log"
run > "$T/run7.out" 2>&1; rc=$?; run >/dev/null 2>&1
expect '[ $rc = 2 ] && grep -q "REFUSED" "$T/run7.out" && grep -q "can admin, create, delete" "$T/run7.out" && [ ! -e "$M/sabnzbd/bookstack-ebooks/Refused Job" ]' "the admin account is refused before anything is read, and says which rights are the problem"
expect '[ "$(grep -c "REFUSED (safety)" "$T/alerts.log")" = 1 ]' "...with one alert, not one a minute"
# shellcheck disable=SC2034  # read inside the expect string
out=$(STACK_DIR="$T/stack" SEEDBOX_ENV="$T/etc/seedbox.env" python3 "$REPO/scripts/seedbox-fetch.py" --check 2>&1)
expect 'printf "%s" "$out" | grep -q "FAIL: the Filebrowser account .hcus. can admin"' "the installer's check refuses it too, before anything is saved"
sed -i.bak "s#SEEDBOX_FB_USER=.*#SEEDBOX_FB_USER=bookstack-reader#; s#SEEDBOX_FB_PASS=.*#SEEDBOX_FB_PASS=fb-test-password-1#" "$T/etc/seedbox.env"
# shellcheck disable=SC2034  # read inside the expect string
out=$(STACK_DIR="$T/stack" SEEDBOX_ENV="$T/etc/seedbox.env" python3 "$REPO/scripts/seedbox-fetch.py" --check 2>&1)
expect 'printf "%s" "$out" | grep -q "download-only (it cannot change anything)"' "the download-only account passes the check"

echo "== guardrail 2: no request but login / list / download can leave this job"
expect 'python3 - "$REPO/scripts/seedbox-fetch.py" <<"PY"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sf", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sb = m.Seedbox({"SEEDBOX_FB_URL": "http://127.0.0.1:9", "SEEDBOX_FB_USER": "x"})
bad = 0
for meth, path in (("DELETE", "/api/resources/x"), ("PUT", "/api/resources/x"), ("PATCH", "/api/resources/x"),
                   ("POST", "/api/resources/x"), ("POST", "/api/tus/x"), ("GET", "/api/users"), ("POST", "/api/login/../resources")):
    try:
        sb._req(meth, path); bad += 1
    except m.Unsafe:
        pass
    except Exception:
        bad += 1            # anything but the guardrail means it tried the network
assert not hasattr(sb, "delete") and not hasattr(m.Seedbox, "delete")
sys.exit(bad)
PY' "DELETE / PUT / PATCH / other POSTs / other paths are refused before a byte is sent, and there is no delete method"

echo "== guardrail 3: rTorrent is only ever asked one read-only question"
expect 'grep -q "^RT_COMMANDS = (\"d.name=\", \"d.complete=\", \"d.directory=\")" "$REPO/scripts/seedbox-fetch.py" && ! grep -qE "d\.(erase|stop|close|delete|set|custom1\.set|directory\.set)|(^|[^A-Za-z_])(execute|load)\.[a-z]" "$REPO/scripts/seedbox-fetch.py"' "the only rTorrent call reads name / complete / directory; no erase, stop, set, execute or load anywhere"

echo "== copy only: the seedbox was never changed by any run"
expect '[ "$TOUCHED" = 0 ] && [ "$UNTOUCHED" -ge 20 ]' "every one of $((UNTOUCHED+TOUCHED)) runs left every file and folder on the seedbox exactly as it was (path, size, mtime, sha256)"

echo
echo "SEEDBOX RESULT: $PASS passed, $FAIL failed"
exit "$FAIL"
