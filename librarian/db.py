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
                          ("wanted_abridged", "INTEGER"),
                          # What the SOURCE said the file will be, carried from the search result
                          # to the download so worker.verify_download can check it. Missing until
                          # v5: the Request form posted six fields and dropped these, so the
                          # Internet Archive size/SHA-1 check never ran on a real request.
                          ("expect_size", "INTEGER"), ("expect_md5", "TEXT"), ("expect_sha1", "TEXT"),
                          ("src_ids", "TEXT"),            # JSON [[kind, value], ...]
                          ("work_key", "TEXT"),           # the Open Library work it was chosen as
                          ("language", "TEXT")):
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
        wcols = {r[1] for r in c.execute("PRAGMA table_info(wanted)")}
        if "work_key" not in wcols:           # the Open Library work, when asked for from its page
            c.execute("ALTER TABLE wanted ADD COLUMN work_key TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS wanted_due ON wanted(status, next_check)")
        # Copies offered on a page, held server-side: the Request button posts an opaque token,
        # so a download address or its expected checksum can never be edited in the browser.
        # L10: owner tags the HOST must add in Calibre for books whose FILE could not carry one
        # (MOBI/AZW3/FB2/TXT/DJVU, and comics CWA's Kindle fixer stripped). Narrower than
        # device_push on purpose: one operation, "add owner:<x> to a book that has NO owner tag".
        c.execute("""CREATE TABLE IF NOT EXISTS tag_push(
            id INTEGER PRIMARY KEY AUTOINCREMENT, calibre_id INTEGER NOT NULL, rid INTEGER,
            owner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
            attempts INTEGER DEFAULT 0, last_error TEXT, created REAL, updated REAL)""")
        # share=1: family sharing (share.py) — add a SECOND owner to a book that already has one,
        # instead of downloading it again. One open job per (book, reader).
        tcols = {r[1] for r in c.execute("PRAGMA table_info(tag_push)")}
        if "share" not in tcols:
            c.execute("ALTER TABLE tag_push ADD COLUMN share INTEGER DEFAULT 0")
        # op='remove': a reader took the book out of THEIR library (the book page); only their
        # owner tag goes, everyone else keeps it
        if "op" not in tcols:
            c.execute("ALTER TABLE tag_push ADD COLUMN op TEXT DEFAULT 'add'")
        # A book no reader has any more (its last reader removed it, or every owner's account is
        # gone) is deleted from the VPS after LIBRARY_RELEASE_DAYS (worker.reconcile_releases,
        # the host job deletes). Books that never had an owner (the admin's own, added in
        # Calibre-Web) are never listed here.
        c.execute("""CREATE TABLE IF NOT EXISTS audio_release(
            item_id TEXT PRIMARY KEY, title TEXT, since REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',   -- waiting | deleted | kept | failed (v6.1)
            attempts INTEGER DEFAULT 0, last_error TEXT, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS book_release(
            calibre_id INTEGER PRIMARY KEY, since REAL NOT NULL, reason TEXT,
            status TEXT NOT NULL DEFAULT 'waiting',   -- waiting | due | deleted | kept | failed
            tags TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, updated REAL)""")
        # v6.3: who removed it last (a private reader's book is never given to anyone else)
        for t in ("book_release", "audio_release"):
            if "removed_by" not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}:
                c.execute(f"ALTER TABLE {t} ADD COLUMN removed_by TEXT")
        # v6.1: a removal waits for the reader's Kobo to be told (not_before; kobo_wait = how)
        for col, typ in (("not_before", "REAL"), ("kobo_wait", "TEXT")):
            if col not in {r[1] for r in c.execute("PRAGMA table_info(tag_push)")}:
                c.execute(f"ALTER TABLE tag_push ADD COLUMN {col} {typ}")
        c.execute("DROP INDEX IF EXISTS tag_push_open")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tag_push_open_owner ON tag_push(calibre_id, owner) WHERE status = 'pending'")
        # "Find a better copy" (the book page): for REPLACE_DAYS the next EPUB of this book that
        # arrives replaces the FILE inside the same Calibre book (owners, cover, corrected metadata
        # and the Kobo's identity of the book kept); the host job swaps it (metadata-push.sh).
        c.execute("""CREATE TABLE IF NOT EXISTS replace_job(
            id INTEGER PRIMARY KEY AUTOINCREMENT, calibre_id INTEGER NOT NULL, opened_by TEXT,
            status TEXT NOT NULL DEFAULT 'open',   -- open | staged | done | failed | cancelled | expired
            staged TEXT, fmt TEXT, rid INTEGER, owner TEXT, attempts INTEGER DEFAULT 0,
            last_error TEXT, created REAL, updated REAL)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS replace_job_live ON replace_job(calibre_id) "
                  "WHERE status IN ('open','staged')")
        # L21: Send-to-Kindle runs in the worker, not inside the web request (a slow relay with a
        # 45 MB attachment could outlast Cloudflare's 100 s and show a 524 for a mail that went out)
        c.execute("""CREATE TABLE IF NOT EXISTS kindle_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
            book_id INTEGER NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'queued',   -- queued | sent | failed
            detail TEXT, attempts INTEGER DEFAULT 0, next_try REAL, created REAL, updated REAL)""")
        # v5: what the providers already sent and meta_store threw away — the device push fills
        # these into Calibre when Calibre has none (a missing cover, an empty description)
        mcols = {r[1] for r in c.execute("PRAGMA table_info(meta_work)")}
        for col in ("cover_url", "publisher", "language", "pages"):
            if col not in mcols:
                c.execute(f"ALTER TABLE meta_work ADD COLUMN {col} {'INTEGER' if col == 'pages' else 'TEXT'}")
        pcols2 = {r[1] for r in c.execute("PRAGMA table_info(device_push)")}
        if "gen" not in pcols2:           # which vocabulary a push was decided with (see PUSH_GEN)
            c.execute("ALTER TABLE device_push ADD COLUMN gen INTEGER DEFAULT 1")
        # on-demand format conversion, done by the host (scripts/metadata-push.sh, third pass)
        c.execute("""CREATE TABLE IF NOT EXISTS convert_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, calibre_id INTEGER NOT NULL, owner TEXT NOT NULL,
            src_fmt TEXT NOT NULL, dst_fmt TEXT NOT NULL, src_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
            attempts INTEGER DEFAULT 0, detail TEXT, created REAL, updated REAL)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS convert_open ON convert_jobs(calibre_id, dst_fmt) WHERE status='pending'")
        # L05: passwords the portal changed, waiting for the host to write them into Authelia's
        # user file (scripts/gate-sync.py). A PBKDF2 hash, never the password; deleted once applied.
        c.execute("""CREATE TABLE IF NOT EXISTS gate_pw(
            user TEXT PRIMARY KEY, hash TEXT NOT NULL, email TEXT, display TEXT, admin INTEGER DEFAULT 0,
            created REAL, attempts INTEGER DEFAULT 0, detail TEXT)""")
        # L08: the synthetic canary journey's runs (scripts/synthetic.py records them)
        c.execute("""CREATE TABLE IF NOT EXISTS canary_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, ok INTEGER NOT NULL,
            secs REAL, import_secs REAL, failed TEXT, steps TEXT)""")
        # the admin's own OPDS catalogs (catalogs.py)
        c.execute("""CREATE TABLE IF NOT EXISTS catalogs(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, user TEXT, password TEXT,
            enabled INTEGER DEFAULT 1, created REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS candidates(
            token TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL, created REAL)""")
        pcols = {r[1] for r in c.execute("PRAGMA table_info(prefs)")}
        if "language" not in pcols:
            c.execute("ALTER TABLE prefs ADD COLUMN language TEXT")
        # v6.0: phone notifications (a private ntfy topic) and the Hardcover Want to Read sync
        for col, typ in (("ntfy_topic", "TEXT"), ("hc_want", "INTEGER DEFAULT 0"), ("hc_want_kind", "TEXT"),
                         ("hc_want_seeded", "REAL"),      # when the list was last recorded without requesting
                         ("hc_want_after", "INTEGER"),    # the newest list entry (user_book id) recorded then
                         ("devices", "TEXT"),             # v6.2: the reader's devices (JSON list, devicemodels.py)
                         ("kobo_finished", "INTEGER"),    # v6.3: take finished books off the Kobo after N days (NULL: never)
                         ("kindle_hint", "INTEGER DEFAULT 1"),   # v6.3: remind me to delete finished books from my Kindle
                         ("private", "INTEGER DEFAULT 0"),       # v6.3: my books are never offered to the family
                         ("kobo_scope", "TEXT")):         # v6.3: an admin's Kobo: own (default) | choose | library
            if col not in pcols:
                c.execute(f"ALTER TABLE prefs ADD COLUMN {col} {typ}")
        c.execute("""CREATE TABLE IF NOT EXISTS hc_want_seen(
            owner TEXT NOT NULL, book_id INTEGER NOT NULL, at REAL, requested INTEGER DEFAULT 0,
            title TEXT, author TEXT, PRIMARY KEY(owner, book_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS meta_provider_state(
            provider TEXT PRIMARY KEY,
            failures INTEGER DEFAULT 0, opened_at REAL, retry_after REAL,
            last_error TEXT, last_ok REAL)""")
        # admin notifications about things polled every few seconds (Shelfmark's queue): told once
        c.execute("""CREATE TABLE IF NOT EXISTS notified(key TEXT PRIMARY KEY, at REAL NOT NULL)""")
        # comics (docs/COMICS.md): what the metadata providers said (a day), the readers' requests,
        # which Calibre books have their Kobo copy, and Kindle jobs that need a comic converted first
        c.execute("""CREATE TABLE IF NOT EXISTS http_cache(key TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL NOT NULL)""")
        if "keep" not in {r[1] for r in c.execute("PRAGMA table_info(http_cache)")}:
            c.execute("ALTER TABLE http_cache ADD COLUMN keep REAL")       # v6.0: days this entry is kept
        c.execute("""CREATE TABLE IF NOT EXISTS comic_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            provider TEXT NOT NULL, series_id TEXT NOT NULL, series_name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'comic', reading TEXT NOT NULL DEFAULT 'ltr', strip INTEGER,
            number TEXT NOT NULL, label TEXT, year INTEGER, publisher TEXT, language TEXT NOT NULL DEFAULT 'en',
            cover TEXT, authors TEXT, summary TEXT,
            -- queued | pending | confirm | downloading | held | done | shared | not-found | failed | cancelled
            status TEXT NOT NULL DEFAULT 'queued',
            detail TEXT, tried TEXT DEFAULT '[]', release_title TEXT, release_id TEXT,
            attempts INTEGER DEFAULT 0, next_try REAL, queued_at REAL, calibre_id INTEGER,
            created REAL, updated REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS comic_requests_owner ON comic_requests(owner, status)")
        c.execute("""CREATE TABLE IF NOT EXISTS comic_convert(
            calibre_id INTEGER PRIMARY KEY, status TEXT NOT NULL,   -- due | done | failed
            forced INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, next_try REAL, detail TEXT, updated REAL)""")
        if "remake" not in {r[1] for r in c.execute("PRAGMA table_info(comic_convert)")}:
            c.execute("ALTER TABLE comic_convert ADD COLUMN remake INTEGER DEFAULT 0")   # v6.1: replace the Kobo copy
        if "made" not in {r[1] for r in c.execute("PRAGMA table_info(comic_convert)")}:
            c.execute("ALTER TABLE comic_convert ADD COLUMN made TEXT")    # v6.2: what the Kobo copy was made for (JSON)
        # v6.3: books the portal took off a reader's device, or keeps on it, or reminded them about:
        # device kobo|kindle; status waiting (finished, counting down) | off | kept | reminded | done
        c.execute("""CREATE TABLE IF NOT EXISTS device_book(
            owner TEXT NOT NULL, book_id INTEGER NOT NULL, device TEXT NOT NULL, status TEXT NOT NULL,
            since REAL, updated REAL, PRIMARY KEY(owner, book_id, device))""")
        # v6.2: how a comic's pages are laid out on e-readers, when a reader chose it (else: from its pages)
        c.execute("""CREATE TABLE IF NOT EXISTS comic_layout(
            calibre_id INTEGER PRIMARY KEY, layout TEXT NOT NULL, owner TEXT, updated REAL)""")
        # v5.8: what readers follow, what turned up for them, and their AniList link
        c.execute("""CREATE TABLE IF NOT EXISTS follows(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            kind TEXT NOT NULL,            -- comic (a comic/manga series) | book-series | author
            provider TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, extra TEXT,
            known TEXT,                    -- what was already out at the last check (JSON list)
            checked REAL, next_check REAL, created REAL, detail TEXT,
            UNIQUE(owner, kind, provider, key))""")
        c.execute("""CREATE TABLE IF NOT EXISTS notices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, follow_id INTEGER,
            item_key TEXT NOT NULL, title TEXT NOT NULL, detail TEXT, item TEXT,
            status TEXT NOT NULL DEFAULT 'new',   -- new | requested | dismissed
            mailed INTEGER DEFAULT 0, created REAL, updated REAL,
            UNIQUE(owner, follow_id, item_key))""")
        # v5.8.3: one-tap book requests, searched and queued through Shelfmark like comics (bookreq.py)
        c.execute("""CREATE TABLE IF NOT EXISTS book_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            title TEXT NOT NULL, author TEXT, series TEXT, language TEXT NOT NULL DEFAULT 'en',
            hardcover_id TEXT, notice_id INTEGER,
            -- queued | pending | confirm (a copy found, the reader decides) | downloading | held (the file
            -- that came does not look like the book) | done | shared | owned | not-found | cancelled
            status TEXT NOT NULL DEFAULT 'queued',
            detail TEXT, tried TEXT DEFAULT '[]', release_title TEXT, release_id TEXT,
            attempts INTEGER DEFAULT 0, next_try REAL, queued_at REAL, calibre_id INTEGER,
            downloaded REAL,                         -- Shelfmark reported the download complete
            candidate TEXT,                          -- the release offered to confirm (JSON, as Shelfmark sent it)
            reasons TEXT,                            -- why it was chosen (JSON list)
            blocked TEXT DEFAULT '[]',               -- release names and Calibre books the reader said were not it
            skip_check INTEGER DEFAULT 0,            -- 'Keep it anyway': the arrival is not checked again
            held_path TEXT, held_meta TEXT,          -- the held file, and what it said it was
            created REAL, updated REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS book_requests_owner ON book_requests(owner, status)")
        c.execute("""CREATE TABLE IF NOT EXISTS anilist(
            owner TEXT PRIMARY KEY, token TEXT NOT NULL, al_user_id INTEGER, al_name TEXT,
            connected REAL, detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS anilist_media(
            series TEXT PRIMARY KEY, media_id INTEGER, title TEXT, volumes INTEGER, status TEXT, at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS anilist_progress(
            owner TEXT NOT NULL, media_id INTEGER NOT NULL, volumes INTEGER NOT NULL, at REAL,
            PRIMARY KEY(owner, media_id))""")
        # v6.0: book requests for audiobooks too (kind), and the Audiobookshelf item that arrived
        bcols = {r[1] for r in c.execute("PRAGMA table_info(book_requests)")}
        # ask: always wait for the reader's yes, even with BOOK_CONFIRM=sure (Hardcover Want to Read)
        for col, typ in (("kind", "TEXT DEFAULT 'ebook'"), ("abs_item", "TEXT"), ("ask", "INTEGER DEFAULT 0"),
                         ("size_bytes", "INTEGER")):            # v6.0.1: the download's size, reserved on the disk
            if col not in bcols:
                c.execute(f"ALTER TABLE book_requests ADD COLUMN {col} {typ}")
        # v5.9: a reader's Metron account (Western comics read), and what was sent to it
        c.execute("""CREATE TABLE IF NOT EXISTS metron_link(
            owner TEXT PRIMARY KEY, username TEXT NOT NULL, secret TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'key', connected REAL, detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS metron_sent(
            owner TEXT NOT NULL, issue_id INTEGER NOT NULL, at REAL, PRIMARY KEY(owner, issue_id))""")
        # v5.9.1: audiobook progress sent to each reader's Hardcover (hcaudio.py)
        c.execute("""CREATE TABLE IF NOT EXISTS hc_audio(
            owner TEXT NOT NULL, item_id TEXT NOT NULL,
            book_id INTEGER, edition_id INTEGER, matched TEXT,   -- the Hardcover book ('' = none found)
            sent_seconds INTEGER, sent_finished INTEGER DEFAULT 0, last_update REAL, at REAL,
            PRIMARY KEY(owner, item_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS hc_audio_state(owner TEXT PRIMARY KEY, detail TEXT, at REAL)""")
        # v5.9.1: send a book to an e-reader's own browser with a 4-character code (sendcode.py)
        c.execute("""CREATE TABLE IF NOT EXISTS send_codes(
            code TEXT PRIMARY KEY, secret TEXT NOT NULL, device TEXT NOT NULL, created REAL NOT NULL,
            owner TEXT, book_id INTEGER, fmt TEXT, attached REAL, fetched REAL)""")
        # v5.9: comic requests get the book safeguards (confirm, the arrival check, Wrong comic)
        ccols = {r[1] for r in c.execute("PRAGMA table_info(comic_requests)")}
        for col, typ in (("candidate", "TEXT"), ("reasons", "TEXT"), ("blocked", "TEXT DEFAULT '[]'"),
                         ("skip_check", "INTEGER DEFAULT 0"), ("held_path", "TEXT"), ("held_meta", "TEXT"),
                         ("unit", "TEXT DEFAULT ''"),           # 'chapter', or '' (a volume or an issue, by kind)
                         ("size_bytes", "INTEGER")):            # v6.0.1: the download's size, reserved on the disk
            if col not in ccols:
                c.execute(f"ALTER TABLE comic_requests ADD COLUMN {col} {typ}")
        # v5.9: a volume arrived for a reader who has its chapters: which to take out (they decide)
        c.execute("""CREATE TABLE IF NOT EXISTS comic_swaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, request_id INTEGER NOT NULL,
            series_name TEXT NOT NULL, volume TEXT NOT NULL, volume_book INTEGER,
            chapters TEXT NOT NULL,                  -- [{book_id, number, tick}] (JSON)
            known INTEGER DEFAULT 0,                 -- MangaDex said which chapters the volume holds
            status TEXT NOT NULL DEFAULT 'offered',  -- offered | done | kept
            created REAL, updated REAL, UNIQUE(owner, request_id))""")
        kcols = {r[1] for r in c.execute("PRAGMA table_info(kindle_jobs)")}
        for col, typ in (("kind", "TEXT DEFAULT 'book'"), ("files", "TEXT")):
            if col not in kcols:
                c.execute(f"ALTER TABLE kindle_jobs ADD COLUMN {col} {typ}")

def first_notice(key, now=None, keep_days=30):
    """True the first time `key` is seen (and remembers it), False after that. Survives a
    restart, so a portal restart does not repeat every notice. Keys older than keep_days go."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("DELETE FROM notified WHERE at < ?", (now - keep_days * 86400,))
        return c.execute("INSERT OR IGNORE INTO notified(key, at) VALUES(?, ?)", (key, now)).rowcount == 1

# ---- a small cache for provider answers (comicmeta.py) ----------------------------------------
def cache_get(key, max_age):
    """The cached value, or None. max_age None: any age (a provider that is down)."""
    with _conn() as c:
        r = c.execute("SELECT data, at FROM http_cache WHERE key=?", (key,)).fetchone()
    if not r or (max_age is not None and time.time() - r["at"] > max_age):
        return None
    return json.loads(r["data"])

def cache_put(key, data, keep_days=14):
    """v6.0: each entry keeps its own retention (keep_days): trimming the whole table by the
    caller's retention dropped every 30-day Hardcover answer and 365-day note after 14 days."""
    now = time.time()
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO http_cache(key, data, at, keep) VALUES(?,?,?,?)",
                  (key, json.dumps(data), now, float(keep_days)))
        c.execute("DELETE FROM http_cache WHERE at < ? - coalesce(keep, 14) * 86400", (now,))

def cache_clear_prefix(prefix):
    with _lock, _conn() as c:
        c.execute("DELETE FROM http_cache WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))

def get_prefs(owner):
    with _conn() as c:
        row = c.execute("SELECT * FROM prefs WHERE owner=?", (owner,)).fetchone()
    d = dict(row) if row else {}
    fmt = d.get("preferred_format") or config.DEFAULT_FORMAT
    if fmt not in config.FORMATS:               # e.g. a 'kepub' pref stored before that choice was dropped
        fmt = config.DEFAULT_FORMAT
    return {"preferred_format": fmt, "auto_kindle": bool(d.get("auto_kindle")),
            "notify_email": bool(d.get("notify_email")), "last_kindle_test": d.get("last_kindle_test"),
            # the language this reader reads in: copies in another language are never taken
            "language": d.get("language") or config.BOOK_LANGUAGE,
            # v6.0
            "ntfy_topic": d.get("ntfy_topic") or "", "hc_want": bool(d.get("hc_want")),
            "hc_want_kind": d.get("hc_want_kind") if d.get("hc_want_kind") in ("ebook", "audio", "both") else "ebook",
            "hc_want_seeded": d.get("hc_want_seeded"),
            # v6.2: the devices the reader reads on (devicemodels.py keys)
            "devices": _json_list(d.get("devices")),
            # v6.3: keeping devices tidy, and privacy
            "kobo_finished": d.get("kobo_finished") if d.get("kobo_finished") in (0, 7, 30) else None,
            "kindle_hint": d.get("kindle_hint") != 0, "private": bool(d.get("private")),
            "kobo_scope": d.get("kobo_scope") if d.get("kobo_scope") in ("own", "choose", "library") else "own"}

def set_device_prefs(owner, **f):
    """v6.3: kobo_finished (None/0/7/30), kindle_hint, private, kobo_scope (checked by the caller)."""
    f = {k: v for k, v in f.items() if k in ("kobo_finished", "kindle_hint", "private", "kobo_scope")}
    if not f:
        return
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO prefs(owner, updated) VALUES(?,?)", (owner, time.time()))
        c.execute(f"UPDATE prefs SET {', '.join(f'{k}=?' for k in f)}, updated=? WHERE owner=?", (*f.values(), time.time(), owner))

def private_readers():
    """v6.3: readers whose books are never offered to the rest of the family."""
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT owner FROM prefs WHERE private=1")}

def readers_with(col):
    """v6.3: [(owner, value)] with kobo_finished set, or kindle_hint on."""
    if col not in ("kobo_finished", "kindle_hint"):
        return []
    with _conn() as c:
        q = "SELECT owner, kobo_finished FROM prefs WHERE kobo_finished IN (0, 7, 30)" if col == "kobo_finished" \
            else "SELECT owner, 1 FROM prefs WHERE coalesce(kindle_hint, 1)=1"
        return [(r[0], r[1]) for r in c.execute(q)]

def device_book(owner, book_id, device):
    with _conn() as c:
        r = c.execute("SELECT * FROM device_book WHERE owner=? AND book_id=? AND device=?", (owner, int(book_id), device)).fetchone()
    return dict(r) if r else None

def device_books(owner, device, statuses=None):
    with _conn() as c:
        q, a = "SELECT * FROM device_book WHERE owner=? AND device=?", [owner, device]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            a += list(statuses)
        return {r["book_id"]: dict(r) for r in c.execute(q, a)}

def device_book_set(owner, book_id, device, status, now=None):
    """Record what happened (since: when the status began; kept when it does not change)."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("INSERT INTO device_book(owner, book_id, device, status, since, updated) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(owner, book_id, device) DO UPDATE SET since=CASE WHEN status=excluded.status "
                  "THEN since ELSE excluded.since END, status=excluded.status, updated=excluded.updated",
                  (owner, int(book_id), device, status, now, now))

def device_book_clear(owner, book_id, device):
    with _lock, _conn() as c:
        c.execute("DELETE FROM device_book WHERE owner=? AND book_id=? AND device=?", (owner, int(book_id), device))

def _json_list(v):
    try:
        out = json.loads(v) if v else []
    except ValueError:
        return []
    return [x for x in out if isinstance(x, str)] if isinstance(out, list) else []

def set_devices(owner, keys):
    """v6.2: the reader's devices, as chosen on the start page (keys already checked)."""
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO prefs(owner, updated) VALUES(?,?)", (owner, time.time()))
        c.execute("UPDATE prefs SET devices=?, updated=? WHERE owner=?", (json.dumps(list(keys)), time.time(), owner))

def set_prefs_v6(owner, **f):
    """v6.0 reader settings: ntfy_topic, hc_want, hc_want_kind (kept apart from set_prefs so its
    callers and their defaults stay exactly as they were)."""
    f = {k: v for k, v in f.items() if k in ("ntfy_topic", "hc_want", "hc_want_kind", "hc_want_seeded", "hc_want_after")}
    if not f:
        return
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO prefs(owner, updated) VALUES(?,?)", (owner, time.time()))
        c.execute(f"UPDATE prefs SET {', '.join(f'{k}=?' for k in f)}, updated=? WHERE owner=?", (*f.values(), time.time(), owner))

def prefs_with(col):
    """[(owner, value)] of readers with a v6.0 setting on (ntfy_topic set, hc_want on)."""
    if col not in ("ntfy_topic", "hc_want"):
        return []
    with _conn() as c:
        return [(r[0], r[1]) for r in c.execute(f"SELECT owner, {col} FROM prefs WHERE {col} IS NOT NULL AND {col} != '' AND {col} != 0")]

def hc_want_after(owner):
    """The newest Want to Read entry (Hardcover user_book id) that was on the list when it was recorded."""
    with _conn() as c:
        r = c.execute("SELECT hc_want_after FROM prefs WHERE owner=?", (owner,)).fetchone()
    return (r[0] or 0) if r else 0

def hc_want_seen(owner):
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT book_id FROM hc_want_seen WHERE owner=?", (owner,))}

def hc_want_mark(owner, book_id, title, author, requested, now=None):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO hc_want_seen(owner, book_id, at, requested, title, author) VALUES(?,?,?,?,?,?)",
                  (owner, int(book_id), now or time.time(), 1 if requested else 0, (title or "")[:300], (author or "")[:200]))

def hc_want_unrequested(owner):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM hc_want_seen WHERE owner=? AND requested=0 ORDER BY at", (owner,))]

def set_prefs(owner, preferred_format=None, auto_kindle=None, notify_email=None, last_kindle_test=None,
              language=None):
    cur = get_prefs(owner)
    lg = language if language in config.LANGUAGES else cur["language"]
    fmt = preferred_format if preferred_format in config.FORMATS else cur["preferred_format"]
    ak = cur["auto_kindle"] if auto_kindle is None else bool(auto_kindle)
    ne = cur["notify_email"] if notify_email is None else bool(notify_email)
    lkt = cur["last_kindle_test"] if last_kindle_test is None else float(last_kindle_test)
    with _lock, _conn() as c:
        c.execute("""INSERT INTO prefs(owner,preferred_format,auto_kindle,notify_email,last_kindle_test,language,updated)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(owner) DO UPDATE SET preferred_format=excluded.preferred_format,
                     auto_kindle=excluded.auto_kindle, notify_email=excluded.notify_email,
                     last_kindle_test=excluded.last_kindle_test, language=excluded.language,
                     updated=excluded.updated""",
                  (owner, fmt, 1 if ak else 0, 1 if ne else 0, lkt, lg, time.time()))

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

_INSERT_SQL = """INSERT INTO requests
    (owner,kind,source,identifier,title,author,download_url,is_torrent,status,created,updated,src_size,src_mtime,
     expect_size,expect_md5,expect_sha1,src_ids,work_key,language)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

def _insert_args(owner, r, status, now):
    size = r.get("expect_size")
    return (owner, r["kind"], r["source"], r.get("identifier"), r["title"], r.get("author"),
            r.get("download_url"), 1 if r.get("is_torrent") else 0, status, now, now,
            r.get("src_size"), r.get("src_mtime"),
            int(size) if str(size or "").isdigit() else None, r.get("expect_md5") or None,
            r.get("expect_sha1") or None,
            json.dumps([list(x) for x in r["src_ids"]]) if r.get("src_ids") else None,
            r.get("work_key") or None, r.get("language") or None)

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
            cur = c.execute(_INSERT_SQL, _insert_args(owner, r, status, now))
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
        cur = c.execute(_INSERT_SQL, _insert_args(owner, r, status, now))
        return cur.lastrowid

def get(rid):
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

def merge_file_meta(rid, meta):
    """Like set_file_meta, but only fills what is still empty: a sidecar .opf is a second
    opinion, never allowed to overwrite what the file itself said. Identifiers accumulate."""
    if not rid or not meta:
        return
    try:
        with _lock, _conn() as c:
            row = c.execute("SELECT file_title, file_author, file_language, file_ids FROM requests WHERE id=?",
                            (rid,)).fetchone()
            if not row:
                return
            try:
                ids = json.loads(row["file_ids"]) if row["file_ids"] else []
            except ValueError:
                ids = []
            have = {(i.get("kind"), i.get("value")) for i in ids}
            ids += [i for i in meta.get("identifiers") or [] if (i.get("kind"), i.get("value")) not in have]
            c.execute("UPDATE requests SET file_title=?, file_author=?, file_language=?, file_ids=? WHERE id=?",
                      (row["file_title"] or meta.get("title") or None,
                       row["file_author"] or meta.get("author") or None,
                       row["file_language"] or meta.get("language") or None,
                       json.dumps(ids) if ids else None, rid))
    except sqlite3.Error:
        pass

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
                      "description,first_publish_year,release_date,cover_url,publisher,language,pages,updated) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(provider,foreign_id) DO UPDATE SET title=excluded.title, "
                      "full_title=excluded.full_title, short_title=excluded.short_title, "
                      "description=excluded.description, first_publish_year=excluded.first_publish_year, "
                      "release_date=excluded.release_date, cover_url=COALESCE(excluded.cover_url, meta_work.cover_url), "
                      "publisher=COALESCE(excluded.publisher, meta_work.publisher), "
                      "language=COALESCE(excluded.language, meta_work.language), "
                      "pages=COALESCE(excluded.pages, meta_work.pages), updated=excluded.updated",
                      (prov, fid, merged.get("title"), merged.get("full_title"),
                       merged.get("short_title"), merged.get("description"),
                       merged.get("first_publish_year"), merged.get("release_date"),
                       merged.get("cover_url"), merged.get("publisher") if isinstance(merged.get("publisher"), str) else None,
                       merged.get("language") if isinstance(merged.get("language"), str) else None,
                       merged.get("pages") if isinstance(merged.get("pages"), int) else None, now))
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
            "SELECT id, owner, title, author, file_title, file_author, file_ids, kind, work_key "
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
# What the host may write into Calibre, and nothing else — three copies of this list exist on
# purpose (here, pending_pushes' re-filter, scripts/metadata-push.sh ALLOWED). `tags` is absent
# and must stay absent. cover_url is not a Calibre field: the host fetches the image and sets
# Calibre's `cover` from it. Every field is FILL-ONLY (worker.queue_device_pushes).
PUSH_FIELDS = ("title", "sort", "authors", "series", "series_index",
               "comments", "publisher", "pubdate", "languages", "identifiers", "cover_url")
PUSH_GEN = 2          # v5 added the second half of the vocabulary: books pushed before get one more look

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
                            "created,updated,gen) VALUES(?,?,?,?,'pending',?,?,?)",
                            (int(calibre_id), rid, owner, json.dumps(clean, ensure_ascii=False), now, now, PUSH_GEN))
            return cur.lastrowid if cur.rowcount else None
    except sqlite3.Error:
        return None

def push_nothing_needed(calibre_id, rid=None, owner=None, now=None):
    """Record that a book was examined and Calibre already had everything worth having, so
    push_candidates stops offering it on every pass for ever."""
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO device_push(calibre_id,rid,owner,fields,status,created,updated,gen) "
                      "VALUES(?,?,?,'{}','skipped',?,?,?)", (int(calibre_id), rid, owner, now, now, PUSH_GEN))
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
            "w.title, w.full_title, w.description, w.publisher, w.language, w.release_date, w.cover_url "
            "FROM requests q JOIN meta_work w ON w.id = q.work_id "
            "WHERE q.calibre_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM device_push p WHERE p.calibre_id = q.calibre_id "
            "AND (p.status = 'pending' OR COALESCE(p.gen, 1) >= ?)) "
            "ORDER BY q.id DESC LIMIT ?", (PUSH_GEN, limit))]

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

def wanted_add(owner, kind, title, author, first_check, limit, same, now=None, work_key=None):
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
                               next_check, created, updated, work_key) VALUES(?,?,?,?,'looking',0,?,?,?,?)""",
                            (owner, kind, title, author or "", first_check, now, now, work_key))
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

def set_match(rid, confidence, reasons, wanted_kind=None):
    """How sure the portal was that this request is the book that was wanted, and why."""
    with _lock, _conn() as c:
        c.execute("UPDATE requests SET match_confidence=?, match_reasons=?, "
                  "wanted_kind=COALESCE(?, wanted_kind) WHERE id=?",
                  (confidence, json.dumps(reasons) if not isinstance(reasons, str) else reasons,
                   wanted_kind, rid))


# ---- candidates offered on a page (the Request button posts a token, never a URL) -------------
CANDIDATE_TTL = 24 * 3600

def candidate_put(owner, cand, now=None):
    import secrets
    token = secrets.token_urlsafe(16)
    with _lock, _conn() as c:
        c.execute("INSERT INTO candidates(token, owner, data, created) VALUES(?,?,?,?)",
                  (token, owner, json.dumps(cand), now or time.time()))
    return token

def candidate_get(token, owner, now=None):
    """The copy behind a token, only for the reader it was offered to and only for a day."""
    now = now or time.time()
    with _conn() as c:
        row = c.execute("SELECT data, created FROM candidates WHERE token=? AND owner=?",
                        (token or "", owner)).fetchone()
    if not row or row["created"] < now - CANDIDATE_TTL:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None

def candidate_purge(now=None):
    with _lock, _conn() as c:
        return c.execute("DELETE FROM candidates WHERE created < ?",
                         ((now or time.time()) - CANDIDATE_TTL,)).rowcount


# ---- the admin's own OPDS catalogs (catalogs.py) ---------------------------------------------
def catalog_rows():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM catalogs ORDER BY name")]

def catalog_put(cid, name, url, user, password, enabled=True):
    """Insert or update. A blank password on update keeps the stored one."""
    with _lock, _conn() as c:
        old = c.execute("SELECT password FROM catalogs WHERE id=?", (cid,)).fetchone()
        pw = password if password or not old else old["password"]
        c.execute("""INSERT INTO catalogs(id, name, url, user, password, enabled, created) VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url, user=excluded.user,
                     password=excluded.password, enabled=excluded.enabled""",
                  (cid, name, url, user or "", pw or "", 1 if enabled else 0, time.time()))

def catalog_delete(cid):
    with _lock, _conn() as c:
        return c.execute("DELETE FROM catalogs WHERE id=?", (cid,)).rowcount > 0


# ---- L10: owner tags added by the host (scripts/metadata-push.sh, second pass) --------------
def queue_tag_push(calibre_id, rid, owner, now=None, share=False, op="add"):
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            if op == "add":
                # v6.1: the reader asks again while their removal waits for the Kobo: the removal is
                # withdrawn (their tag never came off, so there is nothing to add)
                if c.execute("UPDATE tag_push SET status='withdrawn', updated=? WHERE calibre_id=? AND owner=? "
                             "AND status='pending' AND op='remove'", (now, int(calibre_id), owner)).rowcount:
                    return True
            # share: 1 a family share (a second owner); 2 (v6.2.1) a book its last reader removed, given
            # back during its countdown: the first owner again, which the host job otherwise refuses
            c.execute("INSERT INTO tag_push(calibre_id, rid, owner, share, op, created, updated) VALUES(?,?,?,?,?,?,?)",
                      (int(calibre_id), rid, owner, 2 if share == 2 else 1 if share else 0, op, now, now))
        return True
    except sqlite3.IntegrityError:
        return False                          # one open job per book and reader

def queue_untag(calibre_id, owner, now=None, not_before=None, kobo_wait=None):
    """'Remove from my library': take this reader's owner tag off the book (host job). A
    pending ADD for the same book and reader is simply withdrawn instead. v6.1: not_before /
    kobo_wait hold it until the reader's Kobo has been told to delete it (kobo_waits())."""
    now = now or time.time()
    with _lock, _conn() as c:
        row = c.execute("SELECT id, op FROM tag_push WHERE calibre_id=? AND owner=? AND status='pending'",
                        (int(calibre_id), owner)).fetchone()
        if row and row["op"] != "remove":
            c.execute("UPDATE tag_push SET status='withdrawn', updated=? WHERE id=?", (now, row["id"]))
        elif row:
            return True
        c.execute("INSERT INTO tag_push(calibre_id, rid, owner, share, op, created, updated, not_before, kobo_wait) "
                  "VALUES(?,?,?,0,'remove',?,?,?,?)", (int(calibre_id), None, owner, now, now, not_before, kobo_wait))
        return True

def untag_pending(calibre_id, owner):
    with _conn() as c:
        return c.execute("SELECT 1 FROM tag_push WHERE calibre_id=? AND owner=? AND op='remove' AND status='pending'",
                         (int(calibre_id), owner)).fetchone() is not None

def release_note(calibre_id, reason, tags, now=None, removed_by=None):
    """This book has no reader any more: start (or keep) its countdown. removed_by (v6.3): the
    reader whose removal it was."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("INSERT INTO book_release(calibre_id, since, reason, tags, updated, removed_by) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(calibre_id) DO UPDATE SET tags=excluded.tags, updated=excluded.updated, "
                  "removed_by=coalesce(excluded.removed_by, book_release.removed_by), "
                  "status=CASE WHEN book_release.status IN ('kept','failed') THEN 'waiting' ELSE book_release.status END, "
                  "since=CASE WHEN book_release.status IN ('kept','failed') THEN excluded.since ELSE book_release.since END",
                  (int(calibre_id), now, reason, json.dumps(tags), now, removed_by))

# ---- v6.1: audiobooks nobody has any more (Audiobookshelf items), deleted after LIBRARY_RELEASE_DAYS ----
def audio_release_note(item_id, title, now=None, removed_by=None):
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("INSERT INTO audio_release(item_id, title, since, status, updated, removed_by) VALUES(?,?,?,'waiting',?,?) "
                  "ON CONFLICT(item_id) DO UPDATE SET status='waiting', since=CASE WHEN audio_release.status='waiting' "
                  "THEN audio_release.since ELSE excluded.since END, title=excluded.title, updated=excluded.updated, "
                  "removed_by=excluded.removed_by",
                  (item_id, (title or "")[:300], now, now, removed_by))

def release_removed_by(kind, key):
    """v6.3: who removed a book ('book', calibre id) or audiobook ('audio', item id) that is counting down."""
    table, col = ("book_release", "calibre_id") if kind == "book" else ("audio_release", "item_id")
    with _conn() as c:
        r = c.execute(f"SELECT removed_by FROM {table} WHERE {col}=? AND status IN ('waiting','due')", (key,)).fetchone()
    return r[0] if r else None

def audio_releases(statuses=("waiting",)):
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM audio_release WHERE status IN ({marks}) ORDER BY since", tuple(statuses))]

def audio_release_waiting(item_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM audio_release WHERE item_id=? AND status='waiting'", (item_id,)).fetchone() is not None

def audio_release_set(item_id, status, error=None, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE audio_release SET status=?, attempts=attempts+?, last_error=?, updated=? WHERE item_id=?",
                  (status, 1 if error else 0, (error or "")[:300] or None, now or time.time(), item_id))

def release_waiting(calibre_id):
    """v6.1: is this book counting down to deletion (nobody has it, the file is still here)?"""
    with _conn() as c:
        return c.execute("SELECT 1 FROM book_release WHERE calibre_id=? AND status='waiting'",
                         (int(calibre_id),)).fetchone() is not None

def release_keep(calibre_id, now=None):
    """A reader has it again (a share, a re-request): the countdown stops."""
    with _lock, _conn() as c:
        c.execute("UPDATE book_release SET status='kept', updated=? WHERE calibre_id=? AND status IN ('waiting','due')",
                  (now or time.time(), int(calibre_id)))

def releases(statuses=("waiting", "due")):
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM book_release WHERE status IN ({marks}) ORDER BY since", tuple(statuses))]

def release_due(calibre_id, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE book_release SET status='due', updated=? WHERE calibre_id=? AND status='waiting'",
                  (now or time.time(), int(calibre_id)))

def release_result(calibre_id, ok, error=None, max_attempts=3):
    now = time.time()
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM book_release WHERE calibre_id=?", (int(calibre_id),)).fetchone()
        if not row:
            raise ValueError(f"no release for book {calibre_id}")
        attempts = (row["attempts"] or 0) + 1
        st = "deleted" if ok else ("kept" if (error or "").startswith("refused:") else
                                   "failed" if attempts >= max_attempts else "due")
        c.execute("UPDATE book_release SET status=?, attempts=?, last_error=?, updated=? WHERE calibre_id=?",
                  (st, attempts, None if ok else (error or "")[:300], now, int(calibre_id)))
        return dict(row, status=st)

def tag_push_open_for(rid):
    with _conn() as c:
        return c.execute("SELECT 1 FROM tag_push WHERE rid=? AND status IN ('pending','done')", (rid,)).fetchone() is not None

REPLACE_DAYS = 7

def open_replace(calibre_id, user, now=None):
    """Start looking for a better copy of a book; the live job's id (an existing one is kept)."""
    now = now or time.time()
    replace_live(calibre_id, now)                 # lets an expired window go first
    with _lock, _conn() as c:
        try:
            return c.execute("INSERT INTO replace_job(calibre_id, opened_by, created, updated) VALUES(?,?,?,?)",
                             (int(calibre_id), user, now, now)).lastrowid
        except sqlite3.IntegrityError:
            return c.execute("SELECT id FROM replace_job WHERE calibre_id=? AND status IN ('open','staged')",
                             (int(calibre_id),)).fetchone()[0]

def replace_live(calibre_id, now=None):
    """The open or staged job for this book, or None. An open window older than REPLACE_DAYS
    expires here."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("UPDATE replace_job SET status='expired', updated=? WHERE calibre_id=? AND status='open' "
                  "AND created < ?", (now, int(calibre_id), now - REPLACE_DAYS * 86400))
        r = c.execute("SELECT * FROM replace_job WHERE calibre_id=? AND status IN ('open','staged')",
                      (int(calibre_id),)).fetchone()
    return dict(r) if r else None

def replace_for_book(calibre_id):
    """The newest job of any status (the book page shows it)."""
    replace_live(calibre_id)
    with _conn() as c:
        r = c.execute("SELECT * FROM replace_job WHERE calibre_id=? ORDER BY id DESC LIMIT 1",
                      (int(calibre_id),)).fetchone()
    return dict(r) if r else None

def stage_replace(job_id, path, fmt, rid, owner, now=None):
    """A better copy arrived for an open job: hand it to the host. False if no longer open."""
    now = now or time.time()
    with _lock, _conn() as c:
        return c.execute("UPDATE replace_job SET status='staged', staged=?, fmt=?, rid=?, owner=?, updated=? "
                         "WHERE id=? AND status='open'", (path, fmt, rid, owner, now, job_id)).rowcount == 1

def cancel_replace(calibre_id, now=None):
    with _lock, _conn() as c:
        return c.execute("UPDATE replace_job SET status='cancelled', updated=? WHERE calibre_id=? AND status='open'",
                         (now or time.time(), int(calibre_id))).rowcount == 1

def pending_replaces(limit=10):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, calibre_id, fmt, rid, owner FROM replace_job WHERE status='staged' ORDER BY id LIMIT ?", (limit,))]

def replace_result(job_id, ok, error=None, max_attempts=3):
    now = time.time()
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM replace_job WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"no replace job {job_id}")
        attempts = (row["attempts"] or 0) + 1
        st = "done" if ok else ("failed" if attempts >= max_attempts or (error or "").startswith("refused:") else "staged")
        c.execute("UPDATE replace_job SET status=?, attempts=?, last_error=?, updated=? WHERE id=?",
                  (st, attempts, None if ok else (error or "")[:300], now, job_id))
        return dict(row, status=st)

def pending_tag_pushes(limit=50):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, calibre_id, rid, owner, share, op FROM tag_push WHERE status='pending' "
            "AND (not_before IS NULL OR not_before <= ?) ORDER BY id LIMIT ?", (time.time(), limit))]

def kobo_waits():
    """Removals held for a Kobo sync (v6.1)."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, calibre_id, owner, kobo_wait, not_before FROM tag_push WHERE status='pending' AND op='remove' "
            "AND kobo_wait IS NOT NULL AND not_before > ?", (time.time(),))]

def kobo_wait_over(push_id, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE tag_push SET not_before=?, updated=? WHERE id=?", (now or time.time(), now or time.time(), push_id))

def tag_push_result(push_id, ok, error=None, max_attempts=5):
    """Record the host's outcome. Success marks the request done; repeated failure gives up."""
    now = time.time()
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM tag_push WHERE id=?", (push_id,)).fetchone()
        if not row:
            raise ValueError(f"no tag job {push_id}")
        attempts = (row["attempts"] or 0) + 1
        st = "done" if ok else ("failed" if attempts >= max_attempts or (error or "").startswith("refused:") else "pending")
        c.execute("UPDATE tag_push SET status=?, attempts=?, last_error=?, updated=? WHERE id=?",
                  (st, attempts, None if ok else (error or "")[:300], now, push_id))
        return dict(row, status=st)


# ---- L21: Send-to-Kindle jobs ---------------------------------------------------------------
def kindle_enqueue(owner, is_admin, book_id, title, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        return c.execute("INSERT INTO kindle_jobs(owner, is_admin, book_id, title, next_try, created, updated) "
                         "VALUES(?,?,?,?,?,?,?)", (owner, 1 if is_admin else 0, book_id, title, now, now, now)).lastrowid

def kindle_due(now, limit=2):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM kindle_jobs WHERE status='queued' AND next_try <= ? ORDER BY id LIMIT ?", (now, limit))]

def kindle_update(jid, **f):
    f["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in f)
    with _lock, _conn() as c:
        c.execute(f"UPDATE kindle_jobs SET {cols} WHERE id=?", (*f.values(), jid))

def kindle_recent(owner=None, limit=10):
    sql = "SELECT * FROM kindle_jobs" + (" WHERE owner=?" if owner else "") + " ORDER BY id DESC LIMIT ?"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, ((owner,) if owner else ()) + (limit,))]


def linked_calibre_ids():
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT calibre_id FROM requests WHERE calibre_id IS NOT NULL")}


def work_isbn13(work_id):
    with _conn() as c:
        r = c.execute("SELECT value FROM meta_identifier WHERE scope='work' AND target_id=? AND kind='isbn13' "
                      "ORDER BY exact DESC LIMIT 1", (work_id,)).fetchone()
    return r["value"] if r else None


# ---- on-demand conversion ------------------------------------------------------------------
def convert_queue(calibre_id, owner, src_fmt, dst_fmt, src_path, now=None):
    now = now or time.time()
    try:
        with _lock, _conn() as c:
            return c.execute("INSERT INTO convert_jobs(calibre_id, owner, src_fmt, dst_fmt, src_path, created, updated) "
                             "VALUES(?,?,?,?,?,?,?)", (int(calibre_id), owner, src_fmt, dst_fmt, src_path, now, now)).lastrowid
    except sqlite3.IntegrityError:
        return None                                # already converting to that format

def convert_count(owner, since):
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM convert_jobs WHERE owner=? AND created>=?", (owner, since)).fetchone()[0]

def convert_for_book(calibre_id):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM convert_jobs WHERE calibre_id=? ORDER BY id DESC LIMIT 10",
                                           (calibre_id,))]

def pending_converts(limit=3):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT id, calibre_id, owner, src_fmt, dst_fmt, src_path FROM convert_jobs "
                                           "WHERE status='pending' ORDER BY id LIMIT ?", (limit,))]

def convert_result(job_id, ok, detail="", max_attempts=2):
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM convert_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"no conversion job {job_id}")
        attempts = (row["attempts"] or 0) + 1
        st = "done" if ok else ("failed" if attempts >= max_attempts else "pending")
        c.execute("UPDATE convert_jobs SET status=?, attempts=?, detail=?, updated=? WHERE id=?",
                  (st, attempts, (detail or "")[:300], time.time(), job_id))
        return dict(row, status=st)

# ---- L08: synthetic canary journey --------------------------------------------------------
CANARY_KEEP = 200

def canary_record(run):
    """One run of scripts/synthetic.py: {ok, secs, import_secs, steps:[{name, ok, secs, note}]}."""
    steps = [{"name": str(x.get("name", ""))[:60], "ok": bool(x.get("ok")),
              "secs": round(float(x.get("secs") or 0), 1), "note": str(x.get("note") or "")[:200]}
             for x in (run.get("steps") or [])][:40]
    failed = next((x["name"] for x in steps if not x["ok"]), None)
    ok = bool(run.get("ok")) and failed is None
    imp = run.get("import_secs")
    with _lock, _conn() as c:
        rid = c.execute("INSERT INTO canary_runs(ts, ok, secs, import_secs, failed, steps) VALUES(?,?,?,?,?,?)",
                        (float(run.get("ts") or time.time()), int(ok), round(float(run.get("secs") or 0), 1),
                         None if imp is None else round(float(imp), 1), failed, json.dumps(steps))).lastrowid
        c.execute("DELETE FROM canary_runs WHERE id <= ?", (rid - CANARY_KEEP,))
    return {"id": rid, "ok": ok, "failed": failed}

def canary_recent(n=10):
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM canary_runs ORDER BY id DESC LIMIT ?", (int(n),))]
    for r in rows:
        r["steps"] = json.loads(r["steps"] or "[]")
        r["ok"] = bool(r["ok"])
    return rows

# ---- L05: password sync into the Authelia gate ----------------------------------------------
GATE_ROUNDS = 310000          # PBKDF2-SHA512, OWASP 2023; Authelia verifies it (measured on 4.39.28)

def gate_hash(password, salt=None):
    """Authelia's `$pbkdf2-sha512$<rounds>$<salt>$<key>` (passlib's adapted base64)."""
    import base64, hashlib, os as _os
    ab64 = lambda b: base64.b64encode(b).decode().rstrip("=").replace("+", ".")
    salt = salt or _os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, GATE_ROUNDS, 64)
    return f"$pbkdf2-sha512${GATE_ROUNDS}${ab64(salt)}${ab64(key)}"

def gate_flag_path():
    import os as _os
    return _os.path.join(_os.path.dirname(config.STATE_DB), "gate-sync.flag")

def gate_queue(user, password, email=None, display=None, admin=False):
    """Queue a password (and, with an e-mail, a whole new login) for the gate; touch the flag the
    host's path unit watches so it lands within seconds."""
    h = gate_hash(password)
    with _lock, _conn() as c:
        # a password change right after the account was created must not drop the pending
        # creation's e-mail (the gate login could then never be made): newest hash, kept details
        c.execute("INSERT INTO gate_pw(user, hash, email, display, admin, created, attempts, detail) "
                  "VALUES(?,?,?,?,?,?,0,NULL) ON CONFLICT(user) DO UPDATE SET hash=excluded.hash, "
                  "email=COALESCE(excluded.email, gate_pw.email), display=COALESCE(excluded.display, gate_pw.display), "
                  "admin=MAX(excluded.admin, gate_pw.admin), created=excluded.created, attempts=0, detail=NULL",
                  (user, h, email or None, display or None, int(bool(admin)), time.time()))
    try:
        with open(gate_flag_path(), "a"):
            pass
        import os as _os
        _os.utime(gate_flag_path(), None)
    except OSError:
        pass                                   # the host's 10-minute timer still picks it up
    return h

def gate_pending():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT user, hash, email, display, admin, created, attempts "
                                            "FROM gate_pw ORDER BY created")]

def gate_done(user, outcome, detail=""):
    """ok -> the row goes; missing/failed -> kept for the admin to see, retried a few times."""
    with _lock, _conn() as c:
        if outcome == "ok":
            c.execute("DELETE FROM gate_pw WHERE user=?", (user,))
            return "applied"
        c.execute("UPDATE gate_pw SET attempts=attempts+1, detail=? WHERE user=?", (detail[:200] or outcome, user))
        row = c.execute("SELECT attempts FROM gate_pw WHERE user=?", (user,)).fetchone()
        if row and row[0] >= 5:
            c.execute("DELETE FROM gate_pw WHERE user=?", (user,))
            return "dropped"
        return "kept"



# ---- comics (docs/COMICS.md) --------------------------------------------------------------------
COMIC_OPEN = ("queued", "pending", "confirm", "downloading", "held")
COMIC_JSON = {"tried": [], "blocked": [], "reasons": [], "candidate": None, "held_meta": None}
COMIC_FIELDS = ("provider", "series_id", "series_name", "kind", "reading", "strip", "number", "label", "year",
                "publisher", "language", "cover", "authors", "summary", "unit")

def _comic(r):
    if not r:
        return None
    d = dict(r)
    for k, empty in COMIC_JSON.items():
        d[k] = json.loads(d[k]) if d.get(k) else empty
    d["authors"] = json.loads(d["authors"]) if d.get("authors") else []
    return d

def comic_add(owner, fields, now=None):
    """A reader's request for one issue or volume. An open request for the same one is returned
    instead of a second (id, created?)."""
    now = now or time.time()
    f = {k: fields.get(k) for k in COMIC_FIELDS if fields.get(k) is not None}   # NOT NULL columns keep their defaults
    f["authors"] = json.dumps(fields.get("authors") or [])
    with _lock, _conn() as c:
        r = c.execute(f"SELECT id FROM comic_requests WHERE owner=? AND provider=? AND series_id=? AND number=? "
                      f"AND coalesce(unit,'')=? AND status IN ({','.join('?' * len(COMIC_OPEN))})",
                      (owner, f.get("provider"), f.get("series_id"), f.get("number"), f.get("unit") or "", *COMIC_OPEN)).fetchone()
        if r:
            return r["id"], False
        cols = ", ".join(f)
        rid = c.execute(f"INSERT INTO comic_requests(owner, {cols}, status, next_try, created, updated) "
                        f"VALUES(?, {','.join('?' * len(f))}, 'queued', ?, ?, ?)",
                        (owner, *f.values(), now, now, now)).lastrowid
        return rid, True

def comic_get(rid):
    with _conn() as c:
        return _comic(c.execute("SELECT * FROM comic_requests WHERE id=?", (rid,)).fetchone())

def downloads_in_flight(exclude_book=None, exclude_comic=None):
    """Bytes of the large downloads under way (audiobooks, comics): Shelfmark is fetching them,
    or they have arrived and are not imported yet. v6.0.1: a new one starts only when the disk
    has room beside these, so two readers' omnibuses never both start on space for one."""
    with _conn() as c:
        b = c.execute("SELECT coalesce(sum(size_bytes), 0) FROM book_requests WHERE status='downloading' "
                      "AND size_bytes IS NOT NULL AND id IS NOT ?", (exclude_book,)).fetchone()[0]
        m = c.execute("SELECT coalesce(sum(size_bytes), 0) FROM comic_requests WHERE status='downloading' "
                      "AND size_bytes IS NOT NULL AND id IS NOT ?", (exclude_comic,)).fetchone()[0]
    return int(b or 0) + int(m or 0)

def comic_update(rid, **f):
    for k in COMIC_JSON:
        if k in f and f[k] is not None:
            f[k] = json.dumps(f[k])
    f["updated"] = time.time()
    with _lock, _conn() as c:
        c.execute(f"UPDATE comic_requests SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), rid))

def comic_due(now, limit=3):
    with _conn() as c:
        return [_comic(r) for r in c.execute(
            "SELECT * FROM comic_requests WHERE status='queued' AND (next_try IS NULL OR next_try <= ?) "
            "ORDER BY next_try, id LIMIT ?", (now, limit))]

def comic_open(owner=None, statuses=COMIC_OPEN):
    sql = f"SELECT * FROM comic_requests WHERE status IN ({','.join('?' * len(statuses))})"
    args = list(statuses)
    if owner:
        sql += " AND owner=?"
        args.append(owner)
    with _conn() as c:
        return [_comic(r) for r in c.execute(sql + " ORDER BY id", args)]

def comic_list(owner=None, limit=200, closed_days=30, now=None):
    now = now or time.time()
    sql = f"SELECT * FROM comic_requests WHERE (status IN ({','.join('?' * len(COMIC_OPEN))}) OR updated >= ?)"
    args = [*COMIC_OPEN, now - closed_days * 86400]
    if owner:
        sql += " AND owner=?"
        args.append(owner)
    with _conn() as c:
        return [_comic(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]

def comic_for_book(owner, calibre_id):
    """This reader's delivered comic request for one Calibre book (for 'Wrong comic'), or None."""
    with _conn() as c:
        return _comic(c.execute("SELECT * FROM comic_requests WHERE owner=? AND calibre_id=? AND status='done' "
                                "ORDER BY id DESC", (owner, calibre_id)).fetchone())

def comic_waiting(owner):
    """How many of this reader's comics wait for them: a copy to confirm, a file to check, or
    chapters a volume replaced."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM comic_requests WHERE owner=? AND status IN ('confirm','held')",
                         (owner,)).fetchone()[0] + \
            c.execute("SELECT COUNT(*) FROM comic_swaps WHERE owner=? AND status='offered'", (owner,)).fetchone()[0]

def comic_swap_add(owner, request_id, series_name, volume, volume_book, chapters, known, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO comic_swaps(owner, request_id, series_name, volume, volume_book, chapters, known, "
                        "created, updated) VALUES(?,?,?,?,?,?,?,?,?)", (owner, request_id, series_name, str(volume), volume_book,
                                                                     json.dumps(chapters), 1 if known else 0, now, now))
        return cur.lastrowid if cur.rowcount else None

def _swap(r):
    if not r:
        return None
    d = dict(r)
    d["chapters"] = json.loads(d["chapters"] or "[]")
    return d

def comic_swap_get(sid):
    with _conn() as c:
        return _swap(c.execute("SELECT * FROM comic_swaps WHERE id=?", (sid,)).fetchone())

def comic_swaps_offered(owner):
    with _conn() as c:
        return [_swap(r) for r in c.execute("SELECT * FROM comic_swaps WHERE owner=? AND status='offered' ORDER BY id", (owner,))]

def comic_swap_exists(owner, request_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM comic_swaps WHERE owner=? AND request_id=?", (owner, request_id)).fetchone() is not None

def comic_swap_set(sid, status):
    with _lock, _conn() as c:
        c.execute("UPDATE comic_swaps SET status=?, updated=? WHERE id=?", (status, time.time(), sid))

def comic_count_open(owner):
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM comic_requests WHERE owner=? AND status IN "
                         f"({','.join('?' * len(COMIC_OPEN))})", (owner, *COMIC_OPEN)).fetchone()[0]

def comic_series_status(owner, provider, series_id):
    """{number: status} of this reader's requests in one series (the newest per number)."""
    with _conn() as c:
        rows = c.execute("SELECT number, status FROM comic_requests WHERE owner=? AND provider=? AND series_id=? "
                         "AND coalesce(unit,'')='' ORDER BY id", (owner, provider, str(series_id))).fetchall()
    return {r["number"]: r["status"] for r in rows}

# which comics have their Kobo copy (the host job asks, converts, reports)
def comic_convert_state(calibre_ids):
    if not calibre_ids:
        return {}
    ids = list(calibre_ids)
    with _conn() as c:
        return {r["calibre_id"]: dict(r) for r in c.execute(
            f"SELECT * FROM comic_convert WHERE calibre_id IN ({','.join('?' * len(ids))})", ids)}

def comic_convert_force(calibre_id, now=None, remake=False):
    """'Make Kobo copy' now; remake (v6.1): a new one REPLACES the Kobo copy there is."""
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("INSERT INTO comic_convert(calibre_id, status, forced, attempts, next_try, updated, remake) "
                  "VALUES(?, 'due', 1, 0, ?, ?, ?) ON CONFLICT(calibre_id) DO UPDATE SET "
                  "status='due', forced=1, attempts=0, next_try=excluded.next_try, updated=excluded.updated, "
                  "remake=excluded.remake", (calibre_id, now, now, 1 if remake else 0))

def comic_convert_result(calibre_id, ok, detail=None, now=None, max_attempts=3, final=False, made=None):
    """ok: done. A failure is tried again after 1 h, then 6 h, and left 'failed' after three.
    final (v6.0.1): a failure another try cannot change (too large for a Kobo) is 'failed' at once.
    made (v6.2): what the copy was made for ({profile, colour, layout}), kept with a success."""
    now = now or time.time()
    with _lock, _conn() as c:
        r = c.execute("SELECT attempts FROM comic_convert WHERE calibre_id=?", (calibre_id,)).fetchone()
        attempts = max_attempts if (final and not ok) else (r["attempts"] if r else 0) + (0 if ok else 1)
        status = "done" if ok else ("failed" if attempts >= max_attempts else "due")
        nxt = None if ok else now + (3600 if attempts == 1 else 6 * 3600)
        # forced (a reader's 'Make Kobo copy') is spent once the copy is made or given up on
        c.execute("INSERT INTO comic_convert(calibre_id, status, attempts, next_try, detail, updated) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(calibre_id) DO UPDATE SET status=excluded.status, "
                  "attempts=excluded.attempts, next_try=excluded.next_try, detail=excluded.detail, "
                  "updated=excluded.updated, forced=CASE WHEN excluded.status='due' THEN forced ELSE 0 END, "
                  "remake=CASE WHEN excluded.status='due' THEN remake ELSE 0 END",
                  (calibre_id, status, attempts, nxt, (detail or "")[:300], now))
        if ok and made:
            c.execute("UPDATE comic_convert SET made=? WHERE calibre_id=?", (json.dumps(made), calibre_id))
        return status

def comic_layout_get(calibre_id):
    with _conn() as c:
        r = c.execute("SELECT layout FROM comic_layout WHERE calibre_id=?", (calibre_id,)).fetchone()
    return r["layout"] if r else None

def comic_layout_set(calibre_id, layout, owner=None):
    """v6.2: a reader's choice of layout for a comic ('auto' clears it: from its pages again)."""
    with _lock, _conn() as c:
        if layout == "auto":
            c.execute("DELETE FROM comic_layout WHERE calibre_id=?", (calibre_id,))
        else:
            c.execute("INSERT INTO comic_layout(calibre_id, layout, owner, updated) VALUES(?,?,?,?) "
                      "ON CONFLICT(calibre_id) DO UPDATE SET layout=excluded.layout, owner=excluded.owner, "
                      "updated=excluded.updated", (calibre_id, layout, owner, time.time()))

def kindle_comic_jobs(status, limit=5):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM kindle_jobs WHERE kind='comic' AND status=? ORDER BY id LIMIT ?", (status, limit))]


# ---- following (v5.8, follows.py) --------------------------------------------------------------
def _follow(r):
    if not r:
        return None
    d = dict(r)
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    d["known"] = json.loads(d["known"]) if d.get("known") else None
    return d

def follow_add(owner, kind, provider, key, name, extra=None, now=None):
    """(id, created?). Checked for the first time within minutes (that check only records
    what is already out)."""
    now = now or time.time()
    with _lock, _conn() as c:
        r = c.execute("SELECT id FROM follows WHERE owner=? AND kind=? AND provider=? AND key=?",
                      (owner, kind, provider, str(key))).fetchone()
        if r:
            return r["id"], False
        fid = c.execute("INSERT INTO follows(owner, kind, provider, key, name, extra, next_check, created) "
                        "VALUES(?,?,?,?,?,?,?,?)", (owner, kind, provider, str(key), name[:200],
                                                    json.dumps(extra or {}), now, now)).lastrowid
        return fid, True

def follow_get(fid):
    with _conn() as c:
        return _follow(c.execute("SELECT * FROM follows WHERE id=?", (fid,)).fetchone())

def follow_find(owner, kind, provider, key):
    with _conn() as c:
        return _follow(c.execute("SELECT * FROM follows WHERE owner=? AND kind=? AND provider=? AND key=?",
                                 (owner, kind, provider, str(key))).fetchone())

def follow_list(owner=None):
    sql, args = "SELECT * FROM follows", ()
    if owner:
        sql, args = sql + " WHERE owner=?", (owner,)
    with _conn() as c:
        return [_follow(r) for r in c.execute(sql + " ORDER BY name COLLATE NOCASE", args)]

def follow_due(now, limit=10):
    with _conn() as c:
        return [_follow(r) for r in c.execute(
            "SELECT * FROM follows WHERE next_check IS NULL OR next_check <= ? ORDER BY next_check LIMIT ?", (now, limit))]

def follow_checked(fid, known, next_check, detail=None, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE follows SET known=?, checked=?, next_check=?, detail=? WHERE id=?",
                  (json.dumps(known) if known is not None else None, now or time.time(), next_check, detail, fid))

def follow_retry(fid, next_check, detail):
    with _lock, _conn() as c:
        c.execute("UPDATE follows SET next_check=?, detail=? WHERE id=?", (next_check, (detail or "")[:300], fid))

def follow_remove(fid, owner):
    with _lock, _conn() as c:
        c.execute("DELETE FROM notices WHERE follow_id=? AND owner=? AND status='new'", (fid, owner))
        return c.execute("DELETE FROM follows WHERE id=? AND owner=?", (fid, owner)).rowcount

def notice_add(owner, follow_id, item_key, title, detail, item, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO notices(owner, follow_id, item_key, title, detail, item, created, updated) "
                        "VALUES(?,?,?,?,?,?,?,?)", (owner, follow_id, str(item_key), title[:300], (detail or "")[:500],
                                                     json.dumps(item or {}), now, now))
        return cur.lastrowid if cur.rowcount else None

def _notice(r):
    if not r:
        return None
    d = dict(r)
    d["item"] = json.loads(d["item"]) if d.get("item") else {}
    return d

def notices(owner, statuses=("new",), limit=50):
    with _conn() as c:
        return [_notice(r) for r in c.execute(
            f"SELECT * FROM notices WHERE owner=? AND status IN ({','.join('?' * len(statuses))}) "
            f"ORDER BY created DESC, id DESC LIMIT ?", (owner, *statuses, limit))]

def notice_get(nid):
    with _conn() as c:
        return _notice(c.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone())

def notice_set(nid, status):
    with _lock, _conn() as c:
        c.execute("UPDATE notices SET status=?, updated=? WHERE id=?", (status, time.time(), nid))

def notices_unmailed():
    with _conn() as c:
        return [_notice(r) for r in c.execute(
            "SELECT * FROM notices WHERE status='new' AND mailed=0 ORDER BY owner, created")]

def notices_mailed(ids):
    if not ids:
        return
    with _lock, _conn() as c:
        c.execute(f"UPDATE notices SET mailed=1 WHERE id IN ({','.join('?' * len(ids))})", list(ids))

def notices_since(since):
    with _conn() as c:
        return c.execute("SELECT COUNT(*), COUNT(DISTINCT owner) FROM notices WHERE created >= ?", (since,)).fetchone()

# ---- one-tap book requests (v5.8.3, bookreq.py) --------------------------------------------------
BOOK_OPEN = ("queued", "pending", "confirm", "downloading", "held")
BOOK_FIELDS = ("title", "author", "series", "language", "hardcover_id", "notice_id", "kind", "ask")

BOOK_JSON = {"tried": [], "blocked": [], "reasons": [], "candidate": None, "held_meta": None}

def _bookreq(r):
    if not r:
        return None
    d = dict(r)
    for k, empty in BOOK_JSON.items():
        d[k] = json.loads(d[k]) if d.get(k) else empty
    return d

def bookreq_find_open(owner, title, author):
    """This reader's open request for the same book (title and author, as asked), or None."""
    with _conn() as c:
        return _bookreq(c.execute(
            f"SELECT * FROM book_requests WHERE owner=? AND lower(title)=lower(?) AND lower(coalesce(author,''))=lower(?) "
            f"AND status IN ({','.join('?' * len(BOOK_OPEN))}) ORDER BY id DESC",
            (owner, title, author or "", *BOOK_OPEN)).fetchone())

def bookreq_add(owner, fields, now=None):
    """(id, created?): an open request for the same book is returned instead of a second."""
    now = now or time.time()
    f = {k: fields.get(k) for k in BOOK_FIELDS if fields.get(k) is not None}
    with _lock, _conn() as c:
        r = c.execute(f"SELECT id FROM book_requests WHERE owner=? AND lower(title)=lower(?) "
                      f"AND lower(coalesce(author,''))=lower(?) AND coalesce(kind,'ebook')=? "
                      f"AND status IN ({','.join('?' * len(BOOK_OPEN))})",
                      (owner, f.get("title") or "", f.get("author") or "", f.get("kind") or "ebook", *BOOK_OPEN)).fetchone()
        if r:
            return r["id"], False
        cols = ", ".join(f)
        rid = c.execute(f"INSERT INTO book_requests(owner, {cols}, status, next_try, created, updated) "
                        f"VALUES(?, {','.join('?' * len(f))}, 'queued', ?, ?, ?)",
                        (owner, *f.values(), now, now, now)).lastrowid
        return rid, True

def bookreq_get(rid):
    with _conn() as c:
        return _bookreq(c.execute("SELECT * FROM book_requests WHERE id=?", (rid,)).fetchone())

def bookreq_update(rid, **f):
    for k in BOOK_JSON:
        if k in f and f[k] is not None:
            f[k] = json.dumps(f[k])
    f["updated"] = time.time()
    with _lock, _conn() as c:
        c.execute(f"UPDATE book_requests SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), rid))

def bookreq_due(now, limit=2):
    with _conn() as c:
        return [_bookreq(r) for r in c.execute(
            "SELECT * FROM book_requests WHERE status='queued' AND (next_try IS NULL OR next_try <= ?) "
            "ORDER BY next_try, id LIMIT ?", (now, limit))]

def bookreq_open(statuses=BOOK_OPEN, owner=None):
    sql = f"SELECT * FROM book_requests WHERE status IN ({','.join('?' * len(statuses))})"
    args = list(statuses)
    if owner:
        sql += " AND owner=?"
        args.append(owner)
    with _conn() as c:
        return [_bookreq(r) for r in c.execute(sql + " ORDER BY id", args)]

def bookreq_list(owner=None, limit=100, closed_days=30, now=None):
    """Open requests and those closed in the last month; everyone's when owner is None (admin)."""
    now = now or time.time()
    sql = f"SELECT * FROM book_requests WHERE (status IN ({','.join('?' * len(BOOK_OPEN))}) OR updated >= ?)"
    args = [*BOOK_OPEN, now - closed_days * 86400]
    if owner:
        sql += " AND owner=?"
        args.append(owner)
    with _conn() as c:
        return [_bookreq(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]

def bookreq_for_book(owner, calibre_id):
    """This reader's delivered request for one Calibre book (for 'Wrong book'), or None."""
    with _conn() as c:
        return _bookreq(c.execute("SELECT * FROM book_requests WHERE owner=? AND calibre_id=? AND status='done' "
                                  "ORDER BY id DESC", (owner, calibre_id)).fetchone())

def bookreq_for_audio(owner, item_id):
    """This reader's delivered audiobook request for one Audiobookshelf item ('Wrong audiobook')."""
    with _conn() as c:
        return _bookreq(c.execute("SELECT * FROM book_requests WHERE owner=? AND abs_item=? AND status='done' "
                                  "ORDER BY id DESC", (owner, item_id)).fetchone())

def bookreq_waiting(owner):
    """How many of this reader's books wait for them: a copy to confirm, or a file to check."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM book_requests WHERE owner=? AND status IN ('confirm','held')",
                         (owner,)).fetchone()[0]

def bookreq_count_open(owner):
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM book_requests WHERE owner=? AND status IN "
                         f"({','.join('?' * len(BOOK_OPEN))})", (owner, *BOOK_OPEN)).fetchone()[0]

# ---- audiobooks to Hardcover (v5.9.1, hcaudio.py) ---------------------------------------------------
def hc_audio_get(owner, item_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM hc_audio WHERE owner=? AND item_id=?", (owner, item_id)).fetchone()
    return dict(r) if r else None

def hc_audio_put(owner, item_id, **f):
    f["at"] = time.time()
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO hc_audio(owner, item_id) VALUES(?,?)", (owner, item_id))
        c.execute(f"UPDATE hc_audio SET {', '.join(f'{k}=?' for k in f)} WHERE owner=? AND item_id=?", (*f.values(), owner, item_id))

def hc_audio_note(owner, detail):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO hc_audio_state(owner, detail, at) VALUES(?,?,?)", (owner, (detail or "")[:300], time.time()))

def hc_audio_state(owner):
    with _conn() as c:
        r = c.execute("SELECT * FROM hc_audio_state WHERE owner=?", (owner,)).fetchone()
    return dict(r) if r else None

# ---- send to an e-reader (v5.9.1, sendcode.py) -----------------------------------------------------
def send_code_new(code, secret, device, now=None):
    now = now or time.time()
    with _lock, _conn() as c:
        c.execute("DELETE FROM send_codes WHERE created < ?", (now - 3600,))
        c.execute("INSERT INTO send_codes(code, secret, device, created) VALUES(?,?,?,?)", (code, secret, device, now))

def send_code_get(code):
    with _conn() as c:
        r = c.execute("SELECT * FROM send_codes WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None

def send_code_by_secret(secret):
    with _conn() as c:
        r = c.execute("SELECT * FROM send_codes WHERE secret=?", (secret,)).fetchone()
    return dict(r) if r else None

def send_code_attach(code, owner, book_id, fmt, now=None):
    """True when the code was free and is now this book's (one book per code)."""
    with _lock, _conn() as c:
        return c.execute("UPDATE send_codes SET owner=?, book_id=?, fmt=?, attached=? WHERE code=? AND book_id IS NULL",
                         (owner, book_id, fmt, now or time.time(), code)).rowcount == 1

def send_code_fetched(code, now=None):
    with _lock, _conn() as c:
        c.execute("UPDATE send_codes SET fetched=coalesce(fetched, ?) WHERE code=?", (now or time.time(), code))

# ---- Metron (v5.9, metrontrack.py) ---------------------------------------------------------------
def metron_get(owner):
    with _conn() as c:
        r = c.execute("SELECT * FROM metron_link WHERE owner=?", (owner,)).fetchone()
    return dict(r) if r else None

def metron_set(owner, username, secret, method, now=None):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO metron_link(owner, username, secret, method, connected, detail) VALUES(?,?,?,?,?,NULL)",
                  (owner, username[:100], secret[:300], method, now or time.time()))

def metron_note(owner, detail):
    with _lock, _conn() as c:
        c.execute("UPDATE metron_link SET detail=? WHERE owner=?", ((detail or "")[:300], owner))

def metron_remove(owner):
    with _lock, _conn() as c:
        c.execute("DELETE FROM metron_link WHERE owner=?", (owner,))
        c.execute("DELETE FROM metron_sent WHERE owner=?", (owner,))

def metron_all():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM metron_link ORDER BY owner")]

def metron_was_sent(owner, issue_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM metron_sent WHERE owner=? AND issue_id=?", (owner, int(issue_id))).fetchone() is not None

def metron_sent_set(owner, issue_id, now=None):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO metron_sent(owner, issue_id, at) VALUES(?,?,?)", (owner, int(issue_id), now or time.time()))

# ---- AniList (v5.8, anilist.py) ------------------------------------------------------------------
def anilist_get(owner):
    with _conn() as c:
        r = c.execute("SELECT * FROM anilist WHERE owner=?", (owner,)).fetchone()
        return dict(r) if r else None

def anilist_set(owner, token, al_user_id, al_name, now=None):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO anilist(owner, token, al_user_id, al_name, connected, detail) "
                  "VALUES(?,?,?,?,?,NULL)", (owner, token, al_user_id, al_name, now or time.time()))

def anilist_note(owner, detail):
    with _lock, _conn() as c:
        c.execute("UPDATE anilist SET detail=? WHERE owner=?", ((detail or "")[:300], owner))

def anilist_remove(owner):
    with _lock, _conn() as c:
        c.execute("DELETE FROM anilist WHERE owner=?", (owner,))
        c.execute("DELETE FROM anilist_progress WHERE owner=?", (owner,))

def anilist_all():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM anilist ORDER BY owner")]

def anilist_media_get(series):
    with _conn() as c:
        r = c.execute("SELECT * FROM anilist_media WHERE series=?", (series,)).fetchone()
        return dict(r) if r else None

def anilist_media_put(series, media_id, title, volumes, status):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO anilist_media(series, media_id, title, volumes, status, at) VALUES(?,?,?,?,?,?)",
                  (series, media_id, title, volumes, status, time.time()))

def anilist_sent(owner, media_id):
    with _conn() as c:
        r = c.execute("SELECT volumes FROM anilist_progress WHERE owner=? AND media_id=?", (owner, media_id)).fetchone()
        return r["volumes"] if r else 0

def anilist_sent_set(owner, media_id, volumes):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO anilist_progress(owner, media_id, volumes, at) VALUES(?,?,?,?)",
                  (owner, media_id, volumes, time.time()))
