"""v5.8.3: one-tap book requests (bookreq.py, bookrel.py) and the book series / author pages."""
import os, time, zipfile
import pytest
import config, db, bookrel, bookreq, follows, hardcover, notify, shelfmark_api, worker
from conftest import add_calibre_book, login, post, make_epub

WIND = {"title": "Wind and Truth", "author": "Brandon Sanderson", "language": "en", "series": "The Stormlight Archive"}
DUNE = {"title": "Dune", "author": "Frank Herbert", "language": "en"}


# ---- which release (bookrel) --------------------------------------------------------------------
@pytest.mark.parametrize("want,title,ok,why", [
    (WIND, "Brandon Sanderson - Wind and Truth (Stormlight Archive 5) epub", True, "exact"),
    (WIND, "Wind and Truth by Brandon Sanderson EPUB", True, "exact"),
    (WIND, "Brandon.Sanderson.-.Wind.and.Truth.2024.RETAIL.EPUB.eBook-DiVU", True, "exact"),
    (WIND, "Wind and Truth: Book Five of the Stormlight Archive - Brandon Sanderson [EPUB]", True, "exact"),
    (WIND, "Brandon Sanderson - Wind and Truth (Unabridged) [M4B]", False, "a m4b file"),
    (WIND, "Brandon Sanderson - Wind and Truth.pdf", False, "a pdf file"),
    (WIND, "Brandon Sanderson - Stormlight Archive Books 1-5 EPUB", False, "a pack of several books"),
    (DUNE, "Frank Herbert - Dune Messiah (1969) epub", False, "another book (messiah)"),
    (DUNE, "The Road to Dune - Frank Herbert", False, "another book (road to)"),
    (DUNE, "Dune epub", False, "no author, and the title is too short to be sure"),
    (DUNE, "Summary of Dune by Frank Herbert", False, "looks abridged or adapted"),
    (DUNE, "Frank Herbert - Dune (French) epub", False, "in another language (fr)"),
    ({"title": "Harry Potter and the Philosopher's Stone", "author": "J.K. Rowling"},
     "J.K. Rowling - Harry Potter and the Philosophers Stone epub", True, "exact"),
    ({"title": "Guards! Guards!", "author": "Terry Pratchett", "series": "Discworld"},
     "Terry Pratchett - Discworld 08 - Guards! Guards! (1989) [EPUB]", True, "exact"),
    (DUNE, "Dune [Frank Herbert] epub", True, "exact"),
])
def test_a_book_release_is_judged_on_title_author_edition_format_and_language(want, title, ok, why):
    got_ok, _score, got_why = bookrel.judge(want, {"title": title, "protocol": "usenet", "size_bytes": 2_000_000})
    assert (got_ok, got_why) == (ok, why), title

def test_epub_beats_mobi_and_a_dead_torrent_is_never_picked():
    rels = [{"source_id": "a", "title": "Frank Herbert - Dune mobi", "protocol": "usenet"},
            {"source_id": "b", "title": "Frank Herbert - Dune epub", "protocol": "torrent", "seeders": 0},
            {"source_id": "c", "title": "Frank Herbert - Dune epub", "protocol": "torrent", "seeders": 4}]
    best, notes = bookrel.pick(DUNE, rels)
    assert best["source_id"] == "c"
    assert ("Frank Herbert - Dune epub", False, 0, "no seeders") in notes
    assert bookrel.pick(DUNE, rels, exclude={"c"})[0]["source_id"] == "a"

def test_a_release_in_another_language_by_shelfmarks_own_field_is_refused():
    assert bookrel.judge(DUNE, {"title": "Frank Herbert - Dune epub", "language": "de"})[2] == "in another language (de)"

def test_queries_ask_for_the_title_with_the_surname_then_alone():
    assert bookrel.queries(WIND) == ["Wind and Truth sanderson", "Wind and Truth"]


# ---- a request, through Shelfmark ----------------------------------------------------------------
class Shelf:
    ShelfmarkError = shelfmark_api.ShelfmarkError
    def __init__(self, releases, uid=12):
        self.releases, self.uid, self.searched, self.queued = releases, uid, [], []
    def search_releases(self, q, content_type="ebook", book_id="comic"):
        self.searched.append((q, book_id))
        return list(self.releases)
    def user_id(self, name):
        return self.uid
    def queue_release(self, rel, uid, content_type="ebook"):
        self.queued.append((rel["source_id"], uid))
        return {"status": "queued"}
    def failed(self, queue):
        return (queue or {}).get("failed", [])

@pytest.fixture
def open_(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "FAMILY_SHARING", True)

def test_a_book_the_family_has_is_shared_not_downloaded(open_):
    add_calibre_book(7, "Dune", "Frank Herbert", tags=["owner:alice"])
    rid, what = bookreq.request("bob", "Dune", "Frank Herbert")
    assert what == "shared" and db.bookreq_get(rid)["calibre_id"] == 7
    (job,) = db.pending_tag_pushes()
    assert (job["calibre_id"], job["owner"], job["share"]) == (7, "bob", 1)
    assert bookreq.request("alice", "Dune", "Frank Herbert")[1] == "owned"

def test_approvals_hold_a_readers_book_for_the_admin(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append((ev, r["status"])))
    rid, what = bookreq.request("bob", "Dune", "Frank Herbert")
    assert what == "pending" and told == [("requested", "pending")]
    assert bookreq.request("bob", "Dune", "Frank Herbert")[1] == "exists", "never twice"
    assert bookreq.request("admin", "Dune", "Frank Herbert")[1] == "queued"

def test_the_copy_found_waits_for_the_reader_then_is_queued_in_shelfmark_as_them(open_, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append((ev, r.get("status"))))
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    s = Shelf([{"source_id": "x1", "title": "Frank Herbert - Dune (1965) [EPUB]", "protocol": "usenet",
                "size_bytes": 2_000_000, "indexer": "NZBgeek"}])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "confirm"
    r = db.bookreq_get(rid)
    assert s.queued == [] and s.searched == [("Dune herbert", "book")], "nothing downloads before the reader says so"
    assert r["candidate"]["source_id"] == "x1" and "the title and the author match" in r["reasons"]
    assert db.bookreq_waiting("bob") == 1
    assert bookreq.confirm(rid, s) == "downloading"
    r = db.bookreq_get(rid)
    assert s.queued == [("x1", 12)] and r["status"] == "downloading" and r["tried"] == ["x1"] and r["candidate"] is None
    assert ("requested", "queued") in told

def test_sure_mode_downloads_only_a_certain_pick_and_never_after_a_no(open_, monkeypatch):
    monkeypatch.setattr(config, "BOOK_CONFIRM", "sure")
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    s = Shelf([{"source_id": "r", "title": "Frank Herbert - Dune RETAIL epub", "protocol": "usenet"}])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "downloading"
    rid2, _ = bookreq.request("bob", "Children of Dune", "Frank Herbert")
    s2 = Shelf([{"source_id": "m", "title": "Frank Herbert - Children of Dune mobi", "protocol": "usenet"}])
    assert bookreq.search_once(db.bookreq_get(rid2), s2) == "confirm", "a MOBI, not retail: asked"
    db.bookreq_update(rid, status="queued", blocked=["something"])
    assert bookreq.search_once(db.bookreq_get(rid), Shelf([{"source_id": "r2", "title": "Frank Herbert - Dune RETAIL epub", "protocol": "usenet"}])) == "confirm"

def test_not_it_is_never_offered_again_from_any_indexer(open_):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    s = Shelf([{"source_id": "a", "title": "Frank Herbert - Dune epub", "protocol": "usenet"}])
    bookreq.search_once(db.bookreq_get(rid), s)
    bookreq.reject(rid)
    r = db.bookreq_get(rid)
    assert r["status"] == "queued" and r["candidate"] is None and "a" in r["tried"]
    again = Shelf([{"source_id": "b", "title": "Frank Herbert - Dune epub", "protocol": "torrent", "seeders": 9},
                   {"source_id": "c", "title": "Frank Herbert - Dune (Retail) azw3", "protocol": "usenet"}])
    assert bookreq.search_once(db.bookreq_get(rid), again) == "confirm"
    assert db.bookreq_get(rid)["candidate"]["source_id"] == "c", "the same name from another indexer is not it either"

def test_nothing_right_waits_and_looks_again_then_points_to_shelfmark(open_):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert", now=1000.0)
    s = Shelf([{"source_id": "m", "title": "Frank Herbert - Dune Messiah epub", "protocol": "usenet"}])
    assert bookreq.search_once(db.bookreq_get(rid), s, now=1000.0) == "queued"
    r = db.bookreq_get(rid)
    assert r["next_try"] == 1000.0 + 3600 and "another book (messiah)" in r["detail"] and s.queued == []
    assert bookreq.search_once(db.bookreq_get(rid), s, now=1000.0 + 15 * 86400) == "not-found"
    assert "Pick in Shelfmark" in db.bookreq_get(rid)["detail"]

def test_an_arrival_closes_the_request_and_a_failed_download_tries_another(open_):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="downloading", release_title="Frank Herbert - Dune epub", queued_at=time.time())
    assert bookreq.watch_downloads(Shelf([]), {"failed": [{"title": "Frank Herbert - Dune epub"}]}) == (0, 1)
    assert db.bookreq_get(rid)["status"] == "queued" and "failed in Shelfmark" in db.bookreq_get(rid)["detail"]
    db.bookreq_update(rid, status="downloading", queued_at=time.time())
    add_calibre_book(8, "Dune", "Frank Herbert", tags=["owner:bob"])
    assert bookreq.watch_downloads(Shelf([]), {}) == (1, 0)
    assert db.bookreq_get(rid)["status"] == "done" and db.bookreq_get(rid)["calibre_id"] == 8

def test_a_download_shelfmark_completed_is_never_replaced_by_another(open_):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert", now=1000.0)
    db.bookreq_update(rid, status="downloading", release_title="Frank Herbert - Dune epub", queued_at=1000.0)
    done = {"complete": {"t1": {"title": "Frank Herbert - Dune epub"}}}
    assert bookreq.watch_downloads(Shelf([]), done, now=2000.0) == (0, 0)
    assert db.bookreq_get(rid)["downloaded"] == 2000.0
    assert bookreq.watch_downloads(Shelf([]), {}, now=2000.0 + 25 * 3600) == (0, 0), "no second download"
    r = db.bookreq_get(rid)
    assert r["status"] == "done" and "look in My books" in r["detail"]

def test_the_readers_own_copy_counts_with_family_sharing_off(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "FAMILY_SHARING", False)
    add_calibre_book(7, "Dune", "Frank Herbert", tags=["owner:bob"])
    assert bookreq.request("bob", "Dune", "Frank Herbert")[1] == "owned"
    assert bookreq.request("alice", "Dune", "Frank Herbert")[1] == "queued"

# ---- the arrival check ------------------------------------------------------------------------------
def _epub(path, title, author, language="en", isbn=None):
    make_epub(path, title, author)
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    opf = parts["OEBPS/content.opf"].decode().replace("<dc:language>en</dc:language>", f"<dc:language>{language}</dc:language>")
    if isbn:
        opf = opf.replace("</metadata>", f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier></metadata>')
    parts["OEBPS/content.opf"] = opf.encode()
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), parts.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for n, b in parts.items():
            z.writestr(n, b, compress_type=zipfile.ZIP_DEFLATED)
    return path

@pytest.fixture
def downloading(open_):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert", hardcover_id="312")
    db.bookreq_update(rid, status="downloading", release_title="Frank Herbert - Dune (1965) [EPUB]", queued_at=time.time())
    box = os.path.join(config.DROPBOX_DIR, "bob")
    os.makedirs(box, exist_ok=True)
    return rid, box

def test_the_right_file_goes_in(downloading):
    rid, box = downloading
    p = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), "Dune", "Frank Herbert")
    assert bookreq.check_arrival(p, "bob") is None and os.path.exists(p)
    assert "checked: it is this book" in db.bookreq_get(rid)["detail"]

@pytest.mark.parametrize("title,author,language,why", [
    ("Dune Messiah", "Frank Herbert", "en", "the file says it is “Dune Messiah”"),
    ("Dune", "Brian Herbert", "en", "by Brian Herbert"),
    ("Dune", "Frank Herbert", "fr", "in another language (fr)"),
])
def test_a_file_that_is_not_the_book_is_held_not_imported(downloading, monkeypatch, title, author, language, why):
    rid, box = downloading
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append((ev, r.get("detail"))))
    p = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), title, author, language)
    note = bookreq.check_arrival(p, "bob")
    assert note.startswith("skipped: held") and why in note
    r = db.bookreq_get(rid)
    assert r["status"] == "held" and not os.path.exists(p) and os.path.isfile(r["held_path"])
    assert r["held_meta"]["title"] == title and told and told[0][0] == "error"

def test_an_isbn_of_the_book_in_the_readers_language_settles_it(downloading, monkeypatch):
    rid, box = downloading
    monkeypatch.setattr(hardcover, "editions", lambda bid: [{"isbns": ["9780441172719"], "language": "en"},
                                                            {"isbns": ["9782266233200"], "language": "fr"}])
    p = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), "Dune: Deluxe Edition Special", "F. Herbert", isbn="9780441172719")
    assert bookreq.check_arrival(p, "bob") is None, "an English edition of this book, whatever its file calls it"
    q = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), "Dune", "Frank Herbert", "fr", isbn="9782266233200")
    assert "in another language (fr)" in bookreq.check_arrival(q, "bob"), "the French edition is still French"

def test_a_file_nobody_asked_for_is_imported_as_usual(downloading):
    _rid, box = downloading
    p = _epub(os.path.join(box, "Jane Austen - Emma.epub"), "Emma", "Jane Austen")
    assert bookreq.check_arrival(p, "bob") is None

def test_keep_it_anyway_imports_it_unchecked_and_not_it_looks_again(downloading):
    rid, box = downloading
    p = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), "Dune Messiah", "Frank Herbert")
    bookreq.check_arrival(p, "bob")
    bookreq.keep(rid)
    r = db.bookreq_get(rid)
    assert r["status"] == "downloading" and r["skip_check"] == 1 and os.path.exists(p)
    assert bookreq.check_arrival(p, "bob") is None, "kept: not checked again"
    db.bookreq_update(rid, skip_check=0)
    bookreq.check_arrival(p, "bob")
    held = db.bookreq_get(rid)["held_path"]
    bookreq.reject(rid)
    r = db.bookreq_get(rid)
    assert r["status"] == "queued" and not os.path.exists(held)
    assert bookrel.blocked_key("Frank Herbert - Dune (1965) [EPUB]") in r["blocked"]

def test_the_worker_holds_the_file_before_the_import(downloading, monkeypatch):
    rid, box = downloading
    imported = []
    monkeypatch.setattr(worker, "ingest_local_file", lambda p, owner, rid: imported.append(p) or "ok")
    p = _epub(os.path.join(box, "Frank Herbert - Dune (1965) [EPUB].epub"), "Dune Messiah", "Frank Herbert")
    note = worker._ingest_file_entry(p, "bob", 1)
    assert note.startswith("skipped:") and imported == [] and worker._status_for(note) == "skipped"


# ---- Wrong book ---------------------------------------------------------------------------------------
def test_wrong_book_takes_it_out_and_never_counts_that_copy_again(open_, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append((ev, r.get("detail"))))
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    add_calibre_book(9, "Dune", "Frank Herbert", tags=["owner:bob"])
    db.bookreq_update(rid, status="done", calibre_id=9, release_title="Frank Herbert - Dune epub", release_id="z")
    with pytest.raises(bookreq.BookRequestError):
        bookreq.wrong_book("alice", 9)
    bookreq.wrong_book("bob", 9)
    r = db.bookreq_get(rid)
    assert r["status"] == "queued" and "book:9" in r["blocked"] and "z" in r["tried"] and r["calibre_id"] is None
    assert db.untag_pending(9, "bob")
    assert told[-1][0] == "error" and "wrong book reported" in told[-1][1]
    s = Shelf([{"source_id": "y", "title": "Frank Herbert - Dune (Retail) azw3", "protocol": "usenet"}])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "confirm", "the wrong copy in the library is not 'already yours'"

def test_a_book_notice_is_one_tap_now(open_):
    fid, _ = db.follow_add("bob", "author", "hardcover", "9", "Frank Herbert")
    nid = db.notice_add("bob", fid, "2", "Dune", "", {"type": "book", "title": "Dune", "author": "Frank Herbert"})
    assert follows.act("bob", nid, "request") == ("queued", None)
    (r,) = db.bookreq_list("bob")
    assert (r["title"], r["author"], r["notice_id"], r["status"]) == ("Dune", "Frank Herbert", nid, "queued")
    assert db.notice_get(nid)["status"] == "requested"


# ---- the pages ---------------------------------------------------------------------------------------
@pytest.fixture
def shelf_on(client, monkeypatch):
    monkeypatch.setattr(config, "SHELFMARK_SVC_USER", "svc")
    monkeypatch.setattr(config, "SHELFMARK_SVC_PASS", "x")
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc")
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    return client

def test_a_followed_name_opens_its_books_with_what_the_reader_has(shelf_on, monkeypatch):
    books = [{"id": "1", "title": "Dune", "date": "1965-08-01", "position": 1, "author": "Frank Herbert"},
             {"id": "2", "title": "Dune Messiah", "date": "1969-01-01", "position": 2, "author": "Frank Herbert"},
             {"id": "3", "title": "Children of Dune", "date": "1976-01-01", "position": 3, "author": "Frank Herbert"},
             {"id": "4", "title": "Dune Book Nine", "date": "2099-01-01", "position": 9, "author": "Frank Herbert"}]
    asked = []
    monkeypatch.setattr(hardcover, "series_books", lambda sid: (asked.append(sid), ("Dune", False, list(books)))[1])
    add_calibre_book(5, "Dune", "Frank Herbert", tags=["owner:bob"])
    add_calibre_book(6, "Dune Messiah", "Frank Herbert", tags=["owner:alice"])
    fid, _ = db.follow_add("bob", "book-series", "hardcover", "44", "Dune")
    login(shelf_on, "bob", "bobpass1")
    r = shelf_on.get(f"/following/{fid}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/books/series/44")
    page = shelf_on.get("/books/series/44").get_data(as_text=True)
    assert 'href="/book/5"' in page and "in your library" in page
    assert "in the family library" in page and "Add to mine" in page
    assert "Get it" in page and "Pick" in page and "not out yet" in page
    shelf_on.get("/books/series/44")
    assert asked == ["44"], "Hardcover is asked once; the page is cached"
    post(shelf_on, "/books/request", title="Children of Dune", author="Frank Herbert", series="Dune",
         hardcover_id="3", back="/books/series/44")
    assert "looking for a copy" in shelf_on.get("/books/series/44").get_data(as_text=True)
    (req,) = [r for r in db.bookreq_list("bob") if r["title"] == "Children of Dune"]
    db.bookreq_update(req["id"], status="confirm", candidate={"source_id": "q", "title": "Frank Herbert - Children of Dune epub",
                                                               "size_bytes": 3 * 1048576, "indexer": "NZBgeek"},
                      reasons=["the title and the author match", "EPUB file"])
    assert "a copy found: confirm it" in shelf_on.get("/books/series/44").get_data(as_text=True)
    home = shelf_on.get("/").get_data(as_text=True)
    assert "1 book waiting for your answer" in home and "Requests (1)" in home
    status = shelf_on.get("/status").get_data(as_text=True)
    assert "Books found for you through Shelfmark" in status and "Frank Herbert - Children of Dune epub" in status
    assert "Yes, that one" in status and "NZBgeek" in status and "3.0 MB" in status
    queued = []
    monkeypatch.setattr(shelfmark_api, "user_id", lambda name: 12)
    monkeypatch.setattr(shelfmark_api, "queue_release", lambda rel, uid, content_type="ebook": queued.append((rel["source_id"], uid)))
    post(shelf_on, f"/books/requests/{req['id']}/yes")
    assert queued == [("q", 12)] and db.bookreq_get(req["id"])["status"] == "downloading"
    post(shelf_on, "/logout"); login(shelf_on, "alice", "alicepass1")
    assert shelf_on.get(f"/following/{fid}").status_code == 404, "not someone else's"

def test_a_comic_follow_opens_its_series_page(shelf_on):
    fid, _ = db.follow_add("bob", "comic", "mangaupdates", "77", "One Piece")
    login(shelf_on, "bob", "bobpass1")
    assert shelf_on.get(f"/following/{fid}").headers["Location"].endswith("/comics/series/mangaupdates/77")

def test_readers_cancel_their_own_book_requests_only(shelf_on):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    login(shelf_on, "alice", "alicepass1")
    assert post(shelf_on, f"/books/requests/{rid}/cancel").status_code == 404
    post(shelf_on, "/logout"); login(shelf_on, "bob", "bobpass1")
    assert post(shelf_on, f"/books/requests/{rid}/approve").status_code == 400, "only an admin approves"
    post(shelf_on, f"/books/requests/{rid}/cancel")
    assert db.bookreq_get(rid)["status"] == "cancelled"

def test_wrong_book_is_offered_only_on_a_book_that_came_from_get_it(shelf_on):
    add_calibre_book(9, "Dune", "Frank Herbert", tags=["owner:bob"])
    add_calibre_book(10, "Emma", "Jane Austen", tags=["owner:bob"])
    rid, _ = bookreq.request("alice", "Dune", "Frank Herbert")
    db.bookreq_update(rid, owner="bob", status="done", calibre_id=9, release_title="Frank Herbert - Dune epub")
    login(shelf_on, "bob", "bobpass1")
    assert "Wrong book" in shelf_on.get("/book/9").get_data(as_text=True)
    assert "Wrong book" not in shelf_on.get("/book/10").get_data(as_text=True)
    assert post(shelf_on, "/book/10/wrong").status_code == 404
    post(shelf_on, "/book/9/wrong")
    assert db.bookreq_get(rid)["status"] == "queued" and db.untag_pending(9, "bob")

def test_only_the_reader_answers_for_their_copy(shelf_on):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="confirm", candidate={"source_id": "q", "title": "Frank Herbert - Dune epub"})
    login(shelf_on, "admin", "adminpass1")
    assert post(shelf_on, f"/books/requests/{rid}/yes").status_code == 400, "not even the admin says yes for bob"

def test_the_admins_requests_page_says_shelfmark_is_down_instead_of_crashing(shelf_on):
    login(shelf_on, "admin", "adminpass1")
    r = shelf_on.get("/status")
    assert r.status_code == 200 and "Could not read Shelfmark" in r.get_data(as_text=True)
