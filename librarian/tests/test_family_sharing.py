"""Family sharing (share.py): a book the family already has is given to the next reader who asks,
instead of being downloaded again — from Shelfmark, a dropbox, or the portal."""
import json, os, time, zipfile
import pytest
import config, db, worker, share, admin_cli, notify, cwa
import abs as absapi
from conftest import add_calibre_book, calibre_conn, make_epub


def _isbn(book_id, isbn):
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("INSERT INTO identifiers(book, type, val) VALUES(?, 'isbn', ?)", (book_id, isbn))
    c.commit(); c.close()


@pytest.fixture
def quiet(monkeypatch):
    told = []
    monkeypatch.setattr(notify, "send", lambda ev, r: told.append((ev, r.get("owner"), r.get("detail"))))
    return told


# ---- matching --------------------------------------------------------------------------------
def test_only_a_strong_match_shares(users):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    m = share.find_ebook("Emma", "Jane Austen")
    assert m["book_id"] == 7 and m["how"] == "title+author" and m["owners"] == ["alice"]
    assert share.find_ebook("Emma", "") is None, "title alone never shares"
    assert share.find_ebook("Emma", "Somebody Else") is None, "a disagreeing author is a no"
    assert share.find_ebook("Emma: A Novel (Penguin Classics)", "Austen, Jane")["book_id"] == 7, "subtitle and name order do not matter"


def test_an_isbn_is_enough(users):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"]); _isbn(7, "9780141439587")
    assert share.find_ebook("Emma (Annotated)", "J. A.", share.isbn_ids("978-0-14-143958-7"))["how"] == "isbn"


def test_an_untagged_book_is_an_import_in_progress_not_a_family_copy(users):
    add_calibre_book(7, "Emma", "Jane Austen", tags=[])
    assert share.find_ebook("Emma", "Jane Austen") is None


def test_the_switch_turns_it_off(users, monkeypatch):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    monkeypatch.setattr(config, "FAMILY_SHARING", False)
    assert share.find_ebook("Emma", "Jane Austen") is None


def test_find_audiobook_needs_title_and_author_and_one_item(monkeypatch):
    items = {"i1": {"id": "i1", "media": {"metadata": {"title": "The Hobbit", "authorName": "J. R. R. Tolkien"}, "tags": ["owner:alice"]}},
             "i2": {"id": "i2", "media": {"metadata": {"title": "Emma", "authorName": "Jane Austen"}, "tags": []}}}
    class R:
        def __init__(self, body): self.status_code, self.body = 200, body
    monkeypatch.setattr(absapi, "configured", lambda: True)
    monkeypatch.setattr(absapi, "ensure_library", lambda **k: ("lib", "x"))
    monkeypatch.setattr(absapi, "_req", lambda m, path, **k: R({"results": list(items.values())} if path.endswith("/items") else items[path.rsplit("/", 1)[1]]))
    monkeypatch.setattr(absapi, "_json", lambda r: r.body)
    m = share.find_audiobook("The Hobbit: 75th Anniversary Edition", "Tolkien")
    assert m == {"item_id": "i1", "how": "title+author", "owners": ["alice"]}
    assert share.find_audiobook("The Hobbit", "") is None, "an audiobook's name is all there is: both halves must agree"
    assert share.find_audiobook("Emma", "Jane Austen") is None, "no owner yet: not a family copy"


# ---- arrivals (dropbox, Shelfmark) -------------------------------------------------------------
def test_an_arrival_the_family_has_is_not_imported_and_goes_to_the_reader(users, quiet, tmp_path):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    rid = db.add("bob", {"kind": "ebook", "source": "dropbox", "title": "Emma.epub", "download_url": "local"})
    note = worker.ingest_local_file(str(src), "bob", rid)
    assert worker.FAMILY_NOTE in note and worker._status_for(note) == worker.NEEDS_TAG
    assert os.listdir(config.INGEST_DIR) == [], "nothing handed to Calibre-Web: no second copy"
    (job,) = db.pending_tag_pushes()
    assert (job["calibre_id"], job["owner"], job["rid"], job["share"]) == (7, "bob", rid, 1)
    worker._finish(rid, worker._status_for(note), note)
    assert quiet == [], "no 'needs an owner tag' alarm for a share in progress"
    worker.reconcile_untagged()
    assert len(db.pending_tag_pushes()) == 1, "the untagged-book reconciler leaves it alone"


def test_the_reader_is_told_once_the_host_has_added_the_tag(users, quiet, tmp_path, capsys):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    rid = db.add("bob", {"kind": "ebook", "source": "dropbox", "title": "Emma.epub", "download_url": "local"})
    note = worker.ingest_local_file(str(src), "bob", rid); worker._finish(rid, worker._status_for(note), note)
    job = db.pending_tag_pushes()[0]
    assert admin_cli.main(["tags", "result", str(job["id"]), "ok"]) == 0
    capsys.readouterr()
    assert db.get(rid)["status"] == "done" and [e for e, o, _ in quiet] == ["done"]


def test_a_book_the_reader_already_has_is_not_duplicated(users, tmp_path):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    note = worker.ingest_local_file(str(src), "alice", 1)
    assert note.startswith("already in your library") and worker._status_for(note) == "done"
    assert os.listdir(config.INGEST_DIR) == [] and not db.pending_tag_pushes()


def test_a_different_book_imports_as_before(users, tmp_path):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    src = make_epub(str(tmp_path / "Persuasion.epub"), "Persuasion", "Jane Austen")
    assert worker.ingest_local_file(str(src), "bob", 1) == "tagged owner:bob"
    assert len(os.listdir(config.INGEST_DIR)) == 1


def test_a_mobi_arrival_is_shared_by_its_own_metadata(users, tmp_path):
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=["owner:alice"])
    src = tmp_path / "whatever.mobi"
    src.write_bytes(open(os.path.join(os.path.dirname(__file__), "fixtures", "untaggable", "kite-runner.mobi"), "rb").read())
    note = worker.ingest_local_file(str(src), "bob", 3)
    assert worker.FAMILY_NOTE in note and db.pending_tag_pushes()[0]["owner"] == "bob"


def test_an_audiobook_arrival_the_family_has_is_not_added_again(users, monkeypatch, tmp_path):
    given = []
    monkeypatch.setattr(share, "find_audiobook", lambda t, a: {"item_id": "i1", "how": "title+author", "owners": ["alice"]}
                        if (t, a) == ("The Hobbit", "J. R. R. Tolkien") else None)
    monkeypatch.setattr(share, "give_audiobook", lambda m, owner: given.append((m["item_id"], owner)))
    z = tmp_path / "J. R. R. Tolkien - The Hobbit.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("01.mp3", b"ID3")
    note = worker.ingest_local_file(str(z), "bob", 4)
    assert note.startswith(worker.FAMILY_NOTE) and given == [("i1", "bob")]
    assert [n for n in os.listdir(config.AUDIO_DIR) if not n.startswith(".")] == [], "no second copy on disk"


def test_an_audiobook_folder_arrival_is_merged_and_the_folder_leaves(users, monkeypatch, tmp_path):
    monkeypatch.setattr(share, "find_audiobook", lambda t, a: {"item_id": "i1", "how": "title+author", "owners": ["alice"]})
    monkeypatch.setattr(share, "give_audiobook", lambda m, owner: None)
    d = tmp_path / "Tolkien - The Hobbit"; d.mkdir(); (d / "01.mp3").write_bytes(b"ID3")
    note = worker._place_audio_dir(str(d), "bob", "Tolkien - The Hobbit", 5)
    assert note.startswith(worker.FAMILY_NOTE) and not d.exists()
    assert [n for n in os.listdir(config.AUDIO_DIR) if not n.startswith(".")] == []


# ---- portal requests -------------------------------------------------------------------------
def test_a_portal_request_for_a_family_book_downloads_nothing(users, quiet, monkeypatch):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    monkeypatch.setattr(worker, "_place_ebook_http", lambda req: pytest.fail("nothing may be downloaded"))
    rid = db.add("bob", {"kind": "ebook", "source": "gutenberg", "title": "Emma", "author": "Jane Austen",
                         "download_url": "https://www.gutenberg.org/x.epub"})
    worker._process(db.get(rid))
    r = db.get(rid)
    assert r["status"] == worker.NEEDS_TAG and worker.FAMILY_NOTE in r["detail"] and "nothing downloaded" in r["detail"]
    assert db.pending_tag_pushes()[0]["owner"] == "bob" and quiet == []


# ---- the Shelfmark gate ----------------------------------------------------------------------
@pytest.fixture
def shelf(monkeypatch, users):
    import shelfmark_api
    st = {"rows": [], "decided": [], "queue": {}}
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)      # the family default
    monkeypatch.setattr(shelfmark_api, "configured", lambda: True)
    monkeypatch.setattr(shelfmark_api, "queue_status", lambda: st["queue"])
    monkeypatch.setattr(shelfmark_api, "pending", lambda force=False, cache=True: list(st["rows"]))
    monkeypatch.setattr(shelfmark_api, "decide", lambda i, ok, note="": st["decided"].append((i, ok, note)))
    return st


def _req(i, user, title, author, kind="ebook", isbns=()):
    return {"id": i, "requester": user, "title": title, "author": author, "kind": kind, "isbns": list(isbns),
            "source": "prowlarr", "format": "epub", "level": "release", "note": ""}


def test_the_gate_shares_a_family_book_and_approves_a_new_one(shelf, quiet):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    shelf["rows"] = [_req(1, "bob", "Emma", "Jane Austen"), _req(2, "bob", "Persuasion", "Jane Austen")]
    assert worker.shelfmark_gate_once() == (1, 1)
    assert shelf["decided"][0] == (1, False, worker.SHELF_SHARED), "shared: closed with the note, nothing downloaded"
    assert shelf["decided"][1] == (2, True, ""), "a new book is approved at once"
    (job,) = db.pending_tag_pushes()
    assert (job["calibre_id"], job["owner"], job["share"]) == (7, "bob", 1)
    row = db.get(job["rid"])
    assert row["owner"] == "bob" and row["status"] == worker.NEEDS_TAG and quiet == []


def test_the_gate_holds_new_books_for_the_admin_when_approvals_are_on(shelf, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    shelf["rows"] = [_req(1, "bob", "Persuasion", "Jane Austen"), _req(2, "bob", "Emma", "Jane Austen")]
    worker.shelfmark_gate_once()
    assert shelf["decided"] == [(2, False, worker.SHELF_SHARED)], "a family copy is still shared; the new book waits"


def test_the_gate_tells_a_reader_they_already_have_it(shelf):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    shelf["rows"] = [_req(1, "alice", "Emma", "Jane Austen")]
    worker.shelfmark_gate_once()
    assert shelf["decided"] == [(1, False, worker.SHELF_OWNED)] and not db.pending_tag_pushes()


def test_the_gate_shares_audiobooks_through_audiobookshelf(shelf, monkeypatch):
    given = []
    monkeypatch.setattr(share, "find_audiobook", lambda t, a: {"item_id": "i1", "how": "title+author", "owners": ["alice"]})
    monkeypatch.setattr(share, "give_audiobook", lambda m, owner: given.append(owner))
    shelf["rows"] = [_req(1, "bob", "The Hobbit", "Tolkien", kind="audiobook")]
    worker.shelfmark_gate_once()
    assert given == ["bob"] and shelf["decided"] == [(1, False, worker.SHELF_SHARED)]


def test_the_gate_leaves_unknown_requesters_to_the_admin(shelf):
    shelf["rows"] = [_req(1, "?", "Persuasion", "Jane Austen"), _req(2, "mallory", "Persuasion", "Jane Austen")]
    assert worker.shelfmark_gate_once() == (0, 0) and shelf["decided"] == []


def test_one_bad_request_does_not_block_the_rest(shelf, monkeypatch):
    import shelfmark_api
    calls = []
    def decide(i, ok, note=""):
        calls.append(i)
        if i == 1:
            raise shelfmark_api.ShelfmarkError("stale_transition")
    monkeypatch.setattr(shelfmark_api, "decide", decide)
    shelf["rows"] = [_req(1, "bob", "A", "X Y"), _req(2, "bob", "B", "X Y")]
    worker.shelfmark_gate_once()
    assert calls == [1, 2]


def test_the_reader_reads_plain_words():
    import app
    d = f"{worker.NEEDS_TAG}: {worker.FAMILY_NOTE} (matched by isbn), nothing downloaded; {worker.AUTO_TAG_NOTE}: owner:bob"
    assert "already in the family library" in app._friendly_detail(d) and "owner:" not in app._friendly_detail(d)


def test_the_gate_never_stales_the_admins_pending_card(monkeypatch):
    """The gate polls every few seconds; the page's 20 s cache must stay the page's own."""
    import shelfmark_api
    calls = []
    class R:
        status_code = 200
        def json(self): return [{"id": len(calls), "username": "bob", "book_data": {"title": f"B{len(calls)}"}}]
    monkeypatch.setattr(shelfmark_api, "configured", lambda: True)
    monkeypatch.setattr(shelfmark_api, "_call", lambda *a, **k: (calls.append(1), R())[1])
    shelfmark_api._state.update(pending=None, at=0)
    page = shelfmark_api.pending()                      # the page fills its cache
    gate = shelfmark_api.pending(cache=False)           # the gate fetches fresh, writes nothing
    assert shelfmark_api.pending() == page and gate != page and len(calls) == 2


# ---- 'Find a better copy' ----------------------------------------------------------------------
from conftest import login, post


def test_a_replace_window_opens_expires_and_cancels(users):
    now = time.time()
    jid = db.open_replace(7, "alice", now=now)
    assert db.open_replace(7, "alice", now=now) == jid, "one live window per book"
    assert db.replace_live(7, now=now)["status"] == "open"
    assert db.replace_live(7, now=now + db.REPLACE_DAYS * 86400 + 1) is None, "it expires"
    jid2 = db.open_replace(7, "bob", now=now + db.REPLACE_DAYS * 86400 + 2)
    assert jid2 != jid and db.cancel_replace(7) and db.replace_live(7) is None


def test_only_a_reader_who_has_the_book_can_ask(client):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    login(client, "bob", "bobpass1")
    assert post(client, "/book/7/replace", action="open").status_code == 404
    assert db.replace_live(7) is None
    import app as appmod
    alice = appmod.app.test_client(); login(alice, "alice", "alicepass1")
    page = post(alice, "/book/7/replace", action="open").get_data(as_text=True)
    assert "Looking for a better copy" in page and db.replace_live(7)["opened_by"] == "alice"
    page = post(alice, "/book/7/replace", action="cancel").get_data(as_text=True)
    assert "Find a better copy" in page and db.replace_live(7) is None


def test_an_epub_arrival_replaces_the_file_of_the_same_book(users, tmp_path):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    jid = db.open_replace(7, "alice")
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "Emma.epub", "download_url": "local"})
    note = worker.ingest_local_file(str(src), "alice", rid)
    assert note == worker.REPLACE_NOTE and worker._status_for(note) == "done"
    staged = os.path.join(config.STAGING_DIR, "replace", f"{jid}.epub")
    assert open(staged, "rb").read() == open(src, "rb").read(), "the file exactly as it came, not our tagged copy"
    assert os.listdir(config.INGEST_DIR) == [], "not imported as a new book"
    job = db.replace_live(7)
    assert job["status"] == "staged" and job["rid"] == rid and not db.pending_tag_pushes()


def test_a_better_copy_from_another_reader_also_gives_them_the_book(users, tmp_path):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    db.open_replace(7, "alice")
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    note = worker.ingest_local_file(str(src), "bob", 9)
    assert worker.REPLACE_NOTE in note and worker._status_for(note) == worker.NEEDS_TAG
    assert db.replace_live(7)["status"] == "staged" and db.pending_tag_pushes()[0]["owner"] == "bob"


def test_a_mobi_is_not_a_better_copy(users):
    add_calibre_book(7, "The Kite Runner", "Khaled Hosseini", tags=["owner:alice"])
    db.open_replace(7, "alice")
    src = os.path.join(os.path.dirname(__file__), "fixtures", "untaggable", "kite-runner.mobi")
    note = worker.ingest_local_file(src, "alice", 3)
    assert note.startswith("already in your library") and db.replace_live(7)["status"] == "open", "still waiting for an EPUB"


def test_the_gate_lets_an_epub_through_and_asks_for_one_otherwise(shelf):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    db.open_replace(7, "alice")
    epub, mobi = _req(1, "alice", "Emma", "Jane Austen"), _req(2, "alice", "Emma", "Jane Austen")
    mobi["format"] = "mobi"
    shelf["rows"] = [epub, mobi]
    worker.shelfmark_gate_once()
    assert shelf["decided"] == [(1, True, ""), (2, False, worker.SHELF_PICK_EPUB)]


def test_a_portal_request_downloads_while_a_better_copy_is_wanted(users, monkeypatch):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    db.open_replace(7, "alice")
    fetched = []
    monkeypatch.setattr(worker, "_place_ebook_http", lambda req: fetched.append(req["id"]))
    rid = db.add("alice", {"kind": "ebook", "source": "gutenberg", "title": "Emma", "author": "Jane Austen",
                           "download_url": "https://www.gutenberg.org/x.epub"})
    worker._process(db.get(rid))
    assert fetched == [rid]


def test_the_host_reports_the_swap(users, tmp_path, capsys):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    jid = db.open_replace(7, "alice")
    src = make_epub(str(tmp_path / "Emma.epub"), "Emma", "Jane Austen")
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "Emma.epub", "download_url": "local"})
    note = worker.ingest_local_file(str(src), "alice", rid); worker._finish(rid, "done", note)
    admin_cli.main(["replaces", "pending"])
    (row,) = json.loads(capsys.readouterr().out)["rows"]
    assert (row["id"], row["calibre_id"], row["fmt"]) == (jid, 7, "epub")
    assert admin_cli.main(["replaces", "result", str(jid), "ok"]) == 0
    capsys.readouterr()
    assert db.replace_for_book(7)["status"] == "done" and "replaced" in db.get(rid)["detail"]
