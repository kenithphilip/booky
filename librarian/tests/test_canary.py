"""L08: the synthetic canary journey. The host script (scripts/synthetic.py) is exercised live by
tests/stack-test.sh; here: the portal's half — hidden accounts, the run log, the /admin card, and
the session the portal mints only for a canary account while Turnstile guards the login form."""
import importlib.util, io, json, os, sqlite3, sys, zipfile
import pytest
import admin_cli, config, cwa, db
from conftest import login

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def canaries(users, monkeypatch):
    monkeypatch.setattr(config, "CANARY_USERS", ("canary-a", "canary-b"))
    monkeypatch.setattr(cwa, "_abs_sync", lambda *a, **k: pytest.fail("a canary must not get an ABS account"))
    for n in ("canary-a", "canary-b"):
        assert cwa._cli(["add-user", n, "--password", "canarypass-1", "--no-abs"]) == 0


def cli(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = admin_cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_canary_accounts_are_hidden_from_every_user_list(canaries, capsys):
    names = [u["name"] for u in cwa.list_users()]
    assert "canary-a" not in names and "canary-b" not in names and "alice" in names
    assert {"canary-a", "canary-b"} <= {u["name"] for u in cwa.list_users(include_canary=True)}
    capsys.readouterr()
    cwa._cli(["list"]); assert "canary-a" not in capsys.readouterr().out
    cwa._cli(["list", "--all"]); assert "canary-a" in capsys.readouterr().out


def test_canary_accounts_are_ordinary_isolated_readers(canaries):
    u = cwa.get_user("canary-a")
    assert u["allowed_tags"] == cwa.owner_tag("canary-a")


def test_a_name_is_only_hidden_while_it_is_a_canary(users, monkeypatch):
    monkeypatch.setattr(config, "CANARY_USERS", ())
    cwa.add_user("canary-a", "canarypass-1")
    assert "canary-a" in [u["name"] for u in cwa.list_users()]


def test_runs_are_recorded_with_the_failing_step(users, capsys, monkeypatch):
    run = {"ts": 1000, "ok": True, "secs": 80.2, "import_secs": 41.5,
           "steps": [{"name": "upload through the portal", "ok": True, "secs": 1},
                     {"name": "another reader is refused it", "ok": False, "secs": 0.2, "note": "HTTP 200"}]}
    rc, out = cli(capsys, "canary", "record", stdin=json.dumps(run), monkeypatch=monkeypatch)
    assert rc == 0 and out["ok"] is True and out["passed"] is False
    last = db.canary_recent(1)[0]
    assert last["ok"] is False, "a failed step fails the run even when the script said ok"
    assert last["failed"] == "another reader is refused it" and last["import_secs"] == 41.5
    assert any(a["event"] == "canary_failed" for a in db.audit_recent(5))
    rc, out = cli(capsys, "canary", "recent", "--limit", "5")
    assert out["rows"][0]["steps"][1]["note"] == "HTTP 200"


def test_the_run_log_is_bounded(users):
    for i in range(db.CANARY_KEEP + 5):
        db.canary_record({"ok": True, "steps": [{"name": "x", "ok": True}]})
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM canary_runs").fetchone()[0] == db.CANARY_KEEP


def test_admin_sees_the_canary_card(client, canaries):
    db.canary_record({"ok": True, "secs": 70, "import_secs": 33, "steps": [{"name": "upload", "ok": True}]})
    db.canary_record({"ok": False, "secs": 12, "steps": [{"name": "Shelfmark login", "ok": False, "note": "HTTP 502"}]})
    login(client, "admin", "adminpass1")
    html = client.get("/admin").get_data(as_text=True)
    assert "Canary journey" in html and "FAILED at Shelfmark login" in html and "HTTP 502" in html
    assert "33 s" in html
    # only the audit trail (account created from the TUI) names it, never the user list
    assert html.count("canary-a") == html.count('<td>canary-a</td><td class="mut">tui</td>')


def test_minted_session_logs_the_canary_in_and_nobody_else(client, canaries, capsys):
    rc, out = cli(capsys, "canary", "session", "canary-a")
    assert rc == 0 and out["cookie"] and out["csrf"]
    client.set_cookie("session", out["cookie"])
    r = client.get("/library")
    assert r.status_code == 200, "the portal accepts the session it signed"
    r = client.post("/logout", data={"csrf": out["csrf"]})
    assert r.status_code in (302, 303), "and its CSRF token"
    for name in ("alice", "admin", "nobody"):
        rc, out = cli(capsys, "canary", "session", name)
        assert rc == 1 and not out["ok"] and "not a canary account" in out["error"]


def test_a_removed_canary_gets_no_session(canaries, capsys):
    cwa.remove_user("canary-b")
    rc, out = cli(capsys, "canary", "session", "canary-b")
    assert rc == 1 and "missing" in out["error"]


def _synthetic():
    spec = importlib.util.spec_from_file_location("synthetic", os.path.join(ROOT, "scripts", "synthetic.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_the_generated_book_is_a_valid_epub_the_portal_accepts(tmp_path):
    import app as appmod
    data = _synthetic().make_epub("Canary 20260926-0620-abc123", "Bookstack Canary")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.infolist()[0].filename == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert b"<dc:title>Canary 20260926-0620-abc123</dc:title>" in z.read("content.opf")

    class F:
        stream = io.BytesIO(data)
    assert appmod._upload_problem(F, "epub") is None


def test_the_library_lookup_needs_the_owner_tag(tmp_path, monkeypatch):
    m = _synthetic()
    p = tmp_path / "metadata.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, timestamp TEXT);"
                    "CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT);"
                    "CREATE TABLE books_tags_link(book INT, tag INT);"
                    "INSERT INTO books VALUES(1,'Canary X','2026-09-26 06:20:00+00:00'),(2,'Canary Y','2026-09-26 06:20:00+00:00');"
                    "INSERT INTO tags VALUES(1,'owner:canary-a'),(2,'owner:canary-b');"
                    "INSERT INTO books_tags_link VALUES(1,1),(2,2);")
    c.commit(); c.close()
    monkeypatch.setattr(m, "METADATA_DB", str(p)); monkeypatch.setattr(m, "A", "canary-a")
    assert [r[0] for r in m.library_books("Canary %", "canary-a")] == [1]
    assert m.library_books("Canary Y", "canary-a") == [], "a book without MY owner tag is not an import"


def test_canary_accounts_never_notify(monkeypatch):
    import notify
    monkeypatch.setattr(config, "CANARY_USERS", ("canary-a", "canary-b"))
    sent = []
    monkeypatch.setattr(notify, "_webhook", lambda e, r: sent.append((e, r.get("owner"))))
    monkeypatch.setattr(notify, "_mail", lambda e, r: sent.append(("mail", r.get("owner"))))
    notify.send("done", {"owner": "canary-a", "title": "Canary 20260927"})
    assert sent == [], "the canary's test book must not buzz the family's phones twice a day"
    notify.send("done", {"owner": "alice", "title": "Emma"})
    assert ("done", "alice") in sent


def _shelf_env(monkeypatch, method, answers):
    m = _synthetic()
    monkeypatch.setattr(m, "E", {"SHELFMARK_AUTH_METHOD": method})
    monkeypatch.setattr(m, "A", "canary-a"); monkeypatch.setattr(m, "A_PW", "pw")
    calls = []
    def fake(self, url, **kw):
        calls.append((url, kw))
        return answers[len(calls) - 1]
    monkeypatch.setattr(m.Client, "req", fake)
    return m, calls


def test_behind_the_gate_the_canary_checks_shelfmarks_header_login(monkeypatch):
    ok = json.dumps({"authenticated": True, "username": "canary-a", "is_admin": False}).encode()
    m, calls = _shelf_env(monkeypatch, "proxy", [(200, {}, ok), (401, {}, b"")])
    assert m.shelfmark_ok()[0] is True
    assert calls[0][0].endswith("/api/auth/check") and calls[0][1]["headers"]["Remote-User"] == "canary-a"
    assert "json_body" not in calls[0][1], "no password login: Shelfmark has none in proxy mode"
    m, _ = _shelf_env(monkeypatch, "proxy", [(200, {}, ok), (200, {}, b"{}")])
    assert m.shelfmark_ok()[0] is False, "a Shelfmark that answers without an identity is a failure"
    admin = json.dumps({"authenticated": True, "username": "canary-a", "is_admin": True}).encode()
    m, _ = _shelf_env(monkeypatch, "proxy", [(200, {}, admin), (401, {}, b"")])
    assert m.shelfmark_ok()[0] is False, "the canary must never be an admin"


def test_without_the_gate_the_canary_uses_the_password_login(monkeypatch):
    m, calls = _shelf_env(monkeypatch, "cwa", [(200, {}, b"{}")])
    assert m.shelfmark_ok()[0] is True and calls[0][0].endswith("/api/auth/login")
