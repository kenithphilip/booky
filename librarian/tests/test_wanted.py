"""Keep looking: a book no catalog had when the reader searched.

What must hold, each pinned below:
  * matching is exact on the normalised title, never 'contains'; a disagreeing author is a no;
    a missing author can only ever make a CANDIDATE for the reader to confirm;
  * a confident match becomes an ORDINARY request — same source allow-list, same approval
    setting, same daily limit — never a side door around them;
  * a reader's list is theirs: no other reader can see, cancel, confirm or reject it;
  * a cancel always wins over a search that was already running;
  * the schedule widens, entries expire and the reader is told.
"""
import time
import pytest
import config, db, wanted, worker, fetchers, metadata
from conftest import add_calibre_book, login, post

PG = "https://www.gutenberg.org/ebooks/1342.epub3.images"


def _res(title, author, source="gutenberg", url=PG, src_ids=()):
    return {"source": source, "title": title, "author": author, "download_url": url,
            "identifier": "pg:1342", "kind": "ebook", "src_ids": list(src_ids)}


def _want(**kw):
    w = {"kind": "ebook", "title": "Pride and Prejudice", "author": "Jane Austen", "identifiers": []}
    w.update(kw)
    return w


# ---- matching (pure) --------------------------------------------------------------------------
def test_an_exact_title_and_agreeing_author_is_confident():
    conf, reasons = wanted.score(_want(), _res("Pride and Prejudice", "Austen, Jane"))
    assert conf >= wanted.AUTO and any("author" in r for r in reasons)


def test_a_subtitle_or_leading_article_does_not_prevent_a_match():
    assert wanted.score(_want(title="The Hobbit", author="Tolkien"),
                        _res("Hobbit: or There and Back Again", "J. R. R. Tolkien"))[0] >= wanted.AUTO


def test_contains_is_not_a_match():
    """'Emma' is contained in a great many titles; only equality after normalising counts."""
    assert wanted.score(_want(title="Emma", author="Jane Austen"),
                        _res("Emma and the Vampires", "Jane Austen")) is None


def test_a_known_author_that_disagrees_rules_the_result_out():
    assert wanted.score(_want(title="Emma"), _res("Emma", "Wayne Josephson")) is None


def test_a_missing_author_is_never_confidence():
    """Absent evidence is not agreement — the Readarr trap."""
    no_author_result = wanted.score(_want(), _res("Pride and Prejudice", ""))
    no_author_wanted = wanted.score(_want(author=""), _res("Pride and Prejudice", "Jane Austen"))
    assert no_author_result[0] < wanted.AUTO and no_author_wanted[0] < wanted.AUTO


def test_a_matching_isbn_is_the_strongest_evidence():
    w = _want(author="", identifiers=[{"kind": "isbn13", "value": "9780141439518"}])
    conf, reasons = wanted.score(w, _res("Pride and Prejudice", "", src_ids=[("isbn", "978-0-14-143951-8")]))
    assert conf == 1.0 and "same ISBN" in reasons


def test_an_audiobook_is_not_the_ebook_that_was_wanted():
    assert wanted.score(_want(), _res("Pride and Prejudice", "Jane Austen", source="librivox")) is None
    assert wanted.score(_want(kind="audio"), _res("Pride and Prejudice", "Jane Austen", source="librivox"))


def test_best_prefers_confidence_then_the_better_edition_and_skips_rejected():
    se = _res("Pride and Prejudice", "Jane Austen", source="standard_ebooks",
              url="https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice")
    ia = _res("Pride and Prejudice", "Jane Austen", source="internet_archive",
              url="https://archive.org/download/x/x.epub")
    r, conf, _ = wanted.best(_want(), [ia, se])
    assert r["source"] == "standard_ebooks"
    r, _, _ = wanted.best(_want(), [ia, se], rejected=[se["download_url"]])
    assert r["source"] == "internet_archive", "a download the reader turned down is never offered again"


def test_the_schedule_widens_then_settles_at_daily():
    mid = lambda: 0.5                                  # no jitter
    assert [round(wanted.next_delay(n, mid)) for n in (0, 1, 2, 7)] == [3600, 21600, 86400, 86400]


def test_the_same_book_twice_is_one_entry():
    assert wanted.same_want({"kind": "ebook", "title": "The Hobbit", "author": ""},
                            {"kind": "ebook", "title": "Hobbit", "author": "Tolkien"})
    assert not wanted.same_want({"kind": "ebook", "title": "Emma", "author": "Jane Austen"},
                                {"kind": "ebook", "title": "Emma", "author": "Wayne Josephson"})


# ---- the reader's side -----------------------------------------------------------------------
def test_a_search_with_no_results_offers_to_keep_looking(client, users, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [])
    login(client, "alice", users["alice"])
    html = client.get("/?q=Some+Unpublished+Book").get_data(as_text=True)
    assert 'action="/wanted"' in html and 'value="Some Unpublished Book"' in html


def test_keep_looking_creates_an_entry_shown_on_status(client, users):
    login(client, "alice", users["alice"])
    r = post(client, "/wanted", title="Pride and Prejudice", author="Jane Austen", kind="ebook")
    assert b"keep looking" in r.data.lower() and b"Still looking" in r.data
    (w,) = db.wanted_list("alice")
    assert w["status"] == "looking" and w["next_check"] > time.time() + 3000, \
        "the first look is an hour out: the reader has just searched and found nothing"


def test_the_same_book_is_not_added_twice_and_the_limit_holds(client, users, monkeypatch):
    login(client, "alice", users["alice"])
    post(client, "/wanted", title="Emma", author="Jane Austen", kind="ebook")
    r = post(client, "/wanted", title="EMMA", author="Austen", kind="ebook")
    assert b"already looking" in r.data and len(db.wanted_list("alice")) == 1
    monkeypatch.setattr(config, "WANTED_MAX_PER_USER", 2)
    post(client, "/wanted", title="Persuasion", author="Jane Austen", kind="ebook")
    r = post(client, "/wanted", title="Sanditon", author="Jane Austen", kind="ebook")
    assert b"already keeping an eye out for 2" in r.data and len(db.wanted_list("alice")) == 2


def test_a_readers_list_is_private(client, users):
    login(client, "alice", users["alice"])
    post(client, "/wanted", title="Emma", author="Jane Austen", kind="ebook")
    (w,) = db.wanted_list("alice")
    post(client, "/logout")
    login(client, "bob", users["bob"])
    assert b"Emma" not in client.get("/status").data
    for action in ("cancel", "accept", "reject"):
        assert post(client, f"/wanted/{w['id']}/{action}").status_code == 404
    assert db.wanted_get(w["id"])["status"] == "looking"
    post(client, "/logout")
    login(client, "admin", users["admin"])
    assert b"Emma" in client.get("/status").data, "the admin sees every reader's list"


def test_stop_looking(client, users):
    login(client, "alice", users["alice"])
    post(client, "/wanted", title="Emma", author="Jane Austen", kind="ebook")
    (w,) = db.wanted_list("alice")
    post(client, f"/wanted/{w['id']}/cancel")
    assert db.wanted_get(w["id"])["status"] == "cancelled"


# ---- the worker's side ----------------------------------------------------------------------
@pytest.fixture
def entry(users):
    wid, _ = db.wanted_add("alice", "ebook", "Pride and Prejudice", "Jane Austen", first_check=0,
                           limit=0, same=wanted.same_want)
    return wid


@pytest.fixture
def no_metadata(monkeypatch):
    monkeypatch.setattr(config, "METADATA_ENABLED", False)


def test_a_confident_match_becomes_an_ordinary_request(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Pride and Prejudice", "Austen, Jane")])
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    assert worker.check_wanted(db.wanted_get(entry)) == "requested"
    w = db.wanted_get(entry)
    req = db.get(w["rid"])
    assert w["status"] == "found" and req["status"] == "queued" and req["owner"] == "alice"
    assert req["download_url"] == PG and req["match_confidence"] >= wanted.AUTO
    assert "author" in req["match_reasons"]


def test_approvals_still_apply(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Pride and Prejudice", "Jane Austen")])
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", True)
    worker.check_wanted(db.wanted_get(entry))
    assert db.get(db.wanted_get(entry)["rid"])["status"] == "pending"


def test_the_daily_limit_still_applies_and_the_match_is_kept(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Pride and Prejudice", "Jane Austen")])
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_DAY", 1)
    db.add("alice", {"kind": "ebook", "source": "gutenberg", "title": "Other",
                     "download_url": "https://www.gutenberg.org/ebooks/1.epub3.images"})
    assert worker.check_wanted(db.wanted_get(entry)) == "waiting"
    w = db.wanted_get(entry)
    assert w["status"] == "candidate" and w["rid"] is None and "limit" in w["detail"]
    assert w["next_check"] < time.time() + 2 * 3600, "retried within the hour, not tomorrow"


def test_a_tampered_download_address_is_never_requested(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(fetchers, "search",
                        lambda q, **k: [_res("Pride and Prejudice", "Jane Austen", url="https://evil.example/x.epub")])
    assert worker.check_wanted(db.wanted_get(entry)) == "waiting"
    assert db.wanted_get(entry)["rid"] is None and not db.rows_by_status(("queued", "pending"))


def test_an_uncertain_match_waits_for_the_reader_then_yes_requests_it(client, users, no_metadata, monkeypatch):
    wid, _ = db.wanted_add("alice", "ebook", "Pride and Prejudice", "", first_check=0, limit=0,
                           same=wanted.same_want)
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Pride and Prejudice", "Jane Austen")])
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    assert worker.check_wanted(db.wanted_get(wid)) == "candidate"
    assert not db.rows_by_status(("queued",)), "no author given: never requested without asking"
    login(client, "alice", users["alice"])
    page = client.get("/status").get_data(as_text=True)
    assert "possible match" in page and "Yes, request it" in page and "www.gutenberg.org" in page
    post(client, f"/wanted/{wid}/accept")
    w = db.wanted_get(wid)
    assert w["status"] == "found" and db.get(w["rid"])["download_url"] == PG
    assert "confirmed by alice" in db.get(w["rid"])["match_reasons"]


def test_not_it_goes_back_to_looking_and_never_offers_that_file_again(client, users, no_metadata, monkeypatch):
    wid, _ = db.wanted_add("alice", "ebook", "Pride and Prejudice", "", first_check=0, limit=0,
                           same=wanted.same_want)
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Pride and Prejudice", "Jane Austen")])
    worker.check_wanted(db.wanted_get(wid))
    login(client, "alice", users["alice"])
    post(client, f"/wanted/{wid}/reject")
    w = db.wanted_get(wid)
    assert w["status"] == "looking" and w["rejected"] == [PG] and w["candidate"] is None
    assert worker.check_wanted(dict(w, next_check=0)) == "nothing"


def test_a_book_already_in_the_readers_library_closes_the_entry(entry, no_metadata, monkeypatch):
    add_calibre_book(1, "Pride and Prejudice", "Jane Austen", tags=["owner:alice"])
    called = []
    monkeypatch.setattr(fetchers, "search", lambda q, **k: called.append(q) or [])
    assert worker.check_wanted(db.wanted_get(entry)) == "in-library"
    assert db.wanted_get(entry)["status"] == "found" and not called, "no catalog search for a book she has"


def test_a_siblings_copy_does_not_count(entry, no_metadata, monkeypatch):
    add_calibre_book(1, "Pride and Prejudice", "Jane Austen", tags=["owner:bob"])
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [])
    assert worker.check_wanted(db.wanted_get(entry)) == "nothing"


def test_a_cancel_during_the_search_wins(entry, no_metadata, monkeypatch):
    def search_then_cancel(q, **k):
        db.wanted_update(entry, status="cancelled")          # the reader clicks Stop meanwhile
        return [_res("Pride and Prejudice", "Jane Austen")]
    monkeypatch.setattr(fetchers, "search", search_then_cancel)
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    worker.check_wanted(db.wanted_get(entry))
    assert db.wanted_get(entry)["status"] == "cancelled"


def test_nothing_found_reschedules_and_counts(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [_res("Emma", "Jane Austen")])
    now = time.time()
    assert worker.check_wanted(db.wanted_get(entry), now) == "nothing"
    w = db.wanted_get(entry)
    assert w["checks"] == 1 and now + 5 * 3600 < w["next_check"] < now + 7 * 3600
    assert "1 unrelated result" in w["detail"]


def test_an_entry_for_a_removed_account_is_closed(entry, no_metadata):
    import cwa
    cwa.remove_user("alice")
    assert worker.check_wanted(db.wanted_get(entry)) == "gone"
    assert db.wanted_get(entry)["status"] == "cancelled"


def test_old_entries_expire_and_the_reader_is_told(entry, no_metadata, monkeypatch):
    told = []
    monkeypatch.setattr(worker.notify, "send", lambda ev, r: told.append((ev, r["title"])))
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [])
    worker.wanted_once(now=time.time() + (config.WANTED_DAYS + 1) * 86400)
    assert db.wanted_get(entry)["status"] == "expired"
    assert ("wanted-expired", "Pride and Prejudice") in told


def test_the_disk_watchdog_pauses_it(entry, no_metadata, monkeypatch):
    monkeypatch.setattr(worker, "_disk_paused", lambda: True)
    monkeypatch.setattr(fetchers, "search", lambda q, **k: pytest.fail("searched while paused"))
    assert worker.wanted_once() == 0


def test_isbns_from_metadata_are_adopted_only_for_the_same_book(entry, monkeypatch):
    monkeypatch.setattr(config, "METADATA_ENABLED", True)
    monkeypatch.setattr(metadata, "negative_cached", lambda *a, **k: False)
    monkeypatch.setattr(metadata, "fetch", lambda q, now=None: (
        {"title": "Pride and Prejudice", "authors": [{"name": "Jane Austen"}],
         "identifiers": [{"kind": "isbn13", "value": "9780141439518"},
                         {"kind": "goodreads_work", "value": "1"}]}, []))
    ids = worker._wanted_ids(db.wanted_get(entry), time.time())
    assert ids == [{"kind": "isbn13", "value": "9780141439518"}]
    monkeypatch.setattr(metadata, "fetch", lambda q, now=None: (
        {"title": "Pride and Prejudice and Zombies", "authors": [{"name": "Seth Grahame-Smith"}],
         "identifiers": [{"kind": "isbn13", "value": "9781594743344"}]}, []))
    assert worker._wanted_ids(db.wanted_get(entry), time.time()) == [], \
        "a different book's ISBN must never vouch for a result"


def test_a_rename_carries_the_list(entry):
    db.rename_owner("alice", "alicia")
    assert db.wanted_list("alicia") and not db.wanted_list("alice")


def test_the_worker_runs_it_on_its_own_loop_and_health_watches_it():
    import inspect, app
    assert '"wanted", wanted_once' in inspect.getsource(worker.run_forever)
    assert '"wanted"' in inspect.getsource(app._health)


def test_the_admin_page_shows_degraded_reads_breakers_and_the_list(client, users, monkeypatch):
    import app, library
    now = time.time()
    monkeypatch.setattr(worker, "HEARTBEAT", {"queue": now, "dropbox": now, "housekeeping": now, "wanted": now})
    db.wanted_add("alice", "ebook", "Emma", "Jane Austen", first_check=0, limit=0, same=wanted.same_want)
    db.breaker_record("bookinfo", False, "HTTP 503", now, threshold=1)
    monkeypatch.setattr(library, "STALE_READ", ["read in immutable mode"])
    monkeypatch.setattr(library, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("no read")))
    login(client, "admin", users["admin"])
    html = client.get("/admin").get_data(as_text=True)
    assert "read in immutable mode" in html and "bookinfo" in html and "Still looking" in html
    h = app._health()
    assert h["library_read"] == "read in immutable mode" and h["metadata_down"] == ["bookinfo"]
    assert h["ok"], "degraded reads and a stood-down provider are shown, not a reason to restart the portal"
