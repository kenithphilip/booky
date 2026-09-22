"""Authenticate against Calibre-Web Automated's own user table (read-only),
so the username/password you already hand out is the one that works here."""
import sqlite3, hashlib
from werkzeug.security import check_password_hash
import config

UNAVAILABLE = object()   # app.db could not be read right now (locked, mount missing): not "no such user"

def _row(username):
    """The user's row, None when there is no such account, UNAVAILABLE on a read error."""
    try:
        c = sqlite3.connect(f"file:{config.CWA_DB}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT name, password, role FROM user WHERE name=? COLLATE NOCASE",
                        (username,)).fetchone()
        c.close()
    except Exception:
        return UNAVAILABLE
    return row if row and row["password"] else None

def _fp(row):
    """A short fingerprint of the stored hash: changes when the password does, so a session
    minted before a reset (or by a since-removed account) can be recognised and revoked."""
    return hashlib.sha256(row["password"].encode()).hexdigest()[:16]

def verify(username, password):
    """The account dict on a correct password, None when wrong / no such user, UNAVAILABLE
    when app.db cannot be read right now (a CWA restart): callers must not count that as a
    failed login."""
    row = _row(username)
    if row is UNAVAILABLE:
        return UNAVAILABLE
    if not row:
        return None
    try:
        ok = check_password_hash(row["password"], password)
    except Exception:
        ok = False
    if not ok:
        return None
    return {"name": row["name"], "is_admin": bool((row["role"] or 0) & 1), "fp": _fp(row)}

def fingerprint(username):
    """(fp, is_admin) for an existing account, None when it is gone, UNAVAILABLE when app.db
    cannot be read (callers must not treat that as removal). Read-only."""
    row = _row(username)
    if row is UNAVAILABLE:
        return UNAVAILABLE
    if not row:
        return None
    return _fp(row), bool((row["role"] or 0) & 1)
