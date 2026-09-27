"""Your own catalogs: any number of OPDS feeds (Calibre's content server, Calibre-Web/CWA,
COPS, Kavita, Komga, BookLore, Ubooquity, a public library's OPDS…), added by the admin from the
portal's admin page or the TUI (python -m admin_cli catalogs ...).

Each one becomes a first-class source, `opds:<id>`: searched on the search page, searched for a
chosen book on its work page and by keep-looking, its downloads sent its login and nothing else,
its origin trusted for a tailnet/LAN address. The v4 single catalog (MYCATALOG_* in .env) is
still honoured as source "mycatalog", so existing requests keep their meaning.

OPDS is the only protocol accepted, on purpose: it is a standard with a defined acquisition link,
so a new catalog needs configuration, not code. Other kinds of source belong in Shelfmark, which
has adapters for them and attributes every download to the reader who made it."""
import re
from urllib.parse import urlsplit

import config, db

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def _row(r, legacy=False):
    return {"id": r["id"], "source": r["id"] if legacy else f"opds:{r['id']}", "name": r["name"],
            "url": r["url"], "user": r.get("user") or "", "password": r.get("password") or "",
            "enabled": bool(r.get("enabled", 1)), "legacy": legacy}


def all_catalogs(include_disabled=False):
    out = []
    if config.MYCATALOG_URL and (include_disabled or config.SOURCES.get("mycatalog")):
        out.append(_row({"id": "mycatalog", "name": config.MYCATALOG_NAME, "url": config.MYCATALOG_URL,
                         "user": config.MYCATALOG_USER, "password": config.MYCATALOG_PASS,
                         "enabled": config.SOURCES.get("mycatalog")}, legacy=True))
    for r in db.catalog_rows():
        if include_disabled or r["enabled"]:
            out.append(_row(r))
    return out


def get(source):
    for c in all_catalogs(include_disabled=True):
        if c["source"] == source:
            return c
    return None


def validate(cid, name, url):
    if not ID.fullmatch(cid or ""):
        return "the id must be 1-31 lowercase letters, digits or dashes"
    if cid == "mycatalog":
        return "that id is reserved"
    if not (name or "").strip():
        return "give it a name readers will see"
    p = urlsplit(url or "")
    if p.scheme not in ("http", "https") or not p.hostname or p.username is not None:
        return "the address must be an http(s) URL without a login in it (the login has its own fields)"
    return None


def test(url, user="", password=""):
    """(ok, message): fetch the feed once and count its entries."""
    import requests
    from lxml import etree
    try:
        r = requests.get(url.replace("{q}", "a"), auth=(user, password) if user else None,
                         headers={"User-Agent": "bookstack-librarian/5"}, timeout=(3, 10))
    except requests.RequestException as e:
        return False, f"no answer ({e.__class__.__name__})"
    if r.status_code in (401, 403):
        return False, "the catalog refused the login"
    if r.status_code != 200:
        return False, f"it answered HTTP {r.status_code}"
    try:
        root = etree.fromstring(r.content)
    except etree.XMLSyntaxError:
        return False, "that address does not serve an OPDS (Atom) feed"
    n = len(root.findall("{http://www.w3.org/2005/Atom}entry"))
    return True, f"OPDS feed answered with {n} entr{'y' if n == 1 else 'ies'}"


def trusted_netlocs():
    return {urlsplit(c["url"]).netloc.lower() for c in all_catalogs(include_disabled=True)} - {""}
