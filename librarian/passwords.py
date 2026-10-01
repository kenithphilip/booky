"""v6.4.0: the ONE way a reader's password changes, used by Devices -> Account and by the
self-service reset (pwreset.py): every place at once, never two passwords.

Order matters. Audiobookshelf is a separate service that can be down: it goes FIRST, and when it
cannot take the new password nothing has changed anywhere. Then Calibre-Web's app.db (the portal,
the library site, OPDS apps, Shelfmark and KOReader sync all read it), retried briefly because a
CWA restart holds the file for a moment. The sign-in page (Authelia) is queued: the host's
gate-sync writes it within seconds and a 10-minute timer retries, so it cannot be lost."""
import logging, time
import config, db, cwa, auth
import abs as absapi

log = logging.getLogger("passwords")
MIN_LENGTH = 8


class PasswordError(Exception):
    pass


def check(new, repeat=None):
    """The message for a password that cannot be used, or None."""
    if len(new or "") < MIN_LENGTH:
        return f"the password needs at least {MIN_LENGTH} characters"
    if repeat is not None and new != repeat:
        return "the two passwords do not match"
    return None


def set_everywhere(user, new):
    """Set it in Audiobookshelf, Calibre-Web (portal, library site, apps, Shelfmark) and the
    sign-in page. Returns {'fp': the new fingerprint or None, 'abs': bool, 'gate': bool}.
    Raises PasswordError, with nothing changed, when Audiobookshelf cannot take it."""
    bad = check(new)
    if bad:
        raise PasswordError(bad)
    out = {"fp": None, "abs": False, "gate": False}
    if absapi.configured():
        try:
            has_abs = absapi.find_user(user) is not None
        except Exception as e:
            log.warning("password for %s: Audiobookshelf not answering: %s", user, e)
            raise PasswordError("the audiobook server is not answering, so nothing was changed: try again in a minute")
        if has_abs:
            try:
                absapi.set_password(user, new)
                out["abs"] = True
            except Exception as e:
                log.warning("password for %s: Audiobookshelf refused it: %s", user, e)
                raise PasswordError("the audiobook server did not take the new password, so nothing was changed: "
                                    "try again in a minute")
    for attempt in range(5):
        try:
            cwa.set_password(user, new)
            break
        except cwa.CwaUnavailable:
            if attempt == 4:
                # Audiobookshelf already has it: say so plainly and tell the admin (never silent)
                import notify
                notify.alert(f"{user}'s password change is half done",
                             f"Audiobookshelf took {user}'s new password but the library database stayed busy. "
                             f"Ask {user} to set it again on Devices -> Account (or reset it in bookstack.sh).", "high")
                raise PasswordError("the library is busy and took only part of the change: set it again in a minute "
                                    "(your admin has been told)")
            time.sleep(1)
    db.set_pw_temp(user, False)
    fp = auth.fingerprint(user)
    if fp and fp is not auth.UNAVAILABLE:
        db.set_pw_fingerprint(user, fp[0])       # this change IS synced: do not warn about it
        out["fp"] = fp[0]
    if config.AUTHELIA_ENABLED:
        db.gate_queue(user, new)
        out["gate"] = True
    return out
