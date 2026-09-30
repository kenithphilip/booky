"""v6.2.1: where a book stands on a reader's Kobo, from Calibre-Web's own records (one place);
'Put it back on my Kobo'; putting back also takes the book off the synced list (the Peanuts never
came back); every removal tells the Kobo; the library's rules are checked in one place
(crosscheck.py) that the dashboard and the hourly self-check share, for books AND audiobooks."""
import json, time
import pytest
import config, db, cwa, comics, bookreq, share, crosscheck, admin_cli, dash
import abs as absapi
from conftest import add_calibre_book, login, post
from test_comics import _comic_book
from test_v610 import CWA_KOBO_TABLES, _cwa
import sqlite3


@pytest.fixture
def kobo(users):
    c = sqlite3.connect(config.CWA_DB); c.executescript(CWA_KOBO_TABLES); c.close()
    return cwa.get_user("bob")["id"]


def _synced(uid, bid):
    _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, ?)", uid, bid)


def _deleted_on_kobo(uid, bid):
    """What Calibre-Web records when the reader deletes a book on the Kobo, and after the next sync
    (cps/kobo.py HandleBookDeletionRequest, then HandleSyncRequest's add_synced_books)."""
    _cwa("INSERT INTO archived_book(user_id, book_id, is_archived, last_modified) VALUES(?, ?, 1, '2026-09-30 07:23:47')", uid, bid)
    _synced(uid, bid)


# ---- where a book stands on the Kobo ---------------------------------------------------------------
def test_the_kobo_state_follows_calibre_webs_records(kobo):
    for bid in (1, 2, 3, 4):
        add_calibre_book(bid, f"Book {bid}", "A", tags=["owner:bob"])
    assert cwa.kobo_state("bob", 1) == "no-kobo", "no Kobo ever synced"
    _synced(kobo, 1)
    assert cwa.kobo_state("bob", 1) == "on-kobo" and cwa.kobo_state("bob", 2) == "coming"
    _deleted_on_kobo(kobo, 3)
    assert cwa.kobo_state("bob", 3) == "deleted"
    _cwa("INSERT INTO archived_book(user_id, book_id, is_archived) VALUES(?, 4, 1)", kobo)
    assert cwa.kobo_state("bob", 4) == "removing", "archived, not told yet"

def test_a_kobo_that_syncs_only_shelves(kobo):
    for bid in (1, 2, 3):
        add_calibre_book(bid, f"Book {bid}", "A", tags=["owner:bob"])
    _cwa("UPDATE user SET kobo_only_shelves_sync=1 WHERE id=?", kobo)
    _cwa("INSERT INTO shelf(id, name, user_id, kobo_sync) VALUES(7, 'Kobo', ?, 1)", kobo)
    _cwa("INSERT INTO book_shelf_link(book_id, shelf) VALUES(1, 7)")
    _cwa("INSERT INTO book_shelf_link(book_id, shelf) VALUES(2, 7)")
    _synced(kobo, 1); _synced(kobo, 3)
    assert [cwa.kobo_state("bob", b) for b in (1, 2, 3)] == ["on-kobo", "coming", "removing"]
    _cwa("DELETE FROM kobo_synced_books WHERE book_id=3")
    assert cwa.kobo_state("bob", 3) == "not-on-shelf"


# ---- putting it back ---------------------------------------------------------------------------------
def test_putting_back_clears_the_mark_AND_the_synced_list_as_calibre_web_does(kobo):
    add_calibre_book(12, "The Complete Peanuts", "Charles M. Schulz", tags=["owner:bob"])
    _deleted_on_kobo(kobo, 12)
    assert cwa.kobo_put_back("bob", 12)
    assert _cwa("SELECT is_archived FROM archived_book WHERE book_id=12") == [(0,)]
    assert _cwa("SELECT 1 FROM kobo_synced_books WHERE book_id=12") == [], \
        "off the synced list: Calibre-Web sends a Kobo only books NOT on it (v6.2.0 left it there: never sent)"
    assert cwa.kobo_state("bob", 12) == "coming"
    assert not cwa.kobo_put_back("bob", 12), "nothing to put back twice"

def test_a_book_given_back_after_a_removal_reaches_the_kobo_again(kobo):
    add_calibre_book(5, "Mort", "Terry Pratchett", tags=["owner:alice"])
    _deleted_on_kobo(kobo, 5)                     # bob removed it earlier; his Kobo was told
    share.give_ebook({"book_id": 5, "owners": ["alice"]}, "bob")
    assert cwa.kobo_state("bob", 5) == "coming"

def test_the_book_page_offers_put_it_back_to_its_reader(client, kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:alice"])
    _synced(kobo, 9)                              # his Kobo syncs
    _deleted_on_kobo(kobo, 1)
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "you deleted it on the Kobo" in html and "Put it back on my Kobo" in html
    html = post(client, "/book/1/kobo-back").get_data(as_text=True)
    assert "Put back: it comes to your Kobo at its next sync" in html and "it arrives at the next sync" in html
    assert post(client, "/book/2/kobo-back").status_code == 404, "not his book"

def test_remaking_a_comic_deleted_on_the_kobo_puts_it_back_and_says_what_a_kobo_keeps(client, kobo):
    _comic_book(12, "The Complete Peanuts", 1, ["Comics", "owner:bob"], formats=("cbz", "kepub"))
    _synced(kobo, 9)
    login(client, "bob", "bobpass1")
    html = post(client, "/book/12/kobo", remake="1").get_data(as_text=True)
    assert "Remove download" in html and "replaces the old copy" not in html, "a Kobo keeps the file it has"
    _deleted_on_kobo(kobo, 12)
    html = post(client, "/book/12/kobo", remake="1").get_data(as_text=True)
    assert "It was deleted on your Kobo: it is put back" in html and cwa.kobo_state("bob", 12) == "coming"


# ---- every removal tells the Kobo -------------------------------------------------------------------
def test_wrong_comic_and_the_chapter_swap_take_the_book_off_the_kobo_like_remove(kobo, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(comics.notify, "admin", lambda *a, **k: None)
    _comic_book(3, "Saga", 1, ["Comics", "owner:bob"])
    _synced(kobo, 3)
    db.comic_add("bob", {"provider": "metron", "series_id": "1", "series_name": "Saga", "kind": "comic", "reading": "ltr",
                         "strip": None, "number": "1", "label": "#1", "year": None, "publisher": None, "language": "en",
                         "cover": None, "authors": [], "summary": "", "unit": ""})
    rid = db.comic_open("bob")[0]["id"]
    db.comic_update(rid, status="done", calibre_id=3)
    comics.wrong_comic("bob", 3)
    assert cwa.kobo_state("bob", 3) == "removing"
    (w,) = db.kobo_waits()
    assert (w["calibre_id"], w["kobo_wait"]) == (3, "archive"), "the tag waits for the Kobo, as Remove does"

def test_wrong_book_takes_it_off_the_kobo(kobo, monkeypatch):
    monkeypatch.setattr(bookreq.notify, "admin", lambda *a, **k: None)
    add_calibre_book(4, "Dune", "Frank Herbert", tags=["owner:bob"])
    _synced(kobo, 4)
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="done", calibre_id=4)
    bookreq.wrong_book("bob", 4)
    assert cwa.kobo_state("bob", 4) == "removing"


# ---- the library's rules, in one place --------------------------------------------------------------
def _codes(r, kind):
    return [x["code"] for x in r[kind]]

def test_a_removal_counting_down_is_a_note_not_a_failure(users):
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=[])
    add_calibre_book(8, "Lost Book", "Nobody", tags=[])
    add_calibre_book(9, "Fine", "A", tags=["owner:bob"])
    db.release_note(7, "its last reader removed it", [])
    r = crosscheck.run()
    assert _codes(r, "problems") == ["untagged"] and "8 Lost Book" in r["problems"][0]["text"]
    assert "Kite Runner" not in r["problems"][0]["text"]
    assert "releasing" in _codes(r, "notes") and "The Kite Runner (in 7 days)" in r["notes"][0]["text"]

def test_a_share_being_tagged_and_an_orphan_owner_tag(users):
    add_calibre_book(1, "Shared", "A", tags=[])
    add_calibre_book(2, "Ghost's", "A", tags=["owner:ghost"])
    db.queue_tag_push(1, None, "bob", share=True)
    r = crosscheck.run()
    assert _codes(r, "problems") == ["orphan-owner"] and "ghost" in r["problems"][0]["text"]
    assert "being-tagged" in _codes(r, "notes")

def test_books_deleted_on_a_kobo_are_listed_as_a_note(kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _deleted_on_kobo(kobo, 1)
    r = crosscheck.run()
    assert r["problems"] == [] and any("bob: Mort" in n["text"] for n in r["notes"] if n["code"] == "kobo-deleted")

def test_audiobooks_follow_the_same_rules(users, monkeypatch):
    now = time.time()
    monkeypatch.setattr(absapi, "configured", lambda: True)
    item = lambda i, title, tags, added: {"id": i, "addedAt": added * 1000, "media": {"tags": tags, "metadata": {"title": title}}}
    monkeypatch.setattr(share, "_abs_items", lambda: [
        item("a1", "Hail Mary", ["owner:bob"], now - 86400),
        item("a2", "Lost Audio", [], now - 86400),
        item("a3", "Removed Audio", [], now - 86400),
        item("a4", "Just Added", [], now - 60),
        item("a5", "Ghost Audio", ["owner:ghost"], now - 86400)])
    db.audio_release_note("a3", "Removed Audio")
    r = crosscheck.run()
    assert _codes(r, "problems") == ["abs-untagged", "abs-orphan-owner"]
    assert "Lost Audio" in r["problems"][0]["text"] and "Removed Audio" not in r["problems"][0]["text"]
    assert {"abs-releasing", "abs-settling"} <= set(_codes(r, "notes"))

def test_the_self_check_and_the_dashboard_ask_the_same_place(users, capsys):
    add_calibre_book(8, "Lost Book", "Nobody", tags=[])
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=[])
    db.release_note(7, "its last reader removed it", [])
    admin_cli.main(["invariants"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and _codes(out, "problems") == ["untagged"] and "releasing" in _codes(out, "notes")
    texts = [t for _s, t, _l in dash.needs()]
    assert any("Lost Book" in t for t in texts) and any("Kite Runner" in t for t in texts)


# ---- deleting on the Kobo sticks for every reader, as it did for admins ------------------------------
def test_every_reader_may_see_archived_books_so_a_kobo_delete_is_kept(users):
    assert _cwa("SELECT sidebar_view & ? FROM user WHERE name='bob'", cwa.SIDEBAR_ARCHIVED) == [(cwa.SIDEBAR_ARCHIVED,)], "a new reader"
    _cwa("UPDATE user SET sidebar_view=1 WHERE name='alice'")              # a reader from before v6.2.1
    assert cwa.grant_archive_view() == 1 and cwa.grant_archive_view() == 0
    assert _cwa("SELECT sidebar_view FROM user WHERE name='alice'") == [(1 | cwa.SIDEBAR_ARCHIVED,)]
    _cwa("UPDATE user SET sidebar_view=1 WHERE name='alice'")
    cwa.ensure_isolation("alice")                                          # Users -> Repair
    assert _cwa("SELECT sidebar_view & ? FROM user WHERE name='alice'", cwa.SIDEBAR_ARCHIVED) == [(cwa.SIDEBAR_ARCHIVED,)]


def test_a_removed_book_asked_for_again_is_queued_as_a_give_back(users):
    add_calibre_book(6, "Emma", "Jane Austen", tags=[])
    db.release_note(6, "its last reader removed it", [])
    share.give_ebook({"book_id": 6, "owners": [], "released": True}, "bob")
    share.give_ebook({"book_id": 5, "owners": ["alice"]}, "bob")
    with db._conn() as c:
        rows = dict(c.execute("SELECT calibre_id, share FROM tag_push WHERE status='pending'").fetchall())
    assert rows == {6: 2, 5: 1}, "2: the host job may give an ownerless book back to its reader; 1: a second owner only"
