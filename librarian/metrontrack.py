"""Metron: a reader's Western comics, marked read in their own Metron collection (v5.9).

AniList (anilist.py) covers manga only. Metron (metron.cloud, the free comic database the portal
already uses for Western metadata) keeps a collection per user with a read flag and date, and
its API marks an issue read with POST /api/collection/scrobble/ {issue_id, date_read}: the issue
is added to the collection if it is not there yet (Metron's "collection scrobbling", 2026).

Each reader connects their OWN Metron account on Devices: user name and API key (metron.cloud ->
their profile), or the password. Kept in the portal's database only; the key is sent as a Bearer
token (as the portal's metadata calls do), a password as HTTP Basic.

What is sent: when Calibre-Web records a comic as FINISHED for a reader (the Kobo's 'Finished',
or Read marked on the portal for a Kindle or an iPad), and that comic came from a request found
through Metron, its Metron issue is scrobbled once, with the time Calibre-Web recorded. Comics
found through ComicVine or uploaded by hand have no Metron issue and are left out."""
import datetime, logging, sqlite3
import requests
import db, cwa, comicmeta

log = logging.getLogger("metrontrack")

TIMEOUT = (5, 20)
PER_RUN = 10                      # Metron allows 20 requests a minute; the metadata search shares it


class MetronError(Exception):
    pass


class AuthLost(MetronError):
    pass


def _auth(link):
    if link["method"] == "key":
        return {"headers": {"Authorization": f"Bearer {link['secret']}", "User-Agent": "bookstack-librarian"}}
    return {"auth": (link["username"], link["secret"]), "headers": {"User-Agent": "bookstack-librarian"}}


def _req(method, path, link, **kw):
    try:
        r = requests.request(method, f"{comicmeta.METRON}{path}", timeout=TIMEOUT, **_auth(link), **kw)
    except requests.RequestException as e:
        raise MetronError(f"Metron did not answer ({type(e).__name__})") from e
    if r.status_code in (401, 403):
        raise AuthLost("Metron refused the sign-in: connect again on Devices")
    if r.status_code == 429:
        raise MetronError("Metron asked us to slow down")
    if r.status_code >= 400:
        raise MetronError(f"Metron answered HTTP {r.status_code}")
    return r


def connect(owner, username, secret):
    """Check the account against Metron (the reader's collection list), then keep it."""
    username, secret = (username or "").strip(), (secret or "").strip()
    if not username or not secret:
        raise MetronError("the Metron user name and its API key (or password) are both needed")
    method = "key" if len(secret) >= 30 and " " not in secret else "password"
    link = {"username": username, "secret": secret, "method": method}
    _req("GET", "/collection/", link, params={"page_size": 1})
    db.metron_set(owner, username, secret, method)
    return username


def _issue_ids(book_ids):
    """{calibre_id: metron issue id} for comics that came from a Metron-found request."""
    if not book_ids:
        return {}
    ids = list(book_ids)
    with db._conn() as c:
        rows = c.execute(f"SELECT DISTINCT calibre_id, series_id, number FROM comic_requests WHERE provider='metron' "
                         f"AND calibre_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out = {}
    for r in rows:
        try:
            _info, items = comicmeta.series("metron", r["series_id"])
        except comicmeta.MetaError:
            continue
        it = next((i for i in items if i["number"] == r["number"]), None)
        if it and str(it.get("id") or "").isdigit():
            out[r["calibre_id"]] = int(it["id"])
    return out


def _read_dates(owner, book_ids):
    """When Calibre-Web recorded each book as read (book_read_link.last_modified), for date_read."""
    u = cwa.get_user(owner) or {}
    out = {}
    try:
        with cwa._conn() as c:
            for bid, ts in c.execute(f"SELECT book_id, last_modified FROM book_read_link WHERE user_id=? AND read_status=1 "
                                     f"AND book_id IN ({','.join('?' * len(book_ids))})", (u.get("id"), *book_ids)):
                out[bid] = ts
    except (sqlite3.Error, cwa.CwaError):
        pass
    return out


def _iso(ts):
    try:
        d = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        d = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return d.replace(microsecond=0).isoformat() + "Z"


def sync_owner(owner, budget):
    """Scrobble what this reader finished. Returns (sent, budget left)."""
    link = db.metron_get(owner)
    if not link:
        return 0, budget
    done = [bid for bid, s in cwa.reading_state(owner).items() if s["status"] == "read"]
    issues = _issue_ids(done)
    todo = {bid: iid for bid, iid in issues.items() if not db.metron_was_sent(owner, iid)}
    if not todo:
        return 0, budget
    dates = _read_dates(owner, list(todo))
    sent = 0
    for bid, iid in sorted(todo.items()):
        if budget <= 0:
            break
        budget -= 1
        try:
            _req("POST", "/collection/scrobble/", link, json={"issue_id": iid, "date_read": _iso(dates.get(bid))})
        except AuthLost as e:
            db.metron_note(owner, str(e))
            return sent, budget
        except MetronError as e:
            db.metron_note(owner, f"last try: {e}")
            return sent, budget
        db.metron_sent_set(owner, iid)
        sent += 1
    if sent:
        db.metron_note(owner, f"{sent} marked read {datetime.date.today().isoformat()}")
    return sent, budget


def sync_once():
    budget, total = PER_RUN, 0
    for link in db.metron_all():
        if budget <= 0:
            break
        try:
            n, budget = sync_owner(link["owner"], budget)
            total += n
        except Exception as e:                   # one reader's problem never stops the others
            log.warning("Metron sync for %s: %s", link["owner"], e)
    return total
