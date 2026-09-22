"""Optional email-to-library: poll a mailbox and drop ebook/audio attachments into the
requester's dropbox (the dropbox watcher then tags + ingests them). Route by plus-address,
e.g. mail to books+alice@yourdomain -> user 'alice'. Only mail for an EXISTING user, from an
allowed sender (IMAP_ALLOWED_SENDERS, else the user's own e-mail), is filed; the rest is
marked seen and dropped. All files are ones the user owns."""
import imaplib, email, os, time, logging
from email.utils import parseaddr
import config, cwa, worker

log = logging.getLogger("imap")
POLL_SECONDS = 60

def _target_user(msg):
    """The plus-address part, verbatim: it must name an existing user exactly (no cleaning
    that could turn '+../alice' into 'alice')."""
    for hdr in ("Delivered-To", "X-Original-To", "To"):
        v = msg.get(hdr, "") or ""
        local = v.split("@")[0]
        if "+" in local:
            return local.split("+", 1)[1].strip("<> \t") or None
    return config.IMAP_DEFAULT_USER or None

def _existing_user(name):
    if not name or not cwa._valid_name(name):
        return None
    try:
        return cwa.get_user(name)
    except cwa.CwaError:
        return None

def _sender_allowed(msg, user):
    frm = (parseaddr(msg.get("From", "") or "")[1] or "").lower()
    if not frm:
        return False
    if config.IMAP_ALLOWED_SENDERS:
        return frm in config.IMAP_ALLOWED_SENDERS
    return frm == (user.get("email") or "").lower()

def _safe_name(fn):
    stem, dot, ext = os.path.basename(fn).rpartition(".")
    return f"{worker._safe(stem)}.{ext.lower()}"

def _connect():
    if config.IMAP_SSL:
        M = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT or 993)
    else:
        M = imaplib.IMAP4(config.IMAP_HOST, config.IMAP_PORT or 143)
    M.login(config.IMAP_USER, config.IMAP_PASS)
    return M

def poll_once():
    """Fetch unseen mail once; returns the number of attachments filed."""
    exts = tuple(set(config.EBOOK_EXTS + config.AUDIO_EXTS))
    cap = config.MAX_UPLOAD_MB * 1024 * 1024
    filed = 0
    M = _connect()
    try:
        M.select(config.IMAP_FOLDER)
        _, data = M.search(None, "UNSEEN")
        for num in data[0].split():
            _, d = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(d[0][1])
            target = _target_user(msg)
            user = _existing_user(target)
            if not user:
                log.warning("mail for unknown user %r dropped", target)
            elif not _sender_allowed(msg, user):
                log.warning("mail for %s from %r dropped (sender not allowed)", user["name"], msg.get("From", ""))
            else:
                dest = os.path.join(config.DROPBOX_DIR, user["name"])
                for part in msg.walk():
                    fn = part.get_filename()
                    if fn and "." in fn and fn.rsplit(".", 1)[-1].lower() in exts:
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        if len(payload) > cap:
                            log.warning("attachment %r for %s dropped: over %d MB", fn, user["name"], config.MAX_UPLOAD_MB)
                            continue
                        safe = _safe_name(fn)
                        worker.place_in_dropbox(dest, safe, lambda out, data=payload: out.write(data))
                        filed += 1
            M.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return filed

def poll_forever():
    worker._beat("imap")
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("IMAP poll failed")
        worker._beat("imap")
        time.sleep(POLL_SECONDS)
