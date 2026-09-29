"""L05: one login behind the Authelia gate.

  * the portal trusts Remote-User only beside the gate secret Caddy adds (and strips from clients)
  * Calibre-Web's header login follows the gate: on with exactly Remote-User, never auto-create
  * portal password changes reach Authelia's file through the host (scripts/gate-sync.py), as a
    PBKDF2-SHA512 hash Authelia 4.39 verifies — never the password itself
"""
import base64, hashlib, importlib.util, json, os, sqlite3
import pytest
import admin_cli, config, cwa, db
from conftest import login, post

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET = "g" * 64


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True)
    monkeypatch.setattr(config, "GATE_SECRET", SECRET)
    monkeypatch.setattr(config, "DOMAIN", "example.test")


def hdr(user, secret=SECRET):
    h = {"Remote-User": user}
    if secret is not None:
        h["X-Bookstack-Gate"] = secret
    return h


def test_the_gate_signs_the_reader_in(client, gate):
    r = client.get("/status", headers=hdr("alice"))
    assert r.status_code == 200 and b"alice" in r.data
    with client.session_transaction() as s:
        assert s["user"] == "alice" and s["sso"] is True and not s["admin"]
    assert any(a["event"] == "login_sso" for a in db.audit_recent(5))


def test_admin_rights_come_from_calibre_web_not_the_header(client, gate):
    # v6.0: through the gate, admin needs BOTH the Calibre-Web role and Authelia's admins group
    # (the group is what makes Authelia ask admins for a second factor)
    client.get("/status", headers={**hdr("admin"), "Remote-Groups": "users,admins"})
    with client.session_transaction() as s:
        assert s["admin"] is True
    client.get("/logout")
    with client.session_transaction() as s:
        s.clear()
    client.get("/status", headers=hdr("admin"))
    with client.session_transaction() as s:
        assert s["user"] == "admin" and s["admin"] is False, "a Calibre-Web admin outside the admins group signed in with one factor"
    client.get("/status", headers={**hdr("bob"), "Remote-Groups": "admins"})
    with client.session_transaction() as s:
        assert s["user"] == "bob" and s["admin"] is False


@pytest.mark.parametrize("secret", [None, "", "wrong" * 10, SECRET[:-1]])
def test_without_the_gate_secret_the_header_means_nothing(client, gate, secret):
    r = client.get("/status", headers=hdr("admin", secret))
    assert r.status_code in (302, 303) and "/login" in r.headers["Location"]


def test_nothing_is_trusted_while_the_gate_is_off(client, monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", False)
    monkeypatch.setattr(config, "GATE_SECRET", SECRET)
    assert client.get("/status", headers=hdr("admin")).status_code in (302, 303)
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True)
    monkeypatch.setattr(config, "GATE_SECRET", "")
    assert client.get("/status", headers=hdr("admin", "")).status_code in (302, 303)


def test_a_gate_user_without_a_library_account_gets_no_session(client, gate):
    assert client.get("/status", headers=hdr("mallory")).status_code in (302, 303)


def test_when_the_gate_names_someone_else_the_old_session_goes(client, gate):
    login(client, "alice", "alicepass1")
    client.get("/status", headers=hdr("bob"))
    with client.session_transaction() as s:
        assert s["user"] == "bob"
    client.get("/status", headers=hdr("mallory"))       # a gate user with no library account
    with client.session_transaction() as s:
        assert "user" not in s


def test_logging_out_of_a_gate_session_ends_the_gate_session_too(client, gate):
    client.get("/status", headers=hdr("alice"))
    with client.session_transaction() as s:
        tok = s["csrf"]
    r = client.post("/logout", data={"csrf": tok})
    assert r.headers["Location"].startswith("https://auth.example.test/logout?rd=https://request.example.test/")


def test_calibre_web_header_login_follows_the_gate(users, monkeypatch):
    def settings():
        c = sqlite3.connect(config.CWA_DB); c.row_factory = sqlite3.Row
        cols = {r[1] for r in c.execute("PRAGMA table_info(settings)")}
        r = dict(c.execute("SELECT * FROM settings LIMIT 1").fetchone()); c.close()
        return r, cols
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", True); monkeypatch.setattr(config, "GATE_SECRET", SECRET)
    cwa.disable_public_registration()                     # what Deploy's `cwa harden` runs
    r, cols = settings()
    assert r["config_allow_reverse_proxy_header_login"] == 1
    if "config_reverse_proxy_login_header_name" in cols:
        assert r["config_reverse_proxy_login_header_name"] == "Remote-User", "exactly the header Caddy strips"
    if "config_reverse_proxy_auto_create_users" in cols:
        assert r["config_reverse_proxy_auto_create_users"] == 0
    monkeypatch.setattr(config, "GATE_SECRET", "")        # gate without its secret: off again
    cwa.disable_public_registration()
    assert settings()[0]["config_allow_reverse_proxy_header_login"] == 0
    assert cwa._cli(["proxy-login", "on"]) == 0 and settings()[0]["config_allow_reverse_proxy_header_login"] == 1
    assert cwa._cli(["proxy-login", "off"]) == 0 and settings()[0]["config_allow_reverse_proxy_header_login"] == 0


# ---- password sync --------------------------------------------------------------------------
def verify_pbkdf2(h, password):
    """What Authelia does with `$pbkdf2-sha512$rounds$salt$key` (passlib's adapted base64)."""
    _, alg, rounds, salt, key = h.split("$")
    unab = lambda s: base64.b64decode(s.replace(".", "+") + "=" * (-len(s) % 4))
    return alg == "pbkdf2-sha512" and hashlib.pbkdf2_hmac("sha512", password.encode(), unab(salt), int(rounds), 64) == unab(key)


def test_the_gate_hash_is_the_format_authelia_verifies():
    h = db.gate_hash("Tr1cky pa$$ word")
    assert h.startswith(f"$pbkdf2-sha512${db.GATE_ROUNDS}$") and "+" not in h and "=" not in h
    assert verify_pbkdf2(h, "Tr1cky pa$$ word") and not verify_pbkdf2(h, "wrong")
    assert db.gate_hash("x") != db.gate_hash("x"), "salted"


def test_a_password_change_is_queued_for_the_gate_and_the_host_is_woken(client, gate):
    flag = db.gate_flag_path()
    if os.path.exists(flag):
        os.unlink(flag)
    login(client, "alice", "alicepass1")
    r = post(client, "/devices", action="password", current="alicepass1", new="newpass-2026", repeat="newpass-2026")
    assert b"takes the new password within a minute" in r.data or r.status_code in (302, 303)
    row = [g for g in db.gate_pending() if g["user"] == "alice"][0]
    assert verify_pbkdf2(row["hash"], "newpass-2026") and not row["email"]
    assert os.path.exists(flag), "the path unit on the host watches this file"
    with db._conn() as c:
        assert b"newpass-2026" not in json.dumps([dict(r) for r in c.execute("SELECT * FROM gate_pw")]).encode()


def test_a_change_right_after_creation_keeps_the_pending_login(users):
    db.gate_queue("carol", "first-pw-1", email="c@example.test", display="carol", admin=False)
    db.gate_queue("carol", "second-pw-2")
    row = db.gate_pending()[0]
    assert row["email"] == "c@example.test" and verify_pbkdf2(row["hash"], "second-pw-2")


def test_nothing_is_queued_while_the_gate_is_off(client, monkeypatch):
    monkeypatch.setattr(config, "AUTHELIA_ENABLED", False)
    login(client, "alice", "alicepass1")
    post(client, "/devices", action="password", current="alicepass1", new="newpass-2026", repeat="newpass-2026")
    assert db.gate_pending() == []


def test_the_host_reports_back(users, capsys):
    db.gate_queue("alice", "pw-123456")
    assert admin_cli.main(["gate", "pending"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["user"] == "alice"
    for _ in range(4):
        admin_cli.main(["gate", "done", "alice", "missing", "--reason", "no gate login"])
    assert db.gate_pending()[0]["attempts"] == 4
    admin_cli.main(["gate", "done", "alice", "missing"])
    assert db.gate_pending() == [], "given up after five tries (the admin sees it in the audit trail)"
    db.gate_queue("bob", "pw-123456")
    admin_cli.main(["gate", "done", "bob", "ok"])
    assert db.gate_pending() == []


def test_the_portal_login_form_behind_the_gate_keeps_the_group_rule(client, gate):
    """A Calibre-Web admin outside Authelia's admins group got in with a password alone; posting
    the portal's own /login form (same user, their password) must not make them an admin."""
    client.get("/status", headers=hdr("admin"))
    with client.session_transaction() as s:
        tok = s["csrf"]
    client.post("/login", data={"username": "admin", "password": "adminpass1", "csrf": tok}, headers=hdr("admin"))
    client.get("/status", headers=hdr("admin"))
    with client.session_transaction() as s:
        assert s["user"] == "admin" and s["admin"] is False
    assert client.get("/admin", headers=hdr("admin")).status_code in (302, 403)
    client.get("/status", headers={**hdr("admin"), "Remote-Groups": "users,admins"})
    with client.session_transaction() as s:
        assert s["gate_admin"] is True

def test_a_signed_in_reader_is_sent_on_to_next(client):
    login(client, "bob", "bobpass1")
    r = client.get("/login?next=/hub")
    assert r.status_code == 302 and r.headers["Location"].endswith("/hub")
    assert client.get("/login?next=//evil.example").headers["Location"].endswith("/")


def _sync():
    spec = importlib.util.spec_from_file_location("gate_sync", os.path.join(ROOT, "scripts", "gate-sync.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


USERS = '''users:
  alice:
    displayname: "Alice"
    password: "$argon2id$v=19$m=65536,t=3,p=4$old$hash"
    email: "alice@example.test"
    groups:
      - users
  bob:
    displayname: "Bob"
    password: "$argon2id$v=19$m=65536,t=3,p=4$bob$hash"
    email: "bob@example.test"
    groups:
      - users
'''


def test_gate_sync_replaces_only_the_password_line():
    m = _sync(); h = db.gate_hash("new-pw-1")
    out, outcome, _ = m.apply(USERS, {"user": "alice", "hash": h})
    assert outcome == "ok"
    assert f'    password: "{h}"' in out and '"alice@example.test"' in out
    assert '$argon2id$v=19$m=65536,t=3,p=4$bob$hash' in out, "nobody else's entry changes"
    assert out.count("  alice:") == 1


def test_gate_sync_creates_a_login_only_with_an_email():
    m = _sync(); h = db.gate_hash("carol-pw-1")
    out, outcome, _ = m.apply(USERS, {"user": "carol", "hash": h})
    assert outcome == "missing" and out == USERS
    out, outcome, _ = m.apply("users: {}\n", {"user": "carol", "hash": h, "email": "c@example.test", "display": "carol", "admin": 1})
    assert outcome == "ok" and "users: {}" not in out
    assert '  carol:\n    displayname: "carol"\n    password: "%s"\n    email: "c@example.test"\n    groups:\n      - users\n      - admins\n' % h in out


@pytest.mark.parametrize("row", [{"user": "al ice", "hash": "$pbkdf2-sha512$1$a$b"},
                                 {"user": "alice", "hash": "plaintext"},
                                 {"user": "alice", "hash": '$pbkdf2-sha512$1$a$b"\n  evil:'}])
def test_gate_sync_refuses_anything_that_could_rewrite_the_file(row):
    out, outcome, _ = _sync().apply(USERS, row)
    assert outcome == "failed" and out == USERS


# ---- Shelfmark (proxy mode) and Audiobookshelf (OpenID Connect) behind the gate ---------------
class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data, text
    def json(self):
        return self._d if self._d is not None else {}


def test_the_portal_names_its_service_account_to_shelfmark_in_proxy_mode(monkeypatch):
    import shelfmark_api
    monkeypatch.setattr(config, "SHELFMARK_AUTH_METHOD", "proxy")
    monkeypatch.setattr(config, "SHELFMARK_SVC_USER", "svc-portal")
    monkeypatch.setattr(config, "SHELFMARK_SVC_PASS", "")
    seen = {}
    def fake(method, url, headers=None, **kw):
        seen.update(method=method, url=url, headers=headers)
        return _Resp(200, [])
    monkeypatch.setattr(shelfmark_api.requests, "request", fake)
    monkeypatch.setattr(shelfmark_api.requests, "post", lambda *a, **k: pytest.fail("no password login in proxy mode"))
    assert shelfmark_api.configured(), "no password is needed in proxy mode"
    shelfmark_api._call("GET", "/api/admin/requests")
    assert seen["headers"] == {"Remote-User": "svc-portal", "Remote-Groups": "admins"}


def test_shelfmark_refusing_the_identity_is_an_error_not_a_login_loop(monkeypatch):
    import shelfmark_api
    monkeypatch.setattr(config, "SHELFMARK_AUTH_METHOD", "proxy")
    monkeypatch.setattr(shelfmark_api.requests, "request", lambda *a, **k: _Resp(401))
    with pytest.raises(shelfmark_api.ShelfmarkError):
        shelfmark_api._call("GET", "/api/admin/requests")


def test_audiobookshelf_openid_settings_keep_every_existing_account_as_it_is(monkeypatch):
    import abs as absapi
    s = absapi.oidc_settings(True, domain="example.test", secret="s3cret")
    assert s["authActiveAuthMethods"] == ["local", "openid"], "local stays: the apps' saved logins and the automation use it"
    assert s["authOpenIDMatchExistingBy"] == "username" and s["authOpenIDAutoRegister"] is False
    assert s["authOpenIDGroupClaim"] == "" and s["authOpenIDAdvancedPermsClaim"] == "", "no claim may rewrite a reader's permissions"
    assert s["authOpenIDIssuerURL"] == "https://auth.example.test" and s["authOpenIDTokenURL"] == "https://auth.example.test/api/oidc/token"
    assert s["authOpenIDClientID"] == "audiobookshelf" and s["authOpenIDClientSecret"] == "s3cret"
    assert s["authOpenIDMobileRedirectURIs"] == ["audiobookshelf://oauth"]
    assert s["authOpenIDSubfolderForRedirectURLs"] == "", "never left undefined (ABS would build /undefined/auth/openid/callback)"
    assert absapi.oidc_settings(False)["authActiveAuthMethods"] == ["local"]
    monkeypatch.setattr(config, "ABS_OIDC_SECRET", "")
    monkeypatch.setattr(config, "DOMAIN", "example.test")
    with pytest.raises(absapi.AbsError):
        absapi.oidc_settings(True)


def test_audiobookshelf_must_confirm_the_switch(monkeypatch):
    import abs as absapi
    monkeypatch.setattr(config, "ABS_OIDC_SECRET", "s3cret"); monkeypatch.setattr(config, "DOMAIN", "example.test")
    calls = []
    def fake(method, path, token=None, **kw):
        calls.append((method, path))
        return _Resp(200, {"authActiveAuthMethods": ["local"]}) if method == "GET" else _Resp(200)
    monkeypatch.setattr(absapi, "_req", fake)
    with pytest.raises(absapi.AbsError, match="did not switch"):
        absapi.set_oidc(True)
    assert calls == [("PATCH", "/api/auth-settings"), ("GET", "/api/auth-settings")]


def test_gate_sync_keeps_the_admins_group_in_step_with_calibre_web():
    gs = _sync()
    text = ("users:\n  alice:\n    displayname: \"Alice\"\n    password: \"$argon2id$x\"\n    groups:\n      - users\n"
            "  bob:\n    displayname: \"Bob\"\n    password: \"$argon2id$y\"\n    groups:\n      - users\n      - admins\n")
    out, moved = gs.sync_groups(text, {"alice"})
    assert sorted(moved) == ["alice", "bob"]
    assert "  alice:\n    displayname: \"Alice\"\n    password: \"$argon2id$x\"\n    groups:\n      - users\n      - admins\n" in out
    assert "  bob:\n    displayname: \"Bob\"\n    password: \"$argon2id$y\"\n    groups:\n      - users\n" in out and out.count("admins") == 1
    assert gs.sync_groups(out, {"alice"}) == (out, []), "nothing to change: the file is not rewritten"
