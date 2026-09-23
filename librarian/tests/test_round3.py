"""Round-3 regressions.

V-ids are the verification round's findings, B-ids this agent's. Each test names the failure
it locks down; the fakes are the ones the rest of the suite uses (a real CWA app.db schema, a
real calibre metadata.db schema, a scripted Audiobookshelf, a scripted IMAP mailbox).
"""
import base64, errno, json, os, time, zipfile
import pytest
import admin_cli, config, db, cwa, tagger, worker
import abs as absapi
from conftest import make_epub, make_cbz, add_calibre_book, login, post
from test_abs import FakeABS


def _req(owner="alice", **kw):
    status = kw.pop("status", "queued")
    r = {"kind": "ebook", "source": "gutenberg", "title": "T", "author": "A",
         "download_url": "https://x/y.epub"}
    r.update(kw)
    return db.add(owner, r, status=status)


def _zip(path, members, comment=None):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
        if comment is not None:
            z.comment = comment
    return path


def _claim_many_entries(path, count=60000):
    """Patch a real zip's End-Of-Central-Directory record so it CLAIMS `count` members without
    holding them: exactly the shape of a zip bomb, minus the hundreds of MB."""
    with open(path, "rb") as f:
        raw = bytearray(f.read())
    at = raw.rfind(b"PK\x05\x06")
    assert at >= 0
    raw[at + 8:at + 12] = count.to_bytes(2, "little") * 2      # entries on this disk, and total
    with open(path, "wb") as f:
        f.write(raw)
    return path


# ---------------------------------------------------------------- V01 mailed attachments
def test_v01_a_mailed_attachment_can_never_outgrow_the_portal(monkeypatch):
    """A 94 MB attachment passed MAX_UPLOAD_MB and both caps, and message_from_bytes +
    get_payload(decode=True) (about 12x the attachment) then SIGKILLed the container under
    mem_limit 1g. BODY.PEEK left the message unseen, so it came back every 60 s for ever."""
    import email as emailmod
    import imap
    from test_core import FakeImap, _mail
    cwa.add_user("alice", "alicepass1", "alice@example.test")
    monkeypatch.setattr(config, "MAX_MAIL_MB", 1)
    assert config.MAX_MAIL_MB < config.MAX_UPLOAD_MB    # mail is parsed in memory, uploads are not

    parsed = []
    real = emailmod.message_from_bytes
    monkeypatch.setattr(emailmod, "message_from_bytes",
                        lambda b, *a, **k: (parsed.append(len(b)), real(b, *a, **k))[1])
    big = _mail("intake+alice@example.test", filename="big.epub",
                payload=b"x" * (2 * 1024 * 1024))
    small = _mail("intake+alice@example.test", filename="ok.epub")
    fake = FakeImap([big, small])
    monkeypatch.setattr(imap, "_connect", lambda: fake)

    assert imap.poll_once() == 1                                  # only the small one filed
    assert all(n < imap._over_limit(1 << 20) for n in parsed)      # the big one was never parsed
    assert fake.flagged == [b"1", b"2"]                            # ... and IS marked seen: no refetch loop
    rejected = [a["detail"] for a in db.audit_recent(20) if a["event"] == "imap_rejected"]
    assert len(rejected) == 1 and "over the 1 MB limit" in rejected[0]
    assert os.listdir(os.path.join(config.DROPBOX_DIR, "alice")) == ["ok.epub"]


# ---------------------------------------------------------------- V03 audiobook zip in a folder
def test_v03_a_folder_holding_only_an_audiobook_zip_is_audio(tmp_path):
    """Regression from the previous round: excluding 'zip' from the audio extensions made
    dropbox/zoe/The Hobbit/hobbit.zip classify as 'nothing', so it was parked."""
    d = tmp_path / "The Hobbit"
    d.mkdir()
    _zip(str(d / "hobbit.zip"), {"01.mp3": b"ID3" + b"0" * 32, "02.mp3": b"ID3" + b"0" * 32})
    assert worker._classify_dir(str(d)) == "audio"
    # ... and a plain zip with nothing usable in it still is not
    d2 = tmp_path / "Junk"
    d2.mkdir()
    _zip(str(d2 / "junk.zip"), {"readme.txt2": b"x"})
    assert worker._classify_dir(str(d2)) == "nothing"


def test_v03_the_zip_is_unpacked_so_audiobookshelf_can_read_it(monkeypatch):
    """Classifying it as audio is not enough: ABS cannot play a zip, so the folder would land
    in the library with nothing playable in it and the tag job would wait 24 h for an item
    that never appears."""
    cwa.add_user("zoe", "zoepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    box = os.path.join(config.DROPBOX_DIR, "zoe")
    folder = os.path.join(box, "The Hobbit")
    os.makedirs(folder)
    _zip(os.path.join(folder, "hobbit.zip"), {"01.mp3": b"ID3" + b"0" * 32})
    old = time.time() - 3600
    for root, _d, names in os.walk(folder):
        for n in names:
            os.utime(os.path.join(root, n), (old, old))

    assert worker.scan_dropbox_once() == 1
    final = os.path.join(config.AUDIO_DIR, "zoe - The Hobbit")
    assert os.path.isdir(final) and os.listdir(final) == ["01.mp3"]     # unpacked, archive gone
    assert db.rows_by_status(("error",)) == []


# ---------------------------------------------------------------- V04 / V06 / V07 reconciliation
def _done_row_waiting_in_ingest(monkeypatch):
    sent = []
    monkeypatch.setattr(worker.notify, "send", lambda ev, rec: sent.append(ev))
    monkeypatch.setattr(worker.notify, "alert", lambda *a, **k: None)
    rid = _req("alice", source="dropbox", download_url="local", status="done")
    db.set_status(rid, "done", "tagged owner:alice")
    sent.clear()
    stuck = os.path.join(config.INGEST_DIR, f"book [alice-{rid}].epub")
    make_epub(stuck)
    return rid, stuck, sent


def test_v04_a_nudged_row_is_not_announced_a_second_time(monkeypatch):
    """reconcile flips done -> importing -> done; the second _finish re-entered notify.send and
    the reader got 'your book is in your library' twice for one book."""
    cwa.add_user("alice", "alicepass1")
    rid, stuck, sent = _done_row_waiting_in_ingest(monkeypatch)
    later = time.time() + 1000
    assert worker.reconcile_imports(later) == 1 and db.get(rid)["status"] == "importing"
    os.remove(stuck)
    add_calibre_book(41, f"book [alice-{rid}]", "Someone", tags=("owner:alice",))
    assert worker.reconcile_imports(later + 1) == 1 and db.get(rid)["status"] == "done"
    assert sent.count("done") == 0        # it was already announced when it first went done
    assert "still waiting" not in (db.get(rid)["detail"] or "")


def test_v04_a_row_that_was_never_done_is_still_announced(monkeypatch):
    """The suppression must be narrow: a row that only ever was 'importing' has not been
    announced yet and must be."""
    cwa.add_user("alice", "alicepass1")
    sent = []
    monkeypatch.setattr(worker.notify, "send", lambda ev, rec: sent.append(ev))
    rid = _req("alice", source="dropbox", download_url="local", status="importing")
    db.set_status(rid, "importing", "handed to the library")
    add_calibre_book(42, f"x [alice-{rid}]", "Someone")
    assert worker.reconcile_imports(time.time() + 1000) == 1
    assert db.get(rid)["status"] == "done" and sent == ["done"]


def test_v06_a_settled_row_is_not_walked_for_ever(monkeypatch):
    """Nothing aged a reconciled row out, so the newest 200 rows were re-walked every pass."""
    cwa.add_user("alice", "alicepass1")
    rid, stuck, _sent = _done_row_waiting_in_ingest(monkeypatch)
    ancient = time.time() + worker.RECONCILE_MAX_AGE + 100
    assert worker.reconcile_imports(ancient) == 0            # older than the ceiling: left alone
    assert db.get(rid)["status"] == "done"
    assert worker.reconcile_imports(time.time() + 1000) == 1  # ... but still inside it, it is checked


def test_v07_reconciliation_has_its_own_minute_cadence(monkeypatch):
    """It used to ride on the 300 s WAL checkpoint, so a reader saw 'done' (and got the mail)
    for up to IMPORT_GRACE + CHECKPOINT ~ 8 minutes before it was corrected."""
    assert worker.RECONCILE_EVERY == 60 and worker.RECONCILE_EVERY < worker.CHECKPOINT_EVERY
    runs, checkpoints = [], []
    monkeypatch.setattr(worker, "reconcile_imports", lambda *a: runs.append(1))
    monkeypatch.setattr(worker, "check_password_drift", lambda *a: checkpoints.append(1))
    monkeypatch.setattr(worker.cwa, "checkpoint_passive", lambda: None)
    monkeypatch.setattr(worker, "process_tag_jobs", lambda now=None: 0)
    monkeypatch.setattr(worker, "nudge_ingest_once", lambda now=None: 0)
    worker._LAST_CHECKPOINT[0] = worker._LAST_RECONCILE[0] = 0.0
    t = 10_000.0
    for i in range(5):                       # five minutes
        worker.housekeeping_once(t + i * 60)
    assert len(runs) == 5 and len(checkpoints) == 1


# ---------------------------------------------------------------- V08 sniffed extensions
def test_v08_a_correctly_named_audio_file_keeps_its_extension(monkeypatch, tmp_path):
    """Sniffing recognises the container, not the codec: ADTS AAC shares MPEG frame sync with
    MP3 and Opus rides in Ogg, so .aac became .mp3 and .opus became .ogg in Audiobookshelf."""
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    for book, name, head, sniffed in (("Aac Book", "chapter.aac", b"\xff\xf1" + b"0" * 32, "mp3"),
                                      ("Opus Book", "chapter.opus", b"OggS" + b"0" * 32, "ogg")):
        src = tmp_path / name
        src.write_bytes(head)
        assert worker._sniff_ext(str(src)) == sniffed       # the sniff itself is unchanged
        rid = _req("alice", kind="audio", download_url="local", source="dropbox")
        worker._place_audio_file(str(src), "alice", book, rid)
        assert os.listdir(os.path.join(config.AUDIO_DIR, f"alice - {book}")) == [name], f"{name} was renamed"


def test_v08_an_unrelated_name_is_still_corrected(monkeypatch, tmp_path):
    """The rule must stay narrow: 'download.bin' and an m4b someone called .zip still get the
    extension their bytes deserve, or the folder looks like 'no audio here'."""
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    src = tmp_path / "download.bin"
    src.write_bytes(b"0000ftypM4B " + b"0" * 32)
    rid = _req("alice", kind="audio", download_url="local", source="dropbox")
    worker._place_audio_file(str(src), "alice", "Some Book", rid)
    assert os.listdir(os.path.join(config.AUDIO_DIR, "alice - Some Book")) == ["download.m4b"]


# ---------------------------------------------------------------- B09 zip bombs
def test_b09_a_zip_is_refused_on_its_entry_count_before_it_is_opened(tmp_path, monkeypatch):
    """The guard counted zin.infolist(), but ZipFile() has already materialised every ZipInfo
    by then: a 459 MB EPUB claiming 4.5 M entries OOM-killed the whole portal container."""
    p = _claim_many_entries(make_epub(str(tmp_path / "bomb.epub")))
    assert tagger.zip_entry_count(p) == 60000

    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: (opened.append(a[0]), real(*a, **k))[1])
    with pytest.raises(tagger.TagError) as e:
        tagger.add_owner_tag(p, "owner:alice")
    assert "60000 files" in str(e.value) and opened == []      # refused without opening it

    with pytest.raises(ValueError):
        worker._zip_ok(p)
    assert worker._zip_kind(p) == "oversize-zip"
    assert "more memory than the portal has" in worker._SNIFF_NAMES["oversize-zip"]


def test_b09_the_guard_covers_comics_and_the_ingest_path(tmp_path):
    cwa.add_user("alice", "alicepass1")
    cbz = _claim_many_entries(make_cbz(str(tmp_path / "bomb.cbz")))
    with pytest.raises(tagger.TagError):
        tagger.add_owner_tag_cbz(cbz, "owner:alice")
    # and an ADMIN import is refused too, rather than "tag skipped" handing it to Calibre-Web
    cwa.add_user("boss", "bosspass1", "boss@example.test", admin=True)
    epub = _claim_many_entries(make_epub(str(tmp_path / "bomb2.epub")))
    with pytest.raises(ValueError):
        worker._atomic_ingest(epub, "boss", "Bomb", "epub", 1)
    assert os.listdir(config.INGEST_DIR) == []          # and no orphan .part left behind


def test_b09_a_normal_archive_still_passes(tmp_path):
    p = make_epub(str(tmp_path / "ok.epub"))
    assert tagger.zip_entry_count(p) == 5 and tagger.precheck_zip(p) == 5
    tagger.add_owner_tag(p, "owner:alice")
    with zipfile.ZipFile(p) as z:
        assert b"owner:alice" in z.read("OEBPS/content.opf")
    assert tagger.zip_entry_count(str(tmp_path / "missing.epub")) is None      # not a zip: no opinion


# ---------------------------------------------------------------- B08 ENOSPC orphan
def test_b08_a_full_disk_leaves_no_orphan_part(monkeypatch, tmp_path):
    """shutil.copyfile sat OUTSIDE the try whose except deletes the .part, so a disk-full
    import left a full-size orphan that nothing reclaimed for 24 h — and because usage never
    dropped back under 80 %, the disk watchdog never restarted the downloaders."""
    src = make_epub(str(tmp_path / "big.epub"))

    def enospc(s, d, *a, **k):
        with open(d, "wb") as f:                 # what a real ENOSPC leaves behind
            f.write(open(s, "rb").read(512))
        raise OSError(errno.ENOSPC, "No space left on device", d)

    monkeypatch.setattr(worker.shutil, "copyfile", enospc)
    with pytest.raises(OSError):
        worker._atomic_ingest(src, "alice", "Big", "epub", 7)
    assert os.listdir(config.INGEST_DIR) == []


# ---------------------------------------------------------------- B10 IMAP timeout
def test_b10_imap_connect_cannot_hang_for_ever(monkeypatch):
    """A black-holed mail host wedged the poller inside the constructor: mail intake stopped
    silently and /healthz answered 503 for 'imap loop stale', so compose called the whole
    portal unhealthy over a mailbox."""
    import imap, imaplib
    calls = {}

    class Fake:
        def __init__(self, host, port, timeout=None):
            calls.update(host=host, port=port, timeout=timeout)

        def login(self, u, p):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Fake)
    monkeypatch.setattr(imaplib, "IMAP4", Fake)
    monkeypatch.setattr(config, "IMAP_HOST", "mail.example.test")
    monkeypatch.setattr(config, "IMAP_SSL", True)
    monkeypatch.setattr(config, "IMAP_PORT", 0)
    imap._connect()
    assert calls["timeout"] == imap.CONNECT_TIMEOUT and 0 < imap.CONNECT_TIMEOUT <= 60
    monkeypatch.setattr(config, "IMAP_SSL", False)
    imap._connect()
    assert calls["port"] == 143 and calls["timeout"] == imap.CONNECT_TIMEOUT


def test_b10_the_heartbeat_is_kept_before_the_blocking_call(monkeypatch):
    """poll_forever only beat AFTER a pass, so a slow-but-alive mailbox looked like a dead loop."""
    import imap
    from test_core import FakeImap
    worker.HEARTBEAT.pop("imap", None)
    beats = []
    monkeypatch.setattr(imap, "_connect", lambda: (beats.append(worker.HEARTBEAT.get("imap")), FakeImap([]))[1])
    imap.poll_once()
    # poll_once itself beats per message; the loop beats before connecting
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "imap.py")).read()
    body = src.split("def poll_forever")[1]
    assert body.index('_beat("imap")') < body.index("poll_once()")


# ---------------------------------------------------------------- B11 NAME_MAX
def test_b11_a_long_author_and_title_still_fit_name_max(monkeypatch, tmp_path):
    """Two independent 180-byte truncations added up to a 363-byte base; the ingest rename then
    failed with a raw [Errno 36] on every retry, and the downloaded bytes were thrown away."""
    author, title = "Ф" * 200, "Приключения " * 30
    base = worker._safe(f"{worker._safe(author)} - {worker._safe(title)}")
    assert len(base.encode()) <= worker.NAME_MAX_BYTES        # ONE budget, not two halves
    name = f"{worker._unique(base, 'alice', 12345)}.epub"
    assert len(name.encode()) <= 255 and "[alice-12345]" in name
    # and the suffix, which is what identifies the row, survives even an absurd base
    huge = worker._unique("Ф" * 400, "alice", 12345)
    assert len(f"{huge}.epub".encode()) <= 255 and huge.endswith("[alice-12345]")

    src = make_epub(str(tmp_path / "x.epub"))
    worker._atomic_ingest(src, "alice", base, "epub", 12345)       # really lands on the filesystem
    landed = os.listdir(config.INGEST_DIR)
    assert len(landed) == 1 and landed[0].endswith("[alice-12345].epub")
    assert len(landed[0].encode()) <= 255


def test_b11_a_naming_failure_is_not_a_raw_errno(monkeypatch):
    """The user saw '[Errno 36] File name too long: /ingest/<uuid>.part -> ...', internal
    container paths and all."""
    cwa.add_user("alice", "alicepass1")
    rid = _req("alice")
    monkeypatch.setattr(worker, "_download", lambda *a, **k: open(a[1], "wb").write(b"%PDF-1.4"))
    monkeypatch.setattr(worker, "_atomic_ingest",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(36, "File name too long")))
    with pytest.raises(ValueError) as e:
        worker._place_http(db.get(rid))
    assert "too long for this filesystem" in str(e.value) and "Errno" not in str(e.value)


# ---------------------------------------------------------------- B07 duplicate e-mail
def test_b07_a_shared_email_address_is_a_flash_not_a_500(client, users):
    """CWA keeps user.email UNIQUE and a family plausibly shares one address; the raw
    IntegrityError came out as HTTP 500 on /admin and a traceback in the TUI."""
    with pytest.raises(cwa.CwaError) as e:
        cwa.add_user("frank", "frankpass1", "alice@example.test")
    assert "already used by another account" in str(e.value)
    assert cwa.get_user("frank") is None

    login(client, "admin", users["admin"])
    r = post(client, "/admin", action="add_user", name="frank", email="alice@example.test",
             password="frankpass1")
    assert r.status_code == 200 and b"already used by another account" in r.data
    assert cwa.get_user("frank") is None


def test_b07_the_cli_answers_json_not_a_traceback(capsys):
    cwa.add_user("alice", "alicepass1", "alice@example.test")
    assert cwa._cli(["add-user", "frank", "--email", "alice@example.test",
                     "--password", "frankpass1"]) == 2
    out = capsys.readouterr()
    assert json.loads(out.err)["ok"] is False and "already used" in json.loads(out.err)["error"]


# ---------------------------------------------------------------- B12 / B13 / B14 ABS lifecycle
@pytest.fixture
def abs_cli(monkeypatch):
    import requests
    f = FakeABS(init=True)
    f.users.append({"id": "root-id", "username": "root", "type": "root"})
    monkeypatch.setattr(requests, "request", f)
    monkeypatch.setattr(config, "ABS_TOKEN", "apikey-test")
    return f


def test_b12_b13_b14_the_cwa_cli_reaches_audiobookshelf(abs_cli, capsys):
    """`python -m cwa` is what the module docstring tells an admin to run, and it touched only
    app.db: a removed user kept a working audio login, and a reset password still opened ABS."""
    assert cwa._cli(["add-user", "dave", "--email", "d@example.test", "--password", "davepass1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["abs"] == "created"
    assert [u["username"] for u in abs_cli.users if u["username"] == "dave"] == ["dave"]

    assert cwa._cli(["passwd", "dave", "--password", "newpass12"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["abs"] == "updated"
    assert next(u for u in abs_cli.users if u["username"] == "dave")["pw"] == "newpass12"

    assert cwa._cli(["remove-user", "dave"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["abs"] == "removed"
    assert not [u for u in abs_cli.users if u["username"] == "dave"]


def test_b12_an_unreachable_abs_is_reported_not_swallowed(abs_cli, monkeypatch, capsys):
    monkeypatch.setattr(absapi, "ensure_user",
                        lambda *a, **k: (_ for _ in ()).throw(absapi.AbsError("connection refused")))
    assert cwa._cli(["add-user", "erin", "--email", "e@example.test", "--password", "erinpass1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["abs"].startswith("NOT created")      # the CWA user still exists
    assert cwa.get_user("erin")


def test_b14_an_admin_account_needs_no_abs_user(abs_cli, capsys):
    assert cwa._cli(["add-user", "boss", "--email", "b@example.test",
                     "--password", "bosspass1", "--admin"]) == 0
    assert "abs" not in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- B01 the 200-row cap
def test_b01_an_old_approval_is_still_actionable_on_status(client, users):
    """/status derived the admin's pending and failed lists from a 200-row query, so once 200
    newer rows existed a waiting approval was unreachable from every console while /admin kept
    counting it — and 'Retry all' vanished with the section that holds it."""
    old_pending = _req("alice", title="Ancient Request", status="pending")
    old_failed = _req("alice", title="Ancient Failure", status="error")
    for i in range(210):
        _req("alice", title=f"filler {i}", status="done")
    assert old_pending not in [r["id"] for r in db.list_for("admin", True)]

    login(client, "admin", users["admin"])
    page = client.get("/status").get_data(as_text=True)
    assert "Ancient Request" in page and "Ancient Failure" in page
    assert f"/approve/{old_pending}" in page and f"/retry/{old_failed}" in page


def test_b01_a_normal_user_still_only_sees_their_own(client, users):
    _req("bob", title="Bobs Failure", status="error")
    login(client, "alice", users["alice"])
    page = client.get("/status").get_data(as_text=True)
    assert "Bobs Failure" not in page


# ---------------------------------------------------------------- B21 approve/deny races
def test_b21_approve_and_deny_are_one_transition(client, users, monkeypatch):
    """Both were check-then-act, so two admins clicking at once each sent a notification, and
    Approve racing Deny sent the requester 'was denied' AND 'is in your library'."""
    import app as appmod
    sent = []
    monkeypatch.setattr(appmod.notify, "send", lambda ev, rec: sent.append(ev))
    rid = _req("alice", status="pending")
    login(client, "admin", users["admin"])
    first = post(client, f"/approve/{rid}")
    assert b"Approved" in first.data and sent == ["approved"]
    again = post(client, f"/approve/{rid}")
    assert b"Another admin already handled" in again.data and sent == ["approved"]
    denied = post(client, f"/deny/{rid}", reason="changed my mind")
    assert b"Another admin already handled" in denied.data and sent == ["approved"]
    assert db.get(rid)["status"] == "queued"


# ---------------------------------------------------------------- B05 login lockouts
def test_b05_a_lockout_can_be_seen_and_released(capsys):
    """db.clear_login_failures only ever ran after a SUCCESSFUL login, which is exactly what a
    locked-out reader cannot produce. A family behind one NAT shares the bare-IP key, so the
    whole household (admin included) could be locked out with no console path."""
    now = time.time()
    for _ in range(config.LOCKOUT_FAILS):
        db.record_login_failure("alice", "203.0.113.9", now)
    assert db.locked_for("alice", "203.0.113.9", now) > 0
    users, ips = db.locked_keys(now)
    assert [u["user"] for u in users] == ["alice"] and users[0]["seconds"] > 0

    assert admin_cli.main(["lockout", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["users"][0]["user"] == "alice" and out["users"][0]["ip"] == "203.0.113.9"

    assert admin_cli.main(["lockout", "clear", "--user", "alice"]) == 0
    assert json.loads(capsys.readouterr().out)["cleared"] == 1
    assert db.locked_for("alice", "203.0.113.9", now) == 0


def test_b05_the_bare_ip_lock_is_released_by_address(capsys):
    now = time.time()
    for who in ("alice", "bob", "carol"):
        for _ in range(config.LOCKOUT_IP_FAILS):
            db.record_login_failure(who, "198.51.100.4", now)
    _users, ips = db.locked_keys(now)
    assert [i["ip"] for i in ips] == ["198.51.100.4"]
    assert admin_cli.main(["lockout", "clear", "--ip", "198.51.100.4"]) == 0
    assert json.loads(capsys.readouterr().out)["cleared"] >= 1
    assert db.locked_for("alice", "198.51.100.4", now) == 0
    assert db.locked_keys(now) == ([], [])


def test_b05_a_user_name_with_a_like_wildcard_releases_only_itself():
    """'_' and '%' are legal in a CWA user name and are LIKE wildcards."""
    now = time.time()
    for who in ("a_b", "axb"):
        for _ in range(config.LOCKOUT_FAILS):
            db.record_login_failure(who, "203.0.113.1", now)
    assert db.clear_login_failures_for(user="a_b") == 1
    assert db.locked_for("axb", "203.0.113.1", now) > 0


# ---------------------------------------------------------------- admin_cli: requests
def test_admin_cli_requests_list_is_not_capped_at_200(capsys):
    oldest = _req("alice", title="Ancient Request", status="pending")
    for i in range(250):
        _req("alice", title=f"filler {i}", status="done")
    assert admin_cli.main(["requests", "list", "--status", "pending"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["total"] == 1 and out["rows"][0]["rid"] == oldest
    assert out["rows"][0]["user"] == "alice" and out["rows"][0]["title"] == "Ancient Request"

    assert admin_cli.main(["requests", "list", "--limit", "10", "--offset", "245"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["total"] == 251 and len(page["rows"]) == 6 and page["rows"][-1]["rid"] == oldest


def test_admin_cli_requests_retry_and_dismiss(capsys):
    bad = _req("alice", title="Dead", status="error")
    local = _req("alice", title="Upload", source="dropbox", download_url="local", status="error")
    pending = _req("alice", title="Waiting", status="pending")

    assert admin_cli.main(["requests", "retry", str(bad)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert db.get(bad)["status"] == "queued"

    assert admin_cli.main(["requests", "retry", str(local)]) == 1
    assert "no re-fetchable source" in json.loads(capsys.readouterr().out)["error"]

    # a stranded pending row (its owner was removed) must be clearable without a fake denial
    assert admin_cli.main(["requests", "dismiss", str(pending)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert db.get(pending)["status"] == "dismissed"

    assert admin_cli.main(["requests", "retry", "99999"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ---------------------------------------------------------------- B04 parked files
def _park_a_file(user="alice", name="big.epub"):
    d = os.path.join(config.DROPBOX_DIR, user, ".failed")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    open(p, "wb").write(b"x" * 100)
    rid = _req(user, title=name, source="dropbox", download_url="local", status="error")
    db.set_status(rid, "error", f"file is 600 MB; the limit is 500 MB "
                                f"(moved to dropbox/{user}/.failed/{name})")
    return p


def test_b04_parked_files_can_be_listed_retried_and_deleted(capsys):
    """_park() moved every unimportable file to <dropbox>/.failed/ and nothing ever surfaced it
    again: no TUI entry, no dashboard panel, SSH plus mv/rm the only route."""
    cwa.add_user("alice", "alicepass1")
    _park_a_file()
    assert admin_cli.main(["parked", "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    row = out["rows"][0]
    assert row["user"] == "alice" and row["name"] == "big.epub" and row["bytes"] == 100
    assert "the limit is 500 MB" in row["reason"] and "moved to" not in row["reason"]

    assert admin_cli.main(["parked", "retry", row["token"]]) == 0
    moved = json.loads(capsys.readouterr().out)["moved"]
    assert moved.endswith("dropbox/alice/big.epub") and os.path.exists(moved)
    assert os.listdir(os.path.join(config.DROPBOX_DIR, "alice", ".failed")) == []

    p = _park_a_file(name="other.epub")
    assert admin_cli.main(["parked", "list"]) == 0
    tok = json.loads(capsys.readouterr().out)["rows"][0]["token"]
    assert admin_cli.main(["parked", "delete", tok]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True} and not os.path.exists(p)


def test_b04_a_parked_token_can_never_escape_the_dropbox_root(capsys, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("x")
    for rel in ("../../etc/passwd", "/etc/passwd", "alice/book.epub", "..",
                str(outside), "alice/.failed/../../../etc/passwd"):
        tok = base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")
        with pytest.raises(ValueError):
            admin_cli.resolve_token(tok)
    with pytest.raises(ValueError):                       # not even valid UTF-8 behind the id
        admin_cli.resolve_token(base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="))
    escape = base64.urlsafe_b64encode(b"../../etc/passwd").decode().rstrip("=")
    assert admin_cli.main(["parked", "delete", escape]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert outside.exists()


def test_b04_a_symlink_under_failed_is_never_followed(tmp_path):
    cwa.add_user("alice", "alicepass1")
    d = os.path.join(config.DROPBOX_DIR, "alice", ".failed")
    os.makedirs(d, exist_ok=True)
    target = tmp_path / "keepme"
    target.write_text("x")
    os.symlink(str(target), os.path.join(d, "link.epub"))
    rows = admin_cli.parked_rows()
    with pytest.raises(ValueError):
        admin_cli.resolve_token(rows[0]["token"])
    assert target.exists()


def test_b04_retry_does_not_clobber_a_live_dropbox_file(capsys):
    cwa.add_user("alice", "alicepass1")
    _park_a_file()
    live = os.path.join(config.DROPBOX_DIR, "alice", "big.epub")
    open(live, "wb").write(b"the good copy")
    rows = admin_cli.parked_rows()
    with pytest.raises(ValueError):
        admin_cli.parked_retry(rows[0]["token"])
    assert open(live, "rb").read() == b"the good copy"


# ---------------------------------------------------------------- B06 Shelfmark wording
def test_b06_a_password_change_no_longer_claims_shelfmark_is_covered(client, users):
    """Shelfmark signs its own cookie and only checks app.db at login, so whoever holds the old
    cookie keeps reading until the container is restarted — an admin action."""
    login(client, "alice", users["alice"])
    r = post(client, "/devices", action="password", current=users["alice"],
             new="newpass123", repeat="newpass123")
    body = r.get_data(as_text=True)
    assert "Password changed for the portal and the library." in body
    assert "the library and Shelfmark" not in body
    assert "restart Shelfmark" in body


# ---------------------------------------------------------------- B17 bogus interrupted rows
def test_b17_an_interrupted_row_is_closed_when_the_file_did_land(monkeypatch):
    """A kill between the ingest rename and _finish left a permanent red 'interrupted by
    restart' for a book the reader can already see, and it is not retryable."""
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(worker.notify, "alert", lambda *a, **k: None)
    rid = _req("alice", source="dropbox", download_url="local", status="importing")
    db.recover_on_start()
    assert db.get(rid)["status"] == "error" and db.get(rid)["detail"] == db.INTERRUPTED

    add_calibre_book(51, f"book [alice-{rid}]", "Someone", tags=("owner:alice",))
    assert worker.reconcile_imports(time.time() + 1000) == 1
    assert db.get(rid)["status"] == "done"

    # a row whose file really never landed keeps its honest failure
    rid2 = _req("alice", source="dropbox", download_url="local", status="importing")
    db.recover_on_start()
    assert worker.reconcile_imports(time.time() + 1000) == 0
    assert db.get(rid2)["status"] == "error"


# ---------------------------------------------------------------- B22 Ephemera link
def test_b22_the_ephemera_link_is_gated_on_the_feature(monkeypatch):
    """The Applications card always offered an ephemera.<domain> link, but render_caddyfile
    drops the vhost and step_cloudflare creates no DNS record unless it is on."""
    monkeypatch.setattr(config, "EPHEMERA_ENABLED", False)
    assert not [x for x in config.admin_links() if "ephemera" in x[1]]
    monkeypatch.setattr(config, "EPHEMERA_ENABLED", True)
    assert [x for x in config.admin_links() if "ephemera" in x[1]]


# ---------------------------------------------------------------- B20 Shelfmark WAL window
def test_b20_a_drift_detection_checkpoints_immediately(monkeypatch):
    """A password changed in Calibre-Web's own UI sat in the WAL, which Shelfmark (immutable=1)
    cannot read, until the next 5-minute passive checkpoint: the OLD password kept opening
    Shelfmark for up to five minutes."""
    cwa.add_user("alice", "alicepass1")
    monkeypatch.setattr(worker.notify, "alert", lambda *a, **k: None)
    monkeypatch.setattr(worker.absapi, "configured", lambda: True)
    worker.check_password_drift()                      # first sight: remember, do not alert
    checkpoints = []
    monkeypatch.setattr(worker.cwa, "_checkpoint", lambda: checkpoints.append(1))
    assert worker.check_password_drift() == 0 and checkpoints == []
    # what a change in Calibre-Web's OWN UI looks like: app.db moved, nothing checkpointed it
    db.set_pw_fingerprint("alice", "stale-fingerprint")
    assert worker.check_password_drift() == 1 and checkpoints == [1]
