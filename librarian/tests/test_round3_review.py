"""Regressions found reviewing the round-3 fixes themselves.

Two of these lock down mistakes the round-3 fixes introduced (the audiobook-zip placer and the
ingest zip precheck): a fix that quietly breaks a path that used to work is the failure mode
this round kept hitting, so each one reproduces the broken behaviour, not just the new code.
"""
import os, time, zipfile
import config, db, cwa, tagger, worker


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _aged(path):
    old = time.time() - 3600
    for root, _d, names in os.walk(path):
        for n in names:
            os.utime(os.path.join(root, n), (old, old))
    os.utime(path, (old, old))


# ---------------------------------------------------------------- the disk-full pause flag
def test_a_full_disk_stops_the_queue(monkeypatch):
    """disk-watch.sh raises the flag at DISK_STOP_PCT and the alert, README, self-test and
    Advanced settings all tell the admin imports stop. Nothing read it, so they did not."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    cwa.add_user("zoe", "zoepass1")
    db.add("zoe", {"kind": "ebook", "source": "gutenberg", "title": "T", "author": "A",
                   "download_url": "https://x/y.epub"}, status="queued")
    flag = os.path.join(config.STAGING_DIR, worker.DISK_PAUSE_FLAG)
    os.makedirs(config.STAGING_DIR, exist_ok=True)
    open(flag, "w").close()
    try:
        assert worker.queue_once() is False
        assert db.rows_by_status(("queued",))          # still queued, not failed or lost
    finally:
        os.remove(flag)


def test_a_full_disk_stops_the_dropbox_watcher(monkeypatch):
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    cwa.add_user("zoe", "zoepass1")
    box = os.path.join(config.DROPBOX_DIR, "zoe")
    os.makedirs(box, exist_ok=True)
    from conftest import make_epub
    make_epub(os.path.join(box, "book.epub"))
    _aged(box)
    flag = os.path.join(config.STAGING_DIR, worker.DISK_PAUSE_FLAG)
    os.makedirs(config.STAGING_DIR, exist_ok=True)
    open(flag, "w").close()
    try:
        assert worker.scan_dropbox_once() == 0
        assert os.path.exists(os.path.join(box, "book.epub"))   # left where the reader put it
    finally:
        os.remove(flag)
    assert worker.scan_dropbox_once() == 1                      # and picked up once it clears


# ---------------------------------------------------------------- the audiobook-zip placer
def test_a_failed_audiobook_import_never_eats_the_readers_archive(monkeypatch):
    """The round-3 V03 fix unpacked and deleted the zip inside the LIVE dropbox folder before
    the duplicate check. Any later failure destroyed the only copy the reader had — and because
    the folder had been fingerprinted first, the mutation made _changed_since() true, so it was
    reported as 'still being written' and never parked."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    cwa.add_user("zoe", "zoepass1")
    box = os.path.join(config.DROPBOX_DIR, "zoe")
    folder = os.path.join(box, "The Hobbit")
    os.makedirs(folder)
    _zip(os.path.join(folder, "hobbit.zip"), {"01.mp3": b"ID3" + b"0" * 32})
    _aged(folder)
    before = worker._fingerprint(folder)

    monkeypatch.setattr(worker, "_audio_duplicate", lambda *a, **k: True)   # fail after expansion
    assert worker.scan_dropbox_once() == 1

    parked = os.path.join(box, ".failed", "The Hobbit")
    assert os.path.isdir(parked), "the folder must be parked, not left with a false reason"
    assert os.path.exists(os.path.join(parked, "hobbit.zip")), "the reader's archive must survive"
    rows = db.rows_by_status(("error",))
    assert rows and "still being written" not in (rows[0]["detail"] or "")
    assert "already in your audiobooks" in (rows[0]["detail"] or "")
    assert worker._fingerprint(parked) == before        # byte-identical to what was dropped


def test_a_successful_audiobook_import_still_unpacks_and_drops_the_zip(monkeypatch):
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    cwa.add_user("zoe", "zoepass1")
    folder = os.path.join(config.DROPBOX_DIR, "zoe", "The Hobbit")
    os.makedirs(folder)
    _zip(os.path.join(folder, "hobbit.zip"), {"01.mp3": b"ID3" + b"0" * 32})
    _aged(folder)
    assert worker.scan_dropbox_once() == 1
    final = os.path.join(config.AUDIO_DIR, "zoe - The Hobbit")
    assert os.listdir(final) == ["01.mp3"]
    assert db.rows_by_status(("error",)) == []


# ---------------------------------------------------------------- the ingest zip precheck
def test_a_big_but_legitimate_comic_still_imports(tmp_path):
    """The ingest precheck was given the audiobook limit (2000), which is stricter than the
    tagger guard it front-runs (5000). Image-heavy EPUBs and large CBZs that imported before
    were refused with a reason that was not true."""
    assert worker.MAX_ZIP_MEMBERS < tagger.MAX_ZIP_MEMBERS
    cbz = tmp_path / "big.cbz"
    _zip(str(cbz), {f"{i:05d}.jpg": b"\xff\xd8\xff" + bytes(16) for i in
                    range(worker.MAX_ZIP_MEMBERS + 50)})
    worker._zip_ok(str(cbz), "this CBZ", tagger.MAX_ZIP_MEMBERS)      # the ingest path: allowed

    try:
        worker._zip_ok(str(cbz), "this archive")                      # audiobook path: refused
        raise AssertionError("the audiobook limit must stay the tighter one")
    except ValueError as e:
        assert str(worker.MAX_ZIP_MEMBERS) in str(e)


# ---------------------------------------------------------------- username shape
def test_a_username_that_would_get_a_dead_dropbox_is_refused_everywhere():
    """A leading dot passed _valid_name, so the account was created complete while the watcher
    (which skips dot-directories) never scanned its dropbox: uploads were confirmed and lost."""
    for bad in (".kim", "..", ".", "kim/../x", "kim smith", "kím", "x" * 33, ""):
        assert not cwa._valid_name(bad), bad
    for good in ("kim", "kim.smith", "kim_smith-2", "Alice", "x"):
        assert cwa._valid_name(good), good


def test_a_capital_is_a_shape_the_watcher_accepts_but_not_a_new_account():
    """_valid_name is shape-only on purpose: an existing CWA account may carry a capital that
    the dropbox watcher maps back to the real user. Lowercase is enforced only at creation."""
    assert cwa._valid_name("Alice")
    try:
        cwa._check_name("Alice")
        raise AssertionError("a new account must still be refused a capital")
    except cwa.CwaError:
        pass


# ---------------------------------------------------------------- stranded pending requests
def test_a_pending_request_whose_owner_was_removed_is_failed_and_dismissable(monkeypatch):
    """A pending row is never claimed, so _owner_gone never saw it: it stayed for ever, and an
    admin could only 'deny' a person who no longer existed."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    cwa.add_user("zoe", "zoepass1")
    rid = db.add("zoe", {"kind": "ebook", "source": "gutenberg", "title": "T", "author": "A",
                         "download_url": "https://x/y.epub"}, status="pending")
    assert worker.fail_orphaned_pending() == 0          # while the account exists, untouched
    cwa.remove_user("zoe")
    assert worker.fail_orphaned_pending() == 1
    assert db.get(rid)["status"] == "error"
    assert "was removed" in db.get(rid)["detail"]


def test_an_admin_list_is_not_silently_truncated():
    """rows_by_status capped at 500, so /status hid the oldest pending approval — the one most
    in need of acting on — while the TUI's own list showed it."""
    cwa.add_user("zoe", "zoepass1")
    for i in range(520):
        db.add("zoe", {"kind": "ebook", "source": "gutenberg", "title": f"T{i}", "author": "A",
                       "download_url": f"https://x/{i}.epub"}, status="pending")
    assert len(db.rows_by_status(("pending",))) == 520
    assert len(db.rows_by_status(("pending",), limit=10)) == 10
