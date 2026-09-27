"""The providers added for v5: an exact Open Library lookup when a request carries its work,
Google Books and Hardcover's own API — each only with the owner's key, each dropping tags."""
import time
import pytest
import config, metadata


class R:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


def budget():
    return time.monotonic() + 30


def test_a_request_with_a_work_key_is_looked_up_exactly(monkeypatch):
    seen = []
    def get(url, params=None, **k):
        seen.append((url, params))
        if url.endswith("/search.json"):
            return R(200, {"docs": [{"key": "/works/OL66554W", "title": "Pride and Prejudice",
                                     "author_name": ["Jane Austen"], "cover_i": 1}]})
        return R(200, {"description": {"value": "A novel of manners."}})
    monkeypatch.setattr(metadata.requests, "get", get)
    got = metadata._openlibrary({"title": "whatever the file said", "author": "",
                                 "identifiers": [{"kind": "openlibrary_work", "value": "OL66554W"}]}, budget())
    assert seen[0][1]["q"] == "key:/works/OL66554W", "not a title guess"
    assert got["description"] == "A novel of manners."
    assert got["identifiers"][0] == {"kind": "olid", "value": "OL66554W", "provider": "openlibrary", "exact": True}


def test_keyed_providers_are_off_without_a_key(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_BOOKS_API_KEY", "")
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "")
    names = [n for n, _ in metadata.enabled_providers()]
    assert "google" not in names and "hardcover_api" not in names and "openlibrary" in names
    monkeypatch.setattr(config, "GOOGLE_BOOKS_API_KEY", "k")
    assert "google" in [n for n, _ in metadata.enabled_providers()]


def test_google_books_with_a_key_and_its_categories_dropped(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_BOOKS_API_KEY", "gkey")
    calls = []
    def get(url, params=None, **k):
        calls.append(params)
        return R(200, {"items": [{"id": "abc", "volumeInfo": {
            "title": "Emma", "authors": ["Jane Austen"], "description": "Matchmaking.", "pageCount": 474,
            "categories": ["Fiction"], "language": "en",
            "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780141439587"}],
            "imageLinks": {"thumbnail": "http://books.google.com/x.jpg"}}}]})
    monkeypatch.setattr(metadata.requests, "get", get)
    got = metadata._google({"title": "Emma", "author": "Jane Austen", "identifiers": []}, budget())
    assert calls[0]["key"] == "gkey" and 'intitle:"Emma"' in calls[0]["q"]
    assert got["description"] == "Matchmaking." and got["cover_url"].startswith("https://")
    assert "categories" not in got and "tags" not in got and "Genres" not in got
    assert {"kind": "isbn13", "value": "9780141439587", "provider": "google", "exact": False} in got["identifiers"]


def test_hardcover_api_sends_the_token_on_the_request_only(monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "hc_pat_secret")
    sent = []
    def post(url, json=None, headers=None, **k):
        sent.append(headers)
        return R(200, {"data": {"search": {"results": {"hits": [{"document": {
            "id": 42, "title": "The Fellowship of the Ring", "author_names": ["J.R.R. Tolkien"],
            "featured_series": {"position": 1, "series": {"name": "The Lord of the Rings"}},
            "genres": ["Fantasy"], "isbns": ["9780547928210"], "image": {"url": "https://x/y.jpg"}}}]}}}})
    monkeypatch.setattr(metadata.requests, "post", post)
    got = metadata._hardcover_api({"title": "The Fellowship of the Ring", "author": "Tolkien", "identifiers": []}, budget())
    assert sent[0]["Authorization"] == "Bearer hc_pat_secret"
    assert "Authorization" not in metadata.UA, "never stored on a shared header dict (CWA's bug)"
    assert got["series"] == "The Lord of the Rings" and got["series_position"] == 1
    assert "genres" not in got and "tags" not in got


def test_a_refused_hardcover_token_is_a_provider_failure_not_a_miss(monkeypatch):
    monkeypatch.setattr(config, "HARDCOVER_API_KEY", "bad")
    monkeypatch.setattr(metadata.requests, "post", lambda *a, **k: R(401, {}))
    with pytest.raises(metadata.ProviderDown):
        metadata._hardcover_api({"title": "x", "identifiers": []}, budget())
