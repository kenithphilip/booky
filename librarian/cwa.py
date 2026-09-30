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
import sqlite3, os, sys, json, argparse, re, functools, datetime, uuid
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
# v6.2.1: Calibre-Web archives a book deleted on a Kobo ONLY for readers who may see Archived Books
# (cps/kobo.py HandleBookDeletionRequest: check_visibility(SIDEBAR_ARCHIVED)); without it the next
# sync sends the book straight back. Every reader gets it, so deleting on the Kobo sticks for all of
# them as it did for admins, and 'Put it back on my Kobo' has something to undo.
SIDEBAR_ARCHIVED = 1 << 15
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
def list_users(include_canary=False):
    """Real accounts only: CWA's built-in anonymous 'Guest' row (ROLE_ANONYMOUS) is not a
    login and anonymous browsing is switched off by harden(), so it is left out. The canary
    accounts of the synthetic journey (config.CANARY_USERS) are left out too unless asked for."""
    with _conn() as c:
        rows = c.execute("SELECT id,name,email,role,kindle_mail,allowed_tags FROM user ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if (d["role"] or 0) & ROLE_ANONYMOUS:
            continue
        if not include_canary and d["name"] in config.CANARY_USERS:
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
        sidebar = ADMIN_SIDEBAR if admin else int(_setting(c, "config_default_show", USER_SIDEBAR)) | SIDEBAR_ARCHIVED
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
        c.execute("UPDATE user SET allowed_tags=?, sidebar_view=coalesce(sidebar_view, 0) | ? WHERE id=?",
                  (owner_tag(u["name"]), SIDEBAR_ARCHIVED, u["id"]))
    _checkpoint()
    return True

@_guard
def grant_archive_view():
    """v6.2.1 (Deploy's harden): every reader may see Archived Books (see SIDEBAR_ARCHIVED). Returns
    how many accounts changed. Calibre-Web reads it per request: no restart."""
    with _conn() as c:
        n = c.execute("UPDATE user SET sidebar_view=coalesce(sidebar_view, 0) | ? WHERE (role & ?)=0 "
                      "AND (coalesce(sidebar_view, 0) & ?)=0", (SIDEBAR_ARCHIVED, ROLE_ANONYMOUS, SIDEBAR_ARCHIVED)).rowcount
    if n:
        _checkpoint()
    return n

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

READ_STATUS = {0: "unread", 1: "read", 2: "reading"}      # CWA's ReadBook.STATUS_* (cps/ub.py)

def reading_state(name):
    """{book_id: {"status": "read" | "reading", "pct": float | None}} for this reader, from what
    Calibre-Web itself records: its read status (the Kobo's 'Finished' / 'Reading', KOReader's
    sync, the web reader's own mark) and the Kobo's page position. Books never opened are absent.
    Tables CWA has not created yet read as nothing, and a busy or unreadable app.db as nothing too:
    reading status is a nicety, never a reason for a page to fail."""
    try:
        return _reading_state(name)
    except (sqlite3.Error, CwaError):
        return {}

def _reading_state(name):
    u = get_user(name)
    if not u:
        return {}
    out = {}
    with _conn() as c:
        try:
            for bid, st in c.execute("SELECT book_id, read_status FROM book_read_link WHERE user_id=?", (u["id"],)):
                if st in (1, 2):
                    out[bid] = {"status": READ_STATUS[st], "pct": None}
        except sqlite3.OperationalError:
            pass
        try:
            for bid, pct in c.execute(
                    "SELECT s.book_id, b.progress_percent FROM kobo_reading_state s "
                    "JOIN kobo_bookmark b ON b.kobo_reading_state_id = s.id WHERE s.user_id=?", (u["id"],)):
                if pct is None:
                    continue
                cur = out.setdefault(bid, {"status": "reading", "pct": None})
                cur["pct"] = round(float(pct))
                if cur["status"] != "read" and float(pct) > 0:
                    cur["status"] = "reading"
        except sqlite3.OperationalError:
            pass
    return out

READ_CODE = {v: k for k, v in READ_STATUS.items()}

@_guard
def set_read_status(name, book_id, status):
    """v5.9: a reader's own Read / Reading / Unread for one book, for what no device reports (a
    Kindle, Panels or Chunky on an iPad). Written where Calibre-Web keeps its own (book_read_link,
    the row its "Mark as read" writes), so its web reader, the portal and AniList/Metron all see
    one answer.

    v5.9.1, so the Kobo gets it too: Calibre-Web's Kobo sync sends a book's state only when its
    kobo_reading_state.last_modified is newer than the device's sync token (cps/kobo.py
    HandleSyncRequest, CWA v4.0.7), and its own "Mark as read" gets that bump from an ORM hook
    (ub.py before_flush) that raw SQL skips. So the same transaction bumps last_modified and
    priority_timestamp, or creates the state with an empty bookmark and statistics exactly as
    helper.edit_book_read_status does. The Kobo's own position (kobo_bookmark, kobo_statistics
    values) is never written. Times are naive UTC with microseconds, as SQLAlchemy stores them:
    the sync compares them as strings."""
    if status not in READ_CODE:
        raise CwaError("unknown reading status")
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    bid, uid, code = int(book_id), u["id"], READ_CODE[status]
    with _conn() as c:
        try:
            row = c.execute("SELECT id, read_status FROM book_read_link WHERE user_id=? AND book_id=?", (uid, bid)).fetchone()
        except sqlite3.OperationalError as e:
            raise CwaError("Calibre-Web has not created its reading table yet (open a book in it once)") from e
        started = status == "reading" and (not row or row["read_status"] != 2)
        if row:
            c.execute("UPDATE book_read_link SET read_status=?, last_modified=?" +
                      (", last_time_started_reading=?, times_started_reading=coalesce(times_started_reading,0)+1" if started else "") +
                      " WHERE id=?", (code, now, now, row["id"]) if started else (code, now, row["id"]))
        else:
            c.execute("INSERT INTO book_read_link(book_id, user_id, read_status, last_modified, last_time_started_reading, "
                      "times_started_reading) VALUES(?,?,?,?,?,?)", (bid, uid, code, now, now if started else None, 1 if started else 0))
        try:
            st = c.execute("SELECT id FROM kobo_reading_state WHERE user_id=? AND book_id=?", (uid, bid)).fetchone()
            if st:
                c.execute("UPDATE kobo_reading_state SET last_modified=?, priority_timestamp=? WHERE id=?", (now, now, st["id"]))
            else:
                sid = c.execute("INSERT INTO kobo_reading_state(user_id, book_id, last_modified, priority_timestamp) "
                                "VALUES(?,?,?,?)", (uid, bid, now, now)).lastrowid
                c.execute("INSERT INTO kobo_bookmark(kobo_reading_state_id, last_modified) VALUES(?,?)", (sid, now))
                c.execute("INSERT INTO kobo_statistics(kobo_reading_state_id, last_modified) VALUES(?,?)", (sid, now))
        except sqlite3.OperationalError:
            pass                                 # no Kobo tables yet (nobody has synced a Kobo): nothing to tell one
        c.commit()
    return status

@_guard
def kobo_remove(name, book_id, even_unsent=False):
    """v6.1: take a book off this reader's Kobo at its next sync, as Calibre-Web itself does (CWA
    v4.0.8 cps/web.py toggle_archived, kobo.py HandleSyncRequest). Returns how, or None when the
    book never went to their Kobo:
      'archive' (the Kobo syncs every book): archived for this reader and dropped from the Kobo's
                synced list, so the next sync sends it again with IsRemoved and the Kobo deletes
                it. That sync only includes a book the reader can still SEE: their owner tag must
                stay until it has happened (the portal waits for it).
      'shelf'   (the Kobo syncs only chosen shelves): off this reader's Kobo shelves; CWA's
                two-way sync then sends the removal for a synced book no shelf holds any more,
                whether or not the reader can still see it.
    even_unsent (v6.3: 'Take it off my Kobo', finished books): a book not sent yet is archived too,
    so it never is."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    uid, bid = u["id"], int(book_id)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    with _conn() as c:
        try:
            synced = c.execute("SELECT 1 FROM kobo_synced_books WHERE user_id=? AND book_id=?", (uid, bid)).fetchone()
        except sqlite3.OperationalError:
            return None                          # no Kobo has ever synced here
        if not synced and not even_unsent:
            return None
        shelves_only = (c.execute("SELECT kobo_only_shelves_sync FROM user WHERE id=?", (uid,)).fetchone() or [0])[0]
        if shelves_only:
            c.execute("DELETE FROM book_shelf_link WHERE book_id=? AND shelf IN "
                      "(SELECT id FROM shelf WHERE user_id=? AND kobo_sync=1)", (bid, uid))
            how = "shelf"
        else:
            row = c.execute("SELECT id FROM archived_book WHERE user_id=? AND book_id=?", (uid, bid)).fetchone()
            if row:
                c.execute("UPDATE archived_book SET is_archived=1, last_modified=? WHERE id=?", (now, row["id"]))
            else:
                c.execute("INSERT INTO archived_book(user_id, book_id, is_archived, last_modified) VALUES(?,?,1,?)",
                          (uid, bid, now))
            c.execute("DELETE FROM kobo_synced_books WHERE user_id=? AND book_id=?", (uid, bid))
            how = "archive"
        c.commit()
    _checkpoint()
    return how

@_guard
def kobo_removed(name, book_id, how):
    """Has the Kobo had the removal kobo_remove() arranged? 'archive': its sync put the book back
    on the synced list (CWA records every book it sends, the removal too); 'shelf': the sync
    dropped it from that list."""
    u = get_user(name)
    if not u:
        return True                              # the account is gone: nothing left to wait for
    with _conn() as c:
        try:
            on = c.execute("SELECT 1 FROM kobo_synced_books WHERE user_id=? AND book_id=?",
                           (u["id"], int(book_id))).fetchone() is not None
        except sqlite3.OperationalError:
            return True
    return on if how == "archive" else not on

@_guard
def kobo_unarchive(name, book_id):
    """v6.1: a book given to a reader (again) is not 'archived' for them any more, or their Kobo
    would be told to delete it the moment it arrives. v6.2.1: and it comes OFF the reader's synced
    list, exactly as Calibre-Web's own Unarchive does (cps/web.py toggle_archived): a Kobo is only
    ever sent books that are not on that list, and the sync that told it 'removed' put the book
    back on it, so clearing the mark alone never reached the Kobo (the Peanuts, 2026-09-30)."""
    u = get_user(name)
    if not u:
        return False
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    with _conn() as c:
        try:
            n = c.execute("UPDATE archived_book SET is_archived=0, last_modified=? WHERE user_id=? AND book_id=? "
                          "AND is_archived=1", (now, u["id"], int(book_id))).rowcount
            if n:
                c.execute("DELETE FROM kobo_synced_books WHERE user_id=? AND book_id=?", (u["id"], int(book_id)))
        except sqlite3.OperationalError:
            return False
        c.commit()
    if n:
        _checkpoint()
    return bool(n)

# v6.2.1: where a book stands for a reader's Kobo, from Calibre-Web's own records (the one place the
# portal asks, so no page or action guesses). Calibre-Web sends a Kobo only books NOT on the reader's
# synced list; a book the reader deleted on the Kobo is archived for them (cps/kobo.py
# HandleBookDeletionRequest), and the next sync tells the Kobo 'removed' and lists it as synced again.
KOBO_STATES = {
    "no-kobo": "no Kobo has synced for this reader",
    "coming": "reaches the Kobo at its next sync",
    "on-kobo": "on the Kobo",
    "removing": "the Kobo is told to delete it at its next sync",
    "deleted": "deleted on the Kobo (still in the library)",
    "not-on-shelf": "the Kobo syncs only chosen shelves, and this book is on none of them",
}

@_guard
def kobo_states(name, book_ids):
    """{book id: one of KOBO_STATES} for many books at once (My books, the daily passes)."""
    u = get_user(name)
    ids = [int(b) for b in book_ids]
    if not u:
        return {b: "no-kobo" for b in ids}
    uid = u["id"]
    with _conn() as c:
        try:
            # a Kobo: one has synced, or a sync link exists (every book put back empties the list)
            if not (c.execute("SELECT 1 FROM kobo_synced_books WHERE user_id=? LIMIT 1", (uid,)).fetchone()
                    or c.execute("SELECT 1 FROM archived_book WHERE user_id=? LIMIT 1", (uid,)).fetchone()
                    or c.execute("SELECT 1 FROM remote_auth_token WHERE user_id=? AND token_type=?",
                                 (uid, KOBO_TOKEN_TYPE)).fetchone()):
                return {b: "no-kobo" for b in ids}
            synced = {r[0] for r in c.execute("SELECT book_id FROM kobo_synced_books WHERE user_id=?", (uid,))}
            archived = {r[0] for r in c.execute("SELECT book_id FROM archived_book WHERE user_id=? AND is_archived=1", (uid,))}
            shelves_only = (c.execute("SELECT kobo_only_shelves_sync FROM user WHERE id=?", (uid,)).fetchone() or [0])[0]
            on_shelf = {r[0] for r in c.execute("SELECT l.book_id FROM book_shelf_link l JOIN shelf s ON s.id=l.shelf "
                                                "WHERE s.user_id=? AND s.kobo_sync=1", (uid,))} if shelves_only else set()
        except sqlite3.OperationalError:
            return {b: "no-kobo" for b in ids}
    out = {}
    for b in ids:
        if shelves_only:                         # CWA's two-way sync removes a synced book no shelf holds
            out[b] = ("on-kobo" if b in on_shelf else "removing") if b in synced else \
                     ("coming" if b in on_shelf else "not-on-shelf")
        elif b in archived:
            out[b] = "deleted" if b in synced else "removing"
        else:
            out[b] = "on-kobo" if b in synced else "coming"
    return out

def kobo_state(name, book_id):
    """One of KOBO_STATES for this reader and book. The portal asks ondevice.kobo_state, which first
    answers 'not-theirs' for a book the reader does not have (v6.3)."""
    return kobo_states(name, [book_id])[int(book_id)]

# ---- v6.3: the portal's own Kobo shelf ('only the books I choose', and an admin's own books) -----------
# Calibre-Web's "sync only the shelves I mark for Kobo" (kobo_only_shelves_sync) sends a Kobo only the
# books on the reader's Kobo shelves, and takes a book off the Kobo once no such shelf holds it
# (cps/kobo.py HandleSyncRequest, two-way deletion). The portal keeps ONE shelf for this, so nobody
# has to manage shelves in Calibre-Web; on the Kobo it shows as a collection of this name.
MANAGED_SHELF = "From the library"

def _managed_shelf(c, uid, create=True):
    r = c.execute("SELECT id FROM shelf WHERE user_id=? AND name=?", (uid, MANAGED_SHELF)).fetchone()
    if r or not create:
        return r[0] if r else None
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    return c.execute("INSERT INTO shelf(uuid, name, is_public, user_id, kobo_sync, created, last_modified) "
                     "VALUES(?,?,0,?,1,?,?)", (str(uuid.uuid4()), MANAGED_SHELF, uid, now, now)).lastrowid

@_guard
def kobo_choose_only(name):
    """True when this reader's Kobo gets only books on their Kobo shelves."""
    u = get_user(name)
    if not u:
        return False
    with _conn() as c:
        try:
            return bool((c.execute("SELECT kobo_only_shelves_sync FROM user WHERE id=?", (u["id"],)).fetchone() or [0])[0])
        except sqlite3.OperationalError:
            return False

@_guard
def kobo_synced_ids(name):
    """Books on the reader's Kobo now (synced, not archived)."""
    u = get_user(name)
    if not u:
        return set()
    with _conn() as c:
        try:
            synced = {r[0] for r in c.execute("SELECT book_id FROM kobo_synced_books WHERE user_id=?", (u["id"],))}
        except sqlite3.OperationalError:
            return set()
        try:
            archived = {r[0] for r in c.execute("SELECT book_id FROM archived_book WHERE user_id=? AND is_archived=1", (u["id"],))}
        except sqlite3.OperationalError:
            archived = set()
    return synced - archived

@_guard
def kobo_archived_ids(name):
    """Books archived for this reader (deleted on their Kobo, or taken off by the portal)."""
    u = get_user(name)
    if not u:
        return set()
    with _conn() as c:
        try:
            return {r[0] for r in c.execute("SELECT book_id FROM archived_book WHERE user_id=? AND is_archived=1", (u["id"],))}
        except sqlite3.OperationalError:
            return set()

@_guard
def kobo_shelf_ids(name):
    """Books on the portal's Kobo shelf."""
    u = get_user(name)
    if not u:
        return set()
    with _conn() as c:
        sid = _managed_shelf(c, u["id"], create=False)
        return {r[0] for r in c.execute("SELECT book_id FROM book_shelf_link WHERE shelf=?", (sid,))} if sid else set()

@_guard
def kobo_shelf_add(name, book_ids, refresh=False):
    """Onto the portal's Kobo shelf (it goes to the Kobo at the next sync), not archived any more
    (in shelf mode Calibre-Web sends an archived book as removed). refresh: a book already on it is
    dated now, so the next sync sends it again (in shelf mode Calibre-Web sends shelf entries newer
    than the Kobo's last sync: one deleted on the Kobo is otherwise never sent again). Returns how
    many were added."""
    u = get_user(name)
    if not u or not book_ids:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    n = 0
    with _conn() as c:
        sid = _managed_shelf(c, u["id"])
        have = {r[0] for r in c.execute("SELECT book_id FROM book_shelf_link WHERE shelf=?", (sid,))}
        if refresh:
            c.execute(f"UPDATE book_shelf_link SET date_added=? WHERE shelf=? AND book_id IN ({','.join('?' * len(book_ids))})",
                      (now, sid, *[int(x) for x in book_ids]))
        for b in {int(x) for x in book_ids} - have:
            c.execute('INSERT INTO book_shelf_link(book_id, "order", shelf, date_added) VALUES(?,?,?,?)', (b, 0, sid, now))
            n += 1
        c.execute(f"UPDATE archived_book SET is_archived=0, last_modified=? WHERE user_id=? AND is_archived=1 "
                  f"AND book_id IN ({','.join('?' * len(book_ids))})", (now, u["id"], *[int(x) for x in book_ids]))
        c.execute("UPDATE shelf SET last_modified=? WHERE id=?", (now, sid))
        c.commit()
    _checkpoint()
    return n

@_guard
def kobo_shelf_remove(name, book_ids):
    """Off every Kobo shelf of the reader: the next sync takes it off the Kobo (two-way sync)."""
    u = get_user(name)
    if not u or not book_ids:
        return 0
    with _conn() as c:
        n = c.execute(f"DELETE FROM book_shelf_link WHERE book_id IN ({','.join('?' * len(book_ids))}) AND shelf IN "
                      f"(SELECT id FROM shelf WHERE user_id=? AND kobo_sync=1)", (*[int(x) for x in book_ids], u["id"])).rowcount
        c.commit()
    _checkpoint()
    return n

@_guard
def kobo_set_choose_only(name, on, keep=()):
    """Switch 'only the books I choose' on (the books in `keep` go onto the portal's Kobo shelf FIRST,
    so the next sync does not take them off the Kobo) or off (every book the reader can see is sent)."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    if on:
        if keep:
            kobo_shelf_add(name, list(keep))
        with _conn() as c:
            try:
                _managed_shelf(c, u["id"])
            except sqlite3.OperationalError:
                pass                             # no shelf table yet: Calibre-Web makes it; the switch still holds
            c.execute("UPDATE user SET kobo_only_shelves_sync=1 WHERE id=?", (u["id"],))
            c.commit()
    else:
        with _conn() as c:
            c.execute("UPDATE user SET kobo_only_shelves_sync=0 WHERE id=?", (u["id"],))
            c.commit()
    _checkpoint()

def kobo_put_back(name, book_id):
    """'Put it back on my Kobo': what Calibre-Web's Unarchive does. True when something changed."""
    return kobo_unarchive(name, book_id)

@_guard
def kobo_archived():
    """[(user name, book id)] of every book archived for a reader's Kobo (the daily cross-check)."""
    with _conn() as c:
        try:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT u.name, a.book_id FROM archived_book a JOIN user u ON u.id=a.user_id WHERE a.is_archived=1")]
        except sqlite3.OperationalError:
            return []

@_guard
def kobo_status(name):
    """What CWA itself records about this reader's Kobo (L12), read-only: how many books it has
    handed to the device (kobo_synced_books) and when a reading position last arrived
    (kobo_reading_state). Tables CWA has not created yet read as 'never'."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    out = {"books_on_device": 0, "last_reading": None,
           "shelves_only": bool(u.get("kobo_only_shelves_sync")) if "kobo_only_shelves_sync" in u else False,
           "hardcover": False}
    with _conn() as c:
        try:
            out["books_on_device"] = c.execute("SELECT COUNT(*) FROM kobo_synced_books WHERE user_id=?", (u["id"],)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        try:
            r = c.execute("SELECT MAX(last_modified) FROM kobo_reading_state WHERE user_id=?", (u["id"],)).fetchone()
            out["last_reading"] = r[0] if r else None
        except sqlite3.OperationalError:
            pass
        try:
            r = c.execute("SELECT kobo_only_shelves_sync, hardcover_token FROM user WHERE id=?", (u["id"],)).fetchone()
            out["shelves_only"], out["hardcover"] = bool(r[0]), bool(r[1])
        except sqlite3.OperationalError:
            pass
    return out

@_guard
def hardcover_tokens():
    """{user name: Hardcover token} for every reader who set one (v5.9.1: audiobooks, hcaudio.py)."""
    with _conn() as c:
        try:
            return {r["name"]: r["hardcover_token"] for r in c.execute(
                "SELECT name, hardcover_token FROM user WHERE hardcover_token IS NOT NULL AND hardcover_token != ''")}
        except sqlite3.OperationalError:
            return {}

@_guard
def set_kobo_prefs(name, shelves_only=None, hardcover_token=None):
    """The two per-reader Kobo options CWA keeps on the user row. A blank Hardcover token clears
    it (the column is UNIQUE, so '' would collide between readers: NULL it is)."""
    u = get_user(name)
    if not u:
        raise CwaError(f"no such user '{name}'")
    with _conn() as c:
        if shelves_only is not None:
            c.execute("UPDATE user SET kobo_only_shelves_sync=? WHERE id=?", (1 if shelves_only else 0, u["id"]))
        if hardcover_token is not None:
            tok = hardcover_token.strip().replace("Bearer ", "") or None
            c.execute("UPDATE user SET hardcover_token=? WHERE id=?", (tok, u["id"]))
    _checkpoint()

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
    """Turn on CWA's global Kobo sync (the 'Enable Kobo sync' checkbox), store proxy off. With it
    (L12): shelves-only sync ("magic shelves") available to readers who choose it on Devices, and
    Hardcover progress sync — which does nothing for a reader who has not set their own token."""
    return _apply_settings({"config_kobo_sync": 1, "config_kobo_proxy": 0,
                            "config_kobo_sync_magic_shelves": 1, "config_hardcover_sync": 1})

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
    pinned rather than assumed; this one now is too.

    L05: with the Authelia gate on it is pinned ON instead — to exactly Remote-User, the header
    Caddy strips on every path and sets only after Authelia said yes, with auto-creation off
    (see proxy_login_settings)."""
    return _apply_settings({"config_public_reg": 0, "config_anonbrowse": 0,
                            "config_remote_login": 0, **proxy_login_settings()})

def proxy_login_settings(on=None):
    """Calibre-Web's reverse-proxy header login (L05: one login behind the gate). ON only while
    the gate is on and has its secret: Caddy then strips Remote-User on EVERY path (the device
    paths that bypass Authelia included) and copies it from Authelia's answer, and no bridge
    network can reach Calibre-Web (docker-compose.yml, L01). Never auto-creates accounts."""
    if on is None:
        on = bool(config.AUTHELIA_ENABLED and config.GATE_SECRET)
    s = {"config_allow_reverse_proxy_header_login": 1 if on else 0, "config_reverse_proxy_auto_create_users": 0}
    if on:
        s["config_reverse_proxy_login_header_name"] = "Remote-User"
    return s

def set_proxy_login(on):
    return _apply_settings(proxy_login_settings(on))

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
    a.add_argument("--no-abs", action="store_true", help="no Audiobookshelf account (the canary accounts)")
    sp.add_parser("list").add_argument("--all", action="store_true", help="include the canary accounts")
    k = sp.add_parser("kindle"); k.add_argument("name"); k.add_argument("address")
    u = sp.add_parser("kobo-url"); u.add_argument("name"); u.add_argument("--reset", action="store_true")
    w = sp.add_parser("passwd"); w.add_argument("name"); _password_args(w)
    r = sp.add_parser("remove-user"); r.add_argument("name")
    i = sp.add_parser("isolate"); i.add_argument("name")
    n = sp.add_parser("rename-user"); n.add_argument("old"); n.add_argument("new")
    sp.add_parser("enable-kobo-sync"); sp.add_parser("harden")
    sp.add_parser("proxy-login").add_argument("state", choices=("on", "off"))
    args = p.parse_args(argv)
    try:
        if args.cmd == "add-user":
            pw = read_password(args)
            u = add_user(args.name, pw, args.email, args.admin)
            _audit("user_add", u["name"], "admin" if args.admin else "user")
            out = {"ok": True, "user": u["name"], "id": u["id"], "kobo_url": kobo_url(u["name"])}
            # admins see every audiobook through ABS's own admin role; only end users get an
            # ABS account here, matching what /admin and the TUI create
            if not (u["role"] or 0) & ROLE_ADMIN and not args.no_abs:
                a = _abs_sync("create", u["name"], pw)
                if a:
                    out["abs"] = a
            print(json.dumps(out))
        elif args.cmd == "list":
            print(json.dumps(list_users(include_canary=args.all), indent=1))
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
        elif args.cmd == "proxy-login":
            ch = set_proxy_login(args.state == "on")
            _audit("cwa_proxy_login", "-", args.state)
            print(json.dumps({"ok": True, "changed": ch, "restart_cwa": ch}))
        elif args.cmd == "harden":
            ch = disable_public_registration() | enable_kobo_sync()
            print(json.dumps({"ok": True, "changed": ch, "restart_cwa": ch, "archive_view": grant_archive_view()}))
        return 0
    except CwaError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(_cli())
