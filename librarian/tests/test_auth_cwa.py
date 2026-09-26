"""Authentication against, and the few writes into, Calibre-Web's real app.db schema."""
import sqlite3, json, re, sys, io
import pytest
from werkzeug.security import generate_password_hash
import config, cwa, auth

def _row(name):
    c = sqlite3.connect(config.CWA_DB); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM user WHERE name=?", (name,)).fetchone(); c.close()
    return dict(r) if r else None

# ---- users -----------------------------------------------------------------------
def test_add_user_is_isolated_end_user():
    u = cwa.add_user("alice", "alicepass1", "alice@example.test")
    r = _row("alice")
    assert u["name"] == "alice" and r["role"] == cwa.END_USER_ROLES == 338
    assert r["allowed_tags"] == "owner:alice" and r["denied_tags"] == ""
    assert r["email"] == "alice@example.test" and r["kindle_mail"] == ""
    # every column the real schema has is populated the way CWA's own form would do it
    assert r["view_settings"] == "{}" and r["locale"] == "en" and r["default_language"] == "all"
    assert r["theme"] == 1 and r["auto_send_enabled"] == 0 and r["allow_additional_ereader_emails"] == 1
    assert not (r["role"] & cwa.ROLE_ADMIN) and not (r["role"] & cwa.ROLE_UPLOAD) and not (r["role"] & cwa.ROLE_DELETE_BOOKS)

def test_add_admin_sees_everything():
    cwa.add_user("boss", "bosspass1", admin=True)
    r = _row("boss")
    assert r["role"] == cwa.ADMIN_ROLES == 479 and r["allowed_tags"] == "" and r["sidebar_view"] == cwa.ADMIN_SIDEBAR

def test_add_user_defaults_email_and_uses_cwa_default_sidebar():
    c = sqlite3.connect(config.CWA_DB); c.execute("UPDATE settings SET config_default_show=4095"); c.commit(); c.close()
    cwa.add_user("carol", "carolpass1")
    r = _row("carol")
    assert r["email"] == "carol@example.test" and r["sidebar_view"] == 4095

@pytest.mark.parametrize("bad", ["", "al ice", "a/b", "../x", "ALICE!", " alice"])
def test_add_user_rejects_bad_names(bad):
    with pytest.raises(cwa.CwaError):
        cwa.add_user(bad, "password1")

def test_add_user_rejects_short_password_and_duplicates():
    with pytest.raises(cwa.CwaError):
        cwa.add_user("dave", "short")
    cwa.add_user("dave", "davepass1")
    with pytest.raises(cwa.CwaError):
        cwa.add_user("Dave", "davepass2")     # case-insensitive like CWA's login

def test_list_users_flags_isolation():
    cwa.add_user("admin", "adminpass1", admin=True)
    cwa.add_user("alice", "alicepass1")
    c = sqlite3.connect(config.CWA_DB); c.execute("UPDATE user SET allowed_tags='' WHERE name='alice'"); c.commit(); c.close()
    us = {u["name"]: u for u in cwa.list_users()}
    assert us["admin"]["is_admin"] and not us["alice"]["is_admin"]
    assert us["alice"]["isolated"] is False
    assert cwa.ensure_isolation("alice") is True and _row("alice")["allowed_tags"] == "owner:alice"
    assert cwa.ensure_isolation("admin") is False   # admins are never restricted

def test_remove_user_and_last_admin_guard():
    cwa.add_user("admin", "adminpass1", admin=True)
    cwa.add_user("alice", "alicepass1")
    cwa.kobo_token("alice")
    cwa.remove_user("alice")
    assert _row("alice") is None
    c = sqlite3.connect(config.CWA_DB)
    assert c.execute("SELECT COUNT(*) FROM remote_auth_token").fetchone()[0] == 0
    with pytest.raises(cwa.CwaError):
        cwa.remove_user("admin")

# ---- authentication ---------------------------------------------------------------
def test_verify_accepts_pbkdf2_and_scrypt_hashes_and_flags_admin():
    cwa.add_user("alice", "alicepass1")                     # pbkdf2 (ours)
    c = sqlite3.connect(config.CWA_DB)
    c.execute("INSERT INTO user(name,email,role,password,allowed_tags) VALUES('admin','a@x',479,?, '')",
              (generate_password_hash("admin123"),))        # scrypt (what CWA itself writes)
    c.commit(); c.close()
    a = auth.verify("alice", "alicepass1")
    assert (a["name"], a["is_admin"]) == ("alice", False) and re.fullmatch(r"[0-9a-f]{16}", a["fp"])
    assert auth.verify("ALICE", "alicepass1")["name"] == "alice"     # case-insensitive, canonical name back
    b = auth.verify("admin", "admin123"); assert (b["name"], b["is_admin"]) == ("admin", True) and b["fp"] != a["fp"]
    assert auth.verify("alice", "wrong") is None
    assert auth.verify("nobody", "alicepass1") is None
    assert auth.verify("", "") is None
    # the fingerprint follows the stored hash: same until the password changes, gone with the user
    assert auth.fingerprint("alice") == (a["fp"], False) and auth.fingerprint("ADMIN") == (b["fp"], True)
    cwa.set_password("alice", "alicepass2")
    assert auth.fingerprint("alice")[0] != a["fp"] and auth.fingerprint("alice")[0] == auth.verify("alice", "alicepass2")["fp"]
    cwa.remove_user("alice")
    assert auth.fingerprint("alice") is None and auth.fingerprint("") is None

def test_set_password_takes_effect():
    cwa.add_user("alice", "alicepass1")
    cwa.set_password("alice", "newpass123")
    assert auth.verify("alice", "alicepass1") is None and auth.verify("alice", "newpass123")
    with pytest.raises(cwa.CwaError):
        cwa.set_password("ghost", "newpass123")

def test_verify_survives_missing_db(monkeypatch):
    monkeypatch.setattr(config, "CWA_DB", "/nonexistent/app.db")
    assert auth.verify("alice", "x") is auth.UNAVAILABLE      # not "wrong password": no lockout counting (F48)

# ---- devices -----------------------------------------------------------------------
def test_kindle_mail_roundtrip_and_validation():
    cwa.add_user("alice", "alicepass1")
    assert cwa.set_kindle_mail("alice", " alice_42@kindle.com ") == "alice_42@kindle.com"
    assert cwa.get_user("alice")["kindle_mail"] == "alice_42@kindle.com"
    assert cwa.set_kindle_mail("alice", "") == ""
    with pytest.raises(cwa.CwaError):
        cwa.set_kindle_mail("alice", "not an address")
    with pytest.raises(cwa.CwaError):
        cwa.set_kindle_mail("ghost", "a@b.c")

def test_kobo_token_matches_cwa_generate_auth_url_semantics():
    cwa.add_user("alice", "alicepass1")
    assert cwa.kobo_token("alice", create=False) is None
    t1 = cwa.kobo_token("alice")
    assert re.fullmatch(r"[0-9a-f]{32}", t1)                  # hexlify(urandom(16))
    assert cwa.kobo_token("alice") == t1                       # idempotent, like CWA's button
    c = sqlite3.connect(config.CWA_DB); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM remote_auth_token").fetchone()
    assert r["token_type"] == 1 and r["expiration"].startswith("9999-12-31") and r["verified"] == 0
    assert cwa.kobo_url("alice") == f"https://books.example.test/kobo/{t1}"
    t2 = cwa.reset_kobo_token("alice")
    assert t2 != t1 and cwa.kobo_token("alice") == t2
    assert c.execute("SELECT COUNT(*) FROM remote_auth_token").fetchone()[0] == 1

def test_kobo_sync_and_hardening_toggles_report_changes():
    assert cwa.kobo_sync_enabled() is False
    assert cwa.enable_kobo_sync() is True          # changed -> CWA needs a restart
    assert cwa.kobo_sync_enabled() is True
    assert cwa.enable_kobo_sync() is False         # idempotent: nothing to restart for
    c = sqlite3.connect(config.CWA_DB)
    c.execute("UPDATE settings SET config_public_reg=1, config_anonbrowse=1"); c.commit()
    assert cwa.disable_public_registration() is True
    assert c.execute("SELECT config_public_reg, config_anonbrowse, config_kobo_proxy FROM settings").fetchone() == (0, 0, 0)
    assert cwa.disable_public_registration() is False

def _immutable_reader_sees(name):
    """Shelfmark opens app.db with mode=ro&immutable=1, which ignores the WAL entirely."""
    c = sqlite3.connect(f"file:{config.CWA_DB}?mode=ro&immutable=1", uri=True)
    r = c.execute("SELECT name, kindle_mail FROM user WHERE name=?", (name,)).fetchone(); c.close()
    return r

def test_writes_are_checkpointed_so_shelfmark_sees_them_immediately():
    # keep a second connection open the way the running CWA process does
    holder = sqlite3.connect(config.CWA_DB); holder.execute("SELECT COUNT(*) FROM user").fetchone()
    cwa.add_user("alice", "alicepass1")
    assert _immutable_reader_sees("alice") is not None
    cwa.set_kindle_mail("alice", "a@kindle.com")
    assert _immutable_reader_sees("alice")[1] == "a@kindle.com"
    cwa.kobo_token("alice")
    c = sqlite3.connect(f"file:{config.CWA_DB}?mode=ro&immutable=1", uri=True)
    assert c.execute("SELECT COUNT(*) FROM remote_auth_token").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] in ("wal", "delete")   # still a WAL db underneath
    holder.close()

def test_guest_row_is_not_a_user():
    c = sqlite3.connect(config.CWA_DB)
    c.execute("INSERT INTO user(name,email,role,password,allowed_tags) VALUES('Guest','',32,'','')"); c.commit(); c.close()
    cwa.add_user("alice", "alicepass1")
    assert [u["name"] for u in cwa.list_users()] == ["alice"]
    assert auth.verify("Guest", "") is None

# ---- CLI used by bookstack.sh -------------------------------------------------------
def test_cli_roundtrip(capsys):
    assert cwa._cli(["add-user", "alice", "--password", "alicepass1", "--email", "a@x"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["user"] == "alice" and out["kobo_url"].startswith("https://books.example.test/kobo/")
    assert cwa._cli(["list"]) == 0
    assert [u["name"] for u in json.loads(capsys.readouterr().out)] == ["alice"]
    assert cwa._cli(["kindle", "alice", "k@kindle.com"]) == 0
    assert json.loads(capsys.readouterr().out)["kindle_mail"] == "k@kindle.com"
    assert cwa._cli(["harden"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "changed": True, "restart_cwa": True}
    assert cwa.kobo_sync_enabled()
    assert cwa._cli(["harden"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False
    assert cwa._cli(["add-user", "alice", "--password", "alicepass1"]) == 2     # duplicate -> error exit
    assert "already exists" in capsys.readouterr().err

def test_cli_reads_passwords_from_stdin(capsys, monkeypatch):
    """bookstack.sh pipes the secret in so it never appears in argv / ps / docker inspect."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-pass-1\n"))
    assert cwa._cli(["add-user", "alice", "--password-stdin", "--email", "a@x"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] and auth.verify("alice", "stdin-pass-1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-pass-2"))                  # no trailing newline is fine too
    assert cwa._cli(["passwd", "alice", "--password-stdin"]) == 0
    assert auth.verify("alice", "stdin-pass-2") and not auth.verify("alice", "stdin-pass-1")
    for bad in (["passwd", "alice"], ["passwd", "alice", "--password", "x", "--password-stdin"], ["add-user", "bob"]):
        with pytest.raises(SystemExit):                                              # exactly one of the two is required
            cwa._cli(bad)
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    assert cwa._cli(["passwd", "alice", "--password-stdin"]) == 2 and "at least 8" in capsys.readouterr().err

def test_cli_rename_user_for_a_chosen_admin_name(capsys):
    """C10: the installer lets the admin pick a name other than 'admin' and renames the row."""
    cwa.add_user("admin", "adminpass1", admin=True); cwa.add_user("alice", "alicepass1")
    assert cwa._cli(["rename-user", "admin", "kenith-admin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "old": "admin", "new": "kenith-admin", "id": out["id"],
                   "portal_rows": out["portal_rows"]}
    assert cwa.get_user("admin") is None and auth.verify("kenith-admin", "adminpass1")["is_admin"]
    assert _immutable_reader_sees("kenith-admin") is not None                        # WAL checkpointed for Shelfmark
    assert cwa._cli(["rename-user", "kenith-admin", "alice"]) == 2 and "already exists" in capsys.readouterr().err
    assert cwa._cli(["rename-user", "kenith-admin", "bad name"]) == 2 and "no spaces" in capsys.readouterr().err
    assert cwa._cli(["rename-user", "nobody", "x"]) == 2 and "no such user" in capsys.readouterr().err
    assert cwa._cli(["rename-user", "alice", "alicia"]) == 2 and "not an admin" in capsys.readouterr().err
    # J17: uppercase is refused everywhere (Shelfmark matches names case-sensitively)
    assert cwa._cli(["rename-user", "kenith-admin", "Kenith-Admin"]) == 2 and "lowercase" in capsys.readouterr().err
    assert cwa.get_user("kenith-admin")["name"] == "kenith-admin"

def test_passive_checkpoint_folds_cwa_ui_writes_for_shelfmark():
    """F50: a password changed in CWA's own UI sits in the WAL, which immutable=1 readers ignore."""
    cwa.add_user("alice", "alicepass1")
    holder = sqlite3.connect(config.CWA_DB)                   # CWA's own connection keeps the WAL alive
    holder.execute("UPDATE user SET kindle_mail='ui@kindle.com' WHERE name='alice'"); holder.commit()
    assert _immutable_reader_sees("alice")[1] != "ui@kindle.com"
    assert cwa.checkpoint_passive() is not None
    assert _immutable_reader_sees("alice")[1] == "ui@kindle.com"
    holder.close()
