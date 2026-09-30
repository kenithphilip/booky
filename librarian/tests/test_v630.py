"""v6.3.0: keeping devices tidy and private, per reader. What goes to a Kobo (everything / only what
they send; an admin's Kobo only their own books), finished books leaving the Kobo after a delay and
coming back with Send to my Kobo, Kindle reminders, 'Keep my books private', 'not-theirs' for a book
a reader does not have, neutral wording."""
import time, sqlite3
import pytest
import config, db, cwa, share, comics, ondevice, crosscheck
import abs as absapi
from conftest import add_calibre_book, login, post
from test_comics import _comic_book
from test_v610 import CWA_KOBO_TABLES, _cwa
from test_follows import _cwa_reading


@pytest.fixture
def kobo(users):
    c = sqlite3.connect(config.CWA_DB); c.executescript(CWA_KOBO_TABLES); c.close()
    _cwa_reading(0, [])                                    # the reading tables
    return {n: cwa.get_user(n)["id"] for n in ("admin", "alice", "bob")}


def _synced(uid, *bids):
    for b in bids:
        _cwa("INSERT INTO kobo_synced_books(user_id, book_id) VALUES(?, ?)", uid, b)


def _finished(uid, *bids):
    for b in bids:
        _cwa("INSERT INTO book_read_link(book_id, user_id, read_status) VALUES(?, ?, 1)", b, uid)


def _shelf(uid):
    return {r[0] for r in _cwa("SELECT l.book_id FROM book_shelf_link l JOIN shelf s ON s.id=l.shelf WHERE s.user_id=? "
                                "AND s.name=?", uid, cwa.MANAGED_SHELF)}


# ---- privacy: a reader is only ever told about their own books ----------------------------------------
def test_a_reader_hears_nothing_about_a_book_they_do_not_have(kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:alice"])
    _synced(kobo["bob"], 1)
    assert ondevice.kobo_states("bob", [1, 2]) == {1: "on-kobo", 2: "not-theirs"}
    assert ondevice.send_to_kobo("bob", 2) == "not-theirs" and ondevice.take_off_kobo("bob", 2) == "not-theirs"

def test_an_admin_viewing_someone_elses_book_gets_no_kobo_buttons(client, kobo):
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:alice"])
    _synced(kobo["admin"], 9)
    login(client, "admin", "adminpass1")
    html = client.get("/book/2").get_data(as_text=True)
    assert "Your Kobo:" not in html and "Send to my Kobo" not in html
    assert post(client, "/book/2/kobo-back").status_code == 404 and post(client, "/book/2/kobo-off").status_code == 404


# ---- what goes to the Kobo ----------------------------------------------------------------------------------
def test_only_the_books_i_send_keeps_what_is_on_the_kobo_and_sends_on_request(kobo):
    for b in (1, 2, 3):
        add_calibre_book(b, f"Book {b}", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1, 2)
    r = ondevice.set_kobo_send("bob", "choose", keep=True)
    assert r["removed"] == 0 and cwa.kobo_choose_only("bob") and _shelf(kobo["bob"]) == {1, 2}
    assert ondevice.kobo_states("bob", [1, 2, 3]) == {1: "on-kobo", 2: "on-kobo", 3: "not-on-shelf"}, "a new book waits"
    assert ondevice.send_to_kobo("bob", 3) == "coming"
    assert ondevice.take_off_kobo("bob", 1) == "removing", "off the shelf: Calibre-Web's two-way sync removes it"
    r = ondevice.set_kobo_send("bob", "all")
    assert not cwa.kobo_choose_only("bob") and ondevice.kobo_state("bob", 1) in ("removing", "deleted"), \
        "what they took off stays off in 'everything' mode too (archived for them)"

def test_starting_a_choose_kobo_empty(kobo):
    add_calibre_book(1, "Book 1", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    assert ondevice.set_kobo_send("bob", "choose", keep=False)["removed"] == 1
    assert ondevice.kobo_state("bob", 1) == "removing"

def test_an_admins_kobo_gets_only_their_own_books(kobo):
    add_calibre_book(1, "Mine", "A", tags=["owner:admin"])
    add_calibre_book(2, "Alice's", "A", tags=["owner:alice"])
    add_calibre_book(3, "Deleted mine", "A", tags=["owner:admin"])
    add_calibre_book(4, "New mine", "A", tags=["owner:admin"])
    _synced(kobo["admin"], 1, 2, 3)
    _cwa("INSERT INTO archived_book(user_id, book_id, is_archived) VALUES(?, 3, 1)", kobo["admin"])  # deleted on the Kobo
    ondevice.admin_kobo_pass()
    assert cwa.kobo_choose_only("admin") and _shelf(kobo["admin"]) == {1, 4}, \
        "their own books (not the one they deleted on the Kobo), never Alice's"
    assert ondevice.kobo_state("admin", 2) == "not-theirs" and cwa.kobo_state("admin", 2) == "removing", \
        "Alice's book leaves the admin's Kobo at the next sync"
    db.set_device_prefs("admin", kobo_scope="library")
    ondevice.set_kobo_send("admin", "library")
    ondevice.admin_kobo_pass()
    assert not cwa.kobo_choose_only("admin"), "an admin who chose the whole library keeps it"

def test_an_admin_without_a_kobo_is_left_alone(kobo):
    add_calibre_book(1, "Mine", "A", tags=["owner:admin"])
    assert ondevice.admin_kobo_pass() == 0 and not cwa.kobo_choose_only("admin")


# ---- finished books ---------------------------------------------------------------------------------------------
def test_finished_books_leave_the_kobo_after_the_delay_and_come_back_to_stay(kobo):
    for b in (1, 2):
        add_calibre_book(b, f"Book {b}", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1, 2)
    _finished(kobo["bob"], 1)
    assert ondevice.offload_finished() == 0, "the setting is off by default: nothing leaves"
    db.set_device_prefs("bob", kobo_finished=7)
    now = time.time()
    assert ondevice.offload_finished(now) == 0 and db.device_book("bob", 1, "kobo")["status"] == "waiting"
    assert ondevice.offload_finished(now + 3 * 86400) == 0, "not before 7 days"
    assert ondevice.offload_finished(now + 8 * 86400) == 1
    assert ondevice.kobo_state("bob", 1) == "removing" and ondevice.kobo_state("bob", 2) == "on-kobo"
    assert "owner:bob" in [t for (t,) in __import__("library")._conn().execute(
        "SELECT t.name FROM tags t JOIN books_tags_link l ON l.tag=t.id WHERE l.book=1")], "still in their library"
    assert ondevice.send_to_kobo("bob", 1) == "coming" and db.device_book("bob", 1, "kobo")["status"] == "kept"
    _synced(kobo["bob"], 1)
    assert ondevice.offload_finished(now + 60 * 86400) == 0, "a book sent back stays"

def test_right_away_and_a_book_marked_unread_again(kobo):
    for b in (1, 2):
        add_calibre_book(b, f"Book {b}", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1, 2)
    _finished(kobo["bob"], 1, 2)
    db.set_device_prefs("bob", kobo_finished=30)
    ondevice.offload_finished()
    _cwa("DELETE FROM book_read_link WHERE book_id=2")               # marked unread
    ondevice.offload_finished()
    assert db.device_book("bob", 2, "kobo") is None, "its countdown stopped"
    db.set_device_prefs("bob", kobo_finished=0)
    assert ondevice.offload_finished() == 1 and ondevice.kobo_state("bob", 1) == "removing"


# ---- Kindle ------------------------------------------------------------------------------------------------------
def test_finished_books_sent_to_a_kindle_are_listed_to_delete_there(client, kobo, monkeypatch):
    cwa.set_kindle_mail("bob", "bob@kindle.com")
    add_calibre_book(1, "Dune", "Frank Herbert", tags=["owner:bob"])
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:bob"])
    with db._conn() as c:
        for b, t in ((1, "Dune"), (2, "Emma")):
            c.execute("INSERT INTO kindle_jobs(owner, book_id, title, status, created, updated) VALUES('bob', ?, ?, 'sent', 0, 0)", (b, t))
    _finished(kobo["bob"], 1)
    assert ondevice.kindle_to_delete("bob") == [{"book_id": 1, "title": "Dune"}], "finished AND sent to the Kindle"
    login(client, "bob", "bobpass1")
    assert "Finished, and still on your Kindle" in client.get("/library").get_data(as_text=True)
    assert "I deleted it" in client.get("/book/1").get_data(as_text=True)
    post(client, "/book/1/kindle-deleted")
    assert ondevice.kindle_to_delete("bob") == []
    db.device_book_clear("bob", 1, "kindle")
    db.set_device_prefs("bob", kindle_hint=0)
    assert ondevice.kindle_to_delete("bob") == [], "reminders switched off"


# ---- the settings, per reader ---------------------------------------------------------------------------------------
def test_each_reader_sets_their_devices_on_the_start_page(client, kobo, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    add_calibre_book(1, "Book 1", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    login(client, "bob", "bobpass1")
    html = client.get("/hub").get_data(as_text=True)
    assert "Your settings" in html and "Only the books I send" in html and "Keep my books private" in html
    html = post(client, "/hub", action="device_settings", kobo_send="choose", keep="1", kobo_finished="7",
                kindle_hint="1", private="1").get_data(as_text=True)
    p = db.get_prefs("bob")
    assert (p["kobo_finished"], p["private"], p["kindle_hint"]) == (7, True, True) and cwa.kobo_choose_only("bob")
    assert "only the books you send" in html and "7 days after you finish them" in html and "private" in html
    post(client, "/hub", action="device_settings", kobo_send="all", kobo_finished="", back="devices")
    assert db.get_prefs("bob")["kobo_finished"] is None and not cwa.kobo_choose_only("bob")
    assert client.post("/hub", data={"action": "device_settings", "kobo_send": "choose"}).status_code == 400, "no form token"

def test_a_reader_without_a_kobo_is_told_to_link_it_first(client, users):
    login(client, "bob", "bobpass1")
    assert "Link your Kobo first" in client.get("/hub").get_data(as_text=True)

def test_my_books_filters_on_my_kobo(client, kobo):
    for b in (1, 2):
        add_calibre_book(b, f"Book {b}", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    ondevice.take_off_kobo("bob", 2)
    login(client, "bob", "bobpass1")
    on = client.get("/library?kobo=on&view=list").get_data(as_text=True)
    off = client.get("/library?kobo=off&view=list").get_data(as_text=True)
    assert "Book 1" in on and "Book 2" not in on and "Book 2" in off and "Book 1" not in off

def test_the_book_page_sends_and_takes_off(client, kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    login(client, "bob", "bobpass1")
    assert "Take it off my Kobo" in client.get("/book/1").get_data(as_text=True)
    html = post(client, "/book/1/kobo-off").get_data(as_text=True)
    assert "It leaves your Kobo at its next sync" in html and "Send to my Kobo" in html
    html = post(client, "/book/1/kobo-back").get_data(as_text=True)
    assert "Put back" in html and "stays there" in html


# ---- keeping my books private ---------------------------------------------------------------------------------------
def test_a_private_readers_books_are_never_offered_to_anyone_else(users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    add_calibre_book(1, "Dune", "Frank Herbert", tags=["owner:bob"])
    assert share.find_ebook("Dune", "Frank Herbert")["owners"] == ["bob"]
    db.set_device_prefs("bob", private=1)
    assert share.find_ebook("Dune", "Frank Herbert") is None, "alice downloads her own copy"
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:bob", "owner:alice"])
    assert share.find_ebook("Emma", "Jane Austen")["owners"] == ["alice", "bob"], "alice shares hers"
    _comic_book(9, "One Piece", 5, ["Manga", "owner:bob"])
    assert comics.find_in_library("One Piece", 5, "manga") is None
    assert comics.find_in_library("One Piece", 5, "manga", viewer="bob")["book_id"] == 9, "bob still finds his own"

def test_a_private_readers_audiobooks_are_not_offered_either(users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    monkeypatch.setattr(absapi, "configured", lambda: True)
    monkeypatch.setattr(share, "_abs_items", lambda: [{"id": "a1", "media": {"tags": ["owner:bob"],
                        "metadata": {"title": "Project Hail Mary", "authorName": "Andy Weir"}}}])
    assert share.find_audiobook("Project Hail Mary", "Andy Weir")["item_id"] == "a1"
    db.set_device_prefs("bob", private=1)
    assert share.find_audiobook("Project Hail Mary", "Andy Weir") is None
    assert share.audiobook_owned_by("Project Hail Mary", "Andy Weir", "bob")["item_id"] == "a1"

def test_the_dashboard_counts_readers_device_notes_without_titles(kobo):
    add_calibre_book(1, "A Very Private Title", "A", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    _cwa("INSERT INTO archived_book(user_id, book_id, is_archived) VALUES(?, 1, 1)", kobo["bob"])
    texts = " ".join(n["text"] for n in crosscheck.run()["notes"])
    assert "bob 1" in texts and "A Very Private Title" not in texts


# ---- the privacy audit's findings, each fixed ----------------------------------------------------------------
def test_a_comic_page_never_names_another_readers_kobo(client, kobo, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_status", lambda o: {})
    db.set_devices("alice", ["kobo-sage"])
    db.set_devices("bob", ["kobo-clara-bw"])
    _comic_book(1, "Saga", 1, ["Comics", "owner:bob", "owner:alice"], formats=("cbz", "kepub"))
    db.comic_convert_result(1, True, made={"profile": "KoS", "colour": False, "layout": "portrait", "upscale": False})
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "Sage" not in html and "Kobo Clara BW" not in html.split("Change my devices")[0]

def test_another_readers_conversion_and_better_copy_stay_theirs(client, kobo):
    add_calibre_book(1, "Dune", "Frank Herbert", tags=["owner:bob", "owner:alice"])
    with db._conn() as c:
        c.execute("INSERT INTO convert_jobs(calibre_id, owner, src_fmt, dst_fmt, src_path, status, created, updated) "
                  "VALUES(1, 'alice', 'epub', 'azw3', 'x', 'pending', 0, 0)")
    db.open_replace(1, "alice")
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "AZW3: being made" not in html and "Looking for a better copy" not in html
    assert post(client, "/book/1/replace", action="cancel").status_code == 404, "only who asked stops it"
    assert db.replace_for_book(1)["status"] == "open"

def test_an_untagged_comic_is_offered_only_as_a_give_back_and_not_a_private_readers(users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    _comic_book(9, "One Piece", 5, ["Manga"])
    assert comics.find_in_library("One Piece", 5, "manga") is None, "an import under way"
    db.release_note(9, "its last reader removed it", [], removed_by="alice")
    assert comics.find_in_library("One Piece", 5, "manga")["released"] is True
    db.set_device_prefs("alice", private=1)
    assert comics.find_in_library("One Piece", 5, "manga") is None, "removed by a private reader: not offered"

def test_a_private_readers_removed_ebook_is_not_given_to_anyone(users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    add_calibre_book(1, "Dune", "Frank Herbert", tags=[])
    db.release_note(1, "its last reader removed it", [], removed_by="bob")
    assert share.find_ebook("Dune", "Frank Herbert")["released"] is True
    db.set_device_prefs("bob", private=1)
    assert share.find_ebook("Dune", "Frank Herbert") is None

def test_saving_settings_with_sharing_off_keeps_private(client, users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", False)
    db.set_device_prefs("bob", private=1)
    login(client, "bob", "bobpass1")
    html = client.get("/hub").get_data(as_text=True)
    assert 'name="private" value="1"' in html
    post(client, "/hub", action="device_settings", private="1", kindle_hint="1")
    assert db.get_prefs("bob")["private"] is True

def test_given_back_is_said_only_to_the_reader_who_removed_it(users, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    import bookreq
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=[])
    db.release_note(1, "its last reader removed it", [], removed_by="bob")
    rid, _ = bookreq.request("alice", "Mort", "Terry Pratchett")
    d = db.bookreq_get(rid)["detail"]
    assert "given back" not in d and "removed" not in d and "added to your library at once" in d

def test_a_book_the_portal_took_off_is_not_called_deleted_by_the_reader(client, kobo):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    _synced(kobo["bob"], 1)
    ondevice.take_off_kobo("bob", 1)
    _synced(kobo["bob"], 1)                      # the Kobo's sync delivered the removal
    login(client, "bob", "bobpass1")
    html = client.get("/book/1").get_data(as_text=True)
    assert "taken off it" in html and "you deleted it on the Kobo" not in html and "Send to my Kobo" in html
    assert not any(n["code"] == "kobo-deleted" for n in crosscheck.run()["notes"])
