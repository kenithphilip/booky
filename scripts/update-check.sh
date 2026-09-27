#!/usr/bin/env bash
# update-check.sh — weekly "a newer release exists" notice for every pinned image (L06).
# Not Diun: that is another container holding the Docker socket. This reads the IMG_* pins in
# .env, asks each registry for its newest version-looking tag (the same logic as the TUI's
# Operations -> Check for updates), and alerts ONCE per new version. It never updates anything:
# Operations -> Update does that, with a backup first and a rollback.
set -uo pipefail
STACK_DIR="${STACK_DIR:-/srv/bookstack}"
ENV_FILE="$STACK_DIR/.env"
STATE="${UPDATE_STATE:-/etc/bookstack/update-check.state}"
ALERT="${UPDATE_ALERT:-$STACK_DIR/scripts/alert.sh}"
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true; touch "$STATE" 2>/dev/null || true
news=$(STATE="$STATE" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import json, os, re, urllib.request
env, state_path = os.environ["ENV_FILE"], os.environ["STATE"]
pins = {}
for line in open(env, encoding="utf-8"):
    m = re.match(r"^(IMG_[A-Z_]+)=(.*)$", line.rstrip("\n"))
    if m:
        v = m.group(2).strip().strip("'")
        if v:
            pins[m.group(1)] = v
seen = dict(l.rstrip("\n").split("=", 1) for l in open(state_path) if "=" in l)
def get(url, hdr={}):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=20))
def newest(image):
    img = image.split(":")[0]
    if img.startswith("lscr.io/"):
        img = "ghcr.io/" + img[len("lscr.io/"):]
    if img.startswith("ghcr.io/"):
        repo = img[len("ghcr.io/"):]
        tok = get(f"https://ghcr.io/token?scope=repository:{repo}:pull")["token"]
        tags = get(f"https://ghcr.io/v2/{repo}/tags/list?n=10000", {"Authorization": "Bearer " + tok})["tags"]  # one page of ALL tags: the default page missed the newest
    else:
        repo = img if "/" in img else "library/" + img
        tags = [t["name"] for t in get(f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated")["results"]]
    vt = [t for t in tags if re.fullmatch(r"v?\d+(\.\d+){1,3}", t)]
    key = lambda t: tuple(int(x) for x in t.lstrip("v").split("."))
    return max(vt, key=key) if vt else None
out, changed = [], False
for k, image in sorted(pins.items()):
    cur = image.split(":", 1)[1] if ":" in image else "latest"
    try:
        new = newest(image)
    except Exception:
        continue
    if not new or not re.fullmatch(r"v?\d+(\.\d+){1,3}", cur):
        continue                       # floating tags (:1, :latest) follow their line already
    key = lambda t: tuple(int(x) for x in t.lstrip("v").split("."))
    if key(new) > key(cur) and seen.get(k) != new:
        out.append(f"{k}: {image} -> {new}")
        seen[k] = new; changed = True
if changed:
    with open(state_path, "w") as f:
        f.writelines(f"{k}={v}\n" for k, v in seen.items())
print("\n".join(out))
PY
)
if [ -n "$news" ]; then
  "$ALERT" "Bookstack: newer releases are available" "$news

Read each project's release notes, then Operations -> Update (it backs up first and can roll back)." >/dev/null 2>&1 || true
  echo "update-check: new releases:"; echo "$news"
else
  echo "update-check: nothing new"
fi
exit 0
