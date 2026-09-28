"""'Remove from my library', and deleting books no reader has any more from the VPS."""
import json, os, time
import pytest
import config, db, worker, admin_cli, cwa
from conftest import add_calibre_book, calibre_conn, login, post


def _drop_tag(book_id, tag):
    """What the host job does after a removal (the portal mounts the library read-only)."""
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("DELETE FROM books_tags_link WHERE book=? AND tag=(SELECT id FROM tags WHERE name=?)", (book_id, tag))
    c.commit(); c.close()


# ---- the reader's side -----------------------------------------------------------------------
def test_the_page_says_what_happens_and_how_to_clear_devices(client):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice", "owner:bob"])
    login(client, "alice", "alicepass1")
    page = client.get("/book/7/remove").get_data(as_text=True)
    assert "Remove from My Books" in page and "Remove from Device" in page, "Kobo and Kindle steps"
    assert "Anyone else in the family who has it keeps it" in page
    assert not db.pending_tag_pushes(), "a GET changes nothing"


def test_removing_queues_only_this_readers_tag_and_hides_it_at_once(client):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice", "owner:bob"])
    add_calibre_book(8, "Persuasion", "Jane Austen", tags=["owner:alice"])
    login(client, "alice", "alicepass1")
    post(client, "/book/7/remove")
    (job,) = db.pending_tag_pushes()
    assert (job["calibre_id"], job["owner"], job["op"]) == (7, "alice", "remove")
    page = client.get("/library").get_data(as_text=True)
    assert "Persuasion" in page and "Emma" not in page, "gone from My books before the host job even runs"


def test_nobody_removes_a_book_from_someone_elses_shelf(client):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    login(client, "bob", "bobpass1")
    assert client.get("/book/7/remove").status_code == 404
    assert post(client, "/book/7/remove").status_code == 404 and not db.pending_tag_pushes()


def test_a_pending_share_is_withdrawn_instead_of_fighting_it(users):
    db.queue_tag_push(7, None, "bob", share=True)
    db.queue_untag(7, "bob")
    (job,) = db.pending_tag_pushes()
    assert job["op"] == "remove"


# ---- the countdown ---------------------------------------------------------------------------
def test_the_last_reader_removing_it_starts_the_countdown(users, capsys):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice", "owner:bob", "Classics"])
    for who in ("alice", "bob"):
        db.queue_untag(7, who)
        job = [j for j in db.pending_tag_pushes() if j["owner"] == who][0]
        _drop_tag(7, f"owner:{who}")
        admin_cli.main(["tags", "result", str(job["id"]), "ok"])
        capsys.readouterr()
        rel = db.releases()
        assert (rel == []) if who == "alice" else (rel[0]["calibre_id"] == 7 and json.loads(rel[0]["tags"]) == []), \
            "bob still had it after alice left; after bob, nobody does"


def test_due_after_the_days_and_handed_to_the_host(users, capsys):
    add_calibre_book(7, "Emma", "Jane Austen", tags=[])
    db.release_note(7, "its last reader removed it", [], now=1000.0)
    worker.reconcile_releases(now=1000.0 + config.LIBRARY_RELEASE_DAYS * 86400 - 60)
    assert db.releases(("due",)) == [], "not yet"
    assert worker.reconcile_releases(now=1000.0 + config.LIBRARY_RELEASE_DAYS * 86400 + 60) == 1
    admin_cli.main(["releases", "due"])
    assert json.loads(capsys.readouterr().out)["rows"] == [{"calibre_id": 7, "tags": []}]
    admin_cli.main(["releases", "result", "7", "ok"]); capsys.readouterr()
    assert db.releases(("deleted",))[0]["calibre_id"] == 7


def test_someone_asking_again_stops_the_countdown(users):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:bob"])        # shared back to bob meanwhile
    db.release_note(7, "its last reader removed it", [], now=1000.0)
    worker.reconcile_releases(now=1000.0 + 30 * 86400)
    assert db.releases(("due",)) == [] and db.releases(("kept",))[0]["calibre_id"] == 7


def test_a_book_whose_readers_accounts_are_gone_is_counted_down(users):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:carol"])       # carol's account was removed
    add_calibre_book(8, "Persuasion", "Jane Austen", tags=["owner:alice"])
    worker.reconcile_releases(now=1000.0)
    (r,) = db.releases()
    assert r["calibre_id"] == 7 and json.loads(r["tags"]) == ["owner:carol"]


def test_a_book_that_never_had_an_owner_is_never_touched(users):
    add_calibre_book(7, "The Admin's Own Book", "Someone", tags=["Fiction"])
    worker.reconcile_releases(now=1000.0)
    worker.reconcile_releases(now=1000.0 + 365 * 86400)
    assert db.releases(("waiting", "due", "kept")) == []


def test_it_never_reads_an_unreadable_user_list_as_nobody(users, monkeypatch):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    monkeypatch.setattr(cwa, "list_users", lambda include_canary=False: [])
    worker.reconcile_releases(now=1000.0)
    assert db.releases() == []


def test_zero_days_turns_it_off(users, monkeypatch):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:carol"])
    monkeypatch.setattr(config, "LIBRARY_RELEASE_DAYS", 0)
    assert worker.reconcile_releases(now=1000.0) == 0 and db.releases() == []


def test_a_host_refusal_keeps_the_book(users):
    db.release_note(7, "x", [], now=1000.0)
    db.release_due(7)
    assert db.release_result(7, False, "refused: its owners are now ['owner:bob']")["status"] == "kept"


# ---- what Shelfmark is waiting for, for the seedbox job ----------------------------------------
def test_the_waiting_list_is_written_for_the_host(users, monkeypatch, tmp_path):
    class S:
        @staticmethod
        def waiting_for_files(): return [{"title": "The Kite Runner", "author": "Khaled Hosseini"}]
    monkeypatch.setattr(worker, "WAITING_FILE", str(tmp_path / "seedbox-wanted.json"))
    worker._WAITING.update(last=None, at=0.0)
    worker._export_waiting(S, now=5000.0)
    d = json.load(open(tmp_path / "seedbox-wanted.json"))
    assert d == {"at": 5000, "waiting": [{"title": "The Kite Runner", "author": "Khaled Hosseini"}]}


# ---- is anyone using it? (scripts/mem-tidy.sh asks before a nightly restart) --------------------
def _cli(capsys, *argv):
    rc = admin_cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out)


def test_shelfmark_is_busy_while_anything_is_queued_or_downloading(users, monkeypatch, capsys):
    import shelfmark_api
    class R:
        status_code = 200
        def __init__(self, st): self.st = st
        def json(self): return self.st
    monkeypatch.setattr(shelfmark_api, "configured", lambda: True)
    monkeypatch.setattr(shelfmark_api, "_call", lambda *a, **k: R({"complete": {"a": {}}, "error": {"b": {}}}))
    assert _cli(capsys, "busy", "shelfmark")[1]["busy"] is False, "finished and failed tasks do not count"
    monkeypatch.setattr(shelfmark_api, "_call", lambda *a, **k: R({"downloading": {"a": {}}}))
    assert _cli(capsys, "busy", "shelfmark")[1] == {"ok": True, "busy": True, "active": 1}


def test_audiobookshelf_is_busy_while_anyone_listens(users, monkeypatch, capsys):
    import abs as absapi
    class R:
        status_code = 200
    monkeypatch.setattr(absapi, "configured", lambda: True)
    monkeypatch.setattr(absapi, "_req", lambda *a, **k: R())
    monkeypatch.setattr(absapi, "_json", lambda r: {"usersOnline": [], "openSessions": [{"id": "s1"}]})
    assert _cli(capsys, "busy", "audiobookshelf")[1]["busy"] is True
    monkeypatch.setattr(absapi, "_json", lambda r: {"usersOnline": [], "openSessions": []})
    assert _cli(capsys, "busy", "audiobookshelf")[1]["busy"] is False


def test_an_unreachable_service_is_never_reported_idle(users, monkeypatch, capsys):
    import shelfmark_api
    monkeypatch.setattr(shelfmark_api, "configured", lambda: True)
    monkeypatch.setattr(shelfmark_api, "_call", lambda *a, **k: (_ for _ in ()).throw(shelfmark_api.ShelfmarkError("down")))
    rc, out = _cli(capsys, "busy", "shelfmark")
    assert rc == 1 and out["ok"] is False and "busy" not in out, "mem-tidy reads anything but busy:false as busy"
