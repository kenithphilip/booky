"""The provider chain and its failsafes.

The owner's requirement, verbatim: "include fail-safes as well for every failures so they can
fallback on the 2nd 3rd 4th one if one fails." Five failure modes, each proven here — and the
distinction that matters most, that a MISS is not a FAILURE.
"""
import time
import pytest
import requests
import db, metadata


@pytest.fixture(autouse=True)
def _clean_breakers():
    with db._conn() as c:
        c.execute("DELETE FROM meta_provider_state")
        c.execute("DELETE FROM meta_miss")
    yield


def _chain(monkeypatch, *fns):
    monkeypatch.setattr(metadata, "PROVIDERS",
                        tuple((f"p{i}", f) for i, f in enumerate(fns)))
    monkeypatch.setattr(metadata.config, "METADATA_PROVIDERS", {})   # .get(n, True) => all on


Q = {"title": "Moby-Dick", "author": "Melville",
     "identifiers": [{"kind": "isbn13", "value": "9780142437247"}]}


def test_a_dead_provider_falls_through_to_the_next(monkeypatch):
    def down(q, b): raise metadata.ProviderDown("connection refused")
    def good(q, b): return {"title": "Moby-Dick", "description": "A whale.", "cover_url": "u"}
    _chain(monkeypatch, down, good)
    merged, trace = metadata.fetch(Q)
    assert merged["title"] == "Moby-Dick"
    assert [t["outcome"] for t in trace] == ["down", "hit"]


def test_a_soft_miss_advances_the_chain_without_blaming_the_provider(monkeypatch):
    """HTTP 200 carrying nothing is a clean miss. Counting it as a failure would stand down a
    healthy provider for having no answer about one obscure book — which is most books."""
    def miss(q, b): raise metadata.Miss("no docs")
    def good(q, b): return {"title": "T", "description": "D", "cover_url": "u"}
    _chain(monkeypatch, miss, good)
    merged, trace = metadata.fetch(Q)
    assert trace[0]["outcome"] == "miss" and merged["title"] == "T"
    assert db.breaker_state("p0") == (False, 0.0), "a miss must never trip the breaker"


def test_three_hard_failures_stand_a_provider_down_and_it_is_then_skipped(monkeypatch):
    def down(q, b): raise metadata.ProviderDown("timeout")
    _chain(monkeypatch, down)
    for _ in range(3):
        metadata.fetch(Q)
    _, trace = metadata.fetch(Q)
    assert trace[0]["outcome"] == "skipped"
    assert [b["provider"] for b in db.open_breakers()] == ["p0"]


def test_a_rate_limit_counts_as_a_failure_but_a_400_does_not(monkeypatch):
    """429 is the provider refusing us. 400 is bookinfo's answer to an ISBN-10 — our fault,
    so it must advance the chain rather than stand a working provider down."""
    class R:
        def __init__(self, code): self.status_code = code
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(429))
    with pytest.raises(metadata.ProviderDown):
        metadata._get("https://x/y", time.monotonic() + 10)
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(400))
    with pytest.raises(metadata.Miss):
        metadata._get("https://x/y", time.monotonic() + 10)


def test_a_provider_that_raises_anything_else_cannot_take_the_worker_down(monkeypatch):
    def boom(q, b): raise ZeroDivisionError("bad adapter")
    def good(q, b): return {"title": "T", "description": "D", "cover_url": "u"}
    _chain(monkeypatch, boom, good)
    merged, trace = metadata.fetch(Q)
    assert trace[0]["outcome"] == "error" and merged["title"] == "T"


def test_every_provider_failing_is_visible_not_silent(monkeypatch):
    """'We asked and nobody knew' must be distinguishable from 'all three were stood down'."""
    def down(q, b): raise metadata.ProviderDown("dns")
    _chain(monkeypatch, down, down)
    merged, trace = metadata.fetch(Q)
    assert merged == {}
    line = metadata.describe_trace(trace)
    assert "p0: down" in line and "p1: down" in line and "dns" in line


def test_genres_can_never_cross_the_adapter_boundary():
    """The accident that would move a book between family members: CWA appends tags rather
    than replacing, and a genre on a denied-tags list hides the book from its owner."""
    dirty = {"title": "T", "Genres": ["Science Fiction", "Audiobook"],
             "genres": ["x"], "subject": ["y"], "tags": ["z"]}
    clean = metadata._clean(dirty)
    assert clean == {"title": "T"}
    assert not any(k.lower() in ("genres", "subject", "tags") for k in clean)


def test_identifiers_accumulate_across_providers_but_other_fields_do_not(monkeypatch):
    def a(q, b): return {"title": "First", "identifiers": [{"kind": "isbn13", "value": "9780142437247"}]}
    def c(q, b): return {"title": "Second", "description": "D", "cover_url": "u",
                        "identifiers": [{"kind": "olid", "value": "OL1W"}]}
    _chain(monkeypatch, a, c)
    merged, _ = metadata.fetch(Q)
    assert merged["title"] == "First", "the chain is ranked: first non-empty wins"
    kinds = sorted(i["kind"] for i in merged["identifiers"])
    assert kinds == ["isbn13", "olid"], "identifiers accumulate — that is the point of a chain"


def test_the_chain_stops_once_it_has_enough(monkeypatch):
    """Asking a 28-second provider for a page count nobody wanted is not worth it."""
    calls = []
    def good(q, b): calls.append("a"); return {"title": "T", "description": "D", "cover_url": "u"}
    def never(q, b): calls.append("b"); raise AssertionError("must not be reached")
    _chain(monkeypatch, good, never)
    metadata.fetch(Q)
    assert calls == ["a"]


def test_an_isbn10_is_normalised_and_never_sent_as_an_isbn13():
    ids = metadata._ids([("isbn", "0547928220"), ("isbn", "9780142437247"),
                         ("isbn", "nonsense")], "x", True)
    kinds = {i["kind"]: i["value"] for i in ids}
    assert kinds == {"isbn10": "0547928220", "isbn13": "9780142437247"}


def test_the_negative_cache_stops_re_asking_about_a_book_nobody_knows():
    key = metadata.cache_key(Q)
    assert key == "isbn13:9780142437247"
    assert not metadata.negative_cached(key)
    metadata.remember_miss(key)
    assert metadata.negative_cached(key)
    # and it ages out rather than being permanent
    assert not metadata.negative_cached(key, now=time.time() + metadata.NEG_CACHE_SECONDS + 1)


def test_a_slow_provider_cannot_outlive_the_book_budget(monkeypatch):
    """The failure this project shipped twice: a source that accepts the connection and then
    stalls. The budget is a wall clock, not a per-call timeout."""
    captured = {}
    class R:
        status_code = 200
        def json(self): return {}
    def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=True):
        captured["timeout"] = timeout
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    metadata._get("https://x/y", time.monotonic() + 4)
    c, r = captured["timeout"]
    assert c <= metadata.CONNECT_TIMEOUT and c + r <= 4.1, "connect+read must fit the remaining budget"
    with pytest.raises(metadata.ProviderDown):
        metadata._get("https://x/y", time.monotonic() - 1)      # already out of time
