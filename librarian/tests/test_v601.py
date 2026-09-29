"""v6.0.1: comics and audiobooks get generous size caps of their own; credentials never leave
the portal in error text; ' - Title' names (Shelfmark's 'Author - Title' with no author)."""
import os
import pytest
import config, db, comics, comicrel, audiorel, notify, redact, shelfmark_api, worker, bookreq
from test_comics import SERIES, Shelf, _comic_file

# the shape of a real SABnzbd refusal as Shelfmark reports it, with made-up values
ERR = ("Failed to add to sabnzbd: 403 Client Error: Forbidden for url: https://hcus:FAKE-SEEDBOX-PW@sab.example.link/api"
       "?apikey=https%3A%2F%2Fsab.example.link%2F&mode=addurl&output=json&name=https%3A%2F%2Fprowlarr.example.link"
       "%2F4%2Fdownload%3Fapikey%3DFAKEPROWLARRKEY0123456789%26link%3DFAKELINKBLOB%26file%3DPeanuts&nzbname=Peanuts&cat=bookstack-ebooks")


# ---- credentials ------------------------------------------------------------------------------------
def test_a_shelfmark_error_keeps_its_meaning_but_not_its_credentials():
    out = redact.secrets(ERR)
    for secret in ("FAKE-SEEDBOX-PW", "FAKEPROWLARRKEY0123456789", "FAKELINKBLOB", "hcus:"):
        assert secret not in out
    assert "403 Client Error: Forbidden" in out and "sab.example.link" in out and "cat=bookstack-ebooks" in out

@pytest.mark.parametrize("text,gone", [
    ("see https://a:b@x.y/z", "a:b"),
    ("Authorization: Bearer abc.def", "abc.def"),
    ("token=zzz&x=1", "zzz"),
    ("apikey: QQQ123", "QQQ123"),
    ("https%3A%2F%2Fuser%3Apw%40host%2Fp", "user%3Apw"),
])
def test_every_common_credential_shape_is_hidden(text, gone):
    assert gone not in redact.secrets(text)

def test_plain_text_is_left_alone():
    t = "The Complete Peanuts v01 - 1950 to 1952 (2004) passes; file is 339 MB"
    assert redact.secrets(t) == t and redact.secrets(None) is None

def test_shelfmark_errors_and_failed_downloads_arrive_cleaned():
    assert "FAKE-SEEDBOX-PW" not in str(shelfmark_api.ShelfmarkError(ERR))
    got = shelfmark_api.failed({"error": {"t1": {"id": "t1", "title": "Peanuts", "username": "bob", "status_message": ERR}}})
    assert "FAKE-SEEDBOX-PW" not in got[0]["message"] and "FAKEPROWLARRKEY" not in got[0]["message"]

def test_the_admins_alert_and_a_readers_push_never_carry_a_credential(users, monkeypatch):
    sent = []
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://ntfy.example.test/admin")
    monkeypatch.setattr(notify, "_post", lambda url, title, text, *a, **k: sent.append(title + "\n" + text + str(a)))
    notify.admin("error", {"owner": "bob", "title": "Peanuts", "source": "shelfmark", "detail": ERR})
    notify.alert("Shelfmark", ERR)
    assert sent and all("FAKE-SEEDBOX-PW" not in s and "FAKEPROWLARRKEY" not in s for s in sent)
    pushed = []
    class T:
        def __init__(self, target, daemon): self.t = target
        def start(self): self.t()
    monkeypatch.setattr(notify.threading, "Thread", T)
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=8: pushed.append(req.data.decode()))
    db.set_prefs_v6("bob", ntfy_topic="lib-abc")
    notify.reader("bob", "Peanuts", "it failed: " + ERR)
    assert pushed and "FAKE-SEEDBOX-PW" not in pushed[0]


# ---- size caps ------------------------------------------------------------------------------------
def test_comics_and_audiobooks_have_generous_caps_of_their_own():
    assert config.MAX_COMIC_MB >= 2048 and config.MAX_AUDIO_MB >= 4096 and config.MAX_EBOOK_MB == 200
    assert worker._limit_for("comic") == config.MAX_COMIC_MB << 20
    assert worker._limit_for("audio") == config.MAX_AUDIO_MB << 20
    assert worker._limit_for("ebook") == config.MAX_EBOOK_MB << 20

def test_a_comic_bigger_than_an_ebook_is_imported(users, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAX_EBOOK_MB", 1)
    monkeypatch.setattr(config, "MAX_COMIC_MB", 50)
    f = tmp_path / "Big Omnibus v01 (2004) (digital).cbz"
    _comic_file(f, pages=45)
    import zipfile
    with zipfile.ZipFile(f, "a", zipfile.ZIP_STORED) as z:     # colour scans barely compress
        z.writestr("extras.txt", os.urandom(3 << 19))
    assert f.stat().st_size > 1 << 20
    try:
        worker.ingest_local_file(str(f), "bob")
    except ValueError as e:
        assert "MB" not in str(e), f"refused for size: {e}"
    monkeypatch.setattr(config, "MAX_COMIC_MB", 1)
    with pytest.raises(ValueError, match="MAX_COMIC_MB"):
        worker.ingest_local_file(str(f), "bob")

def test_the_pickers_follow_the_same_caps(monkeypatch):
    want = {"title": "Project Hail Mary", "author": "Andy Weir", "language": "en"}
    rel = {"title": "Andy Weir - Project Hail Mary (Unabridged) [M4B]", "protocol": "usenet", "size_bytes": 3 * 1024 ** 3}
    assert audiorel.judge(want, rel)[0], "3 GB audiobook fits the 4 GB default"
    monkeypatch.setattr(config, "MAX_AUDIO_MB", 2048)
    assert audiorel.judge(want, rel)[2] == "too large for one audiobook"
    req = {"series_name": "One Piece", "kind": "manga", "number": "5", "language": "en"}
    big = {"title": "One Piece v05 (2023) (Digital)", "protocol": "usenet", "size_bytes": 1500 * 1024 ** 2}
    monkeypatch.setattr(config, "MAX_COMIC_MB", 2048)
    assert comicrel.judge(req, big)[2] != "too large for one issue or volume"
    monkeypatch.setattr(config, "MAX_COMIC_MB", 1000)
    assert comicrel.judge(req, big)[2] == "too large for one issue or volume"

def test_an_archive_that_would_not_fit_is_refused_before_unpacking(monkeypatch, tmp_path):
    f = tmp_path / "x.cbr"; f.write_bytes(b"x" * 1000)
    with pytest.raises(comics.ComicError, match="more than a comic"):
        comics._check_room(str(f), comics._expand_limit() + 1)
    monkeypatch.setattr(comics.shutil, "disk_usage", lambda p: type("U", (), {"free": 1 << 30})())
    with pytest.raises(comics.ComicError, match="not enough free disk"):
        comics._check_room(str(f), 500 << 20)
    monkeypatch.setattr(comics.shutil, "disk_usage", lambda p: type("U", (), {"free": 50 << 30})())
    comics._check_room(str(f), 500 << 20)

def test_a_large_comic_waits_for_disk_space_without_asking_again(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(comics, "_alt_names", lambda req: [])
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append(ev))
    rid, _ = comics.request("bob", SERIES, {"number": "5", "label": "Vol. 5"})
    rel = {"source_id": "x1", "title": "One Piece v05 (Digital)", "protocol": "usenet", "size_bytes": 900 << 20}
    db.comic_update(rid, status="confirm", candidate=rel)
    room = [False]
    monkeypatch.setattr(bookreq, "_room_for", lambda r, **kw: room[0])
    s = Shelf([rel])
    assert comics.confirm(rid, s) == "queued"
    assert comics.search_once(db.comic_get(rid), s) == "queued"
    assert told.count("error") == 1 and s.searched == [] and s.queued == []
    room[0] = True
    assert comics.search_once(db.comic_get(rid), s) == "downloading" and s.queued == [("x1", 12)]


# ---- names ----------------------------------------------------------------------------------------
def test_v01_then_a_year_range_is_volume_1_not_a_pack():
    p = comicrel.parse(" - The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire)")
    assert p["volumes"] == (1.0, 1.0)
    assert comicrel.parse("One Piece v01-10 (2003)")["volumes"] == (1.0, 10.0)
    assert comicrel.parse("One Piece c001-1100")["chapters"] == (1.0, 1100.0)

def test_a_name_starting_with_a_dash_is_cleaned():
    assert worker._safe(" - The Complete Peanuts v01") == "The Complete Peanuts v01"
    assert worker._safe("Andy Weir - ") == "Andy Weir"
    assert worker._safe("Andy Weir - Project Hail Mary") == "Andy Weir - Project Hail Mary"

def test_the_peanuts_arrival_is_matched_to_its_request(users, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    series = dict(SERIES, provider="metron", id="9", name="The Complete Peanuts", kind="comic", year=2004,
                  publisher="Fantagraphics", authors=["Charles M. Schulz"])
    rid, _ = comics.request("bob", series, {"number": "1", "label": "Vol. 1"})
    db.comic_update(rid, status="downloading", release_title="The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire)")
    src = tmp_path / " - The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire).cbz"
    _comic_file(src, pages=45)
    got = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert got[0] != "skip", got[1]
    cbz, base, req = got
    assert req["id"] == rid and not base.startswith(" -")


# ---- queues: several readers at once ----------------------------------------------------------------
from test_comics import _comic_book

def test_kobo_copies_are_made_with_readers_taking_turns(users, monkeypatch):
    monkeypatch.setattr(comics, "uses_kobo", lambda o: True)
    for bid in range(1, 6):                      # alice drops five volumes first
        _comic_book(bid, "One Piece", bid, ["Manga", "owner:alice"])
    for bid, idx in ((6, 1), (7, 2)):            # then bob two
        _comic_book(bid, "Saga", idx, ["Manga", "owner:bob"])
    order = [r["calibre_id"] for r in comics.kobo_queue()]
    assert order == [1, 6, 2, 7, 3, 4, 5], "one each, in turn: bob does not wait behind all of alice's"
    assert comics.kobo_position(7) == 3 and comics.kobo_position(99) is None


def test_a_make_kobo_copy_goes_first_and_a_too_large_one_is_not_retried(users, monkeypatch):
    monkeypatch.setattr(comics, "uses_kobo", lambda o: True)
    for bid in (1, 2):
        _comic_book(bid, "One Piece", bid, ["Manga", "owner:alice"])
    assert db.comic_convert_result(1, False, "its Kobo copy would be 1500 MB", final=True) == "failed"
    assert [r["calibre_id"] for r in comics.kobo_queue()] == [2], "final: not tried again"

def test_a_big_download_waits_for_the_ones_under_way(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    rid, _ = comics.request("alice", SERIES, {"number": "5", "label": "Vol. 5"})
    db.comic_update(rid, status="downloading", size_bytes=3 << 30)
    monkeypatch.setattr(bookreq.shutil, "disk_usage", lambda p: type("U", (), {"free": 8 << 30})())
    monkeypatch.setattr(worker, "_disk_paused", lambda: False)
    assert db.downloads_in_flight() == 3 << 30
    assert not bookreq._room_for({"size_bytes": 2 << 30}), "8 GB free, 3 GB of it already on its way: 2 GB waits"
    assert bookreq._room_for({"size_bytes": 2 << 30}, comic_id=rid), "a download never waits for itself"
    db.comic_update(rid, status="done")
    assert bookreq._room_for({"size_bytes": 2 << 30})

def test_the_dropbox_takes_readers_in_turn(users, monkeypatch):
    for owner, names in (("alice", ["a1.epub", "a2.epub", "a3.epub"]), ("bob", ["b1.epub"])):
        d = os.path.join(config.DROPBOX_DIR, owner); os.makedirs(d, exist_ok=True)
        for n in names:
            open(os.path.join(d, n), "wb").write(b"x")
    seen = []
    monkeypatch.setattr(worker, "_handle", lambda p, owner, name, *a, **k: seen.append(name) or 0)
    monkeypatch.setattr(worker, "_disk_paused", lambda: False)
    import time as _t
    worker.scan_dropbox_once(now=_t.time() + 3600)
    assert seen == ["a1.epub", "b1.epub", "a2.epub", "a3.epub"]


# ---- the Peanuts, end to end --------------------------------------------------------------------
@pytest.mark.parametrize("stem,title", [
    ("The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire)", "The Complete Peanuts Vol. 1 (2004)"),
    ("Saga 012 (2013) (Digital) (Zone-Empire)", "Saga #12 (2013)"),
    ("Batman - The Long Halloween #1 (1996)", "Batman: The Long Halloween #1 (1996)"),
    ("One Piece c1072 (2023)", "One Piece Ch. 1072 (2023)"),
])
def test_a_comic_nobody_asked_for_gets_a_readable_title_without_a_dash(stem, title):
    assert comics.display_title(stem) == title and " - " not in comics.display_title(stem)

def test_an_unmatched_comic_is_named_by_its_title_not_its_release(users, tmp_path):
    src = tmp_path / " - The Complete Peanuts v01 - 1950 to 1952 (2004) (digital) (Son of Ultron-Empire).cbz"
    _comic_file(src, pages=45)
    cbz, base, req = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert req is None and base == "The Complete Peanuts Vol. 1 (2004)"
    import zipfile
    with zipfile.ZipFile(cbz) as z:
        assert "<Title>The Complete Peanuts Vol. 1 (2004)</Title>" in z.read("ComicInfo.xml").decode()

def test_a_request_whose_comic_is_already_in_the_readers_library_is_closed(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    rid, _ = comics.request("bob", SERIES, {"number": "5", "label": "Vol. 5"})
    db.comic_update(rid, status="downloading", queued_at=1.0)
    _comic_book(40, "One Piece", 5, ["Manga", "owner:bob"])     # e.g. its metadata fixed by hand in Calibre-Web
    comics.watch_downloads(Shelf([]), {}, now=2.0)
    r = db.comic_get(rid)
    assert r["status"] == "done" and r["calibre_id"] == 40, "never downloaded again"


def test_a_url_in_shelfmarks_sabnzbd_api_key_is_named_in_the_alert_and_by_self_test():
    q = {"error": {"t1": {"id": "t1", "title": "Peanuts", "username": "bob", "status_message": ERR},
                   "t2": {"id": "t2", "title": "Other", "username": "bob", "status_message": "timed out"}}}
    f = {x["task_id"]: x["message"] for x in shelfmark_api.failed(q)}
    assert "SABnzbd API key holds a web address" in f["t1"] and "FAKE-SEEDBOX-PW" not in f["t1"]
    assert "API key" not in f["t2"]
    assert shelfmark_api.config_problems(q) == [shelfmark_api._SETTING_HINTS[0][1]]
    assert shelfmark_api.config_problems({"error": {"t2": q["error"]["t2"]}}) == []
