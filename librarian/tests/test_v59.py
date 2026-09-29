"""v5.9: chapters then the volume, reading status by hand, Metron, and the comic safeguards' pages."""
import json
import pytest
import config, db, cwa, comics, comicmeta, follows, mangadex, metrontrack, notify
from conftest import add_calibre_book, login, post
from test_follows import _cwa_reading
from test_comics import _comic_book, _comic_file, Shelf, SERIES, ITEM5


@pytest.fixture
def on(users, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    monkeypatch.setattr(notify, "admin", lambda ev, r: None)


def _items(n):
    return [{"id": f"77:v{i}", "number": str(i), "label": f"Vol. {i}", "date": None, "cover": None} for i in range(1, n + 1)]


# ---- following chapters -------------------------------------------------------------------------
def test_following_chapters_notices_new_ones_and_the_volume_says_what_it_replaces(on, monkeypatch):
    got = {"latest": 150, "vols": 17}
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False:
                        (dict(SERIES, latest_chapter=got["latest"]), _items(got["vols"])))
    monkeypatch.setattr(mangadex, "chapters_in", lambda mu, name, vol: [151.0, 152.0] if str(vol) == "18" else None)
    fid, _ = follows.follow("bob", "comic", "mangaupdates", "77", "One Piece", {"language": "en", "mode": "chapters"})
    assert follows.check(db.follow_get(fid)) == 0, "the first check records 150 chapters, no flood"
    got["latest"] = 152
    assert follows.check(db.follow_get(fid)) == 2
    assert [n["title"] for n in db.notices("bob")] == ["One Piece Ch. 152", "One Piece Ch. 151"] or \
           sorted(n["title"] for n in db.notices("bob")) == ["One Piece Ch. 151", "One Piece Ch. 152"]
    got["vols"] = 18
    follows.check(db.follow_get(fid))
    (vol,) = [n for n in db.notices("bob") if n["title"] == "One Piece Vol. 18"]
    assert "holds chapters 151-152" in vol["detail"]

def test_switching_a_follow_to_chapters_does_not_flood(on, monkeypatch):
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False: (dict(SERIES, latest_chapter=900), _items(3)))
    fid, _ = follows.follow("bob", "comic", "mangaupdates", "77", "One Piece", {"language": "en"})
    follows.check(db.follow_get(fid))
    with db._conn() as c:
        c.execute("UPDATE follows SET extra=? WHERE id=?", (json.dumps({"language": "en", "mode": "chapters"}), fid))
    assert follows.check(db.follow_get(fid)) == 0

def test_a_chapter_notice_requests_the_chapter_not_the_volume(on, monkeypatch):
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False: (dict(SERIES), _items(5)))
    fid, _ = db.follow_add("bob", "comic", "mangaupdates", "77", "One Piece")
    nid = db.notice_add("bob", fid, "c5", "One Piece Ch. 5", "", {"type": "comic", "unit": "chapter", "provider": "mangaupdates",
                                                                  "series_id": "77", "number": "5", "label": "Ch. 5", "language": "en"})
    assert follows.act("bob", nid, "request") == ("queued", None)
    (r,) = db.comic_list("bob")
    assert (r["unit"], r["number"], r["label"]) == ("chapter", "5", "Ch. 5")
    assert comics.request("bob", SERIES, ITEM5)[1] == "queued", "volume 5 is another request than chapter 5"

def test_a_chapter_request_finds_the_chapter_release(on, monkeypatch):
    monkeypatch.setattr(comics, "_alt_names", lambda req: [])
    rid, _ = comics.request("bob", SERIES, {"number": "1148", "label": "Ch. 1148", "unit": "chapter"})
    s = Shelf([{"source_id": "v", "title": "One Piece v110 (Digital)", "protocol": "usenet"},
               {"source_id": "c", "title": "One.Piece.C1148.2023.CBZ.eBook-TONER", "protocol": "usenet"}])
    assert comics.search_once(db.comic_get(rid), s) == "confirm"
    r = db.comic_get(rid)
    assert r["candidate"]["source_id"] == "c" and "exactly this chapter" in r["reasons"]
    assert s.searched == ["One Piece chapter 1148"]

def test_a_chapter_arrives_into_its_own_series(on, tmp_path):
    rid, _ = comics.request("bob", SERIES, {"number": "1148", "label": "Ch. 1148", "unit": "chapter"})
    db.comic_update(rid, status="downloading")
    vid, _ = comics.request("bob", SERIES, ITEM5)
    db.comic_update(vid, status="downloading")
    src = tmp_path / "One Piece Chapter 1148.cbz"
    _comic_file(src, pages=18)
    cbz, base, req = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert req["id"] == rid and base == "One Piece Ch. 1148"
    import zipfile
    with zipfile.ZipFile(cbz) as z:
        cbi = json.loads(z.comment)["ComicBookInfo/1.0"]
    assert cbi["series"] == "One Piece (chapters)" and cbi["issue"] == "1148" and "Chapter" in cbi["tags"]

def test_a_volume_offers_to_replace_the_chapters_it_holds(on, monkeypatch):
    for bid, n in ((20, 151), (21, 152), (22, 153)):
        _comic_book(bid, "One Piece (chapters)", n, ["Manga", "Chapter", "owner:bob"])
    _comic_book(30, "One Piece", 18, ["Manga", "owner:bob"])
    monkeypatch.setattr(mangadex, "chapters_in", lambda mu, name, vol: [151.0, 152.0])
    rid, _ = comics.request("bob", SERIES, {"number": "18", "label": "Vol. 18"})
    sid = comics.offer_swap(db.comic_get(rid), 30)
    (sw,) = db.comic_swaps_offered("bob")
    assert sw["id"] == sid and [c["tick"] for c in sw["chapters"]] == [True, True, False]
    assert db.comic_waiting("bob") >= 1
    assert comics.offer_swap(db.comic_get(rid), 30) is None, "offered once"
    with pytest.raises(comics.ComicError):
        comics.swap("alice", sid, [20])
    assert comics.swap("bob", sid, [20, 21, 999]) == 2, "only the reader's own chapters of this offer"
    assert db.untag_pending(20, "bob") and db.untag_pending(21, "bob") and not db.untag_pending(22, "bob")

def test_no_offer_without_chapters_or_when_none_are_in_the_volume(on, monkeypatch):
    monkeypatch.setattr(mangadex, "chapters_in", lambda mu, name, vol: [1.0, 2.0])
    rid, _ = comics.request("bob", SERIES, {"number": "1", "label": "Vol. 1"})
    assert comics.offer_swap(db.comic_get(rid), 30) is None
    _comic_book(20, "One Piece (chapters)", 151, ["Manga", "Chapter", "owner:bob"])
    assert comics.offer_swap(db.comic_get(rid), 30) is None

def test_mangadex_is_trusted_only_for_the_same_mangaupdates_series(monkeypatch, users):
    calls = []
    def get(path, params=None):
        calls.append(path)
        if path == "/manga":
            return {"data": [{"id": "colored", "attributes": {"links": {"mu": "zzz"}}},
                             {"id": "main", "attributes": {"links": {"mu": mangadex.base36(75336092483)}}}]}
        return {"volumes": {"19": {"chapters": {"165": {}, "175.5": {}}}, "none": {"chapters": {"232": {}}}}}
    monkeypatch.setattr(mangadex, "_get", get)
    assert mangadex.base36(75336092483) == "ylx5wzn"
    assert mangadex.chapters_in("75336092483", "Chainsaw Man", "19") == [165.0, 175.5]
    assert calls == ["/manga", "/manga/main/aggregate"]
    assert mangadex.chapters_in("75336092483", "Chainsaw Man", "20") is None


# ---- reading status by hand ---------------------------------------------------------------------
def test_marking_read_is_stored_where_calibre_web_keeps_it(client):
    bob = cwa.get_user("bob")["id"]
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Eric", "Terry Pratchett", tags=["owner:alice"])
    _cwa_reading(bob, [])
    login(client, "bob", "bobpass1")
    post(client, "/book/1/read/reading")
    assert cwa.reading_state("bob")[1]["status"] == "reading"
    post(client, "/book/1/read/read")
    assert cwa.reading_state("bob")[1]["status"] == "read"
    assert "Read" in client.get("/book/1").get_data(as_text=True)
    assert post(client, "/book/2/read/read").status_code == 404, "only books the reader can see"
    assert post(client, "/book/1/read/finished").status_code == 404

def test_read_up_to_here_marks_the_readers_volumes(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False: (dict(SERIES), _items(4)))
    bob = cwa.get_user("bob")["id"]
    _cwa_reading(bob, [])
    for bid, n in ((11, 1), (12, 2), (13, 3)):
        _comic_book(bid, "One Piece", n, ["Manga", "owner:bob"])
    login(client, "bob", "bobpass1")
    page = client.get("/comics/series/mangaupdates/77").get_data(as_text=True)
    assert "read up to here" in page and "Follow chapters, then volumes" in page
    post(client, "/comics/series/mangaupdates/77/read-up-to", number="2")
    st = cwa.reading_state("bob")
    assert st[11]["status"] == st[12]["status"] == "read" and 13 not in st


# ---- Metron -------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data or {}
    def json(self):
        return self._d

def test_finished_metron_comics_are_scrobbled_once(on, monkeypatch):
    sent = []
    def req(method, url, **kw):
        sent.append((method, url.rsplit("/api", 1)[-1], kw.get("json"), (kw.get("headers") or {}).get("Authorization")))
        return _Resp(200, {"results": []})
    monkeypatch.setattr(metrontrack.requests, "request", req)
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en", fresh=False:
                        ({"provider": "metron", "id": sid, "name": "Saga"}, [{"id": "5501", "number": "12", "label": "#12"}]))
    key = "k" * 40
    assert metrontrack.connect("bob", "bobm", key) == "bobm"
    assert sent[-1][0] == "GET" and sent[-1][3] == f"Bearer {key}"
    bob = cwa.get_user("bob")["id"]
    add_calibre_book(40, "Saga 12", "BKV", tags=["Comics", "owner:bob"])
    rid, _ = db.comic_add("bob", {"provider": "metron", "series_id": "77", "series_name": "Saga", "number": "12"})
    db.comic_update(rid, status="done", calibre_id=40)
    _cwa_reading(bob, [(40, 1)])
    assert metrontrack.sync_once() == 1
    assert sent[-1][:3] == ("POST", "/collection/scrobble/", sent[-1][2]) and sent[-1][2]["issue_id"] == 5501
    assert metrontrack.sync_once() == 0, "once"

def test_a_refused_metron_login_is_said_and_not_kept(on, monkeypatch):
    monkeypatch.setattr(metrontrack.requests, "request", lambda *a, **k: _Resp(401))
    with pytest.raises(metrontrack.AuthLost):
        metrontrack.connect("bob", "bobm", "hunter2")
    assert db.metron_get("bob") is None

def test_metron_on_devices(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(metrontrack.requests, "request", lambda *a, **k: _Resp(200))
    login(client, "bob", "bobpass1")
    assert "Connect Metron" in client.get("/devices").get_data(as_text=True)
    post(client, "/metron/connect", username="bobm", secret="pw")
    assert db.metron_get("bob")["method"] == "password"
    assert "Connected as <strong>bobm</strong>" in client.get("/devices").get_data(as_text=True)
    post(client, "/metron/disconnect")
    assert db.metron_get("bob") is None


# ---- the comic safeguards on the pages ---------------------------------------------------------
def test_the_comics_page_asks_before_downloading_and_yes_to_all(client, monkeypatch):
    import shelfmark_api
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(notify, "admin", lambda ev, r: None)
    ids = []
    for n in (5, 6):
        rid, _ = comics.request("bob", SERIES, {"number": str(n), "label": f"Vol. {n}"})
        db.comic_update(rid, status="confirm", candidate={"source_id": f"s{n}", "title": f"One Piece v0{n} (Digital)", "indexer": "NZBgeek"},
                        reasons=["exactly this volume"])
        ids.append(rid)
    login(client, "bob", "bobpass1")
    page = client.get("/comics").get_data(as_text=True)
    assert "Yes to all 2 for One Piece" in page and "Yes, that one" in page and "NZBgeek" in page and "Comics (2)" in page
    queued = []
    monkeypatch.setattr(shelfmark_api, "user_id", lambda name: 12)
    monkeypatch.setattr(shelfmark_api, "queue_release", lambda rel, uid, content_type="ebook": queued.append(rel["source_id"]))
    post(client, "/comics/series/mangaupdates/77/confirm-all")
    assert sorted(queued) == ["s5", "s6"] and all(db.comic_get(i)["status"] == "downloading" for i in ids)

def test_wrong_comic_on_its_page(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(notify, "admin", lambda ev, r: None)
    _comic_book(9, "One Piece", 5, ["Manga", "owner:bob"])
    rid, _ = db.comic_add("bob", {"provider": "mangaupdates", "series_id": "77", "series_name": "One Piece", "kind": "manga", "number": "5"})
    db.comic_update(rid, status="done", calibre_id=9, release_title="One Piece v05 (Digital)")
    login(client, "bob", "bobpass1")
    assert "Wrong comic" in client.get("/book/9").get_data(as_text=True)
    post(client, "/book/9/wrong")
    assert db.comic_get(rid)["status"] == "queued" and db.untag_pending(9, "bob")
