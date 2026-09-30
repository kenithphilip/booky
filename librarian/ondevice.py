"""v6.3: what is on each reader's devices, and how they keep them tidy. ONE place: every page, the
worker and the cross-check ask here, so no part guesses.

Kobo (Calibre-Web's Kobo sync; cwa.py holds its records):
  - What goes to it: 'all' (every book the reader has; Calibre-Web's normal sync) or 'choose' (only
    the books they send, through the portal's own Kobo shelf). An ADMIN's Calibre-Web account sees
    every book in the library, so their Kobo would get everybody's: for them 'all' means all their
    OWN books (the portal keeps its shelf filled with them), and 'library' is the whole library.
  - Finished books can leave the Kobo by themselves (never / right away / after 7 or 30 days);
    they stay in the reader's library, and 'Send to my Kobo' puts one back (and keeps it there).
Kindle: Amazon has no way to take a book off a Kindle, so a finished book that went to one is
  listed for the reader to delete there (a reminder they can switch off).
Privacy: a reader is only ever told about their own books ('not-theirs' otherwise)."""
import logging, time
import config, db, cwa

log = logging.getLogger("ondevice")

FINISHED_CHOICES = [(None, "Never: they stay on the Kobo"), (0, "Right away"), (7, "7 days after I finish them"),
                    (30, "30 days after I finish them")]
KOBO_SEND = {"all": "Every book in my library", "choose": "Only the books I send"}
ADMIN_SEND = {"all": "All my own books", "choose": "Only the books I send", "library": "Every book in the library (all readers')"}


def _owners(book_id):
    import share
    try:
        return share._owners_of_book(int(book_id))
    except Exception:
        return []


def _owned_ids(user):
    """Every book this reader has (their owner tag)."""
    import library
    c = library._conn()
    try:
        return {r[0] for r in c.execute("SELECT l.book FROM books_tags_link l JOIN tags t ON t.id=l.tag WHERE t.name=?",
                                        (config.OWNER_PREFIX + user,))}
    finally:
        c.close()


def is_admin(user):
    try:
        return bool(((cwa.get_user(user) or {}).get("role") or 0) & cwa.ROLE_ADMIN)
    except Exception:
        return False


# ---- what goes to the Kobo ---------------------------------------------------------------------------
def kobo_send(user, admin=None):
    """'all' | 'choose' | 'library' (the last for admins only)."""
    admin = is_admin(user) if admin is None else admin
    if admin:
        scope = db.get_prefs(user)["kobo_scope"]
        return {"own": "all", "choose": "choose", "library": "library"}[scope]
    return "choose" if cwa.kobo_choose_only(user) else "all"


def set_kobo_send(user, mode, keep=True, admin=None):
    """Change what goes to this reader's Kobo. keep: the books on it now stay (else a 'choose' Kobo
    starts empty, apart from what they send). Returns {'sent': n books the next sync sends,
    'removed': n it takes off} for the message."""
    admin = is_admin(user) if admin is None else admin
    valid = ADMIN_SEND if admin else KOBO_SEND
    if mode not in valid:
        raise ValueError(mode)
    before = cwa.kobo_synced_ids(user)
    owned = _owned_ids(user)
    if admin:
        db.set_device_prefs(user, kobo_scope={"all": "own", "choose": "choose", "library": "library"}[mode])
    if mode == "library":
        cwa.kobo_set_choose_only(user, False)
        return {"sent": 0, "removed": 0, "all": True}
    if mode == "all" and not admin:
        cwa.kobo_set_choose_only(user, False)
        off = set(db.device_books(user, "kobo", ("off",))) & owned
        for b in off:                            # what they took off stays off (archived for them)
            cwa.kobo_remove(user, b, even_unsent=True)
        return {"sent": len(owned - before - off), "removed": 0}
    # 'choose', or an admin's own books: the portal's shelf
    keep_ids = (before & owned) if keep or mode == "all" else set()
    cwa.kobo_set_choose_only(user, True, keep=keep_ids)
    if mode == "all":
        fill_admin_shelf(user)
    return {"sent": 0, "removed": len(before - keep_ids) if mode == "choose" else len(before - owned)}


def fill_admin_shelf(user, now=None):
    """An admin's Kobo in 'all my own books': every book they have goes onto the portal's shelf
    (except those they or the finished-books setting took off), and a book they no longer have
    comes off. Returns (added, removed)."""
    owned = _owned_ids(user)
    off = set(db.device_books(user, "kobo", ("off",))) | cwa.kobo_archived_ids(user)   # deleted on the Kobo: stays off
    want = owned - off
    on = cwa.kobo_shelf_ids(user)
    added = cwa.kobo_shelf_add(user, sorted(want - on)) if want - on else 0
    gone = sorted(on - owned)
    removed = cwa.kobo_shelf_remove(user, gone) if gone else 0
    return added, removed


def admin_kobo_pass(now=None):
    """Worker: every admin with a Kobo gets only their own books unless they chose otherwise (v6.3:
    an admin's Calibre-Web account sees the whole library, and so did their Kobo)."""
    n = 0
    for u in cwa.list_users():
        if not u["is_admin"] or cwa.kobo_states(u["name"], [0])[0] == "no-kobo":
            continue
        scope = db.get_prefs(u["name"])["kobo_scope"]
        try:
            if scope == "own":
                if not cwa.kobo_choose_only(u["name"]):
                    set_kobo_send(u["name"], "all", admin=True)
                    log.info("admin %s: their Kobo now gets only their own books", u["name"])
                n += sum(fill_admin_shelf(u["name"], now))
        except Exception as e:
            log.warning("admin Kobo pass for %s: %s", u["name"], e)
    return n


# ---- where a book stands, and sending it or taking it off ------------------------------------------------
def kobo_states(user, book_ids, admin=None):
    """{book id: state}; 'not-theirs' for a book the reader does not have (an admin in 'library'
    mode excepted: their Kobo gets every book)."""
    admin = is_admin(user) if admin is None else admin
    ids = [int(b) for b in book_ids]
    whole = admin and kobo_send(user, admin) == "library"
    owned = None if whole else _owned_ids(user)
    raw = cwa.kobo_states(user, [b for b in ids if whole or b in owned])
    return {b: raw.get(b, "not-theirs") for b in ids}


def kobo_state(user, book_id, admin=None):
    return kobo_states(user, [book_id], admin)[int(book_id)]


def send_to_kobo(user, book_id, admin=None):
    """'Send to my Kobo' (also 'Put it back'): it goes at the next sync and stays (a finished book
    sent back is not taken off again by itself). Returns the new state."""
    state = kobo_state(user, book_id, admin)
    if state in ("not-theirs", "no-kobo"):
        return state
    if cwa.kobo_choose_only(user):
        cwa.kobo_shelf_add(user, [book_id], refresh=True)
    else:
        cwa.kobo_unarchive(user, book_id)
    if db.device_book(user, book_id, "kobo") or state in ("deleted", "removing"):
        db.device_book_set(user, book_id, "kobo", "kept")
    return kobo_state(user, book_id, admin)


def take_off_kobo(user, book_id, admin=None, why="kept"):
    """'Take it off my Kobo' (or a finished book leaving): off the Kobo at the next sync, still in the
    library. Returns the new state."""
    state = kobo_state(user, book_id, admin)
    if state in ("not-theirs", "no-kobo"):
        return state
    cwa.kobo_remove(user, book_id, even_unsent=True)
    db.device_book_set(user, book_id, "kobo", "off")
    return kobo_state(user, book_id, admin)


def offload_finished(now=None):
    """Worker: readers who chose it have finished books taken off their Kobo after their delay. A
    book counts as finished when the Kobo says so or they marked it Read. Returns how many left."""
    now = now or time.time()
    n = 0
    for user, days in db.readers_with("kobo_finished"):
        try:
            finished = {b for b, st in cwa.reading_state(user).items() if st.get("status") == "read"}
            rows = db.device_books(user, "kobo")
            for b, r in rows.items():            # marked unread again: the countdown stops
                if r["status"] == "waiting" and b not in finished:
                    db.device_book_clear(user, b, "kobo")
            if not finished:
                continue
            states = kobo_states(user, finished)
            for b in finished:
                if states.get(b) != "on-kobo" or (rows.get(b) or {}).get("status") in ("kept", "off"):
                    continue
                r = rows.get(b)
                if not r or r["status"] != "waiting":
                    db.device_book_set(user, b, "kobo", "waiting", now)
                    since = now
                else:
                    since = r["since"] or now
                if now - since >= (days or 0) * 86400:
                    take_off_kobo(user, b)
                    n += 1
        except Exception as e:
            log.warning("finished books for %s: %s", user, e)
    return n


# ---- Kindle: finished books to delete there --------------------------------------------------------------
def kindle_to_delete(user):
    """[{book_id, title}] finished books this reader sent to their Kindle and has not said they
    deleted there (Amazon has no way to take a book off a Kindle)."""
    if not db.get_prefs(user)["kindle_hint"]:
        return []
    finished = {b for b, st in cwa.reading_state(user).items() if st.get("status") == "read"}
    if not finished:
        return []
    with db._conn() as c:
        sent = {r[0]: r[1] for r in c.execute("SELECT book_id, max(title) FROM kindle_jobs WHERE owner=? AND status='sent' "
                                              "GROUP BY book_id", (user,))}
    done = set(db.device_books(user, "kindle", ("done",)))
    owned = _owned_ids(user)
    return [{"book_id": b, "title": sent[b]} for b in sorted(finished & set(sent)) if b not in done and b in owned]


def kindle_deleted(user, book_id):
    db.device_book_set(user, book_id, "kindle", "done")
