"""L16: Shelfmark's pending requests on the portal's Pending card — one approval queue."""
import pytest
import config, shelfmark_api
from conftest import login, post


class R:
    def __init__(self, status, data=None, cookie=None):
        self.status_code, self._d = status, data
        self.headers = {"Set-Cookie": f"shelfmark_session={cookie}; Secure; HttpOnly"} if cookie else {}
        self.cookies = []

    def json(self):
        return self._d


@pytest.fixture
def shelf(monkeypatch):
    monkeypatch.setattr(config, "SHELFMARK_SVC_USER", "svc-portal")
    monkeypatch.setattr(config, "SHELFMARK_SVC_PASS", "pw")
    shelfmark_api._state.update(cookie=None, pending=None, at=0)
    calls = []
    rows = [{"id": 7, "user_id": 3, "username": "alice", "content_type": "ebook",
             "book_data": {"title": "Dune", "author": "Frank Herbert"},
             "release_data": {"source": "direct_download", "format": "epub"}}]
    def post_(url, json=None, timeout=None, **k):
        calls.append(("POST", url, json, None))
        return R(200, {"ok": True}, cookie="abc123")
    def req(method, url, headers=None, **k):
        calls.append((method, url, k.get("json"), headers.get("Cookie")))
        if url.endswith("/api/admin/requests"):
            return R(200, rows)
        return R(200, {"id": 7, "status": "fulfilled"})
    monkeypatch.setattr(shelfmark_api.requests, "post", post_)
    monkeypatch.setattr(shelfmark_api.requests, "request", req)
    return calls


def test_the_secure_session_cookie_is_carried_by_hand(shelf):
    rows = shelfmark_api.pending()
    assert rows[0]["title"] == "Dune" and rows[0]["requester"] == "alice" and rows[0]["level"] == "release"
    login_call, list_call = shelf[0], shelf[1]
    assert login_call[2] == {"username": "svc-portal", "password": "pw"}
    assert list_call[3] == "shelfmark_session=abc123", "a Secure cookie is never sent to http:// by a cookie jar"


def test_the_admin_sees_and_approves_shelfmark_requests_on_the_portal(client, users, shelf):
    login(client, "admin", users["admin"])
    html = client.get("/status").get_data(as_text=True)
    assert "Pending approval in Shelfmark" in html and "Dune" in html and "by alice" in html
    r = post(client, "/shelfmark/7/approve")
    assert b"downloads now, to the reader" in r.data
    assert any(c[1].endswith("/api/admin/requests/7/fulfil") for c in shelf)


def test_deny_carries_the_reason(client, users, shelf):
    login(client, "admin", users["admin"])
    post(client, "/shelfmark/7/deny", reason="not in our library's scope")
    deny = [c for c in shelf if c[1].endswith("/reject")][0]
    assert deny[2] == {"admin_note": "not in our library's scope"}


def test_readers_cannot_decide_or_see_the_queue(client, users, shelf):
    login(client, "alice", users["alice"])
    assert "Pending approval in Shelfmark" not in client.get("/status").get_data(as_text=True)
    post(client, "/shelfmark/7/approve")
    assert not any(c[1].endswith("/fulfil") for c in shelf)


def test_an_expired_session_logs_in_again(monkeypatch, shelf):
    seq = [R(401), R(200, [])]
    monkeypatch.setattr(shelfmark_api.requests, "request", lambda *a, **k: seq.pop(0))
    shelfmark_api._state["cookie"] = "stale"
    assert shelfmark_api.pending(force=True) == []
    assert shelf and shelf[-1][1].endswith("/api/auth/login")


def test_shelfmark_down_is_said_not_hidden(client, users, monkeypatch, shelf):
    def boom(*a, **k):
        raise shelfmark_api.requests.ConnectionError("down")
    monkeypatch.setattr(shelfmark_api.requests, "request", boom)
    login(client, "admin", users["admin"])
    assert "Could not read Shelfmark" in client.get("/status").get_data(as_text=True)


def test_not_configured_is_silent(monkeypatch):
    monkeypatch.setattr(config, "SHELFMARK_SVC_PASS", "")
    assert shelfmark_api.pending() == []
