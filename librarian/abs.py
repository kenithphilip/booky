"""Audiobookshelf (ABS) automation through its REST API, so audiobooks get the same
zero-click, per-user isolation as ebooks:

  - first-run bootstrap: create the root user, mint a permanent API key, create the library
    that points at the shared /audiobooks folder                       (TUI: Library -> Audiobookshelf)
  - accounts: create/align a user with accessAllTags=false + itemTagsSelected=[owner:<user>]
    (ABS's tag restriction is the audiobook equivalent of CWA's Allowed Tags)  (TUI: Users)
  - after the worker places an audiobook: trigger a scan; the worker's persistent tag jobs
    (portal DB, retried by the housekeeping loop) wait for the item and tag it owner:<user>
    so only that user sees it                                           (worker.process_tag_jobs)

Everything needs ABS_TOKEN (an API key). Without it the functions degrade to notes telling the
admin what to do by hand, exactly as before. Also a CLI for bookstack.sh:
    python -m abs init --user root --password ... [--library Audiobooks --path /audiobooks]
    python -m abs ensure-user alice --password ... | passwd alice --password ... | remove-user alice
    python -m abs list-users | scan | tag "<folder name>" alice
    (--password-stdin instead of --password reads the secret from stdin; the installer uses it)
Verified against Audiobookshelf 2.36 (tests/e2e_driver.py section 13)."""
import sys, json, time, argparse
import requests
import config

UA = {"User-Agent": "bookstack-librarian/4.0"}
USER_PERMISSIONS = {"download": True, "update": False, "delete": False, "upload": False,
                    "accessAllLibraries": True, "accessAllTags": False, "accessExplicitContent": True,
                    "createEreader": False}

class AbsError(Exception):
    pass

def configured():
    return bool(config.ABS_TOKEN)

def owner_tag(user):
    return f"{config.OWNER_PREFIX}{user}"

def _req(method, path, token=None, timeout=20, **kw):
    h = dict(UA)
    t = token or config.ABS_TOKEN
    if t:
        h["Authorization"] = f"Bearer {t}"
    return requests.request(method, config.ABS_URL.rstrip("/") + path, headers=h, timeout=timeout, **kw)

def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}

# ---- bootstrap ------------------------------------------------------------------------
def status():
    return _json(_req("GET", "/status"))

def init_root(username, password):
    """Create ABS's root user if the server is still uninitialised. Returns True if it did."""
    if status().get("isInit"):
        return False
    r = _req("POST", "/init", json={"newRoot": {"username": username, "password": password}})
    if r.status_code != 200:
        raise AbsError(f"ABS init failed: {r.status_code} {r.text[:120]}")
    return True

def login(username, password):
    r = _req("POST", "/login", token="", json={"username": username, "password": password})
    if r.status_code != 200:
        raise AbsError(f"ABS login failed for {username}: {r.status_code}")
    u = _json(r).get("user") or {}
    # `token` is the long-lived legacy token; `accessToken` is a short-lived JWT (2.26+)
    return {"id": u.get("id"), "token": u.get("token") or u.get("accessToken"), "user": u}

def create_api_key(session_token, user_id, name="bookstack"):
    """A named, non-expiring API key: what ABS_TOKEN should hold."""
    r = _req("POST", "/api/api-keys", token=session_token,
             json={"name": name, "expiresIn": None, "isActive": True, "userId": user_id})
    if r.status_code != 200:
        raise AbsError(f"could not create an ABS API key: {r.status_code} {r.text[:120]}")
    d = _json(r)
    k = d.get("apiKey", d)
    key = k.get("apiKey") if isinstance(k, dict) else None
    if not key:
        raise AbsError("ABS did not return an API key")
    return key

def ensure_library(name=None, path=None, token=None):
    """Return (library_id, created) for the library whose folder is ABS_LIBRARY_PATH."""
    name, path = name or config.ABS_LIBRARY_NAME, path or config.ABS_LIBRARY_PATH
    r = _req("GET", "/api/libraries", token=token)
    if r.status_code != 200:
        raise AbsError(f"could not list ABS libraries: {r.status_code}")
    for lib in _json(r).get("libraries", []):
        if any(f.get("fullPath") == path for f in lib.get("folders", [])):
            return lib["id"], False
    r = _req("POST", "/api/libraries", token=token,
             json={"name": name, "folders": [{"fullPath": path}], "mediaType": "book", "provider": "audible"})
    if r.status_code != 200:
        raise AbsError(f"could not create the ABS library: {r.status_code} {r.text[:120]}")
    return _json(r)["id"], True

def bootstrap(root_user, root_password, key_name="bookstack"):
    """Everything the TUI needs on first run: root (if needed) -> API key -> library."""
    created_root = init_root(root_user, root_password)
    sess = login(root_user, root_password)
    key = create_api_key(sess["token"], sess["id"], key_name)
    lib_id, created_lib = ensure_library(token=key)
    return {"created_root": created_root, "api_key": key, "library_id": lib_id, "created_library": created_lib}

# ---- users ----------------------------------------------------------------------------------
def list_users(token=None):
    r = _req("GET", "/api/users", token=token)
    if r.status_code != 200:
        raise AbsError(f"could not list ABS users: {r.status_code}")
    return _json(r).get("users", [])

def find_user(name, token=None):
    return next((u for u in list_users(token) if (u.get("username") or "").lower() == name.lower()), None)

def progress(user_id, token=None):
    """[mediaProgress] of one ABS user (admin API, GET /api/users/:id; ABS 2.36.1 UserController.
    findOne): libraryItemId, mediaItemType, duration, progress 0-1, currentTime s, isFinished,
    startedAt / finishedAt / lastUpdate in ms. Podcast episodes are left out."""
    r = _req("GET", f"/api/users/{user_id}", token=token)
    if r.status_code != 200:
        raise AbsError(f"could not read ABS user {user_id}: {r.status_code}")
    return [p for p in (_json(r).get("mediaProgress") or []) if p.get("mediaItemType", "book") == "book" and not p.get("episodeId")]

def item_meta(item_id, token=None):
    """{title, author, asin, isbn, duration} of one library item."""
    r = _req("GET", f"/api/items/{item_id}", token=token, params={"expanded": 1})
    if r.status_code != 200:
        raise AbsError(f"could not read ABS item {item_id}: {r.status_code}")
    m = _json(r).get("media") or {}
    md = m.get("metadata") or {}
    return {"title": md.get("title") or "", "author": md.get("authorName") or "", "asin": md.get("asin") or "",
            "isbn": md.get("isbn") or "", "duration": m.get("duration")}

def ensure_user(name, password=None, token=None):
    """Create the ABS account for a library user, or align an existing one with the
    isolation model (tag-restricted to owner:<name>). Returns (user, 'created'|'updated'|'root')."""
    tag = owner_tag(name)
    u = find_user(name, token)
    body = {"permissions": USER_PERMISSIONS, "itemTagsSelected": [tag], "librariesAccessible": [], "isActive": True}
    if u:
        if u.get("type") in ("root", "admin"):
            return u, "root"
        if password:
            body["password"] = password
        r = _req("PATCH", f"/api/users/{u['id']}", token=token, json=body)
        if r.status_code != 200:
            raise AbsError(f"could not update ABS user {name}: {r.status_code} {r.text[:120]}")
        return (_json(r).get("user") or u), "updated"
    if not password:
        raise AbsError(f"ABS user {name} does not exist and no password was given")
    r = _req("POST", "/api/users", token=token, json={"username": name, "password": password, "type": "user", **body})
    if r.status_code != 200:
        raise AbsError(f"could not create ABS user {name}: {r.status_code} {r.text[:120]}")
    return (_json(r).get("user") or {"username": name}), "created"

def set_password(name, password, token=None):
    u = find_user(name, token)
    if not u:
        raise AbsError(f"no ABS user {name}")
    r = _req("PATCH", f"/api/users/{u['id']}", token=token, json={"password": password})
    if r.status_code != 200:
        raise AbsError(f"could not set ABS password for {name}: {r.status_code}")
    return True

def remove_user(name, token=None):
    u = find_user(name, token)
    if not u:
        return False
    if u.get("type") == "root":
        raise AbsError("refusing to remove the ABS root user")
    r = _req("DELETE", f"/api/users/{u['id']}", token=token)
    if r.status_code != 200:
        raise AbsError(f"could not remove ABS user {name}: {r.status_code}")
    return True

# ---- library items ----------------------------------------------------------------------------
def trigger_scan():
    """Ask ABS to scan every library (kept for the worker; returns a human note)."""
    if not configured():
        return "ABS scan skipped (no token); ABS will pick it up on its schedule"
    try:
        r = _req("GET", "/api/libraries", timeout=15)
        libs = _json(r)
        items = libs.get("libraries", libs) if isinstance(libs, dict) else libs
        for lib in items or []:
            if lib.get("id"):
                _req("POST", f"/api/libraries/{lib['id']}/scan", timeout=15)
        return "ABS scan triggered"
    except Exception as e:
        return f"ABS scan failed ({e}); it will scan on schedule"

def find_items_by_folder(folder, token=None):
    """EVERY item ABS indexed under that folder. A box set (Book One/, Book Two/, ...) or a
    zip holding several books becomes several ABS items under one folder; tagging only the
    first one left the rest invisible to their owner."""
    lib_id, _ = ensure_library(token=token)
    r = _req("GET", f"/api/libraries/{lib_id}/items", token=token, params={"limit": 0})
    if r.status_code != 200:
        return []
    out = []
    for it in _json(r).get("results", []):
        rel = it.get("relPath") or ""
        path = it.get("path") or ""
        if rel == folder or rel.startswith(folder + "/") or path.endswith("/" + folder) \
                or ("/" + folder + "/") in path:
            out.append(it)
    return out

def items_for(owner, is_admin=False, token=None):
    """The audiobooks a reader can see (their owner tag; the admin: all), for the portal's
    download list: [{id, title, author, path, size, is_file}]."""
    lib_id, _ = ensure_library(token=token)
    r = _req("GET", f"/api/libraries/{lib_id}/items", token=token, params={"limit": 0})
    if r.status_code != 200:
        raise AbsError(f"Audiobookshelf answered HTTP {r.status_code} for its items")
    tag, out = owner_tag(owner), []
    for it in _json(r).get("results", []):
        media = it.get("media") or {}
        if not is_admin and tag not in (media.get("tags") or []):
            continue
        md = media.get("metadata") or {}
        out.append({"id": it.get("id"), "title": md.get("title") or it.get("relPath") or "?",
                    "author": md.get("authorName") or "", "path": it.get("path") or "",
                    "size": it.get("size") or media.get("size") or 0, "is_file": bool(it.get("isFile"))})
    return sorted(out, key=lambda x: (x["author"].lower(), x["title"].lower()))

def find_item_by_folder(folder, token=None):
    """The first item under the folder (kept for callers that only need existence)."""
    items = find_items_by_folder(folder, token=token)
    return items[0] if items else None

def tag_item(item_id, tag, token=None):
    r = _req("GET", f"/api/items/{item_id}", token=token)
    tags = list(((_json(r).get("media") or {}).get("tags") or [])) if r.status_code == 200 else []
    if tag in tags:
        return False
    r = _req("PATCH", f"/api/items/{item_id}/media", token=token, json={"tags": sorted(set(tags + [tag]))})
    if r.status_code != 200:
        raise AbsError(f"could not tag ABS item: {r.status_code} {r.text[:120]}")
    return True

def tag_folder(folder, owner, attempts=None, delay=5, sleep=time.sleep):
    """Wait for ABS to index the folder the worker just created, then tag it owner:<owner>.
    Returns a note for the request log."""
    if not configured():
        return f"set tag {owner_tag(owner)} in ABS (no API token)"
    attempts = attempts or config.ABS_TAG_ATTEMPTS
    for i in range(attempts):
        try:
            item = find_item_by_folder(folder)
            if item:
                tag_item(item["id"], owner_tag(owner))
                return f"tagged {owner_tag(owner)} in ABS"
            if i % 6 == 3:            # ABS missed the folder? nudge it again
                trigger_scan()
        except Exception as e:
            last = str(e)[:80]
            if i == attempts - 1:
                return f"ABS tagging failed ({last}); set tag {owner_tag(owner)} in ABS"
        sleep(delay)
    return f"ABS did not index '{folder}' in time; set tag {owner_tag(owner)} in ABS"

# ---- CLI (used by bookstack.sh) -------------------------------------------------------------
# ---- L05: sign in with the family login (Authelia as the OpenID Connect provider) -----------
OIDC_CLIENT_ID = "audiobookshelf"

def oidc_settings(on, domain=None, secret=None):
    """ABS auth settings: local login always stays (the apps' saved logins and this automation use
    it); with `on`, OpenID through Authelia is added and the web login jumps straight to it.
    Readers are matched to their EXISTING account by username (the Authelia login and the ABS
    account share the name by construction), never auto-registered, and no group claim is used,
    so each reader's tag restriction and permissions stay exactly as they are."""
    if not on:
        return {"authActiveAuthMethods": ["local"], "authOpenIDAutoLaunch": False}
    domain = domain or config.DOMAIN
    secret = secret or config.ABS_OIDC_SECRET
    if not domain or not secret:
        raise AbsError("DOMAIN and ABS_OIDC_SECRET are needed to sign in through Authelia")
    auth = f"https://auth.{domain}"
    return {"authActiveAuthMethods": ["local", "openid"],
            "authOpenIDIssuerURL": auth, "authOpenIDAuthorizationURL": f"{auth}/api/oidc/authorization",
            "authOpenIDTokenURL": f"{auth}/api/oidc/token", "authOpenIDUserInfoURL": f"{auth}/api/oidc/userinfo",
            "authOpenIDJwksURL": f"{auth}/jwks.json", "authOpenIDLogoutURL": "",
            "authOpenIDClientID": OIDC_CLIENT_ID, "authOpenIDClientSecret": secret,
            "authOpenIDTokenSigningAlgorithm": "RS256", "authOpenIDButtonText": "Sign in with the family login",
            "authOpenIDAutoLaunch": True, "authOpenIDAutoRegister": False, "authOpenIDMatchExistingBy": "username",
            "authOpenIDMobileRedirectURIs": ["audiobookshelf://oauth"], "authOpenIDGroupClaim": "",
            # ABS leaves this undefined until its own settings page saves it, and then builds the
            # callback as "undefined/auth/openid/callback": the site root, matching Authelia's list
            "authOpenIDSubfolderForRedirectURLs": "",
            "authOpenIDAdvancedPermsClaim": ""}

def set_oidc(on, token=None):
    want = oidc_settings(on)
    r = _req("PATCH", "/api/auth-settings", token=token, json=want)
    if r.status_code != 200:
        raise AbsError(f"Audiobookshelf refused the auth settings (HTTP {r.status_code}: {r.text[:120]})")
    got = _json(_req("GET", "/api/auth-settings", token=token))
    methods = sorted(got.get("authActiveAuthMethods") or [])
    if methods != sorted(want["authActiveAuthMethods"]):
        raise AbsError(f"Audiobookshelf did not switch its sign-in methods (now: {', '.join(methods) or 'none'})")
    return {"methods": methods}

# ---- Audiobookshelf's own backups (database + covers and metadata, never the audio) ----------
# Off in a fresh Audiobookshelf (backupSchedule false). A few nightly copies in
# abs/metadata/backups are a cheap guard against a corrupted absdatabase.sqlite, the one file that
# holds every listener's progress, while restic backups are not set up. It does not protect
# against losing the disk: that is what Install -> Backups is for.
BACKUP_SCHEDULE = "30 2 * * *"     # 02:30: after the 01:00 restic run, before the 03:45 memory tidy
BACKUPS_TO_KEEP = 3
BACKUP_MAX_GB = 1                  # a bigger backup is skipped rather than filling the disk

def set_backups(token=None):
    want = {"backupSchedule": BACKUP_SCHEDULE, "backupsToKeep": BACKUPS_TO_KEEP, "maxBackupSize": BACKUP_MAX_GB}
    r = _req("PATCH", "/api/settings", token=token, json=want)
    if r.status_code != 200:
        raise AbsError(f"Audiobookshelf refused the backup settings (HTTP {r.status_code}: {r.text[:120]})")
    got = (_json(r) or {}).get("serverSettings") or {}
    if any(got.get(k) != v for k, v in want.items()):
        raise AbsError(f"Audiobookshelf did not keep the backup settings (now: {got.get('backupSchedule')!r}, "
                       f"keep {got.get('backupsToKeep')!r})")
    return {k: got[k] for k in want}

def _password_args(sub, required=True):
    """--password <pw> or --password-stdin (keeps the secret out of argv / `ps` / docker inspect)."""
    g = sub.add_mutually_exclusive_group(required=required)
    g.add_argument("--password", default=None)
    g.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

def _password(args):
    return sys.stdin.read().rstrip("\n") if args.password_stdin else args.password

def _cli(argv=None):
    p = argparse.ArgumentParser(prog="abs", description="Audiobookshelf automation for bookstack")
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("init"); a.add_argument("--user", default="root"); _password_args(a)
    a.add_argument("--key-name", default="bookstack")
    e = sp.add_parser("ensure-user"); e.add_argument("name"); _password_args(e, required=False)
    w = sp.add_parser("passwd"); w.add_argument("name"); _password_args(w)
    r = sp.add_parser("remove-user"); r.add_argument("name")
    sp.add_parser("list-users"); sp.add_parser("scan"); sp.add_parser("status")
    t = sp.add_parser("tag"); t.add_argument("folder"); t.add_argument("owner"); t.add_argument("--attempts", type=int, default=1)
    sp.add_parser("oidc").add_argument("state", choices=("on", "off"))
    sp.add_parser("backups")
    args = p.parse_args(argv)
    try:
        if args.cmd == "init":
            out = bootstrap(args.user, _password(args), args.key_name); print(json.dumps({"ok": True, **out}))
        elif args.cmd == "ensure-user":
            u, how = ensure_user(args.name, _password(args)); print(json.dumps({"ok": True, "user": u.get("username"), "result": how}))
        elif args.cmd == "passwd":
            set_password(args.name, _password(args)); print(json.dumps({"ok": True}))
        elif args.cmd == "remove-user":
            print(json.dumps({"ok": True, "removed": remove_user(args.name)}))
        elif args.cmd == "list-users":
            print(json.dumps([{"username": u.get("username"), "type": u.get("type"), "tags": u.get("itemTagsSelected"),
                               "isolated": (u.get("type") == "user" and u.get("itemTagsSelected") == [owner_tag(u.get("username", ""))]
                                            and not (u.get("permissions") or {}).get("accessAllTags", True))} for u in list_users()], indent=1))
        elif args.cmd == "scan":
            print(json.dumps({"ok": True, "note": trigger_scan()}))
        elif args.cmd == "status":
            print(json.dumps(status()))
        elif args.cmd == "tag":
            print(json.dumps({"ok": True, "note": tag_folder(args.folder, args.owner, attempts=args.attempts)}))
        elif args.cmd == "oidc":
            print(json.dumps({"ok": True, **set_oidc(args.state == "on")}))
        elif args.cmd == "backups":
            print(json.dumps({"ok": True, **set_backups()}))
        return 0
    except (AbsError, requests.RequestException) as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(_cli())
