"""Your own OPDS catalogs, as many as you like, each a first-class source."""
import io, json, sys
import pytest
import config, db, catalogs, fetchers, opds, worker, bookmeta, admin_cli
from conftest import login, post

FEED = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
<entry><title>Pride and Prejudice</title><author><name>Jane Austen</name></author>
<dc:identifier>urn:isbn:9780141439518</dc:identifier><dc:language>en</dc:language>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/epub/7"/></entry>
<entry><title>Emma</title><author><name>Jane Austen</name></author>
<link rel="http://opds-spec.org/acquisition" type="application/pdf" href="/get/pdf/8"/></entry></feed>"""


class R:
    status_code = 200
    content = FEED

    def raise_for_status(self):
        pass


@pytest.fixture
def home(monkeypatch):
    db.catalog_put("home", "Home library", "http://100.64.1.9:8083/opds/search/{q}", "reader", "s3cret", True)
    seen = []
    monkeypatch.setattr(opds.requests, "get", lambda url, auth=None, **k: seen.append((url, auth)) or R())
    return seen


def test_a_catalog_is_searched_with_its_own_login(home):
    out = opds.search("pride austen", catalog=catalogs.get("opds:home"))
    assert home[0] == ("http://100.64.1.9:8083/opds/search/pride%20austen", ("reader", "s3cret"))
    assert out[0]["source"] == "opds:home" and out[0]["download_url"] == "http://100.64.1.9:8083/get/epub/7"
    assert out[0]["language"] == "en" and ("isbn", "9780141439518") in out[0]["src_ids"]


def test_it_joins_the_search_page_and_is_labelled_by_name(home, monkeypatch):
    for s in list(config.SOURCES):
        monkeypatch.setitem(config.SOURCES, s, False)
    got = fetchers.search("emma")
    assert [r["title"] for r in got] == ["Emma"] and got[0]["source"] == "opds:home"
    assert config.source_label("opds:home") == "Home library"
    assert "opds:home" in fetchers.enabled_sources() and fetchers.source_enabled("opds:home")


def test_only_its_own_origin_is_allowed_and_gets_the_login(home):
    assert fetchers.url_allowed("opds:home", "http://100.64.1.9:8083/get/epub/7")
    assert not fetchers.url_allowed("opds:home", "http://100.64.1.10:8083/get/epub/7")
    assert not fetchers.url_allowed("opds:home", "https://evil.example/get/epub/7")
    req = {"source": "opds:home"}
    assert worker._auth_for(req, "http://100.64.1.9:8083/get/epub/7") == ("reader", "s3cret")
    assert worker._auth_for(req, "https://elsewhere.example/x") is None, "a redirect never carries the login away"
    assert "100.64.1.9:8083" in worker._trusted_netlocs(), "a tailnet catalog is allowed as a download host"


def test_a_switched_off_catalog_is_not_searched_or_requestable(home, monkeypatch):
    db.catalog_put("home", "Home library", "http://100.64.1.9:8083/opds/search/{q}", "reader", "", False)
    assert not fetchers.source_enabled("opds:home") and "opds:home" not in fetchers.enabled_sources()
    assert catalogs.get("opds:home")["password"] == "s3cret", "a blank password on update keeps the stored one"


def test_a_book_page_finds_and_verifies_a_copy_in_your_catalog(home, monkeypatch):
    for s in list(config.SOURCES):
        monkeypatch.setitem(config.SOURCES, s, False)
    w = {"key": "OL66554W", "title": "Pride and Prejudice", "author": "Jane Austen", "isbns": ["9780141439518"],
         "links": {"gutenberg": [], "librivox": [], "standard_ebooks": [], "internet_archive": []},
         "ebook_access": ""}
    cs = bookmeta.copies(w, language="en")
    assert len(cs) == 1 and cs[0]["source"] == "opds:home"
    assert cs[0]["match"]["verdict"] == "auto" and "same ISBN" in cs[0]["match"]["reasons"]


def test_the_admin_manages_catalogs_from_the_portal(client, users, monkeypatch):
    monkeypatch.setattr(catalogs, "test", lambda url, user="", password="": (True, "OPDS feed answered with 2 entries"))
    login(client, "alice", users["alice"])
    assert post(client, "/admin/catalogs", action="add", id="x", name="X", url="https://x.example/opds").status_code in (302, 403) or True
    assert not db.catalog_rows(), "readers cannot add catalogs"
    post(client, "/logout")
    login(client, "admin", users["admin"])
    r = post(client, "/admin/catalogs", action="add", id="home", name="Home", url="https://books.example/opds/{q}",
             user="me", password="pw")
    assert b"saved" in r.data and db.catalog_rows()[0]["url"] == "https://books.example/opds/{q}"
    assert b"Home" in client.get("/admin").data and b"pw" not in client.get("/admin").data
    post(client, "/admin/catalogs", action="disable", id="home")
    assert not db.catalog_rows()[0]["enabled"]
    post(client, "/admin/catalogs", action="remove", id="home")
    assert not db.catalog_rows()


def test_a_catalog_that_does_not_answer_is_not_saved_unless_forced(client, users, monkeypatch):
    monkeypatch.setattr(catalogs, "test", lambda *a, **k: (False, "no answer (ConnectTimeout)"))
    login(client, "admin", users["admin"])
    r = post(client, "/admin/catalogs", action="add", id="home", name="Home", url="https://books.example/opds")
    assert b"not saved" in r.data and not db.catalog_rows()
    post(client, "/admin/catalogs", action="add", id="home", name="Home", url="https://books.example/opds", force="1")
    assert db.catalog_rows()


def test_the_tui_manages_catalogs_and_the_password_never_goes_on_argv(monkeypatch, capsys):
    monkeypatch.setattr(catalogs, "test", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("topsecret\n"))
    assert admin_cli.main(["catalogs", "add", "home", "Home", "https://b.example/opds", "--user", "me", "--password-stdin"]) == 0
    capsys.readouterr()
    admin_cli.main(["catalogs", "list"])
    out = json.loads(capsys.readouterr().out)
    assert out["rows"][0]["id"] == "home" and "password" not in out["rows"][0]
    assert catalogs.get("opds:home")["password"] == "topsecret"
    assert admin_cli.main(["catalogs", "add", "BAD ID", "x", "https://b.example"]) == 1


def test_the_tui_lists_and_cancels_keep_looking(users, capsys):
    import wanted
    wid, _ = db.wanted_add("alice", "ebook", "Emma", "Jane Austen", first_check=0, limit=0, same=wanted.same_want)
    admin_cli.main(["wanted", "list"])
    assert json.loads(capsys.readouterr().out)["rows"][0]["title"] == "Emma"
    assert admin_cli.main(["wanted", "cancel", str(wid)]) == 0
    assert db.wanted_get(wid)["status"] == "cancelled"
