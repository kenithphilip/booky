"""Notifications. Two channels, both best-effort and never blocking a request:
  - NOTIFY_WEBHOOK: the ADMIN's channel (readers never see it): a POST on every event for every
    reader. Generic hooks get JSON; ntfy (the free default the installer suggests) gets a
    plain-text body with Title/Priority headers so the phone shows a readable message
    (NOTIFY_WEBHOOK_FORMAT=auto|json|ntfy). On ntfy each message also carries:
      Tags         an emoji per kind of event, so the phone says what happened at a glance
      Click        the portal page to open when the notification is tapped
      Actions      a "Review" button on anything waiting for the admin
      Sequence-ID  one notification per request / per problem: a request's later events and a
                   problem's all-clear REPLACE the earlier notification instead of piling up
    Routine successes are Priority low (silent, in the drawer); anything waiting for the admin
    or failed is high.
  - e-mail (the same SMTP as Send-to-Kindle): the requester gets a note when their request is
    done, denied or failed (if they opted in on Devices; own uploads only on failure); the
    admin gets a note when a request waits for approval or imported without an owner tag
    (if ADMIN_EMAIL is set)."""
import json, re, urllib.request, threading, sys
from urllib.parse import urlsplit
from email.message import EmailMessage
import config, db

USER_EVENTS = {"done": "is in your library", "denied": "was denied", "error": "could not be added",
               "needs-tag": "was imported but the admin has to tag it to you first",
               # keep looking (wanted.py): the entry's own events, told to the reader who asked
               "wanted-found": "turned up in a catalog and has been requested for you",
               "wanted-candidate": "may have turned up: confirm it is the right book on your Status page",
               "wanted-expired": "did not turn up in any catalog, so we have stopped looking"}
EVENTS = set(USER_EVENTS) | {"requested", "approved"}

# The admin's view of the same events (webhook only; the reader's mail keeps USER_EVENTS).
ADMIN_EVENTS = {"requested": "requested", "pending": "requested: waiting for your approval",
                "approved": "approved", "done": "added to their library", "denied": "denied",
                "error": "could not be added", "needs-tag": "imported, but needs an owner tag",
                "shared": "given from the family library (nothing downloaded)",
                "wanted-found": "found by Keep looking and requested",
                "wanted-candidate": "possibly found by Keep looking (the reader confirms it)",
                "wanted-expired": "not found by Keep looking; it has stopped looking"}
# ntfy turns a tag that is an emoji short code into that emoji in front of the title
TAGS = {"requested": "inbox_tray", "pending": "raised_hand", "approved": "+1", "done": "books",
        "denied": "no_entry", "error": "x", "needs-tag": "label", "shared": "busts_in_silhouette",
        "wanted-found": "mag", "wanted-candidate": "question", "wanted-expired": "hourglass"}
PRIORITY = {"pending": "high", "error": "high", "needs-tag": "default", "wanted-candidate": "default"}  # else low
ALERT_TAGS = {"urgent": "rotating_light", "high": "rotating_light", "low": "information_source"}   # else warning

def send(event, req):
    # the canary journey's hidden accounts (L08) report through their own run, never as a
    # "library: done" on the family's phones twice a day
    if req.get("owner") in config.CANARY_USERS:
        return
    _webhook(event, req)
    threading.Thread(target=_mail, args=(event, req), daemon=True).start()

def admin(event, req):
    """The admin's notification only (the webhook): for what the portal sees happen elsewhere,
    Shelfmark's requests and failed downloads. The reader is never mailed about these."""
    if req.get("owner") in config.CANARY_USERS:
        return
    _webhook(event, req)

def _ntfy(url):
    fmt = config.NOTIFY_WEBHOOK_FORMAT
    return fmt == "ntfy" or (fmt == "auto" and "ntfy" in (urlsplit(url).hostname or ""))

def portal_url(path="/admin"):
    return f"https://request.{config.DOMAIN}{path}" if config.DOMAIN else ""

def seq_id(*parts):
    """An ntfy Sequence-ID: letters, digits, - and _, at most 64 characters."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", "-".join(str(p) for p in parts if p not in (None, ""))).strip("-")[:64]

def _latin1(s, n=None):
    # HTTP headers must be latin-1: keep them ASCII-safe, the body carries the details
    s = str(s).replace("\r", " ").replace("\n", " ").encode("ascii", "replace").decode()
    return s[:n] if n else s

def _post(url, title, text, priority, payload, tags=None, click=None, seq=None, actions=None):
    """One webhook POST in the configured shape. Raises on failure. tags/click/seq/actions are
    ntfy's (a generic JSON hook gets them as fields, only when set)."""
    if _ntfy(url):
        prio = {"high": "high", "urgent": "urgent", "low": "low", "min": "min"}.get(priority, "default")
        hdr = {"Title": _latin1(title, 200), "Priority": prio, "Content-Type": "text/plain; charset=utf-8"}
        if tags:
            hdr["Tags"] = _latin1(tags)
        if click:
            hdr["Click"] = _latin1(click)
        if seq:
            hdr["Sequence-ID"] = seq_id(seq)
        if actions:
            # "view, <label>, <url>" per action, at most three, separated by ;
            hdr["Actions"] = _latin1("; ".join(f"view, {label}, {u}" for label, u in actions[:3]))
        r = urllib.request.Request(url, data=text.encode("utf-8"), headers=hdr)
    else:
        extra = {k: v for k, v in (("tags", tags), ("click", click), ("seq", seq and seq_id(seq))) if v}
        r = urllib.request.Request(url, data=json.dumps({**payload, **extra}).encode(),
                                   headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=8)

def _webhook(event, req):
    url = config.NOTIFY_WEBHOOK
    if not url:
        return
    body = {"event": event, "title": req.get("title"), "author": req.get("author"), "owner": req.get("owner"),
            "source": req.get("source"), "status": req.get("status"), "detail": req.get("detail")}
    kind = "pending" if event == "requested" and req.get("status") == "pending" else event
    by = f" by {req['author']}" if req.get("author") else ""
    via = f" (via {req['source']})" if req.get("source") else ""
    text = f"\"{req.get('title')}\"{by}\n{req.get('owner')}: {ADMIN_EVENTS.get(kind, kind)}{via}" \
        + (f"\n{req.get('detail')}" if req.get("detail") and event != "done" else "")
    click = portal_url("/status" if kind == "pending" else "/admin")    # Pending card / audit trail
    actions = [("Review", click)] if kind == "pending" and click else None
    # one notification per request: its later events replace the earlier ones on the phone
    seq = req.get("seq") or (seq_id("req", req["id"]) if req.get("id") else None)
    try:
        _post(url, f"{req.get('owner')}: {req.get('title')}", text, PRIORITY.get(kind, "low"), body,
              tags=TAGS.get(kind), click=click or None, seq=seq, actions=actions)
    except Exception:
        pass

def _mail(event, req):
    import kindle, cwa                     # late import: kindle owns the SMTP session helper
    if not kindle.configured():
        return
    try:
        if event == "requested" and req.get("status") == "pending" and config.ADMIN_EMAIL:
            _deliver(config.ADMIN_EMAIL, f"[library] approval needed: {req.get('title')}",
                     f"{req.get('owner')} requested \"{req.get('title')}\" ({req.get('author') or 'unknown author'}) "
                     f"from {req.get('source')}.\n\nApprove or deny it in the portal: "
                     f"https://request.{config.DOMAIN}/status\n")
        if event == "needs-tag" and config.ADMIN_EMAIL:
            _deliver(config.ADMIN_EMAIL, f"[library] needs an owner tag: {req.get('title')}",
                     f"\"{req.get('title')}\" was imported for {req.get('owner')} but carries no owner tag, so "
                     f"{req.get('owner')} cannot see it.\nDetail: {req.get('detail') or ''}\n\n"
                     f"Add the tag in Calibre-Web or Audiobookshelf, then dismiss it: https://request.{config.DOMAIN}/admin\n")
        # own uploads / dropbox files: only failures and untaggable imports are worth a mail
        own = req.get("source") == "dropbox"
        if event in USER_EVENTS and req.get("owner") and not (own and event in ("done", "denied")):
            if not db.get_prefs(req["owner"])["notify_email"]:
                return
            u = cwa.get_user(req["owner"]) or {}
            if u.get("email") and "@" in u["email"]:
                where = "https://request." + config.DOMAIN + ("/library" if event == "done" else "/status")
                _deliver(u["email"], f"[library] {req.get('title')} {USER_EVENTS[event]}",
                         f"Your request \"{req.get('title')}\" {USER_EVENTS[event]}.\n"
                         + (f"Detail: {req.get('detail')}\n" if req.get("detail") and event != "done" else "")
                         + f"\n{where}\n")
    except Exception:
        pass

def _deliver(to, subject, text):
    import kindle
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    kindle._deliver(msg)

def alert(title, text, priority="default", seq=None, tags=None, click=None):
    """Operational alert (backup failed, disk full, container unhealthy, audiobook left
    untagged): webhook + admin mail. Best-effort; returns a list of the channels that took it
    (empty = nobody was told; the reason is printed to stderr). seq: the same value on the
    problem and on its all-clear, so the all-clear replaces the problem on the phone."""
    import kindle
    took = []
    if config.NOTIFY_WEBHOOK:
        try:
            _post(config.NOTIFY_WEBHOOK, f"[bookstack] {title}", text, priority,
                  {"event": "alert", "title": title, "text": text, "priority": priority},
                  tags=tags or ALERT_TAGS.get(priority, "warning"), click=click or portal_url() or None, seq=seq)
            took.append("webhook")
        except Exception as e:
            print(f"alert: webhook failed: {e}", file=sys.stderr)
    mail_it = config.ALERT_MAIL == "all" or (config.ALERT_MAIL != "off" and priority in ("high", "urgent")) \
        or not config.NOTIFY_WEBHOOK            # mail is the only channel: never leave the admin untold
    if config.ADMIN_EMAIL and kindle.configured() and mail_it:
        try:
            _deliver(config.ADMIN_EMAIL, f"[bookstack] {title}", f"{text}\n\npriority: {priority}\n"); took.append("mail")
        except Exception as e:
            print(f"alert: mail failed: {e}", file=sys.stderr)
    return took

def why_undelivered():
    """Human reason an alert reached nobody (for the CLI and the admin page)."""
    import kindle
    missing = []
    if not config.NOTIFY_WEBHOOK:
        missing.append("NOTIFY_WEBHOOK is empty")
    if not config.ADMIN_EMAIL:
        missing.append("ADMIN_EMAIL is empty")
    elif not kindle.configured():
        missing.append("outgoing mail (SMTP) is not configured")
    return "; ".join(missing) or "every configured channel failed (see the errors above)"

def _cli(argv):
    """python -m notify alert "<title>" "<text>" [priority] [--seq ID] [--tags T] [--click URL]
    (scripts/alert.sh, systemd OnFailure=). Exit 0 when at least one channel took the alert, 3
    when nothing was delivered (so the host side logs it to the journal and tries its own
    webhook), 2 on usage errors."""
    opts, pos, it = {}, [], iter(argv)
    for a in it:
        if a in ("--seq", "--tags", "--click"):
            opts[a[2:]] = next(it, "") or None
        else:
            pos.append(a)
    if len(pos) >= 3 and pos[0] == "alert":
        sent = alert(pos[1], pos[2], pos[3] if len(pos) > 3 and pos[3] else "default", **opts)
        if sent:
            print(json.dumps({"ok": True, "sent": sent}))
            return 0
        reason = why_undelivered()
        print(json.dumps({"ok": False, "sent": [], "reason": reason}))
        print(f"alert NOT delivered: {reason}", file=sys.stderr)
        return 3
    print('usage: python -m notify alert "<title>" "<text>" [priority]', file=sys.stderr)
    return 2

if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
