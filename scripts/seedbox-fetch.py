#!/usr/bin/env python3
"""seedbox-fetch.py — hand finished seedbox downloads to Shelfmark (Library -> Seedbox).

Shelfmark sends a reader's pick to the seedbox's SABnzbd (Usenet) or rTorrent (torrents), which
download it on the SEEDBOX's disk. Syncthing carries it to this server:

  seedbox Syncthing (SEND ONLY) --> this server's Syncthing (container "syncthing", RECEIVE ONLY)
                                    into $STACK_DIR/library/seedbox-sync/<sub>/
  this job, every 20 seconds: each item that is finished AND fully arrived is hard-linked (copied
  when it cannot be) into $STACK_DIR/library/seedbox/<sub>/, which Shelfmark sees as /seedbox;
  Shelfmark's remote path mappings find it there and file it into that reader's dropbox.

NOTHING ON THE SEEDBOX IS EVER MOVED, DELETED OR CHANGED (private trackers: strict seeding
rules). Enforced in layers, each on its own enough:
  1. this server's side of every folder is RECEIVE ONLY: Syncthing never sends a change made here
     (measured in tests/seedbox-test.sh: files deleted, edited and added here leave the seedbox
     byte-identical even with the seedbox side wrongly set to Send & Receive). Checked every run;
     a folder found otherwise is PAUSED at once and nothing runs until it is fixed;
  2. the seedbox's side is SEND ONLY (Library -> Seedbox says where): it ignores all changes
     from other devices;
  3. this job never writes into the synced copy, except to drop an item it handed over a week
     ago, and only after Syncthing has been told to ignore that item. Every request to this
     server's Syncthing goes through one allowlist (no revert, no override, no config writes
     outside --setup);
  4. rTorrent is asked one fixed, read-only question (d.multicall2 of d.name / d.complete /
     d.directory / d.timestamp.finished) and nothing else.
Shelfmark's own clean-up is pinned in docker-compose.yml (torrents: keep; Usenet: copy).

An item is handed over when:
  * the seedbox is connected and Syncthing needs nothing more under that item;
  * nothing in it has changed for MIN_AGE (1 minute);
  * torrents only: rTorrent reports it complete, at least RT_SETTLE (90 s) ago. rTorrent writes
    into full-size files in place, so a copy the seedbox's Syncthing scanned mid-download looks
    whole; the wait lets its file watcher (10 s) catch the last pieces first.
  The whole hand-over has to fit in Shelfmark's FIVE minutes: v1.3.15 and v1.4.0 (and upstream main,
  2026-09-28) cancels a download after STALL_TIMEOUT = 300 s without progress, and its "Waiting
  for completed files" loop does not count as progress, whatever Completed Path Wait says. A
  hand-over that misses it (a big audiobook) still arrives; the reader presses Retry in Shelfmark,
  which finds the torrent/job already complete and imports it.
Each item is remembered by its names, sizes and times: handed over once, again only if it
changes. It appears under library/seedbox/ only whole (built in .incoming/, renamed into place).
After KEEP_DAYS both the hand-over and the synced copy go (the latter ignored in Syncthing
first). v6.1.1: a day by default (SEEDBOX_KEEP_DAYS in .env, Advanced settings -> disk): what was
handed over has been imported long before, and one asked for again later is fetched back. Standard library only; root.

Config: /etc/bookstack/seedbox.env (0600; Library -> Seedbox writes it). Syncthing's API key:
SYNCTHING_API_KEY in $STACK_DIR/.env (the container reads it). State and the failure latch:
/etc/bookstack/seedbox.state (JSON).
"""
import base64, fcntl, hashlib, http.client, json, os, re, shutil, subprocess, sys, time, uuid
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

STACK = os.environ.get("STACK_DIR", "/srv/bookstack")
CONF = os.environ.get("SEEDBOX_ENV", "/etc/bookstack/seedbox.env")
STATE = os.environ.get("SEEDBOX_STATE", "/etc/bookstack/seedbox.state")
MIRROR = os.environ.get("SEEDBOX_MIRROR", os.path.join(STACK, "library/seedbox"))
SYNC = os.environ.get("SEEDBOX_SYNC", os.path.join(STACK, "library/seedbox-sync"))
ST_SYNC = os.environ.get("SEEDBOX_ST_SYNC", "/sync")            # the same folder inside the container
ST_URL = os.environ.get("SEEDBOX_ST_URL", "http://127.0.0.1:8384")
ALERT = os.environ.get("SEEDBOX_ALERT", os.path.join(STACK, "scripts/alert.sh"))
# What Shelfmark is waiting for right now (the portal writes it, librarian/worker.py
# _export_waiting): a download the seedbox has had for longer than KEEP_DAYS was dropped here
# and ignored in Syncthing; asked for again, this brings it back.
WANTED = os.environ.get("SEEDBOX_WANTED", os.path.join(STACK, "librarian/state/seedbox-wanted.json"))
WANTED_FRESH = 180
MIN_AGE = int(os.environ.get("SEEDBOX_MIN_AGE", "60"))           # seconds an item must be unchanged
RT_SETTLE = int(os.environ.get("SEEDBOX_RT_SETTLE", "90"))       # seconds after rTorrent finished it
KEEP_DAYS = 1.0         # hand-overs and synced copies here: set from SEEDBOX_KEEP_DAYS below (v6.1.1)
FREE_MARGIN = int(os.environ.get("SEEDBOX_FREE_MARGIN_GB", "5")) * 2**30
FAILS_BEFORE_ALERT = int(os.environ.get("SEEDBOX_FAILS_BEFORE_ALERT", "45"))   # 15 minutes of 20 s runs
SKIP_PREFIX = ("_UNPACK_", "_FAILED_", "_ADMIN_", ".")
SAB_IGNORES = ["/_UNPACK_*", "/_FAILED_*", "/_ADMIN_*"]            # SABnzbd's work in progress
DEVICE_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")
# guardrail 3: the only requests this job may send to THIS server's Syncthing. (method, path);
# a path ending in "/" is a prefix. PATCH of a folder is further limited to {"paused": true}.
ST_RUN = {("GET", "/rest/system/status"), ("GET", "/rest/system/connections"),
          ("GET", "/rest/config/folders/"), ("GET", "/rest/db/status"), ("GET", "/rest/db/need"),
          ("GET", "/rest/db/browse"), ("GET", "/rest/db/completion"),
          ("GET", "/rest/db/ignores"), ("POST", "/rest/db/ignores"),
          ("PATCH", "/rest/config/folders/")}
ST_SETUP = ST_RUN | {("GET", "/rest/config/folders"), ("GET", "/rest/config/devices"), ("GET", "/rest/config/gui"),
                     ("PATCH", "/rest/config/options"), ("PATCH", "/rest/config/gui"),
                     ("POST", "/rest/config/devices"), ("PATCH", "/rest/config/devices/"),
                     ("DELETE", "/rest/config/devices/"), ("POST", "/rest/config/folders"),
                     ("DELETE", "/rest/config/folders/")}
# guardrail 4: the one, read-only rTorrent question
RT_COMMANDS = ("d.name=", "d.complete=", "d.directory=", "d.timestamp.finished=")


class Unsafe(RuntimeError):
    """A guardrail refused: nothing is handed over until it is fixed."""


def envfile(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                if len(v) >= 2 and v[0] == v[-1] == "'":
                    v = v[1:-1].replace("'\\''", "'")
                out[k.strip()] = v
    except OSError:
        pass
    return out



def _keep_days():
    """SEEDBOX_KEEP_DAYS: the environment (tests), else .env (Advanced settings), else 1."""
    v = os.environ.get("SEEDBOX_KEEP_DAYS") or envfile(os.path.join(STACK, ".env")).get("SEEDBOX_KEEP_DAYS") or "1"
    try:
        return max(0.0, float(v))            # 0: dropped at the next run after the hand-over
    except ValueError:
        return 1.0


KEEP_DAYS = _keep_days()

def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def alert(title, body, prio="high", seq="seedbox", tags=None):
    """scripts/alert.sh. seq: a problem and its all-clear share one, so on the admin's phone the
    all-clear replaces the problem."""
    env = dict(os.environ, ALERT_SEQ=seq, **({"ALERT_TAGS": tags} if tags else {}))
    try:
        subprocess.run([ALERT, title, body, prio], capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        pass


class Syncthing:
    """This server's Syncthing, through the allowlist."""

    def __init__(self, key, allowed=ST_RUN):
        if not key:
            raise RuntimeError("SYNCTHING_API_KEY is not set in .env (Library -> Seedbox -> Change the settings)")
        self.key, self.allowed = key, allowed

    def req(self, method, path, query=None, body=None, timeout=30):
        if not any(method == m and (path.startswith(p) if p.endswith("/") else path == p) for m, p in self.allowed):
            raise Unsafe(f"refused to send {method} {path} to Syncthing: not on this job's list")
        if self.allowed is ST_RUN and method == "PATCH" and body != {"paused": True}:
            raise Unsafe(f"refused PATCH {path} {body}: this job may only pause a folder")
        url = ST_URL.rstrip("/") + urllib.parse.quote(path, safe="/") + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode()
        h = {"X-API-Key": self.key, "Content-Type": "application/json", "User-Agent": "bookstack-seedbox/2"}
        raw = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout).read()
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode(errors="replace")

    def folder(self, fid):
        try:
            return self.req("GET", "/rest/config/folders/" + fid)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise


def rtorrent_complete(c):
    """{name: finished time (0 = unknown)} of the torrents rTorrent reports COMPLETE whose data
    sits directly in the watched folder."""
    url = c.get("SEEDBOX_RT_URL", "")
    params = "".join(f"<param><value><string>{x}</string></value></param>" for x in ("", "main") + RT_COMMANDS)
    body = f'<?xml version="1.0"?><methodCall><methodName>d.multicall2</methodName><params>{params}</params></methodCall>'.encode()
    h = {"Content-Type": "text/xml", "User-Agent": "bookstack-seedbox/2"}
    if c.get("SEEDBOX_RT_USER"):
        raw = f'{c["SEEDBOX_RT_USER"]}:{c.get("SEEDBOX_RT_PASS", "")}'.encode()
        h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=60).read())
    if root.find("fault") is not None:
        raise RuntimeError("rTorrent XML-RPC fault")
    base = c.get("SEEDBOX_RT_DIR", "").rstrip("/")
    done = {}
    outer = root.find("./params/param/value/array/data")
    for v in (outer.findall("value") if outer is not None else []):
        row = v.find("array/data")
        vals = [((list(x)[0].text if list(x) else x.text) or "") for x in row.findall("value")] if row is not None else []
        if len(vals) < 4:
            continue
        name, complete, directory = vals[0], vals[1].strip(), vals[2].rstrip("/")
        # a multi-file torrent's d.directory is <base>/<name>, a single file's is <base>
        if complete == "1" and directory in (base, base + "/" + name):
            try:
                done[name] = int(vals[3].strip() or 0)
            except ValueError:
                done[name] = 0
    return done


def sources(c):
    """[(Syncthing folder ID, subdir, 'sab' or 'rt')] from SEEDBOX_SOURCES ('id|sub|kind;...')."""
    out = []
    for part in (c.get("SEEDBOX_SOURCES") or "").split(";"):
        bits = [b.strip() for b in part.split("|")]
        if len(bits) == 3 and re.fullmatch(r"bookstack-[A-Za-z0-9._-]+", bits[0]) and bits[2] in ("sab", "rt") \
                and bits[1] and not bits[1].startswith("/") and ".." not in bits[1].split("/"):
            out.append(tuple(bits))
    return out


def walk(path):
    """[(relative path, size, mtime)] for every file under an item (the item itself when a file),
    sorted, so two walks compare equal only when nothing changed in between. Symlinks skipped."""
    name = os.path.basename(path)
    if os.path.islink(path):
        return []
    if not os.path.isdir(path):
        s = os.stat(path)
        return [(name, s.st_size, int(s.st_mtime))]
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                s = os.lstat(p)
                out.append((os.path.join(name, os.path.relpath(p, path)), s.st_size, int(s.st_mtime)))
    return sorted(out)


def signature(files):
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()[:24]


def inside(path, root):
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp != rr and rp.startswith(rr + os.sep)


def hand_over(src, dest_dir, files, sig, uid, gid):
    """Hard-link (or copy) one item whole into dest_dir; bytes. The synced copy is only read."""
    total = sum(sz for _, sz, _ in files)
    os.makedirs(dest_dir, exist_ok=True)
    d = dest_dir                                    # library/seedbox/<sub>: Shelfmark's, not root's
    while os.path.realpath(d).startswith(os.path.realpath(MIRROR) + os.sep):
        try:
            os.chown(d, uid, gid)
        except OSError:
            pass
        d = os.path.dirname(d)
    stage = os.path.join(MIRROR, ".incoming", uuid.uuid4().hex)
    os.makedirs(stage)
    try:
        base = os.path.dirname(src)
        same_fs = os.stat(base).st_dev == os.stat(stage).st_dev
        if not same_fs and shutil.disk_usage(stage).free < total + FREE_MARGIN:
            raise OSError(f"not enough free space for {os.path.basename(src)} ({total // 2**20} MiB)")
        for rel, size, _ in files:
            s, d = os.path.join(base, rel), os.path.join(stage, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            try:
                os.link(s, d)                       # the same bytes, no second copy on disk
            except OSError:
                shutil.copyfile(s, d)
            if os.path.getsize(d) != size:
                raise OSError(f"{rel}: {os.path.getsize(d)} bytes, expected {size}")
        if signature(walk(src)) != sig:
            raise OSError(f"{os.path.basename(src)} changed while it was handed over; again next minute")
        for root, dirs, _ in os.walk(stage):         # only the new folders: never chown a linked file
            for n in [root] + [os.path.join(root, x) for x in dirs]:
                try:
                    os.chown(n, uid, gid)
                except OSError:
                    pass
        final = os.path.join(dest_dir, os.path.basename(src))
        if os.path.lexists(final):
            shutil.rmtree(final) if os.path.isdir(final) and not os.path.islink(final) else os.unlink(final)
        os.replace(os.path.join(stage, os.path.basename(src)), final)      # whole, in one step
        return total
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def ignore_line(name):
    """A Syncthing ignore pattern matching exactly one top-level item; None when unsafe to express."""
    if not name or "\n" in name or "\r" in name or name != name.strip():
        return None
    return "/" + re.sub(r"([\\*?\[\]{}])", r"\\\1", name)


def set_ignores(stc, fid, add=(), drop=()):
    cur = (stc.req("GET", "/rest/db/ignores", {"folder": fid}) or {}).get("ignore") or []
    new = [x for x in cur if x not in drop] + [x for x in add if x not in cur]
    if new != cur:
        stc.req("POST", "/rest/db/ignores", {"folder": fid}, {"ignore": new})
        got = (stc.req("GET", "/rest/db/ignores", {"folder": fid}) or {}).get("ignore") or []
        if any(x not in got for x in add) or any(x in got for x in drop):
            raise RuntimeError(f"Syncthing did not take the ignore list of {fid}")


def guard(stc, c, srcs, act=True):
    """Guardrail 1: every folder is RECEIVE ONLY here. One that is not is paused at once."""
    bad = []
    for fid, sub, _ in srcs:
        f = stc.folder(fid)
        if f is None:
            raise RuntimeError(f"Syncthing has no folder {fid}; Library -> Seedbox -> Change the settings sets it up")
        if f.get("type") != "receiveonly":
            if act and not f.get("paused"):
                stc.req("PATCH", "/rest/config/folders/" + fid, body={"paused": True})
            bad.append(f"{fid} is '{f.get('type')}'")
        elif f.get("path", "").rstrip("/") != ST_SYNC + "/" + sub:
            raise RuntimeError(f"Syncthing folder {fid} points at {f.get('path')}, not {ST_SYNC}/{sub}")
    if bad:
        raise Unsafe("on this server the Syncthing folder " + ", ".join(bad) + " instead of Receive Only. "
                     "It is PAUSED, so nothing made here can reach the seedbox. Library -> Seedbox -> "
                     "Change the settings puts it back to Receive Only.")


def connected(stc, dev):
    return bool(((stc.req("GET", "/rest/system/connections") or {}).get("connections") or {}).get(dev, {}).get("connected"))


def _words(s):
    s = re.sub(r"\.(epub|mobi|azw3?|pdf|cbz|fb2|m4b|mp3|zip|rar)$", "", (s or "").lower())
    return [w for w in re.split(r"[^a-z0-9]+", s) if w]


def wanted_now(now):
    """[(title words, author words)] Shelfmark is waiting for; [] when the list is stale."""
    try:
        d = json.load(open(WANTED, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if now - float(d.get("at") or 0) > WANTED_FRESH:
        return []
    return [(_words(w.get("title")), _words(w.get("author"))) for w in d.get("waiting") or [] if _words(w.get("title"))]


def is_wanted(name, wanted):
    """Every word of the title is in the item's name and, when an author is known, one of theirs
    too (a one-word title alone, like 'Emma', is not enough)."""
    have = set(_words(name))
    for title, author in wanted:
        if set(title) <= have and ((author and set(author) & have) or (not author and len(" ".join(title)) >= 12)):
            return True
    return False


def prune(now, subs, copied):
    """Hand-overs Shelfmark never claimed (the reader cancelled, a mapping was wrong) go after
    KEEP_DAYS, counted from the hand-over (a linked file keeps the seedbox's own time, so its
    mtime says nothing); a staging leftover from a run that died mid-way after a day."""
    gone = 0
    for sub in subs:
        d = os.path.join(MIRROR, sub)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            p = os.path.join(d, n)
            at = (copied.get(sub + "/" + n) or {}).get("at") or os.lstat(p).st_mtime
            if at < now - KEEP_DAYS * 86400:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
                gone += 1
    inc = os.path.join(MIRROR, ".incoming")
    for n in (os.listdir(inc) if os.path.isdir(inc) else []):
        if os.path.getmtime(os.path.join(inc, n)) < now - 86400:
            shutil.rmtree(os.path.join(inc, n), ignore_errors=True)
    return gone


def run():
    c = envfile(CONF)
    srcs = sources(c)
    if not c.get("SEEDBOX_ST_DEVICE") or not srcs:
        print("seedbox: not configured (Library -> Seedbox)")
        return 0
    env = envfile(os.path.join(STACK, ".env"))
    uid, gid = int(env.get("PUID") or 1000), int(env.get("PGID") or 1000)
    st = load_state()
    copied, seen = st.setdefault("copied", {}), st.setdefault("seen", {})
    now = time.time()
    handed, trimmed, notes = 0, 0, []
    try:
        stc = Syncthing(env.get("SYNCTHING_API_KEY"))
        guard(stc, c, srcs)
        dev = c["SEEDBOX_ST_DEVICE"]
        up = connected(stc, dev)
        rt_done = None
        wanted = wanted_now(now)
        for fid, sub, kind in srcs:
            local_root, dest = os.path.join(SYNC, sub), os.path.join(MIRROR, sub)
            if (stc.folder(fid) or {}).get("paused"):
                notes.append(f"{fid} is paused in Syncthing")
                continue
            names = sorted(n for n in (os.listdir(local_root) if os.path.isdir(local_root) else [])
                           if not n.startswith(SKIP_PREFIX))
            # the seedbox's list (Syncthing's global view): what is still there to forget the rest
            on_seedbox = {e["name"] for e in (stc.req("GET", "/rest/db/browse", {"folder": fid, "levels": 0}) or [])}
            # asked for again after this server dropped its copy: stop ignoring it, so Syncthing
            # brings it back, and forget the old hand-over so it is handed over afresh
            for key in [k for k, r in copied.items() if k.startswith(sub + "/") and r.get("trimmed")]:
                name = key[len(sub) + 1:]
                if name in on_seedbox and is_wanted(name, wanted) and ignore_line(name):
                    set_ignores(stc, fid, drop=[ignore_line(name)])
                    del copied[key]
                    seen.pop(key, None)
                    print(f"seedbox: bringing back {key} (Shelfmark is waiting for it)")
            if up:
                need = stc.req("GET", "/rest/db/need", {"folder": fid, "perpage": 1000000}) or {}
                pending = {e["name"].split("/", 1)[0] for part in ("progress", "queued", "rest") for e in need.get(part) or []}
                if kind == "rt" and rt_done is None and names:
                    rt_done = rtorrent_complete(c) if c.get("SEEDBOX_RT_URL") else {}
                for name in names:
                    key, path = sub + "/" + name, os.path.join(local_root, name)
                    if (copied.get(key) or {}).get("trimmed") or name in pending:
                        seen.pop(key, None)
                        continue                    # still arriving (or already dealt with)
                    if kind == "rt":
                        if name not in (rt_done or {}):
                            continue                # rTorrent is still downloading it
                        fin = rt_done[name] or st.setdefault("rt_first", {}).setdefault(key, int(now))
                        if now - fin < RT_SETTLE:
                            continue                # let the seedbox's rescan catch the last pieces
                    files = walk(path)
                    if not files or any(os.path.basename(r).startswith(".syncthing.") for r, _, _ in files):
                        continue
                    sig = signature(files)
                    if (seen.get(key) or {}).get("sig") != sig:
                        seen[key] = {"sig": sig, "since": int(now)}
                        continue                    # changed since last minute: wait for it to settle
                    if now - seen[key]["since"] < MIN_AGE or (copied.get(key) or {}).get("sig") == sig:
                        continue
                    size = hand_over(path, dest, files, sig, uid, gid)
                    copied[key] = {"sig": sig, "at": int(now), "bytes": size}
                    handed += 1
                    print(f"seedbox: handed over {key} ({size // 2**20} MiB)")
            # after a week: stop syncing a handed-over item and drop this server's copy of it
            for key, rec in copied.items():
                name = key[len(sub) + 1:]
                if not key.startswith(sub + "/") or rec.get("trimmed") or rec.get("at", now) > now - KEEP_DAYS * 86400:
                    continue
                line, path = ignore_line(name), os.path.join(local_root, name)
                if line is None:
                    notes.append(f"{key}: its name cannot be expressed as a Syncthing ignore; kept here")
                    continue
                set_ignores(stc, fid, add=[line])
                if os.path.lexists(path):
                    if not inside(path, local_root):
                        raise Unsafe(f"refused to remove {path}: not inside {local_root}")
                    shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)
                rec["trimmed"] = True
                trimmed += 1
            # gone from the seedbox: forget it (and let a new item of the same name sync again)
            for key in [k for k in copied if k.startswith(sub + "/") and k[len(sub) + 1:] not in on_seedbox]:
                name = key[len(sub) + 1:]
                if os.path.lexists(os.path.join(local_root, name)):
                    continue
                if copied[key].get("trimmed") and ignore_line(name):
                    set_ignores(stc, fid, drop=[ignore_line(name)])
                del copied[key]
            for key in [k for k in seen if k.startswith(sub + "/") and k[len(sub) + 1:] not in names]:
                del seen[key]
            for key in [k for k in st.get("rt_first", {}) if k.startswith(sub + "/") and k[len(sub) + 1:] not in names]:
                del st["rt_first"][key]
        pruned = prune(now, [sub for _, sub, _ in srcs], copied)
        if not up:
            raise RuntimeError("the seedbox's Syncthing is not connected")
        if st.get("fails", 0) >= FAILS_BEFORE_ALERT:
            alert("Bookstack: seedbox connected again", "Finished downloads are arriving again.", "default", tags="white_check_mark")
        st["fails"], st["last_ok"], st["unsafe_alerted"] = 0, int(now), False
        save_state(st)
        for n in notes:
            print("seedbox: " + n)
        print(f"seedbox: {handed} item(s) handed to Shelfmark, {trimmed} old synced item(s) dropped, {pruned} unclaimed hand-over(s) cleared")
        return 0
    except Unsafe as e:
        st["last_error"] = f"REFUSED: {e}"[:400]
        if not st.get("unsafe_alerted"):
            alert("Bookstack: seedbox sync REFUSED (safety)",
                  f"{e}\n\nNothing is handed over until this is fixed (Library -> Seedbox).", seq="seedbox-safety")
            st["unsafe_alerted"] = True
        save_state(st)
        print(f"seedbox: REFUSED ({e})", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError, ET.ParseError, http.client.HTTPException) as e:
        st["fails"] = st.get("fails", 0) + 1
        st["last_error"] = f"{type(e).__name__}: {e}"[:300]
        if st["fails"] == FAILS_BEFORE_ALERT:
            alert("Bookstack: seedbox downloads are not arriving",
                  f"For about {FAILS_BEFORE_ALERT // 3} minutes in a row: {st['last_error']}\n\n"
                  "Downloads keep finishing on the seedbox and arrive once it connects again. Check the "
                  "seedbox's Syncthing, or Library -> Seedbox -> Check the connection.")
        save_state(st)
        print(f"seedbox: FAILED ({st['last_error']})", file=sys.stderr)
        return 1


def setup():
    """Library -> Seedbox: make this server's Syncthing match seedbox.env. The only place that
    writes Syncthing's config. Prints this server's device ID last."""
    c = envfile(CONF)
    env = envfile(os.path.join(STACK, ".env"))
    dev, srcs = c.get("SEEDBOX_ST_DEVICE", ""), sources(c)
    if not DEVICE_RE.match(dev):
        raise RuntimeError(f"'{dev}' is not a Syncthing device ID")
    stc = Syncthing(env.get("SYNCTHING_API_KEY"), ST_SETUP)
    me = stc.req("GET", "/rest/system/status")["myID"]
    stc.req("PATCH", "/rest/config/options", body={
        "localAnnounceEnabled": False, "urAccepted": -1, "crashReportingEnabled": False,
        "autoUpgradeIntervalH": 0, "startBrowser": False})
    addr = [c["SEEDBOX_ST_ADDRESS"]] if c.get("SEEDBOX_ST_ADDRESS") else ["dynamic"]
    have = {d["deviceID"] for d in stc.req("GET", "/rest/config/devices") or []}
    d = {"name": "seedbox", "addresses": addr, "autoAcceptFolders": False, "introducer": False, "paused": False}
    if dev in have:
        stc.req("PATCH", "/rest/config/devices/" + dev, body=d)
    else:
        stc.req("POST", "/rest/config/devices", body=dict(d, deviceID=dev))
    wanted = {fid for fid, _, _ in srcs}
    for f in stc.req("GET", "/rest/config/folders") or []:
        if f["id"] not in wanted:                   # Syncthing's "Default Folder", a dropped source
            stc.req("DELETE", "/rest/config/folders/" + f["id"])
    for fid, sub, kind in srcs:
        label = ("Bookstack SABnzbd " + sub.split("/", 1)[-1] if kind == "sab" else "Bookstack rTorrent") + " (set Send Only)"
        spec = {"label": label, "path": ST_SYNC + "/" + sub, "type": "receiveonly",
                "devices": [{"deviceID": me}, {"deviceID": dev}], "ignorePerms": True,
                "versioning": {"type": ""}, "paused": False}
        if stc.folder(fid) is None:
            stc.req("POST", "/rest/config/folders", body=dict(spec, id=fid))
        else:                                       # type first, THEN unpause (in one PATCH)
            stc.req("PATCH", "/rest/config/folders/" + fid, body=spec)
        if kind == "sab":
            set_ignores(stc, fid, add=SAB_IGNORES)
    for id_ in have - {me, dev}:                    # this Syncthing talks to the seedbox and nothing else
        stc.req("DELETE", "/rest/config/devices/" + id_)
    guard(stc, c, srcs, act=False)
    gui = stc.req("GET", "/rest/config/gui") or {}
    if c.get("SEEDBOX_ST_GUI_PASS") and (gui.get("user") != "bookstack" or not gui.get("password")):
        try:                                        # Syncthing restarts its API to apply this
            stc.req("PATCH", "/rest/config/gui", body={"user": "bookstack", "password": c["SEEDBOX_ST_GUI_PASS"]})
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException):
            pass
        for _ in range(30):
            try:
                stc.req("GET", "/rest/system/status")
                break
            except (urllib.error.URLError, ConnectionError, http.client.HTTPException):
                time.sleep(1)
    print(me)
    return 0


def check():
    """Library -> Seedbox -> Check. One line per finding; exit 1 on a real failure. A seedbox that
    has not connected or accepted the folders yet is 'waiting', not a failure."""
    c = envfile(CONF)
    env = envfile(os.path.join(STACK, ".env"))
    srcs, dev, bad = sources(c), c.get("SEEDBOX_ST_DEVICE", ""), 0
    rt_only = sys.argv[1:] == ["--check-rtorrent"]
    if not rt_only:
        try:
            stc = Syncthing(env.get("SYNCTHING_API_KEY"))
            me = stc.req("GET", "/rest/system/status")["myID"]
            print(f"ok: this server's Syncthing is running (its device ID: {me})")
            guard(stc, c, srcs, act=False)
            print("ok: every folder here is Receive Only (nothing made here can reach the seedbox)")
            if connected(stc, dev):
                print("ok: the seedbox's Syncthing is connected")
                for fid, sub, _ in srcs:
                    s = stc.req("GET", "/rest/db/completion", {"folder": fid, "device": dev}) or {}
                    if s.get("remoteState") == "valid":
                        n = len(stc.req("GET", "/rest/db/browse", {"folder": fid, "levels": 0}) or [])
                        print(f"ok: {fid}: shared by the seedbox ({n} item(s) there now)")
                    else:
                        print(f"waiting: {fid}: not accepted on the seedbox yet (its Syncthing shows it as a new folder to add; set it Send Only)")
            else:
                print(f"waiting: the seedbox's Syncthing has not connected yet: add this server there as a remote device ({me})")
        except Unsafe as e:
            print(f"FAIL: {e}")
            bad += 1
        except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError) as e:
            print(f"FAIL: this server's Syncthing: {getattr(e, 'code', '')} {e}")
            bad += 1
    if c.get("SEEDBOX_PROWLARR_URL"):            # Shelfmark fetches .torrent / .nzb files from here
        url = c["SEEDBOX_PROWLARR_URL"].rstrip("/") + "/ping"
        h = {"User-Agent": "bookstack-seedbox/2"}
        if c.get("SEEDBOX_RT_USER"):
            raw = f'{c["SEEDBOX_RT_USER"]}:{c.get("SEEDBOX_RT_PASS", "")}'.encode()
            h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30).read()
            print("ok: the seedbox login opens Prowlarr (Shelfmark can fetch .torrent / .nzb files)")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            why = {401: "the seedbox login was refused"}.get(getattr(e, "code", None), f"{type(e).__name__}: {e}")
            print(f"FAIL: Prowlarr at {c['SEEDBOX_PROWLARR_URL']}: {why}")
            bad += 1
    if any(kind == "rt" for _, _, kind in srcs):
        if not c.get("SEEDBOX_RT_URL"):
            print("warn: no rTorrent address: torrents are not handed over (they could be unfinished)")
        else:
            try:
                done = rtorrent_complete(c)
                print(f"ok: rTorrent answers ({len(done)} finished torrent(s) in {c.get('SEEDBOX_RT_DIR')})")
            except (urllib.error.URLError, OSError, RuntimeError, ET.ParseError) as e:
                why = {401: "the username/password was refused"}.get(getattr(e, "code", None), f"{type(e).__name__}: {e}")
                print(f"FAIL: rTorrent XML-RPC: {why}")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args in (["--check"], ["--check-rtorrent"]):
        sys.exit(check())
    if args == ["--device-id"]:                     # Library -> Seedbox -> Show what to set up
        try:
            print(Syncthing(envfile(os.path.join(STACK, ".env")).get("SYNCTHING_API_KEY")).req("GET", "/rest/system/status")["myID"])
            sys.exit(0)
        except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError) as e:
            print(f"FAIL: {e}", file=sys.stderr)
            sys.exit(1)
    if args == ["--setup"]:
        try:
            sys.exit(setup())
        except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError, Unsafe) as e:
            print(f"FAIL: {getattr(e, 'code', '')} {e}", file=sys.stderr)
            sys.exit(1)
    os.makedirs(MIRROR, exist_ok=True)
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    lock = open(STATE + ".lock", "w")
    try:                                            # a copy across filesystems can outlast a minute
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("seedbox: the previous run is still busy; skipping this minute")
        sys.exit(0)
    sys.exit(run())
