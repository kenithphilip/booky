"""Comics and manga (docs/COMICS.md): which release is the one asked for, requests, the search
through Shelfmark, arrivals, device copies, and the audiobook download."""
import io, json, os, shutil, struct, subprocess, time, zipfile, zlib
import pytest
import config, db, comicrel, comicmeta, comics, worker, notify, admin_cli, kindle
import abs as absapi
from conftest import add_calibre_book, calibre_conn, login, post


# ---- which release (comicrel) ------------------------------------------------------------------
MANGA = {"series_name": "One Piece", "kind": "manga", "number": "5", "language": "en"}
COMIC = {"series_name": "Saga", "kind": "comic", "number": "12", "language": "en", "year": 2012}

@pytest.mark.parametrize("req,title,ok,why", [
    (MANGA, "One Piece v05 (2023) (Digital) (1r0n)", True, "exact"),
    (MANGA, "One Piece Vol. 5 [Viz]", True, "exact"),
    (MANGA, "One Piece - Digital Colored Comics v05 (2019)", True, "exact"),      # the colour edition
    (MANGA, "One Piece v01-v10 (Digital)", True, "a pack that contains it"),
    (MANGA, "One Piece v06 (Digital)", False, "another volume"),
    (MANGA, "One Piece c050 [Scans]", False, "chapters, not a volume"),
    (MANGA, "One Piece v05 [Raw]", False, "in another language (ja)"),
    (MANGA, "One Piece Vol. 5 (French) VF", False, "in another language (fr)"),
    (MANGA, "One Piece Party v05", False, "another series"),                     # a spin-off
    (COMIC, "Saga 012 (2013) (Digital) (Zone-Empire)", True, "exact"),
    (COMIC, "Saga #12", True, "exact"),
    (COMIC, "Saga Vol. 2 (2013) (Digital)", False, "a collected edition, not the issue"),
    (COMIC, "Saga 001-054 (2012-2018)", True, "a pack that contains it"),
    (COMIC, "Saga 012 (1995)", False, "from 1995, before this series began"),
    (COMIC, "Saga of the Swamp Thing 012 (1983)", False, "another series"),
    (dict(COMIC, kind="collected", number="2"), "Saga Vol. 2 (2013) (Digital)", True, "exact"),
    (dict(MANGA, language="fr"), "One Piece Vol. 5 (French) VF", True, "exact"),
])
def test_a_release_is_judged_on_series_number_kind_and_language(req, title, ok, why):
    got_ok, _score, got_why = comicrel.judge(req, {"title": title, "protocol": "usenet", "size_bytes": 60_000_000})
    assert (got_ok, got_why) == (ok, why), title

def test_the_single_digital_volume_beats_a_pack_and_usenet_needs_no_seeding():
    rels = [{"source_id": "a", "title": "One Piece v01-v10 (Digital)", "protocol": "torrent", "seeders": 50},
            {"source_id": "b", "title": "One Piece v05 (2023) (Digital) (1r0n)", "protocol": "usenet"},
            {"source_id": "c", "title": "One Piece v05 (2023) (Digital) (1r0n)", "protocol": "torrent", "seeders": 3},
            {"source_id": "d", "title": "One Piece v05 (Digital)", "protocol": "torrent", "seeders": 0}]
    best, notes = comicrel.pick(MANGA, rels)
    assert best["source_id"] == "b"
    assert ("One Piece v05 (Digital)", False, 0, "no seeders") in notes, "a dead torrent is never picked"
    assert comicrel.pick(MANGA, rels, exclude={"b"})[0]["source_id"] == "c", "a release that failed is not tried again"

def test_a_reader_who_reads_another_language_never_gets_an_unmarked_one_first():
    fr = dict(MANGA, language="fr")
    best, _ = comicrel.pick(fr, [{"source_id": "1", "title": "One Piece v05 (Digital)", "protocol": "usenet"},
                                 {"source_id": "2", "title": "One Piece T05 (VF) (Glénat)", "protocol": "usenet"}])
    assert best["source_id"] == "2"

def test_queries_ask_for_the_number_the_way_releases_spell_it():
    assert comicrel.queries(MANGA) == ["One Piece v05", "One Piece"]
    assert comicrel.queries(COMIC) == ["Saga 012", "Saga #12"]


# ---- what a series is (comicmeta) ---------------------------------------------------------------
class _R:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
    def json(self):
        return self.data

MU_SERIES = {"series_id": 55099564912, "title": "One Piece", "type": "Manga", "status": "115 Volumes (Ongoing)  \n",
             "year": "1997", "completed": False, "description": "Luffy sets out.",
             "image": {"url": {"original": "https://cdn.mangaupdates.com/image/i531056.jpg"}},
             "associated": [{"title": "Wan Pisu"}], "authors": [{"name": "ODA Eiichiro", "type": "Author"}],
             "publishers": [{"publisher_name": "VIZ Media", "type": "English", "notes": "110 Volumes; Ongoing"},
                            {"publisher_name": "Shueisha", "type": "Original", "notes": "115 Volumes"}]}

def test_mangaupdates_volumes_are_the_english_ones_for_an_english_reader(monkeypatch):
    calls = []
    monkeypatch.setattr(comicmeta.requests, "request", lambda *a, **k: (calls.append(a), _R(MU_SERIES))[1])
    info, items = comicmeta.series("mangaupdates", "55099564912", "en")
    assert (info["kind"], info["count"], info["publisher"]) == ("manga", 110, "VIZ Media")
    assert items[0]["label"] == "Vol. 1" and items[-1]["number"] == "110"
    info_fr, items_fr = comicmeta.series("mangaupdates", "55099564912", "fr")
    assert info_fr["count"] == 115, "someone reading another language gets the original count"
    comicmeta.series("mangaupdates", "55099564912", "en")
    assert len(calls) == 1, "a day's cache: the provider is asked once"

def test_a_provider_that_is_down_shows_its_last_answer(monkeypatch):
    monkeypatch.setattr(comicmeta.requests, "request", lambda *a, **k: _R(MU_SERIES))
    comicmeta.series("mangaupdates", "1", "en")
    c = db._conn(); c.execute("UPDATE http_cache SET at = at - 3 * 86400"); c.commit(); c.close()
    def down(*a, **k):
        raise comicmeta.requests.ConnectionError("down")
    monkeypatch.setattr(comicmeta.requests, "request", down)
    assert comicmeta.series("mangaupdates", "1", "en")[0]["name"] == "One Piece"

@pytest.mark.parametrize("t,kind", [("Manga", "manga"), ("Manhwa", "manhwa"), ("Manhua", "manhua"),
                                    ("Novel", "novel"), ("OEL", "comic"), ("Doujinshi", "manga")])
def test_the_kind_decides_reading_direction(t, kind):
    assert comicmeta.kind_from_mangaupdates(t) == kind

def test_metron_trade_paperbacks_are_numbered_by_volume():
    assert comicmeta.kind_from_metron("Trade Paperback", "Saga") == "collected"
    assert comicmeta.kind_from_metron("Ongoing Series", "Saga") == "comic"

def test_western_comics_need_a_metron_account_or_a_comicvine_key(monkeypatch):
    with pytest.raises(comicmeta.MetaError, match="Metron"):
        comicmeta.search("Saga", "comic")


# ---- requests and the family library ------------------------------------------------------------
SERIES = {"provider": "mangaupdates", "id": "77", "name": "One Piece", "kind": "manga", "year": 1997,
          "publisher": "VIZ Media", "authors": ["ODA Eiichiro"], "desc": "Luffy sets out.", "cover": None}
ITEM5 = {"number": "5", "label": "Vol. 5"}

def _comic_book(book_id, series, index, tags, formats=("cbz",)):
    add_calibre_book(book_id, f"{series} Vol. {index}", "ODA Eiichiro", tags=tags, formats=formats)
    c = calibre_conn(config.CALIBRE_DB)
    sid = (c.execute("SELECT id FROM series WHERE name=?", (series,)).fetchone() or
           [c.execute("INSERT INTO series(name, sort) VALUES(?,?)", (series, series)).lastrowid])[0]
    c.execute("INSERT INTO books_series_link(book, series) VALUES(?,?)", (book_id, sid))
    c.execute("UPDATE books SET series_index=? WHERE id=?", (float(index), book_id))
    c.commit(); c.close()

@pytest.fixture
def family(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "COMICS_ENABLED", True)

def test_a_volume_the_family_has_is_shared_not_downloaded(family):
    _comic_book(9, "One Piece", 5, ["Manga", "owner:alice"])
    rid, what = comics.request("bob", SERIES, ITEM5)
    assert what == "shared" and db.comic_get(rid)["status"] == "shared"
    (job,) = db.pending_tag_pushes()
    assert (job["calibre_id"], job["owner"], job["share"]) == (9, "bob", 1)
    assert comics.request("alice", SERIES, ITEM5)[1] == "owned"
    assert comics.request("bob", SERIES, {"number": "6", "label": "Vol. 6"})[1] == "queued"
    assert comics.request("bob", SERIES, {"number": "6", "label": "Vol. 6"})[1] == "exists", "never twice"

def test_a_novel_is_not_a_comic(family):
    with pytest.raises(comics.ComicError, match="novel"):
        comics.request("bob", dict(SERIES, kind="novel"), ITEM5)

def test_approvals_hold_a_readers_comic_for_the_admin(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    rid, what = comics.request("bob", SERIES, ITEM5)
    assert what == "pending" and db.comic_get(rid)["status"] == "pending"
    assert comics.request("admin", SERIES, {"number": "7", "label": "Vol. 7"})[1] == "queued"


# ---- the search, through Shelfmark ----------------------------------------------------------------
class Shelf:
    """shelfmark_api as the worker sees it."""
    ShelfmarkError = type("ShelfmarkError", (Exception,), {})
    def __init__(self, releases, uid=12):
        self.releases, self.uid, self.searched, self.queued = releases, uid, [], []
    def search_releases(self, q, content_type="ebook"):
        self.searched.append(q)
        return list(self.releases)
    def user_id(self, name):
        return self.uid
    def queue_release(self, rel, uid, content_type="ebook"):
        self.queued.append((rel["source_id"], uid))
        return {"status": "queued"}
    def failed(self, queue):
        return (queue or {}).get("failed", [])

def test_the_best_release_is_queued_in_shelfmark_as_the_reader(family, monkeypatch):
    monkeypatch.setattr(comics, "_alt_names", lambda req: [])
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, r: told.append((ev, r.get("owner"), r.get("status"))))
    rid, _ = comics.request("bob", SERIES, ITEM5)
    s = Shelf([{"source_id": "x1", "title": "One Piece v05 (Digital)", "protocol": "usenet"}])
    assert comics.search_once(db.comic_get(rid), s) == "downloading"
    assert s.queued == [("x1", 12)] and s.searched == ["One Piece v05"], "an exact volume: one search is enough"
    r = db.comic_get(rid)
    assert r["status"] == "downloading" and r["tried"] == ["x1"] and "One Piece v05" in r["detail"]
    assert ("requested", "bob", "queued") in told

def test_nothing_right_waits_and_looks_again_then_gives_up(family, monkeypatch):
    monkeypatch.setattr(comics, "_alt_names", lambda req: [])
    rid, _ = comics.request("bob", SERIES, ITEM5, now=1000.0)
    s = Shelf([{"source_id": "c1", "title": "One Piece c045", "protocol": "usenet"}])
    assert comics.search_once(db.comic_get(rid), s, now=1000.0) == "queued"
    r = db.comic_get(rid)
    assert r["next_try"] == 1000.0 + 3600 and "chapters, not a volume" in r["detail"] and s.queued == []
    assert comics.search_once(db.comic_get(rid), s, now=1000.0 + 31 * 86400) == "not-found"

def test_a_download_that_failed_or_never_came_tries_another_release(family):
    rid, _ = comics.request("bob", SERIES, ITEM5)
    db.comic_update(rid, status="downloading", release_title="One Piece v05 (Digital)", queued_at=time.time())
    assert comics.watch_downloads(Shelf([]), {"failed": [{"title": "One Piece v05 (Digital)"}]}) == 1
    assert db.comic_get(rid)["status"] == "queued" and "failed in Shelfmark" in db.comic_get(rid)["detail"]
    db.comic_update(rid, status="downloading", release_title="other", queued_at=time.time() - 2 * 86400)
    assert comics.watch_downloads(Shelf([]), {}) == 1 and "nothing arrived" in db.comic_get(rid)["detail"]


# ---- arrival ----------------------------------------------------------------------------------------
def _png(w, h):
    raw = b"".join(b"\x00" + b"\x80" * (w * 3) for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
           chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

def _comic_file(path, w=60, h=90, pages=4):
    with zipfile.ZipFile(path, "w") as z:
        for i in range(pages):
            z.writestr(f"{i + 1:03d}.png", _png(w, h))

def test_an_arrival_is_matched_and_carries_its_series_into_calibre(family, tmp_path):
    rid, _ = comics.request("bob", SERIES, ITEM5)
    db.comic_update(rid, status="downloading")
    src = tmp_path / "One Piece v05 (2023) (Digital) (1r0n).cbz"
    _comic_file(src)
    cbz, base, req = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert req["id"] == rid and base == "One Piece Vol. 5"
    with zipfile.ZipFile(cbz) as z:
        ci = z.read("ComicInfo.xml").decode()
        cbi = json.loads(z.comment)["ComicBookInfo/1.0"]
    assert "<Series>One Piece</Series>" in ci and "<Manga>YesAndRightToLeft</Manga>" in ci and "<Volume>5</Volume>" in ci
    assert (cbi["series"], cbi["issue"], cbi["tags"]) == ("One Piece", "5", ["Manga"])

def test_the_rest_of_a_pack_is_left_out(family, tmp_path):
    rid, _ = comics.request("bob", SERIES, ITEM5)
    db.comic_update(rid, status="downloading")
    src = tmp_path / "One Piece v06 (Digital).cbz"
    _comic_file(src)
    got = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert got[0] == "skip" and "part of a pack" in got[1]

def test_a_comic_nobody_requested_still_imports_as_a_comic(family, tmp_path):
    src = tmp_path / "Some Indie Comic 001.cbz"
    _comic_file(src)
    cbz, base, req = comics.prepare_arrival(str(src), "bob", str(tmp_path))
    assert req is None and base == "Some Indie Comic 001"
    with zipfile.ZipFile(cbz) as z:
        assert json.loads(z.comment)["ComicBookInfo/1.0"]["tags"] == ["Comics"]

def test_a_long_strip_is_recognised_for_webtoon_mode(tmp_path):
    tall, page = tmp_path / "tall.cbz", tmp_path / "page.cbz"
    _comic_file(tall, 60, 400)
    _comic_file(page, 60, 90)
    assert comics.looks_like_strip(str(tall)) and not comics.looks_like_strip(str(page))

def test_image_sizes_are_read_from_their_headers():
    assert comics.image_size(_png(123, 456)) == (123, 456)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"\x00" * 14 + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 900, 600)
    assert comics.image_size(jpeg) == (600, 900)

@pytest.mark.skipif(not shutil.which(config.BSDTAR), reason="bsdtar is in the shipping image, not always on a dev machine")
def test_a_cb7_is_repacked_as_a_cbz_with_its_pages_in_order(family, tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    for n in (10, 2, 1):
        (pages / f"p{n}.png").write_bytes(_png(40, 60))
    cb7 = tmp_path / "One Piece v05.cb7"
    subprocess.run([config.BSDTAR, "--format", "7zip", "-cf", str(cb7), "-C", str(pages), "."], check=True)
    out = tmp_path / "out.cbz"
    comics.repack_to_cbz(str(cb7), str(out), str(tmp_path))
    with zipfile.ZipFile(out) as z:
        assert [os.path.basename(n) for n in z.namelist()] == ["p1.png", "p2.png", "p10.png"]

RAR = os.path.join(os.path.dirname(__file__), "fixtures", "rar")

@pytest.mark.skipif(not shutil.which(config.UNAR), reason="unar is in the shipping image, not always on a dev machine")
@pytest.mark.parametrize("name", ["rar5-solid.rar", "rar3-solid.rar"])
def test_a_real_rar_archive_is_read(tmp_path, name):
    """rarfile's own test archives (text files, no pages): bsdtar opens RAR 3 and 5, and an
    archive without images is refused for that reason, not as unreadable."""
    with pytest.raises(comics.ComicError, match="no pages"):
        comics.repack_to_cbz(os.path.join(RAR, name), str(tmp_path / "out.cbz"), str(tmp_path))

@pytest.mark.skipif(not shutil.which(config.UNAR), reason="unar is in the shipping image, not always on a dev machine")
def test_a_symlink_inside_an_archive_is_never_followed(tmp_path):
    with pytest.raises(comics.ComicError, match="no pages"):
        comics.repack_to_cbz(os.path.join(RAR, "rar5-symlink-unix.rar"), str(tmp_path / "out.cbz"), str(tmp_path))

def test_the_dropbox_imports_a_requested_volume_tagged_to_the_reader(family, monkeypatch, tmp_path):
    rid, _ = comics.request("bob", SERIES, ITEM5)
    db.comic_update(rid, status="downloading")
    monkeypatch.setattr(notify, "admin", lambda ev, r: None)
    src = tmp_path / "One Piece v05 (Digital).cbz"
    _comic_file(src)
    note = worker.ingest_local_file(str(src), "bob")
    (placed,) = [f for f in os.listdir(config.INGEST_DIR) if f.endswith(".cbz")]
    assert placed.startswith("One Piece Vol. 5")
    with zipfile.ZipFile(os.path.join(config.INGEST_DIR, placed)) as z:
        tags = json.loads(z.comment)["ComicBookInfo/1.0"]["tags"]
    assert tags == ["Manga", "owner:bob"] and note.startswith("tagged owner:bob")
    assert db.comic_get(rid)["status"] == "done"


# ---- device copies --------------------------------------------------------------------------------
def test_a_kobo_copy_is_due_only_for_a_reader_whose_kobo_syncs(family, monkeypatch):
    _comic_book(9, "One Piece", 5, ["Manga", "owner:alice"])
    _comic_book(10, "One Piece", 6, ["Manga", "owner:bob"])
    _comic_book(11, "One Piece", 7, ["Manga", "owner:alice"], formats=("cbz", "kepub"))
    monkeypatch.setattr(comics, "uses_kobo", lambda o: o == "alice")
    due = comics.kobo_due()
    assert [r["calibre_id"] for r in due] == [9] and due[0]["kind"] == "manga" and due[0]["rel"].endswith(".cbz")
    db.comic_convert_force(10)
    assert [r["calibre_id"] for r in comics.kobo_due()] == [10, 9], "'Make Kobo copy' for bob, and it goes first"
    for _ in range(3):
        db.comic_convert_result(9, False, "KCC failed", now=time.time() - 86400)
    assert db.comic_convert_state([9])[9]["status"] == "failed"
    assert [r["calibre_id"] for r in comics.kobo_due()] == [10], "three failures: not tried again"

def test_make_kobo_copy_is_spent_once_the_copy_is_made(family, monkeypatch):
    _comic_book(10, "One Piece", 6, ["Manga", "owner:bob"])
    monkeypatch.setattr(comics, "uses_kobo", lambda o: False)
    db.comic_convert_force(10)
    assert [r["calibre_id"] for r in comics.kobo_due()] == [10]
    db.comic_convert_result(10, True)
    st = db.comic_convert_state([10])[10]
    assert (st["status"], st["forced"]) == ("done", 0)
    assert comics.kobo_due() == [], "made once: never picked again"

def test_the_host_job_talks_to_the_portal_through_admin_cli(family, monkeypatch, capsys):
    _comic_book(9, "One Piece", 5, ["Manga", "owner:alice"])
    monkeypatch.setattr(comics, "uses_kobo", lambda o: True)
    admin_cli.main(["comics", "kobo-due"])
    assert json.loads(capsys.readouterr().out)["rows"][0]["calibre_id"] == 9
    admin_cli.main(["comics", "kobo-result", "9", "ok"])
    capsys.readouterr()
    assert comics.kobo_due() == []

def test_a_comic_to_a_kindle_is_converted_first_then_mailed_in_parts(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp"); monkeypatch.setattr(config, "SMTP_FROM", "lib@example.test")
    import cwa
    cwa.set_kindle_mail("bob", "bob@kindle.com")
    _comic_book(9, "One Piece", 5, ["Manga", "owner:bob"])
    login(client, "bob", "bobpass1")
    r = post(client, "/kindle/9")
    assert "Making a Kindle copy" in r.get_data(as_text=True)
    (row,) = comics.kindle_due()
    assert row["rel"].endswith(".cbz") and row["max_mb"] == config.KINDLE_MAX_MB
    d = os.path.join(config.STAGING_DIR, comics.KINDLE_STAGE, str(row["job"]))
    os.makedirs(d)
    for n in ("01.epub", "02.epub"):
        open(os.path.join(d, n), "wb").write(b"PK fake")
    comics.kindle_result(row["job"], True, ["01.epub", "02.epub"])
    sent = []
    monkeypatch.setattr(kindle, "send", lambda to, path, title, name, **k: sent.append((to, os.path.basename(path), title, k.get("fix"))))
    worker.kindle_once()
    assert sent == [("bob@kindle.com", "01.epub", "One Piece Vol. 5 (part 1 of 2)", False),
                    ("bob@kindle.com", "02.epub", "One Piece Vol. 5 (part 2 of 2)", False)]
    assert not os.path.exists(d), "the Kindle copy is never kept"


# ---- the pages ------------------------------------------------------------------------------------
def test_the_comics_page_exists_only_when_comics_are_on(client, monkeypatch):
    login(client, "bob", "bobpass1")
    assert client.get("/comics").status_code == 404
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(comicmeta, "search", lambda q, kind: [dict(SERIES, count=110)])
    page = client.get("/comics?q=one+piece&kind=manga").get_data(as_text=True)
    assert "One Piece" in page and "/comics/series/mangaupdates/77" in page

def test_a_reader_requests_volumes_from_the_series_page(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    items = [{"id": f"77:v{n}", "number": str(n), "label": f"Vol. {n}", "date": None, "cover": None} for n in (1, 2, 3)]
    monkeypatch.setattr(comicmeta, "series", lambda p, sid, lang="en": (dict(SERIES), items))
    _comic_book(9, "One Piece", 2, ["Manga", "owner:alice"])
    login(client, "bob", "bobpass1")
    page = client.get("/comics/series/mangaupdates/77").get_data(as_text=True)
    assert "in the family library" in page
    r = post(client, "/comics/request", provider="mangaupdates", series_id="77", number=["1", "2"], language="en", reading="rtl")
    assert "1 requested" in r.get_data(as_text=True) and "1 added from the family library" in r.get_data(as_text=True)
    assert sorted((x["number"], x["status"]) for x in db.comic_list("bob")) == [("1", "queued"), ("2", "shared")]
    assert client.get("/comics/series/bad/77").status_code == 404

def test_nobody_cancels_someone_elses_request(client, monkeypatch):
    monkeypatch.setattr(config, "COMICS_ENABLED", True)
    rid, _ = db.comic_add("alice", {"provider": "mangaupdates", "series_id": "77", "series_name": "One Piece",
                                    "number": "1", "language": "en"})
    login(client, "bob", "bobpass1")
    assert post(client, f"/comics/requests/{rid}/cancel").status_code == 404
    assert db.comic_get(rid)["status"] == "queued"


# ---- audiobooks to a phone or tablet ----------------------------------------------------------------
def test_an_audiobook_folder_downloads_as_one_zip_for_its_reader_only(client, monkeypatch):
    book = os.path.join(config.AUDIO_DIR, "bob", "Hobbit")
    os.makedirs(book)
    for n in ("01.mp3", "02.mp3"):
        open(os.path.join(book, n), "wb").write(os.urandom(2048))
    monkeypatch.setattr(absapi, "configured", lambda: True)
    def items(owner, is_admin=False, token=None):
        return [{"id": "li_1", "title": "The Hobbit", "author": "Tolkien", "path": book, "size": 4096, "is_file": False}] \
            if owner == "bob" or is_admin else []
    monkeypatch.setattr(absapi, "items_for", items)
    login(client, "bob", "bobpass1")
    assert "The Hobbit" in client.get("/audiobooks").get_data(as_text=True)
    r = client.get("/audiobooks/li_1/download")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(r.get_data())) as z:
        assert sorted(z.namelist()) == ["01.mp3", "02.mp3"] and z.testzip() is None
    post(client, "/logout")
    login(client, "alice", "alicepass1")
    assert client.get("/audiobooks/li_1/download").status_code == 404
    assert client.get("/audiobooks/..%2f..%2fetc/download").status_code == 404

def test_an_audiobook_outside_the_audio_folder_is_never_served(client, monkeypatch, tmp_path):
    monkeypatch.setattr(absapi, "items_for", lambda o, a=False, token=None: [
        {"id": "li_2", "title": "x", "author": "", "path": str(tmp_path), "size": 1, "is_file": False}])
    login(client, "bob", "bobpass1")
    assert client.get("/audiobooks/li_2/download").status_code == 404
