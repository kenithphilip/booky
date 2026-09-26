"""Book, author and series pages.

The metadata store is household-wide, so these pages are where the per-reader isolation could
leak without anyone noticing: a series page that says 'book 4 exists' can only know it because
a sibling imported book 4. Each isolation rule is pinned here.
"""
import config, db
from conftest import add_calibre_book, calibre_conn, login, post


def _describe(book_id, html):
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("INSERT INTO comments(book, text) VALUES(?, ?)", (book_id, html))
    c.commit(); c.close()


def _enrich(owner, book_id, title, author, series=None, pos=None):
    rid = db.add(owner, {"kind": "ebook", "source": "dropbox", "title": title, "author": author,
                         "download_url": "local"}, status="done")
    m = {"title": title, "authors": [{"name": author}], "_providers": ["bookinfo"],
         "identifiers": [{"kind": "goodreads_work", "value": f"w{book_id}"}]}
    if series:
        m.update(series=series, series_position=pos)
    wid = db.meta_store(m, rid=rid, owner=owner)
    db.link_calibre(rid, book_id, owner)
    return wid


def test_a_book_page_is_a_404_for_a_book_you_cannot_see(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:bob"])
    login(client, "alice", users["alice"])
    assert client.get("/book/1").status_code == 404
    assert client.get("/book/1/cover").status_code == 404


def test_a_book_page_shows_the_book_and_renders_the_description_as_text(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    _describe(1, "<p>A <b>clever</b> heroine.</p><script>alert(1)</script>")
    login(client, "alice", users["alice"])
    r = client.get("/book/1")
    assert r.status_code == 200
    assert b"Emma" in r.data and b"Jane Austen" in r.data
    assert b"clever" in r.data and b"<b>clever</b>" not in r.data
    assert b"<script>" not in r.data, "comments come from files and providers: never markup"


def test_only_an_admin_sees_who_else_owns_a_book(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice", "owner:bob"])
    login(client, "alice", users["alice"])
    assert b"On the shelf of" not in client.get("/book/1").data
    post(client, "/logout")
    login(client, "admin", users["admin"])
    assert b"On the shelf of" in client.get("/book/1").data


def test_the_portals_description_fills_a_gap_calibre_leaves(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "Emma", "author": "",
                           "download_url": "local"}, status="done")
    db.meta_store({"title": "Emma", "description": "From the provider.",
                   "first_publish_year": 1815, "authors": [{"name": "Jane Austen"}],
                   "_providers": ["bookinfo"]}, rid=rid, owner="alice")
    db.link_calibre(rid, 1, "alice")
    login(client, "alice", users["alice"])
    r = client.get("/book/1")
    assert b"From the provider." in r.data and b"1815" in r.data


def test_a_series_page_never_reveals_what_a_sibling_owns(client, users):
    """Alice has #1 and #3. Bob has #2. Alice must see a gap at #2 — computed from HER
    positions — and must NOT see Bob's book, which would tell her what he is reading."""
    add_calibre_book(1, "Book One", "A. Writer", tags=["owner:alice"])
    add_calibre_book(2, "Book Two", "A. Writer", tags=["owner:bob"])
    add_calibre_book(3, "Book Three", "A. Writer", tags=["owner:alice"])
    _enrich("alice", 1, "Book One", "A. Writer", series="The Saga", pos="1")
    _enrich("bob", 2, "Book Two", "A. Writer", series="The Saga", pos="2")
    _enrich("alice", 3, "Book Three", "A. Writer", series="The Saga", pos="3")
    sid = db.meta_for_calibre(1)["series"]["id"]
    login(client, "alice", users["alice"])
    r = client.get(f"/series/{sid}")
    assert r.status_code == 200
    assert b"Book One" in r.data and b"Book Three" in r.data
    assert b"Book Two" not in r.data, "a sibling's book must never appear on your series page"
    assert b"missing #2" in r.data
    assert b"#4" in r.data                               # next after hers


def test_a_series_page_is_unreachable_without_owning_a_book_in_it(client, users):
    add_calibre_book(2, "Book Two", "A. Writer", tags=["owner:bob"])
    _enrich("bob", 2, "Book Two", "A. Writer", series="The Saga", pos="2")
    sid = db.meta_for_calibre(2)["series"]["id"]
    login(client, "alice", users["alice"])
    assert client.get(f"/series/{sid}").status_code == 404


def test_an_author_page_lists_only_your_books(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    add_calibre_book(2, "Persuasion", "Jane Austen", tags=["owner:bob"])
    _enrich("alice", 1, "Emma", "Jane Austen")
    _enrich("bob", 2, "Persuasion", "Jane Austen")
    aid = db.meta_for_calibre(1)["authors"][0]["id"]
    login(client, "alice", users["alice"])
    r = client.get(f"/author/{aid}")
    assert r.status_code == 200 and b"Emma" in r.data
    assert b"Persuasion" not in r.data


def test_an_author_page_is_unreachable_without_owning_one_of_their_books(client, users):
    add_calibre_book(2, "Persuasion", "Jane Austen", tags=["owner:bob"])
    _enrich("bob", 2, "Persuasion", "Jane Austen")
    aid = db.meta_for_calibre(2)["authors"][0]["id"]
    login(client, "alice", users["alice"])
    assert client.get(f"/author/{aid}").status_code == 404


def test_an_admin_sees_the_whole_series(client, users):
    add_calibre_book(1, "Book One", "A. Writer", tags=["owner:alice"])
    add_calibre_book(2, "Book Two", "A. Writer", tags=["owner:bob"])
    _enrich("alice", 1, "Book One", "A. Writer", series="The Saga", pos="1")
    _enrich("bob", 2, "Book Two", "A. Writer", series="The Saga", pos="2")
    sid = db.meta_for_calibre(1)["series"]["id"]
    login(client, "admin", users["admin"])
    r = client.get(f"/series/{sid}")
    assert b"Book One" in r.data and b"Book Two" in r.data


def test_my_books_links_each_title_to_its_page(client, users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    login(client, "alice", users["alice"])
    assert b'href="/book/1"' in client.get("/library").data
