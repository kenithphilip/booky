"""The ONE place that writes to Calibre-Web Automated's app.db.

Everything else in the portal treats app.db as read-only (auth.py). This module handles the
few, well-understood writes needed to make onboarding and device setup self-service:
  - create / list / password-reset / remove users, with the per-user isolation tag baked in
  - set a user's Send-to-Kindle address (user.kindle_mail)
  - create / read a user's Kobo sync token (remote_auth_token, exactly as CWA's own
    "Generate Kobo Auth URL" button does) and turn on Kobo sync globally
All statements are short SQLite transactions; CWA reads users per request, so changes are
live immediately. Also usable as a CLI (bookstack.sh menu "Users" calls it):
    python -m cwa add-user alice --email a@x --password '...' [--admin]
    python -m cwa list | kindle alice a@kindle.com | kobo-url alice | passwd alice --password ..
    python -m cwa remove-user alice | enable-kobo-sync | rename-user admin kenith-admin
    (--password-stdin instead of --password reads the secret from stdin; the installer uses it)
"""
import sqlite3, os, sys, json, argparse, re, functools
from binascii import hexlify
from werkzeug.security import generate_password_hash
import config

# Calibre-Web role bits (cps/constants.py)
ROLE_ADMIN, ROLE_DOWNLOAD, ROLE_UPLOAD, ROLE_EDIT, ROLE_PASSWD = 1, 2, 4, 8, 16
ROLE_ANONYMOUS, ROLE_EDIT_SHELFS, ROLE_DELETE_BOOKS, ROLE_VIEWER = 32, 64, 128, 256
END_USER_ROLES = ROLE_DOWNLOAD | ROLE_VIEWER | ROLE_PASSWD | ROLE_EDIT_SHELFS      # 338
ADMIN_ROLES    = (ROLE_ADMIN | ROLE_DOWNLOAD | ROLE_UPLOAD | ROLE_EDIT | ROLE_PASSWD
                  | ROLE_EDIT_SHELFS | ROLE_DELETE_BOOKS | ROLE_VIEWER)              # 479
KOBO_TOKEN_TYPE = 1
DATETIME_MAX = "9999-12-31 23:59:59.999999"   # what SQLAlchemy writes for datetime.max
USER_SIDEBAR = 1
ADMIN_SIDEBAR = 524287   # constants.ADMIN_USER_SIDEBAR (all sidebar items)

class CwaError(Exception):
    pass

class CwaUnavailable(CwaError):
    """app.db exists but could not be read or written right now — SQLITE_BUSY after the 30 s
    timeout, 'unable to open database file' when the WAL sidecars cannot be created, 'file is
    not a database' mid-checkpoint.

    A SUBCLASS of CwaError on purpose: _conn() only ever raised for a missing file, so every
    guard in the portal is `except cwa.CwaError` and every other sqlite3 error escaped this
    module raw. worker._owner_gone then read a transient lock as 'that account is gone' and
    _process wrote a permanent `error` row plus a 'could not be added' mail for a request that
    only needed retrying, while app._cwa_user turned it into a bare 500 on /library, /upload,
    /devices and /admin — with /login still working, because auth._row is the one place that
    catches bare Exception. Being a CwaError means those guards now fail open, as their own
    docstrings already promised; the distinct class is there for a caller that wants to tell
    'no such user' from 'ask again in a moment'.

    The portal manufactures this contention itself: _checkpoint() runs a TRUNCATE checkpoint
    after every user/kindle/kobo write and checkpoint_passive() every 300 s."""

def _guard(fn):
    """Translate sqlite3 errors into CwaUnavailable. On every public entry point that touches
    app.db, so no caller has to know this module uses sqlite. sqlite3.IntegrityError is caught
    closer to the statement where it means something specific (a duplicate e-mail)."""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except sqlite3.Error as e:
            raise CwaUnavailable(f"the library database is busy or unreadable ({e}); "
                                 f"try again in a moment") from e
    return wrapped

def kindle_fixer_on():
    """CWA's own settings DB (cwa.db) sits beside app.db. True when its Kindle EPUB fixer runs
    on import. That fixer rewrites every zip it is handed, not just EPUBs, and drops the
    archive comment, which is the only place Calibre reads comic (CBZ) metadata from, so the
    owner tag the portal embeds is lost. Unknown/unreadable counts as off (the shipped default)."""
    p = os.path.join(os.path.dirname(config.CWA_DB), "cwa.db")
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            r = c.execute("SELECT kindle_epub_fixer FROM cwa_settings LIMIT 1").fetchone()
        finally:
            c.close()
        return bool(r and r[0])
    except sqlite3.Error:
        return False

def _conn():
    if not os.path.exists(config.CWA_DB):
        raise CwaError(f"CWA database not found at {config.CWA_DB}")
    c = sqlite3.connect(config.CWA_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def _checkpoint():
    """Fold the WAL into app.db right after we write. CWA keeps app.db in WAL mode and
    Shelfmark reads it with `immutable=1`, which ignores the WAL entirely — without this a
    user created here would be invisible to Shelfmark until CWA's next checkpoint."""
    try:
        c = sqlite3.connect(config.CWA_DB, timeout=30)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except sqlite3.Error:
        pass

def checkpoint_passive():
    """Periodic PASSIVE checkpoint (worker housekeeping): folds changes CWA's own UI wrote
    (a password changed at books.<domain>/me) into app.db so Shelfmark, which opens it with
    immutable=1 and ignores the WAL, sees them within minutes. Never blocks CWA's writers."""
    try:
        c = sqlite3.connect(config.CWA_DB, timeout=5)
        try:
            return c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None

def _hash(pw):
    # pbkdf2 is understood by every Werkzeug version CWA has shipped with.
    return generate_password_hash(pw, method="pbkdf2:sha256")

# Shape only, deliberately case-insensitive: existing CWA accounts may carry a capital, and the
# dropbox watcher resolves a folder named 'Alice' to the user 'alice'. Lowercase is required
# when an account is CREATED — that is _check_name's job, just below.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

def _valid_name(name):
    """The one shape rule, for every caller. A leading dot (or a bare '..') used to pass here,
    and the resulting account looked complete while its dropbox was never scanned: the watcher
    skips dot-directories, so everything the reader dropped or uploaded vanished silently."""
    return bool(name) and bool(_NAME_RE.match(name))

NAME_RULE = ("username: lowercase letters, digits, dot, dash, underscore only; no spaces "
             "(Shelfmark matches names case-sensitively, so a capital letter locks that account "
             "out of it)")

def _check_name(name):
    """Accepted name for a NEW account. Lowercase is enforced, not just suggested: an
    'adm_Kim' worked everywhere except Shelfmark, which answered 401."""
    if not _valid_name(name) or name != name.lower():
        raise CwaError(NAME_RULE)
    return name

def owner_tag(name):
    return f"{config.OWNER_PREFIX}{name}"

# ---- users -----------------------------------------------------------------------
@_guard
def get_user(name):
    with _conn() as c:
        r = c.execute("SELECT id,name,email,role,kindle_mail,allowed_tags FROM user "
                      "WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return dict(r) if r else None

@_guard
def list_users():
    """Real accounts only: CWA's built-in anonymous 'Guest' row (ROLE_ANONYMOUS) is not a
    login and anonymous browsing is switched off by harden(), so it is left out."""
    with _conn() as c:
        rows = c.execute("SELECT id,name,email,role,kindle_mail,allowed_tags FROM user ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if (d["role"] or 0) & ROLE_ANONYMOUS:
            continue
        d["is_admin"] = bool((d["role"] or 0) & ROLE_ADMIN)
        d["isolated"] = (d.get("allowed_tags") or "") == owner_tag(d["name"])
        out.append(d)
    return out

def _columns(c, table):
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]

def _setting(c, name, default):
    try:
        if name in _columns(c, "settings"):
            r = c.execute(f"SELECT {name} FROM settings LIMIT 1").fetchone()
            if r and r[0] is not None:
                return r[0]
    except sqlite3.Error:
        pass
    return default

@_guard
def add_user(name, password, email="", admin=False):
    """Create a CWA user. Non-admins get Allowed Tags = owner:<name> (the isolation model).
    Column set adapts to the CWA version at hand: every known column gets the same value
    CWA's own 'Add user' form would write; unknown columns are left to their defaults."""
    _check_name(name)
    if len(password or "") < 8:
        raise CwaError("password must be at least 8 characters")
    if get_user(name):
        raise CwaError(f"user '{name}' already exists")
    with _conn() as c:
        # CWA keeps user.email UNIQUE. A family plausibly shares one address (a parent's for a
        # child's account), and the raw IntegrityError came out as a Flask 500 on /admin and a
        # traceback in the TUI's whiptail box. Same check set_email() already makes.
        if email and c.execute("SELECT 1 FROM user WHERE email=? COLLATE NOCASE", (email,)).fetchone():
            raise CwaError("that e-mail address is already used by another account")
        have = set(_columns(c, "user"))
        sidebar = ADMIN_SIDEBAR if admin else int(_setting(c, "config_default_show", USER_SIDEBAR))
        values = {
            "name": name, "email": email or f"{name}@{config.DOMAIN or 'localhost'}",
            "role": ADMIN_ROLES if admin else END_USER_ROLES, "password": _hash(password),
            "kindle_mail": "", "kindle_mail_subject": "", "locale": "en", "sidebar_view": sidebar,
            "default_language": "all", "denied_tags": "", "allowed_tags": "" if admin else owner_tag(name),
            "denied_column_value": "", "allowed_column_value": "", "view_settings": "{}",
            "kobo_only_shelves_sync": 0, "theme": 1, "auto_send_enabled": 0,
            "allow_additional_ereader_emails": 1,
        }
        cols = [k for k in values if k in have]
        try:
            c.execute(f"INSERT INTO user({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                      [values[k] for k in cols])
        except sqlite3.IntegrityError as e:   # any future UNIQUE column degrades to a flash, not a 500
            raise CwaError(str(e))
    _checkpoint()
    return get_user(name)

@_guard
def set_password(name, password):
    if len(password or "") < 8:
        raise CwaError("password must be at least 8 characters")
    with _conn() as c:
        n = c.execute("UPDATE user SET password=? WHERE name=? COLLATE NOCASE", (_hash(password), name)).rowcount
    if not n:
        raise CwaError(f"no such user '{name}'")
    _checkpoint()

@_guard
def rename_user(old, new):
    """Rename an ADMIN account (the installer lets the admin choose a name other than 'admin').
    An isolated user is refused: their books carry owner:<old>, so a rename would hide them."""
    if not _valid_name(new) or new != new.lower():
        raise CwaError("new " + NAME_RULE)
    u = get_user(old)
    if not u:
        raise CwaError(f"no such user '{old}'")
    other = get_user(new)
    if other and other["id"] != u["id"]:
        raise CwaError(f"user '{new}' already exists")
    if not (u["role"] or 0) & ROLE_ADMIN:
        raise CwaError(f"'{u['name']}' is not an admin: renaming an isolated user would hide their "
                       f"books (tagged {owner_tag(u['name'])}); create a new user instead")
    with _conn() as c:
        c.execute("UPDATE user SET name=? WHERE id=?", (new, u["id"]))
    # The portal's own rows are keyed on the NAME, not on CWA's user id: without this the
    # renamed admin loses their request history, preferences and audit trail. Imported late so
    # the CLI keeps working against a machine where only app.db is reachable.
    moved = {}
    try:
        import db as _db
        moved = _db.rename_owner(u["name"], new)
    except Exception as e:                       # never leave the CWA account half-renamed
        moved = {"error": str(e)}
    _checkpoint()
    return {"old": u["name"], "new": new, "id": u["id"], "portal_rows": moved}

@_guard
def set_email(name, address):
    """The user's own e-mail (notifications, and the sender allowed for mail-to-library).
    CWA keeps it UNIQUE, so an address another account uses is refused."""
    address = (address or "").strip()
    if not address or "@" not in address or " " in address or len(address) > 120:
        raise CwaError("that does not look like an e-mail address")
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    with _conn() as c:
        taken = c.execute("SELECT 1 FROM user WHERE email=? COLLATE NOCASE AND id!=?", (address, u["id"])).fetchone()
        if taken:
            raise CwaError("that e-mail address is already used by another account")
        c.execute("UPDATE user SET email=? WHERE id=?", (address, u["id"]))
    _checkpoint()
    return address

@_guard
def remove_user(name):
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    if u["role"] & ROLE_ADMIN:
        with _conn() as c:
            admins = c.execute("SELECT COUNT(*) FROM user WHERE role & 1").fetchone()[0]
        if admins <= 1:
            raise CwaError("refusing to remove the last admin")
    with _conn() as c:
        c.execute("DELETE FROM remote_auth_token WHERE user_id=?", (u["id"],))
        c.execute("DELETE FROM user WHERE id=?", (u["id"],))
    _checkpoint()

@_guard
def ensure_isolation(name):
    """Re-apply Allowed Tags = owner:<name> for a non-admin (idempotent)."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    if u["role"] & ROLE_ADMIN:
        return False
    with _conn() as c:
        c.execute("UPDATE user SET allowed_tags=? WHERE id=?", (owner_tag(u["name"]), u["id"]))
    _checkpoint()
    return True

# ---- devices ---------------------------------------------------------------------
@_guard
def set_kindle_mail(name, address):
    address = (address or "").strip()
    if address and ("@" not in address or " " in address):
        raise CwaError("that does not look like an e-mail address")
    with _conn() as c:
        n = c.execute("UPDATE user SET kindle_mail=? WHERE name=? COLLATE NOCASE", (address, name)).rowcount
    if not n:
        raise CwaError(f"no such user '{name}'")
    _checkpoint()
    return address

@_guard
def kobo_token(name, create=True):
    """Return the user's Kobo sync token (create one like CWA's own button if missing)."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    with _conn() as c:
        r = c.execute("SELECT auth_token FROM remote_auth_token WHERE user_id=? AND token_type=?",
                      (u["id"], KOBO_TOKEN_TYPE)).fetchone()
        if r:
            return r["auth_token"]
        if not create:
            return None
        tok = hexlify(os.urandom(16)).decode()
        c.execute("INSERT INTO remote_auth_token(auth_token,user_id,verified,expiration,token_type) "
                  "VALUES(?,?,?,?,?)", (tok, u["id"], 0, DATETIME_MAX, KOBO_TOKEN_TYPE))
    _checkpoint()
    return tok

@_guard
def kobo_url(name, create=True):
    tok = kobo_token(name, create)
    return f"{config.BOOKS_URL}/kobo/{tok}" if tok and config.BOOKS_URL else None

@_guard
def reset_kobo_token(name):
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    with _conn() as c:
        c.execute("DELETE FROM remote_auth_token WHERE user_id=? AND token_type=?", (u["id"], KOBO_TOKEN_TYPE))
    return kobo_token(name, True)

@_guard
def kobo_sync_enabled():
    with _conn() as c:
        r = c.execute("SELECT config_kobo_sync FROM settings LIMIT 1").fetchone()
    return bool(r and r["config_kobo_sync"])

@_guard
def _apply_settings(wanted):
    """UPDATE settings SET k=v for the columns that exist. Returns True if anything changed.
    NOTE: CWA loads `settings` into memory at start-up, so a change here takes effect after
    the calibre-web container restarts (bookstack.sh does that when this returns changed)."""
    with _conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(settings)")}
        wanted = {k: v for k, v in wanted.items() if k in cols}
        if not wanted:
            return False
        cur = c.execute("SELECT " + ", ".join(wanted) + " FROM settings LIMIT 1").fetchone()
        if cur and all((cur[k] or 0) == v for k, v in wanted.items()):
            return False
        c.execute("UPDATE settings SET " + ", ".join(f"{k}=?" for k in wanted), list(wanted.values()))
    _checkpoint()
    return True

def enable_kobo_sync():
    """Turn on CWA's global Kobo sync (the 'Enable Kobo sync' checkbox), store proxy off."""
    return _apply_settings({"config_kobo_sync": 1, "config_kobo_proxy": 0})

def disable_public_registration():
    """Pin every way into CWA that does not go through a password prompt.

    config_allow_reverse_proxy_header_login is off in the shipped image, so this is a pin and
    not a repair — but it is the one flag here whose ON state is a full authentication bypass
    on books.<domain>, and it is one admin click (or one Authelia integration attempt) away.
    With it on, cps/__init__.py:_cwa_ensure_db_session calls load_user_from_reverse_proxy_header
    before ANY blueprint runs and kosync's authenticate_user() short-circuits on it before it
    looks at the Authorization header, so an anonymous request carrying the header is logged in
    as whoever it names, including admin. The header NAME is itself an admin-chosen setting
    (config_reverse_proxy_login_header_name) while caddy/Caddyfile.template strips a FIXED list,
    so Caddy cannot catch a name nobody anticipated. Every other hardening step in this stack is
    pinned rather than assumed; this one now is too."""
    return _apply_settings({"config_public_reg": 0, "config_anonbrowse": 0,
                            "config_remote_login": 0,
                            "config_allow_reverse_proxy_header_login": 0})

# ---- CLI (used by bookstack.sh) ---------------------------------------------------
def _password_args(sub):
    """--password <pw> or --password-stdin (keeps the secret out of argv / `ps` / docker inspect)."""
    g = sub.add_mutually_exclusive_group(required=True)
    g.add_argument("--password")
    g.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

def read_password(args):
    return sys.stdin.read().rstrip("\n") if getattr(args, "password_stdin", False) else args.password

def _audit(event, name, detail=None):
    """The TUI's user lifecycle shows up on /admin's audit trail too (the portal's own actions
    always did). Guarded: the state DB may not be reachable from a one-off CLI call."""
    try:
        import db
        db.init()
        db.audit(event, name, "tui", detail)
    except Exception:
        pass

def _abs_sync(action, name, password=None):
    """Audiobookshelf keeps its OWN credential store, so a user lifecycle change that only
    touches app.db leaves it behind: a removed account kept a working audio login and a reset
    password still opened ABS. The portal's /admin path and the TUI both compensate; this CLI
    (which the module docstring advertises) did not. Non-fatal — the outcome is reported in the
    JSON so the caller can see it failed instead of believing {"ok": true}."""
    import abs as absapi
    if not absapi.configured():
        return None
    done = {"remove": "removed", "passwd": "updated", "create": "created"}[action]
    try:
        if action == "remove":
            absapi.remove_user(name)
        elif action == "passwd":
            absapi.set_password(name, password)
        else:
            absapi.ensure_user(name, password)
        return done
    except Exception as e:
        return f"NOT {done}: {e.__class__.__name__}: {str(e)[:100]}"

def note_password_synced(name):
    """Remember the password hash the portal knows about, so the housekeeping drift check does
    not report a change the portal (or the TUI) made itself."""
    try:
        import db, auth
        fp = auth.fingerprint(name)
        if fp and fp is not auth.UNAVAILABLE:
            db.init()
            db.set_pw_fingerprint(name, fp[0])
    except Exception:
        pass

def _cli(argv=None):
    p = argparse.ArgumentParser(prog="cwa", description="Manage Calibre-Web users/devices for bookstack")
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("add-user"); a.add_argument("name"); a.add_argument("--email", default="")
    _password_args(a); a.add_argument("--admin", action="store_true")
    sp.add_parser("list")
    k = sp.add_parser("kindle"); k.add_argument("name"); k.add_argument("address")
    u = sp.add_parser("kobo-url"); u.add_argument("name"); u.add_argument("--reset", action="store_true")
    w = sp.add_parser("passwd"); w.add_argument("name"); _password_args(w)
    r = sp.add_parser("remove-user"); r.add_argument("name")
    i = sp.add_parser("isolate"); i.add_argument("name")
    n = sp.add_parser("rename-user"); n.add_argument("old"); n.add_argument("new")
    sp.add_parser("enable-kobo-sync"); sp.add_parser("harden")
    args = p.parse_args(argv)
    try:
        if args.cmd == "add-user":
            pw = read_password(args)
            u = add_user(args.name, pw, args.email, args.admin)
            _audit("user_add", u["name"], "admin" if args.admin else "user")
            out = {"ok": True, "user": u["name"], "id": u["id"], "kobo_url": kobo_url(u["name"])}
            # admins see every audiobook through ABS's own admin role; only end users get an
            # ABS account here, matching what /admin and the TUI create
            if not (u["role"] or 0) & ROLE_ADMIN:
                a = _abs_sync("create", u["name"], pw)
                if a:
                    out["abs"] = a
            print(json.dumps(out))
        elif args.cmd == "list":
            print(json.dumps(list_users(), indent=1))
        elif args.cmd == "kindle":
            addr = set_kindle_mail(args.name, args.address)
            _audit("kindle_set", args.name, addr or "(cleared)")
            print(json.dumps({"ok": True, "kindle_mail": addr}))
        elif args.cmd == "kobo-url":
            if args.reset:
                _audit("kobo_reset", args.name)
            print(reset_kobo_token(args.name) and kobo_url(args.name) if args.reset else kobo_url(args.name))
        elif args.cmd == "passwd":
            pw = read_password(args)
            set_password(args.name, pw)
            note_password_synced(args.name)
            _audit("password_reset", args.name)
            out = {"ok": True}
            a = _abs_sync("passwd", args.name, pw)      # else the OLD password still opens ABS
            if a:
                out["abs"] = a
            print(json.dumps(out))
        elif args.cmd == "remove-user":
            remove_user(args.name); _audit("user_remove", args.name)
            out = {"ok": True}
            a = _abs_sync("remove", args.name)          # else the audiobook login survives
            if a:
                out["abs"] = a
            print(json.dumps(out))
        elif args.cmd == "rename-user":
            out = rename_user(args.old, args.new)
            _audit("user_rename", out["new"], f"was {out['old']}")
            print(json.dumps({"ok": True, **out}))
        elif args.cmd == "isolate":
            done = ensure_isolation(args.name)
            _audit("user_isolate", args.name, "re-applied" if done else "admin: nothing to do")
            print(json.dumps({"ok": True, "isolated": done}))
        elif args.cmd == "enable-kobo-sync":
            print(json.dumps({"ok": True, "changed": enable_kobo_sync(), "restart_cwa": True}))
        elif args.cmd == "harden":
            ch = disable_public_registration() | enable_kobo_sync()
            print(json.dumps({"ok": True, "changed": ch, "restart_cwa": ch}))
        return 0
    except CwaError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(_cli())
