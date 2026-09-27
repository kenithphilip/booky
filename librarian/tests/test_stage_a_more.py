"""Stage A remainder: evidence the sources already give, now kept."""
import db, worker, wanted, fetchers


OPF = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>Emma</dc:title><dc:creator>Jane Austen</dc:creator><dc:language>en</dc:language>
<dc:identifier opf:scheme="ISBN">9780141439587</dc:identifier>
<dc:identifier opf:scheme="calibre">1f2e</dc:identifier></metadata></package>"""


def test_a_calibre_sidecar_is_read_not_thrown_away(tmp_path):
    d = tmp_path / "Jane Austen" / "Emma (12)"
    d.mkdir(parents=True)
    (d / "metadata.opf").write_text(OPF)
    book = d / "Emma - Jane Austen.mobi"
    book.write_bytes(b"x")
    got = worker._opf_sidecar(str(book))
    assert got["title"] == "Emma" and got["author"] == "Jane Austen"
    assert {"kind": "isbn", "value": "9780141439587"} in got["identifiers"]


def test_the_sidecar_only_fills_gaps(users):
    rid = db.add("alice", {"kind": "ebook", "source": "dropbox", "title": "file", "download_url": "local"})
    db.set_file_meta(rid, {"title": "Emma (from the file)", "author": "", "identifiers": [{"kind": "uuid", "value": "u1"}]})
    db.merge_file_meta(rid, {"title": "Emma", "author": "Jane Austen", "language": "en",
                             "identifiers": [{"kind": "isbn", "value": "9780141439587"}]})
    r = db.get(rid)
    assert r["file_title"] == "Emma (from the file)", "the file's own word wins"
    assert r["file_author"] == "Jane Austen" and r["file_language"] == "en"
    assert {"kind": "isbn", "value": "9780141439587"} in db.file_ids(rid) and {"kind": "uuid", "value": "u1"} in db.file_ids(rid)


def test_a_symlinked_sidecar_is_ignored(tmp_path):
    (tmp_path / "evil.opf").write_text(OPF)
    (tmp_path / "metadata.opf").symlink_to(tmp_path / "evil.opf")
    (tmp_path / "b.mobi").write_bytes(b"x")
    assert worker._opf_sidecar(str(tmp_path / "b.mobi")) is None


def test_librivox_results_carry_language_and_length(monkeypatch):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"books": [{"id": "253", "title": "Pride and Prejudice", "language": "English",
                               "totaltimesecs": "47204", "num_sections": "37",
                               "url_zip_file": "https://www.archive.org/download/x/x.zip",
                               "authors": [{"last_name": "Austen"}]}]}
    monkeypatch.setattr(fetchers, "_get", lambda *a, **k: R())
    (r,) = fetchers.librivox("pride")
    assert r["language"] == "English" and r["duration_seconds"] == 47204 and r["part_count"] == 37
    assert r["detail"] == "LibriVox, 13 h 6 min"


def test_keep_looking_never_takes_another_language():
    want = {"kind": "audio", "title": "Pride and Prejudice", "author": "Jane Austen", "language": "en"}
    res = {"source": "librivox", "title": "Pride and Prejudice", "author": "Austen", "language": "German",
           "download_url": "https://archive.org/x.zip"}
    assert wanted.score(want, res) is None
    assert wanted.score(want, dict(res, language="English"))
