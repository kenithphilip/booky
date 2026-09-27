"""On-demand conversion, and the fill-only push of covers, descriptions and the rest."""
import config, db, worker
from conftest import add_calibre_book, calibre_conn, login, post


def _meta(rid, owner, **extra):
    m = {"title": "Emma", "authors": [{"name": "Jane Austen"}], "_providers": ["openlibrary"],
         "identifiers": [{"kind": "isbn13", "value": "9780141439587", "exact": True},
                         {"kind": "goodreads_work", "value": "g1"}]}
    m.update(extra)
    return db.meta_store(m, rid=rid, owner=owner)


def _linked(owner="alice", **extra):
    add_calibre_book(1, "Emma", "Jane Austen", tags=[f"owner:{owner}"], formats=("epub",))
    rid = db.add(owner, {"kind": "ebook", "source": "gutenberg", "title": "Emma", "download_url": "x"}, status="done")
    _meta(rid, owner, **extra)
    db.link_calibre(rid, 1, owner)
    return rid


# ---- conversion -------------------------------------------------------------------------------
def test_a_reader_can_ask_for_another_format_of_her_own_book(client, users):
    _linked()
    login(client, "alice", users["alice"])
    html = client.get("/book/1").get_data(as_text=True)
    assert "Need another format?" in html and '<option value="azw3">' in html and '<option value="epub">' not in html
    r = post(client, "/book/1/convert", format="azw3")
    assert b"Converting to AZW3" in r.data and b"being made" in r.data
    (job,) = db.pending_converts()
    assert job["src_fmt"] == "epub" and job["dst_fmt"] == "azw3" and not job["src_path"].startswith(("/", ".."))


def test_nobody_converts_a_book_they_cannot_see(client, users):
    _linked("bob")
    login(client, "alice", users["alice"])
    assert post(client, "/book/1/convert", format="azw3").status_code == 404
    assert not db.pending_converts()


def test_only_known_targets_and_not_one_it_already_has(client, users):
    _linked()
    login(client, "alice", users["alice"])
    post(client, "/book/1/convert", format="exe"); post(client, "/book/1/convert", format="epub")
    assert not db.pending_converts()


def test_the_same_conversion_is_not_queued_twice_and_there_is_a_daily_limit(client, users, monkeypatch):
    _linked()
    monkeypatch.setattr(config, "CONVERT_MAX_PER_DAY", 2)
    login(client, "alice", users["alice"])
    post(client, "/book/1/convert", format="azw3")
    assert b"already being made" in post(client, "/book/1/convert", format="azw3").data
    post(client, "/book/1/convert", format="pdf")
    assert b"which is the limit" in post(client, "/book/1/convert", format="mobi").data
    assert len(db.pending_converts(10)) == 2


def test_the_host_reports_back(users):
    _linked()
    jid = db.convert_queue(1, "alice", "epub", "azw3", "Jane Austen/Emma (1)/Emma - Jane Austen.epub")
    assert db.convert_result(jid, False, "timeout")["status"] == "pending"
    assert db.convert_result(jid, False, "timeout again")["status"] == "failed"


# ---- fill-only push of the second half ------------------------------------------------------
def test_missing_cover_description_and_the_rest_are_queued(users):
    _linked(description="A comedy of errors.", cover_url="https://covers.openlibrary.org/b/id/1-L.jpg",
            publisher="Penguin", language="eng", release_date="2003-05-01")
    c = calibre_conn(config.CALIBRE_DB)                  # Calibre's "unknown" publication date
    c.execute("UPDATE books SET pubdate='0101-01-01 00:00:00+00:00' WHERE id=1"); c.commit(); c.close()
    worker.queue_device_pushes()
    (p,) = db.pending_pushes()
    f = p["fields"]
    assert f["comments"] == "A comedy of errors." and f["cover_url"].startswith("https://covers.openlibrary.org/")
    assert f["publisher"] == "Penguin" and f["languages"] == "en" and f["pubdate"] == "2003-05-01"
    assert f["identifiers"] == "isbn:9780141439587"
    assert "tags" not in f


def test_nothing_calibre_already_has_is_overwritten(users):
    _linked(description="Provider blurb.", cover_url="https://covers.openlibrary.org/b/id/1-L.jpg", publisher="Penguin")
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("INSERT INTO comments(book, text) VALUES(1, 'A hand-written note.')")
    c.execute("UPDATE books SET has_cover=1 WHERE id=1")
    c.execute("INSERT INTO identifiers(book, type, val) VALUES(1, 'isbn', '9780000000002')")
    c.commit(); c.close()
    worker.queue_device_pushes()
    f = db.pending_pushes()[0]["fields"]
    assert "comments" not in f and "cover_url" not in f and "identifiers" not in f
    assert f["publisher"] == "Penguin"


def test_a_cover_from_an_unknown_host_is_never_offered(users):
    _linked(cover_url="https://evil.example/x.jpg")
    worker.queue_device_pushes()
    assert "cover_url" not in (db.pending_pushes() or [{"fields": {}}])[0]["fields"]


def test_books_pushed_before_v5_get_one_more_look(users):
    rid = _linked(description="Blurb.")
    with db._conn() as c:
        c.execute("INSERT INTO device_push(calibre_id, rid, owner, fields, status, gen) VALUES(1, ?, 'alice', '{}', 'done', 1)", (rid,))
    worker.queue_device_pushes()
    assert db.pending_pushes()[0]["fields"]["comments"] == "Blurb."
    db.push_result(db.pending_pushes()[0]["id"], True)
    worker.queue_device_pushes()
    assert not db.pending_pushes(), "and only one"


def test_the_book_page_shows_the_provider_cover_until_calibre_has_one(client, users):
    _linked(cover_url="https://covers.openlibrary.org/b/id/42-L.jpg")
    login(client, "alice", users["alice"])
    html = client.get("/book/1").get_data(as_text=True)
    assert "/cover?u=https://covers.openlibrary.org/b/id/42-L.jpg" in html
