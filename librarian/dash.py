"""The admin dashboard's "What needs you" (v6.0): one place for everything that waits on a
person or is quietly failing, across every queue the portal keeps. Read-only; each part on its
own so one unreachable service never costs the page."""
import sqlite3, time
import config, db


def _count(sql, args=()):
    try:
        with db._conn() as c:
            return c.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return 0


def needs(now=None):
    """[(severity, text, link)] — severity 'bad' (failing), 'act' (waits on you), 'info'."""
    now = now or time.time()
    out = []
    portal = config.PORTAL_URL or ""
    pending = (_count("SELECT COUNT(*) FROM requests WHERE status='pending'")
               + _count("SELECT COUNT(*) FROM book_requests WHERE status='pending'"))
    if pending:
        out.append(("act", f"{pending} request{'s' if pending != 1 else ''} waiting for your approval", f"{portal}/status"))
    # comics switched off: their requests are not shown (their page is a 404 then)
    cpending = _count("SELECT COUNT(*) FROM comic_requests WHERE status='pending'") if config.COMICS_ENABLED else 0
    if cpending:
        out.append(("act", f"{cpending} comic request{'s' if cpending != 1 else ''} waiting for your approval", f"{portal}/comics?all=1"))
    try:
        import shelfmark_api
        sp = len(shelfmark_api.pending()) if shelfmark_api.configured() else 0
    except Exception:
        sp = 0
    if sp:
        out.append(("act", f"{sp} Shelfmark request{'s' if sp != 1 else ''} waiting for your approval", f"{portal}/status"))
    tables = (("book_requests", "book", f"{portal}/status"),) + \
        ((("comic_requests", "comic", f"{portal}/comics?all=1"),) if config.COMICS_ENABLED else ())
    for table, what, link in tables:
        held = _count(f"SELECT COUNT(*) FROM {table} WHERE status='held'")
        if held:
            out.append(("info", f"{held} arrived {what} file{'s' if held != 1 else ''} held for a reader to check (not what was asked for?)", link))
        old = _count(f"SELECT COUNT(*) FROM {table} WHERE status='confirm' AND updated < ?", (now - 3 * 86400,))
        if old:
            out.append(("info", f"{old} {what} cop{'ies' if old != 1 else 'y'} found 3+ days ago still waiting for a reader's yes", link))
    failed = _count("SELECT COUNT(*) FROM requests WHERE status='error'")
    if failed:
        out.append(("bad", f"{failed} failed import{'s' if failed != 1 else ''} (dead-letter)", f"{portal}/status"))
    nf = (_count("SELECT COUNT(*) FROM book_requests WHERE status='not-found' AND updated > ?", (now - 7 * 86400,))
          + _count("SELECT COUNT(*) FROM comic_requests WHERE status='not-found' AND updated > ?", (now - 7 * 86400,)))
    if nf:
        out.append(("info", f"{nf} request{'s' if nf != 1 else ''} not found this week (the indexers may name them differently)", f"{portal}/status"))
    stuck = (_count("SELECT COUNT(*) FROM book_requests WHERE status='downloading' AND queued_at < ?", (now - 2 * 86400,))
             + _count("SELECT COUNT(*) FROM comic_requests WHERE status='downloading' AND queued_at < ?", (now - 2 * 86400,)))
    if stuck:
        out.append(("bad", f"{stuck} download{'s' if stuck != 1 else ''} running for 2+ days (seedbox, Syncthing or Shelfmark?)", f"{portal}/status"))
    try:
        with db._conn() as c:
            bad = [f"{r[1]} ({r[0]})" for r in c.execute(
                "SELECT owner, name FROM follows WHERE detail LIKE 'could not check%' ORDER BY owner LIMIT 6")]
    except sqlite3.Error:
        bad = []
    if bad:
        fol = _count("SELECT COUNT(*) FROM follows WHERE detail LIKE 'could not check%'")
        out.append(("bad", f"{fol} followed name{'s' if fol != 1 else ''} failing their daily check: " + ", ".join(bad)
                    + ("…" if fol > len(bad) else ""), None))
    # v6.2.1: the library's rules (crosscheck.py; the hourly self-check asks the same): problems
    # are failures, notes are normal states worth seeing
    try:
        import crosscheck
        cc = crosscheck.run(now)
        out += [("bad", p["text"], None) for p in cc["problems"]] + [("info", n["text"], None) for n in cc["notes"]]
        if cc.get("error"):
            out.append(("bad", cc["error"], None))
    except Exception as e:
        out.append(("bad", f"the library cross-check could not run ({e})", None))
    for table, label in (("anilist", "AniList"), ("metron_link", "Metron"), ("hc_audio_state", "Hardcover")):
        bad = _count(f"SELECT COUNT(*) FROM {table} WHERE detail LIKE '%refused%' OR detail LIKE '%connect again%'")
        if bad:
            out.append(("info", f"{bad} reader{'s' if bad != 1 else ''} whose {label} connection was refused (they reconnect on Devices)", None))
    return out


def week(now=None):
    """What the library did in the last 7 days: {arrived, shared, requests, readers}."""
    now = now or time.time()
    since = now - 7 * 86400
    return {"arrived": _count("SELECT COUNT(*) FROM requests WHERE status='done' AND updated > ?", (since,))
                       + _count("SELECT COUNT(*) FROM book_requests WHERE status='done' AND updated > ?", (since,))
                       + _count("SELECT COUNT(*) FROM comic_requests WHERE status='done' AND updated > ?", (since,)),
            "shared": _count("SELECT COUNT(*) FROM book_requests WHERE status='shared' AND updated > ?", (since,))
                      + _count("SELECT COUNT(*) FROM comic_requests WHERE status='shared' AND updated > ?", (since,)),
            "requests": _count("SELECT COUNT(*) FROM book_requests WHERE created > ?", (since,))
                        + _count("SELECT COUNT(*) FROM comic_requests WHERE created > ?", (since,))
                        + _count("SELECT COUNT(*) FROM requests WHERE created > ?", (since,)),
            "readers": _count("SELECT COUNT(DISTINCT user) FROM audit WHERE ts > ? AND user IS NOT NULL", (since,))}
