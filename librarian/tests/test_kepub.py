"""L11: real KEPUB downloads, converted by the pinned kepubify in the portal image."""
import os, zipfile
import pytest
import config, library
from conftest import add_calibre_book, login, make_epub

needs_kepubify = pytest.mark.skipif(not config.KEPUBIFY, reason="kepubify is only in the portal image")


def _book(tmp_owner="alice"):
    add_calibre_book(1, "Emma", "Jane Austen", tags=[f"owner:{tmp_owner}"], formats=("epub",))
    d = os.path.join(config.LIBRARY_DIR, "Jane Austen", "Emma (1)")
    os.makedirs(d, exist_ok=True)
    make_epub(os.path.join(d, "Emma - Jane Austen.epub"), title="Emma", author="Jane Austen")


@needs_kepubify
def test_a_kobo_reader_downloads_a_real_kepub(client, users, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEPUB_CACHE_DIR", str(tmp_path / "kepub"))
    _book()
    login(client, "alice", users["alice"])
    r = client.get("/download/1/kepub")
    assert r.status_code == 200 and 'Emma - Jane Austen.kepub.epub' in r.headers["Content-Disposition"]
    p = tmp_path / "got.kepub.epub"; p.write_bytes(r.data)
    z = zipfile.ZipFile(p)
    html = b"".join(z.read(n) for n in z.namelist() if n.endswith((".xhtml", ".html")))
    assert b"koboSpan" in html, "kepubify's span markup is what gives exact page tracking"
    assert len(os.listdir(tmp_path / "kepub")) == 1
    assert client.get("/download/1/kepub").status_code == 200 and len(os.listdir(tmp_path / "kepub")) == 1, "cached"


@needs_kepubify
def test_a_sibling_cannot_convert_or_download_it(client, users, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEPUB_CACHE_DIR", str(tmp_path / "kepub"))
    _book("alice")
    login(client, "bob", users["bob"])
    assert client.get("/download/1/kepub").status_code == 404
    assert not (tmp_path / "kepub").exists() or not os.listdir(tmp_path / "kepub")


@needs_kepubify
def test_kepub_is_a_preference_met_from_the_epub(monkeypatch):
    assert "kepub" in config.FORMATS
    assert library.best_format({"formats": ["epub", "pdf"]}, "kepub") == "kepub"
    assert library.best_format({"formats": ["pdf"]}, "kepub") == "pdf"


def test_the_cache_is_trimmed_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KEPUB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "KEPUB_CACHE_MB", 1)
    for i in range(3):
        f = tmp_path / f"{i}-1.kepub.epub"; f.write_bytes(b"x" * 600_000); os.utime(f, (i, i))
    library._trim_kepub_cache()
    assert sorted(os.listdir(tmp_path)) == ["2-1.kepub.epub"]
