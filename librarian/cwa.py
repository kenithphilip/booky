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
    python -m cwa remove-user alice | enable-kobo-sync
    (--password-stdin instead of --password reads the secret from stdin; the installer uses it)
"""
import sqlite3, os, sys, json, argparse
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

def _hash(pw):
    # pbkdf2 is understood by every Werkzeug version CWA has shipped with.
    return generate_password_hash(pw, method="pbkdf2:sha256")

def _valid_name(name):
    return bool(name) and name == name.strip() and all(ch.isalnum() or ch in "._-" for ch in name)

def owner_tag(name):
    return f"{config.OWNER_PREFIX}{name}"

# ---- users -----------------------------------------------------------------------
def get_user(name):
    with _conn() as c:
        r = c.execute("SELECT id,name,email,role,kindle_mail,allowed_tags FROM user "
                      "WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return dict(r) if r else None

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

def add_user(name, password, email="", admin=False):
    """Create a CWA user. Non-admins get Allowed Tags = owner:<name> (the isolation model).
    Column set adapts to the CWA version at hand: every known column gets the same value
    CWA's own 'Add user' form would write; unknown columns are left to their defaults."""
    if not _valid_name(name):
        raise CwaError("username: letters, digits, dot, dash, underscore only; no spaces")
    if len(password or "") < 8:
        raise CwaError("password must be at least 8 characters")
    if get_user(name):
        raise CwaError(f"user '{name}' already exists")
    with _conn() as c:
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
        c.execute(f"INSERT INTO user({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [values[k] for k in cols])
    _checkpoint()
    return get_user(name)

def set_password(name, password):
    if len(password or "") < 8:
        raise CwaError("password must be at least 8 characters")
    with _conn() as c:
        n = c.execute("UPDATE user SET password=? WHERE name=? COLLATE NOCASE", (_hash(password), name)).rowcount
    if not n:
        raise CwaError(f"no such user '{name}'")
    _checkpoint()

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

def kobo_url(name, create=True):
    tok = kobo_token(name, create)
    return f"{config.BOOKS_URL}/kobo/{tok}" if tok and config.BOOKS_URL else None

def reset_kobo_token(name):
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    with _conn() as c:
        c.execute("DELETE FROM remote_auth_token WHERE user_id=? AND token_type=?", (u["id"], KOBO_TOKEN_TYPE))
    return kobo_token(name, True)

def kobo_sync_enabled():
    with _conn() as c:
        r = c.execute("SELECT config_kobo_sync FROM settings LIMIT 1").fetchone()
    return bool(r and r["config_kobo_sync"])

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
    return _apply_settings({"config_public_reg": 0, "config_anonbrowse": 0, "config_remote_login": 0})

# ---- CLI (used by bookstack.sh) ---------------------------------------------------
def _password_args(sub):
    """--password <pw> or --password-stdin (keeps the secret out of argv / `ps` / docker inspect)."""
    g = sub.add_mutually_exclusive_group(required=True)
    g.add_argument("--password")
    g.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

def read_password(args):
    return sys.stdin.read().rstrip("\n") if getattr(args, "password_stdin", False) else args.password

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
    sp.add_parser("enable-kobo-sync"); sp.add_parser("harden")
    args = p.parse_args(argv)
    try:
        if args.cmd == "add-user":
            u = add_user(args.name, read_password(args), args.email, args.admin)
            print(json.dumps({"ok": True, "user": u["name"], "id": u["id"], "kobo_url": kobo_url(u["name"])}))
        elif args.cmd == "list":
            print(json.dumps(list_users(), indent=1))
        elif args.cmd == "kindle":
            print(json.dumps({"ok": True, "kindle_mail": set_kindle_mail(args.name, args.address)}))
        elif args.cmd == "kobo-url":
            print(reset_kobo_token(args.name) and kobo_url(args.name) if args.reset else kobo_url(args.name))
        elif args.cmd == "passwd":
            set_password(args.name, read_password(args)); print(json.dumps({"ok": True}))
        elif args.cmd == "remove-user":
            remove_user(args.name); print(json.dumps({"ok": True}))
        elif args.cmd == "isolate":
            print(json.dumps({"ok": True, "isolated": ensure_isolation(args.name)}))
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
