"""Optional email-to-library: poll a mailbox and drop ebook/audio attachments into the
requester's dropbox (the dropbox watcher then tags + ingests them). Route by plus-address,
e.g. mail to books+alice@yourdomain -> user 'alice'. Only mail for an EXISTING user, from an
allowed sender (IMAP_ALLOWED_SENDERS, else the user's own e-mail) whose address the receiving
mail server authenticated (DMARC, or DKIM/SPF aligned with the From domain), is filed; the
rest is marked seen, dropped and written to the audit trail on /admin so a family member
whose mail vanished can be told why. All files are ones the user owns."""
import imaplib, email, os, re, time, logging
from email.utils import parseaddr
import config, cwa, worker, db

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

def _domain_ok(d, dom):
    d = (d or "").lower().strip(".")
    return bool(d) and (d == dom or d.endswith("." + dom) or dom.endswith("." + d))

def _authenticated(msg, frm):
    """True when the receiving MTA vouched for the From address. Only the TOPMOST
    Authentication-Results header counts: it is the one our own mail server added; anything
    below it came with the message and can be forged by the sender."""
    if not config.IMAP_REQUIRE_AUTH:
        return True
    hdrs = msg.get_all("Authentication-Results") or []
    if not hdrs or "@" not in frm:
        return False
    ar = " ".join(str(hdrs[0]).split()).lower()
    dom = frm.rsplit("@", 1)[1]
    if re.search(r"\bdmarc=pass\b", ar):
        return True
    for m in re.finditer(r"\bdkim=pass\b[^;]*?header\.(?:d|i)=@?([^\s;]+)", ar):
        if _domain_ok(m.group(1).split("@")[-1], dom):
            return True
    for m in re.finditer(r"\bspf=pass\b[^;]*?smtp\.mailfrom=([^\s;]+)", ar):
        if _domain_ok(m.group(1).split("@")[-1], dom):
            return True
    return False

def _reject(user, reason, msg):
    log.warning("mail %s dropped (%s) from %r", f"for {user}" if user else "", reason, msg.get("From", ""))
    try:
        db.audit("imap_rejected", user, None, f"{reason}; From {str(msg.get('From', ''))[:80]}; "
                                            f"Subject {str(msg.get('Subject', ''))[:60]}")
    except Exception:
        pass

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

def _size(M, num):
    """RFC822.SIZE before downloading, so a huge message is never pulled into memory."""
    try:
        _, d = M.fetch(num, "(RFC822.SIZE)")
        m = re.search(rb"RFC822\.SIZE (\d+)", d[0] if isinstance(d[0], bytes) else d[0][0])
        return int(m.group(1)) if m else None
    except Exception:
        return None

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
            size = _size(M, num)
            # Parsing a message costs roughly a dozen times the attachment in memory, so the
            # precheck sits just above the attachment cap + base64 inflation (~4/3), not at a
            # multiple of it: a bigger message is refused before it is ever downloaded.
            if size is not None and size > cap * 1.37 + (1 << 18):
                log.warning("mail #%s dropped: %d bytes is over the %d MB limit", num, size, config.MAX_UPLOAD_MB)
                db.audit("imap_rejected", None, None, f"message of {size >> 20} MB is over the {config.MAX_UPLOAD_MB} MB limit")
                M.store(num, "+FLAGS", "\\Seen")
                continue
            # BODY.PEEK[] does NOT set \Seen: a message that kills the process while it is
            # parsed is still unseen afterwards and is retried instead of being lost.
            _, d = M.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(d[0][1])
            target = _target_user(msg)
            user = _existing_user(target)
            frm = (parseaddr(msg.get("From", "") or "")[1] or "").lower()
            if not user:
                _reject(None, f"unknown user {str(target)[:40]!r}", msg)
            elif not _sender_allowed(msg, user):
                _reject(user["name"], "sender not allowed", msg)
            elif not _authenticated(msg, frm):
                _reject(user["name"], "sender not authenticated (no DMARC/DKIM/SPF pass for the From domain)", msg)
            else:
                dest = os.path.join(config.DROPBOX_DIR, user["name"])
                for part in msg.walk():
                    fn = part.get_filename()
                    if not fn or part.get_content_maintype() == "multipart":
                        continue
                    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                    if ext not in exts:
                        # silence here meant a family member's book vanished with no trace
                        # (inline signatures and logos are not attachments and stay quiet)
                        if part.get_content_disposition() == "attachment":
                            db.audit("imap_skipped", user["name"], None,
                                     f"attachment {fn[:80]!r} is not a book or audiobook file type")
                        continue
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    if len(payload) > cap:
                        log.warning("attachment %r for %s dropped: over %d MB", fn, user["name"], config.MAX_UPLOAD_MB)
                        db.audit("imap_skipped", user["name"], None,
                                 f"attachment {fn[:80]!r} is {len(payload) >> 20} MB, over the "
                                 f"{config.MAX_UPLOAD_MB} MB limit")
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
