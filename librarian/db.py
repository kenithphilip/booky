"""Local state: the request queue and its status. Separate from CWA's DB."""
import sqlite3, time, threading
import config

_lock = threading.Lock()

def _conn():
    c = sqlite3.connect(config.STATE_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            kind TEXT NOT NULL,            -- ebook | audio
            source TEXT NOT NULL,
            identifier TEXT,
            title TEXT NOT NULL,
            author TEXT,
            download_url TEXT,
            is_torrent INTEGER DEFAULT 0,
            status TEXT NOT NULL,          -- pending|queued|downloading|retrying|importing|done|needs-tag|error|denied|dismissed
            detail TEXT,
            created REAL, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS prefs(
            owner TEXT PRIMARY KEY,
            preferred_format TEXT,         -- epub|kepub|azw3|mobi|pdf
            auto_kindle INTEGER DEFAULT 0, -- email every new ebook to the user's Kindle
            notify_email INTEGER DEFAULT 0,-- e-mail the user when a request completes / is denied
            updated REAL)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(prefs)")}
        if "notify_email" not in cols:
            c.execute("ALTER TABLE prefs ADD COLUMN notify_email INTEGER DEFAULT 0")
        if "last_kindle_test" not in cols:
            c.execute("ALTER TABLE prefs ADD COLUMN last_kindle_test REAL")
        c.execute("""CREATE TABLE IF NOT EXISTS login_attempts(
            key TEXT PRIMARY KEY,          -- 'user|ip' or 'ip'
            fails INTEGER DEFAULT 0, first REAL, locked_until REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, user TEXT, ip TEXT, event TEXT, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")

def get_prefs(owner):
    with _conn() as c:
        row = c.execute("SELECT * FROM prefs WHERE owner=?", (owner,)).fetchone()
    d = dict(row) if row else {}
    fmt = d.get("preferred_format") or config.DEFAULT_FORMAT
    if fmt not in config.FORMATS:               # e.g. a 'kepub' pref stored before that choice was dropped
        fmt = config.DEFAULT_FORMAT
    return {"preferred_format": fmt, "auto_kindle": bool(d.get("auto_kindle")),
            "notify_email": bool(d.get("notify_email")), "last_kindle_test": d.get("last_kindle_test")}

def set_prefs(owner, preferred_format=None, auto_kindle=None, notify_email=None, last_kindle_test=None):
    cur = get_prefs(owner)
    fmt = preferred_format if preferred_format in config.FORMATS else cur["preferred_format"]
    ak = cur["auto_kindle"] if auto_kindle is None else bool(auto_kindle)
    ne = cur["notify_email"] if notify_email is None else bool(notify_email)
    lkt = cur["last_kindle_test"] if last_kindle_test is None else float(last_kindle_test)
    with _lock, _conn() as c:
        c.execute("""INSERT INTO prefs(owner,preferred_format,auto_kindle,notify_email,last_kindle_test,updated) VALUES(?,?,?,?,?,?)
                     ON CONFLICT(owner) DO UPDATE SET preferred_format=excluded.preferred_format,
                     auto_kindle=excluded.auto_kindle, notify_email=excluded.notify_email,
                     last_kindle_test=excluded.last_kindle_test, updated=excluded.updated""",
                  (owner, fmt, 1 if ak else 0, 1 if ne else 0, lkt, time.time()))

# ---- brute-force lockout ------------------------------------------------------------------
def _attempt_row(c, key):
    return c.execute("SELECT fails, first, locked_until FROM login_attempts WHERE key=?", (key,)).fetchone()

def locked_for(user, ip, now=None):
    """Seconds remaining on a lock for this user+ip pair or this ip, else 0."""
    now = now or time.time()
    with _conn() as c:
        worst = 0
        for key in (f"{(user or '').lower()}|{ip}", ip):
            r = _attempt_row(c, key)
            if r and r["locked_until"] and r["locked_until"] > now:
                worst = max(worst, int(r["locked_until"] - now))
    return worst

def record_login_failure(user, ip, now=None):
    """Count a failure; returns lock seconds if this failure triggered a lock, else 0."""
    now = now or time.time()
    locked = 0
    with _lock, _conn() as c:
        for key, limit in ((f"{(user or '').lower()}|{ip}", config.LOCKOUT_FAILS), (ip, config.LOCKOUT_IP_FAILS)):
            r = _attempt_row(c, key)
            if not r or now - (r["first"] or 0) > config.LOCKOUT_WINDOW:
                fails, first = 1, now
            else:
                fails, first = r["fails"] + 1, r["first"]
            until = now + config.LOCKOUT_SECONDS if fails >= limit else (r["locked_until"] if r else None)
            c.execute("INSERT INTO login_attempts(key,fails,first,locked_until) VALUES(?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, first=excluded.first, locked_until=excluded.locked_until",
                      (key, fails, first, until))
            if fails >= limit:
                locked = config.LOCKOUT_SECONDS
        c.execute("DELETE FROM login_attempts WHERE first < ?", (now - 7 * 86400,))
    return locked

def clear_login_failures(user, ip):
    with _lock, _conn() as c:
        c.execute("DELETE FROM login_attempts WHERE key=?", (f"{(user or '').lower()}|{ip}",))

# ---- audit trail ------------------------------------------------------------------------------
def audit(event, user=None, ip=None, detail=None):
    with _lock, _conn() as c:
        c.execute("INSERT INTO audit(ts,user,ip,event,detail) VALUES(?,?,?,?,?)",
                  (time.time(), user, ip, event, (detail or "")[:300]))
        c.execute("DELETE FROM audit WHERE ts < ?", (time.time() - 180 * 86400,))   # keep 6 months

def audit_recent(limit=100, user=None):
    with _conn() as c:
        if user:
            rows = c.execute("SELECT * FROM audit WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

def requests_today(owner, now=None):
    """Requests this user made in the last 24 h (uploads/dropbox files do not count)."""
    now = now or time.time()
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM requests WHERE owner=? AND created>? AND download_url!='local'",
                         (owner, now - 86400)).fetchone()[0]

def add(owner, r, status="queued"):
    now = time.time()
    with _lock, _conn() as c:
        cur = c.execute("""INSERT INTO requests
            (owner,kind,source,identifier,title,author,download_url,is_torrent,status,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, r["kind"], r["source"], r.get("identifier"), r["title"], r.get("author"),
             r.get("download_url"), 1 if r.get("is_torrent") else 0, status, now, now))
        return cur.lastrowid

def get(rid):
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

def set_status(rid, status, detail=None):
    with _lock, _conn() as c:
        c.execute("UPDATE requests SET status=?, detail=COALESCE(?,detail), updated=? WHERE id=?",
                  (status, detail, time.time(), rid))

def claim_one():
    """Atomically take the next queued request for the worker. BEGIN IMMEDIATE serialises
    claimers across processes too, so no request can be processed twice."""
    with _lock:
        c = _conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM requests WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                c.execute("COMMIT"); return None
            c.execute("UPDATE requests SET status='importing', updated=? WHERE id=?", (time.time(), row["id"]))
            c.execute("COMMIT")
            return dict(row)
        except Exception:
            c.execute("ROLLBACK"); raise
        finally:
            c.close()

def recover_on_start(now=None):
    """Called once when the worker starts. A row left 'importing' by a crash or restart is
    requeued when its source can be fetched again, marked failed when it was a local file
    (the file is gone or parked by then); a torrent 'downloading' for over a day is given up."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("UPDATE requests SET status='queued', detail='requeued after restart', updated=? "
                  "WHERE status='importing' AND download_url!='local'", (now,))
        c.execute("UPDATE requests SET status='error', detail='interrupted by restart', updated=? "
                  "WHERE status='importing' AND download_url='local'", (now,))
        c.execute("UPDATE requests SET status='error', detail='torrent did not complete in 24 h', updated=? "
                  "WHERE status='downloading' AND updated < ?", (now, now - 86400))

def last_for(owner, title, source):
    """The most recent request row for this owner/title/source, or None."""
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE owner=? AND title=? AND source=? ORDER BY id DESC LIMIT 1",
                        (owner, title, source)).fetchone()
        return dict(row) if row else None

def downloading_for_torrents():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE status='downloading' AND is_torrent=1 ORDER BY id").fetchall()]

def list_for(owner, is_admin):
    with _conn() as c:
        if is_admin:
            return [dict(r) for r in c.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200")]
        return [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE owner=? ORDER BY id DESC LIMIT 100", (owner,))]
