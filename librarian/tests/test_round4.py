"""Round-4 regressions.

Three of these lock down things that were measured DEAD on the live services, not merely
imperfect: both default ebook search sources returned zero results for ordinary queries, and
/cover re-fetched the same image from Open Library on every page view. Each test reproduces
the broken behaviour (the mirror that never answers, the unanchored LibriVox title, the serial
N+1, the missing cache) rather than only exercising the replacement.
"""
import os, time
import config, fetchers, opds
from conftest import login


# ---------------------------------------------------------------- item 1: Kobo gets EPUB
def test_the_devices_page_no_longer_promises_kobo_a_kepub(client, users):
    """CWA v4.0.6 autodetects kepubify only at /opt/kepubify/kepubify-linux-{64,32}bit and its
    image installs it at /usr/bin/kepubify, so config_kepubifypath is permanently empty and
    cps/kobo.py never converts. The page said the opposite."""
    login(client, "alice", users["alice"])
    html = client.get("/devices").get_data(as_text=True)
    assert "KEPUB" not in html
    assert "Kobo receives EPUB over sync" in html and "chapter" in html
    # the download side is untouched: a KEPUB is still served if one ever exists
    assert "kepub" in config.DOWNLOAD_FORMATS and "kepub" not in config.FORMATS


# ---------------------------------------------------------------- item 3: the search sources
PG_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>https://www.gutenberg.org/ebooks/authors/search.opds/?query=dickens</id>
 <title>Authors</title><content type="text">5 author names match your search.</content></entry>
<entry><id>https://www.gutenberg.org/ebooks/98.opds</id>
 <title>A Tale of Two Cities</title><content type="text">Charles Dickens</content></entry>
<entry><id>https://www.gutenberg.org/ebooks/564.opds</id>
 <title>The Mystery of Edwin Drood</title><content type="text">Charles Dickens</content></entry>
</feed>"""


class _Resp:
    def __init__(self, data=None, content=b"", status=200):
        self._d, self.content, self.status_code = data, content, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(str(self.status_code))


def test_gutenberg_asks_gutenberg_and_not_the_dead_mirror(monkeypatch):
    """gutendex.com is a third-party mirror now behind a Cloudflare challenge: 5/5 timeouts at
    TIMEOUT=8 and 3/3 at 12 s, i.e. the default ebook source returned nothing, always."""
    seen = {}
    monkeypatch.setattr(fetchers, "_get",
                        lambda url, **kw: (seen.update(url=url, params=kw.get("params")), _Resp(content=PG_FEED))[1])
    out = fetchers.gutenberg("dickens")
    assert "gutendex" not in seen["url"] and seen["url"].startswith("https://www.gutenberg.org/")
    assert seen["params"] == {"query": "dickens"}
    # the two navigation entries at the top of every Gutenberg result are not books
    assert [r["title"] for r in out] == ["A Tale of Two Cities", "The Mystery of Edwin Drood"]
    assert out[0]["author"] == "Charles Dickens" and out[0]["identifier"] == "gutenberg:98"
    assert out[0]["download_url"] == "https://www.gutenberg.org/ebooks/98.epub3.images"
    assert all(fetchers.url_allowed("gutenberg", r["download_url"]) for r in out)


def test_gutenberg_still_honours_a_local_mirror(monkeypatch):
    monkeypatch.setattr(fetchers, "_get", lambda url, **kw: _Resp(content=PG_FEED))
    monkeypatch.setattr(config, "GUTENBERG_MIRROR", "https://mirror.local/")
    out = fetchers.gutenberg("dickens")
    assert out[0]["download_url"] == "https://mirror.local/ebooks/98.epub3.images"
    assert fetchers.url_allowed("gutenberg", out[0]["download_url"])


def test_gutenberg_survives_a_feed_it_cannot_parse(monkeypatch):
    monkeypatch.setattr(fetchers, "_get", lambda url, **kw: _Resp(content=b"<html>challenge</html>"))
    assert fetchers.gutenberg("dickens") == []


LV_BOOK = {"id": "753", "title": "Moby Dick, or the Whale", "authors": [{"last_name": "Melville"}],
           "url_zip_file": "https://archive.org/compress/moby_dick_librivox/formats=64KBPS MP3&file=/x.zip"}


def test_librivox_anchors_the_title_and_drops_the_extended_feed(monkeypatch):
    """title=moby is HTTP 404 and title=^moby finds Moby Dick (verified 5/5 both ways), so
    every ordinary query came back empty. extended=1 costs 2.5-7.5 s against 0.5-1.0 s and
    carries only a chapter listing nothing here reads."""
    seen = []

    def fake(url, **kw):
        seen.append(kw.get("params"))
        return _Resp({"books": [LV_BOOK]})

    monkeypatch.setattr(fetchers, "_get", fake)
    out = fetchers.librivox("moby")
    assert seen == [{"title": "^moby", "format": "json"}]
    assert "extended" not in seen[0]
    assert out[0]["title"] == "Moby Dick, or the Whale" and out[0]["author"] == "Melville"
    assert fetchers.url_allowed("librivox", out[0]["download_url"])
    # a query already anchored by the user is not double-anchored
    seen.clear(); fetchers.librivox("  ^moby ")
    assert seen[0]["title"] == "^moby"


def test_librivox_tries_the_author_when_the_title_finds_nothing(monkeypatch):
    """LibriVox says 'no such title' with a 404. 'dickens' is an author, not a title: it 404s
    on the title filter with and without the anchor, and used to end the search there."""
    seen = []

    def fake(url, **kw):
        seen.append(kw.get("params"))
        return _Resp({"error": "no results"}, status=404) if len(seen) == 1 else _Resp({"books": [LV_BOOK]})

    monkeypatch.setattr(fetchers, "_get", fake)
    out = fetchers.librivox("dickens")
    assert [p.get("title") or p.get("author") for p in seen] == ["^dickens", "dickens"]
    assert len(out) == 1


def test_librivox_stops_after_a_true_negative(monkeypatch):
    """'hobbit' legitimately 404s on both filters (still in copyright) — two calls, not more,
    and no exception out of the adapter."""
    calls = []
    monkeypatch.setattr(fetchers, "_get",
                        lambda url, **kw: (calls.append(1), _Resp({"error": "x"}, status=404))[1])
    assert fetchers.librivox("hobbit") == [] and len(calls) == 2


IA_DOCS = {"response": {"docs": [{"identifier": "good", "title": "Moby-Dick", "creator": "Melville"},
                                 {"identifier": "lending", "title": "Borrowed", "creator": "X"},
                                 {"identifier": "lcponly", "title": "DRM", "creator": "Y"},
                                 {"identifier": "spacey", "title": "Reader", "creator": "Z"}]}}
IA_META = {
    "good": {"files": [{"name": "good.epub"}]},
    "lending": {"metadata": {"access-restricted-item": "true"}, "files": [{"name": "lending.epub"}]},
    "lcponly": {"files": [{"name": "lcponly_lcp.epub"}]},
    "spacey": {"files": [{"name": "level 2 - Moby Dick.epub"}]},
}


def _ia_fake(monkeypatch, delay=0.0, calls=None):
    fetchers._IA_FILES.clear()

    def fake(url, **kw):
        if "advancedsearch" in url:
            if calls is not None:
                calls.append(kw.get("params"))
            return _Resp(IA_DOCS)
        time.sleep(delay)
        ident = url.rsplit("/", 1)[1]
        if calls is not None:
            calls.append(ident)
        return _Resp(IA_META[ident])

    monkeypatch.setattr(fetchers, "_get", fake)


def test_internet_archive_asks_only_for_downloadable_epubs(monkeypatch):
    """Measured: 3/8 results had an EPUB at all, and the lending items that did answered
    401/403 to a download. Both filters are now in the query, and the two that slip through
    are dropped on the metadata."""
    calls = []
    _ia_fake(monkeypatch, calls=calls)
    out = fetchers.internet_archive("moby dick")
    q = calls[0]["q"]
    assert "format:EPUB" in q and "NOT collection:inlibrary" in q and "NOT collection:printdisabled" in q
    assert "format" in calls[0]["fl[]"] and calls[0]["rows"] > 8
    assert [r["identifier"] for r in out] == ["ia:good", "ia:spacey"]      # lending + lcp-only dropped
    # a file name with spaces has to survive the form echo and url_allowed
    assert out[1]["download_url"] == "https://archive.org/download/spacey/level%202%20-%20Moby%20Dick.epub"
    assert all(fetchers.url_allowed("internet_archive", r["download_url"]) for r in out)


def test_a_reader_typing_punctuation_does_not_break_the_archive_query(monkeypatch):
    """archive.org speaks Lucene and this adapter wraps the query in parentheses. Verified
    live: 'moby)', 'moby "dick', 'moby /dick', 'who? what!' and 'moby AND' all come back
    HTTP 200 with {"error": ...} and no "response" key, which the reader saw as a blank
    result page. Each of them returns results once the punctuation is dropped."""
    assert fetchers._ia_q('moby)') == "moby"
    assert fetchers._ia_q('moby "dick') == "moby  dick"
    assert fetchers._ia_q("who? what!") == "who  what"
    assert fetchers._ia_q("moby AND") == "moby"
    assert fetchers._ia_q("Moby-Dick: or, The Whale") == "Moby Dick  or, The Whale"
    # Lucene's operators are the UPPERCASE words only, so ordinary titles keep their wording
    assert fetchers._ia_q("To Kill a Mockingbird") == "To Kill a Mockingbird"
    calls = []
    _ia_fake(monkeypatch, calls=calls)
    fetchers.internet_archive('moby)')
    assert calls[0]["q"].startswith("(moby) AND")


def test_internet_archive_fetches_the_metadata_in_parallel(monkeypatch):
    """The N+1: one search plus one /metadata GET per hit, run one after the other. Measured
    live on eight identifiers: 15.3 s serial against 1.5 s together."""
    _ia_fake(monkeypatch, delay=0.4)
    t = time.monotonic()
    assert len(fetchers.internet_archive("moby dick")) == 2
    elapsed = time.monotonic() - t
    assert elapsed < 4 * 0.4 * 0.8, f"metadata still looks serial ({elapsed:.2f}s for 4 x 0.4s)"


def test_internet_archive_remembers_a_file_name_between_searches(monkeypatch):
    """archive.org throttles a burst of /metadata calls (measured: the search leg of the same
    query went 0.9 s -> 4 s -> 8 s -> read timeout). Identifiers repeat across searches."""
    calls = []
    _ia_fake(monkeypatch, calls=calls)
    fetchers.internet_archive("moby dick")
    assert sorted(c for c in calls if isinstance(c, str)) == ["good", "lcponly", "lending", "spacey"]
    calls.clear()
    assert len(fetchers.internet_archive("moby dick")) == 2
    assert [c for c in calls if isinstance(c, str)] == []          # search only, no metadata


def test_internet_archive_does_not_remember_a_call_that_never_answered(monkeypatch):
    fetchers._IA_FILES.clear()

    def fake(url, **kw):
        if "advancedsearch" in url:
            return _Resp(IA_DOCS)
        raise IOError("timed out")

    monkeypatch.setattr(fetchers, "_get", fake)
    assert fetchers.internet_archive("moby dick") == [] and fetchers._IA_FILES == {}


def test_no_single_source_can_spend_the_whole_page_deadline():
    """Two adapters now make a second HTTP call, and requests applies `timeout` to the connect
    AND to each read. Without one budget across the calls an adapter could run to 2 x TIMEOUT
    and keep a thread after the page had already been rendered."""
    assert fetchers.SOURCE_BUDGET < fetchers.SEARCH_DEADLINE
    # a scalar `timeout=8` is up to 16 s on a host that connects and then stalls: connect and
    # read together have to fit inside what is left
    connect, read = fetchers._Budget(fetchers.SOURCE_BUDGET).pair()
    assert connect == fetchers.CONNECT_TIMEOUT and connect + read <= fetchers.SOURCE_BUDGET
    b = fetchers._Budget(1.0)
    assert 0 < b.timeout() <= 1.0 and b.timeout(0.25) == 0.25
    assert sum(b.pair()) <= 1.1                    # the 0.05 "fail at once" floor is the slack
    b.until = time.monotonic() - 5
    assert b.spent() and b.timeout() == 0.0 and sum(b.pair()) <= 0.2 and min(b.pair()) > 0
    # the self-hosted OPDS catalog used a flat 25 s — longer than the page ever waits
    assert opds.TIMEOUT <= fetchers.SEARCH_DEADLINE


def test_every_enabled_default_source_has_a_live_adapter():
    """A source switched on in config with no adapter registered is a search slot that can
    only ever return nothing."""
    for name, on in config.SOURCES.items():
        assert name in fetchers._ADAPTERS, name
        if on:
            assert any(p["name"] == name for p in fetchers.PROVIDERS), name


# ---------------------------------------------------------------- item 4: the cover cache
class _Raw:
    def __init__(self, data): self.data = data
    def read(self, n, decode_content=True): return self.data[:n]


class _Img:
    def __init__(self, data=b"\xff\xd8jpg", ctype="image/jpeg", status=200):
        self.status_code, self.headers, self.raw = status, {"Content-Type": ctype}, _Raw(data)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _cover_client(client, users, monkeypatch, data=b"\xff\xd8jpg"):
    import requests
    login(client, "alice", users["alice"])
    hits = []
    monkeypatch.setattr(requests, "get", lambda u, **kw: (hits.append(u), _Img(data))[1])
    return hits


def test_a_cover_is_fetched_once_and_then_read_from_disk(client, users, monkeypatch):
    """3.14 s per cover, identical on repeat, three serial server-side hops for 5,596 bytes,
    and eight covers on one search page held all eight gunicorn threads."""
    import app as appmod
    hits = _cover_client(client, users, monkeypatch)
    url = "/cover?u=https://covers.openlibrary.org/b/id/12345-M.jpg"
    first = client.get(url)
    assert first.status_code == 200 and first.data == b"\xff\xd8jpg" and len(hits) == 1
    second = client.get(url)
    assert second.status_code == 200 and second.data == first.data and second.mimetype == "image/jpeg"
    assert len(hits) == 1, "the second request went back to Open Library"
    assert os.path.isfile(os.path.join(appmod.COVER_CACHE_DIR, "12345-M.jpg"))
    # the proxy is what keeps the reader's browser off covers.openlibrary.org: the cache must
    # not have been "fixed" by redirecting the browser there instead
    assert second.status_code == 200 and "Location" not in second.headers


def test_each_cover_size_is_its_own_cache_entry(client, users, monkeypatch):
    hits = _cover_client(client, users, monkeypatch)
    client.get("/cover?u=https://covers.openlibrary.org/b/id/7-M.jpg")
    client.get("/cover?u=https://covers.openlibrary.org/b/id/7-S.jpg")
    assert len(hits) == 2


def test_a_cover_is_not_cacheable_by_the_shared_proxy_in_front(client, users, monkeypatch):
    """/cover is behind @login_required and Cloudflare sits in front of this origin."""
    _cover_client(client, users, monkeypatch)
    r = client.get("/cover?u=https://covers.openlibrary.org/b/id/3-M.jpg")
    assert r.headers["Cache-Control"] == "private, max-age=604800"


def test_the_cover_cache_stays_under_its_bound(client, users, monkeypatch):
    """The bound is the point: an 80 GB disk shared with the library cannot host an unbounded
    pile of third-party images."""
    import app as appmod
    monkeypatch.setattr(appmod, "COVER_CACHE_MB", 1)
    hits = _cover_client(client, users, monkeypatch, data=b"\xff\xd8" + b"x" * (100 * 1024 - 2))
    for i in range(40):                       # 40 x 100 KiB against a 1 MiB cap
        assert client.get(f"/cover?u=https://covers.openlibrary.org/b/id/{i}-M.jpg").status_code == 200
    assert len(hits) == 40                    # every one was a miss, so every one was stored
    names = os.listdir(appmod.COVER_CACHE_DIR)
    total = sum(os.path.getsize(os.path.join(appmod.COVER_CACHE_DIR, n)) for n in names)
    assert total <= appmod.COVER_CACHE_MB * 1024 * 1024, f"{total} bytes over the cap"
    assert names, "the cache evicted itself down to nothing"
    assert "39-M.jpg" in names and "0-M.jpg" not in names          # oldest first, newest kept


def test_an_uncacheable_cover_url_is_still_proxied_and_never_written(client, users, monkeypatch):
    """Only the canonical /b/id/<n>-<S>.jpg form becomes a file name; anything else still goes
    through the proxy (which is the security property) but is not stored."""
    import app as appmod
    hits = _cover_client(client, users, monkeypatch)
    r = client.get("/cover?u=https://covers.openlibrary.org/b/olid/OL1M-M.jpg")
    assert r.status_code == 200 and len(hits) == 1
    client.get("/cover?u=https://covers.openlibrary.org/b/olid/OL1M-M.jpg")
    assert len(hits) == 2                                          # not cached, as designed
    assert not os.path.isdir(appmod.COVER_CACHE_DIR) or not os.listdir(appmod.COVER_CACHE_DIR)
    assert appmod._cover_key("https://covers.openlibrary.org/b/id/1-M.jpg/../../etc/passwd") is None
    assert appmod._cover_key("https://covers.openlibrary.org/b/id/1-M.jpg") == "1-M"


def test_a_failed_cover_is_not_written_to_the_cache(client, users, monkeypatch):
    import app as appmod, requests
    login(client, "alice", users["alice"])
    monkeypatch.setattr(requests, "get", lambda u, **kw: _Img(ctype="text/html", data=b"<html>"))
    assert client.get("/cover?u=https://covers.openlibrary.org/b/id/55-M.jpg").status_code == 404
    assert not os.path.isfile(os.path.join(appmod.COVER_CACHE_DIR, "55-M.jpg"))
