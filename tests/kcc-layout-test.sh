#!/usr/bin/env bash
# v6.2: comic page layouts, end to end with the REAL KCC (ghcr.io/ciromattia/kcc:v12.0.0).
# Synthetic comics of every shape (a portrait book with two-page spreads whose margins are uneven,
# a landscape book with a portrait cover, a webtoon, newspaper dailies, a small scan) go through:
#   1. the portal's classifier (librarian/comics.py, in the portal's own test image), then
#   2. scripts/comic-convert.sh (docker exec/cp for the portal and Calibre are stood in for; KCC
#      runs for real), and
#   3. the pages KCC made are measured: halves cut ON the gutter plus the whole spread turned,
#      landscape pages turned and never cut, a webtoon cut into screens, dailies as two rows,
#      greyscale for a black-and-white Kobo, a small scan enlarged to the screen.
#   bash tests/kcc-layout-test.sh        (needs Docker; about two minutes)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMG=ghcr.io/ciromattia/kcc:v12.0.0
W="${KEEP:-}"; [ -n "$W" ] && mkdir -p "$W" || W="$(mktemp -d "${TMPDIR:-/tmp}/kcc-layout.XXXXXX")"
trap '[ -n "${KEEP:-}" ] || rm -rf "$W"' EXIT
FAILS=0
ok()   { echo "  [ OK ] $*"; }
fail() { echo "  [FAIL] $*"; FAILS=$((FAILS + 1)); }

mkdir -p "$W/src" "$W/stack/library/books/c" "$W/bin" "$W/made"
echo "== building the synthetic comics"
cat > "$W/src/mk.py" <<'PY'
import io, zipfile
from PIL import Image, ImageDraw
def jpg(im):
    b = io.BytesIO(); im.save(b, "JPEG", quality=88); return b.getvalue()
def portrait(i, w=1200, h=1800):
    im = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(im)
    d.rectangle([40, 40, w - 40, h - 40], outline=(0, 0, 160), width=8); d.text((w // 2, h // 2), str(i), fill="black")
    return im
def spread():      # uneven margins (300 px left, 30 px right); the gutter, a red line, exactly in the middle
    im = Image.new("RGB", (2400, 1800), "white"); d = ImageDraw.Draw(im)
    d.rectangle([300, 60, 2370, 1740], outline="black", width=8); d.line([1200, 0, 1200, 1800], fill=(255, 0, 0), width=12)
    return im
def landscape():   # three strips stacked on a wide page, a green band across the top strip
    im = Image.new("RGB", (2700, 2000), "white"); d = ImageDraw.Draw(im)
    for k in range(3):
        d.rectangle([60, 60 + k * 640, 2640, 600 + k * 640], outline="black", width=6)
    d.rectangle([60, 60, 2640, 120], fill=(0, 200, 0))
    return im
def tall():
    im = Image.new("RGB", (800, 4800), "white"); d = ImageDraw.Draw(im)
    for k in range(6):
        d.rectangle([40, 40 + k * 800, 760, 760 + k * 800], outline="black", width=6)
    return im
def daily():       # four panels in a row
    im = Image.new("RGB", (2720, 800), "white"); d = ImageDraw.Draw(im)
    for k in range(4):
        x = 40 + k * 670; d.rectangle([x, 40, x + 630, 760], outline="black", width=6)
    return im
books = {
    "spreads": [portrait(i) for i in range(6)] + [spread()] + [portrait(i) for i in range(6)],
    "landscape": [portrait(0)] + [landscape() for _ in range(6)],
    "webtoon": [tall() for _ in range(3)],
    "dailies": [daily() for _ in range(6)],
    "small": [portrait(i, 600, 900) for i in range(6)],
}
for name, pages in books.items():
    with zipfile.ZipFile(f"/w/src/{name}.cbz", "w") as z:
        for i, p in enumerate(pages):
            z.writestr(f"{i + 1:03d}.jpg", jpg(p))
PY
docker run --rm --user "$(id -u):$(id -g)" -v "$W:/w" --entrypoint python3 "$IMG" /w/src/mk.py || { echo "could not build the comics"; exit 1; }

echo "== 1. the portal's classifier, on real JPEGs"
docker build -q -t bookstack/librarian:test "$REPO/librarian/" >/dev/null || { echo "could not build the portal image"; exit 1; }
LAYOUTS="$(docker run --rm -e LIBRARIAN_TEST=1 -v "$W/src:/w:ro" bookstack/librarian:test python -c '
import json, comics
out = {}
for n in ("spreads", "landscape", "webtoon", "dailies", "small"):
    s = comics.page_shapes(f"/w/{n}.cbz")
    out[n] = {"layout": comics.auto_layout(s), "up": comics.wants_upscale(s, "KoLC", comics.auto_layout(s))}
print(json.dumps(out))' 2>&1 | tail -1)"
for pair in spreads:spreads landscape:rotate webtoon:strip dailies:dailies small:portrait; do
  n=${pair%%:*}; want=${pair#*:}
  got="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['$n']['layout'])" "$LAYOUTS" 2>/dev/null)"
  [ "$got" = "$want" ] && ok "$n -> $want" || fail "$n: $got (wanted $want; $LAYOUTS)"
done
up="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['small']['up'], d['spreads']['up'])" "$LAYOUTS" 2>/dev/null)"
[ "$up" = "True False" ] && ok "the small scan is enlarged, a full-size one is not" || fail "upscale: $up"

echo "== 2. scripts/comic-convert.sh with the real KCC"
REAL_DOCKER="$(command -v docker)"
cat > "$W/bin/docker" <<PY
#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
a = sys.argv[1:]
if a[:1] == ["run"]:
    sys.exit(subprocess.run(["$REAL_DOCKER"] + a).returncode)
if a[:1] == ["cp"]:                                      # the Kobo copy handed to "Calibre": kept to measure
    shutil.copyfile(a[1], os.path.join("$W/made", os.environ["BOOK"] + ".epub")); sys.exit(0)
if "admin_cli" in a:
    cmd = a[a.index("admin_cli") + 1:]
    if cmd[:2] == ["comics", "kobo-due"]:
        print(json.dumps({"ok": True, "rows": [json.loads(os.environ["ROW"])] if os.environ.get("ROW") else []}))
    elif cmd[:2] == ["comics", "kindle-due"]:
        print(json.dumps({"ok": True, "rows": [json.loads(os.environ["KROW"])] if os.environ.get("KROW") else []}))
    else:
        open("$W/results", "a").write(json.dumps(cmd) + "\n"); print(json.dumps({"ok": True, "status": "done"}))
    sys.exit(0)
if "list" in a:
    print(json.dumps([{"tags": ["Comics", "owner:bob"], "formats": ["/x/a.cbz", "/x/a.kepub"]}]))
PY
chmod +x "$W/bin/docker"
printf 'COMICS_ENABLED=true\n' > "$W/stack/.env"
BOOK_ID=0
convert() {  # name layout profile colour upscale
  # each book its own id, as in the library: Docker on a Mac (folders shared into a VM) showed KCC a
  # stale, empty folder when one work folder was deleted and made again for the next book
  BOOK_ID=$((BOOK_ID + 1))
  cp "$W/src/$1.cbz" "$W/stack/library/books/c/$1.cbz"
  local row; row="$(printf '{"calibre_id": %s, "rel": "c/%s.cbz", "title": "%s", "kind": "comic", "layout": "%s", "profile": "%s", "colour": %s, "upscale": %s}' "$BOOK_ID" "$1" "$1" "$2" "$3" "$4" "$5")"
  BOOK="$1" ROW="$row" PATH="$W/bin:$PATH" STACK_DIR="$W/stack" COMIC_ALERT=/bin/true COMIC_METAPUSH_LOCK="$W/lock" \
    bash "$REPO/scripts/comic-convert.sh" > "$W/$1.log" 2>&1
  [ -s "$W/made/$1.epub" ] && ok "$1 ($2, $3): KCC made the Kobo copy" || fail "$1: no Kobo copy: $(tail -3 "$W/$1.log")"
}
convert spreads spreads KoLC true false
convert landscape rotate KoLC true false
convert webtoon strip KoLC true false
convert dailies dailies KoLC true false
convert small portrait KoC false true
# a Kindle send: the landscape book for a Kindle Paperwhite (2021), greyscale, KCC's Send-to-Kindle format
cp "$W/src/landscape.cbz" "$W/stack/library/books/c/kindle.cbz"
KROW='{"job": 7, "calibre_id": 9, "rel": "c/kindle.cbz", "title": "kindle", "kind": "comic", "max_mb": 45, "layout": "rotate", "profile": "KPW5", "colour": false, "upscale": false}' \
  BOOK=kindle PATH="$W/bin:$PATH" STACK_DIR="$W/stack" COMIC_ALERT=/bin/true COMIC_METAPUSH_LOCK="$W/lock" \
  bash "$REPO/scripts/comic-convert.sh" > "$W/kindle.log" 2>&1
if [ -s "$W/stack/library/staging/kindle-comics/7/comic.epub" ]; then
  cp "$W/stack/library/staging/kindle-comics/7/comic.epub" "$W/made/kindle.epub"; ok "Kindle: KCC made the Send-to-Kindle copy for the Paperwhite"
else fail "Kindle: no copy: $(tail -3 "$W/kindle.log")"; fi
grep -q '"kindle-result", "7", "ok"' "$W/results" && ok "and the portal was told, with the file to mail" || fail "kindle result: $(grep kindle "$W/results")"
grep -c '"kobo-result"' "$W/results" 2>/dev/null | grep -q '^5$' && ok "each result reported back to the portal, with what it was made for" || fail "results: $(cat "$W/results" 2>/dev/null)"

echo "== 3. the pages KCC made"
cat > "$W/measure.py" <<'PY'
import glob, json, re, sys, zipfile, io
from PIL import Image
out = {}
for f in sorted(glob.glob("/w/made/*.epub")):
    name = f.rsplit("/", 1)[1][:-5]
    with zipfile.ZipFile(f) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        spine = re.findall(r'idref="([^"]+)"', z.read(opf).decode())
        imgs = sorted(n for n in z.namelist() if "/Images/" in n and "cover" not in n and n.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
        pages = []
        for n in imgs:
            im = Image.open(io.BytesIO(z.read(n))).convert("RGB"); w, h = im.size
            red = [x for x in range(w) if sum(1 for y in range(0, h, max(1, h // 40))
                   if (lambda p: p[0] > 170 and p[1] < 100 and p[2] < 100)(im.getpixel((x, y)))) > 10]
            green_rows = [y for y in range(0, h, 4) if sum(1 for x in range(0, w, max(1, w // 40))
                          if (lambda p: p[1] > 150 and p[0] < 100 and p[2] < 100)(im.getpixel((x, y)))) > 10]
            green_cols = [x for x in range(0, w, 4) if sum(1 for y in range(0, h, max(1, h // 40))
                          if (lambda p: p[1] > 150 and p[0] < 100 and p[2] < 100)(im.getpixel((x, y)))) > 10]
            px = im.resize((32, 32)).tobytes()
            grey = all(abs(px[i] - px[i + 1]) < 8 and abs(px[i + 1] - px[i + 2]) < 8 for i in range(0, len(px), 3))
            pages.append({"n": n.rsplit("/", 1)[1], "w": w, "h": h, "red": [min(red), max(red)] if red else None,
                          "green_rows": len(green_rows), "green_cols": len(green_cols), "grey": grey})
    out[name] = {"spine": len(spine), "pages": pages}
print(json.dumps(out))
PY
M="$(docker run --rm --user "$(id -u):$(id -g)" -v "$W:/w" --entrypoint python3 "$IMG" /w/measure.py)"
check() { python3 - "$M" <<PY && ok "$2" || fail "$2"
import json, sys
d = json.loads(sys.argv[1])
$1
PY
}
check '
p = d["spreads"]["pages"]
halves = [x for x in p if x["n"].endswith(("-b.jpg", "-c.jpg"))]
turned = [x for x in p if x["n"].endswith("-d.jpg")]
assert len(p) == 15, len(p)
assert len(halves) == 2 and len(turned) == 1
b, c = sorted(halves, key=lambda x: x["n"])
assert b["red"] and b["red"][0] >= b["w"] - 12 and c["red"] and c["red"][1] <= 12, (b, c)
assert turned[0]["w"] < turned[0]["h"]
' "spreads: each half, cut ON the gutter (uneven margins), then the whole spread turned"
check '
p = d["landscape"]["pages"]
assert len(p) == 7, len(p)
wide = p[1:]
assert all(x["h"] > x["w"] for x in wide), wide
assert all(x["green_cols"] > 0 and x["green_rows"] == 0 for x in wide), wide
' "landscape: every wide page turned (its top strip now runs down the side), none cut; the cover as it is"
check '
p = d["webtoon"]["pages"]
assert len(p) == 18 and all(700 <= x["h"] <= 800 for x in p), [(x["w"], x["h"]) for x in p]
' "webtoon: the three tall pages cut into screens between the panels (18 panels, each whole)"
check '
p = d["dailies"]["pages"]
assert len(p) == 6 and all(x["w"] == 1264 and x["h"] < 1680 and x["h"] > 1000 for x in p), p
' "dailies: each row of four panels as two rows filling the width, not turned"
check '
p = d["small"]["pages"]
assert len(p) == 6 and all(x["grey"] for x in p), p
assert all(x["h"] == 1448 for x in p), p
' "small scan for a Kobo Clara BW: greyscale, enlarged to its 1448-pixel screen"
check '
p = d["kindle"]["pages"]
assert len(p) == 7 and all(x["grey"] for x in p), [(x["w"], x["h"], x["grey"]) for x in p]
assert all(x["w"] > x["h"] and x["w"] >= 1600 for x in p[1:]), [(x["w"], x["h"]) for x in p]
' "Kindle Paperwhite: greyscale, wide pages kept whole and upright, large (its Send-to-Kindle format frames every page alike: a turned page was a strip)"

echo
echo "KCC LAYOUT RESULT: $FAILS failed"
exit $(( FAILS > 0 ))
