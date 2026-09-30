"""v6.3.1: the welcome e-mail a new family member gets (never the password), and a nudge on every
page until they choose their own password."""
import json
import pytest
import config, db, cwa, welcome, admin_cli, kindle
from conftest import login, post


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_FROM", "Library <lib@example.test>")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setattr(config, "HOME_URL", "https://home.example.test")
    monkeypatch.setattr(config, "PORTAL_URL", "https://request.example.test")
    monkeypatch.setattr(config, "AUDIO_URL", "https://audio.example.test")
    monkeypatch.setattr(kindle, "_deliver", lambda msg: sent.append(msg))
    return sent


def test_the_welcome_email_says_where_to_start_and_never_the_password(users, mail, monkeypatch):
    monkeypatch.setattr(config, "FAMILY_SHARING", True)
    subject, text, html = welcome.compose("bob")
    assert subject == "Your family library account is ready"
    for want in ("Hi bob,", "Start here: https://home.example.test", "User name: bob",
                 "never sent by e-mail", "https://request.example.test/devices", "Your devices",
                 "Get it", "https://audio.example.test", "Keep my books private", "reply to this e-mail"):
        assert want in text, want
    assert "bobpass1" not in text and "bobpass1" not in html, "the password is never in the mail"
    assert "second step" not in text, "no 2FA line when the sign-in page does not ask readers for one"
    assert '<a href="https://home.example.test">' in html and "admin dashboard" not in text

def test_what_the_email_adds_for_2fa_a_kindle_and_an_admin(users, mail, monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True)
    cwa.set_kindle_mail("bob", "bob@kindle.com")
    text = welcome.compose("bob")[1]
    assert "bob@kindle.com" in text and "Approved Personal Document E-mail List" in text and "lib@example.test" in text
    assert "second step" not in text, "readers are asked for one only with AUTHELIA_READERS_2FA"
    monkeypatch.setattr(config, "AUTHELIA_READERS_2FA", True)
    assert "second step" in welcome.compose("bob")[1]
    admin = welcome.compose("admin")[1]
    assert "second step" in admin and "admin dashboard" in admin

def test_it_is_sent_to_the_members_own_address_with_replies_to_the_admin(users, mail):
    assert welcome.send("bob") == "bob@example.test"
    (msg,) = mail
    assert msg["To"] == "bob@example.test" and msg["Reply-To"] == "admin@example.test"
    assert msg.is_multipart() and [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]

def test_no_mail_set_up_says_so(users, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    with pytest.raises(welcome.WelcomeError, match="not set up"):
        welcome.send("bob")

def test_the_admin_cli_previews_and_sends(users, mail, capsys):
    admin_cli.main(["welcome", "bob", "--preview"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and "User name: bob" in out["text"] and not mail
    admin_cli.main(["welcome", "bob"])
    assert json.loads(capsys.readouterr().out)["sent_to"] == "bob@example.test" and len(mail) == 1
    admin_cli.main(["welcome", "nobody"])
    assert json.loads(capsys.readouterr().out)["ok"] is False

def test_every_page_asks_for_their_own_password_until_they_choose_it(client, users, capsys):
    admin_cli.main(["temp-password", "bob"])
    capsys.readouterr()
    login(client, "bob", "bobpass1")
    assert "still using the password your library admin set" in client.get("/").get_data(as_text=True)
    assert "still using the password your library admin set" in client.get("/hub").get_data(as_text=True)
    post(client, "/devices", action="password", current="bobpass1", new="my-own-pass-1", repeat="my-own-pass-1")
    assert not db.pw_temp("bob")
    assert "still using the password" not in client.get("/").get_data(as_text=True)
    import app as appmod
    other = appmod.app.test_client()
    login(other, "alice", "alicepass1")
    assert "still using the password" not in other.get("/").get_data(as_text=True), "only for accounts marked so"


def test_the_email_walks_through_setting_up_a_kobo_a_kindle_and_a_phone(users, mail, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_sync_enabled", lambda: True)
    monkeypatch.setattr(config, "BOOKS_URL", "https://books.example.test")
    subject, text, html = welcome.compose("bob")
    for want in ("Setting up a Kobo", "Generate my Kobo link", ".kobo/Kobo/Kobo eReader.conf", "api_endpoint=",
                 "Full Kobo guide: https://home.example.test/help/kobo",
                 "Setting up a Kindle", "https://www.amazon.com/mycd",
                 "add lib@example.test to the Approved Personal Document E-mail List",
                 "paste your @kindle.com address", "Full Kindle guide: https://home.example.test/help/kindle",
                 "Reading on a phone or tablet", "https://books.example.test/opds/",
                 "Full guide: https://home.example.test/help/phone-tablet"):
        assert want in text, want
    assert "Library <lib@example.test>" not in text, "the bare address, as Amazon's list wants it"
    assert '<a href="https://home.example.test/help/kobo">' in html and '<a href="https://www.amazon.com/mycd">' in html
    assert '<a href="https://request.example.test/devices">' in html, "no trailing comma inside a link"

def test_kobo_sync_off_says_to_ask_the_admin(users, mail, monkeypatch):
    monkeypatch.setattr(cwa, "kobo_sync_enabled", lambda: False)
    text = welcome.compose("bob")[1]
    assert "Kobo sync is not switched on" in text and "Generate my Kobo link" not in text


# ---- the address readers approve at Amazon: shown exactly, checked, warned about ---------------------------
def test_readers_see_the_bare_sending_address_with_a_copy_button(client, users, mail, monkeypatch):
    login(client, "bob", "bobpass1")
    html = client.get("/devices").get_data(as_text=True)
    assert 'data-copy="lib@example.test"' in html and "Library &lt;lib@example.test&gt;" not in html
    assert "lib@example.test" in client.get("/help/kindle").get_data(as_text=True)
    assert "lib@example.test" in welcome.compose("bob")[1]

def test_a_provider_that_rewrites_the_sender_is_warned_about(users, mail, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_USER", "family.lib@gmail.com")
    monkeypatch.setattr(config, "SMTP_FROM", "Library <books@mfdata.example>")
    assert kindle.sender() == "books@mfdata.example"
    risk = kindle.sender_risk()
    assert "Gmail" in risk and "family.lib@gmail.com" in risk and "books@mfdata.example" in risk
    import dash
    assert any("Send to Kindle may be dropped" in t for _s, t, _l in dash.needs())
    monkeypatch.setattr(config, "SMTP_FROM", "Library <family.lib@gmail.com>")
    assert kindle.sender_risk() is None, "the same address as the login: nothing to warn about"
    monkeypatch.setattr(config, "SMTP_HOST", "mail.mfdata.example")
    monkeypatch.setattr(config, "SMTP_FROM", "books@mfdata.example")
    assert kindle.sender_risk() is None, "your own mail server sends what it is told"

def test_the_sender_check_sends_a_probe_and_reads_back_its_from_line(users, mail, monkeypatch):
    assert kindle.check_sender()["seen_from"] is None and mail[-1]["To"] == "admin@example.test", "no mailbox to read: a person looks"
    assert "must show exactly: lib@example.test" in mail[-1].get_content()
    monkeypatch.setattr(config, "IMAP_HOST", "imap.example.test")
    monkeypatch.setattr(config, "IMAP_USER", "books@example.test")
    import imap

    class Box:
        deleted = []
        def select(self, f): pass
        def search(self, *a):
            return "OK", [b"7"]
        def fetch(self, n, what):
            return "OK", [(b"7", b"From: Someone Else <other@gmail.com>\r\n\r\n")]
        def store(self, n, flag, v): Box.deleted.append(n)
        def expunge(self): pass
        def logout(self): pass
    monkeypatch.setattr(imap, "_connect", lambda: Box())
    out = kindle.check_sender(sleep=lambda s: None)
    assert mail[-1]["To"] == "books@example.test" and out["seen_from"] == "other@gmail.com" and out["match"] is False
    assert Box.deleted == [b"7"], "the probe does not stay in the intake mailbox"

def test_the_intake_poller_leaves_the_probe_alone(users, monkeypatch):
    import imap
    assert 'startswith("[library] sender check")' in open(imap.__file__).read()


# ---- the guides ------------------------------------------------------------------------------------------------
def test_the_new_guides_open_and_the_privacy_answer_is_honest(client, users):
    login(client, "bob", "bobpass1")
    hub = client.get("/hub").get_data(as_text=True)
    assert "Something’s not working" in hub
    t = client.get("/help/troubleshooting").get_data(as_text=True)
    for want in ("I forgot my password", "Test my link", "Approved Personal Document E-mail List", "Verify",
                 "cannot open it", "looking"):
        assert want in t, want
    w = client.get("/help/words").get_data(as_text=True)
    assert "KEPUB" in w and "OPDS catalog" in w and "Admin:" in w
    faq = client.get("/help/faq").get_data(as_text=True)
    assert "No other reader can" in faq and "admin" in faq and "Which file should I download" in faq
    assert "no other reader sees" in client.get("/help/account").get_data(as_text=True)
    assert "only the library&#39;s admin" in welcome.compose("bob")[2] or "only the library's admin" in welcome.compose("bob")[1]
