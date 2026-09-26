"""Stage A: verify a download against what the source published.

The half Readarr got wrong. Its scorer treated a MISSING identifier as weak evidence of a
match (0.1) against a WRONG one (10.0), so absent evidence read as confidence and a sample
file could satisfy a request for a whole book. Here, absent evidence produces no verdict —
only evidence that is present and disagrees produces a reject.
"""
import hashlib
import db, worker


def _file(tmp_path, data=b"a real book, honest"):
    p = tmp_path / "download.bin"
    p.write_bytes(data)
    return str(p)


def test_nothing_to_check_is_not_a_failure(tmp_path):
    """Most sources publish no hash. Absent evidence must never look like a mismatch."""
    assert worker.verify_download(_file(tmp_path), {}) == []
    assert worker.verify_download(_file(tmp_path), {"title": "T"}) == []


def test_a_matching_size_and_hash_pass(tmp_path):
    data = b"the whole book"
    p = _file(tmp_path, data)
    req = {"expect_size": len(data), "expect_sha1": hashlib.sha1(data).hexdigest().upper()}
    assert worker.verify_download(p, req) == []          # case-insensitive on purpose


def test_a_truncated_download_is_caught_by_size(tmp_path):
    """The check that catches a 40 KB sample importing as a 452-page book."""
    p = _file(tmp_path, b"short")
    reasons = worker.verify_download(p, {"expect_size": 4_200_000})
    assert reasons and "size is 5 bytes" in reasons[0]


def test_a_corrupted_download_is_caught_by_hash(tmp_path):
    p = _file(tmp_path, b"not what was advertised")
    reasons = worker.verify_download(p, {"expect_sha1": hashlib.sha1(b"the real one").hexdigest()})
    assert reasons == ["sha1 does not match what the source published"]


def test_sha1_is_preferred_and_only_one_hash_is_computed(tmp_path, monkeypatch):
    """A multi-GB audiobook should not be hashed twice on a 2-core box."""
    data = b"x" * 1024
    p = _file(tmp_path, data)
    req = {"expect_sha1": hashlib.sha1(data).hexdigest(),
           "expect_md5": "deliberately-wrong-and-never-reached"}
    assert worker.verify_download(p, req) == []


def test_an_unreadable_file_is_reported_not_silently_passed(tmp_path):
    assert worker.verify_download(str(tmp_path / "missing.bin"), {"expect_size": 10}) == \
        ["the downloaded file could not be read"]


def test_a_mismatch_parks_the_request_for_a_human(monkeypatch, users, tmp_path):
    """Not retried, not discarded: 'we got something different from what was advertised' is a
    decision for a person."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    data = b"wrong file"
    rid = db.add("alice", {"kind": "ebook", "source": "internet_archive", "title": "Moby-Dick",
                           "author": "Melville", "download_url": "https://archive.org/x"},
                 status="queued")
    req = dict(db.get(rid))
    req["expect_size"] = 999999

    monkeypatch.setattr(worker, "_download",
                        lambda url, dest, req=None, rid=None, attempts=3: open(dest, "wb").write(data))
    monkeypatch.setattr(worker, "_tmpdir", lambda: str(tmp_path))
    worker._place_http(req)

    row = db.get(rid)
    assert row["status"] == "needs-review"
    assert "does not match what the source published" in row["detail"]
    assert "size is 10 bytes" in row["detail"]
    assert [a for a in db.audit_recent(20) if a["event"] == "download_mismatch"]
