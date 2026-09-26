"""Dedupe: is this book already on THIS reader's shelf?

The old matcher was `lower(title) = lower(?)` and failed both ways: an edition with a subtitle
never matched (a reader told they lacked a book they had), and two unrelated books sharing a
title always matched (a reader told they had a book they did not).
"""
import config, dedupe
from conftest import add_calibre_book, calibre_conn


def _isbn(book_id, isbn):
    c = calibre_conn(config.CALIBRE_DB)
    c.execute("INSERT INTO identifiers(book,type,val) VALUES(?,?,?)", (book_id, "isbn", isbn))
    c.commit(); c.close()


def test_a_subtitle_or_edition_bracket_still_matches(users):
    add_calibre_book(1, "The Hobbit", "J. R. R. Tolkien", tags=["owner:alice"])
    idx = dedupe.Index("alice")
    for variant in ("The Hobbit: or There and Back Again", "Hobbit (75th Anniversary Edition)",
                    "the hobbit", "THE HOBBIT - Illustrated"):
        assert idx.match(variant, "Tolkien", ()), variant


def test_the_same_title_by_a_different_author_is_not_a_duplicate(users):
    """The false positive: 'Emma' by Austen is not 'Emma' by someone else."""
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    idx = dedupe.Index("alice")
    assert idx.match("Emma", "Alexander McCall Smith", ()) is None
    assert idx.match("Emma", "Austen, Jane", ())["how"] == "title+author"   # name order free


def test_no_author_to_compare_falls_back_to_title_and_says_so(users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    hit = dedupe.Index("alice").match("Emma", "", ())
    assert hit and hit["how"] == "title", "the weaker rung must be labelled as weaker"


def test_an_isbn_in_calibres_identifiers_table_wins(users):
    """CWA fills `identifiers` from each file's dc:identifier; no portal module had ever read it."""
    add_calibre_book(1, "Nineteen Eighty-Four", "George Orwell", tags=["owner:alice"])
    _isbn(1, "978-0-452-28423-4")
    hit = dedupe.Index("alice").match("1984 (Signet Classics)", "Orwell",
                                      [{"kind": "isbn13", "value": "9780452284234"}])
    assert hit == {"how": "isbn", "book_id": 1}, "a different title must not hide an exact ISBN"


def test_accents_do_not_break_the_match(users):
    add_calibre_book(1, "Wuthering Heights", "Emily Brontë", tags=["owner:alice"])
    assert dedupe.Index("alice").match("Wuthering Heights", "Emily Bronte", ())


def test_another_readers_copy_is_never_reported_as_yours(users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:bob"])
    assert dedupe.Index("alice").match("Emma", "Jane Austen", ()) is None
    assert dedupe.Index("admin", is_admin=True).match("Emma", "Jane Austen", ())


def test_initials_alone_do_not_make_two_authors_agree(users):
    """'J. Smith' and 'J. Jones' share an initial and nothing else."""
    add_calibre_book(1, "Collected Poems", "J. Smith", tags=["owner:alice"])
    assert dedupe.Index("alice").match("Collected Poems", "J. Jones", ()) is None


def test_the_one_off_form_still_works(users):
    add_calibre_book(1, "Emma", "Jane Austen", tags=["owner:alice"])
    assert dedupe.exists("Emma", "alice", author="Austen")
    assert not dedupe.exists("Emma", "alice", author="Somebody Else")
