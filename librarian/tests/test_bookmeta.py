"""Metadata-first search, the book page and fetching a copy of THAT book.

The network is replaced by the real responses captured from Open Library, Gutenberg and
LibriVox on 2026-09-26 (tests/fixtures/bookmeta), so the shapes are the true ones — including
the Finnish translation Open Library links to 'Pride and Prejudice', which is the case that
proves copies are verified and not merely trusted.
"""
import json, os, re
import pytest
import config, db, bookmeta, fetchers, matching, worker, wanted
from conftest import add_calibre_book, login, post

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bookmeta")


class _Resp:
    def __init__(self, status, body):
        self.status_code, self.content = status, body if isinstance(body, bytes) else body.encode()

    def json(self):
        return json.loads(self.content)


def _file(name):
    p = os.path.join(FIX, name)
    return _Resp(200, open(p, "rb").read()) if os.path.exists(p) else _Resp(404, b"{}")


def fake_get(url, params=None, headers=None, timeout=None, **kw):
    params = params or {}
    if url.endswith("/search.json"):
        q = params.get("q", "")
        if q.startswith("key:/works/"):
            return _file(f"ol_search_key_{q.rsplit('/', 1)[-1]}.json")
        if params.get("author_key"):
            return _file(f"ol_author_works_{params['author_key']}.json")
        return _file("ol_search_hobbit.json" if "hobbit" in q.lower() else "ol_search_pride.json")
    m = re.search(r"/works/(OL\d+W)\.json$", url)
    if m:
        return _file(f"ol_work_{m.group(1)}.json")
    m = re.search(r"/authors/(OL\d+A)\.json$", url)
    if m:
        return _file(f"ol_author_{m.group(1)}.json")
    m = re.search(r"/cache/epub/(\d+)/pg\d+\.rdf$", url)
    if m:
        return _file(f"pg{m.group(1)}.rdf")
    if "librivox.org/api" in url:
        return _file(f"lv{params.get('id')}.json")
    return _Resp(404, b"")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(bookmeta.requests, "get", fake_get)
    monkeypatch.setattr(fetchers, "_ia_epub_cached", lambda ident, budget: None)   # lending copies
    monkeypatch.setitem(config.SOURCES, "standard_ebooks", True)
    for c in (bookmeta._SEARCH, bookmeta._RECORD, bookmeta._COPIES):
        c.clear()


# ---- the engine --------------------------------------------------------------------------------
def test_search_returns_works_with_their_catalogue_links():
    works = bookmeta.search("pride and prejudice")
    w = works[0]
    assert w["key"] == "OL66554W" and w["author"] == "Jane Austen"
    assert w["links"]["gutenberg"][:3] == ["1342", "42671", "45186"]
    assert w["links"]["standard_ebooks"] == ["jane-austen/pride-and-prejudice"]
    assert w["has_ebook"] and w["has_audio"]
    assert all(x.isdigit() for x in w["links"]["librivox"]), "LibriVox slugs are not API ids"


def test_a_work_joins_the_search_record_and_the_description():
    w = bookmeta.work("OL66554W")
    assert w["title"] == "Pride and Prejudice" and len(w["description"]) > 100
    assert bookmeta.work("../etc/passwd") is None and bookmeta.work("OL1A") is None


def test_copies_are_read_and_verified_not_trusted():
    w = bookmeta.work("OL66554W")
    cs = bookmeta.copies(w, language="en")
    by_id = {c["identifier"]: c for c in cs}
    assert by_id["pg:1342"]["match"]["verdict"] == "auto"
    assert by_id["pg:45186"]["match"]["verdict"] == "reject", "Open Library links the Finnish translation"
    assert "another language" in by_id["pg:45186"]["match"]["reasons"][0]
    assert by_id["se:jane-austen/pride-and-prejudice"]["download_url"].endswith(
        "/downloads/jane-austen_pride-and-prejudice.epub?source=download"), "the bare address serves an HTML page"
    audio = [c for c in cs if c["kind"] == "audio"]
    assert audio and all(c["download_url"].startswith("https://") for c in audio)
    assert audio[0]["duration_seconds"] and "LibriVox recording" in audio[0]["detail"]
    assert all(fetchers.url_allowed(c["source"], c["download_url"]) for c in cs)
    assert [c["match"]["verdict"] for c in cs] == sorted((c["match"]["verdict"] for c in cs),
                                                         key={"auto": 0, "review": 1, "reject": 2}.get)


def test_a_finnish_reader_gets_the_finnish_copy():
    cs = bookmeta.copies(bookmeta.work("OL66554W"), language="fi")
    verdicts = {c["identifier"]: c["match"]["verdict"] for c in cs}
    assert verdicts["pg:45186"] == "review" and verdicts["pg:1342"] == "reject", \
        "her translation is offered for her to confirm; the English original is not hers"


def test_a_disabled_catalogue_is_never_asked(monkeypatch):
    monkeypatch.setitem(config.SOURCES, "librivox", False)
    asked = []
    real = bookmeta.requests.get
    monkeypatch.setattr(bookmeta.requests, "get", lambda u, **k: asked.append(u) or real(u, **k))
    cs = bookmeta.copies(bookmeta.work("OL66554W"), language="en")
    assert not any(c["kind"] == "audio" for c in cs) and not any("librivox" in u for u in asked)


def test_open_library_down_is_said_and_the_breaker_opens(monkeypatch):
    monkeypatch.setattr(bookmeta.requests, "get", lambda *a, **k: _Resp(503, b""))
    for _ in range(3):
        with pytest.raises(bookmeta.Unavailable):
            bookmeta.search(f"q{_}")
    calls = []
    monkeypatch.setattr(bookmeta.requests, "get", lambda *a, **k: calls.append(1) or _Resp(200, b"{}"))
    with pytest.raises(bookmeta.Unavailable, match="stood down"):
        bookmeta.search("another")
    assert not calls, "a stood-down service is not asked again until its cooldown passes"


def test_gutenberg_rdf_gives_language_type_and_size():
    rec = bookmeta.parse_pg_rdf(open(os.path.join(FIX, "pg1342.rdf"), "rb").read(), "1342")
    assert rec["language"] == "en" and rec["type"] == "Text" and rec["size"] > 1_000_000
    assert "Austen" in rec["author"]


# ---- the matcher -------------------------------------------------------------------------------
@pytest.mark.parametrize("cand,verdict", [
    ({"title": "Pride and Prejudice", "author": "Austen, Jane, 1775-1817", "language": "en", "linked": True}, "auto"),
    ({"title": "Pride and Prejudice: A Novel", "author": "J. Austen"}, "auto"),
    ({"title": "Pride and Prejudice", "author": ""}, "review"),
    ({"title": "Pride and Prejudice (Abridged)", "author": "Jane Austen"}, "review"),
    ({"title": "Pride and Prejudice and Zombies", "author": "Seth Grahame-Smith; Jane Austen"}, "reject"),
    ({"title": "The Complete Works of Jane Austen", "author": "Jane Austen"}, "reject"),
    ({"title": "Pride and Prejudice", "author": "Wayne Josephson"}, "reject"),
    ({"title": "Pride and Prejudice", "author": "Jane Austen", "source": "librivox"}, "reject"),
    ({"title": "Ylpeys ja ennakkoluulo", "author": "Austen, Jane", "language": "fi", "linked": True}, "reject"),
])
def test_matching_verdicts(cand, verdict):
    want = {"title": "Pride and Prejudice", "author": "Jane Austen", "kind": "ebook", "language": "en"}
    assert matching.distance(want, cand)[1] == verdict


def test_a_shared_isbn_counts_and_a_different_one_counts_against():
    want = {"title": "Pride and Prejudice", "author": "Jane Austen", "isbns": [{"kind": "isbn", "value": "9780141439518"}]}
    same = matching.distance(want, {"title": "Pride and Prejudice", "author": "", "src_ids": [("isbn", "978-0-14-143951-8")]})
    other = matching.distance(want, {"title": "Pride and Prejudice", "author": "", "src_ids": [("isbn", "9780000000002")]})
    assert "same ISBN" in same[2] and same[0] < other[0]


# ---- the pages -------------------------------------------------------------------------------
def test_search_shows_books_not_files(client, users):
    login(client, "alice", users["alice"])
    html = client.get("/?q=pride+and+prejudice").get_data(as_text=True)
    assert 'href="/work/OL66554W"' in html and "free ebook" in html and "free audiobook" in html
    assert 'href="/writer/OL21594A"' in html
    assert "mode=catalogs" in html, "the direct catalogue search stays one click away"


def test_a_book_she_has_is_marked(client, users):
    add_calibre_book(1, "Pride and Prejudice", "Jane Austen", tags=["owner:alice"])
    login(client, "alice", users["alice"])
    assert "in library" in client.get("/?q=pride+and+prejudice").get_data(as_text=True)


def test_open_library_down_falls_back_to_the_catalogues(client, users, monkeypatch):
    monkeypatch.setattr(bookmeta, "search", lambda *a, **k: (_ for _ in ()).throw(bookmeta.Unavailable("down")))
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [])
    login(client, "alice", users["alice"])
    r = client.get("/?q=pride", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "unavailable right now" in html and "No matches in the catalogs" in html


def test_the_book_page_offers_verified_copies_and_explains_refusals(client, users):
    login(client, "alice", users["alice"])
    html = client.get("/work/OL66554W").get_data(as_text=True)
    assert "Copies we can fetch for you" in html and "Project Gutenberg #1342" in html
    assert "good match" in html and 'name="token"' in html
    assert "did not match" in html and "another language" in html
    assert 'name="download_url"' not in html, "no address is ever put in a form"
    assert "Keep looking for it" in html and "shelf.example.test" in html
    assert client.get("/work/OL999W").status_code == 404


def test_requesting_a_copy_carries_its_evidence_and_the_book(client, users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    login(client, "alice", users["alice"])
    html = client.get("/work/OL66554W").get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', html).group(1)
    post(client, "/get", token=token)
    (req,) = db.rows_by_status(("queued",))
    assert req["owner"] == "alice" and req["work_key"] == "OL66554W" and req["language"] == "en"
    assert req["match_confidence"] >= 0.8 and "links it to this book" in req["match_reasons"]
    assert json.loads(req["src_ids"])[-1] == ["openlibrary_work", "OL66554W"]


def test_a_token_is_only_good_for_the_reader_it_was_offered_to(client, users):
    login(client, "alice", users["alice"])
    html = client.get("/work/OL66554W").get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', html).group(1)
    post(client, "/logout")
    login(client, "bob", users["bob"])
    r = post(client, "/get", token=token)
    assert b"expired" in r.data and not db.rows_by_status(("queued", "pending"))
    r = post(client, "/get", token="made-up")
    assert b"expired" in r.data


def test_the_internet_archive_evidence_now_reaches_the_download(client, users, monkeypatch, tmp_path):
    """The bug: the Request form dropped expect_size/sha1, so verify_download never ran."""
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    ia = {"source": "internet_archive", "kind": "ebook", "title": "Emma", "author": "Jane Austen",
          "download_url": "https://archive.org/download/emma00aust/emma.epub", "identifier": "ia:emma00aust",
          "expect_size": 5, "expect_sha1": "0" * 40, "match": {"distance": 0.0, "verdict": "auto", "reasons": ["x"]}}
    login(client, "alice", users["alice"])
    token = db.candidate_put("alice", ia)
    post(client, "/get", token=token)
    (req,) = db.rows_by_status(("queued",))
    assert req["expect_size"] == 5 and req["expect_sha1"] == "0" * 40
    f = tmp_path / "x.epub"; f.write_bytes(b"hello")
    assert worker.verify_download(str(f), req) == ["sha1 does not match what the source published"]


def test_a_refused_copy_cannot_be_requested(client, users):
    login(client, "alice", users["alice"])
    token = db.candidate_put("alice", {"source": "gutenberg", "title": "x", "download_url": "https://www.gutenberg.org/ebooks/1.epub3.images",
                                       "match": {"distance": 1.0, "verdict": "reject", "reasons": ["no"]}})
    assert post(client, "/get", token=token).status_code == 400


def test_the_catalogue_search_uses_tokens_too(client, users, monkeypatch):
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [{
        "source": "gutenberg", "kind": "ebook", "title": "Emma", "author": "Jane Austen", "format": "epub",
        "download_url": "https://www.gutenberg.org/ebooks/158.epub3.images", "identifier": "pg:158"}])
    login(client, "alice", users["alice"])
    html = client.get("/?q=emma&mode=catalogs").get_data(as_text=True)
    assert 'name="token"' in html and 'name="download_url"' not in html


def test_the_author_page(client, users):
    login(client, "alice", users["alice"])
    html = client.get("/writer/OL21594A").get_data(as_text=True)
    assert "Jane Austen" in html and "1775" in html and 'href="/work/' in html
    assert "covers.openlibrary.org" in html      # the photo, through the local cover proxy
    assert client.get("/writer/nonsense").status_code == 404


def test_the_reading_language_is_a_preference(client, users):
    login(client, "alice", users["alice"])
    post(client, "/devices", action="prefs", preferred_format="epub", language="fi")
    assert db.get_prefs("alice")["language"] == "fi"
    html = client.get("/work/OL66554W").get_data(as_text=True)
    offered = html.split("did not match")[0]
    assert "Ylpeys ja ennakkoluulo" in offered and "check this one" in offered, "her language is the one offered"


def test_keep_looking_from_a_book_page_resolves_through_its_links(users, monkeypatch):
    monkeypatch.setattr(config, "APPROVALS_REQUIRED", False)
    monkeypatch.setattr(config, "METADATA_ENABLED", False)
    wid, _ = db.wanted_add("alice", "ebook", "Pride and Prejudice", "Jane Austen", first_check=0, limit=0,
                           same=wanted.same_want, work_key="OL66554W")
    monkeypatch.setattr(fetchers, "search", lambda q, **k: [])      # the keyword ladder finds nothing
    assert worker.check_wanted(db.wanted_get(wid)) == "requested"
    req = db.get(db.wanted_get(wid)["rid"])
    assert req["work_key"] == "OL66554W" and req["source"] in ("standard_ebooks", "gutenberg")



def test_series_come_from_the_goodreads_mirror_in_the_background(client, users, monkeypatch):
    data = {"Works": [{"ForeignId": 1, "Title": "The Fellowship of the Ring"}, {"ForeignId": 2, "Title": "The Two Towers"},
                      {"ForeignId": 3, "Title": "The Return of the King"}],
            "Series": [{"ForeignId": 66, "Title": "The Lord of the Rings", "LinkItems": [
                {"ForeignWorkId": 2, "SeriesPosition": 2}, {"ForeignWorkId": 1, "SeriesPosition": 1},
                {"ForeignWorkId": 3, "SeriesPosition": 3}]},
                       {"ForeignId": 67, "Title": "Only one known", "LinkItems": [{"ForeignWorkId": 1, "SeriesPosition": 1}]}]}
    got = bookmeta.parse_bookinfo_series(data)
    assert [s["title"] for s in got] == ["The Lord of the Rings"], "a one-book 'series' is noise"
    assert [i["title"] for i in got[0]["books"]] == ["The Fellowship of the Ring", "The Two Towers", "The Return of the King"]
    started = []
    monkeypatch.setattr(bookmeta._PREFETCH, "submit", lambda fn, gid: started.append(gid))
    bookmeta._SERIES.clear(); bookmeta._SERIES_PENDING.clear()
    login(client, "alice", users["alice"])
    html = client.get("/writer/OL21594A").get_data(as_text=True)
    assert "being looked up in the background" in html and started == ["1265"], "never on the page's critical path"
    bookmeta._SERIES.put("1265", got)
    html = client.get("/writer/OL21594A").get_data(as_text=True)
    assert "The Lord of the Rings" in html and html.index("Fellowship") < html.index("Two Towers")
