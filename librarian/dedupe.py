"""Read-only check: does a title already exist in the Calibre library? Advisory only."""
import sqlite3
import config

def exists(title):
    if not (config.DEDUPE_WARN and title):
        return False
    try:
        c = sqlite3.connect(f"file:{config.CALIBRE_DB}?mode=ro", uri=True, timeout=5)
        n = c.execute("SELECT 1 FROM books WHERE lower(title)=lower(?) LIMIT 1", (title,)).fetchone()
        c.close()
        return bool(n)
    except Exception:
        return False
