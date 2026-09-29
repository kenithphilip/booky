#!/usr/bin/env bash
# disk-report.sh — the admin's daily disk summary on the alert channel (ntfy), once a day at
# DISK_REPORT_HOUR (09 by default; /etc/cron.d/bookstack-diskreport, installed by Deploy).
# Disk, not RAM, is what this box runs out of first (80 GB): the hourly watchdog
# (disk-watch.sh) shouts at 85 %, this says every morning where the space went and how fast it
# is going, so 85 % is never a surprise:
#   Disk 52% used, 37.5 GB free           <- the title
#   +0.8 GB since yesterday · 7-day average +0.5 GB a day · 85% in about 52 days
#   Ebooks 14.2 GB · Audiobooks 18.4 GB · Seedbox copies 1.3 GB · ...
# Priority low (silent, in the drawer) while under DISK_WARN_PCT, default at or over it. The same
# Sequence-ID every day, so today's report replaces yesterday's instead of piling up.
# DISK_REPORT=false in .env turns it off (Deploy then removes the cron file).
set -uo pipefail
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
ALERT="${DISK_REPORT_ALERT:-$STACK_DIR/scripts/alert.sh}"
HIST="${DISK_HISTORY:-/etc/bookstack/disk-history}"
envget(){ local raw; raw=$({ grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-)
  if [[ "$raw" == \'*\' && "${#raw}" -ge 2 ]]; then raw="${raw:1:${#raw}-2}"; local bs=\\ q=\'; raw="${raw//"$bs$q"/$q}"; fi; printf '%s' "$raw"; }
num(){ local v="${!1:-}"; [ -n "$v" ] || v=$(envget "$1"); case "$v" in ''|*[!0-9]*) v="$2";; esac; printf '%s' "$v"; }
[ "$(envget DISK_REPORT)" = false ] && exit 0
WARN_PCT=$(num DISK_WARN_PCT 85)

# GB, or MB under one (a young library is tens of MB: "0.0 GB" said nothing)
gb(){ awk -v b="${1:-0}" 'BEGIN{ s = b < 0 ? "-" : ""; if (b < 0) b = -b;
  if (b < 1073741824) printf "%s%.0f MB", s, b / 1048576; else printf "%s%.1f GB", s, b / 1073741824 }'; }
# bytes under the given directories, each file counted once (du skips a hard link it has seen
# already, which is how library/seedbox shares its files with library/seedbox-sync)
bytes(){ local d=() p; for p in "$@"; do [ -e "$STACK_DIR/$p" ] && d+=("$STACK_DIR/$p"); done
  [ "${#d[@]}" -gt 0 ] || { echo 0; return; }
  nice -n 19 du -scb "${d[@]}" 2>/dev/null | tail -1 | cut -f1; }
dfb(){ df -B1 --output="$1" "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9; }

size=$(dfb size); used=$(dfb used); avail=$(dfb avail)
if [ -z "$size" ] || [ "$size" = 0 ]; then echo "disk-report: df gave no answer for $STACK_DIR" >&2; exit 1; fi
pct=$(dfb pcent); pct="${pct:-0}"                        # df's own figure, the one disk-watch.sh acts on
ipct=$(df -i --output=ipcent "$STACK_DIR" 2>/dev/null | tail -1 | tr -dc 0-9)

# ---- where it went ---------------------------------------------------------------------------
ebooks=$(bytes library/books)
audio=$(bytes library/audiobooks library/podcasts)
seed=$(bytes library/seedbox-sync library/seedbox)
waiting=$(bytes library/ingest library/staging library/dropbox downloads)
appdata=$(bytes cwa abs librarian/state shelfmark caddy/data uptime-kuma syncthing authelia)
dsf=$(docker system df --format '{{.Type}}={{.Size}}={{.Reclaimable}}' 2>/dev/null)
docker_imgs=$(printf '%s\n' "$dsf" | sed -n 's/^Images=\([^=]*\)=.*/\1/p' | head -1)
docker_cache=$(printf '%s\n' "$dsf" | sed -n 's/^Build Cache=\([^=]*\)=.*/\1/p' | head -1)
docker_free=$(printf '%s\n' "$dsf" | sed -n 's/^Images=[^=]*=\([^ ]*\).*/\1/p' | head -1)
# comics live in the same Calibre library: their share of it, from Calibre's own sizes
comics=$(python3 - "$STACK_DIR/library/books/metadata.db" 2>/dev/null <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(c.execute("""SELECT COALESCE(SUM(d.uncompressed_size), 0) FROM data d WHERE EXISTS (
        SELECT 1 FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE l.book=d.book
        AND t.name IN ('Comics','Manga','Manhwa','Manhua'))""").fetchone()[0])
except Exception:
    print(0)
PY
)
comics=${comics:-0}; case "$comics" in ''|*[!0-9]*) comics=0;; esac
known=$(( ebooks + audio + seed + waiting + appdata ))
other=$(( used - known )); [ "$other" -lt 0 ] && other=0

# ---- how fast it is going ----------------------------------------------------------------------
today=$(date +%F)
mkdir -p "$(dirname "$HIST")" 2>/dev/null
prev=$(grep -v "^$today " "$HIST" 2>/dev/null | tail -1)
week=$(grep -v "^$today " "$HIST" 2>/dev/null | tail -7 | head -1)
{ grep -v "^$today " "$HIST" 2>/dev/null | tail -59; echo "$today $used"; } > "$HIST.tmp" 2>/dev/null && mv -f "$HIST.tmp" "$HIST"
trend=""
if [ -n "$prev" ]; then
  d=$(( used - ${prev#* } ))
  trend="$( [ "$d" -ge 0 ] && printf '+' )$(gb "$d") since yesterday"
  if [ -n "$week" ] && [ "${week%% *}" != "${prev%% *}" ]; then
    days=$(( ( $(date -d "$today" +%s) - $(date -d "${week%% *}" +%s) ) / 86400 ))
    if [ "$days" -gt 0 ]; then
      rate=$(( (used - ${week#* }) / days ))
      trend="$trend · $days-day average $( [ "$rate" -ge 0 ] && printf '+' )$(gb "$rate") a day"
      warn_at=$(( size * WARN_PCT / 100 ))
      if [ "$rate" -gt 0 ] && [ "$used" -lt "$warn_at" ]; then
        trend="$trend · ${WARN_PCT}% in about $(( (warn_at - used) / rate )) days"
      fi
    fi
  fi
else
  trend="First report: the trend starts tomorrow"
fi

mem=$(free -b 2>/dev/null | awk '/^Mem:/{t=$2; u=$3} /^Swap:/{s=$3} END{ if (t) printf "Memory: %.1f of %.1f GB in use, swap %.1f GB", u/1073741824, t/1073741824, s/1073741824 }')

text="Used $(gb "$used") of $(gb "$size") (${pct}%), $(gb "$avail") free${ipct:+ · inodes ${ipct}%}
$trend

Ebooks $(gb "$ebooks")$([ "$comics" -gt 0 ] && printf ' (comics %s of it)' "$(gb "$comics")") · Audiobooks $(gb "$audio") · Seedbox copies $(gb "$seed")
Imports waiting $(gb "$waiting") · App data $(gb "$appdata")
System, Docker and the rest $(gb "$other")${docker_imgs:+ (Docker images $docker_imgs${docker_free:+, $docker_free of it old versions or unused}${docker_cache:+; build cache $docker_cache})}
${mem}"

if [ "$pct" -ge "$WARN_PCT" ] || [ "${ipct:-0}" -ge "$WARN_PCT" ]; then prio=default; tags=warning; else prio=low; tags=floppy_disk; fi
ALERT_SEQ=disk-daily ALERT_TAGS="$tags" "$ALERT" "Disk ${pct}% used, $(gb "$avail") free" "$text" "$prio" >/dev/null 2>&1 || true
printf '%s\n' "$text"
exit 0
