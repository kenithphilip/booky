"""v5.9.1: send to an e-reader, Kobo read marks, audiobook progress to Hardcover, Shelfmark API key."""
import sqlite3
import pytest
import config, db, cwa, hardcover, hcaudio, sendcode, shelfmark_api
import abs as absapi
from conftest import add_calibre_book, login, post


# ---- send to an e-reader -----------------------------------------------------------------------------
KOBO_UA = "Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/538.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/538.1 (Kobo Touch 0377/4.38.23684)"
KINDLE_UA = "Mozilla/5.0 (X11; U; Linux armv7l like Android; en-us) AppleWebKit/531.2+ (KHTML, like Gecko) Version/5.0 Safari/533.2+ Kindle/3.0+"

def test_an_e_reader_page_shows_a_code_and_offers_the_book_once_sent(client):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    add_calibre_book(2, "Eric", "Terry Pratchett", tags=["owner:alice"])
    reader = client.application.test_client()
    page = reader.get("/send", headers={"User-Agent": KOBO_UA})
    assert page.status_code == 200 and b'http-equiv="refresh"' in page.data
    secret = page.headers.get("Set-Cookie", "").split("send_secret=", 1)[1].split(";", 1)[0]
    code = db.send_code_by_secret(secret)["code"]
    assert code.encode() in page.data and db.send_code_get(code)["device"] == "kobo"
    assert reader.get("/send/file").status_code == 404, "nothing attached yet"
    login(client, "bob", "bobpass1")
    post(client, "/book/2/send", code=code)
    assert db.send_code_get(code)["book_id"] is None, "only the reader's own books"
    r = post(client, "/book/1/send", code=code.lower())
    assert b"Sent as KEPUB" in r.data or b"Sent as EPUB" in r.data, r.data[-600:]
    page = reader.get("/send")
    assert b"Download it" in page.data and b"http-equiv" not in page.data
    f = reader.get("/send/file")
    assert f.status_code == 200 and '.epub"' in f.headers["Content-Disposition"]
    stranger = client.application.test_client()
    assert stranger.get("/send/file").status_code == 404, "another browser never gets it"
    assert b"already has a book" in post(client, "/book/1/send", code=code).data

def test_a_kindle_gets_a_format_its_browser_opens_or_is_told_to_convert(client):
    add_calibre_book(1, "Mort", "Terry Pratchett", tags=["owner:bob"])
    code, _secret = sendcode.new_code(KINDLE_UA)
    with pytest.raises(sendcode.SendError, match="Convert to AZW3"):
        sendcode.attach("bob", False, code, 1)
    assert sendcode.choose_format("kindle", ["epub", "azw3"]) == "azw3"
    assert sendcode.choose_format("kobo", ["epub"]) in ("kepub", "epub")
    assert sendcode.choose_format("other", ["pdf"]) == "pdf"

def test_codes_expire_and_are_easy_to_read_on_e_ink(users):
    code, secret = sendcode.new_code(KOBO_UA, now=1000.0)
    assert len(code) == 4 and not set(code) & set("0O1IL")
    with pytest.raises(sendcode.SendError, match="15 minutes"):
        sendcode.attach("bob", False, code, 1, now=1000.0 + 16 * 60)
    assert sendcode.page_state(secret, now=1000.0 + 16 * 60) is None

def test_send_needs_no_login_but_attaching_does(client):
    assert client.get("/send").status_code == 200
    r = client.post("/book/1/send", data={"code": "ABCD"})
    assert r.status_code in (302, 400)


# ---- Kobo read marks (see also test_v59) --------------------------------------------------------------
def test_no_kobo_tables_yet_is_fine(users):
    bob = cwa.get_user("bob")["id"]
    c = sqlite3.connect(config.CWA_DB)
    c.execute("CREATE TABLE IF NOT EXISTS book_read_link(id INTEGER PRIMARY KEY, book_id INTEGER, user_id INTEGER, "
              "read_status INTEGER NOT NULL DEFAULT 0, last_modified DATETIME, last_time_started_reading DATETIME, "
              "times_started_reading INTEGER DEFAULT 0)")
    c.commit(); c.close()
    assert cwa.set_read_status("bob", 5, "reading") == "reading"
    assert cwa.reading_state("bob")[5]["status"] == "reading" and bob


# ---- audiobooks to Hardcover --------------------------------------------------------------------------
class FakeHC:
    """hardcover._q as Hardcover answers it, recording every call."""
    def __init__(self, user_books=None):
        self.calls, self.user_books = [], user_books or []
    def __call__(self, query, variables, token=None):
        self.calls.append((query.split("(")[0].split("{")[0].strip(), variables, token))
        if "asin" in query:
            return {"editions": [{"id": 31878554, "book_id": 427578, "reading_format_id": 2}]}
        if "me {" in query:
            return {"me": [{"id": 1, "user_books": self.user_books}]}
        if "insert_user_book(" in query:
            return {"insert_user_book": {"id": 900, "error": None}}
        return {"x": {"id": 1, "error": None}}

@pytest.fixture
def hc(users, monkeypatch):
    monkeypatch.setattr(absapi, "configured", lambda: True)
    monkeypatch.setattr(absapi, "list_users", lambda token=None: [{"username": "bob", "id": "u-bob"}])
    monkeypatch.setattr(absapi, "item_meta", lambda item, token=None: {"title": "Project Hail Mary", "author": "Andy Weir",
                                                                        "asin": "B08G9PRS1K", "isbn": "", "duration": 58253})
    monkeypatch.setattr(cwa, "hardcover_tokens", lambda: {"bob": "hc_pat_bob"})
    prog = {"p": [{"libraryItemId": "li1", "currentTime": 3600, "duration": 58253, "isFinished": False,
                   "startedAt": 1759000000000, "lastUpdate": 1759100000000}]}
    monkeypatch.setattr(absapi, "progress", lambda uid, token=None: prog["p"])
    fake = FakeHC()
    monkeypatch.setattr(hardcover, "_q", fake)
    return fake, prog

def test_listening_progress_reaches_the_readers_own_hardcover(hc):
    fake, prog = hc
    assert hcaudio.sync_once() == 1
    names = [c[0] for c in fake.calls]
    assert names[0].startswith("query A") and any("mutation C" in n for n in names) and any("mutation N" in n for n in names)
    create = next(c for c in fake.calls if c[0].startswith("mutation C"))
    assert create[1]["o"] == {"book_id": 427578, "edition_id": 31878554, "status_id": 2} and create[2] == "hc_pat_bob"
    read = next(c for c in fake.calls if c[0].startswith("mutation N"))
    assert read[1]["o"]["progress_seconds"] == 3600 and read[1]["o"]["edition_id"] == 31878554
    assert "1 audiobook updated" in db.hc_audio_state("bob")["detail"]
    fake.calls.clear()
    assert hcaudio.sync_once() == 0 and fake.calls == [], "nothing moved: nothing sent"
    prog["p"][0].update(currentTime=58253, isFinished=True, finishedAt=1759200000000)
    fake.user_books = [{"id": 900, "status_id": 2, "user_book_reads": [{"id": 77, "started_at": "2025-09-27", "finished_at": None}]}]
    assert hcaudio.sync_once() == 1
    upd = next(c for c in fake.calls if c[0].startswith("mutation R"))
    assert upd[1]["id"] == 77 and upd[1]["o"]["finished_at"] and upd[1]["o"]["started_at"] == "2025-09-27"
    assert any(c[0].startswith("mutation S") and c[1]["o"] == {"status_id": 3} for c in fake.calls)

def test_a_book_already_read_on_hardcover_is_not_reopened(hc):
    fake, _ = hc
    fake.user_books = [{"id": 900, "status_id": 3, "user_book_reads": []}]
    hcaudio.sync_once()
    assert not any(c[0].startswith("mutation") for c in fake.calls)

def test_a_refused_token_is_said_on_devices(hc, monkeypatch):
    def refuse(q, v, token=None):
        raise hardcover.TokenRefused("Hardcover refused your token (HTTP 403)")
    monkeypatch.setattr(hardcover, "_q", refuse)
    hcaudio.sync_once()
    assert "refused your token" in db.hc_audio_state("bob")["detail"]


# ---- Shelfmark API key ----------------------------------------------------------------------------------
class _R:
    def __init__(self, status):
        self.status_code = status

def test_the_api_key_is_used_first_and_the_login_is_the_fallback(monkeypatch):
    monkeypatch.setattr(config, "SHELFMARK_API_KEY", "k" * 64)
    monkeypatch.setattr(config, "SHELFMARK_SVC_USER", "")
    monkeypatch.setattr(config, "SHELFMARK_SVC_PASS", "")
    sent = []
    monkeypatch.setattr(shelfmark_api.requests, "request", lambda m, url, **kw: sent.append(kw["headers"]) or _R(200))
    assert shelfmark_api.configured()
    assert shelfmark_api._call("GET", "/api/status").status_code == 200 and sent[-1] == {"X-Api-Key": "k" * 64}
    monkeypatch.setattr(shelfmark_api.requests, "request", lambda m, url, **kw: _R(403))
    with pytest.raises(shelfmark_api.ShelfmarkError, match="API key"):
        shelfmark_api._call("GET", "/api/admin/users")
