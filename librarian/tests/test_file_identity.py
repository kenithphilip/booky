"""Stage B: identify an arrival by what the FILE says, not by its filename.

Every intake path except the portal's own adapters (Shelfmark, qBittorrent, the dropbox,
e-mail) hands the worker a file and nothing else. The portal recorded `title` = the raw
filename and `author` = "" — so dedupe could not fire, the request history showed release
names, and an arrival could never be tied to the request that caused it. tagger.py already
had the OPF parsed at tag time and was discarding it.
"""
import json, os, zipfile
import config, db, worker, tagger


def _epub_with_metadata(path, title, author, language="en", ident=None, scheme=None):
    """A minimal but real EPUB whose OPF carries proper Dublin Core metadata."""
    idline = ""
    if ident:
        attr = f' opf:scheme="{scheme}"' if scheme else ""
        idline = f'<dc:identifier id="pub-id"{attr}>{ident}</dc:identifier>'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="pub-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:opf="http://www.idpf.org/2007/opf">'
           f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
           f'<dc:language>{language}</dc:language>{idline}'
           '</metadata><manifest/><spine/></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf", opf)
    return path


def test_the_tagger_hands_back_what_the_file_says(tmp_path):
    p = _epub_with_metadata(str(tmp_path / "x.epub"), "Moby-Dick", "Herman Melville",
                            ident="urn:isbn:9780142437247")
    found = tagger.add_owner_tag(p, "owner:alice")
    assert found["title"] == "Moby-Dick"
    assert found["author"] == "Herman Melville"
    assert found["language"] == "en"
    assert {"kind": "isbn", "value": "9780142437247"} in found["identifiers"]
    # and it still did its real job
    with zipfile.ZipFile(p) as z:
        assert "owner:alice" in z.read("content.opf").decode()


def test_a_calibre_uuid_is_captured_too(tmp_path):
    """An EPUB downloaded from this library carries the household's own Calibre UUID — the
    highest-confidence identifier available anywhere in the adapter set."""
    p = _epub_with_metadata(str(tmp_path / "y.epub"), "T", "A",
                            ident="urn:uuid:8f1c-dead-beef", scheme=None)
    found = tagger.add_owner_tag(p, "owner:bob")
    assert found["identifiers"] == [{"kind": "uuid", "value": "8f1c-dead-beef"}]


def test_a_dropbox_arrival_is_identified_by_its_contents_not_its_filename(monkeypatch, users):
    """The filename is a release name; the OPF is the book. Both get recorded, and the row
    keeps the filename as `title` (that is what the reader dropped) while `file_title` carries
    the truth that dedupe and matching need."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    box = os.path.join(config.DROPBOX_DIR, "alice")
    os.makedirs(box, exist_ok=True)
    name = "Melville.Moby.Dick.1851.RETAIL.EPUB-XYZ.epub"
    _epub_with_metadata(os.path.join(box, name), "Moby-Dick", "Herman Melville",
                        ident="urn:isbn:9780142437247")
    old = os.path.getmtime(os.path.join(box, name)) - 3600
    os.utime(os.path.join(box, name), (old, old))

    assert worker.scan_dropbox_once() == 1
    row = db.rows_by_status(("done", worker.NEEDS_TAG, "error"))[0]
    assert row["file_title"] == "Moby-Dick"
    assert row["file_author"] == "Herman Melville"
    assert row["file_language"] == "en"
    assert {"kind": "isbn", "value": "9780142437247"} in json.loads(row["file_ids"])
    assert db.file_ids(row["id"])                      # and through the accessor


def test_a_file_with_no_metadata_records_nothing_rather_than_guessing(monkeypatch, users):
    """An EPUB with an empty OPF must leave the columns NULL. Writing the filename into
    file_title would make a guess indistinguishable from evidence."""
    monkeypatch.setattr(worker.notify, "send", lambda *a, **k: None)
    box = os.path.join(config.DROPBOX_DIR, "alice")
    os.makedirs(box, exist_ok=True)
    p = os.path.join(box, "nameless.epub")
    with zipfile.ZipFile(p, "w") as z:                 # a real EPUB whose OPF says nothing
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"/>'
                   '<manifest/><spine/></package>')
    old = os.path.getmtime(p) - 3600
    os.utime(p, (old, old))
    assert worker.scan_dropbox_once() == 1
    row = db.rows_by_status(("done", worker.NEEDS_TAG, "error"))[0]
    assert not row["file_author"]
    assert db.file_ids(row["id"]) == []
