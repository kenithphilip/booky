"""Send a book to an e-reader's own web browser with a short code (v5.9.1).

Send-to-Kindle mail has a size limit and needs an approved sender; a Kobo cannot receive mail at
all. Every Kobo and Kindle has a web browser, so: the e-reader opens request.<domain>/send, which
shows a 4-character code (and needs no login: typing a password on an e-ink keyboard is the
problem being solved). The reader, signed in on their phone or computer, types that code on a
book's page. The e-reader's page refreshes itself and offers the file; the e-reader's browser
downloads it into its library.

Only the browser that showed the code can fetch the book: the code is tied to a random secret in
that browser's cookie, so guessing a code gets nobody a book, and typing the wrong code at worst
sends a book to another open /send page for 15 minutes. A code carries one book, lives 15
minutes, and the file can be fetched for 10 minutes after it was attached.

The format suits the device: a Kobo gets a KEPUB (or EPUB), a Kindle's browser only opens
AZW3/MOBI, PDF and TXT (never EPUB), anything else gets EPUB."""
import secrets, time
import config, db

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no 0/O, 1/I/L: read off an e-ink screen
CODE_LIFE = 15 * 60
FETCH_LIFE = 10 * 60
PREFER = {"kobo": ("kepub", "epub", "pdf"), "kindle": ("azw3", "mobi", "pdf", "txt"),
          "other": ("epub", "pdf", "azw3", "mobi")}


class SendError(Exception):
    pass


def device_of(user_agent):
    ua = (user_agent or "").lower()
    return "kobo" if "kobo" in ua else "kindle" if "kindle" in ua else "other"


def new_code(user_agent, now=None):
    """(code, secret) for an e-reader's page."""
    for _ in range(20):
        code = "".join(secrets.choice(ALPHABET) for _ in range(4))
        if not db.send_code_get(code):
            secret = secrets.token_urlsafe(24)
            db.send_code_new(code, secret, device_of(user_agent), now)
            return code, secret
    raise SendError("could not make a code; reload the page")


def page_state(secret, now=None):
    """The row behind a browser's cookie, while it is alive; None otherwise."""
    now = now or time.time()
    r = db.send_code_by_secret(secret) if secret else None
    if not r or now - r["created"] > CODE_LIFE + FETCH_LIFE:
        return None
    if not r["book_id"] and now - r["created"] > CODE_LIFE:
        return None
    return r


def choose_format(device, formats):
    fmts = [f.lower() for f in formats]
    have = set(fmts) | ({"kepub"} if config.KEPUBIFY and "epub" in fmts else set())
    return next((f for f in PREFER[device] if f in have), None)


def attach(owner, is_admin, code, book_id, now=None):
    """Put one of the reader's books on the e-reader page showing `code`. Returns (device, fmt)."""
    import library
    now = now or time.time()
    code = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    r = db.send_code_get(code)
    if not r or now - r["created"] > CODE_LIFE:
        raise SendError("no e-reader is showing that code (codes last 15 minutes: reload the page on the e-reader)")
    if r["book_id"]:
        raise SendError("that code already has a book: reload the page on the e-reader for a new code")
    b = library.book_detail(owner, book_id, is_admin)
    if not b:
        raise SendError("no such book in your library")
    fmt = choose_format(r["device"], b["formats"])
    if not fmt:
        raise SendError("a Kindle's browser cannot open EPUB: use Convert to AZW3 on this page first, then send it"
                        if r["device"] == "kindle" else "this book has no format that e-reader can open")
    if not db.send_code_attach(code, owner, book_id, fmt, now):
        raise SendError("that code was just used: reload the page on the e-reader")
    return r["device"], fmt


def file_for(r, now=None):
    """The file an e-reader page may fetch now, or None."""
    import library
    now = now or time.time()
    if not r or not r.get("book_id") or now - (r.get("attached") or 0) > FETCH_LIFE:
        return None
    import comics
    return library.file_for(r["owner"], r["book_id"], r["fmt"], comics._is_admin(r["owner"]))
