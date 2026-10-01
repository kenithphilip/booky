"""v6.4.0: a reader resets a forgotten password themselves, and it changes EVERYWHERE at once.

  1. /forgot (reachable without signing in; the sign-in page's "Forgot password?" leads here): a
     user name or e-mail address. The answer is always the same, so it never tells whether an
     account exists.
  2. A matching READER gets an e-mail with a one-time link (random, 30 minutes, stored only as its
     SHA-256; asking again cancels the last one). Admins never: their accounts sit behind a second
     factor that an e-mailed link must not get around; they are reset on the server.
  3. /reset/<token>: they choose a new password; passwords.set_everywhere() sets it in
     Audiobookshelf, Calibre-Web (portal, library site, apps, Shelfmark) and the sign-in page, or
     changes nothing. The link is spent, their login lockout cleared, a confirmation mailed to
     them, and the admin told.
Rate limits: a few requests an hour per account and per address, on top of Caddy's per-address
limit on POSTs to these pages."""
import hashlib, logging, secrets, time
from email.message import EmailMessage
import config, cwa, db, passwords

log = logging.getLogger("pwreset")
TTL = 30 * 60
PER_ACCOUNT_HOUR = 3
PER_IP_HOUR = 10


def _hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def available():
    """Self-service needs outgoing mail (the link goes by e-mail)."""
    import kindle
    return kindle.configured()


def _readers_matching(ident):
    ident = (ident or "").strip().lower()
    if not ident:
        return []
    try:
        users = cwa.list_users()
    except Exception:
        return []
    return [u for u in users if not u["is_admin"] and "@" in (u.get("email") or "")
            and (u["name"].lower() == ident or (u.get("email") or "").strip().lower() == ident)]


def _send(to, subject, text):
    import kindle
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if config.ADMIN_EMAIL:
        msg["Reply-To"] = config.ADMIN_EMAIL
    msg.set_content(text)
    kindle._deliver(msg)


def request(ident, ip, now=None):
    """Mail a reset link to the reader this names, if there is one and the limits allow. Returns
    nothing a caller could show: the page says the same thing whatever happened."""
    now = now or time.time()
    if not available():
        return
    if db.reset_asks(ip=ip, since=3600, now=now) >= PER_IP_HOUR:
        log.info("reset: %s over the per-address limit", ip)
        db.reset_asked(ip, None, now)
        return
    matches = _readers_matching(ident)
    db.reset_asked(ip, matches[0]["name"] if matches else None, now)
    for u in matches:
        if db.reset_asks(owner=u["name"], since=3600, now=now) > PER_ACCOUNT_HOUR:
            log.info("reset: %s over the per-account limit", u["name"])
            continue
        token = secrets.token_urlsafe(32)
        db.reset_add(u["name"], _hash(token), ip, TTL, now)
        link = f"{config.PORTAL_URL}/reset/{token}"
        try:
            _send(u["email"], "Reset your library password",
                  f"Hi {u['name']},\n\n"
                  f"Someone (hopefully you) asked to reset the password of your library account, {u['name']}.\n\n"
                  f"To choose a new one, open this link within {TTL // 60} minutes:\n\n  {link}\n\n"
                  "It works once. Your new password then works everywhere: the library, the sign-in page, the "
                  "audiobook app and your reading apps.\n\n"
                  "If it was not you, ignore this e-mail: your password stays as it is.\n")
        except Exception as e:
            log.warning("reset: could not mail %s: %s", u["name"], e)
        db.audit("password_reset_asked", u["name"], ip)


def owner_of(token, now=None):
    return db.reset_owner(_hash(token), now)


def complete(token, new, repeat, ip, now=None):
    """Set the new password everywhere. Returns the reader's name; raises passwords.PasswordError
    (nothing changed) or ValueError (the link is no longer valid)."""
    now = now or time.time()
    owner = db.reset_owner(_hash(token), now)
    if not owner:
        raise ValueError("this link has expired or was already used")
    bad = passwords.check(new, repeat)
    if bad:
        raise passwords.PasswordError(bad)
    passwords.set_everywhere(owner, new)
    db.reset_use(_hash(token), now)
    db.clear_login_failures_for(user=owner)
    db.audit("password_reset_done", owner, ip)
    u = cwa.get_user(owner) or {}
    if "@" in (u.get("email") or ""):
        try:
            _send(u["email"], "Your library password was changed",
                  f"Hi {owner},\n\nThe password of your library account was just changed with a reset link"
                  f"{' (from ' + ip + ')' if ip else ''}. It now works everywhere.\n\n"
                  "If that was not you, tell your library admin straight away.\n")
        except Exception as e:
            log.warning("reset: could not mail the confirmation to %s: %s", owner, e)
    try:
        import notify
        notify.alert(f"{owner} reset their password", f"{owner} chose a new password with a reset link"
                     f"{' from ' + ip if ip else ''}. It was set everywhere.", "default")
    except Exception:
        pass
    return owner
