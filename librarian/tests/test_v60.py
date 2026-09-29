"""v6.0: the start page and guides (home.), the reader's home page, My books filters, the portal script."""
import pytest
import config, db, cwa, home
import abs as absapi
from conftest import add_calibre_book, login, post
from test_follows import _cwa_reading
from test_comics import _comic_book



def test_the_start_page_links_every_site_and_shows_the_setup(client):
    login(client, "bob", "bobpass1")
    page = client.get("/hub").get_data(as_text=True)
    for url in (config.PORTAL_URL, config.BOOKS_URL, config.AUDIO_URL, config.SHELF_URL):
        assert url in page
    assert "Your setup" in page and "Kobo linked" in page and "Admin dashboard" not in page
    assert "For the admin" not in page

def test_every_guide_renders_and_the_admin_one_is_for_admins(client):
    import app as appmod
    login(client, "bob", "bobpass1")
    for key, title, _ in appmod.HELP_TOPICS:
        r = client.get(f"/help/{key}")
        if key == "admin" or (key == "comics" and not config.COMICS_ENABLED):
            assert r.status_code == 404, key
        else:
            assert r.status_code == 200 and title in r.get_data(as_text=True), key
    assert client.get("/help/nope").status_code == 404
    post(client, "/logout"); login(client, "admin", "adminpass1")
    assert client.get("/help/admin").status_code == 200
    assert "Admin dashboard" in client.get("/hub").get_data(as_text=True)

def test_the_start_page_needs_a_login(client):
    assert client.get("/hub").status_code == 302

def test_home_shows_reading_next_in_series_and_recent(client, monkeypatch):
    bob = cwa.get_user("bob")["id"]
    for bid, idx in ((1, 1), (2, 2), (3, 3)):
        _comic_book(bid, "Discworld", idx, ["owner:bob"])
    add_calibre_book(4, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(5, "Alice's Book", "Someone", tags=["owner:alice"])
    _cwa_reading(bob, [(1, 1)], bookmarks=[(4, 40.0)])
    h = home.build("bob")
    assert [b["id"] for b in h["reading"]] == [4]
    assert [(b["series"], b["index"]) for b in h["next"]] == [("Discworld", 2.0)]
    assert 5 not in [b["id"] for b in h["recent"]], "only the reader's own books"
    login(client, "bob", "bobpass1")
    page = client.get("/").get_data(as_text=True)
    assert "Continue reading" in page and "Next in your series" in page and "Recently added" in page
    assert client.get("/?mode=catalogs").status_code == 200

def test_listening_comes_from_audiobookshelf(users, monkeypatch):
    home._CACHE.clear()
    monkeypatch.setattr(absapi, "configured", lambda: True)
    calls = []
    monkeypatch.setattr(absapi, "list_users", lambda token=None: calls.append("users") or [{"username": "bob", "id": "u1"}])
    monkeypatch.setattr(absapi, "progress", lambda uid, token=None: [
        {"libraryItemId": "li1", "currentTime": 1800, "duration": 7200, "progress": 0.25, "lastUpdate": 5},
        {"libraryItemId": "li2", "currentTime": 7200, "duration": 7200, "progress": 1, "isFinished": True, "lastUpdate": 9}])
    monkeypatch.setattr(absapi, "item_meta", lambda item, token=None: {"title": "Hail Mary", "author": "Weir", "duration": 7200})
    (a,) = home.listening("bob")
    assert (a["id"], a["pct"], a["left_min"]) == ("li1", 25, 90)
    home.listening("bob"); home.listening("bob")
    assert calls == ["users"], "cached: the home page does not ask Audiobookshelf on every view"

def test_my_books_filters_and_sorts(client):
    bob = cwa.get_user("bob")["id"]
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Emma", "Jane Austen", tags=["owner:bob"])
    _comic_book(3, "One Piece", 1, ["Manga", "owner:bob"])
    _cwa_reading(bob, [(1, 1)])
    login(client, "bob", "bobpass1")
    page = lambda **kw: client.get("/library", query_string=kw).get_data(as_text=True)
    assert "Mort" in page(status="read") and "Emma" not in page(status="read")
    assert "Mort" not in page(status="unread") and "Emma" in page(status="unread")
    assert "One Piece" in page(kind="comics") and "Emma" not in page(kind="comics")
    assert "One Piece" not in page(kind="books")
    t = page(sort="title", view="list")
    assert t.index("Emma") < t.index("Mort")
    assert "Nothing matches these filters" in page(q="zzzz")

def test_the_portal_runs_only_its_own_script(client):
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert client.get("/static/app.js").status_code == 200

def test_a_missing_cover_is_a_placeholder_on_the_shelves(client):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    login(client, "bob", "bobpass1")
    r = client.get("/book/1/cover?ph=1")
    assert r.status_code == 200 and r.mimetype in ("image/svg+xml", "image/jpeg")


# ---- Get it for audiobooks ---------------------------------------------------------------------------
import time
import audiorel, bookreq, shelfmark_api, share
from test_v591 import FakeHC  # noqa: F401  (keeps the Hardcover fake importable in one place)

W = {"title": "Project Hail Mary", "author": "Andy Weir", "language": "en"}

@pytest.mark.parametrize("title,fmt,ok,why", [
    ("Andy Weir - Project Hail Mary (Unabridged) [M4B]", None, True, "exact"),
    ("Project Hail Mary by Andy Weir narrated by Ray Porter MP3 64kbps", None, True, "exact"),
    ("Andy Weir - Project Hail Mary", "m4b", True, "exact"),
    ("Andy Weir - Project Hail Mary.epub", None, False, "an ebook (epub), not the audiobook"),
    ("Andy Weir - Project Hail Mary (Abridged) mp3", None, False, "looks abridged or adapted"),
    ("The Martian - Andy Weir [M4B]", None, False, "another title"),
    ("Andy Weir Collection Books 1-3 m4b", None, False, "looks like a collection, not the single book"),
])
def test_an_audiobook_release_is_judged_like_a_book_with_audio_formats(title, fmt, ok, why):
    got = audiorel.judge(W, {"title": title, "format": fmt, "protocol": "usenet", "size_bytes": 400_000_000})
    assert (got[0], got[2]) == (ok, why), title

def test_m4b_unabridged_is_preferred_and_sure(users):
    rels = [{"source_id": "a", "title": "Andy Weir - Project Hail Mary MP3", "protocol": "usenet", "size_bytes": 3e8},
            {"source_id": "b", "title": "Andy Weir - Project Hail Mary (Unabridged) [M4B]", "protocol": "usenet", "size_bytes": 3e8}]
    assert audiorel.pick(W, rels)[0]["source_id"] == "b"
    assert audiorel.sure(W, rels[1]) and not audiorel.sure(W, rels[0])

class Shelf:
    ShelfmarkError = shelfmark_api.ShelfmarkError
    def __init__(self, releases):
        self.releases, self.searched, self.queued = releases, [], []
    def search_releases(self, q, content_type="ebook", book_id="comic"):
        self.searched.append((q, content_type)); return list(self.releases)
    def user_id(self, name):
        return 12
    def queue_release(self, rel, uid, content_type="ebook"):
        self.queued.append((rel["source_id"], content_type)); return {}
    def failed(self, queue):
        return []

@pytest.fixture
def audio_on(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(share, "find_audiobook", lambda t, a="", exclude=(): None)
    monkeypatch.setattr(share, "audiobook_owned_by", lambda t, a, o, exclude=(): None)

def test_an_audiobook_request_searches_audiobooks_and_queues_them_as_audiobooks(audio_on):
    rid, what = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    assert what == "queued" and db.bookreq_get(rid)["kind"] == "audio"
    assert bookreq.request("bob", "Project Hail Mary", "Andy Weir")[1] == "queued", "the ebook is another request"
    s = Shelf([{"source_id": "x", "title": "Andy Weir - Project Hail Mary (Unabridged) [M4B]", "protocol": "usenet", "size_bytes": 3e8}])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "confirm"
    assert s.searched[0][1] == "audiobook" and "M4B audio" in db.bookreq_get(rid)["reasons"]
    assert bookreq.confirm(rid, s) == "downloading" and s.queued == [("x", "audiobook")]

def test_the_familys_audiobook_is_shared_not_downloaded(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    given = []
    monkeypatch.setattr(share, "find_audiobook", lambda t, a="", exclude=(): {"item_id": "li9", "how": "title+author", "owners": ["alice"]})
    monkeypatch.setattr(share, "give_audiobook", lambda m, o: given.append((m["item_id"], o)))
    rid, what = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    assert what == "shared" and given == [("li9", "bob")] and db.bookreq_get(rid)["abs_item"] == "li9"

def _m4b_meta(monkeypatch, **meta):
    monkeypatch.setattr(bookreq, "audio_meta", lambda path, limit=400: dict({"files": 1}, **meta))

def test_an_audiobook_that_is_much_shorter_than_the_book_is_held(audio_on, monkeypatch, tmp_path):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", release_title="Andy Weir - Project Hail Mary (Unabridged) [M4B]")
    monkeypatch.setattr(bookreq, "_expected_seconds", lambda req: 58253)
    box = tmp_path / "box"; box.mkdir()
    f = box / "Andy Weir - Project Hail Mary (Unabridged) [M4B].m4b"; f.write_bytes(b"x")
    _m4b_meta(monkeypatch, seconds=3 * 3600, album="Project Hail Mary")
    note = bookreq.check_audio_arrival(str(f), "bob")
    assert note.startswith("skipped: held") and "abridged or incomplete" in note
    assert db.bookreq_get(rid)["status"] == "held" and not f.exists()

def test_the_right_audiobook_goes_in(audio_on, monkeypatch, tmp_path):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", release_title="Andy Weir - Project Hail Mary (Unabridged) [M4B]")
    monkeypatch.setattr(bookreq, "_expected_seconds", lambda req: 58253)
    d = tmp_path / "Andy Weir - Project Hail Mary (Unabridged) [M4B]"; d.mkdir()
    _m4b_meta(monkeypatch, seconds=58000, album="Project Hail Mary: A Novel")
    assert bookreq.check_audio_arrival(str(d), "bob") is None and d.exists()
    _m4b_meta(monkeypatch, seconds=58000, album="The Martian")
    assert "its tags say" in bookreq.check_audio_arrival(str(d), "bob")

def test_audio_meta_reads_nothing_from_a_non_audio_file(tmp_path):
    f = tmp_path / "x.m4b"; f.write_bytes(b"not audio")
    m = bookreq.audio_meta(str(f))
    assert m.get("seconds", 0) == 0

def test_arrival_in_audiobookshelf_closes_the_request(audio_on, monkeypatch):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", release_title="x", queued_at=time.time())
    monkeypatch.setattr(share, "audiobook_owned_by", lambda t, a, o, exclude=(): {"item_id": "li5", "owners": ["bob"]})
    assert bookreq.watch_downloads(Shelf([]), {}) == (1, 0)
    r = db.bookreq_get(rid)
    assert r["status"] == "done" and r["abs_item"] == "li5"

def test_wrong_audiobook_untags_it_and_never_counts_it_again(audio_on, monkeypatch):
    untagged = []
    monkeypatch.setattr(absapi, "untag_item", lambda item, tag, token=None: untagged.append((item, tag)) or True)
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="done", abs_item="li5", release_title="Andy Weir - PHM [M4B]")
    bookreq.wrong_audiobook("bob", "li5")
    r = db.bookreq_get(rid)
    assert r["status"] == "queued" and "abs:li5" in r["blocked"] and untagged == [("li5", "owner:bob")]
    monkeypatch.setattr(share, "audiobook_owned_by", lambda t, a, o, exclude=(): None if "li5" in exclude else {"item_id": "li5", "owners": ["bob"]})
    assert bookreq.search_once(db.bookreq_get(rid), Shelf([])) == "queued", "the wrong copy is not 'already yours'"


# ---- Want to Read, phone notifications, the admin dashboard -------------------------------------------
import hcwant, notify, dash

def test_want_to_read_records_the_list_first_then_requests_what_is_new(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    lst = {"books": [{"id": 1, "title": "Dune", "author": "Frank Herbert"}]}
    monkeypatch.setattr(hcwant, "want_list", lambda token: list(lst["books"]))
    assert hcwant.sync_owner("bob", "tok", "ebook") == 0 and db.bookreq_list("bob") == []
    assert "already on your list" in hcwant.note("bob")
    lst["books"].insert(0, {"id": 2, "title": "Children of Dune", "author": "Frank Herbert"})
    assert hcwant.sync_owner("bob", "tok", "both") == 2
    kinds = sorted(r["kind"] for r in db.bookreq_list("bob"))
    assert kinds == ["audio", "ebook"] and all(r["hardcover_id"] == "2" for r in db.bookreq_list("bob"))
    assert hcwant.sync_owner("bob", "tok", "both") == 0, "never twice"
    made, err = hcwant.request_backlog("bob", "ebook")
    assert made == 1 and err is None and any(r["title"] == "Dune" for r in db.bookreq_list("bob"))

def test_want_to_read_only_for_readers_who_turned_it_on(users, monkeypatch):
    called = []
    monkeypatch.setattr(hcwant, "sync_owner", lambda o, t, k, now=None: called.append(o) or 0)
    monkeypatch.setattr(cwa, "hardcover_tokens", lambda: {"bob": "t1", "alice": "t2"})
    db.set_prefs_v6("bob", hc_want=1, hc_want_kind="audio")
    hcwant.sync_once()
    assert called == ["bob"] and db.get_prefs("bob")["hc_want_kind"] == "audio"

def test_reader_notifications_go_only_to_readers_who_turned_them_on(users, monkeypatch):
    sent = []
    class T:
        def __init__(self, target, daemon): self.t = target
        def start(self): self.t()
    monkeypatch.setattr(notify.threading, "Thread", T)
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=8: sent.append((req.full_url, dict(req.header_items()))))
    assert notify.reader("bob", "t", "x") is False and sent == []
    db.set_prefs_v6("bob", ntfy_topic="lib-abc")
    assert notify.reader("bob", "Dune", "Dune is in your library.", click="https://x/", tags="books")
    url, hdr = sent[-1]
    assert url == "https://ntfy.sh/lib-abc" and hdr["Title"] == "Dune" and hdr["Click"] == "https://x/"
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://ntfy.tailnet.example.test/admin-alerts")
    notify.reader("bob", "t", "x")
    assert sent[-1][0] == "https://ntfy.sh/lib-abc", "never the admin's own (maybe private) alert server by default"
    monkeypatch.setattr(config, "READER_NTFY_URL", "https://ntfy.family.example.test")
    notify.reader("bob", "t", "x")
    assert sent[-1][0] == "https://ntfy.family.example.test/lib-abc"

def test_devices_turns_phone_notifications_and_want_to_read_on(client, monkeypatch):
    login(client, "bob", "bobpass1")
    post(client, "/devices", action="ntfy_on")
    topic = db.get_prefs("bob")["ntfy_topic"]
    assert topic.startswith("lib-") and topic in client.get("/devices").get_data(as_text=True)
    post(client, "/devices", action="ntfy_new")
    assert db.get_prefs("bob")["ntfy_topic"] != topic
    post(client, "/devices", action="ntfy_off")
    assert db.get_prefs("bob")["ntfy_topic"] == ""
    post(client, "/devices", action="hcwant", hc_want="1", hc_want_kind="both")
    p = db.get_prefs("bob")
    assert p["hc_want"] and p["hc_want_kind"] == "both"

def test_the_admin_sees_what_needs_them(client, monkeypatch):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="held")
    rid2, _ = bookreq.request("bob", "Emma", "Jane Austen")
    db.bookreq_update(rid2, status="downloading", queued_at=time.time() - 3 * 86400)
    texts = [t for _s, t, _l in dash.needs()]
    assert any("held for a reader" in t for t in texts) and any("2+ days" in t for t in texts)
    login(client, "admin", "adminpass1")
    page = client.get("/admin").get_data(as_text=True)
    assert "What needs you" in page and "held for a reader" in page and "This week" in page
