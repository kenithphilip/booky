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
            status TEXT NOT NULL,          -- pending|queued|downloading|retrying|importing|tagging|done|needs-tag|error|denied|dismissed
            detail TEXT,
            created REAL, updated REAL)""")
        rcols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
        for col, decl in (("restarts", "INTEGER DEFAULT 0"),   # times a restart interrupted this row
                          ("src_size", "INTEGER"), ("src_mtime", "REAL")):   # dropbox file fingerprint
            if col not in rcols:
                c.execute(f"ALTER TABLE requests ADD COLUMN {col} {decl}")
        # Audiobook owner tags still to be applied in ABS. Persisted (not a daemon thread) so a
        # slow ABS scan or a portal restart cannot leave an audiobook untagged and invisible.
        c.execute("""CREATE TABLE IF NOT EXISTS abs_tags(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rid INTEGER, folder TEXT NOT NULL, owner TEXT NOT NULL,
            attempts INTEGER DEFAULT 0, next_try REAL, last_error TEXT,
            created REAL, done REAL)""")
        # how many ABS items the last pass found under the folder: the job closes only when
        # that count is stable, so a box set does not close after its first book is indexed
        if "last_count" not in {r[1] for r in c.execute("PRAGMA table_info(abs_tags)")}:
            c.execute("ALTER TABLE abs_tags ADD COLUMN last_count INTEGER DEFAULT 0")
        # Password fingerprints as the portal last saw them: a change made in Calibre-Web's own
        # UI (which the portal cannot mirror to Audiobookshelf) shows up as a mismatch.
        c.execute("""CREATE TABLE IF NOT EXISTS pw_sync(
            owner TEXT PRIMARY KEY, fp TEXT, updated REAL)""")
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

def locked_keys(now=None):
    """Every live lock, split into user locks ('<user>|<ip>') and bare-IP ones. The admin
    console had no way to see who was stuck or for how long (the lock only ever showed up as a
    'login_locked' row in the audit trail)."""
    now = now or time.time()
    users, ips = [], []
    with _conn() as c:
        rows = c.execute("SELECT key, locked_until FROM login_attempts WHERE locked_until > ? "
                         "ORDER BY locked_until DESC", (now,)).fetchall()
    for r in rows:
        key, until = r["key"], float(r["locked_until"])
        entry = {"until": int(until), "seconds": int(until - now)}
        if "|" in key:
            u, _, ip = key.partition("|")
            users.append({"user": u, "ip": ip, **entry})
        else:
            ips.append({"ip": key, **entry})
    return users, ips

def clear_login_failures_for(user=None, ip=None, everything=False):
    """Release a lockout from the admin console. A user name clears that user from EVERY
    address; an address clears the bare-IP row and every user locked from it (a family behind
    one NAT shares the address, so the bare-IP lock takes the whole household down)."""
    with _lock, _conn() as c:
        if everything:
            return c.execute("DELETE FROM login_attempts").rowcount
        # matched in Python, not with LIKE: '_' and '%' are legal in a user name and are LIKE
        # wildcards, so 'a_b' would also release 'axb'
        if user:
            want = user.lower()
            keys = [k for (k,) in c.execute("SELECT key FROM login_attempts")
                    if k == want or k.startswith(want + "|")]
        elif ip:
            keys = [k for (k,) in c.execute("SELECT key FROM login_attempts")
                    if k == ip or k.endswith("|" + ip)]
        else:
            return 0
        n = 0
        for k in keys:
            n += c.execute("DELETE FROM login_attempts WHERE key=?", (k,)).rowcount
        return n

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

# A request that was denied, failed or dismissed cost the family nothing: it must not eat the
# daily allowance (a source being down would otherwise lock a reader out for the day).
UNCOUNTED = ("denied", "error", "dismissed")
_QUOTA_SQL = ("SELECT COUNT(*), MIN(created) FROM requests WHERE owner=? AND created>? "
              "AND download_url!='local' AND status NOT IN (?,?,?)")

def requests_today(owner, now=None):
    """Requests this user made in the last 24 h that count against the quota (uploads/dropbox
    files, denied, failed and dismissed rows do not)."""
    now = now or time.time()
    with _conn() as c:
        return c.execute(_QUOTA_SQL, (owner, now - 86400, *UNCOUNTED)).fetchone()[0]

def add_if_under_quota(owner, r, limit, status="queued", now=None):
    """Count and insert in ONE immediate transaction, so parallel submissions cannot each see
    'still under the limit' and all get through. Returns (rid, remaining, resets_at); rid is
    None when the limit is reached (remaining 0, resets_at = when the oldest one ages out)."""
    now = now or time.time()
    with _lock:
        c = _conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            used, oldest = c.execute(_QUOTA_SQL, (owner, now - 86400, *UNCOUNTED)).fetchone()
            resets = (oldest or now) + 86400
            if limit and used >= limit:
                c.execute("COMMIT")
                return None, 0, resets
            cur = c.execute("""INSERT INTO requests
                (owner,kind,source,identifier,title,author,download_url,is_torrent,status,created,updated,src_size,src_mtime)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner, r["kind"], r["source"], r.get("identifier"), r["title"], r.get("author"),
                 r.get("download_url"), 1 if r.get("is_torrent") else 0, status, now, now,
                 r.get("src_size"), r.get("src_mtime")))
            c.execute("COMMIT")
            remaining = max(0, limit - used - 1) if limit else 0
            return cur.lastrowid, remaining, (oldest or now) + 86400
        except Exception:
            c.execute("ROLLBACK"); raise
        finally:
            c.close()

def find_open_by_url(owner, url):
    """An earlier request from the same user for the same URL that is not dead: /intake
    replaying a hook must not queue the same book twice."""
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE owner=? AND download_url=? "
                        "AND status NOT IN (?,?,?) ORDER BY id DESC LIMIT 1",
                        (owner, url, *UNCOUNTED)).fetchone()
        return dict(row) if row else None

def add(owner, r, status="queued"):
    now = time.time()
    with _lock, _conn() as c:
        cur = c.execute("""INSERT INTO requests
            (owner,kind,source,identifier,title,author,download_url,is_torrent,status,created,updated,src_size,src_mtime)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, r["kind"], r["source"], r.get("identifier"), r["title"], r.get("author"),
             r.get("download_url"), 1 if r.get("is_torrent") else 0, status, now, now,
             r.get("src_size"), r.get("src_mtime")))
        return cur.lastrowid

def get(rid):
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

def set_status(rid, status, detail=None):
    with _lock, _conn() as c:
        c.execute("UPDATE requests SET status=?, detail=COALESCE(?,detail), updated=? WHERE id=?",
                  (status, detail, time.time(), rid))

def set_status_if(rid, expect, status, detail=None):
    """Move a row only while it is still in `expect`, and say whether this call is the one that
    did it. Approve/deny were check-then-act, so two admins clicking at once both notified the
    requester (and Approve racing Deny sent 'denied' and 'in your library' for one book)."""
    with _lock, _conn() as c:
        return c.execute("UPDATE requests SET status=?, detail=COALESCE(?,detail), updated=? "
                         "WHERE id=? AND status=?", (status, detail, time.time(), rid, expect)).rowcount

def requeue(rid, detail):
    """Put a failed request back in the queue (admin retry); the restart counter starts over."""
    with _lock, _conn() as c:
        c.execute("UPDATE requests SET status='queued', detail=?, restarts=0, updated=? WHERE id=?",
                  (detail, time.time(), rid))

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

INTERRUPTED = "interrupted by restart"
MAX_RESTARTS = 2    # a job that was running during this many restarts is probably what kills us

def recover_on_start(now=None):
    """Called once when the worker starts. A row left 'importing'/'retrying' by a crash or
    restart is requeued when its source can be fetched again, unless restarts already
    interrupted it MAX_RESTARTS times (an intake URL whose file OOM-kills the portal must not
    loop forever); a local (dropbox) row is marked 'interrupted by restart' and the dropbox
    watcher decides whether the file gets one more try. Rows from the removed P2P path fail."""
    now = now or time.time()
    with _lock, _conn() as c:
        # a torrent row can only come from the removed P2P path: it can never be fetched again
        c.execute("UPDATE requests SET status='error', detail='the P2P download path was removed; "
                  "request it again', updated=? WHERE is_torrent=1 AND status NOT IN "
                  "('done','error','denied','dismissed','needs-tag')", (now,))
        live = "status IN ('importing','retrying','downloading') AND download_url!='local'"
        c.execute(f"UPDATE requests SET restarts=COALESCE(restarts,0)+1 WHERE {live}")
        c.execute(f"UPDATE requests SET status='error', updated=?, detail='interrupted by a restart ' || restarts || "
                  f"' times (too large for the portal?); not retried automatically - retry it from the queue' "
                  f"WHERE {live} AND restarts >= ?", (now, MAX_RESTARTS))
        c.execute(f"UPDATE requests SET status='queued', detail='requeued after restart', updated=? WHERE {live}", (now,))
        c.execute("UPDATE requests SET status='error', detail=?, updated=? "
                  "WHERE status IN ('importing','retrying','downloading') AND download_url='local'", (INTERRUPTED, now))

def last_for(owner, title, source):
    """The most recent request row for this owner/title/source, or None."""
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE owner=? AND title=? AND source=? ORDER BY id DESC LIMIT 1",
                        (owner, title, source)).fetchone()
        return dict(row) if row else None

def interrupted_count(owner, title, source, size, mtime):
    """How often this exact dropbox file (same name, size and mtime) was interrupted by a restart."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM requests WHERE owner=? AND title=? AND source=? AND detail=? "
                         "AND src_size IS ? AND src_mtime IS ?",
                         (owner, title, source, INTERRUPTED, size, mtime)).fetchone()[0]

# ---- pending Audiobookshelf tag jobs --------------------------------------------------------
def add_tag_job(rid, folder, owner, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        return c.execute("INSERT INTO abs_tags(rid,folder,owner,attempts,next_try,created) VALUES(?,?,?,?,?,?)",
                         (rid, folder, owner, 0, now, now)).lastrowid

def due_tag_jobs(now=None):
    now = now or time.time()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM abs_tags WHERE done IS NULL AND next_try <= ? ORDER BY id", (now,)).fetchall()]

def pending_tag_jobs():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM abs_tags WHERE done IS NULL ORDER BY id").fetchall()]

def tag_job_retry(jid, next_try, error=None, count=None):
    with _lock, _conn() as c:
        c.execute("UPDATE abs_tags SET attempts=attempts+1, next_try=?, last_error=COALESCE(?,last_error), "
                  "last_count=COALESCE(?,last_count) WHERE id=?", (next_try, error, count, jid))

def tag_job_close(jid, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE abs_tags SET done=? WHERE id=?", (now or time.time(), jid))

def list_for(owner, is_admin):
    with _conn() as c:
        if is_admin:
            return [dict(r) for r in c.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200")]
        return [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE owner=? ORDER BY id DESC LIMIT 100", (owner,))]

def counts_by_status():
    """Every row counted, not just the newest 200 the admin page lists."""
    with _conn() as c:
        return {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")}

def rows_by_status(statuses, limit=None):
    """All rows in these statuses (needs-tag list, retry-all, the import reconciliation).
    limit=None really means all of them: an admin list that silently stops at N hides exactly
    the oldest pending approval or failure that most needs acting on."""
    statuses = tuple(statuses)
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    sql = f"SELECT * FROM requests WHERE status IN ({marks}) ORDER BY id DESC"
    args = statuses
    if limit is not None:
        sql += " LIMIT ?"
        args = (*statuses, limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]

def list_requests(status=None, limit=50, offset=0):
    """A page of the queue for the admin console, oldest-first within the newest-first order.
    Deliberately NOT capped at 200 the way list_for() is: an approval or a failure older than
    the newest 200 rows was unreachable from every console."""
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    where, args = "", []
    if status:
        where, args = "WHERE status=?", [status]
    with _conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]

def count_requests(status=None):
    with _conn() as c:
        if status:
            return c.execute("SELECT COUNT(*) FROM requests WHERE status=?", (status,)).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

def interrupted_rows(limit=200):
    """Local (dropbox) rows the restart recovery marked INTERRUPTED. The file may well have
    reached /ingest before the kill, in which case the row is a permanent bogus failure for a
    book the reader can already see — reconcile_imports checks and closes those."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE status='error' AND detail=? ORDER BY id DESC LIMIT ?",
            (INTERRUPTED, limit))]

def detail_like(fragment, limit=1):
    """The most recent request detail containing this fragment (the admin console uses it to
    show WHY a file was parked in .failed/)."""
    with _conn() as c:
        return [r["detail"] for r in c.execute(
            "SELECT detail FROM requests WHERE detail LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            ("%" + fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit))]

def dismiss_interrupted(owner, title, source, keep_rid):
    """A dropbox file that was interrupted by a restart and then imported successfully leaves a
    dead error row next to the good one: mark those dismissed so they stop counting as failures."""
    with _lock, _conn() as c:
        return c.execute("UPDATE requests SET status='dismissed', updated=?, "
                         "detail='interrupted by a restart; the retry succeeded' "
                         "WHERE owner=? AND title=? AND source=? AND id!=? AND status='error' AND detail=?",
                         (time.time(), owner, title, source, keep_rid, INTERRUPTED)).rowcount

def get_pw_fingerprint(owner):
    with _conn() as c:
        r = c.execute("SELECT fp FROM pw_sync WHERE owner=?", (owner,)).fetchone()
    return r["fp"] if r else None

def set_pw_fingerprint(owner, fp):
    with _lock, _conn() as c:
        c.execute("INSERT INTO pw_sync(owner,fp,updated) VALUES(?,?,?) ON CONFLICT(owner) "
                  "DO UPDATE SET fp=excluded.fp, updated=excluded.updated", (owner, fp, time.time()))
