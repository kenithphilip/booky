"""v6.0 audit: each fix the end-to-end audit asked for, pinned by a test."""
import os, time
import pytest
import config, db, cwa, bookreq, share, notify, worker, hcwant
import abs as absapi
from conftest import login, post
from test_v60 import Shelf, _m4b_meta


@pytest.fixture
def audio_on(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(share, "find_audiobook", lambda t, a="", exclude=(): None)
    monkeypatch.setattr(share, "audiobook_owned_by", lambda t, a, o, exclude=(): None)


# ---- held arrivals ------------------------------------------------------------------------------------
def test_keep_it_anyway_works_for_a_held_audiobook_folder(audio_on, monkeypatch, tmp_path):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", release_title="Andy Weir - Project Hail Mary")
    monkeypatch.setattr(bookreq, "_expected_seconds", lambda req: 58253)
    src = os.path.join(config.DROPBOX_DIR, "bob", "Andy Weir - Project Hail Mary")
    os.makedirs(src); open(os.path.join(src, "01.mp3"), "wb").write(b"x")
    _m4b_meta(monkeypatch, seconds=3600, album="Project Hail Mary")
    assert bookreq.check_audio_arrival(src, "bob").startswith("skipped: held")
    held = db.bookreq_get(rid)["held_path"]
    assert os.path.isdir(held) and held.startswith(bookreq.held_dir("bob", rid)), "held beside the dropbox, not in /state"
    assert bookreq.keep(rid) == "downloading"
    r = db.bookreq_get(rid)
    assert r["skip_check"] and r["held_path"] is None and r["downloaded"], "the kept copy is the delivery"
    assert os.path.isfile(os.path.join(config.DROPBOX_DIR, "bob", "Andy Weir - Project Hail Mary", "01.mp3"))
    assert not os.path.exists(bookreq.held_dir("bob", rid))

def test_a_single_m4b_is_checked_when_only_an_audiobook_request_is_open(audio_on, monkeypatch, tmp_path):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", release_title="Andy Weir - Project Hail Mary [M4B]")
    monkeypatch.setattr(bookreq, "_expected_seconds", lambda req: 58253)
    f = tmp_path / "Andy Weir - Project Hail Mary [M4B].m4b"; f.write_bytes(b"x")
    _m4b_meta(monkeypatch, seconds=600, album="Project Hail Mary")
    assert (bookreq.check_arrival(str(f), "bob") or "").startswith("skipped: held"), "an .m4b is not waved through as 'no ebook request'"

def test_held_files_expire_and_the_request_looks_again(users, tmp_path):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    held = bookreq.held_dir("bob", rid); os.makedirs(held)
    open(os.path.join(held, "dune.epub"), "wb").write(b"x")
    db.bookreq_update(rid, status="held", held_path=os.path.join(held, "dune.epub"))
    assert worker.expire_held(now=time.time() + 3600) == 0, "not before HELD_DAYS"
    assert worker.expire_held(now=time.time() + (bookreq.HELD_DAYS + 1) * 86400) == 1
    r = db.bookreq_get(rid)
    assert r["status"] == "queued" and r["held_path"] is None and not os.path.exists(held)

def test_a_removed_accounts_requests_are_closed(users):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.set_prefs_v6("bob", ntfy_topic="lib-x", hc_want=1)
    assert worker.close_orphaned_requests() == 0
    cwa.remove_user("bob")
    assert worker.close_orphaned_requests() == 1
    assert db.bookreq_get(rid)["status"] == "cancelled"
    p = db.get_prefs("bob")
    assert p["ntfy_topic"] == "" and not p["hc_want"]


# ---- which request an arrival belongs to --------------------------------------------------------------
def test_an_arrival_goes_to_the_request_whose_whole_title_it_is():
    dune = {"id": 1, "title": "Dune", "author": "Frank Herbert"}
    messiah = {"id": 2, "title": "Dune Messiah", "author": "Frank Herbert"}
    assert bookreq._best([dune, messiah], "Frank Herbert - Dune Messiah", {})["id"] == 2
    assert bookreq._best([messiah, dune], "Frank Herbert - Dune", {})["id"] == 1
    rel = dict(messiah, release_title="Herbert, F - DM (retail)")
    assert bookreq._best([dune, rel], "Herbert, F - DM (retail)", {})["id"] == 2, "the exact release name first"
    assert bookreq._best([dune, messiah], "unrelated scan 0042", {}) is None


# ---- family sharing never hands back the copy a reader rejected ---------------------------------------
def test_family_sharing_skips_the_ebook_the_reader_called_wrong(users, monkeypatch):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="downloading", blocked=["book:7"])
    monkeypatch.setattr(share, "find_ebook", lambda t, a="", identifiers=(): {"book_id": 7, "how": "title+author", "owners": ["alice"]})
    assert worker._family_copy("bob", None, {"title": "Dune", "author": "Frank Herbert"}) is None
    monkeypatch.setattr(share, "find_ebook", lambda t, a="", identifiers=(): {"book_id": 8, "how": "title+author", "owners": ["bob"]})
    assert "already in your library" in worker._family_copy("bob", None, {"title": "Dune", "author": "Frank Herbert"})

def test_family_sharing_skips_the_audiobook_the_reader_called_wrong(users, monkeypatch):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    db.bookreq_update(rid, status="downloading", blocked=["abs:li5"])
    seen = []
    monkeypatch.setattr(share, "find_audiobook", lambda t, a="", exclude=(): seen.append(set(exclude)) or None)
    assert worker._family_audio("bob", "Andy Weir - Project Hail Mary") is None
    assert seen and all(s == {"li5"} for s in seen)

def _abs_item(iid, title, author, owners):
    return {"id": iid, "media": {"metadata": {"title": title, "authorName": author},
                                 "tags": [config.OWNER_PREFIX + o for o in owners]}}

def test_audiobook_matches_are_per_reader_and_the_most_shared_for_the_family(monkeypatch):
    monkeypatch.setattr(absapi, "configured", lambda: True)
    share._ABS_ITEMS.update(at=time.time(), items=[
        _abs_item("a", "Dune", "Frank Herbert", ["alice"]),
        _abs_item("b", "Dune", "Frank Herbert", ["bob", "carol"]),
        _abs_item("c", "Dune", "Frank Herbert", [])])
    assert share.audiobook_owned_by("Dune", "Frank Herbert", "alice")["item_id"] == "a", "alice's own copy, not the first"
    assert share.find_audiobook("Dune", "Frank Herbert")["item_id"] == "b"
    assert share.find_audiobook("Dune", "Frank Herbert", exclude={"b"})["item_id"] == "a"
    assert share.audiobook_owned_by("Dune", "Frank Herbert", "dave") is None


# ---- the dropbox classifier ---------------------------------------------------------------------------
def test_an_audiobook_folder_with_its_pdf_is_still_an_audiobook(tmp_path):
    d = tmp_path / "PHM"; d.mkdir()
    (d / "01.mp3").write_bytes(b"x"); (d / "supplement.pdf").write_bytes(b"%PDF")
    assert worker._classify_dir(str(d)) == "audio"
    (d / "other.mobi").write_bytes(b"x")
    assert worker._classify_dir(str(d)) == "mixed", "a mobi is a book, not a companion"


# ---- notifications ------------------------------------------------------------------------------------
def test_what_a_reader_dropped_in_is_not_pushed_to_their_phone(users, monkeypatch):
    pushed = []
    monkeypatch.setattr(notify, "_webhook", lambda e, r: None)
    monkeypatch.setattr(notify, "_mail", lambda e, r: None)
    monkeypatch.setattr(notify, "reader", lambda owner, title, text, **kw: pushed.append((owner, title)))
    notify.send("done", {"id": 1, "owner": "bob", "title": "Dune", "source": "dropbox"})
    assert pushed == []
    notify.send("done", {"id": 2, "owner": "bob", "title": "Emma", "source": "request"})
    assert pushed == [("bob", "Emma")]

def test_a_copy_waiting_for_a_yes_is_pushed_to_the_reader(audio_on, monkeypatch):
    pushed = []
    monkeypatch.setattr(notify, "reader", lambda owner, title, text, **kw: pushed.append((owner, title, kw.get("seq"))))
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    s = Shelf([{"source_id": "x", "title": "Andy Weir - Project Hail Mary (Unabridged) [M4B]", "protocol": "usenet", "size_bytes": 3e8}])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "confirm"
    assert pushed == [("bob", "Found: Project Hail Mary", f"book-{rid}")]


# ---- Want to Read -------------------------------------------------------------------------------------
def test_want_to_read_seeds_an_empty_list_and_again_after_being_switched_back_on(client, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    lst = []
    monkeypatch.setattr(hcwant, "want_list", lambda token: list(lst))
    assert hcwant.sync_owner("bob", "tok", "ebook") == 0
    lst.append({"id": 1, "title": "Dune", "author": "Frank Herbert"})
    assert hcwant.sync_owner("bob", "tok", "ebook") == 1, "an empty list was recorded: the first book added counts"
    login(client, "bob", "bobpass1")
    post(client, "/devices", action="hcwant", hc_want="")
    post(client, "/devices", action="hcwant", hc_want="1", hc_want_kind="ebook")
    assert db.get_prefs("bob")["hc_want_seeded"] is None
    lst.append({"id": 2, "title": "Emma", "author": "Jane Austen"})
    assert hcwant.sync_owner("bob", "tok", "ebook") == 0, "back on: what is on the list now is only recorded"


# ---- the cache keeps each entry for its own time ------------------------------------------------------
def test_a_short_lived_cache_entry_does_not_evict_a_long_lived_one(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(db.time, "time", lambda: t[0])
    db.cache_put("hc:long", {"x": 1}, keep_days=30)
    t[0] += 20 * 86400
    db.cache_put("ol:short", {"y": 2}, keep_days=14)
    assert db.cache_get("hc:long", None) == {"x": 1}
    t[0] += 11 * 86400
    db.cache_put("ol:other", {}, keep_days=14)
    assert db.cache_get("hc:long", None) is None and db.cache_get("ol:short", None) == {"y": 2}


# ---- the portal's forms may post to the sign-in page (and only there) ---------------------------------
def test_csp_lets_forms_reach_the_sign_in_page_only_when_it_is_on(client):
    import app as appmod
    csp = client.get("/login").headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    if config.AUTHELIA_ENABLED and config.DOMAIN:
        assert f"https://auth.{config.DOMAIN}" in appmod.CSP
    else:
        assert "auth." not in appmod.CSP


# ---- the admin's start-page guide and the reader's are different --------------------------------------
def test_a_reader_never_sees_the_admin_guide_link(client):
    login(client, "bob", "bobpass1")
    page = client.get("/help/getting-started").get_data(as_text=True)
    assert "/help/admin" not in page


# ---- second round (verify workflow) -------------------------------------------------------------------
def test_a_kept_copy_is_never_replaced_by_another_download(audio_on, monkeypatch):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    held = bookreq.held_dir("bob", rid); os.makedirs(held)
    open(os.path.join(held, "x.epub"), "wb").write(b"x")
    db.bookreq_update(rid, status="held", held_path=os.path.join(held, "x.epub"))
    bookreq.keep(rid)
    monkeypatch.setattr(share, "find_ebook", lambda *a, **k: None)
    later = time.time() + (config.BOOK_ARRIVAL_HOURS + 1) * 3600
    assert bookreq.watch_downloads(Shelf([]), {}, now=later) == (0, 0)
    r = db.bookreq_get(rid)
    assert r["status"] == "done" and "My books" in r["detail"]

def test_an_audiobook_still_downloading_is_not_downloaded_twice(audio_on):
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    t0 = time.time()
    db.bookreq_update(rid, status="downloading", release_title="Andy Weir - PHM [M4B]", queued_at=t0)
    q = {"downloading": {"1": {"title": "Andy Weir - PHM [M4B]"}}}
    assert bookreq.watch_downloads(Shelf([]), q, now=t0 + 30 * 3600) == (0, 0), "3x the time for audiobooks"
    assert bookreq.watch_downloads(Shelf([]), q, now=t0 + 80 * 3600) == (0, 0), "still under way in Shelfmark"
    assert db.bookreq_get(rid)["status"] == "downloading"
    assert bookreq.watch_downloads(Shelf([]), {}, now=t0 + 80 * 3600) == (0, 1)
    assert db.bookreq_get(rid)["status"] == "queued"

def test_a_confirmed_audiobook_waits_for_disk_space_without_asking_again(audio_on, monkeypatch):
    told = []
    monkeypatch.setattr(notify, "admin", lambda ev, d: told.append(ev))
    monkeypatch.setattr(notify, "reader", lambda *a, **k: told.append("reader"))
    rid, _ = bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")
    rel = {"source_id": "x", "title": "Andy Weir - Project Hail Mary (Unabridged) [M4B]", "protocol": "usenet", "size_bytes": 3e8}
    db.bookreq_update(rid, status="confirm", candidate=rel)
    room = [False]
    monkeypatch.setattr(bookreq, "_room_for", lambda r, **kw: room[0])
    s = Shelf([rel])
    assert bookreq.confirm(rid, s) == "queued"
    assert bookreq.search_once(db.bookreq_get(rid), s) == "queued"
    assert told == ["error"] and s.searched == [], "the admin is told once; nothing searched, the reader not asked again"
    room[0] = True
    assert bookreq.search_once(db.bookreq_get(rid), s) == "downloading" and s.queued == [("x", "audiobook")]

def test_a_zip_that_came_for_an_ebook_is_still_checked(users, tmp_path):
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert")
    db.bookreq_update(rid, status="downloading", release_title="Frank Herbert - Dune (retail)")
    f = tmp_path / "Frank Herbert - Dune (retail).zip"; f.write_bytes(b"PK\x03\x04")
    assert (bookreq.check_arrival(str(f), "bob") or "").startswith("skipped: held")

def test_a_hardcover_outage_never_blocks_an_audiobook(users, monkeypatch):
    import hardcover
    monkeypatch.setattr(hardcover, "configured", lambda: True)
    monkeypatch.setattr(hardcover, "_q", lambda *a, **k: (_ for _ in ()).throw(ValueError("not JSON")))
    assert bookreq._expected_seconds({"title": "Dune", "author": "Frank Herbert"}) is None

def test_the_family_audiobook_that_cannot_be_tagged_is_downloaded_instead(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(share, "audiobook_owned_by", lambda *a, **k: None)
    monkeypatch.setattr(share, "find_audiobook", lambda *a, **k: {"item_id": "li9", "how": "title+author", "owners": ["alice"]})
    monkeypatch.setattr(share, "give_audiobook", lambda m, o: (_ for _ in ()).throw(absapi.AbsError("403")))
    assert bookreq.request("bob", "Project Hail Mary", "Andy Weir", kind="audio")[1] == "queued"

def test_want_to_read_never_downloads_without_a_yes(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "BOOK_CONFIRM", "sure")
    monkeypatch.setattr(share, "find_ebook", lambda *a, **k: None)
    rid, _ = bookreq.request("bob", "Dune", "Frank Herbert", ask=True)
    rel = {"source_id": "x", "title": "Frank Herbert - Dune (retail) epub", "format": "epub", "protocol": "usenet", "size_bytes": 2e6}
    monkeypatch.setattr(bookreq.bookrel, "sure", lambda *a, **k: True)
    s = Shelf([rel])
    assert bookreq.search_once(db.bookreq_get(rid), s) == "confirm" and s.queued == []

def test_want_to_read_does_not_request_old_books_that_scroll_into_view(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    lst = [{"id": 30, "title": "C", "author": "X", "ub": 300}, {"id": 20, "title": "B", "author": "X", "ub": 200}]
    monkeypatch.setattr(hcwant, "want_list", lambda token: list(lst))
    hcwant.sync_owner("bob", "tok", "ebook")                        # recorded: C and B
    lst[:] = [lst[1], {"id": 10, "title": "A", "author": "X", "ub": 100}]   # C removed, A (older) now in view
    assert hcwant.sync_owner("bob", "tok", "ebook") == 0
    lst.insert(0, {"id": 40, "title": "D", "author": "X", "ub": 400})
    assert hcwant.sync_owner("bob", "tok", "ebook") == 1

def test_a_want_to_read_error_clears_once_it_works(users, monkeypatch):
    db.set_prefs_v6("bob", hc_want=1)
    monkeypatch.setattr(cwa, "hardcover_tokens", lambda: {})
    hcwant.sync_once()
    assert "no Hardcover token" in hcwant.note("bob")
    monkeypatch.setattr(cwa, "hardcover_tokens", lambda: {"bob": "t"})
    monkeypatch.setattr(hcwant, "want_list", lambda token: [])
    hcwant.sync_once()
    assert "no Hardcover token" not in (hcwant.note("bob") or "")

def test_series_numbers_are_shown_as_numbers():
    import app as appmod
    assert [appmod._num(x) for x in (3.0, 1.5, 1.05, 2.01, None, "x")] == ["3", "1.5", "1.05", "2.01", "", "x"]

def test_send_another_book_does_not_make_a_new_code_every_refresh(client):
    import re as _re
    code = lambda: _re.search(r'class="code">(\w+)<', client.get("/send").get_data(as_text=True)).group(1)
    first = code()
    r = client.get("/send?new=1")
    assert r.status_code == 302 and r.headers["Location"].endswith("/send"), "the refreshing page is plain /send"
    assert code() == first == code(), "a code still waiting for its book is kept"


def test_the_dashboard_leaves_comics_out_while_they_are_off(users, monkeypatch):
    import dash
    monkeypatch.setattr(config, "COMICS_ENABLED", False)
    with db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(comic_requests)")}
    assert "status" in cols
    assert not any("/comics" in (l or "") for _s, _t, l in dash.needs())

def test_a_series_page_keeps_the_ebook_and_the_audiobook_apart(client, monkeypatch):
    import app as appmod
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(share, "find_ebook", lambda *a, **k: None)
    monkeypatch.setattr(share, "find_audiobook", lambda *a, **k: None)
    monkeypatch.setattr(share, "audiobook_owned_by", lambda *a, **k: None)
    bookreq.request("bob", "Dune", "Frank Herbert", kind="audio")
    (row,) = appmod._book_rows("bob", [{"id": 1, "title": "Dune", "author": "Frank Herbert", "date": "1965-01-01"}])
    assert row["req"] is None and row["areq"]["kind"] == "audio"

def test_send_another_book_after_one_was_sent_gets_a_new_code(client):
    import re as _re, sendcode
    from conftest import add_calibre_book
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    code = lambda: _re.search(r'class="code">(\w+)<', client.get("/send").get_data(as_text=True)).group(1)
    first = code()
    sendcode.attach("bob", False, first, 1)
    assert "Your book is here" in client.get("/send").get_data(as_text=True)
    assert client.get("/send?new=1").status_code == 302
    assert code() != first, "Send another book: a fresh code, even before the first was downloaded"
