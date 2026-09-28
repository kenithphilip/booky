"""The admin's notifications (v5.6): every reader's events on the admin's ntfy topic, readable at a
glance (emoji, tap-to-open, a Review button), one notification per request or problem, and what
Shelfmark does that the portal only sees from outside: its requests and its failed downloads."""
import json
import pytest
import config, db, worker, notify
import abs as absapi
from conftest import add_calibre_book

NTFY = "https://ntfy.sh/family-books-9f3k"


@pytest.fixture
def hooks(monkeypatch):
    import urllib.request
    got = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: got.append(req))
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", NTFY)
    return got


def _h(req, name):
    return req.get_header(name.capitalize())


# ---- what the admin's phone shows ------------------------------------------------------------
def test_a_request_waiting_for_approval_has_a_review_button(hooks, users):
    notify._webhook("requested", {"id": 5, "owner": "alice", "title": "Emma", "author": "Jane Austen",
                                  "source": "gutenberg", "status": "pending"})
    (r,) = hooks
    assert _h(r, "Title") == "alice: Emma" and _h(r, "Priority") == "high" and _h(r, "Tags") == "raised_hand"
    assert _h(r, "Click") == "https://request.example.test/status"
    assert _h(r, "Actions") == "view, Review, https://request.example.test/status"
    assert _h(r, "Sequence-id") == "req-5"
    assert "waiting for your approval" in r.data.decode() and "Jane Austen" in r.data.decode()


def test_later_events_of_the_same_request_replace_it_quietly(hooks, users):
    notify._webhook("done", {"id": 5, "owner": "alice", "title": "Emma", "source": "gutenberg", "status": "done"})
    (r,) = hooks
    assert _h(r, "Sequence-id") == "req-5", "same id as the request: it replaces that notification"
    assert _h(r, "Priority") == "low" and _h(r, "Tags") == "books" and _h(r, "Actions") is None
    assert "added to their library" in r.data.decode(), "worded for the admin, not 'your library'"


def test_a_failure_is_loud(hooks, users):
    notify._webhook("error", {"id": 6, "owner": "bob", "title": "X", "status": "error", "detail": "not a zip"})
    assert _h(hooks[0], "Priority") == "high" and _h(hooks[0], "Tags") == "x" and "not a zip" in hooks[0].data.decode()


def test_server_alerts_carry_their_problem_id_and_the_all_clear_replaces_them(hooks, users):
    assert notify._cli(["alert", "Disk 91% full", "x", "high", "--seq", "disk-level"]) == 0
    assert notify._cli(["alert", "Disk back to 70%", "y", "--seq", "disk-level", "--tags", "white_check_mark"]) == 0
    bad, good = hooks
    assert _h(bad, "Sequence-id") == _h(good, "Sequence-id") == "disk-level"
    assert _h(bad, "Tags") == "rotating_light" and _h(good, "Tags") == "white_check_mark"
    assert _h(good, "Priority") == "default" and _h(bad, "Click") == "https://request.example.test/admin"


def test_header_values_stay_one_ascii_line():
    assert notify.seq_id("shelf-task", "a b/c") == "shelf-task-a-b-c" and len(notify.seq_id("x" * 99)) == 64
    assert notify._latin1("Émile\nZola") == "?mile Zola"


def test_a_generic_webhook_gets_the_same_facts_as_fields(monkeypatch, users):
    import urllib.request
    got = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: got.append(json.loads(req.data)))
    monkeypatch.setattr(config, "NOTIFY_WEBHOOK", "https://hook.example.test/x")
    notify._webhook("done", {"id": 9, "owner": "alice", "title": "Emma", "status": "done"})
    assert got[0]["event"] == "done" and got[0]["seq"] == "req-9" and got[0]["tags"] == "books"


def test_the_canary_never_reaches_the_admins_phone(hooks, monkeypatch):
    monkeypatch.setattr(config, "CANARY_USERS", ("canary-a",))
    notify.admin("error", {"owner": "canary-a", "title": "Canary"})
    assert hooks == []


# ---- Shelfmark, seen from the portal ---------------------------------------------------------
@pytest.fixture
def shelf(monkeypatch, users):
    import shelfmark_api
    st = {"rows": [], "decided": [], "queue": {}, "reads": 0, "told": []}
    def queue():
        st["reads"] += 1
        return st["queue"]
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(shelfmark_api, "configured", lambda: True)
    monkeypatch.setattr(shelfmark_api, "queue_status", queue)
    monkeypatch.setattr(shelfmark_api, "pending", lambda force=False, cache=True: list(st["rows"]))
    monkeypatch.setattr(shelfmark_api, "decide", lambda i, ok, note="": st["decided"].append((i, ok, note)))
    monkeypatch.setattr(notify, "admin", lambda ev, r: st["told"].append((ev, r.get("owner"), r.get("title"), r.get("status"), r.get("seq"))))
    monkeypatch.setattr(notify, "send", lambda ev, r: pytest.fail("a Shelfmark event must never mail the reader"))
    return st


def _req(i, user, title, author="Jane Austen"):
    return {"id": i, "requester": user, "title": title, "author": author, "kind": "ebook", "isbns": [],
            "source": "prowlarr", "format": "epub", "level": "release", "note": ""}


def test_the_admin_hears_what_the_gate_did(shelf):
    add_calibre_book(7, "Emma", "Jane Austen", tags=["owner:alice"])
    shelf["rows"] = [_req(1, "bob", "Emma"), _req(2, "bob", "Persuasion")]
    worker.shelfmark_gate_once()
    assert shelf["told"] == [("shared", "bob", "Emma", "shared", "shelf-1"),
                             ("requested", "bob", "Persuasion", "queued", "shelf-2")]


def test_a_request_left_for_the_admin_is_announced_once(shelf, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    shelf["rows"] = [_req(1, "bob", "Persuasion"), _req(2, "mallory", "Persuasion")]   # mallory: no library account
    for _ in range(3):                                          # the gate runs every 8 s
        worker.shelfmark_gate_once()
    assert [(t[0], t[1], t[3]) for t in shelf["told"]] == [("requested", "bob", "pending"), ("requested", "mallory", "pending")]


def test_a_failed_download_is_told_once_even_across_a_restart(shelf):
    # the shape Shelfmark v1.4.0 serializes (orchestrator._task_to_dict)
    shelf["queue"] = {"error": {"t1": {"id": "t1", "title": "Dune", "author": "Frank Herbert", "username": "bob", "user_id": 2,
                                       "status": "error", "status_message": "Download stalled for 300 s", "retry_available": True}},
                      "complete": {"t2": {"title": "Emma", "username": "alice"}}}
    worker.shelfmark_gate_once()
    worker.shelfmark_gate_once()
    assert shelf["told"] == [("error", "bob", "Dune", "error", "shelf-task-t1")]
    db.init()                                                   # a portal restart re-runs init
    worker.shelfmark_gate_once()
    assert len(shelf["told"]) == 1


def test_one_queue_read_serves_the_seedbox_list_and_the_failures(shelf, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "WAITING_FILE", str(tmp_path / "w.json"))
    worker._WAITING.update(last=None, at=0.0)
    shelf["queue"] = {"locating": {"t3": {"title": "Kite", "author": "K H", "status_message": "Waiting for completed files"}}}
    worker.shelfmark_gate_once()
    assert shelf["reads"] == 1 and json.load(open(tmp_path / "w.json"))["waiting"] == [{"title": "Kite", "author": "K H"}]


def test_an_unreadable_queue_never_blocks_the_gate(shelf, monkeypatch):
    import shelfmark_api
    monkeypatch.setattr(shelfmark_api, "queue_status", lambda: (_ for _ in ()).throw(shelfmark_api.ShelfmarkError("down")))
    shelf["rows"] = [_req(2, "bob", "Persuasion")]
    assert worker.shelfmark_gate_once() == (0, 1)


def test_notices_are_forgotten_after_a_month():
    assert db.first_notice("k", now=1000.0) and not db.first_notice("k", now=2000.0)
    assert db.first_notice("k", now=1000.0 + 31 * 86400), "older than 30 days: gone, so it could be told again"


# ---- Audiobookshelf's own backups ------------------------------------------------------------
def test_audiobookshelf_backups_are_switched_on_and_checked(monkeypatch):
    sent = []
    class R:
        status_code = 200
        text = ""
        def __init__(self, s): self.s = s
        def json(self): return {"serverSettings": self.s}
    def req(method, path, token=None, **kw):
        sent.append((method, path, kw["json"]))
        return R(dict(kw["json"]))
    monkeypatch.setattr(absapi, "_req", req)
    assert absapi.set_backups() == {"backupSchedule": "30 2 * * *", "backupsToKeep": 3, "maxBackupSize": 1}
    assert sent == [("PATCH", "/api/settings", {"backupSchedule": "30 2 * * *", "backupsToKeep": 3, "maxBackupSize": 1})]
    monkeypatch.setattr(absapi, "_req", lambda *a, **k: R({"backupSchedule": False}))
    with pytest.raises(absapi.AbsError, match="did not keep"):
        absapi.set_backups()
