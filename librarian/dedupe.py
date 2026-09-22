"""Read-only check: does a title already exist in the part of the Calibre library this user
can see? Advisory only (the 'in library' badge on search results)."""
import sqlite3
import config, library

def exists(title, owner=None, is_admin=False):
    if not (config.DEDUPE_WARN and title):
        return False
    scope, params = library._scope_sql(owner, is_admin)
    try:
        c = sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=5)
        n = c.execute(f"SELECT 1 FROM books b WHERE lower(b.title)=lower(?) {scope} LIMIT 1", (title, *params)).fetchone()
        c.close()
        return bool(n)
    except Exception:
        return False
