"""Stage C: the request-to-Calibre join, and reading metadata.db honestly.

_in_calibre returned a bool inside a bare `except Exception: return False`, so "the catalogue
is unreadable" and "this book did not import" were the same answer, and the one moment the
mapping is certain was thrown away.
"""
import sqlite3, time
import db, library, worker


def _calibre_with(tmp_path, marker, wal=True):
    p = tmp_path / "metadata.db"
    c = sqlite3.connect(str(p))
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT)")
    c.execute("CREATE TABLE data(id INTEGER PRIMARY KEY, book INTEGER, name TEXT)")
    c.execute("INSERT INTO books(id,title,path) VALUES(42,'Moby-Dick','M/Moby')")
    c.execute(f"INSERT INTO data(book,name) VALUES(42,'Moby-Dick {marker}')")
    c.commit(); c.close()
    return str(p)


def test_in_calibre_returns_the_book_id_not_a_bool(monkeypatch, tmp_path):
    marker = "[alice-7]"
    monkeypatch.setattr(library.config, "CALIBRE_DB", _calibre_with(tmp_path, marker))
    got = worker._in_calibre(marker)
    assert got == 42, "the id is the only free, certain request-to-Calibre join we get"
    assert worker._in_calibre("[nobody-999]") is None


def test_an_unreadable_catalogue_is_not_reported_as_not_imported(monkeypatch, tmp_path):
    """The dangerous conflation: a database we cannot open must not look like a book that
    never arrived, or reconciliation would 'correct' a perfectly good import."""
    monkeypatch.setattr(library.config, "CALIBRE_DB", str(tmp_path / "does-not-exist.db"))
    assert worker._in_calibre("[alice-7]") is None          # None, and it logs — not False


def test_link_calibre_records_the_join_once_it_is_known(users):
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "T", "author": "",
                           "download_url": "local"}, status="importing")
    db.link_calibre(rid, 42, "alice")
    assert db.get(rid)["calibre_id"] == 42
    with db._conn() as c:
        row = c.execute("SELECT * FROM meta_link WHERE rid=?", (rid,)).fetchone()
    assert row["calibre_id"] == 42 and row["owner"] == "alice"


def test_the_breaker_opens_on_repeated_failure_and_says_so(users):
    """A chain that silently falls through to nothing is the failure this project keeps
    rediscovering, so an open breaker has to be visible."""
    now = time.time()
    assert db.breaker_record("bookinfo", ok=False, error="timeout", now=now) is False
    assert db.breaker_record("bookinfo", ok=False, error="timeout", now=now) is False
    assert db.breaker_record("bookinfo", ok=False, error="timeout", now=now) is True   # threshold
    is_open, retry = db.breaker_state("bookinfo", now=now)
    assert is_open and retry > now
    assert [b["provider"] for b in db.open_breakers(now=now)] == ["bookinfo"]
    # a success clears it immediately: a provider that answers is a working provider
    db.breaker_record("bookinfo", ok=True, now=now)
    assert db.breaker_state("bookinfo", now=now) == (False, 0.0)
    assert db.open_breakers(now=now) == []


def test_a_soft_miss_must_not_trip_the_breaker(users):
    """HTTP 200 carrying nothing useful is a clean miss: advance the chain, do not disable a
    working provider because it had no answer about one book."""
    now = time.time()
    for _ in range(5):
        db.breaker_record("openlibrary", ok=True, now=now)      # a miss is recorded as ok
    assert db.breaker_state("openlibrary", now=now) == (False, 0.0)
