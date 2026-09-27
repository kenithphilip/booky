"""Round-4 review regressions (the fan-out hunters' findings on the round-4 fixes).

These are mostly fail-open and fail-closed properties rather than features: an app.db that is
busy must not read as "that account is gone", a SIGKILL mid-import must not leave a full-size
copy nobody owns, an unlimited page must not multiply threads, a rejected credential must be a
401 and not a traceback, and the page must not promise a limit the code does not keep.
"""
import os, sqlite3, threading, time
import pytest
import config, db, cwa, kindle, worker, fetchers
from conftest import add_calibre_book, login, post


# ------------------------------------------------ a half-placed audiobook is an orphan
def _incoming(name, mb=1):
    d = os.path.join(config.AUDIO_DIR, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "part1.m4b"), "wb") as f:
        f.write(b"\0" * (mb * 1024 * 1024))
    return d


def test_sweep_orphans_reaps_a_half_placed_audiobook():
    """_place_audio_dir renames the reader's folder into library/audiobooks/.incoming-<uuid>
    and only renames it out when the book is complete; the put-it-back cleanup is in an
    `except BaseException`, which a SIGKILL (OOM killer, stop_grace_period, the 04:30 reboot)
    never runs. Nothing swept this shape: the leak was invisible to Audiobookshelf, to the
    request rows, to /admin and to the self-test, and visible only to restic."""
    ours = _incoming(".incoming-" + "a" * 32, mb=2)
    second = _incoming(".incoming-" + "b" * 32)         # the retry's copy, from the next kill
    assert worker.sweep_orphans() >= 2
    assert not os.path.exists(ours) and not os.path.exists(second)


def test_the_sweep_touches_nothing_but_its_own_shape():
    """Same discipline as the .part / .uploading globs: only names this worker makes."""
    book = os.path.join(config.AUDIO_DIR, "Some Author", "Some Book")
    os.makedirs(book)
    open(os.path.join(book, "01.mp3"), "wb").close()
    keep = [_incoming(".incoming-notours"),             # right prefix, wrong shape
            _incoming(".incoming-" + "a" * 31),         # 31 hex digits
            _incoming(".incoming-" + "A" * 32),         # uppercase: uuid4().hex never is
            _incoming("incoming-" + "a" * 32)]          # no leading dot
    mine = _incoming(".incoming-" + "c" * 32)
    worker.sweep_orphans()
    assert not os.path.exists(mine)
    for d in keep + [book]:
        assert os.path.isdir(d), d


# ------------------------------------------------ a busy app.db is not a missing user
class _LockedConn:
    """Opens fine, then every statement raises — SQLITE_BUSY after the 30 s timeout, or
    'unable to open database file' when the WAL sidecars cannot be created."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def locked_db(monkeypatch):
    monkeypatch.setattr(cwa, "_conn", lambda: _LockedConn())


def test_a_busy_app_db_raises_cwa_unavailable_not_a_raw_sqlite_error(locked_db):
    """_conn() raised CwaError only for a MISSING file, so every other sqlite3 error escaped
    the module raw and sailed past every `except cwa.CwaError` guard in the portal."""
    with pytest.raises(cwa.CwaUnavailable):
        cwa.get_user("alice")
    for call in (cwa.list_users, lambda: cwa.kobo_url("alice", create=False),
                 lambda: cwa.set_kindle_mail("alice", "a@kindle.com"),
                 lambda: cwa.set_password("alice", "newpass12"),
                 cwa.kobo_sync_enabled, cwa.disable_public_registration):
        with pytest.raises(cwa.CwaError):          # the class every guard already catches
            call()


def test_a_busy_app_db_does_not_fail_a_queued_request(locked_db):
    """_owner_gone's own docstring: 'an unreadable app.db is not a reason to fail a request'.
    It honoured that for CwaError and not for the error that actually happens, so a transient
    lock reached _process's `except Exception` and wrote a permanent error row — plus a 'could
    not be added' mail — for a request that only needed retrying."""
    assert worker._owner_gone("alice") is False
    assert worker._cwa_user("alice") is None
    assert worker._is_admin("alice") is False


def test_a_busy_app_db_degrades_the_pages_instead_of_500ing(client, users, locked_db):
    """A bare 500 on /library, /upload, /devices and /admin while /login still works (auth.py
    is the one module that catches bare Exception) is the most confusing failure an admin can
    be handed."""
    import app as appmod
    assert appmod._cwa_user("alice") == {}
    login(client, "alice", users["alice"])          # auth.py reads app.db itself, read-only
    for path in ("/library", "/upload", "/devices"):
        assert client.get(path).status_code == 200, path


# ------------------------------------------------ CWA's header login is pinned off
def test_harden_pins_reverse_proxy_header_login_off(users):
    """Off in the shipped image, so this is a pin, not a repair — but ON it is a full auth
    bypass on books.<domain>: CWA loads the user from the header before any blueprint runs, the
    header NAME is an admin-chosen setting, and Caddy strips only a fixed list of names."""
    with sqlite3.connect(config.CWA_DB) as c:
        c.execute("UPDATE settings SET config_allow_reverse_proxy_header_login=1, "
                  "config_public_reg=1, config_anonbrowse=1, config_remote_login=1")
    assert cwa.disable_public_registration() is True
    with sqlite3.connect(config.CWA_DB) as c:
        row = c.execute("SELECT config_allow_reverse_proxy_header_login, config_public_reg, "
                        "config_anonbrowse, config_remote_login FROM settings").fetchone()
    assert row == (0, 0, 0, 0)
    assert cwa.disable_public_registration() is False        # idempotent, as before


# ------------------------------------------------ the search page shares one thread budget
def test_search_and_enrichment_never_build_a_pool_per_request(monkeypatch):
    """GET / is the only authenticated route with no rate limit in front of it, and it used to
    construct a ThreadPoolExecutor in fetchers.search plus a second one in app.index, both
    released with shutdown(wait=False) — which cancels queued work but not work already
    running. A reader on refresh therefore multiplied detached threads and outbound sockets at
    up to 11 per request, on the single gunicorn worker that also runs the queue, dropbox and
    housekeeping loops."""
    def _refuse(*a, **kw):
        raise AssertionError("a ThreadPoolExecutor was constructed during a request")

    monkeypatch.setattr(fetchers, "ThreadPoolExecutor", _refuse)
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True, "librivox": True})
    monkeypatch.setattr(fetchers, "_ADAPTERS",
                        {"gutenberg": lambda q, **kw: [{"source": "gutenberg", "title": q,
                                                        "author": "A", "download_url": "u"}],
                         "librivox": lambda q, **kw: []})
    first, second = fetchers._SEARCH, fetchers._DETAIL
    for q in ("dickens", "frankenstein", "moby"):
        assert fetchers.search(q)
    assert fetchers._SEARCH is first and fetchers._DETAIL is second


def test_the_shared_pools_are_bounded_however_many_searches_run(monkeypatch):
    """The point of one pool is the ceiling: concurrent searches queue instead of spawning."""
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True})
    monkeypatch.setattr(fetchers, "_ADAPTERS", {"gutenberg": lambda q, **kw: [] or time.sleep(0) or []})
    before = threading.active_count()
    for _ in range(40):
        fetchers.search("x")
    assert threading.active_count() <= before + fetchers.SEARCH_WORKERS + fetchers.DETAIL_WORKERS
    assert fetchers._SEARCH._max_workers == fetchers.SEARCH_WORKERS
    assert fetchers._DETAIL._max_workers == fetchers.DETAIL_WORKERS


def test_enrichment_cannot_outlive_the_deadline_it_is_given(monkeypatch):
    """A scalar requests timeout applies to the connect AND to every read, so `timeout=8` was
    up to 16 s against a stalling host — more than twice app.ENRICH_DEADLINE. On a shared pool
    that is one of a fixed number of threads, not a thread of its own."""
    import enrich
    assert isinstance(enrich.TIMEOUT, tuple) and sum(enrich.TIMEOUT) <= 8
    seen = {}
    monkeypatch.setattr(config, "ENRICH_METADATA", True)
    monkeypatch.setattr(enrich.requests, "get",
                        lambda url, **kw: (_ for _ in ()).throw(IOError(str(seen.update(kw)))))
    enrich._cache.clear()
    enrich.for_book("Some Title", "Some Author")
    assert seen["timeout"] == enrich.TIMEOUT


def test_a_source_that_never_answers_does_not_hold_the_page(monkeypatch):
    """A blocked adapter must cost the page its own results, not the whole search — and its
    future is cancelled rather than left to run behind the rendered page."""
    monkeypatch.setattr(config, "SOURCES", {"gutenberg": True, "librivox": True})
    stop = threading.Event()
    monkeypatch.setattr(fetchers, "_ADAPTERS",
                        {"gutenberg": lambda q, **kw: (stop.wait(30), [])[1],
                         "librivox": lambda q, **kw: [{"source": "librivox", "title": q,
                                                       "author": "A", "download_url": "u"}]})
    t = time.monotonic()
    try:
        out = fetchers.search("x", deadline=0.5)
        assert time.monotonic() - t < 5
        assert [r["source"] for r in out] == ["librivox"]
    finally:
        stop.set()


# ------------------------------------------------ /intake: a bad token is a 401, never a 500
def test_a_non_ascii_intake_token_is_rejected_not_crashed(client):
    """hmac.compare_digest on two str operands raises TypeError as soon as either side holds a
    codepoint above U+00FF, so `X-Intake-Token: é中` was an unhandled exception on the one
    anonymous, Authelia-bypassed, CSRF-exempt route this stack exposes: a traceback in the
    gunicorn log and a status code neither Caddy's log nor fail2ban reads as an auth failure."""
    for tok in ("é中", "ሴ", "ünicode", "wrong-token", ""):
        r = client.post("/intake", headers={"X-Intake-Token": tok},
                        json={"user": "alice", "url": "https://example.test/b.epub"})
        assert r.status_code == 401, (tok, r.status_code)
        assert r.get_json() == {"error": "unauthorized"}


def test_the_right_intake_token_still_works(client, users):
    r = client.post("/intake", headers={"X-Intake-Token": config.INTAKE_TOKEN},
                    json={"user": "alice", "url": "https://example.test/b.epub"})
    assert r.status_code == 202 and r.get_json()["ok"] is True


# ------------------------------------------------ the Kobo link survives a password change
def _change_password(client, new="newpass123"):
    return post(client, "/devices", action="password", current="alicepass1", new=new, repeat=new)


def test_a_password_change_says_the_kobo_link_is_not_revoked(client, users):
    """cwa.set_password never touches remote_auth_token, and only reset_kobo_token deletes it,
    so https://books.<domain>/kobo/<token>/ keeps serving that user's whole library after the
    password change that was made BECAUSE the credential leaked. The flash carefully listed
    Authelia and Shelfmark and was silent about the longest-lived credential of the three."""
    login(client, "alice", users["alice"])
    post(client, "/devices", action="kobo")
    assert cwa.kobo_token("alice", create=False)
    r = _change_password(client)
    assert b"Password changed" in r.data and b"Kobo sync link is a separate key" in r.data
    assert b"NOT changed" in r.data


def test_a_reader_without_a_kobo_is_not_told_about_kobo_links(client, users):
    """Only when a token exists: nobody should be sent looking for a feature they do not use."""
    login(client, "alice", users["alice"])
    assert cwa.kobo_token("alice", create=False) is None
    r = _change_password(client)
    assert b"Password changed" in r.data and b"Kobo sync link" not in r.data


def test_the_devices_page_says_the_kobo_link_is_its_own_key(client, users):
    login(client, "alice", users["alice"])
    post(client, "/devices", action="kobo")
    html = client.get("/devices").get_data(as_text=True)
    assert "changing your password does not change it" in html


# ------------------------------------------------ the page quotes the limit the code enforces
def test_the_devices_page_quotes_the_portals_own_kindle_limit(client, users, monkeypatch):
    """The page said Amazon takes 50 MB; librarian/kindle.py refuses at KINDLE_MAX_MB (45), so
    a reader with a 46 MB PDF was told by the page that it was fine and then shown an error
    blaming Amazon for a refusal the portal made itself. It also omitted TXT, which is mailed."""
    login(client, "alice", users["alice"])
    html = client.get("/devices").get_data(as_text=True)
    assert "50 MB" not in html
    assert f"{config.KINDLE_MAX_MB} MB" in html
    for fmt in config.KINDLE_FORMATS:
        assert fmt.upper() in html, fmt
    monkeypatch.setattr(config, "KINDLE_MAX_MB", 25)
    assert "25 MB" in client.get("/devices").get_data(as_text=True)


# ------------------------------------------------ outbound mail has a ceiling
def test_send_to_kindle_has_a_daily_ceiling_for_non_admins(client, users, monkeypatch):
    """Neither mail route was matched by any Caddy rate_limit zone and kindle.send only checks
    the attachment size, so one session cookie could drive unlimited 45 MB messages through the
    SMTP account. The realistic outcome is the provider suspending it — which takes
    Send-to-Kindle, mail notifications and alert.sh's fallback channel down together."""
    add_calibre_book(1, "Alice Book", "Ann Author", tags=["owner:alice"], formats=("epub",))
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    monkeypatch.setattr(config, "KINDLE_MAX_PER_DAY", 3)
    sent = []
    monkeypatch.setattr(kindle, "send",
                        lambda to, path, title=None, filename=None, **kw: (sent.append(to), f"sent to {to}")[1])
    login(client, "alice", users["alice"])
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    import worker
    for _ in range(3):
        assert b"on its way" in post(client, "/kindle/1").data
    r = post(client, "/kindle/1")
    while worker.kindle_once(): pass
    assert b"which is the limit" in r.data and len(sent) == 3
    # yesterday's sends do not count against today
    with sqlite3.connect(config.STATE_DB) as c:
        c.execute("UPDATE audit SET ts=ts-90000 WHERE event='kindle_send'")
    assert b"on its way" in post(client, "/kindle/1").data
    while worker.kindle_once(): pass
    assert len(sent) == 4


def test_an_admin_is_exempt_from_the_kindle_ceiling(client, users, monkeypatch):
    """Same shape as MAX_REQUESTS_PER_DAY: the ceiling protects the SMTP account from a stolen
    reader session, and the admin is the person who would be repairing things."""
    add_calibre_book(1, "Any Book", "Ann Author", tags=["owner:alice"], formats=("epub",))
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    monkeypatch.setattr(config, "KINDLE_MAX_PER_DAY", 1)
    sent = []
    monkeypatch.setattr(kindle, "send",
                        lambda to, path, title=None, filename=None, **kw: (sent.append(to), f"sent to {to}")[1])
    login(client, "admin", users["admin"])
    cwa.set_kindle_mail("admin", "admin@kindle.com")
    for _ in range(3):
        assert b"on its way" in post(client, "/kindle/1").data
    import worker
    while worker.kindle_once(): pass
    assert len(sent) == 3


def test_the_kindle_test_button_has_a_cooldown(client, users, monkeypatch):
    """prefs.last_kindle_test was written and displayed but never read as a guard, so the
    button was an unmetered outbound-mail tap. A test proves the approved-sender step once."""
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    sent = []
    monkeypatch.setattr(kindle, "_deliver", lambda msg: sent.append(msg["To"]))
    login(client, "alice", users["alice"])
    cwa.set_kindle_mail("alice", "alice@kindle.com")
    assert b"Test mail sent" in post(client, "/devices", action="kindle_test").data
    r = post(client, "/devices", action="kindle_test")
    assert b"A test was just sent" in r.data and len(sent) == 1
    db.set_prefs("alice", last_kindle_test=time.time() - config.KINDLE_TEST_COOLDOWN - 1)
    assert b"Test mail sent" in post(client, "/devices", action="kindle_test").data
    assert len(sent) == 2


def test_renaming_the_admin_carries_their_portal_history(users):
    """The installer moves the dropbox, the Authelia key and the qBittorrent path on a rename;
    the portal's own rows are keyed on the NAME, so without db.rename_owner the admin's request
    history, preferences and audit trail stay pointed at an account that no longer exists and
    simply vanish from their own pages."""
    import db, cwa
    rid = db.add("admin", {"kind": "ebook", "source": "gutenberg", "title": "T", "author": "A",
                           "download_url": "https://x/y.epub"}, status="done")
    db.set_prefs("admin", auto_kindle=True)
    db.set_pw_fingerprint("admin", "fp-old")
    db.audit("login", "admin", "10.0.0.1", "before the rename")

    moved = cwa.rename_user("admin", "kenith-admin")["portal_rows"]
    assert "error" not in moved

    assert db.get(rid)["owner"] == "kenith-admin"
    assert db.get_prefs("kenith-admin")["auto_kindle"] is True
    assert db.get_pw_fingerprint("kenith-admin") == "fp-old"
    assert db.get_pw_fingerprint("admin") is None
    assert [r for r in db.audit_recent(50) if r["user"] == "kenith-admin"]
    assert not [r for r in db.audit_recent(50) if r["user"] == "admin"]


def test_a_rename_onto_a_reused_name_keeps_the_renamed_accounts_rows(users):
    """prefs.owner and pw_sync.owner are PRIMARY KEYs. A name reused after a removal can still
    have rows, and a bare UPDATE would fail on the conflict: the account being renamed wins."""
    import db, cwa
    db.set_prefs("admin", auto_kindle=True)
    db.set_prefs("ghost", auto_kindle=False)          # a leftover row under the target name
    cwa.rename_user("admin", "ghost")
    assert db.get_prefs("ghost")["auto_kindle"] is True
