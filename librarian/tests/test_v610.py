"""v6.1.0: removal reaches the Kobo, audiobooks can be removed (and are deleted when nobody has
them), a removed book asked for again is given back, copies already yours are never offered,
landscape comics are rotated (not cut in half) on e-readers, Remake Kobo copy."""
import os, sqlite3, time, zipfile
import pytest
import config, db, cwa, comics, bookreq, share, worker
import abs as absapi
from conftest import add_calibre_book, login, post
from test_comics import SERIES, Shelf, _comic_book, _png
from test_v60 import Shelf as BookShelf

CWA_KOBO_TABLES = """
CREATE TABLE IF NOT EXISTS archived_book (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER, is_archived BOOLEAN, last_modified DATETIME);
CREATE TABLE IF NOT EXISTS kobo_synced_books (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER);
CREATE TABLE IF NOT EXISTS shelf (id INTEGER PRIMARY KEY, uuid VARCHAR, name VARCHAR, is_public INTEGER, user_id INTEGER,
                                  kobo_sync BOOLEAN, created DATETIME, last_modified DATETIME);
CREATE TABLE IF NOT EXISTS book_shelf_link (id INTEGER PRIMARY KEY, book_id INTEGER, "order" INTEGER, shelf INTEGER, date_added DATETIME);
"""

def _cwa(sql, *args):
    c = sqlite3.connect(config.CWA_DB)
    try:
        r = c.execute(sql, args).fetchall()
        c.commit()
        return r
    finally:
        c.close()

@pytest.fixture
def kobo(users):
    c = sqlite3.connect(config.CWA_DB); c.executescript(CWA_KOBO_TABLES); c.close()
    return cwa.get_user("bob")["id"]


# ---- the Kobo is told --------------------------------------------------------------------------
def test_removing_a_book_archives_it_for_the_kobo_and_waits_for_the_sync(client, kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, 1)", kobo)
    login(client, "bob", "bobpass1")
    post(client, "/book/1/remove")
    assert _cwa("SELECT is_archived FROM archived_book WHERE user_id=? AND book_id=1", kobo) == [(1,)]
    assert _cwa("SELECT 1 FROM kobo_synced_books WHERE user_id=? AND book_id=1", kobo) == [], \
        "off the synced list: the next sync sends it again, as removed"
    assert db.untag_pending(1, "bob"), "gone from My books at once"
    assert not [p for p in db.pending_tag_pushes() if p["calibre_id"] == 1], \
        "the owner tag stays until the Kobo has synced (Calibre-Web only tells it about a book bob can see)"
    assert worker.release_kobo_waits() == 0
    _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, 1)", kobo)     # what CWA records when it sends it
    assert worker.release_kobo_waits() == 1
    assert [p["op"] for p in db.pending_tag_pushes() if p["calibre_id"] == 1] == ["remove"]

def test_a_kobo_that_syncs_only_shelves_gets_the_book_taken_off_its_shelves(kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _cwa("UPDATE user SET kobo_only_shelves_sync=1 WHERE id=?", kobo)
    _cwa("INSERT INTO shelf(id, name, user_id, kobo_sync) VALUES(7, 'Kobo', ?, 1)", kobo)
    _cwa("INSERT INTO book_shelf_link(book_id, shelf) VALUES(1, 7)")
    _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, 1)", kobo)
    assert cwa.kobo_remove("bob", 1) == "shelf"
    assert _cwa("SELECT 1 FROM book_shelf_link WHERE book_id=1") == []
    assert not cwa.kobo_removed("bob", 1, "shelf")
    _cwa("DELETE FROM kobo_synced_books")                                      # CWA's two-way sync did it
    assert cwa.kobo_removed("bob", 1, "shelf")

def test_a_book_that_never_went_to_the_kobo_is_untagged_at_once(client, kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    login(client, "bob", "bobpass1")
    post(client, "/book/1/remove")
    assert _cwa("SELECT 1 FROM archived_book") == []
    assert [p["op"] for p in db.pending_tag_pushes() if p["calibre_id"] == 1] == ["remove"]

def test_asked_for_again_before_the_kobo_synced_it_stays(kobo, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, 1)", kobo)
    how = cwa.kobo_remove("bob", 1)
    db.queue_untag(1, "bob", not_before=time.time() + 3600, kobo_wait=how)
    rid, what = bookreq.request("bob", "Mort", "Terry Pratchett")
    assert what == "owned" and "stays in your library" in db.bookreq_get(rid)["detail"]
    assert not db.untag_pending(1, "bob"), "the removal is withdrawn"
    assert _cwa("SELECT is_archived FROM archived_book WHERE book_id=1") == [(0,)], "not archived: the Kobo keeps it"

def test_no_kobo_tables_means_no_kobo(users):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    assert cwa.kobo_remove("bob", 1) is None


# ---- asked for again during the countdown: given back ---------------------------------------------
def test_a_book_counting_down_is_given_back_not_downloaded(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=[])
    db.release_note(1, "its last reader removed it", ["owner:bob"], removed_by="bob")
    given = []
    monkeypatch.setattr(share.db, "queue_tag_push", lambda *a, **k: given.append(a[:3]) or True)
    rid, what = bookreq.request("bob", "Mort", "Terry Pratchett")
    assert what == "shared" and given and given[0][0] == 1
    assert "given back" in db.bookreq_get(rid)["detail"]

def test_an_untagged_book_that_is_not_counting_down_is_someones_import(users):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=[])
    assert share.find_ebook("Mort", "Terry Pratchett") is None


# ---- copies already yours are never offered -----------------------------------------------------
def test_a_comic_offered_after_it_arrived_is_closed(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    rid, _ = comics.request("bob", SERIES, {"number": "5", "label": "Vol. 5"})
    db.comic_update(rid, status="confirm", candidate={"source_id": "x", "title": "One Piece v05"})
    _comic_book(40, "One Piece", 5, ["Manga", "owner:bob"])
    comics.watch_downloads(Shelf([]), {})
    r = db.comic_get(rid)
    assert r["status"] == "done" and r["candidate"] is None and "nothing to confirm" in r["detail"]

def test_a_book_offered_after_it_arrived_is_closed(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    rid, _ = bookreq.request("bob", "Mort", "Terry Pratchett")
    db.bookreq_update(rid, status="confirm", candidate={"source_id": "x", "title": "Mort epub"})
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    bookreq.watch_downloads(BookShelf([]), {})
    r = db.bookreq_get(rid)
    assert r["status"] == "owned" and "nothing to confirm" in r["detail"]

def test_a_request_for_a_book_removed_since_is_not_a_link(client, users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    rid, _ = bookreq.request("bob", "Mort", "Terry Pratchett")
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    db.bookreq_update(rid, status="done", calibre_id=1)
    login(client, "bob", "bobpass1")
    assert 'href="/book/1"' in client.get("/status").get_data(as_text=True)
    db.queue_untag(1, "bob")
    page = client.get("/status").get_data(as_text=True)
    assert 'href="/book/1"' not in page and "removed from your library" in page


# ---- audiobooks -----------------------------------------------------------------------------------
@pytest.fixture
def abs_items(users, monkeypatch):
    items = {"li1": {"title": "Hail Mary", "owners": ["bob"]}}
    monkeypatch.setattr(absapi, "configured", lambda: True)
    monkeypatch.setattr(absapi, "item_meta", lambda i, token=None: {"title": items[i]["title"]} if i in items else {})
    def untag(i, tag, token=None):
        o = tag[len(config.OWNER_PREFIX):]
        if o not in items[i]["owners"]:
            return False
        items[i]["owners"].remove(o); return True
    monkeypatch.setattr(absapi, "untag_item", untag)
    monkeypatch.setattr(absapi, "item_owners", lambda i, token=None: list(items[i]["owners"]) if i in items else None)
    deleted = []
    monkeypatch.setattr(absapi, "delete_item", lambda i, token=None: deleted.append(i) or items.pop(i) and True)
    return items, deleted

def test_the_last_reader_removes_an_audiobook_and_it_is_deleted_after_the_countdown(client, abs_items, monkeypatch):
    items, deleted = abs_items
    monkeypatch.setattr(config, "LIBRARY_RELEASE_DAYS", 7)
    login(client, "bob", "bobpass1")
    post(client, "/audiobooks/li1/remove")
    assert items["li1"]["owners"] == [] and db.audio_release_waiting("li1")
    assert worker.reconcile_audio_releases(now=time.time() + 86400) == 0 and not deleted, "not before 7 days"
    assert worker.reconcile_audio_releases(now=time.time() + 8 * 86400) == 1 and deleted == ["li1"]

def test_an_audiobook_someone_has_again_is_kept(abs_items, monkeypatch):
    items, deleted = abs_items
    monkeypatch.setattr(config, "LIBRARY_RELEASE_DAYS", 7)
    db.audio_release_note("li1", "Hail Mary", now=time.time() - 10 * 86400)
    items["li1"]["owners"] = ["alice"]
    worker.reconcile_audio_releases()
    assert not deleted and db.audio_releases(("kept",))

def test_a_family_audiobook_is_removed_only_for_the_reader(client, abs_items, monkeypatch):
    items, deleted = abs_items
    items["li1"]["owners"] = ["alice", "bob"]
    login(client, "bob", "bobpass1")
    post(client, "/audiobooks/li1/remove")
    assert items["li1"]["owners"] == ["alice"] and not db.audio_release_waiting("li1")


# ---- landscape comics, Remake Kobo copy ------------------------------------------------------------
def _cbz(path, w, h, pages=12):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i + 1:03d}.png", _png(w, h))

def test_a_landscape_book_is_recognised_a_portrait_one_with_a_spread_is_not(tmp_path):
    _cbz(tmp_path / "wide.cbz", 180, 120)
    assert comics.looks_landscape(str(tmp_path / "wide.cbz"))
    with zipfile.ZipFile(tmp_path / "manga.cbz", "w") as z:
        for i in range(12):
            z.writestr(f"{i + 1:03d}.png", _png(180, 120) if i == 6 else _png(120, 180))
    assert not comics.looks_landscape(str(tmp_path / "manga.cbz"))

def test_the_landscape_mark_reaches_the_converter_even_for_a_comic_imported_before(users, monkeypatch):
    monkeypatch.setattr(comics, "uses_kobo", lambda o: True)
    _comic_book(1, "The Complete Peanuts", 1, ["Comics", "owner:bob"])
    rel = comics.comic_books()[1]["rel"]
    _cbz(os.path.join(config.LIBRARY_DIR, rel), 180, 120)            # the file: wide pages, no tag yet
    (row,) = comics.kobo_due()
    assert row["landscape"] is True and row["remake"] is False

def test_remake_kobo_copy_queues_a_comic_that_already_has_one(client, users, monkeypatch):
    monkeypatch.setattr(comics, "uses_kobo", lambda o: True)
    _comic_book(1, "One Piece", 1, ["Manga", "owner:bob"], formats=("cbz", "kepub"))
    assert comics.kobo_queue() == []
    login(client, "bob", "bobpass1")
    post(client, "/book/1/kobo", remake="1")
    (row,) = comics.kobo_queue()
    assert row["calibre_id"] == 1 and row["remake"] is True
    assert "new Kobo copy is" in client.get("/book/1").get_data(as_text=True)
    db.comic_convert_result(1, True)
    assert comics.kobo_queue() == [], "made: remake spent"
