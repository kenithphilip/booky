"""v6.4.0: a reader resets a forgotten password themselves, and the new password is set in every
place at once (Audiobookshelf first: when it cannot take it, nothing changes anywhere)."""
import re, time
import pytest
import config, db, auth, passwords, pwreset, kindle
import abs as absapi
from conftest import login, post, csrf_of


def apost(client, path, page=None, **data):
    """POST as someone who is NOT signed in (the token comes from an open page)."""
    data.setdefault("csrf", csrf_of(client, page or "/forgot"))
    return client.post(path, data=data, follow_redirects=True)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "Library <lib@example.test>")
    monkeypatch.setattr(config, "PORTAL_URL", "https://request.example.test")
    monkeypatch.setattr(kindle, "_deliver", lambda msg: sent.append(msg))
    return sent


@pytest.fixture
def absrv(monkeypatch):
    """A stand-in Audiobookshelf: {'up': bool, 'refuse': bool, 'set': [(user, pw)]}."""
    st = {"up": True, "refuse": False, "set": []}
    monkeypatch.setattr(absapi, "configured", lambda: True)
    def find(name, token=None):
        if not st["up"]:
            raise absapi.AbsError("connection refused")
        return {"id": "u-" + name}
    def setpw(name, pw, token=None):
        if st["refuse"]:
            raise absapi.AbsError("500")
        st["set"].append((name, pw))
        return True
    monkeypatch.setattr(absapi, "find_user", find)
    monkeypatch.setattr(absapi, "set_password", setpw)
    return st


def _link(msg):
    m = re.search(r"https://request\.example\.test/reset/([A-Za-z0-9_-]+)", msg.get_content())
    return m.group(1) if m else None


# ---- one way a password changes ----------------------------------------------------------------------
def test_a_new_password_is_set_everywhere_at_once(users, absrv, monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True)
    db.set_pw_temp("bob", True)
    res = passwords.set_everywhere("bob", "brand-new-pass-1")
    assert auth.verify("bob", "brand-new-pass-1") and not auth.verify("bob", "bobpass1"), "the library (portal, site, apps, Shelfmark)"
    assert absrv["set"] == [("bob", "brand-new-pass-1")], "Audiobookshelf"
    assert res["gate"] is True, "the sign-in page, queued (the host's gate-sync writes it within seconds)"
    assert not db.pw_temp("bob")

def test_when_audiobookshelf_cannot_take_it_nothing_changes_anywhere(users, absrv):
    absrv["up"] = False
    with pytest.raises(passwords.PasswordError, match="nothing was changed"):
        passwords.set_everywhere("bob", "brand-new-pass-1")
    absrv["up"], absrv["refuse"] = True, True
    with pytest.raises(passwords.PasswordError, match="nothing was changed"):
        passwords.set_everywhere("bob", "brand-new-pass-1")
    assert auth.verify("bob", "bobpass1") and not auth.verify("bob", "brand-new-pass-1"), "never two passwords"

def test_devices_change_uses_the_same_path(client, users, absrv):
    absrv["refuse"] = True
    login(client, "bob", "bobpass1")
    html = post(client, "/devices", action="password", current="bobpass1", new="brand-new-pass-1",
                repeat="brand-new-pass-1").get_data(as_text=True)
    assert "nothing was changed" in html and auth.verify("bob", "bobpass1")
    absrv["refuse"] = False
    post(client, "/devices", action="password", current="bobpass1", new="short", repeat="short")
    assert auth.verify("bob", "bobpass1"), "too short: refused by the server too"


# ---- asking for a link -------------------------------------------------------------------------------------
def test_forgot_mails_a_one_time_link_to_a_reader_and_says_the_same_to_everyone(client, users, mail):
    page = client.get("/forgot").get_data(as_text=True)
    assert "Forgot your password?" in page and 'name="ident"' in page, "reachable without signing in"
    said = []
    for ident in ("bob", "BOB@example.test", "nobody", "admin"):
        said.append(apost(client, "/forgot", ident=ident).get_data(as_text=True).split("<h2>")[1][:400])
    assert len(set(said)) == 1, "the same answer whatever was typed: it never tells whether an account exists"
    assert [m["To"] for m in mail] == ["bob@example.test", "bob@example.test"], "by name and by e-mail; never for an admin or nobody"
    t1, t2 = _link(mail[0]), _link(mail[1])
    assert t1 and t2 and pwreset.owner_of(t1) is None and pwreset.owner_of(t2) == "bob", "asking again cancels the last link"
    with db._conn() as c:
        assert not c.execute("SELECT 1 FROM password_reset WHERE token_hash=?", (t2,)).fetchone(), "only the hash is kept"

def test_requests_are_rate_limited(users, mail):
    for _ in range(6):
        pwreset.request("bob", "203.0.113.9")
    assert len(mail) == pwreset.PER_ACCOUNT_HOUR, "a few an hour per account"
    for i in range(12):
        pwreset.request(f"x{i}", "198.51.100.7")
    pwreset.request("alice", "198.51.100.7")
    assert not any(m["To"] == "alice@example.test" for m in mail), "and per address"

def test_without_mail_the_page_says_to_ask_the_admin(client, users, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    page = client.get("/forgot").get_data(as_text=True)
    assert "ask your library admin" in page and 'name="ident"' not in page

def test_the_sign_in_page_links_to_it(client, users):
    assert "/forgot" in client.get("/login").get_data(as_text=True)


# ---- using the link ---------------------------------------------------------------------------------------------
def test_the_link_sets_the_password_everywhere_once(client, users, mail, absrv, monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True)
    alerts = []
    import notify
    monkeypatch.setattr(notify, "alert", lambda *a, **k: alerts.append(a[0]))
    for _ in range(5):
        db.record_login_failure("bob", "203.0.113.9")
    pwreset.request("bob", "203.0.113.9")
    token = _link(mail[-1])
    page = client.get(f"/reset/{token}").get_data(as_text=True)
    assert "Choose a new password" in page and "bob" in page
    page = apost(client, f"/reset/{token}", page=f"/reset/{token}", new="mismatch-pass-1", repeat="mismatch-pass-2").get_data(as_text=True)
    assert "do not match" in page and auth.verify("bob", "bobpass1"), "nothing changed"
    page = apost(client, f"/reset/{token}", page=f"/reset/{token}", new="reset-new-pass-1", repeat="reset-new-pass-1").get_data(as_text=True)
    assert "Password changed" in page
    assert auth.verify("bob", "reset-new-pass-1") and ("bob", "reset-new-pass-1") in absrv["set"]
    assert db.locked_for("bob", "203.0.113.9") == 0, "their lockout is cleared"
    assert mail[-1]["Subject"] == "Your library password was changed" and alerts == ["bob reset their password"]
    assert "no longer works" in client.get(f"/reset/{token}").get_data(as_text=True), "the link works once"

def test_an_expired_link_and_a_failing_audiobook_server(client, users, mail, absrv):
    pwreset.request("bob", "203.0.113.9")
    token = _link(mail[-1])
    assert pwreset.owner_of(token, now=time.time() + pwreset.TTL + 5) is None, "30 minutes"
    absrv["up"] = False
    page = apost(client, f"/reset/{token}", page=f"/reset/{token}", new="reset-new-pass-1", repeat="reset-new-pass-1").get_data(as_text=True)
    assert "nothing was changed" in page and auth.verify("bob", "bobpass1")
    assert pwreset.owner_of(token) == "bob", "the link still works for the next try"
    assert client.get("/reset/not-a-token").status_code == 404
