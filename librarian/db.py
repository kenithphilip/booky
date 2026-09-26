"""Local state: the request queue and its status. Separate from CWA's DB."""
import sqlite3, time, threading, json
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
            status TEXT NOT NULL,          -- pending|queued|downloading|retrying|importing|tagging|done|needs-tag|needs-review|error|denied|dismissed
            detail TEXT,
            created REAL, updated REAL)""")
        rcols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
        for col, decl in (("restarts", "INTEGER DEFAULT 0"),   # times a restart interrupted this row
                          ("src_size", "INTEGER"), ("src_mtime", "REAL"),   # dropbox file fingerprint
                          # What the FILE said about itself, read at tag time (tagger.py already
                          # had it parsed and was discarding it). For a Shelfmark, qBittorrent,
                          # dropbox or mailed-in arrival this is the only real identification the
                          # portal gets: `title` above is the raw filename and `author` is "".
                          # file_ids is a JSON list of {kind, value} — ISBN, or a Calibre UUID
                          # when the EPUB came from someone's own library.
                          ("file_title", "TEXT"), ("file_author", "TEXT"),
                          ("file_language", "TEXT"), ("file_ids", "TEXT"),
                          # what this request was matched to, and how sure we are. Without
                          # wanted_* there is nothing to verify AGAINST; without a confidence
                          # and a reason, absent evidence reads as confidence — the exact trap
                          # Readarr fell into (a missing identifier scored 0.1, a wrong one 10).
                          ("work_id", "INTEGER"), ("edition_id", "INTEGER"),
                          ("calibre_id", "INTEGER"),
                          ("match_confidence", "REAL"), ("match_reasons", "TEXT"),
                          ("wanted_kind", "TEXT"), ("wanted_language", "TEXT"),
                          ("wanted_abridged", "INTEGER")):
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

        # ---- metadata store --------------------------------------------------------------
        # The portal owns descriptive metadata; stored FILES are never modified for it (the
        # owner:<user> tag is the one exception and it is access control, not description).
        # Shapes follow the verification research: a work groups editions for DISPLAY, but the
        # EDITION is the unit that gets requested, matched and deduped — collapsing on the work
        # merges an abridged audiobook with the full text and a translation with the original.
        c.execute("""CREATE TABLE IF NOT EXISTS meta_work(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT, foreign_id TEXT,        -- e.g. 'bookinfo', '79106958'
            title TEXT, full_title TEXT, short_title TEXT,   -- match on short, display full
            description TEXT,
            first_publish_year INTEGER,            -- the WORK's first appearance...
            release_date TEXT,                     -- ...not this printing's date
            updated REAL,
            UNIQUE(provider, foreign_id))""")
        # kind is NOT NULL and is a HARD GATE, never a score: text must never satisfy an audio
        # request. abridged is tri-state — NULL means 'unknown' and has to stay visible, because
        # treating unknown as 'no' is how someone asks for the full text and gets the abridgement.
        c.execute("""CREATE TABLE IF NOT EXISTS meta_edition(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT, foreign_id TEXT,
            kind TEXT NOT NULL,                    -- 'ebook' | 'audio'
            title TEXT, language TEXT, publisher TEXT, format TEXT,
            edition_statement TEXT, translator TEXT, narrator TEXT,
            abridged INTEGER,                      -- 1 | 0 | NULL = unknown
            pages INTEGER, duration_seconds INTEGER, part_count INTEGER, byte_size INTEGER,
            cover_cache_key TEXT, cover_url TEXT,  -- the cache key is the durable handle
            updated REAL,
            UNIQUE(provider, foreign_id))""")
        # many-to-many: an omnibus is one edition containing several works, which a one-to-one
        # model cannot represent at all, so a request satisfied by a box set was unlinkable
        c.execute("""CREATE TABLE IF NOT EXISTS meta_edition_work(
            edition_id INTEGER NOT NULL, work_id INTEGER NOT NULL, ordinal INTEGER,
            PRIMARY KEY(edition_id, work_id))""")
        # identifiers are a SET, not two columns: isbn13 + asin + gutenberg + librivox + ia +
        # calibre_uuid can all describe one edition, and the precedence ladder needs the rungs.
        # `exact` says whether the value came from an edition-exact lookup or a work-level
        # aggregate — Open Library's arrays span every edition, so an unflagged value is a trap.
        c.execute("""CREATE TABLE IF NOT EXISTS meta_identifier(
            scope TEXT NOT NULL,                   -- 'work' | 'edition'
            target_id INTEGER NOT NULL,
            kind TEXT NOT NULL,                    -- isbn13|isbn10|asin|olid|goodreads|
                                                   -- gutenberg|librivox|ia|calibre_uuid|...
            value TEXT NOT NULL,
            provider TEXT, observed_at REAL, exact INTEGER DEFAULT 0,
            PRIMARY KEY(scope, target_id, kind, value))""")
        c.execute("CREATE INDEX IF NOT EXISTS meta_ident_lookup ON meta_identifier(kind, value)")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_author(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT, foreign_id TEXT,
            name TEXT, name_variants TEXT, bio TEXT, image_url TEXT,
            birth_year INTEGER, death_year INTEGER,   -- the cheap same-name disambiguator
            updated REAL,
            UNIQUE(provider, foreign_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_work_author(
            work_id INTEGER NOT NULL, author_id INTEGER NOT NULL, role TEXT,
            PRIMARY KEY(work_id, author_id, role))""")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_series(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT, foreign_id TEXT, name TEXT, updated REAL,
            UNIQUE(provider, foreign_id))""")
        # position is TEXT because real ones are '3.5', '0' and '1-3 omnibus'; sort_position is
        # the number to order by. An integer-only column silently mangles all three.
        c.execute("""CREATE TABLE IF NOT EXISTS meta_series_work(
            series_id INTEGER NOT NULL, work_id INTEGER NOT NULL,
            position TEXT, sort_position REAL,
            PRIMARY KEY(series_id, work_id))""")
        # what a metadata record corresponds to in the real library / queue
        c.execute("""CREATE TABLE IF NOT EXISTS meta_link(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_id INTEGER, edition_id INTEGER,
            calibre_id INTEGER, rid INTEGER, owner TEXT, created REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS meta_link_calibre ON meta_link(calibre_id)")
        # ONE row per request. Enrichment (the work) and reconciliation (the Calibre id) finish
        # in either order; when each inserted its own row, no row ever held both halves and a
        # book could never find its metadata. Fold any split pairs, then make it impossible.
        c.execute("UPDATE meta_link SET "
                  "work_id = (SELECT max(m.work_id) FROM meta_link m WHERE m.rid = meta_link.rid), "
                  "calibre_id = (SELECT max(m.calibre_id) FROM meta_link m WHERE m.rid = meta_link.rid) "
                  "WHERE rid IS NOT NULL")
        c.execute("DELETE FROM meta_link WHERE rid IS NOT NULL AND id NOT IN "
                  "(SELECT min(id) FROM meta_link WHERE rid IS NOT NULL GROUP BY rid)")
        c.execute("DROP INDEX IF EXISTS meta_link_rid")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS meta_link_rid_u ON meta_link(rid)")
        # provider health, so a failing source is circuit-broken rather than retried into the
        # page deadline every single search (see the failure matrix in the spec)
        # a book the WHOLE chain drew a blank on. Without this, an obscure title is re-asked
        # of every provider on every housekeeping pass for ever.
        # Metadata the host must write into CALIBRE's database so the devices show it (Kobo sync
        # serves from metadata.db). The portal cannot do it itself, on purpose: it mounts the
        # library read-only and has no Docker socket. It decides and queues; a root job on the
        # host applies it with CWA's own calibredb (scripts/metadata-push.sh).
        c.execute("""CREATE TABLE IF NOT EXISTS device_push(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calibre_id INTEGER NOT NULL, rid INTEGER, owner TEXT,
            fields TEXT NOT NULL,                  -- JSON {field: value}, PUSH_FIELDS only
            status TEXT NOT NULL DEFAULT 'pending',-- pending | done | failed | skipped
            attempts INTEGER DEFAULT 0, last_error TEXT, created REAL, updated REAL)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS device_push_open ON device_push(calibre_id) "
                  "WHERE status = 'pending'")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_miss(
            key TEXT PRIMARY KEY, seen REAL)""")
        # 'Keep looking' (wanted.py): a book no catalog had when the reader searched. The worker
        # re-searches on a widening schedule and turns a confident match into a normal request.
        c.execute("""CREATE TABLE IF NOT EXISTS wanted(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            kind TEXT NOT NULL,                 -- ebook | audio
            title TEXT NOT NULL, author TEXT,
            status TEXT NOT NULL,               -- looking | candidate | found | cancelled | expired
            checks INTEGER DEFAULT 0, last_check REAL, next_check REAL,
            identifiers TEXT,                   -- JSON [{kind,value}]: verification only, never a query
            candidate TEXT,                     -- JSON search result awaiting the reader's yes
            confidence REAL, reasons TEXT,      -- why the candidate / the request was chosen
            rejected TEXT,                      -- JSON list of download URLs the reader said no to
            rid INTEGER,                        -- the request it became
            detail TEXT, created REAL, updated REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS wanted_due ON wanted(status, next_check)")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_provider_state(
            provider TEXT PRIMARY KEY,
            failures INTEGER DEFAULT 0, opened_at REAL, retry_after REAL,
            last_error TEXT, last_ok REAL)""")

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

def audit_count(event, user, since):
    """How many `event` rows this user has since `since` (epoch seconds). The audit trail is
    already the record of what the portal did for whom and is kept for six months, so a rate
    ceiling that counts it needs no second table and no pruning chore of its own."""
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) FROM audit WHERE event=? AND user=? AND ts>?",
                      (event, user, since)).fetchone()
    return r[0] if r else 0

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

def set_file_meta(rid, meta):
    """Record what the FILE said about itself (tagger.py read it while embedding the owner tag).
    Best effort: identification is never worth failing an import over."""
    if not rid or not meta:
        return
    ids = meta.get("identifiers") or []
    try:
        with _lock, _conn() as c:
            c.execute("UPDATE requests SET file_title=?, file_author=?, file_language=?, file_ids=? "
                      "WHERE id=?",
                      (meta.get("title") or None, meta.get("author") or None,
                       meta.get("language") or None,
                       json.dumps(ids) if ids else None, rid))
    except sqlite3.Error:
        pass

def file_ids(rid):
    """The identifiers read out of the file, as a list of {kind, value}."""
    with _conn() as c:
        r = c.execute("SELECT file_ids FROM requests WHERE id=?", (rid,)).fetchone()
    if not r or not r["file_ids"]:
        return []
    try:
        return json.loads(r["file_ids"])
    except ValueError:
        return []

def meta_store(merged, rid=None, owner=None, now=None):
    """Land one merged provider record across the meta_* tables and tie it to the request.

    Everything is upserted on (provider, foreign_id) or on the natural key, so re-enriching a
    book updates rather than duplicating. Identifiers accumulate: a second provider knowing one
    more is the best verification evidence the portal gets, and they are the only rungs the
    matching ladder has.

    Deliberately tolerant. Enrichment is decoration plus identification — it must never fail an
    import or crash the worker, so a malformed provider payload loses its metadata and nothing
    else."""
    now = now or time.time()
    if not merged:
        return None
    try:
        with _lock, _conn() as c:
            prov = (merged.get("_providers") or ["?"])[0]
            fid = next((i["value"] for i in merged.get("identifiers", [])
                        if i["kind"].startswith("goodreads")), None) or f"rid:{rid}"
            c.execute("INSERT INTO meta_work(provider,foreign_id,title,full_title,short_title,"
                      "description,first_publish_year,release_date,updated) VALUES(?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(provider,foreign_id) DO UPDATE SET title=excluded.title, "
                      "full_title=excluded.full_title, short_title=excluded.short_title, "
                      "description=excluded.description, first_publish_year=excluded.first_publish_year, "
                      "release_date=excluded.release_date, updated=excluded.updated",
                      (prov, fid, merged.get("title"), merged.get("full_title"),
                       merged.get("short_title"), merged.get("description"),
                       merged.get("first_publish_year"), merged.get("release_date"), now))
            wid = c.execute("SELECT id FROM meta_work WHERE provider=? AND foreign_id=?",
                            (prov, fid)).fetchone()["id"]
            for i in merged.get("identifiers", []):
                c.execute("INSERT OR REPLACE INTO meta_identifier(scope,target_id,kind,value,"
                          "provider,observed_at,exact) VALUES('work',?,?,?,?,?,?)",
                          (wid, i["kind"], i["value"], i.get("provider"), now,
                           1 if i.get("exact") else 0))
            for a in merged.get("authors", []):
                if not a.get("name"):
                    continue
                afid = a.get("foreign_id") or a["name"].lower()
                c.execute("INSERT INTO meta_author(provider,foreign_id,name,bio,image_url,updated) "
                          "VALUES(?,?,?,?,?,?) ON CONFLICT(provider,foreign_id) DO UPDATE SET "
                          "name=excluded.name, bio=COALESCE(excluded.bio, meta_author.bio), "
                          "image_url=COALESCE(excluded.image_url, meta_author.image_url), "
                          "updated=excluded.updated",
                          (prov, afid, a["name"], a.get("bio"), a.get("image_url"), now))
                aid = c.execute("SELECT id FROM meta_author WHERE provider=? AND foreign_id=?",
                                (prov, afid)).fetchone()["id"]
                c.execute("INSERT OR IGNORE INTO meta_work_author(work_id,author_id,role) "
                          "VALUES(?,?,'author')", (wid, aid))
            if merged.get("series"):
                sfid = str(merged["series"]).lower()
                c.execute("INSERT INTO meta_series(provider,foreign_id,name,updated) VALUES(?,?,?,?) "
                          "ON CONFLICT(provider,foreign_id) DO UPDATE SET name=excluded.name, "
                          "updated=excluded.updated", (prov, sfid, merged["series"], now))
                sid = c.execute("SELECT id FROM meta_series WHERE provider=? AND foreign_id=?",
                                (prov, sfid)).fetchone()["id"]
                pos = merged.get("series_position")
                # position is TEXT because real ones are '3.5', '0' and '1-3 omnibus';
                # sort_position is the number to order by, and a non-numeric one sorts last
                try:
                    sortpos = float(str(pos).split("-")[0]) if pos not in (None, "") else None
                except ValueError:
                    sortpos = None
                c.execute("INSERT OR REPLACE INTO meta_series_work(series_id,work_id,position,"
                          "sort_position) VALUES(?,?,?,?)",
                          (sid, wid, str(pos) if pos not in (None, "") else None, sortpos))
            if rid:
                c.execute("UPDATE requests SET work_id=? WHERE id=?", (wid, rid))
                c.execute("INSERT INTO meta_link(work_id,rid,owner,created) VALUES(?,?,?,?) "
                          "ON CONFLICT(rid) DO UPDATE SET work_id = excluded.work_id",
                          (wid, rid, owner, now))
            return wid
    except sqlite3.Error:
        return None

def needs_enrichment(limit=5):
    """Rows with no metadata yet, newest first. Bounded: this runs on a 2-core box beside
    Calibre conversions, and a provider's cold path was measured at 28.4 s."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, owner, title, author, file_title, file_author, file_ids, kind "
            "FROM requests WHERE work_id IS NULL AND status IN ('done','needs-tag') "
            "ORDER BY id DESC LIMIT ?", (limit,))]

def meta_work_for(rid):
    with _conn() as c:
        r = c.execute("SELECT w.* FROM meta_work w JOIN requests q ON q.work_id=w.id "
                      "WHERE q.id=?", (rid,)).fetchone()
    return dict(r) if r else None

# The ONLY fields a push may ever carry into Calibre. Enforced here, at the storage layer, so no
# caller can widen it by accident. `tags` above all is absent and must stay absent: owner:<user>
# IS the per-reader isolation, CWA appends tags rather than replacing them, and a provider genre
# landing on a reader's denied-tags list would hide the book from its owner.
PUSH_FIELDS = ("title", "sort", "authors", "series", "series_index")

def queue_push(calibre_id, fields, rid=None, owner=None, now=None):
    """Queue one Calibre metadata update. Returns the push id, or None when nothing is left
    after the allowlist, or when this book already has a pending push (one at a time)."""
    clean = {k: v for k, v in (fields or {}).items() if k in PUSH_FIELDS and v not in (None, "")}
    if not calibre_id or not clean:
        return None
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO device_push(calibre_id,rid,owner,fields,status,"
                            "created,updated) VALUES(?,?,?,?,'pending',?,?)",
                            (int(calibre_id), rid, owner, json.dumps(clean, ensure_ascii=False), now, now))
            return cur.lastrowid if cur.rowcount else None
    except sqlite3.Error:
        return None

def push_nothing_needed(calibre_id, rid=None, owner=None, now=None):
    """Record that a book was examined and Calibre already had everything worth having, so
    push_candidates stops offering it on every pass for ever."""
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO device_push(calibre_id,rid,owner,fields,status,created,updated) "
                      "VALUES(?,?,?,'{}','skipped',?,?)", (int(calibre_id), rid, owner, now, now))
    except sqlite3.Error:
        pass

def pending_pushes(limit=50):
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, calibre_id, rid, owner, fields, attempts FROM device_push "
            "WHERE status='pending' ORDER BY id LIMIT ?", (limit,))]
    for r in rows:
        # re-filtered on the way OUT too: a row written by an older version, or by hand, still
        # cannot smuggle a field past the allowlist into calibredb
        try:
            f = json.loads(r["fields"])
        except ValueError:
            f = {}
        r["fields"] = {k: v for k, v in f.items() if k in PUSH_FIELDS}
    return rows

def push_result(push_id, ok, error=None, max_attempts=5, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        if ok:
            c.execute("UPDATE device_push SET status='done', updated=?, last_error=NULL WHERE id=?",
                      (now, push_id))
            return "done"
        r = c.execute("SELECT attempts FROM device_push WHERE id=?", (push_id,)).fetchone()
        n = (r["attempts"] if r else 0) + 1
        st = "failed" if n >= max_attempts else "pending"
        c.execute("UPDATE device_push SET attempts=?, status=?, last_error=?, updated=? WHERE id=?",
                  (n, st, (error or "")[:300], now, push_id))
        return st

def push_candidates(limit=20):
    """Requests that have BOTH a metadata record and a certain Calibre id, and no push yet.
    Enrichment (every 120 s) and linking (reconcile, after a 180 s grace) finish in either order,
    so this is the idempotent meeting point rather than a hook on either side."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT q.id AS rid, q.owner, q.calibre_id, q.work_id, q.title AS req_title, "
            "w.title, w.full_title "
            "FROM requests q JOIN meta_work w ON w.id = q.work_id "
            "WHERE q.calibre_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM device_push p WHERE p.calibre_id = q.calibre_id) "
            "ORDER BY q.id DESC LIMIT ?", (limit,))]

def work_authors(work_id):
    with _conn() as c:
        return [r["name"] for r in c.execute(
            "SELECT a.name FROM meta_author a JOIN meta_work_author wa ON wa.author_id = a.id "
            "WHERE wa.work_id = ? ORDER BY a.id", (work_id,))]

def work_series(work_id):
    with _conn() as c:
        r = c.execute("SELECT s.name, sw.position, sw.sort_position FROM meta_series s "
                      "JOIN meta_series_work sw ON sw.series_id = s.id WHERE sw.work_id = ? "
                      "LIMIT 1", (work_id,)).fetchone()
    return dict(r) if r else None

# ---- metadata for the book / author / series pages -----------------------------------------
def meta_for_calibre(calibre_id):
    """The portal's metadata for one Calibre book, via meta_link. None when it was never
    enriched (the page then shows what Calibre alone knows)."""
    with _conn() as c:
        w = c.execute("SELECT w.* FROM meta_link l JOIN meta_work w ON w.id = l.work_id "
                      "WHERE l.calibre_id = ? AND l.work_id IS NOT NULL ORDER BY l.id DESC LIMIT 1",
                      (calibre_id,)).fetchone()
        if not w:
            return None
        authors = [dict(r) for r in c.execute(
            "SELECT a.id, a.name, a.bio, a.birth_year, a.death_year FROM meta_author a "
            "JOIN meta_work_author wa ON wa.author_id = a.id WHERE wa.work_id = ? ORDER BY a.id",
            (w["id"],))]
        s = c.execute("SELECT s.id, s.name, sw.position FROM meta_series s JOIN meta_series_work sw "
                      "ON sw.series_id = s.id WHERE sw.work_id = ? LIMIT 1", (w["id"],)).fetchone()
    return {"work": dict(w), "authors": authors, "series": dict(s) if s else None}

def author_record(author_id):
    with _conn() as c:
        a = c.execute("SELECT * FROM meta_author WHERE id = ?", (author_id,)).fetchone()
        if not a:
            return None
        works = [dict(r) for r in c.execute(
            "SELECT w.id AS work_id, w.title, w.first_publish_year, "
            "(SELECT group_concat(l.calibre_id) FROM meta_link l WHERE l.work_id = w.id "
            " AND l.calibre_id IS NOT NULL) AS calibre_ids "
            "FROM meta_work w JOIN meta_work_author wa ON wa.work_id = w.id "
            "WHERE wa.author_id = ? ORDER BY w.first_publish_year, w.title", (author_id,))]
    return {"author": dict(a), "works": works}

def series_record(series_id):
    with _conn() as c:
        s = c.execute("SELECT * FROM meta_series WHERE id = ?", (series_id,)).fetchone()
        if not s:
            return None
        works = [dict(r) for r in c.execute(
            "SELECT w.id AS work_id, w.title, sw.position, sw.sort_position, "
            "(SELECT group_concat(a.name, ' & ') FROM meta_author a JOIN meta_work_author wa "
            " ON wa.author_id = a.id WHERE wa.work_id = w.id) AS authors, "
            "(SELECT group_concat(l.calibre_id) FROM meta_link l WHERE l.work_id = w.id "
            " AND l.calibre_id IS NOT NULL) AS calibre_ids "
            "FROM meta_series_work sw JOIN meta_work w ON w.id = sw.work_id "
            "WHERE sw.series_id = ? "
            # numeric order, with non-numeric positions ('omnibus') after the numbered ones
            "ORDER BY sw.sort_position IS NULL, sw.sort_position, w.title", (series_id,))]
    return {"series": dict(s), "works": works}

def meta_miss_get(key):
    with _conn() as c:
        r = c.execute("SELECT seen FROM meta_miss WHERE key=?", (key,)).fetchone()
    return r["seen"] if r else None

def meta_miss_set(key, when):
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO meta_miss(key,seen) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET seen=excluded.seen", (key, when))
    except sqlite3.Error:
        pass

def meta_miss_clear(key):
    try:
        with _lock, _conn() as c:
            c.execute("DELETE FROM meta_miss WHERE key=?", (key,))
    except sqlite3.Error:
        pass

def link_calibre(rid, calibre_id, owner=None):
    """Record which Calibre row an import became, at the one moment it is knowable for certain
    (worker._in_calibre matched the ' [owner-rid]' marker). Every later join — dedupe, the book
    page's 'who owns it', the device metadata push — depends on this row existing."""
    if not rid or not calibre_id:
        return
    try:
        with _lock, _conn() as c:
            c.execute("UPDATE requests SET calibre_id=? WHERE id=?", (int(calibre_id), rid))
            c.execute("INSERT INTO meta_link(calibre_id, rid, owner, created) VALUES(?,?,?,?) "
                      "ON CONFLICT(rid) DO UPDATE SET calibre_id = excluded.calibre_id",
                      (int(calibre_id), rid, owner, time.time()))
    except sqlite3.Error:
        pass

def breaker_state(provider, now=None):
    """(open, retry_after) for a metadata provider. Open means: stop asking, it is failing."""
    now = now or time.time()
    with _conn() as c:
        r = c.execute("SELECT opened_at, retry_after FROM meta_provider_state WHERE provider=?",
                      (provider,)).fetchone()
    if not r or not r["opened_at"]:
        return False, 0.0
    ra = r["retry_after"] or 0.0
    return (ra > now), ra

def breaker_record(provider, ok, error=None, now=None, threshold=3, cooldown=900):
    """Count a provider's outcome and open the breaker after `threshold` consecutive failures.

    A SOFT failure (HTTP 200 carrying nothing useful) is a clean miss and must NOT be recorded
    as a failure — advancing the chain is the correct behaviour there, and tripping the breaker
    on it would disable a working provider for having no answer about one book."""
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            if ok:
                c.execute("INSERT INTO meta_provider_state(provider, failures, opened_at, retry_after, last_ok) "
                          "VALUES(?,0,NULL,NULL,?) ON CONFLICT(provider) DO UPDATE SET "
                          "failures=0, opened_at=NULL, retry_after=NULL, last_ok=excluded.last_ok", (provider, now))
                return False
            r = c.execute("SELECT failures FROM meta_provider_state WHERE provider=?", (provider,)).fetchone()
            n = (r["failures"] if r else 0) + 1
            opened = now if n >= threshold else None
            retry = now + cooldown if n >= threshold else None
            c.execute("INSERT INTO meta_provider_state(provider, failures, opened_at, retry_after, last_error) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET failures=excluded.failures, "
                      "opened_at=excluded.opened_at, retry_after=excluded.retry_after, last_error=excluded.last_error",
                      (provider, n, opened, retry, (error or "")[:200]))
            return bool(opened)
    except sqlite3.Error:
        return False

def open_breakers(now=None):
    """Providers currently circuit-broken, for /healthz and the admin page. A chain that has
    silently fallen through to nothing is the failure this project keeps rediscovering."""
    now = now or time.time()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT provider, failures, retry_after, last_error FROM meta_provider_state "
            "WHERE retry_after > ? ORDER BY provider", (now,))]

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

def rename_owner(old, new):
    """Follow an account rename through every row keyed on the name. Called by cwa.rename_user,
    which renames the CWA account itself; the installer separately moves the dropbox, the
    Authelia key and the qBittorrent save path. Without this the admin's request history,
    preferences and audit trail stay pointed at a name that no longer exists — they simply
    vanish from their own pages.

    One transaction. prefs.owner and pw_sync.owner are PRIMARY KEYs, so a pre-existing row for
    <new> (a name reused after a removal) would make a bare UPDATE fail: the old row wins,
    because it belongs to the account actually being renamed."""
    if not old or not new or old == new:
        return {}
    moved = {}
    with _lock, _conn() as c:
        for table in ("prefs", "pw_sync"):
            c.execute(f"DELETE FROM {table} WHERE owner=?", (new,))
            moved[table] = c.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (new, old)).rowcount
        moved["requests"] = c.execute("UPDATE requests SET owner=? WHERE owner=?", (new, old)).rowcount
        moved["abs_tags"] = c.execute("UPDATE abs_tags SET owner=? WHERE owner=?", (new, old)).rowcount
        moved["audit"] = c.execute("UPDATE audit SET user=? WHERE user=?", (new, old)).rowcount
        # the tables added after this function was written carry the name too: a wanted entry
        # left on the old name would be searched for an account that no longer exists
        for table in ("wanted", "device_push", "meta_link"):
            moved[table] = c.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (new, old)).rowcount
    return moved


# ---- keep looking (wanted.py) ---------------------------------------------------------------
WANTED_OPEN = ("looking", "candidate")
WANTED_JSON = ("identifiers", "candidate", "reasons", "rejected")

def _wanted_row(row):
    if not row:
        return None
    d = dict(row)
    for k in WANTED_JSON:
        try:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "candidate" else None)
        except ValueError:
            d[k] = [] if k != "candidate" else None
    return d

def wanted_add(owner, kind, title, author, first_check, limit, same, now=None):
    """Insert a wanted entry unless the owner is at `limit` open ones or already has one for
    the same book (`same(a, b)` decides). One transaction, like add_if_under_quota.
    Returns (id, None) or (None, reason) with reason 'limit' or 'duplicate:<id>'."""
    now = now or time.time()
    with _lock:
        c = _conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in c.execute(
                f"SELECT id, kind, title, author FROM wanted WHERE owner=? AND status IN "
                f"({','.join('?' * len(WANTED_OPEN))})", (owner, *WANTED_OPEN))]
            new = {"kind": kind, "title": title, "author": author}
            dup = next((r for r in rows if same(r, new)), None)
            if dup:
                c.execute("COMMIT")
                return None, f"duplicate:{dup['id']}"
            if limit and len(rows) >= limit:
                c.execute("COMMIT")
                return None, "limit"
            cur = c.execute("""INSERT INTO wanted(owner, kind, title, author, status, checks,
                               next_check, created, updated) VALUES(?,?,?,?,'looking',0,?,?,?)""",
                            (owner, kind, title, author or "", first_check, now, now))
            c.execute("COMMIT")
            return cur.lastrowid, None
        except Exception:
            c.execute("ROLLBACK"); raise
        finally:
            c.close()

def wanted_get(wid):
    with _conn() as c:
        return _wanted_row(c.execute("SELECT * FROM wanted WHERE id=?", (wid,)).fetchone())

def wanted_list(owner=None, include_closed_days=14, now=None):
    """Open entries, plus the ones closed in the last `include_closed_days` (so a reader sees
    'found' and 'expired' for a while instead of the entry silently vanishing). owner=None: all."""
    now = now or time.time()
    since = now - include_closed_days * 86400
    sql = (f"SELECT * FROM wanted WHERE (status IN ({','.join('?' * len(WANTED_OPEN))}) OR updated >= ?)"
           + (" AND owner=?" if owner else "") + " ORDER BY status='candidate' DESC, created DESC")
    args = (*WANTED_OPEN, since) + ((owner,) if owner else ())
    with _conn() as c:
        return [_wanted_row(r) for r in c.execute(sql, args)]

def wanted_due(now, limit):
    with _conn() as c:
        return [_wanted_row(r) for r in c.execute(
            f"SELECT * FROM wanted WHERE status IN ({','.join('?' * len(WANTED_OPEN))}) "
            f"AND (next_check IS NULL OR next_check <= ?) ORDER BY next_check LIMIT ?",
            (*WANTED_OPEN, now, limit))]

def wanted_update(wid, only_if_open=False, **fields):
    """Set columns (JSON ones serialised). only_if_open: do nothing to an entry the reader has
    cancelled meanwhile — the worker's search can take seconds, and a cancel must win.
    Returns True when a row changed."""
    if not fields:
        return False
    for k in WANTED_JSON:
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE wanted SET {cols} WHERE id=?"
    args = [*fields.values(), wid]
    if only_if_open:
        sql += f" AND status IN ({','.join('?' * len(WANTED_OPEN))})"
        args += list(WANTED_OPEN)
    with _lock, _conn() as c:
        return c.execute(sql, args).rowcount > 0

def wanted_expired(before):
    """Open entries created before `before`."""
    with _conn() as c:
        return [_wanted_row(r) for r in c.execute(
            f"SELECT * FROM wanted WHERE status IN ({','.join('?' * len(WANTED_OPEN))}) AND created < ?",
            (*WANTED_OPEN, before))]

def wanted_counts():
    with _conn() as c:
        return {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM wanted GROUP BY status")}
