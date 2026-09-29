"""v5.8: following series and authors (New for you), reading status from Calibre-Web, AniList."""
import datetime, json, time
import pytest
import config, db, cwa, follows, hardcover, comicmeta, anilist, notify
from conftest import add_calibre_book, calibre_conn, login, post

TODAY = datetime.date.today()
PAST, FUTURE = (TODAY - datetime.timedelta(days=3)).isoformat(), (TODAY + datetime.timedelta(days=30)).isoformat()
SERIES = {"provider": "mangaupdates", "id": "77", "name": "One Piece", "kind": "manga", "year": 1997,
          "publisher": "VIZ", "authors": ["ODA Eiichiro"], "desc": "", "cover": None}


def _items(n, dated=None):
    return [{"id": f"77:v{i}", "number": str(i), "label": f"Vol. {i}", "date": (dated or {}).get(i), "cover": None}
            for i in range(1, n + 1)]


@pytest.fixture
def on(users, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)


# ---- following ------------------------------------------------------------------------------------
def test_the_first_check_only_records_what_is_out_then_new_volumes_are_noticed(on, monkeypatch):
    got = {"items": _items(3)}
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False: (dict(SERIES), got["items"]))
    fid, _ = follows.follow("bob", "comic", "mangaupdates", "77", "One Piece", {"language": "en"})
    assert follows.check(db.follow_get(fid)) == 0 and db.notices("bob") == [], "no flood of old volumes"
    got["items"] = _items(5, {5: FUTURE})
    assert follows.check(db.follow_get(fid)) == 1, "vol 4 is out; vol 5 is only announced"
    (n,) = db.notices("bob")
    assert n["title"] == "One Piece Vol. 4" and n["item"]["number"] == "4"
    got["items"] = _items(5)
    assert follows.check(db.follow_get(fid)) == 1 and len(db.notices("bob")) == 2, "vol 5, on its release day"
    assert follows.check(db.follow_get(fid)) == 0, "never twice"

def test_a_provider_that_fails_is_tried_again_later_without_losing_what_was_known(on, monkeypatch):
    monkeypatch.setattr(comicmeta, "series", lambda *a, **k: (dict(SERIES), _items(2)))
    fid, _ = follows.follow("bob", "comic", "mangaupdates", "77", "One Piece")
    follows.check(db.follow_get(fid), now=1000.0)
    def down(*a, **k):
        raise comicmeta.MetaError("down")
    monkeypatch.setattr(comicmeta, "series", down)
    follows.check(db.follow_get(fid), now=2000.0)
    f = db.follow_get(fid)
    assert f["known"] == ["1", "2"] and f["next_check"] == 2000.0 + follows.FAILED_RETRY and "could not check" in f["detail"]

def test_book_series_and_authors_come_from_hardcover(users, monkeypatch):
    with pytest.raises(follows.FollowError, match="Hardcover"):
        follows.follow("bob", "book-series", "hardcover", "5", "Discworld")
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc")
    books = [{"id": "1", "title": "The Colour of Magic", "date": "1983-11-24", "position": 1, "author": "Terry Pratchett"}]
    monkeypatch.setattr(hardcover, "series_books", lambda sid: ("Discworld", False, list(books)))
    fid, _ = follows.follow("bob", "book-series", "hardcover", "5", "Discworld")
    follows.check(db.follow_get(fid))
    books.append({"id": "2", "title": "The Light Fantastic", "date": PAST, "position": 2, "author": "Terry Pratchett"})
    books.append({"id": "3", "title": "Future Book", "date": FUTURE, "position": 3, "author": "Terry Pratchett"})
    follows.check(db.follow_get(fid))
    (n,) = db.notices("bob")
    assert n["title"] == "The Light Fantastic (Discworld #2)" and n["item"] == {
        "type": "book", "title": "The Light Fantastic", "author": "Terry Pratchett", "series": "Discworld", "hardcover_id": "2"}

def test_pick_in_shelfmark_opens_it_already_searching_for_the_book(users, monkeypatch):
    fid, _ = db.follow_add("bob", "author", "hardcover", "9", "Terry Pratchett")
    nid = db.notice_add("bob", fid, "2", "The Light Fantastic", "", {"type": "book", "title": "The Light Fantastic", "author": "Terry Pratchett"})
    what, url = follows.act("bob", nid, "shelfmark")
    assert url == "https://shelf.example.test/?content_type=ebook&q=The+Light+Fantastic&author=Terry+Pratchett"
    assert db.notice_get(nid)["status"] == "requested"
    with pytest.raises(follows.FollowError):
        follows.act("alice", nid, "dismiss")

def test_a_comic_notice_requests_it_with_one_tap(on, monkeypatch):
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False: (dict(SERIES), _items(5)))
    fid, _ = db.follow_add("bob", "comic", "mangaupdates", "77", "One Piece")
    nid = db.notice_add("bob", fid, "5", "One Piece Vol. 5", "", {"type": "comic", "provider": "mangaupdates",
                                                                    "series_id": "77", "number": "5", "language": "en"})
    assert follows.act("bob", nid, "request") == ("queued", None)
    (r,) = db.comic_list("bob")
    assert (r["series_name"], r["number"], r["status"]) == ("One Piece", "5", "queued")

def test_readers_who_asked_for_mail_get_one_digest(users, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    mails = []
    monkeypatch.setattr(notify, "_deliver", lambda to, subject, text: mails.append((to, subject, text)))
    db.set_prefs("bob", notify_email=True)
    for i, owner in enumerate(("bob", "bob", "alice")):
        db.notice_add(owner, 1, f"k{i}", f"Book {i}", "", {"type": "book", "title": f"Book {i}"})
    assert follows.mail_digests() == 1
    assert mails[0][0] == "bob@example.test" and "and 1 more" in mails[0][1] and "Book 0" in mails[0][2] and "Book 1" in mails[0][2]
    assert follows.mail_digests() == 0 and db.notices_unmailed() == [], "alice (no mail) is never mailed later either"

def test_the_admin_hears_a_daily_count(users, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "alert", lambda title, text, prio="default", **k: told.append((title, prio, k.get("seq"))))
    follows._SUMMARY["at"] = 0.0
    db.notice_add("bob", 1, "a", "A", "", {}); db.notice_add("alice", 1, "b", "B", "", {})
    assert follows.admin_summary(now=time.time() + 60) == 2
    assert told == [("2 new releases for 2 readers", "low", "follows-daily")]
    assert follows.admin_summary(now=time.time() + 120) is None, "once a day"


# ---- the pages ---------------------------------------------------------------------------------------
def test_new_for_you_on_the_home_page_and_its_buttons(client, monkeypatch):
    fid, _ = db.follow_add("bob", "author", "hardcover", "9", "Terry Pratchett")
    nid = db.notice_add("bob", fid, "2", "The Light Fantastic", "by Terry Pratchett", {"type": "book", "title": "The Light Fantastic", "author": "Terry Pratchett"})
    login(client, "bob", "bobpass1")
    home = client.get("/").get_data(as_text=True)
    assert "New for you" in home and "The Light Fantastic" in home and "Pick in Shelfmark" in home and "Following (1)" in home
    assert f'href="/following/{fid}"' in home, "the name opens what it belongs to"
    assert "Get it" not in home, "no one-tap without the portal's Shelfmark login"
    r = client.post(f"/notices/{nid}/shelfmark", data={"csrf": _csrf(client)})
    assert r.status_code == 302 and r.headers["Location"].startswith("https://shelf.example.test/?content_type=ebook&q=The+Light")
    assert "New for you" not in client.get("/").get_data(as_text=True)

def _csrf(client):
    import re
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/status").get_data(as_text=True)).group(1)

def test_follow_and_stop_following_from_the_pages(client, monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc")
    login(client, "bob", "bobpass1")
    assert post(client, "/follow", kind="author", provider="evil", key="9", name="x").status_code == 400
    r = post(client, "/follow", kind="author", provider="hardcover", key="9", name="Terry Pratchett")
    assert "Following Terry Pratchett" in r.get_data(as_text=True)
    (f,) = db.follow_list("bob")
    post(client, "/logout"); login(client, "alice", "alicepass1")
    assert post(client, f"/follows/{f['id']}/stop").status_code == 404, "not someone else's"
    post(client, "/logout"); login(client, "bob", "bobpass1")
    post(client, f"/follows/{f['id']}/stop")
    assert db.follow_list("bob") == []

def test_hardcover_search_results_arrive_as_text_or_json():
    doc = {"id": 5, "name": "Discworld", "author_name": "Terry Pratchett", "books_count": 41, "books": ["Mort"]}
    assert hardcover._hits(json.dumps({"hits": [{"document": doc}]})) == [doc]
    assert hardcover._hits({"hits": [{"document": doc}]}) == [doc]


# ---- reading status from Calibre-Web ----------------------------------------------------------------------
def _cwa_reading(user_id, rows, bookmarks=()):
    import sqlite3
    c = sqlite3.connect(config.CWA_DB)
    c.executescript("""CREATE TABLE IF NOT EXISTS book_read_link(id INTEGER PRIMARY KEY, book_id INTEGER, user_id INTEGER,
                        read_status INTEGER NOT NULL DEFAULT 0, last_modified DATETIME, last_time_started_reading DATETIME,
                        times_started_reading INTEGER DEFAULT 0);
                       CREATE TABLE IF NOT EXISTS kobo_reading_state(id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER,
                        last_modified DATETIME, priority_timestamp DATETIME);
                       CREATE TABLE IF NOT EXISTS kobo_bookmark(id INTEGER PRIMARY KEY, kobo_reading_state_id INTEGER, last_modified DATETIME,
                        location_source TEXT, location_type TEXT, location_value TEXT, progress_percent FLOAT,
                        content_source_progress_percent FLOAT);
                       CREATE TABLE IF NOT EXISTS kobo_statistics(id INTEGER PRIMARY KEY, kobo_reading_state_id INTEGER, last_modified DATETIME,
                        remaining_time_minutes INTEGER, spent_reading_minutes INTEGER);""")
    for bid, st in rows:
        c.execute("INSERT INTO book_read_link(book_id, user_id, read_status) VALUES(?,?,?)", (bid, user_id, st))
    for bid, pct in bookmarks:
        sid = c.execute("INSERT INTO kobo_reading_state(user_id, book_id) VALUES(?,?)", (user_id, bid)).lastrowid
        c.execute("INSERT INTO kobo_bookmark(kobo_reading_state_id, progress_percent) VALUES(?,?)", (sid, pct))
    c.commit(); c.close()

def test_reading_status_comes_from_what_calibre_web_records(client):
    bob = cwa.get_user("bob")["id"]
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Sourcery", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(3, "Eric", "Terry Pratchett", tags=["owner:bob"])
    _cwa_reading(bob, [(1, 1), (2, 2)], bookmarks=[(2, 45.2)])
    assert cwa.reading_state("bob") == {1: {"status": "read", "pct": None}, 2: {"status": "reading", "pct": 45}}
    assert cwa.reading_state("alice") == {}, "another reader's reading is never theirs"
    login(client, "bob", "bobpass1")
    page = client.get("/library").get_data(as_text=True)
    assert ">Read<" in page and "Reading 45 %" in page


# ---- AniList ---------------------------------------------------------------------------------------
@pytest.fixture
def al(users, monkeypatch):
    monkeypatch.setattr(config, "ANILIST_CLIENT_ID", "52302")
    monkeypatch.setattr(config, "ANILIST_CLIENT_SECRET", "s3cret")

def test_connecting_anilist_checks_that_the_sign_in_started_here(client, al, monkeypatch):
    login(client, "bob", "bobpass1")
    r = client.get("/anilist/connect")
    loc = r.headers["Location"]
    assert loc.startswith(anilist.AUTHORIZE) and "client_id=52302" in loc and \
        "redirect_uri=https%3A%2F%2Frequest.example.test%2Fanilist%2Fcallback" in loc
    state = loc.split("state=")[1]
    r = client.get("/anilist/callback?code=abc&state=forged", follow_redirects=True)
    assert "did not start here" in r.get_data(as_text=True) and db.anilist_get("bob") is None
    client.get("/anilist/connect")
    with client.session_transaction() as s:
        state = s["anilist_state"]
    monkeypatch.setattr(anilist, "exchange", lambda code: "tok-1")
    monkeypatch.setattr(anilist, "viewer", lambda tok: (4242, "bobreads"))
    r = client.get(f"/anilist/callback?code=abc&state={state}", follow_redirects=True)
    assert "connected as bobreads" in r.get_data(as_text=True)
    assert db.anilist_get("bob")["al_user_id"] == 4242
    page = client.get("/devices").get_data(as_text=True)
    assert "Connected as <strong>bobreads</strong>" in page

def _manga_book(bid, series, index, owner="bob"):
    add_calibre_book(bid, f"{series} Vol. {index}", "Oda", tags=["Manga", f"owner:{owner}"], formats=("cbz",))
    c = calibre_conn(config.CALIBRE_DB)
    sid = (c.execute("SELECT id FROM series WHERE name=?", (series,)).fetchone() or
           [c.execute("INSERT INTO series(name, sort) VALUES(?,?)", (series, series)).lastrowid])[0]
    c.execute("INSERT INTO books_series_link(book, series) VALUES(?,?)", (bid, sid))
    c.execute("UPDATE books SET series_index=? WHERE id=?", (float(index), bid))
    c.commit(); c.close()

def test_finished_volumes_update_anilist_and_never_go_back(al, monkeypatch):
    bob = cwa.get_user("bob")["id"]
    for i in (1, 2, 3):
        _manga_book(i, "One Piece", i)
    _manga_book(9, "Unknown Manga", 1)
    _cwa_reading(bob, [(1, 1), (2, 1), (3, 2), (9, 1)])
    db.anilist_set("bob", "tok", 4242, "bobreads")
    calls = []
    def gql(token, query, variables=None):
        calls.append((query.split("(")[0].split()[-1] if "mutation" in query else query.split()[1], variables))
        if "Page" in query:
            s = (variables or {}).get("s")
            return {"Page": {"media": [{"id": 21, "volumes": None, "status": "RELEASING",
                                        "title": {"english": "One Piece", "romaji": "ONE PIECE"}, "synonyms": []}]
                             if s == "One Piece" else [{"id": 99, "title": {"english": "Something Else"}, "synonyms": []}]}}
        if "MediaList(" in query:
            return {"MediaList": {"status": "CURRENT", "progressVolumes": 1}}
        return {"SaveMediaListEntry": {"id": 1, "progressVolumes": variables["v"]}}
    monkeypatch.setattr(anilist, "_gql", gql)
    assert anilist.sync_once() == 1
    saves = [v for q, v in calls if q == "S"]
    assert saves == [{"m": 21, "v": 2, "s": "CURRENT"}], "the highest FINISHED volume (3 is only being read)"
    assert db.anilist_media_get("unknown manga")["media_id"] is None, "a title that does not match is left out"
    calls.clear()
    assert anilist.sync_once() == 0 and not [q for q, v in calls if q == "S"], "sent once"

def test_anilist_already_counting_more_is_never_lowered(al, monkeypatch):
    bob = cwa.get_user("bob")["id"]
    _manga_book(1, "One Piece", 1)
    _cwa_reading(bob, [(1, 1)])
    db.anilist_set("bob", "tok", 4242, "bobreads")
    db.anilist_media_put("one piece", 21, "One Piece", None, "RELEASING")
    saved = []
    monkeypatch.setattr(anilist, "_gql", lambda t, q, v=None: {"MediaList": {"progressVolumes": 40}} if "MediaList(" in q else saved.append(v))
    anilist.sync_once()
    assert saved == [] and db.anilist_sent("bob", 21) == 40

def test_a_revoked_connection_is_said_on_devices(al, monkeypatch):
    bob = cwa.get_user("bob")["id"]
    _manga_book(1, "One Piece", 1)
    _cwa_reading(bob, [(1, 1)])
    db.anilist_set("bob", "tok", 4242, "bobreads")
    db.anilist_media_put("one piece", 21, "One Piece", None, "RELEASING")
    def lost(*a, **k):
        raise anilist.AuthLost("AniList no longer accepts this connection: connect again on Devices")
    monkeypatch.setattr(anilist, "_gql", lost)
    anilist.sync_once()
    assert "connect again" in db.anilist_get("bob")["detail"]


# ---- Hardcover's data, as measured against the real API (2026-09-29) --------------------------------
def test_the_real_series_comes_first_and_stray_records_are_left_out(monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc_pat_x")
    hits = {"hits": [{"document": {"id": 270374, "name": "Discworld", "author_name": "Unknown", "books_count": 4}},
                     {"document": {"id": 1018, "name": "Discworld", "author_name": "Terry Pratchett", "books_count": 41}}]}
    monkeypatch.setattr(hardcover, "_q", lambda q, v: {"search": {"results": hits}})
    assert [h["id"] for h in hardcover.search("Discworld")] == ["1018", "270374"]
    series = {"series": [{"name": "Discworld", "is_completed": True, "book_series": [
        {"position": 4, "book": {"id": 1, "title": "Mort", "release_date": "1987-01-01", "compilation": False, "canonical_id": None, "contributions": []}},
        {"position": 4, "book": {"id": 2, "title": "", "release_date": "1987-01-01", "compilation": False, "canonical_id": None, "contributions": []}},
        {"position": None, "book": {"id": 3, "title": "Discworld Omnibus", "compilation": True, "canonical_id": None, "contributions": []}},
        {"position": 5, "book": {"id": 4, "title": "Sourcery", "compilation": False, "canonical_id": 9, "contributions": []}}]}]}
    monkeypatch.setattr(hardcover, "_q", lambda q, v: series)
    assert [b["title"] for b in hardcover.series_books("1018")[2]] == ["Mort"]

def test_an_authors_books_are_asked_for_released_and_read_ones_only(monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc_pat_x")
    asked = {}
    def q(query, v):
        asked.update(v, query=query)
        return {"authors": [{"name": "Brandon Sanderson"}], "books": [{"id": 1, "title": "Wind and Truth", "release_date": "2024-12-06"}]}
    monkeypatch.setattr(hardcover, "_q", q)
    name, books = hardcover.author_books("204214")
    assert name == "Brandon Sanderson" and books[0]["title"] == "Wind and Truth"
    assert asked["t"] == datetime.date.today().isoformat() and asked["r"] == hardcover.AUTHOR_MIN_READERS
    assert "release_date: {_lte: $t}" in asked["query"] and "users_count: {_gte: $r}" in asked["query"]


def test_a_first_check_that_fails_is_shown_and_tried_again_soon(client, monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc_pat_x")
    def down(aid, limit=60):
        raise hardcover.HardcoverError("Hardcover did not answer (ConnectTimeout)")
    monkeypatch.setattr(hardcover, "author_books", down)
    fid, _ = follows.follow("bob", "author", "hardcover", "9", "Jeffrey Archer")
    follows.check(db.follow_get(fid), now=1000.0)
    assert db.follow_get(fid)["next_check"] == 1000.0 + follows.FIRST_RETRY, "half an hour, not six"
    login(client, "bob", "bobpass1")
    page = client.get("/following").get_data(as_text=True)
    assert "could not check: Hardcover did not answer (ConnectTimeout)" in page and "first check within minutes" not in page

def test_hardcover_is_asked_once_more_when_the_connection_failed(monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc_pat_x")
    monkeypatch.setattr(hardcover, "RETRY_SLEEP", 0)
    calls = []
    class R:
        status_code = 200
        def json(self): return {"data": {"search": {"results": {"hits": []}}}}
    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise hardcover.requests.ConnectTimeout("slow")
        return R()
    monkeypatch.setattr(hardcover.requests, "post", post)
    assert hardcover.search("Jeffrey Archer", "Author") == [] and len(calls) == 2
