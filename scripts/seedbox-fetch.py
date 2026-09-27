#!/usr/bin/env python3
"""seedbox-fetch.py — bring finished downloads back from the seedbox (Library -> Seedbox).

Shelfmark sends a reader's pick to the seedbox's SABnzbd (Usenet) or rTorrent (torrents), which
download it on the SEEDBOX's disk. This job, every minute on the host, COPIES each finished item
to $STACK_DIR/library/seedbox/, which Shelfmark sees as /seedbox; Shelfmark's remote path mapping
then finds it there and files it into that reader's dropbox, and the portal tags and imports it.

COPY ONLY. The seedbox belongs to private trackers with strict seeding rules: nothing here may
move, delete, rename or change anything on it. Enforced three ways, each on its own enough:
  1. the Filebrowser account must be DOWNLOAD-ONLY: its permissions ride in the login token, and
     this refuses to run (and alerts) if it could create, rename, modify, delete, share, execute
     or administer anything. Filebrowser itself then refuses any write (measured: DELETE -> 403);
  2. every request goes through one allowlist: POST /api/login, GET /api/resources/..., GET
     /api/raw/... Anything else raises before a byte is sent. There is no delete code at all;
  3. rTorrent is asked one fixed, read-only question (d.multicall2 of d.name / d.complete /
     d.directory) and nothing else.
Shelfmark's own clean-up is pinned in docker-compose.yml (torrents: keep; Usenet: copy).

What is fetched, and when:
  * sab (SABnzbd): a job folder appears in completed/<category> only once SABnzbd is done with it
    (it unpacks in _UNPACK_* and renames at the end); fetched once every file is 2 minutes old.
  * rt (rTorrent): rTorrent downloads IN PLACE, so only names rTorrent itself reports complete
    (XML-RPC d.complete) are fetched. They keep seeding untouched.
Each item is remembered by its file list (names and sizes) and never fetched twice; if it changes
on the seedbox it is fetched again. An item appears under library/seedbox/ only whole: it is
downloaded into .incoming/, every file's size checked, re-listed on the seedbox, then renamed into
place. Standard library only, runs as root.

Config: /etc/bookstack/seedbox.env (0600; Library -> Seedbox writes it). State and the failure
latch: /etc/bookstack/seedbox.state (JSON).
"""
import base64, datetime, fcntl, hashlib, json, os, re, shutil, subprocess, sys, time, uuid
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

STACK = os.environ.get("STACK_DIR", "/srv/bookstack")
CONF = os.environ.get("SEEDBOX_ENV", "/etc/bookstack/seedbox.env")
STATE = os.environ.get("SEEDBOX_STATE", "/etc/bookstack/seedbox.state")
MIRROR = os.environ.get("SEEDBOX_MIRROR", os.path.join(STACK, "library/seedbox"))
ALERT = os.environ.get("SEEDBOX_ALERT", os.path.join(STACK, "scripts/alert.sh"))
MIN_AGE = int(os.environ.get("SEEDBOX_MIN_AGE", "120"))            # seconds a SABnzbd job must be settled
KEEP_DAYS = int(os.environ.get("SEEDBOX_KEEP_DAYS", "7"))           # unclaimed items on the VPS
FREE_MARGIN = int(os.environ.get("SEEDBOX_FREE_MARGIN_GB", "5")) * 2**30
FAILS_BEFORE_ALERT = 15                                             # a quarter of an hour of minutes
SKIP_PREFIX = ("_UNPACK_", "_FAILED_", "_ADMIN_", ".")
# guardrail 1: permissions a Filebrowser account must NOT have
WRITE_PERMS = ("admin", "create", "rename", "modify", "delete", "share", "execute")
# guardrail 2: the only requests this job may make to the seedbox (method, path prefix)
ALLOWED = (("POST", "/api/login"), ("GET", "/api/resources/"), ("GET", "/api/raw/"))
# guardrail 3: the one, read-only rTorrent question
RT_COMMANDS = ("d.name=", "d.complete=", "d.directory=")


class Unsafe(RuntimeError):
    """A guardrail refused: nothing is fetched until it is fixed."""


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


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def alert(title, body, prio="high"):
    try:
        subprocess.run([ALERT, title, body, prio], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def parse_time(s):
    """Filebrowser: 2026-09-27T17:12:31.780402342Z (nanoseconds, Z or an offset)."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", s or "").replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


class Seedbox:
    def __init__(self, c):
        self.base = c["SEEDBOX_FB_URL"].rstrip("/")
        self.basic = None
        if c.get("SEEDBOX_BASIC_USER"):
            raw = f'{c["SEEDBOX_BASIC_USER"]}:{c.get("SEEDBOX_BASIC_PASS", "")}'.encode()
            self.basic = "Basic " + base64.b64encode(raw).decode()
        self.user, self.pw = c["SEEDBOX_FB_USER"], c.get("SEEDBOX_FB_PASS", "")
        self.token = None

    def _req(self, method, path, data=None, raw=False, timeout=60, stream_to=None):
        if not any(method == m and (path == p if m == "POST" else path.startswith(p)) for m, p in ALLOWED):
            raise Unsafe(f"refused to send {method} {path}: this job only reads from the seedbox")
        h = {"User-Agent": "bookstack-seedbox/1"}
        if self.basic:
            h["Authorization"] = self.basic
        if self.token:
            h["X-Auth"] = self.token
        if data is not None:
            h["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        url = self.base + urllib.parse.quote(path, safe="/")
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)
        if stream_to:
            n = 0
            with open(stream_to, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk); n += len(chunk)
            return n
        body = r.read()
        return body if raw else (json.loads(body) if body else None)

    def login(self):
        self.token = None
        self.token = self._req("POST", "/api/login", {"username": self.user, "password": self.pw, "recaptcha": ""},
                               raw=True).decode().strip()
        if not self.token.startswith("ey"):
            raise RuntimeError("Filebrowser login answered without a token")
        self.perm = token_perms(self.token)
        risky = sorted(k for k in WRITE_PERMS if self.perm.get(k))
        if risky or not self.perm.get("download"):
            raise Unsafe("the Filebrowser account '%s' %s. Use a DOWNLOAD-ONLY account (Filebrowser -> Settings -> "
                         "User Management: only 'Download' ticked) so nothing on the seedbox can ever be changed"
                         % (self.user, ("can " + ", ".join(risky)) if risky else "cannot download"))

    def listdir(self, path):
        return (self._req("GET", "/api/resources" + path.rstrip("/") + "/") or {}).get("items") or []

    def walk(self, item):
        """[(relative path, size, mtime)] for every file under an item (the item itself when a
        file), sorted, so two walks compare equal only when nothing changed in between."""
        if not item.get("isDir"):
            return [(item["name"], int(item.get("size") or 0), parse_time(item.get("modified")))]
        out, todo = [], [(item["path"], item["name"])]
        while todo:
            path, rel = todo.pop()
            for it in self.listdir(path):
                r = rel + "/" + it["name"]
                if it.get("isSymlink"):
                    continue
                if it.get("isDir"):
                    todo.append((it["path"], r))
                else:
                    out.append((r, int(it.get("size") or 0), parse_time(it.get("modified"))))
        return sorted(out)

    def download(self, remote_file, local):
        return self._req("GET", "/api/raw" + remote_file, stream_to=local, timeout=300)


def token_perms(token):
    """The account's permissions, from the JWT Filebrowser signs (its payload's user.perm)."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return dict((payload.get("user") or {}).get("perm") or {})
    except (IndexError, ValueError):
        raise RuntimeError("could not read the Filebrowser account's permissions from its login token")


def signature(files):
    """Names and sizes: equal only for the same content on the seedbox (mtimes can be touched)."""
    return hashlib.sha256(json.dumps([(r, sz) for r, sz, _ in files]).encode()).hexdigest()[:24]


def _scalar(v):
    kids = list(v)
    return (kids[0].text if kids else v.text) or ""


def rtorrent_complete(c):
    """Names of the torrents rTorrent reports COMPLETE whose folder is under the watched one."""
    url = c.get("SEEDBOX_RT_URL", "")
    params = "".join(f"<param><value><string>{x}</string></value></param>" for x in ("", "main") + RT_COMMANDS)
    body = f'<?xml version="1.0"?><methodCall><methodName>d.multicall2</methodName><params>{params}</params></methodCall>'.encode()
    h = {"Content-Type": "text/xml", "User-Agent": "bookstack-seedbox/1"}
    user = c.get("SEEDBOX_RT_USER") or c.get("SEEDBOX_BASIC_USER")
    if user:
        pw = c.get("SEEDBOX_RT_PASS") or c.get("SEEDBOX_BASIC_PASS", "")
        h["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    xml = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=60).read()
    root = ET.fromstring(xml)
    if root.find("fault") is not None:
        raise RuntimeError("rTorrent XML-RPC fault")
    base = c.get("SEEDBOX_RT_DIR", "").rstrip("/")
    done = set()
    outer = root.find("./params/param/value/array/data")
    for v in (outer.findall("value") if outer is not None else []):
        row = v.find("array/data")
        vals = [_scalar(x) for x in row.findall("value")] if row is not None else []
        if len(vals) < 3:
            continue
        name, complete, directory = vals[0], vals[1].strip(), vals[2].rstrip("/")
        if complete != "1":
            continue
        # a multi-file torrent's d.directory is <base>/<name>, a single file's is <base>
        if directory in (base, base + "/" + name):
            done.add(name)
    return done


def sources(c):
    """[(fb path, local subdir, kind)] from SEEDBOX_SOURCES: 'fb path|local subdir|sab or rt' per ';'."""
    out = []
    for part in (c.get("SEEDBOX_SOURCES") or "").split(";"):
        bits = [b.strip() for b in part.split("|")]
        if len(bits) == 3 and bits[0].startswith("/") and bits[2] in ("sab", "rt") \
                and bits[1] and ".." not in bits[1].split("/") and not bits[1].startswith("/"):
            out.append(tuple(bits))
    return out


def chown_tree(path, uid, gid):
    for root, dirs, files in os.walk(path):
        for n in [root] + [os.path.join(root, x) for x in dirs + files]:
            try:
                os.chown(n, uid, gid)
            except OSError:
                pass


def fetch_item(sb, item, dest_dir, uid, gid, files):
    """Download one item (file or folder, already walked) whole into dest_dir; bytes, or raises."""
    total = sum(sz for _, sz, _ in files)
    os.makedirs(dest_dir, exist_ok=True)
    if shutil.disk_usage(dest_dir).free < total + FREE_MARGIN:
        raise OSError(f"not enough free space on the VPS for {item['name']} ({total // 2**20} MiB)")
    stage = os.path.join(MIRROR, ".incoming", uuid.uuid4().hex)
    os.makedirs(stage)
    try:
        for rel, size, _ in files:
            local = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            remote = item["path"] if not item.get("isDir") else item["path"].rstrip("/") + "/" + rel.split("/", 1)[1]
            got = sb.download(remote, local)
            if got != size or os.path.getsize(local) != size:
                raise OSError(f"{rel}: got {got} bytes, the seedbox lists {size}")
        final = os.path.join(dest_dir, item["name"])
        if os.path.exists(final):
            shutil.rmtree(final) if os.path.isdir(final) else os.unlink(final)
        chown_tree(stage, uid, gid)
        os.replace(os.path.join(stage, item["name"]), final)      # whole, in one step
        os.utime(final, None)                                      # the week of grace starts now
        return total
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def prune(now, subs):
    """Items Shelfmark never claimed (the reader cancelled, a mapping was wrong) go after a week;
    a staging leftover from a run that died mid-download goes after a day."""
    gone = 0
    for sub in subs:
        d = os.path.join(MIRROR, sub)
        for n in (os.listdir(d) if os.path.isdir(d) else []):
            p = os.path.join(d, n)
            if os.path.getmtime(p) < now - KEEP_DAYS * 86400:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.unlink(p)
                gone += 1
    inc = os.path.join(MIRROR, ".incoming")
    for n in (os.listdir(inc) if os.path.isdir(inc) else []):
        if os.path.getmtime(os.path.join(inc, n)) < now - 86400:
            shutil.rmtree(os.path.join(inc, n), ignore_errors=True)
    return gone


def run():
    c = envfile(CONF)
    if not c.get("SEEDBOX_FB_URL"):
        print("seedbox: not configured (Library -> Seedbox)")
        return 0
    env = envfile(os.path.join(STACK, ".env"))
    uid, gid = int(env.get("PUID") or 1000), int(env.get("PGID") or 1000)
    st = load_state()
    copied = st.setdefault("copied", {})
    now = time.time()
    fetched, notes = 0, []
    try:
        sb = Seedbox(c)
        sb.login()
        rt_done = None
        srcs = sources(c)
        for fb_path, sub, kind in srcs:
            dest = os.path.join(MIRROR, sub)
            os.makedirs(dest, exist_ok=True)
            chown_tree(dest, uid, gid)
            if kind == "rt":
                if not c.get("SEEDBOX_RT_URL"):
                    notes.append(f"{fb_path}: no rTorrent address, so torrents are not fetched (they could be unfinished)")
                    continue
                if rt_done is None:
                    rt_done = rtorrent_complete(c)
            present = set()
            for item in sb.listdir(fb_path):
                name = item["name"]
                if name.startswith(SKIP_PREFIX) or item.get("isSymlink"):
                    continue
                present.add(name)
                key = sub + "/" + name
                if kind == "rt" and name not in rt_done:
                    continue                        # still downloading: never copied half-done
                files = sb.walk(item)
                if not files:
                    continue                        # an empty folder: nothing to hand over yet
                sig = signature(files)
                if (copied.get(key) or {}).get("sig") == sig:
                    continue                        # already here once: never twice
                if kind == "sab" and now - max(m for _, _, m in files) < MIN_AGE:
                    continue                        # SABnzbd may still be moving files into it
                size = fetch_item(sb, item, dest, uid, gid, files)
                fetched += 1
                if signature(sb.walk(item)) == sig:
                    copied[key] = {"sig": sig, "at": int(now), "bytes": size}
                    print(f"seedbox: copied {key} ({size // 2**20} MiB)")
                else:                               # it changed while copying: again next minute
                    notes.append(f"{key} changed on the seedbox while it was copied; copying it again")
            for key in [k for k in copied if k.startswith(sub + "/") and k[len(sub) + 1:] not in present]:
                del copied[key]                     # gone from the seedbox: forget it
        pruned = prune(now, [sub for _, sub, _ in srcs])
        if st.get("fails", 0) >= FAILS_BEFORE_ALERT:
            alert("Bookstack: seedbox reachable again", "Finished downloads are being fetched again.", "default")
        st["fails"], st["last_ok"], st["unsafe_alerted"] = 0, int(now), False
        save_state(st)
        for n in notes:
            print("seedbox: " + n)
        print(f"seedbox: {fetched} item(s) fetched, {pruned} unclaimed item(s) cleared")
        return 0
    except Unsafe as e:
        st["last_error"] = f"REFUSED: {e}"[:400]
        if not st.get("unsafe_alerted"):
            alert("Bookstack: seedbox fetching REFUSED (safety)", f"{e}\n\nNothing is copied until this is fixed (Library -> Seedbox).")
            st["unsafe_alerted"] = True
        save_state(st)
        print(f"seedbox: REFUSED ({e})", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, RuntimeError, ValueError, ET.ParseError) as e:
        st["fails"] = st.get("fails", 0) + 1
        st["last_error"] = f"{type(e).__name__}: {e}"[:300]
        if st["fails"] == FAILS_BEFORE_ALERT:
            alert("Bookstack: seedbox downloads are not arriving",
                  f"The seedbox could not be reached for {FAILS_BEFORE_ALERT} minutes in a row: {st['last_error']}\n\n"
                  "Downloads keep finishing on the seedbox and will be fetched once it answers. Check the seedbox, "
                  "or Library -> Seedbox if its password changed.")
        save_state(st)
        print(f"seedbox: FAILED ({st['last_error']})", file=sys.stderr)
        return 1


def check():
    """Library -> Seedbox calls this before it saves anything: can we log in, see every folder,
    and (for torrents) ask rTorrent what is finished? One line per finding, exit 1 on any failure."""
    c = envfile(CONF)
    bad = 0
    try:
        sb = Seedbox(c)
        sb.login()
        print("ok: Filebrowser login, and the account is download-only (it cannot change anything)")
    except Unsafe as e:
        print(f"FAIL: {e}")
        return 1
    except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError) as e:
        code = getattr(e, "code", None)
        why = {401: "the seedbox's own login (user/password in front of Filebrowser) was refused",
               403: "Filebrowser refused its username/password"}.get(code, f"{type(e).__name__}: {e}")
        print(f"FAIL: Filebrowser login: {why}")
        return 1
    for fb_path, sub, kind in sources(c):
        try:
            n = len(sb.listdir(fb_path))
            print(f"ok: {fb_path} ({n} item(s) there now)")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"FAIL: {fb_path}: {getattr(e, 'code', '')} {e}; create the folder on the seedbox, or check the path as Filebrowser shows it")
            bad += 1
        if kind == "rt":
            if not c.get("SEEDBOX_RT_URL"):
                print("warn: no rTorrent address: torrents will not be fetched")
                continue
            try:
                done = rtorrent_complete(c)
                print(f"ok: rTorrent answers ({len(done)} finished torrent(s) in {c.get('SEEDBOX_RT_DIR')})")
            except (urllib.error.URLError, OSError, RuntimeError, ET.ParseError) as e:
                print(f"FAIL: rTorrent XML-RPC: {getattr(e, 'code', '')} {e}")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(check())
    os.makedirs(MIRROR, exist_ok=True)
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    lock = open(STATE + ".lock", "w")
    try:                                            # a big audiobook can take longer than a minute
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("seedbox: the previous run is still fetching; skipping this minute")
        sys.exit(0)
    sys.exit(run())
